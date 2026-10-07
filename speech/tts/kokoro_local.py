"""Local TTS: Kokoro-82M via its ONNX build (`kokoro-onnx`), no network, no API key.

Same `TTSProvider` contract as `speech/tts/openai.py` — PCM16 mono at 24 kHz, the same
rate OpenAI's TTS returns, so every caller works unchanged.

Latency design (measured on the dev laptop: ~0.35x real time, i.e. 1 s of speech takes
~0.35 s to generate; first short phrase ~0.4-0.65 s):
- The text is split into speakable pieces and the *first* piece is kept short (up to
  the first comma when that leaves a natural phrase) so the first audio is ready
  quickly; later pieces are full sentences for natural prosody.
- Pieces are generated back-to-back in the background while earlier ones are already
  being streamed out, so playback never waits on generation after the first piece.
- One worker thread for all synthesis: ONNX Runtime already spreads each call over
  several cores (`settings.local_tts_threads`), and running two at once (e.g. VoiceSession's per-sentence pipelining) would only make
  both slower. FIFO order means the first sentence is still synthesized first.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from config.settings import get_settings
from speech.tts.base import TTSProvider

logger = logging.getLogger(__name__)

KOKORO_SAMPLE_RATE_HZ = 24000
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
_CHUNK_BYTES = 4800  # 100 ms of 24 kHz PCM16 per yielded chunk

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kokoro")
_engine = None
_engine_lock = threading.Lock()


def model_paths() -> tuple[Path, Path]:
    base = Path(get_settings().local_models_dir) / "kokoro"
    return base / MODEL_FILE, base / VOICES_FILE


def _load_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            model, voices = model_paths()
            if not model.exists() or not voices.exists():
                raise FileNotFoundError(
                    f"Kokoro model files missing in {model.parent} — run: python scripts/setup_local_models.py"
                )
            s = get_settings()
            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            if s.local_tts_threads > 0:
                options.intra_op_num_threads = s.local_tts_threads  # see settings.local_tts_threads
            session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
            engine = Kokoro.from_session(session, str(voices))
            engine.create("Ready.", voice=s.local_tts_voice, speed=s.local_tts_speed, lang=s.local_tts_lang)  # warm-up
            _engine = engine
            logger.info("kokoro_loaded", extra={"model": str(model)})
    return _engine


async def preload_kokoro() -> None:
    await asyncio.get_running_loop().run_in_executor(_executor, _load_engine)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_FIRST_CLAUSE = re.compile(r"^(.{12,}?[,;:])\s+(.+)$", re.S)
# Words a speaker naturally pauses before; used to break a long opening sentence that
# has no comma ("We can help you sell your products | on Amazon and other ...").
_BREAK_BEFORE = {"and", "but", "or", "so", "because", "to", "on", "for", "with", "about", "if", "when", "while", "that"}
_MAX_FIRST_PIECE_WORDS = 8


def _split_long_opening(sentence: str) -> list[str]:
    words = sentence.split()
    if len(words) <= _MAX_FIRST_PIECE_WORDS + 2:
        return [sentence]
    for i in range(4, min(_MAX_FIRST_PIECE_WORDS, len(words) - 3) + 1):
        if words[i].lower().strip(",;:") in _BREAK_BEFORE:
            return [" ".join(words[:i]), " ".join(words[i:])]
    return [sentence]


def split_for_tts(text: str) -> list[str]:
    """Sentences, with the opening kept short so the first audio is ready sooner
    (Kokoro's time to first audio grows with the length of the first piece: ~0.4 s for
    a short phrase, ~1.1 s for a long sentence). The first sentence is split at its
    first comma when both halves are real phrases (≥3 words), otherwise — if it is
    long — before a natural pause word. Later pieces stay whole sentences."""
    sentences = [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]
    if not sentences:
        return []
    m = _FIRST_CLAUSE.match(sentences[0])
    if m and len(m.group(1).split()) >= 3 and len(m.group(2).split()) >= 3:
        sentences[0:1] = [m.group(1), m.group(2)]
    sentences[0:1] = _split_long_opening(sentences[0])
    return sentences


def _to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class KokoroTTSProvider(TTSProvider):
    sample_rate_hz = KOKORO_SAMPLE_RATE_HZ
    backend = "local"

    def __init__(self) -> None:
        s = get_settings()
        self._voice = s.local_tts_voice
        self._speed = s.local_tts_speed
        self._lang = s.local_tts_lang
        self._active_cancel_events: set[asyncio.Event] = set()

    def _synthesize(self, piece: str) -> bytes:
        engine = _load_engine()
        samples, sample_rate = engine.create(piece, voice=self._voice, speed=self._speed, lang=self._lang)
        if sample_rate != KOKORO_SAMPLE_RATE_HZ:
            raise RuntimeError(f"Kokoro returned {sample_rate} Hz, expected {KOKORO_SAMPLE_RATE_HZ}")
        return _to_pcm16(samples)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        pieces = split_for_tts(text)
        if not pieces:
            return
        loop = asyncio.get_running_loop()
        cancel_event = asyncio.Event()
        self._active_cancel_events.add(cancel_event)
        # Submit every piece now: the single worker thread runs them in order, so
        # piece 2 is being generated while piece 1 is already playing.
        futures = [loop.run_in_executor(_executor, self._synthesize, piece) for piece in pieces]
        try:
            for future in futures:
                if cancel_event.is_set():
                    break
                pcm = await future
                for i in range(0, len(pcm), _CHUNK_BYTES):
                    if cancel_event.is_set():
                        break
                    yield pcm[i : i + _CHUNK_BYTES]
        finally:
            for future in futures:
                future.cancel()  # pieces not started yet are dropped from the queue
            self._active_cancel_events.discard(cancel_event)

    async def cancel(self) -> None:
        for event in list(self._active_cancel_events):
            event.set()

    async def close(self) -> None:
        await self.cancel()

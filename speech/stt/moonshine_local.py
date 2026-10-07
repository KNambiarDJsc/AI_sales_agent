"""Local STT: Moonshine (Useful Sensors) via ONNX Runtime — `useful-moonshine-onnx`.
No network, no API key, English.

Same buffered-per-utterance contract as `speech/stt/openai.py`: audio (PCM16 16 kHz
mono) accumulates via `send_audio`, `receive_final` transcribes it once the turn is
over. Measured on the dev laptop: "base" transcribes 1.5 / 6 / 8 s of speech in about
0.23 / 0.78 / 1.09 s — faster than the OpenAI REST round trip (~0.8 s) for typical
short replies, with no upload.

Note: `pip install moonshine` is an unrelated *satellite imagery* package; the speech
model is `useful-moonshine-onnx` (imported as `moonshine_onnx`).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from config.settings import get_settings
from speech.stt.base import STTProvider, STTStream, TranscriptResult

logger = logging.getLogger(__name__)

SAMPLE_RATE_HZ = 16000
MIN_AUDIO_SECONDS = 0.15  # less than this is a click, not speech
MAX_AUDIO_SECONDS = 30.0  # Moonshine is trained on segments up to ~30 s; keep the tail

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moonshine")
_model = None
_tokenizer = None
_lock = threading.Lock()


def _load():
    global _model, _tokenizer
    with _lock:
        if _model is None:
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer

            name = f"moonshine/{get_settings().local_stt_model}"
            model = MoonshineOnnxModel(model_name=name)
            tokenizer = load_tokenizer()
            model.generate(np.zeros((1, SAMPLE_RATE_HZ // 2), dtype=np.float32))  # warm-up
            _model, _tokenizer = model, tokenizer
            logger.info("moonshine_loaded", extra={"model": name})
    return _model, _tokenizer


async def preload_moonshine() -> None:
    await asyncio.get_running_loop().run_in_executor(_executor, _load)


def transcribe_pcm16(pcm16: bytes) -> str:
    """Blocking; run in `_executor`."""
    model, tokenizer = _load()
    audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    if len(audio) > MAX_AUDIO_SECONDS * SAMPLE_RATE_HZ:
        audio = audio[-int(MAX_AUDIO_SECONDS * SAMPLE_RATE_HZ):]
    tokens = model.generate(audio[None, :])
    return tokenizer.decode_batch(tokens)[0].strip()


class MoonshineSTTStream(STTStream):
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._closed = False

    async def send_audio(self, pcm16_chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("STT stream is closed")
        self._buffer.extend(pcm16_chunk)

    async def receive_partial(self) -> TranscriptResult | None:
        return None

    async def receive_final(self) -> TranscriptResult | None:
        pcm = bytes(self._buffer)
        self._buffer.clear()
        if len(pcm) < MIN_AUDIO_SECONDS * SAMPLE_RATE_HZ * 2:
            return None
        text = await asyncio.get_running_loop().run_in_executor(_executor, transcribe_pcm16, pcm)
        return TranscriptResult(text=text, is_final=True) if text else None

    async def close(self) -> None:
        self._closed = True
        self._buffer.clear()


class MoonshineSTTProvider(STTProvider):
    input_sample_rate_hz = SAMPLE_RATE_HZ
    backend = "local"

    async def start_stream(self) -> STTStream:
        return MoonshineSTTStream()

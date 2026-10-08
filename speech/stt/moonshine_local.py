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


def _cap_threads(model, size: str, threads: int) -> None:
    """The package opens its ONNX sessions with ONNX Runtime's defaults (every core)
    and has no option for it, so they are reopened from the same cached files with a
    thread cap (`settings.local_stt_threads`). Measured on the dev laptop (on battery):
    base took 1.25 s median / 2.41 s worst per utterance on all 8 cores, 0.71 s / 1.00 s
    on 4, same transcripts. Relies on the pinned package version's `encoder`/`decoder`
    attributes (requirements-local.txt)."""
    import onnxruntime as ort
    from moonshine_onnx.model import _get_onnx_weights

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    encoder, decoder = _get_onnx_weights(size, "float")
    model.encoder = ort.InferenceSession(encoder, options, providers=["CPUExecutionProvider"])
    model.decoder = ort.InferenceSession(decoder, options, providers=["CPUExecutionProvider"])


def _load():
    global _model, _tokenizer
    with _lock:
        if _model is None:
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer

            s = get_settings()
            name = f"moonshine/{s.local_stt_model}"
            model = MoonshineOnnxModel(model_name=name)
            if s.local_stt_threads > 0:
                _cap_threads(model, s.local_stt_model, s.local_stt_threads)
            tokenizer = load_tokenizer()
            model.generate(np.zeros((1, SAMPLE_RATE_HZ // 2), dtype=np.float32))  # warm-up
            _model, _tokenizer = model, tokenizer
            logger.info("moonshine_loaded", extra={"model": name})
    return _model, _tokenizer


async def preload_moonshine() -> None:
    await asyncio.get_running_loop().run_in_executor(_executor, _load)


_TRIM_FRAME = SAMPLE_RATE_HZ // 50  # 20 ms
_TRIM_PAD = int(0.3 * SAMPLE_RATE_HZ)


def trim_silence(audio: np.ndarray) -> np.ndarray:
    """Cut leading/trailing silence, keeping 0.3 s around the speech.

    On a phone call the STT buffer holds everything since the last turn — including
    the seconds the customer spent listening to the agent — and Moonshine returns
    *nothing* for an utterance with ~3-5 s of silence in front of it (measured: the
    same clip transcribed correctly with 0-1 s and 8 s of lead-in, empty at 3 s and
    5 s). Push-to-talk in the browser demo never sends that silence, which is why it
    only showed up on the phone path. Loudness is judged per 20 ms frame relative to
    the loudest frames, so a line's background hiss isn't mistaken for speech."""
    frames = len(audio) // _TRIM_FRAME
    if frames < 2:
        return audio
    rms = np.sqrt(np.mean(audio[: frames * _TRIM_FRAME].reshape(frames, _TRIM_FRAME) ** 2, axis=1))
    threshold = max(0.01, 0.1 * float(np.percentile(rms, 95)))
    loud = np.flatnonzero(rms > threshold)
    if loud.size == 0:
        return audio
    start = max(0, loud[0] * _TRIM_FRAME - _TRIM_PAD)
    end = min(len(audio), (loud[-1] + 1) * _TRIM_FRAME + _TRIM_PAD)
    return audio[start:end]


def transcribe_pcm16(pcm16: bytes) -> str:
    """Blocking; run in `_executor`."""
    model, tokenizer = _load()
    audio = trim_silence(np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0)
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

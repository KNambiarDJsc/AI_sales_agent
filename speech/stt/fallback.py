"""STT with automatic OpenAI → local (Moonshine) fallback. See llm/openai_client.py
for when OpenAI counts as unavailable.

The stream buffers the utterance itself (both backends are per-utterance anyway), so
whichever backend ends up transcribing gets the complete audio — including when
OpenAI refuses mid-call and the same utterance has to be redone locally."""
from __future__ import annotations

import asyncio
import logging

from config.settings import get_settings
from llm.openai_client import local_allowed, note_openai_failure, use_openai
from speech.stt.base import STTProvider, STTStream, TranscriptResult
from speech.stt.moonshine_local import MIN_AUDIO_SECONDS, SAMPLE_RATE_HZ, _executor, transcribe_pcm16
from speech.stt.openai import transcribe_pcm16_openai

logger = logging.getLogger(__name__)


class FallbackSTTStream(STTStream):
    def __init__(self, provider: "FallbackSTTProvider") -> None:
        self._provider = provider
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
        s = get_settings()
        if use_openai():
            try:
                text = await transcribe_pcm16_openai(pcm, s.openai_stt_model, s.stt_language)
                self._provider.backend = "openai"
                return TranscriptResult(text=text, is_final=True) if text else None
            except Exception as exc:
                if not (note_openai_failure(exc) and local_allowed()):
                    raise
                logger.warning("stt_falling_back_to_local", extra={"error": type(exc).__name__})
        text = await asyncio.get_running_loop().run_in_executor(_executor, transcribe_pcm16, pcm)
        self._provider.backend = "local"
        return TranscriptResult(text=text, is_final=True) if text else None

    async def close(self) -> None:
        self._closed = True
        self._buffer.clear()


class FallbackSTTProvider(STTProvider):
    input_sample_rate_hz = SAMPLE_RATE_HZ  # both backends take 16 kHz
    backend = "openai"

    async def start_stream(self) -> STTStream:
        return FallbackSTTStream(self)

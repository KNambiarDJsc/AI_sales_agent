"""TTS with automatic OpenAI → local (Kokoro) fallback. See llm/openai_client.py for
when OpenAI counts as unavailable. Both backends produce 24 kHz PCM16, so callers
never notice which one spoke.

Failover only happens before the first audio chunk of an utterance: once the
customer has heard part of a sentence in one voice, finishing it in another would
be worse than the error."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from llm.openai_client import local_allowed, note_openai_failure, use_openai
from speech.tts.base import TTSProvider
from speech.tts.kokoro_local import KOKORO_SAMPLE_RATE_HZ, KokoroTTSProvider
from speech.tts.openai import OPENAI_TTS_SAMPLE_RATE_HZ, OpenAITTSProvider

logger = logging.getLogger(__name__)

assert KOKORO_SAMPLE_RATE_HZ == OPENAI_TTS_SAMPLE_RATE_HZ, "fallback requires both TTS backends at the same rate"


class FallbackTTSProvider(TTSProvider):
    sample_rate_hz = OPENAI_TTS_SAMPLE_RATE_HZ

    def __init__(self, primary: TTSProvider | None = None, secondary: TTSProvider | None = None) -> None:
        self._openai = primary or OpenAITTSProvider()
        self._local = secondary or KokoroTTSProvider()
        self.backend = "openai"

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        if use_openai():
            started = False
            try:
                async for chunk in self._openai.synthesize_stream(text):
                    if not started:
                        started = True
                        self.backend = "openai"
                    yield chunk
                return
            except Exception as exc:
                if started or not (note_openai_failure(exc) and local_allowed()):
                    raise
                logger.warning("tts_falling_back_to_local", extra={"error": type(exc).__name__})
        self.backend = "local"
        async for chunk in self._local.synthesize_stream(text):
            yield chunk

    async def cancel(self) -> None:
        await self._openai.cancel()
        await self._local.cancel()

    async def close(self) -> None:
        await self._openai.close()
        await self._local.close()

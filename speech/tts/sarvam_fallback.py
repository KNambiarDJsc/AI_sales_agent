"""TTS: Sarvam first, OpenAI if Sarvam fails (both 24 kHz PCM16, so callers never
notice the switch).

As in speech/tts/fallback.py, failover only happens before the first audio chunk of a
phrase. After a Sarvam failure the next `_SKIP_AFTER_FAILURE_S` go straight to OpenAI
instead of paying for a failed request on every phrase (e.g. Sarvam credits used up)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from speech.tts.base import TTSProvider
from speech.tts.openai import OPENAI_TTS_SAMPLE_RATE_HZ, OpenAITTSProvider
from speech.tts.sarvam import SARVAM_TTS_SAMPLE_RATE_HZ, SarvamTTSProvider

logger = logging.getLogger(__name__)

assert SARVAM_TTS_SAMPLE_RATE_HZ == OPENAI_TTS_SAMPLE_RATE_HZ, "fallback requires both TTS backends at the same rate"

_SKIP_AFTER_FAILURE_S = 60.0
_sarvam_failed_at: float | None = None


class SarvamFallbackTTSProvider(TTSProvider):
    sample_rate_hz = SARVAM_TTS_SAMPLE_RATE_HZ

    def __init__(self, primary: TTSProvider | None = None, secondary: TTSProvider | None = None) -> None:
        self._sarvam = primary or SarvamTTSProvider()
        self._openai = secondary or OpenAITTSProvider()
        self.backend = "sarvam"

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        global _sarvam_failed_at
        if _sarvam_failed_at is None or time.monotonic() - _sarvam_failed_at > _SKIP_AFTER_FAILURE_S:
            started = False
            try:
                async for chunk in self._sarvam.synthesize_stream(text):
                    if not started:
                        started = True
                        self.backend = "sarvam"
                    yield chunk
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if started:
                    raise
                _sarvam_failed_at = time.monotonic()
                logger.warning("tts_sarvam_failed_using_openai", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
        self.backend = "openai"
        async for chunk in self._openai.synthesize_stream(text):
            yield chunk

    async def cancel(self) -> None:
        await self._sarvam.cancel()
        await self._openai.cancel()

    async def close(self) -> None:
        await self._sarvam.close()
        await self._openai.close()

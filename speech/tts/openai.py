"""OpenAI TTS adapter (Section 8 default).

Uses the streaming speech endpoint (`audio.speech.with_streaming_response.create`) with
`response_format="pcm"` (raw PCM16, 24kHz mono per OpenAI's docs) so audio can start
reaching the customer before the whole utterance is synthesized. voice/audio/processing.py
downsamples 24kHz -> 8kHz mu-law for the telephony leg.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from config.settings import get_settings
from speech.tts.base import TTSProvider

OPENAI_TTS_SAMPLE_RATE_HZ = 24000


class OpenAITTSProvider(TTSProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_tts_model
        self._voice = settings.openai_tts_voice
        self._cancel_event = asyncio.Event()

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        self._cancel_event.clear()
        async with self._client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format="pcm",
        ) as response:
            async for chunk in response.iter_bytes(chunk_size=4096):
                if self._cancel_event.is_set():
                    break
                if chunk:
                    yield chunk

    async def cancel(self) -> None:
        self._cancel_event.set()

    async def close(self) -> None:
        self._cancel_event.set()

"""OpenAI TTS adapter (Section 8 default).

Uses the streaming speech endpoint (`audio.speech.with_streaming_response.create`) with
`response_format="pcm"` (raw PCM16, 24kHz mono per OpenAI's docs) so audio can start
reaching the customer before the whole utterance is synthesized. voice/audio/processing.py
downsamples 24kHz -> 8kHz mu-law for the telephony leg.

Each call to `synthesize_stream()` gets its own cancellation token instead of sharing
one on `self`. That matters once more than one synthesis can be in flight at a time —
`voice/session/session.py`'s sentence-pipelining starts synthesizing sentence 2 while
sentence 1 is still streaming/playing — because a single shared, clear-on-start flag
would let a fresh call silently un-cancel an older one that was mid-cancellation.
`cancel()` still stops everything currently active at once, which is exactly the
barge-in behavior we want (Section 18): all pipelined sentences stop together.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from config.settings import get_settings
from llm.openai_client import get_openai_client
from speech.tts.base import TTSProvider

OPENAI_TTS_SAMPLE_RATE_HZ = 24000


class OpenAITTSProvider(TTSProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.openai_tts_model
        self._voice = settings.openai_tts_voice
        self._active_cancel_events: set[asyncio.Event] = set()

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        cancel_event = asyncio.Event()
        self._active_cancel_events.add(cancel_event)
        try:
            async with get_openai_client().audio.speech.with_streaming_response.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="pcm",
            ) as response:
                # 1200 bytes = 25 ms of 24 kHz PCM16. A 4096-byte chunk held the first
                # ~85 ms of audio back until it was full, adding that much latency to
                # every reply before anything could play.
                async for chunk in response.iter_bytes(chunk_size=1200):
                    if cancel_event.is_set():
                        break
                    if chunk:
                        yield chunk
        finally:
            self._active_cancel_events.discard(cancel_event)

    async def cancel(self) -> None:
        """Stop every synthesis currently in flight, not just one — safe even if
        nothing is active (Section 18's contract for barge-in)."""
        for event in list(self._active_cancel_events):
            event.set()

    async def close(self) -> None:
        await self.cancel()

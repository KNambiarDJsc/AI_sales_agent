"""Phrases in, audio out — in order, with synthesis of each phrase started the moment
the phrase exists.

Used by both the phone path (`VoiceSession`) and the browser demo. The phrases come
from the engine's `on_speech_stream` while the LLM is still writing the reply, so the
first phrase is already being spoken while later ones are being written and
synthesized. Each phrase gets its own TTS request as soon as it arrives (no waiting
for the previous phrase's audio to finish), and audio is yielded strictly in phrase
order. A TTS provider that can only synthesize one thing at a time (local Kokoro)
serializes them internally, FIFO, which preserves the same ordering.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterable

from speech.tts.base import TTSProvider

_DONE = object()


async def phrases_from(texts: Iterable[str]) -> AsyncIterator[str]:
    for text in texts:
        yield text


async def synthesize_phrases(tts: TTSProvider, phrases: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """Yield PCM16 chunks for each phrase, in order. Cancelling the consumer (barge-in)
    cancels every in-flight synthesis; a TTS error is raised to the consumer."""
    ordered: asyncio.Queue = asyncio.Queue()  # per-phrase audio queues, in phrase order
    producers: list[asyncio.Task] = []

    async def produce(text: str, out: asyncio.Queue) -> None:
        try:
            async for chunk in tts.synthesize_stream(text):
                await out.put(chunk)
            await out.put(_DONE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - handed to the consumer, which decides
            await out.put(exc)

    async def feed() -> None:
        try:
            async for phrase in phrases:
                out: asyncio.Queue = asyncio.Queue()
                producers.append(asyncio.create_task(produce(phrase, out)))
                await ordered.put(out)
        finally:
            await ordered.put(_DONE)

    feeder = asyncio.create_task(feed())
    try:
        while True:
            out = await ordered.get()
            if out is _DONE:
                break
            while True:
                item = await out.get()
                if item is _DONE:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
    finally:
        feeder.cancel()
        for task in producers:
            task.cancel()
        for task in (feeder, *producers):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

"""Pre-recorded acknowledgments ("Okay." / "जी।", from the script's `acknowledgments`
config), played the instant the customer stops talking.

The real reply takes 2-3 s to arrive (speech recognition, the LLM's first phrase and
the first TTS audio are each a network round trip). An instant acknowledgment fills
that gap the way a person says "okay…" while thinking. It is only useful if it costs
nothing at that moment, so each phrase is synthesized once, in the background, before
it is needed, and kept in memory per TTS voice; `cached()` never synthesizes on the
hot path — a phrase that isn't recorded yet is simply skipped this turn.
"""
from __future__ import annotations

import asyncio
import logging

from speech.tts.base import TTSProvider

logger = logging.getLogger(__name__)

_cache: dict[tuple[str, str], bytes] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _key(tts: TTSProvider, text: str) -> tuple[str, str]:
    voice = f"{type(tts).__name__}:{getattr(tts, '_voice', '')}:{getattr(tts, '_model', '')}:{tts.sample_rate_hz}"
    return voice, text


def cached(tts: TTSProvider, text: str) -> bytes | None:
    """The recorded audio (PCM16 at the TTS rate), or None if not recorded yet."""
    return _cache.get(_key(tts, text))


async def prerecord(tts: TTSProvider, texts: list[str]) -> None:
    """Record every phrase not recorded yet. Never raises — a missing acknowledgment
    only means silence instead of "Okay." for that turn."""
    for text in texts:
        key = _key(tts, text)
        lock = _locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in _cache:
                continue
            try:
                _cache[key] = b"".join([chunk async for chunk in tts.synthesize_stream(text)])
            except Exception as exc:  # noqa: BLE001
                logger.warning("acknowledgment_prerecord_failed", extra={"text": text, "error": type(exc).__name__})

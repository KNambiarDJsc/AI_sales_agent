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
from collections import deque

from speech.tts.base import TTSProvider

logger = logging.getLogger(__name__)

_cache: dict[tuple[str, str], bytes] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}
# One-off recordings (each lead's own opening line), oldest first — capped so a day of
# calls doesn't keep every greeting in memory.
_one_off: deque[tuple[str, str]] = deque()
_MAX_ONE_OFF = 20


async def prerecord_one_off(tts: TTSProvider, text: str) -> None:
    """Record `text` for a single upcoming use (a lead's greeting, recorded while the
    phone rings so it plays the instant they pick up)."""
    await prerecord(tts, [text])
    key = _key(tts, text)
    if key in _cache and key not in _one_off:
        _one_off.append(key)
        while len(_one_off) > _MAX_ONE_OFF:
            _cache.pop(_one_off.popleft(), None)


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


# What a listener says *to* someone who is talking ("haan", "okay", "hmm") — said over
# the agent, these are not answers and get no reply. Anything else said over the
# agent (a "no", a question) is a real turn and is answered once the agent finishes.
_BACKCHANNEL_WORDS = frozenset(
    "ok okay okk k hmm hm hmmm mm mhm mmm uh um uhh huh ah oh achha acha accha haan han ha haa ji jee "
    "yes yeah yep yup ya right sure alright fine theek thik hai hello hi "
    "हाँ हां हा जी अच्छा ठीक है हम्म ओके हेलो".split()
)


def is_backchannel(text: str) -> bool:
    words = "".join(c if c.isalnum() or c.isspace() or ("\u0900" <= c <= "\u097f" and c not in "\u0964\u0965") else " " for c in text.lower()).split()
    return 0 < len(words) <= 3 and all(w in _BACKCHANNEL_WORDS for w in words)

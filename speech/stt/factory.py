"""Picks the STT provider from settings.ai_backend / settings.stt_backend.

- ai_backend "openai": OpenAI only — `stt_backend` chooses REST ("buffered") or the
  Realtime API ("realtime").
- ai_backend "local": Moonshine only.
- ai_backend "auto": OpenAI REST with automatic Moonshine fallback (the Realtime API
  isn't used here: it needs 24 kHz input and a live socket, which doesn't fail over
  cleanly per utterance; it measured only ~0.1 s faster anyway).

Not cached: a new provider per call is cheap (models are shared underneath), and
`get_settings()` changes (tests, reloads) take effect.
"""
from __future__ import annotations

from config.settings import get_settings
from speech.stt.base import STTProvider


def get_stt_provider(backend: str | None = None) -> STTProvider:
    s = get_settings()
    mode = s.ai_backend
    if mode == "local":
        from speech.stt.moonshine_local import MoonshineSTTProvider

        return MoonshineSTTProvider()
    if mode == "auto":
        from speech.stt.fallback import FallbackSTTProvider

        return FallbackSTTProvider()
    name = backend or s.stt_backend
    if name == "realtime":
        from speech.stt.openai_realtime import OpenAIRealtimeSTTProvider

        return OpenAIRealtimeSTTProvider()
    if name == "buffered":
        from speech.stt.openai import OpenAISTTProvider

        return OpenAISTTProvider()
    raise ValueError(f"Unknown STT backend: {name!r}. Known: ['buffered', 'realtime']")

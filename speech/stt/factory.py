from __future__ import annotations

from functools import lru_cache

from config.settings import get_settings
from speech.stt.base import STTProvider
from speech.stt.openai import OpenAISTTProvider
from speech.stt.openai_realtime import OpenAIRealtimeSTTProvider

_PROVIDERS: dict[str, type[STTProvider]] = {
    "buffered": OpenAISTTProvider,
    "realtime": OpenAIRealtimeSTTProvider,
}


@lru_cache
def get_stt_provider(backend: str | None = None) -> STTProvider:
    name = backend or get_settings().stt_backend
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(f"Unknown STT backend: {name!r}. Known: {list(_PROVIDERS)}")
    return provider_cls()

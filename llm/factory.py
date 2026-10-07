"""Picks the LLM provider from settings.ai_backend (see llm/openai_client.py)."""
from __future__ import annotations

from config.settings import get_settings
from llm.base import LLMProvider


def get_llm_provider() -> LLMProvider:
    mode = get_settings().ai_backend
    if mode == "openai":
        from llm.openai import OpenAILLMProvider

        return OpenAILLMProvider()
    if mode == "local":
        from llm.ollama import OllamaLLMProvider

        return OllamaLLMProvider()
    from llm.fallback import FallbackLLMProvider

    return FallbackLLMProvider()

"""LLM with automatic OpenAI → local (Ollama) fallback. See llm/openai_client.py for
when OpenAI counts as unavailable.

Failover happens only before the first token: the orchestrator may already be
speaking from a partially streamed reply (speculative TTS), and splicing a second
model's output onto it would be incoherent. A failure after that surfaces as an
ordinary error and the engine's own fallback handling takes over, exactly as before.
The validator checks both backends' output identically — the local model gets no
extra authority."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from llm.base import LLMMessage, LLMProposal, LLMProvider
from llm.ollama import OllamaLLMProvider
from llm.openai import OpenAILLMProvider
from llm.openai_client import local_allowed, note_openai_failure, use_openai

logger = logging.getLogger(__name__)


class FallbackLLMProvider(LLMProvider):
    def __init__(self, primary: LLMProvider | None = None, secondary: LLMProvider | None = None) -> None:
        self._openai = primary or OpenAILLMProvider()
        self._local = secondary or OllamaLLMProvider()
        self.backend = "openai"

    async def propose(self, messages: list[LLMMessage], json_schema: dict, schema_name: str = "agent_response") -> LLMProposal:
        if use_openai():
            try:
                result = await self._openai.propose(messages, json_schema, schema_name)
                self.backend = "openai"
                return result
            except Exception as exc:
                if not (note_openai_failure(exc) and local_allowed()):
                    raise
                logger.warning("llm_falling_back_to_local", extra={"error": type(exc).__name__})
        self.backend = "local"
        return await self._local.propose(messages, json_schema, schema_name)

    async def propose_stream(
        self, messages: list[LLMMessage], json_schema: dict, schema_name: str = "agent_response"
    ) -> AsyncIterator[str]:
        if use_openai():
            started = False
            try:
                async for delta in self._openai.propose_stream(messages, json_schema, schema_name):
                    if not started:
                        started = True
                        self.backend = "openai"
                    yield delta
                return
            except Exception as exc:
                if started or not (note_openai_failure(exc) and local_allowed()):
                    raise
                logger.warning("llm_falling_back_to_local", extra={"error": type(exc).__name__})
        self.backend = "local"
        async for delta in self._local.propose_stream(messages, json_schema, schema_name):
            yield delta

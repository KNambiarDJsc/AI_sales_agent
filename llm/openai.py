"""OpenAI LLM adapter (Section 9 default)."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

from config.settings import get_settings
from llm.base import LLMMessage, LLMProposal, LLMProvider
from llm.openai_client import get_openai_client


class OpenAILLMProvider(LLMProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.openai_llm_model
        self._reasoning_effort = settings.openai_llm_reasoning_effort

    def _request_kwargs(self, messages: list[LLMMessage], schema_name: str, json_schema: dict) -> dict:
        kwargs = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            },
        }
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        return kwargs

    async def propose(
        self,
        messages: list[LLMMessage],
        json_schema: dict,
        schema_name: str = "agent_response",
    ) -> LLMProposal:
        start = time.monotonic()
        response = await get_openai_client().chat.completions.create(
            **self._request_kwargs(messages, schema_name, json_schema)
        )
        latency_ms = (time.monotonic() - start) * 1000
        choice = response.choices[0]
        raw_text = choice.message.content or ""
        usage = response.usage
        return LLMProposal(
            raw_text=raw_text,
            model=response.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            latency_ms=latency_ms,
        )

    async def propose_stream(
        self,
        messages: list[LLMMessage],
        json_schema: dict,
        schema_name: str = "agent_response",
    ) -> AsyncIterator[str]:
        # Verified against a live key (STATUS.md): streaming yields incremental deltas
        # alongside strict json_schema, which is what speculative TTS depends on.
        stream = await get_openai_client().chat.completions.create(
            **self._request_kwargs(messages, schema_name, json_schema), stream=True
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

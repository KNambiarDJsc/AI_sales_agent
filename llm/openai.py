"""OpenAI LLM adapter (Section 9 default)."""
from __future__ import annotations

import time

from openai import AsyncOpenAI

from config.settings import get_settings
from llm.base import LLMMessage, LLMProposal, LLMProvider


class OpenAILLMProvider(LLMProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_llm_model

    async def propose(
        self,
        messages: list[LLMMessage],
        json_schema: dict,
        schema_name: str = "agent_response",
    ) -> LLMProposal:
        start = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            },
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

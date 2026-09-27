"""OpenAI LLM adapter (Section 9 default)."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from config.settings import get_settings
from llm.base import LLMMessage, LLMProposal, LLMProvider


class OpenAILLMProvider(LLMProvider):
    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_llm_model

    def _response_format(self, schema_name: str, json_schema: dict) -> dict:
        return {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
        }

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
            response_format=self._response_format(schema_name, json_schema),
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
        # NOTE: streaming `chat.completions` together with strict `json_schema`
        # structured outputs is supported by the documented API surface, but this
        # environment has no live API key to smoke-test it against. If deltas never
        # arrive incrementally in practice (e.g. a future model buffers internally and
        # emits everything in one chunk), the speculative path in
        # orchestrator/streaming.py degrades to "ready right at the end" — same
        # latency as the non-streaming path, not worse. Verify with a real key before
        # flipping settings.enable_speculative_tts on by default.
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            response_format=self._response_format(schema_name, json_schema),
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

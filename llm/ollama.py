"""Local LLM via Ollama's native API (https://github.com/ollama/ollama/blob/main/docs/api.md).

Same `LLMProvider` contract as `llm/openai.py`: raw JSON text in, the orchestrator's
validator decides what to trust. The response is constrained to the per-turn JSON
schema with Ollama's `format` field (grammar-constrained sampling, the local
equivalent of OpenAI strict structured outputs), so a small model can't produce a
state name that doesn't exist — the validator still checks everything.

Native API rather than Ollama's OpenAI-compatible one because only the native API
lets us set `num_ctx` (Ollama's default context would silently drop the start of
the prompt — the system prompt — on long calls) and `keep_alive` (keep the model
in memory; reloading it costs seconds on the first turn after idling).
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from config.settings import get_settings
from llm.base import LLMMessage, LLMProposal, LLMProvider

# Keep the model resident: it's the backup, and a cold load (10+ s) would blow the
# engine's LLM deadline on exactly the turn where OpenAI just failed.
KEEP_ALIVE = "24h"

# Known and measured (2026-10-07): Ollama's Llama 3 template joins every system message
# into the system block at the top, so the engine's per-turn context message
# (orchestrator/prompts.py:build_messages) doesn't stay next to the customer's line
# here, and the history after it is re-read each turn. Sending it in place, as a marked
# block in the user turn, was tested: no faster on calls of normal length and slightly
# worse replies with llama3.2:3b (fallbacks, a repeated line), so messages are sent
# unchanged. Revisit if long calls get slow on the local model.


class OllamaLLMProvider(LLMProvider):
    backend = "local"

    def __init__(self, model: str | None = None) -> None:
        s = get_settings()
        self._base_url = s.local_llm_base_url.rstrip("/")
        self._model = model or s.local_llm_model
        # num_predict caps the reply: a full turn (speech + facts + a tool call) is
        # ~100-250 tokens; a small model that starts looping is cut off instead of
        # running past the engine's deadline.
        self._options = {"num_ctx": s.local_llm_num_ctx, "temperature": s.local_llm_temperature, "num_predict": 400}
        self._timeout = s.local_llm_timeout_seconds
        self._think = s.local_llm_think

    def _payload(self, messages: list[LLMMessage], json_schema: dict, stream: bool) -> dict:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "format": json_schema,
            "stream": stream,
            "keep_alive": KEEP_ALIVE,
            "options": self._options,
        }
        if self._think is not None:
            payload["think"] = self._think
        return payload

    async def propose(self, messages: list[LLMMessage], json_schema: dict, schema_name: str = "agent_response") -> LLMProposal:
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base_url}/api/chat", json=self._payload(messages, json_schema, stream=False))
        resp.raise_for_status()
        body = resp.json()
        return LLMProposal(
            raw_text=(body.get("message") or {}).get("content", ""),
            model=f"ollama/{self._model}",
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
            latency_ms=(time.monotonic() - start) * 1000,
        )

    async def propose_stream(
        self, messages: list[LLMMessage], json_schema: dict, schema_name: str = "agent_response"
    ) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", f"{self._base_url}/api/chat", json=self._payload(messages, json_schema, stream=True)
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(f"Ollama error: {chunk['error']}")
                    content = (chunk.get("message") or {}).get("content")
                    if content:
                        yield content
                    if chunk.get("done"):
                        break


async def warm_up_ollama() -> None:
    """Load the model into memory now (an empty request does exactly that) and keep
    it there, so the first customer turn doesn't pay the model-load time."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{s.local_llm_base_url.rstrip('/')}/api/generate",
            json={"model": s.local_llm_model, "keep_alive": KEEP_ALIVE, "options": {"num_ctx": s.local_llm_num_ctx}},
        )
        resp.raise_for_status()

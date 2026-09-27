"""LLM provider abstraction (Section 9).

Deliberately generic and unaware of orchestrator/schema.py — the LLM layer's only job
is "given a system prompt, message history, and a JSON schema, return raw JSON text
matching that schema as best it can." orchestrator/validator.py is what actually
parses and validates the result and decides whether to trust it. This keeps the LLM
provider swappable (OpenAI today, something else later) without the orchestrator
caring, and keeps "the LLM is not the authority" enforceable in one place.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMProposal:
    raw_text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float | None = None


class LLMProvider(ABC):
    @abstractmethod
    async def propose(
        self,
        messages: list[LLMMessage],
        json_schema: dict,
        schema_name: str = "agent_response",
    ) -> LLMProposal:
        """Return the model's raw JSON-text response constrained to `json_schema`.
        Must NOT raise on a malformed/refused response — return whatever text came
        back (even empty) and let the caller's validator decide; timeouts and
        transport errors should still raise, since those are retriable failures, not
        validation failures."""

    @abstractmethod
    def propose_stream(
        self,
        messages: list[LLMMessage],
        json_schema: dict,
        schema_name: str = "agent_response",
    ) -> AsyncIterator[str]:
        """Same contract as `propose()`, but yields raw text deltas as they arrive
        instead of returning once the full response is complete. Lets a caller (see
        orchestrator/streaming.py) start acting on part of the JSON — specifically the
        `speech` field — before the rest of the structured object has finished
        streaming. Concatenating every yielded delta must equal exactly what `propose`
        would have returned as `raw_text`; callers still run that full text through
        the normal validator before trusting anything beyond the speculative speech."""

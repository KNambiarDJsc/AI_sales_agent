"""The LLM's structured output contract (Section 9).

The LLM proposes; it never has final authority. Every field here is a *proposal* that
orchestrator/validator.py checks against the active script's allowed transitions/tools
before anything is acted on.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCallProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentResponseProposal(BaseModel):
    """Exactly what Section 9's example shows, extended with extracted_facts so
    qualification has something to work with."""

    model_config = ConfigDict(extra="forbid")

    state: str  # proposed next state — validated against the script's transitions
    speech: str  # what the agent should say next
    intent: str = "unknown"  # customer's inferred intent this turn
    extracted_facts: dict[str, Any] = Field(default_factory=dict)
    tool_call: ToolCallProposal | None = None
    end_call: bool = False


# JSON Schema handed to the LLM provider (Section 9/11) — kept in sync with the model
# above by hand rather than via Pydantic's json_schema() export, because OpenAI's
# strict structured-output mode requires `additionalProperties: false` and fully
# required fields at every level, which needs a couple of manual tweaks.
AGENT_RESPONSE_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "state": {"type": "string"},
        "speech": {"type": "string"},
        "intent": {"type": "string"},
        "extracted_facts": {"type": "object", "additionalProperties": True},
        "tool_call": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["name", "arguments"],
                },
                {"type": "null"},
            ]
        },
        "end_call": {"type": "boolean"},
    },
    "required": ["state", "speech", "intent", "extracted_facts", "tool_call", "end_call"],
}


class ValidationOutcome(BaseModel):
    """What orchestrator/validator.py returns: the (possibly downgraded) proposal that
    is actually safe to act on, plus why anything was rejected/changed."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    proposal: AgentResponseProposal
    rejections: list[str] = Field(default_factory=list)
    used_fallback: bool = False

"""The LLM's structured output contract (Section 9).

The LLM proposes; it never has final authority. Every field here is a *proposal* that
orchestrator/validator.py checks against the active script's allowed transitions/tools
before anything is acted on.

`extracted_facts` and `tool_call.arguments` are transmitted on the wire as JSON-encoded
*strings*, not nested objects — confirmed against a live key (see STATUS.md): OpenAI's
strict `json_schema` mode rejects an object schema with `additionalProperties: true`
("... is required to be supplied and to be false"), and strict mode has no true
open-ended/map type. Since both fields are genuinely open-ended by design (arbitrary,
campaign-specific facts; per-tool argument shapes), the fix is to let the model emit
them as a JSON string and decode that ourselves — `ToolCallProposal`/
`AgentResponseProposal` do this transparently via a `mode="before"` validator, so every
other caller (validator.py, tests, tools/registry.py) keeps working with plain dicts
as before and never sees the wire-format detail.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _coerce_json_object(value: Any) -> Any:
    """Accepts either an already-parsed dict (tests, internal callers) or a
    JSON-encoded string (what the LLM actually sends under strict mode). Never
    raises: a malformed string degrades to an empty dict rather than blowing up the
    whole proposal over one flexible field — the overall JSON envelope already parsed
    fine by the time Pydantic sees this."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return value


class ToolCallProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def _parse_arguments(cls, value: Any) -> Any:
        return _coerce_json_object(value)


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

    @field_validator("extracted_facts", mode="before")
    @classmethod
    def _parse_extracted_facts(cls, value: Any) -> Any:
        return _coerce_json_object(value)


# JSON Schema handed to the LLM provider (Section 9/11) — kept in sync with the model
# above by hand rather than via Pydantic's json_schema() export, because OpenAI's
# strict structured-output mode requires `additionalProperties: false` and fully
# required fields at every level, which needs a couple of manual tweaks.
#
# `state` is intentionally just `{"type": "string"}` here, not an enum — this is the
# shape used when the caller has no script context (tests, anything not going through
# build_agent_response_schema below). Real conversation turns must use
# build_agent_response_schema() instead: leaving `state` unconstrained let the model
# propose states that don't exist at all (e.g. "EXPLAIN", "QUALIFY_INTEREST") and
# states it can't reach yet by skipping ahead (INTRO straight to QUALIFICATION) -
# caught on a live call, where every one of those got rejected by the validator and
# fell back to the same re-prompt every single turn, looping forever since the model
# never got to try a state transition the validator would actually accept.
AGENT_RESPONSE_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "state": {"type": "string"},
        "speech": {"type": "string"},
        "intent": {"type": "string"},
        "extracted_facts": {
            "type": "string",
            "description": (
                "A JSON-encoded object of any facts extracted this turn, e.g. "
                '\'{"interested_in_amazon_selling": true}\'. Use \'{}\' if none.'
            ),
        },
        "tool_call": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {
                            "type": "string",
                            "description": "A JSON-encoded object of arguments for this tool. Use '{}' if none.",
                        },
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


def build_agent_response_schema(allowed_states: list[str], intent_options: list[str] | None = None) -> dict:
    """The real per-turn schema: AGENT_RESPONSE_JSON_SCHEMA with `state` constrained to
    an enum of exactly the states reachable from wherever the conversation is right
    now (`ScriptConfig.allowed_next_states(current_state)` — current state + global
    safety states + this state's own transitions_on targets). OpenAI's strict
    json_schema mode enforces enum membership at sampling time, so the model cannot
    hallucinate a nonexistent state name or skip ahead to one it isn't allowed to
    reach yet — both caught live, both previously only caught after the fact by
    validator.py, which meant a full wasted turn (and, in the skip-ahead case, the
    conversation stuck re-asking the same fallback question forever, since every
    retry proposed the same disallowed skip again).

    With `intent_options` (the current state's `transitions_on` labels, e.g.
    "confirmed_identity", plus "other"), `intent` becomes an enum and is generated
    *first*: structured output is produced field by field in schema order, so a model
    that has to commit to `state` before saying anything about what the customer meant
    tends to just repeat the current state — every local model tested stayed in INTRO
    after "Yes, this is Naman". Classifying the reply first, in the script's own
    vocabulary, makes the state choice follow from it. A few extra tokens, and
    `state` still precedes `speech` (speculative TTS gates on it)."""
    schema = json.loads(json.dumps(AGENT_RESPONSE_JSON_SCHEMA))  # cheap deep copy, no extra dependency
    schema["properties"]["state"] = {"type": "string", "enum": list(allowed_states)}
    if intent_options:
        props = schema["properties"]
        props["intent"] = {
            "type": "string",
            "enum": list(intent_options),
            "description": "What the customer's latest message means, using the labels in 'How to choose the next state'.",
        }
        schema["properties"] = {"intent": props.pop("intent"), **props}
        schema["required"] = ["intent"] + [k for k in schema["required"] if k != "intent"]
    return schema


class ValidationOutcome(BaseModel):
    """What orchestrator/validator.py returns: the (possibly downgraded) proposal that
    is actually safe to act on, plus why anything was rejected/changed."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    proposal: AgentResponseProposal
    rejections: list[str] = Field(default_factory=list)
    used_fallback: bool = False

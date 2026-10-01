"""Validates and, where necessary, downgrades the LLM's proposal (Section 9/10).

This is the security/reliability boundary described in Section 22: the LLM's raw text
never reaches the state machine, tools, or the customer's ears until it has passed
through here. Any failure (malformed JSON, schema violation, disallowed transition)
results in a safe fallback response using the script's own configured fallback line —
never a silent guess, never a crash, never a claim of success.

Rejections are handled at two different granularities on purpose (this is a latency-
and-quality fix over the original all-or-nothing version): an invalid `state` means we
can't trust anything the model said (the spoken text is usually written assuming that
transition happened, so speaking it while refusing the transition would leave the
conversation incoherent) and forces a full fallback. An invalid `tool_call` with a
perfectly valid `state`/`speech` does NOT discard the agent's actual response — the
tool call is a side effect independent of what the customer hears, so we just drop it
and keep talking naturally instead of replacing a good response with a generic "sorry,
could you repeat that?" line.
"""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from orchestrator.schema import AgentResponseProposal, ValidationOutcome
from orchestrator.state_machine import StateMachine

logger = logging.getLogger(__name__)


def build_fallback_outcome(
    state_machine: StateMachine, reason: str, lead_fields: dict | None = None
) -> ValidationOutcome:
    """Public so callers outside this module (e.g. orchestrator/engine.py handling a
    transport error/timeout from the LLM call itself, which never reaches this
    validator at all) can produce the exact same safe shape.

    `lead_fields` fills `{contact_name}`/`{business_name}`/etc. placeholders in the
    script's fallback line (`orchestrator/state_machine.py:substitute_placeholders`) —
    without it, a fallback would speak the literal, unsubstituted `{business_name}`
    token to the customer, which is exactly the bug this parameter exists to prevent."""
    fallback = AgentResponseProposal(
        state=state_machine.current_state,
        speech=state_machine.fallback_response(lead_fields),
        intent="unknown",
        extracted_facts={},
        tool_call=None,
        end_call=False,
    )
    return ValidationOutcome(accepted=False, proposal=fallback, rejections=[reason], used_fallback=True)


def validate_llm_response(
    raw_text: str, state_machine: StateMachine, lead_fields: dict | None = None
) -> ValidationOutcome:
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("llm_output_validation_failed", extra={"reason": "invalid_json"})
        return build_fallback_outcome(state_machine, "LLM output was not valid JSON", lead_fields)

    try:
        proposal = AgentResponseProposal.model_validate(payload)
    except ValidationError as exc:
        logger.warning("llm_output_validation_failed", extra={"reason": "schema_violation", "errors": str(exc)})
        return build_fallback_outcome(state_machine, f"LLM output failed schema validation: {exc}", lead_fields)

    if not state_machine.is_transition_allowed(proposal.state):
        reason = f"Proposed state '{proposal.state}' is not reachable from '{state_machine.current_state}'"
        logger.warning("llm_output_rejected", extra={"rejections": [reason]})
        outcome = build_fallback_outcome(state_machine, reason, lead_fields)
        outcome.rejections = [reason]
        return outcome

    rejections: list[str] = []
    if proposal.tool_call is not None:
        allowed = state_machine.allowed_tools()
        if proposal.tool_call.name not in allowed:
            reason = (
                f"Tool '{proposal.tool_call.name}' is not allowed in state "
                f"'{state_machine.current_state}' — dropped, speech and state kept"
            )
            rejections.append(reason)
            logger.warning("llm_tool_call_dropped", extra={"reason": reason})
            proposal = proposal.model_copy(update={"tool_call": None})

    return ValidationOutcome(accepted=True, proposal=proposal, rejections=rejections, used_fallback=False)

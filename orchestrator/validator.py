"""Validates and, where necessary, downgrades the LLM's proposal (Section 9/10).

This is the security/reliability boundary described in Section 22: the LLM's raw text
never reaches the state machine, tools, or the customer's ears until it has passed
through here. Any failure (malformed JSON, schema violation, disallowed transition,
disallowed tool) results in a safe fallback response using the script's own configured
fallback line — never a silent guess, never a crash, never a claim of success.
"""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from orchestrator.schema import AgentResponseProposal, ValidationOutcome
from orchestrator.state_machine import StateMachine

logger = logging.getLogger(__name__)


def _fallback_outcome(state_machine: StateMachine, reason: str) -> ValidationOutcome:
    fallback = AgentResponseProposal(
        state=state_machine.current_state,
        speech=state_machine.fallback_response(),
        intent="unknown",
        extracted_facts={},
        tool_call=None,
        end_call=False,
    )
    return ValidationOutcome(accepted=False, proposal=fallback, rejections=[reason], used_fallback=True)


def validate_llm_response(raw_text: str, state_machine: StateMachine) -> ValidationOutcome:
    rejections: list[str] = []

    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("llm_output_validation_failed", extra={"reason": "invalid_json"})
        return _fallback_outcome(state_machine, "LLM output was not valid JSON")

    try:
        proposal = AgentResponseProposal.model_validate(payload)
    except ValidationError as exc:
        logger.warning("llm_output_validation_failed", extra={"reason": "schema_violation", "errors": str(exc)})
        return _fallback_outcome(state_machine, f"LLM output failed schema validation: {exc}")

    if not state_machine.is_transition_allowed(proposal.state):
        rejections.append(
            f"Proposed state '{proposal.state}' is not reachable from '{state_machine.current_state}'"
        )

    if proposal.tool_call is not None:
        allowed = state_machine.allowed_tools()
        if proposal.tool_call.name not in allowed:
            rejections.append(f"Tool '{proposal.tool_call.name}' is not allowed in state '{state_machine.current_state}'")

    if rejections:
        logger.warning("llm_output_rejected", extra={"rejections": rejections})
        outcome = _fallback_outcome(state_machine, "; ".join(rejections))
        outcome.rejections = rejections
        return outcome

    return ValidationOutcome(accepted=True, proposal=proposal, rejections=[], used_fallback=False)

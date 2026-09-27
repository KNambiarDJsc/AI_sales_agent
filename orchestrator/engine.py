"""Wires one conversational turn together: prompts -> LLM -> validator -> tool
execution -> state transition. This is the "Agent Orchestration" box in the
architecture diagram (Section 4B) — it never touches audio directly; the voice
session calls `run_turn()` with the customer's transcribed text and gets back what to
say next and whether to hang up.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from llm.base import LLMProvider
from orchestrator.context import ConversationContext
from orchestrator.prompts import build_messages
from orchestrator.schema import AGENT_RESPONSE_JSON_SCHEMA
from orchestrator.state_machine import StateMachine
from orchestrator.validator import validate_llm_response
from tools.registry import ToolContext, ToolRegistry, get_default_registry

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    speech: str
    end_call: bool
    new_state: str
    tool_result_summary: str | None = None
    used_fallback: bool = False


class ConversationEngine:
    def __init__(
        self,
        llm: LLMProvider,
        state_machine: StateMachine,
        context: ConversationContext,
        tool_registry: ToolRegistry | None = None,
    ):
        self._llm = llm
        self._state_machine = state_machine
        self._context = context
        self._tools = tool_registry or get_default_registry()

    async def run_turn(
        self,
        customer_text: str,
        *,
        session_factory,
        tenant_id: UUID,
        campaign_id: UUID,
        lead_id: UUID,
        conversation_id: UUID,
    ) -> TurnResult:
        if customer_text.strip():
            self._context.append_turn("customer", customer_text, self._state_machine.current_state)

        messages = build_messages(self._state_machine.script, self._context)
        proposal_raw = await self._llm.propose(messages, AGENT_RESPONSE_JSON_SCHEMA)
        outcome = validate_llm_response(proposal_raw.raw_text, self._state_machine)

        if outcome.used_fallback:
            self._state_machine.record_retry()
            if self._state_machine.retry_limit_exceeded():
                logger.warning("retry_limit_exceeded", extra={"state": self._state_machine.current_state})

        proposal = outcome.proposal
        self._context.merge_facts(proposal.extracted_facts)

        tool_summary: str | None = None
        end_call = proposal.end_call

        if proposal.tool_call is not None:
            async with session_factory() as session:
                ctx = ToolContext(
                    session=session,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    lead_id=lead_id,
                    conversation_id=conversation_id,
                    script_version=self._state_machine.script.version,
                    current_state=self._state_machine.current_state,
                    allowed_tools=frozenset(self._state_machine.allowed_tools()),
                )
                result = await self._tools.invoke(ctx, proposal.tool_call.name, proposal.tool_call.arguments)
                await session.commit()
                tool_summary = result.message
                if proposal.tool_call.name in ("end_call", "mark_dnc") and result.success:
                    end_call = True
                if not result.success:
                    logger.warning(
                        "tool_call_failed",
                        extra={"tool": proposal.tool_call.name, "message": result.message},
                    )

        self._state_machine.transition_to(proposal.state)
        self._context.append_turn("agent", proposal.speech, self._state_machine.current_state)

        return TurnResult(
            speech=proposal.speech,
            end_call=end_call,
            new_state=self._state_machine.current_state,
            tool_result_summary=tool_summary,
            used_fallback=outcome.used_fallback,
        )

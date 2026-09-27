"""Wires one conversational turn together: prompts -> LLM -> validator -> tool
execution -> state transition. This is the "Agent Orchestration" box in the
architecture diagram (Section 4B) — it never touches audio directly; the voice
session calls `run_turn()` with the customer's transcribed text and gets back what to
say next and whether to hang up.

Latency-critical design notes (see STATUS.md's "Latency & realtime" section for the
full rationale):

- `run_turn` takes an `on_speech_ready` callback and invokes it with the agent's
  speech text as soon as it's known — either right after the (non-streaming) LLM call
  validates, or, with `settings.enable_speculative_tts`, mid-stream before the rest of
  the JSON object (tool_call/extracted_facts) has finished arriving. The caller (voice
  session) starts the TTS/telephony playback task from that callback and does NOT wait
  for `run_turn` to return before audio starts going out.
- Tool execution happens AFTER the speech callback fires, so a tool's DB round trip
  never sits on the critical path to the customer hearing a response. It still
  completes, and is still awaited, before `run_turn` returns — `end_call` is only
  acted on by the caller once `run_turn` has returned, so a `mark_dnc`/`end_call` tool
  is never skipped or raced.
- A single already-open `AsyncSession` is passed in per turn (not a session-factory
  callable) — one connection-pool checkout per turn instead of one per tool call.
- The LLM call has a hard deadline (`settings.llm_timeout_seconds`). A timeout or any
  other transport failure never propagates out of `run_turn` and never leaves the
  call hanging — it degrades to the same safe fallback response the validator itself
  would produce for a malformed reply.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from database.models import Turn
from database.session import session_scope
from llm.base import LLMProvider
from orchestrator.context import ConversationContext
from orchestrator.prompts import build_messages
from orchestrator.schema import AGENT_RESPONSE_JSON_SCHEMA, AgentResponseProposal
from orchestrator.state_machine import StateMachine
from orchestrator.streaming import SpeculativeTurnExtractor
from orchestrator.validator import ValidationOutcome, build_fallback_outcome, validate_llm_response
from tools.registry import ToolContext, ToolRegistry, get_default_registry

logger = logging.getLogger(__name__)

OnSpeechReady = Callable[[str], Awaitable[None]]


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
        self._settings = get_settings()

    async def run_turn(
        self,
        customer_text: str,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        campaign_id: UUID,
        lead_id: UUID,
        conversation_id: UUID,
        on_speech_ready: OnSpeechReady | None = None,
    ) -> TurnResult:
        if customer_text.strip():
            self._context.append_turn("customer", customer_text, self._state_machine.current_state)

        messages = build_messages(self._state_machine.script, self._context)
        outcome = await self._propose_and_validate(messages, on_speech_ready)
        proposal = outcome.proposal
        self._context.merge_facts(proposal.extracted_facts)

        tool_summary: str | None = None
        end_call = proposal.end_call

        if proposal.tool_call is not None:
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
            tool_summary = result.message
            if proposal.tool_call.name in ("end_call", "mark_dnc") and result.success:
                end_call = True
            if not result.success:
                logger.warning("tool_call_failed", extra={"tool": proposal.tool_call.name, "message": result.message})

        previous_state = self._state_machine.current_state
        self._state_machine.transition_to(proposal.state)
        self._context.append_turn("agent", proposal.speech, self._state_machine.current_state)
        self._persist_turn_fire_and_forget(previous_state, proposal)

        return TurnResult(
            speech=proposal.speech,
            end_call=end_call,
            new_state=self._state_machine.current_state,
            tool_result_summary=tool_summary,
            used_fallback=outcome.used_fallback,
        )

    async def _propose_and_validate(
        self, messages, on_speech_ready: OnSpeechReady | None
    ) -> ValidationOutcome:
        if self._settings.enable_speculative_tts:
            outcome = await self._propose_and_validate_streaming(messages, on_speech_ready)
            if outcome is not None:
                return outcome
            # `None` means the streaming attempt failed before anything was spoken —
            # safe to fall through and retry via the plain non-streaming path below.
            # It is NEVER returned after `on_speech_ready` has already fired; see
            # `_propose_and_validate_streaming`'s docstring for why that distinction
            # is load-bearing (falling through after speech already played would
            # re-run the whole call and risk speaking a second, different response).

        try:
            proposal_raw = await asyncio.wait_for(
                self._llm.propose(messages, AGENT_RESPONSE_JSON_SCHEMA),
                timeout=self._settings.llm_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure (incl. timeout) degrades safely
            logger.exception("llm_call_failed")
            outcome = build_fallback_outcome(self._state_machine, f"LLM call failed: {exc}")
            if on_speech_ready is not None:
                await on_speech_ready(outcome.proposal.speech)
            return outcome

        outcome = validate_llm_response(proposal_raw.raw_text, self._state_machine)
        if on_speech_ready is not None:
            await on_speech_ready(outcome.proposal.speech)
        return outcome

    async def _propose_and_validate_streaming(
        self, messages, on_speech_ready: OnSpeechReady | None
    ) -> ValidationOutcome | None:
        """Returns None only when it is safe for the caller to retry from scratch via
        the plain non-streaming path — i.e. only when `on_speech_ready` was never
        called. In every other case, by the time this returns, `on_speech_ready` has
        already been called exactly once (if one was given) — the caller must not
        call it again, which is exactly why `_propose_and_validate` treats a non-None
        return here as final rather than falling through."""
        extractor = SpeculativeTurnExtractor(self._state_machine)
        chunks: list[str] = []
        speech_delivered = False

        async def _consume() -> None:
            nonlocal speech_delivered
            async for delta in self._llm.propose_stream(messages, AGENT_RESPONSE_JSON_SCHEMA):
                chunks.append(delta)
                extractor.feed(delta)
                if not speech_delivered and extractor.speech_ready:
                    speech_delivered = True
                    if on_speech_ready is not None:
                        await on_speech_ready(extractor.speech)

        stream_error: BaseException | None = None
        try:
            await asyncio.wait_for(_consume(), timeout=self._settings.llm_timeout_seconds)
        except asyncio.TimeoutError as exc:
            logger.warning("llm_stream_timeout", extra={"speech_already_delivered": speech_delivered})
            stream_error = exc
        except Exception as exc:  # noqa: BLE001 - any transport failure degrades safely below
            logger.exception("speculative_tts_stream_error")
            stream_error = exc

        if stream_error is not None:
            if not speech_delivered:
                return None  # nothing spoken yet — safe for the caller to retry via the plain path
            # Speech already played under a state we'd already confirmed reachable —
            # stay consistent with what the customer heard by transitioning there
            # rather than silently discarding it. We just never saw the
            # tool_call/extracted_facts that would have come later in the stream.
            proposal = AgentResponseProposal(
                state=extractor.state or self._state_machine.current_state,
                speech=extractor.speech or "",
                intent="unknown",
                extracted_facts={},
                tool_call=None,
                end_call=False,
            )
            return ValidationOutcome(
                accepted=False, proposal=proposal, rejections=[f"stream failed after speech: {stream_error}"], used_fallback=True
            )

        raw_text = "".join(chunks)
        outcome = validate_llm_response(raw_text, self._state_machine)

        if speech_delivered and outcome.proposal.speech != extractor.speech:
            # Should be structurally impossible (the extractor only reads substrings
            # of the same raw_text the validator re-parses) — if it ever happens,
            # something upstream changed shape. Log loudly; the customer already
            # heard `extractor.speech`, so we do NOT re-speak or contradict it here.
            logger.error(
                "speculative_tts_mismatch",
                extra={"spoken": extractor.speech, "validated": outcome.proposal.speech},
            )

        if not speech_delivered and on_speech_ready is not None:
            await on_speech_ready(outcome.proposal.speech)

        return outcome

    def current_fallback_response(self) -> str:
        """Exposed for the voice session's silence/no-transcript re-prompt path
        (`voice/session/session.py`) — a deterministic, zero-latency line from the
        active script's config, no LLM round trip needed just to break dead air."""
        return self._state_machine.fallback_response()

    def _persist_turn_fire_and_forget(self, previous_state: str, proposal) -> None:
        """Fire-and-forget on the engine's OWN short-lived session (never the
        turn's/tool's transactional `session` param, whose lifetime the caller owns
        and closes right after `run_turn` returns — sharing it here would race a
        background write against that close). Persistence must never sit on the
        critical path to the next audio frame; errors are logged, not raised — losing
        a Turn row is not a reason to drop or delay a live call."""
        conversation_id = UUID(self._context.conversation_id)
        turn_index = len(self._context.history) - 1  # this agent turn was just appended

        async def _write() -> None:
            try:
                async with session_scope() as bg_session:
                    bg_session.add(
                        Turn(
                            conversation_id=conversation_id,
                            turn_index=turn_index,
                            speaker="agent",
                            state=previous_state,
                            intent=proposal.intent,
                            raw_llm_output=proposal.model_dump(),
                        )
                    )
            except Exception:  # noqa: BLE001
                logger.exception("turn_persistence_failed")

        asyncio.create_task(_write())

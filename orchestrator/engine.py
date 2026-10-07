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
from database.models import Turn, TranscriptSegment
from database.session import session_scope
from llm.base import LLMProvider
from orchestrator.context import ConversationContext
from orchestrator.prompts import build_messages
from orchestrator.schema import AgentResponseProposal, ToolCallProposal, build_agent_response_schema
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
        customer_turn_index: int | None = None
        if customer_text.strip():
            self._context.append_turn("customer", customer_text, self._state_machine.current_state)
            customer_turn_index = len(self._context.history) - 1

        if customer_turn_index is not None and self._state_machine.script.is_dnc_request(customer_text):
            return await self._handle_dnc_request(
                session=session, tenant_id=tenant_id, campaign_id=campaign_id, lead_id=lead_id,
                conversation_id=conversation_id, customer_text=customer_text,
                customer_turn_index=customer_turn_index, on_speech_ready=on_speech_ready,
            )

        customer_spoke = customer_turn_index is not None
        messages = build_messages(self._state_machine.script, self._context)
        outcome = await self._propose_and_validate(messages, on_speech_ready, self._turn_schema(customer_spoke))
        proposal = outcome.proposal
        if not customer_spoke and (
            proposal.state != self._state_machine.current_state or proposal.tool_call is not None or proposal.end_call
        ):
            # Nothing to react to (opening line, re-prompt after silence) — so nothing
            # may change. The schema already pins `state`; this is the backstop for
            # anything that bypasses it. Caught with a small local model proposing
            # DO_NOT_CALL + mark_dnc on the greeting, before the customer said a word.
            logger.warning("opening_turn_change_refused", extra={"proposed_state": proposal.state})
            proposal = proposal.model_copy(
                update={"state": self._state_machine.current_state, "tool_call": None, "end_call": False}
            )
        proposes_dnc = proposal.state == "DO_NOT_CALL" or (
            proposal.tool_call is not None and proposal.tool_call.name == "mark_dnc"
        )
        if proposes_dnc and getattr(self._llm, "backend", "openai") == "local":
            # A real DNC request in the customer's words never gets here — the
            # configured-phrase backstop handles it before the LLM runs. So a local
            # model proposing DNC now is guessing, and small local models guessed
            # wrong: they put a customer who had just said "please have your sales
            # team call me" (and one who only confirmed their name) on do-not-call.
            # Suppressing a lead is irreversible in practice; refuse the guess.
            logger.warning("dnc_from_local_model_refused", extra={"proposed_state": proposal.state})
            update: dict = {"end_call": False}
            if proposal.state == "DO_NOT_CALL":
                update["state"] = self._state_machine.current_state
            if proposal.tool_call is not None and proposal.tool_call.name == "mark_dnc":
                update["tool_call"] = None
            proposal = proposal.model_copy(update=update)
        self._context.merge_facts(proposal.extracted_facts)

        tool_summary: str | None = None
        end_call = proposal.end_call
        dnc_recorded = False

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
                dnc_recorded = proposal.tool_call.name == "mark_dnc"
            if not result.success:
                # Not "message": that key is reserved on LogRecord, and logging raises
                # KeyError for it — which turned every failed tool call into a crashed turn.
                logger.warning("tool_call_failed", extra={"tool": proposal.tool_call.name, "tool_message": result.message})

        # The LLM proposes hanging up; the application decides. A call only ends from
        # a closing state (script config: END, DO_NOT_CALL, or a state whose only exit
        # is END) — or when DNC was just recorded. Caught with small local models,
        # which called the end_call tool on the opening greeting and hung up on the
        # customer mid-pitch; the state machine said the conversation wasn't over.
        if end_call and not dnc_recorded and not self._state_machine.script.is_closing_state(proposal.state):
            logger.warning("end_call_refused_not_closing_state", extra={"proposed_state": proposal.state})
            end_call = False

        previous_state = self._state_machine.current_state
        self._state_machine.transition_to(proposal.state)
        # The prompt is built from the context's state (orchestrator/prompts.py), so it
        # has to follow the state machine — otherwise every turn's prompt describes
        # INTRO's objective/questions/tools for the whole call.
        self._context.current_state = self._state_machine.current_state
        self._context.append_turn("agent", proposal.speech, self._state_machine.current_state)
        agent_turn_index = len(self._context.history) - 1
        self._persist_turns_fire_and_forget(
            customer_text=customer_text if customer_turn_index is not None else None,
            customer_turn_index=customer_turn_index,
            agent_turn_index=agent_turn_index,
            previous_state=previous_state,
            proposal=proposal,
        )

        return TurnResult(
            speech=proposal.speech,
            end_call=end_call,
            new_state=self._state_machine.current_state,
            tool_result_summary=tool_summary,
            used_fallback=outcome.used_fallback,
        )

    async def _handle_dnc_request(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        campaign_id: UUID,
        lead_id: UUID,
        conversation_id: UUID,
        customer_text: str,
        customer_turn_index: int,
        on_speech_ready: OnSpeechReady | None,
    ) -> TurnResult:
        """DNC backstop (CLAUDE.md rule 3): the customer used a configured do-not-call
        phrase, so the application handles the turn itself instead of trusting the LLM
        to call mark_dnc (small local models were seen not to). Speaks DO_NOT_CALL's
        configured line first — same speech-before-tools order as every turn — then
        records the suppression and ends the call."""
        script = self._state_machine.script
        speech = script.fallback_for("DO_NOT_CALL", self._context.lead_fields)
        if on_speech_ready is not None:
            await on_speech_ready(speech)

        ctx = ToolContext(
            session=session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            conversation_id=conversation_id,
            script_version=script.version,
            current_state="DO_NOT_CALL",
            allowed_tools=frozenset(script.allowed_tools("DO_NOT_CALL")),
        )
        result = await self._tools.invoke(ctx, "mark_dnc", {"reason": "customer_request"})
        if not result.success:
            # Still end the call (the customer asked us to stop), but this must be seen.
            logger.error("dnc_backstop_mark_dnc_failed", extra={"tool_message": result.message})
        logger.info("dnc_backstop_triggered", extra={"conversation_id": str(conversation_id)})

        proposal = AgentResponseProposal(
            state="DO_NOT_CALL",
            speech=speech,
            intent="do_not_call_request",
            extracted_facts={"do_not_call": True},
            tool_call=ToolCallProposal(name="mark_dnc", arguments={"reason": "customer_request"}),
            end_call=True,
        )
        self._context.merge_facts(proposal.extracted_facts)
        previous_state = self._state_machine.current_state
        self._state_machine.transition_to("DO_NOT_CALL")
        self._context.current_state = self._state_machine.current_state
        self._context.append_turn("agent", speech, self._state_machine.current_state)
        self._persist_turns_fire_and_forget(
            customer_text=customer_text,
            customer_turn_index=customer_turn_index,
            agent_turn_index=len(self._context.history) - 1,
            previous_state=previous_state,
            proposal=proposal,
        )
        return TurnResult(speech=speech, end_call=True, new_state="DO_NOT_CALL", tool_result_summary=result.message)

    def _turn_schema(self, customer_spoke: bool = True) -> dict:
        """The structured-output schema for this turn. Before the customer has said
        anything, the only valid state is the current one and the only intent is
        "no_customer_message_yet" — there is nothing to classify or react to."""
        if not customer_spoke:
            return build_agent_response_schema([self._state_machine.current_state], ["no_customer_message_yet"])
        return build_agent_response_schema(
            sorted(self._state_machine.allowed_next_states()), self._state_machine.intent_options()
        )

    async def _propose_and_validate(
        self, messages, on_speech_ready: OnSpeechReady | None, schema: dict | None = None
    ) -> ValidationOutcome:
        schema = schema or self._turn_schema()
        # One deadline for the whole turn, shared by the streaming attempt and the
        # plain-path retry. They used to get llm_timeout_seconds each, so a stream that
        # timed out was retried for the full timeout again — ~17 s of dead air
        # before the fallback line, seen live.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.llm_timeout_seconds
        if self._settings.enable_speculative_tts:
            outcome = await self._propose_and_validate_streaming(messages, on_speech_ready, schema)
            if outcome is not None:
                return outcome
            # `None` means the streaming attempt failed before anything was spoken —
            # safe to fall through and retry via the plain non-streaming path below.
            # It is NEVER returned after `on_speech_ready` has already fired; see
            # `_propose_and_validate_streaming`'s docstring for why that distinction
            # is load-bearing (falling through after speech already played would
            # re-run the whole call and risk speaking a second, different response).

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("LLM deadline already spent by the streaming attempt")
            proposal_raw = await asyncio.wait_for(
                self._llm.propose(messages, schema),
                timeout=remaining,
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure (incl. timeout) degrades safely
            logger.exception("llm_call_failed")
            outcome = build_fallback_outcome(self._state_machine, f"LLM call failed: {exc}", self._context.lead_fields)
            if on_speech_ready is not None:
                await on_speech_ready(outcome.proposal.speech)
            return outcome

        outcome = validate_llm_response(proposal_raw.raw_text, self._state_machine, self._context.lead_fields)
        if on_speech_ready is not None:
            await on_speech_ready(outcome.proposal.speech)
        return outcome

    async def _propose_and_validate_streaming(
        self, messages, on_speech_ready: OnSpeechReady | None, schema: dict | None = None
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
            async for delta in self._llm.propose_stream(messages, schema or self._turn_schema()):
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
        outcome = validate_llm_response(raw_text, self._state_machine, self._context.lead_fields)

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

    @property
    def current_state(self) -> str:
        return self._state_machine.current_state

    def current_fallback_response(self) -> str:
        """Exposed for the voice session's silence/no-transcript re-prompt path
        (`voice/session/session.py`) — a deterministic, zero-latency line from the
        active script's config, no LLM round trip needed just to break dead air."""
        return self._state_machine.fallback_response(self._context.lead_fields)

    def _persist_turns_fire_and_forget(
        self,
        *,
        customer_text: str | None,
        customer_turn_index: int | None,
        agent_turn_index: int,
        previous_state: str,
        proposal,
    ) -> None:
        """Fire-and-forget on the engine's OWN short-lived session (never the
        turn's/tool's transactional `session` param, whose lifetime the caller owns
        and closes right after `run_turn` returns — sharing it here would race a
        background write against that close). Persistence must never sit on the
        critical path to the next audio frame; errors are logged, not raised — losing
        a Turn row is not a reason to drop or delay a live call.

        Persists BOTH sides of the turn: the customer's transcribed text (if any this
        turn) and the agent's response, each as a Turn + TranscriptSegment row. Both
        rows use `previous_state` — the state active while this exchange happened, not
        the state the agent's proposal transitions *to* (that's reflected in the next
        turn's own `state` instead, and in Conversation.current_state)."""
        conversation_id = UUID(self._context.conversation_id)

        async def _write() -> None:
            try:
                async with session_scope() as bg_session:
                    if customer_text is not None and customer_turn_index is not None:
                        customer_turn = Turn(
                            conversation_id=conversation_id,
                            turn_index=customer_turn_index,
                            speaker="customer",
                            state=previous_state,
                            intent=None,
                            raw_llm_output=None,
                        )
                        bg_session.add(customer_turn)
                        await bg_session.flush()  # need customer_turn.id for the segment FK
                        bg_session.add(
                            TranscriptSegment(
                                conversation_id=conversation_id,
                                turn_id=customer_turn.id,
                                speaker="customer",
                                text=customer_text,
                                is_final=True,
                            )
                        )

                    agent_turn = Turn(
                        conversation_id=conversation_id,
                        turn_index=agent_turn_index,
                        speaker="agent",
                        state=previous_state,
                        # The column is VARCHAR(100); a model that writes a sentence
                        # here (seen with small local models) used to make the whole
                        # turn's transcript write fail.
                        intent=(proposal.intent or "")[:100] or None,
                        raw_llm_output=proposal.model_dump(),
                    )
                    bg_session.add(agent_turn)
                    await bg_session.flush()
                    bg_session.add(
                        TranscriptSegment(
                            conversation_id=conversation_id,
                            turn_id=agent_turn.id,
                            speaker="agent",
                            text=proposal.speech,
                            is_final=True,
                        )
                    )
            except Exception:  # noqa: BLE001
                logger.exception("turn_persistence_failed")

        asyncio.create_task(_write())

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
from collections.abc import AsyncIterator, Awaitable, Callable
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
from orchestrator.streaming import PhraseSplitter, SpeculativeTurnExtractor
from orchestrator.validator import ValidationOutcome, build_fallback_outcome, validate_llm_response
from tools.registry import ToolContext, ToolRegistry, get_default_registry

logger = logging.getLogger(__name__)

OnSpeechReady = Callable[[str], Awaitable[None]]
OnSpeechStream = Callable[[AsyncIterator[str]], Awaitable[None]]


class _SpeechDelivery:
    """Hands one turn's speech to the caller exactly once (CLAUDE.md rule 10).

    With an `on_speech_stream` callback, speech is a stream of phrases: the callback
    fires once, with the first phrase, and later phrases are pushed into the same
    iterator as the LLM writes them; the iterator ends when the reply's speech is
    complete (or the LLM stream dies — then it ends at what was already said).
    Otherwise `on_speech_ready` fires once with the whole text. Every path that speaks
    in the engine goes through `whole()` or `piece()`, so double speech is a
    RuntimeError here rather than two voices on a call."""

    def __init__(self, on_speech_ready: OnSpeechReady | None, on_speech_stream: OnSpeechStream | None) -> None:
        self._ready = on_speech_ready
        self._stream = on_speech_stream
        self._queue: asyncio.Queue[str | None] | None = None
        self.started = False
        self.closed = False
        self._spoken: list[str] = []

    @property
    def streaming(self) -> bool:
        return self._stream is not None

    @property
    def spoken_text(self) -> str:
        return " ".join(self._spoken)

    async def _open_stream(self) -> None:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._queue = queue

        async def phrases() -> AsyncIterator[str]:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        self.started = True
        await self._stream(phrases())

    async def whole(self, text: str) -> None:
        if self.started:
            raise RuntimeError("speech was already delivered for this turn")
        if self._stream is not None:
            await self._open_stream()
            self._spoken.append(text)
            self._queue.put_nowait(text)
            self.close()
            return
        self.started = True
        self._spoken.append(text)
        if self._ready is not None:
            await self._ready(text)

    async def piece(self, text: str) -> None:
        if self.closed:
            raise RuntimeError("speech stream already ended for this turn")
        if not self.started:
            await self._open_stream()
        self._spoken.append(text)
        self._queue.put_nowait(text)

    def close(self) -> None:
        if self._queue is not None and not self.closed:
            self.closed = True
            self._queue.put_nowait(None)


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
        on_speech_stream: OnSpeechStream | None = None,
    ) -> TurnResult:
        """Speech reaches the caller exactly once per turn (CLAUDE.md rule 10), in one
        of two forms: `on_speech_stream(phrases)` — an async iterator of phrases,
        handed over as soon as the first phrase of the reply is written, so speech can
        start while the LLM is still writing — or, if only `on_speech_ready` is given,
        the whole text once it is complete. Either callback must only *start* playback
        and return; it must not wait for audio."""
        customer_turn_index: int | None = None
        if customer_text.strip():
            self._context.append_turn("customer", customer_text, self._state_machine.current_state)
            customer_turn_index = len(self._context.history) - 1

        if customer_turn_index is not None and self._state_machine.script.is_dnc_request(customer_text):
            return await self._handle_dnc_request(
                session=session, tenant_id=tenant_id, campaign_id=campaign_id, lead_id=lead_id,
                conversation_id=conversation_id, customer_text=customer_text,
                customer_turn_index=customer_turn_index,
                delivery=_SpeechDelivery(on_speech_ready, on_speech_stream),
            )

        customer_spoke = customer_turn_index is not None
        if not customer_spoke and not self._context.history:
            opening = self._state_machine.script.opening_line_for(
                self._state_machine.current_state, self._context.lead_fields
            )
            if opening:
                return await self._speak_configured_opening(
                    opening, delivery=_SpeechDelivery(on_speech_ready, on_speech_stream)
                )

        messages = build_messages(self._state_machine.script, self._context)
        outcome = await self._propose_and_validate(
            messages, on_speech_ready, self._turn_schema(customer_spoke), on_speech_stream
        )
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
        if self._is_local_dnc_guess(proposal):
            # Normally refused before anything is spoken (`_refuse_local_dnc_guess`:
            # the fallback line is said instead). This backstop only runs when the
            # reply's speech was already streamed under an ordinary state and the
            # mark_dnc tool call came after it — drop the tool, keep the call going.
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
        delivery: "_SpeechDelivery",
    ) -> TurnResult:
        """DNC backstop (CLAUDE.md rule 3): the customer used a configured do-not-call
        phrase, so the application handles the turn itself instead of trusting the LLM
        to call mark_dnc (small local models were seen not to). Speaks DO_NOT_CALL's
        configured line first — same speech-before-tools order as every turn — then
        records the suppression and ends the call."""
        script = self._state_machine.script
        speech = script.fallback_for("DO_NOT_CALL", self._context.lead_fields)
        await delivery.whole(speech)

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

    async def _speak_configured_opening(self, speech: str, *, delivery: "_SpeechDelivery") -> TurnResult:
        """The script's fixed first line (`opening_line` in config), said without an
        LLM round trip: there's nothing to react to yet, and the LLM only paraphrased
        the INTRO question — while the customer, who just picked up, heard silence
        (3–12 s with the local model, ~1 s with OpenAI). No state change, no tools."""
        await delivery.whole(speech)
        state = self._state_machine.current_state
        self._context.append_turn("agent", speech, state)
        self._persist_turns_fire_and_forget(
            customer_text=None,
            customer_turn_index=None,
            agent_turn_index=len(self._context.history) - 1,
            previous_state=state,
            proposal=AgentResponseProposal(
                state=state, speech=speech, intent="configured_opening_line", extracted_facts={},
                tool_call=None, end_call=False,
            ),
        )
        return TurnResult(speech=speech, end_call=False, new_state=state)

    def _is_local_dnc_guess(self, proposal: AgentResponseProposal) -> bool:
        """A local model proposing do-not-call. A real DNC request in the customer's
        words never reaches the LLM — the configured-phrase backstop handles it first —
        so a local model proposing DNC is guessing, and small local models guessed
        wrong: they put a customer who had just said "please have your sales team call
        me" (and one who only confirmed their name) on do-not-call. Suppressing a lead
        is irreversible in practice, so the guess is refused (OpenAI models' DNC
        judgement is still honoured)."""
        if getattr(self._llm, "backend", "openai") != "local":
            return False
        return proposal.state == "DO_NOT_CALL" or (
            proposal.tool_call is not None and proposal.tool_call.name == "mark_dnc"
        )

    def _may_speak_early(self, state: str) -> bool:
        """Whether a reply heading to `state` may start speaking before the whole
        reply is validated. Not for a local model's DO_NOT_CALL: that proposal will be
        refused, and its speech ("I'll add your number to our do-not-call list") was
        heard by a customer who had asked for a sales call."""
        return not (state == "DO_NOT_CALL" and getattr(self._llm, "backend", "openai") == "local")

    def _refuse_local_dnc_guess(self, outcome: ValidationOutcome) -> ValidationOutcome:
        """Rule 2: a refused proposal falls back to the current state and its
        configured fallback line — its own speech is never said."""
        if not self._is_local_dnc_guess(outcome.proposal):
            return outcome
        logger.warning("dnc_from_local_model_refused", extra={"proposed_state": outcome.proposal.state})
        return build_fallback_outcome(
            self._state_machine, "do-not-call guessed by the local model", self._context.lead_fields
        )

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
        self,
        messages,
        on_speech_ready: OnSpeechReady | None = None,
        schema: dict | None = None,
        on_speech_stream: OnSpeechStream | None = None,
    ) -> ValidationOutcome:
        schema = schema or self._turn_schema()
        delivery = _SpeechDelivery(on_speech_ready, on_speech_stream)
        # One deadline for the whole turn, shared by the streaming attempt and the
        # plain-path retry. They used to get llm_timeout_seconds each, so a stream that
        # timed out was retried for the full timeout again — ~17 s of dead air
        # before the fallback line, seen live.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.llm_timeout_seconds
        if self._settings.enable_speculative_tts:
            outcome = await self._propose_and_validate_streaming(messages, delivery, schema)
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
            await delivery.whole(outcome.proposal.speech)
            return outcome

        outcome = validate_llm_response(proposal_raw.raw_text, self._state_machine, self._context.lead_fields)
        outcome = self._refuse_local_dnc_guess(outcome)
        await delivery.whole(outcome.proposal.speech)
        return outcome

    async def _propose_and_validate_streaming(
        self, messages, delivery: "_SpeechDelivery", schema: dict | None = None
    ) -> ValidationOutcome | None:
        """Returns None only when it is safe for the caller to retry from scratch via
        the plain non-streaming path — i.e. only when no speech was handed over yet.
        In every other case, by the time this returns, speech has been delivered
        exactly once — the caller must not deliver it again, which is exactly why
        `_propose_and_validate` treats a non-None return here as final rather than
        falling through."""
        extractor = SpeculativeTurnExtractor(self._state_machine, may_speak=self._may_speak_early)
        splitter = PhraseSplitter()
        chunks: list[str] = []

        async def _consume() -> None:
            async for delta in self._llm.propose_stream(messages, schema or self._turn_schema()):
                chunks.append(delta)
                extractor.feed(delta)
                if delivery.streaming:
                    # Hand each phrase over the moment it is complete — only ever
                    # after `state` passed the transition check (the extractor gates
                    # `partial_speech` on it), exactly like the whole-text path.
                    if extractor.partial_speech is not None and not delivery.closed:
                        for phrase in splitter.feed(extractor.partial_speech, final=extractor.speech_ready):
                            await delivery.piece(phrase)
                        if extractor.speech_ready and delivery.started:
                            delivery.close()
                elif not delivery.started and extractor.speech_ready:
                    await delivery.whole(extractor.speech)

        stream_error: BaseException | None = None
        try:
            await asyncio.wait_for(_consume(), timeout=self._settings.llm_timeout_seconds)
        except asyncio.TimeoutError as exc:
            logger.warning("llm_stream_timeout", extra={"speech_already_delivered": delivery.started})
            stream_error = exc
        except Exception as exc:  # noqa: BLE001 - any transport failure degrades safely below
            logger.exception("speculative_tts_stream_error")
            stream_error = exc

        speech_delivered = delivery.started
        if stream_error is not None:
            if not speech_delivered:
                return None  # nothing spoken yet — safe for the caller to retry via the plain path
            delivery.close()  # end the phrase stream at whatever the customer has heard
            # Speech already played under a state we'd already confirmed reachable —
            # stay consistent with what the customer heard by transitioning there
            # rather than silently discarding it. We just never saw the
            # tool_call/extracted_facts that would have come later in the stream.
            proposal = AgentResponseProposal(
                state=extractor.state or self._state_machine.current_state,
                speech=delivery.spoken_text,
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

        if speech_delivered:
            delivery.close()
            if " ".join(outcome.proposal.speech.split()) != " ".join(delivery.spoken_text.split()):
                # Should be structurally impossible (the extractor only reads
                # substrings of the same raw_text the validator re-parses) — if it ever
                # happens, something upstream changed shape. Log loudly; the customer
                # already heard it, so we do NOT re-speak or contradict it here.
                logger.error(
                    "speculative_tts_mismatch",
                    extra={"spoken": delivery.spoken_text, "validated": outcome.proposal.speech},
                )
        else:
            outcome = self._refuse_local_dnc_guess(outcome)
            await delivery.whole(outcome.proposal.speech)

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

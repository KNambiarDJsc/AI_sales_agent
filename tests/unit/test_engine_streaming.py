import json

import pytest

from config.settings import get_settings
from llm.base import LLMProposal, LLMProvider
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.state_machine import ScriptConfig, StateConfig, StateMachine

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_speculative_flag():
    """`enable_speculative_tts` now defaults to True (verified against a live key —
    see STATUS.md), but the non-streaming tests below specifically want the
    non-streaming path in isolation. Force it False by default for every test in this
    file and restore whatever it actually was before, rather than each test hardcoding
    a restore value that would go stale the next time the default changes."""
    settings = get_settings()
    original = settings.enable_speculative_tts
    settings.enable_speculative_tts = False
    yield
    settings.enable_speculative_tts = original


def _script() -> ScriptConfig:
    return ScriptConfig(
        script_id="test",
        version=1,
        states={
            "INTRO": StateConfig(objective="greet", transitions_on={"granted": "DISCOVERY"}),
            "DISCOVERY": StateConfig(objective="ask", transitions_on={}),
        },
    )


def _context() -> ConversationContext:
    return ConversationContext(
        conversation_id="11111111-1111-1111-1111-111111111111",
        campaign_id="c",
        lead_id="l",
        script_id="test",
        script_version=1,
        current_state="INTRO",
    )


class FakeNonStreamingLLM(LLMProvider):
    def __init__(self, raw_text: str | None = None, raise_error: Exception | None = None):
        self._raw_text = raw_text
        self._raise_error = raise_error

    async def propose(self, messages, json_schema, schema_name="agent_response") -> LLMProposal:
        if self._raise_error is not None:
            raise self._raise_error
        return LLMProposal(raw_text=self._raw_text, model="fake")

    async def propose_stream(self, messages, json_schema, schema_name="agent_response"):
        raise NotImplementedError
        yield  # pragma: no cover - make this an async generator


class FakeStreamingLLM(LLMProvider):
    """Yields `chunks` one at a time; if `error_after` is set, raises that exception
    once that many chunks have been yielded (simulating a mid-stream failure)."""

    def __init__(self, chunks: list[str], error_after: int | None = None, error: Exception | None = None):
        self._chunks = chunks
        self._error_after = error_after
        self._error = error or RuntimeError("simulated stream failure")

    async def propose(self, messages, json_schema, schema_name="agent_response") -> LLMProposal:
        return LLMProposal(raw_text="".join(self._chunks), model="fake")

    async def propose_stream(self, messages, json_schema, schema_name="agent_response"):
        for i, chunk in enumerate(self._chunks):
            if self._error_after is not None and i == self._error_after:
                raise self._error
            yield chunk


def _valid_payload(state="DISCOVERY", speech="Hi there", tool_call=None) -> str:
    return json.dumps(
        {
            "state": state,
            "speech": speech,
            "intent": "x",
            "extracted_facts": {},
            "tool_call": tool_call,
            "end_call": False,
        }
    )


async def test_non_streaming_calls_speech_callback_exactly_once():
    engine = ConversationEngine(FakeNonStreamingLLM(_valid_payload()), StateMachine(_script(), "INTRO"), _context())
    calls = []

    async def on_speech_ready(speech: str) -> None:
        calls.append(speech)

    outcome = await engine._propose_and_validate([], on_speech_ready)
    assert outcome.accepted
    assert calls == ["Hi there"]


async def test_non_streaming_llm_failure_falls_back_and_still_calls_callback_once():
    engine = ConversationEngine(
        FakeNonStreamingLLM(raise_error=TimeoutError("boom")), StateMachine(_script(), "INTRO"), _context()
    )
    calls = []

    async def on_speech_ready(speech: str) -> None:
        calls.append(speech)

    outcome = await engine._propose_and_validate([], on_speech_ready)
    assert outcome.used_fallback
    assert len(calls) == 1
    assert calls[0] == outcome.proposal.speech


async def test_speculative_streaming_delivers_speech_before_stream_completes():
    get_settings().enable_speculative_tts = True
    payload = _valid_payload(state="DISCOVERY", speech="Are you interested in selling online?")
    # Chunk it up character-by-character to simulate real token deltas.
    chunks = [payload[i : i + 4] for i in range(0, len(payload), 4)]
    engine = ConversationEngine(FakeStreamingLLM(chunks), StateMachine(_script(), "INTRO"), _context())
    calls = []

    async def on_speech_ready(speech: str) -> None:
        calls.append(speech)

    outcome = await engine._propose_and_validate([], on_speech_ready)
    assert outcome.accepted
    assert calls == ["Are you interested in selling online?"]


async def test_speculative_streaming_never_double_speaks_after_mid_stream_failure():
    get_settings().enable_speculative_tts = True
    payload = _valid_payload(state="DISCOVERY", speech="Are you interested in selling online?")
    chunks = [payload[i : i + 4] for i in range(0, len(payload), 4)]
    # Fail partway through, AFTER "speech" has certainly already closed (the
    # state+speech fields are near the start of the object).
    engine = ConversationEngine(
        FakeStreamingLLM(chunks, error_after=len(chunks) - 2), StateMachine(_script(), "INTRO"), _context()
    )
    calls = []

    async def on_speech_ready(speech: str) -> None:
        calls.append(speech)

    outcome = await engine._propose_and_validate([], on_speech_ready)
    assert len(calls) == 1  # never called twice, regardless of the later failure
    assert outcome.proposal.speech == calls[0]
    assert outcome.proposal.state == "DISCOVERY"  # stayed consistent with what was spoken


async def test_speculative_streaming_falls_back_to_plain_path_when_nothing_was_spoken_yet():
    get_settings().enable_speculative_tts = True
    # Fails immediately, before any field (let alone speech) has been extracted.
    engine = ConversationEngine(FakeStreamingLLM(["not"], error_after=0), StateMachine(_script(), "INTRO"), _context())
    calls = []

    async def on_speech_ready(speech: str) -> None:
        calls.append(speech)

    outcome = await engine._propose_and_validate([], on_speech_ready)
    # Fell through to engine._llm.propose() (the non-streaming path), which for
    # FakeStreamingLLM returns the full joined chunks — here just "not", which
    # will fail JSON parsing and produce a safe fallback.
    assert len(calls) == 1
    assert outcome.used_fallback


async def test_speculative_streaming_never_speaks_early_for_a_disallowed_state():
    get_settings().enable_speculative_tts = True
    payload = _valid_payload(state="NONEXISTENT_STATE", speech="This should not play early.")
    chunks = [payload[i : i + 4] for i in range(0, len(payload), 4)]
    engine = ConversationEngine(FakeStreamingLLM(chunks), StateMachine(_script(), "INTRO"), _context())
    calls = []

    async def on_speech_ready(speech: str) -> None:
        calls.append(speech)

    outcome = await engine._propose_and_validate([], on_speech_ready)
    assert len(calls) == 1
    assert outcome.used_fallback
    assert calls[0] != "This should not play early."  # got the fallback line instead

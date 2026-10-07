"""Engine regressions found during live demo testing (no DB, no network)."""
import asyncio
import json
import uuid

import pytest

from orchestrator.engine import ConversationEngine
from orchestrator.state_machine import ScriptConfig, StateConfig, StateMachine
from tests.unit.test_engine_persistence import FakeNonStreamingLLM, _context, _patch_session_scope
from tests.unit.test_engine_streaming import _valid_payload
from tools.registry import ToolRegistry, ToolResult, ToolSpec

pytestmark = pytest.mark.asyncio


def _script() -> ScriptConfig:
    return ScriptConfig(
        script_id="test",
        version=1,
        states={
            "INTRO": StateConfig(objective="greet", allowed_tools=["flaky"], transitions_on={"granted": "DISCOVERY"}),
            "DISCOVERY": StateConfig(objective="ask", transitions_on={}),
        },
    )


def _registry_with_failing_tool() -> ToolRegistry:
    from pydantic import BaseModel

    class NoArgs(BaseModel):
        pass

    async def handler(ctx, args):
        return ToolResult(success=False, message="downstream said no")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="flaky", input_model=NoArgs, handler=handler))
    return registry


async def _run(engine, text="hello"):
    spoken = []

    async def on_speech(t):
        spoken.append(t)

    result = await engine.run_turn(
        text, session=None, tenant_id=uuid.uuid4(), campaign_id=uuid.uuid4(), lead_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(), on_speech_ready=on_speech,
    )
    return result, spoken


async def test_failed_tool_call_does_not_crash_the_turn(monkeypatch):
    # Logging a failed tool with extra={"message": ...} raised KeyError (reserved
    # LogRecord key), turning every failed tool call into a crashed turn — on a real
    # call that tore down the media loop. Speech had already been sent by then.
    _patch_session_scope(monkeypatch, [])
    engine = ConversationEngine(
        FakeNonStreamingLLM(_valid_payload(state="INTRO", speech="One moment.", tool_call={"name": "flaky", "arguments": "{}"})),
        StateMachine(_script(), "INTRO"),
        _context(),
        tool_registry=_registry_with_failing_tool(),
    )
    result, spoken = await _run(engine)
    await asyncio.sleep(0.05)
    assert spoken == ["One moment."]
    assert result.tool_result_summary == "downstream said no"
    assert result.end_call is False


async def test_prompt_context_follows_the_state_machine(monkeypatch):
    # The prompt is built from context.current_state; it used to stay "INTRO" for the
    # whole call while the state machine moved on, so every turn's prompt described
    # INTRO's objective, questions and tools.
    _patch_session_scope(monkeypatch, [])
    context = _context()
    engine = ConversationEngine(
        FakeNonStreamingLLM(_valid_payload(state="DISCOVERY", speech="Great.")),
        StateMachine(_script(), "INTRO"),
        context,
        tool_registry=ToolRegistry(),
    )
    await _run(engine)
    await asyncio.sleep(0.05)
    assert engine.current_state == "DISCOVERY"
    assert context.current_state == "DISCOVERY"


async def test_end_call_is_refused_outside_a_closing_state(monkeypatch):
    # Caught with a small local model: it called end_call on the opening greeting
    # (state INTRO) and the engine hung up on the customer.
    _patch_session_scope(monkeypatch, [])
    payload = json.dumps({"state": "INTRO", "speech": "Hello!", "intent": "x", "extracted_facts": {}, "tool_call": None,
                          "end_call": True})
    engine = ConversationEngine(FakeNonStreamingLLM(payload), StateMachine(_script(), "INTRO"), _context(),
                                tool_registry=ToolRegistry())
    result, _ = await _run(engine, "")
    await asyncio.sleep(0.05)
    assert result.end_call is False


async def test_end_call_is_honoured_when_moving_to_end(monkeypatch):
    _patch_session_scope(monkeypatch, [])
    payload = json.dumps({"state": "END", "speech": "Bye!", "intent": "x", "extracted_facts": {}, "tool_call": None,
                          "end_call": True})
    engine = ConversationEngine(FakeNonStreamingLLM(payload), StateMachine(_script(), "INTRO"), _context(),
                                tool_registry=ToolRegistry())
    result, _ = await _run(engine)
    await asyncio.sleep(0.05)
    assert result.end_call is True


def test_closing_states_come_from_the_script_config():
    from orchestrator.state_machine import load_script_by_id

    script = load_script_by_id("product-a")
    assert {s for s in script.states if script.is_closing_state(s)} >= {"END", "DO_NOT_CALL", "INTERESTED", "CALLBACK",
                                                                         "NOT_INTERESTED", "WRONG_NUMBER", "UNCERTAIN"}
    for state in ("INTRO", "PERMISSION", "DISCOVERY", "QUALIFICATION", "OBJECTION_HANDLING"):
        assert not script.is_closing_state(state)


async def test_long_intent_does_not_break_turn_persistence(monkeypatch):
    captured = []
    _patch_session_scope(monkeypatch, captured)
    payload = json.dumps({"state": "INTRO", "speech": "Hi", "intent": "x" * 300, "extracted_facts": {}, "tool_call": None,
                          "end_call": False})
    engine = ConversationEngine(FakeNonStreamingLLM(payload), StateMachine(_script(), "INTRO"), _context(),
                                tool_registry=ToolRegistry())
    await _run(engine)
    await asyncio.sleep(0.05)
    from database.models import Turn

    agent_turn = next(o for o in captured[0].added if isinstance(o, Turn) and o.speaker == "agent")
    assert len(agent_turn.intent) == 100


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Stop calling me.", True),
        ("Who is this? Stop calling me and remove my number from your list.", True),
        ("Please take me off your list", True),
        ("Don't call me again!", True),
        ("Add me to your do not call list.", True),
        ("Never call this number again", True),
        ("Don't call me now, call me back tomorrow at 8.", False),
        ("I'm busy, can you call me later?", False),
        ("Yes, please have your sales team call me.", False),
        ("Yes, please have your sales team calming.", False),  # an actual STT mishearing from a test call
        ("Please don't contact me.", True),
        ("Stop bothering me.", True),
        ("I don't want any more calls.", True),
        ("I don't want calls right now, call me next week.", False),
        ("Don't text me, call me instead.", False),
        ("Unsubscribe me please.", True),
    ],
)
def test_dnc_phrases_from_config_are_specific(text, expected):
    from orchestrator.state_machine import load_script_by_id

    assert load_script_by_id("product-a").is_dnc_request(text) is expected


async def test_dnc_backstop_records_dnc_without_the_llm(monkeypatch):
    from orchestrator.state_machine import load_script_by_id

    _patch_session_scope(monkeypatch, [])
    invoked = []

    class RecordingRegistry(ToolRegistry):
        async def invoke(self, ctx, name, arguments):
            invoked.append((name, ctx.current_state))
            return ToolResult(success=True, message="Number suppressed")

    class ExplodingLLM(FakeNonStreamingLLM):
        async def propose(self, *a, **k):
            raise AssertionError("the LLM must not be consulted for a DNC request")

    engine = ConversationEngine(ExplodingLLM(""), StateMachine(load_script_by_id("product-a"), "DISCOVERY"),
                                _context(), tool_registry=RecordingRegistry())
    result, spoken = await _run(engine, "Stop calling me and take me off your list.")
    await asyncio.sleep(0.05)
    assert invoked == [("mark_dnc", "DO_NOT_CALL")]
    assert result.end_call is True and result.new_state == "DO_NOT_CALL"
    assert spoken == ["Understood — we won't call again. Apologies for the disturbance."]  # config text, spoken once


async def test_nothing_changes_before_the_customer_has_spoken(monkeypatch):
    # Caught with a small local model: on the opening greeting — no customer words yet —
    # it proposed DO_NOT_CALL with mark_dnc, which would have suppressed an innocent lead.
    _patch_session_scope(monkeypatch, [])
    invoked = []

    class RecordingRegistry(ToolRegistry):
        async def invoke(self, ctx, name, arguments):
            invoked.append(name)
            return ToolResult(success=True)

    payload = json.dumps({"state": "DO_NOT_CALL", "speech": "Hello!", "intent": "x", "extracted_facts": {},
                          "tool_call": {"name": "mark_dnc", "arguments": "{}"}, "end_call": True})
    engine = ConversationEngine(FakeNonStreamingLLM(payload), StateMachine(_script(), "INTRO"), _context(),
                                tool_registry=RecordingRegistry())
    result, spoken = await _run(engine, "")
    await asyncio.sleep(0.05)
    assert invoked == []
    assert result.new_state == "INTRO" and result.end_call is False
    assert spoken == ["Hello!"]


def test_opening_turn_schema_only_allows_the_current_state():
    from orchestrator.state_machine import load_script_by_id

    engine = ConversationEngine(FakeNonStreamingLLM(""), StateMachine(load_script_by_id("product-a"), "INTRO"), _context())
    opening = engine._turn_schema(customer_spoke=False)
    assert opening["properties"]["state"]["enum"] == ["INTRO"]
    assert opening["properties"]["intent"]["enum"] == ["no_customer_message_yet"]
    normal = engine._turn_schema(customer_spoke=True)
    assert "PERMISSION" in normal["properties"]["state"]["enum"]
    assert "confirmed_identity" in normal["properties"]["intent"]["enum"]
    assert list(normal["properties"])[:3] == ["intent", "state", "speech"]  # classify first, speech after state


def _dnc_guess_payload():
    return json.dumps({"state": "DO_NOT_CALL", "speech": "Sure, our team will call you.", "intent": "x",
                       "extracted_facts": {}, "tool_call": {"name": "mark_dnc", "arguments": "{}"}, "end_call": True})


async def test_local_model_dnc_guess_is_refused(monkeypatch):
    # Real case: "Yes, please have your sales team call me" (heard as "...calming") and
    # the local model proposed DO_NOT_CALL + mark_dnc. Suppressing that lead is wrong.
    _patch_session_scope(monkeypatch, [])
    invoked = []

    class RecordingRegistry(ToolRegistry):
        async def invoke(self, ctx, name, arguments):
            invoked.append(name)
            return ToolResult(success=True)

    llm = FakeNonStreamingLLM(_dnc_guess_payload())
    llm.backend = "local"
    from orchestrator.state_machine import load_script_by_id

    engine = ConversationEngine(llm, StateMachine(load_script_by_id("product-a"), "QUALIFICATION"), _context(),
                                tool_registry=RecordingRegistry())
    result, _ = await _run(engine, "Yes, please have your sales team calming.")
    await asyncio.sleep(0.05)
    assert invoked == []
    assert result.new_state == "QUALIFICATION" and result.end_call is False


async def test_openai_model_dnc_judgement_is_still_honoured(monkeypatch):
    _patch_session_scope(monkeypatch, [])
    invoked = []

    class RecordingRegistry(ToolRegistry):
        async def invoke(self, ctx, name, arguments):
            invoked.append(name)
            return ToolResult(success=True)

    from orchestrator.state_machine import load_script_by_id

    engine = ConversationEngine(FakeNonStreamingLLM(_dnc_guess_payload()),
                                StateMachine(load_script_by_id("product-a"), "QUALIFICATION"), _context(),
                                tool_registry=RecordingRegistry())
    result, _ = await _run(engine, "Look, I've had enough of these.")
    await asyncio.sleep(0.05)
    assert invoked == ["mark_dnc"]
    assert result.new_state == "DO_NOT_CALL"


async def test_invalid_llm_output_keeps_state_and_speaks_the_fallback(monkeypatch):
    _patch_session_scope(monkeypatch, [])
    engine = ConversationEngine(
        FakeNonStreamingLLM("this is not json"),
        StateMachine(_script(), "INTRO"),
        _context(),
        tool_registry=ToolRegistry(),
    )
    result, spoken = await _run(engine)
    await asyncio.sleep(0.05)
    assert result.used_fallback is True
    assert result.new_state == "INTRO"
    assert len(spoken) == 1

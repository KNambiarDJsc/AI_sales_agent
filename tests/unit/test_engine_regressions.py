"""Engine regressions found during live demo testing (no DB, no network)."""
import asyncio
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

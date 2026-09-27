import asyncio
import contextlib
import uuid

import pytest

from database.models import Turn, TranscriptSegment
from llm.base import LLMProposal, LLMProvider
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.state_machine import ScriptConfig, StateConfig, StateMachine
from tests.unit.test_engine_streaming import _valid_payload

pytestmark = pytest.mark.asyncio


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
    def __init__(self, raw_text: str):
        self._raw_text = raw_text

    async def propose(self, messages, json_schema, schema_name="agent_response") -> LLMProposal:
        return LLMProposal(raw_text=self._raw_text, model="fake")

    async def propose_stream(self, messages, json_schema, schema_name="agent_response"):
        raise NotImplementedError
        yield  # pragma: no cover


class FakeBgSession:
    """Records everything `.add()`-ed; `.flush()` assigns fake PKs so FK-dependent
    follow-up inserts (TranscriptSegment.turn_id) work the same way they would
    against a real session."""

    def __init__(self):
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Turn) and obj.id is None:
                obj.id = uuid.uuid4()


def _patch_session_scope(monkeypatch, captured: list):
    @contextlib.asynccontextmanager
    async def fake_session_scope():
        session = FakeBgSession()
        captured.append(session)
        yield session

    monkeypatch.setattr("orchestrator.engine.session_scope", fake_session_scope)


async def test_run_turn_persists_both_customer_and_agent_sides(monkeypatch):
    captured: list[FakeBgSession] = []
    _patch_session_scope(monkeypatch, captured)

    engine = ConversationEngine(
        FakeNonStreamingLLM(_valid_payload(state="DISCOVERY", speech="Great, thanks for confirming!")),
        StateMachine(_script(), "INTRO"),
        _context(),
    )

    await engine.run_turn(
        "Yes, that's me",
        session=None,
        tenant_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    await asyncio.sleep(0.05)  # let the fire-and-forget persistence task run

    assert len(captured) == 1
    turns = [obj for obj in captured[0].added if isinstance(obj, Turn)]
    segments = [obj for obj in captured[0].added if isinstance(obj, TranscriptSegment)]

    assert {t.speaker for t in turns} == {"customer", "agent"}
    assert {s.speaker for s in segments} == {"customer", "agent"}
    assert len(turns) == 2
    assert len(segments) == 2

    customer_turn = next(t for t in turns if t.speaker == "customer")
    agent_turn = next(t for t in turns if t.speaker == "agent")
    assert customer_turn.turn_index < agent_turn.turn_index  # customer spoke first

    customer_segment = next(s for s in segments if s.speaker == "customer")
    agent_segment = next(s for s in segments if s.speaker == "agent")
    assert customer_segment.text == "Yes, that's me"
    assert agent_segment.text == "Great, thanks for confirming!"
    # Segments link back to their own Turn row's real (post-flush) id, not None.
    assert customer_segment.turn_id == customer_turn.id
    assert agent_segment.turn_id == agent_turn.id


async def test_opening_line_with_no_customer_text_only_persists_agent_side(monkeypatch):
    captured: list[FakeBgSession] = []
    _patch_session_scope(monkeypatch, captured)

    engine = ConversationEngine(
        FakeNonStreamingLLM(_valid_payload(state="INTRO", speech="Hi, is this Acme Traders?")),
        StateMachine(_script(), "INTRO"),
        _context(),
    )

    await engine.run_turn(
        "",  # opening line: no customer text yet
        session=None,
        tenant_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    turns = [obj for obj in captured[0].added if isinstance(obj, Turn)]
    segments = [obj for obj in captured[0].added if isinstance(obj, TranscriptSegment)]
    assert len(turns) == 1
    assert len(segments) == 1
    assert turns[0].speaker == "agent"
    assert segments[0].speaker == "agent"

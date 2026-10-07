"""Real-time speech: phrases handed to TTS while the LLM is still writing.
CLAUDE.md rule 10: speech starts exactly once per turn — never zero, never twice."""
import asyncio
import json

import pytest

from config.settings import get_settings
from orchestrator.engine import ConversationEngine
from orchestrator.state_machine import StateMachine
from orchestrator.streaming import PhraseSplitter, extract_partial_json_string
from tests.unit.test_engine_streaming import FakeNonStreamingLLM, FakeStreamingLLM, _context, _script, _valid_payload

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _speculative_on():
    s = get_settings()
    original = s.enable_speculative_tts
    s.enable_speculative_tts = True
    yield
    s.enable_speculative_tts = original


def test_partial_json_string_decoding_handles_escapes_and_cut_offs():
    assert extract_partial_json_string('{"state": "A", "spe', "speech") == (None, False)
    assert extract_partial_json_string('{"speech": "Hel', "speech") == ("Hel", False)
    assert extract_partial_json_string('{"speech": "It\\u2019s fine', "speech") == ("It’s fine", False)
    assert extract_partial_json_string('{"speech": "It\\u20', "speech") == ("It", False)  # incomplete escape held back
    assert extract_partial_json_string('{"speech": "Say \\"hi\\"', "speech") == ('Say "hi"', False)
    assert extract_partial_json_string('{"speech": "Done.", "x": 1}', "speech") == ("Done.", True)


def test_phrase_splitter_releases_a_short_first_phrase_then_longer_ones():
    sp = PhraseSplitter()
    text = "That's great to hear, Naman. What products would you like to sell on Amazon?"
    assert sp.feed("That's great to", final=False) == []
    assert sp.feed("That's great to hear, Na", final=False) == ["That's great to hear,"]
    assert sp.feed("That's great to hear, Naman. What products", final=False) == ["Naman."]
    assert sp.feed(text, final=True) == ["What products would you like to sell on Amazon?"]


def test_long_opening_without_punctuation_is_released_at_a_pause_word():
    sp = PhraseSplitter()
    assert sp.feed("We can help you get your products list", final=False) == []  # too short, last word incomplete
    assert sp.feed("We can help you get your products listed on Ama", final=False) == [
        "We can help you get your products listed"  # cut before the pause word "on"
    ]
    assert sp.feed("We can help you get your products listed on Amazon.", final=True) == ["on Amazon."]


def test_phrase_splitter_never_loses_or_reorders_words():
    text = "Thanks, that helps a lot. We work with sellers like you, every day, across India. Shall I continue?"
    sp = PhraseSplitter()
    pieces = []
    for i in range(1, len(text) + 1):
        pieces += sp.feed(text[:i], final=False)
    pieces += sp.feed(text, final=True)
    assert " ".join(pieces).split() == text.split()


async def _collect_stream(engine, chunks_source):
    phrases, starts = [], []
    done = asyncio.Event()

    async def on_speech_stream(stream):
        starts.append(len(chunks_source.yielded))  # how much of the LLM output existed when speech started

        async def drain():
            async for p in stream:
                phrases.append(p)
            done.set()

        asyncio.create_task(drain())

    outcome = await engine._propose_and_validate([], None, None, on_speech_stream)
    await asyncio.wait_for(done.wait(), 1)
    return outcome, phrases, starts


class CountingStreamingLLM(FakeStreamingLLM):
    def __init__(self, chunks, **kw):
        super().__init__(chunks, **kw)
        self.yielded = []

    async def propose_stream(self, messages, json_schema, schema_name="agent_response"):
        async for c in super().propose_stream(messages, json_schema, schema_name):
            self.yielded.append(c)
            yield c


async def test_speech_starts_with_the_first_phrase_before_the_reply_is_written():
    speech = "That's great to hear, Naman. Can you tell me a bit more about your business?"
    payload = _valid_payload(state="DISCOVERY", speech=speech)
    llm = CountingStreamingLLM([payload[i : i + 3] for i in range(0, len(payload), 3)])
    engine = ConversationEngine(llm, StateMachine(_script(), "INTRO"), _context())
    outcome, phrases, starts = await _collect_stream(engine, llm)
    assert outcome.accepted
    assert len(starts) == 1  # the stream callback fired exactly once
    speech_start = payload.index(speech)
    assert len("".join(llm.yielded[: starts[0]])) < speech_start + len(speech)  # before the speech was complete
    assert phrases[0] == "That's great to hear,"
    assert " ".join(phrases) == speech


async def test_stream_dying_mid_speech_ends_at_what_was_said_and_never_restarts():
    speech = "That's great to hear, Naman. Can you tell me a bit more about your business?"
    payload = _valid_payload(state="DISCOVERY", speech=speech)
    chunks = [payload[i : i + 3] for i in range(0, len(payload), 3)]
    cut = (payload.index("Naman.") + 10) // 3  # dies after the first sentence, before the second finishes
    llm = CountingStreamingLLM(chunks, error_after=cut)
    engine = ConversationEngine(llm, StateMachine(_script(), "INTRO"), _context())
    outcome, phrases, starts = await _collect_stream(engine, llm)
    assert len(starts) == 1
    assert phrases == ["That's great to hear,", "Naman."]
    assert outcome.proposal.speech == "That's great to hear, Naman."  # history matches what was heard
    assert outcome.proposal.state == "DISCOVERY"


async def test_disallowed_state_never_streams_early_and_falls_back_once():
    payload = _valid_payload(state="NONEXISTENT", speech="This must not play, not even a word.")
    llm = CountingStreamingLLM([payload[i : i + 3] for i in range(0, len(payload), 3)])
    engine = ConversationEngine(llm, StateMachine(_script(), "INTRO"), _context())
    outcome, phrases, starts = await _collect_stream(engine, llm)
    assert outcome.used_fallback
    assert len(starts) == 1
    assert "must not play" not in " ".join(phrases)


async def test_non_streaming_path_delivers_the_whole_reply_as_one_phrase(monkeypatch):
    get_settings().enable_speculative_tts = False
    engine = ConversationEngine(FakeNonStreamingLLM(_valid_payload(speech="Hello there. How are you?")),
                                StateMachine(_script(), "INTRO"), _context())
    outcome, phrases, starts = await _collect_stream(engine, type("L", (), {"yielded": []})())
    assert phrases == ["Hello there. How are you?"]
    assert len(starts) == 1

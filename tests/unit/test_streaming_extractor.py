import json

from orchestrator.state_machine import ScriptConfig, StateConfig, StateMachine
from orchestrator.streaming import SpeculativeTurnExtractor, extract_json_string_field


def _sample_script() -> ScriptConfig:
    return ScriptConfig(
        script_id="test-script",
        version=1,
        states={
            "INTRO": StateConfig(objective="greet", transitions_on={"granted": "DISCOVERY"}),
            "DISCOVERY": StateConfig(objective="ask", transitions_on={}),
        },
    )


def _feed_in_chunks(extractor: SpeculativeTurnExtractor, text: str, chunk_size: int = 3) -> None:
    for i in range(0, len(text), chunk_size):
        extractor.feed(text[i : i + chunk_size])


# --- extract_json_string_field ---


def test_extract_returns_none_before_field_present():
    assert extract_json_string_field('{"sta', "state") is None


def test_extract_returns_none_before_closing_quote():
    assert extract_json_string_field('{"state": "DISCO', "state") is None


def test_extract_returns_value_once_closed():
    assert extract_json_string_field('{"state": "DISCOVERY", "speech"', "state") == "DISCOVERY"


def test_extract_handles_escaped_quotes():
    buf = '{"speech": "She said \\"hello\\" to me", "next": 1}'
    assert extract_json_string_field(buf, "speech") == 'She said "hello" to me'


def test_extract_does_not_confuse_similar_prefixes():
    # "statement" must not be mistaken for the "state" field.
    buf = '{"statement": "not this", "state": "DISCOVERY"'
    # our matcher looks for the literal `"state"` marker (with quotes), so "statement"
    # (whose key is "statement", not "state") should not match.
    assert extract_json_string_field(buf, "state") == "DISCOVERY"


# --- SpeculativeTurnExtractor ---


def test_speech_is_released_once_state_is_valid_and_speech_closes():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    extractor = SpeculativeTurnExtractor(sm)
    raw = json.dumps(
        {
            "state": "DISCOVERY",
            "speech": "Are you interested in selling online?",
            "intent": "x",
            "extracted_facts": {"slow_field": "x" * 500},
            "tool_call": None,
            "end_call": False,
        }
    )
    _feed_in_chunks(extractor, raw)
    assert extractor.speech_ready
    assert extractor.speech == "Are you interested in selling online?"


def test_speech_is_never_released_when_state_is_disallowed():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    extractor = SpeculativeTurnExtractor(sm)
    raw = json.dumps(
        {
            "state": "NONEXISTENT_STATE",  # not reachable from INTRO
            "speech": "This should never be spoken early.",
            "intent": "x",
            "extracted_facts": {},
            "tool_call": None,
            "end_call": False,
        }
    )
    _feed_in_chunks(extractor, raw)
    assert not extractor.speech_ready
    assert extractor.speech is None


def test_partial_stream_never_marks_ready():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    extractor = SpeculativeTurnExtractor(sm)
    extractor.feed('{"state": "DISCOVERY", "speech": "Hello, are you')
    assert not extractor.speech_ready


def test_staying_in_current_state_is_allowed_and_releases_speech():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    extractor = SpeculativeTurnExtractor(sm)
    raw = json.dumps(
        {"state": "INTRO", "speech": "Sorry, could you repeat that?", "intent": "x",
         "extracted_facts": {}, "tool_call": None, "end_call": False}
    )
    _feed_in_chunks(extractor, raw)
    assert extractor.speech_ready
    assert extractor.speech == "Sorry, could you repeat that?"

import json

from orchestrator.state_machine import ScriptConfig, StateConfig, StateMachine
from orchestrator.validator import validate_llm_response


def _sample_script() -> ScriptConfig:
    return ScriptConfig(
        script_id="test-script",
        version=1,
        states={
            "INTRO": StateConfig(
                objective="greet",
                allowed_tools=["update_lead"],
                fallback_response="Sorry, could you repeat that?",
                transitions_on={"granted": "DISCOVERY"},
            ),
            "DISCOVERY": StateConfig(objective="ask", allowed_tools=[], transitions_on={}),
        },
    )


def test_valid_response_is_accepted():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    raw = json.dumps(
        {
            "state": "DISCOVERY",
            "speech": "Are you interested in selling online?",
            "intent": "greeting_ack",
            "extracted_facts": {},
            "tool_call": None,
            "end_call": False,
        }
    )
    outcome = validate_llm_response(raw, sm)
    assert outcome.accepted
    assert not outcome.used_fallback
    assert outcome.proposal.state == "DISCOVERY"


def test_malformed_json_falls_back_safely():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    outcome = validate_llm_response("not json at all", sm)
    assert not outcome.accepted
    assert outcome.used_fallback
    assert outcome.proposal.state == "INTRO"  # stays put, never crashes
    assert outcome.proposal.speech == sm.fallback_response()


def test_disallowed_transition_falls_back():
    sm = StateMachine(_sample_script(), current_state="INTRO")
    raw = json.dumps(
        {
            "state": "END",  # END is a global safety state so this actually IS allowed —
            "speech": "ok",  # use a genuinely unreachable state instead below.
            "intent": "x",
            "extracted_facts": {},
            "tool_call": None,
            "end_call": False,
        }
    )
    # Sanity: END is allowed everywhere.
    assert validate_llm_response(raw, sm).accepted

    raw_bad = json.dumps(
        {
            "state": "NONEXISTENT_STATE",
            "speech": "ok",
            "intent": "x",
            "extracted_facts": {},
            "tool_call": None,
            "end_call": False,
        }
    )
    outcome = validate_llm_response(raw_bad, sm)
    assert not outcome.accepted
    assert outcome.used_fallback
    assert "NONEXISTENT_STATE" in outcome.rejections[0]


def test_disallowed_tool_falls_back():
    sm = StateMachine(_sample_script(), current_state="DISCOVERY")  # allowed_tools=[] + safety tools only
    raw = json.dumps(
        {
            "state": "DISCOVERY",
            "speech": "ok",
            "intent": "x",
            "extracted_facts": {},
            "tool_call": {"name": "update_lead", "arguments": {}},
            "end_call": False,
        }
    )
    outcome = validate_llm_response(raw, sm)
    assert not outcome.accepted
    assert outcome.used_fallback

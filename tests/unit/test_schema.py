"""Regression tests for the strict-json_schema fix confirmed against a live OpenAI
key (STATUS.md): OpenAI's strict mode rejects an object schema with
`additionalProperties: true`, so `extracted_facts` and `tool_call.arguments` are sent
on the wire as JSON-encoded strings, not nested objects. These tests lock in that the
Pydantic models decode that wire format correctly, in addition to still accepting the
plain-dict form the rest of the test suite uses for convenience.
"""
import json

from orchestrator.schema import AgentResponseProposal, ToolCallProposal, build_agent_response_schema
from orchestrator.state_machine import ScriptConfig, StateConfig, StateMachine
from orchestrator.validator import validate_llm_response


def _base_fields(**overrides):
    fields = {
        "state": "DISCOVERY",
        "speech": "Are you interested in selling online?",
        "intent": "ask",
        "extracted_facts": {},
        "tool_call": None,
        "end_call": False,
    }
    fields.update(overrides)
    return fields


def test_extracted_facts_as_json_string_decodes_to_dict():
    proposal = AgentResponseProposal.model_validate(
        _base_fields(extracted_facts='{"interested_in_amazon_selling": true}')
    )
    assert proposal.extracted_facts == {"interested_in_amazon_selling": True}


def test_extracted_facts_as_plain_dict_still_works():
    proposal = AgentResponseProposal.model_validate(
        _base_fields(extracted_facts={"interested_in_amazon_selling": True})
    )
    assert proposal.extracted_facts == {"interested_in_amazon_selling": True}


def test_extracted_facts_empty_json_string_decodes_to_empty_dict():
    proposal = AgentResponseProposal.model_validate(_base_fields(extracted_facts="{}"))
    assert proposal.extracted_facts == {}


def test_extracted_facts_malformed_string_degrades_to_empty_dict_not_a_crash():
    proposal = AgentResponseProposal.model_validate(_base_fields(extracted_facts="not json"))
    assert proposal.extracted_facts == {}


def test_tool_call_arguments_as_json_string_decodes_to_dict():
    tool_call = ToolCallProposal.model_validate({"name": "schedule_callback", "arguments": '{"notes": "call back Tuesday"}'})
    assert tool_call.arguments == {"notes": "call back Tuesday"}


def test_tool_call_arguments_malformed_string_degrades_to_empty_dict():
    tool_call = ToolCallProposal.model_validate({"name": "end_call", "arguments": "{oops"})
    assert tool_call.arguments == {}


def test_full_validator_round_trip_with_real_wire_format():
    """Mimics exactly what the live API actually returns (confirmed via smoke test):
    extracted_facts and tool_call.arguments as JSON-encoded strings, everything else
    a plain string/bool."""
    raw_text = json.dumps(
        {
            "state": "QUALIFICATION",
            "speech": "Great, what product are you looking to sell?",
            "intent": "explicit_interest",
            "extracted_facts": '{"interested_in_amazon_selling": true}',
            "tool_call": {"name": "update_lead", "arguments": '{"business_name": "Rao Textiles"}'},
            "end_call": False,
        }
    )
    script = ScriptConfig(
        script_id="test",
        version=1,
        states={
            # Tool authorization is checked against the CURRENT state (before this
            # turn's transition takes effect), so the allow-list belongs on DISCOVERY,
            # not the state being transitioned to.
            "DISCOVERY": StateConfig(allowed_tools=["update_lead"], transitions_on={"explicit_interest": "QUALIFICATION"}),
            "QUALIFICATION": StateConfig(transitions_on={}),
        },
    )
    sm = StateMachine(script, current_state="DISCOVERY")

    outcome = validate_llm_response(raw_text, sm)

    assert outcome.accepted
    assert outcome.proposal.extracted_facts == {"interested_in_amazon_selling": True}
    assert outcome.proposal.tool_call.name == "update_lead"
    assert outcome.proposal.tool_call.arguments == {"business_name": "Rao Textiles"}


def test_build_agent_response_schema_constrains_state_to_an_enum():
    # Regression test for a real bug caught on a live call: `state` had no enum at
    # all, so the model proposed states that don't exist ("EXPLAIN",
    # "QUALIFY_INTEREST") and states it couldn't reach yet by skipping ahead (INTRO
    # straight to QUALIFICATION) - every one got rejected by the validator and fell
    # back to re-asking the same question, forever, since nothing ever nudged the
    # model toward a state it could actually transition to.
    schema = build_agent_response_schema(["INTRO", "PERMISSION", "DO_NOT_CALL", "END"])
    assert schema["properties"]["state"] == {
        "type": "string",
        "enum": ["INTRO", "PERMISSION", "DO_NOT_CALL", "END"],
    }


def test_build_agent_response_schema_does_not_mutate_the_base_schema():
    from orchestrator.schema import AGENT_RESPONSE_JSON_SCHEMA

    build_agent_response_schema(["INTRO", "END"])
    assert AGENT_RESPONSE_JSON_SCHEMA["properties"]["state"] == {"type": "string"}

from orchestrator.context import ConversationContext
from orchestrator.prompts import build_system_message
from orchestrator.state_machine import load_script_by_id


def _context(**overrides) -> ConversationContext:
    defaults = dict(
        conversation_id="conv-1",
        campaign_id="camp-1",
        lead_id="lead-1",
        script_id="product-a",
        script_version=1,
        current_state="INTRO",
        campaign_prompt={"product_info": "Selling on Amazon", "target_customer": "SMBs"},
    )
    defaults.update(overrides)
    return ConversationContext(**defaults)


def test_system_message_includes_current_date_in_campaign_timezone():
    # Regression test for a real gap found while reviewing a client POC: the LLM was
    # never told what "today" is, so a customer saying "call me tomorrow at 8am" had no
    # anchor date to resolve against - schedule_callback's requested_time could never
    # be filled in correctly from a relative phrase like that.
    script = load_script_by_id("product-a")
    context = _context(timezone="Asia/Kolkata")
    message = build_system_message(script, context)
    assert "Current date/time (Asia/Kolkata):" in message.content
    assert "schedule_callback" in message.content


def test_system_message_lists_valid_next_states_explicitly():
    # Companion to the dynamic-enum schema fix: telling the model its options in plain
    # text (not just constraining the schema silently) is what actually produces a
    # sensible transition instead of the model just hitting the enum wall repeatedly.
    script = load_script_by_id("product-a")
    context = _context(current_state="INTRO")
    message = build_system_message(script, context)
    assert "Valid values for 'state' this turn" in message.content
    assert "PERMISSION" in message.content  # INTRO's real transitions_on target
    assert "DO_NOT_CALL" in message.content  # global safety state, always present


def test_system_message_gives_tool_argument_schemas_not_just_names():
    # Regression test for a live finding: the prompt listed tool names only, so the LLM
    # called create_qualification({}) and the tool (correctly) rejected it for missing
    # evidence — no qualification was ever recorded in a real conversation.
    script = load_script_by_id("product-a")
    message = build_system_message(script, _context(current_state="QUALIFICATION")).content
    assert "- create_qualification:" in message
    for field in ("facts (object", "evidence (array", "dimension_confidence (object", "summary ("):
        assert field in message, field
    assert "requested_time (" in message  # schedule_callback is allowed here too


def test_system_message_lists_the_qualification_fact_keys_from_config():
    # The scoring engine reads specific fact keys from config/qualification/rules.yaml;
    # without being told them the LLM invented its own ("product_type") and nothing
    # could ever qualify.
    script = load_script_by_id("product-a")
    message = build_system_message(script, _context(current_state="DISCOVERY")).content
    for key in ("interested_in_amazon_selling", "sufficient_business_info_captured", "willing_to_be_contacted"):
        assert key in message


def test_system_message_does_not_offer_tools_outside_the_state():
    script = load_script_by_id("product-a")
    message = build_system_message(script, _context(current_state="INTRO")).content
    allowed_line = next(line for line in message.splitlines() if line.startswith("Tools you may call this turn"))
    assert "create_qualification" not in allowed_line
    assert "end_call" in allowed_line and "mark_dnc" in allowed_line  # global safety tools


def test_tool_reference_shows_a_complete_example_for_create_qualification():
    script = load_script_by_id("product-a")
    message = build_system_message(script, _context(current_state="QUALIFICATION")).content
    example_line = next(
        line for line in message.splitlines()
        if "example arguments" in line and "interested_in_amazon_selling" in line
    )
    assert '"evidence"' in example_line and '"dimension_confidence"' in example_line


def test_placeholder_config_values_are_not_shown_to_the_llm_as_text():
    # Live finding: the template's "PLACEHOLDER: one or two paragraphs describing..."
    # reached the model verbatim and it said "I'm calling from [Company Name]" on calls.
    script = load_script_by_id("product-a")
    context = _context(campaign_prompt={"product_info": "PLACEHOLDER: describe the service", "target_customer": ""})
    message = build_system_message(script, context).content
    assert "PLACEHOLDER" not in message  # system_prompt.yaml's identity_disclosure is a placeholder too
    assert "Campaign product info: not provided yet" in message
    assert "Target customer: not provided yet" in message


def test_system_message_falls_back_to_utc_for_an_invalid_timezone():
    # A campaign misconfigured with a bad timezone string must not crash a live call.
    script = load_script_by_id("product-a")
    context = _context(timezone="Not/ARealZone")
    message = build_system_message(script, context)
    assert "Current date/time (Not/ARealZone):" in message.content  # label echoes config, not the fallback

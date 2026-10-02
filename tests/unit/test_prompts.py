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


def test_system_message_falls_back_to_utc_for_an_invalid_timezone():
    # A campaign misconfigured with a bad timezone string must not crash a live call.
    script = load_script_by_id("product-a")
    context = _context(timezone="Not/ARealZone")
    message = build_system_message(script, context)
    assert "Current date/time (Not/ARealZone):" in message.content  # label echoes config, not the fallback

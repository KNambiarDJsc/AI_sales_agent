from orchestrator.state_machine import StateMachine, load_script_by_id, substitute_placeholders


def test_product_a_script_loads_and_has_expected_states():
    script = load_script_by_id("product-a")
    assert script.script_id == "product-a"
    assert "INTRO" in script.states
    assert "DO_NOT_CALL" in script.states


def test_dnc_and_end_are_always_reachable():
    script = load_script_by_id("product-a")
    sm = StateMachine(script, current_state="DISCOVERY")
    assert sm.is_transition_allowed("DO_NOT_CALL")
    assert sm.is_transition_allowed("END")
    assert sm.is_transition_allowed("DISCOVERY")  # staying put is always allowed


def test_disallowed_transition_is_rejected():
    script = load_script_by_id("product-a")
    sm = StateMachine(script, current_state="INTRO")
    # INTRO's transitions_on only reach PERMISSION/WRONG_NUMBER (+global safety states).
    assert not sm.is_transition_allowed("INTERESTED")


def test_mark_dnc_and_end_call_are_always_allowed_tools():
    script = load_script_by_id("product-a")
    sm = StateMachine(script, current_state="INTRO")
    tools = sm.allowed_tools()
    assert "mark_dnc" in tools
    assert "end_call" in tools


def test_retry_limit_enforced():
    script = load_script_by_id("product-a")
    sm = StateMachine(script, current_state="DISCOVERY")
    limit = script.retry_limits["per_state"]
    for _ in range(limit):
        assert not sm.retry_limit_exceeded()
        sm.record_retry()
    assert sm.retry_limit_exceeded()


def test_substitute_placeholders_fills_known_fields():
    text = "Confirm you're speaking with {contact_name} at {business_name}."
    result = substitute_placeholders(text, {"contact_name": "Asha Rao", "business_name": "Rao Textiles"})
    assert result == "Confirm you're speaking with Asha Rao at Rao Textiles."


def test_substitute_placeholders_leaves_unknown_fields_untouched_not_crashing():
    # A script typo'd field, or a field this campaign never collects, must never crash
    # a live call — it degrades to leaving the literal placeholder rather than raising.
    result = substitute_placeholders("Hello {nickname}, is this {business_name}?", {"business_name": "Rao Textiles"})
    assert result == "Hello {nickname}, is this Rao Textiles?"


def test_substitute_placeholders_is_a_noop_for_plain_text():
    assert substitute_placeholders("No placeholders here.", {"contact_name": "Asha"}) == "No placeholders here."
    assert substitute_placeholders("", {"contact_name": "Asha"}) == ""


def test_fallback_response_substitutes_lead_fields():
    # Regression test: config/scripts/product-a.yaml's INTRO fallback_response contains
    # a literal {business_name} placeholder that was never being substituted — a real
    # bug caught during a live demo verification (see STATUS.md), where the customer
    # would have heard the literal token spoken aloud.
    script = load_script_by_id("product-a")
    sm = StateMachine(script, current_state="INTRO")
    fallback = sm.fallback_response({"business_name": "Rao Textiles", "contact_name": "Asha Rao"})
    assert "{business_name}" not in fallback
    assert "Rao Textiles" in fallback


def test_fallback_response_with_no_fields_still_works():
    script = load_script_by_id("product-a")
    sm = StateMachine(script, current_state="INTRO")
    assert sm.fallback_response() == sm.fallback_response(None)

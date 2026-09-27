from orchestrator.state_machine import StateMachine, load_script_by_id


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

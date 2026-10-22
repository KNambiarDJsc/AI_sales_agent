"""The client's conversation flow (config/scripts/seller-flow.yaml) is wired the way the
brief says: greeting → pitch → intent branches, every branch can close, verified
knowledge reaches the prompt and unverified answers don't."""
import asyncio

from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.prompts import _reply_language_hint, build_system_message, load_campaign_prompt
from orchestrator.state_machine import StateMachine, load_script_by_id
from qualification.rules import load_rules


def _script():
    return load_script_by_id("seller-flow")


def test_flow_branches_match_the_client_brief():
    s = _script()
    assert s.states["INTRO"].transitions_on["confirmed_business"] == "PITCH"
    pitch = set(s.states["PITCH"].transitions_on.values())
    assert pitch == {"INTERESTED", "OBJECTION_HANDLING", "CALLBACK", "CLARIFY"}
    # not interested → objection handling → reassess (can still become interested)
    assert s.states["OBJECTION_HANDLING"].transitions_on["now_interested"] == "INTERESTED"
    assert s.states["OBJECTION_HANDLING"].transitions_on["still_not_interested"] == "NOT_INTERESTED"
    # unclear → clarify → unresolved → human follow-up
    assert s.states["CLARIFY"].transitions_on["still_unclear"] == "UNCERTAIN"
    assert "schedule_callback" in s.allowed_tools("UNCERTAIN")
    # interested → arrange callback → capture timing → close
    assert "schedule_callback" in s.allowed_tools("INTERESTED") and s.is_closing_state("INTERESTED")
    assert s.sales_followup_states == ["INTERESTED"] and s.not_interested_states == ["NOT_INTERESTED"]


def test_every_state_is_reachable_and_every_target_exists():
    s = _script()
    seen, todo = set(), ["INTRO"]
    while todo:
        state = todo.pop()
        if state in seen:
            continue
        seen.add(state)
        for target in s.states[state].transitions_on.values():
            assert target in s.states, (state, target)
            todo.append(target)
    assert seen | {"DO_NOT_CALL"} == set(s.states)


def test_fallbacks_exist_in_english_and_hindi():
    s = _script()
    for name, state in s.states.items():
        if name == "END":
            continue
        assert set(state.fallback_response) == {"en", "hi"}, name


def test_opening_and_dnc():
    s = _script()
    assert StateMachine(s).opening_line({"contact_name": "Naman", "business_name": "Demo Business"}) == \
        "Hello, am I speaking with Naman from Demo Business?"
    assert s.is_dnc_request("please don't call me again")
    assert s.is_dnc_request("दोबारा कॉल मत करना")
    assert not s.is_dnc_request("abhi call mat karo, baad mein karna")


def test_qualification_rules_load_for_the_flow():
    rules = load_rules("seller-flow")
    facts = [c["fact"] for c in rules.qualified_when["all_of"]]
    assert facts == ["interested_in_selling_online", "willing_to_be_contacted"]


def test_prompt_carries_verified_knowledge_but_never_placeholders():
    s = _script()
    campaign_prompt = load_campaign_prompt("config/prompts/seller-flow.yaml")
    campaign_prompt["knowledge"]["returns"] = "Returns are picked up by the marketplace courier."
    context = ConversationContext(
        conversation_id="c", lead_id="l", campaign_id="k", script_id="seller-flow",
        script_version=1, current_state="OBJECTION_HANDLING", campaign_prompt=campaign_prompt,
        lead_fields={"contact_name": "Naman", "business_name": "Demo Business", "product_category": "Perfumes"},
    )
    text = build_system_message(s, context).content
    assert "Returns are picked up by the marketplace courier." in text
    assert "PLACEHOLDER" not in text
    assert "- pricing: not provided yet" in text
    assert "Perfumes" in text  # product category personalizes the pitch
    assert "heard_from_others" in text  # firsthand vs hearsay tracking


def test_reply_language_hint_follows_the_latest_line():
    s = _script()
    assert "in Hindi/Hinglish" in _reply_language_hint(s, "हाँ, इंटरेस्ट है")
    assert "reply in English" in _reply_language_hint(s, "Mostly customers returned used products.")
    assert "reply in English" in _reply_language_hint(s, "No, I'm not sure.")  # was answered in Hinglish
    assert "reply in Hinglish" in _reply_language_hint(s, "Achha, ismein kya karna padega?")
    assert "reply in Hinglish" in _reply_language_hint(s, "Haan bolo")
    assert _reply_language_hint(s.model_copy(update={"language_policy": "fixed"}), "हाँ") == ""


class _WarmLLM:
    backend = "openai"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def warm_up(self, messages, json_schema, schema_name="agent_response"):
        self.calls.append((messages, json_schema))
        if self.fail:
            raise RuntimeError("network down")


def _engine(llm):
    ctx = ConversationContext(conversation_id="c", lead_id="l", campaign_id="k", script_id="seller-flow",
                              script_version=1, current_state="INTRO",
                              lead_fields={"contact_name": "Naman", "business_name": "Demo Business"})
    return ConversationEngine(llm, StateMachine(_script(), "INTRO"), ctx)


def test_warm_up_sends_the_first_turns_instructions_and_never_raises(monkeypatch):
    llm = _WarmLLM()
    asyncio.run(_engine(llm).warm_up())
    (messages, schema), = llm.calls
    assert messages[0].role == "system" and "Verified knowledge" not in messages[0].content  # no campaign prompt here
    assert "PITCH" in schema["properties"]["state"]["enum"]  # the customer-turn schema, not the opening one
    asyncio.run(_engine(_WarmLLM(fail=True)).warm_up())  # swallowed

    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "llm_warm_up", False)
    off = _WarmLLM()
    asyncio.run(_engine(off).warm_up())
    assert off.calls == []

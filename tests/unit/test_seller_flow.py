"""The client's conversation flow (config/scripts/seller-flow.yaml) is wired the way the
brief says: greeting → pitch → intent branches, every branch can close, verified
knowledge reaches the prompt and unverified answers don't."""
import asyncio
from pathlib import Path

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
        "नमस्ते, क्या मेरी बात Naman जी से हो रही है, Demo Business से?"  # Hindi first (client)
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
    assert "reply in simple, everyday spoken Hindi" in _reply_language_hint(s, ["हाँ, इंटरेस्ट है"])
    assert "must be in English" in _reply_language_hint(s, ["Mostly customers returned used products."])
    assert "must be in English" in _reply_language_hint(s, ["No, I'm not sure."])  # was answered in Hinglish
    assert "write every Hindi word in Devanagari" in _reply_language_hint(s, ["Achha, ismein kya karna padega?"])
    assert "Reply in Hinglish" in _reply_language_hint(s, ["Haan bolo"])
    assert _reply_language_hint(s.model_copy(update={"language_policy": "fixed"}), ["हाँ"]) == ""


def test_a_short_yes_keeps_the_language_hindi_first():
    s = _script()
    # Nothing shows a language yet: the call stays in Hindi (the script's language).
    assert "keep replying in Hindi" in _reply_language_hint(s, ["Yes"])
    assert "keep replying in Hindi" in _reply_language_hint(s, ["Okay sure"])
    # A short line keeps whatever the customer last clearly spoke.
    assert "keep replying in English" in _reply_language_hint(s, ["I sell clothes in Pune.", "Okay."])
    assert "keep replying in Hinglish" in _reply_language_hint(s, ["Haan bolo", "yes"])


def test_fallback_lines_start_in_hindi_and_follow_a_clear_switch():
    sm = StateMachine(_script(), "CALLBACK")
    assert sm.fallback_response() == "ज़रूर। आपसे बात करने का सही समय कब रहेगा?"
    sm.note_customer_text("Yes")
    assert sm.fallback_response() == "ज़रूर। आपसे बात करने का सही समय कब रहेगा?"
    sm.note_customer_text("Please call me tomorrow evening.")
    assert sm.fallback_response() == "Sure. When would be a better time to reach you?"
    sm.note_customer_text("ok")
    assert sm.fallback_response() == "Sure. When would be a better time to reach you?"
    sm.note_customer_text("Haan kal shaam ko karna")
    assert sm.fallback_response() == "ज़रूर। आपसे बात करने का सही समय कब रहेगा?"


def test_hindi_lines_speak_as_a_woman():
    # The call's voice is female (OPENAI_TTS_VOICE): no masculine first-person Hindi.
    text = Path("config/scripts/seller-flow.yaml").read_text(encoding="utf-8")
    for masculine in ("समझ गया", "नहीं पाया", "सकता हूँ", "रहा हूँ"):
        assert masculine not in text, masculine


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

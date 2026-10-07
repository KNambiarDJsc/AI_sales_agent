"""Assembles the LLM message list from: system prompt + campaign prompt + product info
+ current state objective + mandatory questions + conversation history + allowed tools
+ output schema (Section 11). No business wording lives in this file — it only stitches
together what config/prompts and config/scripts already say.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from config.settings import PROMPTS_DIR
from llm.base import LLMMessage
from orchestrator.context import ConversationContext
from orchestrator.state_machine import ScriptConfig, substitute_placeholders
from qualification.rules import load_rules
from tools.registry import get_default_registry


@lru_cache
def _load_system_prompt() -> dict:
    with (PROMPTS_DIR / "system_prompt.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_campaign_prompt(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_NOT_PROVIDED = "not provided yet — leave it out of the conversation; never invent it or say a placeholder for it"


def _configured(value: object) -> str:
    """A config value as the LLM should see it. Values the client hasn't supplied yet are
    marked PLACEHOLDER in config (CLAUDE.md rule 9); handing that text to the model
    ("PLACEHOLDER: one or two paragraphs describing...") made it speak placeholders
    such as "[Company Name]" on calls, so it is told plainly that the detail is
    missing instead."""
    text = str(value or "").strip()
    if not text or text.upper().startswith("PLACEHOLDER"):
        return _NOT_PROVIDED
    return text


def _static_system_text(script: ScriptConfig, context: ConversationContext) -> str:
    """Everything identical on every turn of a call (and across calls of a campaign)."""
    system_prompt = _load_system_prompt()
    parts = [
        system_prompt["role"],
        f"Identity disclosure requirement: {_configured(system_prompt.get('identity_disclosure'))}",
        "Behavior rules:",
        *[f"- {rule}" for rule in system_prompt.get("behavior_rules", [])],
        "",
        f"Campaign product info: {_configured(context.campaign_prompt.get('product_info'))}",
        f"Target customer: {_configured(context.campaign_prompt.get('target_customer'))}",
        "",
        system_prompt["output_contract"],
    ]
    qualification_block = _qualification_facts_block(script)
    if qualification_block:
        parts.append(qualification_block)
    parts.append(_tools_reference(script))
    return "\n".join(parts)


def _turn_context_text(script: ScriptConfig, context: ConversationContext) -> str:
    """Everything that changes from turn to turn: the clock, state, questions, facts,
    tools allowed right now."""
    state_cfg = script.states[context.current_state]
    allowed_tools = sorted(script.allowed_tools(context.current_state))
    try:
        now_local = datetime.now(ZoneInfo(context.timezone))
    except Exception:  # noqa: BLE001 - a bad/missing timezone must not break a live call
        now_local = datetime.now(ZoneInfo("UTC"))
    parts = [
        f"Current date/time ({context.timezone}): {now_local.strftime('%A, %Y-%m-%d %H:%M')}. "
        "Resolve any relative time the customer gives (e.g. \"tomorrow at 8am\", \"Monday evening\") "
        "against this when calling schedule_callback — requested_time must be an absolute "
        "ISO 8601 datetime, never a relative phrase.",
        f"Current conversation state: {context.current_state}",
        f"State objective: {state_cfg.objective}",
        f"Valid values for 'state' this turn (you MUST pick one of these exactly — "
        f"usually stay in {context.current_state} unless this turn's objective is "
        "clearly met): " + ", ".join(sorted(script.allowed_next_states(context.current_state))),
    ]
    customer_just_spoke = bool(context.history) and context.history[-1].speaker == "customer"
    if customer_just_spoke:
        parts.append(_transitions_text(script, context.current_state))
    else:
        parts.append(
            "The customer hasn't said anything yet this turn: speak first, working on this state's objective. "
            f"Set intent to no_customer_message_yet and keep state {context.current_state}; call no tools."
        )
    if state_cfg.mandatory_questions:
        parts.append("Mandatory questions for this state (ask any not yet answered):")
        parts.extend(f"- {substitute_placeholders(q, context.lead_fields)}" for q in state_cfg.mandatory_questions)
    if context.lead_fields:
        parts.append(
            "The person you are calling (the customer — not you): "
            f"{json.dumps(context.lead_fields, ensure_ascii=False)}"
        )
    if context.extracted_facts:
        parts.append(f"Facts captured so far: {json.dumps(context.extracted_facts, ensure_ascii=False)}")
    parts.append(_tools_block(allowed_tools))
    return "\n".join(parts)


def _transitions_text(script: ScriptConfig, current_state: str) -> str:
    """When to move where, straight from the script's `transitions_on` — the model used
    to see only the list of reachable state names, not what each one means. Small
    local models in particular guessed (jumping to CALLBACK, ending the call, or
    putting a merely-uninterested customer on DO_NOT_CALL)."""
    state = script.states.get(current_state)
    transitions = dict(state.transitions_on) if state else {}
    lines = [
        "How to choose the next state: first set 'intent' to the label that matches the customer's latest "
        "message, then set 'state' to the state it leads to:"
    ]
    lines += [f"- {label} → {target}" for label, target in transitions.items()]
    if "asked_not_to_be_called" not in transitions:
        lines.append("- asked_not_to_be_called → DO_NOT_CALL (only an explicit request; not merely uninterested)")
    if "said_goodbye" not in transitions:
        lines.append("- said_goodbye → END (the customer is ending the conversation)")
    lines.append(
        f"- other → {current_state} (stay here: the objective isn't met yet — e.g. the customer answered one "
        "question and others remain, asked a question, or gave an unclear answer)"
    )
    return "\n".join(lines)


def build_system_message(script: ScriptConfig, context: ConversationContext) -> LLMMessage:
    """The full instructions as one message (static + this turn's context). The engine
    sends them split around the history instead — see `build_messages`."""
    return LLMMessage(
        role="system", content=_static_system_text(script, context) + "\n\n" + _turn_context_text(script, context)
    )


def _describe_property(name: str, prop: dict, required: bool) -> str:
    kind = prop.get("type")
    if kind is None and "anyOf" in prop:
        kind = "/".join(sorted({p.get("type", "?") for p in prop["anyOf"] if p.get("type") != "null"}))
    text = f"{name} ({kind or 'any'}{', required' if required else ''})"
    if prop.get("description"):
        text += f": {prop['description']}"
    return text


def _script_tools(script: ScriptConfig) -> list[str]:
    names = {tool for state in script.states for tool in script.allowed_tools(state)}
    return sorted(names)


def _tools_reference(script: ScriptConfig) -> str:
    """Every tool the script can use, with its real argument schema (generated from
    the Pydantic input model the registry validates against, so prompt and validation
    can't drift apart) and a complete example. Without arguments the LLM proposed
    create_qualification({}) — always rejected, so no qualification was recorded; small
    models only filled them in reliably once shown a full example.

    Lives in the static, cached part of the prompt; each turn only names which tools
    are allowed right now (`_tools_block`)."""
    registry = get_default_registry()
    lines = [
        "Tools reference (tool_call.arguments must be a JSON-encoded object with these fields; "
        "the application validates it and may refuse):"
    ]
    for name in _script_tools(script):
        spec = registry.get(name)
        if spec is None:
            continue
        schema = spec.input_model.model_json_schema()
        required = set(schema.get("required", []))
        lines.append(f"- {name}: {spec.description}")
        lines.extend(f"    {_describe_property(k, v, k in required)}" for k, v in schema.get("properties", {}).items())
        for example in schema.get("examples", [])[:1]:
            lines.append(f"    example arguments: {json.dumps(example, ensure_ascii=False)}")
    return "\n".join(lines)


def _tools_block(allowed_tools: list[str]) -> str:
    if not allowed_tools:
        return "Tools you may call this turn: none."
    return (
        f"Tools you may call this turn: {', '.join(allowed_tools)} (arguments: see Tools reference). "
        "Any other tool will be refused."
    )


def _qualification_facts_block(script: ScriptConfig) -> str:
    """The fact keys the qualification rules are written against
    (config/qualification/*.yaml), so the LLM reports facts under keys the scoring
    engine actually reads instead of inventing its own ("product_type"). Rendered from
    config only — the bar itself is never shown or decided here."""
    try:
        rules = load_rules(script.script_id)
    except (ValueError, FileNotFoundError):
        return ""
    required = [c.get("fact") for c in rules.qualified_when.get("all_of", []) if c.get("fact")]
    lines = [
        "Qualification facts — when the customer has clearly established one, report it in extracted_facts "
        "(and in create_qualification.facts) as true/false using exactly these keys:"
    ]
    if required:
        lines.append("- core facts: " + ", ".join(required))
    if rules.interested_conditions:
        lines.append("- interest signals: " + ", ".join(rules.interested_conditions))
    if rules.disqualification_conditions:
        lines.append("- disqualifiers: " + ", ".join(rules.disqualification_conditions))
    if rules.dimensions:
        lines.append("- dimensions for create_qualification.dimension_confidence (0..1): " + ", ".join(rules.dimensions))
    if rules.evidence_required:
        lines.append("- create_qualification must include at least one verbatim customer quote in evidence.")
    return "\n".join(lines)


def build_message_history(context: ConversationContext) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    for turn in context.history:
        role = "assistant" if turn.speaker == "agent" else "user"
        messages.append(LLMMessage(role=role, content=turn.text))
    return messages


def build_messages(script: ScriptConfig, context: ConversationContext) -> list[LLMMessage]:
    """[static instructions] + [conversation before the customer's latest line] +
    [this turn's context] + [the customer's latest line].

    Order is for latency: OpenAI's prompt cache and a local model's KV cache (Ollama)
    only reuse an unchanged *prefix*, so the per-turn context (clock, state, facts)
    must not sit at the top — there it broke the cache on every request. Here the
    instructions and all earlier history stay a stable prefix and each turn only
    processes its last few messages.

    The customer's latest line stays *last*: chat models answer the final message.
    With the turn context placed after it, every local model tested replied to the
    instructions instead of the customer — repeating the same greeting and never
    advancing the state."""
    history = build_message_history(context)
    turn_context = LLMMessage(role="system", content=_turn_context_text(script, context))
    if history and history[-1].role == "user":
        body = [*history[:-1], turn_context, history[-1]]
    else:
        body = [*history, turn_context]  # opening line: nothing said yet
    return [LLMMessage(role="system", content=_static_system_text(script, context)), *body]

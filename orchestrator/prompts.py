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

# Human-readable names for the language codes a script's `language` field actually
# uses (config/scripts/*.yaml) — falls back to the raw code for anything not listed
# here rather than failing, since this is just for prompt readability.
_LANGUAGE_NAMES = {
    "en": "English",
    "en-IN": "English (India)",
    "en-US": "English (US)",
    "hi": "Hindi",
    "hi-IN": "Hindi (India)",
}


@lru_cache
def _load_system_prompt() -> dict:
    with (PROMPTS_DIR / "system_prompt.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_campaign_prompt(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_system_message(script: ScriptConfig, context: ConversationContext) -> LLMMessage:
    system_prompt = _load_system_prompt()
    state_cfg = script.states[context.current_state]
    allowed_tools = sorted(script.allowed_tools(context.current_state))

    try:
        now_local = datetime.now(ZoneInfo(context.timezone))
    except Exception:  # noqa: BLE001 - a bad/missing timezone must not break a live call
        now_local = datetime.now(ZoneInfo("UTC"))

    language_name = _LANGUAGE_NAMES.get(script.language, script.language)

    # Order matters for latency: everything that is identical on every turn of every
    # call comes first, everything that changes per turn last. OpenAI caches a
    # repeated prompt *prefix*, which cuts time-to-first-token — but only up to the
    # first character that differs, so the clock and the state used to sit near the
    # top and broke the cache on every single request. Language is call-level config
    # (doesn't change turn to turn), so it stays up here; the clock and state move
    # below the qualification block.
    parts = [
        system_prompt["role"],
        f"Identity disclosure requirement: {system_prompt.get('identity_disclosure', 'NOT CONFIRMED')}",
        f"Speak only in {language_name} ({script.language}) for this entire call. Stay in this "
        "language even if the customer switches to another language or mixes languages "
        "(e.g. Hindi/English code-switching) — never mirror a language change mid-call.",
        "Behavior rules:",
        *[f"- {rule}" for rule in system_prompt.get("behavior_rules", [])],
        "",
        f"Campaign product info: {context.campaign_prompt.get('product_info', 'NOT PROVIDED')}",
        f"Target customer: {context.campaign_prompt.get('target_customer', 'NOT PROVIDED')}",
        "",
        system_prompt["output_contract"],
    ]
    qualification_block = _qualification_facts_block(script)
    if qualification_block:
        parts.append(qualification_block)
    parts += [
        "",
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
    if state_cfg.mandatory_questions:
        parts.append("Mandatory questions for this state (ask any not yet answered):")
        parts.extend(f"- {substitute_placeholders(q, context.lead_fields)}" for q in state_cfg.mandatory_questions)
    if context.lead_fields:
        parts.append(f"Known lead info: {json.dumps(context.lead_fields, ensure_ascii=False)}")
    if context.extracted_facts:
        parts.append(f"Facts captured so far: {json.dumps(context.extracted_facts, ensure_ascii=False)}")
    parts.append(_tools_block(allowed_tools))

    return LLMMessage(role="system", content="\n".join(parts))


def _describe_property(name: str, prop: dict, required: bool) -> str:
    kind = prop.get("type")
    if kind is None and "anyOf" in prop:
        kind = "/".join(sorted({p.get("type", "?") for p in prop["anyOf"] if p.get("type") != "null"}))
    text = f"{name} ({kind or 'any'}{', required' if required else ''})"
    if prop.get("description"):
        text += f": {prop['description']}"
    return text


def _tools_block(allowed_tools: list[str]) -> str:
    """Each allowed tool's real argument schema, generated from its Pydantic input
    model — the same model the registry validates against (tools/registry.py), so
    what the LLM is told and what the application accepts can't drift apart. Without
    this the LLM only ever saw tool *names* and proposed e.g. create_qualification({}),
    which the tool correctly rejected, so no qualification was ever recorded."""
    if not allowed_tools:
        return "Allowed tools in this state: none"
    registry = get_default_registry()
    lines = [
        "Allowed tools in this state (tool_call.arguments must be a JSON-encoded object with these fields; "
        "the application validates it and may refuse):"
    ]
    for name in allowed_tools:
        spec = registry.get(name)
        if spec is None:
            continue
        schema = spec.input_model.model_json_schema()
        required = set(schema.get("required", []))
        props = [_describe_property(k, v, k in required) for k, v in schema.get("properties", {}).items()]
        lines.append(f"- {name}: {spec.description}")
        lines.extend(f"    {p}" for p in props)
    return "\n".join(lines)


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
    return [build_system_message(script, context), *build_message_history(context)]

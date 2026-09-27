"""Assembles the LLM message list from: system prompt + campaign prompt + product info
+ current state objective + mandatory questions + conversation history + allowed tools
+ output schema (Section 11). No business wording lives in this file — it only stitches
together what config/prompts and config/scripts already say.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml

from config.settings import PROMPTS_DIR
from llm.base import LLMMessage
from orchestrator.context import ConversationContext
from orchestrator.state_machine import ScriptConfig


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

    parts = [
        system_prompt["role"],
        f"Identity disclosure requirement: {system_prompt.get('identity_disclosure', 'NOT CONFIRMED')}",
        "Behavior rules:",
        *[f"- {rule}" for rule in system_prompt.get("behavior_rules", [])],
        "",
        f"Campaign product info: {context.campaign_prompt.get('product_info', 'NOT PROVIDED')}",
        f"Target customer: {context.campaign_prompt.get('target_customer', 'NOT PROVIDED')}",
        "",
        f"Current conversation state: {context.current_state}",
        f"State objective: {state_cfg.objective}",
    ]
    if state_cfg.mandatory_questions:
        parts.append("Mandatory questions for this state (ask any not yet answered):")
        parts.extend(f"- {q}" for q in state_cfg.mandatory_questions)
    if context.lead_fields:
        parts.append(f"Known lead info: {json.dumps(context.lead_fields, ensure_ascii=False)}")
    if context.extracted_facts:
        parts.append(f"Facts captured so far: {json.dumps(context.extracted_facts, ensure_ascii=False)}")
    parts.append(f"Allowed tools in this state: {', '.join(allowed_tools) or 'none'}")
    parts.append(system_prompt["output_contract"])

    return LLMMessage(role="system", content="\n".join(parts))


def build_message_history(context: ConversationContext) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    for turn in context.history:
        role = "assistant" if turn.speaker == "agent" else "user"
        messages.append(LLMMessage(role=role, content=turn.text))
    return messages


def build_messages(script: ScriptConfig, context: ConversationContext) -> list[LLMMessage]:
    return [build_system_message(script, context), *build_message_history(context)]

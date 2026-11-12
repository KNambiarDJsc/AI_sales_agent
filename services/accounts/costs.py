"""What a call costs, estimated from what it used (config/providers.yaml → pricing).

Every figure here is an ESTIMATE in rupees for showing in the console, except the phone
line's share when the provider reports the real charge for that call (Vobiz CDRs).
Per call we know: how long the line was up, how many characters the voice spoke, and
the brain's token counts (recorded per reply since 2026-11; typical figures before).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config.settings import get_settings
from services.accounts.registry import get_registry


@dataclass
class Stack:
    """Which provider does what right now."""
    telephony: str
    stt: str
    tts: str
    llm_model: str
    llm_priority: bool = False


def current_stack() -> Stack:
    s = get_settings()
    speech = "sarvam" if s.sarvam_api_key and s.ai_backend != "local" else "openai"
    return Stack(telephony=s.telephony_provider, stt=speech, tts=speech, llm_model=s.openai_llm_model,
                 llm_priority=bool(s.openai_llm_service_tier))


def _inr(amount: float, currency: str, usd_inr: float) -> float:
    return amount * usd_inr if currency == "USD" else amount


def _llm_rates(model: str) -> dict:
    table = get_registry().pricing.get("llm") or {}
    if model in table:
        return table[model]
    for name, rates in table.items():  # dated snapshots: "gpt-4.1-mini-2025-04-14"
        if model.startswith(name):
            return rates
    return next(iter(table.values()), {"input": 0.0, "cached_input": 0.0, "output": 0.0})


def call_cost(
    *, connected: bool, line_seconds: int, agent_chars: int, llm_turns: int,
    prompt_tokens: int = 0, cached_tokens: int = 0, completion_tokens: int = 0, llm_model: str = "",
    telephony_actual: float | None = None, stack: Stack | None = None, usd_inr: float | None = None,
) -> dict:
    """{"total", "parts": {phone, listening, voice, brain}, "by_provider": {...},
    "phone_is_actual"} — all in rupees."""
    registry = get_registry()
    pricing = registry.pricing
    stack = stack or current_stack()
    rate = usd_inr or registry.usd_inr

    tel = (pricing.get("telephony") or {}).get(stack.telephony) or {}
    if telephony_actual is not None:
        phone = telephony_actual
    elif not connected:
        phone = _inr(float(tel.get("not_connected_fee") or 0), tel.get("currency", "INR"), rate) if line_seconds else 0.0
    else:
        step = int(tel.get("increment_seconds") or 60)
        billed = math.ceil(max(line_seconds, 1) / step) * step
        phone = _inr(billed / 60 * float(tel.get("per_minute") or 0), tel.get("currency", "INR"), rate)

    listening = voice = brain = 0.0
    if connected:
        stt = (pricing.get("stt") or {}).get(stack.stt) or {}
        listening = _inr(line_seconds / 60 * float(stt.get("per_minute") or 0), stt.get("currency", "INR"), rate)
        tts = (pricing.get("tts") or {}).get(stack.tts) or {}
        voice = _inr(agent_chars / 1000 * float(tts.get("per_1k_chars") or 0), tts.get("currency", "INR"), rate)
        if not prompt_tokens and llm_turns:
            typical = pricing.get("llm_typical_turn") or {}
            prompt_tokens = llm_turns * int(typical.get("prompt_tokens") or 0)
            cached_tokens = llm_turns * int(typical.get("cached_tokens") or 0)
            completion_tokens = llm_turns * int(typical.get("completion_tokens") or 0)
        rates = _llm_rates(llm_model or stack.llm_model)
        fresh = max(prompt_tokens - cached_tokens, 0)
        usd = (fresh * float(rates.get("input") or 0) + cached_tokens * float(rates.get("cached_input") or 0)
               + completion_tokens * float(rates.get("output") or 0)) / 1_000_000
        if stack.llm_priority:
            usd *= float(pricing.get("llm_priority_multiplier") or 1)
        brain = usd * rate

    by_provider: dict[str, float] = {}
    for provider, amount in ((stack.telephony, phone), (stack.stt, listening), (stack.tts, voice), ("openai", brain)):
        by_provider[provider] = by_provider.get(provider, 0.0) + amount
    return {
        "total": round(phone + listening + voice + brain, 2),
        "parts": {"phone": round(phone, 2), "listening": round(listening, 2), "voice": round(voice, 2),
                  "brain": round(brain, 2)},
        "by_provider": {k: round(v, 4) for k, v in by_provider.items()},
        "phone_is_actual": telephony_actual is not None,
    }


def typical_call_cost(seconds: int = 120, stack: Stack | None = None, usd_inr: float | None = None) -> dict:
    """What a call of `seconds` costs with the current setup: the agent speaks about
    half the time (~14 characters a second) and replies about every 12 seconds."""
    return call_cost(connected=True, line_seconds=seconds, agent_chars=int(seconds * 0.5 * 14),
                     llm_turns=max(1, round(seconds / 12)), stack=stack, usd_inr=usd_inr)

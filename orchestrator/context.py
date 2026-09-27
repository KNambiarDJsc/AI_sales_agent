"""Conversation context passed between turns (Section 10: "agent memory within the
call"). Kept as a plain dataclass so it can be trivially persisted/rehydrated from the
Conversation/Turn rows if a call needs to resume after a transient failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConversationTurn:
    speaker: str  # "agent" | "customer"
    text: str
    state: str


@dataclass
class ConversationContext:
    conversation_id: str
    campaign_id: str
    lead_id: str
    script_id: str
    script_version: int
    current_state: str
    history: list[ConversationTurn] = field(default_factory=list)
    extracted_facts: dict[str, Any] = field(default_factory=dict)
    lead_fields: dict[str, Any] = field(default_factory=dict)  # contact_name, business_name, extra columns
    campaign_prompt: dict[str, Any] = field(default_factory=dict)  # loaded campaign_prompt yaml

    def append_turn(self, speaker: str, text: str, state: str) -> None:
        self.history.append(ConversationTurn(speaker=speaker, text=text, state=state))

    def merge_facts(self, new_facts: dict[str, Any]) -> None:
        """Facts accumulate across turns; later turns can overwrite earlier ones but
        nothing is silently dropped (callers can still see prior values in Turn rows)."""
        self.extracted_facts.update({k: v for k, v in new_facts.items() if v is not None})

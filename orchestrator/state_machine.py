"""Config-driven state machine (Section 10/12).

Loads a versioned script YAML (config/scripts/*.yaml) and answers the questions the
orchestration layer is responsible for: what transitions are allowed from here, what
tools are allowed here, what the fallback line is, and whether a retry limit has been
hit. It never generates natural language itself — that's the LLM's job, checked
against this.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from config.settings import SCRIPTS_DIR

# States reachable from anywhere as a safety valve, regardless of the script's own
# transitions_on — DNC and a hard end must never be a dead end the script forgot to
# wire up (Section 10: DNC enforcement is not optional/LLM-controlled).
GLOBAL_SAFETY_STATES = {"DO_NOT_CALL", "END"}


class StateConfig(BaseModel):
    objective: str = ""
    mandatory_questions: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    fallback_response: str = ""
    transitions_on: dict[str, str] = Field(default_factory=dict)


class ScriptConfig(BaseModel):
    script_id: str
    version: int
    language: str = "en"
    states: dict[str, StateConfig]
    max_call_duration_seconds: int = 420
    retry_limits: dict[str, int] = Field(default_factory=lambda: {"per_question": 2, "per_state": 3})
    qualification_ref: str | None = None

    def allowed_next_states(self, current_state: str) -> set[str]:
        state = self.states.get(current_state)
        allowed = {current_state} | GLOBAL_SAFETY_STATES
        if state is not None:
            allowed |= set(state.transitions_on.values())
        return allowed

    def allowed_tools(self, state_name: str) -> set[str]:
        state = self.states.get(state_name)
        tools = set(state.allowed_tools) if state else set()
        # mark_dnc and end_call are always available as a safety valve.
        return tools | {"mark_dnc", "end_call"}

    def fallback_for(self, state_name: str) -> str:
        state = self.states.get(state_name)
        return state.fallback_response if state else "Sorry, could you repeat that?"


def load_script(path: str | Path) -> ScriptConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return ScriptConfig(
        script_id=raw["script_id"],
        version=raw["version"],
        language=raw.get("language", "en"),
        states={name: StateConfig(**cfg) for name, cfg in raw["states"].items()},
        max_call_duration_seconds=raw.get("max_call_duration_seconds", 420),
        retry_limits=raw.get("retry_limits", {"per_question": 2, "per_state": 3}),
        qualification_ref=raw.get("qualification_ref"),
    )


def load_script_by_id(script_id: str) -> ScriptConfig:
    """Convention: config/scripts/<script_id>.yaml. Multi-version scripts should use
    config/scripts/<script_id>.v<version>.yaml and be resolved via the script_version
    DB table instead — this helper covers the common single-active-version case."""
    return load_script(SCRIPTS_DIR / f"{script_id}.yaml")


class StateMachine:
    """Per-conversation runtime state: current state + retry counters. Stateless
    beyond that — persistence of turns/transitions is the caller's job (Conversation/
    Turn rows), this class just enforces the rules."""

    def __init__(self, script: ScriptConfig, current_state: str = "INTRO"):
        self.script = script
        self.current_state = current_state
        self._state_retry_counts: dict[str, int] = {}

    def is_transition_allowed(self, to_state: str) -> bool:
        return to_state in self.script.allowed_next_states(self.current_state)

    def allowed_tools(self) -> set[str]:
        return self.script.allowed_tools(self.current_state)

    def fallback_response(self) -> str:
        return self.script.fallback_for(self.current_state)

    def retry_limit_exceeded(self) -> bool:
        limit = self.script.retry_limits.get("per_state", 3)
        return self._state_retry_counts.get(self.current_state, 0) >= limit

    def record_retry(self) -> None:
        self._state_retry_counts[self.current_state] = self._state_retry_counts.get(self.current_state, 0) + 1

    def transition_to(self, to_state: str) -> None:
        if to_state != self.current_state:
            self.current_state = to_state
            self._state_retry_counts.setdefault(to_state, 0)

    def is_terminal(self) -> bool:
        return self.current_state == "END"

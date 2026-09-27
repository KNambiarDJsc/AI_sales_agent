"""Speculative early speech extraction from a streaming LLM response.

Why this exists: the non-streaming path (`llm.propose()`) waits for the *entire*
structured-output JSON object — `state`, `speech`, `intent`, `extracted_facts`,
`tool_call`, `end_call` — before TTS can start on so much as the first word. Most of
that latency buys nothing: `extracted_facts` and `tool_call` are only needed after
speech starts playing, never before. This module lets the caller start speaking as
soon as the `speech` field itself has finished streaming, without waiting for the rest
of the object.

Safety is what makes this OK to ship, not just fast:

- We only ever act on a substring of the exact same `raw_text` that the normal,
  unchanged `orchestrator/validator.py:validate_llm_response()` will later fully parse
  and validate. We never trust a value this extractor produces on its own.
- We gate speech extraction on the `state` field having already been checked against
  `state_machine.is_transition_allowed()` — the one thing that, if wrong, would make
  the spoken text incoherent with the conversation's future state (Section 10). If
  state is invalid, we never speak early; the caller falls through to the normal fully
  validated (fallback) response instead.
- If parsing is ever ambiguous (field not found yet, buffer looks malformed), this
  returns "not ready" rather than guessing — the caller always has a safe fallback:
  wait for the full stream and run it through the same validator as the non-streaming
  path.
- A disallowed *tool* is not gated here at all, deliberately: per
  `orchestrator/validator.py`, an invalid tool call no longer invalidates the spoken
  response, so there is nothing to protect against by waiting for it.
"""
from __future__ import annotations

import json

from orchestrator.state_machine import StateMachine


def extract_json_string_field(buffer: str, field_name: str) -> str | None:
    """Return the fully-decoded value of a top-level JSON string field once its
    closing quote has appeared in `buffer`, else None (not enough data yet, or the
    field genuinely isn't a simple string — never raises on malformed input)."""
    marker = f'"{field_name}"'
    start = buffer.find(marker)
    if start == -1:
        return None

    pos = start + len(marker)
    while pos < len(buffer) and buffer[pos] in ": \t\n\r":
        pos += 1
    if pos >= len(buffer) or buffer[pos] != '"':
        return None  # value hasn't started yet (or isn't a string at all)

    pos += 1
    value_start = pos
    escaped = False
    while pos < len(buffer):
        ch = buffer[pos]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            raw = buffer[value_start:pos]
            try:
                return json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                return None  # shouldn't happen for well-formed escapes, but never guess
        pos += 1
    return None  # closing quote not seen yet


class SpeculativeTurnExtractor:
    """Feed it raw text deltas as they stream in; once `.speech_ready` flips True,
    `.speech` holds the value safe to start synthesizing. Call `feed()` with each new
    delta in order — it maintains the running buffer internally."""

    def __init__(self, state_machine: StateMachine):
        self._state_machine = state_machine
        self._buffer = ""
        self._state_checked = False
        self._state_ok = False
        self.state: str | None = None
        self.speech: str | None = None
        self.speech_ready = False

    def feed(self, delta: str) -> None:
        if self.speech_ready or not delta:
            return
        self._buffer += delta

        if not self._state_checked:
            state_value = extract_json_string_field(self._buffer, "state")
            if state_value is not None:
                self._state_checked = True
                self._state_ok = self._state_machine.is_transition_allowed(state_value)
                if self._state_ok:
                    self.state = state_value

        if self._state_checked and self._state_ok:
            speech_value = extract_json_string_field(self._buffer, "speech")
            if speech_value is not None:
                self.speech = speech_value
                self.speech_ready = True

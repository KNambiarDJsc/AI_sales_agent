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
import re
from collections.abc import Callable

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


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def extract_partial_json_string(buffer: str, field_name: str) -> tuple[str | None, bool]:
    """Decode as much of a top-level JSON string field as has arrived so far.

    Returns (text_so_far, closed). text_so_far is None if the value hasn't started.
    Never guesses: an escape sequence cut off at the end of the buffer is left out
    until the rest of it arrives."""
    marker = f'"{field_name}"'
    start = buffer.find(marker)
    if start == -1:
        return None, False
    pos = start + len(marker)
    while pos < len(buffer) and buffer[pos] in ": \t\n\r":
        pos += 1
    if pos >= len(buffer) or buffer[pos] != '"':
        return None, False
    pos += 1
    out: list[str] = []
    while pos < len(buffer):
        ch = buffer[pos]
        if ch == '"':
            return "".join(out), True
        if ch == "\\":
            if pos + 1 >= len(buffer):
                break  # incomplete escape — wait for more
            nxt = buffer[pos + 1]
            if nxt == "u":
                hex_digits = buffer[pos + 2 : pos + 6]
                if len(hex_digits) < 4:
                    break
                try:
                    out.append(chr(int(hex_digits, 16)))
                except ValueError:
                    return None, False
                pos += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            pos += 2
            continue
        out.append(ch)
        pos += 1
    return "".join(out), False


class PhraseSplitter:
    """Cuts a growing reply into speakable phrases as soon as each one is complete,
    so text-to-speech can start on the first phrase while the LLM is still writing the
    rest. The first phrase may be short (≥ `first_min_words`, ending at a comma or
    sentence end) so audio starts early; later phrases end at a sentence end, or at a
    comma once they're long enough to sound natural on their own."""

    _SENTENCE_END = re.compile(r"[.!?…](?=\s)")
    _CLAUSE_END = re.compile(r"[,;:—–](?=\s)")

    def __init__(self, first_min_words: int = 3, min_words: int = 6):
        self.first_min_words = first_min_words
        self.min_words = min_words
        self._pos = 0
        self._emitted = 0

    def feed(self, text: str, final: bool) -> list[str]:
        phrases: list[str] = []
        while True:
            rest = text[self._pos :]
            cut = self._find_cut(rest)
            if cut is None:
                break
            phrase = rest[:cut].strip()
            self._pos += cut
            if phrase:
                phrases.append(phrase)
                self._emitted += 1
        if final:
            remainder = text[self._pos :].strip()
            self._pos = len(text)
            if remainder:
                phrases.append(remainder)
                self._emitted += 1
        return phrases

    _PAUSE_WORDS = {"and", "but", "or", "so", "because", "to", "on", "for", "with", "about", "if", "when", "while", "that"}
    _FIRST_PHRASE_SOFT_LIMIT = 8  # words, for an opening sentence with no comma yet

    def _find_cut(self, rest: str) -> int | None:
        needed = self.first_min_words if self._emitted == 0 else self.min_words
        m = self._SENTENCE_END.search(rest)
        if m and rest[: m.end()].strip():
            return m.end()  # a complete sentence is always worth speaking, however short
        for m in self._CLAUSE_END.finditer(rest):
            if len(rest[: m.end()].split()) >= needed:
                return m.end()
        if self._emitted == 0:
            return self._soft_cut(rest)
        return None

    def _soft_cut(self, rest: str) -> int | None:
        """A long opening with no punctuation yet ("We can help you get your products
        listed on Amazon and…"): release it before a natural pause word once ~8 words
        are written, so the first audio doesn't wait for the full stop. Only complete
        words count — the last token may still be mid-word."""
        tokens = list(re.finditer(r"\S+", rest))
        if rest and not rest[-1].isspace():
            tokens = tokens[:-1]  # possibly incomplete
        if len(tokens) <= self._FIRST_PHRASE_SOFT_LIMIT:
            return None
        for i in range(4, self._FIRST_PHRASE_SOFT_LIMIT + 1):
            if tokens[i].group().lower().strip(",;:") in self._PAUSE_WORDS:
                return tokens[i].start()
        return None


class SpeculativeTurnExtractor:
    """Feed it raw text deltas as they stream in; once `.speech_ready` flips True,
    `.speech` holds the value safe to start synthesizing. Call `feed()` with each new
    delta in order — it maintains the running buffer internally."""

    def __init__(self, state_machine: StateMachine, may_speak: Callable[[str], bool] | None = None):
        """`may_speak(state)`: the caller's own veto on speaking early for a state the
        transition check allows but the caller will refuse later (the engine refuses a
        local model's DO_NOT_CALL). Vetoed replies wait for full validation."""
        self._state_machine = state_machine
        self._may_speak = may_speak
        self._buffer = ""
        self._state_checked = False
        self._state_ok = False
        self.state: str | None = None
        self.speech: str | None = None
        self.speech_ready = False
        # The speech decoded so far, while it is still being written — only ever set
        # once `state` has passed the same check as the complete-speech path.
        self.partial_speech: str | None = None

    def feed(self, delta: str) -> None:
        if self.speech_ready or not delta:
            return
        self._buffer += delta

        if not self._state_checked:
            state_value = extract_json_string_field(self._buffer, "state")
            if state_value is not None:
                self._state_checked = True
                self._state_ok = self._state_machine.is_transition_allowed(state_value) and (
                    self._may_speak is None or self._may_speak(state_value)
                )
                if self._state_ok:
                    self.state = state_value

        if self._state_checked and self._state_ok:
            partial, closed = extract_partial_json_string(self._buffer, "speech")
            if partial is not None:
                self.partial_speech = partial
            if closed:
                self.speech = partial
                self.speech_ready = True

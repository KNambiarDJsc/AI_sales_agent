"""Endpointing: "has the customer finished speaking?" — separate from VAD's "is speech
happening right now?" (Section 17). Thresholds are configurable (config/settings.py)
rather than hard-coded, per the architecture rule. Provider-native endpointing (e.g. if
a future provider offers it) is an acceptable substitute per Section 17 — this class is
the fallback/default implementation when we're doing our own VAD.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config.settings import get_settings


class TurnEvent(str, Enum):
    SPEECH_STARTED = "speech_started"
    TURN_ENDED = "turn_ended"
    MAX_TURN_REACHED = "max_turn_reached"


@dataclass
class TurnTakerConfig:
    speech_debounce_ms: int
    min_speech_segment_ms: int
    end_of_turn_ms: int
    max_turn_ms: int

    @classmethod
    def from_settings(cls) -> "TurnTakerConfig":
        s = get_settings()
        return cls(
            speech_debounce_ms=s.vad_speech_debounce_ms,
            min_speech_segment_ms=s.vad_min_speech_segment_ms,
            end_of_turn_ms=s.vad_end_of_turn_ms,
            max_turn_ms=s.vad_max_turn_seconds * 1000,
        )


class TurnTaker:
    """Feed it VAD decisions one frame at a time; it tells you when a real utterance
    started and when the customer's turn is over (either by silence or by hitting the
    max-turn cap)."""

    def __init__(self, config: TurnTakerConfig | None = None):
        self._config = config or TurnTakerConfig.from_settings()
        self._reset()

    def _reset(self) -> None:
        self._consecutive_speech_ms = 0
        self._consecutive_silence_ms = 0
        self._total_speech_ms = 0
        self._turn_elapsed_ms = 0
        self._speech_started = False

    @property
    def speech_in_progress(self) -> bool:
        return self._speech_started

    def feed(self, is_speech: bool, frame_ms: int) -> TurnEvent | None:
        if is_speech:
            self._consecutive_speech_ms += frame_ms
            self._consecutive_silence_ms = 0

            if not self._speech_started and self._consecutive_speech_ms >= self._config.speech_debounce_ms:
                self._speech_started = True
                self._total_speech_ms = self._consecutive_speech_ms
                self._turn_elapsed_ms = self._consecutive_speech_ms
                return TurnEvent.SPEECH_STARTED
            if self._speech_started:
                self._total_speech_ms += frame_ms
        else:
            self._consecutive_speech_ms = 0
            self._consecutive_silence_ms += frame_ms
            if self._speech_started and self._consecutive_silence_ms >= self._config.end_of_turn_ms:
                if self._total_speech_ms >= self._config.min_speech_segment_ms:
                    self._reset()
                    return TurnEvent.TURN_ENDED
                # Silence after too-short a blip of "speech" — treat as noise, keep waiting.
                self._reset()
                return None

        if self._speech_started:
            self._turn_elapsed_ms += frame_ms
            if self._turn_elapsed_ms >= self._config.max_turn_ms:
                self._reset()
                return TurnEvent.MAX_TURN_REACHED

        return None

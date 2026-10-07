"""TTSProvider abstraction (Section 8)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class TTSProvider(ABC):
    # PCM16 mono rate of the audio `synthesize_stream` yields. Callers resample from
    # this (VoiceSession → 8 kHz telephony; the browser demo plays it as-is).
    sample_rate_hz: int = 24000
    # "openai" | "local" — which backend produced the most recent audio (shown in the
    # demo's timing line and logs). Fallback providers update it per request.
    backend: str = "openai"

    @abstractmethod
    def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield raw PCM16 audio chunks as they become available. Callers (the voice
        session's barge-in logic, Section 18) must be able to stop iterating at any
        point — that's the cancellation mechanism; also call cancel() to signal the
        provider side to stop generating further chunks."""

    @abstractmethod
    async def cancel(self) -> None:
        """Stop any in-flight synthesis. Safe to call even if nothing is in flight."""

    @abstractmethod
    async def close(self) -> None:
        ...

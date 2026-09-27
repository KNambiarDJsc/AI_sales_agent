"""TTSProvider abstraction (Section 8)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class TTSProvider(ABC):
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

"""STTProvider abstraction (Section 8).

Interface shape follows the spec exactly: start_stream / send_audio / receive_partial /
receive_final / close. A provider that can't produce true interim partials (see
speech/stt/openai.py) just returns None from receive_partial() — callers must treat
that as "no partial available yet," not an error.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TranscriptResult:
    text: str
    is_final: bool
    confidence: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None


class STTStream(ABC):
    """One streaming transcription session, scoped to a single call/turn-taking
    session. Audio in is PCM16 16kHz mono (see voice/audio/processing.py for the
    mu-law 8kHz -> PCM16 16kHz path) unless a provider says otherwise."""

    @abstractmethod
    async def send_audio(self, pcm16_chunk: bytes) -> None:
        ...

    @abstractmethod
    async def receive_partial(self) -> TranscriptResult | None:
        """Non-blocking-ish: return the latest partial transcript if the provider
        supports interim results, else None."""

    @abstractmethod
    async def receive_final(self) -> TranscriptResult | None:
        """Return a final transcript once available (e.g. after endpointing signals
        end-of-turn and the buffered audio has been transcribed), else None."""

    @abstractmethod
    async def close(self) -> None:
        ...


class STTProvider(ABC):
    @abstractmethod
    async def start_stream(self) -> STTStream:
        ...

"""TelephonyProvider abstraction (Section 6).

Every provider (Twilio, Exotel, Freejun, ...) implements this interface. Application
code never imports a concrete provider directly outside of `telephony/factory.py` —
that's what keeps provider-specific quirks isolated and lets the call worker/voice
session stay provider-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CallStatus(str, Enum):
    QUEUED = "queued"
    DIALING = "dialing"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


@dataclass
class OutboundCallRequest:
    to_number: str  # E.164
    from_number: str  # E.164, provider's caller ID
    campaign_id: str
    lead_id: str
    attempt_number: int
    # Where the provider should stream/callback for realtime media + call events.
    media_websocket_url: str
    status_callback_url: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundCallResult:
    provider_call_id: str
    status: CallStatus
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallEvent:
    provider_call_id: str
    event_type: str  # provider-native event name, normalized status lives in `.status`
    status: CallStatus
    raw: dict[str, Any] = field(default_factory=dict)


class TelephonyProvider(ABC):
    """Methods cover what the application actually needs (Section 6) — not a full
    wrapper of the provider's API surface."""

    name: str
    # The encoding each provider's media WebSocket actually sends/expects on the audio
    # leg, confirmed per-provider against real docs/live calls — never assumed. Twilio
    # Media Streams is mu-law 8kHz (the long-standing telephony default); Exotel's
    # Voicebot/Stream applet is raw linear PCM16 8kHz (confirmed against
    # developer.exotel.com/docs/agentstream/stream-voicebot-applet, and against a real
    # call: decoding Exotel's own PCM16 bytes as mu-law produced VAD-triggering noise
    # with no intelligible audio in either direction — see STATUS.md).
    # `voice/session/session.py`'s VoiceSession reads this to pick the right codec path
    # instead of hardcoding one provider's assumption for every provider.
    audio_encoding: str = "mulaw"  # "mulaw" | "pcm16"

    @abstractmethod
    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        """Initiate an outbound call. Must be safe to call at most once per
        (campaign_id, lead_id, attempt_number) — callers enforce idempotency via the DB
        unique constraint on call_attempt; this method should not itself retry."""

    @abstractmethod
    async def hangup_call(self, provider_call_id: str) -> None:
        ...

    @abstractmethod
    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        ...

    @abstractmethod
    def validate_webhook(self, headers: dict[str, str], body: bytes, url: str) -> bool:
        """Verify the inbound webhook/event actually came from this provider
        (signature check). Must be called before trusting any webhook payload —
        never process an unverified call event (Section 22)."""

    @abstractmethod
    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        """Normalize a provider-native status callback payload into a CallEvent."""

    # --- Realtime media control (used once the call is connected) ---

    @abstractmethod
    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        """Instruct the provider to open/attach a bidirectional media stream to the
        given WebSocket for this call, if the provider requires an explicit step
        beyond what create_outbound_call already configured."""

    @abstractmethod
    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        """Send an outbound audio frame (already in the provider's expected encoding,
        e.g. mu-law 8kHz base64-framed) down the media stream."""

    @abstractmethod
    async def clear_audio(self, provider_call_id: str) -> None:
        """Clear any buffered outbound audio on the provider side — used on barge-in
        (Section 18) so the customer doesn't keep hearing stale TTS after they've
        started speaking."""

    # --- Stream registration (Twilio/Exotel-style media WebSockets) ---
    #
    # Not abstract: providers whose media transport works differently (or that don't
    # support realtime media at all, like the Freejun stub) don't need to implement
    # these. The WebSocket handler that accepts the provider's media connection calls
    # register_stream() once it knows the provider's stream/call identifiers; send_audio
    # and clear_audio above then address that registered connection.

    def register_stream(self, provider_call_id: str, stream_id: str, send_text) -> None:
        raise NotImplementedError(f"{self.name} does not support stream registration")

    def unregister_stream(self, provider_call_id: str) -> None:
        raise NotImplementedError(f"{self.name} does not support stream registration")

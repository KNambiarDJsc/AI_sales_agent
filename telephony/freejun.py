"""Freejun adapter — INTENTIONALLY UNIMPLEMENTED.

The client mentioned Freejun (Bangalore-based, Indian-looking number) as a candidate
telephony provider, but as of writing there is no verified public API documentation
for Freejun's call-control or media-streaming APIs available to this project.

Per the architecture rule (Section 6/7/29): do not invent undocumented provider APIs.
This class exists so `telephony/factory.py` and the rest of the app can already depend
on the `TelephonyProvider` interface for Freejun — every method below raises
`NotImplementedError` with a pointer to what's needed.

TO IMPLEMENT THIS ADAPTER, obtain from the client or Freejun directly:
  1. REST API base URL + authentication scheme (API key header? OAuth? basic auth?).
  2. The outbound call creation endpoint + required/optional parameters.
  3. Whether Freejun supports real-time bidirectional media streaming (WebSocket/RTP)
     at all — if not, this provider cannot support the realtime voice agent and can
     only be used for simple forward-to-human calling, which is out of scope here.
  4. The call-status webhook payload shape and how to verify its authenticity.
  5. Number provisioning / KYC process and turnaround time for a Bangalore number.

Once those are in hand, implement this the same shape as telephony/twilio.py or
telephony/exotel.py — do not guess parameter names in the meantime.
"""
from __future__ import annotations

from typing import Any

from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider

_NOT_IMPLEMENTED = (
    "FreejunProvider is a stub: no verified Freejun API documentation is available yet. "
    "See telephony/freejun.py module docstring for what's needed before implementing this."
)


class FreejunProvider(TelephonyProvider):
    name = "freejun"

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def hangup_call(self, provider_call_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def validate_webhook(self, headers: dict[str, str], body: bytes, url: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def clear_audio(self, provider_call_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

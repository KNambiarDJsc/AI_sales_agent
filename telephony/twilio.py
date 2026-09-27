"""Twilio adapter (Programmable Voice + Media Streams).

Reference: https://www.twilio.com/docs/voice/api/call-resource,
https://www.twilio.com/docs/voice/media-streams.

Twilio's bidirectional Media Streams work by us returning TwiML with
`<Connect><Stream>` when Twilio requests the call's instructions (the `url` we pass
to `calls.create`) — Twilio then opens the WebSocket to `media_websocket_url` itself
and starts sending/receiving audio frames as JSON messages over that same socket.
That means `send_audio`/`clear_audio` here don't go through Twilio's REST API — they
write to the WebSocket connection that `apps/api` accepted for this call. The voice
session registers that connection via `register_stream()` when Twilio connects.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Awaitable, Callable

from twilio.request_validator import RequestValidator
from twilio.rest import Client

from config.settings import get_settings
from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider

_STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.DIALING,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELED,
}

SendFn = Callable[[str], Awaitable[None]]


class TwilioProvider(TelephonyProvider):
    name = "twilio"

    def __init__(self) -> None:
        settings = get_settings()
        self._account_sid = settings.twilio_account_sid
        self._auth_token = settings.twilio_auth_token
        self._from_number = settings.twilio_from_number
        self._client = Client(self._account_sid, self._auth_token) if self._account_sid else None
        self._validator = RequestValidator(self._auth_token) if self._auth_token else None
        # provider_call_id -> raw-text send function for the accepted media WebSocket,
        # and the Twilio streamSid needed to address `clear`/`media` events.
        self._streams: dict[str, tuple[SendFn, str]] = {}

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Twilio not configured: set TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN")
        return self._client

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        client = self._require_client()
        twiml = (
            "<Response><Connect>"
            f'<Stream url="{request.media_websocket_url}" track="both_tracks" />'
            "</Connect></Response>"
        )
        call = client.calls.create(
            to=request.to_number,
            from_=request.from_number or self._from_number,
            twiml=twiml,
            status_callback=request.status_callback_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
        return OutboundCallResult(
            provider_call_id=call.sid,
            status=_STATUS_MAP.get(call.status, CallStatus.UNKNOWN),
            raw={"sid": call.sid, "status": call.status},
        )

    async def hangup_call(self, provider_call_id: str) -> None:
        client = self._require_client()
        client.calls(provider_call_id).update(status="completed")

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        client = self._require_client()
        call = client.calls(provider_call_id).fetch()
        return _STATUS_MAP.get(call.status, CallStatus.UNKNOWN)

    def validate_webhook(self, headers: dict[str, str], body: bytes, url: str) -> bool:
        if self._validator is None:
            return False
        signature = headers.get("X-Twilio-Signature", "")
        # Twilio signs the URL-encoded form params, not raw body, for webhook requests.
        from urllib.parse import parse_qsl

        params = dict(parse_qsl(body.decode("utf-8")))
        return self._validator.validate(url, params, signature)

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        call_sid = payload.get("CallSid", "")
        twilio_status = payload.get("CallStatus", "")
        return CallEvent(
            provider_call_id=call_sid,
            event_type=twilio_status,
            status=_STATUS_MAP.get(twilio_status, CallStatus.UNKNOWN),
            raw=payload,
        )

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        # No-op for Twilio: the <Connect><Stream> in the TwiML returned at call setup
        # already tells Twilio to open the media WebSocket itself. This method exists
        # so other providers (which do require an explicit connect step) share the
        # interface.
        return None

    def register_stream(self, provider_call_id: str, stream_sid: str, send_text: SendFn) -> None:
        """Called by the WebSocket handler once Twilio's `start` event arrives, so
        send_audio/clear_audio below can address the right stream."""
        self._streams[provider_call_id] = (send_text, stream_sid)

    def unregister_stream(self, provider_call_id: str) -> None:
        self._streams.pop(provider_call_id, None)

    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        entry = self._streams.get(provider_call_id)
        if entry is None:
            raise RuntimeError(f"No registered media stream for call {provider_call_id}")
        send_text, stream_sid = entry
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(audio_chunk).decode("ascii")},
        }
        await send_text(json.dumps(message))

    async def clear_audio(self, provider_call_id: str) -> None:
        entry = self._streams.get(provider_call_id)
        if entry is None:
            return
        send_text, stream_sid = entry
        await send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))

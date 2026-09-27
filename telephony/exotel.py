"""Exotel adapter.

Call control uses Exotel's documented Voice "Connect" API:
https://developer.exotel.com/api/make-a-call-api (Calls/connect.json).

Realtime media uses Exotel's Voice Streaming / AgentStream, which is modeled on the
same start/media/stop WebSocket event shape as Twilio Media Streams:
https://developer.exotel.com/api/streaming-api-guide (AgentStream / StreamKit).

IMPORTANT — verify before going live: the exact way a call gets routed into the
streaming applet is configured per-account in Exotel's App Bazaar (a "Voicebot"/
"Stream" applet pointing at our WebSocket URL) or via StreamKit, not purely through
REST parameters. `create_outbound_call` below assumes the campaign's configured
`exotel_app_id`/flow already has that applet wired to `media_websocket_url`'s host —
confirm the exact flow configuration with Exotel support or the client's existing
Exotel account before the first real call. Do not extend this file with additional
guessed endpoints; ask for exact API docs/credentials from the client if something
beyond Connect + Streaming is needed (Section 6/29).
"""
from __future__ import annotations

import base64
import json
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx

from config.settings import get_settings
from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider

_STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "in-progress": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELED,
    "ringing": CallStatus.RINGING,
}

SendFn = Callable[[str], Awaitable[None]]


class ExotelProvider(TelephonyProvider):
    name = "exotel"

    def __init__(self) -> None:
        settings = get_settings()
        self._sid = settings.exotel_sid
        self._api_key = settings.exotel_api_key
        self._api_token = settings.exotel_api_token
        self._subdomain = settings.exotel_subdomain
        self._caller_id = settings.exotel_caller_id
        self._app_id = settings.exotel_app_id
        self._streams: dict[str, tuple[SendFn, str]] = {}

    @property
    def _base_url(self) -> str:
        return f"https://{self._subdomain}/v1/Accounts/{self._sid}"

    def _auth(self) -> tuple[str, str]:
        if not (self._api_key and self._api_token):
            raise RuntimeError("Exotel not configured: set EXOTEL_API_KEY/EXOTEL_API_TOKEN")
        return (self._api_key, self._api_token)

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        params = {
            "From": request.to_number,  # Exotel's Connect API dials `From` first, then bridges to `To`/the flow
            "To": request.to_number,
            "CallerId": self._caller_id,
            "CallType": "trans",
            "StatusCallback": request.status_callback_url,
        }
        if self._app_id:
            params["Url"] = f"http://my.exotel.com/{self._sid}/exoml/start_voice/{self._app_id}"
        async with httpx.AsyncClient(auth=self._auth(), timeout=15.0) as client:
            resp = await client.post(f"{self._base_url}/Calls/connect.json", data=params)
            resp.raise_for_status()
            data = resp.json()
        call = data.get("Call", {})
        call_sid = call.get("Sid", "")
        status = call.get("Status", "")
        return OutboundCallResult(
            provider_call_id=call_sid,
            status=_STATUS_MAP.get(status, CallStatus.UNKNOWN),
            raw=call,
        )

    async def hangup_call(self, provider_call_id: str) -> None:
        async with httpx.AsyncClient(auth=self._auth(), timeout=15.0) as client:
            resp = await client.post(
                f"{self._base_url}/Calls/{provider_call_id}.json",
                data={"Status": "completed"},
            )
            resp.raise_for_status()

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        async with httpx.AsyncClient(auth=self._auth(), timeout=15.0) as client:
            resp = await client.get(f"{self._base_url}/Calls/{provider_call_id}.json")
            resp.raise_for_status()
            data = resp.json()
        status = data.get("Call", {}).get("Status", "")
        return _STATUS_MAP.get(status, CallStatus.UNKNOWN)

    def validate_webhook(self, headers: dict[str, str], body: bytes, url: str) -> bool:
        # Exotel does not sign status-callback webhooks with an HMAC the way Twilio
        # does. TODO(confirm with client's Exotel account): if IP allowlisting or a
        # shared-secret query param is configured, validate that here instead of
        # returning True unconditionally. Until then, treat this as a known gap — do
        # not process a callback whose call SID doesn't match an in-flight attempt.
        return True

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        call_sid = payload.get("CallSid", payload.get("Sid", ""))
        status = payload.get("Status", payload.get("CallStatus", ""))
        return CallEvent(
            provider_call_id=call_sid,
            event_type=status,
            status=_STATUS_MAP.get(status, CallStatus.UNKNOWN),
            raw=payload,
        )

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        # Handled by the App Bazaar / StreamKit flow configured against `self._app_id`
        # (see module docstring) — no separate REST call needed once that's wired.
        return None

    def register_stream(self, provider_call_id: str, stream_sid: str, send_text: SendFn) -> None:
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
            "stream_sid": stream_sid,
            "media": {"payload": base64.b64encode(audio_chunk).decode("ascii")},
        }
        await send_text(json.dumps(message))

    async def clear_audio(self, provider_call_id: str) -> None:
        entry = self._streams.get(provider_call_id)
        if entry is None:
            return
        send_text, stream_sid = entry
        await send_text(json.dumps({"event": "clear", "stream_sid": stream_sid}))

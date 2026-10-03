"""FreJun Teler adapter.

Call control and the media protocol are taken from FreJun's official Python SDK source
(github.com/frejun-tech/teler-py), not guessed:
- REST base URL `https://api.frejun.ai/api/v1`, auth via the `x-api-key` header.
- Outbound call: POST /voice/calls/initiate with from_number, to_number, flow_url,
  status_callback_url, record. The response's `id` is the provider call id.
- Hangup: POST /voice/calls/{id}/hangup.
- Teler fetches `flow_url` once the call connects; we answer with a stream action
  (`CallFlow.stream`) pointing at our media WebSocket for this attempt.
- Media frames on that WebSocket are JSON: inbound `{"type": "audio", "data":
  {"audio_b64": ...}}`, outbound `{"type": "audio", "audio_b64": ..., "chunk_id": n}`
  and `{"type": "clear"}` for barge-in (shape taken from the official Teler-to-model
  bridge references).

Still unverified against a live account and therefore intentionally NOT implemented:
the status-callback payload shape, its signature scheme, and the call-status lookup.
Those raise NotImplementedError rather than being guessed, so webhook events are
rejected rather than trusted (Section 22) until they are confirmed.

Inbound/outbound audio is linear PCM16 at 8kHz (FreJun documents L16/8000Hz streaming,
and the SDK's default stream sample_rate is "8k"); confirm on the first live call.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from config.settings import get_settings
from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider

FREJUN_DEFAULT_BASE_URL = "https://api.frejun.ai/api/v1"

SendFn = Any  # Callable[[str], Awaitable[None]] — typed loosely to match the other adapters' registry shape

_UNVERIFIED = (
    "FreJun {what} is not verified against a live account yet — see telephony/freejun.py "
    "module docstring. Not guessed, so not implemented."
)


class FreejunProvider(TelephonyProvider):
    name = "freejun"
    audio_encoding = "pcm16"

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.freejun_api_key
        self._base_url = settings.freejun_api_base_url or FREJUN_DEFAULT_BASE_URL
        self._caller_id = settings.freejun_caller_id
        self._streams: dict[str, tuple[SendFn, str]] = {}
        self._chunk_ids: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError("FreJun not configured: set FREEJUN_API_KEY")
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        from_number = request.from_number or self._caller_id
        if not from_number:
            raise RuntimeError("FreJun not configured: set FREEJUN_CALLER_ID (a Teler virtual number)")
        body = {
            "from_number": from_number,
            "to_number": request.to_number,
            "flow_url": _flow_url_for(request.media_websocket_url),
            "status_callback_url": request.status_callback_url,
            "record": False,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self._base_url}/voice/calls/initiate", headers=self._headers(), json=body)
            resp.raise_for_status()
            data = resp.json()
        call = data.get("data", data) if isinstance(data.get("data"), dict) else data
        return OutboundCallResult(provider_call_id=str(call.get("id", "")), status=CallStatus.QUEUED, raw=call)

    async def hangup_call(self, provider_call_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._base_url}/voice/calls/{provider_call_id}/hangup", headers=self._headers()
            )
            resp.raise_for_status()

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        raise NotImplementedError(_UNVERIFIED.format(what="call-status lookup"))

    def validate_webhook(self, headers: dict[str, str], body: bytes, url: str) -> bool:
        return False  # fail closed until the signature scheme is verified live

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        raise NotImplementedError(_UNVERIFIED.format(what="status-callback payload"))

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        return None  # the flow's stream action is what opens the media socket

    def register_stream(self, provider_call_id: str, stream_id: str, send_text: SendFn) -> None:
        self._streams[provider_call_id] = (send_text, stream_id)
        self._chunk_ids[provider_call_id] = 0

    def unregister_stream(self, provider_call_id: str) -> None:
        self._streams.pop(provider_call_id, None)
        self._chunk_ids.pop(provider_call_id, None)

    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        entry = self._streams.get(provider_call_id)
        if entry is None:
            raise RuntimeError(f"No registered media stream for call {provider_call_id}")
        send_text, _ = entry
        self._chunk_ids[provider_call_id] += 1
        message = {
            "type": "audio",
            "audio_b64": base64.b64encode(audio_chunk).decode("ascii"),
            "chunk_id": self._chunk_ids[provider_call_id],
        }
        await send_text(json.dumps(message))

    async def clear_audio(self, provider_call_id: str) -> None:
        entry = self._streams.get(provider_call_id)
        if entry is None:
            return
        send_text, _ = entry
        await send_text(json.dumps({"type": "clear"}))


def _flow_url_for(media_websocket_url: str) -> str:
    """Our media URL is `wss://<host>/media/freejun/<attempt_id>`; the flow endpoint for
    the same attempt is `https://<host>/freejun/flow/<attempt_id>`."""
    attempt_id = media_websocket_url.rstrip("/").rsplit("/", 1)[-1]
    host = media_websocket_url.split("://", 1)[-1].split("/", 1)[0]
    return f"https://{host}/freejun/flow/{attempt_id}"

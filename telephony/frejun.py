"""FreJun (Teler) adapter.

Everything here is taken from FreJun's official Teler documentation
(https://frejun.com/docs/teler/) and their official Python SDK
(github.com/frejun-tech/teler-py, `teler/resources/voice/*.py`, `teler/flows.py`) —
not guessed. Where the two could disagree, the SDK's request shapes were used.

How a FreJun call works, and where each piece lives in this codebase:

1. `create_outbound_call` → `POST {base}/voice/calls/initiate` with header
   `X-API-Key` and JSON `{from_number, to_number, flow_url, status_callback_url,
   record}`. The response envelope is `{"data": {"id": "cs_...", ...}}`; `id` is the
   Call Session id, which is what every later webhook/stream message calls `call_id`.
   `from_number` must be a Teler virtual number attached to a Voice App (docs:
   telephony/voice-apps, "Outbound calls still link to a Voice App").
2. When the callee answers, Teler POSTs `{call_id, account_id, from_number,
   to_number, direction}` to our `flow_url` and needs a Call Flow back within 5 s.
   That endpoint is `apps/api/routers/flows.py` (`/flow/frejun/{call_attempt_id}`),
   which answers with `build_stream_flow()` below — a `stream` action pointing Teler
   at our media WebSocket.
3. Teler connects to `ws_url` (`/media/frejun/{call_attempt_id}`,
   `apps/api/routers/media.py`). First message is `start` (call_id, stream_id,
   `data.encoding: "audio/l16"`, `data.sample_rate: 8000`), then a stream of
   `{"type": "audio", "data": {"audio_b64": ...}}` — base64 16-bit linear PCM, mono,
   8 kHz (the docs say `start` "currently always advertises 8000, even when the
   Stream flow is configured 16k"). That's `audio_encoding = "pcm16"` at
   `voice/audio/processing.TELEPHONY_SAMPLE_RATE_HZ`, the same path Exotel uses
   inside `VoiceSession`; only the JSON envelope differs, and that translation is
   `send_audio`/`clear_audio` here plus the receive loop in the media route.
4. Outbound audio: `{"type": "audio", "audio_b64": ..., "chunk_id": "<unique>"}`;
   barge-in: `{"type": "clear"}` (drops every queued chunk).
5. Status: Teler POSTs signed JSON webhooks (call.initiated/answered/completed/
   failed, stream.*) to `status_callback_url` → `/webhooks/frejun`. Verified with
   HMAC-SHA256 over `"<X-Teler-Timestamp>.<raw body>"` using the Voice App's secret
   (docs: webhooks/signing).
6. Hangup: `POST {base}/voice/calls/{call_id}/hangup` with a required
   `Idempotency-Key` header; 202 Accepted. A call that already ended answers 409
   (`call_not_live`), which is the outcome we wanted anyway.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from config.settings import get_settings
from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider

logger = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]

WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300  # docs: webhooks/signing, replay protection

# Call Session `state` (docs: telephony/call-resources).
_STATE_MAP = {
    "initiated": CallStatus.DIALING,
    "ringing": CallStatus.RINGING,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "failed": CallStatus.FAILED,
}

# Terminate `reason` on a failed call (docs: telephony/call-resources).
_FAILURE_REASON_MAP = {
    "no_answer": CallStatus.NO_ANSWER,
    "busy": CallStatus.BUSY,
    "canceled": CallStatus.CANCELED,
}

# Webhook event types (docs: webhooks/call-events). stream.*, recording.*, leg.* and
# call.transfer.* don't change the call's own status and map to UNKNOWN (ignored).
_EVENT_MAP = {
    "call.initiated": CallStatus.DIALING,
    "call.answered": CallStatus.IN_PROGRESS,
    "call.completed": CallStatus.COMPLETED,
    "call.failed": CallStatus.FAILED,
}


def build_stream_flow(ws_url: str) -> dict[str, Any]:
    """The Call Flow our flow endpoint returns (docs: telephony/call-flows, `stream`;
    same shape as the SDK's `CallFlow.stream`)."""
    settings = get_settings()
    return {
        "action": "stream",
        "ws_url": ws_url,
        "sample_rate": settings.frejun_sample_rate,
        "chunk_size": settings.frejun_chunk_size_ms,
        "record": settings.frejun_record,
    }


def verify_teler_signature(
    secret: str, raw_body: bytes, timestamp: str, signature: str, *, now: float | None = None
) -> bool:
    """docs: webhooks/signing — HMAC-SHA256(secret, "<timestamp>.<raw_body>") as bare
    hex, constant-time compared, rejecting anything older than 5 minutes."""
    if not (secret and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > WEBHOOK_SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


class FreJunAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"FreJun API error {status_code}: {message}")
        self.status_code = status_code


@dataclass
class _Stream:
    send_text: SendFn
    stream_id: str
    next_chunk_id: int = 1


class FreJunProvider(TelephonyProvider):
    name = "frejun"
    audio_encoding = "pcm16"  # audio/l16, 8 kHz mono — see module docstring

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.frejun_api_key
        self._base_url = settings.frejun_api_base_url.rstrip("/")
        self._from_number = settings.frejun_from_number
        self._secret = settings.frejun_secret
        self._public_base_url = settings.effective_public_base_url
        self._record = settings.frejun_record
        self._timeout = settings.frejun_http_timeout_seconds
        self._transport = transport  # tests inject httpx.MockTransport here
        self._streams: dict[str, _Stream] = {}

    # --- REST -----------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if not self._api_key:
            raise RuntimeError("FreJun not configured: set FREJUN_API_KEY")
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": self._api_key, "Accept": "application/json", "Content-Type": "application/json"},
            timeout=self._timeout,
            transport=self._transport,
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
            message = body.get("message") or body.get("detail") or resp.text
        except ValueError:
            message = resp.text
        raise FreJunAPIError(resp.status_code, str(message)[:300])

    @staticmethod
    def _unwrap(body: Any) -> dict[str, Any]:
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            return body["data"]
        return body if isinstance(body, dict) else {}

    def flow_url_for(self, call_attempt_id: str) -> str:
        return f"{self._public_base_url}/flow/frejun/{call_attempt_id}"

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        from_number = request.from_number or self._from_number
        if not from_number:
            raise RuntimeError("FreJun not configured: set FREJUN_FROM_NUMBER (a Teler number on a Voice App)")
        if not request.call_attempt_id:
            raise RuntimeError("FreJun calls need OutboundCallRequest.call_attempt_id to build the flow URL")
        if not self._public_base_url.startswith("https://"):
            raise RuntimeError("FreJun needs PUBLIC_BASE_URL set to this app's public https:// URL")

        payload = {
            "from_number": from_number,
            "to_number": request.to_number,
            "flow_url": self.flow_url_for(request.call_attempt_id),
            "status_callback_url": request.status_callback_url,
            "record": self._record,
        }
        async with self._client() as client:
            resp = await client.post("/voice/calls/initiate", json=payload)
        self._raise_for_status(resp)
        data = self._unwrap(resp.json())
        call_id = str(data.get("id") or data.get("call_id") or "")
        if not call_id:
            raise FreJunAPIError(resp.status_code, "initiate response had no call id")
        logger.info("frejun_call_initiated", extra={"call_id": call_id, "call_attempt_id": request.call_attempt_id})
        return OutboundCallResult(provider_call_id=call_id, status=CallStatus.QUEUED, raw=data)

    async def hangup_call(self, provider_call_id: str) -> None:
        async with self._client() as client:
            resp = await client.post(
                f"/voice/calls/{provider_call_id}/hangup",
                json={"reason": "agent_done"},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
        if resp.status_code in (404, 409, 410):
            # Not live any more (already ended / not ours / moved node) — the outcome
            # a hangup wants, so not an error worth failing the turn over.
            logger.info("frejun_hangup_call_not_live", extra={"call_id": provider_call_id, "status": resp.status_code})
            return
        self._raise_for_status(resp)

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        async with self._client() as client:
            resp = await client.get(f"/voice/calls/{provider_call_id}")
        self._raise_for_status(resp)
        data = self._unwrap(resp.json())
        status = _STATE_MAP.get(str(data.get("state", "")), CallStatus.UNKNOWN)
        if status == CallStatus.FAILED:
            status = _FAILURE_REASON_MAP.get(str(data.get("reason", "")), CallStatus.FAILED)
        return status

    # --- Webhooks ---------------------------------------------------------------

    def validate_webhook(self, headers: dict[str, str], body: bytes, url: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        return verify_teler_signature(
            self._secret, body, lowered.get("x-teler-timestamp", ""), lowered.get("x-teler-signature", "")
        )

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        """Handles both webhook wire versions (docs: webhooks/call-events): 2026-06-01
        (`type`, root `call_id`, `data.reason`) and legacy 2025-08-01 (`event`,
        `data.call_id`)."""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        event_type = str(payload.get("type") or payload.get("event") or "")
        call_id = str(payload.get("call_id") or data.get("call_id") or "")
        status = _EVENT_MAP.get(event_type, CallStatus.UNKNOWN)
        if status == CallStatus.FAILED:
            status = _FAILURE_REASON_MAP.get(str(data.get("reason", "")), CallStatus.FAILED)
        return CallEvent(provider_call_id=call_id, event_type=event_type, status=status, raw=payload)

    # --- Media (see module docstring, steps 3-4) ----------------------------------

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        # Media is set up by the Call Flow our flow endpoint returns — no REST step.
        return None

    def register_stream(self, provider_call_id: str, stream_id: str, send_text: SendFn) -> None:
        self._streams[provider_call_id] = _Stream(send_text=send_text, stream_id=stream_id)

    def unregister_stream(self, provider_call_id: str) -> None:
        self._streams.pop(provider_call_id, None)

    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        stream = self._streams.get(provider_call_id)
        if stream is None:
            raise RuntimeError(f"No registered media stream for call {provider_call_id}")
        chunk_id = str(stream.next_chunk_id)  # must be unique per stream
        stream.next_chunk_id += 1
        await stream.send_text(
            json.dumps({"type": "audio", "audio_b64": base64.b64encode(audio_chunk).decode("ascii"), "chunk_id": chunk_id})
        )

    async def clear_audio(self, provider_call_id: str) -> None:
        stream = self._streams.get(provider_call_id)
        if stream is None:
            return
        await stream.send_text(json.dumps({"type": "clear"}))

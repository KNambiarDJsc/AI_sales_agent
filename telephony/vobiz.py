"""Vobiz adapter.

Everything here is taken from Vobiz's official documentation (https://vobiz.ai/docs,
full text: https://www.vobiz.ai/docs/llms-full.txt) — not guessed. Page references
are given inline as "docs: <path>".

How a Vobiz call works, and where each piece lives in this codebase:

1. `create_outbound_call` → `POST {base}/Account/{auth_id}/Call/` with headers
   `X-Auth-ID` / `X-Auth-Token` and JSON `{from, to, answer_url, answer_method,
   hangup_url, hangup_method, time_limit}` (docs: call/make-call). Numbers are sent
   without the leading "+" — the docs' examples and responses use `919262171438`.
   The 200 response is `{api_id, message: "Call fired", request_uuid}`; the docs say
   `request_uuid` is "equivalent to `call_uuid`" and the stream's `start.callId`
   "matches the `CallUUID` returned by the REST call-create response", so it is our
   provider_call_id throughout. 200 means queued, not answered.
2. When the callee answers, Vobiz POSTs form-encoded `Event=StartApp`, `CallUUID`,
   `RequestUUID`, `From`, `To`, ... to `answer_url` and executes the XML we return
   (docs: call/make-call "Parameters Sent to answer_url"; concepts/callbacks). That
   endpoint is `apps/api/routers/flows.py` (`/flow/vobiz/{call_attempt_id}`); it
   returns `build_stream_xml()` — a bidirectional `<Stream>` to our media WebSocket.
3. Vobiz connects to the stream URL (`/media/vobiz/{call_attempt_id}/{token}`,
   `apps/api/routers/media.py`). First message `start` (`start.callId`,
   `start.streamId`, `start.mediaFormat {encoding, sampleRate}`), then `media`
   events every 20 ms with base64 `media.payload` in that format (docs:
   xml/stream/stream-events). We ask for `audio/x-l16;rate=8000` — 16-bit linear PCM,
   mono, 8 kHz — the same `pcm16` path FreJun and Exotel use inside `VoiceSession`.
   There is no inbound `stop` event: the WebSocket closing is the end of the stream.
4. Outbound audio: `{"event": "playAudio", "streamId", "media": {"contentType":
   "audio/x-l16", "sampleRate": 8000, "payload": <base64 raw PCM>}}`; barge-in:
   `{"event": "clearAudio", "streamId"}` (docs: xml/stream/play-audio, clear-audio).
5. Status: Vobiz POSTs form-encoded `Event=Hangup` (with `HangupCause`/
   `HangupCauseCode`) to `hangup_url` → `/webhooks/vobiz` (`apps/api/routers/webhooks.py`).
6. Hangup: `DELETE {base}/Account/{auth_id}/Call/{call_uuid}/` → 204 (docs:
   call/hangup-call).

Webhook authenticity (docs: concepts/validating-callbacks): Vobiz signs callbacks
with HMAC-SHA256 over the callback URL *without its query* plus a nonce
(`X-Vobiz-Signature-V3` = base64(HMAC(auth_token, base_url + "." + nonce)); V2 is
the same without the "."), but "signature headers are emitted only when the callback
URL has auth credentials configured on it" — so they can't be relied on to be
present. Every URL we hand Vobiz therefore also carries our own per-call token
(HMAC of the call attempt id, keyed with the auth token, which only we and Vobiz
know): the answer and hangup URLs as `?token=`, the stream URL as a path segment. A
request must carry a valid token, and if Vobiz signature headers are present they
must verify too.

India-specific (docs: compliance/india, faq/domestic-calling-india): the caller ID
must be a Vobiz number, and call media must stay in India ("media anchoring") —
calls fail with hangup cause 2070 otherwise, so the media server must run in India.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from xml.sax.saxutils import escape, quoteattr

import httpx

from config.settings import get_settings
from telephony.base import CallEvent, CallStatus, OutboundCallRequest, OutboundCallResult, TelephonyProvider

logger = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]

# The one audio format both directions use: what VoiceSession's pcm16 path expects
# (voice/audio/processing.TELEPHONY_SAMPLE_RATE_HZ). Inbound is chosen by the
# <Stream contentType>, outbound is declared on every playAudio (docs:
# concepts/streaming-websockets — the two directions are configured separately).
STREAM_CONTENT_TYPE = "audio/x-l16;rate=8000"
PLAY_CONTENT_TYPE = "audio/x-l16"
SAMPLE_RATE_HZ = 8000

# Hangup cause codes (docs: concepts/hangup-causes) that mean the call never became a
# conversation. Everything else that ends a call — normal completions (4000-4030),
# max duration (6000), media timeout (6020) — is "completed" from our side.
_NO_ANSWER_CODES = {3000, 6010}
_BUSY_CODES = {3010, 3100}
_CANCELED_CODES = {1000, 1010, 1020}
_FAILED_CODE_RANGES = ((2000, 2999), (3020, 3999), (5000, 5999), (7000, 8999))
# Text causes, for callbacks that only carry `HangupCause` (docs: xml/overview/
# how-it-works shows `HangupCause=NORMAL_CLEARING`).
_CAUSE_NAMES = {
    "NO_ANSWER": CallStatus.NO_ANSWER,
    "USER_BUSY": CallStatus.BUSY,
    "ORIGINATOR_CANCEL": CallStatus.CANCELED,
}

# `Event` values on the call's own callbacks (docs: concepts/callbacks). Stream
# lifecycle events (StartStream, StopStream, ...) don't change the call's status.
_EVENT_MAP = {
    "Ring": CallStatus.RINGING,
    "StartApp": CallStatus.IN_PROGRESS,
}


def _status_for_hangup(payload: Mapping[str, Any]) -> CallStatus:
    try:
        code = int(str(payload.get("HangupCauseCode", "")).strip())
    except ValueError:
        code = None
    if code is not None:
        if code in _NO_ANSWER_CODES:
            return CallStatus.NO_ANSWER
        if code in _BUSY_CODES:
            return CallStatus.BUSY
        if code in _CANCELED_CODES:
            return CallStatus.CANCELED
        if any(lo <= code <= hi for lo, hi in _FAILED_CODE_RANGES):
            return CallStatus.FAILED
        return CallStatus.COMPLETED
    cause = str(payload.get("HangupCause", "")).strip().upper()
    return _CAUSE_NAMES.get(cause, CallStatus.COMPLETED)


def _vobiz_number(e164: str) -> str:
    return e164.strip().lstrip("+")


def _base_url(url: str) -> str:
    """The URL Vobiz signs: scheme + host + path, no query (docs: validating-callbacks)."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def verify_vobiz_signature(auth_token: str, url: str, headers: Mapping[str, str]) -> bool | None:
    """True/False if Vobiz signature headers are present and do/don't verify; None if
    none were sent (they are only sent when the URL has credentials configured in the
    Vobiz console). V3 preferred, V2 accepted (docs: concepts/validating-callbacks)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    base = _base_url(url).encode()
    key = auth_token.encode()
    for version, joiner in (("v3", b"."), ("v2", b"")):
        signature = lowered.get(f"x-vobiz-signature-{version}", "")
        nonce = lowered.get(f"x-vobiz-signature-{version}-nonce", "")
        if signature and nonce:
            expected = base64.b64encode(hmac.new(key, base + joiner + nonce.encode(), hashlib.sha256).digest()).decode()
            return hmac.compare_digest(signature, expected)
    return None


def call_token(auth_token: str, call_attempt_id: str) -> str:
    """Our own per-call URL token (see module docstring). Hex, so it is safe in a URL
    path and query alike."""
    return hmac.new(auth_token.encode(), f"vobiz-call:{call_attempt_id}".encode(), hashlib.sha256).hexdigest()


def build_stream_xml(ws_url: str) -> str:
    """The answer XML (docs: xml/stream): a bidirectional stream that holds the call
    open (`keepCallAlive`) for as long as our WebSocket is connected. With no element
    after it, Vobiz hangs up when the stream ends (cause 4010)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'    <Stream bidirectional="true" keepCallAlive="true" contentType={quoteattr(STREAM_CONTENT_TYPE)}>'
        f"{escape(ws_url)}</Stream>\n"
        "</Response>"
    )


HANGUP_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Hangup/>\n</Response>'


class VobizAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Vobiz API error {status_code}: {message}")
        self.status_code = status_code


@dataclass
class _Stream:
    send_text: SendFn
    stream_id: str


class VobizProvider(TelephonyProvider):
    name = "vobiz"
    audio_encoding = "pcm16"  # audio/x-l16, 8 kHz mono — see module docstring

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = get_settings()
        self._auth_id = settings.vobiz_auth_id
        self._auth_token = settings.vobiz_auth_token
        self._base_url = settings.vobiz_api_base_url.rstrip("/")
        self._from_number = settings.vobiz_from_number
        self._public_base_url = settings.effective_public_base_url
        self._timeout = settings.vobiz_http_timeout_seconds
        self._max_call_seconds = settings.default_max_call_duration_seconds
        self._transport = transport  # tests inject httpx.MockTransport here
        self._streams: dict[str, _Stream] = {}

    # --- URLs we hand to Vobiz ---------------------------------------------------

    def token_for(self, call_attempt_id: str) -> str:
        return call_token(self._auth_token, call_attempt_id)

    def answer_url_for(self, call_attempt_id: str) -> str:
        return f"{self._public_base_url}/flow/vobiz/{call_attempt_id}?token={self.token_for(call_attempt_id)}"

    def hangup_url_for(self, call_attempt_id: str) -> str:
        query = urlencode({"attempt": call_attempt_id, "token": self.token_for(call_attempt_id)})
        return f"{self._public_base_url}/webhooks/vobiz?{query}"

    def stream_url_for(self, call_attempt_id: str) -> str:
        base = self._public_base_url.replace("https://", "wss://", 1)
        return f"{base}/media/vobiz/{call_attempt_id}/{self.token_for(call_attempt_id)}"

    def token_is_valid(self, call_attempt_id: str, token: str) -> bool:
        return bool(self._auth_token and token) and hmac.compare_digest(token, self.token_for(call_attempt_id))

    # --- REST -----------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if not (self._auth_id and self._auth_token):
            raise RuntimeError("Vobiz not configured: set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN")
        return httpx.AsyncClient(
            base_url=f"{self._base_url}/Account/{self._auth_id}",
            headers={"X-Auth-ID": self._auth_id, "X-Auth-Token": self._auth_token, "Content-Type": "application/json"},
            timeout=self._timeout,
            transport=self._transport,
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except ValueError:
            body = None
        message = (body.get("error") or body.get("message")) if isinstance(body, dict) else None
        message = message or resp.text
        raise VobizAPIError(resp.status_code, str(message)[:300])

    async def get_balance(self, currency: str = "INR") -> dict[str, Any]:
        """`GET /Account/{auth_id}/balance/{currency}` (docs: account/balance) — also a
        cheap check that the credentials work. Used by the preflight script."""
        async with self._client() as client:
            resp = await client.get(f"/balance/{currency}")
        self._raise_for_status(resp)
        return resp.json()

    async def list_numbers(self) -> dict[str, Any]:
        """The account's own numbers (docs: account-phone-number/list-account-phone-numbers)."""
        async with self._client() as client:
            resp = await client.get("/numbers")
        self._raise_for_status(resp)
        return resp.json()

    async def create_outbound_call(self, request: OutboundCallRequest) -> OutboundCallResult:
        from_number = request.from_number or self._from_number
        if not from_number:
            raise RuntimeError("Vobiz not configured: set VOBIZ_FROM_NUMBER (a Vobiz number)")
        if not request.call_attempt_id:
            raise RuntimeError("Vobiz calls need OutboundCallRequest.call_attempt_id to build the answer URL")
        if not self._public_base_url.startswith("https://"):
            raise RuntimeError("Vobiz needs PUBLIC_BASE_URL set to this app's public https:// URL")

        payload = {
            "from": _vobiz_number(from_number),
            "to": _vobiz_number(request.to_number),
            "answer_url": self.answer_url_for(request.call_attempt_id),
            "answer_method": "POST",
            "hangup_url": self.hangup_url_for(request.call_attempt_id),
            "hangup_method": "POST",
            "time_limit": self._max_call_seconds,
        }
        async with self._client() as client:
            resp = await client.post("/Call/", json=payload)
        self._raise_for_status(resp)
        data = resp.json()
        call_uuid = str(data.get("request_uuid") or data.get("call_uuid") or "")
        if not call_uuid:
            raise VobizAPIError(resp.status_code, "make-call response had no request_uuid")
        logger.info("vobiz_call_initiated", extra={"call_uuid": call_uuid, "call_attempt_id": request.call_attempt_id})
        return OutboundCallResult(provider_call_id=call_uuid, status=CallStatus.QUEUED, raw=data)

    async def hangup_call(self, provider_call_id: str) -> None:
        async with self._client() as client:
            resp = await client.delete(f"/Call/{provider_call_id}/")
        if resp.status_code == 404:
            # Already over (or never connected) — the outcome a hangup wants.
            logger.info("vobiz_hangup_call_not_live", extra={"call_uuid": provider_call_id})
            return
        self._raise_for_status(resp)

    async def get_call_status(self, provider_call_id: str) -> CallStatus:
        """Live calls only (docs: call/retrieve-live-call needs `status=live`; an ended
        call answers 404 there, so it reads as UNKNOWN — the hangup webhook is the
        authoritative end-of-call signal)."""
        async with self._client() as client:
            resp = await client.get(f"/Call/{provider_call_id}/", params={"status": "live"})
        if resp.status_code == 404:
            return CallStatus.UNKNOWN
        self._raise_for_status(resp)
        status = str(resp.json().get("call_status", ""))
        return {"in-progress": CallStatus.IN_PROGRESS, "ringing": CallStatus.RINGING}.get(status, CallStatus.UNKNOWN)

    # --- Webhooks ---------------------------------------------------------------

    def validate_webhook(self, headers: Mapping[str, str], body: bytes, url: str) -> bool:
        """Our per-call token in the query must be valid, and any Vobiz signature that
        was sent must verify (see module docstring). The signature is computed over the
        public URL Vobiz called, so it is rebuilt from PUBLIC_BASE_URL — behind a
        tunnel the request URL the app sees may say http://."""
        query = parse_qs(urlparse(url).query)
        attempt = (query.get("attempt") or [""])[0]
        token = (query.get("token") or [""])[0]
        if not (attempt and self.token_is_valid(attempt, token)):
            return False
        public_url = self._public_base_url + urlparse(url).path
        return verify_vobiz_signature(self._auth_token, public_url, headers) is not False

    def parse_call_event(self, payload: dict[str, Any]) -> CallEvent:
        event_type = str(payload.get("Event", ""))
        call_uuid = str(payload.get("RequestUUID") or payload.get("CallUUID") or "")
        if event_type == "Hangup":
            status = _status_for_hangup(payload)
        else:
            status = _EVENT_MAP.get(event_type, CallStatus.UNKNOWN)
        return CallEvent(provider_call_id=call_uuid, event_type=event_type, status=status, raw=payload)

    # --- Media (see module docstring, steps 3-4) ----------------------------------

    async def connect_media(self, provider_call_id: str, media_websocket_url: str) -> None:
        # Media is set up by the <Stream> XML our answer endpoint returns — no REST step.
        return None

    def register_stream(self, provider_call_id: str, stream_id: str, send_text: SendFn) -> None:
        self._streams[provider_call_id] = _Stream(send_text=send_text, stream_id=stream_id)

    def unregister_stream(self, provider_call_id: str) -> None:
        self._streams.pop(provider_call_id, None)

    async def send_audio(self, provider_call_id: str, audio_chunk: bytes) -> None:
        stream = self._streams.get(provider_call_id)
        if stream is None:
            raise RuntimeError(f"No registered media stream for call {provider_call_id}")
        await stream.send_text(
            json.dumps(
                {
                    "event": "playAudio",
                    "streamId": stream.stream_id,
                    "media": {
                        "contentType": PLAY_CONTENT_TYPE,
                        "sampleRate": SAMPLE_RATE_HZ,
                        "payload": base64.b64encode(audio_chunk).decode("ascii"),
                    },
                }
            )
        )

    async def clear_audio(self, provider_call_id: str) -> None:
        stream = self._streams.get(provider_call_id)
        if stream is None:
            return
        await stream.send_text(json.dumps({"event": "clearAudio", "streamId": stream.stream_id}))

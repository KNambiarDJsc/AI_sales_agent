"""FreJunProvider against FreJun's documented wire format (telephony/frejun.py).
HTTP is mocked with httpx.MockTransport — no network, no real calls, no .env."""
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from config.settings import Settings
from telephony.base import CallStatus, OutboundCallRequest
from telephony.frejun import FreJunAPIError, FreJunProvider, build_stream_flow, verify_teler_signature

API_KEY = "test-api-key-not-real"
SECRET = "test-webhook-secret"


@pytest.fixture
def settings(monkeypatch):
    s = Settings(
        _env_file=None,
        frejun_api_key=API_KEY,
        frejun_secret=SECRET,
        frejun_from_number="+918000000000",
        public_base_url="https://agent.example.com",
    )
    monkeypatch.setattr("telephony.frejun.get_settings", lambda: s)
    return s


def _provider(handler) -> FreJunProvider:
    return FreJunProvider(transport=httpx.MockTransport(handler))


def _request(**overrides) -> OutboundCallRequest:
    values = dict(
        to_number="+919999999999", from_number="", campaign_id="c", lead_id="l", attempt_number=1,
        media_websocket_url="wss://unused", status_callback_url="https://agent.example.com/webhooks/frejun",
        call_attempt_id="7f0c1e9a-0000-4000-8000-000000000001",
    )
    values.update(overrides)
    return OutboundCallRequest(**values)


async def test_create_outbound_call_matches_the_documented_initiate_request(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"data": {"id": "cs_01TEST", "from_number": "+918000000000"}})

    result = await _provider(handler).create_outbound_call(_request())

    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.frejun.ai/api/v1/voice/calls/initiate"
    assert seen["key"] == API_KEY
    assert seen["body"] == {
        "from_number": "+918000000000",
        "to_number": "+919999999999",
        "flow_url": "https://agent.example.com/flow/frejun/7f0c1e9a-0000-4000-8000-000000000001",
        "status_callback_url": "https://agent.example.com/webhooks/frejun",
        "record": False,
    }
    assert result.provider_call_id == "cs_01TEST"
    assert result.status == CallStatus.QUEUED


async def test_create_outbound_call_refuses_without_from_number_or_public_url(settings):
    settings.frejun_from_number = ""
    with pytest.raises(RuntimeError, match="FREJUN_FROM_NUMBER"):
        await _provider(lambda r: httpx.Response(500)).create_outbound_call(_request())
    settings.frejun_from_number = "+918000000000"
    settings.public_base_url = "http://localhost:8000"
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        await _provider(lambda r: httpx.Response(500)).create_outbound_call(_request())


async def test_api_errors_surface_status_and_message_but_never_the_key(settings):
    handler = lambda r: httpx.Response(403, json={"success": False, "message": "Invalid API Key."})  # noqa: E731
    with pytest.raises(FreJunAPIError) as exc_info:
        await _provider(handler).create_outbound_call(_request())
    assert exc_info.value.status_code == 403
    assert "Invalid API Key." in str(exc_info.value)
    assert API_KEY not in str(exc_info.value)


async def test_hangup_sends_idempotency_key_and_tolerates_a_call_that_already_ended(settings):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("idempotency-key"), json.loads(request.content)))
        return httpx.Response(409, json={"success": False, "message": "call_not_live"})

    await _provider(handler).hangup_call("cs_01TEST")  # 409 must not raise
    path, idem, body = seen[0]
    assert path == "/api/v1/voice/calls/cs_01TEST/hangup"
    assert idem
    assert body == {"reason": "agent_done"}


async def test_hangup_raises_on_a_real_server_error(settings):
    with pytest.raises(FreJunAPIError):
        await _provider(lambda r: httpx.Response(500, json={"message": "boom"})).hangup_call("cs_01TEST")


async def test_get_call_status_maps_failed_no_answer(settings):
    handler = lambda r: httpx.Response(200, json={"data": {"id": "cs_1", "state": "failed", "reason": "no_answer"}})  # noqa: E731
    assert await _provider(handler).get_call_status("cs_1") == CallStatus.NO_ANSWER


def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def test_signature_verification_follows_the_documented_scheme():
    body = b'{"type":"call.answered"}'
    ts = str(int(time.time()))
    assert verify_teler_signature(SECRET, body, ts, _sign(body, ts))
    assert not verify_teler_signature(SECRET, body + b" ", ts, _sign(body, ts))  # body tampered
    assert not verify_teler_signature(SECRET, body, ts, _sign(body, ts, "other-secret"))
    stale = str(int(time.time()) - 3600)
    assert not verify_teler_signature(SECRET, body, stale, _sign(body, stale))  # replay window
    assert not verify_teler_signature("", body, ts, _sign(body, ts))  # no secret configured


def test_validate_webhook_reads_headers_case_insensitively(settings):
    body = b'{"type":"call.completed"}'
    ts = str(int(time.time()))
    provider = FreJunProvider()
    assert provider.validate_webhook({"x-teler-timestamp": ts, "x-teler-signature": _sign(body, ts)}, body, "")
    assert provider.validate_webhook({"X-Teler-Timestamp": ts, "X-Teler-Signature": _sign(body, ts)}, body, "")
    assert not provider.validate_webhook({}, body, "")


@pytest.mark.parametrize(
    "payload,expected_status,expected_call_id",
    [
        ({"type": "call.initiated", "call_id": "cs_1", "data": {"call_id": "cs_1"}}, CallStatus.DIALING, "cs_1"),
        ({"type": "call.answered", "call_id": "cs_1", "data": {"call_id": "cs_1"}}, CallStatus.IN_PROGRESS, "cs_1"),
        ({"type": "call.completed", "call_id": "cs_1", "data": {"reason": "normal"}}, CallStatus.COMPLETED, "cs_1"),
        ({"type": "call.failed", "call_id": "cs_1", "data": {"reason": "busy"}}, CallStatus.BUSY, "cs_1"),
        ({"type": "call.failed", "call_id": "cs_1", "data": {"reason": "no_answer"}}, CallStatus.NO_ANSWER, "cs_1"),
        ({"type": "call.failed", "call_id": "cs_1", "data": {"reason": "flow_error"}}, CallStatus.FAILED, "cs_1"),
        ({"type": "stream.initiated", "call_id": "cs_1", "data": {}}, CallStatus.UNKNOWN, "cs_1"),
        # legacy 2025-08-01 wire format
        ({"event": "call.answered", "data": {"call_id": "uuid-1"}}, CallStatus.IN_PROGRESS, "uuid-1"),
    ],
)
def test_parse_call_event_handles_both_webhook_versions(settings, payload, expected_status, expected_call_id):
    event = FreJunProvider().parse_call_event(payload)
    assert event.status == expected_status
    assert event.provider_call_id == expected_call_id


async def test_outbound_media_messages_match_the_websocket_protocol(settings):
    sent = []

    async def send_text(text):
        sent.append(json.loads(text))

    provider = FreJunProvider()
    provider.register_stream("cs_1", "ms_1", send_text)
    await provider.send_audio("cs_1", b"\x01\x00\x02\x00")
    await provider.send_audio("cs_1", b"\x03\x00")
    await provider.clear_audio("cs_1")

    assert sent[0] == {"type": "audio", "audio_b64": base64.b64encode(b"\x01\x00\x02\x00").decode(), "chunk_id": "1"}
    assert sent[1]["chunk_id"] == "2"  # unique per stream
    assert sent[2] == {"type": "clear"}
    provider.unregister_stream("cs_1")
    with pytest.raises(RuntimeError):
        await provider.send_audio("cs_1", b"\x00\x00")


def test_stream_flow_matches_the_documented_stream_action(settings):
    flow = build_stream_flow("wss://agent.example.com/media/frejun/abc")
    assert flow == {
        "action": "stream",
        "ws_url": "wss://agent.example.com/media/frejun/abc",
        "sample_rate": "8k",
        "chunk_size": 100,
        "record": False,
    }

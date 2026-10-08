"""Vobiz adapter against the shapes in Vobiz's official docs (no network: requests go to
an httpx.MockTransport). Doc references are in telephony/vobiz.py."""
import asyncio
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from config.settings import Settings
from telephony.base import CallStatus, OutboundCallRequest
from telephony.vobiz import (
    VobizAPIError,
    VobizProvider,
    build_stream_xml,
    call_token,
    verify_vobiz_signature,
)

TOKEN = "test-auth-token"
AUTH_ID = "MA_TEST1234"


@pytest.fixture
def settings(monkeypatch):
    s = Settings(
        _env_file=None,
        vobiz_auth_id=AUTH_ID,
        vobiz_auth_token=TOKEN,
        vobiz_from_number="+918035000000",
        public_base_url="https://agent.example.com",
        database_url="unused",
    )
    monkeypatch.setattr("telephony.vobiz.get_settings", lambda: s)
    return s


def _provider(handler):
    return VobizProvider(transport=httpx.MockTransport(handler))


def _request(attempt_id="11111111-2222-3333-4444-555555555555"):
    return OutboundCallRequest(
        to_number="+919812345678", from_number="", campaign_id="c", lead_id="l", attempt_number=1,
        media_websocket_url="", status_callback_url="", call_attempt_id=attempt_id,
    )


def test_make_call_sends_the_documented_request_and_uses_request_uuid(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["url"] = request.method, str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"api_id": "a1", "message": "Call fired", "request_uuid": "uuid-123"})

    result = asyncio.run(_provider(handler).create_outbound_call(_request()))
    assert (seen["method"], seen["url"]) == ("POST", f"https://api.vobiz.ai/api/v1/Account/{AUTH_ID}/Call/")
    assert seen["headers"]["X-Auth-ID"] == AUTH_ID and seen["headers"]["X-Auth-Token"] == TOKEN
    body = seen["body"]
    assert body["from"] == "918035000000" and body["to"] == "919812345678"  # no "+", as in the docs
    attempt = "11111111-2222-3333-4444-555555555555"
    expected_token = call_token(TOKEN, attempt)
    assert body["answer_url"] == f"https://agent.example.com/flow/vobiz/{attempt}?token={expected_token}"
    assert body["hangup_url"] == f"https://agent.example.com/webhooks/vobiz?attempt={attempt}&token={expected_token}"
    assert body["answer_method"] == body["hangup_method"] == "POST"
    assert result.provider_call_id == "uuid-123" and result.status == CallStatus.QUEUED


def test_make_call_errors_surface_with_the_status_code(settings):
    def handler(request):
        return httpx.Response(402, json={"error": "insufficient balance"})

    with pytest.raises(VobizAPIError) as exc:
        asyncio.run(_provider(handler).create_outbound_call(_request()))
    assert exc.value.status_code == 402 and "insufficient balance" in str(exc.value)


def test_make_call_refuses_without_a_public_https_url(settings):
    settings.public_base_url = "http://localhost:8000"
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        asyncio.run(_provider(lambda r: httpx.Response(200)).create_outbound_call(_request()))


def test_hangup_is_a_delete_and_tolerates_an_already_ended_call(settings):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(204 if len(calls) == 1 else 404)

    provider = _provider(handler)
    asyncio.run(provider.hangup_call("uuid-1"))
    asyncio.run(provider.hangup_call("uuid-1"))  # second time: 404, not an error
    assert calls[0] == ("DELETE", f"/api/v1/Account/{AUTH_ID}/Call/uuid-1/")


def test_stream_xml_is_bidirectional_l16_8k_and_escaped():
    xml = build_stream_xml("wss://agent.example.com/media/vobiz/abc/tok")
    assert 'bidirectional="true"' in xml and 'keepCallAlive="true"' in xml
    assert "contentType=\"audio/x-l16;rate=8000\"" in xml
    assert ">wss://agent.example.com/media/vobiz/abc/tok</Stream>" in xml
    assert "&amp;" in build_stream_xml("wss://h/x?a=1&b=2")  # a raw & would be invalid XML


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"Event": "Ring", "RequestUUID": "u"}, CallStatus.RINGING),
        ({"Event": "StartApp", "RequestUUID": "u"}, CallStatus.IN_PROGRESS),
        ({"Event": "Hangup", "RequestUUID": "u", "HangupCauseCode": "4000"}, CallStatus.COMPLETED),
        ({"Event": "Hangup", "RequestUUID": "u", "HangupCauseCode": "6010"}, CallStatus.NO_ANSWER),
        ({"Event": "Hangup", "RequestUUID": "u", "HangupCauseCode": "3010"}, CallStatus.BUSY),
        ({"Event": "Hangup", "RequestUUID": "u", "HangupCauseCode": "2070"}, CallStatus.FAILED),  # media anchoring
        ({"Event": "Hangup", "RequestUUID": "u", "HangupCause": "NO_ANSWER"}, CallStatus.NO_ANSWER),
        ({"Event": "Hangup", "RequestUUID": "u"}, CallStatus.COMPLETED),
        ({"Event": "StartStream", "RequestUUID": "u"}, CallStatus.UNKNOWN),
    ],
)
def test_callback_events_map_to_call_statuses(settings, payload, expected):
    event = VobizProvider().parse_call_event(payload)
    assert event.status == expected and event.provider_call_id == "u"


def test_signature_check_follows_the_documented_algorithm():
    url = "https://agent.example.com/webhooks/vobiz?attempt=a&token=t"
    nonce = "12345678901234567890"
    base = b"https://agent.example.com/webhooks/vobiz"  # query stripped
    v3 = base64.b64encode(hmac.new(TOKEN.encode(), base + b"." + nonce.encode(), hashlib.sha256).digest()).decode()
    v2 = base64.b64encode(hmac.new(TOKEN.encode(), base + nonce.encode(), hashlib.sha256).digest()).decode()
    assert verify_vobiz_signature(TOKEN, url, {"X-Vobiz-Signature-V3": v3, "X-Vobiz-Signature-V3-Nonce": nonce})
    assert verify_vobiz_signature(TOKEN, url, {"X-Vobiz-Signature-V2": v2, "X-Vobiz-Signature-V2-Nonce": nonce})
    assert verify_vobiz_signature(TOKEN, url, {"X-Vobiz-Signature-V3": v2, "X-Vobiz-Signature-V3-Nonce": nonce}) is False
    assert verify_vobiz_signature(TOKEN, url, {}) is None  # not sent unless configured in the console


def test_webhook_needs_our_call_token_and_any_vobiz_signature_must_verify(settings):
    provider = VobizProvider()
    attempt = "aaaa"
    good = f"http://agent.example.com/webhooks/vobiz?attempt={attempt}&token={call_token(TOKEN, attempt)}"
    assert provider.validate_webhook({}, b"", good)
    assert not provider.validate_webhook({}, b"", f"http://agent.example.com/webhooks/vobiz?attempt={attempt}&token=x")
    assert not provider.validate_webhook({}, b"", "http://agent.example.com/webhooks/vobiz")
    bad_sig = {"X-Vobiz-Signature-V3": "bogus", "X-Vobiz-Signature-V3-Nonce": "1"}
    assert not provider.validate_webhook(bad_sig, b"", good)
    # The signature is over the public https URL, even if the app saw http:// behind a tunnel.
    nonce = "99"
    v3 = base64.b64encode(hmac.new(TOKEN.encode(), b"https://agent.example.com/webhooks/vobiz." + nonce.encode(),
                                   hashlib.sha256).digest()).decode()
    assert provider.validate_webhook({"X-Vobiz-Signature-V3": v3, "X-Vobiz-Signature-V3-Nonce": nonce}, b"", good)


def test_outbound_audio_and_barge_in_use_the_documented_messages(settings):
    sent = []

    async def send_text(text):
        sent.append(json.loads(text))

    provider = VobizProvider()
    provider.register_stream("call-1", "stream-1", send_text)
    asyncio.run(provider.send_audio("call-1", b"\x01\x00\x02\x00"))
    asyncio.run(provider.clear_audio("call-1"))
    assert sent[0] == {
        "event": "playAudio",
        "streamId": "stream-1",
        "media": {"contentType": "audio/x-l16", "sampleRate": 8000, "payload": base64.b64encode(b"\x01\x00\x02\x00").decode()},
    }
    assert sent[1] == {"event": "clearAudio", "streamId": "stream-1"}
    provider.unregister_stream("call-1")
    asyncio.run(provider.clear_audio("call-1"))  # no stream any more: a no-op, not an error
    with pytest.raises(RuntimeError):
        asyncio.run(provider.send_audio("call-1", b"\x00\x00"))

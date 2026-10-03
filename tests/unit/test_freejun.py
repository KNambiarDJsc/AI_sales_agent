import asyncio
import base64
import json
import uuid

from fastapi.testclient import TestClient

from apps.api.main import app
from telephony.freejun import FreejunProvider, _flow_url_for


def test_flow_url_maps_media_url_to_the_flow_endpoint_for_the_same_attempt():
    attempt_id = str(uuid.uuid4())
    media_url = f"wss://example.ngrok.app/media/freejun/{attempt_id}"
    assert _flow_url_for(media_url) == f"https://example.ngrok.app/freejun/flow/{attempt_id}"


def test_send_audio_wraps_pcm16_in_teler_audio_message_with_incrementing_chunk_ids():
    provider = FreejunProvider()
    sent: list[str] = []

    async def capture(text: str) -> None:
        sent.append(text)

    provider.register_stream("call-1", "stream-1", capture)
    asyncio.run(provider.send_audio("call-1", b"\x01\x02\x03\x04"))
    asyncio.run(provider.send_audio("call-1", b"\x05\x06"))

    first, second = (json.loads(s) for s in sent)
    assert first["type"] == "audio"
    assert base64.b64decode(first["audio_b64"]) == b"\x01\x02\x03\x04"
    assert first["chunk_id"] == 1
    assert second["chunk_id"] == 2


def test_clear_audio_sends_teler_clear_message():
    provider = FreejunProvider()
    sent: list[str] = []

    async def capture(text: str) -> None:
        sent.append(text)

    provider.register_stream("call-2", "stream-2", capture)
    asyncio.run(provider.clear_audio("call-2"))
    assert json.loads(sent[0]) == {"type": "clear"}


def test_unverified_webhook_surfaces_fail_closed_not_a_guess():
    provider = FreejunProvider()
    assert provider.validate_webhook({}, b"{}", "https://example/webhooks/freejun") is False


def test_flow_endpoint_returns_a_stream_action_pointing_at_this_attempts_media_socket():
    attempt_id = uuid.uuid4()
    with TestClient(app, base_url="https://example.ngrok.app") as client:
        resp = client.post(f"/freejun/flow/{attempt_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "stream"
    assert body["ws_url"] == f"wss://example.ngrok.app/media/freejun/{attempt_id}"
    assert body["sample_rate"] == "8k"

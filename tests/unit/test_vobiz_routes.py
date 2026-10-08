"""Vobiz HTTP/WebSocket surface against the real Postgres (skipped when it isn't
reachable). No Vobiz or AI network calls: the answer endpoint, the media route's
handshake/rejection paths, and hangup webhooks driving the call lifecycle. Same
event-loop handling as tests/unit/test_frejun_routes.py."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from config.settings import Settings
from database.models import CallAttempt, Campaign, Lead, Tenant
from database.session import session_scope
from telephony.vobiz import VobizProvider, call_token
from tests.unit.test_frejun_routes import _drop_pool, _run, _state
from tests.unit.test_media_exotel import requires_db

TOKEN = "route-test-token"


@pytest.fixture(autouse=True)
def _fresh_pool():
    _drop_pool()
    yield
    _drop_pool()


@pytest.fixture
def vobiz(monkeypatch):
    s = Settings(
        _env_file=None,
        vobiz_auth_id="MA_ROUTES",
        vobiz_auth_token=TOKEN,
        vobiz_from_number="+918035000000",
        public_base_url="https://agent.example.com",
        database_url="unused",
    )
    monkeypatch.setattr("telephony.vobiz.get_settings", lambda: s)
    monkeypatch.setattr("apps.api.routers.flows.get_settings", lambda: s)
    provider = VobizProvider()
    for module in ("apps.api.routers.flows", "apps.api.routers.media", "apps.api.routers.webhooks"):
        monkeypatch.setattr(f"{module}.get_telephony_provider", lambda name=None: provider)
    return provider


async def _make_attempt(provider_call_id: str | None = None):
    async with session_scope() as s:
        tenant = Tenant(name=f"vobiz-test-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        await s.flush()
        campaign = Campaign(
            tenant_id=tenant.id, name="vobiz-test", script_id="product-a", script_version=1, status="paused",
            telephony_provider="vobiz", call_window_start="00:00", call_window_end="23:59", timezone="Asia/Kolkata",
            max_concurrent_calls=1, max_call_duration_seconds=420,
        )
        s.add(campaign)
        await s.flush()
        phone = "+9198" + str(uuid.uuid4().int)[:8]
        lead = Lead(tenant_id=tenant.id, campaign_id=campaign.id, phone_e164=phone, raw_phone=phone, dedupe_key=phone,
                    status="in_progress", attempts_count=1)
        s.add(lead)
        await s.flush()
        attempt = CallAttempt(campaign_id=campaign.id, lead_id=lead.id, attempt_number=1, provider="vobiz",
                              provider_call_id=provider_call_id, status="queued")
        s.add(attempt)
        await s.flush()
        return attempt.id


def _answer(client, attempt_id, token=None, **form):
    token = call_token(TOKEN, str(attempt_id)) if token is None else token
    data = {"Event": "StartApp", "CallStatus": "in-progress", "Direction": "outbound", **form}
    return client.post(f"/flow/vobiz/{attempt_id}?token={token}", data=data)


# --- answer endpoint ---------------------------------------------------------------


@requires_db
def test_answer_returns_a_bidirectional_stream_to_our_tokened_ws_url(vobiz):
    attempt_id = _run(_make_attempt())
    with TestClient(app) as client:
        resp = _answer(client, attempt_id, RequestUUID="uuid-ANS", CallUUID="uuid-ANS")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/xml")
    ws_url = f"wss://agent.example.com/media/vobiz/{attempt_id}/{call_token(TOKEN, str(attempt_id))}"
    assert f">{ws_url}</Stream>" in resp.text and 'bidirectional="true"' in resp.text
    assert _run(_state(attempt_id))[3] == "uuid-ANS"  # recorded when the make-call response hadn't been


@requires_db
def test_answer_hangs_up_for_a_bad_token_or_an_unknown_attempt(vobiz):
    attempt_id = _run(_make_attempt())
    with TestClient(app) as client:
        assert "<Hangup/>" in _answer(client, attempt_id, token="forged").text
        unknown = uuid.uuid4()
        assert "<Hangup/>" in _answer(client, unknown).text  # valid token for an id we never dialled


# --- media WebSocket ------------------------------------------------------------------


def _start(call_id="uuid-1", encoding="audio/x-l16", rate=8000):
    return json.dumps({"sequenceNumber": 0, "event": "start", "extra_headers": "{}",
                       "start": {"callId": call_id, "streamId": "st-1", "accountId": "1", "tracks": ["inbound"],
                                 "mediaFormat": {"encoding": encoding, "sampleRate": rate}}})


def _ws_path(attempt_id, token=None):
    return f"/media/vobiz/{attempt_id}/{token or call_token(TOKEN, str(attempt_id))}"


@requires_db
def test_media_refuses_a_bad_token_before_accepting(vobiz):
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_ws_path(uuid.uuid4(), token="forged")):
            pass
    assert exc.value.code == 4403


@requires_db
def test_media_rejects_a_first_message_that_is_not_start(vobiz):
    with TestClient(app) as client, client.websocket_connect(_ws_path(uuid.uuid4())) as ws:
        ws.send_text(json.dumps({"event": "media", "media": {"payload": ""}}))
        assert ws.receive()["code"] == 4400


@requires_db
@pytest.mark.parametrize("encoding,rate", [("audio/x-l16", 16000), ("audio/x-mulaw", 8000)])
def test_media_refuses_an_audio_format_it_did_not_ask_for(vobiz, encoding, rate):
    with TestClient(app) as client, client.websocket_connect(_ws_path(uuid.uuid4())) as ws:
        ws.send_text(_start(encoding=encoding, rate=rate))
        assert ws.receive()["code"] == 4415


@requires_db
def test_media_closes_for_an_unknown_attempt(vobiz):
    with TestClient(app) as client, client.websocket_connect(_ws_path(uuid.uuid4())) as ws:
        ws.send_text(_start())
        assert ws.receive()["code"] == 4404


@requires_db
def test_media_closes_when_the_start_call_id_is_not_this_attempts_call(vobiz):
    attempt_id = _run(_make_attempt(provider_call_id="uuid-EXPECTED"))
    with TestClient(app) as client, client.websocket_connect(_ws_path(attempt_id)) as ws:
        ws.send_text(_start(call_id="uuid-SOMEONE-ELSE"))
        assert ws.receive()["code"] == 4403


# --- hangup webhook --------------------------------------------------------------------


def _hangup(client, attempt_id, token=None, **form):
    token = call_token(TOKEN, str(attempt_id)) if token is None else token
    return client.post(f"/webhooks/vobiz?attempt={attempt_id}&token={token}",
                       data={"Event": "Hangup", "CallStatus": "completed", **form})


@requires_db
def test_hangup_webhook_needs_our_token(vobiz):
    attempt_id = _run(_make_attempt(provider_call_id="uuid-T"))
    with TestClient(app) as client:
        assert _hangup(client, attempt_id, token="forged", RequestUUID="uuid-T").status_code == 403


@requires_db
def test_unanswered_call_hangup_moves_the_lead_out_of_in_progress(vobiz):
    call_uuid = f"uuid-{uuid.uuid4().hex}"
    attempt_id = _run(_make_attempt(provider_call_id=call_uuid))
    with TestClient(app) as client:
        resp = _hangup(client, attempt_id, RequestUUID=call_uuid, CallUUID=call_uuid, HangupCauseCode="6010",
                       HangupCauseName="Ring Timeout Reached")
    assert resp.json() == {"status": "ok"}
    status, outcome, lead_status, _ = _run(_state(attempt_id))
    assert (status, outcome, lead_status) == ("no_answer", "no_answer", "failed")  # retry sweep can requeue it

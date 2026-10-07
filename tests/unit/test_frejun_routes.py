"""FreJun HTTP/WebSocket surface + call lifecycle, against the real Postgres from
docker-compose (skipped when it isn't reachable). No OpenAI or FreJun network calls:
only the flow endpoint, the media route's handshake/rejection paths, signed webhooks,
`finalize_call`, and `schedule_callback` idempotency.

Plain `def` tests + `asyncio.run` for DB setup, disposing the pool between loops —
same reasoning as tests/unit/test_media_exotel.py."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from config.settings import Settings
from database.models import CallAttempt, Callback, Campaign, Conversation, Lead, Qualification, Tenant
from database.session import engine, session_scope
from services.call_worker.lifecycle import finalize_call
from telephony.frejun import FreJunProvider
from tests.unit.test_media_exotel import requires_db
from tools.callback import ScheduleCallbackInput
from tools.callback import _handle as schedule_callback
from tools.registry import ToolContext

SECRET = "route-test-secret"


def _drop_pool() -> None:
    # close=False: forget pooled connections without trying to close them — they
    # belong to an event loop that has already finished (each asyncio.run and each
    # TestClient has its own), and closing them from another loop raises.
    asyncio.run(engine.dispose(close=False))


@pytest.fixture(autouse=True)
def _fresh_pool():
    _drop_pool()
    yield
    _drop_pool()


@pytest.fixture
def frejun_settings(monkeypatch):
    s = Settings(
        _env_file=None,
        frejun_api_key="test-key",
        frejun_secret=SECRET,
        frejun_from_number="+918000000000",
        public_base_url="https://agent.example.com",
        database_url="unused",
    )
    monkeypatch.setattr("telephony.frejun.get_settings", lambda: s)
    monkeypatch.setattr("apps.api.routers.flows.get_settings", lambda: s)
    provider = FreJunProvider()
    monkeypatch.setattr("apps.api.routers.webhooks.get_telephony_provider", lambda name=None: provider)
    return s


def _run(coro):
    async def _in_one_loop():
        try:
            return await coro
        finally:
            await engine.dispose()  # same loop that opened the connections

    _drop_pool()
    return asyncio.run(_in_one_loop())


async def _make_attempt(provider_call_id: str | None = None, with_conversation: bool = False):
    async with session_scope() as s:
        tenant = Tenant(name=f"frejun-test-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        await s.flush()
        campaign = Campaign(
            tenant_id=tenant.id, name="frejun-test", script_id="product-a", script_version=1, status="paused",
            telephony_provider="frejun", call_window_start="00:00", call_window_end="23:59", timezone="Asia/Kolkata",
            max_concurrent_calls=1, max_call_duration_seconds=420,
        )
        s.add(campaign)
        await s.flush()
        phone = "+9198" + str(uuid.uuid4().int)[:8]
        lead = Lead(tenant_id=tenant.id, campaign_id=campaign.id, phone_e164=phone, raw_phone=phone, dedupe_key=phone,
                    status="in_progress", attempts_count=1)
        s.add(lead)
        await s.flush()
        attempt = CallAttempt(campaign_id=campaign.id, lead_id=lead.id, attempt_number=1, provider="frejun",
                              provider_call_id=provider_call_id, status="queued")
        s.add(attempt)
        await s.flush()
        conversation_id = None
        if with_conversation:
            conv = Conversation(call_attempt_id=attempt.id, script_id="product-a", script_version=1, current_state="INTRO")
            s.add(conv)
            await s.flush()
            conversation_id = conv.id
        return tenant.id, campaign.id, lead.id, attempt.id, conversation_id


async def _state(attempt_id):
    async with session_scope() as s:
        attempt = await s.get(CallAttempt, attempt_id)
        lead = await s.get(Lead, attempt.lead_id)
        return attempt.status, attempt.outcome, lead.status, attempt.provider_call_id


# --- flow endpoint --------------------------------------------------------------


@requires_db
def test_flow_returns_documented_stream_action_and_records_call_id(frejun_settings):
    *_, attempt_id, _ = _run(_make_attempt())
    with TestClient(app) as client:
        resp = client.post(f"/flow/frejun/{attempt_id}", json={"call_id": "cs_FLOW1", "account_id": "acc_1",
                                                                 "from_number": "+918000000000", "to_number": "+919999999999",
                                                                 "direction": "outbound"})
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "stream",
        "ws_url": f"wss://agent.example.com/media/frejun/{attempt_id}",
        "sample_rate": "8k",
        "chunk_size": 100,
        "record": False,
    }
    assert _run(_state(attempt_id))[3] == "cs_FLOW1"


@requires_db
def test_inbound_flow_hangs_up_cleanly(frejun_settings):
    with TestClient(app) as client:
        resp = client.post("/flow/frejun/inbound", json={"call_id": "cs_IN", "direction": "inbound"})
    assert resp.status_code == 200
    assert resp.json() == {"action": "hangup"}


@requires_db
def test_flow_hangs_up_unknown_attempts_and_call_id_mismatches(frejun_settings):
    *_, attempt_id, _ = _run(_make_attempt(provider_call_id="cs_REAL"))
    with TestClient(app) as client:
        assert client.post(f"/flow/frejun/{uuid.uuid4()}", json={"call_id": "cs_X"}).json() == {"action": "hangup"}
        assert client.post(f"/flow/frejun/{attempt_id}", json={"call_id": "cs_OTHER"}).json() == {"action": "hangup"}


# --- media WebSocket --------------------------------------------------------------


def _start(call_id="cs_1", **data):
    fmt = {"encoding": "audio/l16", "sample_rate": 8000, "channels": 1}
    fmt.update(data)
    return json.dumps({"type": "start", "account_id": "acc_1", "call_app_id": "va_1", "call_id": call_id,
                       "stream_id": "ms_1", "message_id": 1, "data": fmt})


@requires_db
def test_media_rejects_a_first_message_that_is_not_start():
    with TestClient(app) as client, client.websocket_connect(f"/media/frejun/{uuid.uuid4()}") as ws:
        ws.send_text(json.dumps({"type": "audio", "data": {"audio_b64": ""}}))
        assert ws.receive()["code"] == 4400


@requires_db
def test_media_refuses_an_audio_format_it_cannot_decode():
    with TestClient(app) as client, client.websocket_connect(f"/media/frejun/{uuid.uuid4()}") as ws:
        ws.send_text(_start(sample_rate=16000))
        assert ws.receive()["code"] == 4415


@requires_db
def test_media_closes_for_an_unknown_attempt():
    with TestClient(app) as client, client.websocket_connect(f"/media/frejun/{uuid.uuid4()}") as ws:
        ws.send_text(_start())
        assert ws.receive()["code"] == 4404


@requires_db
def test_media_closes_when_the_start_call_id_is_not_this_attempts_call():
    *_, attempt_id, _ = _run(_make_attempt(provider_call_id="cs_EXPECTED"))
    with TestClient(app) as client, client.websocket_connect(f"/media/frejun/{attempt_id}") as ws:
        ws.send_text(_start(call_id="cs_SOMEONE_ELSE"))
        assert ws.receive()["code"] == 4403


# --- webhooks -----------------------------------------------------------------------


def _signed_post(client, payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/frejun", content=body,
                       headers={"Content-Type": "application/json", "X-Teler-Timestamp": ts, "X-Teler-Signature": sig})


def _event(type_, call_id, **data):
    return {"id": f"evt_{uuid.uuid4().hex}", "type": type_, "api_version": "2026-06-01", "call_id": call_id,
            "data": {"call_id": call_id, **data}}


@requires_db
def test_webhook_rejects_bad_signatures(frejun_settings):
    with TestClient(app) as client:
        assert _signed_post(client, _event("call.answered", "cs_x"), secret="wrong").status_code == 403


@requires_db
def test_webhook_answered_then_no_answer_moves_the_lead_out_of_in_progress(frejun_settings):
    call_id = f"cs_{uuid.uuid4().hex}"
    *_, attempt_id, _ = _run(_make_attempt(provider_call_id=call_id))
    with TestClient(app) as client:
        assert _signed_post(client, _event("call.initiated", call_id)).json() == {"status": "ok"}
        assert _signed_post(client, _event("stream.initiated", call_id)).json()["status"] == "ignored"
        assert _signed_post(client, _event("call.failed", call_id, reason="no_answer")).json() == {"status": "ok"}
    status, outcome, lead_status, _ = _run(_state(attempt_id))
    assert (status, outcome, lead_status) == ("no_answer", "no_answer", "failed")  # retry sweep can requeue it


# --- lifecycle ----------------------------------------------------------------------


@requires_db
def test_finalize_marks_a_qualified_call_completed_and_is_idempotent():
    async def scenario():
        *_, lead_id, attempt_id, conv_id = await _make_attempt(provider_call_id="cs_q", with_conversation=True)
        async with session_scope() as s:
            s.add(Qualification(conversation_id=conv_id, lead_id=lead_id, outcome="qualified", qualified=True,
                                confidence=0.9, script_version=1))
        async with session_scope() as s:
            await finalize_call(s, attempt_id, ended_reason="media_stream_ended", final_state="INTERESTED")
        async with session_scope() as s:  # a later webhook must not change the result
            await finalize_call(s, attempt_id, call_status="completed")
        async with session_scope() as s:
            conv = await s.get(Conversation, conv_id)
            return (*(await _state(attempt_id))[1:3], conv.current_state, conv.ended_at is not None)

    assert _run(scenario()) == ("qualified", "completed", "INTERESTED", True)


@requires_db
def test_finalize_keeps_dnc_suppressed_and_flags_callbacks():
    async def scenario():
        _, _, lead_id, dnc_attempt, _ = await _make_attempt(with_conversation=True)
        async with session_scope() as s:
            (await s.get(Lead, lead_id)).status = "suppressed"
        async with session_scope() as s:
            await finalize_call(s, dnc_attempt, ended_reason="media_stream_ended")

        _, _, lead2, cb_attempt, conv2 = await _make_attempt(with_conversation=True)
        async with session_scope() as s:
            s.add(Callback(lead_id=lead2, conversation_id=conv2, status="pending"))
        async with session_scope() as s:
            await finalize_call(s, cb_attempt, ended_reason="media_stream_ended")
        return (await _state(dnc_attempt))[1:3], (await _state(cb_attempt))[1:3]

    dnc, callback = _run(scenario())
    assert dnc == ("do_not_call", "suppressed")
    assert callback == ("callback_requested", "callback_scheduled")


# --- schedule_callback idempotency ---------------------------------------------------


@requires_db
def test_schedule_callback_twice_in_one_call_keeps_one_row():
    async def scenario():
        tenant_id, campaign_id, lead_id, _, conv_id = await _make_attempt(with_conversation=True)
        for hour in (8, 9):  # confirm, then correct the time
            async with session_scope() as s:
                ctx = ToolContext(session=s, tenant_id=tenant_id, campaign_id=campaign_id, lead_id=lead_id,
                                  conversation_id=conv_id, script_version=1, current_state="CALLBACK",
                                  allowed_tools=frozenset({"schedule_callback"}))
                result = await schedule_callback(ctx, ScheduleCallbackInput(requested_time=f"2026-10-04T0{hour}:00:00+05:30"))
                assert result.success
        async with session_scope() as s:
            rows = (await s.execute(select(Callback).where(Callback.conversation_id == conv_id))).scalars().all()
            return len(rows), rows[0].requested_time.isoformat()

    count, when = _run(scenario())
    assert count == 1
    assert when == "2026-10-04T03:30:00+00:00"  # the corrected 09:00 IST, stored as UTC

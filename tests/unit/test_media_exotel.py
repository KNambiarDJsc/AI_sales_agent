"""Tests for the Exotel media route's correlation logic (apps/api/routers/media.py).

Only the correlation/rejection paths are covered here (no OpenAI calls, so these run
offline) — the "happy path" (a real call_sid resolving to a real CallAttempt and the
agent actually speaking) needs a live OpenAI key and is covered by manual verification
once real Exotel credentials are available (see STATUS.md), not here, to avoid a real
API cost on every test run. Needs the real Postgres from docker-compose (these are
integration tests, not pure-fake-based unit tests) — skipped if it's not reachable.

Deliberately plain `def` tests, not `async def` under pytest-asyncio: FastAPI's
`TestClient.websocket_connect` runs the ASGI app in its own background thread with its
own event loop (via anyio's blocking portal). Nesting that inside a pytest-asyncio test
function's own event loop causes the app's asyncpg connections to end up attached to
the wrong loop across calls ("Future attached to a different loop") — a real footgun
worth documenting, not a bug in the route itself. Plain sync tests, calling
`TestClient`'s already-synchronous API directly, avoid the nested-loop problem entirely.
"""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import app
from database.session import async_session_factory, engine


def _db_reachable() -> bool:
    async def _check() -> bool:
        try:
            async with async_session_factory() as session:
                await session.execute(select(1))
            return True
        except Exception:  # noqa: BLE001
            return False

    return asyncio.run(_check())


requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable in this environment")


@pytest.fixture(autouse=True)
def _reset_engine_pool_per_test():
    """Each test's `with TestClient(app) as client:` spins up its own event loop in a
    background thread. The app's engine/connection pool (database/session.py) is a
    single global created once at import time — without this, a connection acquired
    inside one test's loop gets pooled and handed to the NEXT test's (different) loop,
    and asyncpg/SQLAlchemy cannot use a connection across event loops ("Event loop is
    closed" / "attached to a different loop"). A real server has exactly one loop for
    its whole process lifetime, so this never happens in production — it's purely an
    artifact of running many TestClient-backed tests in one pytest process. Disposing
    the pool before each test forces fresh connections scoped to that test's own loop."""
    asyncio.run(engine.dispose())
    yield


@requires_db
def test_exotel_media_stream_rejects_non_start_first_event():
    with TestClient(app) as client:
        with client.websocket_connect("/media/exotel") as ws:
            ws.send_text(json.dumps({"event": "media", "media": {"payload": "x"}}))
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4400


@requires_db
def test_exotel_media_stream_closes_for_missing_call_sid():
    with TestClient(app) as client:
        with client.websocket_connect("/media/exotel") as ws:
            ws.send_text(json.dumps({"event": "start", "start": {}}))  # no call_sid anywhere
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4400


@requires_db
def test_exotel_media_stream_skips_preliminary_connected_event():
    # Regression test for a real bug caught via a live Exotel test call: Exotel sends
    # a preliminary {"event": "connected"} message before the real "start" event (the
    # same two-step handshake Twilio Media Streams uses). The handler used to treat
    # whatever arrived first as if it had to be "start", so every real call got
    # rejected at "connected" before "start" was ever seen, cutting the call the
    # instant the person picked up. This proves "connected" is now skipped and "start"
    # is still processed (closing 4404 here only because this call_sid is fake).
    with TestClient(app) as client:
        with client.websocket_connect("/media/exotel") as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(
                json.dumps({"event": "start", "start": {"call_sid": f"nonexistent-{uuid.uuid4()}"}})
            )
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4404


@requires_db
def test_exotel_media_stream_closes_for_unknown_call_sid():
    with TestClient(app) as client:
        with client.websocket_connect("/media/exotel") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "event": "start",
                        "stream_sid": "stream-123",
                        "start": {"call_sid": f"nonexistent-{uuid.uuid4()}"},
                    }
                )
            )
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4404


@requires_db
def test_exotel_media_stream_accepts_camel_case_call_sid_field():
    # Robustness: if a future Exotel response uses callSid (Twilio-style camelCase)
    # instead of the documented call_sid, we should still try to correlate rather than
    # reject outright on field-name alone — it'll still 4404 since this call_sid is
    # fake, but that proves the camelCase field was read, not ignored.
    with TestClient(app) as client:
        with client.websocket_connect("/media/exotel") as ws:
            ws.send_text(json.dumps({"event": "start", "start": {"callSid": f"nonexistent-{uuid.uuid4()}"}}))
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4404  # reached the "unknown call_sid" branch, not "missing"


@requires_db
def test_twilio_route_still_reachable_after_exotel_route_added_first():
    # Regression test for the route-ordering bug this file caught: /media/exotel must
    # be registered before /media/{call_attempt_id}, or Starlette routes "/media/exotel"
    # into the UUID-typed path param and rejects it before ever trying the literal
    # route. This proves the Twilio route is still independently reachable with a
    # genuinely non-UUID-colliding path — a random UUID that doesn't exist should 404,
    # not be swallowed by /media/exotel (which only matches the literal "exotel").
    with TestClient(app) as client:
        with client.websocket_connect(f"/media/{uuid.uuid4()}") as ws:
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4404

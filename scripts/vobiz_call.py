"""Vobiz readiness check, simulated call, and ONE controlled real test call.

    python scripts/vobiz_call.py check                  # preflight; dials nothing
    python scripts/vobiz_call.py simulate [happy|dnc|callback|all]
                                                        # Vobiz-protocol call through the
                                                        # public URL; dials nothing
    python scripts/vobiz_call.py call +9198XXXXXXXX [--name N] [--business B]
                                                        # exactly one real call, only if
                                                        # every preflight check passes
    python scripts/vobiz_call.py report [call_attempt_id]   # what happened on a call

`simulate` plays Vobiz's side exactly as its docs describe (vobiz.ai/docs:
call/make-call "Parameters Sent to answer_url", xml/stream/stream-events): POST the
answer_url (form-encoded, Event=StartApp) → <Stream> XML → connect the stream URL →
`start` → 20 ms `media` frames of synthesized speech (L16 8 kHz) — so everything from
the tunnel inward is the production path. Only Vobiz itself and the phone network
are not exercised. Customer speech is synthesized with the configured TTS (local
Kokoro when there's no OpenAI key).

`call` goes through `services/call_worker/dispatch.py:dispatch_next_call` — the worker's
own function — against a dedicated test campaign holding exactly one lead, paused again
afterwards so a running worker never re-dials it. Never prints secrets.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import phonenumbers
import websockets
from sqlalchemy import select

from config.settings import get_settings
from database.models import CallAttempt, Campaign, Lead, Tenant
from database.session import engine, session_scope
from scripts.frejun_call import report as _report
from services.call_worker.dispatch import dispatch_next_call
from speech.tts.factory import get_tts_provider
from telephony.factory import get_telephony_provider
from telephony.vobiz import VobizProvider
from voice.audio.processing import ResampleState, resample_pcm16

TEST_TENANT = "Vobiz Test Tenant"
TEST_CAMPAIGN = "Vobiz Test Campaign"
SIM_CAMPAIGN = "Vobiz Simulation Campaign"
FRAME_MS = 20  # Vobiz sends a media frame every 20 ms (docs: xml/stream/stream-events)
FRAME_BYTES = 8000 * 2 * FRAME_MS // 1000

OK, FAIL, WARN = "PASS", "FAIL", "WARN"


def _line(status: str, what: str, detail: str = "") -> bool:
    print(f"[{status}] {what}" + (f" — {detail}" if detail else ""))
    return status != FAIL


def _provider() -> VobizProvider:
    provider = get_telephony_provider("vobiz")
    assert isinstance(provider, VobizProvider)
    return provider


async def _campaign(session, name: str) -> Campaign:
    settings = get_settings()
    tenant = (await session.execute(select(Tenant).where(Tenant.name == TEST_TENANT))).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=TEST_TENANT)
        session.add(tenant)
        await session.flush()
    campaign = (
        await session.execute(select(Campaign).where(Campaign.tenant_id == tenant.id, Campaign.name == name))
    ).scalar_one_or_none()
    if campaign is None:
        campaign = Campaign(
            tenant_id=tenant.id, name=name, script_id="product-a", script_version=1, status="paused",
            telephony_provider="vobiz", call_window_start="00:00", call_window_end="23:59",
            timezone=settings.default_timezone, max_concurrent_calls=1, max_call_duration_seconds=420,
        )
        session.add(campaign)
        await session.flush()
    return campaign


async def _sim_attempt(name="Naman", business="Test Business") -> tuple[uuid.UUID, str]:
    """A CallAttempt for a simulated call: fake number, never dialled."""
    async with session_scope() as session:
        campaign = await _campaign(session, SIM_CAMPAIGN)
        phone = "+9190000" + str(uuid.uuid4().int)[:5]
        lead = Lead(tenant_id=campaign.tenant_id, campaign_id=campaign.id, phone_e164=phone, raw_phone=phone,
                    dedupe_key=phone, contact_name=name, business_name=business, status="in_progress", attempts_count=1)
        session.add(lead)
        await session.flush()
        call_uuid = str(uuid.uuid4())
        attempt = CallAttempt(campaign_id=campaign.id, lead_id=lead.id, attempt_number=1, provider="vobiz",
                              provider_call_id=call_uuid, status="in_progress")
        session.add(attempt)
        await session.flush()
        return attempt.id, call_uuid


async def _post_answer(attempt_id, call_uuid: str) -> str:
    provider = _provider()
    s = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(provider.answer_url_for(str(attempt_id)), data={
            "Event": "StartApp", "CallUUID": call_uuid, "RequestUUID": call_uuid, "ALegUUID": call_uuid,
            "From": s.vobiz_from_number.lstrip("+"), "To": "919000000000", "Direction": "outbound",
            "CallStatus": "in-progress",
        })
    resp.raise_for_status()
    return resp.text


def _stream_url_from(xml: str) -> str | None:
    start, end = xml.find(">wss://"), xml.find("</Stream>")
    return xml[start + 1:end].strip() if start != -1 and end != -1 else None


class SimulatedVobizStream:
    """Plays Vobiz's side of the bidirectional stream."""

    def __init__(self, ws, call_uuid: str):
        self.ws, self.call_uuid = ws, call_uuid
        self.stream_id = str(uuid.uuid4())
        self.seq = 0
        self.chunk = 0
        self.outbox = bytearray()
        self.rx_bytes = 0
        self.rx_msgs = 0
        self.last_rx = 0.0
        self.clears = 0
        self.bad_messages: list[str] = []
        self.closed_by_server: int | None = None
        self.stop = asyncio.Event()

    async def send_start(self):
        await self.ws.send(json.dumps({
            "sequenceNumber": self.seq, "event": "start", "extra_headers": "{}",
            "start": {"callId": self.call_uuid, "streamId": self.stream_id, "accountId": "0", "tracks": ["inbound"],
                      "mediaFormat": {"encoding": "audio/x-l16", "sampleRate": 8000}},
        }))

    async def sender(self):
        next_at = time.monotonic()
        while not self.stop.is_set():
            if len(self.outbox) >= FRAME_BYTES:
                frame = bytes(self.outbox[:FRAME_BYTES])
                del self.outbox[:FRAME_BYTES]
            elif self.outbox:
                frame = bytes(self.outbox) + b"\x00" * (FRAME_BYTES - len(self.outbox))
                self.outbox.clear()
            else:
                frame = b"\x00" * FRAME_BYTES
            self.seq += 1
            self.chunk += 1
            try:
                await self.ws.send(json.dumps({
                    "sequenceNumber": self.seq, "streamId": self.stream_id, "event": "media", "extra_headers": "{}",
                    "media": {"track": "inbound", "timestamp": str(int(time.time() * 1000)), "chunk": self.chunk,
                              "payload": base64.b64encode(frame).decode()},
                }))
            except websockets.ConnectionClosed:
                return
            next_at += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    async def receiver(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("event") == "playAudio":
                    media = msg.get("media") or {}
                    audio = base64.b64decode(media.get("payload", ""))
                    if (msg.get("streamId") != self.stream_id or media.get("contentType") != "audio/x-l16"
                            or media.get("sampleRate") != 8000 or len(audio) % 2):
                        self.bad_messages.append(str(msg)[:100])
                    self.rx_bytes += len(audio)
                    self.rx_msgs += 1
                    self.last_rx = time.monotonic()
                elif msg.get("event") == "clearAudio":
                    self.clears += 1
                else:
                    self.bad_messages.append(str(msg)[:100])
        except websockets.ConnectionClosed as exc:
            self.closed_by_server = exc.rcvd.code if exc.rcvd else -1
        else:
            if not self.stop.is_set():
                self.closed_by_server = self.ws.close_code
        self.stop.set()

    async def agent_turn(self, timeout=40.0, quiet=2.0) -> tuple[float | None, float]:
        start_bytes, t0 = self.rx_bytes, time.monotonic()
        while self.rx_bytes == start_bytes and time.monotonic() - t0 < timeout and not self.stop.is_set():
            await asyncio.sleep(0.05)
        if self.rx_bytes == start_bytes:
            return None, 0.0
        first = time.monotonic() - t0
        while time.monotonic() - self.last_rx < quiet and not self.stop.is_set():
            await asyncio.sleep(0.05)
        return first, (self.rx_bytes - start_bytes) / 16000

    async def say(self, pcm8k: bytes):
        self.outbox.extend(pcm8k)
        while self.outbox and not self.stop.is_set():
            await asyncio.sleep(0.05)


async def _speech_8k(text: str) -> bytes:
    """Customer speech from the configured TTS (OpenAI, or local Kokoro), as L16 8 kHz."""
    tts = get_tts_provider()
    pcm = b"".join([chunk async for chunk in tts.synthesize_stream(text)])
    return resample_pcm16(pcm, tts.sample_rate_hz, 8000, ResampleState())


# ------------------------------------------------------------------------------- check


async def check(include_media: bool = True) -> bool:
    s = get_settings()
    ok = True
    print("Vobiz preflight")
    _line(OK if s.telephony_provider == "vobiz" else WARN, "TELEPHONY_PROVIDER", s.telephony_provider)
    ok &= _line(OK if s.vobiz_auth_id and s.vobiz_auth_token else FAIL, "VOBIZ_AUTH_ID + VOBIZ_AUTH_TOKEN present")
    provider = _provider()

    numbers: list[str] = []
    if s.vobiz_auth_id and s.vobiz_auth_token:
        try:
            balance = await provider.get_balance("INR")
            ok &= _line(OK, "Vobiz API reachable + credentials authenticated",
                        f"available balance ₹{balance.get('available_balance')}")
            listing = await provider.list_numbers()
            items = listing.get("items") or []  # docs: account-phone-number/list-account-phone-numbers
            numbers = [str(n.get("e164") or "") for n in items if isinstance(n, dict)]
            _line(OK if numbers else WARN, "numbers on the account", ", ".join(numbers) or "none listed")
        except Exception as exc:  # noqa: BLE001
            ok &= _line(FAIL, "Vobiz API reachable + credentials authenticated", f"{type(exc).__name__}: {exc}")

    if not s.vobiz_from_number:
        ok &= _line(FAIL, "VOBIZ_FROM_NUMBER set (a Vobiz number — required caller ID)")
    else:
        digits = s.vobiz_from_number.lstrip("+")
        listed = any(n.lstrip("+") == digits for n in numbers)
        _line(OK if listed else WARN, "VOBIZ_FROM_NUMBER is one of the account's numbers", s.vobiz_from_number)

    base = s.effective_public_base_url
    if not base.startswith("https://"):
        return _line(FAIL, "PUBLIC_BASE_URL is an https:// URL", base or "<empty>") and False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            health = await c.get(f"{base}/health/db")
        ok &= _line(OK if health.status_code == 200 else FAIL, "public HTTPS reaches the app + DB", f"{base} → HTTP {health.status_code}")
    except httpx.HTTPError as exc:
        return _line(FAIL, "public HTTPS reaches the app", f"{base}: {type(exc).__name__}") and False

    # Hangup webhook through the public URL: our per-call token is what makes it count.
    probe = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=15) as c:
        good = await c.post(provider.hangup_url_for(probe), data={"Event": "Hangup", "RequestUUID": "preflight"})
        forged = await c.post(f"{base}/webhooks/vobiz?attempt={probe}&token=forged", data={"Event": "Hangup"})
    ok &= _line(OK if good.status_code == 200 else FAIL, "hangup webhook accepts a request with our call token", f"HTTP {good.status_code}")
    ok &= _line(OK if forged.status_code == 403 else FAIL, "hangup webhook rejects a forged token", f"HTTP {forged.status_code}")

    attempt_id, call_uuid = await _sim_attempt()
    try:
        xml = await _post_answer(attempt_id, call_uuid)
    except httpx.HTTPError as exc:
        return _line(FAIL, "answer endpoint answers", type(exc).__name__) and False
    ws_url = _stream_url_from(xml)
    xml_ok = bool(ws_url) and 'bidirectional="true"' in xml and ws_url == provider.stream_url_for(str(attempt_id))
    ok &= _line(OK if xml_ok else FAIL, "answer endpoint returns a bidirectional <Stream> to our media URL")

    if include_media and xml_ok:
        try:
            async with websockets.connect(ws_url, max_size=None, open_timeout=15) as ws:
                stream = SimulatedVobizStream(ws, call_uuid)
                await stream.send_start()
                tasks = [asyncio.create_task(stream.sender()), asyncio.create_task(stream.receiver())]
                first, seconds = await stream.agent_turn(timeout=40, quiet=1.5)
                stream.stop.set()
                await ws.close()
                await asyncio.gather(*tasks, return_exceptions=True)
            _line(OK, "public WSS media endpoint accepts Vobiz's start + media frames")
            got_audio = first is not None and seconds > 0.3 and not stream.bad_messages
            ok &= _line(OK if got_audio else FAIL, "opening line → TTS → playAudio in Vobiz's format",
                        f"first audio after {first:.1f}s, {seconds:.1f}s of L16 8 kHz in {stream.rx_msgs} messages"
                        if first else f"no audio; bad messages: {stream.bad_messages[:2]}")
        except Exception as exc:  # noqa: BLE001
            ok &= _line(FAIL, "public WSS media endpoint", f"{type(exc).__name__}: {exc}")

    print("\nRESULT:", "READY" if ok else "NOT READY")
    return bool(ok)


# ---------------------------------------------------------------------------- simulate


SCENARIOS = {
    "happy": [
        "Yes, this is Naman from Test Business.",
        "Sure, go ahead, I have a couple of minutes.",
        "No, we are not selling online anywhere yet, but yes, I am definitely interested in selling on Amazon.",
        "We make handmade soaps and scented candles. We have about forty products.",
        "Yes, absolutely. Please have your sales team call me.",
        "No, that's everything. Thank you, bye.",
    ],
    "dnc": ["Please stop calling me. Remove my number from your list and never call again."],
    "callback": [
        "Yes, this is Naman.",
        "I'm busy right now. Can you call me back tomorrow at eight in the morning?",
        "Yes, eight AM tomorrow is fine.",
        "Thanks, bye.",
    ],
}


async def simulate(name: str) -> uuid.UUID:
    lines = SCENARIOS[name]
    print(f"\n===== simulated Vobiz call: {name} =====")
    audio = [await _speech_8k(text) for text in lines]
    attempt_id, call_uuid = await _sim_attempt()
    ws_url = _stream_url_from(await _post_answer(attempt_id, call_uuid))
    print("answer → <Stream> to our media URL")
    async with websockets.connect(ws_url, max_size=None, open_timeout=15) as ws:
        stream = SimulatedVobizStream(ws, call_uuid)
        await stream.send_start()
        tasks = [asyncio.create_task(stream.sender()), asyncio.create_task(stream.receiver())]
        first, secs = await stream.agent_turn()
        print(f"[agent] opening line: first audio {first and round(first, 2)}s, {secs:.1f}s")
        for text, pcm in zip(lines, audio):
            if stream.stop.is_set():
                break
            await stream.say(pcm)
            print(f"[customer] {text!r} ({len(pcm) / 16000:.1f}s)")
            first, secs = await stream.agent_turn()
            print(f"[agent] reply: first audio {first and round(first, 2)}s after customer stopped, {secs:.1f}s")
        if not stream.stop.is_set():
            try:
                await asyncio.wait_for(stream.stop.wait(), timeout=15)  # did the agent end the call?
            except asyncio.TimeoutError:
                pass
        stream.stop.set()
        await ws.close()
        await asyncio.gather(*tasks, return_exceptions=True)
    print(f"stream: {stream.rx_msgs} playAudio msgs, {stream.rx_bytes / 16000:.1f}s audio, clears={stream.clears}, "
          f"server closed socket={stream.closed_by_server}, malformed={stream.bad_messages[:2]}")
    await asyncio.sleep(1.5)
    await _report(attempt_id)
    return attempt_id


# ------------------------------------------------------------------------------ report


async def report(attempt_id: uuid.UUID | None) -> None:
    if attempt_id is None:
        async with session_scope() as session:
            latest = (await session.execute(
                select(CallAttempt).where(CallAttempt.provider == "vobiz").order_by(CallAttempt.created_at.desc()).limit(1)
            )).scalar_one_or_none()
        if latest is None:
            print("no Vobiz call attempts")
            return
        attempt_id = latest.id
    await _report(attempt_id)


# -------------------------------------------------------------------------------- call


async def call(phone_e164: str, name: str, business: str) -> int:
    if not await check(include_media=True):
        print("\nNot dialling: fix the failed preflight checks first.")
        return 1
    base = get_settings().effective_public_base_url
    async with session_scope() as session:
        campaign = await _campaign(session, TEST_CAMPAIGN)
        for other in (await session.execute(select(Lead).where(Lead.campaign_id == campaign.id))).scalars():
            if other.status in ("pending", "queued"):
                other.status = "completed"  # exactly one dialable lead: the one below
        for stale in (await session.execute(select(CallAttempt).where(
            CallAttempt.campaign_id == campaign.id, CallAttempt.status.in_(["queued", "dialing", "ringing", "in_progress"]),
        ))).scalars():
            stale.status = "canceled"  # an unfinished earlier test would block concurrency=1
        lead = (await session.execute(
            select(Lead).where(Lead.campaign_id == campaign.id, Lead.dedupe_key == phone_e164)
        )).scalar_one_or_none()
        if lead is None:
            lead = Lead(tenant_id=campaign.tenant_id, campaign_id=campaign.id, phone_e164=phone_e164, raw_phone=phone_e164,
                        dedupe_key=phone_e164, contact_name=name, business_name=business, status="pending")
            session.add(lead)
        else:
            lead.contact_name, lead.business_name, lead.status = name, business, "pending"
        campaign.status = "active"
        await session.flush()
        campaign_id, lead_id = campaign.id, lead.id
        try:
            dispatched = await dispatch_next_call(
                session, campaign, _provider(),
                media_websocket_base_url=f"{base.replace('https://', 'wss://')}/media",
                status_callback_base_url=f"{base}/webhooks",
            )
        finally:
            campaign.status = "paused"
    async with session_scope() as session:
        attempt = (await session.execute(
            select(CallAttempt).where(CallAttempt.campaign_id == campaign_id, CallAttempt.lead_id == lead_id)
            .order_by(CallAttempt.attempt_number.desc()).limit(1)
        )).scalar_one_or_none()
    print(f"\ndispatched: {dispatched}")
    if attempt is not None:
        print(f"call attempt: {attempt.id}  Vobiz call uuid: {attempt.provider_call_id}  status: {attempt.status}")
        if attempt.error:
            print(f"error: {attempt.error}")
        print(f"after the call: python scripts/vobiz_call.py report {attempt.id}")
    return 0 if dispatched else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sim = sub.add_parser("simulate")
    sim.add_argument("scenario", nargs="?", default="happy", choices=[*SCENARIOS, "all"])
    rep = sub.add_parser("report")
    rep.add_argument("attempt_id", nargs="?")
    c = sub.add_parser("call")
    c.add_argument("phone")
    c.add_argument("--name", default="there")
    c.add_argument("--business", default="your business")
    args = parser.parse_args()

    async def run() -> int:
        try:
            if args.cmd == "check":
                return 0 if await check() else 1
            if args.cmd == "simulate":
                for name in (SCENARIOS if args.scenario == "all" else [args.scenario]):
                    await simulate(name)
                return 0
            if args.cmd == "report":
                await report(uuid.UUID(args.attempt_id) if args.attempt_id else None)
                return 0
            parsed = phonenumbers.parse(args.phone, "IN")
            if not phonenumbers.is_valid_number(parsed):
                parser.error(f"not a valid phone number: {args.phone!r}")
            return await call(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), args.name, args.business)
        finally:
            await engine.dispose()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())

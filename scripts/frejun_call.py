"""FreJun readiness check, simulated call, and ONE controlled real test call.

    python scripts/frejun_call.py check                  # preflight; dials nothing
    python scripts/frejun_call.py simulate [happy|dnc|callback|all]
                                                         # FreJun-protocol call through the
                                                         # public URL; dials nothing
    python scripts/frejun_call.py call +9198XXXXXXXX [--name N] [--business B]
                                                         # exactly one real call, only if
                                                         # every preflight check passes
    python scripts/frejun_call.py report [call_attempt_id]   # what happened on a call

`simulate` speaks FreJun's documented media protocol (frejun.com/docs/teler/
media-streaming/websocket-protocol) at our public endpoints exactly as Teler would:
POST flow_url → stream flow → connect ws_url → `start` → `audio` chunks of real
synthesized speech (PCM16 8 kHz, `chunk_size` ms each) — so everything from the tunnel
inward (flow endpoint, media route, VoiceSession, STT, LLM, validator, tools,
qualification, TTS, outbound `audio`/`clear`) is the production path. Only Teler
itself and the phone network are not exercised.

`call` goes through `services/call_worker/dispatch.py:dispatch_next_call` — the
worker's own function — against a dedicated test campaign holding exactly one lead,
paused again afterwards so a running worker never re-dials it. Never prints secrets.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
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
from database.models import (
    CallAttempt, Callback, Campaign, Conversation, Lead, Qualification, Suppression, Tenant, TranscriptSegment, Turn,
)
from database.session import engine, session_scope
from services.call_worker.dispatch import dispatch_next_call
from telephony.factory import get_telephony_provider
from voice.audio.processing import ResampleState, resample_pcm16

TEST_TENANT = "FreJun Test Tenant"
TEST_CAMPAIGN = "FreJun Test Campaign"
SIM_CAMPAIGN = "FreJun Simulation Campaign"

OK, FAIL, WARN = "PASS", "FAIL", "WARN"


# --------------------------------------------------------------------------- helpers


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
            telephony_provider="frejun", call_window_start="00:00", call_window_end="23:59",
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
        call_id = f"cs_SIM{uuid.uuid4().hex[:20].upper()}"
        attempt = CallAttempt(campaign_id=campaign.id, lead_id=lead.id, attempt_number=1, provider="frejun",
                              provider_call_id=call_id, status="in_progress")
        session.add(attempt)
        await session.flush()
        return attempt.id, call_id


def _line(status: str, what: str, detail: str = "") -> bool:
    print(f"[{status}] {what}" + (f" — {detail}" if detail else ""))
    return status != FAIL


# ---------------------------------------------------------------------- FreJun media client


class SimulatedTelerStream:
    """Plays Teler's side of the media WebSocket."""

    def __init__(self, ws, call_id: str, chunk_ms: int):
        self.ws, self.call_id, self.chunk_ms = ws, call_id, chunk_ms
        self.chunk_bytes = 8000 * 2 * chunk_ms // 1000
        self.outbox = bytearray()
        self.stream_id = f"ms_SIM{uuid.uuid4().hex[:12]}"
        self.message_id = 1
        self.rx_bytes = 0
        self.rx_msgs = 0
        self.last_rx = 0.0
        self.clears = 0
        self.chunk_ids: set[str] = set()
        self.bad_messages: list[str] = []
        self.closed_by_server: int | None = None
        self.stop = asyncio.Event()

    async def send_start(self):
        await self.ws.send(json.dumps({
            "type": "start", "account_id": "acc_SIM", "call_app_id": "va_SIM", "call_id": self.call_id,
            "stream_id": self.stream_id, "message_id": self.message_id,
            "data": {"encoding": "audio/l16", "sample_rate": 8000, "channels": 1},
        }))

    async def sender(self):
        next_at = time.monotonic()
        while not self.stop.is_set():
            if len(self.outbox) >= self.chunk_bytes:
                chunk = bytes(self.outbox[: self.chunk_bytes])
                del self.outbox[: self.chunk_bytes]
            elif self.outbox:
                chunk = bytes(self.outbox) + b"\x00" * (self.chunk_bytes - len(self.outbox))
                self.outbox.clear()
            else:
                chunk = b"\x00" * self.chunk_bytes
            self.message_id += 1
            try:
                await self.ws.send(json.dumps({
                    "type": "audio", "stream_id": self.stream_id, "message_id": self.message_id,
                    "data": {"audio_b64": base64.b64encode(chunk).decode()},
                }))
            except websockets.ConnectionClosed:
                return
            next_at += self.chunk_ms / 1000
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    async def receiver(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") == "audio":
                    audio = base64.b64decode(msg.get("audio_b64", ""))
                    if not msg.get("chunk_id") or msg["chunk_id"] in self.chunk_ids or len(audio) % 2:
                        self.bad_messages.append(str(msg)[:80])
                    self.chunk_ids.add(msg.get("chunk_id"))
                    self.rx_bytes += len(audio)
                    self.rx_msgs += 1
                    self.last_rx = time.monotonic()
                elif msg.get("type") == "clear":
                    self.clears += 1
                else:
                    self.bad_messages.append(str(msg)[:80])
        except websockets.ConnectionClosed as exc:
            self.closed_by_server = exc.rcvd.code if exc.rcvd else -1
        else:
            # A clean close (1000) ends `async for` without raising.
            if not self.stop.is_set():
                self.closed_by_server = self.ws.close_code
        self.stop.set()

    async def agent_turn(self, timeout=40.0, quiet=2.0) -> tuple[float | None, float]:
        """Wait for the agent to start and finish talking. Returns (seconds until first
        audio, seconds of audio)."""
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
    from openai import AsyncOpenAI

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.audio.speech.create(model=settings.openai_tts_model, voice="onyx", input=text, response_format="pcm")
    return resample_pcm16(resp.content, 24000, 8000, ResampleState())


async def _post_flow(base: str, attempt_id, call_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{base}/flow/frejun/{attempt_id}", json={
            "call_id": call_id, "account_id": "acc_SIM", "from_number": get_settings().frejun_from_number or "+910000000000",
            "to_number": "+919000000000", "direction": "outbound",
        })
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------------------------- check


async def check(include_media: bool = True) -> bool:
    s = get_settings()
    ok = True
    print("FreJun preflight")
    ok &= _line(OK if s.telephony_provider == "frejun" else WARN, "TELEPHONY_PROVIDER", s.telephony_provider)
    ok &= _line(OK if s.frejun_api_key else FAIL, "FREJUN_API_KEY present")
    ok &= _line(OK if s.frejun_secret else FAIL, "FREJUN_SECRET (webhook signing secret) present")
    ok &= _line(OK if s.openai_api_key else FAIL, "OPENAI_API_KEY present")

    numbers: list[dict] = []
    if s.frejun_api_key:
        async with httpx.AsyncClient(base_url=s.frejun_api_base_url, headers={"X-API-Key": s.frejun_api_key}, timeout=15) as c:
            try:
                resp = await c.get("/virtual-numbers", params={"limit": 100})
                authed = resp.status_code == 200
                ok &= _line(OK if authed else FAIL, "FreJun API reachable + key authenticated", f"HTTP {resp.status_code}")
                if authed:
                    numbers = resp.json().get("data", [])
                    apps = (await c.get("/voice/apps", params={"limit": 100})).json().get("data", [])
                    ok &= _line(OK if numbers else FAIL, "Teler virtual numbers on the account", str(len(numbers)))
                    _line(OK if apps else WARN, "Voice Apps on the account", str(len(apps)))
            except httpx.HTTPError as exc:
                ok &= _line(FAIL, "FreJun API reachable", type(exc).__name__)

    if not s.frejun_from_number:
        ok &= _line(FAIL, "FREJUN_FROM_NUMBER set")
    else:
        match = next((n for n in numbers if n.get("number") == s.frejun_from_number), None)
        ok &= _line(OK if match else FAIL, "FREJUN_FROM_NUMBER is one of the account's numbers", s.frejun_from_number)
        if match is not None:
            app = match.get("voice_app") or {}
            ok &= _line(OK if app else FAIL, "FREJUN_FROM_NUMBER is attached to a Voice App", str(app.get("name") or app.get("id") or ""))

    base = s.effective_public_base_url
    if not base.startswith("https://"):
        return _line(FAIL, "PUBLIC_BASE_URL is an https:// URL", base or "<empty>") and False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            health = await c.get(f"{base}/health/db")
        ok &= _line(OK if health.status_code == 200 else FAIL, "public HTTPS reaches the app + DB", f"{base} → HTTP {health.status_code}")
    except httpx.HTTPError as exc:
        return _line(FAIL, "public HTTPS reaches the app", f"{base}: {type(exc).__name__}") and False

    # Signed webhook through the public URL: proves our verification uses the same
    # secret/scheme Teler signs with (an unknown call id is acknowledged, not applied).
    body = json.dumps({"id": "evt_preflight", "type": "call.answered", "call_id": "cs_PREFLIGHT", "data": {}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(s.frejun_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=15) as c:
        signed = await c.post(f"{base}/webhooks/frejun", content=body, headers={
            "Content-Type": "application/json", "X-Teler-Timestamp": ts, "X-Teler-Signature": sig})
        unsigned = await c.post(f"{base}/webhooks/frejun", content=body, headers={"Content-Type": "application/json"})
    ok &= _line(OK if signed.status_code == 200 else FAIL, "status webhook accepts a correctly signed event", f"HTTP {signed.status_code}")
    ok &= _line(OK if unsigned.status_code == 403 else FAIL, "status webhook rejects an unsigned event", f"HTTP {unsigned.status_code}")

    attempt_id, call_id = await _sim_attempt()
    try:
        flow = await _post_flow(base, attempt_id, call_id)
    except httpx.HTTPError as exc:
        return _line(FAIL, "flow endpoint answers", type(exc).__name__) and False
    expected_ws = f"{base.replace('https://', 'wss://')}/media/frejun/{attempt_id}"
    flow_ok = flow.get("action") == "stream" and flow.get("ws_url") == expected_ws and flow.get("sample_rate") in ("8k", "16k")
    ok &= _line(OK if flow_ok else FAIL, "flow endpoint returns a valid stream Call Flow", json.dumps(flow))

    if include_media and flow_ok:
        try:
            async with websockets.connect(flow["ws_url"], max_size=None, open_timeout=15) as ws:
                stream = SimulatedTelerStream(ws, call_id, int(flow.get("chunk_size", 100)))
                await stream.send_start()
                tasks = [asyncio.create_task(stream.sender()), asyncio.create_task(stream.receiver())]
                first, seconds = await stream.agent_turn(timeout=30, quiet=1.5)
                stream.stop.set()
                await ws.close()
                await asyncio.gather(*tasks, return_exceptions=True)
            _line(OK, "public WSS media endpoint accepts Teler's start + audio messages")
            got_audio = first is not None and seconds > 0.3 and not stream.bad_messages
            ok &= _line(OK if got_audio else FAIL, "VoiceSession → LLM → TTS → outbound audio in Teler's format",
                        f"first audio after {first:.1f}s, {seconds:.1f}s of PCM16 8kHz in {stream.rx_msgs} messages, "
                        f"unique chunk_ids={len(stream.chunk_ids) == stream.rx_msgs}" if first else "no audio")
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
    s = get_settings()
    base = s.effective_public_base_url
    lines = SCENARIOS[name]
    print(f"\n===== simulated FreJun call: {name} =====")
    audio = [await _speech_8k(text) for text in lines]
    attempt_id, call_id = await _sim_attempt()
    flow = await _post_flow(base, attempt_id, call_id)
    print(f"flow → {flow['action']} ws_url={flow['ws_url']}")
    async with websockets.connect(flow["ws_url"], max_size=None, open_timeout=15) as ws:
        stream = SimulatedTelerStream(ws, call_id, int(flow.get("chunk_size", 100)))
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
    print(f"stream: {stream.rx_msgs} outbound audio msgs, {stream.rx_bytes / 16000:.1f}s audio, clears={stream.clears}, "
          f"server closed socket={stream.closed_by_server}, malformed={stream.bad_messages[:2]}")
    await asyncio.sleep(1.5)
    await report(attempt_id)
    return attempt_id


# ------------------------------------------------------------------------------ report


async def report(attempt_id: uuid.UUID | None = None) -> None:
    async with session_scope() as session:
        if attempt_id is None:
            attempt = (await session.execute(
                select(CallAttempt).where(CallAttempt.provider == "frejun").order_by(CallAttempt.created_at.desc()).limit(1)
            )).scalar_one_or_none()
        else:
            attempt = await session.get(CallAttempt, attempt_id)
        if attempt is None:
            print("no FreJun call attempts")
            return
        lead = await session.get(Lead, attempt.lead_id)
        conv = (await session.execute(
            select(Conversation).where(Conversation.call_attempt_id == attempt.id).order_by(Conversation.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        print(f"call attempt {attempt.id}: provider_call_id={attempt.provider_call_id} status={attempt.status} "
              f"outcome={attempt.outcome} error={attempt.error}")
        print(f"lead status: {lead.status}")
        if conv is None:
            print("no conversation (media stream never connected)")
            return
        turns = (await session.execute(select(Turn).where(Turn.conversation_id == conv.id).order_by(Turn.turn_index))).scalars().all()
        segments = (await session.execute(select(TranscriptSegment).where(TranscriptSegment.conversation_id == conv.id))).scalars().all()
        seg_by_turn = {seg.turn_id: seg for seg in segments}
        print(f"conversation {conv.id}: final state={conv.current_state} ended={conv.ended_at is not None} "
              f"reason={conv.ended_reason} turns={len(turns)} transcript segments={len(segments)}")
        for t in turns:
            text = seg_by_turn.get(t.id).text if t.id in seg_by_turn else ""
            if t.speaker == "agent" and t.raw_llm_output:
                o = t.raw_llm_output
                tool = o.get("tool_call") or {}
                extra = f"  [tool={tool.get('name')} args={json.dumps(tool.get('arguments'))[:160]}]" if tool else ""
                facts = f"  [facts={json.dumps(o.get('extracted_facts'))[:120]}]" if o.get("extracted_facts") else ""
                print(f"  AGENT    {t.state:>13} → {o['state']:<13} {text[:90]!r}{extra}{facts}{'  [end_call]' if o.get('end_call') else ''}")
            else:
                print(f"  CUSTOMER {t.state:>13}   {text[:100]!r}")
        for q in (await session.execute(select(Qualification).where(Qualification.conversation_id == conv.id))).scalars():
            print(f"qualification: outcome={q.outcome} qualified={q.qualified} confidence={q.confidence:.2f} "
                  f"facts={q.facts} evidence={q.evidence} followup={q.sales_followup_required}")
        for cb in (await session.execute(select(Callback).where(Callback.conversation_id == conv.id))).scalars():
            print(f"callback: {cb.requested_time} ({cb.timezone}) status={cb.status}")
        for sup in (await session.execute(select(Suppression).where(Suppression.source == f"conversation:{conv.id}"))).scalars():
            print(f"suppression: {sup.reason}")


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
                session, campaign, get_telephony_provider("frejun"),
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
        print(f"call attempt: {attempt.id}  FreJun call_id: {attempt.provider_call_id}  status: {attempt.status}")
        if attempt.error:
            print(f"error: {attempt.error}")
        print(f"after the call: python scripts/frejun_call.py report {attempt.id}")
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

"""Browser-based demo — NOT a telephony path, and not meant to replace one.

Why this exists: the client's telephony options (FreJun, Exotel, Twilio) each need
either carrier KYC for a real Indian number (FreJun/Exotel) or a paid account (Twilio
trial accounts cannot use the custom webhook/streaming our architecture needs — see
STATUS.md's "Real outbound call attempt" section). None of that blocks demonstrating
the actual AI agent, though: this endpoint runs the exact same orchestration stack
(`ConversationEngine`, the real state machine/script, the real tool registry and
qualification engine, the same STT/LLM/TTS providers as a phone call — OpenAI, or the
local Moonshine/Ollama/Kokoro backup) over the browser's own microphone and speakers.

This is a demo harness, not a second production transport: push-to-talk instead of
VAD, and it provisions its own throwaway Tenant/Campaign/Lead/CallAttempt rows per
session rather than going through the campaign/lead-import flow. When real telephony
is available, the call path is `apps/api/routers/media.py`, not this file.

Audio: the page captures raw PCM16 at 16 kHz with an AudioWorklet and streams it while
the button is held — what both STT backends take natively (no compressed-audio
decoding, which the local backend can't do, and no encode/upload step after release).

Turn handling:
- One turn at a time: a lock serialises every `engine.run_turn` (the engine's state
  machine and history are not safe to run concurrently anyway).
- The newest utterance wins. Starting a new utterance stops any reply that is playing
  (TTS cancelled, `interrupt` sent so the page drops queued audio) and mutes replies
  still being generated for older turns. An utterance superseded while waiting for
  the lock is skipped entirely.
- The page cancels accidental clicks and silent recordings, and plays reply audio as
  it streams in.
- Each turn reports where its time went and which backend served each stage (`timing`).

Protocol (JSON text frames unless noted):
  client → server: utterance_start; binary PCM16 16 kHz mono chunks while recording;
                   utterance_end | utterance_cancel.
  server → client: info / error / customer_text / agent_text / audio_start
                   {sample_rate} / binary PCM16 chunks / audio_end / interrupt / timing /
                   call_ended.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid

import openai
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from config.settings import PROMPTS_DIR, get_settings
from database.models import CallAttempt, Campaign, Conversation, Lead, Tenant
from database.session import session_scope
from llm.factory import get_llm_provider
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.prompts import load_campaign_prompt
from orchestrator.state_machine import StateMachine, load_script_by_id
from services.call_worker.lifecycle import finalize_call
from speech.stt.factory import get_stt_provider
from speech.tts.factory import get_tts_provider
from voice.session.speech_pipeline import synthesize_phrases

logger = logging.getLogger(__name__)
router = APIRouter(tags=["demo"])

DEMO_TENANT_NAME = "Browser Demo Tenant"
DEMO_CAMPAIGN_NAME = "Browser Demo Campaign"
DEMO_SCRIPT_ID = "product-a"

# Server-side backstop for accidental clicks (the page filters them first): less than
# 0.3 s of 16 kHz PCM16 can't hold a real utterance.
MIN_UTTERANCE_BYTES = int(0.3 * 16000 * 2)


async def _get_or_create_demo_campaign(session) -> tuple[Tenant, Campaign]:
    result = await session.execute(select(Tenant).where(Tenant.name == DEMO_TENANT_NAME))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=DEMO_TENANT_NAME)
        session.add(tenant)
        await session.flush()

    result = await session.execute(
        select(Campaign).where(Campaign.tenant_id == tenant.id, Campaign.name == DEMO_CAMPAIGN_NAME)
    )
    campaign = result.scalar_one_or_none()
    if campaign is None:
        campaign = Campaign(
            tenant_id=tenant.id,
            name=DEMO_CAMPAIGN_NAME,
            script_id=DEMO_SCRIPT_ID,
            script_version=1,
            status="active",
            telephony_provider="browser_demo",
            call_window_start="00:00",
            call_window_end="23:59",
            timezone="Asia/Kolkata",
            max_concurrent_calls=10,
            max_call_duration_seconds=900,
        )
        session.add(campaign)
        await session.flush()
    return tenant, campaign


def _problem_text(exc: BaseException) -> str:
    """A plain-language reason for a failure the person testing can act on. With
    AI_BACKEND=auto, OpenAI account problems fail over to the local models instead of
    landing here; these are what's left (OpenAI-only mode, or the local backend down)."""
    if isinstance(exc, openai.RateLimitError) and "insufficient_quota" in str(exc):
        return "OpenAI account is out of credits — add credits at platform.openai.com → Billing, or set AI_BACKEND=auto/local."
    if isinstance(exc, openai.AuthenticationError):
        return "OpenAI rejected the API key — fix OPENAI_API_KEY in .env, or set AI_BACKEND=auto/local."
    if isinstance(exc, FileNotFoundError):
        return f"Local model files missing — run: python scripts/setup_local_models.py ({exc})"
    if "11434" in str(exc) or "ollama" in str(exc).lower() or type(exc).__name__ == "ConnectError":
        return "Local LLM (Ollama) isn't reachable — start Ollama, then reload this page."
    return f"{type(exc).__name__}: {str(exc)[:200]}"


@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return DEMO_HTML


@router.websocket("/demo/ws")
async def demo_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()
    tts_provider = get_tts_provider()
    stt_provider = get_stt_provider()
    llm_provider = get_llm_provider()

    async with session_scope() as session:
        tenant, campaign = await _get_or_create_demo_campaign(session)

        lead = Lead(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            phone_e164="+10000000000",
            raw_phone="browser-demo",
            dedupe_key=f"demo-{uuid.uuid4().hex[:14]}",  # dedupe_key is VARCHAR(20)
            contact_name="Demo User",
            business_name="Demo Business",
            status="in_progress",
        )
        session.add(lead)
        await session.flush()

        attempt = CallAttempt(
            campaign_id=campaign.id, lead_id=lead.id, attempt_number=1, provider="browser_demo", status="in_progress"
        )
        session.add(attempt)
        await session.flush()

        script = load_script_by_id(campaign.script_id)
        conversation = Conversation(
            call_attempt_id=attempt.id, script_id=script.script_id, script_version=script.version, current_state="INTRO"
        )
        session.add(conversation)
        await session.flush()

        campaign_prompt_path = campaign.config.get("campaign_prompt_path") or (PROMPTS_DIR / "campaign_prompt_template.yaml")
        campaign_prompt = load_campaign_prompt(campaign_prompt_path)

        tenant_id, campaign_id, lead_id, conversation_id = tenant.id, campaign.id, lead.id, conversation.id
        attempt_id = attempt.id
        campaign_timezone = campaign.timezone

    context = ConversationContext(
        conversation_id=str(conversation_id),
        campaign_id=str(campaign_id),
        lead_id=str(lead_id),
        script_id=script.script_id,
        script_version=script.version,
        current_state="INTRO",
        lead_fields={"contact_name": lead.contact_name, "business_name": lead.business_name},
        campaign_prompt=campaign_prompt,
        timezone=campaign_timezone,
    )
    state_machine = StateMachine(script, current_state="INTRO")
    engine = ConversationEngine(llm_provider, state_machine, context)

    loop = asyncio.get_running_loop()
    turn_lock = asyncio.Lock()
    latest_turn = 0  # id of the newest utterance received
    muted_up_to = -1  # replies for turns <= this are not spoken (barge-in)
    speak_task: asyncio.Task | None = None
    turn_tasks: set[asyncio.Task] = set()
    recording = None  # (stt_stream, bytes_received) for the utterance being recorded
    call_ended = False

    async def send_json(payload: dict) -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json(payload)

    async def interrupt() -> None:
        """Stop whatever the agent is saying right now (barge-in)."""
        nonlocal speak_task, muted_up_to
        muted_up_to = latest_turn
        task, speak_task = speak_task, None
        if task is not None and not task.done():
            await tts_provider.cancel()
            task.cancel()
            await asyncio.wait([task])
            await send_json({"type": "interrupt"})

    def backends() -> dict:
        return {
            "stt_backend": getattr(stt_provider, "backend", "?"),
            "llm_backend": getattr(llm_provider, "backend", "?"),
            "tts_backend": getattr(tts_provider, "backend", "?"),
        }

    async def speak(phrases, turn_id: int, timing: dict) -> None:
        """Plays a reply that may still be being written: each phrase's text is shown
        and its audio synthesized as soon as the phrase exists."""

        async def shown(source):
            first_phrase = True
            async for phrase in source:
                await send_json({"type": "agent_text" if first_phrase else "agent_text_append", "text": phrase})
                first_phrase = False
                yield phrase

        await send_json({"type": "audio_start", "sample_rate": tts_provider.sample_rate_hz})
        first = True
        audio = synthesize_phrases(tts_provider, shown(phrases))
        try:
            async for chunk in audio:
                if first:
                    first = False
                    now = loop.time()
                    timing["tts"] = now - timing.pop("_speech_ready_at", now)
                    timing["server"] = now - timing.pop("_received_at", now)
                    numbers = {k: round(v, 3) for k, v in timing.items()}
                    await send_json({"type": "timing", **numbers, **backends()})
                    logger.info("demo_turn_timing", extra={"turn": turn_id, **numbers, **backends()})
                await websocket.send_bytes(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the text is already on screen; don't kill the session over audio
            logger.exception("demo_tts_failed", extra={"turn": turn_id})
            await send_json({"type": "error", "text": "Speech audio failed: " + _problem_text(exc)})
        finally:
            await audio.aclose()
        await send_json({"type": "audio_end"})

    async def run_turn(turn_id: int, customer_text: str, timing: dict) -> None:
        nonlocal speak_task, call_ended

        async def on_speech_stream(phrases) -> None:
            # Fires with the reply's first phrase while the LLM is still writing the
            # rest. Start speaking and return at once — never await TTS here (it would
            # sit inside the engine's LLM deadline).
            nonlocal speak_task
            now = loop.time()
            timing["llm"] = now - timing.pop("_llm_started_at", now)  # time to the first phrase
            timing["_speech_ready_at"] = now
            if turn_id <= muted_up_to:
                logger.info("demo_reply_muted_by_barge_in", extra={"turn": turn_id})
                return
            speak_task = asyncio.create_task(speak(phrases, turn_id, timing))

        timing["_llm_started_at"] = loop.time()
        async with session_scope() as turn_session:
            result = await engine.run_turn(
                customer_text,
                session=turn_session,
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                lead_id=lead_id,
                conversation_id=conversation_id,
                on_speech_stream=on_speech_stream,
            )
        task = speak_task
        if task is not None:
            await asyncio.wait([task])  # finished, or cancelled by a barge-in
        if result.end_call:
            call_ended = True
            await send_json({"type": "call_ended"})

    async def handle_utterance(turn_id: int, stream, received_at: float) -> None:
        try:
            async with turn_lock:
                if turn_id != latest_turn or call_ended:
                    return  # superseded by a newer utterance while waiting
                timing: dict = {"_received_at": received_at}
                try:
                    final = await asyncio.wait_for(stream.receive_final(), timeout=settings.stt_timeout_seconds + 4)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("demo_transcription_failed", extra={"turn": turn_id})
                    await send_json({"type": "error", "text": "Transcription failed: " + _problem_text(exc)})
                    return
                timing["stt"] = loop.time() - received_at
                transcript = final.text.strip() if final else ""
                if turn_id != latest_turn:
                    return
                if not transcript:
                    await send_json({"type": "info", "text": "(didn't catch that — try again)"})
                    return
                await send_json({"type": "customer_text", "text": transcript})
                await run_turn(turn_id, transcript, timing)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the demo session
            logger.exception("demo_turn_failed", extra={"turn": turn_id})
            await send_json({"type": "error", "text": "That turn failed: " + _problem_text(exc)})
        finally:
            with contextlib.suppress(Exception):
                await stream.close()

    async def opening_line() -> None:
        async with turn_lock:
            try:
                await run_turn(0, "", {"_received_at": loop.time()})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("demo_turn_failed", extra={"turn": 0})
                await send_json({"type": "error", "text": "The agent couldn't start: " + _problem_text(exc)})

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        turn_tasks.add(task)
        task.add_done_callback(turn_tasks.discard)

    try:
        await send_json({"type": "info", "text": "Connected — the agent will greet you now."})
        spawn(opening_line())

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if recording is not None:
                    stream, received = recording
                    await stream.send_audio(message["bytes"])
                    recording = (stream, received + len(message["bytes"]))
                continue
            if not message.get("text"):
                continue
            try:
                kind = json.loads(message["text"]).get("type")
            except (json.JSONDecodeError, AttributeError):
                continue

            if kind == "utterance_start":
                await interrupt()  # pressing talk always cuts the agent off
                if recording is not None:  # a previous press never ended — discard it
                    with contextlib.suppress(Exception):
                        await recording[0].close()
                recording = (await stt_provider.start_stream(), 0)
            elif kind in ("utterance_end", "utterance_cancel"):
                if recording is None:
                    continue
                stream, received = recording
                recording = None
                if kind == "utterance_cancel" or call_ended or received < MIN_UTTERANCE_BYTES:
                    with contextlib.suppress(Exception):
                        await stream.close()
                    if kind == "utterance_end" and not call_ended:
                        await send_json({"type": "info", "text": "(too short — hold the button while you speak)"})
                    continue
                received_at = loop.time()
                await interrupt()  # mutes every older turn...
                latest_turn += 1  # ...then this one gets an id above the muted range
                spawn(handle_utterance(latest_turn, stream, received_at))
            elif kind == "barge_in":
                await interrupt()
    except WebSocketDisconnect:
        pass
    finally:
        for task in list(turn_tasks):
            task.cancel()
        if turn_tasks:
            await asyncio.wait(list(turn_tasks))
        if speak_task is not None and not speak_task.done():
            speak_task.cancel()
            await asyncio.wait([speak_task])
        if recording is not None:
            with contextlib.suppress(Exception):
                await recording[0].close()
        await tts_provider.close()
        async with session_scope() as session:
            await finalize_call(
                session, attempt_id, call_status="completed", ended_reason="demo_ended", final_state=engine.current_state
            )


DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Voice Sales Agent — Live Demo</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 680px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h2 { margin-bottom: 4px; }
  p.sub { color: #666; margin-top: 0; }
  #backend { font-size: 13px; color: #555; margin: 6px 0 0; }
  #log { border: 1px solid #ddd; border-radius: 10px; padding: 14px; height: 380px; overflow-y: auto; margin-top: 16px; background: #fafafa; }
  .msg { margin: 10px 0; line-height: 1.4; }
  .agent { color: #1a5fb4; }
  .customer { color: #26a269; }
  .info { color: #888; font-style: italic; }
  .error { color: #c01c28; font-weight: 600; }
  .timing { color: #9a6700; font-size: 13px; margin-top: -6px; }
  .controls { display: flex; gap: 12px; margin-top: 16px; }
  button { font-size: 16px; padding: 12px 22px; border-radius: 10px; border: none; cursor: pointer; touch-action: none; user-select: none; }
  #startBtn { background: #1a1a1a; color: white; }
  #talkBtn { background: #1a5fb4; color: white; flex: 1; }
  #talkBtn:disabled { background: #ccc; cursor: not-allowed; }
  #talkBtn.recording { background: #c01c28; }
  #status { margin-top: 10px; color: #444; font-size: 14px; min-height: 18px; }
</style>
</head>
<body>
<h2>Voice Sales Agent — Live Demo</h2>
<p class="sub">Talks to the real backend (state machine, qualification, tools) over your mic/speakers. Hold the button (or the space bar) while you speak; pressing it while the agent talks interrupts it.</p>
<div id="backend">Checking AI backend…</div>
<div class="controls">
  <button id="startBtn">Start Call</button>
  <button id="talkBtn" disabled>Hold to Talk</button>
</div>
<div id="status"></div>
<div id="log"></div>
<script>
const MIN_RECORDING_MS = 350;   // shorter presses are accidental clicks
const MIN_LEVEL = 0.01;         // peak RMS below this = nothing was said
const CAPTURE_RATE = 16000;     // what both STT backends take natively
const WORKLET = `
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(1600);  // 100 ms at 16 kHz
    this.n = 0;
    this.port.onmessage = () => {     // "flush": send whatever is buffered
      if (this.n) this.port.postMessage(this.buf.slice(0, this.n).buffer);
      this.n = 0;
      this.port.postMessage('flushed');
    };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      const v = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice().buffer); this.n = 0; }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);`;

let ws, playCtx, micCtx, micStream, captureNode, callEnded = false;
let rec = null, flushResolve = null;
let sources = [], nextTime = 0, acceptAudio = false, carry = null, sampleRate = 24000;
let releaseAt = null, waitingFirstAudio = false, lastTimingEl = null, lastAgentEl = null;

const talkBtn = document.getElementById('talkBtn');
const startBtn = document.getElementById('startBtn');
function setStatus(t) { document.getElementById('status').textContent = t; }
function log(cls, text) {
  const div = document.getElementById('log');
  const p = document.createElement('p');
  p.className = 'msg ' + cls;
  p.textContent = (cls === 'agent' ? 'Agent: ' : cls === 'customer' ? 'You: ' : '') + text;
  div.appendChild(p);
  div.scrollTop = div.scrollHeight;
  return p;
}

fetch('/health/ai').then((r) => r.json()).then((h) => {
  const el = document.getElementById('backend');
  const parts = Object.entries(h.components).map(([k, v]) => k.toUpperCase() + ': ' + v);
  el.textContent = 'AI backend (' + h.mode + '): ' + parts.join(' · ') + (h.openai.reason ? '  —  OpenAI unavailable: ' + h.openai.reason : '');
}).catch(() => { document.getElementById('backend').textContent = ''; });

function stopPlayback() {
  for (const s of sources) { try { s.stop(); } catch (e) {} }
  sources = []; nextTime = 0; acceptAudio = false; carry = null;
}

function playChunk(data) {
  if (!acceptAudio) return;  // audio from an interrupted reply
  let bytes = new Uint8Array(data);
  if (carry) { const m = new Uint8Array(carry.length + bytes.length); m.set(carry); m.set(bytes, carry.length); bytes = m; carry = null; }
  if (bytes.length % 2) { carry = bytes.slice(bytes.length - 1); bytes = bytes.slice(0, bytes.length - 1); }
  if (!bytes.length) return;
  const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
  const f = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) f[i] = samples[i] / 32768;
  const buf = playCtx.createBuffer(1, f.length, sampleRate);
  buf.copyToChannel(f, 0);
  const src = playCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playCtx.destination);
  const at = Math.max(playCtx.currentTime + 0.02, nextTime);
  src.start(at);
  nextTime = at + buf.duration;
  sources.push(src);
  src.onended = () => {
    sources = sources.filter((s) => s !== src);
    if (!sources.length && !acceptAudio && !rec && !callEnded) setStatus('Your turn — hold to talk');
  };
  if (waitingFirstAudio && releaseAt !== null) {
    waitingFirstAudio = false;
    const heard = (performance.now() - releaseAt) / 1000 + (at - playCtx.currentTime);
    const el = lastTimingEl || log('timing', '');
    el.textContent = '⏱ you stopped → agent audio: ' + heard.toFixed(2) + 's' + (el.dataset.detail || '');
  }
  setStatus('Agent speaking… (press to interrupt)');
}

function onMessage(event) {
  if (typeof event.data !== 'string') { playChunk(event.data); return; }
  const msg = JSON.parse(event.data);
  if (msg.type === 'agent_text') { lastAgentEl = log('agent', msg.text); lastTimingEl = null; }
  else if (msg.type === 'agent_text_append') { if (lastAgentEl) lastAgentEl.textContent += ' ' + msg.text; else lastAgentEl = log('agent', msg.text); }
  else if (msg.type === 'customer_text') { log('customer', msg.text); setStatus('Thinking…'); }
  else if (msg.type === 'info') { log('info', msg.text); if (!rec) setStatus('Your turn — hold to talk'); }
  else if (msg.type === 'error') { log('error', '⚠ ' + msg.text); setStatus(msg.text); }
  else if (msg.type === 'audio_start') { stopPlayback(); sampleRate = msg.sample_rate; acceptAudio = true; }
  else if (msg.type === 'audio_end') { acceptAudio = false; }
  else if (msg.type === 'interrupt') { stopPlayback(); }
  else if (msg.type === 'timing') {
    const tag = (b) => (b === 'local' ? 'local' : b === 'openai' ? 'OpenAI' : b);
    const parts = [];
    if (msg.stt !== undefined) parts.push('STT ' + msg.stt.toFixed(2) + ' ' + tag(msg.stt_backend));
    if (msg.llm !== undefined) parts.push('LLM ' + msg.llm.toFixed(2) + ' ' + tag(msg.llm_backend));
    if (msg.tts !== undefined) parts.push('TTS ' + msg.tts.toFixed(2) + ' ' + tag(msg.tts_backend));
    lastTimingEl = log('timing', '');
    lastTimingEl.dataset.detail = '  (' + parts.join(' · ') + ')';
    lastTimingEl.textContent = '⏱ server ' + (msg.server || 0).toFixed(2) + 's' + lastTimingEl.dataset.detail;
  }
  else if (msg.type === 'call_ended') {
    callEnded = true; log('info', '--- call ended ---'); talkBtn.disabled = true; setStatus('Call ended');
  }
}

function onCapturedAudio(e) {
  if (e.data === 'flushed') { if (flushResolve) { flushResolve(); flushResolve = null; } return; }
  if (!rec) return;
  const pcm = new Int16Array(e.data);
  let s = 0; for (let i = 0; i < pcm.length; i++) s += (pcm[i] / 32768) ** 2;
  rec.maxLevel = Math.max(rec.maxLevel, Math.sqrt(s / Math.max(1, pcm.length)));
  if (ws && ws.readyState === 1) ws.send(e.data);
}

function startTalking() {
  if (!ws || ws.readyState !== 1 || rec || callEnded || !captureNode) return;
  stopPlayback();                                        // barge-in, locally...
  ws.send(JSON.stringify({ type: 'utterance_start' }));  // ...and on the server
  rec = { startedAt: performance.now(), maxLevel: 0 };
  talkBtn.classList.add('recording');
  talkBtn.textContent = 'Recording… release to send';
  setStatus('Listening…');
}

async function stopTalking() {
  if (!rec) return;
  const r = rec;
  talkBtn.classList.remove('recording');
  talkBtn.textContent = 'Hold to Talk';
  await new Promise((resolve) => { flushResolve = resolve; captureNode.port.postMessage('flush'); setTimeout(resolve, 200); });
  rec = null;
  const ms = performance.now() - r.startedAt;
  if (ms < MIN_RECORDING_MS || r.maxLevel < MIN_LEVEL) {
    ws.send(JSON.stringify({ type: 'utterance_cancel' }));
    log('info', ms < MIN_RECORDING_MS ? '(too short — hold the button while you speak)' : "(didn't hear anything — check your microphone)");
    setStatus('Your turn — hold to talk');
    return;
  }
  releaseAt = performance.now(); waitingFirstAudio = true;
  ws.send(JSON.stringify({ type: 'utterance_end' }));
  setStatus('Thinking…');
}

startBtn.onclick = async () => {
  startBtn.disabled = true; callEnded = false;
  playCtx = playCtx || new (window.AudioContext || window.webkitAudioContext)();
  await playCtx.resume();
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
    if (!micCtx) {
      micCtx = new AudioContext({ sampleRate: CAPTURE_RATE });   // the browser resamples the mic to 16 kHz
      await micCtx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' })));
      captureNode = new AudioWorkletNode(micCtx, 'pcm-capture');
      captureNode.port.onmessage = onCapturedAudio;
      const sink = micCtx.createGain(); sink.gain.value = 0;     // keep the graph pulling audio
      captureNode.connect(sink); sink.connect(micCtx.destination);
    }
    micCtx.createMediaStreamSource(micStream).connect(captureNode);
    await micCtx.resume();
  } catch (err) {
    log('error', '⚠ Microphone unavailable: ' + err.message); startBtn.disabled = false; return;
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/demo/ws');
  ws.binaryType = 'arraybuffer';
  ws.onmessage = onMessage;
  ws.onopen = () => { talkBtn.disabled = false; setStatus('Connecting…'); };
  ws.onclose = () => { startBtn.disabled = false; talkBtn.disabled = true; stopPlayback(); rec = null; if (!callEnded) setStatus('Disconnected'); };
};

talkBtn.addEventListener('pointerdown', (e) => { e.preventDefault(); talkBtn.setPointerCapture(e.pointerId); startTalking(); });
talkBtn.addEventListener('pointerup', (e) => { e.preventDefault(); stopTalking(); });
talkBtn.addEventListener('pointercancel', () => stopTalking());
talkBtn.addEventListener('contextmenu', (e) => e.preventDefault());
document.addEventListener('keydown', (e) => { if (e.code === 'Space' && !e.repeat && !talkBtn.disabled) { e.preventDefault(); startTalking(); } });
document.addEventListener('keyup', (e) => { if (e.code === 'Space') { e.preventDefault(); stopTalking(); } });
</script>
</body>
</html>
"""

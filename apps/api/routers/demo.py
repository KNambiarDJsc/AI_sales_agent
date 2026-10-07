"""Browser-based demo — NOT a telephony path, and not meant to replace one.

Why this exists: the client's telephony options (FreJun, Exotel, Twilio) each need
either carrier KYC for a real Indian number (FreJun/Exotel) or a paid account (Twilio
trial accounts cannot use the custom webhook/streaming our architecture needs — see
STATUS.md's "Real outbound call attempt" section). None of that blocks demonstrating
the actual AI agent, though: this endpoint runs the exact same orchestration stack
(`ConversationEngine`, the real state machine/script, the real tool registry and
qualification engine, real OpenAI STT/TTS) over the browser's own microphone and
speakers instead of a phone line — free, no account, no KYC, testable immediately.

This is a demo harness, not a second production transport: push-to-talk instead of
VAD, and it provisions its own throwaway Tenant/Campaign/Lead/CallAttempt rows per
session rather than going through the campaign/lead-import flow. When real telephony
is available, the call path is `apps/api/routers/media.py`, not this file.

Turn handling (rewritten after "pressing Hold to Talk twice made the agent answer
twice at once"):
- One turn at a time: a lock serialises every `engine.run_turn` (the engine's state
  machine and history are not safe to run concurrently anyway).
- The newest utterance wins. A new utterance, or the client pressing the talk button
  (`barge_in`), stops any reply that is playing (TTS cancelled, `interrupt` sent so the
  page drops queued audio) and mutes replies still being generated for older turns.
  An utterance that was superseded while waiting for the lock is skipped entirely.
- The page discards accidental clicks and silent recordings before sending, gives
  each recording its own MediaRecorder (a shared chunk buffer is how two quick presses
  used to merge), and plays reply audio as it streams in instead of after the whole
  reply has been synthesized.
- Each turn reports where its time went (`timing`), shown under the reply.

Protocol (JSON text frames unless noted):
  client → server: {"type": "hello", "mime": "..."}; utterance audio as one binary
                   frame per utterance; {"type": "barge_in"} when the talk button is
                   pressed.
  server → client: info / customer_text / agent_text / audio_start {sample_rate} /
                   binary PCM16 chunks / audio_end / interrupt / timing / call_ended.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from config.settings import PROMPTS_DIR, get_settings
from database.models import CallAttempt, Campaign, Conversation, Lead, Tenant
from database.session import session_scope
from llm.openai import OpenAILLMProvider
from llm.openai_client import get_openai_client
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.prompts import load_campaign_prompt
from orchestrator.state_machine import StateMachine, load_script_by_id
from services.call_worker.lifecycle import finalize_call
from speech.tts.openai import OPENAI_TTS_SAMPLE_RATE_HZ, OpenAITTSProvider

logger = logging.getLogger(__name__)
router = APIRouter(tags=["demo"])

DEMO_TENANT_NAME = "Browser Demo Tenant"
DEMO_CAMPAIGN_NAME = "Browser Demo Campaign"
DEMO_SCRIPT_ID = "product-a"

# Anything smaller can't hold a real utterance (≈0.3 s of opus already exceeds it);
# the page filters accidental clicks first, this is the server-side backstop.
MIN_UTTERANCE_BYTES = 1500

_MIME_EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}


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


async def _transcribe_blob(blob: bytes, extension: str) -> str:
    settings = get_settings()
    audio_file = io.BytesIO(blob)
    audio_file.name = f"utterance.{extension}"
    extra = {"language": settings.stt_language} if settings.stt_language else {}
    try:
        response = await asyncio.wait_for(
            get_openai_client().audio.transcriptions.create(
                model=settings.openai_stt_model, file=audio_file, response_format="json", **extra
            ),
            timeout=settings.stt_timeout_seconds,
        )
        return (getattr(response, "text", "") or "").strip()
    except Exception:  # noqa: BLE001 - a failed transcription must not kill the demo session
        logger.exception("demo_transcription_failed")
        return ""


@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return DEMO_HTML


@router.websocket("/demo/ws")
async def demo_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    tts_provider = OpenAITTSProvider()

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
    engine = ConversationEngine(OpenAILLMProvider(), state_machine, context)

    loop = asyncio.get_running_loop()
    turn_lock = asyncio.Lock()
    latest_turn = 0  # id of the newest utterance received
    muted_up_to = -1  # replies for turns <= this are not spoken (barge-in)
    speak_task: asyncio.Task | None = None
    turn_tasks: set[asyncio.Task] = set()
    extension = "webm"
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

    async def speak(text: str, turn_id: int, timing: dict) -> None:
        await send_json({"type": "agent_text", "text": text})
        await send_json({"type": "audio_start", "sample_rate": OPENAI_TTS_SAMPLE_RATE_HZ})
        first = True
        try:
            async for chunk in tts_provider.synthesize_stream(text):
                if first:
                    first = False
                    now = loop.time()
                    timing["tts"] = now - timing.pop("_speech_ready_at", now)
                    timing["server"] = now - timing.pop("_received_at", now)
                    await send_json({"type": "timing", **{k: round(v, 3) for k, v in timing.items()}})
                    logger.info("demo_turn_timing", extra={"turn": turn_id, **{k: round(v, 3) for k, v in timing.items()}})
                await websocket.send_bytes(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the text is already on screen; don't kill the session over audio
            logger.exception("demo_tts_failed", extra={"turn": turn_id})
            await send_json({"type": "info", "text": "(audio for this reply failed)"})
        await send_json({"type": "audio_end"})

    async def run_turn(turn_id: int, customer_text: str, timing: dict) -> None:
        nonlocal speak_task, call_ended

        async def on_speech_ready(text: str) -> None:
            # Start speaking and return at once (the engine still has tool calls and
            # the state transition to finish). Never await TTS here — it would sit
            # inside the engine's LLM deadline.
            nonlocal speak_task
            now = loop.time()
            timing["llm"] = now - timing.pop("_llm_started_at", now)
            timing["_speech_ready_at"] = now
            if turn_id <= muted_up_to:
                logger.info("demo_reply_muted_by_barge_in", extra={"turn": turn_id})
                return
            speak_task = asyncio.create_task(speak(text, turn_id, timing))

        timing["_llm_started_at"] = loop.time()
        async with session_scope() as turn_session:
            result = await engine.run_turn(
                customer_text,
                session=turn_session,
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                lead_id=lead_id,
                conversation_id=conversation_id,
                on_speech_ready=on_speech_ready,
            )
        task = speak_task
        if task is not None:
            await asyncio.wait([task])  # finished, or cancelled by a barge-in
        if result.end_call:
            call_ended = True
            await send_json({"type": "call_ended"})

    async def handle_utterance(turn_id: int, blob: bytes, received_at: float) -> None:
        async with turn_lock:
            if turn_id != latest_turn or call_ended:
                return  # superseded by a newer utterance while waiting
            try:
                timing: dict = {"_received_at": received_at}
                transcript = await _transcribe_blob(blob, extension)
                timing["stt"] = loop.time() - received_at
                if turn_id != latest_turn:
                    return
                if not transcript:
                    await send_json({"type": "info", "text": "(didn't catch that — try again)"})
                    return
                await send_json({"type": "customer_text", "text": transcript})
                await run_turn(turn_id, transcript, timing)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad turn must not end the demo session
                logger.exception("demo_turn_failed", extra={"turn": turn_id})
                await send_json({"type": "info", "text": "(something went wrong on that turn — please try again)"})

    async def opening_line() -> None:
        async with turn_lock:
            try:
                await run_turn(0, "", {"_received_at": loop.time()})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("demo_turn_failed", extra={"turn": 0})
                await send_json({"type": "info", "text": "(the agent couldn't start — reconnect to retry)"})

    try:
        await send_json({"type": "info", "text": "Connected — the agent will greet you now."})
        task = asyncio.create_task(opening_line())
        turn_tasks.add(task)
        task.add_done_callback(turn_tasks.discard)

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                blob = message["bytes"]
                if call_ended:
                    continue
                if len(blob) < MIN_UTTERANCE_BYTES:
                    await send_json({"type": "info", "text": "(too short — hold the button while you speak)"})
                    continue
                received_at = loop.time()
                await interrupt()  # the newest utterance always wins (mutes turns up to now)...
                latest_turn += 1  # ...then this one gets an id above the muted range
                task = asyncio.create_task(handle_utterance(latest_turn, blob, received_at))
                turn_tasks.add(task)
                task.add_done_callback(turn_tasks.discard)
            elif message.get("text"):
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "barge_in":
                    await interrupt()
                elif data.get("type") == "hello":
                    mime = str(data.get("mime", "")).split(";")[0].strip().lower()
                    extension = _MIME_EXTENSIONS.get(mime, "webm")
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
  #log { border: 1px solid #ddd; border-radius: 10px; padding: 14px; height: 380px; overflow-y: auto; margin-top: 16px; background: #fafafa; }
  .msg { margin: 10px 0; line-height: 1.4; }
  .agent { color: #1a5fb4; }
  .customer { color: #26a269; }
  .info { color: #888; font-style: italic; }
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
<div class="controls">
  <button id="startBtn">Start Call</button>
  <button id="talkBtn" disabled>Hold to Talk</button>
</div>
<div id="status"></div>
<div id="log"></div>
<script>
const MIN_RECORDING_MS = 350;   // shorter presses are accidental clicks
const MIN_LEVEL = 0.01;         // peak RMS below this = nothing was said
let ws, audioCtx, micStream, analyser, mimeType = '';
let rec = null, levelTimer = null, callEnded = false;
let sources = [], nextTime = 0, acceptAudio = false, carry = null, sampleRate = 24000;
let releaseAt = null, waitingFirstAudio = false, lastTimingEl = null;

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
  const buf = audioCtx.createBuffer(1, f.length, sampleRate);
  buf.copyToChannel(f, 0);
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  const at = Math.max(audioCtx.currentTime + 0.02, nextTime);
  src.start(at);
  nextTime = at + buf.duration;
  sources.push(src);
  src.onended = () => {
    sources = sources.filter((s) => s !== src);
    if (!sources.length && !acceptAudio && !rec && !callEnded) setStatus('Your turn — hold to talk');
  };
  if (waitingFirstAudio && releaseAt !== null) {
    waitingFirstAudio = false;
    const heard = (performance.now() - releaseAt) / 1000 + (at - audioCtx.currentTime);
    const el = lastTimingEl || log('timing', '');
    el.textContent = '⏱ you stopped → agent audio: ' + heard.toFixed(2) + 's' + (el.dataset.detail || '');
  }
  setStatus('Agent speaking… (press to interrupt)');
}

function onMessage(event) {
  if (typeof event.data !== 'string') { playChunk(event.data); return; }
  const msg = JSON.parse(event.data);
  if (msg.type === 'agent_text') { log('agent', msg.text); lastTimingEl = null; }
  else if (msg.type === 'customer_text') { log('customer', msg.text); setStatus('Thinking…'); }
  else if (msg.type === 'info') { log('info', msg.text); if (!rec) setStatus('Your turn — hold to talk'); }
  else if (msg.type === 'audio_start') { stopPlayback(); sampleRate = msg.sample_rate; acceptAudio = true; }
  else if (msg.type === 'audio_end') { acceptAudio = false; }
  else if (msg.type === 'interrupt') { stopPlayback(); }
  else if (msg.type === 'timing') {
    const parts = [];
    if (msg.stt !== undefined) parts.push('STT ' + msg.stt.toFixed(2));
    if (msg.llm !== undefined) parts.push('LLM ' + msg.llm.toFixed(2));
    if (msg.tts !== undefined) parts.push('TTS ' + msg.tts.toFixed(2));
    lastTimingEl = log('timing', '');
    lastTimingEl.dataset.detail = '  (' + parts.join(' · ') + ' s)';
    lastTimingEl.textContent = '⏱ server ' + (msg.server || 0).toFixed(2) + 's' + lastTimingEl.dataset.detail;
  }
  else if (msg.type === 'call_ended') {
    callEnded = true; log('info', '--- call ended ---'); talkBtn.disabled = true; setStatus('Call ended');
  }
}

function pickMime() {
  for (const m of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  }
  return '';
}

function currentLevel() {
  const a = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(a);
  let s = 0; for (const v of a) s += v * v;
  return Math.sqrt(s / a.length);
}

function startTalking() {
  if (!ws || ws.readyState !== 1 || rec || callEnded || !micStream) return;
  stopPlayback();                                   // barge-in, locally...
  ws.send(JSON.stringify({ type: 'barge_in' }));    // ...and on the server
  const r = { chunks: [], startedAt: performance.now(), stoppedAt: 0, maxLevel: 0 };
  r.recorder = new MediaRecorder(micStream, mimeType ? { mimeType } : undefined);  // one per recording
  r.recorder.ondataavailable = (e) => { if (e.data.size > 0) r.chunks.push(e.data); };
  r.recorder.onstop = () => finishRecording(r);
  rec = r;
  r.recorder.start();
  levelTimer = setInterval(() => { r.maxLevel = Math.max(r.maxLevel, currentLevel()); }, 40);
  talkBtn.classList.add('recording');
  talkBtn.textContent = 'Recording… release to send';
  setStatus('Listening…');
}

function stopTalking() {
  if (!rec) return;
  const r = rec; rec = null;
  clearInterval(levelTimer);
  r.maxLevel = Math.max(r.maxLevel, currentLevel());
  r.stoppedAt = performance.now();
  r.recorder.stop();
  talkBtn.classList.remove('recording');
  talkBtn.textContent = 'Hold to Talk';
}

function finishRecording(r) {
  const ms = r.stoppedAt - r.startedAt;
  if (ms < MIN_RECORDING_MS) { log('info', '(too short — hold the button while you speak)'); setStatus('Your turn — hold to talk'); return; }
  if (r.maxLevel < MIN_LEVEL) { log('info', "(didn't hear anything — check your microphone)"); setStatus('Your turn — hold to talk'); return; }
  const blob = new Blob(r.chunks, { type: mimeType || 'audio/webm' });
  releaseAt = r.stoppedAt; waitingFirstAudio = true;
  blob.arrayBuffer().then((buf) => { if (ws && ws.readyState === 1) ws.send(buf); });
  setStatus('Thinking…');
}

startBtn.onclick = async () => {
  startBtn.disabled = true; callEnded = false;
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.resume();
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    log('info', 'Microphone permission denied: ' + err.message); startBtn.disabled = false; return;
  }
  analyser = audioCtx.createAnalyser(); analyser.fftSize = 1024;
  audioCtx.createMediaStreamSource(micStream).connect(analyser);
  mimeType = pickMime();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/demo/ws');
  ws.binaryType = 'arraybuffer';
  ws.onmessage = onMessage;
  ws.onopen = () => { ws.send(JSON.stringify({ type: 'hello', mime: mimeType })); talkBtn.disabled = false; setStatus('Connecting…'); };
  ws.onclose = () => { startBtn.disabled = false; talkBtn.disabled = true; stopPlayback(); if (!callEnded) setStatus('Disconnected'); };
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

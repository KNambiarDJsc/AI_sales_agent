"""Browser-based demo — NOT a telephony path, and not meant to replace one.

Why this exists: the client's telephony options (Freejun, Exotel, Twilio) each need
either carrier KYC for a real Indian number (Freejun/Exotel) or a paid account (Twilio
trial accounts cannot use the custom webhook/streaming our architecture needs — see
STATUS.md's "Real outbound call attempt" section). None of that blocks demonstrating
the actual AI agent, though: this endpoint runs the exact same orchestration stack
(`ConversationEngine`, the real state machine/script, the real tool registry and
qualification engine, real OpenAI STT/TTS) over the browser's own microphone and
speakers instead of a phone line — free, no account, no KYC, testable immediately.

This is a demo harness, not a second production transport: no VAD/barge-in/streaming
(push-to-talk — record one utterance, release, get a response), and it provisions its
own throwaway Tenant/Campaign/Lead/CallAttempt rows per session rather than going
through the campaign/lead-import flow. When real telephony is available, the call path
is `apps/api/routers/media.py`, not this file.
"""
from __future__ import annotations

import io
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from openai import AsyncOpenAI
from sqlalchemy import select

from config.settings import PROMPTS_DIR, get_settings
from database.models import CallAttempt, Campaign, Conversation, Lead, Tenant
from database.session import session_scope
from llm.openai import OpenAILLMProvider
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.prompts import load_campaign_prompt
from orchestrator.state_machine import StateMachine, load_script_by_id
from speech.tts.openai import OPENAI_TTS_SAMPLE_RATE_HZ, OpenAITTSProvider

logger = logging.getLogger(__name__)
router = APIRouter(tags=["demo"])

DEMO_TENANT_NAME = "Browser Demo Tenant"
DEMO_CAMPAIGN_NAME = "Browser Demo Campaign"
DEMO_SCRIPT_ID = "product-a"


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


async def _transcribe_blob(client: AsyncOpenAI, model: str, blob: bytes) -> str:
    audio_file = io.BytesIO(blob)
    audio_file.name = "utterance.webm"
    try:
        response = await client.audio.transcriptions.create(model=model, file=audio_file, response_format="json")
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
    settings = get_settings()
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
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

    context = ConversationContext(
        conversation_id=str(conversation_id),
        campaign_id=str(campaign_id),
        lead_id=str(lead_id),
        script_id=script.script_id,
        script_version=script.version,
        current_state="INTRO",
        lead_fields={"contact_name": lead.contact_name, "business_name": lead.business_name},
        campaign_prompt=campaign_prompt,
    )
    state_machine = StateMachine(script, current_state="INTRO")
    engine = ConversationEngine(OpenAILLMProvider(), state_machine, context)

    async def speak(text: str) -> None:
        await websocket.send_json({"type": "agent_text", "text": text})
        await websocket.send_json({"type": "audio_start", "sample_rate": OPENAI_TTS_SAMPLE_RATE_HZ})
        async for chunk in tts_provider.synthesize_stream(text):
            await websocket.send_bytes(chunk)
        await websocket.send_json({"type": "audio_end"})

    async def run_turn(customer_text: str) -> bool:
        """Returns True if the call should end."""
        async with session_scope() as turn_session:
            result = await engine.run_turn(
                customer_text,
                session=turn_session,
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                lead_id=lead_id,
                conversation_id=conversation_id,
                on_speech_ready=speak,
            )
        return result.end_call

    try:
        await websocket.send_json({"type": "info", "text": "Connected — the agent will greet you now."})
        if await run_turn(""):
            await websocket.send_json({"type": "call_ended"})
            return

        while True:
            try:
                audio_blob = await websocket.receive_bytes()
            except WebSocketDisconnect:
                break

            transcript = await _transcribe_blob(openai_client, settings.openai_stt_model, audio_blob)
            if not transcript.strip():
                await websocket.send_json({"type": "info", "text": "(didn't catch that — try again)"})
                continue

            await websocket.send_json({"type": "customer_text", "text": transcript})
            if await run_turn(transcript):
                await websocket.send_json({"type": "call_ended"})
                break
    finally:
        await tts_provider.close()
        async with session_scope() as session:
            lead_row = await session.get(Lead, lead_id)
            if lead_row is not None and lead_row.status == "in_progress":
                lead_row.status = "completed"


DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Voice Sales Agent — Live Demo</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h2 { margin-bottom: 4px; }
  p.sub { color: #666; margin-top: 0; }
  #log { border: 1px solid #ddd; border-radius: 10px; padding: 14px; height: 360px; overflow-y: auto; margin-top: 16px; background: #fafafa; }
  .msg { margin: 10px 0; line-height: 1.4; }
  .agent { color: #1a5fb4; }
  .customer { color: #26a269; }
  .info { color: #888; font-style: italic; }
  .controls { display: flex; gap: 12px; margin-top: 16px; }
  button { font-size: 16px; padding: 12px 22px; border-radius: 10px; border: none; cursor: pointer; }
  #startBtn { background: #1a1a1a; color: white; }
  #talkBtn { background: #1a5fb4; color: white; flex: 1; }
  #talkBtn:disabled { background: #ccc; cursor: not-allowed; }
  #talkBtn.recording { background: #c01c28; }
</style>
</head>
<body>
<h2>Voice Sales Agent — Live Demo</h2>
<p class="sub">Talks to the real backend (state machine, qualification, tools) over your mic/speakers — no phone line involved.</p>
<div class="controls">
  <button id="startBtn">Start Call</button>
  <button id="talkBtn" disabled>Hold to Talk</button>
</div>
<div id="log"></div>
<script>
let ws, mediaRecorder, audioChunks = [], audioCtx, pendingChunks = [], sampleRate = 24000;

function log(cls, text) {
  const div = document.getElementById('log');
  const p = document.createElement('p');
  p.className = 'msg ' + cls;
  const prefix = cls === 'agent' ? 'Agent: ' : cls === 'customer' ? 'You: ' : '';
  p.textContent = prefix + text;
  div.appendChild(p);
  div.scrollTop = div.scrollHeight;
}

async function playPcm16(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  if (total === 0) return;
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { merged.set(c, offset); offset += c.length; }
  const samples = new Int16Array(merged.buffer, merged.byteOffset, merged.byteLength - (merged.byteLength % 2));
  const float32 = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) float32[i] = samples[i] / 32768;
  const buffer = audioCtx.createBuffer(1, float32.length, rate);
  buffer.copyToChannel(float32, 0);
  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);
  source.start();
  return new Promise((resolve) => { source.onended = resolve; });
}

document.getElementById('startBtn').onclick = async () => {
  document.getElementById('startBtn').disabled = true;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/demo/ws');
  ws.binaryType = 'arraybuffer';
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();

  ws.onmessage = async (event) => {
    if (typeof event.data === 'string') {
      const msg = JSON.parse(event.data);
      if (msg.type === 'agent_text') log('agent', msg.text);
      else if (msg.type === 'customer_text') log('customer', msg.text);
      else if (msg.type === 'info') log('info', msg.text);
      else if (msg.type === 'audio_start') { sampleRate = msg.sample_rate; pendingChunks = []; }
      else if (msg.type === 'audio_end') { await playPcm16(pendingChunks, sampleRate); pendingChunks = []; }
      else if (msg.type === 'call_ended') { log('info', '--- call ended ---'); document.getElementById('talkBtn').disabled = true; ws.close(); }
    } else {
      pendingChunks.push(new Uint8Array(event.data));
    }
  };

  ws.onopen = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
      mediaRecorder.onstop = () => {
        const blob = new Blob(audioChunks, { type: 'audio/webm' });
        audioChunks = [];
        blob.arrayBuffer().then((buf) => ws.send(buf));
      };
      document.getElementById('talkBtn').disabled = false;
    } catch (err) {
      log('info', 'Microphone permission denied: ' + err.message);
    }
  };

  ws.onclose = () => { document.getElementById('startBtn').disabled = false; };
};

const talkBtn = document.getElementById('talkBtn');
function startTalking(e) {
  e.preventDefault();
  if (!mediaRecorder || mediaRecorder.state === 'recording') return;
  talkBtn.classList.add('recording');
  talkBtn.textContent = 'Recording... release to send';
  mediaRecorder.start();
}
function stopTalking(e) {
  e.preventDefault();
  if (!mediaRecorder || mediaRecorder.state !== 'recording') return;
  talkBtn.classList.remove('recording');
  talkBtn.textContent = 'Hold to Talk';
  mediaRecorder.stop();
}
talkBtn.onmousedown = startTalking;
talkBtn.onmouseup = stopTalking;
talkBtn.onmouseleave = (e) => { if (mediaRecorder && mediaRecorder.state === 'recording') stopTalking(e); };
talkBtn.ontouchstart = startTalking;
talkBtn.ontouchend = stopTalking;
</script>
</body>
</html>
"""

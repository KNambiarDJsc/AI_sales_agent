"""Realtime media WebSocket endpoints (Section 16/32).

Two routes, because Twilio and Exotel correlate a media WebSocket connection to a call
differently — both confirmed against documentation/live testing, not assumed:

- Twilio (`/media/{call_attempt_id}`): we control the TwiML returned at call-create
  time (`telephony/twilio.py`), so the attempt ID is embedded directly in the
  `<Connect><Stream url="...">` URL Twilio connects to. Simple path-based correlation.
- Exotel (`/media/exotel`): the Voicebot/Stream applet in Exotel's App Bazaar is
  configured with a URL *once*, at flow-design time in their console — there is no
  per-call dynamic TwiML-equivalent to embed an attempt ID in (Exotel does support a
  dynamic-HTTPS-URL option that returns `{"url": "wss://..."}`, but its own request
  parameters aren't documented, so a static URL + correlating by the `call_sid` Exotel
  sends in its own `start` event — confirmed via developer.exotel.com/docs/agentstream —
  is the robust choice). We match that `call_sid` against the `provider_call_id` we
  stored when `TelephonyProvider.create_outbound_call()` returned it
  (`CallRepository.get_attempt_by_provider_call_id`).

Both routes are deliberately thin: they only translate the provider's wire protocol
into VoiceSession calls. Everything about audio processing, VAD, orchestration, and
qualification is downstream of VoiceSession — this file has no business logic in it,
per Section 4's "keep the media layer and agent layer separate."
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.settings import PROMPTS_DIR
from database.models import CallAttempt, Campaign, Conversation, Lead
from database.repositories.call_repository import CallRepository
from database.session import session_scope
from services.call_worker.lifecycle import finalize_call
from llm.openai import OpenAILLMProvider
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.prompts import load_campaign_prompt
from orchestrator.state_machine import StateMachine, load_script_by_id
from speech.stt.factory import get_stt_provider
from speech.tts.openai import OpenAITTSProvider
from telephony.base import TelephonyProvider
from telephony.factory import get_telephony_provider
from voice.audio.processing import TELEPHONY_SAMPLE_RATE_HZ
from voice.session.session import SessionIdentity, VoiceSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["media"])


async def _resolve_attempt_and_build_session(
    *, attempt_id: uuid.UUID | None = None, provider_call_id: str | None = None
) -> tuple[VoiceSession, SessionIdentity, TelephonyProvider, uuid.UUID, uuid.UUID] | None:
    """Shared setup for both routes: resolve the CallAttempt (by its own id, or by the
    provider's call_sid for Exotel), load campaign/lead, and build a fully-wired
    VoiceSession. Returns None if the attempt/campaign/lead can't be resolved."""
    async with session_scope() as session:
        if attempt_id is not None:
            attempt = await session.get(CallAttempt, attempt_id)
        elif provider_call_id is not None:
            attempt = await CallRepository(session).get_attempt_by_provider_call_id(provider_call_id)
        else:
            raise ValueError("Must provide either attempt_id or provider_call_id")

        if attempt is None:
            return None
        campaign = await session.get(Campaign, attempt.campaign_id)
        lead = await session.get(Lead, attempt.lead_id)
        if campaign is None or lead is None:
            return None

        script = load_script_by_id(campaign.script_id)
        campaign_prompt_path = campaign.config.get("campaign_prompt_path") or (PROMPTS_DIR / "campaign_prompt_template.yaml")
        campaign_prompt = load_campaign_prompt(campaign_prompt_path)

        conversation = Conversation(
            call_attempt_id=attempt.id,
            script_id=script.script_id,
            script_version=script.version,
            current_state="INTRO",
        )
        session.add(conversation)
        await session.flush()
        conversation_id = conversation.id

        context = ConversationContext(
            conversation_id=str(conversation_id),
            campaign_id=str(campaign.id),
            lead_id=str(lead.id),
            script_id=script.script_id,
            script_version=script.version,
            current_state="INTRO",
            lead_fields={"contact_name": lead.contact_name, "business_name": lead.business_name, **lead.extra},
            campaign_prompt=campaign_prompt,
            timezone=campaign.timezone,
        )
        tenant_id, campaign_id_val, lead_id_val, attempt_id_val = campaign.tenant_id, campaign.id, lead.id, attempt.id
        telephony_provider_name = campaign.telephony_provider
        provider_call_id_val = attempt.provider_call_id or provider_call_id or ""

    telephony = get_telephony_provider(telephony_provider_name)
    state_machine = StateMachine(script, current_state="INTRO")
    engine = ConversationEngine(OpenAILLMProvider(), state_machine, context)

    identity = SessionIdentity(
        provider_call_id=provider_call_id_val,
        tenant_id=tenant_id,
        campaign_id=campaign_id_val,
        lead_id=lead_id_val,
        conversation_id=conversation_id,
    )
    voice_session = VoiceSession(
        identity=identity,
        telephony=telephony,
        stt_provider=get_stt_provider(),
        tts_provider=OpenAITTSProvider(),
        engine=engine,
        session_factory=session_scope,
    )
    await voice_session.start()
    return voice_session, identity, telephony, conversation_id, attempt_id_val


async def _teardown(
    telephony: TelephonyProvider, provider_call_id: str, attempt_id: uuid.UUID, voice_session: VoiceSession
) -> None:
    try:
        telephony.unregister_stream(provider_call_id)
    except NotImplementedError:
        pass
    async with session_scope() as session:
        # Closes the conversation (with its final state) and moves the lead out of
        # in_progress; the status webhook may run the same finalization — idempotent.
        await finalize_call(
            session, attempt_id, ended_reason="media_stream_ended", final_state=voice_session.conversation_state
        )


@router.websocket("/media/exotel")
async def exotel_media_stream(websocket: WebSocket) -> None:
    """Exotel's Voicebot/Stream applet, configured with this route's static URL in
    their App Bazaar console. Correlates to our CallAttempt via the `call_sid` in
    Exotel's own `start` event (see module docstring) rather than a path param.

    Confirmed live: Exotel sends a preliminary `{"event": "connected"}` message before
    the real `start` event, the same two-step handshake Twilio Media Streams uses. The
    previous version of this handler treated whatever arrived first as if it had to be
    `start`, so it rejected every real call at the `connected` message before ever
    seeing `start` — caught via a live test call (see STATUS.md). We now skip past
    `connected` specifically, but still reject immediately (not hang waiting) on
    anything else unexpected."""
    await websocket.accept()

    message: dict = {}
    try:
        while True:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            event = message.get("event")
            if event == "start":
                break
            if event == "connected":
                continue
            logger.warning("exotel_media_stream_unexpected_first_event", extra={"event": event})
            await websocket.close(code=4400)
            return
    except WebSocketDisconnect:
        return

    start_info = message.get("start", {})
    call_sid = start_info.get("call_sid") or start_info.get("callSid")
    stream_sid = message.get("stream_sid") or start_info.get("stream_sid", "")
    if not call_sid:
        logger.warning("exotel_media_stream_missing_call_sid", extra={"payload": message})
        await websocket.close(code=4400)
        return

    resolved = await _resolve_attempt_and_build_session(provider_call_id=call_sid)
    if resolved is None:
        logger.warning("exotel_media_stream_unknown_call_sid", extra={"call_sid": call_sid})
        await websocket.close(code=4404)
        return
    voice_session, identity, telephony, conversation_id, attempt_id = resolved
    identity.provider_call_id = call_sid
    telephony.register_stream(call_sid, stream_sid, websocket.send_text)
    await voice_session.speak_opening_line()

    try:
        while True:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            event = message.get("event")

            if event == "media":
                payload_b64 = message.get("media", {}).get("payload", "")
                if payload_b64:
                    await voice_session.handle_inbound_audio(base64.b64decode(payload_b64))
            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("exotel_media_stream_disconnected", extra={"call_sid": call_sid})
    finally:
        await voice_session.close()
        await _teardown(telephony, call_sid, attempt_id, voice_session)


@router.websocket("/media/frejun/{call_attempt_id}")
async def frejun_media_stream(websocket: WebSocket, call_attempt_id: uuid.UUID) -> None:
    """FreJun/Teler media stream (docs: media-streaming/websocket-protocol).

    Teler connects here because our flow endpoint (`apps/api/routers/flows.py`)
    returned this URL — with our CallAttempt id in the path, so correlation is by our
    own id; the `call_id` in Teler's `start` message is then checked against the id
    the initiate call returned.

    Wire protocol, translated to/from VoiceSession and nothing more:
    - in `start`: `{type, call_id, stream_id, data: {encoding: "audio/l16",
      sample_rate: 8000, channels: 1}}` — always first; anything else is rejected.
    - in `audio`: `{type, stream_id, message_id, data: {audio_b64}}` — 16-bit
      linear PCM, mono, 8 kHz → `VoiceSession.handle_inbound_audio` (pcm16 path).
    - out `audio` / `clear`: built by `telephony/frejun.py:send_audio/clear_audio`.

    Unlike the other routes, this one closes the socket itself once the agent ends
    the call, so the stream (and Teler's side of it) ends even if the hangup API
    call couldn't be made."""
    await websocket.accept()

    try:
        start = json.loads(await websocket.receive_text())
    except WebSocketDisconnect:
        return
    except json.JSONDecodeError:
        await websocket.close(code=4400)
        return
    if not isinstance(start, dict) or start.get("type") != "start":
        logger.warning("frejun_media_stream_unexpected_first_message", extra={"msg_type": str(start)[:40]})
        await websocket.close(code=4400)
        return

    fmt = start.get("data") or {}
    encoding = fmt.get("encoding", "audio/l16")
    sample_rate = int(fmt.get("sample_rate", TELEPHONY_SAMPLE_RATE_HZ))
    channels = int(fmt.get("channels", 1))
    if encoding != "audio/l16" or sample_rate != TELEPHONY_SAMPLE_RATE_HZ or channels != 1:
        # Our pcm16 path assumes exactly this; decoding anything else as if it were
        # would feed noise to VAD/STT for the whole call. Refuse loudly instead.
        logger.error(
            "frejun_media_stream_unsupported_format",
            extra={"encoding": encoding, "sample_rate": sample_rate, "channels": channels},
        )
        await websocket.close(code=4415)
        return

    call_id = str(start.get("call_id") or "")
    stream_id = str(start.get("stream_id") or "")
    resolved = await _resolve_attempt_and_build_session(attempt_id=call_attempt_id)
    if resolved is None:
        logger.warning("frejun_media_stream_unknown_attempt", extra={"call_attempt_id": str(call_attempt_id)})
        await websocket.close(code=4404)
        return
    voice_session, identity, telephony, conversation_id, attempt_id = resolved
    if identity.provider_call_id and call_id and identity.provider_call_id != call_id:
        logger.warning(
            "frejun_media_stream_call_id_mismatch",
            extra={"call_attempt_id": str(call_attempt_id), "call_id": call_id},
        )
        await voice_session.close()
        await _teardown(telephony, identity.provider_call_id, attempt_id, voice_session)
        await websocket.close(code=4403)
        return
    identity.provider_call_id = call_id or identity.provider_call_id
    telephony.register_stream(identity.provider_call_id, stream_id, websocket.send_text)
    logger.info(
        "frejun_media_stream_started",
        extra={"call_attempt_id": str(call_attempt_id), "call_id": call_id, "stream_id": stream_id},
    )

    disconnected = False
    try:
        await voice_session.speak_opening_line()
        while not voice_session.ended:
            message = json.loads(await websocket.receive_text())
            if message.get("type") == "audio":
                payload_b64 = (message.get("data") or {}).get("audio_b64", "")
                if payload_b64:
                    await voice_session.handle_inbound_audio(base64.b64decode(payload_b64))
    except WebSocketDisconnect:
        disconnected = True
        logger.info("frejun_media_stream_disconnected", extra={"call_id": call_id})
    finally:
        await voice_session.close()
        await _teardown(telephony, identity.provider_call_id, attempt_id, voice_session)
        if not disconnected:
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)


@router.websocket("/media/{call_attempt_id}")
async def media_stream(websocket: WebSocket, call_attempt_id: uuid.UUID) -> None:
    """Twilio's Media Streams protocol — path-based correlation (see module docstring).

    Registered AFTER `/media/exotel` on purpose: Starlette matches WebSocket routes by
    position, and `{call_attempt_id}` structurally matches *any* single path segment
    (including the literal "exotel") before its UUID type-conversion is even
    attempted — if this route were registered first, a request to `/media/exotel`
    would be routed here and rejected for an invalid UUID instead of ever reaching the
    Exotel handler below. Caught by `tests/unit/test_media_exotel.py`.
    """
    await websocket.accept()

    resolved = await _resolve_attempt_and_build_session(attempt_id=call_attempt_id)
    if resolved is None:
        logger.warning("media_stream_unknown_attempt", extra={"call_attempt_id": str(call_attempt_id)})
        await websocket.close(code=4404)
        return
    voice_session, identity, telephony, conversation_id, attempt_id = resolved

    try:
        while True:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            event = message.get("event")

            if event == "start":
                stream_sid = message.get("streamSid") or message.get("stream_sid", "")
                call_sid = message.get("start", {}).get("callSid", identity.provider_call_id)
                identity.provider_call_id = call_sid or identity.provider_call_id
                telephony.register_stream(identity.provider_call_id, stream_sid, websocket.send_text)
                await voice_session.speak_opening_line()

            elif event == "media":
                payload_b64 = message.get("media", {}).get("payload", "")
                if payload_b64:
                    await voice_session.handle_inbound_audio(base64.b64decode(payload_b64))

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("media_stream_disconnected", extra={"call_attempt_id": str(call_attempt_id)})
    finally:
        await voice_session.close()
        await _teardown(telephony, identity.provider_call_id, attempt_id, voice_session)

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
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.settings import PROMPTS_DIR
from database.models import CallAttempt, Campaign, Conversation, Lead
from database.repositories.call_repository import CallRepository
from database.session import session_scope
from llm.openai import OpenAILLMProvider
from orchestrator.context import ConversationContext
from orchestrator.engine import ConversationEngine
from orchestrator.prompts import load_campaign_prompt
from orchestrator.state_machine import StateMachine, load_script_by_id
from speech.stt.factory import get_stt_provider
from speech.tts.openai import OpenAITTSProvider
from telephony.base import TelephonyProvider
from telephony.factory import get_telephony_provider
from voice.session.session import SessionIdentity, VoiceSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["media"])


async def _resolve_attempt_and_build_session(
    *, attempt_id: uuid.UUID | None = None, provider_call_id: str | None = None
) -> tuple[VoiceSession, SessionIdentity, TelephonyProvider, uuid.UUID] | None:
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
        )
        tenant_id, campaign_id_val, lead_id_val = campaign.tenant_id, campaign.id, lead.id
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
    return voice_session, identity, telephony, conversation_id


async def _teardown(telephony: TelephonyProvider, provider_call_id: str, conversation_id: uuid.UUID) -> None:
    try:
        telephony.unregister_stream(provider_call_id)
    except NotImplementedError:
        pass
    async with session_scope() as session:
        await CallRepository(session).close_conversation(conversation_id, reason="media_stream_ended")


@router.websocket("/media/exotel")
async def exotel_media_stream(websocket: WebSocket) -> None:
    """Exotel's Voicebot/Stream applet, configured with this route's static URL in
    their App Bazaar console. Correlates to our CallAttempt via the `call_sid` in
    Exotel's own `start` event (see module docstring) rather than a path param."""
    await websocket.accept()

    try:
        raw_message = await websocket.receive_text()
    except WebSocketDisconnect:
        return

    message = json.loads(raw_message)
    if message.get("event") != "start":
        logger.warning("exotel_media_stream_unexpected_first_event", extra={"event": message.get("event")})
        await websocket.close(code=4400)
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
    voice_session, identity, telephony, conversation_id = resolved
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
        await _teardown(telephony, call_sid, conversation_id)


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
    voice_session, identity, telephony, conversation_id = resolved

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
        await _teardown(telephony, identity.provider_call_id, conversation_id)

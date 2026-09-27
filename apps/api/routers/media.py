"""Realtime media WebSocket endpoint (Section 16/32).

Speaks Twilio's Media Streams JSON protocol (start/media/stop events, base64 mu-law
8kHz payloads) — see telephony/twilio.py's module docstring. Exotel's Voice Streaming
is modeled on the same shape (telephony/exotel.py). This endpoint is deliberately thin:
it only translates the provider's wire protocol into VoiceSession calls. Everything
about audio processing, VAD, orchestration, and qualification is downstream of
VoiceSession — this file has no business logic in it, per Section 4's "keep the media
layer and agent layer separate."
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
from telephony.factory import get_telephony_provider
from voice.session.session import SessionIdentity, VoiceSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["media"])


@router.websocket("/media/{call_attempt_id}")
async def media_stream(websocket: WebSocket, call_attempt_id: uuid.UUID) -> None:
    await websocket.accept()

    async with session_scope() as session:
        attempt = await session.get(CallAttempt, call_attempt_id)
        if attempt is None:
            logger.warning("media_stream_unknown_attempt", extra={"call_attempt_id": str(call_attempt_id)})
            await websocket.close(code=4404)
            return
        campaign = await session.get(Campaign, attempt.campaign_id)
        lead = await session.get(Lead, attempt.lead_id)
        if campaign is None or lead is None:
            await websocket.close(code=4404)
            return

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

    telephony = get_telephony_provider(campaign.telephony_provider)
    state_machine = StateMachine(script, current_state="INTRO")
    engine = ConversationEngine(OpenAILLMProvider(), state_machine, context)

    identity = SessionIdentity(
        provider_call_id=attempt.provider_call_id or "",
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.id,
        lead_id=lead.id,
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
        try:
            telephony.unregister_stream(identity.provider_call_id)
        except NotImplementedError:
            pass
        async with session_scope() as session:
            await CallRepository(session).close_conversation(conversation_id, reason="media_stream_ended")

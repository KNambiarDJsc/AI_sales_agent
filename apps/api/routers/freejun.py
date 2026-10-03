"""FreJun Teler routes: the flow endpoint Teler fetches once a call connects, and the
media WebSocket its stream action opens. Protocol details are documented in
telephony/freejun.py. Correlation is by attempt id in the URL (same pattern as the
Twilio route), since the flow is served per call.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from apps.api.routers.media import _resolve_attempt_and_build_session, _teardown

logger = logging.getLogger(__name__)
router = APIRouter(tags=["freejun"])


@router.post("/freejun/flow/{call_attempt_id}")
async def freejun_flow(call_attempt_id: uuid.UUID, request: Request) -> dict:
    ws_base = str(request.base_url).replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")
    return {
        "action": "stream",
        "ws_url": f"{ws_base}/media/freejun/{call_attempt_id}",
        "sample_rate": "8k",
        "chunk_size": 400,
        "record": False,
    }


@router.websocket("/media/freejun/{call_attempt_id}")
async def freejun_media_stream(websocket: WebSocket, call_attempt_id: uuid.UUID) -> None:
    await websocket.accept()

    resolved = await _resolve_attempt_and_build_session(attempt_id=call_attempt_id)
    if resolved is None:
        logger.warning("freejun_media_unknown_attempt", extra={"call_attempt_id": str(call_attempt_id)})
        await websocket.close(code=4404)
        return
    voice_session, identity, telephony, conversation_id = resolved
    telephony.register_stream(identity.provider_call_id, str(call_attempt_id), websocket.send_text)
    await voice_session.speak_opening_line()

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            kind = message.get("type")
            if kind == "audio":
                audio_b64 = message.get("data", {}).get("audio_b64", "")
                if audio_b64:
                    await voice_session.handle_inbound_audio(base64.b64decode(audio_b64))
            elif kind == "stop":
                break
    except WebSocketDisconnect:
        logger.info("freejun_media_disconnected", extra={"call_attempt_id": str(call_attempt_id)})
    finally:
        await voice_session.close()
        await _teardown(telephony, identity.provider_call_id, conversation_id)

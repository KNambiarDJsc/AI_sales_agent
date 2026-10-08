"""Call Flow endpoints — providers that ask us what to do once a call connects.

Vobiz (`/flow/vobiz/{call_attempt_id}`) works the same way with XML instead of JSON:
see `vobiz_answer` below and `telephony/vobiz.py`.

FreJun/Teler (docs: telephony/call-flows): when the callee answers an outbound call,
Teler POSTs `{call_id, account_id, from_number, to_number, direction}` to the
`flow_url` we passed at initiate time and needs `200 OK` + one JSON action within 5
seconds, or the call fails (`flow_fetch_failed`). We passed
`/flow/frejun/{call_attempt_id}` (`telephony/frejun.py:flow_url_for`), so the attempt
is known from the path; we answer with a `stream` action pointing Teler at
`/media/frejun/{call_attempt_id}`. Anything we can't place gets `hangup` rather than
an error, so Teler ends the call cleanly instead of retrying a broken flow.

Kept to one indexed primary-key read and at most one small write — well inside the
5-second budget even on a cold connection pool.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request, Response

from config.settings import get_settings
from database.models import CallAttempt
from database.session import session_scope
from telephony.factory import get_telephony_provider
from telephony.frejun import build_stream_flow
from telephony.vobiz import HANGUP_XML, VobizProvider, build_stream_xml, verify_vobiz_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/flow", tags=["flows"])


def frejun_media_ws_url(call_attempt_id: uuid.UUID) -> str:
    base = get_settings().effective_public_base_url
    return f"{base.replace('https://', 'wss://', 1)}/media/frejun/{call_attempt_id}"


@router.post("/frejun/inbound")
async def frejun_inbound_call_flow(request: Request) -> dict:
    """The Voice App's Incoming Call URL (required by Teler for every Voice App,
    docs: telephony/voice-apps). This system only places outbound calls; someone
    calling the Teler number back gets a clean hangup rather than a failed flow.
    Registered before `/frejun/{call_attempt_id}` so "inbound" isn't parsed as an id."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    logger.info("frejun_inbound_call_rejected", extra={"call_id": str((body or {}).get("call_id", ""))})
    return {"action": "hangup"}


@router.post("/frejun/{call_attempt_id}")
async def frejun_call_flow(call_attempt_id: uuid.UUID, request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body must still get a valid flow back
        body = {}
    teler_call_id = str(body.get("call_id") or "") if isinstance(body, dict) else ""

    async with session_scope() as session:
        attempt = await session.get(CallAttempt, call_attempt_id)
        if attempt is None or attempt.provider != "frejun":
            logger.warning("frejun_flow_unknown_attempt", extra={"call_attempt_id": str(call_attempt_id)})
            return {"action": "hangup"}
        if teler_call_id:
            if not attempt.provider_call_id:
                # The initiate response's id normally lands first; record it here too in
                # case this request won the race.
                attempt.provider_call_id = teler_call_id
            elif attempt.provider_call_id != teler_call_id:
                logger.warning(
                    "frejun_flow_call_id_mismatch",
                    extra={"call_attempt_id": str(call_attempt_id), "flow_call_id": teler_call_id},
                )
                return {"action": "hangup"}

    if not get_settings().effective_public_base_url.startswith("https://"):
        logger.error("frejun_flow_no_public_base_url")
        return {"action": "hangup"}

    flow = build_stream_flow(frejun_media_ws_url(call_attempt_id))
    logger.info("frejun_flow_served", extra={"call_attempt_id": str(call_attempt_id), "call_id": teler_call_id})
    return flow


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


@router.post("/vobiz/{call_attempt_id}")
async def vobiz_answer(call_attempt_id: uuid.UUID, request: Request, token: str = "") -> Response:
    """Vobiz `answer_url` (docs: call/make-call, xml/stream): called form-encoded with
    `Event=StartApp` and the call's `CallUUID`/`RequestUUID` once the callee answers;
    must return XML. We return a bidirectional `<Stream>` to our media WebSocket, or
    `<Hangup/>` for anything we can't place — Vobiz retries a failing answer_url (3x /
    60 s) and the callee would sit in silence meanwhile. The per-call token
    (`telephony/vobiz.py`) is what proves the request came from the call we placed."""
    provider = get_telephony_provider("vobiz")
    if not isinstance(provider, VobizProvider) or not provider.token_is_valid(str(call_attempt_id), token):
        logger.warning("vobiz_answer_bad_token", extra={"call_attempt_id": str(call_attempt_id)})
        return _xml(HANGUP_XML)
    if verify_vobiz_signature(
        get_settings().vobiz_auth_token, f"{get_settings().effective_public_base_url}{request.url.path}", request.headers
    ) is False:
        logger.warning("vobiz_answer_bad_signature", extra={"call_attempt_id": str(call_attempt_id)})
        return _xml(HANGUP_XML)

    form = await request.form()
    call_uuid = str(form.get("RequestUUID") or form.get("CallUUID") or "")
    async with session_scope() as session:
        attempt = await session.get(CallAttempt, call_attempt_id)
        if attempt is None or attempt.provider != "vobiz":
            logger.warning("vobiz_answer_unknown_attempt", extra={"call_attempt_id": str(call_attempt_id)})
            return _xml(HANGUP_XML)
        if call_uuid and not attempt.provider_call_id:
            attempt.provider_call_id = call_uuid  # answered before the make-call response was stored

    if not get_settings().effective_public_base_url.startswith("https://"):
        logger.error("vobiz_answer_no_public_base_url")
        return _xml(HANGUP_XML)
    logger.info("vobiz_answer_served", extra={"call_attempt_id": str(call_attempt_id), "call_uuid": call_uuid})
    return _xml(build_stream_xml(provider.stream_url_for(str(call_attempt_id))))

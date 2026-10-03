"""Call Flow endpoints — providers that ask us what to do once a call connects.

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

from fastapi import APIRouter, Request

from config.settings import get_settings
from database.models import CallAttempt
from database.session import session_scope
from telephony.frejun import build_stream_flow

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

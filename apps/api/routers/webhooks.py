"""Telephony status-callback webhooks (Section 6/22).

Every request is verified via the provider's `validate_webhook` before anything in the
payload is trusted (Section 22: "Never rely only on the prompt for security" applies
equally here — never rely only on "it came from the right-looking URL").

Payloads arrive form-encoded (Twilio, Exotel) or as JSON (FreJun); both are handed to
the provider's `parse_call_event` as a plain dict. Events that don't change the call's
own status (FreJun's stream.*/recording.* events, unknown types) are acknowledged and
ignored rather than written as status "unknown". A terminal status also finalizes the
call — lead lifecycle and outcome (`services/call_worker/lifecycle.py`).

TODO (Section 22, replay protection): add a short-TTL dedup store (e.g. a unique
constraint on (provider, provider_call_id, event_type, received-at-minute) or a Redis
SETNX) before this goes to production — right now a replayed webhook would just
re-apply the same status update, which is idempotent for status but not yet guarded
against maliciously replayed old events superseding a newer real one. (FreJun's own
signature check already rejects anything signed more than 5 minutes ago.)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from database.repositories.call_repository import CallRepository
from database.session import session_scope
from services.call_worker.lifecycle import TERMINAL_CALL_STATUSES, finalize_call
from telephony.base import CallStatus
from telephony.factory import get_telephony_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _parse_payload(request: Request, body: bytes) -> dict:
    if "application/json" in request.headers.get("content-type", ""):
        try:
            parsed = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from None
        return parsed if isinstance(parsed, dict) else {}
    return dict(await request.form())


@router.post("/{provider_name}")
async def telephony_status_callback(provider_name: str, request: Request) -> dict:
    try:
        provider = get_telephony_provider(provider_name)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown telephony provider: {provider_name}") from None

    body = await request.body()
    # request.headers (case-insensitive), not dict(request.headers): the dict form
    # lowercases every key, so a provider looking up e.g. "X-Twilio-Signature" never
    # found it and every Twilio callback failed validation.
    if not provider.validate_webhook(request.headers, body, str(request.url)):
        logger.warning("webhook_validation_failed", extra={"provider": provider_name})
        raise HTTPException(status_code=403, detail="Webhook signature validation failed")

    event = provider.parse_call_event(await _parse_payload(request, body))
    if event.status == CallStatus.UNKNOWN or not event.provider_call_id:
        logger.info("webhook_event_ignored", extra={"provider": provider_name, "event_type": event.event_type})
        return {"status": "ignored", "reason": f"no call status change for event {event.event_type!r}"}

    async with session_scope() as session:
        call_repo = CallRepository(session)
        attempt = await call_repo.get_attempt_by_provider_call_id(event.provider_call_id)
        if attempt is None:
            logger.warning("webhook_unknown_call", extra={"provider_call_id": event.provider_call_id})
            return {"status": "ignored", "reason": "unknown call"}

        fields: dict = {}
        if event.status == CallStatus.IN_PROGRESS and attempt.started_at is None:
            fields["started_at"] = datetime.now(timezone.utc)
        if event.status.value in TERMINAL_CALL_STATUSES:
            fields["ended_at"] = datetime.now(timezone.utc)

        await call_repo.mark_attempt_status(attempt.id, event.status.value, **fields)
        if event.status.value in TERMINAL_CALL_STATUSES:
            await finalize_call(session, attempt.id, call_status=event.status.value, ended_reason=event.event_type)

    logger.info(
        "webhook_call_status",
        extra={"provider": provider_name, "event_type": event.event_type, "status": event.status.value},
    )
    return {"status": "ok"}

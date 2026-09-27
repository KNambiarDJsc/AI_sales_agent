"""Telephony status-callback webhooks (Section 6/22).

Every request is verified via the provider's `validate_webhook` before anything in the
payload is trusted (Section 22: "Never rely only on the prompt for security" applies
equally here — never rely only on "it came from the right-looking URL").

TODO (Section 22, replay protection): add a short-TTL dedup store (e.g. a unique
constraint on (provider, provider_call_id, event_type, received-at-minute) or a Redis
SETNX) before this goes to production — right now a replayed webhook would just
re-apply the same status update, which is idempotent for status but not yet guarded
against maliciously replayed old events superseding a newer real one.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from database.session import session_scope
from database.repositories.call_repository import CallRepository
from telephony.base import CallStatus
from telephony.factory import get_telephony_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/{provider_name}")
async def telephony_status_callback(provider_name: str, request: Request) -> dict:
    try:
        provider = get_telephony_provider(provider_name)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown telephony provider: {provider_name}") from None

    body = await request.body()
    if not provider.validate_webhook(dict(request.headers), body, str(request.url)):
        logger.warning("webhook_validation_failed", extra={"provider": provider_name})
        raise HTTPException(status_code=403, detail="Webhook signature validation failed")

    form = await request.form()
    payload = dict(form)
    event = provider.parse_call_event(payload)

    async with session_scope() as session:
        call_repo = CallRepository(session)
        attempt = await call_repo.get_attempt_by_provider_call_id(event.provider_call_id)
        if attempt is None:
            logger.warning("webhook_unknown_call", extra={"provider_call_id": event.provider_call_id})
            return {"status": "ignored", "reason": "unknown call"}

        fields: dict = {}
        if event.status == CallStatus.IN_PROGRESS and attempt.started_at is None:
            from datetime import datetime, timezone

            fields["started_at"] = datetime.now(timezone.utc)
        if event.status in (CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY, CallStatus.CANCELED):
            from datetime import datetime, timezone

            fields["ended_at"] = datetime.now(timezone.utc)

        await call_repo.mark_attempt_status(attempt.id, event.status.value, **fields)

    return {"status": "ok"}

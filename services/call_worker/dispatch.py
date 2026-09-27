"""Call dispatch logic (Section 19): acquire a lead, respect call window and
concurrency limits, create an idempotent call attempt, and hand it to the telephony
provider. This is intentionally just the *dispatch* half — the realtime conversation
itself starts once the provider's webhook confirms the call connected and the media
WebSocket is established (apps/api/routers/webhooks.py + voice/session).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CallAttempt, Campaign
from database.repositories.call_repository import CallRepository
from database.repositories.lead_repository import LeadRepository
from telephony.base import OutboundCallRequest, TelephonyProvider

logger = logging.getLogger(__name__)


def _within_call_window(campaign: Campaign, now: datetime | None = None) -> bool:
    tz = ZoneInfo(campaign.timezone)
    current = (now or datetime.now(tz)).astimezone(tz).time()
    start = datetime.strptime(campaign.call_window_start, "%H:%M").time()
    end = datetime.strptime(campaign.call_window_end, "%H:%M").time()
    return start <= current <= end


async def _active_call_count(session: AsyncSession, campaign_id) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(CallAttempt)
        .where(
            CallAttempt.campaign_id == campaign_id,
            CallAttempt.status.in_(["queued", "dialing", "ringing", "in_progress"]),
        )
    )
    return int(result.scalar_one())


async def dispatch_next_call(
    session: AsyncSession,
    campaign: Campaign,
    telephony: TelephonyProvider,
    media_websocket_base_url: str,
    status_callback_base_url: str,
) -> bool:
    """Attempt to start exactly one new call for this campaign. Returns True if a call
    was dispatched, False if there was nothing to do (no pending leads, outside call
    window, or at concurrency limit) — callers loop on this per active campaign."""
    if campaign.status != "active":
        return False
    if not _within_call_window(campaign):
        return False
    if await _active_call_count(session, campaign.id) >= campaign.max_concurrent_calls:
        return False

    lead_repo = LeadRepository(session)
    call_repo = CallRepository(session)

    lead = await lead_repo.claim_next_pending(campaign.id)
    if lead is None:
        return False

    if await lead_repo.is_suppressed(campaign.tenant_id, lead.phone_e164):
        lead.status = "suppressed"
        await session.flush()
        return False

    attempt_number = lead.attempts_count + 1
    attempt = CallAttempt(
        campaign_id=campaign.id,
        lead_id=lead.id,
        attempt_number=attempt_number,
        provider=telephony.name,
        status="queued",
    )
    try:
        await call_repo.create_attempt(attempt)
    except IntegrityError:
        # Another worker already created this exact (campaign, lead, attempt_number)
        # — idempotency guard (Section 19). Not an error, just a race we lost.
        await session.rollback()
        return False

    request = OutboundCallRequest(
        to_number=lead.phone_e164,
        from_number="",  # provider adapters fall back to their configured caller ID
        campaign_id=str(campaign.id),
        lead_id=str(lead.id),
        attempt_number=attempt_number,
        media_websocket_url=f"{media_websocket_base_url}/{attempt.id}",
        status_callback_url=f"{status_callback_base_url}/{telephony.name}",
    )

    try:
        result = await telephony.create_outbound_call(request)
    except Exception as exc:  # noqa: BLE001 - telephony failures must not crash the dispatch loop
        logger.exception("outbound_call_failed", extra={"lead_id": str(lead.id)})
        attempt.status = "failed"
        attempt.error = str(exc)
        lead.attempts_count += 1
        lead.status = "pending" if lead.attempts_count < 3 else "failed"  # PLACEHOLDER retry cap — confirm with client
        await session.flush()
        return False

    attempt.provider_call_id = result.provider_call_id
    attempt.status = result.status.value
    lead.attempts_count += 1
    lead.status = "in_progress"
    await session.flush()
    return True

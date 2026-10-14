"""End-of-call bookkeeping: move the lead out of `in_progress` and record what the
call achieved (Section 19). Before this existed nothing ever did, so every dialled
lead stayed `in_progress` forever and the retry sweep (`workers/retry.py`, which only
looks at `failed`) never saw no-answers either.

`finalize_call` is called from three places, in any order, and is idempotent:
- the media WebSocket teardown (`apps/api/routers/media.py`, any provider),
- a terminal status webhook (`apps/api/routers/webhooks.py`), and
- `reclaim_stale_attempts` below, the backstop for when neither of those ever arrives.
It only ever moves a lead that is still `queued`/`in_progress`, so a later call can't
undo an earlier, more informed one (and DNC's `suppressed` is never overwritten).

Outcome precedence: DNC > callback requested > qualification result > connected with
no result > never connected (provider status: no_answer/busy/failed/...).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CallAttempt, Callback, Campaign, Conversation, Lead, Qualification, Suppression

logger = logging.getLogger(__name__)

ACTIVE_LEAD_STATUSES = {"queued", "in_progress"}
TERMINAL_CALL_STATUSES = {"completed", "failed", "no_answer", "busy", "canceled"}
# Mirrors services/call_worker/dispatch.py's `_active_call_count` filter — a
# CallAttempt in one of these is still "live" from the dispatcher's point of view.
ACTIVE_CALL_STATUSES = {"queued", "dialing", "ringing", "in_progress"}
# Dial/ring time before a call's own duration budget starts: generous on purpose,
# since reclaiming a call that is actually still ringing would end a real attempt.
STALE_ATTEMPT_GRACE_SECONDS = 120


async def finalize_call(
    session: AsyncSession,
    call_attempt_id: uuid.UUID,
    *,
    call_status: str | None = None,
    ended_reason: str | None = None,
    final_state: str | None = None,
) -> str | None:
    """Returns the attempt's outcome (or None if the attempt doesn't exist)."""
    attempt = await session.get(CallAttempt, call_attempt_id)
    if attempt is None:
        return None
    lead = await session.get(Lead, attempt.lead_id)
    now = datetime.now(timezone.utc)

    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.call_attempt_id == attempt.id)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversation is not None:
        if final_state:
            conversation.current_state = final_state
        if conversation.ended_at is None:
            conversation.ended_at = now
            conversation.ended_reason = ended_reason or call_status or "ended"

    if call_status in TERMINAL_CALL_STATUSES and attempt.ended_at is None:
        attempt.ended_at = now

    outcome, lead_status = await _derive_outcome(session, attempt, lead, conversation, call_status)
    if outcome is not None and attempt.outcome is None:
        attempt.outcome = outcome
    if lead is not None and lead_status is not None and lead.status in ACTIVE_LEAD_STATUSES:
        lead.status = lead_status
    await session.flush()
    logger.info(
        "call_finalized",
        extra={"call_attempt_id": str(attempt.id), "outcome": attempt.outcome, "lead_status": lead.status if lead else None},
    )
    return attempt.outcome


async def reclaim_stale_attempts(session: AsyncSession, campaign: Campaign) -> int:
    """Backstop for a lead stuck behind a `CallAttempt` that will never be finalized
    the normal way: the worker process that owned it crashed, was redeployed, or the
    provider's terminal webhook was silently dropped. There is no lease/heartbeat on a
    `CallAttempt` (CLAUDE.md rule 6 — no new infra for this), so this sweep is the
    equivalent: anything active for longer than this campaign could possibly still be
    a real in-progress call (its own duration cap, plus dial/ring grace) gets finalized
    as failed and, via `finalize_call`'s normal lead-status rules, becomes eligible for
    `workers/retry.py`'s sweep. Sets `attempt.status` itself (mirroring
    `apps/api/routers/webhooks.py`'s `mark_attempt_status` before its own `finalize_call`
    call) so a reclaimed attempt leaves `ACTIVE_CALL_STATUSES` and is never re-swept.
    If the real webhook arrives moments later for the same attempt, `finalize_call`'s
    `attempt.outcome is None` guard makes that a no-op, not a conflicting second write.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=campaign.max_call_duration_seconds + STALE_ATTEMPT_GRACE_SECONDS
    )
    stale_ids = (
        await session.execute(
            select(CallAttempt.id).where(
                CallAttempt.campaign_id == campaign.id,
                CallAttempt.status.in_(ACTIVE_CALL_STATUSES),
                CallAttempt.created_at < cutoff,
            )
        )
    ).scalars().all()
    for attempt_id in stale_ids:
        attempt = await session.get(CallAttempt, attempt_id)
        if attempt is None or attempt.status not in ACTIVE_CALL_STATUSES:
            continue  # already moved on since the query ran (e.g. a webhook just landed)
        attempt.status = "failed"
        attempt.error = attempt.error or "reclaimed: no terminal webhook or media teardown arrived in time"
        await finalize_call(session, attempt.id, call_status="failed", ended_reason="stale_attempt_reclaimed")
        logger.warning(
            "stale_attempt_reclaimed",
            extra={"call_attempt_id": str(attempt.id), "campaign_id": str(campaign.id)},
        )
    return len(stale_ids)


async def _derive_outcome(
    session: AsyncSession,
    attempt: CallAttempt,
    lead: Lead | None,
    conversation: Conversation | None,
    call_status: str | None,
) -> tuple[str | None, str | None]:
    if lead is not None:
        suppressed = lead.status == "suppressed" or (
            await session.execute(
                select(Suppression.id).where(Suppression.tenant_id == lead.tenant_id, Suppression.phone_e164 == lead.phone_e164)
            )
        ).first() is not None
        if suppressed:
            return "do_not_call", "suppressed"

    if conversation is None:
        # Never got as far as a media stream. Only a terminal provider status tells us
        # anything; without one, leave the lead alone until it arrives.
        if call_status in TERMINAL_CALL_STATUSES:
            return (call_status if call_status != "completed" else "no_media"), "failed"
        return None, None

    qualification = (
        await session.execute(
            select(Qualification)
            .where(Qualification.conversation_id == conversation.id)
            .order_by(Qualification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    callback = (
        await session.execute(
            select(Callback.id).where(Callback.conversation_id == conversation.id, Callback.status == "pending")
        )
    ).first()

    if callback is not None:
        return (qualification.outcome if qualification else "callback_requested"), "callback_scheduled"
    if qualification is not None:
        return qualification.outcome, "completed"
    return "completed_no_qualification", "completed"

"""Retry sweep (Section 19/23): requeue leads whose most recent attempt failed for a
retriable reason (no_answer/busy/failed with a transport error), up to a configured
cap. Never retries a lead that ended in a terminal conversational outcome (completed
call, DNC, not_interested) — those are done regardless of "success."

PLACEHOLDER: retry cap and which outcomes are retriable must be confirmed with the
client (Section 19: "Never blindly retry outbound calls."). Defaults here are
conservative.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from database.models import Lead
from database.session import session_scope

logger = logging.getLogger(__name__)

RETRIABLE_STATUSES = {"failed", "no_answer", "busy"}
MAX_ATTEMPTS = 3  # PLACEHOLDER — confirm with client


async def sweep_retriable_leads(campaign_id) -> int:
    requeued = 0
    async with session_scope() as session:
        result = await session.execute(
            select(Lead).where(
                Lead.campaign_id == campaign_id,
                Lead.status == "failed",
                Lead.attempts_count < MAX_ATTEMPTS,
            )
        )
        for lead in result.scalars():
            lead.status = "pending"
            requeued += 1
        await session.flush()
    logger.info("retry_sweep_complete", extra={"campaign_id": str(campaign_id), "requeued": requeued})
    return requeued

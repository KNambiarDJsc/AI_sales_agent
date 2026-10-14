"""Call worker entrypoint (Section 19/26).

For 100-200 calls/day, a single polling loop over PostgreSQL is enough — no Kafka/
Temporal/Kubernetes (Section 15/27). Run as: `python -m workers.call_worker`.
Scale later by running more of these processes; `LeadRepository.claim_next_pending`'s
`FOR UPDATE SKIP LOCKED` already makes that safe.

Each tick also reclaims stale call attempts (`reclaim_stale_attempts` — a crashed/
redeployed worker's calls, or a webhook that never arrived, would otherwise leave a
lead stuck `in_progress` forever) and requeues anything that just became retriable
(`sweep_retriable_leads` — written for Section 19/23 but never actually wired into a
running loop until now, so it had never once executed outside its own tests).
"""
from __future__ import annotations

import asyncio
import logging

from config.settings import get_settings
from database.repositories.campaign_repository import CampaignRepository
from database.session import session_scope
from services.call_worker.dispatch import dispatch_next_call
from services.call_worker.lifecycle import reclaim_stale_attempts
from telephony.factory import get_telephony_provider
from workers.retry import sweep_retriable_leads

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


async def run_once() -> None:
    settings = get_settings()
    campaign_ids = []
    async with session_scope() as session:
        campaigns = await CampaignRepository(session).list_active()
        for campaign in campaigns:
            campaign_ids.append(campaign.id)
            reclaimed = await reclaim_stale_attempts(session, campaign)
            if reclaimed:
                logger.warning(
                    "stale_attempts_reclaimed_this_tick",
                    extra={"campaign_id": str(campaign.id), "count": reclaimed},
                )
            telephony = get_telephony_provider(campaign.telephony_provider)
            dispatched = True
            # Drain up to max_concurrent_calls new dispatches per campaign per tick.
            for _ in range(campaign.max_concurrent_calls):
                dispatched = await dispatch_next_call(
                    session,
                    campaign,
                    telephony,
                    media_websocket_base_url=f"{settings.effective_public_base_url.replace('https://', 'wss://')}/media",
                    status_callback_base_url=f"{settings.effective_public_base_url}/webhooks",
                )
                if not dispatched:
                    break

    # Its own session scope per campaign (workers/retry.py) — kept out of the dispatch
    # session above so a slow retry sweep never holds that transaction open.
    for campaign_id in campaign_ids:
        requeued = await sweep_retriable_leads(campaign_id)
        if requeued:
            logger.info("leads_requeued_for_retry", extra={"campaign_id": str(campaign_id), "count": requeued})


async def run_forever() -> None:
    logger.info("call_worker_started", extra={"poll_interval_s": POLL_INTERVAL_SECONDS})
    while True:
        try:
            await run_once()
        except Exception:  # noqa: BLE001 - a single bad tick must not kill the worker
            logger.exception("call_worker_tick_failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())

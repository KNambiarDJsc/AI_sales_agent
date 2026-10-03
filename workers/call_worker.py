"""Call worker entrypoint (Section 19/26).

For 100-200 calls/day, a single polling loop over PostgreSQL is enough — no Kafka/
Temporal/Kubernetes (Section 15/27). Run as: `python -m workers.call_worker`.
Scale later by running more of these processes; `LeadRepository.claim_next_pending`'s
`FOR UPDATE SKIP LOCKED` already makes that safe.
"""
from __future__ import annotations

import asyncio
import logging

from config.settings import get_settings
from database.repositories.campaign_repository import CampaignRepository
from database.session import session_scope
from services.call_worker.dispatch import dispatch_next_call
from telephony.factory import get_telephony_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


async def run_once() -> None:
    settings = get_settings()
    async with session_scope() as session:
        campaigns = await CampaignRepository(session).list_active()
        for campaign in campaigns:
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

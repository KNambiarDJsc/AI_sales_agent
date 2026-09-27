from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Lead, Suppression


class LeadRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def bulk_create(self, leads: list[Lead]) -> list[Lead]:
        self.session.add_all(leads)
        await self.session.flush()
        return leads

    async def get(self, lead_id: uuid.UUID) -> Lead | None:
        return await self.session.get(Lead, lead_id)

    async def find_by_dedupe_key(self, campaign_id: uuid.UUID, dedupe_key: str) -> Lead | None:
        result = await self.session.execute(
            select(Lead).where(Lead.campaign_id == campaign_id, Lead.dedupe_key == dedupe_key)
        )
        return result.scalar_one_or_none()

    async def is_suppressed(self, tenant_id: uuid.UUID, phone_e164: str) -> bool:
        result = await self.session.execute(
            select(Suppression).where(Suppression.tenant_id == tenant_id, Suppression.phone_e164 == phone_e164)
        )
        return result.scalar_one_or_none() is not None

    async def claim_next_pending(self, campaign_id: uuid.UUID) -> Lead | None:
        """Lease the next pending lead for this campaign with FOR UPDATE SKIP LOCKED so
        concurrent workers never double-dial the same lead (Section 15/19)."""
        result = await self.session.execute(
            select(Lead)
            .where(Lead.campaign_id == campaign_id, Lead.status == "pending")
            .order_by(Lead.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        lead = result.scalar_one_or_none()
        if lead is None:
            return None
        lead.status = "queued"
        await self.session.flush()
        return lead

    async def set_status(self, lead_id: uuid.UUID, status: str) -> None:
        await self.session.execute(update(Lead).where(Lead.id == lead_id).values(status=status))

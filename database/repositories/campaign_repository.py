from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Campaign


class CampaignRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, campaign: Campaign) -> Campaign:
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def get(self, campaign_id: uuid.UUID) -> Campaign | None:
        return await self.session.get(Campaign, campaign_id)

    async def list_for_tenant(self, tenant_id: uuid.UUID) -> list[Campaign]:
        result = await self.session.execute(select(Campaign).where(Campaign.tenant_id == tenant_id))
        return list(result.scalars().all())

    async def list_active(self) -> list[Campaign]:
        result = await self.session.execute(select(Campaign).where(Campaign.status == "active"))
        return list(result.scalars().all())

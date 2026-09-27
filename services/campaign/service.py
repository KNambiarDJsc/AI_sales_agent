"""Campaign domain service: the thin layer apps/api routers call into. Keeps
suppression-checking and lead creation in one place so both the API and any future
bulk-import CLI use the same rules (Section 14: "validate suppression/consent fields").
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Campaign, Lead
from database.repositories.campaign_repository import CampaignRepository
from database.repositories.lead_repository import LeadRepository
from services.lead_import.importer import ParsedLeadRow


class CampaignService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.campaigns = CampaignRepository(session)
        self.leads = LeadRepository(session)

    async def create_campaign(
        self,
        tenant_id: uuid.UUID,
        name: str,
        script_id: str,
        script_version: int,
        telephony_provider: str,
        call_window_start: str,
        call_window_end: str,
        timezone: str,
        max_concurrent_calls: int,
        max_call_duration_seconds: int,
    ) -> Campaign:
        campaign = Campaign(
            tenant_id=tenant_id,
            name=name,
            script_id=script_id,
            script_version=script_version,
            status="draft",
            telephony_provider=telephony_provider,
            call_window_start=call_window_start,
            call_window_end=call_window_end,
            timezone=timezone,
            max_concurrent_calls=max_concurrent_calls,
            max_call_duration_seconds=max_call_duration_seconds,
        )
        return await self.campaigns.create(campaign)

    async def import_leads(
        self, tenant_id: uuid.UUID, campaign_id: uuid.UUID, parsed_rows: list[ParsedLeadRow]
    ) -> dict[str, int]:
        created = 0
        skipped_suppressed = 0
        skipped_existing = 0

        for row in parsed_rows:
            if await self.leads.is_suppressed(tenant_id, row.phone_e164):
                skipped_suppressed += 1
                continue
            existing = await self.leads.find_by_dedupe_key(campaign_id, row.dedupe_key)
            if existing is not None:
                skipped_existing += 1
                continue

            lead = Lead(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                phone_e164=row.phone_e164,
                raw_phone=row.raw_phone,
                dedupe_key=row.dedupe_key,
                contact_name=row.contact_name,
                business_name=row.business_name,
                extra=row.extra,
                consent_flag=row.consent_flag,
                source_row=row.source_row,
                status="pending",
            )
            self.session.add(lead)
            created += 1

        await self.session.flush()
        return {"created": created, "skipped_suppressed": skipped_suppressed, "skipped_existing": skipped_existing}

    async def start_campaign(self, campaign_id: uuid.UUID) -> Campaign | None:
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            return None
        campaign.status = "active"
        await self.session.flush()
        return campaign

    async def pause_campaign(self, campaign_id: uuid.UUID) -> Campaign | None:
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            return None
        campaign.status = "paused"
        await self.session.flush()
        return campaign

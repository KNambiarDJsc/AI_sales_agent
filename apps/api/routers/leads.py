from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_tenant_id
from database.models import Lead, Suppression
from database.session import get_session

router = APIRouter(prefix="/leads", tags=["leads"])


class LeadResponse(BaseModel):
    id: uuid.UUID
    phone_e164: str
    contact_name: str | None
    business_name: str | None
    status: str
    attempts_count: int


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(
    lead_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> LeadResponse:
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    return LeadResponse(
        id=lead.id, phone_e164=lead.phone_e164, contact_name=lead.contact_name,
        business_name=lead.business_name, status=lead.status, attempts_count=lead.attempts_count,
    )


@router.get("", response_model=list[LeadResponse])
async def list_leads(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> list[LeadResponse]:
    result = await session.execute(
        select(Lead).where(Lead.tenant_id == tenant_id, Lead.campaign_id == campaign_id)
    )
    return [
        LeadResponse(
            id=lead.id, phone_e164=lead.phone_e164, contact_name=lead.contact_name,
            business_name=lead.business_name, status=lead.status, attempts_count=lead.attempts_count,
        )
        for lead in result.scalars()
    ]


@router.post("/{lead_id}/dnc")
async def manual_mark_dnc(
    lead_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Manual DNC entry point (e.g. a sales rep marks a number DNC outside a call) —
    separate from the in-call `mark_dnc` tool but writes to the same Suppression table
    so both paths are honored identically by dial-time checks."""
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Lead not found")

    existing = await session.execute(
        select(Suppression).where(Suppression.tenant_id == tenant_id, Suppression.phone_e164 == lead.phone_e164)
    )
    if existing.scalar_one_or_none() is None:
        session.add(Suppression(tenant_id=tenant_id, phone_e164=lead.phone_e164, reason="manual", source="api"))
    lead.status = "suppressed"
    await session.flush()
    return {"status": "suppressed"}

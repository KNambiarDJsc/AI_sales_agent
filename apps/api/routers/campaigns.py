from __future__ import annotations

import io
import uuid

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_tenant_id
from config.settings import get_settings
from database.session import get_session
from services.campaign.service import CampaignService
from services.lead_import.importer import guess_column_mapping, import_leads
from workers.export import export_campaign_qualifications_csv

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


class CreateCampaignRequest(BaseModel):
    name: str
    script_id: str = "product-a"
    script_version: int = 1
    telephony_provider: str | None = None
    call_window_start: str | None = None
    call_window_end: str | None = None
    timezone: str | None = None
    max_concurrent_calls: int | None = None
    max_call_duration_seconds: int | None = None


class CampaignResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    script_id: str
    script_version: int


@router.post("", response_model=CampaignResponse)
async def create_campaign(
    body: CreateCampaignRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> CampaignResponse:
    settings = get_settings()
    service = CampaignService(session)
    campaign = await service.create_campaign(
        tenant_id=tenant_id,
        name=body.name,
        script_id=body.script_id,
        script_version=body.script_version,
        telephony_provider=body.telephony_provider or settings.telephony_provider,
        call_window_start=body.call_window_start or settings.default_call_window_start,
        call_window_end=body.call_window_end or settings.default_call_window_end,
        timezone=body.timezone or settings.default_timezone,
        max_concurrent_calls=body.max_concurrent_calls or settings.default_max_concurrent_calls,
        max_call_duration_seconds=body.max_call_duration_seconds or settings.default_max_call_duration_seconds,
    )
    return CampaignResponse(
        id=campaign.id, name=campaign.name, status=campaign.status,
        script_id=campaign.script_id, script_version=campaign.script_version,
    )


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> CampaignResponse:
    service = CampaignService(session)
    campaign = await service.campaigns.get(campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignResponse(
        id=campaign.id, name=campaign.name, status=campaign.status,
        script_id=campaign.script_id, script_version=campaign.script_version,
    )


@router.post("/{campaign_id}/leads/import")
async def import_campaign_leads(
    campaign_id: uuid.UUID,
    file: UploadFile,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    service = CampaignService(session)
    campaign = await service.campaigns.get(campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")

    file_bytes = await file.read()
    buffer = io.BytesIO(file_bytes)
    if file.filename.lower().endswith(".xlsx"):
        columns = list(pd.read_excel(buffer, nrows=0).columns)
    else:
        columns = list(pd.read_csv(buffer, nrows=0).columns)

    mapping = guess_column_mapping(columns)
    if "phone" not in mapping:
        raise HTTPException(
            status_code=422,
            detail=f"Could not identify a phone number column among: {columns}. "
            "Rename the column to one of the recognized aliases, or contact support to configure an explicit mapping.",
        )

    parsed_rows, report = import_leads(file_bytes, file.filename, mapping)
    result = await service.import_leads(tenant_id, campaign_id, parsed_rows)
    return {
        "column_mapping_used": mapping,
        "parse_report": {
            "total_rows": report.total_rows,
            "valid_rows": report.valid_rows,
            "duplicate_within_file": report.duplicate_within_file,
            "errors": [{"row": e.row_index, "reason": e.reason} for e in report.errors],
        },
        "import_result": result,
    }


@router.post("/{campaign_id}/start")
async def start_campaign(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    service = CampaignService(session)
    campaign = await service.campaigns.get(campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    await service.start_campaign(campaign_id)
    return {"status": "active"}


@router.post("/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    service = CampaignService(session)
    campaign = await service.campaigns.get(campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    await service.pause_campaign(campaign_id)
    return {"status": "paused"}


@router.get("/{campaign_id}/export")
async def export_campaign(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> Response:
    service = CampaignService(session)
    campaign = await service.campaigns.get(campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    csv_content = await export_campaign_qualifications_csv(tenant_id, campaign_id, requested_by=str(tenant_id))
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-qualifications.csv"'},
    )

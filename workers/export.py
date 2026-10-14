"""CSV export (Section 21/26). Google Sheets/CRM/webhook exports are explicitly future
work (Section 21) — not built until the client specifies a CRM.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from database.models import CallAttempt, ExportJob, Lead, Qualification
from database.session import session_scope

EXPORT_FIELDS = [
    "lead_id",
    "phone",
    "contact_name",
    "business_name",
    "lead_status",
    "call_outcome",
    "qualification_outcome",
    "qualified",
    "confidence",
    "sales_followup_required",
    "summary",
    "next_action",
    "objections",
    "created_at",
]


async def export_campaign_qualifications_csv(tenant_id: uuid.UUID, campaign_id: uuid.UUID, requested_by: str) -> str:
    """Returns the CSV content as a string and records an ExportJob row. Writing the
    file to durable storage (S3/etc) is left to the caller — this just produces bytes.

    One row per lead in the campaign, not just leads with a Qualification row: the
    sales script only allows `create_qualification` from QUALIFICATION/INTERESTED/
    UNCERTAIN (config/scripts/product-a.yaml), so a DNC'd, not-interested, wrong-number,
    no-answer, busy, or failed lead never gets one — the old inner join silently dropped
    every one of those from the export the sales team actually works from. `call_outcome`
    (CallAttempt.outcome, set for every finalized attempt by
    services/call_worker/lifecycle.py) is the one field guaranteed to be populated
    regardless of whether a qualification was ever recorded.
    """
    async with session_scope() as session:
        job = ExportJob(tenant_id=tenant_id, campaign_id=campaign_id, status="running", requested_by=requested_by)
        session.add(job)
        await session.flush()

        leads = (
            await session.execute(
                select(Lead)
                .where(Lead.tenant_id == tenant_id, Lead.campaign_id == campaign_id)
                .order_by(Lead.created_at)
            )
        ).scalars().all()
        lead_ids = [lead.id for lead in leads]

        latest_qualification_by_lead: dict[uuid.UUID, Qualification] = {}
        latest_attempt_by_lead: dict[uuid.UUID, CallAttempt] = {}
        if lead_ids:
            qualifications = (
                await session.execute(
                    select(Qualification).where(Qualification.lead_id.in_(lead_ids)).order_by(Qualification.created_at)
                )
            ).scalars().all()
            for q in qualifications:
                latest_qualification_by_lead[q.lead_id] = q  # ascending order: last write wins

            attempts = (
                await session.execute(
                    select(CallAttempt).where(CallAttempt.lead_id.in_(lead_ids)).order_by(CallAttempt.attempt_number)
                )
            ).scalars().all()
            for a in attempts:
                latest_attempt_by_lead[a.lead_id] = a  # ascending order: last write wins

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for lead in leads:
            qualification = latest_qualification_by_lead.get(lead.id)
            attempt = latest_attempt_by_lead.get(lead.id)
            created_at = qualification.created_at if qualification else (attempt.ended_at if attempt else None)
            writer.writerow(
                {
                    "lead_id": str(lead.id),
                    "phone": lead.phone_e164,
                    "contact_name": lead.contact_name or "",
                    "business_name": lead.business_name or "",
                    "lead_status": lead.status,
                    "call_outcome": (attempt.outcome if attempt else "") or "",
                    "qualification_outcome": qualification.outcome if qualification else "",
                    "qualified": qualification.qualified if qualification else "",
                    "confidence": qualification.confidence if qualification else "",
                    "sales_followup_required": qualification.sales_followup_required if qualification else "",
                    "summary": qualification.summary or "" if qualification else "",
                    "next_action": qualification.next_action or "" if qualification else "",
                    "objections": "; ".join(qualification.objections or []) if qualification else "",
                    "created_at": created_at.isoformat() if created_at else "",
                }
            )

        job.status = "done"
        job.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return buffer.getvalue()

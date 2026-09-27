"""CSV export (Section 21/26). Google Sheets/CRM/webhook exports are explicitly future
work (Section 21) — not built until the client specifies a CRM.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from database.models import ExportJob, Lead, Qualification
from database.session import session_scope

EXPORT_FIELDS = [
    "lead_id",
    "phone",
    "contact_name",
    "business_name",
    "outcome",
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
    file to durable storage (S3/etc) is left to the caller — this just produces bytes."""
    async with session_scope() as session:
        job = ExportJob(tenant_id=tenant_id, campaign_id=campaign_id, status="running", requested_by=requested_by)
        session.add(job)
        await session.flush()

        result = await session.execute(
            select(Qualification, Lead)
            .join(Lead, Lead.id == Qualification.lead_id)
            .where(Lead.campaign_id == campaign_id)
        )
        rows = result.all()

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for qualification, lead in rows:
            writer.writerow(
                {
                    "lead_id": str(lead.id),
                    "phone": lead.phone_e164,
                    "contact_name": lead.contact_name or "",
                    "business_name": lead.business_name or "",
                    "outcome": qualification.outcome,
                    "qualified": qualification.qualified,
                    "confidence": qualification.confidence,
                    "sales_followup_required": qualification.sales_followup_required,
                    "summary": qualification.summary or "",
                    "next_action": qualification.next_action or "",
                    "objections": "; ".join(qualification.objections or []),
                    "created_at": qualification.created_at.isoformat(),
                }
            )

        job.status = "done"
        job.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return buffer.getvalue()

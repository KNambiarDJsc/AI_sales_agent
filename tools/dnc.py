"""mark_dnc — Section 13/22: DNC enforcement must never depend solely on the LLM
remembering not to call back. This tool is the actual enforcement point: it writes a
Suppression row, which lead_import/campaign code must always check before dialing
(see database/repositories/lead_repository.py:is_suppressed and
services/call_worker's pre-dial check)."""
from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select

from database.models import Lead, Suppression
from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec


class MarkDNCInput(BaseModel):
    reason: str = "customer_request"


async def _handle(ctx: ToolContext, args: MarkDNCInput) -> ToolResult:
    lead = await ctx.session.get(Lead, ctx.lead_id)
    if lead is None:
        return ToolResult(success=False, message="Lead not found")

    existing = await ctx.session.execute(
        select(Suppression).where(Suppression.tenant_id == ctx.tenant_id, Suppression.phone_e164 == lead.phone_e164)
    )
    if existing.scalar_one_or_none() is None:
        ctx.session.add(
            Suppression(
                tenant_id=ctx.tenant_id,
                phone_e164=lead.phone_e164,
                reason=args.reason,
                source=f"conversation:{ctx.conversation_id}",
            )
        )
    lead.status = "suppressed"
    await ctx.session.flush()
    return ToolResult(success=True, message="Number suppressed", data={"phone_e164": lead.phone_e164})


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="mark_dnc",
            input_model=MarkDNCInput,
            handler=_handle,
            description="Suppress this lead's phone number from all future calls (Do Not Call).",
        )
    )

"""export_lead — Section 13/21. Flags a lead for inclusion in the next export run
rather than performing the export itself (that's workers/export.py's job, run on a
schedule or on demand via the campaign API) — keeps the in-call tool fast and free of
file/network I/O."""
from __future__ import annotations

from pydantic import BaseModel

from database.models import Lead
from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec


class ExportLeadInput(BaseModel):
    reason: str = "qualified"


async def _handle(ctx: ToolContext, args: ExportLeadInput) -> ToolResult:
    lead = await ctx.session.get(Lead, ctx.lead_id)
    if lead is None:
        return ToolResult(success=False, message="Lead not found")
    lead.extra = {**lead.extra, "export_requested": True, "export_reason": args.reason}
    await ctx.session.flush()
    return ToolResult(success=True, message="Lead flagged for export")


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="export_lead",
            input_model=ExportLeadInput,
            handler=_handle,
            description="Flag this lead for inclusion in the next export to the sales team.",
        )
    )

"""update_lead — Section 13: only an explicit allow-list of fields is mutable. The LLM
can never write to protected fields (tenant_id, campaign_id, status transitions that
bypass the state machine, dedupe_key, etc)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from database.models import Lead
from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec

_ALLOWED_FIELDS = {"contact_name", "business_name"}


class UpdateLeadInput(BaseModel):
    contact_name: str | None = None
    business_name: str | None = None
    extra_fields: dict[str, Any] = Field(default_factory=dict)  # merged into Lead.extra, never overwrites wholesale


async def _handle(ctx: ToolContext, args: UpdateLeadInput) -> ToolResult:
    lead = await ctx.session.get(Lead, ctx.lead_id)
    if lead is None:
        return ToolResult(success=False, message="Lead not found")

    updated_fields = []
    for field in _ALLOWED_FIELDS:
        value = getattr(args, field)
        if value is not None:
            setattr(lead, field, value)
            updated_fields.append(field)

    if args.extra_fields:
        lead.extra = {**lead.extra, **args.extra_fields}
        updated_fields.append("extra")

    await ctx.session.flush()
    return ToolResult(success=True, message="Lead updated", data={"updated_fields": updated_fields})


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="update_lead",
            input_model=UpdateLeadInput,
            handler=_handle,
            description="Update non-protected lead fields captured during the call.",
        )
    )

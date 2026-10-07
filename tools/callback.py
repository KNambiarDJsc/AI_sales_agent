"""schedule_callback — idempotent per conversation: at most one pending callback per
call. Caught live in the browser demo: the LLM re-proposed schedule_callback with the
same time on the confirming turn ("Yes that's right"), which inserted a second row.
A repeat call now updates the existing pending callback instead of adding another, so
re-confirming or correcting the time ("actually make it 9") both converge on one row."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select

from database.models import Callback
from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec


class ScheduleCallbackInput(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {"requested_time": "2026-10-08T08:00:00+05:30", "timezone": "Asia/Kolkata", "notes": "Prefers mornings."}
            ]
        }
    }

    requested_time: datetime | None = Field(
        default=None, description="Absolute ISO 8601 datetime with UTC offset, e.g. 2026-10-03T08:00:00+05:30."
    )
    timezone: str = Field(default="Asia/Kolkata", description="IANA timezone the customer gave the time in.")
    notes: str | None = Field(default=None, description="Anything the sales team should know for the callback.")


async def _handle(ctx: ToolContext, args: ScheduleCallbackInput) -> ToolResult:
    existing = (
        await ctx.session.execute(
            select(Callback)
            .where(Callback.conversation_id == ctx.conversation_id, Callback.status == "pending")
            .order_by(Callback.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.requested_time = args.requested_time or existing.requested_time
        existing.timezone = args.timezone
        existing.notes = args.notes or existing.notes
        await ctx.session.flush()
        return ToolResult(success=True, message="Callback updated", data={"callback_id": str(existing.id)})

    callback = Callback(
        lead_id=ctx.lead_id,
        conversation_id=ctx.conversation_id,
        requested_time=args.requested_time,
        timezone=args.timezone,
        status="pending",
        notes=args.notes,
    )
    ctx.session.add(callback)
    await ctx.session.flush()
    return ToolResult(success=True, message="Callback scheduled", data={"callback_id": str(callback.id)})


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="schedule_callback",
            input_model=ScheduleCallbackInput,
            handler=_handle,
            description="Record a requested callback time for this lead.",
        )
    )

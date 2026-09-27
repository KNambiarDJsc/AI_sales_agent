from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from database.models import Callback
from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec


class ScheduleCallbackInput(BaseModel):
    requested_time: datetime | None = None
    timezone: str = "Asia/Kolkata"
    notes: str | None = None


async def _handle(ctx: ToolContext, args: ScheduleCallbackInput) -> ToolResult:
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

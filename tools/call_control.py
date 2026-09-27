"""end_call — Section 13. This tool only records intent; it does not itself hang up
the phone line. It has no access to the TelephonyProvider (Section 13: no unrestricted
access of any kind for tools beyond their narrow job). The voice session / call worker
is what actually calls TelephonyProvider.hangup_call() once it sees
AgentResponseProposal.end_call=True or a successful end_call tool result — see
orchestrator/engine.py."""
from __future__ import annotations

from pydantic import BaseModel

from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec


class EndCallInput(BaseModel):
    reason: str = "conversation_complete"


async def _handle(ctx: ToolContext, args: EndCallInput) -> ToolResult:
    return ToolResult(success=True, message=f"Call end requested: {args.reason}")


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="end_call",
            input_model=EndCallInput,
            handler=_handle,
            description="Signal that the call should end after this turn's speech is delivered.",
        )
    )

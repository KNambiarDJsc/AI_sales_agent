"""Internal typed tool registry (Section 13). No MCP in the MVP — every tool is a
plain Python function with a Pydantic input schema, checked against the current
state's allowed-tools list before it runs. The LLM never gets raw DB/HTTP/shell access;
it can only ever reach one of these, and only the ones the active script allows in the
current state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Everything a tool is allowed to touch. Tools never receive the raw DB session
    plus free rein — repositories still enforce tenant/campaign/lead scoping."""

    session: AsyncSession
    tenant_id: UUID
    campaign_id: UUID
    lead_id: UUID
    conversation_id: UUID
    script_version: int
    current_state: str
    allowed_tools: frozenset[str]


class ToolResult(BaseModel):
    success: bool
    message: str = ""
    data: dict[str, Any] = {}


class ToolHandler(Protocol):
    async def __call__(self, ctx: ToolContext, args: BaseModel) -> ToolResult: ...


@dataclass
class ToolSpec:
    name: str
    input_model: type[BaseModel]
    handler: ToolHandler
    description: str = ""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all_names(self) -> list[str]:
        return list(self._tools)

    async def invoke(self, ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
        spec = self.get(name)
        if spec is None:
            logger.warning("tool_not_found", extra={"tool": name})
            return ToolResult(success=False, message=f"Unknown tool: {name}")

        # Defense in depth: the orchestrator validator already checked this against
        # the script's allowed_tools, but a tool must never trust a caller that skips
        # the orchestrator (Section 13/22).
        if name not in ctx.allowed_tools:
            logger.warning("tool_not_authorized", extra={"tool": name, "state": ctx.current_state})
            return ToolResult(success=False, message=f"Tool '{name}' not authorized in state '{ctx.current_state}'")

        try:
            parsed_args = spec.input_model.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced to caller as a failed tool call, not a crash
            logger.warning("tool_input_invalid", extra={"tool": name, "error": str(exc)})
            return ToolResult(success=False, message=f"Invalid arguments for '{name}': {exc}")

        try:
            return await spec.handler(ctx, parsed_args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool_execution_failed", extra={"tool": name})
            # Section 23: if a tool fails, the agent must NOT claim the action succeeded.
            return ToolResult(success=False, message=f"Tool '{name}' failed: {exc}")


_default_registry: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    global _default_registry
    if _default_registry is None:
        from tools import call_control, callback, dnc, export, lead, qualification  # noqa: F401 (side-effect registration)

        _default_registry = ToolRegistry()
        for module in (dnc, callback, qualification, lead, call_control, export):
            module.register(_default_registry)
    return _default_registry

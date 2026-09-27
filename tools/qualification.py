"""create_qualification — Section 13/20. Note the LLM never sets `qualified` or
`outcome` directly: it can only report facts/dimensions/evidence it extracted from the
conversation. The actual qualification decision is computed here by the config-driven
scoring engine (qualification/scoring.py + config/qualification/rules.yaml), because
"final qualification authority" is explicitly not the LLM's to have (Section 10)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from database.models import Qualification
from qualification.rules import load_rules
from qualification.scoring import score_qualification
from tools.registry import ToolContext, ToolRegistry, ToolResult, ToolSpec


class CreateQualificationInput(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict)
    dimension_confidence: dict[str, float] = Field(default_factory=dict)  # need/fit/timing/authority/willingness/evidence -> 0..1
    objections: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)  # quotes/turn references backing the facts
    summary: str | None = None
    sales_followup_required: bool = False
    dnc_requested: bool = False


def _script_id_from_context(ctx: ToolContext) -> str:
    # PLACEHOLDER: single-script MVP. Once multiple scripts exist per tenant, resolve
    # script_id from the Conversation row rather than assuming "product-a".
    return "product-a"


async def _handle(ctx: ToolContext, args: CreateQualificationInput) -> ToolResult:
    if args.evidence == [] and load_rules(_script_id_from_context(ctx)).evidence_required:
        return ToolResult(success=False, message="evidence is required to create a qualification")

    rules = load_rules(_script_id_from_context(ctx))
    overall_confidence = (
        sum(args.dimension_confidence.values()) / len(args.dimension_confidence)
        if args.dimension_confidence
        else 0.0
    )
    score = score_qualification(rules, args.facts, overall_confidence, dnc_requested=args.dnc_requested)

    qualification = Qualification(
        conversation_id=ctx.conversation_id,
        lead_id=ctx.lead_id,
        outcome=score.outcome,
        qualified=score.qualified,
        confidence=score.confidence,
        dimensions=args.dimension_confidence,
        facts=args.facts,
        objections=args.objections,
        evidence=args.evidence,
        summary=args.summary,
        next_action="sales_followup" if args.sales_followup_required else None,
        script_version=ctx.script_version,
        model_version=None,
        sales_followup_required=args.sales_followup_required,
    )
    ctx.session.add(qualification)
    await ctx.session.flush()
    return ToolResult(
        success=True,
        message=f"Qualification recorded: {score.outcome}",
        data={"outcome": score.outcome, "qualified": score.qualified, "confidence": score.confidence},
    )


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="create_qualification",
            input_model=CreateQualificationInput,
            handler=_handle,
            description="Record extracted facts/evidence for this call; the qualification outcome is computed by config-driven rules, not by the caller.",
        )
    )

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
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "facts": {"interested_in_amazon_selling": True, "sufficient_business_info_captured": True,
                              "willing_to_be_contacted": True},
                    "dimension_confidence": {"need": 0.8, "fit": 0.7, "timing": 0.6, "authority": 0.8,
                                             "willingness": 0.9, "evidence": 0.8},
                    "objections": [],
                    "evidence": ["I'm really interested in selling on Amazon", "We make handmade soaps"],
                    "summary": "Makes handmade soaps, not online yet, wants a sales call.",
                    "sales_followup_required": True,
                    "dnc_requested": False,
                }
            ]
        }
    }

    # Descriptions are rendered into the LLM prompt (orchestrator/prompts.py), so they
    # say what to send, not how it's scored — scoring stays in qualification/scoring.py.
    facts: dict[str, Any] = Field(
        default_factory=dict,
        description="Every qualification fact established so far, using the exact fact keys listed under "
        "'Qualification facts', e.g. {\"interested_in_amazon_selling\": true}.",
    )
    dimension_confidence: dict[str, float] = Field(
        default_factory=dict,
        description="Your confidence 0..1 in each qualification dimension listed under 'Qualification facts'.",
    )
    objections: list[str] = Field(default_factory=list, description="Objections the customer raised, if any.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Short verbatim customer quotes from this call that back the facts. Required, at least one.",
    )
    summary: str | None = Field(default=None, description="One or two sentences summarising the call for the sales team.")
    sales_followup_required: bool = Field(default=False, description="True if the customer agreed to a sales follow-up.")
    dnc_requested: bool = Field(default=False, description="True if the customer asked not to be called again.")


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
            description="Record this call's qualification facts and evidence once they are known (call it before ending an "
            "interested/qualified call). The outcome is computed by the application's rules, not by you.",
        )
    )

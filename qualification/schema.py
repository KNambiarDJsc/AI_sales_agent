from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QualificationRules(BaseModel):
    script_id: str
    version: int
    outcomes: list[str]
    dnc_overrides_all: bool = True
    interested_conditions: list[str] = Field(default_factory=list)
    disqualification_conditions: list[str] = Field(default_factory=list)
    qualified_when: dict[str, Any] = Field(default_factory=dict)
    low_confidence_routes_to: str = "uncertain"
    evidence_required: bool = True
    dimensions: list[str] = Field(default_factory=list)


class QualificationScore(BaseModel):
    outcome: str
    qualified: bool
    confidence: float
    reasons: list[str] = Field(default_factory=list)

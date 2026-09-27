"""Qualification scoring (Section 20).

Deliberately config-driven and conservative: this is NOT "customer sounded positive."
It checks the specific facts config/qualification/rules.yaml requires, at the
confidence bar it requires, and routes anything short of that to `uncertain` rather
than guessing. DNC always wins regardless of everything else (Section 20/22) — that
check happens first and short-circuits the rest.
"""
from __future__ import annotations

from qualification.schema import QualificationRules, QualificationScore


def score_qualification(
    rules: QualificationRules,
    facts: dict[str, object],
    overall_confidence: float,
    dnc_requested: bool = False,
) -> QualificationScore:
    if dnc_requested and rules.dnc_overrides_all:
        return QualificationScore(
            outcome="do_not_call", qualified=False, confidence=1.0, reasons=["DNC requested — overrides all"]
        )

    for condition in rules.disqualification_conditions:
        if facts.get(condition):
            return QualificationScore(
                outcome=_disqualified_outcome(condition, rules),
                qualified=False,
                confidence=overall_confidence,
                reasons=[f"Disqualification condition met: {condition}"],
            )

    qualified_when = rules.qualified_when
    min_confidence = float(qualified_when.get("min_confidence", 0.6))
    all_of = qualified_when.get("all_of", [])

    conditions_met = []
    conditions_missed = []
    for condition in all_of:
        fact_name = condition.get("fact")
        expected = condition.get("equals")
        actual = facts.get(fact_name)
        if actual == expected:
            conditions_met.append(fact_name)
        else:
            conditions_missed.append(fact_name)

    if not conditions_missed and all_of and overall_confidence >= min_confidence:
        return QualificationScore(
            outcome="qualified",
            qualified=True,
            confidence=overall_confidence,
            reasons=[f"All required conditions met: {conditions_met}"],
        )

    if overall_confidence < min_confidence:
        return QualificationScore(
            outcome=rules.low_confidence_routes_to,
            qualified=False,
            confidence=overall_confidence,
            reasons=[f"Confidence {overall_confidence:.2f} below bar {min_confidence:.2f}"],
        )

    interested_hits = [c for c in rules.interested_conditions if facts.get(c)]
    if interested_hits:
        return QualificationScore(
            outcome="mild_interest",
            qualified=False,
            confidence=overall_confidence,
            reasons=[f"Some interest signals present but not all required facts confirmed: {interested_hits}",
                     f"Missing: {conditions_missed}"],
        )

    return QualificationScore(
        outcome="uncertain",
        qualified=False,
        confidence=overall_confidence,
        reasons=[f"Neither qualified nor clearly disqualified. Missing: {conditions_missed}"],
    )


def _disqualified_outcome(condition: str, rules: QualificationRules) -> str:
    mapping = {
        "not_interested": "not_interested",
        "do_not_call": "do_not_call",
        "wrong_number": "wrong_number",
    }
    return mapping.get(condition, "not_interested")

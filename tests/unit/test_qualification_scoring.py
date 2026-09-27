from qualification.schema import QualificationRules
from qualification.scoring import score_qualification


def _rules() -> QualificationRules:
    return QualificationRules(
        script_id="test",
        version=1,
        outcomes=["qualified", "not_interested", "do_not_call", "uncertain", "mild_interest"],
        dnc_overrides_all=True,
        interested_conditions=["explicit_interest"],
        disqualification_conditions=["not_interested", "do_not_call"],
        qualified_when={
            "all_of": [
                {"fact": "interested_in_amazon_selling", "equals": True},
                {"fact": "willing_to_be_contacted", "equals": True},
            ],
            "min_confidence": 0.6,
        },
        low_confidence_routes_to="uncertain",
    )


def test_dnc_overrides_everything():
    rules = _rules()
    facts = {"interested_in_amazon_selling": True, "willing_to_be_contacted": True}
    result = score_qualification(rules, facts, overall_confidence=0.9, dnc_requested=True)
    assert result.outcome == "do_not_call"
    assert result.qualified is False


def test_qualified_when_all_conditions_met_and_confident():
    rules = _rules()
    facts = {"interested_in_amazon_selling": True, "willing_to_be_contacted": True}
    result = score_qualification(rules, facts, overall_confidence=0.8)
    assert result.qualified is True
    assert result.outcome == "qualified"


def test_low_confidence_routes_to_uncertain_even_if_facts_match():
    rules = _rules()
    facts = {"interested_in_amazon_selling": True, "willing_to_be_contacted": True}
    result = score_qualification(rules, facts, overall_confidence=0.3)
    assert result.qualified is False
    assert result.outcome == "uncertain"


def test_disqualification_condition_short_circuits():
    rules = _rules()
    facts = {"not_interested": True, "interested_in_amazon_selling": True}
    result = score_qualification(rules, facts, overall_confidence=0.9)
    assert result.qualified is False
    assert result.outcome == "not_interested"


def test_partial_interest_without_full_qualification_is_mild_interest():
    rules = _rules()
    facts = {"explicit_interest": True, "interested_in_amazon_selling": True}  # missing willing_to_be_contacted
    result = score_qualification(rules, facts, overall_confidence=0.9)
    assert result.qualified is False
    assert result.outcome == "mild_interest"

import pytest

from src.services.creative_planning_models import ConstraintLedger, CreativeBrief, PlanCandidate, PlanScoreVector


def _brief():
    return CreativeBrief(briefId="brief_a", title="文化深游", primaryAxis="culture_deep_dive", requiredGoalIds=["campus", "museum"])


def _score(**changes):
    value = dict(hardConstraintPassed=True, preferenceFit=80, thematicCoherence=80, experienceDiversity=80, routeEfficiency=80, pacingQuality=80, novelty=80, robustness=80, uncertaintyPenalty=10)
    value.update(changes)
    return PlanScoreVector(**value)


def test_strict_models_reject_unowned_fields():
    with pytest.raises(Exception, match="extra_forbidden"):
        _brief().model_validate({**_brief().model_dump(by_alias=True), "amapId": "fake"})


def test_candidate_requires_distinct_proposal_and_brief_identity():
    with pytest.raises(ValueError, match="proposal_id_must_not_reuse_brief_id"):
        PlanCandidate(proposalId="brief_a", portfolioId="portfolio_a", brief=_brief(), itinerarySnapshot={}, score=_score(), verifier={}, canonicalSignature="a" * 16)


def test_ledger_does_not_allow_hard_soft_overlap():
    goal = {"goalId": "museum", "intentType": "museum"}
    with pytest.raises(ValueError, match="both_hard_and_soft"):
        ConstraintLedger(schemaVersion="constraint-ledger-v1", city="北京", dayCount=2, hardGoals=[goal], softGoals=[goal], sourceFingerprint="f" * 16)

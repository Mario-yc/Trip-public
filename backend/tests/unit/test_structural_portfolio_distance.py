from src.services.creative_planning_models import CreativeBrief, PlanCandidate, PlanScoreVector
from src.services.pareto_portfolio_selector import ParetoPortfolioSelector


def _candidate(identifier: str, *, axis: str, poi: str, family: str, role: str):
    return PlanCandidate(
        proposalId=f"proposal_{identifier}", portfolioId="portfolio",
        brief=CreativeBrief(briefId=f"brief_{identifier}", title=f"标题 {identifier}", primaryAxis=axis, dayRoles=[{"dayNumber": 1, "role": role}], requiredGoalIds=[]),
        itinerarySnapshot={"days": [{"dayNumber": 1, "segments": [{"poi": {"amapId": poi}, "semanticMetadata": {"experienceFamily": family}, "kind": "visit"}]}]},
        groundedEvidence=[{"amapId": poi, "family": family}],
        score=PlanScoreVector(hardConstraintPassed=True, preferenceFit=70, thematicCoherence=70, experienceDiversity=70, routeEfficiency=70, pacingQuality=70, novelty=70, robustness=70, uncertaintyPenalty=0, evidence={}),
        verifier={"passed": True}, canonicalSignature=(identifier * 16)[:16],
    )


def test_axis_and_title_only_variants_have_zero_structural_distance_and_do_not_expand_visible_portfolio():
    first = _candidate("a", axis="classic", poi="same", family="museum", role="文化")
    second = _candidate("b", axis="photo_night", poi="same", family="museum", role="文化")
    selector = ParetoPortfolioSelector()
    assert selector.structural_distance_without_axis(first, second) == 0
    assert len(selector.select([first, second])) == 1

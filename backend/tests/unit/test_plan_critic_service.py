from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_planning_models import CreativeBrief, PlanCandidate, PlanScoreVector
from src.services.plan_critic_service import PlanCriticService


def test_critic_is_read_only_and_reports_missing_required_goal():
    ledger = ConstraintLedgerCompiler().compile({"city": "北京", "requestIntentContract": {"requiredIntents": [{"goalId": "museum", "intentType": "museum"}]}})
    candidate = PlanCandidate(proposalId="proposal", portfolioId="portfolio", brief=CreativeBrief(briefId="brief", title="x", primaryAxis="classic", requiredGoalIds=[]), itinerarySnapshot={}, score=PlanScoreVector(hardConstraintPassed=True, preferenceFit=80, thematicCoherence=80, experienceDiversity=80, routeEfficiency=80, pacingQuality=20, novelty=80, robustness=80, uncertaintyPenalty=80), verifier={}, canonicalSignature="x" * 16)
    defects = PlanCriticService().review(candidate, ledger)
    assert {item.defect_type for item in defects} >= {"required_goal_omission", "daily_overload", "uncertainty_too_high"}

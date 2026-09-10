import pytest

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_planning_models import CreativeBrief
from src.services.plan_score_service import PlanScoreService


def _ledger():
    return ConstraintLedgerCompiler().compile({"city": "北京", "requestIntentContract": {"pace": "relaxed", "budget": 100, "requiredIntents": []}})


def _snapshot(*, route_minutes: int, family: str, duplicate: bool = False, unresolved: bool = False):
    family_two = family if duplicate else "museum"
    return {"days": [{"dayNumber": 1, "segments": [
        {"id": "a", "kind": "visit", "startTime": "09:00", "endTime": "10:00", "poi": {"amapId": "a", "longitude": 1, "latitude": 1, "providerType": "AMAP"}, "semanticMetadata": {"groundingStatus": "selected", "experienceFamily": family}, "estimatedCost": 20},
        {"id": "b", "kind": "visit", "startTime": "11:00", "endTime": "12:00", "poi": {"amapId": "b", "longitude": 1, "latitude": 1, "providerType": "AMAP"}, "semanticMetadata": {"groundingStatus": "unresolved" if unresolved else "selected", "experienceFamily": family_two}, "estimatedCost": 20},
    ], "routeEvidence": [{
        "id": "route_a_b",
        "fromSegmentId": "a",
        "toSegmentId": "b",
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "mode": "transit",
        "isSelected": True,
        "distanceMeters": 1200,
        "durationSeconds": route_minutes * 60,
        "durationMinutes": route_minutes,
        "polyline": [[116.3, 39.9], [116.31, 39.91]],
        "queriedAt": "2026-08-02T00:00:00+00:00",
        "status": "verified",
    }]}]}


def test_score_is_fact_driven_and_every_field_has_machine_evidence():
    service = PlanScoreService()
    brief = CreativeBrief(briefId="food", title="食", primaryAxis="food_led", optionalExperiences=[{"family": "local_food", "description": "本地菜"}], requiredGoalIds=[])
    efficient = service.score(snapshot=_snapshot(route_minutes=10, family="local_food"), ledger=_ledger(), brief=brief, verifier={"passed": True})
    slow_duplicate = service.score(snapshot=_snapshot(route_minutes=180, family="museum", duplicate=True, unresolved=True), ledger=_ledger(), brief=brief, verifier={"passed": True})
    assert efficient.route_efficiency > slow_duplicate.route_efficiency
    assert efficient.thematic_coherence > slow_duplicate.thematic_coherence
    assert efficient.experience_diversity > slow_duplicate.experience_diversity
    assert efficient.uncertainty_penalty < slow_duplicate.uncertainty_penalty
    assert efficient.evidence["routeEfficiency"] == ["selectedRouteCount=1", "travelMinutes=10"]
    assert set(efficient.evidence) >= {"preferenceFit", "thematicCoherence", "experienceDiversity", "routeEfficiency", "pacingQuality", "novelty", "robustness", "uncertaintyPenalty", "estimatedCostCny"}


def test_plan_score_consumes_consumer_evidence_strength_and_uncertainty():
    service = PlanScoreService()
    brief = CreativeBrief(
        briefId="local",
        title="本地",
        primaryAxis="local_immersion",
        optionalExperiences=[{"family": "local_life", "description": "本地生活"}],
        requiredGoalIds=[],
    )
    strong = _snapshot(route_minutes=10, family="local_life")
    weak = _snapshot(route_minutes=10, family="local_life")
    for snapshot, value in ((strong, 1.0), (weak, 0.1)):
        snapshot["days"][0]["segments"][0]["semanticMetadata"]["consumerAdmissionReport"] = {
            "scoreComponents": {
                "evidenceStrength": value,
                "sourceFreshness": value,
                "localDistinctiveness": value,
                "userIntentFit": value,
                "uncertaintyPenalty": 1.0 - value,
            }
        }
    strong_score = service.score(snapshot=strong, ledger=_ledger(), brief=brief, verifier={"passed": True})
    weak_score = service.score(snapshot=weak, ledger=_ledger(), brief=brief, verifier={"passed": True})
    assert strong_score.preference_fit > weak_score.preference_fit
    assert strong_score.robustness > weak_score.robustness
    assert strong_score.uncertainty_penalty < weak_score.uncertainty_penalty
    assert "consumerEvidenceStrength=1.0" in strong_score.evidence["preferenceFit"]

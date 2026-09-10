from __future__ import annotations

import copy

from backend.tests.unit.test_guide_grounded_continuation import _guide_requirement
from src.services.agent_service import AgentService
from src.services.simple_open_direction_service import SimpleOpenDirectionService


def _rejected_snapshot() -> dict:
    requirement = _guide_requirement(evidence_fingerprint="e" * 64)
    hint = requirement["placeHints"][0]
    days = []
    for number in (1, 2):
        slot = f"day{number}-park"
        attempt = {
            **copy.deepcopy(hint),
            "schemaVersion": "guide-place-attempt-v1",
            "dayNumber": number,
            "planningSlotId": slot,
            "status": "rejected",
            "reasonCode": "no_match_in_search_scope",
            "providerCalled": True,
            "providerOutcome": "success",
            "providerResultCount": 0,
            "queryText": hint["mentionText"],
            "searchScope": "nearby_low_detour",
            "nearbyRadiusMeters": 5000,
        }
        days.append(
            {
                "dayNumber": number,
                "segments": [
                    {
                        "semanticMetadata": {
                            "planningSlotId": slot,
                            "scheduleConstraints": {"guideEvidenceAttempt": attempt},
                        }
                    }
                ],
            }
        )
    return {"guideContinuationRequirement": requirement, "days": days}


def _usage(snapshot: dict) -> dict:
    return SimpleOpenDirectionService._guide_evidence_usage(
        snapshot,
        route_verified=False,
        hard_constraints_passed=True,
    )


def test_same_place_in_two_day_slots_is_two_attempts_not_two_places() -> None:
    usage = _usage(_rejected_snapshot())
    assert usage["rejectionCounts"] == {"no_match_in_search_scope": 2}
    assert usage["attemptedPlaceCount"] == 1
    assert usage["providerQueryCount"] == 2
    assert len(usage["attemptDetails"]) == 2
    assert usage["attemptDetails"][0]["nearbyRadiusMeters"] == 5000
    assert usage["status"] == "unsatisfied"
    assert usage["usedPlaces"] == []


def test_attempt_diagnostics_must_match_signed_hint_and_slot() -> None:
    snapshot = _rejected_snapshot()
    first = snapshot["days"][0]["segments"][0]["semanticMetadata"]["scheduleConstraints"]
    first["guideEvidenceAttempt"]["mentionText"] = "模型捏造公园"
    second = snapshot["days"][1]["segments"][0]["semanticMetadata"]["scheduleConstraints"]
    second["guideEvidenceAttempt"]["planningSlotId"] = "cross-slot"
    usage = _usage(snapshot)
    assert usage["attemptDetails"] == []
    assert usage["rejectionCounts"] == {"guide_evidence_lineage_invalid": 2}
    assert usage["providerQueryCount"] == 0


def test_cached_search_is_not_counted_as_another_provider_query() -> None:
    snapshot = _rejected_snapshot()
    metadata = snapshot["days"][1]["segments"][0]["semanticMetadata"]
    metadata["scheduleConstraints"]["guideEvidenceAttempt"]["cacheHit"] = True
    usage = _usage(snapshot)
    assert usage["providerQueryCount"] == 1
    assert usage["cacheHitCount"] == 1
    assert usage["attemptedPlaceCount"] == 1


def test_rejection_summary_names_queries_not_globally_missing_places() -> None:
    summary = AgentService._guide_rejection_summary(_usage(_rejected_snapshot()))
    assert "北海公园" in summary
    assert "2 次" in summary
    assert "2 处" not in summary
    assert "范围" in summary
    assert "不代表高德中不存在" in summary


def test_day_slot_timeout_fallback_does_not_claim_model_not_called() -> None:
    agent = object.__new__(AgentService)
    captured = []
    agent._append_pipeline_event = lambda *args, **kwargs: captured.append(args)
    agent._append_initial_day_slot_provider_event(
        [],
        "completed",
        {
            "deterministicInitialPlanUsed": True,
            "fallbackUsed": True,
            "modelProviderCalled": True,
            "providerReturnedMode": "initial_day_slot_provider_timeout",
        },
    )
    detail = captured[0][4]
    assert "未调用" not in detail
    assert "fallback" in detail or "回退" in detail

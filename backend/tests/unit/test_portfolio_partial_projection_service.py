from __future__ import annotations

from src.services.portfolio_partial_projection_service import (
    PortfolioPartialProjectionService,
)


def _snapshot(*, hard_meal: bool = False) -> dict:
    return {
        "id": "plan_partial_meal",
        "portfolioDayAnchorTargets": {"1": 3},
        "portfolioRouteVerificationRequired": True,
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "seg_campus",
                        "kind": "visit",
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "poi": {"amapId": "campus", "name": "清华大学"},
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "required": True,
                            "requirementLevel": "hard",
                        },
                    },
                    {
                        "id": "seg_meal",
                        "kind": "meal",
                        "startTime": "12:00",
                        "endTime": "13:00",
                        "poi": {"amapId": "meal_far", "name": "远途餐厅"},
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "required": hard_meal,
                            "requirementLevel": "hard" if hard_meal else "soft",
                            "softGoalId": None if hard_meal else "goal_meal",
                            "goalId": "goal_meal" if hard_meal else None,
                            "creativeBriefId": "brief_meal",
                            "poolId": "meal_pool_d1",
                            "planningSlotId": "meal_slot_d1",
                            "dayNumber": 1,
                            "timeWindow": "12:00-13:00",
                            "rawNeed": "当地特色午餐",
                            "intentType": "meal",
                        },
                    },
                    {
                        "id": "seg_night",
                        "kind": "night_view",
                        "startTime": "20:00",
                        "endTime": "21:00",
                        "poi": {"amapId": "night", "name": "景山公园"},
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "required": True,
                            "requirementLevel": "hard",
                        },
                    },
                ],
                "routeEvidence": [
                    {
                        "fromSegmentId": "seg_campus",
                        "toSegmentId": "seg_meal",
                        "qualityIssue": "meal_detour_high",
                    },
                    {
                        "fromSegmentId": "seg_meal",
                        "toSegmentId": "seg_night",
                        "qualityIssue": "meal_detour_high",
                    },
                ],
            }
        ],
        "portfolioRouteEvidence": [
            {
                "fromSegmentId": "seg_campus",
                "toSegmentId": "seg_meal",
                "qualityIssue": "meal_detour_high",
            },
            {
                "fromSegmentId": "seg_meal",
                "toSegmentId": "seg_night",
                "qualityIssue": "meal_detour_high",
            },
        ],
        "routeEvidence": [
            {
                "fromSegmentId": "seg_campus",
                "toSegmentId": "seg_meal",
                "qualityIssue": "meal_detour_high",
            }
        ],
        "portfolioRouteQuality": {
            "status": "failed",
            "routeQualityIssues": [
                {
                    "code": "meal_detour_high",
                    "fromSegmentId": "seg_campus",
                    "toSegmentId": "seg_meal",
                    "distanceKm": 17.7,
                    "durationMinutes": 79,
                }
            ],
        },
    }


def test_soft_meal_detour_becomes_metadata_only_pending_slot():
    result = PortfolioPartialProjectionService().sanitize(_snapshot())

    assert result.status == "sanitized"
    assert result.reason_code == "non_hard_meal_detour_projected_to_pending_slot"
    assert result.removed_segment_ids == ["seg_meal"]
    assert result.failure_codes == ["meal_detour_high"]
    assert result.pending_slots_added[0]["planningSlotId"] == "meal_slot_d1"
    assert result.pending_slots_added[0]["requirementLevel"] == "soft"
    assert result.pending_slots_added[0]["futureRouteAnchor"] is True
    assert result.pending_slots_added[0]["routeAnchorExpected"] is True
    assert result.pending_slots_added[0]["groundingStatus"] == "unresolved"
    assert "poi" not in result.pending_slots_added[0]
    segments = result.snapshot["days"][0]["segments"]
    assert [item["id"] for item in segments] == ["seg_campus", "seg_night"]
    assert result.snapshot["portfolioPendingSlots"][0]["reason"] == "meal_detour_high"
    assert result.snapshot["portfolioPartialProjection"]["fakePoiCount"] == 0
    assert result.snapshot["portfolioRouteQuality"]["routeQualityIssues"] == []
    assert result.snapshot["days"][0]["routeEvidence"] == []


def test_hard_meal_detour_cannot_be_sanitized():
    result = PortfolioPartialProjectionService().sanitize(_snapshot(hard_meal=True))

    assert result.status == "rejected"
    assert result.reason_code == "meal_detour_targets_hard_segment"
    assert result.snapshot == _snapshot(hard_meal=True)
    assert result.removed_segment_ids == []


def test_provider_route_matrix_issue_projects_only_its_explicit_soft_meal():
    snapshot = _snapshot()
    issue = snapshot["portfolioRouteQuality"]["routeQualityIssues"][0]
    issue["code"] = "provider_route_matrix_unacceptable"
    issue["mealSegmentId"] = "seg_meal"

    result = PortfolioPartialProjectionService().sanitize(snapshot)

    assert result.status == "sanitized"
    assert result.removed_segment_ids == ["seg_meal"]
    assert result.failure_codes == ["provider_route_matrix_unacceptable"]
    assert result.pending_slots_added[0]["reason"] == "provider_route_matrix_unacceptable"


def test_provider_route_matrix_issue_without_explicit_meal_identity_is_not_sanitized():
    snapshot = _snapshot()
    snapshot["portfolioRouteQuality"]["routeQualityIssues"][0][
        "code"
    ] = "provider_route_matrix_unacceptable"

    result = PortfolioPartialProjectionService().sanitize(snapshot)

    assert result.status == "unchanged"
    assert result.reason_code == "no_sanitizable_route_blocker"
    assert [
        segment["id"]
        for segment in result.snapshot["days"][0]["segments"]
    ] == ["seg_campus", "seg_meal", "seg_night"]


def test_soft_meal_projection_is_stable_for_3_runs():
    metrics = {
        "sanitizedCount": 0,
        "blockingMealPersistedCount": 0,
        "pendingMealSlotCreatedCount": 0,
        "fakePoiCount": 0,
    }
    for _index in range(3):
        result = PortfolioPartialProjectionService().sanitize(_snapshot())
        metrics["sanitizedCount"] += int(
            result.status == "sanitized"
            and result.reason_code == "non_hard_meal_detour_projected_to_pending_slot"
        )
        metrics["blockingMealPersistedCount"] += int(
            any(
                str(segment.get("id") or "") == "seg_meal"
                for day in result.snapshot.get("days") or []
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
            )
        )
        metrics["pendingMealSlotCreatedCount"] += int(
            bool(result.snapshot.get("portfolioPendingSlots"))
        )
        metrics["fakePoiCount"] += int(
            any(
                not str((segment.get("poi") or {}).get("amapId") or "")
                for day in result.snapshot.get("days") or []
                for segment in day.get("segments") or []
                if isinstance(segment, dict) and segment.get("kind") == "meal"
            )
        )
    assert metrics == {
        "sanitizedCount": 3,
        "blockingMealPersistedCount": 0,
        "pendingMealSlotCreatedCount": 3,
        "fakePoiCount": 0,
    }

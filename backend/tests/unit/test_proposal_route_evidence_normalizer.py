from __future__ import annotations

from datetime import datetime, timezone

from src.models.route_option import RouteOption
from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer


def _segment(index: int, day_number: int, *, brief_id: str = "brief_route") -> dict:
    return {
        "id": f"seg_{index}",
        "startTime": f"{8 + index:02d}:00",
        "endTime": f"{9 + index:02d}:00",
        "kind": "visit",
        "estimatedCost": None,
        "poi": {
            "id": f"poi_{index}",
            "amapId": f"B00000{index:05d}",
            "name": "清华大学" if index == 1 else f"北京历史街区 {index}",
            "source": "amap-place-search",
            "latitude": 39.9 + index / 1000,
            "longitude": 116.3 + index / 1000,
            "type": "风景名胜;风景名胜;历史遗址",
            "providerType": "风景名胜;风景名胜;历史遗址",
        },
        "semanticMetadata": {
            "creativeBriefId": brief_id,
            "routeAnchor": True,
            "groundingStatus": "selected",
            "required": index == 1,
            "portfolioOptional": index != 1,
            "optionalExperienceFamily": "heritage_walk" if index != 1 else "",
        },
    }


def _route(start: int, end: int) -> dict:
    return {
        "id": f"route_seg_{start}_seg_{end}",
        "planId": "plan_route",
        "fromSegmentId": f"seg_{start}",
        "toSegmentId": f"seg_{end}",
        "fromPoiId": f"incorrect-amap-id-{start}",
        "toPoiId": f"incorrect-amap-id-{end}",
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "mode": "transit",
        "transportMode": "transit",
        "label": "公交/地铁",
        "isSelected": True,
        "sortOrder": 1,
        "distanceMeters": 1800,
        "durationSeconds": 900,
        "durationMinutes": 15,
        "costAmount": 0,
        "costCurrency": "CNY",
        "costEstimate": 0,
        "crowdingRisk": "medium",
        "polyline": [[116.3, 39.9], [116.31, 39.91]],
        "steps": [{"instruction": "步行至地铁站"}],
        "providerPayload": {"status": "1"},
        "queriedAt": "2026-08-01T00:00:00+00:00",
        "status": "verified",
    }


def _snapshot(*, route_options=None, portfolio_evidence=None, anchor_counts=(4, 3)) -> dict:
    index = 1
    days = []
    for day_number, count in enumerate(anchor_counts, start=1):
        segments = []
        for _ in range(count):
            segments.append(_segment(index, day_number))
            index += 1
        days.append({"id": f"day_{day_number}", "dayNumber": day_number, "segments": segments})
    return {
        "id": "plan_route",
        "proposalId": "proposal_route",
        "title": "路线核验方案",
        "city": "北京",
        "budgetTier": "medium",
        "portfolioSelectionContext": {"focusBriefId": "brief_route"},
        "creativeBrief": {"briefId": "brief_route"},
        "days": days,
        "routeOptions": route_options or [],
        "portfolioRouteEvidence": portfolio_evidence or [],
        "portfolioPendingSlots": [],
    }


def _all_day_routes() -> list[dict]:
    return [_route(1, 2), _route(2, 3), _route(3, 4), _route(5, 6), _route(6, 7)]


def test_portfolio_route_evidence_only_round_trips_as_complete_route_option():
    snapshot = _snapshot(portfolio_evidence=_all_day_routes())

    normalized = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
    readiness = ProposalReadinessService.compute(snapshot, verifier={"passed": True})
    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_route",
        root_portfolio_id="portfolio_route",
        proposal_id="proposal_route",
        source_assistant_turn_id="assistant_route",
        choice_id="choice_route",
        active_version_id=None,
        expected_base_version_id="ver_base",
        is_partial=False,
        is_adopted=False,
    )

    assert len(normalized) == 5
    first = normalized[0]
    assert first["fromPoiId"] == "poi_1"
    assert first["toPoiId"] == "poi_2"
    assert first["fromAmapId"] == "B0000000001"
    assert first["id"] == "route_seg_1_seg_2"
    assert first["durationSeconds"] == 900
    assert first["provider"] == first["source"] == "amap-webservice"
    assert first["polyline"]
    assert readiness["routeStatus"] == "route_ready"
    assert readiness["routeVerifiedLegCount"] == 5
    assert projection["routeEvidence"] == normalized
    assert projection["routeEvidence"][0]["steps"] == [{"instruction": "步行至地铁站"}]


def test_legacy_summary_is_needs_refresh_not_verified_or_adoptable():
    summary = {
        "fromSegmentId": "seg_1",
        "toSegmentId": "seg_2",
        "fromPoiId": "B0000000001",
        "toPoiId": "B0000000002",
        "mode": "transit",
        "distanceMeters": 1800,
        "distanceKm": 1.8,
        "durationMinutes": 15,
        "source": "amap-webservice",
        "status": "verified",
    }
    snapshot = _snapshot(anchor_counts=(2,), portfolio_evidence=[summary])

    normalized = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
    readiness = ProposalReadinessService.compute(snapshot, verifier={"passed": True})

    assert normalized[0]["status"] == "needs_refresh"
    assert normalized[0]["normalizationStatus"] == "legacy_summary_needs_refresh"
    assert normalized[0]["queriedAt"] is None
    assert readiness["routeStatus"] == "route_pending"
    assert readiness["adoptionReady"] is False
    assert readiness["nextAction"] == "verify_routes_and_adopt"


def test_invalid_queried_at_is_needs_refresh_and_not_projected_as_verified():
    route = _route(1, 2)
    route["queriedAt"] = "not-a-time"
    snapshot = _snapshot(anchor_counts=(2,), portfolio_evidence=[route])

    normalized = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_invalid_time",
        root_portfolio_id="portfolio_invalid_time",
        proposal_id="proposal_invalid_time",
        source_assistant_turn_id="assistant_invalid_time",
        choice_id="choice_invalid_time",
        active_version_id=None,
        expected_base_version_id="ver_base",
        is_partial=False,
        is_adopted=False,
    )

    assert normalized[0]["status"] == "needs_refresh"
    assert normalized[0]["normalizationStatus"] == "legacy_summary_needs_refresh"
    assert normalized[0]["queriedAt"] is None
    assert ProposalRouteEvidenceNormalizer.is_verified(normalized[0]) is False
    assert projection["routeStatus"] == "route_pending"
    assert projection["routeEvidence"] == []


def test_route_option_provider_pending_status_is_not_serialized_as_verified():
    route = RouteOption(
        id="route_pending",
        plan_id="plan_route",
        from_poi_id="poi_1",
        to_poi_id="poi_2",
        from_segment_id="seg_1",
        to_segment_id="seg_2",
        provider="amap-webservice",
        mode="transit",
        distance_meters=1800,
        duration_seconds=900,
        polyline=[[116.3, 39.9], [116.31, 39.91]],
        provider_payload={"status": "pending"},
        queried_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    dto = ProposalRouteEvidenceNormalizer.route_option_dto(
        route,
        from_segment_id="seg_1",
        to_segment_id="seg_2",
        from_poi_id="poi_1",
        to_poi_id="poi_2",
    )

    assert dto["status"] == "pending"
    assert ProposalRouteEvidenceNormalizer.is_verified(dto) is False


def test_worker_error_keeps_complete_location_material_retryable_without_a_version_write():
    snapshot = _snapshot()
    snapshot["portfolioRouteQuality"] = {
        "providerState": "worker_error",
        "executionLedger": {
            "expectedLegCount": 5,
            "completedLegCount": 0,
            "verifiedLegCount": 0,
            "failedLegCount": 5,
            "workerFailureClass": "RuntimeError",
            "sanitizedWorkerFailureMessage": "route adapter unavailable",
        },
    }

    readiness = ProposalReadinessService.compute(snapshot, verifier={"passed": True})

    assert readiness["routeExpectedLegCount"] == 5
    assert readiness["routeVerifiedLegCount"] == 0
    assert readiness["routeStatus"] == "route_provider_failed"
    assert readiness["routeRetryable"] is True
    assert readiness["nextAction"] == "retry_route_verification"
    assert readiness["adoptionReady"] is False


def test_comparison_projection_preserves_failed_leg_count_without_drawing_it():
    failed = _route(1, 2)
    failed["status"] = "failed"
    failed["routeStatus"] = "failed"
    failed["error"] = {"code": "provider_timeout"}
    snapshot = _snapshot(anchor_counts=(2,), route_options=[failed])

    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_route",
        root_portfolio_id="portfolio_route",
        proposal_id="proposal_route",
        source_assistant_turn_id="assistant_route",
        choice_id="choice_route",
        active_version_id=None,
        expected_base_version_id="ver_base",
        is_partial=True,
        is_adopted=False,
    )

    assert projection["routeStatus"] == "route_provider_failed"
    assert projection["routeErrorLegCount"] == 1
    assert projection["routeEvidence"] == []


def test_partial_route_coverage_preserves_verified_legs_and_only_requires_missing_legs():
    snapshot = _snapshot(route_options=_all_day_routes()[:3])

    readiness = ProposalReadinessService.compute(snapshot, verifier={"passed": True})
    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_partial",
        root_portfolio_id="portfolio_partial",
        proposal_id="proposal_partial",
        source_assistant_turn_id="assistant_partial",
        choice_id="choice_partial",
        active_version_id=None,
        expected_base_version_id="ver_base",
        is_partial=True,
        is_adopted=False,
    )

    assert readiness["routeExpectedLegCount"] == 5
    assert readiness["routeVerifiedLegCount"] == 3
    assert readiness["routeStatus"] == "route_partial"
    assert readiness["routeRetryable"] is True
    assert readiness["nextAction"] == "verify_routes_and_adopt"
    assert projection["routeSummary"] == "路线部分核验"

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.schemas.agent import AgentInitialPlanOutput, AgentMessageRequest
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_runtime_service import AgentRuntimeLimits
from src.services.agent_service import AgentService
from src.services.amap_call_budget import current_amap_call_budget
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.agent_verifier_service import AgentVerifierReport, AgentVerifierService
from src.services.conversation_service import ConversationService
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.creative_portfolio_provider_service import InitialCreativePortfolio
from src.services.creative_exploration_frontier_service import (
    CreativeExplorationFrontierService,
)
from src.services.creative_planning_models import (
    PlanCandidate,
    PlanPortfolio,
    PlanScoreVector,
    canonical_fingerprint,
)
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.itinerary_service import ItineraryService
from src.services.map_poi_service import MapPoiService
from src.services.poi_discovery_service import PoiDiscoveryResult
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.portfolio_route_feasibility_service import PortfolioRouteFeasibilityResult
from src.models.poi import POI
from src.models.route_option import RouteOption, normalize_route_mode
from src.models.poi_intent import PersistableSegmentPlan
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import RouteService


@pytest.fixture
def db_connection():
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _candidate(amap_id: str, name: str, intent: str, **lineage) -> dict:
    is_campus = intent == "campus_visit"
    canonical_amap_id = (
        amap_id.upper()
        if amap_id.upper().startswith("B") and 9 <= len(amap_id) and amap_id.isalnum()
        else "B" + hashlib.sha256(amap_id.encode("utf-8")).hexdigest()[:16].upper()
    )
    return {
        "id": amap_id,
        "amapId": canonical_amap_id,
        "name": name,
        "city": "北京",
        "type": "高等院校" if is_campus else "美术馆",
        "providerType": "高等院校" if is_campus else "美术馆",
        "providerTypeCode": "141201" if is_campus else "140100",
        "tags": ["大学", "校园"] if is_campus else ["美术馆", "艺术展览"],
        "longitude": 116.30 if is_campus else 116.40,
        "latitude": 40.00 if is_campus else 39.90,
        "source": "amap-place-search",
        "semanticPassed": True,
        "candidateScore": 0.95,
        "confidence": 0.95,
        **lineage,
    }


def _poi(candidate: dict) -> POI:
    return POI(
        id=candidate["id"],
        amap_id=candidate["id"],
        name=candidate["name"],
        city=candidate["city"],
        type=candidate["type"],
        category=candidate["type"],
        longitude=candidate["longitude"],
        latitude=candidate["latitude"],
        source="amap-place-search",
    )


def _canonical_server_route_decision_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="choice_commit_integration_fixture",
        provenance={
            "fixture": "creative-portfolio-choice-commit",
            "transportMode": "transit",
        },
        detour_tolerance={
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        mobility_profile={
            "source": "choice_commit_fixture_mobility",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6,
            "waitTimeMultiplier": 1,
            "riskPenaltyMultiplier": 1,
        },
    )
    assert contract is not None
    return {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        **contract,
    }


def _recorded_amap_route_matrix(
    route_service,
    plan_id,
    pois,
    transport_mode=None,
    segments=None,
    route_pairs=None,
    **_kwargs,
):
    """Return fresh, complete Provider evidence for deterministic writer tests."""

    assert str(transport_mode or "").strip() not in {
        "",
        "unspecified",
    }, "recorded route fixture requires an explicit canonical transport mode"
    mode = normalize_route_mode(transport_mode)
    assert mode != "unspecified", "recorded route fixture requires an explicit canonical transport mode"
    endpoint = {
        "transit": "/v3/direction/transit/integrated",
        "walking": "/v3/direction/walking",
        "bicycling": "/v4/direction/bicycling",
        "driving": "/v3/direction/driving",
    }.get(mode, f"/v3/direction/{mode}")
    requested_pairs = set(route_pairs) if route_pairs is not None else None
    groups = route_service._route_groups(list(pois), list(segments or []))
    if requested_pairs is not None:
        groups = [
            group
            for group in groups
            if group[0] is not None and group[1] is not None and (group[0].id, group[1].id) in requested_pairs
        ]
    queried_at = datetime.now(timezone.utc)
    routes = []
    for from_segment, to_segment, from_poi, to_poi in groups:
        assert from_segment is not None and to_segment is not None
        route_id = (
            "route_recorded_"
            + hashlib.sha256(f"{plan_id}:{from_segment.id}:{to_segment.id}:{mode}".encode("utf-8")).hexdigest()[:20]
        )
        routes.append(
            RouteOption(
                id=route_id,
                plan_id=plan_id,
                from_segment_id=from_segment.id,
                to_segment_id=to_segment.id,
                from_poi_id=from_poi.id,
                to_poi_id=to_poi.id,
                provider="amap-webservice",
                mode=mode,
                is_selected=True,
                distance_meters=1800,
                duration_seconds=900,
                polyline=[
                    [float(from_poi.longitude), float(from_poi.latitude)],
                    [float(to_poi.longitude), float(to_poi.latitude)],
                ],
                steps=[
                    {
                        "instruction": "Walk to the recorded transit stop",
                        "mode": "walking",
                        "distance": 240,
                    },
                    {
                        "instruction": "Take the recorded transit service",
                        "mode": "transit",
                        "distance": 1560,
                    },
                ],
                provider_payload={
                    "fixture": "choice-commit-density-route-matrix-v1",
                    "status": "1",
                    "endpoint": endpoint,
                    "queriedAt": queried_at.isoformat(),
                    "walkingDistanceMeters": 240,
                    "transferCount": 1,
                    "waitSeconds": 120,
                    "riskPenaltyMinutes": 0.0,
                    "costComponentProvenance": {
                        "walkingDistance": "recorded_provider_payload",
                        "transferCount": "recorded_provider_payload",
                        "wait": "recorded_provider_payload",
                        "risk": "recorded_route_fixture",
                    },
                    "provenance": {
                        "provider": "amap-webservice",
                        "endpoint": endpoint,
                        "recording": "deterministic_test_fixture",
                    },
                },
                queried_at=queried_at,
            )
        )
    return routes


@pytest.mark.parametrize(
    ("transport_mode", "expected_mode"),
    [
        ("public_transit", "transit"),
        ("walking", "walking"),
        ("driving", "driving"),
    ],
)
def test_recorded_route_fixture_requires_explicit_mode_and_preserves_supported_modes(
    transport_mode,
    expected_mode,
):
    left_segment = SimpleNamespace(id="left", day_id="day-1", start_time="09:00")
    right_segment = SimpleNamespace(id="right", day_id="day-1", start_time="10:00")
    left_poi = SimpleNamespace(id="left-poi", longitude=116.3, latitude=39.9)
    right_poi = SimpleNamespace(id="right-poi", longitude=116.4, latitude=40.0)

    class RecordedRouteGroups:
        @staticmethod
        def _route_groups(_pois, _segments):
            return [(left_segment, right_segment, left_poi, right_poi)]

    routes = _recorded_amap_route_matrix(
        RecordedRouteGroups(),
        "plan-explicit-mode",
        [],
        transport_mode=transport_mode,
    )
    assert [route.mode for route in routes] == [expected_mode]

    for missing_mode in (None, "", "unspecified"):
        with pytest.raises(AssertionError, match="explicit canonical transport mode"):
            _recorded_amap_route_matrix(
                RecordedRouteGroups(),
                "plan-missing-mode",
                [],
                transport_mode=missing_mode,
            )


def _generated() -> InitialCreativePortfolio:
    axes = ["culture_deep_dive", "local_immersion", "food_led", "photo_night"]
    proposals = []
    for index, axis in enumerate(axes, start=1):
        proposals.append(
            {
                "brief": {
                    "briefId": f"brief_{index}",
                    "title": f"方案 {index}",
                    "primaryAxis": axis,
                    "narrativeArc": f"{axis} narrative",
                    "dayRoles": [
                        {"dayNumber": 1, "role": "高校", "targetRouteAnchors": 1, "densityEvidence": ["pace=standard"]},
                        {
                            "dayNumber": 2,
                            "role": "博物馆",
                            "targetRouteAnchors": 1,
                            "densityEvidence": ["pace=standard"],
                        },
                    ],
                    "requiredGoalIds": ["goal_campus", "goal_museum"],
                },
                "daySlots": [
                    {
                        "slotId": f"brief_{index}_campus",
                        "dayNumber": 1,
                        "timeWindow": "morning",
                        "durationMinutes": 120,
                        "kind": "visit",
                        "rawNeed": "985大学",
                        "routeAnchor": True,
                        "requiredGoalId": "goal_campus",
                    },
                    {
                        "slotId": f"brief_{index}_museum",
                        "dayNumber": 2,
                        "timeWindow": "morning",
                        "durationMinutes": 120,
                        "kind": "visit",
                        "rawNeed": "美术馆",
                        "routeAnchor": True,
                        "requiredGoalId": "goal_museum",
                    },
                ],
                "intentPools": [
                    {
                        "poolId": f"brief_{index}_campus_pool",
                        "briefId": f"brief_{index}",
                        "rawNeed": "985大学",
                        "city": "北京",
                        "intentType": "campus_visit",
                        "targetCount": 1,
                        "requirementLevel": "required",
                        "goalId": "goal_campus",
                        "assignToSlots": [f"brief_{index}_campus"],
                    },
                    {
                        "poolId": f"brief_{index}_museum_pool",
                        "briefId": f"brief_{index}",
                        "rawNeed": "美术馆",
                        "city": "北京",
                        "intentType": "museum",
                        "targetCount": 1,
                        "requirementLevel": "required",
                        "goalId": "goal_museum",
                        "assignToSlots": [f"brief_{index}_museum"],
                    },
                ],
            }
        )
    return InitialCreativePortfolio.model_validate(
        {"schemaVersion": "initial-creative-portfolio-v1", "proposals": proposals}
    )


def _partial_generated() -> InitialCreativePortfolio:
    return InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "brief_partial",
                        "title": "高校与街区",
                        "primaryAxis": "local_immersion",
                        "narrativeArc": "先展示已确认高校，再补充街区漫步。",
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "高校",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            },
                            {
                                "dayNumber": 2,
                                "role": "街区漫步",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["briefOptionalCount=1"],
                            },
                        ],
                        "requiredGoalIds": ["goal_campus"],
                        "optionalExperiences": [
                            {
                                "family": "neighborhood_walk",
                                "description": "北京街区漫步",
                            }
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "partial_campus",
                            "dayNumber": 1,
                            "timeWindow": "09:00-11:00",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "kind": "visit",
                            "rawNeed": "985大学",
                            "routeAnchor": True,
                            "requiredGoalId": "goal_campus",
                        },
                        {
                            "slotId": "partial_walk",
                            "dayNumber": 2,
                            "timeWindow": "14:00-16:00",
                            "startTime": "14:00",
                            "durationMinutes": 120,
                            "kind": "activity",
                            "rawNeed": "街区漫步",
                            "routeAnchor": True,
                            "optionalExperienceFamily": "neighborhood_walk",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "partial_campus_pool",
                            "briefId": "brief_partial",
                            "rawNeed": "985大学",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "goal_campus",
                            "assignToSlots": ["partial_campus"],
                        },
                        {
                            "poolId": "partial_walk_pool",
                            "briefId": "brief_partial",
                            "rawNeed": "街区漫步",
                            "city": "北京",
                            "intentType": "neighborhood_walk",
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "optionalExperienceFamily": "neighborhood_walk",
                            "assignToSlots": ["partial_walk"],
                        },
                    ],
                }
            ],
        }
    )


def _hard_gap_generated() -> InitialCreativePortfolio:
    return InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "brief_hard_gap",
                        "title": "高校与夜景",
                        "primaryAxis": "culture_deep_dive",
                        "narrativeArc": "先核验高校，再补齐夜景。",
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "高校",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            },
                            {
                                "dayNumber": 2,
                                "role": "夜景",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            },
                        ],
                        "requiredGoalIds": ["goal_campus", "goal_night"],
                    },
                    "daySlots": [
                        {
                            "slotId": "hard_gap_campus",
                            "dayNumber": 1,
                            "timeWindow": "09:00-11:00",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "kind": "visit",
                            "rawNeed": "北京高校",
                            "routeAnchor": True,
                            "requiredGoalId": "goal_campus",
                        },
                        {
                            "slotId": "hard_gap_night",
                            "dayNumber": 2,
                            "timeWindow": "19:00-20:30",
                            "startTime": "19:00",
                            "durationMinutes": 90,
                            "kind": "night_view",
                            "rawNeed": "北京夜景",
                            "routeAnchor": True,
                            "requiredGoalId": "goal_night",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "hard_gap_campus_pool",
                            "briefId": "brief_hard_gap",
                            "rawNeed": "北京高校",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "goal_campus",
                            "assignToSlots": ["hard_gap_campus"],
                        },
                        {
                            "poolId": "hard_gap_night_pool",
                            "briefId": "brief_hard_gap",
                            "rawNeed": "北京夜景",
                            "city": "北京",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "goal_night",
                            "assignToSlots": ["hard_gap_night"],
                        },
                    ],
                }
            ],
        }
    )


def _mixed_candidate(
    *,
    passed: bool,
    proposal_id: str,
    portfolio_id: str = "portfolio_mixed",
    include_required_anchors: bool = False,
) -> PlanCandidate:
    brief = _generated().proposals[0].brief
    snapshot = {"id": "plan_mixed", "days": []}
    if not passed:
        snapshot = {
            "id": "plan_mixed_failed",
            "days": [
                {
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "segments": [
                        {
                            "id": "meal_mixed_failed",
                            "kind": "meal",
                            "startTime": "12:00",
                            "endTime": "13:00",
                            "durationMinutes": 60,
                            "transportMode": "transit",
                            "estimatedCost": 0,
                            "poi": {
                                "id": "amap_meal_mixed",
                                "amapId": "amap_meal_mixed",
                                "name": "偏绕午餐",
                                "longitude": 116.42,
                                "latitude": 39.91,
                            },
                            "semanticMetadata": {
                                "creativeBriefId": brief.brief_id,
                                "poolId": "meal_pool_mixed",
                                "planningSlotId": "meal_slot_mixed",
                                "occurrenceId": "occ:goal_meal:day:1",
                                "sourceGoalId": "goal_meal",
                            },
                        }
                    ],
                }
            ],
            "portfolioRouteQuality": {
                "routeQualityIssues": [
                    {
                        "code": "meal_detour_high",
                        "failureCode": "meal_detour_high",
                        "dayNumber": 1,
                        "mealSegmentId": "meal_mixed_failed",
                        "mealPlanningSlotId": "meal_slot_mixed",
                        "mealPoolId": "meal_pool_mixed",
                        "mealBriefId": brief.brief_id,
                        "mealAmapId": "amap_meal_mixed",
                        "previousSegmentId": "campus_mixed",
                        "nextSegmentId": "museum_mixed",
                        "routePair": {
                            "fromSegmentId": "campus_mixed",
                            "toSegmentId": "meal_mixed_failed",
                        },
                        "distanceKm": 13.2,
                        "durationMinutes": 74,
                        "threshold": {
                            "maxDistanceMeters": 12_000,
                            "maxDurationMinutes": 70,
                        },
                    }
                ]
            },
        }
        if include_required_anchors:
            campus = _candidate("amap_campus_mixed", "清华大学", "campus_visit")
            museum = _candidate("amap_museum_mixed", "中国美术馆", "museum")
            snapshot["days"][0]["segments"].insert(
                0,
                {
                    "id": "campus_mixed",
                    "kind": "visit",
                    "startTime": "09:00",
                    "endTime": "11:00",
                    "durationMinutes": 120,
                    "transportMode": "transit",
                    "estimatedCost": 0,
                    "poi": {**campus, "amapId": campus["id"]},
                    "semanticMetadata": {
                        "creativeBriefId": brief.brief_id,
                        "poolId": f"{brief.brief_id}_campus_pool",
                        "planningSlotId": f"{brief.brief_id}_campus",
                        "sourceGoalId": "goal_campus",
                        "routeAnchor": True,
                        "required": True,
                        "groundingStatus": "selected",
                    },
                },
            )
            snapshot["days"].append(
                {
                    "dayNumber": 2,
                    "date": "2026-10-02",
                    "segments": [
                        {
                            "id": "museum_mixed",
                            "kind": "visit",
                            "startTime": "09:00",
                            "endTime": "11:00",
                            "durationMinutes": 120,
                            "transportMode": "transit",
                            "estimatedCost": 0,
                            "poi": {**museum, "amapId": museum["id"]},
                            "semanticMetadata": {
                                "creativeBriefId": brief.brief_id,
                                "poolId": f"{brief.brief_id}_museum_pool",
                                "planningSlotId": f"{brief.brief_id}_museum",
                                "sourceGoalId": "goal_museum",
                                "routeAnchor": True,
                                "required": True,
                                "groundingStatus": "selected",
                            },
                        }
                    ],
                }
            )
            snapshot["portfolioRequiredCandidateBindings"] = [
                {
                    "briefId": brief.brief_id,
                    "sourceGoalId": "goal_campus",
                    "planningSlotId": f"{brief.brief_id}_campus",
                    "amapId": campus["id"],
                },
                {
                    "briefId": brief.brief_id,
                    "sourceGoalId": "goal_museum",
                    "planningSlotId": f"{brief.brief_id}_museum",
                    "amapId": museum["id"],
                },
            ]
    return PlanCandidate(
        proposalId=proposal_id,
        portfolioId=portfolio_id,
        brief=brief,
        itinerarySnapshot=snapshot,
        score=PlanScoreVector(
            hardConstraintPassed=passed,
            preferenceFit=80,
            thematicCoherence=80,
            experienceDiversity=70,
            routeEfficiency=80,
            pacingQuality=80,
            novelty=60,
            robustness=80 if passed else 0,
            uncertaintyPenalty=0 if passed else 50,
            evidence={},
        ),
        verifier={
            "passed": passed,
            "hardFailures": [] if passed else ["portfolio_route_quality:meal_detour_high"],
            "routeQualityFailures": [] if passed else ["meal_detour_high"],
            "requiredCandidateBindingActualCount": 2 if include_required_anchors else 0,
            "hardCandidateLineageMissingCount": 0,
        },
        canonicalSignature=("a" if passed else "b") * 64,
    )


def _assert_no_unsealed_failed_component_handoff(payload: dict) -> None:
    """Synthetic route issues cannot authorize an exact component-repair retry."""

    assert payload["failedComponentCheckpoint"] is None
    assert payload["failedComponentRecoveryState"] is None
    assert payload.get("failedComponentRepair") is None
    assert not any(
        option.get("action") == "retry_failed_component"
        or option.get("handoff") == "retry_failed_component"
        for option in payload.get("choiceOptions") or []
        if isinstance(option, dict)
    )


def _assert_more_plans_continuation_has_no_exact_repair_identity(continuation: dict) -> None:
    """A generic direction continuation must not inherit repair-only authority."""

    checkpoint = continuation.get("continuationCheckpoint")
    assert isinstance(checkpoint, dict)
    repair_only_fields = {
        "failedComponentCheckpoint",
        "failedComponentRecoveryState",
        "failedComponentRepair",
        "repairScopeCertificate",
        "repairScopeCertificateFingerprint",
        "scopeFingerprint",
        "proofFingerprint",
        "certificateFingerprint",
        "routeRepairHandoff",
        "routeRepairScope",
        "repairAttemptLedger",
        "routeBadCheckpoints",
        "adjacentRouteLedgerKeys",
        "fromPhysicalId",
        "toPhysicalId",
        "mode",
        "routeContractFingerprint",
        "routeExecutionLedger",
        "fullProviderMatrixProofFingerprints",
        "fullProviderMatrixProofFingerprint",
        "completedMatrixEvidence",
        "candidateEvidence",
        "noProgressScopeFingerprints",
        "executedQueryFingerprints",
        "usedQueryFingerprints",
        "admittedCanonicalPhysicalIds",
        "rejectedCanonicalPhysicalIds",
        "issueCodes",
        "continuationMode",
        "terminalStatus",
        "terminalReason",
        "queryCursor",
        "attemptCursor",
        "candidateProbeKeys",
        "candidateProbeEvidence",
        "completedMatrixCandidateKeys",
        "routeBadCheckpointScopeFingerprint",
        "executionLedger",
        "candidateRecordId",
        "executionId",
        "candidatePhysicalId",
        "segmentId",
        "amapId",
        "targetSegmentId",
        "mealSegmentId",
        "mealAmapId",
        "failureCode",
        "routeQualityFailures",
        "issueCode",
    }

    def assert_no_nonempty_repair_fields(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in repair_only_fields:
                    assert nested in (None, "", [], {})
                assert_no_nonempty_repair_fields(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_nonempty_repair_fields(nested)

    assert_no_nonempty_repair_fields(continuation)
    for text_field in ("label", "detail", "reason", "failureReason"):
        text = str(continuation.get(text_field) or "").lower()
        assert "meal_detour_high" not in text
        assert "provider_route_matrix_unacceptable" not in text
        assert "route_quality" not in text
    assert checkpoint["checkpointFingerprint"] == canonical_fingerprint(
        {key: value for key, value in checkpoint.items() if key != "checkpointFingerprint"}
    )
    assert checkpoint.get("poolId") in (None, "")
    assert checkpoint.get("planningSlotId") in (None, "")
    assert checkpoint.get("dayNumber") in (None, 0)


def test_visible_proposal_suppresses_failed_partial_writer(db_connection, monkeypatch):
    session = ConversationService(db_connection).create_session("北京", "mixed arbitration")
    service = AgentService(db_connection)
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    visible_candidate = _mixed_candidate(passed=True, proposal_id="proposal_visible_mixed")
    partial_candidate = _mixed_candidate(passed=False, proposal_id="proposal_partial_failed")

    def mixed_stage(self, **kwargs):
        portfolio = PlanPortfolio(
            portfolioId="portfolio_mixed",
            sessionId=kwargs["session_id"],
            sourceUserTurnId=kwargs["source_user_turn_id"],
            sourceAssistantTurnId=kwargs["source_assistant_turn_id"],
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=kwargs["request_fingerprint"],
            status="awaiting_selection",
            proposalIds=[visible_candidate.proposal_id],
            visibleProposalIds=[visible_candidate.proposal_id],
        )
        self.partial_timeline_candidate = partial_candidate
        self.store.create(portfolio, [visible_candidate])
        return portfolio, [visible_candidate]

    monkeypatch.setattr(CreativePortfolioStagingService, "stage", mixed_stage)

    def forbidden_partial_write(**_kwargs):
        raise AssertionError("visible proposal must suppress the partial writer")

    monkeypatch.setattr(service, "_persist_portfolio_partial_timeline", forbidden_partial_write)
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "requiredIntents": [],
        },
        "agentDecisionState": {"observationFingerprint": "o" * 64},
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=service._initial_plan_from_creative_portfolio(_generated()),
        segment_plans=[],
        grounding_report={"poolReports": []},
        ledger=ledger,
        generated=_generated(),
    )

    assert response.terminal_status == "needs_confirmation"
    assert response.version is None
    assert response.itinerary is None
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (assistant_turn_id,),
        ).fetchone()[0]
    )
    assert payload["visibleProposalCount"] == 1
    assert payload["partialCandidateSuppressed"] is True
    assert payload["partialSuppressionReason"] == "visible_proposal_precedence"
    assert payload["partialWriterCallCount"] == 0
    assert payload["versionDelta"] == 0
    assert payload["patchDelta"] == 0
    assert payload["routeWriteDelta"] == 0
    _assert_no_unsealed_failed_component_handoff(payload)


@pytest.mark.parametrize(
    "frontier_mode", ["exact", "discover_next", "temporarily_degraded", "route_degraded", "exhausted"]
)
def test_soft_meal_projection_without_required_anchor_stays_unsealed_and_zero_write(
    db_connection,
    monkeypatch,
    frontier_mode,
):
    frontier_exhausted = frontier_mode == "exhausted"
    discover_next = frontier_mode in {"discover_next", "temporarily_degraded", "route_degraded"}
    session = ConversationService(db_connection).create_session("北京", "meal hard blocker")
    service = AgentService(db_connection)
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    failed_candidate = _mixed_candidate(
        passed=False,
        proposal_id="proposal_meal_hard_block",
    )

    def failed_stage(self, **kwargs):
        portfolio = PlanPortfolio(
            portfolioId="portfolio_meal_hard_block",
            sessionId=kwargs["session_id"],
            sourceUserTurnId=kwargs["source_user_turn_id"],
            sourceAssistantTurnId=kwargs["source_assistant_turn_id"],
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=kwargs["request_fingerprint"],
            status="failed",
            failureReason="portfolio_route_quality_unresolved:meal_detour_high",
        )
        self.store.create(portfolio, [])
        self.partial_timeline_candidate = failed_candidate
        return portfolio, []

    monkeypatch.setattr(CreativePortfolioStagingService, "stage", failed_stage)
    if frontier_exhausted:
        original_advance = CreativeExplorationFrontierService.advance

        def exhausted_advance(self, frontier, **kwargs):
            advanced = original_advance(self, frontier, **kwargs)
            advanced.update(
                {
                    "frontierState": "semantic_exhausted",
                    "exhaustionReason": "directions_exhausted",
                }
            )
            return advanced

        monkeypatch.setattr(
            CreativeExplorationFrontierService,
            "advance",
            exhausted_advance,
        )
    elif discover_next:
        original_update_frontier = PlanPortfolioStore.update_exploration_frontier

        def empty_cursor_update_frontier(self, **kwargs):
            row = self.db.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (kwargs["portfolio_id"],),
            ).fetchone()
            summary = json.loads(row["summary_json"])
            summary["nextBriefId"] = ""
            self.db.execute(
                "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
                (json.dumps(summary), kwargs["portfolio_id"]),
            )
            self.db.commit()
            frontier = copy.deepcopy(kwargs["frontier"])
            frontier["currentFocusBriefId"] = "brief_3"
            if (
                frontier_mode in {"temporarily_degraded", "route_degraded"}
                and frontier.get("frontierState") != "no_progress"
            ):
                frontier["frontierState"] = frontier_mode
            return original_update_frontier(self, **{**kwargs, "frontier": frontier})

        monkeypatch.setattr(
            PlanPortfolioStore,
            "update_exploration_frontier",
            empty_cursor_update_frontier,
        )

    def forbidden_partial_write(**_kwargs):
        raise AssertionError("meal_detour_high must never enter the partial writer")

    monkeypatch.setattr(service, "_persist_portfolio_partial_timeline", forbidden_partial_write)
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "requiredIntents": [],
        },
        "agentDecisionState": {"observationFingerprint": "o" * 64},
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=service._initial_plan_from_creative_portfolio(_generated()),
        segment_plans=[],
        grounding_report={"poolReports": []},
        ledger=ledger,
        generated=_generated(),
    )

    assert response.terminal_status == ("failed" if frontier_exhausted else "needs_confirmation")
    assert response.version is None
    assert response.itinerary is None
    payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (assistant_turn_id,),
        ).fetchone()[0]
    )
    assert payload["visibleProposalCount"] == 0
    assert payload["partialWriterCallCount"] == 0
    assert payload["versionDelta"] == 0
    assert payload["patchDelta"] == 0
    assert payload["routeWriteDelta"] == 0
    assert payload["partialProjection"]["status"] == "sanitized"
    assert payload["partialProjection"]["reasonCode"] == ("non_hard_meal_detour_projected_to_pending_slot")
    assert payload["partialProjection"]["postProjectionEligible"] is False
    _assert_no_unsealed_failed_component_handoff(payload)
    # An empty persisted nextBriefId is the discover-next cursor, not terminal
    # no-progress: a fresh direction may still be generated on the next
    # bounded Provider run.  Only a semantic-exhausted frontier suppresses it.
    assert len(payload["choiceOptions"]) == (0 if frontier_exhausted else 1)
    if not frontier_exhausted:
        continuation = payload["choiceOptions"][0]
        assert continuation["action"] == "retry_model_planning"
        assert continuation["kind"] == "portfolio_more_plans"
        assert continuation["planningSelectionRootTurnId"] == user_turn_id
        assert continuation["rootPortfolioId"] == "portfolio_meal_hard_block"
        assert continuation["focusBriefId"] == ("brief_3" if discover_next else "brief_4")
        assert continuation["expansionFocusMode"] == ("discover_next" if discover_next else "exact")
        assert continuation["requestContractFingerprint"] == ledger.source_fingerprint
        assert continuation["retryCurrentStageEligible"] is True
        _assert_more_plans_continuation_has_no_exact_repair_identity(continuation)
        checkpoint = continuation["continuationCheckpoint"]
        assert checkpoint["sessionId"] == session.session_id
        assert checkpoint["sourceAssistantTurnId"] == assistant_turn_id
        assert checkpoint["choiceId"] == continuation["id"]
        assert checkpoint["planningSelectionRootTurnId"] == user_turn_id
        assert checkpoint["rootPortfolioId"] == "portfolio_meal_hard_block"
        assert checkpoint["requestContractFingerprint"] == ledger.source_fingerprint
        assert checkpoint["focusBriefId"] == continuation["focusBriefId"]
        assert checkpoint["checkpointFingerprint"] == continuation["checkpointFingerprint"]
        auto_advance_events = [
            item for item in payload["planningSteps"] if item.get("type") == "creative_frontier_auto_advanced"
        ]
        assert [item["metadata"]["resultPreview"]["remainingBudget"] for item in auto_advance_events] == [11, 10]
        assert all(
            item["metadata"]["resultPreview"]["versionDelta"] == 0
            and item["metadata"]["resultPreview"]["patchDelta"] == 0
            and item["metadata"]["resultPreview"]["routeWriteDelta"] == 0
            for item in auto_advance_events
        )
    root = db_connection.execute(
        "SELECT status, summary_json FROM agent_plan_portfolios WHERE id = ?",
        ("portfolio_meal_hard_block",),
    ).fetchone()
    summary = json.loads(root["summary_json"])
    assert root["status"] == ("failed" if frontier_exhausted else "awaiting_selection")
    if not frontier_exhausted:
        assert summary["generationState"] == "route_degraded"
    expected_frontier_state = (
        "semantic_exhausted"
        if frontier_exhausted
        else frontier_mode
        if frontier_mode in {"temporarily_degraded", "route_degraded"}
        else "has_more"
    )
    assert summary["creativeExplorationFrontier"]["frontierState"] == expected_frontier_state
    # discover-next deliberately has no preselected brief cursor; the offered
    # option carries the current root focus and resolves a fresh direction only
    # when its next bounded execution begins.
    expected_next_brief_id = "" if frontier_exhausted or discover_next else "brief_4"
    assert summary["nextBriefId"] == expected_next_brief_id
    assert summary["creativeExplorationFrontier"]["nextBriefId"] == expected_next_brief_id
    if discover_next:
        assert summary["creativeExplorationFrontier"]["currentFocusBriefId"] == "brief_3"
    assert "部分时间轴已生成" not in payload["reply"]
    if frontier_exhausted:
        assert "当前约束下可验证方向已生成完毕" in payload["reply"]
        assert "directions_exhausted" not in payload["reply"]
        assert payload["planningDiagnostic"]["category"] == "route_quality_exhausted"
    else:
        assert "继续下一个方案方向" in payload["reply"]
        assert "重试路线核验" not in payload["reply"]
        assert payload["planningDiagnostic"]["category"] == "route_degraded"
        assert not any(item.get("type") == "creative_frontier_no_progress" for item in payload["planningSteps"])
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM amap_poi_candidates WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )


def test_mixed_route_route_density_failures_keep_fourth_direction_continuable(
    db_connection,
    monkeypatch,
):
    """The root outcome is derived from every attempted brief, not the last failure."""

    session = ConversationService(db_connection).create_session("北京", "mixed degraded frontier")
    service = AgentService(db_connection)
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    stage_calls: list[str] = []
    route_budgets: list[object] = []
    route_budget_snapshots: list[tuple[int, int]] = []
    route_lease_counts: list[int] = []
    unauthorized_route_attempts: list[bool] = []

    def mixed_failed_stage(self, **kwargs):
        brief_id = str(kwargs["generated"].proposals[0].brief.brief_id)
        stage_calls.append(brief_id)
        route_budget = current_amap_call_budget()
        assert route_budget is not None
        # Keep each budget alive until the assertion; otherwise CPython may
        # legitimately reuse a released object's id and make this test flaky.
        route_budgets.append(route_budget)
        route_budget_snapshots.append((route_budget.route_refresh_max, route_budget.used_route))
        route_lease_counts.append(
            int(route_budget.snapshot()["derivation"]["routeWorkLeaseCount"])
        )
        for index in range(9):
            unauthorized_route_attempts.append(
                route_budget.try_acquire(
                    endpoint="route/transit",
                    keyword=f"{brief_id}:{index}",
                    source="mixed-frontier-regression",
                )
            )

        if brief_id in {"brief_1", "brief_2"}:
            failure_reason = f"portfolio_route_quality_unresolved:{brief_id}:meal_detour_high"
            diagnostic = {
                "briefId": brief_id,
                "hardFailures": [],
                "routeCoverageFailures": [f"route_coverage_missing:{brief_id}"],
                "routeQualityFailures": ["meal_detour_high"],
                "dayAnchorShortfalls": [],
                "noveltyFailures": [],
            }
        else:
            failure_reason = f"portfolio_anchor_target_shortfall:{brief_id}:day_1:3/4"
            diagnostic = {
                "briefId": brief_id,
                "hardFailures": [],
                "routeCoverageFailures": [],
                "routeQualityFailures": [],
                "dayAnchorShortfalls": ["day_1:3/4"],
                "noveltyFailures": [],
            }
        self.last_staging_metrics = {
            "briefMetrics": [
                {
                    "briefId": brief_id,
                    "routePreflightCallCount": route_budget.used_route,
                    "routeProviderCallCount": route_budget.used_route,
                    "amapRouteCount": route_budget.used_route,
                    "reasonCodes": [
                        *diagnostic["routeQualityFailures"],
                        *diagnostic["dayAnchorShortfalls"],
                    ],
                }
            ],
            "perBriefDiagnostics": [diagnostic],
            "perBriefDurationMs": [1.0],
            "routePreflightMs": 1.0,
        }
        portfolio = PlanPortfolio(
            portfolioId="portfolio_mixed_degraded",
            sessionId=kwargs["session_id"],
            sourceUserTurnId=kwargs["source_user_turn_id"],
            sourceAssistantTurnId=kwargs["source_assistant_turn_id"],
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=kwargs["request_fingerprint"],
            status="failed",
            failureReason=failure_reason,
        )
        if len(stage_calls) == 1:
            self.store.create(portfolio, [])
        return portfolio, []

    monkeypatch.setattr(CreativePortfolioStagingService, "stage", mixed_failed_stage)
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "requiredIntents": [],
        },
        "agentDecisionState": {"observationFingerprint": "o" * 64},
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=service._initial_plan_from_creative_portfolio(_generated()),
        segment_plans=[],
        grounding_report={"poolReports": []},
        ledger=ledger,
        generated=_generated(),
    )

    assert stage_calls == ["brief_1", "brief_2", "brief_3"]
    assert len({id(item) for item in route_budgets}) == 3
    assert route_budget_snapshots == [
        (0, 0),
        (0, 0),
        (0, 0),
    ]
    assert route_lease_counts == [0, 0, 0]
    assert unauthorized_route_attempts == [False] * 27
    assert response.terminal_status == "needs_confirmation"
    assert response.version is None
    assert response.itinerary is None
    payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (assistant_turn_id,),
        ).fetchone()[0]
    )
    assert payload["versionDelta"] == 0
    assert payload["patchDelta"] == 0
    assert payload["routeWriteDelta"] == 0
    performance = next(
        item for item in payload["planningSteps"] if item.get("type") == "portfolio_staging_performance"
    )["metadata"]["resultPreview"]
    assert performance["amapRouteCount"] == 0
    assert performance["routePreflightCallCount"] == 0
    assert performance["routeProviderCallCount"] == 0
    assert performance["providerCacheHitCount"] == 0
    assert [
        int((item.get("used") or {}).get("usedRoute") or 0) for item in performance["routePreflightBudgetsByBrief"]
    ] == [0, 0, 0]
    assert len(payload["choiceOptions"]) == 1
    choice = payload["choiceOptions"][0]
    assert choice["kind"] == "portfolio_more_plans"
    assert choice["focusBriefId"] == "brief_4"
    assert choice["planningSelectionRootTurnId"] == user_turn_id
    assert choice["rootPortfolioId"] == "portfolio_mixed_degraded"
    assert choice["requestContractFingerprint"] == ledger.source_fingerprint
    _assert_more_plans_continuation_has_no_exact_repair_identity(choice)
    checkpoint = choice["continuationCheckpoint"]
    assert checkpoint["schemaVersion"] == "creative-portfolio-continuation-checkpoint-v1"
    assert checkpoint["sessionId"] == session.session_id
    assert checkpoint["sourceAssistantTurnId"] == assistant_turn_id
    assert checkpoint["choiceId"] == choice["id"]
    assert checkpoint["planningSelectionRootTurnId"] == user_turn_id
    assert checkpoint["rootPortfolioId"] == "portfolio_mixed_degraded"
    assert checkpoint["requestContractFingerprint"] == ledger.source_fingerprint
    assert checkpoint["focusBriefId"] == "brief_4"
    assert checkpoint["checkpointState"] == "offered"
    assert choice["checkpointFingerprint"] == checkpoint["checkpointFingerprint"]
    root = db_connection.execute(
        "SELECT status, summary_json FROM agent_plan_portfolios WHERE id = ?",
        ("portfolio_mixed_degraded",),
    ).fetchone()
    summary = json.loads(root["summary_json"])
    assert root["status"] == "awaiting_selection"
    assert summary["generationState"] == "degraded_has_more"
    assert summary["creativeExplorationFrontier"]["frontierState"] == "has_more"
    assert summary["creativeExplorationFrontier"]["planningSelectionRootTurnId"] == user_turn_id
    assert summary["creativeExplorationFrontier"]["rootPortfolioId"] == ("portfolio_mixed_degraded")
    assert summary["creativeExplorationFrontier"]["requestContractFingerprint"] == ledger.source_fingerprint
    assert summary["creativeExplorationFrontier"]["nextBriefId"] == "brief_4"
    assert summary["nextBriefId"] == "brief_4"
    assert [item["status"] for item in summary["briefGenerationState"]] == [
        "failed",
        "failed",
        "failed",
        "remaining",
    ]
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )


def test_soft_meal_detour_keeps_unready_partial_zero_write(db_connection, monkeypatch):
    session = ConversationService(db_connection).create_session("北京", "grounded partial")
    service = AgentService(db_connection)
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    failed_candidate = _mixed_candidate(
        passed=False,
        proposal_id="proposal_grounded_partial",
        include_required_anchors=True,
    )
    failed_snapshot = copy.deepcopy(failed_candidate.itinerary_snapshot)
    failed_snapshot["portfolioGoalOccurrencePlan"] = {
        "occurrences": [
            {
                "occurrenceId": "occ:goal_meal:day:1",
                "sourceGoalId": "goal_meal",
                "dayNumber": 1,
                "requirementLevel": "explicit_soft",
            }
        ],
    }
    failed_candidate = failed_candidate.model_copy(update={"itinerary_snapshot": failed_snapshot})
    generated_payload = _generated().model_dump(by_alias=True)
    focus_skeleton = generated_payload["proposals"][0]
    focus_skeleton["daySlots"].append(
        {
            "slotId": "meal_slot_mixed",
            "dayNumber": 1,
            "timeWindow": "12:00-13:00",
            "startTime": "12:00",
            "durationMinutes": 60,
            "kind": "meal",
            "rawNeed": "北京当地特色午餐",
            "routeAnchor": True,
            "softGoalId": "goal_meal",
        }
    )
    focus_skeleton["intentPools"].append(
        {
            "poolId": "meal_pool_mixed",
            "briefId": failed_candidate.brief.brief_id,
            "rawNeed": "北京当地特色午餐",
            "city": "北京",
            "intentType": "meal",
            "targetCount": 1,
            "requirementLevel": "optional",
            "softGoalId": "goal_meal",
            "assignToSlots": ["meal_slot_mixed"],
        }
    )
    generated_with_meal = InitialCreativePortfolio.model_validate(generated_payload)

    def failed_stage(self, **kwargs):
        portfolio = PlanPortfolio(
            portfolioId="portfolio_grounded_partial",
            sessionId=kwargs["session_id"],
            sourceUserTurnId=kwargs["source_user_turn_id"],
            sourceAssistantTurnId=kwargs["source_assistant_turn_id"],
            expectedBaseVersionId=None,
            sourceObservationFingerprint=kwargs["observation_fingerprint"],
            requestContractFingerprint=kwargs["request_fingerprint"],
            status="failed",
            failureReason="portfolio_route_quality_unresolved:meal_detour_high",
        )
        self.store.create(portfolio, [])
        self.partial_timeline_candidate = failed_candidate
        return portfolio, []

    monkeypatch.setattr(CreativePortfolioStagingService, "stage", failed_stage)
    projected_verifier = {
        **failed_candidate.verifier,
        "passed": False,
        "draftPassed": True,
        "strictFailures": [
            "goal_occurrence_missing:occ:goal_meal:day:1:day_1",
        ],
        "hardFailures": [],
        "pendingHardSlotCount": 0,
        "pendingSoftSlotCount": 1,
        "routeQualityFailures": [],
        "requiredCandidateBindingActualCount": 2,
        "hardCandidateLineageMissingCount": 0,
    }
    monkeypatch.setattr(
        "src.services.plan_proposal_verifier.PlanProposalVerifier.verify",
        lambda self, snapshot, ledger, brief: copy.deepcopy(projected_verifier),
    )
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "requiredIntents": [
                {"goalId": "goal_campus", "intentType": "campus_visit", "requiredMin": 1},
                {"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1},
            ],
        },
        "agentDecisionState": {"observationFingerprint": "o" * 64},
        "goalOccurrencePlan": {
            "occurrences": [
                {
                    "occurrenceId": "occ:goal_meal:day:1",
                    "sourceGoalId": "goal_meal",
                    "dayNumber": 1,
                    "requirementLevel": "explicit_soft",
                }
            ],
        },
    }
    grounding_report = {
        "poolReports": [
            {
                "briefId": failed_candidate.brief.brief_id,
                "poolId": f"{failed_candidate.brief.brief_id}_campus_pool",
                "safeCandidates": [
                    _candidate(
                        "amap_campus_mixed",
                        "清华大学",
                        "campus_visit",
                        briefId=failed_candidate.brief.brief_id,
                        poolId=f"{failed_candidate.brief.brief_id}_campus_pool",
                        planningSlotId=f"{failed_candidate.brief.brief_id}_campus",
                        dayNumber=1,
                        sourceGoalId="goal_campus",
                    )
                ],
            },
            {
                "briefId": failed_candidate.brief.brief_id,
                "poolId": f"{failed_candidate.brief.brief_id}_museum_pool",
                "safeCandidates": [
                    _candidate(
                        "amap_museum_mixed",
                        "中国美术馆",
                        "museum",
                        briefId=failed_candidate.brief.brief_id,
                        poolId=f"{failed_candidate.brief.brief_id}_museum_pool",
                        planningSlotId=f"{failed_candidate.brief.brief_id}_museum",
                        dayNumber=2,
                        sourceGoalId="goal_museum",
                    )
                ],
            },
        ],
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=service._initial_plan_from_creative_portfolio(generated_with_meal),
        segment_plans=[],
        grounding_report=grounding_report,
        ledger=ledger,
        generated=generated_with_meal,
    )

    active = service._session(session.session_id)
    stored_payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (assistant_turn_id,),
        ).fetchone()[0]
    )
    assert stored_payload["partialProjection"]["status"] == "sanitized"
    assert response.terminal_status == "needs_confirmation"
    assert response.version is None
    assert active["active_version_id"] is None
    assert response.itinerary is None
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    assert not any(
        option.get("action") in {"select_plan_proposal", "adopt_active_partial"}
        for option in stored_payload["choiceOptions"]
    )


def test_mixed_arbitration_and_retry_checkpoint_are_stable_for_3_runs(db_connection, monkeypatch):
    class FailIfControllerCalled:
        def __getattr__(self, name):
            raise AssertionError(f"retry must not call provider: {name}")

    candidates_by_session: dict[str, tuple[PlanCandidate, PlanCandidate, str]] = {}

    def mixed_stage(self, **kwargs):
        session_id = kwargs["session_id"]
        visible_candidate, partial_candidate, portfolio_id = candidates_by_session[session_id]
        portfolio = PlanPortfolio(
            portfolioId=portfolio_id,
            sessionId=session_id,
            sourceUserTurnId=kwargs["source_user_turn_id"],
            sourceAssistantTurnId=kwargs["source_assistant_turn_id"],
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=kwargs["request_fingerprint"],
            status="awaiting_selection",
            proposalIds=[visible_candidate.proposal_id],
            visibleProposalIds=[visible_candidate.proposal_id],
        )
        self.partial_timeline_candidate = partial_candidate
        self.store.create(portfolio, [visible_candidate])
        return portfolio, [visible_candidate]

    monkeypatch.setattr(CreativePortfolioStagingService, "stage", mixed_stage)

    def forbidden_partial_write(self, **_kwargs):
        raise AssertionError("visible proposal must suppress partial writer")

    monkeypatch.setattr(AgentService, "_persist_portfolio_partial_timeline", forbidden_partial_write)
    metrics = {
        "initialVisibleProposalCount": 0,
        "falseFailureWithVisibleProposalCount": 0,
        "mixedPartialPreemptionCount": 0,
        "unexpectedPartialWriterCount": 0,
        "initialUnexpectedVersionCount": 0,
        "initialUnexpectedPatchCount": 0,
        "retryCheckpointReuseCount": 0,
        "retryControllerCallCount": 0,
        "retryStagedPipelineCallCount": 0,
        "retryExternalCallCount": 0,
        "retryPortfolioDriftCount": 0,
        "duplicatePortfolioCount": 0,
    }
    stability_runs = 3
    for index in range(stability_runs):
        session = ConversationService(db_connection).create_session("北京", f"mixed stability {index}")
        service = AgentService(db_connection, provider=FailIfControllerCalled())
        user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
        assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
        portfolio_id = f"portfolio_mixed_stability_{index}"
        visible = _mixed_candidate(
            passed=True,
            proposal_id=f"proposal_visible_stability_{index}",
            portfolio_id=portfolio_id,
        )
        partial = _mixed_candidate(
            passed=False,
            proposal_id=f"proposal_partial_stability_{index}",
            portfolio_id=portfolio_id,
        )
        candidates_by_session[session.session_id] = (
            visible,
            partial,
            portfolio_id,
        )
        request_context = {
            "selectedCity": "北京",
            "resolvedTripDates": {
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "requestIntentContract": {
                "city": "北京",
                "dayCount": 2,
                "requiredIntents": [],
            },
            "agentDecisionState": {"observationFingerprint": "o" * 64},
        }
        ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})
        initial = service._stage_creative_portfolio_response(
            session_id=session.session_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            content="北京两日游",
            request_context=request_context,
            session_before=service._session(session.session_id),
            tool_events=[],
            initial_plan=service._initial_plan_from_creative_portfolio(_generated()),
            segment_plans=[],
            grounding_report={"poolReports": []},
            ledger=ledger,
            generated=_generated(),
        )
        initial_payload = json.loads(
            db_connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (assistant_turn_id,),
            ).fetchone()[0]
        )
        metrics["initialVisibleProposalCount"] += int(initial_payload["visibleProposalCount"] >= 1)
        metrics["falseFailureWithVisibleProposalCount"] += int(initial.terminal_status != "needs_confirmation")
        metrics["mixedPartialPreemptionCount"] += int(not initial_payload["partialCandidateSuppressed"])
        metrics["unexpectedPartialWriterCount"] += int(initial_payload["partialWriterCallCount"] != 0)
        metrics["initialUnexpectedVersionCount"] += int(initial_payload["versionDelta"] != 0)
        metrics["initialUnexpectedPatchCount"] += int(initial_payload["patchDelta"] != 0)

        retry = service.send_message(session.session_id, AgentMessageRequest(content="重试一遍"))
        retry_payload = json.loads(
            db_connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (retry.assistant_turn.id,),
            ).fetchone()[0]
        )
        metrics["retryCheckpointReuseCount"] += int(retry_payload["retryExecutionPlan"]["portfolioId"] == portfolio_id)
        metrics["retryControllerCallCount"] += int(retry_payload["controllerFullCallCount"] or 0) + int(
            retry_payload["controllerLiteCallCount"] or 0
        )
        metrics["retryStagedPipelineCallCount"] += int(retry_payload["stagedPipelineCallCount"] or 0)
        metrics["retryExternalCallCount"] += sum(
            int(retry_payload[key] or 0)
            for key in (
                "amapPlaceCallDelta",
                "amapRouteCallDelta",
                "webSearchCallDelta",
            )
        )
        metrics["retryPortfolioDriftCount"] += int(
            retry_payload["retryExecutionPlan"]["proposalIds"] != [visible.proposal_id]
        )
        metrics["duplicatePortfolioCount"] += int(
            db_connection.execute(
                "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
            != 1
        )

    assert metrics["initialVisibleProposalCount"] == stability_runs
    assert metrics["retryCheckpointReuseCount"] == stability_runs
    assert all(
        value == 0
        for key, value in metrics.items()
        if key not in {"initialVisibleProposalCount", "retryCheckpointReuseCount"}
    )


def test_hard_candidate_gap_persists_readonly_comparison_with_zero_timeline_write(
    db_connection,
    monkeypatch,
):
    monkeypatch.setattr(
        AgentService,
        "_creative_portfolio_enabled",
        lambda _self, _context: True,
    )
    session = ConversationService(db_connection).create_session(
        "北京",
        "hard gap partial portfolio",
    )
    service = AgentService(db_connection)
    monkeypatch.setattr(
        service.portfolio_candidate_discovery_service.discovery_service,
        "discover",
        lambda **_kwargs: PoiDiscoveryResult(
            status="provider_failure",
            failureReason="fixture_provider_gap",
        ),
    )
    user_turn_id = service._insert_turn(
        session.session_id,
        "user",
        "北京高校与夜景两日游",
        "active",
    )
    assistant_turn_id = service._insert_turn(
        session.session_id,
        "assistant",
        "planning",
        "active",
    )
    occurrence_plan = {
        "schemaVersion": "goal-occurrence-plan-v1",
        "avoidRecentEntities": True,
        "sourceFingerprint": "h" * 64,
        "occurrences": [
            {
                "occurrenceId": "occ:goal_campus:day:1",
                "sourceGoalId": "goal_campus",
                "intentType": "campus_visit",
                "dayNumber": 1,
                "requirementLevel": "hard",
            },
            {
                "occurrenceId": "occ:goal_night:day:2",
                "sourceGoalId": "goal_night",
                "intentType": "night_view",
                "dayNumber": 2,
                "requirementLevel": "hard",
            },
        ],
    }
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                },
                {
                    "goalId": "goal_night",
                    "intentType": "night_view",
                    "requiredMin": 1,
                },
            ],
        },
        "goalOccurrencePlan": occurrence_plan,
        "agentDecisionState": {"observationFingerprint": "h" * 64},
    }
    generated = _hard_gap_generated()
    campus = _candidate(
        "B000CAMPUS",
        "清华大学",
        "campus_visit",
        providerTypeCode="141201",
        briefId="brief_hard_gap",
        poolId="hard_gap_campus_pool",
        planningSlotId="hard_gap_campus",
        dayNumber=1,
        sourceGoalId="goal_campus",
    )
    grounding_report = {
        "poolReports": [
            {
                "city": "北京",
                "briefId": "brief_hard_gap",
                "poolId": "hard_gap_campus_pool",
                "goalId": "goal_campus",
                "intentType": "campus_visit",
                "requirementLevel": "required",
                "rawNeed": "北京高校",
                "requiredSlotIds": ["hard_gap_campus"],
                "resolvedSlotIds": ["hard_gap_campus"],
                "slotDayNumbers": {"hard_gap_campus": 1},
                "safeCandidates": [campus],
            },
            {
                "city": "北京",
                "briefId": "brief_hard_gap",
                "poolId": "hard_gap_night_pool",
                "goalId": "goal_night",
                "intentType": "night_view",
                "requirementLevel": "required",
                "rawNeed": "北京夜景",
                "requiredSlotIds": ["hard_gap_night"],
                "unresolvedSlotIds": ["hard_gap_night"],
                "slotDayNumbers": {"hard_gap_night": 2},
                "safeCandidates": [],
                "providerState": "provider_failure",
            },
        ],
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京高校与夜景两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=service._initial_plan_from_creative_portfolio(generated),
        segment_plans=[],
        grounding_report=grounding_report,
        ledger=ledger,
        generated=generated,
    )

    active = service._session(session.session_id)
    payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (assistant_turn_id,),
        ).fetchone()[0]
    )
    assert active["active_version_id"] is None
    assert response.terminal_status == "candidate_refresh_required"
    assert response.version is None
    assert response.itinerary is None
    root_blockers = [
        ((step.get("metadata") or {}).get("resultPreview") or {}).get("rootGlobalBlocker")
        for step in payload.get("planningSteps") or []
        if isinstance(step, dict)
    ]
    blocker = next((item for item in root_blockers if isinstance(item, dict)), None)
    assert blocker is not None
    assert blocker["kind"] == "required_goal_gap"
    assert blocker["status"] == "candidate_refresh_required"
    assert blocker["goals"] == [
        {
            "goalId": "goal_night",
            "intentType": "night_view",
            "missingCount": 1,
            "reasonCodes": [
                "goal_occurrence_missing:occ:goal_night:day:2:day_2",
                "required_goal_count_insufficient:goal_night:0/1",
                "required_goal_omitted:goal_night",
            ],
        }
    ]
    assert not payload.get("comparisonProjections")
    assert not any(
        option.get("action") in {"select_plan_proposal", "adopt_active_partial"}
        or option.get("kind") in {"portfolio_more_plans", "portfolio_partial_more_plans"}
        for option in payload.get("choiceOptions") or []
    )
    assert not any(
        step.get("type") == "creative_frontier_auto_advanced"
        for step in payload.get("planningSteps") or []
        if isinstance(step, dict)
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id IN ("
            "SELECT id FROM agent_plan_portfolios WHERE session_id = ?)",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("stability_iteration", range(3))
def test_density_shortfall_creates_partial_timeline_with_pending_slot(db_connection, monkeypatch, stability_iteration):
    monkeypatch.setattr(AgentService, "_creative_portfolio_enabled", lambda _self, _context: True)
    session = ConversationService(db_connection).create_session("北京", "partial portfolio")
    service = AgentService(db_connection)
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "routeDecisionContract": _canonical_server_route_decision_contract(),
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                }
            ],
        },
        "agentDecisionState": {"observationFingerprint": "p" * 64},
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (json.dumps(request_context, ensure_ascii=False), assistant_turn_id),
    )
    db_connection.commit()
    initial_plan = service._initial_plan_from_creative_portfolio(_partial_generated())
    campus = _candidate(
        "campus_partial",
        "清华大学",
        "campus_visit",
        briefId="brief_partial",
        poolId="partial_campus_pool",
        planningSlotId="partial_campus",
        dayNumber=1,
        sourceGoalId="goal_campus",
    )
    grounding_report = {
        "poolReports": [
            {
                "city": "北京",
                "briefId": "brief_partial",
                "poolId": "partial_campus_pool",
                "intentType": "campus_visit",
                "rawNeed": "985大学",
                "safeCandidates": [campus],
            },
            {
                "city": "北京",
                "briefId": "brief_partial",
                "poolId": "partial_walk_pool",
                "intentType": "neighborhood_walk",
                "rawNeed": "街区漫步",
                "safeCandidates": [],
                "unresolvedSlotIds": ["partial_walk"],
            },
        ],
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})
    session_before_partial = service._session(session.session_id)
    streamed_events = []

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=initial_plan,
        segment_plans=[],
        grounding_report=grounding_report,
        ledger=ledger,
        generated=_partial_generated(),
        event_sink=streamed_events.append,
    )

    active = service._session(session.session_id)
    assert response.terminal_status == "needs_confirmation"
    assert response.version is None
    assert response.itinerary is None
    assert active["active_version_id"] is None
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 0
    )
    adoption_choice = next(
        option for option in response.assistant_turn.choice_options if option.get("action") == "select_plan_proposal"
    )
    preview_response = response
    response = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="采用为可编辑草案",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": preview_response.assistant_turn.id,
                    "choiceId": adoption_choice["id"],
                }
            },
        ),
    )
    active = service._session(session.session_id)
    version_row = db_connection.execute(
        "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
        (active["active_version_id"],),
    ).fetchone()
    assert version_row is not None
    snapshot = json.loads(version_row["snapshot_json"])
    assert response.version is not None
    assert response.version.id == active["active_version_id"]
    assert response.itinerary is not None
    assert response.itinerary.status == "partial"
    assert [segment.poi.name for segment in response.itinerary.days[0].segments] == ["清华大学"]
    assert response.itinerary.days[1].segments == []
    assert response.itinerary.days[1].pending_slots[0].planning_slot_id == "partial_walk"
    assert response.assistant_turn.itinerary_version_id == response.version.id
    continuation_options = [
        option
        for option in response.assistant_turn.choice_options
        if option.get("action")
        in {
            "refresh_density_candidates",
            "expand_density_nearby",
        }
    ]
    assert continuation_options
    assert {option.get("expectedBaseVersionId") for option in continuation_options} == {response.version.id}
    assert snapshot["portfolioPartialTimeline"]["strictProposalVerifierPassed"] is False
    assert snapshot["portfolioPendingSlots"][0]["planningSlotId"] == "partial_walk"
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    # The visible partial card has matching server material, allowing future
    # route finalization to reload it without ever accepting frontend state.
    partial_proposal = db_connection.execute(
        """SELECT id, status, evidence_json
        FROM agent_plan_proposals
        WHERE portfolio_id IN (
            SELECT id FROM agent_plan_portfolios WHERE session_id = ?
        )""",
        (session.session_id,),
    ).fetchone()
    assert partial_proposal is not None
    assert str(partial_proposal["id"])
    partial_evidence = json.loads(partial_proposal["evidence_json"])
    assert partial_evidence["readiness"]["pendingSlotCount"] == 1
    assert partial_evidence["readiness"]["nextAction"] == "complete_pending_slots"
    partial_event = next(item for item in response.planning_steps if item.type == "proposal_adoption_committed")
    assert partial_event.status == "completed"
    assert partial_event.metadata["versionWriteCount"] == 1
    assert partial_event.metadata["patchWriteCount"] == 1
    stored_payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (response.assistant_turn.id,),
        ).fetchone()["agent_response_json"]
    )
    assert stored_payload["mode"] == "plan_proposal_commit"
    assert stored_payload["resultVersionId"] == response.version.id
    assert stored_payload["choiceOptions"]
    visible_labels = [event.label for event in streamed_events if event.user_visible]
    assert "发现并核验地点候选" in visible_labels
    assert any(label.startswith("已校验 1/") for label in visible_labels)

    # The editable proposal is adopted exactly once, then only scoped soft-slot
    # continuation controls remain.  No second adoption capability is offered.
    assert not any(
        option.get("action") in {"adopt_active_partial", "select_plan_proposal"}
        for option in response.assistant_turn.choice_options
    )
    assert {option.get("planningSlotId") for option in response.assistant_turn.choice_options} == {"partial_walk"}
    assert {option.get("expectedBaseVersionId") for option in response.assistant_turn.choice_options} == {
        response.version.id
    }
    projection_failure_turn_id = service._insert_turn(
        session.session_id,
        "assistant",
        "projection failed after commit",
        "active",
    )
    degraded = service._failed_staged_pipeline_response(
        session.session_id,
        user_turn_id,
        projection_failure_turn_id,
        "北京两日游",
        request_context,
        session_before_partial,
        [],
        "'timeWindow'",
        AgentVerifierReport(passed=True),
    )
    assert degraded.version is not None
    assert degraded.version.id == response.version.id
    assert degraded.itinerary is not None
    assert degraded.itinerary.status == "partial"
    assert service._session(session.session_id)["active_version_id"] == response.version.id
    assert "timeWindow" not in degraded.assistant_turn.content

    walk = {
        "id": "amap_shichahai",
        "amapId": "amap_shichahai",
        "name": "什刹海",
        "city": "北京",
        "district": "西城区",
        "address": "什刹海街道",
        "type": "风景名胜;风景名胜相关;旅游景点",
        "category": "scenic",
        "longitude": 116.385,
        "latitude": 39.941,
        "source": "amap-place-search",
        "sourceNote": "recorded AMap fixture",
        "confidence": 0.96,
        "photos": [],
        "briefId": "brief_partial",
        "poolId": "partial_walk_pool",
        "planningSlotId": "partial_walk",
        "dayNumber": 2,
        "intentType": "neighborhood_walk",
    }
    candidate_record_id = "cand_partial_walk"
    choice_id = "portfolio_density_candidate_cand_partial_walk_amap_shichahai"
    choice = {
        "id": choice_id,
        "action": "resume_density_candidate",
        "kind": "portfolio_density_candidate",
        "continuationType": "portfolio_density_completion",
        "label": "Day 2：什刹海",
        "candidateRecordId": candidate_record_id,
        "amapId": "amap_shichahai",
        "briefId": "brief_partial",
        "poolId": "partial_walk_pool",
        "planningSlotId": "partial_walk",
        "dayNumber": 2,
        "intentType": "neighborhood_walk",
        "expectedBaseVersionId": response.version.id,
        "sourceUserTurnId": user_turn_id,
        "lifecycle": "offered",
    }
    db_connection.execute(
        """
        INSERT INTO amap_poi_candidates (
            id, session_id, turn_id, query, segment_id, city, category, status,
            candidates_json, selected_amap_id, created_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?, NULL, ?)
        """,
        (
            candidate_record_id,
            session.session_id,
            response.assistant_turn.id,
            "街区漫步",
            "北京",
            "scenic",
            json.dumps([walk], ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    generic_retry_choice = {
        "id": "retry_model_planning_generic_partial",
        "action": "retry_model_planning",
        "kind": "retry",
        "label": "重试模型规划",
        "lifecycle": "offered",
    }
    stored_payload["choiceOptions"] = [choice, generic_retry_choice]
    source_request_context = {
        **request_context,
        "currentUserTurnId": user_turn_id,
        "latestUserMessage": "北京两日游",
        "effectiveUserMessage": "北京两日游",
        "sourceUserRequest": "北京两日游",
        "planningDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校",
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": [],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "街区",
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 1,
            "searchPriority": ["campus_visit", "neighborhood_walk"],
            "candidateSelectionPolicy": {
                "autoSelectWhenDominant": True,
                "askWhenMaterialTradeoff": True,
                "preferLowDetour": True,
                "avoidRecentEntities": True,
            },
            "schedulePolicy": {
                "respectOpeningWindowsWhenKnown": True,
                "allowProvisionalWhenUnknown": True,
            },
        },
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ?, agent_response_json = ? WHERE id = ?",
        (
            json.dumps(source_request_context, ensure_ascii=False),
            json.dumps(stored_payload, ensure_ascii=False),
            response.assistant_turn.id,
        ),
    )
    db_connection.commit()

    assert (
        db_connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        == response.version.id
    )
    with pytest.raises(HTTPException) as rejected_generic_retry:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="重试模型规划",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": response.assistant_turn.id,
                        "choiceId": generic_retry_choice["id"],
                    }
                },
            ),
        )
    assert rejected_generic_retry.value.status_code == 409
    assert rejected_generic_retry.value.detail["code"] == "portfolio_partial_exact_slot_required"
    # Direct service invocation bypasses the API transaction boundary. Mirror
    # the route's rollback before exercising the next independent request.
    db_connection.rollback()
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db_connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        == response.version.id
    )
    rejected_execution = db_connection.execute(
        """SELECT status, result_version_id FROM agent_choice_executions
        WHERE session_id = ? AND choice_id = ?""",
        (session.session_id, generic_retry_choice["id"]),
    ).fetchone()
    assert rejected_execution["status"] == "failed_retryable"
    assert rejected_execution["result_version_id"] is None

    def fail_global_staging(*_args, **_kwargs):
        raise AssertionError("exact pending-slot choice must not enter global staging")

    monkeypatch.setattr(
        service,
        "_generate_with_initial_staged_pipeline",
        fail_global_staging,
    )
    selected = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": response.assistant_turn.id,
                    "choiceId": choice_id,
                }
            },
        ),
    )
    assert selected.version is not None, (
        selected.assistant_turn.timeline_mutation_outcome.get("status"),
        selected.assistant_turn.timeline_mutation_outcome.get("errorCode"),
        selected.assistant_turn.timeline_mutation_outcome.get("warnings"),
    )
    assert selected.version.id != response.version.id
    assert selected.assistant_turn.timeline_mutation_outcome["status"] == "success"
    assert selected.assistant_turn.timeline_mutation_outcome["routeWriteDelta"] == 0
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 2
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    trace = selected.user_turn.structured_choice_trace
    assert trace["requestChoiceId"] == choice_id
    assert trace["persistedChoiceId"] == choice_id
    assert trace["executionChoiceId"] == choice_id
    assert trace["executionAction"] == "resume_density_candidate"
    assert trace["executionStatus"] == "succeeded"
    assert trace["routeWriteDelta"] == 0
    execution = db_connection.execute(
        """SELECT source_turn_id, choice_id, action, status, result_version_id, outcome_json
        FROM agent_choice_executions WHERE session_id = ? AND choice_id = ?""",
        (session.session_id, choice_id),
    ).fetchone()
    assert execution["source_turn_id"] == response.assistant_turn.id
    assert execution["choice_id"] == choice_id
    assert execution["action"] == "resume_density_candidate"
    assert execution["status"] == "succeeded"
    assert execution["result_version_id"] == selected.version.id
    execution_outcome = json.loads(execution["outcome_json"])
    assert execution_outcome["reason"] == "slot_covered"
    assert execution_outcome["routeWriteDelta"] == 0
    selected_snapshot = json.loads(
        db_connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (selected.version.id,),
        ).fetchone()["snapshot_json"]
    )
    assert selected_snapshot["portfolioPendingSlots"] == []
    assert selected_snapshot["portfolioPartialTimeline"]["pendingSlotsStatus"] == "completed"
    assert selected_snapshot["portfolioPartialTimeline"]["strictProposalVerifierPassed"] is False
    assert selected_snapshot["portfolioPartialTimeline"]["status"] == "partial"
    assert selected_snapshot["status"] == "partial"
    added_walk = next(
        segment
        for day in selected_snapshot["days"]
        for segment in day["segments"]
        if (segment.get("poi") or {}).get("amapId") == "amap_shichahai"
    )
    assert added_walk["semanticMetadata"]["planningSlotId"] == "partial_walk"
    assert added_walk["semanticMetadata"]["manualPlacementSource"] == "user_chat_choice"
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 2
    )

    duplicate = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": response.assistant_turn.id,
                    "choiceId": choice_id,
                }
            },
        ),
    )
    assert duplicate.version.id == selected.version.id
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 2
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM agent_choice_executions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 3
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize("stability_iteration", range(30))
def test_three_pending_slots_close_one_exact_slot_at_a_time_and_reoffer_remaining(
    db_connection, monkeypatch, stability_iteration
):
    occurrence_contract = {
        "dayCount": 2,
        "requiredIntents": [
            {
                "goalId": "goal_campus_visit",
                "intentType": "campus_visit",
                "requiredMin": 1,
                "preferredCount": 2,
                "maxCount": 2,
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "required",
            },
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requiredMin": 1,
                "preferredCount": 1,
                "maxCount": 1,
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "required",
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "soft_experience",
            },
        ],
    }
    occurrence_ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "resolvedTripDates": {"dayCount": 2},
            "requestIntentContract": occurrence_contract,
        }
    )
    occurrence_terminal_failure = False
    try:
        occurrence_plan = GoalOccurrenceCompiler().compile(
            occurrence_ledger,
            {
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": [
                            "goal_campus_visit",
                            "goal_night_view",
                            "goal_meal",
                        ],
                        "requiredGoalCounts": {
                            "goal_campus_visit": 1,
                            "goal_night_view": 1,
                            "goal_meal": 1,
                        },
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["goal_campus_visit", "goal_meal"],
                        "requiredGoalCounts": {
                            "goal_campus_visit": 1,
                            "goal_meal": 1,
                        },
                        "optionalGoalIds": [],
                    },
                ],
                "candidateSelectionPolicy": {"avoidRecentEntities": True},
            },
        )
    except ValueError:
        occurrence_terminal_failure = True
        occurrence_plan = None
    assert occurrence_plan is not None
    meal_occurrences = [item for item in occurrence_plan.occurrences if item.source_goal_id == "goal_meal"]
    assert [(item.day_number, item.requirement_level) for item in meal_occurrences] == [
        (1, "explicit_soft"),
        (2, "explicit_soft"),
    ]
    session = ConversationService(db_connection).create_session("北京", "three pending slots")
    service = AgentService(db_connection)
    monkeypatch.setattr(RouteService, "build_routes", _recorded_amap_route_matrix)
    monkeypatch.setattr(
        service.timeline_mutation_service.resolver.adjacent_insertion_service,
        "validate_selected",
        lambda _bound, candidate, *, city: (
            True,
            [
                {
                    "amapPoiId": candidate.id,
                    "routeVerification": {
                        "status": "passed",
                        "legs": [
                            {
                                "distanceMeters": 900,
                                "durationSeconds": 600,
                                "mode": "transit",
                                "source": "recorded-amap-fixture",
                            }
                        ],
                    },
                }
            ],
            None,
        ),
    )
    original_request = (
        "今年国庆参观北京高校两日游，晚上看北京夜景。"
        "10月1日到2日，中等预算，1人，公交地铁优先。每天午餐想体验当地特色美食。"
    )
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "transportPreferences": ["public_transit"],
            "routeDecisionContract": _canonical_server_route_decision_contract(),
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                }
            ],
        },
        "agentDecisionState": {"observationFingerprint": "t" * 64},
    }
    initial_plan = service._initial_plan_from_creative_portfolio(_partial_generated())
    campus = _candidate(
        "campus_three_slot",
        "清华大学",
        "campus_visit",
        briefId="brief_partial",
        poolId="partial_campus_pool",
        planningSlotId="partial_campus",
        dayNumber=1,
        sourceGoalId="goal_campus",
    )
    original_generate_for_user_turn = service._generate_for_user_turn

    def recorded_public_generation(
        session_id,
        user_turn_id,
        content,
        _payload,
        event_sink=None,
    ):
        assert content == original_request
        assistant_turn_id = service._insert_turn(
            session_id,
            "assistant",
            "recorded planning",
            "active",
            agent_request_json=request_context,
        )
        return service._stage_creative_portfolio_response(
            session_id=session_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            content=content,
            request_context=request_context,
            session_before=service._session(session_id),
            tool_events=[],
            initial_plan=initial_plan,
            segment_plans=[],
            grounding_report={
                "poolReports": [
                    {
                        "city": "北京",
                        "briefId": "brief_partial",
                        "poolId": "partial_campus_pool",
                        "intentType": "campus_visit",
                        "rawNeed": "985大学",
                        "safeCandidates": [campus],
                    },
                    {
                        "city": "北京",
                        "briefId": "brief_partial",
                        "poolId": "partial_walk_pool",
                        "intentType": "neighborhood_walk",
                        "rawNeed": "街区漫步",
                        "safeCandidates": [],
                        "unresolvedSlotIds": ["partial_walk"],
                    },
                ],
            },
            ledger=ConstraintLedgerCompiler().compile({**request_context, "city": "北京"}),
            generated=_partial_generated(),
            event_sink=event_sink,
        )

    monkeypatch.setattr(service, "_generate_for_user_turn", recorded_public_generation)
    response = service.send_message(
        session.session_id,
        AgentMessageRequest(content=original_request),
    )
    monkeypatch.setattr(
        service,
        "_generate_for_user_turn",
        original_generate_for_user_turn,
    )
    source_user_turn_id = response.user_turn.id
    source_assistant_turn_id = response.assistant_turn.id
    assert response.version is None, json.dumps(
        {
            "terminalStatus": response.terminal_status,
            "reply": response.assistant_turn.content,
            "choices": [
                (item.get("kind"), item.get("action"), item.get("label"))
                for item in response.assistant_turn.choice_options
            ],
            "steps": [
                (item.type, item.status, item.detail, item.metadata)
                for item in response.planning_steps
                if "partial" in item.type or "proposal" in item.type or "portfolio" in item.type
            ],
        },
        ensure_ascii=False,
        default=str,
    )
    adoption_choice = next(
        item for item in response.assistant_turn.choice_options if item.get("action") == "select_plan_proposal"
    )
    adopted = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="采用为可编辑草案",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": source_assistant_turn_id,
                    "choiceId": adoption_choice["id"],
                }
            },
        ),
    )
    assert adopted.version is not None
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    initial_version_id = adopted.version.id
    initial_comparison_projections = [
        item["comparisonProjection"]
        for item in response.assistant_turn.choice_options
        if item.get("comparisonProjection")
    ]
    assert initial_comparison_projections
    version_row = db_connection.execute(
        "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
        (initial_version_id,),
    ).fetchone()
    snapshot = json.loads(version_row["snapshot_json"])
    snapshot["portfolioPendingSlots"] = [
        {
            "id": "pending:slot_art",
            "briefId": "brief_partial",
            "poolId": "pool_art",
            "planningSlotId": "slot_art",
            "dayNumber": 1,
            "timeWindow": "14:00-18:00",
            "startTime": "14:00",
            "endTime": "18:00",
            "durationMinutes": 120,
            "rawNeed": "art_walk",
            "displayNeed": "art_walk",
            "intentType": "art_walk",
            "kind": "activity",
            "state": "pending",
        },
        {
            "id": "pending:slot_campus_day2",
            "briefId": "brief_partial",
            "poolId": "pool_campus_day2",
            "planningSlotId": "slot_campus_day2",
            "dayNumber": 2,
            "timeWindow": "09:00-12:00",
            "startTime": "09:00",
            "endTime": "12:00",
            "durationMinutes": 120,
            "rawNeed": "高校参观",
            "displayNeed": "高校参观",
            "intentType": "campus_visit",
            "kind": "visit",
            "state": "pending",
        },
        {
            "id": "pending:partial_walk",
            "briefId": "brief_partial",
            "poolId": "partial_walk_pool",
            "planningSlotId": "partial_walk",
            "dayNumber": 2,
            "timeWindow": "14:00-18:00",
            "startTime": "14:00",
            "endTime": "18:00",
            "durationMinutes": 120,
            "rawNeed": "heritage_walk",
            "displayNeed": "heritage_walk",
            "intentType": "neighborhood_walk",
            "kind": "activity",
            "state": "pending",
        },
    ]
    snapshot["status"] = "partial"
    snapshot["portfolioPartialTimeline"] = {
        "status": "partial",
        "sourceProposalId": None,
        "strictProposalVerifierPassed": False,
        "pendingSlotCount": 3,
    }
    portfolio_row = db_connection.execute(
        "SELECT id, source_user_turn_id, request_contract_fingerprint "
        "FROM agent_plan_portfolios WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
        (session.session_id,),
    ).fetchone()
    snapshot["portfolioSelectionContext"] = {
        "planningSelectionRootTurnId": portfolio_row["source_user_turn_id"],
        "rootPortfolioId": portfolio_row["id"],
        "focusBriefId": "brief_partial",
        "sourceUserTurnId": source_user_turn_id,
        "requestContractFingerprint": portfolio_row["request_contract_fingerprint"],
    }
    snapshot["portfolioPartialTimeline"]["pendingSlotsStatus"] = "needs_confirmation"
    db_connection.execute(
        "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
        (json.dumps(snapshot, ensure_ascii=False), initial_version_id),
    )

    candidates = [
        ("slot_art", "pool_art", 1, "art_walk", "B000000798", "798艺术区"),
        ("slot_campus_day2", "pool_campus_day2", 2, "campus_visit", "B000000PKU", "北京大学"),
        ("partial_walk", "partial_walk_pool", 2, "neighborhood_walk", "B0000BJZOO", "北京动物园"),
    ]
    candidate_choices = []
    now = datetime.now(timezone.utc).isoformat()
    for index, (slot_id, pool_id, day_number, intent_type, amap_id, name) in enumerate(candidates):
        if slot_id == "partial_walk":
            continue
        candidate_record_id = f"cand_{slot_id}_{stability_iteration}"
        poi = _candidate(
            amap_id,
            name,
            intent_type,
            briefId="brief_partial",
            poolId=pool_id,
            planningSlotId=slot_id,
            dayNumber=day_number,
        )
        poi.update(
            {
                "amapId": amap_id,
                "district": "海淀区" if day_number == 1 else "西城区",
                "address": f"{name} recorded fixture",
                "category": "scenic",
                "sourceNote": "recorded AMap fixture",
                "photos": [],
            }
        )
        db_connection.execute(
            """INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, NULL, '北京', 'scenic', 'pending', ?, NULL, ?)""",
            (
                candidate_record_id,
                session.session_id,
                source_assistant_turn_id,
                name,
                json.dumps([poi], ensure_ascii=False),
                now,
            ),
        )
        candidate_choices.append(
            {
                "id": f"choice_{slot_id}_{stability_iteration}",
                "action": "resume_density_candidate",
                "kind": "portfolio_density_candidate",
                "continuationType": "portfolio_density_completion",
                "label": f"Day {day_number}：{name}",
                "candidateRecordId": candidate_record_id,
                "amapId": amap_id,
                "briefId": "brief_partial",
                "poolId": pool_id,
                "planningSlotId": slot_id,
                "dayNumber": day_number,
                "intentType": intent_type,
                "expectedBaseVersionId": initial_version_id,
                "sourceUserTurnId": source_user_turn_id,
                "lifecycle": "offered",
            }
        )
    source_payload = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (source_assistant_turn_id,),
        ).fetchone()[0]
    )
    readonly_projection_option = adoption_choice
    # Simulate an old persisted session that still exposes the legacy action.
    # The same opaque choice id already owns authoritative proposal material;
    # current code must migrate it to finalization and then reject adoption
    # while required slots remain pending.
    adoption_option = {
        **readonly_projection_option,
        "id": f"legacy_partial_adoption_{stability_iteration}",
        "action": "adopt_active_partial",
        "kind": "plan_proposal",
        "label": "采用当前部分方案（旧版）",
        "expectedBaseVersionId": initial_version_id,
        "lifecycle": "offered",
    }
    manual_option = {
        "id": f"portfolio_density_manual_partial_walk_{stability_iteration}",
        "action": "manual_continuation",
        "kind": "custom_input",
        "label": "手动输入 Day 2 街区漫步地点",
        "description": "输入具体地点名称后继续核验；不会直接写入未核验地点。",
        "allowsManualInput": True,
        "briefId": "brief_partial",
        "poolId": "partial_walk_pool",
        "planningSlotId": "partial_walk",
        "dayNumber": 2,
        "intentType": "neighborhood_walk",
        "timeWindow": "14:00-18:00",
        "displayNeed": "heritage_walk",
        "sourceUserTurnId": source_user_turn_id,
        "expectedBaseVersionId": initial_version_id,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "lifecycle": "offered",
    }
    source_payload["choiceOptions"] = [adoption_option, *candidate_choices, manual_option]
    source_request_context = {
        **request_context,
        "currentUserTurnId": source_user_turn_id,
        "latestUserMessage": "北京两日游",
        "effectiveUserMessage": "北京两日游",
        "sourceUserRequest": "北京两日游",
        "planningDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus"],
            "dayStrategies": [],
            "optionalExperienceBudget": 3,
            "searchPriority": ["art_walk", "campus_visit", "heritage_walk"],
            "candidateSelectionPolicy": {
                "autoSelectWhenDominant": True,
                "askWhenMaterialTradeoff": True,
                "preferLowDetour": True,
                "avoidRecentEntities": True,
            },
            "schedulePolicy": {
                "respectOpeningWindowsWhenKnown": True,
                "allowProvisionalWhenUnknown": True,
            },
        },
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ?, agent_response_json = ? WHERE id = ?",
        (
            json.dumps(source_request_context, ensure_ascii=False),
            json.dumps(source_payload, ensure_ascii=False),
            source_assistant_turn_id,
        ),
    )
    stale_claim_time = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    db_connection.execute(
        """INSERT INTO agent_choice_executions (
            id, session_id, source_turn_id, source_user_turn_id, choice_id, action,
            status, expected_base_version_id, execution_turn_id, request_turn_id, attempt,
            continuation_json, checkpoint_fingerprint, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'adopt_active_partial', 'executing', ?, ?, ?, 1, NULL, NULL, ?, ?)""",
        (
            f"stale_partial_adoption_{stability_iteration}",
            session.session_id,
            source_assistant_turn_id,
            source_user_turn_id,
            adoption_option["id"],
            initial_version_id,
            f"crashed_turn_{stability_iteration}",
            f"crashed_turn_{stability_iteration}",
            stale_claim_time,
            stale_claim_time,
        ),
    )
    db_connection.commit()

    before_adoption = tuple(
        db_connection.execute(
            "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
            (session.session_id, session.session_id, session.active_plan_id),
        ).fetchone()
    )
    with pytest.raises(HTTPException) as rejected_legacy_adoption:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="采用此方案",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_assistant_turn_id,
                        "choiceId": adoption_option["id"],
                    }
                },
            ),
        )
    assert rejected_legacy_adoption.value.status_code == 409
    assert rejected_legacy_adoption.value.detail["code"] in {
        "plan_proposal_readiness_blocked",
        "plan_proposal_route_quality_failed",
        "partial_adoption_base_stale",
    }
    db_connection.rollback()
    after_adoption = tuple(
        db_connection.execute(
            "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
            (session.session_id, session.session_id, session.active_plan_id),
        ).fetchone()
    )
    assert after_adoption == before_adoption

    source_turn_id = source_assistant_turn_id
    pending_sequence = [3]
    previous_slot_ids = {item[0] for item in candidates}
    first_old_choice = candidate_choices[1]
    final_response = None
    slot_write_deltas = []
    changed_slot_counts = []
    manual_queries = []
    manual_write_delta = None
    manual_trace = None
    candidate_choice_scope = None
    all_remaining_slots_reoffered = True
    latest_base_version_used = True
    stale_base_write_delta = None

    def recorded_manual_search(
        _self,
        city,
        keyword,
        category="all",
        limit=12,
        **_kwargs,
    ):
        manual_budget = current_amap_call_budget()
        assert manual_budget is not None
        assert manual_budget.place_text_max == 1
        assert manual_budget.total_external_max == 1
        manual_queries.append((city, keyword, category, limit))
        if keyword != "北京动物园":
            raise AssertionError(f"manual density search escaped exact seed: {keyword}")
        return MapPoiSearchResponse(
            city="北京",
            keyword=keyword,
            category=category,
            providerName="recorded-amap-fixture",
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="B0000BJZOO",
                    name="北京动物园",
                    type="风景名胜;公园广场;动物园",
                    city="北京市",
                    district="西城区",
                    address="西直门外大街137号",
                    longitude=116.339,
                    latitude=39.938,
                    category="scenic",
                    source="amap-place-search",
                    sourceNote="recorded AMap fixture",
                    confidence=0.98,
                    photos=[],
                )
            ],
        )

    monkeypatch.setattr(MapPoiService, "search", recorded_manual_search)
    for expected_remaining, target in zip((2, 1, 0), candidates):
        current_turn = service._turn_response(source_turn_id)
        if target[0] == "partial_walk":
            scoped_manual = next(
                item
                for item in current_turn.choice_options
                if item.get("action") == "manual_continuation" and item.get("planningSlotId") == target[0]
            )
            before_manual = tuple(
                db_connection.execute(
                    "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
                    (session.session_id, session.session_id, session.active_plan_id),
                ).fetchone()
            )
            manual_response = service.send_message(
                session.session_id,
                AgentMessageRequest(
                    content="北京动物园",
                    context={
                        "selectedAgentChoice": {
                            "sourceAssistantTurnId": source_turn_id,
                            "choiceId": scoped_manual["id"],
                            "manualValue": "北京动物园",
                        }
                    },
                ),
            )
            after_manual = tuple(
                db_connection.execute(
                    "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
                    (session.session_id, session.session_id, session.active_plan_id),
                ).fetchone()
            )
            assert after_manual == before_manual
            manual_write_delta = tuple(after - before for before, after in zip(before_manual, after_manual))
            manual_trace = manual_response.user_turn.structured_choice_trace
            assert (
                manual_response.user_turn.structured_choice_trace["normalizedAction"]
                == "search_density_manual_candidates"
            )
            assert manual_queries and {query[1] for query in manual_queries} == {"北京动物园"}, json.dumps(
                {
                    "manualQueries": manual_queries,
                    "choiceOptions": manual_response.assistant_turn.choice_options,
                    "planningSteps": [step.model_dump(by_alias=True) for step in manual_response.planning_steps],
                },
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
            assert manual_response.user_turn.structured_choice_trace["structuredPlanningChoiceResume"] is True, (
                json.dumps(
                    {
                        "trace": manual_response.user_turn.structured_choice_trace,
                        "request": json.loads(
                            db_connection.execute(
                                "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
                                (manual_response.user_turn.id,),
                            ).fetchone()[0]
                        ),
                        "executions": [
                            dict(row)
                            for row in db_connection.execute(
                                "SELECT * FROM agent_choice_executions WHERE session_id = ? ORDER BY created_at",
                                (session.session_id,),
                            ).fetchall()
                        ],
                    },
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                )
            )
            assert manual_response.user_turn.structured_choice_trace["controllerCalled"] is False
            assert manual_response.user_turn.structured_choice_trace["executionStatus"] == "succeeded", json.dumps(
                manual_response.assistant_turn.choice_options,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
            )
            assert manual_queries and {query[1] for query in manual_queries} == {"北京动物园"}
            source_turn_id = manual_response.assistant_turn.id
            current_turn = service._turn_response(source_turn_id)
            option = next(
                item
                for item in current_turn.choice_options
                if item.get("kind") == "portfolio_density_candidate"
                and item.get("planningSlotId") == target[0]
                and item.get("amapId") == target[4]
            )
            candidate_choice_scope = {
                key: option.get(key)
                for key in (
                    "briefId",
                    "poolId",
                    "planningSlotId",
                    "dayNumber",
                    "intentType",
                    "expectedBaseVersionId",
                    "amapId",
                )
            }
        else:
            option = next(item for item in current_turn.choice_options if item.get("planningSlotId") == target[0])
        expected_base = service._session(session.session_id)["active_version_id"]
        latest_base_version_used = latest_base_version_used and option["expectedBaseVersionId"] == expected_base
        assert option["expectedBaseVersionId"] == expected_base
        before_slot = tuple(
            db_connection.execute(
                "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
                "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
                "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
                (session.session_id, session.session_id, session.active_plan_id),
            ).fetchone()
        )
        final_response = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择 Agent 选项",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_turn_id,
                        "choiceId": option["id"],
                    }
                },
            ),
        )
        if final_response.version is None:
            raise AssertionError(
                json.dumps(
                    {
                        "reply": final_response.assistant_turn.content,
                        "warnings": final_response.warnings,
                        "terminal": final_response.terminal_status,
                        "trace": final_response.user_turn.structured_choice_trace,
                        "outcome": final_response.assistant_turn.timeline_mutation_outcome,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        after_slot = tuple(
            db_connection.execute(
                "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
                "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
                "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
                (session.session_id, session.session_id, session.active_plan_id),
            ).fetchone()
        )
        slot_delta = tuple(after - before for before, after in zip(before_slot, after_slot))
        slot_write_deltas.append(slot_delta)
        assert slot_delta[:2] == (1, 1)
        assert slot_delta[2] == final_response.assistant_turn.timeline_mutation_outcome["routeWriteDelta"]
        active_snapshot = json.loads(
            db_connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (final_response.version.id,),
            ).fetchone()[0]
        )
        remaining_slot_ids = {item["planningSlotId"] for item in active_snapshot["portfolioPendingSlots"]}
        changed_slot_count = len(previous_slot_ids - remaining_slot_ids)
        changed_slot_counts.append(changed_slot_count)
        assert previous_slot_ids - remaining_slot_ids == {target[0]}
        assert len(remaining_slot_ids) == expected_remaining
        pending_sequence.append(expected_remaining)
        if expected_remaining:
            reoffered = {
                item.get("planningSlotId")
                for item in final_response.assistant_turn.choice_options
                if item.get("planningSlotId")
            }
            all_remaining_slots_reoffered = all_remaining_slots_reoffered and reoffered == remaining_slot_ids
            assert reoffered == remaining_slot_ids
            reoffered_base_versions = {
                item.get("expectedBaseVersionId")
                for item in final_response.assistant_turn.choice_options
                if item.get("planningSlotId")
            }
            latest_base_version_used = latest_base_version_used and reoffered_base_versions == {
                final_response.version.id
            }
            assert reoffered_base_versions == {final_response.version.id}
        previous_slot_ids = remaining_slot_ids
        source_turn_id = final_response.assistant_turn.id
        if expected_remaining == 2:
            counts_before_stale_base = tuple(
                db_connection.execute(
                    "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
                    (session.session_id, session.session_id, session.active_plan_id),
                ).fetchone()
            )
            with pytest.raises(HTTPException) as stale_base:
                service.send_message(
                    session.session_id,
                    AgentMessageRequest(
                        content="选择旧版本候选",
                        context={
                            "selectedAgentChoice": {
                                "sourceAssistantTurnId": source_assistant_turn_id,
                                "choiceId": first_old_choice["id"],
                            }
                        },
                    ),
                )
            assert stale_base.value.status_code == 409
            db_connection.rollback()
            counts_after_stale_base = tuple(
                db_connection.execute(
                    "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
                    "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
                    (session.session_id, session.session_id, session.active_plan_id),
                ).fetchone()
            )
            stale_base_write_delta = tuple(
                after - before
                for before, after in zip(
                    counts_before_stale_base,
                    counts_after_stale_base,
                )
            )
            assert stale_base_write_delta == (0, 0, 0)
    assert pending_sequence == [3, 2, 1, 0]
    assert slot_write_deltas == [(1, 1, 1), (1, 1, 0), (1, 1, 1)]

    counts_before_duplicate = tuple(
        db_connection.execute(
            "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
            (session.session_id, session.session_id, session.active_plan_id),
        ).fetchone()
    )
    duplicate = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": final_response.user_turn.structured_choice_trace["sourceAssistantTurnId"],
                    "choiceId": final_response.user_turn.structured_choice_trace["resolvedChoiceId"],
                }
            },
        ),
    )
    assert duplicate.version.id == final_response.version.id
    counts_after_duplicate = tuple(
        db_connection.execute(
            "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
            (session.session_id, session.session_id, session.active_plan_id),
        ).fetchone()
    )
    assert counts_after_duplicate == counts_before_duplicate
    duplicate_write_delta = tuple(
        after - before for before, after in zip(counts_before_duplicate, counts_after_duplicate)
    )

    with pytest.raises(HTTPException) as stale:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择 Agent 选项",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_assistant_turn_id,
                        "choiceId": first_old_choice["id"],
                    }
                },
            ),
        )
    assert stale.value.status_code == 409
    db_connection.rollback()
    counts_after_stale = tuple(
        db_connection.execute(
            "SELECT (SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?), "
            "(SELECT COUNT(*) FROM route_options WHERE plan_id = ?)",
            (session.session_id, session.session_id, session.active_plan_id),
        ).fetchone()
    )
    assert counts_after_stale == counts_before_duplicate
    stale_write_delta = tuple(after - before for before, after in zip(counts_before_duplicate, counts_after_stale))
    active_version_id = service._session(session.session_id)["active_version_id"]
    active_snapshot = json.loads(
        db_connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (active_version_id,),
        ).fetchone()[0]
    )
    response_snapshot = final_response.itinerary.model_dump(by_alias=True)

    def timeline_segment_keys(payload):
        return {
            (
                int(day.get("dayNumber") or 0),
                str(segment.get("id") or ""),
                str((segment.get("poi") or {}).get("id") or ""),
            )
            for day in payload.get("days") or []
            for segment in day.get("segments") or []
        }

    metrics = {
        "iteration": stability_iteration + 1,
        "goalOccurrenceTerminalFailure": occurrence_terminal_failure,
        "publicEntryOriginalRequestPreserved": response.user_turn.content == original_request,
        "visibleComparisonProjectionCount": len(initial_comparison_projections),
        "activePartialVisible": any(
            projection.get("isPartial") is True for projection in initial_comparison_projections
        ),
        "activePartialAdoptionVersionPatchRouteDelta": list(
            after - before for before, after in zip(before_adoption, after_adoption)
        ),
        "legacyPartialAdoptionRejected": rejected_legacy_adoption.value.status_code == 409,
        "initialPendingSlotCount": pending_sequence[0],
        "pendingSlotCountSequence": pending_sequence,
        "manualSearchVersionPatchRouteDelta": list(manual_write_delta),
        "manualInputDirectWriteCount": sum(abs(item) for item in manual_write_delta),
        "manualStructuredResume": manual_trace["structuredPlanningChoiceResume"],
        "manualControllerCalled": manual_trace["controllerCalled"],
        "manualExecutionStatus": manual_trace["executionStatus"],
        "manualSearchQueries": [list(query) for query in manual_queries],
        "candidateChoiceScope": candidate_choice_scope,
        "slotWriteDeltas": [list(delta) for delta in slot_write_deltas],
        "eachChoiceChangedSlotCounts": changed_slot_counts,
        "allRemainingSlotsReoffered": all_remaining_slots_reoffered,
        "latestBaseVersionUsed": latest_base_version_used,
        "duplicateFinalSlotChoiceVersionPatchRouteDelta": list(duplicate_write_delta),
        "staleChoiceVersionPatchRouteDelta": list(stale_write_delta),
        "staleBaseCandidateVersionPatchRouteDelta": list(stale_base_write_delta),
        "activeTimelineProjectionMismatch": (
            final_response.version.id != active_version_id
            or timeline_segment_keys(response_snapshot) != timeline_segment_keys(active_snapshot)
        ),
        "fakeMarkerOrRouteCount": sum(
            1
            for projection in initial_comparison_projections
            for day in projection.get("days") or []
            for segment in day.get("segments") or []
            if segment.get("poi") and segment["poi"].get("source") != "amap-place-search"
        )
        + sum(
            1
            for projection in initial_comparison_projections
            for route in projection.get("routeEvidence") or []
            if route.get("source")
            not in {
                "amap-route",
                "recorded-amap-fixture",
            }
        ),
        "pendingSlotScopeDriftCount": sum(changed_count != 1 for changed_count in changed_slot_counts),
        "proposalScopeDriftCount": sum(
            projection.get("planningSelectionRootTurnId")
            != initial_comparison_projections[0].get("planningSelectionRootTurnId")
            or projection.get("rootPortfolioId") != initial_comparison_projections[0].get("rootPortfolioId")
            for projection in initial_comparison_projections
        ),
        "choiceReplayFailureCount": int(duplicate.version.id != final_response.version.id),
    }
    assert metrics["manualSearchVersionPatchRouteDelta"] == [0, 0, 0]
    assert metrics["manualControllerCalled"] is False
    assert metrics["eachChoiceChangedSlotCounts"] == [1, 1, 1]
    assert metrics["activeTimelineProjectionMismatch"] is False
    assert metrics["fakeMarkerOrRouteCount"] == 0
    assert metrics["pendingSlotScopeDriftCount"] == 0
    assert metrics["proposalScopeDriftCount"] == 0
    assert metrics["choiceReplayFailureCount"] == 0
    assert metrics["staleBaseCandidateVersionPatchRouteDelta"] == [0, 0, 0]
    print(
        "TRIP_BACKEND_STABILITY_METRICS="
        + json.dumps(
            metrics,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def test_portfolio_turn_has_zero_itinerary_writes_until_persisted_opaque_choice(db_connection, monkeypatch):
    session = ConversationService(db_connection).create_session("北京", "creative portfolio")
    service = AgentService(db_connection)

    def recorded_title_candidates(context):
        reserved = set(context.get("reservedTitles") or [])
        signal = str(next(iter(context.get("requiredTitleSignals") or ["京华"])))
        titles = [
            f"{signal}学府艺馆京华",
            f"{signal}书香艺韵京城",
            f"{signal}校园文艺漫步",
            f"{signal}梧桐书页京华",
            f"{signal}丹青书声京城",
            f"{signal}艺馆学府漫游",
            f"{signal}学府丹青慢行",
            f"{signal}书声艺韵相逢",
        ]
        available = [title for title in titles if title not in reserved]
        assert len(available) >= 3
        return {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {
                    "title": title,
                    "evidenceAmapIds": context["evidenceAmapIds"],
                }
                for title in available[:3]
            ],
        }

    monkeypatch.setattr(
        service.provider,
        "generate_proposal_titles",
        recorded_title_candidates,
    )
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"], "dayCount": 2},
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "transportPreferences": ["public_transit"],
            "routeDecisionContract": _canonical_server_route_decision_contract(),
            "requiredIntents": [
                {"goalId": "goal_campus", "intentType": "campus_visit", "requiredMin": 1},
                {"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1},
            ],
        },
        "agentDecisionState": {"observationFingerprint": "a" * 64},
        # Proposal generation persists its planning context. The monotonic
        # deadline is turn-local and will be stale by the time a user chooses
        # a proposal; the choice commit must never inherit this value.
        "runtimeDeadlineMonotonic": time.monotonic() - 1,
    }
    initial_plan = AgentInitialPlanOutput(reply="", mode="initial_plan", daySlots=[], intentPools=[])
    campus = _candidate("B00000001", "清华大学", "campus_visit")
    campus_alternative = _candidate("B00000002", "北京大学", "campus_visit")
    museum = _candidate("B00000003", "中国美术馆", "museum")
    museum_alternative = _candidate("B00000004", "中央美术学院美术馆", "museum")
    segment_plans = [
        PersistableSegmentPlan(
            day_number=1,
            start_time="09:00",
            duration_minutes=120,
            kind="visit",
            route_anchor=True,
            selected_poi=_poi(campus),
            display_title="清华大学",
            notes="",
            grounding_status="selected",
            ticket_status="not_needed",
            date="2026-10-01",
            raw_need="985大学",
            intent_type="campus_visit",
            goal_id="goal_campus",
            requirement_level="required",
            required=True,
        ),
        PersistableSegmentPlan(
            day_number=2,
            start_time="09:00",
            duration_minutes=120,
            kind="visit",
            route_anchor=True,
            selected_poi=_poi(museum),
            display_title="中国美术馆",
            notes="",
            grounding_status="selected",
            ticket_status="not_needed",
            date="2026-10-02",
            raw_need="美术馆",
            intent_type="museum",
            goal_id="goal_museum",
            requirement_level="required",
            required=True,
        ),
    ]
    grounding_report = {
        "poolReports": [
            {
                "city": "北京",
                "intentType": "campus_visit",
                "rawNeed": "985大学",
                "safeCandidates": [campus, campus_alternative],
            },
            {
                "city": "北京",
                "intentType": "museum",
                "rawNeed": "美术馆",
                "safeCandidates": [museum, museum_alternative],
            },
        ]
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})
    db_connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (json.dumps(request_context, ensure_ascii=False), assistant_turn_id),
    )
    db_connection.commit()

    response = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=initial_plan,
        segment_plans=segment_plans,
        grounding_report=grounding_report,
        ledger=ledger,
        generated=_generated(),
    )

    assert response.terminal_status == "needs_confirmation"
    options = [item for item in response.assistant_turn.choice_options if item.get("action") == "select_plan_proposal"]
    # The root persists every direction, but the first turn grounds only the
    # focus brief so its first safe result is not blocked by sibling routes.
    assert len(options) == 1
    visible_choice_ids = [str(option["id"]) for option in options]
    placeholders = ", ".join("?" for _ in visible_choice_ids)
    visible_rows = db_connection.execute(
        f"SELECT snapshot_json FROM agent_plan_proposals WHERE choice_id IN ({placeholders}) ORDER BY rank_index",
        visible_choice_ids,
    ).fetchall()
    visible_grounded_identities = {
        tuple(
            sorted(
                segment["poi"]["amapId"]
                for day in json.loads(row[0])["days"]
                for segment in day["segments"]
                if segment.get("poi", {}).get("amapId")
            )
        )
        for row in visible_rows
    }
    assert len(visible_grounded_identities) == 1
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM itinerary_patches").fetchone()[0] == 0
    observation_context = service.context_builder.build(
        service._session(session.session_id),
        "查看方案",
        AgentMessageRequest(content="查看方案"),
    )
    observation = service.autonomy_observation_builder.build(observation_context)
    assert observation.plan_portfolio.status == "awaiting_selection"
    assert observation.plan_portfolio.visible_proposal_count == 1

    selected = options[0]
    db_connection.execute(
        "UPDATE conversation_turns SET status = 'superseded' WHERE id = ?",
        (assistant_turn_id,),
    )
    db_connection.commit()
    with pytest.raises(HTTPException) as superseded_error:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择已过期方案",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": assistant_turn_id,
                        "choiceId": selected["id"],
                    }
                },
            ),
        )
    assert superseded_error.value.status_code in {404, 409}
    assert db_connection.execute("SELECT count(*) FROM agent_choice_executions").fetchone()[0] == 0
    db_connection.execute(
        "UPDATE conversation_turns SET status = 'active' WHERE id = ?",
        (assistant_turn_id,),
    )
    db_connection.commit()

    original_refresh_routes = ItineraryService.refresh_routes
    original_runtime_limits = service._runtime_limits_for_request
    original_route_verify = service.portfolio_route_feasibility_service.verify_snapshot

    def reject_route_quality(snapshot, **_kwargs):
        return PortfolioRouteFeasibilityResult(
            status="failed",
            snapshot=snapshot,
            route_quality_issues=[
                {
                    "code": "meal_detour_high",
                    "fromPoiName": "清华大学",
                    "toPoiName": "老北京炸酱面",
                    "distanceKm": 15,
                    "durationMinutes": 69,
                }
            ],
            recommended_next_actions=["choose_nearby_meal"],
            requires_route_verification=True,
            provider_state="ok",
        )

    monkeypatch.setattr(service.portfolio_route_feasibility_service, "verify_snapshot", reject_route_quality)
    with pytest.raises(HTTPException) as route_quality_error:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择第二套",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": assistant_turn_id,
                        "choiceId": selected["id"],
                    }
                },
            ),
        )
    assert route_quality_error.value.status_code == 409
    assert route_quality_error.value.detail["code"] == "plan_proposal_route_quality_failed"
    assert route_quality_error.value.detail["details"]["routeQualityIssues"][0]["distanceKm"] == 15
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM itinerary_patches").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM route_options").fetchone()[0] == 0
    assert db_connection.execute("SELECT status FROM agent_plan_portfolios").fetchone()[0] == "awaiting_selection"
    monkeypatch.setattr(service.portfolio_route_feasibility_service, "verify_snapshot", original_route_verify)

    original_verify_agent_write = AgentVerifierService.verify_agent_write

    def reject_final_route_quality(_self, *_args, **_kwargs):
        return AgentVerifierReport(
            passed=False,
            hard_failures=["route_quality:meal_detour_high"],
        )

    monkeypatch.setattr(AgentVerifierService, "verify_agent_write", reject_final_route_quality)
    with pytest.raises(HTTPException) as final_route_quality_error:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择第二套",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": assistant_turn_id,
                        "choiceId": selected["id"],
                    }
                },
            ),
        )
    assert final_route_quality_error.value.status_code == 409
    assert final_route_quality_error.value.detail["code"] == "plan_proposal_route_quality_failed"
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM itinerary_patches").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM route_options").fetchone()[0] == 0
    assert db_connection.execute("SELECT result_version_id FROM agent_choice_executions").fetchone()[0] is None
    assert db_connection.execute("SELECT status FROM agent_plan_portfolios").fetchone()[0] == "awaiting_selection"
    monkeypatch.setattr(AgentVerifierService, "verify_agent_write", original_verify_agent_write)

    def fail_refresh_routes(*_args, **_kwargs):
        raise RuntimeError("AMap route refresh unavailable")

    monkeypatch.setattr(ItineraryService, "refresh_routes", fail_refresh_routes)
    with pytest.raises(HTTPException) as refresh_failure_error:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择第二套",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": assistant_turn_id,
                        "choiceId": selected["id"],
                    }
                },
            ),
        )
    assert refresh_failure_error.value.status_code == 409
    assert refresh_failure_error.value.detail["code"] == "plan_proposal_route_quality_failed"
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM itinerary_patches").fetchone()[0] == 0
    assert db_connection.execute("SELECT count(*) FROM route_options").fetchone()[0] == 0
    assert (
        db_connection.execute(
            "SELECT count(*) FROM itinerary_days WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    assert db_connection.execute("SELECT status FROM agent_plan_portfolios").fetchone()[0] == "awaiting_selection"

    def slow_refresh_routes(*args, **kwargs):

        result = original_refresh_routes(*args, **kwargs)
        time.sleep(0.03)
        return result

    monkeypatch.setattr(ItineraryService, "refresh_routes", slow_refresh_routes)
    monkeypatch.setattr(
        service,
        "_runtime_limits_for_request",
        lambda _context: AgentRuntimeLimits(max_patch_seconds=0.01),
    )
    with pytest.raises(HTTPException) as deadline_error:
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择第二套",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": assistant_turn_id,
                        "choiceId": selected["id"],
                    }
                },
            ),
        )
    assert deadline_error.value.status_code == 409
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 0
    assert (
        db_connection.execute(
            "SELECT count(*) FROM itinerary_days WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_connection.execute(
            "SELECT count(*) FROM itinerary_segments WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 0
    )
    failed_patch_count = db_connection.execute(
        "SELECT count(*) FROM itinerary_patches WHERE plan_id = ?",
        (session.active_plan_id,),
    ).fetchone()[0]
    assert failed_patch_count == 0

    monkeypatch.setattr(ItineraryService, "refresh_routes", original_refresh_routes)
    monkeypatch.setattr(service, "_runtime_limits_for_request", original_runtime_limits)
    # Promote this recorded proposal into a same-day two-anchor fixture so the
    # success path must persist one real RouteOption and can prove the trace
    # delta against database rows instead of counting preview evidence.
    proposal_row = db_connection.execute(
        "SELECT id, snapshot_json FROM agent_plan_proposals WHERE choice_id = ?",
        (selected["id"],),
    ).fetchone()
    proposal_snapshot = json.loads(proposal_row["snapshot_json"])
    first_day, second_day = proposal_snapshot["days"][:2]
    museum_segment = second_day["segments"].pop(0)
    museum_segment["startTime"] = "12:00"
    museum_segment["endTime"] = "14:00"
    museum_segment["dayNumber"] = 1
    museum_segment["poi"].update(
        {
            "providerTypeCode": museum["providerTypeCode"],
            "tags": museum["tags"],
        }
    )
    museum_semantic = museum_segment.get("semanticMetadata") or {}
    museum_semantic["scheduleConstraints"] = {
        "earliestStart": "12:00",
        "latestStart": "16:00",
        "windowEnd": "18:00",
        "hard": True,
        "source": "recorded_route_delta_fixture",
    }
    museum_admission = museum_semantic.get("consumerAdmissionReport")
    if isinstance(museum_admission, dict) and isinstance(museum_admission.get("consumerScope"), dict):
        moved_consumer = {
            key: copy.deepcopy(value) for key, value in museum_admission["consumerScope"].items() if value is not None
        }
        moved_consumer["dayNumber"] = 1
        museum_semantic["consumerAdmissionReport"] = ConsumerCandidateAdmissionService().evaluate(
            museum_segment["poi"],
            moved_consumer,
        )
        assert museum_semantic["consumerAdmissionReport"]["scoreEligible"] is True, json.dumps(
            museum_semantic["consumerAdmissionReport"],
            ensure_ascii=False,
            sort_keys=True,
        )
    museum_segment["semanticMetadata"] = museum_semantic
    first_day["segments"].append(museum_segment)
    flexible_day_two = copy.deepcopy(first_day["segments"][0])
    flexible_day_two["id"] = "seg_recorded_flexible_day_two"
    flexible_day_two["kind"] = "activity"
    flexible_day_two["startTime"] = "09:00"
    flexible_day_two["endTime"] = "11:00"
    flexible_day_two["dayNumber"] = 2
    flexible_day_two["poi"].update(
        {
            "id": campus_alternative["id"],
            "amapId": campus_alternative["id"],
            "name": campus_alternative["name"],
            "city": campus_alternative["city"],
            "type": campus_alternative["type"],
            "providerType": campus_alternative["providerType"],
            "longitude": campus_alternative["longitude"],
            "latitude": campus_alternative["latitude"],
            "source": campus_alternative["source"],
        }
    )
    flexible_semantic = flexible_day_two.get("semanticMetadata") or {}
    flexible_semantic.update(
        {
            "required": False,
            "requirementLevel": "soft_experience",
            "routeAnchor": False,
            "goalId": "",
            "groundingStatus": "selected",
        }
    )
    for key in (
        "occurrenceId",
        "planningSlotId",
        "poolId",
        "slotId",
        "scheduleConstraints",
        "creativeBriefId",
        "consumerAdmissionReport",
    ):
        flexible_semantic.pop(key, None)
    flexible_day_two["semanticMetadata"] = flexible_semantic
    second_day["segments"] = [flexible_day_two]
    proposal_snapshot["portfolioDayAnchorTargets"] = {"1": 2}
    for binding in proposal_snapshot.get("portfolioRequiredCandidateBindings") or []:
        if isinstance(binding, dict) and binding.get("sourceGoalId") == "goal_museum":
            binding["dayNumber"] = 1
    title_context = CreativeProposalTitleService.agent_generation_context(
        proposal_snapshot,
        city="北京",
        primary_axis=str((proposal_snapshot.get("creativeBrief") or {}).get("primaryAxis") or ""),
    )
    proposal_snapshot = CreativeProposalTitleService.generate_and_apply_agent_title(
        proposal_snapshot,
        generator=recorded_title_candidates,
        context=title_context,
    )
    assert proposal_snapshot["portfolioTitleGeneration"]["status"] == "succeeded"
    assert CreativeProposalTitleService.is_valid_agent_projection(
        proposal_snapshot,
        proposal_snapshot["portfolioTitleEvidence"],
    )
    db_connection.execute(
        "UPDATE agent_plan_proposals SET snapshot_json = ? WHERE id = ?",
        (json.dumps(proposal_snapshot, ensure_ascii=False), proposal_row["id"]),
    )
    db_connection.commit()
    readonly_proposal_id = "proposal_readonly_old"
    readonly_choice_id = "portfolio_choice_proposal_readonly_old"
    db_connection.execute(
        """
        INSERT INTO agent_plan_proposals (
            id, portfolio_id, choice_id, rank_index, status, brief_json,
            snapshot_json, score_json, verifier_json, evidence_json,
            canonical_signature, generation_lineage_json, created_at, updated_at
        )
        SELECT ?, portfolio_id, ?, rank_index + 1, 'offered', brief_json,
            snapshot_json, score_json, verifier_json, evidence_json,
            canonical_signature || ':readonly', generation_lineage_json,
            created_at, updated_at
        FROM agent_plan_proposals
        WHERE id = ?
        """,
        (readonly_proposal_id, readonly_choice_id, proposal_row["id"]),
    )
    portfolio_summary_row = db_connection.execute("SELECT id, summary_json FROM agent_plan_portfolios").fetchone()
    persisted_membership = json.loads(portfolio_summary_row["summary_json"] or "{}")
    persisted_membership["visibleProposalIds"] = [
        str(proposal_row["id"]),
        readonly_proposal_id,
    ]
    db_connection.execute(
        "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
        (
            json.dumps(persisted_membership, ensure_ascii=False),
            portfolio_summary_row["id"],
        ),
    )
    db_connection.commit()

    route_row_count_before_commit = db_connection.execute(
        "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
        (session.active_plan_id,),
    ).fetchone()[0]

    def recorded_route_delta(
        _self,
        plan_id,
        pois,
        transport_mode=None,
        segments=None,
        route_pairs=None,
        **_kwargs,
    ):
        return _recorded_amap_route_matrix(
            _self,
            plan_id,
            pois,
            transport_mode=transport_mode,
            segments=segments,
            route_pairs=route_pairs,
            **_kwargs,
        )

    monkeypatch.setattr(RouteService, "build_routes", recorded_route_delta)
    committed = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="选择第二套",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": assistant_turn_id,
                    "choiceId": selected["id"],
                }
            },
        ),
    )
    assert committed.version is not None
    route_row_count_after_commit = db_connection.execute(
        "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
        (session.active_plan_id,),
    ).fetchone()[0]
    adoption_event = next(item for item in committed.planning_steps if item.type == "proposal_adoption_committed")
    assert adoption_event.metadata["proposalCommitAttemptCount"] == 1
    assert adoption_event.metadata["versionWriteCount"] == 1
    assert adoption_event.metadata["patchWriteCount"] == 1
    assert route_row_count_after_commit - route_row_count_before_commit == 1
    assert adoption_event.metadata["routeWriteDelta"] == 1
    assert adoption_event.metadata["routeRowCountBefore"] == route_row_count_before_commit
    assert adoption_event.metadata["routeRowCountAfter"] == route_row_count_after_commit
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 1
    assert db_connection.execute("SELECT count(*) FROM itinerary_patches").fetchone()[0] == 1
    active_snapshot = json.loads(
        db_connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (committed.version.id,),
        ).fetchone()[0]
    )
    assert "尚未写入正式行程" not in active_snapshot["decisionRationale"]
    assert "已采用为当前可编辑行程" in active_snapshot["decisionRationale"]
    assert (
        db_connection.execute(
            "SELECT count(*) FROM itinerary_patches WHERE result_version_id = ?",
            (committed.version.id,),
        ).fetchone()[0]
        == 1
    )
    proposal_rows = db_connection.execute("SELECT id, choice_id, status FROM agent_plan_proposals").fetchall()
    statuses = {row[2] for row in proposal_rows}
    assert {row[0]: row[2] for row in proposal_rows} == {
        readonly_proposal_id: "comparison_only",
        proposal_row["id"]: "committed",
    }, [tuple(row) for row in proposal_rows]
    portfolio_summary = json.loads(
        db_connection.execute("SELECT summary_json FROM agent_plan_portfolios").fetchone()[0]
    )
    assert portfolio_summary["selectedProposalId"] == proposal_rows[0][0]
    adopted_projection = next(
        projection
        for projection in committed.assistant_turn.comparison_projections
        if projection.get("proposalId") == proposal_row["id"]
    )
    assert adopted_projection["isAdopted"] is True
    assert adopted_projection["activeVersionId"] == committed.version.id
    visible_projection_ids = {
        str(projection.get("proposalId") or "") for projection in committed.assistant_turn.comparison_projections
    }
    assert visible_projection_ids == {
        str(proposal_row["id"]),
        readonly_proposal_id,
    }
    readonly_projection = next(
        projection
        for projection in committed.assistant_turn.comparison_projections
        if projection.get("proposalId") == readonly_proposal_id
    )
    assert readonly_projection["isAdopted"] is False
    refreshed_turn = service._turn_response(committed.assistant_turn.id)
    assert {
        str(item.get("proposalId") or "") for item in refreshed_turn.comparison_projections
    } == visible_projection_ids

    replay = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="再次选择第二套",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": assistant_turn_id,
                    "choiceId": selected["id"],
                }
            },
        ),
    )
    assert replay.version is not None
    assert replay.version.id == committed.version.id
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 1

    # Simulate a process interruption after the patch/version commit but before
    # proposal CAS finalization.  A duplicate opaque click must reconcile from
    # the accepted patch evidence instead of creating a second version.
    db_connection.execute("UPDATE agent_plan_portfolios SET status = 'committing'")
    db_connection.execute("UPDATE agent_plan_proposals SET status = 'offered'")
    db_connection.execute(
        "UPDATE agent_choice_executions SET status = 'executing', result_version_id = NULL WHERE choice_id = ?",
        (selected["id"],),
    )
    db_connection.commit()
    recovered = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="恢复提交",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": assistant_turn_id,
                    "choiceId": selected["id"],
                }
            },
        ),
    )
    assert recovered.version is not None
    assert recovered.version.id == committed.version.id
    assert db_connection.execute("SELECT count(*) FROM itinerary_versions").fetchone()[0] == 1
    assert db_connection.execute("SELECT status FROM agent_plan_portfolios").fetchone()[0] == "committed"

    stored = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?", (assistant_turn_id,)
        ).fetchone()[0]
    )
    assert stored["versionDelta"] == 0
    assert stored["patchDelta"] == 0


@pytest.mark.parametrize(
    "corruption",
    ["pending_cross_brief", "segment_cross_brief", "empty_partial_metadata"],
)
def test_active_partial_snapshot_scope_error_fails_closed_without_fresh_choices(db_connection, corruption: str):
    session = ConversationService(db_connection).create_session("北京", "partial snapshot fail closed")
    service = AgentService(db_connection)
    user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
    assistant_turn_id = service._insert_turn(session.session_id, "assistant", "planning", "active")
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 2,
            "routeDecisionContract": _canonical_server_route_decision_contract(),
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                }
            ],
        },
        "agentDecisionState": {"observationFingerprint": "q" * 64},
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (json.dumps(request_context, ensure_ascii=False), assistant_turn_id),
    )
    db_connection.commit()
    initial_plan = service._initial_plan_from_creative_portfolio(_partial_generated())
    campus = _candidate(
        "campus_partial_scope",
        "清华大学",
        "campus_visit",
        briefId="brief_partial",
        poolId="partial_campus_pool",
        planningSlotId="partial_campus",
        dayNumber=1,
        sourceGoalId="goal_campus",
    )
    grounding_report = {
        "poolReports": [
            {
                "city": "北京",
                "briefId": "brief_partial",
                "poolId": "partial_campus_pool",
                "intentType": "campus_visit",
                "rawNeed": "985大学",
                "safeCandidates": [campus],
            },
            {
                "city": "北京",
                "briefId": "brief_partial",
                "poolId": "partial_walk_pool",
                "intentType": "neighborhood_walk",
                "rawNeed": "街区漫步",
                "safeCandidates": [],
                "unresolvedSlotIds": ["partial_walk"],
            },
        ],
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})
    first = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        content="北京两日游",
        request_context=request_context,
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=initial_plan,
        segment_plans=[],
        grounding_report=grounding_report,
        ledger=ledger,
        generated=_partial_generated(),
    )
    assert first.version is None
    adoption_choice = next(
        option for option in first.assistant_turn.choice_options if option.get("action") == "select_plan_proposal"
    )
    first = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="采用为可编辑草案",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": assistant_turn_id,
                    "choiceId": adoption_choice["id"],
                }
            },
        ),
    )
    assert first.version is not None
    version_row = db_connection.execute(
        "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
        (first.version.id,),
    ).fetchone()
    snapshot = json.loads(version_row["snapshot_json"])
    if corruption == "pending_cross_brief":
        snapshot["portfolioPendingSlots"][0]["briefId"] = "brief_stale"
    elif corruption == "segment_cross_brief":
        snapshot["days"][0]["segments"][0]["semanticMetadata"]["creativeBriefId"] = "brief_stale"
    else:
        snapshot["portfolioPartialTimeline"] = {}
    db_connection.execute(
        "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
        (json.dumps(snapshot, ensure_ascii=False), first.version.id),
    )
    db_connection.commit()
    db_connection.execute(
        """INSERT INTO amap_poi_candidates (
            id, session_id, turn_id, query, segment_id, city, category,
            status, candidates_json, selected_amap_id, created_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'pending', ?, NULL, ?)""",
        (
            f"existing_pending_{corruption}",
            session.session_id,
            assistant_turn_id,
            "既有候选",
            "北京",
            "area_walk",
            json.dumps(
                [_candidate("existing_pending_poi", "什刹海", "neighborhood_walk")],
                ensure_ascii=False,
            ),
            "2026-07-22T00:00:00+00:00",
        ),
    )
    db_connection.commit()
    assert len(ConversationService(db_connection)._pending_candidates(session.session_id)) == 1
    before = {
        "version": db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "patch": db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "route": db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (service._session(session.session_id)["active_plan_id"],),
        ).fetchone()[0],
        "candidate": db_connection.execute(
            "SELECT COUNT(*) FROM amap_poi_candidates WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "portfolio": db_connection.execute(
            "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "proposal": db_connection.execute(
            """SELECT COUNT(*) FROM agent_plan_proposals q
            JOIN agent_plan_portfolios p ON p.id = q.portfolio_id
            WHERE p.session_id = ?""",
            (session.session_id,),
        ).fetchone()[0],
    }
    retry_user_turn_id = service._insert_turn(session.session_id, "user", "刷新缺失地点候选", "active")
    retry_turn_id = service._insert_turn(session.session_id, "assistant", "retry", "active")

    retry = service._stage_creative_portfolio_response(
        session_id=session.session_id,
        user_turn_id=retry_user_turn_id,
        assistant_turn_id=retry_turn_id,
        content="刷新缺失地点候选",
        request_context={
            **request_context,
            "portfolioDensityContinuation": {
                "briefId": "brief_partial",
                "poolId": "partial_walk_pool",
                "planningSlotId": "partial_walk",
                "dayNumber": 2,
            },
        },
        session_before=service._session(session.session_id),
        tool_events=[],
        initial_plan=initial_plan,
        segment_plans=[],
        grounding_report=grounding_report,
        ledger=ledger,
        generated=_partial_generated(),
    )

    after = {
        "version": db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "patch": db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "route": db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (service._session(session.session_id)["active_plan_id"],),
        ).fetchone()[0],
        "candidate": db_connection.execute(
            "SELECT COUNT(*) FROM amap_poi_candidates WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "portfolio": db_connection.execute(
            "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "proposal": db_connection.execute(
            """SELECT COUNT(*) FROM agent_plan_proposals q
            JOIN agent_plan_portfolios p ON p.id = q.portfolio_id
            WHERE p.session_id = ?""",
            (session.session_id,),
        ).fetchone()[0],
    }
    assert retry.terminal_status == "failed"
    assert retry.assistant_turn.choice_options == []
    assert retry.pending_poi_candidates == []
    assert "待补槽位身份已失效" in retry.assistant_turn.content
    assert after == before

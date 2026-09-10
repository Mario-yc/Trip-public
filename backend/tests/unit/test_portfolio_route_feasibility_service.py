from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models.poi import POI
from src.models.route_option import RouteOption
from src.services.amap_call_budget import (
    AmapCallBudget,
    amap_call_budget_scope,
    current_amap_call_budget,
    current_amap_route_repair_scope,
)
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
from src.services.creative_output_quality_service import CreativeOutputQualityService
from src.services.creative_planning_models import canonical_fingerprint
from src.services.portfolio_route_feasibility_service import PortfolioRouteFeasibilityService
from src.services.poi_discovery_service import PoiDiscoveryResult
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import RouteService


class FakeRouteService:
    map_provider_key = "test-route"
    NEARBY_CANONICAL_AMAP_ID = PoiPhysicalIdentityService.canonical_amap_id(
        {"amapId": "nearby_meal"}
    )

    def build_routes(self, plan_id, pois, transport_mode="transit", segments=None, **_kwargs):
        routes = []
        for left, right in zip(segments or [], (segments or [])[1:]):
            left_poi = next(poi for poi in pois if poi.id == left.poi_id)
            right_poi = next(poi for poi in pois if poi.id == right.poi_id)
            meal_detour = left.kind == "meal" or right.kind == "meal"
            nearby = (
                left_poi.amap_id == self.NEARBY_CANONICAL_AMAP_ID
                or right_poi.amap_id == self.NEARBY_CANONICAL_AMAP_ID
            )
            distance = 1_000 if nearby else 15_000 if meal_detour else 2_000
            duration = 600 if nearby else 3_600 if meal_detour else 900
            routes.append(
                RouteOption(
                    id=f"route_{left.id}_{right.id}",
                    plan_id=plan_id,
                    from_segment_id=left.id,
                    to_segment_id=right.id,
                    from_poi_id=left_poi.id,
                    to_poi_id=right_poi.id,
                    distance_meters=distance,
                    duration_seconds=duration,
                    mode="transit",
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[[116.30, 39.90], [116.31, 39.91]],
                    steps=[
                        {"mode": "walking", "distance": 100},
                        {"mode": "transit", "distance": max(0, distance - 100)},
                    ],
                    provider_payload={"status": "1", "waitSeconds": 0},
                )
            )
        return routes


class FakeMapPoiService:
    def search_nearby(self, *_args, **_kwargs):
        return SimpleNamespace(
            pois=[
                POI(
                    id="nearby_meal",
                    amap_id="nearby_meal",
                    name="相邻小吃店",
                    city="北京",
                    category="food",
                    type="餐饮服务;中餐厅",
                    latitude=39.999,
                    longitude=116.301,
                    source="amap-place-search",
                    confidence=0.95,
                )
            ]
        )


class PassingNearbyAdmissionService:
    @staticmethod
    def evaluate(_candidate, consumer):
        return {
            "classification": "admitted_final_anchor",
            "scoreEligible": True,
            "consumerFingerprint": canonical_fingerprint(consumer),
        }


class FakeInsertionFriendlyRouteService:
    map_provider_key = "test-route"

    def build_routes(self, plan_id, pois, transport_mode="transit", segments=None, **_kwargs):
        distances = {
            ("campus_segment", "meal_segment"): (453, 240),
            ("meal_segment", "museum_segment"): (13_570, 3_240),
            ("campus_segment", "museum_segment"): (18_870, 5_040),
        }
        routes = []
        for left, right in zip(segments or [], (segments or [])[1:]):
            distance, duration = distances[(left.id, right.id)]
            routes.append(
                RouteOption(
                    id=f"route_{left.id}_{right.id}",
                    plan_id=plan_id,
                    from_segment_id=left.id,
                    to_segment_id=right.id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=distance,
                    duration_seconds=duration,
                    mode="transit",
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[[116.30, 39.90], [116.31, 39.91]],
                    steps=[
                        {"mode": "walking", "distance": min(100, distance)},
                        {"mode": "transit", "distance": max(0, distance - 100)},
                    ],
                    provider_payload={"status": "1", "waitSeconds": 0},
                )
            )
        return routes


class FakeUnavailableMealBypassRouteService:
    map_provider_key = "test-route"

    def __init__(self, failure_mode: str):
        self.failure_mode = failure_mode

    def build_routes(self, plan_id, pois, transport_mode="transit", segments=None, **_kwargs):
        if str(plan_id).endswith("_meal_bypass"):
            if self.failure_mode == "raise":
                raise RuntimeError("bypass route unavailable")
            if self.failure_mode == "empty":
                return []
            left, right = (segments or [])[0], (segments or [])[-1]
            return [
                RouteOption(
                    id=f"route_{left.id}_{right.id}",
                    plan_id=plan_id,
                    from_segment_id=left.id,
                    to_segment_id=right.id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=0,
                    duration_seconds=0,
                    mode="transit",
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[[116.30, 39.90], [116.31, 39.91]],
                    steps=[],
                    provider_payload={
                        "status": "1",
                        "walkingDistanceMeters": 0,
                        "transferCount": 0,
                        "waitSeconds": 0,
                    },
                )
            ]
        routes = []
        for left, right in zip(segments or [], (segments or [])[1:]):
            routes.append(
                RouteOption(
                    id=f"route_{left.id}_{right.id}",
                    plan_id=plan_id,
                    from_segment_id=left.id,
                    to_segment_id=right.id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=11_000,
                    duration_seconds=3_600,
                    mode="transit",
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[[116.30, 39.90], [116.31, 39.91]],
                    steps=[
                        {"mode": "walking", "distance": 100},
                        {"mode": "transit", "distance": 10_900},
                    ],
                    provider_payload={"status": "1", "waitSeconds": 0},
                )
            )
        return routes


class RecordingRouteBudgetOrderService(FakeRouteService):
    def __init__(self):
        self.call_kinds = []

    def build_routes(self, plan_id, pois, transport_mode="transit", segments=None, **kwargs):
        if str(plan_id).endswith("_meal_bypass"):
            self.call_kinds.append("meal_bypass")
        elif kwargs.get("include_compact_fallbacks"):
            self.call_kinds.append("compact_fallback")
        else:
            self.call_kinds.append("adjacent_baseline")
        return super().build_routes(plan_id, pois, transport_mode, segments, **kwargs)


class RecordingCanonicalRouteService:
    map_provider_key = "amap-key"

    def __init__(self, failed_pairs=()):
        self.failed_pairs = set(failed_pairs)
        self.requested_pairs = []

    def build_routes(
        self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **_kwargs,
    ):
        segment_by_id = {segment.id: segment for segment in segments or []}
        pairs = sorted(route_pairs or [])
        self.requested_pairs.append(pairs)
        routes = []
        for left_id, right_id in pairs:
            if (left_id, right_id) in self.failed_pairs:
                continue
            left = segment_by_id[left_id]
            right = segment_by_id[right_id]
            routes.append(
                RouteOption(
                    id=f"route_{left_id}_{right_id}",
                    plan_id=plan_id,
                    from_segment_id=left_id,
                    to_segment_id=right_id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=1_000,
                    duration_seconds=600,
                    mode=transport_mode,
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[[116.30, 39.90], [116.31, 39.91]],
                    steps=[
                        {"mode": "walking", "distance": 100},
                        {"mode": "transit", "distance": 900},
                    ],
                    provider_payload={"status": "1", "waitSeconds": 0},
                )
            )
        return routes


class BudgetAwareRouteService(RecordingCanonicalRouteService):
    def build_routes(
        self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **kwargs,
    ):
        budget = current_amap_call_budget()
        allowed_pairs = set()
        for left_id, right_id in sorted(route_pairs or []):
            if budget is None or budget.try_acquire(
                endpoint="route/transit",
                keyword=f"{left_id}:{right_id}",
                source="route-ledger-test",
            ):
                allowed_pairs.add((left_id, right_id))
        return super().build_routes(
            plan_id,
            pois,
            transport_mode,
            segments,
            route_pairs=allowed_pairs,
            **kwargs,
        )


class RaisingBudgetRouteService:
    map_provider_key = "amap-key"

    def build_routes(self, *_args, **_kwargs):
        budget = current_amap_call_budget()
        assert budget is not None
        budget.try_acquire(
            endpoint="route/transit",
            keyword="raising-provider",
            source="route-ledger-test",
        )
        raise RuntimeError("sanitized route failure")


class RecordingReplacementRouteService(RecordingCanonicalRouteService):
    NEARBY_MEAL_AMAP_ID = PoiPhysicalIdentityService.canonical_amap_id(
        {"amapId": "nearby_meal"}
    )
    NEARBY_MUSEUM_AMAP_ID = PoiPhysicalIdentityService.canonical_amap_id(
        {"amapId": "nearby_museum"}
    )

    def __init__(self):
        super().__init__()
        self.requested_batches = []
        self.scope_receipts = []

    def build_routes(
        self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **_kwargs,
    ):
        segment_by_id = {segment.id: segment for segment in segments or []}
        pairs = sorted(route_pairs or [])
        self.requested_pairs.append(pairs)
        self.requested_batches.append((str(plan_id), pairs))
        self.scope_receipts.append(
            {
                "planId": str(plan_id),
                "mode": str(transport_mode),
                "pairs": pairs,
                "repairScopeCertificate": copy.deepcopy(
                    current_amap_route_repair_scope()
                ),
            }
        )
        routes = []
        for left_id, right_id in pairs:
            left = segment_by_id[left_id]
            right = segment_by_id[right_id]
            includes_meal = left_id == "meal_segment" or right_id == "meal_segment"
            meal_poi_id = (
                left.poi_id if left_id == "meal_segment" else right.poi_id if right_id == "meal_segment" else ""
            )
            nearby = meal_poi_id == self.NEARBY_MEAL_AMAP_ID
            schedule_friendly = self.NEARBY_MUSEUM_AMAP_ID in {
                left.poi_id,
                right.poi_id,
            }
            meal_bypass = str(plan_id).endswith("_meal_bypass")
            distance = (
                2_000 if meal_bypass else 100 if schedule_friendly else 1_000 if not includes_meal or nearby else 15_000
            )
            duration = (
                900 if meal_bypass else 60 if schedule_friendly else 600 if not includes_meal or nearby else 3_600
            )
            routes.append(
                RouteOption(
                    id=f"route_{left_id}_{right_id}_{meal_poi_id or 'base'}",
                    plan_id=plan_id,
                    from_segment_id=left_id,
                    to_segment_id=right_id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=distance,
                    duration_seconds=duration,
                    mode=transport_mode,
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[[116.30, 39.90], [116.31, 39.91]],
                    steps=[
                        {"mode": "walking", "distance": min(100, distance)},
                        {"mode": "transit", "distance": max(0, distance - 100)},
                    ],
                    provider_payload={"status": "1", "waitSeconds": 0},
                )
            )
        return routes


class BudgetAwareReplacementRouteService(RecordingReplacementRouteService):
    def build_routes(
        self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **kwargs,
    ):
        budget = current_amap_call_budget()
        allowed_pairs = set()
        for left_id, right_id in sorted(route_pairs or []):
            if budget is None or budget.try_acquire(
                endpoint="route/transit",
                keyword=f"{left_id}:{right_id}",
                source="route-repair-budget-test",
            ):
                allowed_pairs.add((left_id, right_id))
        return super().build_routes(
            plan_id,
            pois,
            transport_mode,
            segments,
            route_pairs=allowed_pairs,
            **kwargs,
        )


def _poi(poi_id: str, name: str, category: str) -> dict:
    return {
        "id": poi_id,
        "amapId": poi_id,
        "name": name,
        "city": "北京",
        "category": category,
        "type": "餐饮服务;中餐厅" if category == "food" else "科教文化服务;学校;高等院校",
        "providerType": "餐饮服务;中餐厅" if category == "food" else "科教文化服务;学校;高等院校",
        "longitude": 116.30 if category != "food" else 116.80,
        "latitude": 40.00 if category != "food" else 39.90,
        "source": "amap-place-search",
        "confidence": 0.95,
    }


def _explicit_mobility_profile() -> dict:
    return {
        "source": "request_accessibility_profile",
        "walkingPenaltyMinutesPerKm": 2.4,
        "transferPenaltyMinutes": 7.0,
        "waitTimeMultiplier": 1.2,
        "riskPenaltyMultiplier": 1.1,
    }


def _route_decision_contract(*, detour_tolerance: dict | None = None, include_mobility: bool = True) -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"compiler": "goal_request_compiler"},
        detour_tolerance=detour_tolerance
        or {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        mobility_profile=(_explicit_mobility_profile() if include_mobility else {}),
    )
    if contract is None:
        return {
            "schemaVersion": "route-decision-contract-v1",
            "source": "request_intent_contract",
            "provenance": {"compiler": "goal_request_compiler"},
            "detourTolerance": detour_tolerance,
            "fingerprint": "invalid",
        }
    return {"schemaVersion": "route-decision-contract-v1", **contract}


def _snapshot(meal_id: str = "detouring_meal") -> dict:
    return {
        "id": "plan_test",
        "planningSelectionRootTurnId": "planning_root_turn_test",
        "portfolioTransportPreference": "transit",
        "routeDecisionContract": _route_decision_contract(),
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "campus_segment",
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "kind": "visit",
                        "poi": _poi("campus", "清华大学", "campus"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "groundingStatus": "selected",
                            "creativeBriefId": "brief_1",
                            "poolId": "campus_pool",
                            "planningSlotId": "campus_slot",
                        },
                    },
                    {
                        "id": "meal_segment",
                        "startTime": "11:00",
                        "endTime": "12:00",
                        "kind": "meal",
                        "poi": _poi(meal_id, "老北京炸酱面" if meal_id != "nearby_meal" else "相邻小吃店", "food"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "routePreference": {"preferNearAdjacentAnchors": True},
                            "groundingStatus": "selected",
                            "creativeBriefId": "brief_1",
                            "poolId": "meal_pool",
                            "planningSlotId": "meal_slot",
                            "sourceGoalId": "goal_meal",
                        },
                    },
                    {
                        "id": "museum_segment",
                        "startTime": "12:00",
                        "endTime": "14:00",
                        "kind": "visit",
                        "poi": _poi("museum", "中国国家博物馆", "museum"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "groundingStatus": "selected",
                            "creativeBriefId": "brief_1",
                            "poolId": "museum_pool",
                            "planningSlotId": "museum_slot",
                        },
                    },
                ],
            }
        ],
    }


def test_meal_route_proof_preserves_its_authoritative_experience_time_window():
    tolerance = {
        "maxGeneralizedCostDelta": 60,
        "maxDetourRatio": 2.0,
    }
    policy = {
        "timeWindow": {"start": "11:00", "end": "12:30"},
        "detourTolerance": tolerance,
    }
    snapshot = _snapshot("nearby_meal")
    snapshot["routeDecisionContract"] = _route_decision_contract(
        detour_tolerance=tolerance
    )
    meal_semantic = snapshot["days"][0]["segments"][1]["semanticMetadata"]
    meal_semantic["routeContract"] = {
        "requiresProviderInsertionDecision": True,
        "experienceSpecPolicy": policy,
        "specFingerprint": canonical_fingerprint(policy),
    }

    result = PortfolioRouteFeasibilityService(
        route_service=RecordingCanonicalRouteService()
    ).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.passed
    proof = result.snapshot["days"][0]["segments"][1]["semanticMetadata"][
        "routeInsertionMatrixProof"
    ]
    assert proof["timeWindow"] == policy["timeWindow"]


def _attach_current_consumer_admission(
    snapshot: dict,
    *,
    segment_id: str,
    family: str,
    activity_mode: str,
    experience_goal: str,
) -> dict:
    day, segment = next(
        (day, item)
        for day in snapshot["days"]
        for item in day["segments"]
        if item["id"] == segment_id
    )
    semantic = segment["semanticMetadata"]
    semantic["consumerAdmissionInput"] = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id=semantic["creativeBriefId"],
        pool_id=semantic["poolId"],
        planning_slot_id=semantic["planningSlotId"],
        day_number=day["dayNumber"],
        city="北京",
        family=family,
        activity_mode=activity_mode,
        requirement_level="soft",
        experience_shape="single_poi",
        experience_goal=experience_goal,
    )
    return snapshot


def test_route_sensitive_detour_fails_before_any_write_and_keeps_input_unchanged():
    snapshot = _snapshot()
    planned_query_hash = canonical_fingerprint({"query": "nearby-local-food", "scope": "meal_slot"})
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeQueryFingerprint"] = planned_query_hash
    result = PortfolioRouteFeasibilityService(route_service=FakeRouteService()).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    blocking = next(
        item for item in result.route_quality_issues if item["code"] == "provider_route_matrix_unacceptable"
    )
    assert blocking["failureCode"] == "provider_route_matrix_unacceptable"
    assert blocking["dayNumber"] == 1
    assert blocking["mealSegmentId"] == "meal_segment"
    assert blocking["mealPlanningSlotId"] == "meal_slot"
    assert blocking["mealPoolId"] == "meal_pool"
    assert blocking["mealBriefId"] == "brief_1"
    assert blocking["previousSegmentId"] == "campus_segment"
    assert blocking["nextSegmentId"] == "museum_segment"
    assert blocking["assessmentBasis"] == "provider_generalized_cost_delta"
    assert blocking["generalizedCostDelta"] > 0
    assert blocking["detourRatio"] > 0
    assert blocking["timeWindowFeasible"] is True
    assert result.snapshot["portfolioRouteVerificationRequired"] is True
    assert "portfolioRouteEvidence" not in snapshot
    assert snapshot["days"][0]["segments"][1]["poi"]["amapId"] == "detouring_meal"
    checkpoints = result.repair_attempt_ledger["routeBadCheckpoints"]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint["continuationMode"] == "repair_exact_slot"
    assert checkpoint["rootPortfolioId"] == "plan_test"
    assert checkpoint["briefId"] == "brief_1"
    assert checkpoint["poolId"] == "meal_pool"
    assert checkpoint["dayNumber"] == 1
    assert checkpoint["planningSlotId"] == "meal_slot"
    assert checkpoint["candidatePhysicalId"] == "DETOURING_MEAL"
    assert checkpoint["adjacentRouteLedgerKeys"] == [
        {"fromPhysicalId": "CAMPUS", "toPhysicalId": "DETOURING_MEAL", "mode": "transit"},
        {"fromPhysicalId": "DETOURING_MEAL", "toPhysicalId": "MUSEUM", "mode": "transit"},
    ]
    assert checkpoint["routeContractFingerprint"] == snapshot["routeDecisionContract"]["fingerprint"]
    assert checkpoint["scopeFingerprint"]
    assert checkpoint["usedQueryFingerprints"] == [planned_query_hash]
    assert checkpoint["executedQueryFingerprints"] == []
    assert checkpoint["queryCursor"] == 0
    assert checkpoint["admittedCanonicalPhysicalIds"] == []
    assert len(checkpoint["fullProviderMatrixProofFingerprints"]) == 1
    assert "segmentId" not in checkpoint
    assert checkpoint["adjacentAnchorIds"] == ["campus_segment", "museum_segment"]
    assert result.snapshot["portfolioRouteQuality"]["repairAttemptLedger"] == result.repair_attempt_ledger
    assert result.repair_attempt_ledger["rejectedCanonicalPhysicalIds"] == ["DETOURING_MEAL"]


def test_same_physical_pair_with_changed_logical_adjacent_occurrence_rejects_old_scope():
    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    snapshot = _snapshot()
    segment = next(
        item for item in service._snapshot_segments(snapshot) if item["id"] == "meal_segment"
    )
    original_scope = service._route_repair_scope_certificate(
        snapshot,
        segment=segment,
        candidate=service._segment_poi_payload(segment),
        transport_mode="transit",
    )

    rebound_snapshot = json.loads(json.dumps(snapshot))
    rebound_snapshot["days"][0]["segments"][0]["id"] = "campus_segment_rebound"
    rebound_segment = next(
        item
        for item in service._snapshot_segments(rebound_snapshot)
        if item["id"] == "meal_segment"
    )
    rebound_scope = service._route_repair_scope_certificate(
        rebound_snapshot,
        segment=rebound_segment,
        candidate=service._segment_poi_payload(rebound_segment),
        transport_mode="transit",
    )

    assert original_scope is not None
    assert rebound_scope is not None
    assert original_scope["adjacentRouteLedgerKeys"] == rebound_scope["adjacentRouteLedgerKeys"]
    assert original_scope["adjacentAnchorIds"] == ["campus_segment", "museum_segment"]
    assert rebound_scope["adjacentAnchorIds"] == [
        "campus_segment_rebound",
        "museum_segment",
    ]
    assert service._same_repair_slot_scope(original_scope, rebound_scope) is False
    replacement_scope = json.loads(json.dumps(original_scope))
    replacement_scope["candidatePhysicalId"] = "REPLACEMENT_MEAL"
    replacement_scope["adjacentRouteLedgerKeys"][0]["toPhysicalId"] = "REPLACEMENT_MEAL"
    replacement_scope["adjacentRouteLedgerKeys"][1]["fromPhysicalId"] = "REPLACEMENT_MEAL"
    assert service._same_repair_slot_scope(original_scope, replacement_scope) is True
    physical_neighbor_tampered_scope = json.loads(json.dumps(original_scope))
    physical_neighbor_tampered_scope["adjacentRouteLedgerKeys"][0]["fromPhysicalId"] = (
        "OTHER_CAMPUS"
    )
    assert (
        service._same_repair_slot_scope(original_scope, physical_neighbor_tampered_scope)
        is False
    )
    assert (
        service._route_repair_snapshot_scope_fingerprint(snapshot, transport_mode="transit")
        != service._route_repair_snapshot_scope_fingerprint(
            rebound_snapshot,
            transport_mode="transit",
        )
    )


def test_route_bad_checkpoint_keeps_planned_query_and_rejected_identity_in_exact_scope():
    snapshot = _snapshot()
    own_query_fingerprint = canonical_fingerprint({"query": "meal-own", "scope": "meal_slot"})
    other_query_fingerprint = canonical_fingerprint({"query": "museum-other", "scope": "museum_slot"})
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeQueryFingerprint"] = own_query_fingerprint
    snapshot["days"][0]["segments"][2]["semanticMetadata"]["routeQueryFingerprint"] = other_query_fingerprint
    rejected_candidate = {
        **_poi("same_meal", "同一候选", "food"),
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        # It reaches the route-repair scope, but is rejected before a probe.
        "semanticPassed": False,
    }

    result = PortfolioRouteFeasibilityService(route_service=FakeRouteService()).prepare(
        snapshot,
        candidate_pools=[rejected_candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    checkpoint = result.repair_attempt_ledger["routeBadCheckpoints"][0]
    assert checkpoint["usedQueryFingerprints"] == [own_query_fingerprint]
    assert checkpoint["rejectedCanonicalPhysicalIds"] == ["SAME_MEAL"]
    assert checkpoint["rejectedCandidateAttempts"] == [
        {
            "attempt": 1,
            "candidatePhysicalId": "SAME_MEAL",
            "reason": "grounding_failed",
            "scopeFingerprint": checkpoint["scopeFingerprint"],
        }
    ]
    assert checkpoint["attemptCursor"] == 1
    assert other_query_fingerprint not in checkpoint["usedQueryFingerprints"]


def test_exact_persisted_route_repair_scope_skips_same_provider_matrix_before_reprobe():
    route_service = RecordingRouteBudgetOrderService()
    service = PortfolioRouteFeasibilityService(route_service=route_service)
    first = service.prepare(
        _snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert first.status == "failed"
    calls_after_first_attempt = list(route_service.call_kinds)
    assert calls_after_first_attempt

    retried = service.prepare(
        first.snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert retried.status == "failed"
    assert retried.provider_state == "no_progress"
    assert route_service.call_kinds == calls_after_first_attempt
    assert retried.repair_attempt_ledger["terminalReason"] == "no_progress_same_scope"
    assert retried.repair_attempt_ledger["repairCallCount"] == 0
    assert retried.repair_attempt_ledger["providerCallCount"] == 0
    assert retried.repair_attempt_ledger["nearbySearchCallCount"] == 0
    assert retried.repair_attempt_ledger["nearbyQueryCount"] == 0
    assert retried.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert retried.route_execution_ledger["providerCallCount"] == 0
    assert retried.route_execution_ledger["providerCacheHitCount"] == 0
    assert retried.repair_attempt_ledger["routeBadCheckpoints"] == first.repair_attempt_ledger[
        "routeBadCheckpoints"
    ]
    assert retried.snapshot["portfolioRouteQuality"]["repairAttemptLedger"] == retried.repair_attempt_ledger


def test_incomplete_exact_matrix_replay_is_no_progress_before_provider():
    route_service = RecordingRouteBudgetOrderService()
    service = PortfolioRouteFeasibilityService(route_service=route_service)
    first = service.prepare(
        _snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )
    incomplete = copy.deepcopy(first.snapshot)
    prior_ledger = incomplete["portfolioRouteQuality"]["repairAttemptLedger"]
    prior_ledger["fullProviderMatrixProofFingerprints"] = []
    prior_ledger["completedMatrixCandidateKeys"] = []
    prior_ledger["completedMatrixEvidence"] = []
    for checkpoint in prior_ledger["routeBadCheckpoints"]:
        checkpoint["fullProviderMatrixProofFingerprints"] = []
        checkpoint["candidateEvidence"] = []
    for day in incomplete["days"]:
        for segment in day["segments"]:
            semantic = segment.get("semanticMetadata")
            if isinstance(semantic, dict):
                semantic.pop("routeInsertionMatrixProof", None)
                semantic.pop("routeReplacementMatrixProof", None)
    calls_before_replay = list(route_service.call_kinds)

    replayed = service.prepare(
        incomplete,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert replayed.provider_state == "no_progress"
    assert route_service.call_kinds == calls_before_replay
    assert replayed.repair_attempt_ledger["providerCallCount"] == 0
    assert replayed.repair_attempt_ledger["nearbyQueryCount"] == 0
    assert replayed.repair_attempt_ledger["candidateEvaluationCount"] == 0


def test_exact_persisted_route_repair_scope_allows_new_admitted_canonical_candidate():
    route_service = RecordingRouteBudgetOrderService()
    service = PortfolioRouteFeasibilityService(
        route_service=route_service,
        consumer_admission_service=PassingNearbyAdmissionService(),
    )
    snapshot = _attach_current_consumer_admission(
        _snapshot(),
        segment_id="meal_segment",
        family="meal",
        activity_mode="meal",
        experience_goal="本地特色午餐",
    )
    first = service.prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )
    calls_after_first_attempt = len(route_service.call_kinds)
    admitted_candidate = {
        **_poi("nearby_meal", "相邻小吃店", "food"),
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "consumerAdmissionReport": {"scoreEligible": True},
    }

    retried = service.prepare(
        first.snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
        candidate_pools=[admitted_candidate],
    )

    assert retried.provider_state != "no_progress"
    assert len(route_service.call_kinds) > calls_after_first_attempt
    assert "NEARBY_MEAL" in retried.repair_attempt_ledger["admittedCanonicalPhysicalIds"]
    checkpoint = retried.repair_attempt_ledger["routeBadCheckpoints"][0]
    candidate_evidence = next(
        item
        for item in retried.repair_attempt_ledger["candidateProbeEvidence"]
        if item["candidatePhysicalId"] == "NEARBY_MEAL"
    )
    assert candidate_evidence["routeBadCheckpointScopeFingerprint"] == checkpoint["scopeFingerprint"]
    assert candidate_evidence["repairScopeCertificate"]["continuationMode"] == "repair_exact_slot"
    assert candidate_evidence["repairScopeCertificate"]["candidatePhysicalId"] == "NEARBY_MEAL"
    assert "segmentId" not in candidate_evidence["repairScopeCertificate"]


def test_exact_repair_progress_compares_admission_against_its_checkpoint_scope_only():
    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    snapshot = _snapshot()
    museum_semantic = snapshot["days"][0]["segments"][2]["semanticMetadata"]
    museum_semantic.update(
        {
            "routePreference": {"preferNearAdjacentAnchors": True},
            "sourceGoalId": "goal_museum",
        }
    )
    snapshot["days"][0]["segments"].append(
        {
            "id": "park_segment",
            "startTime": "14:00",
            "endTime": "16:00",
            "kind": "visit",
            "poi": _poi("park", "奥林匹克森林公园", "park"),
            "semanticMetadata": {
                "routeAnchor": True,
                "groundingStatus": "selected",
                "creativeBriefId": "brief_1",
                "poolId": "park_pool",
                "planningSlotId": "park_slot",
            },
        }
    )
    segments = service._snapshot_segments(snapshot)
    meal_segment = next(item for item in segments if item["id"] == "meal_segment")
    museum_segment = next(item for item in segments if item["id"] == "museum_segment")
    meal_checkpoint = service._route_repair_scope_certificate(
        snapshot,
        segment=meal_segment,
        candidate=service._segment_poi_payload(meal_segment),
        transport_mode="transit",
        issue_code="provider_route_matrix_unacceptable",
    )
    museum_checkpoint = service._route_repair_scope_certificate(
        snapshot,
        segment=museum_segment,
        candidate=service._segment_poi_payload(museum_segment),
        transport_mode="transit",
        issue_code="provider_route_matrix_unacceptable",
    )
    assert meal_checkpoint is not None
    assert museum_checkpoint is not None
    fresh_museum_candidate = {
        **_poi("new_museum", "新博物馆", "museum"),
        "briefId": "brief_1",
        "poolId": "museum_pool",
        "planningSlotId": "museum_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_museum",
        "consumerAdmissionReport": {"scoreEligible": True},
    }
    prior = {
        "routeBadCheckpoints": [
            {
                **meal_checkpoint,
                # The ID belongs to the other slot and must not suppress its
                # newly admitted candidate on a resumed repair.
                "admittedCanonicalPhysicalIds": ["NEW_MUSEUM"],
            },
            {**museum_checkpoint, "admittedCanonicalPhysicalIds": []},
        ],
        "admittedCanonicalPhysicalIds": ["NEW_MUSEUM"],
    }

    assert service._has_fresh_exact_repair_evidence(
        snapshot,
        transport_mode="transit",
        candidate_pools=[fresh_museum_candidate],
        prior=prior,
    )


def test_inherited_exact_repair_evidence_recomputes_query_cursors_from_executed_queries():
    """A newly admitted candidate may continue one scope without resetting executed-query state."""

    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    scope_fingerprint = "exact-repair-scope"
    snapshot = {
        "portfolioRouteQuality": {
            "repairAttemptLedger": {
                "routeBadCheckpoints": [
                    {
                        "scopeFingerprint": scope_fingerprint,
                        "executedQueryFingerprints": ["executed-query-one"],
                        "admittedCanonicalPhysicalIds": ["OLD_PHYSICAL"],
                        "queryCursor": 1,
                    }
                ],
                "executedQueryFingerprints": ["executed-query-one"],
                "admittedCanonicalPhysicalIds": ["OLD_PHYSICAL"],
                "candidateProbeCount": 1,
                "candidateEvaluationCount": 1,
                "attemptCursor": 1,
            }
        }
    }
    current_ledger = {
        "routeBadCheckpoints": [
            {
                "scopeFingerprint": scope_fingerprint,
                "executedQueryFingerprints": [],
                "admittedCanonicalPhysicalIds": ["NEW_ADMITTED_PHYSICAL"],
                "queryCursor": 0,
            }
        ],
        "executedQueryFingerprints": [],
        "admittedCanonicalPhysicalIds": ["NEW_ADMITTED_PHYSICAL"],
        "candidateProbeCount": 0,
        "candidateEvaluationCount": 0,
        "attemptCursor": 0,
    }

    service._inherit_matching_repair_evidence(snapshot, current_ledger)

    assert current_ledger["executedQueryFingerprints"] == ["executed-query-one"]
    assert current_ledger["queryCursor"] == 1
    assert current_ledger["routeBadCheckpoints"][0]["executedQueryFingerprints"] == [
        "executed-query-one"
    ]
    assert current_ledger["routeBadCheckpoints"][0]["queryCursor"] == 1
    assert set(current_ledger["admittedCanonicalPhysicalIds"]) == {
        "OLD_PHYSICAL",
        "NEW_ADMITTED_PHYSICAL",
    }


def test_incomplete_provider_matrix_does_not_increment_candidate_evaluation():
    ledger = {
        "candidateEvaluationCount": 0,
        "completedMatrixCandidateKeys": [],
        "completedMatrixEvidence": [],
        "fullProviderMatrixProofFingerprints": [],
        "admittedCanonicalPhysicalIds": [],
        "routeBadCheckpoints": [],
    }

    PortfolioRouteFeasibilityService(route_service=FakeRouteService())._record_completed_candidate_matrix(
        ledger,
        probe={"candidateProbeKey": "probe_without_full_matrix"},
        candidate={
            **_poi("nearby_meal", "相邻小吃店", "food"),
            "consumerAdmissionReport": {"scoreEligible": True},
        },
        matrix_proof={"networkVerified": True, "candidateLegs": []},
    )

    assert ledger["candidateEvaluationCount"] == 0
    assert ledger["completedMatrixCandidateKeys"] == []
    assert ledger["fullProviderMatrixProofFingerprints"] == []
    assert ledger["admittedCanonicalPhysicalIds"] == []


def test_exact_persisted_route_repair_scope_does_not_unlock_from_unadmitted_direction():
    route_service = RecordingRouteBudgetOrderService()
    service = PortfolioRouteFeasibilityService(route_service=route_service)
    first = service.prepare(
        _snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )
    calls_after_first_attempt = list(route_service.call_kinds)
    direction_only_candidate = {
        **_poi("nearby_meal", "相邻小吃店", "food"),
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "directionSignature": "fresh_heritage_walk",
    }

    retried = service.prepare(
        first.snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
        candidate_pools=[direction_only_candidate],
    )

    assert retried.provider_state == "no_progress"
    assert route_service.call_kinds == calls_after_first_attempt
    assert retried.repair_attempt_ledger["terminalReason"] == "no_progress_same_scope"


@pytest.mark.parametrize(
    ("missing", "failure_code"),
    [
        ("detourTolerance", "provider_insertion_detour_tolerance_missing"),
        ("mobilityProfile", "provider_insertion_mobility_profile_missing"),
    ],
)
def test_meal_provider_decision_fails_closed_without_explicit_policy(missing, failure_code):
    snapshot = _snapshot()
    if missing == "detourTolerance":
        contract = snapshot["routeDecisionContract"]
        contract.pop("detourTolerance")
    else:
        contract = snapshot["routeDecisionContract"]
        contract.pop("mobilityProfile")
    route_service = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.provider_state == "precondition_failed"
    assert [item["code"] for item in result.route_quality_issues] == [failure_code]
    assert route_service.requested_pairs == []


def _seven_anchor_snapshot() -> dict:
    days = []
    segment_index = 0
    for day_number, anchor_count in ((1, 4), (2, 3)):
        segments = []
        for day_index in range(anchor_count):
            segment_index += 1
            start_hour = 9 + day_index * 2
            segments.append(
                {
                    "id": f"segment_{segment_index}",
                    "startTime": f"{start_hour:02d}:00",
                    "endTime": f"{start_hour + 1:02d}:00",
                    "kind": "visit",
                    "poi": _poi(
                        f"poi_{segment_index}",
                        f"地点 {segment_index}",
                        "campus",
                    ),
                    "semanticMetadata": {
                        "routeAnchor": True,
                        "groundingStatus": "selected",
                        "creativeBriefId": "brief_route_retry",
                        "poolId": f"pool_{segment_index}",
                        "planningSlotId": f"slot_{segment_index}",
                    },
                }
            )
        days.append({"dayNumber": day_number, "segments": segments})
    return {
        "id": "plan_route_retry",
        "portfolioTransportPreference": "transit",
        "portfolioGroundingFocusBriefId": "brief_route_retry",
        "days": days,
    }


def test_long_absolute_leg_is_not_rejected_when_provider_insertion_delta_is_favorable():
    result = PortfolioRouteFeasibilityService(route_service=FakeInsertionFriendlyRouteService()).prepare(
        _snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "passed"
    assert result.route_quality_issues == []
    assert max(item["distanceMeters"] for item in result.route_evidence) == 13_570


@pytest.mark.parametrize("failure_mode", ["empty", "raise", "invalid"])
def test_internal_meal_without_valid_bypass_evidence_stays_pending(failure_mode):
    snapshot = _snapshot()

    result = PortfolioRouteFeasibilityService(
        route_service=FakeUnavailableMealBypassRouteService(failure_mode)
    ).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "pending"
    assert result.provider_state == "route_missing"
    missing = next(
        item
        for item in result.route_quality_issues
        if item.get("code") == "route_evidence_missing" and item.get("evidenceKind") == "meal_bypass"
    )
    assert missing["mealSegmentId"] == "meal_segment"
    assert missing["fromSegmentId"] == "campus_segment"
    assert missing["toSegmentId"] == "museum_segment"
    assert "portfolioRouteEvidence" not in snapshot


def test_preferred_transit_matrix_does_not_preflight_walking_when_transit_is_available():
    route_service = RecordingRouteBudgetOrderService()

    PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        _snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert route_service.call_kinds[:2] == [
        "adjacent_baseline",
        "meal_bypass",
    ]


def test_missing_transit_authorizes_walking_only_after_the_exact_preferred_pair_fails(monkeypatch):
    class LocalAmapResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return self.payload

    requested_modes = []

    def fake_urlopen(url: str, timeout: float):
        if "/v3/direction/transit/integrated" in url:
            requested_modes.append("transit")
            return LocalAmapResponse(b'{"status":"1","route":{"transits":[]}}')
        if "/v3/direction/walking" in url:
            requested_modes.append("walking")
            return LocalAmapResponse(
                b'{"status":"1","route":{"paths":[{"distance":"900","duration":"650","steps":[{"instruction":"walk","distance":"900","duration":"650","polyline":"116.30,39.90;116.31,39.91"}]}]}}'
            )
        raise AssertionError(f"unexpected route request: {url}")

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    snapshot = {
        "id": "conditional_walking_route_budget",
        "portfolioTransportPreference": "transit",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "conditional_walk_left",
                        "kind": "visit",
                        "startTime": "09:00",
                        "endTime": "10:00",
                        "poi": _poi("B000000101", "起点", "campus"),
                        "semanticMetadata": {"routeAnchor": True},
                    },
                    {
                        "id": "conditional_walk_right",
                        "kind": "visit",
                        "startTime": "11:00",
                        "endTime": "12:00",
                        "poi": _poi("B000000102", "终点", "campus"),
                        "semanticMetadata": {"routeAnchor": True},
                    },
                ],
            }
        ],
    }
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    RouteService.clear_cache()
    try:
        with amap_call_budget_scope(budget):
            result = PortfolioRouteFeasibilityService(
                route_service=RouteService(map_provider_key="amap-key")
            ).prepare(
                snapshot,
                city="北京",
                transport_mode="transit",
                allow_nearby_search=False,
            )
    finally:
        RouteService.clear_cache()

    budget_snapshot = budget.snapshot()
    assert result.passed
    assert requested_modes == ["transit", "walking"]
    assert budget.route_refresh_max == 2
    assert budget.place_around_max == 0
    assert budget.total_external_max == 2
    assert budget_snapshot["usedRoute"] == 2
    assert budget_snapshot["derivation"] == {
        "schemaVersion": "creative-portfolio-route-budget-v1",
        "preferredMode": "transit",
        "baselineAdjacentPairCount": 1,
        "insertionBypassPairCount": 0,
        "replacementCandidateCount": 0,
        "replacementPreferredPairCount": 0,
        "conditionalWalkingPairCount": 1,
        "themeWalkingPairCount": 0,
        "nearbySearchMax": 0,
        "routeRequests": [
            {
                "fromPhysicalId": "B000000101",
                "toPhysicalId": "B000000102",
                "mode": "transit",
                "reason": "baseline_adjacent",
                "condition": "always",
            },
            {
                "fromPhysicalId": "B000000101",
                "toPhysicalId": "B000000102",
                "mode": "walking",
                "reason": "conditional_walking",
                "condition": "preferred_mode_unavailable",
            },
        ],
        "routeWorkLeaseCount": 2,
        "routeWorkLeases": [
            {
                "fromPhysicalId": "B000000101",
                "toPhysicalId": "B000000102",
                "mode": "transit",
                "leaseKind": "unscoped",
            },
            {
                "fromPhysicalId": "B000000101",
                "toPhysicalId": "B000000102",
                "mode": "walking",
                "leaseKind": "unscoped",
            },
        ],
    }


def test_exact_repair_scope_re_signs_walking_only_after_scoped_transit_is_unavailable(
    monkeypatch,
):
    class LocalAmapResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return self.payload

    requested_modes = []

    def fake_urlopen(url: str, timeout: float):
        if "/v3/direction/transit/integrated" in url:
            requested_modes.append("transit")
            return LocalAmapResponse(b'{"status":"1","route":{"transits":[]}}')
        if "/v3/direction/walking" in url:
            requested_modes.append("walking")
            return LocalAmapResponse(
                b'{"status":"1","route":{"paths":[{"distance":"900","duration":"650","steps":[{"instruction":"walk","distance":"900","duration":"650","polyline":"116.30,39.90;116.31,39.91"}]}]}}'
            )
        raise AssertionError(f"unexpected route request: {url}")

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    snapshot = {
        "id": "exact_scoped_conditional_walking",
        "planningSelectionRootTurnId": "planning_root_turn_test",
        "portfolioTransportPreference": "transit",
        "routeDecisionContract": _route_decision_contract(),
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "conditional_walk_left",
                        "kind": "visit",
                        "startTime": "09:00",
                        "endTime": "10:00",
                        "poi": _poi("B000000101", "起点", "campus"),
                        "semanticMetadata": {"routeAnchor": True},
                    },
                    {
                        "id": "conditional_walk_right",
                        "kind": "visit",
                        "startTime": "11:00",
                        "endTime": "12:00",
                        "poi": _poi("B000000102", "终点", "campus"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "creativeBriefId": "brief_1",
                            "poolId": "pool_1",
                            "planningSlotId": "slot_1",
                        },
                    },
                ],
            }
        ],
    }
    service = PortfolioRouteFeasibilityService(
        route_service=RouteService(map_provider_key="amap-key")
    )
    target = next(
        item for item in service._snapshot_segments(snapshot) if item["id"] == "conditional_walk_right"
    )
    transit_scope = service._route_repair_scope_certificate(
        snapshot,
        segment=target,
        candidate=service._segment_poi_payload(target),
        transport_mode="transit",
    )
    assert transit_scope is not None
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    RouteService.clear_cache()
    try:
        with amap_call_budget_scope(budget):
            result = service._evaluate_snapshot(
                snapshot,
                city="北京",
                transport_mode="transit",
                preview_id="exact_scoped_conditional_walking",
                repair_scope_certificate=transit_scope,
            )
    finally:
        RouteService.clear_cache()

    calls = budget.snapshot()["calls"]
    assert result["status"] == "passed"
    assert requested_modes == ["transit", "walking"]
    assert [call["mode"] for call in calls] == ["transit", "walking"]
    assert calls[0]["repairScopeCertificate"] == transit_scope
    walking_scope = calls[1]["repairScopeCertificate"]
    assert walking_scope["scopeFingerprint"] != transit_scope["scopeFingerprint"]
    assert walking_scope["adjacentRouteLedgerKeys"][0]["mode"] == "walking"
    for field in (
        "planningSelectionRootTurnId",
        "briefId",
        "dayNumber",
        "planningSlotId",
        "candidatePhysicalId",
        "adjacentAnchorIds",
        "routeContractFingerprint",
    ):
        assert walking_scope[field] == transit_scope[field]


def test_recorded_transit_matrix_does_not_prefetch_walking_and_has_zero_repair_calls():
    """Use the hash-bound, non-live AMap capture through the production parser."""

    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        snapshot = {
            "id": "recorded_transit_route_ledger",
            "portfolioTransportPreference": "transit",
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "recorded_tsinghua",
                            "kind": "visit",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": _poi_payload(tsinghua),
                            "semanticMetadata": {"routeAnchor": True},
                        },
                        {
                            "id": "recorded_pku",
                            "kind": "visit",
                            "startTime": "11:00",
                            "endTime": "12:00",
                            "poi": _poi_payload(pku),
                            "semanticMetadata": {"routeAnchor": True},
                        },
                    ],
                }
            ],
        }
        result = PortfolioRouteFeasibilityService(route_service=RouteService()).prepare(
            snapshot,
            city="北京",
            transport_mode="transit",
            allow_nearby_search=False,
        )

    assert result.passed
    assert result.repair_attempt_ledger["repairCallCount"] == 0
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
        "/v3/direction/transit/integrated",
    ]


def test_recorded_transit_pair_derives_route_budget_from_exact_pairs_and_conditional_modes():
    """The route envelope is a topology lease, not a fixed eight-call allowance."""

    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        snapshot = {
            "id": "recorded_transit_derived_budget",
            "portfolioTransportPreference": "transit",
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "recorded_tsinghua",
                            "kind": "visit",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": _poi_payload(tsinghua),
                            "semanticMetadata": {"routeAnchor": True},
                        },
                        {
                            "id": "recorded_pku",
                            "kind": "visit",
                            "startTime": "11:00",
                            "endTime": "12:00",
                            "poi": _poi_payload(pku),
                            "semanticMetadata": {"routeAnchor": True},
                        },
                    ],
                }
            ],
        }
        service = PortfolioRouteFeasibilityService(route_service=RouteService())
        budget = AmapCallBudget.for_creative_portfolio_route_preflight()
        with amap_call_budget_scope(budget):
            result = service.prepare(
                snapshot,
                city="北京",
                transport_mode="transit",
                allow_nearby_search=False,
            )

    assert result.passed
    budget_after = budget.snapshot()
    assert budget.route_refresh_max == 1
    assert budget.place_around_max == 0
    assert budget.total_external_max == 1
    assert budget_after["derivation"] == {
        "schemaVersion": "creative-portfolio-route-budget-v1",
        "preferredMode": "transit",
        "baselineAdjacentPairCount": 1,
        "insertionBypassPairCount": 0,
        "replacementCandidateCount": 0,
        "replacementPreferredPairCount": 0,
        "conditionalWalkingPairCount": 0,
        "themeWalkingPairCount": 0,
        "nearbySearchMax": 0,
        "routeRequests": [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
                "reason": "baseline_adjacent",
                "condition": "always",
            }
        ],
        "routeWorkLeaseCount": 1,
        "routeWorkLeases": [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
                "leaseKind": "unscoped",
            }
        ],
    }
    assert budget_after["usedRoute"] == 1
    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
        "/v3/direction/transit/integrated",
    ]


def test_recorded_transit_matrix_budget_exhaustion_is_not_route_quality_rejection():
    """A hash-bound replay must preserve budget exhaustion as a recoverable gap."""
    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        snapshot = {
            "id": "recorded_transit_route_budget_exhaustion",
            "portfolioTransportPreference": "transit",
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "recorded_tsinghua",
                            "kind": "visit",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": _poi_payload(tsinghua),
                            "semanticMetadata": {"routeAnchor": True},
                        },
                        {
                            "id": "recorded_pku",
                            "kind": "visit",
                            "startTime": "11:00",
                            "endTime": "12:00",
                            "poi": _poi_payload(pku),
                            "semanticMetadata": {"routeAnchor": True},
                        },
                    ],
                }
            ],
        }
        budget = AmapCallBudget(
            place_text_max=0,
            place_around_max=0,
            route_refresh_max=0,
            total_external_max=0,
            source="recorded-route-budget-exhaustion",
        )
        with amap_call_budget_scope(budget):
            result = PortfolioRouteFeasibilityService(route_service=RouteService()).prepare(
                snapshot,
                city="北京",
                transport_mode="transit",
                allow_nearby_search=False,
            )

    assert result.status == "pending"
    assert [item.get("code") for item in result.route_quality_issues] == ["route_budget_exhausted"]
    assert result.provider_state == "budget_exhausted"
    assert result.route_execution_ledger["expectedLegCount"] == 1
    assert result.route_execution_ledger["providerCallCount"] == 0
    assert result.route_execution_ledger["verifiedLegCount"] == 0
    assert result.route_execution_ledger["budgetExhausted"] is True
    assert any(item["code"] == "route_budget_exhausted" for item in result.route_quality_issues)
    assert all(item["code"] != "provider_route_matrix_unacceptable" for item in result.route_quality_issues)
    assert budget.snapshot()["usedRoute"] == 0
    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
    ]


def test_recorded_nearby_candidate_is_consumer_admitted_before_candidate_matrix_probe():
    """A rejected recorded nearby POI must never reach its candidate route matrix."""

    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    admission = ConsumerCandidateAdmissionService()
    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        source_input = admission.build_consumer_context(
            brief_id="recorded-brief",
            pool_id="recorded-meal-pool",
            planning_slot_id="recorded-pku-meal",
            day_number=1,
            city="北京",
            family="meal",
            activity_mode="meal",
            requirement_level="soft",
            experience_shape="single_poi",
            experience_goal="改为去清华学校里面的食堂吃",
            exact_entity="北京烤鸭",
        )
        snapshot = {
            "id": "recorded-nearby-admission-before-provider",
            "planningSelectionRootTurnId": "recorded-planning-root",
            "portfolioTransportPreference": "transit",
            "routeDecisionContract": _route_decision_contract(),
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "recorded-tsinghua",
                            "kind": "visit",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": _poi_payload(tsinghua),
                            "semanticMetadata": {
                                "routeAnchor": True,
                                "groundingStatus": "selected",
                                "creativeBriefId": "recorded-brief",
                                "poolId": "recorded-campus-pool",
                                "planningSlotId": "recorded-tsinghua-campus",
                            },
                        },
                        {
                            "id": "recorded-pku",
                            "kind": "meal",
                            "startTime": "11:00",
                            "endTime": "12:00",
                            "poi": _poi_payload(pku),
                            "semanticMetadata": {
                                "routeAnchor": True,
                                "routePreference": {"preferNearAdjacentAnchors": True},
                                "groundingStatus": "selected",
                                "creativeBriefId": "recorded-brief",
                                "poolId": "recorded-meal-pool",
                                "planningSlotId": "recorded-pku-meal",
                                "sourceGoalId": "recorded-meal",
                                "intentType": "meal",
                                "rawNeed": "改为去清华学校里面的食堂吃",
                                "consumerAdmissionInput": source_input,
                            },
                        },
                    ],
                }
            ],
        }
        service = PortfolioRouteFeasibilityService(route_service=RouteService())
        baseline = service.prepare(
            snapshot,
            city="北京",
            transport_mode="transit",
            allow_nearby_search=False,
        )
        # The recorded transit leg is intentionally retained as the already
        # evaluated baseline; its quality outcome is irrelevant to the narrow
        # candidate-admission boundary below.
        assert baseline.route_evidence
        replay.requests.clear()
        source_segment = next(
            segment
            for segment in service._snapshot_segments(baseline.snapshot)
            if segment["id"] == "recorded-pku"
        )
        call_metrics = {
            "routeCallCount": 0,
            "nearbySearchCount": 0,
            "expectedLegCount": 0,
            "cachedLegCount": 0,
            "requestedLegCount": 0,
            "completedLegCount": 0,
            "verifiedLegCount": 0,
            "failedLegCount": 0,
            "providerCallCount": 0,
            "providerCacheHitCount": 0,
        }
        replacement = service._find_nearby_replacement(
            baseline.snapshot,
            source_segment,
            city="北京",
            transport_mode="transit",
            preview_id="recorded-nearby-admission",
            call_metrics=call_metrics,
            repair_ledger=baseline.repair_attempt_ledger,
        )

    ledger = baseline.repair_attempt_ledger
    assert replacement is None
    assert ledger["candidateProbeCount"] == 0
    assert ledger["candidateEvaluationCount"] == 0
    assert ledger["completedMatrixCandidateKeys"] == []
    assert ledger["completedMatrixEvidence"] == []
    assert ledger["fullProviderMatrixProofFingerprints"] == []
    assert ledger["rejectedReasonCounts"]["consumer_admission_rejected"] >= 1
    assert replay.requests
    assert all(item["endpoint"] == "/v3/place/around" for item in replay.requests)
    assert not any(item["endpoint"].startswith("/v3/direction/") for item in replay.requests)


def _poi_payload(poi: POI) -> dict:
    return {
        "id": poi.id,
        "amapId": poi.id,
        "name": poi.name,
        "city": poi.city,
        "category": poi.category,
        "type": poi.type,
        "providerType": poi.type,
        "longitude": poi.longitude,
        "latitude": poi.latitude,
        "source": poi.source,
        "confidence": poi.confidence,
    }


def test_nearby_same_lineage_candidate_repairs_route_and_projects_buffered_times():
    snapshot = _snapshot()
    admission_input = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id="brief_1",
        pool_id="meal_pool",
        planning_slot_id="meal_slot",
        day_number=1,
        city="北京",
        family="meal",
        activity_mode="meal",
        requirement_level="soft",
        experience_shape="single_poi",
        experience_goal="本地特色午餐",
    )
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["consumerAdmissionInput"] = admission_input

    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=FakeMapPoiService(),
        consumer_admission_service=PassingNearbyAdmissionService(),
    ).prepare(snapshot, city="北京", transport_mode="transit")

    assert result.status == "passed"
    assert result.route_call_count >= 2
    assert result.nearby_search_count >= 1
    assert result.candidate_repairs[0]["candidateId"] == "nearby_meal"
    assert result.candidate_repairs[0]["groundingEvidence"] == {
        "creativeBriefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "amapId": "nearby_meal",
        "source": "amap-place-search",
        "longitude": 116.301,
        "latitude": 39.999,
    }
    assert result.snapshot["days"][0]["segments"][1]["poi"]["amapId"] == "nearby_meal"
    nearby_proof = result.snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeReplacementMatrixProof"]
    assert nearby_proof["basis"] == "old_two_leg_vs_candidate_two_leg_delta"
    assert len(nearby_proof["baselineLegs"]) == len(nearby_proof["candidateLegs"]) == 2
    assert nearby_proof["networkVerified"] is True
    assert len(result.route_evidence) == 2
    assert result.snapshot["days"][0]["segments"][1]["startTime"] == "11:20"
    assert result.snapshot["days"][0]["segments"][2]["startTime"] == "12:40"
    assert result.snapshot["portfolioScheduleProjection"]["usesVerifiedRouteEvidence"] is True
    replacement_semantic = result.snapshot["days"][0]["segments"][1]["semanticMetadata"]
    assert replacement_semantic["consumerAdmissionReport"]["classification"] == "admitted_final_anchor"
    assert replacement_semantic["consumerAdmissionInput"] == admission_input


def test_nearby_replacement_does_not_reuse_stale_admission_report_without_input():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["consumerAdmissionReport"] = {
        "consumerFingerprint": "stale-meal-report",
        "scoreEligible": True,
    }

    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=FakeMapPoiService(),
    ).prepare(snapshot, city="北京", transport_mode="transit")

    assert result.status == "failed"
    assert result.candidate_repairs == []
    assert result.repair_attempt_ledger["candidateProbeCount"] == 0
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert result.repair_attempt_ledger["rejectedReasonCounts"]["consumer_admission_context_missing"] >= 1


def test_nearby_replacement_rejects_cross_slot_admission_input_before_probe():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["consumerAdmissionInput"] = (
        ConsumerCandidateAdmissionService.build_consumer_context(
            brief_id="brief_1",
            pool_id="meal_pool",
            planning_slot_id="another_meal_slot",
            day_number=1,
            city="北京",
            family="meal",
            activity_mode="meal",
            requirement_level="soft",
            experience_shape="single_poi",
            experience_goal="本地特色午餐",
        )
    )

    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=FakeMapPoiService(),
    ).prepare(snapshot, city="北京", transport_mode="transit")

    assert result.status == "failed"
    assert result.candidate_repairs == []
    assert result.repair_attempt_ledger["candidateProbeCount"] == 0
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert result.repair_attempt_ledger["rejectedReasonCounts"]["consumer_admission_scope_mismatch"] >= 1


def test_nearby_replacement_fails_closed_when_admission_evaluation_errors():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["consumerAdmissionInput"] = (
        ConsumerCandidateAdmissionService.build_consumer_context(
            brief_id="brief_1",
            pool_id="meal_pool",
            planning_slot_id="meal_slot",
            day_number=1,
            city="北京",
            family="meal",
            activity_mode="meal",
            requirement_level="soft",
            experience_shape="single_poi",
            experience_goal="本地特色午餐",
        )
    )

    class RaisingNearbyAdmissionService:
        @staticmethod
        def evaluate(_candidate, _consumer):
            raise RuntimeError("admission evaluator unavailable")

    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=FakeMapPoiService(),
        consumer_admission_service=RaisingNearbyAdmissionService(),
    ).prepare(snapshot, city="北京", transport_mode="transit")

    assert result.status == "failed"
    assert result.candidate_repairs == []
    assert result.repair_attempt_ledger["candidateProbeCount"] == 0
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert result.repair_attempt_ledger["rejectedReasonCounts"]["consumer_admission_evaluation_failed"] >= 1


def test_incremental_repair_does_not_block_one_day_on_another_days_route_issue():
    unrelated_issue = {
        "code": "provider_route_matrix_unacceptable",
        "mealSegmentId": "day_2_meal",
    }
    own_issue = {
        "code": "provider_route_matrix_unacceptable",
        "mealSegmentId": "day_1_meal",
    }

    assert not PortfolioRouteFeasibilityService._has_issue_for_segment(
        [unrelated_issue], "day_1_meal"
    )
    assert PortfolioRouteFeasibilityService._has_issue_for_segment(
        [unrelated_issue, own_issue], "day_1_meal"
    )


def test_same_lineage_pool_candidate_repairs_route_without_nearby_fallback():
    snapshot = _attach_current_consumer_admission(
        _snapshot(),
        segment_id="meal_segment",
        family="meal",
        activity_mode="meal",
        experience_goal="本地特色午餐",
    )
    candidate = {
        **_poi("nearby_meal", "相邻小吃店", "food"),
        "longitude": 116.301,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "semanticPassed": True,
        "consumerAdmissionReport": {"scoreEligible": True},
        "routeContract": {
            "detourTolerance": {
                "maxGeneralizedCostDelta": 12,
                "maxDetourRatio": 0.5,
            },
            "mobilityProfile": {
                "source": "test_accessibility_profile",
                "walkingPenaltyMinutesPerKm": 3,
            },
        },
    }

    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        consumer_admission_service=PassingNearbyAdmissionService(),
    ).prepare(
        snapshot,
        candidate_pools=[candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "passed"
    assert result.route_call_count >= 2
    assert result.nearby_search_count == 0
    assert result.candidate_repairs[0]["candidateId"] == "nearby_meal"
    assert result.candidate_repairs[0]["source"] == "portfolio_candidate"
    assert result.snapshot["days"][0]["segments"][1]["poi"]["amapId"] == "nearby_meal"
    pool_proof = result.snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeReplacementMatrixProof"]
    assert pool_proof["basis"] == "old_two_leg_vs_candidate_two_leg_delta"
    assert pool_proof["generalizedCostDelta"] < 0
    assert pool_proof["detourTolerance"] == {
        "maxGeneralizedCostDelta": 35.0,
        "maxDetourRatio": 0.35,
    }
    assert pool_proof["mobilityProfile"] == _explicit_mobility_profile()
    assert pool_proof["routeDecisionContractSource"] == "request_intent_contract"
    assert pool_proof["contractFingerprint"] == snapshot["routeDecisionContract"]["fingerprint"]
    assert result.repair_attempt_ledger["phaseEntered"] is True
    assert result.repair_attempt_ledger["candidateEvaluationCount"] >= 1
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == len(
        result.repair_attempt_ledger["completedMatrixCandidateKeys"]
    )
    assert result.repair_attempt_ledger["acceptedReplacementCount"] == 1
    assert result.repair_attempt_ledger["terminalReason"] == "replacement_accepted"


def test_meal_replacement_invalidates_only_adjacent_route_legs_before_retry():
    snapshot = _attach_current_consumer_admission(
        _snapshot(),
        segment_id="meal_segment",
        family="meal",
        activity_mode="meal",
        experience_goal="本地特色午餐",
    )
    snapshot["days"][0]["segments"].append(
        {
            "id": "park_segment",
            "startTime": "15:00",
            "endTime": "17:00",
            "kind": "visit",
            "poi": _poi("park", "奥林匹克森林公园", "park"),
            "semanticMetadata": {
                "routeAnchor": True,
                "groundingStatus": "selected",
                "creativeBriefId": "brief_1",
                "poolId": "park_pool",
                "planningSlotId": "park_slot",
            },
        }
    )
    candidate = {
        **_poi("nearby_meal", "相邻小吃店", "food"),
        "longitude": 116.301,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "semanticPassed": True,
        "consumerAdmissionReport": {"scoreEligible": True},
    }
    route_service = RecordingReplacementRouteService()

    result = PortfolioRouteFeasibilityService(
        route_service=route_service,
        consumer_admission_service=PassingNearbyAdmissionService(),
    ).prepare(
        snapshot,
        candidate_pools=[candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "passed"
    adjacent_batches = [
        pairs for plan_id, pairs in route_service.requested_batches if not plan_id.endswith("_meal_bypass")
    ]
    assert adjacent_batches[0] == [
        ("campus_segment", "meal_segment"),
        ("meal_segment", "museum_segment"),
        ("museum_segment", "park_segment"),
    ]
    assert adjacent_batches[-1] == [
        ("campus_segment", "meal_segment"),
        ("meal_segment", "museum_segment"),
    ]
    assert all(item["fromPoiId"] != "detouring_meal" for item in result.route_evidence)
    assert all(item["toPoiId"] != "detouring_meal" for item in result.route_evidence)
    assert result.repair_attempt_ledger["touchedRoutePairs"] == [
        ["campus_segment", "meal_segment"],
        ["meal_segment", "museum_segment"],
    ]
    assert ["museum_segment", "park_segment"] in result.repair_attempt_ledger["reusedRoutePairs"]


def test_latest_start_failure_uses_bounded_optional_replacement_without_relaxing_window():
    snapshot = _snapshot(meal_id="nearby_meal")
    snapshot["days"][0]["segments"].append(
        {
            "id": "park_segment",
            "startTime": "15:00",
            "endTime": "17:00",
            "kind": "visit",
            "poi": _poi("park", "奥林匹克森林公园", "park"),
            "semanticMetadata": {
                "routeAnchor": True,
                "groundingStatus": "selected",
                "creativeBriefId": "brief_1",
                "poolId": "park_pool",
                "planningSlotId": "park_slot",
            },
        }
    )
    snapshot["days"][0]["segments"][2]["semanticMetadata"]["scheduleConstraints"] = {
        "latestStart": "12:35",
        "hard": True,
    }
    snapshot["days"][0]["segments"][2]["semanticMetadata"]["sourceGoalId"] = "goal_museum"
    _attach_current_consumer_admission(
        snapshot,
        segment_id="museum_segment",
        family="museum",
        activity_mode="visit",
        experience_goal="邻近文化空间",
    )
    meal_candidate = {
        # Same physical identity with a different spelling must not consume a
        # schedule-repair attempt or a Provider probe.
        **_poi("NEARBY_MEAL", "相邻小吃店", "food"),
        "longitude": 116.301,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "semanticPassed": True,
        "consumerAdmissionReport": {"scoreEligible": True},
        "consumerAdmissionReport": {"scoreEligible": True},
    }
    optional_candidate = {
        **_poi("nearby_museum", "邻近文化空间", "museum"),
        "longitude": 116.302,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "museum_pool",
        "planningSlotId": "museum_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_museum",
        "semanticPassed": True,
        "consumerAdmissionReport": {"scoreEligible": True},
    }
    route_service = RecordingReplacementRouteService()

    result = PortfolioRouteFeasibilityService(
        route_service=route_service,
        consumer_admission_service=PassingNearbyAdmissionService(),
    ).prepare(
        snapshot,
        candidate_pools=[meal_candidate, optional_candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "passed"
    repaired_museum = next(item for item in result.snapshot["days"][0]["segments"] if item["id"] == "museum_segment")
    assert repaired_museum["poi"]["amapId"] == "nearby_museum", result.candidate_repairs
    assert repaired_museum["startTime"] == "12:31"
    schedule_proof = repaired_museum["semanticMetadata"]["routeReplacementMatrixProof"]
    assert schedule_proof["basis"] == "old_two_leg_vs_candidate_two_leg_delta"
    assert len(schedule_proof["baselineLegs"]) == len(schedule_proof["candidateLegs"]) == 2
    assert repaired_museum["semanticMetadata"]["scheduleConstraints"] == {
        "latestStart": "12:35",
        "hard": True,
    }
    schedule_repairs = [
        item for item in result.candidate_repairs if item.get("reason") == "schedule_window_latest_start_repair"
    ]
    assert len(schedule_repairs) == 1
    assert schedule_repairs[0]["segmentId"] == "museum_segment"
    assert schedule_repairs[0]["attempt"] == 1
    assert result.repair_attempt_ledger["candidateProbeCount"] == 1
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 1
    assert result.repair_attempt_ledger["completedMatrixEvidence"][0][
        "consumerAdmissionPassed"
    ] is True
    affected_receipts = [
        item
        for item in route_service.scope_receipts
        if item["planId"].endswith("_affected_insertion_bypass")
    ]
    assert len(affected_receipts) == 1
    assert affected_receipts[0]["pairs"] == [
        ("campus_segment", "museum_segment")
    ]
    assert affected_receipts[0]["mode"] == "transit"
    assert affected_receipts[0]["repairScopeCertificate"]["planningSlotId"] == (
        "meal_slot"
    )
    assert affected_receipts[0]["repairScopeCertificate"]["candidatePhysicalId"] == (
        "NEARBY_MEAL"
    )
    assert all(item["mode"] != "walking" for item in route_service.scope_receipts)


def test_unadmitted_schedule_candidate_is_rejected_before_probe_or_provider():
    service = PortfolioRouteFeasibilityService(
        route_service=RecordingReplacementRouteService()
    )
    ledger = {
        "candidateProbeCount": 0,
        "candidateProbeKeys": [],
        "candidateProbeEvidence": [],
        "rejectedReasonCounts": {},
        "rejectedCanonicalPhysicalIds": [],
        "rejectedCandidateAttempts": [],
        "attemptCursor": 0,
        "routeBadCheckpoints": [],
    }
    candidate = {
        **_poi("nearby_museum", "未准入文化空间", "museum"),
        "briefId": "brief_1",
        "poolId": "museum_pool",
        "planningSlotId": "museum_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_museum",
    }

    probe = service._reserve_replacement_candidate(
        ledger,
        snapshot=_snapshot(meal_id="nearby_meal"),
        segment_id="museum_segment",
        candidate=candidate,
        city="北京",
        transport_mode="transit",
    )

    assert probe is None
    assert ledger["candidateProbeCount"] == 0
    assert ledger["candidateProbeEvidence"] == []
    assert ledger["rejectedReasonCounts"] == {"consumer_admission_context_missing": 1}
    assert service.route_service.requested_pairs == []


def test_route_quality_repair_does_not_replace_a_user_locked_meal_anchor():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][1]["userLocked"] = True
    candidate = {
        **_poi("nearby_meal", "相邻小吃店", "food"),
        "longitude": 116.301,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_meal",
        "semanticPassed": True,
    }

    result = PortfolioRouteFeasibilityService(route_service=FakeRouteService()).prepare(
        snapshot,
        candidate_pools=[candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.candidate_repairs == []
    assert result.snapshot["days"][0]["segments"][1]["poi"]["amapId"] == "detouring_meal"
    assert result.repair_attempt_ledger["phaseEntered"] is True
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert result.repair_attempt_ledger["terminalReason"] == "no_eligible_segments"


def test_schedule_repair_does_not_replace_or_reorder_a_night_view_anchor():
    snapshot = _snapshot("nearby_meal")
    night_segment = snapshot["days"][0]["segments"][2]
    night_segment["semanticMetadata"].update(
        {
            "intentType": "night_view",
            "sourceGoalId": "goal_night",
            "scheduleConstraints": {"latestStart": "12:05", "hard": True},
        }
    )
    candidate = {
        **_poi("nearby_night", "邻近夜景点", "museum"),
        "longitude": 116.302,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "museum_pool",
        "planningSlotId": "museum_slot",
        "dayNumber": 1,
        "sourceGoalId": "goal_night",
        "semanticPassed": True,
    }

    result = PortfolioRouteFeasibilityService(route_service=RecordingReplacementRouteService()).prepare(
        snapshot,
        candidate_pools=[candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "pending"
    assert result.candidate_repairs == []
    segments = result.snapshot["days"][0]["segments"]
    assert [item["id"] for item in segments] == [
        "campus_segment",
        "meal_segment",
        "museum_segment",
    ]
    assert segments[2]["poi"]["amapId"] == "museum"


def test_schedule_reorder_without_multi_scope_certificate_fails_before_provider():
    snapshot = _snapshot("nearby_meal")
    snapshot["days"][0]["segments"][2]["semanticMetadata"].update(
        {
            "sourceGoalId": "goal_museum",
            "scheduleConstraints": {"latestStart": "12:05", "hard": True},
        }
    )
    route_service = RecordingReplacementRouteService()

    result = PortfolioRouteFeasibilityService(
        route_service=route_service,
        consumer_admission_service=PassingNearbyAdmissionService(),
    ).prepare(
        snapshot,
        candidate_pools=[],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "pending"
    assert result.candidate_repairs == []
    assert [
        item["id"] for item in result.snapshot["days"][0]["segments"]
    ] == ["campus_segment", "meal_segment", "museum_segment"]
    assert not any(
        plan_id.endswith("_schedule_reorder")
        for plan_id, _pairs in route_service.requested_batches
    )
    assert result.repair_attempt_ledger["rejectedReasonCounts"] == {
        "schedule_reorder_exact_scope_unavailable": 1
    }


def test_pool_candidate_repair_rejects_cross_day_or_goal_lineage():
    snapshot = _snapshot()
    candidate = {
        **_poi("nearby_meal", "相邻小吃店", "food"),
        "longitude": 116.301,
        "latitude": 39.999,
        "briefId": "brief_1",
        "poolId": "meal_pool",
        "planningSlotId": "meal_slot",
        "dayNumber": 2,
        "sourceGoalId": "goal_other",
        "semanticPassed": True,
    }

    result = PortfolioRouteFeasibilityService(route_service=FakeRouteService()).prepare(
        snapshot,
        candidate_pools=[candidate],
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.candidate_repairs == []


def test_route_repair_grounding_rejects_non_finite_or_out_of_range_coordinates():
    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    for longitude, latitude in (
        (float("nan"), 39.9),
        (116.3, float("inf")),
        (181.0, 39.9),
        (116.3, -91.0),
    ):
        candidate = {
            **_poi("nearby_meal", "相邻小吃店", "food"),
            "longitude": longitude,
            "latitude": latitude,
            "semanticPassed": True,
        }
        assert service._candidate_grounded(candidate, city="北京") is False


def test_candidate_repair_rejects_missing_cross_brief_lineage():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["consumerAdmissionInput"] = (
        ConsumerCandidateAdmissionService.build_consumer_context(
            brief_id="brief_1",
            pool_id="meal_pool",
            planning_slot_id="meal_slot",
            day_number=1,
            city="北京",
            family="meal",
            activity_mode="meal",
            requirement_level="soft",
            experience_shape="single_poi",
            experience_goal="本地特色午餐",
        )
    )
    service = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=FakeMapPoiService(),
        consumer_admission_service=PassingNearbyAdmissionService(),
    )
    result = service.prepare(
        snapshot,
        candidate_pools=[
            {
                **_poi("wrong_brief_meal", "异方案餐饮", "food"),
                "briefId": "brief_other",
                "poolId": "meal_pool",
                "planningSlotId": "meal_slot",
                "semanticPassed": True,
            }
        ],
        city="北京",
        transport_mode="transit",
    )

    assert result.status == "passed"
    assert result.snapshot["days"][0]["segments"][1]["poi"]["amapId"] == "nearby_meal"
    assert result.candidate_repairs[0]["briefId"] == "brief_1"


def test_route_anchor_meal_is_preflighted_even_when_route_preference_metadata_is_missing():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][1]["semanticMetadata"].pop("routePreference")

    result = PortfolioRouteFeasibilityService(route_service=FakeRouteService()).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert any(item["code"] == "provider_route_matrix_unacceptable" for item in result.route_quality_issues)
    assert result.requires_route_verification is True


def test_nearby_candidate_without_amap_source_is_not_promoted():
    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService(), map_poi_service=FakeMapPoiService())
    candidate = service._nearby_candidate(
        POI(
            id="untrusted_meal",
            amap_id="untrusted_meal",
            name="未知来源小吃店",
            city="北京",
            category="food",
            type="餐饮服务;中餐厅",
            latitude=39.999,
            longitude=116.301,
        ),
        {"semanticMetadata": {"creativeBriefId": "brief_1", "poolId": "meal_pool", "planningSlotId": "meal_slot"}},
    )
    assert candidate["source"] == "unresolved-map-poi"
    assert service._candidate_grounded(candidate, city="北京") is False


def test_nearby_candidate_city_accepts_amap_municipality_suffix():
    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    candidate = _poi("nearby_meal", "相邻小吃店", "food")
    candidate["city"] = "北京市"

    assert service._candidate_grounded(candidate, city="北京") is True


def test_nearby_candidate_city_rejects_different_target_city():
    service = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    candidate = _poi("nearby_meal", "相邻小吃店", "food")
    candidate["city"] = "上海市"

    assert service._candidate_grounded(candidate, city="北京") is False


def test_web_seed_amap_grounded_candidate_repairs_after_nearby_shortage():
    class EmptyNearby:
        def search_nearby(self, *_args, **_kwargs):
            return SimpleNamespace(pois=[])

    class RecordedDiscovery:
        def __init__(self):
            self.calls = []

        def discover(self, **kwargs):
            self.calls.append(kwargs)
            return PoiDiscoveryResult(
                status="grounded",
                candidates=[
                    {
                        **_poi("nearby_meal", "相邻小吃店", "food"),
                        "longitude": 116.301,
                        "latitude": 39.999,
                            "candidateSource": "web_seed_amap_grounded",
                            "consumerAdmissionReport": {"scoreEligible": True},
                        "discoveryProvenance": {
                            "webProvider": "recorded-web",
                            "mapProvider": "amap-place-search",
                            "triggerReason": "portfolio_route_repair",
                        },
                    }
                ],
                webQueryCount=1,
                amapQueryCount=1,
                webSeedCount=1,
                webOnlyFinalPoiCount=0,
                fakeCoordinateCount=0,
            )

    snapshot = _snapshot()
    admission_input = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id="brief_1",
        pool_id="meal_pool",
        planning_slot_id="meal_slot",
        day_number=1,
        city="北京",
        family="meal",
        activity_mode="meal",
        requirement_level="soft",
        experience_shape="single_poi",
        experience_goal="本地特色午餐",
    )
    snapshot["days"][0]["segments"][1]["semanticMetadata"][
        "consumerAdmissionInput"
    ] = admission_input
    discovery = RecordedDiscovery()
    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=EmptyNearby(),
        poi_discovery_service=discovery,
        consumer_admission_service=PassingNearbyAdmissionService(),
    ).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=True,
        allow_web_discovery=True,
    )

    assert result.status == "passed"
    assert discovery.calls[0]["trigger_reason"] == ("route_repair_after_amap_candidate_shortage")
    assert result.snapshot["days"][0]["segments"][1]["poi"]["amapId"] == "nearby_meal"
    semantic = result.snapshot["days"][0]["segments"][1]["semanticMetadata"]
    assert semantic["routeCandidateSource"] == "web_seed_amap_grounded"
    assert semantic["consumerAdmissionInput"] == admission_input
    assert semantic["consumerAdmissionReport"]["consumerFingerprint"] == canonical_fingerprint(
        admission_input
    )
    assert semantic["routeReplacementMatrixProof"]["basis"] == ("old_two_leg_vs_candidate_two_leg_delta")
    assert semantic["discoveryProvenance"]["mapProvider"] == "amap-place-search"
    assert result.candidate_repairs[0]["discoveryProvenance"]["webProvider"] == "recorded-web"
    ledger = result.repair_attempt_ledger
    assert ledger["nearbyQueryCount"] == 3
    assert len(ledger["executedQueryFingerprints"]) == ledger["nearbyQueryCount"] + len(
        discovery.calls
    )
    assert ledger["queryCursor"] == len(ledger["executedQueryFingerprints"])
    checkpoint = ledger["routeBadCheckpoints"][0]
    assert checkpoint["executedQueryFingerprints"] == ledger["executedQueryFingerprints"]
    assert checkpoint["queryCursor"] == ledger["queryCursor"]


def test_web_seed_candidate_without_current_admission_is_rejected_before_probe():
    class EmptyNearby:
        def search_nearby(self, *_args, **_kwargs):
            return SimpleNamespace(pois=[])

    class RecordedDiscovery:
        def discover(self, **_kwargs):
            return PoiDiscoveryResult(
                status="grounded",
                candidates=[
                    {
                        **_poi("nearby_meal", "相邻小吃店", "food"),
                        "longitude": 116.301,
                        "latitude": 39.999,
                        # A report attached to shared raw facts is stale by
                        # definition and must not authorize this consumer.
                        "consumerAdmissionReport": {"scoreEligible": True},
                    }
                ],
                webQueryCount=1,
                amapQueryCount=1,
                webSeedCount=1,
                webOnlyFinalPoiCount=0,
                fakeCoordinateCount=0,
            )

    result = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        map_poi_service=EmptyNearby(),
        poi_discovery_service=RecordedDiscovery(),
    ).prepare(
        _snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=True,
        allow_web_discovery=True,
    )

    assert result.status == "failed"
    assert result.candidate_repairs == []
    assert result.repair_attempt_ledger["candidateProbeCount"] == 0
    assert result.repair_attempt_ledger["candidateEvaluationCount"] == 0
    assert (
        result.repair_attempt_ledger["rejectedReasonCounts"][
            "consumer_admission_context_missing"
        ]
        >= 1
    )


def test_planned_discovery_advances_only_after_completion_then_replays_no_progress():
    planned_query_fingerprint = canonical_fingerprint(
        {"provider": "poi_discovery", "scope": "meal_slot"}
    )

    def snapshot_with_planned_query():
        snapshot = _snapshot()
        snapshot["days"][0]["segments"][1]["semanticMetadata"][
            "routeQueryFingerprint"
        ] = planned_query_fingerprint
        return snapshot

    class RaisingDiscovery:
        calls = 0

        def discover(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("offline discovery interrupted")

    raising = RaisingDiscovery()
    interrupted = PortfolioRouteFeasibilityService(
        route_service=FakeRouteService(),
        poi_discovery_service=raising,
    ).prepare(
        snapshot_with_planned_query(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
        allow_web_discovery=True,
    )
    interrupted_checkpoint = interrupted.repair_attempt_ledger["routeBadCheckpoints"][0]
    assert raising.calls == 1
    assert interrupted_checkpoint["usedQueryFingerprints"] == [planned_query_fingerprint]
    assert interrupted_checkpoint["executedQueryFingerprints"] == []
    assert interrupted_checkpoint["queryCursor"] == 0

    class CompletedDiscovery:
        def __init__(self):
            self.calls = 0

        def discover(self, **_kwargs):
            self.calls += 1
            return PoiDiscoveryResult(
                status="unresolved",
                candidates=[],
                webQueryCount=1,
                amapQueryCount=0,
                webSeedCount=0,
                webOnlyFinalPoiCount=0,
                fakeCoordinateCount=0,
            )

    completed = CompletedDiscovery()
    route_service = RecordingRouteBudgetOrderService()
    service = PortfolioRouteFeasibilityService(
        route_service=route_service,
        poi_discovery_service=completed,
    )
    first = service.prepare(
        snapshot_with_planned_query(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
        allow_web_discovery=True,
    )
    first_checkpoint = first.repair_attempt_ledger["routeBadCheckpoints"][0]
    assert completed.calls == 1
    assert first_checkpoint["executedQueryFingerprints"] == [planned_query_fingerprint]
    assert first_checkpoint["queryCursor"] == 1
    calls_after_completion = list(route_service.call_kinds)

    replayed = service.prepare(
        first.snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
        allow_web_discovery=True,
    )

    assert replayed.provider_state == "no_progress"
    assert completed.calls == 1
    assert route_service.call_kinds == calls_after_completion
    assert replayed.repair_attempt_ledger["providerCallCount"] == 0
    assert replayed.repair_attempt_ledger["nearbyQueryCount"] == 0
    assert replayed.repair_attempt_ledger["candidateEvaluationCount"] == 0


def test_partial_route_evidence_is_retained_and_retry_requests_only_missing_legs():
    snapshot = _seven_anchor_snapshot()
    failed_pairs = {("segment_5", "segment_6"), ("segment_6", "segment_7")}
    first_provider = RecordingCanonicalRouteService(failed_pairs)

    partial = PortfolioRouteFeasibilityService(route_service=first_provider).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert partial.status == "pending"
    assert len(partial.route_evidence) == 3
    assert partial.route_execution_ledger == {
        **partial.route_execution_ledger,
        "expectedLegCount": 5,
        "requestedLegCount": 5,
        "completedLegCount": 3,
        "verifiedLegCount": 3,
        "failedLegCount": 2,
        "retainedEvidenceCount": 3,
    }
    cached_queried_at = {
        (item["fromSegmentId"], item["toSegmentId"]): item["queriedAt"] for item in partial.route_evidence
    }

    retry_provider = RecordingCanonicalRouteService()
    completed = PortfolioRouteFeasibilityService(route_service=retry_provider).prepare(
        partial.snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert retry_provider.requested_pairs == [sorted(failed_pairs)]
    assert completed.status == "passed"
    assert len(completed.route_evidence) == 5
    assert completed.route_execution_ledger["cachedLegCount"] == 3
    assert completed.route_execution_ledger["requestedLegCount"] == 2
    assert completed.route_execution_ledger["verifiedLegCount"] == 5
    completed_by_pair = {(item["fromSegmentId"], item["toSegmentId"]): item for item in completed.route_evidence}
    assert {pair: completed_by_pair[pair]["queriedAt"] for pair in cached_queried_at} == cached_queried_at


def test_exact_scope_reuses_other_logical_slots_sealed_matrix_without_provider_work():
    class ExactPairRouteService:
        map_provider_key = "amap-key"

        def __init__(self):
            self.request_batches = []

        def build_routes(
            self,
            plan_id,
            pois,
            transport_mode="transit",
            segments=None,
            route_pairs=None,
            **_kwargs,
        ):
            by_segment = {segment.id: segment for segment in segments or []}
            by_poi = {poi.id: poi for poi in pois}
            pairs = set(route_pairs or [])
            self.request_batches.append((str(plan_id), sorted(pairs)))
            routes = []
            for left_id, right_id in sorted(pairs):
                left = by_segment[left_id]
                right = by_segment[right_id]
                left_poi = by_poi[left.poi_id]
                right_poi = by_poi[right.poi_id]
                is_bypass = left.kind != "meal" and right.kind != "meal"
                distance = 1_000 if is_bypass else 400
                duration = 600 if is_bypass else 240
                routes.append(
                    RouteOption(
                        id=f"route_{left_id}_{right_id}",
                        plan_id=plan_id,
                        from_segment_id=left_id,
                        to_segment_id=right_id,
                        from_poi_id=left_poi.id,
                        to_poi_id=right_poi.id,
                        distance_meters=distance,
                        duration_seconds=duration,
                        mode=transport_mode,
                        provider="amap-webservice",
                        is_selected=True,
                        polyline=[[116.30, 39.90], [116.31, 39.91]],
                        steps=[
                            {"mode": "walking", "distance": 50},
                            {"mode": "transit", "distance": distance - 50},
                        ],
                        provider_payload={"status": "1", "waitSeconds": 0},
                    )
                )
            return routes

    def day(day_number: int, suffix: str, start_id: int) -> dict:
        return {
            "dayNumber": day_number,
            "segments": [
                {
                    "id": f"start_{suffix}",
                    "kind": "visit",
                    "startTime": "09:00",
                    "endTime": "10:00",
                    "poi": _poi(f"B{start_id:09d}", f"起点{suffix}", "campus"),
                    "semanticMetadata": {"routeAnchor": True},
                },
                {
                    "id": f"meal_{suffix}",
                    "kind": "meal",
                    "startTime": "11:00",
                    "endTime": "12:00",
                    "poi": _poi(f"B{start_id + 1:09d}", f"午餐{suffix}", "food"),
                    "semanticMetadata": {
                        "routeAnchor": True,
                        "creativeBriefId": "brief_1",
                        "poolId": f"pool_{suffix}",
                        "planningSlotId": f"slot_{suffix}",
                        "routePreference": {"preferNearAdjacentAnchors": True},
                    },
                },
                {
                    "id": f"end_{suffix}",
                    "kind": "visit",
                    "startTime": "13:00",
                    "endTime": "14:00",
                    "poi": _poi(f"B{start_id + 2:09d}", f"终点{suffix}", "campus"),
                    "semanticMetadata": {"routeAnchor": True},
                },
            ],
        }

    snapshot = {
        "id": "two_exact_insertion_slots",
        "planningSelectionRootTurnId": "planning_root_turn_test",
        "portfolioTransportPreference": "transit",
        "routeDecisionContract": _route_decision_contract(),
        "days": [day(1, "one", 101), day(2, "two", 201)],
    }
    route_service = ExactPairRouteService()
    service = PortfolioRouteFeasibilityService(route_service=route_service)
    initial = service._evaluate_snapshot(
        snapshot,
        city="北京",
        transport_mode="transit",
        preview_id="two_slots_initial",
    )
    assert initial["status"] == "passed"
    service._store_snapshot_route_evidence(snapshot, initial["evidence"])
    retained_proof = copy.deepcopy(
        snapshot["days"][1]["segments"][1]["semanticMetadata"][
            "routeInsertionMatrixProof"
        ]
    )
    retained_day_two_routes = [
        copy.deepcopy(item)
        for item in initial["evidence"]
        if any(
            "two" in endpoint
            for endpoint in (
                str(item.get("fromSegmentId") or ""),
                str(item.get("toSegmentId") or ""),
            )
        )
    ]
    day_two_items = {
        item["id"]: item
        for item in service._snapshot_segments(snapshot)
        if item["id"] in {"start_two", "meal_two", "end_two"}
    }
    retained_check = service._validated_retained_route_insertion_proof(
        replacement_proof=retained_proof,
        previous=day_two_items["start_two"],
        candidate=day_two_items["meal_two"],
        following=day_two_items["end_two"],
        incoming=service._provider_matrix_leg(retained_day_two_routes[0]),
        outgoing=service._provider_matrix_leg(retained_day_two_routes[1]),
        snapshot=snapshot,
    )
    assert retained_check is not None, (retained_proof, retained_day_two_routes)
    swapped = copy.deepcopy(retained_proof)
    swapped["baselineEndpoints"]["previousSegmentId"], swapped["baselineEndpoints"][
        "nextSegmentId"
    ] = (
        swapped["baselineEndpoints"]["nextSegmentId"],
        swapped["baselineEndpoints"]["previousSegmentId"],
    )
    swapped.pop("proofFingerprint")
    swapped["proofFingerprint"] = RouteInsertionScorer.route_proof_fingerprint(swapped)
    assert (
        service._validated_retained_route_insertion_proof(
            replacement_proof=swapped,
            previous=day_two_items["start_two"],
            candidate=day_two_items["meal_two"],
            following=day_two_items["end_two"],
            incoming=service._provider_matrix_leg(retained_day_two_routes[0]),
            outgoing=service._provider_matrix_leg(retained_day_two_routes[1]),
            snapshot=snapshot,
        )
        is None
    )
    service._invalidate_segment_route_evidence(snapshot, "meal_one")
    target = next(
        item for item in service._snapshot_segments(snapshot) if item["id"] == "meal_one"
    )
    scope = service._route_repair_scope_certificate(
        snapshot,
        segment=target,
        candidate=service._segment_poi_payload(target),
        transport_mode="transit",
    )
    assert scope is not None
    route_service.request_batches.clear()

    scoped = service._evaluate_snapshot(
        snapshot,
        city="北京",
        transport_mode="transit",
        preview_id="two_slots_scoped",
        repair_scope_certificate=scope,
    )

    assert scoped["status"] == "passed"
    requested_pairs = {
        pair
        for _plan_id, batch in route_service.request_batches
        for pair in batch
    }
    assert requested_pairs == {
        ("start_one", "meal_one"),
        ("meal_one", "end_one"),
        ("start_one", "end_one"),
    }
    assert not any("two" in endpoint for pair in requested_pairs for endpoint in pair)
    assert (
        snapshot["days"][1]["segments"][1]["semanticMetadata"][
            "routeInsertionMatrixProof"
        ]
        == retained_proof
    )
    assert not any(
        (issue.get("mealSegmentId") or issue.get("segmentId")) == "meal_two"
        for issue in scoped["issues"]
    )


def test_final_snapshot_fails_closed_when_provider_cost_components_are_not_derivable():
    class IncompleteCostRouteService(RecordingCanonicalRouteService):
        def build_routes(self, *args, **kwargs):
            routes = super().build_routes(*args, **kwargs)
            for route in routes:
                route.steps = []
                route.provider_payload = {"status": "1"}
            return routes

    result = PortfolioRouteFeasibilityService(route_service=IncompleteCostRouteService()).prepare(
        _seven_anchor_snapshot(),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "pending"
    assert result.route_execution_ledger["verifiedLegCount"] == 0
    assert result.route_quality_issues
    assert all(item["code"] == "provider_route_matrix_incomplete" for item in result.route_quality_issues)


def test_one_anchor_per_day_requires_no_route_provider_calls():
    snapshot = _seven_anchor_snapshot()
    snapshot["days"] = [{**day, "segments": day["segments"][:1]} for day in snapshot["days"]]
    route_provider = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_provider).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "passed"
    assert result.requires_route_verification is False
    assert result.provider_state == "not_required"
    assert result.route_execution_ledger["expectedLegCount"] == 0
    assert route_provider.requested_pairs == []


def test_route_ledger_counts_actual_budgeted_provider_calls_not_requested_pairs():
    budget = AmapCallBudget(
        place_text_max=0,
        place_around_max=0,
        route_refresh_max=1,
        total_external_max=1,
        source="route-ledger-test",
    )
    with amap_call_budget_scope(budget):
        result = PortfolioRouteFeasibilityService(route_service=BudgetAwareRouteService()).prepare(
            _seven_anchor_snapshot(),
            city="北京",
            transport_mode="transit",
            allow_nearby_search=False,
        )

    assert result.route_execution_ledger["requestedLegCount"] == 5
    assert result.route_execution_ledger["providerCallCount"] == 1
    assert result.route_execution_ledger["verifiedLegCount"] == 1
    assert result.route_execution_ledger["budgetExhausted"] is True
    assert result.provider_state == "budget_exhausted"
    assert any(item["code"] == "route_budget_exhausted" for item in result.route_quality_issues)
    assert all(item["code"] != "provider_route_matrix_unacceptable" for item in result.route_quality_issues)
    assert budget.snapshot()["usedRoute"] == 1


def test_declared_anchor_density_shortfall_fails_before_route_provider_calls():
    snapshot = _snapshot()
    snapshot["portfolioDayAnchorTargets"] = {"1": 4}
    route_provider = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_provider).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.provider_state == "not_started"
    assert result.route_call_count == 0
    assert result.route_execution_ledger["providerCallCount"] == 0
    assert result.route_quality_issues[0]["code"] == "route_anchor_target_mismatch"
    assert route_provider.requested_pairs == []


def test_soft_pending_future_anchors_do_not_block_routes_for_current_real_anchors():
    snapshot = _seven_anchor_snapshot()
    snapshot["portfolioDayAnchorTargets"] = {"1": 5, "2": 4}
    snapshot["portfolioPendingSlots"] = [
        {
            "planningSlotId": "soft_day_1",
            "dayNumber": 1,
            "requirementLevel": "explicit_soft",
            "futureRouteAnchor": True,
            "state": "pending",
        },
        {
            "planningSlotId": "soft_day_2",
            "dayNumber": 2,
            "requirementLevel": "soft",
            "futureRouteAnchor": True,
            "state": "pending",
        },
    ]
    route_provider = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_provider).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "passed"
    assert result.route_execution_ledger["expectedLegCount"] == 5
    assert result.route_execution_ledger["verifiedLegCount"] == 5
    assert len(route_provider.requested_pairs) == 1
    assert len(route_provider.requested_pairs[0]) == 5


def test_provider_exception_ledger_retains_actual_consumed_route_budget():
    budget = AmapCallBudget(
        place_text_max=0,
        place_around_max=0,
        route_refresh_max=8,
        total_external_max=8,
        source="route-ledger-test",
    )
    with amap_call_budget_scope(budget):
        result = PortfolioRouteFeasibilityService(route_service=RaisingBudgetRouteService()).prepare(
            _seven_anchor_snapshot(),
            city="北京",
            transport_mode="transit",
            allow_nearby_search=False,
        )

    assert result.status == "pending"
    assert result.route_call_count == 1
    assert result.route_execution_ledger["providerCallCount"] == 1
    assert result.route_execution_ledger["workerFailureClass"] == "RuntimeError"
    assert budget.snapshot()["usedRoute"] == 1


class PreconditionFailingRouteService:
    def build_routes(self, *_args, **_kwargs):
        raise RuntimeError("route provider key is not configured")


def test_uninvoked_route_provider_reports_exact_precondition_without_retry_copy():
    budget = AmapCallBudget(
        place_text_max=0,
        place_around_max=0,
        route_refresh_max=2,
        total_external_max=2,
        source="route-precondition-test",
    )
    snapshot = _snapshot()
    snapshot["days"][0]["segments"] = snapshot["days"][0]["segments"][:2]
    with amap_call_budget_scope(budget):
        result = PortfolioRouteFeasibilityService(route_service=PreconditionFailingRouteService()).prepare(
            snapshot,
            city="北京",
            transport_mode="transit",
            allow_nearby_search=False,
        )

    assert result.route_execution_ledger["expectedLegCount"] == 1
    assert result.route_execution_ledger["requestedLegCount"] == 1
    assert result.route_execution_ledger["providerCallCount"] == 0
    assert result.route_execution_ledger["routePreconditionFailureReason"] == "route_provider_not_invoked"
    assert result.route_execution_ledger["desiredDensityAnchorTargets"] == {"1": 2}
    assert result.route_execution_ledger["groundedRouteAnchorTargets"] == {"1": 2}
    assert result.route_execution_ledger["pendingFutureAnchorTargets"] == {"1": 0}
    assert result.provider_state == "precondition_failed"
    assert result.route_quality_issues[0]["code"] == "route_provider_precondition_failed"


def test_transit_theme_completion_does_not_request_walking_when_transit_is_available():
    class ThemeRouteService(RecordingCanonicalRouteService):
        def __init__(self):
            super().__init__()
            self.requested_modes = []

        def build_routes(
            self,
            plan_id,
            pois,
            transport_mode="transit",
            segments=None,
            route_pairs=None,
            **kwargs,
        ):
            self.requested_modes.append((str(transport_mode), set(route_pairs or set())))
            routes = super().build_routes(
                plan_id,
                pois,
                transport_mode,
                segments,
                route_pairs=route_pairs,
                **kwargs,
            )
            return routes

    admitted_meal = {
        "classification": "admitted_final_anchor",
        "scoreEligible": True,
    }
    admitted_area = {
        "classification": "admitted_anchor_set_member",
        "scoreEligible": True,
    }
    snapshot = {
        "id": "theme_completion_transit",
        "city": "北京",
        "portfolioTransportPreference": "transit",
        "routeDecisionContract": _route_decision_contract(),
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "meal",
                        "kind": "meal",
                        "startTime": "12:00",
                        "endTime": "13:00",
                        "poi": _poi("B000MEAL1", "北京地方菜馆", "food"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "briefId": "brief_theme",
                            "creativeBriefId": "brief_theme",
                            "optionalExperienceFamily": "local_food",
                            "consumerAdmissionReport": admitted_meal,
                        },
                    },
                    {
                        "id": "area_1",
                        "kind": "visit",
                        "startTime": "13:15",
                        "endTime": "14:00",
                        "poi": _poi("B000AREA1", "公开市场", "campus"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "briefId": "brief_theme",
                            "creativeBriefId": "brief_theme",
                            "optionalExperienceFamily": "market_walk",
                            "consumerAdmissionReport": admitted_area,
                        },
                    },
                    {
                        "id": "area_2",
                        "kind": "visit",
                        "startTime": "14:15",
                        "endTime": "15:00",
                        "poi": _poi("B000AREA2", "公共街区", "campus"),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "briefId": "brief_theme",
                            "creativeBriefId": "brief_theme",
                            "optionalExperienceFamily": "local_life",
                            "consumerAdmissionReport": admitted_area,
                        },
                    },
                ],
            }
        ],
        "portfolioPendingSlots": [],
        "portfolioThemeWalkingEvidence": [
            {
                "id": "stale-theme-walking",
                "planId": "stale-theme-walking-plan",
                "fromSegmentId": "area_1",
                "toSegmentId": "area_2",
                "fromPoiId": "B000AREA1",
                "toPoiId": "B000AREA2",
                "fromAmapId": "B000AREA1",
                "toAmapId": "B000AREA2",
                "provider": "amap-webservice",
                "mode": "walking",
                "status": "verified",
                "routeStatus": "verified",
                "distanceMeters": 1_000,
                "durationSeconds": 15 * 60,
                "steps": [{"instruction": "历史步行证据"}],
                "dayNumber": 1,
            }
        ],
    }
    route_service = ThemeRouteService()
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()

    with amap_call_budget_scope(budget):
        result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
            snapshot,
            city="北京",
            transport_mode="transit",
            allow_nearby_search=False,
            preview_id="theme_completion",
        )

    assert result.passed
    assert all(mode != "walking" for mode, _pairs in route_service.requested_modes)
    assert budget.route_refresh_max == 2
    assert budget.snapshot()["derivation"]["conditionalWalkingPairCount"] == 0
    assert budget.snapshot()["derivation"]["themeWalkingPairCount"] == 0
    assert len(result.snapshot["routeOptions"]) == 2
    assert all(route["mode"] == "transit" for route in result.snapshot["routeOptions"])
    assert result.snapshot["portfolioThemeWalkingEvidence"] == []
    quality = CreativeOutputQualityService.evaluate(
        result.snapshot,
        requested_theme=CreativeOutputQualityService.THEME,
    )
    assert quality["areaWalkWalkingRelationVerified"] is False
    assert quality["themeEligible"] is False
    assert "retry_route_verification" not in result.recommended_next_actions
    reverified = PortfolioRouteFeasibilityService(route_service=route_service).verify_snapshot(
        result.snapshot,
        city="北京",
        transport_mode="transit",
        preview_id="theme_completion_reverify",
    )
    assert reverified.passed
    assert all(mode != "walking" for mode, _pairs in route_service.requested_modes)


def _non_meal_provider_decision_snapshot(policy: dict) -> dict:
    snapshot = _snapshot()
    snapshot["routeDecisionContract"] = _route_decision_contract(detour_tolerance=policy.get("detourTolerance"))
    if "detourTolerance" not in policy:
        snapshot["routeDecisionContract"].pop("detourTolerance")
    campus, decision, museum = snapshot["days"][0]["segments"]
    campus["startTime"], campus["endTime"] = "16:00", "18:00"
    decision.update(
        {
            "startTime": "18:30",
            "endTime": "19:30",
            "kind": "visit",
            "poi": _poi("night-view", "城市夜景", "night_view"),
        }
    )
    museum["startTime"], museum["endTime"] = "20:00", "21:00"
    decision["semanticMetadata"] = {
        "routeAnchor": True,
        "groundingStatus": "selected",
        "creativeBriefId": "brief_1",
        "poolId": "night-pool",
        "planningSlotId": "night-slot",
        "sourceGoalId": "goal-night",
        "intentType": "night_view",
        "routeContract": {
            "requiresProviderInsertionDecision": True,
            "experienceSpecPolicy": policy,
            "specFingerprint": canonical_fingerprint(policy),
        },
    }
    return snapshot


def test_non_meal_experience_policy_uses_provider_delta_and_schedule_gate():
    policy = {
        "timeWindow": {"start": "18:00", "end": "20:00"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 2.0,
        },
    }
    route_service = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        _non_meal_provider_decision_snapshot(policy),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.passed
    assert route_service.requested_pairs[:2] == [
        [
            ("campus_segment", "meal_segment"),
            ("meal_segment", "museum_segment"),
        ],
        [("campus_segment", "museum_segment")],
    ]
    proof = result.snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeInsertionMatrixProof"]
    assert proof["proofFingerprint"] == RouteInsertionScorer.route_proof_fingerprint(proof)
    assert proof["networkVerified"] is True
    assert proof["timeWindowFeasible"] is True
    assert proof["detourTolerance"] == policy["detourTolerance"]
    assert proof["specFingerprint"] == canonical_fingerprint(policy)
    assert proof["detourToleranceSource"] == ("snapshot.routeDecisionContract.detourTolerance")
    assert proof["detourToleranceFingerprint"] == canonical_fingerprint(
        result.snapshot["routeDecisionContract"]["detourTolerance"]
    )
    assert proof["mobilityProfile"] == _explicit_mobility_profile()
    assert proof["mobilityProfileSource"] == ("snapshot.routeDecisionContract.mobilityProfile")
    assert proof["mobilityProfileFingerprint"] == canonical_fingerprint(_explicit_mobility_profile())
    assert proof["contractFingerprint"] == result.snapshot["routeDecisionContract"]["fingerprint"]
    assert proof["routeDecisionContractSource"] == "request_intent_contract"
    assert proof["routeDecisionContract"] == (
        RouteInsertionScorer.normalized_route_decision_contract(result.snapshot["routeDecisionContract"])
    )
    assert proof["scheduleSlackMinutes"] is None
    assert proof["candidateEndpoint"] == {
        "segmentId": "meal_segment",
        "amapId": PoiPhysicalIdentityService.canonical_amap_id(
            {"amapId": "night-view"}
        ),
    }
    campus_amap_id = PoiPhysicalIdentityService.canonical_amap_id(
        {"amapId": "campus"}
    )
    museum_amap_id = PoiPhysicalIdentityService.canonical_amap_id(
        {"amapId": "museum"}
    )
    night_view_amap_id = PoiPhysicalIdentityService.canonical_amap_id(
        {"amapId": "night-view"}
    )
    assert proof["baselineEndpoints"] == {
        "previousSegmentId": "campus_segment",
        "previousAmapId": campus_amap_id,
        "nextSegmentId": "museum_segment",
        "nextAmapId": museum_amap_id,
    }
    route_matrix = proof["routeMatrix"]
    expected_legs = {
        "previousToCandidate": (
            "campus_segment",
            "meal_segment",
            campus_amap_id,
            night_view_amap_id,
        ),
        "candidateToNext": (
            "meal_segment",
            "museum_segment",
            night_view_amap_id,
            museum_amap_id,
        ),
        "previousToNext": (
            "campus_segment",
            "museum_segment",
            campus_amap_id,
            museum_amap_id,
        ),
    }
    for key, expected_endpoints in expected_legs.items():
        leg = route_matrix[key]
        assert (
            leg["fromSegmentId"],
            leg["toSegmentId"],
            leg["fromAmapId"],
            leg["toAmapId"],
        ) == expected_endpoints
        assert leg["provider"] == leg["source"] == "amap-webservice"
        assert leg["distanceMeters"] > 0
        assert leg["durationSeconds"] > 0
        assert leg["queriedAt"]
        assert leg["walkingDistanceMeters"] >= 0
        assert leg["transferCount"] >= 0
        assert leg["waitSeconds"] >= 0
        assert leg["riskPenaltyMinutes"] >= 0


def test_non_meal_provider_decision_rejects_missing_mobility_profile():
    policy = {
        "timeWindow": {"start": "18:00", "end": "20:00"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 2.0,
        },
    }
    snapshot = _non_meal_provider_decision_snapshot(policy)
    contract = snapshot["routeDecisionContract"]
    contract.pop("mobilityProfile")
    route_service = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.provider_state == "precondition_failed"
    assert [item["code"] for item in result.route_quality_issues] == ["provider_insertion_mobility_profile_missing"]
    assert route_service.requested_pairs == []


def test_route_decision_contract_rejects_bare_legacy_policy_fields():
    snapshot = _snapshot()
    snapshot.pop("routeDecisionContract")
    snapshot["mobilityProfile"] = _explicit_mobility_profile()
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeContract"] = {
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        }
    }
    route_service = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.provider_state == "precondition_failed"
    assert [item["code"] for item in result.route_quality_issues] == ["provider_insertion_mobility_profile_missing"]
    assert route_service.requested_pairs == []


def test_segment_route_contract_accepts_complete_decision_contract_fallback():
    policy = {
        "timeWindow": {"start": "18:00", "end": "20:00"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 2.0,
        },
    }
    snapshot = _non_meal_provider_decision_snapshot(policy)
    decision_contract = snapshot.pop("routeDecisionContract")
    segment_contract = snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeContract"]
    segment_contract.update(decision_contract)

    result = PortfolioRouteFeasibilityService(route_service=RecordingCanonicalRouteService()).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.passed
    proof = result.snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeInsertionMatrixProof"]
    assert proof["routeDecisionContractLocation"] == "segment.routeContract"
    assert proof["contractFingerprint"] == decision_contract["fingerprint"]


def test_route_decision_contract_fingerprint_mismatch_fails_before_provider():
    snapshot = _snapshot()
    snapshot["routeDecisionContract"]["mobilityProfile"]["walkingPenaltyMinutesPerKm"] = 99
    route_service = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert [item["code"] for item in result.route_quality_issues] == ["provider_route_decision_contract_invalid"]
    assert route_service.requested_pairs == []


@pytest.mark.parametrize(
    ("policy", "failure_code"),
    [
        (
            {
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 35,
                    "maxDetourRatio": 0.35,
                }
            },
            "provider_insertion_time_window_missing",
        ),
        (
            {
                "timeWindow": "evening",
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 35,
                    "maxDetourRatio": 0.35,
                },
            },
            "provider_insertion_time_window_invalid",
        ),
        (
            {"timeWindow": {"start": "18:00", "end": "20:00"}},
            "provider_insertion_detour_tolerance_missing",
        ),
    ],
)
def test_non_meal_provider_decision_rejects_unknown_or_incomplete_policy(policy, failure_code):
    route_service = RecordingCanonicalRouteService()

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        _non_meal_provider_decision_snapshot(policy),
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.provider_state == "precondition_failed"
    assert [item["code"] for item in result.route_quality_issues] == [failure_code]
    assert route_service.requested_pairs == []


def test_non_meal_experience_policy_cannot_skip_route_gate_when_route_fields_are_absent():
    route_service = RecordingCanonicalRouteService()
    policy = {
        "accessPolicy": "public_outdoor",
        "evidenceFreshness": {"maxAgeHours": 24},
    }
    snapshot = _non_meal_provider_decision_snapshot(policy)
    snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeContract"].pop("requiresProviderInsertionDecision")

    result = PortfolioRouteFeasibilityService(route_service=route_service).prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert result.status == "failed"
    assert result.provider_state == "precondition_failed"
    assert [item["code"] for item in result.route_quality_issues] == ["provider_insertion_time_window_missing"]
    assert route_service.requested_pairs == []

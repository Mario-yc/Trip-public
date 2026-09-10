from __future__ import annotations

from datetime import datetime, timezone
import copy

from src.models.poi import POI
from src.models.poi_intent import PersistableSegmentPlan
from src.services.agent_service import AgentService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.daily_route_overlap_service import DailyRouteOverlapService
from src.services.simple_open_route_assignment_service import SimpleOpenRouteAssignmentService


def _poi(amap_id: str, name: str, longitude: float, latitude: float) -> POI:
    return POI(
        id=amap_id,
        amap_id=amap_id,
        name=name,
        city="北京",
        category="scenic",
        longitude=longitude,
        latitude=latitude,
        source="amap-place-search",
        confidence=1.0,
        type="科教文化服务;学校;高等院校",
    )


def _plan(
    slot_id: str,
    *,
    sequence: int,
    day_number: int = 1,
    intent_type: str = "campus_visit",
    route_anchor: bool = True,
    requires_route_edge: bool | None = True,
) -> PersistableSegmentPlan:
    plan = PersistableSegmentPlan(
        day_number=day_number,
        date=f"2026-10-{day_number:02d}",
        start_time="",
        duration_minutes=120,
        kind="campus",
        route_anchor=route_anchor,
        selected_poi=None,
        display_title="待分配",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        requires_route_edge=requires_route_edge,
        raw_need="高校参观",
        intent_type=intent_type,
        goal_id=f"goal_{slot_id}",
        source_goal_id=f"goal_{slot_id}",
        occurrence_id=f"occ:goal_{slot_id}:day:{day_number}",
        planning_slot_id=slot_id,
        pool_id=f"pool_{slot_id}",
        lineage_authority="goal_occurrence_compiler",
        requirement_level="hard",
        required=True,
        schedule_preference={"dayPart": "morning", "sequence": sequence},
        schedule_constraints={},
    )
    return plan


def _route_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="controller_semantic_choice",
        provenance={"transportMode": "transit", "confirmed": True},
        detour_tolerance={"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.15},
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
        adjacent_leg_constraint={
            "candidateSearchRadiusMeters": 5000,
            "maxProviderTravelMinutes": 45,
        },
    )
    assert contract is not None
    return {
        "schemaVersion": "route-decision-contract-v2",
        "status": "ready",
        "missingFields": [],
        "topologyConstraint": {"maxBacktrackRatio": 0.15},
        **contract,
    }


def _compact_route_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="controller_semantic_choice",
        provenance={"transportMode": "transit", "confirmed": True},
        detour_tolerance={"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.15},
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
        adjacent_leg_constraint={
            "candidateSearchRadiusMeters": 5000,
            "maxProviderTravelMinutes": 45,
        },
    )
    assert contract is not None
    return {
        "schemaVersion": "route-decision-contract-v2",
        "status": "ready",
        "missingFields": [],
        "topologyConstraint": {"maxBacktrackRatio": 0.15},
        **contract,
    }


class MatrixProvider:
    def __init__(self, durations: dict[tuple[str, str], int | tuple[int, int]]) -> None:
        self.durations = durations
        self.calls: list[tuple[str, str]] = []

    def verified_leg(self, *, plan_id, left, right, transport_mode):
        del plan_id, transport_mode
        pair = (left["amapId"], right["amapId"])
        self.calls.append(pair)
        route_value = self.durations.get(pair)
        if route_value is None:
            return None
        if isinstance(route_value, tuple):
            duration, distance_meters = route_value
        else:
            duration = route_value
            # Keep the generic fixture physically plausible under the sealed
            # adjacent-distance envelope. Tests that exercise the distance
            # boundary provide an explicit ``(minutes, meters)`` tuple.
            distance_meters = duration * 100
        return {
            "fromAmapId": pair[0],
            "toAmapId": pair[1],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "transit",
            "distanceMeters": distance_meters,
            "durationSeconds": duration * 60,
            "walkingDistanceMeters": 200,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
        }


class AlternativeMatrixProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def verified_leg_options(self, *, plan_id, left, right, transport_mode):
        del plan_id, transport_mode
        pair = (left["amapId"], right["amapId"])
        self.calls.append(pair)
        common = {
            "fromAmapId": pair[0],
            "toAmapId": pair[1],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "transit",
            "distanceMeters": 1000,
            "durationSeconds": 600,
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
            "steps": [],
        }
        if pair == ("B000A00001", "B000B00001"):
            return [
                {
                    **common,
                    "routeOptionId": "a-b-1",
                    "polyline": [[116.0, 39.0], [116.001, 39.0]],
                }
            ]
        if pair == ("B000B00001", "B000C00001"):
            return [
                {
                    **common,
                    "routeOptionId": "b-c-overlap",
                    "polyline": [[116.001, 39.0], [116.0, 39.0]],
                },
                {
                    **common,
                    "routeOptionId": "b-c-clean",
                    "distanceMeters": 1100,
                    "durationSeconds": 660,
                    "polyline": [[116.001, 39.0], [116.001, 39.001]],
                },
            ]
        return []

    def verified_leg(self, *, plan_id, left, right, transport_mode):
        options = self.verified_leg_options(
            plan_id=plan_id,
            left=left,
            right=right,
            transport_mode=transport_mode,
        )
        return options[0] if options else None


def test_single_stop_route_assignment_preserves_grounded_identity() -> None:
    plan = _plan("day2_campus", sequence=1, day_number=2)
    grounded = _poi("B000A84ZRH", "北京理工大学良乡校区北校区", 116.17, 39.73)
    lexically_first = _poi("B000A7PRL6", "北京科技大学管庄校区", 116.59, 39.91)
    another = _poi("B000A87JYS", "中国科学院大学雁栖湖校区", 116.68, 40.41)
    plan.selected_poi = copy.deepcopy(grounded)
    plan.display_title = grounded.name
    plan.grounding_status = "verified_amap"
    provider = MatrixProvider({})

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [plan],
        {"day2_campus": [grounded, lexically_first, another]},
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=4,
    )

    assert result.plans[0].selected_poi is not None
    assert result.plans[0].selected_poi.amap_id == "B000A84ZRH"
    assert result.plans[0].display_title == "北京理工大学良乡校区北校区"
    assert result.audit["selectedCombination"][0]["amapIds"] == ["B000A84ZRH"]
    assert provider.calls == []


def test_single_stop_route_assignment_rebinds_stale_selection_to_current_admission() -> None:
    plan = _plan("day2_campus", sequence=1, day_number=2)
    stale = _poi("B000STALE1", "旧候选高校", 116.17, 39.73)
    currently_admitted = _poi("B000CURRENT1", "当前准入高校", 116.18, 39.74)
    plan.selected_poi = copy.deepcopy(stale)
    plan.display_title = stale.name
    plan.grounding_status = "verified_amap"
    provider = MatrixProvider({})

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [plan],
        {"day2_campus": [currently_admitted]},
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=4,
    )

    assert result.audit["routeCoverageComplete"] is True
    assert result.plans[0].selected_poi is not None
    assert result.plans[0].selected_poi.amap_id == "B000CURRENT1"
    assert result.plans[0].display_title == "当前准入高校"
    assert result.audit["selectedCombination"][0]["amapIds"] == ["B000CURRENT1"]
    assert provider.calls == []


def test_single_stop_route_assignment_rejects_stale_selection_without_current_admission() -> None:
    plan = _plan("day2_campus", sequence=1, day_number=2)
    stale = _poi("B000STALE1", "旧候选高校", 116.17, 39.73)
    plan.selected_poi = copy.deepcopy(stale)
    plan.display_title = stale.name
    plan.grounding_status = "verified_amap"
    provider = MatrixProvider({})

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [plan],
        {"day2_campus": []},
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=4,
    )

    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["failureReason"] == "candidate_assignment_incomplete"
    assert result.audit["incompleteDayNumbers"] == [2]
    assert result.audit["selectedCombination"] == []
    assert provider.calls == []


def _three_anchor_assignment(policy: str) -> tuple[AlternativeMatrixProvider, object]:
    provider = AlternativeMatrixProvider()
    service = SimpleOpenRouteAssignmentService(
        route_leg_provider=provider,
        route_overlap_policy=policy,
    )
    result = service.assign(
        [
            _plan("a", sequence=1),
            _plan("b", sequence=2),
            _plan("c", sequence=3, intent_type="park"),
        ],
        {
            "a": [_poi("B000A00001", "A", 116.0, 39.0)],
            "b": [_poi("B000B00001", "B", 116.001, 39.0)],
            "c": [_poi("B000C00001", "C", 116.001, 39.001)],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )
    return provider, result


def test_trace_shaped_assignment_uses_provider_generalized_cost_not_first_candidate() -> None:
    far_campus = _poi("B000FARCAMP", "远端高校", 116.32, 40.00)
    near_campus = _poi("B000NEARCAM", "近端高校", 116.50, 39.91)
    far_park = _poi("B000FARPARK", "远端公园", 116.73, 39.90)
    near_park = _poi("B000NEARPARK", "近端公园", 116.52, 39.90)
    provider = MatrixProvider(
        {
            ("B000FARCAMP", "B000FARPARK"): 110,
            ("B000FARCAMP", "B000NEARPARK"): 75,
            ("B000NEARCAM", "B000FARPARK"): 70,
            ("B000NEARCAM", "B000NEARPARK"): 16,
        }
    )
    service = SimpleOpenRouteAssignmentService(route_leg_provider=provider)

    result = service.assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {
            "campus": [far_campus, near_campus],
            "park": [far_park, near_park],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=8,
    )

    assert [plan.selected_poi.amap_id for plan in result.plans] == ["B000NEARCAM", "B000NEARPARK"]
    assert result.audit["providerBaselineCompared"] is False
    assert result.audit["topologyCompliance"] == "verified"
    assert result.audit["detourCompliance"] == "not_evaluated"
    assert result.audit["routeContractFingerprint"] == _route_contract()["fingerprint"]
    assert result.audit["detourEnvelope"] == {
        "maxGeneralizedCostDelta": 15.0,
        "maxDetourRatio": 0.15,
    }
    assert 0 < result.audit["routeProviderAttemptCount"] <= 8
    assert result.audit["routeProviderAttemptCount"] == len(provider.calls)


def test_route_budget_or_provider_failure_is_truthful_pending_not_geometry_verification() -> None:
    provider = MatrixProvider({})
    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {
            "campus": [_poi("B000CAMPUS1", "高校", 116.32, 40.00)],
            "park": [_poi("B000PARK001", "公园", 116.33, 40.01)],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=1,
    )

    assert result.audit["detourCompliance"] == "not_evaluated"
    assert result.audit["routeProviderAttemptCount"] == 1
    assert result.audit["topologyEvidence"]["geometryUsedAsRouteFeasibilityEvidence"] is False
    assert result.audit["failureReason"] == "provider_route_matrix_incomplete"
    assert result.audit["selectedCombinationRouteBlocked"] is True
    assert result.audit["routeFeasibilityExhausted"] is True


def test_selected_route_failure_does_not_claim_topology_frontier_exhaustion() -> None:
    provider = MatrixProvider({})
    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {
            "campus": [_poi("B000CAMPUS1", "高校", 116.32, 40.00)],
            "park": [
                _poi("B000PARK001", "近公园", 116.33, 40.01),
                _poi("B000PARK002", "次近公园", 116.34, 40.01),
            ],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=1,
    )

    assert result.audit["failureReason"] == "provider_route_budget_insufficient"
    assert result.audit["topologyCombinationFrontierCount"] == 2
    assert result.audit["unverifiedTopologyCombinationCount"] == 1
    assert result.audit["selectedCombinationRouteBlocked"] is True
    assert result.audit["routeFeasibilityExhausted"] is False


def test_route_budget_stop_keeps_audit_pairs_aligned_with_materialized_segments() -> None:
    campus = _poi("B000CAMPUS1", "高校", 116.32, 40.00)
    selected_park = _poi("B000PARK001", "近公园", 116.33, 40.01)
    unattempted_park = _poi("B000PARK002", "次近公园", 116.34, 40.01)
    campus_plan = _plan("campus", sequence=1)
    park_plan = _plan("park", sequence=2, intent_type="park")
    campus_plan.selected_poi = copy.deepcopy(campus)
    campus_plan.display_title = campus.name
    campus_plan.grounding_status = "verified_amap"
    park_plan.selected_poi = copy.deepcopy(selected_park)
    park_plan.display_title = selected_park.name
    park_plan.grounding_status = "verified_amap"

    result = SimpleOpenRouteAssignmentService(route_leg_provider=MatrixProvider({})).assign(
        [campus_plan, park_plan],
        {
            "campus": [campus],
            "park": [selected_park, unattempted_park],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=1,
    )

    assert [plan.selected_poi.amap_id for plan in result.plans if plan.selected_poi is not None] == [
        "B000CAMPUS1",
        "B000PARK001",
    ]
    assert result.audit["selectedCombination"][0]["amapIds"] == ["B000CAMPUS1", "B000PARK001"]
    assert result.audit["expectedPairs"] == [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "campus",
            "toSegmentId": "park",
            "fromAmapId": "B000CAMPUS1",
            "toAmapId": "B000PARK001",
        }
    ]
    assert result.audit["topologyCandidateAttempts"][-1]["status"] == (
        "not_attempted_route_budget_insufficient"
    )


def test_first_topology_provider_failure_advances_with_pair_cache_within_existing_budget() -> None:
    campus = _poi("B000CAMPUS1", "高校", 116.000, 39.950)
    meal = _poi("B000MEAL001", "午餐", 116.010, 39.950)
    nearest_park = _poi("B000PARK001", "最近公园", 116.020, 39.950)
    next_park = _poi("B000PARK002", "次近公园", 116.025, 39.950)
    provider = MatrixProvider(
        {
            ("B000CAMPUS1", "B000MEAL001"): 10,
            # The geometry-first combination ends at B000PARK001, whose
            # Provider leg is deliberately absent. The next bounded topology
            # candidate is complete and shares the first pair.
            ("B000MEAL001", "B000PARK002"): 12,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [
            _plan("campus", sequence=1),
            _plan("meal", sequence=2, intent_type="meal"),
            _plan("park", sequence=3, intent_type="park"),
        ],
        {
            "campus": [campus],
            "meal": [meal],
            "park": [nearest_park, next_park],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=3,
    )

    assert [plan.selected_poi.amap_id for plan in result.plans] == [
        "B000CAMPUS1",
        "B000MEAL001",
        "B000PARK002",
    ]
    assert result.audit["routeCoverageComplete"] is True
    assert result.audit["routeProviderAttemptCount"] == 3
    assert result.audit["routeProviderCacheHitCount"] >= 1
    assert provider.calls == [
        ("B000CAMPUS1", "B000MEAL001"),
        ("B000MEAL001", "B000PARK001"),
        ("B000MEAL001", "B000PARK002"),
    ]


def test_route_budget_covers_each_day_first_topology_before_same_day_alternatives() -> None:
    day_one_campus = _poi("B000D1CAMP", "第一天高校", 116.000, 39.950)
    day_one_meal = _poi("B000D1MEAL", "第一天午餐", 116.010, 39.950)
    day_one_nearest_park = _poi("B000D1PK01", "第一天最近公园", 116.020, 39.950)
    day_one_alternative_park = _poi("B000D1PK02", "第一天替代公园", 116.025, 39.950)
    day_two_campus = _poi("B000D2CAMP", "第二天高校", 116.100, 39.950)
    day_two_meal = _poi("B000D2MEAL", "第二天午餐", 116.110, 39.950)
    day_two_park = _poi("B000D2PARK", "第二天公园", 116.120, 39.950)
    provider = MatrixProvider(
        {
            ("B000D1CAMP", "B000D1MEAL"): 10,
            # Day 1 rank 1 is incomplete; its rank-2 topology would be
            # feasible if the service spent the final call on this day.
            ("B000D1MEAL", "B000D1PK02"): 12,
            ("B000D2CAMP", "B000D2MEAL"): 10,
            ("B000D2MEAL", "B000D2PARK"): 12,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [
            _plan("day1-campus", day_number=1, sequence=1),
            _plan("day1-meal", day_number=1, sequence=2, intent_type="meal"),
            _plan("day1-park", day_number=1, sequence=3, intent_type="park"),
            _plan("day2-campus", day_number=2, sequence=1),
            _plan("day2-meal", day_number=2, sequence=2, intent_type="meal"),
            _plan("day2-park", day_number=2, sequence=3, intent_type="park"),
        ],
        {
            "day1-campus": [day_one_campus],
            "day1-meal": [day_one_meal],
            "day1-park": [day_one_nearest_park, day_one_alternative_park],
            "day2-campus": [day_two_campus],
            "day2-meal": [day_two_meal],
            "day2-park": [day_two_park],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=4,
    )

    assert provider.calls == [
        ("B000D1CAMP", "B000D1MEAL"),
        ("B000D1MEAL", "B000D1PK01"),
        ("B000D2CAMP", "B000D2MEAL"),
        ("B000D2MEAL", "B000D2PARK"),
    ]
    assert result.audit["routeProviderAttemptCount"] == len(provider.calls) == 4
    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["selectedCombinationRouteBlocked"] is True
    assert result.audit["incompleteDayNumbers"] == [1]
    assert result.audit["verifiedDayNumbers"] == [2]
    assert [(pair["dayNumber"], pair["pairOrdinal"]) for pair in result.audit["verifiedPairs"]] == [
        (2, 1),
        (2, 2),
    ]
    assert result.audit["failureReason"] == "provider_route_budget_insufficient"


def test_four_call_two_day_coverage_prioritizes_materialized_topology_over_lower_geometry_candidates() -> None:
    day_one_campus = _poi("B000D1CAMP", "第一天高校", 116.000, 39.950)
    day_one_meal = _poi("B000D1MEAL", "第一天午餐", 116.010, 39.950)
    day_one_park = _poi("B000D1PARK", "第一天公园", 116.020, 39.950)
    day_two_campus = _poi("B000D2CAMP", "第二天高校", 116.100, 39.950)
    day_two_meal = _poi("B000D2MEAL", "第二天午餐", 116.110, 39.950)
    day_two_nearest_park = _poi("B000D2PK01", "第二天最近公园", 116.120, 39.950)
    day_two_middle_park = _poi("B000D2PK02", "第二天次近公园", 116.125, 39.950)
    day_two_materialized_park = _poi("B000D2PK03", "第二天已选公园", 116.130, 39.950)

    plans = [
        _plan("day1-campus", day_number=1, sequence=1),
        _plan("day1-meal", day_number=1, sequence=2, intent_type="meal"),
        _plan("day1-park", day_number=1, sequence=3, intent_type="park"),
        _plan("day2-campus", day_number=2, sequence=1),
        _plan("day2-meal", day_number=2, sequence=2, intent_type="meal"),
        _plan("day2-park", day_number=2, sequence=3, intent_type="park"),
    ]
    materialized = [
        day_one_campus,
        day_one_meal,
        day_one_park,
        day_two_campus,
        day_two_meal,
        day_two_materialized_park,
    ]
    for plan, poi in zip(plans, materialized):
        plan.selected_poi = copy.deepcopy(poi)
        plan.display_title = poi.name
        plan.grounding_status = "verified_amap"

    provider = MatrixProvider(
        {
            ("B000D1CAMP", "B000D1MEAL"): 10,
            ("B000D1MEAL", "B000D1PARK"): 12,
            ("B000D2CAMP", "B000D2MEAL"): 10,
            # The two nearer geometry candidates deliberately have no complete
            # Provider route. The materialized third candidate is complete and
            # is the only topology that can become the displayed proposal.
            ("B000D2MEAL", "B000D2PK03"): 12,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        plans,
        {
            "day1-campus": [day_one_campus],
            "day1-meal": [day_one_meal],
            "day1-park": [day_one_park],
            "day2-campus": [day_two_campus],
            "day2-meal": [day_two_meal],
            "day2-park": [
                day_two_nearest_park,
                day_two_middle_park,
                day_two_materialized_park,
            ],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=4,
    )

    assert provider.calls == [
        ("B000D1CAMP", "B000D1MEAL"),
        ("B000D1MEAL", "B000D1PARK"),
        ("B000D2CAMP", "B000D2MEAL"),
        ("B000D2MEAL", "B000D2PK03"),
    ]
    assert result.audit["routeProviderAttemptCount"] == len(provider.calls) == 4
    assert result.audit["routeCoverageComplete"] is True
    assert result.audit["incompleteDayNumbers"] == []
    assert {
        (pair["dayNumber"], pair["pairOrdinal"])
        for pair in result.audit["verifiedPairs"]
    } == {(1, 1), (1, 2), (2, 1), (2, 2)}
    assert [
        plan.selected_poi.amap_id
        for plan in result.plans
        if plan.day_number == 2 and plan.selected_poi is not None
    ] == ["B000D2CAMP", "B000D2MEAL", "B000D2PK03"]
    assert any(
        attempt["dayNumber"] == 2
        and attempt["amapIds"] == ["B000D2CAMP", "B000D2MEAL", "B000D2PK03"]
        and attempt["status"] == "verified"
        for attempt in result.audit["topologyCandidateAttempts"]
    )


def test_materialized_topology_beyond_product_cutoff_is_forced_into_bounded_frontier() -> None:
    slot_candidates = {
        "campus": [
            _poi(f"B000CAMP{i}", f"高校{i}", 116.000, 39.950)
            for i in range(1, 6)
        ],
        "meal": [
            _poi(f"B000MEAL{i}", f"午餐{i}", 116.010, 39.950)
            for i in range(1, 6)
        ],
        "park": [
            _poi(f"B000PARK{i}", f"公园{i}", 116.020, 39.950)
            for i in range(1, 6)
        ],
    }
    plans = [
        _plan("campus", sequence=1),
        _plan("meal", sequence=2, intent_type="meal"),
        _plan("park", sequence=3, intent_type="park"),
    ]
    materialized = tuple(slot_candidates[slot][-1] for slot in ("campus", "meal", "park"))
    for plan, poi in zip(plans, materialized):
        plan.selected_poi = copy.deepcopy(poi)
        plan.display_title = poi.name
        plan.grounding_status = "verified_amap"
    provider = MatrixProvider(
        {
            ("B000CAMP5", "B000MEAL5"): 10,
            ("B000MEAL5", "B000PARK5"): 12,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        plans,
        slot_candidates,
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert provider.calls == [
        ("B000CAMP5", "B000MEAL5"),
        ("B000MEAL5", "B000PARK5"),
    ]
    assert result.audit["routeCoverageComplete"] is True
    assert result.audit["topologyCandidateCountByDay"] == [
        {
            "dayNumber": 1,
            "candidateCount": 32,
            "materializedCandidateGeometryRank": 32,
            "materializedCandidatePrioritized": True,
            "materializedCandidateForcedIntoBoundedFrontier": True,
            "materializedCandidateSurvivedTopologyFilter": True,
        }
    ]
    assert result.audit["selectedCombination"][0]["amapIds"] == [
        "B000CAMP5",
        "B000MEAL5",
        "B000PARK5",
    ]
    assert [
        (pair["fromAmapId"], pair["toAmapId"])
        for pair in result.audit["expectedPairs"]
    ] == [
        ("B000CAMP5", "B000MEAL5"),
        ("B000MEAL5", "B000PARK5"),
    ]


def test_trace_shaped_materialized_stops_do_not_hide_85_9_percent_backtrack_behind_creative_anchor() -> None:
    campus = _poi("B000TRACE01", "高校", 116.00000, 39.950)
    meal = _poi("B000TRACE02", "午餐", 116.05000, 39.950)
    park = _poi("B000TRACE03", "公园", 116.00705, 39.950)
    contract = _route_contract()
    contract["adjacentLegConstraint"]["candidateSearchRadiusMeters"] = 8000

    result = SimpleOpenRouteAssignmentService(route_leg_provider=MatrixProvider({})).assign(
        [
            _plan("campus", sequence=1, route_anchor=True, requires_route_edge=True),
            _plan("meal", sequence=2, intent_type="meal", route_anchor=False, requires_route_edge=True),
            _plan("park", sequence=3, intent_type="park", route_anchor=False, requires_route_edge=True),
        ],
        {
            "campus": [campus],
            "meal": [meal],
            "park": [park],
        },
        route_decision_contract=contract,
        transport_mode="transit",
        route_budget=2,
    )

    ordered = SimpleOpenRouteAssignmentService._haversine_km(
        campus, meal
    ) + SimpleOpenRouteAssignmentService._haversine_km(meal, park)
    best = SimpleOpenRouteAssignmentService._haversine_km(
        campus, park
    ) + SimpleOpenRouteAssignmentService._haversine_km(park, meal)
    assert round(ordered / best - 1, 3) == 0.859
    assert result.audit["topologyCompliance"] == "failed"
    assert result.audit["failureReason"] == "topology_constraint_exceeded"
    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["expectedPairs"] == []
    assert result.audit["routeProviderAttemptCount"] == 0


def test_legacy_materialized_real_amap_stop_defaults_into_physical_route_chain() -> None:
    campus = _poi("B000LEGACY1", "高校", 116.000, 39.950)
    meal = _poi("B000LEGACY2", "午餐", 116.010, 39.950)
    campus_plan = _plan("campus", sequence=1, requires_route_edge=None)
    meal_plan = _plan(
        "meal",
        sequence=2,
        intent_type="meal",
        route_anchor=False,
        requires_route_edge=None,
    )
    campus_plan.selected_poi = copy.deepcopy(campus)
    campus_plan.grounding_status = "verified_amap"
    meal_plan.selected_poi = copy.deepcopy(meal)
    meal_plan.grounding_status = "verified_amap"

    result = SimpleOpenRouteAssignmentService(
        route_leg_provider=MatrixProvider({("B000LEGACY1", "B000LEGACY2"): 10})
    ).assign(
        [campus_plan, meal_plan],
        {"campus": [campus], "meal": [meal]},
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=1,
    )

    assert result.audit["expectedPairs"] == [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "campus",
            "toSegmentId": "meal",
            "fromAmapId": "B000LEGACY1",
            "toAmapId": "B000LEGACY2",
        }
    ]
    assert result.audit["routeCoverageComplete"] is True


def test_multi_day_budget_shortfall_counts_unexpanded_topology_frontier_truthfully() -> None:
    day_one_campus = _poi("B000D1CAMP", "第一天高校", 116.000, 39.950)
    day_one_park_a = _poi("B000D1PRKA", "第一天公园甲", 116.010, 39.950)
    day_one_park_b = _poi("B000D1PRKB", "第一天公园乙", 116.012, 39.950)
    day_two_campus = _poi("B000D2CAMP", "第二天高校", 116.100, 39.950)
    day_two_park_a = _poi("B000D2PRKA", "第二天公园甲", 116.110, 39.950)
    day_two_park_b = _poi("B000D2PRKB", "第二天公园乙", 116.112, 39.950)

    result = SimpleOpenRouteAssignmentService(route_leg_provider=MatrixProvider({})).assign(
        [
            _plan("day1-campus", day_number=1, sequence=1),
            _plan("day1-park", day_number=1, sequence=2, intent_type="park"),
            _plan("day2-campus", day_number=2, sequence=1),
            _plan("day2-park", day_number=2, sequence=2, intent_type="park"),
        ],
        {
            "day1-campus": [day_one_campus],
            "day1-park": [day_one_park_a, day_one_park_b],
            "day2-campus": [day_two_campus],
            "day2-park": [day_two_park_a, day_two_park_b],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=0,
    )

    assert result.audit["topologyCombinationFrontierCount"] == 4
    assert result.audit["unverifiedTopologyCombinationCount"] == 4
    assert result.audit["routeFeasibilityExhausted"] is False


def test_compact_transit_contract_requires_complete_provider_leg_within_45_minutes() -> None:
    campus = _poi("B000CAMPUS1", "高校", 116.31, 39.99)
    park = _poi("B000PARK001", "公园", 116.33, 40.0)
    passing = SimpleOpenRouteAssignmentService(
        route_leg_provider=MatrixProvider({("B000CAMPUS1", "B000PARK001"): 30})
    ).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {"campus": [campus], "park": [park]},
        route_decision_contract=_compact_route_contract(),
        transport_mode="transit",
        route_budget=8,
    )

    assert passing.audit["routeCoverageComplete"] is True
    assert passing.audit["adjacentLegCompliance"] == "verified"
    assert passing.audit["topologyCompliance"] == "verified"
    assert passing.audit["detourCompliance"] == "not_evaluated"
    assert passing.audit["expectedPairs"] == [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "campus",
            "toSegmentId": "park",
            "fromAmapId": "B000CAMPUS1",
            "toAmapId": "B000PARK001",
        }
    ]
    assert passing.audit["verifiedPairs"][0]["durationSeconds"] == 1800
    assert passing.audit["verifiedPairs"][0]["distanceMeters"] == 3000
    assert passing.audit["verifiedPairs"][0]["transportMode"] == "transit"

    blocked = SimpleOpenRouteAssignmentService(
        route_leg_provider=MatrixProvider({("B000CAMPUS1", "B000PARK001"): 46})
    ).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {"campus": [campus], "park": [park]},
        route_decision_contract=_compact_route_contract(),
        transport_mode="transit",
        route_budget=8,
    )

    assert blocked.audit["routeCoverageComplete"] is False
    assert blocked.audit["adjacentLegCompliance"] == "failed"
    assert blocked.audit["failureReason"] == "adjacent_leg_limit_exceeded"
    assert blocked.audit["routeProviderAttemptCount"] == 1


def test_fast_provider_leg_over_adjacent_distance_envelope_is_rejected() -> None:
    campus = _poi("B000CAMPUS1", "高校", 116.31, 39.99)
    park = _poi("B000PARK001", "公园", 116.33, 40.0)

    result = SimpleOpenRouteAssignmentService(
        route_leg_provider=MatrixProvider({("B000CAMPUS1", "B000PARK001"): (12, 6000)})
    ).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {"campus": [campus], "park": [park]},
        route_decision_contract=_compact_route_contract(),
        transport_mode="transit",
        route_budget=8,
    )

    # The coordinates are within the 5 km candidate envelope, but the actual
    # Provider route is 6 km. A fast-but-long route is not adoption evidence.
    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["adjacentLegCompliance"] == "failed"
    assert result.audit["failureReason"] == "adjacent_leg_limit_exceeded"
    assert result.audit["routeProviderAttemptCount"] == 1


def test_dynamic_signed_strict_contract_is_executable_by_route_assignment() -> None:
    """The compiler's relative-strict output satisfies the route consumer."""

    selected_tolerance = {
        "maxGeneralizedCostDelta": 20,
        "maxDetourRatio": 0.2,
    }
    contract = AgentService._compile_route_decision_contract(
        request_text="公交地铁优先",
        experience_specs=[],
        existing_contract={
            "detourTolerance": selected_tolerance,
            "detourToleranceSource": "controller_semantic_choice",
        },
        detour_option_policy={
            "source": "server_signed_clarification_option_set",
            "selectionRole": "relative_strict",
            "selectionPolicy": "min_ratio_then_delta_then_option_id",
            "selectedOptionId": "opaque-low",
            "selectedOptionRank": 0,
            "selectedDetourTolerance": selected_tolerance,
            "optionCount": 3,
            "optionSetFingerprint": "a" * 64,
            "checkpointId": "clarify_dynamic",
            "checkpointFingerprint": "b" * 64,
            "sourceAssistantTurnId": "turn_dynamic",
        },
    )
    provider = MatrixProvider({("B000CAMPUS1", "B000VIEW001"): 30})
    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [_plan("campus", sequence=1), _plan("night", sequence=2, intent_type="night_view")],
        {
            "campus": [_poi("B000CAMPUS1", "高校", 116.31, 39.99)],
            "night": [_poi("B000VIEW001", "城市观景点", 116.33, 40.0)],
        },
        route_decision_contract=contract,
        transport_mode="transit",
        route_budget=1,
    )

    assert contract["schemaVersion"] == "route-decision-contract-v2"
    assert result.audit["failureReason"] is None
    assert result.audit["routeCoverageComplete"] is True
    assert result.audit["expectedPairs"] == [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "campus",
            "toSegmentId": "night",
            "fromAmapId": "B000CAMPUS1",
            "toAmapId": "B000VIEW001",
        }
    ]
    assert result.audit["routeProviderAttemptCount"] == 1
    assert provider.calls == [("B000CAMPUS1", "B000VIEW001")]


def test_verified_route_evidence_canonicalizes_public_transit_before_fingerprinting() -> None:
    campus = _poi("B000CAMPUS1", "高校", 116.31, 39.99)
    park = _poi("B000PARK001", "公园", 116.33, 40.0)

    result = SimpleOpenRouteAssignmentService(
        route_leg_provider=MatrixProvider({("B000CAMPUS1", "B000PARK001"): 30})
    ).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {"campus": [campus], "park": [park]},
        route_decision_contract=_compact_route_contract(),
        transport_mode="public_transit",
        route_budget=1,
    )

    pair = result.audit["verifiedPairs"][0]
    assert pair["transportMode"] == "transit"
    assert pair["providerEvidenceFingerprint"] == SimpleOpenRouteAssignmentService._provider_evidence_fingerprint(pair)


def test_route_matrix_pairs_noon_before_evening_when_server_slot_sequence_is_stale() -> None:
    campus = _plan("campus", sequence=1)
    campus.schedule_preference = {
        "dayPart": "flexible",
        "sequence": 1,
        "sequenceSource": "server_sealed_slot_order",
    }
    park = _plan("park", sequence=2, intent_type="park", route_anchor=False)
    park.schedule_preference = {
        "dayPart": "evening",
        "sequence": 2,
        "sequenceSource": "server_sealed_slot_order",
    }
    meal = _plan("meal", sequence=3, intent_type="meal", route_anchor=False)
    meal.schedule_preference = {
        "dayPart": "noon",
        "sequence": 3,
        "sequenceSource": "server_sealed_slot_order",
    }
    provider = MatrixProvider(
        {
            ("B000CAMPUS1", "B000MEAL001"): 12,
            ("B000MEAL001", "B000PARK001"): 15,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [campus, park, meal],
        {
            "campus": [_poi("B000CAMPUS1", "高校", 116.31, 39.99)],
            "meal": [_poi("B000MEAL001", "餐厅", 116.33, 39.98)],
            "park": [_poi("B000PARK001", "公园", 116.35, 39.97)],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["topologyCompliance"] == "verified"
    assert result.audit["detourCompliance"] == "not_evaluated"
    assert result.audit["selectedCombination"][0]["planningSlotIds"] == [
        "campus",
        "meal",
        "park",
    ]
    assert provider.calls == [
        ("B000CAMPUS1", "B000MEAL001"),
        ("B000MEAL001", "B000PARK001"),
    ]


def test_far_novel_combination_is_rejected_without_provider_baseline() -> None:
    novel_far_a = _poi("B000NOVELA1", "远端新点 A", 116.60, 40.10)
    novel_far_b = _poi("B000NOVELB1", "远端新点 B", 116.80, 40.20)
    provider = MatrixProvider(
        {
            ("B000PRIORA1", "B000PRIORB1"): 10,
            ("B000NOVELA1", "B000NOVELB1"): 75,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [_plan("campus", sequence=1), _plan("park", sequence=2, intent_type="park")],
        {
            "campus": [novel_far_a],
            "park": [novel_far_b],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["providerBaselineCompared"] is False
    assert result.audit["detourCompliance"] == "not_evaluated"
    assert result.audit["topologyCompliance"] == "failed"
    assert result.audit["failureReason"] == "topology_constraint_exceeded"
    assert provider.calls == []
    assert all(plan.selected_poi is None for plan in result.plans)


def test_nearby_candidate_needs_only_final_provider_leg() -> None:
    campus = _poi("B000CAMPUS1", "高校", 116.31, 39.99)
    nearby_meal = _poi("B000MEAL001", "附近餐厅", 116.33, 39.98)
    provider = MatrixProvider({("B000CAMPUS1", "B000MEAL001"): 12})

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [_plan("campus", sequence=1), _plan("meal", sequence=2, intent_type="meal")],
        {
            "campus": [campus],
            "meal": [nearby_meal],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["topologyCompliance"] == "verified"
    assert result.audit["adjacentLegCompliance"] == "verified"
    assert result.audit["detourCompliance"] == "not_evaluated"
    assert result.audit["failureReason"] is None
    assert provider.calls == [("B000CAMPUS1", "B000MEAL001")]


def test_exact_poi_is_preserved_when_relative_route_envelope_blocks_confirmation() -> None:
    exact = _plan("exact", sequence=1)
    exact.schedule_constraints = {
        "entityBindingMode": "exact_entity",
        "replaceablePoi": False,
    }
    replaceable = _plan("park", sequence=2, intent_type="park")
    prior_exact = _poi("B000EXACT01", "用户指定高校", 116.30, 39.95)
    novel_park = _poi("B000NOVELP1", "远端新公园", 116.80, 40.20)
    provider = MatrixProvider(
        {
            ("B000EXACT01", "B000PRIORP1"): 10,
            ("B000EXACT01", "B000NOVELP1"): 75,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [exact, replaceable],
        {"exact": [prior_exact], "park": [novel_park]},
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["detourCompliance"] == "not_evaluated"
    assert result.audit["topologyCompliance"] == "failed"
    assert result.audit["failureReason"] == "fixed_poi_route_constraint_conflict"
    assert result.plans[0].selected_poi is not None
    assert result.plans[0].selected_poi.amap_id == "B000EXACT01"
    assert "已保留" in result.plans[0].notes
    assert result.plans[1].selected_poi is None


def test_exact_poi_binding_ignores_alternate_candidates_even_when_the_alternate_is_first() -> None:
    exact = _plan("exact", sequence=1)
    exact.schedule_constraints = {
        "entityBindingMode": "exact_entity",
        "replaceablePoi": False,
    }
    replaceable = _plan("park", sequence=2, intent_type="park")
    alternate = _poi("B000ALTERN1", "错误替代高校", 116.31, 39.95)
    fixed = _poi("B000EXACT01", "用户指定高校", 116.30, 39.95)
    park = _poi("B000PARK001", "附近公园", 116.32, 39.95)
    exact.selected_poi = fixed
    provider = MatrixProvider({("B000EXACT01", "B000PARK001"): 12})

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [exact, replaceable],
        {"exact": [alternate, fixed], "park": [park]},
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["adjacentLegCompliance"] == "verified"
    assert result.plans[0].selected_poi is not None
    assert result.plans[0].selected_poi.amap_id == "B000EXACT01"
    assert ("B000ALTERN1", "B000PARK001") not in provider.calls


def test_overlap_observe_policy_keeps_first_provider_option_and_freezes_replayable_evidence() -> None:
    provider, result = _three_anchor_assignment("observe")

    assert provider.calls == [
        ("B000A00001", "B000B00001"),
        ("B000B00001", "B000C00001"),
    ]
    assert [item["routeOptionId"] for item in result.route_legs] == ["a-b-1", "b-c-overlap"]
    assert [plan.selected_poi.amap_id for plan in result.plans] == [
        "B000A00001",
        "B000B00001",
        "B000C00001",
    ]
    assert result.audit["dailyRouteOverlapPolicy"] == "observe"
    assert result.audit["dailyRouteOverlapStatus"] == "evaluated_single_option"
    assert result.audit["legacyBacktrackMetric"] == "waypoint_haversine_geometry_proxy"
    evidence = result.audit["dailyRouteOverlapEvidence"]["perDay"][0]
    assert evidence["reverseDirectionRepeatedMeters"] > 70
    assert evidence["selectionStatus"] == "evaluated_single_option"
    assert evidence["alternativesEvaluated"] == 1
    assert evidence["selectedAlternativeIds"] == ["a-b-1", "b-c-overlap"]
    assert evidence["selectedRouteGeometry"][0]["providerEvidenceFingerprint"]
    assert evidence["selectedRouteGeometry"][0]["geometryParts"]
    assert (
        evidence["selectedRouteGeometry"][0]["providerEvidenceFingerprint"]
        == result.audit["verifiedPairs"][0]["providerEvidenceFingerprint"]
    )


def test_overlap_rank_policy_uses_bounded_same_response_option_without_more_provider_calls() -> None:
    provider, result = _three_anchor_assignment("rank")

    assert provider.calls == [
        ("B000A00001", "B000B00001"),
        ("B000B00001", "B000C00001"),
    ]
    assert [item["routeOptionId"] for item in result.route_legs] == ["a-b-1", "b-c-clean"]
    assert result.audit["routeProviderAttemptCount"] == 2
    assert result.audit["dailyRouteOverlapStatus"] == "ranked_bounded_options"
    evidence = result.audit["dailyRouteOverlapEvidence"]["perDay"][0]
    assert evidence["nonExemptRepeatedMeters"] == 0
    assert evidence["alternativesEvaluated"] == 2
    assert evidence["selectedAlternativeIds"] == ["a-b-1", "b-c-clean"]
    assert DailyRouteOverlapService.verify_evidence_fingerprint(evidence) is True
    assert DailyRouteOverlapService.evidence_fingerprint(evidence) == evidence["evidenceFingerprint"]

    tampered = copy.deepcopy(evidence)
    tampered["boundedRouteOptionCombinationCount"] = 1
    assert DailyRouteOverlapService.verify_evidence_fingerprint(tampered) is False


def test_explicit_low_detour_contract_enables_bounded_ranking_over_observe_default() -> None:
    provider = AlternativeMatrixProvider()
    contract = _route_contract()
    contract["detourToleranceSource"] = "controller_semantic_choice"
    contract["detourTolerance"] = {
        "maxGeneralizedCostDelta": 15.0,
        "maxDetourRatio": 0.15,
    }

    result = SimpleOpenRouteAssignmentService(
        route_leg_provider=provider,
        route_overlap_policy="observe",
    ).assign(
        [
            _plan("a", sequence=1),
            _plan("b", sequence=2),
            _plan("c", sequence=3, intent_type="park"),
        ],
        {
            "a": [_poi("B000A00001", "A", 116.0, 39.0)],
            "b": [_poi("B000B00001", "B", 116.001, 39.0)],
            "c": [_poi("B000C00001", "C", 116.001, 39.001)],
        },
        route_decision_contract=contract,
        transport_mode="transit",
        route_budget=2,
    )

    assert provider.calls == [
        ("B000A00001", "B000B00001"),
        ("B000B00001", "B000C00001"),
    ]
    assert result.audit["dailyRouteOverlapPolicy"] == "rank"
    assert result.audit["dailyRouteOverlapPolicySource"] == "explicit_low_detour_contract"
    assert result.audit["routeProviderAttemptCount"] == 2
    assert [item["routeOptionId"] for item in result.route_legs] == ["a-b-1", "b-c-clean"]


def test_explicit_low_detour_contract_rejects_best_option_above_backtrack_limit() -> None:
    class OnlyBacktrackingProvider(AlternativeMatrixProvider):
        def verified_leg_options(self, *, plan_id, left, right, transport_mode):
            options = super().verified_leg_options(
                plan_id=plan_id,
                left=left,
                right=right,
                transport_mode=transport_mode,
            )
            if (left["amapId"], right["amapId"]) == ("B000B00001", "B000C00001"):
                return options[:1]
            return options

    provider = OnlyBacktrackingProvider()
    contract = _route_contract()
    contract["detourToleranceSource"] = "controller_semantic_choice"
    contract["detourTolerance"] = {
        "maxGeneralizedCostDelta": 15.0,
        "maxDetourRatio": 0.15,
    }

    result = SimpleOpenRouteAssignmentService(
        route_leg_provider=provider,
        route_overlap_policy="observe",
    ).assign(
        [
            _plan("a", sequence=1),
            _plan("b", sequence=2),
            _plan("c", sequence=3, intent_type="park"),
        ],
        {
            "a": [_poi("B000A00001", "A", 116.0, 39.0)],
            "b": [_poi("B000B00001", "B", 116.001, 39.0)],
            "c": [_poi("B000C00001", "C", 116.001, 39.001)],
        },
        route_decision_contract=contract,
        transport_mode="transit",
        route_budget=2,
    )

    assert provider.calls == [
        ("B000A00001", "B000B00001"),
        ("B000B00001", "B000C00001"),
    ]
    assert result.audit["dailyRouteOverlapPolicy"] == "rank"
    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["failureReason"] == "daily_route_backtrack_limit_exceeded"
    assert result.audit["routeFeasibilityExhausted"] is True
    assert result.route_legs == []


def test_explicit_balanced_detour_contract_preserves_observe_default() -> None:
    provider = AlternativeMatrixProvider()
    contract = _route_contract()
    contract["detourToleranceSource"] = "controller_semantic_choice"
    contract["detourTolerance"] = {
        "maxGeneralizedCostDelta": 30.0,
        "maxDetourRatio": 0.3,
    }

    result = SimpleOpenRouteAssignmentService(
        route_leg_provider=provider,
        route_overlap_policy="observe",
    ).assign(
        [
            _plan("a", sequence=1),
            _plan("b", sequence=2),
            _plan("c", sequence=3, intent_type="park"),
        ],
        {
            "a": [_poi("B000A00001", "A", 116.0, 39.0)],
            "b": [_poi("B000B00001", "B", 116.001, 39.0)],
            "c": [_poi("B000C00001", "C", 116.001, 39.001)],
        },
        route_decision_contract=contract,
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["dailyRouteOverlapPolicy"] == "observe"
    assert result.audit["dailyRouteOverlapPolicySource"] == "configured_observe_policy"
    assert [item["routeOptionId"] for item in result.route_legs] == ["a-b-1", "b-c-overlap"]


def test_incomplete_day_does_not_skip_route_optimization_for_later_complete_day() -> None:
    missing_day_one = _plan("day1_night", sequence=1, intent_type="night_view")

    day_two_campus = _plan("day2_campus", sequence=1)
    day_two_meal = _plan("day2_meal", sequence=2, intent_type="meal")
    day_two_night = _plan("day2_night", sequence=3, intent_type="night_view")
    for plan in (day_two_campus, day_two_meal, day_two_night):
        plan.day_number = 2
        plan.date = "2026-10-02"

    campus = _poi("B000DAY2C", "第二天高校", 116.00, 39.95)
    backtracking_meal = _poi("B000DAY2MF", "绕路午餐", 116.04, 39.95)
    compact_meal = _poi("B000DAY2MN", "顺路午餐", 116.01, 39.95)
    night = _poi("B000DAY2N", "第二天夜景", 116.02, 39.95)
    provider = MatrixProvider(
        {
            ("B000DAY2C", "B000DAY2MN"): 10,
            ("B000DAY2MN", "B000DAY2N"): 10,
        }
    )

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [missing_day_one, day_two_campus, day_two_meal, day_two_night],
        {
            "day1_night": [],
            "day2_campus": [campus],
            "day2_meal": [backtracking_meal, compact_meal],
            "day2_night": [night],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=4,
    )

    assert result.audit["failureReason"] == "candidate_assignment_incomplete"
    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["incompleteDayNumbers"] == [1]
    assert result.audit["verifiedDayNumbers"] == [2]
    assert result.audit["routeProviderAttemptCount"] == 2
    assert provider.calls == [
        ("B000DAY2C", "B000DAY2MN"),
        ("B000DAY2MN", "B000DAY2N"),
    ]
    assigned_day_two_meal = next(plan for plan in result.plans if plan.planning_slot_id == "day2_meal")
    assert assigned_day_two_meal.selected_poi is not None
    assert assigned_day_two_meal.selected_poi.amap_id == "B000DAY2MN"
    assert [leg["fromAmapId"] for leg in result.route_legs] == ["B000DAY2C", "B000DAY2MN"]


def test_repeated_amap_pair_on_two_days_keeps_day_and_segment_pair_identity() -> None:
    """A cached Provider pair is still two distinct day-local itinerary edges."""

    provider = MatrixProvider({("B000REPEAT1", "B000REPEAT2"): 12})
    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [
            _plan("day1_campus", sequence=1, day_number=1),
            _plan("day1_meal", sequence=2, day_number=1, intent_type="attraction"),
            _plan("day2_campus", sequence=1, day_number=2),
            _plan("day2_meal", sequence=2, day_number=2, intent_type="attraction"),
        ],
        {
            "day1_campus": [_poi("B000REPEAT1", "同一高校", 116.30, 39.90)],
            "day1_meal": [_poi("B000REPEAT2", "同一餐厅", 116.31, 39.90)],
            "day2_campus": [_poi("B000REPEAT1", "同一高校", 116.30, 39.90)],
            "day2_meal": [_poi("B000REPEAT2", "同一餐厅", 116.31, 39.90)],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=1,
    )

    expected = [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "day1_campus",
            "toSegmentId": "day1_meal",
            "fromAmapId": "B000REPEAT1",
            "toAmapId": "B000REPEAT2",
        },
        {
            "dayNumber": 2,
            "pairOrdinal": 1,
            "fromSegmentId": "day2_campus",
            "toSegmentId": "day2_meal",
            "fromAmapId": "B000REPEAT1",
            "toAmapId": "B000REPEAT2",
        },
    ]
    assert result.audit["routeCoverageComplete"] is True
    assert result.audit["expectedPairs"] == expected
    assert [{key: item[key] for key in expected[0]} for item in result.audit["verifiedPairs"]] == expected
    assert (
        result.audit["verifiedPairs"][0]["providerEvidenceFingerprint"]
        != result.audit["verifiedPairs"][1]["providerEvidenceFingerprint"]
    )
    assert provider.calls == [("B000REPEAT1", "B000REPEAT2")]


def test_route_assignment_rejects_same_grounded_meal_family_across_days() -> None:
    provider = MatrixProvider(
        {
            ("B000CAMPUS1", "B000MEAL1"): 12,
            ("B000CAMPUS2", "B000MEAL2"): 12,
        }
    )
    first_meal = _poi("B000MEAL1", "烤鸭甲店", 116.31, 39.90)
    second_meal = _poi("B000MEAL2", "烤鸭乙店", 116.33, 39.91)
    first_meal.meal_semantic_evidence = {
        "canonicalBrand": "烤鸭甲店",
        "groundedFamilyKey": "烤鸭",
    }
    second_meal.meal_semantic_evidence = {
        "canonicalBrand": "烤鸭乙店",
        "groundedFamilyKey": "烤鸭",
    }

    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [
            _plan("day1_campus", sequence=1, day_number=1),
            _plan("day1_meal", sequence=2, day_number=1, intent_type="meal"),
            _plan("day2_campus", sequence=1, day_number=2),
            _plan("day2_meal", sequence=2, day_number=2, intent_type="meal"),
        ],
        {
            "day1_campus": [_poi("B000CAMPUS1", "高校甲", 116.30, 39.90)],
            "day1_meal": [first_meal],
            "day2_campus": [_poi("B000CAMPUS2", "高校乙", 116.32, 39.91)],
            "day2_meal": [second_meal],
        },
        route_decision_contract=_route_contract(),
        transport_mode="transit",
        route_budget=2,
    )

    assert result.audit["routeCoverageComplete"] is False
    assert result.audit["failureReason"] == "simple_direction_meal_family_repeated"
    assert provider.calls == [("B000CAMPUS1", "B000MEAL1")]

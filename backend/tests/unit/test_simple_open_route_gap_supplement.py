from __future__ import annotations

from datetime import datetime, timezone

from src.api.schemas.agent import AgentInitialPlanOutput
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor
from src.services.simple_open_route_assignment_service import SimpleOpenRouteAssignmentService


def _provider_poi(amap_id: str, name: str, longitude: float, *, type_name: str) -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type=type_name,
        address=f"{name}地址",
        city="北京市",
        district="海淀区",
        longitude=longitude,
        latitude=39.95,
        category="scenic",
        source="amap-place-search",
        sourceNote="recorded-provider-shape",
        confidence=0.95,
        providerTypeCode="110101" if "公园" in type_name else "141201",
        providerQueriedAt=datetime(2026, 8, 24, tzinfo=timezone.utc),
        providerQueryReceiptFingerprint="a" * 64,
        openTimeToday="08:00-18:00",
    )


class GapMapProvider:
    def __init__(self) -> None:
        self.nearby_calls = 0

    def search(self, city, *, keyword, category, limit):
        del limit
        poi = (
            _provider_poi("B000ANCHOR1", "清华大学", 116.30, type_name="科教文化服务;学校;高等院校")
            if "上午" in keyword
            else _provider_poi("B000ANCHOR2", "颐和园公园", 116.34, type_name="风景名胜;公园广场;公园")
        )
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[poi],
        )

    def search_nearby(
        self,
        city,
        longitude=None,
        latitude=None,
        keyword=None,
        category="all",
        **kwargs,
    ):
        del longitude, latitude
        self.nearby_calls += 1
        poi = (
            _provider_poi("B000ANCHOR2", "颐和园公园", 116.34, type_name="风景名胜;公园广场;公园")
            if self.nearby_calls == 1
            else _provider_poi(
                "B000GAPPARK",
                "沿途口袋公园",
                116.32,
                type_name="风景名胜;公园广场;公园",
            )
        )
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword or "",
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[poi],
        )


class GapRouteProvider:
    def __init__(self, *, insertion_minutes: int = 12) -> None:
        self.insertion_minutes = insertion_minutes
        self.calls: list[tuple[str, str]] = []

    def verified_leg(self, *, plan_id, left, right, transport_mode):
        del plan_id
        pair = (left["amapId"], right["amapId"])
        self.calls.append(pair)
        duration = 20 if pair == ("B000ANCHOR1", "B000ANCHOR2") else self.insertion_minutes
        return {
            "fromAmapId": pair[0],
            "toAmapId": pair[1],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": transport_mode,
            # The fixture's final anchor pair is a valid route under the
            # authoritative 5 km adjacent-leg contract.  Keep the deliberately
            # slow insertion alternative invalid through duration, rather than
            # accidentally making the only final pair fail the distance gate.
            "distanceMeters": duration * 200,
            "durationSeconds": duration * 60,
            "walkingDistanceMeters": 100,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "queriedAt": "2026-08-24T08:00:00+00:00",
        }


def _route_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="controller_semantic_choice",
        provenance={"transportMode": "transit", "confirmed": True},
        detour_tolerance={"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.25},
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 1.0,
            "transferPenaltyMinutes": 5.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
        adjacent_leg_constraint={
            "candidateSearchRadiusMeters": 5000,
            "maxProviderTravelMinutes": 45,
        },
        topology_constraint={"maxBacktrackRatio": 0.15},
    )
    assert contract is not None
    return {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        **contract,
    }


def _initial_plan() -> AgentInitialPlanOutput:
    return AgentInitialPlanOutput.model_validate(
        {
            "reply": "先完成必选，再按真实路线空档补充。",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_morning",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "09:00",
                    "durationMinutes": 60,
                    "kind": "campus",
                    "rawNeed": "上午高校",
                    "routeAnchor": True,
                },
                {
                    "slotId": "day1_afternoon",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "14:00",
                    "durationMinutes": 60,
                    "kind": "visit",
                    "rawNeed": "下午公园",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "pool_campus",
                    "rawNeed": "上午高校",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_morning"],
                    "candidateHints": ["上午高校"],
                },
                {
                    "poolId": "pool_park",
                    "rawNeed": "下午公园",
                    "city": "北京",
                    "intentType": "park",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_afternoon"],
                    "candidateHints": ["下午公园"],
                },
            ],
        }
    )


def _lineage() -> dict[str, dict]:
    return {
        "day1_morning": {
            "goalId": "goal_campus",
            "sourceGoalId": "goal_campus",
            "occurrenceId": "occ:goal_campus:day:1",
            "poolId": "pool_campus",
            "requirementLevel": "required",
            "lineageAuthority": "goal_occurrence_compiler",
            "schedulePreference": {"dayPart": "morning", "sequence": 1},
        },
        "day1_afternoon": {
            "goalId": "goal_park",
            "sourceGoalId": "goal_park",
            "occurrenceId": "occ:goal_park:day:1",
            "poolId": "pool_park",
            "requirementLevel": "required",
            "lineageAuthority": "goal_occurrence_compiler",
            "schedulePreference": {"dayPart": "afternoon", "sequence": 2},
        },
    }


def _hint() -> dict:
    return {
        "dayNumber": 1,
        "intentType": "park",
        "experienceFamily": "park_relax",
        "queryHint": "公园",
        "durationEstimate": {"min": 45, "preferred": 60, "max": 90},
        "confidence": 0.8,
        "maxRouteAnchors": 4,
    }


def test_route_gap_hint_cannot_mutate_final_pair_only_assignment() -> None:
    map_provider = GapMapProvider()
    route_provider = GapRouteProvider()
    executor = SimpleOpenItineraryExecutor(
        map_poi_service=map_provider,
        route_assignment_service=SimpleOpenRouteAssignmentService(route_leg_provider=route_provider),
    )

    plans, events = executor.build_segment_plans(
        _initial_plan(),
        city="北京",
        transport_mode="transit",
        slot_lineage=_lineage(),
        authoritative_lineage_required=True,
        route_decision_contract=_route_contract(),
        route_budget=3,
        route_gap_supplement_hints=[_hint(), _hint(), _hint()],
    )

    supplements = [item for item in plans if item.lineage_authority == "simple_open_route_gap_supplement"]
    assert supplements == []
    assert map_provider.nearby_calls == 1
    assert route_provider.calls == [("B000ANCHOR1", "B000ANCHOR2")]
    route_assignment = plans[0].schedule_constraints["routeAssignment"]
    assert route_assignment["routeCoverageComplete"] is True
    assert route_assignment["routeProviderAttemptCount"] == 1
    assert len(route_assignment["verifiedPairs"]) == 1
    assert route_assignment["verifiedPairs"][0]["distanceMeters"] <= 5000
    assert not any(item["type"] == "simple_open_route_gap_search" for item in events)


def test_route_gap_hint_does_not_expand_final_route_budget() -> None:
    map_provider = GapMapProvider()
    route_provider = GapRouteProvider(insertion_minutes=80)
    executor = SimpleOpenItineraryExecutor(
        map_poi_service=map_provider,
        route_assignment_service=SimpleOpenRouteAssignmentService(route_leg_provider=route_provider),
    )

    plans, _events = executor.build_segment_plans(
        _initial_plan(),
        city="北京",
        transport_mode="transit",
        slot_lineage=_lineage(),
        authoritative_lineage_required=True,
        route_decision_contract=_route_contract(),
        route_budget=3,
        route_gap_supplement_hints=[_hint()],
    )

    assert not any(item.lineage_authority == "simple_open_route_gap_supplement" for item in plans)
    assert map_provider.nearby_calls == 1
    assert route_provider.calls == [("B000ANCHOR1", "B000ANCHOR2")]
    route_assignment = plans[0].schedule_constraints["routeAssignment"]
    assert route_assignment["routeCoverageComplete"] is True
    assert route_assignment["routeProviderAttemptCount"] == 1
    assert route_assignment["verifiedPairs"][0]["distanceMeters"] <= 5000

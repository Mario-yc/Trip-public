from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

from src.api.schemas.agent import AgentInitialPlanOutput
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.models.poi import POI
from src.models.poi_intent import PersistableSegmentPlan
from src.services.agent_service import AgentService
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor
from src.services.simple_open_route_assignment_service import SimpleOpenRouteAssignmentService


def _balanced_route_contract() -> dict:
    selected_tolerance = {
        "maxGeneralizedCostDelta": 35,
        "maxDetourRatio": 0.35,
    }
    return AgentService._compile_route_decision_contract(
        request_text="公交地铁优先",
        experience_specs=[],
        existing_contract={
            "detourTolerance": selected_tolerance,
            "detourToleranceSource": "controller_semantic_choice",
        },
        detour_option_policy={
            "source": "server_signed_clarification_option_set",
            "selectionRole": "non_strict",
            "selectionPolicy": "min_ratio_then_delta_then_option_id",
            "selectedOptionId": "opaque-balanced",
            "selectedOptionRank": 1,
            "selectedDetourTolerance": selected_tolerance,
            "optionCount": 3,
            "optionSetFingerprint": "a" * 64,
            "checkpointId": "clarify_route",
            "checkpointFingerprint": "b" * 64,
            "sourceAssistantTurnId": "turn_route_question",
        },
    )


def _plan(slot_id: str, *, sequence: int, intent_type: str) -> PersistableSegmentPlan:
    return PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="",
        duration_minutes=120,
        kind="campus" if intent_type == "campus_visit" else "night_view",
        route_anchor=True,
        selected_poi=None,
        display_title="待分配",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        transport_mode="transit",
        raw_need="高校参观" if intent_type == "campus_visit" else "北京夜景",
        intent_type=intent_type,
        goal_id=f"goal_{slot_id}",
        source_goal_id=f"goal_{slot_id}",
        occurrence_id=f"occ:{slot_id}:day:1",
        planning_slot_id=slot_id,
        pool_id=f"pool_{slot_id}",
        lineage_authority="goal_occurrence_compiler",
        requirement_level="hard",
        required=True,
        schedule_preference={"sequence": sequence},
    )


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
        type="风景名胜;旅游景点",
    )


class RecordingRouteProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def verified_leg(self, *, plan_id, left, right, transport_mode):
        del plan_id, transport_mode
        pair = (str(left["amapId"]), str(right["amapId"]))
        self.calls.append(pair)
        return {
            "fromAmapId": pair[0],
            "toAmapId": pair[1],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "transit",
            "distanceMeters": 12000,
            "durationSeconds": 3600,
            "walkingDistanceMeters": 200,
            "transferCount": 1,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "queriedAt": "2026-08-29T00:00:00+00:00",
        }


class TwoDayNearbyProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float | None, int | None]] = []

    @staticmethod
    def _candidate(
        *,
        city: str,
        category: str,
        amap_id: str,
        name: str,
        provider_type: str,
        provider_type_code: str | None = None,
        longitude: float,
        latitude: float,
        open_time: str,
        tags: list[str] | None = None,
    ) -> MapPoiResponse:
        return MapPoiResponse(
            id=amap_id,
            name=name,
            type=provider_type,
            city=city,
            district="测试区",
            address="测试地址",
            longitude=longitude,
            latitude=latitude,
            category=category,
            source="amap-place-search",
            sourceNote="provider-fixture",
            confidence=1.0,
            providerTypeCode=provider_type_code,
            tags=tags or [],
            openTimeToday=open_time,
        )

    @staticmethod
    def _response(
        city: str,
        keyword: str,
        category: str,
        pois: list[MapPoiResponse],
    ) -> MapPoiSearchResponse:
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=pois,
        )

    def search(self, city, keyword, category="all", limit=5, **_kwargs):
        del limit
        self.calls.append(("text", keyword, None, None))
        assert keyword == "高校参观"
        return self._response(
            city,
            keyword,
            category,
            [
                self._candidate(
                    city=city,
                    category=category,
                    amap_id="B000CAMPUS1",
                    name="第一测试大学",
                    provider_type="科教文化服务;学校;高等院校",
                    longitude=116.31,
                    latitude=39.99,
                    open_time="08:00-18:00",
                ),
                self._candidate(
                    city=city,
                    category=category,
                    amap_id="B000CAMPUS2",
                    name="第二测试大学",
                    provider_type="科教文化服务;学校;高等院校",
                    longitude=116.40,
                    latitude=39.90,
                    open_time="08:00-18:00",
                ),
            ],
        )

    def search_nearby(
        self,
        city,
        longitude,
        latitude,
        keyword,
        category="all",
        radius=1500,
        limit=5,
        **_kwargs,
    ):
        del limit
        self.calls.append(("nearby", keyword, float(longitude), int(radius)))
        first_day_anchor = float(longitude) < 116.35
        if keyword == "餐厅":
            return self._response(
                city,
                keyword,
                category,
                [
                    self._candidate(
                        city=city,
                        category=category,
                        amap_id="B000MEAL001" if first_day_anchor else "B000MEAL002",
                        name="第一天京味午餐" if first_day_anchor else "第二天京味午餐",
                        provider_type="餐饮服务;中餐厅;北京菜",
                        provider_type_code="050100",
                        tags=["北京菜", "京味小吃" if first_day_anchor else "传统面食"],
                        longitude=float(longitude) + 0.005,
                        latitude=float(latitude),
                        open_time="10:00-22:00",
                    )
                ],
            )
        assert keyword == "北京 滨水夜景"
        return self._response(
            city,
            keyword,
            category,
            [
                self._candidate(
                    city=city,
                    category=category,
                    amap_id="B000NIGHT01" if first_day_anchor else "B000NIGHT02",
                    name="第一天滨水夜景步道" if first_day_anchor else "第二天滨水夜景步道",
                    provider_type="风景名胜;水域景观;滨水步道",
                    longitude=float(longitude) + 0.010,
                    latitude=float(latitude),
                    open_time="00:00-23:59",
                )
            ],
        )


def test_balanced_detour_keeps_choice_semantics_and_adds_product_quality_floor() -> None:
    contract = _balanced_route_contract()

    assert contract["status"] == "ready"
    assert contract["detourTolerance"] == {
        "maxGeneralizedCostDelta": 35.0,
        "maxDetourRatio": 0.35,
    }
    assert contract["detourToleranceSource"] == "controller_semantic_choice"
    assert contract["schemaVersion"] == "route-decision-contract-v2"
    assert contract["adjacentLegConstraint"] == {
        "candidateSearchRadiusMeters": 8000.0,
        "maxProviderTravelMinutes": 60.0,
    }
    assert contract["adjacentLegConstraintSource"] == "versioned_product_travel_quality_floor"
    assert contract["provenance"]["travelQualityFloor"]["doesNotChangeDetourChoice"] is True


def test_balanced_quality_floor_rejects_ten_plus_kilometre_leg_before_route_provider() -> None:
    provider = RecordingRouteProvider()
    result = SimpleOpenRouteAssignmentService(route_leg_provider=provider).assign(
        [
            _plan("campus", sequence=1, intent_type="campus_visit"),
            _plan("night", sequence=2, intent_type="night_view"),
        ],
        {
            "campus": [_poi("B000CAMPUS1", "测试高校", 116.31, 39.99)],
            "night": [_poi("B000NIGHT01", "远端夜景", 116.45, 39.99)],
        },
        route_decision_contract=_balanced_route_contract(),
        transport_mode="transit",
        route_budget=1,
    )

    assert result.audit["failureReason"] == "topology_constraint_exceeded"
    assert result.audit["topologyCompliance"] == "failed"
    assert result.audit["routeProviderAttemptCount"] == 0
    assert provider.calls == []


def test_public_outdoor_night_contract_rejects_theme_park_and_controlled_tower() -> None:
    policy = {
        "experienceFamilies": ["public_city_view", "waterfront_evening"],
        "accessPolicy": "public_outdoor",
    }
    theme_park = {
        "id": "B000A7N4BI",
        "name": "北京欢乐谷",
        "type": "风景名胜;主题公园",
        "city": "北京",
        "longitude": 116.494743,
        "latitude": 39.867355,
        "source": "amap-place-search",
    }
    controlled_tower = {
        "id": "B0JDPFC6DW",
        "name": "中央广播电视塔观景台",
        "type": "风景名胜;观景点",
        "city": "北京",
        "longitude": 116.30028,
        "latitude": 39.91894,
        "source": "amap-place-search",
    }
    public_waterfront = {
        "id": "B0LIANGMA1",
        "name": "亮马河国际风情水岸滨水夜景步道",
        "type": "风景名胜;水域景观;滨水步道",
        "city": "北京",
        "longitude": 116.471,
        "latitude": 39.952,
        "source": "amap-place-search",
    }

    assert (
        SimpleOpenItineraryExecutor._candidate_type_matches_intent(
            theme_park,
            "night_view",
            experience_policy=policy,
        )
        is False
    )
    assert (
        SimpleOpenItineraryExecutor._candidate_type_matches_intent(
            controlled_tower,
            "night_view",
            experience_policy=policy,
        )
        is False
    )
    assert (
        SimpleOpenItineraryExecutor._candidate_type_matches_intent(
            public_waterfront,
            "night_view",
            experience_policy=policy,
        )
        is True
    )


def test_public_outdoor_night_contract_rewrites_generic_query_from_authoritative_policy() -> None:
    query = SimpleOpenItineraryExecutor._safe_query_for_intent(
        "night view",
        city="北京市",
        intent_type="night_view",
        experience_policy={
            "experienceFamilies": ["public_city_view", "waterfront_evening"],
            "accessPolicy": "public_outdoor",
        },
    )

    assert query == "北京 滨水夜景"


def test_explicit_public_park_or_waterfront_night_walk_is_one_disjunctive_goal_per_day() -> None:
    service = AgentService(sqlite3.connect(":memory:"))
    contract = service._request_intent_contract(
        (
            "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，"
            "每天参观一所不同的985高校，每天中午品尝不同的当地网红美食，"
            "每天晚上去当日附近公共开放的公园或滨水夜景散步"
        ),
        {
            "status": "resolved",
            "dayCount": 2,
            "dates": ["2026-10-01", "2026-10-02"],
            "startDate": "2026-10-01",
            "endDate": "2026-10-02",
        },
        SimpleNamespace(meal_slots=[]),
        city="北京",
        simple_open_profile=True,
    )

    required_by_intent = {str(item.get("intentType") or ""): item for item in contract["requiredIntents"]}
    assert "park" not in required_by_intent
    assert required_by_intent["night_view"]["requiredMin"] == 2
    assert required_by_intent["night_view"]["allowedDayNumbers"] == [1, 2]

    night_spec = next(item for item in contract["experienceSpecs"] if item["intentType"] == "night_view")
    assert night_spec["frequency"] == "every_allowed_day"
    assert night_spec["allowedDayNumbers"] == [1, 2]
    assert night_spec["experienceFamilies"] == ["park_relax", "waterfront_evening"]
    assert night_spec["accessPolicy"] == "public_outdoor"
    assert night_spec["distinctnessPolicy"] == "distinct_physical_identity_per_occurrence"
    assert night_spec["timeWindow"] == {"dayPart": "evening"}
    assert "night_view.detourTolerance" in night_spec["unresolvedDimensions"]
    assert not any(
        str(item.get("dimensionId") or "").startswith("night_view.") for item in contract["clarificationDimensions"]
    )


def test_explicit_public_park_night_policy_uses_one_nearby_query_and_admits_public_park() -> None:
    policy = {
        "experienceFamilies": ["park_relax", "waterfront_evening"],
        "accessPolicy": "public_outdoor",
    }
    query = SimpleOpenItineraryExecutor._safe_query_for_intent(
        "night view",
        city="北京市",
        intent_type="night_view",
        experience_policy=policy,
    )
    natural_language_query = SimpleOpenItineraryExecutor._safe_query_for_intent(
        "公共开放的公园或滨水夜景散步",
        city="北京市",
        intent_type="night_view",
        experience_policy=policy,
    )
    exact_entity_query = SimpleOpenItineraryExecutor._safe_query_for_intent(
        "奥林匹克森林公园",
        city="北京市",
        intent_type="night_view",
        experience_policy=policy,
        preserve_exact_entity=True,
    )
    public_park = {
        "id": "B0KUFC8T5V",
        "name": "中海体育公园",
        "type": "风景名胜;公园广场;公园",
        "city": "北京",
        "longitude": 116.315203,
        "latitude": 39.986339,
        "source": "amap-place-search",
    }
    theme_park = {
        "id": "B000A7N4BI",
        "name": "北京欢乐谷",
        "type": "风景名胜;主题公园",
        "city": "北京",
        "longitude": 116.494743,
        "latitude": 39.867355,
        "source": "amap-place-search",
    }

    assert query == "公园"
    assert natural_language_query == "公园"
    assert exact_entity_query == "奥林匹克森林公园"
    assert (
        SimpleOpenItineraryExecutor._candidate_type_matches_intent(
            public_park,
            "night_view",
            experience_policy=policy,
        )
        is True
    )
    assert (
        SimpleOpenItineraryExecutor._candidate_type_matches_intent(
            theme_park,
            "night_view",
            experience_policy=policy,
        )
        is False
    )


def test_separate_daytime_park_and_night_view_clauses_remain_two_goals() -> None:
    service = AgentService(sqlite3.connect(":memory:"))
    contract = service._request_intent_contract(
        "10月1日北京一日游，白天去公共开放的城市公园，晚上去公共开放的公园或滨水夜景散步",
        {
            "status": "resolved",
            "dayCount": 1,
            "dates": ["2026-10-01"],
            "startDate": "2026-10-01",
            "endDate": "2026-10-01",
        },
        SimpleNamespace(meal_slots=[]),
        city="北京",
        simple_open_profile=True,
    )

    required_intents = {str(item.get("intentType") or "") for item in contract["requiredIntents"]}
    assert {"park", "night_view"}.issubset(required_intents)


def test_two_day_balanced_plan_keeps_day_seed_locality_and_advances_real_predecessor() -> None:
    slots: list[dict] = []
    pools: list[dict] = []
    lineage: dict[str, dict] = {}
    for day_number in (1, 2):
        date = f"2026-10-0{day_number}"
        for sequence, (kind, intent_type, raw_need) in enumerate(
            (
                ("campus", "campus_visit", "高校参观"),
                ("meal", "meal", "当地特色午餐"),
                ("night_view", "night_view", "night view"),
            ),
            start=1,
        ):
            slot_id = f"day{day_number}_{intent_type}"
            slots.append(
                {
                    "slotId": slot_id,
                    "dayNumber": day_number,
                    "date": date,
                    "startTime": "",
                    "durationMinutes": 90,
                    "kind": kind,
                    "rawNeed": raw_need,
                    "routeAnchor": True,
                }
            )
            lineage[slot_id] = {
                "goalId": f"goal_{intent_type}",
                "sourceGoalId": f"goal_{intent_type}",
                "occurrenceId": f"occ:goal_{intent_type}:day:{day_number}",
                "poolId": f"pool_{intent_type}",
                "planningSlotId": slot_id,
                "dayNumber": day_number,
                "requirementLevel": "hard",
                "lineageAuthority": "goal_occurrence_compiler",
                "futureRouteAnchor": True,
                "routeAnchorExpected": True,
                "schedulePreference": {
                    "sequence": sequence,
                    "dayPart": "morning" if sequence == 1 else "noon" if sequence == 2 else "evening",
                },
            }
    for intent_type, raw_need in (
        ("campus_visit", "高校参观"),
        ("meal", "当地特色午餐"),
        ("night_view", "night view"),
    ):
        pools.append(
            {
                "poolId": f"pool_{intent_type}",
                "rawNeed": raw_need,
                "city": "北京",
                "intentType": intent_type,
                "targetCount": 2,
                "requirementLevel": "required",
                "assignToSlots": [f"day1_{intent_type}", f"day2_{intent_type}"],
                "candidateHints": [raw_need],
            }
        )
    initial_plan = AgentInitialPlanOutput.model_validate(
        {
            "reply": "两日高校、午餐和公共夜景",
            "mode": "day_slots",
            "daySlots": slots,
            "intentPools": pools,
        }
    )
    provider = TwoDayNearbyProvider()

    plans, _events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial_plan,
        city="北京",
        transport_mode="transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        route_decision_contract=_balanced_route_contract(),
        experience_policies_by_intent={
            "night_view": {
                "intentType": "night_view",
                "experienceFamilies": ["public_city_view", "waterfront_evening"],
                "accessPolicy": "public_outdoor",
            }
        },
        route_budget=0,
    )

    assert all(plan.selected_poi is not None for plan in plans), [
        (plan.day_number, plan.intent_type, plan.notes) for plan in plans
    ]
    assert provider.calls == [
        ("text", "高校参观", None, None),
        ("nearby", "餐厅", 116.31, 5000),
        ("nearby", "北京 滨水夜景", 116.315, 5000),
        ("nearby", "餐厅", 116.4, 5000),
        ("nearby", "北京 滨水夜景", 116.405, 5000),
    ]
    by_day = {day_number: [plan for plan in plans if plan.day_number == day_number] for day_number in (1, 2)}
    assert {day_number: [plan.intent_type for plan in day_plans] for day_number, day_plans in by_day.items()} == {
        1: ["campus_visit", "meal", "night_view"],
        2: ["campus_visit", "meal", "night_view"],
    }
    route_audit = plans[0].schedule_constraints["routeAssignment"]
    assert route_audit["topologyCompliance"] == "verified"
    assert route_audit["failureReason"] == "provider_route_budget_insufficient"
    assert route_audit["routeProviderAttemptCount"] == 0

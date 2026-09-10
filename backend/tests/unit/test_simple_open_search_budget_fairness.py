from __future__ import annotations

from datetime import datetime, timezone

from src.api.schemas.agent import AgentInitialPlanOutput
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.services.simple_open_itinerary_executor import (
    MAX_SIMPLE_OPEN_POI_SEARCHES,
    SimpleOpenItineraryExecutor,
)


def _poi(
    *,
    amap_id: str,
    name: str,
    provider_type: str,
    provider_type_code: str | None = None,
    longitude: float,
    latitude: float = 39.99,
) -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type=provider_type,
        city="北京",
        district="海淀区",
        address=f"{name}测试地址",
        longitude=longitude,
        latitude=latitude,
        category="all",
        source="amap-place-search",
        sourceNote="trace-shaped provider fixture",
        confidence=1.0,
        providerTypeCode=provider_type_code,
        openTimeToday="08:00-23:00",
    )


class _TraceShapedProvider:
    def __init__(self) -> None:
        self.keywords: list[str] = []
        self.meal_call_count = 0

    def search_nearby(
        self,
        city: str,
        longitude: float,
        latitude: float,
        keyword: str,
        category: str = "all",
        limit: int = 5,
        **_kwargs: object,
    ) -> MapPoiSearchResponse:
        del longitude, latitude
        return self.search(city, keyword, category=category, limit=limit)

    def search(self, city: str, keyword: str, category: str = "all", limit: int = 5) -> MapPoiSearchResponse:
        del limit
        self.keywords.append(keyword)
        if keyword == "高校参观":
            pois = [
                _poi(
                    amap_id="B0FFF0EFZY",
                    name="清华大学工字厅",
                    provider_type="科教文化服务;学校;高等院校",
                    longitude=116.326,
                ),
                _poi(
                    amap_id="B0FFHL2A77",
                    name="北京英国学校顺义校区",
                    provider_type="科教文化服务;学校;中学",
                    longitude=116.520,
                ),
            ]
        elif keyword == "北京 高等院校 校区":
            pois = [
                _poi(
                    amap_id="B0FFFDCTTI",
                    name="中央财经大学沙河校区西校区",
                    provider_type="科教文化服务;学校;高等院校",
                    longitude=116.279,
                    latitude=40.166,
                )
            ]
        elif keyword == "night view":
            pois = []
        elif keyword == "北京 夜景 观景台":
            pois = [
                _poi(
                    amap_id="B0JDPFC6DW",
                    name="中央广播电视塔观景台",
                    provider_type="风景名胜;风景名胜;观景点",
                    longitude=116.330,
                    latitude=39.995,
                )
            ]
        elif keyword == "餐厅":
            self.meal_call_count += 1
            pois = [
                _poi(
                    amap_id=f"B000MEAL0{self.meal_call_count}",
                    name=f"北京风味餐厅{self.meal_call_count}",
                    provider_type="餐饮服务;中餐厅;北京菜",
                    provider_type_code="050100",
                    longitude=116.332 if self.meal_call_count == 1 else 116.280,
                    latitude=39.995 if self.meal_call_count == 1 else 40.165,
                )
            ]
        else:
            raise AssertionError(f"unexpected query: {keyword}")
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            cacheHit=keyword == "高校参观" and self.keywords.count(keyword) > 1,
            pois=pois,
        )


def test_authoritative_explicit_soft_meals_are_not_starved_by_repeated_hard_query() -> None:
    slots = [
        ("day1_goal_campus_visit_1", 1, "campus", "高校参观", "campus_visit", "hard", 1),
        ("day1_goal_night_view_2", 1, "night_view", "night view", "night_view", "hard", 2),
        ("day1_goal_meal_3", 1, "meal", "当地特色美食", "meal", "explicit_soft", 3),
        ("day2_goal_campus_visit_1", 2, "campus", "高校参观", "campus_visit", "hard", 1),
        ("day2_goal_meal_2", 2, "meal", "当地特色美食", "meal", "explicit_soft", 2),
    ]
    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "V4 trace-shaped deterministic slot fallback",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": slot_id,
                    "dayNumber": day,
                    "date": f"2026-10-0{day}",
                    "timeWindow": "",
                    "startTime": "",
                    "durationMinutes": 0,
                    "kind": kind,
                    "rawNeed": raw_need,
                    "routeAnchor": intent != "meal",
                }
                for slot_id, day, kind, raw_need, intent, _level, _sequence in slots
            ],
            "intentPools": [
                {
                    "poolId": f"goal_{intent}_pool",
                    "rawNeed": raw_need,
                    "city": "北京",
                    "intentType": intent,
                    "targetCount": sum(1 for item in slots if item[4] == intent),
                    "requirementLevel": "optional",
                    "assignToSlots": [item[0] for item in slots if item[4] == intent],
                    "candidateHints": [],
                }
                for intent, raw_need in (
                    ("campus_visit", "高校参观"),
                    ("night_view", "night view"),
                    ("meal", "当地特色美食"),
                )
            ],
        }
    )
    lineage = {
        slot_id: {
            "goalId": f"goal_{intent}",
            "sourceGoalId": f"goal_{intent}",
            "occurrenceId": f"occ:goal_{intent}:day:{day}",
            "poolId": f"goal_{intent}_pool",
            "planningSlotId": slot_id,
            "dayNumber": day,
            "intentType": intent,
            "requirementLevel": level,
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": intent != "meal",
            "routeAnchorExpected": intent != "meal",
            "schedulePreference": {
                "sequence": sequence,
                "sequenceSource": "controller_schedule_hint",
            },
        }
        for slot_id, day, _kind, _raw_need, intent, level, sequence in slots
    }
    provider = _TraceShapedProvider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="public_transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        route_decision_contract={
            "status": "ready",
            "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000},
        },
    )

    assert provider.keywords == [
        "高校参观",
        "night view",
        "北京 夜景 观景台",
        "餐厅",
        "北京 高等院校 校区",
        "餐厅",
    ]
    assert len(provider.keywords) == MAX_SIMPLE_OPEN_POI_SEARCHES
    assert [plan.planning_slot_id for plan in plans if plan.selected_poi is not None] == [item[0] for item in slots]
    assert [
        plan.selected_poi.amap_id for plan in plans if plan.intent_type == "meal" and plan.selected_poi is not None
    ] == ["B000MEAL01", "B000MEAL02"]
    diversified = [
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and (event.get("metadata") or {}).get("queryRole") == "budget_preserving_primary_alternative"
    ]
    assert len(diversified) == 1
    assert diversified[0]["detail"] == "北京 高等院校 校区"
    assert diversified[0]["metadata"]["originalQueryFingerprint"]
    assert diversified[0]["metadata"]["queryFingerprint"] != diversified[0]["metadata"]["originalQueryFingerprint"]


def test_citywide_explicit_soft_night_does_not_spend_hard_alternative_budget() -> None:
    class ExplicitSoftNightProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(
            self,
            city: str,
            keyword: str,
            category: str = "all",
            limit: int = 5,
        ) -> MapPoiSearchResponse:
            del limit
            self.keywords.append(keyword)
            pois = [
                _poi(
                    amap_id="B000PHOTO01",
                    name="夜景摄影工作室",
                    provider_type="生活服务;摄影冲印店;摄影冲印",
                    longitude=116.33,
                )
            ]
            if keyword == "北京 夜景 观景台":
                pois = [
                    _poi(
                        amap_id="B000NIGHT01",
                        name="城市夜景观景台",
                        provider_type="风景名胜;风景名胜;观景点",
                        longitude=116.34,
                    )
                ]
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=pois,
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "optional night",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_optional_night",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "timeWindow": "19:00-21:00",
                    "startTime": "19:00",
                    "durationMinutes": 90,
                    "kind": "night_view",
                    "rawNeed": "night view",
                    "routeAnchor": True,
                }
            ],
            "intentPools": [
                {
                    "poolId": "night_pool",
                    "rawNeed": "night view",
                    "city": "北京",
                    "intentType": "night_view",
                    "targetCount": 1,
                    "requirementLevel": "optional",
                    "assignToSlots": ["day1_optional_night"],
                    "candidateHints": ["night view"],
                }
            ],
        }
    )
    provider = ExplicitSoftNightProvider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="public_transit",
        slot_lineage={
            "day1_optional_night": {
                "goalId": "goal_night_view",
                "sourceGoalId": "goal_night_view",
                "occurrenceId": "occ:goal_night_view:day:1",
                "poolId": "night_pool",
                "planningSlotId": "day1_optional_night",
                "dayNumber": 1,
                "intentType": "night_view",
                "requirementLevel": "explicit_soft",
                "lineageAuthority": "goal_occurrence_compiler",
                "futureRouteAnchor": True,
                "routeAnchorExpected": True,
            }
        },
        authoritative_lineage_required=True,
    )

    assert provider.keywords == ["night view"]
    assert plans[0].selected_poi is None
    assert not any(
        event["type"] == "simple_open_tool_call"
        and (event.get("metadata") or {}).get("queryRole") == "safe_alternative"
        for event in events
    )

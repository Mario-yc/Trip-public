import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import HTTPException

from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.models.inspiration_set import InspirationSet
from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.models.poi_intent import PoiIntent
from src.models.poi_risk_alert import POIRiskAlert
from src.models.route_option import RouteOption
from src.services.functional_slot_context_service import FunctionalSlotContext
from src.services.itinerary_service import ItineraryService
from src.services.ticket_service import TicketService
from src.services.amap_call_budget import AmapCallBudget, amap_call_budget_scope
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService, clear_map_poi_runtime_state
from src.services.poi_risk_service import POIRiskService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.weather_service import WeatherService


def test_legacy_route_readiness_requires_contract_bound_provider_matrix():
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"requestFingerprint": "legacy-route-readiness-test"},
        detour_tolerance={
            "maxGeneralizedCostDelta": 28.0,
            "maxDetourRatio": 0.22,
        },
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 1.5,
            "transferPenaltyMinutes": 4.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert contract is not None

    assert (
        ItineraryService._planning_provider_matrix_verified(
            {"routeDecisionContract": contract},
            contract,
        )
        is False
    )
    assert (
        ItineraryService._planning_provider_matrix_verified(
            {
                "routeDecisionContract": contract,
                "routeInsertionProofs": [
                    {
                        "status": "passed",
                        "networkVerified": True,
                        "timeWindowFeasible": True,
                        "routeDecisionContract": contract,
                        "legs": {"previousToCandidate": {"provider": "amap-webservice"}},
                    }
                ],
            },
            contract,
        )
        is True
    )


def map_poi(
    poi_id: str,
    name: str,
    *,
    longitude: float,
    latitude: float,
    city: str = "示例市",
    district: str = "示例区",
    category: str = "food",
    poi_type: str = "餐饮服务;中餐厅",
    meal_family: Optional[str] = None,
) -> MapPoiResponse:
    source_claims = [{"claimKey": "local_food", "stance": "support", "locality": city}] if category == "food" else []
    if meal_family:
        source_claims.append(
            {
                "claimKey": "meal_family",
                "value": meal_family,
                "stance": "support",
            }
        )
    return MapPoiResponse(
        id=poi_id,
        name=name,
        type=poi_type,
        city=city,
        district=district,
        address=f"{district}测试路",
        longitude=longitude,
        latitude=latitude,
        category=category,
        source=AMAP_PLACE_SOURCE,
        sourceNote="来源：高德地图",
        providerTypeCode="050100" if category == "food" else None,
        tags=["地方风味"] if category == "food" else [],
        sourceClaims=source_claims,
        confidence=0.92,
        photos=[],
    )


class RecordingMapPoiService:
    def __init__(self, citywide: list[MapPoiResponse], nearby: list[MapPoiResponse]):
        self.citywide = citywide
        self.nearby = nearby
        self.search_calls: list[tuple[str, str]] = []
        self.nearby_calls: list[tuple[str, float, float]] = []
        self.calls: list[tuple[str, str]] = []

    def search(self, city: str, keyword: str = "", category: str = "all", limit: int = 12) -> MapPoiSearchResponse:
        self.search_calls.append((keyword, category))
        self.calls.append(("citywide", keyword))
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=self.citywide,
        )

    def search_nearby(
        self,
        city: str,
        longitude: float,
        latitude: float,
        keyword: str,
        category: str = "all",
        radius: int = 1500,
        limit: int = 12,
    ) -> MapPoiSearchResponse:
        self.nearby_calls.append((keyword, longitude, latitude))
        self.calls.append(("nearby", keyword))
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=self.nearby,
        )


def test_functional_meal_wrong_family_nearby_continues_to_bounded_citywide_search():
    wrong_family = map_poi(
        "amap_zhajiangmian",
        "方砖厂69号炸酱面",
        longitude=116.401,
        latitude=39.912,
        city="北京",
        meal_family="zhajiangmian",
    )
    matching_family = map_poi(
        "amap_luzhu",
        "门框胡同百年卤煮",
        longitude=116.397,
        latitude=39.905,
        city="北京",
        meal_family="luzhu",
    )
    map_service = RecordingMapPoiService(
        citywide=[matching_family],
        nearby=[wrong_family],
    )
    service = ItineraryService(sqlite3.connect(":memory:"), map_poi_service=map_service)
    anchor = POI(
        id="poi_anchor",
        name="天坛公园",
        city="北京",
        category="scenic",
        latitude=39.882,
        longitude=116.407,
        source=AMAP_PLACE_SOURCE,
        confidence=0.95,
        amap_id="amap_anchor",
        type="风景名胜;公园",
    )
    intent = PoiIntent(
        raw_need="当地特色午餐",
        city="北京",
        day_number=2,
        time_window="12:00-13:00",
        intent_type="meal",
        specificity="functional",
        search_queries=["北京 卤煮"],
        preferred_types=["餐饮服务"],
        rejected_types=[],
        candidate_hints=["北京 卤煮"],
        hint_policy="meal_experience_assignment",
        assigned_meal_family="luzhu",
        target_count=1,
    )

    candidates, provider_state = service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
        slot_context=FunctionalSlotContext(
            slot_id="day2_lunch",
            day_number=2,
            intent_type="meal",
            raw_need="当地特色午餐",
            previous_anchor=anchor,
            next_anchor=None,
            same_day_anchors=[anchor],
        ),
    )

    assert provider_state == "ok"
    assert map_service.nearby_calls
    assert map_service.search_calls == [("北京 卤煮", "food")]
    assert {item.id for item in candidates} == {"amap_zhajiangmian", "amap_luzhu"}
    stats = service._last_candidate_collection_stats
    assert stats["rejectedReasonCounts"]["assigned_meal_family_mismatch"] >= 1
    assert stats["uniqueEligibleEntityCount"] >= 1
    assert stats["poolBudget"]["textSearchMax"] == 2


def test_collection_optional_experience_family_rejects_unrelated_provider_type():
    service = ItineraryService(sqlite3.connect(":memory:"))
    intent = PoiIntent(
        raw_need="体验本地生活街区",
        city="北京",
        day_number=2,
        time_window="14:00-16:00",
        intent_type="area_walk",
        specificity="functional",
        search_queries=["北京 本地生活街区"],
        preferred_types=["风景名胜"],
        rejected_types=[],
        optional_experience_family="local_life",
    )
    zoo = map_poi(
        "amap_zoo",
        "北京动物园",
        longitude=116.337,
        latitude=39.938,
        city="北京",
        category="scenic",
        poi_type="风景名胜;公园广场;动物园",
    )

    reasons = service._collection_candidate_rejection_reasons(intent, zoo)

    assert "area_walk_provider_type_mismatch" in reasons


def test_museum_intent_uses_museum_search_category():
    service = ItineraryService(sqlite3.connect(":memory:"))

    assert service._intent_search_category("museum") == "museum"


class DebugRateLimitedMapPoiService:
    def __init__(self):
        self.search_calls = 0

    def search(self, city: str, keyword: str = "", category: str = "all", limit: int = 12) -> MapPoiSearchResponse:
        self.search_calls += 1
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_rate_limited",
                "message": "高德 POI 查询暂时受限，请稍后重试。",
                "providerName": "amap",
                "retryAfterSeconds": 90,
                "debug": {
                    "endpoint": "place/text",
                    "source": "place/text",
                    "httpStatusCode": 429,
                    "rawInfo": "HTTP 429",
                    "classifiedReason": "http_429",
                    "retryAfterSeconds": 90,
                    "requestParams": {"keywords": keyword, "city": "110000"},
                    "processId": 12345,
                    "cacheHit": False,
                    "localCooldown": False,
                },
            },
        )


class DebugClassifiedMapPoiService:
    def __init__(self, classified_reason: str):
        self.classified_reason = classified_reason
        self.search_calls = 0

    def search(self, city: str, keyword: str = "", category: str = "all", limit: int = 12) -> MapPoiSearchResponse:
        self.search_calls += 1
        code = "provider_down" if self.classified_reason == "provider_down" else "provider_rate_limited"
        raise HTTPException(
            status_code=502,
            detail={
                "code": code,
                "message": "AMap POI search failed: SERVICE_BUSY，请稍后重试",
                "providerName": "amap",
                "debug": {
                    "endpoint": "place/text",
                    "source": "place/text",
                    "rawInfo": "SERVICE_BUSY，请稍后重试",
                    "classifiedReason": self.classified_reason,
                    "requestParams": {"keywords": keyword, "city": "110000"},
                    "processId": 12345,
                    "cacheHit": False,
                    "localCooldown": self.classified_reason == "local_cooldown",
                },
            },
        )


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM traffic_crowding_signals;
            DELETE FROM weather_signals;
            DELETE FROM route_options;
            DELETE FROM ticket_lookup_results;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            DELETE FROM extraction_results;
            DELETE FROM source_materials;
            DELETE FROM inspiration_sets;
            DELETE FROM preference_summary_cards;
            DELETE FROM preference_profiles;
            """
        )


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def test_build_segments_persists_canonical_semantic_metadata_at_creation_time():
    poi = POI(
        id="poi_semantic",
        name="清华大学",
        city="北京",
        category="scenic",
        latitude=40.0,
        longitude=116.3,
        source=AMAP_PLACE_SOURCE,
        amap_id="B0THU",
        type="科教文化服务;高等院校",
    )
    with open_db() as connection:
        segment = ItineraryService(connection)._build_segments(
            "day_semantic",
            [poi],
            SimpleNamespace(id="weather_semantic"),
            [],
        )[0]

    assert segment.semantic_metadata["intentType"] == "campus_visit"
    assert segment.semantic_metadata["groundingStatus"] == "verified_amap"
    assert segment.semantic_metadata["routeAnchor"] is True
    assert segment.semantic_metadata["aliases"] == ["清华大学"]


def test_route_anchor_meal_candidate_collection_skips_citywide_when_nearby_succeeds():
    clear_database()
    far_citywide = map_poi("city_far", "城市远处特色餐厅", longitude=120.8, latitude=30.8)
    near_candidate = map_poi("near_route", "路线附近传统餐厅", longitude=120.05, latitude=30.0)
    map_service = RecordingMapPoiService(citywide=[far_citywide], nearby=[near_candidate])
    intent = PoiIntent(
        raw_need="午餐体验当地美食",
        city="示例市",
        day_number=1,
        time_window="12:00-13:00",
        intent_type="meal",
        specificity="functional",
        search_queries=["当地美食"],
        preferred_types=["餐饮"],
        rejected_types=["酒店"],
        candidate_hints=["特色餐厅"],
        hint_policy="llm_common_knowledge_hint",
        target_count=1,
    )
    context = FunctionalSlotContext(
        slot_id="day1_lunch",
        day_number=1,
        intent_type="meal",
        raw_need=intent.raw_need,
        previous_anchor=POI(
            id="prev", name="上午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.0
        ),
        next_anchor=POI(id="next", name="下午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.1),
        same_day_anchors=[],
        transport_mode="transit",
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
            slot_context=context,
        )

    assert provider_state == "ok"
    assert map_service.nearby_calls
    assert map_service.search_calls == []
    assert any(candidate.id == "near_route" for candidate in candidates)
    assert service._last_candidate_collection_stats["nearbyCandidateCount"] >= 1
    assert service._last_candidate_collection_stats["citywideCandidateCount"] == 0
    assert service._last_candidate_collection_stats["uniqueEligibleEntityCount"] == 1
    assert service._last_candidate_collection_stats["earlyStopReason"] == "unique_eligible_stop_count_reached"


def test_density_expand_without_persisted_hints_uses_raw_night_need_before_broad_queries():
    clear_database()
    nearby = map_poi(
        "near_night",
        "什刹海观景点",
        longitude=116.389,
        latitude=39.941,
        city="北京",
        district="西城区",
        category="scenic",
        poi_type="风景名胜;观景台;城市观景点",
    )
    map_service = RecordingMapPoiService(citywide=[], nearby=[nearby])
    intent = PoiIntent(
        raw_need="晚上看北京夜景",
        city="北京",
        day_number=1,
        time_window="night",
        intent_type="night_view",
        specificity="functional",
        search_queries=["地标", "观景点"],
        preferred_types=["地标", "观景点"],
        candidate_hints=[],
        target_count=1,
    )
    context = FunctionalSlotContext(
        slot_id="day1_night",
        day_number=1,
        intent_type="night_view",
        raw_need=intent.raw_need,
        previous_anchor=POI(
            id="prev",
            name="高校",
            city="北京",
            category="campus",
            latitude=39.94,
            longitude=116.36,
        ),
        next_anchor=None,
        same_day_anchors=[],
        transport_mode="transit",
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        service._expand_density_nearby = True
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
            slot_context=context,
        )

    assert provider_state == "ok"
    assert map_service.nearby_calls
    assert map_service.nearby_calls[0][0] == "晚上看北京夜景"
    assert map_service.calls[0] == ("nearby", "晚上看北京夜景")
    assert [candidate.id for candidate in candidates] == ["near_night"]


def test_route_anchor_meal_candidate_collection_continues_route_context_when_nearby_below_pool_target():
    clear_database()
    far_citywide = map_poi("city_far", "城市远处特色餐厅", longitude=120.8, latitude=30.8)
    near_candidate = map_poi("near_route", "路线附近传统小吃", longitude=120.05, latitude=30.0)
    map_service = RecordingMapPoiService(citywide=[far_citywide], nearby=[near_candidate])
    intent = PoiIntent(
        raw_need="午餐 当地特色美食 / 晚餐 当地特色美食",
        city="示例市",
        day_number=1,
        time_window="12:00-13:00",
        intent_type="meal",
        specificity="functional",
        search_queries=["当地美食", "特色餐厅"],
        preferred_types=["餐饮"],
        rejected_types=["酒店"],
        candidate_hints=["特色餐厅"],
        hint_policy="llm_common_knowledge_hint",
        target_count=4,
    )
    context = FunctionalSlotContext(
        slot_id="day1_lunch",
        day_number=1,
        intent_type="meal",
        raw_need="午餐 当地特色美食",
        previous_anchor=POI(
            id="prev", name="上午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.0
        ),
        next_anchor=POI(id="next", name="下午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.1),
        same_day_anchors=[],
        transport_mode="transit",
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
            slot_context=context,
        )

    stats = service._last_candidate_collection_stats
    assert provider_state == "ok"
    assert [candidate.id for candidate in candidates] == ["near_route"]
    assert map_service.search_calls == []
    assert len(map_service.nearby_calls) == 2
    assert stats["earlyStopReason"] == "per_slot_around_max_reached"
    assert stats["nearbyCandidateCount"] == 1
    assert stats["citywideCandidateCount"] == 0
    assert stats["uniqueEligibleEntityCount"] == 1


def test_meal_candidate_collection_limits_nearby_calls_per_slot():
    clear_database()
    map_service = RecordingMapPoiService(citywide=[], nearby=[])
    intent = PoiIntent(
        raw_need="午餐体验当地美食",
        city="示例市",
        day_number=1,
        time_window="12:00-13:00",
        intent_type="meal",
        specificity="functional",
        search_queries=["当地美食", "本地菜", "特色餐厅"],
        preferred_types=["餐饮"],
        rejected_types=["酒店"],
        candidate_hints=["特色餐厅", "传统餐厅"],
        hint_policy="llm_common_knowledge_hint",
        target_count=1,
    )
    context = FunctionalSlotContext(
        slot_id="day1_lunch",
        day_number=1,
        intent_type="meal",
        raw_need=intent.raw_need,
        previous_anchor=POI(
            id="prev", name="上午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.0
        ),
        next_anchor=POI(id="next", name="下午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.1),
        same_day_anchors=[],
        transport_mode="transit",
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
            slot_context=context,
        )

    stats = service._last_candidate_collection_stats
    assert provider_state == "ok"
    assert candidates == []
    assert len(map_service.nearby_calls) == 2
    assert len(map_service.search_calls) == 2
    assert stats["poolBudget"]["perSlotAroundMax"] == 2
    assert stats["usedAroundSearch"] == 2
    assert stats["earlyStopReason"] == "text_search_max_reached"
    assert stats["usedTextSearch"] == 2


def test_meal_candidate_collection_stops_after_unique_eligible_threshold():
    clear_database()
    nearby = [
        map_poi(f"near_{index}", f"路线附近餐厅{index}", longitude=120.01 + index * 0.001, latitude=30.0)
        for index in range(9)
    ]
    map_service = RecordingMapPoiService(citywide=[], nearby=nearby)
    intent = PoiIntent(
        raw_need="午餐体验当地美食",
        city="示例市",
        day_number=1,
        time_window="12:00-13:00",
        intent_type="meal",
        specificity="functional",
        search_queries=["当地美食"],
        preferred_types=["餐饮"],
        rejected_types=["酒店"],
        candidate_hints=["特色餐厅"],
        hint_policy="llm_common_knowledge_hint",
        target_count=1,
    )
    context = FunctionalSlotContext(
        slot_id="day1_lunch",
        day_number=1,
        intent_type="meal",
        raw_need=intent.raw_need,
        previous_anchor=POI(
            id="prev", name="上午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.0
        ),
        next_anchor=POI(id="next", name="下午地点", city="示例市", category="scenic", latitude=30.0, longitude=120.1),
        same_day_anchors=[],
        transport_mode="transit",
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
            slot_context=context,
        )

    stats = service._last_candidate_collection_stats
    assert provider_state == "ok"
    assert len(candidates) == 1
    assert len(map_service.nearby_calls) == 1
    assert len(map_service.search_calls) == 0
    assert stats["earlyStopReason"] == "unique_eligible_stop_count_reached"
    assert stats["poolBudget"]["uniqueEligibleStopCount"] == 1
    assert stats["uniqueEligibleEntityCount"] == 1


def test_campus_candidate_collection_stops_after_hints_satisfy_target():
    clear_database()
    citywide = [
        map_poi(
            "campus_qh",
            "清华大学",
            longitude=116.32,
            latitude=40.0,
            city="北京",
            category="education",
            poi_type="科教文化服务;学校;高等院校",
        ),
        map_poi(
            "campus_pk",
            "北京大学",
            longitude=116.31,
            latitude=39.99,
            city="北京",
            category="education",
            poi_type="科教文化服务;学校;高等院校",
        ),
    ]
    map_service = RecordingMapPoiService(citywide=citywide, nearby=[])
    intent = PoiIntent(
        raw_need="高校参观",
        city="北京",
        day_number=1,
        time_window="09:00-16:00",
        intent_type="campus_visit",
        specificity="functional",
        search_queries=["大学", "学院", "高等院校", "校园"],
        preferred_types=["高等院校"],
        rejected_types=["培训机构"],
        candidate_hints=["清华大学", "北京大学"],
        hint_policy="llm_common_knowledge_hint",
        target_count=2,
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )

    stats = service._last_candidate_collection_stats
    assert provider_state == "ok"
    assert [poi.name for poi in candidates] == ["清华大学", "北京大学"]
    assert map_service.search_calls == [
        ("清华大学", "campus"),
        ("北京大学", "campus"),
        ("大学", "campus"),
        ("学院", "campus"),
    ]
    assert stats["poolBudget"]["textSearchMax"] == 4
    assert stats["poolBudget"]["uniqueEligibleStopCount"] == 8
    assert stats["earlyStopReason"] == "text_search_max_reached"
    assert stats["usedTextSearch"] == 4
    assert stats["uniqueEligibleEntityCount"] == 2


def test_night_candidate_collection_caps_candidates_inside_provider_batch():
    clear_database()
    citywide = [
        map_poi(
            f"night_{index}",
            f"夜景观景点{index}",
            longitude=116.40 + index * 0.001,
            latitude=39.90,
            category="scenic",
            poi_type="风景名胜;观景点;城市广场",
        )
        for index in range(20)
    ]
    map_service = RecordingMapPoiService(citywide=citywide, nearby=[])
    intent = PoiIntent(
        raw_need="夜景观景点",
        city="北京",
        day_number=1,
        time_window="19:00-21:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=["夜景", "观景点", "城市夜景", "地标夜景", "灯光"],
        preferred_types=["地标", "观景点"],
        rejected_types=["酒店", "停车场"],
        candidate_hints=[],
        hint_policy="no_hint",
        target_count=2,
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )

    stats = service._last_candidate_collection_stats
    assert provider_state == "ok"
    assert len(candidates) == 12
    assert len(map_service.search_calls) == 1
    assert stats["poolBudget"]["rawCandidateMax"] == 12
    assert stats["poolBudget"]["uniqueEligibleStopCount"] == 5
    assert stats["poolBudget"]["textSearchMax"] == 4
    assert stats["usedTextSearch"] == 1
    assert stats["earlyStopReason"] == "raw_candidate_budget_reached"


def test_night_candidate_hint_search_uses_unfiltered_category_for_exact_place():
    clear_database()
    exact_place = map_poi(
        "night_qianmen",
        "前门大街",
        longitude=116.397,
        latitude=39.899,
        city="北京",
        category="shopping",
        poi_type="购物服务;特色商业街|风景名胜;旅游景点",
    )

    class CategorySensitiveNightMap(RecordingMapPoiService):
        def search(self, city: str, keyword: str = "", category: str = "all", limit: int = 12):
            self.search_calls.append((keyword, category))
            self.calls.append(("citywide", keyword))
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName=AMAP_PLACE_SOURCE,
                queriedAt=datetime.now(timezone.utc),
                pois=[exact_place] if category == "all" else [],
            )

    map_service = CategorySensitiveNightMap(citywide=[], nearby=[])
    intent = PoiIntent(
        raw_need="晚上看城市夜景",
        city="北京",
        day_number=1,
        time_window="18:00-22:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=[],
        preferred_types=["地标", "观景点"],
        rejected_types=["酒店", "停车场"],
        candidate_hints=["前门大街 夜景"],
        hint_policy="server_curated_hint",
        target_count=1,
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )

    assert provider_state == "ok"
    assert map_service.search_calls == [("前门大街", "all")]
    assert [candidate.name for candidate in candidates] == ["前门大街"]


def test_candidate_collection_records_provider_debug_from_http_exception():
    clear_database()
    map_service = DebugRateLimitedMapPoiService()
    intent = PoiIntent(
        raw_need="高校参观",
        city="北京",
        day_number=1,
        time_window="09:00-10:30",
        intent_type="campus_visit",
        specificity="functional",
        search_queries=["清华大学"],
        preferred_types=["高等院校"],
        rejected_types=["酒店"],
        candidate_hints=[],
        hint_policy="no_hint",
        target_count=1,
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )

    assert candidates == []
    assert provider_state == "rate_limited"
    assert map_service.search_calls == 1
    debug = service._last_candidate_collection_stats["providerDebug"][0]
    assert debug["classifiedReason"] == "http_429"
    assert debug["requestParams"] == {"keywords": "清华大学", "city": "110000"}
    serialized = json.dumps(debug, ensure_ascii=False).lower()
    assert "test-amap-key" not in serialized
    assert "key=" not in serialized
    assert '"key"' not in serialized


@pytest.mark.parametrize(
    ("classified_reason", "expected_provider_state", "expected_rate_limited"),
    [
        ("daily_quota_limited", "rate_limited", True),
        ("account_quota_limited", "rate_limited", True),
        ("unknown_rate_limited", "rate_limited", True),
        ("local_cooldown", "rate_limited", True),
        ("provider_down", "provider_down", False),
    ],
)
def test_candidate_collection_uses_debug_classified_reason_for_provider_state(
    classified_reason,
    expected_provider_state,
    expected_rate_limited,
):
    clear_database()
    map_service = DebugClassifiedMapPoiService(classified_reason)
    intent = PoiIntent(
        raw_need="高校参观",
        city="北京",
        day_number=1,
        time_window="09:00-10:30",
        intent_type="campus_visit",
        specificity="functional",
        search_queries=["清华大学"],
        preferred_types=["高等院校"],
        rejected_types=["酒店"],
        candidate_hints=[],
        hint_policy="no_hint",
        target_count=1,
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )

    assert candidates == []
    assert provider_state == expected_provider_state
    assert service._poi_grounding_rate_limited is expected_rate_limited
    assert map_service.search_calls == 1
    debug = service._last_candidate_collection_stats["providerDebug"][0]
    assert debug["classifiedReason"] == classified_reason
    assert debug["requestParams"] == {"keywords": "清华大学", "city": "110000"}
    serialized = json.dumps(debug, ensure_ascii=False).lower()
    assert "test-amap-key" not in serialized
    assert "key=" not in serialized
    assert '"key"' not in serialized


def test_area_walk_hint_accepts_thematic_scenic_result_without_exact_name_match():
    clear_database()
    century_park = map_candidate("PARK1", "世纪公园", "风景名胜;公园广场;公园", city="上海市")
    map_service = CandidateListMapPoiService({"上海 公园": [century_park]})
    intent = PoiIntent(
        raw_need="亲子",
        city="上海",
        day_number=1,
        time_window="09:00-11:00",
        intent_type="area_walk",
        specificity="area",
        search_queries=[],
        preferred_types=["公园", "风景名胜"],
        rejected_types=["酒店"],
        candidate_hints=["上海 公园"],
        hint_policy="static_candidate_hints",
        target_count=1,
    )

    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        candidates, provider_state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )

    assert provider_state == "ok"
    assert [candidate.name for candidate in candidates] == ["世纪公园"]
    assert map_service.keywords == ["上海 公园"]


def test_candidate_collection_records_amap_call_budget(monkeypatch):
    clear_database()
    clear_map_poi_runtime_state()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    calls = {"count": 0}

    def fake_fetch(_service, params):
        calls["count"] += 1
        return {
            "status": "1",
            "pois": [
                {
                    "id": "amap_budget_campus",
                    "name": params["keywords"],
                    "type": "科教文化服务;学校;高等院校",
                    "cityname": "北京市",
                    "adname": "海淀区",
                    "address": "测试地址",
                    "location": "116.3269,40.0036",
                    "photos": [],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)
    intent = PoiIntent(
        raw_need="高校参观",
        city="北京",
        day_number=1,
        time_window="09:00-10:30",
        intent_type="campus_visit",
        specificity="functional",
        search_queries=["清华大学"],
        preferred_types=["高等院校"],
        rejected_types=["酒店"],
        candidate_hints=[],
        hint_policy="no_hint",
        target_count=1,
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=MapPoiService(map_provider_key="test-amap-key"))
        budget = AmapCallBudget(place_text_max=2, total_external_max=2, source="test_candidate_collection")
        with amap_call_budget_scope(budget):
            first_candidates, first_state = service._collect_poi_candidates(
                intent,
                plan=None,
                original=None,
                pois_by_id={},
                resolved_by_id={},
                warnings=[],
            )
            second_candidates, second_state = service._collect_poi_candidates(
                intent,
                plan=None,
                original=None,
                pois_by_id={},
                resolved_by_id={},
                warnings=[],
            )

    assert first_state == "ok"
    assert second_state == "ok"
    assert first_candidates
    assert second_candidates
    assert calls["count"] == 1
    snapshot = service._last_candidate_collection_stats["amapCallBudget"]
    assert snapshot["usedPlaceText"] == 1
    assert snapshot["cacheHitCount"] >= 1
    assert snapshot["usedTotalExternal"] == 1


def test_candidate_scoring_does_not_hard_reject_primary_poi_for_location_text_terms():
    clear_database()
    intent = PoiIntent(
        raw_need="高校参观",
        city="北京",
        day_number=1,
        time_window="09:00-10:30",
        intent_type="campus_visit",
        specificity="functional",
        search_queries=[],
        preferred_types=["高等院校"],
        rejected_types=["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
        candidate_hints=["样例大学"],
        hint_policy="llm_common_knowledge_hint",
        semantic_context="北京高校两日游",
    )
    candidates = [
        MapPoiResponse(
            id="amap_primary_campus",
            name="样例大学",
            type="科教文化服务;学校;高等院校",
            city="北京市",
            district="大学城区",
            address="大学城停车场旁主入口",
            longitude=116.3,
            latitude=39.9,
            category="campus",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.95,
            photos=[],
        )
    ]
    with open_db() as connection:
        scored, rejected = ItineraryService(connection)._score_poi_candidates(intent, candidates)

    assert rejected == []
    assert scored[0].candidate.name == "样例大学"
    assert scored[0].components["locationTextPenalty"] < 0


def test_meal_candidate_scoring_allows_food_store_with_residential_branch_suffix():
    clear_database()
    intent = PoiIntent(
        raw_need="午餐 当地特色美食",
        city="杭州",
        day_number=1,
        time_window="12:00-13:00",
        intent_type="meal",
        specificity="functional",
        search_queries=["当地美食"],
        preferred_types=["餐饮"],
        rejected_types=["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
        candidate_hints=["杭州 小吃"],
        hint_policy="llm_common_knowledge_hint",
    )
    candidates = [
        MapPoiResponse(
            id="amap_local_snack",
            name="新窑小吃(翠园路小区店)",
            type="餐饮服务;中餐厅;特色小吃",
            city="杭州市",
            district="西湖区",
            address="翠园路",
            longitude=120.12,
            latitude=30.28,
            category="food",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            providerTypeCode="050100",
            tags=["地方小吃"],
            sourceClaims=[{"claimKey": "local_food", "stance": "support", "locality": "杭州"}],
            confidence=0.92,
            photos=[],
        )
    ]
    with open_db() as connection:
        scored, rejected = ItineraryService(connection)._score_poi_candidates(intent, candidates)

    assert rejected == []
    assert scored[0].candidate.name == "新窑小吃(翠园路小区店)"
    assert scored[0].components["typeMatch"] > 0


def test_night_view_candidate_scoring_rejects_photography_shop_subpoi():
    clear_database()
    intent = PoiIntent(
        raw_need="夜景观景点",
        city="北京",
        day_number=1,
        time_window="19:00-21:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=[],
        preferred_types=["观景点", "地标"],
        rejected_types=["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
        candidate_hints=["水立方", "中央电视塔"],
        hint_policy="llm_common_knowledge_hint",
        semantic_context="晚上看北京夜景",
    )
    candidates = [
        MapPoiResponse(
            id="B0PHOTO001",
            name="水立方拍照留念点",
            type="购物服务;专卖店;儿童用品店|生活服务;摄影冲印店;摄影冲印",
            city="北京市",
            district="朝阳区",
            address="天辰东路",
            longitude=116.390,
            latitude=39.992,
            category="all",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.92,
            photos=[],
        ),
        MapPoiResponse(
            id="B0MANAGE001",
            name="国家广播电视总局中央广播电视塔管理中心",
            type="科教文化服务;科教文化场所;科教文化场所",
            city="北京市",
            district="海淀区",
            address="西三环中路",
            longitude=116.301,
            latitude=39.919,
            category="all",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.92,
            photos=[],
        ),
        MapPoiResponse(
            id="B0LIBRARY1",
            name="城市图书馆夜景打卡点",
            type="科教文化服务;图书馆",
            city="北京市",
            district="海淀区",
            address="文化中心东门",
            longitude=116.301,
            latitude=39.919,
            category="all",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.92,
            photos=[],
        ),
        MapPoiResponse(
            id="B0TOWER001",
            name="中央电视塔",
            type="风景名胜;观景点;塔",
            city="北京市",
            district="海淀区",
            address="西三环中路",
            longitude=116.300,
            latitude=39.918,
            category="all",
            source=AMAP_PLACE_SOURCE,
            openTimeToday="09:00-22:00",
            sourceNote="高德 WebService POI 搜索",
            confidence=0.92,
            photos=[],
        ),
    ]
    with open_db() as connection:
        scored, rejected = ItineraryService(connection)._score_poi_candidates(intent, candidates)

    assert [item.candidate.name for item in scored] == ["中央电视塔"]
    rejected_by_name = {item.candidate.name: item.rejected_reasons for item in rejected}
    assert "functional_subpoi" in rejected_by_name["水立方拍照留念点"]
    assert "functional_subpoi" in rejected_by_name["国家广播电视总局中央广播电视塔管理中心"]
    assert "weak_night_view_entity" in rejected_by_name["城市图书馆夜景打卡点"]
    assert "nightViewTypeQuality" in scored[0].components
    assert "weakNightViewEntityPenalty" in scored[0].components
    assert "nightViewPublicAccessScore" in scored[0].components


def test_landmark_candidate_scoring_rejects_night_view_checkin_subpoi():
    clear_database()
    intent = PoiIntent(
        raw_need="广州城市建筑",
        city="广州",
        day_number=2,
        time_window="14:00-16:00",
        intent_type="landmark",
        specificity="functional",
        search_queries=[],
        preferred_types=["风景名胜", "地标", "建筑"],
        rejected_types=["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
        candidate_hints=["石室圣心大教堂", "广州塔"],
        hint_policy="llm_common_knowledge_hint",
        semantic_context="城市建筑、博物馆和夜景，尽量少绕路",
    )
    candidates = [
        MapPoiResponse(
            id="amap_checkin",
            name="首府大厦-广州塔和城市建筑夜景(打卡点)",
            type="风景名胜;风景名胜;观景点",
            city="广州市",
            district="天河区",
            address="珠江新城冼村路2号首府大厦",
            longitude=113.318,
            latitude=23.121,
            category="all",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.92,
            photos=[],
        ),
        MapPoiResponse(
            id="amap_church",
            name="石室圣心大教堂-石室耶稣圣心堂",
            type="风景名胜;风景名胜;教堂",
            city="广州市",
            district="越秀区",
            address="卖麻街与白米巷交叉口西北100米",
            longitude=113.260,
            latitude=23.114,
            category="all",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.92,
            photos=[],
        ),
    ]

    with open_db() as connection:
        scored, rejected = ItineraryService(connection)._score_poi_candidates(intent, candidates)

    assert [item.candidate.name for item in scored] == ["石室圣心大教堂-石室耶稣圣心堂"]
    rejected_by_name = {item.candidate.name: item.rejected_reasons for item in rejected}
    assert "functional_subpoi" in rejected_by_name["首府大厦-广州塔和城市建筑夜景(打卡点)"]


def test_staged_risk_segments_skip_ordinary_meals_on_national_day():
    clear_database()
    with open_db() as connection:
        service = ItineraryService(connection)
        pois = [
            POI(
                "poi_campus",
                "示例大学",
                "北京",
                "科教文化服务",
                39.9,
                116.3,
                amap_id="amap_campus",
                type="科教文化服务;高等院校",
            ),
            POI("poi_meal", "附近餐馆", "北京", "餐饮服务", 39.91, 116.31, amap_id="amap_meal", type="餐饮服务;中餐厅"),
            POI(
                "poi_night",
                "城市夜景广场",
                "北京",
                "风景名胜",
                39.92,
                116.32,
                amap_id="amap_night",
                type="风景名胜;广场",
            ),
            POI("poi_area", "中关村区域漫步", "北京", "街区", 39.93, 116.33, amap_id="amap_area", type="街区"),
            POI(
                "poi_draft",
                "待确认博物馆",
                "北京",
                "博物馆",
                39.94,
                116.34,
                source="agent-text-timeline",
                type="博物馆",
            ),
        ]
        segments = [
            ItinerarySegment("seg_campus", "day1", 1, "visit", "09:00", "11:00", "poi_campus", "public_transit", 0, ""),
            ItinerarySegment("seg_meal", "day1", 2, "meal", "12:00", "13:00", "poi_meal", "public_transit", 60, ""),
            ItinerarySegment(
                "seg_night", "day1", 3, "visit", "19:00", "20:30", "poi_night", "public_transit", 0, "夜景"
            ),
            ItinerarySegment(
                "seg_area", "day1", 4, "area_walk", "15:00", "16:00", "poi_area", "public_transit", 0, "区域漫步"
            ),
            ItinerarySegment(
                "seg_draft", "day1", 5, "visit", "16:30", "17:30", "poi_draft", "public_transit", 0, "博物馆"
            ),
        ]

        selected = service._staged_risk_segments(
            pois,
            segments,
            {"resolvedTripDates": {"dates": ["2026-10-01"], "holidayName": "国庆"}},
        )

    assert [segment.id for segment in selected] == ["seg_campus", "seg_night"]


def seed_extraction(
    connection: sqlite3.Connection,
    inspiration_id: str = "insp_us2",
    poi_candidates: Optional[list[dict]] = None,
) -> InspirationSet:
    inspiration = InspirationSet(id=inspiration_id, user_id=get_settings().default_user_id, city="北京", status="ready")
    connection.execute(
        """
        INSERT INTO inspiration_sets (
            id, user_id, city, status, theme_summary, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inspiration.id,
            inspiration.user_id,
            inspiration.city,
            inspiration.status,
            inspiration.theme_summary,
            inspiration.created_at.isoformat(),
            inspiration.updated_at.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO extraction_results (
            id, inspiration_set_id, city_candidates, poi_candidates, style_tags,
            budget_clues, route_clues, confidence, needs_user_confirmation,
            source_links, provider_name, fallback_used, provider_failure_reason,
            user_visible_caveat, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"ext_{inspiration.id}",
            inspiration.id,
            json.dumps(["北京"], ensure_ascii=False),
            json.dumps(
                poi_candidates
                if poi_candidates is not None
                else [
                    {"name": "故宫博物院", "confidence": 0.9, "sourceLinks": []},
                    {"name": "景山公园", "confidence": 0.55, "sourceLinks": []},
                ],
                ensure_ascii=False,
            ),
            json.dumps(["拍照优先"], ensure_ascii=False),
            json.dumps(["预算 3000 元"], ensure_ascii=False),
            json.dumps(["市中心一天"], ensure_ascii=False),
            0.82,
            0,
            json.dumps([], ensure_ascii=False),
            "mock-vision-provider",
            0,
            None,
            None,
            inspiration.created_at.isoformat(),
        ),
    )
    connection.commit()
    return inspiration


class StubRouteService:
    warnings = []

    def build_routes(self, plan_id, pois, transport_mode="public_transit", segments=None, route_pairs=None):
        if segments:
            pairs = [
                (index, from_segment, to_segment, pois[index - 1], pois[index])
                for index, (from_segment, to_segment) in enumerate(zip(segments, segments[1:]), start=1)
                if route_pairs is None or (from_segment.id, to_segment.id) in route_pairs
            ]
        else:
            pairs = [
                (index, None, None, from_poi, to_poi)
                for index, (from_poi, to_poi) in enumerate(zip(pois, pois[1:]), start=1)
            ]
        return [
            RouteOption(
                id=f"route_{plan_id}_{index}",
                plan_id=plan_id,
                from_segment_id=from_segment.id if from_segment else None,
                to_segment_id=to_segment.id if to_segment else None,
                from_poi_id=from_poi.id,
                to_poi_id=to_poi.id,
                transport_mode=transport_mode,
                is_selected=True,
                distance_meters=1800 + index,
                duration_minutes=15,
                cost_estimate=4.0,
                crowding_risk="medium",
                source="amap-webservice",
            )
            for index, from_segment, to_segment, from_poi, to_poi in pairs
        ]


class StubMapPoiService:
    def search(self, city, keyword="", category="all", limit=12):
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id=f"amap_{keyword}",
                    name=keyword,
                    type="风景名胜",
                    city=city,
                    district="东城区",
                    address=f"{keyword} 高德地址",
                    longitude=116.3972 if keyword == "故宫博物院" else 116.4074,
                    latitude=39.9163 if keyword == "故宫博物院" else 39.9042,
                    category=category,
                    source=AMAP_PLACE_SOURCE,
                    sourceNote="高德 WebService POI 搜索，限定当前城市，extensions=all",
                    confidence=0.86,
                    photos=[],
                )
            ],
        )


class UnavailableMapPoiService:
    def search(self, city, keyword="", category="all", limit=12):
        raise HTTPException(status_code=400, detail="MAP_PROVIDER_KEY is not configured; cannot search AMap POIs")


class PartialRiskService:
    def build_alerts(
        self,
        plan_id,
        _city,
        _pois,
        segments,
        _weather,
        _traffic_signals,
        _ticket_results,
        route_options=None,
        risk_context=None,
    ):
        alerts = []
        if segments:
            alerts.append(
                POIRiskAlert(
                    id=f"risk_{segments[0].id}",
                    plan_id=plan_id,
                    segment_id=segments[0].id,
                    poi_name="清华大学",
                    status="available",
                    summary="已找到官方预约来源。",
                    source_name="官方来源",
                    source_url="https://www.tsinghua.edu.cn/",
                    confidence=0.86,
                )
            )
        if len(segments) > 1:
            alerts.append(
                POIRiskAlert(
                    id=f"risk_{segments[1].id}",
                    plan_id=plan_id,
                    segment_id=segments[1].id,
                    poi_name="北京大学",
                    status="unavailable",
                    summary="未找到可用官方或近期来源。",
                    source_name="",
                    confidence=0.0,
                    failure_reason="no usable official source",
                )
            )
        return alerts


class CapturingRiskService:
    def __init__(self):
        self.risk_context = None
        self.ticket_results = []

    def build_alerts(
        self,
        _plan_id,
        _city,
        _pois,
        _segments,
        _weather,
        _traffic_signals,
        _ticket_results,
        route_options=None,
        risk_context=None,
    ):
        self.risk_context = risk_context
        self.ticket_results = _ticket_results
        return []


def test_itinerary_service_generates_map_linked_timeline():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        plan = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
        ).generate_for_inspiration(inspiration.id, "北京")

    assert plan.city == "北京"
    assert plan.days[0].segments[0].poi.name == "故宫博物院"
    assert plan.route_options[0].distance_meters > 0
    assert plan.weather_signals[0].data_status == "degraded"
    assert plan.weather_signals[0].fallback_used is False
    assert plan.weather_signals[0].provider_name == "amap-weather-provider"
    assert plan.weather_signals[0].failure_reason
    assert plan.poi_risk_alerts[0].status == "unavailable"
    assert plan.poi_risk_alerts[0].segment_id == plan.days[0].segments[0].id
    assert "无法联网搜索" in plan.poi_risk_alerts[0].user_visible_caveat
    assert plan.ticket_lookup_results[0].fallback_used is False
    assert plan.ticket_lookup_results[0].provider_name == "local-ticket-guard"
    assert plan.ticket_lookup_results[0].status == "not_checked"
    assert plan.ticket_lookup_results[0].source_url == ""
    assert plan.ticket_lookup_results[0].booking_url == ""
    assert "初始行程未全量查询预约状态" in plan.ticket_lookup_results[0].caveat
    assert plan.traffic_crowding_signals[0].crowding_level == "unknown"
    assert plan.traffic_crowding_signals[0].real_data_available is False


def test_itinerary_service_exposes_poi_grounding_lifecycle_fields():
    clear_database()
    with open_db() as connection:
        service = ItineraryService(connection)
        draft = service._poi_response(
            POI(
                id="poi_draft_only",
                name="待确认地点",
                city="北京",
                category="candidate",
                latitude=None,
                longitude=None,
                source="agent-text-timeline",
                confidence=0.35,
            )
        )
        anchor = service._poi_response(
            POI(
                id="poi_routeable_anchor",
                amap_id="B000ROUTEABLE",
                name="五道口周边午餐",
                city="北京",
                category="food",
                latitude=39.992,
                longitude=116.337,
                source="agent-text-timeline",
                source_note="已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。地图锚点：五道口购物中心；高德 POI 待校验",
                confidence=0.4,
            )
        )
        verified = service._poi_response(
            POI(
                id="poi_verified_amap",
                amap_id="B000VERIFIED",
                name="故宫博物院",
                city="北京",
                category="scenic",
                latitude=39.918058,
                longitude=116.397026,
                source=AMAP_PLACE_SOURCE,
                confidence=0.9,
            )
        )

    assert draft.grounding_status == "draft_only"
    assert draft.map_ready is False
    assert draft.routeable is False
    assert anchor.grounding_status == "composite_poi"
    assert anchor.map_ready is True
    assert anchor.routeable is True
    assert anchor.matched_amap_name == "五道口购物中心"
    assert anchor.grounding["matchedAmapName"] == "五道口购物中心"
    assert anchor.poi_specificity == "composite_poi"
    assert anchor.intent_type == "meal"
    assert anchor.needs_concrete_poi is True
    assert verified.grounding_status == "verified_amap"
    assert verified.map_ready is True
    assert verified.routeable is True
    assert verified.matched_amap_name == "故宫博物院"


def test_map_readiness_summary_distinguishes_item_and_itinerary_readiness():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        service = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
        )
        plan = service.generate_for_inspiration(inspiration.id, "北京")
        first = plan.days[0].segments[0].poi.id
        second = plan.days[0].segments[1].poi.id
        connection.execute(
            """
            UPDATE pois
            SET source = ?, amap_id = ?, latitude = ?, longitude = ?, confidence = ?, source_note = ?
            WHERE id = ?
            """,
            (
                "agent-text-timeline",
                "B000ANCHOR",
                39.918058,
                116.397026,
                0.4,
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。地图锚点：故宫博物院；高德 POI 待校验",
                first,
            ),
        )
        connection.execute(
            """
            UPDATE pois
            SET source = ?, amap_id = NULL, latitude = NULL, longitude = NULL, confidence = ?, source_note = ?
            WHERE id = ?
            """,
            ("agent-text-timeline", 0.35, "Agent 先写入可编辑时间轴，占位 POI 未完成地图 grounding。", second),
        )
        connection.commit()
        summary = service._map_readiness_summary(plan.id)

    assert summary["mapReady"] is False
    assert [item["groundingStatus"] for item in summary["items"]] == ["verified_amap", "draft_only"]
    assert summary["items"][0]["mapReady"] is True
    assert summary["items"][0]["matchedAmapName"] == "故宫博物院"
    assert summary["items"][1]["mapReady"] is False
    assert summary["missing"] == ["景山公园 missing confirmed or routeable AMap anchor"]


def test_agent_text_timeline_anchor_source_note_is_user_readable():
    clear_database()
    with open_db() as connection:
        service = ItineraryService(connection)
        anchor = service._agent_text_timeline_anchor(
            "清华周边午餐",
            POI(
                id="amap_anchor",
                amap_id="B000ANCHOR",
                name="五道口购物中心",
                city="北京",
                category="food",
                latitude=39.992,
                longitude=116.337,
                source=AMAP_PLACE_SOURCE,
                confidence=0.91,
            ),
            0.4,
            "composite_poi",
        )

    assert anchor.source == "agent-text-timeline"
    assert "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点" in anchor.source_note
    assert "地图锚点：五道口购物中心" in anchor.source_note
    assert "POI意图：composite_poi" in anchor.source_note


def test_mock_food_poi_metadata_is_not_map_ready_or_routeable():
    with open_db() as connection:
        service = ItineraryService(connection)
        metadata = service._poi_grounding_metadata(
            POI(
                id="poi_mock_food",
                amap_id="mock_amap_food_1",
                name="北京本地菜餐厅",
                city="北京",
                category="food",
                latitude=39.91,
                longitude=116.39,
                source=AMAP_PLACE_SOURCE,
                confidence=0.92,
                source_note="CLI eval mock AMap candidate; only used when mockMapProvider is enabled. groundingStatus：agent_selected_candidate",
            )
        )

    assert metadata["groundingStatus"] == "waiting_for_poi_grounding"
    assert metadata["mapReady"] is False
    assert metadata["routeable"] is False
    assert metadata["needsConcretePoi"] is True
    assert metadata["untrustedMockOrSynthetic"] is True


def test_real_amap_meal_poi_metadata_remains_map_ready():
    with open_db() as connection:
        service = ItineraryService(connection)
        metadata = service._poi_grounding_metadata(
            POI(
                id="poi_real_food",
                amap_id="B0REALFOOD",
                name="护国寺小吃(测试店)",
                city="北京",
                category="food",
                latitude=39.91,
                longitude=116.39,
                source=AMAP_PLACE_SOURCE,
                confidence=0.92,
                source_note="高德 WebService POI 搜索；groundingStatus：agent_selected_candidate；intentType：meal",
            )
        )

    assert metadata["groundingStatus"] == "agent_selected_candidate"
    assert metadata["mapReady"] is True
    assert metadata["routeable"] is True
    assert metadata["needsConcretePoi"] is False
    assert metadata["untrustedMockOrSynthetic"] is False


def test_agent_text_night_view_metadata_is_not_map_ready_or_routeable():
    with open_db() as connection:
        service = ItineraryService(connection)
        metadata = service._poi_grounding_metadata(
            POI(
                id="poi_night_placeholder",
                amap_id="B0DRAFTNIGHT",
                name="夜景观景点",
                city="北京",
                category="scenic",
                latitude=39.91,
                longitude=116.39,
                source="agent-text-timeline",
                confidence=0.92,
                source_note="groundingStatus：composite_poi；intentType：night_view；needsConcretePoi=true",
            )
        )

    assert metadata["groundingStatus"] == "waiting_for_poi_grounding"
    assert metadata["mapReady"] is False
    assert metadata["routeable"] is False
    assert metadata["needsConcretePoi"] is True
    assert metadata["nightViewConcreteRequired"] is True


def test_real_amap_night_view_metadata_remains_map_ready():
    with open_db() as connection:
        service = ItineraryService(connection)
        metadata = service._poi_grounding_metadata(
            POI(
                id="poi_real_night",
                amap_id="B0REALNIGHT",
                name="景山公园",
                city="北京",
                category="scenic",
                latitude=39.9236,
                longitude=116.3969,
                source=AMAP_PLACE_SOURCE,
                confidence=0.94,
                source_note="高德 WebService POI 搜索；groundingStatus：agent_selected_candidate；intentType：night_view",
            )
        )

    assert metadata["groundingStatus"] == "agent_selected_candidate"
    assert metadata["mapReady"] is True
    assert metadata["routeable"] is True
    assert metadata["needsConcretePoi"] is False
    assert metadata["nightViewConcreteRequired"] is False


def test_itinerary_service_passes_preferences_and_frontend_context_to_poi_risk_service():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        connection.execute(
            """
            INSERT INTO preference_profiles (
                id, user_id, budget_range, pace_preference, transport_preferences,
                food_preferences, photo_preference, accessibility_notes, party_size,
                traveler_types, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pref_risk",
                get_settings().default_user_id,
                "3000 左右",
                "轻松不赶路",
                json.dumps(["public_transit"], ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                "高",
                "",
                3,
                json.dumps(["老人"], ensure_ascii=False),
                "2026-06-10T00:00:00+00:00",
            ),
        )
        connection.commit()
        risk_service = CapturingRiskService()
        ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=risk_service,
        ).generate_for_inspiration(
            inspiration.id,
            "北京",
            preference_profile_id="pref_risk",
            preference_summary="两个大人一个老人，预算 3000 左右，拍照优先",
            planning_context={"tripPurpose": "拍照和历史文化", "travelDateRange": {"start": "2026-10-16"}},
        )

    assert risk_service.risk_context == {
        "preferenceSummary": "两个大人一个老人，预算 3000 左右，拍照优先",
        "partySize": 3,
        "travelerTypes": ["老人"],
        "budgetRange": "3000 左右",
        "pacePreference": "轻松不赶路",
        "travelDateRange": {"start": "2026-10-16"},
        "tripPurpose": "拍照和历史文化",
        "weatherSensitivity": "",
        "memoryRiskRules": {},
        "officialSourceFirst": False,
        "riskPriorityTerms": [],
        "travelerSensitivity": "normal",
        "travelTimeWindows": risk_service.risk_context["travelTimeWindows"],
    }
    assert risk_service.risk_context["travelTimeWindows"][0] == {
        "poiName": "故宫博物院",
        "startTime": "09:30",
        "endTime": "11:30",
    }
    assert risk_service.risk_context["travelTimeWindows"][1]["poiName"]
    assert risk_service.risk_context["travelTimeWindows"][1]["startTime"] == "10:30"
    assert risk_service.risk_context["travelTimeWindows"][1]["endTime"] == "12:30"
    assert risk_service.ticket_results
    assert any(result.status == "not_checked" for result in risk_service.ticket_results)
    assert all(result.fallback_used is False for result in risk_service.ticket_results)
    assert all(result.provider_name == "local-ticket-guard" for result in risk_service.ticket_results)
    assert any(result.credibility_rank == "unavailable" for result in risk_service.ticket_results)


class FailingTicketSearchProvider:
    def search(self, *_args, **_kwargs):
        raise AssertionError("initial itinerary generation must not query ticket web search")


def test_initial_ticket_placeholders_do_not_call_web_search():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        plan = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
            ticket_service=TicketService(connection, web_search_provider=FailingTicketSearchProvider()),
        ).generate_for_inspiration(inspiration.id, "北京")

    assert plan.ticket_lookup_results
    assert {result.provider_name for result in plan.ticket_lookup_results} == {"local-ticket-guard"}
    assert all(result.status == "not_checked" for result in plan.ticket_lookup_results)
    assert all(result.source_url == "" and result.booking_url == "" for result in plan.ticket_lookup_results)


def test_pending_ticket_for_area_poi_returns_local_caveat_without_search():
    clear_database()
    with open_db() as connection:
        service = TicketService(connection, web_search_provider=FailingTicketSearchProvider())
        segment = type("Segment", (), {"id": "seg_area", "poi_id": "poi_area", "estimated_cost": 0})()
        poi = POI(
            id="poi_area",
            name="奥林匹克公园夜景",
            city="北京",
            category="area",
            latitude=39.99,
            longitude=116.39,
            source="agent-text-timeline",
            confidence=0.4,
        )
        results = service.build_pending_for_segments([segment], [poi])

    assert len(results) == 1
    assert results[0].status == "needs_concrete_poi"
    assert results[0].provider_name == "local-ticket-guard"
    assert results[0].source_url == ""
    assert results[0].booking_url == ""
    assert "请选择具体场馆/入口/区域" in results[0].caveat


def test_itinerary_service_updates_transport_mode_and_routes():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        service = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
        )
        plan = service.generate_for_inspiration(inspiration.id, "北京")
        segment_id = plan.days[0].segments[0].id
        updated = service.apply_edit(
            plan.id,
            operation="replace_transport_mode",
            segment_id=segment_id,
            value="self_drive",
        )

    assert updated.days[0].segments[0].transport_mode == "self_drive"
    assert updated.days[0].segments[1].transport_mode == plan.days[0].segments[1].transport_mode
    assert updated.route_options[0].mode == "driving"
    assert {route.mode for route in updated.route_options}.issubset({"driving", "taxi"})
    assert updated.route_options[0].from_poi_id == plan.days[0].segments[0].poi.id
    assert updated.route_options[0].to_poi_id == plan.days[0].segments[1].poi.id


def test_refresh_planning_tools_rolls_back_failed_route_write_before_next_tool(monkeypatch):
    clear_database()
    weather_transaction_states = []

    def fail_after_route_write(self, plan_id, preferred_mode=None):
        self.db.execute("UPDATE itinerary_plans SET updated_at = updated_at WHERE id = ?", (plan_id,))
        assert self.db.in_transaction
        raise RuntimeError("route write failed")

    def capture_weather_transaction(self, *_args, **_kwargs):
        weather_transaction_states.append(self.db.in_transaction)
        raise RuntimeError("weather skipped")

    monkeypatch.setattr(ItineraryService, "refresh_routes", fail_after_route_write)
    monkeypatch.setattr(ItineraryService, "_refresh_weather_signal", capture_weather_transaction)

    with open_db() as connection:
        inspiration = seed_extraction(connection)
        service = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
        )
        plan = service.generate_for_inspiration(inspiration.id, "北京")
        connection.commit()
        warnings = service.refresh_planning_tools(plan.id, commit_between_tools=True)

    assert any("地图/路线工具失败" in warning for warning in warnings)
    assert any("高德天气工具失败" in warning for warning in warnings)
    assert weather_transaction_states == [False]


def test_itinerary_edit_rejects_segment_from_another_plan():
    clear_database()
    with open_db() as connection:
        first = seed_extraction(connection, "insp_us2_first")
        service = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
        )
        first_plan = service.generate_for_inspiration(first.id, "北京")
        second = seed_extraction(connection, "insp_us2_second")
        second_plan = service.generate_for_inspiration(second.id, "北京")

        with pytest.raises(HTTPException) as error:
            service.apply_edit(
                first_plan.id,
                operation="replace_transport_mode",
                segment_id=second_plan.days[0].segments[0].id,
                value="self_drive",
            )

    assert error.value.status_code == 404


def test_itinerary_service_does_not_create_mock_pois_when_map_provider_is_unavailable():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection, "insp_no_map", poi_candidates=[])
        plan = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=UnavailableMapPoiService(),
            poi_risk_service=POIRiskService(search_provider_key=""),
        ).generate_for_inspiration(inspiration.id, "北京")
        persisted_pois = connection.execute("SELECT * FROM pois WHERE plan_id = ?", (plan.id,)).fetchall()

    assert plan.days[0].segments == []
    assert plan.route_options == []
    assert plan.ticket_lookup_results == []
    assert persisted_pois == []
    assert any("地图服务未确认任何 POI" in warning for warning in plan.route_warnings)
    assert all("热门景点" not in warning and "热门打卡点" not in warning for warning in plan.route_warnings)


class AliasCapturingMapPoiService:
    def __init__(self):
        self.keywords = []

    def search(self, city, keyword="", category="all", limit=12):
        self.keywords.append(keyword)
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="amap_generic_landmark",
                    name="样例地标",
                    type="风景名胜",
                    city=city,
                    district="样例区",
                    address="样例路1号",
                    longitude=116.3966,
                    latitude=39.9929,
                    category="scenic",
                    source=AMAP_PLACE_SOURCE,
                    sourceNote="高德 WebService POI 搜索",
                    confidence=0.9,
                    photos=[],
                )
            ],
        )


def test_itinerary_service_does_not_apply_specific_poi_alias_before_amap_lookup():
    clear_database()
    map_service = AliasCapturingMapPoiService()
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        poi = service._resolve_amap_poi("样例市", "样例地标简称", [])

    assert map_service.keywords == ["样例地标简称"]
    assert poi is not None
    assert poi.amap_id == "amap_generic_landmark"


def test_staged_enrichment_keeps_risk_pending_until_all_high_risk_segments_have_sources():
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(
            connection,
            "insp_partial_risk",
            poi_candidates=[
                {"name": "清华大学", "confidence": 0.9, "sourceLinks": []},
                {"name": "北京大学", "confidence": 0.9, "sourceLinks": []},
            ],
        )
        service = ItineraryService(
            connection,
            route_service=StubRouteService(),
            weather_service=WeatherService(weather_provider_key=""),
            map_poi_service=StubMapPoiService(),
            poi_risk_service=PartialRiskService(),
        )
        plan = service.generate_for_inspiration(inspiration.id, "北京")
        report = service.refresh_staged_enrichment(
            plan.id,
            "sess_partial_risk",
            planning_context={
                "resolvedTripDates": {
                    "status": "resolved",
                    "startDate": "2026-10-01",
                    "endDate": "2026-10-02",
                    "dates": ["2026-10-01", "2026-10-02"],
                    "weatherForecastSupported": False,
                }
            },
            preferred_mode="transit",
        )

    assert report["riskChecked"] is False
    assert report["riskStatus"] == "pending"
    assert report["state"] in {"risk_pending", "route_partial"}
    assert any("高风险 POI 尚未取得可用官方/近期来源" in warning for warning in report["warnings"])


def test_poi_specificity_metadata_distinguishes_area_functional_and_exact():
    clear_database()
    with open_db() as connection:
        service = ItineraryService(connection)
        area = service._poi_response(
            POI(
                id="poi_area",
                amap_id="B000AREA",
                name="奥林匹克公园夜景",
                city="北京",
                category="area",
                latitude=39.991,
                longitude=116.39,
                source="agent-text-timeline",
                source_note="已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。POI意图：composite_poi；地图锚点：奥林匹克公园；高德 POI 待校验",
                confidence=0.4,
            )
        )
        functional = service._poi_response(
            POI(
                id="poi_functional",
                name="晚餐",
                city="北京",
                category="pending",
                latitude=None,
                longitude=None,
                source="agent-text-timeline",
                confidence=0.3,
            )
        )
        exact = service._poi_response(
            POI(
                id="poi_exact",
                amap_id="B000EXACT",
                name="故宫博物院",
                city="北京",
                category="scenic",
                latitude=39.918058,
                longitude=116.397026,
                source=AMAP_PLACE_SOURCE,
                confidence=0.9,
            )
        )

    assert area.grounding_status == "composite_poi"
    assert area.map_ready is False
    assert area.routeable is False
    assert area.needs_concrete_poi is True
    assert area.matched_amap_name == "奥林匹克公园"
    assert functional.grounding_status == "functional_poi"
    assert functional.map_ready is False
    assert functional.routeable is False
    assert functional.needs_concrete_poi is True
    assert exact.grounding_status == "verified_amap"
    assert exact.poi_specificity == "exact_entity"


def test_functional_poi_name_is_not_resolved_as_final_amap_poi():
    clear_database()
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=StubMapPoiService())
        warnings: list[str] = []
        resolved = service._resolve_amap_poi("北京", "晚餐", warnings)

    assert resolved is None
    assert any("功能型地点" in warning for warning in warnings)


class KeywordEchoMapPoiService:
    def __init__(self, fail_on_search: bool = False):
        self.keywords = []
        self.nearby_calls = []
        self.fail_on_search = fail_on_search

    def search(self, city, keyword="", category="all", limit=12):
        self.keywords.append(keyword)
        if self.fail_on_search:
            raise AssertionError("MapPoiService.search should not be called on persistent cache hit")
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id=f"amap_{keyword}",
                    name=keyword,
                    type="风景名胜",
                    city=city,
                    district="朝阳区",
                    address=f"{keyword} 地址",
                    longitude=116.4,
                    latitude=39.9,
                    category=category,
                    source=AMAP_PLACE_SOURCE,
                    sourceNote="来源：高德地图",
                    confidence=0.9,
                    photos=[],
                )
            ],
        )

    def search_nearby(self, city, longitude, latitude, keyword, category="all", radius=1500, limit=12):
        self.nearby_calls.append(
            {"keyword": keyword, "longitude": longitude, "latitude": latitude, "category": category, "radius": radius}
        )
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="amap_nearby_restaurant",
                    name="附近餐厅",
                    type="餐饮服务",
                    city=city,
                    district="东城区",
                    address="附近餐厅地址",
                    longitude=float(longitude) + 0.001,
                    latitude=float(latitude) + 0.001,
                    category=category,
                    source=AMAP_PLACE_SOURCE,
                    sourceNote="来源：高德地图",
                    confidence=0.88,
                    photos=[],
                )
            ],
        )


class RateLimitedMapPoiService:
    def __init__(self):
        self.calls = []

    def search(self, city, keyword="", category="all", limit=12):
        self.calls.append(keyword)
        raise HTTPException(status_code=502, detail="CUQPS_HAS_EXCEEDED")


class CandidateListMapPoiService:
    def __init__(self, candidates_by_keyword: dict[str, list[MapPoiResponse]]):
        self.candidates_by_keyword = candidates_by_keyword
        self.keywords = []

    def search(self, city, keyword="", category="all", limit=12):
        self.keywords.append(keyword)
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=self.candidates_by_keyword.get(keyword, []),
        )


class PartialRateLimitedMapPoiService:
    def __init__(self):
        self.calls = []

    def search(self, city, keyword="", category="all", limit=12):
        self.calls.append(keyword)
        if len(self.calls) > 1:
            raise HTTPException(status_code=502, detail="CUQPS_HAS_EXCEEDED")
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="B000FIRST",
                    name="首个成功候选",
                    type="风景名胜",
                    city=city,
                    district="朝阳区",
                    address="首个成功候选地址",
                    longitude=116.4,
                    latitude=39.9,
                    category=category,
                    source=AMAP_PLACE_SOURCE,
                    sourceNote="来源：高德地图",
                    confidence=0.4,
                    photos=[],
                )
            ],
        )


def map_candidate(
    amap_id: str, name: str, poi_type: str, city: str = "北京市", confidence: float = 0.9
) -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type=poi_type,
        city=city,
        district="朝阳区",
        address=f"{name}地址",
        longitude=116.4,
        latitude=39.9,
        category="all",
        source=AMAP_PLACE_SOURCE,
        sourceNote="来源：高德地图",
        confidence=confidence,
        photos=[],
    )


def test_candidate_canonical_entity_normalizes_generic_entity_variants():
    clear_database()
    with open_db() as connection:
        service = ItineraryService(connection)
        names = [
            "样例大学主校区",
            "样例大学（主校区）",
            "样例大学国际学院",
            "样例博物馆东门",
            "样例公园停车场",
            "样例购物中心服务中心",
        ]
        normalized = [
            service._candidate_canonical_entity_key(map_candidate(f"amap_{index}", name, "风景名胜"))
            for index, name in enumerate(names)
        ]

    assert normalized == ["样例大学", "样例大学", "样例大学", "样例博物馆", "样例公园", "样例购物中心"]


def seed_three_segment_draft_plan(connection: sqlite3.Connection) -> str:
    plan_id = "plan_draft_refresh"
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO itinerary_plans (
            id, user_id, inspiration_set_id, template_type, title, city,
            budget_target, budget_estimate, budget_delta_explanation,
            decision_rationale, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            get_settings().default_user_id,
            "manual",
            "custom",
            "北京待补全行程",
            "北京",
            None,
            0,
            "",
            "",
            "draft",
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO itinerary_days (id, plan_id, day_number, date, title, weather_summary, risk_summary, total_estimated_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("day_draft_refresh", plan_id, 1, None, "Day 1", "待查询", "待查询", 0),
    )
    pois = [
        ("poi_verified_a", "故宫博物院", AMAP_PLACE_SOURCE, "amap_a", 39.918, 116.397, 0.9),
        ("poi_draft", "北京大学", "agent-text-timeline", None, None, None, 0.5),
        ("poi_verified_b", "清华大学", AMAP_PLACE_SOURCE, "amap_b", 40.0, 116.326, 0.9),
    ]
    for poi_id, name, source, amap_id, lat, lon, confidence in pois:
        connection.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url,
                source, confidence, amap_id, type, district, address, source_note,
                source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                poi_id,
                plan_id,
                name,
                "北京",
                "scenic",
                lat,
                lon,
                None,
                source,
                confidence,
                amap_id,
                "风景名胜",
                "",
                "",
                "",
                None,
                "[]",
            ),
        )
    for index, poi_id in enumerate(["poi_verified_a", "poi_draft", "poi_verified_b"], start=1):
        connection.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time,
                poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"seg_draft_{index}",
                plan_id,
                "day_draft_refresh",
                index,
                "visit",
                f"0{8 + index}:00",
                f"0{9 + index}:00",
                poi_id,
                "walk",
                0,
                "",
                None,
                None,
                None,
            ),
        )
    connection.commit()
    return plan_id


class RecordingRouteService(StubRouteService):
    def __init__(self):
        self.route_pairs = None
        self.warnings = []
        self.calls = 0

    def build_routes(self, plan_id, pois, transport_mode="public_transit", segments=None, route_pairs=None):
        self.calls += 1
        self.route_pairs = route_pairs
        return super().build_routes(plan_id, pois, transport_mode, segments=segments, route_pairs=route_pairs)


def test_schema_does_not_create_persistent_poi_grounding_cache():
    clear_database()
    with open_db() as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'amap_poi_grounding_cache'"
        ).fetchone()

    assert row is None


def test_get_plan_filters_routes_with_missing_timeline_segment():
    clear_database()
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO route_options (
                id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                provider, mode, label, is_selected, sort_order, transport_mode,
                distance_meters, duration_seconds, duration_minutes, cost_amount,
                cost_currency, cost_estimate, crowding_risk, source, polyline_json,
                steps_json, provider_payload_json, error_json, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "route_orphan_segment",
                plan_id,
                "seg_deleted",
                "seg_draft_2",
                "poi_deleted",
                "poi_draft",
                "test",
                "walking",
                "步行",
                1,
                1,
                "walking",
                1200,
                20 * 60,
                20,
                0,
                "CNY",
                0,
                "low",
                "test",
                "[[116,39],[116.1,39.1]]",
                "[]",
                "{}",
                None,
                now,
            ),
        )
        connection.commit()
        plan = ItineraryService(connection).get_plan(plan_id)

    assert plan.route_options == []


def test_repeated_poi_grounding_does_not_use_cross_request_cache():
    clear_database()
    map_service = KeywordEchoMapPoiService()
    with open_db() as connection:
        first = ItineraryService(connection, map_poi_service=map_service)._resolve_amap_poi("北京", "北京大学", [])
        second = ItineraryService(connection, map_poi_service=map_service)._resolve_amap_poi("北京", "北京大学", [])

    assert first is not None
    assert second is not None
    assert map_service.keywords == ["北京大学", "北京大学"]


def test_area_poi_is_automatically_refined_to_concrete_amap_poi():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "北京 奥林匹克公园 夜景 地标": [
                map_candidate("B0TOWER001", "奥林匹克塔", "风景名胜;塔;观景点", confidence=0.95),
                map_candidate("B0STADIUM1", "国家体育场", "体育休闲服务;体育场馆", confidence=0.82),
            ]
        }
    )
    with open_db() as connection:
        resolved = ItineraryService(connection, map_poi_service=map_service)._resolve_amap_poi(
            "北京", "奥林匹克公园夜景", []
        )

    assert resolved is not None
    assert resolved.source == AMAP_PLACE_SOURCE
    assert resolved.name == "奥林匹克塔"
    assert "agent_selected_candidate" in resolved.source_note
    assert map_service.keywords[:2] == ["北京 奥林匹克公园 夜景 地标", "北京 奥林匹克公园 夜景 观景点"]


def test_grounding_auto_concrete_area_replaces_with_verified_amap_poi():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "北京 奥林匹克公园 夜景 地标": [
                map_candidate("B0TOWER001", "奥林匹克塔", "风景名胜;塔;观景点", confidence=0.95),
                map_candidate("B0STADIUM1", "国家体育场", "体育休闲服务;体育场馆", confidence=0.82),
            ]
        }
    )
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, confidence = ?, source_note = ?
            WHERE id = ?
            """,
            ("奥林匹克公园夜景", 0.55, "Agent 先写入可编辑时间轴，占位 POI 未完成地图 grounding。", "poi_draft"),
        )
        service = ItineraryService(connection, map_poi_service=map_service)
        plan = service._load_plan(plan_id)
        pois = service._load_pois(plan_id)
        grounded, warnings = service._ground_agent_text_timeline_pois(plan, pois, only_poi_ids={"poi_draft"})
        connection.commit()
        row = connection.execute(
            "SELECT name, source, amap_id, latitude, longitude, source_note FROM pois WHERE id = ?",
            ("poi_draft",),
        ).fetchone()
        response = service._poi_response(next(poi for poi in service._load_pois(plan_id) if poi.id == "poi_draft"))

    assert grounded is True
    assert map_service.keywords[:2] == ["北京 奥林匹克公园 夜景 地标", "北京 奥林匹克公园 夜景 观景点"]
    assert row["name"] == "奥林匹克塔"
    assert row["source"] == AMAP_PLACE_SOURCE
    assert row["amap_id"] == "B0TOWER001"
    assert row["latitude"] == 39.9
    assert row["longitude"] == 116.4
    assert "agent_selected_candidate" in row["source_note"]
    assert response.grounding_status == "agent_selected_candidate"
    assert response.map_ready is True
    assert response.routeable is True
    assert response.matched_amap_name == "奥林匹克塔"
    assert response.needs_concrete_poi is False
    assert not any("待校验地图锚点" in warning for warning in warnings)


def test_staged_enrichment_does_not_revive_semantically_rejected_museum_candidate():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "博物馆或美术馆": [
                map_candidate(
                    "B0SCENICMISMATCH",
                    "八达岭长城",
                    "风景名胜;风景名胜;国家级景点",
                    confidence=0.95,
                )
            ]
        }
    )
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, source = ?, amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = ?, type = ?, source_note = ?
            WHERE id = ?
            """,
            (
                "博物馆或美术馆",
                "agent-text-timeline",
                0.35,
                "地图候选待补全",
                "intentType=museum；groundingStatus=waiting_for_poi_grounding；needsConcretePoi=true",
                "poi_draft",
            ),
        )
        service = ItineraryService(connection, map_poi_service=map_service)
        plan = service._load_plan(plan_id)
        pois = service._load_pois(plan_id)

        grounded, warnings = service._ground_agent_text_timeline_pois(
            plan,
            pois,
            only_poi_ids={"poi_draft"},
        )
        connection.commit()
        row = connection.execute(
            "SELECT name, source, amap_id, latitude, longitude, source_note FROM pois WHERE id = ?",
            ("poi_draft",),
        ).fetchone()

    assert grounded is False
    assert row["name"] == "博物馆或美术馆"
    assert row["source"] == "agent-text-timeline"
    assert row["amap_id"] is None
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert "groundingStatus=waiting_for_poi_grounding" in row["source_note"]
    assert any("museum_semantic_mismatch" in warning for warning in warnings)


def test_broad_museum_candidate_query_starts_with_provider_searchable_museum_class():
    service = ItineraryService(sqlite3.connect(":memory:"))

    queries = service._candidate_search_queries("北京", "北京当地博物馆", "museum", "functional")

    assert queries[0] == "博物馆"
    assert "北京当地博物馆" in queries


def test_grounding_functional_poi_uses_nearby_search_and_replaces_function_word():
    clear_database()
    map_service = KeywordEchoMapPoiService()
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, source = ?, amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = ?, source_note = ?
            WHERE id = ?
            """,
            ("晚餐", "agent-text-timeline", 0.35, "Agent 先写入功能型地点，等待附近搜索补全。", "poi_draft"),
        )
        service = ItineraryService(connection, map_poi_service=map_service)
        plan = service._load_plan(plan_id)
        pois = service._load_pois(plan_id)
        grounded, warnings = service._ground_agent_text_timeline_pois(plan, pois, only_poi_ids={"poi_draft"})
        connection.commit()
        row = connection.execute(
            "SELECT name, source, amap_id, latitude, longitude, source_note FROM pois WHERE id = ?",
            ("poi_draft",),
        ).fetchone()
        response = service._poi_response(next(poi for poi in service._load_pois(plan_id) if poi.id == "poi_draft"))

    assert grounded is True
    assert map_service.nearby_calls == [
        {"keyword": "北京 晚餐 餐厅", "longitude": 116.397, "latitude": 39.918, "category": "food", "radius": 1500},
        {"keyword": "晚餐", "longitude": 116.397, "latitude": 39.918, "category": "food", "radius": 1500},
    ]
    assert row["name"] == "附近餐厅"
    assert row["source"] == AMAP_PLACE_SOURCE
    assert row["amap_id"] == "amap_nearby_restaurant"
    assert row["latitude"] == pytest.approx(39.919)
    assert row["longitude"] == pytest.approx(116.398)
    assert "自动补全" in row["source_note"]
    assert response.grounding_status == "verified_amap"
    assert response.needs_concrete_poi is False
    assert any("自动补全" in warning for warning in warnings)


def test_grounding_stops_early_when_amap_rate_limited():
    clear_database()
    map_service = RateLimitedMapPoiService()
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        service = ItineraryService(connection, map_poi_service=map_service)
        plan = service._load_plan(plan_id)
        pois = service._load_pois(plan_id)
        grounded, warnings = service._ground_agent_text_timeline_pois(plan, pois)

    assert grounded is False
    assert map_service.calls == ["北京大学"]
    assert any("频率限制" in warning for warning in warnings)


def test_candidate_first_guomao_night_view_filters_lodging_candidates():
    clear_database()
    query = "北京 国贸CBD 夜景 地标"
    map_service = CandidateListMapPoiService(
        {
            query: [
                map_candidate("B0HOTEL001", "国贸民宿", "住宿服务;民宿"),
                map_candidate("B0PARKING1", "国贸停车场", "交通设施服务;停车场"),
                map_candidate("B0LANDMARK1", "国贸桥", "风景名胜;地标;桥"),
            ]
        }
    )
    with open_db() as connection:
        resolved = ItineraryService(connection, map_poi_service=map_service)._resolve_amap_poi(
            "北京", "国贸CBD夜景", []
        )

    assert resolved is not None
    assert resolved.name == "国贸桥"
    assert resolved.amap_id == "B0LANDMARK1"
    assert map_service.keywords[0] == query


def test_candidate_first_avoids_same_day_duplicate_poi():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "北京 奥林匹克公园 夜景 地标": [
                map_candidate("B0STADIUM1", "国家体育场", "体育休闲服务;体育场馆"),
                map_candidate("B0TOWER001", "奥林匹克塔", "风景名胜;塔;观景点"),
            ]
        }
    )
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        connection.execute(
            "UPDATE pois SET name = ?, amap_id = ?, source = ?, confidence = ? WHERE id = ?",
            ("国家体育场", "B0STADIUM1", AMAP_PLACE_SOURCE, 0.9, "poi_verified_a"),
        )
        connection.execute("UPDATE pois SET name = ? WHERE id = ?", ("奥林匹克公园夜景", "poi_draft"))
        service = ItineraryService(connection, map_poi_service=map_service)
        plan = service._load_plan(plan_id)
        pois = service._load_pois(plan_id)
        resolved = service._resolve_amap_poi(
            "北京",
            "奥林匹克公园夜景",
            [],
            plan=plan,
            original=next(poi for poi in pois if poi.id == "poi_draft"),
            pois_by_id={poi.id: poi for poi in pois},
            resolved_by_id={},
        )

    assert resolved is not None
    assert resolved.name == "奥林匹克塔"
    assert resolved.amap_id == "B0TOWER001"


def test_candidate_first_clear_top_candidate_does_not_create_pending_choice():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "北京大学": [
                map_candidate("PKU1", "北京大学", "科教文化服务;学校;高等院校", confidence=0.95),
                map_candidate("SHOP1", "北京大学停车场", "交通设施服务;停车场", confidence=0.9),
            ]
        }
    )
    with open_db() as connection:
        resolved = ItineraryService(connection, map_poi_service=map_service)._resolve_amap_poi("北京", "北京大学", [])
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM amap_poi_candidates WHERE status = 'pending'"
        ).fetchone()[0]

    assert resolved is not None
    assert resolved.name == "北京大学"
    assert pending_count == 0


def test_candidate_first_area_unresolved_is_reachable_when_candidates_are_unclear():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "北京 奥林匹克公园 夜景 地标": [
                map_candidate("LOW1", "奥林匹克公园停车场", "交通设施服务;停车场", confidence=0.95),
                map_candidate("LOW2", "奥林匹克公园周边公寓", "住宿服务;公寓", confidence=0.95),
            ]
        }
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        resolved = service._resolve_amap_poi("北京", "奥林匹克公园夜景", [])
        response = service._poi_response(resolved)

    assert resolved is not None
    assert resolved.amap_id is None
    assert "area_unresolved" in resolved.source_note
    assert response.grounding_status == "area_unresolved"
    assert response.map_ready is False


def test_exact_entity_strong_match_is_verified_amap():
    clear_database()
    map_service = CandidateListMapPoiService(
        {
            "清华大学": [
                map_candidate("THU1", "清华大学", "科教文化服务;学校;高等院校", confidence=0.95),
                map_candidate("SHOP1", "清华大学停车场", "交通设施服务;停车场", confidence=0.9),
            ]
        }
    )
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        resolved = service._resolve_amap_poi("北京", "清华大学", [])
        response = service._poi_response(resolved)

    assert resolved is not None
    assert resolved.name == "清华大学"
    assert "verified_amap" in resolved.source_note
    assert response.grounding_status == "verified_amap"


def test_candidate_collection_stops_after_amap_rate_limit_and_keeps_successes():
    clear_database()
    map_service = PartialRateLimitedMapPoiService()
    with open_db() as connection:
        service = ItineraryService(connection, map_poi_service=map_service)
        resolved = service._resolve_amap_poi("北京", "国贸CBD夜景", [])

    assert resolved is not None
    assert "provider_rate_limited" in resolved.source_note
    assert map_service.calls == ["北京 国贸CBD 夜景 地标", "北京 国贸CBD 夜景 观景点"]
    assert service._poi_grounding_rate_limited is True


def test_refresh_planning_tools_stops_after_poi_rate_limit_without_routes():
    clear_database()
    route_service = RecordingRouteService()
    map_service = RateLimitedMapPoiService()
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        warnings = ItineraryService(
            connection,
            route_service=route_service,
            map_poi_service=map_service,
        ).refresh_planning_tools(plan_id, preferred_mode="walk")
        route_count = connection.execute("SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (plan_id,)).fetchone()[
            0
        ]
        weather_count = connection.execute(
            "SELECT COUNT(*) FROM weather_signals WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]
        ticket_count = connection.execute("SELECT COUNT(*) FROM ticket_lookup_results").fetchone()[0]
        risk_count = connection.execute(
            "SELECT COUNT(*) FROM poi_risk_alerts WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]

    assert map_service.calls == ["北京大学"]
    assert route_service.calls == 0
    assert route_count == 0
    assert weather_count == 0
    assert ticket_count == 0
    assert risk_count == 0
    assert any("provider_rate_limited" in warning for warning in warnings)


def test_refresh_unfinished_pois_only_retries_draft_and_refreshes_touching_routes():
    clear_database()
    route_service = RecordingRouteService()
    map_service = KeywordEchoMapPoiService()
    with open_db() as connection:
        plan_id = seed_three_segment_draft_plan(connection)
        warnings = ItineraryService(
            connection, route_service=route_service, map_poi_service=map_service
        ).refresh_unfinished_pois(plan_id, preferred_mode="walk")
        connection.commit()
        poi_rows = connection.execute(
            "SELECT name, source, amap_id FROM pois WHERE plan_id = ? ORDER BY id", (plan_id,)
        ).fetchall()

    assert map_service.keywords == ["北京大学"]
    assert route_service.route_pairs == {("seg_draft_1", "seg_draft_2"), ("seg_draft_2", "seg_draft_3")}
    assert any(row["name"] == "北京大学" and row["source"] == AMAP_PLACE_SOURCE and row["amap_id"] for row in poi_rows)
    assert not any("高德天气" in warning or "票务" in warning for warning in warnings)

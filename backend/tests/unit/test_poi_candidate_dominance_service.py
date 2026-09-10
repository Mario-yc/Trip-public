from __future__ import annotations

from src.services.poi_candidate_dominance_service import PoiCandidateDominanceService


def _candidate(
    candidate_id: str,
    name: str,
    *,
    category: str = "museum",
    distance: int = 500,
    semantic: float = 0.95,
    provider: str = "amap",
    city: str = "北京",
    longitude: float | None = 116.3,
    latitude: float | None = 39.9,
    price_level: str = "medium",
    cuisine: str = "北京菜",
    entity_key: str | None = None,
):
    return {
        "candidateId": candidate_id,
        "amapPoiId": f"amap_{candidate_id}",
        "name": name,
        "category": category,
        "distanceMeters": distance,
        "semanticScore": semantic,
        "provider": provider,
        "city": city,
        "longitude": longitude,
        "latitude": latitude,
        "priceLevel": price_level,
        "cuisine": cuisine,
        "entityKey": entity_key or candidate_id,
        "openStatus": "unknown",
    }


def test_unique_safe_museum_candidate_is_auto_selected():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "museum", "searchText": "清华美术馆", "city": "北京"},
        candidates=[_candidate("museum", "清华大学艺术博物馆")],
        route_context={"maxDetourMeters": 3000},
    )

    assert result.action == "auto_select"
    assert result.selected_candidate_id == "museum"
    assert result.evidence["dominant"] is True


def test_wrong_category_shop_and_parking_are_filtered_before_auto_select():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "museum", "searchText": "大学艺术博物馆", "city": "北京"},
        candidates=[
            _candidate("museum", "大学艺术博物馆"),
            _candidate("shop", "大学艺术博物馆文创商店", category="shop"),
            _candidate("parking", "大学艺术博物馆停车场", category="parking"),
        ],
        route_context={"maxDetourMeters": 3000},
    )

    assert result.action == "auto_select"
    assert result.selected_candidate_id == "museum"
    assert {item["candidateId"] for item in result.rejected_candidates} == {"shop", "parking"}


def test_close_equally_safe_museums_require_material_choice():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "museum", "searchText": "艺术馆", "city": "北京"},
        candidates=[
            _candidate("a", "甲艺术馆", distance=500, semantic=0.91),
            _candidate("b", "乙艺术馆", distance=540, semantic=0.90),
        ],
        route_context={"maxDetourMeters": 3000},
    )

    assert result.action == "ask_user"
    assert result.selected_candidate_id is None
    assert result.evidence["materialTradeoff"] is True


def test_non_amap_wrong_city_or_missing_coordinates_are_never_safe():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "museum", "searchText": "博物馆", "city": "北京"},
        candidates=[
            _candidate("web", "网页博物馆", provider="web"),
            _candidate("shanghai", "上海博物馆", city="上海"),
            _candidate("no_coord", "无坐标博物馆", longitude=None, latitude=None),
        ],
        route_context={"maxDetourMeters": 3000},
    )

    assert result.action == "unresolved"
    assert result.safe_candidates == []


def test_unique_route_appropriate_beijing_restaurant_is_auto_selected():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "meal", "searchText": "北京特色美食", "city": "北京", "budget": "medium"},
        candidates=[
            _candidate("duck", "本地烤鸭店", category="restaurant", distance=450, cuisine="北京菜"),
            _candidate("hotel", "酒店西餐厅", category="hotel_restaurant", distance=300, cuisine="西餐"),
        ],
        route_context={"maxDetourMeters": 1800},
    )

    assert result.action == "auto_select"
    assert result.selected_candidate_id == "duck"


def test_restaurant_cuisine_tradeoff_asks_user():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "meal", "searchText": "北京特色美食", "city": "北京", "budget": "medium"},
        candidates=[
            _candidate("duck", "烤鸭店", category="restaurant", distance=500, cuisine="烤鸭"),
            _candidate("hotpot", "铜锅涮肉", category="restaurant", distance=520, cuisine="涮肉"),
        ],
        route_context={"maxDetourMeters": 1800},
    )

    assert result.action == "ask_user"
    assert result.evidence["materialTradeoff"] is True


def test_large_coarse_distance_only_ranks_and_does_not_remove_candidate():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "meal", "searchText": "北京特色美食", "city": "北京", "budget": "medium"},
        candidates=[
            _candidate("far", "远处名店", category="restaurant", distance=9000, semantic=0.99),
            _candidate("near", "附近北京菜", category="restaurant", distance=600, semantic=0.88),
        ],
        route_context={"maxDetourMeters": 1800},
    )

    assert {item["candidateId"] for item in result.safe_candidates} == {"far", "near"}
    assert not any("detour_exceeds_limit" in item["reasonCodes"] for item in result.rejected_candidates)


def test_recent_entity_is_filtered_and_no_safe_result_stays_unresolved():
    result = PoiCandidateDominanceService().decide(
        intent={"intentType": "meal", "searchText": "北京特色美食", "city": "北京", "budget": "medium"},
        candidates=[_candidate("repeat", "已吃过的店", category="restaurant", entity_key="used_entity")],
        route_context={"maxDetourMeters": 1800, "recentEntityKeys": ["used_entity"]},
    )

    assert result.action == "unresolved"
    assert result.selected_candidate_id is None
    assert result.evidence["reason"] == "no_safe_candidate"

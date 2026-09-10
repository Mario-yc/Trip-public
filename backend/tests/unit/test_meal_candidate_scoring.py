from datetime import datetime, timezone
import sqlite3

from src.api.schemas.maps import MapPoiResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.models.poi_intent import PoiIntent
from src.services.itinerary_service import ItineraryService
from src.services.map_poi_service import AMAP_PLACE_SOURCE


def open_db() -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_path_from_url(get_settings().database_url))
    connection.row_factory = sqlite3.Row
    return connection


def test_meal_candidate_scoring_rejects_weak_service_subentities():
    connection = open_db()
    service = ItineraryService(connection)
    intent = PoiIntent(
        raw_need="午餐 当地特色美食",
        city="北京",
        day_number=1,
        time_window="12:00-13:15",
        intent_type="meal",
        specificity="functional",
        preferred_types=["餐饮服务"],
        rejected_types=["酒店", "停车场", "公司"],
    )
    weak = MapPoiResponse(
        id="weak_service_counter",
        name="北京本地菜餐厅服务台",
        type="餐饮服务;中餐厅",
        city="北京",
        district="测试区",
        address="测试路",
        longitude=116.39,
        latitude=39.91,
        category="food",
        source=AMAP_PLACE_SOURCE,
        sourceNote="高德 WebService POI 搜索",
        confidence=0.94,
        photos=[],
        queriedAt=datetime.now(timezone.utc),
    )
    strong = MapPoiResponse(
        id="strong_food_anchor",
        name="护国寺小吃(测试店)",
        type="餐饮服务;中餐厅",
        city="北京",
        district="测试区",
        address="测试路",
        longitude=116.391,
        latitude=39.911,
        category="food",
        source=AMAP_PLACE_SOURCE,
        sourceNote="高德 WebService POI 搜索",
        providerTypeCode="050100",
        tags=["地方小吃"],
        sourceClaims=[{"claimKey": "local_food", "stance": "support", "locality": "北京"}],
        confidence=0.94,
        photos=[],
        queriedAt=datetime.now(timezone.utc),
    )

    scored, rejected = service._score_poi_candidates(intent, [weak, strong])

    assert [item.candidate.name for item in scored] == ["护国寺小吃(测试店)"]
    weak_rejection = next(item for item in rejected if item.candidate.name == "北京本地菜餐厅服务台")
    assert "weak_subentity" in weak_rejection.rejected_reasons
    connection.close()


def test_meal_candidate_scoring_rejects_generic_placeholder_names():
    connection = open_db()
    service = ItineraryService(connection)
    intent = PoiIntent(
        raw_need="午餐 当地特色美食",
        city="北京",
        day_number=1,
        time_window="12:00-13:15",
        intent_type="meal",
        specificity="functional",
        preferred_types=["餐饮服务"],
        rejected_types=[],
    )
    candidates = [
        MapPoiResponse(
            id=f"generic_{index}",
            name=name,
            type="餐饮服务;中餐厅",
            city="北京",
            district="测试区",
            address="测试路",
            longitude=116.39 + index * 0.001,
            latitude=39.91 + index * 0.001,
            category="food",
            source=AMAP_PLACE_SOURCE,
            sourceNote="高德 WebService POI 搜索",
            confidence=0.94,
            photos=[],
            queriedAt=datetime.now(timezone.utc),
        )
        for index, name in enumerate(["北京本地菜餐厅", "北京特色小吃馆", "北京老字号餐厅", "北京家常菜馆"], start=1)
    ]

    scored, rejected = service._score_poi_candidates(intent, candidates)

    assert scored == []
    assert {item.candidate.name for item in rejected} == {candidate.name for candidate in candidates}
    assert all("generic_meal_placeholder_candidate" in item.rejected_reasons for item in rejected)
    connection.close()


def test_meal_candidate_scoring_rejects_mock_food_candidates():
    connection = open_db()
    service = ItineraryService(connection)
    intent = PoiIntent(
        raw_need="午餐 当地特色美食",
        city="北京",
        day_number=1,
        time_window="12:00-13:15",
        intent_type="meal",
        specificity="functional",
        preferred_types=["餐饮服务"],
        rejected_types=[],
    )
    mock = MapPoiResponse(
        id="mock_amap_food_1",
        name="北京本地菜餐厅",
        type="餐饮服务;中餐厅",
        city="北京",
        district="测试区",
        address="测试路",
        longitude=116.39,
        latitude=39.91,
        category="food",
        source=AMAP_PLACE_SOURCE,
        sourceNote="CLI eval mock AMap candidate; only used when mockMapProvider is enabled.",
        confidence=0.92,
        photos=[],
        queriedAt=datetime.now(timezone.utc),
    )

    scored, rejected = service._score_poi_candidates(intent, [mock])

    assert scored == []
    assert rejected[0].candidate.name == "北京本地菜餐厅"
    assert "mock_or_synthetic_candidate" in rejected[0].rejected_reasons
    connection.close()

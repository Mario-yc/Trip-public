import json
import sqlite3
from typing import Optional

import pytest
from fastapi import HTTPException

from src.api.schemas.maps import (
    MapPoiResolveQueryRequest,
    MapPoiResolveRequest,
    MapPoiResponse,
    MapPoiSearchResponse,
)
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.poi_resolution_service import PoiResolutionService


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM amap_poi_candidates;
            DELETE FROM conversation_sessions;
            """
        )


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def test_poi_resolution_accepts_unique_high_confidence_amap_poi():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(connection, map_service=FakeMapService([poi_fixture("故宫博物院")]))
        result = service.resolve(resolve_request("故宫"))
        pending_count = connection.execute("SELECT COUNT(*) FROM amap_poi_candidates").fetchone()[0]

    assert result.resolved[0].poi.id == "amap_故宫博物院"
    assert result.pending == []
    assert pending_count == 0


def test_poi_resolution_accepts_clear_first_candidate_entities():
    clear_database()
    with open_db() as connection:
        palace = PoiResolutionService(connection, map_service=FakeMapService([poi_fixture("故宫博物院")])).resolve(
            resolve_request("故宫博物院")
        )
        university = PoiResolutionService(connection, map_service=FakeMapService([poi_fixture("清华大学", type_name="科教文化服务;学校;高等院校")])).resolve(
            resolve_request("清华大学", category="education")
        )
        pending_count = connection.execute("SELECT COUNT(*) FROM amap_poi_candidates").fetchone()[0]

    assert palace.resolved[0].poi.name == "故宫博物院"
    assert university.resolved[0].poi.name == "清华大学"
    assert pending_count == 0


def test_poi_resolution_rejects_non_985_unique_candidate_for_explicit_985_need():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [poi_fixture("北京工商大学", type_name="科教文化服务;学校;高等院校")]
            ),
        )
        result = service.resolve(resolve_request("985高校参观", category="education"))
        row = connection.execute(
            "SELECT status, candidates_json FROM amap_poi_candidates"
        ).fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "no_safe_candidate"
    assert result.pending[0].candidates == []
    assert row["status"] == "rejected"
    assert json.loads(row["candidates_json"]) == []


def test_poi_resolution_accepts_exact_match_inside_unrelated_amap_candidates():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("天坛公园"),
                    poi_fixture("地坛公园"),
                    poi_fixture("什刹海"),
                    poi_fixture("故宫博物院"),
                    poi_fixture("圆明园遗址公园"),
                    poi_fixture("故宫博物院-皇极殿"),
                    poi_fixture("北海公园"),
                ]
            ),
        )
        result = service.resolve(resolve_request("故宫博物院", category="scenic"))
        pending_count = connection.execute("SELECT COUNT(*) FROM amap_poi_candidates").fetchone()[0]

    assert result.pending == []
    assert result.resolved[0].poi.name == "故宫博物院"
    assert pending_count == 0


def test_poi_resolution_filters_semantic_museum_mismatches_before_pending_display():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("花海畔溪谷", type_name="风景名胜;风景名胜;风景名胜"),
                    poi_fixture("中国美术馆", type_name="科教文化服务;博物馆;美术馆"),
                    poi_fixture("今日美术馆", type_name="科教文化服务;博物馆;美术馆"),
                ]
            ),
        )
        result = service.resolve(resolve_request("美术馆", category="museum"))
        row = connection.execute("SELECT candidates_json FROM amap_poi_candidates").fetchone()

    displayed = json.loads(row["candidates_json"])
    assert [candidate.name for candidate in result.pending[0].candidates] == ["中国美术馆", "今日美术馆"]
    assert [candidate["name"] for candidate in displayed] == ["中国美术馆", "今日美术馆"]


@pytest.mark.parametrize(
    ("candidate_name", "candidate_type"),
    [
        ("中国美术馆", "科教文化服务;博物馆;美术馆"),
        ("清华大学艺术博物馆停车场", "交通设施服务;停车场;公共停车场"),
        ("清华大学艺术博物馆地下车库", "交通设施服务;停车场;地下车库"),
        ("清华大学艺术博物馆停车楼", "交通设施服务;停车场;停车楼"),
        ("清华大学艺术博物馆东门", "通行设施;门;东门"),
        ("清华大学艺术博物馆", "风景名胜;风景名胜"),
    ],
)
def test_poi_resolution_rejects_wrong_entity_or_subordinate_facility_for_named_museum(
    candidate_name: str,
    candidate_type: str,
):
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService([poi_fixture(candidate_name, type_name=candidate_type)]),
        )
        result = service.resolve(resolve_request("清华美术馆", category="museum"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "no_safe_candidate"
    assert result.pending[0].candidates == []
    assert row["status"] == "rejected"


def test_poi_resolution_does_not_accept_unrelated_defaults_for_explicit_university():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("故宫博物院"),
                    poi_fixture("天坛公园"),
                ]
            ),
        )
        result = service.resolve(resolve_request("北京大学", category="education"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "no_safe_candidate"
    assert result.pending[0].candidates == []
    assert row["query"] == "北京大学"
    assert row["status"] == "rejected"


def test_poi_resolution_sends_multiple_strong_matches_to_pending():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService([poi_fixture("胡同餐厅(东城店)"), poi_fixture("胡同餐厅(西城店)")]),
        )
        result = service.resolve(resolve_request("胡同餐厅", category="food"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "material_tradeoff"
    assert row["query"] == "胡同餐厅"
    assert row["status"] == "pending"


def test_poi_resolution_accepts_nearest_unique_match_when_near_context_is_available():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("胡同餐厅(东城店)", distance_meters=55, longitude=116.3971, latitude=39.9181),
                    poi_fixture("胡同餐厅(西城店)", distance_meters=260, longitude=116.3900, latitude=39.9100),
                ]
            ),
        )
        result = service.resolve(resolve_request("胡同餐厅", category="food", near=(116.397026, 39.918058)))
        pending_count = connection.execute("SELECT COUNT(*) FROM amap_poi_candidates").fetchone()[0]

    assert result.pending == []
    assert result.resolved[0].poi.name == "胡同餐厅(东城店)"
    assert pending_count == 0


def test_poi_resolution_keeps_nearby_ambiguous_matches_pending():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("胡同餐厅(东城店)", distance_meters=55, longitude=116.3971, latitude=39.9181),
                    poi_fixture("胡同餐厅(景山店)", distance_meters=95, longitude=116.3972, latitude=39.9182),
                ]
            ),
        )
        result = service.resolve(resolve_request("胡同餐厅", category="food", near=(116.397026, 39.918058)))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "material_tradeoff"
    assert row["query"] == "胡同餐厅"


def test_poi_resolution_keeps_multiple_campus_matches_pending():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("北京大学(燕园校区)", type_name="科教文化服务;学校;高等院校"),
                    poi_fixture("北京大学医学部", type_name="科教文化服务;学校;高等院校"),
                ]
            ),
        )
        result = service.resolve(resolve_request("北京大学", category="education"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "material_tradeoff"
    assert row["query"] == "北京大学"


def test_poi_resolution_keeps_exact_university_with_related_campus_pending():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("北京大学", type_name="科教文化服务;学校;高等院校"),
                    poi_fixture("北京大学医学部", type_name="科教文化服务;学校;高等院校"),
                ]
            ),
        )
        result = service.resolve(resolve_request("北京大学", category="education"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "material_tradeoff"
    assert row["query"] == "北京大学"


def test_poi_resolution_rejects_invalid_amap_candidates():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(
            connection,
            map_service=FakeMapService(
                [
                    poi_fixture("故宫博物院", amap_id=""),
                    poi_fixture("故宫博物院", longitude=0, latitude=0),
                ]
            ),
        )
        result = service.resolve(resolve_request("故宫博物院"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "low_confidence"
    assert row["query"] == "故宫博物院"


def test_poi_resolution_sends_low_confidence_match_to_pending():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(connection, map_service=FakeMapService([poi_fixture("相似地点", confidence=0.62)]))
        result = service.resolve(resolve_request("故宫"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "low_confidence"
    assert row["candidates_json"]


def test_poi_resolution_records_no_results_as_pending_without_final_poi():
    clear_database()
    with open_db() as connection:
        service = PoiResolutionService(connection, map_service=FakeMapService([]))
        result = service.resolve(resolve_request("不存在的地点"))
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()

    assert result.resolved == []
    assert result.pending[0].reason == "no_results"
    assert row["query"] == "不存在的地点"


def test_poi_resolution_propagates_key_missing_and_provider_failures():
    clear_database()
    with open_db() as connection:
        with pytest.raises(HTTPException) as missing_key:
            PoiResolutionService(connection, map_service=FailingMapService(400, "MAP_PROVIDER_KEY is not configured")).resolve(
                resolve_request("故宫")
            )
        with pytest.raises(HTTPException) as provider_failure:
            PoiResolutionService(connection, map_service=FailingMapService(502, "AMap POI search failed: INVALID_USER_KEY")).resolve(
                resolve_request("故宫")
            )
        with pytest.raises(HTTPException) as quota_failure:
            PoiResolutionService(
                connection,
                map_service=FailingMapService(502, "高德 POI 查询频率超限，请稍后重试。行程未写入，避免插入未确认地点。"),
            ).resolve(resolve_request("故宫"))

    assert missing_key.value.status_code == 400
    assert "MAP_PROVIDER_KEY" in missing_key.value.detail
    assert provider_failure.value.status_code == 502
    assert "AMap POI search failed" in provider_failure.value.detail
    assert quota_failure.value.status_code == 502
    assert "高德 POI 查询频率超限" in quota_failure.value.detail


def resolve_request(name: str, category: str = "scenic", near: Optional[tuple[float, float]] = None) -> MapPoiResolveRequest:
    near_payload = None
    if near is not None:
        near_payload = {"longitude": near[0], "latitude": near[1], "radius": 300}
    return MapPoiResolveRequest(
        sessionId="sess_test",
        turnId="turn_test",
        city="北京",
        queries=[MapPoiResolveQueryRequest(name=name, category=category, near=near_payload)],
    )


class FakeMapService:
    def __init__(self, pois: list[MapPoiResponse]):
        self.pois = pois

    def search(self, city: str, keyword: str, category: str = "all", limit: int = 12) -> MapPoiSearchResponse:
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt="2026-06-10T00:00:00Z",
            pois=self.pois,
        )

    def search_nearby(self, **kwargs) -> MapPoiSearchResponse:
        return self.search(kwargs["city"], kwargs["keyword"], kwargs.get("category", "all"))


class FailingMapService:
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail

    def search(self, *_args, **_kwargs):
        raise HTTPException(status_code=self.status_code, detail=self.detail)


def poi_fixture(
    name: str,
    confidence: float = 0.86,
    type_name: str = "风景名胜",
    amap_id: Optional[str] = None,
    longitude: float = 116.397026,
    latitude: float = 39.918058,
    distance_meters: Optional[float] = None,
) -> MapPoiResponse:
    return MapPoiResponse(
        id=f"amap_{name}" if amap_id is None else amap_id,
        name=name,
        type=type_name,
        city="北京市",
        district="东城区",
        address="景山前街4号",
        longitude=longitude,
        latitude=latitude,
        category="scenic",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索",
        distanceMeters=distance_meters,
        confidence=confidence,
        photos=[],
    )

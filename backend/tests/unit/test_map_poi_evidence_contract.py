import hashlib
import json
import re

import pytest
from fastapi import HTTPException

from src.api.schemas.maps import MapPoiResponse
from src.models.poi import POI
from src.services.agent_service import AgentService
from src.services.amap_call_budget import AmapCallBudget, amap_call_budget_scope
from src.services.experience_independence_service import ExperienceIndependenceService
from src.services.map_poi_service import (
    AMAP_PLACE_AROUND_MAX_RADIUS_METERS,
    CATEGORY_QUERY,
    MapPoiService,
    clear_map_poi_runtime_state,
)
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


def test_map_poi_contract_preserves_provider_neutral_detail_evidence():
    poi = MapPoiResponse.model_validate(
        {
            "id": "B000000001",
            "name": "社区农贸市场",
            "type": "购物服务;综合市场;农贸市场",
            "city": "上海",
            "district": "黄浦区",
            "address": "示例路 1 号",
            "longitude": 121.48,
            "latitude": 31.23,
            "category": "农贸市场",
            "source": "amap-place-search",
            "sourceNote": "高德地点详情",
            "confidence": 0.9,
            "providerTypeCode": "060703",
            "tags": ["农贸市场", "社区商业"],
            "businessArea": "老城厢",
            "rating": 4.5,
            "cost": 35,
            "openTimeToday": "06:00-19:00",
            "openTimeWeek": "周一至周日 06:00-19:00",
            "parentPoiId": "B000000099",
            "indoorParentPoiId": "B000000088",
            "businessStatus": "营业中",
            "providerQueriedAt": "2026-08-24T08:00:00+00:00",
            "providerQueryReceiptFingerprint": "f" * 64,
            "children": [{"id": "B000000002", "name": "熟食区"}],
            "photos": [{"title": "门头", "url": "https://example.com/1.jpg"}],
        }
    )
    payload = poi.model_dump(by_alias=True)
    assert payload["providerTypeCode"] == "060703"
    assert payload["tags"] == ["农贸市场", "社区商业"]
    assert payload["businessArea"] == "老城厢"
    assert payload["rating"] == 4.5
    assert payload["parentPoiId"] == "B000000099"
    assert payload["indoorParentPoiId"] == "B000000088"
    assert payload["businessStatus"] == "营业中"
    assert payload["providerQueryReceiptFingerprint"] == "f" * 64
    assert payload["children"][0]["name"] == "熟食区"


def test_extensions_all_parser_preserves_detail_fields():
    parsed = MapPoiService(map_provider_key="test")._parse_poi(
        {
            "id": "B000000001",
            "name": "社区市场",
            "type": "购物服务;综合市场;农贸市场",
            "typecode": "060703",
            "tag": "农贸市场;社区商业",
            "business_area": "老城厢",
            "cityname": "上海",
            "adname": "黄浦区",
            "address": "示例路",
            "location": "121.48,31.23",
            "parent": "B000000099",
            "indoor_data": {"cpid": "B000000088"},
            "business_status": "营业中",
            "biz_ext": {"rating": "4.6", "cost": "28", "opentime_today": "06:00-19:00", "opentime_week": "周一至周日"},
            "children": [{"id": "B000000002", "name": "熟食区"}],
        },
        "market",
    )
    assert parsed.provider_type_code == "060703"
    assert parsed.tags == ["农贸市场", "社区商业"]
    assert parsed.rating == 4.6
    assert parsed.cost == 28
    assert parsed.children[0]["id"] == "B000000002"
    assert parsed.indoor_parent_poi_id == "B000000088"
    assert parsed.business_status == "营业中"


def test_park_query_typecodes_match_versioned_standalone_admission_evidence():
    evidence = json.loads(ExperienceIndependenceService.CATEGORY_EVIDENCE_ASSET.read_text(encoding="utf-8"))
    accepted = {str(item["typecode"]) for item in evidence["experienceRoles"]["standalone_park"]["acceptedCategories"]}
    rejected = {str(item["typecode"]) for item in evidence["experienceRoles"]["standalone_park"]["rejectedCategories"]}
    configured = {item for item in CATEGORY_QUERY["park"][1].split("|") if item}

    assert configured == accepted == {"110101", "110103"}
    assert configured.isdisjoint(rejected | {"110200"})


def test_text_search_pagination_is_in_provider_params_cache_and_query_receipt(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls: list[dict[str, str]] = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {
            "pois": [
                {
                    "id": "B000000001",
                    "name": "独立城市公园",
                    "type": "风景名胜;公园广场;公园",
                    "typecode": "110101",
                    "cityname": "测试市",
                    "adname": "测试区",
                    "address": "测试路",
                    "location": "116.3,40.0",
                    "indoor_data": {"cpid": "B000000088"},
                    "business_status": "营业中",
                }
            ]
        }

    monkeypatch.setattr(service, "_fetch_amap_place", fake_fetch)
    scope = "a" * 64
    first = service.search(
        "测试",
        keyword="城市公园",
        category="park",
        page=2,
        offset=7,
        query_scope_fingerprint=scope,
    )
    cached = service.search(
        "测试",
        keyword="城市公园",
        category="park",
        page=2,
        offset=7,
        query_scope_fingerprint=scope,
    )
    next_page = service.search(
        "测试",
        keyword="城市公园",
        category="park",
        page=3,
        offset=7,
        query_scope_fingerprint=scope,
    )

    assert [call["page"] for call in calls] == ["2", "3"]
    assert all(call["offset"] == "7" for call in calls)
    assert all(call["types"] == "110101|110103" for call in calls)
    assert first.cache_hit is False
    assert cached.cache_hit is True
    assert next_page.cache_hit is False
    assert len(first.provider_query_receipt_fingerprint or "") == 64
    assert first.pois[0].provider_query_receipt_fingerprint == first.provider_query_receipt_fingerprint
    assert first.pois[0].provider_queried_at == first.queried_at
    assert cached.provider_query_receipt_fingerprint == first.provider_query_receipt_fingerprint


def test_text_search_pages_are_distinct_in_provider_budget_ledger(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls: list[dict[str, str]] = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {
            "pois": [
                {
                    "id": f"B{int(params['page']):09d}",
                    "name": "独立城市公园",
                    "type": "风景名胜;公园广场;公园",
                    "typecode": "110101",
                    "cityname": "测试市",
                    "adname": "测试区",
                    "address": "测试路",
                    "location": "116.3,40.0",
                }
            ]
        }

    monkeypatch.setattr(service, "_fetch_amap_place", fake_fetch)
    scope = "f" * 64
    budget = AmapCallBudget(place_text_max=2, total_external_max=2, source="pagination_contract_test")
    with amap_call_budget_scope(budget):
        service.search(
            "测试",
            keyword="公园",
            category="park",
            page=1,
            offset=5,
            query_scope_fingerprint=scope,
            bypass_cache=True,
        )
        service.search(
            "测试",
            keyword="公园",
            category="park",
            page=2,
            offset=5,
            query_scope_fingerprint=scope,
            bypass_cache=True,
        )

    assert [item["page"] for item in calls] == ["1", "2"]
    assert budget.used_place_text == 2
    assert budget.duplicate_external_query_count == 0


def test_nearby_pagination_partitions_cache_without_exposing_server_scope(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls: list[dict[str, str]] = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {"pois": []}

    monkeypatch.setattr(service, "_fetch_amap_around", fake_fetch)
    scope = "b" * 64
    service.search_nearby("测试", 116.3, 40.0, "公园", category="park", page=1, offset=5, query_scope_fingerprint=scope)
    service.search_nearby("测试", 116.3, 40.0, "公园", category="park", page=2, offset=5, query_scope_fingerprint=scope)

    assert [call["page"] for call in calls] == ["1", "2"]
    assert all(call["types"] == "110101|110103" for call in calls)
    assert all("query_scope_fingerprint" not in call for call in calls)


def test_nearby_search_can_narrow_a_broad_food_category_with_server_owned_provider_types(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls: list[dict[str, str]] = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {"pois": []}

    monkeypatch.setattr(service, "_fetch_amap_around", fake_fetch)
    service.search_nearby(
        "北京",
        116.189912,
        40.247449,
        "北京菜",
        category="food",
        provider_types="北京菜",
    )

    assert calls[0]["keywords"] == "北京菜"
    assert calls[0]["types"] == "北京菜"


def test_provider_budget_identity_distinguishes_provider_type_filters():
    service = MapPoiService(map_provider_key="test")
    scope = "a" * 64
    broad = service._provider_query_ledger_fingerprint(
        scope,
        {"page": "1", "offset": "5", "types": "050000"},
    )
    local = service._provider_query_ledger_fingerprint(
        scope,
        {"page": "1", "offset": "5", "types": "北京菜"},
    )

    assert broad != local


def test_nearby_search_applies_the_shared_provider_radius_limit(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls: list[dict[str, str]] = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {"pois": []}

    monkeypatch.setattr(service, "_fetch_amap_around", fake_fetch)
    service.search_nearby("测试", 116.3, 40.0, "公园", category="park", radius=8000)

    assert AMAP_PLACE_AROUND_MAX_RADIUS_METERS == 5000
    assert calls[0]["radius"] == str(AMAP_PLACE_AROUND_MAX_RADIUS_METERS)


def test_physical_identity_prefers_parent_then_indoor_parent_then_self():
    assert (
        PoiPhysicalIdentityService.canonical_amap_id(
            {"amapId": "B000000001", "parentPoiId": "B000000099", "indoorParentPoiId": "B000000088"}
        )
        == "B000000099"
    )
    assert (
        PoiPhysicalIdentityService.canonical_amap_id({"amapId": "B000000001", "indoorParentPoiId": "B000000088"})
        == "B000000088"
    )
    assert PoiPhysicalIdentityService.canonical_amap_id({"amapId": "B000000001"}) == "B000000001"


def test_persistable_poi_payload_keeps_provider_and_independence_evidence():
    queried_at = "2026-08-24T08:00:00+00:00"
    poi = POI(
        id="poi_test",
        amap_id="B000000001",
        parent_poi_id="B000000099",
        indoor_parent_poi_id="B000000088",
        name="独立城市公园",
        city="测试",
        category="park",
        latitude=40.0,
        longitude=116.3,
        source="amap-place-search",
        type="风景名胜;公园广场;公园",
        provider_type_code="110101",
        business_status="营业中",
        provider_queried_at=queried_at,
        provider_query_receipt_fingerprint="f" * 64,
        experience_independence_evidence={
            "schemaVersion": "experience-independence-v1",
            "status": "standalone_verified",
            "physicalGroupId": "B000000099",
        },
        photos=[],
    )

    payload = AgentService._persistable_poi_payload(object(), poi)

    assert payload["parentPoiId"] == "B000000099"
    assert payload["indoorParentPoiId"] == "B000000088"
    assert payload["providerTypeCode"] == "110101"
    assert payload["businessStatus"] == "营业中"
    assert payload["providerQueriedAt"] == queried_at
    assert payload["providerQueryReceiptFingerprint"] == "f" * 64
    assert payload["experienceIndependenceEvidence"]["status"] == "standalone_verified"


def test_detail_is_cached_by_amap_id_and_counted_once(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {
            "pois": [
                {
                    "id": params["id"],
                    "name": "社区市场",
                    "type": "购物服务;综合市场",
                    "typecode": "060703",
                    "cityname": "广州",
                    "adname": "越秀区",
                    "address": "示例路",
                    "location": "113.26,23.13",
                }
            ]
        }

    monkeypatch.setattr(service, "_fetch_amap_detail", fake_fetch)
    first = service.detail("B000000001")
    second = service.detail("B000000001")

    assert first.id == second.id == "B000000001"
    assert first.provider_queried_at is not None
    assert re.fullmatch(r"[0-9a-f]{64}", first.provider_query_receipt_fingerprint or "")
    assert second.provider_query_receipt_fingerprint == first.provider_query_receipt_fingerprint
    assert len(calls) == 1
    assert service.detail_fetch_count == 1
    assert service.detail_cache_hit_count == 1


def _query_scope_fingerprint(*, contract_version: str, corridor: str) -> str:
    payload = {
        "city": "北京",
        "intentType": "night_view",
        "experienceFamily": "night_view",
        "dayNumber": 1,
        "occurrenceId": "slot-night-1",
        "routeCorridorHash": corridor,
        "transportMode": "transit",
        "evidenceRequirementFingerprint": "e" * 64,
        "contractVersion": contract_version,
        "anchorPolicy": "previous_only",
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_nearby_cache_is_partitioned_by_complete_server_query_scope(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    calls: list[dict[str, str]] = []

    def fake_fetch(params):
        calls.append(dict(params))
        return {
            "pois": [
                {
                    "id": "B000000001",
                    "name": "滨水公共夜景步道",
                    "type": "风景名胜;水域景观;城市景观",
                    "typecode": "110102",
                    "cityname": "北京市",
                    "adname": "朝阳区",
                    "address": "亮马河沿岸",
                    "location": "116.471,39.952",
                }
            ]
        }

    monkeypatch.setattr(service, "_fetch_amap_around", fake_fetch)
    scope_v7 = _query_scope_fingerprint(contract_version="7", corridor="a" * 64)
    scope_v8 = _query_scope_fingerprint(contract_version="8", corridor="a" * 64)
    other_corridor = _query_scope_fingerprint(contract_version="8", corridor="b" * 64)

    first = service.search_nearby(
        "北京",
        116.46,
        39.95,
        "滨水公共夜景",
        query_scope_fingerprint=scope_v7,
    )
    same_scope = service.search_nearby(
        "北京",
        116.46,
        39.95,
        "滨水公共夜景",
        query_scope_fingerprint=scope_v7,
    )
    changed_contract = service.search_nearby(
        "北京",
        116.46,
        39.95,
        "滨水公共夜景",
        query_scope_fingerprint=scope_v8,
    )
    changed_corridor = service.search_nearby(
        "北京",
        116.46,
        39.95,
        "滨水公共夜景",
        query_scope_fingerprint=other_corridor,
    )

    assert first.cache_hit is False
    assert same_scope.cache_hit is True
    assert changed_contract.cache_hit is False
    assert changed_corridor.cache_hit is False
    assert len(calls) == 3
    assert all("query_scope_fingerprint" not in params for params in calls)


def test_query_scope_cache_discriminator_rejects_non_fingerprint_input(monkeypatch):
    clear_map_poi_runtime_state()
    service = MapPoiService(map_provider_key="test")
    monkeypatch.setattr(
        service,
        "_fetch_amap_around",
        lambda _params: pytest.fail("invalid query scope must fail before provider I/O"),
    )

    with pytest.raises(HTTPException) as exc_info:
        service.search_nearby(
            "北京",
            116.46,
            39.95,
            "滨水公共夜景",
            query_scope_fingerprint="caller-controlled-policy-bypass",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "A valid server query scope fingerprint is required"


def _district_polygon(*, min_longitude: float, min_latitude: float, max_longitude: float, max_latitude: float) -> str:
    return ";".join(
        (
            f"{min_longitude},{min_latitude}",
            f"{max_longitude},{min_latitude}",
            f"{max_longitude},{max_latitude}",
            f"{min_longitude},{max_latitude}",
            f"{min_longitude},{min_latitude}",
        )
    )


def test_city_scope_merges_duplicate_provider_representations_by_adcode(monkeypatch):
    service = MapPoiService(map_provider_key="test")
    monkeypatch.setattr(
        service,
        "_fetch_with_limit",
        lambda *_args, **_kwargs: {
            "districts": [
                {
                    "name": "测试市",
                    "adcode": "900100",
                    "level": "city",
                    "polyline": _district_polygon(
                        min_longitude=100.0,
                        min_latitude=20.0,
                        max_longitude=101.0,
                        max_latitude=21.0,
                    ),
                },
                {
                    "name": "测试",
                    "adcode": "900100",
                    "level": "city",
                    "polyline": _district_polygon(
                        min_longitude=99.0,
                        min_latitude=19.0,
                        max_longitude=100.0,
                        max_latitude=20.0,
                    ),
                },
            ]
        },
    )

    matches = service.resolve_city_scope("测试")

    assert matches == [
        {
            "name": "测试",
            "adcode": "900100",
            "level": "city",
            "queryBbox": [18.98, 98.98, 21.02, 101.02],
            "queryBboxSource": "amap_gcj02_district_bounds_padded_for_provider_lookup",
        }
    ]


def test_city_scope_prefers_exact_normalized_city_name(monkeypatch):
    service = MapPoiService(map_provider_key="test")
    monkeypatch.setattr(
        service,
        "_fetch_with_limit",
        lambda *_args, **_kwargs: {
            "districts": [
                {
                    "name": "测试市",
                    "adcode": "900100",
                    "level": "city",
                    "polyline": _district_polygon(
                        min_longitude=100.0,
                        min_latitude=20.0,
                        max_longitude=101.0,
                        max_latitude=21.0,
                    ),
                },
                {
                    "name": "测试新区",
                    "adcode": "900101",
                    "level": "district",
                    "polyline": _district_polygon(
                        min_longitude=101.0,
                        min_latitude=20.0,
                        max_longitude=102.0,
                        max_latitude=21.0,
                    ),
                },
            ]
        },
    )

    matches = service.resolve_city_scope("测试")

    assert [match["adcode"] for match in matches] == ["900100"]


def test_city_scope_keeps_distinct_exact_provider_identities_ambiguous(monkeypatch):
    service = MapPoiService(map_provider_key="test")
    monkeypatch.setattr(
        service,
        "_fetch_with_limit",
        lambda *_args, **_kwargs: {
            "districts": [
                {
                    "name": "测试市",
                    "adcode": "900100",
                    "level": "city",
                    "polyline": _district_polygon(
                        min_longitude=100.0,
                        min_latitude=20.0,
                        max_longitude=101.0,
                        max_latitude=21.0,
                    ),
                },
                {
                    "name": "测试",
                    "adcode": "900200",
                    "level": "city",
                    "polyline": _district_polygon(
                        min_longitude=102.0,
                        min_latitude=20.0,
                        max_longitude=103.0,
                        max_latitude=21.0,
                    ),
                },
            ]
        },
    )

    matches = service.resolve_city_scope("测试")

    assert [match["adcode"] for match in matches] == ["900100", "900200"]

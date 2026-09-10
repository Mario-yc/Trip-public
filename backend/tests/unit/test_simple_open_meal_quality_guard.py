from datetime import datetime, timezone

from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.models.poi import POI
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor


class _InstitutionalMealProvider:
    def search(self, city, *, keyword, category, limit):
        del limit
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="B0I2OUSEJO",
                    name="中央财经大学(沙河校区)西区食堂",
                    city="北京市",
                    district="昌平区",
                    category="餐饮服务",
                    type="餐饮服务;中餐厅;中餐厅",
                    providerTypeCode="050100",
                    address="顺沙路沙河段1号",
                    longitude=116.281,
                    latitude=40.154,
                    source="amap-place-search",
                    sourceNote="real-provider-shape",
                    confidence=0.9,
                )
            ],
        )


class _GenericMealProvider:
    def search(self, city, *, keyword, category, limit, **kwargs):
        del limit, kwargs
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="B000GENERIC1",
                    name="学府餐厅",
                    city="北京市",
                    district="海淀区",
                    category="餐饮服务",
                    type="餐饮服务;中餐厅;中餐厅",
                    providerTypeCode="050100",
                    address="学院路1号",
                    longitude=116.35,
                    latitude=39.99,
                    source="amap-place-search",
                    sourceNote="real-provider-shape",
                    confidence=0.9,
                )
            ],
        )


class _LocalCuisineRecordingProvider:
    def __init__(self) -> None:
        self.provider_types: str | None = None
        self.keyword: str | None = None

    def search_nearby(self, city, longitude, latitude, keyword, *, provider_types=None, **kwargs):
        del longitude, latitude, kwargs
        self.keyword = keyword
        self.provider_types = provider_types
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category="food",
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[
                MapPoiResponse(
                    id="B000LOCAL01",
                    name="全聚德(昌平店)",
                    city="北京市",
                    district="昌平区",
                    category="food",
                    type="餐饮服务;中餐厅;北京菜",
                    providerTypeCode="050111",
                    tags=["烤鸭", "京味"],
                    address="鼓楼南街",
                    longitude=116.22,
                    latitude=40.21,
                    source="amap-place-search",
                    sourceNote="real-provider-shape",
                    confidence=0.9,
                    providerQueriedAt=datetime.now(timezone.utc),
                    providerQueryReceiptFingerprint="f" * 64,
                )
            ],
        )


def test_simple_open_search_rejects_institutional_meal_for_local_food() -> None:
    executor = SimpleOpenItineraryExecutor(map_poi_service=_InstitutionalMealProvider())

    (
        _search,
        selected,
        _duplicate_rejection,
        semantic_rejection,
        admitted,
        baseline_admitted,
        diagnostics,
    ) = executor._search_candidate(
        city="北京",
        query="北京 当地特色餐厅",
        category="food",
        intent_type="meal",
        raw_need="当地特色美食",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
    )

    assert selected is None
    assert admitted == []
    assert baseline_admitted == []
    assert semantic_rejection is True
    assert diagnostics[0]["reasonCodes"] == ["institutional_meal"]


def test_authoritative_local_food_contract_does_not_depend_on_controller_raw_need() -> None:
    executor = SimpleOpenItineraryExecutor(map_poi_service=_GenericMealProvider())

    (
        _search,
        selected,
        _duplicate_rejection,
        semantic_rejection,
        admitted,
        baseline_admitted,
        diagnostics,
    ) = executor._search_candidate(
        city="北京",
        query="北京 餐厅",
        category="food",
        intent_type="meal",
        raw_need="午餐",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
        experience_policy={
            "localExperienceConstraint": {
                "experienceType": "local_cuisine",
                "evidencePolicy": "provider_city_specific_fact",
            }
        },
    )

    assert selected is None
    assert admitted == []
    assert baseline_admitted == []
    assert semantic_rejection is True
    assert diagnostics[0]["reasonCodes"] == ["local_food_evidence_missing"]


def test_authoritative_local_food_contract_reaches_amap_with_city_cuisine_type_filter() -> None:
    provider = _LocalCuisineRecordingProvider()
    executor = SimpleOpenItineraryExecutor(map_poi_service=provider)
    anchor = POI(
        id="poi_anchor",
        amap_id="B000CAMPUS1",
        name="北京大学(昌平校区)",
        city="北京",
        category="campus",
        latitude=40.247449,
        longitude=116.189912,
        source="amap-place-search",
        confidence=1.0,
        type="科教文化服务;学校;高等院校",
        provider_type_code="141201",
    )

    (
        _search,
        selected,
        _duplicate_rejection,
        semantic_rejection,
        admitted,
        _baseline_admitted,
        diagnostics,
    ) = executor._search_candidate(
        city="北京",
        query="北京菜",
        category="food",
        intent_type="meal",
        raw_need="当地特色美食",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
        experience_policy={
            "localExperienceConstraint": {
                "experienceType": "local_cuisine",
                "evidencePolicy": "provider_city_specific_fact",
                "locality": {"city": "北京", "source": "request_destination"},
            }
        },
        nearby_anchor=anchor,
        nearby_radius=5000,
    )

    assert provider.provider_types == "北京菜"
    assert selected is not None
    assert selected.amap_id == "B000LOCAL01"
    assert selected.provider_type_code == "050111"
    assert admitted == []
    assert semantic_rejection is False
    assert diagnostics == []


def test_specific_meal_query_keeps_keyword_and_carries_provider_type_separately() -> None:
    provider = _LocalCuisineRecordingProvider()
    executor = SimpleOpenItineraryExecutor(map_poi_service=provider)
    anchor = POI(
        id="poi_anchor",
        amap_id="B000CAMPUS1",
        name="北京大学(昌平校区)",
        city="北京",
        category="campus",
        latitude=40.247449,
        longitude=116.189912,
        source="amap-place-search",
        confidence=1.0,
        type="科教文化服务;学校;高等院校",
        provider_type_code="141201",
    )

    result = executor._search_candidate(
        city="北京",
        query="便宜坊烤鸭店",
        category="food",
        intent_type="meal",
        raw_need="北京菜 烤鸭",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
        experience_policy={
            "localExperienceConstraint": {
                "experienceType": "local_cuisine",
                "evidencePolicy": "provider_city_specific_fact",
                "locality": {"city": "北京", "source": "request_destination"},
            }
        },
        nearby_anchor=anchor,
        nearby_radius=5000,
    )

    assert provider.keyword == "便宜坊烤鸭店"
    assert provider.provider_types == "北京菜"
    assert result[1] is not None
    assert result[1].provider_type_code == "050111"

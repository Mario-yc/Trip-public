from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace

from src.providers.travel_tools import WebSearchItem, WebSearchResponse
from src.models.poi_search_profile import ExperienceSemanticInput
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
)
from src.services.poi_discovery_service import PoiDiscoveryService


class FakeWebProvider:
    def __init__(self):
        self.calls = []

    def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
        self.calls.append((query, count, freshness))
        return WebSearchResponse(
            query=query,
            provider_name="recorded-web",
            confidence=0.9,
            provider_diagnostics=[{"providerName": "recorded-web", "status": "success"}],
            results=[
                WebSearchItem(
                    title="四季民福烤鸭店（故宫店）- 官方介绍",
                    url="https://example.gov.cn/food/siji",
                    snippet="北京特色烤鸭餐厅。",
                    source_name="官方文旅",
                    provider_name="recorded-web",
                    confidence=0.9,
                    credibility_rank="official",
                )
            ],
        )


class FakeMapService:
    def __init__(self):
        self.calls = []

    def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
        self.calls.append((city, keyword, category, limit))
        return SimpleNamespace(
            provider_name="amap-place-search",
            pois=[
                SimpleNamespace(
                    id="B0AMAPFOOD",
                    amap_id="B0AMAPFOOD",
                    name="四季民福烤鸭店(故宫店)",
                    city="北京",
                    category="food",
                    type="中餐厅",
                    longitude=116.397,
                    latitude=39.916,
                    source="amap-place-search",
                    confidence=0.96,
                    model_dump=lambda by_alias=True: {
                        "id": "B0AMAPFOOD",
                        "amapId": "B0AMAPFOOD",
                        "name": "四季民福烤鸭店(故宫店)",
                        "city": "北京",
                        "category": "food",
                        "type": "中餐厅",
                        "providerType": "中餐厅",
                        "longitude": 116.397,
                        "latitude": 39.916,
                        "source": "amap-place-search",
                        "confidence": 0.96,
                    },
                )
            ],
        )


def test_web_discovers_seed_but_only_amap_grounded_candidate_is_returned():
    web = FakeWebProvider()
    amap = FakeMapService()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=3,
    ).discover(
        city="北京",
        intent_type="meal",
        raw_need="故宫附近北京特色午餐",
        trigger_reason="amap_candidate_shortage",
    )

    assert result.status == "grounded"
    assert result.web_query_count == 1
    assert result.amap_query_count == 1
    assert result.web_only_final_poi_count == 0
    assert result.fake_coordinate_count == 0
    assert [item["amapId"] for item in result.candidates] == ["B0AMAPFOOD"]
    candidate = result.candidates[0]
    assert candidate["source"] == "amap-place-search"
    assert candidate["providerType"] == "中餐厅"
    assert candidate["candidateSource"] == "web_seed_amap_grounded"
    assert candidate["discoveryProvenance"]["webProvider"] == "recorded-web"
    assert candidate["discoveryProvenance"]["mapProvider"] == "amap-place-search"
    assert candidate["discoveryProvenance"]["triggerReason"] == "amap_candidate_shortage"
    assert candidate["longitude"] == 116.397
    assert candidate["latitude"] == 39.916


def test_amap_detail_identity_mismatch_cannot_rebind_web_claims_to_another_place():
    class MismatchedDetailMap(FakeMapService):
        def detail(self, amap_id):
            assert amap_id == "B0AMAPFOOD"
            payload = {
                "id": "B0OTHERPOI",
                "amapId": "B0OTHERPOI",
                "name": "无关餐厅",
                "city": "北京",
                "providerType": "餐饮服务;中餐厅",
                "type": "餐饮服务;中餐厅",
                "longitude": 116.5,
                "latitude": 39.8,
                "source": "amap-place-search",
            }
            return SimpleNamespace(
                model_dump=lambda by_alias=True: dict(payload),
            )

    result = PoiDiscoveryService(
        web_search_provider=FakeWebProvider(),
        map_poi_service=MismatchedDetailMap(),
        max_web_queries=1,
        max_amap_seed_queries=1,
    ).discover(
        city="北京",
        intent_type="meal",
        raw_need="北京特色午餐",
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    candidate = result.candidates[0]
    assert candidate["amapId"] == "B0AMAPFOOD"
    assert candidate["name"] == "四季民福烤鸭店(故宫店)"
    assert candidate["discoveryProvenance"]["amapId"] == "B0AMAPFOOD"
    assert any(item.get("reason") == "amap_detail_identity_mismatch" for item in result.provider_diagnostics)


def test_amap_detail_conflicting_identity_aliases_are_rejected():
    class ConflictingAliasDetailMap(FakeMapService):
        def detail(self, amap_id):
            assert amap_id == "B0AMAPFOOD"
            payload = {
                "id": "B0OTHERPOI",
                "amapId": "B0AMAPFOOD",
                "name": "被矛盾别名污染的餐厅",
                "city": "北京",
                "providerType": "餐饮服务;中餐厅",
                "type": "餐饮服务;中餐厅",
                "longitude": 116.5,
                "latitude": 39.8,
                "source": "amap-place-search",
            }
            return SimpleNamespace(
                model_dump=lambda by_alias=True: dict(payload),
            )

    result = PoiDiscoveryService(
        web_search_provider=FakeWebProvider(),
        map_poi_service=ConflictingAliasDetailMap(),
        max_web_queries=1,
        max_amap_seed_queries=1,
    ).discover(
        city="北京",
        intent_type="meal",
        raw_need="北京特色午餐",
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    candidate = result.candidates[0]
    assert candidate["amapId"] == "B0AMAPFOOD"
    assert candidate["id"] == "B0AMAPFOOD"
    assert candidate["name"] == "四季民福烤鸭店(故宫店)"
    assert candidate["sourceClaims"][0]["claimKey"] == "local_food"
    assert "北京特色烤鸭餐厅" in candidate["sourceClaims"][0]["summary"]
    assert any(item.get("reason") == "amap_detail_identity_mismatch" for item in result.provider_diagnostics)


def test_generic_night_gap_queries_unselected_concrete_hint_and_binds_web_evidence():
    class NightWeb(FakeWebProvider):
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="奥林匹克塔夜景观赏指南 - 北京文旅",
                        url="https://example.gov.cn/night/olympic-tower",
                        snippet="奥林匹克塔是北京夜间观景和城市天际线拍摄地点。",
                        source_name="北京文旅",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                ],
            )

    class NightMap(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            payload = {
                "id": "B0NIGHT",
                "amapId": "B0NIGHT",
                "name": "奥林匹克塔",
                "city": "北京",
                "providerType": "风景名胜;旅游景点",
                "type": "风景名胜;旅游景点",
                "longitude": 116.3946,
                "latitude": 40.0086,
                "source": "amap-place-search",
                "confidence": 0.96,
            }
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[SimpleNamespace(**payload, model_dump=lambda by_alias=True: dict(payload))],
            )

    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-night",
            briefId="brief-night",
            planningSlotId="day-2-night",
            requirementLevel="required",
            goalId="goal-night",
            rawNeed="晚上看城市夜景",
            intentType="night_view",
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )
    web, amap = NightWeb(), NightMap()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=2,
    ).discover(
        search_profile=profile.model_dump(by_alias=True),
        candidate_hints=["中央广播电视塔", "奥林匹克塔"],
        excluded_candidate_names=["中央广播电视塔"],
        query_variant_index=0,
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    assert "奥林匹克塔" in web.calls[0][0]
    assert "晚上看城市夜景" not in web.calls[0][0]
    assert amap.calls[0][1] == "奥林匹克塔"
    assert amap.calls[0][2] == "all"
    candidate = result.candidates[0]
    assert "matchedCandidateHint:奥林匹克塔" in candidate["sourceNote"]
    assert "matchedCandidateHintBinding:discovery_query" in candidate["sourceNote"]


def test_generic_night_family_discovers_named_entity_before_exact_amap_grounding():
    """A semantic family is a query seed, never the final POI identity."""

    class PublicNightWeb(FakeWebProvider):
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="景山公园夜景观赏指南",
                        url="https://example.gov.cn/night/jingshan",
                        snippet="景山公园可俯瞰北京城市天际线，是公共户外夜景观赏地点。",
                        source_name="北京文旅",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                ],
            )

    class PublicNightMap(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            assert keyword == "景山公园"
            payload = {
                "id": "B0JINGSHAN",
                "amapId": "B0JINGSHAN",
                "name": "景山公园",
                "city": "北京",
                "category": "scenic",
                "providerType": "风景名胜;公园广场;城市公园",
                "type": "风景名胜;公园广场;城市公园",
                "longitude": 116.3966,
                "latitude": 39.9251,
                "source": "amap-place-search",
                "confidence": 0.97,
            }
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[SimpleNamespace(**payload, model_dump=lambda by_alias=True: dict(payload))],
            )

    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-public-night",
            briefId="brief-public-night",
            planningSlotId="day-1-public-night",
            requirementLevel="required",
            goalId="goal-night",
            rawNeed="每晚安排不同的公共城市夜景",
            intentType="night_view",
            candidateHints=["公共城市夜景空间"],
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )
    assert profile.queryPlans[-1].mode == "web_seed_then_amap"

    web, amap = PublicNightWeb(), PublicNightMap()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=2,
    ).discover(
        search_profile=profile.model_dump(by_alias=True),
        candidate_hints=["公共城市夜景空间"],
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    assert amap.calls[0][1] == "景山公园"
    assert result.candidates[0]["name"] == "景山公园"
    assert result.candidates[0]["source"] == "amap-place-search"
    assert result.candidates[0]["candidateSource"] == "web_seed_amap_grounded"


def test_meal_candidate_name_is_web_verified_then_rebound_to_exact_amap_place():
    class MealWeb(FakeWebProvider):
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="四季民福烤鸭店(故宫店) - 北京餐饮指南",
                        url="https://example.gov.cn/food/sijiminfu",
                        snippet="四季民福烤鸭店的招牌菜是北京烤鸭，提供堂食与预约服务。",
                        source_name="北京餐饮指南",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                ],
            )

    class MealMap(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            payload = {
                "id": "B0MEAL01",
                "amapId": "B0MEAL01",
                "name": "四季民福烤鸭店(故宫店)",
                "city": "北京",
                "providerType": "餐饮服务;中餐厅;特色/地方风味餐厅",
                "type": "餐饮服务;中餐厅;特色/地方风味餐厅",
                "longitude": 116.405,
                "latitude": 39.918,
                "source": "amap-place-search",
                "confidence": 0.96,
            }
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[SimpleNamespace(**payload, model_dump=lambda by_alias=True: dict(payload))],
            )

    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-meal",
            briefId="brief-meal",
            planningSlotId="day-1-meal",
            requirementLevel="soft",
            softGoalId="goal-local-food",
            rawNeed="当地特色美食",
            intentType="meal",
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )
    web, amap = MealWeb(), MealMap()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=1,
    ).discover(
        search_profile=profile.model_dump(by_alias=True),
        evidence_candidate_names=["四季民福烤鸭店(故宫店)"],
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    assert "四季民福烤鸭店" in web.calls[0][0]
    assert amap.calls[0][1] == "四季民福烤鸭店(故宫店)"
    assert amap.calls[0][2] == "all"
    candidate = result.candidates[0]
    assert any(claim["claimKey"] == "local_food" for claim in candidate["sourceClaims"])
    assert "matchedCandidateHint:四季民福烤鸭店(故宫店)" in candidate["sourceNote"]


def test_named_historic_area_is_web_verified_then_rebound_to_exact_amap_place():
    class AreaWeb(FakeWebProvider):
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="模式口历史文化街区 - 北京文旅介绍",
                        url="https://example.gov.cn/culture/moshikou",
                        snippet="模式口历史文化街区保留老街与传统建筑，适合步行了解历史风貌。",
                        source_name="北京文旅",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                ],
            )

    class AreaMap(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            payload = {
                "id": "B0K6VHYJ41",
                "amapId": "B0K6VHYJ41",
                "name": "模式口历史文化街区",
                "city": "北京",
                "providerType": "风景名胜;风景名胜相关;旅游景点",
                "providerTypeCode": "110200",
                "type": "风景名胜;风景名胜相关;旅游景点",
                "longitude": 116.164,
                "latitude": 39.939,
                "source": "amap-place-search",
                "confidence": 0.96,
            }
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[
                    SimpleNamespace(
                        **payload,
                        model_dump=lambda by_alias=True: dict(payload),
                    )
                ],
            )

    web, amap = AreaWeb(), AreaMap()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=1,
    ).discover(
        city="北京",
        intent_type="area_walk",
        raw_need="历史街区与胡同漫步",
        evidence_candidate_names=["模式口历史文化街区"],
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    assert "模式口历史文化街区" in web.calls[0][0]
    assert amap.calls[0][1] == "模式口历史文化街区"
    candidate = result.candidates[0]
    assert candidate["amapId"] == "B0K6VHYJ41"
    assert any(
        claim["claimKey"] == "heritage_walk" and claim["stance"] == "support" for claim in candidate["sourceClaims"]
    )
    assert "matchedCandidateHint:模式口历史文化街区" in candidate["sourceNote"]


def test_named_area_evidence_can_seed_a_distinct_relevant_entity_without_claim_misattribution():
    class AlternativeAreaWeb(FakeWebProvider):
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="东四胡同历史文化街区 - 北京文旅介绍",
                        url="https://example.gov.cn/culture/dongsi",
                        snippet="东四胡同保留传统建筑和老街格局，适合步行了解历史风貌。",
                        source_name="北京文旅",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                ],
            )

    class AlternativeAreaMap(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            payload = {
                "id": "B0DONGSI01",
                "amapId": "B0DONGSI01",
                "name": "东四胡同历史文化街区",
                "city": "北京",
                "providerType": "风景名胜;风景名胜相关;旅游景点",
                "providerTypeCode": "110200",
                "type": "风景名胜;风景名胜相关;旅游景点",
                "longitude": 116.423,
                "latitude": 39.931,
                "source": "amap-place-search",
                "confidence": 0.96,
            }
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[
                    SimpleNamespace(
                        **payload,
                        model_dump=lambda by_alias=True: dict(payload),
                    )
                ],
            )

    web, amap = AlternativeAreaWeb(), AlternativeAreaMap()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=1,
    ).discover(
        city="北京",
        intent_type="area_walk",
        raw_need="历史街区与胡同漫步",
        evidence_candidate_names=["南锣鼓巷历史文化街区"],
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    assert "南锣鼓巷历史文化街区" in web.calls[0][0]
    assert amap.calls[0][1] == "东四胡同历史文化街区"
    candidate = result.candidates[0]
    assert candidate["amapId"] == "B0DONGSI01"
    assert candidate["name"] == "东四胡同历史文化街区"
    assert "matchedCandidateHint" not in str(candidate.get("sourceNote") or "")
    assert candidate["discoveryProvenance"]["entitySeed"] == "东四胡同历史文化街区"
    assert any(
        claim["claimKey"] == "heritage_walk" and claim["stance"] == "support" for claim in candidate["sourceClaims"]
    )


def test_old_web_page_is_only_an_entity_seed_and_is_rebound_to_amap():
    class OldEntityWeb(FakeWebProvider):
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="景山公园夜景介绍",
                        url="https://example.com/2020/jingshan-night",
                        snippet="2020年介绍了景山公园夜间开放和城市夜景。",
                        source_name="历史页面",
                        provider_name="recorded-web",
                        credibility_rank="guide",
                        published_at="2020-01-01",
                    )
                ],
            )

    class OldEntityMap(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            payload = {
                "id": "B0JINGSHAN1",
                "amapId": "B0JINGSHAN1",
                "name": "景山公园",
                "city": "北京",
                "providerType": "风景名胜;公园广场;公园",
                "type": "风景名胜;公园广场;公园",
                "longitude": 116.396,
                "latitude": 39.925,
                "source": "amap-place-search",
                "confidence": 0.96,
            }
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[SimpleNamespace(**payload, model_dump=lambda by_alias=True: dict(payload))],
            )

    web, amap = OldEntityWeb(), OldEntityMap()
    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=1,
    ).discover(
        city="北京",
        intent_type="night_view",
        raw_need="晚上看城市夜景",
        trigger_reason="entity_discovery",
    )

    assert result.status == "grounded"
    assert web.calls[0][2] == "noLimit"
    assert amap.calls[0][1] == "景山公园"
    assert result.candidates[0]["source"] == "amap-place-search"
    assert result.candidates[0]["sourceClaims"] == []


def test_profile_drives_web_seed_and_family_specific_amap_grounding_plan():
    class MarketWebProvider:
        def __init__(self):
            self.calls = []

        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            self.calls.append((query, count, freshness))
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title="三源里菜市场 - 官方介绍",
                        url="https://example.gov.cn/market/sanyuanli",
                        snippet="北京社区市场。",
                        source_name="官方文旅",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                ],
            )

    class MarketMapService:
        def __init__(self):
            self.calls = []

        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            return SimpleNamespace(
                provider_name="amap-place-search",
                pois=[
                    SimpleNamespace(
                        id="B0MARKET",
                        amap_id="B0MARKET",
                        name="三源里菜市场",
                        city="北京",
                        category="market",
                        type="购物服务;综合市场;农副产品市场",
                        longitude=116.47,
                        latitude=39.96,
                        source="amap-place-search",
                        confidence=0.96,
                        model_dump=lambda by_alias=True: {
                            "id": "B0MARKET",
                            "amapId": "B0MARKET",
                            "name": "三源里菜市场",
                            "city": "北京",
                            "category": "market",
                            "type": "购物服务;综合市场;农副产品市场",
                            "providerType": "购物服务;综合市场;农副产品市场",
                            "longitude": 116.47,
                            "latitude": 39.96,
                            "source": "amap-place-search",
                            "confidence": 0.96,
                        },
                    )
                ],
            )

    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-local",
            briefId="brief-local",
            planningSlotId="day-1-local",
            requirementLevel="optional",
            rawNeed="社区生活和市场漫步",
            intentType="area_walk",
            optionalExperienceFamily="local_life",
            rejectedTypes=["住宅", "公司", "停车场"],
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )
    web = MarketWebProvider()
    amap = MarketMapService()

    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=2,
    ).discover(
        search_profile=profile.model_dump(by_alias=True),
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.status == "grounded"
    assert "社区" in web.calls[0][0] or "本地生活" in web.calls[0][0]
    assert amap.calls[0][2] in {"local_service", "market", "shopping", "food"}
    assert amap.calls[0][2] != "scenic"
    candidate = result.candidates[0]
    assert candidate["source"] == "amap-place-search"
    assert candidate["searchProfileId"] == profile.profileId
    assert candidate["searchProfileFingerprint"] == profile.profileFingerprint
    assert candidate["experienceFamily"] == "local_life"
    assert candidate["queryPlanId"]
    assert result.discovery_evidence[0]["profileFingerprint"] == (profile.profileFingerprint)


def test_web_only_entity_is_never_returned_as_final_poi():
    class EmptyMap(FakeMapService):
        def search(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(provider_name="amap-place-search", pois=[])

    result = PoiDiscoveryService(
        web_search_provider=FakeWebProvider(),
        map_poi_service=EmptyMap(),
    ).discover(
        city="北京",
        intent_type="meal",
        raw_need="特色午餐",
        trigger_reason="route_repair",
    )

    assert result.status == "unresolved"
    assert result.candidates == []
    assert result.web_only_final_poi_count == 0
    assert result.fake_coordinate_count == 0


def test_discovery_evidence_traces_each_web_seed_to_amap_without_sensitive_payloads():
    class EvidenceWebProvider:
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                confidence=0.9,
                failure_reason=None,
                provider_diagnostics=[
                    {
                        "providerName": "recorded-web",
                        "status": "success",
                        "reason": "ok",
                        "durationMs": 12,
                        "resultCount": 2,
                        "transportRoute": "direct",
                        "proxyConfigured": False,
                        "timeoutSeconds": 4.5,
                        "headers": {"Authorization": "Bearer trace-secret"},
                        "proxyUrl": "http://proxy-secret@127.0.0.1:8899",
                        "requestUrl": "https://search.example.invalid/?q=PRIVATE",
                        "prompt": "PRIVATE PROMPT MUST NOT EXPORT",
                        "reasoning": "PRIVATE REASONING MUST NOT EXPORT",
                    }
                ],
                results=[
                    WebSearchItem(
                        title="四季民福烤鸭店（故宫店）- 官方介绍",
                        url="https://example.invalid/food/siji",
                        snippet="PRIVATE SNIPPET MUST NOT EXPORT",
                        source_name="官方文旅",
                        provider_name="recorded-web",
                        confidence=0.9,
                        credibility_rank="official",
                    ),
                    WebSearchItem(
                        title="未落地餐厅 - 官方介绍",
                        url="https://example.invalid/food/unresolved",
                        snippet="C:\\Users\\Thinkpad\\private.txt",
                        source_name="官方文旅",
                        provider_name="recorded-web",
                        confidence=0.8,
                        credibility_rank="official",
                    ),
                ],
            )

    class EvidenceMapService(FakeMapService):
        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            if keyword == "未落地餐厅":
                return SimpleNamespace(
                    provider_name="amap-place-search",
                    pois=[],
                )
            return super().search(city, keyword, category=category, limit=limit)

    result = PoiDiscoveryService(
        web_search_provider=EvidenceWebProvider(),
        map_poi_service=EvidenceMapService(),
        max_web_queries=1,
        max_amap_seed_queries=2,
    ).discover(
        city="北京",
        intent_type="meal",
        raw_need="故宫附近北京特色午餐",
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert len(result.discovery_evidence) == 1
    attempt = result.discovery_evidence[0]
    expected_query = "北京 故宫附近北京特色午餐 官方 地点"
    assert attempt["queryFingerprint"] == sha256(expected_query.encode("utf-8")).hexdigest()
    assert "query" not in attempt
    assert attempt["providerName"] == "recorded-web"
    assert attempt["providerStatus"] == "success"
    assert attempt["status"] == "grounded"
    assert attempt["resultCount"] == 2
    assert isinstance(attempt["durationMs"], (int, float))
    assert isinstance(attempt["webDurationMs"], (int, float))
    assert isinstance(attempt["amapGroundingMs"], (int, float))
    assert attempt["webDurationMs"] >= 0
    assert attempt["amapGroundingMs"] >= 0
    assert attempt["durationMs"] == round(attempt["webDurationMs"] + attempt["amapGroundingMs"], 3)
    assert attempt["seedCount"] == 2
    assert attempt["providerAttempts"] == [
        {
            "providerName": "recorded-web",
            "status": "success",
            "reasonCode": "ok",
            "durationMs": 12,
            "resultCount": 2,
            "transportRoute": "direct",
            "proxyConfigured": False,
            "timeoutSeconds": 4.5,
        }
    ]
    assert "proxy-secret" not in str(attempt)
    assert "PRIVATE" not in str(attempt)
    assert "proxy-secret" not in str(result.provider_diagnostics)
    assert "PRIVATE" not in str(result.provider_diagnostics)
    assert [item["seedName"] for item in attempt["seedGroundings"]] == [
        "四季民福烤鸭店（故宫店）",
        "未落地餐厅",
    ]
    grounded, unresolved = attempt["seedGroundings"]
    assert grounded["providerName"] == "amap-place-search"
    assert grounded["status"] == "grounded"
    assert grounded["candidateCount"] == 1
    assert isinstance(grounded["durationMs"], (int, float))
    assert grounded["selectedCandidates"] == [{"amapId": "B0AMAPFOOD", "name": "四季民福烤鸭店(故宫店)"}]
    assert unresolved["providerName"] == "amap-place-search"
    assert unresolved["status"] == "unresolved"
    assert unresolved["reasonCode"] == "amap_seed_no_exact_match"
    assert unresolved["candidateCount"] == 0
    assert unresolved["selectedCandidates"] == []

    exported_text = json.dumps(result.discovery_evidence, ensure_ascii=False)
    for forbidden in (
        "https://",
        "PRIVATE SNIPPET",
        "Authorization",
        "Bearer trace-secret",
        "PRIVATE PROMPT",
        "PRIVATE REASONING",
        "C:\\Users\\Thinkpad",
        "headers",
        "snippet",
        "prompt",
        "reasoning",
        "url",
    ):
        assert forbidden not in exported_text


def test_discovery_evidence_limits_seed_records_to_the_configured_requirement_budget():
    class ManySeedWebProvider:
        def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
            return WebSearchResponse(
                query=query,
                provider_name="recorded-web",
                results=[
                    WebSearchItem(
                        title=f"候选地点{index} - 官方介绍",
                        url=f"https://example.invalid/place/{index}",
                        snippet="not exported",
                        source_name="官方文旅",
                        provider_name="recorded-web",
                        credibility_rank="official",
                    )
                    for index in range(6)
                ],
            )

    class EmptyMap:
        def __init__(self):
            self.calls = []

        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((city, keyword, category, limit))
            return SimpleNamespace(provider_name="amap-place-search", pois=[])

    amap = EmptyMap()
    result = PoiDiscoveryService(
        web_search_provider=ManySeedWebProvider(),
        map_poi_service=amap,
        max_web_queries=1,
        max_amap_seed_queries=2,
    ).discover(
        city="北京",
        intent_type="area_walk",
        raw_need="文化街区",
        trigger_reason="portfolio_always_on_candidate_discovery",
    )

    assert result.discovery_evidence[0]["seedCount"] == 6
    assert result.discovery_evidence[0]["seedRecordsTruncated"] is True
    assert len(result.discovery_evidence[0]["seedGroundings"]) == 2
    assert len(amap.calls) == 2


def test_amap_grounding_rejects_missing_original_source():
    candidate = {
        "id": "B0SPOOF",
        "amapId": "B0SPOOF",
        "name": "什刹海",
        "city": "北京",
        "providerType": "风景名胜",
        "longitude": 116.38,
        "latitude": 39.94,
    }

    assert PoiDiscoveryService._best_exact_amap_match("什刹海", [candidate], city="北京") is None


def test_amap_grounding_rejects_spoofed_amap_prefix_source():
    candidate = {
        "id": "B0SPOOF",
        "amapId": "B0SPOOF",
        "name": "什刹海",
        "city": "北京",
        "providerType": "风景名胜",
        "longitude": 116.38,
        "latitude": 39.94,
        "source": "amap-fake",
    }

    assert PoiDiscoveryService._best_exact_amap_match("什刹海", [candidate], city="北京") is None


def test_amap_grounding_rejects_name_substring_substitution():
    candidate = {
        "id": "B0SHADOW",
        "amapId": "B0SHADOW",
        "name": "北京什刹海皮影戏",
        "city": "北京",
        "providerType": "剧院",
        "longitude": 116.38,
        "latitude": 39.94,
        "source": "amap-place-search",
    }

    assert PoiDiscoveryService._best_exact_amap_match("什刹海", [candidate], city="北京") is None


def test_missing_legacy_semantic_input_is_zero_call_unresolved():
    web = FakeWebProvider()
    map_service = FakeMapService()

    result = PoiDiscoveryService(
        web_search_provider=web,
        map_poi_service=map_service,
    ).discover(
        city="",
        intent_type="",
        raw_need="",
        trigger_reason="legacy_incomplete_report",
    )

    assert result.status == "unresolved"
    assert result.failure_reason == "poi_discovery_missing_semantic_input"
    assert result.web_query_count == 0
    assert result.amap_query_count == 0
    assert web.calls == []
    assert map_service.calls == []


def test_unsupported_creative_profile_is_zero_provider_call_unresolved():
    for family, intent_type, expected_reason in (
        (
            "future_unknown_family",
            "area_walk",
            "creative_optional_family_unregistered",
        ),
        (
            "art_walk",
            "future_intent",
            "creative_optional_intent_unregistered",
        ),
    ):
        profile = ExperienceSearchProfileCompiler().compile(
            ExperienceSemanticInput(
                city="北京",
                poolId=f"pool-{family}-{intent_type}",
                briefId="brief-unsupported",
                planningSlotId="slot-unsupported",
                requirementLevel="optional",
                rawNeed="未知创意体验",
                intentType=intent_type,
                optionalExperienceFamily=family,
                targetCount=1,
                evidenceTargetCount=1,
                maxQueries=4,
            )
        )
        web = FakeWebProvider()
        amap = FakeMapService()

        result = PoiDiscoveryService(
            web_search_provider=web,
            map_poi_service=amap,
        ).discover(
            search_profile=profile,
            trigger_reason="portfolio_always_on_candidate_discovery",
        )

        assert result.status == "unresolved"
        assert result.failure_reason == expected_reason
        assert result.web_query_count == 0
        assert result.amap_query_count == 0
        assert web.calls == []
        assert amap.calls == []
        assert result.discovery_evidence[0]["providerStatus"] == "skipped"
        assert result.discovery_evidence[0]["reasonCode"] == expected_reason

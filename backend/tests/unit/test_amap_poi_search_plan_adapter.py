from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

import pytest

from src.models.poi_search_profile import (
    PoiSearchProfile,
    SearchBudgetPolicy,
    SearchCoveragePolicy,
    SearchFallbackPolicy,
    SearchQueryPlan,
    SearchScoringPolicy,
    SearchSourceEvidence,
)
from src.services.amap_poi_search_plan_adapter import (
    AmapPoiSearchPlanAdapter,
)
from src.services.map_poi_service import MapPoiService, clear_map_poi_runtime_state


def _profile(
    experience_family: str,
    provider_category_keys: Iterable[str],
    *,
    intent_type: str = "area_walk",
    max_queries: int = 8,
    max_amap_calls: Optional[int] = None,
    query_plans: Optional[list[SearchQueryPlan]] = None,
) -> PoiSearchProfile:
    categories = list(provider_category_keys)
    plans = query_plans or [
        SearchQueryPlan(
            planId=f"query_{experience_family}",
            priority=100,
            mode="amap_text",
            keyword="主题体验",
            keywordVariants=["主题体验", "第二关键词"],
            providerCategoryKeys=categories,
            preferredTypeGroups=[],
            rejectedTypeGroups=[],
            anchorPolicy="none",
            radiusMeters=1500,
            resultLimit=12,
            fallbackLevel=0,
            requiresAmapGrounding=True,
            stopWhenTargetReached=True,
        )
    ]
    return PoiSearchProfile(
        profileId=f"profile_{experience_family}",
        profileFingerprint="a" * 64,
        city="北京",
        poolId="pool-1",
        briefId="brief-1",
        planningSlotId="slot-1",
        experienceFamily=experience_family,
        activityMode="walk",
        intentType=intent_type,
        requirementLevel="optional",
        entityBindingMode="category",
        exactEntity=None,
        semanticFacets=[],
        keywordVariants=["主题体验", "第二关键词"],
        preferredPlaceFacets=[],
        rejectedPlaceFacets=[],
        queryPlans=plans,
        scoringPolicy=SearchScoringPolicy(),
        fallbackPolicy=SearchFallbackPolicy(),
        coveragePolicy=SearchCoveragePolicy(
            targetCount=1,
            evidenceTargetCount=1,
        ),
        budgetPolicy=SearchBudgetPolicy(
            maxQueries=max_queries,
            maxAmapCalls=max_amap_calls or max_queries,
            maxWebSeedQueries=0,
            resultLimit=12,
        ),
        excludedPhysicalPoiIds=[],
        sourceEvidence=SearchSourceEvidence(
            rawNeed="主题体验",
            familySource="optional_experience_family",
        ),
        exclusionFingerprint="b" * 64,
        executionFingerprint="c" * 64,
    )


def _categories(profile: PoiSearchProfile) -> list[str]:
    return [
        plan.category
        for plan in AmapPoiSearchPlanAdapter().adapt(profile)
    ]


def test_adapter_expands_one_profile_into_multiple_bounded_provider_plans() -> None:
    profile = _profile(
        "market",
        ["market", "pedestrian_street"],
        max_queries=8,
        max_amap_calls=3,
        query_plans=[
            SearchQueryPlan(
                planId="query_text",
                priority=100,
                mode="amap_text",
                keyword="传统市场",
                keywordVariants=["传统市场", "菜市场"],
                providerCategoryKeys=["market", "pedestrian_street"],
                anchorPolicy="none",
                radiusMeters=1500,
                resultLimit=20,
            ),
            SearchQueryPlan(
                planId="query_around",
                priority=90,
                mode="amap_around",
                keyword="传统市场",
                keywordVariants=["传统市场", "菜市场"],
                providerCategoryKeys=["market", "pedestrian_street"],
                anchorPolicy="previous_only",
                radiusMeters=5000,
                resultLimit=25,
            ),
        ],
    )

    plans = AmapPoiSearchPlanAdapter().adapt(profile.model_dump(by_alias=True))

    assert len(plans) == 3
    assert len({plan.planId for plan in plans}) == 3
    assert {plan.endpoint for plan in plans} == {"place/text", "place/around"}
    assert {plan.category for plan in plans} == {"market", "shopping", "food"}
    assert all(plan.radiusMeters <= 5000 for plan in plans)
    assert all(plan.resultLimit <= 25 for plan in plans)
    assert [plan.priority for plan in plans] == sorted(
        (plan.priority for plan in plans),
        reverse=True,
    )


@pytest.mark.parametrize(
    ("family", "provider_keys", "expected_categories"),
    [
        (
            "heritage",
            ["heritage_district", "pedestrian_street"],
            {"culture", "scenic", "shopping"},
        ),
        (
            "local_life",
            ["neighborhood", "community_market"],
            {"local_service", "market", "shopping", "food"},
        ),
        (
            "market",
            ["market", "pedestrian_street"],
            {"market", "shopping", "food", "culture"},
        ),
        (
            "art",
            ["art_district", "cultural_venue"],
            {"culture", "experience", "museum"},
        ),
        (
            "park",
            ["park", "green_space"],
            {"park", "scenic"},
        ),
    ],
)
def test_family_category_combinations_are_distinct(
    family: str,
    provider_keys: list[str],
    expected_categories: set[str],
) -> None:
    categories = set(_categories(_profile(family, provider_keys)))

    assert categories == expected_categories


def test_market_expands_across_market_shopping_and_food() -> None:
    categories = set(
        _categories(_profile("market", ["market", "pedestrian_street"]))
    )

    assert {"market", "shopping", "food"} <= categories


def test_art_search_is_not_collapsed_to_museum_only() -> None:
    categories = set(
        _categories(_profile("art", ["art_district", "cultural_venue"]))
    )

    assert "museum" in categories
    assert {"culture", "experience"} <= categories
    assert categories != {"museum"}


def test_unknown_family_does_not_silently_become_scenic() -> None:
    categories = _categories(
        _profile("unknown", ["all"], intent_type="area_walk")
    )

    assert categories == ["all"]
    assert "scenic" not in categories


def test_existing_search_signature_and_request_parameters_remain_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_fetch(_self: MapPoiService, params: dict[str, str]) -> dict:
        captured.update(params)
        return {"status": "1", "pois": []}

    clear_map_poi_runtime_state()
    monkeypatch.setattr(MapPoiService, "_fetch_amap_place", fake_fetch)

    response = MapPoiService(map_provider_key="test-amap-key").search(
        "北京",
        "故宫",
        "scenic",
        99,
        True,
    )

    assert response.city == "北京"
    assert response.keyword == "故宫"
    assert response.category == "scenic"
    assert captured["city"] == "110000"
    assert captured["types"] == "110000"
    assert captured["offset"] == "25"

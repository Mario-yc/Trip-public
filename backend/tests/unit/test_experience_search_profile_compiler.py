from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from src.models.poi_search_profile import ExperienceSemanticInput
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
    search_profile_provider_rejection_reason,
)


def _semantic_input(**overrides: object) -> ExperienceSemanticInput:
    values: dict[str, object] = {
        "city": "北京",
        "poolId": "pool-local",
        "briefId": "brief-local",
        "planningSlotId": "slot-local",
        "requirementLevel": "optional",
        "goalId": None,
        "softGoalId": None,
        "rawNeed": "体验北京本地街区",
        "intentType": "area_walk",
        "optionalExperienceFamily": "local_life",
        "assignedMealFamily": None,
        "preferredTypes": [],
        "rejectedTypes": ["酒店", "停车场"],
        "candidateHints": [],
        "hintPolicy": "no_hint",
        "routePreference": {"preferNearAdjacentAnchors": True},
        "entityBindingMode": "category",
        "exactEntity": None,
        "semanticContext": "本地生活与社区漫步",
        "previousAnchor": {
            "amapId": "anchor-before",
            "longitude": 116.397,
            "latitude": 39.908,
        },
        "nextAnchor": {
            "amapId": "anchor-after",
            "longitude": 116.407,
            "latitude": 39.918,
        },
        "excludedPhysicalPoiIds": ["excluded-2", "excluded-1"],
        "targetCount": 1,
        "evidenceTargetCount": 2,
        "maxQueries": 4,
    }
    values.update(overrides)
    return ExperienceSemanticInput(**values)


@pytest.mark.parametrize(
    (
        "family",
        "raw_need",
        "expected_facets",
        "expected_keyword",
        "expected_category",
        "expected_family",
        "expected_activity",
    ),
    [
        (
            "heritage_walk",
            "历史街区与胡同漫步",
            {"heritage", "historic_district", "walkable"},
            "历史文化街区",
            "heritage_district",
            "heritage",
            "walk",
        ),
        (
            "local_life",
            "体验社区日常生活",
            {"local_life", "neighborhood", "walkable"},
            "社区市场",
            "neighborhood",
            "local_life",
            "observe_walk",
        ),
        (
            "market_walk",
            "逛传统市集和菜市场",
            {"market", "traditional_market", "walkable"},
            "传统市集",
            "market",
            "market",
            "walk_eat",
        ),
        (
            "art_walk",
            "艺术街区与创意园区漫步",
            {"art", "creative_district", "walkable"},
            "艺术街区",
            "art_district",
            "art",
            "walk_visit",
        ),
        (
            "park_relax",
            "在城市公园放松散步",
            {"park", "green_space", "relax"},
            "城市公园",
            "park",
            "park",
            "relax_walk",
        ),
    ],
)
def test_compiles_supported_experience_family_to_provider_neutral_query_plans(
    family: str,
    raw_need: str,
    expected_facets: set[str],
    expected_keyword: str,
    expected_category: str,
    expected_family: str,
    expected_activity: str,
) -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            optionalExperienceFamily=family,
            rawNeed=raw_need,
            semanticContext=raw_need,
            intentType="park" if family == "park_relax" else "area_walk",
        )
    )

    assert profile.schemaVersion == "poi-search-profile-v1"
    assert profile.experienceFamily == expected_family
    assert profile.originalExperienceFamily == family
    assert expected_facets.issubset(profile.semanticFacets)
    assert profile.activityMode == expected_activity
    assert profile.queryPlans
    assert any(expected_keyword in plan.keywordVariants for plan in profile.queryPlans)
    assert any(
        expected_category in plan.providerCategoryKeys for plan in profile.queryPlans
    )
    assert all(plan.mode != "exact_entity" for plan in profile.queryPlans)
    assert all(plan.requiresAmapGrounding is True for plan in profile.queryPlans)
    assert "web_seed_then_amap" in {plan.mode for plan in profile.queryPlans}
    assert len({plan.planId for plan in profile.queryPlans}) == len(profile.queryPlans)
    assert re.fullmatch(r"[0-9a-f]{64}", profile.profileFingerprint)


def test_unknown_family_uses_explicit_non_scenic_fallback() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            optionalExperienceFamily="tea_ceremony_walk",
            rawNeed="茶文化慢体验",
            semanticContext="茶文化慢体验",
        )
    )

    assert profile.experienceFamily == "unknown"
    assert profile.originalExperienceFamily == "tea_ceremony_walk"
    assert profile.fallbackPolicy.status == "explicit_fallback"
    assert profile.fallbackPolicy.reasonCode == "unknown_experience_family"
    assert search_profile_provider_rejection_reason(profile) == (
        "creative_optional_family_unregistered"
    )
    assert profile.fallbackPolicy.allowGenericScenic is False
    assert profile.sourceEvidence.fallbackApplied is True
    assert profile.queryPlans
    assert all("scenic" not in plan.providerCategoryKeys for plan in profile.queryPlans)
    assert all(plan.fallbackLevel > 0 for plan in profile.queryPlans)


def test_unknown_optional_family_does_not_inherit_area_walk_core_category() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            optionalExperienceFamily="scenic",
            intentType="area_walk",
            rawNeed="泛化景点漫步",
            semanticContext="未知 Creative optional family",
        )
    )

    assert profile.experienceFamily == "unknown"
    assert profile.originalExperienceFamily == "scenic"
    assert profile.sourceEvidence.familySource == "optional_experience_family"
    assert profile.sourceEvidence.fallbackApplied is True
    assert profile.fallbackPolicy.reasonCode == "unknown_experience_family"
    assert profile.fallbackPolicy.requiresUserVisibleDegradedState is True
    assert profile.semanticFacets == []
    assert {
        category
        for plan in profile.queryPlans
        for category in plan.providerCategoryKeys
    } == {"all"}


def test_core_intent_candidate_hints_compile_to_distinct_amap_text_plans() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            requirementLevel="required",
            rawNeed="高校参观",
            intentType="campus_visit",
            optionalExperienceFamily=None,
            candidateHints=["A大学", "B大学"],
            hintPolicy="llm_common_knowledge_hint",
            targetCount=2,
            evidenceTargetCount=2,
            maxQueries=4,
        )
    )

    direct_keywords = {
        plan.keyword for plan in profile.queryPlans if plan.mode == "amap_text"
    }
    assert {"A大学", "B大学"}.issubset(direct_keywords)
    assert profile.experienceFamily == "campus_visit"
    assert profile.fallbackPolicy.requiresUserVisibleDegradedState is False
    assert all(
        "campus" in plan.providerCategoryKeys
        for plan in profile.queryPlans
        if plan.mode == "amap_text"
    )
    assert profile.sourceEvidence.candidateHintsUsedAsSemanticEvidence is False


def test_server_experience_spec_family_overlays_core_night_view_without_becoming_creative_optional() -> None:
    compiler = ExperienceSearchProfileCompiler()
    baseline = compiler.compile(
        _semantic_input(
            requirementLevel="required",
            rawNeed="公共城市夜景",
            intentType="night_view",
            optionalExperienceFamily=None,
            candidateHints=[],
            maxQueries=4,
        )
    )
    profile = compiler.compile(
        _semantic_input(
            requirementLevel="required",
            rawNeed="公共城市夜景",
            intentType="night_view",
            optionalExperienceFamily=None,
            candidateHints=[],
            maxQueries=4,
            experienceSpecFamily="public_city_view",
        )
    )

    assert profile.experienceFamily == "public_city_view"
    assert profile.originalExperienceFamily is None
    assert profile.sourceEvidence.familySource == "experience_spec_family"
    assert profile.profileFingerprint != baseline.profileFingerprint
    assert "web_seed_then_amap" in {plan.mode for plan in profile.queryPlans}
    assert search_profile_provider_rejection_reason(profile) is None


def test_exact_entity_compiles_identity_only_query_without_semantic_broadening() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            rawNeed="中国美术馆",
            intentType="museum",
            optionalExperienceFamily="art_walk",
            entityBindingMode="exact_entity",
            exactEntity="中国美术馆",
            preferredTypes=["博物馆", "美术馆"],
        )
    )

    assert profile.entityBindingMode == "exact_entity"
    assert profile.exactEntity == "中国美术馆"
    assert len(profile.queryPlans) == 1
    plan = profile.queryPlans[0]
    assert plan.mode == "exact_entity"
    assert plan.keyword == "中国美术馆"
    assert plan.keywordVariants == ["中国美术馆"]
    assert plan.fallbackLevel == 0
    assert plan.anchorPolicy == "none"
    assert profile.scoringPolicy.exactEntityRequired is True
    assert profile.fallbackPolicy.allowSemanticBroadening is False


def test_exact_entity_overrides_unknown_family_degraded_fallback() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            optionalExperienceFamily="future_unknown_family",
            entityBindingMode="exact_entity",
            exactEntity="中国美术馆",
            intentType="museum",
        )
    )

    assert profile.queryPlans[0].mode == "exact_entity"
    assert profile.queryPlans[0].keyword == "中国美术馆"
    assert profile.fallbackPolicy.requiresUserVisibleDegradedState is False
    assert profile.fallbackPolicy.allowSemanticBroadening is False
    assert search_profile_provider_rejection_reason(profile) is None


def test_exact_entity_mode_requires_an_exact_entity() -> None:
    with pytest.raises(ValueError, match="exactEntity"):
        ExperienceSearchProfileCompiler().compile(
            _semantic_input(entityBindingMode="exact_entity", exactEntity=None)
        )


def test_fingerprints_separate_semantics_exclusions_and_execution_anchors() -> None:
    compiler = ExperienceSearchProfileCompiler()
    base = compiler.compile(_semantic_input())
    same_semantics_different_execution = compiler.compile(
        _semantic_input(
            excludedPhysicalPoiIds=["excluded-3"],
            previousAnchor={
                "amapId": "other-before",
                "longitude": 116.3,
                "latitude": 39.8,
            },
            nextAnchor=None,
        )
    )

    assert (
        base.profileFingerprint
        == same_semantics_different_execution.profileFingerprint
    )
    assert (
        base.exclusionFingerprint
        != same_semantics_different_execution.exclusionFingerprint
    )
    assert base.executionFingerprint != same_semantics_different_execution.executionFingerprint

    same_set_different_order = compiler.compile(
        _semantic_input(
            excludedPhysicalPoiIds=["excluded-1", "excluded-2", "excluded-1"]
        )
    )
    assert same_set_different_order.excludedPhysicalPoiIds == [
        "excluded-1",
        "excluded-2",
    ]
    assert base.exclusionFingerprint == same_set_different_order.exclusionFingerprint
    assert base.executionFingerprint == same_set_different_order.executionFingerprint

    changed_semantics = compiler.compile(
        _semantic_input(rawNeed="传统市场漫步", optionalExperienceFamily="market_walk")
    )
    assert base.profileFingerprint != changed_semantics.profileFingerprint
    assert base.executionFingerprint != changed_semantics.executionFingerprint


def test_profile_preserves_slot_experience_intent_contract():
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            requirementLevel="soft",
            experienceShape="area",
            experienceGoal="观察居民日常活动",
            desiredSignals=["community_market"],
            avoidSignals=["museum_only"],
            evidencePolicy={"minimumIndependentClaims": 1},
            groundingPolicy={"consumerRecheckRequired": True},
            routeContext={"maxDetourMinutes": 20},
            intentFingerprint="f" * 64,
        )
    )
    assert profile.experienceShape == "area"
    assert profile.experienceGoal == "观察居民日常活动"
    assert profile.desiredSignals == ["community_market"]
    assert profile.intentFingerprint == "f" * 64


def test_profile_models_forbid_unknown_contract_fields() -> None:
    with pytest.raises(ValidationError):
        _semantic_input(unexpectedField="must fail closed")


def test_registered_family_with_unknown_creative_intent_is_provider_blocked() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        _semantic_input(
            optionalExperienceFamily="art_walk",
            intentType="future_intent",
            rawNeed="未来艺术体验",
        )
    )

    assert profile.experienceFamily == "art"
    assert profile.originalExperienceFamily == "art_walk"
    assert profile.fallbackPolicy.status == "explicit_fallback"
    assert profile.fallbackPolicy.reasonCode == "creative_optional_intent_unregistered"
    assert profile.fallbackPolicy.allowSemanticBroadening is False
    assert search_profile_provider_rejection_reason(profile) == (
        "creative_optional_intent_unregistered"
    )
    assert profile.fallbackPolicy.requiresUserVisibleDegradedState is True
    assert profile.sourceEvidence.fallbackApplied is True

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.api.schemas.agent import AgentInitialPlanOutput
from src.api.schemas.maps import MapPoiSearchResponse
from src.services.meal_experience_portfolio import MealExperiencePortfolioPolicy
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor


def _brief(term: str, *, slot_id: str = "meal_day_1") -> dict:
    return {
        "briefId": f"brief:{slot_id}",
        "proposalBriefId": "proposal:1",
        "planningSlotId": slot_id,
        "dayNumber": 1,
        "themeId": term,
        "themeLabel": term,
        "experienceMode": "signature_dish",
        "searchTerms": [term],
        "generationSource": "llm_search_hypothesis",
        "sourceFingerprint": "f" * 64,
    }


def test_query_plan_keeps_concrete_keyword_separate_from_provider_type() -> None:
    plan = MealExperiencePortfolioPolicy.query_plan(
        _brief("卤煮"),
        raw_query="当地特色美食",
        city="北京",
        provider_types="北京菜",
        local_food_required=True,
    )

    assert plan.keyword == "卤煮"
    assert plan.provider_types == "北京菜"
    assert plan.provider_type_policy == "destination_cuisine_subtype"
    assert plan.fallback_allowed is True


def test_provider_fallback_uses_only_the_hint_assigned_to_the_current_slot() -> None:
    pool = SimpleNamespace(
        meal_experience_briefs=[],
        candidate_hints=["当地特色餐饮", "地方风味餐厅", "传统市场周边餐饮"],
        assign_to_slots=["meal_day_1", "meal_day_2"],
        brief_id="proposal:1",
    )

    day_one = MealExperiencePortfolioPolicy.brief_for_slot(
        pool,
        slot_id="meal_day_1",
        day_number=1,
        raw_need="当地特色餐饮",
        city="北京",
        source_fingerprint="f" * 64,
    )
    day_two = MealExperiencePortfolioPolicy.brief_for_slot(
        pool,
        slot_id="meal_day_2",
        day_number=2,
        raw_need="当地特色餐饮",
        city="北京",
        source_fingerprint="f" * 64,
    )

    assert day_one["searchTerms"] == []
    assert day_two["searchTerms"] == []
    assert day_one["generationSource"] == "provider_candidate_fallback"


def test_controller_meal_briefs_must_cover_slots_with_unique_themes() -> None:
    raw = {
        "reply": "two distinct meal hypotheses",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "meal_day_1",
                "dayNumber": 1,
                "startTime": "12:00",
                "kind": "meal",
                "rawNeed": "当地特色午餐",
            },
            {
                "slotId": "meal_day_2",
                "dayNumber": 2,
                "startTime": "12:00",
                "kind": "meal",
                "rawNeed": "当地特色午餐",
            },
        ],
        "intentPools": [
            {
                "poolId": "meal_pool",
                "rawNeed": "当地特色午餐",
                "city": "北京",
                "intentType": "meal",
                "targetCount": 2,
                "assignToSlots": ["meal_day_1", "meal_day_2"],
                "candidateHints": ["当地特色午餐", "当地特色午餐"],
                "mealExperienceBriefs": [
                    {
                        "briefId": "meal-brief-1",
                        "proposalBriefId": "proposal-1",
                        "planningSlotId": "meal_day_1",
                        "dayNumber": 1,
                        "mealLabel": "lunch",
                        "themeId": "theme-a",
                        "themeLabel": "theme a",
                        "experienceMode": "signature_dish",
                        "searchTerms": ["dish a"],
                        "selectionIntent": "first evidence hypothesis",
                    },
                    {
                        "briefId": "meal-brief-2",
                        "proposalBriefId": "proposal-1",
                        "planningSlotId": "meal_day_2",
                        "dayNumber": 2,
                        "mealLabel": "lunch",
                        "themeId": "theme-b",
                        "themeLabel": "theme b",
                        "experienceMode": "traditional_snack",
                        "searchTerms": ["dish b"],
                        "selectionIntent": "second evidence hypothesis",
                    },
                ],
            }
        ],
    }

    assert len(AgentInitialPlanOutput.model_validate(raw).intent_pools[0].meal_experience_briefs) == 2

    duplicate = raw.copy()
    duplicate["intentPools"] = [dict(raw["intentPools"][0])]
    duplicate["intentPools"][0]["mealExperienceBriefs"] = [
        dict(item) for item in raw["intentPools"][0]["mealExperienceBriefs"]
    ]
    duplicate["intentPools"][0]["mealExperienceBriefs"][1]["themeId"] = "theme-a"
    with pytest.raises(ValidationError, match="meal_experience_brief_theme_duplicate"):
        AgentInitialPlanOutput.model_validate(duplicate)


def test_model_theme_is_not_grounded_without_provider_match() -> None:
    evidence = MealExperiencePortfolioPolicy.semantic_evidence(
        {
            "id": "B000GENERIC1",
            "name": "学府餐厅",
            "type": "餐饮服务;中餐厅;北京菜",
            "tags": ["聚餐"],
        },
        brief=_brief("卤煮"),
        city="北京",
        provider_types="北京菜",
        local_food_required=True,
    )

    assert evidence["themeGrounded"] is False
    assert evidence["groundedFamilyKey"] == ""
    assert evidence["localFoodPassed"] is True


def test_amap_tags_ground_theme_and_local_cuisine_evidence() -> None:
    evidence = MealExperiencePortfolioPolicy.semantic_evidence(
        {
            "id": "B000LOCAL01",
            "name": "老北京风味馆(学院路店)",
            "type": "餐饮服务;中餐厅;北京菜",
            "tags": ["卤煮", "豆汁"],
        },
        brief=_brief("卤煮"),
        city="北京",
        provider_types="北京菜",
        local_food_required=True,
    )

    assert evidence["themeGrounded"] is True
    assert evidence["groundedFamilyKey"] == "卤煮"
    assert evidence["matchedFields"] == ["tags"]
    assert evidence["localFoodEvidenceKind"] == "amap_destination_cuisine_subtype"


def test_snapshot_quality_rejects_repeated_brand_and_grounded_family() -> None:
    def segment(segment_id: str, day_number: int, amap_id: str) -> dict:
        brief = _brief("炸酱面", slot_id=segment_id)
        return {
            "id": segment_id,
            "kind": "meal",
            "poi": {"id": amap_id},
            "semanticMetadata": {
                "intentType": "meal",
                "planningSlotId": segment_id,
                "scheduleConstraints": {
                    "localFoodRequired": True,
                    "mealExperienceBrief": brief,
                    "mealSemanticEvidence": {
                        "amapPoiId": amap_id,
                        "canonicalBrand": "同一品牌",
                        "themeId": "炸酱面",
                        "themeLabel": "炸酱面",
                        "groundedFamilyKey": "炸酱面",
                        "matchedTerms": ["炸酱面"],
                        "matchedFields": ["tags"],
                        "localFoodEvidenceKind": "amap_destination_cuisine_subtype",
                        "themeGrounded": True,
                        "localFoodPassed": True,
                        "sourceFingerprint": brief["sourceFingerprint"],
                    },
                },
            },
        }

    quality = MealExperiencePortfolioPolicy.snapshot_quality(
        {
            "days": [
                {"dayNumber": 1, "segments": [segment("meal_day_1", 1, "B0001")]},
                {"dayNumber": 2, "segments": [segment("meal_day_2", 2, "B0002")]},
            ]
        }
    )

    assert quality["mealQualityPassed"] is False
    assert quality["mealDiversityPassed"] is False
    assert "simple_direction_meal_brand_repeated" in quality["mealUnresolvedReasons"]
    assert "simple_direction_meal_family_repeated" in quality["mealUnresolvedReasons"]


def test_snapshot_quality_rejects_grounded_meal_without_authoritative_brief() -> None:
    quality = MealExperiencePortfolioPolicy.snapshot_quality(
        {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "meal_day_1",
                            "kind": "meal",
                            "semanticMetadata": {
                                "intentType": "meal",
                                "planningSlotId": "meal_day_1",
                                "scheduleConstraints": {
                                    "localFoodRequired": True,
                                    "mealSemanticEvidence": {
                                        "amapPoiId": "B000LOCAL01",
                                        "canonicalBrand": "本地风味馆",
                                        "themeId": "炸酱面",
                                        "themeLabel": "炸酱面",
                                        "groundedFamilyKey": "炸酱面",
                                        "matchedTerms": ["炸酱面"],
                                        "matchedFields": ["tags"],
                                        "localFoodEvidenceKind": "amap_destination_cuisine_subtype",
                                        "themeGrounded": True,
                                        "localFoodPassed": True,
                                        "sourceFingerprint": "f" * 64,
                                    },
                                },
                            },
                        }
                    ],
                }
            ]
        }
    )

    assert quality["mealQualityPassed"] is False
    assert quality["mealDiversityPassed"] is False
    assert quality["mealUnresolvedReasons"] == ["simple_direction_meal_brief_missing"]


class _EmptyMealProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def search(self, city: str, *, keyword: str, category: str, **kwargs) -> MapPoiSearchResponse:
        self.calls.append((keyword, kwargs.get("provider_types")))
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[],
        )


def test_two_meal_slots_share_at_most_one_general_food_fallback_per_direction() -> None:
    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "two meal themes",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "meal_day_1",
                    "dayNumber": 1,
                    "startTime": "12:00",
                    "kind": "meal",
                    "rawNeed": "当地特色午餐",
                },
                {
                    "slotId": "meal_day_2",
                    "dayNumber": 2,
                    "startTime": "12:00",
                    "kind": "meal",
                    "rawNeed": "当地特色午餐",
                },
            ],
            "intentPools": [
                {
                    "poolId": "meal_pool",
                    "rawNeed": "当地特色午餐",
                    "city": "北京",
                    "intentType": "meal",
                    "targetCount": 2,
                    "assignToSlots": ["meal_day_1", "meal_day_2"],
                    "candidateHints": ["卤煮", "炸酱面"],
                    "mealExperienceBriefs": [
                        {
                            **_brief("卤煮", slot_id="meal_day_1"),
                            "dayNumber": 1,
                        },
                        {
                            **_brief("炸酱面", slot_id="meal_day_2"),
                            "dayNumber": 2,
                        },
                    ],
                }
            ],
        }
    )
    provider = _EmptyMealProvider()

    _plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="transit",
        experience_policies_by_intent={
            "meal": {
                "localExperienceConstraint": {
                    "experienceType": "local_cuisine",
                    "evidencePolicy": "provider_city_specific_fact",
                    "locality": {"city": "北京", "source": "request_destination"},
                }
            }
        },
        request_contract_fingerprint="f" * 64,
    )

    fallbacks = [
        event
        for event in events
        if (event.get("metadata") or {}).get("queryRole") == "meal_general_food_shared_fallback"
    ]
    assert len(fallbacks) == 1
    assert fallbacks[0]["metadata"]["fallbackOrdinalWithinDirection"] == 1
    assert provider.calls == [("卤煮", "北京菜"), ("卤煮", None), ("炸酱面", "北京菜")]


def test_route_comfort_preserves_unknown_optional_provider_fields() -> None:
    evidence = MealExperiencePortfolioPolicy.route_comfort_evidence(
        [
            {"durationSeconds": 600, "distanceMeters": 3200},
            {"durationSeconds": 900, "distanceMeters": 4800},
        ]
    )

    assert evidence["totalTravelSeconds"] == 1500
    assert evidence["maxAdjacentTravelSeconds"] == 900
    assert evidence["totalDistanceMeters"] == 8000
    assert evidence["walkingDistanceMeters"] is None
    assert evidence["transferCount"] is None
    assert evidence["unknownFields"] == ["walkingDistanceMeters", "transferCount"]

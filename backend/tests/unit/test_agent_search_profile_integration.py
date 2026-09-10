from __future__ import annotations

from src.models.poi_intent import DaySlot, IntentPool, PoiIntent
from src.services.agent_service import AgentService


class _RecordedItineraryService:
    def _plan_poi_intent(self, city: str, raw_need: str) -> PoiIntent:
        return PoiIntent(
            raw_need=raw_need,
            city=city,
            day_number=1,
            time_window="14:00-17:00",
            intent_type="area_walk",
            specificity="functional",
            preferred_types=["街区", "公园"],
            rejected_types=["酒店"],
        )

    def _candidate_search_queries(
        self,
        city: str,
        raw_need: str,
        intent_type: str,
        specificity: str,
    ) -> list[str]:
        return [f"{city} {raw_need}"]


def _compile_pool_intent(optional_family: str) -> PoiIntent:
    service = object.__new__(AgentService)
    pool = IntentPool(
        pool_id=f"pool-{optional_family}",
        brief_id=f"brief-{optional_family}",
        raw_need=optional_family,
        city="北京",
        intent_type="area_walk",
        target_count=1,
        requirement_level="optional",
        optional_experience_family=optional_family,
        preferred_types=[],
        rejected_types=["酒店", "停车场"],
        route_preference={"radiusMeters": 1800},
        assign_to_slots=["day-1-flex"],
        candidate_hints=[],
        hint_policy="no_hint",
    )
    slot = DaySlot(
        slot_id="day-1-flex",
        day_number=1,
        date="2026-10-01",
        time_window="14:00-17:00",
        start_time="14:00",
        duration_minutes=180,
        kind="visit",
        raw_need=optional_family,
        route_anchor=True,
    )

    return service._poi_intent_from_pool_slot(
        pool,
        slot,
        _RecordedItineraryService(),
        {
            "creativePortfolioMode": True,
            "latestUserMessage": "继续生成一种真正不同的北京玩法",
            "excludedFlexiblePhysicalPoiIds": ["B0USED1", "B0USED2"],
        },
    )


def test_optional_experience_family_is_compiled_before_map_collection() -> None:
    market = _compile_pool_intent("market_walk")
    heritage = _compile_pool_intent("heritage_walk")

    assert market.search_profile is not None
    assert heritage.search_profile is not None
    assert market.search_profile.experienceFamily == "market"
    assert market.search_profile.activityMode == "walk_eat"
    assert market.search_profile.excludedPhysicalPoiIds == ["B0USED1", "B0USED2"]
    assert heritage.search_profile.experienceFamily == "heritage"
    assert heritage.search_profile.activityMode == "walk"
    assert market.search_profile.profileFingerprint != heritage.search_profile.profileFingerprint
    assert market.optional_experience_family == "market_walk"


def test_profile_queries_replace_the_generic_area_walk_query_source() -> None:
    profile_intent = _compile_pool_intent("local_life")

    assert profile_intent.search_profile is not None
    assert profile_intent.search_queries == profile_intent.search_profile.keywordVariants[:4]
    assert any(
        "社区" in keyword or "本地生活" in keyword
        for keyword in profile_intent.search_queries
    )
    assert "scenic" not in {
        category
        for plan in profile_intent.search_profile.queryPlans
        for category in plan.providerCategoryKeys
    }


def test_non_portfolio_optional_family_keeps_legacy_grounding_path() -> None:
    service = object.__new__(AgentService)
    pool = IntentPool(
        pool_id="single-plan-optional",
        raw_need="社区生活体验",
        city="北京",
        intent_type="area_walk",
        target_count=21,
        requirement_level="optional",
        optional_experience_family="local_life",
        assign_to_slots=["single-plan-slot"],
    )
    slot = DaySlot(
        slot_id="single-plan-slot",
        day_number=1,
        date="2026-10-01",
        time_window="14:00-17:00",
        start_time="14:00",
        duration_minutes=180,
        kind="visit",
        raw_need="社区生活体验",
        route_anchor=True,
    )

    intent = service._poi_intent_from_pool_slot(
        pool,
        slot,
        _RecordedItineraryService(),
        {"creativePortfolioMode": False},
    )

    assert intent.search_profile is None


def test_same_root_visible_amap_ids_become_flexible_search_exclusions() -> None:
    service = object.__new__(AgentService)
    service._comparison_novelty_baseline = lambda **_kwargs: {
        "projections": [
            {
                "proposalId": "proposal-existing",
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "poi": {
                                    "amapId": "B0ABCDEF12",
                                    "source": "amap-place-search",
                                    "longitude": 116.4,
                                    "latitude": 39.9,
                                }
                            }
                        ],
                    }
                ],
            }
        ],
        "historicalProjectionCount": 1,
        "comparisonBaselineProposalIds": ["proposal-existing"],
        "suppressedLegacyProjectionIds": [],
    }
    context = {
        "partialPlanExpansionClaim": {
            "rootPortfolioId": "portfolio-root",
            "planningSelectionRootTurnId": "turn-root",
        }
    }

    service._bind_same_root_search_exclusions(
        session_id="session-1",
        assistant_turn_id="assistant-current",
        pipeline_context=context,
    )

    assert context["visiblePhysicalPoiIds"] == ["B0ABCDEF12"]
    assert context["excludedFlexiblePhysicalPoiIds"] == ["B0ABCDEF12"]
    assert context["planningSelectionRootTurnId"] == "turn-root"
    assert context["rootPortfolioId"] == "portfolio-root"
    assert context["searchExclusionContext"]["baselineProposalIds"] == [
        "proposal-existing"
    ]

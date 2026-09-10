"""Regression for the 2026-07-19 Beijing portfolio transcript.

The transcript is represented by its controller-produced structural directive.
No backend phrase matcher decides which day receives a goal.
"""
from __future__ import annotations

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import CreativePortfolioProviderService
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
from src.services.daily_capacity_planner import DailyCapacityPlanner
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.shared_candidate_universe_service import SharedCandidateUniverse

from test_agent_daily_recurring_goal_portfolio import _Store, _candidate, _snapshot_builder


ORIGINAL_TRANSCRIPT = (
    "今年国庆参观985大学两日游，然后去体验下北京当地博物馆陶冶情操。"
    "10月1日到2日，2天，中等预算，1人，公交地铁优先。路途中能品尝北京当地特色美食"
)


def test_20260719_transcript_structural_directive_offers_only_without_version_or_patch_write():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {"goalId": "goal_campus", "intentType": "campus_visit", "requiredMin": 1},
                    {"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1},
                    {"goalId": "goal_meal", "intentType": "meal", "requiredMin": 1, "requirementLevel": "soft_experience"},
                ],
            },
        }
    )
    directive = {
        "type": "draft_itinerary",
        "goalPriority": ["goal_campus", "goal_museum", "goal_meal"],
        "dayStrategies": [
            {"dayNumber": 1, "theme": "campus", "requiredGoalIds": ["goal_campus"], "requiredGoalCounts": {"goal_campus": 1}, "optionalGoalIds": ["goal_meal"], "pace": "standard", "maxRouteAnchors": 4},
            {"dayNumber": 2, "theme": "museum", "requiredGoalIds": ["goal_museum"], "requiredGoalCounts": {"goal_museum": 1}, "optionalGoalIds": [], "pace": "standard", "maxRouteAnchors": 4},
        ],
        "optionalExperienceBudget": 1,
        "candidateSelectionPolicy": {"autoSelectWhenDominant": True, "askWhenMaterialTradeoff": False, "preferLowDetour": True, "avoidRecentEntities": True},
        "schedulePolicy": {"respectOpeningWindowsWhenKnown": True, "allowProvisionalWhenUnknown": True},
    }
    occurrences = GoalOccurrenceCompiler().compile(ledger, directive)
    generated = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive=directive,
        schema_repair_attempts=0,
        occurrence_plan=occurrences,
        daily_capacity=DailyCapacityPlanner().plan(occurrences, pace=ledger.pace, day_count=2),
    )
    universe = SharedCandidateUniverse(
        {
            "campus_visit": [_candidate("campus-1", intent_type="campus_visit", score=1)],
            "museum": [_candidate("museum-1", intent_type="museum", score=1)],
            "meal": [_candidate("meal-1", intent_type="meal", score=1)],
            "area_walk": [_candidate("walk-1", intent_type="area_walk", score=1)],
            "art_walk": [_candidate("art-1", intent_type="art_walk", score=1)],
            "heritage_walk": [_candidate("heritage-1", intent_type="heritage_walk", score=1)],
            "local_life": [_candidate("local-1", intent_type="local_life", score=1)],
            "market_walk": [_candidate("market-1", intent_type="market_walk", score=1)],
            "park": [_candidate("park-1", intent_type="park", score=1)],
        },
        6,
        0,
    )
    store = _Store()
    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session", source_user_turn_id="user", source_assistant_turn_id="assistant",
        expected_base_version_id=None, observation_fingerprint="o" * 16, request_fingerprint="r" * 16,
        ledger=ledger, generated=generated, universe=universe, snapshot_builder=_snapshot_builder,
        goal_occurrence_plan=occurrences.model_dump(by_alias=True),
    )

    assert ORIGINAL_TRANSCRIPT
    assert portfolio.status == "awaiting_selection"
    assert visible
    assert store.saved is not None
    assert [item.source_goal_id for item in occurrences.occurrences] == ["goal_campus", "goal_meal", "goal_museum"]
    for proposal in visible:
        goal_days = {
            (segment["semanticMetadata"].get("sourceGoalId"), day["dayNumber"])
            for day in proposal.itinerary_snapshot["days"]
            for segment in day["segments"]
            if segment["semanticMetadata"].get("sourceGoalId")
        }
        assert ("goal_campus", 2) not in goal_days
        assert proposal.verifier["hardFailures"] == []
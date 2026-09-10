"""Cold/warm Portfolio parity for the recorded daily-goal contract."""
from __future__ import annotations

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import CreativePortfolioProviderService
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
from src.services.daily_capacity_planner import DailyCapacityPlanner
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.shared_candidate_universe_service import SharedCandidateUniverse

from test_agent_daily_recurring_goal_portfolio import _Store, _candidate, _recorded_directive, _snapshot_builder


def _stage_once(cache_state: str):
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
    directive = _recorded_directive()
    occurrences = GoalOccurrenceCompiler().compile(ledger, directive)
    generated = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive=directive,
        schema_repair_attempts=0,
        occurrence_plan=occurrences,
        daily_capacity=DailyCapacityPlanner().plan(occurrences, pace=ledger.pace, day_count=2),
    )
    pools = {
        intent: [_candidate(f"{prefix}-1", intent_type=intent, score=1), _candidate(f"{prefix}-2", intent_type=intent, score=2)]
        for intent, prefix in {
            "campus_visit": "campus", "museum": "museum", "meal": "meal", "area_walk": "walk",
            "art_walk": "art", "heritage_walk": "heritage", "local_life": "local", "market_walk": "market", "park": "park",
        }.items()
    }
    portfolio, visible = CreativePortfolioStagingService(_Store()).stage(
        session_id=f"session-{cache_state}",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(pools, 6, 0),
        snapshot_builder=_snapshot_builder,
        goal_occurrence_plan=occurrences.model_dump(by_alias=True),
    )
    signature = [
        {
            "brief": proposal.brief.brief_id,
            "canonical": proposal.canonical_signature,
            "anchors": proposal.verifier["dayAnchorActuals"],
            "goals": sorted(
                (day["dayNumber"], segment["semanticMetadata"].get("sourceGoalId"), segment["poi"]["amapId"])
                for day in proposal.itinerary_snapshot["days"]
                for segment in day["segments"]
                if segment["semanticMetadata"].get("sourceGoalId")
            ),
        }
        for proposal in visible
    ]
    return portfolio.status, signature


def test_creative_portfolio_cold_warm_parity_preserves_visible_structural_signature():
    cold_status, cold_signature = _stage_once("cold")
    warm_status, warm_signature = _stage_once("warm")

    assert cold_status == warm_status == "awaiting_selection"
    assert cold_signature
    assert warm_signature == cold_signature
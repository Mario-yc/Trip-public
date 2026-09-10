from src.services.creative_direction_generator import CreativeDirectionGenerator
from src.services.creative_exploration_frontier_service import CreativeExplorationFrontierService


def test_three_bounded_rounds_can_attempt_more_than_four_structural_directions():
    service = CreativeExplorationFrontierService(max_generated_proposals_per_root=12)
    frontier = service.initial(
        planning_root_id="turn_root",
        portfolio_id="portfolio_root",
        fingerprint="fingerprint",
    )

    for round_number in range(1, 4):
        batch = CreativeDirectionGenerator.generate_next(
            hard_goal_strategy=["campus_day_1", "night_view_day_2"],
            day_anchor_targets={1: 3, 2: 3},
            used_signatures=frontier["attemptedDirectionSignatures"],
            limit=4,
        )
        signatures = [item["directionSignature"] for item in batch]
        assert len(batch) == 4
        frontier = service.advance(
            frontier,
            execution_id=f"execution_{round_number}",
            attempted_direction_signatures=signatures,
            accepted_direction_signatures=signatures,
            provider_called=True,
        )

    assert len(frontier["attemptedDirectionSignatures"]) == 12
    assert len(frontier["acceptedDirectionSignatures"]) == 12
    assert frontier["limits"]["maxProposalsPerProviderCall"] == 4
    assert frontier["frontierState"] == "budget_exhausted"
    assert frontier["exhaustionReason"] == "max_generated_proposals_per_root"


def test_duplicate_execution_does_not_advance_cursor_or_consume_budget_twice():
    service = CreativeExplorationFrontierService(max_generated_proposals_per_root=12)
    frontier = service.initial(
        planning_root_id="turn_root",
        portfolio_id="portfolio_root",
        fingerprint="fingerprint",
    )
    advanced = service.advance(
        frontier,
        execution_id="execution_1",
        attempted_direction_signatures=["direction_1"],
        accepted_direction_signatures=["direction_1"],
        provider_called=True,
    )

    assert service.advance(
        advanced,
        execution_id="execution_1",
        attempted_direction_signatures=["direction_2"],
        accepted_direction_signatures=["direction_2"],
        provider_called=True,
    ) == advanced

from src.services.creative_exploration_frontier_service import CreativeExplorationFrontierService


def test_root_budget_is_distinct_from_per_call_limit_and_advances_idempotently():
    service = CreativeExplorationFrontierService(max_generated_proposals_per_root=10)
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")
    assert frontier["limits"]["maxProposalsPerProviderCall"] == 4
    assert frontier["remainingBudget"] == 10

    advanced = service.advance(
        frontier,
        execution_id="exec_1",
        attempted_direction_signatures=[f"direction_{index}" for index in range(4)],
        accepted_direction_signatures=[f"direction_{index}" for index in range(4)],
        provider_called=True,
    )
    duplicate = service.advance(
        advanced,
        execution_id="exec_1",
        attempted_direction_signatures=["direction_extra"],
        accepted_direction_signatures=["direction_extra"],
        provider_called=True,
    )

    assert len(advanced["attemptedDirectionSignatures"]) == 4
    assert advanced["remainingBudget"] == 6
    assert duplicate == advanced
    assert advanced["frontierState"] == "has_more"


def test_amap_candidate_budget_is_root_scoped_and_advance_is_idempotent():
    service = CreativeExplorationFrontierService(
        max_amap_candidate_text_calls_per_root=3,
        max_amap_candidate_around_calls_per_root=2,
        max_amap_candidate_calls_per_root=4,
        max_amap_calls_per_continuation=2,
    )
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")

    initial_budget = service.candidate_amap_budget(frontier)
    assert initial_budget.place_text_max == 3
    assert initial_budget.place_around_max == 2
    assert initial_budget.total_external_max == 4

    first = service.advance(
        frontier,
        execution_id="exec_1",
        attempted_direction_signatures=["direction_1"],
        accepted_direction_signatures=[],
        provider_called=False,
        amap_candidate_usage={
            "usedPlaceText": 2,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 1,
            "usedTotalExternal": 3,
        },
    )
    duplicate = service.advance(
        first,
        execution_id="exec_1",
        attempted_direction_signatures=["direction_duplicate"],
        accepted_direction_signatures=[],
        provider_called=False,
        amap_candidate_usage={
            "usedPlaceText": 2,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 1,
            "usedTotalExternal": 3,
        },
    )

    assert duplicate == first
    assert first["amapCandidateUsage"] == {
        "usedPlaceText": 2,
        "usedPlaceDetail": 0,
        "usedPlaceAround": 1,
        "usedTotalExternal": 3,
    }
    continuation_budget = service.candidate_amap_budget(first)
    assert continuation_budget.place_text_max == 1
    assert continuation_budget.place_around_max == 1
    assert continuation_budget.total_external_max == 1

    exhausted = service.advance(
        first,
        execution_id="exec_2",
        attempted_direction_signatures=["direction_2"],
        accepted_direction_signatures=[],
        provider_called=False,
        amap_candidate_usage={
            "usedPlaceText": 1,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 0,
            "usedTotalExternal": 1,
        },
    )

    assert exhausted["amapCandidateUsage"]["usedTotalExternal"] == 4
    assert exhausted["frontierState"] == "budget_exhausted"
    assert exhausted["exhaustionReason"] == "max_amap_candidate_calls_per_root"
    assert not service.can_continue(exhausted)
    denied_budget = service.candidate_amap_budget(exhausted)
    assert denied_budget.place_text_max == 0
    assert denied_budget.place_around_max == 0
    assert denied_budget.total_external_max == 0


def test_amap_candidate_budget_claim_blocks_parallel_provider_work_and_settles_actual_usage():
    service = CreativeExplorationFrontierService(
        max_amap_candidate_text_calls_per_root=3,
        max_amap_candidate_around_calls_per_root=2,
        max_amap_candidate_calls_per_root=4,
        max_amap_calls_per_continuation=2,
    )
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")
    frontier["continuationRound"] = 1

    claimed = service.reserve_candidate_amap_budget(frontier, execution_id="exec_1")
    contender = service.reserve_candidate_amap_budget(claimed["frontier"], execution_id="exec_2")
    replay = service.reserve_candidate_amap_budget(claimed["frontier"], execution_id="exec_1")

    assert claimed["status"] == "CLAIMED"
    assert claimed["providerCallAllowed"] is True
    assert claimed["allocation"] == {
        "placeTextMax": 2,
        "placeAroundMax": 2,
        "totalExternalMax": 2,
    }
    assert contender["status"] == "CLAIM_IN_FLIGHT"
    assert contender["providerCallAllowed"] is False
    assert contender["allocation"]["totalExternalMax"] == 0
    assert replay["status"] == "REPLAY_RESERVED"
    assert replay["providerCallAllowed"] is False

    settled = service.settle_candidate_amap_budget(
        claimed["frontier"],
        execution_id="exec_1",
        actual_usage={
            "usedPlaceText": 1,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 0,
            "usedTotalExternal": 1,
        },
    )
    next_claim = service.reserve_candidate_amap_budget(settled["frontier"], execution_id="exec_2")
    settled_replay = service.settle_candidate_amap_budget(
        settled["frontier"],
        execution_id="exec_1",
        actual_usage={
            "usedPlaceText": 1,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 0,
            "usedTotalExternal": 1,
        },
    )

    assert settled["status"] == "SETTLED"
    assert settled["frontier"]["amapCandidateUsage"]["usedTotalExternal"] == 1
    assert next_claim["status"] == "CLAIMED"
    assert next_claim["providerCallAllowed"] is True
    assert next_claim["allocation"]["totalExternalMax"] == 2
    assert settled_replay["status"] == "REPLAY_SETTLED"
    assert settled_replay["frontier"] == settled["frontier"]


def test_frontier_reports_budget_exhaustion_instead_of_silent_completion():
    service = CreativeExplorationFrontierService(max_generated_proposals_per_root=5)
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")
    exhausted = service.advance(
        frontier,
        execution_id="exec_1",
        attempted_direction_signatures=[f"direction_{index}" for index in range(5)],
        accepted_direction_signatures=[f"direction_{index}" for index in range(5)],
        provider_called=True,
    )
    assert exhausted["frontierState"] == "budget_exhausted"
    assert exhausted["exhaustionReason"] == "max_generated_proposals_per_root"
    assert exhausted["remainingBudget"] == 0


def test_empty_next_direction_must_become_no_progress_not_has_more():
    service = CreativeExplorationFrontierService()
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")
    frontier = service.advance(
        frontier,
        execution_id="exec_1",
        attempted_direction_signatures=["rejected_direction"],
        accepted_direction_signatures=[],
        rejected_direction_signatures=["rejected_direction"],
        provider_called=False,
    )

    assert frontier["frontierState"] == "has_more"
    no_progress = service.no_progress(frontier, reason="no_progress_no_next_brief")

    assert no_progress["frontierState"] == "no_progress"
    assert no_progress["exhaustionReason"] == "no_progress_no_next_brief"
    assert no_progress["progressCertificate"] is None
    assert not service.can_continue(no_progress)


def test_terminal_frontier_never_offers_continuation_even_with_budget_field():
    for state in ("ready", "has_more", "temporarily_degraded", "route_degraded"):
        assert CreativeExplorationFrontierService.can_continue({"frontierState": state, "remainingBudget": 1})
    assert not CreativeExplorationFrontierService.can_continue(
        {"frontierState": "budget_exhausted", "remainingBudget": 1}
    )
    assert not CreativeExplorationFrontierService.can_continue({"frontierState": "has_more", "remainingBudget": 0})


def test_failed_attempts_do_not_consume_formal_proposal_budget_or_exhaust_after_successes():
    service = CreativeExplorationFrontierService(
        max_generated_proposals_per_root=8,
        max_continuation_rounds_per_root=4,
        max_provider_calls_per_root=20,
    )
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")
    frontier = service.advance(
        frontier,
        execution_id="failed_initial",
        attempted_direction_signatures=["failed_direction"],
        accepted_direction_signatures=[],
        rejected_direction_signatures=["failed_direction"],
        provider_called=True,
    )
    for index in range(5):
        signature = f"accepted_{index}"
        frontier = service.advance(
            frontier,
            execution_id=f"success_{index}",
            attempted_direction_signatures=[signature],
            accepted_direction_signatures=[signature],
            provider_called=True,
        )

    assert frontier["frontierState"] == "has_more"
    assert frontier["remainingBudget"] == 3
    assert frontier["acceptedProposalCount"] == 5
    assert frontier["consecutiveFailedAttempts"] == 0

    sixth = service.advance(
        frontier,
        execution_id="success_5",
        attempted_direction_signatures=["accepted_5"],
        accepted_direction_signatures=["accepted_5"],
        provider_called=True,
    )
    assert sixth["acceptedProposalCount"] == 6
    assert sixth["remainingBudget"] == 2
    assert sixth["frontierState"] == "has_more"


def test_consecutive_failed_attempt_window_is_retryable_not_formal_budget_exhaustion():
    service = CreativeExplorationFrontierService(
        max_generated_proposals_per_root=8,
        max_continuation_rounds_per_root=2,
        max_provider_calls_per_root=20,
    )
    frontier = service.initial(planning_root_id="turn_root", portfolio_id="portfolio_root", fingerprint="fp")
    for index in range(2):
        frontier = service.advance(
            frontier,
            execution_id=f"failure_{index}",
            attempted_direction_signatures=[f"failed_{index}"],
            accepted_direction_signatures=[],
            rejected_direction_signatures=[f"failed_{index}"],
            provider_called=True,
        )

    assert frontier["frontierState"] == "temporarily_degraded"
    assert frontier["exhaustionReason"] == "max_consecutive_failed_attempts"
    assert frontier["remainingBudget"] == 8

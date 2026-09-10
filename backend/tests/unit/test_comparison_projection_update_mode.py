from src.services.agent_choice_trace_service import comparison_projection_update_mode


def test_persisted_expansion_proof_is_authoritative_over_the_mode_marker():
    assert (
        comparison_projection_update_mode(
            {
                "comparisonProjectionUpdateMode": "append",
                "comparisonProjections": [
                    {
                        "proposalId": "proposal_new",
                        "choiceId": "adopt_proposal_new",
                        "sourceAssistantTurnId": "turn_result",
                        "planningSelectionRootTurnId": "turn_root",
                        "rootPortfolioId": "portfolio_root",
                    }
                ],
                "choiceOptions": [
                    {
                        "id": "adopt_proposal_new",
                        "comparisonProjection": {
                            "proposalId": "proposal_new",
                            "choiceId": "adopt_proposal_new",
                            "sourceAssistantTurnId": "turn_result",
                            "planningSelectionRootTurnId": "turn_root",
                            "rootPortfolioId": "portfolio_root",
                        },
                    }
                ],
            },
            {
                "sourceAssistantTurnId": "turn_source",
                "sourceAssistantTurnRole": "assistant",
                "sourceAssistantTurnStatus": "active",
                "requestChoiceId": "choice_more_plans",
                "persistedChoiceId": "choice_more_plans",
                "resolvedChoiceId": "choice_more_plans",
                "executionChoiceId": "choice_more_plans",
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
                "executionAction": "retry_model_planning",
                "executionRoute": "controller_choice_resume",
                "executionStatus": "succeeded",
                "outcome": {
                    "reason": "new_verified_proposal",
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                },
            },
            "turn_result",
        )
        == "append"
    )
    assert (
        comparison_projection_update_mode(
            {
                "comparisonProjectionUpdateMode": "replace",
                "comparisonProjections": [
                    {
                        "proposalId": "proposal_final",
                        "sourceAssistantTurnId": "turn_result",
                        "planningSelectionRootTurnId": "turn_root",
                        "rootPortfolioId": "portfolio_root",
                    }
                ],
            },
            {
                "executionRoute": "controller_choice_resume",
                "executionStatus": "succeeded",
                "outcome": {"reason": "new_verified_proposal"},
            },
            "turn_result",
        )
        == "replace"
    )


def test_legacy_successful_more_plans_trace_is_inferred_as_append():
    assert (
        comparison_projection_update_mode(
            {
                "comparisonProjections": [
                    {
                        "proposalId": "proposal_new",
                        "choiceId": "adopt_proposal_new",
                        "sourceAssistantTurnId": "turn_result",
                        "planningSelectionRootTurnId": "turn_root",
                        "rootPortfolioId": "portfolio_root",
                    }
                ],
                "choiceOptions": [
                    {
                        "id": "adopt_proposal_new",
                        "comparisonProjection": {
                            "proposalId": "proposal_new",
                            "choiceId": "adopt_proposal_new",
                            "sourceAssistantTurnId": "turn_result",
                            "planningSelectionRootTurnId": "turn_root",
                            "rootPortfolioId": "portfolio_root",
                        },
                    }
                ],
            },
            {
                "sourceAssistantTurnId": "turn_source",
                "sourceAssistantTurnRole": "assistant",
                "sourceAssistantTurnStatus": "active",
                "requestChoiceId": "choice_more_plans",
                "persistedChoiceId": "choice_more_plans",
                "resolvedChoiceId": "choice_more_plans",
                "executionChoiceId": "choice_more_plans",
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
                "executionAction": "retry_model_planning",
                "executionRoute": "controller_choice_resume",
                "executionStatus": "succeeded",
                "outcome": {
                    "reason": "new_verified_proposal",
                    "proposalDelta": 1,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                },
            },
            "turn_result",
        )
        == "append"
    )


def test_legacy_expansion_without_scope_or_zero_write_proof_fails_closed_to_noop():
    base_trace = {
        "sourceAssistantTurnId": "turn_source",
        "sourceAssistantTurnRole": "assistant",
        "sourceAssistantTurnStatus": "active",
        "requestChoiceId": "choice_more_plans",
        "persistedChoiceId": "choice_more_plans",
        "resolvedChoiceId": "choice_more_plans",
        "executionChoiceId": "choice_more_plans",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "executionAction": "retry_model_planning",
        "executionRoute": "controller_choice_resume",
        "executionStatus": "succeeded",
        "outcome": {
            "reason": "new_verified_proposal",
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        },
    }
    payload = {
        "comparisonProjections": [
            {
                "proposalId": "proposal_new",
                "choiceId": "adopt_proposal_new",
                "sourceAssistantTurnId": "turn_result",
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
            }
        ],
        "choiceOptions": [
            {
                "id": "adopt_proposal_new",
                "comparisonProjection": {
                    "proposalId": "proposal_new",
                    "choiceId": "adopt_proposal_new",
                    "sourceAssistantTurnId": "turn_result",
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                },
            }
        ],
    }

    assert (
        comparison_projection_update_mode(
            payload,
            {**base_trace, "rootPortfolioId": ""},
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            payload,
            {**base_trace, "sourceAssistantTurnStatus": "superseded"},
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            payload,
            {**base_trace, "sourceAssistantTurnRole": "user"},
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            payload,
            {**base_trace, "executionChoiceId": "choice_foreign"},
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            payload,
            {
                **base_trace,
                "outcome": {**base_trace["outcome"], "versionDelta": 1},
            },
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            {
                "comparisonProjections": [
                    {
                        "proposalId": "proposal_foreign",
                        "sourceAssistantTurnId": "turn_result",
                        "planningSelectionRootTurnId": "turn_root",
                        "rootPortfolioId": "portfolio_foreign",
                    }
                ]
            },
            base_trace,
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            {
                "comparisonProjections": [
                    {
                        "proposalId": "proposal_stale_source",
                        "sourceAssistantTurnId": "turn_other",
                        "planningSelectionRootTurnId": "turn_root",
                        "rootPortfolioId": "portfolio_root",
                    }
                ]
            },
            base_trace,
            "turn_result",
        )
        is None
    )
    assert (
        comparison_projection_update_mode(
            payload,
            {
                **base_trace,
                "outcome": {"reason": "new_verified_proposal"},
            },
            "turn_result",
        )
        is None
    )


def test_non_expansion_projection_defaults_to_replace():
    assert (
        comparison_projection_update_mode(
            {"comparisonProjections": [{"proposalId": "proposal_final"}]},
            None,
            "turn_result",
        )
        == "replace"
    )


def test_expansion_projection_requires_matching_persisted_adoption_choice():
    projection = {
        "proposalId": "proposal_new",
        "choiceId": "adopt_proposal_new",
        "sourceAssistantTurnId": "turn_result",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
    }
    trace = {
        "sourceAssistantTurnId": "turn_source",
        "sourceAssistantTurnRole": "assistant",
        "sourceAssistantTurnStatus": "active",
        "requestChoiceId": "choice_more_plans",
        "persistedChoiceId": "choice_more_plans",
        "resolvedChoiceId": "choice_more_plans",
        "executionChoiceId": "choice_more_plans",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "executionAction": "retry_model_planning",
        "executionRoute": "controller_choice_resume",
        "executionStatus": "succeeded",
        "outcome": {
            "reason": "new_verified_proposal",
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        },
    }

    assert (
        comparison_projection_update_mode(
            {
                "comparisonProjections": [projection],
                "choiceOptions": [
                    {
                        "id": "foreign_adoption_choice",
                        "comparisonProjection": projection,
                    }
                ],
            },
            trace,
            "turn_result",
        )
        is None
    )


def test_grounded_partial_preview_expansion_appends_without_an_adoption_choice():
    active_partial = {
        "proposalId": "partial:portfolio_root",
        "choiceId": "adopt_active_partial",
        "sourceAssistantTurnId": "turn_result",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "isPartial": True,
        "adoptionReady": True,
    }
    read_only_partial = {
        "proposalId": "partial-preview:portfolio_root:brief_food",
        "choiceId": "preview_partial_portfolio_root_brief_food",
        "sourceAssistantTurnId": "turn_result",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "isPartial": True,
        "adoptionReady": False,
    }
    payload = {
        "comparisonProjectionUpdateMode": "append",
        "comparisonProjections": [active_partial, read_only_partial],
        "choiceOptions": [
            {
                "id": "adopt_active_partial",
                "comparisonProjection": active_partial,
            }
        ],
    }
    trace = {
        "sourceAssistantTurnId": "turn_source",
        "sourceAssistantTurnRole": "assistant",
        "sourceAssistantTurnStatus": "active",
        "requestChoiceId": "choice_more_plans",
        "persistedChoiceId": "choice_more_plans",
        "resolvedChoiceId": "choice_more_plans",
        "executionChoiceId": "choice_more_plans",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "executionAction": "retry_model_planning",
        "executionRoute": "controller_choice_resume",
        "executionStatus": "succeeded",
        "outcome": {
            "reason": "new_grounded_partial_preview",
            "qualifiedPartialProjectionIds": ["partial-preview:portfolio_root:brief_food"],
            "partialProjectionDelta": 1,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        },
    }

    assert comparison_projection_update_mode(payload, trace, "turn_result") == "append"
    assert (
        comparison_projection_update_mode(
            payload,
            {
                **trace,
                "outcome": {
                    **trace["outcome"],
                    "qualifiedPartialProjectionIds": [],
                    "partialProjectionDelta": 0,
                },
            },
            "turn_result",
        )
        is None
    )


def test_legacy_persisted_partial_preview_failure_recovers_append_only_with_exact_zero_write_delta():
    projection = {
        "proposalId": "partial-preview:portfolio_root:brief_food",
        "choiceId": "preview_partial_portfolio_root_brief_food",
        "sourceAssistantTurnId": "turn_result",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "isPartial": True,
        "adoptionReady": False,
    }
    payload = {
        "comparisonProjectionUpdateMode": "append",
        "comparisonProjections": [projection],
        "choiceOptions": [],
    }
    trace = {
        "sourceAssistantTurnId": "turn_source",
        "sourceAssistantTurnRole": "assistant",
        "sourceAssistantTurnStatus": "active",
        "requestChoiceId": "choice_more_plans",
        "persistedChoiceId": "choice_more_plans",
        "resolvedChoiceId": "choice_more_plans",
        "executionChoiceId": "choice_more_plans",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "executionAction": "retry_model_planning",
        "executionRoute": "controller_choice_resume",
        "executionStatus": "failed_retryable",
        "outcome": {
            "reason": "no_verified_proposal_delta",
            "newProposalIds": [projection["proposalId"]],
            "qualifiedPartialProjectionIds": [projection["proposalId"]],
            "partialProjectionDelta": 1,
            "visibleProposalIdsBefore": ["partial:portfolio_root"],
            "visibleProposalIdsAfter": ["partial:portfolio_root", projection["proposalId"]],
            "visibleProposalCountBefore": 1,
            "visibleProposalCountAfter": 2,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        },
    }

    assert comparison_projection_update_mode(payload, trace, "turn_result") == "append"
    tampered = {
        **trace,
        "outcome": {**trace["outcome"], "visibleProposalCountAfter": 3},
    }
    assert comparison_projection_update_mode(payload, tampered, "turn_result") is None


def test_simple_direction_append_uses_server_persisted_zero_write_delta_without_retry_trace():
    new_projection = {
        "proposalId": "proposal_b",
        "choiceId": "choice_b",
        "sourceAssistantTurnId": "turn_result",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "adoptionReady": False,
        "blockingReasons": ["simple_direction_pending_slot_lineage_conflict"],
    }
    payload = {
        "mode": "simple_open_direction_proposal",
        "workflowMode": "simple_direction_v1",
        "comparisonProjectionUpdateMode": "append",
        "comparisonProjections": [new_projection],
        # A remains confirmable and is intentionally reissued even when B is
        # blocked; the append proof must not require a B adoption choice.
        "choiceOptions": [
            {
                "id": "choice_a",
                "choiceId": "choice_a",
                "action": "select_plan_proposal",
                "proposalId": "proposal_a",
                "sourceAssistantTurnId": "turn_result",
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
            },
            {
                "id": "continue_root",
                "choiceId": "continue_root",
                "action": "manual_continuation",
                "sourceAssistantTurnId": "turn_result",
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
            },
        ],
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "visibleProposalCount": 2,
        "adoptionReadyProposalCount": 1,
        "proposalDelta": 1,
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
    }

    assert comparison_projection_update_mode(payload, None, "turn_result") == "append"
    assert (
        comparison_projection_update_mode(
            {**payload, "choiceOptions": []},
            None,
            "turn_result",
        )
        == "replace"
    )
    assert (
        comparison_projection_update_mode(
            {
                **payload,
                "choiceOptions": [
                    {
                        "sourceAssistantTurnId": "turn_result",
                        "planningSelectionRootTurnId": "turn_root",
                        "rootPortfolioId": "portfolio_root",
                    }
                ],
            },
            None,
            "turn_result",
        )
        == "replace"
    )
    assert (
        comparison_projection_update_mode(
            {**payload, "routeWriteDelta": 1},
            None,
            "turn_result",
        )
        == "replace"
    )
    assert (
        comparison_projection_update_mode(
            {**payload, "workflowMode": "strict_portfolio"},
            None,
            "turn_result",
        )
        == "replace"
    )

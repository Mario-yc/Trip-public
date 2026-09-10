import json
import sqlite3
from typing import Any, Optional


COMPARISON_EXPANSION_SUCCESS_REASONS = {
    "new_verified_proposal",
    "new_adoption_ready_partial_proposal",
    "new_route_pending_proposal",
    "new_grounded_partial_preview",
    "new_verified_partial_preview",
}
READ_ONLY_PARTIAL_EXPANSION_SUCCESS_REASONS = {
    "new_verified_proposal",
    "new_adoption_ready_partial_proposal",
    "new_route_pending_proposal",
    "new_grounded_partial_preview",
    "new_verified_partial_preview",
}


def comparison_projection_update_mode(
    response_payload: dict[str, Any],
    structured_choice_trace: Optional[dict[str, Any]],
    current_turn_id: str,
) -> Optional[str]:
    """Return the persisted comparison collection semantics for one assistant turn.

    Expansion turns intentionally contain only the newly verified projections.
    Older stored turns predate the explicit mode, so infer append only from a
    successful persisted more-plans execution; all other projection payloads
    retain the existing authoritative replacement behavior.
    """

    projections = response_payload.get("comparisonProjections")
    if not isinstance(projections, list) or not projections:
        return None
    choice_options = response_payload.get("choiceOptions")
    persisted_projection_choices = choice_options if isinstance(choice_options, list) else []
    simple_direction_projection_choices = [
        option
        for option in persisted_projection_choices
        if isinstance(option, dict) and option.get("action") == "select_plan_proposal"
    ]
    simple_direction_root_id = response_payload.get("rootPortfolioId")
    simple_direction_planning_root_id = response_payload.get("planningSelectionRootTurnId")
    simple_direction_adoption_ready_count = response_payload.get("adoptionReadyProposalCount")
    simple_direction_zero_write = all(
        isinstance(response_payload.get(field), int)
        and not isinstance(response_payload.get(field), bool)
        and response_payload.get(field) == 0
        for field in ("versionDelta", "patchDelta", "routeWriteDelta")
    )
    simple_direction_append = bool(
        response_payload.get("comparisonProjectionUpdateMode") == "append"
        and response_payload.get("workflowMode") == "simple_direction_v1"
        and response_payload.get("mode") == "simple_open_direction_proposal"
        and response_payload.get("proposalDelta") == 1
        and isinstance(response_payload.get("visibleProposalCount"), int)
        and not isinstance(response_payload.get("visibleProposalCount"), bool)
        and response_payload.get("visibleProposalCount") >= 2
        and isinstance(simple_direction_adoption_ready_count, int)
        and not isinstance(simple_direction_adoption_ready_count, bool)
        and simple_direction_adoption_ready_count >= 0
        and len(projections) == 1
        and simple_direction_zero_write
        and isinstance(simple_direction_root_id, str)
        and bool(simple_direction_root_id.strip())
        and isinstance(simple_direction_planning_root_id, str)
        and bool(simple_direction_planning_root_id.strip())
        and all(
            isinstance(projection, dict)
            and projection.get("sourceAssistantTurnId") == current_turn_id
            and projection.get("planningSelectionRootTurnId") == simple_direction_planning_root_id
            and projection.get("rootPortfolioId") == simple_direction_root_id
            and isinstance(projection.get("proposalId"), str)
            and bool(projection.get("proposalId", "").strip())
            and isinstance(projection.get("choiceId"), str)
            and bool(projection.get("choiceId", "").strip())
            for projection in projections
        )
        and len(simple_direction_projection_choices) == simple_direction_adoption_ready_count
        and all(
            isinstance(option, dict)
            and option.get("action") == "select_plan_proposal"
            and isinstance(option.get("id"), str)
            and bool(option.get("id", "").strip())
            and option.get("choiceId") == option.get("id")
            and isinstance(option.get("proposalId"), str)
            and bool(option.get("proposalId", "").strip())
            and option.get("sourceAssistantTurnId") == current_turn_id
            and option.get("planningSelectionRootTurnId") == simple_direction_planning_root_id
            and option.get("rootPortfolioId") == simple_direction_root_id
            and not any(
                projection.get("proposalId") == option.get("proposalId")
                and projection.get("adoptionReady") is not True
                for projection in projections
                if isinstance(projection, dict)
            )
            for option in simple_direction_projection_choices
        )
    )
    if simple_direction_append:
        # This evidence is written by the Simple Direction proposal producer,
        # not accepted from a client request.  The new-card payload is append
        # only when the same persisted turn proves one proposal and no formal
        # itinerary/version/patch/route mutation.
        return "append"
    trace = structured_choice_trace if isinstance(structured_choice_trace, dict) else {}
    outcome = trace.get("outcome") if isinstance(trace.get("outcome"), dict) else {}
    scope_identity = (
        trace.get("sourceAssistantTurnId"),
        trace.get("resolvedChoiceId"),
        trace.get("planningSelectionRootTurnId"),
        trace.get("rootPortfolioId"),
    )
    choice_identity = (
        trace.get("requestChoiceId"),
        trace.get("persistedChoiceId"),
        trace.get("resolvedChoiceId"),
        trace.get("executionChoiceId"),
    )
    reason = outcome.get("reason")
    qualified_partial_projection_ids_raw = outcome.get("qualifiedPartialProjectionIds")
    qualified_partial_projection_ids = (
        {value for value in qualified_partial_projection_ids_raw if isinstance(value, str) and value.strip()}
        if isinstance(qualified_partial_projection_ids_raw, list)
        else set()
    )
    synthetic_partial_projection_ids = {
        projection.get("proposalId")
        for projection in projections
        if isinstance(projection, dict)
        and projection.get("isPartial") is True
        and projection.get("adoptionReady") is False
        and isinstance(projection.get("proposalId"), str)
        and projection.get("proposalId", "").strip()
        and (
            isinstance(projection.get("partialPreviewEvidence"), dict)
            or projection.get("proposalId", "").startswith("partial-preview:")
        )
    }
    partial_projection_delta = outcome.get("partialProjectionDelta")
    qualified_read_only_partial_evidence = not synthetic_partial_projection_ids or (
        reason in READ_ONLY_PARTIAL_EXPANSION_SUCCESS_REASONS
        and isinstance(qualified_partial_projection_ids_raw, list)
        and len(qualified_partial_projection_ids_raw) == len(qualified_partial_projection_ids)
        and qualified_partial_projection_ids == synthetic_partial_projection_ids
        and isinstance(partial_projection_delta, int)
        and not isinstance(partial_projection_delta, bool)
        and partial_projection_delta == len(synthetic_partial_projection_ids)
    )
    projection_scope_matches = all(
        isinstance(projection, dict)
        and projection.get("sourceAssistantTurnId") == current_turn_id
        and projection.get("planningSelectionRootTurnId") == trace.get("planningSelectionRootTurnId")
        and projection.get("rootPortfolioId") == trace.get("rootPortfolioId")
        and isinstance(projection.get("choiceId"), str)
        and bool(projection.get("choiceId", "").strip())
        and (
            (
                projection.get("isPartial") is True
                and projection.get("adoptionReady") is False
                and projection.get("proposalId") in synthetic_partial_projection_ids
            )
            or any(
                isinstance(option, dict)
                and option.get("id") == projection.get("choiceId")
                and isinstance(option.get("comparisonProjection"), dict)
                and all(
                    option["comparisonProjection"].get(field) == projection.get(field)
                    for field in (
                        "proposalId",
                        "choiceId",
                        "sourceAssistantTurnId",
                        "planningSelectionRootTurnId",
                        "rootPortfolioId",
                    )
                )
                for option in persisted_projection_choices
            )
        )
        for projection in projections
    )
    zero_write_evidence = all(
        isinstance(outcome.get(field), int) and not isinstance(outcome.get(field), bool) and outcome.get(field) == 0
        for field in ("versionDelta", "patchDelta", "routeWriteDelta")
    )
    common_append_evidence = bool(
        trace.get("executionAction") == "retry_model_planning"
        and trace.get("executionRoute") == "controller_choice_resume"
        and all(isinstance(value, str) and value.strip() for value in scope_identity)
        and trace.get("sourceAssistantTurnStatus") == "active"
        and trace.get("sourceAssistantTurnRole") == "assistant"
        and all(isinstance(value, str) and value.strip() for value in choice_identity)
        and len(set(choice_identity)) == 1
        and projection_scope_matches
        and zero_write_evidence
    )
    normal_append = bool(
        trace.get("executionStatus") == "succeeded"
        and reason in COMPARISON_EXPANSION_SUCCESS_REASONS
        and qualified_read_only_partial_evidence
    )

    def strict_string_set(value: Any) -> Optional[set[str]]:
        if not isinstance(value, list):
            return None
        normalized = [item for item in value if isinstance(item, str) and item.strip()]
        if len(normalized) != len(value) or len(normalized) != len(set(normalized)):
            return None
        return set(normalized)

    legacy_new_ids = strict_string_set(outcome.get("newProposalIds"))
    legacy_before_ids = strict_string_set(outcome.get("visibleProposalIdsBefore"))
    legacy_after_ids = strict_string_set(outcome.get("visibleProposalIdsAfter"))
    legacy_partial_append = bool(
        response_payload.get("comparisonProjectionUpdateMode") == "append"
        and trace.get("executionStatus") == "failed_retryable"
        and reason == "no_verified_proposal_delta"
        and synthetic_partial_projection_ids
        and legacy_new_ids == synthetic_partial_projection_ids
        and qualified_partial_projection_ids == synthetic_partial_projection_ids
        and partial_projection_delta == len(synthetic_partial_projection_ids)
        and legacy_before_ids is not None
        and legacy_after_ids is not None
        and legacy_before_ids.issubset(legacy_after_ids)
        and legacy_after_ids - legacy_before_ids == synthetic_partial_projection_ids
        and outcome.get("visibleProposalCountBefore") == len(legacy_before_ids)
        and outcome.get("visibleProposalCountAfter") == len(legacy_after_ids)
        and len(legacy_after_ids) == len(legacy_before_ids) + len(synthetic_partial_projection_ids)
    )
    if common_append_evidence and (normal_append or legacy_partial_append):
        return "append"
    if (
        trace.get("executionAction") == "retry_model_planning"
        and trace.get("executionRoute") == "controller_choice_resume"
    ):
        # Attempted expansion material is not an authoritative replacement.
        return None
    return "replace"


def build_structured_choice_trace(
    db: sqlite3.Connection,
    row: sqlite3.Row,
) -> Optional[dict[str, Any]]:
    request_payload = _json_dict(row["agent_request_json"])
    selected = (
        request_payload.get("selectedAgentChoice")
        if isinstance(request_payload.get("selectedAgentChoice"), dict)
        else {}
    )
    request_choice_id = str(selected.get("requestChoiceId") or selected.get("choiceId") or "")
    source_turn_id = str(selected.get("sourceAssistantTurnId") or "")
    if not request_choice_id or not source_turn_id:
        return None
    persisted_choice_id = str(selected.get("persistedChoiceId") or request_choice_id)
    selected_option = selected.get("option") if isinstance(selected.get("option"), dict) else {}
    execution = db.execute(
        """
        SELECT id, choice_id, action, status, result_version_id,
               checkpoint_fingerprint, continuation_json, outcome_json
        FROM agent_choice_executions
        WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?
        """,
        (row["session_id"], source_turn_id, persisted_choice_id),
    ).fetchone()
    identity_lookup_status = "legacy_exact"
    from src.services.conversation_operation_identity import ConversationOperationIdentity
    if selected_option.get("action") in ConversationOperationIdentity.ACTIONS:
        identities = ConversationOperationIdentity(db)
        requirement = request_payload.get("guideContinuationRequirement") or {}
        try:
            execution = identities.execution(str(row["session_id"]), source_turn_id, persisted_choice_id,
                guide_evidence=requirement.get("evidenceFingerprint"))
            identity_lookup_status = "canonical_resolved"
        except (ValueError, KeyError):
            execution = None
            identity_lookup_status = "identity_lookup_incomplete"
    source_turn = db.execute(
        "SELECT role, status, agent_response_json FROM conversation_turns WHERE session_id = ? AND id = ?",
        (row["session_id"], source_turn_id),
    ).fetchone()
    selected_option = selected.get("option") if isinstance(selected.get("option"), dict) else {}
    is_clarification_batch = bool(
        str(selected.get("persistedChoiceAction") or selected_option.get("action") or "")
        == "submit_clarification_batch"
        or (
            execution is not None
            and str(execution["action"] or "") == "submit_clarification_batch"
        )
    )
    clarification_checkpoint_id: Optional[str] = None
    clarification_checkpoint_fingerprint: Optional[str] = None
    if is_clarification_batch:
        # The request-selected option, the original assistant response, and
        # the execution claim must all identify the same *source* checkpoint.
        # Result/advanced checkpoints are deliberately not consulted.
        source_payload = _json_dict(source_turn["agent_response_json"]) if source_turn is not None else {}
        source_checkpoint = (
            source_payload.get("clarificationCheckpoint")
            if isinstance(source_payload.get("clarificationCheckpoint"), dict)
            else {}
        )
        source_options = source_payload.get("choiceOptions")
        source_options = source_options if isinstance(source_options, list) else []
        matching_options = [
            option
            for option in source_options
            if isinstance(option, dict)
            and str(option.get("id") or "") == persisted_choice_id
            and str(option.get("kind") or "") == "clarification_batch_submit"
            and str(option.get("action") or "") == "submit_clarification_batch"
        ]
        option_checkpoint_id = str(selected_option.get("checkpointId") or "")
        option_checkpoint_fingerprint = str(selected_option.get("checkpointFingerprint") or "")
        source_checkpoint_id = str(source_checkpoint.get("checkpointId") or "")
        source_checkpoint_fingerprint = str(source_checkpoint.get("fingerprint") or "")
        if (
            len(matching_options) != 1
            or not option_checkpoint_id
            or not option_checkpoint_fingerprint
            or not source_checkpoint_id
            or not source_checkpoint_fingerprint
            or source_turn is None
            or str(source_turn["role"] or "") != "assistant"
            or str(source_checkpoint.get("status") or "") != "awaiting_answer"
            or str(source_checkpoint.get("sourceAssistantTurnId") or "") != source_turn_id
            or str(selected_option.get("sourceAssistantTurnId") or "") != source_turn_id
            or str(matching_options[0].get("sourceAssistantTurnId") or "") != source_turn_id
            or option_checkpoint_id != source_checkpoint_id
            or option_checkpoint_fingerprint != source_checkpoint_fingerprint
            or str(matching_options[0].get("checkpointId") or "") != source_checkpoint_id
            or str(matching_options[0].get("checkpointFingerprint") or "") != source_checkpoint_fingerprint
            or execution is None
            or str(execution["action"] or "") != "submit_clarification_batch"
            or str(execution["checkpoint_fingerprint"] or "") != source_checkpoint_fingerprint
        ):
            return None
        clarification_checkpoint_id = source_checkpoint_id
        clarification_checkpoint_fingerprint = source_checkpoint_fingerprint
    execution_continuation = _json_dict(execution["continuation_json"]) if execution is not None else {}
    request_continuation = (
        request_payload.get("portfolioDensityContinuation")
        if isinstance(request_payload.get("portfolioDensityContinuation"), dict)
        else {}
    )
    continuation = execution_continuation or request_continuation
    outcome = _json_dict(execution["outcome_json"]) if execution is not None else {}
    partial_expansion = str(continuation.get("kind") or "") == "expand_partial_portfolio"
    runtime = continuation.get("runtime") if isinstance(continuation.get("runtime"), dict) else {}
    decision_state = (
        request_payload.get("agentDecisionState") if isinstance(request_payload.get("agentDecisionState"), dict) else {}
    )
    return {
        "identityLookupStatus": identity_lookup_status,
        "sourceAssistantTurnId": source_turn_id,
        "sourceAssistantTurnRole": source_turn["role"] if source_turn is not None else None,
        "sourceAssistantTurnStatus": source_turn["status"] if source_turn is not None else None,
        "outboundRequest": {
            "sourceAssistantTurnId": source_turn_id,
            "choiceId": request_choice_id,
            **({"manualValue": "[REDACTED_USER_INPUT]"} if selected.get("manualValue") else {}),
        },
        "requestChoiceId": request_choice_id,
        "persistedChoiceId": persisted_choice_id,
        "persistedChoiceAction": selected.get("persistedChoiceAction"),
        "resolvedChoiceId": persisted_choice_id,
        "resolvedAction": selected.get("persistedChoiceAction"),
        "executionId": execution["id"] if execution is not None else selected.get("executionId"),
        "executionChoiceId": execution["choice_id"] if execution is not None else selected.get("executionChoiceId"),
        "executionAction": execution["action"] if execution is not None else selected.get("executionAction"),
        "executionStatus": execution["status"] if execution is not None else None,
        "resultVersionId": execution["result_version_id"] if execution is not None else None,
        "planningSelectionRootTurnId": outcome.get("planningSelectionRootTurnId")
        or continuation.get("planningSelectionRootTurnId")
        or request_payload.get("planningSelectionRootTurnId"),
        "rootPortfolioId": outcome.get("rootPortfolioId")
        or continuation.get("rootPortfolioId")
        or request_payload.get("rootPortfolioId"),
        "requestContractFingerprint": outcome.get("requestContractFingerprint")
        or continuation.get("requestContractFingerprint")
        or request_payload.get("requestContractFingerprint"),
        "expectedBaseVersionId": continuation.get("activeVersionId")
        or continuation.get("expectedBaseVersionId")
        or request_payload.get("expectedBaseVersionId"),
        "versionDelta": int(outcome.get("versionDelta") or 0) if outcome else None,
        "patchDelta": int(outcome.get("patchDelta") or 0) if outcome else None,
        "routeWriteDelta": int(outcome.get("routeWriteDelta") or 0) if outcome else None,
        "normalizedAction": continuation.get("normalizedAction") or selected.get("normalizedAction"),
        "structuredPlanningChoiceResume": bool(continuation),
        "executionRoute": (
            "controller_choice_resume" if partial_expansion else "portfolio_density_resume" if continuation else None
        ),
        "controllerCalled": (
            bool(decision_state.get("controllerCalled"))
            if (continuation and "controllerCalled" in decision_state) or partial_expansion
            else False
            if continuation
            else None
        ),
        "controlOwner": decision_state.get("controlOwner")
        or (
            "agent_turn_coordinator"
            if partial_expansion
            else "portfolio_density_continuation"
            if continuation
            else None
        ),
        "checkpointFingerprint": execution["checkpoint_fingerprint"]
        if execution is not None
        else continuation.get("checkpointFingerprint"),
        **({"checkpointId": clarification_checkpoint_id} if clarification_checkpoint_id else {}),
        **(
            {"checkpointFingerprint": clarification_checkpoint_fingerprint}
            if clarification_checkpoint_fingerprint
            else {}
        ),
        "runtimeBuildId": runtime.get("runtimeBuildId"),
        "runtimeStartedAt": runtime.get("runtimeStartedAt"),
        "outcome": outcome or None,
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

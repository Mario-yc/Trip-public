from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService


class SimpleDirectionExecutionEvidenceService:
    """Verify and reconcile zero-write Simple Direction choice executions.

    The execution row is not sufficient proof that a candidate frontier was
    consumed.  Completion is derived from persisted choice, root, frontier,
    assistant-result, and zero-write evidence owned by the server.
    """

    WORKFLOW_MODE = "simple_direction_v1"
    CHOICE_KIND = "simple_direction_more_plans"
    ACTION = "continue_plan_expansion"
    INITIAL_RETRY_CHOICE_KIND = "safe_fallback_action"
    INITIAL_RETRY_ACTION = "retry_model_planning"
    PROVIDER_PENDING_REASONS = frozenset(
        {
            "simple_direction_frontier_provider_pending",
            "simple_direction_frontier_provider_schema_pending",
        }
    )

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    @staticmethod
    def current_frontier_execution_id(
        *,
        pipeline_execution_id: Any,
        response_execution_id: Any,
        attempt_consumed: bool,
    ) -> Optional[str]:
        """Bind response lineage to the current server-owned execution."""

        pipeline_identity = str(pipeline_execution_id or "").strip()
        response_identity = str(response_execution_id or "").strip()
        if pipeline_identity and response_identity and pipeline_identity != response_identity:
            raise ValueError("simple_direction_frontier_execution_identity_mismatch")
        resolved = response_identity or pipeline_identity
        if attempt_consumed and not resolved:
            raise ValueError("simple_direction_frontier_execution_identity_missing")
        return resolved or None

    def verify(
        self,
        execution_id: str,
        *,
        assistant_turn_id: Optional[str] = None,
        expected_request_turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        execution = self.db.execute(
            "SELECT * FROM agent_choice_executions WHERE id = ?",
            (str(execution_id or ""),),
        ).fetchone()
        if execution is None:
            return self._failure("simple_direction_execution_missing")
        execution = dict(execution)
        if str(execution.get("action") or "") != self.ACTION:
            return self._failure("simple_direction_execution_action_mismatch")
        session_id = str(execution.get("session_id") or "")
        request_turn_id = str(execution.get("request_turn_id") or "")
        if expected_request_turn_id and request_turn_id != str(expected_request_turn_id):
            return self._failure("simple_direction_execution_request_turn_mismatch")
        continuation = self._json_dict(execution.get("continuation_json"))
        guide_requirement = (
            continuation.get("guideContinuationRequirement")
            if isinstance(continuation.get("guideContinuationRequirement"), dict)
            else {}
        )
        if guide_requirement and (
            str(guide_requirement.get("schemaVersion") or "") != "guide-continuation-requirement-v1"
            or not str(guide_requirement.get("requirementFingerprint") or "")
            or str(guide_requirement.get("requirementFingerprint") or "")
            != GuideContinuationRequirementService.requirement_fingerprint(guide_requirement)
        ):
            return self._failure("simple_direction_execution_guide_requirement_invalid")

        source_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (str(execution.get("source_turn_id") or ""), session_id),
        ).fetchone()
        request_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'user' AND status != 'superseded'""",
            (request_turn_id, session_id),
        ).fetchone()
        result_turn_id = str(assistant_turn_id or execution.get("execution_turn_id") or "")
        result_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (result_turn_id, session_id),
        ).fetchone()
        if source_turn is None or request_turn is None or result_turn is None:
            return self._failure("simple_direction_execution_turn_lineage_missing")

        source_payload = self._json_dict(source_turn["agent_response_json"])
        from src.services.conversation_operation_identity import ConversationOperationIdentity
        try:
            source_option = ConversationOperationIdentity(self.db).source_option_for_execution(execution)
        except (ValueError, KeyError):
            source_option = None
        if not isinstance(source_option, dict):
            return self._failure("simple_direction_execution_source_choice_missing")
        if (
            str(source_option.get("kind") or "") != self.CHOICE_KIND
            or str(source_option.get("action") or "") != self.ACTION
            or str(source_option.get("sourceAssistantTurnId") or source_turn["id"]) != str(source_turn["id"])
        ):
            return self._failure("simple_direction_execution_source_choice_mismatch")
        source_user_turn_id = str(source_option.get("sourceUserTurnId") or "")
        if source_user_turn_id and source_user_turn_id != str(execution.get("source_user_turn_id") or ""):
            return self._failure("simple_direction_execution_source_user_turn_mismatch")

        planning_root_id = str(source_option.get("planningSelectionRootTurnId") or "")
        portfolio_id = str(source_option.get("rootPortfolioId") or "")
        request_fingerprint = str(source_option.get("requestContractFingerprint") or "")
        if not planning_root_id or not portfolio_id or not request_fingerprint:
            return self._failure("simple_direction_execution_choice_scope_missing")
        portfolio = self.db.execute(
            "SELECT * FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
            (portfolio_id, session_id),
        ).fetchone()
        if portfolio is None:
            return self._failure("simple_direction_execution_portfolio_missing")
        if (
            str(portfolio["status"] or "") != "awaiting_selection"
            or str(portfolio["source_user_turn_id"] or "") != planning_root_id
            or str(portfolio["request_contract_fingerprint"] or "") != request_fingerprint
        ):
            return self._failure("simple_direction_execution_portfolio_scope_mismatch")
        summary = self._json_dict(portfolio["summary_json"])
        if (
            str(summary.get("workflowMode") or "") != self.WORKFLOW_MODE
            or str(summary.get("portfolioId") or portfolio_id) != portfolio_id
            or str(summary.get("planningSelectionRootTurnId") or planning_root_id) != planning_root_id
            or str(summary.get("requestContractFingerprint") or request_fingerprint) != request_fingerprint
        ):
            return self._failure("simple_direction_execution_root_identity_mismatch")
        attempts = summary.get("simpleDirectionFrontierAttempts")
        frontier_attempt_record = attempts.get(execution_id) if isinstance(attempts, dict) else None
        compatibility_attempts = summary.get("simpleDirectionCompatibilityAttempts")
        compatibility_attempt_record = (
            compatibility_attempts.get(execution_id) if isinstance(compatibility_attempts, dict) else None
        )
        attempt_kind = "frontier"
        attempt_record = frontier_attempt_record
        if not isinstance(attempt_record, dict) and isinstance(compatibility_attempt_record, dict):
            attempt_kind = "compatibility"
            attempt_record = compatibility_attempt_record
        if (
            not isinstance(attempt_record, dict)
            or str(attempt_record.get("executionId") or "") != str(execution_id)
            or str(attempt_record.get("requestContractFingerprint") or "") != request_fingerprint
            or str(attempt_record.get("status") or "") not in {"reconciled", "no_progress"}
        ):
            return self._failure("simple_direction_execution_frontier_not_reconciled")
        if attempt_kind == "compatibility" and (
            str(attempt_record.get("schemaVersion") or "")
            not in {
                "simple-direction-compatibility-attempt-v1",
                "simple-direction-compatibility-attempt-v2",
                "simple-direction-compatibility-attempt-v3",
            }
            or str(attempt_record.get("sourceAssistantTurnId") or "") != str(source_turn["id"])
            or str(attempt_record.get("requestTurnId") or "") != request_turn_id
            or str(attempt_record.get("choiceId") or "") != str(execution.get("choice_id") or "")
            or str(attempt_record.get("planningSelectionRootTurnId") or "") != planning_root_id
            or str(attempt_record.get("rootPortfolioId") or "") != portfolio_id
            or str(attempt_record.get("resultAssistantTurnId") or "") != result_turn_id
            or str(attempt_record.get("attemptFingerprint") or "")
            != PlanPortfolioStore.simple_direction_compatibility_attempt_fingerprint(attempt_record)
        ):
            return self._failure("simple_direction_execution_compatibility_attempt_mismatch")

        response_payload = self._json_dict(result_turn["agent_response_json"])
        if (
            str(response_payload.get("mode") or "") != "simple_open_direction_proposal"
            or str(response_payload.get("workflowMode") or "") != self.WORKFLOW_MODE
            or str(response_payload.get("planningSelectionRootTurnId") or "") != planning_root_id
            or str(response_payload.get("rootPortfolioId") or "") != portfolio_id
            or str(response_payload.get("frontierExecutionId") or "") != str(execution_id)
            or response_payload.get("frontierAttemptConsumed") is not True
        ):
            return self._failure("simple_direction_execution_assistant_lineage_mismatch")
        reason_code = str(response_payload.get("reasonCode") or "")
        if reason_code in self.PROVIDER_PENDING_REASONS or self._contains_provider_failure(response_payload):
            return self._failure("simple_direction_execution_provider_failure")
        if any(int(response_payload.get(key) or 0) != 0 for key in ("versionDelta", "patchDelta", "routeWriteDelta")):
            return self._failure("simple_direction_execution_nonzero_write_delta")
        if str(execution.get("result_version_id") or ""):
            return self._failure("simple_direction_execution_result_version_present")
        if self._formal_write_count(session_id, request_turn_id, result_turn_id) != 0:
            return self._failure("simple_direction_execution_formal_write_present")

        proposal_delta = int(response_payload.get("proposalDelta") or 0)
        guide_usage = (
            response_payload.get("guideEvidenceUsage")
            if isinstance(response_payload.get("guideEvidenceUsage"), dict)
            else {}
        )
        if guide_requirement:
            required_minimum = max(
                1,
                int(guide_requirement.get("minimumNovelGroundedPlaceCount") or 1),
            )
            used_places = [item for item in guide_usage.get("usedPlaces") or [] if isinstance(item, dict)]
            guide_usage_valid = bool(
                str(guide_usage.get("schemaVersion") or "") == "guide-evidence-usage-v1"
                and str(guide_usage.get("evidenceFingerprint") or "")
                == str(guide_requirement.get("evidenceFingerprint") or "")
                and str(guide_usage.get("requirementFingerprint") or "")
                == str(guide_requirement.get("requirementFingerprint") or "")
                and int(guide_usage.get("requiredMinimum") or 0) == required_minimum
                and (
                    (
                        proposal_delta > 0
                        and str(guide_usage.get("status") or "") == "satisfied"
                        and len(used_places) >= required_minimum
                        and all(item.get("routeVerified") is True for item in used_places)
                    )
                    or (
                        proposal_delta == 0
                        and str(guide_usage.get("status") or "") == "unsatisfied"
                        and len(used_places) < required_minimum
                    )
                )
            )
            if not guide_usage_valid:
                return self._failure("simple_direction_execution_guide_usage_mismatch")
        frontier_status = str(response_payload.get("frontierStatus") or "")
        expected_proposal_id = (
            str(attempt_record.get("proposalId") or "") or None if attempt_kind == "compatibility" else None
        )
        expected_attempt_fingerprint = (
            str(attempt_record.get("attemptFingerprint") or "") if attempt_kind == "compatibility" else None
        )
        if attempt_kind == "compatibility" and (
            int(attempt_record.get("proposalDelta") or 0) != proposal_delta
            or str(attempt_record.get("frontierStatus") or "") != frontier_status
        ):
            return self._failure("simple_direction_execution_compatibility_result_mismatch")
        if proposal_delta > 0 and not self._proposal_lineage_exists(
            portfolio_id=portfolio_id,
            execution_id=str(execution_id),
            assistant_turn_id=result_turn_id,
            request_fingerprint=request_fingerprint,
            expected_proposal_id=expected_proposal_id,
            expected_attempt_fingerprint=expected_attempt_fingerprint,
            guide_requirement=guide_requirement,
            expected_guide_usage=guide_usage,
        ):
            return self._failure("simple_direction_execution_proposal_lineage_missing")
        continuation_choices = [
            option
            for option in response_payload.get("choiceOptions") or []
            if isinstance(option, dict)
            and str(option.get("kind") or "") == self.CHOICE_KIND
            and str(option.get("action") or "") == self.ACTION
            and str(option.get("planningSelectionRootTurnId") or "") == planning_root_id
            and str(option.get("rootPortfolioId") or "") == portfolio_id
            and str(option.get("requestContractFingerprint") or "") == request_fingerprint
            and str(option.get("sourceAssistantTurnId") or "") == result_turn_id
            and str(option.get("sourceUserTurnId") or "") == planning_root_id
            and str(option.get("id") or "") != str(execution.get("choice_id") or "")
        ]
        if frontier_status == "has_more" and len(continuation_choices) != 1:
            return self._failure("simple_direction_execution_next_choice_missing")

        return {
            "passed": True,
            "reason": "simple_direction_execution_evidence_verified",
            "sessionId": session_id,
            "requestTurnId": request_turn_id,
            "assistantTurnId": result_turn_id,
            "sourceAssistantTurnId": str(source_turn["id"]),
            "choiceId": str(execution.get("choice_id") or ""),
            "planningSelectionRootTurnId": planning_root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": request_fingerprint,
            "frontierExecutionId": str(execution_id),
            "frontierAttemptFingerprint": str(
                attempt_record.get("attemptFingerprint")
                or (
                    (
                        (attempt_record.get("attempt") or {}) if isinstance(attempt_record.get("attempt"), dict) else {}
                    ).get("attemptFingerprint")
                )
                or ""
            ),
            "attemptKind": attempt_kind,
            "frontierStatus": frontier_status,
            "proposalDelta": proposal_delta,
            **(
                {
                    "guideContinuationRequirementFingerprint": str(
                        guide_requirement.get("requirementFingerprint") or ""
                    ),
                    "guideEvidenceUsage": copy.deepcopy(guide_usage),
                }
                if guide_requirement
                else {}
            ),
            "nextChoiceId": str(continuation_choices[0].get("id") or "") if continuation_choices else None,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "zeroWrite": True,
        }

    def verify_initial_retry(
        self,
        execution_id: str,
        *,
        assistant_turn_id: Optional[str] = None,
        expected_request_turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Verify a retry that produced the first persisted Simple Direction.

        A Controller retry option exists before a Simple Direction root, so it
        cannot carry a portfolio/frontier capability.  Success is therefore
        derived after execution from the persisted opaque source option, the
        exact execution/request/result turn lineage, the newly created root,
        and exactly one proposal whose generation lineage is bound to the
        result assistant turn.  Client-shaped proposal counters or request
        authorization booleans are never sufficient evidence.
        """

        execution_row = self.db.execute(
            "SELECT * FROM agent_choice_executions WHERE id = ?",
            (str(execution_id or ""),),
        ).fetchone()
        if execution_row is None:
            return self._failure("simple_direction_initial_execution_missing")
        execution = dict(execution_row)
        if str(execution.get("action") or "") != self.INITIAL_RETRY_ACTION:
            return self._failure("simple_direction_initial_execution_action_mismatch")
        session_id = str(execution.get("session_id") or "")
        request_turn_id = str(execution.get("request_turn_id") or "")
        if expected_request_turn_id and request_turn_id != str(expected_request_turn_id):
            return self._failure("simple_direction_initial_execution_request_turn_mismatch")

        source_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (str(execution.get("source_turn_id") or ""), session_id),
        ).fetchone()
        request_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'user' AND status != 'superseded'""",
            (request_turn_id, session_id),
        ).fetchone()
        result_turn_id = str(assistant_turn_id or execution.get("execution_turn_id") or "")
        result_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (result_turn_id, session_id),
        ).fetchone()
        if source_turn is None or request_turn is None or result_turn is None:
            return self._failure("simple_direction_initial_execution_turn_lineage_missing")

        source_payload = self._json_dict(source_turn["agent_response_json"])
        matching_source_options = [
            copy.deepcopy(option)
            for option in source_payload.get("choiceOptions") or []
            if isinstance(option, dict) and str(option.get("id") or "") == str(execution.get("choice_id") or "")
        ]
        if len(matching_source_options) != 1:
            return self._failure("simple_direction_initial_execution_source_choice_missing")
        source_option = matching_source_options[0]
        source_user_turn_id = str(source_option.get("sourceUserTurnId") or "")
        if (
            str(source_option.get("kind") or "") != self.INITIAL_RETRY_CHOICE_KIND
            or str(source_option.get("action") or "") != self.INITIAL_RETRY_ACTION
            or not source_user_turn_id
            or source_user_turn_id != str(execution.get("source_user_turn_id") or "")
        ):
            return self._failure("simple_direction_initial_execution_source_choice_mismatch")

        request_payload = self._json_dict(request_turn["agent_request_json"])
        selected_choice = (
            request_payload.get("selectedAgentChoice")
            if isinstance(request_payload.get("selectedAgentChoice"), dict)
            else {}
        )
        selected_option = selected_choice.get("option") if isinstance(selected_choice.get("option"), dict) else {}
        claim = (
            request_payload.get("fallbackChoiceExecutionClaim")
            if isinstance(request_payload.get("fallbackChoiceExecutionClaim"), dict)
            else {}
        )
        choice_id = str(execution.get("choice_id") or "")
        source_turn_id = str(source_turn["id"])
        if (
            str(claim.get("id") or "") != str(execution_id)
            or str(selected_choice.get("sourceAssistantTurnId") or "") != source_turn_id
            or str(selected_choice.get("choiceId") or "") != choice_id
            or str(selected_choice.get("persistedChoiceId") or "") != choice_id
            or str(selected_choice.get("action") or "") != self.INITIAL_RETRY_ACTION
            or str(selected_choice.get("persistedChoiceAction") or "") != self.INITIAL_RETRY_ACTION
            or str(selected_option.get("id") or "") != choice_id
            or str(selected_option.get("kind") or "") != self.INITIAL_RETRY_CHOICE_KIND
            or str(selected_option.get("action") or "") != self.INITIAL_RETRY_ACTION
        ):
            return self._failure("simple_direction_initial_execution_request_lineage_mismatch")

        response_payload = self._json_dict(result_turn["agent_response_json"])
        planning_root_id = str(response_payload.get("planningSelectionRootTurnId") or "")
        portfolio_id = str(response_payload.get("rootPortfolioId") or "")
        request_fingerprint = str(response_payload.get("requestContractFingerprint") or "")
        proposal_delta = int(response_payload.get("proposalDelta") or 0)
        if (
            str(response_payload.get("mode") or "") != "simple_open_direction_proposal"
            or str(response_payload.get("workflowMode") or "") != self.WORKFLOW_MODE
            or not planning_root_id
            or planning_root_id != source_user_turn_id
            or not portfolio_id
            or not request_fingerprint
            or str(response_payload.get("frontierExecutionId") or "")
            or proposal_delta not in {0, 1}
        ):
            return self._failure("simple_direction_initial_execution_assistant_lineage_mismatch")
        reason_code = str(response_payload.get("reasonCode") or "")
        if reason_code in self.PROVIDER_PENDING_REASONS or self._contains_provider_failure(response_payload):
            return self._failure("simple_direction_initial_execution_provider_failure")
        if any(int(response_payload.get(key) or 0) != 0 for key in ("versionDelta", "patchDelta", "routeWriteDelta")):
            return self._failure("simple_direction_initial_execution_nonzero_write_delta")
        if str(execution.get("result_version_id") or ""):
            return self._failure("simple_direction_initial_execution_result_version_present")
        if self._formal_write_count(session_id, request_turn_id, result_turn_id) != 0:
            return self._failure("simple_direction_initial_execution_formal_write_present")

        portfolio_row = self.db.execute(
            "SELECT * FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
            (portfolio_id, session_id),
        ).fetchone()
        if portfolio_row is None:
            return self._failure("simple_direction_initial_execution_portfolio_missing")
        portfolio = dict(portfolio_row)
        if (
            str(portfolio.get("status") or "") != "awaiting_selection"
            or str(portfolio.get("source_user_turn_id") or "") != planning_root_id
            or str(portfolio.get("source_assistant_turn_id") or "") != result_turn_id
            or str(portfolio.get("request_contract_fingerprint") or "") != request_fingerprint
        ):
            return self._failure("simple_direction_initial_execution_portfolio_scope_mismatch")
        summary = self._json_dict(portfolio.get("summary_json"))
        visible_proposal_ids = [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)]
        if (
            str(summary.get("workflowMode") or "") != self.WORKFLOW_MODE
            or str(summary.get("portfolioId") or portfolio_id) != portfolio_id
            or str(summary.get("planningSelectionRootTurnId") or planning_root_id) != planning_root_id
            or str(summary.get("requestContractFingerprint") or request_fingerprint) != request_fingerprint
            or len(visible_proposal_ids) != proposal_delta
        ):
            return self._failure("simple_direction_initial_execution_root_identity_mismatch")

        frontier_status = str(response_payload.get("frontierStatus") or "")
        persisted_frontier_status = self._persisted_initial_frontier_status(summary)
        if persisted_frontier_status and frontier_status != persisted_frontier_status:
            return self._failure("simple_direction_initial_execution_frontier_status_mismatch")
        continuation_choices = [
            option
            for option in response_payload.get("choiceOptions") or []
            if isinstance(option, dict)
            and str(option.get("kind") or "") == self.CHOICE_KIND
            and str(option.get("action") or "") == self.ACTION
            and str(option.get("planningSelectionRootTurnId") or "") == planning_root_id
            and str(option.get("rootPortfolioId") or "") == portfolio_id
            and str(option.get("requestContractFingerprint") or "") == request_fingerprint
            and str(option.get("sourceAssistantTurnId") or "") == result_turn_id
            and str(option.get("sourceUserTurnId") or "") == planning_root_id
            and str(option.get("id") or "") != choice_id
        ]
        if frontier_status == "has_more" and len(continuation_choices) != 1:
            return self._failure("simple_direction_initial_execution_next_choice_missing")
        if frontier_status != "has_more" and continuation_choices:
            return self._failure("simple_direction_initial_execution_unexpected_next_choice")

        if proposal_delta == 0:
            if frontier_status != "has_more" or persisted_frontier_status != "has_more":
                return self._failure("simple_direction_initial_execution_frontier_progress_missing")
            return {
                "passed": True,
                "reason": "simple_direction_initial_execution_evidence_verified",
                "sessionId": session_id,
                "requestTurnId": request_turn_id,
                "assistantTurnId": result_turn_id,
                "sourceAssistantTurnId": source_turn_id,
                "choiceId": choice_id,
                "planningSelectionRootTurnId": planning_root_id,
                "rootPortfolioId": portfolio_id,
                "requestContractFingerprint": request_fingerprint,
                "proposalId": None,
                "proposalStatus": None,
                "partialProposal": False,
                "proposalDelta": 0,
                "frontierStatus": frontier_status,
                "nextChoiceId": str(continuation_choices[0].get("id") or ""),
                "versionDelta": 0,
                "patchDelta": 0,
                "routeWriteDelta": 0,
                "zeroWrite": True,
            }

        matching_proposals: list[dict[str, Any]] = []
        for row in self.db.execute(
            """SELECT id, status, verifier_json, generation_lineage_json
            FROM agent_plan_proposals WHERE portfolio_id = ?""",
            (portfolio_id,),
        ).fetchall():
            lineage = self._json_dict(row["generation_lineage_json"])
            if (
                str(row["id"] or "") in visible_proposal_ids
                and str(lineage.get("workflowMode") or "") == self.WORKFLOW_MODE
                and str(lineage.get("sourceUserTurnId") or "") == request_turn_id
                and str(lineage.get("sourceAssistantTurnId") or "") == result_turn_id
                and str(lineage.get("requestContractFingerprint") or "") == request_fingerprint
                and not str(lineage.get("frontierExecutionId") or "")
                and int(lineage.get("itineraryWriteCount") or 0) == 0
            ):
                matching_proposals.append(dict(row))
        if len(matching_proposals) != 1:
            return self._failure("simple_direction_initial_execution_proposal_lineage_missing")
        proposal = matching_proposals[0]
        proposal_id = str(proposal.get("id") or "")
        proposal_status = str(proposal.get("status") or "")
        verifier = self._json_dict(proposal.get("verifier_json"))
        partial_proposal = proposal_status == "blocked" and verifier.get("confirmationPassed") is not True
        ready_proposal = proposal_status == "adoption_ready" and verifier.get("confirmationPassed") is True
        if not (partial_proposal or ready_proposal):
            return self._failure("simple_direction_initial_execution_proposal_readiness_mismatch")
        projections = [item for item in response_payload.get("comparisonProjections") or [] if isinstance(item, dict)]
        projection = next(
            (item for item in projections if str(item.get("proposalId") or "") == proposal_id),
            None,
        )
        if projection is None or bool(projection.get("adoptionReady")) != ready_proposal:
            return self._failure("simple_direction_initial_execution_projection_mismatch")
        if partial_proposal and any(
            isinstance(option, dict) and str(option.get("action") or "") == "select_plan_proposal"
            for option in response_payload.get("choiceOptions") or []
        ):
            return self._failure("simple_direction_initial_execution_partial_select_capability_present")

        return {
            "passed": True,
            "reason": "simple_direction_initial_execution_evidence_verified",
            "sessionId": session_id,
            "requestTurnId": request_turn_id,
            "assistantTurnId": result_turn_id,
            "sourceAssistantTurnId": source_turn_id,
            "choiceId": choice_id,
            "planningSelectionRootTurnId": planning_root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": request_fingerprint,
            "proposalId": proposal_id,
            "proposalStatus": proposal_status,
            "partialProposal": partial_proposal,
            "proposalDelta": 1,
            "frontierStatus": frontier_status,
            "nextChoiceId": (str(continuation_choices[0].get("id") or "") if continuation_choices else None),
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "zeroWrite": True,
        }

    @staticmethod
    def _persisted_initial_frontier_status(summary: dict[str, Any]) -> str:
        """Project one initial result from frozen frontier state without advancing it."""

        qualification_frontier = summary.get("simpleDirectionFrontier")
        if isinstance(qualification_frontier, dict) and qualification_frontier:
            frozen = copy.deepcopy(qualification_frontier)
            SimpleDirectionFrontierService._refresh_status(frozen)
            return str(frozen.get("frontierStatus") or "")

        compatibility_frontier = summary.get("simpleDirectionCompatibilityFrontier")
        if isinstance(compatibility_frontier, dict):
            slot_snapshot = compatibility_frontier.get("slotFrontierSnapshot")
            if isinstance(slot_snapshot, dict) and slot_snapshot.get("remainingQueryScopesAuthoritative") is True:
                frozen = copy.deepcopy(slot_snapshot)
                SimpleDirectionFrontierService._refresh_status(frozen)
                return str(frozen.get("frontierStatus") or "")
            return str(compatibility_frontier.get("frontierStatus") or "")

        comparison_summary = summary.get("comparisonSummary")
        if isinstance(comparison_summary, dict):
            return str(comparison_summary.get("frontierStatus") or "")
        return ""

    def terminalize_claimed_frontier_execution(
        self,
        execution_id: str,
        *,
        execution_turn_id: Optional[str],
        reason_code: str,
        outcome: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Atomically close one claimed Simple Direction frontier attempt.

        Only ``claimed`` attempts are terminalized here. Provider-pending and
        already-settled attempts keep their existing recovery contract. The
        caller may add source-choice consumption in the same surrounding
        transaction before committing.
        """

        execution_identity = str(execution_id or "").strip()
        normalized_reason = str(reason_code or "simple_direction_execution_failed_after_claim").strip()
        execution_row = self.db.execute(
            "SELECT * FROM agent_choice_executions WHERE id = ?",
            (execution_identity,),
        ).fetchone()
        if execution_row is None or str(execution_row["status"] or "") != "executing":
            return False
        execution = dict(execution_row)
        session_id = str(execution.get("session_id") or "")
        source_turn_id = str(execution.get("source_turn_id") or "")
        request_turn_id = str(execution.get("request_turn_id") or "")
        choice_id = str(execution.get("choice_id") or "")
        source_user_turn_id = str(execution.get("source_user_turn_id") or "")
        execution_action = str(execution.get("action") or "")
        if not all((session_id, source_turn_id, choice_id)):
            if execution_action == self.ACTION:
                raise ValueError("simple_direction_execution_turn_lineage_missing")
            return False
        source_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (source_turn_id, session_id),
        ).fetchone()
        if source_turn is None:
            if execution_action == self.ACTION:
                raise ValueError("simple_direction_execution_turn_lineage_missing")
            return False
        source_payload = self._json_dict(source_turn["agent_response_json"])
        source_option = next(
            (
                copy.deepcopy(option)
                for option in source_payload.get("choiceOptions") or []
                if isinstance(option, dict) and str(option.get("id") or "") == choice_id
            ),
            None,
        )
        if execution_action == self.ACTION:
            from src.services.conversation_operation_identity import ConversationOperationIdentity
            try:
                source_option = ConversationOperationIdentity(self.db).source_option_for_execution(execution)
            except (ValueError, KeyError) as error:
                raise ValueError("simple_direction_execution_source_choice_mismatch") from error
        is_simple_direction_choice = bool(
            isinstance(source_option, dict)
            and str(source_option.get("kind") or "") == self.CHOICE_KIND
            and str(source_option.get("action") or "") == self.ACTION
        )
        if execution_action != self.ACTION:
            if is_simple_direction_choice:
                raise ValueError("simple_direction_execution_action_mismatch")
            return False
        if not is_simple_direction_choice:
            raise ValueError("simple_direction_execution_source_choice_mismatch")
        if not request_turn_id or not source_user_turn_id:
            raise ValueError("simple_direction_execution_turn_lineage_missing")
        request_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'user' AND status != 'superseded'""",
            (request_turn_id, session_id),
        ).fetchone()
        if request_turn is None:
            raise ValueError("simple_direction_execution_turn_lineage_missing")
        if (
            str(source_option.get("kind") or "") != self.CHOICE_KIND
            or str(source_option.get("action") or "") != self.ACTION
        ):
            raise ValueError("simple_direction_execution_source_choice_mismatch")
        source_assistant_turn_identity = str(source_option.get("sourceAssistantTurnId") or "")
        if not source_assistant_turn_identity or source_assistant_turn_identity != str(source_turn["id"]):
            raise ValueError("simple_direction_execution_source_choice_mismatch")
        source_choice_user_turn_id = str(source_option.get("sourceUserTurnId") or "")
        if not source_choice_user_turn_id or source_choice_user_turn_id != source_user_turn_id:
            raise ValueError("simple_direction_execution_source_user_turn_mismatch")
        planning_root_id = str(source_option.get("planningSelectionRootTurnId") or "")
        portfolio_id = str(source_option.get("rootPortfolioId") or "")
        request_fingerprint = str(source_option.get("requestContractFingerprint") or "")
        if not planning_root_id or not portfolio_id or not request_fingerprint:
            raise ValueError("simple_direction_execution_choice_scope_missing")
        if source_user_turn_id and source_user_turn_id != planning_root_id:
            raise ValueError("simple_direction_execution_source_user_turn_mismatch")
        portfolio = self.db.execute(
            "SELECT * FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
            (portfolio_id, session_id),
        ).fetchone()
        if portfolio is None:
            raise ValueError("simple_direction_execution_portfolio_missing")
        if (
            str(portfolio["status"] or "") != "awaiting_selection"
            or str(portfolio["source_user_turn_id"] or "") != planning_root_id
            or str(portfolio["request_contract_fingerprint"] or "") != request_fingerprint
        ):
            raise ValueError("simple_direction_execution_portfolio_scope_mismatch")
        raw_summary = str(portfolio["summary_json"] or "{}")
        summary = self._json_dict(raw_summary)
        if (
            str(summary.get("workflowMode") or "") != self.WORKFLOW_MODE
            or str(summary.get("portfolioId") or portfolio_id) != portfolio_id
            or str(summary.get("planningSelectionRootTurnId") or planning_root_id) != planning_root_id
            or str(summary.get("requestContractFingerprint") or request_fingerprint) != request_fingerprint
        ):
            raise ValueError("simple_direction_execution_root_identity_mismatch")
        matches: list[tuple[str, dict[str, Any]]] = []
        attempts = summary.get("simpleDirectionFrontierAttempts")
        attempt_record = attempts.get(execution_identity) if isinstance(attempts, dict) else None
        if isinstance(attempt_record, dict) and str(attempt_record.get("status") or "") == "claimed":
            attempt = attempt_record.get("attempt")
            attempt_fingerprint = (
                attempt.get("attemptFingerprint")
                if isinstance(attempt, dict)
                else None
            )
            expected_fingerprint = (
                SimpleDirectionFrontierService._fingerprint(
                    {key: value for key, value in attempt.items() if key != "attemptFingerprint"}
                )
                if isinstance(attempt, dict)
                else ""
            )
            if (
                str(attempt_record.get("schemaVersion") or "") != "simple-direction-frontier-claim-v1"
                or str(attempt_record.get("executionId") or "") != execution_identity
                or str(attempt_record.get("requestContractFingerprint") or "") != request_fingerprint
                or not isinstance(attempt, dict)
                or str(attempt.get("executionId") or "") != execution_identity
                or str(attempt.get("requestContractFingerprint") or "") != request_fingerprint
                or str(attempt_fingerprint or "") != expected_fingerprint
            ):
                raise ValueError("simple_direction_frontier_attempt_identity_mismatch")
            matches.append(("frontier", attempt_record))
        compatibility_attempts = summary.get("simpleDirectionCompatibilityAttempts")
        compatibility_record = (
            compatibility_attempts.get(execution_identity)
            if isinstance(compatibility_attempts, dict)
            else None
        )
        if isinstance(compatibility_record, dict) and str(compatibility_record.get("status") or "") == "claimed":
            if (
                str(compatibility_record.get("schemaVersion") or "")
                not in {
                    "simple-direction-compatibility-attempt-v1",
                    "simple-direction-compatibility-attempt-v2",
                    "simple-direction-compatibility-attempt-v3",
                }
                or str(compatibility_record.get("executionId") or "") != execution_identity
                or str(compatibility_record.get("planningSelectionRootTurnId") or "") != planning_root_id
                or str(compatibility_record.get("rootPortfolioId") or "") != portfolio_id
                or str(compatibility_record.get("sourceAssistantTurnId") or "") != source_turn_id
                or str(compatibility_record.get("requestTurnId") or "") != request_turn_id
                or str(compatibility_record.get("choiceId") or "") != choice_id
                or str(compatibility_record.get("requestContractFingerprint") or "") != request_fingerprint
                or str(compatibility_record.get("attemptFingerprint") or "")
                != PlanPortfolioStore.simple_direction_compatibility_attempt_fingerprint(compatibility_record)
            ):
                raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
            matches.append(("compatibility", compatibility_record))
        if not matches:
            return False
        if len(matches) != 1:
            raise ValueError("simple_direction_frontier_execution_identity_ambiguous")

        now = datetime.now(timezone.utc).isoformat()
        attempt_kind, attempt_record = matches[0]
        if attempt_kind == "frontier":
            frontier = summary.get("simpleDirectionFrontier")
            if not isinstance(frontier, dict):
                raise ValueError("simple_direction_frontier_missing")
            terminalized = SimpleDirectionFrontierService.terminalize_attempt(
                frontier,
                attempt=attempt_record["attempt"],
                reason_code=normalized_reason,
            )
            next_record = copy.deepcopy(attempt_record)
            next_record.update(
                {
                    "status": "reconciled",
                    "disposition": "failed_terminal",
                    "proposalId": None,
                    "outcomes": copy.deepcopy(terminalized["outcomes"]),
                    "slotQueryOutcomes": [],
                    "blockingLayer": "qualification",
                    "reasonCode": normalized_reason,
                    "providerCalled": False,
                    "resultAssistantTurnId": str(execution_turn_id or "") or None,
                    "resultFrontierFingerprint": str(
                        terminalized["frontier"].get("frontierFingerprint") or ""
                    ),
                    "updatedAt": now,
                }
            )
            summary["simpleDirectionFrontierAttempts"][execution_identity] = next_record
            summary["simpleDirectionFrontier"] = copy.deepcopy(terminalized["frontier"])
        else:
            next_record = copy.deepcopy(attempt_record)
            slot_query_outcomes: list[dict[str, Any]] = []
            if str(attempt_record.get("schemaVersion") or "") == "simple-direction-compatibility-attempt-v3":
                compatibility_frontier = summary.get("simpleDirectionCompatibilityFrontier")
                if (
                    not isinstance(compatibility_frontier, dict)
                    or str(compatibility_frontier.get("schemaVersion") or "")
                    != "simple-direction-compatibility-frontier-v1"
                    or str(compatibility_frontier.get("planningRootId") or "") != planning_root_id
                    or str(compatibility_frontier.get("requestContractFingerprint") or "")
                    != request_fingerprint
                ):
                    raise ValueError("simple_direction_compatibility_frontier_missing")
                authoritative_snapshot = compatibility_frontier.get("slotFrontierSnapshot")
                authoritative_remaining_scopes = compatibility_frontier.get("remainingQueryScopes") or []
                slot_snapshot = attempt_record.get("slotFrontierSnapshot")
                slot_queries = attempt_record.get("slotQueries")
                attempt_remaining_scopes = attempt_record.get("remainingQueryScopes") or []
                if (
                    not isinstance(authoritative_snapshot, dict)
                    or authoritative_snapshot.get("remainingQueryScopesAuthoritative") is not True
                    or not isinstance(slot_snapshot, dict)
                    or not isinstance(slot_queries, dict)
                    or not slot_queries
                ):
                    raise ValueError("simple_direction_compatibility_slot_frontier_missing")
                canonical_attempt_snapshot = self._canonical_frontier_snapshot(slot_snapshot)
                canonical_authoritative_snapshot = self._canonical_frontier_snapshot(authoritative_snapshot)
                if canonical_attempt_snapshot != canonical_authoritative_snapshot:
                    raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
                canonical_attempt_remaining_scopes = (
                    SimpleDirectionFrontierService.normalize_remaining_query_scopes(attempt_remaining_scopes)
                )
                canonical_authoritative_remaining_scopes = (
                    SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                        authoritative_remaining_scopes
                    )
                )
                if (
                    canonical_attempt_remaining_scopes != canonical_authoritative_remaining_scopes
                    or canonical_authoritative_remaining_scopes
                    != SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                        canonical_authoritative_snapshot.get("remainingQueryScopes") or []
                    )
                ):
                    raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
                updated_slot_snapshot = copy.deepcopy(slot_snapshot)
                for slot_id, raw_query in slot_queries.items():
                    if not isinstance(raw_query, dict):
                        raise ValueError("simple_direction_compatibility_slot_outcome_invalid")
                    expected_query = SimpleDirectionFrontierService.begin_slot_query(
                        updated_slot_snapshot,
                        day_number=int(raw_query.get("dayNumber") or 0),
                        slot_id=str(raw_query.get("slotId") or ""),
                        day_seed_amap_id=str(raw_query.get("daySeedAmapId") or ""),
                        query_scope_fingerprint=str(raw_query.get("queryScopeFingerprint") or ""),
                    )
                    if any(
                        str(raw_query.get(field) if raw_query.get(field) is not None else "")
                        != str(expected_query.get(field) if expected_query.get(field) is not None else "")
                        for field in (
                            "slotFrontierKey",
                            "queryFingerprint",
                            "requestContractFingerprint",
                            "dayNumber",
                            "slotId",
                            "daySeedAmapId",
                            "queryScopeFingerprint",
                            "page",
                            "offset",
                        )
                    ):
                        raise ValueError("simple_direction_compatibility_slot_outcome_identity_mismatch")
                    query = copy.deepcopy(raw_query)
                    slot_query_outcomes.append(
                        {
                            "slotId": str(slot_id or ""),
                            "query": query,
                            "providerOutcome": "rejected",
                            "providerCalled": False,
                            "outcomeSource": "server_fail_closed_terminalization",
                            "admittedPhysicalGroups": [],
                            "rejectedPhysicalGroups": [],
                            "selectedAmapId": None,
                            "reasonCode": normalized_reason,
                        }
                    )
                    updated_slot_snapshot = SimpleDirectionFrontierService.record_slot_query(
                        updated_slot_snapshot,
                        query=query,
                        provider_outcome="rejected",
                        admitted_physical_groups=[],
                        rejected_physical_groups=[],
                    )
                    stored_query = (updated_slot_snapshot.get("slotFrontiers") or {}).get(
                        str(query.get("slotFrontierKey") or "")
                    )
                    if isinstance(stored_query, dict):
                        stored_query["lastTerminalizationReasonCode"] = normalized_reason
                SimpleDirectionFrontierService._refresh_status(updated_slot_snapshot)
                remaining_query_scopes = SimpleDirectionFrontierService.remaining_query_scopes(
                    updated_slot_snapshot
                )
                compatibility_frontier["slotFrontierSnapshot"] = copy.deepcopy(updated_slot_snapshot)
                compatibility_frontier["remainingQueryScopes"] = copy.deepcopy(remaining_query_scopes)
                compatibility_frontier["frontierStatus"] = str(
                    updated_slot_snapshot.get("frontierStatus") or "poi_exhausted"
                )
                compatibility_frontier["remainingPoiPageCount"] = int(
                    updated_slot_snapshot.get("remainingPoiPageCount") or 0
                )
                summary["simpleDirectionCompatibilityFrontier"] = compatibility_frontier
            next_record.update(
                {
                    "status": "failed_terminal",
                    "frontierStatus": "failed_terminal",
                    "disposition": "failed_terminal",
                    "proposalId": None,
                    "proposalDelta": 0,
                    "progress": {
                        "madeProgress": False,
                        "queryProgress": False,
                        "candidateProgress": False,
                        "routeProgress": False,
                    },
                    "reasonCode": normalized_reason,
                    "providerCalled": False,
                    "slotQueryOutcomes": slot_query_outcomes,
                    "resultAssistantTurnId": str(execution_turn_id or "") or None,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                    "updatedAt": now,
                }
            )
            if slot_query_outcomes:
                next_record["slotFrontierSnapshot"] = copy.deepcopy(
                    summary["simpleDirectionCompatibilityFrontier"]["slotFrontierSnapshot"]
                )
                next_record["remainingQueryScopes"] = copy.deepcopy(
                    summary["simpleDirectionCompatibilityFrontier"].get("remainingQueryScopes") or []
                )
            next_record["attemptFingerprint"] = PlanPortfolioStore.simple_direction_compatibility_attempt_fingerprint(
                next_record
            )
            summary["simpleDirectionCompatibilityAttempts"][execution_identity] = next_record
        terminal_outcome = {
            **copy.deepcopy(outcome or {}),
            "reason": normalized_reason,
            "frontierExecutionId": execution_identity,
            "frontierAttemptConsumed": True,
            "boundedAttemptConsumed": True,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "zeroWrite": True,
        }
        savepoint = "simple_direction_claim_failure_terminalize"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            portfolio_updated = self.db.execute(
                """UPDATE agent_plan_portfolios
                SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (
                    json.dumps(summary, ensure_ascii=False, default=str),
                    now,
                    str(portfolio["id"] or ""),
                    raw_summary,
                ),
            )
            if portfolio_updated.rowcount != 1:
                raise ValueError("simple_direction_frontier_attempt_stale")
            execution_updated = self.db.execute(
                """UPDATE agent_choice_executions
                SET status = 'failed_terminal', execution_turn_id = ?, outcome_json = ?,
                    error_json = ?, updated_at = ?
                WHERE id = ? AND status = 'executing' AND updated_at = ?""",
                (
                    str(execution_turn_id or "") or None,
                    json.dumps(terminal_outcome, ensure_ascii=False, default=str),
                    json.dumps({"code": normalized_reason, "retryable": False}, ensure_ascii=False),
                    now,
                    execution_identity,
                    str(execution.get("updated_at") or ""),
                ),
            )
            if execution_updated.rowcount != 1:
                raise ValueError("simple_direction_frontier_execution_stale")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return True
        except Exception:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def recover_or_terminalize_expired_compatibility_execution(
        self,
        execution_id: str,
    ) -> dict[str, Any]:
        """Resolve a stale compatibility lease without another Provider call.

        A reconciled attempt can be completed solely from its atomically
        persisted assistant evidence.  A claim that never reached settlement
        is terminalized instead of being replayed, preserving the one-attempt
        budget and replacing a permanent ``executing`` lock with an explicit
        fail-closed state.
        """

        execution = self.db.execute(
            "SELECT * FROM agent_choice_executions WHERE id = ?",
            (str(execution_id or ""),),
        ).fetchone()
        if (
            execution is None
            or str(execution["action"] or "") != self.ACTION
            or str(execution["status"] or "") != "executing"
        ):
            return self._failure("simple_direction_execution_not_recoverable")
        execution = dict(execution)
        source_turn = self.db.execute(
            """SELECT agent_response_json FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (str(execution.get("source_turn_id") or ""), str(execution.get("session_id") or "")),
        ).fetchone()
        source_payload = self._json_dict(source_turn["agent_response_json"] if source_turn else None)
        source_option = next(
            (
                item
                for item in source_payload.get("choiceOptions") or []
                if isinstance(item, dict)
                and str(item.get("id") or "") == str(execution.get("choice_id") or "")
                and str(item.get("action") or "") == self.ACTION
            ),
            None,
        )
        portfolio_id = str(source_option.get("rootPortfolioId") or "") if isinstance(source_option, dict) else ""
        portfolio = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
            (portfolio_id, str(execution.get("session_id") or "")),
        ).fetchone()
        summary = self._json_dict(portfolio["summary_json"] if portfolio else None)
        attempts = summary.get("simpleDirectionCompatibilityAttempts")
        attempt = attempts.get(str(execution_id)) if isinstance(attempts, dict) else None
        if not portfolio_id or not isinstance(attempt, dict):
            return self._terminalize_expired_execution(
                execution,
                reason="simple_direction_compatibility_attempt_missing",
            )

        if str(attempt.get("status") or "") in {"reconciled", "no_progress"}:
            result_turn_id = str(attempt.get("resultAssistantTurnId") or "")
            evidence = self.verify(
                str(execution_id),
                assistant_turn_id=result_turn_id,
                expected_request_turn_id=str(execution.get("request_turn_id") or ""),
            )
            if evidence.get("passed") is True:
                consumed_ids = [str(item) for item in source_payload.get("consumedChoiceIds") or [] if str(item)]
                choice_id = str(execution.get("choice_id") or "")
                if choice_id not in consumed_ids:
                    consumed_ids.append(choice_id)
                recovered_source_payload = copy.deepcopy(source_payload)
                recovered_source_payload["consumedChoiceIds"] = consumed_ids
                source_updated = self.db.execute(
                    """UPDATE conversation_turns SET agent_response_json = ?, updated_at = ?
                    WHERE id = ? AND session_id = ? AND agent_response_json = ?""",
                    (
                        json.dumps(recovered_source_payload, ensure_ascii=False, default=str),
                        datetime.now(timezone.utc).isoformat(),
                        str(execution.get("source_turn_id") or ""),
                        str(execution.get("session_id") or ""),
                        str(source_turn["agent_response_json"] or "{}"),
                    ),
                )
                if source_updated.rowcount != 1:
                    self.db.rollback()
                    return self._failure("simple_direction_execution_recovery_source_choice_stale")
                updated = self.db.execute(
                    """UPDATE agent_choice_executions
                    SET status = 'succeeded', execution_turn_id = ?, outcome_json = ?,
                        error_json = NULL, updated_at = ?
                    WHERE id = ? AND status = 'executing' AND updated_at = ?""",
                    (
                        result_turn_id,
                        json.dumps(evidence, ensure_ascii=False, default=str),
                        datetime.now(timezone.utc).isoformat(),
                        str(execution_id),
                        str(execution.get("updated_at") or ""),
                    ),
                )
                if updated.rowcount == 1:
                    self.db.commit()
                    return {**evidence, "status": "succeeded", "recovered": True}
                self.db.rollback()
                return self._failure("simple_direction_execution_recovery_stale")
            return self._terminalize_expired_execution(
                execution,
                reason=str(evidence.get("reason") or "simple_direction_execution_recovery_evidence_invalid"),
            )

        if str(attempt.get("status") or "") == "claimed":
            PlanPortfolioStore(self.db).fail_simple_direction_compatibility_attempt(
                portfolio_id=portfolio_id,
                execution_id=str(execution_id),
                reason_code="simple_direction_compatibility_execution_lease_expired",
                expected_execution_updated_at=str(execution.get("updated_at") or ""),
            )
            return {
                "passed": False,
                "reason": "simple_direction_compatibility_execution_lease_expired",
                "status": "failed_terminal",
                "recovered": False,
                "frontierAttemptConsumed": False,
                "versionDelta": 0,
                "patchDelta": 0,
                "routeWriteDelta": 0,
                "zeroWrite": True,
            }
        if str(attempt.get("status") or "") == "provider_pending":
            return self._mark_expired_execution_retryable(
                execution,
                reason=str(attempt.get("reasonCode") or "simple_direction_frontier_provider_pending"),
            )
        return self._terminalize_expired_execution(
            execution,
            reason="simple_direction_compatibility_execution_lease_expired",
        )

    def _mark_expired_execution_retryable(
        self,
        execution: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        outcome = {
            "passed": False,
            "reason": str(reason),
            "frontierAttemptConsumed": False,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "zeroWrite": True,
        }
        updated = self.db.execute(
            """UPDATE agent_choice_executions
            SET status = 'failed_retryable', outcome_json = ?, error_json = ?, updated_at = ?
            WHERE id = ? AND status = 'executing' AND updated_at = ?""",
            (
                json.dumps(outcome, ensure_ascii=False, default=str),
                json.dumps({"code": str(reason), "retryable": True}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                str(execution.get("id") or ""),
                str(execution.get("updated_at") or ""),
            ),
        )
        if updated.rowcount == 1:
            self.db.commit()
            return {**outcome, "status": "failed_retryable", "recovered": False}
        self.db.rollback()
        return self._failure("simple_direction_execution_recovery_stale")

    def _terminalize_expired_execution(
        self,
        execution: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        outcome = {
            "passed": False,
            "reason": str(reason),
            "frontierAttemptConsumed": False,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "zeroWrite": True,
        }
        updated = self.db.execute(
            """UPDATE agent_choice_executions
            SET status = 'failed_terminal', outcome_json = ?, error_json = ?, updated_at = ?
            WHERE id = ? AND status = 'executing' AND updated_at = ?""",
            (
                json.dumps(outcome, ensure_ascii=False, default=str),
                json.dumps({"code": str(reason), "retryable": False}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                str(execution.get("id") or ""),
                str(execution.get("updated_at") or ""),
            ),
        )
        if updated.rowcount == 1:
            self.db.commit()
            return {**outcome, "status": "failed_terminal", "recovered": False}
        self.db.rollback()
        return self._failure("simple_direction_execution_recovery_stale")

    def reconcile_legacy_execution(self, execution_id: str) -> bool:
        row = self.db.execute(
            "SELECT * FROM agent_choice_executions WHERE id = ?",
            (str(execution_id or ""),),
        ).fetchone()
        if row is None or str(row["status"] or "") != "failed_retryable":
            return False
        old_outcome = self._json_dict(row["outcome_json"])
        if str(old_outcome.get("reason") or "") != "no_material_progress":
            return False
        evidence = self.verify(str(execution_id))
        if evidence.get("passed") is not True:
            return False
        source_turn_id = str(row["source_turn_id"] or "")
        choice_id = str(row["choice_id"] or "")
        source_turn = self.db.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ? AND session_id = ?",
            (source_turn_id, str(row["session_id"] or "")),
        ).fetchone()
        if source_turn is None:
            return False
        source_payload = self._json_dict(source_turn["agent_response_json"])
        consumed = [str(item) for item in source_payload.get("consumedChoiceIds") or [] if str(item)]
        if choice_id not in consumed:
            consumed.append(choice_id)
        source_payload["consumedChoiceIds"] = consumed
        corrected_outcome = {
            **old_outcome,
            **copy.deepcopy(evidence),
            "reason": (
                "new_simple_direction_proposal"
                if int(evidence.get("proposalDelta") or 0) > 0
                else "simple_direction_frontier_advanced_without_proposal"
            ),
            "reconciledFrom": "failed_retryable/no_material_progress",
            "boundedAttemptConsumed": True,
        }
        now = datetime.now(timezone.utc).isoformat()
        savepoint = "simple_direction_execution_lazy_reconcile"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            updated = self.db.execute(
                """UPDATE agent_choice_executions
                SET status = 'succeeded', outcome_json = ?, error_json = NULL, updated_at = ?
                WHERE id = ? AND status = 'failed_retryable'""",
                (json.dumps(corrected_outcome, ensure_ascii=False, default=str), now, str(execution_id)),
            )
            if updated.rowcount != 1:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                return False
            self.db.execute(
                "UPDATE conversation_turns SET agent_response_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(source_payload, ensure_ascii=False, default=str), now, source_turn_id),
            )
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.db.commit()
            return True
        except Exception:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def reconcile_legacy_for_source_turn(self, *, session_id: str, source_turn_id: str) -> int:
        rows = self.db.execute(
            """SELECT id FROM agent_choice_executions
            WHERE session_id = ? AND source_turn_id = ? AND action = ? AND status = 'failed_retryable'""",
            (session_id, source_turn_id, self.ACTION),
        ).fetchall()
        return sum(self.reconcile_legacy_execution(str(row["id"])) for row in rows)

    @classmethod
    def build_continuation_choice(
        cls,
        *,
        portfolio_id: str,
        source_assistant_turn_id: str,
        planning_root_id: str,
        request_fingerprint: str,
        expected_base_version_id: Optional[str],
    ) -> dict[str, Any]:
        choice_id = (
            "simple_direction_continue_"
            + hashlib.sha256(f"{portfolio_id}:{source_assistant_turn_id}".encode("utf-8")).hexdigest()[:24]
        )
        return {
            "id": choice_id,
            "choiceId": choice_id,
            "action": cls.ACTION,
            "kind": cls.CHOICE_KIND,
            "scopeKind": "comparison",
            "label": "继续探索其他方向",
            "value": "继续生成其他方向",
            "sourceAssistantTurnId": source_assistant_turn_id,
            "sourceUserTurnId": planning_root_id,
            "planningSelectionRootTurnId": planning_root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": request_fingerprint,
            "expectedBaseVersionId": expected_base_version_id,
            "workflowMode": cls.WORKFLOW_MODE,
        }

    def reconcile_missing_continuation_for_source_turn(
        self,
        *,
        session_id: str,
        source_turn_id: str,
    ) -> bool:
        """Repair a latest partial result that truthfully promised expansion.

        Older builds promoted a proposal-scoped route Provider blocker to the
        whole qualification frontier.  They consequently persisted a partial
        proposal with remaining entities but no opaque continuation choice.
        Reissue is allowed only from complete, zero-write server evidence for
        the latest awaiting-selection root in the session.
        """

        source_turn = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (str(source_turn_id or ""), str(session_id or "")),
        ).fetchone()
        if source_turn is None:
            return False
        raw_payload = str(source_turn["agent_response_json"] or "")
        payload = self._json_dict(raw_payload)
        if (
            str(payload.get("mode") or "") != "simple_open_direction_proposal"
            or str(payload.get("workflowMode") or "") != self.WORKFLOW_MODE
            or int(payload.get("proposalDelta") or 0) <= 0
            or payload.get("frontierAttemptConsumed") is not True
            or any(int(payload.get(key) or 0) != 0 for key in ("versionDelta", "patchDelta", "routeWriteDelta"))
            or str(payload.get("reasonCode") or "") in self.PROVIDER_PENDING_REASONS
            or self._contains_provider_failure(payload)
        ):
            return False
        existing_options = [item for item in payload.get("choiceOptions") or [] if isinstance(item, dict)]
        if any(
            str(item.get("action") or "") == self.ACTION and str(item.get("kind") or "") == self.CHOICE_KIND
            for item in existing_options
        ):
            return False

        portfolio_id = str(payload.get("rootPortfolioId") or "")
        planning_root_id = str(payload.get("planningSelectionRootTurnId") or "")
        portfolio = self.db.execute(
            """SELECT * FROM agent_plan_portfolios
            WHERE id = ? AND session_id = ? AND source_assistant_turn_id = ? AND status = 'awaiting_selection'""",
            (portfolio_id, session_id, source_turn_id),
        ).fetchone()
        if portfolio is None:
            return False
        request_fingerprint = str(portfolio["request_contract_fingerprint"] or "")
        if (
            not planning_root_id
            or str(portfolio["source_user_turn_id"] or "") != planning_root_id
            or str(payload.get("requestContractFingerprint") or request_fingerprint) != request_fingerprint
        ):
            return False
        latest = self.db.execute(
            """SELECT p.id
            FROM agent_plan_portfolios p
            JOIN conversation_turns t ON t.id = p.source_assistant_turn_id
            WHERE p.session_id = ? AND p.status = 'awaiting_selection' AND t.status != 'superseded'
            ORDER BY t.turn_index DESC, p.updated_at DESC
            LIMIT 1""",
            (session_id,),
        ).fetchone()
        if latest is None or str(latest["id"] or "") != portfolio_id:
            return False
        if self._formal_write_count(session_id, planning_root_id, source_turn_id) != 0:
            return False

        summary = PlanPortfolioStore(self.db).simple_direction_comparison_summary(portfolio_id=portfolio_id)
        if (
            str(summary.get("frontierStatus") or "") != "has_more"
            or int(summary.get("remainingQualifiedEntityCount") or 0) + int(summary.get("remainingPoiPageCount") or 0)
            <= 0
            or int(summary.get("adoptionReadyCount") or 0) + int(summary.get("repairablePartialCount") or 0) <= 0
        ):
            return False
        choice = self.build_continuation_choice(
            portfolio_id=portfolio_id,
            source_assistant_turn_id=source_turn_id,
            planning_root_id=planning_root_id,
            request_fingerprint=request_fingerprint,
            expected_base_version_id=(str(portfolio["expected_base_version_id"] or "") or None),
        )
        if self.db.execute(
            "SELECT 1 FROM agent_choice_executions WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?",
            (session_id, source_turn_id, str(choice["id"])),
        ).fetchone():
            return False

        payload["choiceOptions"] = [*existing_options, choice]
        payload["frontierStatus"] = "has_more"
        payload["remainingQualifiedEntityCount"] = int(summary.get("remainingQualifiedEntityCount") or 0)
        payload["comparisonSummary"] = copy.deepcopy(summary)
        now = datetime.now(timezone.utc).isoformat()
        updated = self.db.execute(
            """UPDATE conversation_turns
            SET agent_response_json = ?, updated_at = ?
            WHERE id = ? AND session_id = ? AND agent_response_json = ? AND status != 'superseded'""",
            (
                json.dumps(payload, ensure_ascii=False, default=str),
                now,
                source_turn_id,
                session_id,
                raw_payload,
            ),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            return False
        self.db.commit()
        return True

    def _proposal_lineage_exists(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        assistant_turn_id: str,
        request_fingerprint: str,
        expected_proposal_id: Optional[str] = None,
        expected_attempt_fingerprint: Optional[str] = None,
        guide_requirement: Optional[dict[str, Any]] = None,
        expected_guide_usage: Optional[dict[str, Any]] = None,
    ) -> bool:
        for row in self.db.execute(
            """SELECT id, snapshot_json, verifier_json, evidence_json, generation_lineage_json
            FROM agent_plan_proposals WHERE portfolio_id = ?""",
            (portfolio_id,),
        ).fetchall():
            lineage = self._json_dict(row["generation_lineage_json"])
            snapshot = self._json_dict(row["snapshot_json"])
            verifier = self._json_dict(row["verifier_json"])
            proposal_evidence = self._json_dict(row["evidence_json"])
            persisted_requirement = (
                snapshot.get("guideContinuationRequirement")
                if isinstance(snapshot.get("guideContinuationRequirement"), dict)
                else {}
            )
            persisted_usage = (
                verifier.get("guideEvidenceUsage")
                if isinstance(verifier.get("guideEvidenceUsage"), dict)
                else {}
            )
            evidence_usage = (
                proposal_evidence.get("guideEvidenceUsage")
                if isinstance(proposal_evidence.get("guideEvidenceUsage"), dict)
                else {}
            )
            guide_lineage_valid = bool(
                not guide_requirement
                or (
                    str(lineage.get("guideContinuationRequirementFingerprint") or "")
                    == str(guide_requirement.get("requirementFingerprint") or "")
                    and str(lineage.get("guideEvidenceSourceAssistantTurnId") or "")
                    == str(guide_requirement.get("sourceAssistantTurnId") or "")
                    and str(lineage.get("guideChoiceExecutionId") or "")
                    == str(guide_requirement.get("guideChoiceExecutionId") or "")
                    and str(lineage.get("guideEvidenceFingerprint") or "")
                    == str(guide_requirement.get("evidenceFingerprint") or "")
                    and persisted_requirement == guide_requirement
                    and str(persisted_usage.get("status") or "") == "satisfied"
                    and persisted_usage == (expected_guide_usage or {})
                    and evidence_usage == persisted_usage
                )
            )
            if (
                (not expected_proposal_id or str(row["id"] or "") == expected_proposal_id)
                and str(lineage.get("workflowMode") or "") == self.WORKFLOW_MODE
                and str(lineage.get("frontierExecutionId") or "") == execution_id
                and str(lineage.get("sourceAssistantTurnId") or "") == assistant_turn_id
                and str(lineage.get("requestContractFingerprint") or "") == request_fingerprint
                and (
                    not expected_attempt_fingerprint
                    or str(lineage.get("frontierAttemptFingerprint") or "") == expected_attempt_fingerprint
                )
                and int(lineage.get("itineraryWriteCount") or 0) == 0
                and guide_lineage_valid
            ):
                return True
        return False

    def _formal_write_count(self, session_id: str, request_turn_id: str, assistant_turn_id: str) -> int:
        turn_ids = [value for value in {request_turn_id, assistant_turn_id} if value]
        if not turn_ids:
            return 0
        placeholders = ",".join("?" for _ in turn_ids)
        parameters = (session_id, *turn_ids)
        version_count = int(
            self.db.execute(
                f"SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ? AND source_turn_id IN ({placeholders})",
                parameters,
            ).fetchone()[0]
        )
        patch_count = int(
            self.db.execute(
                f"SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ? AND source_turn_id IN ({placeholders})",
                parameters,
            ).fetchone()[0]
        )
        transaction_count = int(
            self.db.execute(
                f"SELECT COUNT(*) FROM timeline_mutation_transactions WHERE session_id = ? AND source_turn_id IN ({placeholders})",
                parameters,
            ).fetchone()[0]
        )
        return version_count + patch_count + transaction_count

    @classmethod
    def _contains_provider_failure(cls, value: Any) -> bool:
        if isinstance(value, dict):
            if str(value.get("providerOutcome") or "").casefold() == "failure":
                return True
            if str(value.get("providerState") or "").casefold() in {"failed", "unavailable", "timeout"}:
                return True
            return any(cls._contains_provider_failure(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_provider_failure(item) for item in value)
        return False

    @staticmethod
    def _failure(reason: str) -> dict[str, Any]:
        return {"passed": False, "reason": reason}

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _canonical_frontier_snapshot(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        snapshot = copy.deepcopy(value)
        snapshot["remainingQueryScopes"] = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
            snapshot.get("remainingQueryScopes") or []
        )
        SimpleDirectionFrontierService._refresh_status(snapshot)
        return snapshot

import copy
import json
import sqlite3
from collections.abc import Callable
from typing import Any, Optional

from fastapi import HTTPException

from src.api.schemas.agent import AgentMessageRequest
from src.services.agent_model_registry import resolve_agent_model
from src.services.conversation_service import ConversationService
from src.services.conversation_intent_router import (
    ConversationIntentClassification,
)
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.planning_attempt_resume_service import PlanningAttemptResumeService
from src.services.portfolio_density_continuation_service import PortfolioDensityContinuationService
from src.services.preference_service import PreferenceService
from src.services.retry_recovery_service import (
    RetryExecutionPlanService,
    RetryIntentClassification,
    RetrySemanticClassifier,
)
from src.services.trip_date_resolver import TripDateResolver


SUPPORTED_AGENT_PATCH_OPS = [
    "replace_itinerary",
    "replace_trip_title",
    "replace_day_title",
    "replace_segment_start_time",
    "replace_segment_duration",
    "replace_transport_mode",
    "add_day",
    "add_segment",
    "replace_segment_poi",
    "remove_segment",
    "move_segment",
    "reorder_segments",
    "update_segment_notes",
]


RequirementAnalyzer = Callable[[str, str, Optional[dict], str], dict[str, Any]]
PreferenceImpactAnalyzer = Callable[[str], dict[str, Any]]


class AgentContextBuilder:
    def __init__(
        self,
        db: sqlite3.Connection,
        planner_service: Any = None,
        skill_service: Any = None,
        supported_patch_operations: Optional[list[str]] = None,
        requirement_analyzer: Optional[RequirementAnalyzer] = None,
        preference_impact_analyzer: Optional[PreferenceImpactAnalyzer] = None,
    ):
        self.db = db
        # Kept as ignored constructor compatibility only. Planning and skill
        # selection are orchestration responsibilities owned by AgentService.
        _ = planner_service, skill_service
        self.supported_patch_operations = supported_patch_operations or SUPPORTED_AGENT_PATCH_OPS
        self.requirement_analyzer = requirement_analyzer or self._default_requirement_analyzer
        self.preference_impact_analyzer = preference_impact_analyzer or self._default_preference_impact_analyzer

    def build(
        self,
        session: sqlite3.Row,
        latest_message: str,
        payload: AgentMessageRequest,
        *,
        allow_plan_expansion_rebind: bool = True,
        conversation_intent_route: Optional[dict[str, Any]] = None,
        conversation_capability_resolution: Optional[dict[str, Any]] = None,
    ) -> dict:
        current_snapshot = None
        if session["active_version_id"]:
            current_snapshot = ItinerarySnapshotService(self.db).capture_active_version_snapshot(
                session["active_plan_id"],
                session["active_version_id"],
            )
        active_turns = self._active_turns(session["id"])
        context_payload = payload.context.model_dump(by_alias=True)
        raw_selected_agent_choice = context_payload.get("selectedAgentChoice")
        if conversation_intent_route is None:
            legacy_retry_classifier = RetrySemanticClassifier()
            retry_intent = legacy_retry_classifier.classify(latest_message)
            if raw_selected_agent_choice is not None and legacy_retry_classifier.is_rejected_plan_expansion(
                latest_message
            ):
                raw_selected_agent_choice = None
        else:
            retry_intent = self._retry_intent_from_conversation_route(
                conversation_intent_route,
                latest_message,
            )
        selected_agent_choice = self._resolve_selected_agent_choice(session, raw_selected_agent_choice)
        selected_kind = str(((selected_agent_choice or {}).get("option") or {}).get("kind") or "")
        selected_action = str((selected_agent_choice or {}).get("action") or "")
        if (
            conversation_intent_route is None
            and allow_plan_expansion_rebind
            and retry_intent.intent_class == "continue_plan_expansion"
        ):
            can_rebind_expansion = selected_agent_choice is None or (
                selected_kind == "custom_input" and selected_action == "manual_continuation"
            )
            if can_rebind_expansion:
                selected_agent_choice = self._resolve_current_plan_expansion_choice(session)
        if (
            allow_plan_expansion_rebind
            and selected_agent_choice is None
            and retry_intent.intent_class == "retry_contextual"
        ):
            selected_agent_choice = self._resolve_current_plan_expansion_choice(
                session,
                retry_current_stage_only=True,
            )
        selected_agent_model = resolve_agent_model(
            getattr(payload, "agent_model", None) or context_payload.get("agentModel")
        )
        current_preference_summary, memory_text, memory = self._preference_context(context_payload, session["id"])
        memory_rules = memory.compiled_rules
        structured_memory = memory.structured_memory
        planning_resume_service = PlanningAttemptResumeService(self.db)
        structured_route_active = conversation_intent_route is not None
        structured_retry_intent = retry_intent.intent_class in {
            "retry_contextual",
            "retry_failed_component",
            "continue_pending_choice",
            "regenerate_from_scratch",
        }
        structured_latest_is_retry = structured_retry_intent or (retry_intent.intent_class == "continue_plan_expansion")
        retry_execution_plan = RetryExecutionPlanService(self.db).resolve(
            session_id=str(session["id"]),
            intent=retry_intent,
            request_contract_fingerprint=(str(context_payload.get("requestContractFingerprint") or "").strip() or None),
        )
        resume_attempt = (
            planning_resume_service.resume_context_for_retry(
                session["id"],
                latest_message,
                is_retry_intent=(structured_retry_intent if structured_route_active else None),
            )
            if retry_execution_plan.kind == "resume_incomplete_stage"
            or (
                retry_execution_plan.kind == "regenerate_full"
                and retry_execution_plan.reason_code == "explicit_full_regeneration"
            )
            else None
        )
        if retry_execution_plan.kind == "start_modified_request":
            resume_attempt = None
        if (
            resume_attempt
            and retry_execution_plan.kind == "regenerate_full"
            and retry_execution_plan.reason_code == "explicit_full_regeneration"
        ):
            resume_attempt = {
                **resume_attempt,
                "reason": "explicit_full_regeneration",
                "resumeFromStage": "initial_day_slot_provider",
                "reuseInitialPlan": False,
                "retryOnlyUnresolvedSlots": False,
                "initialPlan": None,
                "pipelineContext": {},
                "groundingCheckpoint": None,
            }
        regenerate_context = None
        if not resume_attempt and not selected_agent_choice and retry_execution_plan.kind != "start_modified_request":
            if retry_execution_plan.kind == "regenerate_full":
                regenerate_context = planning_resume_service.regenerate_context_from_request_history(
                    session["id"],
                    latest_message,
                    is_retry_intent=(structured_retry_intent if structured_route_active else None),
                )
            else:
                regenerate_context = planning_resume_service.regenerate_context_for_active_version(
                    session["id"],
                    latest_message,
                    is_retry_intent=(structured_retry_intent if structured_route_active else None),
                )
        clarification_context = (
            None
            if resume_attempt or regenerate_context or selected_agent_choice
            else planning_resume_service.clarification_context_for_answer(
                session["id"],
                latest_message,
                is_retry_intent=(structured_latest_is_retry if structured_route_active else None),
            )
        )
        effective_user_message = (
            str(resume_attempt.get("originalUserMessage") or "").strip()
            if resume_attempt
            else str(regenerate_context.get("originalUserMessage") or "").strip()
            if regenerate_context
            else str(clarification_context.get("effectiveUserMessage") or "").strip()
            if clarification_context
            else latest_message
        )
        if selected_agent_choice:
            # A generic clarification answer completes the user request that
            # preceded the source assistant turn.  This is server-derived;
            # neither its display label nor any client-provided text is used.
            effective_user_message = str(selected_agent_choice["semanticMessage"])
        latest_message_is_retry_intent = (
            structured_latest_is_retry
            if structured_route_active
            else PlanningAttemptResumeService.is_retry_intent(latest_message)
        )
        resume_next_actions = [str(item) for item in (resume_attempt or {}).get("nextActions", []) if str(item).strip()]
        retry_candidate_hints = bool(
            context_payload.get("retryCandidateHints")
            or context_payload.get("retry_candidate_hints")
            or (
                resume_attempt
                and any(
                    action
                    in {
                        "retry_candidate_hints",
                        "manual_candidate_hints",
                        "retry_after_map_provider_recovers",
                        "retry_unfinished_poi_grounding",
                        "choose_spatially_reasonable_candidate_or_retry",
                    }
                    or "candidate" in action
                    or "poi_grounding" in action
                    for action in resume_next_actions
                )
            )
        )
        understood_requirements = self.requirement_analyzer(
            effective_user_message,
            session["city"],
            current_snapshot,
            current_preference_summary,
        )
        resolved_date_source = (
            "latestUserMessage" if effective_user_message == latest_message else "effectiveUserMessage"
        )
        resolved_trip_dates = (
            TripDateResolver()
            .resolve(
                effective_user_message,
                {
                    **context_payload,
                    "understoodRequirements": understood_requirements,
                    "timelineContext": context_payload.get("timelineContext") or current_snapshot,
                },
                source=resolved_date_source,
            )
            .to_camel_dict()
        )
        if resolved_trip_dates.get("status") == "resolved" and resolved_trip_dates.get("dates"):
            canonical_dates = [str(item) for item in resolved_trip_dates["dates"] if str(item)]
            fields = (
                understood_requirements.setdefault("fields", {})
                if isinstance(understood_requirements, dict)
                else {}
            )
            if isinstance(fields, dict) and canonical_dates:
                fields["travelDays"] = f"{len(canonical_dates)} 天"
                fields["travelDate"] = (
                    canonical_dates[0]
                    if len(canonical_dates) == 1
                    else f"{canonical_dates[0]} 至 {canonical_dates[-1]}"
                )
        preference_card = self._preference_card(context_payload)
        planning_quality_contract = self._planning_quality_contract(
            effective_user_message, understood_requirements, current_preference_summary, memory_rules
        )
        request_context = {
            "sessionId": session["id"],
            # Preserve the literal current turn. A persisted choice may supply
            # a server-derived semantic message for execution, but it must not
            # overwrite what the user actually sent this turn.
            "latestUserMessage": latest_message,
            "effectiveUserMessage": effective_user_message,
            "latestMessageIsRetryIntent": latest_message_is_retry_intent,
            "retryIntent": retry_intent.model_dump(by_alias=True),
            "retryExecutionPlan": retry_execution_plan.model_dump(by_alias=True),
            "conversationIntent": conversation_intent_route,
            "conversationCapability": conversation_capability_resolution,
            "viewContext": context_payload.get("viewContext"),
            "viewResolution": context_payload.get("viewResolution"),
            "resumePlanningAttempt": resume_attempt or {"enabled": False},
            "regeneratePlanningRequest": regenerate_context or {"enabled": False},
            "conversationIntentContext": {
                "source": "latest_complete_user_request"
                if resume_attempt
                else "latest_complete_user_request_for_regenerate"
                if regenerate_context
                else "clarification_answer"
                if clarification_context
                else "latest_user_message",
                "sourceAssistantTurnId": (resume_attempt or clarification_context or {}).get("sourceAssistantTurnId"),
                "sourceAssistantTurnIndex": (resume_attempt or clarification_context or {}).get(
                    "sourceAssistantTurnIndex"
                ),
                "sourceUserTurnId": (resume_attempt or regenerate_context or clarification_context or {}).get(
                    "sourceUserTurnId"
                ),
                "sourceUserTurnIndex": (resume_attempt or regenerate_context or clarification_context or {}).get(
                    "sourceUserTurnIndex"
                ),
                "ignoredRetryMessage": latest_message if resume_attempt or regenerate_context else None,
                "clarificationAnswer": (clarification_context or {}).get("clarificationAnswer"),
                "effectiveUserMessage": effective_user_message,
            },
            "agentModel": selected_agent_model.id,
            "agentModelLabel": selected_agent_model.label,
            "providerModel": selected_agent_model.provider_model,
            "activeConversationTurns": active_turns,
            "currentItinerarySnapshot": current_snapshot,
            "timelineContext": context_payload.get("timelineContext") or current_snapshot,
            "preferenceCard": preference_card,
            "currentPreferenceSummary": current_preference_summary,
            "memoryText": memory_text,
            "travelPreferenceMemory": {
                "memoryText": memory_text,
                "structuredMemory": structured_memory,
                "compiledRules": memory_rules,
                "pendingConfirmations": memory.pending_confirmations,
                "autoUpdateEnabled": memory.auto_update_enabled,
                "updatedAt": memory.updated_at,
            },
            "structuredMemory": structured_memory,
            "memoryRules": memory_rules,
            "pendingMemoryConfirmations": memory.pending_confirmations,
            "preferenceCardId": context_payload.get("preferenceCardId"),
            "selectedCity": session["city"],
            # Browser map selection is presentation state, not write
            # provenance.  A POI can authorize a patch only through a
            # persisted candidate record (or a server-side resolve result).
            "candidateMapPois": context_payload.get("candidateMapPois") or [],
            "candidatePoiIds": context_payload.get("candidatePoiIds") or [],
            "sourceMaterialIds": [
                str(item)
                for item in context_payload.get("sourceMaterialIds") or []
                if str(item).strip()
            ],
            "retryCandidateHints": retry_candidate_hints,
            "manualCandidateHints": context_payload.get("manualCandidateHints")
            or context_payload.get("manual_candidate_hints")
            or [],
            "selectedAgentChoice": selected_agent_choice,
            "spatialClarificationAnswer": context_payload.get("spatialClarificationAnswer"),
            "pendingAmapPoiCandidates": [
                item.model_dump(by_alias=True)
                for item in ConversationService(self.db)._pending_candidates(session["id"])
            ],
            "planPortfolio": self._plan_portfolio_state(session["id"]),
            "activeDay": context_payload.get("selectedDayNumber"),
            "activeSegment": context_payload.get("selectedSegmentId"),
            "understoodRequirements": understood_requirements,
            "resolvedTripDates": resolved_trip_dates,
            "preferenceImpact": self._preference_impact(current_preference_summary, memory_rules),
            "planningQualityContract": planning_quality_contract,
            "supportedPatchOperations": self.supported_patch_operations,
            "unresolvedQuestions": understood_requirements["clarificationQuestions"],
        }
        return request_context

    @staticmethod
    def _retry_intent_from_conversation_route(
        route: Optional[dict[str, Any]],
        literal_message: str,
    ) -> RetryIntentClassification:
        route = route if isinstance(route, dict) else {}
        raw_classification = route.get("classification")
        classification = (
            ConversationIntentClassification.model_validate(raw_classification)
            if isinstance(raw_classification, dict)
            else None
        )
        intent = classification.intent if classification is not None else None
        intent_class = {
            "modify_itinerary": "modify_request",
            "continue_plan_expansion": "continue_plan_expansion",
            "continue_pending_slot": "continue_pending_choice",
            "manual_candidate_search": "continue_pending_choice",
            "retry_current_stage": "retry_contextual",
            "regenerate_from_scratch": "regenerate_from_scratch",
        }.get(intent, "not_retry")
        outcome_scope = {
            "continue_plan_expansion": "portfolio_expansion",
            "continue_pending_slot": "pending_choice",
            "manual_candidate_search": "pending_choice",
            "retry_current_stage": "exact_component",
            "regenerate_from_scratch": "full_task",
            "modify_itinerary": "full_task",
        }.get(intent, "none")
        return RetryIntentClassification(
            intentClass=intent_class,
            source=(
                "structured_choice"
                if route.get("source") == "persisted_opaque_choice"
                else "explicit_scope"
                if classification is not None
                else "none"
            ),
            literalMessage=literal_message,
            outcomeScope=outcome_scope,
            materialChangeFields=[],
            confidence=classification.confidence if classification is not None else 1.0,
        )

    def _resolve_current_plan_expansion_choice(
        self,
        session: Any,
        *,
        retry_current_stage_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Bind explicit expansion intent to one current persisted capability.

        This lookup never reads labels or user-provided option values.  It only
        locates the newest unconsumed server-issued expansion capability; a
        custom input's source turn never selects an older expansion cursor. The
        existing choice resolver and AgentService still enforce base version,
        planning root, fingerprint, cursor, and portfolio identity.
        """

        session_id = str(session["id"])
        active_version_id = str(session["active_version_id"] or "")
        rows = self.db.execute(
            """SELECT id, turn_index, agent_response_json
            FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status = 'active'
            ORDER BY turn_index DESC, created_at DESC""",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                response = json.loads(row["agent_response_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(response, dict):
                continue
            consumed_ids = {str(item) for item in response.get("consumedChoiceIds") or []}
            matches: list[dict[str, Any]] = []
            for option in response.get("choiceOptions") or []:
                if not isinstance(option, dict):
                    continue
                choice_id = str(option.get("id") or "").strip()
                is_portfolio_expansion = bool(
                    str(option.get("action") or "") == "retry_model_planning"
                    and str(option.get("kind") or "")
                    in {"portfolio_partial_more_plans", "portfolio_more_plans"}
                )
                is_simple_direction_expansion = bool(
                    str(option.get("action") or "") == "continue_plan_expansion"
                    and str(option.get("kind") or "") == "simple_direction_more_plans"
                )
                if (
                    not choice_id
                    or choice_id in consumed_ids
                    or not (is_portfolio_expansion or is_simple_direction_expansion)
                    or str(option.get("expectedBaseVersionId") or "") != active_version_id
                    or any(
                        not str(option.get(key) or "").strip()
                        for key in (
                            "planningSelectionRootTurnId",
                            "rootPortfolioId",
                            "requestContractFingerprint",
                        )
                    )
                    or (is_portfolio_expansion and not str(option.get("focusBriefId") or "").strip())
                    or (retry_current_stage_only and option.get("retryCurrentStageEligible") is not True)
                ):
                    continue
                matches.append(option)
            if len(matches) != 1:
                if matches:
                    return None
                continue
            return self._resolve_selected_agent_choice(
                session,
                {
                    "sourceAssistantTurnId": str(row["id"]),
                    "choiceId": str(matches[0]["id"]),
                },
            )
        return None

    def _plan_portfolio_state(self, session_id: str) -> dict[str, Any]:
        """Expose a bounded status projection, never proposal snapshots, to observation."""
        row = self.db.execute(
            """SELECT p.* FROM agent_plan_portfolios AS p
            LEFT JOIN conversation_turns AS source_user
              ON source_user.id = p.source_user_turn_id
             AND source_user.session_id = p.session_id
            LEFT JOIN conversation_turns AS source_assistant
              ON source_assistant.id = p.source_assistant_turn_id
             AND source_assistant.session_id = p.session_id
            WHERE p.session_id = ?
              AND source_user.status != 'superseded'
              AND (
                p.source_assistant_turn_id IS NULL
                OR source_assistant.status != 'superseded'
              )
            ORDER BY p.created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return {"status": "none"}
        raw = dict(row)
        try:
            summary = json.loads(raw.get("summary_json") or "{}")
        except (TypeError, ValueError):
            summary = {}
        visible_ids = {str(item) for item in summary.get("visibleProposalIds") or [] if str(item)}
        proposals = self.db.execute(
            """SELECT id, brief_json, score_json, status FROM agent_plan_proposals
            WHERE portfolio_id = ? ORDER BY rank_index ASC""",
            (raw["id"],),
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        for proposal in proposals:
            if str(proposal["id"]) not in visible_ids:
                continue
            try:
                brief = json.loads(proposal["brief_json"] or "{}")
                score = json.loads(proposal["score_json"] or "{}")
            except (TypeError, ValueError):
                continue
            summaries.append(
                {
                    "title": str(brief.get("title") or ""),
                    "primaryAxis": str(brief.get("primaryAxis") or ""),
                    "hardConstraintPassed": bool(score.get("hardConstraintPassed")),
                    "status": str(proposal["status"] or ""),
                }
            )
        return {
            "portfolioId": raw["id"],
            "status": raw["status"],
            "proposalCount": len(proposals),
            "feasibleProposalCount": sum(
                1 for proposal in proposals if str(proposal["status"]) in {"offered", "committed"}
            ),
            "visibleProposalCount": len(summaries),
            "selectedProposalId": raw["selected_proposal_id"],
            "expectedBaseVersionId": raw["expected_base_version_id"],
            "sourceUserTurnId": raw["source_user_turn_id"],
            "requestContractFingerprint": raw["request_contract_fingerprint"],
            "proposalSummaries": summaries,
        }

    def _resolve_selected_agent_choice(self, session: Any, raw_choice: Any) -> Optional[dict[str, Any]]:
        """Resolve a click against its persisted assistant turn, never its label."""
        session_id = str(session["id"])
        if raw_choice is None:
            return None
        if not isinstance(raw_choice, dict):
            raise HTTPException(status_code=422, detail={"code": "invalid_agent_choice", "message": "选择格式无效。"})
        source_turn_id = str(raw_choice.get("sourceAssistantTurnId") or "").strip()
        choice_id = str(raw_choice.get("choiceId") or "").strip()
        if not source_turn_id or not choice_id:
            raise HTTPException(status_code=422, detail={"code": "invalid_agent_choice", "message": "缺少选择来源。"})
        row = self.db.execute(
            "SELECT turn_index, agent_response_json, status FROM conversation_turns "
            "WHERE id = ? AND session_id = ? AND role = 'assistant' "
            "AND status IN ('active', 'internal_capability', 'failed')",
            (source_turn_id, session_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "agent_choice_source_not_found", "message": "该选择不属于当前会话或已经失效。"},
            )
        try:
            response = json.loads(row["agent_response_json"] or "{}")
        except (TypeError, ValueError):
            response = {}
        options = response.get("choiceOptions") if isinstance(response, dict) else []
        matching_options = [
            item for item in options if isinstance(item, dict) and str(item.get("id") or "") == choice_id
        ]
        if len(matching_options) != 1:
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_choice_not_available", "message": "该选择已失效，请使用当前回复中的选项。"},
            )
        option = matching_options[0]
        carry_marker = response.get("capabilityCarryForward") or response.get("guideContinuationRecovery") or {}
        if row["status"] == "failed" and not (
            option.get("operationOrigin")
            and isinstance(carry_marker, dict)
            and carry_marker.get("schemaVersion") in {"capability-carry-forward-v1", "guide-continuation-recovery-v1"}
            and carry_marker.get("targetAssistantTurnId") == source_turn_id
            and carry_marker.get("status") == "reissued"
            and carry_marker.get("businessExecutionStarted") is False
        ):
            raise HTTPException(409, {"code": "agent_choice_not_available", "message": "失败回复尚未重新核验此操作，请获取有效操作。"})
        if option.get("operationOrigin") is not None or option.get("guideEvidenceFingerprint"):
            from src.services.conversation_operation_identity import ConversationOperationIdentity
            identities = ConversationOperationIdentity(self.db)
            try:
                execution = identities.offered_execution(session_id, source_turn_id, choice_id)
                if not execution or execution.get("status") != "succeeded":
                    identities.assert_latest(session_id, source_turn_id)
            except ValueError as error:
                raise HTTPException(409, {"code": str(error), "message": "该操作入口已更新，请使用最新回复中的操作。"}) from error
        canonical_choice_id = str(option.get("id") or choice_id)
        action = str(option.get("action") or "").strip()
        consumed = canonical_choice_id in set(response.get("consumedChoiceIds") or [])
        replayable_consumed_action = action in {
            "select_plan_proposal",
            "adopt_active_partial",
            "confirm_portfolio_theme_upgrade",
            "confirm_portfolio_theme_replacement",
            "submit_clarification_batch",
            "continue_plan_expansion",
        } or PortfolioDensityContinuationService.is_density_option(option)
        # A committed portfolio choice is deliberately replayable: execution
        # ownership is still checked by agent_choice_executions and the replay
        # path verifies that the result version is current.  Other consumed
        # actions remain one-shot.
        if consumed and not replayable_consumed_action:
            raise HTTPException(
                status_code=409, detail={"code": "agent_choice_consumed", "message": "该选择已处理，请查看最新结果。"}
            )
        expected_base_version_id = option.get("expectedBaseVersionId")
        if (
            "expectedBaseVersionId" in option
            and str(expected_base_version_id or "") != str(session["active_version_id"] or "")
            and not (consumed and replayable_consumed_action)
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_choice_version_stale", "message": "该选择对应的行程版本已变化。"},
            )
        prior = self.db.execute(
            "SELECT content FROM conversation_turns WHERE session_id = ? AND role = 'user' AND status = 'active' AND turn_index < ? ORDER BY turn_index DESC LIMIT 1",
            (session_id, row["turn_index"]),
        ).fetchone()
        checkpoint = (
            option.get("continuationCheckpoint") if isinstance(option.get("continuationCheckpoint"), dict) else {}
        )
        root_user_turn_id = str(
            option.get("planningSelectionRootTurnId")
            or checkpoint.get("planningSelectionRootTurnId")
            or checkpoint.get("planningRootId")
            or ""
        ).strip()
        root_user_turn = (
            self.db.execute(
                """
                SELECT content FROM conversation_turns
                WHERE id = ? AND session_id = ? AND role = 'user' AND status = 'active'
                """,
                (root_user_turn_id, session_id),
            ).fetchone()
            if root_user_turn_id
            else None
        )
        value = str(option.get("value") or "").strip()
        batch_selections = [
            copy.deepcopy(item)
            for item in raw_choice.get("batchSelections") or []
            if isinstance(item, dict)
        ]
        manual_input_kind = str(option.get("kind") or "") in {
            "custom_input",
            "simple_direction_constraint_modification",
        }
        manual_value = str(raw_choice.get("manualValue") or "").strip() if manual_input_kind else ""
        if action == "manual_continuation" and manual_input_kind and not manual_value:
            raise HTTPException(
                status_code=422, detail={"code": "agent_choice_manual_value_required", "message": "请输入补充内容。"}
            )
        # Portfolio continuations are children of one immutable planning root.
        # The immediately preceding user turn may itself be only “继续生成”, so
        # it must never replace the semantic request used for dates, party,
        # budget, transport or the request-contract fingerprint.
        original_message = str(
            root_user_turn["content"] if root_user_turn is not None else prior["content"] if prior is not None else ""
        ).strip()
        if action in {"retry_model_planning", "confirm_rule_safe_draft"}:
            semantic_message = original_message
        else:
            semantic_message = "\n".join(part for part in (original_message, value, manual_value) if part)
        return {
            "sourceAssistantTurnId": source_turn_id,
            "choiceId": canonical_choice_id,
            "action": action or None,
            "requestChoiceId": choice_id,
            "persistedChoiceId": canonical_choice_id,
            "persistedChoiceAction": action or None,
            "option": option,
            "manualValue": manual_value or None,
            "batchSelections": batch_selections,
            "semanticMessage": semantic_message or value,
            "rootUserTurnId": root_user_turn_id or None,
        }

    def _active_turns(self, session_id: str) -> list[dict]:
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "turnIndex": row["turn_index"],
                "itineraryVersionId": row["itinerary_version_id"],
            }
            for row in self.db.execute(
                """
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND status = 'active'
                ORDER BY turn_index ASC, created_at ASC
                """,
                (session_id,),
            ).fetchall()
        ]

    def _preference_context(self, context_payload: dict, session_id: str) -> tuple[str, str, Any]:
        raw_preference_summary = (
            context_payload.get("currentPreferenceSummary")
            or context_payload.get("preferenceSummary")
            or (context_payload.get("preferenceCard") or {}).get("summaryText")
            or ""
        )
        memory = PreferenceService(self.db).get_memory(session_id=session_id, commit=False)
        memory_text = PreferenceService.effective_memory_text(memory.memory_text)
        current_preference_summary = PreferenceService.effective_memory_text(str(raw_preference_summary or ""))
        if not current_preference_summary:
            current_preference_summary = memory_text
        return current_preference_summary, memory_text, memory

    def _preference_card(self, context_payload: dict) -> Optional[dict]:
        preference_card = context_payload.get("preferenceCard")
        if not isinstance(preference_card, dict):
            return preference_card
        summary_text = PreferenceService.effective_memory_text(str(preference_card.get("summaryText") or ""))
        if not summary_text:
            return None
        return {
            "id": preference_card.get("id"),
            "profileId": preference_card.get("profileId") or preference_card.get("profile_id"),
            "summaryText": summary_text,
            "status": preference_card.get("status"),
        }

    def _planning_quality_contract(
        self,
        latest_message: str,
        understood_requirements: dict[str, Any],
        current_preference_summary: str,
        memory_rules: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        text = f"{latest_message}\n{current_preference_summary}".lower()
        rules = memory_rules if isinstance(memory_rules, dict) else {}
        pace_rules = rules.get("pace") if isinstance(rules.get("pace"), dict) else {}
        route_rules = rules.get("routePlanning") if isinstance(rules.get("routePlanning"), dict) else {}
        meal_rules = rules.get("mealHandling") if isinstance(rules.get("mealHandling"), dict) else {}
        risk_rules = rules.get("riskChecks") if isinstance(rules.get("riskChecks"), dict) else {}
        relaxed = bool(pace_rules.get("relaxed")) or any(
            marker in text for marker in ("轻松", "不赶", "慢", "亲子", "老人", "relaxed")
        )
        max_visit_segments = int(pace_rules.get("maxVisitSegmentsPerDay") or (3 if relaxed else 5))
        soft_max_segments = int(pace_rules.get("softMaxTotalSegmentsPerDay") or (5 if relaxed else 6))
        if relaxed and not bool(pace_rules.get("relaxed")):
            max_visit_segments = min(max_visit_segments, 3)
            soft_max_segments = min(soft_max_segments, 5)
        return {
            "version": "planning-quality-p1-v1",
            "memoryRulesVersion": rules.get("version"),
            "compiledFromMemoryFactIds": rules.get("factsApplied") or [],
            "dailyStructure": {
                "requiredAnchors": ["morning", "noon", "evening"],
                "includeMeals": True,
                "includeRestBuffers": bool(pace_rules.get("requireMealOrRestBuffer", True)),
                "includeTransportBetweenAreas": True,
            },
            "memoryExecutionRules": {
                "poiSelection": rules.get("poiSelection") or {},
                "routePlanning": route_rules,
                "mealHandling": meal_rules,
                "riskChecks": risk_rules,
            },
            "segmentRequirements": {
                "visitSegmentsNeedDurationMinutes": True,
                "segmentsNeedEstimatedCostOrNull": True,
                "notesMustCoverPracticalUse": ["reservation", "weather", "crowding", "transport", "needs_verification"],
                "pureMealLabelsAreNotPois": bool(meal_rules.get("pureMealLabelsAreNotPois", True)),
            },
            "paceRules": {
                "relaxedRequested": relaxed,
                "maxVisitSegmentsPerDay": max_visit_segments,
                "softMaxTotalSegmentsPerDay": soft_max_segments,
                "avoidBackToBackLongVisits": True,
                "keepMealOrRestWhenDayHasThreeOrMoreVisits": bool(pace_rules.get("requireMealOrRestBuffer", True)),
            },
            "factGrounding": {
                "realtimeFactsRequireTools": ["ticket", "reservation", "opening_hours", "weather", "route_feasibility"],
                "fallbackFactsMustSetNeedsVerification": True,
                "doNotInventOfficialTicketOrWeatherSuccess": True,
            },
            "comparisonPolicy": {
                "readOnlyByDefault": True,
                "requiresExplicitApplyRequestBeforePatch": True,
            },
            "verifierSoftChecks": [
                "thin_day",
                "overpacked_day",
                "missing_practical_notes",
                "unresolved_reservation_risk",
                "weak_day_theme",
            ],
            "understoodRequirementsSummary": understood_requirements.get("summary")
            if isinstance(understood_requirements, dict)
            else None,
        }

    def _preference_impact(
        self, current_preference_summary: str, memory_rules: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        base = self.preference_impact_analyzer(current_preference_summary)
        rules = memory_rules if isinstance(memory_rules, dict) else {}
        if not isinstance(base, dict):
            base = {"summary": str(base)}
        return {
            **base,
            "compiledRulesApplied": bool(rules.get("factsApplied")),
            "appliedRules": rules.get("factsApplied") or base.get("appliedRules") or [],
            "routePlanning": rules.get("routePlanning") or {},
            "mealHandling": rules.get("mealHandling") or {},
            "pace": rules.get("pace") or {},
            "riskChecks": rules.get("riskChecks") or {},
        }

    def _default_requirement_analyzer(
        self,
        latest_message: str,
        city: str,
        current_snapshot: Optional[dict],
        current_preference_summary: str,
    ) -> dict[str, Any]:
        return {
            "summary": latest_message,
            "city": city,
            "hasCurrentItinerary": current_snapshot is not None,
            "hasPreferenceSummary": bool(current_preference_summary),
            "missingFields": [],
            "clarificationQuestions": [],
            "isCompleteEnoughToPlan": True,
        }

    def _default_preference_impact_analyzer(self, current_preference_summary: str) -> dict[str, Any]:
        return {
            "hasPreferenceSummary": bool(current_preference_summary),
            "appliedRules": [],
        }

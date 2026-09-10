import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.agent import (
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentSessionSummaryResponse,
    ConversationTurnResponse,
    PendingPoiCandidateResponse,
    project_comparison_summary,
    project_persisted_identity,
)
from src.api.schemas.planning import PlanningRunResponse
from src.core.config import get_settings
from src.core.database import PROJECT_ROOT
from src.services.agent_event_compat import normalize_persisted_planning_events
from src.services.agent_choice_trace_service import (
    build_structured_choice_trace,
    comparison_projection_update_mode,
)
from src.services.agent_run_control import acquire_session_run, release_session_run
from src.services.agent_reasoning_status_service import sanitize_execution_events
from src.services.clarification_checkpoint_service import (
    ClarificationCheckpointService,
)
from src.services.itinerary_service import ItineraryService
from src.services.preference_service import PreferenceService
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.proposal_visit_facts_service import ProposalVisitFactsService
from src.services.simple_direction_execution_evidence_service import (
    SimpleDirectionExecutionEvidenceService,
)
from src.services.simple_open_direction_service import SimpleOpenDirectionService


class ConversationService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_session(
        self,
        city: str,
        title: Optional[str] = None,
        *,
        include_editable_draft: bool = False,
    ) -> AgentSessionResponse:
        settings = get_settings()
        now = datetime.now(timezone.utc).isoformat()
        session_id = f"sess_{uuid4().hex[:12]}"
        plan_id = f"plan_{uuid4().hex[:12]}"
        session_title = title or f"{city} AI 行程"
        self.db.execute(
            """
            INSERT INTO itinerary_plans (
                id, user_id, inspiration_set_id, template_type, title, city,
                budget_target, budget_estimate, budget_delta_explanation,
                decision_rationale, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                settings.default_user_id,
                session_id,
                "agent_mvp",
                session_title,
                city,
                None,
                0,
                "Agent MVP 行程将随对话和手动编辑持续更新。",
                "Agent MVP 会话创建的空行程容器。",
                "draft",
                now,
                now,
            ),
        )
        self.db.execute(
            """
            INSERT INTO conversation_sessions (
                id, user_id, title, city, active_plan_id, active_version_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                settings.default_user_id,
                session_title,
                city,
                plan_id,
                None,
                "active",
                now,
                now,
            ),
        )
        if include_editable_draft:
            self.ensure_editable_draft_day(plan_id)
        self.db.commit()
        return self.get_session(session_id)

    def ensure_editable_draft_day(self, plan_id: str) -> None:
        exists = self.db.execute("SELECT 1 FROM itinerary_days WHERE plan_id = ? LIMIT 1", (plan_id,)).fetchone()
        if exists is not None:
            return
        self.db.execute(
            """
            INSERT INTO itinerary_days (
                id, plan_id, day_number, date, title, weather_summary,
                risk_summary, total_estimated_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"day_{uuid4().hex[:12]}",
                plan_id,
                1,
                None,
                "Day 1 待规划",
                "等待 Agent 或用户补充天气信息。",
                "等待 Agent 查询开放、预约和拥挤风险。",
                0,
            ),
        )

    def list_sessions(self) -> AgentSessionListResponse:
        settings = get_settings()
        rows = self.db.execute(
            """
            SELECT
                s.*,
                COUNT(t.id) AS turn_count
            FROM conversation_sessions s
            LEFT JOIN conversation_turns t ON t.session_id = s.id
              AND t.status NOT IN ('superseded', 'internal_capability')
            WHERE s.user_id = ? AND s.status = 'active'
            GROUP BY s.id
            ORDER BY s.updated_at DESC, s.created_at DESC
            """,
            (settings.default_user_id,),
        ).fetchall()
        return AgentSessionListResponse(
            sessions=[
                AgentSessionSummaryResponse(
                    sessionId=row["id"],
                    status=row["status"],
                    city=row["city"],
                    title=row["title"],
                    activePlanId=row["active_plan_id"],
                    activeVersionId=row["active_version_id"],
                    turnCount=int(row["turn_count"] or 0),
                    updatedAt=row["updated_at"],
                    createdAt=row["created_at"],
                )
                for row in rows
            ]
        )

    def delete_session(self, session_id: str) -> AgentSessionListResponse:
        settings = get_settings()
        session = self.db.execute(
            "SELECT * FROM conversation_sessions WHERE id = ? AND user_id = ?",
            (session_id, settings.default_user_id),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="Conversation session not found")
        if not acquire_session_run(session_id):
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_run_in_progress", "message": "当前对话仍在规划中，请等待完成或先停止。"},
            )
        try:
            material_paths = [
                path
                for row in self.db.execute(
                    "SELECT original_path, thumbnail_path FROM source_materials WHERE inspiration_set_id = ?",
                    (session_id,),
                ).fetchall()
                for path in (row["original_path"], row["thumbnail_path"])
                if path
            ]
            plan_id = str(session["active_plan_id"])
            self.db.execute(
                "DELETE FROM proposal_segment_visit_facts WHERE session_id = ?",
                (session_id,),
            )
            self.db.execute(
                "DELETE FROM agent_plan_proposals WHERE portfolio_id IN "
                "(SELECT id FROM agent_plan_portfolios WHERE session_id = ?)",
                (session_id,),
            )
            self.db.execute("DELETE FROM agent_plan_portfolios WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM agent_choice_executions WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM amap_poi_candidates WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM session_preference_memories WHERE session_id = ?", (session_id,))
            self.db.execute(
                "DELETE FROM planning_runs WHERE itinerary_plan_id = ? OR itinerary_version_id IN "
                "(SELECT id FROM itinerary_versions WHERE session_id = ?)",
                (plan_id, session_id),
            )
            self.db.execute("DELETE FROM saved_itinerary_versions WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM timeline_mutation_transactions WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM itinerary_patches WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM itinerary_versions WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM conversation_turns WHERE session_id = ?", (session_id,))
            self.db.execute("DELETE FROM reminder_drafts WHERE itinerary_plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM plan_comparisons WHERE inspiration_set_id = ?", (session_id,))
            self.db.execute(
                "DELETE FROM traffic_crowding_signals WHERE route_option_id IN "
                "(SELECT id FROM route_options WHERE plan_id = ?)",
                (plan_id,),
            )
            self.db.execute(
                "DELETE FROM ticket_lookup_results WHERE segment_id IN "
                "(SELECT id FROM itinerary_segments WHERE plan_id = ?)",
                (plan_id,),
            )
            self.db.execute("DELETE FROM poi_risk_alerts WHERE plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM route_options WHERE plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM weather_signals WHERE plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM itinerary_segments WHERE plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM itinerary_days WHERE plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM pois WHERE plan_id = ?", (plan_id,))
            self.db.execute("DELETE FROM itinerary_plans WHERE id = ?", (plan_id,))
            self.db.execute("DELETE FROM extraction_results WHERE inspiration_set_id = ?", (session_id,))
            self.db.execute("DELETE FROM source_materials WHERE inspiration_set_id = ?", (session_id,))
            self.db.execute("DELETE FROM inspiration_sets WHERE id = ?", (session_id,))
            self.db.execute("DELETE FROM conversation_sessions WHERE id = ?", (session_id,))
            self.db.commit()
            self._delete_session_material_files(material_paths)
        except Exception:
            self.db.rollback()
            raise
        finally:
            release_session_run(session_id)
        return self.list_sessions()

    @staticmethod
    def _delete_session_material_files(paths: list[str]) -> None:
        project_root = PROJECT_ROOT.resolve()
        for raw_path in paths:
            candidate = (project_root / Path(raw_path)).resolve()
            try:
                candidate.relative_to(project_root)
            except ValueError:
                continue
            try:
                if candidate.is_file():
                    candidate.unlink()
            except OSError:
                # The database deletion remains authoritative. A transient
                # filesystem error must not restore the deleted conversation.
                continue

    def get_session(self, session_id: str) -> AgentSessionResponse:
        session = self.db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="Conversation session not found")
        return self._session_response(session)

    def get_current_session(self) -> AgentSessionResponse:
        settings = get_settings()
        session = self.db.execute(
            """
            SELECT * FROM conversation_sessions
            WHERE user_id = ? AND status = 'active'
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (settings.default_user_id,),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="No active conversation session")
        return self._session_response(session)

    def _session_response(self, session: sqlite3.Row) -> AgentSessionResponse:
        itinerary = None
        if session["active_version_id"] or self._has_read_model(session["active_plan_id"]):
            itinerary = ItineraryService(self.db).get_plan(session["active_plan_id"])
        return AgentSessionResponse(
            session_id=session["id"],
            status=session["status"],
            city=session["city"],
            title=session["title"],
            active_plan_id=session["active_plan_id"],
            active_version_id=session["active_version_id"],
            turns=self._turns(session["id"]),
            itinerary=itinerary,
            pending_poi_candidates=self._pending_candidates(session["id"]),
            preference_memory=PreferenceService(self.db).get_memory(session_id=session["id"], commit=False),
            planning_run=self._latest_planning_run(session),
        )

    def _has_read_model(self, plan_id: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM itinerary_days WHERE plan_id = ? LIMIT 1", (plan_id,)).fetchone() is not None
        )

    def _turns(self, session_id: str) -> list[ConversationTurnResponse]:
        try:
            SimpleOpenDirectionService(self.db).reconcile_legacy_guide_capability_carrier(
                session_id=session_id
            )
        except ValueError:
            # A reload may observe a concurrent turn/capability rotation.  The
            # reconciler is CAS-bound and fail-closed, so expose the unchanged
            # persisted turns instead of guessing an older capability.
            self.db.rollback()
        rows = self.db.execute(
            """
            SELECT * FROM conversation_turns
            WHERE session_id = ?
            ORDER BY turn_index ASC, created_at ASC
            """,
            (session_id,),
        ).fetchall()
        recoveries = self._clarification_recovery_envelopes(session_id)
        recovery_by_source = {str(item["sourceAssistantTurnId"]): item for item in recoveries}
        recovery_by_user = {str(item["sourceUserTurnId"]): item for item in recoveries}
        turns: list[ConversationTurnResponse] = []
        for row in rows:
            if str(row["role"] or "") == "assistant":
                row = self._simple_direction_reconciled_turn(row)
            structured_choice_trace = build_structured_choice_trace(self.db, row)
            response_payload = self._response_payload_from_turn(row)
            clarification_recovery = (
                recovery_by_source.get(str(row["id"] or ""))
                if str(row["role"] or "") == "assistant"
                else recovery_by_user.get(str(row["id"] or ""))
                if str(row["role"] or "") == "user"
                else None
            )
            response_payload = self._response_payload_with_clarification_recovery(
                response_payload,
                clarification_recovery,
            )
            clarification_checkpoint = response_payload.get("clarificationCheckpoint")
            experience_specs = response_payload.get("experienceSpecs")
            candidate_gap_summary = response_payload.get("candidateGapSummary")
            spatial_boundary_preview = response_payload.get("spatialBoundaryPreview")
            turns.append(
                ConversationTurnResponse(
                    id=row["id"],
                    role=row["role"],
                    content=row["content"],
                    turn_index=row["turn_index"],
                    status=row["status"],
                    parent_turn_id=row["parent_turn_id"],
                    itinerary_version_id=row["itinerary_version_id"],
                    planning_run_id=row["planning_run_id"],
                    comparison_projections=self._comparison_projections_from_turn(row),
                    comparison_projection_update_mode=comparison_projection_update_mode(
                        self._response_payload_from_turn(row),
                        structured_choice_trace,
                        row["id"],
                    ),
                    planning_selection_root_turn_id=project_persisted_identity(
                        response_payload.get("planningSelectionRootTurnId")
                    ),
                    root_portfolio_id=project_persisted_identity(
                        response_payload.get("rootPortfolioId")
                    ),
                    comparison_summary=project_comparison_summary(
                        response_payload.get("comparisonSummary")
                    ),
                    choice_options=self._choice_options_from_turn(
                        row,
                        clarification_recovery=(
                            clarification_recovery if str(row["role"] or "") == "assistant" else None
                        ),
                    ),
                    clarification_checkpoint=(
                        clarification_checkpoint if isinstance(clarification_checkpoint, dict) else None
                    ),
                    clarification_submission=(
                        self._clarification_submission_projection(row, response_payload)
                        if str(row["role"] or "") == "assistant"
                        else None
                    ),
                    shared_source=response_payload.get("sharedSource") if isinstance(response_payload.get("sharedSource"), dict) else None,
                    guide_advice=(
                        response_payload.get("guideAdvice")
                        if response_payload.get("mode") != "shared_travel_source" and isinstance(response_payload.get("guideAdvice"), dict)
                        else None
                    ),
                    experience_specs=(
                        [dict(item) for item in experience_specs if isinstance(item, dict)]
                        if isinstance(experience_specs, list)
                        else []
                    ),
                    candidate_gap_summary=(candidate_gap_summary if isinstance(candidate_gap_summary, dict) else None),
                    spatial_boundary_preview=(
                        spatial_boundary_preview if isinstance(spatial_boundary_preview, dict) else None
                    ),
                    local_poi_options=self._local_poi_options_from_turn(row),
                    structured_choice_trace=structured_choice_trace,
                    planning_steps=self._planning_events_from_turn(row, "planningSteps"),
                    tool_events=self._planning_events_from_turn(row, "toolEvents"),
                    reasoning_statuses=(
                        sanitize_execution_events(
                            item
                            for item in response_payload.get("reasoningStatuses") or []
                            if isinstance(item, dict)
                        )
                    ),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        return turns

    def _clarification_submission_projection(
        self,
        source_row: sqlite3.Row,
        source_response: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Project a completed batch without mutating its signed source card.

        The read model is deliberately derived from four durable facts: the
        issued checkpoint, its opaque submit capability, the successful
        execution claim, and the signed answered checkpoint on the request
        user turn.  A partial or tampered chain remains unprojected.
        """

        source_turn_id = str(source_row["id"] or "")
        session_id = str(source_row["session_id"] or "")
        source_checkpoint = source_response.get("clarificationCheckpoint")
        if (
            not isinstance(source_checkpoint, dict)
            or str(source_checkpoint.get("schemaVersion") or "") != ClarificationCheckpointService.SCHEMA_VERSION
            or str(source_checkpoint.get("status") or "") != "awaiting_answer"
            or str(source_checkpoint.get("sourceAssistantTurnId") or "") != source_turn_id
            or not self._valid_clarification_checkpoint(source_checkpoint)
        ):
            return None
        checkpoint_id = str(source_checkpoint.get("checkpointId") or "")
        checkpoint_fingerprint = str(source_checkpoint.get("fingerprint") or "")
        submit_choice_id = str(source_checkpoint.get("submitChoiceId") or "")
        request_fingerprint = str(source_checkpoint.get("requestFingerprint") or "")
        questions = [item for item in source_checkpoint.get("questions") or [] if isinstance(item, dict)]
        if not checkpoint_id or not submit_choice_id or not request_fingerprint or not questions:
            return None

        matching_options = [
            item
            for item in source_response.get("choiceOptions") or []
            if isinstance(item, dict)
            and str(item.get("id") or "") == submit_choice_id
            and str(item.get("kind") or "") == "clarification_batch_submit"
            and str(item.get("action") or "") == "submit_clarification_batch"
            and str(item.get("checkpointId") or "") == checkpoint_id
            and str(item.get("checkpointFingerprint") or "") == checkpoint_fingerprint
            and str(item.get("sourceAssistantTurnId") or source_turn_id) == source_turn_id
        ]
        if len(matching_options) != 1:
            return None

        execution = self.db.execute(
            """
            SELECT * FROM agent_choice_executions
            WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?
              AND action = 'submit_clarification_batch' AND status = 'succeeded'
            """,
            (session_id, source_turn_id, submit_choice_id),
        ).fetchone()
        if (
            execution is None
            or str(execution["checkpoint_fingerprint"] or "") != checkpoint_fingerprint
            or not str(execution["request_turn_id"] or "")
        ):
            return None
        request_turn_id = str(execution["request_turn_id"])
        request_row = self.db.execute(
            """
            SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'user' AND status != 'superseded'
            """,
            (request_turn_id, session_id),
        ).fetchone()
        if request_row is None or int(request_row["turn_index"]) <= int(source_row["turn_index"]):
            return None
        request_context = self._json_dict(request_row["agent_request_json"])
        answered = request_context.get("clarificationCheckpoint")
        request_contract = request_context.get("requestIntentContract")
        selected = request_context.get("selectedAgentChoice")
        selected_option = selected.get("option") if isinstance(selected, dict) else None
        if (
            not isinstance(answered, dict)
            or not isinstance(request_contract, dict)
            or not isinstance(selected, dict)
            or not isinstance(selected_option, dict)
            or str(selected.get("sourceAssistantTurnId") or "") != source_turn_id
            or str(selected.get("choiceId") or "") != submit_choice_id
            or str(selected_option.get("id") or "") != submit_choice_id
            or str(selected_option.get("kind") or "") != "clarification_batch_submit"
            or str(selected_option.get("action") or "") != "submit_clarification_batch"
            or str(selected_option.get("checkpointId") or "") != checkpoint_id
            or str(selected_option.get("checkpointFingerprint") or "") != checkpoint_fingerprint
            or str(answered.get("schemaVersion") or "") != ClarificationCheckpointService.SCHEMA_VERSION
            or str(answered.get("status") or "") != "answered"
            or str(answered.get("checkpointId") or "") != checkpoint_id
            or str(answered.get("sourceAssistantTurnId") or "") != source_turn_id
            or str(answered.get("sourceUserTurnId") or "") != request_turn_id
            or str(answered.get("planningRootId") or "") != str(source_checkpoint.get("planningRootId") or "")
            or str(answered.get("requestFingerprint") or "") != request_fingerprint
            or not self._valid_clarification_checkpoint(answered)
            or not ClarificationCheckpointService._valid_prior_checkpoint(
                answered,
                planning_root_id=str(source_checkpoint.get("planningRootId") or ""),
                request_contract=request_contract,
            )
        ):
            return None

        resolved_answers = [item for item in answered.get("resolvedAnswers") or [] if isinstance(item, dict)]
        contract_answers = [
            item for item in request_contract.get("clarificationAnswers") or [] if isinstance(item, dict)
        ]
        if ClarificationCheckpointService._fingerprint({"answers": resolved_answers}) != (
            ClarificationCheckpointService._fingerprint({"answers": contract_answers})
        ):
            return None

        question_dimensions = {
            str(question.get("dimensionId") or "") for question in questions if str(question.get("dimensionId") or "")
        }
        if len(question_dimensions) != len(questions):
            return None
        by_dimension: dict[str, dict[str, Any]] = {}
        for answer in resolved_answers:
            dimension_id = str(answer.get("dimensionId") or "")
            if dimension_id not in question_dimensions:
                continue
            if dimension_id in by_dimension:
                return None
            by_dimension[dimension_id] = answer
        projected_answers: list[dict[str, Any]] = []
        expected_dimensions: set[str] = set()
        for question in questions:
            dimension_id = str(question.get("dimensionId") or "")
            if not dimension_id or dimension_id in expected_dimensions:
                return None
            expected_dimensions.add(dimension_id)
            answer = by_dimension.get(dimension_id)
            if answer is None:
                return None
            option_id = str(answer.get("optionId") or "").strip()
            label = str(answer.get("label") or "").strip()
            source = str(answer.get("source") or "").strip()
            if not label or not source:
                return None
            projected: dict[str, Any] = {
                "dimensionId": dimension_id,
                "label": label,
                "source": source,
            }
            if option_id:
                option_matches = [
                    option
                    for option in question.get("options") or []
                    if isinstance(option, dict) and str(option.get("id") or "") == option_id
                ]
                if len(option_matches) != 1 or str(option_matches[0].get("label") or "") != label:
                    return None
                projected["optionId"] = option_id
            elif source != "free_text_normalized" or not bool(question.get("allowFreeText")):
                return None
            projected_answers.append(projected)
        if set(by_dimension) != expected_dimensions:
            return None
        return {
            "checkpointId": checkpoint_id,
            "sourceAssistantTurnId": source_turn_id,
            "requestUserTurnId": request_turn_id,
            "executionId": str(execution["id"]),
            "status": "succeeded",
            "answers": projected_answers,
        }

    def _response_payload_from_turn(self, row: sqlite3.Row) -> dict:
        if not row["agent_response_json"]:
            return {}
        try:
            payload = json.loads(row["agent_response_json"])
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _response_payload_with_clarification_recovery(
        response_payload: dict[str, Any],
        recovery: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(recovery, dict):
            return response_payload
        request_context = recovery.get("requestContext")
        if not isinstance(request_context, dict):
            return response_payload
        merged = dict(response_payload)
        checkpoint = request_context.get("clarificationCheckpoint")
        if isinstance(checkpoint, dict):
            merged["clarificationCheckpoint"] = copy.deepcopy(checkpoint)
        specs = request_context.get("experienceSpecs")
        if isinstance(specs, list):
            merged["experienceSpecs"] = [copy.deepcopy(item) for item in specs if isinstance(item, dict)]
        gap = request_context.get("candidateGapSummary")
        if isinstance(gap, dict):
            merged["candidateGapSummary"] = copy.deepcopy(gap)
        return merged

    def latest_clarification_recovery_envelope(
        self,
        session_id: str,
        *,
        source_assistant_turn_id: Optional[str] = None,
        source_user_turn_id: Optional[str] = None,
        unmaterialized_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Return the newest durable answer envelope that is safe to resume.

        A clarification answer is committed on the user turn before the next
        Controller call.  If that call is interrupted, this envelope is the
        authoritative recovery point.  Validation binds it to the persisted
        source question, planning root, request contract, checkpoint
        fingerprint, and canonical request copy before it is exposed.
        """

        for envelope in self._clarification_recovery_envelopes(
            session_id,
            source_assistant_turn_id=source_assistant_turn_id,
            source_user_turn_id=source_user_turn_id,
        ):
            if unmaterialized_only and self._clarification_recovery_has_later_assistant(envelope):
                continue
            return envelope
        return None

    def _clarification_recovery_envelopes(
        self,
        session_id: str,
        *,
        source_assistant_turn_id: Optional[str] = None,
        source_user_turn_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT * FROM conversation_turns
            WHERE session_id = ?
              AND role = 'user'
              AND status != 'superseded'
              AND agent_request_json IS NOT NULL
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 24
            """,
            (session_id,),
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for row in rows:
            envelope = self._validated_clarification_request_envelope(
                row,
                session_id=session_id,
            )
            if envelope is None:
                continue
            source_turn_id = str(envelope["sourceAssistantTurnId"])
            if source_assistant_turn_id is not None and source_turn_id != str(source_assistant_turn_id):
                continue
            if source_user_turn_id is not None and str(envelope["sourceUserTurnId"]) != str(source_user_turn_id):
                continue
            if source_turn_id in seen_sources:
                continue
            seen_sources.add(source_turn_id)
            recovered.append(envelope)
        return recovered

    def _clarification_recovery_has_later_assistant(self, envelope: dict[str, Any]) -> bool:
        """Return whether a newer assistant turn supersedes this fallback.

        Once any later assistant turn has materialized, its persisted request is
        the newer continuation source. Reusing the older answer envelope could
        otherwise roll a later checkpoint back to an already-consumed question.
        """

        session_id = envelope.get("sessionId")
        if not session_id:
            return True
        return (
            self.db.execute(
                """
                SELECT 1 FROM conversation_turns
                WHERE session_id = ?
                  AND role = 'assistant'
                  AND status != 'superseded'
                  AND turn_index > ?
                LIMIT 1
                """,
                (str(session_id), int(envelope.get("turnIndex") or 0)),
            ).fetchone()
            is not None
        )

    def _validated_clarification_request_envelope(
        self,
        row: sqlite3.Row,
        *,
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        if str(row["session_id"] or "") != str(session_id or "") or str(row["role"] or "") != "user":
            return None
        request_context = self._json_dict(row["agent_request_json"])
        checkpoint = (
            request_context.get("clarificationCheckpoint")
            if isinstance(request_context.get("clarificationCheckpoint"), dict)
            else {}
        )
        selected = (
            request_context.get("selectedAgentChoice")
            if isinstance(request_context.get("selectedAgentChoice"), dict)
            else {}
        )
        selected_option = selected.get("option") if isinstance(selected.get("option"), dict) else {}
        source_turn_id = str(selected.get("sourceAssistantTurnId") or "").strip()
        choice_id = str(selected.get("persistedChoiceId") or selected.get("choiceId") or "").strip()
        if (
            not source_turn_id
            or not choice_id
            or str(request_context.get("sessionId") or "") != str(session_id or "")
            or str(checkpoint.get("status") or "") not in {"answered", "awaiting_agent_resolution"}
            or str(checkpoint.get("sourceUserTurnId") or "") != str(row["id"] or "")
            or str(checkpoint.get("sourceAssistantTurnId") or "") != source_turn_id
            or not self._valid_clarification_checkpoint(checkpoint)
        ):
            return None
        source_row = self.db.execute(
            """
            SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ?
              AND role = 'assistant' AND status != 'superseded'
            """,
            (source_turn_id, session_id),
        ).fetchone()
        if source_row is None or int(source_row["turn_index"]) >= int(row["turn_index"]):
            return None
        source_response = self._response_payload_from_turn(source_row)
        source_checkpoint = (
            source_response.get("clarificationCheckpoint")
            if isinstance(source_response.get("clarificationCheckpoint"), dict)
            else {}
        )
        source_request = self._json_dict(source_row["agent_request_json"])
        source_contract = (
            source_request.get("requestIntentContract")
            if isinstance(source_request.get("requestIntentContract"), dict)
            else {}
        )
        if (
            not source_contract
            or not self._valid_clarification_checkpoint(source_checkpoint)
            or str(source_checkpoint.get("status") or "") != "awaiting_answer"
            or str(source_checkpoint.get("sourceAssistantTurnId") or "") != source_turn_id
            or str(source_checkpoint.get("checkpointId") or "") != str(checkpoint.get("checkpointId") or "")
            or str(source_checkpoint.get("planningRootId") or "") != str(checkpoint.get("planningRootId") or "")
            or str(source_checkpoint.get("requestFingerprint") or "") != str(checkpoint.get("requestFingerprint") or "")
            or str(source_checkpoint.get("requestFingerprint") or "")
            != ClarificationCheckpointService._fingerprint(source_contract)
        ):
            return None
        source_options = [item for item in source_response.get("choiceOptions") or [] if isinstance(item, dict)]
        persisted_option = next(
            (item for item in source_options if str(item.get("id") or "") == choice_id),
            None,
        )
        if not isinstance(persisted_option, dict):
            return None
        current_dimension = str(source_checkpoint.get("nextQuestionDimensionId") or "")
        if (
            str(persisted_option.get("action") or "") != "continue_clarification"
            or str(persisted_option.get("kind") or "") not in {"clarification_checkpoint", "custom_input"}
            or str(persisted_option.get("checkpointId") or "") != str(source_checkpoint.get("checkpointId") or "")
            or str(persisted_option.get("dimensionId") or "") != current_dimension
            or str(selected_option.get("id") or "") != choice_id
            or str(selected_option.get("action") or "") != str(persisted_option.get("action") or "")
            or str(selected_option.get("kind") or "") != str(persisted_option.get("kind") or "")
            or str(selected_option.get("checkpointId") or "") != str(persisted_option.get("checkpointId") or "")
            or str(selected_option.get("dimensionId") or "") != current_dimension
        ):
            return None
        planning_root_id = str(checkpoint.get("planningRootId") or "")
        if not planning_root_id or str(request_context.get("planningSelectionRootTurnId") or "") != planning_root_id:
            return None
        request_contract = (
            request_context.get("requestIntentContract")
            if isinstance(request_context.get("requestIntentContract"), dict)
            else {}
        )
        canonical = (
            request_context.get("canonicalRequestContext")
            if isinstance(request_context.get("canonicalRequestContext"), dict)
            else {}
        )
        canonical_checkpoint = (
            canonical.get("clarificationCheckpoint")
            if isinstance(canonical.get("clarificationCheckpoint"), dict)
            else {}
        )
        if not request_contract or canonical.get("requestIntentContract") != request_contract:
            return None
        experience_specs = request_context.get("experienceSpecs")
        if isinstance(experience_specs, list) and canonical.get("experienceSpecs") != experience_specs:
            return None
        candidate_gap = request_context.get("candidateGapSummary")
        if isinstance(candidate_gap, dict) and canonical.get("candidateGapSummary") != candidate_gap:
            return None
        if str(checkpoint.get("status") or "") == "answered":
            if (
                canonical_checkpoint != checkpoint
                or str(request_contract.get("clarificationCheckpointId") or "")
                != str(checkpoint.get("checkpointId") or "")
                or int(request_contract.get("clarificationContractVersion") or 0)
                != int(checkpoint.get("contractVersion") or 0)
                or request_contract.get("clarificationAnswers") != checkpoint.get("resolvedAnswers")
                or request_contract.get("experienceSpecs") != checkpoint.get("experienceSpecs")
            ):
                return None
        else:
            pending = (
                checkpoint.get("pendingFreeTextAnswer")
                if isinstance(checkpoint.get("pendingFreeTextAnswer"), dict)
                else {}
            )
            if (
                canonical_checkpoint not in ({}, checkpoint, source_checkpoint)
                or str(pending.get("sourceUserTurnId") or "") != str(row["id"] or "")
                or str(pending.get("dimensionId") or "") != current_dimension
                or not str(pending.get("text") or "").strip()
                or str(selected.get("manualValue") or "").strip() != str(pending.get("text") or "").strip()
                or request_context.get("clarificationFreeTextCandidate") != pending
                or ClarificationCheckpointService._fingerprint(request_contract)
                != str(checkpoint.get("requestFingerprint") or "")
            ):
                return None
        recovered_context = copy.deepcopy(request_context)
        recovered_canonical = recovered_context["canonicalRequestContext"]
        recovered_canonical["clarificationCheckpoint"] = copy.deepcopy(checkpoint)
        return {
            "requestContext": recovered_context,
            "sessionId": str(row["session_id"]),
            "sourceUserTurnId": str(row["id"]),
            "sourceAssistantTurnId": source_turn_id,
            "itineraryVersionId": row["itinerary_version_id"],
            "turnIndex": int(row["turn_index"]),
            "selectedChoiceId": choice_id,
            "checkpointId": str(checkpoint.get("checkpointId") or ""),
            "dimensionId": current_dimension,
        }

    @staticmethod
    def _valid_clarification_checkpoint(checkpoint: dict[str, Any]) -> bool:
        fingerprint = str(checkpoint.get("fingerprint") or "")
        return bool(
            checkpoint.get("schemaVersion") in ClarificationCheckpointService.SUPPORTED_SCHEMA_VERSIONS
            and fingerprint
            and fingerprint
            == ClarificationCheckpointService._fingerprint(
                {key: value for key, value in checkpoint.items() if key != "fingerprint"}
            )
        )

    @staticmethod
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

    def _comparison_projections_from_turn(self, row: sqlite3.Row) -> list[dict]:
        projections = self._response_payload_from_turn(row).get("comparisonProjections")
        if not isinstance(projections, list):
            return []
        resolver = ProposalVisitFactsService(self.db)
        return [
            resolver.merge_projection(dict(item))
            for item in projections
            if isinstance(item, dict)
        ]

    def _choice_options_from_turn(
        self,
        row: sqlite3.Row,
        *,
        clarification_recovery: Optional[dict[str, Any]] = None,
    ) -> list[dict]:
        row = self._simple_direction_reconciled_turn(row)
        options = self._response_payload_from_turn(row).get("choiceOptions")
        if not isinstance(options, list):
            return []
        response_payload = self._response_payload_from_turn(row)
        consumed = {str(item) for item in response_payload.get("consumedChoiceIds") or []}
        executions = {
            str(item["choice_id"]): str(item["status"])
            for item in self.db.execute(
                """
                SELECT choice_id, status FROM agent_choice_executions
                WHERE session_id = ? AND source_turn_id = ?
                """,
                (row["session_id"], row["id"]),
            ).fetchall()
        }
        recovered_choice_id = str((clarification_recovery or {}).get("selectedChoiceId") or "")
        recovered_checkpoint_id = str((clarification_recovery or {}).get("checkpointId") or "")
        recovered_dimension_id = str((clarification_recovery or {}).get("dimensionId") or "")
        visit_facts = ProposalVisitFactsService(self.db)
        projected: list[dict] = []
        for raw in options:
            if not isinstance(raw, dict):
                continue
            option = visit_facts.merge_choice_option(raw)
            choice_id = str(option.get("id") or "")
            same_recovered_question = bool(
                recovered_checkpoint_id
                and str(option.get("action") or "") == "continue_clarification"
                and str(option.get("checkpointId") or "") == recovered_checkpoint_id
                and str(option.get("dimensionId") or "") == recovered_dimension_id
            )
            execution_status = executions.get(choice_id)
            if option.get("operationOrigin") is not None or option.get("action") in {"continue_plan_expansion", "select_plan_proposal", "search_travel_guide_advice"}:
                from src.services.conversation_operation_identity import ConversationOperationIdentity
                execution_status = ConversationOperationIdentity(self.db).lifecycle_status(str(row["session_id"]), str(row["id"]), option)
            if same_recovered_question:
                lifecycle = "consumed" if choice_id == recovered_choice_id else "stale"
            elif choice_id in consumed:
                lifecycle = "consumed"
            elif execution_status == "executing":
                lifecycle = "executing"
            elif execution_status in {
                "failed_retryable",
                "failed_terminal",
                "cancelled",
                "stale",
                "expired",
            }:
                lifecycle = execution_status
            elif execution_status == "succeeded":
                lifecycle = "consumed"
            else:
                lifecycle = "offered"
            option["lifecycle"] = lifecycle
            projected.append(option)
        return projected

    def _simple_direction_reconciled_turn(self, row: sqlite3.Row) -> sqlite3.Row:
        simple_direction_evidence = SimpleDirectionExecutionEvidenceService(self.db)
        reconciled_execution_count = simple_direction_evidence.reconcile_legacy_for_source_turn(
            session_id=str(row["session_id"]),
            source_turn_id=str(row["id"]),
        )
        continuation_reissued = simple_direction_evidence.reconcile_missing_continuation_for_source_turn(
            session_id=str(row["session_id"]),
            source_turn_id=str(row["id"]),
        )
        if reconciled_execution_count or continuation_reissued:
            refreshed_row = self.db.execute(
                "SELECT * FROM conversation_turns WHERE id = ? AND session_id = ?",
                (str(row["id"]), str(row["session_id"])),
            ).fetchone()
            if refreshed_row is not None:
                row = refreshed_row
        return row

    def _local_poi_options_from_turn(self, row: sqlite3.Row) -> Optional[dict]:
        options = self._response_payload_from_turn(row).get("localPoiOptions")
        return options if isinstance(options, dict) else None

    def _planning_events_from_turn(self, row: sqlite3.Row, key: str) -> list[dict]:
        payload = self._response_payload_from_turn(row)
        events = payload.get(key)
        return sanitize_execution_events(
            normalize_persisted_planning_events(events, row["updated_at"] or row["created_at"])
        )

    def _latest_planning_run(self, session: sqlite3.Row) -> Optional[PlanningRunResponse]:
        row = None
        if session["active_version_id"]:
            row = self.db.execute(
                """
                SELECT * FROM planning_runs
                WHERE itinerary_version_id = ? AND itinerary_plan_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session["active_version_id"], session["active_plan_id"]),
            ).fetchone()
            if row is None:
                return None
        else:
            row = self.db.execute(
                """
                SELECT * FROM planning_runs
                WHERE itinerary_plan_id = ? AND itinerary_version_id IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session["active_plan_id"],),
            ).fetchone()
        if row is None:
            return None
        return PlanningRunResponse(
            id=row["id"],
            run_type=row["run_type"],
            user_input=row["user_input"],
            preference_summary=row["preference_summary"],
            itinerary_plan_id=row["itinerary_plan_id"],
            itinerary_version_id=row["itinerary_version_id"],
            understood_requirements=json.loads(row["understood_requirements_json"] or "{}"),
            constraint_summary=json.loads(row["constraint_summary_json"] or "[]"),
            tool_calls=json.loads(row["tool_calls_json"] or "[]"),
            source_assessments=json.loads(row["source_assessments_json"] or "[]"),
            feasibility_report=json.loads(row["feasibility_report_json"]) if row["feasibility_report_json"] else None,
            final_summary=row["final_summary"],
            created_at=row["created_at"],
        )

    def _pending_candidates(self, session_id: str) -> list[PendingPoiCandidateResponse]:
        rows = self.db.execute(
            """
            SELECT * FROM amap_poi_candidates
            WHERE session_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        result: list[PendingPoiCandidateResponse] = []
        for row in rows:
            raw_candidates = json.loads(row["candidates_json"] or "[]")
            candidates = self._safe_pending_candidates(
                str(row["category"] or ""), str(row["query"] or ""), raw_candidates
            )
            if not candidates:
                continue
            result.append(
                PendingPoiCandidateResponse(
                    id=row["id"],
                    query=row["query"],
                    city=row["city"],
                    category=row["category"],
                    status=row["status"],
                    candidates=candidates,
                    selected_amap_id=row["selected_amap_id"],
                    source_segment_id=row["segment_id"] if "segment_id" in row.keys() else None,
                    created_at=row["created_at"],
                )
            )
        return result

    @staticmethod
    def _safe_pending_candidates(category: str, raw_need: str, candidates: object) -> list[dict]:
        if not isinstance(candidates, list):
            return []
        intent_type = {
            "museum": "museum",
            "university": "campus_visit",
            "education": "campus_visit",
            "school": "campus_visit",
            "food": "meal",
        }.get(str(category or "").strip().casefold(), "")
        policy = IntentCandidateSemanticPolicy()
        safe: list[dict] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            provider_id = candidate.get("id") or candidate.get("amapId")
            source = candidate.get("source")
            if not provider_id or source != AMAP_PLACE_SOURCE:
                continue
            if candidate.get("longitude") is None or candidate.get("latitude") is None:
                continue
            if intent_type and not policy.evaluate(intent_type, candidate, raw_need=raw_need).passed:
                continue
            safe.append(candidate)
        return safe

    def expire_pending_candidates(self, session_id: str) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        self.db.execute(
            """
            UPDATE amap_poi_candidates
            SET status = 'expired'
            WHERE session_id = ? AND status = 'pending' AND created_at < ?
            """,
            (session_id, cutoff),
        )

    def reject_pending_candidate(self, session_id: str, candidate_id: str) -> AgentSessionResponse:
        candidate = self.db.execute(
            "SELECT status FROM amap_poi_candidates WHERE id = ? AND session_id = ?",
            (candidate_id, session_id),
        ).fetchone()
        if candidate is None:
            raise HTTPException(status_code=404, detail="Pending POI candidate not found for this session")
        if candidate["status"] != "pending":
            raise HTTPException(status_code=409, detail="Pending POI candidate is no longer pending")
        self.db.execute(
            """
            UPDATE amap_poi_candidates
            SET status = 'rejected'
            WHERE id = ? AND session_id = ? AND status = 'pending'
            """,
            (candidate_id, session_id),
        )
        self.db.commit()
        return self.get_session(session_id)

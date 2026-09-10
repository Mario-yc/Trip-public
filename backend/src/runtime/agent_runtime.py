import subprocess
import traceback
import json
import re
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.agent import (
    AgentMessageContext,
    AgentMessageEditRequest,
    AgentMessageEditResponse,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentSessionCreateRequest,
    AgentSessionListResponse,
    AgentSessionResponse,
    ConversationTurnResponse,
)
from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.core.config import get_settings
from src.core.database import PROJECT_ROOT, sqlite_path_from_url
from src.core.schema import initialize_database
from src.runtime.run_artifacts import (
    RunArtifactReplayer,
    RunArtifactWriter,
    redact,
    redact_artifact,
    redact_database_url,
    utc_now,
)
from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
from src.runtime.runtime_models import RuntimeErrorRecord, RuntimeFinalResponse, RuntimeRunOptions, RuntimeStatus
from src.runtime.state_exporter import AgentStateExporter
from src.providers.travel_tools import web_search_provider_config_diagnostics
from src.services.deepseek_agent_provider import AgentToolLoopResult
from src.services.agent_service import AgentExecutionEventSink, AgentService
from src.services.conversation_service import ConversationService
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.map_poi_service import clear_map_poi_runtime_state, MapPoiService
from src.services.meal_diversity_policy import MealDiversityPolicy
from src.services.meal_experience_assignment import MealExperienceAssignmentPolicy
from src.services.goal_ledger_service import GoalLedgerService
from src.services.route_service import RouteService
from src.services.amap_call_budget import current_amap_call_budget
from src.services.timeline_target_binder import TimelineTargetBinder
from src.services.timeline_mutation_postcondition_verifier import (
    MutationPostconditionReport,
    TimelineMutationPostconditionVerifier,
)
from src.services.poi_candidate_dominance_service import PoiCandidateDominanceService
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse


class TripAgentRuntime:
    def __init__(self, db):
        self.db = db

    def list_sessions(self) -> AgentSessionListResponse:
        return ConversationService(self.db).list_sessions()

    def create_session(self, payload: AgentSessionCreateRequest) -> AgentSessionResponse:
        return ConversationService(self.db).create_session(
            payload.city,
            payload.title,
            include_editable_draft=True,
        )

    def get_current_session(self) -> AgentSessionResponse:
        return ConversationService(self.db).get_current_session()

    def get_session(self, session_id: str) -> AgentSessionResponse:
        return ConversationService(self.db).get_session(session_id)

    def delete_session(self, session_id: str) -> AgentSessionListResponse:
        return ConversationService(self.db).delete_session(session_id)

    def reject_pending_poi_candidate(self, session_id: str, candidate_id: str) -> AgentSessionResponse:
        return ConversationService(self.db).reject_pending_candidate(session_id, candidate_id)

    def send_agent_message(
        self,
        session_id: str,
        payload: AgentMessageRequest,
        event_sink: Optional[AgentExecutionEventSink] = None,
        user_turn_sink: Optional[Callable[[ConversationTurnResponse], None]] = None,
        *,
        mock_providers: bool = False,
        mock_map_provider: Optional[dict[str, Any]] = None,
    ) -> AgentMessageResponse:
        provider = (
            RuntimeStagedMapMockProvider(mock_map_provider)
            if mock_map_provider
            else (RuntimeMockAgentProvider() if mock_providers else None)
        )
        return AgentService(self.db, provider=provider).send_message(
            session_id,
            payload,
            event_sink=event_sink,
            user_turn_sink=user_turn_sink,
        )

    def inspect_session(self, session_id: str) -> dict:
        return AgentStateExporter(self.db).inspect_session(session_id)

    def export_state(self, session_id: str) -> dict:
        return AgentStateExporter(self.db).export_state(session_id)

    @staticmethod
    def replay_artifact(artifact_path: str) -> dict:
        return RunArtifactReplayer().replay(Path(artifact_path))

    def edit_user_message(
        self,
        session_id: str,
        turn_id: str,
        payload: AgentMessageEditRequest,
    ) -> AgentMessageEditResponse:
        return AgentService(self.db).edit_user_message(session_id, turn_id, payload)

    def resume_failed_turn(
        self,
        session_id: str,
        turn_id: str,
        event_sink: Optional[AgentExecutionEventSink] = None,
    ) -> AgentMessageResponse:
        return AgentService(self.db).resume_failed_turn(session_id, turn_id, event_sink=event_sink)

    def health(
        self,
        state_dir: str = ".ai-runs",
        baseline_ref: str = "preview/agent-mvp-foundation-20260629-agentcli",
    ) -> dict:
        settings = get_settings()
        db_path = sqlite_path_from_url(settings.database_url)
        memory_counts = {"preferenceMemoryRows": 0, "sessionMemoryRows": 0}
        database = {
            "configured": bool(settings.database_url),
            "initialized": False,
            "error": None,
            "databasePath": str(db_path),
            "databaseExists": db_path.exists(),
            **memory_counts,
        }
        try:
            initialize_database()
            database["initialized"] = True
            database["databaseExists"] = db_path.exists()
            memory_counts = self._memory_row_counts()
            database.update(memory_counts)
        except Exception as error:
            database["error"] = str(error)
        artifact = self._check_artifact_dir(Path(state_dir))
        provider_status = self._provider_status()
        status = "ok" if database["initialized"] and artifact["writable"] else "failed"
        if status == "ok" and not provider_status["agent"]["configured"]:
            status = "degraded"
        return {
            "schemaVersion": "ai-runtime-health-v1",
            "status": status,
            "baselineRef": baseline_ref,
            "databasePath": database["databasePath"],
            "databaseExists": database["databaseExists"],
            **memory_counts,
            "database": {
                **database,
                "databaseUrl": redact_database_url(settings.database_url),
            },
            "providerStatus": provider_status,
            "artifactDirectory": artifact,
        }

    def _memory_row_counts(self) -> dict[str, int]:
        try:
            preference_rows = self.db.execute("SELECT COUNT(*) FROM travel_preference_memories").fetchone()[0]
            session_rows = self.db.execute("SELECT COUNT(*) FROM session_preference_memories").fetchone()[0]
        except Exception:
            return {"preferenceMemoryRows": 0, "sessionMemoryRows": 0}
        return {
            "preferenceMemoryRows": int(preference_rows or 0),
            "sessionMemoryRows": int(session_rows or 0),
        }

    def run_once(
        self, options: RuntimeRunOptions, argv: Optional[list[str]] = None
    ) -> tuple[RuntimeFinalResponse, int]:
        settings = get_settings()
        if options.mock_providers and not options.mock_map_provider:
            options = options.model_copy(
                update={
                    "mock_map_provider": {
                        "enabled": True,
                        "trustedPoiFixtures": True,
                        "trustedFoodForDeterministicEdit": True,
                        "mockRouteRefresh": True,
                        "deterministicSingleCandidate": True,
                    }
                }
            )
        writer = RunArtifactWriter(Path(options.state_dir))
        writer.ensure_jsonl_files()
        started_at = utc_now()
        manifest = self._manifest(
            writer,
            options,
            argv or [],
            started_at=started_at,
            finished_at=None,
            status="running",
        )
        writer.write_json("manifest", manifest)
        artifact_input = options.model_dump(by_alias=True)
        artifact_input["stateDir"] = "artifact://state"
        writer.write_json("input", artifact_input)

        session_id = options.session_id
        response = None
        events: list[dict] = []
        try:
            if not options.input.strip() and not options.selected_agent_choice:
                raise RuntimeInputError("invalid_input", "--input is required", "validate_input")
            if not options.mock_providers and not options.mock_map_provider and not settings.deepseek_api_key:
                raise RuntimeInputError(
                    "provider_unavailable",
                    "DeepSeek provider is not configured; pass --mock-providers for local dry runs.",
                    "provider_preflight",
                    status="provider_unavailable",
                )
            if session_id is None:
                session = ConversationService(self.db).create_session(options.city, include_editable_draft=True)
                session_id = session.session_id
            active_version_before = self._session_active_version_id(session_id)
            # Failure artifacts still need an accurate zero-write delta when
            # the executor raises before the post-action reload occurs.
            active_version_after = active_version_before

            def collect_event(event) -> None:
                events.append(event.model_dump(by_alias=True, exclude_none=True))

            send_kwargs = {"mock_providers": options.mock_providers}
            if options.mock_map_provider:
                send_kwargs["mock_map_provider"] = options.mock_map_provider
            with self._mock_map_provider_scope(options.mock_map_provider):
                response = self.send_agent_message(
                    session_id,
                    AgentMessageRequest(
                        content=options.input,
                        context=AgentMessageContext(selectedAgentChoice=options.selected_agent_choice),
                    ),
                    event_sink=collect_event,
                    **send_kwargs,
                )
            active_version_after = self._session_active_version_id(session_id)
            status = self._status_from_response(response)
            final = self._final_response(
                response,
                status,
                writer.as_posix_or_str(),
                debug_enabled=options.debug,
                active_version_changed=active_version_before != active_version_after,
            )
            exit_code = self._exit_code(status)
        except RuntimeInputError as error:
            record = RuntimeErrorRecord(
                errorCode=error.error_code,
                message=error.message,
                stage=error.stage,
                timestamp=utc_now(),
            )
            writer.append_error(record)
            final = RuntimeFinalResponse(
                status=error.status,
                terminalStatus=error.status,
                activeVersionChanged=False,
                sessionId=session_id,
                assistantReply="",
                warnings=[error.message],
                artifactPath=writer.as_posix_or_str(),
                artifactPathAbsolute=writer.absolute_path(),
                nextActions=[error.message],
            )
            exit_code = self._exit_code(error.status)
        except Exception as error:
            record = RuntimeErrorRecord(
                errorCode=self._error_code(error),
                message=self._safe_error_message(error),
                stage="run_agent",
                timestamp=utc_now(),
                traceback=traceback.format_exc() if options.debug else None,
            )
            writer.append_error(record)
            final = RuntimeFinalResponse(
                status="failed",
                terminalStatus="failed",
                activeVersionChanged=False,
                sessionId=session_id,
                assistantReply="",
                warnings=[record.message],
                artifactPath=writer.as_posix_or_str(),
                artifactPathAbsolute=writer.absolute_path(),
                nextActions=["检查 errors.jsonl 和 providerStatus 后重试。"],
            )
            exit_code = 1

        if response is not None:
            planning_steps = [item.model_dump(by_alias=True, exclude_none=True) for item in response.planning_steps]
            tool_events = [item.model_dump(by_alias=True, exclude_none=True) for item in response.tool_events]
        else:
            planning_steps = []
            tool_events = []
        if response is not None and final.status in {"validation_failed", "stale_version", "failed"}:
            writer.append_error(
                RuntimeErrorRecord(
                    errorCode=self._error_code_for_status(final.status),
                    message=self._runtime_failure_message(response),
                    stage="agent_response",
                    timestamp=utc_now(),
                )
            )
        snapshot = AgentStateExporter(self.db).export_session(session_id)
        turn_payload = self._turn_payload(response.assistant_turn.id) if response is not None else {}
        context = turn_payload.get("agent_request_json") or {}
        agent_plan = context.get("agentPlan") or (context.get("understoodRequirements") or {}).get("agentPlan") or {}
        decision_state = context.get("agentDecisionState") or context.get("agentDecisionShadow") or {}
        provider_raw_decisions = (
            decision_state.get("providerRawDecisions")
            if isinstance(decision_state.get("providerRawDecisions"), list)
            else []
        )
        provider_raw_decision = (
            provider_raw_decisions[-1]
            if provider_raw_decisions and isinstance(provider_raw_decisions[-1], dict)
            else decision_state.get("normalizedDecision")
            if isinstance(decision_state.get("normalizedDecision"), dict)
            else {}
        )
        if decision_state and not any(item.get("type") == "agent_decision" for item in planning_steps):
            planning_steps.insert(
                0,
                {
                    "type": "agent_decision",
                    "label": "自治决策",
                    "status": "completed",
                    "detail": decision_state.get("decisionSummary") or "已生成自治决策。",
                    "sessionId": session_id,
                    "turnId": response.assistant_turn.id if response is not None else None,
                    "decisionSummary": decision_state.get("decisionSummary") or "",
                    "userVisible": True,
                    "category": "decision",
                    "metadata": decision_state,
                    "timestamp": utc_now(),
                },
            )
        writer.append_jsonl("planningSteps", planning_steps or events)
        writer.append_jsonl("toolEvents", tool_events)
        autonomy_events = planning_steps or events
        writer.append_jsonl(
            "agentObservations",
            [
                item.get("metadata", {}).get("observation")
                for item in autonomy_events
                if item.get("type") == "agent_observation"
                and isinstance(item.get("metadata", {}).get("observation"), dict)
            ],
        )
        writer.append_jsonl(
            "agentDecisions", [item for item in autonomy_events if item.get("type") == "agent_decision"]
        )
        writer.append_jsonl(
            "agentActionOutcomes", [item for item in autonomy_events if item.get("type") == "agent_action_outcome"]
        )
        stop_events = [item for item in autonomy_events if item.get("type") == "agent_stop"]
        writer.write_json("agentStop", stop_events[-1] if stop_events else {"reason": final.status})
        verifier_report = self._verifier_report(planning_steps or events)
        writer.write_json("sessionSnapshot", snapshot)
        writer.write_json("context", context)
        writer.write_json("agentPlan", agent_plan)
        writer.write_json(
            "agentDecision",
            {
                "rawDecision": redact(provider_raw_decision),
                "gatedDecision": decision_state,
                "executionOutcome": next(
                    (item.get("metadata") for item in autonomy_events if item.get("type") == "agent_action_outcome"), {}
                ),
            },
        )
        writer.append_jsonl("patches", snapshot.get("itinerary_patches") or [])
        portfolios = list(snapshot.get("plan_portfolios") or [])
        if portfolios:
            latest_portfolio = portfolios[0]
            portfolio_id = str(latest_portfolio.get("id") or "")
            proposals = [
                proposal
                for proposal in snapshot.get("plan_proposals") or []
                if str(proposal.get("portfolio_id") or "") == portfolio_id
            ]
            writer.write_json("portfolio", latest_portfolio)
            writer.append_jsonl("planProposals", proposals)
            writer.append_jsonl("portfolioScores", [proposal.get("score_json") or {} for proposal in proposals])
            writer.append_jsonl("portfolioVerifier", [proposal.get("verifier_json") or {} for proposal in proposals])
            writer.write_json(
                "portfolioSelection",
                {
                    "portfolioId": portfolio_id,
                    "status": latest_portfolio.get("status"),
                    "selectedProposalId": latest_portfolio.get("selected_proposal_id"),
                    "activeVersionId": final.active_version_id,
                    "versionDelta": int(active_version_before != active_version_after),
                },
            )
            selected_proposal_id = str(latest_portfolio.get("selected_proposal_id") or "")
            selected_proposal = next(
                (proposal for proposal in proposals if str(proposal.get("id") or "") == selected_proposal_id),
                None,
            )
            if selected_proposal is not None and final.active_version_id:
                verifier_report = {
                    **(selected_proposal.get("verifier_json") or {}),
                    "evidenceType": "selected_plan_proposal",
                    "selectedProposalId": selected_proposal_id,
                    "activeVersionId": final.active_version_id,
                }
        writer.write_json("verifierReport", verifier_report)
        writer.write_json("itinerarySnapshot", snapshot.get("active_itinerary_snapshot"))
        # The CLI result may carry a local path so the caller can inspect the
        # run. Persisted artifacts must remain portable and must not disclose
        # the machine's directory layout.
        artifact_final_response = final.model_dump(by_alias=True)
        artifact_final_response["artifactPath"] = f"artifact://{writer.run_id}"
        artifact_final_response.pop("artifactPathAbsolute", None)
        writer.write_json("finalResponse", artifact_final_response)
        writer.path("readme").write_text(self._artifact_readme(final), encoding="utf-8")
        finished_at = utc_now()
        writer.write_json(
            "manifest",
            self._manifest(
                writer,
                options,
                argv or [],
                started_at=started_at,
                finished_at=finished_at,
                status=final.status,
                session_id=session_id,
            ),
        )
        return final, exit_code

    def _status_from_response(self, response: AgentMessageResponse) -> RuntimeStatus:
        response_text = self._response_text(response)
        if self._is_stale_version_text(response_text):
            return "stale_version"
        if self._has_failed_patch_tool(response):
            return "validation_failed"
        if self._has_response_verifier_failure(response):
            return "validation_failed"
        if response.terminal_status == "needs_confirmation":
            return "needs_confirmation"
        if response.terminal_status == "partial_success":
            return "partial_success"
        if response.terminal_status == "draft_pending_grounding":
            return "draft_pending_grounding"
        if response.terminal_status == "candidate_refresh_required":
            return "candidate_refresh_required"
        if response.terminal_status == "no_safe_action":
            return "no_safe_action"
        if response.terminal_status == "failed":
            return "failed"
        turn_payload = self._turn_payload(response.assistant_turn.id)
        agent_response = turn_payload.get("agent_response_json") or {}
        if agent_response.get("mode") == "tool_loop_exceeded_after_write":
            return "partial_success"
        if self._has_patch_validation_failure(response) or self._has_verifier_failure(turn_payload):
            return "validation_failed"
        if response.assistant_turn.status == "failed":
            return "failed"
        if response.version is not None:
            return "success"
        if response.pending_poi_candidates:
            return "needs_confirmation"
        if response.planning_run and response.planning_run.run_type in {
            "agent_clarification",
            "agent_poi_clarification",
        }:
            return "needs_confirmation"
        return "success"

    def _session_active_version_id(self, session_id: str) -> Optional[str]:
        row = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return str(row["active_version_id"]) if row is not None and row["active_version_id"] else None

    def _final_response(
        self,
        response: AgentMessageResponse,
        status: str,
        artifact_path: str,
        *,
        debug_enabled: bool = False,
        active_version_changed: bool = False,
    ) -> RuntimeFinalResponse:
        active_plan_id = response.itinerary.id if response.itinerary is not None else None
        active_version_id = response.version.id if response.version is not None else None
        if response.assistant_turn.itinerary_version_id:
            active_version_id = response.assistant_turn.itinerary_version_id
        session_id = self._session_id_for_turn(response.user_turn.id)
        session_row = self.db.execute(
            "SELECT active_plan_id, active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session_row is not None:
            active_plan_id = session_row["active_plan_id"]
            active_version_id = session_row["active_version_id"]
        return RuntimeFinalResponse(
            status=status,
            terminalStatus=response.terminal_status or status,
            activeVersionChanged=active_version_changed,
            sessionId=session_id,
            activePlanId=active_plan_id,
            activeVersionId=active_version_id,
            assistantReply=response.assistant_turn.content,
            warnings=response.warnings,
            pendingPoiCandidates=[
                item.model_dump(by_alias=True, exclude_none=True) for item in response.pending_poi_candidates
            ],
            artifactPath=artifact_path,
            artifactPathAbsolute=str(Path(artifact_path).resolve()),
            nextActions=self._next_actions(status, response),
            debug=self._runtime_debug_summary(response, status, active_version_changed) if debug_enabled else {},
        )

    def _runtime_debug_summary(
        self,
        response: AgentMessageResponse,
        status: str,
        active_version_changed: bool = False,
    ) -> dict[str, Any]:
        events = [
            *[item.model_dump(by_alias=True, exclude_none=True) for item in response.planning_steps],
            *[item.model_dump(by_alias=True, exclude_none=True) for item in response.tool_events],
        ]
        previews = [self._event_preview(event) for event in events]
        timeline_previews = [
            preview
            for event, preview in zip(events, previews)
            if event.get("type") == "timeline_edit" or event.get("toolName") == "timeline_edit"
        ]
        amap_snapshots = [
            preview.get("amapCallBudget") for preview in previews if isinstance(preview.get("amapCallBudget"), dict)
        ]
        latest_amap_snapshot = max(
            amap_snapshots,
            key=lambda item: len(item.get("calls") or []),
            default={},
        )
        meal_queries = [
            str(call.get("keyword") or "")
            for call in latest_amap_snapshot.get("calls") or []
            if isinstance(call, dict)
            and str(call.get("category") or "") == "food"
            and str(call.get("endpoint") or "") in {"place/around", "place/text"}
            and call.get("keyword")
        ]
        meal_pois = [
            segment.poi.model_dump(by_alias=True)
            for day in (response.itinerary.days if response.itinerary is not None else [])
            for segment in day.segments
            if segment.kind == "meal"
        ]
        meal_policy = MealDiversityPolicy()
        meal_families = [meal_policy.dish_family(poi) for poi in meal_pois if meal_policy.dish_family(poi)]
        meal_brands = [meal_policy.canonical_meal_brand(poi.get("name")) for poi in meal_pois if poi.get("name")]
        planning_events = [item.model_dump(by_alias=True, exclude_none=True) for item in response.planning_steps]
        schema_versions = {
            str(preview.get("toolSchemaVersion")) for preview in previews if preview.get("toolSchemaVersion")
        }
        schema_hashes: dict[str, str] = {}
        thinking_modes: set[str] = set()
        reasoning_efforts: set[str] = set()
        for preview in previews:
            if isinstance(preview.get("toolSchemaHashes"), dict):
                schema_hashes.update({str(key): str(value) for key, value in preview["toolSchemaHashes"].items()})
            if preview.get("thinkingMode"):
                thinking_modes.add(str(preview["thinkingMode"]))
            if preview.get("reasoningEffort"):
                reasoning_efforts.add(str(preview["reasoningEffort"]))
        return {
            "executionMode": response.execution_mode,
            "terminalStatus": response.terminal_status,
            "agentDecisionCount": response.agent_decision_count,
            "visibleActionCount": sum(1 for event in planning_events if event.get("userVisible")),
            "hiddenInternalEventCount": sum(1 for event in planning_events if not event.get("userVisible")),
            "outcomeStatuses": response.outcome_statuses,
            "toolSchemaVersions": sorted(schema_versions),
            "toolSchemaHashes": schema_hashes,
            "thinkingModes": sorted(thinking_modes),
            "reasoningEfforts": sorted(reasoning_efforts),
            "businessToolRounds": self._max_preview_int(previews, "businessToolRounds"),
            "schemaRepairAttempts": self._max_preview_int(previews, "schemaRepairAttempts"),
            "toolArgumentValidationFailureCount": sum(
                1 for event in events if str(event.get("failureReason") or "") == "tool_argument_schema_error"
            ),
            "inventedAmapPoiCount": sum(
                1
                for event in events
                if "amap_poi_provenance_error" in json.dumps(event, ensure_ascii=False, default=str)
            ),
            "pendingCandidateCount": len(response.pending_poi_candidates),
            "changedSegmentIds": sorted(
                {
                    str(segment_id)
                    for preview in previews
                    for segment_id in preview.get("changedSegmentIds") or []
                    if str(segment_id)
                }
            ),
            "mealSearchQueries": meal_queries,
            "repeatedAmapQueryCount": max(0, len(meal_queries) - len(set(meal_queries))),
            "selectedMealFamilies": meal_families,
            "uniqueMealFamilyCount": len(set(meal_families)),
            "zhajiangmianCount": sum(1 for family in meal_families if family == "炸酱面"),
            "duplicateMealBrandCount": max(
                0, len([brand for brand in meal_brands if brand]) - len(set(brand for brand in meal_brands if brand))
            ),
            "deterministicTimelineCommand": any(
                preview.get("toolLoopEntered") is False for preview in timeline_previews
            )
            or bool(timeline_previews),
            "toolLoopEntered": self._tool_loop_entered(events, previews),
            "amapPoiExternalCalls": self._max_budget_value(amap_snapshots, "usedTotalExternal"),
            "amapPoiTextSearchCalls": self._max_budget_value(amap_snapshots, "usedPlaceText"),
            "amapPoiAroundSearchCalls": self._max_budget_value(amap_snapshots, "usedPlaceAround"),
            "amapCacheHitCount": self._max_budget_value(amap_snapshots, "cacheHitCount"),
            "amapSkippedBecauseBudget": self._max_budget_value(amap_snapshots, "skippedBecauseBudget"),
            "duplicateExternalQueryCount": self._max_budget_value(amap_snapshots, "duplicateExternalQueryCount"),
            "reusedQueryCount": self._max_budget_value(amap_snapshots, "reusedQueryCount"),
            "newQueryCount": self._max_budget_value(amap_snapshots, "newQueryCount"),
            "webSearchCalls": self._tool_call_count(events, previews, "web_search", "webSearchCalls"),
            "ticketLookupCalls": self._tool_call_count(events, previews, "ticket_lookup", "ticketLookupCalls"),
            "amapWeatherCalls": self._tool_call_count(events, previews, "amap_weather", "amapWeatherCalls"),
            "patchOperationCount": self._max_preview_int(previews, "operationCount"),
            "activeVersionChanged": active_version_changed,
            "verifierTriggered": any(self._is_verifier_event(event) for event in events),
            "failureReason": self._runtime_failure_message(response)
            if status in {"failed", "validation_failed", "stale_version"}
            else "",
        }

    def _event_preview(self, event: dict[str, Any]) -> dict[str, Any]:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
        return preview

    def _tool_loop_entered(self, events: list[dict[str, Any]], previews: list[dict[str, Any]]) -> bool:
        if any(preview.get("toolLoopEntered") is True for preview in previews):
            return True
        if any(preview.get("toolLoopEntered") is False for preview in previews):
            return False
        return any(event.get("type") == "tool" for event in events)

    def _tool_call_count(
        self, events: list[dict[str, Any]], previews: list[dict[str, Any]], tool_name: str, preview_key: str
    ) -> int:
        counted = sum(1 for event in events if str(event.get("toolName") or event.get("label") or "") == tool_name)
        return max(counted, self._max_preview_int(previews, preview_key))

    def _max_preview_int(self, previews: list[dict[str, Any]], key: str) -> int:
        values: list[int] = []
        for preview in previews:
            try:
                values.append(int(preview.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return max(values) if values else 0

    def _max_budget_value(self, snapshots: list[dict[str, Any]], key: str) -> int:
        values: list[int] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            raw_value = snapshot.get(key)
            if raw_value is None and isinstance(snapshot.get("used"), dict):
                raw_value = snapshot["used"].get(key)
            try:
                values.append(int(raw_value or 0))
            except (TypeError, ValueError):
                continue
        return max(values) if values else 0

    def _session_id_for_turn(self, turn_id: str) -> Optional[str]:
        row = self.db.execute("SELECT session_id FROM conversation_turns WHERE id = ?", (turn_id,)).fetchone()
        return row["session_id"] if row else None

    def _next_actions(self, status: str, response: AgentMessageResponse) -> list[str]:
        if status == "needs_confirmation" and response.pending_poi_candidates:
            return ["用 inspect 查看 pendingPoiCandidates，并通过现有确认 API 或前端确认具体 POI。"]
        if status == "needs_confirmation":
            return ["补齐 assistantReply 中要求的信息后再次运行 CLI。"]
        if status == "candidate_refresh_required":
            return ["先刷新必选体验的真实候选地点；在候选完整前不会创建或扩展方案。"]
        if status == "provider_unavailable":
            return ["配置 DEEPSEEK_API_KEY 后重试，或仅在本地 dry-run 时传 --mock-providers。"]
        if status == "stale_version":
            return ["先运行 export-state 或 inspect 获取最新 activeVersionId，再用最新 session 状态重试。"]
        if status in {"failed", "validation_failed"}:
            return ["查看 errors.jsonl、planning_steps.jsonl 和 session_snapshot.json 后重试。"]
        return ["可用 inspect 读取当前 session 状态，或继续 run 发送下一条 Agent 指令。"]

    def _exit_code(self, status: str) -> int:
        return {
            "success": 0,
            "partial_success": 0,
            "draft_pending_grounding": 0,
            "candidate_refresh_required": 3,
            "needs_confirmation": 3,
            "no_safe_action": 3,
            "validation_failed": 2,
            "provider_unavailable": 4,
            "stale_version": 5,
            "failed": 1,
        }.get(status, 1)

    def _error_code_for_status(self, status: str) -> str:
        return {
            "validation_failed": "validation_failed",
            "stale_version": "stale_base_version",
            "provider_unavailable": "provider_unavailable",
            "failed": "runtime_failed",
        }.get(status, status)

    def _response_text(self, response: AgentMessageResponse) -> str:
        parts = [response.assistant_turn.content, *response.warnings]
        for event in [*response.planning_steps, *response.tool_events]:
            parts.extend(
                [
                    str(event.label or ""),
                    str(event.detail or ""),
                    str(event.failure_reason or ""),
                    str(event.metadata or {}),
                ]
            )
        turn_payload = self._turn_payload(response.assistant_turn.id)
        parts.extend([str(turn_payload.get("agent_response_json") or ""), str(turn_payload.get("error_json") or "")])
        return "\n".join(part for part in parts if part)

    def _runtime_failure_message(self, response: AgentMessageResponse) -> str:
        if response.warnings:
            return str(response.warnings[0])
        turn_payload = self._turn_payload(response.assistant_turn.id)
        error_json = turn_payload.get("error_json") or {}
        if error_json.get("message"):
            return str(error_json["message"])
        return response.assistant_turn.content or "runtime failed"

    def _is_stale_version_text(self, text: str) -> bool:
        lowered = text.lower()
        return (
            "base itinerary version is stale" in lowered
            or "stale_base_version" in lowered
            or "stale baseversionid" in lowered
        )

    def _has_patch_validation_failure(self, response: AgentMessageResponse) -> bool:
        for event in response.tool_events:
            if event.type != "tool" or event.label != "patch_itinerary":
                continue
            metadata = event.metadata or {}
            result_preview = metadata.get("resultPreview") if isinstance(metadata, dict) else None
            if not isinstance(result_preview, dict):
                continue
            validation_feedback = result_preview.get("validationFeedback")
            if isinstance(validation_feedback, dict) and validation_feedback.get("validationErrors"):
                return True
        return False

    def _has_failed_patch_tool(self, response: AgentMessageResponse) -> bool:
        for event in response.tool_events:
            if event.type == "tool" and event.label == "patch_itinerary" and event.status in {"failed", "rejected"}:
                return True
        return False

    def _has_verifier_failure(self, turn_payload: dict) -> bool:
        error_json = turn_payload.get("error_json") or {}
        if not isinstance(error_json, dict):
            return False
        verifier_report = error_json.get("verifierReport")
        return isinstance(verifier_report, dict) and bool(verifier_report.get("hardFailures"))

    def _has_response_verifier_failure(self, response: AgentMessageResponse) -> bool:
        for event in [*list(response.planning_steps or []), *list(response.tool_events or [])]:
            if event.type != "verify":
                continue
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            if metadata.get("hardFailures"):
                return True
        return False

    def _manifest(
        self,
        writer: RunArtifactWriter,
        options: RuntimeRunOptions,
        argv: list[str],
        *,
        started_at: str,
        finished_at: Optional[str],
        status: str,
        session_id: Optional[str] = None,
    ) -> dict:
        settings = get_settings()
        return {
            "schemaVersion": "trip-ai-runtime-artifact-v1",
            "runId": writer.run_id,
            "sessionId": session_id or options.session_id,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "status": status,
            "command": [self._artifact_safe_command_argument(value) for value in argv],
            # Artifacts are portable evidence bundles, not machine diagnostics.
            # Keep stable logical identifiers rather than local directory paths.
            "cwd": ".",
            "projectRoot": "trip",
            "baselineRef": options.baseline_ref,
            "baselineCommit": self._git_rev_parse(options.baseline_ref),
            "gitCommit": self._git_rev_parse("HEAD"),
            "databaseUrl": redact_database_url(settings.database_url),
            "providerStatus": self._provider_status(
                mock_providers=options.mock_providers,
                mock_map_provider=bool(options.mock_map_provider),
            ),
            "artifactFiles": writer.artifact_files(),
        }

    @staticmethod
    def _artifact_safe_command_argument(value: object) -> str:
        """Keep command shape while never persisting a local path operand."""
        return str(redact_artifact(str(value)))

    def _provider_status(
        self,
        *,
        mock_providers: Optional[bool] = None,
        mock_map_provider: bool = False,
    ) -> dict:
        settings = get_settings()
        search_diagnostics = web_search_provider_config_diagnostics()
        free_or_configured = any(provider.get("configured") for provider in search_diagnostics.get("providers") or [])
        if mock_providers is True:
            effective_mode = "mock"
            agent_provider_name = "RuntimeMockAgentProvider"
            agent_configured = True
            agent_status = "available"
            agent_model = "recorded-runtime-mock"
        elif mock_providers is False:
            effective_mode = "live" if settings.deepseek_api_key else "unconfigured"
            agent_provider_name = "DeepSeek"
            agent_configured = bool(settings.deepseek_api_key)
            agent_status = "available" if settings.deepseek_api_key else "unavailable"
            agent_model = settings.deepseek_model
        else:
            effective_mode = settings.provider_mode
            agent_provider_name = "DeepSeek"
            agent_configured = bool(settings.deepseek_api_key)
            agent_status = "available" if settings.deepseek_api_key else "unavailable"
            agent_model = settings.deepseek_model
        return redact(
            {
                "mode": effective_mode,
                "agent": {
                    "providerName": agent_provider_name,
                    "configured": agent_configured,
                    "model": agent_model,
                    "status": agent_status,
                    "timeoutSeconds": settings.deepseek_timeout_seconds,
                },
                "tools": {
                    "amap": {
                        "configured": bool(mock_map_provider or settings.map_provider_key),
                        "status": "mock"
                        if mock_map_provider
                        else "available"
                        if settings.map_provider_key
                        else "degraded",
                    },
                    "weather": {
                        "configured": bool(settings.weather_provider_key),
                        "status": "available" if settings.weather_provider_key else "degraded",
                    },
                    "search": {
                        "providerName": settings.web_search_provider,
                        "providerMode": settings.web_search_provider_mode,
                        "providerChain": search_diagnostics.get("providerChain") or [],
                        "configured": free_or_configured,
                        "diagnostics": search_diagnostics,
                    },
                },
            }
        )

    def _check_artifact_dir(self, state_dir: Path) -> dict:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            probe = state_dir / ".health-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return {"path": str(state_dir), "writable": True, "error": None}
        except Exception as error:
            return {"path": str(state_dir), "writable": False, "error": str(error)}

    def _git_rev_parse(self, ref: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", ref],
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            return None
        return result.stdout.strip()

    def _error_code(self, error: Exception) -> str:
        if isinstance(error, HTTPException):
            detail = error.detail
            if isinstance(detail, dict) and detail.get("code"):
                return str(detail["code"])
            if error.status_code == 409:
                return "stale_base_version"
        return error.__class__.__name__

    def _safe_error_message(self, error: Exception) -> str:
        if isinstance(error, HTTPException):
            return str(error.detail)
        return str(error)

    def _turn_payload(self, turn_id: str) -> dict:
        row = self.db.execute(
            "SELECT agent_request_json, agent_response_json, error_json FROM conversation_turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return {}
        return {
            "agent_request_json": self._parse_json(row["agent_request_json"], {}),
            "agent_response_json": self._parse_json(row["agent_response_json"], {}),
            "error_json": self._parse_json(row["error_json"], {}),
        }

    def _parse_json(self, value, default):
        import json

        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    def _verifier_report(self, events: list[dict]) -> dict:
        for event in reversed(events):
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
            reports = [
                preview.get(key)
                for key in ("mapVerifier", "routeVerifier", "scheduleVerifier")
                if isinstance(preview.get(key), dict)
            ]
            if reports:
                return {
                    "passed": all(report.get("passed") is True for report in reports),
                    "hardFailures": [str(item) for report in reports for item in report.get("hardFailures") or []],
                    "softFailures": [str(item) for report in reports for item in report.get("softFailures") or []],
                    "checks": [
                        check for report in reports for check in report.get("checks") or [] if isinstance(check, dict)
                    ],
                    "state": preview.get("state"),
                    "routeCoverage": preview.get("routeCoverage"),
                }
        for event in reversed(events):
            if self._is_verifier_event(event):
                metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
                return preview or metadata
        return {"passed": None, "checks": [], "hardFailures": [], "softFailures": []}

    def _is_verifier_event(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "")
        label = str(event.get("label") or "")
        return event_type == "verify" or event_type.endswith("_verifier") or "校验" in label

    def _artifact_readme(self, final: RuntimeFinalResponse) -> str:
        return (
            "# Trip AI Runtime Artifact\n\n"
            "This directory is machine-readable output from `trip-agent run`.\n\n"
            "Recommended handoff order:\n"
            "1. Read `manifest.json` for run identity and environment metadata.\n"
            "2. Read `final_response.json` for status, sessionId, activeVersionId, warnings, and nextActions.\n"
            "3. Read `context.json` and `agent_plan.json` to understand the Agent request contract.\n"
            "4. Read `session_snapshot.json`, `itinerary_snapshot.json`, and `patches.jsonl` to inspect persisted state.\n"
            "5. Read `planning_steps.jsonl`, `tool_events.jsonl`, and `verifier_report.json` for execution evidence.\n"
            "6. Read `errors.jsonl` for recoverable failures.\n\n"
            f"Status: {final.status}\n"
            f"Session: {final.session_id}\n"
            f"Active version: {final.active_version_id}\n"
        )

    @contextmanager
    def _mock_map_provider_scope(self, config: dict[str, Any]):
        if not config:
            yield
            return
        recorded_fixture = str(config.get("recordedFixture") or "").strip()
        if recorded_fixture:
            fixture_path = Path(recorded_fixture)
            if not fixture_path.is_absolute():
                fixture_path = PROJECT_ROOT / fixture_path
            original_refresh_routes = ItineraryService.refresh_routes
            try:
                if config.get("mockRouteRefresh"):
                    ItineraryService.refresh_routes = lambda _service, _plan_id, preferred_mode=None, route_pairs=None: []
                with recorded_amap_replay_scope(fixture_path):
                    yield
            finally:
                ItineraryService.refresh_routes = original_refresh_routes
            return
        original_search = MapPoiService.search
        original_search_nearby = MapPoiService.search_nearby
        original_refresh_routes = ItineraryService.refresh_routes
        original_route_init = RouteService.__init__
        original_route_fetch = RouteService._fetch_amap_route
        original_meal_assign = MealExperienceAssignmentPolicy.assign
        original_mutation_assert_current = TimelineTargetBinder.assert_current
        original_mutation_postcondition_verify = TimelineMutationPostconditionVerifier.verify
        original_dominance_margin = PoiCandidateDominanceService.DOMINANCE_MARGIN
        original_material_tradeoff = PoiCandidateDominanceService.__dict__["_material_tradeoff"]
        clear_map_poi_runtime_state()
        if config.get("forceDominantCandidates"):
            PoiCandidateDominanceService.DOMINANCE_MARGIN = 0.0
            PoiCandidateDominanceService._material_tradeoff = staticmethod(lambda *_args, **_kwargs: False)
        state = {"calls": 0}
        recover = bool(config.get("recover"))
        rate_limit_stage = str(config.get("rateLimitAtStage") or "").strip()
        rate_limit_after = self._mock_map_rate_limit_after(config)

        def maybe_rate_limit() -> None:
            if recover or not rate_limit_stage:
                return
            state["calls"] += 1
            if state["calls"] > rate_limit_after:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "provider_rate_limited",
                        "message": f"mock amap rate limit at {rate_limit_stage}",
                        "providerName": "amap",
                        "retryAfterSeconds": 60,
                    },
                )

        def fake_search(_service, city: str, keyword: str = "", category: str = "all", limit: int = 12):
            maybe_rate_limit()
            budget = current_amap_call_budget()
            if budget is not None and not budget.try_acquire(
                endpoint="place/text", keyword=keyword, category=category, source="runtime_mock_map"
            ):
                return MapPoiSearchResponse(
                    city=city,
                    keyword=keyword,
                    category=category,
                    providerName="amap-place-search",
                    queriedAt=datetime.now(timezone.utc),
                    pois=[],
                    cacheHit=False,
                )
            if config.get("reactMultiturnMuseumEdit") and category == "campus":
                candidates = self._mock_city_campus_candidates(city, keyword)
                return MapPoiSearchResponse(
                    city=city,
                    keyword=keyword,
                    category=category,
                    providerName="amap-place-search",
                    queriedAt=datetime.now(timezone.utc),
                    pois=[
                        self._mock_map_poi(
                            city,
                            name,
                            poi_type,
                            poi_category,
                            index,
                            trusted_all=True,
                        )
                        for index, (name, poi_type, poi_category) in enumerate(candidates[:1], start=1)
                    ],
                    cacheHit=False,
                )
            response = self._mock_map_search_response(
                city,
                keyword,
                category,
                limit=limit,
                trusted_food=bool(config.get("trustedFoodForDeterministicEdit")),
                trusted_all=bool(config.get("trustedPoiFixtures")),
                exact_poi_fixtures=config.get("exactPoiFixtures"),
            )
            if config.get("deterministicSingleCandidate"):
                response.pois = response.pois[:1]
            return response

        def fake_search_nearby(
            _service,
            city: str,
            longitude: float,
            latitude: float,
            keyword: str,
            category: str = "all",
            radius: int = 1500,
            limit: int = 12,
        ):
            maybe_rate_limit()
            budget = current_amap_call_budget()
            if budget is not None and not budget.try_acquire(
                endpoint="place/around", keyword=keyword, category=category, source="runtime_mock_map"
            ):
                return MapPoiSearchResponse(
                    city=city,
                    keyword=keyword,
                    category=category,
                    providerName="amap-place-search",
                    queriedAt=datetime.now(timezone.utc),
                    pois=[],
                    cacheHit=False,
                )
            return self._mock_map_search_response(
                city,
                keyword,
                category,
                limit=limit,
                origin=(float(longitude), float(latitude)),
                trusted_food=bool(config.get("trustedFoodForDeterministicEdit")),
                trusted_all=bool(config.get("trustedPoiFixtures")),
                exact_poi_fixtures=config.get("exactPoiFixtures"),
            )

        def fake_route_init(_service, map_provider_key=None, timeout_seconds=5.0):
            original_route_init(_service, map_provider_key="runtime-mock-map", timeout_seconds=timeout_seconds)

        def fake_route_fetch(_service, from_poi, to_poi, transport_mode):
            mode = "transit" if transport_mode == "public_transit" else str(transport_mode)
            budget = current_amap_call_budget()
            if budget is not None:
                budget.try_acquire(
                    endpoint=f"route/{mode}",
                    keyword=f"{from_poi.name}->{to_poi.name}",
                    category=mode,
                    source="runtime_mock_route",
                )
            # Route fixtures model the external AMap request, whose stable identity is
            # the AMap POI id rather than the regenerated local POI row id.
            from_identity = str(getattr(from_poi, "amap_id", None) or from_poi.id)
            to_identity = str(getattr(to_poi, "amap_id", None) or to_poi.id)
            pair_digest = md5(f"{from_identity}:{to_identity}:{mode}".encode("utf-8")).hexdigest()
            distance = 700 + int(pair_digest[:4], 16) % 1800
            duration = 480 + int(pair_digest[4:8], 16) % 1200
            line = f"{from_poi.longitude},{from_poi.latitude};{to_poi.longitude},{to_poi.latitude}"
            step = {
                "instruction": "runtime mock route",
                "road": "mock",
                "distance": str(distance),
                "duration": str(duration),
                "polyline": line,
            }
            if mode == "transit":
                return {
                    "status": "1",
                    "route": {
                        "transits": [
                            {
                                "distance": str(distance),
                                "duration": str(duration),
                                "cost": "3",
                                "segments": [{"walking": {"steps": [step]}, "bus": {"buslines": []}}],
                            }
                        ]
                    },
                }
            if mode == "bicycling":
                return {
                    "errcode": 0,
                    "data": {"paths": [{"distance": str(distance), "duration": str(duration), "steps": [step]}]},
                }
            return {
                "status": "1",
                "route": {"paths": [{"distance": str(distance), "duration": str(duration), "steps": [step]}]},
            }

        def fixed_meal_assign(_service, city, slots, *, request_text, seed):
            return original_meal_assign(
                _service,
                city,
                slots,
                request_text=request_text,
                seed=str(config.get("mealDiversitySeed")),
            )

        MapPoiService.search = fake_search
        MapPoiService.search_nearby = fake_search_nearby
        if config.get("mockRouteRefresh"):
            RouteService.__init__ = fake_route_init
            RouteService._fetch_amap_route = fake_route_fetch
        if config.get("mealDiversitySeed"):
            MealExperienceAssignmentPolicy.assign = fixed_meal_assign
        if config.get("skipRouteRefresh"):
            ItineraryService.refresh_routes = lambda _service, _plan_id, preferred_mode=None, route_pairs=None: []
        if config.get("timelineMutationStaleBeforeCommit"):
            concurrent_injected = {"done": False}

            def inject_concurrent_winner(_binder, _bound):
                if not concurrent_injected["done"]:
                    concurrent_injected["done"] = True
                    target_id = str((_bound.target_segment_ids or [""])[0])
                    ItineraryPatchService(_binder.db).apply_patch(
                        _bound.plan_id,
                        [
                            ItineraryPatchOperation(
                                op="replace_segment_start_time",
                                segmentId=target_id,
                                startTime="15:45",
                            )
                        ],
                        source_type="runtime_concurrent_winner",
                        base_version_id=_bound.base_version_id,
                        planning_context={"taskType": "local_modification", "skipOptionalToolRefresh": True},
                    )
                return original_mutation_assert_current(_binder, _bound)

            TimelineTargetBinder.assert_current = inject_concurrent_winner
        if config.get("timelineMutationPostconditionFailure"):

            def fail_mutation_postcondition(_verifier, before, after, spec, *, version_delta):
                report = original_mutation_postcondition_verify(
                    _verifier, before, after, spec, version_delta=version_delta
                )
                return MutationPostconditionReport(
                    passed=False,
                    diff=report.diff,
                    errors=[*report.errors, "runtime injected postcondition failure"],
                )

            TimelineMutationPostconditionVerifier.verify = fail_mutation_postcondition
        try:
            yield
        finally:
            MapPoiService.search = original_search
            MapPoiService.search_nearby = original_search_nearby
            ItineraryService.refresh_routes = original_refresh_routes
            RouteService.__init__ = original_route_init
            RouteService._fetch_amap_route = original_route_fetch
            MealExperienceAssignmentPolicy.assign = original_meal_assign
            TimelineTargetBinder.assert_current = original_mutation_assert_current
            TimelineMutationPostconditionVerifier.verify = original_mutation_postcondition_verify
            PoiCandidateDominanceService.DOMINANCE_MARGIN = original_dominance_margin
            PoiCandidateDominanceService._material_tradeoff = original_material_tradeoff
            clear_map_poi_runtime_state()

    def _mock_map_rate_limit_after(self, config: dict[str, Any]) -> int:
        if config.get("rateLimitAfter") is not None:
            try:
                return max(0, int(config.get("rateLimitAfter")))
            except (TypeError, ValueError):
                return 0
        stage = str(config.get("rateLimitAtStage") or "").lower()
        if "meal_pool" in stage:
            return 6
        if "after_first_pool" in stage or "partial" in stage:
            return 4
        return 0

    def _mock_map_search_response(
        self,
        city: str,
        keyword: str,
        category: str,
        *,
        limit: int,
        origin: Optional[tuple[float, float]] = None,
        trusted_food: bool = False,
        trusted_all: bool = False,
        exact_poi_fixtures: Optional[list[dict[str, Any]]] = None,
    ) -> MapPoiSearchResponse:
        pois = [
            self._mock_map_poi(
                city,
                name,
                poi_type,
                poi_category,
                index,
                origin=origin,
                trusted_food=trusted_food,
                trusted_all=trusted_all,
            )
            for index, (name, poi_type, poi_category) in enumerate(
                self._mock_map_candidates(
                    city,
                    keyword,
                    category,
                    exact_poi_fixtures=exact_poi_fixtures,
                ),
                start=1,
            )
        ][: max(1, min(int(limit or 12), 12))]
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category if category else "all",
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=pois,
            cacheHit=False,
        )

    def _mock_map_candidates(
        self,
        city: str,
        keyword: str,
        category: str,
        *,
        exact_poi_fixtures: Optional[list[dict[str, Any]]] = None,
    ) -> list[tuple[str, str, str]]:
        text = f"{keyword} {category}"
        for fixture in exact_poi_fixtures or []:
            if not isinstance(fixture, dict):
                continue
            fixture_city = str(fixture.get("city") or "")
            fixture_name = str(fixture.get("name") or "").strip()
            if fixture_city and fixture_city != city:
                continue
            match_keywords = [str(item).strip() for item in (fixture.get("matchKeywords") or []) if str(item).strip()]
            if not fixture_name or (
                fixture_name not in keyword and not any(item in keyword for item in match_keywords)
            ):
                continue
            return [
                (
                    fixture_name,
                    str(fixture.get("type") or "风景名胜;风景名胜;风景名胜"),
                    str(fixture.get("category") or category or "scenic"),
                )
            ]
        if "大融城" in keyword:
            return [("中关村ARTPARK大融城", "购物服务;商场;商场", "shopping")]
        if "清华美术馆" in keyword or "清华大学艺术博物馆" in keyword:
            return [
                ("清华大学艺术博物馆", "科教文化服务;博物馆;美术馆", "museum"),
                ("清华园", "风景名胜;公园广场", "scenic"),
            ]
        if category == "campus" or any(marker in text for marker in ("高校", "大学", "学院", "校园")):
            return self._mock_city_campus_candidates(city, keyword)
        if any(marker in text for marker in ("博物馆", "美术馆", "展览馆")):
            return [
                ("中国美术馆", "科教文化服务;博物馆;美术馆", "museum"),
                ("花海畔溪谷", "风景名胜;风景名胜;风景名胜", "scenic"),
                (f"{city}城市公园", "风景名胜;公园广场", "scenic"),
            ]
        if category == "food" or any(marker in text for marker in ("美食", "餐厅", "午餐", "晚餐", "吃饭", "餐饮")):
            if "炸酱面" in text:
                return [("京味炸酱面馆", "餐饮服务;中餐厅", "food")]
            if "爆肚" in text:
                return [("东来顺饭庄爆肚", "餐饮服务;中餐厅;爆肚", "food")]
            if "宫廷点心" in text or "北京奶酪" in text:
                return [("老北京宫廷点心铺", "餐饮服务;小吃快餐店;宫廷点心", "food")]
            if "铜锅涮肉" in text or "老北京涮肉" in text:
                return [("老北京铜锅涮肉馆", "餐饮服务;中餐厅;涮肉", "food")]
            if "护国寺" in text or "炒肝" in text or "豆汁" in text:
                return [("护国寺北京小吃店", "餐饮服务;小吃快餐店", "food")]
            if "卤煮" in text:
                return [("门框胡同卤煮店", "餐饮服务;中餐厅;卤煮", "food")]
            if "牛街" in text or "清真" in text:
                return [("牛街老字号清真小吃", "餐饮服务;小吃快餐店;清真", "food")]
            if "烤鸭" in text:
                return [("便宜坊老字号烤鸭店", "餐饮服务;中餐厅;烤鸭", "food")]
            if "北京菜" in text or "京味" in text:
                return [("京味北京菜馆", "餐饮服务;中餐厅;北京菜", "food")]
            return [
                (f"{city}本地菜餐厅", "餐饮服务;中餐厅", "food"),
                (f"{city}特色小吃馆", "餐饮服务;小吃快餐店", "food"),
                (f"{city}老字号餐厅", "餐饮服务;中餐厅", "food"),
                (f"{city}家常菜馆", "餐饮服务;中餐厅", "food"),
            ]
        if any(marker in text for marker in ("艺术区", "艺术街区", "艺术园区", "创意园区")):
            return [(str(keyword).strip(), "风景名胜;文化园区;艺术区", "scenic")]
        if any(marker in text for marker in ("社区生活街区", "社区市场", "本地生活街区")):
            return [(str(keyword).strip(), "风景名胜;特色街区;社区", "scenic")]
        if any(marker in text for marker in ("菜市场", "传统市场", "市井市场", "市集")):
            return [(str(keyword).strip(), "购物服务;综合市场;菜市场", "shopping")]
        if any(marker in text for marker in ("历史文化街区", "历史街区", "胡同", "老街", "古街")):
            return [(str(keyword).strip(), "风景名胜;历史文化街区;胡同", "scenic")]
        if any(marker in text for marker in ("夜景", "观景", "夜游", "塔", "天际线", "滨水", "广场")):
            return self._mock_city_night_candidates(city, keyword)
        return [
            (f"{city}城市公园", "风景名胜;公园广场", "scenic"),
            (f"{city}核心广场", "风景名胜;城市广场", "scenic"),
            (f"{city}历史街区", "风景名胜;特色街区", "scenic"),
            (f"{city}博物馆", "科教文化服务;博物馆", "scenic"),
        ]

    def _mock_city_campus_candidates(self, city: str, keyword: str) -> list[tuple[str, str, str]]:
        # The executable runtime mock must not become a second, unversioned
        # catalogue of universities.  Exact qualification entities arrive as
        # server-generated query hints from the frozen evidence asset.  Generic
        # campus requests get semantic provider-like candidates only.
        normalized_query = re.sub(r"\s+", " ", str(keyword or "").strip())
        qualified_names = {
            re.sub(r"\s+", " ", str(name or "").strip())
            for name in EntityQualificationEvidenceService.canonical_hints(
                locality=city,
                scheme="moe_project_classification",
                value="985",
            )
            if str(name or "").strip()
        }
        if normalized_query in qualified_names:
            # A query hint signed by the frozen qualification asset is an exact
            # entity lookup.  Appending generic campuses here lets downstream
            # route ordering silently replace the qualified entity, which makes
            # the mock contradict the production frontier identity contract.
            return [(normalized_query, "科教文化服务;学校;高等院校", "education")]
        names = [normalized_query] if normalized_query else []
        names.extend(
            [
                f"{city}高等院校候选一",
                f"{city}高等院校候选二",
                f"{city}大学校园候选",
                f"{city}学院校园候选",
            ]
        )
        return [
            (name, "科教文化服务;学校;高等院校", "education")
            for name in dict.fromkeys(names)
            if name
        ]

    def _mock_city_night_candidates(self, city: str, keyword: str) -> list[tuple[str, str, str]]:
        # The runtime mock models provider discovery, not a hidden city POI
        # catalogue.  Deterministic fixtures may pass explicit entities through
        # their request; otherwise the mock exposes only semantic result kinds.
        terms = [
            "公共城市夜景空间",
            "滨水夜间公共空间",
            "灯光历史街区",
            "公共天际线视野",
        ]
        normalized = str(keyword or "").strip()
        names = [normalized] if normalized and city in normalized else [f"{city}{term}" for term in terms]
        return [(name, "风景名胜;公共空间;夜景", "scenic") for name in dict.fromkeys(names) if name]

    def _mock_map_poi(
        self,
        city: str,
        name: str,
        poi_type: str,
        category: str,
        index: int,
        *,
        origin: Optional[tuple[float, float]] = None,
        trusted_food: bool = False,
        trusted_all: bool = False,
    ) -> MapPoiResponse:
        base_lon, base_lat = self._mock_city_center(city)
        if origin is not None:
            base_lon, base_lat = origin
        identity_digest = md5(f"{city}:{name}:{poi_type}".encode("utf-8")).hexdigest()
        longitude_offset = ((int(identity_digest[:8], 16) % 24001) - 12000) / 1_000_000
        latitude_offset = ((int(identity_digest[8:16], 16) % 18001) - 9000) / 1_000_000
        longitude = base_lon + longitude_offset
        latitude = base_lat + latitude_offset
        amap_id = (
            "B0FIXTURE" + md5(f"{city}:{name}:{poi_type}".encode("utf-8")).hexdigest()[:10].upper()
            if trusted_food or trusted_all
            else "mock_amap_" + md5(f"{city}:{name}:{poi_type}".encode("utf-8")).hexdigest()[:12]
        )
        is_food_mock = category == "food"
        return MapPoiResponse(
            id=amap_id,
            name=name,
            type=poi_type,
            city=city,
            district="",
            address=f"{city}{name}附近",
            longitude=longitude,
            latitude=latitude,
            category=category,
            source="amap-place-search"
            if (not is_food_mock or trusted_food or trusted_all)
            else "mock-amap-place-search",
            sourceNote=(
                "AMap place fixture for deterministic Agent CLI scenario."
                if trusted_all
                else "AMap place fixture for deterministic local meal scenario."
                if is_food_mock and trusted_food
                else "CLI eval mock AMap candidate; not a real map POI; must remain pending; only used when mockMapProvider is enabled."
                if is_food_mock
                else "CLI eval mock AMap candidate; only used when mockMapProvider is enabled."
            ),
            providerTypeCode=(
                "050100"
                if is_food_mock
                else "141201"
                if category == "education"
                else "140100"
                if "博物馆" in poi_type or "美术馆" in poi_type
                else "060100"
                if category == "shopping"
                else "110000"
                if category == "scenic"
                else "990000"
            )
            if trusted_all or (is_food_mock and trusted_food)
            else None,
            tags=["local-food-fixture"] if is_food_mock and (trusted_food or trusted_all) else [],
            openTimeToday=(
                "18:00-22:00"
                if trusted_all
                and category == "scenic"
                and any(marker in name for marker in ("观景", "公共空间", "天际线"))
                else None
            ),
            sourceClaims=(
                [
                    {
                        "claimKey": "local_food",
                        "stance": "support",
                        "source": "runtime_fixture",
                        "locality": city,
                    }
                ]
                if is_food_mock and (trusted_food or trusted_all)
                else []
            ),
            distanceMeters=round(index * 180.0, 1) if origin is not None else None,
            confidence=0.95 if trusted_all or (is_food_mock and trusted_food) else 0.35 if is_food_mock else 0.92,
            photos=[],
        )

    def _mock_city_center(self, city: str) -> tuple[float, float]:
        return {
            "北京": (116.397, 39.908),
            "上海": (121.473, 31.230),
            "杭州": (120.155, 30.274),
            "广州": (113.264, 23.129),
            "成都": (104.066, 30.572),
        }.get(city, (116.397, 39.908))


class RuntimeInputError(Exception):
    def __init__(self, error_code: str, message: str, stage: str, status: RuntimeStatus = "failed"):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.stage = stage
        self.status = status


class RuntimeStagedMapMockProvider:
    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.config = dict(config or {})

    def _synthetic_requirement_items_from_initial_plan_payloads(self) -> list[dict[str, Any]]:
        payloads = self.config.get("initialPlanPayloads")
        if not isinstance(payloads, list) or not payloads:
            return []
        payload = payloads[0] if isinstance(payloads[0], dict) else {}
        day_slots = {
            str(slot.get("slotId") or ""): slot
            for slot in payload.get("daySlots") or []
            if isinstance(slot, dict) and str(slot.get("slotId") or "").strip()
        }
        requirement_items: list[dict[str, Any]] = []
        for pool in payload.get("intentPools") or []:
            if not isinstance(pool, dict):
                continue
            pool_id = str(pool.get("poolId") or "").strip()
            intent_type = str(pool.get("intentType") or "landmark").strip() or "landmark"
            for slot_id in pool.get("assignToSlots") or []:
                slot = day_slots.get(str(slot_id))
                if slot is None:
                    continue
                day_number = int(slot.get("dayNumber") or 0)
                if day_number <= 0:
                    continue
                requirement_items.append(
                    {
                        "goalId": f"{pool_id}:{slot_id}",
                        "intentType": intent_type,
                        "requirementLevel": "required",
                        "requiredMin": 1,
                        "allowedDayNumbers": [day_number],
                        "schedulePreference": {
                            "dayPart": str(slot.get("dayPart") or "").strip() or None,
                        },
                    }
                )
        return requirement_items

    def decide_autonomy(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
        repair_feedback: str = "",
    ) -> str:
        """Simulate a healthy model Controller for deterministic offline CLI evals."""
        del timeout_seconds, repair_feedback
        if self.config.get("controllerFailure") == "timeout":
            raise TimeoutError("runtime_mock_controller_timeout")
        if self.config.get("controllerFailure") == "invalid_schema":
            return "{}"
        observation = (
            context.get("agentObservation")
            if isinstance(context.get("agentObservation"), dict)
            else context.get("observation")
            if isinstance(context.get("observation"), dict)
            else {}
        )
        itinerary = observation.get("itinerary") if isinstance(observation.get("itinerary"), dict) else {}
        lifecycle = str(itinerary.get("lifecycleState") or observation.get("itineraryLifecycleState") or "")
        message = str(context.get("latestUserMessage") or context.get("effectiveUserMessage") or "")
        continuation_kind = str((context.get("continuationContext") or {}).get("kind") or "")
        cycle_index = int(observation.get("cycleIndex") or 0)
        inventory = observation.get("targetInventory") if isinstance(observation.get("targetInventory"), dict) else {}
        segment_ids = [str(item) for item in inventory.get("segmentIds") or [] if str(item)]
        segment_refs = [item for item in observation.get("segmentRefs") or [] if isinstance(item, dict)]
        museum_ref = next(
            (
                item
                for item in segment_refs
                if int(item.get("dayNumber") or 0) == 1 and str(item.get("intentType") or "") == "museum"
            ),
            None,
        )
        current_version = str((observation.get("versionLineage") or {}).get("currentVersionId") or "")
        pending_groups = (observation.get("candidateState") or {}).get("pendingGroups") or []
        unresolved_slots = observation.get("unresolvedSlots") or []
        required_unresolved_slots = [
            item for item in unresolved_slots if isinstance(item, dict) and bool(item.get("required"))
        ]
        unresolved_segment_ids = [
            str(item.get("segmentId"))
            for item in unresolved_slots
            if isinstance(item, dict) and str(item.get("segmentId") or "")
        ]
        dominant_group = next(
            (item for item in pending_groups if isinstance(item, dict) and len(item.get("safeCandidates") or []) == 1),
            None,
        )
        dominant_candidate = (
            dominant_group.get("safeCandidates")[0]
            if isinstance(dominant_group, dict) and dominant_group.get("safeCandidates")
            else None
        )
        museum_replacement = bool(
            cycle_index == 0
            and museum_ref is not None
            and any(marker in message for marker in ("改为", "修改为", "换成", "替换为"))
            and any(marker in message for marker in ("博物馆", "美术馆", "艺术馆"))
        )
        manual_museum_continuation = bool(
            cycle_index == 0
            and museum_ref is not None
            and str((context.get("continuationContext") or {}).get("kind") or "") == "manual_continuation"
            and any(marker in message for marker in ("博物馆", "美术馆", "艺术馆"))
        )
        replacement_match = re.search(r"(?:改为|修改为|换成|替换为)([^，。]+)", message)
        replacement_search_intent = replacement_match.group(1).strip() if replacement_match else ""
        react_time_edit = bool(
            self.config.get("reactMultiturnMuseumEdit")
            and cycle_index == 0
            and museum_ref is not None
            and "15:30" in message
        )
        if cycle_index == 0 and message.strip() == "想出去玩":
            action = "ask_user"
        elif cycle_index == 0 and continuation_kind == "retry_model_planning":
            action = "draft_itinerary"
        elif react_time_edit:
            action = "patch_itinerary"
        elif (museum_replacement or manual_museum_continuation) and dominant_candidate is None:
            action = "resolve_poi"
        elif (
            cycle_index > 0
            and pending_groups
            and any(len(item.get("safeCandidates") or []) > 1 for item in pending_groups if isinstance(item, dict))
        ):
            action = "ask_user"
        elif cycle_index > 0 and dominant_candidate is not None:
            action = "patch_itinerary"
        elif cycle_index == 1 and required_unresolved_slots:
            action = "resolve_poi"
        elif cycle_index > 0:
            action = "finish"
        elif lifecycle in {"", "empty_scaffold", "no_itinerary"}:
            action = "draft_itinerary"
        elif re.search(r"(只看|不要改|不修改|先不改|当前.*安排|哪一天|几点|在哪一天)", message):
            action = "read_itinerary"
        elif dominant_candidate is not None:
            action = "patch_itinerary"
        elif pending_groups and any(
            len(item.get("safeCandidates") or []) > 1 for item in pending_groups if isinstance(item, dict)
        ):
            action = "ask_user"
        elif re.search(r"(优化.*路线|路线.*优化|少换乘|重排路线)", message):
            action = "optimize_route"
        elif re.search(r"(改|换|替换|调整|移动|删除|添加|加入|时间)", message) and segment_ids:
            action = "patch_itinerary"
        elif re.search(r"(天气|预约|门票|风险|官方|核验)", message):
            action = "verify_external_facts"
        else:
            action = "read_itinerary"
        requirement_items = [
            item
            for item in (observation.get("requirementCoverage") or {}).get("required") or []
            if isinstance(item, dict) and str(item.get("goalId") or "")
        ]
        if not requirement_items:
            requirement_items = self._synthetic_requirement_items_from_initial_plan_payloads()
        goal_ids = list(dict.fromkeys(str(item["goalId"]) for item in requirement_items))
        hard_goal_ids = list(
            dict.fromkeys(
                str(item["goalId"])
                for item in requirement_items
                if item.get("requirementLevel") not in {"soft_experience", "optional"}
                and int(item.get("requiredMin") or 0) > 0
            )
        )
        optional_goal_ids = list(
            dict.fromkeys(
                str(item["goalId"])
                for item in requirement_items
                if item.get("requirementLevel") in {"soft_experience", "optional"}
            )
        )
        required_goal_counts = {
            str(item.get("goalId") or ""): int(
                item.get("requiredMin") or item.get("target") or item.get("requiredCount") or 1
            )
            for item in requirement_items
            if str(item.get("goalId") or "") in hard_goal_ids
        }
        resolve_slot = (
            required_unresolved_slots[0]
            if required_unresolved_slots
            else next((item for item in unresolved_slots if isinstance(item, dict)), {})
        )
        resolve_segment_id = str(
            (museum_ref or {}).get("segmentId") or (museum_ref or {}).get("id") or resolve_slot.get("segmentId") or ""
        )
        resolve_segment_ref = next(
            (item for item in segment_refs if str(item.get("segmentId") or item.get("id") or "") == resolve_segment_id),
            {},
        )
        resolve_goal_id = str(
            (museum_ref or {}).get("goalId") or resolve_slot.get("goalId") or resolve_segment_ref.get("goalId") or ""
        )
        resolve_search_intent = str(
            resolve_slot.get("rawNeed")
            or resolve_segment_ref.get("poiName")
            or resolve_segment_ref.get("rawNeed")
            or resolve_segment_ref.get("intentType")
            or ""
        )
        resolved_dates = context.get("resolvedTripDates") if isinstance(context.get("resolvedTripDates"), dict) else {}
        trip_dates = [str(item) for item in resolved_dates.get("dates") or [] if str(item)] or [""]
        trip_day_numbers = list(range(1, len(trip_dates) + 1))
        required_goals_by_day: dict[int, list[str]] = {day_number: [] for day_number in trip_day_numbers}
        scheduled_goal_ids: set[str] = set()
        for requirement in requirement_items:
            goal_id = str(requirement.get("goalId") or "")
            if goal_id not in hard_goal_ids or goal_id in scheduled_goal_ids:
                continue
            scheduled_goal_ids.add(goal_id)
            allowed_days = [
                int(day_number)
                for day_number in requirement.get("allowedDayNumbers") or trip_day_numbers
                if int(day_number) in required_goals_by_day
            ]
            target = int(required_goal_counts.get(goal_id) or 1)
            for day_number in allowed_days[:target]:
                required_goals_by_day[day_number].append(goal_id)
        optional_goals_by_day: dict[int, list[str]] = {day_number: [] for day_number in trip_day_numbers}
        optional_occurrence_count = 0
        optional_occurrence_limit = 3
        scheduled_optional_goal_ids: set[str] = set()
        for requirement in requirement_items:
            goal_id = str(requirement.get("goalId") or "")
            if goal_id not in optional_goal_ids or goal_id in scheduled_optional_goal_ids:
                continue
            scheduled_optional_goal_ids.add(goal_id)
            allowed_days = [
                int(day_number)
                for day_number in requirement.get("allowedDayNumbers") or trip_day_numbers
                if int(day_number) in optional_goals_by_day
            ]
            target = int(
                requirement.get("requiredMin") or requirement.get("target") or requirement.get("requiredCount") or 1
            )
            for day_number in allowed_days[:target]:
                if optional_occurrence_count >= optional_occurrence_limit:
                    break
                optional_goals_by_day[day_number].append(goal_id)
                optional_occurrence_count += 1
        requirement_by_goal = {str(item["goalId"]): item for item in requirement_items}
        day_part_defaults = {
            "campus_visit": "morning",
            "meal": "noon",
            "local_food": "noon",
            "museum": "afternoon",
            "night_view": "night",
        }
        start_defaults = {
            "morning": "09:00",
            "noon": "12:00",
            "afternoon": "14:00",
            "evening": "19:00",
            "night": "19:00",
            "flexible": "10:00",
        }
        duration_defaults = {
            "campus_visit": 120,
            "meal": 60,
            "local_food": 60,
            "museum": 120,
            "night_view": 90,
        }
        occurrence_schedule_hints: list[dict[str, Any]] = []
        for day_number in trip_day_numbers:
            scheduled_for_day = [
                *required_goals_by_day.get(day_number, []),
                *optional_goals_by_day.get(day_number, []),
            ]
            for sequence, goal_id in enumerate(scheduled_for_day, start=1):
                requirement = requirement_by_goal[goal_id]
                intent_type = str(requirement.get("intentType") or "")
                day_part = str((requirement.get("schedulePreference") or {}).get("dayPart") or "")
                if day_part not in {"morning", "noon", "afternoon", "evening", "night", "flexible"}:
                    day_part = day_part_defaults.get(intent_type, "flexible")
                duration = duration_defaults.get(intent_type, 90)
                occurrence_schedule_hints.append(
                    {
                        "goalId": goal_id,
                        "dayNumber": day_number,
                        "dayPart": day_part,
                        "sequence": sequence,
                        "preferredStartTime": start_defaults[day_part],
                        "durationEstimate": {
                            "min": max(1, duration - 30),
                            "preferred": duration,
                            "max": duration + 30,
                        },
                        "estimateSource": "controller_estimate",
                        "confidence": 0.95,
                    }
                )
        request_contract = (
            context.get("requestIntentContract")
            if isinstance(context.get("requestIntentContract"), dict)
            else {}
        )
        route_contract = (
            request_contract.get("routeDecisionContract")
            if isinstance(request_contract.get("routeDecisionContract"), dict)
            else {}
        )
        detour_envelope = (
            deepcopy(route_contract.get("detourTolerance"))
            if isinstance(route_contract.get("detourTolerance"), dict)
            else {"maxGeneralizedCostDelta": 30.0, "maxDetourRatio": 0.35}
        )
        transport_mode = str((route_contract.get("provenance") or {}).get("transportMode") or "transit")
        if transport_mode not in {"transit", "walking", "bicycling", "driving"}:
            transport_mode = "transit"
        semantic_mutation_intent = (
            {
                "schemaVersion": "timeline-mutation-intent-v1",
                "operation": "set_start_time",
                "selector": {
                    "dayNumber": 1,
                    "intentType": "museum",
                    "currentText": str((museum_ref or {}).get("poiName") or "美术馆"),
                },
                "replacement": {"startTime": "15:30"},
                "preserve": ["target_duration", "other_days", "user_locked_times"],
                "confidence": 0.99,
                "source": "model_semantic_extractor",
                "sourceText": message,
            }
            if react_time_edit
            else None
        )
        action_directive = (
            {
                "type": "draft_itinerary",
                "goalPriority": goal_ids,
                "dayStrategies": [
                    {
                        "dayNumber": day_number,
                        "theme": "模型主导的核心目标日" if day_number == 1 else "模型主导的续行日",
                        "requiredGoalIds": required_goals_by_day.get(day_number, []),
                        "requiredGoalCounts": {goal_id: 1 for goal_id in required_goals_by_day.get(day_number, [])},
                        "optionalGoalIds": optional_goals_by_day.get(day_number, []),
                        "pace": "standard",
                        "maxRouteAnchors": 4,
                    }
                    for day_number, _trip_date in enumerate(trip_dates, start=1)
                ],
                "optionalExperienceBudget": optional_occurrence_count,
                "routePlanningPolicy": {
                    "objective": "least_generalized_cost",
                    "source": "controller_estimate",
                    "allowExperienceDetour": optional_occurrence_count > 0,
                    "mobilityProfile": {
                        "transportMode": transport_mode,
                        "paceClass": "standard",
                    },
                    "detourEnvelope": detour_envelope,
                },
                "occurrenceScheduleHints": occurrence_schedule_hints,
                "searchPriority": ["exact_entity", "hard_constraint", "required"],
                "candidateSelectionPolicy": {
                    "autoSelectWhenDominant": True,
                    "askWhenMaterialTradeoff": True,
                    "preferLowDetour": True,
                    "avoidRecentEntities": True,
                },
                "schedulePolicy": {
                    "respectOpeningWindowsWhenKnown": True,
                    "allowProvisionalWhenUnknown": True,
                },
            }
            if action == "draft_itinerary"
            else {
                "type": "read_itinerary",
                "queryType": "timeline_summary",
                "targetText": message,
                "includeDay": True,
                "includeTime": True,
                "includeGroundingStatus": True,
            }
            if action == "read_itinerary"
            else {
                "type": "resolve_poi",
                "targetGoalId": resolve_goal_id,
                "targetSegmentIds": [resolve_segment_id] if resolve_segment_id else [],
                "searchIntent": replacement_search_intent
                if museum_replacement and replacement_search_intent
                else message.strip()
                if manual_museum_continuation
                else resolve_search_intent,
                "searchMode": "text",
                "maxCandidates": 4,
                "autoSelectPolicy": "dominant_safe_candidate_only",
                "askUserPolicy": "material_tradeoff_only",
            }
            if action == "resolve_poi"
            else {
                "type": "patch_itinerary",
                **(
                    {
                        "requestedOutcome": "第一天美术馆改到 15:30 并保持原时长",
                        "mutationIntent": semantic_mutation_intent,
                        "preserve": semantic_mutation_intent["preserve"],
                        "maxChangedSegmentCount": 1,
                    }
                    if semantic_mutation_intent is not None
                    else {
                        "operationIntent": "replace_segment_poi_from_candidate"
                        if dominant_candidate is not None
                        else "replace_segment_start_time",
                        "baseVersionId": current_version,
                        "targetSegmentIds": [str(dominant_group.get("sourceSegmentId"))]
                        if dominant_candidate is not None
                        else segment_ids[:1],
                        "requestedOutcome": message,
                        "preserve": (
                            ["target_duration", "other_days", "other_segments", "user_locked_times"]
                            if dominant_candidate is not None
                            else ["other_days", "other_segments", "user_locked_times"]
                        ),
                        "maxChangedSegmentCount": 1,
                        **(
                            {
                                "candidateId": str(dominant_group.get("id")),
                                "amapPoiId": str(dominant_candidate.get("id")),
                            }
                            if dominant_candidate is not None
                            else {"startTime": "09:00"}
                        ),
                    }
                ),
            }
            if action == "patch_itinerary"
            else {
                "type": "optimize_route",
                "baseVersionId": current_version,
                "dayNumbers": [1],
                "optimizationObjective": "balanced",
            }
            if action == "optimize_route"
            else {
                "type": "verify_external_facts",
                "factTypes": ["weather"] if "天气" in message else ["risk", "reservation"],
                "segmentIds": unresolved_segment_ids[:1],
            }
            if action == "verify_external_facts"
            else {
                "type": "ask_user",
                "question": "候选存在实质差异，请选择一个或手动填写。",
                "choiceIds": [
                    str(candidate.get("id"))
                    for group in pending_groups[:1]
                    if isinstance(group, dict)
                    for candidate in group.get("safeCandidates") or []
                    if isinstance(candidate, dict) and candidate.get("id")
                ],
            }
            if action == "ask_user"
            else {"type": "finish", "assistantReply": "已依据持久化结果完成本轮。"}
        )
        return json.dumps(
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": action,
                "actionDirective": action_directive,
            },
            ensure_ascii=False,
        )

    def decide_autonomy_lite(self, context: dict[str, Any], *, timeout_seconds: float) -> str:
        del timeout_seconds
        failure = self.config.get("controllerLiteFailure")
        if failure == "timeout" or (
            self.config.get("controllerFailure") == "timeout" and self.config.get("controllerLiteMode") != "success"
        ):
            raise TimeoutError("runtime_mock_controller_lite_timeout")
        if failure == "invalid_schema":
            return "{}"
        if context.get("schemaVersion") == "conversation-intent-context-v1":
            message = str(context.get("message") or "")
            if message.startswith(("为什么", "为何", "怎么")):
                intent = "inspect_or_explain"
                requested_scope = "current_action"
                is_question = True
                is_negated = False
            elif message.startswith(("不要", "别", "取消", "停止")):
                intent = "cancel_action"
                requested_scope = "current_action"
                is_question = False
                is_negated = True
            elif "不同玩法" in message or "其他方案" in message:
                intent = "continue_plan_expansion"
                requested_scope = "planning_root"
                is_question = False
                is_negated = False
            elif message.strip() == "重新构建":
                intent = "regenerate_from_scratch"
                requested_scope = "full_task"
                is_question = False
                is_negated = False
            else:
                intent = (
                    "modify_itinerary"
                    if bool((context.get("state") or {}).get("hasActiveVersion"))
                    else "create_itinerary"
                )
                requested_scope = "active_itinerary" if intent == "modify_itinerary" else "new_itinerary"
                is_question = False
                is_negated = False
            return json.dumps(
                {
                    "intent": intent,
                    "confidence": 0.92,
                    "requestedScope": requested_scope,
                    "isQuestion": is_question,
                    "isNegated": is_negated,
                },
                ensure_ascii=False,
            )
        observation = context.get("observation") if isinstance(context.get("observation"), dict) else {}
        lifecycle = str((observation.get("itinerary") or {}).get("lifecycleState") or "")
        action = "draft_itinerary" if lifecycle in {"", "no_active_version", "empty_scaffold"} else "finish"
        return json.dumps(
            {
                "schemaVersion": "agent-decision-lite-v1",
                "primaryAction": action,
                "confidence": 0.92,
                "reasonCode": "runtime_mock_lite_initial_draft"
                if action == "draft_itinerary"
                else "runtime_mock_lite_finish",
                "userVisibleReason": "信息已足够，可起草行程。"
                if action == "draft_itinerary"
                else "当前没有需要继续执行的安全动作。",
            },
            ensure_ascii=False,
        )

    def generate(self, _context: dict) -> str:
        return json.dumps(
            {
                "reply": "Runtime staged map mock provider only supports initial DaySlot planning.",
                "mode": "cannot_plan",
                "warnings": ["runtime_staged_map_mock_generate_not_used"],
            },
            ensure_ascii=False,
        )

    def generate_initial_plan(self, context: dict) -> str:
        city = str(context.get("selectedCity") or "北京")
        resolved_dates = context.get("resolvedTripDates") if isinstance(context.get("resolvedTripDates"), dict) else {}
        dates = [str(item) for item in resolved_dates.get("dates") or [] if str(item).strip()]
        if not dates:
            dates = ["2026-10-01", "2026-10-02"]
        message = str(context.get("effectiveUserMessage") or context.get("latestUserMessage") or "")
        request_contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        qualification_constraint = (
            request_contract.get("entityQualificationConstraint")
            if isinstance(request_contract.get("entityQualificationConstraint"), dict)
            else None
        )
        qualified_campus_hints = self._qualified_campus_hints(
            city,
            qualification_constraint=qualification_constraint,
        )
        campus_hints = qualified_campus_hints or self._campus_hints(city)
        campus_hint_source = (
            "versioned_qualification_evidence"
            if qualified_campus_hints
            else "generic_provider_discovery_hint"
        )
        if self.config.get("reactMultiturnMuseumEdit"):
            return self._react_multiturn_initial_plan(
                city,
                dates,
                campus_hints=campus_hints,
                campus_hint_source=campus_hint_source,
            )
        wants_food = any(marker in message for marker in ("美食", "餐厅", "特色饮食", "当地特色", "小吃"))
        wants_night = "夜景" in message or "夜游" in message
        night_goal = (
            GoalLedgerService()
            .from_message(
                message,
                day_count=len(dates),
                clarify_ambiguous_night=True,
            )
            .goal("night_view")
        )
        night_day_numbers = set(
            (night_goal.allowed_day_numbers or tuple(range(1, len(dates) + 1)))[
                : max(0, min(int(night_goal.preferred_count or 0), len(dates)))
            ]
        )
        wants_campus = any(marker in message for marker in ("高校", "大学", "学院", "校园"))
        wants_museum = any(marker in message for marker in ("博物馆", "美术馆", "展览馆"))
        primary_kind = "campus" if wants_campus else "landmark"
        primary_intent = "campus_visit" if wants_campus else "landmark"
        primary_need = "高校参观" if wants_campus else "城市核心地点"
        primary_pool_id = "campus_visit_pool" if wants_campus else "landmark_pool"
        campus_slot_ids: list[str] = []
        landmark_slot_entities: list[tuple[str, str]] = []
        landmark_hints = self._night_hints(city)
        museum_slot_ids: list[str] = []
        night_slot_ids: list[str] = []
        meal_slot_ids: list[str] = []
        day_slots: list[dict[str, Any]] = []
        for index, trip_date in enumerate(dates, start=1):
            morning_id = f"day{index}_morning_primary"
            lunch_id = f"day{index}_lunch"
            dinner_id = f"day{index}_dinner"
            afternoon_id = f"day{index}_afternoon_primary"
            evening_id = f"day{index}_evening_night"
            campus_slot_ids.append(morning_id)
            campus_slot_ids.append(afternoon_id)
            morning_landmark = landmark_hints[((index - 1) * 2) % len(landmark_hints)]
            afternoon_landmark = landmark_hints[((index - 1) * 2 + 1) % len(landmark_hints)]
            if not wants_campus:
                landmark_slot_entities.extend([(morning_id, morning_landmark), (afternoon_id, afternoon_landmark)])
            museum_id = f"day{index}_art_museum"
            include_museum = wants_museum and index == 1
            if include_museum:
                museum_slot_ids.append(museum_id)
            include_evening = wants_night and index in night_day_numbers
            if include_evening:
                night_slot_ids.append(evening_id)
            if wants_food:
                meal_slot_ids.extend([lunch_id, dinner_id])
            day_slots.extend(
                [
                    {
                        "slotId": morning_id,
                        "dayNumber": index,
                        "date": trip_date,
                        "timeWindow": "09:00-11:00",
                        "startTime": "09:00",
                        "durationMinutes": 120,
                        "kind": primary_kind,
                        "rawNeed": f"上午{primary_need}" if wants_campus else morning_landmark,
                        "routeAnchor": True,
                        "priority": 90,
                        "notes": "runtime eval mock DaySlot；最终地点仍需高德候选选择。",
                    },
                    {
                        "slotId": lunch_id,
                        "dayNumber": index,
                        "date": trip_date,
                        "timeWindow": "12:00-13:00",
                        "startTime": "12:00",
                        "durationMinutes": 60,
                        "kind": "meal",
                        "rawNeed": "当地特色美食" if wants_food else "午餐",
                        "routeAnchor": wants_food,
                        "priority": 72 if wants_food else 30,
                        "notes": "requiredGrounding=true；explicit_food_experience"
                        if wants_food
                        else "普通午餐时间；无具体餐厅时路线跳过。",
                    },
                    {
                        "slotId": afternoon_id,
                        "dayNumber": index,
                        "date": trip_date,
                        "timeWindow": "13:30-15:00" if include_museum else "14:00-16:00",
                        "startTime": "13:30" if include_museum else "14:00",
                        "durationMinutes": 90 if include_museum else 120,
                        "kind": primary_kind,
                        "rawNeed": f"下午{primary_need}" if wants_campus else afternoon_landmark,
                        "routeAnchor": True,
                        "priority": 85,
                        "notes": "runtime eval mock DaySlot；最终地点仍需高德候选选择。",
                    },
                    *(
                        [
                            {
                                "slotId": museum_id,
                                "dayNumber": index,
                                "date": trip_date,
                                "timeWindow": "15:30-17:00",
                                "startTime": "15:30",
                                "durationMinutes": 90,
                                "kind": "museum",
                                "rawNeed": "博物馆或美术馆参观",
                                "routeAnchor": True,
                                "priority": 88,
                                "notes": "runtime eval mock required museum DaySlot；最终地点仍需高德候选选择。",
                            }
                        ]
                        if include_museum
                        else []
                    ),
                    *(
                        [
                            {
                                "slotId": dinner_id,
                                "dayNumber": index,
                                "date": trip_date,
                                "timeWindow": "18:30-19:45" if include_museum else "17:30-18:30",
                                "startTime": "18:30" if include_museum else "17:30",
                                "durationMinutes": 60,
                                "kind": "meal",
                                "rawNeed": "当地特色美食",
                                "routeAnchor": True,
                                "priority": 72,
                                "notes": "requiredGrounding=true；explicit_food_experience",
                            }
                        ]
                        if wants_food
                        else []
                    ),
                    *(
                        [
                            {
                                "slotId": evening_id,
                                "dayNumber": index,
                                "date": trip_date,
                                "timeWindow": "20:00-21:30" if include_museum else "19:00-20:30",
                                "startTime": "20:00" if include_museum else "19:00",
                                "durationMinutes": 90,
                                "kind": "night_view" if wants_night else "area_walk",
                                "rawNeed": "夜景观景点" if wants_night else "区域漫步",
                                "routeAnchor": True,
                                "priority": 75,
                                "notes": "runtime eval mock DaySlot；最终地点仍需高德候选选择。",
                            }
                        ]
                        if include_evening
                        else []
                    ),
                ]
            )
        intent_pools = []
        if wants_campus:
            intent_pools.append(
                {
                    "poolId": primary_pool_id,
                    "rawNeed": primary_need,
                    "city": city,
                    "intentType": primary_intent,
                    "targetCount": len(campus_slot_ids),
                    "preferredTypes": ["大学", "学院", "高等院校", "校区"],
                    "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                    "routePreference": {
                        "sameDayUnique": True,
                        "candidateHintSource": campus_hint_source,
                    },
                    "assignToSlots": campus_slot_ids,
                    "candidateHints": campus_hints,
                    "hintPolicy": "llm_common_knowledge_hint",
                }
            )
        else:
            intent_pools.extend(
                {
                    "poolId": f"landmark_pool_{slot_id}",
                    "rawNeed": entity_name,
                    "city": city,
                    "intentType": primary_intent,
                    "targetCount": 1,
                    "preferredTypes": ["风景名胜", "地标", "广场", "塔"],
                    "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                    "routePreference": {"sameDayUnique": True},
                    "assignToSlots": [slot_id],
                    "candidateHints": [entity_name],
                    "hintPolicy": "llm_common_knowledge_hint",
                }
                for slot_id, entity_name in landmark_slot_entities
            )
        if night_slot_ids:
            intent_pools.append(
                {
                    "poolId": "night_view_pool" if wants_night else "area_walk_pool",
                    "rawNeed": "夜景观景点" if wants_night else "区域漫步",
                    "city": city,
                    "intentType": "night_view" if wants_night else "area_walk",
                    "targetCount": len(night_slot_ids),
                    "preferredTypes": ["地标", "桥", "塔", "广场", "商圈", "购物中心", "体育场馆", "观景点"],
                    "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                    "routePreference": {"sameDayUnique": True},
                    "assignToSlots": night_slot_ids,
                    "candidateHints": self._night_hints(city),
                    "hintPolicy": "llm_common_knowledge_hint",
                }
            )
        if museum_slot_ids:
            intent_pools.append(
                {
                    "poolId": "museum_pool",
                    "rawNeed": "博物馆或美术馆参观",
                    "city": city,
                    "intentType": "museum",
                    "targetCount": len(museum_slot_ids),
                    "preferredTypes": ["博物馆", "美术馆", "展览馆", "纪念馆"],
                    "rejectedTypes": ["酒店", "公司", "停车场", "住宅", "商场"],
                    "routePreference": {"sameDayUnique": True},
                    "assignToSlots": museum_slot_ids,
                    "candidateHints": [f"{city} 国家博物馆", f"{city} 美术馆", f"{city} 城市博物馆"],
                    "hintPolicy": "llm_common_knowledge_hint",
                }
            )
        if meal_slot_ids:
            intent_pools.append(
                {
                    "poolId": "meal_pool",
                    "rawNeed": "当地特色美食",
                    "city": city,
                    "intentType": "meal",
                    "targetCount": 1,
                    "preferredTypes": ["餐饮服务", "中餐厅", "小吃", "饭店", "餐厅"],
                    "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区", "写字楼"],
                    "routePreference": {"sameDayUnique": True, "preferNearAdjacentAnchors": True},
                    "assignToSlots": meal_slot_ids,
                    "candidateHints": [f"{city}本地菜餐厅", f"{city}特色小吃馆", f"{city}老字号餐厅"],
                    "hintPolicy": "user_explicit_hint",
                }
            )
        return json.dumps(
            {
                "reply": "已按 runtime eval mock 拆解 DaySlot/IntentPool，后续仍通过地图候选选择。",
                "mode": "day_slots",
                "daySlots": day_slots,
                "intentPools": intent_pools,
                "warnings": ["RUNTIME_EVAL_MOCK_INITIAL_PLAN"],
            },
            ensure_ascii=False,
        )

    def generate_initial_portfolio(self, context: dict, *, repair_feedback: str = "") -> str:
        """Recorded, identity-free portfolio skeletons for runtime golden tests."""
        plan = json.loads(self.generate_initial_plan(context))
        city = str(context.get("selectedCity") or "目的地")
        contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        required_goal_ids = [
            str(item.get("goalId") or "")
            for item in contract.get("requiredIntents") or []
            if str(item.get("goalId") or "")
            and str(item.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
        ]
        required_goal_by_intent = {
            str(item.get("intentType") or ""): str(item.get("goalId") or "")
            for item in contract.get("requiredIntents") or []
            if str(item.get("goalId") or "")
            and str(item.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
        }
        soft_goal_by_intent = {
            str(item.get("intentType") or ""): str(item.get("goalId") or "")
            for item in contract.get("requiredIntents") or []
            if str(item.get("goalId") or "")
            and str(item.get("requirementLevel") or "required") in {"soft_experience", "optional"}
        }
        axes = ["culture_deep_dive", "local_immersion", "food_led", "photo_night"]
        optional_specs = [
            ("art_walk", "艺术文化街区", "艺术文化街区", "area_walk", ["艺术文化街区"]),
            ("local_life", "社区本地生活", "社区生活街区", "area_walk", ["社区生活街区"]),
            ("market_walk", "传统市场漫步", "传统市场", "area_walk", ["传统市场"]),
            ("heritage_walk", "夜间历史街区摄影", "历史文化街区", "area_walk", ["历史文化街区"]),
        ]
        companion_specs = {
            "art_walk": ("heritage_walk", "历史文化街区", ["历史文化街区"]),
            "local_life": ("art_walk", "艺术创意街区", ["艺术创意街区"]),
            "market_walk": ("local_life", "社区生活街区", ["社区生活街区"]),
            "heritage_walk": ("market_walk", "传统市场", ["传统市场"]),
        }
        proposals = []
        for index, (axis, optional_spec) in enumerate(zip(axes, optional_specs), start=1):
            family, optional_day_role, optional_raw_need, optional_intent_type, hints = optional_spec
            brief_id = f"runtime_brief_{index}"
            pools = []
            goal_by_slot: dict[str, str] = {}
            soft_goal_by_slot: dict[str, str] = {}
            for raw_pool in plan.get("intentPools") or []:
                intent = str(raw_pool.get("intentType") or "")
                goal_id = required_goal_by_intent.get(intent)
                soft_goal_id = soft_goal_by_intent.get(intent)
                if not goal_id and not soft_goal_id:
                    continue
                pool = dict(raw_pool)
                selected_slot_ids = [str(slot_id) for slot_id in pool.get("assignToSlots") or [] if str(slot_id)][:1]
                pool.update(
                    {
                        "poolId": f"{brief_id}_{pool.get('poolId')}",
                        "briefId": brief_id,
                        "requirementLevel": "required" if goal_id else "optional",
                        "goalId": goal_id or None,
                        "softGoalId": soft_goal_id or None,
                        "targetCount": 1,
                        "assignToSlots": selected_slot_ids,
                    }
                )
                pools.append(pool)
                for slot_id in selected_slot_ids:
                    if goal_id:
                        goal_by_slot[str(slot_id)] = goal_id
                    if soft_goal_id:
                        soft_goal_by_slot[str(slot_id)] = soft_goal_id
            slots = []
            for raw_slot in plan.get("daySlots") or []:
                slot = dict(raw_slot)
                slot_id = str(slot.get("slotId") or "")
                if slot_id not in goal_by_slot and slot_id not in soft_goal_by_slot:
                    continue
                if slot_id in goal_by_slot:
                    slot["requiredGoalId"] = goal_by_slot[slot_id]
                if slot_id in soft_goal_by_slot:
                    slot["softGoalId"] = soft_goal_by_slot[slot_id]
                slots.append(slot)
            companion_family, companion_raw_need, companion_hints = companion_specs[family]
            optional_families = [
                (family, optional_raw_need, hints),
                (companion_family, companion_raw_need, companion_hints),
            ]
            optional_days = (1, 2) if index % 2 else (2, 2)
            for optional_index, ((optional_family, raw_need, family_hints), optional_day) in enumerate(
                zip(optional_families, optional_days), start=1
            ):
                optional_slot_id = f"{brief_id}_optional_{optional_index}"
                slots.append(
                    {
                        "slotId": optional_slot_id,
                        "dayNumber": optional_day,
                        "timeWindow": "evening",
                        "startTime": "19:00",
                        "durationMinutes": 60,
                        "kind": "activity",
                        "rawNeed": raw_need,
                        "routeAnchor": True,
                        "priority": 55,
                        "optionalExperienceFamily": optional_family,
                    }
                )
                pools.append(
                    {
                        "poolId": f"{brief_id}_{optional_family}",
                        "briefId": brief_id,
                        "rawNeed": raw_need,
                        "city": city,
                        "intentType": optional_intent_type,
                        "targetCount": 1,
                        "requirementLevel": "optional",
                        "assignToSlots": [optional_slot_id],
                        "optionalExperienceFamily": optional_family,
                        "candidateHints": family_hints,
                        "hintPolicy": "llm_common_knowledge_hint",
                        "routePreference": {"sameDayUnique": True},
                    }
                )
            anchor_count_by_day = {
                day_number: sum(
                    1
                    for slot in slots
                    if int(slot.get("dayNumber") or 1) == day_number and bool(slot.get("routeAnchor"))
                )
                for day_number in (1, 2)
            }
            required_count_by_day = {
                day_number: sum(
                    1 for slot in slots if int(slot.get("dayNumber") or 1) == day_number and slot.get("requiredGoalId")
                )
                for day_number in (1, 2)
            }
            soft_count_by_day = {
                day_number: sum(
                    1 for slot in slots if int(slot.get("dayNumber") or 1) == day_number and slot.get("softGoalId")
                )
                for day_number in (1, 2)
            }
            optional_count_by_day = {
                day_number: sum(
                    1
                    for slot in slots
                    if int(slot.get("dayNumber") or 1) == day_number and slot.get("optionalExperienceFamily")
                )
                for day_number in (1, 2)
            }
            transport = str(((context.get("creativePortfolioTransportPreferences") or ["public_transit"])[0]))

            def density_evidence(day_number):
                return [
                    "pace=standard",
                    "availableWindow=full_day",
                    f"requiredGoalCount={required_count_by_day[day_number]}",
                    f"explicitSoftGoalCount={soft_count_by_day[day_number]}",
                    f"briefOptionalCount={optional_count_by_day[day_number]}",
                    f"transport={transport}",
                ]

            proposals.append(
                {
                    "brief": {
                        "briefId": brief_id,
                        "title": f"运行时方案 {index}",
                        "primaryAxis": axis,
                        "narrativeArc": f"{axis} 的可执行{city}两日路线",
                        "requiredGoalIds": required_goal_ids,
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "必选体验",
                                "targetRouteAnchors": anchor_count_by_day[1],
                                "densityEvidence": density_evidence(1),
                            },
                            {
                                "dayNumber": 2,
                                "role": optional_day_role,
                                "targetRouteAnchors": anchor_count_by_day[2],
                                "densityEvidence": density_evidence(2),
                            },
                        ],
                        "optionalExperiences": [
                            {"family": optional_family, "description": raw_need}
                            for optional_family, raw_need, _family_hints in optional_families
                        ],
                    },
                    "daySlots": slots,
                    "intentPools": pools,
                }
            )
        return json.dumps(
            {"schemaVersion": "initial-creative-portfolio-v1", "proposals": proposals},
            ensure_ascii=False,
        )

    @staticmethod
    def _react_multiturn_initial_plan(
        city: str,
        dates: list[str],
        *,
        campus_hints: list[str],
        campus_hint_source: str,
    ) -> str:
        first_date = dates[0] if dates else "2026-10-01"
        second_date = dates[1] if len(dates) > 1 else "2026-10-02"
        campus_name = next((str(item).strip() for item in campus_hints if str(item).strip()), f"{city}高等院校")
        day_slots = [
            {
                "slotId": "day1_campus",
                "dayNumber": 1,
                "date": first_date,
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": campus_name,
                "routeAnchor": True,
                "priority": 95,
                "notes": "requiredGrounding=true",
            },
            {
                "slotId": "day1_museum",
                "dayNumber": 1,
                "date": first_date,
                "timeWindow": "16:15-18:45",
                "startTime": "16:15",
                "durationMinutes": 150,
                "kind": "museum",
                "rawNeed": "美术馆参观",
                "routeAnchor": True,
                "priority": 92,
                "notes": "requiredGrounding=true",
            },
            {
                "slotId": "day1_lunch",
                "dayNumber": 1,
                "date": first_date,
                "timeWindow": "12:15-13:30",
                "startTime": "12:15",
                "durationMinutes": 75,
                "kind": "meal",
                "rawNeed": "午餐 当地特色美食",
                "routeAnchor": True,
                "priority": 70,
                "notes": "runtime fixture local meal slot",
            },
            {
                "slotId": "day2_free_time",
                "dayNumber": 2,
                "date": second_date,
                "timeWindow": "10:00-12:00",
                "startTime": "10:00",
                "durationMinutes": 120,
                "kind": "rest",
                "rawNeed": "自由活动",
                "routeAnchor": False,
                "priority": 40,
                "notes": "optional flexible time",
            },
        ]
        pools = [
            {
                "poolId": "campus_visit_pool",
                "rawNeed": campus_name,
                "city": city,
                "intentType": "campus_visit",
                "targetCount": 1,
                "preferredTypes": ["大学", "高等院校", "校区"],
                "rejectedTypes": ["培训机构", "中学", "小学"],
                "routePreference": {
                    "sameDayUnique": True,
                    "candidateHintSource": campus_hint_source,
                },
                "assignToSlots": ["day1_campus"],
                "candidateHints": campus_hints[:2] or [campus_name],
                "hintPolicy": "llm_common_knowledge_hint",
            },
            {
                "poolId": "museum_pool",
                "rawNeed": "美术馆参观",
                "city": city,
                "intentType": "museum",
                "targetCount": 1,
                "preferredTypes": ["美术馆", "艺术博物馆"],
                "rejectedTypes": ["商场", "酒店"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day1_museum"],
                "candidateHints": ["中国美术馆", "清华大学艺术博物馆"],
                "hintPolicy": "llm_common_knowledge_hint",
            },
            {
                "poolId": "meal_pool",
                "rawNeed": "午餐 当地特色美食",
                "city": city,
                "intentType": "meal",
                "targetCount": 1,
                "preferredTypes": ["餐饮服务", "中餐厅"],
                "rejectedTypes": ["酒店", "便利店"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day1_lunch"],
                "candidateHints": ["当地特色美食", "地方风味餐厅"],
                "hintPolicy": "llm_common_knowledge_hint",
            },
        ]
        return json.dumps(
            {
                "reply": "Recorded ReAct eval DaySlot/IntentPool fixture.",
                "mode": "day_slots",
                "daySlots": day_slots,
                "intentPools": pools,
                "warnings": ["RUNTIME_RECORDED_REACT_MULTITURN_FIXTURE"],
            },
            ensure_ascii=False,
        )

    def _campus_hints(
        self,
        city: str,
    ) -> list[str]:
        return [
            f"{city}高等院校",
            f"{city}大学校园",
            f"{city}学院校园",
            f"{city}高校园区",
        ]

    def _qualified_campus_hints(
        self,
        city: str,
        *,
        qualification_constraint: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        qualification = qualification_constraint or {}
        scheme = str(qualification.get("qualificationScheme") or "").strip()
        value = str(qualification.get("qualificationValue") or "").strip()
        if scheme and value:
            qualified = EntityQualificationEvidenceService.canonical_hints(
                locality=city,
                scheme=scheme,
                value=value,
            )
            if qualified:
                return qualified
        return []

    def _night_hints(self, city: str) -> list[str]:
        return [
            f"{city}公共城市夜景空间",
            f"{city}滨水夜间公共空间",
            f"{city}灯光历史街区",
            f"{city}公共天际线视野",
        ]


class RuntimeMockAgentProvider(RuntimeStagedMapMockProvider):
    """Offline provider that follows the same Controller V2 and staged-executor contract."""

    def run_tool_loop(
        self, context: dict, tool_registry, max_tool_rounds: int = 5, max_tool_calls_per_round: int = 3
    ) -> AgentToolLoopResult:
        read_result = tool_registry.execute("mock_read_itinerary", "read_itinerary", {})
        city = str(context.get("selectedCity") or "北京")
        base_version_id = context.get("activeVersionId")
        if read_result.get("itinerary") and isinstance(read_result["itinerary"], dict):
            base_version_id = read_result["itinerary"].get("activeVersionId") or base_version_id
        patch_result = tool_registry.execute(
            "mock_patch_itinerary",
            "patch_itinerary",
            {
                "baseVersionId": base_version_id,
                "operations": [
                    {
                        "op": "replace_itinerary",
                        "fullItinerary": self._mock_full_itinerary(city),
                    }
                ],
            },
        )
        reply = (
            "已用 runtime mock provider 写入一版可编辑草案。"
            "其中 POI 是低置信 agent-text-timeline 草稿，后续仍需要通过高德候选核对。"
        )
        if not patch_result.get("ok"):
            reply = f"runtime mock provider 写入失败：{patch_result.get('error') or 'unknown error'}"
        return AgentToolLoopResult(reply=reply, tool_events=tool_registry.events)

    def _mock_full_itinerary(self, city: str) -> dict:
        return {
            "title": f"{city} AI runtime mock 1 日草案",
            "city": city,
            "templateType": "agent_mvp",
            "budgetEstimate": 120.0,
            "budgetDeltaExplanation": "runtime mock dry-run 估算，真实费用待核对。",
            "decisionRationale": "用于验证无前端 Agent runtime 写入链路；POI 不伪装成已高德核验。",
            "status": "draft",
            "days": [
                {
                    "dayNumber": 1,
                    "title": "轻松城市体验",
                    "weatherSummary": "天气待查询。",
                    "riskSummary": "开放、预约和拥挤风险待核对。",
                    "totalEstimatedCost": 120.0,
                    "segments": [
                        self._mock_segment(city, "09:30", "11:00", "visit", "城市代表性景点", 60.0),
                        self._mock_segment(city, "11:30", "12:30", "meal", "午餐", 60.0, category="food"),
                        self._mock_segment(city, "14:00", "15:30", "visit", "轻松散步街区", 0.0),
                    ],
                }
            ],
            "routeOptions": [],
            "weatherSignals": [],
            "trafficCrowdingSignals": [],
            "ticketLookupResults": [],
        }

    def _mock_segment(
        self,
        city: str,
        start_time: str,
        end_time: str,
        kind: str,
        poi_name: str,
        estimated_cost: float,
        *,
        category: str = "scenic",
    ) -> dict:
        return {
            "startTime": start_time,
            "endTime": end_time,
            "kind": kind,
            "poi": {
                "id": f"agent-text-{uuid4().hex[:8]}-{poi_name}",
                "name": poi_name,
                "type": category,
                "city": city,
                "district": "",
                "address": "",
                "longitude": None,
                "latitude": None,
                "category": category,
                "source": "agent-text-timeline",
                "sourceNote": "高德 POI 待校验",
                "confidence": 0.4,
                "photos": [],
            },
            "transportMode": "walk",
            "estimatedCost": estimated_cost,
            "notes": "runtime mock dry-run 草稿；地点、开放时间和费用需后续核对。",
        }

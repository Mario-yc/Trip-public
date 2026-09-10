from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from backend.tests.intent_contract_support import IntentContractProviderMixin
from src.core.config import get_settings
from src.services.agent_autonomy_service import (
    AgentAutonomyController,
    AgentDecision,
    AgentDecisionResult,
    GatedAgentDecision,
)
from src.api.schemas.agent import AgentMessageRequest
from src.services.agent_executor_registry import (
    AgentActionExecutorRegistry,
    AgentActionOutcome,
    DraftItineraryExecutor,
    TimelineMutationExecutor,
)
from src.services.agent_observation_service import AgentObservationBuilder
from src.services.agent_service import AgentService
from src.services.agent_turn_coordinator import AgentTurnCoordinator
from src.services.conversation_service import ConversationService
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.provider_route_insertion_service import ProviderRouteInsertionService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import AMAP_ROUTE_SOURCE
from src.services.timeline_mutation_intent_service import TimelineMutationIntentExtractor
from src.services.timeline_mutation_models import (
    TimelineMutationIntent,
    TimelineMutationReplacement,
    TimelineMutationSelector,
)
from src.services.timeline_mutation_transaction_service import TimelineMutationTransactionService

from timeline_mutation_test_support import open_db, seed_timeline, server_route_decision_contract


def _confirmed_route_decision_contract() -> dict:
    base = server_route_decision_contract()
    portable = RouteInsertionScorer.build_route_decision_contract(
        source="persisted_active_version",
        provenance={
            **base["provenance"],
            "confirmed": True,
            "confirmationSource": "explicit_fixture_user_preference",
        },
        detour_tolerance=base["detourTolerance"],
        mobility_profile=base["mobilityProfile"],
    )
    assert portable is not None
    return {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        **portable,
    }


def _install_recorded_provider_route_matrix(monkeypatch) -> None:
    def recorded_leg(
        _self,
        *,
        plan_id: str,
        left: dict,
        right: dict,
        transport_mode: str,
    ) -> dict:
        del plan_id
        return {
            "fromSegmentId": str(left["segmentId"]),
            "toSegmentId": str(right["segmentId"]),
            "fromAmapId": str(left["amapId"]),
            "toAmapId": str(right["amapId"]),
            "provider": AMAP_ROUTE_SOURCE,
            "source": AMAP_ROUTE_SOURCE,
            "mode": transport_mode,
            "distanceMeters": 600,
            "durationSeconds": 300,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
            "walkingDistanceMeters": 600 if transport_mode == "walking" else 120,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "polyline": [],
            "steps": [],
            "providerPayload": {"fixture": "recorded_provider_route_matrix"},
            "costAmount": 0,
            "costCurrency": "CNY",
        }

    monkeypatch.setattr(ProviderRouteInsertionService, "_provider_leg", recorded_leg)


def _patch_decision_result() -> AgentDecisionResult:
    mutation_intent = {
        "schemaVersion": "timeline-mutation-intent-v1",
        "operation": "set_start_time",
        "selector": {"dayNumber": 1, "intentType": "museum", "currentText": "美术馆"},
        "replacement": {"startTime": "15:30"},
        "preserve": ["target_duration", "other_days", "user_locked_times"],
        "confidence": 0.99,
        "source": "model_semantic_extractor",
        "sourceText": "把第一天美术馆改到 15:30",
    }
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "patch_itinerary",
            "confidence": 0.99,
            "decisionSummary": "调整唯一匹配的美术馆开始时间",
            "requiredTools": ["patch_itinerary"],
            "actionDirective": {
                "type": "patch_itinerary",
                "requestedOutcome": "第一天美术馆 15:30 开始并保持时长",
                "mutationIntent": mutation_intent,
                "preserve": mutation_intent["preserve"],
                "maxChangedSegmentCount": 1,
            },
            "targetScope": {"mutationIntent": mutation_intent},
            "stopCondition": {"type": "verified_patch_then_reobserve"},
        }
    )
    gated = GatedAgentDecision.model_validate(
        {
            "decision": decision,
            "effectiveWriteRisk": "medium",
            "effectiveTools": ["patch_itinerary"],
            "policyReasonCodes": [],
            "accepted": True,
        }
    )
    return AgentDecisionResult(
        decision=decision,
        gated_decision=gated,
        source="controller",
        controller_error=None,
        planner_plan={},
        controller_full_called=True,
        controller_full_succeeded=True,
        decision_path="full",
    )


def _draft_decision_result() -> AgentDecisionResult:
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "draft_itinerary",
            "confidence": 0.99,
            "decisionSummary": "create itinerary",
            "requiredTools": ["resolve_poi", "patch_itinerary"],
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_campus_visit"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "campus",
                        "requiredGoalIds": ["goal_campus_visit"],
                        "requiredGoalCounts": {"goal_campus_visit": 1},
                        "optionalGoalIds": [],
                    }
                ],
            },
            "stopCondition": {"type": "verified_write_then_reobserve"},
        }
    )
    gated = GatedAgentDecision.model_validate(
        {
            "decision": decision,
            "effectiveWriteRisk": "high",
            "effectiveTools": ["resolve_poi", "patch_itinerary"],
            "policyReasonCodes": [],
            "accepted": True,
        }
    )
    return AgentDecisionResult(
        decision=decision,
        gated_decision=gated,
        source="controller",
        controller_error=None,
        planner_plan={},
        controller_full_called=True,
        controller_full_succeeded=True,
        decision_path="full",
    )


def _observation_context() -> dict:
    return {
        "latestUserMessage": "把第一天美术馆改到 15:30",
        "activeVersionId": "ver_1",
        "currentItinerarySnapshot": {
            "id": "plan_1",
            "versionId": "ver_1",
            "days": [
                {
                    "id": "day_1",
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_museum",
                            "startTime": "16:15",
                            "endTime": "17:45",
                            "kind": "visit",
                            "poi": {"name": "中国美术馆", "intentType": "museum"},
                            "semanticMetadata": {"intentType": "museum", "aliases": ["美术馆"]},
                        }
                    ],
                }
            ],
        },
    }


def test_verified_partial_timeline_is_terminal_for_same_control_loop() -> None:
    decide_calls: list[int] = []
    reloads: list[int] = []

    def execute(_request):
        return AgentActionOutcome(
            action="draft_itinerary",
            execution_route="staged_initial_pipeline",
            status="partial",
            result_version_id="ver_partial",
            observed_active_version_id="ver_partial",
            patch_ids=["patch_partial"],
            candidate_summary={"terminalStatus": "needs_confirmation", "pendingSlotCount": 1},
            verifier={"passed": True, "pendingSlotTruthValid": True},
            control_loop_disposition="terminal_needs_confirmation",
            safe_for_future_user_continuation=True,
            safe_to_continue=True,
        )

    coordinator = AgentTurnCoordinator(AgentActionExecutorRegistry([DraftItineraryExecutor(execute)]))
    fixed = _observation_context()
    result = coordinator.run(
        copy.deepcopy(fixed),
        observe=lambda _context, cycle, _outcome: AgentObservationBuilder().build(fixed, cycle_index=cycle),
        decide=lambda _context, _observation, cycle: decide_calls.append(cycle) or _draft_decision_result(),
        reload_context=lambda context, _outcome, cycle: reloads.append(cycle) or context,
        max_cycles=3,
    )

    assert decide_calls == [0]
    assert reloads == [0]
    assert result.stop_reason == "partial_timeline_needs_confirmation"
    assert len(result.cycles) == 1
    assert result.outcomes[0].control_loop_disposition == "terminal_needs_confirmation"
    assert result.outcomes[0].safe_for_future_user_continuation is True


def test_partial_without_verified_persisted_truth_is_not_terminal_success() -> None:
    decide_calls: list[int] = []

    def execute(_request):
        return AgentActionOutcome(
            action="draft_itinerary",
            execution_route="staged_initial_pipeline",
            status="partial",
            result_version_id=None,
            candidate_summary={"terminalStatus": "needs_confirmation"},
            verifier={"passed": False},
            control_loop_disposition="terminal_failure",
            safe_for_future_user_continuation=False,
            safe_to_continue=False,
        )

    coordinator = AgentTurnCoordinator(AgentActionExecutorRegistry([DraftItineraryExecutor(execute)]))
    fixed = _observation_context()
    result = coordinator.run(
        copy.deepcopy(fixed),
        observe=lambda _context, cycle, _outcome: AgentObservationBuilder().build(fixed, cycle_index=cycle),
        decide=lambda _context, _observation, cycle: decide_calls.append(cycle) or _draft_decision_result(),
        reload_context=lambda context, _outcome, _cycle: context,
        max_cycles=3,
    )

    assert decide_calls == [0]
    assert result.stop_reason == "partial"
    assert result.stop_reason != "partial_timeline_needs_confirmation"


def test_persisted_planning_checkpoint_stops_before_a_second_controller_decision() -> None:
    decide_calls: list[int] = []
    reloads: list[int] = []

    def execute(_request):
        return AgentActionOutcome(
            action="draft_itinerary",
            execution_route="staged_initial_pipeline",
            status="partial",
            result_version_id=None,
            candidate_summary={"terminalStatus": "planning_pending"},
            verifier={"passed": None, "notApplicable": True},
            goal_delta={"planningAttemptPersisted": True},
            safe_to_continue=True,
        )

    coordinator = AgentTurnCoordinator(AgentActionExecutorRegistry([DraftItineraryExecutor(execute)]))
    fixed = _observation_context()
    result = coordinator.run(
        copy.deepcopy(fixed),
        observe=lambda _context, cycle, _outcome: AgentObservationBuilder().build(fixed, cycle_index=cycle),
        decide=lambda _context, _observation, cycle: decide_calls.append(cycle) or _draft_decision_result(),
        reload_context=lambda context, _outcome, cycle: reloads.append(cycle) or context,
        max_cycles=3,
    )

    assert decide_calls == [0]
    assert reloads == [0]
    assert result.stop_reason == "needs_confirmation"
    assert len(result.cycles) == 1


def test_repeated_state_and_action_signature_execute_only_once() -> None:
    calls: list[int] = []

    def execute(request):
        calls.append(request.cycle_index)
        return AgentActionOutcome(
            action="patch_itinerary",
            execution_route="timeline_mutation_executor",
            status="success",
            base_version_id="ver_1",
            result_version_id="ver_2",
            patch_ids=["patch_1"],
            changed_segment_ids=["seg_museum"],
            verifier={"passed": True, "postconditionPassed": True},
            safe_to_continue=True,
        )

    coordinator = AgentTurnCoordinator(AgentActionExecutorRegistry([TimelineMutationExecutor(execute)]))
    fixed = _observation_context()
    result = coordinator.run(
        copy.deepcopy(fixed),
        observe=lambda _context, cycle, _outcome: AgentObservationBuilder().build(fixed, cycle_index=cycle),
        decide=lambda _context, _observation, _cycle: _patch_decision_result(),
        reload_context=lambda context, _outcome, _cycle: context,
        max_cycles=3,
    )

    assert calls == [0]
    assert result.stop_reason == "no_progress_repeated_state_action"
    assert len(result.outcomes) == 1
    assert len(result.cycles) == 2
    assert result.cycles[1].outcome is None


def test_failed_executor_outcome_stops_after_reload_without_false_finish() -> None:
    reloads: list[int] = []

    def execute(_request):
        return AgentActionOutcome(
            action="patch_itinerary",
            execution_route="timeline_mutation_executor",
            status="failed",
            base_version_id="ver_1",
            candidate_summary={"failureReason": "transaction_evidence_missing"},
            verifier={"passed": False},
            safe_to_continue=False,
            terminal_payload={"assistantReply": "已修改成功"},
        )

    coordinator = AgentTurnCoordinator(AgentActionExecutorRegistry([TimelineMutationExecutor(execute)]))
    fixed = _observation_context()
    result = coordinator.run(
        copy.deepcopy(fixed),
        observe=lambda _context, cycle, _outcome: AgentObservationBuilder().build(fixed, cycle_index=cycle),
        decide=lambda _context, _observation, _cycle: _patch_decision_result(),
        reload_context=lambda context, _outcome, cycle: reloads.append(cycle) or context,
        max_cycles=3,
    )

    assert result.stop_reason == "failed"
    assert reloads == [0]
    assert len(result.outcomes) == 1
    assert result.outcomes[0].result_version_id is None
    assert result.outcomes[0].patch_ids == []
    assert result.cycles[0].outcome["status"] == "failed"
    assert "assistantReply" not in result.cycles[0].outcome


@pytest.mark.parametrize(
    ("duration_minutes", "duration_user_locked"),
    [(90, False), (150, False), (150, True)],
)
def test_set_start_time_preserves_90_150_and_user_locked_duration(
    monkeypatch,
    duration_minutes: int,
    duration_user_locked: bool,
) -> None:
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    _install_recorded_provider_route_matrix(monkeypatch)
    with open_db() as connection:
        session, base_version, snapshot = seed_timeline(connection, grounded_museum=True)
        prepared = copy.deepcopy(snapshot)
        target = next(
            segment for day in prepared["days"] for segment in day["segments"] if segment["id"] == "seg_831b98d1cb6e"
        )
        target["startTime"] = "10:00"
        target["endTime"] = f"{10 + duration_minutes // 60:02d}:{duration_minutes % 60:02d}"
        target.setdefault("estimateMetadata", {}).setdefault("duration", {})["userLocked"] = duration_user_locked
        snapshot_service = ItinerarySnapshotService(connection)
        snapshot_service.apply_snapshot(session.active_plan_id, prepared)
        prepared = snapshot_service.capture_snapshot(session.active_plan_id)
        prepared["routeDecisionContract"] = server_route_decision_contract()
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(prepared, ensure_ascii=False, default=str), base_version.id),
        )
        connection.commit()

        intent = TimelineMutationIntentExtractor().extract(
            "把第一天美术馆改到 15:30",
            has_active_timeline=True,
        )
        assert intent is not None
        outcome = TimelineMutationTransactionService(connection).execute(session.session_id, intent)
        after = snapshot_service.capture_snapshot(session.active_plan_id)
        changed = next(
            segment for day in after["days"] for segment in day["segments"] if segment["id"] == "seg_831b98d1cb6e"
        )

    expected_end_minutes = 15 * 60 + 30 + duration_minutes
    assert outcome.status == "success"
    assert changed["startTime"] == "15:30"
    assert changed["endTime"] == f"{expected_end_minutes // 60:02d}:{expected_end_minutes % 60:02d}"
    if duration_user_locked:
        assert changed["estimateMetadata"]["duration"]["userLocked"] is True


def test_model_resolved_pronoun_uses_last_unique_target_and_current_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    _install_recorded_provider_route_matrix(monkeypatch)
    with open_db() as connection:
        session, _, _ = seed_timeline(connection, grounded_museum=True)
        service = TimelineMutationTransactionService(connection)
        first = TimelineMutationIntentExtractor().extract(
            "把第一天美术馆改到 15:30",
            has_active_timeline=True,
        )
        assert first is not None
        first_outcome = service.execute(session.session_id, first)
        first_version = first_outcome.result_version_id

        # The model has resolved “再提前半小时” from lastOutcome + the reloaded
        # snapshot. The executor receives only the typed semantic directive.
        pronoun_intent = TimelineMutationIntent(
            operation="set_start_time",
            selector=TimelineMutationSelector(
                dayNumber=1,
                intentType="museum",
                currentText="美术馆",
            ),
            replacement=TimelineMutationReplacement(startTime="15:00"),
            preserve=["target_duration", "other_days", "user_locked_times"],
            confidence=0.99,
            source="model_semantic_extractor",
            sourceText="再提前半小时",
        )
        second_outcome = service.execute(session.session_id, pronoun_intent)
        after = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        changed = next(
            segment for day in after["days"] for segment in day["segments"] if segment["id"] == "seg_831b98d1cb6e"
        )

    assert first_outcome.status == "success"
    assert second_outcome.status == "success"
    assert second_outcome.base_version_id == first_version
    assert changed["startTime"] == "15:00"
    assert changed["endTime"] == "16:30"


def test_same_session_pronoun_turn_reobserves_current_segment_before_second_patch(monkeypatch) -> None:
    class PronounController(IntentContractProviderMixin):
        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def _patch(start_time: str, source_text: str) -> dict:
            intent = {
                "schemaVersion": "timeline-mutation-intent-v1",
                "operation": "set_start_time",
                "selector": {"dayNumber": 1, "intentType": "museum", "currentText": "美术馆"},
                "replacement": {"startTime": start_time},
                "preserve": ["target_duration", "other_days", "user_locked_times"],
                "confidence": 0.99,
                "source": "model_semantic_extractor",
                "sourceText": source_text,
            }
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "patch_itinerary",
                "actionDirective": {
                    "type": "patch_itinerary",
                    "requestedOutcome": f"美术馆改到 {start_time} 并保持原时长",
                    "mutationIntent": intent,
                    "preserve": intent["preserve"],
                    "maxChangedSegmentCount": 1,
                },
            }

        def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
            self.calls += 1
            observation = context.get("observation") or {}
            if observation.get("latestMessage") == "再提前半小时":
                refs = [
                    item
                    for item in observation.get("segmentRefs") or []
                    if item.get("dayNumber") == 1 and item.get("intentType") == "museum"
                ]
                assert len(refs) == 1 and refs[0]["startTime"] == "15:30"
                assert any(
                    item.get("content") == "把第一天美术馆改到 15:30" for item in observation.get("recentTurns") or []
                )
                return self._patch("15:00", "再提前半小时")
            return self._patch("15:30", "把第一天美术馆改到 15:30")

    monkeypatch.setattr(ItineraryService, "refresh_routes", lambda *_args, **_kwargs: [])
    _install_recorded_provider_route_matrix(monkeypatch)
    provider = PronounController()
    with open_db() as connection:
        session, base_version, snapshot = seed_timeline(connection, grounded_museum=True)
        snapshot["routeDecisionContract"] = _confirmed_route_decision_contract()
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, default=str), base_version.id),
        )
        connection.commit()
        service = AgentService(
            connection,
            provider=provider,
            timeline_mutation_service=TimelineMutationTransactionService(connection),
        )
        first = service.send_message(
            session.session_id,
            AgentMessageRequest(content="把第一天美术馆改到 15:30"),
        )
        second = service.send_message(
            session.session_id,
            AgentMessageRequest(content="再提前半小时"),
        )
        after = ItinerarySnapshotService(connection).capture_snapshot(session.active_plan_id)
        changed = next(
            segment for day in after["days"] for segment in day["segments"] if segment["id"] == "seg_831b98d1cb6e"
        )

    assert first.terminal_status == "success"
    assert second.terminal_status == "success"
    assert provider.calls == 2
    assert changed["startTime"] == "15:00"
    assert changed["endTime"] == "16:30"
    assert [event.metadata["primaryAction"] for event in second.planning_steps if event.type == "agent_decision"] == [
        "patch_itinerary",
    ]


def test_local_edit_rejects_controller_route_question_that_host_context_does_not_need(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_INITIAL_PLANNING_MODE", "simple_open_v1")
    get_settings.cache_clear()

    class EmptyContextController(IntentContractProviderMixin):
        def __init__(self) -> None:
            self.calls = 0

        def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
            self.calls += 1
            dimensions = {
                str(item.get("dimensionId") or "")
                for item in context.get("clarificationDimensions") or []
                if isinstance(item, dict)
            }
            assert "route_decision.mobility_profile" in dimensions
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": {
                    "type": "ask_user",
                    "questions": [
                        {
                            "dimensionId": "route_decision.mobility_profile",
                            "question": "这次希望采用怎样的主要交通节奏？",
                            "whyItMatters": "交通方式会改变真实路线矩阵和可行排期。",
                            "allowFreeText": True,
                            "options": [
                                {
                                    "id": "mobility_transit_standard",
                                    "label": "公共交通、标准节奏",
                                },
                                {
                                    "id": "mobility_transit_relaxed",
                                    "label": "公共交通、少走慢行",
                                },
                                {
                                    "id": "mobility_driving_relaxed",
                                    "label": "驾车为主、少走慢行",
                                },
                            ],
                        }
                    ],
                },
            }

    provider = EmptyContextController()
    with open_db() as connection:
        seed_timeline(connection)
        session = ConversationService(connection).create_session("北京", "空上下文对抗测试")
        before_versions = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        before_patches = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        response = AgentService(connection, provider=provider).send_message(
            session.session_id,
            AgentMessageRequest(content="把第一天美术馆改到 15:30"),
        )
        after_versions = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        after_patches = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]

    checkpoint = response.assistant_turn.clarification_checkpoint
    assert provider.calls == 1
    assert response.terminal_status == "needs_confirmation"
    assert checkpoint is None
    assert "规划控制器本轮未形成可执行规划" in response.assistant_turn.content
    assert "澄清问题未通过" not in response.assistant_turn.content
    assert response.version is None
    assert after_versions == before_versions == 0
    assert after_patches == before_patches == 0
    assert "已修改" not in response.assistant_turn.content


def test_existing_itinerary_controller_unavailable_is_fail_closed_without_planner_write() -> None:
    class UnavailableController:
        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            raise RuntimeError("controller_provider_unavailable")

    class ForbiddenPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def plan(self, _latest_message, _request_context):
            self.calls += 1
            raise AssertionError("existing-itinerary write must not fall back to planner")

    planner = ForbiddenPlanner()
    context = _observation_context()
    observation = AgentObservationBuilder().build(context)
    result = AgentAutonomyController(
        provider=UnavailableController(),
        planner_service=planner,
        decision_timeout_seconds=0.05,
        lite_timeout_seconds=0.05,
        total_budget_seconds=0.1,
    ).decide(
        context["latestUserMessage"],
        context,
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        observation=observation,
    )

    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.effective_write_risk == "none"
    assert result.planner_called is False
    assert planner.calls == 0

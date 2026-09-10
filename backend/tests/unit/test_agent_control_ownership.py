from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend.tests.intent_contract_support import IntentContractProviderMixin
from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.agent_autonomy_service import AgentAutonomyController, AgentDecision, AgentDecisionPolicyGate
from src.services.agent_observation_service import AgentObservationBuilder
from src.services.agent_service import AgentService
from src.services.agent_stop_policy import AgentStopPolicy
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.conversation_service import ConversationService
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.timeline_command_planner import TimelineCommandPlanner


def _hard_and_soft_goal_observation():
    return AgentObservationBuilder().build(
        {
            "resolvedTripDates": {
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
            },
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_campus_visit",
                        "intentType": "campus_visit",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    },
                    {
                        "goalId": "goal_museum",
                        "intentType": "museum",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    },
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "requiredMin": 1,
                        "requirementLevel": "soft_experience",
                    },
                ]
            },
        }
    )


def _hard_and_soft_draft_payload(*, include_museum: bool = True) -> dict[str, Any]:
    required_ids = ["goal_campus_visit", *(["goal_museum"] if include_museum else [])]
    occurrence_schedule_hints = [
        {
            "goalId": "goal_campus_visit",
            "dayNumber": 1,
            "dayPart": "morning",
            "sequence": 1,
            "preferredStartTime": "09:00",
            "durationEstimate": {"min": 90, "preferred": 120, "max": 150},
            "estimateSource": "controller_estimate",
            "confidence": 0.95,
        },
        {
            "goalId": "goal_meal",
            "dayNumber": 1,
            "dayPart": "noon",
            "sequence": 2,
            "preferredStartTime": "12:00",
            "durationEstimate": {"min": 45, "preferred": 60, "max": 90},
            "estimateSource": "controller_estimate",
            "confidence": 0.95,
        },
    ]
    if include_museum:
        occurrence_schedule_hints.append(
            {
                "goalId": "goal_museum",
                "dayNumber": 1,
                "dayPart": "afternoon",
                "sequence": 3,
                "preferredStartTime": "14:00",
                "durationEstimate": {"min": 90, "preferred": 120, "max": 150},
                "estimateSource": "controller_estimate",
                "confidence": 0.95,
            }
        )
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus_visit", "goal_museum", "goal_meal"],
            "optionalExperienceBudget": 1,
            "occurrenceScheduleHints": occurrence_schedule_hints,
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校与博物馆",
                    "requiredGoalIds": required_ids,
                    "requiredGoalCounts": {goal_id: 1 for goal_id in required_ids},
                    "optionalGoalIds": ["goal_meal"],
                }
            ],
        },
    }


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM amap_poi_candidates;
            DELETE FROM planning_runs;
            DELETE FROM travel_preference_memories;
            DELETE FROM session_preference_memories;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM route_options;
            DELETE FROM ticket_lookup_results;
            DELETE FROM poi_risk_alerts;
            DELETE FROM weather_signals;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            """
        )


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


class _CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, latest_message: str, request_context: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {
            "taskRoute": "read_only",
            "requiresPatch": False,
            "readOnly": True,
            "allowedTools": ["read_itinerary"],
        }


class _HealthyReadController:
    def decide_autonomy(
        self, context: dict[str, Any], *, timeout_seconds: float, repair_feedback: str = ""
    ) -> dict[str, Any]:
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "read_itinerary",
            "actionDirective": {
                "type": "read_itinerary",
                "queryType": "timeline_summary",
                "targetText": "当前安排",
                "includeDay": True,
                "includeTime": True,
                "includeGroundingStatus": True,
            },
        }


class _HealthyAskUserController(IntentContractProviderMixin):
    def decide_autonomy(
        self, context: dict[str, Any], *, timeout_seconds: float, repair_feedback: str = ""
    ) -> dict[str, Any]:
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "type": "ask_user",
                "question": "这次行程希望采用哪种主要交通方式和节奏？",
                "dimensionId": "route_decision.mobility_profile",
                "whyItMatters": "交通方式与节奏决定路线耗时、步行和换乘成本。",
                "allowFreeText": False,
                "options": [
                    {
                        "id": "transit_standard",
                        "label": "公共交通 · 常规节奏",
                        "semanticValue": {
                            "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"}
                        },
                    },
                    {
                        "id": "transit_relaxed",
                        "label": "公共交通 · 少走慢行",
                        "semanticValue": {
                            "mobilityProfile": {"transportMode": "transit", "paceClass": "relaxed"}
                        },
                    },
                ],
            },
        }


class _InvalidDailyCardinalityController:
    def __init__(self) -> None:
        self.calls = 0

    def decide_autonomy(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
        repair_feedback: str = "",
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_night_view"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "夜景",
                        "requiredGoalIds": ["goal_night_view"],
                        "requiredGoalCounts": {"goal_night_view": 2},
                    }
                ],
            },
        }


class _SoftGoalMisclassifiedController:
    def __init__(self) -> None:
        self.calls = 0
        self.repair_feedback: list[dict[str, Any]] = []

    def decide_autonomy(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
        repair_feedback: str = "",
    ) -> dict[str, Any]:
        self.calls += 1
        if repair_feedback:
            self.repair_feedback.append(json.loads(repair_feedback))
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_campus_visit", "goal_night_view", "goal_meal"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "高校与夜景",
                        "requiredGoalIds": ["goal_campus_visit", "goal_night_view", "goal_meal"],
                        "requiredGoalCounts": {
                            "goal_campus_visit": 1,
                            "goal_night_view": 1,
                            "goal_meal": 1,
                        },
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "theme": "高校",
                        "requiredGoalIds": ["goal_campus_visit", "goal_meal"],
                        "requiredGoalCounts": {"goal_campus_visit": 1, "goal_meal": 1},
                        "optionalGoalIds": [],
                    },
                ],
            },
        }


def test_healthy_controller_does_not_call_planner() -> None:
    planner = _CountingPlanner()

    result = AgentAutonomyController(provider=_HealthyReadController(), planner_service=planner).decide(
        "当前安排是什么",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    assert result.source == "controller"
    assert planner.calls == 0
    assert result.to_event_metadata()["plannerCalled"] is False
    assert result.to_event_metadata()["plannerFallbackUsed"] is False


def test_invalid_daily_cardinality_uses_authoritative_fallback_without_model_repair() -> None:
    provider = _InvalidDailyCardinalityController()
    observation = AgentObservationBuilder().build(
        {
            "resolvedTripDates": {
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
            },
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "preferredCount": 1,
                        "maxCount": 1,
                        "cardinalitySource": "explicit_user_request",
                        "distributionPolicy": "spread_across_distinct_days",
                        "allowedDayNumbers": [1, 2],
                        "requirementLevel": "required",
                    }
                ]
            },
        }
    )
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "observation": observation.model_dump(by_alias=True),
    }

    result = AgentAutonomyController(provider=provider).decide(
        "北京两日游，晚上看夜景",
        {},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert provider.calls == 1
    assert result.schema_repair_attempts == 0
    assert result.source == "safe_fallback"
    assert result.decision_path == "fallback"
    assert result.planner_called is False
    assert result.controller_full_called is True
    assert result.controller_full_succeeded is False
    assert result.controller_lite_called is False
    assert result.gated_decision.accepted is True
    assert result.decision.primary_action == "draft_itinerary"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "server_directive_after_authoritative_contract_rejection" in result.decision.reason_codes
    day_counts = [
        count for day in result.decision.action_directive.day_strategies for count in day.required_goal_counts.values()
    ]
    assert day_counts == [1]


def test_soft_goal_in_required_bucket_uses_authoritative_fallback_without_model_repair() -> None:
    provider = _SoftGoalMisclassifiedController()
    request_contract = {
        "requiredIntents": [
            {
                "goalId": "goal_campus_visit",
                "intentType": "campus_visit",
                "requiredMin": 1,
                "preferredCount": 2,
                "maxCount": 2,
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "required",
            },
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requiredMin": 1,
                "preferredCount": 1,
                "maxCount": 1,
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "required",
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "soft_experience",
            },
        ],
    }
    observation = AgentObservationBuilder().build(
        {
            "resolvedTripDates": {"dates": ["2026-10-01", "2026-10-02"]},
            "requestIntentContract": request_contract,
        }
    )
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"]},
        "observation": observation.model_dump(by_alias=True),
    }

    result = AgentAutonomyController(provider=provider).decide(
        "今年国庆参观北京高校两日游，晚上看北京夜景。每天午餐想体验当地特色美食。",
        {},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert provider.calls == 1
    assert result.schema_repair_attempts == 0
    assert provider.repair_feedback == []
    assert result.source == "safe_fallback"
    assert result.decision_path == "fallback"
    assert result.planner_called is False
    assert result.controller_full_called is True
    assert result.controller_full_succeeded is False
    assert result.controller_lite_called is False
    assert result.gated_decision.accepted is True
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "server_directive_after_authoritative_contract_rejection" in result.decision.reason_codes
    strategies = result.decision.action_directive.day_strategies
    assert all("goal_meal" not in item.required_goal_ids for item in strategies)
    assert [item.day_number for item in strategies if "goal_meal" in item.optional_goal_ids] == [1, 2]
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "resolvedTripDates": {
                "dayCount": 2,
                "dates": ["2026-10-01", "2026-10-02"],
            },
            "requestIntentContract": request_contract,
        }
    )
    occurrence_plan = GoalOccurrenceCompiler().compile(
        ledger,
        result.decision.action_directive.model_dump(by_alias=True),
    )
    meal_occurrences = [item for item in occurrence_plan.occurrences if item.source_goal_id == "goal_meal"]
    assert [(item.day_number, item.requirement_level) for item in meal_occurrences] == [
        (1, "explicit_soft"),
        (2, "explicit_soft"),
    ]


def test_natural_language_controller_runs_before_legacy_arbitrator_and_timeline_parser(monkeypatch) -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        service = AgentService(connection, provider=_HealthyAskUserController())

        def fail_legacy_route(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("legacy semantic router ran before healthy controller")

        monkeypatch.setattr(service.autonomy_arbitrator, "decide", fail_legacy_route)
        monkeypatch.setattr(service.timeline_command_planner, "parse", fail_legacy_route)

        response = service.send_message(
            session.session_id,
            AgentMessageRequest(content="把行程改得更好一点"),
        )

    decision_step = next(step for step in response.planning_steps if step.type == "agent_decision")
    assert decision_step.metadata["controlOwner"] == "model_controller"
    assert decision_step.metadata["deterministicArbitratorCalled"] is False
    assert decision_step.metadata["timelineParserCalledBeforeDecision"] is False


def test_draft_decision_v2_requires_model_planning_directive() -> None:
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "draft_itinerary",
            "confidence": 0.94,
            "decisionSummary": "生成两日行程",
            "requiredCapabilities": ["poi_grounding", "route", "versioned_write"],
            "requiredTools": ["resolve_poi", "patch_itinerary"],
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["campus", "museum"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "高校与艺术",
                        "requiredGoalIds": ["campus", "museum"],
                        "optionalGoalIds": [],
                        "pace": "standard",
                        "maxRouteAnchors": 4,
                    }
                ],
                "optionalExperienceBudget": 0,
                "searchPriority": ["exact_entity", "required"],
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
            },
            "targetScope": {},
            "stopCondition": {"type": "verified_goal_state_or_material_choice"},
        }
    )

    dumped = decision.model_dump(by_alias=True)
    assert dumped["schemaVersion"] == "agent-decision-v2"
    assert dumped["actionDirective"]["type"] == "draft_itinerary"
    assert dumped["requiredCapabilities"] == ["poi_grounding", "route", "versioned_write"]


def test_ownership_telemetry_records_proposed_and_actual_executor_route() -> None:
    planner = _CountingPlanner()
    result = AgentAutonomyController(provider=_HealthyReadController(), planner_service=planner).decide(
        "当前安排是什么",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    metadata = result.to_event_metadata(actual_execution_route="read_only")

    assert metadata["controlOwner"] == "model_controller"
    assert metadata["controllerCalled"] is True
    assert metadata["controllerSucceeded"] is True
    assert metadata["proposedExecutionRoute"] == "read_only"
    assert metadata["actualExecutionRoute"] == "read_only"
    assert metadata["executionOverride"] is False
    assert metadata["actionDirectiveSource"] == "model"


def test_timeline_command_planner_compiles_patch_directive_without_raw_text() -> None:
    command = TimelineCommandPlanner().compile(
        {
            "type": "patch_itinerary",
            "operationIntent": "replace_segment_start_time",
            "baseVersionId": "ver_1",
            "targetSegmentIds": ["seg_1"],
            "requestedOutcome": "九点开始",
            "preserve": ["other_segments"],
            "maxChangedSegmentCount": 1,
            "startTime": "09:00",
        },
        {"stateFingerprint": "fingerprint_1"},
    )

    assert command is not None
    assert command.intent == "directive_patch"
    assert command.operation == "replace_segment_start_time"
    assert command.constraints["targetSegmentIds"] == ["seg_1"]


def test_policy_gate_rejects_candidate_identity_outside_safe_observation() -> None:
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [{"id": "day_1", "dayNumber": 1, "segments": [{"id": "seg_1", "title": "晚餐"}]}],
            },
            "pendingAmapPoiCandidates": [
                {
                    "id": "candidate_safe_group",
                    "sourceSegmentId": "seg_1",
                    "status": "pending",
                    "candidates": [{"id": "amap_safe", "name": "安全候选"}],
                }
            ],
        }
    )
    decision = AgentDecision(
        primaryAction="patch_itinerary",
        confidence=0.9,
        decisionSummary="尝试选择观察之外的候选",
        requiredTools=["patch_itinerary"],
        targetScope={
            "baseVersionId": "ver_1",
            "segmentIds": ["seg_1"],
            "operationScope": "replace_segment_poi_from_candidate",
            "candidateId": "candidate_invented",
            "amapPoiId": "amap_invented",
        },
        stopCondition={"type": "verified_patch"},
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "target_scope_not_in_observation" in gated.policy_reason_codes


def test_policy_gate_rejects_model_draft_that_omits_required_goal() -> None:
    observation = AgentObservationBuilder().build(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {"intentType": "campus_visit", "target": 1},
                    {"intentType": "museum", "target": 1},
                ]
            }
        }
    )
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "draft_itinerary",
            "confidence": 0.94,
            "decisionSummary": "只安排高校，漏掉博物馆",
            "requiredTools": ["resolve_poi", "patch_itinerary"],
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["campus_visit"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "高校",
                        "requiredGoalIds": ["campus_visit"],
                        "optionalGoalIds": [],
                    }
                ],
            },
            "targetScope": {},
            "stopCondition": {"type": "verified_goal_state"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "required_goal_omitted_from_planning_directive" in gated.policy_reason_codes


def test_policy_gate_rejects_model_draft_that_undercounts_required_goal() -> None:
    observation = AgentObservationBuilder().build(
        {"requestIntentContract": {"requiredIntents": [{"intentType": "campus_visit", "target": 2}]}}
    )
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "draft_itinerary",
            "confidence": 0.94,
            "decisionSummary": "只安排一所高校",
            "requiredTools": ["resolve_poi", "patch_itinerary"],
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["campus_visit"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "高校",
                        "requiredGoalIds": ["campus_visit"],
                        "requiredGoalCounts": {"campus_visit": 1},
                    }
                ],
            },
            "targetScope": {},
            "stopCondition": {"type": "verified_goal_state"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "required_goal_omitted_from_planning_directive" in gated.policy_reason_codes


def test_policy_gate_rejects_model_draft_that_exceeds_authoritative_maximum() -> None:
    observation = AgentObservationBuilder().build(
        {
            "resolvedTripDates": {"dates": ["2026-10-01", "2026-10-02"]},
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "preferredCount": 1,
                        "maxCount": 1,
                        "distributionPolicy": "spread_across_distinct_days",
                        "allowedDayNumbers": [1, 2],
                    }
                ]
            },
        }
    )
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "draft_itinerary",
            "confidence": 0.94,
            "decisionSummary": "错误地把单数夜景扩展为两晚",
            "requiredTools": ["resolve_poi", "patch_itinerary"],
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_night_view"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "夜景",
                        "requiredGoalIds": ["goal_night_view"],
                        "requiredGoalCounts": {"goal_night_view": 1},
                    },
                    {
                        "dayNumber": 2,
                        "theme": "夜景",
                        "requiredGoalIds": ["goal_night_view"],
                        "requiredGoalCounts": {"goal_night_view": 1},
                    },
                ],
            },
            "targetScope": {},
            "stopCondition": {"type": "verified_goal_state"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "draft_goal_cardinality_overallocated" in gated.policy_reason_codes


def test_policy_gate_rejects_goal_scheduled_on_disallowed_day() -> None:
    observation = AgentObservationBuilder().build(
        {
            "resolvedTripDates": {"dates": ["2026-10-01", "2026-10-02"]},
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "maxCount": 1,
                        "allowedDayNumbers": [2],
                    }
                ]
            },
        }
    )
    payload = {
        "schemaVersion": "agent-decision-v2",
        "primaryAction": "draft_itinerary",
        "confidence": 0.94,
        "decisionSummary": "夜景放在不允许的日期",
        "requiredTools": ["resolve_poi", "patch_itinerary"],
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_night_view"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "夜景",
                    "requiredGoalIds": ["goal_night_view"],
                    "requiredGoalCounts": {"goal_night_view": 1},
                }
            ],
        },
        "targetScope": {},
        "stopCondition": {"type": "verified_goal_state"},
    }

    gated = AgentDecisionPolicyGate().evaluate(
        AgentDecision.model_validate(payload),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "draft_goal_day_not_allowed" in gated.policy_reason_codes


def test_v3_draft_accepts_soft_goal_in_priority_but_only_optional_schedule() -> None:
    observation = _hard_and_soft_goal_observation()
    controller = AgentAutonomyController(provider=None)
    decision, _aliases, _elapsed, _normalized = controller._validate_provider_decision(
        _hard_and_soft_draft_payload(),
        controller._normalization_context(
            {
                "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"]},
                "observation": observation.model_dump(by_alias=True),
            }
        ),
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is True
    assert decision.action_directive.goal_priority == [
        "goal_campus_visit",
        "goal_museum",
        "goal_meal",
    ]
    assert decision.action_directive.day_strategies[0].optional_goal_ids == ["goal_meal"]


def test_v3_draft_policy_rejects_unscheduled_hard_goal() -> None:
    observation = _hard_and_soft_goal_observation()
    controller = AgentAutonomyController(provider=None)
    decision, _aliases, _elapsed, _normalized = controller._validate_provider_decision(
        _hard_and_soft_draft_payload(include_museum=False),
        controller._normalization_context(
            {
                "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"]},
                "observation": observation.model_dump(by_alias=True),
            }
        ),
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "required_goal_omitted_from_planning_directive" in gated.policy_reason_codes


def test_v3_draft_policy_rejects_soft_goal_in_required_bucket() -> None:
    observation = _hard_and_soft_goal_observation()
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "confidence": 1.0,
            "decisionSummary": "错误地把 soft meal 当成 required",
            "requiredTools": ["resolve_poi", "patch_itinerary"],
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_campus_visit", "goal_museum", "goal_meal"],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "高校与博物馆",
                        "requiredGoalIds": ["goal_campus_visit", "goal_museum", "goal_meal"],
                        "requiredGoalCounts": {
                            "goal_campus_visit": 1,
                            "goal_museum": 1,
                            "goal_meal": 1,
                        },
                        "optionalGoalIds": [],
                    }
                ],
            },
            "targetScope": {},
            "stopCondition": {"type": "verified_goal_state"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "draft_soft_goal_misclassified_as_required" in gated.policy_reason_codes


def test_v3_draft_policy_rejects_goal_id_outside_observation() -> None:
    observation = _hard_and_soft_goal_observation()
    payload = _hard_and_soft_draft_payload()
    payload["actionDirective"]["goalPriority"].append("goal_invented")
    controller = AgentAutonomyController(provider=None)
    decision, _aliases, _elapsed, _normalized = controller._validate_provider_decision(
        payload,
        controller._normalization_context(
            {
                "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"]},
                "observation": observation.model_dump(by_alias=True),
            }
        ),
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "draft_goal_id_not_in_observation" in gated.policy_reason_codes


def test_v3_draft_repair_output_with_soft_optional_goal_is_executable() -> None:
    observation = _hard_and_soft_goal_observation()

    class RepairingDraftProvider:
        model = "provider-shaped"

        def __init__(self) -> None:
            self.repair: dict[str, Any] = {}

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            if not repair_feedback:
                invalid = _hard_and_soft_draft_payload()
                invalid["actionDirective"]["unknownField"] = True
                return invalid
            self.repair = json.loads(repair_feedback)
            return _hard_and_soft_draft_payload()

    provider = RepairingDraftProvider()
    context = {
        "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"]},
        "observation": observation.model_dump(by_alias=True),
    }
    result = AgentAutonomyController(provider=provider).decide(
        "安排北京两日游",
        {"runtimeLimits": {"remainingRunMs": 12500}},
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert result.schema_repair_attempts == 1
    assert result.gated_decision.accepted is True
    assert provider.repair["requiredGoalCounts"] == {
        "goal_campus_visit": 1,
        "goal_museum": 1,
    }
    assert provider.repair["optionalGoalIds"] == ["goal_meal"]
    assert provider.repair["minimalExample"]["actionDirective"]["goalPriority"] == [
        "goal_campus_visit",
        "goal_museum",
        "goal_meal",
    ]


def test_policy_gate_rejects_cross_group_candidate_pair() -> None:
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {"id": "seg_1", "title": "晚餐"},
                            {"id": "seg_2", "title": "夜景"},
                        ],
                    }
                ],
            },
            "pendingAmapPoiCandidates": [
                {
                    "id": "group_a",
                    "sourceSegmentId": "seg_1",
                    "status": "pending",
                    "candidates": [{"id": "amap_a", "name": "A"}],
                },
                {
                    "id": "group_b",
                    "sourceSegmentId": "seg_2",
                    "status": "pending",
                    "candidates": [{"id": "amap_b", "name": "B"}],
                },
            ],
        }
    )
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "patch_itinerary",
            "confidence": 0.95,
            "decisionSummary": "交叉组合候选",
            "requiredTools": ["patch_itinerary"],
            "actionDirective": {
                "type": "patch_itinerary",
                "operationIntent": "replace_segment_poi_from_candidate",
                "baseVersionId": "ver_1",
                "targetSegmentIds": ["seg_1"],
                "requestedOutcome": "选择候选",
                "candidateId": "group_a",
                "amapPoiId": "amap_b",
            },
            "targetScope": {
                "baseVersionId": "ver_1",
                "segmentIds": ["seg_1"],
                "operationScope": "replace_segment_poi_from_candidate",
                "candidateId": "group_a",
                "amapPoiId": "amap_b",
            },
            "stopCondition": {"type": "verified_patch"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "target_scope_not_in_observation" in gated.policy_reason_codes


def test_policy_gate_rejects_patch_directive_scope_conflict() -> None:
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [{"id": "day_1", "dayNumber": 1, "segments": [{"id": "seg_1", "title": "上午行程"}]}],
            },
        }
    )
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "patch_itinerary",
            "confidence": 0.95,
            "decisionSummary": "directive 与 scope 时间冲突",
            "requiredTools": ["patch_itinerary"],
            "actionDirective": {
                "type": "patch_itinerary",
                "operationIntent": "replace_segment_start_time",
                "baseVersionId": "ver_1",
                "targetSegmentIds": ["seg_1"],
                "requestedOutcome": "改到十点",
                "startTime": "10:00",
            },
            "targetScope": {
                "baseVersionId": "ver_1",
                "segmentIds": ["seg_1"],
                "operationScope": "replace_segment_start_time",
                "startTime": "11:00",
            },
            "stopCondition": {"type": "verified_patch"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "action_directive_target_scope_mismatch" in gated.policy_reason_codes


def test_policy_gate_rejects_candidate_directive_scope_conflict_even_when_both_pairs_are_safe() -> None:
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {"id": "seg_1", "title": "晚餐"},
                            {"id": "seg_2", "title": "夜景"},
                        ],
                    }
                ],
            },
            "pendingAmapPoiCandidates": [
                {
                    "id": "group_a",
                    "sourceSegmentId": "seg_1",
                    "status": "pending",
                    "candidates": [{"id": "amap_a", "name": "A"}],
                },
                {
                    "id": "group_b",
                    "sourceSegmentId": "seg_2",
                    "status": "pending",
                    "candidates": [{"id": "amap_b", "name": "B"}],
                },
            ],
        }
    )
    decision = AgentDecision.model_validate(
        {
            "schemaVersion": "agent-decision-v2",
            "primaryAction": "patch_itinerary",
            "confidence": 0.95,
            "decisionSummary": "directive 与 scope 选择不同安全候选",
            "requiredTools": ["patch_itinerary"],
            "actionDirective": {
                "type": "patch_itinerary",
                "operationIntent": "replace_segment_poi_from_candidate",
                "baseVersionId": "ver_1",
                "targetSegmentIds": ["seg_1"],
                "requestedOutcome": "选择 A",
                "candidateId": "group_a",
                "amapPoiId": "amap_a",
            },
            "targetScope": {
                "baseVersionId": "ver_1",
                "segmentIds": ["seg_2"],
                "operationScope": "replace_segment_poi_from_candidate",
                "candidateId": "group_b",
                "amapPoiId": "amap_b",
            },
            "stopCondition": {"type": "verified_patch"},
        }
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "action_directive_target_scope_mismatch" in gated.policy_reason_codes


def test_stop_policy_blocks_repeated_action_signature_and_no_progress() -> None:
    observation = AgentObservationBuilder().build({"runtimeLimits": {"remainingCycles": 2}})

    repeated = AgentStopPolicy().evaluate(
        observation,
        cycle_index=1,
        max_cycles=3,
        action_signature="resolve_poi:seg_1",
        previous_action_signature="resolve_poi:seg_1",
    )
    no_progress = AgentStopPolicy().evaluate(
        observation,
        cycle_index=1,
        max_cycles=3,
        no_progress_cycles=1,
    )

    assert repeated.should_stop is True
    assert repeated.reason == "repeated_action_signature"
    assert no_progress.should_stop is True
    assert no_progress.reason == "no_progress"


def test_model_owned_draft_cannot_silently_use_rule_fallback() -> None:
    assert (
        AgentService._rule_initial_fallback_allowed(
            {
                "agentDecisionState": {
                    "source": "controller",
                    "primaryAction": "draft_itinerary",
                    "actionDirective": {"type": "draft_itinerary"},
                }
            }
        )
        is False
    )
    assert (
        AgentService._rule_initial_fallback_allowed(
            {
                "explicitRuleSafeDraft": True,
                "agentDecisionState": {
                    "source": "safe_fallback",
                    "primaryAction": "draft_itinerary",
                },
            }
        )
        is True
    )

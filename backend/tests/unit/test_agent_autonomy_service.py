import json
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Thread

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.services.agent_autonomy_service import (
    AgentAutonomyController,
    AgentDecision,
    AgentDecisionArbitrator,
    AgentDecisionPolicyGate,
    AutonomyContextProjector,
    ModelDecisionV3,
    PRIMARY_ACTION_VALUES,
    allowed_primary_actions_for_observation,
    request_context_requires_clarification,
)
from src.services.agent_decision_normalizer import DecisionNormalizationError
from src.services.agent_decision_contract_service import AgentDecisionContractService
from src.services.clarification_checkpoint_service import ClarificationCheckpointService
from src.services.agent_executor_registry import AgentActionExecutorRegistry
from src.services.controller_failure_classifier import classify_controller_failure
from src.services.controller_context_projection_service import FULL_REQUEST_BYTE_LIMIT
from src.services.deepseek_agent_provider import AUTONOMY_DECISION_SYSTEM_PROMPT, DeepSeekAgentProvider
from src.services.controller_response_integrity import (
    CONTROLLER_FULL_MAX_OUTPUT_TOKENS,
    ControllerOutputTruncatedError,
    ControllerResponseIntegrityEvidence,
)
from src.services.agent_observation_service import AgentObservationBuilder


@pytest.mark.parametrize("placement", ["required", "only_optional", "both", "preference_extra_day", "authorized_extra_day", "underquota_extra_day"])
def test_frozen_hard_goal_bucket_conflict_never_repairs_or_executes(placement):
    from copy import deepcopy
    from types import SimpleNamespace

    goals = [{"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1, "maxCount": 1,
              "requirementLevel": "required", "allowedDayNumbers": [1]},
             {"goalId": "goal_meal", "intentType": "meal", "requiredMin": 0, "maxCount": 1,
              "requirementLevel": "soft_experience", "allowedDayNumbers": [1]}]
    context = {"latestUserMessage": "安排参观和午餐", "effectiveUserMessage": "安排参观和午餐", "selectedCity": "北京",
               "serverExecutionProfile": "simple_open_v1",
               "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01"], "dayCount": 1},
               "requestIntentContract": {"dayCount": 1, "requiredPlanningDayNumbers": [1], "requiredIntents": goals,
                                         "planningRequestEnvelope": {"sourcePlanningRootTurnId": "frozen_root"},
                                         "routeDecisionContract": {"status": "ready", "missingFields": [],
                                                                   "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
                                                                   "detourTolerance": {"maxGeneralizedCostDelta": 0, "maxDetourRatio": 0}}}}
    directive = {"type": "draft_itinerary", "optionalExperienceBudget": 2,
                 "dayStrategies": [{"dayNumber": 1, "theme": "参观与午餐", "requiredGoalIds": ["goal_museum"],
                                    "requiredGoalCounts": {"goal_museum": 1}, "optionalGoalIds": ["goal_meal"]}],
                 "routePlanningPolicy": {"source": "controller_estimate", "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
                                         "detourEnvelope": {"maxGeneralizedCostDelta": 0, "maxDetourRatio": 0}},
                 "occurrenceScheduleHints": [{"goalId": goal, "dayNumber": 1, "dayPart": part, "sequence": number,
                                              "preferredStartTime": clock, "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                                              "estimateSource": "controller_estimate", "confidence": 0.8}
                                             for goal, part, number, clock in [("goal_museum", "morning", 1, "09:00"), ("goal_meal", "noon", 2, "12:00")]]}
    strategy = directive["dayStrategies"][0]
    if placement in {"only_optional", "both"}:
        strategy["optionalGoalIds"].append("goal_museum")
    if placement == "only_optional":
        strategy["requiredGoalIds"] = []
        strategy["requiredGoalCounts"] = {}
    if placement in {"preference_extra_day", "authorized_extra_day", "underquota_extra_day"}:
        # preferredCount is a soft target, not maxCount's hard upper bound.
        goals[0].update(requiredMin=2 if placement == "underquota_extra_day" else 1,
                        preferredCount=1 if placement == "authorized_extra_day" else 2,
                        maxCount=2, allowedDayNumbers=[1, 2])
        context["resolvedTripDates"].update(dates=["2026-10-01", "2026-10-02"], dayCount=2)
        context["requestIntentContract"].update(dayCount=2, requiredPlanningDayNumbers=[1, 2])
        directive["dayStrategies"].append({"dayNumber": 2, "theme": "次日偏好额外参观",
                                            "requiredGoalIds": [], "requiredGoalCounts": {}, "optionalGoalIds": ["goal_museum"]})
        second_hint = deepcopy(directive["occurrenceScheduleHints"][0])
        second_hint["dayNumber"] = 2
        directive["occurrenceScheduleHints"].append(second_hint)
    output = {"schemaVersion": "agent-decision-v3", "primaryAction": "draft_itinerary", "actionDirective": directive}
    before_output = deepcopy(output)
    before_contract = deepcopy(context["requestIntentContract"])
    calls = []

    def full(*args, **kwargs):
        calls.append(kwargs.get("repair_feedback", ""))
        assert len(calls) == 1, "Hard bucket conflicts may not be repaired by another model call"
        return deepcopy(output)

    def forbidden(*args, **kwargs):
        pytest.fail("Hard bucket conflicts must not enter Lite or rule fallback")

    observation = AgentObservationBuilder().build(context)
    context["agentObservation"] = observation.model_dump(by_alias=True)
    result = AgentAutonomyController(provider=SimpleNamespace(decide_autonomy=full, decide_autonomy_lite=forbidden),
                                     fallback_decision_resolver=SimpleNamespace(resolve=forbidden)).decide(
        context["latestUserMessage"], context, AutonomyContextProjector().project(context),
        available_tools={"resolve_poi", "patch_itinerary"}, runtime_budget_tools={"resolve_poi", "patch_itinerary"}, observation=observation,
    )
    assert calls == [""] and result.schema_repair_attempts == 0 and not result.controller_lite_called
    assert context["requestIntentContract"] == before_contract and output == before_output
    if placement in {"required", "preference_extra_day", "authorized_extra_day"}:
        assert result.controller_full_succeeded, result.controller_error
        assert result.gated_decision.accepted
        assert result.decision.action_directive.day_strategies[0].optional_goal_ids == ["goal_meal"]
        if placement in {"preference_extra_day", "authorized_extra_day"}:
            assert result.decision.action_directive.day_strategies[1].optional_goal_ids == ["goal_museum"]
    else:
        assert not result.controller_full_succeeded
        assert result.decision.primary_action == "ask_user" and result.decision.required_tools == []
        assert "draft_required_goal_misclassified_as_optional" in result.decision.reason_codes
        assert result.decision.expected_outcome["conflictingGoalId"] == "goal_museum"
        assert result.decision.side_effects["itinerary"] is False


def test_controller_ask_user_preserves_dynamic_dimension_and_semantic_options():
    decision = ModelDecisionV3.model_validate(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "type": "ask_user",
                "question": "两晚的夜间安排各自更希望强调什么？",
                "dimensionId": "night_view.experience_mode",
                "whyItMatters": "体验类型会改变候选准入和路线可行性。",
                "allowFreeText": True,
                "options": [
                    {
                        "id": "public_view",
                        "label": "开放的城市公共视野",
                        "semanticValue": {
                            "accessPolicy": "public_outdoor",
                            "experienceFamilies": ["public_city_view"],
                        },
                        "allowsManualInput": False,
                    },
                    {
                        "id": "waterfront",
                        "label": "滨水夜游",
                        "semanticValue": {
                            "accessPolicy": "public_outdoor",
                            "experienceFamilies": ["waterfront_evening"],
                        },
                        "allowsManualInput": False,
                    },
                ],
            },
        }
    )

    result = AgentAutonomyController._bind_server_target_scope(
        decision,
        {
            "requestedDayNumber": None,
            "unresolvedSlots": [],
            "clarificationDimensions": [
                {
                    "dimensionId": "night_view.experience_mode",
                    "status": "unresolved",
                    "allowedSemanticFields": ["accessPolicy", "experienceFamilies"],
                }
            ],
        },
    )

    clarification = result.clarification
    assert clarification is not None
    assert clarification["dimensionId"] == "night_view.experience_mode"
    assert clarification["options"][0]["semanticValue"] == {
        "accessPolicy": "public_outdoor",
        "experienceFamilies": ["public_city_view"],
    }
    assert clarification["allowFreeText"] is True


def test_controller_accepts_live_shaped_batch_when_ask_user_type_is_omitted():
    controller = AgentAutonomyController()
    decision, aliases, _, normalized = controller._validate_provider_decision(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "questions": [
                    {
                        "dimensionId": "night_view.cardinality",
                        "question": "国庆两晚中，希望安排几次夜景体验？",
                        "whyItMatters": "决定夜景在两天中的分布。",
                        "allowFreeText": True,
                        "options": [
                            {
                                "id": "once",
                                "label": "仅一晚",
                                "semanticValue": {"frequency": 1},
                            },
                            {
                                "id": "twice",
                                "label": "两晚都安排",
                                "semanticValue": {"frequency": 2},
                            },
                        ],
                    },
                    {
                        "dimensionId": "night_view.experience_mode",
                        "question": "偏好哪种夜景体验？",
                        "whyItMatters": "影响候选地点类型。",
                        "allowFreeText": True,
                        "options": [
                            {
                                "id": "public",
                                "label": "公共开放观景点",
                                "semanticValue": {"experienceFamilies": ["public_city_view"]},
                            },
                            {
                                "id": "waterfront",
                                "label": "滨水夜景",
                                "semanticValue": {"experienceFamilies": ["waterfront_evening"]},
                            },
                        ],
                    },
                ]
            },
        },
        {
            "requestedDayNumber": None,
            "unresolvedSlots": [],
            "clarificationDimensions": [
                {
                    "dimensionId": "night_view.cardinality",
                    "status": "unresolved",
                    "allowedSemanticFields": ["frequency"],
                },
                {
                    "dimensionId": "night_view.experience_mode",
                    "status": "unresolved",
                    "allowedSemanticFields": ["experienceFamilies"],
                },
            ],
        },
    )

    assert decision.primary_action == "ask_user"
    assert decision.action_directive.type == "ask_user"
    assert [item.dimension_id for item in decision.action_directive.questions] == [
        "night_view.cardinality",
        "night_view.experience_mode",
    ]
    assert aliases == ["actionDirective.type:copied_from_primaryAction"]
    assert normalized["actionDirective"]["type"] == "ask_user"


def _checkpoint_bound_ask_user_directive() -> dict:
    return {
        "type": "ask_user",
        "question": "第二轮需要继续确认哪一种体验约束？",
        "dimensionId": "night_view.experience_mode",
        "whyItMatters": "这会改变候选准入和路线可行性。",
        "allowFreeText": True,
        "checkpointId": "clarify_existing",
        "planningRootId": "turn_root",
        "requestFingerprint": "request-fingerprint",
        "checkpointFingerprint": "checkpoint-fingerprint",
        "options": [
            {
                "id": "public",
                "label": "公共户外体验",
                "semanticValue": {"accessPolicy": "public_outdoor"},
                "allowsManualInput": False,
            },
            {
                "id": "manual",
                "label": "我补充约束",
                "semanticValue": {"accessPolicy": "custom"},
                "allowsManualInput": True,
            },
        ],
    }


def _checkpoint_identity_context() -> dict:
    return {
        "clarificationCheckpoint": {
            "schemaVersion": "clarification-checkpoint-v1",
            "checkpointId": "clarify_existing",
            "planningRootId": "turn_root",
            "requestFingerprint": "request-fingerprint",
            "fingerprint": "checkpoint-fingerprint",
            "status": "answered",
        },
        "clarificationDimensions": [
            {
                "dimensionId": "night_view.experience_mode",
                "status": "unresolved",
                "allowedSemanticFields": ["accessPolicy"],
            }
        ],
    }


@pytest.mark.parametrize(
    "missing_field",
    [
        "checkpointId",
        "planningRootId",
        "requestFingerprint",
        "checkpointFingerprint",
    ],
)
def test_existing_checkpoint_requires_every_controller_identity_field(missing_field):
    directive = _checkpoint_bound_ask_user_directive()
    directive.pop(missing_field)
    controller = AgentAutonomyController()

    with pytest.raises(
        DecisionNormalizationError,
        match="clarification_checkpoint_identity_missing",
    ):
        controller._validate_provider_decision(
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": directive,
            },
            _checkpoint_identity_context(),
        )


@pytest.mark.parametrize(
    "tampered_field",
    [
        "checkpointId",
        "planningRootId",
        "requestFingerprint",
        "checkpointFingerprint",
    ],
)
def test_existing_checkpoint_rejects_each_tampered_controller_identity(tampered_field):
    directive = _checkpoint_bound_ask_user_directive()
    directive[tampered_field] = f"tampered-{tampered_field}"
    controller = AgentAutonomyController()

    with pytest.raises(
        DecisionNormalizationError,
        match="clarification_checkpoint_identity_mismatch",
    ):
        controller._validate_provider_decision(
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": directive,
            },
            _checkpoint_identity_context(),
        )


def test_existing_checkpoint_identity_is_preserved_in_bound_clarification():
    controller = AgentAutonomyController()
    decision, _, _, _ = controller._validate_provider_decision(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": _checkpoint_bound_ask_user_directive(),
        },
        _checkpoint_identity_context(),
    )

    assert decision.clarification is not None
    assert {
        key: decision.clarification[key]
        for key in (
            "checkpointId",
            "planningRootId",
            "requestFingerprint",
            "checkpointFingerprint",
        )
    } == {
        "checkpointId": "clarify_existing",
        "planningRootId": "turn_root",
        "requestFingerprint": "request-fingerprint",
        "checkpointFingerprint": "checkpoint-fingerprint",
    }


@pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_controller_accepts_one_valid_v3_object_with_only_an_inert_trailing_closer(model_name):
    provider = DeepSeekAgentProvider(api_key="test-key", model=model_name, timeout_seconds=30)
    controller = AgentAutonomyController(provider=provider)
    raw = (
        json.dumps(
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": _checkpoint_bound_ask_user_directive(),
            },
            ensure_ascii=False,
        )
        + "}"
    )

    decision, aliases, _elapsed, _normalized = controller._validate_provider_decision(
        raw,
        _checkpoint_identity_context(),
    )

    assert provider.model == model_name
    assert decision.primary_action == "ask_user"
    assert decision.clarification is not None
    assert "controller_json_inert_suffix_trimmed" in aliases


def test_controller_rejects_a_second_json_object_after_a_valid_decision():
    controller = AgentAutonomyController()
    raw = json.dumps(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": _checkpoint_bound_ask_user_directive(),
        },
        ensure_ascii=False,
    ) + json.dumps({"primaryAction": "finish"})

    with pytest.raises(json.JSONDecodeError):
        controller._validate_provider_decision(
            raw,
            _checkpoint_identity_context(),
        )


@pytest.mark.parametrize("suffix", ["]", "```", " trailing explanation"])
def test_controller_rejects_non_inert_content_after_a_valid_decision(suffix):
    controller = AgentAutonomyController()
    raw = (
        json.dumps(
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": _checkpoint_bound_ask_user_directive(),
            },
            ensure_ascii=False,
        )
        + suffix
    )

    with pytest.raises(json.JSONDecodeError):
        controller._validate_provider_decision(
            raw,
            _checkpoint_identity_context(),
        )


def test_controller_prompt_requires_exact_checkpoint_identity_echo():
    assert "checkpointId" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "planningRootId" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "requestFingerprint" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "checkpointFingerprint" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "echo" in AUTONOMY_DECISION_SYSTEM_PROMPT.casefold()


def test_v3_resolve_scope_derives_unique_segment_from_observed_goal():
    model_decision = ModelDecisionV3.model_validate(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "resolve_poi",
            "actionDirective": {
                "type": "resolve_poi",
                "targetGoalId": "goal_museum",
                "targetSegmentIds": [],
                "searchIntent": "清华大学艺术博物馆",
            },
        }
    )

    decision = AgentAutonomyController._bind_server_target_scope(
        model_decision,
        {
            "requestedDayNumber": 1,
            "unresolvedSlots": [
                {"goalId": "goal_museum", "segmentId": "seg_museum", "dayNumber": 1},
                {"goalId": "goal_campus_visit", "segmentId": "seg_campus"},
            ],
        },
    )

    scope = decision.target_scope.model_dump(by_alias=True)
    assert scope["segmentIds"] == ["seg_museum"]
    assert scope["dayNumber"] == 1


def test_v3_resolve_scope_does_not_cross_day_when_explicit_day_has_no_goal_match():
    model_decision = ModelDecisionV3.model_validate(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "resolve_poi",
            "actionDirective": {
                "type": "resolve_poi",
                "targetGoalId": "goal_museum",
                "targetSegmentIds": [],
                "searchIntent": "清华大学艺术博物馆",
            },
        }
    )

    decision = AgentAutonomyController._bind_server_target_scope(
        model_decision,
        {
            "requestedDayNumber": 1,
            "unresolvedSlots": [
                {"goalId": "goal_museum", "segmentId": "seg_day2_museum", "dayNumber": 2},
            ],
        },
    )

    assert decision.target_scope.model_dump(by_alias=True)["segmentIds"] == []


def test_provider_decision_artifact_redaction_is_recursive_and_drops_headers_and_secrets():
    redacted = AgentAutonomyController._redacted_provider_decision(
        {
            "schemaVersion": "agent-decision-v3",
            "headers": {"Authorization": "Bearer secret"},
            "nested": {
                "reasoning": "allowed summary",
                "reasoningContent": "private chain",
                "hiddenPrompt": "private hidden prompt",
                "internalPrompt": "private internal prompt",
                "analysis": "private analysis",
                "thinking": "private thinking",
                "thought": "private thought",
                "api_key": "private key",
                "access-token": "private token",
                "cookie": "private cookie",
            },
        }
    )

    assert redacted == {
        "schemaVersion": "agent-decision-v3",
        "nested": {},
    }
    assert AgentAutonomyController._redacted_provider_decision("not-json") == {
        "invalidJson": True,
        "payloadLength": 8,
    }


def test_full_controller_rejects_legacy_v2_target_scope_payload():
    controller = AgentAutonomyController(provider=_RepairingProvider(), planner_service=_Planner())

    with pytest.raises(ValueError, match="controller_full_requires_model_decision_v3"):
        controller._validate_provider_decision(
            {
                "schemaVersion": "agent-decision-v2",
                "primaryAction": "read_itinerary",
                "confidence": 0.9,
                "decisionSummary": "legacy scope",
                "actionDirective": {"type": "read_itinerary", "queryType": "timeline_summary"},
                "targetScope": {},
                "stopCondition": {"type": "read_complete"},
            },
            {},
        )


def test_controller_prompt_continues_persisted_pre_version_slots_without_redrafting():
    assert "observation.planningAttempt.persisted is true" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "do not choose\ndraft_itinerary again" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "targetGoalId" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "targetSegmentIds=[]" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "A planning slot is not a segment" in AUTONOMY_DECISION_SYSTEM_PROMPT


def test_controller_prompt_explicitly_requires_nested_draft_fields():
    prompt = " ".join(AUTONOMY_DECISION_SYSTEM_PROMPT.split())
    assert "Every dayStrategies item must include a non-empty theme" in prompt
    assert (
        "Every occurrenceScheduleHints item MUST include goalId, dayNumber, dayPart, sequence (integer 1-12)" in prompt
    )
    assert "durationEstimate with required min/preferred/max" in prompt
    assert "estimateSource (controller_estimate, user_explicit, or trusted_server_fact)" in prompt
    assert "confidence (number 0-1)" in prompt


def test_autonomy_context_projector_excludes_large_provider_and_route_payloads():
    projected = AutonomyContextProjector(max_turns=2).project(
        {
            "sessionId": "session_1",
            "latestUserMessage": "只看一下当前行程",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "title": "北京行程",
                "days": [{"segments": [{"id": "seg_1"}]}],
                "routeOptions": [{"polyline": "large" * 1000}],
                "photos": ["large" * 1000],
            },
            "understoodRequirements": {
                "summary": "只读查询",
                "providerDebug": {"request": "must-not-leak"},
            },
            "activeConversationTurns": [
                {"role": "user", "content": "old", "turnIndex": 1},
                {"role": "assistant", "content": "newer", "turnIndex": 2},
                {"role": "user", "content": "latest", "turnIndex": 3},
            ],
        }
    )

    assert projected["itinerarySummary"] == {
        "planId": "plan_1",
        "title": "北京行程",
        "dayCount": 1,
        "segmentCount": 1,
    }
    assert [turn["turnIndex"] for turn in projected["recentTurns"]] == [2, 3]
    assert "providerDebug" not in projected["requirements"]
    assert "routeOptions" not in projected["itinerarySummary"]
    assert "photos" not in projected["itinerarySummary"]


def test_policy_gate_clips_tools_and_recomputes_write_risk():
    decision = AgentDecision(
        primaryAction="patch_itinerary",
        confidence=0.9,
        decisionSummary="修改一个 segment",
        requiredTools=["read_itinerary", "patch_itinerary", "web_search", "admin_write"],
        proposedWriteRisk="none",
        targetScope={
            "segmentIds": ["seg_1"],
            "baseVersionId": "ver_1",
            "operationScope": "replace_segment_start_time",
        },
        stopCondition={"type": "verified_patch"},
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"read_itinerary", "patch_itinerary", "web_search"},
        runtime_budget_tools={"read_itinerary", "patch_itinerary", "web_search"},
    )

    assert gated.effective_tools == ["patch_itinerary", "read_itinerary"]
    assert gated.effective_write_risk == "medium"
    assert "required_tools_clipped" in gated.policy_reason_codes
    assert "write_risk_recomputed" in gated.policy_reason_codes
    assert (
        AgentActionExecutorRegistry().route(
            {
                "accepted": gated.accepted,
                "primaryAction": gated.decision.primary_action,
                "actionDirective": None,
            }
        )
        == "timeline_mutation_executor"
    )


def test_patch_target_scope_preserves_candidate_replace_preserve_markers():
    decision = AgentDecision(
        primaryAction="patch_itinerary",
        confidence=0.9,
        decisionSummary="替换候选地点并保留原停留时长",
        requiredTools=["patch_itinerary"],
        proposedWriteRisk="medium",
        targetScope={
            "segmentIds": ["seg_1"],
            "baseVersionId": "ver_1",
            "operationScope": "replace_segment_poi_from_candidate",
            "candidateId": "cand_1",
            "amapPoiId": "B0001",
            "preserve": ["target_duration", "other_segments", "other_days"],
        },
        stopCondition={"type": "verified_patch"},
    )

    target_scope = decision.target_scope
    assert target_scope.model_dump(by_alias=True, exclude_none=True)["preserve"] == [
        "target_duration",
        "other_segments",
        "other_days",
    ]


def test_v3_patch_scope_binds_preserve_contract_from_validated_directive():
    model_decision = ModelDecisionV3.model_validate(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "patch_itinerary",
            "actionDirective": {
                "type": "patch_itinerary",
                "operationIntent": "replace_segment_poi_from_candidate",
                "baseVersionId": "ver_1",
                "targetSegmentIds": ["seg_1"],
                "requestedOutcome": "替换地点并保持原停留时长",
                "candidateId": "cand_1",
                "amapPoiId": "B0001",
                "preserve": ["target_duration", "other_segments", "other_days"],
            },
        }
    )

    decision = AgentAutonomyController._bind_server_target_scope(model_decision, {})
    scope = decision.target_scope.model_dump(by_alias=True, exclude_none=True)

    assert scope["preserve"] == ["target_duration", "other_segments", "other_days"]
    assert AgentDecisionPolicyGate._directive_matches_target_scope(decision) is True


@pytest.mark.parametrize("checkpoint_status", ["awaiting_answer", "awaiting_agent_resolution"])
def test_pending_clarification_rejects_draft_at_policy_gate(checkpoint_status):
    decision = AgentDecision(
        primaryAction="draft_itinerary",
        confidence=1.0,
        decisionSummary="错误地尝试跳过澄清",
        requiredTools=["patch_itinerary"],
        proposedWriteRisk="high",
        targetScope={},
        stopCondition={"type": "verified_draft"},
    )
    request_context = {
        "clarificationCheckpoint": {
            "status": checkpoint_status,
            "checkpointId": "checkpoint_pending",
        },
        "requestIntentContract": {"clarificationRequired": True},
    }

    assert request_context_requires_clarification(request_context) is True
    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        request_context=request_context,
    )

    assert gated.accepted is False
    assert "pending_clarification_requires_ask_user" in gated.policy_reason_codes


def test_pending_clarification_allows_read_only_itinerary_query_at_policy_gate():
    decision = AgentDecision(
        primaryAction="read_itinerary",
        confidence=1.0,
        decisionSummary="读取当前行程而不改变待澄清规划",
        requiredTools=["read_itinerary"],
        proposedWriteRisk="none",
        targetScope={},
        stopCondition={"type": "read_complete"},
    )
    request_context = {
        "clarificationCheckpoint": {
            "status": "awaiting_agent_resolution",
            "checkpointId": "checkpoint_pending",
        },
        "requestIntentContract": {"clarificationRequired": True},
    }

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
        request_context=request_context,
    )

    assert gated.accepted is True
    assert gated.effective_tools == ["read_itinerary"]
    assert gated.effective_write_risk == "none"


@pytest.mark.parametrize(
    ("latest_message", "accepted"),
    [
        ("谢谢，不需要继续", True),
        ("继续帮我规划北京行程", False),
    ],
)
def test_pending_clarification_allows_finish_only_for_explicit_stop_intent(latest_message, accepted):
    decision = AgentDecision(
        primaryAction="finish",
        confidence=1.0,
        decisionSummary="结束当前规划轮次",
        requiredTools=[],
        proposedWriteRisk="none",
        targetScope={},
        actionDirective={"type": "finish", "assistantReply": "本轮未写入行程。"},
        stopCondition={"type": "finished"},
    )
    request_context = {
        "latestUserMessage": latest_message,
        "clarificationCheckpoint": {
            "status": "awaiting_agent_resolution",
            "checkpointId": "checkpoint_pending",
        },
        "requestIntentContract": {"clarificationRequired": True},
    }

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools=set(),
        runtime_budget_tools=set(),
        request_context=request_context,
    )

    assert gated.accepted is accepted
    assert ("pending_clarification_requires_ask_user" in gated.policy_reason_codes) is (not accepted)


def test_pending_route_clarification_allows_finish_for_server_classified_read_only_turn():
    decision = AgentDecision(
        primaryAction="finish",
        confidence=1.0,
        decisionSummary="只读查询已结束",
        requiredTools=[],
        proposedWriteRisk="none",
        targetScope={},
        actionDirective={"type": "finish", "assistantReply": "当前没有可验证的远期天气结果。"},
        stopCondition={"type": "uncertainty_stated"},
    )
    request_context = {
        "latestUserMessage": "帮我查明年国庆北京天气",
        "conversationIntent": {
            "classification": {
                "intent": "inspect_or_explain",
                "confidence": 0.99,
                "requestedScope": "current_action",
                "isQuestion": True,
                "isNegated": False,
            },
            "executionDisposition": "read_only",
        },
        "requestIntentContract": {
            "clarificationRequired": True,
            "routeDecisionContract": {
                "status": "awaiting_clarification",
                "missingFields": ["mobilityProfile", "detourTolerance"],
            },
        },
    }

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools=set(),
        runtime_budget_tools=set(),
        request_context=request_context,
    )

    assert gated.accepted is True
    assert gated.effective_write_risk == "none"


def test_pending_clarification_controller_contract_allows_only_safe_read_actions_and_ask_user():
    class DraftingController:
        model = "adversarial-controller"

        def __init__(self):
            self.contexts = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.contexts.append(dict(autonomy_context))
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "draft_itinerary",
                "confidence": 1.0,
                "decisionSummary": "忽略澄清并创建草案",
                "actionDirective": {
                    "type": "draft_itinerary",
                    "goalPriority": [],
                    "dayStrategies": [],
                    "optionalExperienceBudget": 0,
                    "searchPriority": [],
                    "candidateSelectionPolicy": {},
                },
                "stopCondition": {"type": "verified_draft"},
            }

    provider = DraftingController()
    request_context = {
        "clarificationCheckpoint": {
            "status": "awaiting_agent_resolution",
            "checkpointId": "checkpoint_pending",
        },
        "requestIntentContract": {"clarificationRequired": True},
    }
    result = AgentAutonomyController(provider=provider).decide(
        "我补充了一段仍待 Controller 归一化的文字",
        request_context,
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
    )

    assert provider.contexts
    assert all(
        context["allowedActions"] == ["ask_user", "read_itinerary", "verify_external_facts"]
        for context in provider.contexts
    )
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.effective_write_risk == "none"


def test_policy_gate_rejects_unstructured_question_and_long_term_trip_only_memory():
    decision = AgentDecision(
        primaryAction="ask_user",
        confidence=0.5,
        decisionSummary="需要澄清",
        proposedWriteRisk="none",
        clarification={"question": "去哪？", "options": [{"id": "one", "label": "北京"}]},
        memoryPolicy="propose_long_term",
        memoryCandidates=[{"key": "this_trip_budget", "value": "低预算", "confidence": 0.9, "tripOnly": True}],
        stopCondition={"type": "needs_confirmation"},
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools=set(),
        runtime_budget_tools=set(),
    )

    assert gated.accepted is False
    assert "structured_clarification_required" in gated.policy_reason_codes
    assert "trip_only_memory_escalation_forbidden" in gated.policy_reason_codes


def test_policy_gate_rejects_target_ids_absent_from_canonical_observation():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [{"id": "day_1", "dayNumber": 1, "segments": [{"id": "seg_real", "title": "景点"}]}],
            },
        }
    )
    decision = AgentDecision(
        primaryAction="patch_itinerary",
        confidence=0.9,
        decisionSummary="修改不存在的 segment",
        requiredTools=["patch_itinerary"],
        proposedWriteRisk="medium",
        targetScope={
            "segmentIds": ["seg_fake"],
            "baseVersionId": "ver_1",
            "operationScope": "replace_segment_start_time",
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


def test_existing_meaningful_timeline_contract_forbids_full_draft_replacement():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_existing",
            "latestUserMessage": "请只调整现有行程中的一个体验",
            "currentItinerarySnapshot": {
                "id": "plan_existing",
                "versionId": "ver_existing",
                "days": [
                    {
                        "id": "day_existing_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_existing",
                                "startTime": "09:00",
                                "endTime": "11:00",
                                "kind": "visit",
                                "poi": {"name": "清华大学", "amapId": "B0THU"},
                            }
                        ],
                    }
                ],
            },
        }
    )

    allowed = allowed_primary_actions_for_observation(observation)
    assert observation.itinerary.meaningful_segment_count == 1
    assert "draft_itinerary" not in allowed
    assert "patch_itinerary" in allowed


def test_unversioned_placeholder_does_not_block_initial_draft():
    observation = AgentObservationBuilder().build(
        {
            "currentItinerarySnapshot": {
                "id": "plan_placeholder",
                "days": [
                    {
                        "id": "day_placeholder",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_placeholder",
                                "startTime": "09:00",
                                "endTime": "10:00",
                                "kind": "visit",
                                "poi": {
                                    "name": "待定景点/活动",
                                    "groundingStatus": "waiting_for_poi_grounding",
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )

    assert observation.itinerary.meaningful_segment_count == 1
    assert "draft_itinerary" in allowed_primary_actions_for_observation(observation)


def test_versioned_placeholder_without_grounded_or_locked_node_does_not_block_draft():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_placeholder",
            "currentItinerarySnapshot": {
                "id": "plan_placeholder",
                "versionId": "ver_placeholder",
                "days": [
                    {
                        "id": "day_placeholder",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_placeholder",
                                "startTime": "09:00",
                                "endTime": "10:00",
                                "kind": "visit",
                                "poi": {
                                    "name": "待定景点/活动",
                                    "groundingStatus": "waiting_for_poi_grounding",
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert "draft_itinerary" in allowed_primary_actions_for_observation(observation)


def test_policy_gate_rejects_resolve_target_whose_segment_goal_conflicts_with_target_goal():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
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
                ]
            },
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_campus",
                                "kind": "visit",
                                "notes": "goalId=goal_campus_visit；intentType=campus_visit",
                            }
                        ],
                    }
                ],
            },
        }
    )
    decision = AgentDecision(
        primaryAction="resolve_poi",
        confidence=0.9,
        decisionSummary="错误地把第一天高校槽位当成博物馆槽位",
        requiredTools=["resolve_poi"],
        proposedWriteRisk="none",
        targetScope={
            "segmentIds": ["seg_campus"],
            "goalId": "goal_museum",
            "dayNumber": 1,
            "query": "清华大学艺术博物馆",
        },
        stopCondition={"type": "candidate_or_patch"},
    )

    gated = AgentDecisionPolicyGate().evaluate(
        decision,
        available_tools={"resolve_poi"},
        runtime_budget_tools={"resolve_poi"},
        observation=observation,
    )

    assert gated.accepted is False
    assert "target_scope_not_in_observation" in gated.policy_reason_codes


def test_policy_gate_rejects_stale_version_before_executor_can_write():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_current",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_current",
                "days": [{"id": "day_1", "dayNumber": 1, "segments": [{"id": "seg_real", "title": "景点"}]}],
            },
        }
    )
    decision = AgentDecision(
        primaryAction="patch_itinerary",
        confidence=0.9,
        decisionSummary="使用过期版本修改",
        requiredTools=["patch_itinerary"],
        proposedWriteRisk="medium",
        targetScope={
            "segmentIds": ["seg_real"],
            "baseVersionId": "ver_stale",
            "operationScope": "replace_segment_start_time",
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


class _Planner:
    def plan(self, latest_message, request_context):
        return {
            "taskRoute": "timeline_patch",
            "requiresPatch": True,
            "requiresPoiResolution": False,
            "readOnly": False,
            "allowedTools": ["read_itinerary", "patch_itinerary", "web_search"],
        }


class _InvalidProvider:
    def decide(self, autonomy_context):
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "finish",
            "confidence": 2.0,
            "decisionSummary": "invalid confidence",
            "actionDirective": {"type": "finish", "assistantReply": "invalid"},
            "stopCondition": {"type": "finished"},
        }


def test_controller_schema_invalid_uses_safe_fallback_without_planner_write():
    result = AgentAutonomyController(provider=_InvalidProvider(), planner_service=_Planner()).decide_shadow(
        "把第一天时间改到九点",
        {},
        {"schemaVersion": "autonomy-context-v1"},
        available_tools={"read_itinerary", "patch_itinerary", "web_search"},
        runtime_budget_tools={"read_itinerary", "patch_itinerary"},
    )

    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.planner_called is False
    assert result.gated_decision.effective_tools == []
    assert result.gated_decision.effective_write_risk == "none"
    assert result.controller_error.startswith("ValidationError:")
    assert result.schema_repair_attempts == 1


class _MalformedMemoryProvider:
    def decide(self, autonomy_context):
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "finish",
            "confidence": 0.8,
            "decisionSummary": "完成",
            "memoryPolicy": "propose_long_term",
            "memoryCandidates": [{"key": "pace", "value": "slow", "confidence": "high"}],
            "actionDirective": {"type": "finish", "assistantReply": "完成"},
            "stopCondition": {"type": "finished"},
        }


def test_malformed_memory_candidate_uses_schema_fallback_instead_of_raising():
    result = AgentAutonomyController(provider=_MalformedMemoryProvider(), planner_service=_Planner()).decide_shadow(
        "以后都慢一点",
        {},
        {},
        available_tools={"read_itinerary", "patch_itinerary"},
        runtime_budget_tools={"read_itinerary", "patch_itinerary"},
    )

    assert result.source == "safe_fallback"
    assert result.planner_called is False
    assert result.controller_error.startswith("ValidationError:")
    assert result.decision.primary_action == "ask_user"


class _SlowProvider:
    def decide(self, autonomy_context):
        time.sleep(0.2)
        return {}


def test_controller_timeout_uses_safe_fallback_without_waiting_or_planner_write():
    started = time.monotonic()
    result = AgentAutonomyController(
        provider=_SlowProvider(),
        planner_service=_Planner(),
        decision_timeout_seconds=0.03,
    ).decide_shadow(
        "把第一天时间改到九点",
        {},
        {},
        available_tools={"read_itinerary", "patch_itinerary"},
        runtime_budget_tools={"read_itinerary", "patch_itinerary"},
    )

    assert time.monotonic() - started < 0.15
    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.planner_called is False
    assert result.controller_error == "TimeoutError:controller_decision_timeout"


class _RepairingProvider:
    def __init__(self):
        self.calls = []

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        self.calls.append({"timeout": timeout_seconds, "repair": repair_feedback})
        if not repair_feedback:
            return {
                "primaryAction": "read_itinerary",
                "actionDirective": {"type": "finish"},
            }
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "read_itinerary",
            "actionDirective": {"type": "read_itinerary", "queryType": "timeline_summary"},
        }


def test_controller_repairs_invalid_schema_once_then_accepts_decision():
    provider = _RepairingProvider()
    result = AgentAutonomyController(provider=provider, planner_service=_Planner()).decide_shadow(
        "当前安排是什么",
        {},
        {},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    assert result.source == "controller"
    assert result.schema_repair_attempts == 1
    assert result.decision.primary_action == "read_itinerary"
    assert len(provider.calls) == 2
    assert provider.calls[0]["repair"] == ""
    assert provider.calls[1]["repair"]


def test_malformed_json_with_embedded_draft_action_fails_closed_without_repair():
    class MalformedDraftProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(repair_feedback)
            if not repair_feedback:
                return '{"schemaVersion":"agent-decision-v3","primaryAction":"draft_itinerary",'
            raise AssertionError("invalid JSON must not reach an action-specific repair provider")

    provider = MalformedDraftProvider()
    result = AgentAutonomyController(
        provider=provider,
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == [""]
    assert result.schema_repair_attempts == 0
    assert result.source == "safe_fallback"
    assert result.decision_path == "fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.controller_error.startswith(
        "DecisionNormalizationError:controller_repair_action_unresolved:primaryAction"
    )


def test_controller_fails_closed_when_truncated_primary_action_is_ambiguous():
    class AmbiguousTruncatedProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(repair_feedback)
            if repair_feedback:
                raise AssertionError("ambiguous primaryAction must not reach repair provider")
            return "{"

    provider = AmbiguousTruncatedProvider()
    result = AgentAutonomyController(
        provider=provider,
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == [""]
    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.controller_error.startswith(
        "DecisionNormalizationError:controller_repair_action_unresolved:primaryAction"
    )


def test_unknown_primary_action_with_multiple_safe_schemas_fails_closed():
    controller = AgentAutonomyController()
    contract_service = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=("patch_itinerary", "ask_user"),
    )

    with pytest.raises(
        DecisionNormalizationError,
        match="controller_repair_action_unresolved:primaryAction",
    ):
        controller._repair_feedback(
            raw="{",
            error=json.JSONDecodeError("unterminated", "{", 1),
            contract=contract_service.build(),
            contract_service=contract_service,
            context={
                "tripDatesResolved": True,
                "authoritativeGoalLedger": True,
                "activeVersionId": "ver_active",
            },
            aliases=[],
        )


def test_controller_repairs_untyped_second_clarification_into_exact_semantics():
    class ClarificationRepairProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(
                {
                    "context": autonomy_context,
                    "repair": repair_feedback,
                }
            )
            options = (
                [
                    {
                        "id": "public_view",
                        "label": "公共开放视野",
                        "semanticValue": {
                            "experienceFamilies": ["public_city_view", "waterfront_evening"],
                            "accessPolicy": "public_outdoor",
                            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
                            "timeWindow": {"start": "18:30", "end": "22:00"},
                            "detourTolerance": {
                                "maxGeneralizedCostDelta": 35,
                                "maxDetourRatio": 0.35,
                            },
                            "evidenceFreshness": {
                                "maxAgeHours": 24,
                                "requiredForControlledAccess": True,
                            },
                            "confidence": 0.9,
                        },
                        "allowsManualInput": False,
                    },
                    {
                        "id": "verified_view",
                        "label": "开放状态已核验的观景体验",
                        "semanticValue": {
                            "experienceFamilies": ["verified_viewing_platform"],
                            "accessPolicy": "verified_controlled_access",
                            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
                            "timeWindow": {"start": "19:00", "end": "22:30"},
                            "detourTolerance": {
                                "maxGeneralizedCostDelta": 25,
                                "maxDetourRatio": 0.25,
                            },
                            "evidenceFreshness": {
                                "maxAgeHours": 12,
                                "requiredForControlledAccess": True,
                            },
                            "confidence": 0.95,
                        },
                        "allowsManualInput": False,
                    },
                ]
                if repair_feedback
                else [
                    {
                        "id": "mixed",
                        "label": "公共空间与观景平台均可",
                        "semanticValue": {
                            "experienceFamilies": ["public_city_view"],
                            "accessPolicy": "public_outdoor_or_verified_controlled_access",
                            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
                            "timeWindow": "evening",
                            "detourTolerance": "moderate",
                            "evidenceFreshness": "recent",
                            "confidence": "high",
                        },
                        "allowsManualInput": False,
                    },
                    {
                        "id": "manual",
                        "label": "我自己填写",
                        "semanticValue": None,
                        "allowsManualInput": True,
                    },
                ]
            )
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": {
                    "type": "ask_user",
                    "question": "两晚夜景更偏好公共开放空间，还是开放状态已核验的观景体验？",
                    "dimensionId": "night_view.experience_mode",
                    "whyItMatters": "体验类型会改变候选准入、营业证据和路线。",
                    "allowFreeText": True,
                    "checkpointId": "clarify_existing",
                    "planningRootId": "turn_root",
                    "requestFingerprint": "request-fingerprint",
                    "checkpointFingerprint": "checkpoint-fingerprint",
                    "options": options,
                },
            }

    dimension = {
        "dimensionId": "night_view.experience_mode",
        "status": "unresolved",
        "allowedSemanticFields": [
            "experienceFamilies",
            "accessPolicy",
            "distinctnessPolicy",
            "timeWindow",
            "detourTolerance",
            "evidenceFreshness",
            "confidence",
        ],
    }
    intent_contract = {
        "clarificationRequired": True,
        "clarificationDimensions": [dimension],
    }
    provider = ClarificationRepairProvider()
    result = AgentAutonomyController(provider=provider, planner_service=_Planner()).decide(
        "每晚都安排夜景",
        {"requestIntentContract": intent_contract},
        {
            "schemaVersion": "model-first-autonomy-context-v2",
            "latestUserMessage": "每晚都安排夜景",
            "requestIntentContract": intent_contract,
            "clarificationCheckpoint": {
                "schemaVersion": "clarification-checkpoint-v1",
                "checkpointId": "clarify_existing",
                "planningRootId": "turn_root",
                "requestFingerprint": "request-fingerprint",
                "fingerprint": "checkpoint-fingerprint",
                "status": "answered",
            },
        },
        available_tools=set(),
        runtime_budget_tools=set(),
    )

    assert result.schema_repair_attempts == 1
    assert result.source == "controller"
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert len(provider.calls) == 2
    assert provider.calls[1]["repair"]
    semantic = result.decision.clarification["options"][0]["semanticValue"]
    assert semantic["timeWindow"] == {"start": "18:30", "end": "22:00"}
    assert semantic["confidence"] == 0.9


def test_controller_uses_one_call_server_fallback_above_authoritative_maximum():
    class CardinalityRepairProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(repair_feedback)
            day_two_optional = ["goal_night_view"] if not repair_feedback else []
            schedule_hints = [
                {
                    "goalId": "goal_night_view",
                    "dayNumber": 1,
                    "dayPart": "night",
                    "sequence": 1,
                    "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.8,
                }
            ]
            if day_two_optional:
                schedule_hints.append(
                    {
                        "goalId": "goal_night_view",
                        "dayNumber": 2,
                        "dayPart": "night",
                        "sequence": 1,
                        "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                        "estimateSource": "controller_estimate",
                        "confidence": 0.8,
                    }
                )
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "draft_itinerary",
                "actionDirective": {
                    "type": "draft_itinerary",
                    "goalPriority": ["goal_night_view"],
                    "optionalExperienceBudget": 1,
                    "dayStrategies": [
                        {
                            "dayNumber": 1,
                            "theme": "滨水夜游",
                            "requiredGoalIds": ["goal_night_view"],
                            "requiredGoalCounts": {"goal_night_view": 1},
                            "optionalGoalIds": [],
                        },
                        {
                            "dayNumber": 2,
                            "theme": "高校参观",
                            "requiredGoalIds": [],
                            "requiredGoalCounts": {},
                            "optionalGoalIds": day_two_optional,
                        },
                    ],
                    "occurrenceScheduleHints": schedule_hints,
                },
            }

    night_requirement = {
        "goalId": "goal_night_view",
        "intentType": "night_view",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferredCount": 1,
        "maxCount": 1,
        "allowedDayNumbers": [1, 2],
        "distributionPolicy": "spread_across_distinct_days",
        "cardinalitySource": "explicit_single_evening",
    }
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "只安排一晚街区或滨水夜游，不登塔",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "observation": {
            "request": {"intentContract": {"requiredIntents": [night_requirement]}},
            "requirementCoverage": {"required": [night_requirement]},
        },
    }
    provider = CardinalityRepairProvider()

    result = AgentAutonomyController(
        provider=provider,
        planner_service=_Planner(),
    ).decide(
        "只安排一晚街区或滨水夜游，不登塔",
        {},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.schema_repair_attempts == 0
    assert provider.calls == [""]
    assert result.source == "safe_fallback"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert result.gated_decision.accepted is True
    assert result.decision.action_directive.day_strategies[1].optional_goal_ids == []


def test_deterministic_cardinality_fallback_never_exceeds_authoritative_maximum():
    context = {
        "availableDayNumbers": [1, 2],
        "goalRequirements": [
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requirementLevel": "hard",
                "requiredMin": 1,
                "preferredCount": 2,
                "maxCount": 1,
                "allowedDayNumbers": [1, 2],
            }
        ],
    }

    decision = AgentAutonomyController._deterministic_cardinality_draft_decision(context)

    assert decision is not None
    strategies = decision.action_directive.day_strategies
    assert (
        sum(
            strategy.required_goal_counts.get("goal_night_view", 0)
            + strategy.optional_goal_ids.count("goal_night_view")
            for strategy in strategies
        )
        == 1
    )


def test_controller_uses_one_call_server_fallback_above_declared_optional_budget():
    class OptionalBudgetRepairProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(repair_feedback)
            budget = 2 if repair_feedback else 1
            schedule_hints = [
                {
                    "goalId": goal_id,
                    "dayNumber": day,
                    "dayPart": "morning" if goal_id == "goal_campus" else "noon",
                    "sequence": sequence,
                    "preferredStartTime": "09:00" if goal_id == "goal_campus" else "12:00",
                    "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.8,
                }
                for day in (1, 2)
                for sequence, goal_id in enumerate(("goal_campus", "goal_meal"), start=1)
            ]
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "draft_itinerary",
                "actionDirective": {
                    "type": "draft_itinerary",
                    "goalPriority": ["goal_campus", "goal_meal"],
                    "optionalExperienceBudget": budget,
                    "dayStrategies": [
                        {
                            "dayNumber": day,
                            "theme": "高校与当地午餐",
                            "requiredGoalIds": ["goal_campus"],
                            "requiredGoalCounts": {"goal_campus": 1},
                            "optionalGoalIds": ["goal_meal"],
                        }
                        for day in (1, 2)
                    ],
                    "occurrenceScheduleHints": schedule_hints,
                },
            }

    requirements = [
        {
            "goalId": "goal_campus",
            "intentType": "campus_visit",
            "requirementLevel": "hard",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "allowedDayNumbers": [1, 2],
        },
        {
            "goalId": "goal_meal",
            "intentType": "meal",
            "requirementLevel": "soft_experience",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
        },
    ]
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "两天参观高校，每天体验当地午餐",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "observation": {
            "request": {"intentContract": {"requiredIntents": requirements}},
            "requirementCoverage": {"required": requirements},
        },
    }
    provider = OptionalBudgetRepairProvider()

    result = AgentAutonomyController(
        provider=provider,
        planner_service=_Planner(),
    ).decide(
        "两天参观高校，每天体验当地午餐",
        {},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.schema_repair_attempts == 0
    assert provider.calls == [""]
    assert result.source == "safe_fallback"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert result.gated_decision.accepted is True
    assert result.decision.action_directive.optional_experience_budget == 2


def test_deterministic_cardinality_fallback_reports_actual_optional_occurrence_budget():
    context = {
        "availableDayNumbers": [1, 2],
        "goalRequirements": [
            {
                "goalId": "goal_campus",
                "intentType": "campus_visit",
                "requirementLevel": "hard",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requirementLevel": "soft_experience",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "allowedDayNumbers": [1, 2],
                "distributionPolicy": "every_allowed_day",
            },
        ],
    }

    decision = AgentAutonomyController._deterministic_cardinality_draft_decision(context)

    assert decision is not None
    strategies = decision.action_directive.day_strategies
    optional_occurrences = sum(len(item.optional_goal_ids) for item in strategies)
    assert optional_occurrences == 2
    assert decision.action_directive.optional_experience_budget == optional_occurrences


def test_deterministic_cardinality_fallback_caps_optional_occurrences_at_three():
    context = {
        "availableDayNumbers": [1],
        "goalRequirements": [
            {
                "goalId": "goal_campus",
                "intentType": "campus_visit",
                "requirementLevel": "hard",
                "requiredMin": 1,
                "preferredCount": 1,
                "maxCount": 1,
                "allowedDayNumbers": [1],
            },
            *[
                {
                    "goalId": f"goal_optional_{index}",
                    "intentType": "soft_experience",
                    "requirementLevel": "soft_experience",
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "allowedDayNumbers": [1],
                }
                for index in range(4)
            ],
        ],
    }

    decision = AgentAutonomyController._deterministic_cardinality_draft_decision(context)

    assert decision is not None
    optional_occurrences = sum(len(item.optional_goal_ids) for item in decision.action_directive.day_strategies)
    assert optional_occurrences == 3
    assert decision.action_directive.optional_experience_budget == 3


def _two_day_daily_template_controller_context() -> dict:
    return {
        "availableDayNumbers": [1, 2],
        "requiredPlanningDayNumbers": [1, 2],
        "explicitRestDayNumbers": [],
        "goalRequirements": [
            {
                "goalId": "goal_campus",
                "intentType": "campus_visit",
                "requirementLevel": "required",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "cardinalitySource": "multi_day_daily_template",
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requirementLevel": "required",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "cardinalitySource": "multi_day_daily_template",
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_park",
                "intentType": "park",
                "requirementLevel": "required",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "cardinalitySource": "multi_day_daily_template",
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
        ],
    }


def _daily_template_strategy(day_number: int) -> dict:
    return {
        "dayNumber": day_number,
        "theme": f"Day {day_number} 高校、当地午餐与晚间公园",
        "requiredGoalIds": ["goal_campus", "goal_meal", "goal_park"],
        "requiredGoalCounts": {
            "goal_campus": 1,
            "goal_meal": 1,
            "goal_park": 1,
        },
        "optionalGoalIds": [],
        "pace": "standard",
        "maxRouteAnchors": 4,
    }


def _daily_template_hints(day_numbers: list[int]) -> list[dict]:
    result = []
    for day_number in day_numbers:
        result.extend(
            [
                {
                    "goalId": "goal_campus",
                    "dayNumber": day_number,
                    "dayPart": "morning",
                    "sequence": 1,
                    "preferredStartTime": "09:00",
                    "durationEstimate": {"min": 90, "preferred": 120, "max": 180},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.8,
                },
                {
                    "goalId": "goal_meal",
                    "dayNumber": day_number,
                    "dayPart": "noon",
                    "sequence": 2,
                    "preferredStartTime": "12:00",
                    "durationEstimate": {"min": 45, "preferred": 60, "max": 90},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.8,
                },
                {
                    "goalId": "goal_park",
                    "dayNumber": day_number,
                    "dayPart": "evening",
                    "sequence": 3,
                    "preferredStartTime": "18:30",
                    "durationEstimate": {"min": 45, "preferred": 60, "max": 90},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.8,
                },
            ]
        )
    return result


def _daily_template_provider_decision(day_numbers: list[int]) -> dict:
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus", "goal_meal", "goal_park"],
            "dayStrategies": [_daily_template_strategy(day) for day in day_numbers],
            "optionalExperienceBudget": 0,
            "searchPriority": ["goal_campus", "goal_meal", "goal_park"],
            "occurrenceScheduleHints": _daily_template_hints(day_numbers),
        },
    }


def test_controller_rejects_day_one_only_directive_before_grounding() -> None:
    with pytest.raises(DecisionNormalizationError) as exc_info:
        AgentAutonomyController()._validate_provider_decision(
            _daily_template_provider_decision([1]),
            _two_day_daily_template_controller_context(),
        )

    assert exc_info.value.reason_code == "draft_required_day_coverage_incomplete"
    assert json.loads(exc_info.value.detail) == {"missingDayNumbers": [2]}


def test_controller_accepts_exactly_one_strategy_for_each_required_day() -> None:
    decision = AgentAutonomyController()._validate_provider_decision(
        _daily_template_provider_decision([1, 2]),
        _two_day_daily_template_controller_context(),
    )[0]

    assert [item.day_number for item in decision.action_directive.day_strategies] == [1, 2]


def test_allowed_day_numbers_are_assignment_bounds_not_proof_of_daily_coverage() -> None:
    raw = _daily_template_provider_decision([1, 2])
    day_two = raw["actionDirective"]["dayStrategies"][1]
    day_two["requiredGoalIds"].remove("goal_campus")
    day_two["requiredGoalCounts"].pop("goal_campus")
    raw["actionDirective"]["occurrenceScheduleHints"] = [
        hint
        for hint in raw["actionDirective"]["occurrenceScheduleHints"]
        if not (hint["goalId"] == "goal_campus" and hint["dayNumber"] == 2)
    ]

    with pytest.raises(DecisionNormalizationError) as exc_info:
        AgentAutonomyController()._validate_provider_decision(
            raw,
            _two_day_daily_template_controller_context(),
        )

    assert exc_info.value.reason_code == "draft_required_day_occurrence_coverage_incomplete"
    assert json.loads(exc_info.value.detail) == {
        "goalId": "goal_campus",
        "missingDayNumbers": [2],
    }


def test_controller_allows_zero_target_only_for_explicit_rest_day() -> None:
    context = _two_day_daily_template_controller_context()
    context["requiredPlanningDayNumbers"] = [1]
    context["explicitRestDayNumbers"] = [2]
    for requirement in context["goalRequirements"]:
        requirement["requiredMin"] = 1
        requirement["preferredCount"] = 1
        requirement["maxCount"] = 1
        requirement["allowedDayNumbers"] = [1]

    decision = AgentAutonomyController()._validate_provider_decision(
        _daily_template_provider_decision([1]),
        context,
    )[0]

    assert [item.day_number for item in decision.action_directive.day_strategies] == [1]


def test_deterministic_fallback_rebuilds_every_required_day_from_daily_contract() -> None:
    decision = AgentAutonomyController._deterministic_cardinality_draft_decision(
        _two_day_daily_template_controller_context()
    )

    assert decision is not None
    strategies = decision.action_directive.day_strategies
    assert [item.day_number for item in strategies] == [1, 2]
    assert all(set(item.required_goal_ids) == {"goal_campus", "goal_meal", "goal_park"} for item in strategies)


def test_deterministic_fallback_does_not_treat_explicit_rest_day_as_required_coverage() -> None:
    context = _two_day_daily_template_controller_context()
    context["requiredPlanningDayNumbers"] = [1]
    context["explicitRestDayNumbers"] = [2]
    for requirement in context["goalRequirements"]:
        requirement["requiredMin"] = 1
        requirement["preferredCount"] = 1
        requirement["maxCount"] = 1
        requirement["allowedDayNumbers"] = [1]

    decision = AgentAutonomyController._deterministic_cardinality_draft_decision(context)

    assert decision is not None
    assert [item.day_number for item in decision.action_directive.day_strategies] == [1]


def test_day_one_only_provider_falls_back_without_spending_model_repair_budget() -> None:
    class DayOneOnlyProvider:
        model = "recorded-day-one-only"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(repair_feedback)
            return _daily_template_provider_decision([1])

    contract = _two_day_daily_template_controller_context()
    request_intent_contract = {
        "requiredIntents": contract["goalRequirements"],
        "requiredPlanningDayNumbers": [1, 2],
        "explicitRestDayNumbers": [],
    }
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "北京两日高校、美食与公园行程",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "requestIntentContract": request_intent_contract,
        "observation": {
            "request": {"intentContract": request_intent_contract},
            "requirementCoverage": {"required": contract["goalRequirements"]},
        },
    }
    provider = DayOneOnlyProvider()

    result = AgentAutonomyController(provider=provider).decide(
        "北京两日高校、美食与公园行程",
        {"requestIntentContract": request_intent_contract},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == [""]
    assert result.schema_repair_attempts == 0
    assert result.decision.primary_action == "draft_itinerary"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert [item.day_number for item in result.decision.action_directive.day_strategies] == [1, 2]
    assert all(
        set(item.required_goal_ids) == {"goal_campus", "goal_meal", "goal_park"}
        for item in result.decision.action_directive.day_strategies
    )


def test_pydantic_wrapped_daily_cardinality_error_uses_one_call_server_fallback() -> None:
    class DuplicateDailyGoalProvider:
        model = "recorded-duplicate-daily-goal"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(repair_feedback)
            decision = _daily_template_provider_decision([1, 2])
            decision["actionDirective"]["dayStrategies"][0]["requiredGoalIds"].append("goal_campus")
            return decision

    contract = _two_day_daily_template_controller_context()
    request_intent_contract = {
        "requiredIntents": contract["goalRequirements"],
        "requiredPlanningDayNumbers": [1, 2],
        "explicitRestDayNumbers": [],
    }
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "北京两日高校、美食与公园行程",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "requestIntentContract": request_intent_contract,
        "observation": {
            "request": {"intentContract": request_intent_contract},
            "requirementCoverage": {"required": contract["goalRequirements"]},
        },
    }
    provider = DuplicateDailyGoalProvider()

    result = AgentAutonomyController(provider=provider).decide(
        "北京两日高校、美食与公园行程",
        {"requestIntentContract": request_intent_contract},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == [""]
    assert result.schema_repair_attempts == 0
    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "draft_itinerary"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert [item.day_number for item in result.decision.action_directive.day_strategies] == [1, 2]


def test_normalization_context_preserves_required_days_separately_from_available_days() -> None:
    context = AgentAutonomyController._normalization_context(
        {
            "resolvedTripDates": {
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
            },
            "requestIntentContract": {
                "requiredPlanningDayNumbers": [1],
                "explicitRestDayNumbers": [2],
            },
        }
    )

    assert context["availableDayNumbers"] == [1, 2]
    assert context["requiredPlanningDayNumbers"] == [1]
    assert context["explicitRestDayNumbers"] == [2]


@pytest.mark.parametrize(
    "reason",
    [
        "date_duration_conflict",
        "duration_without_dates",
        "holiday_dates_unspecified",
        "invalid_calendar_date",
        "non_contiguous_explicit_date_list",
        "ambiguous_multiple_explicit_date_groups",
        "ambiguous_national_day_relative_duration",
        "unparseable_explicit_date",
    ],
)
def test_unresolved_canonical_date_contract_stops_before_controller_provider_and_planner(
    reason: str,
) -> None:
    class ProviderMustNotRun:
        model = "must-not-run"

        def __init__(self) -> None:
            self.calls = 0

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            self.calls += 1
            raise AssertionError("date clarification must stop before Controller provider")

    class PlannerMustNotRun:
        def __init__(self) -> None:
            self.calls = 0

        def plan(self, _message, _context):
            self.calls += 1
            raise AssertionError("date clarification must stop before deterministic planning")

    provider = ProviderMustNotRun()
    planner = PlannerMustNotRun()
    resolved_dates = {
        "status": "unresolved",
        "dates": [],
        "reason": reason,
    }

    result = AgentAutonomyController(
        provider=provider,
        planner_service=planner,
    ).decide(
        "国庆玩两天",
        {"resolvedTripDates": resolved_dates},
        {"resolvedTripDates": resolved_dates},
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        deterministic_fast_path=True,
    )

    assert provider.calls == 0
    assert planner.calls == 0
    assert result.decision.primary_action == "ask_user"
    assert result.decision.reason_codes == [reason, "date_contract_clarification_required"]
    assert result.decision.required_tools == []
    assert result.decision.side_effects["itinerary"] is False
    assert result.gated_decision.accepted is True
    assert result.controller_full_called is False
    assert result.controller_lite_called is False


def test_material_tradeoff_observation_contract_repairs_repeated_resolve_to_ask_user():
    class MaterialRepairProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.calls.append(
                {
                    "allowedActions": autonomy_context.get("allowedActions"),
                    "contractRef": autonomy_context.get("decisionContractRef"),
                    "repair": repair_feedback,
                }
            )
            if not repair_feedback:
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "resolve_poi",
                    "actionDirective": {
                        "type": "resolve_poi",
                        "targetGoalId": "goal_campus_visit",
                        "targetSegmentIds": [],
                        "searchIntent": "985高校参观",
                    },
                }
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": {
                    "type": "ask_user",
                    "question": "请选择具体高校。",
                    "choiceIds": ["campus_candidates", "manual"],
                },
            }

    observation = AgentObservationBuilder().build(
        {
            "pendingAmapPoiCandidates": [
                {
                    "id": "cand_campus",
                    "status": "pending",
                    "candidates": [{"id": "B0CAMPUS1"}, {"id": "B0CAMPUS2"}],
                }
            ]
        },
        cycle_index=2,
        last_outcome={
            "action": "resolve_poi",
            "status": "needs_confirmation",
            "candidateSummary": {
                "materialTradeoff": True,
                "pendingCandidateRecordIds": ["cand_campus"],
            },
            "safeToContinue": True,
        },
    )
    provider = MaterialRepairProvider()
    result = AgentAutonomyController(provider=provider, planner_service=_Planner()).decide(
        "今年国庆参观985大学两日游",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"resolve_poi"},
        runtime_budget_tools={"resolve_poi"},
        observation=observation,
    )

    assert result.decision.primary_action == "ask_user"
    assert result.schema_repair_attempts == 1
    assert len(provider.calls) == 2
    assert provider.calls[0]["allowedActions"] == ["ask_user"]
    assert provider.calls[0]["contractRef"]["sha256"]
    repair = json.loads(provider.calls[1]["repair"])
    assert repair["allowedActions"] == ["ask_user"]


def test_material_tradeoff_rejects_lite_finish_and_fails_closed_to_ask_user(monkeypatch):
    class FullTimeoutLiteFinishProvider:
        model = "recorded-controller"

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            raise TimeoutError("controller_decision_timeout")

        def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
            return {
                "schemaVersion": "agent-decision-lite-v1",
                "primaryAction": "finish",
                "confidence": 1.0,
                "reasonCode": "candidate_resolution_complete",
                "userVisibleReason": "候选解析完成。",
            }

    message = "今年国庆参观985大学两日游，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    observation = AgentObservationBuilder().build(
        {
            "latestUserMessage": message,
            "effectiveUserMessage": message,
            "pendingAmapPoiCandidates": [
                {
                    "id": "cand_campus",
                    "status": "pending",
                    "candidates": [{"id": "B0CAMPUS1"}, {"id": "B0CAMPUS2"}],
                }
            ],
        },
        cycle_index=2,
        last_outcome={
            "action": "resolve_poi",
            "status": "needs_confirmation",
            "candidateSummary": {
                "materialTradeoff": True,
                "pendingCandidateRecordIds": ["cand_campus"],
            },
            "safeToContinue": True,
        },
    )

    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="material-tradeoff-test",
    ) as decision_executor:
        monkeypatch.setattr(
            AgentAutonomyController,
            "_decision_executor",
            decision_executor,
        )
        monkeypatch.setattr(
            AgentAutonomyController,
            "_decision_worker_slots",
            BoundedSemaphore(value=4),
        )
        result = AgentAutonomyController(
            provider=FullTimeoutLiteFinishProvider(),
            fallback_decision_resolver=AgentDecisionArbitrator(),
            decision_timeout_seconds=0.2,
            lite_timeout_seconds=0.2,
            total_budget_seconds=0.4,
        ).decide(
            message,
            {},
            {"schemaVersion": "model-first-autonomy-context-v2"},
            available_tools=set(),
            runtime_budget_tools=set(),
            observation=observation,
        )

    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.decision_path == "fallback"
    assert result.controller_lite_succeeded is False
    assert result.controller_failures[-1].stage == "lite"
    assert result.controller_failures[-1].failure_class == "schema_validation_failed"
    assert result.controller_failures[-1].schema_error_summary == (
        "lite_primary_action_not_allowed_by_observation:primaryAction:finish"
    )


def test_healthy_controller_preempts_deterministic_fallback_decision():
    provider = _RepairingProvider()
    deterministic = AgentDecision(
        primaryAction="finish",
        confidence=1.0,
        decisionSummary="fallback only",
        stopCondition={"type": "fallback"},
    )
    result = AgentAutonomyController(provider=provider, planner_service=_Planner()).decide(
        "当前安排是什么",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
        deterministic_decision=deterministic,
    )

    assert result.source == "controller"
    assert result.decision.primary_action == "read_itinerary"
    assert provider.calls


def test_deterministic_fast_path_does_not_call_controller_provider():
    provider = _RepairingProvider()
    result = AgentAutonomyController(provider=provider, planner_service=_Planner()).decide_shadow(
        "把第一天时间改到九点",
        {},
        {},
        available_tools={"read_itinerary", "patch_itinerary"},
        runtime_budget_tools={"read_itinerary", "patch_itinerary"},
        deterministic_fast_path=True,
    )

    assert result.source == "deterministic_fast_path"
    assert provider.calls == []


def test_deterministic_fast_path_skips_controller_projection_for_oversized_goal_maps():
    provider = _RepairingProvider()
    oversized_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "把第一天时间改到九点",
        "requiredGoalCounts": {f"goal_{index}": 1 for index in range(2_000)},
    }

    result = AgentAutonomyController(provider=provider, planner_service=_Planner()).decide_shadow(
        "把第一天时间改到九点",
        {},
        oversized_context,
        available_tools={"read_itinerary", "patch_itinerary"},
        runtime_budget_tools={"read_itinerary", "patch_itinerary"},
        deterministic_fast_path=True,
    )

    assert result.source == "deterministic_fast_path"
    assert provider.calls == []


@pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_deepseek_autonomy_payload_has_no_tools_and_uses_short_deterministic_output(model_name):
    provider = DeepSeekAgentProvider(api_key="test-key", model=model_name, timeout_seconds=30)
    captured = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        captured["timeout"] = timeout_seconds
        return '{"primaryAction":"finish"}'

    provider._post = fake_post
    provider.decide_autonomy({"schemaVersion": "autonomy-context-v1"}, timeout_seconds=2.5)

    assert "tools" not in captured["payload"]
    assert captured["payload"]["model"] == model_name
    assert captured["payload"]["temperature"] == 0.0
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["max_tokens"] == CONTROLLER_FULL_MAX_OUTPUT_TOKENS
    assert captured["timeout"] == 2.5


def _controller_truncation_error(
    *,
    call_kind: str,
    content: str = '{"schemaVersion":"agent-decision-v3"',
) -> ControllerOutputTruncatedError:
    try:
        json.loads(content)
    except json.JSONDecodeError as error:
        category = error.msg
        position = error.pos
    else:
        category = "syntactically_valid"
        position = len(content)
    return ControllerOutputTruncatedError(
        call_kind=call_kind,
        evidence=ControllerResponseIntegrityEvidence(
            finish_reason="length",
            content_length=len(content),
            content_bytes=len(content.encode("utf-8")),
            response_bytes=len(content.encode("utf-8")) + 128,
            parse_error_category=category,
            parse_error_position=position,
        ),
    )


@pytest.mark.parametrize(
    "content",
    [
        '{"schemaVersion":"agent-decision-v3","primaryAction":"draft_itinerary",',
        json.dumps(
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {"type": "finish", "assistantReply": "done"},
            }
        ),
    ],
    ids=["incomplete-json", "syntactically-valid-json"],
)
def test_controller_transport_rejects_finish_reason_length_without_guessing(content):
    provider = DeepSeekAgentProvider(api_key="test-key", model="deepseek-chat", timeout_seconds=30)
    shared = {"responseBytes": len(content.encode("utf-8")) + 128}
    provider.prepare_controller_performance({}, shared, call_kind="full")
    provider._post_json = lambda *_args, **_kwargs: {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": content, "reasoning_content": ""},
            }
        ]
    }

    with pytest.raises(ControllerOutputTruncatedError) as raised:
        provider._post({"messages": []}, timeout_seconds=1.0)

    assert raised.value.call_kind == "full"
    assert raised.value.evidence.finish_reason == "length"
    assert shared["responseIntegrity"] == "truncated"
    assert "content" not in raised.value.to_safe_dict()


def test_length_truncated_full_response_uses_one_content_free_repair_and_succeeds():
    class TruncationThenRepairProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls: list[dict] = []

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            feedback = json.loads(repair_feedback) if repair_feedback else {}
            self.calls.append({"timeout": timeout_seconds, "feedback": feedback})
            if not repair_feedback:
                raise _controller_truncation_error(call_kind="full")
            assert feedback["repairMode"] == "full_response_truncation_reissue"
            assert "action" not in feedback
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "read_itinerary",
                "actionDirective": {"type": "read_itinerary", "queryType": "timeline_summary"},
            }

    provider = TruncationThenRepairProvider()
    result = AgentAutonomyController(provider=provider).decide_shadow(
        "current itinerary",
        {},
        {},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    assert len(provider.calls) == 2
    assert result.source == "controller"
    assert result.schema_repair_attempts == 1
    assert result.decision.primary_action == "read_itinerary"
    assert result.controller_full_succeeded is True
    assert result.controller_performance[0]["captureState"] == "response_rejected"
    assert result.controller_performance[0]["responseIntegrity"] == "truncated"
    assert result.provider_raw_decisions[0]["truncated"] is True
    assert "content" not in result.provider_raw_decisions[0]


@pytest.mark.parametrize(
    ("repair_failure", "expected_class"),
    [
        ("truncated", "output_truncated"),
        ("provider", "provider_unavailable"),
        ("schema", "schema_validation_failed"),
    ],
)
def test_repair_failure_is_terminal_and_never_enters_lite_or_server_draft(
    repair_failure,
    expected_class,
):
    class RepairFailureProvider:
        model = "recorded-controller"

        def __init__(self):
            self.calls: list[str] = []
            self.lite_calls = 0

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            self.calls.append("repair" if repair_feedback else "full")
            if not repair_feedback:
                return {
                    "primaryAction": "read_itinerary",
                    "actionDirective": {"type": "finish"},
                }
            if repair_failure == "truncated":
                raise _controller_truncation_error(call_kind="repair")
            if repair_failure == "provider":
                raise HTTPException(status_code=502, detail="recorded provider failure")
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "invalid",
                "actionDirective": {"type": "finish", "assistantReply": "must not pass"},
            }

        def decide_autonomy_lite(self, _context, *, timeout_seconds):
            self.lite_calls += 1
            raise AssertionError("repair failure must not enter Lite")

    provider = RepairFailureProvider()
    result = AgentAutonomyController(
        provider=provider,
        fallback_decision_resolver=AgentDecisionArbitrator(),
    ).decide_shadow(
        "current itinerary",
        {},
        {},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    assert provider.calls == ["full", "repair"]
    assert provider.lite_calls == 0
    assert result.schema_repair_attempts == 1
    assert result.controller_full_succeeded is False
    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.controller_failures[-1].stage == "repair"
    assert result.controller_failures[-1].failure_class == expected_class
    assert "deterministic_cardinality_fallback" not in result.decision.reason_codes


def test_truncation_repair_request_has_separate_bounded_input_and_output_budget():
    provider = DeepSeekAgentProvider(api_key="test-key", model="deepseek-chat", timeout_seconds=30)
    captured = {}
    shared = {}
    provider.prepare_controller_performance({}, shared, call_kind="repair")
    provider._post = lambda payload, **_kwargs: captured.setdefault("payload", payload) or "{}"
    contract = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=("ask_user", "draft_itinerary"),
    ).build()
    repair_feedback = AgentAutonomyController._truncation_repair_feedback(
        error=_controller_truncation_error(call_kind="full"),
        contract=contract,
    )

    provider.decide_autonomy(
        {
            "schemaVersion": "controller-context-full-v1",
            "latestUserMessage": "bounded request",
            "itineraryLifecycle": {"state": "empty_scaffold"},
            "allowedActions": ["ask_user", "draft_itinerary"],
            "routePlanningPolicyRequirement": {
                "required": True,
                "source": "controller_estimate",
                "missingFields": ["detourTolerance", "mobilityProfile"],
                "mobilityProfile": {
                    "required": True,
                    "fields": ["transportMode", "paceClass"],
                },
                "detourEnvelope": {
                    "required": True,
                    "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
                },
            },
            "fingerprints": {},
            "decisionContractRef": {"version": "agent-decision-contract-v3"},
            "observation": {"mustBeDropped": "x" * 30_000},
        },
        timeout_seconds=2.5,
        repair_feedback=json.dumps(repair_feedback, ensure_ascii=False),
    )

    payload = captured["payload"]
    projected_context = json.loads(payload["messages"][1]["content"])
    assert "observation" not in projected_context
    assert projected_context["repairMode"] == "full_response_truncation_reissue"
    assert projected_context["routePlanningPolicyRequirement"]["missingFields"] == [
        "detourTolerance",
        "mobilityProfile",
    ]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT
    assert payload["max_tokens"] == CONTROLLER_FULL_MAX_OUTPUT_TOKENS
    assert shared["requestByteLimit"] == FULL_REQUEST_BYTE_LIMIT
    assert shared["reservedOutputTokens"] == CONTROLLER_FULL_MAX_OUTPUT_TOKENS
    assert shared["maxOutputTokens"] == CONTROLLER_FULL_MAX_OUTPUT_TOKENS


def test_unknown_action_repair_over_request_budget_fails_before_provider_call():
    provider = DeepSeekAgentProvider(api_key="test-key", model="deepseek-chat", timeout_seconds=30)
    provider._post = lambda *_args, **_kwargs: pytest.fail("oversized repair must not call provider")

    with pytest.raises(ValueError, match="controller_repair_payload_too_large"):
        provider.decide_autonomy(
            {
                "schemaVersion": "controller-context-full-v1",
                "allowedActions": ["read_itinerary", "finish"],
                "unboundedDiagnostic": "x" * 30_000,
            },
            timeout_seconds=2.5,
            repair_feedback=json.dumps({"action": ""}),
        )

    failure = classify_controller_failure(
        ValueError("controller_repair_payload_too_large"),
        stage="repair",
        provider="DeepSeekAgentProvider",
        model="deepseek-chat",
        duration_ms=0,
        timeout_seconds=2.5,
    )
    assert failure.failure_class == "request_budget_exceeded"
    assert failure.retryable is False


@pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_trace_shaped_draft_schema_repair_stays_within_full_payload_budget(model_name):
    goals = [
        {
            "goalId": "goal_campus_visit",
            "intentType": "campus_visit",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "user_explicit_every_day",
        },
        {
            "goalId": "goal_evening_park",
            "intentType": "park",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "user_explicit_every_day",
        },
        {
            "goalId": "goal_daily_meal",
            "intentType": "meal",
            "requiredMin": 0,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "soft_experience",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "user_explicit_every_day",
        },
    ]
    repair = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=PRIMARY_ACTION_VALUES,
    ).repair_payload(
        action="draft_itinerary",
        invalid_paths=["actionDirective"],
        allowed_ids={"goalIds": [item["goalId"] for item in goals]},
        aliases_applied=[],
        goal_requirements=goals,
    )
    provider = DeepSeekAgentProvider(api_key="test-key", model=model_name, timeout_seconds=30)
    captured = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        return json.dumps(repair["minimalExample"], ensure_ascii=False)

    provider._post = fake_post
    response = provider.decide_autonomy(
        {
            "schemaVersion": "model-first-autonomy-context-v2",
            "latestUserMessage": (
                "今年国庆参观北京高校两日游，每天晚上逛公园。10月1日到2日，中等预算，1人，"
                "公交地铁优先。每天午餐想体验北京当地特色美食。"
            ),
            "selectedCity": "北京",
            "itineraryLifecycle": "empty_scaffold",
            "allowedActions": ["ask_user", "draft_itinerary"],
            "fingerprints": {},
            "decisionConstraints": {},
            "decisionContractRef": {},
            "goalRequirements": goals,
        },
        timeout_seconds=2.5,
        repair_feedback=json.dumps(repair, ensure_ascii=False),
    )

    assert json.loads(response)["primaryAction"] == "draft_itinerary"
    assert captured["payload"]["model"] == model_name
    assert len(json.dumps(captured["payload"], ensure_ascii=False).encode("utf-8")) <= 15_360


def test_v4_route_ask_repair_final_http_payload_stays_within_full_request_limit():
    """V4 bundle projection plus compact ask repair must fit the final HTTP envelope."""

    goals = [
        {
            "goalId": "goal_campus_visit",
            "intentType": "campus_visit",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "user_explicit_every_day",
        },
        {
            "goalId": "goal_evening_park",
            "intentType": "park",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "user_explicit_every_day",
        },
        {
            "goalId": "goal_daily_meal",
            "intentType": "meal",
            "requiredMin": 0,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "soft_experience",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "user_explicit_every_day",
        },
    ]
    route_requirement = {
        "required": True,
        "source": "controller_estimate",
        "missingFields": ["detourTolerance", "mobilityProfile"],
        "mobilityProfile": {
            "required": True,
            "fields": ["transportMode", "paceClass"],
        },
        "detourEnvelope": {
            "required": True,
            "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
        },
    }
    repair = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=PRIMARY_ACTION_VALUES,
    ).repair_payload(
        action="ask_user",
        invalid_paths=[
            "actionDirective.questions.0.options",
            "actionDirective.questions.1.options.0.semanticValue",
        ],
        allowed_ids={"goalIds": [item["goalId"] for item in goals]},
        aliases_applied=[],
        goal_requirements=goals,
        route_policy_requirement=route_requirement,
    )
    forbidden_draft_keys = {
        "goalRequirements",
        "requiredGoalCounts",
        "goalCardinality",
        "optionalGoalIds",
        "draftSchedulingRules",
        "routePlanningPolicyRequirement",
    }
    assert forbidden_draft_keys.isdisjoint(repair)
    assert repair["minimalExample"]["primaryAction"] == "ask_user"

    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=30,
    )
    captured = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        return json.dumps(repair["minimalExample"], ensure_ascii=False)

    provider._post = fake_post
    response = provider.decide_autonomy(
        {
            "schemaVersion": "controller-context-full-v1",
            "latestUserMessage": (
                "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，"
                "中午品尝当地网红美食，晚上去附近的公园逛一下"
            ),
            "selectedCity": "北京",
            "itineraryLifecycle": {
                "state": "empty_scaffold",
                "activeVersionId": None,
                "dayCount": 0,
                "segmentCount": 0,
            },
            "allowedActions": list(PRIMARY_ACTION_VALUES),
            "fingerprints": {
                "requestIntentContractFingerprint": "647811364c08ec763902c1512c17bfb4",
                "observationFingerprint": "381eb99c43df59383f1cf1cc",
            },
            "decisionConstraints": {
                "allWritesVersioned": True,
                "finalPoiMustBeAmapGrounded": True,
                "askOnlyWhenMaterialChoiceRemains": True,
            },
            "decisionContractRef": {
                "version": "agent-decision-contract-v3",
                "hash": "v4-debug-bundle-projection",
            },
            "goalRequirements": goals,
            "routePlanningPolicyRequirement": route_requirement,
            "clarificationDimensions": [
                {
                    "dimensionId": "route_decision.mobility_profile",
                    "intentType": "route_decision",
                    "status": "unresolved",
                    "impactCode": "route_mobility_profile_required",
                    "candidateScope": {
                        "routeDecisionContractFingerprint": (
                            "647811364c08ec763902c1512c17bfb4cb5f5e9236e07708e93e95836552f46c"
                        ),
                        "missingFields": ["detourTolerance", "mobilityProfile"],
                        "transportMode": "",
                    },
                    "allowedSemanticFields": ["mobilityProfile"],
                    "semanticFieldSchemas": ClarificationCheckpointService.semantic_field_schemas(["mobilityProfile"]),
                },
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "intentType": "route_decision",
                    "status": "unresolved",
                    "impactCode": "provider_route_matrix_acceptance_threshold",
                    "candidateScope": {
                        "routeDecisionContractFingerprint": (
                            "647811364c08ec763902c1512c17bfb4cb5f5e9236e07708e93e95836552f46c"
                        ),
                        "missingFields": ["detourTolerance", "mobilityProfile"],
                        "transportMode": "",
                    },
                    "allowedSemanticFields": [
                        "detourTolerance",
                        "adjacentLegConstraint",
                    ],
                    "semanticFieldSchemas": ClarificationCheckpointService.semantic_field_schemas(
                        ["detourTolerance", "adjacentLegConstraint"]
                    ),
                },
            ],
            "candidateGapSummary": {
                "missingRouteDecisionFields": ["mobilityProfile", "detourTolerance"],
                "pendingCandidateCount": 0,
            },
        },
        timeout_seconds=2.5,
        repair_feedback=json.dumps(repair, ensure_ascii=False),
    )

    final_http_payload = captured["payload"]
    assert json.loads(response)["primaryAction"] == "ask_user"
    assert set(final_http_payload) == {
        "model",
        "messages",
        "temperature",
        "thinking",
        "response_format",
        "max_tokens",
    }
    assert len(final_http_payload["messages"]) == 3
    assert final_http_payload["messages"][0] == {
        "role": "system",
        "content": AUTONOMY_DECISION_SYSTEM_PROMPT,
    }
    projected_context = json.loads(final_http_payload["messages"][1]["content"])
    assert projected_context["repairMode"] == "clarification_schema_only"
    assert len(projected_context["clarificationDimensions"]) == 2
    projected_mobility = projected_context["clarificationDimensions"][0]["semanticFieldSchemas"]["mobilityProfile"]
    assert projected_mobility["properties"]["transportMode"]["enum"] == [
        "transit",
        "public_transit",
        "driving",
        "walking",
        "bicycling",
    ]
    assert projected_mobility["properties"]["paceClass"]["enum"] == [
        "relaxed",
        "standard",
        "intensive",
    ]
    assert forbidden_draft_keys.isdisjoint(projected_context)
    assert "Repair contract:" in final_http_payload["messages"][2]["content"]
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT


def test_v4_post_clarification_draft_repair_final_http_payload_stays_within_full_request_limit():
    """The real post-clarification draft repair shape must fit the complete HTTP envelope."""

    goals = [
        {
            "goalId": "goal_campus_visit",
            "intentType": "campus_visit",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "simple_open_explicit_trip_theme_every_day",
        },
        {
            "goalId": "goal_park",
            "intentType": "park",
            "requiredMin": 1,
            "preferredCount": 1,
            "maxCount": 1,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "one_of_allowed_days",
            "cardinalitySource": "explicit_singular",
        },
        {
            "goalId": "goal_meal",
            "intentType": "meal",
            "requiredMin": 1,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "soft_experience",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "spread_across_distinct_days",
            "cardinalitySource": "explicit_user_request",
        },
    ]
    route_requirement = {
        "required": True,
        "source": "controller_estimate",
        "missingFields": ["detourTolerance", "mobilityProfile"],
        "mobilityProfile": {
            "required": True,
            "fields": ["transportMode", "paceClass"],
        },
        "detourEnvelope": {
            "required": True,
            "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
        },
    }
    allowed_ids = {
        "segmentIds": [],
        "versionIds": [],
        "candidateIds": [],
        "amapPoiIds": [],
        "goalIds": [item["goalId"] for item in goals],
    }
    repair = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=PRIMARY_ACTION_VALUES,
    ).repair_payload(
        action="draft_itinerary",
        invalid_paths=[
            "actionDirective.draft_itinerary.dayStrategies[].goalOccurrences",
            "actionDirective.draft_itinerary.occurrenceScheduleHints[].dayPart",
            "actionDirective.draft_itinerary.occurrenceScheduleHints[].sequence",
            "actionDirective.draft_itinerary.occurrenceScheduleHints[].durationEstimate",
            "actionDirective.draft_itinerary.occurrenceScheduleHints[].confidence",
            "actionDirective.draft_itinerary.occurrenceScheduleHints[].occurrenceIndex",
            "actionDirective.draft_itinerary.occurrenceScheduleHints[].timeOfDay",
            "actionDirective.draft_itinerary.searchGroupingStrategy",
        ],
        allowed_ids=allowed_ids,
        aliases_applied=[],
        goal_requirements=goals,
        route_policy_requirement=route_requirement,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=30,
    )
    captured: dict[str, dict] = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        return json.dumps(repair["minimalExample"], ensure_ascii=False)

    provider._post = fake_post
    response = provider.decide_autonomy(
        {
            "schemaVersion": "controller-context-full-v1",
            "latestUserMessage": "确认并开始规划",
            "selectedCity": "北京",
            "itineraryLifecycle": {
                "state": "empty_scaffold",
                "activeVersionId": None,
                "meaningfulSegmentCount": 0,
                "planningAttemptPersisted": False,
                "cycleIndex": 0,
            },
            "allowedActions": list(PRIMARY_ACTION_VALUES),
            "fingerprints": {
                "requestIntentContractFingerprint": "647811364c08ec763902c1512c17bfb4",
                "observationFingerprint": "4a37488dcef696209bce48b4",
            },
            "decisionConstraints": {
                "allWritesVersioned": True,
                "finalPoiMustBeAmapGrounded": True,
                "askOnlyWhenMaterialChoiceRemains": True,
            },
            "decisionContractRef": {
                "version": "agent-decision-contract-v3",
                "hash": "post-clarification-v4-projection",
            },
            "goalRequirements": goals,
            "routePlanningPolicyRequirement": route_requirement,
        },
        timeout_seconds=2.5,
        repair_feedback=json.dumps(repair, ensure_ascii=False),
    )

    final_http_payload = captured["payload"]
    assert json.loads(response)["primaryAction"] == "draft_itinerary"
    assert len(final_http_payload["messages"]) == 3
    assert final_http_payload["messages"][0] == {
        "role": "system",
        "content": AUTONOMY_DECISION_SYSTEM_PROMPT,
    }
    projected_context = json.loads(final_http_payload["messages"][1]["content"])
    assert projected_context["repairMode"] == "draft_itinerary_schema_only"
    assert "allowedActions" not in projected_context
    assert "Repair contract:" in final_http_payload["messages"][2]["content"]
    repair_contract = json.loads(final_http_payload["messages"][2]["content"].split("Repair contract: ", 1)[1])
    assert repair_contract["allowedActions"] == repair["allowedActions"]
    assert {"ask_user", "draft_itinerary"}.issubset(set(repair_contract["allowedActions"]))
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT


def test_live_12_repeated_schema_paths_and_full_goal_metadata_fit_one_repair_request():
    """Live -12: bounded repair must survive repeated indexed schema failures."""

    goals = [
        {
            "goalId": "goal_campus_visit",
            "intentType": "campus_visit",
            "target": 2,
            "requiredMin": 2,
            "minCount": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "source": "simple_open_explicit_trip_theme_every_day",
            "cardinalitySource": "simple_open_explicit_trip_theme_every_day",
            "distributionPolicy": "every_allowed_day",
            "allowedDayNumbers": [1, 2],
            "priorityTier": "hard",
            "userExplicit": True,
        },
        {
            "goalId": "goal_night_view",
            "intentType": "night_view",
            "target": 1,
            "requiredMin": 1,
            "minCount": 1,
            "preferredCount": 1,
            "maxCount": 1,
            "requirementLevel": "required",
            "source": "explicit_user_clarification",
            "cardinalitySource": "explicit_user_clarification",
            "distributionPolicy": "spread_across_distinct_days",
            "allowedDayNumbers": [1, 2],
            "priorityTier": "hard",
            "userExplicit": True,
            "schedulePreference": {
                "dayPart": "evening",
                "intentType": "night_view",
                "preferredDayNumbers": [1, 2],
                "userExplicit": True,
                "priority": "hard",
                "sourceGoalId": "goal_night_view",
            },
            "timeWindow": {"start": "19:00", "end": "22:00"},
            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
            "accessPolicy": "public_outdoor_or_verified_controlled_access",
            "detourTolerance": {
                "maxGeneralizedCostDelta": 35,
                "maxDetourRatio": 0.35,
            },
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForControlledAccess": True,
            },
            "confidence": 0.9,
        },
        {
            "goalId": "goal_meal",
            "intentType": "meal",
            "target": 2,
            "requiredMin": 2,
            "minCount": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "soft_experience",
            "source": "explicit_every_day",
            "cardinalitySource": "explicit_every_day",
            "distributionPolicy": "every_allowed_day",
            "allowedDayNumbers": [1, 2],
            "priorityTier": "explicit_soft",
            "userExplicit": True,
            "schedulePreference": {
                "dayPart": "noon",
                "intentType": "meal",
                "preferredDayNumbers": [1, 2],
                "userExplicit": True,
                "priority": "explicit_soft",
                "sourceGoalId": "goal_meal",
            },
            "timeWindow": {"start": "12:00", "end": "13:15"},
            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
            "accessPolicy": "verified_amap_food_service",
            "detourTolerance": {
                "maxGeneralizedCostDelta": 35.0,
                "maxDetourRatio": 0.35,
            },
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForControlledAccess": False,
                "requiredForPublicOutdoor": False,
                "allowExplicitNoClosure": False,
            },
            "confidence": 0.84,
        },
    ]
    invalid_paths = [
        "actionDirective.draft_itinerary.dayStrategies.0.theme",
        "actionDirective.draft_itinerary.dayStrategies.0.goalOccurrences",
        "actionDirective.draft_itinerary.dayStrategies.1.theme",
        "actionDirective.draft_itinerary.dayStrategies.1.goalOccurrences",
        *[
            f"actionDirective.draft_itinerary.occurrenceScheduleHints.{index}.{field}"
            for index in range(5)
            for field in (
                "dayPart",
                "sequence",
                "durationEstimate",
                "estimateSource",
                "confidence",
                "timeOfDay",
            )
        ],
    ]
    repair = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=PRIMARY_ACTION_VALUES,
    ).repair_payload(
        action="draft_itinerary",
        invalid_paths=invalid_paths,
        allowed_ids={"goalIds": [item["goalId"] for item in goals]},
        aliases_applied=[],
        goal_requirements=goals,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=30,
    )
    captured = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        return json.dumps(repair["minimalExample"], ensure_ascii=False)

    provider._post = fake_post
    response = provider.decide_autonomy(
        {
            "schemaVersion": "model-first-autonomy-context-v2",
            "latestUserMessage": "今年国庆参观北京高校两日游，晚上看北京夜景。",
            "selectedCity": "北京",
            "itineraryLifecycle": {"state": "empty_scaffold"},
            "allowedActions": list(PRIMARY_ACTION_VALUES),
            "fingerprints": {},
            "decisionConstraints": {
                "requiredGoalCounts": {
                    "goal_campus_visit": 2,
                    "goal_night_view": 1,
                },
                "optionalGoalIds": ["goal_meal"],
                "availableDayNumbers": [1, 2],
            },
            "decisionContractRef": {"version": "agent-decision-contract-v3"},
            "goalRequirements": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "goalId",
                        "intentType",
                        "requirementLevel",
                        "requiredMin",
                        "preferredCount",
                        "maxCount",
                        "distributionPolicy",
                        "allowedDayNumbers",
                    }
                }
                for item in goals
            ],
        },
        timeout_seconds=2.5,
        repair_feedback=json.dumps(repair, ensure_ascii=False),
    )

    assert json.loads(response)["primaryAction"] == "draft_itinerary"
    assert len(json.dumps(captured["payload"], ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT
    assert repair["invalidPaths"] == [
        "actionDirective.draft_itinerary.dayStrategies[].theme",
        "actionDirective.draft_itinerary.dayStrategies[].goalOccurrences",
        "actionDirective.draft_itinerary.occurrenceScheduleHints[].dayPart",
        "actionDirective.draft_itinerary.occurrenceScheduleHints[].sequence",
        "actionDirective.draft_itinerary.occurrenceScheduleHints[].durationEstimate",
        "actionDirective.draft_itinerary.occurrenceScheduleHints[].estimateSource",
        "actionDirective.draft_itinerary.occurrenceScheduleHints[].confidence",
        "actionDirective.draft_itinerary.occurrenceScheduleHints[].timeOfDay",
    ]
    assert all(
        set(item)
        <= {
            "goalId",
            "intentType",
            "requirementLevel",
            "requiredMin",
            "preferredCount",
            "maxCount",
            "cardinalitySource",
            "distributionPolicy",
            "allowedDayNumbers",
        }
        for item in repair["goalRequirements"]
    )


@pytest.mark.parametrize("model_name", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_trace_shaped_legacy_draft_without_route_policy_repairs_to_controller_route_estimate(model_name):
    class TraceRepairProvider:
        def __init__(self):
            self.model = model_name
            self.calls: list[dict] = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            feedback = json.loads(repair_feedback) if repair_feedback else {}
            self.calls.append({"context": autonomy_context, "feedback": feedback})
            if not repair_feedback:
                assert autonomy_context["routePlanningPolicyRequirement"] == {
                    "required": True,
                    "source": "controller_estimate",
                    "missingFields": ["detourTolerance", "mobilityProfile"],
                    "mobilityProfile": {
                        "required": True,
                        "fields": ["transportMode", "paceClass"],
                    },
                    "detourEnvelope": {
                        "required": True,
                        "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
                    },
                }
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "draft_itinerary",
                    "actionDirective": {
                        "goalRequirements": [{"goalId": "goal_campus"}],
                        "decisionConstraints": {"pace": "relaxed"},
                        "searchStrategy": {"mode": "candidate_first"},
                    },
                }
            assert feedback["action"] == "draft_itinerary"
            assert feedback["routePlanningPolicyRequirement"] == autonomy_context["routePlanningPolicyRequirement"]
            return feedback["minimalExample"]

    context = _authoritative_two_day_autonomy_context()
    route_dimensions = [
        {
            "dimensionId": "route_decision.mobility_profile",
            "intentType": "route_decision",
            "status": "unresolved",
            "allowedSemanticFields": ["mobilityProfile"],
        },
        {
            "dimensionId": "route_decision.detour_tolerance",
            "intentType": "route_decision",
            "status": "unresolved",
            "allowedSemanticFields": ["detourTolerance"],
        },
    ]
    intent_contract = context["observation"]["request"]["intentContract"]
    intent_contract["clarificationDimensions"] = route_dimensions
    intent_contract["routeDecisionContract"] = {
        "schemaVersion": "route-decision-contract-v1",
        "status": "awaiting_clarification",
        "missingFields": ["mobilityProfile", "detourTolerance"],
        "mobilityProfile": None,
        "detourTolerance": None,
        "provenance": {"mobilitySensitive": False},
        "detourToleranceSource": "missing",
    }
    context["requestIntentContract"] = intent_contract
    context["serverExecutionProfile"] = "simple_open_v1"
    provider = TraceRepairProvider()
    request_context = {
        "serverExecutionProfile": "simple_open_v1",
        "requestIntentContract": intent_contract,
    }

    result = AgentAutonomyController(
        provider=provider,
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游，公交地铁优先",
        request_context,
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert len(provider.calls) == 2
    assert provider.calls[1]["feedback"]["action"] == "draft_itinerary"
    assert result.schema_repair_attempts == 1
    assert result.source == "controller"
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True
    route_policy = result.decision.action_directive.route_planning_policy
    assert route_policy.source == "controller_estimate"
    assert route_policy.mobility_profile.transport_mode == "transit"
    assert route_policy.mobility_profile.pace_class == "standard"
    assert route_policy.detour_envelope.max_generalized_cost_delta == 30
    assert route_policy.detour_envelope.max_detour_ratio == 0.3


def test_live_shaped_valid_draft_with_null_route_policy_repairs_before_policy_gate():
    requirement = {
        "goalId": "goal_campus",
        "intentType": "campus_visit",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferredCount": 2,
        "maxCount": 2,
        "allowedDayNumbers": [1, 2],
        "distributionPolicy": "spread_across_distinct_days",
    }
    initial_directive = AgentDecisionContractService.deterministic_draft_directive([requirement])
    initial_directive["routePlanningPolicy"] = None

    class NullRoutePolicyRepairProvider:
        model = "recorded-live-null-route-policy"

        def __init__(self) -> None:
            self.feedback: dict = {}

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            if not repair_feedback:
                assert autonomy_context["routePlanningPolicyRequirement"]["required"] is True
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "draft_itinerary",
                    "actionDirective": initial_directive,
                }
            self.feedback = json.loads(repair_feedback)
            assert self.feedback["invalidPaths"] == ["actionDirective.routePlanningPolicy"]
            return self.feedback["minimalExample"]

    context = _authoritative_two_day_autonomy_context()
    context["observation"]["request"]["intentContract"] = {
        "requiredIntents": [requirement],
        "clarificationRequired": True,
        "clarificationDimensions": [
            {
                "dimensionId": "route_decision.mobility_profile",
                "intentType": "route_decision",
                "status": "unresolved",
                "allowedSemanticFields": ["mobilityProfile"],
            },
            {
                "dimensionId": "route_decision.detour_tolerance",
                "intentType": "route_decision",
                "status": "unresolved",
                "allowedSemanticFields": ["detourTolerance"],
            },
        ],
        "routeDecisionContract": {
            "schemaVersion": "route-decision-contract-v1",
            "status": "awaiting_clarification",
            "missingFields": ["mobilityProfile", "detourTolerance"],
            "mobilityProfile": None,
            "detourTolerance": None,
            "provenance": {"mobilitySensitive": False},
        },
    }
    context["requestIntentContract"] = context["observation"]["request"]["intentContract"]
    context["serverExecutionProfile"] = "simple_open_v1"
    request_context = {
        "serverExecutionProfile": "simple_open_v1",
        "requestIntentContract": context["requestIntentContract"],
    }
    provider = NullRoutePolicyRepairProvider()

    result = AgentAutonomyController(provider=provider).decide(
        "北京高校两日游，路线由你安排",
        request_context,
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.source == "controller"
    assert result.schema_repair_attempts == 1
    assert result.gated_decision.accepted is True
    assert result.decision.action_directive.route_planning_policy.source == "controller_estimate"
    assert provider.feedback["routePlanningPolicyRequirement"]["missingFields"] == [
        "detourTolerance",
        "mobilityProfile",
    ]


def test_ready_route_contract_null_policy_still_repairs_to_typed_controller_estimate():
    """A resolved route contract does not make the draft directive policy optional."""

    requirement = {
        "goalId": "goal_campus",
        "intentType": "campus_visit",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferredCount": 2,
        "maxCount": 2,
        "allowedDayNumbers": [1, 2],
        "distributionPolicy": "spread_across_distinct_days",
    }
    initial_directive = AgentDecisionContractService.deterministic_draft_directive([requirement])
    initial_directive["routePlanningPolicy"] = None

    class ReadyRoutePolicyRepairProvider:
        model = "recorded-live-ready-route-policy"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            feedback = json.loads(repair_feedback) if repair_feedback else {}
            self.calls.append(
                {
                    "context": autonomy_context,
                    "feedback": feedback,
                }
            )
            if not repair_feedback:
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "draft_itinerary",
                    "actionDirective": initial_directive,
                }
            assert feedback["action"] == "draft_itinerary"
            assert feedback["routePlanningPolicyRequirement"] == {
                "required": True,
                "source": "controller_estimate",
                "missingFields": ["detourTolerance", "mobilityProfile"],
                "mobilityProfile": {
                    "required": True,
                    "fields": ["transportMode", "paceClass"],
                },
                "detourEnvelope": {
                    "required": True,
                    "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
                },
            }
            assert feedback["minimalExample"]["primaryAction"] == "draft_itinerary"
            return feedback["minimalExample"]

    ready_route_contract = {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        "mobilityProfile": {
            "transportMode": "transit",
            "paceClass": "standard",
        },
        "detourTolerance": {
            "maxGeneralizedCostDelta": 15,
            "maxDetourRatio": 0.15,
        },
    }
    context = _authoritative_two_day_autonomy_context()
    context["observation"]["request"]["intentContract"] = {
        "requiredIntents": [requirement],
        "clarificationRequired": False,
        "routeDecisionContract": ready_route_contract,
    }
    context["requestIntentContract"] = context["observation"]["request"]["intentContract"]
    context["serverExecutionProfile"] = "simple_open_v1"
    request_context = {
        "serverExecutionProfile": "simple_open_v1",
        "requestIntentContract": context["requestIntentContract"],
    }
    provider = ReadyRoutePolicyRepairProvider()

    result = AgentAutonomyController(provider=provider).decide(
        "北京高校两日游，路线偏好已经确认",
        request_context,
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert len(provider.calls) == 2
    assert result.schema_repair_attempts == 1
    assert result.decision.primary_action == "draft_itinerary"
    policy = result.decision.action_directive.route_planning_policy
    assert policy.source == "controller_estimate"
    assert policy.mobility_profile.transport_mode == "transit"
    assert policy.mobility_profile.pace_class == "standard"
    assert policy.detour_envelope.max_generalized_cost_delta == 30
    assert policy.detour_envelope.max_detour_ratio == 0.3


def test_deepseek_repair_projection_preserves_route_policy_requirement():
    repair = {
        "contractVersion": "agent-decision-contract-v3",
        "action": "draft_itinerary",
        "routePlanningPolicyRequirement": {
            "required": True,
            "source": "controller_estimate",
            "missingFields": ["detourTolerance", "mobilityProfile"],
        },
        "minimalExample": {"schemaVersion": "agent-decision-v3"},
        "unsafeDiagnostic": "must-not-forward",
    }

    bounded = json.loads(
        DeepSeekAgentProvider._bounded_controller_repair_feedback(json.dumps(repair, ensure_ascii=False))
    )

    assert bounded["routePlanningPolicyRequirement"] == repair["routePlanningPolicyRequirement"]
    assert "unsafeDiagnostic" not in bounded


def test_route_policy_repair_stays_within_existing_controller_payload_budget():
    goals = [
        {
            "goalId": "goal_campus_visit",
            "intentType": "campus_visit",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
        },
        {
            "goalId": "goal_park",
            "intentType": "park",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
        },
        {
            "goalId": "goal_meal",
            "intentType": "meal",
            "requiredMin": 0,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "soft_experience",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
        },
    ]
    requirement = {
        "required": True,
        "source": "controller_estimate",
        "missingFields": ["detourTolerance", "mobilityProfile"],
        "mobilityProfile": {
            "required": True,
            "fields": ["transportMode", "paceClass"],
        },
        "detourEnvelope": {
            "required": True,
            "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
        },
    }
    repair = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=("draft_itinerary", "read_itinerary", "verify_external_facts"),
    ).repair_payload(
        action="draft_itinerary",
        invalid_paths=["actionDirective.routePlanningPolicy"],
        allowed_ids={"goalIds": [item["goalId"] for item in goals]},
        aliases_applied=[],
        goal_requirements=goals,
        route_policy_requirement=requirement,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=30,
    )
    captured = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        return json.dumps(repair["minimalExample"], ensure_ascii=False)

    provider._post = fake_post
    response = provider.decide_autonomy(
        {
            "schemaVersion": "controller-context-full-v1",
            "latestUserMessage": (
                "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，"
                "中午品尝当地网红美食，晚上去附近的公园逛一下"
            ),
            "selectedCity": "北京",
            "itineraryLifecycle": {
                "state": "empty_scaffold",
                "activeVersionId": None,
                "dayCount": 0,
                "segmentCount": 0,
            },
            "allowedActions": ["draft_itinerary", "read_itinerary", "verify_external_facts"],
            "fingerprints": {
                "requestIntentContractFingerprint": "647811364c08ec763902c1512c17bfb4",
                "observationFingerprint": "381eb99c43df59383f1cf1cc",
            },
            "decisionConstraints": {
                "allWritesVersioned": True,
                "finalPoiMustBeAmapGrounded": True,
                "askOnlyWhenMaterialChoiceRemains": True,
            },
            "decisionContractRef": {
                "version": "agent-decision-contract-v3",
                "hash": "v4-debug-bundle-projection",
            },
            "goalRequirements": goals,
            "routePlanningPolicyRequirement": requirement,
        },
        timeout_seconds=2.5,
        repair_feedback=json.dumps(repair, ensure_ascii=False),
    )

    assert json.loads(response)["primaryAction"] == "draft_itinerary"
    final_http_payload = captured["payload"]
    assert set(final_http_payload) == {
        "model",
        "messages",
        "temperature",
        "thinking",
        "response_format",
        "max_tokens",
    }
    assert final_http_payload["messages"][0] == {
        "role": "system",
        "content": AUTONOMY_DECISION_SYSTEM_PROMPT,
    }
    assert len(final_http_payload["messages"]) == 3
    projected_context = json.loads(final_http_payload["messages"][1]["content"])
    assert projected_context["repairMode"] == "draft_itinerary_schema_only"
    assert "goalRequirements" not in projected_context
    assert "Repair contract:" in final_http_payload["messages"][2]["content"]
    repair_prompt = final_http_payload["messages"][2]["content"]
    assert all(goal["goalId"] in repair_prompt for goal in goals)
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT


def test_deepseek_lite_payload_is_smaller_and_has_no_tools_or_full_directive_prompt():
    provider = DeepSeekAgentProvider(api_key="test-key", model="test-model", timeout_seconds=30)
    captured = {}

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        captured["timeout"] = timeout_seconds
        return '{"schemaVersion":"agent-decision-lite-v1","primaryAction":"finish","confidence":1,"reasonCode":"done","userVisibleReason":"完成"}'

    provider._post = fake_post
    provider.decide_autonomy_lite({"schemaVersion": "autonomy-context-v2"}, timeout_seconds=1.5)

    assert "tools" not in captured["payload"]
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["payload"]["max_tokens"] == 180
    assert captured["payload"]["max_tokens"] < 900
    assert captured["timeout"] == 1.5


def test_controller_performance_evidence_is_redacted_and_tracks_transport_boundaries(monkeypatch):
    provider = DeepSeekAgentProvider(api_key="secret-test-key", model="test-model", timeout_seconds=30)
    shared = {}

    class _Response:
        headers = {"x-sensitive": "must-not-be-copied"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            time.sleep(0.002)
            return b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"primaryAction\\":\\"finish\\"}"}}]}'

    monkeypatch.setattr("src.services.deepseek_agent_provider.urlopen", lambda *_args, **_kwargs: _Response())
    context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "sensitive trip request",
        "observation": {"privateValue": "do-not-copy"},
    }
    provider.prepare_controller_performance(context, shared, call_kind="full")
    provider.decide_autonomy(context, timeout_seconds=2.5)

    assert shared["payloadBytes"] > 0
    assert shared["contextCharCounts"]["latestUserMessage"] == len('"sensitive trip request"')
    assert shared["contextCharCounts"]["observation"] > 0
    assert shared["responseHeadersReceived"] is True
    assert shared["ttfbDurationMs"] >= 0
    assert shared["readDurationMs"] >= 0
    assert shared["connectDurationMs"] is None
    assert shared["connectTimingAvailable"] is False
    assert shared["transportTimingBoundary"] == "urlopen_response_headers"
    serialized = json.dumps(shared)
    assert "secret-test-key" not in serialized
    assert "sensitive trip request" not in serialized
    assert "do-not-copy" not in serialized
    assert "must-not-be-copied" not in serialized


def test_controller_event_metadata_includes_worker_queue_and_redacted_provider_performance():
    class _PerformanceProvider:
        model = "performance-controller"

        def prepare_controller_performance(self, autonomy_context, sink, *, call_kind):
            sink.update(
                {
                    "callKind": call_kind,
                    "payloadBytes": 321,
                    "contextCharCounts": {"observation": 17},
                    "connectDurationMs": None,
                    "connectTimingAvailable": False,
                    "ttfbDurationMs": 8,
                    "readDurationMs": 2,
                    "responseHeadersReceived": True,
                    "authorization": "must-not-leak",
                }
            )

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "read_itinerary",
                "actionDirective": {"type": "read_itinerary", "queryType": "timeline_summary"},
            }

    result = AgentAutonomyController(provider=_PerformanceProvider()).decide(
        "当前安排是什么",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    evidence = result.to_event_metadata()["controllerPerformance"][0]
    assert evidence["callKind"] == "full"
    assert evidence["workerQueueMs"] >= 0
    assert evidence["payloadBytes"] == 321
    assert evidence["responseHeadersReceived"] is True
    assert "authorization" not in evidence


class _QueuedTimeoutProvider:
    model = "timeout-controller"

    def prepare_controller_performance(self, autonomy_context, sink, *, call_kind):
        sink.update(
            {
                "callKind": call_kind,
                "payloadBytes": 987,
                "contextCharCounts": {"observation": 44},
                "connectDurationMs": None,
                "connectTimingAvailable": False,
                "preHeaderWaitDurationMs": None,
                "ttfbDurationMs": None,
                "readDurationMs": None,
                "responseHeadersReceived": False,
            }
        )

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        time.sleep(0.8)
        raise TimeoutError("controller_decision_timeout")


def test_controller_timeout_preserves_pre_header_and_worker_queue_evidence():
    result = AgentAutonomyController(
        provider=_QueuedTimeoutProvider(),
        decision_timeout_seconds=0.4,
        lite_timeout_seconds=0.2,
        total_budget_seconds=0.4,
        max_schema_repair_attempts=0,
    ).decide(
        "北京两日游",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools=set(),
        runtime_budget_tools=set(),
    )

    evidence = result.to_event_metadata()["controllerPerformance"][0]
    assert result.controller_failures[0].failure_class == "provider_timeout"
    assert evidence["workerQueueMs"] >= 0
    assert evidence["payloadBytes"] == 987
    assert evidence["preHeaderWaitDurationMs"] >= 0
    assert evidence["captureState"] == "controller_deadline_snapshot"
    assert evidence["ttfbDurationMs"] is None
    assert evidence["responseHeadersReceived"] is False


def test_controller_transport_read_timeout_preserves_received_headers(monkeypatch):
    provider = DeepSeekAgentProvider(
        api_key="secret-read-timeout-key",
        model="controller-read-timeout-test",
        timeout_seconds=30,
    )
    shared = {}

    class _ReadTimeoutResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            time.sleep(0.002)
            raise TimeoutError("body_read_timeout")

    monkeypatch.setattr(
        "src.services.deepseek_agent_provider.urlopen",
        lambda *_args, **_kwargs: _ReadTimeoutResponse(),
    )
    provider.prepare_controller_performance(
        {"schemaVersion": "model-first-autonomy-context-v2"},
        shared,
        call_kind="full",
    )

    with pytest.raises(HTTPException) as captured_error:
        provider._post_json({"messages": []}, timeout_seconds=1.0)

    assert captured_error.value.status_code == 504

    assert shared["responseHeadersReceived"] is True
    assert shared["preHeaderWaitDurationMs"] >= 0
    assert shared["ttfbDurationMs"] == shared["preHeaderWaitDurationMs"]
    assert shared["readDurationMs"] >= 0
    assert shared["ttfbMeasurement"] == "response_headers_available"


def test_invalid_provider_json_remains_bad_gateway_not_timeout(monkeypatch):
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="controller-invalid-json-test",
        timeout_seconds=30,
    )

    class InvalidJsonResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{"

    monkeypatch.setattr(
        "src.services.deepseek_agent_provider.urlopen",
        lambda *_args, **_kwargs: InvalidJsonResponse(),
    )

    with pytest.raises(HTTPException) as captured_error:
        provider._post_json({"messages": []}, timeout_seconds=1.0)

    assert captured_error.value.status_code == 502


def test_controller_performance_prepare_failure_does_not_change_decision():
    class _PrepareFailureProvider:
        model = "prepare-failure-controller"

        def prepare_controller_performance(self, autonomy_context, sink, *, call_kind):
            raise RuntimeError("observability_hook_failed")

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "read_itinerary",
                "actionDirective": {"type": "read_itinerary", "queryType": "timeline_summary"},
            }

    result = AgentAutonomyController(provider=_PrepareFailureProvider()).decide(
        "当前安排是什么",
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"read_itinerary"},
        runtime_budget_tools={"read_itinerary"},
    )

    assert result.source == "controller"
    assert result.to_event_metadata()["controllerPerformance"][0]["instrumentationStatus"] == "prepare_failed"


def test_controller_worker_admission_is_bounded_and_falls_back_without_submit():
    class _SaturatedController(AgentAutonomyController):
        _decision_worker_slots = BoundedSemaphore(value=1)

    class _Provider:
        model = "bounded-admission-controller"

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            raise AssertionError("provider must not run while worker admission is saturated")

    controller = _SaturatedController(
        provider=_Provider(),
        max_schema_repair_attempts=0,
    )
    controller._decision_worker_slots.acquire()
    try:
        result = controller.decide(
            "北京两日游",
            {},
            {"schemaVersion": "model-first-autonomy-context-v2"},
            available_tools=set(),
            runtime_budget_tools=set(),
        )
    finally:
        controller._decision_worker_slots.release()

    evidence = result.to_event_metadata()["controllerPerformance"][0]
    assert result.source == "safe_fallback"
    assert evidence["captureState"] == "worker_queue_saturated"
    assert evidence["workerQueueMs"] is None


@pytest.mark.parametrize("call_kind", ["full", "lite"])
def test_timed_out_controller_releases_the_worker_pool_that_admitted_it(monkeypatch, call_kind):
    provider_started = Event()
    allow_provider_finish = Event()

    class RecordingWorkerSlots:
        def __init__(self):
            self.acquire_count = 0
            self.release_count = 0
            self.released = Event()

        def acquire(self, *, blocking):
            assert blocking is False
            self.acquire_count += 1
            return True

        def release(self):
            self.release_count += 1
            self.released.set()

    class StartedExecutor:
        def submit(self, invoke):
            future = Future()

            def run():
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    future.set_result(invoke())
                except BaseException as error:  # pragma: no cover - Future carries the provider error.
                    future.set_exception(error)

            Thread(target=run, daemon=True).start()
            assert provider_started.wait(timeout=1)
            return future

    class BlockingProvider:
        model = "worker-slot-ownership-controller"

        @staticmethod
        def _block():
            provider_started.set()
            assert allow_provider_finish.wait(timeout=1)
            return {}

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            return self._block()

        def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
            return self._block()

    admitted_slots = RecordingWorkerSlots()
    replacement_slots = RecordingWorkerSlots()
    monkeypatch.setattr(AgentAutonomyController, "_decision_executor", StartedExecutor())
    monkeypatch.setattr(AgentAutonomyController, "_decision_worker_slots", admitted_slots)

    controller = AgentAutonomyController(
        provider=BlockingProvider(),
        decision_timeout_seconds=0.02,
        lite_timeout_seconds=0.01,
        total_budget_seconds=0.02,
        max_schema_repair_attempts=0,
    )
    performance_evidence = []
    deadline = time.monotonic() + 0.02
    with pytest.raises(TimeoutError):
        if call_kind == "full":
            controller._call_provider(
                {"schemaVersion": "model-first-autonomy-context-v2"},
                repair_feedback="",
                deadline=deadline,
                timeout_seconds=0.02,
                performance_evidence=performance_evidence,
            )
        else:
            controller._call_lite_provider(
                {"schemaVersion": "model-first-autonomy-context-v2"},
                deadline=deadline,
                timeout_seconds=0.02,
                performance_evidence=performance_evidence,
            )

    monkeypatch.setattr(AgentAutonomyController, "_decision_worker_slots", replacement_slots)
    allow_provider_finish.set()
    assert admitted_slots.released.wait(timeout=1)
    assert admitted_slots.acquire_count == 1
    assert admitted_slots.release_count == 1
    assert replacement_slots.release_count == 0


class _FullTimeoutLiteDraftProvider:
    model = "mock-controller"

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        raise TimeoutError("controller_decision_timeout")

    def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
        return {
            "schemaVersion": "agent-decision-lite-v1",
            "primaryAction": "draft_itinerary",
            "confidence": 0.94,
            "reasonCode": "complete_initial_trip_request",
            "userVisibleReason": "信息已足够，可起草行程。",
        }


class _BudgetProbeProvider:
    model = "mock-controller"

    def __init__(self):
        self.full_calls = 0
        self.lite_calls = 0

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        self.full_calls += 1
        raise AssertionError("Full Controller must not run after the runtime budget is exhausted")

    def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
        self.lite_calls += 1
        raise AssertionError("Lite Controller must not run after the runtime budget is exhausted")


def test_runtime_budget_below_call_threshold_does_not_expand_deadline_or_call_controller():
    message = "今年国庆北京两日游，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    provider = _BudgetProbeProvider()
    observation = AgentObservationBuilder().build(
        {"latestUserMessage": message, "effectiveUserMessage": message, "currentItinerarySnapshot": None}
    )

    result = AgentAutonomyController(
        provider=provider,
        fallback_decision_resolver=AgentDecisionArbitrator(),
    ).decide(
        message,
        {"runtimeLimits": {"remainingRunMs": 1}},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools=set(),
        runtime_budget_tools=set(),
        observation=observation,
    )

    metadata = result.to_event_metadata()
    assert provider.full_calls == 0
    assert provider.lite_calls == 0
    assert result.decision_path == "fallback"
    assert result.controller_failures[0].failure_class == "provider_timeout"
    assert metadata["controllerCalled"] is False
    assert metadata["actualExecutionRoute"] is None


def test_full_timeout_then_lite_draft_is_not_authorized_as_high_risk_write():
    result = AgentAutonomyController(
        provider=_FullTimeoutLiteDraftProvider(),
        decision_timeout_seconds=1.0,
        lite_timeout_seconds=0.4,
        total_budget_seconds=1.4,
    ).decide(
        "北京两日游",
        {"runtimeLimits": {"remainingRunMs": 1200}},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"resolve_poi", "web_search", "amap_weather", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "web_search", "amap_weather", "patch_itinerary"},
    )

    metadata = result.to_event_metadata()
    assert result.decision_path == "lite"
    assert result.decision.primary_action == "ask_user"
    assert (
        AgentActionExecutorRegistry().route(
            {
                "accepted": result.gated_decision.accepted,
                "primaryAction": result.decision.primary_action,
                "actionDirective": None,
            }
        )
        == "clarification"
    )
    assert "lite_high_risk_write_authority_forbidden" in result.decision.reason_codes
    assert result.decision.side_effects["itinerary"] is False
    assert metadata["controllerFullCalled"] is True
    assert metadata["controllerFullSucceeded"] is False
    assert metadata["controllerLiteCalled"] is True
    assert metadata["controllerLiteSucceeded"] is True
    assert metadata["controllerFailures"][0]["failureClass"] == "provider_timeout"
    assert (
        metadata["controllerTimeoutConfig"]["deadlineSource"] == "agent_runtime_remaining_and_controller_total_budget"
    )
    assert metadata["actualExecutionRoute"] is None


class _FullAndLiteTimeoutProvider:
    model = "mock-controller"

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        raise TimeoutError("controller_decision_timeout")

    def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
        raise TimeoutError("controller_lite_timeout")


class _FullAndLiteInvalidStructuredResponseProvider:
    model = "mock-controller"

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        if repair_feedback:
            raise json.JSONDecodeError("unterminated controller response", "{", 1)
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "confidence": 0.9,
            "decisionSummary": "missing required day strategies",
            "actionDirective": {
                "type": "draft_itinerary",
                "requestIntentContract": {},
            },
            "stopCondition": {"type": "verified_draft"},
        }

    def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
        raise json.JSONDecodeError("unterminated lite response", "{", 1)


class _DraftRepairPayloadBudgetProvider:
    """Mirror the trace: obsolete draft schema, then a local repair-budget failure."""

    model = "mock-controller"

    def __init__(self):
        self.calls: list[str] = []

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        self.calls.append("repair" if repair_feedback else "full")
        if repair_feedback:
            raise ValueError("controller_repair_payload_too_large")
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "confidence": 0.9,
            "decisionSummary": "obsolete draft contract",
            "actionDirective": {
                "decisionContractRef": "legacy",
                "goalConstraints": [{"goalId": "goal_campus"}],
                "searchStrategy": {"mode": "candidate_first"},
            },
            "stopCondition": {"type": "verified_draft"},
        }


class _ScalarRequiredGoalCountsProvider:
    """Mirror the live bundle's scalar requiredGoalCounts contract violation."""

    model = "mock-controller"

    def __init__(self):
        self.calls: list[str] = []

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        self.calls.append("repair" if repair_feedback else "full")
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "confidence": 0.9,
            "decisionSummary": "scalar required goal counts",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_campus"],
                "optionalExperienceBudget": 0,
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "campus one",
                        "requiredGoalIds": ["goal_campus"],
                        "requiredGoalCounts": 1,
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "theme": "campus two",
                        "requiredGoalIds": ["goal_campus"],
                        "requiredGoalCounts": 1,
                        "optionalGoalIds": [],
                    },
                ],
            },
            "stopCondition": {"type": "continue_after_observation"},
        }


class _TopLevelRequiredGoalCountsProvider:
    """Mirror the live bundle's otherwise usable draft with one forbidden root field."""

    model = "mock-controller"

    def __init__(self):
        self.calls: list[str] = []

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        self.calls.append("repair" if repair_feedback else "full")
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                # This field belongs only inside dayStrategies[*].  The strict
                # model must reject it here, while the host may rebuild the
                # directive from its frozen occurrence ledger.
                "requiredGoalCounts": {"goal_campus": 1},
                "goalPriority": ["goal_campus"],
                "optionalExperienceBudget": 0,
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "campus one",
                        "requiredGoalIds": ["goal_campus"],
                        "requiredGoalCounts": {"goal_campus": 1},
                        "optionalGoalIds": [],
                    }
                ],
            },
            "stopCondition": {"type": "continue_after_observation"},
        }


class _TruncatedRepairPayloadBudgetProvider:
    """A transport-confirmed truncation gets one content-free reissue before local budget failure."""

    model = "mock-controller"

    def __init__(self):
        self.calls: list[str] = []

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        self.calls.append("repair" if repair_feedback else "full")
        if repair_feedback:
            feedback = json.loads(repair_feedback)
            assert feedback["repairMode"] == "full_response_truncation_reissue"
            assert "action" not in feedback
            raise ValueError("controller_repair_payload_too_large")
        raise _controller_truncation_error(
            call_kind="full",
            content='{"schemaVersion":"agent-decision-v3","primaryAction":"draft_itinerary",',
        )


class _OverallocatedDraftGoalProvider:
    model = "mock-controller"

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": ["goal_campus"],
                "optionalExperienceBudget": 0,
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "campus one",
                        "requiredGoalIds": ["goal_campus"],
                        "requiredGoalCounts": {"goal_campus": 1},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "theme": "campus two",
                        "requiredGoalIds": ["goal_campus"],
                        "requiredGoalCounts": {"goal_campus": 1},
                        "optionalGoalIds": [],
                    },
                ],
            },
        }


class _MissingRequiredDraftGoalProvider:
    model = "mock-controller"

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": [],
                "optionalExperienceBudget": 0,
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "missing required goal",
                        "requiredGoalIds": [],
                        "requiredGoalCounts": {},
                        "optionalGoalIds": [],
                    }
                ],
            },
        }


class _FullAndLiteInternalErrorProvider:
    model = "mock-controller"

    def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
        raise RuntimeError("unexpected_controller_bug")

    def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
        raise RuntimeError("unexpected_lite_controller_bug")


def test_full_and_lite_failure_offer_confirmation_only_after_both_attempts():
    message = "今年国庆北京两日游，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    observation = AgentObservationBuilder().build(
        {"latestUserMessage": message, "effectiveUserMessage": message, "currentItinerarySnapshot": None}
    )
    result = AgentAutonomyController(
        provider=_FullAndLiteTimeoutProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=1,
        lite_timeout_seconds=0.5,
        total_budget_seconds=1.5,
    ).decide(
        message,
        {},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools=set(),
        runtime_budget_tools=set(),
        observation=observation,
    )

    assert result.decision.primary_action == "ask_user"
    assert result.decision_path == "fallback"
    assert result.controller_full_called is True
    assert result.controller_lite_called is True
    assert result.controller_lite_succeeded is False
    assert [failure.stage for failure in result.controller_failures] == ["full", "lite"]
    assert [item["action"] for item in result.decision.clarification["options"]] == [
        "retry_model_planning",
        "confirm_rule_safe_draft",
        "manual_continuation",
    ]


def test_partial_plan_expansion_attempts_full_then_lite_and_fails_retryable_without_manual_edit():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_partial",
            "latestUserMessage": "继续生成其他方案",
            "currentItinerarySnapshot": {
                "id": "plan_partial",
                "versionId": "ver_partial",
                "portfolioPartialTimeline": {"status": "partial"},
                "portfolioPendingSlots": [{"planningSlotId": "meal_day_2"}],
                "portfolioSelectionContext": {
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                    "focusBriefId": "brief_campus",
                    "requestContractFingerprint": "fp_request",
                },
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [{"id": "seg_1", "title": "清华大学"}],
                    }
                ],
            },
        }
    )
    result = AgentAutonomyController(
        provider=_FullAndLiteTimeoutProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=1,
        lite_timeout_seconds=0.5,
        total_budget_seconds=1.5,
    ).decide(
        "继续生成其他方案",
        {
            "_partialPlanExpansionAuthorized": True,
            "activeVersionId": "ver_partial",
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": "portfolio_root",
            "focusBriefId": "brief_campus",
            "requestContractFingerprint": "fp_request",
            "retryExecutionPlan": {
                "kind": "expand_partial_portfolio",
                "controllerAllowed": True,
                "allowedActions": ["draft_itinerary", "ask_user"],
                "writeBudget": 0,
            },
        },
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"draft_itinerary"},
        runtime_budget_tools={"draft_itinerary"},
        observation=observation,
    )

    assert result.controller_full_called is True
    assert result.controller_lite_called is True
    assert result.controller_lite_succeeded is False
    assert result.decision_path == "fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.decision.side_effects["itinerary"] is False
    assert result.decision.reason_codes == ["partial_plan_expansion_controller_failed_retryable"]
    assert result.decision.clarification["question"] == ("生成其他方案时模型请求超时，当前方案和时间轴未发生变化。")
    assert [item["action"] for item in result.decision.clarification["options"]] == ["retry_model_planning"]


def test_lite_target_dependent_action_without_binding_fails_closed():
    lite = AgentAutonomyController._validate_lite_provider_decision(
        {
            "schemaVersion": "agent-decision-lite-v1",
            "primaryAction": "patch_itinerary",
            "confidence": 0.9,
            "reasonCode": "edit_requested",
            "userVisibleReason": "需要修改行程。",
        }
    )
    decision = AgentAutonomyController._decision_from_lite(lite, {})

    assert decision.primary_action == "finish"
    assert "lite_target_binding_required" in decision.reason_codes
    assert decision.target_scope.model_dump() == {}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("timeout"), "provider_timeout"),
        (HTTPException(status_code=400, detail="DEEPSEEK_API_KEY is not configured"), "provider_unavailable"),
        (HTTPException(status_code=429, detail="slow down"), "rate_limited"),
        (ConnectionError("offline"), "network_error"),
        (__import__("json").JSONDecodeError("bad", "{", 0), "invalid_json"),
        (RuntimeError("controller_provider_unavailable"), "provider_unavailable"),
        (CancelledError(), "cancelled"),
    ],
)
def test_controller_failure_classifier_normalizes_transport_and_provider_errors(error, expected):
    failure = classify_controller_failure(
        error,
        stage="full",
        provider="mock",
        model="mock-model",
        duration_ms=12,
        timeout_seconds=1.0,
    )
    assert failure.failure_class == expected
    assert failure.stage == "full"


def test_controller_failure_classifier_distinguishes_schema_and_missing_directive():
    with pytest.raises(ValidationError) as missing_directive:
        AgentDecision.model_validate(
            {
                "schemaVersion": "agent-decision-v2",
                "primaryAction": "finish",
                "confidence": 1,
                "decisionSummary": "done",
                "targetScope": {},
            }
        )
    missing = classify_controller_failure(
        missing_directive.value,
        stage="full",
        provider="mock",
        model="mock-model",
        duration_ms=1,
        timeout_seconds=1,
    )
    with pytest.raises(ValidationError) as schema_invalid:
        AgentDecision.model_validate({"primaryAction": "not-valid"})
    invalid = classify_controller_failure(
        schema_invalid.value,
        stage="lite",
        provider="mock",
        model="mock-model",
        duration_ms=1,
        timeout_seconds=1,
    )
    assert missing.failure_class == "action_directive_missing"
    assert invalid.failure_class == "schema_validation_failed"


@pytest.mark.parametrize(
    "provider",
    [
        _SlowProvider(),
        _InvalidProvider(),
    ],
)
def test_existing_itinerary_controller_failure_is_fail_closed_without_planner_write(provider):
    class CountingPlanner(_Planner):
        def __init__(self):
            self.calls = 0

        def plan(self, latest_message, request_context):
            self.calls += 1
            return super().plan(latest_message, request_context)

    planner = CountingPlanner()
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_existing",
            "currentItinerarySnapshot": {
                "id": "plan_existing",
                "versionId": "ver_existing",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_1",
                                "startTime": "09:00",
                                "endTime": "10:30",
                                "kind": "visit",
                                "poi": {"name": "中国美术馆", "amapId": "B0MUSEUM"},
                            }
                        ],
                    }
                ],
            },
        }
    )
    result = AgentAutonomyController(
        provider=provider,
        planner_service=planner,
        decision_timeout_seconds=0.03,
        lite_timeout_seconds=0.03,
        total_budget_seconds=0.06,
    ).decide(
        "把美术馆改到十五点半",
        {"activeVersionId": "ver_existing"},
        {"schemaVersion": "model-first-autonomy-context-v2"},
        available_tools={"patch_itinerary"},
        runtime_budget_tools={"patch_itinerary"},
        observation=observation,
    )

    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.gated_decision.effective_write_risk == "none"
    assert result.planner_called is False
    assert planner.calls == 0


def test_planner_fallback_maps_initial_planning_to_staged_draft_action():
    decision = AgentAutonomyController._decision_from_planner(
        {
            "taskType": "initial_planning",
            "taskRoute": "initial_planning -> versioned_patch -> basic_verifier",
            "requiresPatch": True,
            "allowedTools": ["patch_itinerary"],
        }
    )

    assert decision.primary_action == "draft_itinerary"


def test_controller_full_timeout_reuses_independent_lite_projection_without_server_only_sections():
    class CapturingProvider:
        model = "projection-controller"

        def __init__(self):
            self.full_context = None
            self.lite_context = None

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            self.full_context = autonomy_context
            raise TimeoutError("controller_decision_timeout")

        def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
            self.lite_context = autonomy_context
            return {
                "schemaVersion": "agent-decision-lite-v1",
                "primaryAction": "ask_user",
                "confidence": 1.0,
                "reasonCode": "bounded_lite",
                "userVisibleReason": "请选择继续方式。",
            }

    provider = CapturingProvider()
    result = AgentAutonomyController(
        provider=provider,
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.4,
        total_budget_seconds=1.0,
    ).decide(
        "北京两日游",
        {},
        {
            "schemaVersion": "model-first-autonomy-context-v2",
            "latestUserMessage": "北京两日游",
            "memoryRules": {"large": "x" * 20_000},
            "observation": {"large": "y" * 20_000},
        },
        available_tools=set(),
        runtime_budget_tools=set(),
    )

    assert result.controller_lite_succeeded is True
    assert provider.full_context["schemaVersion"] == "controller-context-full-v1"
    assert provider.lite_context["schemaVersion"] == "controller-context-lite-v1"
    assert provider.full_context is not provider.lite_context
    assert "memoryRules" not in provider.full_context
    assert "observation" not in provider.full_context
    assert "decisionContract" not in provider.full_context
    assert "decisionContractRef" in provider.full_context
    assert "decisionConstraints" not in provider.lite_context
    assert len(json.dumps(provider.full_context, ensure_ascii=False).encode("utf-8")) <= 15_360
    assert len(json.dumps(provider.lite_context, ensure_ascii=False).encode("utf-8")) <= 8_192


def _authoritative_two_day_autonomy_context() -> dict:
    requirement = {
        "goalId": "goal_campus",
        "intentType": "campus_visit",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferredCount": 2,
        "maxCount": 2,
        "allowedDayNumbers": [1, 2],
        "distributionPolicy": "spread_across_distinct_days",
    }
    return {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "北京高校两日游",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "observation": {
            "request": {"intentContract": {"requiredIntents": [requirement]}},
            "requirementCoverage": {"required": [requirement]},
        },
    }


def test_full_and_lite_timeout_continue_with_server_cardinality_draft_without_preseeded_decision():
    result = AgentAutonomyController(
        provider=_FullAndLiteTimeoutProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.controller_full_succeeded is False
    assert result.controller_lite_succeeded is False
    assert result.deterministic_arbitrator_called is False
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert result.decision_path == "fallback"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "server_directive_after_controller_timeout" in result.decision.reason_codes


def test_controller_timeout_with_unresolved_detour_returns_retryable_route_clarification() -> None:
    request_context = {
        "serverExecutionProfile": "simple_open_v1",
        "latestUserMessage": "北京高校两日游，公交地铁优先",
        "requestIntentContract": {
            "clarificationRequired": True,
            "clarificationDimensions": [
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "intentType": "route_decision",
                    "status": "unresolved",
                    "allowedSemanticFields": ["detourTolerance"],
                }
            ],
            "routeDecisionContract": {
                "schemaVersion": "route-decision-contract-v1",
                "status": "awaiting_clarification",
                "missingFields": ["detourTolerance"],
                "mobilityProfile": {
                    "source": "explicit_request_mobility_semantics",
                    "transportMode": "transit",
                    "paceClass": "standard",
                    "walkingPenaltyMinutesPerKm": 1.8,
                    "transferPenaltyMinutes": 6.0,
                    "waitTimeMultiplier": 1.0,
                    "riskPenaltyMultiplier": 1.0,
                },
                "detourTolerance": None,
                "detourToleranceSource": None,
                "fingerprint": "route-contract-pending-detour",
            },
        },
    }
    autonomy_context = _authoritative_two_day_autonomy_context()
    autonomy_context["requestIntentContract"] = request_context["requestIntentContract"]

    result = AgentAutonomyController(
        provider=_FullAndLiteTimeoutProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        request_context["latestUserMessage"],
        request_context,
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert "route_clarification_controller_retry_required" in result.decision.reason_codes
    assert "绕路" in result.decision.clarification["question"]
    assert [item["action"] for item in result.decision.clarification["options"]] == [
        "retry_model_planning",
        "manual_continuation",
    ]
    assert all(item.get("semanticValue") is None for item in result.decision.clarification["options"])


def test_repair_invalid_structured_response_fails_closed_without_lite_or_server_draft():
    result = AgentAutonomyController(
        provider=_FullAndLiteInvalidStructuredResponseProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.controller_full_succeeded is False
    assert result.controller_lite_succeeded is False
    assert result.deterministic_arbitrator_called is False
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert result.decision_path == "fallback"
    assert "deterministic_cardinality_fallback" not in result.decision.reason_codes
    assert "server_directive_after_controller_invalid_response" not in result.decision.reason_codes
    assert {failure.failure_class for failure in result.controller_failures} <= {
        "invalid_json",
        "schema_validation_failed",
        "action_directive_missing",
    }
    assert {"repair"} == {failure.stage for failure in result.controller_failures}
    assert all(item["providerInvoked"] is True for item in result.controller_performance)


def test_draft_repair_payload_budget_failure_uses_authoritative_server_directive():
    provider = _DraftRepairPayloadBudgetProvider()
    result = AgentAutonomyController(
        provider=provider,
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == ["full", "repair"]
    assert result.schema_repair_attempts == 1
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert result.controller_failures[-1].stage == "repair"
    assert result.controller_failures[-1].failure_class == "request_budget_exceeded"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "server_directive_after_controller_repair_budget" in result.decision.reason_codes


def test_scalar_required_goal_counts_uses_authoritative_server_directive_without_repair():
    provider = _ScalarRequiredGoalCountsProvider()
    result = AgentAutonomyController(
        provider=provider,
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == ["full"]
    assert result.schema_repair_attempts == 0
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "server_directive_after_authoritative_contract_rejection" in result.decision.reason_codes


def test_top_level_required_goal_counts_uses_authoritative_server_directive_without_repair():
    provider = _TopLevelRequiredGoalCountsProvider()
    autonomy_context = _authoritative_two_day_autonomy_context()
    requirement = autonomy_context["observation"]["request"]["intentContract"]["requiredIntents"][0]
    request_contract = {
        "requiredIntents": [requirement],
        "clarificationRequired": False,
        "clarificationDimensions": [],
        "routeDecisionContract": {
            "schemaVersion": "route-decision-contract-v1",
            "status": "ready",
            "missingFields": [],
            "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
            "mobilityProfileSource": "server_safe_default_v1",
            "detourTolerance": {"maxGeneralizedCostDelta": 35, "maxDetourRatio": 0.35},
            "detourToleranceSource": "server_safe_default_v1",
        },
    }
    autonomy_context.update(
        {
            "effectiveUserMessage": "北京高校两日游",
            "serverExecutionProfile": "simple_open_v1",
            "requestIntentContract": request_contract,
        }
    )
    result = AgentAutonomyController(
        provider=provider,
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {
            "serverExecutionProfile": "simple_open_v1",
            "requestIntentContract": request_contract,
        },
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == ["full"]
    assert result.schema_repair_attempts == 0
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "server_directive_after_authoritative_contract_rejection" in result.decision.reason_codes


def test_top_level_required_goal_counts_remains_strictly_invalid_model_output():
    provider = _TopLevelRequiredGoalCountsProvider()
    raw = provider.decide_autonomy({}, timeout_seconds=0.1)

    with pytest.raises(ValidationError) as exc_info:
        AgentAutonomyController()._validate_provider_decision(
            raw,
            AgentAutonomyController._normalization_context(_authoritative_two_day_autonomy_context()),
        )

    assert any(
        tuple(str(item) for item in error.get("loc") or ())
        == ("actionDirective", "draft_itinerary", "requiredGoalCounts")
        and error.get("type") == "extra_forbidden"
        for error in exc_info.value.errors(include_input=False, include_url=False)
    )


def test_ready_request_contract_does_not_offer_controller_unissued_clarification():
    requirement = {
        "goalId": "goal_campus",
        "intentType": "campus_visit",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferredCount": 2,
        "maxCount": 2,
        "allowedDayNumbers": [1, 2],
        "distributionPolicy": "spread_across_distinct_days",
    }
    ready_contract = {
        "requiredIntents": [requirement],
        "clarificationRequired": False,
        "clarificationDimensions": [],
        "routeDecisionContract": {
            "schemaVersion": "route-decision-contract-v1",
            "status": "ready",
            "missingFields": [],
            "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
            "mobilityProfileSource": "server_safe_default_v1",
            "detourTolerance": {"maxGeneralizedCostDelta": 35, "maxDetourRatio": 0.35},
            "detourToleranceSource": "server_safe_default_v1",
        },
    }

    class ReadyContractProvider:
        model = "recorded-live-ready-contract"

        def __init__(self) -> None:
            self.contexts: list[dict] = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            assert not repair_feedback
            self.contexts.append(autonomy_context)
            assert "ask_user" not in autonomy_context["allowedActions"]
            assert autonomy_context["clarificationDimensions"] == []
            directive = AgentDecisionContractService.deterministic_draft_directive([requirement])
            directive["routePlanningPolicy"] = {
                "source": "controller_estimate",
                "objective": "least_generalized_cost",
                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
                "detourEnvelope": {"maxGeneralizedCostDelta": 35, "maxDetourRatio": 0.35},
            }
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            }

    autonomy_context = _authoritative_two_day_autonomy_context()
    autonomy_context.update(
        {
            "latestUserMessage": "重试生成澄清问题",
            "effectiveUserMessage": "今年国庆安排北京高校两日游",
            "serverExecutionProfile": "simple_open_v1",
            # Simulate the stale pre-default contract captured by the debug
            # bundle.  The final request_context below is authoritative.
            "requestIntentContract": {
                "requiredIntents": [requirement],
                "clarificationRequired": True,
                "clarificationDimensions": [
                    {
                        "dimensionId": "route_decision.mobility_profile",
                        "status": "unresolved",
                        "allowedSemanticFields": ["mobilityProfile"],
                    }
                ],
            },
        }
    )
    provider = ReadyContractProvider()

    result = AgentAutonomyController(provider=provider).decide(
        "今年国庆安排北京高校两日游",
        {
            "serverExecutionProfile": "simple_open_v1",
            "requestIntentContract": ready_contract,
        },
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert len(provider.contexts) == 1
    assert result.decision.primary_action == "draft_itinerary", (
        result.controller_error,
        [failure.__dict__ for failure in result.controller_failures],
    )
    assert result.gated_decision.accepted is True


def test_known_truncation_repair_budget_failure_fails_closed_without_guessing_draft_action():
    provider = _TruncatedRepairPayloadBudgetProvider()
    result = AgentAutonomyController(
        provider=provider,
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == ["full", "repair"]
    assert result.schema_repair_attempts == 1
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert result.controller_failures[-1].stage == "repair"
    assert result.controller_failures[-1].failure_class == "request_budget_exceeded"
    assert result.decision.reason_codes == ["controller_failure_existing_itinerary_fail_closed"]


def test_draft_goal_error_cannot_fallback_without_authoritative_goal_contract():
    context = _authoritative_two_day_autonomy_context()
    requirement = context["observation"]["requirementCoverage"]["required"][0]
    requirement["maxCount"] = 1
    context["observation"]["request"]["intentContract"]["requiredIntents"] = []
    assert AgentAutonomyController._normalization_context(context)["authoritativeGoalLedger"] is False
    observation = AgentObservationBuilder().build(
        {
            "latestUserMessage": "北京高校两日游",
            "effectiveUserMessage": "北京高校两日游",
            "requestIntentContract": {"requiredIntents": [requirement]},
        }
    )

    result = AgentAutonomyController(
        provider=_OverallocatedDraftGoalProvider(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert "deterministic_cardinality_fallback" not in result.decision.reason_codes


def test_lite_draft_cannot_fallback_while_trip_dates_are_pending():
    context = _authoritative_two_day_autonomy_context()
    context["resolvedTripDates"]["status"] = "pending"

    result = AgentAutonomyController(
        provider=_FullTimeoutLiteDraftProvider(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.controller_lite_succeeded is True
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert "lite_high_risk_write_authority_forbidden" in result.decision.reason_codes
    assert "deterministic_cardinality_fallback" not in result.decision.reason_codes


def test_policy_rejected_draft_cannot_fallback_without_authoritative_goal_contract():
    context = _authoritative_two_day_autonomy_context()
    requirement = context["observation"]["requirementCoverage"]["required"][0]
    context["observation"]["request"]["intentContract"]["requiredIntents"] = []
    assert AgentAutonomyController._normalization_context(context)["authoritativeGoalLedger"] is False
    observation = AgentObservationBuilder().build(
        {
            "latestUserMessage": "北京高校两日游",
            "effectiveUserMessage": "北京高校两日游",
            "requestIntentContract": {"requiredIntents": [requirement]},
        }
    )

    result = AgentAutonomyController(
        provider=_MissingRequiredDraftGoalProvider(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )

    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    assert result.source == "safe_fallback"
    assert "deterministic_cardinality_fallback" not in result.decision.reason_codes


def test_repair_queue_saturation_cannot_reuse_initial_full_provider_invocation(monkeypatch):
    class ScriptedWorkerSlots:
        def __init__(self):
            self.acquire_results = iter([True, False, True])

        def acquire(self, *, blocking):
            assert blocking is False
            return next(self.acquire_results)

        def release(self):
            return None

    monkeypatch.setattr(AgentAutonomyController, "_decision_worker_slots", ScriptedWorkerSlots())
    result = AgentAutonomyController(
        provider=_FullAndLiteInvalidStructuredResponseProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.decision.primary_action == "ask_user"
    assert "server_directive_after_controller_invalid_response" not in result.decision.reason_codes
    assert [(item["callKind"], item["providerInvoked"]) for item in result.controller_performance] == [
        ("full", True),
        ("repair", False),
    ]
    assert result.controller_performance[1]["captureState"] == "worker_queue_saturated"


def test_unknown_internal_controller_errors_remain_fail_closed():
    result = AgentAutonomyController(
        provider=_FullAndLiteInternalErrorProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.decision.primary_action == "ask_user"
    assert result.controller_lite_called is False
    assert result.controller_failures[0].failure_class == "controller_internal_error"
    assert result.controller_failures[0].retryable is False


def test_full_and_lite_timeout_without_resolved_dates_remains_fail_closed():
    context = _authoritative_two_day_autonomy_context()
    context.pop("resolvedTripDates")

    result = AgentAutonomyController(
        provider=_FullAndLiteTimeoutProvider(),
        fallback_decision_resolver=AgentDecisionArbitrator(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.3,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.decision.primary_action == "ask_user"
    assert result.source == "safe_fallback"
    assert "server_directive_after_controller_timeout" not in result.decision.reason_codes


def test_worker_queue_saturation_cannot_masquerade_as_provider_double_timeout():
    acquired = 0
    try:
        for _ in range(4):
            assert AgentAutonomyController._decision_worker_slots.acquire(blocking=False)
            acquired += 1
        result = AgentAutonomyController(
            provider=_FullAndLiteTimeoutProvider(),
            fallback_decision_resolver=AgentDecisionArbitrator(),
            decision_timeout_seconds=0.5,
            lite_timeout_seconds=0.3,
            total_budget_seconds=1.0,
        ).decide(
            "北京高校两日游",
            {},
            _authoritative_two_day_autonomy_context(),
            available_tools={"resolve_poi", "patch_itinerary"},
            runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        )
    finally:
        for _ in range(acquired):
            AgentAutonomyController._decision_worker_slots.release()

    assert result.decision.primary_action == "ask_user"
    assert "server_directive_after_controller_timeout" not in result.decision.reason_codes
    assert [failure.failure_class for failure in result.controller_failures] == [
        "provider_timeout",
        "provider_timeout",
    ]
    assert all(item["providerInvoked"] is False for item in result.controller_performance)
    assert all(item["captureState"] == "worker_queue_saturated" for item in result.controller_performance)


def test_controller_deadline_reports_current_body_read_elapsed_after_headers():
    release_body = Event()
    body_finished = Event()

    class ReadingProvider:
        model = "reading-controller"

        def prepare_controller_performance(self, autonomy_context, sink, *, call_kind):
            sink.update(
                {
                    "callKind": call_kind,
                    "payloadBytes": 400,
                    "contextCharCounts": {"latestUserMessage": 8},
                    "responseHeadersReceived": True,
                    "preHeaderWaitDurationMs": 5,
                    "ttfbDurationMs": 5,
                    "readDurationMs": None,
                    "_readStartedMonotonic": time.monotonic(),
                    "promptCacheSupported": False,
                    "promptCacheHit": None,
                }
            )

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            try:
                release_body.wait(timeout=1.0)
            finally:
                body_finished.set()
            raise TimeoutError("body_read_timeout")

    try:
        result = AgentAutonomyController(
            provider=ReadingProvider(),
            decision_timeout_seconds=0.08,
            # This test exercises the per-call body-read deadline.  Keep a
            # separate orchestration reserve so full-suite scheduler load cannot
            # consume the whole turn budget before the provider worker starts.
            lite_timeout_seconds=0.25,
            total_budget_seconds=0.33,
            max_schema_repair_attempts=0,
        ).decide(
            "北京两日游",
            {},
            {"schemaVersion": "model-first-autonomy-context-v2", "latestUserMessage": "北京两日游"},
            available_tools=set(),
            runtime_budget_tools=set(),
        )

        evidence = result.to_event_metadata()["controllerPerformance"][0]
        assert evidence["captureState"] == "controller_deadline_snapshot"
        assert evidence["responseHeadersReceived"] is True
        assert evidence["readDurationMs"] is None
        assert evidence["currentReadElapsedMs"] >= 0
        assert evidence["promptCacheSupported"] is False
        assert evidence["promptCacheHit"] is None
    finally:
        release_body.set()
        assert body_finished.wait(timeout=1.0)


def test_controller_performance_snapshot_keeps_lengths_and_usage_but_not_model_content():
    evidence = AgentAutonomyController._controller_performance_snapshot(
        {
            "callKind": "full",
            "captureState": "completed",
            "providerInvoked": True,
            "httpStatus": 200,
            "responseBytes": 876,
            "finishReason": "stop",
            "contentLength": 321,
            "reasoningContentLength": 654,
            "tokenUsage": {
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "total_tokens": 300,
            },
            "content": "must-not-leak",
            "reasoning_content": "must-not-leak",
            "authorization": "must-not-leak",
        }
    )

    assert evidence["httpStatus"] == 200
    assert evidence["responseBytes"] == 876
    assert evidence["finishReason"] == "stop"
    assert evidence["contentLength"] == 321
    assert evidence["reasoningContentLength"] == 654
    assert evidence["tokenUsage"] == {
        "completion_tokens": 200,
        "prompt_tokens": 100,
        "total_tokens": 300,
    }
    assert "content" not in evidence
    assert "reasoning_content" not in evidence
    assert "authorization" not in evidence


def test_body_read_gateway_timeout_remains_retryable_and_enters_lite_controller():
    class BodyReadTimeoutThenLiteProvider(_FullTimeoutLiteDraftProvider):
        def __init__(self):
            self.lite_calls = 0

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            raise HTTPException(status_code=504, detail="Provider response body read timed out")

        def decide_autonomy_lite(self, autonomy_context, *, timeout_seconds):
            self.lite_calls += 1
            return super().decide_autonomy_lite(autonomy_context, timeout_seconds=timeout_seconds)

    provider = BodyReadTimeoutThenLiteProvider()
    result = AgentAutonomyController(
        provider=provider,
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.4,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.controller_failures[0].failure_class == "provider_timeout"
    assert result.controller_failures[0].retryable is True
    assert provider.lite_calls == 1
    assert result.controller_lite_succeeded is True
    assert result.decision_path == "lite"
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True


def test_lite_draft_classification_uses_server_cardinality_directive_without_model_write_authority():
    result = AgentAutonomyController(
        provider=_FullTimeoutLiteDraftProvider(),
        decision_timeout_seconds=0.5,
        lite_timeout_seconds=0.4,
        total_budget_seconds=1.0,
    ).decide(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert result.controller_lite_succeeded is True
    assert result.decision_path == "lite"
    assert result.decision.primary_action == "draft_itinerary"
    assert result.gated_decision.accepted is True
    assert result.decision.action_directive is not None
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert "lite_classification_server_directive" in result.decision.reason_codes


def test_draft_route_policy_and_schedule_hints_are_typed_without_model_occurrence_identity() -> None:
    from pydantic import ValidationError

    from src.services.agent_autonomy_service import ModelDecisionV3

    payload = {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus_visit"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校与晚间公园",
                    "requiredGoalIds": ["goal_campus_visit"],
                    "requiredGoalCounts": {"goal_campus_visit": 1},
                    "optionalGoalIds": [],
                    "pace": "relaxed",
                    "maxRouteAnchors": 3,
                }
            ],
            "routePlanningPolicy": {
                "objective": "least_generalized_cost",
                "source": "controller_estimate",
                "allowExperienceDetour": True,
                "detourEnvelope": {
                    "maxGeneralizedCostDelta": 20,
                    "maxDetourRatio": 0.2,
                },
            },
            "occurrenceScheduleHints": [
                {
                    "goalId": "goal_campus_visit",
                    "dayNumber": 1,
                    "dayPart": "morning",
                    "sequence": 1,
                    "durationEstimate": {"min": 90, "preferred": 120, "max": 150},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.8,
                }
            ],
            "routeGapSupplementHints": [
                {
                    "dayNumber": 1,
                    "intentType": "park",
                    "experienceFamily": "park_relax",
                    "queryHint": "城市公园",
                    "durationEstimate": {"min": 45, "preferred": 60, "max": 90},
                    "confidence": 0.75,
                }
            ],
        },
    }

    decision = ModelDecisionV3.model_validate(payload)
    directive = decision.action_directive
    assert directive.route_planning_policy.source == "controller_estimate"
    assert directive.occurrence_schedule_hints[0].duration_estimate.preferred == 120
    assert directive.route_gap_supplement_hints[0].experience_family == "park_relax"
    with pytest.raises(ValidationError):
        ModelDecisionV3.model_validate(
            {
                **payload,
                "actionDirective": {
                    **payload["actionDirective"],
                    "occurrenceScheduleHints": [
                        {
                            **payload["actionDirective"]["occurrenceScheduleHints"][0],
                            "occurrenceId": "model-cannot-sign-this",
                        }
                    ],
                },
            }
        )
    with pytest.raises(ValidationError):
        ModelDecisionV3.model_validate(
            {
                **payload,
                "actionDirective": {
                    **payload["actionDirective"],
                    "routeGapSupplementHints": [
                        {
                            **payload["actionDirective"]["routeGapSupplementHints"][0],
                            "occurrenceId": "model-cannot-sign-route-gap-occurrence",
                        }
                    ],
                },
            }
        )


def test_controller_draft_rejects_missing_schedule_hint_coverage_for_scheduled_occurrences() -> None:
    """Live -10: scheduled goal/day pairs need typed producer timing evidence."""

    raw = {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus_visit", "goal_night_view", "goal_meal"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校与夜景",
                    "requiredGoalIds": ["goal_campus_visit", "goal_night_view"],
                    "requiredGoalCounts": {"goal_campus_visit": 1, "goal_night_view": 1},
                    "optionalGoalIds": ["goal_meal"],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "另一所高校",
                    "requiredGoalIds": ["goal_campus_visit"],
                    "requiredGoalCounts": {"goal_campus_visit": 1},
                    "optionalGoalIds": ["goal_meal"],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 2,
            "searchPriority": ["goal_campus_visit", "goal_night_view"],
            "occurrenceScheduleHints": [],
        },
    }
    context = {
        "goalRequirements": [
            {
                "goalId": "goal_campus_visit",
                "intentType": "campus_visit",
                "requirementLevel": "required",
                "requiredMin": 2,
                "maxCount": 2,
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requirementLevel": "required",
                "requiredMin": 1,
                "maxCount": 1,
                "allowedDayNumbers": [1],
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requirementLevel": "soft_experience",
                "requiredMin": 2,
                "maxCount": 2,
                "allowedDayNumbers": [1, 2],
            },
        ]
    }

    with pytest.raises(
        DecisionNormalizationError,
        match="draft_occurrence_schedule_hint_coverage_incomplete",
    ):
        AgentAutonomyController()._validate_provider_decision(raw, context)


def _schedule_contract_draft(*, hints: list[dict]) -> dict:
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus", "goal_night"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "campus and night view",
                    "requiredGoalIds": ["goal_campus", "goal_night"],
                    "requiredGoalCounts": {"goal_campus": 1, "goal_night": 1},
                    "optionalGoalIds": [],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                }
            ],
            "optionalExperienceBudget": 0,
            "searchPriority": ["goal_campus", "goal_night"],
            "occurrenceScheduleHints": hints,
        },
    }


def _schedule_contract_context() -> dict:
    return {
        "goalRequirements": [
            {
                "goalId": "goal_campus",
                "intentType": "campus_visit",
                "requirementLevel": "required",
                "requiredMin": 1,
                "maxCount": 1,
                "allowedDayNumbers": [1],
            },
            {
                "goalId": "goal_night",
                "intentType": "night_view",
                "requirementLevel": "required",
                "requiredMin": 1,
                "maxCount": 1,
                "allowedDayNumbers": [1],
            },
        ],
    }


def _complete_schedule_hints() -> list[dict]:
    return [
        {
            "goalId": "goal_campus",
            "dayNumber": 1,
            "dayPart": "morning",
            "sequence": 1,
            "preferredStartTime": "09:00",
            "durationEstimate": {"min": 90, "preferred": 120, "max": 150},
            "estimateSource": "controller_estimate",
            "confidence": 0.8,
        },
        {
            "goalId": "goal_night",
            "dayNumber": 1,
            "dayPart": "night",
            "sequence": 2,
            "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
            "estimateSource": "controller_estimate",
            "confidence": 0.75,
        },
    ]


def test_controller_draft_schedule_hint_coverage_is_identity_based_and_order_independent() -> None:
    controller = AgentAutonomyController()
    context = _schedule_contract_context()

    forward = controller._validate_provider_decision(
        _schedule_contract_draft(hints=_complete_schedule_hints()),
        context,
    )[0]
    reverse = controller._validate_provider_decision(
        _schedule_contract_draft(hints=list(reversed(_complete_schedule_hints()))),
        context,
    )[0]

    assert forward.primary_action == "draft_itinerary"
    assert reverse.primary_action == "draft_itinerary"
    assert {(hint.goal_id, hint.day_number) for hint in forward.action_directive.occurrence_schedule_hints} == {
        (hint.goal_id, hint.day_number) for hint in reverse.action_directive.occurrence_schedule_hints
    }


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda hints: [*hints, dict(hints[0])],
            "draft_occurrence_schedule_hint_duplicate",
        ),
        (
            lambda hints: [
                *hints,
                {
                    **hints[0],
                    "goalId": "goal_extra",
                    "dayNumber": 2,
                },
            ],
            "draft_occurrence_schedule_hint_extra",
        ),
        (
            lambda hints: [{key: value for key, value in hints[0].items() if key != "preferredStartTime"}, hints[1]],
            "draft_occurrence_schedule_hint_clock_required",
        ),
        (
            lambda hints: [{**hints[0], "preferredStartTime": "99:99"}, hints[1]],
            "draft_occurrence_schedule_hint_clock_invalid",
        ),
    ],
    ids=["duplicate", "extra", "non-night-no-clock", "invalid-clock"],
)
def test_controller_draft_schedule_hint_semantics_fail_closed(mutator, expected_error) -> None:
    with pytest.raises(DecisionNormalizationError, match=expected_error):
        AgentAutonomyController()._validate_provider_decision(
            _schedule_contract_draft(hints=mutator(_complete_schedule_hints())),
            _schedule_contract_context(),
        )


def test_live_shaped_missing_schedule_hints_use_one_call_server_fallback_without_lite() -> None:
    class ScheduleRepairProvider:
        model = "recorded-schedule-controller"

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.lite_calls = 0
            self.repair_contract: dict = {}

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            call_kind = "repair" if repair_feedback else "full"
            self.calls.append(call_kind)
            if not repair_feedback:
                return _schedule_contract_draft(hints=[])
            self.repair_contract = json.loads(repair_feedback)
            return _schedule_contract_draft(hints=_complete_schedule_hints())

        def decide_autonomy_lite(self, _context, *, timeout_seconds):
            self.lite_calls += 1
            raise AssertionError("successful schema repair must not invoke Lite")

    provider = ScheduleRepairProvider()
    context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "one campus and one night view",
        "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01"]},
        "observation": {
            "request": {"intentContract": {"requiredIntents": _schedule_contract_context()["goalRequirements"]}},
            "requirementCoverage": {"required": _schedule_contract_context()["goalRequirements"]},
        },
    }

    result = AgentAutonomyController(provider=provider).decide_shadow(
        "one campus and one night view",
        {},
        context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == ["full"]
    assert provider.lite_calls == 0
    assert result.schema_repair_attempts == 0
    assert result.controller_full_succeeded is False
    assert result.controller_lite_succeeded is False
    assert result.source == "safe_fallback"
    assert result.decision.primary_action == "draft_itinerary"
    assert result.decision.action_directive.occurrence_schedule_hints == []
    assert "deterministic_cardinality_fallback" in result.decision.reason_codes
    assert result.decision.action_directive.day_strategies[0].required_goal_counts == {
        "goal_campus": 1,
        "goal_night": 1,
    }


def test_live_shaped_missing_theme_and_estimate_source_repairs_from_explicit_contract() -> None:
    """Real debug-bundle shape: Full and repair omitted nested required fields."""

    invalid_decision = {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": [],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                }
            ],
            "occurrenceScheduleHints": [
                {
                    "goalId": "goal_campus",
                    "dayNumber": 1,
                    "dayPart": "morning",
                    "preferredStartTime": "09:00",
                    "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                    "sequence": 1,
                    "confidence": 0.7,
                }
            ],
        },
    }

    class LiveShapeRepairProvider:
        model = "recorded-live-controller"

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.lite_calls = 0
            self.repair_contract: dict = {}

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            self.calls.append("repair" if repair_feedback else "full")
            if not repair_feedback:
                return invalid_decision
            self.repair_contract = json.loads(repair_feedback)
            rules = self.repair_contract.get("draftSchedulingRules") or {}
            checklist = self.repair_contract.get("requiredFieldChecklist") or {}
            nested_fields_are_explicit = (
                rules.get("everyDayStrategyRequiresNonEmptyTheme") is True
                and rules.get("everyOccurrenceScheduleHintRequiresEstimateSource") is True
                and "theme" in checklist.get("actionDirective.dayStrategies[]", [])
                and "estimateSource" in checklist.get("actionDirective.occurrenceScheduleHints[]", [])
            )
            if not nested_fields_are_explicit:
                return invalid_decision
            return self.repair_contract["minimalExample"]

        def decide_autonomy_lite(self, _context, *, timeout_seconds):
            self.lite_calls += 1
            raise AssertionError("one bounded repair must close the structural failure without Lite")

    provider = LiveShapeRepairProvider()
    result = AgentAutonomyController(provider=provider).decide_shadow(
        "北京高校两日游",
        {},
        _authoritative_two_day_autonomy_context(),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert provider.calls == ["full", "repair"]
    assert provider.lite_calls == 0
    assert result.schema_repair_attempts == 1
    assert result.controller_full_succeeded is True
    assert result.controller_lite_succeeded is False
    assert result.source == "controller"
    assert result.decision.primary_action == "draft_itinerary"
    assert result.decision.action_directive.day_strategies[0].theme
    assert result.decision.action_directive.occurrence_schedule_hints[0].estimate_source


def test_live_adjacent_generic_draft_repair_requires_sequence_and_confidence_within_fixed_http_budget() -> None:
    """The live Full -> one-repair path must author every strict nested hint field."""

    goals = [
        {
            "goalId": "goal_campus",
            "intentType": "campus_visit",
            "requirementLevel": "required",
            "requiredMin": 2,
            "preferredCount": 2,
            "maxCount": 2,
            "cardinalitySource": "explicit_every_day",
            "distributionPolicy": "every_allowed_day",
            "allowedDayNumbers": [1, 2],
        },
        {
            "goalId": "goal_night",
            "intentType": "night_view",
            "requirementLevel": "required",
            "requiredMin": 1,
            "preferredCount": 1,
            "maxCount": 1,
            "cardinalitySource": "explicit_singular",
            "distributionPolicy": "one_of_allowed_days",
            "allowedDayNumbers": [1, 2],
        },
        {
            "goalId": "goal_meal",
            "intentType": "meal",
            "requirementLevel": "soft_experience",
            "requiredMin": 0,
            "preferredCount": 2,
            "maxCount": 2,
            "cardinalitySource": "explicit_every_day",
            "distributionPolicy": "every_allowed_day",
            "allowedDayNumbers": [1, 2],
        },
    ]
    invalid_full = {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus", "goal_night", "goal_meal"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "campus, meal, and night view",
                    "requiredGoalIds": ["goal_campus", "goal_night"],
                    "requiredGoalCounts": {"goal_campus": 1, "goal_night": 1},
                    "optionalGoalIds": ["goal_meal"],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "campus and local lunch",
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": ["goal_meal"],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 2,
            "searchPriority": ["goal_campus", "goal_night", "goal_meal"],
            "occurrenceScheduleHints": [
                {
                    "goalId": "goal_campus",
                    "dayNumber": 1,
                    "dayPart": "morning",
                    "preferredStartTime": "09:00",
                    "durationEstimate": 90,
                    "estimateSource": "controller_estimate",
                },
                {
                    "goalId": "goal_meal",
                    "dayNumber": 1,
                    "dayPart": "noon",
                    "preferredStartTime": "12:00",
                    "durationEstimate": 75,
                    "estimateSource": "controller_estimate",
                },
                {
                    "goalId": "goal_night",
                    "dayNumber": 1,
                    "dayPart": "night",
                    "durationEstimate": 90,
                    "estimateSource": "controller_estimate",
                },
                {
                    "goalId": "goal_campus",
                    "dayNumber": 2,
                    "dayPart": "morning",
                    "preferredStartTime": "09:00",
                    "durationEstimate": 90,
                    "estimateSource": "controller_estimate",
                },
                {
                    "goalId": "goal_meal",
                    "dayNumber": 2,
                    "dayPart": "noon",
                    "preferredStartTime": "12:00",
                    "durationEstimate": 75,
                    "estimateSource": "controller_estimate",
                },
            ],
        },
    }
    autonomy_context = {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "北京高校两日游，每天午餐并安排一次夜景",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "observation": {
            "request": {"intentContract": {"requiredIntents": goals}},
            "requirementCoverage": {"required": goals},
        },
    }
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=30,
    )
    payloads: list[dict] = []
    returned_decisions: list[dict] = []
    lite_calls: list[bool] = []

    def fake_post(payload, *, timeout_seconds=None):
        payloads.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        if len(payloads) == 1:
            returned_decisions.append(invalid_full)
            return json.dumps(invalid_full, ensure_ascii=False)
        repair_contract = json.loads(payload["messages"][2]["content"].split("Repair contract: ", 1)[1])
        repaired = json.loads(json.dumps(repair_contract["minimalExample"], ensure_ascii=False))
        system_prompt = " ".join(payload["messages"][0]["content"].split())
        nested_fields_are_mandatory = (
            "Every occurrenceScheduleHints item MUST include goalId, dayNumber, dayPart, sequence (integer 1-12)"
            in system_prompt
            and "durationEstimate with required min/preferred/max" in system_prompt
            and "confidence (number 0-1)" in system_prompt
        )
        if not nested_fields_are_mandatory:
            for hint in repaired["actionDirective"]["occurrenceScheduleHints"]:
                hint.pop("sequence", None)
                hint.pop("confidence", None)
        returned_decisions.append(repaired)
        return json.dumps(repaired, ensure_ascii=False)

    def fail_lite(_context, *, timeout_seconds):
        lite_calls.append(True)
        raise AssertionError("one bounded draft repair must not invoke Lite")

    provider._post = fake_post
    provider.decide_autonomy_lite = fail_lite
    result = AgentAutonomyController(provider=provider).decide_shadow(
        autonomy_context["latestUserMessage"],
        {},
        autonomy_context,
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
    )

    assert [len(payload["messages"]) for payload in payloads] == [2, 3]
    assert [item["primaryAction"] for item in returned_decisions] == [
        "draft_itinerary",
        "draft_itinerary",
    ]
    assert lite_calls == []
    assert result.schema_repair_attempts == 1
    assert result.controller_full_succeeded is True
    assert result.controller_lite_succeeded is False
    assert result.source == "controller"
    assert result.decision.primary_action == "draft_itinerary"
    assert all(
        len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT for payload in payloads
    )
    assert json.loads(payloads[1]["messages"][1]["content"])["repairMode"] == "draft_itinerary_schema_only"
    strict_repaired = ModelDecisionV3.model_validate(returned_decisions[1])
    hints = strict_repaired.action_directive.occurrence_schedule_hints
    assert len(hints) == 5
    assert all(1 <= hint.sequence <= 12 for hint in hints)
    assert all(0 <= hint.confidence <= 1 for hint in hints)

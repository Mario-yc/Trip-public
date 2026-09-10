from __future__ import annotations

import json

from src.services.controller_context_projection_service import (
    FULL_REQUEST_BYTE_LIMIT,
    ControllerContextProjectionService,
)


def test_deep_full_compaction_preserves_every_original_request_clause():
    clauses = [{"clauseId": "clause_1", "sourceText": "上午参观博物馆", "goalIds": ["goal_1"]}]
    payload = {
        "schemaVersion": "full-controller-context-v1",
        "latestUserMessage": "上午参观博物馆",
        "requestActivityClauses": clauses,
        "allowedActions": ["draft_itinerary", "ask_user"],
        "untrustedSourceEvidence": "bounded public evidence " * 1000,
    }
    result, steps = ControllerContextProjectionService._fit_full_payload(payload)
    assert "minimal_executable_projection" in steps
    assert result.get("requestActivityClauses") == clauses


def _large_context() -> dict:
    return {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "北京高校两日游，晚上看一次夜景，每天午餐体验当地特色美食。",
        "effectiveUserMessage": "北京高校两日游，晚上看一次夜景，每天午餐体验当地特色美食。",
        "selectedCity": "北京",
        "activeVersionId": None,
        "hasItinerary": False,
        "requestIntentContract": {
            "fingerprint": "request-fingerprint",
            "dayCount": 2,
            "goals": [
                {
                    "goalId": "goal_campus",
                    "requirementLevel": "hard",
                    "requiredMin": 1,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "allowedDayNumbers": [1, 2],
                },
                {
                    "goalId": "goal_meal",
                    "requirementLevel": "soft_experience",
                    "requiredMin": 0,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "allowedDayNumbers": [1, 2],
                },
            ],
            "largeServerOnlyRules": "x" * 20_000,
            "clarificationDimensions": [
                {
                    "dimensionId": "night_view.experience_mode",
                    "status": "unresolved",
                    "allowedSemanticFields": [
                        "experienceFamilies",
                        "accessPolicy",
                    ],
                }
            ],
            "completionCriteria": [
                {
                    "intentType": "night_view",
                    "requiredResolvedDimensions": ["night_view.experience_mode"],
                }
            ],
        },
        "observation": {
            "itinerary": {"lifecycleState": "empty_scaffold", "activeVersionId": None, "meaningfulSegmentCount": 0},
            "planningAttempt": {
                "persisted": False,
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
                "requestContractFingerprint": "request-fingerprint",
            },
            "unresolvedSlots": [
                {
                    "goalId": "goal_campus",
                    "briefId": "brief_focus",
                    "poolId": "pool_day2",
                    "planningSlotId": "slot_day2",
                    "dayNumber": 2,
                    "timeWindow": {"start": "10:20", "end": "11:35"},
                }
            ],
            "largeObservationPayload": "y" * 12_000,
        },
        "continuationContext": {
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": "portfolio_root",
            "requestContractFingerprint": "request-fingerprint",
            "expectedBaseVersionId": None,
            "focusBriefId": "brief_focus",
            "largeCheckpoint": "z" * 8_000,
        },
        "memoryRules": {"large": "m" * 8_000},
        "supportedPatchOperations": ["replace_segment_poi"] * 100,
        "clarificationCheckpoint": {
            "schemaVersion": "clarification-checkpoint-v1",
            "checkpointId": "clarify_1",
            "planningRootId": "turn_root",
            "requestFingerprint": "request-fingerprint",
            "fingerprint": "checkpoint-fingerprint",
            "contractVersion": 2,
            "resolvedAnswers": [
                {
                    "dimensionId": "night_view.cardinality",
                    "semanticValue": {"occurrencePolicy": "every_available_evening"},
                    "source": "structured_option",
                }
            ],
            "experienceSpecs": [
                {
                    "intentType": "night_view",
                    "frequency": "every_available_evening",
                    "allowedDayNumbers": [1, 2],
                }
            ],
            "status": "answered",
        },
        "candidateGapSummary": {
            "schemaVersion": "candidate-gap-summary-v1",
            "missingOccurrenceCount": 2,
            "rejectedReasonCounts": {"access_evidence_missing": 7},
        },
        "provisionalGoalOccurrenceProjection": {
            "schemaVersion": "clarification-goal-occurrence-plan-v1",
            "projectedPlacements": [
                {
                    "projectionId": "goal_night_view:day:1",
                    "intentType": "night_view",
                    "dayNumber": 1,
                },
                {
                    "projectionId": "goal_night_view:day:2",
                    "intentType": "night_view",
                    "dayNumber": 2,
                },
            ],
        },
    }


def _v4_completed_clarification_context() -> tuple[dict, dict]:
    """Relevant server-authored shape from the 2026-08-29 V4 failure bundle."""

    context = _large_context()
    context["latestUserMessage"] = "确认并开始规划"
    context["selectedCity"] = "北京"
    night_semantics = {
        "experienceFamilies": ["public_city_view"],
        "accessPolicy": "public_outdoor_or_verified_controlled_access",
        "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
        "timeWindow": {"start": "19:00", "end": "22:00"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        "evidenceFreshness": {
            "maxAgeHours": 24,
            "requiredForControlledAccess": True,
        },
        "confidence": 0.9,
    }
    meal_semantics = {
        "intentType": "meal",
        "frequency": "every_allowed_day",
        "allowedDayNumbers": [1, 2],
        "experienceFamilies": ["meal"],
        "accessPolicy": "verified_amap_food_service",
        "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
        "timeWindow": {"start": "12:00", "end": "13:15"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 15,
            "maxDetourRatio": 0.15,
        },
        "evidenceFreshness": {
            "maxAgeHours": 24,
            "requiredForControlledAccess": False,
            "requiredForPublicOutdoor": False,
            "allowExplicitNoClosure": False,
        },
        "confidence": 0.84,
        "specFingerprint": "6f9ff8ba89c54c08111d4f2b7e1b072f94f7b845602a6111d62cadf6839df78a",
    }
    resolved_answers = [
        {
            "dimensionId": "night_view.cardinality",
            "semanticValue": {
                "frequency": 2,
                "occurrencePolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
            "source": "structured_option",
            "sourceUserTurnId": "turn_e53f0dad07f6",
            "optionId": "opt_night_every_day",
            "label": "每晚都安排夜景",
        },
        {
            "dimensionId": "night_view.experience_mode",
            "semanticValue": night_semantics,
            "source": "structured_option",
            "sourceUserTurnId": "turn_e53f0dad07f6",
            "optionId": "opt_night_public",
            "label": "公共开放的城市观景点",
        },
        {
            "dimensionId": "route_decision.detour_tolerance",
            "semanticValue": {
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 15,
                    "maxDetourRatio": 0.15,
                }
            },
            "source": "structured_option",
            "sourceUserTurnId": "turn_e53f0dad07f6",
            "optionId": "opt_detour_low",
            "label": "尽量少绕路",
        },
    ]
    context["clarificationCheckpoint"] = {
        "schemaVersion": "clarification-checkpoint-v2",
        "checkpointId": "clarify_e3ac5a5715814902",
        "planningRootId": "turn_3c131b5e554c",
        "requestFingerprint": "5689113988d33230bcee1c274083c363d0a0ced07cfd152fadbf4cfcae903bd6",
        "fingerprint": "a77df3642ee8f67cc1007d480ee35ca7e00660d79f51c612b0bfcdcb174d70c6",
        "contractVersion": 2,
        "resolvedAnswers": resolved_answers,
        "status": "answered",
        "sourceUserTurnId": "turn_e53f0dad07f6",
        "sourceAssistantTurnId": "turn_b0d4bd85331a",
    }
    context["experienceSpecs"] = [
        meal_semantics,
        {
            "intentType": "night_view",
            "frequency": 2,
            "allowedDayNumbers": [1, 2],
            **night_semantics,
        },
    ]
    context["requestIntentContract"].update(
        {
            "fingerprint": "e517c1e7d82593a58544112a98656e2ee79e947966250830bf3c27aca8cf925d",
            "clarificationDimensions": [
                {
                    "dimensionId": "night_view.cardinality",
                    "intentType": "night_view",
                    "status": "resolved",
                    "impactCode": "required_occurrence_and_route_evidence_count",
                    "candidateScope": {
                        "intentType": "night_view",
                        "allowedDayNumbers": [1, 2],
                        "tripDayCount": 2,
                    },
                    "allowedSemanticFields": [
                        "frequency",
                        "occurrencePolicy",
                        "allowedDayNumbers",
                    ],
                },
                {
                    "dimensionId": "night_view.experience_mode",
                    "intentType": "night_view",
                    "status": "resolved",
                    "impactCode": "candidate_admission_route_and_access_policy",
                    "candidateScope": {
                        "intentType": "night_view",
                        "allowedDayNumbers": [1, 2],
                        "tripDayCount": 2,
                    },
                    "allowedSemanticFields": list(night_semantics),
                },
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "intentType": "route_decision",
                    "status": "resolved",
                    "impactCode": "provider_route_matrix_acceptance_threshold",
                    "candidateScope": {
                        "routeDecisionContractFingerprint": (
                            "f385104bfd7d64a46ed5132c0b61a4b8217c0a7c84afd3545c9c4ffbcca2757a"
                        ),
                        "missingFields": ["detourTolerance"],
                        "transportMode": "transit",
                    },
                    "allowedSemanticFields": [
                        "detourTolerance",
                        "adjacentLegConstraint",
                    ],
                },
            ],
            "completionCriteria": [
                {
                    "intentType": "night_view",
                    "requiredResolvedDimensions": [
                        "night_view.cardinality",
                        "night_view.experience_mode",
                    ],
                    "requiredExperienceSpecFields": list(night_semantics),
                }
            ],
            "provisionalGoalOccurrenceProjection": {
                "schemaVersion": "provisional-goal-occurrence-projection-v1",
                "authority": "request_intent_contract",
                "checkpointId": "clarify_e3ac5a5715814902",
                "planningRootId": "turn_3c131b5e554c",
                "contractVersion": 2,
                "source": "controller_semantic_choice",
                "goalConstraints": [
                    {
                        "goalId": "goal_campus_visit",
                        "intentType": "campus_visit",
                        "requiredCount": 2,
                        "allowedDayNumbers": [1, 2],
                        "requirementLevel": "required",
                        "priorityTier": "hard",
                        "userExplicit": True,
                        "source": "simple_open_explicit_trip_theme_every_day",
                        "distributionPolicy": "every_allowed_day",
                        "placementState": "exact_from_every_allowed_day",
                    },
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredCount": 2,
                        "allowedDayNumbers": [1, 2],
                        "requirementLevel": "required",
                        "priorityTier": "hard",
                        "userExplicit": True,
                        "source": "explicit_user_clarification",
                        "distributionPolicy": "spread_across_distinct_days",
                        "placementState": "requires_controller_day_strategy",
                        **night_semantics,
                    },
                    {
                        "goalId": "goal_meal",
                        "requiredCount": 2,
                        "requirementLevel": "soft_experience",
                        "priorityTier": "explicit_soft",
                        "userExplicit": True,
                        "source": "explicit_every_day",
                        "distributionPolicy": "every_allowed_day",
                        "placementState": "exact_from_every_allowed_day",
                        **meal_semantics,
                    },
                ],
                "projectedPlacements": [
                    {
                        "projectionId": f"goal_{intent}:day:{day}:projection:{day}",
                        "goalId": f"goal_{intent}",
                        "intentType": intent,
                        "placementIndex": day,
                        "dayNumber": day,
                        "source": "explicit_every_allowed_day_constraint",
                    }
                    for intent in ("campus_visit", "meal")
                    for day in (1, 2)
                ],
                "unresolvedDimensionIds": [],
                "status": "awaiting_controller_day_strategy",
                "sourceContractFingerprint": ("e517c1e7d82593a58544112a98656e2ee79e947966250830bf3c27aca8cf925d"),
                "fingerprint": "b7d22ccab0591c6593b399c54b15820041b785421fd81f2460f6e4b2a0b83d5c",
            },
        }
    )
    context["provisionalGoalOccurrenceProjection"] = context["requestIntentContract"][
        "provisionalGoalOccurrenceProjection"
    ]
    context["candidateGapSummary"] = {}
    context["observation"]["unresolvedSlots"] = []
    normalization = {
        "goalRequirements": [
            {
                "goalId": "goal_campus_visit",
                "intentType": "campus_visit",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "requirementLevel": "required",
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "requirementLevel": "required",
                "distributionPolicy": "spread_across_distinct_days",
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "requirementLevel": "soft_experience",
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
        ],
        "requiredGoalCounts": {
            "goal_campus_visit": 2,
            "goal_night_view": 2,
        },
        "optionalGoalIds": ["goal_meal"],
        "availableDayNumbers": [1, 2],
        "routePlanningPolicyRequirement": {
            "required": True,
            "source": "controller_estimate",
            "status": "ready",
            "missingFields": [],
            "mobilityProfile": {
                "walkingPenaltyMinutesPerKm": 1.8,
                "transferPenaltyMinutes": 6,
                "waitTimeMultiplier": 1,
                "riskPenaltyMultiplier": 1,
            },
            "detourEnvelope": {
                "maxGeneralizedCostDelta": 15,
                "maxDetourRatio": 0.15,
            },
        },
    }
    return context, normalization


def test_full_and_lite_are_independent_typed_projections_with_hard_byte_limits():
    projection = ControllerContextProjectionService().build(
        _large_context(),
        allowed_actions=("draft_itinerary", "ask_user"),
        decision_contract={"schemaVersion": "agent-decision-contract-v3", "large": "c" * 20_000},
    )
    full = projection.full
    lite = projection.lite
    assert full["schemaVersion"] == "controller-context-full-v1"
    assert lite["schemaVersion"] == "controller-context-lite-v1"
    assert full is not lite
    assert len(json.dumps(full, ensure_ascii=False).encode("utf-8")) <= 15_360
    assert len(json.dumps(lite, ensure_ascii=False).encode("utf-8")) <= 8_192
    assert "decisionContract" not in full
    assert "decisionContract" not in lite
    assert "memoryRules" not in full
    assert "observation" not in full
    assert full.get("observation")["largeObservationPayload"] == "y" * 12_000
    assert "largeObservationPayload" not in json.dumps(full, ensure_ascii=False)
    serialized = json.dumps(dict(full), ensure_ascii=False)
    assert "every_available_evening" in serialized
    assert full["clarificationCheckpoint"]["checkpointId"] == "clarify_1"
    assert full["clarificationCheckpoint"]["planningRootId"] == "turn_root"
    assert full["clarificationCheckpoint"]["requestFingerprint"] == ("request-fingerprint")
    assert full["clarificationCheckpoint"]["fingerprint"] == ("checkpoint-fingerprint")
    assert full["clarificationCheckpoint"]["resolvedAnswers"]
    assert "compact_resolved_clarification_history" not in projection.telemetry["fullCompactionSteps"]
    assert full["experienceSpecs"][0]["intentType"] == "night_view"
    assert full["candidateGapSummary"]["missingOccurrenceCount"] == 2
    assert full["clarificationDimensions"][0]["dimensionId"] == ("night_view.experience_mode")
    assert full["clarificationDimensions"][0]["semanticFieldSchemas"] == {
        "experienceFamilies": {
            "type": "nonempty_token_array",
            "example": ["public_city_view", "waterfront_evening"],
        },
        "accessPolicy": {
            "type": "token",
            "example": "public_outdoor_or_verified_controlled_access",
        },
    }
    assert full["completionCriteria"][0]["intentType"] == "night_view"
    assert len(full["provisionalGoalOccurrenceProjection"]["projectedPlacements"]) == 2
    assert "requestIntentContract" not in lite
    assert "decisionContractRef" in full
    assert full["pendingSlots"][0]["planningSlotId"] == "slot_day2"
    assert lite["pendingSlots"][0]["dayNumber"] == 2


def test_normalization_empty_clarification_registry_overrides_stale_autonomy_context():
    context = _large_context()
    assert context["requestIntentContract"]["clarificationDimensions"]

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("draft_itinerary",),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
        normalization_context={"clarificationDimensions": []},
    )

    assert projection.full["clarificationDimensions"] == []


def test_normalization_clarification_registry_overrides_empty_autonomy_context():
    context = _large_context()
    context["requestIntentContract"]["clarificationDimensions"] = []
    authoritative_dimensions = [
        {
            "dimensionId": "route_decision.mobility_profile",
            "intentType": "route_decision",
            "status": "unresolved",
            "impactCode": "route_mobility_profile_required",
            "allowedSemanticFields": ["mobilityProfile"],
            "semanticOptions": [
                {"id": "transit_standard", "defaultLabel": "公共交通，标准节奏"},
                {"id": "driving_standard", "defaultLabel": "自驾，标准节奏"},
            ],
        },
        {
            "dimensionId": "route_decision.detour_tolerance",
            "intentType": "route_decision",
            "status": "unresolved",
            "impactCode": "provider_route_matrix_acceptance_threshold",
            "allowedSemanticFields": ["detourTolerance"],
            "semanticOptions": [
                {"id": "minimize_detour", "defaultLabel": "尽量少绕路"},
                {"id": "balanced", "defaultLabel": "均衡"},
            ],
        },
    ]

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("ask_user",),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
        normalization_context={"clarificationDimensions": authoritative_dimensions},
    )

    assert [item["dimensionId"] for item in projection.full["clarificationDimensions"]] == [
        "route_decision.mobility_profile",
        "route_decision.detour_tolerance",
    ]
    assert projection.full["clarificationDimensions"][0]["semanticOptions"] == [
        {"id": "transit_standard", "defaultLabel": "公共交通，标准节奏"},
        {"id": "driving_standard", "defaultLabel": "自驾，标准节奏"},
    ]
    assert projection.full["clarificationDimensions"][1]["semanticOptions"] == [
        {"id": "minimize_detour", "defaultLabel": "尽量少绕路"},
        {"id": "balanced", "defaultLabel": "均衡"},
    ]


def test_full_and_lite_use_effective_request_message_for_retry_projection():
    context = _large_context()
    context["latestUserMessage"] = "重试生成澄清问题"
    context["effectiveUserMessage"] = "今年国庆安排北京高校两日游"
    context["selectedAgentChoice"] = {
        "action": "retry_model_planning",
        "option": {"action": "retry_model_planning"},
    }

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("draft_itinerary",),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
    )

    assert projection.full["latestUserMessage"] == "今年国庆安排北京高校两日游"
    assert projection.lite["latestUserMessage"] == "今年国庆安排北京高校两日游"


def test_full_and_lite_keep_latest_message_for_non_retry_refinement():
    context = _large_context()
    context["latestUserMessage"] = "把第一天美术馆改到 15:30"
    context["effectiveUserMessage"] = "今年国庆安排北京高校两日游\n把第一天美术馆改到 15:30"

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("draft_itinerary",),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
    )

    assert projection.full["latestUserMessage"] == "把第一天美术馆改到 15:30"
    assert projection.lite["latestUserMessage"] == "把第一天美术馆改到 15:30"


def test_projection_telemetry_reports_only_section_lengths_and_hashes():
    projection = ControllerContextProjectionService().build(
        _large_context(),
        allowed_actions=("draft_itinerary", "ask_user"),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
    )
    assert projection.telemetry["fullPayloadBytes"] > projection.telemetry["litePayloadBytes"]
    assert projection.telemetry["fullPayloadBytes"] <= 15_360
    assert projection.telemetry["litePayloadBytes"] <= 8_192
    assert projection.telemetry["decisionContractHash"]
    assert set(projection.telemetry["fullSectionChars"]) == set(projection.full)
    assert "largeServerOnlyRules" not in json.dumps(projection.telemetry, ensure_ascii=False)


def test_complete_provider_request_payloads_stay_under_goal_limits():
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    projection = ControllerContextProjectionService().build(
        _large_context(),
        allowed_actions=("draft_itinerary", "ask_user"),
        decision_contract={"schemaVersion": "agent-decision-contract-v3", "large": "c" * 20_000},
    )
    provider = DeepSeekAgentProvider(api_key="test-key", model="test-model", timeout_seconds=1)
    captured: list[int] = []

    def fake_post(payload, *, timeout_seconds=None):
        captured.append(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        return "{}"

    provider._post = fake_post
    provider.decide_autonomy(projection.full, timeout_seconds=1)
    provider.decide_autonomy_lite(projection.lite, timeout_seconds=1)

    assert captured[0] <= 15_360
    assert captured[1] <= 8_192


def test_v4_completed_clarification_full_http_payload_stays_under_hard_limit():
    """The final Provider envelope from the V4 failure must remain executable."""

    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context, normalization = _v4_completed_clarification_context()
    allowed_actions = (
        "ask_user",
        "draft_itinerary",
        "read_itinerary",
        "resolve_poi",
        "optimize_route",
        "patch_itinerary",
        "verify_external_facts",
        "finish",
    )
    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=allowed_actions,
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
        normalization_context=normalization,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=1,
    )
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"

    provider.decide_autonomy(projection.full, timeout_seconds=1)

    assert len(captured) == 1
    final_http_payload = captured[0]
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT
    projected_context = json.loads(final_http_payload["messages"][1]["content"])
    assert projected_context["allowedActions"] == list(allowed_actions)
    assert projected_context["clarificationCheckpoint"]["status"] == "answered"
    compact_answers = projected_context["clarificationCheckpoint"]["resolvedAnswers"]
    assert len(compact_answers) == 3
    assert all(set(item) == {"dimensionId", "semanticValue", "source"} for item in compact_answers)
    assert compact_answers == [
        {
            "dimensionId": item["dimensionId"],
            "semanticValue": item["semanticValue"],
            "source": item["source"],
        }
        for item in context["clarificationCheckpoint"]["resolvedAnswers"]
    ]
    assert projected_context["clarificationDimensions"] == []
    assert len(projected_context["goalRequirements"]) == 3
    assert {item["intentType"] for item in projected_context["experienceSpecs"]} == {"meal", "night_view"}
    assert len(projected_context["provisionalGoalOccurrenceProjection"]["projectedPlacements"]) == 4
    route_requirement = projected_context["routePlanningPolicyRequirement"]
    assert route_requirement["source"] == "controller_estimate"
    assert route_requirement["mobilityProfile"]
    assert route_requirement["detourEnvelope"]
    assert "compact_resolved_clarification_history" in projection.telemetry["fullCompactionSteps"]


def test_live_generic_02_answered_context_uses_compact_full_http_transport():
    """The real four-answer context must fit without dropping semantic authority."""

    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context, normalization = _v4_completed_clarification_context()
    context["clarificationCheckpoint"]["resolvedAnswers"].append(
        {
            "dimensionId": "route_decision.mobility_profile",
            "semanticValue": {
                "mobilityProfile": {
                    "transportMode": "transit",
                    "paceClass": "standard",
                }
            },
            "source": "structured_option",
            "sourceUserTurnId": "turn_route_choice",
            "optionId": "opt_mobility_transit",
            "label": "公共交通",
        }
    )
    context["requestIntentContract"]["clarificationDimensions"].append(
        {
            "dimensionId": "route_decision.mobility_profile",
            "intentType": "route_decision",
            "status": "resolved",
            "impactCode": "route_mobility_profile_required",
            "candidateScope": {
                "routeDecisionContractFingerprint": (
                    "647811364c08ec763902c1512c17bfb4cb5f5e9236e07708e93e95836552f46c"
                ),
                "missingFields": ["detourTolerance", "mobilityProfile"],
                "transportMode": "",
            },
            "allowedSemanticFields": ["mobilityProfile"],
        }
    )
    meal_spec = context["experienceSpecs"][0]
    meal_spec["source"] = "server_authored_meal_experience_policy"
    meal_spec["fieldProvenance"] = {
        "frequency": "goal_ledger.meal_cardinality",
        "allowedDayNumbers": "goal_ledger.meal_allowed_days",
        "experienceFamilies": "meal_grounding_policy.authoritative_request_semantics",
        "accessPolicy": "meal_grounding_policy.amap_food_service_contract",
        "distinctnessPolicy": "goal_ledger.meal_occurrence_distribution",
        "timeWindow": "meal_grounding_policy.meal_label_schedule",
        "detourTolerance": "route_decision_contract.detour_tolerance",
        "evidenceFreshness": "meal_grounding_policy.consumer_evidence_contract",
        "confidence": "meal_grounding_policy.route_anchor_confidence",
    }
    allowed_actions = (
        "ask_user",
        "draft_itinerary",
        "read_itinerary",
        "resolve_poi",
        "optimize_route",
        "patch_itinerary",
        "verify_external_facts",
        "finish",
    )
    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=allowed_actions,
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
        normalization_context=normalization,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=1,
    )
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"

    provider.decide_autonomy(projection.full, timeout_seconds=1)

    assert len(captured) == 1
    final_http_payload = captured[0]
    assert len(final_http_payload["messages"]) == 2
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT
    provider_context_text = final_http_payload["messages"][1]["content"]
    assert provider_context_text == json.dumps(
        dict(projection.full),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    projected_context = json.loads(provider_context_text)
    assert projected_context["allowedActions"] == list(allowed_actions)
    assert projected_context["clarificationCheckpoint"]["status"] == "answered"
    assert len(projected_context["clarificationCheckpoint"]["resolvedAnswers"]) == 4
    assert projected_context["experienceSpecs"][0]["fieldProvenance"] == meal_spec["fieldProvenance"]
    assert len(projected_context["provisionalGoalOccurrenceProjection"]["projectedPlacements"]) == 4
    assert projected_context["routePlanningPolicyRequirement"]["mobilityProfile"]
    assert projected_context["routePlanningPolicyRequirement"]["detourEnvelope"]


def test_v4_truncation_reissue_payload_forbids_strict_day_strategy_extras():
    """The one full-response reissue must state the exact nested draft shape."""

    from src.services.agent_autonomy_service import (
        AgentAutonomyController,
        AgentDecisionContractService,
        ModelDecisionV3,
    )
    from src.services.controller_response_integrity import (
        ControllerOutputTruncatedError,
        ControllerResponseIntegrityEvidence,
    )
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context, normalization = _v4_completed_clarification_context()
    allowed_actions = (
        "ask_user",
        "draft_itinerary",
        "read_itinerary",
        "resolve_poi",
        "optimize_route",
        "patch_itinerary",
        "verify_external_facts",
        "finish",
    )
    contract = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=allowed_actions,
    ).build()
    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=allowed_actions,
        decision_contract=contract,
        normalization_context=normalization,
    )
    repair_feedback = AgentAutonomyController._truncation_repair_feedback(
        error=ControllerOutputTruncatedError(
            call_kind="full",
            evidence=ControllerResponseIntegrityEvidence(
                finish_reason="length",
                content_length=72,
                content_bytes=72,
                response_bytes=12_000,
                parse_error_category="Expecting property name enclosed in double quotes",
                parse_error_position=72,
            ),
        ),
        contract=contract,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=1,
    )
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"

    provider.decide_autonomy(
        projection.full,
        timeout_seconds=1,
        repair_feedback=json.dumps(repair_feedback, ensure_ascii=False),
    )

    assert len(captured) == 1
    final_http_payload = captured[0]
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT
    compact_context = json.loads(final_http_payload["messages"][1]["content"])
    assert compact_context["repairMode"] == "full_response_truncation_reissue"
    assert compact_context["allowedActions"] == list(allowed_actions)
    assert "clarificationCheckpoint" not in compact_context
    bounded_contract = json.loads(final_http_payload["messages"][2]["content"].split("Repair contract: ", 1)[1])
    assert bounded_contract["repairMode"] == "full_response_truncation_reissue"
    assert "action" not in bounded_contract
    provider_instructions = " ".join(
        "\n".join(message["content"] for message in final_http_payload["messages"]).split()
    )
    assert "dayStrategies items may contain only" in provider_instructions
    assert (
        "dayNumber, theme, requiredGoalIds, requiredGoalCounts, optionalGoalIds, pace, maxRouteAnchors"
        in provider_instructions
    )
    assert "Do not output goalOccurrences" in provider_instructions
    assert "durationEstimate may contain only min, preferred, max" in provider_instructions
    assert "estimateSource and confidence are sibling fields of occurrenceScheduleHints items" in provider_instructions


def test_clarification_repair_uses_compact_schema_context_and_reaches_provider():
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    projection = ControllerContextProjectionService().build(
        _large_context(),
        allowed_actions=("ask_user",),
        decision_contract={"schemaVersion": "agent-decision-contract-v3", "large": "c" * 20_000},
    )
    provider = DeepSeekAgentProvider(api_key="test-key", model="test-model", timeout_seconds=1)
    captured: list[dict] = []

    def fake_post(payload, *, timeout_seconds=None):
        captured.append(payload)
        return "{}"

    provider._post = fake_post
    provider.decide_autonomy(
        projection.full,
        timeout_seconds=1,
        repair_feedback=json.dumps(
            {
                "invalidPaths": ["actionDirective.options.semanticValue"],
                "exactSchema": "x" * 4_000,
            }
        ),
    )

    assert len(captured) == 1
    payload = captured[0]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 15_360
    compact_context = json.loads(payload["messages"][1]["content"])
    assert compact_context["repairMode"] == "clarification_schema_only"
    assert compact_context["allowedActions"] == ["ask_user"]
    assert compact_context["clarificationDimensions"][0]["semanticFieldSchemas"]
    assert compact_context["fingerprints"]
    assert compact_context["decisionContractRef"]
    assert "goalRequirements" not in compact_context
    assert "pendingSlots" not in compact_context
    assert "provisionalGoalOccurrenceProjection" not in compact_context


def test_draft_repair_uses_action_scoped_context_and_reaches_provider():
    """A valid first call must not strand planning when its V3 draft needs repair."""

    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context = _large_context()
    context["clarificationCheckpoint"] = {
        **context["clarificationCheckpoint"],
        "resolvedAnswers": [
            {
                "dimensionId": "night_view.cardinality",
                "semanticValue": {"occurrencePolicy": "every_available_evening"},
                "source": "structured_option",
            },
            {
                "dimensionId": "night_view.experience_mode",
                "semanticValue": {
                    "experienceFamilies": ["public_city_view"],
                    "accessPolicy": "public_outdoor_or_verified_controlled_access",
                },
                "source": "structured_option",
            },
        ],
        "status": "answered",
    }
    context["requestIntentContract"]["clarificationDimensions"] = []
    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("draft_itinerary", "ask_user"),
        decision_contract={"schemaVersion": "agent-decision-contract-v3", "large": "c" * 20_000},
    )
    # Mirror the live request that first returned HTTP 200 + finish_reason=length.
    # These fields are authoritative during the first classification, but the
    # repair contract already carries the goal cardinality needed to repair a
    # draft directive. Repeating them in the repair context used to push the
    # complete request beyond the unchanged 15 KiB hard limit.
    projection.full["experienceSpecs"][0]["fieldProvenance"] = {
        "largeLiveEvidence": "e" * 2_200,
    }
    projection.full["provisionalGoalOccurrenceProjection"]["projectedPlacements"][0]["largeLiveEvidence"] = "p" * 2_200
    provider = DeepSeekAgentProvider(api_key="test-key", model="test-model", timeout_seconds=1)
    captured: list[dict] = []

    def fake_post(payload, *, timeout_seconds=None):
        captured.append(payload)
        return "{}"

    provider._post = fake_post
    provider.decide_autonomy(
        projection.full,
        timeout_seconds=1,
        repair_feedback=json.dumps(
            {
                "action": "draft_itinerary",
                "invalidPaths": ["actionDirective.type", "actionDirective.dayStrategies"],
                "exactSchema": "x" * 5_000,
            }
        ),
    )

    assert len(captured) == 1
    payload = captured[0]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 15_360
    compact_context = json.loads(payload["messages"][1]["content"])
    assert compact_context["repairMode"] == "draft_itinerary_schema_only"
    assert compact_context["allowedActions"] == ["draft_itinerary", "ask_user"]
    assert compact_context["goalRequirements"]
    assert compact_context["decisionConstraints"]
    assert compact_context["fingerprints"]
    assert compact_context["decisionContractRef"]
    assert "experienceSpecs" not in compact_context
    assert "provisionalGoalOccurrenceProjection" not in compact_context
    assert "clarificationCheckpoint" not in compact_context
    assert "clarificationDimensions" not in compact_context
    assert "pendingSlots" not in compact_context


def test_v4_draft_repair_final_http_payload_stays_under_hard_limit():
    """The live V4 draft repair must reach DeepSeek with its typed authority."""

    from src.services.agent_autonomy_service import (
        AgentDecisionContractService,
        ModelDecisionV3,
        controller_draft_route_policy_repair_requirement,
    )
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context, normalization = _v4_completed_clarification_context()
    route_requirement = controller_draft_route_policy_repair_requirement()
    normalization["routePlanningPolicyRequirement"] = route_requirement
    allowed_actions = (
        "ask_user",
        "draft_itinerary",
        "read_itinerary",
        "resolve_poi",
        "optimize_route",
        "patch_itinerary",
        "verify_external_facts",
        "finish",
    )
    contract_service = AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=allowed_actions,
    )
    contract = contract_service.build()
    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=allowed_actions,
        decision_contract=contract,
        normalization_context=normalization,
    )
    goal_requirements = projection.full["goalRequirements"]
    repair_contract = contract_service.repair_payload(
        action="draft_itinerary",
        invalid_paths=[
            "actionDirective.draft_itinerary.dayStrategies.0.goalOccurrences",
            "actionDirective.draft_itinerary.dayStrategies.1.goalOccurrences",
            "actionDirective.draft_itinerary.routePlanningPolicy.source",
            "actionDirective.draft_itinerary.occurrenceScheduleHints.0.dayPart",
            "actionDirective.draft_itinerary.occurrenceScheduleHints.0.sequence",
            "actionDirective.draft_itinerary.occurrenceScheduleHints.0.durationEstimate",
            "actionDirective.draft_itinerary.occurrenceScheduleHints.0.confidence",
            "actionDirective.draft_itinerary.occurrenceScheduleHints.0.timeOfDay",
        ],
        allowed_ids={
            "segmentIds": [],
            "versionIds": [],
            "candidateIds": [],
            "amapPoiIds": [],
            "goalIds": [item["goalId"] for item in goal_requirements],
        },
        aliases_applied=[],
        goal_requirements=goal_requirements,
        route_policy_requirement=route_requirement,
    )
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=1,
    )
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"

    provider.decide_autonomy(
        projection.full,
        timeout_seconds=1,
        repair_feedback=json.dumps(repair_contract, ensure_ascii=False),
    )

    assert len(captured) == 1
    final_http_payload = captured[0]
    assert len(json.dumps(final_http_payload, ensure_ascii=False).encode("utf-8")) <= FULL_REQUEST_BYTE_LIMIT
    compact_context = json.loads(final_http_payload["messages"][1]["content"])
    assert compact_context["repairMode"] == "draft_itinerary_schema_only"
    assert "decisionContractRef" not in compact_context
    assert "fingerprints" not in compact_context
    repair_prompt = final_http_payload["messages"][2]["content"]
    bounded_contract = json.loads(repair_prompt.split("Repair contract: ", 1)[1])
    assert bounded_contract["action"] == "draft_itinerary"
    assert bounded_contract["goalRequirements"] == repair_contract["goalRequirements"]
    assert bounded_contract["requiredGoalCounts"] == repair_contract["requiredGoalCounts"]
    assert bounded_contract["goalCardinality"] == repair_contract["goalCardinality"]
    assert bounded_contract["optionalGoalIds"] == repair_contract["optionalGoalIds"]
    assert bounded_contract["draftSchedulingRules"] == repair_contract["draftSchedulingRules"]
    assert bounded_contract["routePlanningPolicyRequirement"] == route_requirement
    assert bounded_contract["minimalExample"] == repair_contract["minimalExample"]


def test_answered_checkpoint_with_trace_scale_candidate_gap_compacts_and_reaches_provider():
    """A real candidate-gap retry must stay executable after two clarifications."""

    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context = _large_context()
    context["clarificationCheckpoint"] = {
        **context["clarificationCheckpoint"],
        "status": "answered",
        "ambiguities": [
            {
                "dimensionId": f"night_view.dimension_{index}",
                "question": "请继续确认夜景体验方式" + "问" * 300,
                "options": [
                    {
                        "label": "公共城市天际线",
                        "semanticValue": {
                            "experienceFamilies": ["public_city_view"],
                            "accessPolicy": "public_outdoor_or_verified_controlled_access",
                        },
                    }
                    for _ in range(8)
                ],
            }
            for index in range(12)
        ],
        "resolvedAnswers": [
            {
                "dimensionId": "night_view.cardinality",
                "semanticValue": {"occurrencePolicy": "every_available_evening"},
                "source": "structured_option",
            },
            {
                "dimensionId": "night_view.experience_mode",
                "semanticValue": {
                    "experienceFamilies": ["public_city_view"],
                    "accessPolicy": "public_outdoor_or_verified_controlled_access",
                },
                "source": "structured_option",
            },
        ],
    }
    context["requestIntentContract"]["clarificationDimensions"] = []
    context["candidateGapSummary"] = {
        "schemaVersion": "candidate-gap-summary-v1",
        "status": "candidate_refresh_required",
        "missingOccurrenceCount": 4,
        "rejectedReasonCounts": {f"night_view_rejection_{index}": 60 - index for index in range(24)},
        "goals": [
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requirementLevel": "hard",
                "missingOccurrenceCount": 2,
                "requiredMin": 2,
                "targetCount": 2,
                "allowedDayNumbers": [1, 2],
                "diagnostics": "g" * 2_000,
            }
        ],
        "poolEvidence": [
            {
                "poolId": f"pool-night-{index}",
                "briefId": f"brief-{index}",
                "planningSlotId": f"slot-{index}",
                "intentType": "night_view",
                "dayNumber": 1 + index % 2,
                "providerState": "ok",
                "coverageStatus": "missing",
                "rawCandidateCount": 45,
                "candidateCount": 45,
                "eligibleCandidateCount": 0,
                "admittedCandidateCount": 0,
                "rejectedCandidates": [
                    {"name": "候选地点" + str(item), "reason": "night_view_signal_missing", "raw": "x" * 800}
                    for item in range(16)
                ],
            }
            for index in range(12)
        ],
    }

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("draft_itinerary", "ask_user"),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
    )
    assert projection.telemetry["fullPayloadBytes"] <= 8_500
    assert projection.full["candidateGapSummary"]["missingOccurrenceCount"] == 4
    assert len(projection.full["candidateGapSummary"]["rejectedReasonCounts"]) <= 12
    assert "ambiguities" not in projection.full["clarificationCheckpoint"]

    provider = DeepSeekAgentProvider(api_key="test-key", model="test-model", timeout_seconds=1)
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"
    provider.decide_autonomy(projection.full, timeout_seconds=1)
    assert len(captured) == 1
    assert len(json.dumps(captured[0], ensure_ascii=False).encode("utf-8")) <= 15_360


def test_post_batch_route_clarification_compacts_before_complete_provider_request_limit():
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context = _large_context()
    context["clarificationCheckpoint"].update(
        {
            "status": "awaiting_response",
            "ambiguities": [
                {
                    "dimensionId": f"dimension_{index}",
                    "candidateScope": {"diagnostic": "x" * 400},
                    "allowedSemanticValues": [{"value": "y" * 400}],
                }
                for index in range(8)
            ],
        }
    )
    context["requestIntentContract"]["clarificationDimensions"] = [
        {
            "dimensionId": "night_view.cardinality",
            "intentType": "night_view",
            "status": "resolved",
            "allowedSemanticFields": ["frequency", "allowedDayNumbers"],
        },
        {
            "dimensionId": "night_view.experience_mode",
            "intentType": "night_view",
            "status": "resolved",
            "allowedSemanticFields": ["experienceFamilies", "accessPolicy"],
        },
        {
            "dimensionId": "route_decision.mobility_profile",
            "intentType": "route_decision",
            "status": "unresolved",
            "impactCode": "route_mobility_profile_required",
            "candidateScope": {"transportMode": "", "diagnostic": "m" * 400},
            "allowedSemanticFields": ["mobilityProfile"],
        },
        {
            "dimensionId": "route_decision.detour_tolerance",
            "intentType": "route_decision",
            "status": "unresolved",
            "impactCode": "provider_route_matrix_acceptance_threshold",
            "candidateScope": {"transportMode": "", "diagnostic": "d" * 400},
            "allowedSemanticFields": ["detourTolerance", "adjacentLegConstraint"],
        },
        {
            "dimensionId": "controller_extension.future_policy",
            "intentType": "controller_extension",
            "status": "controller_extension",
            "impactCode": "server_owned_future_policy",
            "candidateScope": {"contractVersion": 4},
            "allowedSemanticFields": ["futurePolicy"],
        },
        {
            "dimensionId": "controller_extension.missing_status",
            "intentType": "controller_extension",
            "impactCode": "server_owned_missing_status",
            "candidateScope": {"contractVersion": 4},
            "allowedSemanticFields": ["futurePolicy"],
        },
    ]
    context["experienceSpecs"] = [
        {
            "intentType": f"experience_{index}",
            "experienceFamilies": ["public_city_view"],
            "fieldProvenance": {f"field_{item}": "p" * 400 for item in range(12)},
            "diagnostic": "z" * 2_000,
        }
        for index in range(8)
    ]
    context["provisionalGoalOccurrenceProjection"] = {
        "schemaVersion": "provisional-goal-occurrence-projection-v1",
        "status": "awaiting_controller_day_strategy",
        "unresolvedDimensionIds": [
            "route_decision.mobility_profile",
            "route_decision.detour_tolerance",
        ],
        "goalConstraints": [{"goalId": f"goal_{index}", "diagnostic": "g" * 400} for index in range(16)],
        "projectedPlacements": [
            {"projectionId": f"projection_{index}", "diagnostic": "q" * 400} for index in range(16)
        ],
        "fingerprint": "projection-fingerprint",
    }

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("ask_user",),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
    )
    dimension_ids = [item["dimensionId"] for item in projection.full["clarificationDimensions"]]
    provider = DeepSeekAgentProvider(
        api_key="test-key",
        model="deepseek-v4-flash",
        timeout_seconds=1,
    )
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"

    provider.decide_autonomy(projection.full, timeout_seconds=1)

    assert projection.telemetry["fullPayloadBytes"] <= 8_500
    assert "retain_all_clarification_dimensions" in projection.telemetry["fullCompactionSteps"]
    assert "compact_experience_specs" in projection.telemetry["fullCompactionSteps"]
    assert dimension_ids == [
        "night_view.cardinality",
        "night_view.experience_mode",
        "route_decision.mobility_profile",
        "route_decision.detour_tolerance",
        "controller_extension.future_policy",
        "controller_extension.missing_status",
    ]
    mobility_dimension = next(
        item
        for item in projection.full["clarificationDimensions"]
        if item["dimensionId"] == "route_decision.mobility_profile"
    )
    mobility_properties = mobility_dimension["semanticFieldSchemas"]["mobilityProfile"]["properties"]
    assert mobility_properties["transportMode"]["enum"] == [
        "transit",
        "public_transit",
        "driving",
        "walking",
        "bicycling",
    ]
    assert mobility_properties["paceClass"]["enum"] == ["relaxed", "standard", "intensive"]
    assert len(json.dumps(captured[0], ensure_ascii=False).encode("utf-8")) <= 15_360


def test_patch_repair_uses_write_scope_context_and_reaches_provider_under_full_request_limit():
    """A schema-only repair for an active edit must not resend planning evidence."""

    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    context = _large_context()
    context["activeVersionId"] = "version_active_1"
    context["observation"]["itinerary"] = {
        "lifecycleState": "active",
        "activeVersionId": "version_active_1",
        "meaningfulSegmentCount": 6,
    }
    context["continuationContext"]["expectedBaseVersionId"] = "version_active_1"
    context["observationFingerprint"] = "observation-fingerprint-active"
    context["clarificationCheckpoint"] = {
        **context["clarificationCheckpoint"],
        "status": "answered",
    }

    projection = ControllerContextProjectionService().build(
        context,
        allowed_actions=("patch_itinerary", "ask_user"),
        decision_contract={"schemaVersion": "agent-decision-contract-v3"},
        normalization_context={
            "requestedDayNumber": 1,
            "segmentRefs": [
                {
                    "segmentId": "segment_day1_campus",
                    "dayNumber": 1,
                    "startTime": "09:00",
                }
            ],
        },
    )
    provider = DeepSeekAgentProvider(api_key="test-key", model="test-model", timeout_seconds=1)
    captured: list[dict] = []
    provider._post = lambda payload, timeout_seconds=None: captured.append(payload) or "{}"

    provider.decide_autonomy(
        projection.full,
        timeout_seconds=1,
        repair_feedback=json.dumps(
            {
                "action": "patch_itinerary",
                "invalidPaths": ["actionDirective.type"],
                "allowedActions": ["patch_itinerary", "ask_user"],
                "persistedIds": {"segmentIds": ["segment_day1_campus"]},
                "exactSchema": "x" * 8_500,
                "minimalExample": {
                    "primaryAction": "patch_itinerary",
                    "actionDirective": {
                        "type": "patch_itinerary",
                        "targetSegmentId": "segment_day1_campus",
                        "newStartTime": "10:00",
                    },
                },
            },
            ensure_ascii=False,
        ),
    )

    assert len(captured) == 1
    payload = captured[0]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 15_360
    compact_context = json.loads(payload["messages"][1]["content"])
    assert "repairMode" in compact_context, len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert compact_context["repairMode"] == "patch_itinerary_schema_only"
    assert compact_context["targetScope"] == {
        "requestedDayNumber": 1,
        "segmentIds": ["segment_day1_campus"],
    }
    assert compact_context["itineraryLifecycle"] == {
        "state": "active",
        "activeVersionId": "version_active_1",
        "meaningfulSegmentCount": 6,
        "planningAttemptPersisted": False,
        "cycleIndex": 0,
    }
    assert compact_context["fingerprints"]["expectedBaseVersionId"] == "version_active_1"
    assert compact_context["fingerprints"]["observationFingerprint"] == ("observation-fingerprint-active")
    assert compact_context["allowedActions"] == ["patch_itinerary", "ask_user"]
    assert "clarificationCheckpoint" not in compact_context
    assert "clarificationDimensions" not in compact_context
    assert "experienceSpecs" not in compact_context
    assert "candidateGapSummary" not in compact_context
    assert "provisionalGoalOccurrenceProjection" not in compact_context

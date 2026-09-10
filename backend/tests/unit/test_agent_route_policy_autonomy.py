import json
from types import SimpleNamespace

import pytest

from src.core.config import get_settings
from src.services.agent_service import AgentService
from src.services.agent_autonomy_service import (
    AgentAutonomyController,
    clarification_safe_actions_for_request_context,
    controller_allowed_actions_for_request_context,
    controller_route_policy_may_resolve_detour,
)
from src.services.route_insertion_scorer import RouteInsertionScorer


def _unresolved_route_context(*, mobility_sensitive: bool = False) -> dict:
    return {
        "serverExecutionProfile": "simple_open_v1",
        "latestUserMessage": "北京两日游，行程安排由你规划",
        "requestIntentContract": {
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
                    "allowedSemanticFields": ["detourTolerance", "adjacentLegConstraint"],
                },
            ],
            "routeDecisionContract": {
                "status": "awaiting_clarification",
                "missingFields": ["mobilityProfile", "detourTolerance"],
                "mobilityProfile": None,
                "detourTolerance": None,
                "provenance": {"mobilitySensitive": mobility_sensitive},
            },
        },
    }


def _controller_draft_decision(*, include_mobility: bool = True) -> SimpleNamespace:
    policy = SimpleNamespace(
        source="controller_estimate",
        mobility_profile=(
            SimpleNamespace(transport_mode="transit", pace_class="standard")
            if include_mobility
            else None
        ),
        detour_envelope=SimpleNamespace(
            max_generalized_cost_delta=30,
            max_detour_ratio=0.15,
        ),
    )
    return SimpleNamespace(
        primary_action="draft_itinerary",
        action_directive=SimpleNamespace(route_planning_policy=policy),
    )


def test_controller_can_choose_dynamic_route_questions_or_typed_estimates() -> None:
    context = _unresolved_route_context()

    safe_actions = clarification_safe_actions_for_request_context(context)
    assert "draft_itinerary" in safe_actions
    assert "ask_user" in safe_actions
    controller_actions = controller_allowed_actions_for_request_context(context)
    assert "draft_itinerary" in controller_actions
    assert "ask_user" in controller_actions
    assert controller_route_policy_may_resolve_detour(
        context,
        _controller_draft_decision(),
    ) is True


def test_controller_accepts_valid_dynamic_route_question_batch_without_repair() -> None:
    class DynamicRouteQuestionProvider:
        model = "recorded-controller"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            mobility_dimension = next(
                item
                for item in autonomy_context.get("clarificationDimensions") or []
                if item.get("dimensionId") == "route_decision.mobility_profile"
            )
            self.calls.append(
                {
                    "allowedActions": list(autonomy_context.get("allowedActions") or []),
                    "repairFeedback": repair_feedback,
                    "mobilitySchema": mobility_dimension["semanticFieldSchemas"]["mobilityProfile"],
                }
            )
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": {
                    "type": "ask_user",
                    "questions": [
                        {
                            "dimensionId": "route_decision.mobility_profile",
                            "question": "主要采用哪种交通方式？",
                            "whyItMatters": "这会改变路线时间与步行强度。",
                            "allowFreeText": True,
                            "options": [
                                {
                                    "id": "transit_standard",
                                    "label": "公共交通，标准步行",
                                    "semanticValue": {
                                        "mobilityProfile": {
                                            "transportMode": "transit",
                                            "paceClass": "standard",
                                        }
                                    },
                                    "allowsManualInput": False,
                                },
                                {
                                    "id": "walking_relaxed",
                                    "label": "步行，宽松节奏",
                                    "semanticValue": {
                                        "mobilityProfile": {
                                            "transportMode": "walking",
                                            "paceClass": "relaxed",
                                        }
                                    },
                                    "allowsManualInput": True,
                                },
                            ],
                        },
                        {
                            "dimensionId": "route_decision.detour_tolerance",
                            "question": "更看重少绕路还是体验差异？",
                            "whyItMatters": "这会约束地点与路线候选的可接受偏绕。",
                            "allowFreeText": False,
                            "options": [
                                {
                                    "id": "low_detour",
                                    "label": "尽量少绕路",
                                    "semanticValue": {
                                        "detourTolerance": {
                                            "maxGeneralizedCostDelta": 15,
                                            "maxDetourRatio": 0.15,
                                        }
                                    },
                                    "allowsManualInput": False,
                                },
                                {
                                    "id": "moderate_detour",
                                    "label": "可接受适度绕路",
                                    "semanticValue": {
                                        "detourTolerance": {
                                            "maxGeneralizedCostDelta": 35,
                                            "maxDetourRatio": 0.35,
                                        }
                                    },
                                    "allowsManualInput": False,
                                },
                            ],
                        },
                    ],
                },
            }

    context = _unresolved_route_context()
    provider = DynamicRouteQuestionProvider()
    result = AgentAutonomyController(provider=provider).decide(
        "北京两日高校、美食和公园行程",
        context,
        {
            "schemaVersion": "model-first-autonomy-context-v2",
            "latestUserMessage": "北京两日高校、美食和公园行程",
            **context,
        },
        available_tools=set(),
        runtime_budget_tools=set(),
    )

    assert len(provider.calls) == 1
    assert provider.calls[0]["repairFeedback"] == ""
    assert "ask_user" in provider.calls[0]["allowedActions"]
    assert "draft_itinerary" in provider.calls[0]["allowedActions"]
    assert provider.calls[0]["mobilitySchema"]["properties"]["transportMode"]["enum"] == [
        "transit",
        "public_transit",
        "driving",
        "walking",
        "bicycling",
    ]
    assert provider.calls[0]["mobilitySchema"]["properties"]["paceClass"]["enum"] == [
        "relaxed",
        "standard",
        "intensive",
    ]
    assert result.source == "controller"
    assert result.controller_full_called is True
    assert result.controller_full_succeeded is True
    assert result.controller_lite_called is False
    assert result.schema_repair_attempts == 0
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    questions = result.decision.action_directive.model_dump(by_alias=True)["questions"]
    assert [question["dimensionId"] for question in questions] == [
        "route_decision.mobility_profile",
        "route_decision.detour_tolerance",
    ]
    assert [question["question"] for question in questions] == [
        "主要采用哪种交通方式？",
        "更看重少绕路还是体验差异？",
    ]
    assert questions[0]["options"] == [
        {
            "id": "transit_standard",
            "label": "公共交通，标准步行",
            "semanticValue": {
                "mobilityProfile": {
                    "transportMode": "transit",
                    "paceClass": "standard",
                }
            },
            "allowsManualInput": False,
        },
        {
            "id": "walking_relaxed",
            "label": "步行，宽松节奏",
            "semanticValue": {
                "mobilityProfile": {
                    "transportMode": "walking",
                    "paceClass": "relaxed",
                }
            },
            "allowsManualInput": True,
        },
    ]
    assert [option["allowsManualInput"] for option in questions[1]["options"]] == [False, False]


def test_invalid_ask_user_uses_one_compact_ask_scoped_repair() -> None:
    class InvalidThenRepairedAskProvider:
        model = "recorded-controller"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def decide_autonomy(self, autonomy_context, *, timeout_seconds, repair_feedback=""):
            feedback = json.loads(repair_feedback) if repair_feedback else {}
            self.calls.append(feedback)
            options = [
                {
                    "id": "transit_standard",
                    "label": "公共交通，标准步行",
                    "semanticValue": {
                        "mobilityProfile": {
                            "transportMode": "transit",
                            "paceClass": "standard",
                        }
                    },
                    "allowsManualInput": False,
                },
                {
                    "id": "walking_relaxed",
                    "label": "步行，宽松节奏",
                    "semanticValue": {
                        "mobilityProfile": {
                            "transportMode": "walking",
                            "paceClass": "relaxed",
                        }
                    },
                    "allowsManualInput": True,
                },
                {
                    "id": "driving_intensive",
                    "label": "驾车，紧凑节奏",
                    "semanticValue": {
                        "mobilityProfile": {
                            "transportMode": "driving",
                            "paceClass": "intensive" if repair_feedback else "fast",
                        }
                    },
                    "allowsManualInput": False,
                },
            ]
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": {
                    "type": "ask_user",
                    "questions": [
                        {
                            "dimensionId": "route_decision.mobility_profile",
                            "question": "主要采用哪种交通方式？",
                            "whyItMatters": "这会改变路线时间与步行强度。",
                            "allowFreeText": True,
                            "options": options,
                        }
                    ],
                },
            }

    context = _unresolved_route_context()
    provider = InvalidThenRepairedAskProvider()
    result = AgentAutonomyController(provider=provider).decide(
        "北京两日高校、美食和公园行程",
        context,
        {
            "schemaVersion": "model-first-autonomy-context-v2",
            "latestUserMessage": "北京两日高校、美食和公园行程",
            **context,
        },
        available_tools=set(),
        runtime_budget_tools=set(),
    )

    assert len(provider.calls) == 2
    assert provider.calls[0] == {}
    repair = provider.calls[1]
    assert repair["action"] == "ask_user"
    assert repair["minimalExample"]["primaryAction"] == "ask_user"
    assert repair["minimalExample"]["actionDirective"]["type"] == "ask_user"
    assert {
        "goalRequirements",
        "requiredGoalCounts",
        "goalCardinality",
        "optionalGoalIds",
        "draftSchedulingRules",
        "routePlanningPolicyRequirement",
    }.isdisjoint(repair)
    assert result.source == "controller"
    assert result.controller_lite_called is False
    assert result.schema_repair_attempts == 1
    assert result.decision.primary_action == "ask_user"
    assert result.gated_decision.accepted is True
    repaired_options = result.decision.action_directive.model_dump(by_alias=True)["questions"][0][
        "options"
    ]
    assert repaired_options[2]["semanticValue"]["mobilityProfile"] == {
        "transportMode": "driving",
        "paceClass": "intensive",
    }


def test_controller_route_estimate_cannot_hide_missing_mobility_or_accessibility_risk() -> None:
    normal_context = _unresolved_route_context()
    sensitive_context = _unresolved_route_context(mobility_sensitive=True)

    assert controller_route_policy_may_resolve_detour(
        normal_context,
        _controller_draft_decision(include_mobility=False),
    ) is False
    assert "draft_itinerary" not in clarification_safe_actions_for_request_context(
        sensitive_context
    )


@pytest.mark.parametrize(
    "risk_kind",
    ["mobility_sensitive", "accessibility_fallback"],
)
def test_detour_only_controller_estimate_remains_blocked_for_mobility_risk(
    risk_kind: str,
) -> None:
    context = _unresolved_route_context()
    contract = context["requestIntentContract"]
    contract["clarificationDimensions"] = [
        {
            "dimensionId": "route_decision.detour_tolerance",
            "intentType": "route_decision",
            "status": "unresolved",
        }
    ]
    route = contract["routeDecisionContract"]
    route.update(
        {
            "missingFields": ["detourTolerance"],
            "mobilityProfile": {
                "transportMode": "transit",
                "paceClass": "standard",
                "source": "user_explicit",
            },
            "detourTolerance": None,
            "accessibilityFallbackRequired": risk_kind == "accessibility_fallback",
            "provenance": {
                "mobilitySensitive": risk_kind == "mobility_sensitive",
            },
        }
    )

    assert controller_route_policy_may_resolve_detour(context) is False
    assert controller_route_policy_may_resolve_detour(
        context,
        _controller_draft_decision(),
    ) is False
    assert "ask_user" in controller_allowed_actions_for_request_context(context)
    assert "draft_itinerary" not in clarification_safe_actions_for_request_context(
        context
    )

    service = object.__new__(AgentService)
    service.settings = get_settings()
    service.route_insertion_scorer = RouteInsertionScorer()
    directive = {
        "type": "draft_itinerary",
        "routePlanningPolicy": {
            "source": "controller_estimate",
            "objective": "least_generalized_cost",
            "allowExperienceDetour": True,
            "mobilityProfile": {
                "transportMode": "transit",
                "paceClass": "standard",
            },
            "detourEnvelope": {
                "maxGeneralizedCostDelta": 30,
                "maxDetourRatio": 0.15,
            },
        },
    }

    service._apply_controller_route_policy(context, directive)

    assert route["status"] == "awaiting_clarification"
    assert route["missingFields"] == ["detourTolerance"]
    assert route["detourTolerance"] is None


def test_controller_route_estimate_cannot_override_pending_user_free_text() -> None:
    context = _unresolved_route_context()
    context["clarificationCheckpoint"] = {
        "status": "awaiting_agent_resolution",
        "pendingFreeTextAnswer": {
            "dimensionId": "route_decision.mobility_profile",
            "text": "我想少走路",
            "sourceUserTurnId": "turn_user_answer",
            "source": "free_text",
        },
    }

    assert "draft_itinerary" not in clarification_safe_actions_for_request_context(
        context
    )
    assert controller_route_policy_may_resolve_detour(
        context,
        _controller_draft_decision(),
    ) is False


def test_controller_route_estimate_cannot_bypass_checkpoint_only_non_route_gap() -> None:
    context = _unresolved_route_context()
    context["clarificationCheckpoint"] = {
        "status": "answered",
        "unresolvedDimensions": ["spatial_focus"],
        "ambiguities": [
            {
                "dimensionId": "spatial_focus",
                "resolved": False,
            }
        ],
    }

    assert "draft_itinerary" not in clarification_safe_actions_for_request_context(
        context
    )
    assert controller_route_policy_may_resolve_detour(
        context,
        _controller_draft_decision(),
    ) is False


def test_controller_route_estimates_are_sealed_without_pretending_to_be_user_answers() -> None:
    context = _unresolved_route_context()
    context["requestIntentContract"]["experienceSpecs"] = [
        {
            "intentType": "meal",
            "frequency": "one",
            "allowedDayNumbers": [1, 2],
            "experienceFamilies": ["meal"],
            "accessPolicy": "verified_amap_food_service",
            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
            "timeWindow": {"start": "12:00", "end": "13:15"},
            "detourTolerance": None,
            "evidenceFreshness": {"maxAgeHours": 24},
            "confidence": 0.84,
            "unresolvedDimensions": ["meal.detourTolerance"],
        }
    ]
    context["routeDecisionContract"] = {
        "status": "awaiting_clarification",
        "missingFields": ["mobilityProfile", "detourTolerance"],
    }
    context["experienceSpecs"] = [
        {
            **context["requestIntentContract"]["experienceSpecs"][0],
            "unresolvedDimensions": ["meal.detourTolerance"],
        }
    ]
    context["clarificationCheckpoint"] = {
        "status": "awaiting_agent_resolution",
        "unresolvedDimensions": [
            "route_decision.mobility_profile",
            "route_decision.detour_tolerance",
        ],
        "ambiguities": [
            {
                "dimensionId": "route_decision.mobility_profile",
                "resolved": False,
            },
            {
                "dimensionId": "route_decision.detour_tolerance",
                "resolved": False,
            },
        ],
    }
    context["canonicalRequestContext"] = {
        "requestIntentContract": {
            **context["requestIntentContract"],
            "routeDecisionContract": {
                **context["requestIntentContract"]["routeDecisionContract"],
            },
            "experienceSpecs": [
                {**context["requestIntentContract"]["experienceSpecs"][0]}
            ],
        },
        "routeDecisionContract": {**context["routeDecisionContract"]},
        "experienceSpecs": [{**context["experienceSpecs"][0]}],
        "clarificationCheckpoint": {**context["clarificationCheckpoint"]},
    }
    service = object.__new__(AgentService)
    service.settings = get_settings()
    service.route_insertion_scorer = RouteInsertionScorer()
    directive = {
        "type": "draft_itinerary",
        "routePlanningPolicy": {
            "source": "controller_estimate",
            "objective": "least_generalized_cost",
            "allowExperienceDetour": True,
            "mobilityProfile": {
                "transportMode": "transit",
                "paceClass": "standard",
            },
            "detourEnvelope": {
                "maxGeneralizedCostDelta": 30,
                "maxDetourRatio": 0.15,
            },
        },
    }

    service._apply_controller_route_policy(context, directive)

    contract = context["requestIntentContract"]
    route = contract["routeDecisionContract"]
    assert contract["clarificationRequired"] is False
    assert route["status"] == "ready"
    assert route["missingFields"] == []
    assert route["source"] == "request_intent_contract"
    assert route["mobilityProfile"]["source"] == "controller_estimate"
    assert route["detourToleranceSource"] == "controller_estimate"
    assert route["routePlanningPolicy"] == directive["routePlanningPolicy"]
    assert {
        item["dimensionId"]: item["status"]
        for item in contract["clarificationDimensions"]
    } == {
        "route_decision.mobility_profile": "resolved_by_controller_policy",
        "route_decision.detour_tolerance": "resolved_by_controller_policy",
    }
    meal_spec = contract["experienceSpecs"][0]
    assert meal_spec["detourTolerance"] == {
        "maxGeneralizedCostDelta": 30.0,
        "maxDetourRatio": 0.15,
    }
    assert "meal.detourTolerance" not in meal_spec["unresolvedDimensions"]
    assert meal_spec["specFingerprint"]
    assert context["routeDecisionContract"] == route
    assert context["experienceSpecs"] == contract["experienceSpecs"]
    canonical = context["canonicalRequestContext"]
    assert canonical["requestIntentContract"] == contract
    assert canonical["routeDecisionContract"] == route
    assert canonical["experienceSpecs"] == contract["experienceSpecs"]
    assert canonical["clarificationCheckpoint"] == context["clarificationCheckpoint"]
    assert canonical["clarificationCheckpoint"]["status"] == "answered"


def test_ready_route_contract_repairs_stale_context_aliases_without_reestimating() -> None:
    context = _unresolved_route_context()
    service = object.__new__(AgentService)
    service.settings = get_settings()
    service.route_insertion_scorer = RouteInsertionScorer()
    directive = {
        "type": "draft_itinerary",
        "routePlanningPolicy": {
            "source": "controller_estimate",
            "objective": "least_generalized_cost",
            "allowExperienceDetour": True,
            "mobilityProfile": {
                "transportMode": "transit",
                "paceClass": "standard",
            },
            "detourEnvelope": {
                "maxGeneralizedCostDelta": 20,
                "maxDetourRatio": 0.2,
            },
        },
    }
    service._apply_controller_route_policy(context, directive)
    assert context["requestIntentContract"]["routeDecisionContract"]["status"] == "ready"
    context["requestIntentContract"]["experienceSpecs"] = []
    context["routeDecisionContract"] = {"status": "awaiting_clarification"}
    context["experienceSpecs"] = [{"intentType": "stale"}]
    context["canonicalRequestContext"] = {
        "requestIntentContract": {"clarificationRequired": True},
        "routeDecisionContract": {"status": "awaiting_clarification"},
        "experienceSpecs": [{"intentType": "stale"}],
    }

    service._apply_controller_route_policy(context, directive)

    contract = context["requestIntentContract"]
    assert context["routeDecisionContract"] == contract["routeDecisionContract"]
    assert context["experienceSpecs"] == []
    assert context["canonicalRequestContext"]["requestIntentContract"] == contract
    assert context["canonicalRequestContext"]["routeDecisionContract"] == contract["routeDecisionContract"]
    assert context["canonicalRequestContext"]["experienceSpecs"] == []

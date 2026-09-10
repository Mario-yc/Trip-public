from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from src.services.agent_autonomy_service import ModelDecisionV3, PRIMARY_ACTION_VALUES
from src.services.agent_decision_contract_service import AgentDecisionContractService


def _service() -> AgentDecisionContractService:
    return AgentDecisionContractService(
        decision_model=ModelDecisionV3,
        allowed_actions=PRIMARY_ACTION_VALUES,
    )


def test_contract_is_generated_from_agent_decision_schema():
    contract = _service().build()

    assert contract["decisionSchema"] == ModelDecisionV3.model_json_schema(by_alias=True)
    assert contract["contractVersion"] == "agent-decision-contract-v3"
    assert "targetScope" not in contract["decisionSchema"]["properties"]


def test_provider_facing_v3_schema_exposes_only_action_owned_fields():
    contract = _service().build()
    schema = contract["decisionSchema"]

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "schemaVersion",
        "primaryAction",
        "actionDirective",
    }

    repair = _service().repair_payload(
        action="resolve_poi",
        invalid_paths=["actionDirective.resolve_poi.segmentId"],
        allowed_ids={"segmentIds": ["seg_live"], "versionIds": ["ver_live"]},
        aliases_applied=["segmentId"],
    )
    assert set(repair["minimalExample"]) == {
        "schemaVersion",
        "primaryAction",
        "actionDirective",
    }


def test_contract_exposes_stable_hashes_and_allowed_actions():
    first = _service().build()
    second = _service().build()

    assert first["contractHash"] == second["contractHash"]
    assert first["allowedActionsHash"] == second["allowedActionsHash"]
    assert first["allowedActions"] == sorted(PRIMARY_ACTION_VALUES)
    assert first["actionSchemaHashes"]["resolve_poi"]


def test_contract_contains_exact_resolve_poi_schema_and_alias_guidance():
    contract = _service().build()
    schema = contract["actionSchemas"]["resolve_poi"]

    serialized = str(schema)
    assert "targetSegmentIds" in serialized
    assert "searchIntent" in serialized
    assert "segmentId" not in schema.get("properties", {})
    assert "searchText" not in schema.get("properties", {})
    assert contract["normalizationGuidance"]["resolve_poi"]["segmentId"] == "targetSegmentIds"
    assert contract["normalizationGuidance"]["resolve_poi"]["searchText"] == "searchIntent"


def test_repair_payload_uses_same_contract_and_exact_invalid_paths():
    service = _service()
    contract = service.build()
    repair = service.repair_payload(
        action="resolve_poi",
        invalid_paths=["actionDirective.resolve_poi.segmentId", "actionDirective.resolve_poi.searchText"],
        allowed_ids={"segmentIds": ["seg_live"], "versionIds": ["ver_live"]},
        aliases_applied=["segmentId", "searchText"],
    )

    assert repair["contractHash"] == contract["contractHash"]
    assert repair["contractVersion"] == contract["contractVersion"]
    assert repair["actionSchema"] == contract["actionSchemas"]["resolve_poi"]
    assert repair["invalidPaths"] == [
        "actionDirective.resolve_poi.segmentId",
        "actionDirective.resolve_poi.searchText",
    ]
    assert repair["allowedIds"] == {"segmentIds": ["seg_live"], "versionIds": ["ver_live"]}
    assert repair["minimalExample"]["actionDirective"]["type"] == "resolve_poi"


def test_patch_repair_minimal_example_is_canonical_and_uses_only_allowed_ids():
    repair = _service().repair_payload(
        action="patch_itinerary",
        invalid_paths=["actionDirective.type"],
        allowed_ids={
            "segmentIds": ["seg_first", "seg_second"],
            "versionIds": ["ver_first", "ver_second"],
        },
        aliases_applied=[],
    )

    directive = repair["minimalExample"]["actionDirective"]
    assert directive == {
        "type": "patch_itinerary",
        "operationIntent": "replace_segment_start_time",
        "baseVersionId": "ver_first",
        "targetSegmentIds": ["seg_first"],
        "requestedOutcome": "将目标行程开始时间调整为 10:00",
        "preserve": ["其余行程内容与顺序"],
        "maxChangedSegmentCount": 1,
        "startTime": "10:00",
    }
    assert ModelDecisionV3.model_validate(repair["minimalExample"]).action_directive is not None


def test_draft_repair_contains_goal_constraints_and_minimal_strict_example():
    service = _service()
    goals = [
        {
            "goalId": "goal_campus_visit",
            "intentType": "campus_visit",
            "requiredMin": 1,
            "preferredCount": 1,
            "maxCount": None,
            "requirementLevel": "required",
            "allowedDayNumbers": [1, 2],
        },
        {
            "goalId": "goal_meal",
            "intentType": "meal",
            "requiredMin": 1,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "soft_experience",
            "allowedDayNumbers": [1, 2],
        },
    ]

    repair = service.repair_payload(
        action="draft_itinerary",
        invalid_paths=["actionDirective.draft_itinerary"],
        allowed_ids={"goalIds": ["goal_campus_visit", "goal_meal"]},
        aliases_applied=[],
        goal_requirements=goals,
    )

    directive = repair["minimalExample"]["actionDirective"]
    assert repair["goalRequirements"] == goals
    assert repair["requiredGoalCounts"] == {"goal_campus_visit": 1}
    assert repair["optionalGoalIds"] == ["goal_meal"]
    assert repair["draftSchedulingRules"] == {
        "goalPriorityIsOrderingOnly": True,
        "goalPriorityMayContainKnownHardOrSoftGoals": True,
        "requiredGoalCountTotalsMustMeetRequiredGoalCounts": True,
        "requiredGoalCountPerDayMustBeOneOrOmitted": True,
        "requiredGoalCountTotalsMustNotExceedMaxCount": True,
        "optionalGoalOccurrencesMustFitOptionalExperienceBudget": True,
        "requiredGoalsMustUseAllowedDayNumbers": True,
        "repeatedGoalsMustBeDistributedAcrossDistinctDays": True,
        "softExperienceGoalsMayBeOptionalOrUnscheduled": True,
        "eachScheduledGoalDayRequiresExactlyOneOccurrenceScheduleHint": True,
        "occurrenceScheduleHintDuplicatesOrExtrasAllowed": False,
        "nonEveningNightHintRequiresPreferredStartTime": True,
        "eveningNightHintMayUseDateAndCoordinatesInsteadOfPreferredStartTime": True,
        "everyOccurrenceScheduleHintRequiresPositiveDurationEstimate": True,
        "everyDayStrategyRequiresNonEmptyTheme": True,
        "everyOccurrenceScheduleHintRequiresEstimateSource": True,
        "occurrenceScheduleHintEstimateSourceAllowedValues": [
            "controller_estimate",
            "user_explicit",
            "trusted_server_fact",
        ],
    }
    assert repair["requiredFieldChecklist"]["actionDirective.dayStrategies[]"] == [
        "dayNumber",
        "theme",
    ]
    assert repair["requiredFieldChecklist"]["actionDirective.occurrenceScheduleHints[]"] == [
        "goalId",
        "dayNumber",
        "dayPart",
        "sequence",
        "durationEstimate",
        "estimateSource",
        "confidence",
    ]
    assert repair["requiredFieldChecklist"][
        "actionDirective.occurrenceScheduleHints[].durationEstimate"
    ] == ["min", "preferred", "max"]
    assert directive["goalPriority"] == ["goal_campus_visit", "goal_meal"]
    assert directive["dayStrategies"][0]["requiredGoalIds"] == ["goal_campus_visit"]
    assert directive["dayStrategies"][0]["requiredGoalCounts"] == {"goal_campus_visit": 1}
    assert directive["dayStrategies"][0]["optionalGoalIds"] == ["goal_meal"]
    assert {
        (hint["goalId"], hint["dayNumber"])
        for hint in directive["occurrenceScheduleHints"]
    } == {("goal_campus_visit", 1), ("goal_meal", 1)}
    assert all(hint["durationEstimate"]["preferred"] > 0 for hint in directive["occurrenceScheduleHints"])


def test_ask_user_repair_omits_draft_only_contract_and_stays_within_controller_budget():
    service = _service()
    goals = [
        {
            "goalId": f"goal_{index}",
            "intentType": "campus_visit" if index < 2 else "meal",
            "requiredMin": 1,
            "preferredCount": 2,
            "maxCount": 2,
            "requirementLevel": "required" if index < 2 else "soft_experience",
            "allowedDayNumbers": [1, 2],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "explicit_user_request",
        }
        for index in range(12)
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

    repair = service.repair_payload(
        action="ask_user",
        invalid_paths=["actionDirective.questions[0].options[0].semanticValue.mobilityProfile.paceClass"],
        allowed_ids={"goalIds": [item["goalId"] for item in goals]},
        aliases_applied=[],
        goal_requirements=goals,
        route_policy_requirement=route_requirement,
    )

    assert {
        "goalRequirements",
        "requiredGoalCounts",
        "goalCardinality",
        "optionalGoalIds",
        "draftSchedulingRules",
        "routePlanningPolicyRequirement",
    }.isdisjoint(repair)
    assert repair["minimalExample"]["primaryAction"] == "ask_user"
    assert len(json.dumps(repair, ensure_ascii=False).encode("utf-8")) <= 15_360


def test_route_only_draft_repair_makes_typed_route_policy_explicit_and_non_nullable():
    service = _service()
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

    repair = service.repair_payload(
        action="draft_itinerary",
        invalid_paths=["actionDirective.routePlanningPolicy"],
        allowed_ids={"goalIds": ["goal_campus_visit"]},
        aliases_applied=[],
        goal_requirements=[
            {
                "goalId": "goal_campus_visit",
                "intentType": "campus_visit",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "requirementLevel": "required",
                "allowedDayNumbers": [1, 2],
            }
        ],
        route_policy_requirement=requirement,
    )

    assert repair["routePlanningPolicyRequirement"] == requirement
    assert "routePlanningPolicy" in repair["actionSchema"]["required"]
    assert repair["actionSchema"]["properties"]["routePlanningPolicy"]["type"] == "object"
    assert repair["requiredFieldChecklist"]["actionDirective.routePlanningPolicy"] == [
        "source",
        "objective",
        "allowExperienceDetour",
        "mobilityProfile",
        "detourEnvelope",
    ]
    policy = repair["minimalExample"]["actionDirective"]["routePlanningPolicy"]
    assert policy == {
        "objective": "least_generalized_cost",
        "source": "controller_estimate",
        "allowExperienceDetour": True,
        "mobilityProfile": {
            "transportMode": "transit",
            "paceClass": "standard",
        },
        "detourEnvelope": {
            "maxGeneralizedCostDelta": 30,
            "maxDetourRatio": 0.3,
        },
    }
    assert ModelDecisionV3.model_validate(repair["minimalExample"]).primary_action == "draft_itinerary"


def test_model_decision_v3_rejects_server_scope_and_draft_poi_assumptions():
    service = _service()
    repair = service.repair_payload(
        action="draft_itinerary",
        invalid_paths=[],
        allowed_ids={"goalIds": ["goal_campus_visit"]},
        aliases_applied=[],
        goal_requirements=[
            {
                "goalId": "goal_campus_visit",
                "requiredMin": 1,
                "requirementLevel": "required",
                "allowedDayNumbers": [1, 2],
            }
        ],
    )
    payload = repair["minimalExample"]
    payload["targetScope"] = {}
    with pytest.raises(ValidationError) as scope_error:
        ModelDecisionV3.model_validate(payload)
    assert "targetScope" in str(scope_error.value)

    payload.pop("targetScope")
    payload["assumptionPolicy"] = "allow_reversible"
    payload["assumptions"] = [{"key": "museum", "value": "默认国家博物馆"}]
    with pytest.raises(ValidationError) as assumption_error:
        ModelDecisionV3.model_validate(payload)
    errors = assumption_error.value.errors()
    assert {(error["loc"], error["type"]) for error in errors} == {
        (("assumptionPolicy",), "extra_forbidden"),
        (("assumptions",), "extra_forbidden"),
    }


def test_deterministic_draft_directive_uses_authoritative_hard_and_soft_cardinality():
    directive = AgentDecisionContractService.deterministic_draft_directive(
        [
            {
                "goalId": "goal_campus_visit",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "requirementLevel": "required",
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_night_view",
                "requiredMin": 1,
                "preferredCount": 2,
                "maxCount": 1,
                "requirementLevel": "required",
                "allowedDayNumbers": [1, 2],
            },
            {
                "goalId": "goal_meal",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "requirementLevel": "soft_experience",
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
            },
        ]
    )

    assert directive["type"] == "draft_itinerary"
    assert [item["dayNumber"] for item in directive["dayStrategies"]] == [1, 2]
    assert [item["requiredGoalCounts"] for item in directive["dayStrategies"]] == [
        {"goal_campus_visit": 1, "goal_night_view": 1},
        {"goal_campus_visit": 1},
    ]
    assert [item["optionalGoalIds"] for item in directive["dayStrategies"]] == [
        ["goal_meal"],
        ["goal_meal"],
    ]
    assert directive["optionalExperienceBudget"] == 2


def test_contract_hash_changes_when_decision_schema_changes():
    service = _service()
    original = service.build()
    mutated_schema = copy.deepcopy(original["decisionSchema"])
    mutated_schema["properties"]["newServerField"] = {"type": "string"}

    assert service.hash_schema(mutated_schema) != original["contractHash"]

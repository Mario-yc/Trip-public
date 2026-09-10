from __future__ import annotations

import copy

import pytest

from src.services.agent_autonomy_service import AgentAutonomyController, AgentDecision
from src.services.agent_decision_normalizer import (
    AgentDecisionNormalizer,
    DecisionNormalizationError,
)


SEGMENT_ID = "seg_live_museum"
OTHER_SEGMENT_ID = "seg_live_other"
VERSION_ID = "ver_live"


def _payload(**directive_overrides):
    directive = {
        "type": "resolve_poi",
        "targetSegmentIds": [SEGMENT_ID],
        "searchIntent": "清华美术馆",
        "searchMode": "text",
        "maxCandidates": 4,
        "autoSelectPolicy": "dominant_safe_candidate_only",
        "askUserPolicy": "material_tradeoff_only",
    }
    directive.update(directive_overrides)
    return {
        "schemaVersion": "agent-decision-v3",
        "decisionId": "decision_live",
        "primaryAction": "resolve_poi",
        "confidence": 0.9,
        "decisionSummary": "解析第一天的美术馆占位段",
        "userVisibleReason": "先核验真实地点",
        "reasonCodes": ["unresolved_required_poi"],
        "requiredTools": ["resolve_poi"],
        "actionDirective": directive,
        "proposedWriteRisk": "none",
        "stopCondition": {"type": "continue_after_observation"},
        "fallbackAction": "ask_user",
    }


def _context():
    return {
        "activeVersionId": VERSION_ID,
        "segmentRefs": [
            {"segmentId": SEGMENT_ID, "dayNumber": 1, "activeVersionId": VERSION_ID},
            {"segmentId": OTHER_SEGMENT_ID, "dayNumber": 2, "activeVersionId": VERSION_ID},
        ],
        "candidateGroups": [
            {
                "segmentId": SEGMENT_ID,
                "candidateIds": ["cand_safe"],
                "amapPoiIds": ["amap_safe"],
            }
        ],
    }


@pytest.mark.parametrize(
    ("aliases", "expected_aliases"),
    [
        ({"segmentId": SEGMENT_ID, "searchText": "清华美术馆"}, ["segmentId", "searchText"]),
        ({"targetSegmentId": SEGMENT_ID, "searchQuery": "清华美术馆"}, ["searchQuery", "targetSegmentId"]),
        ({"segmentId": SEGMENT_ID, "query": "清华美术馆"}, ["query", "segmentId"]),
        (
            {
                "targetSegmentIds": [SEGMENT_ID],
                "segmentId": SEGMENT_ID,
                "searchIntent": "清华美术馆",
                "searchText": "清华美术馆",
            },
            ["segmentId", "searchText"],
        ),
        (
            {"segmentId": SEGMENT_ID, "searchText": "清华美术馆", "dayNumber": 1},
            ["dayNumber", "segmentId", "searchText"],
        ),
    ],
)
def test_safe_aliases_are_canonicalized_and_strictly_validated(aliases, expected_aliases):
    payload = _payload()
    payload["actionDirective"] = {"type": "resolve_poi", **aliases}

    result = AgentDecisionNormalizer().normalize(payload, context=_context())

    directive = result.normalized["actionDirective"]
    assert directive["targetSegmentIds"] == [SEGMENT_ID]
    assert directive["searchIntent"] == "清华美术馆"
    assert not ({"segmentId", "targetSegmentId", "searchText", "searchQuery", "query", "dayNumber"} & directive.keys())
    assert result.aliases_applied == sorted(expected_aliases)
    assert AgentDecision.model_validate(result.normalized).primary_action == "resolve_poi"


def test_canonical_payload_passes_without_mutating_risk_or_action():
    payload = _payload()

    result = AgentDecisionNormalizer().normalize(payload, context=_context())

    assert result.normalized["primaryAction"] == payload["primaryAction"]
    assert result.normalized["proposedWriteRisk"] == payload["proposedWriteRisk"]
    assert result.normalized["actionDirective"]["searchIntent"] == "清华美术馆"
    assert result.aliases_applied == []


def test_missing_non_writing_ask_user_discriminator_is_copied_from_model_action():
    payload = {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "ask_user",
        "actionDirective": {
            "questions": [
                {
                    "dimensionId": "night_view.cardinality",
                    "question": "两晚安排几次夜景？",
                    "whyItMatters": "决定夜景分布。",
                    "allowFreeText": True,
                    "options": [],
                }
            ]
        },
    }

    result = AgentDecisionNormalizer().normalize(payload, context={})

    assert result.normalized["actionDirective"]["type"] == "ask_user"
    assert result.aliases_applied == ["actionDirective.type:copied_from_primaryAction"]
    assert "type" not in payload["actionDirective"]


def _spatial_clarification_context() -> dict:
    return {
        "clarificationDimensions": [
            {
                "dimensionId": "spatial_focus",
                "status": "unresolved",
                "allowedSemanticFields": ["spatialResolutionInput"],
            },
            {
                "dimensionId": "route_decision.detour_tolerance",
                "status": "unresolved",
                "allowedSemanticFields": ["detourTolerance", "adjacentLegConstraint"],
            },
        ]
    }


def _trace_shaped_spatial_clarification() -> dict:
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "ask_user",
        "actionDirective": {
            "questions": [
                {
                    "dimensionId": "spatial_focus",
                    "question": "希望用哪个具体活动区域约束这次行程？",
                    "whyItMatters": "需要先把自然语言空间意图转换为可核验范围。",
                    "allowFreeText": True,
                    "options": [
                        {
                            "id": "landmark_radius",
                            "label": "以候选地标为中心 8 公里",
                            "semanticValue": {
                                "spatialResolutionInput": {
                                    "kind": "reference_point_radius",
                                    "referencePoint": "天安门广场",
                                    "radiusMeters": 8000,
                                }
                            },
                            "allowsManualInput": False,
                        },
                        {
                            "id": "administrative_area",
                            "label": "明确行政区域",
                            "semanticValue": {
                                "spatialResolutionInput": {
                                    "kind": "administrative_area",
                                    "area": "北京市东城区、西城区",
                                }
                            },
                            "allowsManualInput": False,
                        },
                    ],
                },
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "question": "对相邻地点的距离与用时希望如何控制？",
                    "whyItMatters": "决定候选半径和路线验收阈值。",
                    "allowFreeText": True,
                    "options": [
                        {
                            "id": "compact",
                            "label": "紧凑安排",
                            "semanticValue": {
                                "detourTolerance": {
                                    "maxGeneralizedCostDelta": 10,
                                    "maxDetourRatio": 0.2,
                                },
                                "adjacentLegConstraint": {
                                    "candidateSearchRadiusMeters": 5000,
                                    "maxProviderTravelMinutes": 45,
                                },
                            },
                            "allowsManualInput": False,
                        },
                        {
                            "id": "relaxed",
                            "label": "适度放宽",
                            "semanticValue": {
                                "detourTolerance": {
                                    "maxGeneralizedCostDelta": 20,
                                    "maxDetourRatio": 0.3,
                                },
                                "adjacentLegConstraint": {
                                    "candidateSearchRadiusMeters": 8000,
                                    "maxProviderTravelMinutes": 60,
                                },
                            },
                            "allowsManualInput": False,
                        },
                    ],
                },
            ]
        },
    }


def test_trace_shaped_spatial_text_aliases_are_canonicalized_without_authoring_geography():
    payload = _trace_shaped_spatial_clarification()

    result = AgentDecisionNormalizer().normalize(payload, context=_spatial_clarification_context())

    questions = result.normalized["actionDirective"]["questions"]
    reference = questions[0]["options"][0]["semanticValue"]["spatialResolutionInput"]
    administrative = questions[0]["options"][1]["semanticValue"]["spatialResolutionInput"]
    assert reference == {
        "kind": "reference_point_radius",
        "referenceText": "天安门广场",
        "radiusMeters": 8000,
    }
    assert administrative == {
        "kind": "administrative_area",
        "administrativeAreaText": "北京市东城区、西城区",
    }
    assert result.aliases_applied == [
        "actionDirective.questions[0].options[0].semanticValue.spatialResolutionInput.referencePoint:referenceText",
        "actionDirective.questions[0].options[1].semanticValue.spatialResolutionInput.area:administrativeAreaText",
        "actionDirective.type:copied_from_primaryAction",
    ]
    AgentAutonomyController._validate_ask_user_contract(
        result.normalized["actionDirective"],
        _spatial_clarification_context(),
    )
    assert (
        "referencePoint"
        in payload["actionDirective"]["questions"][0]["options"][0]["semanticValue"]["spatialResolutionInput"]
    )


def test_abstract_city_center_alias_remains_rejected_instead_of_getting_a_default_place():
    payload = _trace_shaped_spatial_clarification()
    spatial_input = payload["actionDirective"]["questions"][0]["options"][0]["semanticValue"]["spatialResolutionInput"]
    spatial_input["referencePoint"] = {"type": "city_center"}

    result = AgentDecisionNormalizer().normalize(payload, context=_spatial_clarification_context())

    assert result.normalized["actionDirective"]["questions"][0]["options"][0]["semanticValue"][
        "spatialResolutionInput"
    ]["referencePoint"] == {"type": "city_center"}
    with pytest.raises(DecisionNormalizationError) as raised:
        AgentAutonomyController._validate_ask_user_contract(
            result.normalized["actionDirective"],
            _spatial_clarification_context(),
        )
    assert raised.value.reason_code == "clarification_semantic_options_invalid"


def test_controller_cannot_author_a_map_selection_fingerprint():
    payload = _trace_shaped_spatial_clarification()
    spatial_input = payload["actionDirective"]["questions"][0]["options"][0]["semanticValue"]["spatialResolutionInput"]
    spatial_input.clear()
    spatial_input.update(
        {
            "kind": "map_selection",
            "mapSelectionFingerprint": "a" * 64,
        }
    )

    result = AgentDecisionNormalizer().normalize(payload, context=_spatial_clarification_context())

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentAutonomyController._validate_ask_user_contract(
            result.normalized["actionDirective"],
            _spatial_clarification_context(),
        )
    assert raised.value.reason_code == "clarification_semantic_fields_invalid"


def test_conflicting_spatial_alias_and_canonical_text_fails_closed():
    payload = _trace_shaped_spatial_clarification()
    spatial_input = payload["actionDirective"]["questions"][0]["options"][0]["semanticValue"]["spatialResolutionInput"]
    spatial_input["referenceText"] = "国贸"

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentDecisionNormalizer().normalize(payload, context=_spatial_clarification_context())

    assert raised.value.reason_code == "conflicting_alias"
    assert raised.value.path.endswith("spatialResolutionInput.referenceText")


def test_missing_write_action_discriminator_still_fails_closed():
    payload = _payload()
    payload["actionDirective"].pop("type")

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentDecisionNormalizer().normalize(payload, context=_context())

    assert raised.value.reason_code == "action_directive_mismatch"
    assert raised.value.path == "actionDirective.type"


def test_unique_amap_id_used_as_candidate_id_normalizes_to_persisted_group_identity():
    payload = _payload()
    payload["primaryAction"] = "patch_itinerary"
    payload["actionDirective"] = {
        "type": "patch_itinerary",
        "operationIntent": "replace_segment_poi_from_candidate",
        "baseVersionId": VERSION_ID,
        "targetSegmentIds": [SEGMENT_ID],
        "candidateId": "amap_safe",
        "amapPoiId": "amap_safe",
        "requestedOutcome": "替换为唯一安全候选",
    }

    result = AgentDecisionNormalizer().normalize(payload, context=_context())

    assert result.normalized["actionDirective"]["candidateId"] == "cand_safe"
    assert result.aliases_applied == ["candidateId:amap_safe->cand_safe"]


@pytest.mark.parametrize(
    ("mutate", "reason_code", "path"),
    [
        (
            lambda p: p["actionDirective"].update(segmentId=OTHER_SEGMENT_ID),
            "conflicting_alias",
            "actionDirective.targetSegmentIds",
        ),
        (
            lambda p: p["actionDirective"].update(segmentId=SEGMENT_ID, targetSegmentId=OTHER_SEGMENT_ID),
            "conflicting_alias",
            "actionDirective.targetSegmentIds",
        ),
        (
            lambda p: p["actionDirective"].update(searchText="A", searchQuery="B"),
            "conflicting_alias",
            "actionDirective.searchIntent",
        ),
        (
            lambda p: p["actionDirective"].update(dayNumber=2),
            "day_segment_mismatch",
            "actionDirective.dayNumber",
        ),
        (
            lambda p: p["actionDirective"].update(targetSegmentIds=["seg_invented"]),
            "invented_or_stale_target",
            "actionDirective.targetSegmentIds[0]",
        ),
        (
            lambda p: p["actionDirective"].update(unsafeWrite=True),
            "unknown_dangerous_field",
            "actionDirective.unsafeWrite",
        ),
        (
            lambda p: p["actionDirective"].update(searchIntent=""),
            "invalid_canonical_value",
            "actionDirective.searchIntent",
        ),
        (
            lambda p: p.update(primaryAction="patch_itinerary"),
            "action_directive_mismatch",
            "actionDirective.type",
        ),
    ],
)
def test_unsafe_or_conflicting_normalization_fails_closed(mutate, reason_code, path):
    payload = _payload()
    mutate(payload)

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentDecisionNormalizer().normalize(payload, context=_context())

    assert raised.value.reason_code == reason_code
    assert raised.value.path == path


def test_stale_base_version_is_rejected():
    payload = _payload()
    payload["primaryAction"] = "patch_itinerary"
    payload["proposedWriteRisk"] = "medium"
    payload["actionDirective"] = {
        "type": "patch_itinerary",
        "operationIntent": "replace_segment_poi",
        "baseVersionId": "ver_stale",
        "targetSegmentIds": [SEGMENT_ID],
        "requestedOutcome": "替换美术馆",
    }

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentDecisionNormalizer().normalize(payload, context=_context())

    assert raised.value.reason_code == "stale_base_version"
    assert raised.value.path == "actionDirective.baseVersionId"


def test_explicit_user_day_rejects_cross_day_resolve_target():
    context = _context()
    context["requestedDayNumber"] = 1
    payload = _payload(targetSegmentIds=[OTHER_SEGMENT_ID])

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentDecisionNormalizer().normalize(payload, context=context)

    assert raised.value.reason_code == "explicit_user_day_mismatch"
    assert raised.value.path == "actionDirective.targetSegmentIds[0]"


def test_candidate_amap_pair_outside_pending_safe_group_is_rejected():
    payload = _payload()
    payload["primaryAction"] = "patch_itinerary"
    payload["proposedWriteRisk"] = "medium"
    payload["actionDirective"] = {
        "type": "patch_itinerary",
        "operationIntent": "replace_segment_poi_from_candidate",
        "baseVersionId": VERSION_ID,
        "targetSegmentIds": [SEGMENT_ID],
        "requestedOutcome": "替换美术馆",
        "candidateId": "cand_safe",
        "amapPoiId": "amap_invented",
    }

    with pytest.raises(DecisionNormalizationError) as raised:
        AgentDecisionNormalizer().normalize(payload, context=_context())

    assert raised.value.reason_code == "candidate_identity_not_in_safe_group"


def test_normalizer_does_not_mutate_provider_payload():
    payload = _payload()
    payload["actionDirective"] = {
        "type": "resolve_poi",
        "targetSegmentId": SEGMENT_ID,
        "searchQuery": "清华美术馆",
    }
    original = copy.deepcopy(payload)

    AgentDecisionNormalizer().normalize(payload, context=_context())

    assert payload == original


def test_user_message_facts_are_removed_from_assumptions_without_relaxing_inferred_assumptions():
    payload = _payload()
    payload["assumptionPolicy"] = "allow_reversible"
    payload["assumptions"] = [
        {"key": "budget", "value": "medium", "source": "user_message"},
        {"key": "opening_hours", "value": "unknown", "source": "model", "reversible": True},
    ]

    result = AgentDecisionNormalizer().normalize(payload, context=_context())

    assert result.normalized["assumptions"] == [
        {"key": "opening_hours", "value": "unknown", "source": "model", "reversible": True}
    ]
    assert result.normalized["assumptionPolicy"] == "allow_reversible"
    assert "userMessageFactsRemovedFromAssumptions" in result.aliases_applied


def _server_owned_clarification_context():
    return {
        "clarificationDimensions": [
            {
                "dimensionId": "route_decision.detour_tolerance",
                "status": "unresolved",
                "allowedSemanticFields": ["detourTolerance"],
                "allowFreeText": False,
                "semanticOptions": [
                    {
                        "id": "detour_minimal",
                        "semanticValue": {
                            "detourTolerance": {"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.15}
                        },
                    },
                    {
                        "id": "detour_balanced",
                        "semanticValue": {
                            "detourTolerance": {"maxGeneralizedCostDelta": 35.0, "maxDetourRatio": 0.35}
                        },
                    },
                ],
            }
        ]
    }


def _server_owned_clarification_payload():
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "ask_user",
        "actionDirective": {
            "type": "ask_user",
            "questions": [
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "question": "本次更看重少绕路还是体验变化？",
                    "whyItMatters": "会决定两所高校之间采用哪一组真实路线阈值。",
                    "options": [
                        {"id": "detour_minimal", "label": "尽量少绕路"},
                        {"id": "detour_balanced", "label": "路线与体验均衡"},
                    ],
                }
            ],
        },
    }


def test_server_owned_clarification_injects_semantics_after_exact_option_identity_check():
    result = AgentDecisionNormalizer().normalize(
        _server_owned_clarification_payload(),
        context=_server_owned_clarification_context(),
    )

    question = result.normalized["actionDirective"]["questions"][0]
    assert question["allowFreeText"] is False
    assert question["options"][0]["semanticValue"]["detourTolerance"]["maxDetourRatio"] == 0.15
    assert "actionDirective.questions[0].options:server_semantics_injected" in result.aliases_applied


def test_server_owned_clarification_rejects_controller_semantic_values_and_changed_option_ids():
    semantic_payload = _server_owned_clarification_payload()
    semantic_payload["actionDirective"]["questions"][0]["options"][0]["semanticValue"] = {
        "detourTolerance": {"maxGeneralizedCostDelta": 999, "maxDetourRatio": 0.99}
    }
    with pytest.raises(DecisionNormalizationError) as semantic_error:
        AgentDecisionNormalizer().normalize(semantic_payload, context=_server_owned_clarification_context())
    assert semantic_error.value.reason_code == "controller_semantic_value_not_allowed"

    identity_payload = _server_owned_clarification_payload()
    identity_payload["actionDirective"]["questions"][0]["options"][1]["id"] = "invented"
    with pytest.raises(DecisionNormalizationError) as identity_error:
        AgentDecisionNormalizer().normalize(identity_payload, context=_server_owned_clarification_context())
    assert identity_error.value.reason_code == "server_semantic_option_identity_mismatch"

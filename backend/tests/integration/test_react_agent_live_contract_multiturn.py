from __future__ import annotations

import json

from src.services.agent_autonomy_service import AgentAutonomyController, AutonomyContextProjector
from src.services.agent_observation_service import AgentObservationBuilder


SEGMENT_ID = "seg_live_museum"
VERSION_ID = "ver_live"


def _request_context() -> dict:
    context = {
        "sessionId": "sess_live_contract",
        "latestUserMessage": "重试一次，将第一天美术馆修改为清华美术馆",
        "effectiveUserMessage": "将第一天美术馆修改为清华美术馆",
        "activeVersionId": VERSION_ID,
        "currentItinerarySnapshot": {
            "id": "plan_live",
            "versionId": VERSION_ID,
            "days": [
                {
                    "id": "day_1",
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": SEGMENT_ID,
                            "kind": "visit",
                            "startTime": "15:30",
                            "endTime": "18:00",
                            "metadata": {"routeAnchor": True, "requirementLevel": "required"},
                            "poi": {
                                "name": "美术馆",
                                "category": "museum",
                                "groundingStatus": "waiting_for_poi_grounding",
                            },
                        }
                    ],
                }
            ],
        },
        "activeConversationTurns": [
            {"id": "turn_user_1", "role": "user", "content": "安排北京两日行程", "turnIndex": 1},
            {"id": "turn_assistant_1", "role": "assistant", "content": "已创建草稿", "turnIndex": 2, "itineraryVersionId": VERSION_ID},
            {"id": "turn_user_2", "role": "user", "content": "重试一次，将第一天美术馆修改为清华美术馆", "turnIndex": 3},
        ],
        "runtimeLimits": {"remainingRunMs": 12000},
    }
    observation = AgentObservationBuilder().build(context)
    context["agentObservation"] = observation.model_dump(by_alias=True)
    return context


def _alias_decision() -> dict:
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "resolve_poi",
        "actionDirective": {
            "type": "resolve_poi",
            "segmentId": SEGMENT_ID,
            "searchText": "清华美术馆",
        },
    }


class _Provider:
    model = "recorded-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        self.calls.append(
            {
                "context": context,
                "timeoutSeconds": timeout_seconds,
                "repairFeedback": repair_feedback,
            }
        )
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value, ensure_ascii=False)


def _run(provider: _Provider):
    context = _request_context()
    observation = AgentObservationBuilder().build(context)
    projected = AutonomyContextProjector().project(context)
    return AgentAutonomyController(provider=provider).decide(
        context["effectiveUserMessage"],
        context,
        projected,
        available_tools={"resolve_poi"},
        runtime_budget_tools={"resolve_poi"},
        observation=observation,
    )


def test_real_segment_id_search_text_alias_is_normalized_and_server_binds_scope():
    provider = _Provider([_alias_decision()])

    result = _run(provider)

    assert result.source == "controller"
    assert result.gated_decision.accepted is True
    assert result.decision.schema_version == "agent-decision-v3"
    assert result.decision.action_directive.target_segment_ids == [SEGMENT_ID]
    assert result.decision.action_directive.search_intent == "清华美术馆"
    assert result.decision.target_scope.segment_ids == [SEGMENT_ID]
    assert result.normalization_aliases == ("searchText", "segmentId")
    assert result.decision_contract_hash
    assert provider.calls[0]["context"]["decisionContractRef"]["sha256"] == result.decision_contract_hash
    assert "decisionContract" not in provider.calls[0]["context"]


def test_target_segment_id_and_matching_day_number_are_normalized():
    payload = _alias_decision()
    payload["actionDirective"] = {
        "type": "resolve_poi",
        "targetSegmentId": SEGMENT_ID,
        "searchQuery": "清华美术馆",
        "dayNumber": 1,
    }

    result = _run(_Provider([payload]))

    assert result.source == "controller"
    assert result.gated_decision.accepted is True
    assert result.normalization_aliases == ("dayNumber", "searchQuery", "targetSegmentId")


def test_schema_repair_receives_exact_contract_paths_allowed_ids_and_example():
    invalid = _alias_decision()
    invalid["actionDirective"]["unsafeWrite"] = True
    provider = _Provider([invalid, _alias_decision()])

    result = _run(provider)

    assert result.source == "controller"
    assert result.schema_repair_attempts == 1
    repair = json.loads(provider.calls[1]["repairFeedback"])
    assert repair["contractHash"] == result.decision_contract_hash
    assert repair["action"] == "resolve_poi"
    assert repair["invalidPaths"] == ["actionDirective.unsafeWrite"]
    assert repair["allowedIds"]["segmentIds"] == [SEGMENT_ID]
    assert repair["allowedIds"]["versionIds"] == [VERSION_ID]
    assert repair["actionSchema"]["additionalProperties"] is False
    assert repair["minimalExample"]["actionDirective"]["type"] == "resolve_poi"
    assert result.repair_reserved_ms > 0


def test_context_projection_recursively_removes_large_provider_fields_but_keeps_identity():
    context = _request_context()
    context["agentObservation"]["candidateState"]["pendingGroups"] = [
        {
            "id": "group_1",
            "sourceSegmentId": SEGMENT_ID,
            "safeCandidates": [
                {
                    "id": "cand_1",
                    "name": "清华大学艺术博物馆",
                    "providerDebug": {"headers": "secret"},
                    "photos": ["x" * 5000],
                    "route": {"polyline": "1,2;" * 5000},
                }
            ],
        }
    ]

    projected = AutonomyContextProjector().project(context)
    serialized = json.dumps(projected, ensure_ascii=False)

    assert SEGMENT_ID in serialized
    assert VERSION_ID in serialized
    assert "providerDebug" not in serialized
    assert "photos" not in serialized
    assert "polyline" not in serialized
    assert projected["recentTurns"][-1]["turnId"] == "turn_user_2"
    assert projected["projectionTelemetry"]["recursiveProjection"] is True


def test_lite_draft_cannot_authorize_high_risk_write():
    provider = _Provider([TimeoutError("full timeout")])

    def decide_lite(context, *, timeout_seconds):
        return json.dumps(
            {
                "schemaVersion": "agent-decision-lite-v1",
                "primaryAction": "draft_itinerary",
                "confidence": 0.8,
                "reasonCode": "requirements_complete",
                "userVisibleReason": "可以开始生成",
            },
            ensure_ascii=False,
        )

    provider.decide_autonomy_lite = decide_lite
    empty_context = {
        "latestUserMessage": "安排两日游",
        "effectiveUserMessage": "安排两日游",
        "runtimeLimits": {"remainingRunMs": 12000},
    }
    observation = AgentObservationBuilder().build(empty_context)
    result = AgentAutonomyController(provider=provider).decide(
        "安排两日游",
        empty_context,
        AutonomyContextProjector().project(empty_context),
        available_tools=set(),
        runtime_budget_tools=set(),
        observation=observation,
    )

    assert result.decision_path == "lite"
    assert result.decision.primary_action == "ask_user"
    assert result.decision.proposed_write_risk == "none"
    assert result.decision.side_effects["itinerary"] is False

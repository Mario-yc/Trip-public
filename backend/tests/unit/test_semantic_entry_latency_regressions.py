"""State and protocol regressions from the bounded real entry baseline."""

import json

import pytest

from backend.tests.unit.test_shared_travel_source import _counts, _payload, _send, intake  # noqa: F401
from src.api.schemas.agent import AgentMessageRequest
from src.services.conversation_action_catalog import ConversationActionCatalog
from src.services.conversation_intent_router import ConversationCapabilityResolver, ConversationIntentRouter
from src.services.shared_travel_source_service import SharedTravelSourceService


def test_entry_failure_before_root_is_not_an_unanswered_business_question(intake):
    source = _send(intake)
    proof = SharedTravelSourceService(intake.db).initial_source(intake.session)
    proof.pop("carrierAssistantTurnId")
    failed = {
        "mode": "clarification",
        "terminalStatus": "needs_confirmation",
        "failureReasonCode": "controller_action_unavailable",
        "clarificationCheckpoint": None,
        "pendingSharedSource": proof,
        "choiceOptions": [],
        "controllerFullCallCount": 0,
    }
    intake.agent._insert_turn(intake.session, "assistant", "尚未调用规划器", "active", agent_response_json=failed)
    before = _counts(intake.db)
    intake.semantic.action = "create_from_shared_guide"
    message = "参考攻略建议的地点，生成新的方案"
    _, result, capability = intake.agent._route_conversation_turn(
        session=intake.agent._session(intake.session),
        content=message,
        payload=AgentMessageRequest(content=message, context={}),
    )
    supplied = intake.semantic.calls[-1]
    assert supplied["state"]["latestAssistant"]["hasPendingClarification"] is False
    assert "answer_clarification" not in {tool["function"]["name"] for tool in supplied["tools"]}
    assert result.semantic_action["name"] == "create_from_shared_guide"
    assert result.semantic_action["sharedSourceBinding"]["sourceAssistantTurnId"] == source.assistant_turn.id
    assert capability.capability == "create_itinerary"
    assert _counts(intake.db) == before
    assert len(intake.reads) == 1


def test_controller_failure_does_not_invent_missing_travel_fields(intake):
    _send(intake)
    intake.semantic.action = "create_from_shared_guide"
    response = _send(intake, content="参考这份攻略安排北京旅行", request_id="missing-fields-inspect")
    saved = _payload(intake.db, response.assistant_turn.id)
    assert saved["failureReasonCode"] == "controller_action_unavailable"
    assert (
        not ConversationCapabilityResolver(intake.db)
        .routing_snapshot(intake.agent._session(intake.session))
        .server_state["pendingClarification"]
    )


@pytest.mark.parametrize(
    "question", [{"clarificationCheckpoint": {"question": "出发日期？"}}, {"clarification": {"question": "出发日期？"}}]
)
def test_actual_business_question_remains_available(intake, question):
    intake.agent._insert_turn(
        intake.session,
        "assistant",
        "出发日期？",
        "active",
        agent_response_json={"mode": "clarification", "terminalStatus": "needs_confirmation", **question},
    )
    snapshot = ConversationCapabilityResolver(intake.db).routing_snapshot(intake.agent._session(intake.session))
    assert snapshot.server_state["pendingClarification"] is True
    assert "answer_clarification" in ConversationActionCatalog(snapshot).names


@pytest.mark.parametrize(
    "message",
    [
        "不要生成方案，只解释这篇攻略的五天行程有什么特点。",
        "不要生成方案只解释这篇攻略有什么特点",
    ],
)
def test_compound_negative_readonly_request_is_understood_as_a_whole(message):
    calls = []

    def model(context):
        calls.append(context)
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {"type": "function", "function": {"name": "explain", "arguments": json.dumps({})}}
                        ]
                    },
                }
            ]
        }

    router = ConversationIntentRouter(model, routing_mode="active-all")
    result = router.classify(message)
    assert len(calls) == 1 and calls[0]["userMessage"] == message
    assert result.semantic_action["name"] == "explain"
    assert result.classification.intent == "inspect_or_explain"
    calls.clear()
    assert router.classify("不要继续生成其他方案").classification.intent == "cancel_action"
    assert calls == []


def test_semantic_transport_requires_a_proposal_but_does_not_force_its_action(monkeypatch):
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider

    provider = DeepSeekAgentProvider(api_key="test-only", model="deepseek-v4-flash")
    sent = []
    monkeypatch.setattr(provider, "_post_json", lambda payload, **kwargs: sent.append((payload, kwargs)))
    snapshot = ConversationIntentRouter._compatibility_snapshot({})
    provider.propose_conversation_action(
        {"tools": ConversationActionCatalog(snapshot).tools(), "userMessage": "只解释原因"}, timeout_seconds=2.5
    )
    payload, options = sent[0]
    assert payload["tool_choice"] == "required"
    assert {"explain", "clarify", "cancel", "unavailable"} <= {tool["function"]["name"] for tool in payload["tools"]}
    assert payload["max_tokens"] == 512
    assert options["timeout_seconds"] == 2.5
    assert payload["thinking"] == {"type": "disabled"}

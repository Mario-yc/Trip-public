"""Inspect final Request.data; no Provider request leaves this test process."""

from copy import deepcopy
from io import BytesIO
import json

import pytest

from src.services import deepseek_agent_provider as provider_module
from src.services.agent_autonomy_service import ModelDecisionV3
from src.services.agent_decision_contract_service import AgentDecisionContractService
from src.services.controller_context_projection_service import FULL_REQUEST_BYTE_LIMIT
from src.services.request_activity_coverage_service import RequestActivityCoverageService


@pytest.fixture
def transport(monkeypatch):
    captured = []

    def capture(request, timeout):
        captured.append({"bytes": request.data, "payload": json.loads(request.data), "timeout": timeout})
        response = BytesIO(json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode("utf-8"))
        response.status = 200
        return response

    monkeypatch.setattr(provider_module, "urlopen", capture)
    provider = provider_module.DeepSeekAgentProvider(
        api_key="unused-test-key", model="test-full-type-transport", timeout_seconds=1,
    )
    provider.base_url = "https://transport-test.invalid/v1"
    return provider, captured


def context(allowed_actions):
    # Intentionally not the eleven-clause activity acceptance fixture: these
    # tests own the HTTP boundary and legal-action gating only.
    return {"schemaVersion": "controller-full-context-v1", "selectedCity": "北京",
            "latestUserMessage": "规划北京一日游。", "allowedActions": list(allowed_actions),
            "decisionConstraints": {}, "goalRequirements": []}


def test_full_draft_guide_and_source_hash_reach_actual_http_request_data(transport):
    provider, captured = transport
    request_context = context(["draft_itinerary", "ask_user", "finish"])
    original = deepcopy(request_context)
    contract = AgentDecisionContractService.full_draft_type_contract()

    assert provider.decide_autonomy(request_context, timeout_seconds=1) == "{}"

    assert len(captured) == 1
    wire = captured[0]
    payload = wire["payload"]
    system = payload["messages"][0]["content"]
    assert system.count(contract["guide"]) == 1
    assert "actionSchemaHash=" + contract["actionSchemaHash"] in system
    assert contract["actionSchemaHash"].encode("utf-8") in wire["bytes"]
    assert json.dumps(contract["guide"], ensure_ascii=False)[1:-1].encode("utf-8") in wire["bytes"]
    assert json.loads(payload["messages"][1]["content"]) == original
    assert request_context == original
    assert len(wire["bytes"]) <= FULL_REQUEST_BYTE_LIMIT == 15360
    assert payload["max_tokens"] == provider_module.CONTROLLER_FULL_MAX_OUTPUT_TOKENS
    assert wire["timeout"] == 1


def test_full_compact_profile_and_activity_semantics_are_sent_without_more_budget(transport):
    provider, captured = transport
    request_context = context(["draft_itinerary", "ask_user"])
    request_context["requestActivityClauses"] = [{
        "clauseId": "request_clause_1", "text": "安排我的行程", "start": 0, "end": 6,
        "goalIds": ["goal_request_1_1", "goal_request_1_2"],
    }]
    original = deepcopy(request_context)
    provider.decide_autonomy(request_context, timeout_seconds=1)

    assert len(captured) == 1
    payload = captured[0]["payload"]
    system = payload["messages"][0]["content"]
    profile = AgentDecisionContractService.full_draft_compact_output_profile()
    assert system.count(profile["guide"]) == 1
    assert "ONE compact JSON line" in system
    assert "what the ASSISTANT must do from what" in system
    assert "NOT a traveller activity" in system
    assert "NEVER optionalGoalIds" in system
    assert "omit the empty activities field" in system
    assert "Schedule hints use estimateSource=controller_estimate" in system
    assert "routePlanningPolicy uses source=controller_estimate" in system
    assert "goalIds are slots for distinct activities, NOT days or visits" in system
    assert "Declare a recurring activity ONCE" in system
    assert "Reuse that goalId across scheduled days and their hints" in system
    assert "instruction" in AgentDecisionContractService.full_draft_type_contract()["guide"]
    # Prompt guidance is not proof of semantic success; the real bounded gate
    # independently checks activities, root, coverage, proposal and adoption.
    assert json.loads(payload["messages"][1]["content"]) == original
    assert payload["max_tokens"] == provider_module.CONTROLLER_FULL_MAX_OUTPUT_TOKENS == 1200
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0.0
    assert captured[0]["timeout"] == 1
    assert len(captured[0]["bytes"]) <= FULL_REQUEST_BYTE_LIMIT


def test_non_draft_full_does_not_build_or_inject_guide_or_change_allowed_actions(transport, monkeypatch):
    provider, captured = transport
    contract = AgentDecisionContractService.full_draft_type_contract()
    request_context = context(["read_itinerary", "verify_external_facts", "ask_user"])
    original = deepcopy(request_context)

    def forbidden(cls):
        pytest.fail("A non-draft Full call must not build a draft type contract")

    monkeypatch.setattr(AgentDecisionContractService, "full_draft_type_contract", classmethod(forbidden))
    assert provider.decide_autonomy(request_context, timeout_seconds=1) == "{}"

    assert len(captured) == 1
    wire = captured[0]
    system = wire["payload"]["messages"][0]["content"]
    assert contract["guide"] not in system
    assert contract["actionSchemaHash"].encode("utf-8") not in wire["bytes"]
    assert json.loads(wire["payload"]["messages"][1]["content"]) == original
    assert request_context == original
    assert len(wire["bytes"]) <= FULL_REQUEST_BYTE_LIMIT


def test_recurring_identity_instruction_preserves_clause_identity_and_output_budget(transport):
    provider, captured = transport
    message = "今年国庆参观北京985高校两日游，每晚都去逛公园，中午想体验当地特色美食。10月1日到2日，2天，中等预算，1人，公交地铁优先"
    prepared = RequestActivityCoverageService.prepare({"dayCount": 2}, message)
    request_context = context(["draft_itinerary", "ask_user"])
    request_context.update(
        latestUserMessage=message,
        availableDayNumbers=[1, 2],
        requestActivityClauses=prepared["requestActivityCoverage"]["clauses"],
    )
    original = deepcopy(request_context)
    provider.decide_autonomy(request_context, timeout_seconds=1)

    assert len(captured) == 1
    payload = captured[0]["payload"]
    system = payload["messages"][0]["content"]
    guidance = provider_module.REQUEST_ACTIVITY_IDENTITY_PROMPT
    assert system.count(guidance) == 1
    assert provider_module.REQUEST_ACTIVITY_COVERAGE_PROMPT.count(guidance) == 1
    assert "Different activities need different goalIds" in guidance
    assert "unused IDs need not be used" in guidance
    sent_context = json.loads(payload["messages"][1]["content"])
    assert sent_context == original == request_context
    assert len(sent_context["requestActivityClauses"]) == 8
    assert all(len(clause["goalIds"]) == 2 for clause in sent_context["requestActivityClauses"])
    assert payload["max_tokens"] == 1200
    assert payload["thinking"] == {"type": "disabled"}
    assert len(captured[0]["bytes"]) <= FULL_REQUEST_BYTE_LIMIT == 15360


def test_over_budget_full_is_rejected_before_urlopen_with_zero_calls(transport):
    provider, captured = transport
    request_context = context(["draft_itinerary", "finish"])
    request_context["latestUserMessage"] = "超" * FULL_REQUEST_BYTE_LIMIT
    assert len(json.dumps(request_context, ensure_ascii=False).encode("utf-8")) > 15360

    with pytest.raises(ValueError, match="^controller_full_payload_too_large$"):
        provider.decide_autonomy(request_context, timeout_seconds=1)

    assert captured == []


def test_repair_keeps_existing_bounded_contract_without_duplicating_full_guide(transport, monkeypatch):
    provider, captured = transport
    request_context = context(["draft_itinerary", "ask_user", "finish"])
    contract_service = AgentDecisionContractService(
        decision_model=ModelDecisionV3, allowed_actions=request_context["allowedActions"],
    )
    repair = contract_service.repair_payload(
        action="draft_itinerary", invalid_paths=["actionDirective.draft_itinerary.dayStrategies.0.pace"],
        allowed_ids={"goalIds": ["goal_museum"]}, aliases_applied=[],
        goal_requirements=[{"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1,
                            "allowedDayNumbers": [1], "requirementLevel": "required"}],
    )
    guide = AgentDecisionContractService.full_draft_type_contract()["guide"]

    def forbidden(cls):
        pytest.fail("Repair already has a bounded schema contract; do not build the Full guide")

    monkeypatch.setattr(AgentDecisionContractService, "full_draft_type_contract", classmethod(forbidden))
    assert provider.decide_autonomy(
        request_context, timeout_seconds=1, repair_feedback=json.dumps(repair, ensure_ascii=False),
    ) == "{}"

    assert len(captured) == 1
    wire = captured[0]
    payload = wire["payload"]
    assert len(payload["messages"]) == 3
    assert guide not in "\n".join(message["content"] for message in payload["messages"])
    assert "DraftItineraryDirective actionSchemaHash=" not in payload["messages"][0]["content"]
    bounded = json.loads(payload["messages"][2]["content"].split("Repair contract: ", 1)[1])
    assert bounded == repair
    assert bounded["allowedActions"] == contract_service.build()["allowedActions"]
    assert bounded["actionSchemaHash"] == contract_service.build()["actionSchemaHashes"]["draft_itinerary"]
    assert json.loads(payload["messages"][1]["content"])["repairMode"] == "draft_itinerary_schema_only"
    assert len(wire["bytes"]) <= FULL_REQUEST_BYTE_LIMIT == 15360
    assert payload["max_tokens"] == provider_module.CONTROLLER_REPAIR_MAX_OUTPUT_TOKENS
    assert wire["timeout"] == 1

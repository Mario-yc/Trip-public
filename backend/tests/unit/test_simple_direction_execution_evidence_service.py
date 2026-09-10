from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from src.api.schemas.agent import AgentInitialPlanOutput, AgentMessageResponse
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
from src.services.simple_direction_execution_evidence_service import (
    SimpleDirectionExecutionEvidenceService,
)
from src.services.simple_open_direction_service import SimpleOpenDirectionService


def _route_contract() -> dict:
    return {
        "schemaVersion": "route-decision-contract-v1",
        "transportMode": "transit",
        "fingerprint": "d" * 64,
    }


def _prepared_execution(
    connection,
    *,
    provider_failure: bool = False,
    settle_attempt: bool = True,
) -> dict:
    conversation = ConversationService(connection)
    session = conversation.create_session("北京", "simple direction execution evidence")
    agent = AgentService(connection)
    source_user_turn_id = agent._insert_turn(session.session_id, "user", "规划 985 高校方向", "active")
    source_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "首个方向已生成",
        "active",
        agent_response_json={},
    )
    request_fingerprint = "r" * 64
    contract = {
        "routeDecisionContract": _route_contract(),
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
    }
    direction = SimpleOpenDirectionService(connection)
    root = direction.ensure_root(
        session_id=session.session_id,
        planning_root_id=source_user_turn_id,
        source_assistant_turn_id=source_assistant_turn_id,
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint=request_fingerprint,
        request_contract=contract,
        locality="北京",
        max_pages_per_query=2,
    )
    choice_id = "choice_simple_direction_more"
    source_option = {
        "id": choice_id,
        "kind": "simple_direction_more_plans",
        "action": "continue_plan_expansion",
        "label": "继续探索其他方向",
        "sourceUserTurnId": source_user_turn_id,
        "sourceAssistantTurnId": source_assistant_turn_id,
        "planningSelectionRootTurnId": source_user_turn_id,
        "rootPortfolioId": root["id"],
        "requestContractFingerprint": request_fingerprint,
        "expectedBaseVersionId": None,
    }
    connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (
            json.dumps(
                {"choiceOptions": [source_option], "consumedChoiceIds": []},
                ensure_ascii=False,
            ),
            source_assistant_turn_id,
        ),
    )
    request_turn_id = agent._insert_turn(
        session.session_id,
        "user",
        "继续探索其他方向",
        "active",
    )
    selected_choice = {
        "sourceAssistantTurnId": source_assistant_turn_id,
        "choiceId": choice_id,
        "requestChoiceId": choice_id,
        "persistedChoiceId": choice_id,
        "action": "continue_plan_expansion",
        "normalizedAction": "continue_plan_expansion",
        "persistedChoiceAction": "continue_plan_expansion",
        "option": copy.deepcopy(source_option),
    }
    claim = agent._claim_fallback_choice_execution(
        session.session_id,
        request_turn_id,
        selected_choice,
    )
    execution_id = str(claim["id"])
    connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "selectedAgentChoice": selected_choice,
                    "fallbackChoiceExecutionClaim": claim,
                },
                ensure_ascii=False,
                default=str,
            ),
            request_turn_id,
        ),
    )
    attempt = direction.claim_frontier_assignment(
        portfolio_id=root["id"],
        execution_id=execution_id,
        campus_slots=[{"dayNumber": 1, "slotId": "slot_RUC"}],
        request_contract_fingerprint=request_fingerprint,
    )
    assignment = attempt["campusAssignments"][0]
    assert assignment["canonicalName"]
    assert (
        EntityQualificationEvidenceService.validate_binding(
            assignment["qualificationBinding"],
            expected_planning_root_id=source_user_turn_id,
            expected_request_contract_fingerprint=request_fingerprint,
            expected_entity_fingerprint=assignment["evidenceEntityFingerprint"],
            expected_canonical_name=assignment["canonicalName"],
        )
        == ""
    )
    result_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "处理中",
        "active",
    )
    result_payload: dict = {}
    if settle_attempt:
        provider_outcome = "failure" if provider_failure else "rejected"
        result_payload = direction.settle_unresolved_frontier_attempt(
            session_id=session.session_id,
            planning_root_id=source_user_turn_id,
            source_assistant_turn_id=result_assistant_turn_id,
            expected_base_version_id=None,
            request_contract_fingerprint=request_fingerprint,
            frontier_execution_id=execution_id,
            frontier_outcomes=[
                {
                    "slotId": assignment["slotId"],
                    "evidenceEntityFingerprint": assignment["evidenceEntityFingerprint"],
                    "providerOutcome": provider_outcome,
                    "selectedAmapId": None,
                    "queryFingerprint": assignment["queryFingerprint"],
                    "page": assignment["page"],
                    "reasonCode": "provider_timeout" if provider_failure else "campus_assignment_candidate_rejected",
                    "rejectionReasonCodes": [] if provider_failure else ["exact_entity_mismatch"],
                }
            ],
            slot_frontier_outcomes=[],
        )
        result_payload.update(
            {
                "mode": "simple_open_direction_proposal",
                "workflowMode": "simple_direction_v1",
                "planningSelectionRootTurnId": source_user_turn_id,
                "rootPortfolioId": root["id"],
                "requestContractFingerprint": request_fingerprint,
                "frontierExecutionId": execution_id,
                "versionDelta": 0,
                "patchDelta": 0,
                "routeWriteDelta": 0,
            }
        )
    connection.execute(
        "UPDATE conversation_turns SET content = ?, agent_response_json = ? WHERE id = ?",
        (
            "Provider 暂时失败" if provider_failure else "本页候选已检查，仍可继续探索",
            json.dumps(result_payload, ensure_ascii=False, default=str),
            result_assistant_turn_id,
        ),
    )
    connection.execute(
        "UPDATE agent_choice_executions SET execution_turn_id = ?, updated_at = ? WHERE id = ?",
        (result_assistant_turn_id, datetime.now(timezone.utc).isoformat(), execution_id),
    )
    connection.commit()
    return {
        "agent": agent,
        "sessionId": session.session_id,
        "sourceAssistantTurnId": source_assistant_turn_id,
        "requestTurnId": request_turn_id,
        "resultAssistantTurnId": result_assistant_turn_id,
        "executionId": execution_id,
        "selectedChoice": selected_choice,
        "resultPayload": result_payload,
    }


def _prepared_compatibility_execution(connection, *, claim_compatibility: bool = True) -> dict:
    """Claim a no-qualification-frontier continuation from a persisted choice."""

    from backend.tests.unit.test_simple_open_direction_workflow import (
        _route_contract as _workflow_route_contract,
        _snapshot,
    )

    conversation = ConversationService(connection)
    session = conversation.create_session("北京", "simple direction compatibility execution")
    agent = AgentService(connection)
    planning_root_id = agent._insert_turn(session.session_id, "user", "规划北京简单方向", "active")
    source_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "已有部分方向，还可以继续探索。",
        "active",
        agent_response_json={},
    )
    request_fingerprint = "c" * 64
    contract = {"routeDecisionContract": _workflow_route_contract()}
    direction = SimpleOpenDirectionService(connection)
    root = direction.ensure_root(
        session_id=session.session_id,
        planning_root_id=planning_root_id,
        source_assistant_turn_id=source_assistant_turn_id,
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint=request_fingerprint,
        request_contract=contract,
        locality="北京",
        max_pages_per_query=3,
    )
    assert root["simpleDirectionFrontier"] == {}
    option = SimpleDirectionExecutionEvidenceService.build_continuation_choice(
        portfolio_id=root["id"],
        source_assistant_turn_id=source_assistant_turn_id,
        planning_root_id=planning_root_id,
        request_fingerprint=request_fingerprint,
        expected_base_version_id=None,
    )
    connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (json.dumps({"choiceOptions": [option], "consumedChoiceIds": []}), source_assistant_turn_id),
    )
    request_turn_id = agent._insert_turn(
        session.session_id,
        "user",
        "继续探索其他方向",
        "active",
    )
    selected_choice = {
        "sourceAssistantTurnId": source_assistant_turn_id,
        "choiceId": option["id"],
        "requestChoiceId": option["id"],
        "persistedChoiceId": option["id"],
        "action": "continue_plan_expansion",
        "normalizedAction": "continue_plan_expansion",
        "persistedChoiceAction": "continue_plan_expansion",
        "option": copy.deepcopy(option),
    }
    claim = agent._claim_fallback_choice_execution(
        session.session_id,
        request_turn_id,
        selected_choice,
    )
    connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "selectedAgentChoice": selected_choice,
                    "fallbackChoiceExecutionClaim": claim,
                },
                ensure_ascii=False,
                default=str,
            ),
            request_turn_id,
        ),
    )
    compatibility_attempt = (
        direction.claim_compatibility_continuation(
            portfolio_id=root["id"],
            execution_id=claim["id"],
            source_assistant_turn_id=source_assistant_turn_id,
            request_turn_id=request_turn_id,
            choice_id=option["id"],
            request_contract_fingerprint=request_fingerprint,
        )
        if claim_compatibility
        else None
    )
    result_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "处理中",
        "active",
    )
    return {
        "agent": agent,
        "session": session,
        "direction": direction,
        "root": root,
        "planningRootId": planning_root_id,
        "sourceAssistantTurnId": source_assistant_turn_id,
        "requestTurnId": request_turn_id,
        "resultAssistantTurnId": result_assistant_turn_id,
        "requestFingerprint": request_fingerprint,
        "requestContract": contract,
        "selectedChoice": selected_choice,
        "executionId": str(claim["id"]),
        "compatibilityAttempt": compatibility_attempt,
        "snapshot": _snapshot(session.active_plan_id, "兼容 continuation 方向", "B000A"),
    }


def _persist_compatibility_result(connection, prepared: dict, payload: dict) -> AgentMessageResponse:
    persisted = {
        **copy.deepcopy(payload),
        "mode": "simple_open_direction_proposal",
        "workflowMode": "simple_direction_v1",
        "planningSelectionRootTurnId": prepared["planningRootId"],
        "rootPortfolioId": prepared["root"]["id"],
        "requestContractFingerprint": prepared["requestFingerprint"],
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
    }
    connection.execute(
        "UPDATE conversation_turns SET content = ?, agent_response_json = ? WHERE id = ?",
        (
            "兼容 continuation 已结算",
            json.dumps(persisted, ensure_ascii=False, default=str),
            prepared["resultAssistantTurnId"],
        ),
    )
    connection.execute(
        "UPDATE agent_choice_executions SET execution_turn_id = ?, updated_at = ? WHERE id = ?",
        (
            prepared["resultAssistantTurnId"],
            datetime.now(timezone.utc).isoformat(),
            prepared["executionId"],
        ),
    )
    connection.commit()
    return AgentMessageResponse(
        userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
        assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
        terminalStatus="needs_confirmation",
    )


def _prepared_v3_compatibility_execution(connection) -> dict:
    prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
    prepared["direction"].claim_compatibility_continuation(
        portfolio_id=prepared["root"]["id"],
        execution_id=prepared["executionId"],
        source_assistant_turn_id=prepared["sourceAssistantTurnId"],
        request_turn_id=prepared["requestTurnId"],
        choice_id=prepared["selectedChoice"]["choiceId"],
        request_contract_fingerprint=prepared["requestFingerprint"],
        defer_slot_scope_claim=True,
    )
    slot_scope = {
        "dayNumber": 1,
        "slotId": "day1_lunch",
        "daySeedAmapId": "B000TEST01",
        "queryScopeFingerprint": "q" * 64,
        "centerRole": "midpoint",
        "queryRole": "adjacent_candidate_center",
        "queryText": "北京 当地特色午餐",
        "priority": 0,
        "currentPartialCompletionSlot": True,
        "predecessorAmapId": "",
        "successorAmapId": "",
        "predecessorBeamRank": 0,
        "initialPageAlreadyAttempted": False,
        "isActiveScope": True,
        "attemptedThisTurn": False,
        "providerOutcome": "",
        "remainingReason": "",
    }
    compatibility_attempt = prepared["direction"].claim_compatibility_current_scopes(
        portfolio_id=prepared["root"]["id"],
        execution_id=prepared["executionId"],
        request_contract_fingerprint=prepared["requestFingerprint"],
        slot_query_scopes=[slot_scope],
    )
    return {
        **prepared,
        "compatibilityAttempt": compatibility_attempt,
        "slotScope": slot_scope,
    }


def _zero_write_counts(connection, session_id: str) -> tuple[int, int, int]:
    return tuple(
        int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        for table in (
            "itinerary_versions",
            "itinerary_patches",
            "timeline_mutation_transactions",
        )
    )


def _prepared_initial_retry_partial(connection) -> dict:
    """Persist a real blocked Simple Direction from one opaque retry choice."""

    from backend.tests.unit.test_simple_open_direction_workflow import _route_contract, _snapshot

    conversation = ConversationService(connection)
    session = conversation.create_session("北京", "simple direction initial retry evidence")
    agent = AgentService(connection)
    planning_root_id = agent._insert_turn(
        session.session_id,
        "user",
        "北京高校两日游，每天中午吃当地美食",
        "active",
    )
    retry_choice_id = "controller-retry:initial-simple-direction"
    retry_option = {
        "id": retry_choice_id,
        "kind": "safe_fallback_action",
        "action": "retry_model_planning",
        "label": "重试生成行程",
        "sourceUserTurnId": planning_root_id,
        "allowsManualInput": False,
    }
    source_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "澄清已完成，但规划控制器本轮未形成可执行规划。",
        "active",
        agent_response_json={
            "mode": "clarification",
            "choiceOptions": [retry_option],
            "consumedChoiceIds": [],
        },
    )
    request_turn_id = agent._insert_turn(
        session.session_id,
        "user",
        "重试生成行程",
        "active",
    )
    selected_choice = {
        "sourceAssistantTurnId": source_assistant_turn_id,
        "choiceId": retry_choice_id,
        "requestChoiceId": retry_choice_id,
        "persistedChoiceId": retry_choice_id,
        "action": "retry_model_planning",
        "normalizedAction": "retry_model_planning",
        "persistedChoiceAction": "retry_model_planning",
        "option": copy.deepcopy(retry_option),
    }
    claim = agent._claim_fallback_choice_execution(
        session.session_id,
        request_turn_id,
        selected_choice,
    )
    result_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "处理中",
        "active",
    )
    connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "selectedAgentChoice": selected_choice,
                    "fallbackChoiceExecutionClaim": claim,
                },
                ensure_ascii=False,
                default=str,
            ),
            request_turn_id,
        ),
    )

    route_contract = _route_contract(compact=True)
    snapshot = _snapshot(session.active_plan_id, "待补全高校方向", "B000A")
    snapshot["status"] = "partial"
    snapshot["routeDecisionContract"] = copy.deepcopy(route_contract)
    snapshot["portfolioPendingSlots"] = [
        {
            "id": "pending:day2:meal",
            "state": "pending",
            "groundingStatus": "unresolved",
            "required": False,
            "requirementLevel": "explicit_soft",
            "completionRequired": True,
            "userExplicit": True,
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "explicit_every_day",
            "intentType": "meal",
            "kind": "meal",
            "goalId": "goal_meal",
            "sourceGoalId": "goal_meal",
            "occurrenceId": "occ:goal_meal:day:2",
            "planningSlotId": "day2_goal_meal",
            "poolId": "pool_meal",
            "dayNumber": 2,
            "displayNeed": "第 2 天午餐",
            "reason": "provider_candidate_frontier_remaining",
            "lineageAuthority": "goal_occurrence_compiler",
            "simpleDirectionProviderExhausted": False,
        }
    ]
    request_fingerprint = "i" * 64
    SimpleOpenDirectionService(connection).ensure_root(
        session_id=session.session_id,
        planning_root_id=planning_root_id,
        source_assistant_turn_id=result_assistant_turn_id,
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint=request_fingerprint,
        request_contract={"routeDecisionContract": route_contract},
        locality="北京",
        max_pages_per_query=3,
    )
    payload = SimpleOpenDirectionService(connection).offer_direction(
        session_id=session.session_id,
        planning_root_id=planning_root_id,
        source_user_turn_id=request_turn_id,
        source_assistant_turn_id=result_assistant_turn_id,
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint=request_fingerprint,
        snapshot=snapshot,
        request_contract={"routeDecisionContract": route_contract},
        remaining_query_scopes=[
            {
                "dayNumber": 2,
                "slotId": "day2_goal_meal",
                "daySeedAmapId": "B000000001",
                "queryScopeFingerprint": "m" * 64,
                "centerRole": "predecessor",
                "queryRole": "adjacent_candidate_center",
                "queryText": "餐厅",
                "priority": 0,
                "currentPartialCompletionSlot": True,
                "predecessorAmapId": "B000000001",
                "successorAmapId": None,
                "predecessorBeamRank": 0,
                "isActiveScope": True,
                "attemptedThisTurn": True,
                "providerOutcome": "success",
                "remainingReason": "no_candidate_selected",
            }
        ],
    )
    payload.update(
        {
            "mode": "simple_open_direction_proposal",
            "workflowMode": "simple_direction_v1",
            "planningSelectionRootTurnId": planning_root_id,
            "requestContractFingerprint": request_fingerprint,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        }
    )
    connection.execute(
        "UPDATE conversation_turns SET content = ?, agent_response_json = ? WHERE id = ?",
        (
            "已生成一个待补全方向。",
            json.dumps(payload, ensure_ascii=False, default=str),
            result_assistant_turn_id,
        ),
    )
    connection.execute(
        "UPDATE agent_choice_executions SET execution_turn_id = ?, updated_at = ? WHERE id = ?",
        (
            result_assistant_turn_id,
            datetime.now(timezone.utc).isoformat(),
            str(claim["id"]),
        ),
    )
    connection.commit()
    return {
        "agent": agent,
        "sessionId": session.session_id,
        "planningRootId": planning_root_id,
        "sourceAssistantTurnId": source_assistant_turn_id,
        "requestTurnId": request_turn_id,
        "resultAssistantTurnId": result_assistant_turn_id,
        "executionId": str(claim["id"]),
        "payload": payload,
    }


def _prepared_initial_retry_frontier_only(connection) -> dict:
    """Persist a real zero-proposal root with one authoritative next step."""

    conversation = ConversationService(connection)
    session = conversation.create_session("北京", "simple direction initial retry frontier")
    agent = AgentService(connection)
    planning_root_id = agent._insert_turn(
        session.session_id,
        "user",
        "规划 985 高校方向",
        "active",
    )
    retry_choice_id = "controller-retry:initial-frontier"
    retry_option = {
        "id": retry_choice_id,
        "kind": "safe_fallback_action",
        "action": "retry_model_planning",
        "label": "重试生成行程",
        "sourceUserTurnId": planning_root_id,
        "allowsManualInput": False,
    }
    source_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "规划控制器本轮未形成可执行规划。",
        "active",
        agent_response_json={
            "mode": "clarification",
            "choiceOptions": [retry_option],
            "consumedChoiceIds": [],
        },
    )
    request_turn_id = agent._insert_turn(
        session.session_id,
        "user",
        "重试生成行程",
        "active",
    )
    selected_choice = {
        "sourceAssistantTurnId": source_assistant_turn_id,
        "choiceId": retry_choice_id,
        "requestChoiceId": retry_choice_id,
        "persistedChoiceId": retry_choice_id,
        "action": "retry_model_planning",
        "normalizedAction": "retry_model_planning",
        "persistedChoiceAction": "retry_model_planning",
        "option": copy.deepcopy(retry_option),
    }
    claim = agent._claim_fallback_choice_execution(
        session.session_id,
        request_turn_id,
        selected_choice,
    )
    result_assistant_turn_id = agent._insert_turn(
        session.session_id,
        "assistant",
        "处理中",
        "active",
    )
    connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "selectedAgentChoice": selected_choice,
                    "fallbackChoiceExecutionClaim": claim,
                },
                ensure_ascii=False,
                default=str,
            ),
            request_turn_id,
        ),
    )

    request_fingerprint = "f" * 64
    contract = {
        "routeDecisionContract": _route_contract(),
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
    }
    direction = SimpleOpenDirectionService(connection)
    root = direction.ensure_root(
        session_id=session.session_id,
        planning_root_id=planning_root_id,
        source_assistant_turn_id=result_assistant_turn_id,
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint=request_fingerprint,
        request_contract=contract,
        locality="北京",
        max_pages_per_query=2,
    )
    comparison_summary = PlanPortfolioStore(connection).simple_direction_comparison_summary(portfolio_id=root["id"])
    assert comparison_summary["frontierStatus"] == "has_more"
    continuation = SimpleDirectionExecutionEvidenceService.build_continuation_choice(
        portfolio_id=root["id"],
        source_assistant_turn_id=result_assistant_turn_id,
        planning_root_id=planning_root_id,
        request_fingerprint=request_fingerprint,
        expected_base_version_id=None,
    )
    payload = {
        "mode": "simple_open_direction_proposal",
        "workflowMode": "simple_direction_v1",
        "planningSelectionRootTurnId": planning_root_id,
        "rootPortfolioId": root["id"],
        "requestContractFingerprint": request_fingerprint,
        "proposalDelta": 0,
        "frontierAttemptConsumed": False,
        "frontierExecutionId": None,
        "frontierStatus": "has_more",
        "comparisonSummary": comparison_summary,
        "comparisonProjections": [],
        "choiceOptions": [continuation],
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
    }
    connection.execute(
        "UPDATE conversation_turns SET content = ?, agent_response_json = ? WHERE id = ?",
        (
            "已建立真实候选前沿，可继续检查下一组候选。",
            json.dumps(payload, ensure_ascii=False, default=str),
            result_assistant_turn_id,
        ),
    )
    connection.execute(
        "UPDATE agent_choice_executions SET execution_turn_id = ?, updated_at = ? WHERE id = ?",
        (
            result_assistant_turn_id,
            datetime.now(timezone.utc).isoformat(),
            str(claim["id"]),
        ),
    )
    connection.commit()
    return {
        "agent": agent,
        "sessionId": session.session_id,
        "sourceAssistantTurnId": source_assistant_turn_id,
        "requestTurnId": request_turn_id,
        "resultAssistantTurnId": result_assistant_turn_id,
        "executionId": str(claim["id"]),
        "payload": payload,
    }


def _real_qualification_root(connection, *, planning_root_id: str = "turn_binding_root") -> tuple[dict, str]:
    session = ConversationService(connection).create_session("北京", "qualification binding root")
    request_fingerprint = "q" * 64
    root = SimpleOpenDirectionService(connection).ensure_root(
        session_id=session.session_id,
        planning_root_id=planning_root_id,
        source_assistant_turn_id="turn_binding_source",
        expected_base_version_id=None,
        source_observation_fingerprint="b" * 64,
        request_contract_fingerprint=request_fingerprint,
        request_contract={
            "routeDecisionContract": _route_contract(),
            "entityQualificationConstraint": {
                "qualificationScheme": "moe_project_classification",
                "qualificationValue": "985",
            },
        },
        locality="北京",
        max_pages_per_query=2,
    )
    return root, request_fingerprint


def test_initial_retry_persisted_blocked_partial_finishes_as_succeeded_and_consumes_choice() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_initial_retry_partial(connection)
        assert prepared["payload"]["proposalDelta"] == 1
        assert prepared["payload"]["comparisonProjections"][0]["adoptionReady"] is False
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)

        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="candidate_refresh_required",
        )
        prepared["agent"]._complete_controller_choice_execution(
            prepared["sessionId"],
            prepared["requestTurnId"],
            response,
        )

        execution = connection.execute(
            "SELECT status, outcome_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        outcome = json.loads(execution["outcome_json"])
        source_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )

        assert execution["status"] == "succeeded"
        assert outcome["reason"] == "new_simple_direction_partial_proposal"
        assert outcome["proposalDelta"] == 1
        assert outcome["zeroWrite"] is True
        assert source_payload["consumedChoiceIds"] == ["controller-retry:initial-simple-direction"]
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


def test_initial_retry_payload_delta_without_persisted_proposal_lineage_fails_closed() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_initial_retry_partial(connection)
        connection.execute(
            "DELETE FROM agent_plan_proposals WHERE portfolio_id = ?",
            (prepared["payload"]["rootPortfolioId"],),
        )
        connection.commit()

        evidence = SimpleDirectionExecutionEvidenceService(connection).verify_initial_retry(
            prepared["executionId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_request_turn_id=prepared["requestTurnId"],
        )
        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="candidate_refresh_required",
        )
        prepared["agent"]._complete_controller_choice_execution(
            prepared["sessionId"],
            prepared["requestTurnId"],
            response,
        )
        execution = connection.execute(
            "SELECT status, outcome_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        outcome = json.loads(execution["outcome_json"])

        assert evidence == {
            "passed": False,
            "reason": "simple_direction_initial_execution_proposal_lineage_missing",
        }
        assert execution["status"] == "failed_terminal"
        assert outcome["reason"] == "simple_direction_initial_execution_proposal_lineage_missing"
        assert outcome["boundedAttemptConsumed"] is True
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


def test_initial_retry_authoritative_frontier_without_proposal_is_consumed_once() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_initial_retry_frontier_only(connection)
        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="candidate_refresh_required",
        )
        prepared["agent"]._complete_controller_choice_execution(
            prepared["sessionId"],
            prepared["requestTurnId"],
            response,
        )

        execution = connection.execute(
            "SELECT status, outcome_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        outcome = json.loads(execution["outcome_json"])
        source_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )

        assert execution["status"] == "succeeded"
        assert outcome["reason"] == "simple_direction_frontier_advanced_without_proposal"
        assert outcome["proposalDelta"] == 0
        assert outcome["frontierStatus"] == "has_more"
        assert source_payload["consumedChoiceIds"] == ["controller-retry:initial-frontier"]
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


@pytest.mark.parametrize("continuation_count", [0, 2])
def test_initial_retry_has_more_requires_exactly_one_bound_continuation(continuation_count: int) -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_initial_retry_partial(connection)
        assert prepared["payload"]["frontierStatus"] == "has_more"
        tampered = copy.deepcopy(prepared["payload"])
        continuation = next(
            option for option in prepared["payload"]["choiceOptions"] if option["action"] == "continue_plan_expansion"
        )
        tampered["choiceOptions"] = [copy.deepcopy(continuation) for _index in range(continuation_count)]
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (
                json.dumps(tampered, ensure_ascii=False, default=str),
                prepared["resultAssistantTurnId"],
            ),
        )
        connection.commit()

        evidence = SimpleDirectionExecutionEvidenceService(connection).verify_initial_retry(
            prepared["executionId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_request_turn_id=prepared["requestTurnId"],
        )

        assert evidence == {
            "passed": False,
            "reason": "simple_direction_initial_execution_next_choice_missing",
        }


def test_initial_retry_partial_rejects_select_capability_even_with_valid_projection() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_initial_retry_partial(connection)
        tampered = copy.deepcopy(prepared["payload"])
        tampered["choiceOptions"].append(
            {
                "id": "forged-select-partial",
                "kind": "plan_proposal",
                "action": "select_plan_proposal",
                "proposalId": tampered["comparisonProjections"][0]["proposalId"],
            }
        )
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (
                json.dumps(tampered, ensure_ascii=False, default=str),
                prepared["resultAssistantTurnId"],
            ),
        )
        connection.commit()

        evidence = SimpleDirectionExecutionEvidenceService(connection).verify_initial_retry(
            prepared["executionId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_request_turn_id=prepared["requestTurnId"],
        )

        assert evidence == {
            "passed": False,
            "reason": "simple_direction_initial_execution_partial_select_capability_present",
        }


def test_persisted_reconciled_frontier_finishes_choice_as_succeeded_and_consumed() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection)
        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(
            prepared["executionId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_request_turn_id=prepared["requestTurnId"],
        )
        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="needs_confirmation",
        )
        prepared["agent"]._complete_controller_choice_execution(
            prepared["sessionId"],
            prepared["requestTurnId"],
            response,
        )
        execution = connection.execute(
            "SELECT status, outcome_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        source_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )

        assert evidence["passed"] is True
        assert evidence["proposalDelta"] == 0
        assert evidence["nextChoiceId"]
        assert execution["status"] == "succeeded"
        assert json.loads(execution["outcome_json"])["boundedAttemptConsumed"] is True
        assert source_payload["consumedChoiceIds"] == ["choice_simple_direction_more"]
        assert {item["action"] for item in prepared["resultPayload"]["choiceOptions"]} == {
            "continue_plan_expansion",
            "search_travel_guide_advice",
        }
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


def test_continuation_response_lineage_uses_current_server_execution_id() -> None:
    execution_id = "choice_exec_current_frontier"

    assert (
        SimpleDirectionExecutionEvidenceService.current_frontier_execution_id(
            pipeline_execution_id=execution_id,
            response_execution_id=None,
            attempt_consumed=True,
        )
        == execution_id
    )
    with pytest.raises(ValueError, match="simple_direction_frontier_execution_identity_mismatch"):
        SimpleDirectionExecutionEvidenceService.current_frontier_execution_id(
            pipeline_execution_id=execution_id,
            response_execution_id="choice_exec_other_frontier",
            attempt_consumed=True,
        )


def test_failed_turn_terminalizes_claimed_frontier_attempt_and_forbids_replay() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection, settle_attempt=False)
        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="candidate_refresh_required",
        )
        response.assistant_turn.status = "failed"
        response.assistant_turn.failure_reason = "simple direction continuation failed after claim"

        prepared["agent"]._complete_controller_choice_execution(
            prepared["sessionId"],
            prepared["requestTurnId"],
            response,
        )

        execution = connection.execute(
            "SELECT status, outcome_json, error_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["selectedChoice"]["option"]["rootPortfolioId"],),
            ).fetchone()["summary_json"]
        )
        attempt = summary["simpleDirectionFrontierAttempts"][prepared["executionId"]]
        source_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )

        assert execution["status"] == "failed_terminal"
        assert json.loads(execution["error_json"])["retryable"] is False
        assert json.loads(execution["outcome_json"])["frontierAttemptConsumed"] is True
        assert attempt["status"] == "reconciled"
        assert attempt["disposition"] == "failed_terminal"
        assert len(attempt["outcomes"]) == len(attempt["attempt"]["campusAssignments"])
        assert {item["providerOutcome"] for item in attempt["outcomes"]} == {"rejected"}
        assert {item["outcomeSource"] for item in attempt["outcomes"]} == {
            "server_fail_closed_terminalization"
        }
        assert prepared["selectedChoice"]["choiceId"] in source_payload["consumedChoiceIds"]
        with pytest.raises(Exception):
            prepared["agent"]._claim_fallback_choice_execution(
                prepared["sessionId"],
                "turn_replay_forbidden",
                copy.deepcopy(prepared["selectedChoice"]),
            )
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


def test_failed_turn_terminalizes_claimed_compatibility_attempt_and_forbids_replay() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="candidate_refresh_required",
        )
        response.assistant_turn.status = "failed"
        response.assistant_turn.failure_reason = "simple direction compatibility continuation failed"

        prepared["agent"]._complete_controller_choice_execution(
            prepared["session"].session_id,
            prepared["requestTurnId"],
            response,
        )

        execution = connection.execute(
            "SELECT status, outcome_json, error_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )
        attempt = summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]
        source_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )

        assert execution["status"] == "failed_terminal"
        assert json.loads(execution["error_json"])["retryable"] is False
        assert json.loads(execution["outcome_json"])["frontierAttemptConsumed"] is True
        assert attempt["status"] == "failed_terminal"
        assert attempt["frontierStatus"] == "failed_terminal"
        assert attempt["reasonCode"] == "simple_direction_execution_failed_after_claim"
        assert attempt["resultAssistantTurnId"] == prepared["resultAssistantTurnId"]
        assert prepared["selectedChoice"]["choiceId"] in source_payload["consumedChoiceIds"]
        with pytest.raises(Exception):
            prepared["agent"]._claim_fallback_choice_execution(
                prepared["session"].session_id,
                "turn_replay_forbidden",
                copy.deepcopy(prepared["selectedChoice"]),
            )
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_failed_terminal_compatibility_attempt_consumes_page_for_next_claim() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)

        assert SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
            prepared["executionId"],
            execution_turn_id=prepared["resultAssistantTurnId"],
            reason_code="simple_direction_execution_failed_after_claim",
        )

        next_source_turn_id = prepared["agent"]._insert_turn(
            prepared["session"].session_id,
            "assistant",
            "仍可继续探索。",
            "active",
            agent_response_json={},
        )
        next_option = SimpleDirectionExecutionEvidenceService.build_continuation_choice(
            portfolio_id=prepared["root"]["id"],
            source_assistant_turn_id=next_source_turn_id,
            planning_root_id=prepared["planningRootId"],
            request_fingerprint=prepared["requestFingerprint"],
            expected_base_version_id=None,
        )
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps({"choiceOptions": [next_option], "consumedChoiceIds": []}), next_source_turn_id),
        )
        next_request_turn_id = prepared["agent"]._insert_turn(
            prepared["session"].session_id,
            "user",
            "继续探索其他方向",
            "active",
        )
        next_selected_choice = {
            "sourceAssistantTurnId": next_source_turn_id,
            "choiceId": next_option["id"],
            "requestChoiceId": next_option["id"],
            "persistedChoiceId": next_option["id"],
            "action": "continue_plan_expansion",
            "normalizedAction": "continue_plan_expansion",
            "persistedChoiceAction": "continue_plan_expansion",
            "option": copy.deepcopy(next_option),
        }
        next_claim = prepared["agent"]._claim_fallback_choice_execution(
            prepared["session"].session_id,
            next_request_turn_id,
            next_selected_choice,
        )

        next_attempt = prepared["direction"].claim_compatibility_continuation(
            portfolio_id=prepared["root"]["id"],
            execution_id=str(next_claim["id"]),
            source_assistant_turn_id=next_source_turn_id,
            request_turn_id=next_request_turn_id,
            choice_id=next_option["id"],
            request_contract_fingerprint=prepared["requestFingerprint"],
        )

        assert prepared["compatibilityAttempt"]["providerPage"] == 2
        assert next_attempt["providerPage"] == 3
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_terminalization_rejects_execution_action_tamper() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection, settle_attempt=False)
        connection.execute(
            "UPDATE agent_choice_executions SET action = 'tampered_action' WHERE id = ?",
            (prepared["executionId"],),
        )
        connection.commit()

        with pytest.raises(ValueError, match="simple_direction_execution_action_mismatch"):
            SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
                prepared["executionId"],
                execution_turn_id=prepared["resultAssistantTurnId"],
                reason_code="simple_direction_execution_failed_after_claim",
            )

        execution = connection.execute(
            "SELECT status FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        assert execution["status"] == "executing"


def test_terminalization_rejects_compatibility_source_turn_tamper() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )
        record = summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]
        record["sourceAssistantTurnId"] = "turn_tampered"
        record["attemptFingerprint"] = PlanPortfolioStore.simple_direction_compatibility_attempt_fingerprint(
            record
        )
        summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]] = record
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), prepared["root"]["id"]),
        )
        connection.commit()

        with pytest.raises(ValueError, match="simple_direction_compatibility_attempt_identity_mismatch"):
            SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
                prepared["executionId"],
                execution_turn_id=prepared["resultAssistantTurnId"],
                reason_code="simple_direction_execution_failed_after_claim",
            )

        execution = connection.execute(
            "SELECT status FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        assert execution["status"] == "executing"


def test_failed_terminal_v3_compatibility_attempt_consumes_slot_query_page_for_next_claim() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_v3_compatibility_execution(connection)

        assert SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
            prepared["executionId"],
            execution_turn_id=prepared["resultAssistantTurnId"],
            reason_code="simple_direction_execution_failed_after_claim",
        )

        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )
        record = summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]
        frontier = summary["simpleDirectionCompatibilityFrontier"]
        slot_snapshot = frontier["slotFrontierSnapshot"]
        slot_outcomes = record["slotQueryOutcomes"]
        claimed_query = record["slotQueries"]["day1_lunch"]
        stored_slot = slot_snapshot["slotFrontiers"][claimed_query["slotFrontierKey"]]

        assert record["status"] == "failed_terminal"
        assert record["providerCalled"] is False
        assert slot_outcomes[0]["providerOutcome"] == "rejected"
        assert slot_outcomes[0]["providerCalled"] is False
        assert slot_outcomes[0]["outcomeSource"] == "server_fail_closed_terminalization"
        assert stored_slot["attemptedPages"] == [1]
        assert stored_slot["nextPage"] == 2
        assert slot_snapshot["frontierFingerprint"] == SimpleDirectionFrontierService._frontier_fingerprint(
            slot_snapshot
        )

        next_source_turn_id = prepared["agent"]._insert_turn(
            prepared["session"].session_id,
            "assistant",
            "仍可继续探索。",
            "active",
            agent_response_json={},
        )
        next_option = SimpleDirectionExecutionEvidenceService.build_continuation_choice(
            portfolio_id=prepared["root"]["id"],
            source_assistant_turn_id=next_source_turn_id,
            planning_root_id=prepared["planningRootId"],
            request_fingerprint=prepared["requestFingerprint"],
            expected_base_version_id=None,
        )
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps({"choiceOptions": [next_option], "consumedChoiceIds": []}), next_source_turn_id),
        )
        next_request_turn_id = prepared["agent"]._insert_turn(
            prepared["session"].session_id,
            "user",
            "继续探索其他方向",
            "active",
        )
        next_selected_choice = {
            "sourceAssistantTurnId": next_source_turn_id,
            "choiceId": next_option["id"],
            "requestChoiceId": next_option["id"],
            "persistedChoiceId": next_option["id"],
            "action": "continue_plan_expansion",
            "normalizedAction": "continue_plan_expansion",
            "persistedChoiceAction": "continue_plan_expansion",
            "option": copy.deepcopy(next_option),
        }
        next_claim = prepared["agent"]._claim_fallback_choice_execution(
            prepared["session"].session_id,
            next_request_turn_id,
            next_selected_choice,
        )
        prepared["direction"].claim_compatibility_continuation(
            portfolio_id=prepared["root"]["id"],
            execution_id=str(next_claim["id"]),
            source_assistant_turn_id=next_source_turn_id,
            request_turn_id=next_request_turn_id,
            choice_id=next_option["id"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            defer_slot_scope_claim=True,
        )
        next_attempt = prepared["direction"].claim_compatibility_current_scopes(
            portfolio_id=prepared["root"]["id"],
            execution_id=str(next_claim["id"]),
            request_contract_fingerprint=prepared["requestFingerprint"],
            slot_query_scopes=[prepared["slotScope"]],
        )

        assert next_attempt["providerPage"] == 2
        assert next_attempt["slotQueries"]["day1_lunch"]["page"] == 2
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_terminalization_rejects_missing_execution_source_user_turn() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection, settle_attempt=False)
        connection.execute(
            "UPDATE agent_choice_executions SET source_user_turn_id = '' WHERE id = ?",
            (prepared["executionId"],),
        )
        connection.commit()

        with pytest.raises(ValueError, match="simple_direction_execution_turn_lineage_missing"):
            SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
                prepared["executionId"],
                execution_turn_id=prepared["resultAssistantTurnId"],
                reason_code="simple_direction_execution_failed_after_claim",
            )


def test_terminalization_rejects_tampered_v3_authoritative_snapshot_even_with_recomputed_attempt_fingerprint() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_v3_compatibility_execution(connection)
        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )
        record = copy.deepcopy(summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]])
        tampered_snapshot = copy.deepcopy(record["slotFrontierSnapshot"])
        tampered_snapshot["remainingQueryScopes"][0]["priority"] = 99
        SimpleDirectionFrontierService._refresh_status(tampered_snapshot)
        record["slotFrontierSnapshot"] = tampered_snapshot
        record["attemptFingerprint"] = PlanPortfolioStore.simple_direction_compatibility_attempt_fingerprint(
            record
        )
        summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]] = record
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), prepared["root"]["id"]),
        )
        connection.commit()

        with pytest.raises(ValueError, match="simple_direction_compatibility_frontier_identity_mismatch"):
            SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
                prepared["executionId"],
                execution_turn_id=prepared["resultAssistantTurnId"],
                reason_code="simple_direction_execution_failed_after_claim",
            )

        execution = connection.execute(
            "SELECT status FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        root_summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )
        assert execution["status"] == "executing"
        assert (
            root_summary["simpleDirectionCompatibilityFrontier"]["slotFrontierSnapshot"]["remainingQueryScopes"][0][
                "priority"
            ]
            == prepared["slotScope"]["priority"]
        )


def test_terminalization_rejects_missing_choice_source_assistant_turn() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection, settle_attempt=False)
        payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )
        payload["choiceOptions"][0].pop("sourceAssistantTurnId", None)
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), prepared["sourceAssistantTurnId"]),
        )
        connection.commit()

        with pytest.raises(ValueError, match="simple_direction_execution_source_choice_mismatch"):
            SimpleDirectionExecutionEvidenceService(connection).terminalize_claimed_frontier_execution(
                prepared["executionId"],
                execution_turn_id=prepared["resultAssistantTurnId"],
                reason_code="simple_direction_execution_failed_after_claim",
            )


def test_legacy_no_material_progress_is_reconciled_once_without_provider_or_writes() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection)
        connection.execute(
            "UPDATE agent_choice_executions SET status = 'failed_retryable', outcome_json = ? WHERE id = ?",
            (json.dumps({"reason": "no_material_progress"}), prepared["executionId"]),
        )
        connection.commit()
        retry_turn_id = prepared["agent"]._insert_turn(
            prepared["sessionId"],
            "user",
            "继续探索其他方向",
            "active",
        )
        replay = prepared["agent"]._claim_fallback_choice_execution(
            prepared["sessionId"],
            retry_turn_id,
            copy.deepcopy(prepared["selectedChoice"]),
        )
        source_row = connection.execute(
            "SELECT * FROM conversation_turns WHERE id = ?",
            (prepared["sourceAssistantTurnId"],),
        ).fetchone()
        source_choices = ConversationService(connection)._choice_options_from_turn(source_row)

        assert replay["id"] == prepared["executionId"]
        assert replay["status"] == "succeeded"
        assert source_choices[0]["lifecycle"] == "consumed"
        assert (
            SimpleDirectionExecutionEvidenceService(connection).reconcile_legacy_execution(prepared["executionId"])
            is False
        )
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


def test_provider_failure_does_not_consume_frontier_or_repair_execution() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection, provider_failure=True)
        connection.execute(
            "UPDATE agent_choice_executions SET status = 'failed_retryable', outcome_json = ? WHERE id = ?",
            (json.dumps({"reason": "no_material_progress"}), prepared["executionId"]),
        )
        connection.commit()
        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])
        repaired = SimpleDirectionExecutionEvidenceService(connection).reconcile_legacy_execution(
            prepared["executionId"]
        )

        assert evidence["passed"] is False
        assert evidence["reason"] in {
            "simple_direction_execution_frontier_not_reconciled",
            "simple_direction_execution_assistant_lineage_mismatch",
            "simple_direction_execution_provider_failure",
        }
        assert repaired is False
        assert (
            connection.execute(
                "SELECT status FROM agent_choice_executions WHERE id = ?",
                (prepared["executionId"],),
            ).fetchone()["status"]
            == "failed_retryable"
        )
        assert _zero_write_counts(connection, prepared["sessionId"]) == (0, 0, 0)


def test_unstarted_continuation_clarification_does_not_consume_choice_or_frontier() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        clarification_payload = {
            "mode": "clarification",
            "reply": "当前没有形成可执行规划。",
            "terminalStatus": "needs_confirmation",
            "failureReasonCode": "controller_action_unavailable",
            "choiceOptions": [],
        }
        connection.execute(
            "UPDATE conversation_turns SET content = ?, agent_response_json = ? WHERE id = ?",
            (
                clarification_payload["reply"],
                json.dumps(clarification_payload, ensure_ascii=False),
                prepared["resultAssistantTurnId"],
            ),
        )
        connection.execute(
            "UPDATE agent_choice_executions SET execution_turn_id = ? WHERE id = ?",
            (prepared["resultAssistantTurnId"], prepared["executionId"]),
        )
        connection.commit()
        response = AgentMessageResponse(
            userTurn=prepared["agent"]._turn_response(prepared["requestTurnId"]),
            assistantTurn=prepared["agent"]._turn_response(prepared["resultAssistantTurnId"]),
            terminalStatus="needs_confirmation",
        )

        prepared["agent"]._complete_controller_choice_execution(
            prepared["session"].session_id,
            prepared["requestTurnId"],
            response,
        )

        execution = connection.execute(
            "SELECT status, outcome_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        source_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )
        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )

        assert execution["status"] == "failed_retryable"
        assert json.loads(execution["outcome_json"])["boundedAttemptConsumed"] is False
        assert prepared["selectedChoice"]["choiceId"] not in source_payload.get("consumedChoiceIds", [])
        assert summary.get("simpleDirectionCompatibilityAttempts") in ({}, None)
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_compatibility_continuation_persists_server_attempt_and_finishes_new_proposal() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        assert prepared["compatibilityAttempt"]["status"] == "claimed"

        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=prepared["snapshot"],
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )
        response = _persist_compatibility_result(connection, prepared, payload)
        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(
            prepared["executionId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_request_turn_id=prepared["requestTurnId"],
        )
        prepared["agent"]._complete_controller_choice_execution(
            prepared["session"].session_id,
            prepared["requestTurnId"],
            response,
        )
        execution = connection.execute(
            "SELECT status, outcome_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        root_summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )

        assert payload["proposalDelta"] == 1
        assert payload["frontierExecutionId"] == prepared["executionId"]
        assert payload["frontierAttemptConsumed"] is True
        assert payload["frontierStatus"] == "has_more"
        assert any(item["action"] == "continue_plan_expansion" for item in payload["choiceOptions"])
        assert root_summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]["status"] == "reconciled"
        assert evidence["passed"] is True
        assert evidence["proposalDelta"] == 1
        assert execution["status"] == "succeeded"
        assert json.loads(execution["outcome_json"])["boundedAttemptConsumed"] is True
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_visible_proposal_rehydrates_same_occurrence_verified_required_candidate() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=prepared["snapshot"],
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )

        by_occurrence = prepared["direction"].prior_required_occurrence_candidates(
            session_id=prepared["session"].session_id,
            portfolio_id=prepared["root"]["id"],
        )

        assert payload["proposalDelta"] == 1
        assert set(by_occurrence) == {"occ:goal_campus:day:1"}
        assert len(by_occurrence["occ:goal_campus:day:1"]) == 1
        candidate = by_occurrence["occ:goal_campus:day:1"][0]
        assert candidate.amap_id == "B000A6EA36"
        assert candidate.name == "清华大学"
        assert candidate.source == "amap-place-search"
        assert candidate.latitude == 39.99
        assert candidate.longitude == 116.31

        proposal_row = connection.execute(
            "SELECT id, snapshot_json FROM agent_plan_proposals WHERE portfolio_id = ?",
            (prepared["root"]["id"],),
        ).fetchone()
        valid_snapshot = json.loads(proposal_row["snapshot_json"])
        invalid_material = [
            ("source", "agent-text-timeline"),
            ("amapId", "invalid-amap-id"),
            ("latitude", float("nan")),
            ("latitude", 999.0),
            ("latitude", 0.0),
        ]
        for field, value in invalid_material:
            invalid_snapshot = copy.deepcopy(valid_snapshot)
            invalid_snapshot["days"][0]["segments"][0]["poi"][field] = value
            connection.execute(
                "UPDATE agent_plan_proposals SET snapshot_json = ? WHERE id = ?",
                (json.dumps(invalid_snapshot, ensure_ascii=False), proposal_row["id"]),
            )
            assert (
                prepared["direction"].prior_required_occurrence_candidates(
                    session_id=prepared["session"].session_id,
                    portfolio_id=prepared["root"]["id"],
                )
                == {}
            ), field
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_compatibility_collision_without_query_candidate_or_route_progress_stops_capability() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        seed = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["planningRootId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="s" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=copy.deepcopy(prepared["snapshot"]),
            request_contract=prepared["requestContract"],
        )
        assert seed["proposalDelta"] == 1

        collision = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="t" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=copy.deepcopy(prepared["snapshot"]),
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )

        assert prepared["compatibilityAttempt"]["providerPage"] == 2
        assert collision["proposalDelta"] == 0
        assert collision["status"] == "no_progress"
        assert collision["reasonCode"] == "no_progress_no_query_candidate_or_route_delta"
        assert collision["frontierStatus"] == "poi_exhausted"
        assert collision["progress"] == {
            "madeProgress": False,
            "queryProgress": False,
            "candidateProgress": False,
            "routeProgress": False,
        }
        continuation = [item for item in collision["choiceOptions"] if item["action"] == "continue_plan_expansion"]
        assert continuation == []


def test_compatibility_blocked_proposal_keeps_retry_while_exact_page_remains() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        blocked_snapshot = copy.deepcopy(prepared["snapshot"])
        blocked_snapshot["status"] = "partial"
        blocked_snapshot["portfolioPendingSlots"] = [
            {
                "id": "pending:day1_unresolved_park",
                "state": "pending",
                "groundingStatus": "unresolved",
                "required": True,
                "requirementLevel": "hard",
                "intentType": "park",
                "kind": "park",
                "goalId": "goal_park",
                "sourceGoalId": "goal_park",
                "occurrenceId": "occ:goal_park:day:1",
                "planningSlotId": "day1_unresolved_park",
                "poolId": "park_pool",
                "dayNumber": 1,
                "lineageAuthority": "goal_occurrence_compiler",
                "routeAnchorExpected": True,
                "simpleDirectionProviderExhausted": False,
                "simpleDirectionRequirementLineageConflict": False,
                "reasonCode": "candidate_semantics_unresolved",
            }
        ]
        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=blocked_snapshot,
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )
        response = _persist_compatibility_result(connection, prepared, payload)
        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])
        prepared["agent"]._complete_controller_choice_execution(
            prepared["session"].session_id,
            prepared["requestTurnId"],
            response,
        )
        execution = connection.execute(
            "SELECT status FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()

        assert payload["proposalDelta"] == 1
        assert payload["adoptionReadyProposalCount"] == 0
        assert payload["frontierStatus"] == "has_more"
        assert payload["frontierAttemptConsumed"] is True
        continuation = [item for item in payload["choiceOptions"] if item["action"] == "continue_plan_expansion"]
        assert len(continuation) == 1
        assert evidence["passed"] is True
        assert evidence["nextChoiceId"] == continuation[0]["id"]
        assert execution["status"] == "succeeded"
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_agent_preprovider_stage_does_not_fabricate_exact_compatibility_scope() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        request_context = {
            "requestIntentContract": copy.deepcopy(prepared["requestContract"]),
            "planningSelectionRootTurnId": prepared["planningRootId"],
            "rootPortfolioId": prepared["root"]["id"],
            "selectedAgentChoice": copy.deepcopy(prepared["selectedChoice"]),
            "fallbackChoiceExecutionClaim": {"id": prepared["executionId"], "status": "executing"},
            "_simpleDirectionGenerationAuthorized": True,
        }
        pipeline_context = {
            "city": "北京",
            "requestIntentContract": copy.deepcopy(prepared["requestContract"]),
        }
        events: list[dict] = []

        initial_plan = AgentInitialPlanOutput.model_validate(
            {
                "reply": "",
                "mode": "plan",
                "daySlots": [
                    {
                        "slotId": "day1_campus",
                        "dayNumber": 1,
                        "startTime": "09:00",
                        "durationMinutes": 120,
                        "kind": "campus",
                        "rawNeed": "参观高校",
                        "routeAnchor": True,
                    },
                    {
                        "slotId": "day1_lunch",
                        "dayNumber": 1,
                        "startTime": "12:00",
                        "durationMinutes": 75,
                        "kind": "meal",
                        "rawNeed": "当地特色午餐",
                        "routeAnchor": True,
                    },
                    {
                        "slotId": "day1_night",
                        "dayNumber": 1,
                        "startTime": "19:00",
                        "durationMinutes": 90,
                        "kind": "park",
                        "rawNeed": "公共城市夜景空间",
                        "routeAnchor": True,
                    },
                ],
                "intentPools": [
                    {
                        "poolId": "campus_pool",
                        "rawNeed": "参观高校",
                        "city": "北京",
                        "intentType": "campus_visit",
                        "targetCount": 1,
                        "requirementLevel": "required",
                        "assignToSlots": ["day1_campus"],
                        "candidateHints": ["北京 高校"],
                    },
                    {
                        "poolId": "meal_pool",
                        "rawNeed": "当地特色午餐",
                        "city": "北京",
                        "intentType": "meal",
                        "targetCount": 1,
                        "requirementLevel": "optional",
                        "assignToSlots": ["day1_lunch"],
                        "candidateHints": ["北京 当地特色餐厅"],
                    },
                    {
                        "poolId": "night_pool",
                        "rawNeed": "公共城市夜景空间",
                        "city": "北京",
                        "intentType": "night_view",
                        "targetCount": 1,
                        "requirementLevel": "required",
                        "assignToSlots": ["day1_night"],
                        "candidateHints": ["北京 公共城市夜景空间"],
                    },
                ],
            }
        )

        prepared["agent"]._prepare_simple_open_direction_frontier(
            session_id=prepared["session"].session_id,
            user_turn_id=prepared["requestTurnId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            city="北京",
            active_version_id=None,
            request_context=request_context,
            pipeline_context=pipeline_context,
            initial_plan=initial_plan,
            resolved_dates={
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-01",
                "dates": ["2026-10-01"],
            },
            tool_events=events,
        )
        persisted = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]

        assert pipeline_context["simpleDirectionFrontierExecutionId"] == prepared["executionId"]
        assert pipeline_context["simpleDirectionCompatibilityAttempt"]["attemptFingerprint"]
        # The exact current-campus scope is late-bound below this stage, so its
        # first real Provider page remains page 1 regardless of prior directions.
        assert pipeline_context["simpleDirectionCompatibilityAttempt"]["providerPage"] == 1
        assert pipeline_context["simpleDirectionCompatibilityAttempt"]["maxPagesPerQuery"] == 3
        assignment = pipeline_context["simpleDirectionFrontierAssignment"]
        assert assignment["schemaVersion"] == "simple-direction-compatibility-query-pages-v2"
        assert assignment["slotQueries"] == {}
        assert assignment["slotFrontierSnapshot"] == {}
        assert assignment["remainingQueryScopes"] == []
        assert persisted["status"] == "claimed"
        assert persisted["sourceAssistantTurnId"] == prepared["sourceAssistantTurnId"]
        assert persisted["requestTurnId"] == prepared["requestTurnId"]
        assert events[-1]["metadata"]["resultPreview"]["frontierExecutionId"] == prepared["executionId"]
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_compatibility_continuation_no_novelty_is_consumed_and_exhausts_capability() -> None:
    clear_database()
    with open_db() as connection:
        first = _prepared_compatibility_execution(connection)
        first_payload = first["direction"].offer_direction(
            session_id=first["session"].session_id,
            planning_root_id=first["planningRootId"],
            source_user_turn_id=first["requestTurnId"],
            source_assistant_turn_id=first["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=first["requestFingerprint"],
            snapshot=first["snapshot"],
            request_contract=first["requestContract"],
            frontier_execution_id=first["executionId"],
        )
        _persist_compatibility_result(connection, first, first_payload)
        first["agent"]._complete_controller_choice_execution(
            first["session"].session_id,
            first["requestTurnId"],
            AgentMessageResponse(
                userTurn=first["agent"]._turn_response(first["requestTurnId"]),
                assistantTurn=first["agent"]._turn_response(first["resultAssistantTurnId"]),
                terminalStatus="needs_confirmation",
            ),
        )

        next_option = next(
            item for item in first_payload["choiceOptions"] if item["action"] == "continue_plan_expansion"
        )
        second_request_turn_id = first["agent"]._insert_turn(
            first["session"].session_id,
            "user",
            "继续探索其他方向",
            "active",
        )
        selected_choice = {
            "sourceAssistantTurnId": first["resultAssistantTurnId"],
            "choiceId": next_option["id"],
            "requestChoiceId": next_option["id"],
            "persistedChoiceId": next_option["id"],
            "action": "continue_plan_expansion",
            "normalizedAction": "continue_plan_expansion",
            "persistedChoiceAction": "continue_plan_expansion",
            "option": copy.deepcopy(next_option),
        }
        second_claim = first["agent"]._claim_fallback_choice_execution(
            first["session"].session_id,
            second_request_turn_id,
            selected_choice,
        )
        connection.execute(
            "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "selectedAgentChoice": selected_choice,
                        "fallbackChoiceExecutionClaim": second_claim,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                second_request_turn_id,
            ),
        )
        frozen_frontier = PlanPortfolioStore(connection).initialize_simple_direction_compatibility_frontier(
            portfolio_id=first["root"]["id"],
            expected_request_contract_fingerprint=first["requestFingerprint"],
            max_pages_per_query=10,
        )
        assert frozen_frontier["maxPagesPerQuery"] == 3
        second_compatibility_attempt = first["direction"].claim_compatibility_continuation(
            portfolio_id=first["root"]["id"],
            execution_id=second_claim["id"],
            source_assistant_turn_id=first["resultAssistantTurnId"],
            request_turn_id=second_request_turn_id,
            choice_id=next_option["id"],
            request_contract_fingerprint=first["requestFingerprint"],
            max_pages_per_query=10,
        )
        assert first["compatibilityAttempt"]["providerPage"] == 2
        assert second_compatibility_attempt["providerPage"] == 3
        assert second_compatibility_attempt["maxPagesPerQuery"] == 3
        second_result_turn_id = first["agent"]._insert_turn(
            first["session"].session_id,
            "assistant",
            "处理中",
            "active",
        )
        second = {
            **first,
            "sourceAssistantTurnId": first["resultAssistantTurnId"],
            "requestTurnId": second_request_turn_id,
            "resultAssistantTurnId": second_result_turn_id,
            "selectedChoice": selected_choice,
            "executionId": str(second_claim["id"]),
        }
        second_payload = first["direction"].offer_direction(
            session_id=first["session"].session_id,
            planning_root_id=first["planningRootId"],
            source_user_turn_id=second_request_turn_id,
            source_assistant_turn_id=second_result_turn_id,
            expected_base_version_id=None,
            source_observation_fingerprint="q" * 64,
            request_contract_fingerprint=first["requestFingerprint"],
            snapshot=copy.deepcopy(first["snapshot"]),
            request_contract=first["requestContract"],
            frontier_execution_id=second["executionId"],
        )
        second_response = _persist_compatibility_result(connection, second, second_payload)
        second_evidence = SimpleDirectionExecutionEvidenceService(connection).verify(second["executionId"])
        first["agent"]._complete_controller_choice_execution(
            first["session"].session_id,
            second_request_turn_id,
            second_response,
        )
        second_execution = connection.execute(
            "SELECT status FROM agent_choice_executions WHERE id = ?",
            (second["executionId"],),
        ).fetchone()

        assert second_payload["proposalDelta"] == 0
        assert second_payload["reasonCode"] == "no_progress_no_query_candidate_or_route_delta"
        assert second_payload["frontierAttemptConsumed"] is True
        assert second_payload["frontierStatus"] != "has_more"
        assert not any(item["action"] == "continue_plan_expansion" for item in second_payload["choiceOptions"])
        assert second_evidence["passed"] is True, second_evidence
        assert second_execution["status"] == "succeeded"
        assert _zero_write_counts(connection, first["session"].session_id) == (0, 0, 0)

        terminal_decision = first["agent"]._conversation_intent_decision(
            {
                "sessionId": first["session"].session_id,
                "planningSelectionRootTurnId": first["planningRootId"],
                "conversationIntent": {
                    "classification": {"intent": "continue_plan_expansion"},
                    "executionDisposition": "execute",
                },
                "conversationCapability": {
                    "status": "none",
                    "reasonCode": "no_matching_capability",
                },
            },
            cycle_index=0,
        )
        assert terminal_decision is not None
        assert terminal_decision.decision.primary_action == "finish"
        assert terminal_decision.decision.clarification is None
        assert "地点分页上限" in terminal_decision.decision.user_visible_reason


def test_compatibility_continuation_provider_failure_stays_unconsumed_and_retryable() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=prepared["snapshot"],
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
            frontier_outcomes=[
                {
                    "providerOutcome": "failure",
                    "reasonCode": "provider_timeout",
                }
            ],
        )
        _persist_compatibility_result(connection, prepared, payload)
        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])
        attempt = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]
        reloaded_root = prepared["direction"].root_for_planning_root(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
        )

        assert payload["proposalDelta"] == 0
        assert payload["reasonCode"] == "simple_direction_frontier_provider_pending"
        assert payload.get("frontierAttemptConsumed") is not True
        assert attempt["status"] == "provider_pending"
        assert reloaded_root["simpleDirectionFrontierStatus"] == "provider_pending"
        assert evidence["passed"] is False
        assert evidence["reason"] in {
            "simple_direction_execution_frontier_not_reconciled",
            "simple_direction_execution_provider_failure",
        }
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_compatibility_reconciled_replay_requires_exact_result_tuple_and_fingerprint() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=prepared["snapshot"],
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )
        _persist_compatibility_result(connection, prepared, payload)
        summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )
        record = summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]
        other_result_turn_id = prepared["agent"]._insert_turn(
            prepared["session"].session_id,
            "assistant",
            "另一次结果",
            "active",
        )

        with pytest.raises(ValueError, match="simple_direction_compatibility_reconcile_replay_mismatch"):
            PlanPortfolioStore(connection).reconcile_simple_direction_compatibility_attempt(
                portfolio_id=prepared["root"]["id"],
                execution_id=prepared["executionId"],
                result_assistant_turn_id=other_result_turn_id,
                proposal_id=record["proposalId"],
                proposal_delta=1,
                disposition=record["disposition"],
                expected_request_contract_fingerprint=prepared["requestFingerprint"],
                frontier_status=record["frontierStatus"],
            )

        record["reasonCode"] = "persisted_reason"
        summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]] = record
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), prepared["root"]["id"]),
        )
        connection.commit()
        with pytest.raises(ValueError, match="simple_direction_compatibility_reconcile_replay_mismatch"):
            PlanPortfolioStore(connection).reconcile_simple_direction_compatibility_attempt(
                portfolio_id=prepared["root"]["id"],
                execution_id=prepared["executionId"],
                result_assistant_turn_id=prepared["resultAssistantTurnId"],
                proposal_id=record["proposalId"],
                proposal_delta=1,
                disposition=record["disposition"],
                expected_request_contract_fingerprint=prepared["requestFingerprint"],
                frontier_status=record["frontierStatus"],
                reason_code="",
            )

        summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]["choiceId"] = "tampered"
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), prepared["root"]["id"]),
        )
        connection.commit()
        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])

        assert evidence == {
            "passed": False,
            "reason": "simple_direction_execution_compatibility_attempt_mismatch",
        }
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_expired_reconciled_compatibility_execution_recovers_without_provider_replay() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=prepared["snapshot"],
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )
        atomic_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["resultAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )
        connection.execute(
            "UPDATE agent_choice_executions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (prepared["executionId"],),
        )
        connection.commit()

        recovered = prepared["agent"]._claim_fallback_choice_execution(
            prepared["session"].session_id,
            "turn_after_crash",
            copy.deepcopy(prepared["selectedChoice"]),
        )

        assert atomic_payload["frontierExecutionId"] == prepared["executionId"]
        assert atomic_payload["frontierAttemptConsumed"] is True
        assert atomic_payload["proposalDelta"] == payload["proposalDelta"] == 1
        assert recovered["status"] == "succeeded"
        assert recovered["execution_turn_id"] == prepared["resultAssistantTurnId"]
        assert json.loads(recovered["outcome_json"]).get("recovered") is not True
        recovered_source = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (prepared["sourceAssistantTurnId"],),
            ).fetchone()["agent_response_json"]
        )
        assert recovered_source["consumedChoiceIds"] == [prepared["selectedChoice"]["choiceId"]]
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_expired_claimed_compatibility_execution_terminalizes_without_provider_replay() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        connection.execute(
            "UPDATE agent_choice_executions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (prepared["executionId"],),
        )
        connection.commit()

        with pytest.raises(Exception) as error:
            prepared["agent"]._claim_fallback_choice_execution(
                prepared["session"].session_id,
                "turn_after_crash",
                copy.deepcopy(prepared["selectedChoice"]),
            )

        execution = connection.execute(
            "SELECT status, outcome_json, error_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        attempt = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]

        assert getattr(error.value, "status_code", None) == 409
        assert execution["status"] == "failed_terminal"
        assert json.loads(execution["error_json"])["retryable"] is False
        assert attempt["status"] == "failed_terminal"
        assert attempt["frontierStatus"] == "failed_terminal"
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


def test_expired_provider_pending_compatibility_execution_stays_retryable_without_provider_call() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=prepared["snapshot"],
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
            frontier_outcomes=[
                {
                    "providerOutcome": "failure",
                    "reasonCode": "provider_timeout",
                }
            ],
        )
        connection.execute(
            "UPDATE agent_choice_executions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (prepared["executionId"],),
        )
        connection.commit()

        recovered = SimpleDirectionExecutionEvidenceService(
            connection
        ).recover_or_terminalize_expired_compatibility_execution(prepared["executionId"])
        execution = connection.execute(
            "SELECT status, outcome_json, error_json FROM agent_choice_executions WHERE id = ?",
            (prepared["executionId"],),
        ).fetchone()
        attempt = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (prepared["root"]["id"],),
            ).fetchone()["summary_json"]
        )["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]

        assert recovered["status"] == "failed_retryable"
        assert execution["status"] == "failed_retryable"
        assert json.loads(execution["error_json"])["retryable"] is True
        assert attempt["status"] == "provider_pending"
        assert recovered["frontierAttemptConsumed"] is False
        assert _zero_write_counts(connection, prepared["session"].session_id) == (0, 0, 0)


@pytest.mark.parametrize(
    ("seed_number", "expected_name"),
    [(0, "清华大学"), (1, "北京航空航天大学"), (4, "北京大学"), (8, "中国人民大学")],
)
def test_reload_reissues_missing_continuation_for_latest_route_blocked_partial(
    monkeypatch,
    seed_number: int,
    expected_name: str,
) -> None:
    from backend.tests.unit.test_simple_open_direction_workflow import (
        _route_contract as _workflow_route_contract,
        _snapshot,
    )

    monkeypatch.setattr(
        SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(lambda: format(seed_number, "032x"))
    )
    qualification_evidence = {
        "schemaVersion": "entity-qualification-evidence-v1",
        "contentSha256": "e" * 64,
        "qualificationScheme": "moe_project_classification",
        "qualificationValue": "985",
        "entities": [
            {"canonicalName": name, "locality": "北京"}
            for name in ["中国人民大学", "北京航空航天大学", "北京大学", "清华大学"]
        ],
    }
    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "qualified_entities",
        classmethod(lambda _cls, **_kwargs: copy.deepcopy(qualification_evidence)),
    )
    clear_database()
    with open_db() as connection:
        conversation = ConversationService(connection)
        session = conversation.create_session("北京", "missing continuation reload")
        agent = AgentService(connection)
        root_turn_id = agent._insert_turn(session.session_id, "user", "规划 985 高校方向", "active")
        source_turn_id = agent._insert_turn(
            session.session_id,
            "assistant",
            "已有部分方向，还可以继续探索。",
            "active",
            agent_response_json={},
        )
        request_fingerprint = "m" * 64
        route_contract = _workflow_route_contract(compact=True)
        contract = {
            "routeDecisionContract": route_contract,
            "entityQualificationConstraint": {
                "qualificationScheme": "moe_project_classification",
                "qualificationValue": "985",
            },
        }
        direction = SimpleOpenDirectionService(connection)
        root = direction.ensure_root(
            session_id=session.session_id,
            planning_root_id=root_turn_id,
            source_assistant_turn_id=source_turn_id,
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            request_contract=contract,
            locality="北京",
            max_pages_per_query=3,
        )
        attempt = direction.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_route_partial",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_B000RUC001"}],
            request_contract_fingerprint=request_fingerprint,
        )
        assignment = attempt["campusAssignments"][0]
        evidence_entity = next(
            entity
            for entity in qualification_evidence["entities"]
            if EntityQualificationEvidenceService.entity_fingerprint(
                evidence_fingerprint=qualification_evidence["contentSha256"], entity=entity
            )
            == assignment["evidenceEntityFingerprint"]
        )
        assert evidence_entity["canonicalName"] == assignment["canonicalName"] == expected_name
        selected_amap_id = "B" + assignment["evidenceEntityFingerprint"][:10].upper()
        snapshot = _snapshot(
            session.active_plan_id,
            "路线待补的高校方向",
            selected_amap_id,
            poi_name=evidence_entity["canonicalName"],
        )
        campus_segment = snapshot["days"][0]["segments"][0]
        campus_segment["semanticMetadata"]["planningSlotId"] = assignment["slotId"]
        assert campus_segment["poi"]["name"] == evidence_entity["canonicalName"]
        snapshot["status"] = "partial"
        snapshot["routeDecisionContract"] = copy.deepcopy(contract["routeDecisionContract"])
        snapshot["simpleOpenRouteAssignment"] = {
            "schemaVersion": "simple-open-route-evidence-v2",
            "routeContractFingerprint": contract["routeDecisionContract"]["fingerprint"],
            "expectedPairs": [{"fromAmapId": selected_amap_id, "toAmapId": "B000MEAL01"}],
            "verifiedPairs": [],
            "providerRoutePairs": [],
            "routeCoverageComplete": False,
            "routeFeasibilityExhausted": False,
            "adjacentLegCompliance": "pending",
            "detourCompliance": "not_evaluated",
            "topologyCompliance": "verified",
            "failureReason": "provider_route_matrix_incomplete",
        }
        response = direction.offer_direction(
            session_id=session.session_id,
            planning_root_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            source_assistant_turn_id=source_turn_id,
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=request_fingerprint,
            snapshot=snapshot,
            request_contract=contract,
            frontier_execution_id="choice_exec_route_partial",
            frontier_outcomes=[
                {
                    "slotId": assignment["slotId"],
                    "evidenceEntityFingerprint": assignment["evidenceEntityFingerprint"],
                    "providerOutcome": "success",
                    "selectedAmapId": selected_amap_id,
                    "queryFingerprint": assignment["queryFingerprint"],
                    "page": assignment["page"],
                    "reasonCode": None,
                }
            ],
            slot_frontier_outcomes=[],
        )
        assert response["proposalDelta"] == 1, {
            key: response.get(key)
            for key in (
                "status",
                "reasonCode",
                "frontierStatus",
                "comparisonSummary",
                "simpleDirectionNoveltyEvidence",
            )
        }
        assert response["frontierStatus"] == "has_more"
        assert [item["action"] for item in response["choiceOptions"]] == [
            "continue_plan_expansion",
            "search_travel_guide_advice",
        ]

        # Recreate the persisted shape from the reported live failure: the
        # partial proposal and reconciled attempt exist, but the source turn
        # incorrectly says provider_pending and contains no opaque choice.
        legacy_payload = copy.deepcopy(response)
        legacy_payload["mode"] = "simple_open_direction_proposal"
        legacy_payload["frontierAttemptConsumed"] = True
        legacy_payload["versionDelta"] = 0
        legacy_payload["patchDelta"] = 0
        legacy_payload["routeWriteDelta"] = 0
        legacy_payload["choiceOptions"] = []
        legacy_payload["frontierStatus"] = "provider_pending"
        legacy_payload["comparisonSummary"]["frontierStatus"] = "provider_pending"
        row = connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (root["id"],),
        ).fetchone()
        root_summary = json.loads(row["summary_json"])
        root_summary["simpleDirectionFrontier"]["frontierStatus"] = "provider_pending"
        root_summary["comparisonSummary"]["frontierStatus"] = "provider_pending"
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(root_summary, ensure_ascii=False), root["id"]),
        )
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(legacy_payload, ensure_ascii=False), source_turn_id),
        )
        connection.commit()

        source_row = connection.execute(
            "SELECT * FROM conversation_turns WHERE id = ?",
            (source_turn_id,),
        ).fetchone()
        repaired_directly = SimpleDirectionExecutionEvidenceService(
            connection
        ).reconcile_missing_continuation_for_source_turn(
            session_id=session.session_id,
            source_turn_id=source_turn_id,
        )
        assert repaired_directly is True, {
            "frontierAttemptConsumed": legacy_payload.get("frontierAttemptConsumed"),
            "proposalDelta": legacy_payload.get("proposalDelta"),
            "deltas": [legacy_payload.get(key) for key in ("versionDelta", "patchDelta", "routeWriteDelta")],
            "summary": PlanPortfolioStore(connection).simple_direction_comparison_summary(portfolio_id=root["id"]),
        }
        # Recreate the same pre-reconciliation input once more so this call
        # verifies that ConversationService performs the repair and reloads
        # the freshly persisted source turn in the same response.
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(legacy_payload, ensure_ascii=False), source_turn_id),
        )
        connection.commit()
        source_row = connection.execute(
            "SELECT * FROM conversation_turns WHERE id = ?",
            (source_turn_id,),
        ).fetchone()
        first_reload = conversation._choice_options_from_turn(source_row)
        source_row = connection.execute(
            "SELECT * FROM conversation_turns WHERE id = ?",
            (source_turn_id,),
        ).fetchone()
        second_reload = conversation._choice_options_from_turn(source_row)
        persisted_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (source_turn_id,),
            ).fetchone()["agent_response_json"]
        )

        assert len(first_reload) == 1
        assert first_reload[0]["action"] == "continue_plan_expansion"
        assert first_reload[0]["lifecycle"] == "offered"
        assert second_reload == first_reload
        assert persisted_payload["frontierStatus"] == "has_more"
        assert persisted_payload["choiceOptions"] == [
            {key: value for key, value in first_reload[0].items() if key != "lifecycle"}
        ]
        assert _zero_write_counts(connection, session.session_id) == (0, 0, 0)


def test_cross_root_result_cannot_reconcile_legacy_execution() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection)
        tampered = copy.deepcopy(prepared["resultPayload"])
        tampered["planningSelectionRootTurnId"] = "turn_other_root"
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (
                json.dumps(tampered, ensure_ascii=False),
                prepared["resultAssistantTurnId"],
            ),
        )
        connection.execute(
            "UPDATE agent_choice_executions SET status = 'failed_retryable', outcome_json = ? WHERE id = ?",
            (json.dumps({"reason": "no_material_progress"}), prepared["executionId"]),
        )
        connection.commit()

        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])
        assert evidence == {
            "passed": False,
            "reason": "simple_direction_execution_assistant_lineage_mismatch",
        }
        assert (
            SimpleDirectionExecutionEvidenceService(connection).reconcile_legacy_execution(prepared["executionId"])
            is False
        )


def test_formal_itinerary_write_prevents_zero_write_execution_reconciliation() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_execution(connection)
        plan_id = connection.execute(
            "SELECT active_plan_id FROM conversation_sessions WHERE id = ?",
            (prepared["sessionId"],),
        ).fetchone()["active_plan_id"]
        connection.execute(
            """INSERT INTO itinerary_versions (
                id, session_id, plan_id, version_number, source_type,
                source_turn_id, source_patch_id, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "version_unexpected_write",
                prepared["sessionId"],
                plan_id,
                1,
                "agent",
                prepared["resultAssistantTurnId"],
                None,
                "{}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])
        assert evidence == {
            "passed": False,
            "reason": "simple_direction_execution_formal_write_present",
        }


def test_legacy_root_reconstructs_binding_only_when_frozen_evidence_epoch_matches() -> None:
    clear_database()
    with open_db() as connection:
        root, request_fingerprint = _real_qualification_root(connection)
        row = connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (root["id"],),
        ).fetchone()
        summary = json.loads(row["summary_json"])
        frontier = summary["simpleDirectionFrontier"]
        # Historical frontiers predate both the binding and exploration
        # policy. Missing binding on a newly seeded root is corruption.
        frontier.pop("explorationOrdering", None)
        for entity in frontier["qualifiedEntityFrontier"]:
            entity.pop("qualificationBinding", None)
        frontier["frontierFingerprint"] = SimpleDirectionFrontierService._frontier_fingerprint(frontier)
        frozen_legacy_frontier = copy.deepcopy(frontier)
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), root["id"]),
        )
        connection.commit()

        attempt = SimpleOpenDirectionService(connection).claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_legacy_binding",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_campus"}],
            request_contract_fingerprint=request_fingerprint,
        )
        assignment = attempt["campusAssignments"][0]
        assert (
            EntityQualificationEvidenceService.validate_binding(
                assignment["qualificationBinding"],
                expected_planning_root_id="turn_binding_root",
                expected_request_contract_fingerprint=request_fingerprint,
                expected_entity_fingerprint=assignment["evidenceEntityFingerprint"],
                expected_canonical_name=assignment["canonicalName"],
            )
            == ""
        )
        restored = PlanPortfolioStore(connection).summary(portfolio_id=root["id"])
        assert restored["simpleDirectionFrontier"] == frozen_legacy_frontier
        assert assignment["canonicalName"] == frozen_legacy_frontier["qualifiedEntityFrontier"][0]["canonicalName"]
        assert _zero_write_counts(connection, root["sessionId"]) == (0, 0, 0)


@pytest.mark.parametrize("tamper", ["evidence_epoch", "cross_root_binding"])
@pytest.mark.parametrize("frontier_kind", ["legacy", "seeded"])
def test_stale_or_cross_root_qualification_binding_fails_closed(tamper: str, frontier_kind: str) -> None:
    clear_database()
    with open_db() as connection:
        root, request_fingerprint = _real_qualification_root(connection)
        row = connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (root["id"],),
        ).fetchone()
        summary = json.loads(row["summary_json"])
        frontier = summary["simpleDirectionFrontier"]
        if frontier_kind == "legacy":
            frontier.pop("explorationOrdering", None)
        if tamper == "evidence_epoch":
            frontier["qualificationEvidenceFingerprint"] = "0" * 64
            expected = "simple_direction_qualification_evidence_epoch_mismatch"
        else:
            entity = frontier["qualifiedEntityFrontier"][0]
            evidence = EntityQualificationEvidenceService.load(
                "moe_project_classification",
                "985",
            )
            evidence_entity = next(
                item
                for item in evidence["entities"]
                if item["canonicalName"] == entity["canonicalName"] and item["locality"] == entity["locality"]
            )
            entity["qualificationBinding"] = EntityQualificationEvidenceService.build_binding(
                evidence=evidence,
                entity=evidence_entity,
                planning_root_id="turn_other_root",
                request_contract_fingerprint=request_fingerprint,
            )
            expected = "simple_direction_qualification_binding_scope_mismatch"
        frontier["frontierFingerprint"] = SimpleDirectionFrontierService._frontier_fingerprint(frontier)
        frozen_summary = json.dumps(summary, ensure_ascii=False)
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (frozen_summary, root["id"]),
        )
        connection.commit()

        with pytest.raises(ValueError, match=expected):
            SimpleOpenDirectionService(connection).claim_frontier_assignment(
                portfolio_id=root["id"],
                execution_id=f"choice_exec_{tamper}",
                campus_slots=[{"dayNumber": 1, "slotId": "slot_campus"}],
                request_contract_fingerprint=request_fingerprint,
            )
        stored_summary = connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?", (root["id"],)
        ).fetchone()[0]
        assert stored_summary == frozen_summary
        assert _zero_write_counts(connection, root["sessionId"]) == (0, 0, 0)

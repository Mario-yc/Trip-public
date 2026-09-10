from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from backend.tests.unit.test_simple_direction_execution_evidence_service import (
    _persist_compatibility_result,
    _prepared_compatibility_execution,
)
from backend.tests.unit.test_simple_open_direction_workflow import (
    _compact_two_anchor_snapshot,
    _persist_direction_turn,
    _route_contract,
    _snapshot,
)
from src.api.schemas.agent import AgentMessageRequest, AgentMessageResponse
from src.models.poi import POI
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.creative_planning_models import PlanPortfolio
from src.services.guide_continuation_requirement_service import (
    GuideContinuationRequirementError,
    GuideContinuationRequirementService,
)
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.planning_run_service import PlanningRunService
from src.services.simple_open_direction_service import SimpleOpenDirectionService
from src.services.simple_direction_execution_evidence_service import (
    SimpleDirectionExecutionEvidenceService,
)
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor


def _guide_advice() -> dict:
    query_fingerprint = "a" * 64
    title = "北京北海公园游览攻略"
    summary = "北海公园适合傍晚散步。"
    source_url = "https://travel.example/guide/park"
    queried_at = "2026-09-03T00:00:00+00:00"
    source_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "title": title,
                "summary": summary,
                "url": source_url,
                "queriedAt": queried_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    evidence_fingerprint = GuideContinuationRequirementService.evidence_fingerprint(
        query_fingerprint=query_fingerprint,
        source_fingerprints=[source_fingerprint],
    )
    return {
        "status": "completed",
        "queryFingerprint": query_fingerprint,
        "evidenceFingerprint": evidence_fingerprint,
        "sourceRefs": [
            {
                "refId": "guide-ref-park",
                "sourceFingerprint": source_fingerprint,
                "title": title,
                "url": source_url,
                "queriedAt": queried_at,
            }
        ],
        "recommendations": [
            {
                "refId": "guide-ref-park",
                "sourceFingerprint": source_fingerprint,
                "title": title,
                "text": summary,
                "sourceUrl": source_url,
                "queriedAt": queried_at,
            }
        ],
        "placeHints": [
            {
                "schemaVersion": "guide-place-hint-v1",
                "mentionText": "北海公园",
                "intentType": "park",
                "sourceRefIds": ["guide-ref-park"],
                "sourceFingerprints": [source_fingerprint],
                "guideEvidenceFingerprint": evidence_fingerprint,
                "verificationStatus": "unresolved_amap_grounding",
            }
        ],
    }


def _guide_requirement(
    *,
    evidence_fingerprint: str,
    mention_text: str = "北海公园",
    intent_type: str = "park",
    source_ref_id: str = "guide-ref-park",
    source_fingerprint: str = "source-park",
) -> dict:
    requirement = {
        "schemaVersion": "guide-continuation-requirement-v1",
        "sourceAssistantTurnId": "turn-guide-result",
        "capabilitySourceAssistantTurnId": "turn-guide-result",
        "guideChoiceExecutionId": "choice-exec-guide-search",
        "planningSelectionRootTurnId": "turn-root-guide",
        "rootPortfolioId": "simple_direction_portfolio_guide",
        "requestContractFingerprint": "r" * 64,
        "expectedBaseVersionId": None,
        "queryFingerprint": "a" * 64,
        "evidenceFingerprint": evidence_fingerprint,
        "minimumNovelGroundedPlaceCount": 1,
        "placeHints": [
            {
                "schemaVersion": "guide-place-hint-v1",
                "mentionText": mention_text,
                "intentType": intent_type,
                "sourceRefIds": [source_ref_id],
                "sourceFingerprints": [source_fingerprint],
                "guideEvidenceFingerprint": evidence_fingerprint,
                "verificationStatus": "unresolved_amap_grounding",
            }
        ],
    }
    requirement["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(
        requirement
    )
    return requirement


def _persist_guide_carrier(connection, *, tamper_fingerprint: bool = False) -> tuple[str, dict]:
    session = ConversationService(connection).create_session("北京", "guide grounded continuation")
    session_id = session.session_id
    planning_root_id = "turn-root-guide"
    guide_turn_id = "turn-guide-result"
    portfolio_id = "simple_direction_portfolio_guide"
    request_fingerprint = "r" * 64
    advice = _guide_advice()
    if tamper_fingerprint:
        advice["evidenceFingerprint"] = "x" * 64
        advice["placeHints"][0]["guideEvidenceFingerprint"] = "x" * 64
    choice_id = "simple_direction_continue_guide"
    option = {
        "id": choice_id,
        "choiceId": choice_id,
        "action": "continue_plan_expansion",
        "kind": "simple_direction_more_plans",
        "scopeKind": "comparison",
        "label": "opaque",
        "sourceAssistantTurnId": guide_turn_id,
        "sourceUserTurnId": planning_root_id,
        "planningSelectionRootTurnId": planning_root_id,
        "rootPortfolioId": portfolio_id,
        "requestContractFingerprint": request_fingerprint,
        "expectedBaseVersionId": None,
        "workflowMode": "simple_direction_v1",
    }
    payload = {
        "mode": "travel_guide_advice",
        "guideAdvice": advice,
        "choiceOptions": [option],
    }
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """INSERT INTO conversation_turns (id, session_id, role, content, turn_index, status, created_at, updated_at)
        VALUES (?, ?, 'user', '北京高校公园一日游', 0, 'active', ?, ?)""",
        (planning_root_id, session_id, now, now),
    )
    connection.execute(
        """INSERT INTO conversation_turns (
            id, session_id, role, content, turn_index, status,
            agent_response_json, created_at, updated_at
        ) VALUES (?, ?, 'assistant', 'guide', 1, 'active', ?, ?, ?)""",
        (guide_turn_id, session_id, json.dumps(payload, ensure_ascii=False), now, now),
    )
    PlanPortfolioStore(connection).create(
        PlanPortfolio(
            portfolioId=portfolio_id,
            sessionId=session_id,
            sourceUserTurnId=planning_root_id,
            sourceAssistantTurnId=guide_turn_id,
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=request_fingerprint,
            status="awaiting_selection",
        ),
        [],
    )
    connection.execute(
        """INSERT INTO agent_choice_executions (
            id, session_id, source_turn_id, source_user_turn_id, choice_id, action,
            status, execution_turn_id, request_turn_id, attempt, outcome_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'search_travel_guide_advice', 'succeeded', ?, ?, 1, ?, ?, ?)""",
        (
            "choice-exec-guide-search",
            session_id,
            "turn-before-guide",
            planning_root_id,
            "guide-search-choice",
            guide_turn_id,
            "turn-guide-request",
            json.dumps(payload, ensure_ascii=False),
            now,
            now,
        ),
    )
    connection.commit()
    return session_id, {
        "sourceAssistantTurnId": guide_turn_id,
        "choiceId": choice_id,
        "action": "continue_plan_expansion",
        "option": option,
    }


def test_guide_continuation_requirement_rebinds_exact_persisted_lineage() -> None:
    clear_database()
    with open_db() as connection:
        session_id, selected = _persist_guide_carrier(connection)

        requirement = GuideContinuationRequirementService(connection).build(
            session_id=session_id,
            selected_choice=selected,
            active_version_id=None,
        )

    assert requirement["schemaVersion"] == "guide-continuation-requirement-v1"
    assert requirement["sourceAssistantTurnId"] == "turn-guide-result"
    assert requirement["guideChoiceExecutionId"] == "choice-exec-guide-search"
    assert requirement["minimumNovelGroundedPlaceCount"] == 1
    assert requirement["identityPolicy"] == "guide-poi-identity-v1"
    assert requirement["requirementFingerprint"] != GuideContinuationRequirementService.requirement_fingerprint(
        {key: value for key, value in requirement.items() if key != "identityPolicy"}
    )
    assert [item["mentionText"] for item in requirement["placeHints"]] == ["北海公园"]


@pytest.mark.parametrize("policy", [None, "guide-poi-identity-v1"])
def test_claimed_identity_policy_is_revalidated_without_upgrading_or_writing_overlay(policy) -> None:
    from src.services.conversation_operation_identity import ConversationOperationIdentity

    clear_database()
    with open_db() as connection:
        session_id, selected = _persist_guide_carrier(connection)
        service = GuideContinuationRequirementService(connection)
        stored = service.validate_original_evidence(
            session_id=session_id, option=selected["option"],
            capability_source_turn_id=selected["sourceAssistantTurnId"], active_version_id=None,
        )
        if policy is not None:
            stored["identityPolicy"] = policy
        stored["requirementFingerprint"] = service.requirement_fingerprint(stored)
        identity = ConversationOperationIdentity(connection).resolve(
            session_id, selected["sourceAssistantTurnId"], selected["choiceId"],
            guide_evidence=stored["evidenceFingerprint"],
        )
        journal = json.dumps({"guideSourceBindingRequirement": stored, "guideContinuationRequirement": stored})
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO agent_choice_executions (id, session_id, source_turn_id, source_user_turn_id, choice_id, "
            "action, status, continuation_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("claimed-identity-policy", session_id, identity.source_turn_id, "turn-root-guide", identity.choice_id,
             "continue_plan_expansion", "executing", journal, now, now),
        )
        connection.commit()
        changes = connection.total_changes
        rebuilt = service.build(session_id=session_id, selected_choice=selected, active_version_id=None)
        assert rebuilt == stored
        assert connection.total_changes == changes
        assert connection.execute("SELECT continuation_json FROM agent_choice_executions WHERE id=?",
                                  ("claimed-identity-policy",)).fetchone()[0] == journal


def test_guide_continuation_requirement_rejects_tampered_evidence_fingerprint() -> None:
    clear_database()
    with open_db() as connection:
        session_id, selected = _persist_guide_carrier(connection, tamper_fingerprint=True)

        with pytest.raises(GuideContinuationRequirementError, match="guide_evidence_lineage_invalid"):
            GuideContinuationRequirementService(connection).build(
                session_id=session_id,
                selected_choice=selected,
                active_version_id=None,
            )


def test_guide_grounding_attaches_provenance_only_to_matching_real_amap_candidate() -> None:
    advice = _guide_advice()
    hint = advice["placeHints"][0]
    selected = POI(
        id="poi-guide-park",
        amap_id="B0GUIDEPARK",
        name="北海公园",
        city="北京",
        category="park",
        latitude=39.925,
        longitude=116.389,
        source="amap-place-search",
        confidence=0.94,
    )

    constraints = SimpleOpenItineraryExecutor._guide_evidence_schedule_constraints(
        hint,
        selected=selected,
        day_number=1,
        planning_slot_id="slot-park-1",
        primary_result_count=3,
        guide_match_count=1,
        semantic_rejection=False,
        duplicate_rejection=False,
    )
    mismatched_selected = copy.deepcopy(selected)
    mismatched_selected.name = "景山公园"
    mismatched = SimpleOpenItineraryExecutor._guide_evidence_schedule_constraints(
        hint,
        selected=mismatched_selected,
        day_number=1,
        planning_slot_id="slot-park-1",
        primary_result_count=3,
        guide_match_count=0,
        semantic_rejection=False,
        duplicate_rejection=False,
    )
    ambiguous = SimpleOpenItineraryExecutor._guide_evidence_schedule_constraints(
        hint,
        selected=selected,
        day_number=1,
        planning_slot_id="slot-park-1",
        primary_result_count=3,
        guide_match_count=2,
        semantic_rejection=False,
        duplicate_rejection=False,
    )

    assert constraints["guideEvidence"]["amapPoiId"] == "B0GUIDEPARK"
    assert constraints["guideEvidenceAttempt"]["status"] == "grounded"
    assert "guideEvidence" not in mismatched
    assert mismatched["guideEvidenceAttempt"]["reasonCode"] == "no_amap_match"
    assert "guideEvidence" not in ambiguous
    assert ambiguous["guideEvidenceAttempt"]["reasonCode"] == "ambiguous_amap_match"


def test_proposal_verifier_requires_one_route_verified_guide_place() -> None:
    snapshot = _compact_two_anchor_snapshot()
    contract = snapshot["routeDecisionContract"]
    expected = [{"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"}]
    pair = {
        **expected[0],
        "transportMode": "transit",
        "durationSeconds": 1800,
        "distanceMeters": 4000,
        "provider": "amap-webservice",
        "queriedAt": "2026-09-03T00:00:00+00:00",
    }
    pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(pair)
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": expected,
        "verifiedPairs": [pair],
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
    }
    advice = _guide_advice()
    evidence_fingerprint = advice["evidenceFingerprint"]
    snapshot["guideContinuationRequirement"] = _guide_requirement(
        evidence_fingerprint=evidence_fingerprint,
        mention_text="清华大学",
        intent_type="campus_visit",
        source_ref_id="guide-ref-campus",
        source_fingerprint="source-campus",
    )
    guide_segment = snapshot["days"][0]["segments"][0]
    guide_segment["semanticMetadata"]["scheduleConstraints"] = {
        "guideEvidence": {
            "schemaVersion": "guide-place-evidence-v1",
            "mentionText": "清华大学",
            "intentType": "campus_visit",
            "sourceRefIds": ["guide-ref-campus"],
            "sourceFingerprints": ["source-campus"],
            "guideEvidenceFingerprint": evidence_fingerprint,
            "verificationStatus": "verified_amap_grounding",
            "amapPoiId": "B000A6EA36",
            "planningSlotId": "slot_B000A",
            "dayNumber": 1,
        }
    }

    verifier = SimpleOpenDirectionService._proposal_verifier(snapshot)
    snapshot["days"][0]["segments"][0]["semanticMetadata"]["scheduleConstraints"] = {}
    rejected = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert verifier["guideEvidenceUsage"]["status"] == "satisfied"
    assert verifier["guideEvidenceUsage"]["usedPlaces"][0]["routeVerified"] is True
    assert verifier["confirmationPassed"] is True
    assert rejected["guideEvidenceUsage"]["status"] == "unsatisfied"
    assert "guide_grounded_requirement_unsatisfied" in rejected["hardFailures"]


def test_unsatisfied_guide_requirement_is_zero_proposal_write() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "guide zero proposal")
        snapshot = _snapshot("guide-zero", "攻略候选", "B000A")
        snapshot["guideContinuationRequirement"] = _guide_requirement(evidence_fingerprint="e" * 64)
        request_contract = {"routeDecisionContract": _route_contract()}

        result = SimpleOpenDirectionService(connection).offer_direction(
            session_id=session.session_id,
            planning_root_id="turn-guide-root",
            source_user_turn_id="turn-guide-user",
            source_assistant_turn_id="turn-guide-assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=snapshot,
            request_contract=request_contract,
        )
        proposal_count = connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0]

    assert result["reasonCode"] == "guide_grounded_requirement_unsatisfied"
    assert result["proposalDelta"] == 0
    assert result["guideEvidenceUsage"]["status"] == "unsatisfied"
    assert proposal_count == 0


def test_misrouted_guide_clarification_reissues_one_fresh_bound_capability() -> None:
    clear_database()
    with open_db() as connection:
        conversation = ConversationService(connection)
        session = conversation.create_session("北京", "guide recovery")
        agent = AgentService(connection)
        agent.initial_planning_mode = "simple_open_v1"
        root_turn_id = agent._insert_turn(session.session_id, "user", "规划北京两日行程", "active")
        direction = SimpleOpenDirectionService(connection)
        direction_turn_id, _proposal_id = _persist_direction_turn(
            connection=connection,
            service=agent,
            direction_service=direction,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="既有方向",
            poi_id="B000A",
        )
        direction_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (direction_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        guide_choice = next(
            item
            for item in direction_payload["choiceOptions"]
            if item.get("action") == "search_travel_guide_advice"
        )
        guide_turn_id = agent._insert_turn(
            session.session_id,
            "assistant",
            "攻略已生成",
            "active",
            agent_response_json={},
        )
        rotated = direction.rotate_capability_carrier(
            session_id=session.session_id,
            portfolio_id=str(guide_choice["rootPortfolioId"]),
            expected_source_assistant_turn_id=direction_turn_id,
            next_source_assistant_turn_id=guide_turn_id,
            request_contract_fingerprint=str(guide_choice["requestContractFingerprint"]),
            retry_guide=True,
        )
        advice = _guide_advice()
        guide_payload = {
            "mode": "travel_guide_advice",
            "guideAdvice": advice,
            **rotated,
            "proposalDelta": 0,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        }
        now = datetime.now(timezone.utc).isoformat()
        guide_execution_id = "choice-exec-guide-recovery"
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(guide_payload, ensure_ascii=False), guide_turn_id),
        )
        connection.execute(
            """INSERT INTO agent_choice_executions (
                id, session_id, source_turn_id, source_user_turn_id, choice_id, action,
                status, execution_turn_id, request_turn_id, attempt, outcome_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'search_travel_guide_advice', 'succeeded', ?, ?, 1, ?, ?, ?)""",
            (
                guide_execution_id,
                session.session_id,
                direction_turn_id,
                root_turn_id,
                str(guide_choice["id"]),
                guide_turn_id,
                "turn-guide-request",
                json.dumps(guide_payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        guide_continuation = next(
            item
            for item in guide_payload["choiceOptions"]
            if item.get("action") == "continue_plan_expansion"
        )
        failed_request = {
            "conversationIntent": {
                "reasonCode": "intent_confidence_below_threshold",
                "requiresClarification": True,
            },
            "conversationCapability": {
                "status": "not_required",
                "reasonCode": "intent_requires_clarification",
            },
            "agentDecisionState": {"controllerFullCalled": False},
            "continuationContext": {"sourceAssistantTurnId": guide_turn_id},
            "planningSelectionRootTurnId": root_turn_id,
            "rootPortfolioId": str(guide_continuation["rootPortfolioId"]),
            "_simpleDirectionRequestContractFingerprint": str(
                guide_continuation["requestContractFingerprint"]
            ),
        }
        failed_user_turn_id = agent._insert_turn(
            session.session_id,
            "user",
            "参考攻略建议的地点，生成新的方案",
            "active",
            agent_request_json=failed_request,
        )
        failed_turn_id = agent._insert_turn(
            session.session_id,
            "assistant",
            "尚未调用规划控制器",
            "active",
            agent_request_json=failed_request,
            agent_response_json={
                "mode": "clarification",
                "reasonCode": "intent_confidence_below_threshold",
                "choiceOptions": [
                    {
                        "id": "retry-old-routing",
                        "action": "retry_model_planning",
                        "kind": "safe_fallback_action",
                    }
                ],
                "proposalDelta": 0,
                "versionDelta": 0,
                "patchDelta": 0,
                "routeWriteDelta": 0,
            },
        )
        planning_run = PlanningRunService(connection).create_run(
            "agent_clarification",
            user_input="参考攻略建议的地点，生成新的方案",
        )
        finalized = agent._finalize_generated_response(
            session.session_id,
            AgentMessageResponse(
                userTurn=agent._turn_response(failed_user_turn_id),
                assistantTurn=agent._turn_response(failed_turn_id),
                planningRun=planning_run,
            ),
        )
        proposal_count_before = connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0]

        recovered = any(
            item.get("action") == "continue_plan_expansion"
            for item in finalized.assistant_turn.choice_options
        )
        recovered_again = direction.reconcile_legacy_guide_capability_carrier(session_id=session.session_id)
        failed_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (failed_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        fresh = [
            item
            for item in failed_payload["choiceOptions"]
            if item.get("action") == "continue_plan_expansion"
        ]
        retry = [
            item
            for item in failed_payload["choiceOptions"]
            if item.get("action") == "retry_model_planning"
        ]
        proposal_choices = [
            item
            for item in failed_payload["choiceOptions"]
            if item.get("action") == "select_plan_proposal"
        ]
        selected = {
            "sourceAssistantTurnId": failed_turn_id,
            "choiceId": fresh[0]["id"],
            "action": "continue_plan_expansion",
            "option": copy.deepcopy(fresh[0]),
        }
        requirement = GuideContinuationRequirementService(connection).build(
            session_id=session.session_id,
            selected_choice=selected,
            active_version_id=None,
        )
        agent.conversation_intent_router.routing_mode = "active-all"
        routed_payload, routed, routed_capability = agent._route_conversation_turn(
            session=connection.execute(
                "SELECT * FROM conversation_sessions WHERE id = ?",
                (session.session_id,),
            ).fetchone(),
            content="参考攻略里面的景点，按照我的需求给出个方案",
            payload=AgentMessageRequest(
                content="参考攻略里面的景点，按照我的需求给出个方案",
                context={},
            ),
        )
        proposal_count_after = connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0]
        write_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("itinerary_versions", "itinerary_patches", "timeline_mutation_transactions")
        )

        repeated_request = copy.deepcopy(failed_request)
        repeated_request["continuationContext"] = {"sourceAssistantTurnId": failed_turn_id}
        repeated_user_turn_id = agent._insert_turn(
            session.session_id,
            "user",
            "参考攻略里面的景点，按照我的需求给出个方案",
            "active",
            agent_request_json=repeated_request,
        )
        repeated_turn_id = agent._insert_turn(
            session.session_id,
            "assistant",
            "尚未调用规划控制器",
            "active",
            agent_request_json=repeated_request,
            agent_response_json={
                "mode": "clarification",
                "choiceOptions": [
                    {
                        "id": "retry-repeated-routing",
                        "action": "retry_model_planning",
                        "kind": "safe_fallback_action",
                    }
                ],
                "proposalDelta": 0,
                "versionDelta": 0,
                "patchDelta": 0,
                "routeWriteDelta": 0,
            },
        )
        repeated_run = PlanningRunService(connection).create_run(
            "agent_clarification",
            user_input="参考攻略里面的景点，按照我的需求给出个方案",
        )
        connection.execute(
            "UPDATE conversation_turns SET planning_run_id = ? WHERE id IN (?, ?)",
            (repeated_run.id, repeated_user_turn_id, repeated_turn_id),
        )
        connection.commit()
        recovered_repeated = direction.reconcile_legacy_guide_capability_carrier(session_id=session.session_id)
        repeated_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (repeated_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        repeated_proposal_choices = [
            item for item in repeated_payload["choiceOptions"] if item.get("action") == "select_plan_proposal"
        ]
        repeated_continuations = [
            item for item in repeated_payload["choiceOptions"] if item.get("action") == "continue_plan_expansion"
        ]

    assert recovered is True
    assert recovered_again is False
    assert len(fresh) == 1
    assert len(retry) == 1
    assert proposal_choices
    assert all(item["sourceAssistantTurnId"] == failed_turn_id for item in proposal_choices)
    assert any(
        item.get("action") == "select_plan_proposal"
        for item in finalized.assistant_turn.choice_options
    )
    assert fresh[0]["id"] != guide_continuation["id"]
    assert fresh[0]["operationOrigin"]["sourceAssistantTurnId"] == guide_turn_id
    assert fresh[0]["operationOrigin"]["choiceId"] == guide_continuation["id"]
    assert fresh[0]["sourceAssistantTurnId"] == failed_turn_id
    assert fresh[0]["guideEvidenceSourceAssistantTurnId"] == guide_turn_id
    assert fresh[0]["guideChoiceExecutionId"] == guide_execution_id
    assert requirement["sourceAssistantTurnId"] == guide_turn_id
    assert requirement["capabilitySourceAssistantTurnId"] == failed_turn_id
    assert routed.requires_clarification is False
    assert routed_capability.status == "unique"
    assert routed_payload.context.selected_agent_choice is not None
    assert routed_payload.context.selected_agent_choice.source_assistant_turn_id == failed_turn_id
    assert proposal_count_after == proposal_count_before
    assert write_counts == (0, 0, 0)
    assert recovered_repeated is True
    assert repeated_proposal_choices
    assert len(repeated_continuations) == 1
    assert all(item["sourceAssistantTurnId"] == repeated_turn_id for item in repeated_proposal_choices)
    assert repeated_continuations[0]["sourceAssistantTurnId"] == repeated_turn_id
    assert repeated_continuations[0]["operationOrigin"] == fresh[0]["operationOrigin"]
    assert repeated_payload["guideContinuationRecovery"]["guideEvidenceSourceAssistantTurnId"] == guide_turn_id


def test_choice_completion_proves_claimed_guide_requirement_through_persisted_proposal() -> None:
    clear_database()
    with open_db() as connection:
        prepared = _prepared_compatibility_execution(connection)
        snapshot = _compact_two_anchor_snapshot()
        compact_request_contract = {
            "routeDecisionContract": copy.deepcopy(snapshot["routeDecisionContract"]),
        }
        portfolio_row = connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (prepared["root"]["id"],),
        ).fetchone()
        portfolio_summary = json.loads(portfolio_row["summary_json"])
        portfolio_summary["requestIntentContract"] = copy.deepcopy(compact_request_contract)
        portfolio_summary["requestIntentContractMaterialFingerprint"] = SimpleOpenDirectionService._fingerprint(
            compact_request_contract
        )
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (json.dumps(portfolio_summary, ensure_ascii=False), prepared["root"]["id"]),
        )
        prepared["requestContract"] = compact_request_contract
        expected = [{"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"}]
        pair = {
            **expected[0],
            "transportMode": "transit",
            "durationSeconds": 1800,
            "distanceMeters": 4000,
            "provider": "amap-webservice",
            "queriedAt": "2026-09-03T00:00:00+00:00",
        }
        pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(pair)
        snapshot["simpleOpenRouteAssignment"] = {
            "routeContractFingerprint": snapshot["routeDecisionContract"]["fingerprint"],
            "expectedPairs": expected,
            "verifiedPairs": [pair],
            "routeCoverageComplete": True,
            "adjacentLegCompliance": "verified",
            "topologyCompliance": "verified",
            "providerBaselineCompared": False,
            "detourCompliance": "not_evaluated",
        }
        advice = _guide_advice()
        evidence_fingerprint = advice["evidenceFingerprint"]
        requirement = _guide_requirement(
            evidence_fingerprint=evidence_fingerprint,
            mention_text="清华大学",
            intent_type="campus_visit",
            source_ref_id="guide-ref-campus",
            source_fingerprint="source-campus",
        )
        requirement.update(
            {
                "capabilitySourceAssistantTurnId": prepared["sourceAssistantTurnId"],
                "planningSelectionRootTurnId": prepared["planningRootId"],
                "rootPortfolioId": prepared["root"]["id"],
                "requestContractFingerprint": prepared["requestFingerprint"],
            }
        )
        requirement["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(
            requirement
        )
        snapshot["guideContinuationRequirement"] = copy.deepcopy(requirement)
        guide_segment = snapshot["days"][0]["segments"][0]
        guide_segment["semanticMetadata"]["scheduleConstraints"] = {
            "guideEvidence": {
                "schemaVersion": "guide-place-evidence-v1",
                "mentionText": "清华大学",
                "intentType": "campus_visit",
                "sourceRefIds": ["guide-ref-campus"],
                "sourceFingerprints": ["source-campus"],
                "guideEvidenceFingerprint": evidence_fingerprint,
                "verificationStatus": "verified_amap_grounding",
                "amapPoiId": "B000A6EA36",
                "planningSlotId": "slot_B000A",
                "dayNumber": 1,
            }
        }
        connection.execute(
            "UPDATE agent_choice_executions SET continuation_json = ? WHERE id = ?",
            (
                json.dumps({"guideContinuationRequirement": requirement}, ensure_ascii=False),
                prepared["executionId"],
            ),
        )
        pre_verifier = SimpleOpenDirectionService._proposal_verifier(snapshot)
        assert pre_verifier["guideEvidenceUsage"]["status"] == "satisfied", (
            pre_verifier["guideEvidenceUsage"],
            pre_verifier.get("routeStatus"),
            pre_verifier.get("hardFailures"),
        )
        payload = prepared["direction"].offer_direction(
            session_id=prepared["session"].session_id,
            planning_root_id=prepared["planningRootId"],
            source_user_turn_id=prepared["requestTurnId"],
            source_assistant_turn_id=prepared["resultAssistantTurnId"],
            expected_base_version_id=None,
            source_observation_fingerprint="g" * 64,
            request_contract_fingerprint=prepared["requestFingerprint"],
            snapshot=snapshot,
            request_contract=prepared["requestContract"],
            frontier_execution_id=prepared["executionId"],
        )
        assert payload["proposalDelta"] == 1, payload
        _persist_compatibility_result(connection, prepared, payload)

        evidence = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])
        proposal = connection.execute(
            """SELECT generation_lineage_json, evidence_json
            FROM agent_plan_proposals WHERE portfolio_id = ? ORDER BY created_at DESC LIMIT 1""",
            (prepared["root"]["id"],),
        ).fetchone()
        lineage = json.loads(proposal["generation_lineage_json"])
        proposal_evidence = json.loads(proposal["evidence_json"])
        lineage["guideContinuationRequirementFingerprint"] = "f" * 64
        connection.execute(
            "UPDATE agent_plan_proposals SET generation_lineage_json = ? WHERE portfolio_id = ?",
            (json.dumps(lineage, ensure_ascii=False), prepared["root"]["id"]),
        )
        connection.commit()
        tampered = SimpleDirectionExecutionEvidenceService(connection).verify(prepared["executionId"])

    assert payload["proposalDelta"] == 1
    assert payload["guideEvidenceUsage"]["status"] == "satisfied"
    assert evidence["passed"] is True
    assert evidence["guideContinuationRequirementFingerprint"] == requirement["requirementFingerprint"]
    assert evidence["guideEvidenceUsage"] == payload["guideEvidenceUsage"]
    assert proposal_evidence["guideEvidenceUsage"] == payload["guideEvidenceUsage"]
    assert tampered == {
        "passed": False,
        "reason": "simple_direction_execution_proposal_lineage_missing",
    }

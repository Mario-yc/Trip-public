import hashlib
import json
import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from backend.tests.unit.test_simple_open_direction_workflow import _persist_direction_turn
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.simple_open_direction_service import SimpleOpenDirectionService
from src.services.conversation_capability_carrier import ConversationCapabilityCarrier
from src.services.conversation_operation_identity import ConversationOperationIdentity


def _persist_no_hint_guide_frontier(db):
    from backend.tests.unit.test_guide_grounded_continuation import _guide_advice
    from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
    from src.services.travel_guide_advice_service import TravelGuideAdviceService

    session_id = ConversationService(db).create_session("北京", "guide without summary places").session_id
    service = AgentService(db)
    root = service._insert_turn(session_id, "user", "北京高校一日游", "active")
    source, _ = _persist_direction_turn(
        connection=db, service=service, direction_service=SimpleOpenDirectionService(db),
        session_id=session_id, root_turn_id=root, source_user_turn_id=root, title="高校", poi_id="B000A",
    )
    payload = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (source,)).fetchone()[0])
    advice = _guide_advice()
    title = "出行准备与参观注意事项"
    summary = "提前查看开放通知和预约要求，具体地点与安排请阅读原文。"
    source_ref = advice["sourceRefs"][0]
    fingerprint = hashlib.sha256(json.dumps({
        "title": title, "summary": summary, "url": source_ref["url"], "queriedAt": source_ref["queriedAt"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    source_ref.update(title=title, sourceFingerprint=fingerprint)
    advice["recommendations"][0].update(title=title, text=summary, sourceFingerprint=fingerprint)
    advice["evidenceFingerprint"] = GuideContinuationRequirementService.evidence_fingerprint(
        query_fingerprint=advice["queryFingerprint"], source_fingerprints=[fingerprint],
    )
    advice["placeHints"] = []
    payload.update(mode="travel_guide_advice", guideAdvice=advice)
    db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False), source))
    search_choice = next(item for item in payload["choiceOptions"] if item["action"] == "search_travel_guide_advice")
    outcome = {
        "mode": "travel_guide_advice", "guideAdvice": advice,
        "plannerCalled": False, "proposalDelta": 0, "versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0,
    }
    db.execute("""INSERT INTO agent_choice_executions (
        id, session_id, source_turn_id, source_user_turn_id, choice_id, action,
        status, execution_turn_id, request_turn_id, attempt, outcome_json, created_at, updated_at
    ) SELECT ?, session_id, id, ?, ?, 'search_travel_guide_advice', 'succeeded', id, ?, 1, ?, created_at, updated_at
        FROM conversation_turns WHERE id = ?""",
        ("empty-hint-guide-search", root, search_choice["id"], root, json.dumps(outcome, ensure_ascii=False), source))
    db.commit()
    option = next(item for item in payload["choiceOptions"] if item["action"] == "continue_plan_expansion")
    selected = {"sourceAssistantTurnId": source, "choiceId": option["id"], "action": option["action"], "option": option}
    # Validate the actual source fingerprint, success execution, root and base;
    # no evidence verifier or permission boundary is substituted in this fixture.
    assert TravelGuideAdviceService._guide_payload_evidence(advice) is not None
    requirement = GuideContinuationRequirementService(db).build(
        session_id=session_id, selected_choice=selected, active_version_id=None, require_place_hints=False,
    )
    assert requirement["placeHints"] == []
    return service, session_id, source, option, requirement


def _forbid_carrier_external_calls(monkeypatch, service):
    import httpx
    from src.services.map_poi_service import MapPoiService
    from src.services.route_service import RouteService

    calls = {"model": 0, "map": 0, "route": 0, "http": 0}

    def forbidden(kind):
        def call(*args, **kwargs):
            calls[kind] += 1
            raise AssertionError(f"Readonly carry must not call {kind}")
        return call

    monkeypatch.setattr(type(service.provider), "generate", forbidden("model"))
    if hasattr(type(service.provider), "run_tool_loop"):
        monkeypatch.setattr(type(service.provider), "run_tool_loop", forbidden("model"))
    monkeypatch.setattr(service.autonomy_controller, "decide", forbidden("model"))
    monkeypatch.setattr(MapPoiService, "search", forbidden("map"))
    monkeypatch.setattr(MapPoiService, "search_nearby", forbidden("map"))
    monkeypatch.setattr(RouteService, "build_routes", forbidden("route"))
    monkeypatch.setattr(httpx.Client, "send", forbidden("http"))
    return calls


@pytest.mark.parametrize("reply_status", ["active", "failed"])
def test_two_readonly_carriers_preserve_guide_lineage_without_summary_place_hints(monkeypatch, reply_status):
    from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService

    clear_database()
    with open_db() as db:
        service, session_id, source, choice, requirement = _persist_no_hint_guide_frontier(db)
        calls = _forbid_carrier_external_calls(monkeypatch, service)
        carrier = ConversationCapabilityCarrier(db)
        identities = ConversationOperationIdentity(db)
        origin = identities.origin(session_id, source, choice["id"])
        operation = identities.resolve(session_id, source, choice["id"], guide_evidence=requirement["evidenceFingerprint"])
        guide_fields = {
            "guideEvidenceSourceAssistantTurnId": source,
            "guideChoiceExecutionId": requirement["guideChoiceExecutionId"],
            "guideQueryFingerprint": requirement["queryFingerprint"],
            "guideEvidenceFingerprint": requirement["evidenceFingerprint"],
        }
        before = carrier.counts(session_id)
        seen_choices = {choice["id"]}
        for _ in range(2):
            capture = carrier.capture(session_id, source)
            next_id = service._insert_turn(session_id, "assistant", "这次只回答你的问题，不开始规划。", reply_status,
                agent_response_json={"mode": "answer"})
            db.commit()
            assert carrier.carry(session_id, next_id, capture, nonexecuting=True)
            payload = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (next_id,)).fetchone()[0])
            fresh = next(item for item in payload["choiceOptions"] if item["action"] == "continue_plan_expansion")
            assert {field: fresh.get(field) for field in guide_fields} == guide_fields
            assert fresh["id"] == fresh["choiceId"] and fresh["id"] not in seen_choices
            assert fresh["operationOrigin"] == origin
            assert identities.resolve(session_id, next_id, fresh["id"], guide_evidence=requirement["evidenceFingerprint"]) == operation
            rebound = GuideContinuationRequirementService(db).build(
                session_id=session_id, selected_choice={"sourceAssistantTurnId": next_id, "choiceId": fresh["id"], "option": fresh},
                active_version_id=None, require_place_hints=False,
            )
            assert rebound["placeHints"] == []
            assert rebound["sourceAssistantTurnId"] == requirement["sourceAssistantTurnId"]
            assert rebound["guideChoiceExecutionId"] == requirement["guideChoiceExecutionId"]
            assert rebound["evidenceFingerprint"] == requirement["evidenceFingerprint"]
            assert carrier.counts(session_id) == before
            assert calls == {"model": 0, "map": 0, "route": 0, "http": 0}
            seen_choices.add(fresh["id"])
            source, choice = next_id, fresh
        assert len(seen_choices) == 3


@pytest.mark.parametrize("tamper", ["evidence_fingerprint", "source_fingerprint"])
def test_readonly_carriers_do_not_propagate_tampered_empty_hint_guide_evidence(monkeypatch, tamper):
    clear_database()
    with open_db() as db:
        service, session_id, source, choice, _ = _persist_no_hint_guide_frontier(db)
        calls = _forbid_carrier_external_calls(monkeypatch, service)
        payload = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (source,)).fetchone()[0])
        if tamper == "evidence_fingerprint":
            payload["guideAdvice"]["evidenceFingerprint"] = "f" * 64
        else:
            payload["guideAdvice"]["sourceRefs"][0]["sourceFingerprint"] = "f" * 64
        db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?", (json.dumps(payload), source))
        db.commit()
        carrier = ConversationCapabilityCarrier(db)
        identities = ConversationOperationIdentity(db)
        ordinary_operation = identities.resolve(session_id, source, choice["id"])
        before = carrier.counts(session_id)
        for _ in range(2):
            capture = carrier.capture(session_id, source)
            next_id = service._insert_turn(session_id, "assistant", "只读回答", "active", agent_response_json={"mode": "answer"})
            db.commit()
            assert carrier.carry(session_id, next_id, capture, nonexecuting=True)
            latest = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (next_id,)).fetchone()[0])
            fresh = next(item for item in latest["choiceOptions"] if item["action"] == "continue_plan_expansion")
            assert not any(field in fresh for field in (
                "guideEvidenceSourceAssistantTurnId", "guideChoiceExecutionId", "guideQueryFingerprint", "guideEvidenceFingerprint",
            ))
            assert fresh["id"] != choice["id"]
            assert identities.resolve(session_id, next_id, fresh["id"]) == ordinary_operation
            assert carrier.counts(session_id) == before
            assert calls == {"model": 0, "map": 0, "route": 0, "http": 0}
            source, choice = next_id, fresh


def test_retained_guide_snapshot_does_not_turn_semantic_failure_into_missing_trip_fields():
    from backend.tests.unit.test_guide_grounded_continuation import _persist_guide_carrier
    from src.services.conversation_intent_router import ConversationCapabilityResolver
    from src.services.conversation_action_catalog import ConversationActionCatalog

    clear_database()
    with open_db() as db:
        session_id, selected = _persist_guide_carrier(db)
        service = AgentService(db)
        from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
        requirement = GuideContinuationRequirementService(db).build(session_id=session_id, selected_choice=selected, active_version_id=None)
        origin = ConversationOperationIdentity(db).origin(session_id, selected["sourceAssistantTurnId"], selected["choiceId"])
        failed = service._insert_turn(session_id, "assistant", "尚未绑定执行意图", "active", agent_response_json={
            "mode": "clarification", "terminalStatus": "needs_confirmation",
            "clarification": {"question": "请明确操作", "choiceIds": []},
        })
        # A deterministic post-carry fixture: all reads/lineage checks below use
        # the real resolver, not a substituted model snapshot or guide verifier.
        response = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (failed,)).fetchone()[0])
        response["choiceOptions"] = [{**selected["option"], "id": "fresh-continue", "choiceId": "fresh-continue",
            "sourceAssistantTurnId": failed, "operationOrigin": origin,
            "guideEvidenceSourceAssistantTurnId": requirement["sourceAssistantTurnId"],
            "guideChoiceExecutionId": requirement["guideChoiceExecutionId"], "guideQueryFingerprint": requirement["queryFingerprint"],
            "guideEvidenceFingerprint": requirement["evidenceFingerprint"]}]
        response["capabilityCarryForward"] = {"schemaVersion": "capability-carry-forward-v1",
            "sourceAssistantTurnId": selected["sourceAssistantTurnId"], "targetAssistantTurnId": failed,
            "status": "reissued", "businessExecutionStarted": False}
        db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?", (json.dumps(response), failed))
        db.execute("UPDATE agent_plan_portfolios SET source_assistant_turn_id = ? WHERE session_id = ?", (failed, session_id))
        db.commit()
        row = db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        snapshot = ConversationCapabilityResolver(db).routing_snapshot(row)
        assert snapshot.server_state["guideRequirement"]
        assert not snapshot.server_state["pendingClarification"]
        assert snapshot.model_projection["workflowPhase"] == "guide_advice"
        assert snapshot.model_projection["planningContext"]["hasFrozenTravelRequest"] is True
        assert snapshot.model_projection["guideContinuation"]["evidenceStatus"] == "validated"
        assert snapshot.model_projection["guideContinuation"]["sourceRelation"] == "validated_retained_guide"
        assert "continue_with_guide" in ConversationActionCatalog(snapshot).names
        assert "answer_clarification" not in ConversationActionCatalog(snapshot).names
        response = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (failed,)).fetchone()[0])
        response["clarificationCheckpoint"] = {"checkpointId": "business-question"}
        db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?", (json.dumps(response), failed))
        db.commit()
        assert ConversationCapabilityResolver(db).routing_snapshot(row).server_state["pendingClarification"] is True


@pytest.mark.parametrize("reply_status", ["active", "failed"])
def test_two_readonly_carriers_preserve_proposal_operation_and_have_fresh_choices(reply_status):
    clear_database()
    with open_db() as db:
        session = ConversationService(db).create_session("北京", "carrier")
        service = AgentService(db)
        root = service._insert_turn(session.session_id, "user", "北京高校一日游", "active")
        source, proposal = _persist_direction_turn(connection=db, service=service,
            direction_service=SimpleOpenDirectionService(db), session_id=session.session_id,
            root_turn_id=root, source_user_turn_id=root, title="高校", poi_id="B000A")
        carrier = ConversationCapabilityCarrier(db)
        identities = ConversationOperationIdentity(db)
        original = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (source,)).fetchone()[0])
        choice = next(item for item in original["choiceOptions"] if item.get("proposalId") == proposal)
        operation = identities.resolve(session.session_id, source, choice["id"])
        before = carrier.counts(session.session_id)
        for _ in range(2):
            capture = carrier.capture(session.session_id, source)
            next_id = service._insert_turn(session.session_id, "assistant", "只读回答", reply_status, agent_response_json={"mode": "answer"})
            db.commit()
            assert carrier.carry(session.session_id, next_id, capture, nonexecuting=True)
            assert carrier.counts(session.session_id) == before
            latest = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (next_id,)).fetchone()[0])
            fresh = next(item for item in latest["choiceOptions"] if item.get("proposalId") == proposal)
            assert fresh["id"] != choice["id"]
            assert identities.resolve(session.session_id, next_id, fresh["id"]) == operation
            from src.services.agent_context_builder_service import AgentContextBuilder
            bound = AgentContextBuilder(db)._resolve_selected_agent_choice(
                db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone(),
                {"sourceAssistantTurnId": next_id, "choiceId": fresh["id"]},
            )
            assert bound["choiceId"] == fresh["id"]
            assert not carrier.carry(session.session_id, next_id, capture, nonexecuting=True)
            source, choice = next_id, fresh


@pytest.mark.parametrize("proof_valid", [False, True])
def test_new_claim_only_carries_with_affirmative_unstarted_settlement(proof_valid):
    clear_database()
    with open_db() as db:
        session = ConversationService(db).create_session("北京", "carrier")
        service = AgentService(db)
        service.conversation_intent_router.routing_mode = "active-all"
        root = service._insert_turn(session.session_id, "user", "北京高校一日游", "active")
        source, proposal = _persist_direction_turn(connection=db, service=service,
            direction_service=SimpleOpenDirectionService(db), session_id=session.session_id,
            root_turn_id=root, source_user_turn_id=root, title="高校", poi_id="B000A")
        payload = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (source,)).fetchone()[0])
        choice = next(item for item in payload["choiceOptions"] if item.get("proposalId") == proposal)
        carrier = ConversationCapabilityCarrier(db)
        capture = carrier.capture(session.session_id, source)
        request = service._insert_turn(session.session_id, "user", "确认", "active")
        claim = service._claim_fallback_choice_execution(session.session_id, request,
            {"sourceAssistantTurnId": source, "choiceId": choice["id"], "action": choice["action"], "option": choice})
        proof = {"businessExecutionStarted": False, "boundedAttemptConsumed": False, "plannerCalled": False,
                 "proposalDelta": 0, "versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0} if proof_valid else None
        service._finish_fallback_choice_execution(claim["id"], "failed_retryable", outcome=proof)
        next_id = service._insert_turn(session.session_id, "assistant", "执行前失败", "failed", agent_response_json={"mode": "answer"})
        db.commit()
        assert carrier.carry(session.session_id, next_id, capture, nonexecuting=True) is proof_valid
        if proof_valid:
            result = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (next_id,)).fetchone()[0])
            fresh = next(item for item in result["choiceOptions"] if item.get("proposalId") == proposal)
            assert fresh["id"] != choice["id"]
            assert ConversationOperationIdentity(db).offered_execution(session.session_id, next_id, fresh["id"])["id"] == claim["id"]
            assert not carrier.carry(session.session_id, next_id, capture, nonexecuting=True)

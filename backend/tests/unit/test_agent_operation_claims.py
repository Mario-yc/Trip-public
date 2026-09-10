import json

import pytest
from fastapi import HTTPException

from backend.tests.unit.test_agent_service import clear_database, open_db
from backend.tests.unit.test_simple_open_direction_workflow import _persist_direction_turn
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.simple_open_direction_service import SimpleOpenDirectionService


@pytest.fixture
def operation(request):
    clear_database()
    with open_db() as db:
        session = ConversationService(db).create_session("北京", "claim")
        service = AgentService(db)
        service.conversation_intent_router.routing_mode = "active-all"
        root = service._insert_turn(session.session_id, "user", "北京高校一日游", "active")
        source, proposal = _persist_direction_turn(connection=db, service=service,
            direction_service=SimpleOpenDirectionService(db), session_id=session.session_id,
            root_turn_id=root, source_user_turn_id=root, title="高校", poi_id="B000A")
        response = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (source,)).fetchone()[0])
        option = next(item for item in response["choiceOptions"] if item.get("proposalId") == proposal)
        if getattr(request, "param", None) == "retry_model_planning":
            option.update(action="retry_model_planning", kind="portfolio_more_plans")
            db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?", (json.dumps(response), source))
            db.commit()
        choice = {"sourceAssistantTurnId": source, "choiceId": option["id"], "action": option["action"], "option": option}
        request = service._insert_turn(session.session_id, "user", "确认", "active")
        claim = service._claim_fallback_choice_execution(session.session_id, request, choice)
        yield db, service, session.session_id, choice, claim


def test_active_entry_never_reclaims_unknown_selection_from_expired_lease(operation):
    db, service, session, choice, claim = operation
    db.execute("UPDATE agent_choice_executions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (claim["id"],))
    db.commit()
    request = service._insert_turn(session, "user", "再次确认", "active")
    with pytest.raises(HTTPException) as error:
        service._claim_fallback_choice_execution(session, request, choice)
    assert error.value.detail["code"] == "agent_choice_execution_in_progress"
    assert db.execute("SELECT attempt FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone()[0] == 1


def test_active_entry_unknown_retryable_result_does_not_grant_another_attempt(operation):
    db, service, session, choice, claim = operation
    service._finish_fallback_choice_execution(claim["id"], "failed_retryable")
    request = service._insert_turn(session, "user", "再次确认", "active")
    with pytest.raises(HTTPException) as error:
        service._claim_fallback_choice_execution(session, request, choice)
    assert error.value.detail["code"] == "agent_choice_execution_state_unknown"
    assert db.execute("SELECT attempt FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["retry_model_planning"], indirect=True)
def test_active_entry_legacy_retry_also_requires_explicit_unstarted_proof(operation):
    test_active_entry_unknown_retryable_result_does_not_grant_another_attempt(operation)


@pytest.mark.parametrize("operation", ["retry_model_planning"], indirect=True)
def test_native_catalog_does_not_advertise_unknown_retryable_operation(operation):
    from src.services.conversation_intent_router import ConversationCapabilityResolver
    db, service, session, choice, claim = operation
    service._finish_fallback_choice_execution(claim["id"], "failed_retryable")
    resolver = ConversationCapabilityResolver(db)
    row = db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session,)).fetchone()
    candidate = {"sourceAssistantTurnId": choice["sourceAssistantTurnId"], "choiceId": choice["choiceId"], "option": choice["option"]}
    assert not resolver._catalog_candidate_current(row, candidate)


def test_proven_unstarted_resume_preserves_business_attempt_and_journal(operation):
    db, service, session, choice, claim = operation
    proof = {"businessExecutionStarted": False, "boundedAttemptConsumed": False, "plannerCalled": False,
             "proposalDelta": 0, "versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0}
    journal = json.loads(claim["continuation_json"])
    journal["persistedRequirement"] = {"immutable": True}
    db.execute("UPDATE agent_choice_executions SET continuation_json = ? WHERE id = ?", (json.dumps(journal), claim["id"]))
    service._finish_fallback_choice_execution(claim["id"], "failed_retryable", outcome=proof)
    request = service._insert_turn(session, "user", "再次确认", "active")
    resumed = service._claim_fallback_choice_execution(session, request, choice)
    assert resumed["id"] == claim["id"]
    assert resumed["attempt"] == 1
    assert json.loads(resumed["continuation_json"])["persistedRequirement"] == {"immutable": True}


@pytest.mark.parametrize("mutation", ["expired", "carrier", "root", "proposal"])
def test_catalog_excludes_stale_authority_without_hydrating_or_writing(operation, mutation):
    from src.services.conversation_intent_router import ConversationCapabilityResolver
    db, service, session, choice, claim = operation
    proof = {"businessExecutionStarted": False, "boundedAttemptConsumed": False, "plannerCalled": False,
             "proposalDelta": 0, "versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0}
    service._finish_fallback_choice_execution(claim["id"], "failed_retryable", outcome=proof)
    session_row = db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session,)).fetchone()
    resolver = ConversationCapabilityResolver(db)
    assert resolver.routing_snapshot(session_row).server_state["capabilityMatches"]["adopt_plan"]
    portfolio = choice["option"]["rootPortfolioId"]
    if mutation == "expired":
        db.execute("UPDATE agent_plan_portfolios SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (portfolio,))
    elif mutation == "carrier":
        db.execute("UPDATE agent_plan_portfolios SET source_assistant_turn_id = 'foreign' WHERE id = ?", (portfolio,))
    elif mutation == "root":
        db.execute("UPDATE conversation_turns SET status = 'superseded' WHERE id = ?", (choice["option"]["planningSelectionRootTurnId"],))
    else:
        db.execute("UPDATE agent_plan_proposals SET status = 'committed' WHERE id = ?", (choice["option"]["proposalId"],))
    db.commit()
    changes = db.total_changes
    assert not resolver.routing_snapshot(session_row).server_state["capabilityMatches"]["adopt_plan"]
    assert not resolver.routing_snapshot(session_row).server_state["capabilityMatches"]["adopt_plan"]
    assert db.total_changes == changes


@pytest.mark.parametrize("old_button", [False, True])
def test_reissued_operation_claim_race_has_one_winner_and_preserves_attempt(operation, old_button):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from src.services.conversation_capability_carrier import ConversationCapabilityCarrier

    db, service, session, original, claim = operation
    proof = {"businessExecutionStarted": False, "boundedAttemptConsumed": False, "plannerCalled": False,
             "proposalDelta": 0, "versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0}
    service._finish_fallback_choice_execution(claim["id"], "failed_retryable", outcome=proof)
    carrier = ConversationCapabilityCarrier(db)
    source = original["sourceAssistantTurnId"]
    for _ in range(2):
        capture = carrier.capture(session, source)
        latest = service._insert_turn(session, "assistant", "只读回答", "active", agent_response_json={"mode": "answer"})
        assert carrier.carry(session, latest, capture, nonexecuting=True)
        source = latest
    payload = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (source,)).fetchone()[0])
    option = next(item for item in payload["choiceOptions"] if item.get("proposalId") == original["option"]["proposalId"])
    fresh = {"sourceAssistantTurnId": source, "choiceId": option["id"], "action": option["action"], "option": option}
    requests = [service._insert_turn(session, "user", f"独立请求-{index}", "active") for index in range(2)]
    db.commit()
    path = db.execute("PRAGMA database_list").fetchone()[2]
    before = carrier.counts(session)
    start = Barrier(2)

    def submit(index):
        with sqlite3.connect(path, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            contender = AgentService(connection)
            contender.conversation_intent_router.routing_mode = "active-all"
            selected = json.loads(json.dumps(original if old_button and index == 0 else fresh))
            start.wait(timeout=5)
            try:
                result = contender._claim_fallback_choice_execution(session, requests[index], selected)
                return index, result["id"]
            except HTTPException as error:
                return index, error.detail["code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, (0, 1)))
    assert sum(value == claim["id"] for _, value in outcomes) == 1, outcomes
    if old_button:
        assert outcomes[0][1] != claim["id"]
        assert outcomes[1][1] == claim["id"]
    assert carrier.counts(session) == before
    with sqlite3.connect(path) as restarted:
        execution = restarted.execute("SELECT status, attempt FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone()
        assert execution == ("executing", 1)

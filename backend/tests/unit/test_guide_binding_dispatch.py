import json
import sqlite3

import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from backend.tests.unit.test_guide_grounded_continuation import _persist_guide_carrier
from src.services.agent_service import AgentService
from src.services.conversation_operation_identity import ConversationOperationIdentity
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService


@pytest.fixture
def binding():
    clear_database()
    with open_db() as db:
        session, selected = _persist_guide_carrier(db)
        service = AgentService(db)
        service.conversation_intent_router.routing_mode = "active-all"
        requirement = GuideContinuationRequirementService(db).build(session_id=session, selected_choice=selected, active_version_id=None)
        selected["_serverGuideEvidenceFingerprint"] = requirement["evidenceFingerprint"]
        request = service._insert_turn(session, "user", "参考攻略生成方案", "active")
        claim = service._claim_fallback_choice_execution(session, request, selected, guide_binding_requirement=requirement)
        yield db, service, session, selected, request, claim


def test_pure_binding_failure_produces_affirmative_settlement_before_cleanup(binding):
    db, service, session, selected, request, claim = binding
    before = service._choice_business_state(session)
    service._fail_fallback_choice_execution(session, request, ValueError("binding-validation-failed"))
    execution = dict(db.execute("SELECT * FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone())
    assert ConversationOperationIdentity.explicitly_unstarted(execution)
    assert service._choice_business_state(session) == before
    next_request = service._insert_turn(session, "user", "继续", "active")
    resumed = service._claim_fallback_choice_execution(session, next_request, selected)
    assert resumed["id"] == claim["id"] and resumed["attempt"] == 1
    assert json.loads(resumed["continuation_json"])["guideContinuationRequirement"] == json.loads(claim["continuation_json"])["guideContinuationRequirement"]


@pytest.mark.parametrize("change", ["dispatch", "frontier", "cancelled", "missing", "scope"])
def test_unknown_or_changed_binding_cannot_be_settled_as_unstarted(binding, change):
    db, service, session, _, _, claim = binding
    if change == "dispatch":
        service._finish_guide_binding_dispatch(session, claim["id"])
    elif change == "frontier":
        db.execute("UPDATE agent_plan_portfolios SET summary_json = ? WHERE session_id = ?", ('{"frontier":"advanced"}', session))
        db.commit()
    elif change == "cancelled":
        db.execute("UPDATE agent_choice_executions SET status = 'cancelled' WHERE id = ?", (claim["id"],))
        db.commit()
    else:
        journal = json.loads(claim["continuation_json"])
        if change == "scope":
            journal["guideContinuationRequirement"]["rootPortfolioId"] = "foreign"
        else:
            journal.pop("guideBindingDispatch")
        db.execute("UPDATE agent_choice_executions SET continuation_json = ? WHERE id = ?", (json.dumps(journal), claim["id"]))
        db.commit()
    assert not service._settle_unstarted_guide_binding_failure(session, claim["id"], RuntimeError("interrupted"))
    execution = dict(db.execute("SELECT * FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone())
    assert not ConversationOperationIdentity.explicitly_unstarted(execution)


def test_dispatch_fence_is_durable_to_another_connection_before_controller(binding):
    db, service, session, _, _, claim = binding
    service._finish_guide_binding_dispatch(session, claim["id"])
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as other:
        journal = json.loads(other.execute("SELECT continuation_json FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone()[0])
        assert journal["guideBindingDispatch"]["state"] == "dispatching"
    assert not db.in_transaction


def test_resuming_old_claim_does_not_retrofit_missing_dispatch_fence(binding):
    db, service, session, selected, _, claim = binding
    journal = json.loads(claim["continuation_json"])
    journal.pop("guideBindingDispatch")
    db.execute("UPDATE agent_choice_executions SET continuation_json = ? WHERE id = ?", (json.dumps(journal), claim["id"]))
    proof = {"businessExecutionStarted": False, "boundedAttemptConsumed": False, "plannerCalled": False,
             "proposalDelta": 0, "versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0}
    service._finish_fallback_choice_execution(claim["id"], "failed_retryable", outcome=proof)
    next_request = service._insert_turn(session, "user", "继续", "active")
    resumed = service._claim_fallback_choice_execution(session, next_request, selected,
        guide_binding_requirement=journal["guideContinuationRequirement"])
    assert resumed["id"] == claim["id"] and resumed["attempt"] == 1
    assert "guideBindingDispatch" not in json.loads(resumed["continuation_json"])
    assert not service._settle_unstarted_guide_binding_failure(session, claim["id"], RuntimeError("interrupted"))


@pytest.mark.parametrize("dispatched", [False, True])
def test_expired_native_guide_claim_uses_dispatch_proof_not_compatibility_recovery(binding, dispatched):
    from fastapi import HTTPException

    db, service, session, selected, _, claim = binding
    if dispatched:
        service._finish_guide_binding_dispatch(session, claim["id"])
    db.execute("UPDATE agent_choice_executions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (claim["id"],))
    db.commit()
    next_request = service._insert_turn(session, "user", "继续", "active")
    before = service._choice_business_state(session)
    if dispatched:
        with pytest.raises(HTTPException) as error:
            service._claim_fallback_choice_execution(session, next_request, selected)
        assert error.value.detail["code"] == "agent_choice_execution_in_progress"
        current = dict(db.execute("SELECT * FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone())
        assert current["status"] == "executing"
        assert current["attempt"] == 1
    else:
        resumed = service._claim_fallback_choice_execution(session, next_request, selected)
        assert resumed["id"] == claim["id"] and resumed["attempt"] == 1
        assert json.loads(resumed["continuation_json"])["guideBindingDispatch"]["state"] == "not_started"
    assert service._choice_business_state(session) == before


@pytest.mark.parametrize("status,expected", [("executing", "executing"), ("succeeded", "consumed"),
                                             ("failed_terminal", "failed_terminal"), ("cancelled", "cancelled")])
def test_generic_option_lifecycle_tracks_ordinary_operation_not_response_guide_evidence(binding, status, expected):
    db, service, _, selected, _, claim = binding
    service._finish_fallback_choice_execution(claim["id"], status)
    response = json.loads(db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ?", (selected["sourceAssistantTurnId"],)).fetchone()[0])
    original = next(item for item in response["choiceOptions"] if item["id"] == selected["choiceId"])
    assert not original.get("operationOrigin")
    # A guide response's generic button still authorizes ordinary continuation.
    # The natural-language guide action has a separate evidence-bound key.
    lifecycle = service._choice_options_with_lifecycle(selected["sourceAssistantTurnId"], response)
    assert next(item for item in lifecycle if item["id"] == selected["choiceId"])["lifecycle"] == "offered"
    ordinary_selected = json.loads(json.dumps(selected))
    ordinary_selected.pop("_serverGuideEvidenceFingerprint", None)
    request = service._insert_turn(claim["session_id"], "user", "普通继续探索", "active")
    ordinary = service._claim_fallback_choice_execution(claim["session_id"], request, ordinary_selected)
    assert ordinary["id"] != claim["id"]
    service._finish_fallback_choice_execution(ordinary["id"], status)
    lifecycle = service._choice_options_with_lifecycle(selected["sourceAssistantTurnId"], response)
    assert next(item for item in lifecycle if item["id"] == selected["choiceId"])["lifecycle"] == expected

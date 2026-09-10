import json
import sqlite3

import pytest
from fastapi import HTTPException

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_ingress_journal import AgentIngressJournal
from src.services.conversation_service import ConversationService


@pytest.fixture
def state():
    db = sqlite3.connect(sqlite_path_from_url(get_settings().database_url))
    db.row_factory = sqlite3.Row
    session = ConversationService(db).create_session(city="北京市")
    db.commit()
    yield db, session.session_id
    db.close()


def test_claim_is_durable_and_same_id_different_payload_conflicts(state):
    db, session = state
    service = AgentIngressJournal(db)
    payload = {"content": "参考攻略生成方案", "requestId": "request-1"}
    turn, claimed = service.claim(session, "request-1", payload)
    assert claimed and not db.in_transaction
    assert service.claim(session, "request-1", payload) == (turn, False)
    assert db.execute("SELECT COUNT(*) FROM conversation_turns WHERE session_id = ?", (session,)).fetchone()[0] == 1
    with pytest.raises(HTTPException) as error:
        service.claim(session, "request-1", {**payload, "content": "不同内容"})
    assert error.value.detail["code"] == "request_id_payload_conflict"
    with pytest.raises(HTTPException) as error:
        service.replay(session, service.existing(session, "request-1", payload))
    assert error.value.detail["code"] == "request_outcome_pending"


def test_context_merge_ignores_supplied_journal_and_retains_claim(state):
    db, session = state
    service = AgentIngressJournal(db)
    turn, _ = service.claim(session, "a", {"content": "hello"})
    merged = service.merge_context(turn, {"ingressJournal": {"state": "completed"}, "other": 1})
    assert merged["ingressJournal"]["state"] == "claimed"
    assert merged["other"] == 1


@pytest.mark.parametrize("persisted_policy", [None, "guide-poi-identity-v1"])
def test_identity_policy_is_owned_by_persisted_server_context(state, persisted_policy):
    db, session_id = state
    service = AgentIngressJournal(db)
    turn, _ = service.claim(session_id, "identity-policy", {"content": "source planning"})
    row = db.execute("SELECT agent_request_json FROM conversation_turns WHERE id=?", (turn,)).fetchone()
    context = json.loads(row[0])
    if persisted_policy is not None:
        context["sharedGuideIdentityPolicy"] = persisted_policy
        db.execute("UPDATE conversation_turns SET agent_request_json=? WHERE id=?", (json.dumps(context), turn))
        db.commit()
    merged = service.merge_context(turn, {"sharedGuideIdentityPolicy": "client-forged", "other": 1})
    if persisted_policy is None:
        assert "sharedGuideIdentityPolicy" not in merged
    else:
        assert merged["sharedGuideIdentityPolicy"] == persisted_policy
    assert merged["other"] == 1


def test_completed_result_survives_new_connection_without_writes(state):
    db, session = state
    service = AgentIngressJournal(db)
    turn, _ = service.claim(session, "a", {"content": "hello"})
    service.update(turn, state="completed", response={"userTurn": {"id": turn}, "assistantTurn": {"id": "old"}, "version": {"id": "old-version"}})
    db.commit()
    with sqlite3.connect(sqlite_path_from_url(get_settings().database_url)) as other:
        other.row_factory = sqlite3.Row
        recovered = AgentIngressJournal(other)
        before = other.total_changes
        result = recovered.replay(session, recovered.existing(session, "a", {"content": "hello"}))
        assert result["requestReplay"] == {"replayed": True, "isHistorical": True}
        assert result["version"] is None
        assert other.total_changes == before


def test_deleted_session_cannot_be_recreated_by_request_claim(state):
    db, session = state
    service = AgentIngressJournal(db)
    with pytest.raises(HTTPException) as error:
        service.claim("missing", "a", {"content": "hello"})
    assert error.value.status_code == 404


@pytest.mark.parametrize("different_payload", [False, True])
def test_concurrent_connections_claim_one_immutable_request(state, different_payload):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db, session = state
    path = db.execute("PRAGMA database_list").fetchone()[2]
    start = Barrier(2)

    def submit(index):
        with sqlite3.connect(path, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            payload = {"content": f"request-{index if different_payload else 0}"}
            start.wait(timeout=5)
            try:
                return AgentIngressJournal(connection).claim(session, "concurrent", payload)[1]
            except HTTPException as error:
                return error.detail["code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (0, 1)))
    assert results.count(True) == 1
    assert results.count("request_id_payload_conflict" if different_payload else False) == 1
    assert db.execute("SELECT COUNT(*) FROM conversation_turns WHERE session_id = ?", (session,)).fetchone()[0] == 1


@pytest.mark.parametrize("changed_before_completion", [False, True])
def test_in_place_carrier_reissue_cannot_replay_old_choices_as_current(state, changed_before_completion):
    from src.services.agent_service import AgentService

    db, session = state
    ingress = AgentIngressJournal(db)
    turn, _ = ingress.claim(session, "reissue-replay", {"content": "只读问题"})
    choices = [{"id": "old-choice", "action": "continue_plan_expansion"}]
    assistant = AgentService(db)._insert_turn(session, "assistant", "只读回答", "active",
        agent_response_json={"choiceOptions": choices})
    response = {"userTurn": {"id": turn}, "assistantTurn": {"id": assistant, "choiceOptions": choices}, "version": None}

    def reissue():
        # Do not rely on timestamp changes: capability CAS can replace only
        # the persisted response material on the same assistant row.
        db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps({"choiceOptions": [{**choices[0], "id": "fresh-choice"}]}), assistant))
        db.commit()

    if changed_before_completion:
        reissue()
    ingress.update(turn, state="completed", response=response, activeVersionId=None)
    db.commit()
    journal = ingress.existing(session, "reissue-replay", {"content": "只读问题"})
    if not changed_before_completion:
        assert ingress.replay(session, journal)["requestReplay"]["isHistorical"] is False
        reissue()
    before = db.total_changes
    replay = ingress.replay(session, journal)
    assert replay["requestReplay"] == {"replayed": True, "isHistorical": True}
    assert replay["assistantTurn"]["choiceOptions"] == []
    assert replay["assistantTurn"]["comparisonProjections"] == []
    assert db.total_changes == before


def test_old_journal_without_response_revision_never_revives_current_authority(state):
    from src.services.agent_service import AgentService

    db, session = state
    ingress = AgentIngressJournal(db)
    turn, _ = ingress.claim(session, "legacy-journal", {"content": "hello"})
    assistant = AgentService(db)._insert_turn(session, "assistant", "hello", "active", agent_response_json={})
    legacy = {"state": "completed", "activeVersionId": None,
              "response": {"userTurn": {"id": turn}, "assistantTurn": {"id": assistant}}}
    assert ingress.replay(session, legacy)["requestReplay"]["isHistorical"] is True

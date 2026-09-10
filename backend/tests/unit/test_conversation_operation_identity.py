import json
import sqlite3

import pytest

from src.services.conversation_operation_identity import ConversationOperationIdentity


@pytest.fixture
def state():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE conversation_turns (id TEXT, session_id TEXT, role TEXT, status TEXT, turn_index INT, agent_response_json TEXT)")
    db.execute("CREATE TABLE agent_choice_executions (session_id TEXT, source_turn_id TEXT, choice_id TEXT, status TEXT, continuation_json TEXT)")
    option = {"id": "c1", "action": "continue_plan_expansion", "kind": "simple_direction_more_plans",
              "rootPortfolioId": "p", "planningSelectionRootTurnId": "root", "requestContractFingerprint": "fp", "expectedBaseVersionId": None}
    def add(source, index, choice):
        db.execute("INSERT INTO conversation_turns VALUES (?, 's', 'assistant', 'active', ?, ?)", (source, index, json.dumps({"choiceOptions": [choice]})))
    add("a1", 1, option)
    yield db, option, add
    db.close()


def test_two_reissues_keep_direct_identity_but_next_direction_does_not(state):
    db, option, add = state
    service = ConversationOperationIdentity(db)
    initial = service.resolve("s", "a1", "c1")
    origin = service.origin("s", "a1", "c1")
    add("a2", 2, {**option, "id": "c2", "operationOrigin": origin})
    add("a3", 3, {**option, "id": "c3", "operationOrigin": service.origin("s", "a2", "c2")})
    assert service.resolve("s", "a3", "c3") == initial
    with pytest.raises(ValueError, match="operation_carrier_stale"):
        service.assert_latest("s", "a1")
    service.assert_latest("s", "a3")
    add("a4", 4, {**option, "id": "c4"})
    assert service.resolve("s", "a4", "c4") != initial


def test_guide_and_general_and_distinct_evidence_are_separate_operations(state):
    db, _, _ = state
    service = ConversationOperationIdentity(db)
    assert len({service.resolve("s", "a1", "c1", guide_evidence=evidence).operation_id
                for evidence in (None, "guide1", "guide2")}) == 3


def test_origin_is_server_canonical_direct_and_scope_bound(state):
    db, option, add = state
    service = ConversationOperationIdentity(db)
    origin = service.origin("s", "a1", "c1")
    add("a2", 2, {**option, "id": "c2", "rootPortfolioId": "foreign", "operationOrigin": origin})
    with pytest.raises(ValueError, match="operation_origin_scope_invalid"):
        service.resolve("s", "a2", "c2")
    add("a3", 3, {**option, "id": "c3", "operationOrigin": {**origin, "sourceAssistantTurnId": "a2", "choiceId": "c2"}})
    with pytest.raises(ValueError):
        service.resolve("s", "a3", "c3")


def test_modern_ordinary_claim_does_not_alias_or_block_guide_claim(state):
    db, option, _ = state
    service = ConversationOperationIdentity(db)
    ordinary = service.resolve("s", "a1", "c1")
    journal = {"operationIdentity": {"schemaVersion": "execution-operation-identity-v1",
        "sourceAssistantTurnId": "a1", "choiceId": "c1", "operationId": ordinary.operation_id,
        "guideEvidenceFingerprint": None}}
    db.execute("INSERT INTO agent_choice_executions VALUES ('s', 'a1', 'c1', 'succeeded', ?)", (json.dumps(journal),))
    guide = service.resolve("s", "a1", "c1", guide_evidence="guide")
    assert guide != ordinary
    option["guideEvidenceFingerprint"] = "guide"
    db.execute("UPDATE conversation_turns SET agent_response_json = ?", (json.dumps({"choiceOptions": [option]}),))
    assert service.offered_execution("s", "a1", "c1") is None
    journal["operationIdentity"].update(operationId=guide.operation_id, guideEvidenceFingerprint="guide")
    db.execute("INSERT INTO agent_choice_executions VALUES ('s', 'a1', ?, 'executing', ?)", (guide.choice_id, json.dumps(journal)))
    assert service.offered_execution("s", "a1", "c1")["status"] == "executing"
    assert service.execution("s", "a1", "c1")["status"] == "succeeded"


def test_missing_execution_evidence_does_not_mean_unstarted():
    assert not ConversationOperationIdentity.explicitly_unstarted({"status": "failed_retryable"})
    assert not ConversationOperationIdentity.explicitly_unstarted({"status": "cancelled", "outcome_json": "{}"})


def test_failed_carrier_requires_server_marker_before_resolver_binding(state):
    db, _, _ = state
    db.execute("UPDATE conversation_turns SET status = 'failed'")
    with pytest.raises(ValueError, match="operation_failed_carrier_not_reissued"):
        ConversationOperationIdentity(db).canonical_option("s", "a1", "c1")


@pytest.mark.parametrize("status", ["executing", "succeeded", "failed_retryable", "failed_terminal", "cancelled"])
def test_offered_lookup_detects_evidence_key_and_execution_has_direct_source(state, status):
    db, option, _ = state
    service = ConversationOperationIdentity(db)
    option["guideEvidenceFingerprint"] = "guide"
    db.execute("UPDATE conversation_turns SET agent_response_json = ?", (json.dumps({"choiceOptions": [option]}),))
    identity = service.resolve("s", "a1", "c1", guide_evidence="guide")
    journal = {"operationIdentity": {"schemaVersion": "execution-operation-identity-v1",
        "sourceAssistantTurnId": "a1", "choiceId": "c1", "operationId": identity.operation_id,
        "guideEvidenceFingerprint": "guide"}}
    db.execute("INSERT INTO agent_choice_executions VALUES ('s', 'a1', ?, ?, ?)", (identity.choice_id, status, json.dumps(journal)))
    execution = service.offered_execution("s", "a1", "c1")
    assert execution["status"] == status
    assert service.source_option_for_execution(execution)["id"] == "c1"
    assert service.lifecycle_status("s", "a1", option) == status


@pytest.mark.parametrize("status", ["succeeded", "executing", "failed_retryable", "failed_terminal", "cancelled"])
def test_legacy_guide_execution_keeps_exact_key_or_rejects_unknown_lineage(state, status):
    db, _, _ = state
    service = ConversationOperationIdentity(db)
    db.execute("INSERT INTO agent_choice_executions VALUES ('s', 'a1', 'c1', ?, '{}')", (status,))
    with pytest.raises(ValueError, match="operation_legacy_execution_lineage_unknown"):
        service.resolve("s", "a1", "c1", guide_evidence="evidence")
    db.execute("UPDATE agent_choice_executions SET continuation_json = ?", (json.dumps({
        "guideContinuationRequirement": {"evidenceFingerprint": "evidence"}}),))
    assert service.resolve("s", "a1", "c1", guide_evidence="evidence").choice_id == "c1"
    assert service.execution("s", "a1", "c1", guide_evidence="evidence")["status"] == status
    assert db.execute("SELECT COUNT(*) FROM agent_choice_executions").fetchone()[0] == 1

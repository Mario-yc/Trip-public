"""The corpus harness must not acquire authority, retry, or expose answers."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.conversation_intent_router import (
    ConversationIntentClassification,
    ConversationIntentModelOutcome,
    ConversationIntentRouter,
    IntentRoutingSnapshotV1,
)


SPEC = importlib.util.spec_from_file_location(
    "semantic_entry_corpus", Path(__file__).resolve().parents[2] / "evals/run_semantic_entry_corpus.py"
)
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)


def snapshot():
    lineage = {"planningSelectionRootTurnId": "private_root", "rootPortfolioId": "private_portfolio",
               "requestContractFingerprint": "private_request", "expectedBaseVersionId": None}
    choice = {"sourceAssistantTurnId": "private_carrier", "choiceId": "private_choice"}
    guide = {**lineage, "sourceAssistantTurnId": "private_guide", "capabilitySourceAssistantTurnId": "private_carrier",
             "guideChoiceExecutionId": "private_execution", "evidenceFingerprint": "private_evidence",
             "requirementFingerprint": "private_requirement"}
    return IntentRoutingSnapshotV1("frozen_snapshot", {"guideContinuation": {"available": True}}, {
        **lineage, "hasPlanningRoot": True, "hasActiveVersion": False, "hasPortfolio": True,
        "guideRequirement": guide, "latestAssistant": {"turnId": "private_carrier"},
        "capabilityMatches": {"continue_plan_expansion": [{**choice, "boundState": lineage,
            "option": {"action": "continue_plan_expansion", "kind": "simple_direction_more_plans"}}]},
    })


def tool_response(action):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
        "type": "function", "function": {"name": action, "arguments": '{}'}
    }]}}]}


def test_snapshot_read_closes_connection_even_when_session_missing(tmp_path, monkeypatch):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversation_sessions (id TEXT)")
    opened = []
    real_open = corpus.read_only

    def recording_open(path):
        connection = real_open(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(corpus, "read_only", recording_open)
    with pytest.raises(ValueError, match="real_session_missing"):
        corpus.read_snapshot(path, "missing")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_backup_preserves_source_without_schema_migration(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE conversation_sessions (id TEXT, active_version_id TEXT)")
        db.execute("INSERT INTO conversation_sessions VALUES ('real_session', NULL)")
        db.execute("CREATE TABLE immutable_evidence (payload TEXT)")
        db.execute("INSERT INTO immutable_evidence VALUES ('real persisted evidence')")
    original_hash = corpus.file_digest(source)
    monkeypatch.setattr(corpus, "sqlite_path_from_url", lambda _: source)
    monkeypatch.setattr(corpus.ConversationCapabilityResolver, "routing_snapshot", lambda self, row: snapshot())
    run_dir = tmp_path / "new_run"
    manifest = corpus.prepare(run_dir, "real_session")
    assert corpus.file_digest(source) == original_hash
    with sqlite3.connect(run_dir / manifest["backup"]) as db:
        assert db.execute("SELECT payload FROM immutable_evidence").fetchone()[0] == "real persisted evidence"
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 2
    with pytest.raises(FileExistsError):
        corpus.prepare(run_dir, "real_session")


def test_core_prepare_copies_prior_verified_backup_and_preserves_prior_artifacts(tmp_path, monkeypatch):
    prior_dir = tmp_path / "prior_run"
    prior_dir.mkdir()
    source = prior_dir / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE conversation_sessions (id TEXT, active_version_id TEXT)")
        db.execute("INSERT INTO conversation_sessions VALUES ('real_session', NULL)")
    source_hash = corpus.file_digest(source)
    corpus.write_json(prior_dir / "manifest.json", {"backup": source.name, "backupSha256": source_hash})
    corpus.write_json(prior_dir / "report.json", {"status": "measured", "backupSha256": source_hash, "backupUnchanged": True})
    preserved = {path.name: path.read_bytes() for path in prior_dir.iterdir()}
    monkeypatch.setattr(corpus, "sqlite_path_from_url", lambda _: pytest.fail("must not open current configured database"))
    monkeypatch.setattr(corpus.ConversationCapabilityResolver, "routing_snapshot", lambda self, row: snapshot())
    run_dir = tmp_path / "core_run"
    manifest = corpus.prepare(run_dir, "real_session", core_only=True, source_backup=source)
    assert manifest["caseSuite"] == "core_seven"
    assert manifest["maximumNativeInvocations"] == 7
    assert manifest["maximumLegacyInvocations"] == 0
    assert manifest["priorBackupSource"]["sha256"] == source_hash
    assert len(json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))) == 7
    assert len(json.loads((run_dir / "expectations.json").read_text(encoding="utf-8"))) == 7
    assert {path.name: path.read_bytes() for path in prior_dir.iterdir()} == preserved


def test_prior_backup_hash_drift_is_rejected_before_new_run_is_created(tmp_path):
    prior_dir = tmp_path / "prior_run"
    prior_dir.mkdir()
    source = prior_dir / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE original (value TEXT)")
    corpus.write_json(prior_dir / "manifest.json", {"backup": source.name, "backupSha256": "wrong_hash"})
    corpus.write_json(prior_dir / "report.json", {"status": "measured", "backupSha256": "wrong_hash", "backupUnchanged": True})
    run_dir = tmp_path / "core_run"
    with pytest.raises(ValueError, match="prior_backup_attribution_invalid"):
        corpus.prepare(run_dir, "real_session", core_only=True, source_backup=source)
    assert not run_dir.exists()


def test_direct_model_correctness_does_not_count_safe_fallback_as_success():
    state = snapshot()
    outcome = ConversationIntentModelOutcome(error_code="controller_lite_timeout", provider_invoked=True)
    routed = ConversationIntentRouter(lite_classifier=lambda _: outcome, routing_mode="active-all").classify(
        corpus.CORPUS[0][1], routing_snapshot=state)
    row = corpus.summarize_result({"id": "timeout"}, "continue_with_guide", routed, [outcome], state, 2500)
    assert row["timeout"] is True
    assert row["directActionCorrect"] is False
    assert row["structuredValid"] is False
    assert row["finalActionCorrect"] is True
    assert row["fallback"] is True


def test_valid_but_uninvoked_value_cannot_count_as_direct_model_success():
    state = snapshot()
    outcome = ConversationIntentModelOutcome(value=tool_response("continue_with_guide"), provider_invoked=False)
    routed = ConversationIntentRouter(lite_classifier=lambda _: outcome, routing_mode="active-all").classify(
        corpus.CORPUS[0][1], routing_snapshot=state)
    row = corpus.summarize_result({"id": "uninvoked"}, "continue_with_guide", routed, [outcome], state, 0)
    assert row["structuredValid"] is True
    assert row["modelCalled"] is False
    assert row["directActionCorrect"] is False


def test_ordinary_continuation_never_builds_guide_obligation(monkeypatch):
    state = snapshot()
    routed = ConversationIntentRouter(lite_classifier=lambda _: tool_response("continue_directions"),
        routing_mode="active-all").classify("沿用原来的需求，继续生成其他方案", routing_snapshot=state)
    manifest = {"authority": {key: state.server_state[key] for key in corpus.AUTHORITY_FIELDS},
                "expectedChoice": {"sourceAssistantTurnId": "private_carrier", "choiceId": "private_choice"}}
    monkeypatch.setattr(corpus.GuideContinuationRequirementService, "build",
                        lambda *args, **kwargs: pytest.fail("ordinary continuation must not bind guide evidence"))
    result = corpus.verify_binding(None, {"id": "real_session", "active_version_id": None}, state,
                                   routed, "continue_directions", manifest)
    assert result["status"] == "unique"
    assert result["sourceChoicePreserved"] is True
    assert result["wrongRoot"] is False
    assert result["unexpectedGuideObligation"] is False


def test_guide_downgrade_detects_wrong_root_and_lost_obligation():
    state = snapshot()
    routed = ConversationIntentRouter(lite_classifier=lambda _: tool_response("create"),
        routing_mode="active-all").classify("开始另一个旅行", routing_snapshot=state)
    manifest = {"authority": {key: state.server_state[key] for key in corpus.AUTHORITY_FIELDS},
                "expectedChoice": {"sourceAssistantTurnId": "private_carrier", "choiceId": "private_choice"}}
    result = corpus.verify_binding(None, {"id": "real_session", "active_version_id": None}, state,
                                   routed, "continue_with_guide", manifest)
    assert result["wrongRoot"] is True
    assert result["guideObligationLost"] is True
    assert result["newRootRequested"] is True


def test_corpus_answers_and_authority_never_enter_model_context():
    state = snapshot()
    captured = []

    def classify(context):
        captured.append(json.dumps(context, ensure_ascii=False))
        return tool_response("continue_with_guide")

    router = ConversationIntentRouter(lite_classifier=classify, routing_mode="active-all")
    router.classify(corpus.CORPUS[6][1], routing_snapshot=state)
    assert len(captured) == 1
    assert corpus.CORPUS[6][1] in captured[0]
    assert all(value not in captured[0] for value in ("private_", "expectedAction", "expectationsFingerprint", "core_1"))


def test_changed_frozen_corpus_stops_before_any_provider_construction(tmp_path, monkeypatch):
    inputs = [{"id": "changed", "message": "changed"}]
    corpus.write_json(tmp_path / "manifest.json", {
        "backup": "source.sqlite3", "inputsFingerprint": "original", "expectationsFingerprint": "original"})
    corpus.write_json(tmp_path / "inputs.json", inputs)
    corpus.write_json(tmp_path / "expectations.json", [])
    monkeypatch.setattr(corpus, "DeepSeekAgentProvider", lambda: pytest.fail("provider must remain untouched"))
    with pytest.raises(ValueError, match="frozen_corpus_changed"):
        corpus.execute(tmp_path)


def test_nonexecution_does_not_report_wrong_root_or_dropped_guide():
    result = ConversationIntentRouter(routing_mode="active-all").classify("为什么生成失败？", routing_snapshot=snapshot())
    result = replace(result, classification=ConversationIntentClassification(intent="inspect_or_explain", confidence=1,
        requestedScope="none", isQuestion=True, isNegated=False))
    binding = corpus.verify_binding(None, None, snapshot(), result, "continue_with_guide", {})
    assert binding["status"] == "not_dispatched"
    assert binding["wrongRoot"] is False
    assert binding["guideObligationLost"] is False


@pytest.mark.parametrize("core_only", [False, True])
def test_execute_drains_workers_before_binding_and_refuses_second_run(tmp_path, monkeypatch, core_only):
    state = snapshot()
    backup = tmp_path / "source.sqlite3"
    with sqlite3.connect(backup) as db:
        db.execute("CREATE TABLE existing_data (value TEXT)")
    cases = corpus.CORPUS[:7] if core_only else corpus.CORPUS
    inputs = [{"id": case_id, "message": message} for case_id, message, _ in cases]
    expectations = [{"id": case_id, "action": action} for case_id, _, action in cases]
    corpus.write_json(tmp_path / "inputs.json", inputs)
    corpus.write_json(tmp_path / "expectations.json", expectations)
    corpus.write_json(tmp_path / "manifest.json", {
        "sessionId": "real_session", "backup": "source.sqlite3", "backupSha256": corpus.file_digest(backup),
        "caseSuite": "core_seven" if core_only else "full_twelve",
        "snapshotFingerprint": state.fingerprint, "inputsFingerprint": corpus.digest(inputs),
        "expectationsFingerprint": corpus.digest(expectations),
        "authority": {field: state.server_state[field] for field in corpus.AUTHORITY_FIELDS},
        "expectedChoice": {"sourceAssistantTurnId": "private_carrier", "choiceId": "private_choice"},
        "guideAuthority": {field: state.server_state["guideRequirement"].get(field) for field in corpus.GUIDE_FIELDS},
    })
    flags = {"drained": False, "model_calls": 0}

    class FakeController:
        def __init__(self, **kwargs):
            self._decision_executor = self

        def shutdown(self, *, wait):
            assert wait is True
            flags["drained"] = True

        def classify_conversation_intent(self, context):
            assert flags["drained"] is False
            flags["model_calls"] += 1
            encoded = json.dumps(context)
            assert "private_" not in encoded and "expectationsFingerprint" not in encoded
            if context["schemaVersion"] == "conversation-action-context-v1":
                return ConversationIntentModelOutcome(value=tool_response("continue_with_guide"), provider_invoked=True)
            assert core_only is False, "core corpus cannot call the legacy classifier"
            return ConversationIntentModelOutcome(value={"intent": "create_itinerary", "confidence": 0.8,
                "requestedScope": "new_itinerary", "isQuestion": False, "isNegated": False}, provider_invoked=True)

    real_read_only = corpus.read_only

    def after_draining(path):
        assert flags["drained"] is True
        return real_read_only(path)

    monkeypatch.setattr(corpus, "read_snapshot", lambda *args: ({"id": "real_session", "active_version_id": None}, state))
    monkeypatch.setattr(corpus, "source_fingerprint", lambda: "frozen_source")
    monkeypatch.setattr(corpus, "read_only", after_draining)
    monkeypatch.setattr(corpus, "get_settings", lambda: SimpleNamespace(deepseek_api_key="test-only", provider_mode="mock", deepseek_tool_strict_mode=False,
        agent_intent_routing_mode="active-all", deepseek_model="test", deepseek_base_url="https://example.invalid",
        agent_controller_lite_timeout_seconds=2.5, agent_controller_decision_timeout_seconds=10,
        agent_controller_total_budget_seconds=12.5))
    monkeypatch.setattr(corpus, "DeepSeekAgentProvider", lambda: SimpleNamespace(model="test"))
    monkeypatch.setattr(corpus, "AgentAutonomyController", FakeController)
    monkeypatch.setattr(corpus.GuideContinuationRequirementService, "build", lambda *args, **kwargs: state.server_state["guideRequirement"])
    corpus.freeze(tmp_path)
    result = corpus.execute(tmp_path)
    assert result["status"] == "measured"
    assert result["backupUnchanged"] is True
    assert result["providerWorkersDrainedBeforeBinding"] is True
    assert 1 <= flags["model_calls"] <= 13
    if core_only:
        assert flags["model_calls"] == 7
        assert result["maximumNativeInvocations"] == 7
        assert result["maximumLegacyInvocations"] == 0
        assert result["legacyBaseline"]["modelCalled"] is False
    calls = flags["model_calls"]
    with pytest.raises(FileExistsError):
        corpus.execute(tmp_path)
    assert flags["model_calls"] == calls
    monkeypatch.setattr(corpus, "source_fingerprint", lambda: "changed_source")
    with pytest.raises(ValueError, match="frozen_source_changed"):
        corpus.execute(tmp_path)
    assert flags["model_calls"] == calls

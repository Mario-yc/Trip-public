"""One-shot real-state semantic selection; never execute domain actions.

Prepare an unmodified SQLite backup and blind corpus first. Execute only after
the reviewed production sources are frozen. All SQLite connections are closed
before model calls; subsequent authority binding uses the backup in query-only
mode. Inputs/expected answers are separate and expectations never reach models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.conversation_action_catalog import ConversationActionCatalog, parse_action_response
from src.services.conversation_intent_router import ConversationCapabilityResolver, ConversationIntentRouter
from src.services.deepseek_agent_provider import CONTROLLER_LITE_MAX_OUTPUT_TOKENS, DeepSeekAgentProvider
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService


ROOT = Path(__file__).resolve().parents[2]
CORPUS = (
    ("core_1", "参考攻略建议的地点，生成新的方案", "continue_with_guide"),
    ("core_2", "参考攻略里面的景点，按照我的需求给出个方案", "continue_with_guide"),
    ("core_3", "基于普通攻略的建议，生成个方案", "continue_with_guide"),
    ("core_4", "基于搜索到的建议，给我生成一个方案出来", "continue_with_guide"),
    ("core_5", "按刚才推荐的地方再排一版", "continue_with_guide"),
    ("core_6", "能不能按这些建议排个方案？", "continue_with_guide"),
    ("reported_failure", "结合我的需求，参考网络攻略制定一份方案", "continue_with_guide"),
    ("ordinary_continuation", "沿用原来的需求，继续生成其他方案", "continue_directions"),
    ("explanation", "为什么参考攻略生成方案会失败？", "explain"),
    ("negation", "不要按攻略生成方案，我只想了解原因", "explain"),
    ("polite_request", "麻烦你参考上面的攻略，按我的要求安排一版，可以吗？", "continue_with_guide"),
    ("ambiguous", "就按那个来吧", "clarify"),
)
AUTHORITY_FIELDS = (
    "planningSelectionRootTurnId", "rootPortfolioId", "requestContractFingerprint", "expectedBaseVersionId",
)
GUIDE_FIELDS = (
    "sourceAssistantTurnId", "capabilitySourceAssistantTurnId", "guideChoiceExecutionId",
    "evidenceFingerprint", "requirementFingerprint", *AUTHORITY_FIELDS,
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def source_fingerprint() -> str:
    paths = sorted([*(ROOT / "backend/src").rglob("*.py"), Path(__file__).resolve()])
    return digest([(path.relative_to(ROOT).as_posix(), file_digest(path)) for path in paths])


def read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    return db


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    with path.open("x" if exclusive else "w", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2)


def read_snapshot(path: Path, session_id: str):
    with closing(read_only(path)) as db:
        row = db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("real_session_missing")
        snapshot = ConversationCapabilityResolver(db).routing_snapshot(row)
        return dict(row), snapshot


def prepare(run_dir: Path, session_id: str, *, core_only: bool = False,
            source_backup: Path | None = None) -> dict[str, Any]:
    source_path = source_backup.resolve() if source_backup else sqlite_path_from_url(get_settings().database_url).resolve()
    prior_source = None
    if source_backup is not None:
        prior_manifest = json.loads((source_path.parent / "manifest.json").read_text(encoding="utf-8"))
        prior_report = json.loads((source_path.parent / "report.json").read_text(encoding="utf-8"))
        original_hash = file_digest(source_path)
        if (source_path.name != prior_manifest["backup"]
                or original_hash != prior_manifest["backupSha256"]
                or prior_report.get("backupSha256") != original_hash
                or prior_report.get("status") != "measured"
                or prior_report.get("backupUnchanged") is not True):
            raise ValueError("prior_backup_attribution_invalid")
        prior_source = {"runId": source_path.parent.name, "file": source_path.name, "sha256": original_hash}
    with closing(read_only(source_path)) as source:
        if source.execute("SELECT 1 FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise ValueError("real_session_missing")
        run_dir.mkdir(parents=True, exist_ok=False)
        backup_path = run_dir / "source.sqlite3"
        with closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)
    _, snapshot = read_snapshot(backup_path, session_id)
    state = snapshot.server_state
    matches = (state.get("capabilityMatches") or {}).get("continue_plan_expansion") or []
    if len(matches) != 1 or not state.get("guideRequirement") or not state.get("hasPlanningRoot"):
        raise ValueError("real_guide_continuation_authority_required")
    cases = CORPUS[:7] if core_only else CORPUS
    inputs = [{"id": case_id, "message": message} for case_id, message, _ in cases]
    expectations = [{"id": case_id, "action": action} for case_id, _, action in cases]
    if prior_source and file_digest(source_path) != prior_source["sha256"]:
        raise ValueError("prior_backup_changed")
    write_json(run_dir / "inputs.json", inputs, exclusive=True)
    write_json(run_dir / "expectations.json", expectations, exclusive=True)
    manifest = {
        "schemaVersion": "semantic-entry-corpus-v1", "runId": run_dir.name, "sessionId": session_id,
        "status": "prepared_no_model_calls", "backup": backup_path.name,
        "caseSuite": "core_seven" if core_only else "full_twelve", "priorBackupSource": prior_source,
        "backupSha256": file_digest(backup_path), "snapshotFingerprint": snapshot.fingerprint,
        "inputsFingerprint": digest(inputs), "expectationsFingerprint": digest(expectations),
        "modelProjection": snapshot.model_projection,
        "availableActions": sorted(ConversationActionCatalog(snapshot).names),
        "authority": {field: state.get(field) for field in AUTHORITY_FIELDS},
        "expectedChoice": {field: matches[0][field] for field in ("sourceAssistantTurnId", "choiceId")},
        "guideAuthority": {field: state["guideRequirement"].get(field) for field in GUIDE_FIELDS},
        "maximumNativeInvocations": len(cases), "maximumLegacyInvocations": 0 if core_only else 1, "automaticRetries": 0,
        "domainExecutorsInvoked": False,
    }
    write_json(run_dir / "manifest.json", manifest, exclusive=True)
    return manifest


def projected_action(result) -> str:
    if result.semantic_action:
        return result.semantic_action["name"]
    if result.requires_clarification:
        return "clarify"
    if result.classification is None:
        return "unavailable"
    intent = result.classification.intent
    if intent == "continue_plan_expansion":
        return "continue_with_guide" if result.continuation_mode == "guide_grounded" else "continue_directions"
    return {
        "inspect_or_explain": "explain", "cancel_action": "cancel", "create_itinerary": "create",
        "regenerate_from_scratch": "regenerate", "search_travel_guide_advice": "search_guide",
        "clarification_answer": "answer_clarification",
    }.get(intent, intent)


def freeze(run_dir: Path) -> dict:
    """Seal production after the parent finishes changes; preparation is earlier."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    result = {"status": "source_frozen_no_model_calls", "sourceFingerprint": source_fingerprint(),
              "caseSuite": manifest.get("caseSuite", "full_twelve"),
              "snapshotFingerprint": manifest["snapshotFingerprint"],
              "inputsFingerprint": manifest["inputsFingerprint"],
              "expectationsFingerprint": manifest["expectationsFingerprint"]}
    write_json(run_dir / "source-freeze.json", result, exclusive=True)
    return result


def summarize_result(case: dict, expected: str, result, observations: list, snapshot, elapsed_ms: float) -> dict:
    raw_action = None
    structured = None
    if observations:
        structured = False
        try:
            raw_action = parse_action_response(observations[-1].value, ConversationActionCatalog(snapshot))[0].name
            structured = True
        except ValueError:
            pass
    final_action = projected_action(result)
    error_texts = [str(item.error_code or "").lower() for item in observations]
    return {
        "id": case["id"], "elapsedMs": round(elapsed_ms, 2), "rawAction": raw_action,
        "structuredValid": structured,
        "directActionCorrect": result.model_called and structured is True and raw_action == expected,
        "modelCalled": result.model_called, "modelAttempted": result.model_attempted,
        "modelSucceeded": result.model_succeeded,
        "timeout": any("timeout" in value or "timed out" in value for value in error_texts),
        "providerError": bool(any(error_texts)), "fallback": result.source in {"legacy_fallback", "safe_fallback"},
        "source": result.source, "reasonCode": result.reason_code, "finalAction": final_action,
        "finalActionCorrect": final_action == expected,
        "intent": result.classification.intent if result.classification else None,
        "confidence": result.classification.confidence if result.classification else None,
        "executionDisposition": result.execution_disposition, "requiresClarification": result.requires_clarification,
        "continuationMode": result.continuation_mode, "targetReference": result.target_reference,
        "invocationLedger": result.invocation_ledger,
        "providerTelemetry": [
            {key: evidence[key] for key in (
                "callKind", "providerInvoked", "payloadBytes", "captureState", "httpStatus",
                "responseHeadersReceived", "responseSchemaVersion", "finishReason", "responseIntegrity",
                "workerQueueMs", "ttfbDurationMs", "readDurationMs",
            ) if key in evidence}
            for evidence in result.model_performance_evidence
        ],
    }


def verify_binding(db, session, snapshot, result, expected: str, manifest: dict) -> dict:
    final_action = projected_action(result)
    active_planning = final_action in {"create", "regenerate", "continue_directions", "continue_with_guide", "refine_request"}
    binding = {"status": "not_dispatched", "newRootRequested": active_planning and final_action in {"create", "regenerate"},
               "wrongRoot": False, "guideObligationLost": expected == "continue_with_guide" and active_planning and final_action != expected,
               "unexpectedGuideObligation": expected == "continue_directions" and result.continuation_mode == "guide_grounded"}
    if result.requires_clarification or result.classification is None or not active_planning:
        return binding
    resolver = ConversationCapabilityResolver(db)
    target = resolver.resolve_target(snapshot=snapshot, classification=result.classification, target_reference=result.target_reference)
    capability = resolver.resolve(session=session, classification=result.classification, routing_snapshot=snapshot,
                                  target_resolution=target, continuation_mode=result.continuation_mode)
    binding.update(status=capability.status, targetStatus=target.status, capability=capability.to_context())
    if expected in {"continue_with_guide", "continue_directions"}:
        context = capability.to_context()
        binding["wrongRoot"] = binding["newRootRequested"] or any(context.get(field) != manifest["authority"][field] for field in AUTHORITY_FIELDS)
        binding["sourceChoicePreserved"] = capability.selected_choice_request == manifest["expectedChoice"]
    if result.continuation_mode == "guide_grounded" and capability.status == "unique":
        choice = capability.selected_choice_request
        matches = snapshot.server_state["capabilityMatches"]["continue_plan_expansion"]
        match = next(item for item in matches if all(item[field] == value for field, value in choice.items()))
        requirement = GuideContinuationRequirementService(db).build(
            session_id=session["id"], selected_choice=match, active_version_id=session["active_version_id"])
        preserved = all(requirement.get(field) == value for field, value in manifest["guideAuthority"].items())
        target_source_preserved = (target.target or {}).get("sourceAssistantTurnId") == requirement["sourceAssistantTurnId"]
        binding.update(guideRequirementPreserved=preserved, guideTargetSourcePreserved=target_source_preserved)
        binding["guideObligationLost"] = not preserved or not target_source_preserved
    return binding


def metrics(rows: list[dict]) -> dict:
    durations = sorted(row["elapsedMs"] for row in rows)
    model_rows = [row for row in rows if row["modelCalled"]]
    model_durations = sorted(row["elapsedMs"] for row in model_rows)
    core_rows = [row for row in rows if row["id"].startswith("core_") or row["id"] == "reported_failure"]
    return {
        "sampleCount": len(rows), "nativeProviderCallCount": len(model_rows),
        "directActionCorrectCount": sum(row["directActionCorrect"] for row in rows),
        "structuredValidCount": sum(row["structuredValid"] is True for row in model_rows),
        "safetyShortCircuitCount": sum(not row["modelCalled"] and row["source"] == "deterministic_fast_path" for row in rows),
        "uninvokedModelAttemptCount": sum(row["modelAttempted"] and not row["modelCalled"] for row in rows),
        "timeoutCount": sum(row["timeout"] for row in rows), "fallbackCount": sum(row["fallback"] for row in rows),
        "finalActionCorrectCount": sum(row["finalActionCorrect"] for row in rows),
        "wrongRootCount": sum(row["binding"]["wrongRoot"] for row in rows),
        "guideObligationLostCount": sum(row["binding"]["guideObligationLost"] for row in rows),
        "unexpectedGuideObligationCount": sum(row["binding"]["unexpectedGuideObligation"] for row in rows),
        "p50Ms": durations[math.ceil(len(durations) * 0.5) - 1], "p95Ms": durations[math.ceil(len(durations) * 0.95) - 1],
        "nativeP50Ms": model_durations[math.ceil(len(model_durations) * 0.5) - 1] if model_durations else None,
        "nativeP95Ms": model_durations[math.ceil(len(model_durations) * 0.95) - 1] if model_durations else None,
        "coreAndReportedFailureCount": len(core_rows),
        "coreAndReportedFailureDirectCorrectCount": sum(row["directActionCorrect"] for row in core_rows),
        "coreAndReportedFailureFinalCorrectCount": sum(row["finalActionCorrect"] for row in core_rows),
        "coreAndReportedFailureBindingCorrectCount": sum(
            row["binding"].get("status") == "unique"
            and row["binding"].get("sourceChoicePreserved") is True
            and row["binding"].get("guideRequirementPreserved") is True
            and row["binding"].get("guideTargetSourcePreserved") is True
            and row["binding"]["wrongRoot"] is False
            and row["binding"]["guideObligationLost"] is False
            for row in core_rows
        ),
        "percentileMethod": "nearest_rank_all_entries_including_safety_short_circuits",
        "acceptanceBoundary": "semantic_selection_and_read_only_binding_only_not_planner_or_browser_acceptance",
    }


def execute(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    inputs = json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))
    expectations = json.loads((run_dir / "expectations.json").read_text(encoding="utf-8"))
    backup = run_dir / manifest["backup"]
    if digest(inputs) != manifest["inputsFingerprint"] or digest(expectations) != manifest["expectationsFingerprint"]:
        raise ValueError("frozen_corpus_changed")
    if file_digest(backup) != manifest["backupSha256"]:
        raise ValueError("frozen_backup_changed")
    case_suite = manifest.get("caseSuite", "full_twelve")
    if case_suite not in {"full_twelve", "core_seven"}:
        raise ValueError("frozen_corpus_scope_invalid")
    core_only = case_suite == "core_seven"
    cases = CORPUS[:7] if core_only else CORPUS
    if (inputs != [{"id": case_id, "message": message} for case_id, message, _ in cases]
            or expectations != [{"id": case_id, "action": action} for case_id, _, action in cases]):
        raise ValueError("frozen_corpus_scope_invalid")
    session, snapshot = read_snapshot(backup, manifest["sessionId"])
    if snapshot.fingerprint != manifest["snapshotFingerprint"]:
        raise ValueError("frozen_routing_snapshot_changed")
    source_freeze = json.loads((run_dir / "source-freeze.json").read_text(encoding="utf-8"))
    frozen_source = source_fingerprint()
    if source_freeze["sourceFingerprint"] != frozen_source:
        raise ValueError("frozen_source_changed")
    if any(source_freeze[field] != manifest[field] for field in (
        "snapshotFingerprint", "inputsFingerprint", "expectationsFingerprint"
    )) or source_freeze.get("caseSuite", "full_twelve") != case_suite:
        raise ValueError("frozen_source_scope_changed")
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise ValueError("provider_not_configured")
    provider = DeepSeekAgentProvider()
    report = {
        "schemaVersion": "semantic-entry-corpus-result-v1", "runId": run_dir.name, "status": "reserved",
        "caseSuite": case_suite, "priorBackupSource": manifest.get("priorBackupSource"),
        "sourceFingerprint": frozen_source, "sourceScope": "all_backend_src_python_and_this_runner",
        "snapshotFingerprint": snapshot.fingerprint, "backupSha256": manifest["backupSha256"],
        "inputsFingerprint": manifest["inputsFingerprint"], "expectationsFingerprint": manifest["expectationsFingerprint"],
        "configuredMode": settings.agent_intent_routing_mode, "evaluationMode": "active-all",
        "providerMode": settings.provider_mode, "model": provider.model, "configuredModel": settings.deepseek_model,
        "toolStrictMode": settings.deepseek_tool_strict_mode,
        "endpointHost": urlsplit(settings.deepseek_base_url).hostname,
        "timeoutSeconds": settings.agent_controller_lite_timeout_seconds,
        "nativeOutputTokenLimit": 512, "legacyOutputTokenLimit": CONTROLLER_LITE_MAX_OUTPUT_TOKENS,
        "maximumNativeInvocations": len(cases), "maximumLegacyInvocations": 0 if core_only else 1, "automaticRetries": 0,
        "databaseConnectionsDuringModelCalls": 0, "domainExecutorsInvoked": False, "results": [],
    }
    # Exclusive claim remains even after timeout, interruption or assertion failure.
    write_json(run_dir / "execution-claim.json", {"sourceFingerprint": frozen_source}, exclusive=True)
    write_json(run_dir / "report.json", report, exclusive=True)
    controller = AgentAutonomyController(provider=provider,
        lite_timeout_seconds=settings.agent_controller_lite_timeout_seconds,
        decision_timeout_seconds=settings.agent_controller_decision_timeout_seconds,
        total_budget_seconds=settings.agent_controller_total_budget_seconds)
    pending = []
    observations = []

    def classify(context):
        outcome = controller.classify_conversation_intent(context)
        observations.append(outcome)
        return outcome

    router = ConversationIntentRouter(lite_classifier=classify, routing_mode="active-all")
    for case, expectation in zip(inputs, expectations):
        observations.clear()
        started = time.perf_counter()
        result = router.classify(case["message"], routing_snapshot=snapshot)
        row = summarize_result(case, expectation["action"], result, observations, snapshot, (time.perf_counter() - started) * 1000)
        report["results"].append(row)
        pending.append((row, result, expectation["action"]))
        write_json(run_dir / "report.json", report)
        print(json.dumps({key: row[key] for key in ("id", "finalAction", "modelCalled", "timeout", "elapsedMs")}), flush=True)
    legacy = None
    report["legacyBaseline"] = {"status": "not_run", "reason": "core_only_budget_zero", "modelCalled": False}
    if not core_only:
        legacy_case = next(case for case in inputs if case["id"] == "reported_failure")
        observations.clear()
        started = time.perf_counter()
        legacy = ConversationIntentRouter(lite_classifier=classify, routing_mode="legacy-only").classify(
            legacy_case["message"], state_summary=ConversationCapabilityResolver.state_summary_from_snapshot(snapshot))
        report["legacyBaseline"] = summarize_result(legacy_case, "continue_with_guide", legacy, [], snapshot,
                                                   (time.perf_counter() - started) * 1000)
        report["legacyBaseline"]["timeout"] = any("timeout" in str(item.error_code or "").lower() for item in observations)
        report["legacyBaseline"]["structuredValid"] = legacy.model_succeeded
    # A controller deadline can return while urllib's existing request is still
    # finishing. Drain this standalone process's executor before reopening DB;
    # this does not start or retry a model request or change its decision budget.
    drain_started = time.perf_counter()
    controller._decision_executor.shutdown(wait=True)
    report["providerWorkerDrainMs"] = round((time.perf_counter() - drain_started) * 1000, 2)
    report["providerWorkersDrainedBeforeBinding"] = True
    # Only now open SQLite, and only for pure authority/guide-requirement binding.
    with closing(read_only(backup)) as db:
        for row, result, expected in pending:
            row["binding"] = verify_binding(db, session, snapshot, result, expected, manifest)
        if legacy is not None:
            report["legacyBaseline"]["binding"] = verify_binding(db, session, snapshot, legacy, "continue_with_guide", manifest)
    report["backupUnchanged"] = file_digest(backup) == manifest["backupSha256"]
    report["sourceUnchanged"] = source_fingerprint() == frozen_source
    report["metrics"] = metrics(report["results"])
    report["status"] = "measured" if report["backupUnchanged"] and report["sourceUnchanged"] else "attribution_invalid"
    write_json(run_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "freeze", "execute"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--core-only", action="store_true", help="Prepare only the seven core cases with zero legacy calls")
    parser.add_argument("--source-backup", type=Path, help="Prepare from an unchanged prior measured run's SQLite backup")
    args = parser.parse_args()
    if args.mode != "prepare" and (args.core_only or args.source_backup is not None):
        parser.error("--core-only and --source-backup are only valid for prepare; execution uses the frozen manifest")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", args.run_id):
        raise SystemExit("invalid_run_id")
    run_dir = ROOT / ".ai-runs" / args.run_id
    try:
        if args.mode == "prepare":
            if not args.session_id:
                raise ValueError("session_id_required")
            result = prepare(run_dir, args.session_id, core_only=args.core_only, source_backup=args.source_backup)
            print(json.dumps({key: result[key] for key in ("status", "snapshotFingerprint", "availableActions")}, ensure_ascii=False))
        elif args.mode == "freeze":
            result = freeze(run_dir)
            print(json.dumps(result, ensure_ascii=False))
        else:
            result = execute(run_dir)
            print(json.dumps({key: result[key] for key in ("status", "configuredMode", "model", "metrics")}, ensure_ascii=False))
    except (ValueError, FileExistsError, sqlite3.DatabaseError) as error:
        # Never print arbitrary Provider error text or private database contents.
        print(json.dumps({"status": "stopped", "errorType": type(error).__name__}), file=sys.stderr)
        return 1
    return 0 if result["status"] in {"prepared_no_model_calls", "source_frozen_no_model_calls", "measured"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

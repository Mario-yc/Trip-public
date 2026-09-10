"""Bounded live entry baseline against a read-only, real empty session.

This is NOT the guide-state semantic corpus or default-active acceptance.
No writer, planner, Web or AMap executor is invoked. Never retry a run ID.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.conversation_intent_router import ConversationCapabilityResolver, ConversationIntentRouter
from src.services.deepseek_agent_provider import DeepSeekAgentProvider, CONTROLLER_LITE_MAX_OUTPUT_TOKENS


CASES = (
    ("initial", "10月1日北京一日游，参观高校和城市公园，1人中等预算，公交地铁。", "create"),
    ("guide_without_authority", "能不能按这些建议排个方案？", "unavailable"),
)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_hash() -> str:
    root = Path(__file__).resolve().parents[2]
    paths = sorted([*(root / "backend/src").rglob("*.py"), Path(__file__).resolve()])
    return digest([(str(path.relative_to(root)).replace("\\", "/"), hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    provider = DeepSeekAgentProvider()
    if not settings.deepseek_api_key:
        raise SystemExit("Live baseline unavailable: DeepSeek credential is not configured")
    db_path = sqlite_path_from_url(settings.database_url).resolve()
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        session = db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (args.session_id,)).fetchone()
        if session is None:
            raise SystemExit("Real session is missing; no authority will be fabricated")
        snapshot = ConversationCapabilityResolver(db).routing_snapshot(session)
        if snapshot.server_state.get("hasPlanningRoot") or snapshot.server_state.get("hasActiveVersion"):
            raise SystemExit("This baseline only accepts a real empty session")
    # Close the SQLite connection before any model call. Reserve output with
    # exclusive create, so accidental reruns cannot spend another call budget.
    report = {
        "schemaVersion": "semantic-entry-baseline-v1", "sourceFingerprint": source_hash(),
        "snapshotFingerprint": snapshot.fingerprint, "modelProjection": snapshot.model_projection,
        "cases": [{"id": item[0], "input": item[1], "expectedAction": item[2]} for item in CASES],
        "configuredMode": settings.agent_intent_routing_mode, "evaluationModes": ["legacy-only", "active-all"],
        "model": provider.model, "configuredModel": settings.deepseek_model,
        "endpointHost": urlsplit(settings.deepseek_base_url).hostname,
        "timeoutSeconds": settings.agent_controller_lite_timeout_seconds,
        "activeOutputTokenLimit": 512, "legacyOutputTokenLimit": CONTROLLER_LITE_MAX_OUTPUT_TOKENS,
        "maximumClassifierInvocations": len(CASES) * 2, "automaticRetries": 0,
        "status": "reserved", "guideStateAcceptance": "not_run", "defaultActiveAcceptance": "not_run",
        "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False))
    controller = AgentAutonomyController(provider=provider,
        lite_timeout_seconds=settings.agent_controller_lite_timeout_seconds,
        decision_timeout_seconds=settings.agent_controller_decision_timeout_seconds,
        total_budget_seconds=settings.agent_controller_total_budget_seconds)
    for mode in report["evaluationModes"]:
        observations = []
        def classify(context):
            outcome = controller.classify_conversation_intent(context)
            observations.append(outcome)
            return outcome
        router = ConversationIntentRouter(lite_classifier=classify, routing_mode=mode)
        for case_id, message, expected in CASES:
            observations.clear()
            started = time.perf_counter()
            result = router.classify(message, routing_snapshot=snapshot)
            elapsed = (time.perf_counter() - started) * 1000
            action = (result.semantic_action or {}).get("name")
            report["results"].append({"id": case_id, "mode": mode, "elapsedMs": round(elapsed, 2),
                "modelCalled": result.model_called, "modelSucceeded": result.model_succeeded,
                "source": result.source, "reasonCode": result.reason_code, "action": action,
                "intent": result.classification.intent if result.classification else None,
                "requiresClarification": result.requires_clarification,
                "directActionCorrect": result.model_succeeded and action == expected,
                "timeout": any("timeout" in str(item.error_code).lower() or "timed out" in str(item.error_code).lower() for item in observations),
                "invocationLedger": result.invocation_ledger})
    samples = [row for row in report["results"] if row["mode"] == "active-all"]
    durations = sorted(row["elapsedMs"] for row in samples)
    report["metrics"] = {"sampleCount": len(samples),
        "directActionCorrectCount": sum(row["directActionCorrect"] for row in samples),
        "structuredSuccessCount": sum(row["modelSucceeded"] for row in samples),
        "fallbackCount": sum(row["source"] == "legacy_fallback" for row in samples),
        "timeoutCount": sum(row["timeout"] for row in samples),
        "p50Ms": durations[math.ceil(len(durations) * .5) - 1],
        "p95Ms": durations[math.ceil(len(durations) * .95) - 1],
        "statistics": "nearest-rank, two empty-state inputs; insufficient for enablement thresholds"}
    report["status"] = "measured" if report["sourceFingerprint"] == source_hash() else "source_changed_invalid"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "model", "configuredMode", "timeoutSeconds", "metrics", "results")}, ensure_ascii=False))
    return 0 if report["status"] == "measured" else 1


if __name__ == "__main__":
    raise SystemExit(main())

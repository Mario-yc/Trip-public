"""A single frozen semantic pass. Real transport; read-only DB; no executor."""

from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
RUN = None
sys.path.insert(0, str(ROOT / "backend"))
from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.services.agent_service import AgentService
from src.services.conversation_action_catalog import ConversationActionCatalog, parse_action_response
from src.services.conversation_intent_router import ConversationIntentRouter
from src.services.deepseek_agent_provider import DeepSeekAgentProvider
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_fingerprint():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True
    ).splitlines()
    paths = sorted(
        {
            n
            for n in names
            if n.startswith(("backend/src/", "frontend/src/")) and Path(n).suffix in {".py", ".ts", ".tsx", ".css"}
        }
    )
    return digest([(p, hashlib.sha256((ROOT / p).read_bytes()).hexdigest()) for p in paths])


def read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


class ProviderCaseBindings:
    """Bind before worker submission; a late transport cannot inherit the next case."""

    def __init__(self):
        self._bindings = {}

    def bind(self, provider, case_id):
        if provider in self._bindings and self._bindings[provider] != case_id:
            raise ValueError("evaluation_provider_reused_across_cases")
        self._bindings[provider] = case_id

    def case_for(self, provider):
        # Strong object keys also prevent id reuse after an earlier case exits.
        return self._bindings[provider]


def semantic_action(result):
    action = (result.semantic_action or {}).get("name")
    if action:
        return action
    if result.requires_clarification:
        return "clarify"
    if result.classification and result.classification.intent == "continue_plan_expansion":
        return "continue_with_guide" if result.continuation_mode == "guide_grounded" else "continue_directions"
    if result.classification and result.classification.intent == "inspect_or_explain":
        return "explain"
    return result.classification.intent if result.classification else None


def main():
    global RUN
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=["baseline", "optimized", "protocol_single"], required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    RUN = args.run_dir.resolve()
    inputs = json.loads((RUN / "inputs.json").read_text(encoding="utf-8"))
    expected = {r["id"]: r for r in json.loads((RUN / "expectations.json").read_text(encoding="utf-8"))}
    states = json.loads((RUN / "states.json").read_text(encoding="utf-8"))
    assert len(inputs) == 8 and len(expected) == 8
    maximum_calls = 8
    if args.phase == "protocol_single":
        inputs = [case for case in inputs if case["id"] == "readonly_negative"]
        maximum_calls = 1
    settings = get_settings()
    assert settings.agent_intent_routing_mode == "active-all"
    assert settings.agent_controller_lite_timeout_seconds == 2.5
    assert settings.deepseek_api_key
    report = {
        "schemaVersion": "entry-latency-eight-v1",
        "phase": args.phase,
        "status": "reserved",
        "sourceFingerprint": source_fingerprint(),
        "inputFingerprint": digest(inputs),
        "expectedFingerprint": digest(expected),
        "model": "deepseek-v4-flash",
        "configuredModel": settings.deepseek_model,
        "actualMode": settings.agent_intent_routing_mode,
        "timeoutSeconds": settings.agent_controller_lite_timeout_seconds,
        "maximumNativeCalls": maximum_calls,
        "automaticRetries": 0,
        "startedAt": time.time(),
        "results": [],
        "domainExecutorsInvoked": False,
    }
    output = RUN / (args.phase + ".json")
    source_hashes = {key: hashlib.sha256(Path(state["path"]).read_bytes()).hexdigest() for key, state in states.items()}
    report["stateFileHashes"] = source_hashes
    if not args.preflight:
        with output.open("x", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
    lock = threading.Lock()
    calls = []
    completions = []
    original = DeepSeekAgentProvider._post_json
    provider_cases = ProviderCaseBindings()

    def observed(provider, payload, **kwargs):
        assert not args.preflight, "preflight_network_forbidden"
        assert payload.get("tools") and payload.get("max_tokens") == 512, "non_semantic_dispatch_forbidden"
        with lock:
            assert len(calls) < maximum_calls, "call_cap_exceeded"
            item = {
                "caseId": provider_cases.case_for(provider),
                "requestBytes": len(json.dumps(payload, ensure_ascii=False).encode()),
                "toolsBytes": len(json.dumps(payload.get("tools"), ensure_ascii=False).encode()),
                "model": payload.get("model"),
                "maxTokens": payload.get("max_tokens"),
                "toolChoice": payload.get("tool_choice"),
                "toolNames": [t["function"]["name"] for t in payload["tools"]],
            }
            calls.append(item)
            done = threading.Event()
            completions.append(done)
        begin = time.perf_counter()
        try:
            body = original(provider, payload, **kwargs)
            message = (body.get("choices") or [{}])[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []
            item.update(
                transportCompleted=True,
                finishReason=(body.get("choices") or [{}])[0].get("finish_reason"),
                usage=body.get("usage"),
                toolCallCount=len(tool_calls),
                rawAction=(tool_calls[0].get("function") or {}).get("name") if tool_calls else None,
                argumentsChars=sum(len((t.get("function") or {}).get("arguments") or "") for t in tool_calls),
                argumentsBytes=sum(
                    len(((t.get("function") or {}).get("arguments") or "").encode()) for t in tool_calls
                ),
            )
            return body
        except Exception as error:
            item.update(transportCompleted=False, errorType=type(error).__name__)
            raise
        finally:
            item["transportMs"] = round((time.perf_counter() - begin) * 1000, 2)
            done.set()

    DeepSeekAgentProvider._post_json = observed
    for case in inputs:
        state = states[case["state"]]
        with closing(read_only(state["path"])) as db:
            session = db.execute("SELECT * FROM conversation_sessions WHERE id=?", (state["sessionId"],)).fetchone()
            assert session is not None
            service = AgentService(db)
            captured = []
            outcomes = []
            classify = service.conversation_intent_router.classify
            native = service.conversation_intent_router.lite_classifier

            def capture(message, **kwargs):
                captured.append(kwargs["routing_snapshot"])
                if args.preflight:

                    class PreflightDone(Exception):
                        pass

                    raise PreflightDone("preflight_done")
                return classify(message, **kwargs)

            def capture_native(context, *, case_id=case["id"], controller=service.autonomy_controller):
                assert not db.in_transaction, "model_read_transaction_open"
                with lock:
                    provider_cases.bind(controller.provider, case_id)
                outcome = native(context)
                outcomes.append(outcome)
                return outcome

            service.conversation_intent_router.classify = capture
            service.conversation_intent_router.lite_classifier = capture_native
            begin = time.perf_counter()
            try:
                payload, result, capability = service._route_conversation_turn(
                    session=session,
                    content=case["message"],
                    payload=AgentMessageRequest(content=case["message"], agentModel="deepseek-v4-flash", context={}),
                )
            except Exception as error:
                if args.preflight and str(error) == "preflight_done":
                    snapshot = captured[-1]
                    print(
                        json.dumps(
                            {
                                "id": case["id"],
                                "snapshotFingerprint": snapshot.fingerprint,
                                "tools": sorted(ConversationActionCatalog(snapshot).names),
                                "changes": db.total_changes,
                            },
                            ensure_ascii=False,
                        )
                    )
                    continue
                raise
            elapsed = (time.perf_counter() - begin) * 1000
            snapshot = captured[-1]
            snapshot_path = RUN / (args.phase + "-" + case["id"] + "-snapshot.json")
            snapshot_path.write_text(
                json.dumps(
                    {
                        "fingerprint": snapshot.fingerprint,
                        "model": snapshot.model_projection,
                        "server": snapshot.server_state,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            expectation = expected[case["id"]]
            structured = False
            raw_action = None
            try:
                raw_action = parse_action_response(outcomes[-1].value, ConversationActionCatalog(snapshot))[0].name
                structured = True
            except (IndexError, ValueError):
                pass
            action = semantic_action(result)
            bound = capability.to_context()
            shared = (result.semantic_action or {}).get("sharedSourceBinding")
            guide_ok = (result.continuation_mode == "guide_grounded" and capability.status == "unique") or bool(shared)
            if result.continuation_mode == "guide_grounded" and capability.status == "unique":
                match = next(
                    x
                    for x in snapshot.server_state["capabilityMatches"]["continue_plan_expansion"]
                    if all(x[k] == v for k, v in capability.selected_choice_request.items())
                )
                requirement = GuideContinuationRequirementService(db).build(
                    session_id=session["id"], selected_choice=match, active_version_id=session["active_version_id"]
                )
                guide_ok = bool(requirement.get("evidenceFingerprint"))
            row = {
                "id": case["id"],
                "state": case["state"],
                "snapshotFingerprint": snapshot.fingerprint,
                "elapsedMs": round(elapsed, 2),
                "rawAction": raw_action,
                "finalAction": action,
                "structuredValid": structured,
                "modelCalled": result.model_called,
                "modelSucceeded": result.model_succeeded,
                "reasonCode": result.reason_code,
                "source": result.source,
                "fallback": result.source == "legacy_fallback",
                "timeout": any(
                    "timeout" in str(o.error_code).lower()
                    or any(p.get("captureState") == "controller_deadline_snapshot" for p in o.performance_evidence)
                    for o in outcomes
                ),
                "directCorrect": bool(
                    result.model_called
                    and result.model_succeeded
                    and structured
                    and raw_action == expectation["action"]
                ),
                "finalCorrect": action == expectation["action"],
                "guideRequired": expectation.get("guideRequired", False),
                "guidePreserved": guide_ok,
                "boundRoot": bound.get("planningSelectionRootTurnId"),
                "expectedRoot": expectation.get("root"),
                "binding": bound,
                "performance": list(result.model_performance_evidence),
                "ledger": result.invocation_ledger,
            }
            row["wrongRoot"] = bool(
                row["boundRoot"] != row["expectedRoot"]
                and action in ("continue_with_guide", "continue_directions", "create_from_shared_guide", "create")
            )
            row["guideLost"] = bool(
                expectation.get("guideRequired")
                and action in ("continue_with_guide", "continue_directions", "create_from_shared_guide", "create")
                and not guide_ok
            )
            report["results"].append(row)
            report["calls"] = calls
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in (
                            "id",
                            "elapsedMs",
                            "rawAction",
                            "finalAction",
                            "reasonCode",
                            "timeout",
                            "directCorrect",
                            "finalCorrect",
                            "wrongRoot",
                            "guideLost",
                        )
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        db.close()
    if args.preflight:
        return 0
    deadline = time.perf_counter() + 5
    for done in completions:
        done.wait(max(0, deadline - time.perf_counter()))
    report["transportPendingAtDrain"] = sum(not done.is_set() for done in completions)
    assert source_hashes == {
        key: hashlib.sha256(Path(state["path"]).read_bytes()).hexdigest() for key, state in states.items()
    }, "source_db_changed"
    rows = report["results"]
    durations = sorted(r["elapsedMs"] for r in rows)
    models = [r for r in rows if r["modelCalled"]]
    report["metrics"] = {
        "samples": len(rows),
        "modelCalls": len(models),
        "directCorrect": sum(r["directCorrect"] for r in models),
        "structuredValid": sum(r["structuredValid"] for r in models),
        "timeouts": sum(r["timeout"] for r in rows),
        "fallbacks": sum(r["fallback"] for r in rows),
        "finalCorrect": sum(r["finalCorrect"] for r in rows),
        "legalWrongActions": sum(r["structuredValid"] and not r["directCorrect"] for r in rows),
        "wrongRoots": sum(r["wrongRoot"] for r in rows),
        "guideLost": sum(r["guideLost"] for r in rows),
        "p50Ms": durations[math.ceil(len(rows) * 0.5) - 1],
        "p95Ms": durations[math.ceil(len(rows) * 0.95) - 1],
        "statistics": f"nearest-rank over all entry cases; n={len(rows)}, not a population percentile estimate",
    }
    assert source_fingerprint() == report["sourceFingerprint"], "source_changed_during_measurement"
    report.update(status="measured", finishedAt=time.time(), calls=calls)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

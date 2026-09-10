from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
for path in (PROJECT_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from replay import replay_trace  # type: ignore
except ModuleNotFoundError:
    from backend.evals.replay import replay_trace  # type: ignore


def replay_eval_output(payload: dict[str, Any]) -> dict[str, Any]:
    cases = payload.get("cases") or []
    replayed = []
    for case in cases:
        trace_payload = case.get("traceReplay") or {}
        events = trace_payload.get("events") or case.get("planningSteps") or []
        if events:
            report = replay_trace(
                events,
                trace_payload.get("expectedStages"),
                trace_payload.get("allowedFailedStages"),
            )
        else:
            report = trace_payload
        replayed.append(
            {
                "id": case.get("id"),
                "passed": bool(report.get("passed")),
                "missingStages": report.get("missingStages") or [],
                "outOfOrder": report.get("outOfOrder") or [],
                "failedStages": report.get("failedStages") or [],
            }
        )
    failed = [item for item in replayed if not item["passed"]]
    return {
        "total": len(replayed),
        "passed": len(replayed) - len(failed),
        "failed": len(failed),
        "failures": failed,
        "cases": replayed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay Agent Harness trace reports from offline eval JSON.")
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = replay_eval_output(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

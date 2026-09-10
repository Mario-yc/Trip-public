"""Read-only upgrade validation of real, persisted proposal time requirements.

Never repairs a database or calls a model/map/writer. Not a generation E2E.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from src.services.agent_service import AgentService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as reserved:
        reserved.write('{"status":"reserved"}')
    root = Path(__file__).resolve().parents[2]
    sources = [root / "backend/src/services" / name for name in (
        "agent_service.py", "goal_ledger_service.py", "simple_open_dynamic_schedule_service.py",
        "simple_open_direction_service.py",
    )]
    source_fingerprints = {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sources}
    report = {"schemaVersion": "original-schedule-guard-eval-v1", "sessionId": args.session_id,
              "sourceFingerprints": source_fingerprints, "status": "running", "results": []}
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        service = object.__new__(AgentService)
        service.db = db
        before = db.total_changes
        portfolios = db.execute("SELECT * FROM agent_plan_portfolios WHERE session_id = ?", (args.session_id,)).fetchall()
        if not portfolios:
            raise SystemExit("No persisted portfolio; do not fabricate old state")
        for row in portfolios:
            portfolio = dict(row)
            portfolio["summary"] = json.loads(portfolio["summary_json"])
            result = {"kind": "continuation_preflight", "portfolioId": portfolio["id"]}
            try:
                service._assert_frozen_original_time_requirements(session_id=args.session_id, portfolio=portfolio)
                result["status"] = "allowed"
            except HTTPException as error:
                result.update(status="rejected", detail=error.detail)
            report["results"].append(result)
            proposals = db.execute("SELECT id, snapshot_json FROM agent_plan_proposals WHERE portfolio_id = ?", (row["id"],)).fetchall()
            for proposal in proposals:
                snapshot = json.loads(proposal["snapshot_json"])
                immutable_before = json.dumps(snapshot, sort_keys=True)
                result = {"kind": "proposal_time_preflight", "proposalId": proposal["id"]}
                try:
                    service._assert_proposal_original_time_requirements(session_id=args.session_id, portfolio=portfolio, snapshot=snapshot)
                    result["status"] = "allowed"
                except HTTPException as error:
                    result.update(status="rejected", detail=error.detail)
                assert json.dumps(snapshot, sort_keys=True) == immutable_before
                report["results"].append(result)
        report["databaseChangeCount"] = db.total_changes - before
        report["providerExecution"] = "not_entered_read_only_preflight"
        report["modelCalled"] = False
        report["generationAcceptance"] = "not_run"
        assert report["databaseChangeCount"] == 0
    report["status"] = "validated"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "databaseChangeCount": report["databaseChangeCount"],
                      "results": [{"kind": row["kind"], "status": row["status"], "code": row.get("detail", {}).get("code")}
                                  for row in report["results"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

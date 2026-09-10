from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross-check a live Portfolio journey against its isolated SQLite database.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--journey-result", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    args = parse_args()
    journey = json.loads(args.journey_result.read_text(encoding="utf-8"))
    database = args.database.resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    failures: list[str] = []

    session_id = str(journey.get("sessionId") or "")
    root_portfolio_id = str(journey.get("rootPortfolioId") or "")
    selected_proposal_id = str(journey.get("selectedProposalId") or "")
    post_version_id = str(journey.get("postAdoptionVersionId") or "")
    formal_proposal_ids = {str(item) for item in journey.get("formalProposalIds") or [] if str(item)}

    session = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
    check(session is not None, "session_not_found", failures)
    if session is None:
        report = {"passed": False, "failures": failures, "sessionId": session_id}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    check(str(session["active_version_id"] or "") == post_version_id, "ui_db_active_version_mismatch", failures)
    portfolio = connection.execute("SELECT * FROM agent_plan_portfolios WHERE id = ?", (root_portfolio_id,)).fetchone()
    check(portfolio is not None, "root_portfolio_not_found", failures)
    if portfolio is not None:
        check(str(portfolio["session_id"] or "") == session_id, "portfolio_session_mismatch", failures)
        check(str(portfolio["status"] or "") == "committed", "portfolio_not_committed", failures)
        check(str(portfolio["selected_proposal_id"] or "") == selected_proposal_id, "selected_proposal_mismatch", failures)

    proposal_rows = connection.execute(
        "SELECT * FROM agent_plan_proposals WHERE portfolio_id = ? ORDER BY rank_index", (root_portfolio_id,)
    ).fetchall()
    proposal_by_id = {str(row["id"]): row for row in proposal_rows}
    check(selected_proposal_id in proposal_by_id, "selected_proposal_row_missing", failures)
    for proposal_id in formal_proposal_ids:
        row = proposal_by_id.get(proposal_id)
        check(row is not None, f"formal_proposal_missing:{proposal_id}", failures)
        if row is None:
            continue
        verifier = json_object(row["verifier_json"])
        score = json_object(row["score_json"])
        check(verifier.get("passed") is True, f"proposal_verifier_failed:{proposal_id}", failures)
        check(score.get("hardConstraintPassed") is True, f"proposal_hard_constraint_failed:{proposal_id}", failures)
        check(str(row["status"] or "") in {"offered", "committed"}, f"proposal_status_invalid:{proposal_id}", failures)

    expansion_rows = connection.execute(
        "SELECT * FROM agent_choice_executions WHERE session_id = ? AND action = 'retry_model_planning' ORDER BY created_at",
        (session_id,),
    ).fetchall()
    check(bool(expansion_rows), "choice_expansion_execution_missing", failures)
    successful_expansions = 0
    expansion_evidence: list[dict[str, Any]] = []
    for row in expansion_rows:
        continuation = json_object(row["continuation_json"])
        outcome = json_object(row["outcome_json"])
        check(str(row["status"] or "") in {"succeeded", "failed_retryable"}, f"expansion_status_invalid:{row['id']}", failures)
        check(int(outcome.get("versionDelta") or 0) == 0, f"expansion_version_write:{row['id']}", failures)
        check(int(outcome.get("patchDelta") or 0) == 0, f"expansion_patch_write:{row['id']}", failures)
        check(int(outcome.get("routeWriteDelta") or 0) == 0, f"expansion_route_write:{row['id']}", failures)
        check(str(outcome.get("rootPortfolioId") or continuation.get("rootPortfolioId") or "") == root_portfolio_id, f"expansion_root_mismatch:{row['id']}", failures)
        if outcome.get("succeeded") is True:
            successful_expansions += 1
            check(int(outcome.get("proposalDelta") or 0) > 0, f"expansion_without_proposal_delta:{row['id']}", failures)
        expansion_evidence.append(
            {
                "id": row["id"],
                "status": row["status"],
                "proposalDelta": outcome.get("proposalDelta"),
                "newProposalIds": outcome.get("newProposalIds"),
                "versionDelta": outcome.get("versionDelta"),
                "patchDelta": outcome.get("patchDelta"),
                "routeWriteDelta": outcome.get("routeWriteDelta"),
            }
        )
    check(successful_expansions >= 1, "no_successful_verified_expansion", failures)

    adoption_rows = connection.execute(
        "SELECT * FROM agent_choice_executions WHERE session_id = ? AND action = 'select_plan_proposal' AND status = 'succeeded'",
        (session_id,),
    ).fetchall()
    check(len(adoption_rows) == 1, f"adoption_success_count:{len(adoption_rows)}", failures)
    adoption = adoption_rows[0] if len(adoption_rows) == 1 else None
    if adoption is not None:
        check(str(adoption["result_version_id"] or "") == post_version_id, "adoption_result_version_mismatch", failures)
        selected_row = proposal_by_id.get(selected_proposal_id)
        check(selected_row is not None and str(selected_row["choice_id"] or "") == str(adoption["choice_id"] or ""), "adoption_choice_proposal_mismatch", failures)

    version = connection.execute("SELECT * FROM itinerary_versions WHERE id = ?", (post_version_id,)).fetchone()
    check(version is not None, "adoption_version_missing", failures)
    patches = connection.execute(
        "SELECT * FROM itinerary_patches WHERE session_id = ? AND result_version_id = ?", (session_id, post_version_id)
    ).fetchall()
    check(len(patches) == 1, f"adoption_patch_count:{len(patches)}", failures)
    if adoption is not None and patches:
        check(str(patches[0]["source_turn_id"] or "") == str(adoption["execution_turn_id"] or ""), "single_writer_turn_mismatch", failures)
        check(str(patches[0]["validation_status"] or "") == "passed", "adoption_patch_not_validated", failures)

    selected_routes = connection.execute(
        "SELECT * FROM route_options WHERE plan_id = ? AND is_selected = 1 ORDER BY sort_order", (session["active_plan_id"],)
    ).fetchall()
    check(bool(selected_routes), "selected_routes_missing", failures)
    for route in selected_routes:
        check(int(route["duration_seconds"] or 0) > 0, f"route_duration_invalid:{route['id']}", failures)
        check(int(route["distance_meters"] or 0) > 0, f"route_distance_invalid:{route['id']}", failures)
        check("amap" in f"{route['provider']} {route['source']}".lower(), f"route_provider_not_amap:{route['id']}", failures)

    snapshot = json_object(version["snapshot_json"]) if version is not None else {}
    anchor_actuals = json_object(snapshot.get("portfolioDayAnchorActuals"))
    expected_route_pairs = sum(max(int(value or 0) - 1, 0) for value in anchor_actuals.values())
    route_evidence = snapshot.get("portfolioRouteEvidence") if isinstance(snapshot.get("portfolioRouteEvidence"), list) else []
    check(expected_route_pairs > 0, "selected_snapshot_anchor_coverage_missing", failures)
    check(len(route_evidence) == expected_route_pairs, f"snapshot_route_coverage:{len(route_evidence)}/{expected_route_pairs}", failures)

    report = {
        "passed": not failures,
        "failures": failures,
        "session": {
            "id": session_id,
            "activePlanId": session["active_plan_id"],
            "activeVersionId": session["active_version_id"],
        },
        "portfolio": {
            "id": root_portfolio_id,
            "status": portfolio["status"] if portfolio is not None else None,
            "selectedProposalId": portfolio["selected_proposal_id"] if portfolio is not None else None,
            "proposalCount": len(proposal_rows),
            "formalProposalIds": sorted(formal_proposal_ids),
        },
        "choiceExecutions": {
            "expansionCount": len(expansion_rows),
            "successfulExpansionCount": successful_expansions,
            "expansions": expansion_evidence,
            "successfulAdoptionCount": len(adoption_rows),
        },
        "writes": {
            "adoptionVersionId": post_version_id,
            "adoptionPatchCount": len(patches),
            "selectedRouteCount": len(selected_routes),
            "expectedRoutePairCount": expected_route_pairs,
            "snapshotRouteEvidenceCount": len(route_evidence),
        },
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

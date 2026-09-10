import json
import sqlite3
from typing import Any, Optional


def _parse_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row(row: Optional[sqlite3.Row], json_fields: Optional[dict[str, Any]] = None) -> Optional[dict]:
    if row is None:
        return None
    item = dict(row)
    for field, default in (json_fields or {}).items():
        if field in item:
            item[field] = _parse_json(item[field], default)
    return item


def _rows(rows: list[sqlite3.Row], json_fields: Optional[dict[str, Any]] = None) -> list[dict]:
    return [_row(row, json_fields) or {} for row in rows]


class AgentStateExporter:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def export_session(self, session_id: Optional[str]) -> dict:
        if not session_id:
            return self._empty_snapshot()
        session = self.db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            return self._empty_snapshot(session_id=session_id)
        active_version = None
        if session["active_version_id"]:
            active_version = self.db.execute(
                "SELECT * FROM itinerary_versions WHERE id = ?",
                (session["active_version_id"],),
            ).fetchone()
        versions = self.db.execute(
            """
            SELECT * FROM itinerary_versions
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (session_id,),
        ).fetchall()
        patches = self.db.execute(
            """
            SELECT * FROM itinerary_patches
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (session_id,),
        ).fetchall()
        turns = self.db.execute(
            """
            SELECT * FROM conversation_turns
            WHERE session_id = ?
            ORDER BY turn_index ASC, created_at ASC
            """,
            (session_id,),
        ).fetchall()
        choice_executions = self.db.execute(
            """
            SELECT * FROM agent_choice_executions
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        mutation_transactions = self.db.execute(
            """
            SELECT * FROM timeline_mutation_transactions
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        planning_runs = self.db.execute(
            """
            SELECT * FROM planning_runs
            WHERE itinerary_plan_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (session["active_plan_id"],),
        ).fetchall()
        pending = self.db.execute(
            """
            SELECT * FROM amap_poi_candidates
            WHERE session_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        portfolios = self.db.execute(
            """SELECT * FROM agent_plan_portfolios
            WHERE session_id = ? ORDER BY created_at DESC LIMIT 8""",
            (session_id,),
        ).fetchall()
        portfolio_ids = [str(row["id"]) for row in portfolios]
        proposals = []
        if portfolio_ids:
            placeholders = ",".join("?" for _ in portfolio_ids)
            proposals = self.db.execute(
                f"""SELECT * FROM agent_plan_proposals WHERE portfolio_id IN ({placeholders})
                ORDER BY created_at ASC, rank_index ASC""",
                tuple(portfolio_ids),
            ).fetchall()
        if not planning_runs and turns:
            latest_user_input = next((turn["content"] for turn in reversed(turns) if turn["role"] == "user"), "")
            if latest_user_input:
                planning_runs = self.db.execute(
                    """
                    SELECT * FROM planning_runs
                    WHERE user_input = ?
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (latest_user_input,),
                ).fetchall()
        return {
            "conversation_session": _row(session),
            "conversation_turns": _rows(
                turns,
                {
                    "agent_request_json": {},
                    "agent_response_json": {},
                    "error_json": {},
                },
            ),
            "agent_choice_executions": _rows(choice_executions, {"error_json": {}}),
            "timeline_mutation_transactions": _rows(mutation_transactions, {"transaction_json": {}}),
            "active_version": _row(active_version, {"snapshot_json": {}}),
            "itinerary_versions": _rows(versions, {"snapshot_json": {}}),
            "itinerary_patches": _rows(
                patches,
                {
                    "operations_json": [],
                    "validation_errors_json": [],
                },
            ),
            "planning_runs": _rows(
                planning_runs,
                {
                    "understood_requirements_json": {},
                    "constraint_summary_json": [],
                    "tool_calls_json": [],
                    "source_assessments_json": [],
                    "feasibility_report_json": None,
                },
            ),
            "pending_poi_candidates": _rows(pending, {"candidates_json": []}),
            "plan_portfolios": _rows(portfolios, {"summary_json": {}}),
            "plan_proposals": _rows(
                proposals,
                {
                    "brief_json": {}, "snapshot_json": {}, "score_json": {},
                    "verifier_json": {}, "evidence_json": {}, "generation_lineage_json": {},
                },
            ),
            "active_itinerary_snapshot": _parse_json(active_version["snapshot_json"], {})
            if active_version is not None
            else None,
        }

    def export_state(self, session_id: str) -> dict:
        snapshot = self.export_session(session_id)
        return {
            "schemaVersion": "trip-ai-runtime-state-v1",
            "readOnly": True,
            "sessionId": session_id,
            "session": snapshot["conversation_session"],
            "turns": snapshot["conversation_turns"],
            "choice_executions": snapshot["agent_choice_executions"],
            "timeline_mutation_transactions": snapshot["timeline_mutation_transactions"],
            "versions": snapshot["itinerary_versions"],
            "patches": snapshot["itinerary_patches"],
            "planning_runs": snapshot["planning_runs"],
            "pending_poi_candidates": snapshot["pending_poi_candidates"],
            "plan_portfolios": snapshot["plan_portfolios"],
            "plan_proposals": snapshot["plan_proposals"],
            "active_itinerary_snapshot": snapshot["active_itinerary_snapshot"],
        }

    def inspect_session(self, session_id: str) -> dict:
        snapshot = self.export_session(session_id)
        session = snapshot.get("conversation_session") or {}
        return {
            "readOnly": True,
            "sessionId": session_id,
            "session": session,
            "turns": snapshot["conversation_turns"],
            "choiceExecutions": snapshot["agent_choice_executions"],
            "activePlanId": session.get("active_plan_id"),
            "activeVersionId": session.get("active_version_id"),
            "pendingPoiCandidates": snapshot["pending_poi_candidates"],
            "planPortfolios": snapshot["plan_portfolios"],
            "latestPlanningRun": snapshot["planning_runs"][0] if snapshot["planning_runs"] else None,
            "latestVersions": snapshot["itinerary_versions"],
            "latestPatches": snapshot["itinerary_patches"],
        }

    def _empty_snapshot(self, session_id: Optional[str] = None) -> dict:
        return {
            "conversation_session": {"id": session_id} if session_id else None,
            "conversation_turns": [],
            "agent_choice_executions": [],
            "timeline_mutation_transactions": [],
            "active_version": None,
            "itinerary_versions": [],
            "itinerary_patches": [],
            "planning_runs": [],
            "pending_poi_candidates": [],
            "plan_portfolios": [],
            "plan_proposals": [],
            "active_itinerary_snapshot": None,
        }

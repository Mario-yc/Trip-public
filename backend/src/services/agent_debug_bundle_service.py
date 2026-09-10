"""Read-only, lossless, normalized debug export for one Agent session."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import sqlite3
from typing import Any

from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer


class AgentDebugBundleService:
    SCHEMA_VERSION = "trip-debug-bundle-v4"
    _JSON_FIELDS = re.compile(r"(?:_json|_payload)$")
    _SECRET_KEY = re.compile(
        r"(api.?key|authorization|cookie|(?:access|refresh|id)?token$|secret|securityJsCode|"
        r"credential|password|manualValue|^sig(?:nature)?$|"
        r"(?:^|[-_])(?:request|auth|credential)[-_]sig(?:nature)?$|x.?amz.?(?:credential|signature)|"
        r"^reasoning$|reasoning_content|reasoningText|chainOfThought)",
        re.I,
    )
    _BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
    _QUERY_SECRET = re.compile(
        r"(?i)([?&#](?:api[_-]?key|(?:access|refresh|id)[_-]?token|token|key|auth(?:orization)?|"
        r"cookie|client[_-]?secret|secret|sig(?:nature)?|x-amz-(?:credential|signature)|"
        r"securityJsCode)=)[^&#\s]+"
    )
    _WINDOWS_PATH = re.compile(r"(?i)(?<![A-Z0-9_])[A-Z]:[\\/]+(?:[^\\/\s\"'<>|]+[\\/]+)*[^\\/\s\"'<>|]+")
    _NETWORK_PATH = re.compile(
        r"(?i)(?<![:A-Z0-9_])(?:\\\\+[^\\\s\"'<>|]+\\+[^\\\s\"'<>|]+(?:\\+[^\\\s\"'<>|]+)*|"
        r"//+[^/\s\"'<>]+/[^/\s\"'<>]+(?:/[^/\s\"'<>]+)*)"
    )
    _UNIX_PATH = re.compile(
        r"(?<![:/A-Za-z0-9_\u4e00-\u9fff])/(?!/)(?:(?:home|Users|tmp|var|private|opt|srv|mnt|Volumes|"
        r"workspace|root)(?:/[^/\s\"'<>]+)+|"
        r"[^/\s\"'<>]+(?:/[^/\s\"'<>]+){2,})"
    )

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def export(self, *, session_id: str) -> dict[str, Any]:
        session = self._one("conversation_sessions", "id = ?", (session_id,))
        if session is None:
            raise ValueError("agent_session_not_found")
        turns = self._rows(
            "conversation_turns",
            "session_id = ?",
            (session_id,),
            order_by="turn_index, created_at, id",
        )
        planning_run_ids = [str(item.get("planning_run_id") or "") for item in turns if item.get("planning_run_id")]
        planning_runs = self._rows_in("planning_runs", "id", planning_run_ids, order_by="created_at, id")
        choices = self._rows("agent_choice_executions", "session_id = ?", (session_id,), order_by="created_at, id")
        portfolios = self._rows("agent_plan_portfolios", "session_id = ?", (session_id,), order_by="created_at, id")
        portfolio_ids = [str(item.get("id") or "") for item in portfolios if item.get("id")]
        proposals = self._normalize_proposal_routes(self._rows_in(
            "agent_plan_proposals", "portfolio_id", portfolio_ids, order_by="rank_index, created_at, id"
        ))

        plan_id = str(session.get("active_plan_id") or "")
        versions = self._rows("itinerary_versions", "session_id = ?", (session_id,), order_by="created_at, id")
        patches = self._rows("itinerary_patches", "session_id = ?", (session_id,), order_by="created_at, id")
        plan_ids = sorted(
            {str(item.get("plan_id") or "") for item in versions if item.get("plan_id")}
            | ({plan_id} if plan_id else set())
        )
        route_options = self._rows_in("route_options", "plan_id", plan_ids, order_by="queried_at, id")
        pending = self._rows("amap_poi_candidates", "session_id = ?", (session_id,), order_by="created_at, id")
        mutations = self._rows(
            "timeline_mutation_transactions", "session_id = ?", (session_id,), order_by="created_at, mutation_id"
        )

        event_store: dict[str, Any] = {}
        turn_refs: list[dict[str, Any]] = []
        normalized_turns: list[dict[str, Any]] = []
        request_contexts: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for raw_turn in turns:
            turn = dict(raw_turn)
            request = turn.pop("agent_request_json", None)
            response = turn.pop("agent_response_json", None)
            error = turn.pop("error_json", None)
            request_value = self._json_value(request)
            response_value = self._json_value(response)
            error_value = self._json_value(error)
            event_ids: list[str] = []
            for event in self._events(response_value):
                event_id = self._event_id(event)
                event_store.setdefault(event_id, self._redact(event))
                event_ids.append(event_id)
            turn_id = str(turn.get("id") or "")
            normalized_turns.append(self._redact(turn))
            turn_refs.append({"turnId": turn_id, "eventIds": list(dict.fromkeys(event_ids))})
            if request_value is not None:
                request_contexts.append({"turnId": turn_id, "value": self._redact(request_value)})
            if response_value is not None:
                responses.append(
                    {
                        "turnId": turn_id,
                        "value": self._redact(self._replace_event_lists(response_value)),
                    }
                )
            if error_value is not None:
                errors.append({"turnId": turn_id, "value": self._redact(error_value)})
        for run in planning_runs:
            for event in self._events(run):
                event_id = self._event_id(event)
                event_store.setdefault(event_id, self._redact(event))

        normalized_planning_runs = [self._redact(self._replace_event_lists(run)) for run in planning_runs]
        active_snapshot = None
        active_version_id = str(session.get("active_version_id") or "")
        for version in versions:
            if str(version.get("id") or "") == active_version_id:
                active_snapshot = version.get("snapshot_json")
                break
        sections: dict[str, Any] = {
            "META": {
                "schemaVersion": self.SCHEMA_VERSION,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "sessionId": session_id,
                "planningRootIds": sorted(
                    {
                        str(item.get("source_user_turn_id") or "")
                        for item in portfolios
                        if item.get("source_user_turn_id")
                    }
                ),
                "planningRootId": str(portfolios[-1].get("source_user_turn_id") or "") if portfolios else None,
            },
            "CONVERSATION_TURNS": normalized_turns,
            "EVENT_STORE": event_store,
            "TURN_EVENT_REFERENCES": turn_refs,
            "AGENT_REQUEST_CONTEXTS": request_contexts,
            "AGENT_RESPONSES": responses,
            "ERRORS": errors,
            "CONTROLLER_RUNS": normalized_planning_runs,
            "PLANNING_RUNS": normalized_planning_runs,
            "PLANNING_TRACE": {"eventIds": list(event_store), "eventStoreRef": "EVENT_STORE"},
            "STRUCTURED_CHOICES": self._structured_choices(responses),
            "CHOICE_EXECUTIONS": self._redact(choices),
            "EXPLORATION_FRONTIERS": self._frontiers(portfolios),
            "PORTFOLIOS": self._redact(portfolios),
            "PROPOSALS": self._redact(proposals),
            "PROPOSAL_SCORES": self._referenced_json(proposals, "score_json"),
            "PROPOSAL_VERIFIERS": self._referenced_json(proposals, "verifier_json"),
            "PROPOSAL_LINEAGE": self._referenced_json(proposals, "generation_lineage_json"),
            "COMPARISON_STATE": {"status": "server_persisted", "proposalIds": [item.get("id") for item in proposals]},
            "MAP_STATE": {"status": "unavailable", "reason": "frontend_only_state"},
            "ACTIVE_ITINERARY": self._redact(self._normalize_snapshot_routes(self._json_value(active_snapshot))),
            "ITINERARY_VERSIONS": self._redact(versions),
            "PATCHES": self._redact(patches),
            "ROUTE_EVIDENCE": self._redact(route_options),
            "PENDING_SLOTS": self._redact(pending),
            "ONLINE_ENRICHMENT": {"status": "unavailable", "reason": "not_persisted_as_session_section"},
            "PROVIDER_STATUS": {"status": "unavailable", "reason": "credentials_intentionally_excluded"},
            "FRONTEND_UI_STATE": {"status": "unavailable", "reason": "frontend_merge_required"},
            "TIMELINE_MUTATION_TRANSACTIONS": self._redact(mutations),
        }
        expected = {
            "conversationTurnCount": len(turns),
            "eventCount": len(event_store),
            "proposalCount": len(proposals),
            "choiceExecutionCount": len(choices),
            "versionCount": len(versions),
        }
        sections["COMPLETENESS"] = {
            "truncated": False,
            "captureSource": "server_authoritative",
            "expected": expected,
            "exported": dict(expected),
            "sections": {
                name: {
                    "status": value.get("status")
                    if isinstance(value, dict) and value.get("status") in {"unavailable", "redacted"}
                    else "complete"
                }
                for name, value in sections.items()
            },
        }
        sections["COMPLETENESS"]["sections"]["COMPLETENESS"] = {"status": "complete"}
        return self._redact({"schemaVersion": self.SCHEMA_VERSION, "sections": sections})

    def _rows(self, table: str, where: str, params: tuple[Any, ...], *, order_by: str = "id") -> list[dict[str, Any]]:
        if not self._table_exists(table):
            return []
        rows = self.db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {order_by}", params).fetchall()
        return [self._normalize_row(row) for row in rows]

    def _rows_in(self, table: str, column: str, values: list[str], *, order_by: str) -> list[dict[str, Any]]:
        if not values or not self._table_exists(table):
            return []
        placeholders = ",".join("?" for _ in values)
        return [
            self._normalize_row(row)
            for row in self.db.execute(
                f"SELECT * FROM {table} WHERE {column} IN ({placeholders}) ORDER BY {order_by}", tuple(values)
            ).fetchall()
        ]

    def _one(self, table: str, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._rows(table, where, params)
        return rows[0] if rows else None

    def _table_exists(self, table: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            is not None
        )

    def _normalize_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: self._json_value(value) if self._JSON_FIELDS.search(key) else value for key, value in dict(row).items()
        }

    @staticmethod
    def _json_value(value: Any) -> Any:
        if value is None or isinstance(value, (dict, list, int, float, bool)):
            return value
        try:
            return json.loads(str(value))
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _events(value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def walk(item: Any, key: str = "") -> None:
            if isinstance(item, dict):
                for child_key, child in item.items():
                    if child_key in {"planningSteps", "toolEvents", "events", "executionEvents"} and isinstance(
                        child, list
                    ):
                        found.extend(event for event in child if isinstance(event, dict))
                    else:
                        walk(child, child_key)
            elif isinstance(item, list):
                for child in item:
                    walk(child, key)

        walk(value)
        return found

    def _replace_event_lists(self, value: Any) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, child in value.items():
                if key in {"planningSteps", "toolEvents", "events", "executionEvents"} and isinstance(child, list):
                    normalized[key] = {
                        "eventIds": list(
                            dict.fromkeys(self._event_id(event) for event in child if isinstance(event, dict))
                        ),
                        "eventStoreRef": "EVENT_STORE",
                    }
                else:
                    normalized[key] = self._replace_event_lists(child)
            return normalized
        if isinstance(value, list):
            return [self._replace_event_lists(item) for item in value]
        return value

    @staticmethod
    def _event_id(event: dict[str, Any]) -> str:
        explicit = str(event.get("id") or event.get("eventId") or "").strip()
        if explicit:
            return explicit
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return f"event_{sha256(payload.encode('utf-8')).hexdigest()[:24]}"

    def _redact(self, value: Any, key: str = "") -> Any:
        if self._SECRET_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(item_key): self._redact(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item, key) for item in value]
        if isinstance(value, str):
            text = self._BEARER.sub("Bearer [REDACTED]", value)
            text = self._QUERY_SECRET.sub(r"\1[REDACTED]", text)
            text = self._WINDOWS_PATH.sub("[LOCAL_PATH]", text)
            text = self._NETWORK_PATH.sub("[LOCAL_PATH]", text)
            return self._UNIX_PATH.sub("[LOCAL_PATH]", text)
        return value

    @staticmethod
    def _structured_choices(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for response in responses:
            value = response.get("value") if isinstance(response, dict) else None
            if isinstance(value, dict) and isinstance(value.get("choiceOptions"), list):
                output.append({"turnId": response.get("turnId"), "choiceOptions": value["choiceOptions"]})
        return output

    def _frontiers(self, portfolios: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for portfolio in portfolios:
            summary = portfolio.get("summary_json")
            if isinstance(summary, dict) and isinstance(summary.get("creativeExplorationFrontier"), dict):
                output.append(
                    {
                        "portfolioId": portfolio.get("id"),
                        "frontier": self._redact(summary["creativeExplorationFrontier"]),
                    }
                )
        return output

    @staticmethod
    def _normalize_snapshot_routes(snapshot: Any) -> Any:
        if not isinstance(snapshot, dict):
            return snapshot
        if not any(key in snapshot for key in ("routeOptions", "portfolioRouteEvidence", "routeEvidence")):
            return snapshot
        normalized = dict(snapshot)
        normalized["normalizedRouteEvidence"] = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
        return normalized

    @classmethod
    def _normalize_proposal_routes(cls, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_rows: list[dict[str, Any]] = []
        for proposal in proposals:
            item = dict(proposal)
            item["snapshot_json"] = cls._normalize_snapshot_routes(item.get("snapshot_json"))
            normalized_rows.append(item)
        return normalized_rows

    def _referenced_json(self, rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        return [{"proposalId": item.get("id"), "value": self._redact(item.get(field))} for item in rows]

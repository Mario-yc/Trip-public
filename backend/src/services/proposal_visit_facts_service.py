"""Expiring, read-only visit facts for immutable comparison proposals."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterator

from fastapi import HTTPException

from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.segment_visit_facts_service import SegmentVisitFactsService


_REFRESH_REGISTRY_GUARD = Lock()
_REFRESH_LOCKS: dict[str, Lock] = {}
_REFRESH_LOCK_USERS: dict[str, int] = {}


@contextmanager
def _proposal_refresh_singleflight(refresh_key: str) -> Iterator[None]:
    """Serialize one proposal/material refresh inside the serving process.

    The Trip API runs provider work in threads inside one process.  A page
    remount can therefore issue the same refresh while the first request is
    still resolving web facts.  Sharing one keyed lease prevents duplicate
    provider calls and, critically, prevents two request connections from
    competing for the same SQLite rows.
    """

    with _REFRESH_REGISTRY_GUARD:
        lease = _REFRESH_LOCKS.setdefault(refresh_key, Lock())
        _REFRESH_LOCK_USERS[refresh_key] = _REFRESH_LOCK_USERS.get(refresh_key, 0) + 1
    try:
        with lease:
            yield
    finally:
        with _REFRESH_REGISTRY_GUARD:
            remaining = _REFRESH_LOCK_USERS.get(refresh_key, 1) - 1
            if remaining <= 0:
                _REFRESH_LOCK_USERS.pop(refresh_key, None)
                _REFRESH_LOCKS.pop(refresh_key, None)
            else:
                _REFRESH_LOCK_USERS[refresh_key] = remaining


class ProposalVisitFactsService:
    MAX_UNIQUE_PLACES = 6
    MAX_WEB_QUERIES = 6

    def __init__(self, db: sqlite3.Connection, *, resolver: SegmentVisitFactsService | None = None) -> None:
        self.db = db
        self.resolver = resolver or SegmentVisitFactsService(db)

    def refresh(self, *, session_id: str, proposal_id: str) -> dict[str, Any]:
        scope = self._scope(session_id=session_id, proposal_id=proposal_id)
        snapshot = scope["snapshot"]
        material_fingerprint = PlanComparisonPreviewService.material_fingerprint(snapshot)
        refresh_key = "\n".join((session_id, proposal_id, material_fingerprint))
        with _proposal_refresh_singleflight(refresh_key):
            # Re-read authority after waiting.  Proposals are immutable, but
            # their portfolio may have been adopted while this request waited.
            current_scope = self._scope(session_id=session_id, proposal_id=proposal_id)
            current_material = PlanComparisonPreviewService.material_fingerprint(current_scope["snapshot"])
            if current_material != material_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "plan_proposal_material_changed",
                        "message": "方案内容已变化，请加载最新规划状态后重新核验。",
                    },
                )
            return self._refresh_scoped(scope=current_scope, material_fingerprint=current_material)

    def _refresh_scoped(self, *, scope: dict[str, Any], material_fingerprint: str) -> dict[str, Any]:
        proposal_id = scope["proposalId"]
        snapshot = scope["snapshot"]
        segments = self._segments(snapshot)
        unique_segments = self._prioritized_unique(segments)
        selected = unique_segments[: self.MAX_UNIQUE_PLACES]
        selected_keys = {(item["amapPoiId"], item["visitDate"]) for item in selected}
        extra_query_slots = max(0, self.MAX_WEB_QUERIES - len(selected))
        budgets_by_key = {
            (item["amapPoiId"], item["visitDate"]): 2 if index < extra_query_slots else 1
            for index, item in enumerate(selected)
        }
        now = datetime.now(timezone.utc)
        web_budget = self.MAX_WEB_QUERIES
        results: dict[str, dict[str, Any]] = {}
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        pending_persistence: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in [value for value in segments if (value["amapPoiId"], value["visitDate"]) in selected_keys]:
            key = (item["amapPoiId"], item["visitDate"])
            resolved = cache.get(key)
            if resolved is None:
                cached = self._fresh_cached(
                    proposal_id=proposal_id,
                    amap_poi_id=key[0],
                    visit_date=key[1],
                    material_fingerprint=material_fingerprint,
                    now=now,
                )
                if cached is not None:
                    resolved = cached
                else:
                    per_place_budget = min(web_budget, budgets_by_key.get(key, 1))
                    resolved = self.resolver.resolve_visit_facts(
                        amap_poi_id=key[0],
                        poi_name=item["poiName"],
                        visit_date=key[1],
                        queried_at=now,
                        web_query_budget=per_place_budget,
                    )
                    web_budget -= per_place_budget
                cache[key] = resolved
            projected = self._project_segment_fact(item, resolved)
            if not self._has_fresh_segment_row(
                proposal_id=proposal_id,
                segment_id=item["segmentId"],
                amap_poi_id=item["amapPoiId"],
                visit_date=item["visitDate"],
                material_fingerprint=material_fingerprint,
                now=now,
            ):
                pending_persistence.append((item, projected))
            results[item["segmentId"]] = projected
        omitted = max(0, len(unique_segments) - len(selected))
        conflicts = self._conflicts(results)
        statuses = {str(item.get("refreshStatus") or "") for item in results.values()}
        refresh_status = (
            "failed"
            if results and statuses == {"failed"}
            else "partial"
            if omitted or any(status in {"partial", "failed"} for status in statuses)
            else "completed"
        )
        if pending_persistence:
            latest_scope = self._scope(session_id=scope["sessionId"], proposal_id=proposal_id)
            latest_material = PlanComparisonPreviewService.material_fingerprint(latest_scope["snapshot"])
            if latest_material != material_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "plan_proposal_material_changed",
                        "message": "方案内容已变化，请加载最新规划状态后重新核验。",
                    },
                )
            self._persist_batch(
                scope=latest_scope,
                material=material_fingerprint,
                rows=pending_persistence,
            )
        evidence_times = [str(item.get("queriedAt") or "") for item in results.values() if item.get("queriedAt")]
        return {
            "proposalId": proposal_id,
            "materialFingerprint": material_fingerprint,
            "refreshStatus": refresh_status,
            "visitFactsBySegment": results,
            "verifiedScheduleConflicts": conflicts,
            "queriedAt": max(evidence_times) if evidence_times else now.isoformat(),
            "omittedSegmentCount": omitted,
        }

    def load_for_proposal(
        self,
        *,
        proposal_id: str,
        material_fingerprint: str,
    ) -> dict[str, Any]:
        rows = self.db.execute(
            """SELECT * FROM proposal_segment_visit_facts
            WHERE proposal_id = ? AND material_fingerprint = ?
            ORDER BY queried_at DESC""",
            (proposal_id, material_fingerprint),
        ).fetchall()
        now = datetime.now(timezone.utc)
        facts: dict[str, dict[str, Any]] = {}
        for row in rows:
            segment_id = str(row["proposal_segment_id"])
            if segment_id in facts:
                continue
            projected = self._row_payload(row)
            if self._parse_time(str(row["expires_at"])) <= now:
                projected["refreshStatus"] = "expired"
                opening = dict(projected.get("openingHours") or {})
                opening.update(
                    status="unknown",
                    valueText=f"未找到适用于 {row['visit_date']} 的最新可靠开放时间",
                    caveat="信息已过期，请重新核验。",
                )
                projected["openingHours"] = opening
                projected["scheduleCompatibility"] = "unknown"
            facts[segment_id] = projected
        conflicts = self._conflicts(facts)
        return {
            "refreshStatus": (
                "not_started"
                if not facts
                else "partial"
                if any(item.get("refreshStatus") in {"partial", "failed", "expired"} for item in facts.values())
                else "completed"
            ),
            "visitFactsBySegment": facts,
            "verifiedScheduleConflicts": conflicts,
        }

    def merge_projection(self, projection: dict[str, Any]) -> dict[str, Any]:
        proposal_id = str(projection.get("proposalId") or "")
        material = str(projection.get("materialFingerprint") or "")
        if not proposal_id or not material:
            return projection
        merged = copy.deepcopy(projection)
        state = self.load_for_proposal(proposal_id=proposal_id, material_fingerprint=material)
        merged.update(state)
        return merged

    def merge_choice_option(self, option: dict[str, Any]) -> dict[str, Any]:
        """Project the same persisted visit-fact overlay into nested choices.

        A comparison proposal is carried both at the turn top level and inside
        its opaque selection choice.  Both carriers must expose the same
        material-bound read model or client replay can regress a refreshed
        proposal to its original ``not_started`` snapshot.
        """

        merged = copy.deepcopy(option)
        projection = merged.get("comparisonProjection")
        if isinstance(projection, dict):
            merged["comparisonProjection"] = self.merge_projection(projection)
        return merged

    def assert_adoption_allowed(self, *, proposal_id: str) -> None:
        row = self.db.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return
        snapshot = self._json_object(row["snapshot_json"])
        material = PlanComparisonPreviewService.material_fingerprint(snapshot)
        state = self.load_for_proposal(proposal_id=proposal_id, material_fingerprint=material)
        if state["verifiedScheduleConflicts"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "proposal_opening_hours_conflict",
                    "message": "该方案的访问时间与已核验开放时间冲突，请补全或重新生成该方向。",
                    "conflicts": state["verifiedScheduleConflicts"],
                },
            )

    def copy_to_adopted_plan(self, *, proposal_id: str, plan_id: str) -> int:
        """Best-effort post-commit reuse; enrichment failure cannot undo adoption."""

        savepoint = "proposal_visit_fact_reuse"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            copied = self._copy_to_adopted_plan(proposal_id=proposal_id, plan_id=plan_id)
        except Exception:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return 0
        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        return copied

    def _copy_to_adopted_plan(self, *, proposal_id: str, plan_id: str) -> int:
        """Reuse only fresh facts whose proposal and itinerary identities still match.

        This runs after the proposal Single-Writer has committed.  It copies
        online evidence into the formal itinerary facts table without changing
        either the frozen proposal snapshot or an itinerary version.
        """

        proposal_row = self.db.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if proposal_row is None or not plan_id:
            return 0
        snapshot = self._json_object(proposal_row["snapshot_json"])
        material = PlanComparisonPreviewService.material_fingerprint(snapshot)
        now = datetime.now(timezone.utc)
        proposal_segments = {item["segmentId"]: item for item in self._segments(snapshot)}
        rows = self.db.execute(
            """SELECT * FROM proposal_segment_visit_facts
               WHERE proposal_id = ? AND material_fingerprint = ?
               ORDER BY queried_at DESC""",
            (proposal_id, material),
        ).fetchall()
        copied = 0
        seen: set[str] = set()
        for row in rows:
            segment_id = str(row["proposal_segment_id"])
            if segment_id in seen or self._parse_time(str(row["expires_at"])) <= now:
                continue
            seen.add(segment_id)
            proposal_segment = proposal_segments.get(segment_id)
            if proposal_segment is None:
                continue
            amap_poi_id = str(row["amap_poi_id"])
            visit_date = str(row["visit_date"])
            if (
                amap_poi_id != proposal_segment["amapPoiId"]
                or visit_date != proposal_segment["visitDate"]
                or not str(row["evidence_fingerprint"] or "").strip()
            ):
                continue
            adopted = self.db.execute(
                """SELECT s.id AS segment_id, p.amap_id, d.date AS visit_date
                   FROM itinerary_segments s
                   JOIN pois p ON p.id = s.poi_id
                   JOIN itinerary_days d ON d.id = s.day_id
                   WHERE s.plan_id = ? AND s.id = ?""",
                (plan_id, segment_id),
            ).fetchone()
            if (
                adopted is None
                or str(adopted["amap_id"] or "") != amap_poi_id
                or str(adopted["visit_date"] or "date_pending") != visit_date
            ):
                continue
            fact_identity = "\n".join((plan_id, segment_id, amap_poi_id, visit_date))
            fact_id = f"visitfact_{hashlib.sha256(fact_identity.encode('utf-8')).hexdigest()[:18]}"
            facts = self._json_object(row["facts_json"])
            facts.pop("_proposalSchedule", None)
            self.db.execute(
                "DELETE FROM segment_visit_facts WHERE segment_id = ? AND amap_poi_id = ? AND visit_date = ?",
                (segment_id, amap_poi_id, visit_date),
            )
            self.db.execute(
                """INSERT INTO segment_visit_facts (
                  id, plan_id, segment_id, amap_poi_id, visit_date, refresh_status,
                  facts_json, source_refs_json, evidence_fingerprint, queried_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fact_id,
                    plan_id,
                    segment_id,
                    amap_poi_id,
                    visit_date,
                    str(row["refresh_status"]),
                    json.dumps(facts, ensure_ascii=False, sort_keys=True),
                    str(row["source_refs_json"]),
                    str(row["evidence_fingerprint"]),
                    str(row["queried_at"]),
                    str(row["expires_at"]),
                ),
            )
            copied += 1
        return copied

    def _scope(self, *, session_id: str, proposal_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT p.id AS portfolio_id, p.status AS portfolio_status, p.summary_json,
                      p.request_contract_fingerprint, q.snapshot_json
               FROM agent_plan_portfolios p
               JOIN agent_plan_proposals q ON q.portfolio_id = p.id
               WHERE p.session_id = ? AND q.id = ?""",
            (session_id, proposal_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "plan_proposal_not_found", "message": "方案不存在。"})
        summary = self._json_object(row["summary_json"])
        if str(row["portfolio_status"]) != "awaiting_selection":
            raise HTTPException(
                status_code=409,
                detail={"code": "plan_portfolio_not_current", "message": "该方案已不是当前可比较方案。"},
            )
        visible = {str(value) for value in summary.get("visibleProposalIds") or []}
        if proposal_id not in visible:
            raise HTTPException(
                status_code=409, detail={"code": "plan_proposal_not_visible", "message": "该方案不属于当前可见方向。"}
            )
        return {
            "sessionId": session_id,
            "portfolioId": str(row["portfolio_id"]),
            "proposalId": proposal_id,
            "snapshot": self._json_object(row["snapshot_json"]),
        }

    @classmethod
    def _segments(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            visit_date = str(day.get("date") or "date_pending")
            for index, segment in enumerate(day.get("segments") or []):
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                amap_id = str(poi.get("amapId") or poi.get("amap_id") or "").strip()
                segment_id = str(segment.get("id") or "").strip()
                if not segment_id or not amap_id:
                    continue
                intent = str(segment.get("intentType") or segment.get("kind") or "").casefold()
                is_meal = intent in {"meal", "food", "local_food"} or any(
                    token in str(segment.get("title") or poi.get("name") or "") for token in ("午餐", "晚餐", "早餐")
                )
                optional = bool(segment.get("optional") or segment.get("isOptional"))
                result.append(
                    {
                        "segmentId": segment_id,
                        "amapPoiId": amap_id,
                        "poiName": str(poi.get("name") or segment.get("title") or amap_id),
                        "visitDate": visit_date,
                        "startTime": str(segment.get("startTime") or ""),
                        "endTime": str(segment.get("endTime") or ""),
                        "priority": 2 if optional else 1 if is_meal else 0,
                        "order": len(result) + index,
                    }
                )
        return result

    @staticmethod
    def _prioritized_unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        selected: list[dict[str, Any]] = []
        for item in sorted(items, key=lambda value: (value["priority"], value["order"])):
            key = (item["amapPoiId"], item["visitDate"])
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)
        return selected

    def _fresh_cached(
        self, *, proposal_id: str, amap_poi_id: str, visit_date: str, material_fingerprint: str, now: datetime
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT * FROM proposal_segment_visit_facts
            WHERE proposal_id = ? AND amap_poi_id = ? AND visit_date = ?
              AND material_fingerprint = ? ORDER BY queried_at DESC LIMIT 1""",
            (proposal_id, amap_poi_id, visit_date, material_fingerprint),
        ).fetchone()
        if row is None or self._parse_time(str(row["expires_at"])) <= now:
            return None
        payload = self._row_payload(row)
        return {
            "amapPoiId": amap_poi_id,
            "visitDate": visit_date,
            "refreshStatus": payload["refreshStatus"],
            "facts": payload["facts"],
            "sourceRefs": payload["sourceRefs"],
            "evidenceFingerprint": payload["evidenceFingerprint"],
            "queriedAt": payload["queriedAt"],
            "expiresAt": payload["expiresAt"],
        }

    @classmethod
    def _project_segment_fact(cls, item: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
        opening = dict((resolved.get("facts") or {}).get("openingHours") or {})
        compatibility = cls._compatibility(item, opening)
        return {
            "segmentId": item["segmentId"],
            "amapPoiId": item["amapPoiId"],
            "visitDate": item["visitDate"],
            "refreshStatus": resolved.get("refreshStatus") or "partial",
            "facts": copy.deepcopy(resolved.get("facts") or {}),
            "openingHours": opening,
            "sourceRefs": copy.deepcopy(resolved.get("sourceRefs") or []),
            "evidenceFingerprint": resolved.get("evidenceFingerprint"),
            "queriedAt": resolved.get("queriedAt"),
            "expiresAt": resolved.get("expiresAt"),
            "scheduleCompatibility": compatibility,
            "scheduledStartTime": item.get("startTime"),
            "scheduledEndTime": item.get("endTime"),
        }

    @staticmethod
    def _compatibility(item: dict[str, Any], opening: dict[str, Any]) -> str:
        if opening.get("status") != "verified" or opening.get("effectiveForDate") != item.get("visitDate"):
            return "unknown"
        text = str(opening.get("valueText") or "")
        if any(token in text for token in ("闭馆", "不开放", "暂停开放")):
            return "verified_conflict"
        intervals = (opening.get("structuredValue") or {}).get("intervals") or []
        start = str(item.get("startTime") or "")[:5]
        end = str(item.get("endTime") or "")[:5]
        if not intervals or not start or not end:
            return "verified_unknown_schedule"
        return (
            "verified_compatible"
            if any(
                str(value.get("start") or "") <= start and end <= str(value.get("end") or "")
                for value in intervals
                if isinstance(value, dict)
            )
            else "verified_conflict"
        )

    @staticmethod
    def _conflicts(facts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "segmentId": segment_id,
                "visitDate": item.get("visitDate"),
                "openingHours": item.get("openingHours"),
                "scheduledStartTime": item.get("scheduledStartTime"),
                "scheduledEndTime": item.get("scheduledEndTime"),
            }
            for segment_id, item in facts.items()
            if item.get("scheduleCompatibility") == "verified_conflict"
        ]

    def _has_fresh_segment_row(
        self,
        *,
        proposal_id: str,
        segment_id: str,
        amap_poi_id: str,
        visit_date: str,
        material_fingerprint: str,
        now: datetime,
    ) -> bool:
        row = self.db.execute(
            """SELECT expires_at FROM proposal_segment_visit_facts
               WHERE proposal_id = ? AND proposal_segment_id = ?
                 AND amap_poi_id = ? AND visit_date = ? AND material_fingerprint = ?
               LIMIT 1""",
            (proposal_id, segment_id, amap_poi_id, visit_date, material_fingerprint),
        ).fetchone()
        return row is not None and self._parse_time(str(row["expires_at"])) > now

    def _persist_batch(
        self,
        *,
        scope: dict[str, Any],
        material: str,
        rows: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        """Persist resolved facts in one short transaction after all I/O ends."""

        savepoint = "proposal_visit_facts_refresh"
        try:
            self.db.execute(f"SAVEPOINT {savepoint}")
            for item, projected in rows:
                self._persist(scope, item, material, projected)
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.db.commit()
        except sqlite3.OperationalError as exc:
            self._rollback_refresh_savepoint(savepoint)
            message = str(exc).casefold()
            if "locked" in message or "busy" in message:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "proposal_visit_facts_persist_busy",
                        "message": "开放时间核验结果暂时无法保存，请稍后重试；方案与行程均未修改。",
                    },
                ) from exc
            raise
        except Exception:
            self._rollback_refresh_savepoint(savepoint)
            raise

    def _rollback_refresh_savepoint(self, savepoint: str) -> None:
        try:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            self.db.rollback()

    def _persist(self, scope: dict[str, Any], item: dict[str, Any], material: str, projected: dict[str, Any]) -> None:
        identity = "\n".join((scope["proposalId"], item["segmentId"], item["amapPoiId"], item["visitDate"], material))
        row_id = f"proposal_visitfact_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:18]}"
        stored_facts = copy.deepcopy(projected["facts"])
        stored_facts["_proposalSchedule"] = {
            "compatibility": projected["scheduleCompatibility"],
            "scheduledStartTime": projected.get("scheduledStartTime"),
            "scheduledEndTime": projected.get("scheduledEndTime"),
        }
        self.db.execute(
            "DELETE FROM proposal_segment_visit_facts WHERE proposal_id = ? AND proposal_segment_id = ?",
            (scope["proposalId"], item["segmentId"]),
        )
        self.db.execute(
            """INSERT INTO proposal_segment_visit_facts (
              id, session_id, portfolio_id, proposal_id, proposal_segment_id,
              material_fingerprint, amap_poi_id, visit_date, refresh_status,
              facts_json, source_refs_json, evidence_fingerprint, queried_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_id,
                scope["sessionId"],
                scope["portfolioId"],
                scope["proposalId"],
                item["segmentId"],
                material,
                item["amapPoiId"],
                item["visitDate"],
                projected["refreshStatus"],
                json.dumps(stored_facts, ensure_ascii=False, sort_keys=True),
                json.dumps(projected["sourceRefs"], ensure_ascii=False, sort_keys=True),
                projected["evidenceFingerprint"],
                projected["queriedAt"],
                projected["expiresAt"],
            ),
        )

    @classmethod
    def _row_payload(cls, row: sqlite3.Row) -> dict[str, Any]:
        facts = cls._json_object(row["facts_json"])
        schedule = dict(facts.pop("_proposalSchedule", {}) or {})
        opening = dict(facts.get("openingHours") or {})
        return {
            "segmentId": str(row["proposal_segment_id"]),
            "amapPoiId": str(row["amap_poi_id"]),
            "visitDate": str(row["visit_date"]),
            "refreshStatus": str(row["refresh_status"]),
            "facts": facts,
            "openingHours": opening,
            "sourceRefs": cls._json_list(row["source_refs_json"]),
            "evidenceFingerprint": str(row["evidence_fingerprint"]),
            "queriedAt": str(row["queried_at"]),
            "expiresAt": str(row["expires_at"]),
            "scheduleCompatibility": str(schedule.get("compatibility") or "unknown"),
            "scheduledStartTime": schedule.get("scheduledStartTime"),
            "scheduledEndTime": schedule.get("scheduledEndTime"),
        }

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(str(value or "{}"))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(value: Any) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError):
            return []
        return [dict(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

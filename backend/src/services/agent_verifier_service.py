import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from types import SimpleNamespace

from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.night_view_candidate_policy import NightViewCandidatePolicy
from src.services.itinerary_schedule_service import ItineraryScheduleService
from src.services.poi_trust_policy import PoiTrustPolicy
from src.services.campus_candidate_policy import CampusCandidatePolicy
from src.services.budget_invariant_policy import BudgetInvariantPolicy
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import AMAP_ROUTE_SOURCE, ROUTE_CACHE_TTL_SECONDS


@dataclass
class AgentVerifierReport:
    passed: bool
    hard_failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def as_metadata(self) -> dict:
        return {
            "passed": self.passed,
            "hardFailures": self.hard_failures,
            "softFailures": self.soft_failures,
            "checks": self.checks,
            **self.metadata,
        }


class AgentVerifierService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.poi_trust_policy = PoiTrustPolicy()
        self.night_view_candidate_policy = NightViewCandidatePolicy()
        self.intent_candidate_semantic_policy = IntentCandidateSemanticPolicy()

    def verify_agent_write(
        self,
        session_id: str,
        plan_id: str,
        version_id: Optional[str],
        patch_id: Optional[str],
        planning_context: Optional[dict] = None,
    ) -> AgentVerifierReport:
        planning_context = planning_context or {}
        hard_failures: list[str] = []
        soft_failures: list[str] = []
        version_snapshot = self._version_snapshot(plan_id, version_id, hard_failures)
        expected_constraints = self._expected_hard_constraints(planning_context, version_snapshot)
        simple_open_non_blocking_routes = planning_context.get("simpleOpenRoutePolicyAuthorized") is True
        route_quality_check = self._check_route_quality(
            plan_id,
            soft_failures,
            hard_failures=None if simple_open_non_blocking_routes else hard_failures,
            version_snapshot=version_snapshot,
            require_matrix_proof=not simple_open_non_blocking_routes,
        )
        if bool(planning_context.get("portfolioCommit")):
            # Portfolio proposals are offered only after a route preflight. A later
            # provider failure must not be downgraded into a warning and persisted
            # as if the preflight evidence still represented the official routes.
            errored_pairs = [
                f"{row['from_segment_id']}->{row['to_segment_id']} {row['mode']}"
                for row in self._route_quality_rows(plan_id, selected_only=False)
                if row["error_json"]
            ]
            missing_pairs = route_quality_check.get("missingPairs", [])
            if errored_pairs:
                hard_failures.append(
                    "route_quality: route_refresh_failed; official route provider returned errors for "
                    + ", ".join(errored_pairs)
                )
            if missing_pairs:
                hard_failures.append(
                    "route_quality: route_coverage_missing; official adjacent route evidence is incomplete"
                )
        checks = [
            self._check_final_poi_grounding(plan_id, hard_failures, planning_context),
            self._check_intent_grounding_semantics(version_snapshot, hard_failures, planning_context),
            self._check_pending_poi_not_final(session_id, plan_id, hard_failures),
            self._check_patch_version(session_id, plan_id, version_id, patch_id, hard_failures),
            self._check_ticket_source_transparency(plan_id, hard_failures),
            self._check_required_meal_grounding(plan_id, hard_failures, soft_failures, planning_context),
            route_quality_check,
            self._check_basic_itinerary(plan_id, planning_context, hard_failures, soft_failures, version_snapshot),
            self._check_hard_campus_constraints(
                plan_id, hard_failures, expected_constraints, planning_context=planning_context
            ),
            self._check_budget_invariant(version_snapshot, hard_failures),
        ]
        return AgentVerifierReport(
            passed=not hard_failures,
            hard_failures=hard_failures,
            soft_failures=soft_failures,
            checks=checks,
            metadata={
                "sessionId": session_id,
                "planId": plan_id,
                "versionId": version_id,
                "patchId": patch_id,
                "routeQualityStatus": route_quality_check["status"],
                "routeQualityWarnings": route_quality_check.get("warnings", []),
            },
        )

    def _version_snapshot(self, plan_id: str, version_id: Optional[str], failures: list[str]) -> Optional[dict]:
        if not version_id:
            failures.append("VERIFIER_INPUT_MISSING: version_id is required to verify the persisted itinerary snapshot")
            return None
        row = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND plan_id = ?",
            (version_id, plan_id),
        ).fetchone()
        if row is None:
            failures.append(f"VERIFIER_INPUT_MISSING: itinerary version snapshot not found for {version_id}")
            return None
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except json.JSONDecodeError:
            failures.append(f"VERIFIER_INPUT_MISSING: itinerary version snapshot is invalid JSON for {version_id}")
            return None
        if not isinstance(snapshot, dict):
            failures.append(f"VERIFIER_INPUT_MISSING: itinerary version snapshot must be an object for {version_id}")
            return None
        return snapshot

    def verify_active_version_unchanged(
        self,
        session_id: str,
        expected_active_version_id: Optional[str],
        reason: str,
    ) -> AgentVerifierReport:
        failures: list[str] = []
        session = self.db.execute(
            "SELECT active_plan_id, active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        plan_id = session["active_plan_id"] if session is not None else None
        if session is None:
            failures.append("session not found")
        elif session["active_version_id"] != expected_active_version_id:
            failures.append(
                f"activeVersionId changed during {reason}: expected {expected_active_version_id}, got {session['active_version_id']}"
            )
        checks = [
            {
                "name": "active_version_unchanged",
                "status": "passed" if not failures else "failed",
                "expectedActiveVersionId": expected_active_version_id,
                "actualActiveVersionId": session["active_version_id"] if session is not None else None,
                "reason": reason,
            }
        ]
        if plan_id:
            checks.append(self._check_ticket_source_transparency(plan_id, failures))
        return AgentVerifierReport(
            passed=not failures,
            hard_failures=failures,
            checks=checks,
            metadata={"sessionId": session_id, "planId": plan_id, "reason": reason},
        )

    def verify_current_itinerary_state(self, session_id: str, plan_id: str, reason: str) -> AgentVerifierReport:
        hard_failures: list[str] = []
        version_snapshot = self._latest_version_snapshot(plan_id)
        checks = [
            self._check_final_poi_grounding(plan_id, hard_failures),
            self._check_intent_grounding_semantics(version_snapshot, hard_failures),
            self._check_pending_poi_not_final(session_id, plan_id, hard_failures),
            self._check_ticket_source_transparency(plan_id, hard_failures),
        ]
        return AgentVerifierReport(
            passed=not hard_failures,
            hard_failures=hard_failures,
            checks=checks,
            metadata={"sessionId": session_id, "planId": plan_id, "reason": reason},
        )

    def verify_map_readiness(self, plan_id: str, reason: str = "map_readiness") -> AgentVerifierReport:
        failures: list[str] = []
        check = self._check_map_readiness(plan_id, failures)
        semantic_check = self._check_intent_grounding_semantics(self._latest_version_snapshot(plan_id), failures)
        hard_requirement_check = self._check_hard_campus_constraints(
            plan_id,
            failures,
            self._expected_hard_constraints({}, self._latest_version_snapshot(plan_id)),
        )
        return AgentVerifierReport(
            passed=not failures,
            hard_failures=failures,
            checks=[check, semantic_check, hard_requirement_check],
            metadata={"planId": plan_id, "reason": reason, "mapReady": not failures},
        )

    def verify_route_feasibility(self, plan_id: str, reason: str = "route_feasibility") -> AgentVerifierReport:
        failures: list[str] = []
        soft_failures: list[str] = []
        route_rows = self.db.execute(
            "SELECT from_segment_id, to_segment_id, mode, error_json FROM route_options WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        errored = [
            f"{row['from_segment_id']}->{row['to_segment_id']} {row['mode']}" for row in route_rows if row["error_json"]
        ]
        if errored:
            failures.append(f"route.error: errored generated route legs {errored}")
        quality_check = self._check_route_quality(
            plan_id,
            soft_failures,
            hard_failures=failures,
            version_snapshot=self._latest_version_snapshot(plan_id),
        )
        missing_pairs = quality_check.get("missingPairs", [])
        if missing_pairs:
            failures.append(f"route.coverage: {len(missing_pairs)} required adjacent leg(s) missing")
        check = {
            "name": "route_feasibility_verifier",
            "status": "passed" if not failures else "failed",
            "checkedRoutes": len(route_rows),
            "errored": errored,
        }
        return AgentVerifierReport(
            passed=not failures,
            hard_failures=failures,
            soft_failures=soft_failures,
            checks=[check, quality_check],
            metadata={
                "planId": plan_id,
                "reason": reason,
                "routeReady": bool(route_rows) and not failures and not missing_pairs,
                "routeCoverage": quality_check.get("routeCoverage"),
                "routeQualityStatus": quality_check["status"],
                "routeQualityWarnings": quality_check.get("warnings", []),
            },
        )

    def validate_route_matrix_snapshot_before_write(
        self,
        plan_id: str,
        version_snapshot: dict,
    ) -> list[str]:
        """Validate exact final route rows and matrix proof before version save."""
        check = self._check_route_quality(
            plan_id,
            [],
            version_snapshot=version_snapshot,
            require_matrix_proof=True,
        )
        return [str(item) for item in check.get("blockingIssues") or []]

    def _expected_hard_constraints(self, planning_context: dict, version_snapshot: Optional[dict]) -> dict:
        context_constraints = planning_context.get("hardConstraints") if isinstance(planning_context, dict) else None
        snapshot_constraints = version_snapshot.get("hardConstraints") if isinstance(version_snapshot, dict) else None
        constraints = context_constraints if isinstance(context_constraints, dict) else snapshot_constraints
        return dict(constraints) if isinstance(constraints, dict) else {}

    def _check_intent_grounding_semantics(
        self,
        version_snapshot: Optional[dict],
        failures: list[str],
        planning_context: Optional[dict] = None,
    ) -> dict:
        checked = 0
        valid_by_intent: dict[str, list[str]] = {}
        invalid_claims: list[dict] = []
        unresolved_by_intent: dict[str, list[str]] = {}
        for day in (version_snapshot.get("days") if isinstance(version_snapshot, dict) else []) or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                semantic_metadata = (
                    segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                )
                intent_type = self._snapshot_segment_intent_type(segment, poi)
                if not intent_type:
                    continue
                checked += 1
                segment_id = str(segment.get("id") or "")
                grounding_status = str(
                    semantic_metadata.get("groundingStatus")
                    or poi.get("groundingStatus")
                    or poi.get("grounding_status")
                    or ""
                )
                pending = bool(poi.get("needsConcretePoi")) or grounding_status in {
                    "waiting_for_poi_grounding",
                    "pending",
                    "provider_rate_limited",
                    "draft_only",
                }
                if pending:
                    unresolved_by_intent.setdefault(intent_type, []).append(segment_id)
                    continue
                decision = self.intent_candidate_semantic_policy.evaluate(intent_type, poi)
                if decision.passed:
                    valid_by_intent.setdefault(intent_type, []).append(segment_id)
                    continue
                invalid = {
                    "segmentId": segment_id,
                    "intentType": intent_type,
                    "poiName": str(poi.get("name") or ""),
                    "poiType": str(poi.get("type") or ""),
                    **decision.to_camel_dict(),
                }
                invalid_claims.append(invalid)
                failures.append(
                    f"intent.semantic.{intent_type}: segment {segment_id} selected {invalid['poiName']}, "
                    f"but candidate lacks {intent_type}-like evidence ({decision.reason_code})"
                )

        contract = (planning_context or {}).get("requestIntentContract") if isinstance(planning_context, dict) else None
        coverage: list[dict] = []
        for required in (contract.get("requiredIntents") if isinstance(contract, dict) else []) or []:
            if not isinstance(required, dict):
                continue
            intent_type = str(required.get("intentType") or "")
            required_min_value = required.get("requiredMin")
            if required_min_value is None:
                required_min_value = required.get("target")
            target = max(0, int(required_min_value or 0))
            satisfied = len(valid_by_intent.get(intent_type, []))
            coverage.append(
                {
                    "intentType": intent_type,
                    "requiredMin": target,
                    "satisfiedCount": satisfied,
                    "satisfiedBySegmentIds": valid_by_intent.get(intent_type, []),
                    "status": "covered" if satisfied >= target else "unresolved",
                }
            )
        return {
            "name": "intent_grounding_semantics_verifier",
            "status": "passed" if not invalid_claims else "failed",
            "checkedSegmentCount": checked,
            "semanticValidByIntent": valid_by_intent,
            "unresolvedByIntent": unresolved_by_intent,
            "invalidClaims": invalid_claims,
            "coverage": coverage,
        }

    @staticmethod
    def _snapshot_segment_intent_type(segment: dict, poi: dict) -> str:
        semantic_metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        if str(semantic_metadata.get("intentType") or "").strip():
            return str(semantic_metadata.get("intentType")).strip()
        grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
        if str(segment.get("intentType") or "").strip():
            return str(segment.get("intentType")).strip()
        for value in (poi.get("intentType"), grounding.get("intentType")):
            if str(value or "").strip():
                return str(value).strip()
        return ""

    def _check_budget_invariant(self, version_snapshot: Optional[dict], failures: list[str]) -> dict:
        budget = version_snapshot.get("budgetBreakdown") if isinstance(version_snapshot, dict) else None
        if not isinstance(budget, dict):
            return {"name": "budget_invariant", "status": "not_checked", "reason": "budget_breakdown_missing"}
        valid = BudgetInvariantPolicy.is_valid(
            known_total=float(budget.get("knownTotal") or 0),
            provisional_min=float(budget.get("provisionalMin") or 0),
            provisional_preferred=float(budget.get("provisionalPreferred") or 0),
            provisional_max=float(budget.get("provisionalMax") or 0),
        )
        if not valid:
            failures.append(
                "budget.invariant: expected 0 <= knownTotal <= provisionalMin <= provisionalPreferred <= provisionalMax"
            )
        return {
            "name": "budget_invariant",
            "status": "passed" if valid else "failed",
            "knownTotal": float(budget.get("knownTotal") or 0),
            "provisionalMin": float(budget.get("provisionalMin") or 0),
            "provisionalPreferred": float(budget.get("provisionalPreferred") or 0),
            "provisionalMax": float(budget.get("provisionalMax") or 0),
            "invariantValid": valid,
        }

    def _latest_version_snapshot(self, plan_id: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE plan_id = ? ORDER BY version_number DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def _check_hard_campus_constraints(
        self,
        plan_id: str,
        failures: list[str],
        expected_constraints: Optional[dict] = None,
        planning_context: Optional[dict] = None,
    ) -> dict:
        expected = expected_constraints if isinstance(expected_constraints, dict) else {}
        campus_constraint = expected.get("campusTier") if isinstance(expected.get("campusTier"), dict) else {}
        tier = str(campus_constraint.get("value") or "").strip()
        strict = bool(campus_constraint.get("strict"))
        expected_count = int(campus_constraint.get("expectedCount") or 0)
        source = str(campus_constraint.get("source") or "")
        rows = self.db.execute(
            """
            SELECT s.id, s.kind, s.semantic_metadata_json, p.name, p.type, p.category, p.city,
                   p.address, p.district,
                   p.source, p.source_note
            FROM itinerary_segments s JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND s.kind IN ('visit', 'activity')
            """,
            (plan_id,),
        ).fetchall()

        def is_campus_row(row: Any) -> bool:
            metadata = self._json_object(row["semantic_metadata_json"])
            if str(metadata.get("intentType") or "") == "campus_visit":
                return True
            category = str(row["category"] or "").strip().casefold()
            if category in {"campus", "higher_education_institution"}:
                return True
            provider_type = str(row["type"] or "")
            return bool(
                re.search(r"(高等院校|大学院校|校园|校区|campus)", provider_type, re.IGNORECASE)
                and not re.search(r"(博物馆|美术馆|餐厅|食堂|服务中心|附属中学|附属小学)", provider_type)
            )

        campus_rows = [row for row in rows if is_campus_row(row)]
        unresolved_campus_rows = [
            row
            for row in campus_rows
            if str(self._json_object(row["semantic_metadata_json"]).get("groundingStatus") or "")
            in {"waiting_for_poi_grounding", "provider_rate_limited", "draft_only"}
            or str(row["source"] or "") != AMAP_PLACE_SOURCE
        ]
        confirmed_campus_rows = [row for row in campus_rows if row not in unresolved_campus_rows]
        mismatches = []
        policy = CampusCandidatePolicy()
        for row in confirmed_campus_rows:
            candidate = SimpleNamespace(**dict(row))
            if tier and not policy.matches_tier(candidate, tier):
                mismatches.append(str(row["name"] or row["id"]))
        missing_count = max(0, expected_count - len(confirmed_campus_rows)) if tier and strict else 0
        candidate_grounding = (
            (planning_context or {}).get("candidateFirstGrounding")
            if isinstance((planning_context or {}).get("candidateFirstGrounding"), dict)
            else {}
        )
        finalization = (
            candidate_grounding.get("finalization") if isinstance(candidate_grounding.get("finalization"), dict) else {}
        )
        draft_persistence_override = bool(finalization.get("draftPersistenceOverride"))
        if tier and strict and not source:
            failures.append("hard_requirement_coverage: expected constraint source is missing")
        if mismatches:
            failures.append(f"hard_requirement_coverage: non-{tier} campus selected {mismatches}")
        if missing_count and not draft_persistence_override:
            failures.append(
                "hard_requirement_coverage: missing_required_campus_segments "
                f"expected {expected_count}, got {len(confirmed_campus_rows)}"
            )
        checked = bool(tier and strict)
        pending = bool(checked and missing_count and draft_persistence_override and not mismatches)
        return {
            "name": "hard_requirement_coverage",
            "status": (
                "pending"
                if pending
                else "passed"
                if checked and not mismatches and not missing_count and source
                else "failed"
                if checked
                else "not_checked"
            ),
            "campusTier": tier or None,
            "constraintSource": source or None,
            "expectedCampusCount": expected_count or None,
            "checkedCampusCount": len(confirmed_campus_rows),
            "unresolvedCampusCount": len(unresolved_campus_rows),
            "unresolvedCampusNames": [str(row["name"] or row["id"]) for row in unresolved_campus_rows],
            "missingCampusCount": missing_count,
            "non985CampusNames": mismatches,
            "hardConstraintRelaxed": bool(campus_constraint.get("relaxed")),
            "draftPersistenceOverride": draft_persistence_override,
        }

    def verify_schedule_feasibility(self, plan_id: str, reason: str = "schedule_feasibility") -> AgentVerifierReport:
        route_rows = self.db.execute(
            """
            SELECT r.from_segment_id, r.to_segment_id, r.duration_seconds,
                   sf.end_time AS from_end, st.start_time AS to_start,
                   pf.name AS from_poi_name, pt.name AS to_poi_name
            FROM route_options r
            JOIN itinerary_segments sf ON sf.id = r.from_segment_id
            JOIN itinerary_segments st ON st.id = r.to_segment_id
            JOIN pois pf ON pf.id = sf.poi_id
            JOIN pois pt ON pt.id = st.poi_id
            WHERE r.plan_id = ? AND r.is_selected = 1 AND r.error_json IS NULL
            """,
            (plan_id,),
        ).fetchall()
        route_conflicts: list[dict[str, object]] = []
        for row in route_rows:
            from_end = self._clock_minutes(row["from_end"])
            to_start = self._clock_minutes(row["to_start"])
            if from_end is None or to_start is None:
                continue
            route_minutes = (int(row["duration_seconds"] or 0) + 59) // 60
            required_start = from_end + route_minutes + 10
            if to_start < required_start:
                route_conflicts.append(
                    {
                        "type": "route_timing",
                        "fromSegmentId": row["from_segment_id"],
                        "toSegmentId": row["to_segment_id"],
                        "fromPoiName": row["from_poi_name"],
                        "toPoiName": row["to_poi_name"],
                        "actualStartMinutes": to_start,
                        "requiredStartMinutes": required_start,
                    }
                )
        failures = [
            f"schedule.conflict: {item['fromPoiName']} → {item['toPoiName']} starts before route and buffer complete"
            for item in route_conflicts
        ]

        segment_rows = self.db.execute(
            """
            SELECT d.day_number, s.id, s.segment_order, s.start_time, s.end_time,
                   s.kind, s.estimate_metadata_json, s.semantic_metadata_json,
                   p.name AS poi_name
            FROM itinerary_segments s
            JOIN itinerary_days d ON d.id = s.day_id
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ?
            ORDER BY d.day_number ASC, s.segment_order ASC, s.id ASC
            """,
            (plan_id,),
        ).fetchall()
        by_day: dict[int, list[sqlite3.Row]] = {}
        for row in segment_rows:
            by_day.setdefault(int(row["day_number"]), []).append(row)

        interval_conflicts: list[dict[str, object]] = []
        chronology_violations: list[dict[str, object]] = []
        invalid_intervals: list[dict[str, object]] = []
        overlap_minutes = 0
        user_locked_conflicts: set[str] = set()
        provisional_segment_count = 0
        temporal_failures: list[dict[str, object]] = []
        for day_number, day_rows in by_day.items():
            intervals: list[tuple[int, int, sqlite3.Row, bool]] = []
            previous_start: Optional[int] = None
            for row in day_rows:
                start = self._clock_minutes(row["start_time"])
                end = self._clock_minutes(row["end_time"])
                metadata = self._json_object(row["estimate_metadata_json"])
                semantic_metadata = self._json_object(row["semantic_metadata_json"])
                temporal_failures.extend(
                    ItineraryScheduleService.temporal_failures(
                        {
                            "id": row["id"],
                            "startTime": row["start_time"],
                            "endTime": row["end_time"],
                            "kind": row["kind"],
                            "semanticMetadata": semantic_metadata,
                        }
                    )
                )
                duration = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
                schedule = metadata.get("schedule") if isinstance(metadata.get("schedule"), dict) else {}
                user_locked = bool(duration.get("userLocked"))
                if str(schedule.get("status") or "") == "provisional_missing_route":
                    provisional_segment_count += 1
                if start is None or end is None or end <= start:
                    invalid_intervals.append(
                        {
                            "dayNumber": day_number,
                            "segmentId": row["id"],
                            "poiName": row["poi_name"],
                            "startTime": row["start_time"],
                            "endTime": row["end_time"],
                        }
                    )
                    if user_locked:
                        user_locked_conflicts.add(str(row["id"]))
                    continue
                if previous_start is not None and start < previous_start:
                    chronology_violations.append(
                        {
                            "dayNumber": day_number,
                            "segmentId": row["id"],
                            "poiName": row["poi_name"],
                            "startMinutes": start,
                        }
                    )
                previous_start = start
                intervals.append((start, end, row, user_locked))

            ordered_intervals = sorted(
                intervals,
                key=lambda item: (item[0], int(item[2]["segment_order"] or 0), str(item[2]["id"])),
            )
            for left_index, (left_start, left_end, left_row, left_locked) in enumerate(ordered_intervals):
                for right_start, right_end, right_row, right_locked in ordered_intervals[left_index + 1 :]:
                    minutes = min(left_end, right_end) - max(left_start, right_start)
                    if minutes <= 0:
                        continue
                    interval_conflicts.append(
                        {
                            "type": "interval_overlap",
                            "dayNumber": day_number,
                            "fromSegmentId": left_row["id"],
                            "toSegmentId": right_row["id"],
                            "fromPoiName": left_row["poi_name"],
                            "toPoiName": right_row["poi_name"],
                            "overlapMinutes": minutes,
                        }
                    )
                    if left_locked:
                        user_locked_conflicts.add(str(left_row["id"]))
                    if right_locked:
                        user_locked_conflicts.add(str(right_row["id"]))

            events: dict[int, int] = {}
            for start, end, _row, _locked in intervals:
                events[start] = events.get(start, 0) + 1
                events[end] = events.get(end, 0) - 1
            active = 0
            previous_time: Optional[int] = None
            for event_time in sorted(events):
                if previous_time is not None and active > 1:
                    overlap_minutes += event_time - previous_time
                active += events[event_time]
                previous_time = event_time

        for item in invalid_intervals:
            failures.append(f"schedule.invalid_interval: {item['poiName']} has an invalid time range")
        for item in chronology_violations:
            failures.append(f"schedule.chronology: Day {item['dayNumber']} segment_order is not chronological")
        for item in interval_conflicts:
            failures.append(
                f"schedule.overlap: {item['fromPoiName']} overlaps {item['toPoiName']} by {item['overlapMinutes']} minute(s)"
            )
        failures.extend(f"{item.get('code')}: {item.get('segmentId')}" for item in temporal_failures)
        all_conflicts = [*route_conflicts, *interval_conflicts]
        schedule_status = "conflict" if failures else "provisional" if provisional_segment_count else "executable"
        return AgentVerifierReport(
            passed=not failures,
            hard_failures=failures,
            checks=[
                {
                    "name": "schedule_feasibility_verifier",
                    "status": "passed" if not failures else "failed",
                    "scheduleStatus": schedule_status,
                    "conflicts": all_conflicts,
                    "invalidIntervals": invalid_intervals,
                    "chronologyViolations": chronology_violations,
                    "temporalFailures": temporal_failures,
                }
            ],
            metadata={
                "planId": plan_id,
                "reason": reason,
                "scheduleStatus": schedule_status,
                "scheduleConflictCount": len(all_conflicts),
                "scheduleOverlapCount": len(interval_conflicts),
                "scheduleOverlapMinutes": overlap_minutes,
                "chronologyViolationCount": len(chronology_violations),
                "userLockedConflictCount": len(user_locked_conflicts),
                "provisionalSegmentCount": provisional_segment_count,
                "temporalFailureCount": len(temporal_failures),
            },
        )

    @staticmethod
    def _json_object(value: object) -> dict:
        try:
            parsed = json.loads(str(value or "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _clock_minutes(value: object) -> Optional[int]:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or ""))
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return hour * 60 + minute

    def count_get_side_effect_tables(self, plan_id: str) -> dict[str, int]:
        return {
            "planning_runs": self._count("planning_runs", "itinerary_plan_id = ?", (plan_id,)),
            "ticket_lookup_results": self._count(
                "ticket_lookup_results",
                "segment_id IN (SELECT id FROM itinerary_segments WHERE plan_id = ?)",
                (plan_id,),
            ),
            "route_options": self._count("route_options", "plan_id = ?", (plan_id,)),
            "weather_signals": self._count("weather_signals", "plan_id = ?", (plan_id,)),
        }

    def verify_get_itinerary_read_only(self, before: dict[str, int], after: dict[str, int]) -> AgentVerifierReport:
        failures = [
            f"GET itinerary changed {key}: {before_count} -> {after.get(key)}"
            for key, before_count in before.items()
            if after.get(key) != before_count
        ]
        return AgentVerifierReport(
            passed=not failures,
            hard_failures=failures,
            checks=[{"name": "get_itinerary_read_only", "status": "passed" if not failures else "failed"}],
            metadata={"before": before, "after": after},
        )

    def _check_final_poi_grounding(
        self,
        plan_id: str,
        failures: list[str],
        planning_context: Optional[dict] = None,
    ) -> dict:
        draft_mode = self._draft_persistence_mode(planning_context or {})
        rows = self.db.execute(
            """
            SELECT s.kind, s.semantic_metadata_json, p.*
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND s.kind IN ('visit', 'activity', 'meal', 'shopping')
            """,
            (plan_id,),
        ).fetchall()
        invalid = []
        draft = []
        for row in rows:
            semantic_metadata = self._json_object(row["semantic_metadata_json"])
            if not self._is_required_routeable_segment(row["kind"], semantic_metadata):
                continue
            if self._is_mock_or_synthetic_row(row):
                if draft_mode:
                    draft.append(f"{row['name']} mock_or_synthetic_poi 不能作为真实高德地点，需重新选择")
                    continue
                invalid.append(f"{row['name']} mock_or_synthetic_poi cannot be mapReady")
                continue
            if self._night_view_requires_concrete_amap_poi(row["kind"], row["name"], row["source"], semantic_metadata):
                agent_text_map_ready_shape = (
                    row["source"] == "agent-text-timeline"
                    and bool(row["amap_id"])
                    and self._valid_coordinate(row["longitude"], row["latitude"])
                )
                if draft_mode and not agent_text_map_ready_shape and row["source"] != AMAP_PLACE_SOURCE:
                    draft.append(f"night_view_requires_concrete_amap_poi: {row['name']}")
                    continue
                invalid.append(f"night_view_requires_concrete_amap_poi: {row['name']}")
                continue
            if row["source"] == "agent-text-timeline":
                draft.append(f"{row['name']} 是可编辑时间轴草稿 POI，高德 POI 待校验")
                continue
            if not row["amap_id"]:
                invalid.append(f"{row['name']} missing amapId")
            if row["source"] != AMAP_PLACE_SOURCE:
                invalid.append(f"{row['name']} source is {row['source']}")
            if float(row["confidence"] or 0) < 0.8:
                invalid.append(f"{row['name']} confidence below 0.8")
            if not self._valid_coordinate(row["longitude"], row["latitude"]):
                invalid.append(f"{row['name']} missing real coordinates")
        failures.extend(invalid)
        if invalid:
            status = "failed"
        elif draft and draft_mode:
            status = "warning"
        else:
            status = "passed" if not draft else "failed"
        return {
            "name": "amap_poi_grounding",
            "status": status,
            "checked": len(rows),
            "invalid": invalid,
            "draft": draft,
            "draftPersistenceMode": draft_mode,
            "requiredMapReady": not invalid and not draft and bool(rows),
            "optionalMapReady": True,
            "mapReady": not invalid and not draft and bool(rows),
        }

    def _check_map_readiness(self, plan_id: str, failures: list[str]) -> dict:
        rows = self.db.execute(
            """
            SELECT s.id AS segment_id, s.kind, s.semantic_metadata_json, p.id AS poi_id, p.name, p.amap_id, p.longitude, p.latitude, p.source, p.source_note
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND s.kind IN ('visit', 'activity', 'meal', 'shopping')
            ORDER BY s.day_id ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        items = []
        missing = []
        for row in rows:
            semantic_metadata = self._json_object(row["semantic_metadata_json"])
            if not self._is_required_routeable_segment(row["kind"], semantic_metadata):
                continue
            has_provider_poi_id = bool(row["amap_id"])
            has_coordinates = self._valid_coordinate(row["longitude"], row["latitude"])
            untrusted = self._is_mock_or_synthetic_row(row)
            night_view_requires_concrete = self._night_view_requires_concrete_amap_poi(
                row["kind"], row["name"], row["source"], semantic_metadata
            )
            trusted_source = str(row["source"] or "") == AMAP_PLACE_SOURCE
            ready = (
                has_provider_poi_id
                and has_coordinates
                and trusted_source
                and not untrusted
                and not night_view_requires_concrete
            )
            status = "map_ready" if ready else "draft_only"
            items.append(
                {
                    "segmentId": row["segment_id"],
                    "poiId": row["poi_id"],
                    "poiName": row["name"],
                    "status": status,
                    "source": row["source"],
                    "hasProviderPoiId": has_provider_poi_id,
                    "hasCoordinates": has_coordinates,
                    "trustedSource": trusted_source,
                    "mockOrSynthetic": untrusted,
                    "nightViewConcreteRequired": night_view_requires_concrete,
                }
            )
            if not ready:
                if night_view_requires_concrete:
                    missing.append(f"night_view_requires_concrete_amap_poi: {row['name']}")
                elif untrusted:
                    missing.append(f"{row['name']} mock_or_synthetic_poi cannot be mapReady")
                elif not has_provider_poi_id or not has_coordinates:
                    missing.append(f"{row['name']} missing providerPoiId/coordinates")
                elif not trusted_source:
                    missing.append(f"{row['name']} source is {row['source']}")
                else:
                    missing.append(f"{row['name']} missing providerPoiId/coordinates")
        failures.extend(missing)
        return {
            "name": "map_readiness_verifier",
            "status": "passed" if not missing else "failed",
            "checked": len(rows),
            "requiredMapReady": not missing,
            "optionalMapReady": True,
            "mapReady": not missing,
            "missing": missing,
            "items": items,
        }

    def _check_required_meal_grounding(
        self,
        plan_id: str,
        hard_failures: list[str],
        soft_failures: list[str],
        planning_context: dict,
    ) -> dict:
        rows = self.db.execute(
            """
            SELECT s.id AS segment_id, s.semantic_metadata_json, p.id AS poi_id, p.name, p.amap_id,
                   p.longitude, p.latitude, p.source, p.source_note
            FROM itinerary_segments s
            LEFT JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND s.kind = 'meal'
            ORDER BY s.day_id ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        required_rows = [row for row in rows if self._is_required_meal_row(row)]
        unresolved: list[dict[str, object]] = []
        provider_rate_limited = False
        for row in required_rows:
            semantic_metadata = self._json_object(row["semantic_metadata_json"])
            status = str(semantic_metadata.get("groundingStatus") or "draft_only")
            provider_rate_limited = provider_rate_limited or status == "provider_rate_limited"
            has_provider_poi_id = bool(row["amap_id"])
            has_coordinates = self._valid_coordinate(row["longitude"], row["latitude"])
            trusted_source = str(row["source"] or "") == AMAP_PLACE_SOURCE
            placeholder_name = self._generic_required_meal_name(row["name"])
            grounded = (
                trusted_source
                and has_provider_poi_id
                and has_coordinates
                and status
                not in {
                    "draft_only",
                    "not_required",
                    "optional_waiting",
                    "provider_rate_limited",
                    "waiting_for_poi_grounding",
                }
                and not placeholder_name
            )
            if grounded:
                continue
            unresolved.append(
                {
                    "segmentId": row["segment_id"],
                    "poiId": row["poi_id"],
                    "poiName": row["name"],
                    "groundingStatus": status,
                    "source": row["source"],
                    "hasProviderPoiId": has_provider_poi_id,
                    "hasCoordinates": has_coordinates,
                    "placeholderName": placeholder_name,
                }
            )
        partial_draft_allowed = self._candidate_first_allows_pending_required_meals(planning_context, unresolved)
        if unresolved and partial_draft_allowed:
            soft_failures.append(
                f"required meal grounding incomplete in partial editable draft: {len(unresolved)} meal segment(s) still need concrete map POI"
            )
        elif unresolved:
            message = (
                f"required meal grounding incomplete: {len(unresolved)} meal segment(s) still need concrete map POI"
            )
            hard_failures.append(message)
            if provider_rate_limited:
                soft_failures.append(
                    "required meal grounding blocked by provider_rate_limited; retry after map provider recovers"
                )
        return {
            "name": "required_meal_grounding_verifier",
            "status": "passed" if not unresolved else ("warning" if partial_draft_allowed else "failed"),
            "requiredMealCount": len(required_rows),
            "groundedMealCount": len(required_rows) - len(unresolved),
            "unresolvedMealCount": len(unresolved),
            "unresolvedMealSegments": unresolved,
            "providerRateLimited": provider_rate_limited,
            "partialDraftAllowed": partial_draft_allowed,
            "suggestedNextActions": ["retry_after_map_provider_recovers"]
            if provider_rate_limited
            else (["retry_unfinished_poi_grounding"] if unresolved else []),
        }

    def _candidate_first_allows_pending_required_meals(
        self,
        planning_context: dict,
        unresolved_meals: list[dict[str, object]],
    ) -> bool:
        grounding = (
            planning_context.get("candidateFirstGrounding")
            if isinstance(planning_context.get("candidateFirstGrounding"), dict)
            else {}
        )
        finalization = grounding.get("finalization") if isinstance(grounding.get("finalization"), dict) else {}
        if not (
            (
                finalization.get("canCreateVersion") is True
                and finalization.get("unresolvedPolicy") == "persist_viable_partial_days"
            )
            or finalization.get("draftPersistenceOverride") is True
        ):
            return False
        retryable_statuses = {"waiting_for_poi_grounding", "provider_rate_limited"}
        if any(str(item.get("groundingStatus") or "") not in retryable_statuses for item in unresolved_meals):
            return False
        if self._candidate_first_selected_required_meal_count(grounding) > 0:
            return True
        return self._candidate_first_persisted_pending_required_meal_count(grounding) >= len(unresolved_meals)

    def _draft_persistence_mode(self, planning_context: dict) -> bool:
        grounding = (
            planning_context.get("candidateFirstGrounding")
            if isinstance(planning_context.get("candidateFirstGrounding"), dict)
            else {}
        )
        finalization = grounding.get("finalization") if isinstance(grounding.get("finalization"), dict) else {}
        return bool(
            finalization.get("draftPersistenceOverride")
            or (
                finalization.get("canCreateVersion") is True
                and finalization.get("unresolvedPolicy") == "persist_viable_partial_days"
            )
            or planning_context.get("timelinePersistencePolicy") == "draft_first"
        )

    def _candidate_first_selected_required_meal_count(self, grounding: dict) -> int:
        count = 0
        coverage_items = (
            grounding.get("requiredIntentCoverage") if isinstance(grounding.get("requiredIntentCoverage"), list) else []
        )
        for item in coverage_items:
            if not isinstance(item, dict):
                continue
            if item.get("intentType") != "meal" or item.get("requiredIntent") is not True:
                continue
            try:
                count += int(item.get("selectedCount") or 0)
            except (TypeError, ValueError):
                continue
        return count

    def _candidate_first_persisted_pending_required_meal_count(self, grounding: dict) -> int:
        count = 0
        unresolved_slots = (
            grounding.get("unresolvedSlots") if isinstance(grounding.get("unresolvedSlots"), list) else []
        )
        for item in unresolved_slots:
            if not isinstance(item, dict) or item.get("persistedPending") is not True:
                continue
            if str(item.get("kind") or "") == "meal" or str(item.get("intentType") or "") == "meal":
                count += 1
        return count

    def _is_required_meal_row(self, row: sqlite3.Row) -> bool:
        metadata = self._json_object(row["semantic_metadata_json"])
        return (
            str(metadata.get("intentType") or "") == "meal"
            and bool(metadata.get("required"))
            and str(metadata.get("requirementLevel") or "") not in {"soft_experience", "optional"}
        )

    def _grounding_status_from_text(self, text: str) -> str:
        for status in (
            "not_required",
            "optional_waiting",
            "draft_only",
            "waiting_for_poi_grounding",
            "provider_rate_limited",
            "agent_selected_candidate",
            "verified_amap",
            "user_confirmed",
            "routeable_anchor",
            "area_unresolved",
            "functional_poi",
            "composite_poi",
        ):
            if f"groundingStatus：{status}" in text or f"groundingStatus: {status}" in text:
                return status
        return "draft_only"

    def _generic_required_meal_name(self, value: object) -> bool:
        text = str(value or "").strip()
        compact = "".join(text.split())
        if not compact:
            return True
        generic_terms = ("早餐", "午餐", "中餐", "晚餐", "用餐", "吃饭", "餐饮体验", "当地特色美食", "本地特色美食")
        return compact in generic_terms or bool(
            any(term in compact for term in ("当地特色美食", "本地特色餐饮"))
            and not any(term in compact for term in ("店", "餐厅", "饭店", "馆", "楼", "坊", "小吃"))
        )

    def _check_route_quality(
        self,
        plan_id: str,
        soft_failures: list[str],
        hard_failures: Optional[list[str]] = None,
        version_snapshot: Optional[dict] = None,
        require_matrix_proof: bool = False,
    ) -> dict:
        rows = self._route_quality_rows(plan_id, selected_only=True)
        if not rows:
            rows = self._route_quality_rows(plan_id, selected_only=False)
        day_stats: dict[int, dict[str, object]] = {}
        warnings: list[str] = []
        blocking_issues: list[str] = []
        poor = False
        for row in rows:
            day_number = int(row["day_number"] or 0)
            if row["error_json"]:
                poor = True
                blocking_issues.append("route_provider_error")
                warnings.append(f"Day {day_number} 路线 Provider 返回错误；该路线不能作为最终路线证据。")
                continue
            route_endpoint_issue = self._route_endpoint_trust_issue(row)
            if route_endpoint_issue:
                poor = True
                warnings.append(f"Day {day_number} route option includes {route_endpoint_issue}.")
                blocking_issues.append(route_endpoint_issue)
                continue
            distance_km = float(row["distance_meters"] or 0) / 1000
            duration_min = float(row["duration_minutes"] or 0)
            if distance_km <= 0 or duration_min <= 0:
                poor = True
                blocking_issues.append("route_provider_metrics_invalid")
                warnings.append(f"Day {day_number} 路线 Provider 缺少正数距离或时长；该路线不能作为最终路线证据。")
                continue
            stats = day_stats.setdefault(
                day_number,
                {
                    "dayNumber": day_number,
                    "totalRouteDistanceKm": 0.0,
                    "totalRouteDurationMinutes": 0.0,
                    "maxLegDistanceKm": 0.0,
                    "issues": [],
                },
            )
            stats["totalRouteDistanceKm"] = float(stats["totalRouteDistanceKm"]) + distance_km
            stats["totalRouteDurationMinutes"] = float(stats["totalRouteDurationMinutes"]) + duration_min
            stats["maxLegDistanceKm"] = max(float(stats["maxLegDistanceKm"]), distance_km)
            if distance_km > 20:
                stats["issues"].append("route_leg_distance_diagnostic")
                warnings.append(
                    f"Day {day_number} Provider 路线段为 {distance_km:.1f} km；这是负担诊断，不作为顺路接受或拒绝依据。"
                )
            if (row["from_kind"] == "meal" or row["to_kind"] == "meal") and (distance_km > 12 or duration_min > 70):
                stats["issues"].append("meal_route_load_diagnostic")
                warnings.append(
                    f"Day {day_number} 餐饮相邻 Provider 路线为 "
                    f"{distance_km:.1f} km / {duration_min:.0f} min；"
                    "是否顺路仅由完整路线矩阵的相对增量决定。"
                )
        for stats in day_stats.values():
            day_number = int(stats["dayNumber"])
            total_km = float(stats["totalRouteDistanceKm"])
            total_min = float(stats["totalRouteDurationMinutes"])
            if total_km > 40 or total_min > 240:
                stats["issues"].append("day_route_load_diagnostic")
                warnings.append(
                    f"Day {day_number} 当日 Provider 路线负担诊断："
                    f"{total_km:.1f} km / {total_min:.0f} min；不作为顺路硬门槛。"
                )
            stats["totalRouteDistanceKm"] = round(total_km, 1)
            stats["totalRouteDurationMinutes"] = round(total_min, 0)
            stats["maxLegDistanceKm"] = round(float(stats["maxLegDistanceKm"]), 1)
            stats["issues"] = sorted(set(stats["issues"]))
        missing_pairs = self._missing_routeable_functional_pairs(plan_id, rows)
        for item in missing_pairs:
            warnings.append(f"Day {item['dayNumber']} {item['fromPoiName']} → {item['toPoiName']}：路线待补全。")
        if missing_pairs:
            poor = True
            blocking_issues.append("route_coverage_missing")
        matrix_issues = self._route_matrix_proof_issues(
            rows,
            version_snapshot,
            require_proof=require_matrix_proof,
        )
        if matrix_issues:
            poor = True
            blocking_issues.extend(matrix_issues)
            for issue in matrix_issues:
                warnings.append(f"Provider 路线矩阵证据未通过最终核验：{issue}。")
        status = "poor" if poor else ("warning" if warnings else "good")
        for warning in warnings:
            if warning not in soft_failures:
                soft_failures.append(warning)
        if hard_failures is not None:
            for issue in sorted(set(blocking_issues)):
                failure = (
                    f"route_quality: {issue}; refresh complete Provider route evidence before marking itinerary ready"
                )
                if failure not in hard_failures:
                    hard_failures.append(failure)
        return {
            "name": "route_quality_verifier",
            "status": status,
            "checkedRoutes": len(rows),
            "warnings": warnings,
            "blockingIssues": sorted(set(blocking_issues)),
            "days": list(day_stats.values()),
            "suggestedNextActions": ["重新获取完整 Provider 路线矩阵并修复路线或时间窗证据"] if warnings else [],
            "missingPairs": missing_pairs,
            "routeCoverage": {
                "requiredLegCount": len(
                    {(row["from_segment_id"], row["to_segment_id"]) for row in rows if not row["error_json"]}
                )
                + len(missing_pairs),
                "coveredLegCount": len(
                    {(row["from_segment_id"], row["to_segment_id"]) for row in rows if not row["error_json"]}
                ),
                "missingLegCount": len(missing_pairs),
            },
        }

    def _route_matrix_proof_issues(
        self,
        rows: list[sqlite3.Row],
        version_snapshot: Optional[dict],
        *,
        require_proof: bool = False,
    ) -> list[str]:
        proofs: list[dict] = []
        if isinstance(version_snapshot, dict):
            raw_proofs = version_snapshot.get("routeInsertionProofs")
            if isinstance(raw_proofs, list):
                proofs.extend(item for item in raw_proofs if isinstance(item, dict))
            elif raw_proofs is not None:
                return ["provider_route_matrix_proof_invalid"]
        for row in rows:
            for key in ("from_semantic_metadata_json", "to_semantic_metadata_json"):
                semantic = self._json_object(row[key])
                proof = semantic.get("routeReplacementMatrixProof")
                if isinstance(proof, dict):
                    proofs.append(proof)
        unique: dict[str, dict] = {}
        for proof in proofs:
            unique[json.dumps(proof, ensure_ascii=False, sort_keys=True, default=str)] = proof
        if require_proof and rows and not unique:
            return ["provider_route_matrix_proof_missing"]
        if require_proof and rows:
            raw_expected = (
                version_snapshot.get("routeMatrixExpectedPairs") if isinstance(version_snapshot, dict) else None
            )
            if not isinstance(raw_expected, list):
                return ["provider_route_matrix_expected_pairs_missing"]
            expected_pairs: set[tuple[str, str]] = set()
            for pair in raw_expected:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    return ["provider_route_matrix_expected_pairs_invalid"]
                normalized = (str(pair[0] or ""), str(pair[1] or ""))
                if not all(normalized):
                    return ["provider_route_matrix_expected_pairs_invalid"]
                expected_pairs.add(normalized)
            proof_pairs = {
                (
                    str(proof.get("fromSegmentId") or ""),
                    str(proof.get("segmentId") or ""),
                )
                for proof in unique.values()
                if str(proof.get("proofType") or "") == "adjacent_route_coverage"
                and str(proof.get("fromSegmentId") or "")
                and str(proof.get("segmentId") or "")
            }
            selected_pairs = {
                (
                    str(row["from_segment_id"] or ""),
                    str(row["to_segment_id"] or ""),
                )
                for row in rows
                if not row["error_json"]
            }
            if expected_pairs != proof_pairs or not expected_pairs.issubset(selected_pairs):
                return ["provider_route_matrix_expected_pairs_mismatch"]
        issues: list[str] = []
        for proof in unique.values():
            issue = self._route_matrix_proof_issue(proof, rows)
            if issue:
                issues.append(issue)
        return sorted(set(issues))

    def _route_matrix_proof_issue(
        self,
        proof: dict,
        rows: list[sqlite3.Row],
    ) -> str:
        if str(proof.get("status") or "") == "not_required":
            segment_id = str(proof.get("segmentId") or "")
            if segment_id and any(
                segment_id
                in {
                    str(row["from_segment_id"] or ""),
                    str(row["to_segment_id"] or ""),
                }
                for row in rows
            ):
                return "provider_route_matrix_proof_invalid"
            return ""
        if (
            proof.get("status") not in {None, "passed"}
            or proof.get("networkVerified") is not True
            or proof.get("timeWindowFeasible") is not True
            or str(proof.get("detourLevel") or "") == "unacceptable"
        ):
            return "provider_route_matrix_unacceptable"
        scorer = RouteInsertionScorer()
        route_decision_contract = scorer.normalized_route_decision_contract(proof.get("routeDecisionContract"))
        if route_decision_contract is None:
            return "provider_route_decision_contract_invalid"
        legs = proof.get("legs") if isinstance(proof.get("legs"), dict) else {}
        candidate_legs = proof.get("candidateLegs")
        if not isinstance(candidate_legs, list):
            candidate_legs = [
                legs[key] for key in ("previousToCandidate", "candidateToNext") if isinstance(legs.get(key), dict)
            ]
        if not candidate_legs:
            return "provider_route_matrix_proof_incomplete"
        for leg in candidate_legs:
            if not isinstance(leg, dict) or not self._provider_matrix_leg_complete(leg):
                return "provider_route_matrix_proof_incomplete"
            if not self._provider_matrix_leg_matches_selected_route(leg, rows):
                return "provider_route_matrix_final_leg_mismatch"
        if str(proof.get("proofType") or "") == "adjacent_route_coverage":
            if len(candidate_legs) != 1:
                return "provider_route_matrix_proof_incomplete"
            leg = candidate_legs[0]
            if (
                str(proof.get("fromSegmentId") or "") != str(leg.get("fromSegmentId") or "")
                or str(proof.get("segmentId") or "") != str(leg.get("toSegmentId") or "")
                or str(proof.get("candidateAmapId") or "") != str(leg.get("toAmapId") or "")
            ):
                return "provider_route_matrix_proof_mismatch"
            return ""
        baseline = None
        baseline_legs = proof.get("baselineLegs")
        if isinstance(baseline_legs, list):
            if len(baseline_legs) != 2 or any(
                not isinstance(leg, dict) or not self._provider_matrix_leg_complete(leg) for leg in baseline_legs
            ):
                return "provider_route_matrix_proof_incomplete"
            baseline = self._combine_provider_matrix_legs(baseline_legs)
        else:
            raw_baseline = legs.get("previousToNext")
            if isinstance(raw_baseline, dict):
                baseline = raw_baseline
            replacement_operation = str(proof.get("operation") or "") in {
                "replace_poi",
                "replace_segment_poi",
                "replace_segment_poi_from_candidate",
            }
            if baseline is None and (len(candidate_legs) == 2 or replacement_operation):
                return "provider_route_matrix_proof_incomplete"
        if isinstance(baseline, dict):
            if baseline.get("composite"):
                baseline_components = [
                    legs.get("baselinePreviousToCurrent"),
                    legs.get("baselineCurrentToNext"),
                ]
                if any(
                    not isinstance(leg, dict) or not self._provider_matrix_leg_complete(leg)
                    for leg in baseline_components
                ):
                    return "provider_route_matrix_proof_incomplete"
            elif not self._provider_matrix_leg_complete(baseline):
                return "provider_route_matrix_proof_incomplete"
        score = scorer.score_from_route_matrix(
            previous_to_candidate=candidate_legs[0],
            candidate_to_next=candidate_legs[1] if len(candidate_legs) == 2 else None,
            previous_to_next=baseline,
            detour_tolerance=route_decision_contract["detourTolerance"],
            schedule_slack_minutes=proof.get("scheduleSlackMinutes"),
            time_window_feasible=True,
            mobility_profile=route_decision_contract["mobilityProfile"],
        )
        if score is None or not score.network_verified or score.detour_level == "unacceptable":
            return "provider_route_matrix_unacceptable"
        try:
            recorded_delta = float(proof.get("generalizedCostDelta"))
        except (TypeError, ValueError):
            return "provider_route_matrix_proof_incomplete"
        if (
            not math.isfinite(recorded_delta)
            or score.generalized_cost_delta is None
            or abs(recorded_delta - score.generalized_cost_delta) > 0.02
        ):
            return "provider_route_matrix_proof_mismatch"
        return ""

    @staticmethod
    def _combine_provider_matrix_legs(legs: list[dict]) -> dict:
        fields = (
            "durationSeconds",
            "distanceMeters",
            "walkingDistanceMeters",
            "transferCount",
            "waitSeconds",
            "riskPenaltyMinutes",
        )
        return {field: sum(float(item.get(field) or 0) for item in legs) for field in fields}

    @staticmethod
    def _provider_matrix_leg_complete(leg: dict) -> bool:
        if str(leg.get("provider") or "") != AMAP_ROUTE_SOURCE:
            return False
        try:
            distance = float(leg.get("distanceMeters"))
            duration = float(leg.get("durationSeconds"))
            costs = [
                float(leg[key])
                for key in (
                    "walkingDistanceMeters",
                    "transferCount",
                    "waitSeconds",
                    "riskPenaltyMinutes",
                )
            ]
            queried_at = datetime.fromisoformat(str(leg.get("queriedAt") or "").replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return False
        if queried_at.tzinfo is None:
            queried_at = queried_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - queried_at.astimezone(timezone.utc)).total_seconds()
        return bool(
            math.isfinite(distance)
            and distance > 0
            and math.isfinite(duration)
            and duration > 0
            and all(math.isfinite(value) and value >= 0 for value in costs)
            and -60 <= age_seconds <= ROUTE_CACHE_TTL_SECONDS
        )

    @staticmethod
    def _provider_matrix_leg_matches_selected_route(
        leg: dict,
        rows: list[sqlite3.Row],
    ) -> bool:
        try:
            expected_distance = int(float(leg.get("distanceMeters")))
            expected_duration = int(float(leg.get("durationSeconds")))
        except (TypeError, ValueError):
            return False
        return any(
            str(row["from_segment_id"] or "") == str(leg.get("fromSegmentId") or "")
            and str(row["to_segment_id"] or "") == str(leg.get("toSegmentId") or "")
            and str(row["from_amap_id"] or "") == str(leg.get("fromAmapId") or "")
            and str(row["to_amap_id"] or "") == str(leg.get("toAmapId") or "")
            and str(row["provider"] or "") == AMAP_ROUTE_SOURCE
            and int(row["distance_meters"] or 0) == expected_distance
            and int(row["duration_seconds"] or 0) == expected_duration
            and not row["error_json"]
            for row in rows
        )

    def _missing_routeable_functional_pairs(
        self, plan_id: str, route_rows: list[sqlite3.Row]
    ) -> list[dict[str, object]]:
        existing_pairs = {
            (str(row["from_segment_id"] or ""), str(row["to_segment_id"] or ""))
            for row in route_rows
            if row["from_segment_id"] and row["to_segment_id"] and not row["error_json"]
        }
        rows = self.db.execute(
            """
            SELECT s.id, s.kind, s.semantic_metadata_json, d.day_number, p.name AS poi_name, p.source_note
            FROM itinerary_segments s
            JOIN itinerary_days d ON d.id = s.day_id
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ?
            ORDER BY d.day_number ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        by_day: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            if self._is_required_routeable_segment(row["kind"], self._json_object(row["semantic_metadata_json"])):
                by_day.setdefault(int(row["day_number"] or 0), []).append(row)
        missing: list[dict[str, object]] = []
        for day_number, day_rows in by_day.items():
            for from_row, to_row in zip(day_rows, day_rows[1:]):
                pair = (str(from_row["id"]), str(to_row["id"]))
                if pair in existing_pairs:
                    continue
                missing.append(
                    {
                        "dayNumber": day_number,
                        "kind": "required",
                        "fromSegmentId": pair[0],
                        "toSegmentId": pair[1],
                        "fromPoiName": str(from_row["poi_name"]),
                        "toPoiName": str(to_row["poi_name"]),
                    }
                )
        return missing

    def _route_quality_rows(self, plan_id: str, *, selected_only: bool) -> list[sqlite3.Row]:
        selected_clause = "AND r.is_selected = 1" if selected_only else ""
        return self.db.execute(
            f"""
            SELECT r.*, d.day_number, sf.kind AS from_kind, st.kind AS to_kind,
                   sf.semantic_metadata_json AS from_semantic_metadata_json,
                   st.semantic_metadata_json AS to_semantic_metadata_json,
                   pf.name AS from_poi_name, pt.name AS to_poi_name,
                   pf.source AS from_source, pt.source AS to_source,
                   pf.amap_id AS from_amap_id, pt.amap_id AS to_amap_id,
                   pf.source_note AS from_source_note, pt.source_note AS to_source_note
            FROM route_options r
            JOIN itinerary_segments sf ON sf.id = r.from_segment_id
            JOIN itinerary_segments st ON st.id = r.to_segment_id
            JOIN itinerary_days d ON d.id = sf.day_id
            JOIN pois pf ON pf.id = r.from_poi_id
            JOIN pois pt ON pt.id = r.to_poi_id
            WHERE r.plan_id = ?
            {selected_clause}
            ORDER BY d.day_number ASC, sf.segment_order ASC, r.sort_order ASC
            """,
            (plan_id,),
        ).fetchall()

    def _is_required_routeable_segment(self, kind: str, semantic_metadata: object = None) -> bool:
        if kind in {"visit", "activity"}:
            return bool(not isinstance(semantic_metadata, dict) or semantic_metadata.get("routeAnchor", True))
        if kind not in {"meal", "shopping"}:
            return False
        metadata = semantic_metadata if isinstance(semantic_metadata, dict) else {}
        return bool(metadata.get("routeAnchor")) and str(metadata.get("groundingStatus") or "") not in {
            "not_required",
            "optional_waiting",
            "draft_only",
            "waiting_for_poi_grounding",
            "area_unresolved",
            "provider_rate_limited",
        }

    def _is_mock_or_synthetic_row(self, row: sqlite3.Row) -> bool:
        return self.poi_trust_policy.is_mock_or_synthetic_poi_values(
            source=row["source"],
            amap_id=row["amap_id"],
            source_note=row["source_note"],
            name=row["name"],
            kind=row["kind"],
        )

    def _night_view_requires_concrete_amap_poi(
        self,
        kind: object,
        name: object,
        source: object,
        semantic_metadata: object = None,
    ) -> bool:
        if str(kind or "") not in {"visit", "activity"}:
            return False
        metadata = semantic_metadata if isinstance(semantic_metadata, dict) else {}
        if str(metadata.get("intentType") or "") != "night_view":
            return False
        status = str(metadata.get("groundingStatus") or "draft_only")
        if self.night_view_candidate_policy.placeholder_reject_reason(name):
            return True
        if str(source or "") != AMAP_PLACE_SOURCE:
            return True
        return status in {
            "draft_only",
            "waiting_for_poi_grounding",
            "provider_rate_limited",
            "area_unresolved",
            "functional_poi",
            "composite_poi",
        }

    def _route_endpoint_trust_issue(self, row: sqlite3.Row) -> str:
        endpoints = (
            (
                "from",
                row["from_kind"],
                row["from_poi_name"],
                row["from_source"],
                row["from_amap_id"],
                row["from_source_note"],
                self._json_object(row["from_semantic_metadata_json"]),
            ),
            (
                "to",
                row["to_kind"],
                row["to_poi_name"],
                row["to_source"],
                row["to_amap_id"],
                row["to_source_note"],
                self._json_object(row["to_semantic_metadata_json"]),
            ),
        )
        for _side, kind, name, source, amap_id, source_note, semantic_metadata in endpoints:
            if kind == "meal" and not self._is_required_routeable_segment(kind, semantic_metadata):
                return "pending_meal_in_route_options"
            if self._night_view_requires_concrete_amap_poi(kind, name, source, semantic_metadata):
                return "night_view_requires_concrete_amap_poi_in_route_options"
            if self.poi_trust_policy.is_mock_or_synthetic_poi_values(
                source=source,
                amap_id=amap_id,
                source_note=source_note,
                name=name,
                kind=kind,
            ):
                return "mock_or_synthetic_poi_in_route_options"
        return ""

    def _check_pending_poi_not_final(self, session_id: str, plan_id: str, failures: list[str]) -> dict:
        # Raw AMap results can serve several Portfolio briefs. A pending record
        # only blocks its own brief/pool/slot, never another scoped proposal.
        final_scopes_by_amap: dict[str, list[dict[str, str]]] = {}
        for row in self.db.execute(
            """
            SELECT p.amap_id, s.semantic_metadata_json
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND p.amap_id IS NOT NULL
            """,
            (plan_id,),
        ).fetchall():
            metadata = self._json_object(row["semantic_metadata_json"])
            final_scopes_by_amap.setdefault(str(row["amap_id"]), []).append(
                {
                    "briefId": str(metadata.get("creativeBriefId") or metadata.get("briefId") or ""),
                    "poolId": str(metadata.get("poolId") or ""),
                    "sourceGoalId": str(metadata.get("sourceGoalId") or metadata.get("goalId") or ""),
                    "planningSlotId": str(metadata.get("planningSlotId") or metadata.get("slotId") or ""),
                }
            )
        pending_rows = self.db.execute(
            """
            SELECT id, segment_id, candidates_json
            FROM amap_poi_candidates
            WHERE session_id = ? AND status = 'pending'
            """,
            (session_id,),
        ).fetchall()
        invalid: list[str] = []
        ignored_scoped_candidates: list[str] = []
        for row in pending_rows:
            for candidate in self._loads_list(row["candidates_json"]):
                amap_id = str(candidate.get("id") or candidate.get("amapId") or candidate.get("amap_id") or "")
                final_scopes = final_scopes_by_amap.get(amap_id, [])
                if not amap_id or not final_scopes:
                    continue
                candidate_scope = {
                    "briefId": str(candidate.get("briefId") or candidate.get("creativeBriefId") or ""),
                    "poolId": str(candidate.get("poolId") or ""),
                    "sourceGoalId": str(candidate.get("sourceGoalId") or candidate.get("goalId") or ""),
                    "planningSlotId": str(candidate.get("planningSlotId") or candidate.get("slotId") or ""),
                }
                scope_matches_final_segment = any(
                    all(
                        not value or str(final_scope.get(field) or "") == value
                        for field, value in candidate_scope.items()
                    )
                    for final_scope in final_scopes
                )
                scoped_mismatch = (
                    not row["segment_id"] and any(candidate_scope.values()) and not scope_matches_final_segment
                )
                if scoped_mismatch:
                    ignored_scoped_candidates.append(str(row["id"]))
                    continue
                invalid.append(f"pending POI candidate {row['id']} appears in final itinerary")
        failures.extend(invalid)
        return {
            "name": "pending_poi_not_final",
            "status": "passed" if not invalid else "failed",
            "checked": len(pending_rows),
            "invalid": invalid,
            "ignoredScopedCandidates": sorted(set(ignored_scoped_candidates)),
        }

    def _check_patch_version(
        self,
        session_id: str,
        plan_id: str,
        version_id: Optional[str],
        patch_id: Optional[str],
        failures: list[str],
    ) -> dict:
        invalid = []
        if not patch_id:
            invalid.append("accepted Agent write did not return a patch id")
        if not version_id:
            invalid.append("accepted Agent write did not return an itinerary version id")

        session = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ? AND active_plan_id = ?",
            (session_id, plan_id),
        ).fetchone()
        if session is None:
            invalid.append("session not found for plan")
        elif version_id and session["active_version_id"] != version_id:
            invalid.append("session active_version_id does not match returned version")

        if patch_id:
            patch = self.db.execute(
                "SELECT * FROM itinerary_patches WHERE id = ? AND plan_id = ?", (patch_id, plan_id)
            ).fetchone()
            if patch is None:
                invalid.append("itinerary_patches row not found")
            elif patch["validation_status"] != "accepted":
                invalid.append("patch validation_status is not accepted")
            elif version_id and patch["result_version_id"] != version_id:
                invalid.append("patch result_version_id does not match returned version")
        if version_id:
            version = self.db.execute(
                "SELECT * FROM itinerary_versions WHERE id = ? AND session_id = ? AND plan_id = ?",
                (version_id, session_id, plan_id),
            ).fetchone()
            if version is None:
                invalid.append("itinerary_versions row not found")
        failures.extend(invalid)
        return {"name": "patch_version_invariant", "status": "passed" if not invalid else "failed", "invalid": invalid}

    def _check_ticket_source_transparency(self, plan_id: str, failures: list[str]) -> dict:
        rows = self.db.execute(
            """
            SELECT *
            FROM ticket_lookup_results
            WHERE segment_id IN (SELECT id FROM itinerary_segments WHERE plan_id = ?)
            """,
            (plan_id,),
        ).fetchall()
        invalid = []
        for row in rows:
            if not row["fallback_used"]:
                continue
            source_name = str(row["source_name"] or "").lower()
            credibility = str(row["credibility_rank"] or "").lower()
            caveat = str(row["caveat"] or "").lower()
            failure = str(row["provider_failure_reason"] or "").lower()
            combined = " ".join([source_name, credibility, caveat, failure])
            transparent = any(
                marker in combined for marker in ("fallback", "mock", "unavailable", "暂无", "失败", "模拟", "官方渠道")
            )
            masquerading = credibility in {"official", "map", "ticketing", "ota", "aggregator"} or "官方" in source_name
            if masquerading or not transparent:
                invalid.append(f"ticket fallback {row['id']} masquerades as a trusted live source")
        failures.extend(invalid)
        return {
            "name": "ticket_source_transparency",
            "status": "passed" if not invalid else "failed",
            "checked": len(rows),
            "invalid": invalid,
        }

    def _check_basic_itinerary(
        self,
        plan_id: str,
        planning_context: dict,
        hard_failures: list[str],
        soft_failures: list[str],
        version_snapshot: Optional[dict] = None,
    ) -> dict:
        if version_snapshot is not None:
            return self._check_basic_itinerary_snapshot(
                version_snapshot, planning_context, hard_failures, soft_failures
            )
        return self._check_basic_itinerary_live(plan_id, planning_context, hard_failures, soft_failures)

    def _check_basic_itinerary_live(
        self,
        plan_id: str,
        planning_context: dict,
        hard_failures: list[str],
        soft_failures: list[str],
    ) -> dict:
        day_rows = self.db.execute(
            "SELECT id, day_number, date, title FROM itinerary_days WHERE plan_id = ? ORDER BY day_number ASC",
            (plan_id,),
        ).fetchall()
        segment_rows = self.db.execute(
            """
            SELECT
                s.id,
                s.day_id,
                d.title AS day_title,
                s.kind,
                s.start_time,
                s.end_time,
                s.transport_mode,
                s.notes,
                p.name AS poi_name
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            JOIN itinerary_days d ON d.id = s.day_id
            WHERE s.plan_id = ?
            ORDER BY s.day_id ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        invalid: list[str] = []
        soft: list[str] = []
        if not day_rows:
            invalid.append("itinerary must contain at least one day")
        if not segment_rows:
            invalid.append("itinerary must contain at least one segment")

        resolved_dates = (
            planning_context.get("resolvedTripDates")
            if isinstance(planning_context.get("resolvedTripDates"), dict)
            else {}
        )
        expected_dates = [str(item) for item in resolved_dates.get("dates") or [] if str(item).strip()]
        if expected_dates:
            actual_dates = [str(row["date"] or "") for row in day_rows]
            expected_dates = self._candidate_first_expected_dates(planning_context, expected_dates, actual_dates)
            if actual_dates != expected_dates[: len(actual_dates)] or len(actual_dates) != len(expected_dates):
                invalid.append(
                    f"itinerary dates do not match resolved date range: expected {expected_dates}, got {actual_dates}"
                )

        segments_by_day: dict[str, list[sqlite3.Row]] = {}
        for row in segment_rows:
            segments_by_day.setdefault(row["day_id"], []).append(row)

        for day_id, rows in segments_by_day.items():
            seen_in_day: set[str] = set()
            duplicate_in_day: list[str] = []
            visit_rows = [row for row in rows if self._is_visit_kind(str(row["kind"] or "activity"))]
            practical_note_missing: list[str] = []
            day_title = str(rows[0]["day_title"] or "") if rows else ""
            if self._weak_day_title(day_title):
                soft.append(f"day {day_id} has weak day theme: {day_title or 'empty'}")
            if 0 < len(visit_rows) < 2:
                soft.append(f"day {day_id} has only {len(visit_rows)} visit segment; itinerary may be too thin")
            for row in rows:
                key = str(row["poi_name"] or "").strip()
                if key and key in seen_in_day:
                    duplicate_in_day.append(key)
                if key:
                    seen_in_day.add(key)
                if self._is_visit_kind(str(row["kind"] or "activity")) and not self._has_practical_notes(
                    str(row["notes"] or "")
                ):
                    practical_note_missing.append(str(row["poi_name"] or row["id"]))
            if duplicate_in_day:
                soft.append(f"day {day_id} repeats POIs: {', '.join(sorted(set(duplicate_in_day)))}")
            if len(rows) > 6:
                soft.append(f"day {day_id} has {len(rows)} segments; schedule may be overpacked")
            if practical_note_missing:
                soft.append("missing practical notes: " + ", ".join(dict.fromkeys(practical_note_missing[:6])))
            soft.extend(self._duration_soft_failures_live(rows, day_id))

        expected_transport = self._expected_transport(planning_context)
        if expected_transport:
            mismatched = [
                str(row["id"])
                for row in segment_rows
                if str(row["transport_mode"] or "") and str(row["transport_mode"] or "") != expected_transport
            ]
            if mismatched:
                soft.append(f"transport preference mismatch for segments: {', '.join(mismatched[:5])}")

        unresolved_reservation_risks = [
            str(row["poi_name"])
            for row in segment_rows
            if any(marker in str(row["notes"] or "") for marker in ("预约", "门票", "开放", "入园", "入校"))
            and not any(marker in str(row["notes"] or "") for marker in ("已确认", "官方确认", "无需预约"))
        ]
        if unresolved_reservation_risks:
            soft.append("unresolved reservation risks: " + ", ".join(dict.fromkeys(unresolved_reservation_risks[:6])))

        hard_failures.extend(invalid)
        soft_failures.extend(soft)
        return {
            "name": "basic_itinerary_verifier",
            "status": "passed" if not invalid else "failed",
            "dayCount": len(day_rows),
            "segmentCount": len(segment_rows),
            "expectedDates": expected_dates,
            "invalid": invalid,
            "softFailures": soft,
            "source": "live_plan_tables",
        }

    def _check_basic_itinerary_snapshot(
        self,
        snapshot: dict,
        planning_context: dict,
        hard_failures: list[str],
        soft_failures: list[str],
    ) -> dict:
        days = snapshot.get("days")
        day_items = days if isinstance(days, list) else []
        invalid: list[str] = []
        soft: list[str] = []
        if not isinstance(days, list):
            invalid.append("VERIFIER_INPUT_MISSING: fullItinerary snapshot days must be a list")
        if not day_items:
            invalid.append("itinerary snapshot must contain at least one day")

        segment_items: list[dict] = []
        segments_by_day: dict[str, list[dict]] = {}
        for day_index, day in enumerate(day_items, start=1):
            if not isinstance(day, dict):
                invalid.append(f"itinerary snapshot day {day_index} must be an object")
                continue
            day_key = str(day.get("id") or day.get("dayNumber") or day_index)
            segments = day.get("segments")
            day_segments = segments if isinstance(segments, list) else []
            if not isinstance(segments, list):
                invalid.append(f"itinerary snapshot day {day_key} segments must be a list")
            segments_by_day[day_key] = [segment for segment in day_segments if isinstance(segment, dict)]
            segment_items.extend(segments_by_day[day_key])
        if not segment_items:
            invalid.append("itinerary snapshot must contain at least one segment")

        structured_practical_segment_ids = {
            str(item.get("segmentId") or item.get("segment_id") or "")
            for item in (snapshot.get("ticketLookupResults") or [])
            if isinstance(item, dict)
        }

        resolved_dates = (
            planning_context.get("resolvedTripDates")
            if isinstance(planning_context.get("resolvedTripDates"), dict)
            else {}
        )
        expected_dates = [str(item) for item in resolved_dates.get("dates") or [] if str(item).strip()]
        if expected_dates:
            actual_dates = [str(day.get("date") or "") for day in day_items if isinstance(day, dict)]
            expected_dates = self._candidate_first_expected_dates(planning_context, expected_dates, actual_dates)
            if actual_dates != expected_dates[: len(actual_dates)] or len(actual_dates) != len(expected_dates):
                invalid.append(
                    f"itinerary dates do not match resolved date range: expected {expected_dates}, got {actual_dates}"
                )

        for day_key, rows in segments_by_day.items():
            seen_in_day: set[str] = set()
            duplicate_in_day: list[str] = []
            visit_rows = [
                row for row in rows if self._is_visit_kind(str(row.get("kind") or row.get("type") or "activity"))
            ]
            practical_note_missing: list[str] = []
            day = next(
                (
                    item
                    for item in day_items
                    if isinstance(item, dict) and str(item.get("id") or item.get("dayNumber") or "") == str(day_key)
                ),
                {},
            )
            day_title = str(day.get("title") or "") if isinstance(day, dict) else ""
            if self._weak_day_title(day_title):
                soft.append(f"day {day_key} has weak day theme: {day_title or 'empty'}")
            if 0 < len(visit_rows) < 2:
                soft.append(f"day {day_key} has only {len(visit_rows)} visit segment; itinerary may be too thin")
            for row in rows:
                poi = row.get("poi") if isinstance(row.get("poi"), dict) else {}
                key = str(poi.get("name") or row.get("poiName") or row.get("title") or "").strip()
                if key and key in seen_in_day:
                    duplicate_in_day.append(key)
                if key:
                    seen_in_day.add(key)
                if self._is_visit_kind(
                    str(row.get("kind") or row.get("type") or "activity")
                ) and not self._has_practical_notes(
                    str(row.get("notes") or ""),
                    row.get("ticketLookupResultId")
                    or row.get("ticket_lookup_result_id")
                    or str(row.get("id") or "") in structured_practical_segment_ids,
                    row.get("weatherSignalId") or row.get("weather_signal_id"),
                ):
                    practical_note_missing.append(key or str(row.get("id") or "segment"))
            if duplicate_in_day:
                soft.append(f"day {day_key} repeats POIs: {', '.join(sorted(set(duplicate_in_day)))}")
            if len(rows) > 6:
                soft.append(f"day {day_key} has {len(rows)} segments; schedule may be overpacked")
            if practical_note_missing:
                soft.append("missing practical notes: " + ", ".join(dict.fromkeys(practical_note_missing[:6])))
            soft.extend(self._duration_soft_failures_snapshot(rows, day_key))

        expected_transport = self._expected_transport(planning_context)
        if expected_transport:
            mismatched = [
                str(row.get("id") or row.get("startTime") or "segment")
                for row in segment_items
                if str(row.get("transportMode") or "") and str(row.get("transportMode") or "") != expected_transport
            ]
            if mismatched:
                soft.append(f"transport preference mismatch for segments: {', '.join(mismatched[:5])}")

        unresolved_reservation_risks = []
        for row in segment_items:
            notes = str(row.get("notes") or "")
            poi = row.get("poi") if isinstance(row.get("poi"), dict) else {}
            poi_name = str(poi.get("name") or row.get("poiName") or row.get("title") or "").strip()
            if any(marker in notes for marker in ("预约", "门票", "开放", "入园", "入校")) and not any(
                marker in notes for marker in ("已确认", "官方确认", "无需预约")
            ):
                unresolved_reservation_risks.append(poi_name or str(row.get("id") or "segment"))
        if unresolved_reservation_risks:
            soft.append("unresolved reservation risks: " + ", ".join(dict.fromkeys(unresolved_reservation_risks[:6])))

        hard_failures.extend(invalid)
        soft_failures.extend(soft)
        return {
            "name": "basic_itinerary_verifier",
            "status": "passed" if not invalid else "failed",
            "dayCount": len(day_items),
            "segmentCount": len(segment_items),
            "expectedDates": expected_dates,
            "invalid": invalid,
            "softFailures": soft,
            "source": "version_snapshot",
        }

    def _is_visit_kind(self, kind: str) -> bool:
        normalized = kind.strip().lower()
        return normalized not in {"meal", "rest", "transport", "note", "buffer"}

    def _duration_soft_failures_live(self, rows: list[sqlite3.Row], day_id: str) -> list[str]:
        failures: list[str] = []
        for row in rows:
            duration = self._clock_minutes(str(row["end_time"] or "")) - self._clock_minutes(
                str(row["start_time"] or "")
            )
            if duration <= 0 or duration > 360 or (self._is_visit_kind(str(row["kind"] or "")) and duration < 30):
                failures.append(f"day {day_id} segment {row['id']} has abnormal duration {duration} minutes")
        return failures

    def _duration_soft_failures_snapshot(self, rows: list[dict], day_key: str) -> list[str]:
        failures: list[str] = []
        for row in rows:
            start = str(row.get("startTime") or "")
            end = str(row.get("endTime") or "")
            duration = self._clock_minutes(end) - self._clock_minutes(start)
            declared = row.get("durationMinutes")
            if declared is not None:
                try:
                    declared_int = int(declared)
                except (TypeError, ValueError):
                    declared_int = None
                if declared_int is None or duration != declared_int:
                    failures.append(
                        f"day {day_key} segment {row.get('id') or start} endTime does not match durationMinutes"
                    )
            if duration <= 0 or duration > 360 or (self._is_visit_kind(str(row.get("kind") or "")) and duration < 30):
                failures.append(
                    f"day {day_key} segment {row.get('id') or start} has abnormal duration {duration} minutes"
                )
        return failures

    def _clock_minutes(self, value: str) -> int:
        try:
            hour, minute = value[:5].split(":")
            return int(hour) * 60 + int(minute)
        except (ValueError, AttributeError):
            return 0

    def _candidate_first_expected_dates(
        self,
        planning_context: dict,
        expected_dates: list[str],
        actual_dates: Optional[list[str]] = None,
    ) -> list[str]:
        grounding = (
            planning_context.get("candidateFirstGrounding")
            if isinstance(planning_context.get("candidateFirstGrounding"), dict)
            else {}
        )
        unresolved_days = {int(item) for item in grounding.get("unresolvedDays") or [] if str(item).isdigit()}
        if not unresolved_days:
            return expected_dates
        finalization = grounding.get("finalization") if isinstance(grounding.get("finalization"), dict) else {}
        actual_set = {str(item) for item in actual_dates or [] if str(item).strip()}
        if (
            finalization.get("canCreateVersion") is True
            and finalization.get("unresolvedPolicy") == "persist_viable_partial_days"
        ):
            return [
                date
                for index, date in enumerate(expected_dates, start=1)
                if index not in unresolved_days or date in actual_set
            ]
        return [date for index, date in enumerate(expected_dates, start=1) if index not in unresolved_days]

    def _has_practical_notes(
        self,
        notes: str,
        ticket_lookup_result_id: object = None,
        weather_signal_id: object = None,
    ) -> bool:
        if ticket_lookup_result_id or weather_signal_id:
            return True
        if len(notes.strip()) >= 18:
            return True
        return any(
            marker in notes
            for marker in (
                "预约",
                "门票",
                "开放",
                "排队",
                "拥挤",
                "天气",
                "雨",
                "热",
                "冷",
                "交通",
                "地铁",
                "公交",
                "打车",
                "费用",
                "耗时",
                "待校验",
                "needs_verification",
            )
        )

    def _weak_day_title(self, title: str) -> bool:
        normalized = title.strip().lower().replace(" ", "")
        if not normalized:
            return True
        generic_titles = {"day1", "day2", "day3", "第1天", "第2天", "第3天", "行程", "待确认"}
        return normalized in generic_titles or normalized.startswith("day")

    def _expected_transport(self, planning_context: dict) -> str:
        requirements = (
            planning_context.get("understoodRequirements")
            if isinstance(planning_context.get("understoodRequirements"), dict)
            else {}
        )
        fields = requirements.get("fields") if isinstance(requirements.get("fields"), dict) else {}
        transport = str(fields.get("transportPreference") or planning_context.get("latestUserMessage") or "")
        if any(marker in transport for marker in ("公交", "地铁", "公共交通")):
            return "transit"
        if any(marker in transport for marker in ("打车", "出租")):
            return "taxi"
        if "自驾" in transport:
            return "drive"
        if any(marker in transport for marker in ("骑行", "自行车", "自行车出行")):
            return "bicycling"
        if any(marker in transport for marker in ("步行", "走路", "徒步", "步行强度")):
            return "walk"
        return ""

    def _valid_coordinate(self, longitude: object, latitude: object) -> bool:
        try:
            lng = float(longitude)
            lat = float(latitude)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(lng)
            and math.isfinite(lat)
            and -180 <= lng <= 180
            and -90 <= lat <= 90
            and (lng != 0 or lat != 0)
        )

    def _count(self, table: str, where_clause: str, params: tuple) -> int:
        row = self.db.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where_clause}", params).fetchone()
        return int(row["count"])

    def _loads_list(self, value: str) -> list[dict]:
        try:
            payload = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

import math
import json
import re
import sqlite3
from typing import Optional


DEFAULT_DAY_START_MINUTES = 9 * 60
DEFAULT_SEGMENT_DURATION_MINUTES = 90
DEFAULT_ROUTE_BUFFER_MINUTES = 10

# The only server-owned translation from semantic dayparts to hard windows.
# Frontend, pending-slot projection and materializers consume the compiled
# payload and must never duplicate these clock assumptions.
SERVER_DAYPART_WINDOWS: dict[str, tuple[str, str]] = {
    "early_morning": ("08:00", "10:00"),
    "morning": ("09:00", "12:00"),
    "noon": ("12:00", "14:00"),
    "lunch": ("12:00", "14:00"),
    "afternoon": ("14:00", "18:00"),
    "evening": ("18:00", "22:00"),
    "dinner": ("18:00", "21:00"),
    "night": ("18:00", "23:00"),
}


class ItineraryScheduleService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @classmethod
    def schedule_constraint_payload(
        cls,
        *,
        time_window: object,
        start_time: object,
        duration: int,
        intent_type: str,
        source: str = "planning_slot",
    ) -> dict[str, object]:
        window = str(time_window or "").strip()
        semantic_window = SERVER_DAYPART_WINDOWS.get(window.lower())
        if semantic_window is not None:
            window = f"{semantic_window[0]}-{semantic_window[1]}"
        start = cls._parse_time(start_time)
        earliest: Optional[int] = None
        window_end: Optional[int] = None
        if window:
            parts = window.replace("–", "-").split("-", 1)
            if parts:
                earliest = cls._parse_time(parts[0].strip())
            if len(parts) == 2:
                window_end = cls._parse_time(parts[1].strip())
        if earliest is None:
            earliest = start
        if str(intent_type or "").strip().lower() in {"night", "night_view"}:
            earliest = max(18 * 60, earliest if earliest is not None else 18 * 60)
        latest = window_end - max(1, int(duration)) if window_end is not None else None
        if earliest is None and latest is None and window_end is None:
            return {}
        if earliest is not None and latest is not None and earliest > latest:
            raise ValueError("schedule_window_duration_exceeded")
        return {
            "earliestStart": cls._format_time(earliest) if earliest is not None else None,
            "latestStart": cls._format_time(latest) if latest is not None else None,
            "windowEnd": cls._format_time(window_end) if window_end is not None else None,
            "source": source,
            "confidence": 1.0,
            "hard": True,
        }

    @classmethod
    def compiled_materialization_timing(
        cls,
        *,
        time_window: object,
        start_time: object,
        duration: object,
        intent_type: str,
        source: str = "planning_slot",
    ) -> Optional[dict[str, object]]:
        """Return an exact server-compiled placement or ``None`` when time is unproven.

        Materializers must not infer clocks from labels such as ``morning`` or
        ``afternoon``. Domain hard windows (for example night-view lower bounds)
        remain centralized in :meth:`schedule_constraint_payload`.
        """

        try:
            duration_minutes = int(duration)
        except (TypeError, ValueError):
            return None
        if duration_minutes <= 0:
            return None
        constraint = cls.schedule_constraint_payload(
            time_window=time_window,
            start_time=start_time,
            duration=duration_minutes,
            intent_type=intent_type,
            source=source,
        )
        start = cls._parse_time(start_time)
        if start is None:
            start = cls._parse_time(constraint.get("earliestStart"))
        if start is None:
            return None
        return {
            "startMinutes": start,
            "durationMinutes": duration_minutes,
            "scheduleConstraints": constraint,
        }

    @classmethod
    def temporal_failures(cls, segment: dict) -> list[dict[str, object]]:
        start = cls._parse_time(segment.get("startTime"))
        end = cls._parse_time(segment.get("endTime"))
        semantic = (
            segment.get("semanticMetadata")
            if isinstance(segment.get("semanticMetadata"), dict)
            else {}
        )
        intent_type = str(semantic.get("intentType") or segment.get("kind") or "")
        segment_id = str(segment.get("id") or "")
        if start is None or end is None or end <= start:
            return [
                {
                    "code": "schedule.window.invalid_interval",
                    "segmentId": segment_id,
                    "intentType": intent_type,
                }
            ]
        try:
            constraint = cls._normalize_schedule_constraint(
                semantic,
                original_start=start,
                duration=end - start,
                intent_type=intent_type,
            )
        except ValueError as error:
            return [
                {
                    "code": str(error),
                    "segmentId": segment_id,
                    "intentType": intent_type,
                    "scheduledStart": cls._format_time(start),
                    "scheduledEnd": cls._format_time(end),
                }
            ]
        failures: list[dict[str, object]] = []
        earliest = constraint.get("earliestStartMinutes")
        latest = constraint.get("latestStartMinutes")
        window_end = constraint.get("windowEndMinutes")
        hard = constraint.get("hard") is True
        if isinstance(earliest, int) and start < earliest:
            failures.append({"code": "schedule.window.lower_bound", "expected": cls._format_time(earliest)})
        if hard and isinstance(latest, int) and start > latest:
            failures.append({"code": "schedule.window.upper_bound", "expected": cls._format_time(latest)})
        if hard and isinstance(window_end, int) and end > window_end:
            failures.append({"code": "schedule.window.duration_fit", "expected": cls._format_time(window_end)})
        if intent_type in {"night", "night_view"} and start < 18 * 60:
            failures.append({"code": "intent_time.night_view_after_18", "expected": "18:00"})
        return [
            {
                **item,
                "segmentId": segment_id,
                "intentType": intent_type,
                "scheduledStart": cls._format_time(start),
                "scheduledEnd": cls._format_time(end),
            }
            for item in failures
        ]

    def recompute_plan_schedule(
        self,
        plan_id: str,
        *,
        route_pairs: Optional[set[tuple[str, str]]] = None,
    ) -> int:
        day_ids = self._day_ids_for_routes(plan_id, route_pairs=route_pairs)
        updated = 0
        for day_id in day_ids:
            updated += self.recompute_day_schedule(plan_id, day_id)
        return updated

    def recompute_after_route_selection(self, plan_id: str, route_option_id: str) -> int:
        route = self.db.execute(
            """
            SELECT from_segment_id, to_segment_id
            FROM route_options
            WHERE id = ? AND plan_id = ?
            """,
            (route_option_id, plan_id),
        ).fetchone()
        if route is None or not route["from_segment_id"] or not route["to_segment_id"]:
            return 0
        day = self.db.execute(
            """
            SELECT from_segment.day_id AS from_day_id, to_segment.day_id AS to_day_id
            FROM itinerary_segments from_segment
            JOIN itinerary_segments to_segment ON to_segment.id = ?
            WHERE from_segment.id = ?
              AND from_segment.plan_id = ?
              AND to_segment.plan_id = ?
            """,
            (route["to_segment_id"], route["from_segment_id"], plan_id, plan_id),
        ).fetchone()
        if day is None or day["from_day_id"] != day["to_day_id"]:
            return 0
        return self.recompute_day_schedule(plan_id, str(day["from_day_id"]))

    def recompute_day_schedule(self, plan_id: str, day_id: str) -> int:
        segments = self.db.execute(
            """
            SELECT id, start_time, end_time, segment_order, kind, notes,
                   estimate_metadata_json, semantic_metadata_json
            FROM itinerary_segments
            WHERE plan_id = ? AND day_id = ?
            ORDER BY segment_order ASC, id ASC
            """,
            (plan_id, day_id),
        ).fetchall()
        if len(segments) < 2:
            return 0
        selected_routes = self._selected_routes_for_day(plan_id, day_id)
        segment_ids = {str(segment["id"]) for segment in segments}
        route_participants = {
            segment_id
            for to_segment_id, incoming in selected_routes.items()
            for segment_id in [to_segment_id, *[from_segment_id for from_segment_id, _minutes in incoming]]
        }
        prepared: list[dict[str, object]] = []
        for index, segment in enumerate(segments):
            original_start = self._parse_time(segment["start_time"])
            original_end = self._parse_time(segment["end_time"])
            metadata = self._estimate_metadata(segment["estimate_metadata_json"])
            semantic_metadata = self._estimate_metadata(segment["semantic_metadata_json"])
            duration_metadata = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
            user_locked = bool(duration_metadata.get("userLocked"))
            prepared.append(
                {
                    "row": segment,
                    "id": str(segment["id"]),
                    "originalStart": original_start,
                    "duration": self._segment_duration(original_start, original_end),
                    "metadata": metadata,
                    "scheduleConstraint": self._normalize_schedule_constraint(
                        semantic_metadata if semantic_metadata else metadata,
                        original_start=original_start,
                        duration=self._segment_duration(original_start, original_end),
                        intent_type=str(
                            semantic_metadata.get("intentType")
                            or metadata.get("intentType")
                            or segment["kind"]
                            or ""
                        ),
                    ),
                    "userLocked": user_locked,
                    "flexible": not user_locked and self._is_flexible_segment(segment, route_participants),
                    "originalIndex": index,
                }
            )

        scheduled_by_id = self._compute_schedule_assignments(prepared, selected_routes)
        changed_segment_ids: set[str] = set()

        scheduled: list[tuple[str, int, int]] = []
        for item in prepared:
            segment = item["row"]
            segment_id = str(item["id"])
            metadata = item["metadata"] if isinstance(item["metadata"], dict) else {}
            start_minutes, end_minutes, schedule_status = scheduled_by_id[segment_id]
            scheduled.append((segment_id, start_minutes, int(segment["segment_order"] or int(item["originalIndex"]) + 1)))
            next_start = self._format_time(start_minutes)
            next_end = self._format_time(end_minutes)
            if next_start != segment["start_time"] or next_end != segment["end_time"]:
                self.db.execute(
                    """
                    UPDATE itinerary_segments
                    SET start_time = ?, end_time = ?
                    WHERE id = ? AND plan_id = ?
                    """,
                    (next_start, next_end, segment_id, plan_id),
                )
                changed_segment_ids.add(segment_id)
            schedule = metadata.get("schedule") if isinstance(metadata.get("schedule"), dict) else {}
            schedule.update(
                {
                    "status": schedule_status,
                    "routeBufferMinutes": DEFAULT_ROUTE_BUFFER_MINUTES if schedule_status == "committed_route_evidence" else 0,
                }
            )
            if item.get("scheduleConstraint"):
                schedule["constraintPassed"] = True
            metadata["schedule"] = schedule
            self.db.execute(
                "UPDATE itinerary_segments SET estimate_metadata_json = ? WHERE id = ? AND plan_id = ?",
                (json.dumps(metadata, ensure_ascii=False), segment_id, plan_id),
            )

        # segment_order is the canonical API/display/route ordering contract.
        # Re-number after route-derived times move a segment earlier or later.
        canonical = sorted(scheduled, key=lambda item: (item[1], item[2], item[0]))
        for next_order, (segment_id, _start, previous_order) in enumerate(canonical, start=1):
            if previous_order != next_order:
                self.db.execute(
                    "UPDATE itinerary_segments SET segment_order = ? WHERE id = ? AND plan_id = ?",
                    (next_order, segment_id, plan_id),
                )
                changed_segment_ids.add(segment_id)
        return len(changed_segment_ids)

    @classmethod
    def project_snapshot_schedule(cls, snapshot: dict, route_evidence: list[dict]) -> None:
        """Project proposal times with the exact pure algorithm used by persistence."""
        selected_routes: dict[str, list[tuple[str, int]]] = {}
        for item in route_evidence:
            if not isinstance(item, dict) or item.get("status") not in {"verified", "selected"}:
                continue
            from_segment_id = str(item.get("fromSegmentId") or "")
            to_segment_id = str(item.get("toSegmentId") or "")
            if not from_segment_id or not to_segment_id:
                continue
            selected_routes.setdefault(to_segment_id, []).append((from_segment_id, max(0, int(item.get("durationMinutes") or 0))))
        route_participants = {
            segment_id
            for to_segment_id, incoming in selected_routes.items()
            for segment_id in [to_segment_id, *[from_segment_id for from_segment_id, _minutes in incoming]]
        }
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            segments = [item for item in (day.get("segments") or []) if isinstance(item, dict)]
            prepared: list[dict[str, object]] = []
            for index, segment in enumerate(segments):
                original_start = cls._parse_time(segment.get("startTime"))
                original_end = cls._parse_time(segment.get("endTime"))
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                duration_metadata = semantic.get("duration") if isinstance(semantic.get("duration"), dict) else {}
                schedule_metadata = semantic.get("schedule") if isinstance(semantic.get("schedule"), dict) else {}
                user_locked = bool(segment.get("userLocked") or semantic.get("userLocked") or duration_metadata.get("userLocked") or schedule_metadata.get("userLocked"))
                prepared.append({
                    "segment": segment,
                    "id": str(segment.get("id") or f"snapshot_{index}"),
                    "originalStart": original_start,
                    "duration": cls._segment_duration(original_start, original_end),
                    "scheduleConstraint": cls._normalize_schedule_constraint(
                        semantic,
                        original_start=original_start,
                        duration=cls._segment_duration(original_start, original_end),
                        intent_type=str(semantic.get("intentType") or segment.get("kind") or ""),
                    ),
                    "userLocked": user_locked,
                    "flexible": not user_locked and cls._is_snapshot_flexible(segment, route_participants),
                    "originalIndex": index,
                })
            scheduled_by_id = cls._compute_schedule_assignments(prepared, selected_routes)
            for item in prepared:
                segment = item["segment"]
                if not isinstance(segment, dict):
                    continue
                start, end, schedule_status = scheduled_by_id[str(item["id"])]
                segment["startTime"] = cls._format_time(start)
                segment["endTime"] = cls._format_time(end)
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                schedule = semantic.get("schedule") if isinstance(semantic.get("schedule"), dict) else {}
                schedule.update({
                    "status": schedule_status,
                    "routeBufferMinutes": DEFAULT_ROUTE_BUFFER_MINUTES if schedule_status == "committed_route_evidence" else 0,
                })
                if item.get("scheduleConstraint"):
                    schedule["constraintPassed"] = True
                semantic["schedule"] = schedule
                segment["semanticMetadata"] = semantic
            day["segments"] = sorted(segments, key=lambda item: (cls._parse_time(item.get("startTime")) or DEFAULT_DAY_START_MINUTES, int(item.get("segmentOrder") or 0), str(item.get("id") or "")))
        snapshot["portfolioScheduleProjection"] = {
            "routeBufferMinutes": DEFAULT_ROUTE_BUFFER_MINUTES,
            "usesVerifiedRouteEvidence": bool(selected_routes),
        }

    @classmethod
    def _compute_schedule_assignments(
        cls,
        prepared: list[dict[str, object]],
        selected_routes: dict[str, list[tuple[str, int]]],
    ) -> dict[str, tuple[int, int, str]]:
        """Pure timing kernel shared by proposal projection and SQLite persistence."""
        segment_ids = {str(item["id"]) for item in prepared}
        end_by_segment: dict[str, int] = {}
        scheduled_by_id: dict[str, tuple[int, int, str]] = {}
        previous_committed_end: Optional[int] = None
        for item in [candidate for candidate in prepared if not candidate["flexible"]]:
            segment_id = str(item["id"])
            original_start = item["originalStart"] if isinstance(item["originalStart"], int) else None
            duration = int(item["duration"])
            user_locked = bool(item["userLocked"])
            constraint = (
                item.get("scheduleConstraint")
                if isinstance(item.get("scheduleConstraint"), dict)
                else {}
            )
            earliest_start = (
                int(constraint["earliestStartMinutes"])
                if isinstance(constraint.get("earliestStartMinutes"), int)
                else None
            )
            if previous_committed_end is None:
                start_minutes = original_start if original_start is not None else DEFAULT_DAY_START_MINUTES
                schedule_status = "user_locked" if user_locked else "scheduled_without_incoming_route"
            else:
                incoming_route_start = cls._incoming_route_start(segment_id, selected_routes, end_by_segment, segment_ids)
                if user_locked and original_start is not None:
                    start_minutes = original_start
                    schedule_status = "user_locked"
                elif incoming_route_start is not None:
                    start_minutes = max(
                        incoming_route_start,
                        previous_committed_end,
                        earliest_start if earliest_start is not None else 0,
                        original_start
                        if constraint.get("hard") is True and original_start is not None
                        else 0,
                    )
                    schedule_status = "committed_route_evidence"
                else:
                    start_minutes = max(original_start if original_start is not None else previous_committed_end, previous_committed_end, 0)
                    schedule_status = "provisional_missing_route"
            if earliest_start is not None:
                start_minutes = max(start_minutes, earliest_start)
            end_minutes = start_minutes + duration
            cls._validate_schedule_constraint(segment_id, start_minutes, end_minutes, constraint)
            if end_minutes >= 24 * 60:
                raise ValueError(f"schedule crosses midnight for segment {segment_id}")
            end_by_segment[segment_id] = end_minutes
            previous_committed_end = max(previous_committed_end or 0, end_minutes)
            scheduled_by_id[segment_id] = (start_minutes, end_minutes, schedule_status)
        occupied = sorted((start, end) for start, end, _status in scheduled_by_id.values())
        for item in sorted([candidate for candidate in prepared if candidate["flexible"]], key=lambda candidate: (int(candidate["originalStart"]) if isinstance(candidate["originalStart"], int) else DEFAULT_DAY_START_MINUTES, int(candidate["originalIndex"]))):
            segment_id = str(item["id"])
            duration = int(item["duration"])
            constraint = (
                item.get("scheduleConstraint")
                if isinstance(item.get("scheduleConstraint"), dict)
                else {}
            )
            earliest_start = (
                int(constraint["earliestStartMinutes"])
                if isinstance(constraint.get("earliestStartMinutes"), int)
                else None
            )
            preferred_start = int(item["originalStart"]) if isinstance(item["originalStart"], int) else DEFAULT_DAY_START_MINUTES
            if earliest_start is not None:
                preferred_start = max(preferred_start, earliest_start)
            start_minutes = cls._first_available_start(preferred_start, duration, occupied)
            end_minutes = start_minutes + duration
            cls._validate_schedule_constraint(segment_id, start_minutes, end_minutes, constraint)
            if end_minutes >= 24 * 60:
                raise ValueError(f"schedule crosses midnight for segment {segment_id}")
            occupied.append((start_minutes, end_minutes))
            occupied.sort()
            scheduled_by_id[segment_id] = (start_minutes, end_minutes, "flexible_slot_scheduled")
        return scheduled_by_id

    @classmethod
    def _normalize_schedule_constraint(
        cls,
        metadata: dict,
        *,
        original_start: Optional[int],
        duration: int,
        intent_type: str,
    ) -> dict[str, object]:
        raw = (
            metadata.get("scheduleConstraints")
            if isinstance(metadata.get("scheduleConstraints"), dict)
            else {}
        )
        earliest = cls._parse_time(raw.get("earliestStart"))
        latest = cls._parse_time(raw.get("latestStart"))
        window_end = cls._parse_time(raw.get("windowEnd"))
        time_window = str(raw.get("timeWindow") or metadata.get("timeWindow") or "")
        if time_window:
            parts = time_window.replace("–", "-").split("-", 1)
            if earliest is None and parts:
                earliest = cls._parse_time(parts[0].strip())
            if window_end is None and len(parts) == 2:
                window_end = cls._parse_time(parts[1].strip())
        if latest is None and window_end is not None:
            latest = window_end - duration
        normalized_intent = str(intent_type or "").strip().lower()
        if normalized_intent == "night_view" or normalized_intent == "night":
            earliest = max(18 * 60, earliest if earliest is not None else 18 * 60)
        hard = bool(raw.get("hard")) or normalized_intent in {"night_view", "night"}
        if hard and raw and earliest is None and original_start is not None:
            earliest = original_start
        if earliest is not None and latest is not None and earliest > latest:
            raise ValueError("schedule_window_bounds_invalid")
        if window_end is not None and earliest is not None and earliest + duration > window_end:
            raise ValueError("schedule_window_duration_exceeded")
        if not raw and normalized_intent not in {"night_view", "night"}:
            return {}
        return {
            "earliestStartMinutes": earliest,
            "latestStartMinutes": latest,
            "windowEndMinutes": window_end,
            "hard": hard,
            "source": str(raw.get("source") or ("intent_time_policy" if normalized_intent in {"night_view", "night"} else "schedule_metadata")),
        }

    @staticmethod
    def _validate_schedule_constraint(
        segment_id: str,
        start_minutes: int,
        end_minutes: int,
        constraint: dict[str, object],
    ) -> None:
        if not constraint:
            return
        earliest = constraint.get("earliestStartMinutes")
        latest = constraint.get("latestStartMinutes")
        window_end = constraint.get("windowEndMinutes")
        if isinstance(earliest, int) and start_minutes < earliest:
            raise ValueError(f"schedule_window_earliest_start_violated:{segment_id}")
        if constraint.get("hard") is True and isinstance(latest, int) and start_minutes > latest:
            raise ValueError(f"schedule_window_latest_start_exceeded:{segment_id}")
        if constraint.get("hard") is True and isinstance(window_end, int) and end_minutes > window_end:
            raise ValueError(f"schedule_window_end_exceeded:{segment_id}")

    @staticmethod
    def _is_snapshot_flexible(segment: dict, route_participants: set[str]) -> bool:
        segment_id = str(segment.get("id") or "")
        if segment_id in route_participants:
            return False
        kind = str(segment.get("kind") or "")
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        notes = " ".join(str(value or "") for value in (segment.get("notes"), semantic.get("notes"), semantic.get("groundingStatus")))
        explicitly_flexible = bool(semantic.get("routeAnchor") is False or "routeAnchor=false" in notes or re.search(r"(waiting_for_poi_grounding|optional_waiting|pendingMeal=true|needsConcretePoi=true)", notes))
        return explicitly_flexible or kind in {"meal", "rest", "note", "buffer", "travel_buffer"}
    @staticmethod
    def _is_flexible_segment(segment: sqlite3.Row, route_participants: set[str]) -> bool:
        segment_id = str(segment["id"])
        if segment_id in route_participants:
            return False
        kind = str(segment["kind"] or "")
        notes = str(segment["notes"] or "")
        explicitly_flexible = bool(
            "routeAnchor=false" in notes
            or re.search(r"(waiting_for_poi_grounding|optional_waiting|pendingMeal=true|needsConcretePoi=true)", notes)
        )
        return explicitly_flexible or kind in {"meal", "rest", "note", "buffer", "travel_buffer"}

    @staticmethod
    def _first_available_start(preferred_start: int, duration: int, occupied: list[tuple[int, int]]) -> int:
        candidate = max(0, preferred_start)
        for start, end in sorted(occupied):
            if candidate + duration <= start:
                return candidate
            if candidate >= end:
                continue
            candidate = end
        return candidate

    @staticmethod
    def _estimate_metadata(value: object) -> dict:
        try:
            parsed = json.loads(str(value or "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _day_ids_for_routes(
        self,
        plan_id: str,
        *,
        route_pairs: Optional[set[tuple[str, str]]] = None,
    ) -> list[str]:
        if not route_pairs:
            rows = self.db.execute(
                "SELECT id FROM itinerary_days WHERE plan_id = ? ORDER BY day_number ASC, id ASC",
                (plan_id,),
            ).fetchall()
            return [str(row["id"]) for row in rows]
        params: list[object] = [plan_id]
        pair_filter = ""
        if route_pairs:
            pair_filter = "AND (" + " OR ".join(
                "(route_options.from_segment_id = ? AND route_options.to_segment_id = ?)"
                for _ in route_pairs
            ) + ")"
            for from_segment_id, to_segment_id in route_pairs:
                params.extend([from_segment_id, to_segment_id])
        rows = self.db.execute(
            f"""
            SELECT DISTINCT from_segment.day_id AS day_id
            FROM route_options
            JOIN itinerary_segments from_segment ON from_segment.id = route_options.from_segment_id
            JOIN itinerary_segments to_segment ON to_segment.id = route_options.to_segment_id
            WHERE route_options.plan_id = ?
              AND route_options.is_selected = 1
              AND route_options.error_json IS NULL
              AND from_segment.day_id = to_segment.day_id
              {pair_filter}
            ORDER BY from_segment.day_id
            """,
            tuple(params),
        ).fetchall()
        day_ids = {str(row["day_id"]) for row in rows}
        # A POI replacement can invalidate the only selected route for a day.
        # The schedule must still cascade from the changed segment instead of
        # silently keeping stale downstream times until route data returns.
        if route_pairs:
            placeholders = ",".join("?" for _ in range(len(route_pairs) * 2))
            segment_ids = [segment_id for pair in route_pairs for segment_id in pair]
            impacted = self.db.execute(
                f"""
                SELECT DISTINCT day_id
                FROM itinerary_segments
                WHERE plan_id = ? AND id IN ({placeholders})
                """,
                (plan_id, *segment_ids),
            ).fetchall()
            day_ids.update(str(row["day_id"]) for row in impacted)
        return sorted(day_ids)

    def _selected_routes_for_day(self, plan_id: str, day_id: str) -> dict[str, list[tuple[str, int]]]:
        rows = self.db.execute(
            """
            SELECT route_options.from_segment_id, route_options.to_segment_id, route_options.duration_seconds
            FROM route_options
            JOIN itinerary_segments from_segment ON from_segment.id = route_options.from_segment_id
            JOIN itinerary_segments to_segment ON to_segment.id = route_options.to_segment_id
            WHERE route_options.plan_id = ?
              AND route_options.is_selected = 1
              AND route_options.error_json IS NULL
              AND from_segment.day_id = ?
              AND to_segment.day_id = ?
              AND route_options.from_segment_id IS NOT NULL
              AND route_options.to_segment_id IS NOT NULL
            """,
            (plan_id, day_id, day_id),
        ).fetchall()
        routes: dict[str, list[tuple[str, int]]] = {}
        for row in rows:
            from_segment_id = str(row["from_segment_id"])
            to_segment_id = str(row["to_segment_id"])
            travel_minutes = max(0, math.ceil(int(row["duration_seconds"] or 0) / 60))
            routes.setdefault(to_segment_id, []).append((from_segment_id, travel_minutes))
        return routes

    @staticmethod
    def _incoming_route_start(
        segment_id: str,
        selected_routes: dict[str, list[tuple[str, int]]],
        end_by_segment: dict[str, int],
        segment_ids: set[str],
    ) -> Optional[int]:
        starts: list[int] = []
        for from_segment_id, travel_minutes in selected_routes.get(segment_id, []):
            if from_segment_id not in segment_ids or from_segment_id not in end_by_segment:
                continue
            starts.append(end_by_segment[from_segment_id] + travel_minutes + DEFAULT_ROUTE_BUFFER_MINUTES)
        return max(starts) if starts else None

    @staticmethod
    def _segment_duration(start_minutes: Optional[int], end_minutes: Optional[int]) -> int:
        if start_minutes is None or end_minutes is None or end_minutes <= start_minutes:
            return DEFAULT_SEGMENT_DURATION_MINUTES
        return max(15, end_minutes - start_minutes)

    @staticmethod
    def _parse_time(value: object) -> Optional[int]:
        if not value:
            return None
        parts = str(value).split(":", 1)
        if len(parts) != 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour * 60 + minute

    @staticmethod
    def _format_time(minutes: int) -> str:
        minutes = max(0, int(minutes))
        hour = minutes // 60
        minute = minutes % 60
        return f"{hour:02d}:{minute:02d}"

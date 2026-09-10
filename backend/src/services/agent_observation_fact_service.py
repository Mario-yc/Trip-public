from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ScheduleOverlap:
    day_number: int
    segment_ids: tuple[str, str]
    minutes: int


@dataclass(frozen=True)
class ScheduleFacts:
    overlap_count: int
    overlaps: list[ScheduleOverlap]
    invalid_interval_count: int = 0


@dataclass(frozen=True)
class CoverageFacts:
    unresolved_required_count: int
    unresolved_optional_count: int


class AgentObservationFactService:
    """Shared structural facts for observation and verifier consumers."""

    @staticmethod
    def is_required(segment: dict[str, Any]) -> bool:
        return segment.get("requirementLevel") == "required"

    @staticmethod
    def is_route_anchor(segment: dict[str, Any]) -> bool:
        return segment.get("routeAnchor") is True

    def schedule_facts(self, segments: list[dict[str, Any]]) -> ScheduleFacts:
        by_day: dict[int, list[tuple[int, int, str]]] = {}
        invalid = 0
        for segment in segments:
            try:
                day = int(segment.get("dayNumber"))
                start = self._minutes(str(segment.get("startTime") or ""))
                end = self._minutes(str(segment.get("endTime") or ""))
                if end <= start:
                    invalid += 1
                    continue
                by_day.setdefault(day, []).append((start, end, str(segment.get("id") or "")))
            except (TypeError, ValueError):
                invalid += 1
        overlaps: list[ScheduleOverlap] = []
        for day, intervals in by_day.items():
            intervals.sort()
            for index, (start, end, segment_id) in enumerate(intervals):
                for other_start, other_end, other_id in intervals[index + 1 :]:
                    if other_start >= end:
                        break
                    minutes = min(end, other_end) - max(start, other_start)
                    if minutes > 0:
                        overlaps.append(ScheduleOverlap(day, (segment_id, other_id), minutes))
        return ScheduleFacts(len(overlaps), overlaps, invalid)

    def coverage_facts(self, segments: list[dict[str, Any]]) -> CoverageFacts:
        unresolved_required = 0
        unresolved_optional = 0
        for segment in segments:
            unresolved = segment.get("groundingStatus") in {
                "waiting",
                "waiting_for_poi_grounding",
                "unresolved",
                "draft",
            }
            if not unresolved:
                continue
            if self.is_required(segment):
                unresolved_required += 1
            else:
                unresolved_optional += 1
        return CoverageFacts(unresolved_required, unresolved_optional)

    @staticmethod
    def version_invariants(
        *,
        active_version_id: str | None,
        lineage_current_version_id: str | None,
        reloaded_version_id: str | None,
    ) -> list[str]:
        errors: list[str] = []
        if active_version_id != lineage_current_version_id:
            errors.append("active_version_lineage_mismatch")
        if active_version_id != reloaded_version_id:
            errors.append("active_version_reload_mismatch")
        return errors

    @staticmethod
    def _minutes(value: str) -> int:
        parsed = datetime.strptime(value, "%H:%M")
        return parsed.hour * 60 + parsed.minute


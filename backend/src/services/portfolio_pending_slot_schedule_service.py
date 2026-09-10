"""Project metadata-only pending slots from server-compiled time constraints."""

from __future__ import annotations

import copy
from typing import Any, Optional


class PortfolioPendingSlotScheduleService:
    """Place pending metadata without inferring domain-specific clock times."""

    def project(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(snapshot)
        days = {
            int(day.get("dayNumber") or 0): day
            for day in result.get("days") or []
            if isinstance(day, dict) and int(day.get("dayNumber") or 0) > 0
        }
        compiled = self._compiled_constraints(result)
        occupied_by_day = {
            day_number: self._occupied(day) for day_number, day in days.items()
        }
        projected: list[dict[str, Any]] = []
        for raw in result.get("portfolioPendingSlots") or []:
            if not isinstance(raw, dict):
                continue
            slot = copy.deepcopy(raw)
            day_number = int(slot.get("dayNumber") or 0)
            key = self._slot_key(slot)
            constraint, basis = self._constraint(slot, compiled.get(key))
            resolved = self._resolve_window(constraint)
            if resolved is None:
                for field_name in ("startTime", "endTime", "durationMinutes"):
                    slot.pop(field_name, None)
                slot.update(
                    {
                        "timingStatus": "time_pending",
                        "timingLabel": "时间待定",
                        "timingBasis": basis,
                        "constraintSummary": "服务器尚未确定该待补槽位的具体时间。",
                        "placementAfterSegmentId": None,
                        "placementBeforeSegmentId": None,
                    }
                )
                projected.append(slot)
                continue
            preferred, window_start, window_end, duration = resolved
            occupied = occupied_by_day.setdefault(day_number, [])
            start = self._first_available_start(preferred, duration, occupied, window_end)
            timing_status = "awaiting_route_confirmation"
            if start is None:
                start = preferred
                timing_status = "schedule_conflict_pending"
            end = start + duration
            previous_id, next_id = self._neighbors(start, end, occupied)
            slot.update(
                {
                    "timeWindow": str(slot.get("timeWindow") or "").strip()
                    or (
                        f"{self._format_time(window_start)}-"
                        f"{self._format_time(window_end)}"
                    ),
                    "startTime": self._format_time(start),
                    "endTime": self._format_time(end),
                    "durationMinutes": duration,
                    "timingStatus": timing_status,
                    "timingLabel": f"{self._format_time(start)}–{self._format_time(end)}",
                    "timingBasis": basis,
                    "constraintSummary": (
                        "按服务器编译的时间约束安排；当前约束 "
                        f"{self._format_time(window_start)}–{self._format_time(window_end)}"
                    ),
                    "placementAfterSegmentId": previous_id,
                    "placementBeforeSegmentId": next_id,
                }
            )
            projected.append(slot)
            occupied.append((start, end, str(slot.get("id") or "")))
            occupied.sort(key=lambda item: (item[0], item[1], item[2]))
        projected.sort(
            key=lambda item: (
                int(item.get("dayNumber") or 0),
                1 if item.get("timingStatus") == "time_pending" else 0,
                self._parse_time(item.get("startTime")) or 0,
                str(item.get("planningSlotId") or ""),
            )
        )
        result["portfolioPendingSlots"] = projected
        return result

    @classmethod
    def _constraint(
        cls,
        slot: dict[str, Any],
        compiled: Optional[dict[str, Any]],
    ) -> tuple[dict[str, Any], str]:
        semantic = (
            slot.get("semanticMetadata")
            if isinstance(slot.get("semanticMetadata"), dict)
            else {}
        )
        semantic_schedule = (
            semantic.get("scheduleConstraints")
            if isinstance(semantic.get("scheduleConstraints"), dict)
            else None
        )
        direct_schedule = (
            slot.get("scheduleConstraints")
            if isinstance(slot.get("scheduleConstraints"), dict)
            else None
        )
        if semantic_schedule:
            return copy.deepcopy(semantic_schedule), "semantic_schedule_constraints"
        if direct_schedule:
            return copy.deepcopy(direct_schedule), "slot_schedule_constraints"
        exact = {
            key: slot.get(key)
            for key in (
                "startTime",
                "endTime",
                "timeWindow",
                "durationMinutes",
                "earliestStart",
                "latestEnd",
                "windowEnd",
            )
            if slot.get(key) not in (None, "")
        }
        if exact:
            return exact, "planning_slot_constraint"
        if isinstance(compiled, dict) and compiled:
            return copy.deepcopy(compiled), "compiled_request_constraint"
        return {}, "time_pending"

    @classmethod
    def _resolve_window(
        cls,
        constraint: dict[str, Any],
    ) -> Optional[tuple[int, int, int, int]]:
        raw_window = str(constraint.get("timeWindow") or "").strip()
        exact_start = cls._parse_time(constraint.get("startTime"))
        earliest = cls._parse_time(
            constraint.get("earliestStart") or constraint.get("startTime")
        )
        window_end = cls._parse_time(
            constraint.get("latestEnd")
            or constraint.get("windowEnd")
            or constraint.get("endTime")
        )
        if raw_window:
            parts = raw_window.replace("–", "-").split("-", 1)
            if len(parts) == 2:
                earliest = cls._parse_time(parts[0].strip())
                window_end = cls._parse_time(parts[1].strip())
        if earliest is None or window_end is None or window_end <= earliest:
            return None
        raw_duration = constraint.get("durationMinutes")
        try:
            duration = int(raw_duration) if raw_duration not in (None, "") else window_end - earliest
        except (TypeError, ValueError):
            return None
        if duration <= 0 or earliest + duration > window_end:
            return None
        preferred = exact_start if exact_start is not None else earliest
        if preferred < earliest or preferred + duration > window_end:
            preferred = earliest
        return preferred, earliest, window_end, duration

    @classmethod
    def _compiled_constraints(
        cls,
        snapshot: dict[str, Any],
    ) -> dict[tuple[str, str, str, int], dict[str, Any]]:
        result: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for container_name in (
            "portfolioPendingSlotScheduleConstraints",
            "compiledPendingSlotConstraints",
        ):
            for item in snapshot.get(container_name) or []:
                if isinstance(item, dict):
                    result[cls._slot_key(item)] = item
        return result

    @staticmethod
    def _slot_key(value: dict[str, Any]) -> tuple[str, str, str, int]:
        return (
            str(value.get("briefId") or value.get("creativeBriefId") or ""),
            str(value.get("poolId") or ""),
            str(value.get("planningSlotId") or value.get("slotId") or ""),
            int(value.get("dayNumber") or 0),
        )

    @classmethod
    def _occupied(cls, day: dict[str, Any]) -> list[tuple[int, int, str]]:
        occupied: list[tuple[int, int, str]] = []
        for segment in day.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            start = cls._parse_time(segment.get("startTime"))
            end = cls._parse_time(segment.get("endTime"))
            if start is None or end is None or end <= start:
                continue
            occupied.append((start, end, str(segment.get("id") or "")))
        return sorted(occupied, key=lambda item: (item[0], item[1], item[2]))

    @staticmethod
    def _first_available_start(
        preferred: int,
        duration: int,
        occupied: list[tuple[int, int, str]],
        window_end: int,
    ) -> Optional[int]:
        candidate = preferred
        for start, end, _segment_id in occupied:
            if end <= candidate:
                continue
            if candidate + duration <= start:
                break
            candidate = end
        return candidate if candidate + duration <= window_end else None

    @staticmethod
    def _neighbors(
        start: int,
        end: int,
        occupied: list[tuple[int, int, str]],
    ) -> tuple[Optional[str], Optional[str]]:
        previous = [item for item in occupied if item[1] <= start]
        following = [item for item in occupied if item[0] >= end]
        previous_id = max(previous, key=lambda item: item[1])[2] if previous else None
        next_id = min(following, key=lambda item: item[0])[2] if following else None
        return previous_id or None, next_id or None

    @staticmethod
    def _parse_time(value: object) -> Optional[int]:
        if not value or ":" not in str(value):
            return None
        try:
            hour, minute = (int(part) for part in str(value).split(":", 1))
        except (TypeError, ValueError):
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour * 60 + minute

    @staticmethod
    def _format_time(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

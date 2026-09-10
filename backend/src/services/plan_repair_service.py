"""One bounded, deterministic optional-only repair for proposal staging."""
from __future__ import annotations

import copy
from typing import Any

from src.services.plan_critic_service import PlanDefect


class PlanRepairService:
    """Never calls providers and never changes a required segment."""

    def repair_once(self, snapshot: dict[str, Any], defects: list[PlanDefect]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        repairable = [item for item in defects if item.repair_scope in {"remove_optional", "move_or_remove_optional"}]
        if not repairable:
            return None, {"attempted": False, "reason": "no_repairable_defect"}
        repaired = copy.deepcopy(snapshot)
        for day in repaired.get("days") or []:
            segments = day.get("segments") if isinstance(day, dict) else None
            if not isinstance(segments, list):
                continue
            for segment in segments:
                semantic = segment.get("semanticMetadata") if isinstance(segment, dict) else {}
                if not isinstance(semantic, dict) or not semantic.get("portfolioOptional"):
                    continue
                if semantic.get("required"):
                    return None, {"attempted": True, "reason": "repair_would_touch_required"}
                # Keep the optional experience but move it after all existing
                # segments on its assigned day. This is deterministic and does
                # not require a new map or route request.
                latest = max((self._minutes(item.get("endTime")) or 0 for item in segments if item is not segment), default=0)
                start = min(max(latest + 15, 9 * 60), 21 * 60)
                duration = max(30, (self._minutes(segment.get("endTime")) or start + 60) - (self._minutes(segment.get("startTime")) or start))
                if start + duration > 22 * 60:
                    segments.remove(segment)
                    return repaired, {"attempted": True, "action": "remove_optional", "providerCalls": 0}
                segment["startTime"] = self._clock(start)
                segment["endTime"] = self._clock(start + duration)
                semantic["repairSuggested"] = False
                semantic["repairAction"] = "reposition_optional"
                return repaired, {"attempted": True, "action": "reposition_optional", "providerCalls": 0}
        return None, {"attempted": True, "reason": "no_optional_segment"}

    @staticmethod
    def _minutes(value: Any) -> int | None:
        try:
            hour, minute = (int(item) for item in str(value).split(":"))
            return hour * 60 + minute
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

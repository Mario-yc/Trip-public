"""Central same-day duplicate and cross-day POI reuse policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CrossDayPoiReuseDecision:
    allowed: bool
    penalty: float
    previous_day_numbers: list[int]
    reuse_allowed_reason: str
    explicit_repeat_requested: bool
    reason_code: str

    def evidence(self) -> dict[str, Any]:
        return {
            "crossDayReusePenalty": self.penalty,
            "previousDayNumbers": list(self.previous_day_numbers),
            "reuseAllowedReason": self.reuse_allowed_reason,
            "explicitRepeatRequested": self.explicit_repeat_requested,
            "reuseReasonCode": self.reason_code,
        }


class CrossDayPoiReusePolicy:
    SAME_AMAP_PENALTY = 0.35
    SAME_CANONICAL_PENALTY = 0.20
    SAME_FAMILY_PENALTY = 0.05

    def evaluate(
        self,
        *,
        amap_id: str,
        canonical_entity: str,
        family: str,
        day_number: int,
        prior_occurrences: Iterable[dict[str, Any]],
        explicit_repeat_requested: bool = False,
    ) -> CrossDayPoiReuseDecision:
        prior = [dict(item) for item in prior_occurrences if isinstance(item, dict)]
        same_amap = [item for item in prior if amap_id and str(item.get("amapId") or "") == amap_id]
        same_canonical = [
            item for item in prior
            if canonical_entity and str(item.get("canonicalEntity") or "") == canonical_entity
        ]
        same_family = [item for item in prior if family and str(item.get("family") or "") == family]
        same_day_amap = any(int(item.get("dayNumber") or 0) == day_number for item in same_amap)
        same_day_canonical = any(
            int(item.get("dayNumber") or 0) == day_number for item in same_canonical
        )
        previous_days = sorted(
            {
                int(item.get("dayNumber") or 0)
                for item in [*same_amap, *same_canonical, *same_family]
                if int(item.get("dayNumber") or 0) > 0
                and int(item.get("dayNumber") or 0) != day_number
            }
        )
        if same_day_amap:
            return CrossDayPoiReuseDecision(
                False, 0.0, previous_days, "same_day_duplicate_rejected",
                explicit_repeat_requested, "same_day_duplicate_amap",
            )
        if same_day_canonical:
            return CrossDayPoiReuseDecision(
                False, 0.0, previous_days, "same_day_duplicate_rejected",
                explicit_repeat_requested, "same_day_duplicate_canonical_entity",
            )
        if explicit_repeat_requested and (same_amap or same_canonical or same_family):
            return CrossDayPoiReuseDecision(
                True, 0.0, previous_days, "explicit_repeat_requested", True,
                "cross_day_explicit_repeat",
            )
        if same_amap:
            return CrossDayPoiReuseDecision(
                True, self.SAME_AMAP_PENALTY, previous_days,
                "cross_day_same_amap_allowed", False, "cross_day_same_amap",
            )
        if same_canonical:
            return CrossDayPoiReuseDecision(
                True, self.SAME_CANONICAL_PENALTY, previous_days,
                "cross_day_canonical_entity_allowed", False,
                "cross_day_same_canonical_entity",
            )
        if same_family:
            return CrossDayPoiReuseDecision(
                True, self.SAME_FAMILY_PENALTY, previous_days,
                "cross_day_family_reuse_allowed", False, "cross_day_same_family",
            )
        return CrossDayPoiReuseDecision(
            True, 0.0, [], "not_reused", explicit_repeat_requested, "not_reused",
        )

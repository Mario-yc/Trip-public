"""Plan bounded AMap demand before candidate collection.

The plan contains no provider I/O.  It is a stable ordering and dedupe contract
for the existing candidate collector, so cold and warm cache only differ in
latency/call count, never in which essential occurrence is attempted first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class AmapPlanningDemand:
    pool_id: str
    query_family: str
    priority: int
    reason: str


class AmapPlanningDemandService:
    _ORDER = {
        "hard_occurrence": 0,
        "explicit_soft_occurrence": 1,
        "defining_theme_occurrence": 2,
        "inferred_preferred_occurrence": 3,
        "required_nearby_route_verification": 4,
        "brief_optional": 5,
        "extra_variant": 6,
    }

    def plan(self, pools: Iterable[Any], occurrence_plan: dict[str, Any] | None = None) -> list[AmapPlanningDemand]:
        occurrences = [item for item in (occurrence_plan or {}).get("occurrences") or [] if isinstance(item, dict)]
        levels_by_intent: dict[str, set[str]] = {}
        for item in occurrences:
            intent = str(item.get("intentType") or "")
            if intent:
                levels_by_intent.setdefault(intent, set()).add(
                    str(item.get("requirementLevel") or "optional")
                )
        recurring_intents = {
            str(item.get("intentType") or "")
            for item in occurrences
            if item.get("distinctGroupId")
        }
        demand: list[AmapPlanningDemand] = []
        seen: set[tuple[str, str, str]] = set()
        for pool in pools:
            intent = str(getattr(pool, "intent_type", "") or "")
            raw = str(getattr(pool, "raw_need", "") or "").strip().casefold()
            city = str(getattr(pool, "city", "") or "").strip().casefold()
            key = (city, intent, raw)
            if key in seen:
                continue
            seen.add(key)
            levels = levels_by_intent.get(intent, set())
            if "hard" in levels:
                # One text search serves every same-family hard occurrence.  The
                # later identity assignment consumes distinctGroupId evidence.
                reason = "hard_occurrence"
            elif "explicit_soft" in levels:
                reason = "explicit_soft_occurrence"
            elif "defining_theme" in levels:
                reason = "defining_theme_occurrence"
            elif "inferred_preferred" in levels:
                reason = "inferred_preferred_occurrence"
            elif intent in {"meal", "local_food"}:
                reason = "required_nearby_route_verification"
            else:
                reason = "brief_optional"
            demand.append(AmapPlanningDemand(
                pool_id=str(getattr(pool, "pool_id", "")), query_family=f"{city}:{intent}:{raw}",
                priority=self._ORDER[reason], reason=reason,
            ))
        return sorted(demand, key=lambda item: (item.priority, item.query_family, item.pool_id))

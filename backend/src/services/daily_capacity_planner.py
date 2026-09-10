"""Pure daily capacity evidence derived from structured occurrence contracts."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from src.services.goal_occurrence_compiler import GoalOccurrencePlan
from src.services.visit_duration_policy import VisitDurationPolicy


class DailyCapacityPlan(BaseModel):
    model_config = {"populate_by_name": True, "extra": "forbid"}

    day_number: int = Field(alias="dayNumber", ge=1)
    usable_minutes: int = Field(alias="usableMinutes", ge=0)
    planned_minutes: int = Field(alias="plannedMinutes", ge=0)
    route_reserve_minutes: int = Field(alias="routeReserveMinutes", ge=0)
    buffer_minutes: int = Field(alias="bufferMinutes", ge=0)
    intentional_free_minutes: int = Field(alias="intentionalFreeMinutes", ge=0)
    unexplained_gap_minutes: int = Field(alias="unexplainedGapMinutes", ge=0)
    target_route_anchors: int = Field(alias="targetRouteAnchors", ge=0)
    evidence: list[str] = Field(default_factory=list)


class DailyCapacityPlanner:
    """Keep capacity deterministic without assigning goals to days itself."""

    USABLE_MINUTES = {"relaxed": 420, "standard": 510, "intensive": 600}
    ROUTE_RESERVE_PER_TRANSIT_LEG = 30
    BUFFER_PER_ANCHOR = 10

    def __init__(self, duration_policy: Optional[VisitDurationPolicy] = None) -> None:
        self.duration_policy = duration_policy or VisitDurationPolicy()

    def plan(self, occurrence_plan: GoalOccurrencePlan, *, pace: str, day_count: int) -> dict[int, DailyCapacityPlan]:
        normalized_pace = pace if pace in self.USABLE_MINUTES else "standard"
        result: dict[int, DailyCapacityPlan] = {}
        for day in range(1, day_count + 1):
            occurrences = [item for item in occurrence_plan.occurrences if item.day_number == day]
            durations = [
                self.duration_policy.normalize_duration(
                    None,
                    kind="meal" if item.intent_type in {"meal", "local_food"} else "visit",
                    intent_type=item.intent_type,
                    context={"structuredPace": normalized_pace},
                ).preferred_minutes
                for item in occurrences
            ]
            anchors = len(occurrences)
            usable = self.USABLE_MINUTES[normalized_pace]
            route_reserve = max(0, anchors - 1) * self.ROUTE_RESERVE_PER_TRANSIT_LEG
            buffer = anchors * self.BUFFER_PER_ANCHOR
            planned = sum(durations)
            free = max(0, usable - planned - route_reserve - buffer)
            result[day] = DailyCapacityPlan(
                dayNumber=day,
                usableMinutes=usable,
                plannedMinutes=planned,
                routeReserveMinutes=route_reserve,
                bufferMinutes=buffer,
                intentionalFreeMinutes=free,
                unexplainedGapMinutes=0,
                targetRouteAnchors=anchors,
                evidence=[
                    "source=goal_occurrence_plan",
                    f"pace={normalized_pace}",
                    f"occurrenceCount={anchors}",
                    f"durationPolicy=VisitDurationPolicy",
                ],
            )
        return result

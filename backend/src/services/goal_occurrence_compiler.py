"""Compile accepted controller day strategies into bounded goal occurrences.

This is intentionally a pure, proposal-only service.  It consumes the
validated ``DraftItineraryDirective`` shape instead of re-interpreting user
wording, so the controller remains the owner of daily scope and cardinality.
"""

from __future__ import annotations

import copy
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from src.services.creative_planning_models import (
    ConstraintLedger,
    GoalPriorityTier,
    canonical_fingerprint,
)


_DISTINCT_IDENTITY_POLICIES = frozenset(
    {
        "distinct_physical_identity_per_occurrence",
        "distinct_physical_poi_per_day",
    }
)
_REUSE_IDENTITY_POLICIES = frozenset({"reuse_physical_identity_allowed"})


class _StrictModel(BaseModel):
    model_config = {"populate_by_name": True, "extra": "forbid"}


class GoalOccurrence(_StrictModel):
    occurrence_id: str = Field(alias="occurrenceId", min_length=1)
    source_goal_id: str = Field(alias="sourceGoalId", min_length=1)
    intent_type: str = Field(alias="intentType", min_length=1)
    day_number: int = Field(alias="dayNumber", ge=1)
    requirement_level: GoalPriorityTier = Field(alias="requirementLevel")
    user_explicit: bool = Field(default=False, alias="userExplicit")
    distinct_group_id: Optional[str] = Field(default=None, alias="distinctGroupId")
    access_policy: Optional[str] = Field(default=None, alias="accessPolicy")
    distinctness_policy: Optional[str] = Field(default=None, alias="distinctnessPolicy")
    time_window: Optional[Union[str, dict[str, Any]]] = Field(default=None, alias="timeWindow")
    schedule_preference: dict[str, Any] = Field(default_factory=dict, alias="schedulePreference")
    detour_tolerance: Optional[dict[str, Any]] = Field(default=None, alias="detourTolerance")
    evidence_freshness: Optional[Union[str, dict[str, Any]]] = Field(default=None, alias="evidenceFreshness")
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    allowed_day_numbers: list[int] = Field(default_factory=list, alias="allowedDayNumbers")
    experience_families: list[str] = Field(default_factory=list, alias="experienceFamilies")
    unresolved_dimensions: list[str] = Field(default_factory=list, alias="unresolvedDimensions")
    source: Literal["controller_day_strategy"] = "controller_day_strategy"


class GoalOccurrencePlan(_StrictModel):
    schema_version: Literal["goal-occurrence-plan-v1"] = Field(alias="schemaVersion")
    occurrences: list[GoalOccurrence] = Field(default_factory=list)
    avoid_recent_entities: bool = Field(alias="avoidRecentEntities")
    source_fingerprint: str = Field(alias="sourceFingerprint", min_length=16)

    @model_validator(mode="after")
    def occurrence_ids_and_days_are_unique(self) -> "GoalOccurrencePlan":
        ids = [item.occurrence_id for item in self.occurrences]
        if len(ids) != len(set(ids)):
            raise ValueError("goal_occurrence_duplicate_id")
        keys = [(item.source_goal_id, item.day_number) for item in self.occurrences]
        if len(keys) != len(set(keys)):
            raise ValueError("goal_occurrence_daily_cardinality_exceeded")
        return self


class GoalOccurrenceCompiler:
    """Validate and expand the existing controller directive without NLP rules."""

    def compile(self, ledger: ConstraintLedger, directive: dict[str, Any]) -> GoalOccurrencePlan:
        strategies = [item for item in directive.get("dayStrategies") or [] if isinstance(item, dict)]
        if not strategies:
            raise ValueError("goal_occurrence_day_strategies_missing")
        optional_budget = int(directive.get("optionalExperienceBudget") or 0)
        optional_occurrence_count = sum(len(strategy.get("optionalGoalIds") or []) for strategy in strategies)
        if optional_occurrence_count > optional_budget:
            raise ValueError("goal_occurrence_optional_experience_budget_exceeded")
        hard = {item.goal_id: item for item in ledger.hard_goals}
        soft = {item.goal_id: item for item in ledger.soft_goals}
        occurrences: list[GoalOccurrence] = []
        occurrence_counts: dict[str, int] = {}
        seen_days: set[int] = set()
        for strategy in strategies:
            day = int(strategy.get("dayNumber") or 0)
            if day < 1 or day > ledger.day_count or day in seen_days:
                raise ValueError("goal_occurrence_day_strategy_invalid")
            seen_days.add(day)
            required_ids = [str(item) for item in strategy.get("requiredGoalIds") or []]
            counts = strategy.get("requiredGoalCounts") if isinstance(strategy.get("requiredGoalCounts"), dict) else {}
            if any(str(goal_id) not in hard and str(goal_id) not in soft for goal_id in required_ids):
                raise ValueError("goal_occurrence_unknown_required_goal")
            if any(str(goal_id) not in required_ids for goal_id in counts):
                raise ValueError("goal_occurrence_count_without_goal")
            for goal_id in required_ids:
                count = int(counts.get(goal_id, 1))
                if count != 1:
                    raise ValueError("goal_occurrence_daily_cardinality_exceeded")
                goal = hard.get(goal_id) or soft[goal_id]
                if goal.allowed_day_numbers and day not in goal.allowed_day_numbers:
                    raise ValueError("goal_occurrence_day_not_allowed")
                occurrence_counts[goal_id] = occurrence_counts.get(goal_id, 0) + 1
                occurrences.append(
                    self._occurrence(
                        goal,
                        day,
                        self._priority_for_occurrence(
                            goal,
                            hard=goal_id in hard,
                            ordinal=occurrence_counts[goal_id],
                        ),
                        user_explicit=bool(goal.user_explicit),
                    )
                )
            optional_ids = [str(item) for item in strategy.get("optionalGoalIds") or []]
            if any(goal_id not in soft and goal_id not in hard for goal_id in optional_ids):
                raise ValueError("goal_occurrence_unknown_optional_goal")
            for goal_id in optional_ids:
                goal = soft.get(goal_id) or hard[goal_id]
                if goal.allowed_day_numbers and day not in goal.allowed_day_numbers:
                    raise ValueError("goal_occurrence_day_not_allowed")
                occurrence_counts[goal_id] = occurrence_counts.get(goal_id, 0) + 1
                occurrences.append(
                    self._occurrence(
                        goal,
                        day,
                        self._priority_for_occurrence(
                            goal,
                            hard=goal_id in hard,
                            ordinal=occurrence_counts[goal_id],
                            optional_placement=True,
                        ),
                        user_explicit=bool(goal.user_explicit),
                    )
                )

        # A hard goal must be explicitly placed by the accepted controller.  A
        # soft goal is intentionally optional unless the controller chose a day.
        covered_hard = {item.source_goal_id for item in occurrences if item.requirement_level == "hard"}
        if covered_hard != set(hard):
            raise ValueError("goal_occurrence_required_goal_unplaced")
        hard_counts: dict[str, int] = {}
        for item in occurrences:
            if item.requirement_level == "hard":
                hard_counts[item.source_goal_id] = hard_counts.get(item.source_goal_id, 0) + 1
        if any(hard_counts.get(goal_id, 0) < goal.required_min for goal_id, goal in hard.items()):
            raise ValueError("goal_occurrence_required_cardinality_mismatch")
        if any(
            goal.max_count is not None and hard_counts.get(goal_id, 0) > goal.max_count
            for goal_id, goal in hard.items()
        ):
            raise ValueError("goal_occurrence_maximum_cardinality_exceeded")
        if any(
            goal.distribution_policy == "every_allowed_day"
            and set(goal.allowed_day_numbers)
            != {
                item.day_number
                for item in occurrences
                if item.requirement_level == "hard" and item.source_goal_id == goal_id
            }
            for goal_id, goal in hard.items()
        ):
            raise ValueError("goal_occurrence_distribution_invalid")
        all_counts = {
            goal_id: sum(item.source_goal_id == goal_id for item in occurrences) for goal_id in {*hard, *soft}
        }
        if any(
            goal.max_count is not None and all_counts.get(goal_id, 0) > goal.max_count for goal_id, goal in hard.items()
        ):
            raise ValueError("goal_occurrence_maximum_cardinality_exceeded")
        soft_occurrences = [item for item in occurrences if item.requirement_level == "explicit_soft"]
        soft_counts = {goal_id: sum(item.source_goal_id == goal_id for item in soft_occurrences) for goal_id in soft}
        if any(
            goal.distribution_policy == "every_allowed_day"
            and set(goal.allowed_day_numbers)
            != {item.day_number for item in soft_occurrences if item.source_goal_id == goal_id}
            for goal_id, goal in soft.items()
        ):
            raise ValueError("goal_occurrence_soft_distribution_invalid")
        if any(
            goal.distribution_policy == "every_allowed_day" and soft_counts.get(goal_id, 0) < goal.required_min
            for goal_id, goal in soft.items()
        ):
            raise ValueError("goal_occurrence_soft_cardinality_mismatch")
        if any(
            goal.max_count is not None and soft_counts.get(goal_id, 0) > goal.max_count
            for goal_id, goal in soft.items()
        ):
            raise ValueError("goal_occurrence_soft_maximum_exceeded")
        avoid_recent = bool((directive.get("candidateSelectionPolicy") or {}).get("avoidRecentEntities", True))
        counts_by_goal: dict[str, int] = {}
        for item in occurrences:
            counts_by_goal[item.source_goal_id] = counts_by_goal.get(item.source_goal_id, 0) + 1
        occurrences = [
            item.model_copy(
                update={
                    "distinct_group_id": self._distinct_group_id(
                        item,
                        occurrence_count=counts_by_goal[item.source_goal_id],
                        avoid_recent_entities=avoid_recent,
                    )
                }
            )
            for item in occurrences
        ]
        raw = {
            "ledger": ledger.model_dump(by_alias=True),
            "dayStrategies": strategies,
            "avoidRecentEntities": avoid_recent,
        }
        return GoalOccurrencePlan(
            schemaVersion="goal-occurrence-plan-v1",
            occurrences=occurrences,
            avoidRecentEntities=avoid_recent,
            sourceFingerprint=canonical_fingerprint(raw),
        )

    @staticmethod
    def _priority_for_occurrence(
        goal: Any,
        *,
        hard: bool,
        ordinal: int,
        optional_placement: bool = False,
    ) -> GoalPriorityTier:
        if hard:
            if not optional_placement and ordinal <= int(goal.required_min):
                return "hard"
            return "inferred_preferred"
        if bool(goal.user_explicit) or str(goal.priority_tier) == "explicit_soft":
            return "explicit_soft"
        if str(goal.priority_tier) == "defining_theme":
            return "defining_theme"
        if str(goal.priority_tier) == "inferred_preferred":
            return "inferred_preferred"
        return "optional"

    @staticmethod
    def _distinct_group_id(
        occurrence: GoalOccurrence,
        *,
        occurrence_count: int,
        avoid_recent_entities: bool,
    ) -> Optional[str]:
        policy = str(occurrence.distinctness_policy or "").strip()
        repeated_hard_night_view = (
            occurrence_count > 1
            and str(occurrence.requirement_level) == "hard"
            and str(occurrence.intent_type).strip().lower() == "night_view"
        )
        if repeated_hard_night_view:
            # A generic reuse policy may be valid for repeat visits to the same
            # campus or hotel, but it must never turn one physical viewpoint
            # into multiple required night-view experiences.
            requires_distinct_identity = True
        elif policy in _DISTINCT_IDENTITY_POLICIES:
            requires_distinct_identity = True
        elif policy in _REUSE_IDENTITY_POLICIES:
            requires_distinct_identity = False
        elif policy:
            raise ValueError("goal_occurrence_distinctness_policy_unknown")
        else:
            requires_distinct_identity = avoid_recent_entities
        if occurrence_count <= 1 or not requires_distinct_identity:
            return None
        return f"distinct:{occurrence.source_goal_id}"

    @staticmethod
    def _occurrence(
        goal: Any,
        day: int,
        level: GoalPriorityTier,
        *,
        user_explicit: bool = False,
    ) -> GoalOccurrence:
        return GoalOccurrence(
            occurrenceId=f"occ:{goal.goal_id}:day:{day}",
            sourceGoalId=goal.goal_id,
            intentType=goal.intent_type,
            dayNumber=day,
            requirementLevel=level,
            userExplicit=user_explicit,
            accessPolicy=goal.access_policy,
            distinctnessPolicy=goal.distinctness_policy,
            timeWindow=copy.deepcopy(goal.time_window),
            schedulePreference=copy.deepcopy(goal.schedule_preference),
            detourTolerance=copy.deepcopy(goal.detour_tolerance),
            evidenceFreshness=copy.deepcopy(goal.evidence_freshness),
            confidence=goal.confidence,
            allowedDayNumbers=list(goal.allowed_day_numbers),
            experienceFamilies=list(goal.experience_families),
            unresolvedDimensions=list(goal.unresolved_dimensions),
        )

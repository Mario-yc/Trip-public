"""Strict one-call parser for creative portfolio skeletons.

The provider may describe themes and ungrounded intent pools only.  It cannot
provide AMap identifiers, coordinates, schedule facts, or write instructions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from src.services.creative_direction_generator import CreativeDirectionGenerator
from src.services.creative_planning_models import (
    ConstraintLedger,
    CreativeBrief,
    CreativePortfolioDaySlot,
    CreativePortfolioIntentPool,
    TripExperienceIntent,
)
from src.services.daily_capacity_planner import DailyCapacityPlan, DailyCapacityPlanner
from src.services.goal_occurrence_compiler import GoalOccurrencePlan
from src.services.itinerary_schedule_service import ItineraryScheduleService
from src.services.visit_duration_policy import VisitDurationPolicy


class _Strict(BaseModel):
    model_config = {"populate_by_name": True, "extra": "forbid"}


class PortfolioSkeleton(_Strict):
    brief: CreativeBrief
    day_slots: list[CreativePortfolioDaySlot] = Field(default_factory=list, alias="daySlots", max_length=24)
    intent_pools: list[CreativePortfolioIntentPool] = Field(default_factory=list, alias="intentPools", max_length=16)


class InitialCreativePortfolio(_Strict):
    schema_version: str = Field(alias="schemaVersion")
    proposals: list[PortfolioSkeleton] = Field(min_length=1, max_length=4)
    warnings: list[str] = Field(default_factory=list, max_length=8)
    parser_metadata: dict[str, int] = Field(default_factory=dict, alias="parserMetadata")


class CreativePortfolioProviderService:
    SCHEMA_VERSION = "initial-creative-portfolio-v1"
    _OPTIONAL_FAMILY_SPECS: dict[str, tuple[str, str, str, int]] = {
        "heritage_walk": ("历史街区漫步", "area_walk", "afternoon", 1),
        "art_walk": ("艺术文化街区", "area_walk", "afternoon", 2),
        "local_life": ("社区生活体验", "area_walk", "afternoon", 2),
        "market_walk": ("市井市场漫步", "area_walk", "afternoon", 1),
        "park_relax": ("城市公园休憩", "park", "afternoon", 2),
        "night_view": ("城市夜景观察", "night_view", "evening", 2),
        "local_food": ("地方风味体验", "meal", "lunch", 1),
    }

    @staticmethod
    def _optional_supply_state(direction: dict[str, Any]) -> str:
        """Return an explicit provenance state for optional-slot allocation.

        A seed direction is an instruction to search, not evidence that a
        candidate is grounded.  Conversely, zero only means "no optional" when
        it came from a completed admitted inventory.
        """
        candidate_supply = direction.get("candidateSupply")
        candidate_supply = candidate_supply if isinstance(candidate_supply, dict) else {}
        declared = str(candidate_supply.get("supplyState") or "").strip().lower()
        if declared in {"pending_search", "observed_complete"}:
            return declared
        feasibility = direction.get("feasibilityEvidence")
        feasibility = feasibility if isinstance(feasibility, dict) else {}
        generation_source = str(direction.get("generationSource") or "").strip().lower()
        area_key = str(direction.get("areaClusterKey") or "").strip().lower()
        if (
            generation_source == "seed_composition"
            or area_key == "inventory_pending"
            or bool(feasibility.get("requiresBoundedSearch"))
        ):
            return "pending_search"
        if generation_source == "admitted_candidate_inventory":
            return "observed_complete"
        # Legacy directions lack provenance.  They may open a bounded search
        # obligation, but cannot make a zero count authoritative.
        return "pending_search"

    @staticmethod
    def _slots_fit_schedule(slots: list[dict[str, Any]]) -> bool:
        """Check the same server-owned windows used by materialization.

        The fallback may promise an optional search slot only when the slot can
        coexist with the day's hard/explicit slots and transfer buffers.
        """
        occupied: list[tuple[int, int]] = []
        timings: list[tuple[int, str, dict[str, Any]]] = []
        for slot in slots:
            timing = ItineraryScheduleService.compiled_materialization_timing(
                time_window=slot.get("timeWindow"),
                start_time=slot.get("startTime"),
                duration=slot.get("durationMinutes"),
                intent_type=str(slot.get("kind") or ""),
                source="creative_day_slot",
            )
            if timing is None:
                return False
            timings.append((int(timing["startMinutes"]), str(slot.get("slotId") or ""), timing))
        for preferred_start, _slot_id, timing in sorted(timings):
            start = preferred_start
            duration = int(timing["durationMinutes"])
            while True:
                conflict_end = max(
                    (
                        occupied_end
                        for occupied_start, occupied_end in occupied
                        if start < occupied_end and start + duration > occupied_start
                    ),
                    default=None,
                )
                if conflict_end is None:
                    break
                start = conflict_end + 15
            constraints = timing["scheduleConstraints"]
            latest_start = ItineraryScheduleService._parse_time(constraints.get("latestStart"))
            window_end = ItineraryScheduleService._parse_time(constraints.get("windowEnd"))
            if (latest_start is not None and start > latest_start) or (
                window_end is not None and start + duration > window_end
            ):
                return False
            occupied.append((start, start + duration))
        return True

    def generate(
        self,
        *,
        ledger: ConstraintLedger,
        invoke: Callable[[], str],
        repair: Callable[[str], str] | None = None,
        day_anchor_limits: dict[int, int] | None = None,
        occurrence_plan: Optional[GoalOccurrencePlan] = None,
    ) -> tuple[InitialCreativePortfolio, int]:
        """Invoke once, with at most one schema-only repair and no hidden fallback."""
        raw = invoke()
        try:
            output = self._validate(raw, ledger, day_anchor_limits=day_anchor_limits, occurrence_plan=occurrence_plan)
            return output.model_copy(update={"parser_metadata": {"providerCallCount": 1, "schemaRepairAttempts": 0}}), 0
        except ValueError as error:
            if repair is None:
                raise
            repaired = repair(str(error))
            output = self._validate(
                repaired, ledger, day_anchor_limits=day_anchor_limits, occurrence_plan=occurrence_plan
            )
            return output.model_copy(update={"parser_metadata": {"providerCallCount": 1, "schemaRepairAttempts": 1}}), 1

    def deterministic_fallback(
        self,
        *,
        ledger: ConstraintLedger,
        directive: dict[str, Any],
        schema_repair_attempts: int,
        day_anchor_limits: dict[int, int] | None = None,
        occurrence_plan: Optional[GoalOccurrencePlan] = None,
        daily_capacity: Optional[dict[int, DailyCapacityPlan]] = None,
        direction_axes: Optional[list[str]] = None,
        direction_candidates: Optional[list[dict[str, Any]]] = None,
    ) -> InitialCreativePortfolio:
        """Build an identity-free portfolio from a validated controller draft."""

        hard_goal_ids = [goal.goal_id for goal in ledger.hard_goals]
        if occurrence_plan is None:
            from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler

            occurrence_plan = GoalOccurrenceCompiler().compile(ledger, directive)
        if daily_capacity is None:
            daily_capacity = DailyCapacityPlanner().plan(occurrence_plan, pace=ledger.pace, day_count=ledger.day_count)
        occurrences_by_day: dict[int, list[Any]] = {
            day: [item for item in occurrence_plan.occurrences if item.day_number == day]
            for day in range(1, ledger.day_count + 1)
        }
        day_themes: dict[int, str] = {}
        for strategy in directive.get("dayStrategies") or []:
            if isinstance(strategy, dict):
                day_number = int(strategy.get("dayNumber") or 0)
                if 1 <= day_number <= ledger.day_count:
                    day_themes[day_number] = str(strategy.get("theme") or f"Day {day_number}")

        limits = {
            day: max(1, min(6, int((day_anchor_limits or {}).get(day, 4)))) for day in range(1, ledger.day_count + 1)
        }
        for strategy in directive.get("dayStrategies") or []:
            if not isinstance(strategy, dict):
                continue
            day = max(1, min(ledger.day_count, int(strategy.get("dayNumber") or 1)))
            if day_anchor_limits is None:
                limits[day] = max(1, min(6, int(strategy.get("maxRouteAnchors") or 4)))
        # maxRouteAnchors is a capacity, never an instruction to fill (or a
        # reason to discard) a directional experience.  Start with only the
        # hard and explicit-soft route anchors; executable optional families
        # are assigned below against the remaining capacity.
        day_targets = {
            day: max(1, min(limits[day], explicit_count))
            for day in range(1, ledger.day_count + 1)
            for explicit_count in [
                sum(1 for item in occurrences_by_day[day] if item.requirement_level in {"hard", "soft"})
            ]
        }
        hard_goal_strategy = [
            f"{goal.intent_type}:{goal.required_min}" for goal in [*ledger.hard_goals, *ledger.soft_goals]
        ]
        directions = [dict(item) for item in direction_candidates or [] if isinstance(item, dict)]
        if not directions and direction_axes:
            directions = [
                candidate
                for axis in dict.fromkeys(str(item) for item in direction_axes if str(item))
                for candidate in [
                    CreativeDirectionGenerator.seed_for_axis(
                        axis,
                        hard_goal_strategy=hard_goal_strategy,
                        day_anchor_targets=day_targets,
                        city=ledger.city,
                    )
                ]
                if candidate is not None
            ]
        if not directions:
            directions = CreativeDirectionGenerator.generate_next(
                hard_goal_strategy=hard_goal_strategy,
                day_anchor_targets=day_targets,
                used_signatures=[],
                limit=4,
                experience_intent=(ledger.experience_intent if isinstance(ledger.experience_intent, dict) else {}),
                city=ledger.city,
            )
        specs: list[tuple[str, str, str, list[tuple[str, str, str, str, int]], dict[str, Any]]] = []
        for direction in directions[:4]:
            direction = dict(direction)
            candidate_supply = (
                dict(direction.get("candidateSupply") or {})
                if isinstance(direction.get("candidateSupply"), dict)
                else {}
            )
            supply_state = self._optional_supply_state(direction)
            candidate_supply["supplyState"] = supply_state
            direction["candidateSupply"] = candidate_supply
            axis = str(direction.get("primaryAxis") or "classic")
            title = str(direction.get("title") or f"{ledger.city}主题探索")
            families = [
                str(item)
                for item in (direction.get("themeFamilies") or direction.get("experienceFamilies") or [])
                if str(item) in self._OPTIONAL_FAMILY_SPECS
            ]
            family_counts = (
                (direction.get("candidateSupply") or {}).get("familyCounts")
                if isinstance(direction.get("candidateSupply"), dict)
                else {}
            )
            # Older controller drafts did not persist an explicit budget.  They
            # retain the established bounded single optional seed; an explicit
            # zero remains an authoritative instruction to allocate none.
            optional_budget = (
                max(0, int(directive["optionalExperienceBudget"]))
                if "optionalExperienceBudget" in directive
                else min(
                    3,
                    sum(max(0, limits[day] - day_targets[day]) for day in range(1, ledger.day_count + 1)),
                )
            )
            remaining_capacity = sum(
                max(0, limits[day] - day_targets[day])
                for day in range(1, ledger.day_count + 1)
            )
            optional_slot_budget = min(3, optional_budget, remaining_capacity)
            # A pending-search family is not observed candidate supply, but the
            # fallback may still declare bounded search work.  Legacy fallback
            # must retain one directional obligation even on a fully occupied
            # day; on longer plans it also covers the already-declared minimum
            # daily target deficit.  Placement below remains capacity-first,
            # rather than treating an empty occurrence list as a special case.
            minimum_pending_obligations = max(
                1,
                sum(
                    max(
                        0,
                        day_targets[day]
                        - sum(
                            1
                            for occurrence in occurrences_by_day[day]
                            if occurrence.requirement_level in {"hard", "soft"}
                        ),
                    )
                    for day in range(1, ledger.day_count + 1)
                ),
            )
            expanded_families: list[str] = []
            for family in list(dict.fromkeys(families))[:3]:
                raw_available_count = (family_counts or {}).get(family)
                available_count = int(raw_available_count or 0)
                if supply_state == "pending_search":
                    # This creates only a bounded search obligation.  It does
                    # not change candidateSupply.familyCounts or assert that a
                    # physical POI has already been admitted.  A later
                    # grounding pass must still prove distinct physical POIs
                    # before multiple obligations can be adopted.
                    available_count = (
                        optional_slot_budget
                        if "optionalExperienceBudget" in directive
                        else min(minimum_pending_obligations, optional_slot_budget)
                    )
                if available_count > 0:
                    expanded_families.extend([family] * min(available_count, optional_slot_budget))
            optional_specs = [
                (family, *self._OPTIONAL_FAMILY_SPECS[family])
                for family in expanded_families[:optional_slot_budget]
            ]
            specs.append((axis, title, title, optional_specs, direction))
        if not specs:
            raise ValueError("creative_portfolio_dynamic_fallback_exhausted")
        family_search_needs = {
            "heritage_walk": "历史街区与胡同漫步",
            "local_life": "社区生活街区与社区市场",
            "market_walk": "市井市场与传统市集",
            "art_walk": "艺术街区与创意园区",
            "park_relax": "城市公园休憩",
        }
        goal_by_id = {goal.goal_id: goal for goal in ledger.hard_goals}
        proposals: list[dict[str, Any]] = []
        for proposal_index, (
            axis,
            title,
            role_suffix,
            optional_specs,
            direction,
        ) in enumerate(specs, start=1):
            signature = str(direction.get("directionSignature") or CreativeDirectionGenerator.signature(direction))
            brief_id = f"dynamic_{signature[:12]}"
            day_slots: list[dict[str, Any]] = []
            intent_pools: list[dict[str, Any]] = []
            per_day_goal_count: dict[int, int] = {}
            for day_number in range(1, ledger.day_count + 1):
                for occurrence in occurrences_by_day[day_number]:
                    if occurrence.requirement_level != "hard":
                        continue
                    goal_id = occurrence.source_goal_id
                    goal = goal_by_id[goal_id]
                    goal_index = per_day_goal_count.get(day_number, 0)
                    per_day_goal_count[day_number] = goal_index + 1
                    time_window = self._required_goal_time_window(
                        goal.intent_type,
                        goal_index,
                    )
                    slot_id = f"{brief_id}_{goal_id}_day_{day_number}_slot"
                    raw_need, kind, preferred_types = self._required_goal_query(goal.intent_type)
                    if goal.exact_entity:
                        raw_need = goal.exact_entity
                    duration = (
                        VisitDurationPolicy()
                        .normalize_duration(
                            None,
                            kind=kind,
                            intent_type=goal.intent_type,
                            context={"structuredPace": ledger.pace},
                        )
                        .preferred_minutes
                    )
                    day_slots.append(
                        {
                            "slotId": slot_id,
                            "dayNumber": day_number,
                            "timeWindow": time_window,
                            "durationMinutes": duration,
                            "kind": kind,
                            "rawNeed": raw_need,
                            "routeAnchor": True,
                            "priority": 90,
                            "requiredGoalId": goal_id,
                        }
                    )
                    intent_pools.append(
                        {
                            "poolId": f"{brief_id}_{goal_id}_day_{day_number}_pool",
                            "briefId": brief_id,
                            "rawNeed": raw_need,
                            "city": ledger.city,
                            "intentType": goal.intent_type,
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": goal_id,
                            "assignToSlots": [slot_id],
                            "preferredTypes": preferred_types,
                            "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                            **(
                                {
                                    "entityBindingMode": "exact_entity",
                                    "exactEntity": goal.exact_entity,
                                }
                                if goal.exact_entity
                                else {}
                            ),
                        }
                    )
            soft_by_id = {goal.goal_id: goal for goal in ledger.soft_goals}
            for day_number in range(1, ledger.day_count + 1):
                for occurrence in occurrences_by_day[day_number]:
                    if occurrence.requirement_level == "hard":
                        continue
                    soft_goal = soft_by_id.get(occurrence.source_goal_id) or goal_by_id.get(occurrence.source_goal_id)
                    if soft_goal is None:
                        raise ValueError("creative_portfolio_explicit_soft_goal_missing")
                    if per_day_goal_count.get(day_number, 0) >= limits[day_number]:
                        raise ValueError("creative_portfolio_explicit_soft_exceeds_day_capacity")
                    goal_index = per_day_goal_count.get(day_number, 0)
                    per_day_goal_count[day_number] = goal_index + 1
                    raw_need, kind, preferred_types, time_window = self._soft_goal_query(soft_goal.intent_type)
                    slot_id = f"{brief_id}_{soft_goal.goal_id}_day_{day_number}_soft_slot"
                    duration = (
                        VisitDurationPolicy()
                        .normalize_duration(
                            None,
                            kind=kind,
                            intent_type=soft_goal.intent_type,
                            context={"structuredPace": ledger.pace},
                        )
                        .preferred_minutes
                    )
                    day_slots.append(
                        {
                            "slotId": slot_id,
                            "dayNumber": day_number,
                            "timeWindow": time_window,
                            "durationMinutes": duration,
                            "kind": kind,
                            "rawNeed": raw_need,
                            "routeAnchor": True,
                            "priority": {
                                "explicit_soft": 80,
                                "defining_theme": 70,
                                "inferred_preferred": 60,
                                "optional": 50,
                            }.get(occurrence.requirement_level, 50),
                            "softGoalId": soft_goal.goal_id,
                            "requirementLevel": "soft",
                            "experienceShape": "single_poi",
                            "experienceGoal": raw_need,
                            "desiredSignals": list((ledger.experience_intent or {}).get("desiredSignals") or []),
                            "avoidSignals": list((ledger.experience_intent or {}).get("avoidSignals") or []),
                            "evidenceRequirements": (
                                {"minimumIndependentClaims": 1}
                                if soft_goal.intent_type
                                in {
                                    "meal",
                                    "local_food",
                                    "food",
                                    "local_life",
                                    "heritage_walk",
                                    "market_walk",
                                    "art_walk",
                                }
                                else {}
                            ),
                            "groundingContract": {"consumerRecheckRequired": True},
                            "routeContract": {"preserveExistingBudget": True},
                        }
                    )
                    intent_pools.append(
                        {
                            "poolId": f"{brief_id}_{soft_goal.goal_id}_day_{day_number}_soft_pool",
                            "briefId": brief_id,
                            "rawNeed": raw_need,
                            "city": ledger.city,
                            "intentType": soft_goal.intent_type,
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "goalId": None,
                            "softGoalId": soft_goal.goal_id,
                            "assignToSlots": [slot_id],
                            "preferredTypes": preferred_types,
                            "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                        }
                    )
            optional_experiences = []
            for optional_index, (family, description, intent_type, time_window, _preferred_day) in enumerate(
                optional_specs, start=1
            ):
                optional_duration = (
                    VisitDurationPolicy()
                    .normalize_duration(
                        None,
                        kind=intent_type,
                        intent_type=intent_type,
                        context={"structuredPace": ledger.pace},
                    )
                    .preferred_minutes
                )
                available_days = [
                    day
                    for day in range(1, ledger.day_count + 1)
                    if per_day_goal_count.get(day, 0) < limits[day]
                ]
                semantic_day = 1 + (int(hashlib.sha256(family.encode("utf-8")).hexdigest()[:8], 16) % ledger.day_count)
                allowed_windows = [time_window]
                if intent_type in {"area_walk", "park"} and time_window == "afternoon":
                    allowed_windows.append("evening")
                feasible_placements = [
                    (day, candidate_window)
                    for day in available_days
                    for candidate_window in allowed_windows
                    if self._slots_fit_schedule(
                        [
                            slot
                            for slot in day_slots
                            if int(slot.get("dayNumber") or 0) == day
                        ]
                        + [
                            {
                                "slotId": f"{brief_id}_{family}_{optional_index}_slot",
                                "timeWindow": candidate_window,
                                "durationMinutes": optional_duration,
                                "kind": intent_type,
                            }
                        ]
                    )
                ]
                if not feasible_placements:
                    continue
                day_number, selected_window = min(
                    feasible_placements,
                    key=lambda placement: (
                        per_day_goal_count.get(placement[0], 0) / limits[placement[0]],
                        0 if placement[0] == semantic_day else 1,
                        allowed_windows.index(placement[1]),
                        -daily_capacity[placement[0]].intentional_free_minutes,
                        placement[0],
                    ),
                )
                per_day_goal_count[day_number] = per_day_goal_count.get(day_number, 0) + 1
                slot_id = f"{brief_id}_{family}_{optional_index}_slot"
                # City is already an explicit pool field.  Prefixing it into
                # the semantic need makes a category request look like an exact
                # entity (for example, 北京 + 博物馆), which rejects valid POIs
                # whose canonical AMap name does not repeat the city.
                optional_raw_need = family_search_needs.get(family, description)
                optional_experiences.append({"family": family, "description": description})
                day_slots.append(
                    {
                        "slotId": slot_id,
                        "dayNumber": day_number,
                        "timeWindow": selected_window,
                        "durationMinutes": optional_duration,
                        "kind": intent_type,
                        "rawNeed": optional_raw_need,
                        "routeAnchor": True,
                        "priority": 55,
                        "optionalExperienceFamily": family,
                        "requirementLevel": "soft",
                        "experienceShape": "area" if intent_type == "area_walk" else "single_poi",
                        "experienceGoal": description,
                        "desiredSignals": list((ledger.experience_intent or {}).get("desiredSignals") or []),
                        "avoidSignals": list((ledger.experience_intent or {}).get("avoidSignals") or []),
                        "evidenceRequirements": (
                            {"minimumIndependentClaims": 1}
                            if family
                            in {
                                "local_food",
                                "local_life",
                                "heritage_walk",
                                "market_walk",
                                "art_walk",
                            }
                            else {}
                        ),
                        "groundingContract": {"consumerRecheckRequired": True},
                        "routeContract": {"preserveExistingBudget": True},
                    }
                )
                intent_pools.append(
                    {
                        "poolId": f"{brief_id}_{family}_{optional_index}_pool",
                        "briefId": brief_id,
                        "rawNeed": optional_raw_need,
                        "city": ledger.city,
                        "intentType": intent_type,
                        "targetCount": 1,
                        "requirementLevel": "optional",
                        "goalId": None,
                        "assignToSlots": [slot_id],
                        "optionalExperienceFamily": family,
                        "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                    }
                )
            day_roles = [
                {
                    "dayNumber": day_number,
                    "role": f"{day_themes.get(day_number, f'Day {day_number}')} · {role_suffix}",
                    "targetRouteAnchors": per_day_goal_count.get(day_number, 0),
                    "densityEvidence": [
                        f"pace={ledger.pace}",
                        "availableWindow=full_day",
                        f"requiredGoalCount={sum(1 for slot in day_slots if slot.get('dayNumber') == day_number and slot.get('requiredGoalId'))}",
                        f"explicitSoftGoalCount={sum(1 for slot in day_slots if slot.get('dayNumber') == day_number and slot.get('softGoalId'))}",
                        f"briefOptionalCount={sum(1 for slot in day_slots if slot.get('dayNumber') == day_number and slot.get('optionalExperienceFamily'))}",
                        f"transport={','.join(ledger.transport_preferences) or 'unspecified'}",
                        f"briefAxis={axis}",
                        f"capacity={daily_capacity[day_number].usable_minutes}/{daily_capacity[day_number].planned_minutes}/{daily_capacity[day_number].route_reserve_minutes}/{daily_capacity[day_number].buffer_minutes}",
                    ],
                }
                for day_number in range(1, ledger.day_count + 1)
            ]
            proposals.append(
                {
                    "brief": {
                        "briefId": brief_id,
                        "title": title,
                        "primaryAxis": axis,
                        "dayRoles": day_roles,
                        "optionalExperiences": optional_experiences,
                        "requiredGoalIds": hard_goal_ids,
                        "themeFamilies": list(
                            direction.get("themeFamilies") or direction.get("experienceFamilies") or []
                        ),
                        "candidateSupply": dict(direction.get("candidateSupply") or {}),
                        "directionSignature": signature,
                        "noveltyEvidence": dict(direction.get("noveltyEvidence") or {}),
                        "feasibilityEvidence": dict(direction.get("feasibilityEvidence") or {}),
                        "generationSource": str(direction.get("generationSource") or "seed_composition"),
                        "experienceIntent": (
                            dict(ledger.experience_intent)
                            if isinstance(ledger.experience_intent, dict) and ledger.experience_intent
                            else None
                        ),
                    },
                    "daySlots": day_slots,
                    "intentPools": intent_pools,
                }
            )
        output = self._validate(
            json.dumps(
                {
                    "schemaVersion": self.SCHEMA_VERSION,
                    "proposals": proposals,
                    "warnings": ["创意模型不可用时已生成仅承诺已落地硬目标的基础方案；未落地软体验不会被伪造。"],
                },
                ensure_ascii=False,
            ),
            ledger,
            day_anchor_limits=limits,
            occurrence_plan=occurrence_plan,
        )
        return output.model_copy(
            update={
                "parser_metadata": {
                    "providerCallCount": 1,
                    "schemaRepairAttempts": schema_repair_attempts,
                    "deterministicFallbackUsed": 1,
                }
            }
        )

    @staticmethod
    def _required_goal_query(intent_type: str) -> tuple[str, str, list[str]]:
        return {
            "campus_visit": ("高校参观", "campus", ["大学", "学院", "高等院校", "校区"]),
            "museum": ("博物馆或美术馆", "museum", ["博物馆", "美术馆", "展览馆", "文化馆"]),
            "night_view": (
                "晚上看城市夜景",
                "night_view",
                ["地标", "观景台", "观景平台", "滨水空间", "夜游步道"],
            ),
        }.get(intent_type, (intent_type.replace("_", " ") or "活动", "visit", ["文化场馆"]))

    @staticmethod
    def _required_goal_time_window(intent_type: str, goal_index: int) -> str:
        """Keep time-sensitive hard goals in their executable daypart."""

        if intent_type == "night_view":
            return "night"
        if intent_type == "meal":
            return "noon"
        return ["morning", "afternoon", "evening"][min(goal_index, 2)]

    @staticmethod
    def _soft_goal_query(intent_type: str) -> tuple[str, str, list[str], str]:
        return {
            "local_food": ("当地特色美食", "meal", ["餐厅", "小吃", "老字号"], "noon"),
            "meal": ("当地特色美食", "meal", ["餐厅", "小吃", "老字号"], "noon"),
            "local_culture": ("本地文化体验", "culture", ["文化馆", "街区", "体验馆"], "afternoon"),
            "campus_visit": ("高校参观", "campus", ["大学", "学院", "高等院校", "校区"], "morning"),
            "museum": (
                "博物馆或美术馆",
                "museum",
                ["博物馆", "美术馆", "展览馆", "文化馆"],
                "afternoon",
            ),
            "night_view": (
                "城市夜景",
                "night_view",
                ["地标", "观景台", "观景平台", "滨水空间", "夜游步道"],
                "night",
            ),
        }.get(
            intent_type,
            (intent_type.replace("_", " ") or "体验", "experience", ["文化场馆"], "afternoon"),
        )

    def _validate(
        self,
        raw: str,
        ledger: ConstraintLedger,
        *,
        day_anchor_limits: dict[int, int] | None = None,
        occurrence_plan: Optional[GoalOccurrencePlan] = None,
    ) -> InitialCreativePortfolio:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("creative_portfolio_schema_invalid") from error
        self._reject_grounded_facts(value)
        try:
            output = InitialCreativePortfolio.model_validate(value)
        except ValidationError as error:
            repair_errors = [
                {"path": ".".join(str(part) for part in item["loc"]), "type": item["type"]}
                for item in error.errors(include_url=False, include_context=False, include_input=False)[:12]
            ]
            raise ValueError(
                "creative_portfolio_schema_invalid:"
                + json.dumps(repair_errors, ensure_ascii=False, separators=(",", ":"))
            ) from error
        if output.schema_version != self.SCHEMA_VERSION:
            raise ValueError("creative_portfolio_schema_version_invalid")
        if isinstance(ledger.experience_intent, dict) and ledger.experience_intent:
            authoritative_intent = TripExperienceIntent.model_validate(ledger.experience_intent)
            output = output.model_copy(
                update={
                    "proposals": [
                        skeleton.model_copy(
                            update={
                                "brief": skeleton.brief.model_copy(update={"experience_intent": authoritative_intent})
                            }
                        )
                        for skeleton in output.proposals
                    ]
                }
            )
        required = {goal.goal_id for goal in ledger.hard_goals}
        identities: set[str] = set()
        direction_signatures: set[str] = set()
        shared_soft_signature: tuple[tuple[str, int, str], ...] | None = None
        for skeleton in output.proposals:
            brief = skeleton.brief
            if set(brief.required_goal_ids) != required:
                raise ValueError("creative_portfolio_required_goal_ids_invalid")
            if brief.brief_id in identities:
                raise ValueError("creative_portfolio_duplicate_brief_id")
            identities.add(brief.brief_id)
            direction_signature = CreativeDirectionGenerator.signature(
                {
                    "primaryAxis": brief.primary_axis,
                    "secondaryAxes": list(brief.secondary_axes),
                    "experienceFamilies": sorted(item.family for item in brief.optional_experiences),
                    "activityModes": [],
                    "dayRoleSignature": [f"day_{item.day_number}:{item.role}" for item in brief.day_roles],
                    "dayAnchorTargets": {str(item.day_number): item.target_route_anchors for item in brief.day_roles},
                    "hardSoftGoalPlacementStrategy": list(brief.required_goal_ids),
                }
            )
            if direction_signature in direction_signatures:
                raise ValueError("creative_portfolio_duplicate_direction_signature")
            direction_signatures.add(direction_signature)
            self._validate_nested_contract(
                skeleton,
                ledger,
                day_anchor_limits=day_anchor_limits,
                occurrence_plan=occurrence_plan,
            )
            if occurrence_plan is not None:
                self._validate_occurrence_contract(skeleton, occurrence_plan)
            slots_by_id = {slot.slot_id: slot for slot in skeleton.day_slots}
            soft_signature = tuple(
                sorted(
                    (
                        str(pool.soft_goal_id),
                        slots_by_id[pool.assign_to_slots[0]].day_number,
                        pool.intent_type,
                    )
                    for pool in skeleton.intent_pools
                    if pool.soft_goal_id is not None
                )
            )
            if shared_soft_signature is None:
                shared_soft_signature = soft_signature
            elif soft_signature != shared_soft_signature:
                raise ValueError("creative_portfolio_cross_brief_soft_goal_placement")
        return output

    @staticmethod
    def _validate_occurrence_contract(skeleton: PortfolioSkeleton, occurrence_plan: GoalOccurrencePlan) -> None:
        expected = {
            (item.source_goal_id, item.day_number, item.requirement_level) for item in occurrence_plan.occurrences
        }
        expected_level_by_key = {
            (item.source_goal_id, item.day_number): item.requirement_level for item in occurrence_plan.occurrences
        }
        actual: set[tuple[str, int, str]] = set()
        for slot in skeleton.day_slots:
            goal_id = str(slot.required_goal_id or slot.soft_goal_id or "")
            if not goal_id:
                continue
            level = expected_level_by_key.get(
                (goal_id, int(slot.day_number)),
                "hard" if slot.required_goal_id else "explicit_soft",
            )
            actual.add((goal_id, int(slot.day_number), level))
        if actual != expected:
            raise ValueError("creative_portfolio_goal_occurrence_contract_invalid")

    @staticmethod
    def _validate_nested_contract(
        skeleton: PortfolioSkeleton,
        ledger: ConstraintLedger,
        *,
        day_anchor_limits: dict[int, int] | None = None,
        occurrence_plan: GoalOccurrencePlan | None = None,
    ) -> None:
        required = {goal.goal_id for goal in ledger.hard_goals}
        soft = (
            {
                item.source_goal_id
                for item in occurrence_plan.occurrences
                if item.requirement_level != "hard"
            }
            if occurrence_plan is not None
            else {goal.goal_id for goal in ledger.soft_goals}
        )
        optional_families = {item.family for item in skeleton.brief.optional_experiences}
        role_by_day = {role.day_number: role for role in skeleton.brief.day_roles}
        expected_days = set(range(1, ledger.day_count + 1))
        if len(role_by_day) != len(skeleton.brief.day_roles) or set(role_by_day) != expected_days:
            raise ValueError("creative_portfolio_day_role_coverage_invalid")
        slot_ids = {slot.slot_id for slot in skeleton.day_slots}
        slots_by_id = {slot.slot_id: slot for slot in skeleton.day_slots}
        if len(slot_ids) != len(skeleton.day_slots):
            raise ValueError("creative_portfolio_duplicate_slot_id")
        route_anchor_counts = {day: 0 for day in expected_days}
        for slot in skeleton.day_slots:
            if slot.day_number > ledger.day_count:
                raise ValueError("creative_portfolio_day_number_invalid")
            if slot.route_anchor:
                route_anchor_counts[slot.day_number] += 1
            if slot.required_goal_id and slot.soft_goal_id:
                raise ValueError("creative_portfolio_slot_goal_identity_ambiguous")
            if slot.required_goal_id and slot.required_goal_id not in required:
                raise ValueError("creative_portfolio_required_goal_invalid")
            if slot.soft_goal_id and slot.soft_goal_id not in soft:
                raise ValueError("creative_portfolio_soft_goal_invalid")
            if slot.optional_experience_family and slot.optional_experience_family not in optional_families:
                raise ValueError("creative_portfolio_optional_family_invalid")
        required_pool_goals: set[str] = set()
        soft_pool_goals: set[str] = set()
        for pool in skeleton.intent_pools:
            if pool.brief_id != skeleton.brief.brief_id:
                raise ValueError("creative_portfolio_cross_brief_pool")
            if not pool.assign_to_slots or not set(pool.assign_to_slots) <= slot_ids:
                raise ValueError("creative_portfolio_cross_brief_slot")
            assigned_slots = [slots_by_id[slot_id] for slot_id in pool.assign_to_slots]
            if any(not slot.route_anchor for slot in assigned_slots):
                raise ValueError(
                    "creative_portfolio_required_pool_route_anchor_invalid"
                    if pool.requirement_level == "required"
                    else "creative_portfolio_pool_route_anchor_invalid"
                )
            if pool.requirement_level == "required":
                if pool.goal_id not in required or pool.soft_goal_id is not None:
                    raise ValueError("creative_portfolio_required_goal_invalid")
                required_pool_goals.add(str(pool.goal_id))
                if any(slot.required_goal_id != pool.goal_id for slot in assigned_slots):
                    raise ValueError("creative_portfolio_required_pool_slot_goal_mismatch")
                if pool.optional_experience_family:
                    raise ValueError("creative_portfolio_required_pool_optional_family")
            else:
                if pool.goal_id is not None:
                    raise ValueError("creative_portfolio_optional_pool_goal_id")
                if pool.soft_goal_id is not None:
                    if pool.soft_goal_id not in soft:
                        raise ValueError("creative_portfolio_soft_goal_invalid")
                    soft_pool_goals.add(pool.soft_goal_id)
                    if any(slot.soft_goal_id != pool.soft_goal_id for slot in assigned_slots):
                        raise ValueError("creative_portfolio_soft_pool_slot_goal_mismatch")
                elif pool.optional_experience_family not in optional_families:
                    raise ValueError("creative_portfolio_optional_family_invalid")
                elif any(slot.optional_experience_family != pool.optional_experience_family for slot in assigned_slots):
                    raise ValueError("creative_portfolio_optional_pool_slot_family_mismatch")
        for day_number, role in role_by_day.items():
            if role.target_route_anchors is None or not role.density_evidence:
                raise ValueError("creative_portfolio_day_anchor_target_missing")
            evidence = {
                key: value
                for item in role.density_evidence
                for key, separator, value in [str(item).partition("=")]
                if separator
            }
            required_keys = {
                "pace",
                "availableWindow",
                "requiredGoalCount",
                "explicitSoftGoalCount",
                "briefOptionalCount",
                "transport",
            }
            if not required_keys <= set(evidence):
                raise ValueError("creative_portfolio_density_evidence_incomplete")
            expected_counts = {
                "requiredGoalCount": sum(
                    1
                    for slot in skeleton.day_slots
                    if slot.day_number == day_number and slot.required_goal_id is not None
                ),
                "explicitSoftGoalCount": sum(
                    1 for slot in skeleton.day_slots if slot.day_number == day_number and slot.soft_goal_id is not None
                ),
                "briefOptionalCount": sum(
                    1
                    for slot in skeleton.day_slots
                    if slot.day_number == day_number and slot.optional_experience_family is not None
                ),
            }
            if any(str(expected) != evidence.get(key) for key, expected in expected_counts.items()):
                raise ValueError("creative_portfolio_density_evidence_count_mismatch")
            if evidence.get("pace") != ledger.pace:
                raise ValueError("creative_portfolio_density_evidence_pace_mismatch")
            expected_transport = ",".join(ledger.transport_preferences) or "unspecified"
            if evidence.get("transport") != expected_transport:
                raise ValueError("creative_portfolio_density_evidence_transport_mismatch")
            limit = int((day_anchor_limits or {}).get(day_number, 6))
            if role.target_route_anchors > limit:
                raise ValueError("creative_portfolio_day_anchor_target_exceeds_limit")
            if route_anchor_counts[day_number] != role.target_route_anchors:
                raise ValueError("creative_portfolio_day_anchor_target_slot_mismatch")
        if required_pool_goals != required:
            raise ValueError("creative_portfolio_required_pool_coverage_invalid")
        if soft_pool_goals != soft:
            raise ValueError("creative_portfolio_soft_pool_coverage_invalid")

    @staticmethod
    def _reject_grounded_facts(items: Any) -> None:
        forbidden = {
            "amapId",
            "poiId",
            "longitude",
            "latitude",
            "coordinates",
            "routeLeg",
            "openingHours",
            "ticketPrice",
        }

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if forbidden & set(value):
                    raise ValueError("creative_portfolio_contains_grounded_fact")
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(items)

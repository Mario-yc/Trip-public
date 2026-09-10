"""Bounded Provider-backed candidate assignment for Simple Direction.

Candidate discovery and Consumer Admission happen before this service. It may
use geometry only to order a bounded combination set; a combination is called
route-compliant only after every adjacent leg has complete Provider evidence.
The service is read-only and never persists route or itinerary rows.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from src.core.config import get_settings
from src.models.poi import POI
from src.models.poi_intent import PersistableSegmentPlan
from src.services.daily_route_overlap_service import DailyRouteOverlapService
from src.services.meal_diversity_policy import MealDiversityPolicy
from src.services.meal_experience_portfolio import MealExperiencePortfolioPolicy
from src.services.provider_route_insertion_service import ProviderRouteInsertionService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import AMAP_ROUTE_SOURCE
from src.services.simple_open_dynamic_schedule_service import SimpleOpenDynamicScheduleService


@dataclass(frozen=True)
class SimpleOpenRouteAssignmentResult:
    plans: list[PersistableSegmentPlan]
    audit: dict[str, Any]
    route_legs: list[dict[str, Any]]


class SimpleOpenRouteAssignmentService:
    """Try bounded compact combinations and verify only their adjacent legs.

    Geometry is a bounded topology gate, never Provider route evidence.  The
    route budget is spent only on pairs from topology-admitted combinations;
    failed pairs are cached, and no synthetic or city-wide baseline matrix is
    introduced.
    """

    MAX_COMBINATIONS = 32
    MAX_DAILY_ROUTE_OPTION_COMBINATIONS = 32
    MAX_ROUTE_OPTIONS_PER_LEG = 3

    def __init__(
        self,
        *,
        route_leg_provider: Any | None = None,
        route_overlap_service: DailyRouteOverlapService | None = None,
        route_overlap_policy: str | None = None,
    ) -> None:
        self.route_leg_provider = route_leg_provider or ProviderRouteInsertionService()
        self.route_overlap_service = route_overlap_service or DailyRouteOverlapService()
        self.meal_diversity_policy = MealDiversityPolicy()
        configured_policy = str(route_overlap_policy or get_settings().daily_route_overlap_policy).strip().casefold()
        self.route_overlap_policy = configured_policy if configured_policy in {"observe", "rank"} else "observe"

    def _effective_route_overlap_policy(
        self,
        contract: dict[str, Any],
        *,
        tolerance: dict[str, Any] | None,
        topology: dict[str, Any],
    ) -> tuple[str, str]:
        if self.route_overlap_policy == "rank":
            return "rank", "configured_rank_policy"
        source = str(contract.get("detourToleranceSource") or "").strip()
        try:
            max_detour_ratio = float((tolerance or {}).get("maxDetourRatio"))
            max_backtrack_ratio = float(topology.get("maxBacktrackRatio"))
        except (TypeError, ValueError):
            max_detour_ratio = math.nan
            max_backtrack_ratio = math.nan
        if (
            source in {"user_explicit", "opaque_clarification_answer", "controller_semantic_choice"}
            and math.isfinite(max_detour_ratio)
            and math.isfinite(max_backtrack_ratio)
            and max_detour_ratio <= max_backtrack_ratio
        ):
            return "rank", "explicit_low_detour_contract"
        return "observe", "configured_observe_policy"

    def assign(
        self,
        plans: Iterable[PersistableSegmentPlan],
        candidates_by_slot: dict[str, list[POI]],
        *,
        route_decision_contract: dict[str, Any] | None,
        transport_mode: str,
        route_budget: int,
        plan_id: str = "simple-direction-proposal",
    ) -> SimpleOpenRouteAssignmentResult:
        assigned = [copy.deepcopy(item) for item in plans]
        contract = route_decision_contract if isinstance(route_decision_contract, dict) else {}
        fingerprint = str(contract.get("fingerprint") or "")
        mobility = contract.get("mobilityProfile") if isinstance(contract.get("mobilityProfile"), dict) else None
        tolerance = contract.get("detourTolerance") if isinstance(contract.get("detourTolerance"), dict) else None
        adjacent = (
            contract.get("adjacentLegConstraint") if isinstance(contract.get("adjacentLegConstraint"), dict) else None
        )
        budget = max(0, int(route_budget or 0))
        topology = contract.get("topologyConstraint") if isinstance(contract.get("topologyConstraint"), dict) else {}
        effective_overlap_policy, overlap_policy_source = self._effective_route_overlap_policy(
            contract,
            tolerance=tolerance,
            topology=topology,
        )
        try:
            max_backtrack_ratio = float(topology.get("maxBacktrackRatio"))
            max_geometry_meters = float((adjacent or {}).get("candidateSearchRadiusMeters"))
            max_provider_minutes = float((adjacent or {}).get("maxProviderTravelMinutes"))
        except (TypeError, ValueError):
            max_backtrack_ratio = math.nan
            max_geometry_meters = math.nan
            max_provider_minutes = math.nan
        audit: dict[str, Any] = {
            "schemaVersion": "simple-open-route-evidence-v2",
            "decisionSource": "bounded_candidate_geometry_then_provider_final_legs",
            "routeContractFingerprint": fingerprint,
            "routePolicySource": str(contract.get("source") or ""),
            "detourEnvelope": copy.deepcopy(tolerance) if isinstance(tolerance, dict) else None,
            "adjacentLegConstraint": copy.deepcopy(adjacent) if isinstance(adjacent, dict) else None,
            "routeProviderAttemptCount": 0,
            "routeProviderCacheHitCount": 0,
            "topologyEvidence": {
                "evidenceSource": "bounded_candidate_geometry",
                "geometryUsedAsRouteFeasibilityEvidence": False,
                "perDay": [],
            },
            "topologyCompliance": "pending",
            "providerBaselineCompared": False,
            "selectedGeneralizedCost": None,
            "generalizedCostDelta": None,
            "detourRatio": None,
            "detourCompliance": "not_evaluated",
            "adjacentLegCompliance": "pending",
            "routeCoverageComplete": False,
            "expectedPairs": [],
            "verifiedPairs": [],
            "failureReason": None,
            "selectedCombination": [],
            "providerRoutePairs": [],
            "topologyCandidateCountByDay": [],
            "topologyCombinationFrontierCount": 0,
            "unverifiedTopologyCombinationCount": 0,
            "topologyCandidateAttemptCount": 0,
            "topologyCandidateAttempts": [],
            "selectedCombinationRouteBlocked": False,
            "routeFeasibilityExhausted": False,
            "incompleteDayNumbers": [],
            "verifiedDayNumbers": [],
            # ``maxBacktrackRatio`` remains the legacy waypoint/Haversine
            # proxy.  It is useful for bounding candidate geography but is not
            # evidence that actual roads were not repeated.
            "legacyBacktrackMetric": "waypoint_haversine_geometry_proxy",
            "legacyBacktrackMetricUsedForActualRoadOverlap": False,
            "dailyRouteOverlapPolicy": effective_overlap_policy,
            "dailyRouteOverlapPolicySource": overlap_policy_source,
            "dailyRouteOverlapStatus": "pending",
            "dailyRouteOverlapEvidence": {
                "schemaVersion": "daily-route-continuity-audit-v1",
                "perDay": [],
            },
        }
        if str(contract.get("status") or "") != "ready" or not fingerprint or mobility is None or tolerance is None:
            audit["failureReason"] = "route_decision_contract_not_ready"
            return SimpleOpenRouteAssignmentResult(assigned, audit, [])
        if (
            not adjacent
            or not math.isfinite(max_backtrack_ratio)
            or not 0 <= max_backtrack_ratio <= 1
            or not math.isfinite(max_geometry_meters)
            or max_geometry_meters <= 0
            or not math.isfinite(max_provider_minutes)
            or max_provider_minutes <= 0
        ):
            audit["failureReason"] = "topology_constraint_not_ready"
            return SimpleOpenRouteAssignmentResult(assigned, audit, [])

        by_day: dict[int, list[PersistableSegmentPlan]] = {}
        for plan in assigned:
            if self._plan_requires_route_edge(plan):
                by_day.setdefault(int(plan.day_number), []).append(plan)

        all_legs: list[dict[str, Any]] = []
        verified_day_count = 0
        leg_cache: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        topology_days: list[
            tuple[int, list[PersistableSegmentPlan], list[tuple[float, tuple[POI, ...], dict[str, Any]]]]
        ] = []
        for day_number in sorted(by_day):
            controller_sequence_authoritative = SimpleOpenDynamicScheduleService.controller_sequence_authoritative(
                by_day[day_number]
            )
            day_plans = sorted(
                by_day[day_number],
                key=lambda plan: self._plan_order_key(
                    plan,
                    controller_sequence_authoritative=controller_sequence_authoritative,
                ),
            )
            selected_queues: list[list[POI]] = []
            day_assignment_incomplete = False
            single_stop_day = len(day_plans) == 1
            for plan in day_plans:
                slot_id = str(plan.planning_slot_id or "")
                exact_entity = self._is_exact_entity_plan(plan)
                admitted_candidates = self._canonical_candidates(candidates_by_slot.get(slot_id) or [])
                fixed_poi = plan.selected_poi
                if exact_entity and fixed_poi is None:
                    if len(admitted_candidates) != 1:
                        audit["failureReason"] = (
                            "fixed_poi_identity_missing" if not admitted_candidates else "fixed_poi_identity_ambiguous"
                        )
                        return SimpleOpenRouteAssignmentResult(assigned, audit, all_legs)
                    fixed_poi = admitted_candidates[0]
                if single_stop_day:
                    fixed_identity = (
                        str(fixed_poi.amap_id or fixed_poi.id or "").strip().upper()
                        if fixed_poi is not None
                        else ""
                    )
                    admitted_fixed_poi = next(
                        (
                            candidate
                            for candidate in admitted_candidates
                            if str(candidate.amap_id or candidate.id or "").strip().upper() == fixed_identity
                        ),
                        None,
                    )
                    # A single stop has no adjacent Provider leg, so current
                    # Consumer Admission is the only proof that a replaceable
                    # materialized identity is still valid. Exact user-bound
                    # entities retain their existing identity contract.
                    primary = (
                        fixed_poi
                        if exact_entity
                        else admitted_fixed_poi
                        or (admitted_candidates[0] if admitted_candidates else None)
                    )
                    candidates = [copy.deepcopy(primary)] if primary is not None else []
                else:
                    candidates = (
                        [copy.deepcopy(fixed_poi)] if exact_entity and fixed_poi is not None else admitted_candidates
                    )
                if not candidates and plan.selected_poi is not None and not single_stop_day:
                    candidates = [plan.selected_poi]
                if not candidates:
                    audit["failureReason"] = "candidate_assignment_incomplete"
                    audit["incompleteDayNumbers"].append(day_number)
                    day_assignment_incomplete = True
                    break
                selected_queues.append(candidates[:5])

            if day_assignment_incomplete:
                continue

            materialized_combination: tuple[POI, ...] | None = None
            if all(plan.selected_poi is not None for plan in day_plans):
                rebound_materialized: list[POI] = []
                for plan, queue in zip(day_plans, selected_queues):
                    selected_identity = str(
                        plan.selected_poi.amap_id or plan.selected_poi.id or ""
                    ).strip().upper()
                    admitted_match = next(
                        (
                            candidate
                            for candidate in queue
                            if str(candidate.amap_id or candidate.id or "").strip().upper()
                            == selected_identity
                        ),
                        None,
                    )
                    if admitted_match is None:
                        rebound_materialized = []
                        break
                    rebound_materialized.append(admitted_match)
                if len(rebound_materialized) == len(day_plans):
                    materialized_combination = tuple(rebound_materialized)

            selected_combinations = list(
                itertools.islice(itertools.product(*selected_queues), self.MAX_COMBINATIONS)
            )
            materialized_candidate_forced = False
            if materialized_combination is not None:
                materialized_signature = self._combination_signature(materialized_combination)
                materialized_already_bounded = any(
                    self._combination_signature(combination) == materialized_signature
                    for combination in selected_combinations
                )
                if not materialized_already_bounded:
                    materialized_candidate_forced = True
                    if len(selected_combinations) >= self.MAX_COMBINATIONS:
                        selected_combinations[-1] = materialized_combination
                    else:
                        selected_combinations.append(materialized_combination)
            topology_candidates: list[tuple[float, tuple[POI, ...], dict[str, Any]]] = []
            for combination in selected_combinations:
                identities = [str(item.amap_id or item.id or "").strip().upper() for item in combination]
                if len(set(identities)) != len(identities):
                    continue
                adjacent_meters = [
                    self._haversine_km(left, right) * 1000 for left, right in zip(combination, combination[1:])
                ]
                if any(not math.isfinite(value) or value > max_geometry_meters for value in adjacent_meters):
                    continue
                ordered_geometry = sum(adjacent_meters)
                best_geometry = self._best_same_start_geometry_meters(combination)
                backtrack_ratio = max(0.0, ordered_geometry / max(best_geometry, 1.0) - 1.0)
                evidence = {
                    "dayNumber": day_number,
                    "daySeedAmapId": identities[0] if identities else "",
                    "orderedAmapIds": identities,
                    "adjacentGeometryMeters": [round(value, 2) for value in adjacent_meters],
                    "orderedGeometryMeters": round(ordered_geometry, 2),
                    "bestSameStartPermutationMeters": round(best_geometry, 2),
                    "backtrackRatio": round(backtrack_ratio, 6),
                }
                if backtrack_ratio <= max_backtrack_ratio:
                    topology_candidates.append((ordered_geometry, combination, evidence))
            if not topology_candidates:
                exact_constraint_conflict = any(self._is_exact_entity_plan(plan) for plan in day_plans)
                for plan, queue in zip(day_plans, selected_queues):
                    if self._is_exact_entity_plan(plan) and queue:
                        plan.selected_poi = copy.deepcopy(queue[0])
                        plan.display_title = str(queue[0].name or plan.display_title)
                        plan.grounding_status = "verified_amap"
                        plan.notes = "用户明确指定的地点已保留，但与当前日内紧凑性约束冲突。"
                audit.update(
                    {
                        "topologyCompliance": "failed",
                        "failureReason": (
                            "fixed_poi_route_constraint_conflict"
                            if exact_constraint_conflict
                            else "topology_constraint_exceeded"
                        ),
                    }
                )
                return SimpleOpenRouteAssignmentResult(assigned, audit, all_legs)
            topology_candidates.sort(
                key=lambda item: (
                    item[0],
                    self._combination_signature(item[1]),
                )
            )
            geometry_ranked_candidates = [
                (
                    geometry_meters,
                    selected,
                    {**topology_evidence, "geometryRank": geometry_rank},
                )
                for geometry_rank, (geometry_meters, selected, topology_evidence) in enumerate(
                    topology_candidates,
                    start=1,
                )
            ]
            materialized_signature = self._combination_signature(
                tuple(
                    plan.selected_poi
                    for plan in day_plans
                    if plan.selected_poi is not None
                )
            )
            materialized_candidate = next(
                (
                    candidate
                    for candidate in geometry_ranked_candidates
                    if len(materialized_signature) == len(day_plans)
                    and self._combination_signature(candidate[1]) == materialized_signature
                ),
                None,
            )
            # Provider calls must first verify the topology that this proposal
            # will actually display. Geometry still determines and records the
            # bounded frontier, but spending the fixed route budget on a nearer
            # sibling before the materialized topology can leave Day 2 with no
            # route evidence for the places shown to the user.
            provider_ranked_candidates = (
                [materialized_candidate]
                + [candidate for candidate in geometry_ranked_candidates if candidate is not materialized_candidate]
                if materialized_candidate is not None
                else geometry_ranked_candidates
            )
            topology_candidates = [
                (
                    geometry_meters,
                    selected,
                    {**topology_evidence, "providerPriorityRank": provider_priority_rank},
                )
                for provider_priority_rank, (geometry_meters, selected, topology_evidence) in enumerate(
                    provider_ranked_candidates,
                    start=1,
                )
            ]
            audit["topologyCandidateCountByDay"].append(
                {
                    "dayNumber": day_number,
                    "candidateCount": len(topology_candidates),
                    "materializedCandidateGeometryRank": (
                        int(materialized_candidate[2].get("geometryRank") or 0)
                        if materialized_candidate is not None
                        else None
                    ),
                    "materializedCandidatePrioritized": materialized_candidate is not None,
                    "materializedCandidateForcedIntoBoundedFrontier": materialized_candidate_forced,
                    "materializedCandidateSurvivedTopologyFilter": materialized_candidate is not None,
                }
            )
            topology_days.append((day_number, day_plans, topology_candidates))

        # Geometry and Provider route evidence are intentionally independent.
        # Complete the bounded topology pass for every day before spending any
        # Provider budget so a missing route leg cannot erase truthful geometry
        # evidence or masquerade as a topology failure.
        if by_day and len(topology_days) == len(by_day):
            audit["topologyCompliance"] = "verified"
            frontier_count = math.prod(
                max(0, int(item.get("candidateCount") or 0))
                for item in audit["topologyCandidateCountByDay"]
                if isinstance(item, dict)
            )
            audit["topologyCombinationFrontierCount"] = frontier_count
            audit["unverifiedTopologyCombinationCount"] = frontier_count

        day_states: dict[int, dict[str, Any]] = {
            day_number: {
                "verified": False,
                "lastFailureReason": "provider_route_matrix_incomplete",
                "lastAdjacentStatus": "pending",
                "lastAttempted": None,
                "selectionRecorded": False,
                "budgetBlocked": False,
                "attemptedCandidateCount": 0,
                "providerAttemptCount": 0,
                "alternativeProviderAttemptCount": 0,
            }
            for day_number, _day_plans, _topology_candidates in topology_days
        }
        selected_meal_brands: set[str] = set()
        selected_meal_families: set[str] = set()
        max_candidate_rank = max((len(candidates) for _day, _plans, candidates in topology_days), default=0)
        # Spend the fixed Provider budget coverage-first: every day gets its
        # materialized topology (or geometry rank 1 when none is materialized)
        # before any day explores a sibling. This keeps same-day alternatives
        # bounded without starving a later day or the actual displayed plan.
        for candidate_rank in range(1, max_candidate_rank + 1):
            for day_number, day_plans, topology_candidates in topology_days:
                state = day_states[day_number]
                if (
                    state["verified"] is True
                    or state["budgetBlocked"] is True
                    or candidate_rank > len(topology_candidates)
                ):
                    continue
                candidate = topology_candidates[candidate_rank - 1]
                geometry_meters, selected, topology_evidence = candidate
                meal_duplicate_reason = self._meal_combination_duplicate_reason(
                    day_plans,
                    selected,
                    prior_brands=selected_meal_brands,
                    prior_families=selected_meal_families,
                )
                if meal_duplicate_reason:
                    state["attemptedCandidateCount"] += 1
                    state["lastFailureReason"] = meal_duplicate_reason
                    state["lastAdjacentStatus"] = "failed"
                    audit["topologyCandidateAttempts"].append(
                        {
                            "dayNumber": day_number,
                            "candidateRank": candidate_rank,
                            "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                            "providerPriorityRank": candidate_rank,
                            "amapIds": list(self._combination_signature(selected)),
                            "status": meal_duplicate_reason,
                            "providerAttemptCountAfter": audit["routeProviderAttemptCount"],
                        }
                    )
                    continue
                pair_keys = [
                    (
                        str(left.amap_id or left.id),
                        str(right.amap_id or right.id),
                        transport_mode,
                        effective_overlap_policy,
                    )
                    for left, right in zip(selected, selected[1:])
                ]
                new_pair_count = len({key for key in pair_keys if key not in leg_cache})
                if (
                    candidate_rank > 1
                    and state["alternativeProviderAttemptCount"] + new_pair_count > 2
                ):
                    audit["topologyCandidateAttempts"].append(
                        {
                            "dayNumber": day_number,
                            "candidateRank": candidate_rank,
                            "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                            "providerPriorityRank": candidate_rank,
                            "amapIds": list(self._combination_signature(selected)),
                            "status": "not_attempted_day_alternative_route_budget_exhausted",
                            "newProviderPairCount": new_pair_count,
                            "dayAlternativeProviderAttemptCount": state["alternativeProviderAttemptCount"],
                            "dayAlternativeProviderAttemptLimit": 2,
                        }
                    )
                    state["lastFailureReason"] = "provider_route_budget_insufficient"
                    state["budgetBlocked"] = True
                    continue
                if audit["routeProviderAttemptCount"] + new_pair_count > budget:
                    materialized_signature = self._combination_signature(
                        tuple(
                            plan.selected_poi
                            for plan in day_plans
                            if plan.selected_poi is not None
                        )
                    )
                    materialized_candidate = next(
                        (
                            current
                            for current in topology_candidates
                            if len(materialized_signature) == len(day_plans)
                            and self._combination_signature(current[1]) == materialized_signature
                        ),
                        None,
                    )
                    if materialized_candidate is not None:
                        (
                            materialized_geometry,
                            materialized_selected,
                            materialized_topology_evidence,
                        ) = materialized_candidate
                        self._record_selected_topology_candidate(
                            audit,
                            day_number=day_number,
                            day_plans=day_plans,
                            geometry_meters=materialized_geometry,
                            selected=materialized_selected,
                            topology_evidence=materialized_topology_evidence,
                        )
                        state["selectionRecorded"] = True
                    audit["topologyCandidateAttempts"].append(
                        {
                            "dayNumber": day_number,
                            "candidateRank": candidate_rank,
                            "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                            "providerPriorityRank": candidate_rank,
                            "amapIds": list(self._combination_signature(selected)),
                            "status": "not_attempted_route_budget_insufficient",
                            "newProviderPairCount": new_pair_count,
                        }
                    )
                    state["lastFailureReason"] = "provider_route_budget_insufficient"
                    state["budgetBlocked"] = True
                    continue

                audit["topologyCandidateAttemptCount"] += 1
                state["attemptedCandidateCount"] += 1
                state["lastAttempted"] = candidate
                option_queues: list[list[dict[str, Any]]] = []
                candidate_failure_reason = ""
                candidate_adjacent_status = "pending"
                for (left, right), key in zip(zip(selected, selected[1:]), pair_keys):
                    if key in leg_cache:
                        options = leg_cache[key]
                        audit["routeProviderCacheHitCount"] += 1
                    else:
                        audit["routeProviderAttemptCount"] += 1
                        state["providerAttemptCount"] += 1
                        if candidate_rank > 1:
                            state["alternativeProviderAttemptCount"] += 1
                        options = self._provider_route_options(
                            plan_id=f"{plan_id}:day:{day_number}",
                            left=left,
                            right=right,
                            transport_mode=transport_mode,
                            route_overlap_policy=effective_overlap_policy,
                        )
                        leg_cache[key] = options
                    complete_options = [
                        option
                        for option in options
                        if self._is_complete_route_option(
                            option,
                            left=left,
                            right=right,
                            transport_mode=transport_mode,
                            route_overlap_policy=effective_overlap_policy,
                        )
                    ]
                    if not complete_options:
                        candidate_failure_reason = "provider_route_matrix_incomplete"
                        candidate_adjacent_status = "pending"
                        break
                    eligible_options = self._eligible_route_options(
                        complete_options,
                        mobility_profile=mobility,
                        detour_tolerance=tolerance,
                        max_provider_distance_meters=max_geometry_meters,
                        max_provider_minutes=max_provider_minutes,
                        apply_cost_envelope=effective_overlap_policy == "rank",
                    )
                    if not eligible_options:
                        candidate_failure_reason = "adjacent_leg_limit_exceeded"
                        candidate_adjacent_status = "failed"
                        break
                    option_queues.append(eligible_options[: self.MAX_ROUTE_OPTIONS_PER_LEG])

                if candidate_failure_reason:
                    state["lastFailureReason"] = candidate_failure_reason
                    state["lastAdjacentStatus"] = candidate_adjacent_status
                    audit["topologyCandidateAttempts"].append(
                        {
                            "dayNumber": day_number,
                            "candidateRank": candidate_rank,
                            "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                            "providerPriorityRank": candidate_rank,
                            "amapIds": list(self._combination_signature(selected)),
                            "status": candidate_failure_reason,
                            "providerAttemptCountAfter": audit["routeProviderAttemptCount"],
                        }
                    )
                    continue

                available_combination_count = (
                    math.prod(len(options) for options in option_queues) if option_queues else 1
                )
                combinations = list(
                    itertools.islice(
                        itertools.product(*option_queues),
                        self.MAX_DAILY_ROUTE_OPTION_COMBINATIONS,
                    )
                )
                if not option_queues:
                    combinations = [tuple()]
                if not combinations:
                    state["lastFailureReason"] = "provider_route_matrix_incomplete"
                    state["lastAdjacentStatus"] = "pending"
                    audit["topologyCandidateAttempts"].append(
                        {
                            "dayNumber": day_number,
                            "candidateRank": candidate_rank,
                            "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                            "providerPriorityRank": candidate_rank,
                            "amapIds": list(self._combination_signature(selected)),
                            "status": state["lastFailureReason"],
                            "providerAttemptCountAfter": audit["routeProviderAttemptCount"],
                        }
                    )
                    continue

                if effective_overlap_policy == "rank":
                    evaluated = [
                        (
                            combination,
                            self.route_overlap_service.evaluate(
                                day_number=day_number,
                                route_legs=combination,
                                alternatives_evaluated=1,
                                selected_alternative_ids=self._route_option_ids(combination),
                            ),
                        )
                        for combination in combinations
                    ]
                    selected_combination, _provisional_evidence = min(
                        evaluated,
                        key=lambda item: self._route_option_rank_key(
                            item[0],
                            item[1],
                            mobility_profile=mobility,
                        ),
                    )
                    alternatives_evaluated = len(evaluated)
                else:
                    selected_combination = combinations[0]
                    alternatives_evaluated = 1

                legs = [copy.deepcopy(item) for item in selected_combination]
                for pair_ordinal, (leg, left_plan, right_plan, left_poi, right_poi) in enumerate(
                    zip(legs, day_plans, day_plans[1:], selected, selected[1:]),
                    start=1,
                ):
                    leg.update(
                        self._route_pair_identity(
                            day_number=day_number,
                            pair_ordinal=pair_ordinal,
                            left_plan=left_plan,
                            right_plan=right_plan,
                            left_poi=left_poi,
                            right_poi=right_poi,
                        )
                    )
                overlap_evidence = self.route_overlap_service.evaluate(
                    day_number=day_number,
                    route_legs=legs,
                    alternatives_evaluated=alternatives_evaluated,
                    selected_alternative_ids=self._route_option_ids(legs),
                )
                if effective_overlap_policy == "rank":
                    try:
                        route_overlap_ratio = float(overlap_evidence.get("overlapRatio"))
                        allowed_overlap_ratio = min(
                            max_backtrack_ratio,
                            float((tolerance or {}).get("maxDetourRatio")),
                        )
                    except (TypeError, ValueError):
                        route_overlap_ratio = math.nan
                        allowed_overlap_ratio = math.nan
                    if (
                        str(overlap_evidence.get("status") or "") == "route_geometry_pending"
                        or not math.isfinite(route_overlap_ratio)
                        or not math.isfinite(allowed_overlap_ratio)
                        or route_overlap_ratio > allowed_overlap_ratio
                    ):
                        state["lastFailureReason"] = (
                            "daily_route_overlap_geometry_pending"
                            if str(overlap_evidence.get("status") or "") == "route_geometry_pending"
                            else "daily_route_backtrack_limit_exceeded"
                        )
                        state["lastAdjacentStatus"] = "failed"
                        audit["topologyCandidateAttempts"].append(
                            {
                                "dayNumber": day_number,
                                "candidateRank": candidate_rank,
                                "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                                "providerPriorityRank": candidate_rank,
                                "amapIds": list(self._combination_signature(selected)),
                                "status": state["lastFailureReason"],
                                "providerAttemptCountAfter": audit["routeProviderAttemptCount"],
                                "routeOverlapRatio": (
                                    route_overlap_ratio if math.isfinite(route_overlap_ratio) else None
                                ),
                                "maxRouteOverlapRatio": (
                                    allowed_overlap_ratio if math.isfinite(allowed_overlap_ratio) else None
                                ),
                            }
                        )
                        continue
                if overlap_evidence["status"] == "route_geometry_pending":
                    selection_status = "route_geometry_pending"
                elif available_combination_count <= 1:
                    selection_status = "evaluated_single_option"
                elif effective_overlap_policy == "rank":
                    selection_status = "ranked_bounded_options"
                else:
                    selection_status = "observed_first_option"
                overlap_evidence.update(
                    {
                        "selectionStatus": selection_status,
                        "boundedRouteOptionCombinationCount": len(combinations),
                        "availableRouteOptionCombinationCount": available_combination_count,
                        "routeOptionCombinationTruncated": (
                            available_combination_count > self.MAX_DAILY_ROUTE_OPTION_COMBINATIONS
                        ),
                    }
                )
                self._refresh_overlap_evidence_fingerprint(overlap_evidence)
                audit["dailyRouteOverlapEvidence"]["perDay"].append(overlap_evidence)
                self._record_selected_topology_candidate(
                    audit,
                    day_number=day_number,
                    day_plans=day_plans,
                    geometry_meters=geometry_meters,
                    selected=selected,
                    topology_evidence=topology_evidence,
                )
                state["selectionRecorded"] = True
                audit["topologyCandidateAttempts"].append(
                    {
                        "dayNumber": day_number,
                        "candidateRank": candidate_rank,
                        "geometryRank": int(topology_evidence.get("geometryRank") or candidate_rank),
                        "providerPriorityRank": candidate_rank,
                        "amapIds": list(self._combination_signature(selected)),
                        "status": "verified",
                        "providerAttemptCountAfter": audit["routeProviderAttemptCount"],
                    }
                )
                verified_day_count += 1
                audit["verifiedDayNumbers"].append(day_number)
                all_legs.extend(legs)
                for plan, poi in zip(day_plans, selected):
                    plan.selected_poi = copy.deepcopy(poi)
                    plan.display_title = str(poi.name or plan.display_title)
                    plan.grounding_status = "verified_amap"
                    plan.requires_route_edge = True
                    if str(plan.intent_type or "") == "meal":
                        brand = self.meal_diversity_policy.canonical_meal_brand(poi)
                        family = self.meal_diversity_policy.dish_family(poi)
                        if brand:
                            selected_meal_brands.add(brand)
                        if family:
                            selected_meal_families.add(family)
                for index, plan in enumerate(day_plans):
                    constraints = copy.deepcopy(plan.schedule_constraints or {})
                    if str(plan.intent_type or "") == "meal" and plan.selected_poi is not None:
                        constraints["mealSemanticEvidence"] = copy.deepcopy(
                            plan.selected_poi.meal_semantic_evidence
                        )
                    if index > 0 and index - 1 < len(legs):
                        leg = legs[index - 1]
                        duration_seconds = self._route_duration_seconds(leg)
                        if duration_seconds is not None:
                            constraints["routeTravelMinutesFromPrevious"] = max(
                                1,
                                int(math.ceil(duration_seconds / 60)),
                            )
                            constraints["routeArrivalSource"] = "verified_provider_route_matrix"
                    plan.schedule_constraints = constraints
                state["verified"] = True

        failed_route_days: list[tuple[int, dict[str, Any]]] = []
        remaining_candidate_factors: list[int] = []
        for day_number, day_plans, topology_candidates in topology_days:
            state = day_states[day_number]
            if state["verified"] is True:
                remaining_candidate_factors.append(1)
                continue
            if state["selectionRecorded"] is not True and state["lastAttempted"] is not None:
                geometry_meters, selected, topology_evidence = state["lastAttempted"]
                self._record_selected_topology_candidate(
                    audit,
                    day_number=day_number,
                    day_plans=day_plans,
                    geometry_meters=geometry_meters,
                    selected=selected,
                    topology_evidence=topology_evidence,
                )
                state["selectionRecorded"] = True
            if day_number not in audit["incompleteDayNumbers"]:
                audit["incompleteDayNumbers"].append(day_number)
            remaining_candidate_count = max(
                len(topology_candidates) - int(state["attemptedCandidateCount"]),
                0,
            )
            remaining_candidate_factors.append(remaining_candidate_count)
            failed_route_days.append((day_number, state))

        if failed_route_days:
            _first_failed_day, first_failed_state = failed_route_days[0]
            audit["failureReason"] = first_failed_state["lastFailureReason"]
            audit["adjacentLegCompliance"] = first_failed_state["lastAdjacentStatus"]
            audit["selectedCombinationRouteBlocked"] = True
            audit["routeFeasibilityExhausted"] = any(
                state["budgetBlocked"] is not True
                and int(state["attemptedCandidateCount"]) == len(topology_candidates)
                for day_number, _day_plans, topology_candidates in topology_days
                for state in (day_states[day_number],)
                if state["verified"] is not True
            )
            audit["unverifiedTopologyCombinationCount"] = (
                math.prod(remaining_candidate_factors) if remaining_candidate_factors else 0
            )

        audit["verifiedDayNumbers"] = sorted(set(audit["verifiedDayNumbers"]))
        audit["incompleteDayNumbers"] = sorted(set(audit["incompleteDayNumbers"]))
        audit["topologyEvidence"]["perDay"].sort(key=lambda item: int(item.get("dayNumber") or 0))
        audit["selectedCombination"].sort(key=lambda item: int(item.get("dayNumber") or 0))
        audit["expectedPairs"].sort(
            key=lambda item: (int(item.get("dayNumber") or 0), int(item.get("pairOrdinal") or 0))
        )
        audit["dailyRouteOverlapEvidence"]["perDay"].sort(
            key=lambda item: int(item.get("dayNumber") or 0)
        )
        all_legs.sort(
            key=lambda item: (int(item.get("dayNumber") or 0), int(item.get("pairOrdinal") or 0))
        )

        overlap_statuses = [
            str(item.get("selectionStatus") or "")
            for item in audit["dailyRouteOverlapEvidence"]["perDay"]
            if isinstance(item, dict)
        ]
        if overlap_statuses:
            if "route_geometry_pending" in overlap_statuses:
                audit["dailyRouteOverlapStatus"] = "route_geometry_pending"
            elif all(status == "evaluated_single_option" for status in overlap_statuses):
                audit["dailyRouteOverlapStatus"] = "evaluated_single_option"
            elif effective_overlap_policy == "rank":
                audit["dailyRouteOverlapStatus"] = "ranked_bounded_options"
            else:
                audit["dailyRouteOverlapStatus"] = "observed_first_option"

        if all_legs:
            audit["providerRoutePairs"] = [
                {
                    "dayNumber": leg.get("dayNumber"),
                    "pairOrdinal": leg.get("pairOrdinal"),
                    "fromSegmentId": str(leg.get("fromSegmentId") or ""),
                    "toSegmentId": str(leg.get("toSegmentId") or ""),
                    "fromAmapId": str(leg.get("fromAmapId") or ""),
                    "toAmapId": str(leg.get("toAmapId") or ""),
                    "provider": str(leg.get("provider") or leg.get("source") or ""),
                    "routeOptionId": str(leg.get("routeOptionId") or ""),
                }
                for leg in all_legs
            ]
            audit["verifiedPairs"] = self._verified_route_pair_summary(all_legs, transport_mode)
        audit["routeComfortEvidence"] = MealExperiencePortfolioPolicy.route_comfort_evidence(all_legs)

        if by_day and verified_day_count == len(by_day):
            audit.update(
                {
                    "topologyCompliance": "verified",
                    "detourCompliance": "not_evaluated",
                    "adjacentLegCompliance": "verified",
                    "routeCoverageComplete": True,
                    "failureReason": None,
                    "providerRoutePairs": [
                        {
                            "dayNumber": leg.get("dayNumber"),
                            "pairOrdinal": leg.get("pairOrdinal"),
                            "fromSegmentId": str(leg.get("fromSegmentId") or ""),
                            "toSegmentId": str(leg.get("toSegmentId") or ""),
                            "fromAmapId": str(leg.get("fromAmapId") or ""),
                            "toAmapId": str(leg.get("toAmapId") or ""),
                            "provider": str(leg.get("provider") or leg.get("source") or ""),
                            "routeOptionId": str(leg.get("routeOptionId") or ""),
                        }
                        for leg in all_legs
                    ],
                    "verifiedPairs": self._verified_route_pair_summary(all_legs, transport_mode),
                }
            )
        return SimpleOpenRouteAssignmentResult(assigned, audit, all_legs)

    def _meal_combination_duplicate_reason(
        self,
        day_plans: list[PersistableSegmentPlan],
        selected: tuple[POI, ...],
        *,
        prior_brands: set[str],
        prior_families: set[str],
    ) -> str:
        day_brands: set[str] = set()
        day_families: set[str] = set()
        for plan, poi in zip(day_plans, selected):
            if str(plan.intent_type or "") != "meal":
                continue
            reason = self.meal_diversity_policy.duplicate_reason(
                poi,
                day_brands,
                prior_brands,
                day_families,
                prior_families,
            )
            if reason:
                return (
                    "simple_direction_meal_brand_repeated"
                    if "brand" in reason
                    else "simple_direction_meal_family_repeated"
                )
            brand = self.meal_diversity_policy.canonical_meal_brand(poi)
            family = self.meal_diversity_policy.dish_family(poi)
            if brand:
                day_brands.add(brand)
            if family:
                day_families.add(family)
        return ""

    def _provider_route_options(
        self,
        *,
        plan_id: str,
        left: POI,
        right: POI,
        transport_mode: str,
        route_overlap_policy: str,
    ) -> list[dict[str, Any]]:
        left_payload = self.provider_poi_payload(left)
        right_payload = self.provider_poi_payload(right)
        options_method = getattr(self.route_leg_provider, "verified_leg_options", None)
        if route_overlap_policy == "rank" and callable(options_method):
            raw = options_method(
                plan_id=plan_id,
                left=left_payload,
                right=right_payload,
                transport_mode=transport_mode,
            )
            values = list(raw) if isinstance(raw, (list, tuple)) else []
        else:
            leg = self.route_leg_provider.verified_leg(
                plan_id=plan_id,
                left=left_payload,
                right=right_payload,
                transport_mode=transport_mode,
            )
            values = [leg] if isinstance(leg, dict) else []
        result: list[dict[str, Any]] = []
        for index, value in enumerate(values[: self.MAX_ROUTE_OPTIONS_PER_LEG], start=1):
            if not isinstance(value, dict):
                continue
            option = copy.deepcopy(value)
            if not str(option.get("mode") or option.get("transportMode") or "").strip():
                option["mode"] = self._canonical_transport_mode(transport_mode)
            if not str(option.get("routeOptionId") or "").strip():
                option["routeOptionId"] = self._stable_route_option_id(option, index=index)
            result.append(option)
        return result

    def _is_complete_route_option(
        self,
        option: dict[str, Any],
        *,
        left: POI,
        right: POI,
        transport_mode: str,
        route_overlap_policy: str,
    ) -> bool:
        expected_mode = self._canonical_transport_mode(transport_mode)
        actual_mode = self._canonical_transport_mode(option.get("mode") or option.get("transportMode"))
        mode_matches = route_overlap_policy != "rank" or actual_mode == expected_mode
        return bool(
            str(option.get("fromAmapId") or "").strip().upper() == str(left.amap_id or left.id or "").strip().upper()
            and str(option.get("toAmapId") or "").strip().upper()
            == str(right.amap_id or right.id or "").strip().upper()
            and self._route_duration_seconds(option) is not None
            and self._route_distance_meters(option) is not None
            and str(option.get("provider") or option.get("source") or "") == AMAP_ROUTE_SOURCE
            and bool(str(option.get("queriedAt") or "").strip())
            and mode_matches
            and expected_mode == "transit"
        )

    @classmethod
    def _eligible_route_options(
        cls,
        options: list[dict[str, Any]],
        *,
        mobility_profile: dict[str, Any],
        detour_tolerance: dict[str, Any],
        max_provider_distance_meters: float,
        max_provider_minutes: float,
        apply_cost_envelope: bool,
    ) -> list[dict[str, Any]]:
        within_envelope = [
            option
            for option in options
            if (cls._route_duration_seconds(option) or math.inf) <= max_provider_minutes * 60
            and (cls._route_distance_meters(option) or math.inf) <= max_provider_distance_meters
        ]
        if not within_envelope or not apply_cost_envelope:
            return within_envelope
        costs = [RouteInsertionScorer._generalized_cost(option, mobility_profile) for option in within_envelope]
        if not costs or costs[0] is None:
            return []
        baseline = float(costs[0][0])
        try:
            max_delta = float(detour_tolerance["maxGeneralizedCostDelta"])
            max_ratio = float(detour_tolerance["maxDetourRatio"])
        except (KeyError, TypeError, ValueError):
            return []
        eligible: list[dict[str, Any]] = []
        for option, cost in zip(within_envelope, costs):
            if cost is None:
                continue
            delta = float(cost[0]) - baseline
            ratio = delta / baseline if baseline > 0 else math.inf
            if delta <= max_delta and ratio <= max_ratio:
                eligible.append(option)
        return eligible

    @classmethod
    def _route_option_rank_key(
        cls,
        combination: tuple[dict[str, Any], ...],
        evidence: dict[str, Any],
        *,
        mobility_profile: dict[str, Any],
    ) -> tuple[float, float, float, float, str]:
        geometry_pending = str(evidence.get("status") or "") == "route_geometry_pending"
        non_exempt_ratio = math.inf if geometry_pending else float(evidence.get("overlapRatio") or 0)
        reverse = math.inf if geometry_pending else float(evidence.get("reverseDirectionRepeatedMeters") or 0)
        generalized = 0.0
        duration = 0.0
        for option in combination:
            cost = RouteInsertionScorer._generalized_cost(option, mobility_profile)
            if cost is None:
                generalized = math.inf
            elif math.isfinite(generalized):
                generalized += float(cost[0])
            duration += float(cls._route_duration_seconds(option) or math.inf)
        signature = "|".join(cls._route_option_ids(combination))
        return non_exempt_ratio, reverse, generalized, duration, signature

    @staticmethod
    def _route_option_ids(options: Iterable[dict[str, Any]]) -> list[str]:
        return [str(option.get("routeOptionId") or "") for option in options]

    @staticmethod
    def _stable_route_option_id(option: dict[str, Any], *, index: int) -> str:
        material = {
            "providerEvidenceFingerprint": DailyRouteOverlapService.provider_evidence_fingerprint(option),
            "polyline": option.get("polyline"),
            "steps": option.get("steps"),
            "index": index,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return f"verified-route-option-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _refresh_overlap_evidence_fingerprint(evidence: dict[str, Any]) -> None:
        evidence["evidenceFingerprint"] = DailyRouteOverlapService.evidence_fingerprint(evidence)

    @classmethod
    def _best_same_start_geometry_meters(cls, combination: tuple[POI, ...]) -> float:
        if len(combination) < 2:
            return 0.0
        start, remaining = combination[0], combination[1:]
        return min(
            cls._geometry_cost((start, *permutation)) * 1000 for permutation in itertools.permutations(remaining)
        )

    @staticmethod
    def _canonical_candidates(candidates: Iterable[POI]) -> list[POI]:
        result: list[POI] = []
        seen: set[str] = set()
        for candidate in candidates:
            identity = str(candidate.amap_id or candidate.id or "").strip().upper()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            result.append(candidate)
        return result

    @staticmethod
    def _plan_requires_route_edge(plan: PersistableSegmentPlan) -> bool:
        """Return physical route membership without conflating density role.

        ``None`` is a narrow compatibility boundary for plans created before
        the explicit field existed. Simple Open production plans always seal a
        boolean, so a creative ``route_anchor`` can no longer add or remove a
        real materialized stop from the travel chain.
        """

        explicit = getattr(plan, "requires_route_edge", None)
        if explicit is not None:
            return explicit is True
        poi = plan.selected_poi
        if poi is None:
            return plan.route_anchor is True
        identity = str(poi.amap_id or poi.id or "").strip()
        try:
            longitude = float(poi.longitude)
            latitude = float(poi.latitude)
        except (TypeError, ValueError):
            return plan.route_anchor is True
        if not identity or not math.isfinite(longitude) or not math.isfinite(latitude):
            return plan.route_anchor is True
        if longitude == 0 or latitude == 0:
            return plan.route_anchor is True
        grounding_status = str(plan.grounding_status or "").strip().lower()
        if grounding_status in {
            "waiting_for_poi_grounding",
            "optional_waiting",
            "not_required",
            "unresolved",
            "provider_rate_limited",
        }:
            return plan.route_anchor is True
        return True

    @classmethod
    def _record_selected_topology_candidate(
        cls,
        audit: dict[str, Any],
        *,
        day_number: int,
        day_plans: list[PersistableSegmentPlan],
        geometry_meters: float,
        selected: tuple[POI, ...],
        topology_evidence: dict[str, Any],
    ) -> None:
        audit["topologyEvidence"]["perDay"].append(copy.deepcopy(topology_evidence))
        audit["selectedCombination"].append(
            {
                "dayNumber": day_number,
                "planningSlotIds": [str(item.planning_slot_id or "") for item in day_plans],
                "amapIds": [str(item.amap_id or item.id) for item in selected],
                "orderedGeometryMeters": round(geometry_meters, 2),
            }
        )
        audit["expectedPairs"].extend(
            cls._route_pair_identity(
                day_number=day_number,
                pair_ordinal=pair_ordinal,
                left_plan=left_plan,
                right_plan=right_plan,
                left_poi=left,
                right_poi=right,
            )
            for pair_ordinal, (left_plan, right_plan, left, right) in enumerate(
                zip(day_plans, day_plans[1:], selected, selected[1:]),
                start=1,
            )
        )

    @staticmethod
    def _route_segment_identity(
        plan: PersistableSegmentPlan,
        *,
        day_number: int,
        stop_ordinal: int,
    ) -> str:
        """Return the stable proposal-local identity persisted in segment metadata."""

        for value in (plan.planning_slot_id, plan.occurrence_id):
            identity = str(value or "").strip()
            if identity:
                return identity
        return f"simple-open-route-stop:day:{day_number}:ordinal:{stop_ordinal}"

    @classmethod
    def _route_pair_identity(
        cls,
        *,
        day_number: int,
        pair_ordinal: int,
        left_plan: PersistableSegmentPlan,
        right_plan: PersistableSegmentPlan,
        left_poi: POI,
        right_poi: POI,
    ) -> dict[str, Any]:
        return {
            "dayNumber": int(day_number),
            "pairOrdinal": int(pair_ordinal),
            "fromSegmentId": cls._route_segment_identity(
                left_plan,
                day_number=day_number,
                stop_ordinal=pair_ordinal,
            ),
            "toSegmentId": cls._route_segment_identity(
                right_plan,
                day_number=day_number,
                stop_ordinal=pair_ordinal + 1,
            ),
            "fromAmapId": str(left_poi.amap_id or left_poi.id or "").strip().upper(),
            "toAmapId": str(right_poi.amap_id or right_poi.id or "").strip().upper(),
        }

    @staticmethod
    def _is_exact_entity_plan(plan: PersistableSegmentPlan) -> bool:
        constraints = plan.schedule_constraints or {}
        return bool(
            str(constraints.get("entityBindingMode") or "") == "exact_entity"
            or constraints.get("replaceablePoi") is False
        )

    @staticmethod
    def _combination_signature(combination: tuple[POI, ...]) -> tuple[str, ...]:
        return tuple(str(item.amap_id or item.id or "").strip().upper() for item in combination)

    @staticmethod
    def _route_pair_summary(legs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "dayNumber": leg.get("dayNumber"),
                "pairOrdinal": leg.get("pairOrdinal"),
                "fromSegmentId": str(leg.get("fromSegmentId") or ""),
                "toSegmentId": str(leg.get("toSegmentId") or ""),
                "fromAmapId": str(leg.get("fromAmapId") or ""),
                "toAmapId": str(leg.get("toAmapId") or ""),
                "provider": str(leg.get("provider") or leg.get("source") or ""),
            }
            for leg in legs
        ]

    @classmethod
    def _verified_route_pair_summary(
        cls,
        legs: Iterable[dict[str, Any]],
        transport_mode: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for leg in legs:
            duration = cls._route_duration_seconds(leg)
            distance = cls._route_distance_meters(leg)
            if duration is None or distance is None:
                continue
            result.append(
                {
                    "dayNumber": int(leg.get("dayNumber") or 0),
                    "pairOrdinal": int(leg.get("pairOrdinal") or 0),
                    "fromSegmentId": str(leg.get("fromSegmentId") or "").strip(),
                    "toSegmentId": str(leg.get("toSegmentId") or "").strip(),
                    "fromAmapId": str(leg.get("fromAmapId") or "").strip().upper(),
                    "toAmapId": str(leg.get("toAmapId") or "").strip().upper(),
                    # Freeze the same canonical transport identity that the
                    # proposal verifier and commit gate use.  Hashing the
                    # caller alias (for example ``public_transit``) and later
                    # verifying ``transit`` makes an untampered Provider leg
                    # fail its own evidence fingerprint.
                    "transportMode": cls._canonical_transport_mode(transport_mode),
                    "durationSeconds": duration,
                    "distanceMeters": distance,
                    "provider": str(leg.get("provider") or leg.get("source") or ""),
                    "queriedAt": str(leg.get("queriedAt") or ""),
                }
            )
            result[-1]["providerEvidenceFingerprint"] = cls._provider_evidence_fingerprint(result[-1])
        return result

    @staticmethod
    def _canonical_transport_mode(value: Any) -> str:
        normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        return {
            "public_transit": "transit",
            "public_transport": "transit",
            "bus_subway": "transit",
            "subway": "transit",
            "metro": "transit",
        }.get(normalized, normalized)

    @staticmethod
    def _provider_evidence_fingerprint(value: dict[str, Any]) -> str:
        return DailyRouteOverlapService.provider_evidence_fingerprint(value)

    @staticmethod
    def _plan_order_key(
        plan: PersistableSegmentPlan,
        *,
        controller_sequence_authoritative: bool = False,
    ) -> tuple[int, int, str]:
        return SimpleOpenDynamicScheduleService.semantic_order_key(
            plan.schedule_preference,
            plan.start_time,
            tie_breaker=str(plan.planning_slot_id or plan.occurrence_id or ""),
            controller_sequence_authoritative=controller_sequence_authoritative,
            explicit_start_time=(plan.schedule_constraints or {}).get("explicitStartTime"),
        )

    @classmethod
    def _geometry_cost(cls, combination: tuple[POI, ...]) -> float:
        return sum(cls._haversine_km(left, right) for left, right in zip(combination, combination[1:]))

    @staticmethod
    def _haversine_km(left: POI, right: POI) -> float:
        if None in (left.latitude, left.longitude, right.latitude, right.longitude):
            return math.inf
        radius = 6371.0088
        lat1, lat2 = math.radians(float(left.latitude)), math.radians(float(right.latitude))
        dlat = lat2 - lat1
        dlon = math.radians(float(right.longitude) - float(left.longitude))
        value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))

    @staticmethod
    def provider_poi_payload(poi: POI) -> dict[str, Any]:
        identity = str(poi.amap_id or poi.id or "")
        return {
            "id": str(poi.id or poi.amap_id or ""),
            # ProviderRouteInsertionService uses segment identity to construct
            # its transient two-point route matrix.  This is proposal-local
            # evidence identity, not a persisted itinerary segment id.
            "segmentId": f"proposal-route-anchor:{identity}",
            "amapId": identity,
            "name": str(poi.name or ""),
            "address": str(poi.address or ""),
            "city": str(poi.city or ""),
            "latitude": poi.latitude,
            "longitude": poi.longitude,
            "source": str(poi.source or ""),
        }

    @staticmethod
    def _route_duration_seconds(leg: dict[str, Any]) -> float | None:
        for key in ("durationSeconds", "duration_seconds", "duration"):
            try:
                value = float(leg.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _route_distance_meters(leg: dict[str, Any]) -> float | None:
        for key in ("distanceMeters", "distance_meters", "distance"):
            try:
                value = float(leg.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

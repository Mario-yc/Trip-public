"""Small deterministic optimizer for a shared, already-grounded candidate universe.

It intentionally has no provider and no database dependency.  Callers must pass
only candidates which have already cleared the existing AMap semantic policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import asin, cos, radians, sin, sqrt
from typing import Any

from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


@dataclass(frozen=True)
class PortfolioRuntimeLimits:
    beam_width: int = 6
    # P0 presents a portfolio, not a per-brief search tree.  One structurally
    # distinct assignment per brief keeps the persisted proposal cap bounded.
    max_candidates_per_brief: int = 1


@dataclass(frozen=True)
class PortfolioAssignment:
    brief_id: str
    required: dict[str, dict[str, Any]]
    soft: dict[str, dict[str, Any]]
    optional: list[dict[str, Any]]
    estimated_cost_cny: float
    route_score: float


class BoundedPortfolioOptimizer:
    """Enumerate only bounded combinations; no solution can omit a hard pool."""

    def solve(
        self,
        *,
        brief_ids: list[str],
        required_pools: dict[str, list[dict[str, Any]]],
        soft_pools: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
        optional_pools: dict[str, Any],
        limits: PortfolioRuntimeLimits = PortfolioRuntimeLimits(),
    ) -> list[PortfolioAssignment]:
        if not brief_ids or len(brief_ids) > 4:
            raise ValueError("portfolio_brief_count_out_of_bounds")
        if limits.beam_width < 1 or limits.max_candidates_per_brief < 1:
            raise ValueError("portfolio_runtime_limits_invalid")
        ordered_required = sorted(required_pools)
        if any(not required_pools[key] for key in ordered_required):
            return []
        bounded = [required_pools[key][: limits.beam_width] for key in ordered_required]
        combinations = [
            {key: dict(value) for key, value in zip(ordered_required, selection)} for selection in product(*bounded)
        ]
        # One physical place cannot satisfy two separate required slots.  This
        # matters when two named goals share an intent type (or an upstream
        # caller expands a cardinality requirement into slots).
        valid_combinations: list[dict[str, dict[str, Any]]] = []
        for required in combinations:
            identities = [self._physical_identity(value) for value in required.values()]
            if all(identities) and len(set(identities)) == len(identities):
                valid_combinations.append(required)
        combinations = valid_combinations
        # A title or creative axis cannot make two identical itineraries into
        # separate proposals. Assign a different grounded combination to each
        # brief and stop when the candidate universe is exhausted.
        combinations.sort(key=lambda required: self._required_sort_key(required))
        unique: list[dict[str, dict[str, Any]]] = []
        seen: set[tuple[str, ...]] = set()
        for required in combinations:
            identity = tuple(self._physical_identity(required[key]) for key in ordered_required)
            if identity not in seen:
                seen.add(identity)
                unique.append(required)
        if not unique:
            return []
        chosen: list[PortfolioAssignment] = []
        for index, brief_id in enumerate(brief_ids):
            if len(chosen) >= limits.beam_width:
                break
            # Required coverage is non-negotiable and may be shared by briefs.
            # Distinctness is earned by brief-local optional experience/placement,
            # not by gratuitously rotating mandatory POIs.
            required = unique[index % len(unique)]
            soft = self._soft_for(
                (soft_pools or {}).get(brief_id, {}),
                required,
                limits,
            )
            pools = optional_pools.get(brief_id, {})
            if isinstance(pools, list) or (
                not pools and any(isinstance(value, list) for value in optional_pools.values())
            ):  # P0.0 compatibility: one global optional pool map.
                pools = optional_pools
            optional = self._optional_for(
                brief_id,
                pools,
                required,
                limits,
                reserved_keys={key for value in soft.values() for key in self._reservation_keys(value)},
            )
            route_score = self._route_score(required, [*soft.values(), *optional])
            cost = sum(
                float(item.get("estimatedCost") or 0) for item in [*required.values(), *soft.values(), *optional]
            )
            chosen.append(PortfolioAssignment(brief_id, required, soft, optional, cost, route_score))
        return chosen

    @staticmethod
    def _soft_for(
        pools: dict[str, list[dict[str, Any]]],
        required: dict[str, dict[str, Any]],
        limits: PortfolioRuntimeLimits,
    ) -> dict[str, dict[str, Any]]:
        used_keys = {key for value in required.values() for key in BoundedPortfolioOptimizer._reservation_keys(value)}
        result: dict[str, dict[str, Any]] = {}
        for goal_id in sorted(pools):
            candidates = list(pools[goal_id])
            if any("consumerEvidenceScore" in item for item in candidates):
                candidates = sorted(
                    candidates,
                    key=lambda item: BoundedPortfolioOptimizer._contextual_candidate_quality_key(
                        item,
                        required,
                    ),
                )
            candidates = candidates[: limits.beam_width]
            for candidate in candidates:
                candidate_id = BoundedPortfolioOptimizer._physical_identity(candidate)
                reservation_keys = BoundedPortfolioOptimizer._reservation_keys(candidate)
                if candidate_id and not (reservation_keys & used_keys):
                    result[goal_id] = dict(candidate)
                    used_keys.update(reservation_keys)
                    break
        return result

    @staticmethod
    def _required_sort_key(required: dict[str, dict[str, Any]]) -> tuple[float, float, tuple[str, ...]]:
        route_score = BoundedPortfolioOptimizer._route_score(required, [])
        cost = sum(float(item.get("estimatedCost") or 0) for item in required.values())
        identities = tuple(BoundedPortfolioOptimizer._physical_identity(item) for item in required.values())
        return (-route_score, cost, identities)

    @staticmethod
    def _optional_for(
        brief_id: str,
        pools: dict[str, Any],
        required: dict[str, dict[str, Any]],
        limits: PortfolioRuntimeLimits,
        *,
        reserved_keys: set[tuple[str, str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        required_keys = {
            key for value in required.values() for key in BoundedPortfolioOptimizer._reservation_keys(value)
        }
        used_keys = set(required_keys) | set(reserved_keys or set())
        result: list[dict[str, Any]] = []
        for key in sorted(pools):
            values = pools[key] if isinstance(pools[key], list) else []
            ordered = values[: limits.beam_width]
            if any("consumerEvidenceScore" in item for item in ordered):
                ordered = sorted(
                    ordered,
                    key=BoundedPortfolioOptimizer._candidate_quality_key,
                )
            elif ordered:
                offset = sum(ord(char) for char in key) % len(ordered)
                ordered = ordered[offset:] + ordered[:offset]
            for candidate in ordered:
                candidate_id = BoundedPortfolioOptimizer._physical_identity(candidate)
                reservation_keys = BoundedPortfolioOptimizer._reservation_keys(candidate)
                allowed = candidate.get("briefIds")
                if (
                    candidate_id
                    and not (reservation_keys & used_keys)
                    and (not isinstance(allowed, list) or brief_id in allowed)
                ):
                    result.append(dict(candidate))
                    used_keys.update(reservation_keys)
                    break
        return result[:2]

    @staticmethod
    def _route_score(required: dict[str, dict[str, Any]], optional: list[dict[str, Any]]) -> float:
        values = [*required.values(), *optional]
        return round(
            sum(
                float(item.get("localRouteScore") or 0) + 2.0 * float(item.get("consumerEvidenceScore") or 0)
                for item in values
            ),
            4,
        )

    @staticmethod
    def _candidate_quality_key(item: dict[str, Any]) -> tuple[float, float, str]:
        return (
            -float(item.get("consumerEvidenceScore") or 0),
            -float(item.get("localRouteScore") or 0),
            str(item.get("amapId") or item.get("id") or ""),
        )

    @staticmethod
    def _contextual_candidate_quality_key(
        item: dict[str, Any],
        required: dict[str, dict[str, Any]],
    ) -> tuple[int, float, float, float, str]:
        """Prefer admitted soft candidates that fit the selected same-day route.

        Consumer admission has already decided which candidates are truthful and
        eligible.  This key only orders those eligible candidates.  A straight-line
        insertion estimate is deliberately used as a cheap pre-filter; the provider
        route matrix remains the final feasibility authority.
        """
        day_number = int(item.get("dayNumber") or 0)
        point = BoundedPortfolioOptimizer._coordinates(item)
        anchors = [
            anchor
            for anchor in required.values()
            if int(anchor.get("dayNumber") or 0) == day_number
            and BoundedPortfolioOptimizer._coordinates(anchor) is not None
        ]
        if point is None or not day_number or not anchors:
            evidence, route, identity = BoundedPortfolioOptimizer._candidate_quality_key(item)
            return (1, float("inf"), evidence, route, identity)

        candidate_time = BoundedPortfolioOptimizer._time_minutes(item)
        ordered = sorted(
            anchors,
            key=lambda anchor: (
                BoundedPortfolioOptimizer._time_minutes(anchor) is None,
                BoundedPortfolioOptimizer._time_minutes(anchor) or 0,
            ),
        )
        before = [
            anchor
            for anchor in ordered
            if candidate_time is not None
            and BoundedPortfolioOptimizer._time_minutes(anchor) is not None
            and BoundedPortfolioOptimizer._time_minutes(anchor) <= candidate_time
        ]
        after = [
            anchor
            for anchor in ordered
            if candidate_time is not None
            and BoundedPortfolioOptimizer._time_minutes(anchor) is not None
            and BoundedPortfolioOptimizer._time_minutes(anchor) >= candidate_time
        ]
        if before and after and before[-1] is not after[0]:
            insertion_km = BoundedPortfolioOptimizer._insertion_distance_km(
                BoundedPortfolioOptimizer._coordinates(before[-1]),
                point,
                BoundedPortfolioOptimizer._coordinates(after[0]),
            )
        elif len(ordered) >= 2:
            insertion_km = min(
                BoundedPortfolioOptimizer._insertion_distance_km(
                    BoundedPortfolioOptimizer._coordinates(left),
                    point,
                    BoundedPortfolioOptimizer._coordinates(right),
                )
                for left, right in zip(ordered, ordered[1:])
            )
        else:
            insertion_km = BoundedPortfolioOptimizer._distance_km(
                point,
                BoundedPortfolioOptimizer._coordinates(ordered[0]),
            )
        evidence, route, identity = BoundedPortfolioOptimizer._candidate_quality_key(item)
        return (0, round(insertion_km, 6), evidence, route, identity)

    @staticmethod
    def _coordinates(item: dict[str, Any]) -> tuple[float, float] | None:
        try:
            longitude = float(item.get("longitude"))
            latitude = float(item.get("latitude"))
        except (TypeError, ValueError):
            return None
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            return None
        if longitude == 0 and latitude == 0:
            return None
        return longitude, latitude

    @staticmethod
    def _time_minutes(item: dict[str, Any]) -> int | None:
        raw = str(item.get("startTime") or item.get("time") or "").strip()
        if len(raw) < 5 or raw[2] != ":":
            return None
        try:
            hour, minute = int(raw[:2]), int(raw[3:5])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    @staticmethod
    def _insertion_distance_km(
        left: tuple[float, float] | None,
        middle: tuple[float, float],
        right: tuple[float, float] | None,
    ) -> float:
        if left is None or right is None:
            return float("inf")
        return max(
            BoundedPortfolioOptimizer._distance_km(left, middle)
            + BoundedPortfolioOptimizer._distance_km(middle, right)
            - BoundedPortfolioOptimizer._distance_km(left, right),
            0.0,
        )

    @staticmethod
    def _distance_km(left: tuple[float, float], right: tuple[float, float]) -> float:
        lon1, lat1 = map(radians, left)
        lon2, lat2 = map(radians, right)
        delta_lon = lon2 - lon1
        delta_lat = lat2 - lat1
        value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        return 6371.0088 * 2 * asin(sqrt(min(max(value, 0.0), 1.0)))

    @staticmethod
    def _identity_key(item: PortfolioAssignment) -> tuple[str, ...]:
        return tuple(BoundedPortfolioOptimizer._physical_identity(value) for value in item.required.values())

    @staticmethod
    def _physical_identity(value: dict[str, Any]) -> str:
        return PoiPhysicalIdentityService.canonical_amap_id(value)

    @staticmethod
    def _reservation_keys(value: dict[str, Any]) -> set[tuple[str, str, str]]:
        """Reserve same-day identity and any authoritative occurrence group."""
        identity = BoundedPortfolioOptimizer._physical_identity(value)
        if not identity:
            return set()
        keys = {("day", str(int(value.get("dayNumber") or 0)), identity)}
        distinct_group_id = str(value.get("distinctGroupId") or "")
        if distinct_group_id:
            keys.add(("distinct", distinct_group_id, identity))
        return keys

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class RouteInsertionScore:
    total_distance_km: float
    added_distance_km: float
    estimated_duration_minutes: int
    added_duration_minutes: int
    detour_level: str
    reason: str
    network_verified: bool = False
    generalized_cost_delta: Optional[float] = None
    detour_ratio: Optional[float] = None
    detour_tolerance: Optional[dict[str, float]] = None
    mobility_profile: Optional[dict[str, float | str]] = None


class RouteInsertionScorer:
    """Score insertion cost from route evidence.

    ``score`` deliberately remains a cheap geometry precheck for discovery
    ordering only.  It must never reject a candidate: road topology, transit
    transfers, operating windows and day slack are unknown at that point.
    ``score_from_route_matrix`` is the only method that emits a decisive
    detour level and is intended for the provider-backed feasibility/writer
    path.
    """

    COARSE_DISTANCE_BANDS_KM = (2.5, 5.0)

    def score(
        self,
        previous_anchor: Optional[Any],
        candidate: Any,
        next_anchor: Optional[Any],
        *,
        transport_mode: str = "",
    ) -> Optional[RouteInsertionScore]:
        candidate_coord = self._coord(candidate)
        if candidate_coord is None:
            return None
        previous_coord = self._coord(previous_anchor)
        next_coord = self._coord(next_anchor)
        if previous_coord is None and next_coord is None:
            return None

        baseline_km = 0.0
        inserted_km = 0.0
        reason = "single_anchor"
        if previous_coord is not None and next_coord is not None:
            baseline_km = self._haversine_km(*previous_coord, *next_coord)
            inserted_km = self._haversine_km(*previous_coord, *candidate_coord) + self._haversine_km(
                *candidate_coord, *next_coord
            )
            reason = "between_previous_and_next"
        elif previous_coord is not None:
            inserted_km = self._haversine_km(*previous_coord, *candidate_coord)
            reason = "near_previous"
        elif next_coord is not None:
            inserted_km = self._haversine_km(*candidate_coord, *next_coord)
            reason = "near_next"

        added_km = max(0.0, inserted_km - baseline_km)
        # Geometry is useful for ordering a bounded candidate batch, but is
        # deliberately capped at a non-decisive "high" band.  Only
        # ``score_from_route_matrix`` may return ``unacceptable``.
        if added_km <= self.COARSE_DISTANCE_BANDS_KM[0]:
            level = "low"
        elif added_km <= self.COARSE_DISTANCE_BANDS_KM[1]:
            level = "medium"
        else:
            level = "high"
        minutes_per_km = 5.0 if self._is_transit(transport_mode) else 4.0
        estimated_duration = int(round(inserted_km * minutes_per_km))
        added_duration = int(round(added_km * minutes_per_km))
        return RouteInsertionScore(
            total_distance_km=round(inserted_km, 2),
            added_distance_km=round(added_km, 2),
            estimated_duration_minutes=estimated_duration,
            added_duration_minutes=added_duration,
            detour_level=level,
            reason=f"geometry_precheck:{reason}",
        )

    def score_from_route_matrix(
        self,
        *,
        previous_to_candidate: Optional[dict[str, Any]],
        candidate_to_next: Optional[dict[str, Any]],
        previous_to_next: Optional[dict[str, Any]],
        detour_tolerance: Optional[dict[str, float]] = None,
        schedule_slack_minutes: Optional[float] = None,
        time_window_feasible: Optional[bool] = None,
        mobility_profile: Optional[dict[str, Any]] = None,
    ) -> Optional[RouteInsertionScore]:
        """Calculate ΔG from provider-returned route legs.

        Each leg is expected to expose provider distance/duration and may add
        walking, transfer, wait and risk penalties.  Missing/invalid provider
        evidence returns ``None`` so callers fail closed instead of replacing
        it with a straight-line decision.
        """
        if not isinstance(time_window_feasible, bool):
            return None
        profile = self._normalized_mobility_profile(mobility_profile)
        tolerance = self._normalized_detour_tolerance(detour_tolerance)
        if profile is None or tolerance is None:
            return None
        incoming = self._generalized_cost(previous_to_candidate, profile)
        outgoing = self._generalized_cost(candidate_to_next, profile)
        baseline = self._generalized_cost(previous_to_next, profile)
        two_sided = previous_to_candidate is not None and candidate_to_next is not None
        if two_sided and (incoming is None or outgoing is None or baseline is None):
            return None
        if not two_sided and (incoming is None) == (outgoing is None):
            return None
        # A one-sided insertion has no direct route to subtract, but is still
        # based on its real Provider leg rather than a geometric estimate.
        baseline_cost = baseline[0] if baseline is not None else 0.0
        available_legs = [leg for leg in (incoming, outgoing) if leg is not None]
        total_cost = sum(leg[0] for leg in available_legs)
        # Preserve the signed replacement delta: a candidate can reduce the
        # network burden and must rank ahead of a merely acceptable one.  A
        # terminal insertion has no baseline, so its full Provider cost is the
        # incremental cost.
        delta = total_cost - baseline_cost if baseline is not None else total_cost
        # A terminal one-sided insertion has no bypass baseline.  Its actual
        # Provider cost is still bounded by max delta and schedule slack; a
        # fabricated ratio against 1 minute would reject every valid endpoint.
        ratio = delta / baseline_cost if baseline_cost > 0 else None
        max_delta = tolerance["maxGeneralizedCostDelta"]
        max_ratio = tolerance["maxDetourRatio"]
        if schedule_slack_minutes is None:
            slack = None
        else:
            try:
                slack = float(schedule_slack_minutes)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(slack) or slack < 0:
                return None
        unacceptable = (
            not time_window_feasible
            or delta > max_delta
            or (ratio is not None and ratio > max_ratio)
            or (slack is not None and delta > max(0.0, slack))
        )
        ratio_for_band = ratio if ratio is not None else 0.0
        if unacceptable:
            level = "unacceptable"
        elif delta <= max_delta * 0.35 and ratio_for_band <= max_ratio * 0.35:
            level = "low"
        elif delta <= max_delta * 0.7 and ratio_for_band <= max_ratio * 0.7:
            level = "medium"
        else:
            level = "high"
        total_distance_m = sum(leg[1] for leg in available_legs)
        baseline_distance_m = baseline[1] if baseline is not None else 0.0
        total_duration_m = sum(leg[2] for leg in available_legs)
        baseline_duration_m = baseline[2] if baseline is not None else 0.0
        return RouteInsertionScore(
            total_distance_km=round(total_distance_m / 1000, 2),
            added_distance_km=round(max(0.0, total_distance_m - baseline_distance_m) / 1000, 2),
            estimated_duration_minutes=int(round(total_duration_m)),
            added_duration_minutes=int(round(max(0.0, total_duration_m - baseline_duration_m))),
            detour_level=level,
            reason="provider_route_matrix",
            network_verified=True,
            generalized_cost_delta=round(delta, 2),
            detour_ratio=round(ratio, 4) if ratio is not None else None,
            detour_tolerance=tolerance,
            mobility_profile=profile,
        )

    @classmethod
    def _generalized_cost(
        cls,
        leg: Optional[dict[str, Any]],
        mobility_profile: dict[str, float | str],
    ) -> Optional[tuple[float, float, float]]:
        if not isinstance(leg, dict):
            return None
        required_cost_fields = (
            "walkingDistanceMeters",
            "transferCount",
            "waitSeconds",
            "riskPenaltyMinutes",
        )
        if any(field not in leg and cls._snake_case(field) not in leg for field in required_cost_fields):
            return None
        try:
            duration = float(leg.get("durationSeconds") or leg.get("duration_seconds") or 0) / 60
            distance = float(leg.get("distanceMeters") or leg.get("distance_meters") or 0)
            walking = float(leg.get("walkingDistanceMeters") or leg.get("walking_distance_meters") or 0) / 1000
            transfers = float(leg.get("transferCount") or leg.get("transfer_count") or 0)
            wait = float(leg.get("waitSeconds") or leg.get("wait_seconds") or 0) / 60
            risk = float(leg.get("riskPenaltyMinutes") or leg.get("risk_penalty_minutes") or 0)
        except (TypeError, ValueError):
            return None
        values = (duration, distance, walking, transfers, wait, risk)
        if not all(math.isfinite(value) for value in values):
            return None
        if duration <= 0 or distance <= 0 or any(value < 0 for value in (walking, transfers, wait, risk)):
            return None
        return (
            duration
            + walking * float(mobility_profile["walkingPenaltyMinutesPerKm"])
            + transfers * float(mobility_profile["transferPenaltyMinutes"])
            + wait * float(mobility_profile["waitTimeMultiplier"])
            + risk * float(mobility_profile["riskPenaltyMultiplier"]),
            distance,
            duration,
        )

    @classmethod
    def _normalized_detour_tolerance(
        cls,
        value: Optional[dict[str, float]],
    ) -> Optional[dict[str, float]]:
        # There is deliberately no policy fallback here.  A final route
        # decision without an explicit user/request/plan tolerance has no
        # authority to choose a candidate.
        raw = value if isinstance(value, dict) and value else None
        if raw is None:
            return None
        try:
            max_delta = float(raw["maxGeneralizedCostDelta"])
            max_ratio = float(raw["maxDetourRatio"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(max_delta) or not math.isfinite(max_ratio):
            return None
        if max_delta < 0 or max_ratio < 0:
            return None
        return {
            "maxGeneralizedCostDelta": max_delta,
            "maxDetourRatio": max_ratio,
        }

    @classmethod
    def _normalized_mobility_profile(
        cls,
        value: Optional[dict[str, Any]],
    ) -> Optional[dict[str, float | str]]:
        # Likewise, never silently turn an omitted mobility contract into a
        # generic one.  Geometry can rank discovery candidates, but Provider
        # matrix acceptance must be attributable to an explicit contract.
        if not isinstance(value, dict):
            return None
        raw = value
        source = str(raw.get("source") or "").strip()
        if not source:
            return None
        result: dict[str, float | str] = {
            "source": source,
        }
        for field in (
            "walkingPenaltyMinutesPerKm",
            "transferPenaltyMinutes",
            "waitTimeMultiplier",
            "riskPenaltyMultiplier",
        ):
            try:
                number = float(raw[field])
            except (KeyError, TypeError, ValueError):
                return None
            if not math.isfinite(number) or number < 0:
                return None
            result[field] = number
        return result

    @classmethod
    def build_route_decision_contract(
        cls,
        *,
        source: str,
        provenance: dict[str, Any],
        detour_tolerance: dict[str, Any],
        mobility_profile: dict[str, Any],
        adjacent_leg_constraint: dict[str, Any] | None = None,
        topology_constraint: dict[str, Any] | None = None,
    ) -> Optional[dict[str, Any]]:
        """Create a portable, self-verifying final-route decision contract.

        Callers must opt in explicitly and retain the returned object with the
        route proof.  The fingerprint covers the source, policy, and
        provenance, preventing a later proof from swapping in a looser policy.
        """
        normalized_tolerance = cls._normalized_detour_tolerance(detour_tolerance)
        normalized_profile = cls._normalized_mobility_profile(mobility_profile)
        normalized_adjacent = cls._normalized_adjacent_leg_constraint(adjacent_leg_constraint)
        normalized_topology = cls._normalized_topology_constraint(topology_constraint)
        if (
            not str(source or "").strip()
            or not isinstance(provenance, dict)
            or not provenance
            or normalized_tolerance is None
            or normalized_profile is None
        ):
            return None
        try:
            canonical_provenance = json.loads(json.dumps(provenance, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError):
            return None
        body = {
            "source": str(source).strip(),
            "provenance": canonical_provenance,
            "detourTolerance": normalized_tolerance,
            "mobilityProfile": normalized_profile,
        }
        if adjacent_leg_constraint is not None:
            if normalized_adjacent is None:
                return None
            body["adjacentLegConstraint"] = normalized_adjacent
        if topology_constraint is not None:
            if normalized_topology is None:
                return None
            body["topologyConstraint"] = normalized_topology
        return {
            **body,
            "fingerprint": cls._route_contract_fingerprint(body),
        }

    @classmethod
    def normalized_route_decision_contract(
        cls,
        value: Any,
    ) -> Optional[dict[str, Any]]:
        """Validate a persisted route-decision contract without defaults."""
        if not isinstance(value, dict):
            return None
        source = str(value.get("source") or "").strip()
        provenance = value.get("provenance")
        fingerprint = str(value.get("fingerprint") or "").strip().lower()
        if not source or not isinstance(provenance, dict) or not provenance:
            return None
        normalized_tolerance = cls._normalized_detour_tolerance(value.get("detourTolerance"))
        normalized_profile = cls._normalized_mobility_profile(value.get("mobilityProfile"))
        if normalized_tolerance is None or normalized_profile is None:
            return None
        try:
            canonical_provenance = json.loads(json.dumps(provenance, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError):
            return None
        body = {
            "source": source,
            "provenance": canonical_provenance,
            "detourTolerance": normalized_tolerance,
            "mobilityProfile": normalized_profile,
        }
        if "adjacentLegConstraint" in value:
            normalized_adjacent = cls._normalized_adjacent_leg_constraint(value.get("adjacentLegConstraint"))
            if normalized_adjacent is None:
                return None
            body["adjacentLegConstraint"] = normalized_adjacent
        if "topologyConstraint" in value:
            normalized_topology = cls._normalized_topology_constraint(value.get("topologyConstraint"))
            if normalized_topology is None:
                return None
            body["topologyConstraint"] = normalized_topology
        expected = cls._route_contract_fingerprint(body)
        if fingerprint != expected:
            return None
        return {**body, "fingerprint": expected}

    @staticmethod
    def _normalized_adjacent_leg_constraint(value: Any) -> Optional[dict[str, float]]:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {
            "candidateSearchRadiusMeters",
            "maxProviderTravelMinutes",
        }:
            return None
        try:
            radius = float(value["candidateSearchRadiusMeters"])
            minutes = float(value["maxProviderTravelMinutes"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not math.isfinite(radius)
            or not math.isfinite(minutes)
            or not 100 <= radius <= 50000
            or not 1 <= minutes <= 480
        ):
            return None
        return {
            "candidateSearchRadiusMeters": radius,
            "maxProviderTravelMinutes": minutes,
        }

    @staticmethod
    def _normalized_topology_constraint(value: Any) -> Optional[dict[str, float]]:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"maxBacktrackRatio"}:
            return None
        try:
            ratio = float(value["maxBacktrackRatio"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            return None
        return {"maxBacktrackRatio": ratio}

    @staticmethod
    def _route_contract_fingerprint(body: dict[str, Any]) -> str:
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def route_proof_fingerprint(cls, proof: Any) -> Optional[str]:
        """Fingerprint the portable fields needed to replay one route decision.

        The aggregate proposal status and human-readable diagnostics are
        intentionally excluded.  Every raw Provider leg, endpoint, policy,
        decision result, schedule value and freshness timestamp is covered.
        """

        if not isinstance(proof, dict):
            return None
        covered_fields = (
            "basis",
            "routeMatrix",
            "candidateEndpoint",
            "baselineEndpoints",
            "routeDecisionContract",
            "routeDecisionContractSource",
            "routeDecisionContractProvenance",
            "routeDecisionContractLocation",
            "contractFingerprint",
            "detourTolerance",
            "detourToleranceFingerprint",
            "mobilityProfile",
            "mobilityProfileFingerprint",
            "specFingerprint",
            "generalizedCostDelta",
            "detourRatio",
            "detourLevel",
            "networkVerified",
            "timeWindow",
            "timeWindowFeasible",
            "scheduleSlackMinutes",
            "verifiedAt",
        )
        payload = {field: proof.get(field) for field in covered_fields}
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _snake_case(value: str) -> str:
        return "".join(("_" + char.lower()) if char.isupper() else char for char in value)

    def _coord(self, value: Optional[Any]) -> Optional[tuple[float, float]]:
        if value is None:
            return None
        try:
            latitude = float(getattr(value, "latitude", None))
            longitude = float(getattr(value, "longitude", None))
        except (TypeError, ValueError):
            return None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return latitude, longitude

    def _haversine_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius_km = 6371.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        hav = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        return 2 * radius_km * math.asin(min(1.0, math.sqrt(hav)))

    def _is_transit(self, transport_mode: str) -> bool:
        return str(transport_mode or "").strip() in {"transit", "public_transit", "公交地铁", "地铁", "公交"}

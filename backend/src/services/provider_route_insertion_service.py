"""Provider-backed route-matrix gate for final insertion decisions.

Geometry may order discovery candidates, but every final "on the way"
decision goes through this service.  A two-sided insertion requires the two
candidate legs and the direct baseline leg; missing or non-AMap evidence fails
closed before any itinerary writer runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.services.route_insertion_scorer import (
    RouteInsertionScore,
    RouteInsertionScorer,
)
from src.services.route_service import (
    AMAP_ROUTE_SOURCE,
    MAX_PROVIDER_ROUTE_ALTERNATIVES,
    ROUTE_CACHE_TTL_SECONDS,
    RouteService,
)


@dataclass(frozen=True)
class ProviderRouteInsertionResult:
    status: Literal["passed", "failed", "pending", "not_required"]
    score: RouteInsertionScore | None
    legs: dict[str, dict[str, Any]]
    failure_reason: str | None = None
    time_window_feasible: bool = True
    schedule_slack_minutes: float | None = None
    route_decision_contract: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.status in {"passed", "not_required"}

    def to_dict(self) -> dict[str, Any]:
        score = self.score
        return {
            "status": self.status,
            "failureReason": self.failure_reason,
            "networkVerified": bool(score and score.network_verified),
            "detourLevel": score.detour_level if score else None,
            "generalizedCostDelta": score.generalized_cost_delta if score else None,
            "detourRatio": score.detour_ratio if score else None,
            "detourTolerance": score.detour_tolerance if score else None,
            "addedDistanceKm": score.added_distance_km if score else None,
            "addedDurationMinutes": score.added_duration_minutes if score else None,
            "timeWindowFeasible": self.time_window_feasible,
            "scheduleSlackMinutes": self.schedule_slack_minutes,
            "legs": self.legs,
            "mobilityProfile": self._mobility_profile(),
            "routeDecisionContract": self.route_decision_contract,
        }

    def _mobility_profile(self) -> dict[str, Any]:
        legs = list(self.legs.values())
        effective = dict(self.score.mobility_profile or {}) if self.score else {}
        return {
            **effective,
            "evidenceSource": "provider_route_matrix",
            "modes": sorted({str(item.get("mode") or "") for item in legs if str(item.get("mode") or "")}),
            "walkingDistanceMeters": int(sum(float(item.get("walkingDistanceMeters") or 0) for item in legs)),
            "transferCount": int(sum(float(item.get("transferCount") or 0) for item in legs)),
            "waitSeconds": int(sum(float(item.get("waitSeconds") or 0) for item in legs)),
        }


class ProviderRouteInsertionService:
    """Fetch and score the exact Provider legs for one insertion/replacement."""

    def __init__(
        self,
        route_service: RouteService | None = None,
        scorer: RouteInsertionScorer | None = None,
    ) -> None:
        self.route_service = route_service or RouteService()
        self.scorer = scorer or RouteInsertionScorer()

    def evaluate(
        self,
        *,
        plan_id: str,
        previous: dict[str, Any] | None,
        candidate: dict[str, Any],
        following: dict[str, Any] | None,
        baseline_candidate: dict[str, Any] | None = None,
        transport_mode: str = "transit",
        detour_tolerance: dict[str, float] | None = None,
        candidate_duration_minutes: int = 0,
        schedule_slack_minutes: float | None = None,
        time_window_feasible: bool | None = None,
        mobility_profile: dict[str, Any] | None = None,
        route_decision_contract: dict[str, Any] | None = None,
    ) -> ProviderRouteInsertionResult:
        contract = self.scorer.normalized_route_decision_contract(route_decision_contract)
        if contract is None:
            return ProviderRouteInsertionResult(
                status="failed",
                score=None,
                legs={},
                failure_reason="route_decision_contract_missing_or_invalid",
            )
        if (
            self.scorer._normalized_detour_tolerance(detour_tolerance) != contract["detourTolerance"]
            or self.scorer._normalized_mobility_profile(mobility_profile) != contract["mobilityProfile"]
        ):
            return ProviderRouteInsertionResult(
                status="failed",
                score=None,
                legs={},
                failure_reason="route_decision_contract_policy_mismatch",
                route_decision_contract=contract,
            )
        if previous is None and following is None:
            return ProviderRouteInsertionResult(
                status="not_required",
                score=None,
                legs={},
                route_decision_contract=contract,
            )
        if not self._valid_point(candidate):
            return ProviderRouteInsertionResult(
                status="failed",
                score=None,
                legs={},
                failure_reason="candidate_route_identity_invalid",
                route_decision_contract=contract,
            )
        for point in (previous, following, baseline_candidate):
            if point is not None and not self._valid_point(point):
                return ProviderRouteInsertionResult(
                    status="failed",
                    score=None,
                    legs={},
                    failure_reason="anchor_route_identity_invalid",
                    route_decision_contract=contract,
                )
        if (
            previous is not None
            and following is not None
            and int(candidate_duration_minutes or 0) <= 0
            and time_window_feasible is None
        ):
            return ProviderRouteInsertionResult(
                status="failed",
                score=None,
                legs={},
                failure_reason="candidate_duration_missing",
                time_window_feasible=False,
                route_decision_contract=contract,
            )

        legs: dict[str, dict[str, Any]] = {}
        try:
            if previous is not None:
                incoming = self._provider_leg(
                    plan_id=f"{plan_id}_incoming",
                    left=previous,
                    right=candidate,
                    transport_mode=transport_mode,
                )
                if incoming is None:
                    return self._missing(legs, "previous_to_candidate", contract)
                legs["previousToCandidate"] = incoming
            if following is not None:
                outgoing = self._provider_leg(
                    plan_id=f"{plan_id}_outgoing",
                    left=candidate,
                    right=following,
                    transport_mode=transport_mode,
                )
                if outgoing is None:
                    return self._missing(legs, "candidate_to_next", contract)
                legs["candidateToNext"] = outgoing
            if previous is not None and following is not None:
                if baseline_candidate is None:
                    baseline = self._provider_leg(
                        plan_id=f"{plan_id}_baseline",
                        left=previous,
                        right=following,
                        transport_mode=transport_mode,
                    )
                    if baseline is None:
                        return self._missing(legs, "previous_to_next", contract)
                    legs["previousToNext"] = baseline
                else:
                    baseline_incoming = self._provider_leg(
                        plan_id=f"{plan_id}_baseline_incoming",
                        left=previous,
                        right=baseline_candidate,
                        transport_mode=transport_mode,
                    )
                    if baseline_incoming is None:
                        return self._missing(legs, "previous_to_current", contract)
                    baseline_outgoing = self._provider_leg(
                        plan_id=f"{plan_id}_baseline_outgoing",
                        left=baseline_candidate,
                        right=following,
                        transport_mode=transport_mode,
                    )
                    if baseline_outgoing is None:
                        return self._missing(legs, "current_to_next", contract)
                    legs["baselinePreviousToCurrent"] = baseline_incoming
                    legs["baselineCurrentToNext"] = baseline_outgoing
                    legs["previousToNext"] = self._combine_legs(
                        baseline_incoming,
                        baseline_outgoing,
                    )
            elif baseline_candidate is not None and previous is not None:
                baseline_incoming = self._provider_leg(
                    plan_id=f"{plan_id}_baseline_incoming",
                    left=previous,
                    right=baseline_candidate,
                    transport_mode=transport_mode,
                )
                if baseline_incoming is None:
                    return self._missing(legs, "previous_to_current", contract)
                legs["baselinePreviousToCurrent"] = baseline_incoming
                legs["previousToNext"] = baseline_incoming
            elif baseline_candidate is not None and following is not None:
                baseline_outgoing = self._provider_leg(
                    plan_id=f"{plan_id}_baseline_outgoing",
                    left=baseline_candidate,
                    right=following,
                    transport_mode=transport_mode,
                )
                if baseline_outgoing is None:
                    return self._missing(legs, "current_to_next", contract)
                legs["baselineCurrentToNext"] = baseline_outgoing
                legs["previousToNext"] = baseline_outgoing
        except Exception:
            return ProviderRouteInsertionResult(
                status="pending",
                score=None,
                legs=legs,
                failure_reason="route_provider_unavailable",
                route_decision_contract=contract,
            )

        derived_feasible, derived_slack = self._schedule_context(
            previous=previous,
            candidate=candidate,
            following=following,
            legs=legs,
            candidate_duration_minutes=candidate_duration_minutes,
        )
        effective_feasible = bool(time_window_feasible) if time_window_feasible is not None else derived_feasible
        effective_slack = float(schedule_slack_minutes) if schedule_slack_minutes is not None else derived_slack
        score = self.scorer.score_from_route_matrix(
            previous_to_candidate=legs.get("previousToCandidate"),
            candidate_to_next=legs.get("candidateToNext"),
            previous_to_next=legs.get("previousToNext"),
            detour_tolerance=detour_tolerance,
            schedule_slack_minutes=effective_slack,
            time_window_feasible=effective_feasible,
            mobility_profile=mobility_profile,
        )
        if score is None or not score.network_verified:
            return ProviderRouteInsertionResult(
                status="failed",
                score=None,
                legs=legs,
                failure_reason="provider_route_matrix_incomplete",
                time_window_feasible=effective_feasible,
                schedule_slack_minutes=effective_slack,
                route_decision_contract=contract,
            )
        if score.detour_level == "unacceptable":
            return ProviderRouteInsertionResult(
                status="failed",
                score=score,
                legs=legs,
                failure_reason=(
                    "route_time_window_infeasible" if not effective_feasible else "provider_route_matrix_unacceptable"
                ),
                time_window_feasible=effective_feasible,
                schedule_slack_minutes=effective_slack,
                route_decision_contract=contract,
            )
        return ProviderRouteInsertionResult(
            status="passed",
            score=score,
            legs=legs,
            time_window_feasible=effective_feasible,
            schedule_slack_minutes=effective_slack,
            route_decision_contract=contract,
        )

    def verified_leg(
        self,
        *,
        plan_id: str,
        left: dict[str, Any],
        right: dict[str, Any],
        transport_mode: str = "transit",
    ) -> dict[str, Any] | None:
        """Return one complete, fresh Provider matrix leg or fail closed."""
        if not self._valid_point(left) or not self._valid_point(right):
            return None
        try:
            return self._provider_leg(
                plan_id=plan_id,
                left=left,
                right=right,
                transport_mode=transport_mode,
            )
        except Exception:
            return None

    def verified_leg_options(
        self,
        *,
        plan_id: str,
        left: dict[str, Any],
        right: dict[str, Any],
        transport_mode: str = "transit",
    ) -> list[dict[str, Any]]:
        """Return bounded same-response alternatives for one exact OD pair.

        This seam is explicit so legacy ``verified_leg`` continues to consume
        only the first Provider route.  RouteService owns the hard response
        cap; this adapter repeats that cap defensively and performs no retries.
        """

        if not self._valid_point(left) or not self._valid_point(right):
            return []
        try:
            return self._provider_leg_options(
                plan_id=plan_id,
                left=left,
                right=right,
                transport_mode=transport_mode,
            )
        except Exception:
            return []

    def _provider_leg(
        self,
        *,
        plan_id: str,
        left: dict[str, Any],
        right: dict[str, Any],
        transport_mode: str,
    ) -> dict[str, Any] | None:
        pois = [self._poi(left), self._poi(right)]
        segments = [
            self._segment(left, index=1, transport_mode=transport_mode),
            self._segment(right, index=2, transport_mode=transport_mode),
        ]
        try:
            routes = self.route_service.build_routes(
                plan_id,
                pois,
                transport_mode=transport_mode,
                segments=segments,
                preferred_mode_only=True,
            )
        except TypeError:
            # The compact-route contract requires a single requested mode.
            # An older adapter signature cannot prove that property, so fail
            # closed instead of retrying through a mode-expanding path.
            return None
        selected = next(
            (route for route in routes or [] if bool(getattr(route, "is_selected", False))),
            None,
        )
        selected = selected or (routes[0] if routes else None)
        if selected is None:
            return None
        return self._route_option_to_leg(selected, left=left, right=right, segments=segments)

    def _provider_leg_options(
        self,
        *,
        plan_id: str,
        left: dict[str, Any],
        right: dict[str, Any],
        transport_mode: str,
    ) -> list[dict[str, Any]]:
        pois = [self._poi(left), self._poi(right)]
        segments = [
            self._segment(left, index=1, transport_mode=transport_mode),
            self._segment(right, index=2, transport_mode=transport_mode),
        ]
        try:
            routes = self.route_service.build_routes(
                plan_id,
                pois,
                transport_mode=transport_mode,
                segments=segments,
                preferred_mode_only=True,
                include_provider_alternatives=True,
            )
        except TypeError:
            return []
        result: list[dict[str, Any]] = []
        for route in (routes or [])[:MAX_PROVIDER_ROUTE_ALTERNATIVES]:
            leg = self._route_option_to_leg(route, left=left, right=right, segments=segments)
            if leg is not None:
                result.append(leg)
        return result

    def _route_option_to_leg(
        self,
        selected: Any,
        *,
        left: dict[str, Any],
        right: dict[str, Any],
        segments: list[ItinerarySegment],
    ) -> dict[str, Any] | None:
        provider = str(getattr(selected, "provider", "") or "")
        distance = int(getattr(selected, "distance_meters", 0) or 0)
        duration = int(getattr(selected, "duration_seconds", 0) or 0)
        if provider != AMAP_ROUTE_SOURCE or distance <= 0 or duration <= 0:
            return None
        queried_at = getattr(selected, "queried_at", None)
        if not isinstance(queried_at, datetime):
            return None
        if queried_at.tzinfo is None:
            queried_at = queried_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - queried_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -60 or age_seconds > ROUTE_CACHE_TTL_SECONDS:
            return None
        payload = (
            getattr(selected, "provider_payload", None)
            if isinstance(getattr(selected, "provider_payload", None), dict)
            else {}
        )
        steps = [item for item in (getattr(selected, "steps", None) or []) if isinstance(item, dict)]
        derived_walking = sum(
            max(0.0, self._float(item.get("distance")))
            for item in steps
            if str(item.get("mode") or "").casefold() == "walking"
        )
        transit_leg_count = len(
            [item for item in steps if str(item.get("mode") or "").casefold() in {"transit", "bus", "subway", "rail"}]
        )
        explicit_walking = self._optional_number(
            payload,
            "walkingDistanceMeters",
            "walking_distance_meters",
            "walking_distance",
        )
        explicit_transfers = self._optional_number(
            payload,
            "transferCount",
            "transfer_count",
            "transfers",
        )
        explicit_wait = self._optional_number(
            payload,
            "waitSeconds",
            "wait_seconds",
        )
        risk_penalty = max(
            self._float(left.get("riskPenaltyMinutes")),
            self._float(right.get("riskPenaltyMinutes")),
        )
        return {
            "routeOptionId": str(getattr(selected, "id", "") or ""),
            "fromSegmentId": segments[0].id,
            "toSegmentId": segments[1].id,
            "fromAmapId": str(left.get("amapId") or ""),
            "toAmapId": str(right.get("amapId") or ""),
            "provider": provider,
            "source": provider,
            "mode": str(getattr(selected, "mode", "transit") or "transit"),
            "distanceMeters": distance,
            "durationSeconds": duration,
            "queriedAt": queried_at.isoformat(),
            "walkingDistanceMeters": (explicit_walking if explicit_walking is not None else derived_walking),
            "transferCount": (explicit_transfers if explicit_transfers is not None else max(0, transit_leg_count - 1)),
            # AMap integrated transit duration already includes in-network
            # waiting.  Keep an explicit zero only when there is no separate
            # wait component, and record that provenance below.
            "waitSeconds": explicit_wait if explicit_wait is not None else 0.0,
            "riskPenaltyMinutes": risk_penalty,
            "polyline": [
                list(point)
                for point in (getattr(selected, "polyline", None) or [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ],
            "steps": steps,
            "providerPayload": dict(payload),
            "costAmount": float(getattr(selected, "cost_amount", 0) or 0),
            "costCurrency": str(getattr(selected, "cost_currency", "CNY") or "CNY"),
            "costComponentProvenance": {
                "walkingDistance": ("provider_payload" if explicit_walking is not None else "normalized_route_steps"),
                "transferCount": ("provider_payload" if explicit_transfers is not None else "normalized_route_steps"),
                "wait": ("provider_payload" if explicit_wait is not None else "included_in_provider_duration"),
                "risk": ("candidate_or_anchor_contract" if risk_penalty > 0 else "no_explicit_risk_penalty"),
            },
        }

    @staticmethod
    def _missing(
        legs: dict[str, dict[str, Any]],
        name: str,
        route_decision_contract: dict[str, Any],
    ) -> ProviderRouteInsertionResult:
        return ProviderRouteInsertionResult(
            status="failed",
            score=None,
            legs=legs,
            failure_reason=f"provider_route_leg_missing:{name}",
            route_decision_contract=route_decision_contract,
        )

    @staticmethod
    def _combine_legs(*legs: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": AMAP_ROUTE_SOURCE,
            "source": AMAP_ROUTE_SOURCE,
            "mode": str(legs[0].get("mode") or "transit"),
            "distanceMeters": sum(float(item.get("distanceMeters") or 0) for item in legs),
            "durationSeconds": sum(float(item.get("durationSeconds") or 0) for item in legs),
            "walkingDistanceMeters": sum(float(item.get("walkingDistanceMeters") or 0) for item in legs),
            "transferCount": sum(float(item.get("transferCount") or 0) for item in legs),
            "waitSeconds": sum(float(item.get("waitSeconds") or 0) for item in legs),
            "riskPenaltyMinutes": sum(float(item.get("riskPenaltyMinutes") or 0) for item in legs),
            "composite": True,
            "componentCount": len(legs),
        }

    @staticmethod
    def _valid_point(point: dict[str, Any]) -> bool:
        if not isinstance(point, dict):
            return False
        try:
            longitude = float(point.get("longitude"))
            latitude = float(point.get("latitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            str(point.get("segmentId") or "")
            and str(point.get("amapId") or "")
            and str(point.get("name") or "")
            and str(point.get("source") or "") == "amap-place-search"
            and math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
        )

    @staticmethod
    def _poi(point: dict[str, Any]) -> POI:
        return POI(
            id=str(point.get("amapId") or ""),
            amap_id=str(point.get("amapId") or ""),
            name=str(point.get("name") or ""),
            city=str(point.get("city") or ""),
            category=str(point.get("category") or ""),
            type=str(point.get("type") or "route_anchor"),
            longitude=float(point.get("longitude")),
            latitude=float(point.get("latitude")),
            source="amap-place-search",
            confidence=1.0,
        )

    @staticmethod
    def _segment(
        point: dict[str, Any],
        *,
        index: int,
        transport_mode: str,
    ) -> ItinerarySegment:
        return ItinerarySegment(
            id=str(point.get("segmentId") or ""),
            day_id=str(point.get("dayId") or "route_matrix_day"),
            segment_order=index,
            kind="visit",
            start_time=str(point.get("startTime") or "09:00"),
            end_time=str(point.get("endTime") or "10:00"),
            poi_id=str(point.get("amapId") or ""),
            transport_mode=transport_mode,
            estimated_cost=0,
            notes="provider route insertion preflight",
        )

    @classmethod
    def _schedule_context(
        cls,
        *,
        previous: dict[str, Any] | None,
        candidate: dict[str, Any],
        following: dict[str, Any] | None,
        legs: dict[str, dict[str, Any]],
        candidate_duration_minutes: int,
    ) -> tuple[bool, float | None]:
        if previous is None and following is None:
            return True, None
        if previous is not None and following is None:
            previous_end = cls._minutes(previous.get("endTime"))
            candidate_start = cls._minutes(candidate.get("startTime"))
            try:
                duration = float(legs.get("previousToCandidate", {}).get("durationSeconds") or 0) / 60
            except (TypeError, ValueError):
                return False, None
            if previous_end is None or candidate_start is None or not math.isfinite(duration) or duration <= 0:
                return False, None
            slack = candidate_start - previous_end - duration
            return slack >= 0, max(0.0, slack)
        if previous is None and following is not None:
            candidate_end = cls._minutes(candidate.get("endTime"))
            following_start = cls._minutes(following.get("startTime"))
            try:
                duration = float(legs.get("candidateToNext", {}).get("durationSeconds") or 0) / 60
            except (TypeError, ValueError):
                return False, None
            if candidate_end is None or following_start is None or not math.isfinite(duration) or duration <= 0:
                return False, None
            slack = following_start - candidate_end - duration
            return slack >= 0, max(0.0, slack)
        previous_end = cls._minutes(previous.get("endTime"))
        next_start = cls._minutes(following.get("startTime"))
        if previous_end is None or next_start is None:
            return False, None
        window = next_start - previous_end
        incoming = float(legs.get("previousToCandidate", {}).get("durationSeconds") or 0) / 60
        outgoing = float(legs.get("candidateToNext", {}).get("durationSeconds") or 0) / 60
        baseline = float(legs.get("previousToNext", {}).get("durationSeconds") or 0) / 60
        if not all(math.isfinite(value) and value > 0 for value in (incoming, outgoing, baseline)):
            return False, None
        duration = max(0, int(candidate_duration_minutes or 0))
        feasible = window >= duration + incoming + outgoing
        slack = max(0.0, window - duration - baseline)
        return feasible, slack

    @staticmethod
    def _minutes(value: Any) -> int | None:
        try:
            hour, minute = (int(item) for item in str(value).split(":", 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    @staticmethod
    def _optional_number(
        payload: dict[str, Any],
        *keys: str,
    ) -> float | None:
        for key in keys:
            try:
                value = float(payload.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                return value
        return None

    @staticmethod
    def _float(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, number) if math.isfinite(number) else 0.0

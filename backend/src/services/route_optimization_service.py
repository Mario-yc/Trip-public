import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


SIMILAR_DISTANCE_RATIO = 1.15
SIMILAR_DISTANCE_ABSOLUTE_METERS = 1200
FASTEST_DISTANCE_RATIO = 1.25
FASTEST_DISTANCE_ABSOLUTE_METERS = 2000
MIN_FASTEST_DURATION_SAVINGS_SECONDS = 5 * 60
MIN_DURATION_SAVINGS_SECONDS = 8 * 60
MIN_COST_SAVINGS = 3.0
MAX_COST_SAVING_DURATION_PENALTY_SECONDS = 10 * 60
MAX_CHEAPEST_DURATION_PENALTY_SECONDS = 15 * 60
FASTEST_COST_WARNING_DELTA = 30.0
WALKING_AUTO_MAX_DISTANCE_METERS = 2500
WALKING_AUTO_MAX_DURATION_SECONDS = 35 * 60
ROUTE_OPTIMIZATION_OBJECTIVES = {"balanced", "fastest", "cheapest"}


@dataclass
class RouteOptimizationChange:
    from_route_id: str
    to_route_id: str
    from_segment_id: Optional[str]
    to_segment_id: Optional[str]
    reasons: list[str]
    duration_delta_seconds: int
    cost_delta: float
    distance_delta_meters: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class RouteOptimizationResult:
    objective: str = "balanced"
    changed_count: int = 0
    changes: list[RouteOptimizationChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_metadata(self) -> dict:
        return {
            "objective": self.objective,
            "changedCount": self.changed_count,
            "warnings": self.warnings,
            "changes": [
                {
                    "fromRouteId": change.from_route_id,
                    "toRouteId": change.to_route_id,
                    "fromSegmentId": change.from_segment_id,
                    "toSegmentId": change.to_segment_id,
                    "reasons": change.reasons,
                    "durationDeltaSeconds": change.duration_delta_seconds,
                    "costDelta": change.cost_delta,
                    "distanceDeltaMeters": change.distance_delta_meters,
                    "warnings": change.warnings,
                }
                for change in self.changes
            ],
        }


class RouteOptimizationService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def optimize_plan_routes(
        self,
        plan_id: str,
        *,
        day_id: Optional[str] = None,
        preference: Optional[dict] = None,
        objective: str = "balanced",
    ) -> RouteOptimizationResult:
        objective = objective if objective in ROUTE_OPTIMIZATION_OBJECTIVES else "balanced"
        result = RouteOptimizationResult(objective=objective)
        for routes in self._route_groups(plan_id, day_id=day_id).values():
            selected = next((route for route in routes if bool(route["is_selected"])), None)
            candidate = self.best_route_for_leg(routes, selected, preference or {}, objective=objective)
            if selected is None or candidate is None or selected["id"] == candidate["id"]:
                continue
            self._select_route(plan_id, candidate)
            reasons = self._switch_reasons_for_objective(selected, candidate, objective)
            warnings = self._switch_warnings(selected, candidate, objective)
            change = self._change(selected, candidate, reasons=reasons, warnings=warnings)
            result.changes.append(change)
            for warning in warnings:
                if warning not in result.warnings:
                    result.warnings.append(warning)
        result.changed_count = len(result.changes)
        return result

    def best_route_for_leg(
        self,
        routes: list[sqlite3.Row],
        selected: Optional[sqlite3.Row],
        preference: dict,
        objective: str = "balanced",
    ) -> Optional[sqlite3.Row]:
        usable = [route for route in routes if self._usable_route(route)]
        if not usable:
            return None
        baseline = selected if selected is not None and self._usable_route(selected) else self._default_route(usable, preference)
        if objective == "fastest":
            return self._fastest_route(usable, baseline)
        if objective == "cheapest":
            return self._cheapest_route(usable, baseline)
        best = baseline
        best_score: tuple[int, int, float, int] = (0, 0, 0.0, 0)
        for route in usable:
            if route["id"] == baseline["id"]:
                continue
            reasons = self._switch_reasons(baseline, route)
            if not reasons:
                continue
            duration_savings = int(baseline["duration_seconds"] or 0) - int(route["duration_seconds"] or 0)
            cost_savings = float(baseline["cost_amount"] or 0) - float(route["cost_amount"] or 0)
            score = (len(reasons), duration_savings, cost_savings, -int(route["distance_meters"] or 0))
            if score > best_score:
                best = route
                best_score = score
        return best

    def _fastest_route(self, routes: list[sqlite3.Row], baseline: sqlite3.Row) -> sqlite3.Row:
        candidates = [
            route
            for route in routes
            if route["id"] != baseline["id"] and self._acceptable_fastest_route(baseline, route)
        ]
        if not candidates:
            return baseline
        return sorted(candidates, key=lambda route: (int(route["duration_seconds"] or 0), float(route["cost_amount"] or 0), int(route["distance_meters"] or 0)))[0]

    def _cheapest_route(self, routes: list[sqlite3.Row], baseline: sqlite3.Row) -> sqlite3.Row:
        candidates = [
            route
            for route in routes
            if route["id"] != baseline["id"] and self._acceptable_cheapest_route(baseline, route)
        ]
        if not candidates:
            return baseline
        return sorted(candidates, key=lambda route: (float(route["cost_amount"] or 0), int(route["duration_seconds"] or 0), int(route["distance_meters"] or 0)))[0]

    def _route_groups(self, plan_id: str, *, day_id: Optional[str]) -> dict[tuple, list[sqlite3.Row]]:
        day_filter = ""
        params: list[object] = [plan_id]
        if day_id:
            day_filter = "AND from_segment.day_id = ? AND to_segment.day_id = ?"
            params.extend([day_id, day_id])
        rows = self.db.execute(
            f"""
            SELECT route_options.*
            FROM route_options
            LEFT JOIN itinerary_segments from_segment ON from_segment.id = route_options.from_segment_id
            LEFT JOIN itinerary_segments to_segment ON to_segment.id = route_options.to_segment_id
            WHERE route_options.plan_id = ?
              AND route_options.from_segment_id IS NOT NULL
              AND route_options.to_segment_id IS NOT NULL
              {day_filter}
            ORDER BY route_options.sort_order ASC, route_options.id ASC
            """,
            tuple(params),
        ).fetchall()
        groups: dict[tuple, list[sqlite3.Row]] = {}
        for row in rows:
            key = (
                row["from_segment_id"],
                row["to_segment_id"],
                row["from_poi_id"],
                row["to_poi_id"],
            )
            groups.setdefault(key, []).append(row)
        return groups

    def _select_route(self, plan_id: str, route: sqlite3.Row) -> None:
        self.db.execute(
            """
            UPDATE route_options
            SET is_selected = 0
            WHERE plan_id = ?
              AND COALESCE(from_segment_id, '') = COALESCE(?, '')
              AND COALESCE(to_segment_id, '') = COALESCE(?, '')
              AND from_poi_id = ?
              AND to_poi_id = ?
            """,
            (plan_id, route["from_segment_id"], route["to_segment_id"], route["from_poi_id"], route["to_poi_id"]),
        )
        payload = self._provider_payload(route)
        payload["routeOptimizationSelected"] = True
        self.db.execute(
            """
            UPDATE route_options
            SET is_selected = 1, provider_payload_json = ?
            WHERE id = ? AND plan_id = ?
            """,
            (json.dumps(payload, ensure_ascii=False), route["id"], plan_id),
        )

    def _default_route(self, routes: list[sqlite3.Row], preference: dict) -> sqlite3.Row:
        preferred = str(preference.get("preferredMode") or preference.get("transportMode") or "").strip()
        return sorted(routes, key=lambda route: (self._mode_rank(str(route["mode"] or route["transport_mode"] or ""), preferred), int(route["duration_seconds"] or 0), float(route["cost_amount"] or 0)))[0]

    def _switch_reasons(self, baseline: sqlite3.Row, candidate: sqlite3.Row) -> list[str]:
        if not self._similar_distance(baseline, candidate):
            return []
        if self._walking_or_bicycling_too_long(candidate, baseline):
            return []
        reasons = ["similar_distance"]
        duration_delta = int(baseline["duration_seconds"] or 0) - int(candidate["duration_seconds"] or 0)
        cost_delta = float(baseline["cost_amount"] or 0) - float(candidate["cost_amount"] or 0)
        if duration_delta >= MIN_DURATION_SAVINGS_SECONDS:
            reasons.append("shorter_duration")
        if cost_delta >= MIN_COST_SAVINGS and -duration_delta <= MAX_COST_SAVING_DURATION_PENALTY_SECONDS:
            reasons.append("lower_cost")
        if self._risk_rank(candidate) < self._risk_rank(baseline) and duration_delta >= -MAX_COST_SAVING_DURATION_PENALTY_SECONDS and cost_delta >= -MIN_COST_SAVINGS:
            reasons.append("lower_risk")
        return reasons if len(reasons) > 1 else []

    def _switch_reasons_for_objective(self, baseline: sqlite3.Row, candidate: sqlite3.Row, objective: str) -> list[str]:
        if objective == "fastest":
            return ["objective_fastest", "shorter_duration", "acceptable_distance"]
        if objective == "cheapest":
            return ["objective_cheapest", "lower_cost", "acceptable_duration_penalty"]
        return self._switch_reasons(baseline, candidate)

    def _switch_warnings(self, baseline: sqlite3.Row, candidate: sqlite3.Row, objective: str) -> list[str]:
        warnings: list[str] = []
        if objective == "fastest":
            cost_delta = float(candidate["cost_amount"] or 0) - float(baseline["cost_amount"] or 0)
            if cost_delta > FASTEST_COST_WARNING_DELTA:
                warnings.append("faster_route_costs_more")
        return warnings

    def _acceptable_fastest_route(self, baseline: sqlite3.Row, candidate: sqlite3.Row) -> bool:
        duration_delta = int(baseline["duration_seconds"] or 0) - int(candidate["duration_seconds"] or 0)
        if duration_delta < MIN_FASTEST_DURATION_SAVINGS_SECONDS:
            return False
        if not self._fastest_distance_acceptable(baseline, candidate):
            return False
        return True

    def _acceptable_cheapest_route(self, baseline: sqlite3.Row, candidate: sqlite3.Row) -> bool:
        if self._walking_or_bicycling_too_long(candidate, baseline):
            return False
        if not self._similar_distance(baseline, candidate):
            return False
        cost_delta = float(baseline["cost_amount"] or 0) - float(candidate["cost_amount"] or 0)
        if cost_delta < MIN_COST_SAVINGS:
            return False
        duration_delta = int(candidate["duration_seconds"] or 0) - int(baseline["duration_seconds"] or 0)
        return duration_delta <= MAX_CHEAPEST_DURATION_PENALTY_SECONDS

    def _fastest_distance_acceptable(self, baseline: sqlite3.Row, candidate: sqlite3.Row) -> bool:
        baseline_distance = max(1, int(baseline["distance_meters"] or 0))
        candidate_distance = int(candidate["distance_meters"] or 0)
        return (
            candidate_distance <= baseline_distance * FASTEST_DISTANCE_RATIO
            or candidate_distance - baseline_distance <= FASTEST_DISTANCE_ABSOLUTE_METERS
        )

    def _walking_or_bicycling_too_long(self, candidate: sqlite3.Row, baseline: sqlite3.Row) -> bool:
        mode = str(candidate["mode"] or candidate["transport_mode"] or "")
        if mode not in {"walking", "walk", "bicycling", "bike", "cycling"}:
            return False
        if int(candidate["distance_meters"] or 0) <= WALKING_AUTO_MAX_DISTANCE_METERS and int(candidate["duration_seconds"] or 0) <= WALKING_AUTO_MAX_DURATION_SECONDS:
            return False
        return int(baseline["duration_seconds"] or 0) <= 60 * 60

    def _similar_distance(self, baseline: sqlite3.Row, candidate: sqlite3.Row) -> bool:
        baseline_distance = max(1, int(baseline["distance_meters"] or 0))
        candidate_distance = int(candidate["distance_meters"] or 0)
        return (
            candidate_distance <= baseline_distance * SIMILAR_DISTANCE_RATIO
            or candidate_distance - baseline_distance <= SIMILAR_DISTANCE_ABSOLUTE_METERS
        )

    def _usable_route(self, route: sqlite3.Row) -> bool:
        if route["error_json"]:
            return False
        if not route["polyline_json"] or route["polyline_json"] == "[]":
            return False
        payload = self._provider_payload(route)
        return str(payload.get("routeStatus") or "") not in {"waiting_for_poi_grounding", "route_skipped"}

    def _change(
        self,
        baseline: sqlite3.Row,
        candidate: sqlite3.Row,
        *,
        reasons: Optional[list[str]] = None,
        warnings: Optional[list[str]] = None,
    ) -> RouteOptimizationChange:
        return RouteOptimizationChange(
            from_route_id=str(baseline["id"]),
            to_route_id=str(candidate["id"]),
            from_segment_id=baseline["from_segment_id"],
            to_segment_id=baseline["to_segment_id"],
            reasons=reasons if reasons is not None else self._switch_reasons(baseline, candidate),
            duration_delta_seconds=int(candidate["duration_seconds"] or 0) - int(baseline["duration_seconds"] or 0),
            cost_delta=float(candidate["cost_amount"] or 0) - float(baseline["cost_amount"] or 0),
            distance_delta_meters=int(candidate["distance_meters"] or 0) - int(baseline["distance_meters"] or 0),
            warnings=warnings or [],
        )

    @staticmethod
    def _provider_payload(route: sqlite3.Row) -> dict:
        try:
            payload = json.loads(route["provider_payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _risk_rank(route: sqlite3.Row) -> int:
        return {"low": 0, "medium": 1, "high": 2}.get(str(route["crowding_risk"] or "").lower(), 1)

    @staticmethod
    def _mode_rank(mode: str, preferred: str) -> int:
        if not preferred:
            return 0
        normalized = {"walk": "walking", "bike": "bicycling", "cycling": "bicycling", "public_transit": "transit", "self_drive": "driving"}.get(mode, mode)
        preferred = {"walk": "walking", "bike": "bicycling", "cycling": "bicycling", "public_transit": "transit", "self_drive": "driving"}.get(preferred, preferred)
        ranks = {
            "transit": {"transit": 0, "walking": 1, "bicycling": 2, "taxi": 3, "driving": 4},
            "driving": {"driving": 0, "taxi": 1, "transit": 2, "bicycling": 3, "walking": 4},
            "taxi": {"taxi": 0, "driving": 1, "transit": 2, "bicycling": 3, "walking": 4},
            "walking": {"walking": 0, "bicycling": 1, "transit": 2, "taxi": 3, "driving": 4},
            "bicycling": {"bicycling": 0, "walking": 1, "transit": 2, "taxi": 3, "driving": 4},
        }
        return ranks.get(preferred, {}).get(normalized, 9)

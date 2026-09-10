import math

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


VERIFIED_PROVIDER_ROUTE_STATUSES = frozenset(
    {"1", "ok", "success", "succeeded", "verified", "selected", "ready"}
)
NON_VERIFIED_PROVIDER_ROUTE_STATUSES = frozenset(
    {
        "failed",
        "error",
        "provider_error",
        "unverified",
        "invalid",
        "needs_refresh",
        "provider_timeout",
        "timeout",
        "worker_error",
        "provider_failed",
        "route_skipped_deadline_budget",
        "route_skipped_provider_degraded",
        "route_skipped_provider_unavailable",
        "provider_rate_limited",
        "route_skipped_not_enough_anchors",
        "waiting_for_poi_grounding",
        "pending",
        "rejected",
        "cancelled",
        "route_pending",
        "route_verifying",
        "route_partial",
        "route_provider_failed",
    }
)


def route_evidence_status(
    *,
    provider_payload: Optional[dict[str, Any]] = None,
    error: Optional[dict[str, Any]] = None,
    explicit_status: Any = None,
) -> str:
    """Return a fail-closed canonical status for persisted route evidence."""

    if error:
        return "failed"
    payload = provider_payload if isinstance(provider_payload, dict) else {}
    candidate = explicit_status
    if candidate is None or not str(candidate).strip():
        candidate = payload.get("routeStatus") or payload.get("status")
    normalized = str(candidate or "").strip().casefold()
    if not normalized:
        return "verified"
    if normalized in VERIFIED_PROVIDER_ROUTE_STATUSES:
        return "verified"
    if normalized in NON_VERIFIED_PROVIDER_ROUTE_STATUSES:
        return normalized
    return "needs_refresh"


@dataclass(init=False)
class RouteOption:
    id: str
    plan_id: str
    from_segment_id: Optional[str]
    to_segment_id: Optional[str]
    from_poi_id: str
    to_poi_id: str
    provider: str
    mode: str
    label: str
    is_selected: bool
    sort_order: int
    distance_meters: int
    duration_seconds: int
    cost_amount: float
    cost_currency: str
    polyline: list[list[float]]
    steps: list[dict]
    provider_payload: dict
    error: Optional[dict]
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __init__(
        self,
        id: str,
        plan_id: str,
        from_poi_id: str,
        to_poi_id: str,
        distance_meters: int,
        from_segment_id: Optional[str] = None,
        to_segment_id: Optional[str] = None,
        provider: str = "amap-webservice",
        mode: Optional[str] = None,
        label: str = "",
        is_selected: bool = False,
        sort_order: int = 0,
        duration_seconds: Optional[int] = None,
        cost_amount: Optional[float] = None,
        cost_currency: str = "CNY",
        polyline: Optional[list[list[float]]] = None,
        steps: Optional[list[dict]] = None,
        provider_payload: Optional[dict] = None,
        error: Optional[dict] = None,
        queried_at: Optional[datetime] = None,
        transport_mode: Optional[str] = None,
        duration_minutes: Optional[int] = None,
        cost_estimate: Optional[float] = None,
        crowding_risk: str = "",
        source: Optional[str] = None,
    ) -> None:
        self.id = id
        self.plan_id = plan_id
        self.from_segment_id = from_segment_id
        self.to_segment_id = to_segment_id
        self.from_poi_id = from_poi_id
        self.to_poi_id = to_poi_id
        self.provider = source or provider
        self.mode = normalize_route_mode(mode or transport_mode or "walking")
        self.label = label or route_mode_label(self.mode)
        self.is_selected = is_selected
        self.sort_order = sort_order
        self.distance_meters = distance_meters
        self.duration_seconds = duration_seconds if duration_seconds is not None else int(duration_minutes or 0) * 60
        self.cost_amount = float(cost_amount if cost_amount is not None else (cost_estimate or 0.0))
        self.cost_currency = cost_currency
        self.polyline = polyline or []
        self.steps = steps or []
        self.provider_payload = provider_payload or {}
        self.error = error
        self.queried_at = queried_at or datetime.now(timezone.utc)
        self._crowding_risk = crowding_risk

    @property
    def transport_mode(self) -> str:
        return self.mode

    @property
    def duration_minutes(self) -> int:
        return max(1, math.ceil(self.duration_seconds / 60)) if self.duration_seconds else 0

    @property
    def cost_estimate(self) -> float:
        return self.cost_amount

    @property
    def source(self) -> str:
        return self.provider

    @property
    def crowding_risk(self) -> str:
        if self._crowding_risk:
            return self._crowding_risk
        if self.mode == "transit":
            return "medium"
        return "low"


def normalize_route_mode(mode: str) -> str:
    mapping = {
        "walk": "walking",
        "walking": "walking",
        "bicycling": "bicycling",
        "bike": "bicycling",
        "cycling": "bicycling",
        "public_transit": "transit",
        "transit": "transit",
        "self_drive": "driving",
        "driving": "driving",
        "taxi": "taxi",
    }
    return mapping.get(mode, mode)


def route_mode_label(mode: str) -> str:
    return {
        "walking": "步行",
        "bicycling": "骑行",
        "transit": "公交/地铁",
        "driving": "驾车",
        "taxi": "打车",
    }.get(mode, mode)

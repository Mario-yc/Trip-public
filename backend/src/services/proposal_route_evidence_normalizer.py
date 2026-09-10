"""Normalize every Portfolio route source to the canonical RouteOption DTO.

Creative Portfolio proposals can outlive a route preflight attempt.  Older
snapshots therefore contain a mixture of ``routeOptions``, compact
``portfolioRouteEvidence`` records, and day-scoped evidence.  Consumers must
not guess which shape they received: this module is the one place that turns
those sources into the RouteOptionResponse-compatible payload used by the
comparison UI, readiness checks, finalization, and debug export.

The normalizer is deliberately fail-closed.  A legacy summary without route
geometry, provider, or a durable route id is useful diagnostic evidence, but
it is *not* a verified route and is marked ``needs_refresh``.
"""

from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from src.models.route_option import (
    RouteOption,
    normalize_route_mode,
    route_evidence_status,
    route_mode_label,
)


class ProposalRouteEvidenceNormalizer:
    """Create one full, scoped RouteOption DTO stream for Portfolio snapshots."""

    CANONICAL_PROVIDERS = {
        "amap",
        "amap-route",
        "amap-webservice",
        "amap-web-service",
        "amap-route-provider",
    }
    VERIFIED_STATUSES = {"verified", "selected", "ready"}
    FAILURE_STATUSES = {"failed", "error", "provider_error", "unverified", "invalid", "needs_refresh"}

    @classmethod
    def normalize_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        focus_brief_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Merge top-level and day-scoped evidence without changing ``snapshot``."""

        if not isinstance(snapshot, dict):
            return []
        focus = (focus_brief_id or cls._focus_brief_id(snapshot)).strip()
        segments, segment_days = cls._segments(snapshot, focus)
        source_items: list[tuple[str, Any]] = []
        for source, rows in (
            ("route_options", snapshot.get("routeOptions")),
            ("portfolio_route_evidence", snapshot.get("portfolioRouteEvidence")),
            ("legacy_route_evidence", snapshot.get("routeEvidence")),
        ):
            if isinstance(rows, list):
                source_items.extend((source, item) for item in rows)
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict) or not isinstance(day.get("routeEvidence"), list):
                continue
            source_items.extend(("day_route_evidence", item) for item in day["routeEvidence"])
        return cls.normalize_items(
            source_items,
            segments=segments,
            segment_days=segment_days,
            plan_id=str(snapshot.get("id") or snapshot.get("planId") or ""),
            proposal_id=str(snapshot.get("proposalId") or snapshot.get("portfolioProposalId") or ""),
            candidate_fingerprint=str(snapshot.get("candidateFingerprint") or cls._snapshot_fingerprint(snapshot)),
        )

    @classmethod
    def normalize_items(
        cls,
        source_items: Iterable[tuple[str, Any]],
        *,
        segments: dict[str, dict[str, Any]],
        segment_days: dict[str, int],
        plan_id: str = "",
        proposal_id: str = "",
        candidate_fingerprint: str = "",
    ) -> list[dict[str, Any]]:
        """Normalize and de-duplicate by route pair/mode, preferring full DTOs."""

        chosen: dict[tuple[str, str, str], dict[str, Any]] = {}
        for source_kind, raw in source_items:
            dto = cls._normalize_one(
                raw,
                source_kind=source_kind,
                segments=segments,
                segment_days=segment_days,
                plan_id=plan_id,
                proposal_id=proposal_id,
                candidate_fingerprint=candidate_fingerprint,
            )
            if dto is None:
                continue
            key = (str(dto["fromSegmentId"]), str(dto["toSegmentId"]), str(dto["mode"]))
            previous = chosen.get(key)
            if previous is None or cls._priority(dto) > cls._priority(previous):
                chosen[key] = dto
        return sorted(
            chosen.values(),
            key=lambda item: (
                int(item.get("dayNumber") or 0),
                str(item.get("fromSegmentId") or ""),
                str(item.get("toSegmentId") or ""),
                str(item.get("mode") or ""),
            ),
        )

    @classmethod
    def route_option_dto(
        cls,
        route: RouteOption,
        *,
        from_segment_id: str,
        to_segment_id: str,
        from_poi_id: str,
        to_poi_id: str,
        from_amap_id: str = "",
        to_amap_id: str = "",
        day_number: int = 0,
        candidate_fingerprint: str = "",
        proposal_id: str = "",
    ) -> dict[str, Any]:
        """Serialize a RouteService ``RouteOption`` without losing fields."""

        queried = cls._iso(route.queried_at)
        status = route_evidence_status(
            provider_payload=route.provider_payload,
            error=route.error,
        )
        mode = normalize_route_mode(str(route.mode or "walking"))
        return {
            "id": str(route.id),
            "planId": str(route.plan_id or ""),
            "proposalId": proposal_id or None,
            "fromSegmentId": from_segment_id,
            "toSegmentId": to_segment_id,
            "fromPoiId": from_poi_id,
            "toPoiId": to_poi_id,
            "fromAmapId": from_amap_id or None,
            "toAmapId": to_amap_id or None,
            "provider": str(route.provider or "amap-webservice"),
            "mode": mode,
            "label": str(route.label or route_mode_label(mode)),
            "isSelected": bool(route.is_selected),
            "sortOrder": int(route.sort_order or 0),
            "transportMode": normalize_route_mode(str(route.transport_mode or mode)),
            "distanceMeters": int(route.distance_meters or 0),
            "distanceKm": round(float(route.distance_meters or 0) / 1000, 3),
            "durationSeconds": int(route.duration_seconds or 0),
            "durationMinutes": int(route.duration_minutes or 0),
            "costAmount": float(route.cost_amount or 0),
            "costCurrency": str(route.cost_currency or "CNY"),
            "costEstimate": float(route.cost_estimate or 0),
            "crowdingRisk": str(route.crowding_risk or ""),
            "source": str(route.source or route.provider or "amap-webservice"),
            "polyline": copy.deepcopy(route.polyline or []),
            "steps": copy.deepcopy(route.steps or []),
            "providerPayload": copy.deepcopy(route.provider_payload or {}),
            "error": copy.deepcopy(route.error) if isinstance(route.error, dict) else None,
            "queriedAt": queried,
            "status": status,
            "routeStatus": status,
            "dayNumber": int(day_number or 0),
            "candidateFingerprint": candidate_fingerprint or None,
            "normalizationStatus": (
                "canonical" if queried is not None else "legacy_summary_needs_refresh"
            ),
        }

    @classmethod
    def is_verified(cls, route: Any, segment_days: dict[str, int] | None = None) -> bool:
        if not isinstance(route, dict):
            return False
        start = str(route.get("fromSegmentId") or route.get("from_segment_id") or "")
        end = str(route.get("toSegmentId") or route.get("to_segment_id") or "")
        if not start or not end or start == end:
            return False
        if segment_days is not None and (start not in segment_days or end not in segment_days or segment_days[start] != segment_days[end]):
            return False
        provider = str(route.get("provider") or route.get("providerName") or "").casefold()
        source = str(route.get("source") or "").casefold()
        if provider not in cls.CANONICAL_PROVIDERS and source not in cls.CANONICAL_PROVIDERS:
            return False
        status = str(route.get("status") or route.get("routeStatus") or "").casefold()
        if route.get("error") or status not in cls.VERIFIED_STATUSES:
            return False
        try:
            distance = int(route.get("distanceMeters") or 0)
            duration = int(route.get("durationSeconds") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            str(route.get("id") or "")
            and distance > 0
            and duration > 0
            and isinstance(route.get("polyline"), list)
            and route.get("polyline")
            and cls._iso(route.get("queriedAt") or route.get("queried_at")) is not None
        )

    @classmethod
    def _normalize_one(
        cls,
        raw: Any,
        *,
        source_kind: str,
        segments: dict[str, dict[str, Any]],
        segment_days: dict[str, int],
        plan_id: str,
        proposal_id: str,
        candidate_fingerprint: str,
    ) -> dict[str, Any] | None:
        if isinstance(raw, RouteOption):
            raw = {
                "id": raw.id,
                "planId": raw.plan_id,
                "fromSegmentId": raw.from_segment_id,
                "toSegmentId": raw.to_segment_id,
                "fromPoiId": raw.from_poi_id,
                "toPoiId": raw.to_poi_id,
                "provider": raw.provider,
                "mode": raw.mode,
                "label": raw.label,
                "isSelected": raw.is_selected,
                "sortOrder": raw.sort_order,
                "distanceMeters": raw.distance_meters,
                "durationSeconds": raw.duration_seconds,
                "costAmount": raw.cost_amount,
                "costCurrency": raw.cost_currency,
                "polyline": raw.polyline,
                "steps": raw.steps,
                "providerPayload": raw.provider_payload,
                "error": raw.error,
                "queriedAt": raw.queried_at,
            }
        if not isinstance(raw, dict):
            return None
        start = str(raw.get("fromSegmentId") or raw.get("from_segment_id") or "").strip()
        end = str(raw.get("toSegmentId") or raw.get("to_segment_id") or "").strip()
        if not start or not end or start == end or start not in segments or end not in segments:
            return None
        if segment_days.get(start) != segment_days.get(end):
            return None
        source = str(raw.get("source") or raw.get("provider") or raw.get("providerName") or "").strip()
        provider = str(raw.get("provider") or raw.get("providerName") or source or "").strip()
        mode = normalize_route_mode(str(raw.get("mode") or raw.get("transportMode") or "walking"))
        distance = cls._integer(raw.get("distanceMeters"), default=0)
        if distance <= 0:
            distance = int(round(cls._float(raw.get("distanceKm"), default=0.0) * 1000))
        duration_seconds = cls._integer(raw.get("durationSeconds"), default=0)
        duration_minutes = cls._integer(raw.get("durationMinutes"), default=0)
        if duration_seconds <= 0 and duration_minutes > 0:
            duration_seconds = duration_minutes * 60
        if duration_minutes <= 0 and duration_seconds > 0:
            duration_minutes = max(1, math.ceil(duration_seconds / 60))
        polyline = copy.deepcopy(raw.get("polyline") or raw.get("geometry") or [])
        if not isinstance(polyline, list):
            polyline = []
        steps = copy.deepcopy(raw.get("steps") or [])
        if not isinstance(steps, list):
            steps = []
        provider_payload = copy.deepcopy(raw.get("providerPayload") or raw.get("provider_payload") or {})
        if not isinstance(provider_payload, dict):
            provider_payload = {}
        error = copy.deepcopy(raw.get("error")) if isinstance(raw.get("error"), dict) else None
        queried_at = cls._iso(raw.get("queriedAt") or raw.get("queried_at"))
        raw_status = route_evidence_status(
            provider_payload=provider_payload,
            error=error,
            explicit_status=raw.get("status") or raw.get("routeStatus"),
        )
        full = bool(
            str(raw.get("id") or "").strip()
            and provider.casefold() in cls.CANONICAL_PROVIDERS
            and source.casefold() in cls.CANONICAL_PROVIDERS
            and distance > 0
            and duration_seconds > 0
            and polyline
            and queried_at is not None
        )
        status = raw_status
        normalization_status = "canonical" if full else "legacy_summary_needs_refresh"
        if not full and raw_status in cls.VERIFIED_STATUSES:
            status = "needs_refresh"
        elif raw_status not in cls.FAILURE_STATUSES | cls.VERIFIED_STATUSES:
            status = "needs_refresh"
        source = source or provider
        provider = provider or source
        route_id = str(raw.get("id") or "").strip()
        if not route_id:
            route_id = "legacy_route_" + hashlib.sha256(
                f"{start}|{end}|{mode}|{source}|{distance}|{duration_seconds}".encode("utf-8")
            ).hexdigest()[:20]
        from_poi = cls._poi_identity(segments[start])
        to_poi = cls._poi_identity(segments[end])
        return {
            "id": route_id,
            "planId": str(raw.get("planId") or raw.get("plan_id") or plan_id or ""),
            "proposalId": str(raw.get("proposalId") or proposal_id or "") or None,
            "fromSegmentId": start,
            "toSegmentId": end,
            "fromPoiId": from_poi[0],
            "toPoiId": to_poi[0],
            "fromAmapId": from_poi[1] or None,
            "toAmapId": to_poi[1] or None,
            "provider": provider,
            "mode": mode,
            "label": str(raw.get("label") or route_mode_label(mode)),
            "isSelected": bool(raw.get("isSelected") if "isSelected" in raw else raw.get("is_selected", True)),
            "sortOrder": cls._integer(raw.get("sortOrder", raw.get("sort_order", 1)), default=1),
            "transportMode": normalize_route_mode(str(raw.get("transportMode") or mode)),
            "distanceMeters": distance,
            "distanceKm": round(distance / 1000, 3),
            "durationSeconds": duration_seconds,
            "durationMinutes": duration_minutes,
            "costAmount": cls._float(raw.get("costAmount", raw.get("cost_amount", raw.get("costEstimate", 0))), default=0.0),
            "costCurrency": str(raw.get("costCurrency") or raw.get("cost_currency") or "CNY"),
            "costEstimate": cls._float(raw.get("costEstimate", raw.get("cost_amount", raw.get("costAmount", 0))), default=0.0),
            "crowdingRisk": str(raw.get("crowdingRisk") or raw.get("crowding_risk") or ""),
            "source": source,
            "polyline": polyline,
            "steps": steps,
            "providerPayload": provider_payload,
            "error": error,
            "queriedAt": queried_at,
            "status": status,
            "routeStatus": status,
            "dayNumber": int(segment_days[start]),
            "candidateFingerprint": str(raw.get("candidateFingerprint") or candidate_fingerprint or "") or None,
            "normalizationStatus": normalization_status,
            "sourceKind": source_kind,
            "qualityIssue": raw.get("qualityIssue"),
        }

    @classmethod
    def _segments(cls, snapshot: dict[str, Any], focus_brief_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        segments: dict[str, dict[str, Any]] = {}
        days: dict[str, int] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = cls._integer(day.get("dayNumber"), default=0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                # A legacy active draft can predate per-segment brief lineage.
                # It is still the *current* snapshot, so retain unscoped anchors;
                # only exclude evidence that explicitly belongs to another brief.
                if focus_brief_id and not cls._segment_in_scope(segment, focus_brief_id):
                    continue
                identifier = str(segment.get("id") or "").strip()
                if identifier:
                    segments[identifier] = segment
                    days[identifier] = day_number
        return segments, days

    @staticmethod
    def _segment_in_scope(segment: dict[str, Any], focus_brief_id: str) -> bool:
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        segment_brief = str(semantic.get("creativeBriefId") or semantic.get("briefId") or "").strip()
        return not focus_brief_id or not segment_brief or segment_brief == focus_brief_id

    @staticmethod
    def _focus_brief_id(snapshot: dict[str, Any]) -> str:
        context = snapshot.get("portfolioSelectionContext")
        if isinstance(context, dict) and str(context.get("focusBriefId") or "").strip():
            return str(context["focusBriefId"]).strip()
        brief = snapshot.get("creativeBrief")
        return str(brief.get("briefId") or "").strip() if isinstance(brief, dict) else ""

    @staticmethod
    def _poi_identity(segment: dict[str, Any]) -> tuple[str, str]:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return str(poi.get("id") or ""), str(poi.get("amapId") or "")

    @staticmethod
    def _priority(route: dict[str, Any]) -> tuple[int, int, int]:
        verified = int(ProposalRouteEvidenceNormalizer.is_verified(route))
        canonical = int(route.get("normalizationStatus") == "canonical")
        selected = int(bool(route.get("isSelected")))
        return verified, canonical, selected

    @staticmethod
    def _integer(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _iso(value: Any) -> str | None:
        parsed: datetime
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
        identifiers = [
            str(segment.get("id") or "")
            for day in snapshot.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
        ]
        return hashlib.sha256("|".join(sorted(identifiers)).encode("utf-8")).hexdigest()[:24]

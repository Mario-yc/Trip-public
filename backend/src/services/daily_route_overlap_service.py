"""Deterministic per-day overlap evidence for verified route geometry.

The service is deliberately pure: it performs no Provider calls and writes no
database state.  It only accepts real, ordered route-leg geometry and returns
portable evidence that can be replayed by later verifier/readiness gates.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable


EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True)
class _RouteUnit:
    leg_index: int
    mode: str
    start: tuple[float, float]
    end: tuple[float, float]
    length_meters: float
    exception: dict[str, Any] | None


class DailyRouteOverlapService:
    """Measure repeated physical route fragments for one ordered day."""

    GEOMETRY_POLICY_VERSION = "route-overlap-geometry-v1"
    SCHEMA_VERSION = "daily-route-continuity-evidence-v1"
    RESAMPLE_METERS = 10.0
    MATCH_DISTANCE_METERS = 5.0
    MIN_MATCH_METERS = 1.5
    MAX_HEADING_DELTA_DEGREES = 20.0
    SPATIAL_INDEX_CELL_METERS = 10.0
    MAX_GEOMETRY_PARTS_PER_DAY = 96
    MAX_GEOMETRY_POINTS_PER_DAY = 4096
    _ALLOWED_EXCEPTION_REASONS = frozenset(
        {
            "transit_transfer",
            "bounded_meal_spur",
            "bounded_required_poi",
            "hotel_or_terminal_return",
            "user_locked_sequence",
        }
    )
    _ALLOWED_EXCEPTION_SOURCES = frozenset(
        {
            "server_route_policy",
            "route_decision_contract",
            "provider_step_metadata",
            "user_explicit_sequence",
        }
    )

    def evaluate(
        self,
        *,
        day_number: int,
        route_legs: Iterable[dict[str, Any]],
        alternatives_evaluated: int = 1,
        selected_alternative_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        legs = [dict(item) for item in route_legs if isinstance(item, dict)]
        selected_ids = [str(item) for item in (selected_alternative_ids or []) if str(item).strip()]
        route_material: list[dict[str, Any]] = []
        all_units: list[_RouteUnit] = []
        missing_ids: list[str] = []
        oversized_ids: list[str] = []
        accepted_part_count = 0
        accepted_point_count = 0
        parts_by_leg: list[tuple[int, list[tuple[str, list[list[float]], dict[str, Any] | None]]]] = []

        for leg_index, leg in enumerate(legs):
            route_option_id = self._route_option_id(leg, leg_index)
            parts = self._geometry_parts(leg)
            provider_fingerprint = self.provider_evidence_fingerprint(leg)
            part_count = len(parts)
            point_count = sum(len(points) for _mode, points, _exception in parts)
            geometry_too_large = bool(
                parts
                and (
                    accepted_part_count + part_count > self.MAX_GEOMETRY_PARTS_PER_DAY
                    or accepted_point_count + point_count > self.MAX_GEOMETRY_POINTS_PER_DAY
                )
            )
            geometry_material = (
                []
                if geometry_too_large
                else [
                    {
                        "mode": mode,
                        "polyline": self._canonical_polyline(points),
                        "exceptionProvenance": exception,
                    }
                    for mode, points, exception in parts
                ]
            )
            geometry_fingerprint = self._fingerprint(geometry_material) if geometry_material else ""
            selected_geometry = {
                "routeOptionId": route_option_id,
                "fromAmapId": str(leg.get("fromAmapId") or "").strip().upper(),
                "toAmapId": str(leg.get("toAmapId") or "").strip().upper(),
                "transportMode": self._canonical_mode(leg.get("mode") or leg.get("transportMode")),
                "provider": str(leg.get("provider") or leg.get("source") or ""),
                "queriedAt": str(leg.get("queriedAt") or ""),
                "distanceMeters": self._positive_number(leg, "distanceMeters", "distance_meters", "distance"),
                "durationSeconds": self._positive_number(
                    leg,
                    "durationSeconds",
                    "duration_seconds",
                    "duration",
                ),
                "providerEvidenceFingerprint": provider_fingerprint,
                "geometryFingerprint": geometry_fingerprint,
                "geometryParts": geometry_material,
                "geometryPartCount": part_count,
                "geometryPointCount": point_count,
                "geometryStatus": ("too_large" if geometry_too_large else "ready" if geometry_material else "missing"),
            }
            edge_identity = self._edge_identity_material(leg)
            if edge_identity:
                selected_geometry.update(edge_identity)
            selected_geometry["routeFingerprint"] = self._fingerprint(
                {
                    "providerEvidenceFingerprint": provider_fingerprint,
                    "geometryFingerprint": geometry_fingerprint,
                }
            )
            route_material.append(selected_geometry)
            if not parts:
                missing_ids.append(route_option_id)
                continue
            if geometry_too_large:
                oversized_ids.append(route_option_id)
                continue
            accepted_part_count += part_count
            accepted_point_count += point_count
            parts_by_leg.append((leg_index, parts))

        route_pair_fingerprints = [item["providerEvidenceFingerprint"] for item in route_material]
        base: dict[str, Any] = {
            "schemaVersion": self.SCHEMA_VERSION,
            "geometryPolicyVersion": self.GEOMETRY_POLICY_VERSION,
            "dayNumber": int(day_number),
            "routePairFingerprints": route_pair_fingerprints,
            "geometryFingerprint": "",
            "geometryComplete": not missing_ids and not oversized_ids and len(route_material) == len(legs),
            "totalTraversedMeters": 0.0,
            "repeatedMeters": 0.0,
            "exemptRepeatedMeters": 0.0,
            "nonExemptRepeatedMeters": 0.0,
            "overlapRatio": 0.0,
            "sameDirectionRepeatedMeters": 0.0,
            "reverseDirectionRepeatedMeters": 0.0,
            "exceptionApplications": [],
            "alternativesEvaluated": max(0, int(alternatives_evaluated or 0)),
            "selectedAlternativeIds": selected_ids,
            "selectedRouteGeometry": route_material,
            "source": "verified_route_options",
            "status": "route_geometry_pending",
            "failureReason": (
                "verified_route_geometry_too_large"
                if oversized_ids
                else "verified_route_geometry_missing"
                if missing_ids
                else None
            ),
            "missingGeometryRouteOptionIds": missing_ids,
            "oversizedGeometryRouteOptionIds": oversized_ids,
            "geometryMaterialLimits": {
                "maxPartsPerDay": self.MAX_GEOMETRY_PARTS_PER_DAY,
                "maxPointsPerDay": self.MAX_GEOMETRY_POINTS_PER_DAY,
            },
        }
        if missing_ids or oversized_ids:
            base["evidenceFingerprint"] = self.evidence_fingerprint(base)
            return base

        day_latitudes = [
            point[1] for _leg_index, parts in parts_by_leg for _mode, points, _exception in parts for point in points
        ]
        reference_latitude = sum(day_latitudes) / len(day_latitudes) if day_latitudes else 0.0
        for leg_index, parts in parts_by_leg:
            for mode, points, exception in parts:
                all_units.extend(
                    self._resampled_units(
                        points,
                        leg_index=leg_index,
                        mode=mode,
                        exception=exception,
                        reference_latitude=reference_latitude,
                    )
                )

        geometry_fingerprints = [item["geometryFingerprint"] for item in route_material]
        base["geometryFingerprint"] = self._fingerprint(geometry_fingerprints)
        metrics = self._measure(all_units)
        base.update(metrics)
        base["status"] = (
            "passed_with_exempt_overlap"
            if metrics["exemptRepeatedMeters"] > 0 and metrics["nonExemptRepeatedMeters"] == 0
            else "overlap_evaluated"
        )
        base["evidenceFingerprint"] = self.evidence_fingerprint(base)
        return base

    @classmethod
    def evidence_fingerprint(cls, evidence: dict[str, Any]) -> str:
        """Seal every final evidence field except the seal itself."""

        material = dict(evidence)
        material.pop("evidenceFingerprint", None)
        return cls._fingerprint(material)

    @classmethod
    def verify_evidence_fingerprint(cls, evidence: Any) -> bool:
        if not isinstance(evidence, dict):
            return False
        actual = str(evidence.get("evidenceFingerprint") or "").strip().lower()
        return bool(actual) and actual == cls.evidence_fingerprint(evidence)

    def _measure(self, units: list[_RouteUnit]) -> dict[str, Any]:
        total = sum(unit.length_meters for unit in units)
        repeated = 0.0
        same = 0.0
        reverse = 0.0
        exempt = 0.0
        applications: dict[tuple[str, str, float], dict[str, Any]] = {}
        exception_usage: dict[tuple[str, str, float], float] = {}
        spatial_index: dict[tuple[str, int, int], list[_RouteUnit]] = {}

        for unit in units:
            cell_x, cell_y = self._unit_cell(unit)
            candidates = [
                item
                for offset_x in range(-2, 3)
                for offset_y in range(-2, 3)
                for item in spatial_index.get((unit.mode, cell_x + offset_x, cell_y + offset_y), [])
                if item.leg_index < unit.leg_index
            ]
            same_intervals: list[tuple[float, float]] = []
            reverse_intervals: list[tuple[float, float]] = []
            for existing in candidates:
                match = self._matching_interval(unit, existing)
                if match is None:
                    continue
                interval, is_reverse = match
                if is_reverse:
                    reverse_intervals.append(interval)
                else:
                    same_intervals.append(interval)
            classified = self._classified_interval_lengths(same_intervals, reverse_intervals)
            unit_same = classified["same"]
            unit_reverse = classified["reverse"]
            unit_repeated = unit_same + unit_reverse
            repeated += unit_repeated
            same += unit_same
            reverse += unit_reverse

            provenance = unit.exception
            if provenance is not None and unit_repeated > 0:
                key = (
                    str(provenance["reasonCode"]),
                    str(provenance["source"]),
                    float(provenance["maxExemptMeters"]),
                )
                remaining = max(0.0, key[2] - exception_usage.get(key, 0.0))
                applied = min(unit_repeated, remaining)
                if applied > 0:
                    exempt += applied
                    exception_usage[key] = exception_usage.get(key, 0.0) + applied
                    application = applications.setdefault(
                        key,
                        {
                            "schemaVersion": "route-overlap-exception-application-v1",
                            "reasonCode": key[0],
                            "source": key[1],
                            "maxExemptMeters": key[2],
                            "exemptRepeatedMeters": 0.0,
                        },
                    )
                    application["exemptRepeatedMeters"] += applied
            spatial_index.setdefault((unit.mode, cell_x, cell_y), []).append(unit)

        non_exempt = max(0.0, repeated - exempt)
        return {
            "totalTraversedMeters": self._rounded(total),
            "repeatedMeters": self._rounded(repeated),
            "exemptRepeatedMeters": self._rounded(exempt),
            "nonExemptRepeatedMeters": self._rounded(non_exempt),
            "overlapRatio": round(non_exempt / total, 6) if total > 0 else 0.0,
            "sameDirectionRepeatedMeters": self._rounded(same),
            "reverseDirectionRepeatedMeters": self._rounded(reverse),
            "exceptionApplications": [
                {
                    **value,
                    "maxExemptMeters": self._rounded(float(value["maxExemptMeters"])),
                    "exemptRepeatedMeters": self._rounded(float(value["exemptRepeatedMeters"])),
                }
                for _key, value in sorted(applications.items())
            ],
        }

    def _matching_interval(
        self,
        current: _RouteUnit,
        existing: _RouteUnit,
    ) -> tuple[tuple[float, float], bool] | None:
        current_dx = current.end[0] - current.start[0]
        current_dy = current.end[1] - current.start[1]
        existing_dx = existing.end[0] - existing.start[0]
        existing_dy = existing.end[1] - existing.start[1]
        dot = (current_dx * existing_dx + current_dy * existing_dy) / (current.length_meters * existing.length_meters)
        if abs(dot) < math.cos(math.radians(self.MAX_HEADING_DELTA_DEGREES)):
            return None

        existing_ux = existing_dx / existing.length_meters
        existing_uy = existing_dy / existing.length_meters
        perpendicular_distances = []
        for point in (current.start, current.end):
            rel_x = point[0] - existing.start[0]
            rel_y = point[1] - existing.start[1]
            perpendicular_distances.append(abs(rel_x * existing_uy - rel_y * existing_ux))
        if max(perpendicular_distances) > self.MATCH_DISTANCE_METERS:
            return None

        current_ux = current_dx / current.length_meters
        current_uy = current_dy / current.length_meters
        projected = []
        for point in (existing.start, existing.end):
            rel_x = point[0] - current.start[0]
            rel_y = point[1] - current.start[1]
            projected.append(rel_x * current_ux + rel_y * current_uy)
        start = max(0.0, min(projected))
        end = min(current.length_meters, max(projected))
        overlap = end - start
        required = min(self.MIN_MATCH_METERS, current.length_meters * 0.25, existing.length_meters * 0.25)
        if overlap + 1e-6 < required:
            return None
        return (start, end), dot < 0

    @staticmethod
    def _classified_interval_lengths(
        same_intervals: list[tuple[float, float]],
        reverse_intervals: list[tuple[float, float]],
    ) -> dict[str, float]:
        tagged = [(start, end, "same") for start, end in same_intervals] + [
            (start, end, "reverse") for start, end in reverse_intervals
        ]
        boundaries = sorted({value for start, end, _kind in tagged for value in (start, end)})
        same = 0.0
        reverse = 0.0
        for left, right in zip(boundaries, boundaries[1:]):
            if right <= left:
                continue
            midpoint = (left + right) / 2
            kinds = {kind for start, end, kind in tagged if start <= midpoint <= end}
            if "reverse" in kinds and "same" not in kinds:
                reverse += right - left
            elif kinds:
                same += right - left
        return {"same": same, "reverse": reverse}

    @classmethod
    def _unit_cell(cls, unit: _RouteUnit) -> tuple[int, int]:
        midpoint_x = (unit.start[0] + unit.end[0]) / 2
        midpoint_y = (unit.start[1] + unit.end[1]) / 2
        return (
            math.floor(midpoint_x / cls.SPATIAL_INDEX_CELL_METERS),
            math.floor(midpoint_y / cls.SPATIAL_INDEX_CELL_METERS),
        )

    def _geometry_parts(
        self,
        leg: dict[str, Any],
    ) -> list[tuple[str, list[list[float]], dict[str, Any] | None]]:
        if "geometryParts" in leg:
            raw_parts = leg.get("geometryParts")
            if not isinstance(raw_parts, list) or not raw_parts:
                return []
            sealed_parts: list[tuple[str, list[list[float]], dict[str, Any] | None]] = []
            for part in raw_parts:
                if not isinstance(part, dict) or set(part) != {
                    "mode",
                    "polyline",
                    "exceptionProvenance",
                }:
                    return []
                mode = self._canonical_mode(part.get("mode"))
                points = self._polyline_points(part.get("polyline"))
                raw_exception = part.get("exceptionProvenance")
                exception = self._normalized_exception(raw_exception)
                if not mode or len(points) < 2 or (raw_exception is not None and exception is None):
                    return []
                sealed_parts.append((mode, points, exception))
            return sealed_parts
        leg_mode = self._canonical_mode(leg.get("mode") or leg.get("transportMode"))
        leg_exception = self._normalized_exception(leg.get("overlapException") or leg.get("exceptionProvenance"))
        parts: list[tuple[str, list[list[float]], dict[str, Any] | None]] = []
        raw_steps = leg.get("steps") or []
        if not isinstance(raw_steps, list):
            return []
        for step in raw_steps:
            if not isinstance(step, dict):
                return []
            points = self._polyline_points(step.get("polyline"))
            if len(points) < 2:
                # A partially materialized Provider route cannot support an
                # actual-road overlap claim.  Falling back to a top-level
                # polyline here would silently erase the missing step and
                # could turn an incomplete route into a successful audit.
                return []
            step_mode = self._canonical_mode(step.get("mode") or leg_mode)
            step_exception = self._normalized_exception(step.get("overlapException") or step.get("exceptionProvenance"))
            parts.append((step_mode, points, step_exception or leg_exception))
        if parts:
            return parts
        points = self._top_level_polyline(leg)
        if len(points) >= 2:
            parts.append((leg_mode, points, leg_exception))
        return parts

    def _resampled_units(
        self,
        points: list[list[float]],
        *,
        leg_index: int,
        mode: str,
        exception: dict[str, Any] | None,
        reference_latitude: float,
    ) -> list[_RouteUnit]:
        projected = [self._project(point, reference_latitude) for point in points]
        units: list[_RouteUnit] = []
        for start, end in zip(projected, projected[1:]):
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            length = math.hypot(dx, dy)
            if not math.isfinite(length) or length <= 0.05:
                continue
            count = max(1, int(math.ceil(length / self.RESAMPLE_METERS)))
            for index in range(count):
                ratio_start = index / count
                ratio_end = (index + 1) / count
                unit_start = (start[0] + dx * ratio_start, start[1] + dy * ratio_start)
                unit_end = (start[0] + dx * ratio_end, start[1] + dy * ratio_end)
                unit_length = math.hypot(unit_end[0] - unit_start[0], unit_end[1] - unit_start[1])
                units.append(
                    _RouteUnit(
                        leg_index=leg_index,
                        mode=mode,
                        start=unit_start,
                        end=unit_end,
                        length_meters=unit_length,
                        exception=exception,
                    )
                )
        return units

    def _normalized_exception(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        if str(value.get("schemaVersion") or "") != "route-overlap-exception-v1":
            return None
        reason = str(value.get("reasonCode") or "").strip()
        source = str(value.get("source") or "").strip()
        if reason not in self._ALLOWED_EXCEPTION_REASONS or source not in self._ALLOWED_EXCEPTION_SOURCES:
            return None
        try:
            maximum = float(value.get("maxExemptMeters"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(maximum) or maximum <= 0:
            return None
        return {
            "schemaVersion": "route-overlap-exception-v1",
            "reasonCode": reason,
            "source": source,
            "maxExemptMeters": maximum,
        }

    @classmethod
    def provider_evidence_fingerprint(cls, leg: dict[str, Any]) -> str:
        edge_identity = cls._edge_identity_material(leg)
        if edge_identity is None:
            return ""
        material: dict[str, Any] = {
            **edge_identity,
            "fromAmapId": str(leg.get("fromAmapId") or "").strip().upper(),
            "toAmapId": str(leg.get("toAmapId") or "").strip().upper(),
            "transportMode": cls._canonical_mode(leg.get("mode") or leg.get("transportMode")),
            "durationSeconds": cls._positive_number(leg, "durationSeconds", "duration_seconds", "duration"),
            "distanceMeters": cls._positive_number(leg, "distanceMeters", "distance_meters", "distance"),
            "provider": str(leg.get("provider") or leg.get("source") or ""),
            "queriedAt": str(leg.get("queriedAt") or ""),
        }
        return cls._fingerprint(material)

    @staticmethod
    def _edge_identity_material(leg: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize a complete day-local edge identity.

        An empty mapping is the legacy Provider-only contract. Partial identity
        cannot be downgraded to that legacy shape and therefore has no valid
        Provider evidence fingerprint.
        """

        keys = ("dayNumber", "pairOrdinal", "fromSegmentId", "toSegmentId")
        present = [key in leg for key in keys]
        if not any(present):
            return {}
        if not all(present):
            return None
        try:
            day_number = int(leg.get("dayNumber"))
            pair_ordinal = int(leg.get("pairOrdinal"))
        except (TypeError, ValueError):
            return None
        from_segment_id = str(leg.get("fromSegmentId") or "").strip()
        to_segment_id = str(leg.get("toSegmentId") or "").strip()
        if (
            day_number <= 0
            or pair_ordinal <= 0
            or not from_segment_id
            or not to_segment_id
            or from_segment_id == to_segment_id
        ):
            return None
        return {
            "dayNumber": day_number,
            "pairOrdinal": pair_ordinal,
            "fromSegmentId": from_segment_id,
            "toSegmentId": to_segment_id,
        }

    @classmethod
    def _route_option_id(cls, leg: dict[str, Any], index: int) -> str:
        explicit = str(leg.get("routeOptionId") or leg.get("route_option_id") or leg.get("id") or "").strip()
        if explicit:
            return explicit
        return f"verified-leg-{index + 1}-{cls.provider_evidence_fingerprint(leg)[:12]}"

    @classmethod
    def _top_level_polyline(cls, leg: dict[str, Any]) -> list[list[float]]:
        return cls._polyline_points(leg.get("polyline"))

    @staticmethod
    def _polyline_points(value: Any) -> list[list[float]]:
        raw: list[Any]
        if isinstance(value, str):
            raw = [item.split(",", 1) for item in value.split(";") if item.strip()]
        elif isinstance(value, (list, tuple)):
            raw = list(value)
        else:
            return []
        result: list[list[float]] = []
        for point in raw:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                return []
            try:
                longitude = float(point[0])
                latitude = float(point[1])
            except (TypeError, ValueError):
                return []
            if not math.isfinite(longitude) or not math.isfinite(latitude):
                return []
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                return []
            canonical = [longitude, latitude]
            if not result or result[-1] != canonical:
                result.append(canonical)
        return result

    @staticmethod
    def _canonical_polyline(points: list[list[float]]) -> list[list[float]]:
        return [[round(float(point[0]), 7), round(float(point[1]), 7)] for point in points]

    @staticmethod
    def _project(point: list[float], reference_latitude: float) -> tuple[float, float]:
        longitude, latitude = point
        return (
            math.radians(longitude) * EARTH_RADIUS_METERS * math.cos(math.radians(reference_latitude)),
            math.radians(latitude) * EARTH_RADIUS_METERS,
        )

    @staticmethod
    def _canonical_mode(value: Any) -> str:
        normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        return {
            "walk": "walking",
            "public_transit": "transit",
            "public_transport": "transit",
            "bus": "transit",
            "subway": "transit",
            "metro": "transit",
            "rail": "transit",
            "bike": "bicycling",
            "cycling": "bicycling",
            "self_drive": "driving",
        }.get(normalized, normalized)

    @staticmethod
    def _positive_number(value: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            try:
                number = float(value.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                return number
        return None

    @staticmethod
    def _rounded(value: float) -> float:
        return round(max(0.0, value), 2)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

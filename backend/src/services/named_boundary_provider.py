from __future__ import annotations

import hashlib
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.services.spatial_geometry_service import Point, SpatialGeometryService


@dataclass(frozen=True)
class NamedBoundaryCandidate:
    source_entity_id: str
    canonical_name: str
    source_version: str | None
    content_hash: str
    connected_component_count: int
    closed_cycle_count: int
    original_polygon: list[Point]
    simplified_polygon: list[Point]
    simplification_max_deviation_meters: float


@dataclass(frozen=True)
class NamedBoundaryResolutionResult:
    status: str
    candidates: list[NamedBoundaryCandidate]
    reason: str | None = None


class NamedBoundaryProvider(Protocol):
    provider_name: str
    source_url: str

    def resolve(
        self,
        *,
        city_scope: dict[str, Any],
        boundary_text: str,
        containment: str,
    ) -> NamedBoundaryResolutionResult: ...


class DisabledNamedBoundaryProvider:
    provider_name = "disabled"
    source_url = ""

    def resolve(self, **_: Any) -> NamedBoundaryResolutionResult:
        return NamedBoundaryResolutionResult(status="provider_unavailable", candidates=[], reason="provider_disabled")


class OverpassNamedBoundaryProvider:
    """Resolve named boundaries from dynamic OSM relation geometry.

    Open components are reported but never closed, bridged, or replaced by a
    hull.  Multiple valid cycles remain ambiguous for explicit user selection.
    """

    provider_name = "osm_overpass"

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: float,
        max_vertices: int = 40,
        max_deviation_meters: float = 50.0,
    ):
        self.endpoint = str(endpoint).strip()
        self.source_url = self.endpoint
        self.timeout_seconds = float(timeout_seconds)
        self.max_vertices = int(max_vertices)
        self.max_deviation_meters = float(max_deviation_meters)

    def resolve(
        self,
        *,
        city_scope: dict[str, Any],
        boundary_text: str,
        containment: str,
    ) -> NamedBoundaryResolutionResult:
        if containment not in {"inside", "outside"}:
            return NamedBoundaryResolutionResult(status="invalid_input", candidates=[], reason="containment_invalid")
        bbox = city_scope.get("queryBbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            return NamedBoundaryResolutionResult(status="invalid_city_scope", candidates=[], reason="city_bbox_missing")
        try:
            south, west, north, east = (float(value) for value in bbox)
        except (TypeError, ValueError):
            return NamedBoundaryResolutionResult(status="invalid_city_scope", candidates=[], reason="city_bbox_invalid")
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            return NamedBoundaryResolutionResult(status="invalid_city_scope", candidates=[], reason="city_bbox_invalid")
        escaped = re.escape(str(boundary_text).strip())
        if not escaped:
            return NamedBoundaryResolutionResult(status="invalid_input", candidates=[], reason="boundary_text_missing")
        query = (
            "[out:json][timeout:20];("
            f'relation["name"~"{escaped}",i]({south},{west},{north},{east});'
            f'relation["name:zh"~"{escaped}",i]({south},{west},{north},{east});'
            f'relation["alt_name"~"{escaped}",i]({south},{west},{north},{east});'
            f'relation["short_name"~"{escaped}",i]({south},{west},{north},{east});'
            ");out body geom;"
        )
        request = Request(
            self.endpoint,
            data=urlencode({"data": query}).encode("utf-8"),
            headers={"User-Agent": "trip-agent-named-boundary/1.0", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, socket.timeout, ValueError, json.JSONDecodeError) as error:
            wrapped_reason = error.reason if isinstance(error, URLError) else None
            is_timeout = isinstance(error, (TimeoutError, socket.timeout)) or isinstance(
                wrapped_reason,
                (TimeoutError, socket.timeout),
            )
            return NamedBoundaryResolutionResult(
                status="provider_unavailable",
                candidates=[],
                reason="provider_timeout" if is_timeout else type(error).__name__,
            )
        return self.resolve_payload(payload, city_scope=city_scope, boundary_text=boundary_text)

    def resolve_payload(
        self,
        payload: dict[str, Any],
        *,
        city_scope: dict[str, Any],
        boundary_text: str,
    ) -> NamedBoundaryResolutionResult:
        bbox = [float(value) for value in city_scope.get("queryBbox") or []]
        candidates: list[NamedBoundaryCandidate] = []
        saw_matching_relation = False
        saw_open_geometry = False
        for element in payload.get("elements") or []:
            if not isinstance(element, dict) or str(element.get("type") or "") != "relation":
                continue
            relation_id = element.get("id")
            if not isinstance(relation_id, int) or isinstance(relation_id, bool) or relation_id <= 0:
                continue
            tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
            names = [str(tags.get(key) or "") for key in ("name", "name:zh", "alt_name", "short_name")]
            if not any(str(boundary_text).casefold() in name.casefold() for name in names if name):
                continue
            saw_matching_relation = True
            ways: list[list[Point]] = []
            for member in element.get("members") or []:
                if not isinstance(member, dict) or str(member.get("type") or "") != "way":
                    continue
                geometry = member.get("geometry") if isinstance(member.get("geometry"), list) else []
                points = [
                    (float(node["lon"]), float(node["lat"]))
                    for node in geometry
                    if isinstance(node, dict) and isinstance(node.get("lon"), (int, float)) and isinstance(node.get("lat"), (int, float))
                ]
                if len(points) >= 2:
                    ways.append(points)
            components = SpatialGeometryService.closed_components(ways)
            if not components:
                saw_open_geometry = saw_open_geometry or bool(ways)
                continue
            for polygon in components:
                if len(bbox) != 4 or not self._inside_bbox(polygon, bbox):
                    continue
                simplified = SpatialGeometryService.simplify_closed_polygon(
                    polygon,
                    max_vertices=self.max_vertices,
                    max_deviation_meters=self.max_deviation_meters,
                )
                if simplified is None:
                    continue
                simplified_polygon, deviation = simplified
                canonical = {
                    "elementId": str(relation_id),
                    "tags": {key: tags[key] for key in sorted(tags) if key in {"name", "name:zh", "alt_name", "short_name", "type"}},
                    "polygon": polygon,
                }
                candidates.append(
                    NamedBoundaryCandidate(
                        source_entity_id=f"relation/{relation_id}",
                        canonical_name=next((name for name in names if name), str(boundary_text)),
                        source_version=str(element.get("version")) if element.get("version") is not None else None,
                        content_hash=hashlib.sha256(
                            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest(),
                        connected_component_count=self._component_count(ways),
                        closed_cycle_count=len(components),
                        original_polygon=polygon,
                        simplified_polygon=simplified_polygon,
                        simplification_max_deviation_meters=round(float(deviation), 3),
                    )
                )
        if len(candidates) == 1:
            return NamedBoundaryResolutionResult(status="resolved", candidates=candidates)
        if len(candidates) > 1:
            return NamedBoundaryResolutionResult(status="ambiguous", candidates=candidates, reason="multiple_closed_boundaries")
        if saw_open_geometry:
            return NamedBoundaryResolutionResult(status="boundary_not_closed", candidates=[], reason="no_real_closed_cycle")
        return NamedBoundaryResolutionResult(
            status="boundary_not_found",
            candidates=[],
            reason="matching_relation_missing" if not saw_matching_relation else "closed_boundary_unusable",
        )

    @staticmethod
    def evidence_base(
        candidate: NamedBoundaryCandidate,
        *,
        boundary_text: str,
        containment: str,
        source_url: str,
    ) -> dict[str, Any]:
        entity_url = f"https://www.openstreetmap.org/{candidate.source_entity_id}"
        return {
            "schemaVersion": "named-boundary-evidence-v1",
            "provider": "osm_overpass",
            "sourceUrl": entity_url,
            "retrievalEndpoint": source_url,
            "sourceEntityId": candidate.source_entity_id,
            "sourceVersion": candidate.source_version,
            "retrievedAt": datetime.now(timezone.utc).isoformat(),
            "contentHash": candidate.content_hash,
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "boundaryText": boundary_text,
            "canonicalName": candidate.canonical_name,
            "containment": containment,
            "connectedComponentCount": candidate.connected_component_count,
            "closedCycleCount": candidate.closed_cycle_count,
            "originalVertexCount": len(candidate.original_polygon),
            "simplifiedVertexCount": len(candidate.simplified_polygon),
            "simplificationMaxDeviationMeters": candidate.simplification_max_deviation_meters,
        }

    @staticmethod
    def _inside_bbox(polygon: list[Point], bbox: list[float]) -> bool:
        south, west, north, east = bbox
        return all(west <= lon <= east and south <= lat <= north for lon, lat in polygon)

    @staticmethod
    def _component_count(ways: list[list[Point]]) -> int:
        unseen = set(range(len(ways)))
        count = 0
        while unseen:
            count += 1
            stack = [unseen.pop()]
            while stack:
                index = stack.pop()
                endpoints = {SpatialGeometryService.point_key(ways[index][0]), SpatialGeometryService.point_key(ways[index][-1])}
                neighbours = {
                    other
                    for other in unseen
                    if endpoints
                    & {SpatialGeometryService.point_key(ways[other][0]), SpatialGeometryService.point_key(ways[other][-1])}
                }
                unseen -= neighbours
                stack.extend(neighbours)
        return count

from __future__ import annotations

import math
import socket
from urllib.error import URLError

import src.services.named_boundary_provider as named_boundary_provider_module
from src.services.named_boundary_provider import OverpassNamedBoundaryProvider
from src.services.spatial_geometry_service import SpatialGeometryService


def _provider() -> OverpassNamedBoundaryProvider:
    return OverpassNamedBoundaryProvider(
        endpoint="https://example.invalid/overpass",
        timeout_seconds=1,
        max_vertices=40,
        max_deviation_meters=50,
    )


def _scope() -> dict:
    return {"queryBbox": [0.0, 0.0, 10.0, 10.0], "name": "fixture-city", "adcode": "fixture"}


def test_named_boundary_accepts_real_closed_component_and_never_closes_open_component() -> None:
    payload = {
        "elements": [
            {
                "type": "relation",
                "id": 17,
                "version": 3,
                "tags": {"name": "环形测试边界", "type": "route"},
                "members": [
                    {"type": "way", "geometry": [{"lon": 1.0, "lat": 1.0}, {"lon": 4.0, "lat": 1.0}]},
                    {"type": "way", "geometry": [{"lon": 4.0, "lat": 1.0}, {"lon": 4.0, "lat": 4.0}]},
                    {"type": "way", "geometry": [{"lon": 4.0, "lat": 4.0}, {"lon": 1.0, "lat": 4.0}]},
                    {"type": "way", "geometry": [{"lon": 1.0, "lat": 4.0}, {"lon": 1.0, "lat": 1.0}]},
                    {"type": "way", "geometry": [{"lon": 6.0, "lat": 6.0}, {"lon": 8.0, "lat": 8.0}]},
                ],
            }
        ]
    }

    result = _provider().resolve_payload(payload, city_scope=_scope(), boundary_text="测试边界")

    assert result.status == "resolved"
    assert len(result.candidates) == 1
    assert result.candidates[0].connected_component_count == 2
    assert result.candidates[0].closed_cycle_count == 1
    assert result.candidates[0].simplified_polygon[0] == result.candidates[0].simplified_polygon[-1]


def test_named_boundary_open_relation_fails_closed() -> None:
    payload = {
        "elements": [
            {
                "type": "relation",
                "id": 23,
                "tags": {"name": "开放测试边界"},
                "members": [
                    {"type": "way", "geometry": [{"lon": 1.0, "lat": 1.0}, {"lon": 2.0, "lat": 2.0}]},
                    {"type": "way", "geometry": [{"lon": 2.0, "lat": 2.0}, {"lon": 3.0, "lat": 1.0}]},
                ],
            }
        ]
    }

    result = _provider().resolve_payload(payload, city_scope=_scope(), boundary_text="测试边界")

    assert result.status == "boundary_not_closed"
    assert result.candidates == []


def test_named_boundary_socket_timeout_is_reported_as_provider_unavailable(monkeypatch) -> None:
    def raise_socket_timeout(*_args, **_kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(named_boundary_provider_module, "urlopen", raise_socket_timeout)

    result = _provider().resolve(
        city_scope=_scope(),
        boundary_text="测试边界",
        containment="inside",
    )

    assert result.status == "provider_unavailable"
    assert result.reason == "provider_timeout"
    assert result.candidates == []


def test_named_boundary_wrapped_socket_timeout_uses_same_reason(monkeypatch) -> None:
    def raise_wrapped_timeout(*_args, **_kwargs):
        raise URLError(socket.timeout("timed out"))

    monkeypatch.setattr(named_boundary_provider_module, "urlopen", raise_wrapped_timeout)

    result = _provider().resolve(
        city_scope=_scope(),
        boundary_text="测试边界",
        containment="inside",
    )

    assert result.status == "provider_unavailable"
    assert result.reason == "provider_timeout"
    assert result.candidates == []


def test_named_boundary_relation_requires_a_positive_provider_identity() -> None:
    payload = {
        "elements": [
            {
                "type": "relation",
                "tags": {"name": "测试边界"},
                "members": [
                    {
                        "type": "way",
                        "geometry": [
                            {"lon": 1.0, "lat": 1.0},
                            {"lon": 2.0, "lat": 1.0},
                            {"lon": 2.0, "lat": 2.0},
                            {"lon": 1.0, "lat": 2.0},
                            {"lon": 1.0, "lat": 1.0},
                        ],
                    }
                ],
            }
        ]
    }

    result = _provider().resolve_payload(payload, city_scope=_scope(), boundary_text="测试边界")

    assert result.status == "boundary_not_found"
    assert result.candidates == []


def test_self_intersecting_polygon_is_rejected_and_point_in_polygon_is_bounded() -> None:
    bow_tie = [(0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0), (0.0, 0.0)]
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]

    assert SpatialGeometryService.valid_closed_polygon(bow_tie) is False
    assert SpatialGeometryService.point_in_polygon((1.0, 1.0), square) is True
    assert SpatialGeometryService.point_in_polygon((3.0, 1.0), square) is False


def test_polygon_simplification_reports_measured_error_not_search_tolerance() -> None:
    center = (116.4, 39.9)
    ring = [
        (
            center[0] + 0.01 * math.cos(2 * math.pi * index / 392),
            center[1] + 0.01 * math.sin(2 * math.pi * index / 392),
        )
        for index in range(392)
    ]
    polygon = [*ring, ring[0]]

    simplified = SpatialGeometryService.simplify_closed_polygon(
        polygon,
        max_vertices=40,
        max_deviation_meters=50,
    )

    assert simplified is not None
    simplified_polygon, measured_deviation = simplified
    assert len(simplified_polygon) <= 40
    assert measured_deviation == SpatialGeometryService.max_simplification_deviation_meters(
        polygon,
        simplified_polygon,
    )
    assert 0 < measured_deviation <= 50

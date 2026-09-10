from __future__ import annotations

from datetime import datetime, timezone

from src.models.route_option import RouteOption
from src.services.provider_route_insertion_service import ProviderRouteInsertionService


class FakeRouteService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build_routes(self, plan_id, pois, **kwargs):
        self.calls.append(dict(kwargs))
        return [
            RouteOption(
                id=f"route-{index}",
                plan_id=plan_id,
                from_segment_id="from-segment",
                to_segment_id="to-segment",
                from_poi_id=pois[0].id,
                to_poi_id=pois[1].id,
                provider="amap-webservice",
                mode="transit",
                distance_meters=1000 + index * 100,
                duration_seconds=600 + index * 60,
                polyline=[[116.1, 39.1], [116.2 + index * 0.001, 39.2]],
                steps=[
                    {
                        "mode": "transit",
                        "distance": 1000 + index * 100,
                        "duration": 600 + index * 60,
                        "polyline": f"116.1,39.1;{116.2 + index * 0.001},39.2",
                    }
                ],
                provider_payload={
                    "providerAlternativeIndex": index,
                    "walkingDistanceMeters": 50,
                    "transferCount": 0,
                    "waitSeconds": 0,
                },
                queried_at=datetime.now(timezone.utc),
                is_selected=index == 1,
                sort_order=index,
            )
            for index in range(1, 6)
        ]


def _point(identity: str, longitude: float) -> dict:
    return {
        "id": identity,
        "segmentId": f"segment-{identity}",
        "amapId": identity,
        "name": identity,
        "city": "北京",
        "longitude": longitude,
        "latitude": 39.9,
        "source": "amap-place-search",
    }


def test_verified_leg_options_is_opt_in_bounded_and_keeps_legacy_semantics() -> None:
    route_service = FakeRouteService()
    service = ProviderRouteInsertionService(route_service=route_service)

    legacy = service.verified_leg(
        plan_id="legacy",
        left=_point("A", 116.1),
        right=_point("B", 116.2),
        transport_mode="transit",
    )
    options = service.verified_leg_options(
        plan_id="bounded",
        left=_point("A", 116.1),
        right=_point("B", 116.2),
        transport_mode="transit",
    )

    assert legacy is not None
    assert legacy["routeOptionId"] == "route-1"
    assert "include_provider_alternatives" not in route_service.calls[0]
    assert route_service.calls[1]["include_provider_alternatives"] is True
    assert [item["routeOptionId"] for item in options] == ["route-1", "route-2", "route-3"]
    assert all(item["polyline"] for item in options)

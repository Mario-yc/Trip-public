from src.models.poi import POI
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.route_service import RouteService
from src.services.traffic_service import TrafficService
from src.services.weather_service import WeatherService


def test_route_weather_and_traffic_skeletons_return_provider_metadata(monkeypatch):
    pois = [
        POI(
            id="poi_1",
            amap_id="B0001",
            name="故宫博物院",
            city="北京",
            category="attraction",
            latitude=39.9163,
            longitude=116.3972,
            source=AMAP_PLACE_SOURCE,
        ),
        POI(
            id="poi_2",
            amap_id="B0002",
            name="北京热门景点",
            city="北京",
            category="candidate",
            latitude=39.9042,
            longitude=116.4074,
            source=AMAP_PLACE_SOURCE,
        ),
    ]

    service = RouteService(map_provider_key="test-amap-key")

    def fake_amap_route(_from_poi, _to_poi, mode):
        if mode == "transit":
            return {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "distance": "1800",
                            "duration": "900",
                            "cost": "4",
                            "segments": [
                                {
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "metro",
                                                "distance": "1800",
                                                "duration": "900",
                                                "polyline": "116.3972,39.9163;116.4074,39.9042",
                                            }
                                        ]
                                    }
                                }
                            ],
                        }
                    ]
                },
            }
        return {
            "status": "1",
            "route": {
                "taxi_cost": "28",
                "paths": [
                    {
                        "distance": "1800",
                        "duration": "800",
                        "steps": [
                            {
                                "instruction": "drive",
                                "distance": "1800",
                                "duration": "800",
                                "polyline": "116.3972,39.9163;116.4074,39.9042",
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setattr(
        service,
        "_fetch_amap_route",
        fake_amap_route,
    )
    routes = service.build_routes("plan_1", pois, "public_transit")
    weather = WeatherService(weather_provider_key="").build_weather_signal("北京", travel_purpose_tags=["拍照优先"])
    traffic = TrafficService().build_signals(routes)

    assert routes[0].source == "amap-webservice"
    assert routes[0].distance_meters == 1800
    assert routes[0].mode == "transit"
    assert routes[0].duration_minutes == 15
    assert routes[0].is_selected is True
    assert weather.source == "高德天气"
    assert weather.data_status == "degraded"
    assert weather.provider_name == "amap-weather-provider"
    assert weather.fallback_used is False
    assert weather.failure_reason
    assert weather.risk_level in {"ideal", "neutral", "risky", "unknown"}
    assert traffic[0].source == "mock-traffic-provider"
    assert traffic[0].estimated_reason


def test_route_service_returns_clear_error_when_amap_key_missing():
    pois = [
        POI(id="poi_1", name="故宫博物院", city="北京", category="attraction", latitude=39.9163, longitude=116.3972),
        POI(id="poi_2", name="北京热门景点", city="北京", category="candidate", latitude=39.9042, longitude=116.4074),
    ]

    service = RouteService(map_provider_key="")
    routes = service.build_routes("plan_1", pois, "walk")

    assert routes == []
    assert "MAP_PROVIDER_KEY" in service.warnings[0]

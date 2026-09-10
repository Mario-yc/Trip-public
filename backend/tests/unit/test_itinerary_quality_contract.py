from types import SimpleNamespace

from src.models.poi import POI
from src.services.itinerary_quality_contract import ItineraryQualityContract
from src.services.map_poi_service import AMAP_PLACE_SOURCE


def _plan(identifier: str, name: str, longitude: float, latitude: float, start: str):
    return SimpleNamespace(
        id=identifier,
        day_number=1,
        start_time=start,
        end_time=start,
        kind="visit",
        display_title=name,
        notes="",
        grounding_status="selected",
        route_anchor=True,
        transport_mode="transit",
        selected_poi=POI(
            id=identifier,
            amap_id=identifier,
            name=name,
            city="测试城市",
            category="scenic",
            type="风景名胜",
            longitude=longitude,
            latitude=latitude,
            source=AMAP_PLACE_SOURCE,
            confidence=0.95,
        ),
    )


def test_straight_line_distance_is_diagnostic_and_cannot_block_active_version():
    report = (
        ItineraryQualityContract()
        .evaluate(
            day_slots=[],
            persistable_plans=[
                _plan("B00000001", "地点甲", 116.30, 39.90, "09:00"),
                _plan("B00000002", "地点乙", 121.47, 31.23, "11:00"),
            ],
            unresolved=[],
            required_intent_coverage=[],
        )
        .to_dict()
    )

    assert report["routeQuality"]["maxLegDistanceKm"] > 18
    assert report["routeQuality"]["distanceSource"] == "straight_line_coarse_diagnostic"
    assert report["routeQuality"]["decisionRole"] == "ordering_and_diagnostics_only"
    assert report["canCreateActiveVersion"] is True
    assert not any("route_distance" in item for item in report["hardFailures"])

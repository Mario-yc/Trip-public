from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.travel_tool_registry import TravelToolRegistry


def test_normalize_operation_amap_poi_maps_common_amap_aliases_to_trusted_source():
    registry = TravelToolRegistry.__new__(TravelToolRegistry)
    operation = {
        "op": "replace_segment_poi",
        "segmentId": "seg_1",
        "amapPoi": {
            "id": "B0REAL",
            "name": "景山公园",
            "source": "amap",
            "longitude": "116.3969",
            "latitude": "39.9236",
            "confidence": "0.94",
        },
    }

    registry._normalize_operation_amap_poi(operation)

    assert operation["amapPoi"]["source"] == AMAP_PLACE_SOURCE
    assert operation["amapPoi"]["longitude"] == 116.3969
    assert operation["amapPoi"]["latitude"] == 39.9236
    assert operation["amapPoi"]["confidence"] == 0.94


def test_normalize_operation_amap_poi_still_removes_agent_text_timeline_source():
    registry = TravelToolRegistry.__new__(TravelToolRegistry)
    operation = {
        "op": "replace_segment_poi",
        "segmentId": "seg_1",
        "amapPoi": {"id": "draft", "name": "夜景观景点", "source": "agent-text-timeline"},
    }

    registry._normalize_operation_amap_poi(operation)

    assert "amapPoi" not in operation


def test_resolve_poi_preview_includes_full_poi_debug_fields():
    registry = TravelToolRegistry.__new__(TravelToolRegistry)

    preview = registry._output_preview(
        "resolve_poi",
        {
            "providerName": "amap-poi-resolution",
            "fallbackUsed": False,
            "failureReason": None,
            "confidence": 0.9,
            "resolved": [
                {
                    "query": "景山公园",
                    "poi": {
                        "id": "B0REAL",
                        "name": "景山公园",
                        "source": AMAP_PLACE_SOURCE,
                        "longitude": 116.3969,
                        "latitude": 39.9236,
                        "confidence": 0.94,
                    },
                }
            ],
            "pending": [],
        },
    )

    assert preview["resolved"][0]["poi"] == {
        "id": "B0REAL",
        "name": "景山公园",
        "source": AMAP_PLACE_SOURCE,
        "longitude": 116.3969,
        "latitude": 39.9236,
        "confidence": 0.94,
    }

from datetime import datetime, timezone

import pytest

from src.services.map_poi_service import MapPoiService


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("国家博物馆;国博", ["国家博物馆", "国博"]),
        ("国博；国家博物馆|国博", ["国博", "国家博物馆"]),
        (["国家博物馆", "国博", "国博"], ["国家博物馆", "国博"]),
        ([], []),
        (None, []),
        ({"modelSuggestedAlias": "国博"}, []),
        ([{"name": "国博"}, 42], []),
    ],
)
def test_official_alias_survives_provider_parsing_and_query_receipt(raw, expected):
    service = MapPoiService()
    poi = service._parse_poi(
        {
            "id": "B000A83M61",
            "name": "中国国家博物馆",
            "alias": raw,
            "cityname": "北京市",
            "type": "科教文化服务;博物馆",
            "location": "116.401304,39.905374",
            "address": "东长安街16号",
        },
        "scenic",
    )
    assert poi.provider_aliases == expected
    bound = service._attach_query_receipt(
        poi,
        queried_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        query_receipt_fingerprint="a" * 64,
    )
    payload = bound.model_dump(mode="json", by_alias=True)
    assert payload["providerAliases"] == expected
    assert payload["providerQueryReceiptFingerprint"] == "a" * 64
    assert payload["name"] == "中国国家博物馆"


def test_alias_evidence_is_bounded_and_never_derived_from_tags_or_source_claims():
    service = MapPoiService()
    poi = service._parse_poi(
        {
            "id": "B000A83M61", "name": "中国国家博物馆",
            "location": "116.401304,39.905374",
            "tag": "国家博物馆", "sourceClaims": [{"alias": "国博"}],
        },
        "scenic",
    )
    assert poi.provider_aliases == []
    assert service._provider_aliases("a" * 257) == []
    assert service._provider_aliases("valid;" + "a" * 8192) == []
    assert len(service._provider_aliases([f"alias{i}" for i in range(50)])) == 16

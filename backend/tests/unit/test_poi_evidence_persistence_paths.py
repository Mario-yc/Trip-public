from __future__ import annotations

import json
from types import SimpleNamespace

from src.services.agent_service import AgentService


def test_active_itinerary_reuse_preserves_structured_poi_evidence(monkeypatch) -> None:
    source_claims = [
        {
            "claimKey": "local_food",
            "stance": "support",
            "locality": "北京",
        }
    ]
    row = {
        "id": "poi_reusable_meal",
        "amap_id": "B000REUSE01",
        "name": "北京本地餐馆",
        "city": "北京",
        "category": "food",
        "latitude": 39.91,
        "longitude": 116.41,
        "photo_url": None,
        "source": "amap-place-search",
        "confidence": 0.95,
        "type": "餐饮服务;中餐厅",
        "district": "东城区",
        "address": "示例路1号",
        "source_note": "高德 WebService POI 搜索",
        "source_url": None,
        "photos_json": "[]",
        "provider_type_code": "050100",
        "tags_json": json.dumps(["地方风味", "北京菜"], ensure_ascii=False),
        "source_claims_json": json.dumps(source_claims, ensure_ascii=False),
    }
    service = AgentService.__new__(AgentService)

    hydrated = service._poi_from_row(row)
    monkeypatch.setattr(service, "_active_poi_matches_intent", lambda *_args: True)
    reused = service._matching_reusable_poi(
        SimpleNamespace(),
        [hydrated],
        SimpleNamespace(),
    )

    assert hydrated.provider_type_code == "050100"
    assert hydrated.tags == ["地方风味", "北京菜"]
    assert hydrated.source_claims == source_claims
    assert reused is not None
    assert reused.provider_type_code == "050100"
    assert reused.tags == ["地方风味", "北京菜"]
    assert reused.source_claims == source_claims

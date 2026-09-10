from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.services.experience_independence_service import ExperienceIndependenceService


def poi(**overrides):
    values = {
        "id": "B000000002",
        "name": "示例公园",
        "type": "风景名胜;公园广场;公园",
        "provider_type_code": "110101",
        "address": "城市道路 1 号",
        "parent_poi_id": None,
        "indoor_parent_poi_id": None,
        "distance_meters": 800.0,
        "provider_queried_at": "2026-08-24T08:00:00+00:00",
        "provider_query_receipt_fingerprint": "f" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def anchor(**overrides):
    values = {
        "amap_id": "B000000001",
        "id": "poi_local_anchor",
        "name": "示例大学",
        "parent_poi_id": None,
        "indoor_parent_poi_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parent_or_indoor_parent_matching_day_anchor_is_embedded() -> None:
    by_parent = ExperienceIndependenceService.evaluate(poi(parent_poi_id="B000000001"), anchor())
    by_cpid = ExperienceIndependenceService.evaluate(poi(indoor_parent_poi_id="B000000001"), anchor())

    assert by_parent["status"] == "embedded_in_day_anchor"
    assert by_cpid["status"] == "embedded_in_day_anchor"
    assert by_parent["physicalGroupId"] == "B000000001"


def test_provider_address_inside_anchor_is_embedded_without_named_blacklist() -> None:
    result = ExperienceIndependenceService.evaluate(
        poi(address="示例大学内东侧"),
        anchor(),
    )

    assert result["status"] == "embedded_in_day_anchor"
    assert result["evidence"]["addressContainmentMatched"] is True


def test_generic_scenic_candidate_near_anchor_is_pending_not_falsely_proven_embedded() -> None:
    result = ExperienceIndependenceService.evaluate(
        poi(type="风景名胜", provider_type_code="110000", distance_meters=228.0),
        anchor(),
    )

    assert result["status"] == "independence_pending"
    assert "standalone_park_category_missing" in result["reasonCodes"]
    assert "distance_is_not_parent_evidence" in result["reasonCodes"]


def test_versioned_provider_category_and_no_parent_conflict_verifies_standalone_park() -> None:
    result = ExperienceIndependenceService.evaluate(poi(), anchor())

    assert result["status"] == "standalone_verified"
    evidence = result["evidence"]
    assert evidence["providerCategorySourceFingerprint"]
    assert evidence["providerCategorySourceArtifactFingerprint"] == (
        "6fc13305a1cf8dead9b96a2679807b7861ab6a22a1b709c2a04a5b318c1f7422"
    )
    assert evidence["providerCategorySourceVersion"] == "V1.06_20230208"
    assert evidence["providerCategoryDecision"] == "accepted"
    assert evidence["providerQueryReceiptFingerprint"] == "f" * 64
    assert evidence["providerQueriedAt"] == "2026-08-24T08:00:00+00:00"
    assert result["physicalGroupId"] == "B000000002"


def test_nonempty_unrelated_typecode_with_park_text_is_not_category_evidence() -> None:
    result = ExperienceIndependenceService.evaluate(
        poi(provider_type_code="110200"),
        anchor(),
    )

    assert result["status"] == "independence_pending"
    assert result["evidence"]["providerCategoryDecision"] == "unrecognized"
    assert "standalone_park_category_missing" in result["reasonCodes"]


def test_provider_category_code_and_type_path_must_match_the_same_versioned_row() -> None:
    result = ExperienceIndependenceService.evaluate(
        poi(provider_type_code="110103", type="风景名胜;公园广场;公园"),
        anchor(),
    )

    assert result["status"] == "independence_pending"
    assert result["evidence"]["providerCategoryDecision"] == "unrecognized"


def test_provider_internal_facility_and_ticket_office_categories_are_rejected() -> None:
    internal = ExperienceIndependenceService.evaluate(
        poi(
            name="园区内部设施",
            provider_type_code="110106",
            type="风景名胜;公园广场;公园内部设施",
        ),
        anchor(),
    )
    ticket_office = ExperienceIndependenceService.evaluate(
        poi(
            name="景区接待点",
            provider_type_code="070306",
            type="生活服务;售票处;公园景点售票处",
        ),
        anchor(),
    )

    assert internal["status"] == "independence_pending"
    assert ticket_office["status"] == "independence_pending"
    assert internal["evidence"]["providerCategoryDecision"] == "rejected"
    assert ticket_office["evidence"]["providerCategoryDecision"] == "rejected"
    assert "standalone_park_category_rejected" in internal["reasonCodes"]
    assert "standalone_park_category_rejected" in ticket_office["reasonCodes"]


def test_missing_provider_receipt_or_queried_at_fails_closed() -> None:
    missing_receipt = ExperienceIndependenceService.evaluate(
        poi(provider_query_receipt_fingerprint=None),
        anchor(),
    )
    missing_time = ExperienceIndependenceService.evaluate(
        poi(provider_queried_at=None),
        anchor(),
    )

    assert missing_receipt["status"] == "independence_pending"
    assert missing_time["status"] == "independence_pending"
    assert "provider_query_receipt_missing_or_invalid" in missing_receipt["reasonCodes"]
    assert "provider_queried_at_missing_or_invalid" in missing_time["reasonCodes"]


def test_category_evidence_asset_is_generic_versioned_and_attributable() -> None:
    path = Path(ExperienceIndependenceService.CATEGORY_EVIDENCE_ASSET)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schemaVersion"] == "provider-category-evidence-v1"
    assert payload["provider"] == "amap-place-search"
    assert payload["source"]["contentVersion"] == "V1.06_20230208"
    assert len(payload["source"]["artifactSha256"]) == 64
    assert payload["source"]["url"] == "https://lbs.amap.com/api/webservice/download"
    assert set(payload["experienceRoles"]) == {"standalone_park"}


def test_child_facility_cannot_become_standalone_park() -> None:
    result = ExperienceIndependenceService.evaluate(
        poi(name="示例公园售票处", type="风景名胜;公园广场;售票处", provider_type_code="110101"),
        anchor(),
    )

    assert result["status"] == "independence_pending"
    assert "attached_facility_conflict" in result["reasonCodes"]


def test_paused_attached_facility_cannot_pass_even_with_park_category_evidence() -> None:
    result = ExperienceIndependenceService.evaluate(
        poi(
            name="示例公园游客中心(暂停营业)",
            type="风景名胜;公园广场;公园",
            provider_type_code="110101",
            business_status="暂停营业",
        ),
        anchor(),
    )

    assert result["status"] == "independence_pending"
    assert "attached_facility_conflict" in result["reasonCodes"]
    assert "provider_business_status_not_open" in result["reasonCodes"]

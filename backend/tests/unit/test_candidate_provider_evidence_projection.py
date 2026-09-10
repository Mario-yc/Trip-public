from __future__ import annotations

from datetime import datetime, timezone

from src.services.candidate_provider_evidence_service import (
    CandidateProviderEvidenceService,
)


def test_provider_evidence_projection_preserves_parent_query_and_business_facts():
    queried_at = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    candidate = {
        "amapId": "b000000001",
        "parentPoiId": "B000000099",
        "indoor_parent_poi_id": "B000000088",
        "providerTypeCode": "110200",
        "business_status": "营业中",
        "provider_queried_at": queried_at,
        "provider_query_receipt_fingerprint": "A" * 64,
    }

    projected = CandidateProviderEvidenceService.project(
        candidate,
        include_missing=False,
    )

    assert projected == {
        "amapId": "B000000001",
        "providerTypeCode": "110200",
        "parentPoiId": "B000000099",
        "indoorParentPoiId": "B000000088",
        "businessStatus": "营业中",
        "providerQueryReceiptFingerprint": "a" * 64,
        "providerQueriedAt": "2026-08-24T08:00:00+00:00",
    }


def test_provider_evidence_materialization_keeps_replay_facts_and_rejects_invalid_receipt():
    payload = CandidateProviderEvidenceService.materialize_poi(
        {
            "amapId": "B000000001",
            "name": "独立公园",
            "parentPoiId": "B000000099",
            "indoorParentPoiId": "B000000088",
            "providerTypeCode": "110200",
            "businessStatus": "营业中",
            "providerQueriedAt": "2026-08-24T08:00:00+00:00",
            "providerQueryReceiptFingerprint": "not-a-provider-receipt",
        },
        local_id="poi-local",
    )

    assert payload["parentPoiId"] == "B000000099"
    assert payload["indoorParentPoiId"] == "B000000088"
    assert payload["providerTypeCode"] == "110200"
    assert payload["businessStatus"] == "营业中"
    assert payload["providerQueriedAt"] == "2026-08-24T08:00:00+00:00"
    assert "providerQueryReceiptFingerprint" not in payload

    replayed = CandidateProviderEvidenceService.project(
        payload,
        include_missing=False,
    )
    assert replayed["indoorParentPoiId"] == "B000000088"
    assert replayed["providerQueriedAt"] == "2026-08-24T08:00:00+00:00"
    assert "providerQueryReceiptFingerprint" not in replayed

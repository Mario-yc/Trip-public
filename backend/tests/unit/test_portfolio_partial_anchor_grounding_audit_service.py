from __future__ import annotations

import pytest

from src.services.portfolio_partial_anchor_grounding_audit_service import (
    PortfolioPartialAnchorGroundingAuditService,
)


def _anchor(
    *,
    segment_id: str = "segment_a",
    brief_id: str = "brief_a",
    pool_id: str = "pool_a",
    slot_id: str = "slot_a",
    day_number: int = 1,
    goal_id: str = "goal_campus",
    amap_id: str = "B000SAFE",
    source: str = "amap-place-search",
    longitude: float = 116.3,
    latitude: float = 39.9,
) -> dict:
    return {
        "id": segment_id,
        "kind": "visit",
        "poi": {
            "amapId": amap_id,
            "source": source,
            "longitude": longitude,
            "latitude": latitude,
        },
        "semanticMetadata": {
            "routeAnchor": True,
            "creativeBriefId": brief_id,
            "poolId": pool_id,
            "planningSlotId": slot_id,
            "sourceGoalId": goal_id,
        },
        "_dayNumber": day_number,
    }


def _snapshot(anchor: dict) -> dict:
    item = dict(anchor)
    day_number = int(item.pop("_dayNumber"))
    return {"days": [{"dayNumber": day_number, "segments": [item]}]}


def _grounding(**overrides) -> dict:
    candidate = {
        "amapId": "B000SAFE",
        "source": "amap-place-search",
        "longitude": 116.3,
        "latitude": 39.9,
        "briefId": "brief_a",
        "poolId": "pool_a",
        "planningSlotId": "slot_a",
        "dayNumber": 1,
        "sourceGoalId": "goal_campus",
    }
    candidate.update(overrides)
    return {
        "poolReports": [
            {
                "briefId": "brief_a",
                "poolId": "pool_a",
                "goalId": "goal_campus",
                "safeCandidates": [candidate],
            }
        ]
    }


def test_grounding_audit_matches_full_scoped_anchor_identity():
    audit = PortfolioPartialAnchorGroundingAuditService().audit(
        _snapshot(_anchor()),
        _grounding(),
        focus_brief_id="brief_a",
    )

    assert audit.anchor_count == 1
    assert audit.matched_anchor_count == 1
    assert audit.unmatched_anchor_count == 0
    assert audit.reason_codes == []
    assert audit.partial_eligible is True
    assert audit.write_attempted is False
    assert audit.version_delta == 0
    assert audit.patch_delta == 0
    assert audit.route_write_delta == 0


@pytest.mark.parametrize(
    ("anchor", "grounding", "reason"),
    [
        (_anchor(slot_id=""), _grounding(), "partial_anchor_lineage_missing"),
        (_anchor(day_number=2), _grounding(), "partial_anchor_scope_mismatch"),
        (_anchor(amap_id=""), _grounding(), "partial_anchor_amap_identity_missing"),
        (_anchor(source="web-search"), _grounding(), "partial_anchor_source_invalid"),
        (_anchor(longitude=116.31), _grounding(), "partial_anchor_coordinate_mismatch"),
        (_anchor(amap_id="B000OTHER"), _grounding(), "partial_anchor_not_in_server_grounding"),
    ],
)
def test_grounding_audit_reports_specific_reason_codes(anchor, grounding, reason):
    audit = PortfolioPartialAnchorGroundingAuditService().audit(
        _snapshot(anchor),
        grounding,
        focus_brief_id="brief_a",
    )

    assert audit.partial_eligible is False
    assert audit.unmatched_anchor_count == 1
    assert audit.reason_codes == [reason]


def test_route_repair_grounding_evidence_is_rebound_before_audit():
    repaired = _anchor(amap_id="B000REPAIR", longitude=116.42, latitude=39.98)
    snapshot = _snapshot(repaired)
    snapshot["portfolioRouteQuality"] = {
        "candidateRepairs": [
            {
                "segmentId": "segment_a",
                "groundingEvidence": {
                    "creativeBriefId": "brief_a",
                    "poolId": "pool_a",
                    "planningSlotId": "slot_a",
                    "dayNumber": 1,
                    "sourceGoalId": "goal_campus",
                    "amapId": "B000REPAIR",
                    "source": "amap-place-search",
                    "longitude": 116.42,
                    "latitude": 39.98,
                },
            }
        ]
    }

    merged = PortfolioPartialAnchorGroundingAuditService().merge_route_repair_evidence(_grounding(), snapshot)
    audit = PortfolioPartialAnchorGroundingAuditService().audit(snapshot, merged, focus_brief_id="brief_a")

    assert merged["routeRepairGroundingEvidence"][0]["amapId"] == "B000REPAIR"
    assert audit.partial_eligible is True


def test_optimizer_grounding_evidence_is_rebound_to_exact_snapshot_lineage():
    snapshot = _snapshot(_anchor(amap_id="B000OPT", longitude=116.42, latitude=39.98))
    grounding = _grounding(amapId="B000OTHER")
    optimizer_evidence = [
        {
            "id": "B000OPT",
            "source": "amap-place-search",
            "longitude": 116.42,
            "latitude": 39.98,
            "briefId": "brief_a",
            "poolId": "pool_a",
            "planningSlotId": "slot_a",
            "dayNumber": 1,
        }
    ]
    service = PortfolioPartialAnchorGroundingAuditService()

    merged = service.merge_route_repair_evidence(
        grounding,
        snapshot,
        candidate_evidence=optimizer_evidence,
    )
    audit = service.audit(snapshot, merged, focus_brief_id="brief_a")

    rebound = next(item for item in merged["routeRepairGroundingEvidence"] if item["amapId"] == "B000OPT")
    assert rebound["sourceGoalId"] == "goal_campus"
    assert audit.partial_eligible is True


def test_optimizer_grounding_evidence_cannot_cross_brief_or_ambiguous_lineage():
    snapshot = _snapshot(_anchor(amap_id="B000OPT", longitude=116.42, latitude=39.98))
    service = PortfolioPartialAnchorGroundingAuditService()

    merged = service.merge_route_repair_evidence(
        _grounding(amapId="B000OTHER"),
        snapshot,
        candidate_evidence=[
            {
                "id": "B000OPT",
                "source": "amap-place-search",
                "longitude": 116.42,
                "latitude": 39.98,
                "briefId": "brief_other",
                "poolId": "pool_a",
                "planningSlotId": "slot_a",
                "dayNumber": 1,
            }
        ],
    )
    audit = service.audit(snapshot, merged, focus_brief_id="brief_a")

    assert not merged["routeRepairGroundingEvidence"]
    assert audit.partial_eligible is False
    assert audit.reason_codes == ["partial_anchor_not_in_server_grounding"]


def test_unproven_route_repair_anchor_remains_ineligible():
    repaired = _anchor(amap_id="B000REPAIR", longitude=116.42, latitude=39.98)
    audit = PortfolioPartialAnchorGroundingAuditService().audit(
        _snapshot(repaired), _grounding(), focus_brief_id="brief_a"
    )

    assert audit.partial_eligible is False
    assert audit.reason_codes == ["partial_anchor_not_in_server_grounding"]


def test_unproven_repair_anchor_is_removed_and_represented_only_as_pending_metadata():
    valid = _anchor()
    invalid = _anchor(
        segment_id="segment_repair",
        slot_id="slot_repair",
        goal_id="goal_night",
        amap_id="B000UNPROVEN",
        longitude=116.45,
        latitude=39.95,
    )
    valid_segment = dict(valid)
    invalid_segment = dict(invalid)
    valid_segment.pop("_dayNumber")
    invalid_segment.pop("_dayNumber")
    snapshot = {
        "days": [{"dayNumber": 1, "segments": [valid_segment, invalid_segment]}],
        "portfolioPendingSlots": [],
    }
    service = PortfolioPartialAnchorGroundingAuditService()

    audit = service.audit(snapshot, _grounding(), focus_brief_id="brief_a")
    projected = service.project_verified_snapshot(
        snapshot,
        audit,
    )

    assert projected is not None
    assert [item["poi"]["amapId"] for item in projected["days"][0]["segments"]] == ["B000SAFE"]
    assert len(projected["portfolioPendingSlots"]) == 1
    pending = projected["portfolioPendingSlots"][0]
    assert {
        key: pending[key]
        for key in (
            "briefId",
            "poolId",
            "planningSlotId",
            "dayNumber",
            "sourceGoalId",
            "state",
        )
    } == {
        "briefId": "brief_a",
        "poolId": "pool_a",
        "planningSlotId": "slot_repair",
        "dayNumber": 1,
        "sourceGoalId": "goal_night",
        "state": "pending",
    }
    assert pending["reason"] == "grounding_projection_mismatch"

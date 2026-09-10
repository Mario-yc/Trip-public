from __future__ import annotations

import copy

import pytest

from src.services.portfolio_pending_slot_service import (
    PortfolioPendingSlotError,
    PortfolioPendingSlotService,
)


def _snapshot() -> dict:
    return {
        "status": "partial",
        "creativeBrief": {"briefId": "brief_focus"},
        "portfolioSelectionContext": {
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": "portfolio_root",
            "focusBriefId": "brief_focus",
            "requestContractFingerprint": "contract_fp",
        },
        "portfolioPendingSlots": [
            {
                "id": "pending_walk",
                "briefId": "brief_focus",
                "poolId": "pool_walk",
                "planningSlotId": "slot_walk",
                "dayNumber": 1,
                "timeWindow": "14:00-16:00",
                "startTime": "14:00",
                "endTime": "16:00",
                "durationMinutes": 120,
                "intentType": "area_walk",
                "sourceGoalId": "goal_walk",
                "rawNeed": "街区漫步",
                "kind": "activity",
                "state": "pending",
            },
            {
                "id": "pending_night",
                "briefId": "brief_focus",
                "poolId": "pool_night",
                "planningSlotId": "slot_night",
                "dayNumber": 1,
                "timeWindow": "20:00-21:15",
                "startTime": "20:00",
                "endTime": "21:15",
                "durationMinutes": 75,
                "intentType": "night_view",
                "sourceGoalId": "goal_night",
                "rawNeed": "夜景观景点",
                "kind": "activity",
                "state": "pending",
            },
        ],
        "portfolioPartialTimeline": {"status": "partial", "pendingSlotCount": 2},
        "days": [
            {
                "id": "day_1",
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "seg_campus",
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "semanticMetadata": {
                            "creativeBriefId": "brief_focus",
                            "planningSlotId": "slot_campus",
                        },
                        "poi": {"amapId": "B_CAMPUS"},
                    }
                ],
            }
        ],
    }


def test_reconcile_removes_only_exact_covered_slot_and_updates_count() -> None:
    snapshot = _snapshot()
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_walk",
            "startTime": "14:00",
            "endTime": "16:00",
            "semanticMetadata": {
                "creativeBriefId": "brief_focus",
                "poolId": "pool_walk",
                "planningSlotId": "slot_walk",
                "selectedAmapId": "B_WALK",
            },
            "poi": {"amapId": "B_WALK"},
        }
    )

    reconciled = PortfolioPendingSlotService.reconcile(snapshot)

    assert [item["planningSlotId"] for item in reconciled["portfolioPendingSlots"]] == [
        "slot_night"
    ]
    assert reconciled["portfolioPartialTimeline"]["pendingSlotCount"] == 1
    assert snapshot["portfolioPendingSlots"][0]["planningSlotId"] == "slot_walk"


def test_reconcile_does_not_remove_same_intent_or_name_without_exact_slot() -> None:
    snapshot = _snapshot()
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_other_walk",
            "semanticMetadata": {
                "creativeBriefId": "brief_focus",
                "planningSlotId": "slot_other",
                "intentType": "area_walk",
            },
            "poi": {"amapId": "B_WALK", "name": "街区漫步"},
        }
    )

    reconciled = PortfolioPendingSlotService.reconcile(snapshot)

    assert len(reconciled["portfolioPendingSlots"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("briefId", "brief_other"),
        ("poolId", ""),
        ("planningSlotId", ""),
        ("dayNumber", 0),
    ],
)
def test_reconcile_rejects_cross_scope_or_empty_pending_identity(field: str, value: object) -> None:
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"][0][field] = value

    with pytest.raises(PortfolioPendingSlotError):
        PortfolioPendingSlotService.reconcile(snapshot)


def test_reconcile_rejects_pending_slot_for_day_missing_from_timeline() -> None:
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"][0]["dayNumber"] = 2

    with pytest.raises(PortfolioPendingSlotError) as raised:
        PortfolioPendingSlotService.reconcile(snapshot)

    assert raised.value.code == "portfolio_pending_slot_day_missing"


def test_find_exact_slot_requires_frozen_selection_root() -> None:
    snapshot = _snapshot()
    command = {
        **copy.deepcopy(snapshot["portfolioSelectionContext"]),
        "briefId": "brief_focus",
        "poolId": "pool_walk",
        "planningSlotId": "slot_walk",
        "dayNumber": 1,
    }

    slot = PortfolioPendingSlotService.find_exact_slot(snapshot, command)
    assert slot["startTime"] == "14:00"

    command["focusBriefId"] = "brief_other"
    with pytest.raises(PortfolioPendingSlotError):
        PortfolioPendingSlotService.find_exact_slot(snapshot, command)


def test_reconcile_marks_timeline_completed_when_last_slot_is_covered() -> None:
    snapshot = _snapshot()
    snapshot["days"][0]["segments"].extend(
        [
            {
                "id": "seg_walk",
                "semanticMetadata": {
                    "creativeBriefId": "brief_focus",
                    "poolId": "pool_walk",
                    "planningSlotId": "slot_walk",
                },
                "poi": {"amapId": "B_WALK"},
            },
            {
                "id": "seg_night",
                "semanticMetadata": {
                    "creativeBriefId": "brief_focus",
                    "poolId": "pool_night",
                    "planningSlotId": "slot_night",
                },
                "poi": {"amapId": "B_NIGHT"},
            },
        ]
    )

    reconciled = PortfolioPendingSlotService.reconcile(snapshot)

    assert reconciled["portfolioPendingSlots"] == []
    assert reconciled["portfolioPartialTimeline"] == {
        "status": "partial",
        "pendingSlotCount": 0,
        "pendingSlotsStatus": "completed",
    }
    assert reconciled["status"] == "partial"

    snapshot["portfolioPartialTimeline"]["strictProposalVerifierPassed"] = True
    verified = PortfolioPendingSlotService.reconcile(snapshot)
    assert verified["portfolioPartialTimeline"]["status"] == "completed"
    assert verified["status"] == "completed"
@pytest.mark.parametrize("scope_source", ["segment", "required_binding"])
def test_reconcile_rejects_cross_brief_persisted_lineage(scope_source: str) -> None:
    snapshot = _snapshot()
    if scope_source == "segment":
        snapshot["days"][0]["segments"][0]["semanticMetadata"]["creativeBriefId"] = (
            "brief_other"
        )
    else:
        snapshot["portfolioRequiredCandidateBindings"] = [
            {"briefId": "brief_other", "planningSlotId": "slot_campus"}
        ]

    with pytest.raises(PortfolioPendingSlotError):
        PortfolioPendingSlotService.reconcile(snapshot)

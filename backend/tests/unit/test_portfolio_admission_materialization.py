from types import SimpleNamespace

from src.services.creative_portfolio_staging_service import (
    CreativePortfolioStagingService,
)


def _candidate(index: int) -> dict:
    return {
        "briefId": "brief_1",
        "poolId": f"pool_{index}",
        "planningSlotId": f"slot_{index}",
        "dayNumber": 1 if index <= 4 else 2,
        "amapId": f"B000000{index:02d}",
        "source": "amap-place-search",
        "rawNeed": "夜景" if index > 4 else "高校",
        "intentType": "night_view" if index > 4 else "campus_visit",
        "requirementLevel": "required",
        "consumerAdmissionReport": {"scoreEligible": True},
        "scoreEligible": True,
    }


def test_selected_seven_but_materialized_four_becomes_three_explicit_pending_slots():
    candidates = [_candidate(index) for index in range(1, 8)]
    snapshot = {
        "days": [
            {
                "dayNumber": day_number,
                "segments": [
                    {
                        "id": f"seg_{index}",
                        "poi": {
                            "amapId": candidate["amapId"],
                            "source": "amap-place-search",
                        },
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "creativeBriefId": "brief_1",
                            "poolId": candidate["poolId"],
                            "planningSlotId": candidate["planningSlotId"],
                            "consumerAdmissionReport": {"scoreEligible": True},
                        },
                    }
                    for index, candidate in enumerate(candidates[:4], start=1)
                    if candidate["dayNumber"] == day_number
                ],
            }
            for day_number in (1, 2)
        ],
        "portfolioPendingSlots": [],
    }
    skeleton = SimpleNamespace(
        brief=SimpleNamespace(brief_id="brief_1"),
        day_slots=[
            SimpleNamespace(
                slot_id=f"slot_{index}",
                requirement_level="required",
                time_window="18:00-20:00",
                start_time="18:00",
                duration_minutes=120,
            )
            for index in range(1, 8)
        ],
    )

    projected = CreativePortfolioStagingService._reconcile_admitted_anchor_materialization(
        snapshot,
        admitted_candidates=candidates,
        skeleton=skeleton,
        admission_enforced=True,
    )

    audit = projected["portfolioAdmissionMaterializationAudit"]
    assert audit["selectedAdmittedCandidateCount"] == 7
    assert audit["actualProposalAnchorCount"] == 4
    assert audit["actualAdmissionReportCount"] == 4
    assert audit["materializationMissingCount"] == 3
    assert audit["countInvariantPassed"] is True
    assert [item["planningSlotId"] for item in projected["portfolioPendingSlots"]] == [
        "slot_5",
        "slot_6",
        "slot_7",
    ]
    assert all(item["reason"] == "admitted_candidate_not_materialized" for item in projected["portfolioPendingSlots"])

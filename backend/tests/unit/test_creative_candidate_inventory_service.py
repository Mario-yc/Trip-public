from src.services.creative_candidate_inventory_service import (
    CreativeCandidateInventoryService,
)


def _candidate(*, eligible: bool = True) -> dict:
    return {
        "amapId": "B0000INV01",
        "name": "朝阳艺术空间",
        "longitude": 116.49,
        "latitude": 39.98,
        "district": "朝阳区",
        "consumerAdmissionReport": {
            "scoreEligible": eligible,
            "evidenceFingerprint": "f" * 64,
        },
    }


def test_inventory_indexes_only_score_eligible_admitted_candidates():
    admitted = CreativeCandidateInventoryService.capture(
        _candidate(),
        family="art_walk",
        intent_type="area_walk",
        day_number=1,
        time_window="afternoon",
        brief_id="brief_art",
        pool_id="pool_art",
        planning_slot_id="slot_art",
    )
    rejected = CreativeCandidateInventoryService.capture(
        _candidate(eligible=False),
        family="art_walk",
        intent_type="area_walk",
        day_number=1,
        time_window="afternoon",
        brief_id="brief_art",
        pool_id="pool_art",
        planning_slot_id="slot_art",
    )
    inventory = CreativeCandidateInventoryService.build([item for item in (admitted, rejected) if item is not None])

    assert rejected is None
    assert inventory["scoreEligibleCount"] == 1
    assert inventory["indexes"]["byIntent"] == {"area_walk": ["B0000INV01"]}
    assert inventory["indexes"]["byDay"] == {"1": ["B0000INV01"]}
    assert inventory["indexes"]["byTime"] == {"afternoon": ["B0000INV01"]}
    assert inventory["indexes"]["byArea"] == {"朝阳区": ["B0000INV01"]}
    assert inventory["indexes"]["byThemeFamily"] == {"art_walk": ["B0000INV01"]}


def test_inventory_merge_is_bounded_and_deduplicated():
    item = CreativeCandidateInventoryService.capture(
        _candidate(),
        family="art_walk",
        intent_type="area_walk",
        day_number=1,
        time_window="afternoon",
        brief_id="brief_art",
        pool_id="pool_art",
        planning_slot_id="slot_art",
    )
    first = CreativeCandidateInventoryService.build([item])
    merged = CreativeCandidateInventoryService.merge(first, first)

    assert merged["scoreEligibleCount"] == 1
    assert merged["areaClusterCount"] == 1

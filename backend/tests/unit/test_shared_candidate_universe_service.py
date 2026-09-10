from src.services.shared_candidate_universe_service import SharedCandidateUniverseBuilder


def _candidate(identity):
    return {"amapId": identity, "longitude": 116.3, "latitude": 39.9, "providerType": "AMAP", "semanticPassed": True}


def test_builder_dedupes_same_query_and_same_entity_without_dropping_other_intents():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "city": "北京",
                "intentType": "museum",
                "rawNeed": "美术馆",
                "searchMode": "text",
                "candidates": [_candidate("m1")],
            },
            {
                "city": "北京",
                "intentType": "museum",
                "rawNeed": "美术馆",
                "searchMode": "text",
                "candidates": [_candidate("m2")],
            },
            {
                "city": "北京",
                "intentType": "campus_visit",
                "rawNeed": "985",
                "searchMode": "text",
                "candidates": [_candidate("c1"), _candidate("c1")],
            },
        ]
    )
    assert universe.unique_query_count == 2
    assert universe.deduped_query_count == 1
    assert [item["amapId"] for item in universe.pools["museum"]] == ["m1", "m2"]
    assert [item["amapId"] for item in universe.pools["campus_visit"]] == ["c1"]


def test_ungrounded_candidate_never_enters_universe():
    universe = SharedCandidateUniverseBuilder().build(
        [{"intentType": "museum", "rawNeed": "美术馆", "candidates": [{"amapId": "bad", "semanticPassed": True}]}]
    )
    assert universe.pools == {"museum": []}


def test_existing_safe_candidate_evidence_is_normalized_without_accepting_raw_candidates():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "intentType": "museum",
                "rawNeed": "美术馆",
                "safeCandidates": [
                    {
                        "id": "amap-safe",
                        "longitude": 116.3,
                        "latitude": 39.9,
                        "type": "美术馆",
                        "sourceGoalId": "goal_museum",
                    }
                ],
            },
        ]
    )
    assert universe.pools["museum"][0]["amapId"] == "amap-safe"
    assert universe.pools["museum"][0]["sourcePrecheck"]["passed"] is True
    assert "semanticPassed" not in universe.pools["museum"][0]


def test_shared_evidence_strips_target_consumer_authorization():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "briefId": "source_brief",
                "poolId": "source_pool",
                "intentType": "area_walk",
                "rawNeed": "本地生活",
                "safeCandidates": [
                    {
                        **_candidate("shared-1"),
                        "consumerAdmission": {"classification": "admitted_final_anchor"},
                        "scoreEligible": True,
                        "sourceNote": "target-only semantic note",
                    }
                ],
            }
        ]
    )

    shared = universe.pools["area_walk"][0]
    assert shared["sourcePrecheck"]["passed"] is True
    assert "semanticPassed" not in shared
    assert "consumerAdmission" not in shared
    assert "scoreEligible" not in shared
    assert "sourceNote" not in shared


def test_identity_eligible_source_evidence_is_shared_without_inheriting_semantic_approval():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "briefId": "market_brief",
                "poolId": "market_pool",
                "intentType": "area_walk",
                "rawNeed": "市井市场",
                "sourceCandidates": [
                    {
                        **_candidate("art-from-broad-query"),
                        "sourceEvidenceEligible": True,
                        "semanticPassed": False,
                        "scoreEligible": False,
                        "consumerAdmission": {"decision": "rejected_for_market"},
                    }
                ],
            }
        ]
    )

    shared = universe.pools["area_walk"][0]
    assert shared["amapId"] == "art-from-broad-query"
    assert shared["sourcePrecheck"]["scope"] == "amap_identity_and_provider_facts"
    assert "semanticPassed" not in shared
    assert "scoreEligible" not in shared
    assert "consumerAdmission" not in shared


def test_optional_safe_candidate_does_not_require_a_hard_goal_id():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "intentType": "area_walk",
                "rawNeed": "历史街区",
                "safeCandidates": [
                    {
                        "id": "optional-safe",
                        "longitude": 116.3,
                        "latitude": 39.9,
                        "type": "风景名胜;街区",
                    }
                ],
            }
        ]
    )
    assert [item["amapId"] for item in universe.pools["area_walk"]] == ["optional-safe"]


def test_duplicate_query_reports_merge_late_grounded_candidates_without_recounting_query():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {"city": "北京", "intentType": "area_walk", "rawNeed": "街区", "searchMode": "text", "safeCandidates": []},
            {
                "city": "北京",
                "intentType": "area_walk",
                "rawNeed": "街区",
                "searchMode": "text",
                "safeCandidates": [
                    {
                        "id": "late-safe",
                        "longitude": 116.3,
                        "latitude": 39.9,
                        "type": "风景名胜;街区",
                        "sourceGoalId": "goal_optional",
                    }
                ],
            },
        ]
    )
    assert universe.unique_query_count == 1
    assert universe.deduped_query_count == 1
    assert [item["amapId"] for item in universe.pools["area_walk"]] == ["late-safe"]


def test_reused_query_retains_pool_slot_and_day_supply_identity():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "poolId": "day_1_museum_pool",
                "intentType": "museum",
                "rawNeed": "博物馆",
                "requiredSlotIds": ["day_1_museum_slot"],
                "slotDayNumbers": {"day_1_museum_slot": 1},
                "safeCandidates": [_candidate("museum-1")],
            },
            {
                "poolId": "day_2_museum_pool",
                "intentType": "museum",
                "rawNeed": "博物馆",
                "requiredSlotIds": ["day_2_museum_slot"],
                "slotDayNumbers": {"day_2_museum_slot": 2},
                "safeCandidates": [_candidate("museum-1")],
            },
        ]
    )

    assert [item["planningSlotId"] for item in universe.candidates_for_pool("day_1_museum_pool", "museum")] == [
        "day_1_museum_slot"
    ]
    day_2 = universe.candidates_for_pool("day_2_museum_pool", "museum")
    assert [(item["planningSlotId"], item["dayNumber"]) for item in day_2] == [("day_2_museum_slot", 2)]


def test_same_pool_id_in_different_briefs_keeps_scoped_supply_identity():
    universe = SharedCandidateUniverseBuilder().build(
        [
            {
                "briefId": "brief_a",
                "poolId": "shared_meal_pool",
                "intentType": "meal",
                "rawNeed": "当地午餐",
                "requiredSlotIds": ["brief_a_meal"],
                "slotDayNumbers": {"brief_a_meal": 1},
                "safeCandidates": [_candidate("meal-a")],
            },
            {
                "briefId": "brief_b",
                "poolId": "shared_meal_pool",
                "intentType": "meal",
                "rawNeed": "当地午餐",
                "requiredSlotIds": ["brief_b_meal"],
                "slotDayNumbers": {"brief_b_meal": 2},
                "safeCandidates": [_candidate("meal-b")],
            },
        ]
    )

    brief_a = universe.candidates_for_pool("shared_meal_pool", "meal", "brief_a")
    brief_b = universe.candidates_for_pool("shared_meal_pool", "meal", "brief_b")

    assert brief_a[0]["amapId"] == "meal-a"
    assert brief_a[0]["planningSlotId"] == "brief_a_meal"
    assert brief_a[0]["briefId"] == "brief_a"
    assert brief_b[0]["amapId"] == "meal-b"
    assert brief_b[0]["planningSlotId"] == "brief_b_meal"
    assert brief_b[0]["briefId"] == "brief_b"
    assert {item["amapId"] for item in brief_a} == {"meal-a", "meal-b"}
    assert {item["amapId"] for item in brief_b} == {"meal-a", "meal-b"}

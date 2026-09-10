import copy

from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.creative_proposal_title_service import CreativeProposalTitleService


def _snapshot():
    return {
        "id": "plan_1",
        "title": "北京两日游",
        "city": "北京",
        "budgetTier": "medium",
        "budgetEstimate": 680,
        "portfolioSelectionContext": {"focusBriefId": "brief_1"},
        "days": [
            {
                "id": "day_1",
                "dayNumber": 1,
                "title": "Day 1",
                "segments": [
                    {
                        "id": "seg_real",
                        "title": "清华大学",
                        "kind": "visit",
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "poi": {
                            "id": "B000A8UIN8",
                            "amapId": "B000A8UIN8",
                            "name": "清华大学",
                            "source": "amap-place-search",
                            "latitude": 40.0,
                            "longitude": 116.3,
                        },
                        "semanticMetadata": {"creativeBriefId": "brief_1"},
                    },
                    {
                        "id": "seg_fake",
                        "title": "虚构地点",
                        "kind": "visit",
                        "startTime": "14:00",
                        "endTime": "16:00",
                        "poi": {
                            "id": "fake",
                            "name": "虚构地点",
                            "source": "amap-fake",
                            "latitude": 40.1,
                            "longitude": 116.4,
                        },
                    },
                ],
            },
            {"id": "day_2", "dayNumber": 2, "title": "Day 2", "segments": []},
        ],
        "routeOptions": [
            {
                "id": "route_real",
                "fromSegmentId": "seg_real",
                "toSegmentId": "seg_real",
                "durationMinutes": 5,
                "distanceKm": 0.3,
                "polyline": [[116.3, 40.0], [116.31, 40.01]],
                "providerName": "amap-route",
            },
            {
                "id": "route_fake",
                "fromSegmentId": "seg_real",
                "toSegmentId": "seg_fake",
                "durationMinutes": 5,
                "distanceKm": 0.3,
                "polyline": [[116.3, 40.0], [116.4, 40.1]],
                "providerName": "amap-route",
            },
        ],
        "portfolioPendingSlots": [
            {
                "briefId": "brief_1",
                "poolId": "pool_art",
                "planningSlotId": "slot_art",
                "dayNumber": 1,
                "timeWindow": "14:00-18:00",
                "displayNeed": "art_walk",
            },
            {
                "briefId": "brief_1",
                "poolId": "pool_campus",
                "planningSlotId": "slot_campus",
                "dayNumber": 2,
                "timeWindow": "09:00-12:00",
                "displayNeed": "高校参观",
            },
        ],
    }


def _project(snapshot, *, simple_direction_scope: bool = False):
    return PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_1",
        root_portfolio_id="portfolio_1",
        proposal_id="partial:ver_1",
        source_assistant_turn_id="assistant_1",
        choice_id="adopt_partial_1",
        active_version_id="ver_1",
        expected_base_version_id="ver_1",
        is_partial=True,
        is_adopted=False,
        simple_direction_scope=simple_direction_scope,
    )


def test_projection_keeps_only_real_amap_anchors_and_verified_route_scope():
    projection = _project(_snapshot())
    assert [segment["poi"]["id"] for segment in projection["days"][0]["segments"]] == ["B000A8UIN8"]
    assert projection["routeEvidence"] == []
    assert [slot["planningSlotId"] for slot in projection["pendingSlots"]] == ["slot_art", "slot_campus"]
    assert projection["isAdopted"] is False
    assert projection["activeVersionId"] == "ver_1"


def test_simple_direction_projection_exposes_required_day_gap_and_blocks_confirmation():
    snapshot = _snapshot()
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["requiredPlanningDayNumbers"] = [1, 2]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["portfolioVerifier"] = {
        "passed": True,
        "draftPassed": True,
        "confirmationPassed": True,
        "hardFailures": [],
    }

    projection = _project(snapshot, simple_direction_scope=True)

    assert projection["requiredPlanningDayNumbers"] == [1, 2]
    assert projection["explicitRestDayNumbers"] == []
    assert projection["uncoveredDayNumbers"] == [2]
    assert projection["confirmationPassed"] is False
    assert projection["adoptionReady"] is False
    assert projection["status"] == "partial"


def test_simple_direction_projection_preserves_meal_quality_evidence() -> None:
    snapshot = _snapshot()
    brief = {
        "briefId": "meal-brief-1",
        "proposalBriefId": "proposal-1",
        "planningSlotId": "meal_day_1",
        "dayNumber": 1,
        "mealLabel": "lunch",
        "themeId": "炸酱面",
        "themeLabel": "炸酱面",
        "experienceMode": "signature_dish",
        "searchTerms": ["炸酱面"],
        "generationSource": "llm_search_hypothesis",
        "sourceFingerprint": "f" * 64,
    }
    snapshot["days"][0]["segments"] = [
        {
            "id": "seg_meal",
            "title": "本地风味馆",
            "kind": "meal",
            "startTime": "12:00",
            "endTime": "13:15",
            "poi": {
                "id": "B000MEAL01",
                "amapId": "B000MEAL01",
                "name": "本地风味馆",
                "source": "amap-place-search",
                "latitude": 39.98,
                "longitude": 116.32,
            },
            "semanticMetadata": {
                "intentType": "meal",
                "planningSlotId": "meal_day_1",
                "scheduleConstraints": {
                    "localFoodRequired": True,
                    "mealExperienceBrief": brief,
                    "mealSemanticEvidence": {
                        "amapPoiId": "B000MEAL01",
                        "canonicalBrand": "本地风味馆",
                        "themeId": "炸酱面",
                        "themeLabel": "炸酱面",
                        "groundedFamilyKey": "炸酱面",
                        "matchedTerms": ["炸酱面"],
                        "matchedFields": ["tags"],
                        "localFoodEvidenceKind": "amap_destination_cuisine_subtype",
                        "themeGrounded": True,
                        "localFoodPassed": True,
                        "sourceFingerprint": "f" * 64,
                    },
                },
            },
        }
    ]
    snapshot["days"] = [snapshot["days"][0]]
    snapshot["routeOptions"] = []
    snapshot["portfolioPendingSlots"] = []
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["desiredDensityAnchorTargets"] = {"1": 1}
    snapshot["dailyPlanningCoverageSource"] = "authoritative_goal_occurrences"
    snapshot["portfolioVerifier"] = {"passed": False, "draftPassed": False, "confirmationPassed": False}

    projection = _project(snapshot, simple_direction_scope=True)

    assert projection["mealQualityPassed"] is True
    assert projection["mealDiversityPassed"] is True
    assert projection["mealUnresolvedReasons"] == []
    assert projection["mealThemeSignature"] == ["炸酱面"]
    assert projection["mealExperienceBriefs"] == [brief]
    assert projection["mealSemanticEvidence"][0]["amapPoiId"] == "B000MEAL01"


def test_simple_direction_title_uses_sealed_intents_and_keeps_blocked_state_separate():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][0]["semanticMetadata"]["intentType"] = "campus_visit"
    snapshot = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_provider_unavailable",
    )
    snapshot["portfolioVerifier"] = {
        "passed": False,
        "draftPassed": False,
        "hardFailures": ["simple_direction_planned_day_missing_verified_amap_anchor"],
    }
    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_1",
        root_portfolio_id="portfolio_1",
        proposal_id="proposal_1",
        source_assistant_turn_id="assistant_1",
        choice_id="choice_1",
        active_version_id=None,
        expected_base_version_id=None,
        is_partial=True,
        is_adopted=False,
        simple_direction_scope=True,
    )

    assert "清华" in projection["displayTitle"]
    assert projection["title"] == projection["displayTitle"]
    assert projection["adoptionReady"] is False
    assert projection["title"] != "方案待补全"
    assert projection["title"] != "清华大学"


def test_simple_direction_title_never_attaches_evening_semantics_to_a_meal() -> None:
    snapshot = _snapshot()
    meal = {
        "id": "seg_meal",
        "title": "测试餐厅",
        "kind": "meal",
        "startTime": "12:00",
        "endTime": "13:15",
        "poi": {
            "id": "B000MEAL01",
            "amapId": "B000MEAL01",
            "name": "测试餐厅",
            "source": "amap-place-search",
            "latitude": 39.98,
            "longitude": 116.32,
        },
        "semanticMetadata": {
            "creativeBriefId": "brief_1",
            "intentType": "meal",
            "schedulePreference": {"dayPart": "noon"},
        },
    }
    park = {
        "id": "seg_park",
        "title": "测试公园",
        "kind": "park",
        "startTime": "18:20",
        "endTime": "19:40",
        "poi": {
            "id": "B000PARK01",
            "amapId": "B000PARK01",
            "name": "测试公园",
            "source": "amap-place-search",
            "latitude": 39.96,
            "longitude": 116.34,
        },
        "semanticMetadata": {
            "creativeBriefId": "brief_1",
            "intentType": "park",
            "schedulePreference": {"dayPart": "evening"},
            "scheduleDecision": {"scheduleConfidence": "provisional"},
        },
    }
    snapshot["days"][0]["segments"] = [snapshot["days"][0]["segments"][0], meal, park]
    snapshot["days"][0]["segments"][0]["semanticMetadata"]["intentType"] = "campus_visit"
    snapshot = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_candidates_invalid",
    )

    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_1",
        root_portfolio_id="portfolio_1",
        proposal_id="proposal_1",
        source_assistant_turn_id="assistant_1",
        choice_id="choice_1",
        active_version_id=None,
        expected_base_version_id=None,
        is_partial=True,
        is_adopted=False,
        simple_direction_scope=True,
    )

    assert "清华" in projection["displayTitle"]
    assert "测试餐厅" not in projection["displayTitle"]
    assert "测试公园" not in projection["displayTitle"]


def test_simple_direction_preview_orders_noon_before_evening_when_server_sequence_is_stale() -> None:
    snapshot = _snapshot()
    campus = snapshot["days"][0]["segments"][0]
    campus["semanticMetadata"].update(
        {
            "intentType": "campus_visit",
            "schedulePreference": {
                "dayPart": "flexible",
                "sequence": 1,
                "sequenceSource": "server_sealed_slot_order",
            },
        }
    )
    campus["startTime"] = ""
    campus["endTime"] = ""
    meal = copy.deepcopy(campus)
    meal.update({"id": "seg_meal", "title": "测试餐厅", "kind": "meal"})
    meal["poi"].update(
        {
            "id": "B000MEAL01",
            "amapId": "B000MEAL01",
            "name": "测试餐厅",
            "latitude": 39.98,
            "longitude": 116.32,
        }
    )
    meal["semanticMetadata"].update(
        {
            "intentType": "meal",
            "schedulePreference": {
                "dayPart": "noon",
                "sequence": 3,
                "sequenceSource": "server_sealed_slot_order",
            },
        }
    )
    meal["startTime"] = "12:00"
    park = copy.deepcopy(campus)
    park.update({"id": "seg_park", "title": "测试公园", "kind": "park"})
    park["poi"].update(
        {
            "id": "B000PARK01",
            "amapId": "B000PARK01",
            "name": "测试公园",
            "latitude": 39.96,
            "longitude": 116.34,
        }
    )
    park["semanticMetadata"].update(
        {
            "intentType": "park",
            "schedulePreference": {
                "dayPart": "evening",
                "sequence": 2,
                "sequenceSource": "server_sealed_slot_order",
            },
        }
    )
    # The provider estimate is stale and conflicts with the explicit evening
    # semantics; it must not move the park before lunch.
    park["startTime"] = "11:30"
    snapshot["days"][0]["segments"] = [campus, park, meal]

    projection = _project(snapshot, simple_direction_scope=True)

    assert [item["poi"]["name"] for item in projection["days"][0]["segments"]] == [
        "清华大学",
        "测试餐厅",
        "测试公园",
    ]


def test_simple_direction_title_metadata_does_not_change_material_or_repair_identity() -> None:
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][0]["semanticMetadata"]["intentType"] = "campus_visit"
    first = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_provider_unavailable",
    )
    second = CreativeProposalTitleService.apply_agent_projection(
        snapshot,
        CreativeProposalTitleService.select_agent_candidate(
            snapshot,
            {
                "schemaVersion": "creative-proposal-title-candidates-v1",
                "candidates": [
                    {"title": "清华风物与京城漫游", "evidenceAmapIds": ["B000A8UIN8"]},
                    {"title": "清华校园慢游拾光", "evidenceAmapIds": ["B000A8UIN8"]},
                    {"title": "清华学府风光漫行", "evidenceAmapIds": ["B000A8UIN8"]},
                ],
            },
        ),
    )

    first_projection = _project(first, simple_direction_scope=True)
    second_projection = _project(second, simple_direction_scope=True)

    assert first_projection["displayTitle"] != second_projection["displayTitle"]
    assert first_projection["materialFingerprint"] == second_projection["materialFingerprint"]
    assert first_projection["repairChoiceId"] == second_projection["repairChoiceId"]


def test_simple_direction_projects_provider_route_assignment_truth() -> None:
    snapshot = _snapshot()
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["simpleOpenRouteAssignment"] = {
        "decisionSource": "provider_route_matrix",
        "routeContractFingerprint": "route_fp_1",
        "routeProviderAttemptCount": 3,
        "routeProviderCacheHitCount": 1,
        "baselineGeneralizedCost": 48.0,
        "selectedGeneralizedCost": 52.0,
        "generalizedCostDelta": 4.0,
        "detourRatio": 0.0833,
        "detourCompliance": "verified",
        "providerRoutePairs": [
            {"fromAmapId": "B000A8UIN8", "toAmapId": "B000PARK01", "provider": "amap"}
        ],
    }

    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_1",
        root_portfolio_id="portfolio_1",
        proposal_id="proposal_1",
        source_assistant_turn_id="assistant_1",
        choice_id="choice_1",
        active_version_id=None,
        expected_base_version_id=None,
        is_partial=True,
        is_adopted=False,
        simple_direction_scope=True,
    )

    assert projection["detourCompliance"] == "verified"
    assert projection["routeAssignmentEvidence"]["routeContractFingerprint"] == "route_fp_1"
    assert projection["routeAssignmentEvidence"]["routeProviderAttemptCount"] == 3


def test_simple_direction_prefers_valid_agent_theme_title_over_poi_compilation() -> None:
    snapshot = _snapshot()
    snapshot["days"] = [snapshot["days"][0]]
    snapshot["days"][0]["segments"] = [snapshot["days"][0]["segments"][0]]
    amap_ids = ["B000A8UIN8"]
    titled = CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=lambda _context: {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "清华秋日漫游京华", "evidenceAmapIds": amap_ids},
                {"title": "清华书香城韵寻访", "evidenceAmapIds": amap_ids},
                {"title": "清华校园风物漫步", "evidenceAmapIds": amap_ids},
            ],
        },
        context={},
    )

    projection = PlanComparisonPreviewService.project_snapshot(
        titled,
        planning_selection_root_turn_id="root_1",
        root_portfolio_id="portfolio_1",
        proposal_id="proposal_1",
        source_assistant_turn_id="assistant_1",
        choice_id="choice_1",
        active_version_id=None,
        expected_base_version_id=None,
        is_partial=True,
        is_adopted=False,
        simple_direction_scope=True,
    )

    assert projection["displayTitle"] == "清华秋日漫游京华"


def test_physical_novelty_collapses_amap_parent_and_child_records():
    parent_id = "B000AA3ZCC"
    projection = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": {
                            "amapId": parent_id,
                            "source": "amap-place-search",
                            "longitude": 116.40,
                            "latitude": 39.98,
                        }
                    },
                    {
                        "poi": {
                            "amapId": "B0FFILD7HG",
                            "parentPoiId": parent_id,
                            "source": "amap-place-search",
                            "longitude": 116.4001,
                            "latitude": 39.9801,
                        }
                    },
                ],
            }
        ]
    }

    assert PlanComparisonPreviewService.physical_poi_ids(projection) == {parent_id}


def test_projection_exposes_chinese_budget_and_parameterized_block_labels():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {
        "passed": False,
        "hardFailures": [
            "route_anchor_target_mismatch:day_1:2/4",
            "required_goal_count_insufficient:goal_night_view:0/2",
            "required_goal_omitted:goal_night_view",
            "theme_optional_family_missing",
            "portfolio_route_quality:route_evidence_missing",
        ],
    }
    projection = _project(snapshot)
    assert projection["budgetTierLabel"] == "中等预算"
    assert projection["budgetSummary"].startswith("中等预算 · ")
    assert "第 1 天计划 4 个地点，已确认 2 个" in projection["blockingReasonLabels"]
    assert "夜景必选地点需要 2 个，当前确认 0 个" in projection["blockingReasonLabels"]
    assert "夜景必选目标尚未加入" in projection["blockingReasonLabels"]
    assert "该方案的主题体验尚未补齐" in projection["blockingReasonLabels"]
    assert "仍缺与当前停靠顺序一致的路线核验" in projection["blockingReasonLabels"]


def test_projection_uses_one_route_readiness_scope_for_verified_main_route_and_pending_meal():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"] = [
        snapshot["days"][0]["segments"][0],
        {
            "id": "seg_real_2",
            "title": "圆明园",
            "kind": "visit",
            "startTime": "14:00",
            "endTime": "16:00",
            "poi": {
                "id": "B000A8UIN9",
                "amapId": "B000A8UIN9",
                "name": "圆明园",
                "source": "amap-place-search",
                "latitude": 40.01,
                "longitude": 116.31,
            },
            "semanticMetadata": {"creativeBriefId": "brief_1"},
        },
    ]
    snapshot["routeOptions"] = [
        {
            "id": "route_real",
            "fromSegmentId": "seg_real",
            "toSegmentId": "seg_real_2",
            "fromPoiId": "B000A8UIN8",
            "toPoiId": "B000A8UIN9",
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "transit",
            "transportMode": "transit",
            "isSelected": True,
            "durationMinutes": 10,
            "durationSeconds": 600,
            "distanceKm": 1.2,
            "distanceMeters": 1200,
            "polyline": [[116.3, 40.0], [116.31, 40.01]],
            "providerName": "amap-route",
            "status": "verified",
            "queriedAt": "2026-08-07T00:00:00+00:00",
        }
    ]
    snapshot["portfolioPendingSlots"] = [
        {
            "briefId": "brief_1",
            "poolId": "pool_meal",
            "planningSlotId": "slot_meal",
            "dayNumber": 1,
            "timeWindow": "12:00-14:00",
            "displayNeed": "当地特色美食",
            "intentType": "meal",
            "requirementLevel": "explicit_soft",
            "futureRouteAnchor": True,
        }
    ]

    projection = _project(snapshot)
    assert projection["routeStatus"] == "route_ready"
    assert projection["routeExpectedLegCount"] == 1
    assert projection["routeVerifiedLegCount"] == 1
    assert projection["routeSummary"] == "主路线已核验 1 段；餐饮插入后仍有 1 段待核验"


def test_projection_signs_completion_action_and_exposes_three_density_targets():
    snapshot = _snapshot()
    snapshot["portfolioOutputQuality"] = {
        "mode": "enforce",
        "requestedTheme": "local_food_and_area_walk",
    }
    snapshot["portfolioDayAnchorTargets"] = {"1": 3, "2": 2}
    snapshot["portfolioPendingSlots"][0]["futureRouteAnchor"] = True

    projection = _project(snapshot)

    assert projection["completionAction"]["choiceId"] == "portfolio_theme_completion_partial:ver_1"
    assert projection["desiredDensityAnchorTargets"] == {"1": 3, "2": 2}
    assert projection["groundedRouteAnchorTargets"] == {"1": 1, "2": 0}
    assert projection["pendingFutureAnchorTargets"] == {"1": 1, "2": 0}


def test_projection_preserves_pending_priority_and_cannot_claim_adoption_without_version():
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"][0]["requirementLevel"] = "explicit_soft"
    snapshot["portfolioPendingSlots"][1]["requirementLevel"] = "hard"
    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="root_1",
        root_portfolio_id="portfolio_1",
        proposal_id="proposal_1",
        source_assistant_turn_id="assistant_1",
        choice_id="choice_1",
        active_version_id=None,
        expected_base_version_id=None,
        is_partial=True,
        is_adopted=True,
    )

    assert projection["isAdopted"] is False
    assert projection["activeVersionId"] is None
    assert projection["pendingHardSlotCount"] == 1
    assert projection["pendingSoftSlotCount"] == 1
    assert [item["requirementLevel"] for item in projection["pendingSlots"]] == [
        "explicit_soft",
        "hard",
    ]


def test_real_trace_shape_with_five_soft_slots_is_partial_even_when_editable():
    snapshot = _snapshot()
    day_one_anchor = snapshot["days"][0]["segments"][0]
    day_one_anchor["semanticMetadata"].update({"routeAnchor": True, "required": True})
    day_two_anchor = {
        **day_one_anchor,
        "id": "seg_day_2",
        "title": "北京大学",
        "poi": {
            **day_one_anchor["poi"],
            "id": "B000A7BD6C",
            "amapId": "B000A7BD6C",
            "name": "北京大学",
            "latitude": 39.9928,
            "longitude": 116.3109,
        },
    }
    snapshot["days"][0]["segments"] = [day_one_anchor]
    snapshot["days"][1]["segments"] = [day_two_anchor]
    snapshot["routeOptions"] = []
    snapshot["portfolioVerifier"] = {"passed": False, "draftPassed": True, "hardFailures": []}
    snapshot["portfolioPendingSlots"] = [
        {
            "briefId": "brief_1",
            "poolId": f"pool_soft_{index}",
            "planningSlotId": f"slot_soft_{index}",
            "dayNumber": 1 if index < 3 else 2,
            "timeWindow": "12:00-14:00" if index % 2 == 0 else "18:00-22:00",
            "displayNeed": f"待选体验 {index + 1}",
            "requirementLevel": "explicit_soft",
        }
        for index in range(5)
    ]

    projection = PlanComparisonPreviewService.project_snapshot(
        snapshot,
        planning_selection_root_turn_id="turn_20a72783de1a",
        root_portfolio_id="portfolio_a0989b2718ca4034",
        proposal_id="proposal_trace_shape",
        source_assistant_turn_id="assistant_trace_shape",
        choice_id="choice_trace_shape",
        active_version_id=None,
        expected_base_version_id=None,
        is_partial=True,
        is_adopted=False,
    )

    assert projection["pendingSoftSlotCount"] == 5
    assert projection["pendingHardSlotCount"] == 0
    assert projection["adoptionReady"] is True, projection
    assert projection["strictlyVerified"] is False
    assert projection["isPartial"] is True
    assert projection["status"] == "partial"
    assert projection["adoptionMode"] == "editable_draft"
    assert projection["activeVersionId"] is None
    assert projection["isAdopted"] is False


def test_projection_sort_keeps_unknown_time_after_known_time_on_the_same_day():
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"].append(
        {
            "briefId": "brief_1",
            "poolId": "pool_unknown",
            "planningSlotId": "slot_unknown",
            "dayNumber": 1,
            "displayNeed": "时间待定",
        }
    )
    projection = _project(snapshot)
    assert [slot["planningSlotId"] for slot in projection["pendingSlots"]] == [
        "slot_art",
        "slot_unknown",
        "slot_campus",
    ]
    assert projection["pendingSlots"][1]["timeLabel"] == "时间待定"


def test_pending_slot_window_is_provisional_until_place_and_route_are_selected():
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"][0].update(
        {
            "timingStatus": "awaiting_route_confirmation",
            "timingBasis": "preferred_window",
            "constraintSummary": "等待地点和交通方式",
            "placementAfterSegmentId": "seg_real",
        }
    )

    projection = _project(snapshot)
    slot = projection["pendingSlots"][0]

    assert slot["timeLabel"] == ("可安排时段 14:00-18:00（选点和交通方式确认后自动重排）")
    assert slot["timingStatus"] == "awaiting_route_confirmation"
    assert slot["timingBasis"] == "preferred_window"
    assert slot["constraintSummary"] == "等待地点和交通方式"
    assert slot["placementAfterSegmentId"] == "seg_real"


def test_projection_rejects_spoofed_identity_route_status_distance_and_cross_scope():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"].extend(
        [
            {
                "id": "seg_spoof",
                "poi": {
                    "id": "spoof",
                    "name": "伪造地点",
                    "source": "amap-place-search",
                    "latitude": 40.02,
                    "longitude": 116.32,
                },
            },
            {
                "id": "seg_real_2",
                "poi": {
                    "id": "B000A8UIN9",
                    "amapId": "B000A8UIN9",
                    "name": "圆明园",
                    "source": "amap-place-search",
                    "latitude": 40.01,
                    "longitude": 116.31,
                },
                "semanticMetadata": {"creativeBriefId": "brief_1"},
            },
        ]
    )
    snapshot["routeOptions"] = [
        {
            "id": "route_fake_provider",
            "fromSegmentId": "seg_real",
            "toSegmentId": "seg_real_2",
            "providerName": "amap-fake",
            "status": "success",
            "polyline": [[116.3, 40.0], [116.31, 40.01]],
            "durationMinutes": 5,
            "distanceKm": 1,
        },
        {
            "id": "route_error",
            "fromSegmentId": "seg_real",
            "toSegmentId": "seg_real_2",
            "providerName": "amap-route",
            "status": "error",
            "polyline": [[116.3, 40.0], [116.31, 40.01]],
            "durationMinutes": 5,
            "distanceKm": 1,
        },
        {
            "id": "route_zero_distance",
            "fromSegmentId": "seg_real",
            "toSegmentId": "seg_real_2",
            "providerName": "amap-route",
            "status": "success",
            "polyline": [[116.3, 40.0], [116.31, 40.01]],
            "durationMinutes": 5,
            "distanceKm": 0,
        },
    ]
    snapshot["portfolioPendingSlots"].extend(
        [
            {"briefId": "brief_foreign", "poolId": "pool_foreign", "planningSlotId": "slot_foreign", "dayNumber": 1},
            {"briefId": "brief_1", "poolId": "pool_foreign_day", "planningSlotId": "slot_foreign_day", "dayNumber": 99},
        ]
    )
    projection = _project(snapshot)
    assert "seg_spoof" not in {segment["id"] for day in projection["days"] for segment in day["segments"]}
    assert projection["routeEvidence"] == []
    assert {slot["planningSlotId"] for slot in projection["pendingSlots"]} == {"slot_art", "slot_campus"}
    assert {day["dayNumber"] for day in projection["days"]} == {1, 2}


def test_projection_rejects_arbitrary_amap_identity_and_missing_brief_lineage():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"].extend(
        [
            {
                "id": "seg_arbitrary",
                "poi": {
                    "id": "X123",
                    "amapId": "X123",
                    "name": "任意身份",
                    "source": "amap-place-search",
                    "latitude": 40.02,
                    "longitude": 116.32,
                },
                "semanticMetadata": {"creativeBriefId": "brief_1"},
            },
            {
                "id": "seg_missing_lineage",
                "poi": {
                    "id": "B000A8UIN7",
                    "amapId": "B000A8UIN7",
                    "name": "缺谱系",
                    "source": "amap-place-search",
                    "latitude": 40.03,
                    "longitude": 116.33,
                },
            },
        ]
    )
    ids = {segment["id"] for day in _project(snapshot)["days"] for segment in day["segments"]}
    assert "seg_arbitrary" not in ids
    assert "seg_missing_lineage" not in ids


def test_projection_without_frozen_brief_identity_fails_closed():
    snapshot = _snapshot()
    snapshot.pop("portfolioSelectionContext")
    projection = _project(snapshot)
    assert all(not day["segments"] for day in projection["days"])
    assert projection["pendingSlots"] == []


def test_projection_readiness_uses_only_visible_brief_scope():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"][0]["semanticMetadata"]["routeAnchor"] = True
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_foreign_invalid",
            "poi": {"name": "外部方案未落地地点", "source": "amap-fake"},
            "semanticMetadata": {
                "creativeBriefId": "brief_foreign",
                "routeAnchor": True,
            },
        }
    )

    projection = _project(snapshot)

    assert "map_identity_incomplete" not in projection["blockingReasons"]


def _physical_projection(proposal_id: str, poi_ids: list[str]) -> dict:
    return {
        "planningSelectionRootTurnId": "root_1",
        "rootPortfolioId": "portfolio_1",
        "proposalId": proposal_id,
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": f"{proposal_id}_{index}",
                        "title": f"地点 {index}",
                        "poi": {
                            "amapId": amap_id,
                            "name": f"地点 {index}",
                            "source": "amap-place-search",
                            "latitude": 39.9 + index / 100,
                            "longitude": 116.3 + index / 100,
                        },
                        "semanticMetadata": {
                            "creativeBriefId": proposal_id,
                            "optionalExperienceFamily": (
                                "market_walk" if proposal_id.endswith("food") else "local_life"
                            ),
                        },
                    }
                    for index, amap_id in enumerate(poi_ids, start=1)
                ],
            }
        ],
    }


def test_partial_preview_novelty_ignores_theme_labels_when_physical_pois_are_identical():
    local = _physical_projection(
        "partial-preview:portfolio_1:local",
        ["B000A7BD6C", "B000A7JEXW", "B000A80HO9", "B0MGGZ3HPM", "B000A81CB2"],
    )
    food = _physical_projection(
        "partial-preview:portfolio_1:food",
        ["B000A7BD6C", "B000A7JEXW", "B000A80HO9", "B0MGGZ3HPM", "B000A81CB2"],
    )
    food["title"] = "京味美食串联"
    local["title"] = "本地文化沉浸"

    audit = PlanComparisonPreviewService.material_novelty_audit(food, [local])

    assert audit["passed"] is False
    assert audit["minimumDistance"] == 0.0
    assert audit["reasonCode"] == "partial_preview_not_materially_distinct"


def test_partial_preview_novelty_rejects_four_of_five_overlap_but_accepts_material_change():
    classic = _physical_projection(
        "partial:portfolio_1",
        ["B000A7BD6C", "B000A81FMM", "B000A80HO9", "B0MGGZ3HPM", "B000A81CB2"],
    )
    mostly_same = _physical_projection(
        "partial-preview:portfolio_1:local",
        ["B000A7BD6C", "B000A7JEXW", "B000A80HO9", "B0MGGZ3HPM", "B000A81CB2"],
    )
    materially_different = _physical_projection(
        "partial-preview:portfolio_1:nature",
        ["B000A7BD6C", "B000A7JEXW", "B000A80HO9", "B0H2N1J6LL", "B000A81CB2"],
    )

    mostly_same_audit = PlanComparisonPreviewService.material_novelty_audit(
        mostly_same,
        [classic],
    )
    materially_different_audit = PlanComparisonPreviewService.material_novelty_audit(
        materially_different,
        [classic],
    )

    assert mostly_same_audit["minimumDistance"] == 0.3333
    assert mostly_same_audit["minimumNewPoiFraction"] == 0.2
    assert mostly_same_audit["passed"] is False
    assert materially_different_audit["minimumDistance"] == 0.5714
    assert materially_different_audit["minimumNewPoiFraction"] == 0.4
    assert materially_different_audit["passed"] is True


def test_material_novelty_uses_theme_anchors_without_shared_required_dilution():
    shared_ids = ["B000A7BD6C", "B000A81FMM", "B000A80HO9", "B0MGGZ3HPM", "B000A81CB2", "B000A7JEXW"]
    prior = _physical_projection("proposal_prior", [*shared_ids, "B000THEME1"])
    candidate = _physical_projection("proposal_candidate", [*shared_ids, "B000THEME2"])
    for projection in (prior, candidate):
        segments = projection["days"][0]["segments"]
        for segment in segments[:-1]:
            segment["semanticMetadata"] = {
                "creativeBriefId": projection["proposalId"],
                "requirementLevel": "required",
                "required": True,
            }
        segments[-1]["semanticMetadata"].update(
            {"portfolioOptional": True, "optionalExperienceFamily": "heritage_walk"}
        )

    audit = PlanComparisonPreviewService.material_novelty_audit(candidate, [prior])

    assert audit["passed"] is True
    assert audit["minimumNewPoiCount"] == 1
    assert audit["minimumNewPoiFraction"] == 1.0
    assert audit["candidateFlexiblePoiCount"] == 1
    assert audit["perReferenceComparisons"][0]["noveltyScope"] == "flexible_theme_anchors"


def test_partial_preview_novelty_never_treats_a_prior_subset_as_a_new_plan():
    prior = _physical_projection(
        "partial:portfolio_1",
        ["B000A7BD6C", "B000A81FMM", "B000A80HO9", "B0MGGZ3HPM", "B000A81CB2"],
    )
    subset = _physical_projection(
        "partial-preview:portfolio_1:subset",
        ["B000A7BD6C", "B000A80HO9", "B000A81CB2"],
    )

    audit = PlanComparisonPreviewService.material_novelty_audit(subset, [prior])

    assert audit["minimumDistance"] == 0.4
    assert audit["minimumNewPoiFraction"] == 0.0
    assert audit["nearestProposalId"] == "partial:portfolio_1"
    assert audit["minimumDistanceProposalId"] == "partial:portfolio_1"
    assert audit["minimumNewPoiFractionProposalId"] == "partial:portfolio_1"
    assert audit["perReferenceComparisons"][0]["newPoiCount"] == 0
    assert audit["passed"] is False


def test_partial_preview_with_complete_strict_evidence_is_promoted():
    snapshot = _snapshot()
    real = snapshot["days"][0]["segments"][0]
    real["semanticMetadata"].update({"routeAnchor": True, "required": True})
    snapshot["days"][0]["segments"] = [real]
    snapshot["portfolioPendingSlots"] = []
    snapshot["routeOptions"] = []
    snapshot["portfolioVerifier"] = {"passed": True}

    projection = _project(snapshot)

    assert projection["status"] == "complete"
    assert projection["isPartial"] is False
    assert projection["adoptionReady"] is True
    assert projection["originProjectionMode"] == "partial_preview"
    assert projection["promotionStatus"] == "promoted"
    assert projection["nextAction"] == "continue_editing"
    assert "explicit_partial_preview" not in projection["blockingReasons"]


def test_partial_concept_novelty_ignores_title_and_family_label_only_changes():
    base = {
        "proposalId": "base",
        "title": "方向 A",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "startTime": "14:00",
                        "semanticMetadata": {
                            "dayRole": "慢节奏",
                            "experienceShape": "area",
                            "desiredSignals": ["resident_activity"],
                            "optionalExperienceFamily": "local_life",
                        },
                    }
                ],
            }
        ],
        "pendingSlots": [],
    }
    relabeled = {
        **base,
        "proposalId": "other",
        "title": "完全不同标题",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "startTime": "14:00",
                        "semanticMetadata": {
                            "dayRole": "慢节奏",
                            "experienceShape": "area",
                            "desiredSignals": ["resident_activity"],
                            "optionalExperienceFamily": "market_walk",
                        },
                    }
                ],
            }
        ],
    }
    audit = PlanComparisonPreviewService.concept_novelty_audit(relabeled, [base])
    assert audit["passed"] is False
    assert audit["noveltyMode"] == "concept"


def test_partial_concept_novelty_accepts_a_real_shape_change():
    base = {
        "proposalId": "base",
        "days": [],
        "pendingSlots": [
            {
                "dayNumber": 1,
                "timeWindow": "afternoon",
                "experienceShape": "area",
                "desiredSignals": ["resident_activity"],
            }
        ],
    }
    changed = {
        "proposalId": "changed",
        "days": [],
        "pendingSlots": [
            {
                "dayNumber": 1,
                "timeWindow": "afternoon",
                "experienceShape": "micro_route",
                "desiredSignals": ["resident_activity"],
            }
        ],
    }
    assert PlanComparisonPreviewService.concept_novelty_audit(changed, [base])["passed"] is True


def test_material_novelty_rejects_label_only_duplicate_even_for_editable_draft():
    prior = _physical_projection("prior", ["B00000001", "B00000002", "B00000003", "B00000004"])
    relabeled = {
        **prior,
        "proposalId": "relabeled",
        "title": "完全不同的概念标题",
        "portfolioPendingSlots": [{"family": "art_walk"}],
    }

    audit = PlanComparisonPreviewService.material_novelty_audit(relabeled, [prior])

    assert audit["passed"] is False
    assert audit["minimumNewPoiCount"] == 0
    assert audit["minimumDistance"] == 0.0


def test_two_day_material_novelty_requires_two_new_pois_or_two_sequence_changes():
    prior = _physical_projection("prior", ["B00000001", "B00000002", "B00000003", "B00000004"])
    prior["days"] = [
        {**prior["days"][0], "dayNumber": 1, "segments": prior["days"][0]["segments"][:2]},
        {**prior["days"][0], "dayNumber": 2, "segments": prior["days"][0]["segments"][2:]},
    ]
    one_replacement = _physical_projection("one", ["B00000001", "B00000005", "B00000003", "B00000004"])
    one_replacement["days"] = [
        {**one_replacement["days"][0], "dayNumber": 1, "segments": one_replacement["days"][0]["segments"][:2]},
        {**one_replacement["days"][0], "dayNumber": 2, "segments": one_replacement["days"][0]["segments"][2:]},
    ]
    two_replacements = _physical_projection("two", ["B00000005", "B00000006", "B00000003", "B00000004"])
    two_replacements["days"] = [
        {**two_replacements["days"][0], "dayNumber": 1, "segments": two_replacements["days"][0]["segments"][:2]},
        {**two_replacements["days"][0], "dayNumber": 2, "segments": two_replacements["days"][0]["segments"][2:]},
    ]

    assert PlanComparisonPreviewService.material_novelty_audit(one_replacement, [prior])["passed"] is False
    assert PlanComparisonPreviewService.material_novelty_audit(two_replacements, [prior])["passed"] is True

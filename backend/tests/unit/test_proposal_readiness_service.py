from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.proposal_readiness_service import ProposalReadinessService


def _verified_route(*, start="seg_1", end="seg_2"):
    return {
        "id": f"route_{start}_{end}",
        "fromSegmentId": start,
        "toSegmentId": end,
        "fromPoiId": f"poi_{start}",
        "toPoiId": f"poi_{end}",
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "mode": "transit",
        "transportMode": "transit",
        "label": "公交地铁",
        "isSelected": True,
        "sortOrder": 1,
        "status": "verified",
        "distanceMeters": 4200,
        "durationSeconds": 1320,
        "durationMinutes": 22,
        "distanceKm": 4.2,
        "costAmount": 0,
        "costCurrency": "CNY",
        "costEstimate": 0,
        "crowdingRisk": "",
        "polyline": [[116.31, 39.91], [116.32, 39.92]],
        "steps": [],
        "providerPayload": {},
        "queriedAt": "2026-08-01T00:00:00+00:00",
    }


def _snapshot(*, route_options=None, estimated_cost=None, free=False):
    segments = []
    for index, name in enumerate(("清华大学", "模式口历史文化街区"), start=1):
        segments.append(
            {
                "id": f"seg_{index}",
                "kind": "visit",
                "estimatedCost": estimated_cost,
                "costEvidence": {"status": "verified_free"} if free else None,
                "poi": {
                    "name": name,
                    "amapId": f"B0000000{index}",
                    "source": "amap-place-search",
                    "latitude": 39.9 + index / 100,
                    "longitude": 116.3 + index / 100,
                },
                "semanticMetadata": {
                    "routeAnchor": True,
                    "groundingStatus": "selected",
                    "required": index == 1,
                    "portfolioOptional": index == 2,
                    "optionalExperienceFamily": "heritage_walk",
                },
            }
        )
    snapshot = {
        "budgetTier": "medium",
        "days": [{"dayNumber": 1, "segments": segments}],
        "routeOptions": route_options or [],
        "portfolioPendingSlots": [],
    }
    evidence_ids = [segment["poi"]["amapId"] for segment in segments]
    return CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=lambda _context: {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "清华学府古巷京城", "evidenceAmapIds": evidence_ids},
                {"title": "模式口书香街韵京华", "evidenceAmapIds": evidence_ids},
                {"title": "清华校园巷陌慢步", "evidenceAmapIds": evidence_ids},
            ],
        },
        context={},
    )


def test_multiple_anchors_without_routes_are_not_complete_or_adoptable():
    report = ProposalReadinessService.compute(_snapshot(), verifier={"passed": True})

    assert report["routeStatus"] == "route_pending"
    assert report["routeExpectedLegCount"] == 1
    assert report["routeVerifiedLegCount"] == 0
    assert report["adoptionReady"] is False
    assert report["strictlyVerified"] is False
    assert report["isPartial"] is True
    assert "route_evidence_incomplete" in report["blockingReasons"]


def test_verified_route_and_free_cost_evidence_can_be_ready():
    report = ProposalReadinessService.compute(
        _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True),
        verifier={"passed": True},
    )

    assert report["routeStatus"] == "route_ready"
    assert report["budgetStatus"] == "verified"
    assert report["budgetEstimate"] == 0
    assert report["adoptionReady"] is True
    assert report["strictlyVerified"] is True
    assert report["isPartial"] is False


def test_extra_verified_non_adjacent_route_blocks_readiness():
    snapshot = _snapshot(estimated_cost=0, free=True)
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_3",
            "kind": "visit",
            "estimatedCost": 0,
            "costEvidence": {"status": "verified_free"},
            "poi": {
                "name": "正阳门",
                "amapId": "B00000003",
                "source": "amap-place-search",
                "latitude": 39.9,
                "longitude": 116.39,
            },
            "semanticMetadata": {
                "routeAnchor": True,
                "groundingStatus": "selected",
                "required": False,
            },
        }
    )
    snapshot["routeOptions"] = [
        _verified_route(start="seg_1", end="seg_2"),
        _verified_route(start="seg_2", end="seg_3"),
        _verified_route(start="seg_1", end="seg_3"),
    ]

    report = ProposalReadinessService.compute(snapshot, verifier={"passed": True})

    assert report["routeStatus"] == "route_invalid"
    assert report["routeCoverage"] is False
    assert report["routeExpectedLegCount"] == 2
    assert report["routeVerifiedLegCount"] == 2
    assert report["routeUnexpectedLegCount"] == 1
    assert report["adoptionReady"] is False
    assert report["currentReadiness"] == "blocked"
    assert "route_evidence_incomplete" in report["blockingReasons"]


def test_title_generation_failure_is_presentation_only_for_adoption():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["portfolioTitleGeneration"] = {
        "schemaVersion": "creative-proposal-title-generation-v1",
        "status": "failed_retryable",
        "retryable": True,
        "reasonCode": "title_provider_failed",
    }

    report = ProposalReadinessService.compute(snapshot, verifier={"passed": True})

    assert report["routeStatus"] == "route_ready"
    assert report["adoptionReady"] is True
    assert "proposal_title_generation_pending" not in report["blockingReasons"]


def test_complete_proposal_does_not_require_agent_title_projection():
    missing_title = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    missing_title.pop("portfolioTitleEvidence")
    missing_title.pop("portfolioTitleGeneration")

    missing_report = ProposalReadinessService.compute(
        missing_title,
        verifier={"passed": True},
    )

    assert missing_report["adoptionReady"] is True
    assert "proposal_title_generation_pending" not in missing_report["blockingReasons"]


def test_simple_direction_title_failure_is_presentation_only() -> None:
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot.pop("portfolioTitleEvidence", None)
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["desiredDensityAnchorTargets"] = {"1": 2}
    snapshot["dailyPlanningCoverageSource"] = "authoritative_goal_occurrences"
    snapshot["portfolioTitleGeneration"] = {
        "status": "failed_non_blocking",
        "reasonCode": "title_candidates_invalid",
    }
    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
        route_failures_non_blocking=True,
    )

    assert "proposal_title_generation_pending" not in report["blockingReasons"]
    assert report["adoptionReady"] is True

    stale_title = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    stale_title["days"][0]["segments"][0]["poi"]["amapId"] = "B00000009"

    stale_report = ProposalReadinessService.compute(
        stale_title,
        verifier={"passed": True},
    )

    assert stale_report["adoptionReady"] is True
    assert "proposal_title_generation_pending" not in stale_report["blockingReasons"]


def test_simple_direction_required_day_without_anchor_fails_closed() -> None:
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["days"].append({"dayNumber": 2, "segments": []})
    snapshot["requiredPlanningDayNumbers"] = [1, 2]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["desiredDensityAnchorTargets"] = {"1": 2, "2": 1}
    snapshot["dailyPlanningCoverageSource"] = "authoritative_goal_occurrences"

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
    )

    assert report["requiredPlanningDayNumbers"] == [1, 2]
    assert report["explicitRestDayNumbers"] == []
    assert report["uncoveredDayNumbers"] == [2]
    assert report["confirmationPassed"] is False
    assert report["adoptionReady"] is False
    assert report["strictlyVerified"] is False
    assert report["isPartial"] is True
    assert "simple_direction_required_day_empty" in report["hardFailures"]


def test_simple_direction_empty_day_requires_explicit_rest_provenance() -> None:
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["days"].append({"dayNumber": 2, "segments": []})
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = [2]
    snapshot["desiredDensityAnchorTargets"] = {"1": 2, "2": 0}
    snapshot["dailyPlanningCoverageSource"] = "authoritative_goal_occurrences"

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
    )

    assert report["uncoveredDayNumbers"] == []
    assert report["dailyPlanningCoverageContractInvalid"] is False
    assert report["confirmationPassed"] is True
    assert report["adoptionReady"] is True


def test_legacy_simple_direction_without_daily_contract_is_read_only() -> None:
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
    )

    assert report["dailyPlanningCoverageContractInvalid"] is True
    assert report["confirmationPassed"] is False
    assert report["adoptionReady"] is False
    assert "simple_direction_daily_planning_coverage_contract_invalid" in report["hardFailures"]


def test_simple_direction_grounded_meal_without_authoritative_brief_is_read_only() -> None:
    snapshot = _snapshot(
        route_options=[
            _verified_route(),
            _verified_route(start="seg_2", end="seg_meal"),
        ],
        estimated_cost=0,
        free=True,
    )
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["desiredDensityAnchorTargets"] = {"1": 3}
    snapshot["dailyPlanningCoverageSource"] = "authoritative_goal_occurrences"
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_meal",
            "kind": "meal",
            "estimatedCost": 80,
            "poi": {
                "name": "本地风味馆",
                "amapId": "B000MEAL01",
                "source": "amap-place-search",
                "latitude": 39.93,
                "longitude": 116.33,
            },
            "semanticMetadata": {
                "intentType": "meal",
                "planningSlotId": "meal_day_1",
                "routeAnchor": True,
                "groundingStatus": "selected",
                "scheduleConstraints": {
                    "localFoodRequired": True,
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
    )

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
    )

    assert report["mealQualityPassed"] is False
    assert report["mealDiversityPassed"] is False
    assert report["mealUnresolvedReasons"] == ["simple_direction_meal_brief_missing"]
    assert report["confirmationPassed"] is False
    assert report["adoptionReady"] is False
    assert report["strictlyVerified"] is False
    assert "simple_direction_meal_brief_missing" in report["blockingReasons"]


def test_simple_direction_non_authoritative_daily_source_is_read_only() -> None:
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["desiredDensityAnchorTargets"] = {"1": 2}
    snapshot["dailyPlanningCoverageSource"] = "derived_from_materialized_segments"

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
    )

    assert report["dailyPlanningCoverageSourceInvalid"] is True
    assert report["confirmationPassed"] is False
    assert report["adoptionReady"] is False
    assert "simple_direction_daily_planning_coverage_source_invalid" in report["hardFailures"]


def test_simple_direction_rest_day_with_positive_target_is_read_only() -> None:
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
    snapshot["days"].append({"dayNumber": 2, "segments": []})
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = [2]
    snapshot["desiredDensityAnchorTargets"] = {"1": 2, "2": 1}
    snapshot["dailyPlanningCoverageSource"] = "authoritative_goal_occurrences"

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True, "confirmationPassed": True},
    )

    assert report["dailyTargetDayPartitionInvalid"] is True
    assert report["confirmationPassed"] is False
    assert report["adoptionReady"] is False
    assert "simple_direction_daily_anchor_target_day_partition_invalid" in report["hardFailures"]


def test_incomplete_structural_draft_does_not_require_agent_title_generation():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot.pop("portfolioTitleEvidence")
    snapshot.pop("portfolioTitleGeneration")
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": "slot_local_life",
            "requirementLevel": "soft",
            "status": "pending_evidence",
        }
    ]

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "draftPassed": True},
        soft_slot_draft_adoption_enabled=True,
    )

    assert "proposal_title_generation_pending" not in report["blockingReasons"]


def test_zero_without_free_evidence_is_unknown_not_zero_budget():
    report = ProposalReadinessService.compute(
        _snapshot(estimated_cost=0), verifier={"passed": False, "hardFailures": ["route"]}
    )

    assert report["budgetStatus"] == "pending"
    assert report["budgetEstimate"] is None
    assert report["unknownCostSegmentCount"] == 2


def test_empty_or_filtered_snapshot_cannot_be_adopted():
    report = ProposalReadinessService.compute(
        {"days": [], "routeOptions": [], "portfolioPendingSlots": []},
        verifier={"passed": True},
    )

    assert report["routeStatus"] == "route_not_required"
    assert report["adoptionReady"] is False
    assert report["isPartial"] is True
    assert "proposal_has_no_visit_anchors" in report["blockingReasons"]


def test_missing_physical_stop_identity_fails_closed_without_shortcutting_neighbours():
    snapshot = _snapshot(estimated_cost=100)
    snapshot["days"][0]["segments"].insert(
        1,
        {
            "kind": "visit",
            "poi": {
                "name": "午餐",
                "amapId": "B000MEAL01",
                "source": "amap-place-search",
                "latitude": 39.915,
                "longitude": 116.315,
            },
            "semanticMetadata": {
                "routeAnchor": False,
                "requiresRouteEdge": True,
            },
        },
    )
    snapshot["routeOptions"] = [_verified_route(start="seg_1", end="seg_2")]

    assert ProposalReadinessService.expected_route_pairs(snapshot) == []

    report = ProposalReadinessService.compute(snapshot, verifier={"passed": True})

    assert report["routeStatus"] == "route_invalid"
    assert report["mapIdentityCoverage"] is False
    assert report["strictlyVerified"] is False
    assert report["adoptionReady"] is False
    assert "map_identity_incomplete" in report["blockingReasons"]


def test_readiness_rechecks_functional_facility_anchor_eligibility():
    snapshot = _snapshot(
        route_options=[_verified_route()],
        estimated_cost=100,
    )
    for segment, name in zip(
        snapshot["days"][0]["segments"],
        ("社区服务站", "养老服务驿站"),
    ):
        segment["poi"]["name"] = name
        segment["semanticMetadata"]["optionalExperienceFamily"] = "local_life"
        segment["semanticMetadata"]["portfolioOptional"] = True

    report = ProposalReadinessService.compute(snapshot, verifier={"passed": True})

    assert report["semanticCoverage"] is False
    assert report["adoptionReady"] is False
    assert "semantic_coverage_failed" in report["blockingReasons"]


def test_soft_pending_slot_can_be_an_explicit_editable_draft():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot["portfolioPendingSlots"] = [
        {"slotId": "slot_local_life", "requirementLevel": "soft", "family": "local_life", "status": "pending_evidence"}
    ]
    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "draftPassed": True, "softWarnings": ["soft_slot_pending"]},
        soft_slot_draft_adoption_enabled=True,
    )
    assert report["hardPendingSlotCount"] == 0
    assert report["softPendingSlotCount"] == 1
    assert report["structureReady"] is True
    assert report["draftAdoptionReady"] is True
    assert report["adoptionReady"] is True
    assert report["strictlyVerified"] is False
    assert report["isPartial"] is True
    assert report["adoptionMode"] == "editable_draft"
    assert "pending_slots_remaining" not in report["blockingReasons"]
    assert report["nextAction"] == "adopt_proposal"
    assert report["legacyNextAction"] == "adopt_editable_draft"
    assert report["nextActionLabel"] == "采用为可编辑草案（仍可补充 1 项）"


def test_authoritative_explicit_every_day_pending_occurrence_blocks_adoption_without_hardening_other_soft_slots():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": "day1_lunch",
            "planningSlotId": "day1_lunch",
            "poolId": "meal_pool",
            "dayNumber": 1,
            "intentType": "meal",
            "requirementLevel": "explicit_soft",
            "goalId": "goal_meal",
            "sourceGoalId": "goal_meal",
            "occurrenceId": "occ:goal_meal:day:1",
            "lineageAuthority": "simple_open_request_contract_every_day_meal",
            "userExplicit": True,
            "allowedDayNumbers": [1],
            "distributionPolicy": "every_allowed_day",
            "cardinalitySource": "explicit_every_day",
            "completionRequired": True,
            "groundingStatus": "unresolved",
        }
    ]

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "draftPassed": True},
        soft_slot_draft_adoption_enabled=True,
    )

    assert report["hardPendingSlotCount"] == 0
    assert report["softPendingSlotCount"] == 1
    assert report["completionRequiredPendingSlotCount"] == 1
    assert report["structureReady"] is False
    assert report["draftAdoptionReady"] is False
    assert report["adoptionReady"] is False
    assert report["strictlyVerified"] is False
    assert report["isPartial"] is True
    assert report["adoptionMode"] == "blocked"
    assert "pending_slots_remaining" in report["blockingReasons"]


def test_authoritative_daily_completion_pending_occurrence_blocks_adoption_directly():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=0, free=True)
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": "day1_daily_completion_1",
            "planningSlotId": "day1_daily_completion_1",
            "poolId": "daily_completion_day_1_pool",
            "dayNumber": 1,
            "intentType": "park",
            "requirementLevel": "inferred_preferred",
            "goalId": "goal_daily_completion_day_1",
            "sourceGoalId": "goal_daily_completion_day_1",
            "occurrenceId": "occ:goal_daily_completion_day_1:day:1",
            "lineageAuthority": "simple_open_daily_completion_policy",
            "userExplicit": False,
            "allowedDayNumbers": [1],
            "completionRequired": False,
            "dayCompletionRequired": True,
            "groundingStatus": "unresolved",
        }
    ]

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": True, "draftPassed": True},
        soft_slot_draft_adoption_enabled=True,
    )

    assert report["hardPendingSlotCount"] == 0
    assert report["softPendingSlotCount"] == 1
    assert report["completionRequiredPendingSlotCount"] == 1
    assert report["structureReady"] is False
    assert report["draftAdoptionReady"] is False
    assert report["adoptionReady"] is False
    assert report["strictlyVerified"] is False
    assert report["isPartial"] is True
    assert report["adoptionMode"] == "blocked"
    assert "pending_slots_remaining" in report["blockingReasons"]


def test_hard_pending_slot_still_blocks_draft_adoption():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot["portfolioPendingSlots"] = [
        {"slotId": "slot_required", "requirementLevel": "required", "family": "campus", "status": "pending_evidence"}
    ]
    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "draftPassed": False, "hardFailures": ["required_goal_missing"]},
        soft_slot_draft_adoption_enabled=True,
    )
    assert report["hardPendingSlotCount"] == 1
    assert report["draftAdoptionReady"] is False
    assert report["adoptionMode"] == "blocked"
    assert report["semanticCoverage"] is False
    assert report["nextAction"] == "continue_grounding_hard_slots"
    assert report["nextActionLabel"] == "补齐 1 个必选地点"


def test_hard_night_view_pending_has_specific_recoverable_action():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": f"slot_night_{index}",
            "requirementLevel": "required",
            "intentType": "night_view",
            "status": "pending_evidence",
        }
        for index in range(2)
    ]
    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "draftPassed": False},
        soft_slot_draft_adoption_enabled=True,
    )
    assert report["nextAction"] == "continue_grounding_hard_slots"
    assert report["nextActionLabel"] == "补齐 2 个必选夜景地点"
    assert report["adoptionReady"] is False


def test_soft_pending_completion_does_not_hide_route_shortfall():
    snapshot = _snapshot(estimated_cost=100)
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": f"slot_soft_{index}",
            "requirementLevel": "soft",
            "status": "pending_evidence",
        }
        for index in range(3)
    ]

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "draftPassed": False},
        soft_slot_draft_adoption_enabled=True,
    )

    assert report["hardPendingSlotCount"] == 0
    assert report["softPendingSlotCount"] == 3
    assert report["routeStatus"] == "route_pending"
    assert report["routeExpectedLegCount"] == 1
    assert report["routeVerifiedLegCount"] == 0
    assert report["nextAction"] == "complete_pending_slots"
    assert report["nextActionLabel"] == "补全 3 个待选体验"
    assert "route_evidence_incomplete" in report["blockingReasons"]


def test_pending_threshold_excess_is_authoritative_preview_only_not_adoptable():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_3",
            "kind": "visit",
            "estimatedCost": 0,
            "poi": {
                "name": "休息点",
                "amapId": "B00000003",
                "source": "amap-place-search",
                "latitude": 39.93,
                "longitude": 116.33,
            },
            "semanticMetadata": {"routeAnchor": False},
        }
    )
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": f"slot_soft_{index}",
            "dayNumber": 1,
            "requirementLevel": "explicit_soft",
            "status": "pending_evidence",
        }
        for index in range(6)
    ]
    snapshot["portfolioOutputQuality"] = {
        "mode": "enforce",
        "requestedTheme": "local_food_and_area_walk",
    }

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={
            "passed": False,
            "draftPassed": True,
            "softWarnings": ["soft_slot_pending"],
        },
        soft_slot_draft_adoption_enabled=True,
    )

    assert report["visibilityMode"] == "skeleton_preview_only"
    assert report["partialAdoptionReady"] is False


def test_production_hard_requirement_level_is_not_counted_as_soft():
    snapshot = _snapshot(route_options=[_verified_route()], estimated_cost=100)
    snapshot["portfolioPendingSlots"] = [
        {"slotId": "slot_hard", "requirementLevel": "hard", "status": "pending_evidence"}
    ]
    report = ProposalReadinessService.compute(
        snapshot,
        verifier={
            "passed": False,
            "draftPassed": False,
            "hardFailures": ["required_goal_missing"],
        },
        soft_slot_draft_adoption_enabled=True,
    )
    assert report["hardPendingSlotCount"] == 1
    assert report["softPendingSlotCount"] == 0
    assert report["draftAdoptionReady"] is False


def test_zero_provider_calls_never_offer_route_provider_retry_copy():
    snapshot = _snapshot(estimated_cost=100)
    snapshot["portfolioRouteQuality"] = {
        "status": "provider_error",
        "providerState": "precondition_failed",
        "executionLedger": {
            "expectedLegCount": 1,
            "requestedLegCount": 1,
            "providerCallCount": 0,
            "providerCacheHitCount": 0,
            "routePreconditionFailureReason": "route_provider_not_invoked",
        },
        "routeQualityIssues": [{"code": "route_provider_precondition_failed"}],
    }

    report = ProposalReadinessService.compute(
        snapshot,
        verifier={"passed": False, "hardFailures": ["route_evidence_incomplete"]},
    )

    assert report["routeProviderAttemptCount"] == 0
    assert report["routeRetryable"] is False
    assert report["nextAction"] == "none"
    assert report["nextActionLabel"] == "路线前置条件未满足"

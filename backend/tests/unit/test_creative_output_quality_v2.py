from __future__ import annotations

from time import monotonic, sleep

from src.services.creative_output_quality_service import (
    CreativeOutputQualityService,
    ThemeCompletionBudget,
)
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.creative_theme_completion_service import (
    CreativeThemeCompletionService,
)
from src.services.consumer_candidate_admission_service import (
    ConsumerCandidateAdmissionService,
)
from src.services.creative_planning_models import ConstraintLedger, GoalRequirement
from src.services.goal_ledger_service import GoalLedgerService
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.proposal_readiness_service import ProposalReadinessService


def _poi(index: int, name: str) -> dict:
    return {
        "name": name,
        "amapId": f"B0000000{index}",
        "source": "amap-place-search",
        "latitude": 39.90 + index / 100,
        "longitude": 116.30 + index / 100,
    }


def _segment(index: int, name: str, *, family: str = "campus", route_anchor: bool = True) -> dict:
    return {
        "id": f"seg_{index}",
        "kind": "meal" if family == "local_food" else "visit",
        "startTime": f"{8 + index:02d}:00",
        "endTime": f"{9 + index:02d}:00",
        "poi": _poi(index, name),
        "semanticMetadata": {
            "routeAnchor": route_anchor,
            "optionalExperienceFamily": family,
            "consumerAdmissionReport": {
                "classification": "admitted_final_anchor" if family == "local_food" else "admitted_anchor_set_member",
                "scoreEligible": True,
            },
        },
    }


def _ledger() -> ConstraintLedger:
    return ConstraintLedger(
        schemaVersion="constraint-ledger-v1",
        city="北京",
        dayCount=2,
        hardGoals=[
            GoalRequirement(
                goalId="goal_campus",
                intentType="campus_visit",
                requiredMin=1,
                preferredCount=2,
                maxCount=2,
                cardinalitySource="theme_spans_trip_days",
                allowedDayNumbers=[1, 2],
            ),
            GoalRequirement(
                goalId="goal_night",
                intentType="night_view",
                requiredMin=2,
                preferredCount=2,
                maxCount=2,
                cardinalitySource="explicit_every_day",
                allowedDayNumbers=[1, 2],
            ),
        ],
        softGoals=[
            GoalRequirement(
                goalId="goal_meal",
                intentType="meal",
                requiredMin=2,
                preferredCount=2,
                maxCount=2,
                cardinalitySource="explicit_every_day",
                allowedDayNumbers=[1, 2],
                userExplicit=True,
                priorityTier="explicit_soft",
            )
        ],
        sourceFingerprint="a" * 64,
    )


def test_golden_request_requires_one_night_cardinality_clarification():
    ledger = GoalLedgerService().from_message(
        "今年国庆参观北京高校两日游，晚上看北京城市夜景。每天午餐想体验当地特色美食。",
        day_count=2,
        clarify_ambiguous_night=True,
    )

    night = ledger.goal("night_view")
    meal = ledger.goal("meal")
    campus = ledger.goal("campus_visit")
    assert night.source == "clarification_required"
    assert night.min_count == 1
    assert night.preferred_count == 2
    assert night.limitation_reason == "night_view_cardinality_ambiguous"
    assert campus.min_count == 2 and campus.preferred_count == 2
    assert campus.source == "multi_day_daily_template"
    assert meal.min_count == 2 and meal.source == "explicit_every_day"


def test_goal_occurrences_preserve_five_level_priority_and_explicit_meals():
    plan = GoalOccurrenceCompiler().compile(
        _ledger(),
        {
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["goal_campus", "goal_night", "goal_meal"],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["goal_campus", "goal_night", "goal_meal"],
                },
            ]
        },
    )

    by_key = {(item.source_goal_id, item.day_number): item for item in plan.occurrences}
    assert by_key[("goal_campus", 1)].requirement_level == "hard"
    assert by_key[("goal_campus", 2)].requirement_level == "inferred_preferred"
    assert by_key[("goal_night", 1)].requirement_level == "hard"
    assert by_key[("goal_night", 2)].requirement_level == "hard"
    assert by_key[("goal_meal", 1)].requirement_level == "explicit_soft"
    assert by_key[("goal_meal", 2)].requirement_level == "explicit_soft"
    assert by_key[("goal_meal", 1)].user_explicit is True


def test_unearned_food_and_neighborhood_theme_is_neutral_and_has_completion_action():
    snapshot = {
        "title": "地方饮食与街区",
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": [_segment(1, "清华大学")]}],
        "portfolioPendingSlots": [],
    }

    result = CreativeOutputQualityService.evaluate(snapshot, requested_theme="local_food_and_area_walk")

    assert result["themeEligible"] is False
    assert result["visibilityMode"] == "neutral_skeleton"
    assert result["displayTitle"] == "方案待补全"
    assert result["completionAction"]["kind"] == "portfolio_theme_completion"
    assert result["completionAction"]["label"] == "尝试补全“地方饮食与街区”"


def test_unverified_creative_directions_use_the_same_structured_status_title():
    base = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": [_segment(1, "清华大学")]}],
        "portfolioPendingSlots": [],
    }
    food = CreativeOutputQualityService.evaluate(
        {
            **base,
            "title": "地方饮食与街区｜北京行程",
            "creativeBrief": {"briefId": "fallback_1_food_led", "title": "地方饮食与街区"},
        }
    )
    night = CreativeOutputQualityService.evaluate(
        {
            **base,
            "title": "光影夜游｜北京行程",
            "creativeBrief": {"briefId": "fallback_2_photo_night", "title": "光影夜游"},
        }
    )

    assert food["themeEligible"] is False
    assert night["themeEligible"] is False
    assert food["displayTitle"] == "方案待补全"
    assert night["displayTitle"] == "方案待补全"


def test_theme_requires_one_meal_two_distinct_area_anchors_and_walking_relation():
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment(1, "北京地方菜馆", family="local_food"),
                    _segment(2, "街区甲", family="local_life"),
                    _segment(3, "街区乙", family="local_life"),
                ],
            }
        ],
        "portfolioPendingSlots": [],
        "routeOptions": [
            {
                "fromSegmentId": "seg_2",
                "toSegmentId": "seg_3",
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 900,
                "distanceMeters": 1000,
                "provider": "amap-webservice",
                "source": "amap-webservice",
            }
        ],
    }

    for segment in snapshot["days"][0]["segments"]:
        segment["semanticMetadata"]["briefId"] = "brief_local_food_walk"

    result = CreativeOutputQualityService.evaluate(snapshot, requested_theme="local_food_and_area_walk")

    assert result["themeEligible"] is True
    assert result["admittedLocalMealCount"] == 1
    assert result["distinctAreaWalkPhysicalAnchorCount"] == 2
    assert result["areaWalkWalkingRelationVerified"] is True
    assert result["visibilityMode"] == "themed"


def test_complete_proposal_does_not_accept_legacy_content_title_projection():
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment(1, "北京地方菜馆", family="local_food"),
                    _segment(2, "街区甲", family="local_life"),
                    _segment(3, "街区乙", family="local_life"),
                ],
            }
        ],
        "portfolioPendingSlots": [],
        "routeOptions": [
            {
                "fromSegmentId": "seg_2",
                "toSegmentId": "seg_3",
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 900,
                "distanceMeters": 1000,
                "provider": "amap-webservice",
                "source": "amap-webservice",
            }
        ],
    }
    for segment in snapshot["days"][0]["segments"]:
        segment["semanticMetadata"]["briefId"] = "brief_local_food_walk"
    legacy = CreativeProposalTitleService.apply(snapshot, city="北京")
    legacy["portfolioVerifier"] = {"passed": True}

    result = CreativeOutputQualityService.evaluate(
        legacy,
        requested_theme="local_food_and_area_walk",
    )

    assert result["themeEligible"] is True
    assert result["displayTitle"] == "标题生成待重试"
    assert "portfolioTitleEvidence" not in legacy


def test_neutral_partial_quality_floor_enforces_ratio_and_per_day_caps():
    snapshot = {
        "city": "北京",
        "days": [
            {"dayNumber": 1, "segments": [_segment(1, "高校甲"), _segment(2, "夜景甲")]},
            {"dayNumber": 2, "segments": [_segment(3, "高校乙")]},
        ],
        "portfolioPendingSlots": [
            {"planningSlotId": "meal_1", "dayNumber": 1, "requirementLevel": "explicit_soft"},
            {"planningSlotId": "meal_2", "dayNumber": 2, "requirementLevel": "explicit_soft"},
        ],
    }
    result = CreativeOutputQualityService.evaluate(snapshot, requested_theme="local_food_and_area_walk")
    assert result["neutralPartialQualityPassed"] is True
    assert result["pendingRatio"] == 0.4

    snapshot["portfolioPendingSlots"].extend(
        {"planningSlotId": f"extra_{index}", "dayNumber": 1, "requirementLevel": "optional"} for index in range(3)
    )
    blocked = CreativeOutputQualityService.evaluate(snapshot, requested_theme="local_food_and_area_walk")
    assert blocked["neutralPartialQualityPassed"] is False
    assert "pending_per_day_exceeded" in blocked["qualityFailureReasons"]
    assert blocked["visibilityMode"] == "skeleton_preview_only"


def test_theme_fails_closed_for_invalid_amap_identity_or_zero_coordinates():
    meal = _segment(40, "地方菜馆", family="local_food")
    first = _segment(41, "街区甲", family="local_life")
    second = _segment(42, "街区乙", family="local_life")
    for segment in (meal, first, second):
        segment["semanticMetadata"]["briefId"] = "brief_strict_identity"
    meal["poi"]["amapId"] = "Bfake"
    first["poi"]["latitude"] = 0
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": [meal, first, second]}],
        "portfolioThemeWalkingEvidence": [
            {
                "fromSegmentId": first["id"],
                "toSegmentId": second["id"],
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 15 * 60,
            }
        ],
    }

    result = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )

    assert result["themeEligible"] is False
    assert result["groundedSegmentCount"] == 1
    assert result["admittedLocalMealCount"] == 0
    assert result["distinctAreaWalkPhysicalAnchorCount"] == 1


def test_theme_walking_relation_requires_nonempty_matching_brief_provenance():
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment(50, "地方菜馆", family="local_food"),
                    _segment(51, "街区甲", family="local_life"),
                    _segment(52, "街区乙", family="local_life"),
                ],
            }
        ],
        "portfolioThemeWalkingEvidence": [
            {
                "fromSegmentId": "seg_51",
                "toSegmentId": "seg_52",
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 15 * 60,
            }
        ],
    }

    result = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )

    assert result["themeEligible"] is False
    assert result["areaWalkWalkingRelationVerified"] is False


def test_all_real_timed_segments_include_meal_in_route_target_but_pending_does_not():
    meal = _segment(2, "午餐", family="local_food", route_anchor=False)
    snapshot = {
        "days": [{"dayNumber": 1, "segments": [_segment(1, "高校"), meal]}],
        "portfolioPendingSlots": [{"planningSlotId": "future", "dayNumber": 1, "requirementLevel": "optional"}],
    }

    assert ProposalReadinessService.expected_route_pairs(snapshot) == [
        {"fromSegmentId": "seg_1", "toSegmentId": "seg_2", "dayNumber": 1}
    ]


def test_pending_placeholder_with_route_anchor_metadata_is_not_a_grounded_route_target():
    pending = {
        "id": "pending_seg",
        "kind": "pending",
        "startTime": "12:00",
        "endTime": "13:00",
        "semanticMetadata": {
            "routeAnchor": True,
            "routeAnchorExpected": True,
            "groundingStatus": "waiting_for_poi_grounding",
        },
    }
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment(1, "高校"),
                    pending,
                    _segment(2, "夜景"),
                ],
            }
        ],
        "portfolioPendingSlots": [],
    }

    assert ProposalReadinessService.expected_route_pairs(snapshot) == [
        {"fromSegmentId": "seg_1", "toSegmentId": "seg_2", "dayNumber": 1}
    ]
    assert ProposalReadinessService.density_targets(snapshot)["groundedRouteAnchorTargets"] == {"1": 2}


def test_readiness_separates_desired_grounded_and_future_pending_anchor_targets():
    snapshot = {
        "portfolioDayAnchorTargets": {"1": 4, "2": 4},
        "days": [
            {"dayNumber": 1, "segments": [_segment(1, "高校"), _segment(2, "午餐", family="local_food")]},
            {"dayNumber": 2, "segments": [_segment(3, "夜景")]},
        ],
        "portfolioPendingSlots": [
            {"planningSlotId": "meal_2", "dayNumber": 2, "futureRouteAnchor": True},
            {"planningSlotId": "note_only", "dayNumber": 1, "futureRouteAnchor": False},
        ],
    }

    result = ProposalReadinessService.compute(snapshot, verifier={})

    assert result["desiredDensityAnchorTargets"] == {"1": 4, "2": 4}
    assert result["groundedRouteAnchorTargets"] == {"1": 2, "2": 1}
    assert result["pendingFutureAnchorTargets"] == {"1": 0, "2": 1}


def test_theme_completion_budget_is_bounded_and_independent():
    budget = ThemeCompletionBudget()
    assert budget.deadline_seconds == 20
    assert budget.amap_detail_limit == 4
    assert budget.identity_web_limit == 2
    assert budget.auto_attempt_limit == 1
    assert budget.to_trace()["writeAuthority"] == "none"


def test_completion_detail_budget_is_fair_between_explicit_meals_before_area():
    detail_order: list[str] = []

    def detail(candidate: dict) -> dict:
        detail_order.append(candidate["amapId"])
        return {**candidate, "detailEvidence": True}

    result = CreativeThemeCompletionService().complete(
        meal_candidates_by_slot={
            "meal_day_1": [{"amapId": "B00000001"}, {"amapId": "B00000002"}],
            "meal_day_2": [{"amapId": "B00000003"}, {"amapId": "B00000004"}],
        },
        area_candidates=[{"amapId": "B00000005"}],
        detail_fetcher=detail,
        identity_web_fetcher=lambda candidate: {
            "amapId": candidate["amapId"],
            "identityBound": True,
        },
        admission=lambda candidate: bool(candidate.get("detailEvidence")),
    )

    assert detail_order[:2] == ["B00000001", "B00000003"]
    assert result["trace"]["mealFairFirstPassCount"] == 2
    assert result["trace"]["amapDetailAttemptCount"] == 4
    assert result["trace"]["identitySpecificWebQueryCount"] <= 2
    assert result["trace"]["writeAuthority"] == "none"
    assert result["trace"]["versionWriteCount"] == 0


def test_completion_rejects_web_claim_without_amap_identity_and_stops_at_deadline():
    ticks = iter((0.0, 0.0, 21.0, 21.0))
    result = CreativeThemeCompletionService().complete(
        meal_candidates_by_slot={"meal_day_1": [{"name": "泛城市美食攻略"}]},
        area_candidates=[],
        detail_fetcher=lambda candidate: candidate,
        identity_web_fetcher=lambda candidate: {"identityBound": False},
        admission=lambda candidate: False,
        monotonic=lambda: next(ticks),
    )

    assert result["status"] == "deadline_reached"
    assert result["reasonCode"] == "deadline_exhausted"
    assert result["trace"]["identitySpecificWebQueryCount"] == 0
    assert result["trace"]["versionWriteCount"] == 0


def test_completion_enforces_wall_clock_deadline_around_blocking_provider_callback():
    started = monotonic()
    result = CreativeThemeCompletionService(ThemeCompletionBudget(deadline_seconds=0.01)).complete(
        meal_candidates_by_slot={"meal_day_1": [{"amapId": "B00000001"}]},
        area_candidates=[],
        detail_fetcher=lambda candidate: (sleep(0.08), candidate)[1],
        identity_web_fetcher=lambda candidate: {},
        admission=lambda candidate: False,
    )

    assert monotonic() - started < 0.06
    assert result["status"] == "deadline_reached"
    assert result["reasonCode"] == "deadline_exhausted"
    assert result["trace"]["themeCompletionTerminalStatus"] == "deadline_reached"
    assert result["trace"]["versionWriteCount"] == 0


def test_completion_provider_exception_is_typed_and_zero_write():
    def failed_detail(_candidate: dict) -> dict:
        raise TimeoutError("secret upstream URL and query must not escape")

    result = CreativeThemeCompletionService().complete(
        meal_candidates_by_slot={"meal_day_1": [{"amapId": "B00000001"}]},
        area_candidates=[],
        detail_fetcher=failed_detail,
        identity_web_fetcher=lambda candidate: {},
        admission=lambda candidate: False,
    )

    assert result["status"] == "provider_failed"
    assert result["reasonCode"] == "amap_detail_provider_failed"
    assert "secret" not in str(result)
    assert result["trace"]["versionWriteCount"] == 0


def test_completion_materializes_only_admitted_addition_and_preserves_other_pending():
    snapshot = {
        "days": [{"dayNumber": 1, "segments": [_segment(1, "高校")]}],
        "portfolioDayAnchorTargets": {"1": 3},
        "portfolioPendingSlots": [
            {
                "briefId": "food",
                "poolId": "meal_pool",
                "planningSlotId": "meal_day_1",
                "dayNumber": 1,
                "startTime": "12:00",
                "durationMinutes": 60,
                "optionalExperienceFamily": "local_food",
                "futureRouteAnchor": True,
                "requirementLevel": "explicit_soft",
            },
            {
                "briefId": "food",
                "poolId": "walk_pool",
                "planningSlotId": "walk_day_1",
                "dayNumber": 1,
                "futureRouteAnchor": True,
                "requirementLevel": "defining_theme",
            },
        ],
    }
    admitted = {
        **_poi(8, "北京地方菜馆"),
        "planningSlotId": "meal_day_1",
        "optionalExperienceFamily": "local_food",
        "coverageRoles": ["local_food", "area_walk_anchor"],
        "coverageRoleAdmissionReports": {
            "area_walk_anchor": {
                "classification": "admitted_anchor_set_member",
                "scoreEligible": True,
            }
        },
        "consumerAdmissionReport": {
            "classification": "admitted_final_anchor",
            "scoreEligible": True,
        },
    }

    projected, added_ids = CreativeThemeCompletionService.apply_admitted_additions(snapshot, [admitted])

    assert len(added_ids) == 1
    assert [item["planningSlotId"] for item in projected["portfolioPendingSlots"]] == ["walk_day_1"]
    meal = next(item for item in projected["days"][0]["segments"] if item["kind"] == "meal")
    assert meal["poi"]["source"] == "amap-place-search"
    assert meal["startTime"] == "12:00" and meal["endTime"] == "13:00"
    assert meal["semanticMetadata"]["coverageRoles"] == [
        "local_food",
        "area_walk_anchor",
    ]
    assert meal["semanticMetadata"]["coverageRoleAdmissionReports"]["area_walk_anchor"]["scoreEligible"] is True
    assert projected["groundedRouteAnchorTargets"] == {"1": 2}
    assert projected["pendingFutureAnchorTargets"] == {"1": 1}


def test_completion_does_not_materialize_noncanonical_amap_candidate():
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": []}],
        "portfolioPendingSlots": [
            {
                "planningSlotId": "meal_invalid",
                "dayNumber": 1,
                "requirementLevel": "defining_theme",
            }
        ],
    }
    invalid = {
        **_poi(9, "无效地方菜馆"),
        "latitude": 0,
        "planningSlotId": "meal_invalid",
        "optionalExperienceFamily": "local_food",
        "consumerAdmissionReport": {
            "classification": "admitted_final_anchor",
            "scoreEligible": True,
        },
    }

    projected, added_ids = CreativeThemeCompletionService.apply_admitted_additions(
        snapshot,
        [invalid],
    )

    assert added_ids == []
    assert projected["days"][0]["segments"] == []
    assert [item["planningSlotId"] for item in projected["portfolioPendingSlots"]] == ["meal_invalid"]


def test_completion_preserves_provider_evidence_and_matches_admission_report():
    candidate = {
        "id": "B000000088",
        "amapId": "B000000088",
        "name": "北京地方风味餐厅",
        "type": "餐饮服务;中餐厅;特色餐厅",
        "category": "特色餐厅",
        "providerTypeCode": "050101",
        "tags": ["地方风味", "中餐"],
        "aliases": ["地方风味馆"],
        "businessArea": "海淀",
        "address": "学院路 8 号",
        "district": "海淀区",
        "city": "北京",
        "longitude": 116.36,
        "latitude": 39.98,
        "source": "amap-place-search",
        "openTimeToday": "11:00-21:30",
        "openTimeWeek": "周一至周日 11:00-21:30",
        "parentPoiId": "B000000080",
        "rating": 4.5,
        "cost": 68,
        "routeDetourMinutes": 9,
        "children": [{"id": "B0FF000088", "name": "餐厅入口", "type": "出入口"}],
        "sourceClaims": [
            {
                "claimType": "local_food_context",
                "stance": "support",
                "sourceName": "public-guide",
                "sourceUrlHash": "c" * 64,
                "summary": "面向周边居民的地方风味餐厅",
                "locality": "北京",
            }
        ],
        "briefId": "food",
        "poolId": "meal_pool",
        "planningSlotId": "meal_day_1",
        "optionalExperienceFamily": "local_food",
    }
    consumer = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id="food",
        pool_id="meal_pool",
        planning_slot_id="meal_day_1",
        day_number=1,
        city="北京",
        family="meal",
        optional_experience_family="local_food",
        activity_mode="meal",
        requirement_level="explicit_soft",
        experience_shape="single_poi",
        experience_goal="当地特色美食",
        evidence_requirements={"minimumIndependentClaims": 1},
    )
    report = ConsumerCandidateAdmissionService().evaluate(candidate, consumer)
    assert report["classification"] == "admitted_final_anchor"
    candidate["consumerAdmissionReport"] = report
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": []}],
        "portfolioPendingSlots": [
            {
                "briefId": "food",
                "poolId": "meal_pool",
                "planningSlotId": "meal_day_1",
                "dayNumber": 1,
                "startTime": "12:00",
                "durationMinutes": 60,
                "optionalExperienceFamily": "local_food",
                "requirementLevel": "explicit_soft",
            }
        ],
    }

    projected, added_ids = CreativeThemeCompletionService.apply_admitted_additions(
        snapshot,
        [candidate],
    )

    assert len(added_ids) == 1
    poi = projected["days"][0]["segments"][0]["poi"]
    assert poi["parentPoiId"] == "B000000080"
    assert poi["openTimeToday"] == "11:00-21:30"
    assert poi["tags"] == ["地方风味", "中餐"]
    assert poi["sourceClaims"][0]["claimType"] == "local_food_context"
    assert ConsumerCandidateAdmissionService.report_matches_poi(report, poi) is True


def test_completion_does_not_materialize_amap_child_as_a_second_physical_place():
    parent = _segment(40, "奥林匹克塔", family="night_view")
    parent["poi"]["amapId"] = "B000AA3ZCC"
    child = {
        **_poi(41, "奥林匹克塔观景台"),
        "amapId": "B0FFILD7HG",
        "parentPoiId": "B000AA3ZCC",
        "planningSlotId": "night_day_2",
        "optionalExperienceFamily": "night_view",
        "consumerAdmissionReport": {
            "classification": "admitted_final_anchor",
            "scoreEligible": True,
        },
    }
    snapshot = {
        "city": "北京",
        "days": [
            {"dayNumber": 1, "segments": [parent]},
            {"dayNumber": 2, "segments": []},
        ],
        "portfolioPendingSlots": [
            {
                "planningSlotId": "night_day_2",
                "dayNumber": 2,
                "optionalExperienceFamily": "night_view",
                "requirementLevel": "hard",
            }
        ],
    }

    projected, added_ids = CreativeThemeCompletionService.apply_admitted_additions(
        snapshot,
        [child],
    )

    assert added_ids == []
    assert projected["days"][1]["segments"] == []
    assert projected["portfolioPendingSlots"][0]["planningSlotId"] == "night_day_2"


def test_parent_and_child_count_as_one_area_anchor_for_theme_quality():
    parent = _segment(42, "历史街区", family="local_life")
    child = _segment(43, "历史街区观景平台", family="market_walk")
    child["poi"]["parentPoiId"] = parent["poi"]["amapId"]
    for segment in (parent, child):
        segment["semanticMetadata"]["briefId"] = "brief_parent_child"
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": [parent, child]}],
        "portfolioThemeWalkingEvidence": [
            {
                "fromSegmentId": parent["id"],
                "toSegmentId": child["id"],
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 600,
            }
        ],
    }

    result = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )

    assert result["distinctAreaWalkPhysicalAnchorCount"] == 1
    assert result["themeEligible"] is False


def test_area_walk_relation_must_be_same_day_and_same_authorized_brief():
    first = _segment(1, "街区甲", family="local_life")
    second = _segment(2, "街区乙", family="local_life")
    first["semanticMetadata"]["briefId"] = "brief_a"
    second["semanticMetadata"]["briefId"] = "brief_b"
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [_segment(3, "北京地方菜馆", family="local_food"), first],
            },
            {"dayNumber": 2, "segments": [second]},
        ],
        "routeOptions": [
            {
                "fromSegmentId": first["id"],
                "toSegmentId": second["id"],
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 900,
            }
        ],
    }

    result = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )

    assert result["themeEligible"] is False
    assert result["areaWalkWalkingRelationVerified"] is False


def test_admitted_meal_can_be_one_area_anchor_without_duplicate_segment():
    meal = _segment(20, "北京地方菜馆", family="local_food")
    market = _segment(21, "公开市场", family="market_walk")
    for segment in (meal, market):
        segment["semanticMetadata"]["briefId"] = "brief_dual_role"
        segment["semanticMetadata"]["creativeBriefId"] = "brief_dual_role"
    meal["semanticMetadata"]["coverageRoles"] = [
        "local_food",
        "area_walk_anchor",
    ]
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": [meal, market]}],
        "portfolioThemeWalkingEvidence": [
            {
                "fromSegmentId": meal["id"],
                "toSegmentId": market["id"],
                "mode": "walking",
                "status": "verified",
                "durationSeconds": 15 * 60,
            }
        ],
    }

    unverified_role = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )
    assert unverified_role["themeEligible"] is False
    meal["semanticMetadata"]["coverageRoleAdmissionReports"] = {
        "area_walk_anchor": {
            "classification": "admitted_anchor_set_member",
            "scoreEligible": True,
        }
    }
    result = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )

    assert result["themeEligible"] is True
    assert result["admittedLocalMealCount"] == 1
    assert result["distinctAreaWalkPhysicalAnchorCount"] == 2
    assert len(snapshot["days"][0]["segments"]) == 2


def test_hard_replacement_is_a_zero_write_diff_until_confirmed():
    result = CreativeThemeCompletionService.replacement_diff(
        before=[
            {
                "segmentId": "seg_hard",
                "requirementLevel": "hard",
                "amapId": "B00000001",
                "userLocked": False,
                "intentOccurrenceId": "occ:campus:1",
                "evidenceScore": 0.8,
                "hardCoverageCount": 2,
                "scheduleFeasible": True,
            }
        ],
        after=[
            {
                "segmentId": "seg_hard",
                "requirementLevel": "hard",
                "amapId": "B00000002",
                "userLocked": False,
                "intentOccurrenceId": "occ:campus:1",
                "sameIntentOccurrence": True,
                "replacementAdmission": "admitted",
                "evidenceScore": 0.9,
                "hardCoverageCount": 2,
                "scheduleFeasible": True,
                "travelMinutesSaved": 20,
            }
        ],
    )

    assert result["requiresConfirmation"] is True
    assert result["hardReplacements"][0]["beforeAmapId"] == "B00000001"
    assert result["hardReplacements"][0]["afterAmapId"] == "B00000002"
    assert result["versionWriteCount"] == 0
    assert result["patchWriteCount"] == 0


def test_hard_replacement_without_all_policy_gates_or_material_improvement_is_blocked():
    result = CreativeThemeCompletionService.replacement_diff(
        before=[
            {
                "segmentId": "seg_hard",
                "requirementLevel": "hard",
                "amapId": "B00000001",
                "userLocked": False,
                "intentOccurrenceId": "occ:campus:1",
                "evidenceScore": 0.9,
                "hardCoverageCount": 2,
                "scheduleFeasible": True,
            }
        ],
        after=[
            {
                "segmentId": "seg_hard",
                "requirementLevel": "hard",
                "amapId": "B00000002",
                "userLocked": False,
                "intentOccurrenceId": "occ:campus:1",
                "sameIntentOccurrence": True,
                "replacementAdmission": "admitted",
                "evidenceScore": 0.8,
                "hardCoverageCount": 2,
                "scheduleFeasible": True,
                "travelMinutesSaved": 14,
                "transfersReduced": 1,
                "continuousWalkingKmReduced": 0.9,
                "enablesThemeClosure": False,
            }
        ],
    )

    assert result["requiresConfirmation"] is False
    assert result["hardReplacements"] == []
    assert result["blockedHardReplacements"][0]["reasonCodes"] == [
        "replacement_evidence_regressed",
        "replacement_material_improvement_missing",
    ]
    assert result["versionWriteCount"] == 0
    assert result["patchWriteCount"] == 0


def test_reject_hard_replacement_restores_original_and_keeps_compatible_addition():
    before = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "seg_hard",
                        "poi": {"amapId": "B00000001", "source": "amap-place-search"},
                        "semanticMetadata": {
                            "requirementLevel": "hard",
                            "occurrenceId": "occ:campus:1",
                        },
                    }
                ],
            }
        ]
    }
    proposed = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "seg_hard",
                        "poi": {"amapId": "B00000002", "source": "amap-place-search"},
                        "semanticMetadata": {
                            "requirementLevel": "hard",
                            "occurrenceId": "occ:campus:1",
                        },
                    },
                    {
                        "id": "seg_added_meal",
                        "poi": {"amapId": "B00000003", "source": "amap-place-search"},
                        "semanticMetadata": {"requirementLevel": "explicit_soft"},
                    },
                ],
            }
        ]
    }

    rejected = CreativeThemeCompletionService.reject_hard_replacements(
        before_snapshot=before,
        proposed_snapshot=proposed,
    )

    segments = rejected["days"][0]["segments"]
    assert [item["id"] for item in segments] == ["seg_hard", "seg_added_meal"]
    assert segments[0]["poi"]["amapId"] == "B00000001"
    assert segments[1]["poi"]["amapId"] == "B00000003"
    assert rejected["portfolioRouteEvidence"] == []
    assert rejected["routeEvidenceInvalidationReason"] == "hard_replacement_rejected"

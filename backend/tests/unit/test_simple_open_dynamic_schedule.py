from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.api.schemas.agent import AgentInitialPlanOutput
from src.models.poi import POI
from src.models.poi_intent import PersistableSegmentPlan
from src.services.simple_open_dynamic_schedule_service import SimpleOpenDynamicScheduleService
from src.services.agent_service import AgentService
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.goal_ledger_service import GoalLedgerService
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.meal_grounding_policy import MealGroundingPolicy


def _park_plan(
    *,
    trip_date: str,
    longitude: float,
    latitude: float,
    open_time_today: str | None,
    explicit_start: str | None = None,
) -> PersistableSegmentPlan:
    constraints = {"explicitStartTime": explicit_start} if explicit_start else {}
    return PersistableSegmentPlan(
        day_number=1,
        date=trip_date,
        start_time="11:30",
        duration_minutes=80,
        kind="park",
        route_anchor=True,
        selected_poi=POI(
            id="poi_park",
            amap_id="B000PARK01",
            name="测试公园",
            city="北京",
            category="scenic",
            latitude=latitude,
            longitude=longitude,
            source="amap-place-search",
            confidence=1.0,
            type="风景名胜;公园广场;公园",
            open_time_today=open_time_today,
        ),
        display_title="测试公园",
        notes="",
        grounding_status="verified_amap",
        ticket_status="not_checked",
        raw_need="晚上逛公园",
        intent_type="park",
        goal_id="goal_park",
        source_goal_id="goal_park",
        occurrence_id="occ:goal_park:day:1",
        planning_slot_id="day1_evening_park",
        pool_id="park_pool",
        lineage_authority="goal_occurrence_compiler",
        requirement_level="hard",
        required=True,
        schedule_preference={
            "dayPart": "evening",
            "intentType": "park",
            "preferredDayNumbers": [1],
            "userExplicit": True,
            "priority": 90,
            "sourceGoalId": "goal_park",
            "occurrenceId": "occ:goal_park:day:1",
        },
        schedule_constraints=constraints,
    )


def test_evening_is_solar_and_context_driven_not_a_fixed_clock_window() -> None:
    service = SimpleOpenDynamicScheduleService()
    first = _park_plan(
        trip_date="2026-06-21",
        longitude=116.397,
        latitude=39.904,
        open_time_today="06:00-23:00",
    )
    second = _park_plan(
        trip_date="2026-12-21",
        longitude=116.397,
        latitude=39.904,
        open_time_today="06:00-23:00",
    )

    summer = service.schedule([first])[0]
    winter = service.schedule([second])[0]

    assert summer.start_time != winter.start_time
    assert summer.schedule_decision["solarBoundarySource"] == "local_sunset_from_date_and_coordinates"
    assert winter.schedule_decision["solarBoundarySource"] == "local_sunset_from_date_and_coordinates"
    assert summer.schedule_decision["decisionSource"] == "dynamic_schedule_solver"
    assert winter.schedule_decision["decisionSource"] == "dynamic_schedule_solver"
    assert summer.schedule_decision["constraintPassed"] is True
    assert winter.schedule_decision["constraintPassed"] is True
    assert summer.schedule_constraints.get("earliestStart") is None
    assert winter.schedule_constraints.get("earliestStart") is None


def test_explicit_user_clock_remains_hard_while_missing_opening_is_provisional() -> None:
    service = SimpleOpenDynamicScheduleService()
    plan = _park_plan(
        trip_date="2026-10-01",
        longitude=116.397,
        latitude=39.904,
        open_time_today=None,
        explicit_start="20:30",
    )

    scheduled = service.schedule([plan])[0]

    assert scheduled.start_time == "20:30"
    assert scheduled.schedule_decision["openingEvidenceStatus"] == "unverified"
    assert scheduled.schedule_decision["scheduleConfidence"] == "provisional"
    assert "opening_hours_unverified" in scheduled.schedule_decision["provisionalReasons"]
    assert scheduled.schedule_decision["constraintPassed"] is True


def test_known_closing_before_local_evening_fails_closed() -> None:
    service = SimpleOpenDynamicScheduleService()
    plan = _park_plan(
        trip_date="2026-06-21",
        longitude=116.397,
        latitude=39.904,
        open_time_today="06:00-17:00",
    )

    scheduled = service.schedule([plan])[0]

    assert scheduled.schedule_decision["constraintPassed"] is False
    assert scheduled.schedule_decision["failureReason"] == "opening_window_does_not_overlap_semantic_evening"
    assert scheduled.start_time == ""


def test_solar_boundary_changes_with_location_for_same_date() -> None:
    service = SimpleOpenDynamicScheduleService()
    beijing = service.local_sunset_minutes(date(2026, 10, 1), latitude=39.904, longitude=116.397)
    urumqi = service.local_sunset_minutes(date(2026, 10, 1), latitude=43.825, longitude=87.617)

    assert beijing is not None
    assert urumqi is not None
    assert beijing != urumqi


def test_evening_park_request_compiles_semantic_preference_without_a_fixed_clock() -> None:
    service = object.__new__(AgentService)
    contract = service._request_intent_contract(
        "北京两日游，晚上逛公园",
        {"dates": ["2026-10-01", "2026-10-02"]},
        SimpleNamespace(meal_slots=[]),
    )
    park = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_park")

    assert park["requiredMin"] == 2
    assert park["distributionPolicy"] == "every_allowed_day"
    assert park["requirementLevel"] == "required"
    assert park["timeWindow"] is None
    assert park["schedulePreference"]["dayPart"] == "evening"
    assert not any(clock in str(park) for clock in ("18:00", "19:00", "20:30", "22:00", "23:00"))

    night_contract = service._request_intent_contract(
        "北京两日游，晚上看北京夜景",
        {"dates": ["2026-10-01", "2026-10-02"]},
        SimpleNamespace(meal_slots=[]),
    )
    night = next(item for item in night_contract["requiredIntents"] if item["goalId"] == "goal_night_view")
    assert night["timeWindow"] is None
    assert night["schedulePreference"]["dayPart"] == "evening"
    assert not any(clock in str(night) for clock in ("18:00", "19:00", "20:30", "22:00", "23:00"))


@pytest.mark.parametrize("evening_clause", ["每晚都去逛公园", "每夜都去逛公园", "每天晚上都去逛公园"])
def test_repeated_evening_park_wording_preserves_authoritative_day_part(evening_clause: str) -> None:
    message = (
        f"今年国庆参观北京985高校两日游，{evening_clause}，中午想体验当地特色美食。"
        "10月1日到2日，2天，中等预算，1人，公交地铁优先"
    )
    service = AgentService.__new__(AgentService)
    contract = service._request_intent_contract(
        message,
        {"dates": ["2026-10-01", "2026-10-02"]},
        MealGroundingPolicy().analyze(message, city="北京", budget="medium", day_count=2),
        city="北京",
        simple_open_profile=True,
    )
    park = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_park")

    assert park["schedulePreference"].get("dayPart") == "evening"
    assert park["schedulePreference"]["userExplicit"] is True
    assert park["requiredMin"] == 2
    assert park["allowedDayNumbers"] == [1, 2]
    assert park["distributionPolicy"] == "every_allowed_day"
    assert park["timeWindow"] is None


@pytest.mark.parametrize(
    "message",
    ["晚上吃特色美食，下午去公园", "上午逛公园，晚上体验美食"],
)
def test_evening_day_part_does_not_cross_into_another_activity_clause(message: str) -> None:
    contract = AgentService.__new__(AgentService)._request_intent_contract(
        message,
        {"dates": ["2026-10-01", "2026-10-02"]},
        MealGroundingPolicy().analyze(message, city="北京", budget="medium", day_count=2),
        city="北京",
        simple_open_profile=True,
    )
    park = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_park")

    assert park["schedulePreference"].get("dayPart") != "evening"


@pytest.mark.parametrize("controller_hint", [False, True])
def test_reported_evening_request_survives_occurrence_lineage_and_afternoon_slot(controller_hint: bool) -> None:
    message = (
        "今年国庆参观北京985高校两日游，每晚都去逛公园，中午想体验当地特色美食。"
        "10月1日到2日，2天，中等预算，1人，公交地铁优先"
    )
    service = AgentService.__new__(AgentService)
    resolved_dates = {
        "dates": ["2026-10-01", "2026-10-02"],
        "startDate": "2026-10-01",
        "endDate": "2026-10-02",
        "dayCount": 2,
    }
    contract = service._request_intent_contract(
        message,
        resolved_dates,
        MealGroundingPolicy().analyze(message, city="北京", budget="medium", day_count=2),
        city="北京",
        simple_open_profile=True,
    )
    goal_ids = [item["goalId"] for item in contract["requiredIntents"]]
    directive = {
        "type": "draft_itinerary",
        "dayStrategies": [
            {
                "dayNumber": day,
                "requiredGoalIds": goal_ids,
                "requiredGoalCounts": {goal_id: 1 for goal_id in goal_ids},
                "optionalGoalIds": [],
            }
            for day in (1, 2)
        ],
        "optionalExperienceBudget": 0,
        "candidateSelectionPolicy": {"avoidRecentEntities": True},
        "occurrenceScheduleHints": [
            {
                "goalId": "goal_park",
                "dayNumber": day,
                "dayPart": "afternoon",
                "sequence": 3,
                "preferredStartTime": "14:00",
                "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                "estimateSource": "controller_estimate",
                "confidence": 0.8,
            }
            for day in (1, 2)
        ]
        if controller_hint
        else [],
    }
    context = {
        "city": "北京",
        "serverExecutionProfile": "simple_open_v1",
        "requestIntentContract": contract,
        "resolvedTripDates": resolved_dates,
        "planningDirective": directive,
        "agentDecisionState": {
            "source": "controller",
            "controlOwner": "model_controller",
            "decisionPath": "full",
            "accepted": True,
            "primaryAction": "draft_itinerary",
            "actionDirective": directive,
        },
    }
    occurrences = service._simple_open_authoritative_occurrences(context)
    initial_plan = AgentInitialPlanOutput(
        reply="",
        mode="plan",
        daySlots=[
            {
                "slotId": f"day{item['dayNumber']}_{item['sourceGoalId']}",
                "dayNumber": item["dayNumber"],
                "date": f"2026-10-0{item['dayNumber']}",
                "startTime": "14:00",
                "durationMinutes": 90,
                "kind": "park" if item["intentType"] == "park" else "visit",
                "rawNeed": item["intentType"],
                "routeAnchor": True,
            }
            for item in occurrences
        ],
        intentPools=[
            {
                "poolId": f"pool_{item['occurrenceId']}",
                "city": "北京",
                "rawNeed": item["intentType"],
                "intentType": item["intentType"],
                "targetCount": 1,
                "requirementLevel": "required",
                "goalId": item["sourceGoalId"],
                "assignToSlots": [f"day{item['dayNumber']}_{item['sourceGoalId']}"],
            }
            for item in occurrences
        ],
    )
    lineage = service._simple_open_slot_occurrence_lineage(initial_plan, context)
    parks = [item for item in occurrences if item["intentType"] == "park"]
    assert len(parks) == 2
    for occurrence in parks:
        day = occurrence["dayNumber"]
        assert occurrence["schedulePreference"]["dayPart"] == "evening"
        slot_lineage = lineage[f"day{day}_goal_park"]
        assert slot_lineage["schedulePreference"]["dayPart"] == "evening"
        plan = _park_plan(
            trip_date=f"2026-10-0{day}",
            longitude=116.397,
            latitude=39.904,
            open_time_today=None,
        )
        plan.day_number = day
        plan.start_time = "14:00"
        plan.schedule_preference = slot_lineage["schedulePreference"]
        plan.schedule_constraints = slot_lineage["scheduleConstraints"]
        scheduled = SimpleOpenDynamicScheduleService().schedule([plan])[0]
        sunset = SimpleOpenDynamicScheduleService.local_sunset_minutes(
            date(2026, 10, day),
            latitude=39.904,
            longitude=116.397,
        )
        assert scheduled.schedule_decision["constraintPassed"] is True
        assert SimpleOpenDynamicScheduleService._minutes(scheduled.start_time) >= sunset
        assert scheduled.schedule_decision["openingEvidenceStatus"] == "unverified"


def test_unestimated_evening_occurrence_stays_flexible_without_fabricated_clock() -> None:
    plan = _park_plan(
        trip_date="2026-10-01",
        longitude=116.397,
        latitude=39.904,
        open_time_today=None,
    )
    plan.duration_minutes = 0

    scheduled = SimpleOpenDynamicScheduleService().schedule([plan])[0]

    assert scheduled.start_time == ""
    assert scheduled.schedule_decision["durationMinutes"] is None
    assert scheduled.schedule_decision["constraintPassed"] is True
    assert scheduled.schedule_decision["scheduleConfidence"] == "flexible"
    assert scheduled.schedule_decision["provisionalReasons"] == ["duration_estimate_unavailable"]
    assert "failureReason" not in scheduled.schedule_decision


def test_controller_duration_estimate_materializes_commit_ready_clock() -> None:
    plan = _park_plan(
        trip_date="2026-10-01",
        longitude=116.397,
        latitude=39.904,
        open_time_today=None,
    )
    plan.duration_minutes = 0
    plan.schedule_constraints = {
        "durationEstimate": {"min": 60, "preferred": 80, "max": 120},
        "durationEstimateSource": "controller_estimate",
        "durationEstimateConfidence": 0.7,
    }

    scheduled = SimpleOpenDynamicScheduleService().schedule([plan])[0]

    assert scheduled.start_time
    assert scheduled.duration_minutes == 80
    assert scheduled.schedule_decision["durationMinutes"] == 80
    assert scheduled.schedule_decision["durationSource"] == "controller_estimate"
    assert scheduled.schedule_decision["constraintPassed"] is True
    assert scheduled.schedule_decision["endTime"] > scheduled.start_time


def test_verified_arrival_and_next_locked_activity_bound_dynamic_evening() -> None:
    plan = _park_plan(
        trip_date="2026-10-01",
        longitude=116.397,
        latitude=39.904,
        open_time_today="06:00-23:00",
    )
    plan.schedule_constraints = {
        "routeArrivalTime": "20:05",
        "routeArrivalSource": "verified_route",
        "nextLockedStartTime": "22:00",
    }

    scheduled = SimpleOpenDynamicScheduleService().schedule([plan])[0]

    assert scheduled.schedule_decision["constraintPassed"] is True
    assert scheduled.start_time >= "20:05"
    assert scheduled.schedule_decision["endTime"] <= "22:00"


def _campus_then_noon_meal(*, hard: bool) -> list[PersistableSegmentPlan]:
    campus = _park_plan(
        trip_date="2026-10-01",
        longitude=116.312876,
        latitude=39.99684,
        open_time_today=None,
        explicit_start="09:00",
    )
    campus.kind = "campus"
    campus.intent_type = "campus_visit"
    campus.start_time = "09:00"
    campus.duration_minutes = 180
    campus.schedule_preference = {
        **campus.schedule_preference,
        "dayPart": "morning",
        "sequence": 1,
        "sequenceSource": "controller_schedule_hint",
    }

    meal = _park_plan(
        trip_date="2026-10-01",
        longitude=116.314171,
        latitude=39.979626,
        open_time_today=None,
    )
    meal.kind = "meal"
    meal.intent_type = "meal"
    meal.route_anchor = False
    meal.start_time = "12:00"
    meal.duration_minutes = 90
    meal.schedule_preference = {
        **meal.schedule_preference,
        "dayPart": "noon",
        "sequence": 2,
        "sequenceSource": "controller_schedule_hint",
    }
    meal.schedule_constraints = {
        "earliestStart": "12:00",
        "latestStart": "12:30",
        "windowEnd": "14:00",
        "source": "controller_schedule_hint_daypart" if not hard else "locked_activity",
        "hard": hard,
        "routeTravelMinutesFromPrevious": 31,
        "routeArrivalSource": "verified_provider_route_matrix",
    }
    return [campus, meal]


def test_controller_daypart_latest_is_advisory_after_verified_route() -> None:
    campus, meal = SimpleOpenDynamicScheduleService().schedule(
        _campus_then_noon_meal(hard=False)
    )

    assert campus.start_time == "09:00"
    assert meal.start_time == "12:35"
    assert meal.schedule_decision["endTime"] == "14:05"
    assert meal.schedule_decision["constraintPassed"] is True
    assert meal.schedule_decision["routeArrivalSource"] == "verified_provider_route_matrix"


def test_locked_latest_still_rejects_route_arrival_after_hard_window() -> None:
    _campus, meal = SimpleOpenDynamicScheduleService().schedule(
        _campus_then_noon_meal(hard=True)
    )

    assert meal.start_time == ""
    assert meal.schedule_decision["constraintPassed"] is False
    assert meal.schedule_decision["failureReason"] == "latest_start_constraint_missed"


@pytest.mark.parametrize("boundary_key", ["nextLockedStartTime", "dayEndTime"])
def test_midnight_locked_or_day_boundary_remains_hard_when_estimated_window_is_soft(
    boundary_key: str,
) -> None:
    plan = _park_plan(
        trip_date="2026-10-01",
        longitude=116.397,
        latitude=39.904,
        open_time_today=None,
        explicit_start="08:00",
    )
    plan.schedule_constraints = {
        "explicitStartTime": "08:00",
        "hard": False,
        boundary_key: "00:00",
    }

    scheduled = SimpleOpenDynamicScheduleService().schedule([plan])[0]

    assert scheduled.start_time == ""
    assert scheduled.schedule_decision["constraintPassed"] is False
    assert scheduled.schedule_decision["failureReason"] == "next_locked_activity_or_day_boundary_conflict"


def test_user_explicit_evening_clock_is_compiled_as_the_only_hard_clock() -> None:
    service = object.__new__(AgentService)
    contract = service._request_intent_contract(
        "北京两日游，20:30开始逛公园",
        {"dates": ["2026-10-01", "2026-10-02"]},
        SimpleNamespace(meal_slots=[]),
    )
    park = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_park")

    assert park["timeWindow"] == "20:30"
    assert park["schedulePreference"]["dayPart"] == "evening"


def test_every_day_lunch_is_a_semantic_day_part_not_a_hard_time_window() -> None:
    service = object.__new__(AgentService)
    contract = service._request_intent_contract(
        "北京两日游，每天午餐体验当地特色美食",
        {"dates": ["2026-10-01", "2026-10-02"]},
        MealGroundingPolicy().analyze(
            "北京两日游，每天午餐体验当地特色美食",
            city="北京",
            budget="medium",
            day_count=2,
        ),
    )
    meal = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_meal")

    assert meal["timeWindow"] is None
    assert meal["schedulePreference"]["dayPart"] == "noon"
    assert meal["schedulePreference"]["userExplicit"] is True


def test_exact_reported_request_preserves_two_day_campus_theme_and_explicit_noon_meal() -> None:
    message = (
        "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，"
        "中午品尝当地网红美食，晚上去附近的公园逛一下"
    )
    service = object.__new__(AgentService)
    meal_policy = MealGroundingPolicy().analyze(
        message,
        city="北京",
        budget="medium",
        day_count=2,
    )

    contract = service._request_intent_contract(
        message,
        {"dates": ["2026-10-01", "2026-10-02"]},
        meal_policy,
        city="北京",
        simple_open_profile=True,
    )

    campus = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_campus_visit")
    meal = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_meal")
    park = next(item for item in contract["requiredIntents"] if item["goalId"] == "goal_park")
    meal_spec = next(item for item in contract["experienceSpecs"] if item["intentType"] == "meal")

    assert campus["target"] == 2
    assert campus["distributionPolicy"] == "every_allowed_day"
    assert meal["requiredMin"] == 2
    assert meal["source"] == "multi_day_daily_template"
    assert meal["userExplicit"] is True
    assert meal["schedulePreference"]["dayPart"] == "noon"
    assert meal["schedulePreference"]["userExplicit"] is True
    assert meal_spec["timeWindow"] == {"start": "12:00", "end": "13:15"}
    assert park["schedulePreference"]["dayPart"] == "evening"


def test_strict_every_day_order_compiles_each_named_stop_as_a_hard_daily_occurrence() -> None:
    message = (
        "2026年10月1日至2日，1人，中等预算，全程公共交通，每天09:00至19:00规划北京两日985高校游。"
        "每天严格按“北京985高校→12:00至13:30北京当地特色午餐→独立公共公园”的顺序安排3个不同地点；"
        "两天的高校、餐厅和公园均不得重复。高校必须是北京985，排除北京科技大学等非985学校；"
        "午餐必须为高德分类中的北京菜，不接受高校食堂或泛餐饮；公园必须是独立公共公园，不得用校园景观代替。"
        "地点间尽量少绕路，只有每天2段相邻公共交通路线都经高德核验后，方案才可确认编辑。"
    )
    service = object.__new__(AgentService)
    meal_policy = MealGroundingPolicy().analyze(
        message,
        city="北京",
        budget="medium",
        day_count=2,
    )

    contract = service._request_intent_contract(
        message,
        {"dates": ["2026-10-01", "2026-10-02"]},
        meal_policy,
        city="北京",
        simple_open_profile=True,
    )

    required_by_goal = {item["goalId"]: item for item in contract["requiredIntents"]}
    campus = required_by_goal["goal_campus_visit"]
    meal = required_by_goal["goal_meal"]
    park = required_by_goal["goal_park"]
    meal_spec = next(item for item in contract["experienceSpecs"] if item["intentType"] == "meal")

    for goal in (campus, meal, park):
        assert goal["target"] == 2
        assert goal["requiredMin"] == 2
        assert goal["distributionPolicy"] == "every_allowed_day"
        assert goal["allowedDayNumbers"] == [1, 2]
        assert goal["priorityTier"] == "hard"
    assert meal["source"] == "explicit_every_day"
    assert meal["cardinalitySource"] == "explicit_every_day"
    assert meal["requirementLevel"] == "hard_requirement"
    assert park["cardinalitySource"] == "explicit_every_day"
    assert park["requirementLevel"] == "required"
    assert meal_spec["frequency"] == "every_allowed_day"
    assert contract["localExperienceConstraint"]["occurrencesByDay"] == {"1": 1, "2": 1}

    directive = {
        "dayStrategies": [
            {
                "dayNumber": day_number,
                "requiredGoalIds": ["goal_campus_visit", "goal_meal", "goal_park"],
                "requiredGoalCounts": {
                    "goal_campus_visit": 1,
                    "goal_meal": 1,
                    "goal_park": 1,
                },
                "optionalGoalIds": [],
            }
            for day_number in (1, 2)
        ],
        "optionalExperienceBudget": 0,
        "candidateSelectionPolicy": {"avoidRecentEntities": True},
    }
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": contract,
            "resolvedTripDates": {
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dayCount": 2,
            },
        },
        directive,
    )
    occurrence_plan = GoalOccurrenceCompiler().compile(ledger, directive)

    assert [(item.day_number, item.intent_type) for item in occurrence_plan.occurrences] == [
        (1, "campus_visit"),
        (1, "meal"),
        (1, "park"),
        (2, "campus_visit"),
        (2, "meal"),
        (2, "park"),
    ]
    assert {item.occurrence_id for item in occurrence_plan.occurrences} >= {
        "occ:goal_meal:day:2",
        "occ:goal_park:day:2",
    }
    assert all(item.requirement_level == "hard" for item in occurrence_plan.occurrences)
    assert all(item.user_explicit is True for item in occurrence_plan.occurrences)


def test_daily_time_scope_does_not_multiply_later_single_day_meal_or_park() -> None:
    message = "每天09:00至19:00规划行程；其中一天安排午餐和公园。"

    assert GoalLedgerService.explicit_every_day_ordered_intents(message) == frozenset()
    meal = GoalLedgerService().from_message(message, day_count=2).goal("meal")

    assert meal.source == "explicit_single_scope"
    assert meal.distribution_policy == "spread_across_distinct_days"


def test_daily_order_only_quantifies_intents_inside_the_ordered_payload() -> None:
    message = "每天严格按高校→公园的顺序安排，午餐只在其中一天吃。"

    assert GoalLedgerService.explicit_every_day_ordered_intents(message) == frozenset(
        {"campus_visit", "park"}
    )
    assert GoalLedgerService.authoritative_daily_template_intents(
        message,
        day_count=2,
    ) == frozenset({"campus_visit", "park"})
    meal = GoalLedgerService().from_message(message, day_count=2).goal("meal")

    assert meal.source == "explicit_single_scope"
    assert meal.distribution_policy == "spread_across_distinct_days"


def test_schedule_returns_sealed_sequence_instead_of_original_empty_clock_order() -> None:
    service = SimpleOpenDynamicScheduleService()
    campus = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="",
        duration_minutes=120,
        kind="campus",
        route_anchor=True,
        selected_poi=POI(
            id="poi_campus",
            amap_id="B000CAMPUS1",
            name="测试高校",
            city="北京",
            category="campus",
            latitude=39.99,
            longitude=116.31,
            source="amap-place-search",
            confidence=1.0,
            type="科教文化服务;学校;高等院校",
        ),
        display_title="测试高校",
        notes="",
        grounding_status="verified_amap",
        ticket_status="not_checked",
        planning_slot_id="day1_campus",
        occurrence_id="occ:campus:day:1",
        schedule_preference={"dayPart": "morning", "sequence": 1},
        schedule_constraints={"preferredStartTime": "09:00"},
    )
    meal = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="12:00",
        duration_minutes=75,
        kind="meal",
        route_anchor=True,
        selected_poi=POI(
            id="poi_meal",
            amap_id="B000MEAL001",
            name="测试餐厅",
            city="北京",
            category="food",
            latitude=39.98,
            longitude=116.32,
            source="amap-place-search",
            confidence=1.0,
            type="餐饮服务;中餐厅",
        ),
        display_title="测试餐厅",
        notes="",
        grounding_status="verified_amap",
        ticket_status="not_checked",
        planning_slot_id="day1_meal",
        occurrence_id="occ:meal:day:1",
        schedule_preference={"dayPart": "noon", "sequence": 2},
        schedule_constraints={"preferredStartTime": "12:00"},
    )

    scheduled = service.schedule([meal, campus])

    assert [item.planning_slot_id for item in scheduled] == ["day1_campus", "day1_meal"]
    assert scheduled[0].start_time == "09:00"
    assert scheduled[1].start_time == "12:00"


def test_schedule_repairs_server_slot_order_from_semantic_day_parts_when_no_controller_sequence_exists() -> None:
    first = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="",
        duration_minutes=0,
        kind="campus",
        route_anchor=True,
        selected_poi=None,
        display_title="高校待补",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        planning_slot_id="slot_z_first",
        occurrence_id="occ:first:day:1",
        schedule_preference={
            "dayPart": "flexible",
            "sequence": 1,
            "sequenceSource": "server_sealed_slot_order",
        },
    )
    second = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="12:00",
        duration_minutes=0,
        kind="meal",
        route_anchor=True,
        selected_poi=None,
        display_title="午餐待补",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        planning_slot_id="slot_a_second",
        occurrence_id="occ:second:day:1",
        schedule_preference={
            "dayPart": "noon",
            "sequence": 3,
            "sequenceSource": "server_sealed_slot_order",
        },
    )
    evening = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="11:30",
        duration_minutes=0,
        kind="park",
        route_anchor=True,
        selected_poi=None,
        display_title="晚间公园待补",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        planning_slot_id="slot_m_evening",
        occurrence_id="occ:evening:day:1",
        schedule_preference={
            "dayPart": "evening",
            "sequence": 2,
            "sequenceSource": "server_sealed_slot_order",
        },
    )

    scheduled = SimpleOpenDynamicScheduleService().schedule([first, evening, second])

    assert [item.planning_slot_id for item in scheduled] == [
        "slot_z_first",
        "slot_a_second",
        "slot_m_evening",
    ]
    assert [item.schedule_preference["sequence"] for item in scheduled] == [1, 2, 3]
    assert {
        item.schedule_preference["sequenceSource"] for item in scheduled
    } == {"server_semantic_schedule_order"}


def test_schedule_rejects_controller_sequence_that_places_explicit_noon_after_evening() -> None:
    campus = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="09:00",
        duration_minutes=90,
        kind="campus",
        route_anchor=True,
        selected_poi=None,
        display_title="高校",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        planning_slot_id="day1_campus",
        occurrence_id="occ:campus:day:1",
        schedule_preference={
            "dayPart": "morning",
            "sequence": 1,
            "sequenceSource": "controller_schedule_hint",
            "userExplicit": True,
        },
        schedule_constraints={"preferredStartTime": "09:00"},
    )
    park = _park_plan(
        trip_date="2026-10-01",
        longitude=116.397,
        latitude=39.904,
        open_time_today="06:00-23:00",
    )
    park.schedule_preference.update(
        {"sequence": 2, "sequenceSource": "controller_schedule_hint"}
    )
    meal = PersistableSegmentPlan(
        day_number=1,
        date="2026-10-01",
        start_time="12:00",
        duration_minutes=75,
        kind="meal",
        route_anchor=True,
        selected_poi=None,
        display_title="网红美食",
        notes="",
        grounding_status="unresolved",
        ticket_status="not_checked",
        planning_slot_id="day1_lunch",
        occurrence_id="occ:meal:day:1",
        schedule_preference={
            "dayPart": "noon",
            "sequence": 3,
            "sequenceSource": "controller_schedule_hint",
            "userExplicit": True,
        },
        schedule_constraints={"preferredStartTime": "12:00"},
    )

    scheduled = SimpleOpenDynamicScheduleService().schedule([campus, park, meal])

    assert [item.planning_slot_id for item in scheduled] == [
        "day1_campus",
        "day1_lunch",
        "day1_evening_park",
    ]
    assert scheduled[1].start_time == "12:00"
    assert scheduled[2].start_time >= "18:00"
    assert {
        item.schedule_preference["sequenceSource"] for item in scheduled
    } == {"server_semantic_schedule_order"}


def test_schedule_reseals_mixed_controller_and_server_completion_order_by_day_part() -> None:
    def plan(
        *,
        slot_id: str,
        kind: str,
        day_part: str,
        sequence: int,
        sequence_source: str,
        start_time: str,
        user_explicit: bool,
    ) -> PersistableSegmentPlan:
        return PersistableSegmentPlan(
            day_number=2,
            date="2026-10-02",
            start_time=start_time,
            duration_minutes=60,
            kind=kind,
            route_anchor=True,
            selected_poi=None,
            display_title=slot_id,
            notes="",
            grounding_status="unresolved",
            ticket_status="not_checked",
            planning_slot_id=slot_id,
            occurrence_id=f"occ:{slot_id}",
            schedule_preference={
                "dayPart": day_part,
                "sequence": sequence,
                "sequenceSource": sequence_source,
                "userExplicit": user_explicit,
            },
            schedule_constraints={"preferredStartTime": start_time},
        )

    campus = plan(
        slot_id="day2_campus",
        kind="campus",
        day_part="morning",
        sequence=1,
        sequence_source="controller_schedule_hint",
        start_time="09:00",
        user_explicit=True,
    )
    completion = plan(
        slot_id="day2_daily_completion_1",
        kind="park",
        day_part="afternoon",
        sequence=2,
        sequence_source="server_sealed_slot_order",
        start_time="14:00",
        user_explicit=False,
    )
    meal = plan(
        slot_id="day2_lunch",
        kind="meal",
        day_part="noon",
        sequence=3,
        sequence_source="controller_schedule_hint",
        start_time="12:00",
        user_explicit=True,
    )

    scheduled = SimpleOpenDynamicScheduleService().schedule([campus, completion, meal])

    assert [item.planning_slot_id for item in scheduled] == [
        "day2_campus",
        "day2_lunch",
        "day2_daily_completion_1",
    ]
    assert [item.start_time for item in scheduled] == ["09:00", "12:00", "14:00"]
    assert {
        item.schedule_preference["sequenceSource"] for item in scheduled
    } == {"server_semantic_schedule_order"}


def test_controller_evening_hint_is_compiled_for_the_commit_schedule_kernel() -> None:
    service = AgentService.__new__(AgentService)
    service._simple_open_authoritative_occurrences = lambda _context: [
        {
            "sourceGoalId": "goal_park",
            "occurrenceId": "occ:goal_park:day:1",
            "dayNumber": 1,
            "intentType": "park",
            "requirementLevel": "hard",
            "userExplicit": True,
            "lineageAuthority": "goal_occurrence_compiler",
        }
    ]
    service._simple_open_schedule_hints_by_goal_day = lambda _context: {
        ("goal_park", 1): {
            "goalId": "goal_park",
            "dayNumber": 1,
            "dayPart": "evening",
            "sequence": 3,
            "preferredStartTime": "18:00",
            "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
            "estimateSource": "controller_estimate",
            "confidence": 0.9,
        }
    }
    initial_plan = AgentInitialPlanOutput(
        reply="",
        mode="plan",
        daySlots=[
            {
                "slotId": "day1_evening_park",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "evening",
                "startTime": "18:00",
                "durationMinutes": 90,
                "kind": "park",
                "rawNeed": "晚上去附近公园逛一下",
                "routeAnchor": True,
            }
        ],
        intentPools=[
            {
                "poolId": "park_pool",
                "rawNeed": "晚上去附近公园逛一下",
                "city": "北京",
                "intentType": "park",
                "targetCount": 1,
                "requirementLevel": "required",
                "goalId": "goal_park",
                "assignToSlots": ["day1_evening_park"],
            }
        ],
    )

    lineage = service._simple_open_slot_occurrence_lineage(initial_plan, {})

    assert lineage["day1_evening_park"]["schedulePreference"] == {
        "dayPart": "evening",
        "intentType": "park",
        "preferredDayNumbers": [1],
        "userExplicit": True,
        "priority": "hard",
        "sourceGoalId": "goal_park",
        "occurrenceId": "occ:goal_park:day:1",
        "sequence": 3,
        "sequenceSource": "controller_schedule_hint",
    }
    constraints = lineage["day1_evening_park"]["scheduleConstraints"]
    assert constraints["earliestStart"] == "18:00"
    assert constraints["latestStart"] == "20:30"
    assert constraints["windowEnd"] == "22:00"
    assert constraints["hard"] is False
    assert constraints["source"] == "controller_schedule_hint_daypart"
    assert constraints["preferredStartTime"] == "18:00"

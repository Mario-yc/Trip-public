from __future__ import annotations

import pytest

from src.services.agent_observation_fact_service import AgentObservationFactService
from src.services.goal_ledger_service import GoalLedgerService


@pytest.mark.parametrize(
    ("message", "minimum", "preferred", "maximum"),
    [
        ("今年国庆参观985大学两日游", 1, 1, None),
        ("参观四所985大学", 4, 4, 4),
        ("两天每天参观两所985大学", 4, 4, 4),
        ("两天各两所985大学", 4, 4, 4),
        ("最好能路过一所985大学，不强求", 0, 1, 1),
    ],
)
def test_campus_goal_cardinality_preserves_user_quantification(message, minimum, preferred, maximum):
    goal = GoalLedgerService().from_message(message).goal("campus_visit")

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (minimum, preferred, maximum)


def test_campus_theme_spanning_two_days_is_required_once_per_planning_day():
    goal = (
        GoalLedgerService()
        .from_message(
            "今年国庆参观北京高校两日游，10月1日到2日。",
            day_count=2,
        )
        .goal("campus_visit")
    )

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (2, 2, 2)
    assert goal.source == "multi_day_daily_template"
    assert goal.distribution_policy == "every_allowed_day"
    assert goal.allowed_day_numbers == (1, 2)


def test_explicit_one_campus_stays_exact_one_even_on_two_day_trip():
    goal = (
        GoalLedgerService()
        .from_message(
            "北京两日游，参观一所985大学。",
            day_count=2,
        )
        .goal("campus_visit")
    )

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (1, 1, 1)


def test_daily_campus_uses_resolved_trip_day_count_without_requiring_two_day_word_order():
    goal = (
        GoalLedgerService()
        .from_message(
            "2026年10月1日至2日北京两日游，每天安排一所985高校。",
            day_count=2,
        )
        .goal("campus_visit")
    )

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (2, 2, 2)
    assert goal.source == "explicit_every_day"
    assert goal.distribution_policy == "every_allowed_day"
    assert goal.allowed_day_numbers == (1, 2)


def test_daily_lunch_is_one_explicit_soft_occurrence_per_day():
    goal = (
        GoalLedgerService()
        .from_message(
            "北京两日游，每天午餐想体验当地特色美食。",
            day_count=2,
        )
        .goal("meal")
    )

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (2, 2, 2)
    assert goal.source == "explicit_every_day"
    assert goal.distribution_policy == "every_allowed_day"
    assert goal.allowed_day_numbers == (1, 2)


def test_daily_list_scope_applies_to_lunch_after_other_itinerary_items():
    goal = (
        GoalLedgerService()
        .from_message(
            "北京两日游。每天安排一所985高校、一顿北京当地特色午餐和一个独立公共公园。",
            day_count=2,
        )
        .goal("meal")
    )

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (2, 2, 2)
    assert goal.source == "explicit_every_day"
    assert goal.distribution_policy == "every_allowed_day"
    assert goal.allowed_day_numbers == (1, 2)


def test_museum_is_independent_from_generic_local_culture():
    ledger = GoalLedgerService().from_message("安排一所985大学和一个博物馆，再体验当地风土人情")

    assert ledger.goal("campus_visit").min_count == 1
    assert ledger.goal("museum").min_count == 1
    assert ledger.goal("local_culture").min_count == 1


@pytest.mark.parametrize(
    ("message", "minimum", "preferred", "maximum", "source", "distribution"),
    [
        ("北京两日游，晚上看北京夜景", 1, 2, 2, "clarification_required", "spread_across_distinct_days"),
        ("北京两日游，每晚都看北京夜景", 2, 2, 2, "explicit_every_day", "every_allowed_day"),
        ("北京两日游，看两处夜景", 2, 2, 2, "explicit_count", "spread_across_distinct_days"),
        ("北京两日游，如果方便最好看看夜景", 0, 1, 1, "soft_experience", "spread_across_distinct_days"),
    ],
)
def test_night_view_cardinality_requires_explicit_evening_scope(
    message, minimum, preferred, maximum, source, distribution
):
    goal = GoalLedgerService().from_message(message, day_count=2).goal("night_view")

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (minimum, preferred, maximum)
    assert goal.source == source
    assert goal.distribution_policy == distribution
    assert goal.allowed_day_numbers == (1, 2)


def test_night_view_count_above_trip_days_requires_clarification():
    goal = GoalLedgerService().from_message("北京两日游，看三处夜景", day_count=2).goal("night_view")

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (0, 0, 0)
    assert goal.requested_count == 3
    assert goal.source == "clarification_required"
    assert goal.limitation_reason == "night_view_daily_max_one"


def test_single_or_explicit_day_night_view_stays_single():
    service = GoalLedgerService()
    one_night = service.from_message("北京两日游，其中一晚看夜景", day_count=2).goal("night_view")
    first_day = service.from_message("北京两日游，只在第一天晚上看夜景", day_count=2).goal("night_view")
    dated = service.from_message(
        "10月2日晚上看夜景",
        day_count=2,
        trip_dates=("2026-10-01", "2026-10-02"),
    ).goal("night_view")

    assert (one_night.min_count, one_night.preferred_count, one_night.max_count) == (1, 1, 1)
    assert one_night.source == "explicit_single_evening"
    assert first_day.allowed_day_numbers == (1,)
    assert dated.allowed_day_numbers == (2,)


def test_ambiguous_arrival_or_departure_evening_requires_clarification():
    goal = (
        GoalLedgerService()
        .from_message(
            "北京两日游，第一天抵达、第二天返程，晚上看夜景",
            day_count=2,
        )
        .goal("night_view")
    )

    assert goal.source == "clarification_required"
    assert goal.limitation_reason == "night_view_evening_availability_ambiguous"


def test_two_nights_all_uses_universal_evening_distribution():
    goal = (
        GoalLedgerService()
        .from_message(
            "北京三日游，两晚都看北京夜景",
            day_count=3,
        )
        .goal("night_view")
    )

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (3, 3, 3)
    assert goal.requested_count is None
    assert goal.source == "explicit_every_day"
    assert goal.distribution_policy == "every_allowed_day"
    assert goal.allowed_day_numbers == (1, 2, 3)


@pytest.mark.parametrize(
    ("segment", "expected_required", "expected_anchor"),
    [
        ({"requirementLevel": "required", "routeAnchor": True, "notes": "requiredGrounding=false"}, True, True),
        ({"requirementLevel": "optional", "routeAnchor": False, "notes": "requiredGrounding=true"}, False, False),
        ({"kind": "visit", "notes": "requiredGrounding=true"}, False, False),
        ({"kind": "meal", "requirementLevel": "required", "routeAnchor": False}, True, False),
    ],
)
def test_required_and_route_anchor_use_structured_facts_only(segment, expected_required, expected_anchor):
    facts = AgentObservationFactService()

    assert facts.is_required(segment) is expected_required
    assert facts.is_route_anchor(segment) is expected_anchor


def test_same_clock_time_on_different_days_is_not_overlap():
    result = AgentObservationFactService().schedule_facts(
        [
            {"id": "a", "dayNumber": 1, "startTime": "10:00", "endTime": "11:00"},
            {"id": "b", "dayNumber": 2, "startTime": "10:00", "endTime": "11:00"},
        ]
    )

    assert result.overlap_count == 0
    assert result.overlaps == []


def test_real_same_day_overlap_is_reported_with_segment_ids():
    result = AgentObservationFactService().schedule_facts(
        [
            {"id": "a", "dayNumber": 1, "startTime": "10:00", "endTime": "11:30"},
            {"id": "b", "dayNumber": 1, "startTime": "11:00", "endTime": "12:00"},
        ]
    )

    assert result.overlap_count == 1
    assert result.overlaps[0].segment_ids == ("a", "b")
    assert result.overlaps[0].minutes == 30


def test_optional_unresolved_does_not_increment_required_waiting():
    facts = AgentObservationFactService().coverage_facts(
        [
            {"id": "required", "requirementLevel": "required", "groundingStatus": "confirmed"},
            {"id": "optional", "requirementLevel": "optional", "groundingStatus": "waiting_for_poi_grounding"},
        ]
    )

    assert facts.unresolved_required_count == 0
    assert facts.unresolved_optional_count == 1


def test_version_lineage_conflict_becomes_blocking_invariant():
    invariants = AgentObservationFactService().version_invariants(
        active_version_id="ver_active",
        lineage_current_version_id="ver_old",
        reloaded_version_id="ver_active",
    )

    assert invariants == ["active_version_lineage_mismatch"]

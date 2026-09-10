from __future__ import annotations

import pytest

from src.services.goal_ledger_service import GoalLedgerService


TWO_DAY_BEIJING_REQUEST = (
    "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，"
    "中午品尝当地网红美食，晚上去附近的公园逛一下"
)


def test_complete_two_day_request_expands_theme_and_dayparts_to_each_required_day() -> None:
    ledger = GoalLedgerService().from_message(TWO_DAY_BEIJING_REQUEST, day_count=2)

    assert ledger.required_planning_day_numbers == (1, 2)
    assert ledger.explicit_rest_day_numbers == ()
    for intent_type in ("campus_visit", "meal", "park"):
        goal = ledger.goal(intent_type)
        assert (goal.min_count, goal.preferred_count, goal.max_count) == (2, 2, 2)
        assert goal.source == "multi_day_daily_template"
        assert goal.distribution_policy == "every_allowed_day"
        assert goal.allowed_day_numbers == (1, 2)


def test_unscoped_food_experience_without_daypart_remains_one_soft_occurrence() -> None:
    ledger = GoalLedgerService().from_message(
        "北京两日游，参观985高校，路途中能品尝北京当地特色美食。",
        day_count=2,
    )

    campus = ledger.goal("campus_visit")
    meal = ledger.goal("meal")
    assert (campus.min_count, campus.preferred_count, campus.max_count) == (2, 2, 2)
    assert campus.source == "multi_day_daily_template"
    assert (meal.min_count, meal.preferred_count, meal.max_count) == (0, 1, 2)
    assert meal.source == "explicit_user_request"
    assert meal.distribution_policy == "spread_across_distinct_days"


@pytest.mark.parametrize(
    ("message", "intent_type"),
    [
        (
            "北京两日游，只去一所985高校，中午品尝当地美食，晚上逛公园。",
            "campus_visit",
        ),
        (
            "北京两日游，参观985高校，中午当地美食只吃一次，晚上逛公园。",
            "meal",
        ),
        (
            "北京两日游，参观985高校，中午品尝当地美食，公园只去一次。",
            "park",
        ),
        (
            "北京两日游，其中一天参观985高校，中午品尝当地美食，晚上逛公园。",
            "campus_visit",
        ),
        (
            "北京两日游，参观985高校，其中一天中午品尝当地美食，晚上逛公园。",
            "meal",
        ),
        (
            "北京两日游，参观985高校，中午品尝当地美食，其中一天晚上逛公园。",
            "park",
        ),
    ],
)
def test_explicit_once_or_one_day_scope_narrows_only_that_intent(
    message: str,
    intent_type: str,
) -> None:
    goal = GoalLedgerService().from_message(message, day_count=2).goal(intent_type)

    assert (goal.min_count, goal.preferred_count, goal.max_count) == (1, 1, 1)
    assert goal.distribution_policy == "spread_across_distinct_days"
    assert goal.allowed_day_numbers == (1, 2)


def test_explicit_rest_day_is_not_a_required_planning_day_or_daily_goal_target() -> None:
    ledger = GoalLedgerService().from_message(
        "北京两日游，参观985高校，中午品尝当地美食，晚上逛公园，第二天休息。",
        day_count=2,
    )

    assert ledger.required_planning_day_numbers == (1,)
    assert ledger.explicit_rest_day_numbers == (2,)
    for intent_type in ("campus_visit", "meal", "park"):
        goal = ledger.goal(intent_type)
        assert (goal.min_count, goal.preferred_count, goal.max_count) == (1, 1, 1)
        assert goal.allowed_day_numbers == (1,)


def test_adjacent_day_clauses_do_not_leak_rest_into_the_previous_day() -> None:
    ledger = GoalLedgerService().from_message(
        "北京两日游，第一天逛高校，第二天休息。",
        day_count=2,
    )

    assert ledger.required_planning_day_numbers == (1,)
    assert ledger.explicit_rest_day_numbers == (2,)
    campus = ledger.goal("campus_visit")
    assert (campus.min_count, campus.preferred_count, campus.max_count) == (1, 1, 1)
    assert campus.source == "explicit_day_scope"
    assert campus.allowed_day_numbers == (1,)


def test_explicit_day_scopes_are_not_multiplied_across_the_trip() -> None:
    ledger = GoalLedgerService().from_message(
        "北京三日游，第一天逛高校，第二天中午吃当地美食，第三天晚上逛公园。",
        day_count=3,
    )

    expected_days = {
        "campus_visit": (1,),
        "meal": (2,),
        "park": (3,),
    }
    for intent_type, allowed_days in expected_days.items():
        goal = ledger.goal(intent_type)
        assert (goal.min_count, goal.preferred_count, goal.max_count) == (1, 1, 1)
        assert goal.source == "explicit_day_scope"
        assert goal.distribution_policy == "every_allowed_day"
        assert goal.allowed_day_numbers == allowed_days


@pytest.mark.parametrize(
    "message",
    [
        "北京两日游，第二天返程前逛公园。",
        "北京两日游，第二天不休息，继续参观高校。",
    ],
)
def test_return_or_negated_rest_does_not_self_authorize_an_empty_day(message: str) -> None:
    ledger = GoalLedgerService().from_message(message, day_count=2)

    assert ledger.required_planning_day_numbers == (1, 2)
    assert ledger.explicit_rest_day_numbers == ()


def test_only_return_day_has_explicit_rest_provenance() -> None:
    ledger = GoalLedgerService().from_message("北京两日游，第二天仅返程。", day_count=2)

    assert ledger.required_planning_day_numbers == (1,)
    assert ledger.explicit_rest_day_numbers == (2,)


def test_single_day_request_is_not_expanded_beyond_one_day() -> None:
    ledger = GoalLedgerService().from_message(
        "北京一日游，参观985高校，中午品尝当地美食，晚上逛公园。",
        day_count=1,
    )

    assert ledger.required_planning_day_numbers == (1,)
    assert ledger.explicit_rest_day_numbers == ()
    for intent_type in ("campus_visit", "meal", "park"):
        goal = ledger.goal(intent_type)
        assert (goal.min_count, goal.preferred_count, goal.max_count) == (1, 1, 1)
        assert goal.allowed_day_numbers == (1,)

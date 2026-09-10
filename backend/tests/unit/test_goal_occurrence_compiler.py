import pytest

from src.services.creative_planning_models import ConstraintLedger, GoalRequirement
from src.services.daily_capacity_planner import DailyCapacityPlanner
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler


def ledger() -> ConstraintLedger:
    return ConstraintLedger(
        schemaVersion="constraint-ledger-v1",
        city="北京",
        dayCount=2,
        hardGoals=[
            GoalRequirement(goalId="campus", intentType="campus_visit"),
            GoalRequirement(goalId="museum", intentType="museum"),
        ],
        softGoals=[GoalRequirement(goalId="meal", intentType="meal")],
        sourceFingerprint="a" * 64,
    )


def directive(day_strategies, *, avoid_recent_entities=True):
    return {
        "dayStrategies": day_strategies,
        "optionalExperienceBudget": sum(len(item.get("optionalGoalIds") or []) for item in day_strategies),
        "candidateSelectionPolicy": {"avoidRecentEntities": avoid_recent_entities},
    }


def test_compiles_controller_daily_occurrences_without_expanding_ambiguous_goals():
    plan = GoalOccurrenceCompiler().compile(
        ledger(),
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": ["meal"],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": [],
                },
            ]
        ),
    )
    assert [(item.source_goal_id, item.day_number) for item in plan.occurrences] == [
        ("campus", 1),
        ("meal", 1),
        ("museum", 2),
    ]
    assert all(item.distinct_group_id is None for item in plan.occurrences)


def test_recurring_controller_goal_is_distinct_when_avoid_recent_entities():
    recurring = ledger().model_copy(deep=True)
    recurring.hard_goals[0].required_min = 2
    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": ["meal"],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": ["meal"],
                },
            ]
        ),
    )
    campus = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert len(campus) == 2
    assert {item.distinct_group_id for item in campus} == {"distinct:campus"}


def test_empty_distinctness_policy_preserves_disabled_avoid_recent_entities():
    recurring = ledger().model_copy(deep=True)
    recurring.hard_goals[0].required_min = 2

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                },
            ],
            avoid_recent_entities=False,
        ),
    )

    campus = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert all(item.distinct_group_id is None for item in campus)


@pytest.mark.parametrize(
    "distinctness_policy",
    [
        "distinct_physical_identity_per_occurrence",
        "distinct_physical_poi_per_day",
    ],
)
def test_explicit_distinctness_policy_overrides_disabled_avoid_recent_entities(
    distinctness_policy,
):
    recurring = ledger().model_copy(deep=True)
    campus_goal = recurring.hard_goals[0]
    campus_goal.required_min = 2
    campus_goal.distinctness_policy = distinctness_policy

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                },
            ],
            avoid_recent_entities=False,
        ),
    )

    campus = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert {item.distinct_group_id for item in campus} == {"distinct:campus"}


def test_explicit_reuse_policy_overrides_enabled_avoid_recent_entities():
    recurring = ledger().model_copy(deep=True)
    campus_goal = recurring.hard_goals[0]
    campus_goal.required_min = 2
    campus_goal.distinctness_policy = "reuse_physical_identity_allowed"

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                },
            ]
        ),
    )

    campus = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert all(item.distinct_group_id is None for item in campus)


def test_repeated_hard_night_view_ignores_generic_reuse_policy():
    recurring = ledger().model_copy(deep=True)
    night_goal = recurring.hard_goals[0]
    night_goal.intent_type = "night_view"
    night_goal.required_min = 2
    night_goal.distinctness_policy = "reuse_physical_identity_allowed"

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                },
            ],
            avoid_recent_entities=False,
        ),
    )

    night = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert {item.distinct_group_id for item in night} == {"distinct:campus"}


def test_unknown_non_empty_distinctness_policy_fails_closed():
    unknown = ledger().model_copy(deep=True)
    unknown.hard_goals[0].distinctness_policy = "reuse_when_convenient"

    with pytest.raises(
        ValueError,
        match="goal_occurrence_distinctness_policy_unknown",
    ):
        GoalOccurrenceCompiler().compile(
            unknown,
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus"],
                        "requiredGoalCounts": {"campus": 1},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["museum"],
                        "requiredGoalCounts": {"museum": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_occurrences_inherit_machine_experience_policies_without_changing_day_authority():
    recurring = ledger().model_copy(deep=True)
    campus_goal = recurring.hard_goals[0]
    campus_goal.required_min = 2
    campus_goal.preferred_count = 2
    campus_goal.max_count = 2
    campus_goal.allowed_day_numbers = [1, 2]
    campus_goal.distribution_policy = "every_allowed_day"
    campus_goal.access_policy = "public_outdoor"
    campus_goal.distinctness_policy = "distinct_physical_identity_per_occurrence"
    campus_goal.time_window = {"start": "18:30", "end": "22:00"}
    campus_goal.detour_tolerance = {
        "maxGeneralizedCostDelta": 35,
        "maxDetourRatio": 0.35,
    }
    campus_goal.evidence_freshness = {
        "maxAgeHours": 24,
        "requiredForControlledAccess": True,
    }
    campus_goal.confidence = 0.9

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                },
            ]
        ),
    )

    campus = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert [item.day_number for item in campus] == [1, 2]
    for occurrence in campus:
        assert occurrence.access_policy == campus_goal.access_policy
        assert occurrence.distinctness_policy == campus_goal.distinctness_policy
        assert occurrence.time_window == campus_goal.time_window
        assert occurrence.detour_tolerance == campus_goal.detour_tolerance
        assert occurrence.evidence_freshness == campus_goal.evidence_freshness
        assert occurrence.confidence == campus_goal.confidence


def test_controller_cannot_schedule_more_occurrences_than_authoritative_maximum():
    bounded = ledger().model_copy(deep=True)
    bounded.hard_goals[0].max_count = 1
    with pytest.raises(ValueError, match="maximum_cardinality"):
        GoalOccurrenceCompiler().compile(
            bounded,
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus", "museum"],
                        "requiredGoalCounts": {"campus": 1, "museum": 1},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["campus"],
                        "requiredGoalCounts": {"campus": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_controller_cannot_exceed_declared_optional_experience_budget():
    over_budget = directive(
        [
            {
                "dayNumber": 1,
                "requiredGoalIds": ["campus"],
                "requiredGoalCounts": {"campus": 1},
                "optionalGoalIds": ["meal"],
            },
            {
                "dayNumber": 2,
                "requiredGoalIds": ["museum"],
                "requiredGoalCounts": {"museum": 1},
                "optionalGoalIds": ["meal"],
            },
        ]
    )
    over_budget["optionalExperienceBudget"] = 1

    with pytest.raises(ValueError, match="optional_experience_budget_exceeded"):
        GoalOccurrenceCompiler().compile(ledger(), over_budget)


def test_recurring_controller_goal_must_meet_required_minimum():
    recurring = ledger().model_copy(deep=True)
    recurring.hard_goals[0].required_min = 2
    with pytest.raises(ValueError, match="required_cardinality_mismatch"):
        GoalOccurrenceCompiler().compile(
            recurring,
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus"],
                        "requiredGoalCounts": {"campus": 1},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["museum"],
                        "requiredGoalCounts": {"museum": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_more_than_one_goal_occurrence_per_day_fails_closed():
    with pytest.raises(ValueError, match="daily_cardinality"):
        GoalOccurrenceCompiler().compile(
            ledger(),
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus"],
                        "requiredGoalCounts": {"campus": 2},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["museum"],
                        "requiredGoalCounts": {"museum": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_goal_occurrence_rejects_disallowed_day():
    bounded = ledger().model_copy(deep=True)
    bounded.hard_goals[0].allowed_day_numbers = [2]
    with pytest.raises(ValueError, match="day_not_allowed"):
        GoalOccurrenceCompiler().compile(
            bounded,
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus"],
                        "requiredGoalCounts": {"campus": 1},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["museum"],
                        "requiredGoalCounts": {"museum": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_explicit_soft_every_day_goal_must_be_placed_on_every_allowed_day():
    recurring = ledger().model_copy(deep=True)
    recurring.soft_goals[0].required_min = 2
    recurring.soft_goals[0].preferred_count = 2
    recurring.soft_goals[0].max_count = 2
    recurring.soft_goals[0].distribution_policy = "every_allowed_day"
    recurring.soft_goals[0].allowed_day_numbers = [1, 2]

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": ["meal"],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": ["meal"],
                },
            ]
        ),
    )

    assert [item.day_number for item in plan.occurrences if item.source_goal_id == "meal"] == [1, 2]


def test_known_soft_goal_in_required_bucket_uses_authoritative_ledger_level():
    recurring = ledger().model_copy(deep=True)
    recurring.soft_goals[0].required_min = 2
    recurring.soft_goals[0].preferred_count = 2
    recurring.soft_goals[0].max_count = 2
    recurring.soft_goals[0].distribution_policy = "every_allowed_day"
    recurring.soft_goals[0].allowed_day_numbers = [1, 2]

    plan = GoalOccurrenceCompiler().compile(
        recurring,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus", "meal"],
                    "requiredGoalCounts": {"campus": 1, "meal": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["museum", "meal"],
                    "requiredGoalCounts": {"museum": 1, "meal": 1},
                    "optionalGoalIds": [],
                },
            ]
        ),
    )

    meals = [item for item in plan.occurrences if item.source_goal_id == "meal"]
    assert [(item.day_number, item.requirement_level) for item in meals] == [
        (1, "explicit_soft"),
        (2, "explicit_soft"),
    ]


def test_truly_unknown_required_goal_still_fails_closed():
    with pytest.raises(ValueError, match="goal_occurrence_unknown_required_goal"):
        GoalOccurrenceCompiler().compile(
            ledger(),
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus", "invented"],
                        "requiredGoalCounts": {"campus": 1, "invented": 1},
                        "optionalGoalIds": [],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["museum"],
                        "requiredGoalCounts": {"museum": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_explicit_soft_every_day_goal_missing_a_day_fails_closed():
    recurring = ledger().model_copy(deep=True)
    recurring.soft_goals[0].required_min = 2
    recurring.soft_goals[0].preferred_count = 2
    recurring.soft_goals[0].max_count = 2
    recurring.soft_goals[0].distribution_policy = "every_allowed_day"
    recurring.soft_goals[0].allowed_day_numbers = [1, 2]

    with pytest.raises(ValueError, match="soft_distribution_invalid"):
        GoalOccurrenceCompiler().compile(
            recurring,
            directive(
                [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["campus"],
                        "requiredGoalCounts": {"campus": 1},
                        "optionalGoalIds": ["meal"],
                    },
                    {
                        "dayNumber": 2,
                        "requiredGoalIds": ["museum"],
                        "requiredGoalCounts": {"museum": 1},
                        "optionalGoalIds": [],
                    },
                ]
            ),
        )


def test_hard_goal_preferred_extra_is_optional_not_a_second_hard_occurrence():
    preferred = ledger().model_copy(deep=True)
    preferred.hard_goals[0].required_min = 1
    preferred.hard_goals[0].preferred_count = 2
    preferred.hard_goals[0].max_count = 2
    preferred.hard_goals[0].allowed_day_numbers = [1, 2]

    plan = GoalOccurrenceCompiler().compile(
        preferred,
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": ["campus"],
                },
            ]
        ),
    )

    campus = [item for item in plan.occurrences if item.source_goal_id == "campus"]
    assert [(item.day_number, item.requirement_level) for item in campus] == [
        (1, "hard"),
        (2, "inferred_preferred"),
    ]


def test_capacity_uses_duration_policy_and_reports_all_minutes():
    plan = GoalOccurrenceCompiler().compile(
        ledger(),
        directive(
            [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": ["meal"],
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": [],
                },
            ]
        ),
    )
    capacity = DailyCapacityPlanner().plan(plan, pace="standard", day_count=2)
    assert capacity[1].target_route_anchors == 2
    assert capacity[1].planned_minutes > 0
    assert capacity[1].unexplained_gap_minutes == 0
    assert "durationPolicy=VisitDurationPolicy" in capacity[1].evidence

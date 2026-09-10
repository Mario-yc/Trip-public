from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler


def test_compiler_uses_contract_not_directive_to_define_hard_goals():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "resolvedTripDates": {"dayCount": 2},
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "museum", "intentType": "museum", "requiredMin": 1}]
            },
        },
        {"requiredGoalIds": ["invented"]},
    )
    assert [goal.goal_id for goal in ledger.hard_goals] == ["museum"]
    assert ledger.day_count == 2


def test_compiler_canonicalizes_public_transit_without_losing_user_preference():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [],
                "transportPreferences": ["公交地铁优先"],
            },
        }
    )

    assert ledger.transport_preferences == ["public_transit"]


def test_compiler_canonicalizes_explicit_public_transport_phrase_without_a_default():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": []},
            "latestUserMessage": "北京两日游，公共交通",
        }
    )

    assert ledger.transport_preferences == ["public_transit"]


def test_compiler_does_not_turn_negated_public_transport_into_a_route_authorization():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": []},
            "latestUserMessage": "北京两日游，不乘公共交通，打车优先",
        }
    )

    assert ledger.transport_preferences == ["driving"]


def test_compiler_excludes_negated_public_transport_without_requiring_priority_wording():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": []},
            "latestUserMessage": "北京两日游，不要坐公共交通，打车",
        }
    )

    assert ledger.transport_preferences == ["driving"]


def test_compiler_excludes_negated_metro_before_accepting_explicit_taxi():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": []},
            "latestUserMessage": "北京两日游，不想乘地铁，改打车",
        }
    )

    assert ledger.transport_preferences == ["driving"]


def test_compiler_preserves_authoritative_goal_cardinality_and_distribution():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "resolvedTripDates": {"dayCount": 2},
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "preferredCount": 1,
                        "maxCount": 1,
                        "cardinalitySource": "explicit_user_request",
                        "distributionPolicy": "spread_across_distinct_days",
                        "allowedDayNumbers": [1, 2],
                    }
                ]
            },
        }
    )

    goal = ledger.hard_goals[0]
    assert (goal.required_min, goal.preferred_count, goal.max_count) == (1, 1, 1)
    assert goal.cardinality_source == "explicit_user_request"
    assert goal.distribution_policy == "spread_across_distinct_days"
    assert goal.allowed_day_numbers == [1, 2]


def test_compiler_joins_experience_spec_policies_by_intent_without_a_second_ledger_source():
    policies = {
        "accessPolicy": "public_outdoor_or_verified_controlled_access",
        "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
        "timeWindow": {"start": "18:30", "end": "22:00"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        "evidenceFreshness": {
            "maxAgeHours": 24,
            "requiredForControlledAccess": True,
        },
        "confidence": 0.0,
    }
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "resolvedTripDates": {"dayCount": 2},
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 2,
                        "preferredCount": 2,
                        "maxCount": 2,
                        "allowedDayNumbers": [1, 2],
                    }
                ],
                "experienceSpecs": [{"intentType": "night_view", **policies}],
            },
        }
    )

    goal = ledger.hard_goals[0]
    dumped = goal.model_dump(by_alias=True)
    assert {key: dumped[key] for key in policies} == policies

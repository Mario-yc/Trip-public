from src.services.bounded_portfolio_optimizer import BoundedPortfolioOptimizer, PortfolioRuntimeLimits


def test_required_pools_are_never_replaced_by_optional_candidates():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["classic", "local"],
        required_pools={
            "campus": [{"amapId": "campus", "localRouteScore": 8}, {"amapId": "campus_2", "localRouteScore": 7}],
            "museum": [{"amapId": "museum", "localRouteScore": 9}],
        },
        optional_pools={
            "food": [
                {"amapId": "food", "localRouteScore": 2, "briefIds": ["local"]},
                {"amapId": "campus", "localRouteScore": 99},
            ]
        },
        limits=PortfolioRuntimeLimits(beam_width=4, max_candidates_per_brief=1),
    )
    assert len(result) == 2
    assert all(set(item.required) == {"campus", "museum"} for item in result)
    by_brief = {item.brief_id: item for item in result}
    assert by_brief["classic"].optional == []
    assert by_brief["local"].optional[0]["amapId"] == "food"


def test_identical_required_combinations_can_be_reused_but_need_optional_structure_to_be_visible():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["classic", "local"],
        required_pools={"campus": [{"amapId": "campus"}], "museum": [{"amapId": "museum"}]},
        optional_pools={},
    )
    assert len(result) == 2
    assert result[0].required == result[1].required
    assert result[0].optional == result[1].optional == []


def test_missing_required_pool_is_fail_closed():
    assert (
        BoundedPortfolioOptimizer().solve(brief_ids=["classic"], required_pools={"museum": []}, optional_pools={}) == []
    )


def test_required_goals_sharing_only_one_physical_poi_fail_closed():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={
            "campus": [{"amapId": "same-place"}],
            "museum": [{"amapId": "same-place"}],
        },
        optional_pools={},
    )

    assert result == []


def test_amap_parent_and_child_cannot_fill_two_distinct_required_slots():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={
            "night_day_1": [{"amapId": "B000AA3ZCC", "dayNumber": 1}],
            "night_day_2": [
                {
                    "amapId": "B0FFILD7HG",
                    "parentPoiId": "B000AA3ZCC",
                    "dayNumber": 2,
                }
            ],
        },
        optional_pools={},
    )

    assert result == []


def test_required_candidate_without_identity_fails_closed():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"name": "missing identity"}]},
        optional_pools={},
    )

    assert result == []


def test_two_optional_slots_cannot_reuse_the_same_grounded_poi_identity():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"amapId": "campus"}]},
        optional_pools={
            "family_a": [{"amapId": "shared"}, {"amapId": "a"}],
            "family_b": [{"amapId": "shared"}, {"amapId": "b"}],
        },
    )

    optional_ids = [item["amapId"] for item in result[0].optional]
    assert len(optional_ids) == 2
    assert len(set(optional_ids)) == 2


def test_soft_goal_is_allocated_before_optional_and_reserves_its_poi_identity():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"amapId": "campus"}]},
        soft_pools={
            "brief": {
                "meal": [
                    {"amapId": "shared-meal"},
                    {"amapId": "fallback-meal"},
                ],
            },
        },
        optional_pools={
            "brief": {
                "local_food": [
                    {"amapId": "shared-meal"},
                    {"amapId": "optional-food"},
                ],
            },
        },
    )

    assert result[0].soft["meal"]["amapId"] == "shared-meal"
    assert result[0].optional[0]["amapId"] == "optional-food"


def test_soft_meal_prefers_same_day_route_corridor_over_far_higher_evidence_candidate():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={
            "campus": [
                {
                    "amapId": "B000CAMPUS1",
                    "dayNumber": 1,
                    "startTime": "09:00",
                    "longitude": 116.326,
                    "latitude": 39.992,
                }
            ],
            "night": [
                {
                    "amapId": "B000NIGHT01",
                    "dayNumber": 1,
                    "startTime": "18:00",
                    "longitude": 116.405,
                    "latitude": 39.929,
                }
            ],
        },
        soft_pools={
            "brief": {
                "meal": [
                    {
                        "amapId": "B000FARMEAL",
                        "dayNumber": 1,
                        "startTime": "12:00",
                        "longitude": 117.2,
                        "latitude": 40.15,
                        "consumerEvidenceScore": 0.99,
                        "localRouteScore": 9,
                    },
                    {
                        "amapId": "B000NEARMEAL",
                        "dayNumber": 1,
                        "startTime": "12:00",
                        "longitude": 116.36,
                        "latitude": 39.965,
                        "consumerEvidenceScore": 0.8,
                        "localRouteScore": 5,
                    },
                ]
            }
        },
        optional_pools={"brief": {}},
    )

    assert result[0].soft["meal"]["amapId"] == "B000NEARMEAL"


def test_soft_meal_route_ordering_happens_before_beam_truncation():
    far_candidates = [
        {
            "amapId": f"B000FAR00{index}",
            "dayNumber": 1,
            "startTime": "12:00",
            "longitude": 116.75 + index * 0.01,
            "latitude": 40.20,
            "consumerEvidenceScore": 0.99,
        }
        for index in range(6)
    ]
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={
            "campus": [
                {
                    "amapId": "B000CAMPUS1",
                    "dayNumber": 1,
                    "startTime": "09:00",
                    "longitude": 116.32,
                    "latitude": 39.99,
                }
            ],
            "night": [
                {
                    "amapId": "B000NIGHT01",
                    "dayNumber": 1,
                    "startTime": "18:30",
                    "longitude": 116.42,
                    "latitude": 39.93,
                }
            ],
        },
        soft_pools={
            "brief": {
                "meal": [
                    *far_candidates,
                    {
                        "amapId": "B0AFTERBEAM",
                        "dayNumber": 1,
                        "startTime": "12:00",
                        "longitude": 116.37,
                        "latitude": 39.96,
                        "consumerEvidenceScore": 0.80,
                    },
                ]
            }
        },
        optional_pools={"brief": {}},
        limits=PortfolioRuntimeLimits(beam_width=6),
    )

    assert result[0].soft["meal"]["amapId"] == "B0AFTERBEAM"


def test_soft_occurrence_cannot_reuse_required_identity_for_same_goal_on_another_day():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={
            "occ:goal_campus_visit:day:1": [
                {
                    "amapId": "campus-shared",
                    "dayNumber": 1,
                    "sourceGoalId": "goal_campus_visit",
                    "distinctGroupId": "distinct:goal_campus_visit",
                }
            ]
        },
        soft_pools={
            "brief": {
                "occ:goal_campus_visit:day:2": [
                    {
                        "amapId": "campus-shared",
                        "dayNumber": 2,
                        "softGoalId": "goal_campus_visit",
                        "distinctGroupId": "distinct:goal_campus_visit",
                    },
                    {
                        "amapId": "campus-distinct",
                        "dayNumber": 2,
                        "softGoalId": "goal_campus_visit",
                        "distinctGroupId": "distinct:goal_campus_visit",
                    },
                ],
            },
        },
        optional_pools={"brief": {}},
    )

    assert result[0].soft["occ:goal_campus_visit:day:2"]["amapId"] == "campus-distinct"


def test_soft_occurrence_is_omitted_when_distinct_group_has_no_unused_identity():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={
            "occ:goal_campus_visit:day:1": [
                {
                    "amapId": "campus-shared",
                    "dayNumber": 1,
                    "distinctGroupId": "distinct:goal_campus_visit",
                }
            ]
        },
        soft_pools={
            "brief": {
                "occ:goal_campus_visit:day:2": [
                    {
                        "amapId": "campus-shared",
                        "dayNumber": 2,
                        "distinctGroupId": "distinct:goal_campus_visit",
                    }
                ]
            }
        },
        optional_pools={"brief": {}},
    )

    assert result[0].soft == {}


def test_optional_identity_reservation_is_scoped_to_same_brief_and_day():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"amapId": "campus", "dayNumber": 1}]},
        optional_pools={
            "brief": {
                "day1_walk": [{"amapId": "shared", "dayNumber": 1}],
                "day2_walk": [{"amapId": "shared", "dayNumber": 2}],
            },
        },
    )

    assert [(item["amapId"], item["dayNumber"]) for item in result[0].optional] == [
        ("shared", 1),
        ("shared", 2),
    ]


def test_optional_identity_duplicate_on_same_day_uses_next_candidate():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"amapId": "campus", "dayNumber": 1}]},
        optional_pools={
            "brief": {
                "walk_a": [{"amapId": "shared", "dayNumber": 1}],
                "walk_b": [
                    {"amapId": "shared", "dayNumber": 1},
                    {"amapId": "other", "dayNumber": 1},
                ],
            },
        },
    )

    assert {item["amapId"] for item in result[0].optional} == {"shared", "other"}


def test_optional_parent_child_collision_uses_a_different_physical_place():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"amapId": "B000CAMPUS", "dayNumber": 1}]},
        optional_pools={
            "brief": {
                "night_a": [{"amapId": "B000AA3ZCC", "dayNumber": 1}],
                "night_b": [
                    {
                        "amapId": "B0FFILD7HG",
                        "parentPoiId": "B000AA3ZCC",
                        "dayNumber": 1,
                    },
                    {"amapId": "B000OTHER1", "dayNumber": 1},
                ],
            },
        },
    )

    assert {item["amapId"] for item in result[0].optional} == {
        "B000AA3ZCC",
        "B000OTHER1",
    }


def test_gate_passed_optional_candidates_are_ranked_by_consumer_evidence_score():
    result = BoundedPortfolioOptimizer().solve(
        brief_ids=["brief"],
        required_pools={"campus": [{"amapId": "campus"}]},
        optional_pools={
            "brief": {
                "local_life": [
                    {"amapId": "weak", "consumerEvidenceScore": 0.2, "localRouteScore": 9},
                    {"amapId": "strong", "consumerEvidenceScore": 0.9, "localRouteScore": 5},
                ]
            }
        },
    )
    assert result[0].optional[0]["amapId"] == "strong"

from src.services.agent_service import AgentService
from src.services.creative_proposal_title_service import CreativeProposalTitleService


def test_agent_route_difference_choice_uses_real_coordinates_and_existing_anchors():
    candidates = [
        {"id": "B00000001", "latitude": 39.901, "longitude": 116.301},
        {"id": "B00000002", "latitude": 39.99, "longitude": 116.49},
    ]
    anchors = [{"amapId": "B00000003", "latitude": 39.9, "longitude": 116.3}]

    selected = AgentService._portfolio_density_max_route_difference_candidate(
        candidates,
        anchors,
    )

    assert selected is not None
    assert selected["id"] == "B00000002"


def test_agent_route_difference_choice_is_omitted_without_truthful_coordinates():
    assert (
        AgentService._portfolio_density_max_route_difference_candidate(
            [{"id": "B00000001", "name": "候选"}],
            [{"amapId": "B00000003", "name": "已有地点"}],
        )
        is None
    )


def test_root_hard_pending_slot_blocks_partial_timeline_even_when_soft_slots_are_allowed():
    blockers = AgentService._root_required_pending_slots(
        {
            "portfolioPendingSlots": [
                {"sourceGoalId": "goal_meal", "intentType": "meal", "dayNumber": 1},
                {
                    "sourceGoalId": "goal_night_view",
                    "intentType": "night_view",
                    "dayNumber": 2,
                    "planningSlotId": "night_day_2",
                },
            ]
        },
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "goal_meal", "requiredMin": 2, "requirementLevel": "soft_experience"},
                    {"goalId": "goal_night_view", "requiredMin": 2, "requirementLevel": "required"},
                ]
            }
        },
    )

    assert blockers == [
        {
            "goalId": "goal_night_view",
            "intentType": "night_view",
            "dayNumber": 2,
            "planningSlotId": "night_day_2",
        }
    ]


def test_root_hard_staging_gap_stops_frontier_after_consumer_admission_failure():
    blockers = AgentService._root_required_staging_gaps(
        {
            "perBriefDiagnostics": [
                {
                    "hardFailures": [
                        "required_goal_ungrounded:goal_night_view",
                        "required_goal_count_insufficient:goal_night_view:0/2",
                        "portfolio_optional_consumer_admission_invalid:seg_soft",
                    ]
                }
            ]
        },
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "requiredMin": 2,
                        "requirementLevel": "soft_experience",
                    },
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 2,
                        "requirementLevel": "required",
                    },
                ]
            }
        },
    )

    assert blockers == [
        {
            "goalId": "goal_night_view",
            "intentType": "night_view",
            "missingCount": 1,
            "reasonCodes": [
                "required_goal_count_insufficient:goal_night_view:0/2",
                "required_goal_ungrounded:goal_night_view",
            ],
        }
    ]


def test_repeated_hard_goal_with_one_grounded_occurrence_can_remain_an_explicit_partial():
    assert AgentService._root_required_gaps_are_recoverable(
        [
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "missingCount": 1,
                "reasonCodes": [
                    "required_goal_count_insufficient:goal_night_view:1/2",
                    "required_goal_omitted:goal_night_view",
                    "goal_occurrence_missing:occ:goal_night_view:day:2:day_2",
                ],
            }
        ]
    ) is True


def test_zero_grounded_or_single_occurrence_hard_goal_stays_fail_closed():
    assert AgentService._root_required_gaps_are_recoverable(
        [
            {
                "goalId": "goal_night_view",
                "reasonCodes": [
                    "required_goal_count_insufficient:goal_night_view:0/2",
                ],
            }
        ]
    ) is False
    assert AgentService._root_required_gaps_are_recoverable(
        [
            {
                "goalId": "goal_campus_visit",
                "reasonCodes": [
                    "required_goal_count_insufficient:goal_campus_visit:0/1",
                ],
            }
        ]
    ) is False


def test_night_discovery_reads_agent_experience_families_contract():
    hints = AgentService._night_view_semantic_search_strategies(
        AgentService.__new__(AgentService),
        "测试城",
        {
            "experienceSpecs": [
                {
                    "intentType": "night_view",
                    "experienceFamilies": [
                        "historic_lit_street",
                        "waterfront_evening",
                    ],
                }
            ]
        },
    )

    assert hints == [
        "测试城 灯光历史街区 夜景",
        "测试城 滨水夜间公共空间 夜景",
    ]


def test_night_discovery_without_agent_experience_family_fails_closed():
    assert (
        AgentService._night_view_semantic_search_strategies(
            AgentService.__new__(AgentService),
            "测试城",
            {"experienceSpecs": []},
        )
        == []
    )


def test_candidate_gap_summary_is_derived_from_real_admission_rejections():
    summary = AgentService._candidate_gap_summary(
        {
            "rootGlobalBlocker": {
                "status": "candidate_refresh_required",
                "goals": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 2,
                        "actualCount": 0,
                        "missingCount": 2,
                        "allowedDayNumbers": [1, 2],
                    }
                ],
            },
            "blockingRequiredIntents": [
                {
                    "goalId": "goal_night_view",
                    "intentType": "night_view",
                    "poolId": "night_day_1",
                    "targetCount": 2,
                    "selectedCount": 0,
                    "missingCount": 2,
                }
            ],
            "requiredIntentCoverage": [
                {
                    "goalId": "goal_night_view",
                    "intentType": "night_view",
                    "poolId": "night_day_1",
                    "targetCount": 2,
                    "selectedCount": 0,
                    "missingCount": 2,
                }
            ],
            "poolReports": [
                {
                    "poolId": "night_day_1",
                    "briefId": "brief_night",
                    "sourceGoalId": "goal_night_view",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "targetCount": 2,
                    "selectedCount": 0,
                    "missingCount": 2,
                    "requiredSlotIds": ["night_slot_1", "night_slot_2"],
                    "resolvedSlotIds": [],
                    "unresolvedSlotIds": ["night_slot_1", "night_slot_2"],
                    "slotDayNumbers": {"night_slot_1": 1, "night_slot_2": 2},
                    "candidateCount": 7,
                    "admittedCount": 0,
                    "rejectedReasonCounts": {
                        "night_view_explicitly_unavailable": 2,
                        "night_view_availability_unverified": 2,
                        "weak_night_view_entity": 1,
                        "night_view_dining_or_non_view": 1,
                        "night_view_signal_missing": 1,
                    },
                }
            ],
        },
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 2,
                        "allowedDayNumbers": [1, 2],
                    }
                ]
            },
            "goalOccurrencePlan": {
                "occurrences": [
                    {
                        "occurrenceId": "night_occurrence_1",
                        "sourceGoalId": "goal_night_view",
                        "intentType": "night_view",
                        "dayNumber": 1,
                    },
                    {
                        "occurrenceId": "night_occurrence_2",
                        "sourceGoalId": "goal_night_view",
                        "intentType": "night_view",
                        "dayNumber": 2,
                    },
                ]
            },
        },
    )

    assert summary["status"] == "candidate_refresh_required"
    assert summary["missingOccurrenceCount"] == 2
    assert summary["poolEvidence"] == [
        {
            "poolId": "night_day_1",
            "briefId": "brief_night",
            "intentType": "night_view",
            "dayNumber": 1,
            "candidateCount": 7,
            "admittedCount": 0,
            "sourceGoalId": "goal_night_view",
            "requiredSlotIds": ["night_slot_1", "night_slot_2"],
            "unresolvedSlotIds": ["night_slot_1", "night_slot_2"],
            "slotDayNumbers": {"night_slot_1": 1, "night_slot_2": 2},
        }
    ]
    assert sum(summary["rejectedReasonCounts"].values()) == 7
    assert len(summary["fingerprint"]) == 64


def test_complete_grounded_title_requires_agent_candidates_bound_to_all_pois():
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": {
                            "amapId": "B00000001",
                            "name": "亮马河国际风情水岸",
                            "source": "amap-place-search",
                            "latitude": 39.95,
                            "longitude": 116.48,
                        },
                        "semanticMetadata": {
                            "portfolioOptional": True,
                            "optionalExperienceFamily": "night_view",
                        },
                    }
                ],
            },
            {
                "dayNumber": 2,
                "segments": [
                    {
                        "poi": {
                            "amapId": "B00000002",
                            "name": "奥林匹克塔",
                            "source": "amap-place-search",
                            "latitude": 40.0,
                            "longitude": 116.39,
                        },
                        "semanticMetadata": {
                            "portfolioOptional": True,
                            "optionalExperienceFamily": "night_view",
                        },
                    }
                ],
            },
        ],
    }

    snapshot["portfolioVerifier"] = {"passed": True}
    evidence_ids = ["B00000001", "B00000002"]
    titled = CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=lambda _context: {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "奥林匹克塔映京城风物", "evidenceAmapIds": evidence_ids},
                {"title": "奥林匹克塔下城市漫游", "evidenceAmapIds": evidence_ids},
                {"title": "奥林匹克塔伴水岸行旅", "evidenceAmapIds": evidence_ids},
            ],
        },
        context={},
    )

    assert titled["title"] == "奥林匹克塔映京城风物"
    assert titled["portfolioTitleGeneration"]["status"] == "succeeded"
    assert titled["portfolioTitleEvidence"]["selectedAmapIds"] == evidence_ids
    assert titled["title"] != "北京｜亮马河国际风情水岸、奥林匹克塔"

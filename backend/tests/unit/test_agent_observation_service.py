from src.services.agent_autonomy_service import AgentDecisionArbitrator
from src.services.agent_observation_service import AgentObservationBuilder


def _canonical_route(
    start: str,
    end: str,
    *,
    duration_seconds: int = 18 * 60,
    selected: bool = True,
    status: str = "verified",
    error=None,
) -> dict:
    return {
        "id": f"route_{start}_{end}",
        "fromSegmentId": start,
        "toSegmentId": end,
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "mode": "transit",
        "isSelected": selected,
        "distanceMeters": 1200,
        "durationSeconds": duration_seconds,
        "polyline": [[116.3, 39.9], [116.31, 39.91]],
        "queriedAt": "2026-08-02T00:00:00+00:00",
        "status": status,
        "error": error,
    }


def test_empty_editable_scaffold_fallback_requires_explicit_safe_draft_choice():
    observation = AgentObservationBuilder().build(
        {
            "latestUserMessage": "北京两日游，10月1日到2日，1人，中等预算，公交地铁优先，帮我安排",
            "effectiveUserMessage": "北京两日游，10月1日到2日，1人，中等预算，公交地铁优先，帮我安排",
            "currentItinerarySnapshot": {"id": "plan_1", "days": [{"segments": []}]},
            "activeVersionId": None,
        }
    )

    decision = AgentDecisionArbitrator().decide(observation)

    assert observation.itinerary.lifecycle_state == "empty_scaffold"
    assert observation.itinerary.meaningful_segment_count == 0
    assert decision is not None
    assert decision.primary_action == "ask_user"
    assert decision.clarification is not None
    assert decision.clarification["options"][1]["id"] == "confirm_rule_safe_draft"


def test_transport_preference_is_not_route_optimization_command():
    observation = AgentObservationBuilder().build(
        {
            "latestUserMessage": "北京两日游，10月1日到2日，1人，中等预算，公交地铁优先",
            "effectiveUserMessage": "北京两日游，10月1日到2日，1人，中等预算，公交地铁优先",
            "currentItinerarySnapshot": {"id": "plan_1", "days": [{"segments": []}]},
        }
    )

    decision = AgentDecisionArbitrator().decide(observation)

    assert decision is not None
    assert decision.primary_action == "ask_user"


def test_observation_separates_confirmed_route_from_complete_door_to_door_and_normalizes_reason():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_1",
                                "kind": "visit",
                                "startTime": "09:00",
                                "endTime": "10:00",
                                "poi": {"name": "A", "amapId": "a"},
                            },
                            {
                                "id": "seg_2",
                                "kind": "meal",
                                "startTime": "12:00",
                                "endTime": "13:00",
                                "notes": "pendingMeal=true；reason=ok",
                                "poi": {"name": "午餐", "groundingStatus": "waiting_for_poi_grounding"},
                            },
                        ],
                    }
                ],
            },
        }
    )

    assert observation.requirement_coverage.unresolved_required_slot_count == 0
    assert observation.unresolved_slots[0].reason == "waiting_for_poi_grounding"
    assert observation.route_state.complete_door_to_door_status == "not_ready"
    assert observation.map_state.complete_intent_map_ready is True
    assert observation.invariant_errors == []


def test_persisted_skeleton_unresolved_slot_uses_structured_metadata_over_conflicting_notes():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "dayNumber": 2,
                        "segments": [
                            {
                                "id": "seg_museum",
                                "kind": "visit",
                                "startTime": "09:00",
                                "endTime": "10:30",
                                "notes": "goalId=goal_wrong；requirementLevel=optional；intentType：campus_visit",
                                "semanticMetadata": {
                                    "goalId": "goal_museum",
                                    "requirementLevel": "required",
                                    "required": True,
                                    "intentType": "museum",
                                    "routeAnchor": False,
                                    "groundingStatus": "waiting_for_poi_grounding",
                                },
                                "poi": {
                                    "name": "博物馆或美术馆",
                                    "groundingStatus": "waiting_for_poi_grounding",
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    slot = observation.unresolved_slots[0]
    assert slot.segment_id == "seg_museum"
    assert slot.goal_id == "goal_museum"
    assert slot.intent_type == "museum"
    assert slot.raw_need == "博物馆或美术馆"
    assert slot.required is True


def test_confirmed_visit_without_structured_route_anchor_does_not_invent_route_leg():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_1",
                                "kind": "visit",
                                "semanticMetadata": {"routeAnchor": False, "required": False},
                                "poi": {"name": "A", "amapId": "a", "groundingStatus": "confirmed"},
                            },
                            {
                                "id": "seg_2",
                                "kind": "visit",
                                "semanticMetadata": {"routeAnchor": False, "required": False},
                                "poi": {"name": "B", "amapId": "b", "groundingStatus": "confirmed"},
                            },
                        ],
                    }
                ],
            },
        }
    )

    assert all(item.route_anchor is False for item in observation.segment_refs)
    assert observation.route_state.confirmed_anchor_required_legs == 0
    assert observation.route_state.confirmed_anchor_covered_legs == 0


def test_segment_ref_projects_goal_identity_from_goal_ledger_when_metadata_has_only_intent():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_museum",
                        "intentType": "museum",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    }
                ]
            },
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_museum",
                                "kind": "visit",
                                "notes": "goalId=goal_wrong；requirementLevel=optional",
                                "semanticMetadata": {
                                    "intentType": "museum",
                                    "required": True,
                                    "routeAnchor": True,
                                    "groundingStatus": "agent_selected_candidate",
                                },
                                "poi": {
                                    "name": "中国美术馆",
                                    "amapId": "amap_museum",
                                    "type": "科教文化服务;博物馆;美术馆",
                                    "groundingStatus": "agent_selected_candidate",
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    segment = observation.segment_refs[0]
    assert segment.intent_type == "museum"
    assert segment.goal_id == "goal_museum"
    assert segment.requirement_level == "required"


def test_structured_draft_only_segment_remains_optional_unresolved_without_hard_goal():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_meal",
                                "kind": "meal",
                                "notes": "pendingMeal=false；needsConcretePoi=false",
                                "semanticMetadata": {
                                    "intentType": "meal",
                                    "groundingStatus": "draft_only",
                                    "routeAnchor": False,
                                    "required": True,
                                },
                                "poi": {"name": "午餐", "groundingStatus": "draft_only"},
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert len(observation.unresolved_slots) == 1
    slot = observation.unresolved_slots[0]
    assert slot.reason == "draft_only"
    assert slot.intent_type == "meal"
    assert slot.required is False
    assert observation.requirement_coverage.unresolved_required_slot_count == 0


def test_goal_ledger_identity_is_capped_to_required_min_for_extra_same_intent_segments():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_museum",
                        "intentType": "museum",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    }
                ]
            },
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_required_museum",
                                "kind": "visit",
                                "semanticMetadata": {
                                    "intentType": "museum",
                                    "groundingStatus": "verified_amap",
                                    "routeAnchor": True,
                                },
                                "poi": {
                                    "name": "中国美术馆",
                                    "amapId": "amap_1",
                                    "type": "科教文化服务;博物馆;美术馆",
                                    "groundingStatus": "verified_amap",
                                },
                            },
                            {
                                "id": "seg_extra_museum",
                                "kind": "visit",
                                "semanticMetadata": {
                                    "intentType": "museum",
                                    "groundingStatus": "verified_amap",
                                    "routeAnchor": True,
                                },
                                "poi": {
                                    "name": "故宫博物院",
                                    "amapId": "amap_2",
                                    "type": "科教文化服务;博物馆;博物馆",
                                    "groundingStatus": "verified_amap",
                                },
                            },
                        ],
                    }
                ],
            },
        }
    )

    refs = {item.segment_id: item for item in observation.segment_refs}
    assert refs["seg_required_museum"].goal_id == "goal_museum"
    assert refs["seg_required_museum"].requirement_level == "required"
    assert refs["seg_extra_museum"].goal_id is None
    assert refs["seg_extra_museum"].requirement_level == ""


def test_observation_genuine_free_gap_excludes_selected_route_duration():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_1",
                                "kind": "visit",
                                "startTime": "09:00",
                                "endTime": "10:00",
                                "poi": {"name": "A", "amapId": "a"},
                            },
                            {
                                "id": "seg_2",
                                "kind": "visit",
                                "startTime": "12:00",
                                "endTime": "13:00",
                                "poi": {"name": "B", "amapId": "b"},
                            },
                        ],
                    }
                ],
                "routeOptions": [
                    _canonical_route("seg_1", "seg_2"),
                    _canonical_route("seg_1", "seg_2", selected=False, duration_seconds=90 * 60),
                ],
            },
        }
    )

    assert observation.schedule_state.genuine_free_gap_minutes == 102


def test_observation_route_coverage_never_creates_cross_day_leg():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "id": "day_1",
                        "dayNumber": 1,
                        "segments": [
                            {"id": "seg_1", "kind": "visit", "routeAnchor": True, "poi": {"name": "A", "amapId": "a", "routeable": True}},
                            {"id": "seg_2", "kind": "visit", "routeAnchor": True, "poi": {"name": "B", "amapId": "b", "routeable": True}},
                        ],
                    },
                    {
                        "id": "day_2",
                        "dayNumber": 2,
                        "segments": [
                            {"id": "seg_3", "kind": "visit", "routeAnchor": True, "poi": {"name": "C", "amapId": "c", "routeable": True}},
                            {"id": "seg_4", "kind": "visit", "routeAnchor": True, "poi": {"name": "D", "amapId": "d", "routeable": True}},
                        ],
                    },
                ],
                "routeOptions": [
                    _canonical_route("seg_1", "seg_2"),
                    _canonical_route("seg_3", "seg_4"),
                ],
            },
        }
    )

    assert observation.route_state.confirmed_anchor_required_legs == 2
    assert observation.route_state.confirmed_anchor_covered_legs == 2
    assert observation.route_state.confirmed_anchor_route_ready is True
    assert observation.route_state.complete_door_to_door_status == "ready"


def test_observation_does_not_count_failed_route_option_as_covered():
    snapshot = {
        "id": "plan_failed_route",
        "days": [
            {
                "id": "day_1",
                "dayNumber": 1,
                "segments": [
                    {"id": "seg_1", "kind": "visit", "routeAnchor": True, "poi": {"id": "poi_1", "name": "A", "amapId": "a", "routeable": True}},
                    {"id": "seg_2", "kind": "visit", "routeAnchor": True, "poi": {"id": "poi_2", "name": "B", "amapId": "b", "routeable": True}},
                ],
            }
        ],
        "routeOptions": [
            _canonical_route(
                "seg_1",
                "seg_2",
                status="failed",
                error={"code": "provider_timeout"},
            )
        ],
    }

    observation = AgentObservationBuilder().build({"currentItinerarySnapshot": snapshot})

    assert observation.route_state.confirmed_anchor_required_legs == 1
    assert observation.route_state.confirmed_anchor_covered_legs == 0
    assert observation.route_state.confirmed_anchor_route_ready is False


def test_observation_goal_ledger_does_not_count_semantic_museum_mismatch_as_satisfied():
    observation = AgentObservationBuilder().build(
        {
            "activeVersionId": "ver_museum_bad",
            "requestIntentContract": {
                "requiredIntents": [{"intentType": "museum", "target": 1, "source": "explicit_user_request"}]
            },
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_museum_bad",
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_museum_bad",
                                "kind": "visit",
                                "startTime": "17:51",
                                "endTime": "19:21",
                                "notes": "legacy prose must not own intent identity",
                                "semanticMetadata": {
                                    "goalId": "goal_museum",
                                    "intentType": "museum",
                                    "requirementLevel": "required",
                                    "required": True,
                                    "routeAnchor": True,
                                    "groundingStatus": "agent_selected_candidate",
                                },
                                "poi": {
                                    "name": "花海畔溪谷",
                                    "type": "风景名胜;风景名胜;风景名胜",
                                    "amapId": "B0BADSCENIC",
                                    "source": "amap-place-search",
                                    "latitude": 39.9,
                                    "longitude": 116.4,
                                    "intentType": "landmark",
                                    "groundingStatus": "agent_selected_candidate",
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    goal = observation.requirement_coverage.required[0]
    assert goal["satisfiedCount"] == 0
    assert goal["status"] == "unresolved"
    assert goal["invalidClaims"][0]["poiName"] == "花海畔溪谷"
    assert observation.requirement_coverage.unresolved_required_slot_count == 1
    assert observation.map_state.complete_intent_map_ready is False


def test_observation_invariant_reports_contradictory_complete_route_state():
    builder = AgentObservationBuilder()
    observation = builder.build(
        {
            "activeVersionId": "ver_1",
            "currentItinerarySnapshot": {
                "id": "plan_1",
                "versionId": "ver_1",
                "days": [
                    {
                        "segments": [
                            {
                                "id": "seg_1",
                                "kind": "meal",
                                "poi": {"name": "午餐", "groundingStatus": "waiting_for_poi_grounding"},
                            }
                        ]
                    }
                ],
            },
        }
    )

    assert observation.route_state.complete_door_to_door_status == "not_ready"
    assert "unresolved_required_slot_with_complete_route" not in observation.invariant_errors


def test_observation_projects_persisted_pre_version_planning_slots_without_segment_identity():
    observation = AgentObservationBuilder().build(
        {
            "latestUserMessage": "重试模型规划",
            "effectiveUserMessage": "北京985高校和博物馆两日游，10月1日到2日",
            "activeVersionId": None,
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_campus_visit",
                        "intentType": "campus_visit",
                        "requiredMin": 1,
                        "requirementLevel": "hard_requirement",
                    },
                    {
                        "goalId": "goal_museum",
                        "intentType": "museum",
                        "requiredMin": 1,
                        "requirementLevel": "hard_requirement",
                    },
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "requiredMin": 1,
                        "requirementLevel": "soft_experience",
                    },
                ]
            },
            "resumePlanningAttempt": {
                "enabled": True,
                "sourceAssistantTurnId": "turn_waiting",
                "resultState": "waiting_for_poi_grounding",
                "initialPlan": {"mode": "day_slots"},
                "nextActions": ["retry_unfinished_poi_grounding"],
                "unresolvedSlots": [
                    {
                        "slotId": "day1_goal_campus_visit_1",
                        "dayNumber": 1,
                        "intentType": "campus_visit",
                        "poolId": "goal_campus_visit_pool",
                        "rawNeed": "985高校参观",
                        "reason": "low_confidence_or_ambiguous_candidates",
                        "candidateHintCount": 8,
                        "topCandidatePreview": {"name": "北京大学", "score": 1.0},
                    },
                    {
                        "slotId": "day2_goal_museum_1",
                        "dayNumber": 2,
                        "intentType": "museum",
                        "poolId": "goal_museum_pool",
                        "rawNeed": "博物馆或美术馆",
                        "reason": "waiting_for_poi_grounding",
                    },
                    {
                        "slotId": "day1_goal_meal_2",
                        "dayNumber": 1,
                        "intentType": "meal",
                        "poolId": "goal_meal_pool",
                        "rawNeed": "北京当地特色美食",
                        "reason": "optional_waiting",
                    },
                ],
            },
        }
    )

    assert observation.planning_attempt.persisted is True
    assert observation.planning_attempt.source_assistant_turn_id == "turn_waiting"
    assert observation.target_inventory.segment_ids == []
    assert observation.target_inventory.planning_slot_ids == [
        "day1_goal_campus_visit_1",
        "day2_goal_museum_1",
        "day1_goal_meal_2",
    ]
    slots = {item.goal_id: item for item in observation.unresolved_slots}
    assert slots["goal_campus_visit"].required is True
    assert slots["goal_campus_visit"].candidate_evidence["name"] == "北京大学"
    assert slots["goal_museum"].next_action == "resolve_poi"
    assert slots["goal_meal"].required is False
    assert observation.requirement_coverage.unresolved_required_slot_count == 2
    assert observation.invariant_errors == []


def test_observation_preserves_explicit_zero_required_min_for_soft_experience():
    observation = AgentObservationBuilder().build(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_local_life",
                        "intentType": "local_culture",
                        "target": 1,
                        "requiredMin": 0,
                        "preferredCount": 1,
                        "requirementLevel": "soft_experience",
                    }
                ]
            }
        }
    )

    coverage = observation.requirement_coverage.required[0]
    assert coverage["requiredMin"] == 0
    assert coverage["status"] == "optional_pending"


def test_active_partial_snapshot_is_the_only_pending_slot_truth():
    snapshot = {
        "id": "plan_partial",
        "status": "partial",
        "portfolioPartialTimeline": {"status": "partial"},
        "portfolioSelectionContext": {
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": "portfolio_root",
            "focusBriefId": "brief_focus",
            "requestContractFingerprint": "f" * 24,
        },
        "portfolioPendingSlots": [
            {
                "briefId": "brief_focus",
                "poolId": "pool_night",
                "planningSlotId": "slot_night_day_1",
                "dayNumber": 1,
                "timeWindow": "20:00-21:15",
                "intentType": "night_view",
                "requirementLevel": "preferred",
                "rawNeed": "夜景观景点",
            }
        ],
        "days": [{"id": "day_1", "dayNumber": 1, "segments": []}],
    }
    observation = AgentObservationBuilder().build(
        {
            "currentItinerarySnapshot": snapshot,
            "activeVersionId": "version_partial",
            "pendingAmapPoiCandidates": [
                {
                    "briefId": "brief_focus",
                    "poolId": "pool_night",
                    "planningSlotId": "slot_night_day_1",
                    "dayNumber": 1,
                    "candidates": [{"id": "amap_night", "name": "景山公园"}],
                }
            ],
        }
    )

    slot = observation.unresolved_slots[0]
    assert (slot.brief_id, slot.pool_id, slot.planning_slot_id, slot.day_number) == (
        "brief_focus", "pool_night", "slot_night_day_1", 1,
    )
    assert slot.required is False
    assert slot.candidate_count == 1
    assert observation.target_inventory.planning_slot_ids == ["slot_night_day_1"]
    assert observation.partial_timeline_state.active is True
    assert observation.partial_timeline_state.pending_slot_count == 1

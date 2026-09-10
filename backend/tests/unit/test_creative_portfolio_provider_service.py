import json

import pytest

from src.core.config import get_settings
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import CreativePortfolioProviderService, InitialCreativePortfolio
from src.services.agent_service import AgentService
from src.services.creative_planning_models import ConstraintLedger
from src.services.itinerary_schedule_service import ItineraryScheduleService


def _ledger():
    return ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit"},
                    {"goalId": "museum", "intentType": "museum"},
                ]
            },
        }
    )


def test_deterministic_fallback_places_required_night_view_in_night_window():
    ledger = ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "北京",
            "startDate": "2026-10-01",
            "endDate": "2026-10-02",
            "dayCount": 2,
            "hardGoals": [
                {"goalId": "campus", "intentType": "campus_visit", "requiredMin": 1},
                {
                    "goalId": "night",
                    "intentType": "night_view",
                    "requiredMin": 2,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "cardinalitySource": "explicit_every_day",
                    "allowedDayNumbers": [1, 2],
                },
            ],
            "sourceFingerprint": "night-window-test-fingerprint",
        }
    )
    directive = {
        "dayStrategies": [
            {
                "dayNumber": 1,
                "requiredGoalIds": ["campus", "night"],
                "requiredGoalCounts": {"campus": 1, "night": 1},
                "optionalGoalIds": [],
            },
            {
                "dayNumber": 2,
                "requiredGoalIds": ["night"],
                "requiredGoalCounts": {"night": 1},
                "optionalGoalIds": [],
            },
        ]
    }

    fallback = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive=directive,
        schema_repair_attempts=0,
    )

    night_slots = [
        slot for proposal in fallback.proposals for slot in proposal.day_slots if slot.required_goal_id == "night"
    ]
    assert night_slots
    assert {slot.day_number for slot in night_slots} == {1, 2}
    assert all(slot.time_window == "night" for slot in night_slots)


def test_deterministic_fallback_preserves_user_explicit_exact_entity_binding():
    ledger = ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "测试城市",
            "dayCount": 1,
            "hardGoals": [
                {
                    "goalId": "museum",
                    "intentType": "museum",
                    "requiredMin": 1,
                    "explicitlyNamed": True,
                    "exactEntity": "用户明确场馆",
                }
            ],
            "lockedEntities": ["用户明确场馆"],
            "sourceFingerprint": "exact-entity-fallback-fingerprint",
        }
    )
    fallback = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": [],
                }
            ]
        },
        schema_repair_attempts=0,
    )

    for proposal in fallback.proposals:
        pool = next(item for item in proposal.intent_pools if item.goal_id == "museum")
        assert pool.raw_need == "用户明确场馆"
        assert pool.entity_binding_mode == "exact_entity"
        assert pool.exact_entity == "用户明确场馆"


def test_deterministic_fallback_preserves_family_specific_area_walk_demand():
    ledger = ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "北京",
            "dayCount": 2,
            "hardGoals": [
                {"goalId": "campus", "intentType": "campus_visit", "requiredMin": 1},
            ],
            "sourceFingerprint": "family-demand-test-fingerprint",
        }
    )
    fallback = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": day_number,
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": [],
                }
                for day_number in (1, 2)
            ]
        },
        schema_repair_attempts=0,
    )

    raw_needs_by_family = {
        pool.optional_experience_family: pool.raw_need
        for proposal in fallback.proposals
        for pool in proposal.intent_pools
        if pool.intent_type == "area_walk" and pool.optional_experience_family
    }

    assert "历史街区" in raw_needs_by_family["heritage_walk"]
    assert "社区" in raw_needs_by_family["local_life"]
    assert "市场" in raw_needs_by_family["market_walk"]
    assert "艺术" in raw_needs_by_family["art_walk"]
    assert all(raw_need != "北京街区体验" for raw_need in raw_needs_by_family.values())


def test_deterministic_fallback_spreads_soft_direction_slots_across_empty_days():
    ledger = ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "测试城市",
            "dayCount": 3,
            "hardGoals": [
                {"goalId": "museum", "intentType": "museum", "requiredMin": 1},
            ],
            "sourceFingerprint": "three-day-fallback-density-fingerprint",
        }
    )
    fallback = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": day_number,
                    "requiredGoalIds": ["museum"] if day_number == 1 else [],
                    "requiredGoalCounts": {"museum": 1} if day_number == 1 else {},
                    "optionalGoalIds": [],
                }
                for day_number in (1, 2, 3)
            ]
        },
        schema_repair_attempts=0,
    )

    assert all(
        all(
            role.target_route_anchors
            == sum(1 for slot in proposal.day_slots if slot.day_number == role.day_number and slot.route_anchor)
            >= 1
            for role in proposal.brief.day_roles
        )
        for proposal in fallback.proposals
    )


def test_deterministic_fallback_slots_fit_their_hard_windows_with_transfer_buffers():
    ledger = ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "北京",
            "dayCount": 1,
            "hardGoals": [
                {"goalId": "museum", "intentType": "museum", "requiredMin": 1},
            ],
            "sourceFingerprint": "fallback-slot-window-test-fingerprint",
        }
    )
    fallback = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 3,
                }
            ]
        },
        schema_repair_attempts=0,
    )
    culture = next(proposal for proposal in fallback.proposals if proposal.brief.primary_axis == "culture_deep_dive")

    occupied: list[tuple[int, int]] = []
    timings = []
    for slot in culture.day_slots:
        timing = ItineraryScheduleService.compiled_materialization_timing(
            time_window=slot.time_window,
            start_time=slot.start_time,
            duration=slot.duration_minutes,
            intent_type=slot.kind,
            source="creative_day_slot",
        )
        assert timing is not None
        timings.append((int(timing["startMinutes"]), slot.slot_id, timing))

    for preferred_start, _slot_id, timing in sorted(timings):
        start = preferred_start
        duration = int(timing["durationMinutes"])
        while True:
            conflict_end = max(
                (
                    occupied_end
                    for occupied_start, occupied_end in occupied
                    if start < occupied_end and start + duration > occupied_start
                ),
                default=None,
            )
            if conflict_end is None:
                break
            start = conflict_end + 15
        constraints = timing["scheduleConstraints"]
        latest_start = ItineraryScheduleService._parse_time(constraints.get("latestStart"))
        window_end = ItineraryScheduleService._parse_time(constraints.get("windowEnd"))
        assert latest_start is None or start <= latest_start
        assert window_end is None or start + duration <= window_end
        occupied.append((start, start + duration))

    optional_families = {
        slot.optional_experience_family for slot in culture.day_slots if slot.optional_experience_family
    }
    assert optional_families
    assert culture.brief.day_roles[0].target_route_anchors == 1 + len(optional_families)


def _three_anchor_two_day_ledger() -> ConstraintLedger:
    return ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "测试城市",
            "dayCount": 2,
            "hardGoals": [
                {
                    "goalId": goal_id,
                    "intentType": intent_type,
                    "requiredMin": 2,
                    "preferredCount": 2,
                    "maxCount": 2,
                }
                for goal_id, intent_type in (
                    ("campus", "campus_visit"),
                    ("museum", "museum"),
                    ("landmark", "landmark"),
                )
            ],
            "sourceFingerprint": "fallback-supply-state-three-anchor-test",
        }
    )


def _three_anchor_directive() -> dict:
    return {
        "optionalExperienceBudget": 2,
        "dayStrategies": [
            {
                "dayNumber": day,
                "requiredGoalIds": ["campus", "museum", "landmark"],
                "requiredGoalCounts": {"campus": 1, "museum": 1, "landmark": 1},
                "optionalGoalIds": [],
                "maxRouteAnchors": 4,
            }
            for day in (1, 2)
        ],
    }


@pytest.mark.parametrize(
    ("supply_state", "family_count", "expected_optional", "expected_targets"),
    [
        ("pending_search", 0, 2, {1: 4, 2: 4}),
        ("observed_complete", 0, 0, {1: 3, 2: 3}),
        ("observed_complete", 2, 2, {1: 4, 2: 4}),
    ],
)
def test_deterministic_fallback_distinguishes_pending_search_from_observed_supply(
    supply_state,
    family_count,
    expected_optional,
    expected_targets,
):
    """A search obligation is not a claim that grounded supply already exists."""
    direction = {
        "primaryAxis": "culture_deep_dive",
        "title": "历史街区方向",
        "themeFamilies": ["heritage_walk"],
        "candidateSupply": {
            "supplyState": supply_state,
            "familyCounts": {"heritage_walk": family_count},
            "scoreEligibleCount": family_count,
        },
        "generationSource": "seed_composition" if supply_state == "pending_search" else "admitted_candidate_inventory",
        "feasibilityEvidence": {"requiresBoundedSearch": supply_state == "pending_search"},
    }

    fallback = CreativePortfolioProviderService().deterministic_fallback(
        ledger=_three_anchor_two_day_ledger(),
        directive=_three_anchor_directive(),
        schema_repair_attempts=0,
        direction_candidates=[direction],
    )
    proposal = fallback.proposals[0]
    optional_slots = [
        slot for slot in proposal.day_slots if slot.optional_experience_family == "heritage_walk"
    ]
    targets = {role.day_number: role.target_route_anchors for role in proposal.brief.day_roles}

    assert len(optional_slots) == expected_optional
    assert targets == expected_targets
    assert proposal.brief.candidate_supply["familyCounts"]["heritage_walk"] == family_count
    if supply_state == "pending_search":
        assert family_count == 0
        assert optional_slots
        assert all(slot.route_anchor for slot in optional_slots)


def _payload(**changes):
    def proposal(brief_id, title, axis):
        return {
            "brief": {
                "briefId": brief_id,
                "title": title,
                "primaryAxis": axis,
                "dayRoles": [
                    {
                        "dayNumber": 1,
                        "role": "完整一日",
                        "targetRouteAnchors": 2,
                        "densityEvidence": [
                            "pace=standard",
                            "availableWindow=full_day",
                            "requiredGoalCount=2",
                            "explicitSoftGoalCount=0",
                            "briefOptionalCount=0",
                            "transport=unspecified",
                        ],
                    }
                ],
                "requiredGoalIds": ["campus", "museum"],
            },
            "daySlots": [
                {
                    "slotId": f"{brief_id}-campus",
                    "dayNumber": 1,
                    "timeWindow": "morning",
                    "durationMinutes": 120,
                    "kind": "visit",
                    "rawNeed": "985大学",
                    "routeAnchor": True,
                    "requiredGoalId": "campus",
                },
                {
                    "slotId": f"{brief_id}-museum",
                    "dayNumber": 1,
                    "timeWindow": "afternoon",
                    "durationMinutes": 120,
                    "kind": "visit",
                    "rawNeed": "美术馆",
                    "routeAnchor": True,
                    "requiredGoalId": "museum",
                },
            ],
            "intentPools": [
                {
                    "poolId": f"{brief_id}-campus",
                    "briefId": brief_id,
                    "city": "北京",
                    "targetCount": 1,
                    "intentType": "campus_visit",
                    "rawNeed": "985大学",
                    "requirementLevel": "required",
                    "goalId": "campus",
                    "assignToSlots": [f"{brief_id}-campus"],
                },
                {
                    "poolId": f"{brief_id}-museum",
                    "briefId": brief_id,
                    "city": "北京",
                    "targetCount": 1,
                    "intentType": "museum",
                    "rawNeed": "美术馆",
                    "requirementLevel": "required",
                    "goalId": "museum",
                    "assignToSlots": [f"{brief_id}-museum"],
                },
            ],
        }

    value = {
        "schemaVersion": "initial-creative-portfolio-v1",
        "proposals": [proposal("a", "文化", "culture_deep_dive"), proposal("b", "本地", "local_immersion")],
    }
    value.update(changes)
    return json.dumps(value)


def test_provider_is_called_once_when_schema_is_valid():
    calls = []
    output, repairs = CreativePortfolioProviderService().generate(
        ledger=_ledger(), invoke=lambda: calls.append(1) or _payload()
    )
    assert len(calls) == 1
    assert repairs == 0
    assert len(output.proposals) == 2


def test_provider_rejects_day_target_that_does_not_match_route_anchor_slots():
    payload = json.loads(_payload())
    payload["proposals"][0]["brief"]["dayRoles"][0]["targetRouteAnchors"] = 1
    with pytest.raises(ValueError, match="day_anchor_target_slot_mismatch"):
        CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(payload))


def test_provider_rejects_day_target_above_existing_controller_limit():
    payload = json.loads(_payload())
    proposal = payload["proposals"][0]
    proposal["brief"]["dayRoles"][0]["targetRouteAnchors"] = 3
    proposal["brief"]["dayRoles"][0]["densityEvidence"][-2] = "briefOptionalCount=1"
    proposal["brief"]["optionalExperiences"] = [{"family": "walk", "description": "街区"}]
    proposal["daySlots"].append(
        {
            "slotId": "a-walk",
            "dayNumber": 1,
            "timeWindow": "evening",
            "durationMinutes": 60,
            "kind": "experience",
            "rawNeed": "街区",
            "routeAnchor": True,
            "optionalExperienceFamily": "walk",
        }
    )
    proposal["intentPools"].append(
        {
            "poolId": "a-walk",
            "briefId": "a",
            "city": "北京",
            "targetCount": 1,
            "intentType": "area_walk",
            "rawNeed": "街区",
            "requirementLevel": "optional",
            "goalId": None,
            "assignToSlots": ["a-walk"],
            "optionalExperienceFamily": "walk",
        }
    )
    with pytest.raises(ValueError, match="day_anchor_target_exceeds_limit"):
        CreativePortfolioProviderService().generate(
            ledger=_ledger(), invoke=lambda: json.dumps(payload), day_anchor_limits={1: 2}
        )


def test_same_portfolio_allows_briefs_to_plan_different_daily_anchor_targets():
    payload = json.loads(_payload())
    proposal = payload["proposals"][1]
    proposal["brief"]["dayRoles"][0]["targetRouteAnchors"] = 3
    proposal["brief"]["dayRoles"][0]["densityEvidence"][-2] = "briefOptionalCount=1"
    proposal["brief"]["optionalExperiences"] = [{"family": "local_life", "description": "街区"}]
    proposal["daySlots"].append(
        {
            "slotId": "b-walk",
            "dayNumber": 1,
            "timeWindow": "evening",
            "durationMinutes": 60,
            "kind": "experience",
            "rawNeed": "历史街区",
            "routeAnchor": True,
            "optionalExperienceFamily": "local_life",
        }
    )
    proposal["intentPools"].append(
        {
            "poolId": "b-walk",
            "briefId": "b",
            "city": "北京",
            "targetCount": 1,
            "intentType": "area_walk",
            "rawNeed": "历史街区",
            "requirementLevel": "optional",
            "assignToSlots": ["b-walk"],
            "optionalExperienceFamily": "local_life",
        }
    )

    generated, _ = CreativePortfolioProviderService().generate(
        ledger=_ledger(), invoke=lambda: json.dumps(payload), day_anchor_limits={1: 4}
    )

    assert [proposal.brief.day_roles[0].target_route_anchors for proposal in generated.proposals] == [2, 3]


def test_invalid_schema_allows_exactly_one_repair():
    calls = []
    output, repairs = CreativePortfolioProviderService().generate(
        ledger=_ledger(), invoke=lambda: "{}", repair=lambda reason: calls.append(reason) or _payload()
    )
    assert repairs == 1
    assert len(calls) == 1
    assert calls[0].startswith("creative_portfolio_schema_invalid:")
    assert "proposals" in calls[0]
    assert output.schema_version == "initial-creative-portfolio-v1"


def test_duplicate_brief_after_one_repair_uses_distinct_deterministic_fallback():
    calls = []

    class DuplicateBriefProvider:
        def generate_initial_portfolio(self, context, *, repair_feedback=""):
            calls.append(repair_feedback)
            payload = json.loads(_payload())
            for proposal in payload["proposals"]:
                proposal["brief"]["briefId"] = "current_brief"
                for pool in proposal["intentPools"]:
                    pool["briefId"] = "current_brief"
            return json.dumps(payload, ensure_ascii=False)

    service = AgentService.__new__(AgentService)
    service.provider = DuplicateBriefProvider()
    service._pipeline_event_perf = None
    events = []
    context = {
        **_valid_controller_portfolio_context(),
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01"],
            "dayCount": 1,
        },
        "requestIntentContract": {
            "requiredIntents": [
                {
                    "goalId": "campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                    "requirementLevel": "required",
                },
                {
                    "goalId": "museum",
                    "intentType": "museum",
                    "requiredMin": 1,
                    "requirementLevel": "required",
                },
            ]
        },
        "planningDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["campus", "museum"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校与博物馆",
                    "requiredGoalIds": ["campus", "museum"],
                    "requiredGoalCounts": {"campus": 1, "museum": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                }
            ],
            "optionalExperienceBudget": 0,
        },
    }

    _ledger_value, generated = service._generate_initial_creative_portfolio(context, events)

    brief_ids = [proposal.brief.brief_id for proposal in generated.proposals]
    preview = events[-1]["metadata"]["resultPreview"]
    assert len(calls) == 2
    assert calls[0] == ""
    assert "creative_portfolio_duplicate_brief_id" in calls[1]
    assert preview["providerCallCount"] == 2
    assert preview["schemaRepairCount"] == 1
    assert preview["deterministicFallbackUsed"] is True
    assert preview["providerFailureCode"] == "CREATIVE_PORTFOLIO_SCHEMA_INVALID"
    assert len(brief_ids) == len(set(brief_ids)) == 4
    assert "current_brief" not in brief_ids


def test_agent_service_injects_only_compiled_hard_goal_ids_into_portfolio_context():
    captured = {}

    class Provider:
        def generate_initial_portfolio(self, context, *, repair_feedback=""):
            captured.update(context)
            payload = json.loads(_payload())
            return json.dumps(payload, ensure_ascii=False)

    service = AgentService.__new__(AgentService)
    service.provider = Provider()
    service._pipeline_event_perf = None
    ledger, generated = service._generate_initial_creative_portfolio(
        {
            "selectedCity": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit", "requirementLevel": "required"},
                    {"goalId": "museum", "intentType": "museum", "requirementLevel": "required"},
                    {"goalId": "meal", "intentType": "local_food", "requirementLevel": "soft_experience"},
                ]
            },
        },
        [],
    )

    assert captured["creativePortfolioHardGoalIds"] == ["campus", "museum"]
    assert captured["creativePortfolioSoftGoals"] == [{"goalId": "meal", "intentType": "local_food"}]
    assert captured["goalOccurrencePlan"]["occurrences"]
    assert [goal.goal_id for goal in ledger.soft_goals] == ["meal"]
    assert all(proposal.brief.required_goal_ids == ["campus", "museum"] for proposal in generated.proposals)


def test_agent_service_default_occurrence_rejects_ambiguous_multi_day_placement():
    service = AgentService.__new__(AgentService)
    service.provider = object()
    service._pipeline_event_perf = None

    with pytest.raises(ValueError, match="goal_occurrence_default_ambiguous"):
        service._generate_initial_creative_portfolio(
            {
                "selectedCity": "测试城市",
                "resolvedTripDates": {"dayCount": 2},
                "requestIntentContract": {
                    "requiredIntents": [
                        {"goalId": "museum", "intentType": "museum", "requiredMin": 1},
                    ]
                },
            },
            [],
        )


def test_agent_service_preserves_pending_supply_state_when_provider_direction_is_unmatched():
    class Provider:
        def generate_initial_portfolio(self, _context, *, repair_feedback=""):
            payload = json.loads(_payload())
            for proposal, axis in zip(payload["proposals"], ("family_light", "photo_night")):
                proposal["brief"]["primaryAxis"] = axis
                proposal["brief"]["candidateSupply"] = {
                    "supplyState": "observed_complete",
                    "familyCounts": {"heritage_walk": 99},
                    "candidateIds": ["provider-asserted-id"],
                }
            return json.dumps(payload, ensure_ascii=False)

    service = AgentService.__new__(AgentService)
    service.provider = Provider()
    service._pipeline_event_perf = None

    _ledger_value, generated = service._generate_initial_creative_portfolio(
        {
            "selectedCity": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit", "requirementLevel": "required"},
                    {"goalId": "museum", "intentType": "museum", "requirementLevel": "required"},
                ]
            },
        },
        [],
    )

    assert all(proposal.brief.candidate_supply == {"supplyState": "pending_search"} for proposal in generated.proposals)


def test_grounded_identity_in_provider_output_fails_closed():
    bad = json.loads(_payload())
    bad["proposals"][0]["intentPools"] = [{"amapId": "untrusted"}]
    with pytest.raises(ValueError, match="contains_grounded_fact"):
        CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(bad))


def test_nested_grounded_identity_in_provider_output_also_fails_closed():
    bad = json.loads(_payload())
    bad["proposals"][0]["daySlots"] = [{"theme": {"coordinates": [116.3, 39.9]}}]
    with pytest.raises(ValueError, match="contains_grounded_fact"):
        CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(bad))


def test_shared_query_contract_retains_distinct_dict_pools_from_each_brief():
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {"briefId": "a", "title": "A", "primaryAxis": "classic", "requiredGoalIds": []},
                    "intentPools": [
                        {
                            "poolId": "museum",
                            "city": "北京",
                            "targetCount": 1,
                            "intentType": "museum",
                            "rawNeed": "美术馆",
                            "candidateHints": ["艺术馆"],
                        }
                    ],
                },
                {
                    "brief": {"briefId": "b", "title": "B", "primaryAxis": "local_immersion", "requiredGoalIds": []},
                    "intentPools": [
                        {
                            "poolId": "campus",
                            "city": "北京",
                            "targetCount": 1,
                            "intentType": "campus_visit",
                            "rawNeed": "985大学",
                            "candidateHints": ["高校"],
                        }
                    ],
                },
            ],
        }
    )
    initial = AgentService._initial_plan_from_creative_portfolio(generated)
    assert {(item.intent_type, item.raw_need) for item in initial.intent_pools} == {
        ("museum", "美术馆"),
        ("campus_visit", "985大学"),
    }


def test_shared_query_contract_retains_slots_referenced_by_later_brief_pools():
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "culture",
                        "title": "文化",
                        "primaryAxis": "classic",
                        "requiredGoalIds": [],
                    },
                    "daySlots": [
                        {
                            "slotId": "museum-slot",
                            "dayNumber": 2,
                            "timeWindow": "morning",
                            "durationMinutes": 120,
                            "kind": "visit",
                            "rawNeed": "博物馆",
                            "routeAnchor": True,
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "museum-pool",
                            "briefId": "culture",
                            "city": "北京",
                            "targetCount": 1,
                            "intentType": "museum",
                            "rawNeed": "博物馆",
                            "assignToSlots": ["museum-slot"],
                        }
                    ],
                },
                {
                    "brief": {
                        "briefId": "food",
                        "title": "京味美食",
                        "primaryAxis": "food_led",
                        "requiredGoalIds": [],
                    },
                    "daySlots": [
                        {
                            "slotId": "meal-slot",
                            "dayNumber": 1,
                            "timeWindow": "noon",
                            "durationMinutes": 90,
                            "kind": "meal",
                            "rawNeed": "北京特色美食",
                            "routeAnchor": True,
                            "optionalExperienceFamily": "local_food",
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "meal-pool",
                            "briefId": "food",
                            "city": "北京",
                            "targetCount": 1,
                            "intentType": "meal",
                            "rawNeed": "北京特色美食",
                            "requirementLevel": "optional",
                            "softGoalId": "goal_meal",
                            "optionalExperienceFamily": "local_food",
                            "assignToSlots": ["meal-slot"],
                        }
                    ],
                },
            ],
        }
    )

    initial = AgentService._initial_plan_from_creative_portfolio(generated)

    slots = {slot.slot_id for slot in initial.day_slots}
    assert slots == {"museum-slot", "meal-slot"}
    assert all(set(pool.assign_to_slots) <= slots for pool in initial.intent_pools)
    meal_pool = next(pool for pool in initial.intent_pools if pool.pool_id == "meal-pool")
    assert meal_pool.brief_id == "food"
    assert meal_pool.requirement_level == "optional"
    assert meal_pool.soft_goal_id == "goal_meal"
    assert meal_pool.optional_experience_family == "local_food"


def test_portfolio_day_slots_without_start_time_compile_to_non_overlapping_staged_slots():
    payload = json.loads(_payload())
    payload["proposals"][0]["daySlots"][1]["timeWindow"] = "morning"
    generated, _ = CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(payload))

    initial = AgentService._initial_plan_from_creative_portfolio(generated)

    assert [slot.start_time for slot in initial.day_slots] == ["09:00", "11:00", "09:00", "14:00"]
    assert [slot.time_window for slot in initial.day_slots] == [
        "09:00-11:00",
        "11:00-13:00",
        "09:00-11:00",
        "14:00-16:00",
    ]


def test_portfolio_day_slots_compile_in_clock_order_even_when_provider_order_is_reversed():
    payload = json.loads(_payload())
    slots = payload["proposals"][0]["daySlots"]
    slots[0]["timeWindow"] = "09:00-11:00"
    slots[1]["timeWindow"] = "14:00-16:00"
    payload["proposals"][0]["daySlots"] = list(reversed(slots))
    generated, _ = CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(payload))

    initial = AgentService._initial_plan_from_creative_portfolio(generated)

    assert [slot.slot_id for slot in initial.day_slots] == ["a-campus", "a-museum", "b-campus", "b-museum"]
    assert [slot.start_time for slot in initial.day_slots] == ["09:00", "14:00", "09:00", "14:00"]
    assert [slot.time_window for slot in initial.day_slots] == [
        "09:00-11:00",
        "14:00-16:00",
        "09:00-11:00",
        "14:00-16:00",
    ]


def _valid_controller_portfolio_context():
    return {
        "selectedCity": "北京",
        "effectiveUserMessage": "北京985高校、博物馆和当地特色美食两日游",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": {
            "requiredIntents": [
                {
                    "goalId": "campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                    "requirementLevel": "required",
                },
                {
                    "goalId": "museum",
                    "intentType": "museum",
                    "requiredMin": 1,
                    "requirementLevel": "required",
                },
                {
                    "goalId": "meal",
                    "intentType": "meal",
                    "requiredMin": 1,
                    "requirementLevel": "soft_experience",
                },
            ]
        },
        "planningDirective": {
            "type": "draft_itinerary",
            "goalPriority": ["campus", "museum", "meal"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校与京味",
                    "requiredGoalIds": ["campus"],
                    "requiredGoalCounts": {"campus": 1},
                    "optionalGoalIds": ["meal"],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "博物馆",
                    "requiredGoalIds": ["museum"],
                    "requiredGoalCounts": {"museum": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 1,
        },
        "agentDecisionState": {
            "source": "controller",
            "accepted": True,
            "primaryAction": "draft_itinerary",
        },
    }


@pytest.mark.parametrize("failure", ["timeout", "missing_required_pools"])
def test_valid_controller_draft_uses_deterministic_portfolio_when_creative_provider_is_unusable(failure):
    class UnusableProvider:
        def generate_initial_portfolio(self, context, *, repair_feedback=""):
            if failure == "timeout":
                raise TimeoutError("portfolio provider timed out")
            proposals = [
                {
                    "brief": {
                        "briefId": f"broken_{index}",
                        "title": f"方案 {index}",
                        "primaryAxis": axis,
                        "requiredGoalIds": context["creativePortfolioHardGoalIds"],
                    }
                }
                for index, axis in enumerate(["classic", "local_immersion", "food_led", "nature_relaxed"], start=1)
            ]
            return json.dumps({"schemaVersion": "initial-creative-portfolio-v1", "proposals": proposals})

    service = AgentService.__new__(AgentService)
    service.provider = UnusableProvider()
    service._pipeline_event_perf = None
    events = []

    ledger, generated = service._generate_initial_creative_portfolio(_valid_controller_portfolio_context(), events)

    required = {goal.goal_id for goal in ledger.hard_goals}
    assert len(generated.proposals) == 4
    assert generated.parser_metadata["deterministicFallbackUsed"] == 1
    assert all(
        {pool.goal_id for pool in proposal.intent_pools if pool.requirement_level == "required"} == required
        for proposal in generated.proposals
    )
    assert all(
        all(
            role.target_route_anchors
            == sum(1 for slot in proposal.day_slots if slot.day_number == role.day_number and slot.route_anchor)
            and role.density_evidence
            for role in proposal.brief.day_roles
        )
        for proposal in generated.proposals
    )
    assert all(
        {pool.soft_goal_id for pool in proposal.intent_pools if pool.soft_goal_id} == {"meal"}
        for proposal in generated.proposals
    )
    assert all(proposal.brief.optional_experiences for proposal in generated.proposals)
    optional_lineages = {
        tuple(item.family for item in proposal.brief.optional_experiences) for proposal in generated.proposals
    }
    target_signatures = {
        tuple(role.target_route_anchors for role in proposal.brief.day_roles) for proposal in generated.proposals
    }
    assert len(optional_lineages) >= 2
    assert target_signatures == {(2, 2)}
    assert events[-1]["status"] == "fallback"


def test_valid_controller_draft_uses_deterministic_portfolio_when_provider_lacks_portfolio_capability():
    """A Controller-only provider is compatible with the Portfolio rollout gate."""

    class ControllerOnlyProvider:
        pass

    service = AgentService.__new__(AgentService)
    service.provider = ControllerOnlyProvider()
    service._pipeline_event_perf = None
    events = []

    _ledger, generated = service._generate_initial_creative_portfolio(_valid_controller_portfolio_context(), events)

    assert len(generated.proposals) == 4
    assert generated.parser_metadata["deterministicFallbackUsed"] == 1
    assert events[-1]["status"] == "fallback"
    assert events[-1]["metadata"]["resultPreview"]["providerCallCount"] == 0
    assert events[-1]["metadata"]["resultPreview"]["providerFailureCode"] == "CREATIVE_PORTFOLIO_PROVIDER_MISSING"


def test_configured_target_count_limits_downstream_portfolio_directions(monkeypatch):
    class ControllerOnlyProvider:
        pass

    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT", "2")
    get_settings.cache_clear()
    try:
        service = AgentService.__new__(AgentService)
        service.provider = ControllerOnlyProvider()
        service._pipeline_event_perf = None
        events = []
        _ledger, generated = service._generate_initial_creative_portfolio(_valid_controller_portfolio_context(), events)
        assert len(generated.proposals) == 2
        assert events[-1]["metadata"]["resultPreview"]["briefCount"] == 2
    finally:
        monkeypatch.delenv("AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT", raising=False)
        get_settings.cache_clear()


def test_deterministic_portfolio_supports_preferred_hard_goal_as_explicit_soft_occurrence():
    context = json.loads(json.dumps(_valid_controller_portfolio_context()))
    campus = context["requestIntentContract"]["requiredIntents"][0]
    campus.update(
        {
            "preferredCount": 2,
            "maxCount": 2,
            "distributionPolicy": "spread_across_distinct_days",
            "allowedDayNumbers": [1, 2],
        }
    )
    context["planningDirective"]["dayStrategies"][1]["optionalGoalIds"] = ["campus"]
    context["planningDirective"]["optionalExperienceBudget"] = 2

    service = AgentService.__new__(AgentService)
    service.provider = object()
    service._pipeline_event_perf = None
    _ledger, generated = service._generate_initial_creative_portfolio(context, [])

    assert all(
        any(
            pool.soft_goal_id == "campus"
            and pool.intent_type == "campus_visit"
            and pool.requirement_level == "optional"
            for pool in proposal.intent_pools
        )
        for proposal in generated.proposals
    )


def test_required_pool_rejects_non_route_day_slot_before_production_grounding():
    payload = json.loads(_payload())
    payload["proposals"][0]["daySlots"][0]["routeAnchor"] = False

    with pytest.raises(ValueError, match="required_pool_route_anchor_invalid"):
        CreativePortfolioProviderService().generate(
            ledger=_ledger(),
            invoke=lambda: json.dumps(payload),
        )

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from src.services.creative_planning_models import CreativeBrief, canonical_fingerprint
from src.services.plan_proposal_verifier import PlanProposalVerifier
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.route_insertion_scorer import RouteInsertionScorer

import pytest


def _ledger():
    return ConstraintLedgerCompiler().compile(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit"},
                    {"goalId": "museum", "intentType": "museum"},
                ]
            }
        }
    )


def _segment(goal, start, end):
    return {
        "startTime": start,
        "endTime": end,
        "poi": {
            "amapId": goal,
            "longitude": 116.3,
            "latitude": 39.9,
            "providerType": "AMAP",
            "source": "amap-place-search",
        },
        "semanticMetadata": {"goalId": goal, "required": True, "routeAnchor": True, "groundingStatus": "selected"},
    }


def test_verifier_accepts_grounded_complete_non_overlapping_snapshot():
    result = PlanProposalVerifier().verify(
        {"days": [{"segments": [_segment("campus", "09:00", "10:00"), _segment("museum", "11:00", "12:00")]}]},
        _ledger(),
    )
    assert result["passed"] is True


def test_verifier_rejects_explicit_noon_occurrence_after_explicit_evening_occurrence():
    campus = _segment("campus", "09:00", "10:00")
    museum = _segment("museum", "10:30", "11:30")
    park = _segment("park", "18:00", "19:30")
    meal = _segment("meal", "20:05", "21:20")
    park["semanticMetadata"].update(
        {
            "intentType": "park",
            "schedulePreference": {
                "dayPart": "evening",
                "userExplicit": True,
                "sequence": 2,
            },
        }
    )
    meal["semanticMetadata"].update(
        {
            "intentType": "meal",
            "schedulePreference": {
                "dayPart": "noon",
                "userExplicit": True,
                "sequence": 3,
            },
        }
    )

    result = PlanProposalVerifier().verify(
        {"days": [{"dayNumber": 1, "segments": [campus, museum, park, meal]}]},
        _ledger(),
    )

    assert result["passed"] is False
    assert "schedule_semantic_order" in result["hardFailures"]
    assert any(
        item["code"] == "schedule.semantic_day_part_order"
        and item["previousDayPart"] == "evening"
        and item["dayPart"] == "noon"
        for item in result["temporalFailures"]
    )


def test_exact_route_pairs_override_stale_aggregate_route_missing_flag():
    left = _segment("campus", "09:00", "10:00")
    right = _segment("museum", "11:00", "12:00")
    left["id"] = "seg_campus"
    right["id"] = "seg_museum"
    left["poi"]["amapId"] = "B00000001"
    right["poi"]["amapId"] = "B00000002"
    snapshot = {
        "days": [{"dayNumber": 1, "segments": [left, right]}],
        "portfolioRouteVerificationRequired": True,
        "routeOptions": [
            {
                "id": "route_1",
                "fromSegmentId": "seg_campus",
                "toSegmentId": "seg_museum",
                "fromPoiId": "B00000001",
                "toPoiId": "B00000002",
                "provider": "amap-webservice",
                "source": "amap-webservice",
                "mode": "transit",
                "transportMode": "transit",
                "label": "公交地铁",
                "isSelected": True,
                "sortOrder": 1,
                "status": "verified",
                "distanceMeters": 1000,
                "durationSeconds": 600,
                "durationMinutes": 10,
                "distanceKm": 1.0,
                "costAmount": 0,
                "costCurrency": "CNY",
                "costEstimate": 0,
                "crowdingRisk": "",
                "polyline": [[116.3, 39.9], [116.31, 39.91]],
                "steps": [],
                "providerPayload": {},
                "queriedAt": "2026-08-01T00:00:00+00:00",
            }
        ],
        "portfolioRouteQuality": {"routeQualityIssues": [{"code": "route_evidence_missing"}]},
    }
    result = PlanProposalVerifier().verify(snapshot, _ledger())
    assert result["routeCoverageFailures"] == []
    assert result["routeQualityFailures"] == []
    assert "stale_route_evidence_missing_ignored_after_exact_pair_coverage" in result["routeQualityWarnings"]
    assert result["passed"] is True


def test_extra_verified_non_adjacent_route_is_rejected():
    campus = _segment("campus", "09:00", "10:00")
    museum = _segment("museum", "11:00", "12:00")
    optional = _segment("", "13:00", "14:00")
    for segment, segment_id, amap_id in (
        (campus, "seg_campus", "B00000001"),
        (museum, "seg_museum", "B00000002"),
        (optional, "seg_optional", "B00000003"),
    ):
        segment["id"] = segment_id
        segment["poi"]["amapId"] = amap_id
    optional["semanticMetadata"].update({"goalId": "", "required": False})

    def route(route_id, left, right, left_amap, right_amap):
        return {
            "id": route_id,
            "fromSegmentId": left,
            "toSegmentId": right,
            "fromPoiId": left_amap,
            "toPoiId": right_amap,
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "transit",
            "transportMode": "transit",
            "label": "公交地铁",
            "isSelected": True,
            "sortOrder": 1,
            "status": "verified",
            "distanceMeters": 1000,
            "durationSeconds": 600,
            "durationMinutes": 10,
            "distanceKm": 1.0,
            "costAmount": 0,
            "costCurrency": "CNY",
            "costEstimate": 0,
            "crowdingRisk": "",
            "polyline": [[116.3, 39.9], [116.31, 39.91]],
            "steps": [],
            "providerPayload": {},
            "queriedAt": "2026-08-01T00:00:00+00:00",
        }

    snapshot = {
        "days": [{"dayNumber": 1, "segments": [campus, museum, optional]}],
        "portfolioRouteVerificationRequired": True,
        "routeOptions": [
            route("route_campus_museum", "seg_campus", "seg_museum", "B00000001", "B00000002"),
            route("route_museum_optional", "seg_museum", "seg_optional", "B00000002", "B00000003"),
            route("route_campus_optional", "seg_campus", "seg_optional", "B00000001", "B00000003"),
        ],
    }

    result = PlanProposalVerifier().verify(snapshot, _ledger())

    assert "route_coverage_unexpected:seg_campus:seg_optional" in result["routeCoverageFailures"]
    assert result["passed"] is False


def _provider_matrix_proof_snapshot():
    now = datetime.now(timezone.utc).isoformat()
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"requestFingerprint": "route-proof-test"},
        detour_tolerance={
            "maxGeneralizedCostDelta": 50.0,
            "maxDetourRatio": 1.0,
        },
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 0.0,
            "transferPenaltyMinutes": 0.0,
            "waitTimeMultiplier": 0.0,
            "riskPenaltyMultiplier": 0.0,
        },
    )
    assert contract is not None

    def segment(segment_id, amap_id, start, end, requires=False):
        return {
            "id": segment_id,
            "kind": "visit",
            "startTime": start,
            "endTime": end,
            "poi": {
                "id": amap_id,
                "amapId": amap_id,
                "longitude": 116.3,
                "latitude": 39.9,
                "providerType": "AMAP",
                "source": "amap-place-search",
            },
            "semanticMetadata": {
                "required": False,
                "routeAnchor": True,
                "groundingStatus": "selected",
                "routeContract": {
                    "requiresProviderInsertionDecision": requires,
                    **(
                        {
                            "specFingerprint": "spec-route-proof-test",
                            "experienceSpecPolicy": {"timeWindow": {"start": "10:30", "end": "12:30"}},
                        }
                        if requires
                        else {}
                    ),
                },
            },
        }

    previous = segment("seg_previous", "B00000001", "09:00", "10:00")
    candidate = segment("seg_candidate", "B00000002", "11:00", "12:00", requires=True)
    following = segment("seg_following", "B00000003", "13:00", "14:00")

    def leg(name, left, right, left_amap, right_amap, duration, distance):
        return {
            "fromSegmentId": left,
            "toSegmentId": right,
            "fromAmapId": left_amap,
            "toAmapId": right_amap,
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "transit",
            "distanceMeters": distance,
            "durationSeconds": duration,
            "queriedAt": now,
            "walkingDistanceMeters": 0.0,
            "transferCount": 0.0,
            "waitSeconds": 0.0,
            "riskPenaltyMinutes": 0.0,
            "name": name,
        }

    matrix = {
        "previousToCandidate": leg(
            "incoming",
            "seg_previous",
            "seg_candidate",
            "B00000001",
            "B00000002",
            600,
            1000,
        ),
        "candidateToNext": leg(
            "outgoing",
            "seg_candidate",
            "seg_following",
            "B00000002",
            "B00000003",
            600,
            1000,
        ),
        "previousToNext": leg(
            "baseline",
            "seg_previous",
            "seg_following",
            "B00000001",
            "B00000003",
            900,
            1500,
        ),
    }
    proof = {
        "networkVerified": True,
        "contractFingerprint": contract["fingerprint"],
        "routeDecisionContract": deepcopy(contract),
        "specFingerprint": "spec-route-proof-test",
        "detourTolerance": deepcopy(contract["detourTolerance"]),
        "detourToleranceFingerprint": canonical_fingerprint(contract["detourTolerance"]),
        "mobilityProfile": deepcopy(contract["mobilityProfile"]),
        "mobilityProfileFingerprint": canonical_fingerprint(contract["mobilityProfile"]),
        "generalizedCostDelta": 5.0,
        "detourRatio": 0.3333,
        "detourLevel": "low",
        "timeWindowFeasible": True,
        "timeWindow": {"start": "10:30", "end": "12:30"},
        "verifiedAt": now,
        "candidateEndpoint": {
            "segmentId": "seg_candidate",
            "amapId": "B00000002",
        },
        "baselineEndpoints": {
            "previousSegmentId": "seg_previous",
            "previousAmapId": "B00000001",
            "nextSegmentId": "seg_following",
            "nextAmapId": "B00000003",
        },
        "routeMatrix": matrix,
    }
    proof["proofFingerprint"] = RouteInsertionScorer.route_proof_fingerprint(proof)
    candidate["semanticMetadata"]["routeInsertionMatrixProof"] = proof
    return {
        "routeDecisionContract": contract,
        "days": [
            {
                "dayNumber": 1,
                "segments": [previous, candidate, following],
            }
        ],
    }


def test_verifier_recomputes_provider_route_decision_proof():
    snapshot = _provider_matrix_proof_snapshot()
    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile({"requestIntentContract": {"requiredIntents": []}}),
    )

    assert result["routeDecisionProofFailures"] == []
    assert result["routeDecisionProofEvidence"]["checkedProofCount"] == 1
    assert result["passed"] is True


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda proof: proof["routeMatrix"]["candidateToNext"].update({"fromSegmentId": "seg_wrong"}),
            "route_matrix_endpoint_mismatch",
        ),
        (
            lambda proof: proof.update({"generalizedCostDelta": 1.0}),
            "route_generalized_cost_delta_mismatch",
        ),
        (
            lambda proof: proof.update({"contractFingerprint": "0" * 64}),
            "route_contract_fingerprint_mismatch",
        ),
        (
            lambda proof: proof.update({"timeWindow": {"start": "11:00", "end": "13:00"}}),
            "route_time_window_mismatch",
        ),
        (
            lambda proof: proof["candidateEndpoint"].update({"amapId": "B00000009"}),
            "route_candidate_endpoint_mismatch",
        ),
        (
            lambda proof: proof["routeDecisionContract"].update({"fingerprint": "0" * 64}),
            "route_embedded_contract_mismatch",
        ),
        (
            lambda proof: proof.update({"verifiedAt": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}),
            "route_proof_stale_or_invalid",
        ),
    ],
)
def test_verifier_rejects_tampered_or_stale_route_decision_proof(mutation, expected):
    snapshot = _provider_matrix_proof_snapshot()
    proof = snapshot["days"][0]["segments"][1]["semanticMetadata"]["routeInsertionMatrixProof"]
    mutation(proof)

    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile({"requestIntentContract": {"requiredIntents": []}}),
    )

    assert any(item.startswith(expected) for item in result["routeDecisionProofFailures"])
    assert any(item.startswith("route_proof_fingerprint_mismatch") for item in result["routeDecisionProofFailures"])
    assert result["passed"] is False


def test_verifier_rejects_brief_when_its_declared_optional_family_is_unavailable():
    brief = CreativeBrief(
        briefId="food",
        title="北京味道",
        primaryAxis="food_led",
        requiredGoalIds=["campus", "museum"],
        optionalExperiences=[{"family": "local_food", "description": "北京特色美食"}],
    )
    result = PlanProposalVerifier().verify(
        {
            "days": [
                {
                    "segments": [
                        _segment("campus", "09:00", "10:00"),
                        _segment("museum", "11:00", "12:00"),
                    ]
                }
            ]
        },
        _ledger(),
        brief,
    )

    assert result["passed"] is False
    assert result["themeAlignmentFailures"] == ["theme_optional_family_missing"]
    assert result["themeAlignmentWarnings"] == []


def test_verifier_marks_only_structured_soft_pending_as_editable_draft():
    brief = CreativeBrief(
        briefId="local",
        title="本地生活",
        primaryAxis="local_immersion",
        requiredGoalIds=["campus", "museum"],
        optionalExperiences=[{"family": "local_life", "description": "社区生活体验"}],
    )
    result = PlanProposalVerifier().verify(
        {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [_segment("campus", "09:00", "10:00"), _segment("museum", "11:00", "12:00")],
                }
            ],
            "portfolioPendingSlots": [
                {
                    "planningSlotId": "slot_local",
                    "dayNumber": 1,
                    "requirementLevel": "soft",
                    "sourceGoalId": "goal_local",
                    "optionalExperienceFamily": "local_life",
                    "futureRouteAnchor": True,
                }
            ],
            "portfolioGoalOccurrencePlan": {
                "occurrences": [
                    {
                        "occurrenceId": "goal_local:preferred:1",
                        "sourceGoalId": "goal_local",
                        "intentType": "local_culture",
                        "dayNumber": 1,
                        "requirementLevel": "explicit_soft",
                    }
                ]
            },
        },
        _ledger(),
        brief,
    )

    assert result["passed"] is False
    assert result["draftPassed"] is True
    assert result["pendingHardSlotCount"] == 0
    assert result["pendingSoftSlotCount"] == 1
    assert result["hardFailures"] == []
    assert result["goalOccurrenceCoverage"] == {"goal_local:preferred:1": False}
    assert any(item.startswith("soft_slot_pending:") for item in result["softWarnings"])


def test_verifier_rejects_target_three_when_only_two_grounded_anchors_exist():
    result = PlanProposalVerifier().verify(
        {
            "portfolioDayAnchorTargets": {"1": 3},
            "portfolioDensityDecisionSource": "creative_portfolio_provider",
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        _segment("campus", "09:00", "10:00"),
                        _segment("museum", "11:00", "12:00"),
                    ],
                }
            ],
        },
        _ledger(),
    )
    assert result["passed"] is False
    assert result["dayAnchorShortfalls"] == ["day_1:2/3"]
    assert result["targetRouteAnchorCoveragePassed"] is False


def test_verifier_allows_agent_declared_single_anchor_day():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
    )
    result = PlanProposalVerifier().verify(
        {
            "portfolioDayAnchorTargets": {"1": 1},
            "days": [{"dayNumber": 1, "segments": [_segment("campus", "09:00", "10:00")]}],
        },
        ledger,
    )
    assert result["passed"] is True
    assert result["dayAnchorActuals"] == {"1": 1}


def test_verifier_rejects_mixed_allowed_and_out_of_brief_optional_families():
    brief = CreativeBrief(
        briefId="food",
        title="Food",
        primaryAxis="food_led",
        requiredGoalIds=["campus", "museum"],
        optionalExperiences=[{"family": "local_food", "description": "local food"}],
    )
    local_food = {
        "startTime": "13:00",
        "endTime": "14:00",
        "semanticMetadata": {
            "portfolioOptional": True,
            "optionalExperienceFamily": "local_food",
        },
    }
    unrelated_nightlife = {
        "startTime": "15:00",
        "endTime": "16:00",
        "semanticMetadata": {
            "portfolioOptional": True,
            "optionalExperienceFamily": "nightlife",
        },
    }
    result = PlanProposalVerifier().verify(
        {
            "days": [
                {
                    "segments": [
                        _segment("campus", "09:00", "10:00"),
                        _segment("museum", "11:00", "12:00"),
                        local_food,
                        unrelated_nightlife,
                    ]
                }
            ]
        },
        _ledger(),
        brief,
    )

    assert result["passed"] is False
    assert "theme_optional_family_missing" in result["hardFailures"]
    assert result["themeAlignmentWarnings"] == []


def test_verifier_rejects_missing_goal_and_overlap():
    result = PlanProposalVerifier().verify(
        {"days": [{"segments": [_segment("campus", "09:00", "10:00"), _segment("campus", "09:30", "11:00")]}]},
        _ledger(),
    )
    assert result["passed"] is False
    assert "schedule_overlap" in result["hardFailures"]
    assert "required_goal_omitted:museum" in result["hardFailures"]


def test_verifier_accepts_existing_selected_candidate_for_matching_hard_intent():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "meal", "intentType": "meal"}]}}
    )
    snapshot = {
        "days": [
            {
                "segments": [
                    {
                        "startTime": "12:00",
                        "endTime": "13:00",
                        "poi": {
                            "amapId": "meal-1",
                            "longitude": 116.3,
                            "latitude": 39.9,
                            "providerType": "餐饮服务;中餐厅",
                            "source": "amap-place-search",
                        },
                        "semanticMetadata": {
                            "intentType": "meal",
                            "routeAnchor": False,
                            "groundingStatus": "agent_selected_candidate",
                        },
                    }
                ]
            }
        ]
    }
    result = PlanProposalVerifier().verify(snapshot, ledger)
    assert result["passed"] is True
    assert result["requiredGoalCoverage"] == {"meal": True}


def test_density_targets_do_not_count_explicit_non_anchor_meal() -> None:
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "meal",
                        "kind": "meal",
                        "startTime": "12:00",
                        "endTime": "13:00",
                        "poi": {
                            "amapId": "B0JGXR51R5",
                            "latitude": 39.979626,
                            "longitude": 116.314171,
                            "source": "amap-place-search",
                        },
                        "semanticMetadata": {
                            "intentType": "meal",
                            "routeAnchor": False,
                            "routeAnchorExpected": False,
                        },
                    }
                ],
            }
        ]
    }

    targets = ProposalReadinessService.density_targets(snapshot)

    assert targets["groundedRouteAnchorTargets"] == {"1": 0}


def test_verifier_rejects_required_goal_without_grounded_identity():
    result = PlanProposalVerifier().verify(
        {
            "days": [
                {
                    "segments": [
                        {
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": {},
                            "semanticMetadata": {
                                "goalId": "campus",
                                "required": True,
                                "intentType": "campus_visit",
                                "routeAnchor": False,
                                "groundingStatus": "unresolved",
                            },
                        }
                    ]
                }
            ]
        },
        _ledger(),
    )
    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]
    assert "required_goal_omitted:campus" in result["hardFailures"]


def test_enforce_mode_rejects_noncanonical_amap_identity():
    snapshot = {"days": [{"segments": [_segment("campus", "09:00", "10:00")]}]}

    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        snapshot,
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )

    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


def test_enforce_mode_rejects_malformed_amap_parent_identity():
    segment = _segment("campus", "09:00", "10:00")
    segment["poi"].update(
        {
            "amapId": "B000A8UIN8",
            "parentPoiId": "NOT_AN_AMAP_ID",
            "city": "北京",
            "type": "科教文化服务;学校;高等院校",
            "providerType": "科教文化服务;学校;高等院校",
            "source": "amap-place-search",
            "longitude": 116.32,
            "latitude": 40.00,
        }
    )

    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        {"city": "北京", "days": [{"dayNumber": 1, "segments": [segment]}]},
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )

    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


def test_verifier_rejects_zero_coordinate_amap_identity():
    segment = _segment("campus", "09:00", "10:00")
    segment["poi"].update(
        {
            "amapId": "B000A8UIN8",
            "name": "清华大学",
            "city": "北京市",
            "type": "科教文化服务;学校;高等院校",
            "providerType": "科教文化服务;学校;高等院校",
            "source": "amap-place-search",
            "longitude": 0.0,
            "latitude": 0.0,
        }
    )
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": [segment]}],
    }

    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )

    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


def test_verifier_requires_distinct_grounded_identities_for_each_goal_cardinality():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "museum", "intentType": "museum", "requiredMin": 2}]}}
    )
    result = PlanProposalVerifier().verify(
        {"days": [{"segments": [_segment("museum", "09:00", "10:00"), _segment("museum", "11:00", "12:00")]}]},
        ledger,
    )
    assert result["passed"] is False
    assert "required_goal_count_insufficient:museum:1/2" in result["hardFailures"]


def test_verifier_normalizes_amap_identity_case_for_goal_cardinality():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "museum",
                        "intentType": "museum",
                        "requiredMin": 2,
                    }
                ]
            }
        }
    )
    first = _segment("museum", "09:00", "10:00")
    first["poi"]["amapId"] = "b000a8uin8"
    second = _segment("museum", "11:00", "12:00")
    second["poi"]["amapId"] = "B000A8UIN8"

    result = PlanProposalVerifier().verify(
        {"days": [{"segments": [first, second]}]},
        ledger,
    )

    assert result["passed"] is False
    assert "required_goal_count_insufficient:museum:1/2" in result["hardFailures"]


def test_verifier_does_not_project_one_intent_only_segment_to_two_named_goals():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "museum_a", "intentType": "museum"},
                    {"goalId": "museum_b", "intentType": "museum"},
                ]
            }
        }
    )
    segment = _segment("museum_a", "09:00", "10:00")
    segment["semanticMetadata"]["intentType"] = "museum"
    result = PlanProposalVerifier().verify({"days": [{"segments": [segment]}]}, ledger)
    assert result["passed"] is False
    assert result["requiredGoalCoverage"] == {"museum_a": True, "museum_b": False}


def test_verifier_rejects_unknown_persisted_goal_id_without_intent_fallback():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "museum", "intentType": "museum"}]}}
    )
    segment = _segment("forged_museum", "09:00", "10:00")
    segment["semanticMetadata"]["intentType"] = "museum"

    result = PlanProposalVerifier().verify({"days": [{"segments": [segment]}]}, ledger)

    assert result["passed"] is False
    assert result["requiredGoalCoverage"] == {"museum": False}
    assert "required_goal_omitted:museum" in result["hardFailures"]


def test_verifier_rejects_one_grounded_amap_identity_reused_by_different_hard_goals():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "museum_a", "intentType": "museum"},
                    {"goalId": "museum_b", "intentType": "museum"},
                ]
            }
        }
    )
    first = _segment("museum_a", "09:00", "10:00")
    first["poi"]["amapId"] = "same-museum"
    second = _segment("museum_b", "11:00", "12:00")
    second["poi"]["amapId"] = "same-museum"
    result = PlanProposalVerifier().verify({"days": [{"segments": [first, second]}]}, ledger)
    assert result["passed"] is False
    assert "required_goal_identity_reused:SAME-MUSEUM" in result["hardFailures"]
    assert result["requiredGoalCoverage"] == {"museum_a": True, "museum_b": False}


def test_verifier_enforces_model_owned_occurrences_and_capacity_balance():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
    )
    first = _segment("campus", "09:00", "10:00")
    first["semanticMetadata"]["intentType"] = "campus_visit"
    second = _segment("campus", "09:00", "10:00")
    second["semanticMetadata"]["intentType"] = "campus_visit"
    first["semanticMetadata"]["occurrenceId"] = "occ:campus:day:1"
    second["semanticMetadata"]["occurrenceId"] = "occ:campus:day:2"
    snapshot = {
        "portfolioGoalOccurrencePlan": {
            "schemaVersion": "goal-occurrence-plan-v1",
            "occurrences": [
                {
                    "occurrenceId": "occ:campus:day:1",
                    "sourceGoalId": "campus",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                    "distinctGroupId": "distinct:campus",
                },
                {
                    "occurrenceId": "occ:campus:day:2",
                    "sourceGoalId": "campus",
                    "intentType": "campus_visit",
                    "dayNumber": 2,
                    "requirementLevel": "hard",
                    "distinctGroupId": "distinct:campus",
                },
            ],
            "avoidRecentEntities": True,
            "sourceFingerprint": "a" * 16,
        },
        "portfolioDailyCapacityPlan": {
            "1": {
                "usableMinutes": 120,
                "plannedMinutes": 60,
                "routeReserveMinutes": 0,
                "bufferMinutes": 10,
                "intentionalFreeMinutes": 50,
                "unexplainedGapMinutes": 0,
                "targetRouteAnchors": 1,
            },
            "2": {
                "usableMinutes": 120,
                "plannedMinutes": 60,
                "routeReserveMinutes": 0,
                "bufferMinutes": 10,
                "intentionalFreeMinutes": 50,
                "unexplainedGapMinutes": 0,
                "targetRouteAnchors": 1,
            },
        },
        "days": [{"dayNumber": 1, "segments": [first]}, {"dayNumber": 2, "segments": [second]}],
    }
    assert PlanProposalVerifier().verify(snapshot, ledger)["passed"] is False
    assert (
        "goal_occurrence_identity_reused:distinct:campus:CAMPUS"
        in PlanProposalVerifier().verify(snapshot, ledger)["hardFailures"]
    )

    second["poi"]["amapId"] = "campus-day-2"
    result = PlanProposalVerifier().verify(snapshot, ledger)
    assert result["passed"] is True
    assert result["goalOccurrenceCoverage"] == {"occ:campus:day:1": True, "occ:campus:day:2": True}


def test_verifier_collapses_amap_parent_and_child_for_distinct_night_occurrences():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "night", "intentType": "night_view"}]}}
    )
    parent = _segment("night", "18:00", "19:00")
    parent["poi"].update({"amapId": "B000AA3ZCC", "city": "北京"})
    parent["semanticMetadata"].update({"intentType": "night_view", "occurrenceId": "occ:night:day:1"})
    child = _segment("night", "18:00", "19:00")
    child["poi"].update(
        {
            "amapId": "B0FFILD7HG",
            "parentPoiId": "B000AA3ZCC",
            "city": "北京",
        }
    )
    child["semanticMetadata"].update({"intentType": "night_view", "occurrenceId": "occ:night:day:2"})
    snapshot = {
        "portfolioGoalOccurrencePlan": {
            "schemaVersion": "goal-occurrence-plan-v1",
            "occurrences": [
                {
                    "occurrenceId": "occ:night:day:1",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                    "distinctGroupId": "distinct:goal_night_view",
                },
                {
                    "occurrenceId": "occ:night:day:2",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 2,
                    "requirementLevel": "hard",
                    "distinctGroupId": "distinct:goal_night_view",
                },
            ],
        },
        "days": [
            {"dayNumber": 1, "segments": [parent]},
            {"dayNumber": 2, "segments": [child]},
        ],
    }

    result = PlanProposalVerifier().verify(snapshot, ledger)

    assert result["passed"] is False
    assert "goal_occurrence_identity_reused:distinct:goal_night_view:B000AA3ZCC" in result["hardFailures"]


def test_verifier_forces_distinct_identity_for_repeated_hard_night_without_group():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "night", "intentType": "night_view"}]}}
    )
    first = _segment("night", "18:00", "19:00")
    first["poi"].update({"amapId": "B000AA3ZCC", "city": "北京"})
    first["semanticMetadata"].update({"intentType": "night_view", "occurrenceId": "occ:night:day:1"})
    second = _segment("night", "18:00", "19:00")
    second["poi"].update(
        {
            "amapId": "B0FFILD7HG",
            "parentPoiId": "B000AA3ZCC",
            "city": "北京",
        }
    )
    second["semanticMetadata"].update({"intentType": "night_view", "occurrenceId": "occ:night:day:2"})
    snapshot = {
        "portfolioGoalOccurrencePlan": {
            "schemaVersion": "goal-occurrence-plan-v1",
            "occurrences": [
                {
                    "occurrenceId": "occ:night:day:1",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                    "distinctnessPolicy": "reuse_physical_identity_allowed",
                },
                {
                    "occurrenceId": "occ:night:day:2",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 2,
                    "requirementLevel": "hard",
                    "distinctnessPolicy": "reuse_physical_identity_allowed",
                },
            ],
        },
        "days": [
            {"dayNumber": 1, "segments": [first]},
            {"dayNumber": 2, "segments": [second]},
        ],
    }

    result = PlanProposalVerifier().verify(snapshot, ledger)

    assert result["passed"] is False
    assert "goal_occurrence_identity_reused:distinct:night:B000AA3ZCC" in result["hardFailures"]


def test_verifier_rejects_daily_capacity_target_drift_from_brief_targets():
    ledger = ConstraintLedgerCompiler().compile({"city": "Beijing"})
    snapshot = {
        "portfolioDayAnchorTargets": {"1": 2},
        "portfolioDailyCapacityPlan": {
            "1": {
                "usableMinutes": 120,
                "plannedMinutes": 60,
                "routeReserveMinutes": 0,
                "bufferMinutes": 10,
                "intentionalFreeMinutes": 50,
                "unexplainedGapMinutes": 0,
                "targetRouteAnchors": 1,
            }
        },
        "days": [{"dayNumber": 1, "segments": []}],
    }

    result = PlanProposalVerifier().verify(snapshot, ledger)

    assert result["passed"] is False
    assert "daily_capacity_anchor_target_mismatch:day_1:1/2" in result["hardFailures"]


def test_portfolio_verifier_rejects_hard_occurrences_without_candidate_bindings():
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
    )
    segment = _segment("campus", "09:00", "10:00")
    segment["semanticMetadata"].update(
        {
            "intentType": "campus_visit",
            "occurrenceId": "occ:campus:day:1",
            "creativeBriefId": "brief_binding_required",
        }
    )
    snapshot = {
        "portfolioGoalOccurrencePlan": {
            "schemaVersion": "goal-occurrence-plan-v1",
            "occurrences": [
                {
                    "occurrenceId": "occ:campus:day:1",
                    "sourceGoalId": "campus",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                }
            ],
        },
        "days": [{"dayNumber": 1, "segments": [segment]}],
    }
    brief = CreativeBrief(
        briefId="brief_binding_required",
        title="高校",
        primaryAxis="classic",
        requiredGoalIds=["campus"],
    )

    result = PlanProposalVerifier().verify(snapshot, ledger, brief)

    assert result["passed"] is False
    assert "required_candidate_bindings_missing" in result["hardFailures"]
    assert result["requiredCandidateBindingExpectedCount"] == 1
    assert result["requiredCandidateBindingActualCount"] == 0
    assert result["hardCandidateLineageMissingCount"] == 1


def test_verifier_rejects_untrusted_or_wrong_city_poi_identity():
    snapshot = {
        "city": "北京",
        "days": [{"segments": [_segment("campus", "09:00", "10:00")]}],
    }
    snapshot["days"][0]["segments"][0]["poi"]["city"] = "上海市"
    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )
    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


def test_verifier_rejects_non_amap_poi_identity():
    snapshot = {"days": [{"segments": [_segment("campus", "09:00", "10:00")]}]}
    snapshot["days"][0]["segments"][0]["poi"]["source"] = "manual-input"
    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )
    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


@pytest.mark.parametrize("amap_id", ["fake", "fake-amap-id", "spoof"])
def test_verifier_rejects_explicitly_synthetic_amap_identity(amap_id):
    snapshot = {"days": [{"segments": [_segment("campus", "09:00", "10:00")]}]}
    snapshot["days"][0]["segments"][0]["poi"]["amapId"] = amap_id

    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )

    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


@pytest.mark.parametrize(
    ("longitude", "latitude"),
    [
        (float("nan"), 39.9),
        (116.3, float("inf")),
        (200.0, 39.9),
        (116.3, 100.0),
    ],
)
def test_verifier_rejects_non_finite_or_out_of_range_amap_coordinates(
    longitude,
    latitude,
):
    snapshot = {"days": [{"segments": [_segment("campus", "09:00", "10:00")]}]}
    snapshot["days"][0]["segments"][0]["poi"].update({"longitude": longitude, "latitude": latitude})

    result = PlanProposalVerifier().verify(
        snapshot,
        ConstraintLedgerCompiler().compile(
            {"requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]}}
        ),
    )

    assert result["passed"] is False
    assert "required_goal_ungrounded:campus" in result["hardFailures"]


def test_enforce_mode_rejects_optional_segment_without_replayable_v2_admission():
    segment = _segment("optional", "09:00", "10:00")
    segment["id"] = "optional"
    segment["semanticMetadata"].update(
        {
            "portfolioOptional": True,
            "optionalExperienceFamily": "local_life",
            "intentType": "experience",
            "creativeBriefId": "brief_local",
            "poolId": "pool_local",
            "planningSlotId": "slot_local",
            "slotId": "slot_local",
            "dayNumber": 1,
        }
    )
    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        {"days": [{"dayNumber": 1, "segments": [segment]}]}, ConstraintLedgerCompiler().compile({})
    )
    assert "portfolio_optional_consumer_admission_missing:optional" in result["hardFailures"]


def test_enforce_mode_rejects_tampered_v2_admission_report():
    from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService

    segment = _segment("optional", "09:00", "10:00")
    segment["id"] = "optional"
    candidate = {
        **segment["poi"],
        "id": "B000000001",
        "amapId": "B000000001",
        "name": "朝阳社区农贸市场",
        "city": "北京",
        "source": "amap-place-search",
        "providerTypeCode": "060703",
        "type": "购物服务;综合市场;农贸市场",
        "category": "农贸市场",
        "tags": ["社区", "农贸市场"],
        "sourceClaims": [{"claimKey": "local_life", "stance": "support", "sourceUrlHash": "a" * 64}],
    }
    consumer = {
        "briefId": "brief_local",
        "poolId": "pool_local",
        "planningSlotId": "slot_local",
        "slotId": "slot_local",
        "dayNumber": 1,
        "city": "北京",
        "family": "local_life",
        "optionalExperienceFamily": "local_life",
        "activityMode": "experience",
        "requirementLevel": "soft",
        "experienceShape": "area",
        "goal": "体验本地生活",
        "experienceGoal": "体验本地生活",
        "desiredSignals": ["community_market"],
        "avoidSignals": [],
        "evidenceRequirements": {"minimumIndependentClaims": 1},
    }
    report = ConsumerCandidateAdmissionService().evaluate(candidate, consumer)
    report["classification"] = "rejected"
    segment["semanticMetadata"].update(
        {
            "portfolioOptional": True,
            "optionalExperienceFamily": "local_life",
            "intentType": "experience",
            "creativeBriefId": "brief_local",
            "poolId": "pool_local",
            "planningSlotId": "slot_local",
            "slotId": "slot_local",
            "dayNumber": 1,
            "consumerAdmissionReport": report,
        }
    )
    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        {"days": [{"dayNumber": 1, "segments": [segment]}]}, ConstraintLedgerCompiler().compile({})
    )
    assert "portfolio_optional_consumer_admission_invalid:optional" in result["hardFailures"]


def test_enforce_mode_rejects_valid_report_transplanted_to_different_poi():
    from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService

    candidate = {
        "id": "B000000001",
        "amapId": "B000000001",
        "name": "朝阳社区农贸市场",
        "city": "北京",
        "source": "amap-place-search",
        "longitude": 116.42,
        "latitude": 39.92,
        "providerTypeCode": "060703",
        "type": "购物服务;综合市场;农贸市场",
        "category": "农贸市场",
        "tags": ["社区", "农贸市场"],
        "sourceClaims": [
            {
                "claimKey": "local_life",
                "stance": "support",
                "sourceUrlHash": "a" * 64,
            }
        ],
    }
    consumer = {
        "briefId": "brief_local",
        "poolId": "pool_local",
        "planningSlotId": "slot_local",
        "slotId": "slot_local",
        "dayNumber": 1,
        "city": "北京",
        "family": "local_life",
        "optionalExperienceFamily": "local_life",
        "activityMode": "experience",
        "requirementLevel": "soft",
        "experienceShape": "area",
        "goal": "体验本地生活",
        "experienceGoal": "体验本地生活",
        "desiredSignals": ["community_market"],
        "avoidSignals": [],
        "evidenceRequirements": {"minimumIndependentClaims": 1},
    }
    report = ConsumerCandidateAdmissionService().evaluate(candidate, consumer)
    segment = _segment("optional", "09:00", "10:00")
    segment["id"] = "optional"
    segment["poi"].update(
        {
            "amapId": "B000000099",
            "name": "另一座博物馆",
            "city": "北京",
            "source": "amap-place-search",
            "longitude": 116.50,
            "latitude": 39.95,
        }
    )
    segment["semanticMetadata"].update(
        {
            "requirementLevel": "soft_experience",
            "softGoalId": "goal_local_life",
            "intentType": "experience",
            "creativeBriefId": "brief_local",
            "poolId": "pool_local",
            "planningSlotId": "slot_local",
            "slotId": "slot_local",
            "dayNumber": 1,
            "consumerAdmissionReport": report,
        }
    )
    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        {"days": [{"dayNumber": 1, "segments": [segment]}]}, ConstraintLedgerCompiler().compile({})
    )
    assert "portfolio_optional_consumer_admission_invalid:optional" in result["hardFailures"]


def test_enforce_mode_rejects_explicit_soft_segment_without_admission_report():
    segment = _segment("soft_meal", "12:00", "13:00")
    segment["id"] = "soft_meal"
    segment["semanticMetadata"].update(
        {
            "requirementLevel": "soft_experience",
            "softGoalId": "goal_meal",
            "intentType": "meal",
            "creativeBriefId": "brief_food",
            "poolId": "pool_meal",
            "planningSlotId": "slot_meal",
            "slotId": "slot_meal",
            "dayNumber": 1,
        }
    )
    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        {"days": [{"dayNumber": 1, "segments": [segment]}]}, ConstraintLedgerCompiler().compile({})
    )
    assert "portfolio_optional_consumer_admission_missing:soft_meal" in result["hardFailures"]


def test_enforce_mode_rejects_required_segment_without_admission_report():
    segment = _segment("goal_meal", "12:00", "13:00")
    segment["id"] = "required_meal"
    segment["semanticMetadata"].update(
        {
            "intentType": "meal",
            "creativeBriefId": "brief_food",
            "poolId": "pool_meal",
            "planningSlotId": "slot_meal",
            "slotId": "slot_meal",
            "dayNumber": 1,
        }
    )
    ledger = ConstraintLedgerCompiler().compile(
        {"requestIntentContract": {"requiredIntents": [{"goalId": "goal_meal", "intentType": "meal"}]}}
    )

    result = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(
        {"days": [{"dayNumber": 1, "segments": [segment]}]}, ledger
    )

    assert "portfolio_required_consumer_admission_missing:required_meal" in result["hardFailures"]

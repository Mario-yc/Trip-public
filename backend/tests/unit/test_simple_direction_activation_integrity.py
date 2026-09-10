from __future__ import annotations

import copy
import json

import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from backend.tests.unit.test_simple_open_direction_workflow import (
    _live_shaped_simple_partial_snapshot,
    _persist_direction_turn,
    _request_context,
    _route_contract,
)
from src.api.schemas.agent import AgentMessageRequest
from src.api.schemas.agent import AgentInitialPlanOutput
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.simple_open_direction_service import SimpleOpenDirectionService


def _lineage_complete_snapshot(plan_id: str) -> dict:
    snapshot = _live_shaped_simple_partial_snapshot(plan_id)
    for day in snapshot["days"]:
        day_number = int(day["dayNumber"])
        for index, segment in enumerate(day["segments"], start=1):
            metadata = segment["semanticMetadata"]
            intent_type = str(metadata["intentType"])
            goal_id = f"goal_{intent_type}_{day_number}_{index}"
            metadata.update(
                {
                    "dayNumber": day_number,
                    "goalId": goal_id,
                    "sourceGoalId": goal_id,
                    "occurrenceId": f"occ:{goal_id}:day:{day_number}",
                    "lineageAuthority": "goal_occurrence_compiler",
                }
            )
            segment["poi"]["address"] = f"北京市测试路{day_number}-{index}号"
    return snapshot


def _confirmation_ready_snapshot(plan_id: str) -> dict:
    """Build a proposal whose adoption capability is backed by frozen AMap legs."""

    snapshot = _lineage_complete_snapshot(plan_id)
    contract = _route_contract(compact=True)
    snapshot["routeDecisionContract"] = copy.deepcopy(contract)
    for day in snapshot["days"]:
        day_number = int(day["dayNumber"])
        for segment in day["segments"]:
            if str(segment.get("kind") or "") != "meal":
                continue
            metadata = segment["semanticMetadata"]
            constraints = metadata.setdefault("scheduleConstraints", {})
            family = "fixture-family-a" if day_number == 1 else "fixture-family-b"
            fingerprint = ("a" if day_number == 1 else "b") * 64
            constraints["mealExperienceBrief"] = {
                "briefId": f"meal-brief-{day_number}",
                "occurrenceId": metadata["occurrenceId"],
                "planningSlotId": metadata["planningSlotId"],
                "dayNumber": day_number,
                "themeId": family,
                "themeLabel": family,
                "searchTerms": [family],
                "sourceFingerprint": fingerprint,
            }
            constraints["mealSemanticEvidence"] = {
                "amapPoiId": segment["poi"]["amapId"],
                "canonicalBrand": f"fixture-brand-{day_number}",
                "themeId": family,
                "themeLabel": family,
                "groundedFamilyKey": family,
                "matchedTerms": [family],
                "matchedFields": ["tags"],
                "localFoodEvidenceKind": "amap_destination_cuisine_subtype",
                "themeGrounded": True,
                "localFoodPassed": True,
                "sourceFingerprint": fingerprint,
            }
    expected_pairs: list[dict[str, str]] = []
    verified_pairs: list[dict] = []
    for day in snapshot["days"]:
        amap_ids = [str(segment["poi"]["amapId"]).strip().upper() for segment in day["segments"]]
        for from_amap_id, to_amap_id in zip(amap_ids, amap_ids[1:]):
            expected_pair = {
                "fromAmapId": from_amap_id,
                "toAmapId": to_amap_id,
            }
            verified_pair = {
                **expected_pair,
                "transportMode": "transit",
                "durationSeconds": 900,
                "distanceMeters": 2000,
                "provider": "amap-webservice",
                "queriedAt": "2026-08-23T00:00:00+00:00",
            }
            verified_pair["providerEvidenceFingerprint"] = (
                SimpleOpenDirectionService._provider_evidence_fingerprint(verified_pair)
            )
            expected_pairs.append(expected_pair)
            verified_pairs.append(verified_pair)
    snapshot["simpleOpenRouteAssignment"] = {
        "schemaVersion": "simple-open-route-evidence-v2",
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": expected_pairs,
        "verifiedPairs": verified_pairs,
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
    }
    verifier = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert verifier["confirmationPassed"] is True, verifier
    return snapshot


def _bind_route_contract(request_context: dict, snapshot: dict) -> None:
    contract = copy.deepcopy(snapshot["routeDecisionContract"])
    request_context["routeDecisionContract"] = copy.deepcopy(contract)
    request_context["requestIntentContract"]["routeDecisionContract"] = contract


def test_activation_requires_a_verified_amap_route_anchor_on_every_planned_day() -> None:
    healthy_partial = _lineage_complete_snapshot("plan_simple_daily_anchor_integrity")

    healthy_result = SimpleOpenDirectionService.activation_verifier(healthy_partial)

    assert healthy_result["passed"] is True
    assert healthy_result["allPlannedDaysHaveVerifiedAnchor"] is True
    assert healthy_result["plannedDayNumbers"] == [1, 2]
    assert healthy_result["plannedDaysMissingVerifiedAnchor"] == []

    empty_second_day = copy.deepcopy(healthy_partial)
    empty_second_day["days"][1]["segments"] = []
    empty_day_result = SimpleOpenDirectionService.activation_verifier(empty_second_day)

    assert empty_day_result["passed"] is False
    assert empty_day_result["verifiedAmapRouteAnchorCount"] > 0
    assert empty_day_result["allPlannedDaysHaveVerifiedAnchor"] is False
    assert empty_day_result["plannedDaysMissingVerifiedAnchor"] == [2]
    assert "simple_direction_planned_day_missing_verified_amap_anchor" in empty_day_result["hardFailures"]

    non_anchor_second_day = copy.deepcopy(healthy_partial)
    for segment in non_anchor_second_day["days"][1]["segments"]:
        segment["semanticMetadata"]["routeAnchor"] = False
    non_anchor_result = SimpleOpenDirectionService.activation_verifier(non_anchor_second_day)

    assert non_anchor_result["passed"] is False
    assert non_anchor_result["verifiedAmapRouteAnchorCount"] > 0
    assert non_anchor_result["plannedDaysMissingVerifiedAnchor"] == [2]
    assert "simple_direction_planned_day_missing_verified_amap_anchor" in non_anchor_result["hardFailures"]


def test_activation_rejects_zero_target_on_required_calendar_day() -> None:
    partial = _lineage_complete_snapshot("plan_simple_zero_target_calendar_day")
    partial["days"][1]["segments"] = []
    partial["desiredDensityAnchorTargets"] = {"1": 2, "2": 0}
    partial["requiredPlanningDayNumbers"] = [1, 2]
    partial["explicitRestDayNumbers"] = []

    result = SimpleOpenDirectionService.activation_verifier(partial)

    assert result["passed"] is False
    assert result["calendarDayNumbers"] == [1, 2]
    assert result["plannedDayNumbers"] == [1, 2]
    assert result["uncoveredDayNumbers"] == [2]
    assert result["plannedDaysMissingVerifiedAnchor"] == [2]
    assert "simple_direction_required_day_target_missing" in result["hardFailures"]
    assert "simple_direction_required_day_empty" in result["hardFailures"]
    assert "simple_direction_daily_anchor_target_day_partition_invalid" in result["hardFailures"]


def test_activation_allows_zero_target_only_with_explicit_rest_day_provenance() -> None:
    partial = _lineage_complete_snapshot("plan_simple_explicit_rest_calendar_day")
    partial["days"][1]["segments"] = []
    partial["desiredDensityAnchorTargets"] = {"1": 2, "2": 0}
    partial["requiredPlanningDayNumbers"] = [1]
    partial["explicitRestDayNumbers"] = [2]

    result = SimpleOpenDirectionService.activation_verifier(partial)

    assert result["passed"] is True
    assert result["calendarDayNumbers"] == [1, 2]
    assert result["plannedDayNumbers"] == [1]
    assert result["explicitRestDayNumbers"] == [2]
    assert result["uncoveredDayNumbers"] == []


def test_activation_requires_authoritative_daily_coverage_source() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_untrusted_daily_source")
    snapshot["dailyPlanningCoverageSource"] = "derived_from_materialized_segments"

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert result["dailyPlanningCoverageSourceInvalid"] is True
    assert "simple_direction_daily_planning_coverage_source_invalid" in result["hardFailures"]


def test_activation_rejects_positive_target_on_explicit_rest_day() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_rest_day_positive_target")
    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = [2]
    snapshot["desiredDensityAnchorTargets"] = {"1": 2, "2": 2}

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert result["dailyTargetDayPartitionInvalid"] is True
    assert result["positiveTargetDayNumbers"] == [1, 2]
    assert result["zeroTargetDayNumbers"] == []
    assert "simple_direction_daily_anchor_target_day_partition_invalid" in result["hardFailures"]


def test_activation_and_proposal_confirmation_require_exact_daily_anchor_targets() -> None:
    activation_snapshot = _lineage_complete_snapshot("plan_simple_anchor_target_mismatch")
    activation_snapshot["desiredDensityAnchorTargets"] = {"1": 3, "2": 1}

    activation = SimpleOpenDirectionService.activation_verifier(activation_snapshot)

    assert activation["passed"] is False
    assert activation["dayAnchorTargets"] == {"1": 3, "2": 1}
    assert activation["dayAnchorActuals"] == {"1": 2, "2": 2}
    assert activation["anchorTargetMismatchDays"] == [1, 2]
    assert "simple_direction_daily_anchor_target_mismatch" in activation["hardFailures"]

    proposal_snapshot = _confirmation_ready_snapshot("plan_simple_proposal_anchor_target_mismatch")
    proposal_snapshot["desiredDensityAnchorTargets"] = {"1": 3, "2": 1}

    proposal = SimpleOpenDirectionService._proposal_verifier(proposal_snapshot)

    assert proposal["confirmationPassed"] is False
    assert "simple_direction_daily_anchor_target_mismatch" in proposal["hardFailures"]


@pytest.mark.parametrize(
    "invalid_targets",
    [
        {"1": 2},
        {"1": 2, "2": False},
        {"1": 2, "2": 0.0},
        {"1": 2, "02": 0},
        {"1": 0, "2": 0},
        {1: 2, 2: 0},
    ],
)
def test_activation_fails_closed_when_declared_daily_anchor_contract_is_invalid(invalid_targets: dict) -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_invalid_anchor_target_contract")
    snapshot["desiredDensityAnchorTargets"] = invalid_targets

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert result["explicitAnchorTargetsApplied"] is False
    assert result["anchorTargetContractInvalid"] is True
    assert "simple_direction_daily_anchor_target_contract_invalid" in result["hardFailures"]


def test_activation_fails_closed_when_simple_open_daily_anchor_contract_is_missing() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_missing_anchor_target_contract")
    snapshot.pop("desiredDensityAnchorTargets")

    result = SimpleOpenDirectionService.activation_verifier(snapshot)
    proposal = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert result["passed"] is False
    assert result["anchorTargetContractInvalid"] is True
    assert "simple_direction_daily_anchor_target_contract_invalid" in result["hardFailures"]
    assert proposal["confirmationPassed"] is False
    assert "simple_direction_daily_anchor_target_contract_invalid" in proposal["hardFailures"]


def test_activation_blocks_provider_exhausted_required_pending_even_when_each_day_has_an_anchor() -> None:
    partial = _lineage_complete_snapshot("plan_simple_daily_anchor_pending")
    partial["portfolioPendingSlots"] = [
        {
            "id": "pending:slot_required_pending_night",
            "slotId": "slot_required_pending_night",
            "planningSlotId": "slot_required_pending_night",
            "poolId": "night_view_pool",
            "dayNumber": 2,
            "intentType": "night_view",
            "requirementLevel": "required",
            "goalId": "goal_required_pending_night",
            "sourceGoalId": "goal_required_pending_night",
            "occurrenceId": "occ:goal_required_pending_night:day:2",
            "lineageAuthority": "goal_occurrence_compiler",
            "groundingStatus": "unresolved",
            "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
            "simpleDirectionProviderExhausted": True,
        }
    ]

    result = SimpleOpenDirectionService.activation_verifier(partial)

    assert result["passed"] is False
    assert result["allPlannedDaysHaveVerifiedAnchor"] is True
    assert result["providerExhaustedRequiredSlotCount"] == 1
    assert result["blockingPendingHardSlotCount"] == 1
    assert "simple_direction_pending_hard_slot" in result["hardFailures"]


def test_activation_rejects_materialized_evening_slot_when_dynamic_schedule_proves_it_infeasible() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_rejected_evening_schedule")
    segment = snapshot["days"][0]["segments"][0]
    segment["semanticMetadata"]["schedulePreference"] = {
        "dayPart": "evening",
        "intentType": "park",
        "sourceGoalId": segment["semanticMetadata"]["sourceGoalId"],
        "occurrenceId": segment["semanticMetadata"]["occurrenceId"],
    }
    segment["semanticMetadata"]["scheduleDecision"] = {
        "startTime": None,
        "endTime": None,
        "durationMinutes": 80,
        "decisionSource": "dynamic_schedule_solver",
        "openingEvidenceStatus": "verified_provider_evidence",
        "solarBoundarySource": "local_sunset_from_date_and_coordinates",
        "scheduleConfidence": "rejected",
        "provisionalReasons": [],
        "constraintPassed": False,
        "failureReason": "opening_window_does_not_overlap_semantic_evening",
    }

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert result["scheduleRejectedSegmentCount"] == 1
    assert "simple_direction_dynamic_schedule_constraint_failed" in result["hardFailures"]


def test_confirmation_rejects_materialized_segment_without_commit_ready_clock() -> None:
    """Live -10: no proven conflict is not the same as a writable schedule."""

    snapshot = _confirmation_ready_snapshot("plan_simple_unresolved_materialized_schedule")
    segment = snapshot["days"][0]["segments"][0]
    segment["startTime"] = ""
    segment["endTime"] = ""
    segment["durationMinutes"] = 0
    segment["semanticMetadata"]["scheduleDecision"] = {
        "startTime": None,
        "endTime": None,
        "durationMinutes": None,
        "decisionSource": "dynamic_schedule_solver",
        "scheduleConfidence": "flexible",
        "constraintPassed": True,
        "provisionalReasons": ["duration_estimate_unavailable"],
    }

    result = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert result["confirmationPassed"] is False
    assert result["simpleOpenDirectionVerified"] is False
    assert result["scheduleInvalidSegmentCount"] == 1
    assert "simple_direction_schedule_interval_invalid" in result["hardFailures"]


@pytest.mark.parametrize(
    ("start_time", "end_time", "duration_minutes"),
    [
        ("9:00", "10:30", 90),
        ("10:30", "10:30", 1),
    ],
    ids=["non-strict-clock", "non-positive-interval"],
)
def test_activation_rejects_non_commit_ready_clock_intervals(
    start_time: str,
    end_time: str,
    duration_minutes: int,
) -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_invalid_clock_contract")
    segment = snapshot["days"][0]["segments"][0]
    segment["startTime"] = start_time
    segment["endTime"] = end_time
    segment["durationMinutes"] = duration_minutes

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert result["scheduleInvalidSegmentCount"] == 1
    assert "simple_direction_schedule_interval_invalid" in result["hardFailures"]


def test_activation_accepts_valid_non_overlapping_materialized_schedule() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_valid_materialized_schedule")

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is True
    assert result["scheduleInvalidSegmentCount"] == 0
    assert result["scheduleDurationMismatchCount"] == 0
    assert result["scheduleOverlapCount"] == 0


def test_activation_does_not_reject_server_sealed_explicit_every_day_park_lineage() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_every_day_park_lineage")
    park_segment = next(
        segment
        for day in snapshot["days"]
        for segment in day["segments"]
        if segment["semanticMetadata"]["intentType"] == "meal"
    )
    park_segment["poi"].update(
        {
            "name": "独立公共公园",
            "category": "park",
            "type": "风景名胜;公园广场;公园",
            "providerType": "风景名胜;公园广场;公园",
            "providerTypeCode": "110101",
            "experienceIndependenceEvidence": {
                "schemaVersion": "experience-independence-v1",
                "status": "standalone_verified",
                "physicalGroupId": park_segment["poi"]["amapId"],
            },
        }
    )
    park_segment["semanticMetadata"].update(
        {
            "intentType": "park",
            "rawNeed": "独立公共公园",
            "lineageAuthority": "simple_open_request_contract_every_day_park",
        }
    )
    snapshot["desiredDensityAnchorTargets"]["1"] = 3

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["materializedLineageFailureCount"] == 0
    assert "simple_direction_materialized_lineage_invalid" not in result["hardFailures"]


def test_activation_rejects_materialized_schedule_overlap_and_duration_drift() -> None:
    snapshot = _lineage_complete_snapshot("plan_simple_invalid_materialized_schedule")
    first, second = snapshot["days"][0]["segments"][:2]
    second["startTime"] = "10:00"
    second["endTime"] = "11:30"
    second["durationMinutes"] = 90
    first["durationMinutes"] = 60

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert result["scheduleInvalidSegmentCount"] == 1
    assert result["scheduleDurationMismatchCount"] == 1
    assert result["scheduleOverlapCount"] == 1
    assert "simple_direction_schedule_interval_invalid" in result["hardFailures"]
    assert "simple_direction_schedule_overlap" in result["hardFailures"]


def test_activation_rejects_duplicate_city_and_occurrence_lineage_conflicts() -> None:
    base = _lineage_complete_snapshot("plan_simple_activation_integrity")
    assert SimpleOpenDirectionService.activation_verifier(base)["passed"] is True

    duplicate = copy.deepcopy(base)
    first_poi = duplicate["days"][0]["segments"][0]["poi"]
    second_campus_poi = duplicate["days"][1]["segments"][0]["poi"]
    second_campus_poi.update(
        {
            "amapId": "B123456789",
            "name": first_poi["name"],
            "address": first_poi["address"],
            "latitude": first_poi["latitude"],
            "longitude": first_poi["longitude"],
        }
    )
    duplicate_result = SimpleOpenDirectionService.activation_verifier(duplicate)
    assert duplicate_result["passed"] is False
    assert duplicate_result["duplicateMaterializedIdentityCount"] == 1
    assert "simple_direction_materialized_poi_duplicate" in duplicate_result["hardFailures"]

    wrong_city = copy.deepcopy(base)
    wrong_city["days"][0]["segments"][1]["poi"]["city"] = "上海"
    wrong_city_result = SimpleOpenDirectionService.activation_verifier(wrong_city)
    assert wrong_city_result["passed"] is False
    assert wrong_city_result["materializedCityMismatchCount"] == 1
    assert "simple_direction_materialized_poi_city_mismatch" in wrong_city_result["hardFailures"]

    xor_conflict = copy.deepcopy(base)
    materialized_metadata = xor_conflict["days"][0]["segments"][2]["semanticMetadata"]
    xor_conflict["portfolioPendingSlots"] = [
        {
            "planningSlotId": materialized_metadata["planningSlotId"],
            "poolId": materialized_metadata["poolId"],
            "dayNumber": materialized_metadata["dayNumber"],
            "intentType": materialized_metadata["intentType"],
            "requirementLevel": "required",
            "goalId": materialized_metadata["goalId"],
            "sourceGoalId": materialized_metadata["sourceGoalId"],
            "occurrenceId": materialized_metadata["occurrenceId"],
            "groundingStatus": "unresolved",
            "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
            "simpleDirectionProviderExhausted": True,
        }
    ]
    xor_result = SimpleOpenDirectionService.activation_verifier(xor_conflict)
    assert xor_result["passed"] is False
    assert xor_result["materializedPendingLineageConflictCount"] == 1
    assert "simple_direction_materialized_pending_lineage_conflict" in xor_result["hardFailures"]

    wrong_day = copy.deepcopy(base)
    wrong_day["days"][0]["segments"][0]["semanticMetadata"]["dayNumber"] = 2
    wrong_day_result = SimpleOpenDirectionService.activation_verifier(wrong_day)
    assert wrong_day_result["passed"] is False
    assert wrong_day_result["materializedLineageFailureCount"] == 1
    assert "simple_direction_materialized_lineage_invalid" in wrong_day_result["hardFailures"]


def test_activation_rejects_noncanonical_required_lineage_and_duplicate_pending_occurrence() -> None:
    base = _lineage_complete_snapshot("plan_simple_activation_lineage")

    missing_authority = copy.deepcopy(base)
    missing_authority["days"][0]["segments"][0]["semanticMetadata"].pop("lineageAuthority")
    missing_authority_result = SimpleOpenDirectionService.activation_verifier(missing_authority)
    assert missing_authority_result["passed"] is False
    assert "simple_direction_materialized_lineage_invalid" in missing_authority_result["hardFailures"]

    conflicting_goal = copy.deepcopy(base)
    conflicting_goal["days"][0]["segments"][0]["semanticMetadata"]["sourceGoalId"] = "goal_other"
    conflicting_goal_result = SimpleOpenDirectionService.activation_verifier(conflicting_goal)
    assert conflicting_goal_result["passed"] is False
    assert "simple_direction_materialized_lineage_invalid" in conflicting_goal_result["hardFailures"]

    duplicate_pending = copy.deepcopy(base)
    duplicate_pending["portfolioPendingSlots"] = [
        {
            "planningSlotId": planning_slot_id,
            "poolId": "night_view_pool",
            "dayNumber": 1,
            "intentType": "night_view",
            "requirementLevel": "required",
            "goalId": "goal_missing_night",
            "sourceGoalId": "goal_missing_night",
            "occurrenceId": "occ:goal_missing_night:day:1",
            "lineageAuthority": "goal_occurrence_compiler",
            "groundingStatus": "unresolved",
            "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
            "simpleDirectionProviderExhausted": True,
        }
        for planning_slot_id in ("slot_missing_night_a", "slot_missing_night_b")
    ]
    duplicate_pending_result = SimpleOpenDirectionService.activation_verifier(duplicate_pending)
    assert duplicate_pending_result["passed"] is False
    assert duplicate_pending_result["pendingSlotLineageConflictCount"] > 0
    assert "simple_direction_pending_slot_lineage_conflict" in duplicate_pending_result["hardFailures"]


@pytest.mark.parametrize(
    "corruption",
    [
        "physical_duplicate",
        "coordinated_city_change",
        "coordinated_lineage_change",
        "delete_required_materialized",
        "demote_required_materialized",
        "promote_optional_materialized",
        "inject_untrusted_adcode",
    ],
)
def test_save_active_rejects_invalid_server_snapshot_before_proposal_or_capability_mutation(
    corruption: str,
) -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "save activation integrity")
        agent = AgentService(connection)
        agent.initial_planning_mode = "simple_open_v1"
        direction = SimpleOpenDirectionService(connection)
        root_turn_id = agent._insert_turn(
            session.session_id,
            "user",
            "北京高校两日游，公共交通，适度绕行",
            "active",
        )
        request_context = _request_context(root_turn_id=root_turn_id)
        request_context["resolvedTripDates"] = {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        }
        request_context["requestIntentContract"]["dayCount"] = 2
        offered_snapshot = _confirmation_ready_snapshot(session.active_plan_id)
        _bind_route_contract(request_context, offered_snapshot)
        source_turn_id, proposal_id = _persist_direction_turn(
            connection=connection,
            service=agent,
            direction_service=direction,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="服务端保存校验方向",
            poi_id="B000A",
            snapshot_override=offered_snapshot,
            request_context_override=request_context,
        )
        choice = next(
            option
            for option in agent._turn_response(source_turn_id).choice_options
            if option["action"] == "select_plan_proposal"
        )
        confirmed = agent.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_turn_id,
                        "choiceId": choice["id"],
                    }
                },
            ),
        )
        assert confirmed.version is not None
        root_portfolio_id = str(choice["rootPortfolioId"])

        version_row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (confirmed.version.id,),
        ).fetchone()
        active_snapshot = json.loads(version_row["snapshot_json"])
        if corruption == "physical_duplicate":
            first_poi = active_snapshot["days"][0]["segments"][0]["poi"]
            second_campus_poi = active_snapshot["days"][1]["segments"][0]["poi"]
            second_campus_poi.update(
                {
                    "amapId": "B123456789",
                    "name": first_poi["name"],
                    "address": first_poi["address"],
                    "latitude": first_poi["latitude"],
                    "longitude": first_poi["longitude"],
                }
            )
        elif corruption == "coordinated_city_change":
            active_snapshot["city"] = "上海"
            for day in active_snapshot["days"]:
                for segment in day["segments"]:
                    if isinstance(segment.get("poi"), dict):
                        segment["poi"]["city"] = "上海"
        elif corruption == "coordinated_lineage_change":
            for day in active_snapshot["days"]:
                day_number = int(day["dayNumber"])
                for index, segment in enumerate(day["segments"], start=1):
                    metadata = segment["semanticMetadata"]
                    goal_id = f"goal_forged_{day_number}_{index}"
                    metadata.update(
                        {
                            "goalId": goal_id,
                            "sourceGoalId": goal_id,
                            "occurrenceId": f"occ:{goal_id}:day:{day_number}",
                        }
                    )
        elif corruption == "delete_required_materialized":
            active_snapshot["days"][0]["segments"] = active_snapshot["days"][0]["segments"][1:]
        elif corruption == "demote_required_materialized":
            active_snapshot["days"][0]["segments"][0]["semanticMetadata"].update(
                {"required": False, "requirementLevel": "optional"}
            )
        elif corruption == "promote_optional_materialized":
            optional_segment = next(
                segment
                for day in active_snapshot["days"]
                for segment in day["segments"]
                if segment["semanticMetadata"].get("requirementLevel") == "optional"
            )
            optional_segment["semanticMetadata"].update({"required": True, "requirementLevel": "required"})
        else:
            active_snapshot["adcode"] = "310000"
            for day in active_snapshot["days"]:
                for segment in day["segments"]:
                    if isinstance(segment.get("poi"), dict):
                        segment["poi"]["adcode"] = "310000"
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(active_snapshot, ensure_ascii=False), confirmed.version.id),
        )
        connection.commit()

        proposal_before = tuple(
            connection.execute(
                "SELECT snapshot_json, verifier_json, evidence_json, status FROM agent_plan_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        )
        root_before = tuple(
            connection.execute(
                "SELECT summary_json, expected_base_version_id, status FROM agent_plan_portfolios WHERE id = ?",
                (root_portfolio_id,),
            ).fetchone()
        )
        counts_before = tuple(
            connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM itinerary_versions), "
                "(SELECT COUNT(*) FROM itinerary_patches), "
                "(SELECT COUNT(*) FROM conversation_turns WHERE status = 'internal_capability')"
            ).fetchone()
        )

        with pytest.raises(ValueError, match="simple_direction_save_activation_failed"):
            direction.save_active_direction(
                session_id=session.session_id,
                proposal_id=proposal_id,
                planning_root_id=root_turn_id,
                portfolio_id=root_portfolio_id,
                base_version_id=confirmed.version.id,
            )

        assert (
            tuple(
                connection.execute(
                    "SELECT snapshot_json, verifier_json, evidence_json, status FROM agent_plan_proposals WHERE id = ?",
                    (proposal_id,),
                ).fetchone()
            )
            == proposal_before
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT summary_json, expected_base_version_id, status FROM agent_plan_portfolios WHERE id = ?",
                    (root_portfolio_id,),
                ).fetchone()
            )
            == root_before
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM itinerary_versions), "
                    "(SELECT COUNT(*) FROM itinerary_patches), "
                    "(SELECT COUNT(*) FROM conversation_turns WHERE status = 'internal_capability')"
                ).fetchone()
            )
            == counts_before
        )


def test_activation_authority_allows_a_new_canonical_optional_activity() -> None:
    authoritative = _lineage_complete_snapshot("plan_optional_add")
    active = copy.deepcopy(authoritative)
    optional = copy.deepcopy(active["days"][0]["segments"][0])
    optional["id"] = "seg_new_optional_campus"
    optional["poi"].update(
        {
            "id": "poi_new_optional_campus",
            "amapId": "B0OPTIONAL1",
            "name": "新增可选高校",
            "address": "北京市海淀区新增可选路1号",
            "latitude": 39.912345,
            "longitude": 116.312345,
        }
    )
    optional["semanticMetadata"].update(
        {
            "required": False,
            "requirementLevel": "optional",
            "goalId": "goal_optional_campus",
            "sourceGoalId": "goal_optional_campus",
            "occurrenceId": "occ:goal_optional_campus:day:1",
            "planningSlotId": "slot_optional_campus",
            "poolId": "pool_optional_campus",
            "dayNumber": 1,
            "lineageAuthority": "goal_occurrence_compiler",
        }
    )
    optional["startTime"] = "15:00"
    optional["endTime"] = "16:30"
    active["days"][0]["segments"].append(optional)
    active["desiredDensityAnchorTargets"] = {"1": 3, "2": 2}

    result = SimpleOpenDirectionService.activation_verifier(
        active,
        authoritative_city="北京",
        authoritative_adcode="",
        authoritative_snapshot=authoritative,
    )

    assert result["passed"] is True
    assert result["authoritativeLineageMismatchCount"] == 0


def test_agent_response_reports_duplicate_direction_as_zero_delta_not_a_new_offer() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "duplicate direction response truth")
        agent = AgentService(connection)
        agent.initial_planning_mode = "simple_open_v1"
        root_turn_id = agent._insert_turn(
            session.session_id,
            "user",
            "北京高校两日游，公共交通，适度绕行",
            "active",
        )
        first_assistant_id = agent._insert_turn(session.session_id, "assistant", "", "streaming")
        request_context = _request_context(root_turn_id=root_turn_id)
        request_context["sessionId"] = session.session_id
        request_context["resolvedTripDates"] = {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        }
        request_context["requestIntentContract"]["dayCount"] = 2
        first_snapshot = _confirmation_ready_snapshot(session.active_plan_id)
        _bind_route_contract(request_context, first_snapshot)
        first = agent._simple_open_direction_proposal_response(
            session_id=session.session_id,
            user_turn_id=root_turn_id,
            assistant_turn_id=first_assistant_id,
            content="北京高校两日游，公共交通，适度绕行",
            request_context=request_context,
            pipeline_context=request_context["pipelineContext"],
            session_before=agent._session(session.session_id),
            initial_plan=AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[]),
            resolved_dates=request_context["resolvedTripDates"],
            grounding_report={"resultState": "partial"},
            snapshot=first_snapshot,
            tool_events=[],
        )
        assert first.assistant_turn.choice_options

        second_user_id = agent._insert_turn(
            session.session_id,
            "user",
            "再生成一个方向",
            "active",
        )
        second_assistant_id = agent._insert_turn(session.session_id, "assistant", "", "streaming")
        second_context = copy.deepcopy(request_context)
        second_context["currentUserTurnId"] = second_user_id
        second_context["planningSelectionRootTurnId"] = root_turn_id
        second_context["viewResolution"] = {
            "inputViewContext": "comparison",
            "resolvedAction": "generate_new_direction",
            "resolutionSource": "server_validated_opaque_choice",
        }
        duplicate_snapshot = copy.deepcopy(first_snapshot)
        duplicate_snapshot["title"] = "只有标题变化的伪新方向"
        duplicate_snapshot["decisionRationale"] = "地点集合没有变化。"

        duplicate = agent._simple_open_direction_proposal_response(
            session_id=session.session_id,
            user_turn_id=second_user_id,
            assistant_turn_id=second_assistant_id,
            content="再生成一个方向",
            request_context=second_context,
            pipeline_context=second_context["pipelineContext"],
            session_before=agent._session(session.session_id),
            initial_plan=AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[]),
            resolved_dates=second_context["resolvedTripDates"],
            grounding_report={"resultState": "partial"},
            snapshot=duplicate_snapshot,
            tool_events=[],
        )

        payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (second_assistant_id,),
            ).fetchone()["agent_response_json"]
        )
        # No proposal was appended, but the root still contains the original
        # adoption-ready A direction, so the user can continue with its
        # already-issued opaque capability instead of being told that no
        # confirmable direction exists.
        assert duplicate.terminal_status == "needs_confirmation"
        assert duplicate.assistant_turn.choice_options == []
        assert payload["reasonCode"] == "no_material_novelty"
        assert payload["proposalDelta"] == 0
        assert payload["comparisonProjections"] == []
        assert "没有生成新的方向" in duplicate.assistant_turn.content
        assert "已生成 1 个" not in duplicate.assistant_turn.content
        assert "simple_direction_no_material_novelty" in json.dumps(payload["toolEvents"], ensure_ascii=False)
        assert '"newProposalDelta": 0' in json.dumps(payload["toolEvents"], ensure_ascii=False)
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0] == 1

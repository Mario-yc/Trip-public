from __future__ import annotations

from typing import Any

from backend.tests.unit.test_agent_service import clear_database, open_db
from src.services.conversation_service import ConversationService
from src.services.creative_planning_models import PlanPortfolio
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.simple_open_direction_service import SimpleOpenDirectionService


REQUEST_FINGERPRINT = "r" * 64


def _amap_id(number: int) -> str:
    return f"B{number:08d}"


def _poi(
    number: int,
    *,
    name: str,
    parent_number: int | None = None,
    independence_status: str | None = None,
) -> dict[str, Any]:
    material: dict[str, Any] = {
        "id": f"poi_{number}",
        "amapId": _amap_id(number),
        "name": name,
        "city": "示例城",
        "category": "all",
        "type": "风景名胜;公园广场;公园",
        "providerType": "风景名胜;公园广场;公园",
        "providerTypeCode": "110101",
        "latitude": 30.0 + number / 100000,
        "longitude": 120.0 + number / 100000,
        "source": "amap-place-search",
        "confidence": 0.9,
    }
    if parent_number is not None:
        material["parentPoiId"] = _amap_id(parent_number)
    if independence_status:
        material["experienceIndependenceEvidence"] = {
            "schemaVersion": "experience-independence-v1",
            "status": independence_status,
            "physicalGroupId": _amap_id(parent_number or number),
        }
    return material


def _segment(
    *,
    day_number: int,
    role: str,
    poi_number: int,
    parent_number: int | None = None,
    independence_status: str | None = None,
) -> dict[str, Any]:
    slot_id = f"day_{day_number}_{role}"
    replaceable = role in {"meal", "park"}
    independence = (
        {
            "schemaVersion": "experience-independence-v1",
            "status": independence_status,
            "physicalGroupId": _amap_id(parent_number or poi_number),
        }
        if independence_status
        else None
    )
    return {
        "id": f"segment_{slot_id}",
        "startTime": {"campus": "09:00", "meal": "12:00", "park": "17:00"}[role],
        "durationMinutes": 90,
        "kind": role,
        "poi": _poi(
            poi_number,
            name=f"{role}-{poi_number}",
            parent_number=parent_number,
            independence_status=independence_status,
        ),
        "semanticMetadata": {
            "intentType": "campus_visit" if role == "campus" else role,
            "routeAnchor": True,
            "groundingStatus": "verified_amap",
            "planningSlotId": slot_id,
            "dayNumber": day_number,
            "scheduleConstraints": {
                "replaceablePoi": replaceable,
                **({"experienceIndependenceEvidence": independence} if independence else {}),
            },
        },
    }


def _snapshot(
    plan_id: str,
    *,
    campuses: tuple[int, int],
    meals: tuple[int, int] | None = None,
    parks: tuple[int, int] | None = None,
    park_parents: tuple[int | None, int | None] = (None, None),
    park_statuses: tuple[str, str] = ("standalone_verified", "standalone_verified"),
) -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    for day_number in (1, 2):
        segments = [
            _segment(day_number=day_number, role="campus", poi_number=campuses[day_number - 1])
        ]
        if meals is not None:
            segments.append(
                _segment(day_number=day_number, role="meal", poi_number=meals[day_number - 1])
            )
        if parks is not None:
            segments.append(
                _segment(
                    day_number=day_number,
                    role="park",
                    poi_number=parks[day_number - 1],
                    parent_number=park_parents[day_number - 1],
                    independence_status=park_statuses[day_number - 1],
                )
            )
        days.append(
            {
                "id": f"day_{day_number}",
                "dayNumber": day_number,
                "date": f"2026-10-0{day_number}",
                "segments": segments,
            }
        )
    return {
        "id": plan_id,
        "title": plan_id,
        "city": "示例城",
        "status": "draft",
        "days": days,
        "routeOptions": [],
    }


def _attempt(
    campuses: tuple[int, int],
    *,
    two_new: bool = True,
    single_new: bool = False,
) -> dict[str, Any]:
    assignments: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for day_number, campus_number in enumerate(campuses, start=1):
        slot_id = f"day_{day_number}_campus"
        entity_fingerprint = f"entity-{campus_number}"
        query_fingerprint = f"query-{campus_number}"
        assignment = {
            "dayNumber": day_number,
            "slotId": slot_id,
            "evidenceEntityFingerprint": entity_fingerprint,
            "canonicalName": f"实体{campus_number}",
            "isNewQualificationEntity": not (single_new and day_number == 2),
            "page": 1,
            "offset": 5,
            "queryFingerprint": query_fingerprint,
        }
        assignments.append(assignment)
        outcomes.append(
            {
                "slotId": slot_id,
                "evidenceEntityFingerprint": entity_fingerprint,
                "providerOutcome": "success",
                "selectedAmapId": _amap_id(campus_number),
                "queryFingerprint": query_fingerprint,
                "page": 1,
            }
        )
    return {
        "schemaVersion": "simple-direction-frontier-attempt-v1",
        "executionId": "execution-v3",
        "requestContractFingerprint": REQUEST_FINGERPRINT,
        "attemptFingerprint": "a" * 64,
        "campusAssignments": assignments,
        "validatedOutcomes": outcomes,
        "twoNewAnchorsRequired": two_new,
        "singleNewAnchorFallback": single_new,
        "newQualifiedEntityCount": 1 if single_new else 2,
    }


def _service_with_history(snapshot: dict[str, Any], *, status: str = "adoption_ready"):
    clear_database()
    connection = open_db()
    session = ConversationService(connection).create_session("示例城", "novelty v3")
    store = PlanPortfolioStore(connection)
    portfolio_id = "portfolio_novelty_v3"
    store.create(
        PlanPortfolio(
            portfolioId=portfolio_id,
            sessionId=session.session_id,
            sourceUserTurnId="root_novelty_v3",
            sourceAssistantTurnId="assistant_novelty_v3",
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=REQUEST_FINGERPRINT,
            status="awaiting_selection",
        ),
        [],
    )
    store.upsert_partial_preview(
        portfolio_id=portfolio_id,
        proposal_id="proposal_prior",
        choice_id="choice_prior",
        snapshot=snapshot,
        brief={"briefId": "brief_prior", "title": "prior"},
        verifier={"compactRouteContractRequired": True, "confirmationPassed": status == "adoption_ready"},
        score={"hardConstraintPassed": status == "adoption_ready"},
        generation_lineage={"workflowMode": "simple_direction_v1"},
        status=status,
        evidence={"workflowMode": "simple_direction_v1"},
    )
    return connection, SimpleOpenDirectionService(connection), portfolio_id


def test_v3_two_new_campuses_and_two_standalone_changes_are_storage_ready() -> None:
    connection, service, portfolio_id = _service_with_history(
        _snapshot("prior", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    )
    try:
        candidate = _snapshot("candidate", campuses=(3, 4), meals=(13, 14), parks=(23, 24))
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=_attempt((3, 4)),
            candidate_confirmation_passed=True,
        )
        evidence = candidate["simpleDirectionNoveltyEvidence"]
        assert evaluation.status == "distinct"
        assert evaluation.matching_proposal_id is None
        assert evidence["schemaVersion"] == "simple-direction-novelty-v3"
        assert evidence["campusAssignmentsGrounded"] is True
        assert evidence["readyNoveltyPassed"] is True
        assert evidence["comparisons"][0]["changedCampusDayCount"] == 2
        assert evidence["comparisons"][0]["totalStandaloneChangedCount"] == 4
        assert evidence["comparisons"][0]["changedOrderedPairCount"] >= 1
    finally:
        connection.close()


def test_v3_two_new_anchor_claim_rejects_candidate_that_changes_only_one_campus() -> None:
    connection, service, portfolio_id = _service_with_history(
        _snapshot("prior", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    )
    try:
        candidate = _snapshot("candidate", campuses=(3, 2), meals=(13, 14), parks=(23, 24))
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=_attempt((3, 2)),
            candidate_confirmation_passed=True,
        )
        comparison = candidate["simpleDirectionNoveltyEvidence"]["comparisons"][0]
        assert evaluation.status == "collision"
        assert evaluation.matching_proposal_id == "proposal_prior"
        assert comparison["campusRotationPassed"] is False
        assert comparison["passedForStorage"] is False
    finally:
        connection.close()


def test_v3_single_new_anchor_fallback_is_disclosed_and_can_pass() -> None:
    connection, service, portfolio_id = _service_with_history(
        _snapshot("prior", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    )
    try:
        candidate = _snapshot("candidate", campuses=(3, 2), meals=(13, 14), parks=(23, 24))
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=_attempt((3, 2), two_new=False, single_new=True),
            candidate_confirmation_passed=True,
        )
        evidence = candidate["simpleDirectionNoveltyEvidence"]
        assert evaluation.status == "distinct"
        assert evidence["singleNewAnchorFallback"] is True
        assert evidence["readyNoveltyPassed"] is True
    finally:
        connection.close()


def test_v3_embedded_or_same_parent_park_children_do_not_manufacture_novelty() -> None:
    prior = _snapshot(
        "prior",
        campuses=(1, 2),
        meals=(11, 12),
        parks=(21, 22),
        park_parents=(31, 32),
    )
    connection, service, portfolio_id = _service_with_history(prior)
    try:
        candidate = _snapshot(
            "candidate",
            campuses=(3, 4),
            meals=(11, 12),
            parks=(23, 24),
            park_parents=(31, 32),
            park_statuses=("embedded_in_day_anchor", "independence_pending"),
        )
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=_attempt((3, 4)),
            candidate_confirmation_passed=True,
        )
        comparison = candidate["simpleDirectionNoveltyEvidence"]["comparisons"][0]
        assert evaluation.status == "collision"
        assert evaluation.matching_proposal_id == "proposal_prior"
        assert comparison["totalStandaloneChangedCount"] == 0
        assert comparison["standaloneNoveltyPassed"] is False
    finally:
        connection.close()


def test_v3_campus_rotation_cannot_also_count_as_standalone_experience_novelty() -> None:
    prior = _snapshot("prior", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    candidate = _snapshot("candidate", campuses=(3, 4), meals=(11, 12), parks=(21, 22))
    for snapshot in (prior, candidate):
        for day in snapshot["days"]:
            campus = day["segments"][0]
            campus["semanticMetadata"]["scheduleConstraints"]["replaceablePoi"] = True

    connection, service, portfolio_id = _service_with_history(prior)
    try:
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=_attempt((3, 4)),
            candidate_confirmation_passed=True,
        )
        comparison = candidate["simpleDirectionNoveltyEvidence"]["comparisons"][0]
        assert evaluation.status == "collision"
        assert evaluation.matching_proposal_id == "proposal_prior"
        assert comparison["campusRotationPassed"] is True
        assert comparison["totalStandaloneChangedCount"] == 0
        assert comparison["standaloneNoveltyPassed"] is False
        assert comparison["passedForStorage"] is False
    finally:
        connection.close()


def test_v3_new_campus_partial_is_saved_by_identity_without_becoming_ready() -> None:
    connection, service, portfolio_id = _service_with_history(
        _snapshot("prior", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    )
    try:
        candidate = _snapshot("candidate_partial", campuses=(3, 4), meals=(13, 14), parks=None)
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=_attempt((3, 4)),
            candidate_confirmation_passed=False,
        )
        evidence = candidate["simpleDirectionNoveltyEvidence"]
        assert evaluation.status == "distinct"
        assert evidence["repairablePartialAccepted"] is True
        assert evidence["readyNoveltyPassed"] is False
        assert evidence["passed"] is True
    finally:
        connection.close()


def test_v3_reports_incomplete_assignment_without_overloading_a_proposal_id() -> None:
    connection, service, portfolio_id = _service_with_history(
        _snapshot("prior", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    )
    try:
        candidate = _snapshot("candidate_partial", campuses=(3, 4), meals=(13, 14), parks=None)
        attempt = _attempt((3, 4))
        attempt["validatedOutcomes"][1]["selectedAmapId"] = _amap_id(99)
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior"],
            candidate_snapshot=candidate,
            frontier={},
            frontier_attempt=attempt,
            candidate_confirmation_passed=False,
        )
        evidence = candidate["simpleDirectionNoveltyEvidence"]
        assert evaluation.status == "incomplete"
        assert evaluation.matching_proposal_id is None
        assert evaluation.reason_code == "frontier_assignment_grounding_incomplete"
        assert evidence["campusAssignmentsGrounded"] is False
        assert evidence["reasonCode"] == "frontier_assignment_grounding_incomplete"
    finally:
        connection.close()


def test_v3_compares_against_repairable_partial_and_blocks_a_to_b_to_a() -> None:
    connection, service, portfolio_id = _service_with_history(
        _snapshot("ready_a", campuses=(1, 2), meals=(11, 12), parks=(21, 22))
    )
    try:
        partial_snapshot = _snapshot(
            "partial_b",
            campuses=(3, 4),
            meals=(13, 14),
            parks=None,
        )
        PlanPortfolioStore(connection).upsert_partial_preview(
            portfolio_id=portfolio_id,
            proposal_id="proposal_partial",
            choice_id="choice_partial",
            snapshot=partial_snapshot,
            brief={"briefId": "brief_partial", "title": "partial"},
            verifier={"compactRouteContractRequired": True, "confirmationPassed": False},
            score={"hardConstraintPassed": False},
            generation_lineage={"workflowMode": "simple_direction_v1"},
            status="blocked",
            evidence={"workflowMode": "simple_direction_v1"},
        )
        frontier = {
            "qualifiedEntityFrontier": [
                {
                    "state": "assigned_partial",
                    "assignedProposalId": "proposal_partial",
                }
            ]
        }
        candidate = _snapshot("candidate", campuses=(3, 4), meals=(15, 16), parks=(25, 26))
        evaluation = service._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=["proposal_prior", "proposal_partial"],
            candidate_snapshot=candidate,
            frontier=frontier,
            frontier_attempt=_attempt((3, 4)),
            candidate_confirmation_passed=True,
        )
        evidence = candidate["simpleDirectionNoveltyEvidence"]
        assert evaluation.status == "collision"
        assert evaluation.matching_proposal_id == "proposal_partial"
        assert [item["priorProposalId"] for item in evidence["comparisons"]] == [
            "proposal_prior",
            "proposal_partial",
        ]
        assert evidence["comparisons"][0]["campusRotationPassed"] is True
        assert evidence["comparisons"][1]["campusRotationPassed"] is False
        assert evidence["passed"] is False
    finally:
        connection.close()

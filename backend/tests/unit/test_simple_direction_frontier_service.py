from __future__ import annotations

import copy

import pytest

from backend.tests.unit.test_agent_service import clear_database, open_db
from src.api.schemas.agent import AgentInitialPlanOutput
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.creative_planning_models import PlanPortfolio
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService


def evidence(names: list[str]) -> dict:
    return {
        "schemaVersion": "entity-qualification-evidence-v1",
        "contentSha256": "e" * 64,
        "qualificationScheme": "scheme",
        "qualificationValue": "value",
        "entities": [{"canonicalName": name, "locality": "示例城"} for name in names],
    }


def outcome_for(
    assignment: dict,
    *,
    provider_outcome: str,
    selected_amap_id: str | None = None,
    reason_code: str | None = None,
) -> dict:
    return {
        "slotId": assignment["slotId"],
        "evidenceEntityFingerprint": assignment["evidenceEntityFingerprint"],
        "providerOutcome": provider_outcome,
        "selectedAmapId": selected_amap_id,
        "queryFingerprint": assignment["queryFingerprint"],
        "page": assignment["page"],
        "reasonCode": reason_code,
    }


def compatibility_scope(
    *,
    slot_id: str,
    day_seed_amap_id: str,
    fingerprint: str,
    day_number: int = 1,
    priority: int = 0,
) -> dict:
    return {
        "dayNumber": day_number,
        "slotId": slot_id,
        "daySeedAmapId": day_seed_amap_id,
        "queryScopeFingerprint": fingerprint,
        "centerRole": "predecessor",
        "queryRole": "adjacent_candidate_center",
        "queryText": "北京 当地特色餐厅",
        "priority": priority,
        "currentPartialCompletionSlot": True,
        "predecessorAmapId": day_seed_amap_id,
        "successorAmapId": "",
        "predecessorBeamRank": 0,
        "isActiveScope": True,
        "attemptedThisTurn": False,
        "providerOutcome": "not_called",
        "remainingReason": "not_executed",
    }


def test_frontier_advances_two_qualified_entities_per_ready_direction() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(list("ABCDEFGH")),
        locality="示例城",
        max_pages_per_query=3,
    )

    assignments: list[list[str]] = []
    for proposal_index in range(4):
        attempt = SimpleDirectionFrontierService.begin_attempt(
            frontier,
            campus_slots=[
                {"dayNumber": 1, "slotId": "day_1_campus"},
                {"dayNumber": 2, "slotId": "day_2_campus"},
            ],
        )
        assignments.append([item["canonicalName"] for item in attempt["campusAssignments"]])
        frontier = SimpleDirectionFrontierService.reconcile_attempt(
            frontier,
            attempt=attempt,
            outcomes=[
                outcome_for(
                    item,
                    provider_outcome="success",
                    selected_amap_id=f"B{proposal_index}{slot_index:08d}",
                )
                for slot_index, item in enumerate(attempt["campusAssignments"], start=1)
            ],
            proposal_id=f"proposal_{proposal_index}",
            disposition="used_ready",
        )

    assert assignments == [["A", "B"], ["C", "D"], ["E", "F"], ["G", "H"]]
    assert frontier["remainingQualifiedEntityCount"] == 0
    assert frontier["frontierStatus"] == "qualification_exhausted"


def test_single_new_anchor_fallback_is_explicit_and_does_not_fake_two_new_entities() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B", "C"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    first = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=first,
        outcomes=[
            outcome_for(first["campusAssignments"][0], provider_outcome="success", selected_amap_id="B000000001"),
            outcome_for(first["campusAssignments"][1], provider_outcome="success", selected_amap_id="B000000002"),
        ],
        proposal_id="proposal_1",
        disposition="used_ready",
    )

    second = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )

    assert second["singleNewAnchorFallback"] is True
    assert second["twoNewAnchorsRequired"] is False
    assert sum(item["isNewQualificationEntity"] is True for item in second["campusAssignments"]) == 1
    assert {item["canonicalName"] for item in second["campusAssignments"]} >= {"C"}


def test_provider_failure_does_not_mark_qualification_frontier_exhausted_or_advance_page() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B", "C", "D"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(
                attempt["campusAssignments"][0],
                provider_outcome="failure",
                reason_code="provider_unavailable",
            ),
            outcome_for(
                attempt["campusAssignments"][1],
                provider_outcome="failure",
                reason_code="provider_unavailable",
            ),
        ],
        proposal_id=None,
        disposition="provider_pending",
    )

    assert frontier["frontierStatus"] == "provider_pending"
    assert frontier["remainingQualifiedEntityCount"] == 2
    assert {item["state"] for item in frontier["qualifiedEntityFrontier"][:2]} == {"grounding_pending"}


def test_fail_closed_terminalization_consumes_claimed_page_without_provider_success() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_failed_claim",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A"]),
        locality="示例城",
        max_pages_per_query=2,
    )
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}],
    )

    terminalized = SimpleDirectionFrontierService.terminalize_attempt(
        frontier,
        attempt=attempt,
        reason_code="simple_direction_execution_failed_after_claim",
    )

    outcome = terminalized["outcomes"][0]
    next_attempt = SimpleDirectionFrontierService.begin_attempt(
        terminalized["frontier"],
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}],
    )
    assert outcome["providerOutcome"] == "rejected"
    assert outcome["providerCalled"] is False
    assert outcome["outcomeSource"] == "server_fail_closed_terminalization"
    assert next_attempt["campusAssignments"][0]["page"] == 2
    assert next_attempt["campusAssignments"][0]["queryFingerprint"] != attempt["campusAssignments"][0][
        "queryFingerprint"
    ]


def test_route_blocked_partial_keeps_other_qualified_entities_expandable() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B", "C", "D"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(attempt["campusAssignments"], start=1)
        ],
        proposal_id="proposal_partial",
        disposition="assigned_partial",
        blocking_layer="provider",
        reason_code="provider_route_matrix_incomplete",
    )

    assert frontier["terminalBlockingLayer"] == "provider"
    assert frontier["terminalReasonCode"] == "provider_route_matrix_incomplete"
    assert frontier["frontierStatus"] == "has_more"
    assert frontier["remainingQualifiedEntityCount"] == 2
    next_attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    assert [item["canonicalName"] for item in next_attempt["campusAssignments"]] == ["C", "D"]


def test_new_qualification_entities_cannot_claim_prior_direction_adjacent_scopes() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_new_entities_no_old_scopes",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B", "C", "D"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    first = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    old_scope = compatibility_scope(
        slot_id="day1_meal",
        day_seed_amap_id="B000000001",
        fingerprint="o" * 64,
    )
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=first,
        outcomes=[
            outcome_for(first["campusAssignments"][0], provider_outcome="success", selected_amap_id="B000000001"),
            outcome_for(first["campusAssignments"][1], provider_outcome="success", selected_amap_id="B000000002"),
        ],
        proposal_id="proposal_old_direction",
        disposition="assigned_partial",
        blocking_layer="poi",
        reason_code="pending_slot_candidate_missing",
        remaining_query_scopes=[old_scope],
    )

    second = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)

    assert [item["canonicalName"] for item in second["campusAssignments"]] == ["C", "D"]
    assert all(item["priorCanonicalAmapId"] is None for item in second["campusAssignments"])
    assert second.get("slotQueries", {}) == {}
    assert second.get("remainingQueryScopes", []) == []
    assert SimpleDirectionFrontierService.claim_slot_queries(
        frontier,
        allowed_day_seed_amap_ids=set(),
    ) == {}
    assert SimpleDirectionFrontierService.remaining_query_scopes(
        frontier,
        allowed_day_seed_amap_ids=set(),
    ) == []


def test_assigned_partial_retries_same_grounded_pair_while_exact_slot_page_remains() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_partial_slot_retry",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    attempt = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(attempt["campusAssignments"], start=1)
        ],
        proposal_id="proposal_partial_slot_retry",
        disposition="assigned_partial",
        blocking_layer="poi",
        reason_code="pending_slot_candidate_missing",
    )
    slot_query = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="day1_meal",
        day_seed_amap_id="B000000001",
        query_scope_fingerprint="s" * 64,
    )
    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=slot_query,
        provider_outcome="success",
        admitted_physical_groups=[],
        rejected_physical_groups=[],
    )

    assert frontier["frontierStatus"] == "has_more"
    assert frontier["remainingPoiPageCount"] == 2
    retry = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    assert [item["canonicalName"] for item in retry["campusAssignments"]] == ["A", "B"]
    assert {item["assignmentMode"] for item in retry["campusAssignments"]} == {"partial_retry"}


def test_assigned_partial_claims_next_unexecuted_exact_center_scope_for_same_slot() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_partial_center_queue",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=1,
    )
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    attempt = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    remaining_scopes = [
        {
            "dayNumber": 2,
            "slotId": "day2_park",
            "daySeedAmapId": "B000000002",
            "queryScopeFingerprint": "p" * 64,
            "centerRole": "predecessor",
            "queryRole": "adjacent_candidate_center",
            "queryText": "示例城 公园",
            "priority": 0,
            "currentPartialCompletionSlot": False,
            "predecessorAmapId": "B000000002",
            "successorAmapId": "",
            "predecessorBeamRank": 0,
        },
        *[
            {
                "dayNumber": 1,
                "slotId": "day1_meal",
                "daySeedAmapId": "B000000001",
                "queryScopeFingerprint": fingerprint * 64,
                "centerRole": center_role,
                "queryRole": "adjacent_candidate_center",
                "queryText": "示例城 午餐",
                "priority": priority,
                "currentPartialCompletionSlot": True,
                "predecessorAmapId": "B000000001",
                "successorAmapId": "B000000009",
                "predecessorBeamRank": 0,
            }
            for fingerprint, center_role, priority in (
                ("m", "midpoint", 1),
                ("r", "predecessor", 2),
                ("s", "successor", 3),
            )
        ],
    ]
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(attempt["campusAssignments"], start=1)
        ],
        proposal_id="proposal_partial_center_queue",
        disposition="assigned_partial",
        blocking_layer="poi",
        reason_code="pending_slot_candidate_missing",
        remaining_query_scopes=remaining_scopes,
    )

    retry = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    assert list(retry["slotQueries"])[0] == "day1_meal"
    midpoint_query = retry["slotQueries"]["day1_meal"]
    assert midpoint_query["queryScopeFingerprint"] == "m" * 64
    assert midpoint_query["centerRole"] == "midpoint"
    assert midpoint_query["page"] == 1

    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=midpoint_query,
        provider_outcome="success",
        admitted_physical_groups=[],
        rejected_physical_groups=[],
    )
    next_retry = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    assert next_retry["slotQueries"]["day1_meal"]["queryScopeFingerprint"] == "r" * 64
    assert next_retry["slotQueries"]["day1_meal"]["centerRole"] == "predecessor"
    assert next_retry["slotQueries"]["day1_meal"]["page"] == 1


def test_novelty_collision_consumes_attempt_but_keeps_real_remaining_frontier() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B", "C", "D"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(attempt["campusAssignments"][0], provider_outcome="success", selected_amap_id="B000000001"),
            outcome_for(attempt["campusAssignments"][1], provider_outcome="success", selected_amap_id="B000000002"),
        ],
        proposal_id=None,
        disposition="novelty_collision",
    )

    assert frontier["frontierStatus"] == "has_more"
    assert frontier["remainingQualifiedEntityCount"] == 2
    assert {item["reasonCode"] for item in frontier["qualifiedEntityFrontier"][:2]} == {"novelty_collision"}


def test_slot_page_identity_includes_anchor_scope_and_only_advances_on_provider_success() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    query = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="day_1_park",
        day_seed_amap_id="B000000001",
        query_scope_fingerprint="q" * 64,
    )
    assert query["page"] == 1
    assert query["offset"] > 0
    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=query,
        provider_outcome="failure",
        admitted_physical_groups=[],
        rejected_physical_groups=[],
    )
    assert (
        SimpleDirectionFrontierService.begin_slot_query(
            frontier,
            day_number=1,
            slot_id="day_1_park",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )["page"]
        == 1
    )
    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=query,
        provider_outcome="success",
        admitted_physical_groups=["B000000009"],
        rejected_physical_groups=[{"physicalGroupId": "B000000010", "reasonCode": "independence_pending"}],
    )
    assert (
        SimpleDirectionFrontierService.begin_slot_query(
            frontier,
            day_number=1,
            slot_id="day_1_park",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )["page"]
        == 2
    )
    for expected_page in (2, 3):
        query = SimpleDirectionFrontierService.begin_slot_query(
            frontier,
            day_number=1,
            slot_id="day_1_park",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )
        assert query["page"] == expected_page
        assert query["exhausted"] is False
        frontier = SimpleDirectionFrontierService.record_slot_query(
            frontier,
            query=query,
            provider_outcome="success",
            admitted_physical_groups=[],
            rejected_physical_groups=[],
        )
    exhausted = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="day_1_park",
        day_seed_amap_id="B000000001",
        query_scope_fingerprint="q" * 64,
    )
    assert exhausted["page"] == 4
    assert exhausted["exhausted"] is True


def test_authoritative_remaining_scope_queue_does_not_resurrect_completed_historical_page() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_authoritative_scope_queue",
        request_contract_fingerprint="r" * 64,
        evidence=evidence([]),
        locality="示例城",
        max_pages_per_query=3,
    )
    query = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="day1_meal",
        day_seed_amap_id="B000000001",
        query_scope_fingerprint="m" * 64,
    )
    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=query,
        provider_outcome="success",
        admitted_physical_groups=["B000000101"],
        rejected_physical_groups=[],
    )
    assert SimpleDirectionFrontierService.claim_slot_queries(frontier)["day1_meal"]["page"] == 2

    frontier["remainingQueryScopes"] = []
    frontier["remainingQueryScopesAuthoritative"] = True

    assert SimpleDirectionFrontierService.claim_slot_queries(frontier) == {}


def test_persisted_slot_page_advances_once_from_server_bound_day_seed() -> None:
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(["A", "B"])
    try:
        attempt = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="exec_slot_page",
            campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
            expected_request_contract_fingerprint=request_fingerprint,
        )
        campus_outcomes = [
            {
                "slotId": assignment["slotId"],
                "evidenceEntityFingerprint": assignment["evidenceEntityFingerprint"],
                "providerOutcome": "success",
                "selectedAmapId": f"B00000000{index}",
                "queryFingerprint": assignment["queryFingerprint"],
                "page": assignment["page"],
            }
            for index, assignment in enumerate(attempt["campusAssignments"], start=1)
        ]
        slot_query = SimpleDirectionFrontierService.begin_slot_query(
            attempt["slotFrontierSnapshot"],
            day_number=1,
            slot_id="d1_meal",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )
        first = store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="exec_slot_page",
            outcomes=campus_outcomes,
            slot_query_outcomes=[
                {
                    "query": slot_query,
                    "providerOutcome": "success",
                    "admittedPhysicalGroups": ["B000000101"],
                    "rejectedPhysicalGroups": [],
                }
            ],
            proposal_id="proposal_partial",
            disposition="assigned_partial",
            expected_request_contract_fingerprint=request_fingerprint,
        )
        next_query = SimpleDirectionFrontierService.begin_slot_query(
            first["frontier"],
            day_number=1,
            slot_id="d1_meal",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )
        assert next_query["page"] == 2

        replay = store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="exec_slot_page",
            outcomes=campus_outcomes,
            slot_query_outcomes=[
                {
                    "query": slot_query,
                    "providerOutcome": "success",
                    "admittedPhysicalGroups": ["B000000101"],
                    "rejectedPhysicalGroups": [],
                }
            ],
            proposal_id="proposal_partial",
            disposition="assigned_partial",
            expected_request_contract_fingerprint=request_fingerprint,
        )
        replay_query = SimpleDirectionFrontierService.begin_slot_query(
            replay["frontier"],
            day_number=1,
            slot_id="d1_meal",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )
        assert replay_query["page"] == 2
    finally:
        connection.close()


def test_persisted_settlement_advances_only_attempted_exact_scope_and_keeps_unexecuted_scope() -> None:
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(["A", "B"])
    try:
        slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
        attempt = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="exec_exact_scope_queue",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        campus_outcomes = [
            outcome_for(
                assignment,
                provider_outcome="success",
                selected_amap_id=f"B00000000{index}",
            )
            for index, assignment in enumerate(attempt["campusAssignments"], start=1)
        ]
        midpoint_scope = {
            "dayNumber": 1,
            "slotId": "day1_meal",
            "daySeedAmapId": "B000000001",
            "queryScopeFingerprint": "m" * 64,
            "centerRole": "midpoint",
            "queryRole": "adjacent_candidate_center",
            "queryText": "示例城 午餐",
            "priority": 1,
            "currentPartialCompletionSlot": True,
            "predecessorAmapId": "B000000001",
            "successorAmapId": "B000000009",
            "predecessorBeamRank": 0,
            "isActiveScope": True,
            "attemptedThisTurn": True,
            "providerOutcome": "success",
            "remainingReason": "no_candidate_selected",
        }
        predecessor_scope = {
            **midpoint_scope,
            "queryScopeFingerprint": "r" * 64,
            "centerRole": "predecessor",
            "priority": 2,
            "isActiveScope": False,
            "attemptedThisTurn": False,
            "providerOutcome": "not_called",
            "remainingReason": "not_executed",
        }
        midpoint_query = SimpleDirectionFrontierService.begin_slot_query(
            attempt["slotFrontierSnapshot"],
            day_number=1,
            slot_id="day1_meal",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="m" * 64,
        )
        settled = store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="exec_exact_scope_queue",
            outcomes=campus_outcomes,
            slot_query_outcomes=[
                {
                    "query": {**midpoint_scope, **midpoint_query},
                    "providerOutcome": "success",
                    "admittedPhysicalGroups": [],
                    "rejectedPhysicalGroups": [],
                }
            ],
            proposal_id="proposal_exact_scope_queue",
            disposition="assigned_partial",
            expected_request_contract_fingerprint=request_fingerprint,
            remaining_query_scopes=[midpoint_scope, predecessor_scope],
        )

        assert (
            SimpleDirectionFrontierService.begin_slot_query(
                settled["frontier"],
                day_number=1,
                slot_id="day1_meal",
                day_seed_amap_id="B000000001",
                query_scope_fingerprint="m" * 64,
            )["page"]
            == 2
        )
        assert (
            SimpleDirectionFrontierService.begin_slot_query(
                settled["frontier"],
                day_number=1,
                slot_id="day1_meal",
                day_seed_amap_id="B000000001",
                query_scope_fingerprint="r" * 64,
            )["page"]
            == 1
        )

        retry = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="exec_exact_scope_queue_retry",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        assert retry["slotQueries"]["day1_meal"]["queryScopeFingerprint"] == "m" * 64
        assert retry["slotQueries"]["day1_meal"]["page"] == 2
        assert any(item["queryScopeFingerprint"] == "r" * 64 for item in retry["remainingQueryScopes"])
    finally:
        connection.close()


def test_compatibility_claim_does_not_invent_attempted_exact_slot_scopes() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        request_context = {
            "requestIntentContract": copy.deepcopy(prepared["requestContract"]),
            "planningSelectionRootTurnId": prepared["planningRootId"],
            "rootPortfolioId": prepared["root"]["id"],
            "selectedAgentChoice": copy.deepcopy(prepared["selectedChoice"]),
            "fallbackChoiceExecutionClaim": {"id": prepared["executionId"], "status": "executing"},
            "_simpleDirectionGenerationAuthorized": True,
        }
        pipeline_context = {
            "city": "北京",
            "requestIntentContract": copy.deepcopy(prepared["requestContract"]),
        }
        initial_plan = AgentInitialPlanOutput.model_validate(
            {
                "reply": "",
                "mode": "plan",
                "daySlots": [
                    {
                        "slotId": "day1_campus",
                        "dayNumber": 1,
                        "startTime": "09:00",
                        "durationMinutes": 120,
                        "kind": "campus",
                        "rawNeed": "参观高校",
                        "routeAnchor": True,
                    },
                    {
                        "slotId": "day1_lunch",
                        "dayNumber": 1,
                        "startTime": "12:00",
                        "durationMinutes": 75,
                        "kind": "meal",
                        "rawNeed": "当地特色午餐",
                        "routeAnchor": True,
                    },
                    {
                        "slotId": "day1_night",
                        "dayNumber": 1,
                        "startTime": "19:00",
                        "durationMinutes": 90,
                        "kind": "night_view",
                        "rawNeed": "公共城市夜景空间",
                        "routeAnchor": True,
                    },
                ],
                "intentPools": [
                    {
                        "poolId": "campus_pool",
                        "rawNeed": "参观高校",
                        "city": "北京",
                        "intentType": "campus_visit",
                        "targetCount": 1,
                        "requirementLevel": "required",
                        "assignToSlots": ["day1_campus"],
                        "candidateHints": ["北京 高校"],
                    },
                    {
                        "poolId": "meal_pool",
                        "rawNeed": "当地特色午餐",
                        "city": "北京",
                        "intentType": "meal",
                        "targetCount": 1,
                        "requirementLevel": "optional",
                        "assignToSlots": ["day1_lunch"],
                        "candidateHints": ["北京 当地特色餐厅"],
                    },
                    {
                        "poolId": "night_pool",
                        "rawNeed": "公共城市夜景空间",
                        "city": "北京",
                        "intentType": "night_view",
                        "targetCount": 1,
                        "requirementLevel": "required",
                        "assignToSlots": ["day1_night"],
                        "candidateHints": ["北京 公共城市夜景空间"],
                    },
                ],
            }
        )

        prepared["agent"]._prepare_simple_open_direction_frontier(
            session_id=prepared["session"].session_id,
            user_turn_id=prepared["requestTurnId"],
            assistant_turn_id=prepared["resultAssistantTurnId"],
            city="北京",
            active_version_id=None,
            request_context=request_context,
            pipeline_context=pipeline_context,
            initial_plan=initial_plan,
            resolved_dates={
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-01",
                "dates": ["2026-10-01"],
            },
            tool_events=[],
        )

        attempt = pipeline_context["simpleDirectionCompatibilityAttempt"]
        assignment = pipeline_context["simpleDirectionFrontierAssignment"]
        assert attempt["schemaVersion"] == "simple-direction-compatibility-attempt-v2"
        assert attempt.get("slotQueries") in (None, {})
        assert assignment["slotQueries"] == {}
        assert assignment["slotFrontierSnapshot"] == {}
        assert assignment["remainingQueryScopes"] == []
    finally:
        connection.close()


def test_current_scope_attach_claims_each_late_slot_and_replays_same_scopes_idempotently() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection)
        initial = prepared["compatibilityAttempt"]
        assert initial["schemaVersion"] == "simple-direction-compatibility-attempt-v2"
        assert initial.get("slotQueries") in (None, {})

        lunch_scope = compatibility_scope(
            slot_id="day1_lunch",
            day_seed_amap_id="B000000001",
            fingerprint="l" * 64,
        )
        after_lunch = prepared["direction"].claim_compatibility_current_scopes(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            slot_query_scopes=[lunch_scope],
        )
        assert after_lunch["schemaVersion"] == "simple-direction-compatibility-attempt-v3"
        assert after_lunch["attemptFingerprint"] != initial["attemptFingerprint"]
        assert after_lunch["slotQueries"]["day1_lunch"]["daySeedAmapId"] == "B000000001"
        assert after_lunch["slotQueries"]["day1_lunch"]["page"] == 1
        assert after_lunch["slotFrontierSnapshot"]["slotFrontiers"] == {}

        night_scope = compatibility_scope(
            slot_id="day1_night",
            day_seed_amap_id="B000000001",
            fingerprint="n" * 64,
            priority=1,
        )
        after_night = prepared["direction"].claim_compatibility_current_scopes(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            slot_query_scopes=[night_scope],
        )
        assert after_night["attemptFingerprint"] != after_lunch["attemptFingerprint"]
        assert set(after_night["slotQueries"]) == {"day1_lunch", "day1_night"}
        assert all(item["page"] == 1 for item in after_night["slotQueries"].values())

        replay = prepared["direction"].claim_compatibility_current_scopes(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            slot_query_scopes=[night_scope, lunch_scope],
        )
        assert replay == after_night

        summary = PlanPortfolioStore(connection).summary(portfolio_id=prepared["root"]["id"])
        frozen = summary["simpleDirectionCompatibilityFrontier"]
        persisted = summary["simpleDirectionCompatibilityAttempts"][prepared["executionId"]]
        assert persisted == replay
        assert frozen["slotFrontierSnapshot"] == replay["slotFrontierSnapshot"]
        assert frozen["remainingQueryScopes"] == replay["remainingQueryScopes"]
    finally:
        connection.close()


def test_deferred_current_scope_claim_is_not_capped_by_prior_direction_count() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        store = PlanPortfolioStore(connection)
        summary = store.summary(portfolio_id=prepared["root"]["id"])
        summary["simpleDirectionCompatibilityAttempts"] = {
            "choice_exec_prior_1": {
                "status": "reconciled",
                "frontierStatus": "has_more",
                "updatedAt": "2026-08-30T17:04:00+00:00",
            },
            "choice_exec_prior_2": {
                "status": "reconciled",
                "frontierStatus": "has_more",
                "updatedAt": "2026-08-30T17:05:00+00:00",
            },
        }
        connection.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?",
            (store._dump(summary), prepared["root"]["id"]),
        )
        connection.commit()

        claimed = prepared["direction"].claim_compatibility_continuation(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            source_assistant_turn_id=prepared["sourceAssistantTurnId"],
            request_turn_id=prepared["requestTurnId"],
            choice_id=prepared["selectedChoice"]["choiceId"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            max_pages_per_query=3,
            defer_slot_scope_claim=True,
        )

        assert claimed["schemaVersion"] == "simple-direction-compatibility-attempt-v2"
        assert claimed["providerPage"] == 1
        assert claimed.get("slotQueries") in (None, {})

        current_scope = compatibility_scope(
            slot_id="day1_lunch",
            day_seed_amap_id="B000000009",
            fingerprint="c" * 64,
        )
        attached = prepared["direction"].claim_compatibility_current_scopes(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            slot_query_scopes=[current_scope],
        )

        assert attached["schemaVersion"] == "simple-direction-compatibility-attempt-v3"
        assert attached["providerPage"] == 1
        assert attached["slotQueries"]["day1_lunch"]["daySeedAmapId"] == "B000000009"
    finally:
        connection.close()


def test_current_scope_attach_rejects_different_exact_scope_for_bound_slot() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection)
        first_scope = compatibility_scope(
            slot_id="day1_lunch",
            day_seed_amap_id="B000000001",
            fingerprint="l" * 64,
        )
        first = prepared["direction"].claim_compatibility_current_scopes(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            request_contract_fingerprint=prepared["requestFingerprint"],
            slot_query_scopes=[first_scope],
        )

        with pytest.raises(ValueError, match="simple_direction_compatibility_slot_scope_conflict"):
            prepared["direction"].claim_compatibility_current_scopes(
                portfolio_id=prepared["root"]["id"],
                execution_id=prepared["executionId"],
                request_contract_fingerprint=prepared["requestFingerprint"],
                slot_query_scopes=[
                    compatibility_scope(
                        slot_id="day1_lunch",
                        day_seed_amap_id="B000000009",
                        fingerprint="x" * 64,
                    )
                ],
            )

        persisted = PlanPortfolioStore(connection).summary(portfolio_id=prepared["root"]["id"])[
            "simpleDirectionCompatibilityAttempts"
        ][prepared["executionId"]]
        assert persisted == first
    finally:
        connection.close()


def test_current_scope_attach_rejects_other_execution_identity() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection)
        with pytest.raises(ValueError, match="simple_direction_compatibility_attempt_missing"):
            prepared["direction"].claim_compatibility_current_scopes(
                portfolio_id=prepared["root"]["id"],
                execution_id="other_execution",
                request_contract_fingerprint=prepared["requestFingerprint"],
                slot_query_scopes=[
                    compatibility_scope(
                        slot_id="day1_lunch",
                        day_seed_amap_id="B000000001",
                        fingerprint="l" * 64,
                    )
                ],
            )

        original = PlanPortfolioStore(connection).summary(portfolio_id=prepared["root"]["id"])[
            "simpleDirectionCompatibilityAttempts"
        ][prepared["executionId"]]
        assert original == prepared["compatibilityAttempt"]
    finally:
        connection.close()


def test_compatibility_claim_accepts_multiple_scopes_per_slot_and_prioritizes_current_partial() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        scopes = [
            {
                "dayNumber": 1,
                "slotId": "day1_lunch",
                "daySeedAmapId": "B000000001",
                "queryScopeFingerprint": fingerprint * 64,
                "centerRole": center_role,
                "queryRole": "adjacent_candidate_center",
                "queryText": "北京 当地特色午餐",
                "priority": priority,
                "currentPartialCompletionSlot": current_partial,
                "predecessorAmapId": "B000000001",
                "successorAmapId": "B000000009",
                "predecessorBeamRank": 0,
            }
            for fingerprint, center_role, priority, current_partial in (
                ("x", "predecessor", 0, False),
                ("m", "midpoint", 1, True),
                ("r", "predecessor", 2, True),
                ("s", "successor", 3, True),
            )
        ]
        attempt = PlanPortfolioStore(connection).claim_simple_direction_compatibility_attempt(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            source_assistant_turn_id=prepared["selectedChoice"]["sourceAssistantTurnId"],
            request_turn_id=prepared["requestTurnId"],
            choice_id=prepared["selectedChoice"]["choiceId"],
            expected_request_contract_fingerprint=prepared["requestFingerprint"],
            max_pages_per_query=3,
            slot_query_scopes=scopes,
        )

        assert set(attempt["slotQueries"]) == {"day1_lunch"}
        assert attempt["slotQueries"]["day1_lunch"]["queryScopeFingerprint"] == "m" * 64
        assert attempt["slotQueries"]["day1_lunch"]["centerRole"] == "midpoint"
        assert attempt["slotQueries"]["day1_lunch"]["page"] == 1
        assert [item["queryScopeFingerprint"] for item in attempt["remainingQueryScopes"]] == [
            "m" * 64,
            "r" * 64,
            "s" * 64,
            "x" * 64,
        ]
    finally:
        connection.close()


def test_initial_compatibility_scope_observation_seeds_only_attempted_exact_page() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        active_scope = {
            "dayNumber": 1,
            "slotId": "day1_lunch",
            "daySeedAmapId": "B000000001",
            "queryScopeFingerprint": "m" * 64,
            "centerRole": "midpoint",
            "queryRole": "adjacent_candidate_center",
            "queryText": "北京 当地特色午餐",
            "priority": 1,
            "currentPartialCompletionSlot": True,
            "predecessorAmapId": "B000000001",
            "successorAmapId": "B000000009",
            "predecessorBeamRank": 0,
            "isActiveScope": True,
            "attemptedThisTurn": True,
            "providerOutcome": "success",
            "remainingReason": "no_candidate_selected",
        }
        pending_scope = {
            **active_scope,
            "queryScopeFingerprint": "r" * 64,
            "centerRole": "predecessor",
            "priority": 2,
            "isActiveScope": False,
            "attemptedThisTurn": False,
            "providerOutcome": "not_called",
            "remainingReason": "not_executed",
        }
        store = PlanPortfolioStore(connection)
        frozen = store.record_simple_direction_compatibility_query_scopes(
            portfolio_id=prepared["root"]["id"],
            expected_request_contract_fingerprint=prepared["requestFingerprint"],
            remaining_query_scopes=[active_scope, pending_scope],
        )
        attempt = store.claim_simple_direction_compatibility_attempt(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            source_assistant_turn_id=prepared["selectedChoice"]["sourceAssistantTurnId"],
            request_turn_id=prepared["requestTurnId"],
            choice_id=prepared["selectedChoice"]["choiceId"],
            expected_request_contract_fingerprint=prepared["requestFingerprint"],
        )

        assert frozen["frontierStatus"] == "has_more"
        assert attempt["slotQueries"]["day1_lunch"]["queryScopeFingerprint"] == "m" * 64
        assert attempt["slotQueries"]["day1_lunch"]["page"] == 2
        pending_query = SimpleDirectionFrontierService.begin_slot_query(
            attempt["slotFrontierSnapshot"],
            day_number=1,
            slot_id="day1_lunch",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="r" * 64,
        )
        assert pending_query["page"] == 1
    finally:
        connection.close()


def test_compatibility_reconcile_without_query_candidate_or_route_delta_is_no_progress() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection)
        settled = PlanPortfolioStore(connection).reconcile_simple_direction_compatibility_attempt(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            result_assistant_turn_id=prepared["resultAssistantTurnId"],
            proposal_id=None,
            proposal_delta=0,
            disposition="no_material_novelty",
            expected_request_contract_fingerprint=prepared["requestFingerprint"],
            frontier_status="has_more",
            reason_code="candidate_collision_frontier_remaining",
            slot_query_outcomes=[],
            route_progress={
                "routeCoverageComplete": True,
                "verifiedPairCount": 1,
                "verifiedPairs": [{"from": "B0001", "to": "B0002"}],
                "routeProviderAttemptCount": 0,
                "topologyCandidateAttemptCount": 0,
                "unverifiedTopologyCombinationCount": 0,
                "routeFeasibilityExhausted": True,
            },
        )

        assert settled["status"] == "no_progress"
        assert settled["frontierStatus"] == "poi_exhausted"
        assert settled["reasonCode"] == "no_progress_no_query_candidate_or_route_delta"
        assert settled["progress"] == {
            "madeProgress": False,
            "queryProgress": False,
            "candidateProgress": False,
            "routeProgress": False,
        }
        replay = PlanPortfolioStore(connection).claim_simple_direction_compatibility_attempt(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            source_assistant_turn_id=prepared["selectedChoice"]["sourceAssistantTurnId"],
            request_turn_id=prepared["requestTurnId"],
            choice_id=prepared["selectedChoice"]["choiceId"],
            expected_request_contract_fingerprint=prepared["requestFingerprint"],
        )
        assert replay["status"] == "no_progress"
        assert replay["claimAttempt"] == 1
    finally:
        connection.close()


def test_compatibility_exact_slot_outcome_replay_is_idempotent_before_cursor_advance() -> None:
    from backend.tests.unit.test_simple_direction_execution_evidence_service import (
        _prepared_compatibility_execution,
    )

    clear_database()
    connection = open_db()
    try:
        prepared = _prepared_compatibility_execution(connection, claim_compatibility=False)
        scope = {
            "dayNumber": 1,
            "slotId": "day1_lunch",
            "daySeedAmapId": "B000000001",
            "queryScopeFingerprint": "m" * 64,
            "centerRole": "midpoint",
            "queryRole": "adjacent_candidate_center",
            "queryText": "北京 当地特色午餐",
            "priority": 1,
            "currentPartialCompletionSlot": True,
            "predecessorAmapId": "B000000001",
            "successorAmapId": "B000000009",
            "predecessorBeamRank": 0,
        }
        store = PlanPortfolioStore(connection)
        attempt = store.claim_simple_direction_compatibility_attempt(
            portfolio_id=prepared["root"]["id"],
            execution_id=prepared["executionId"],
            source_assistant_turn_id=prepared["selectedChoice"]["sourceAssistantTurnId"],
            request_turn_id=prepared["requestTurnId"],
            choice_id=prepared["selectedChoice"]["choiceId"],
            expected_request_contract_fingerprint=prepared["requestFingerprint"],
            max_pages_per_query=3,
            slot_query_scopes=[scope],
        )
        query = copy.deepcopy(attempt["slotQueries"]["day1_lunch"])
        reconcile_args = {
            "portfolio_id": prepared["root"]["id"],
            "execution_id": prepared["executionId"],
            "result_assistant_turn_id": prepared["resultAssistantTurnId"],
            "proposal_id": None,
            "proposal_delta": 0,
            "disposition": "no_material_novelty",
            "expected_request_contract_fingerprint": prepared["requestFingerprint"],
            "frontier_status": "has_more",
            "reason_code": "candidate_collision_frontier_remaining",
            "slot_query_outcomes": [
                {
                    "query": query,
                    "providerOutcome": "success",
                    "admittedPhysicalGroups": [],
                    "rejectedPhysicalGroups": [],
                }
            ],
            "route_progress": {
                "topologyCandidateAttemptCount": 0,
                "unverifiedTopologyCombinationCount": 0,
                "routeFeasibilityExhausted": True,
            },
        }

        first = store.reconcile_simple_direction_compatibility_attempt(**reconcile_args)
        replay = store.reconcile_simple_direction_compatibility_attempt(**reconcile_args)

        assert first["status"] == "reconciled"
        assert replay == first
        assert replay["progress"]["queryProgress"] is True
    finally:
        connection.close()


def test_validated_controller_fallback_preserves_explicit_soft_meal_route_grounding() -> None:
    clear_database()
    connection = open_db()
    try:
        service = AgentService(connection)
        pipeline_context = {
            "serverExecutionProfile": "simple_open_v1",
            "selectedCity": "北京",
            "effectiveUserMessage": "参观高校并体验当地特色午餐",
            "agentDecisionState": {
                "source": "controller",
                "accepted": True,
                "primaryAction": "draft_itinerary",
            },
            "planningDirective": {
                "type": "draft_itinerary",
                "optionalExperienceBudget": 1,
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "requiredGoalIds": ["goal_campus"],
                        "optionalGoalIds": ["goal_meal"],
                        "requiredGoalCounts": {"goal_campus": 1},
                        "maxRouteAnchors": 3,
                    }
                ],
            },
            "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01"]},
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_campus",
                        "intentType": "campus_visit",
                        "requirementLevel": "required",
                        "requiredMin": 1,
                        "allowedDayNumbers": [1],
                    },
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "requirementLevel": "soft_experience",
                        "allowedDayNumbers": [1],
                    },
                ]
            },
            "mealGroundingPolicy": {
                "explicitFoodExperience": True,
                "mealSlots": [
                    {
                        "mealLabel": "lunch",
                        "routeAnchor": True,
                        "rawNeed": "午餐 当地特色美食",
                    }
                ],
                "reason": "explicit_food_experience",
            },
        }

        fallback = service._validated_controller_draft_slot_fallback(
            pipeline_context,
            TimeoutError("controller day-slot provider timed out"),
        )

        assert fallback is not None
        meal_slot = next(item for item in fallback.day_slots if item.kind == "meal")
        meal_pool = next(item for item in fallback.intent_pools if item.intent_type == "meal")
        assert meal_slot.route_anchor is True
        assert meal_pool.requirement_level == "optional"
        assert meal_pool.goal_id is None
        assert meal_pool.soft_goal_id == "goal_meal"
        assert meal_pool.route_preference["requiresRouteEdge"] is True
        assert meal_pool.route_preference["routeAnchorDecisionSource"] == "meal_grounding_policy"
    finally:
        connection.close()


def test_materialized_route_stop_serializes_requires_route_edge_from_real_plan_path() -> None:
    from datetime import datetime, timezone

    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse

    clear_database()
    connection = open_db()
    try:
        service = AgentService(connection)

        class MaterializedNightProvider:
            @staticmethod
            def search(city, *, keyword, category, limit):
                del category, limit
                return MapPoiSearchResponse(
                    city=city,
                    keyword=keyword,
                    category="all",
                    providerName="amap-place-search",
                    queriedAt=datetime.now(timezone.utc),
                    pois=[
                        MapPoiResponse(
                            id="B000ROUTE01",
                            name="示例湖",
                            city=city,
                            district="示例区",
                            category="风景名胜",
                            type="风景名胜;风景名胜;城市景观",
                            address="示例路 1 号",
                            longitude=116.31,
                            latitude=39.99,
                            source="amap-place-search",
                            sourceNote="deterministic-provider-shape",
                            confidence=0.99,
                        )
                    ],
                )

        class AcceptingSemanticPolicy:
            @staticmethod
            def evaluate(*_args, **_kwargs):
                class Decision:
                    passed = True

                return Decision()

        initial_plan = AgentInitialPlanOutput.model_validate(
            {
                "reply": "",
                "mode": "day_slots",
                "daySlots": [
                    {
                        "slotId": "day1_night_density_optional",
                        "dayNumber": 1,
                        "startTime": "09:00",
                        "durationMinutes": 120,
                        "kind": "night_view",
                        "rawNeed": "示例湖夜景",
                        "routeAnchor": False,
                    }
                ],
                "intentPools": [
                    {
                        "poolId": "night_pool",
                        "rawNeed": "示例湖夜景",
                        "city": "示例城",
                        "intentType": "night_view",
                        "targetCount": 1,
                        "requirementLevel": "optional",
                        "assignToSlots": ["day1_night_density_optional"],
                        "candidateHints": ["示例湖夜景"],
                    }
                ],
            }
        )
        service.simple_open_itinerary_executor.map_poi_service = MaterializedNightProvider()
        service.simple_open_itinerary_executor.intent_candidate_semantic_policy = AcceptingSemanticPolicy()
        plans = service._simple_open_persistable_segment_plans(
            initial_plan,
            city="示例城",
            transport_mode="walk",
            tool_events=[],
            pipeline_context={},
        )

        assert len(plans) == 1
        assert plans[0].selected_poi is not None
        assert plans[0].route_anchor is False
        assert plans[0].requires_route_edge is True

        snapshot = service._snapshot_from_persistable_segment_plans(
            {"active_plan_id": "plan_route_stop", "city": "示例城"},
            initial_plan,
            plans,
            {},
        )
        semantic = snapshot["days"][0]["segments"][0]["semanticMetadata"]
        assert semantic["routeAnchor"] is False
        assert semantic["requiresRouteEdge"] is True
    finally:
        connection.close()


@pytest.mark.parametrize("mixed_batch", [False, True])
def test_offer_direction_treats_selected_identity_missing_from_snapshot_as_provider_pending(
    monkeypatch,
    mixed_batch: bool,
) -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    connection = open_db()
    try:
        session = ConversationService(connection).create_session("示例城", "provider schema pending")
        monkeypatch.setattr(
            EntityQualificationEvidenceService,
            "qualified_entities",
            classmethod(lambda _cls, **_kwargs: evidence(["A", "B"])),
        )
        route_decision = RouteInsertionScorer.build_route_decision_contract(
            source="request_intent_contract",
            provenance={"transportMode": "transit", "requestSemanticsPresent": True},
            detour_tolerance={"maxGeneralizedCostDelta": 10.0, "maxDetourRatio": 0.2},
            mobility_profile={
                "source": "explicit_request_mobility_semantics",
                "walkingPenaltyMinutesPerKm": 1.8,
                "transferPenaltyMinutes": 6.0,
                "waitTimeMultiplier": 1.0,
                "riskPenaltyMultiplier": 1.0,
            },
        )
        assert route_decision is not None
        request_fingerprint = "r" * 64
        request_contract = {
            "routeDecisionContract": {
                "schemaVersion": "route-decision-contract-v1",
                "status": "ready",
                "missingFields": [],
                **route_decision,
            },
            "entityQualificationConstraint": {
                "qualificationScheme": "scheme",
                "qualificationValue": "value",
            },
        }
        service = SimpleOpenDirectionService(connection)
        root = service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_assistant_turn_id="turn_assistant_initial",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            request_contract=request_contract,
            locality="示例城",
            max_pages_per_query=3,
        )
        attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_execution_1",
            campus_slots=[
                {"dayNumber": 1, "slotId": "d1"},
                {"dayNumber": 2, "slotId": "d2"},
            ],
            request_contract_fingerprint=request_fingerprint,
        )
        outcomes = [
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(attempt["campusAssignments"], start=1)
        ]
        if mixed_batch:
            outcomes[0] = outcome_for(
                attempt["campusAssignments"][0],
                provider_outcome="rejected",
                reason_code="campus_assignment_candidate_rejected",
            )

        response = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_offer_user",
            source_assistant_turn_id="turn_offer_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            snapshot={"title": "缺失校园身份的候选", "days": [], "routeEvidence": []},
            request_contract=request_contract,
            frontier_execution_id="choice_execution_1",
            frontier_outcomes=outcomes,
        )

        refreshed = service.root_for_planning_root(
            session_id=session.session_id,
            planning_root_id="turn_root",
        )
        proposal_count = connection.execute(
            "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?",
            (root["id"],),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

        assert response["status"] == "provider_pending"
        assert response["reasonCode"] == "simple_direction_frontier_provider_schema_pending"
        assert proposal_count == 0
        assert version_count == 0
        assert refreshed is not None
        assert refreshed["simpleDirectionFrontier"]["frontierStatus"] == "provider_pending"
        assert refreshed["simpleDirectionFrontier"]["terminalBlockingLayer"] == "provider"
        assert refreshed["simpleDirectionFrontier"]["terminalReasonCode"] == (
            "simple_direction_frontier_selected_identity_not_in_snapshot"
        )
    finally:
        connection.close()


def _persisted_frontier_store(names: list[str]):
    connection = open_db()
    session = ConversationService(connection).create_session("示例城", "frontier persistence")
    store = PlanPortfolioStore(connection)
    portfolio_id = "portfolio_frontier_test"
    root_id = "turn_frontier_root"
    request_fingerprint = "r" * 64
    store.create(
        PlanPortfolio(
            portfolioId=portfolio_id,
            sessionId=session.session_id,
            sourceUserTurnId=root_id,
            sourceAssistantTurnId="turn_frontier_assistant",
            expectedBaseVersionId=None,
            sourceObservationFingerprint="o" * 64,
            requestContractFingerprint=request_fingerprint,
            status="awaiting_selection",
        ),
        [],
    )
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id=root_id,
        request_contract_fingerprint=request_fingerprint,
        evidence=evidence(names),
        locality="示例城",
        max_pages_per_query=3,
    )
    store.initialize_simple_direction_frontier(
        portfolio_id=portfolio_id,
        frontier=frontier,
        expected_request_contract_fingerprint=request_fingerprint,
    )
    return connection, store, portfolio_id, request_fingerprint


def test_persisted_frontier_claim_replay_and_next_execution_rotate_entities() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(list("ABCDEF"))
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        first = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_1",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        replay = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_1",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        assert replay == first
        assert [item["canonicalName"] for item in first["campusAssignments"]] == ["A", "B"]
        store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_1",
            outcomes=[
                {
                    "slotId": item["slotId"],
                    "evidenceEntityFingerprint": item["evidenceEntityFingerprint"],
                    "providerOutcome": "success",
                    "selectedAmapId": f"B00000000{index}",
                    "queryFingerprint": item["queryFingerprint"],
                    "page": item["page"],
                }
                for index, item in enumerate(first["campusAssignments"], start=1)
            ],
            proposal_id="proposal_1",
            disposition="used_ready",
            expected_request_contract_fingerprint=request_fingerprint,
        )
        persisted_attempt = store.summary(portfolio_id=portfolio_id)["simpleDirectionFrontierAttempts"]["choice_exec_1"]
        assert persisted_attempt["status"] == "reconciled"
        assert [item["queryFingerprint"] for item in persisted_attempt["outcomes"]] == [
            item["queryFingerprint"] for item in first["campusAssignments"]
        ]
        second = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_2",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        assert [item["canonicalName"] for item in second["campusAssignments"]] == ["C", "D"]
        summary = store.simple_direction_comparison_summary(portfolio_id=portfolio_id)
        assert summary["remainingQualifiedEntityCount"] == 4
        assert summary["frontierStatus"] == "has_more"
        assert summary["exploredQualifiedEntityCount"] == 2
        assert summary["attemptedPoiPageCount"] == 2
    finally:
        connection.close()


def test_persisted_provider_failure_keeps_exact_assignment_and_blocks_parallel_cursor() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(list("ABCD"))
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        claimed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_provider",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_provider",
            outcomes=[
                {
                    "slotId": item["slotId"],
                    "evidenceEntityFingerprint": item["evidenceEntityFingerprint"],
                    "providerOutcome": "failure",
                    "queryFingerprint": item["queryFingerprint"],
                    "page": item["page"],
                    "reasonCode": "provider_unavailable",
                }
                for item in claimed["campusAssignments"]
            ],
            proposal_id=None,
            disposition="provider_pending",
            expected_request_contract_fingerprint=request_fingerprint,
            reason_code="provider_unavailable",
        )
        summary = store.simple_direction_comparison_summary(portfolio_id=portfolio_id)
        assert summary["frontierStatus"] == "provider_pending"
        assert summary["remainingQualifiedEntityCount"] == 4
        assert summary["attemptedPoiPageCount"] == 0
        with pytest.raises(ValueError, match="provider_recovery_required"):
            store.claim_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_other",
                campus_slots=slots,
                expected_request_contract_fingerprint=request_fingerprint,
            )
        resumed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_provider",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        assert resumed == claimed
    finally:
        connection.close()


def test_compact_route_confirmation_is_authoritative_for_comparison_summary() -> None:
    clear_database()
    connection, store, portfolio_id, _request_fingerprint = _persisted_frontier_store(list("ABCD"))
    try:
        for proposal_id, status, confirmation_passed in (
            ("proposal_ready", "adoption_ready", True),
            ("proposal_blocked", "blocked", False),
        ):
            store.upsert_partial_preview(
                portfolio_id=portfolio_id,
                proposal_id=proposal_id,
                choice_id=f"choice_{proposal_id}",
                snapshot={
                    "schemaVersion": "itinerary-snapshot-v1",
                    "title": proposal_id,
                    "days": [],
                },
                brief={"briefId": f"brief_{proposal_id}", "title": proposal_id},
                verifier={
                    "compactRouteContractRequired": True,
                    "confirmationPassed": confirmation_passed,
                },
                score={"hardConstraintPassed": confirmation_passed},
                generation_lineage={"workflowMode": "simple_direction_v1"},
                status=status,
                evidence={"workflowMode": "simple_direction_v1"},
            )

        summary = store.simple_direction_comparison_summary(portfolio_id=portfolio_id)
        assert summary["adoptionReadyCount"] == 1
        assert summary["repairablePartialCount"] == 1
        counts = store.classification_counts(portfolio_id=portfolio_id)
        assert counts["verifiedComparisonProposalCount"] == 1
        assert counts["adoptionReadyProposalCount"] == 1
        assert counts["partialComparisonProposalCount"] == 1
    finally:
        connection.close()


def test_rejected_entity_resumes_next_page_before_advancing_to_next_entity_pair() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(list("ABCD")),
        locality="示例城",
        max_pages_per_query=3,
    )
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    first = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=first,
        outcomes=[outcome_for(item, provider_outcome="rejected") for item in first["campusAssignments"]],
        proposal_id=None,
        disposition="assigned_partial",
    )
    second = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    assert [item["canonicalName"] for item in second["campusAssignments"]] == ["A", "B"]
    assert {item["page"] for item in second["campusAssignments"]} == {2}


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda outcome: outcome.pop("providerOutcome"), "provider_outcome_invalid"),
        (lambda outcome: outcome.update(providerOutcome="unknown"), "provider_outcome_invalid"),
        (
            lambda outcome: outcome.update(providerOutcome="success", selectedAmapId=None),
            "success_identity_missing",
        ),
        (
            lambda outcome: outcome.update(providerOutcome="success", selectedAmapId="not-an-amap-id"),
            "success_identity_missing",
        ),
    ],
)
def test_malformed_provider_outcome_fails_before_cursor_mutation(mutate, reason: str) -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    original = copy.deepcopy(frontier)
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    outcomes = [
        outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
        for index, item in enumerate(attempt["campusAssignments"], start=1)
    ]
    mutate(outcomes[0])

    with pytest.raises(ValueError, match=reason):
        SimpleDirectionFrontierService.reconcile_attempt(
            frontier,
            attempt=attempt,
            outcomes=outcomes,
            proposal_id="proposal_invalid",
            disposition="used_ready",
        )

    assert frontier == original
    retry = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    assert [item["canonicalName"] for item in retry["campusAssignments"]] == ["A", "B"]
    assert {item["page"] for item in retry["campusAssignments"]} == {1}


def test_non_object_provider_outcome_fails_before_cursor_mutation() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    original = copy.deepcopy(frontier)
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    outcomes: list[object] = [
        outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
        for index, item in enumerate(attempt["campusAssignments"], start=1)
    ]
    outcomes.append("unexpected-provider-payload")

    with pytest.raises(ValueError, match="outcomes_invalid"):
        SimpleDirectionFrontierService.reconcile_attempt(
            frontier,
            attempt=attempt,
            outcomes=outcomes,
            proposal_id="proposal_invalid",
            disposition="used_ready",
        )

    assert frontier == original


def test_success_reuse_must_preserve_frozen_canonical_amap_identity() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B", "C"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    first = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=first,
        outcomes=[
            outcome_for(first["campusAssignments"][0], provider_outcome="success", selected_amap_id="B000000001"),
            outcome_for(first["campusAssignments"][1], provider_outcome="success", selected_amap_id="B000000002"),
        ],
        proposal_id="proposal_1",
        disposition="used_ready",
    )
    second = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    reused = next(item for item in second["campusAssignments"] if item["isNewQualificationEntity"] is False)
    outcomes = [
        outcome_for(
            item,
            provider_outcome="success",
            selected_amap_id=("B000000099" if item is reused else "B000000003"),
        )
        for item in second["campusAssignments"]
    ]

    with pytest.raises(ValueError, match="reused_identity_mismatch"):
        SimpleDirectionFrontierService.reconcile_attempt(
            frontier,
            attempt=second,
            outcomes=outcomes,
            proposal_id="proposal_2",
            disposition="used_ready",
        )


def test_novelty_collision_reuses_grounded_pair_while_slot_pages_remain() -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    attempt = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(attempt["campusAssignments"][0], provider_outcome="success", selected_amap_id="B000000001"),
            outcome_for(attempt["campusAssignments"][1], provider_outcome="success", selected_amap_id="B000000002"),
        ],
        proposal_id=None,
        disposition="novelty_collision",
        blocking_layer="poi",
        reason_code="novelty_collision",
    )
    assert frontier["frontierStatus"] == "poi_exhausted"

    slot_query = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="d1_park",
        day_seed_amap_id="B000000001",
        query_scope_fingerprint="q" * 64,
    )
    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=slot_query,
        provider_outcome="success",
        admitted_physical_groups=[],
        rejected_physical_groups=[{"physicalGroupId": "B000000101", "reasonCode": "independence_pending"}],
    )
    assert frontier["frontierStatus"] == "has_more"
    assert frontier["remainingQualifiedEntityCount"] == 0
    assert frontier["remainingPoiPageCount"] == 2

    retry = SimpleDirectionFrontierService.begin_attempt(frontier, campus_slots=slots)
    assert [item["canonicalName"] for item in retry["campusAssignments"]] == ["A", "B"]
    assert {item["assignmentMode"] for item in retry["campusAssignments"]} == {"collision_retry"}
    assert [item["priorCanonicalAmapId"] for item in retry["campusAssignments"]] == [
        "B000000001",
        "B000000002",
    ]

    for expected_page in (2, 3):
        slot_query = SimpleDirectionFrontierService.begin_slot_query(
            frontier,
            day_number=1,
            slot_id="d1_park",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )
        assert slot_query["page"] == expected_page
        frontier = SimpleDirectionFrontierService.record_slot_query(
            frontier,
            query=slot_query,
            provider_outcome="success",
            admitted_physical_groups=[],
            rejected_physical_groups=[],
        )
    assert frontier["remainingPoiPageCount"] == 0
    assert frontier["frontierStatus"] == "poi_exhausted"


@pytest.mark.parametrize(
    ("blocking_layer", "expected_status"),
    [
        ("poi", "poi_exhausted"),
        ("route", "route_feasible_exhausted"),
        ("qualification", "qualification_exhausted"),
    ],
)
def test_terminal_frontier_status_reports_actual_blocking_layer(
    blocking_layer: str,
    expected_status: str,
) -> None:
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_1",
        request_contract_fingerprint="r" * 64,
        evidence=evidence(["A", "B"]),
        locality="示例城",
        max_pages_per_query=3,
    )
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    frontier = SimpleDirectionFrontierService.reconcile_attempt(
        frontier,
        attempt=attempt,
        outcomes=[
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(attempt["campusAssignments"], start=1)
        ],
        proposal_id="proposal_partial",
        disposition="assigned_partial",
        blocking_layer=blocking_layer,
        reason_code=f"{blocking_layer}_frontier_exhausted",
    )
    assert frontier["frontierStatus"] == expected_status
    assert frontier["terminalBlockingLayer"] == blocking_layer
    assert frontier["terminalReasonCode"] == f"{blocking_layer}_frontier_exhausted"


def test_different_execution_cannot_claim_while_frontier_attempt_is_active() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(list("ABCD"))
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        claimed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_active",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        assert (
            store.claim_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_active",
                campus_slots=slots,
                expected_request_contract_fingerprint=request_fingerprint,
            )
            == claimed
        )
        with pytest.raises(ValueError, match="claim_in_progress"):
            store.claim_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_parallel",
                campus_slots=slots,
                expected_request_contract_fingerprint=request_fingerprint,
            )
    finally:
        connection.close()


def test_persisted_malformed_outcome_keeps_claim_and_frontier_unchanged() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(list("ABCD"))
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        claimed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_invalid",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        invalid_outcomes = [
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(claimed["campusAssignments"], start=1)
        ]
        invalid_outcomes[0].pop("providerOutcome")
        with pytest.raises(ValueError, match="provider_outcome_invalid"):
            store.reconcile_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_invalid",
                outcomes=invalid_outcomes,
                proposal_id="proposal_invalid",
                disposition="used_ready",
                expected_request_contract_fingerprint=request_fingerprint,
            )

        summary = store.summary(portfolio_id=portfolio_id)
        persisted_frontier = summary["simpleDirectionFrontier"]
        assert persisted_frontier["remainingQualifiedEntityCount"] == 4
        assert {item["state"] for item in persisted_frontier["qualifiedEntityFrontier"]} == {"untried"}
        assert summary["simpleDirectionFrontierAttempts"]["choice_exec_invalid"]["status"] == "claimed"
        assert (
            store.claim_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_invalid",
                campus_slots=slots,
                expected_request_contract_fingerprint=request_fingerprint,
            )
            == claimed
        )
    finally:
        connection.close()


def test_persisted_non_object_slot_outcome_keeps_claim_and_frontier_unchanged() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(list("ABCD"))
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        claimed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_invalid_slot",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        valid_outcomes = [
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(claimed["campusAssignments"], start=1)
        ]
        with pytest.raises(ValueError, match="slot_frontier_outcomes_invalid"):
            store.reconcile_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_invalid_slot",
                outcomes=valid_outcomes,
                slot_query_outcomes=["unexpected-provider-payload"],
                proposal_id="proposal_invalid",
                disposition="used_ready",
                expected_request_contract_fingerprint=request_fingerprint,
            )

        summary = store.summary(portfolio_id=portfolio_id)
        persisted_frontier = summary["simpleDirectionFrontier"]
        assert persisted_frontier["remainingQualifiedEntityCount"] == 4
        assert {item["state"] for item in persisted_frontier["qualifiedEntityFrontier"]} == {"untried"}
        assert summary["simpleDirectionFrontierAttempts"]["choice_exec_invalid_slot"]["status"] == "claimed"
    finally:
        connection.close()


def test_rejected_campus_attempt_rejects_dependent_seed_but_reconciles_without_slot_queries() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(["A", "B"])
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        claimed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_rejected_campus",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        rejected_outcomes = [
            outcome_for(
                item,
                provider_outcome="rejected",
                reason_code="campus_assignment_candidate_rejected",
            )
            for item in claimed["campusAssignments"]
        ]
        invalid_query = SimpleDirectionFrontierService.begin_slot_query(
            claimed["slotFrontierSnapshot"],
            day_number=1,
            slot_id="day1_lunch",
            day_seed_amap_id="B000000099",
            query_scope_fingerprint="9" * 64,
        )
        with pytest.raises(ValueError, match="simple_direction_slot_frontier_day_seed_mismatch"):
            store.reconcile_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_rejected_campus",
                outcomes=rejected_outcomes,
                slot_query_outcomes=[
                    {
                        "query": invalid_query,
                        "providerOutcome": "success",
                        "admittedPhysicalGroups": [],
                        "rejectedPhysicalGroups": [],
                        "selectedAmapId": None,
                        "reasonCode": None,
                    }
                ],
                proposal_id="proposal_rejected_campus",
                disposition="assigned_partial",
                expected_request_contract_fingerprint=request_fingerprint,
            )

        settled = store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_rejected_campus",
            outcomes=rejected_outcomes,
            slot_query_outcomes=[],
            proposal_id="proposal_rejected_campus",
            disposition="assigned_partial",
            expected_request_contract_fingerprint=request_fingerprint,
        )

        assert settled["claim"]["status"] == "reconciled"
        assert settled["frontier"]["slotFrontiers"] == {}
        assert all(
            item["state"] == "untried" and item["reasonCode"] == "poi_page_remaining"
            for item in settled["frontier"]["qualifiedEntityFrontier"]
        )
    finally:
        connection.close()


def test_persisted_summary_exposes_route_frontier_exhaustion_without_relabeling() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(["A", "B"])
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        claimed = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_route_blocked",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_route_blocked",
            outcomes=[
                outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
                for index, item in enumerate(claimed["campusAssignments"], start=1)
            ],
            proposal_id="proposal_route_blocked",
            disposition="assigned_partial",
            expected_request_contract_fingerprint=request_fingerprint,
            reason_code="route_coverage_incomplete",
            blocking_layer="route",
        )

        comparison = store.simple_direction_comparison_summary(portfolio_id=portfolio_id)
        assert comparison["frontierStatus"] == "route_feasible_exhausted"
        assert comparison["blockingLayer"] == "route"
        assert comparison["lastOutcomeReason"] == "route_coverage_incomplete"
        assert comparison["remainingQualifiedEntityCount"] == 0
        assert comparison["remainingPoiPageCount"] == 0
        with pytest.raises(ValueError, match="frontier_not_available"):
            store.claim_simple_direction_frontier_attempt(
                portfolio_id=portfolio_id,
                execution_id="choice_exec_blind_retry",
                campus_slots=slots,
                expected_request_contract_fingerprint=request_fingerprint,
            )
    finally:
        connection.close()


def test_persisted_collision_reuses_same_grounded_pair_on_next_untried_slot_page() -> None:
    clear_database()
    connection, store, portfolio_id, request_fingerprint = _persisted_frontier_store(["A", "B"])
    slots = [{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}]
    try:
        first = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_collision_1",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        campus_outcomes = [
            outcome_for(item, provider_outcome="success", selected_amap_id=f"B00000000{index}")
            for index, item in enumerate(first["campusAssignments"], start=1)
        ]
        park_query = SimpleDirectionFrontierService.begin_slot_query(
            first["slotFrontierSnapshot"],
            day_number=1,
            slot_id="d1_park",
            day_seed_amap_id="B000000001",
            query_scope_fingerprint="q" * 64,
        )
        settled = store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_collision_1",
            outcomes=campus_outcomes,
            slot_query_outcomes=[
                {
                    "query": park_query,
                    "providerOutcome": "success",
                    "admittedPhysicalGroups": ["B000000101"],
                    "rejectedPhysicalGroups": [],
                }
            ],
            proposal_id=None,
            disposition="novelty_collision",
            expected_request_contract_fingerprint=request_fingerprint,
            reason_code="novelty_collision",
            blocking_layer="poi",
        )
        assert settled["frontier"]["frontierStatus"] == "has_more"
        comparison = store.simple_direction_comparison_summary(portfolio_id=portfolio_id)
        assert comparison["remainingQualifiedEntityCount"] == 0
        assert comparison["remainingPoiPageCount"] == 2
        assert comparison["frontierStatus"] == "has_more"

        second = store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id="choice_exec_collision_2",
            campus_slots=slots,
            expected_request_contract_fingerprint=request_fingerprint,
        )
        assert [item["canonicalName"] for item in second["campusAssignments"]] == ["A", "B"]
        assert {item["assignmentMode"] for item in second["campusAssignments"]} == {"collision_retry"}
        assert second["slotFrontierSnapshot"]["slotFrontiers"][park_query["slotFrontierKey"]]["nextPage"] == 2
    finally:
        connection.close()


@pytest.mark.parametrize("binding_mode", ["category", "exact_entity"])
def test_agent_presearch_claim_is_forwarded_to_executor_and_outcomes_return_to_pipeline(
    monkeypatch, binding_mode: str
) -> None:
    clear_database()
    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "qualified_entities",
        classmethod(lambda _cls, **_kwargs: evidence(list("ABCD"))),
    )
    initial_plan = AgentInitialPlanOutput(
        reply="",
        mode="plan",
        daySlots=[
            {
                "slotId": "d1",
                "dayNumber": 1,
                "date": "2026-10-01",
                "startTime": "09:00",
                "rawNeed": "高校",
            },
            {
                "slotId": "d2",
                "dayNumber": 2,
                "date": "2026-10-02",
                "startTime": "09:00",
                "rawNeed": "高校",
            },
        ],
        intentPools=[
            {
                "poolId": "campus_pool",
                "rawNeed": "资格高校",
                "city": "示例城",
                "intentType": "campus_visit",
                "targetCount": 2,
                "requirementLevel": "required",
                "assignToSlots": ["d1", "d2"],
                "candidateHints": ["模型提示不得决定前沿"],
                "entityBindingMode": binding_mode,
                "exactEntity": "A" if binding_mode == "exact_entity" else None,
            }
        ],
    )
    route_decision = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"transportMode": "transit", "requestSemanticsPresent": True},
        detour_tolerance={"maxGeneralizedCostDelta": 10.0, "maxDetourRatio": 0.2},
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert route_decision is not None
    route_contract = {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        **route_decision,
    }
    connection = open_db()
    try:
        session = ConversationService(connection).create_session("示例城", "agent frontier presearch")
        service = AgentService(connection)
        request_context = {
            "requestIntentContract": {
                "routeDecisionContract": route_contract,
                "entityQualificationConstraint": {
                    "qualificationScheme": "scheme",
                    "qualificationValue": "value",
                },
            }
        }
        pipeline_context = {"city": "示例城", "requestIntentContract": request_context["requestIntentContract"]}
        events: list[dict] = []
        service._prepare_simple_open_direction_frontier(
            session_id=session.session_id,
            user_turn_id="turn_user_initial",
            assistant_turn_id="turn_assistant_initial",
            city="示例城",
            active_version_id=None,
            request_context=request_context,
            pipeline_context=pipeline_context,
            initial_plan=initial_plan,
            resolved_dates={
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "source": "literal_current_turn",
            },
            tool_events=events,
        )
        assignment = pipeline_context["simpleDirectionFrontierAssignment"]
        assigned_names = [item["canonicalName"] for item in assignment["campusAssignments"]]
        assert len(assigned_names) == len(set(assigned_names)) == 2
        assert set(assigned_names) <= set("ABCD")
        frozen = PlanPortfolioStore(connection).summary(portfolio_id=pipeline_context["rootPortfolioId"])
        ordering = frozen["simpleDirectionFrontier"].get("explorationOrdering")
        assert isinstance(ordering, dict) is (binding_mode == "category")
        if binding_mode == "exact_entity":
            assert assigned_names == ["A", "B"]
        assert events[-1]["metadata"]["resultPreview"]["providerCalled"] is False

        captured: dict = {}

        class CapturingExecutor:
            @staticmethod
            def build_segment_plans(_initial_plan, **kwargs):
                captured.update(kwargs)
                outcomes = [
                    {
                        "slotId": item["slotId"],
                        "evidenceEntityFingerprint": item["evidenceEntityFingerprint"],
                        "providerOutcome": "success",
                        "selectedAmapId": f"B00000000{index}",
                        "queryFingerprint": item["queryFingerprint"],
                        "page": item["page"],
                    }
                    for index, item in enumerate(kwargs["frontier_assignment"]["campusAssignments"], start=1)
                ]
                return [], [
                    {
                        "type": "simple_direction_frontier_outcomes",
                        "metadata": {"outcomes": outcomes},
                    }
                ]

        service.simple_open_itinerary_executor = CapturingExecutor()
        service._simple_open_persistable_segment_plans(
            initial_plan,
            city="示例城",
            transport_mode="public_transit",
            tool_events=events,
            pipeline_context=pipeline_context,
            session_id=session.session_id,
        )
        assert captured["frontier_assignment"] == assignment
        assert len(pipeline_context["simpleDirectionFrontierOutcomes"]) == 2
        assert [item["queryFingerprint"] for item in pipeline_context["simpleDirectionFrontierOutcomes"]] == [
            item["queryFingerprint"] for item in assignment["campusAssignments"]
        ]
    finally:
        connection.close()

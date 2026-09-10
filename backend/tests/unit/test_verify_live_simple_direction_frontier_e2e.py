from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER_PATH = REPO_ROOT / "scripts" / "verify_live_simple_direction_frontier_e2e.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_live_simple_direction_frontier_e2e",
    VERIFIER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _amap_id(number: int) -> str:
    return f"B{number:09d}"


def _independence(
    group_id: str,
    *,
    status: str = "standalone_verified",
    receipt: str = "f" * 64,
    queried_at: str = "2026-08-24T08:00:00+00:00",
    category: str = "accepted",
) -> dict:
    return {
        "schemaVersion": "experience-independence-v1",
        "status": status,
        "physicalGroupId": group_id,
        "evidence": {
            "providerQueryReceiptFingerprint": receipt,
            "providerQueriedAt": queried_at,
            "providerCategoryDecision": category,
        },
    }


def _segment(
    *,
    day_number: int,
    role: str,
    poi_number: int,
    group_number: int | None = None,
    park_status: str = "standalone_verified",
    park_receipt: str = "f" * 64,
    park_queried_at: str = "2026-08-24T08:00:00+00:00",
    park_category: str = "accepted",
) -> dict:
    amap_id = _amap_id(poi_number)
    poi = {
        "id": f"poi_{role}_{day_number}",
        "amapId": amap_id,
        "name": f"{role}-{poi_number}",
        "source": "amap-place-search",
        "latitude": 39.9 + poi_number / 10000,
        "longitude": 116.3 + poi_number / 10000,
    }
    metadata = {
        "intentType": "campus_visit" if role == "campus" else role,
        "planningSlotId": f"slot_{day_number}_{role}",
        "groundingStatus": "verified_amap",
        "scheduleConstraints": {"replaceablePoi": role in {"meal", "park"}},
    }
    if role == "park":
        independence = _independence(
            _amap_id(group_number or poi_number),
            status=park_status,
            receipt=park_receipt,
            queried_at=park_queried_at,
            category=park_category,
        )
        poi["experienceIndependenceEvidence"] = independence
        metadata["scheduleConstraints"]["experienceIndependenceEvidence"] = independence
    return {
        "id": f"segment_{day_number}_{role}",
        "kind": role,
        "startTime": {"campus": "09:00", "meal": "12:00", "park": "17:00"}[role],
        "endTime": {"campus": "10:30", "meal": "13:30", "park": "18:30"}[role],
        "poi": poi,
        "semanticMetadata": metadata,
    }


def _route_assignment(
    ordered_segments: list[list[dict]],
    *,
    complete: bool = True,
    geometry_used: bool = False,
    pair_duration_seconds: int = 1800,
) -> dict:
    expected_pairs: list[dict] = []
    verified_pairs: list[dict] = []
    for day_number, day_segments in enumerate(ordered_segments, start=1):
        for pair_ordinal, (left, right) in enumerate(
            zip(day_segments, day_segments[1:]),
            start=1,
        ):
            pair_identity = {
                "dayNumber": day_number,
                "pairOrdinal": pair_ordinal,
                "fromSegmentId": left["id"],
                "toSegmentId": right["id"],
                "fromAmapId": left["poi"]["amapId"],
                "toAmapId": right["poi"]["amapId"],
            }
            expected_pairs.append(pair_identity)
            if complete:
                verified_pairs.append(
                    {
                        **pair_identity,
                        "transportMode": "transit",
                        "durationSeconds": pair_duration_seconds,
                        "distanceMeters": 2400,
                        "provider": "amap-route",
                        "queriedAt": "2026-08-24T08:05:00+00:00",
                        "providerEvidenceFingerprint": "d" * 64,
                    }
                )
    return {
        "schemaVersion": "simple-open-route-evidence-v2",
        "decisionSource": "bounded_candidate_geometry_then_provider_final_legs",
        "routeContractFingerprint": "route-contract-fingerprint",
        "topologyEvidence": {
            "evidenceSource": "bounded_candidate_geometry",
            "geometryUsedAsRouteFeasibilityEvidence": geometry_used,
        },
        "providerBaselineCompared": False,
        "topologyCompliance": "verified" if complete else "pending",
        "adjacentLegCompliance": "verified" if complete else "pending",
        "routeCoverageComplete": complete,
        "detourCompliance": "not_evaluated",
        "expectedPairs": expected_pairs,
        "verifiedPairs": verified_pairs,
        "failureReason": None if complete else "provider_route_matrix_incomplete",
    }


def _snapshot(
    *,
    title: str,
    campuses: tuple[int, int],
    parks: tuple[int, int],
    park_groups: tuple[int, int],
    novelty_prior_ids: list[str],
    route_complete: bool = True,
    geometry_used: bool = False,
    pair_duration_seconds: int = 1800,
    park_status: str = "standalone_verified",
) -> dict:
    days: list[dict] = []
    ordered_segments: list[list[dict]] = []
    for day_number in (1, 2):
        campus = _segment(day_number=day_number, role="campus", poi_number=campuses[day_number - 1])
        meal = _segment(day_number=day_number, role="meal", poi_number=100 + day_number)
        park = _segment(
            day_number=day_number,
            role="park",
            poi_number=parks[day_number - 1],
            group_number=park_groups[day_number - 1],
            park_status=park_status,
        )
        days.append(
            {
                "id": f"day_{day_number}",
                "dayNumber": day_number,
                "date": f"2026-10-0{day_number}",
                "segments": [campus, meal, park],
            }
        )
        ordered_segments.append([campus, meal, park])
    return {
        "id": title,
        "title": title,
        "city": "北京",
        "status": "draft",
        "workflowMode": "simple_direction_v1",
        "days": days,
        "routeDecisionContract": {
            "schemaVersion": "route-decision-contract-v2",
            "status": "ready",
            "missingFields": [],
            "mobilityProfile": {"transportMode": "transit"},
            "detourTolerance": {
                "maxGeneralizedCostDelta": 10,
                "maxDetourRatio": 0.2,
            },
            "adjacentLegConstraint": {
                "candidateSearchRadiusMeters": 5000,
                "maxProviderTravelMinutes": 45,
            },
            "topologyConstraint": {"maxBacktrackRatio": 0.15},
            "fingerprint": "route-contract-fingerprint",
        },
        "simpleOpenRouteAssignment": _route_assignment(
            ordered_segments,
            complete=route_complete,
            geometry_used=geometry_used,
            pair_duration_seconds=pair_duration_seconds,
        ),
        "simpleDirectionNoveltyEvidence": {
            "schemaVersion": "simple-direction-novelty-v3",
            "frontierAttemptFingerprint": "3" * 64,
            "passed": True,
            "readyNoveltyPassed": route_complete,
            "repairablePartialAccepted": not route_complete,
            "campusAssignmentsGrounded": True,
            "comparisons": [
                {
                    "priorProposalId": proposal_id,
                    "passedForStorage": True,
                    "campusRotationPassed": True,
                    "standaloneNoveltyPassed": True,
                    "orderedPairNoveltyPassed": True,
                    "changedCampusDayCount": 2,
                    "totalStandaloneChangedCount": 2,
                    "changedOrderedPairCount": 1,
                    "standaloneDayEvidence": [
                        {"dayNumber": 1, "changedCount": 1},
                        {"dayNumber": 2, "changedCount": 1},
                    ],
                    "partialIdentityPassed": True,
                }
                for proposal_id in novelty_prior_ids
            ],
        },
        "portfolioVerifier": {
            "confirmationPassed": route_complete,
            "blockingReasons": ([] if route_complete else [{"reasonCode": "provider_route_matrix_incomplete"}]),
        },
        "portfolioTitleGeneration": {
            "attemptCount": 1,
            "maxAttempts": 2,
            "titleDecisionSource": "route_fact_bound_server_fallback",
            "usedTitleSignal": title[:2],
        },
    }


def _frontier(
    *,
    status: str,
    remaining_entities: int,
    remaining_pages: int = 0,
    provider_pending: bool = False,
) -> dict:
    entities = [
        {
            "evidenceEntityFingerprint": "a" * 64,
            "state": "grounding_pending" if provider_pending else "used_ready",
            "canonicalAmapId": _amap_id(1),
        }
    ]
    return {
        "schemaVersion": "simple-direction-frontier-v1",
        "planningRootId": "plan-root-1",
        "requestContractFingerprint": "b" * 64,
        "qualificationEvidenceFingerprint": "e" * 64,
        "qualifiedEntityFrontier": entities,
        "slotFrontiers": {"slot-frontier": {"lastProviderOutcome": "failure"}} if provider_pending else {},
        "remainingQualifiedEntityCount": remaining_entities,
        "remainingPoiPageCount": remaining_pages,
        "frontierStatus": status,
        "terminalBlockingLayer": "provider" if provider_pending else "qualification",
        "executionProfile": {
            "maxPagesPerQuery": 3,
            "pageOffset": 20,
            "profileFingerprint": "c" * 64,
        },
        "frontierFingerprint": "f" * 64,
    }


def _frontier_attempt_record(execution_id: str) -> dict:
    assignment = {
        "slotId": "campus_day_1",
        "dayNumber": 1,
        "evidenceEntityFingerprint": "a" * 64,
        "page": 1,
        "offset": 20,
        "queryScopeFingerprint": "1" * 64,
        "queryFingerprint": "2" * 64,
    }
    attempt = {
        "schemaVersion": "simple-direction-frontier-attempt-v1",
        "frontierFingerprint": "f" * 64,
        "attemptFingerprint": "3" * 64,
        "requestContractFingerprint": "b" * 64,
        "requestedCampusSlotCount": 1,
        "campusAssignments": [assignment],
    }
    return {
        "schemaVersion": "simple-direction-frontier-claim-v1",
        "executionId": execution_id,
        "requestContractFingerprint": "b" * 64,
        "status": "reconciled",
        "attempt": attempt,
        "outcomes": [
            {
                "slotId": "campus_day_1",
                "evidenceEntityFingerprint": "a" * 64,
                "queryFingerprint": "2" * 64,
                "page": 1,
                "providerOutcome": "success",
                "selectedAmapId": _amap_id(1),
            }
        ],
    }


def _summary(frontier: dict, *, ready_count: int, partial_count: int, visible_ids: list[str]) -> dict:
    return {
        "workflowMode": "simple_direction_v1",
        "simpleDirectionFrontier": frontier,
        "comparisonSummary": {
            "frontierStatus": frontier["frontierStatus"],
            "adoptionReadyCount": ready_count,
            "repairablePartialCount": partial_count,
            "remainingQualifiedEntityCount": frontier["remainingQualifiedEntityCount"],
        },
        "visibleProposalIds": visible_ids,
    }


def _turn(turn_id: str, index: int, role: str, *, response: dict | None = None, request: dict | None = None) -> tuple:
    return (
        turn_id,
        index,
        role,
        "active",
        None,
        None,
        "",
        json.dumps(request or {}, ensure_ascii=False),
        json.dumps(response or {}, ensure_ascii=False),
        f"2026-08-24T08:{index:02d}:00+00:00",
    )


def _proposal_row(
    proposal_id: str,
    *,
    title: str,
    snapshot: dict,
    status: str = "adoption_ready",
    source_turn_id: str,
    source_user_turn_id: str = "turn_user_0",
    choice_id: str | None = None,
    rank_index: int = 0,
    frontier_execution_id: str | None = None,
    frontier_attempt_fingerprint: str = "3" * 64,
    request_contract_fingerprint: str = "b" * 64,
) -> tuple:
    return (
        proposal_id,
        "portfolio_1",
        rank_index,
        status,
        choice_id or f"choice_{proposal_id}",
        json.dumps(snapshot, ensure_ascii=False),
        json.dumps(
            {
                "sourceAssistantTurnId": source_turn_id,
                "sourceUserTurnId": source_user_turn_id,
                "frontierExecutionId": (frontier_execution_id or f"exec_frontier_{proposal_id}"),
                "frontierAttemptFingerprint": frontier_attempt_fingerprint,
                "requestContractFingerprint": request_contract_fingerprint,
            },
            ensure_ascii=False,
        ),
        "2026-08-24T08:10:00+00:00",
    )


def _create_database(
    path: Path,
    *,
    frontier: dict,
    proposals: list[tuple],
    turns: list[tuple],
    continuation_rows: list[tuple],
    adoption_rows: list[tuple],
    version_count: int,
    patch_count: int,
    route_count: int,
    active_version_id: str = "version_1",
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE conversation_sessions (
                id TEXT PRIMARY KEY,
                active_plan_id TEXT,
                active_version_id TEXT
            );
            CREATE TABLE conversation_turns (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                turn_index INTEGER,
                role TEXT,
                status TEXT,
                parent_turn_id TEXT,
                itinerary_version_id TEXT,
                content TEXT,
                agent_request_json TEXT,
                agent_response_json TEXT,
                created_at TEXT
            );
            CREATE TABLE agent_plan_portfolios (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                status TEXT,
                selected_proposal_id TEXT,
                expected_base_version_id TEXT,
                summary_json TEXT
            );
            CREATE TABLE agent_plan_proposals (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT,
                rank_index INTEGER,
                status TEXT,
                choice_id TEXT,
                snapshot_json TEXT,
                generation_lineage_json TEXT,
                created_at TEXT
            );
            CREATE TABLE agent_choice_executions (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                action TEXT,
                status TEXT,
                choice_id TEXT,
                source_turn_id TEXT,
                request_turn_id TEXT,
                execution_turn_id TEXT,
                result_version_id TEXT,
                outcome_json TEXT,
                created_at TEXT
            );
            CREATE TABLE itinerary_versions (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                version_number INTEGER
            );
            CREATE TABLE itinerary_patches (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                created_at TEXT
            );
            CREATE TABLE route_options (
                id TEXT PRIMARY KEY,
                plan_id TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO conversation_sessions (id, active_plan_id, active_version_id) VALUES (?, ?, ?)",
            ("session_1", "plan_1", active_version_id),
        )
        summary = _summary(
            frontier,
            ready_count=sum(1 for item in proposals if item[3] in {"adoption_ready", "committed"}),
            partial_count=sum(1 for item in proposals if item[3] not in {"adoption_ready", "committed"}),
            visible_ids=[item[0] for item in proposals],
        )
        frontier_attempts = {}
        for proposal in proposals:
            lineage = json.loads(str(proposal[6] or "{}"))
            execution_id = str(lineage.get("frontierExecutionId") or "")
            if execution_id:
                frontier_attempts[execution_id] = _frontier_attempt_record(execution_id)
        frontier_attempts.update({str(item[0]): _frontier_attempt_record(str(item[0])) for item in continuation_rows})
        summary["simpleDirectionFrontierAttempts"] = frontier_attempts
        connection.execute(
            "INSERT INTO agent_plan_portfolios (id, session_id, status, selected_proposal_id, expected_base_version_id, summary_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "portfolio_1",
                "session_1",
                "awaiting_selection",
                proposals[0][0] if proposals else None,
                active_version_id,
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        connection.executemany(
            "INSERT INTO conversation_turns (id, session_id, turn_index, role, status, parent_turn_id, itinerary_version_id, content, agent_request_json, agent_response_json, created_at) VALUES (?, 'session_1', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            turns,
        )
        connection.executemany(
            "INSERT INTO agent_plan_proposals (id, portfolio_id, rank_index, status, choice_id, snapshot_json, generation_lineage_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            proposals,
        )
        connection.executemany(
            "INSERT INTO agent_choice_executions (id, session_id, action, status, choice_id, source_turn_id, request_turn_id, execution_turn_id, result_version_id, outcome_json, created_at) VALUES (?, 'session_1', ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?)",
            continuation_rows + adoption_rows,
        )
        for index in range(version_count):
            connection.execute(
                "INSERT INTO itinerary_versions (id, session_id, version_number) VALUES (?, 'session_1', ?)",
                (f"version_{index + 1}", index + 1),
            )
        for index in range(patch_count):
            connection.execute(
                "INSERT INTO itinerary_patches (id, session_id, created_at) VALUES (?, 'session_1', ?)",
                (f"patch_{index + 1}", f"2026-08-24T08:40:0{index}+00:00"),
            )
        for index in range(route_count):
            connection.execute(
                "INSERT INTO route_options (id, plan_id) VALUES (?, 'plan_1')",
                (f"route_{index + 1}",),
            )
        connection.commit()
    finally:
        connection.close()


def _base_turns() -> list[tuple]:
    controller_trace = {
        "source": "controller",
        "decisionPath": "full",
        "controllerSucceeded": True,
        "accepted": True,
        "decisionId": "controller_decision_1",
        "controllerPerformance": [
            {
                "providerInvoked": True,
                "captureState": "completed",
                "responseHeadersReceived": True,
                "httpStatus": 200,
                "responseBytes": 128,
                "callKind": "controller_full",
            }
        ],
        "providerRawDecisions": [
            {
                "schemaVersion": "react-controller-decision-v2",
                "primaryAction": "plan",
            }
        ],
    }
    turn_offer_1 = _turn(
        "turn_assistant_offer_1",
        1,
        "assistant",
        response={
            "controllerDecisionTrace": controller_trace,
            "choiceOptions": [
                {
                    "id": "choice_continue_1",
                    "action": "continue_plan_expansion",
                    "kind": "simple_direction_more_plans",
                    "scopeKind": "comparison",
                    "sourceAssistantTurnId": "turn_assistant_offer_1",
                    "planningSelectionRootTurnId": "plan-root-1",
                    "rootPortfolioId": "portfolio_1",
                    "requestContractFingerprint": "b" * 64,
                },
                {
                    "id": "choice_proposal_1",
                    "action": "select_plan_proposal",
                    "proposalId": "proposal_1",
                },
            ],
        },
    )
    turn_continue_request = _turn(
        "turn_user_continue_1",
        2,
        "user",
        request={
            "context": {
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "turn_assistant_offer_1",
                    "choiceId": "choice_continue_1",
                }
            }
        },
    )
    turn_continue_exec = _turn("turn_assistant_continue_1", 3, "assistant")
    turn_offer_2 = _turn(
        "turn_assistant_offer_2",
        4,
        "assistant",
        response={
            "choiceOptions": [
                {
                    "id": "choice_proposal_2",
                    "action": "select_plan_proposal",
                    "proposalId": "proposal_2",
                }
            ]
        },
    )
    turn_adopt_request = _turn("turn_user_adopt_1", 5, "user")
    turn_adopt_exec = _turn("turn_assistant_adopt_1", 6, "assistant")
    turn_user_root = _turn("turn_user_0", 0, "user")
    return [
        turn_user_root,
        turn_offer_1,
        turn_continue_request,
        turn_continue_exec,
        turn_offer_2,
        turn_adopt_request,
        turn_adopt_exec,
    ]


def _success_journey() -> dict:
    rounds = [
        {
            "roundIndex": 0,
            "continueClicked": True,
            "comparisonSummary": {
                "frontierStatus": "has_more",
                "adoptionReadyCount": 1,
                "repairablePartialCount": 0,
            },
            "proposalIds": ["proposal_1"],
            "newProposalIds": ["proposal_1"],
            "readyProposalIds": ["proposal_1"],
            "partialProposalIds": [],
            "ui": {"readyCardCount": 1, "partialCardCount": 0},
            "continueCapability": {
                "sourceAssistantTurnId": "turn_assistant_offer_1",
                "choiceId": "choice_continue_1",
                "planningSelectionRootTurnId": "plan-root-1",
                "rootPortfolioId": "portfolio_1",
                "requestContractFingerprint": "b" * 64,
            },
        },
        {
            "roundIndex": 1,
            "comparisonSummary": {
                "frontierStatus": "qualification_exhausted",
                "adoptionReadyCount": 2,
                "repairablePartialCount": 0,
            },
            "proposalIds": ["proposal_1", "proposal_2"],
            "newProposalIds": ["proposal_2"],
            "readyProposalIds": ["proposal_1", "proposal_2"],
            "partialProposalIds": [],
            "ui": {"readyCardCount": 2, "partialCardCount": 0},
            "continueCapability": None,
        },
    ]
    return {
        "schemaVersion": "simple-direction-frontier-live-journey-v1",
        "status": "success",
        "sessionId": "session_1",
        "boundedExecution": {
            "continuationClicks": 1,
            "stopReason": "frontier_qualification_exhausted",
            "frontierConverged": True,
        },
        "exploration": {
            "rounds": rounds,
            "finalPortfolioId": "portfolio_1",
            "finalPlanningRootId": "plan-root-1",
            "frontierEvidence": _frontier(
                status="qualification_exhausted",
                remaining_entities=0,
            ),
        },
        "preAdoption": {
            "zeroFormalWrites": True,
            "counts": {
                "itineraryVersionCount": 0,
                "patchCount": 0,
                "formalRouteWriteCount": 0,
            },
        },
        "proposals": [
            {"proposalId": "proposal_1"},
            {"proposalId": "proposal_2"},
        ],
        "adoption": {
            "status": "confirmed_and_exactly_once_replayed",
            "proposalId": "proposal_1",
            "firstActiveVersionId": "version_1",
            "replayActiveVersionId": "version_1",
            "postAdoptionCounts": {
                "itineraryVersionCount": 1,
                "patchCount": 1,
                "formalRouteWriteCount": 1,
                "choiceExecutionCount": 2,
            },
            "finalCounts": {
                "itineraryVersionCount": 1,
                "patchCount": 1,
                "formalRouteWriteCount": 1,
                "choiceExecutionCount": 2,
            },
        },
    }


def _zero_pre_adoption_bundle() -> dict:
    return {
        "sections": {
            "ITINERARY_VERSIONS": [],
            "PATCHES": [],
            "ROUTE_EVIDENCE": [],
        }
    }


def _create_success_database(path: Path) -> None:
    snapshot_1 = _snapshot(
        title="清北学府与夜色公园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(201, 202),
        novelty_prior_ids=[],
    )
    snapshot_2 = _snapshot(
        title="双校漫步伴黄昏林地",
        campuses=(3, 4),
        parks=(23, 24),
        park_groups=(203, 204),
        novelty_prior_ids=["proposal_1"],
    )
    _create_database(
        path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=0),
        proposals=[
            _proposal_row(
                "proposal_1",
                title="清北学府与夜色公园",
                snapshot=snapshot_1,
                source_turn_id="turn_assistant_offer_1",
                choice_id="choice_proposal_1",
                rank_index=0,
                status="committed",
            ),
            _proposal_row(
                "proposal_2",
                title="双校漫步伴黄昏林地",
                snapshot=snapshot_2,
                source_turn_id="turn_assistant_offer_2",
                choice_id="choice_proposal_2",
                rank_index=1,
                frontier_execution_id="exec_continue_1",
            ),
        ],
        turns=_base_turns(),
        continuation_rows=[
            (
                "exec_continue_1",
                "continue_plan_expansion",
                "choice_continue_1",
                "turn_assistant_offer_1",
                "turn_user_continue_1",
                "turn_assistant_continue_1",
                "",
                json.dumps(
                    {"versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0},
                    ensure_ascii=False,
                ),
                "2026-08-24T08:20:00+00:00",
            )
        ],
        adoption_rows=[
            (
                "exec_adopt_1",
                "select_plan_proposal",
                "choice_proposal_1",
                "turn_assistant_offer_1",
                "turn_user_adopt_1",
                "turn_assistant_adopt_1",
                "version_1",
                json.dumps({}, ensure_ascii=False),
                "2026-08-24T08:30:00+00:00",
            )
        ],
        version_count=1,
        patch_count=1,
        route_count=1,
    )


def test_frontier_verifier_accepts_successful_journey(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-success.db"
    _create_success_database(database_path)

    report = verifier.verify_database(
        database_path,
        _success_journey(),
        deepseek_endpoint_host="api.deepseek.com",
        pre_adoption_bundle=_zero_pre_adoption_bundle(),
    )

    assert report["passed"] is True, report["failures"]
    assert report["failures"] == []


def test_frontier_verifier_accepts_provider_pending_degraded_branch(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-degraded.db"
    _create_database(
        database_path,
        frontier=_frontier(
            status="provider_pending",
            remaining_entities=1,
            provider_pending=True,
        ),
        proposals=[],
        turns=_base_turns()[:2],
        continuation_rows=[],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
        active_version_id="",
    )
    journey = {
        "schemaVersion": "simple-direction-frontier-live-journey-v1",
        "sessionId": "session_1",
        "status": "journey_failed_before_contract_completion",
        "exploration": {
            "finalPortfolioId": "portfolio_1",
            "finalPlanningRootId": "plan-root-1",
            "frontierEvidence": _frontier(
                status="provider_pending",
                remaining_entities=1,
                provider_pending=True,
            ),
        },
    }

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is True, report["failures"]
    assert report["blockers"] == ["frontier_provider_pending"]


def test_frontier_verifier_accepts_truthful_repairable_partial(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-partial.db"
    snapshot = _snapshot(
        title="高校方向待补全公园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(201, 202),
        novelty_prior_ids=[],
        route_complete=False,
    )
    _create_database(
        database_path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=0),
        proposals=[
            _proposal_row(
                "proposal_partial",
                title="高校方向待补全公园",
                snapshot=snapshot,
                status="blocked",
                source_turn_id="turn_assistant_offer_1",
            )
        ],
        turns=_base_turns()[:2],
        continuation_rows=[],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
        active_version_id="",
    )
    journey = {
        "schemaVersion": "simple-direction-frontier-live-journey-v1",
        "sessionId": "session_1",
        "status": "journey_failed_before_contract_completion",
        "boundedExecution": {"continuationClicks": 0},
        "exploration": {
            "rounds": [
                {
                    "roundIndex": 0,
                    "comparisonSummary": {
                        "frontierStatus": "qualification_exhausted",
                        "adoptionReadyCount": 0,
                        "repairablePartialCount": 1,
                    },
                    "proposalIds": ["proposal_partial"],
                    "newProposalIds": ["proposal_partial"],
                    "readyProposalIds": [],
                    "partialProposalIds": ["proposal_partial"],
                    "ui": {"readyCardCount": 0, "partialCardCount": 1},
                }
            ],
            "finalPortfolioId": "portfolio_1",
            "finalPlanningRootId": "plan-root-1",
            "frontierEvidence": _frontier(
                status="qualification_exhausted",
                remaining_entities=0,
            ),
        },
        "proposals": [{"proposalId": "proposal_partial"}],
    }

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is True, report["failures"]
    assert report["blockers"] == ["provider_route_matrix_incomplete"]


def test_frontier_verifier_rejects_false_exhaustion(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-false-exhaustion.db"
    journey = _success_journey()
    journey["exploration"]["frontierEvidence"] = _frontier(
        status="qualification_exhausted",
        remaining_entities=1,
    )
    _create_database(
        database_path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=1),
        proposals=[],
        turns=[_turn("turn_user_0", 0, "user")],
        continuation_rows=[],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
    )

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is False
    assert "frontier_false_exhaustion_remaining_entities" in report["failures"]


def test_frontier_verifier_rejects_forged_continuation_identity(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-forged-continuation.db"
    snapshot = _snapshot(
        title="清北学府与夜色公园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(201, 202),
        novelty_prior_ids=[],
    )
    _create_database(
        database_path,
        frontier=_frontier(status="has_more", remaining_entities=1),
        proposals=[
            _proposal_row(
                "proposal_1",
                title="清北学府与夜色公园",
                snapshot=snapshot,
                source_turn_id="turn_assistant_offer_1",
                choice_id="choice_proposal_1",
            )
        ],
        turns=_base_turns(),
        continuation_rows=[
            (
                "exec_continue_1",
                "continue_plan_expansion",
                "choice_forged",
                "turn_assistant_offer_1",
                "turn_user_continue_1",
                "turn_assistant_continue_1",
                "",
                json.dumps(
                    {"versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0},
                    ensure_ascii=False,
                ),
                "2026-08-24T08:20:00+00:00",
            )
        ],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
    )
    journey = _success_journey()
    journey["adoption"] = {"status": "not_attempted_no_ready_proposal"}

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is False
    assert "continuation_capability_identity_mismatch" in report["failures"]


def test_frontier_verifier_rejects_embedded_or_pending_park(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-park.db"
    snapshot = _snapshot(
        title="清北学府与夜色公园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(1, 2),
        novelty_prior_ids=[],
        park_status="embedded_in_day_anchor",
    )
    _create_database(
        database_path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=0),
        proposals=[
            _proposal_row(
                "proposal_1",
                title="清北学府与夜色公园",
                snapshot=snapshot,
                source_turn_id="turn_assistant_offer_1",
                choice_id="choice_proposal_1",
            )
        ],
        turns=_base_turns(),
        continuation_rows=[],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
    )
    journey = _success_journey()
    journey["proposals"] = [{"proposalId": "proposal_1"}]
    journey["adoption"] = {"status": "not_attempted_no_ready_proposal"}

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is False
    assert any(item.startswith("proposal_park:proposal_1:park_not_standalone_verified") for item in report["failures"])


def test_frontier_verifier_rejects_incomplete_route_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-route.db"
    snapshot = _snapshot(
        title="清北学府与夜色公园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(201, 202),
        novelty_prior_ids=[],
        route_complete=False,
    )
    # Model a corrupt/forged ready classification so the verifier must reject
    # the missing Provider route coverage rather than quietly treating it as a
    # legitimate partial direction.
    snapshot["portfolioVerifier"]["confirmationPassed"] = True
    _create_database(
        database_path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=0),
        proposals=[
            _proposal_row(
                "proposal_1",
                title="清北学府与夜色公园",
                snapshot=snapshot,
                source_turn_id="turn_assistant_offer_1",
                choice_id="choice_proposal_1",
            )
        ],
        turns=_base_turns(),
        continuation_rows=[],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
    )
    journey = _success_journey()
    journey["proposals"] = [{"proposalId": "proposal_1"}]
    journey["adoption"] = {"status": "not_attempted_no_ready_proposal"}

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is False
    assert any(item.startswith("proposal_route:proposal_1:route_coverage_incomplete") for item in report["failures"])


def test_frontier_verifier_rejects_title_collision(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-title.db"
    snapshot_1 = _snapshot(
        title="学府漫游与晚间游园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(201, 202),
        novelty_prior_ids=[],
    )
    snapshot_2 = _snapshot(
        title="学府探访与傍晚游园",
        campuses=(3, 4),
        parks=(23, 24),
        park_groups=(203, 204),
        novelty_prior_ids=["proposal_1"],
    )
    _create_database(
        database_path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=0),
        proposals=[
            _proposal_row(
                "proposal_1",
                title="学府漫游与晚间游园",
                snapshot=snapshot_1,
                source_turn_id="turn_assistant_offer_1",
                choice_id="choice_proposal_1",
            ),
            _proposal_row(
                "proposal_2",
                title="学府探访与傍晚游园",
                snapshot=snapshot_2,
                source_turn_id="turn_assistant_offer_2",
                choice_id="choice_proposal_2",
                rank_index=1,
            ),
        ],
        turns=_base_turns(),
        continuation_rows=[],
        adoption_rows=[],
        version_count=0,
        patch_count=0,
        route_count=0,
    )
    journey = _success_journey()
    journey["adoption"] = {"status": "not_attempted_no_ready_proposal"}

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is False
    assert "ready_title_common_prefix" in report["failures"]
    assert "ready_title_common_suffix" in report["failures"]


def test_frontier_verifier_rejects_duplicate_adoption_write_growth(tmp_path: Path) -> None:
    database_path = tmp_path / "frontier-adoption-growth.db"
    snapshot = _snapshot(
        title="清北学府与夜色公园",
        campuses=(1, 2),
        parks=(21, 22),
        park_groups=(201, 202),
        novelty_prior_ids=[],
    )
    _create_database(
        database_path,
        frontier=_frontier(status="qualification_exhausted", remaining_entities=0),
        proposals=[
            _proposal_row(
                "proposal_1",
                title="清北学府与夜色公园",
                snapshot=snapshot,
                source_turn_id="turn_assistant_offer_1",
                choice_id="choice_proposal_1",
            )
        ],
        turns=_base_turns(),
        continuation_rows=[],
        adoption_rows=[
            (
                "exec_adopt_1",
                "select_plan_proposal",
                "choice_proposal_1",
                "turn_assistant_offer_1",
                "turn_user_adopt_1",
                "turn_assistant_adopt_1",
                "version_1",
                json.dumps({}, ensure_ascii=False),
                "2026-08-24T08:30:00+00:00",
            ),
            (
                "exec_adopt_2",
                "select_plan_proposal",
                "choice_proposal_1",
                "turn_assistant_offer_1",
                "turn_user_adopt_1",
                "turn_assistant_adopt_1",
                "version_2",
                json.dumps({}, ensure_ascii=False),
                "2026-08-24T08:31:00+00:00",
            ),
        ],
        version_count=2,
        patch_count=2,
        route_count=2,
        active_version_id="version_2",
    )
    journey = _success_journey()

    report = verifier.verify_database(
        database_path,
        journey,
        deepseek_endpoint_host="api.deepseek.com",
    )

    assert report["passed"] is False
    assert "adoption_execution_count:2" in report["failures"]


def test_frontier_verifier_rejects_forged_proposal_frontier_execution_id(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "frontier-forged-proposal-execution.db"
    _create_success_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT generation_lineage_json FROM agent_plan_proposals WHERE id = 'proposal_2'"
        ).fetchone()
        lineage = json.loads(str(row[0]))
        lineage["frontierExecutionId"] = "exec_forged_cross_root"
        connection.execute(
            "UPDATE agent_plan_proposals SET generation_lineage_json = ? WHERE id = 'proposal_2'",
            (json.dumps(lineage, ensure_ascii=False),),
        )
        connection.commit()
    finally:
        connection.close()

    report = verifier.verify_database(
        database_path,
        _success_journey(),
        deepseek_endpoint_host="api.deepseek.com",
        pre_adoption_bundle=_zero_pre_adoption_bundle(),
    )

    assert report["passed"] is False
    assert "proposal_frontier_execution_not_persisted:proposal_2" in report["failures"]


def test_frontier_verifier_rejects_cross_fingerprint_proposal_lineage(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "frontier-cross-fingerprint.db"
    _create_success_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT generation_lineage_json FROM agent_plan_proposals WHERE id = 'proposal_2'"
        ).fetchone()
        lineage = json.loads(str(row[0]))
        lineage["frontierAttemptFingerprint"] = "4" * 64
        lineage["requestContractFingerprint"] = "5" * 64
        connection.execute(
            "UPDATE agent_plan_proposals SET generation_lineage_json = ? WHERE id = 'proposal_2'",
            (json.dumps(lineage, ensure_ascii=False),),
        )
        connection.commit()
    finally:
        connection.close()

    report = verifier.verify_database(
        database_path,
        _success_journey(),
        deepseek_endpoint_host="api.deepseek.com",
        pre_adoption_bundle=_zero_pre_adoption_bundle(),
    )

    assert report["passed"] is False
    assert "proposal_lineage_request_fingerprint_mismatch:proposal_2" in report["failures"]
    assert "proposal_frontier_attempt_fingerprint_mismatch:proposal_2" in report["failures"]


def test_frontier_verifier_rejects_reused_frontier_execution_across_proposals(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "frontier-reused-execution.db"
    _create_success_database(database_path)
    connection = sqlite3.connect(database_path)
    try:
        lineages = connection.execute(
            "SELECT id, generation_lineage_json FROM agent_plan_proposals ORDER BY rank_index"
        ).fetchall()
        first_lineage = json.loads(str(lineages[0][1]))
        second_lineage = json.loads(str(lineages[1][1]))
        second_lineage["frontierExecutionId"] = first_lineage["frontierExecutionId"]
        connection.execute(
            "UPDATE agent_plan_proposals SET generation_lineage_json = ? WHERE id = ?",
            (json.dumps(second_lineage, ensure_ascii=False), str(lineages[1][0])),
        )
        connection.commit()
    finally:
        connection.close()

    report = verifier.verify_database(
        database_path,
        _success_journey(),
        deepseek_endpoint_host="api.deepseek.com",
        pre_adoption_bundle=_zero_pre_adoption_bundle(),
    )

    assert report["passed"] is False
    assert "proposal_frontier_execution_reused:proposal_2" in report["failures"]


def test_artifact_commit_contract_requires_one_frozen_40hex_commit() -> None:
    git_commit = "a" * 40
    assert (
        verifier.artifact_commit_errors(
            journey={"gitCommit": git_commit},
            run_summary={"gitCommit": git_commit},
            expected_git_commit=git_commit,
        )
        == []
    )
    failures = verifier.artifact_commit_errors(
        journey={"gitCommit": "b" * 40},
        run_summary={"gitCommit": "c" * 40},
        expected_git_commit=git_commit,
    )
    assert "journey_git_commit_mismatch" in failures
    assert "run_summary_git_commit_mismatch" in failures
    assert "artifact_git_commit_mismatch" in failures


def test_frontier_verifier_cli_rejects_artifact_commit_mismatch_before_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "must-not-open.db"
    journey_path = tmp_path / "journey-result.json"
    summary_path = tmp_path / "run-summary.json"
    output_path = tmp_path / "verification.json"
    journey = _success_journey()
    journey["gitCommit"] = "a" * 40
    journey_path.write_text(json.dumps(journey), encoding="utf-8")
    summary_path.write_text(
        json.dumps({"gitCommit": "b" * 40}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER_PATH),
            "--database",
            str(database_path),
            "--journey-result",
            str(journey_path),
            "--run-summary",
            str(summary_path),
            "--expected-git-commit",
            "a" * 40,
            "--output",
            str(output_path),
            "--deepseek-endpoint-host",
            "api.deepseek.com",
        ],
    )

    assert verifier.main() == 1
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert "run_summary_git_commit_mismatch" in report["failures"]
    assert "artifact_git_commit_mismatch" in report["failures"]


def test_frontier_verifier_cli_rejects_local_host_before_opening_journey_or_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "must-not-open.db"
    journey_path = tmp_path / "must-not-read.json"
    output_path = tmp_path / "verification.json"
    journey_path.write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER_PATH),
            "--database",
            str(database_path),
            "--journey-result",
            str(journey_path),
            "--output",
            str(output_path),
            "--deepseek-endpoint-host",
            "127.0.0.1:18080",
        ],
    )

    assert verifier.main() == 1

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["failures"] == ["deepseek_endpoint_host_not_allowed"]

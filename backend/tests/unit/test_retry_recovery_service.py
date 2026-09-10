from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.conversation_service import ConversationService
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_planning_models import canonical_fingerprint
from src.services.agent_service import AgentService
from src.services.planning_attempt_resume_service import PlanningAttemptResumeService
from src.services.retry_recovery_service import (
    RetryExecutionPlanService,
    RetrySemanticClassifier,
)
from src.services.portfolio_route_feasibility_service import (
    PortfolioRouteFeasibilityService,
)
from src.services.route_insertion_scorer import RouteInsertionScorer
from test_portfolio_route_feasibility_service import FakeRouteService, _snapshot


def test_provider_route_failure_checkpoint_uses_actual_prepare_certificate_without_adoption(db_connection):
    """A real route matrix failure may issue an exact retry capability, never a write."""

    source_user_turn_id = "turn_route_repair_root"
    source_assistant_turn_id = "turn_route_repair_failure"
    reissued_assistant_turn_id = "turn_route_repair_reissued"
    portfolio_id = "portfolio_route_repair"
    request_context = {
        "city": "北京",
        "resolvedTripDates": {
            "startDate": "2026-10-01",
            "endDate": "2026-10-01",
            "dayCount": 1,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 1,
            "transportPreferences": ["transit"],
            "requiredIntents": [
                {
                    "goalId": "goal_route_repair_meal",
                    "intentType": "meal",
                    "requiredMin": 1,
                    "maxCount": 1,
                }
            ],
        },
    }
    request_fingerprint = ConstraintLedgerCompiler().compile(request_context).source_fingerprint
    protected_tables = ("itinerary_versions", "itinerary_patches", "route_options")

    def table_content_fingerprint(table: str) -> str:
        rows = [dict(row) for row in db_connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        return sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    snapshot = _snapshot()
    snapshot["rootPortfolioId"] = portfolio_id
    snapshot["portfolioSelectionContext"] = {
        "planningSelectionRootTurnId": source_user_turn_id,
        "rootPortfolioId": portfolio_id,
        "requestContractFingerprint": request_fingerprint,
    }
    feasibility = PortfolioRouteFeasibilityService(route_service=FakeRouteService())
    zero_write_before = {table: table_content_fingerprint(table) for table in protected_tables}

    prepared = feasibility.prepare(
        snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )

    assert prepared.status == "failed"
    assert prepared.provider_state != "no_progress"
    assert len(prepared.repair_attempt_ledger["routeBadCheckpoints"]) == 1
    prepared_certificate = prepared.repair_attempt_ledger["routeBadCheckpoints"][0]
    assert prepared_certificate["continuationMode"] == "repair_exact_slot"
    assert prepared_certificate["planningSelectionRootTurnId"] == source_user_turn_id
    assert prepared_certificate["rootPortfolioId"] == portfolio_id
    assert prepared_certificate["issueCodes"] == ["provider_route_matrix_unacceptable"]
    assert prepared_certificate["adjacentAnchorIds"] == ["campus_segment", "museum_segment"]
    assert prepared_certificate["fullProviderMatrixProofFingerprints"]

    candidate = SimpleNamespace(
        itinerary_snapshot=prepared.snapshot,
        brief=SimpleNamespace(brief_id=prepared_certificate["briefId"]),
        proposal_id="proposal_route_repair",
    )
    checkpoint = AgentService._portfolio_failed_component_checkpoint(
        candidate=candidate,
        portfolio_id=portfolio_id,
        source_user_turn_id=source_user_turn_id,
        source_assistant_turn_id=source_assistant_turn_id,
        request_contract_fingerprint=request_fingerprint,
        expected_base_version_id=None,
    )

    assert checkpoint is not None
    assert checkpoint["failureCode"] == "provider_route_matrix_unacceptable"
    certificate = checkpoint["repairScopeCertificate"]
    assert certificate["scopeFingerprint"] == prepared_certificate["scopeFingerprint"]
    assert certificate["adjacentAnchorIds"] == ["campus_segment", "museum_segment"]
    assert certificate["adjacentRouteLedgerKeys"] == prepared_certificate["adjacentRouteLedgerKeys"]
    assert certificate["routeContractFingerprint"] == prepared_certificate["routeContractFingerprint"]
    assert (
        certificate["fullProviderMatrixProofFingerprints"]
        == prepared_certificate["fullProviderMatrixProofFingerprints"]
    )
    assert certificate["sourceAssistantTurnId"] == source_assistant_turn_id
    assert certificate["executionId"] == source_assistant_turn_id
    assert (
        AgentService._validated_route_repair_scope_certificate(
            checkpoint,
            source_user_turn_id=source_user_turn_id,
            source_assistant_turn_id=source_assistant_turn_id,
            portfolio_id=portfolio_id,
            request_contract_fingerprint=request_fingerprint,
            expected_base_version_id=None,
            brief_id=prepared_certificate["briefId"],
            pool_id=prepared_certificate["poolId"],
            planning_slot_id=prepared_certificate["planningSlotId"],
            day_number=prepared_certificate["dayNumber"],
            target_poi=prepared.snapshot["days"][0]["segments"][1]["poi"],
            snapshot=prepared.snapshot,
            transport_mode="transit",
            route_feasibility_service=feasibility,
        )
        is not None
    )

    def resealed_adjacent_certificate(value, *, remove: bool = False):
        tampered_checkpoint = json.loads(json.dumps(checkpoint))
        tampered_certificate = tampered_checkpoint["repairScopeCertificate"]
        if remove:
            tampered_certificate.pop("adjacentAnchorIds", None)
        else:
            tampered_certificate["adjacentAnchorIds"] = value
        tampered_certificate["scopeFingerprint"] = canonical_fingerprint(
            AgentService._route_repair_scope_certificate_material(tampered_certificate)
        )
        tampered_certificate["certificateFingerprint"] = canonical_fingerprint(
            {
                key: item
                for key, item in tampered_certificate.items()
                if key != "certificateFingerprint"
            }
        )
        tampered_checkpoint["repairScopeCertificateFingerprint"] = tampered_certificate[
            "certificateFingerprint"
        ]
        return tampered_checkpoint

    for invalid_checkpoint in (
        resealed_adjacent_certificate(None, remove=True),
        resealed_adjacent_certificate(["campus_segment", "other_segment"]),
        resealed_adjacent_certificate(["museum_segment", "campus_segment"]),
    ):
        assert (
            AgentService._validated_route_repair_scope_certificate(
                invalid_checkpoint,
                source_user_turn_id=source_user_turn_id,
                source_assistant_turn_id=source_assistant_turn_id,
                portfolio_id=portfolio_id,
                request_contract_fingerprint=request_fingerprint,
                expected_base_version_id=None,
                brief_id=prepared_certificate["briefId"],
                pool_id=prepared_certificate["poolId"],
                planning_slot_id=prepared_certificate["planningSlotId"],
                day_number=prepared_certificate["dayNumber"],
                target_poi=prepared.snapshot["days"][0]["segments"][1]["poi"],
                snapshot=prepared.snapshot,
                transport_mode="transit",
                route_feasibility_service=feasibility,
            )
            is None
        )

    logical_occurrence_tampered_snapshot = json.loads(json.dumps(prepared.snapshot))
    logical_occurrence_tampered_snapshot["days"][0]["segments"][0]["id"] = (
        "campus_segment_rebound"
    )
    assert (
        AgentService._validated_route_repair_scope_certificate(
            checkpoint,
            source_user_turn_id=source_user_turn_id,
            source_assistant_turn_id=source_assistant_turn_id,
            portfolio_id=portfolio_id,
            request_contract_fingerprint=request_fingerprint,
            expected_base_version_id=None,
            brief_id=prepared_certificate["briefId"],
            pool_id=prepared_certificate["poolId"],
            planning_slot_id=prepared_certificate["planningSlotId"],
            day_number=prepared_certificate["dayNumber"],
            target_poi=logical_occurrence_tampered_snapshot["days"][0]["segments"][1]["poi"],
            snapshot=logical_occurrence_tampered_snapshot,
            transport_mode="transit",
            route_feasibility_service=feasibility,
        )
        is None
    )

    matrix_tampered_snapshot = json.loads(json.dumps(prepared.snapshot))
    matrix_tampered_snapshot["days"][0]["segments"][1]["semanticMetadata"][
        "routeInsertionMatrixProof"
    ]["routeMatrix"]["previousToCandidate"]["durationSeconds"] += 1
    assert (
        AgentService._validated_route_repair_scope_certificate(
            checkpoint,
            source_user_turn_id=source_user_turn_id,
            source_assistant_turn_id=source_assistant_turn_id,
            portfolio_id=portfolio_id,
            request_contract_fingerprint=request_fingerprint,
            expected_base_version_id=None,
            brief_id=prepared_certificate["briefId"],
            pool_id=prepared_certificate["poolId"],
            planning_slot_id=prepared_certificate["planningSlotId"],
            day_number=prepared_certificate["dayNumber"],
            target_poi=matrix_tampered_snapshot["days"][0]["segments"][1]["poi"],
            snapshot=matrix_tampered_snapshot,
            transport_mode="transit",
            route_feasibility_service=feasibility,
        )
        is None
    )

    contract_tampered_snapshot = json.loads(json.dumps(prepared.snapshot))
    contract_tampered_snapshot["routeDecisionContract"]["detourTolerance"]["maxDetourRatio"] = 0.01
    assert (
        AgentService._validated_route_repair_scope_certificate(
            checkpoint,
            source_user_turn_id=source_user_turn_id,
            source_assistant_turn_id=source_assistant_turn_id,
            portfolio_id=portfolio_id,
            request_contract_fingerprint=request_fingerprint,
            expected_base_version_id=None,
            brief_id=prepared_certificate["briefId"],
            pool_id=prepared_certificate["poolId"],
            planning_slot_id=prepared_certificate["planningSlotId"],
            day_number=prepared_certificate["dayNumber"],
            target_poi=contract_tampered_snapshot["days"][0]["segments"][1]["poi"],
            snapshot=contract_tampered_snapshot,
            transport_mode="transit",
            route_feasibility_service=feasibility,
        )
        is None
    )

    endpoint_tampered_snapshot = json.loads(json.dumps(prepared.snapshot))
    endpoint_tampered_proof = endpoint_tampered_snapshot["days"][0]["segments"][1][
        "semanticMetadata"
    ]["routeInsertionMatrixProof"]
    endpoint_tampered_proof["routeMatrix"]["previousToCandidate"]["fromAmapId"] = "amap_wrong_previous"
    endpoint_tampered_proof["proofFingerprint"] = RouteInsertionScorer.route_proof_fingerprint(
        endpoint_tampered_proof
    )
    # Model a coherent persisted-data tamper: the proof and its ledger reference
    # agree with each other, but the actual Provider-leg endpoint is no longer the
    # current snapshot's authoritative adjacent route pair.
    endpoint_tampered_snapshot["portfolioRouteQuality"]["repairAttemptLedger"]["routeBadCheckpoints"][0][
        "fullProviderMatrixProofFingerprints"
    ] = [endpoint_tampered_proof["proofFingerprint"]]
    endpoint_tampered_checkpoint = AgentService._portfolio_failed_component_checkpoint(
        candidate=SimpleNamespace(
            itinerary_snapshot=endpoint_tampered_snapshot,
            brief=SimpleNamespace(brief_id=prepared_certificate["briefId"]),
            proposal_id="proposal_route_repair",
        ),
        portfolio_id=portfolio_id,
        source_user_turn_id=source_user_turn_id,
        source_assistant_turn_id=source_assistant_turn_id,
        request_contract_fingerprint=request_fingerprint,
        expected_base_version_id=None,
    )
    assert endpoint_tampered_checkpoint is None

    mode_tampered_snapshot = json.loads(json.dumps(prepared.snapshot))
    mode_tampered_proof = mode_tampered_snapshot["days"][0]["segments"][1]["semanticMetadata"][
        "routeInsertionMatrixProof"
    ]
    mode_tampered_proof["routeMatrix"]["previousToCandidate"]["mode"] = "walking"
    mode_tampered_proof["proofFingerprint"] = RouteInsertionScorer.route_proof_fingerprint(
        mode_tampered_proof
    )
    mode_tampered_snapshot["portfolioRouteQuality"]["repairAttemptLedger"]["routeBadCheckpoints"][0][
        "fullProviderMatrixProofFingerprints"
    ] = [mode_tampered_proof["proofFingerprint"]]
    mode_tampered_checkpoint = AgentService._portfolio_failed_component_checkpoint(
        candidate=SimpleNamespace(
            itinerary_snapshot=mode_tampered_snapshot,
            brief=SimpleNamespace(brief_id=prepared_certificate["briefId"]),
            proposal_id="proposal_route_repair",
        ),
        portfolio_id=portfolio_id,
        source_user_turn_id=source_user_turn_id,
        source_assistant_turn_id=source_assistant_turn_id,
        request_contract_fingerprint=request_fingerprint,
        expected_base_version_id=None,
    )
    assert mode_tampered_checkpoint is None

    no_progress = feasibility.prepare(
        prepared.snapshot,
        city="北京",
        transport_mode="transit",
        allow_nearby_search=False,
    )
    assert no_progress.provider_state == "no_progress"
    assert no_progress.repair_attempt_ledger["terminalReason"] == "no_progress_same_scope"

    handoff = AgentService._route_repair_reissue_handoff(
        checkpoint=checkpoint,
        recovery={
            "portfolioId": portfolio_id,
            "proposalId": candidate.proposal_id,
            "proposalSnapshot": prepared.snapshot,
        },
        latest_snapshot=no_progress.snapshot,
        certificate=certificate,
        terminal_no_progress=True,
    )
    bound = AgentService._bind_route_repair_handoff_to_assistant_turn(handoff, reissued_assistant_turn_id)

    assert bound is not None
    rebound_checkpoint = bound["checkpoint"]
    rebound_certificate = rebound_checkpoint["repairScopeCertificate"]
    assert rebound_certificate["sourceAssistantTurnId"] == reissued_assistant_turn_id
    assert rebound_certificate["executionId"] == reissued_assistant_turn_id
    assert rebound_certificate["adjacentAnchorIds"] == ["campus_segment", "museum_segment"]
    assert rebound_certificate["terminalStatus"] == "no_progress"
    assert rebound_checkpoint["repairScopeCertificateFingerprint"] != checkpoint["repairScopeCertificateFingerprint"]
    assert (
        AgentService._validated_route_repair_scope_certificate(
            rebound_checkpoint,
            source_user_turn_id=source_user_turn_id,
            source_assistant_turn_id=source_assistant_turn_id,
            portfolio_id=portfolio_id,
            request_contract_fingerprint=request_fingerprint,
            expected_base_version_id=None,
            brief_id=prepared_certificate["briefId"],
            pool_id=prepared_certificate["poolId"],
            planning_slot_id=prepared_certificate["planningSlotId"],
            day_number=prepared_certificate["dayNumber"],
            target_poi=prepared.snapshot["days"][0]["segments"][1]["poi"],
            snapshot=no_progress.snapshot,
            transport_mode="transit",
            route_feasibility_service=feasibility,
        )
        is None
    )
    assert (
        AgentService._validated_route_repair_scope_certificate(
            rebound_checkpoint,
            source_user_turn_id=source_user_turn_id,
            source_assistant_turn_id=reissued_assistant_turn_id,
            portfolio_id=portfolio_id,
            request_contract_fingerprint=request_fingerprint,
            expected_base_version_id=None,
            brief_id=prepared_certificate["briefId"],
            pool_id=prepared_certificate["poolId"],
            planning_slot_id=prepared_certificate["planningSlotId"],
            day_number=prepared_certificate["dayNumber"],
            target_poi=prepared.snapshot["days"][0]["segments"][1]["poi"],
            snapshot=no_progress.snapshot,
            transport_mode="transit",
            route_feasibility_service=feasibility,
        )
        is not None
    )
    assert {table: table_content_fingerprint(table) for table in protected_tables} == zero_write_before


def _safe_fallback_pipeline_context() -> dict:
    return {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "agentDecisionState": {
            "source": "safe_fallback",
            "accepted": True,
            "primaryAction": "draft_itinerary",
        },
        "requestIntentContract": {
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "requirementLevel": "required",
                    "requiredMin": 1,
                    "maxCount": 2,
                    "allowedDayNumbers": [1, 2],
                }
            ]
        },
        "planningDirective": {
            "type": "draft_itinerary",
            "optionalExperienceBudget": 0,
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校",
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "自由安排",
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
        },
    }


def test_safe_fallback_directive_remains_eligible_after_portfolio_provider_timeout(db_connection):
    service = AgentService(db_connection)

    result = service._validated_controller_draft_slot_fallback(
        _safe_fallback_pipeline_context(),
        TimeoutError("creative portfolio provider timed out"),
    )

    assert result is not None
    assert len(result.day_slots) == 1
    assert len(result.intent_pools) == 1


def test_server_deterministic_directive_remains_eligible_after_portfolio_provider_timeout(db_connection):
    service = AgentService(db_connection)
    context = _safe_fallback_pipeline_context()
    context["agentDecisionState"]["source"] = "deterministic_fast_path"

    result = service._validated_controller_draft_slot_fallback(
        context,
        TimeoutError("creative portfolio provider timed out"),
    )

    assert result is not None
    assert len(result.day_slots) == 1
    assert len(result.intent_pools) == 1


def test_partial_plan_expansion_compiles_server_cardinality_without_controller(db_connection):
    service = AgentService(db_connection)
    requirement = {
        "goalId": "goal_campus",
        "intentType": "campus_visit",
        "requirementLevel": "required",
        "requiredMin": 1,
        "preferredCount": 2,
        "maxCount": 2,
        "allowedDayNumbers": [1, 2],
    }
    context = {
        "retryExecutionPlan": {
            "kind": "expand_partial_portfolio",
            "reasonCode": "persisted_partial_same_root_expansion",
            "controllerAllowed": False,
            "writeBudget": 0,
        },
        "autonomyContext": {
            "resolvedTripDates": {
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
            },
            "observation": {
                "request": {"intentContract": {"requiredIntents": [requirement]}},
                "requirementCoverage": {"required": [requirement]},
            },
        },
    }

    decision = service._retry_recovery_decision(context)

    assert decision.source == "deterministic_fast_path"
    assert decision.controller_full_called is False
    assert decision.controller_lite_called is False
    assert decision.gated_decision.accepted is True
    assert decision.decision.primary_action == "draft_itinerary"
    assert decision.decision.action_directive.type == "draft_itinerary"
    assert decision.decision.expected_outcome["taskRoute"] == "portfolio_expansion"
    assert decision.decision.expected_outcome["writeBudget"] == 0


def test_resumable_checkpoint_uses_deterministic_draft_decision_without_controller(db_connection):
    service = AgentService(db_connection)
    context = _safe_fallback_pipeline_context()
    context["retryExecutionPlan"] = {
        "kind": "resume_incomplete_stage",
        "reasonCode": "persisted_incomplete_stage_available",
        "controllerAllowed": False,
        "writeBudget": 1,
    }

    decision = service._retry_recovery_decision(context)

    assert decision.source == "deterministic_fast_path"
    assert decision.controller_full_called is False
    assert decision.controller_lite_called is False
    assert decision.gated_decision.accepted is True
    assert decision.decision.primary_action == "draft_itinerary"
    assert decision.decision.action_directive.type == "draft_itinerary"


def test_resumable_attempt_preserves_portfolio_checkpoint_payload(db_connection):
    session = ConversationService(db_connection).create_session("北京", "portfolio checkpoint")
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_checkpoint_source_user",
        role="user",
        content="今年国庆北京高校两日游，晚上看夜景",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_checkpoint_source_assistant",
        role="assistant",
        content="正在等待地点候选",
        index=2,
    )
    checkpoint = {
        "mode": "staged_initial_pipeline_failed",
        "resultState": "waiting_for_poi_grounding",
        "initialPlan": {
            "reply": "checkpoint",
            "mode": "initial_plan",
            "daySlots": [],
            "intentPools": [],
            "warnings": [],
        },
        "pipelineContext": {"effectiveUserMessage": "今年国庆北京高校两日游，晚上看夜景"},
        "constraintLedger": {"schemaVersion": "constraint-ledger-v1", "hardGoals": []},
        "creativePortfolio": {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [],
        },
        "groundingCheckpoint": {"poolReports": [{"poolId": "pool_day_2_campus"}]},
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (json.dumps(checkpoint, ensure_ascii=False), "turn_checkpoint_source_assistant"),
    )
    db_connection.commit()

    attempt = PlanningAttemptResumeService(db_connection).resume_context_for_retry(
        session.session_id,
        "再试一遍",
    )

    assert attempt is not None
    assert attempt["constraintLedger"]["schemaVersion"] == "constraint-ledger-v1"
    assert attempt["creativePortfolio"]["schemaVersion"] == "initial-creative-portfolio-v1"
    assert attempt["groundingCheckpoint"]["poolReports"][0]["poolId"] == "pool_day_2_campus"


@pytest.fixture
def db_connection():
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _insert_turn(db_connection, *, session_id: str, turn_id: str, role: str, content: str, index: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db_connection.execute(
        """INSERT INTO conversation_turns (
            id, session_id, role, content, turn_index, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (turn_id, session_id, role, content, index, now, now),
    )


def _insert_portfolio(
    db_connection,
    *,
    session_id: str,
    source_user_turn_id: str,
    source_assistant_turn_id: str = "turn_assistant_source",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    summary = {
        "visibleProposalIds": ["proposal_visible"],
        "proposalIds": ["proposal_visible"],
    }
    db_connection.execute(
        """INSERT INTO agent_plan_portfolios (
            id, session_id, source_user_turn_id, source_assistant_turn_id,
            expected_base_version_id, source_observation_fingerprint,
            request_contract_fingerprint, status, summary_json, expires_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'awaiting_selection', ?, ?, ?, ?)""",
        (
            "portfolio_recoverable",
            session_id,
            source_user_turn_id,
            source_assistant_turn_id,
            "o" * 64,
            "r" * 64,
            json.dumps(summary),
            expires,
            now,
            now,
        ),
    )
    db_connection.execute(
        """INSERT INTO agent_plan_proposals (
            id, portfolio_id, choice_id, rank_index, status, brief_json,
            snapshot_json, score_json, verifier_json, evidence_json,
            canonical_signature, generation_lineage_json, created_at, updated_at
        ) VALUES (?, ?, ?, 0, 'offered', ?, '{}', ?, ?, '{}', ?, '{}', ?, ?)""",
        (
            "proposal_visible",
            "portfolio_recoverable",
            "portfolio_choice_proposal_visible",
            json.dumps(
                {
                    "briefId": "brief_visible",
                    "title": "经典文化主线",
                    "primaryAxis": "classic",
                }
            ),
            json.dumps({"hardConstraintPassed": True}),
            json.dumps({"passed": True}),
            "c" * 64,
            now,
            now,
        ),
    )
    db_connection.commit()


def _insert_active_partial(db_connection, *, session_id: str, plan_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "status": "partial",
        "days": [
            {"id": "day_partial_1", "dayNumber": 1, "segments": []},
            {"id": "day_partial_2", "dayNumber": 2, "segments": []},
        ],
        "portfolioSelectionContext": {
            "planningSelectionRootTurnId": "turn_partial_choices",
            "rootPortfolioId": "portfolio_partial_retry",
            "focusBriefId": "brief_partial_retry",
            "requestContractFingerprint": "p" * 64,
        },
        "portfolioPartialTimeline": {
            "portfolioId": "portfolio_partial_retry",
            "briefId": "brief_partial_retry",
        },
        "portfolioPendingSlots": [
            {
                "briefId": "brief_partial_retry",
                "poolId": "meal_pool_retry",
                "planningSlotId": "meal_slot_retry",
                "dayNumber": 2,
            }
        ],
    }
    db_connection.execute(
        """INSERT INTO itinerary_versions (
            id, session_id, plan_id, version_number, source_type,
            source_turn_id, source_patch_id, snapshot_json, created_at
        ) VALUES (?, ?, ?, 1, 'agent', NULL, NULL, ?, ?)""",
        (
            "version_partial_retry",
            session_id,
            plan_id,
            json.dumps(snapshot),
            now,
        ),
    )
    db_connection.execute(
        "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
        ("version_partial_retry", session_id),
    )
    _insert_turn(
        db_connection,
        session_id=session_id,
        turn_id="turn_partial_choices",
        role="assistant",
        content="请选择第二天午餐",
        index=1,
    )
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "choiceOptions": [
                        {
                            "id": "choice_partial_meal",
                            "kind": "portfolio_density_candidate",
                            "briefId": "brief_partial_retry",
                            "poolId": "meal_pool_retry",
                            "planningSlotId": "meal_slot_retry",
                            "dayNumber": 2,
                            "planningSelectionRootTurnId": "turn_partial_choices",
                            "rootPortfolioId": "portfolio_partial_retry",
                            "focusBriefId": "brief_partial_retry",
                            "requestContractFingerprint": "p" * 64,
                            "candidateRecordId": "candidate_partial_meal",
                            "amapId": "amap_partial_meal",
                            "label": "Day 2：北京特色午餐",
                        }
                    ]
                }
            ),
            "turn_partial_choices",
        ),
    )
    db_connection.commit()


def test_retry_semantic_classifier_returns_typed_intents_without_selecting_actions():
    classifier = RetrySemanticClassifier()

    assert classifier.classify("重试一遍").intent_class == "retry_contextual"
    assert classifier.classify("再来一遍").intent_class == "retry_contextual"
    for message in (
        "继续生成其他方案",
        "继续新增一个方案",
        "继续新增其他方案",
        "增加其他方案",
        "继续生成更多方案",
        "继续出方案",
        "多给几个方案",
        "再多做一套",
        "再来几套",
        "还想看别的方案",
        "刚才失败了，请继续生成其他方案",
    ):
        assert classifier.classify(message).intent_class == "continue_plan_expansion"
    for message in (
        "不要继续生成其他方案",
        "不需要继续生成其他方案",
        "请不要“继续生成其他方案”",
        "我只想查看方案，不继续生成",
        "“继续生成其他方案”",
        "继续生成其他方案时报错",
        "继续生成其他方案失败",
        "继续生成其他方案超时",
        "故障：继续生成其他方案",
        "讨论“继续生成其他方案”这个按钮",
        "请问为什么继续生成方案",
        "我想知道为什么新增其他方案",
        "我不想生成其他方案",
        "不需要多给几个方案",
        "我只是讨论继续生成其他方案，不要执行",
    ):
        assert classifier.classify(message).intent_class == "not_retry"
    assert classifier.classify("只修复 Day 2 午餐").intent_class == "retry_failed_component"
    assert classifier.classify("全部从头重新生成，不要上一版").intent_class == "regenerate_from_scratch"
    assert classifier.classify("继续补齐刚才的待选地点").intent_class == "continue_pending_choice"
    assert classifier.classify("按原要求改成三天").intent_class == "modify_request"
    assert (
        classifier.classify("重试一次，将第一天美术馆修改为清华美术馆").intent_class
        == "modify_request"
    )
    assert classifier.classify("北京有什么夜景").intent_class == "not_retry"


def test_exact_pending_choice_without_authoritative_schedule_fails_before_write(db_connection):
    session = ConversationService(db_connection).create_session("北京", "pending schedule")
    _insert_active_partial(
        db_connection,
        session_id=session.session_id,
        plan_id=session.active_plan_id,
    )
    service = AgentService(db_connection)
    before = (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
    )

    with pytest.raises(HTTPException) as rejected:
        service._pending_slot_timeline_mutation(
            session.session_id,
            {
                "selectedPlanningCandidate": {
                    "briefId": "brief_partial_retry",
                    "poolId": "meal_pool_retry",
                    "planningSlotId": "meal_slot_retry",
                    "dayNumber": 2,
                    "amapId": "amap_partial_meal",
                }
            },
            selection_source="user_chat_choice",
        )

    assert rejected.value.detail["code"] == "portfolio_pending_slot_schedule_unresolved"
    after = (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
    )
    assert after == before


def test_generic_retry_reuses_compatible_visible_portfolio_without_regeneration(db_connection):
    session = ConversationService(db_connection).create_session("北京", "retry portfolio")
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_user_source",
        role="user",
        content="北京两日游，参观高校并体验夜景",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_assistant_source",
        role="assistant",
        content="请选择已验证方案",
        index=2,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_user_retry",
        role="user",
        content="重试一遍",
        index=3,
    )
    _insert_portfolio(
        db_connection,
        session_id=session.session_id,
        source_user_turn_id="turn_user_source",
    )

    intent = RetrySemanticClassifier().classify("重试一遍")
    plan = RetryExecutionPlanService(db_connection).resolve(
        session_id=session.session_id,
        intent=intent,
        latest_user_turn_id="turn_user_retry",
    )

    assert plan.kind == "resume_portfolio"
    assert plan.reason_code == "compatible_visible_portfolio"
    assert plan.portfolio_id == "portfolio_recoverable"
    assert plan.allowed_actions == ["ask_user"]
    assert plan.write_budget == 0
    assert plan.controller_allowed is False
    assert [item["choiceId"] for item in plan.choice_options] == [
        "portfolio_choice_proposal_visible"
    ]


def test_superseded_portfolio_source_assistant_is_not_reoffered(db_connection):
    session = ConversationService(db_connection).create_session("北京", "retry superseded")
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_user_source",
        role="user",
        content="北京两日游，参观高校",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_assistant_source",
        role="assistant",
        content="请选择已验证方案",
        index=2,
    )
    _insert_portfolio(
        db_connection,
        session_id=session.session_id,
        source_user_turn_id="turn_user_source",
    )
    db_connection.execute(
        "UPDATE conversation_turns SET status = 'superseded' WHERE id = ?",
        ("turn_assistant_source",),
    )
    db_connection.commit()

    plan = RetryExecutionPlanService(db_connection).resolve(
        session_id=session.session_id,
        intent=RetrySemanticClassifier().classify("重试一遍"),
    )

    assert plan.kind != "resume_portfolio"
    assert "portfolio_source_assistant_superseded" in plan.compatibility_failures


def test_modified_request_and_explicit_regeneration_do_not_resume_old_portfolio(db_connection):
    session = ConversationService(db_connection).create_session("北京", "retry changed")
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_user_source_changed",
        role="user",
        content="北京两日游，参观高校并体验夜景",
        index=1,
    )
    _insert_portfolio(
        db_connection,
        session_id=session.session_id,
        source_user_turn_id="turn_user_source_changed",
    )

    resolver = RetryExecutionPlanService(db_connection)
    changed = resolver.resolve(
        session_id=session.session_id,
        intent=RetrySemanticClassifier().classify("按原要求改成三天"),
        latest_user_turn_id="turn_changed",
    )
    regenerate = resolver.resolve(
        session_id=session.session_id,
        intent=RetrySemanticClassifier().classify("全部从头重新生成，不要上一版"),
        latest_user_turn_id="turn_regenerate",
    )

    assert changed.kind == "start_modified_request"
    assert changed.portfolio_id is None
    assert regenerate.kind == "regenerate_full"
    assert regenerate.portfolio_id is None
    assert regenerate.controller_allowed is True


def test_expired_or_fingerprint_mismatched_portfolio_is_not_reused(db_connection):
    classifier = RetrySemanticClassifier()
    resolver = RetryExecutionPlanService(db_connection)
    session = ConversationService(db_connection).create_session(
        "北京", "retry incompatible portfolio"
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_incompatible_source",
        role="user",
        content="北京两日游，参观高校",
        index=1,
    )
    _insert_portfolio(
        db_connection,
        session_id=session.session_id,
        source_user_turn_id="turn_incompatible_source",
    )
    fingerprint_mismatch = resolver.resolve(
        session_id=session.session_id,
        intent=classifier.classify("重试一遍"),
        request_contract_fingerprint="x" * 64,
    )
    assert fingerprint_mismatch.kind == "regenerate_full"
    assert "portfolio_request_fingerprint_mismatch" in (
        fingerprint_mismatch.compatibility_failures
    )

    db_connection.execute(
        "UPDATE agent_plan_portfolios SET expires_at = ? WHERE id = ?",
        (
            (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "portfolio_recoverable",
        ),
    )
    db_connection.commit()
    expired = resolver.resolve(
        session_id=session.session_id,
        intent=classifier.classify("重试一遍"),
        request_contract_fingerprint="r" * 64,
    )
    assert expired.kind == "regenerate_full"
    assert "portfolio_expired" in expired.compatibility_failures


def test_retry_resolution_distinguishes_failed_component_partial_and_no_checkpoint(
    db_connection,
):
    resolver = RetryExecutionPlanService(db_connection)
    classifier = RetrySemanticClassifier()

    failure_session = ConversationService(db_connection).create_session(
        "北京", "retry exact failure"
    )
    _insert_turn(
        db_connection,
        session_id=failure_session.session_id,
        turn_id="turn_failure_request",
        role="user",
        content="北京两日游，每天午餐体验特色美食",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=failure_session.session_id,
        turn_id="turn_failure_assistant",
        role="assistant",
        content="午餐路线需要修复",
        index=2,
    )
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "failedComponentCheckpoint": {
                        "failureCode": "meal_detour_high",
                        "portfolioId": "portfolio_failed_meal",
                        "proposalId": "proposal_failed_meal",
                        "briefId": "brief_failed_meal",
                        "poolId": "pool_failed_meal",
                        "planningSlotId": "slot_failed_meal",
                        "dayNumber": 2,
                        "segmentId": "segment_failed_meal",
                        "amapId": "amap_failed_meal",
                        "sourceUserTurnId": "turn_failure_request",
                        "requestContractFingerprint": "f" * 64,
                    }
                }
            ),
            "turn_failure_assistant",
        ),
    )
    db_connection.commit()
    failure_plan = resolver.resolve(
        session_id=failure_session.session_id,
        intent=classifier.classify("只修复第二天午餐"),
    )
    assert failure_plan.kind == "repair_last_failure"
    assert failure_plan.preconditions["planningSlotId"] == "slot_failed_meal"
    assert failure_plan.controller_allowed is False
    assert failure_plan.write_budget == 0

    partial_session = ConversationService(db_connection).create_session(
        "北京", "retry active partial"
    )
    _insert_active_partial(
        db_connection,
        session_id=partial_session.session_id,
        plan_id=partial_session.active_plan_id,
    )
    scoped_partial = resolver.resolve(
        session_id=partial_session.session_id,
        intent=classifier.classify("继续补齐刚才的待选地点"),
    )
    generic_partial = resolver.resolve(
        session_id=partial_session.session_id,
        intent=classifier.classify("重试一遍"),
    )
    assert scoped_partial.kind == "continue_partial_slot"
    assert scoped_partial.preconditions["pendingSlotCount"] == 1
    assert generic_partial.kind == "continue_partial_slot"
    assert generic_partial.reason_code == (
        "active_partial_reoffers_exact_persisted_slot_choices"
    )
    assert generic_partial.choice_options[0]["id"] == "choice_partial_meal"

    empty_session = ConversationService(db_connection).create_session(
        "北京", "retry no checkpoint"
    )
    _insert_turn(
        db_connection,
        session_id=empty_session.session_id,
        turn_id="turn_empty_request",
        role="user",
        content="北京两日游，参观高校",
        index=1,
    )
    no_checkpoint = resolver.resolve(
        session_id=empty_session.session_id,
        intent=classifier.classify("重试一遍"),
    )
    assert no_checkpoint.kind == "regenerate_full"
    assert no_checkpoint.reason_code == "no_recoverable_checkpoint"
    assert no_checkpoint.compatibility_failures == ["awaiting_portfolio_missing"]

    checkpoint_session = ConversationService(db_connection).create_session(
        "北京", "retry persisted checkpoint"
    )
    _insert_turn(
        db_connection,
        session_id=checkpoint_session.session_id,
        turn_id="turn_checkpoint_request",
        role="user",
        content="今年国庆北京高校两日游，晚上看夜景",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=checkpoint_session.session_id,
        turn_id="turn_checkpoint_assistant",
        role="assistant",
        content="上次安全阶段未完成",
        index=2,
    )
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "mode": "staged_initial_pipeline_failed",
                    "resultState": "waiting_for_poi_grounding",
                    "initialPlan": {
                        "reply": "checkpoint",
                        "mode": "initial_plan",
                        "daySlots": [],
                        "intentPools": [],
                        "warnings": [],
                    },
                    "pipelineContext": {"effectiveUserMessage": "今年国庆北京高校两日游，晚上看夜景"},
                    "resultVersionId": None,
                    "terminalStatus": "failed",
                },
                ensure_ascii=False,
            ),
            "turn_checkpoint_assistant",
        ),
    )
    db_connection.commit()

    checkpoint_plan = resolver.resolve(
        session_id=checkpoint_session.session_id,
        intent=classifier.classify("再试一遍"),
    )
    assert checkpoint_plan.kind == "resume_incomplete_stage"
    assert checkpoint_plan.controller_allowed is False
    assert checkpoint_plan.preconditions["checkpointReused"] is True

    db_connection.execute(
        "UPDATE conversation_turns SET status = 'superseded' WHERE id = ?",
        ("turn_checkpoint_assistant",),
    )
    db_connection.commit()
    assert (
        PlanningAttemptResumeService(db_connection).latest_resumable_attempt(
            checkpoint_session.session_id
        )
        is None
    )
    superseded_plan = resolver.resolve(
        session_id=checkpoint_session.session_id,
        intent=classifier.classify("再试一遍"),
    )
    assert superseded_plan.kind == "regenerate_full"
    assert superseded_plan.preconditions.get("checkpointReused") is not True

    empty_checkpoint_session = ConversationService(db_connection).create_session(
        "北京", "retry empty checkpoint"
    )
    _insert_turn(
        db_connection,
        session_id=empty_checkpoint_session.session_id,
        turn_id="turn_empty_checkpoint_request",
        role="user",
        content="今年国庆北京高校两日游，晚上看夜景",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=empty_checkpoint_session.session_id,
        turn_id="turn_empty_checkpoint_assistant",
        role="assistant",
        content="上次执行失败",
        index=2,
    )
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "mode": "staged_initial_pipeline_failed",
                    "resultState": "waiting_for_poi_grounding",
                    "initialPlan": {
                        "reply": "checkpoint",
                        "mode": "initial_plan",
                        "daySlots": [],
                        "intentPools": [],
                        "warnings": [],
                    },
                    "pipelineContext": {},
                    "planningDirective": None,
                    "resultVersionId": None,
                    "terminalStatus": "failed",
                },
                ensure_ascii=False,
            ),
            "turn_empty_checkpoint_assistant",
        ),
    )
    db_connection.commit()

    assert (
        PlanningAttemptResumeService(db_connection).latest_resumable_attempt(
            empty_checkpoint_session.session_id
        )
        is None
    )
    empty_checkpoint_plan = resolver.resolve(
        session_id=empty_checkpoint_session.session_id,
        intent=classifier.classify("重试"),
    )
    assert empty_checkpoint_plan.kind == "regenerate_full"
    assert empty_checkpoint_plan.reason_code == "no_recoverable_checkpoint"
    assert empty_checkpoint_plan.preconditions.get("checkpointReused") is not True


@pytest.mark.parametrize(
    "payload_override",
    [
        {"resultVersionId": "ver_already_created", "terminalStatus": "failed"},
        {"resultVersionId": None, "terminalStatus": "completed"},
    ],
)
def test_retry_services_share_payload_version_and_terminal_eligibility(
    db_connection,
    payload_override,
):
    session = ConversationService(db_connection).create_session(
        "北京",
        "shared retry eligibility",
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_shared_retry_request",
        role="user",
        content="今年国庆北京高校两日游，晚上看夜景",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_shared_retry_assistant",
        role="assistant",
        content="上次安全阶段未完成",
        index=2,
    )
    checkpoint = {
        "mode": "staged_initial_pipeline_failed",
        "resultState": "waiting_for_poi_grounding",
        "initialPlan": {
            "reply": "checkpoint",
            "mode": "initial_plan",
            "daySlots": [],
            "intentPools": [],
            "warnings": [],
        },
        "pipelineContext": {
            "effectiveUserMessage": "今年国庆北京高校两日游，晚上看夜景"
        },
        **payload_override,
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (
            json.dumps(checkpoint, ensure_ascii=False),
            "turn_shared_retry_assistant",
        ),
    )
    db_connection.commit()

    assert (
        PlanningAttemptResumeService(db_connection).latest_resumable_attempt(
            session.session_id
        )
        is None
    )
    retry_plan = RetryExecutionPlanService(db_connection).resolve(
        session_id=session.session_id,
        intent=RetrySemanticClassifier().classify("重试"),
    )
    assert retry_plan.kind == "regenerate_full"
    assert retry_plan.preconditions.get("checkpointReused") is not True


def test_active_partial_does_not_reoffer_choice_from_another_planning_root(db_connection):
    session = ConversationService(db_connection).create_session("北京", "retry stale partial")
    _insert_active_partial(
        db_connection,
        session_id=session.session_id,
        plan_id=session.active_plan_id,
    )
    row = db_connection.execute(
        "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
        ("turn_partial_choices",),
    ).fetchone()
    payload = json.loads(row["agent_response_json"])
    payload["choiceOptions"][0]["planningSelectionRootTurnId"] = "turn_stale_root"
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (json.dumps(payload), "turn_partial_choices"),
    )
    db_connection.commit()

    plan = RetryExecutionPlanService(db_connection).resolve(
        session_id=session.session_id,
        intent=RetrySemanticClassifier().classify("重试一遍"),
    )

    assert plan.kind == "ask_retry_scope"
    assert plan.reason_code == "active_partial_persisted_choices_missing"


def test_public_agent_turn_reoffers_persisted_portfolio_without_controller_or_pipeline(
    db_connection,
):
    class FailIfCalledProvider:
        def __getattr__(self, name):
            raise AssertionError(f"provider must not be called during portfolio recovery: {name}")

    session = ConversationService(db_connection).create_session("北京", "retry public")
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_public_source",
        role="user",
        content="北京两日游，参观高校并体验夜景",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_public_assistant",
        role="assistant",
        content="请选择已验证方案",
        index=2,
    )
    _insert_portfolio(
        db_connection,
        session_id=session.session_id,
        source_user_turn_id="turn_public_source",
        source_assistant_turn_id="turn_public_assistant",
    )

    before = {
        "portfolio": db_connection.execute(
            "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
        "proposal": db_connection.execute(
            """SELECT COUNT(*) FROM agent_plan_proposals q
            JOIN agent_plan_portfolios p ON p.id = q.portfolio_id
            WHERE p.session_id = ?""",
            (session.session_id,),
        ).fetchone()[0],
        "version": db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0],
    }

    response = AgentService(db_connection, provider=FailIfCalledProvider()).send_message(
        session.session_id,
        AgentMessageRequest(content="重试一遍"),
    )

    assert response.terminal_status == "needs_confirmation"
    assert response.version is None
    assert [item["id"] for item in response.assistant_turn.choice_options] == [
        "portfolio_choice_proposal_visible"
    ]
    stored = json.loads(
        db_connection.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
            (response.assistant_turn.id,),
        ).fetchone()[0]
    )
    assert stored["retryIntent"]["intentClass"] == "retry_contextual"
    assert stored["retryExecutionPlan"]["kind"] == "resume_portfolio"
    assert stored["controllerFullCallCount"] == 0
    assert stored["controllerLiteCallCount"] == 0
    assert stored["stagedPipelineCallCount"] == 0
    assert stored["newPortfolioDelta"] == 0
    assert stored["newProposalDelta"] == 0
    assert stored["amapPlaceCallDelta"] == 0
    assert stored["amapRouteCallDelta"] == 0
    assert stored["webSearchCallDelta"] == 0
    assert stored["versionDelta"] == 0
    assert stored["patchDelta"] == 0
    assert stored["routeWriteDelta"] == 0
    assert db_connection.execute(
        "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?",
        (session.session_id,),
    ).fetchone()[0] == before["portfolio"]
    assert db_connection.execute(
        """SELECT COUNT(*) FROM agent_plan_proposals q
        JOIN agent_plan_portfolios p ON p.id = q.portfolio_id
        WHERE p.session_id = ?""",
        (session.session_id,),
    ).fetchone()[0] == before["proposal"]
    assert db_connection.execute(
        "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
        (session.session_id,),
    ).fetchone()[0] == before["version"]


def test_exact_meal_repair_without_sealed_provider_proof_fails_closed_zero_write(db_connection):
    session = ConversationService(db_connection).create_session(
        "北京", "exact meal repair"
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_repair_source_user",
        role="user",
        content="北京一日游，午餐体验当地美食",
        index=1,
    )
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_repair_source_assistant",
        role="assistant",
        content="午餐路线需要修复",
        index=2,
    )
    now = datetime.now(timezone.utc).isoformat()
    fingerprint = "f" * 64
    brief = {
        "briefId": "brief_repair_meal",
        "title": "午餐顺路版",
        "primaryAxis": "classic",
        "dayRoles": [{"dayNumber": 1, "role": "城市文化"}],
    }
    snapshot = {
        "id": "plan_repair_meal",
        "portfolioTransportPreference": "transit",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "segment_repair_meal",
                        "kind": "meal",
                        "startTime": "12:00",
                        "endTime": "13:00",
                        "poi": {
                            "id": "amap_old_meal",
                            "amapId": "amap_old_meal",
                            "name": "偏绕午餐",
                            "source": "amap-place-search",
                            "providerType": "中餐厅",
                            "longitude": 116.5,
                            "latitude": 39.9,
                        },
                        "semanticMetadata": {
                            "creativeBriefId": "brief_repair_meal",
                            "poolId": "pool_repair_meal",
                            "planningSlotId": "slot_repair_meal",
                            "intentType": "meal",
                            "routeAnchor": True,
                            "groundingStatus": "selected",
                        },
                    }
                ],
            }
        ],
        "rootPortfolioId": "portfolio_repair_meal",
        "portfolioSelectionContext": {
            "planningSelectionRootTurnId": "turn_repair_source_user",
            "rootPortfolioId": "portfolio_repair_meal",
            "requestContractFingerprint": fingerprint,
        },
        "portfolioRouteQuality": {
            "routeQualityIssues": [
                {
                    "code": "meal_detour_high",
                    "failureCode": "meal_detour_high",
                    "dayNumber": 1,
                    "mealSegmentId": "segment_repair_meal",
                    "mealBriefId": "brief_repair_meal",
                    "mealPoolId": "pool_repair_meal",
                    "mealPlanningSlotId": "slot_repair_meal",
                    "mealAmapId": "amap_old_meal",
                }
            ]
        },
    }
    protected_tables = ("itinerary_versions", "itinerary_patches", "route_options")
    before = {
        table: db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected_tables
    }
    checkpoint = AgentService._portfolio_failed_component_checkpoint(
        candidate=SimpleNamespace(
            itinerary_snapshot=snapshot,
            brief=SimpleNamespace(brief_id="brief_repair_meal"),
            proposal_id="proposal_failed_meal",
        ),
        portfolio_id="portfolio_repair_meal",
        source_user_turn_id="turn_repair_source_user",
        source_assistant_turn_id="turn_repair_source_assistant",
        request_contract_fingerprint=fingerprint,
        expected_base_version_id=None,
    )
    # This deliberately synthetic route issue has no server-issued full
    # Provider matrix proof.  It must not become an exact repair capability.
    assert checkpoint is None
    source_payload = {
        "failedComponentCheckpoint": None,
        "failedComponentRecoveryState": {
            "portfolioId": "portfolio_repair_meal",
            "proposalId": "proposal_failed_meal",
            "brief": brief,
            "proposalSnapshot": snapshot,
        },
        "constraintLedger": {
            "schemaVersion": "constraint-ledger-v1",
            "city": "北京",
            "dayCount": 1,
            "sourceFingerprint": fingerprint,
        },
    }
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (json.dumps(source_payload), "turn_repair_source_assistant"),
    )
    db_connection.execute(
        """INSERT INTO agent_plan_portfolios (
            id, session_id, source_user_turn_id, source_assistant_turn_id,
            expected_base_version_id, source_observation_fingerprint,
            request_contract_fingerprint, status, summary_json, expires_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'failed', '{}', ?, ?, ?)""",
        (
            "portfolio_repair_meal",
            session.session_id,
            "turn_repair_source_user",
            "turn_repair_source_assistant",
            "o" * 64,
            fingerprint,
            (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            now,
            now,
        ),
    )
    db_connection.commit()

    service = AgentService(db_connection)
    plan = RetryExecutionPlanService(db_connection).resolve(
        session_id=session.session_id,
        intent=RetrySemanticClassifier().classify("只修复午餐"),
    )
    choices, reply, telemetry = service._prepare_failed_component_repair(
        session_id=session.session_id,
        retry_execution_plan=plan.model_dump(by_alias=True),
    )

    assert plan.kind == "ask_retry_scope"
    assert plan.reason_code == "failed_component_identity_missing"
    assert choices == []
    assert telemetry["status"] == "rejected"
    assert telemetry["reason"] == "repair_source_assistant_turn_missing"
    assert "无法安全定位" in reply
    assert telemetry["versionDelta"] == 0
    assert telemetry["patchDelta"] == 0
    assert telemetry["routeWriteDelta"] == 0
    assert "failedComponentRecoveryState" in source_payload
    assert "failedComponentCheckpoint" in source_payload
    assert {
        table: db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in protected_tables
    } == before

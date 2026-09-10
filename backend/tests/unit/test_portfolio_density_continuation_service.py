from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.services.portfolio_density_continuation_service import (
    DENSITY_MANUAL_SEARCH_ACTION,
    DENSITY_NEARBY_ACTION,
    DENSITY_REFRESH_ACTION,
    PortfolioDensityContinuationError,
    PortfolioDensityContinuationService,
)


def _connection() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE conversation_turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            turn_index INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            agent_request_json TEXT,
            agent_response_json TEXT,
            itinerary_version_id TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE conversation_sessions (
            id TEXT PRIMARY KEY,
            active_version_id TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE itinerary_versions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            snapshot_json TEXT NOT NULL
        )
        """
    )
    db.execute(
        "INSERT INTO conversation_sessions (id, active_version_id) VALUES ('sess_density', 'version_partial')"
    )
    return db


def _insert_pause(
    db: sqlite3.Connection,
    *,
    option: dict[str, object],
    created_at: str | None = None,
) -> None:
    source_request = "北京两日游，参观大学和博物馆并品尝沿途美食"
    request = {
        "currentUserTurnId": "turn_source_user",
        "effectiveUserMessage": source_request,
        "sourceUserRequest": source_request,
        "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01", "2026-10-02"]},
        "requestIntentContract": {"requiredIntents": [{"goalId": "goal_meal", "intentType": "meal"}]},
        "planningDirective": {"type": "draft_itinerary", "dayStrategies": []},
    }
    response = {
        "choiceOptions": [option],
        "initialPlan": {"daySlots": [{"slotId": "slot_day_2"}], "intentPools": []},
        "pipelineContext": {
            "resolvedTripDates": request["resolvedTripDates"],
            "planningSelectionRootTurnId": "turn_original_density_root",
        },
    }
    group = next(
        (item for item in option.get("densityGroups", []) if isinstance(item, dict)),
        {
            "briefId": option.get("briefId"),
            "poolId": option.get("poolId"),
            "planningSlotId": option.get("planningSlotId"),
            "dayNumber": option.get("dayNumber"),
            "intentType": option.get("intentType"),
        },
    )
    db.execute(
        """INSERT INTO itinerary_versions (id, session_id, snapshot_json)
        VALUES ('version_partial', 'sess_density', ?)""",
        (
            json.dumps(
                {
                    "days": [{"dayNumber": int(group.get("dayNumber") or 2), "segments": []}],
                    "portfolioPendingSlots": [{
                        "briefId": group.get("briefId"),
                        "poolId": group.get("poolId"),
                        "planningSlotId": group.get("planningSlotId") or group.get("slotId"),
                        "dayNumber": group.get("dayNumber"),
                        "intentType": group.get("intentType"),
                    }],
                    "portfolioSelectionContext": {
                        "planningSelectionRootTurnId": option.get("planningSelectionRootTurnId"),
                        "rootPortfolioId": option.get("rootPortfolioId"),
                        "focusBriefId": option.get("focusBriefId"),
                        "requestContractFingerprint": option.get("requestContractFingerprint"),
                    },
                },
                ensure_ascii=False,
            ),
        ),
    )
    db.execute(
        """
        INSERT INTO conversation_turns (
            id, session_id, role, turn_index, status, created_at,
            agent_request_json, agent_response_json
        ) VALUES (?, 'sess_density', 'assistant', 1, 'active', ?, ?, ?)
        """,
        (
            "turn_density_pause",
            created_at or datetime.now(timezone.utc).isoformat(),
            json.dumps(request, ensure_ascii=False),
            json.dumps(response, ensure_ascii=False),
        ),
    )
    db.commit()


def test_checkpoint_restores_original_request_and_promotes_nested_scope() -> None:
    db = _connection()
    option = {
        "id": "density_refresh",
        "action": "retry_model_planning",
        "kind": "portfolio_density_retry",
        "retryMode": "refresh_candidates",
        "label": "刷新按钮文字不能成为需求",
        "planningSelectionRootTurnId": "turn_original_density_root",
        "rootPortfolioId": "portfolio_root",
        "focusBriefId": "brief_local",
        "requestContractFingerprint": "f" * 64,
        "expectedBaseVersionId": "version_partial",
        "densityGroups": [
            {
                "briefId": "brief_local",
                "poolId": "pool_day_2_walk",
                "slotId": "slot_day_2",
                "dayNumber": 2,
                "intentType": "area_walk",
            }
        ],
    }
    _insert_pause(db, option=option)

    checkpoint = PortfolioDensityContinuationService(db).load_checkpoint(
        session_id="sess_density",
        source_assistant_turn_id="turn_density_pause",
        option=option,
    )

    assert checkpoint.payload["normalizedAction"] == DENSITY_REFRESH_ACTION
    assert checkpoint.payload["effectiveUserMessage"].startswith("北京两日游")
    assert "按钮文字" not in checkpoint.payload["effectiveUserMessage"]
    assert checkpoint.payload["briefId"] == "brief_local"
    assert checkpoint.payload["planningSlotId"] == "slot_day_2"
    assert checkpoint.payload["dayNumber"] == 2
    assert (
        checkpoint.payload["planningSelectionRootTurnId"]
        == "turn_original_density_root"
    )
    assert checkpoint.payload["checkpointFingerprint"] == checkpoint.fingerprint
    assert checkpoint.payload["rootPortfolioId"] == "portfolio_root"
    assert checkpoint.payload["focusBriefId"] == "brief_local"
    assert checkpoint.payload["requestContractFingerprint"] == "f" * 64
    assert checkpoint.payload["expectedBaseVersionId"] == "version_partial"
    assert checkpoint.payload["initialPlan"]["daySlots"] == [
        {
            "slotId": "slot_day_2",
            "dayNumber": 2,
            "date": None,
            "timeWindow": "",
            "startTime": "",
            "durationMinutes": 0,
            "kind": "area_walk",
            "rawNeed": "area_walk",
            "routeAnchor": True,
            "priority": 0,
            "notes": "由持久化 opaque choice 的 exact pending slot 恢复。",
        }
    ]
    assert checkpoint.payload["initialPlan"]["intentPools"][0]["briefId"] == "brief_local"
    assert checkpoint.payload["initialPlan"]["intentPools"][0]["poolId"] == "pool_day_2_walk"
    assert checkpoint.payload["initialPlan"]["intentPools"][0]["assignToSlots"] == ["slot_day_2"]
    assert checkpoint.payload["runtime"]["runtimeBuildId"]

    context: dict[str, object] = {}
    PortfolioDensityContinuationService.apply_to_request_context(context, checkpoint)
    assert context["actualExecutionRoute"] == "portfolio_density_resume"
    assert context["agentDecisionState"]["controllerCalled"] is False
    assert context["agentDecisionState"]["checkpointFingerprint"] == checkpoint.fingerprint
    assert context["planningSelectionRootTurnId"] == "turn_original_density_root"
    assert context["rootPortfolioId"] == "portfolio_root"
    assert context["portfolioGroundingFocusBriefId"] == "brief_local"
    assert context["requestContractFingerprint"] == "f" * 64


def test_scope_restores_complete_persisted_creative_brief_before_retrying_one_slot() -> None:
    scoped = PortfolioDensityContinuationService._scope_initial_plan(
        {"daySlots": [{"slotId": "other_slot", "dayNumber": 1}], "intentPools": []},
        creative_portfolio={
            "proposals": [
                {
                    "brief": {"briefId": "brief_local"},
                    "daySlots": [
                        {
                            "slotId": "slot_campus_day_1",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "startTime": None,
                            "kind": "campus",
                            "routeAnchor": True,
                        },
                        {
                            "slotId": "slot_night_day_1",
                            "dayNumber": 1,
                            "timeWindow": "night",
                            "startTime": None,
                            "kind": "night_view",
                            "routeAnchor": True,
                        },
                        {
                            "slotId": "slot_night_day_2",
                            "dayNumber": 2,
                            "timeWindow": "18:30-22:00",
                            "startTime": None,
                            "kind": "night_view",
                            "routeAnchor": True,
                        },
                    ],
                    "intentPools": [
                        {
                            "briefId": "brief_local",
                            "poolId": "pool_campus_day_1",
                            "intentType": "campus_visit",
                            "requirementLevel": "required",
                            "assignToSlots": ["slot_campus_day_1"],
                        },
                        {
                            "briefId": "brief_local",
                            "poolId": "pool_night_day_1",
                            "intentType": "night_view",
                            "requirementLevel": "required",
                            "assignToSlots": ["slot_night_day_1"],
                        },
                        {
                            "briefId": "brief_local",
                            "poolId": "pool_night_day_2",
                            "intentType": "night_view",
                            "requirementLevel": "required",
                            "assignToSlots": ["slot_night_day_2"],
                        },
                    ],
                },
                {
                    "brief": {"briefId": "brief_other"},
                    "daySlots": [{"slotId": "other_slot", "dayNumber": 1}],
                    "intentPools": [],
                },
            ]
        },
        scope={
            "briefId": "brief_local",
            "poolId": "pool_night_day_2",
            "planningSlotId": "slot_night_day_2",
            "dayNumber": 2,
        },
        primary_group={
            "briefId": "brief_local",
            "poolId": "pool_night_day_2",
            "slotId": "slot_night_day_2",
            "dayNumber": 2,
            "intentType": "night_view",
        },
        city="北京",
    )

    assert [slot["slotId"] for slot in scoped["daySlots"]] == [
        "slot_campus_day_1",
        "slot_night_day_1",
        "slot_night_day_2",
    ]
    assert [slot["startTime"] for slot in scoped["daySlots"]] == [
        "09:00",
        "18:00",
        "18:30",
    ]
    assert [pool["poolId"] for pool in scoped["intentPools"]] == [
        "pool_campus_day_1",
        "pool_night_day_1",
        "pool_night_day_2",
    ]
    assert scoped["reply"] == "从持久化 Portfolio 恢复目标方向的完整计划。"


def test_legacy_expand_action_is_normalized_from_persisted_kind() -> None:
    option = {
        "id": "density_nearby",
        "action": "manual_continuation",
        "kind": "portfolio_density_retry",
        "retryMode": "expand_nearby",
    }

    assert PortfolioDensityContinuationService.normalized_action(option) == DENSITY_NEARBY_ACTION


def test_fully_scoped_custom_input_is_density_manual_search() -> None:
    option = {
        "id": "density_manual",
        "action": "manual_continuation",
        "kind": "custom_input",
        "briefId": "brief_local",
        "poolId": "pool_day_2_walk",
        "planningSlotId": "slot_day_2_walk",
        "dayNumber": 2,
        "intentType": "area_walk",
        "planningSelectionRootTurnId": "turn_original_density_root",
        "rootPortfolioId": "portfolio_root",
        "focusBriefId": "brief_local",
        "requestContractFingerprint": "f" * 64,
        "expectedBaseVersionId": "version_partial",
    }

    assert PortfolioDensityContinuationService.is_density_option(option) is True
    assert (
        PortfolioDensityContinuationService.normalized_action(option)
        == DENSITY_MANUAL_SEARCH_ACTION
    )


@pytest.mark.parametrize(
    "option",
    [
        {"action": "manual_continuation", "kind": "custom_input"},
        {
            "action": "manual_continuation",
            "kind": "custom_input",
            "briefId": "brief_local",
            "poolId": "pool_day_2_walk",
            "planningSlotId": "slot_day_2_walk",
            "dayNumber": 2,
        },
        {
            "action": "manual_continuation",
            "kind": "material_tradeoff",
            "briefId": "brief_local",
            "poolId": "pool_day_2_walk",
            "planningSlotId": "slot_day_2_walk",
            "dayNumber": 2,
            "intentType": "area_walk",
        },
    ],
)
def test_generic_or_incompletely_scoped_manual_input_is_not_density(option) -> None:
    assert PortfolioDensityContinuationService.is_density_option(option) is False
    assert (
        PortfolioDensityContinuationService.normalized_action(option)
        == "manual_continuation"
    )


def test_historical_option_without_expiry_uses_source_turn_ttl() -> None:
    db = _connection()
    option = {
        "id": "density_refresh_expired",
        "action": "refresh_density_candidates",
        "kind": "portfolio_density_retry",
        "densityGroups": [
            {
                "briefId": "brief_local",
                "poolId": "pool_day_2_walk",
                "slotId": "slot_day_2",
                "dayNumber": 2,
                "intentType": "area_walk",
            }
        ],
    }
    _insert_pause(
        db,
        option=option,
        created_at=(datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat(),
    )

    with pytest.raises(PortfolioDensityContinuationError) as raised:
        PortfolioDensityContinuationService(db).load_checkpoint(
            session_id="sess_density",
            source_assistant_turn_id="turn_density_pause",
            option=option,
        )

    assert raised.value.code == "plan_proposal_expired"


def test_checkpoint_rejects_choice_when_active_version_changed() -> None:
    db = _connection()
    option = {
        "id": "density_candidate_stale_base",
        "action": "resume_density_candidate",
        "kind": "portfolio_density_candidate",
        "briefId": "brief_local",
        "poolId": "pool_walk",
        "planningSlotId": "slot_day_2",
        "dayNumber": 2,
        "intentType": "area_walk",
        "expectedBaseVersionId": "version_partial",
        "candidateRecordId": "candidate_record",
        "amapId": "B0STALE",
    }
    _insert_pause(db, option=option)
    db.execute(
        "UPDATE conversation_sessions SET active_version_id = 'version_new' WHERE id = 'sess_density'"
    )
    db.commit()

    with pytest.raises(PortfolioDensityContinuationError) as raised:
        PortfolioDensityContinuationService(db).load_checkpoint(
            session_id="sess_density",
            source_assistant_turn_id="turn_density_pause",
            option=option,
        )

    assert raised.value.code == "agent_choice_target_stale"


def test_progress_requires_the_target_slot_to_disappear_even_when_nested() -> None:
    response = SimpleNamespace(
        version=None,
        assistant_turn=SimpleNamespace(
            choice_options=[
                {
                    "id": "new_portfolio_retry",
                    "kind": "portfolio_density_retry",
                    "densityGroups": [{"slotId": "slot_day_2"}],
                }
            ]
        ),
    )

    assert PortfolioDensityContinuationService.made_progress(
        response,
        {"choiceId": "old_choice", "planningSlotId": "slot_day_2"},
    ) is False


def test_progress_does_not_accept_an_unrelated_followup_slot() -> None:
    response = SimpleNamespace(
        version=None,
        planning_steps=[],
        assistant_turn=SimpleNamespace(
            choice_options=[
                {
                    "id": "next_choice",
                    "kind": "portfolio_density_candidate",
                    "planningSlotId": "slot_other",
                }
            ]
        ),
    )

    assert PortfolioDensityContinuationService.made_progress(
        response,
        {"choiceId": "old_choice", "planningSlotId": "slot_target"},
    ) is False


def test_search_metadata_cannot_claim_committed_target_slot_progress() -> None:
    response = SimpleNamespace(
        version=None,
        planning_steps=[
            SimpleNamespace(
                metadata={
                    "resultPreview": {
                        "poolReports": [
                            {
                                "briefId": "brief_target",
                                "poolId": "pool_target",
                                "resolvedSlotIds": ["slot_target"],
                                "slotDayNumbers": {"slot_target": 2},
                            }
                        ]
                    }
                }
            )
        ],
        assistant_turn=SimpleNamespace(choice_options=[{"planningSlotId": "slot_other"}]),
    )

    assert PortfolioDensityContinuationService.made_progress(
        response,
        {
            "choiceId": "old_choice",
            "briefId": "brief_target",
            "poolId": "pool_target",
            "planningSlotId": "slot_target",
            "dayNumber": 2,
            "groundingCheckpoint": {
                "unresolvedSlots": [
                    {
                        "briefId": "brief_target",
                        "poolId": "pool_target",
                        "slotId": "slot_target",
                        "dayNumber": 2,
                    }
                ]
            },
        },
    ) is False


def test_progress_rejects_created_version_without_target_scope_coverage() -> None:
    response = SimpleNamespace(
        version=SimpleNamespace(id="version_placeholder"),
        planning_steps=[
            SimpleNamespace(
                metadata={
                    "resultPreview": {
                        "poolReports": [
                            {
                                "briefId": "brief_other",
                                "poolId": "pool_target",
                                "resolvedSlotIds": ["slot_target"],
                                "slotDayNumbers": {"slot_target": 2},
                            }
                        ]
                    }
                }
            )
        ],
        assistant_turn=SimpleNamespace(choice_options=[]),
    )

    assert PortfolioDensityContinuationService.made_progress(
        response,
        {
            "choiceId": "old_choice",
            "briefId": "brief_target",
            "poolId": "pool_target",
            "planningSlotId": "slot_target",
            "dayNumber": 2,
        },
    ) is False


def test_progress_rejects_covered_evidence_when_checkpoint_was_not_unresolved() -> None:
    response = SimpleNamespace(
        version=None,
        planning_steps=[
            SimpleNamespace(
                metadata={
                    "poolReports": [
                        {
                            "briefId": "brief_target",
                            "poolId": "pool_target",
                            "resolvedSlotIds": ["slot_target"],
                            "slotDayNumbers": {"slot_target": 2},
                        }
                    ]
                }
            )
        ],
        assistant_turn=SimpleNamespace(choice_options=[]),
    )
    assert PortfolioDensityContinuationService.made_progress(
        response,
        {
            "briefId": "brief_target",
            "poolId": "pool_target",
            "planningSlotId": "slot_target",
            "dayNumber": 2,
            "groundingCheckpoint": {"unresolvedSlots": []},
        },
    ) is False


def test_new_candidate_progress_compares_scoped_amap_identity_not_record_id() -> None:
    checkpoint = {
        "briefId": "brief_target",
        "poolId": "pool_walk",
        "planningSlotId": "slot_walk",
        "dayNumber": 1,
        "densityGroups": [
            {
                "briefId": "brief_target",
                "poolId": "pool_walk",
                "slotId": "slot_walk",
                "dayNumber": 1,
                "candidateRecordId": "old_record",
                "candidates": [{"id": "B0OLD"}],
            }
        ],
    }
    same_identity = SimpleNamespace(
        assistant_turn=SimpleNamespace(
            choice_options=[
                {
                    "kind": "portfolio_density_candidate",
                    "briefId": "brief_target",
                    "poolId": "pool_walk",
                    "planningSlotId": "slot_walk",
                    "dayNumber": 1,
                    "candidateRecordId": "new_record",
                    "amapId": "B0OLD",
                }
            ]
        )
    )
    new_identity = SimpleNamespace(
        assistant_turn=SimpleNamespace(
            choice_options=[
                {
                    "kind": "portfolio_density_candidate",
                    "briefId": "brief_target",
                    "poolId": "pool_walk",
                    "planningSlotId": "slot_walk",
                    "dayNumber": 1,
                    "candidateRecordId": "new_record",
                    "amapId": "B0NEW",
                }
            ]
        )
    )

    assert (
        PortfolioDensityContinuationService.has_new_candidate_options(
            same_identity,
            checkpoint,
        )
        is False
    )
    assert (
        PortfolioDensityContinuationService.has_new_candidate_options(
            new_identity,
            checkpoint,
        )
        is True
    )


def test_load_checkpoint_rejects_retry_after_later_turn_covered_target() -> None:
    db = _connection()
    option = {
        "id": "density_refresh_stale",
        "action": "refresh_density_candidates",
        "kind": "portfolio_density_retry",
        "briefId": "brief_local",
        "poolId": "pool_walk",
        "planningSlotId": "slot_day_2",
        "dayNumber": 2,
        "intentType": "area_walk",
        "planningSelectionRootTurnId": "turn_original_density_root",
        "rootPortfolioId": "portfolio_root",
        "focusBriefId": "brief_local",
        "requestContractFingerprint": "f" * 64,
        "expectedBaseVersionId": "version_partial",
    }
    _insert_pause(db, option=option)
    later_response = {
        "groundingCheckpoint": {
            "poolReports": [
                {
                    "briefId": "brief_local",
                    "poolId": "pool_walk",
                    "slotDayNumbers": {"slot_day_2": 2},
                    "resolvedSlotIds": ["slot_day_2"],
                }
            ]
        }
    }
    db.execute(
        """
        INSERT INTO conversation_turns (
            id, session_id, role, turn_index, status, created_at, agent_response_json
        ) VALUES (
            'turn_later', 'sess_density', 'assistant', 2, 'active', ?, ?
        )
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            json.dumps(later_response, ensure_ascii=False),
        ),
    )
    db.commit()

    with pytest.raises(PortfolioDensityContinuationError) as raised:
        PortfolioDensityContinuationService(db).load_checkpoint(
            session_id="sess_density",
            source_assistant_turn_id="turn_density_pause",
            option=option,
        )

    assert raised.value.code == "agent_choice_target_stale"


def test_scope_requires_complete_persisted_brief_pool_slot_day_identity() -> None:
    with pytest.raises(PortfolioDensityContinuationError) as raised:
        PortfolioDensityContinuationService._validate_scope(
            {"id": "choice", "kind": "portfolio_density_retry"},
            {"id": "choice", "kind": "portfolio_density_retry", "densityGroups": []},
        )

    assert raised.value.code == "agent_choice_source_context_missing"


def test_scope_rejects_cross_brief_identity_even_when_pool_and_slot_match() -> None:
    persisted = {
        "id": "choice",
        "kind": "portfolio_density_retry",
        "briefId": "brief_a",
        "poolId": "pool_walk",
        "planningSlotId": "slot_walk",
        "dayNumber": 2,
        "intentType": "area_walk",
    }
    requested = {**persisted, "briefId": "brief_b"}

    with pytest.raises(PortfolioDensityContinuationError) as raised:
        PortfolioDensityContinuationService._validate_scope(requested, persisted)

    assert raised.value.code == "agent_choice_identity_mismatch"

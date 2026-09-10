from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.main import app
from src.services.agent_run_control import acquire_session_run, release_session_run
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.simple_open_direction_service import SimpleOpenDirectionService


def _open_db() -> sqlite3.Connection:
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _route_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={
            "transportMode": "transit",
            "requestSemanticsPresent": True,
        },
        detour_tolerance={
            "maxGeneralizedCostDelta": 30.0,
            "maxDetourRatio": 0.25,
        },
        mobility_profile={
            "source": "explicit_user_preference",
            "walkingPenaltyMinutesPerKm": 3.0,
            "transferPenaltyMinutes": 8.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert contract is not None
    return {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        **contract,
    }


def _snapshot(plan_id: str, *, title: str, start_time: str) -> dict:
    start_hour, start_minute = (int(value) for value in start_time.split(":"))
    duration_minutes = 11 * 60 - (start_hour * 60 + start_minute)
    return {
        "id": plan_id,
        "title": title,
        "city": "北京",
        "templateType": "agent_mvp",
        "budgetEstimate": 0,
        "decisionRationale": "服务端持久化的单方向行程。",
        "status": "draft",
        "simpleOpenExecutionProfile": "simple_open_v1",
        "requiredPlanningDayNumbers": [1],
        "explicitRestDayNumbers": [],
        "dailyPlanningCoverageSource": "authoritative_goal_occurrences",
        "routeDecisionContract": _route_contract(),
        "desiredDensityAnchorTargets": {"1": 1},
        "days": [
            {
                "id": "day_direction_api",
                "dayNumber": 1,
                "date": "2026-10-01",
                "title": title,
                "totalEstimatedCost": 0,
                "segments": [
                    {
                        "id": "seg_direction_api",
                        "startTime": start_time,
                        "endTime": "11:00",
                        "durationMinutes": duration_minutes,
                        "kind": "campus",
                        "poi": {
                            "id": "poi_direction_api",
                            "amapId": "B000DIRECTIONAPI",
                            "name": "北京高校",
                            "city": "北京",
                            "category": "campus",
                            "type": "科教文化服务;学校;高等院校",
                            "providerType": "科教文化服务;学校;高等院校",
                            "providerTypeCode": "141201",
                            "latitude": 39.99,
                            "longitude": 116.31,
                            "source": "amap-place-search",
                            "confidence": 0.9,
                        },
                        "transportMode": "public_transit",
                        "estimatedCost": 0,
                        "semanticMetadata": {
                            "intentType": "campus_visit",
                            "routeAnchor": True,
                            "groundingStatus": "verified_amap",
                            "required": True,
                            "requirementLevel": "required",
                            "goalId": "goal_direction_api",
                            "sourceGoalId": "goal_direction_api",
                            "occurrenceId": "occ:goal_direction_api:day:1",
                            "planningSlotId": "slot_direction_api",
                            "poolId": "pool_direction_api",
                            "dayNumber": 1,
                            "lineageAuthority": "goal_occurrence_compiler",
                        },
                        "notes": "",
                    }
                ],
            }
        ],
        "routeOptions": [],
        "weatherSignals": [],
        "trafficCrowdingSignals": [],
        "ticketLookupResults": [],
    }


def _seed_confirmed_direction(*, session_id: str, plan_id: str) -> tuple[str, str]:
    with _open_db() as connection:
        service = SimpleOpenDirectionService(connection)
        offered = service.offer_direction(
            session_id=session_id,
            planning_root_id="turn_direction_root",
            source_user_turn_id="turn_direction_root",
            source_assistant_turn_id="turn_direction_offer",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(plan_id, title="高校经典线", start_time="09:00"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        proposal_id = str(offered["comparisonProjections"][0]["proposalId"])
        portfolio_id = str(offered["rootPortfolioId"])
        active_snapshot = _snapshot(plan_id, title="高校经典线（已编辑）", start_time="08:30")
        active_verifier = SimpleOpenDirectionService.activation_verifier(active_snapshot)
        assert active_verifier["passed"] is True, active_verifier["hardFailures"]
        assert active_verifier["requiredDayTargetMissing"] == []
        assert active_verifier["uncoveredDayNumbers"] == []
        connection.execute(
            "INSERT INTO itinerary_versions "
            "(id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES ('ver_direction_active', ?, ?, 1, 'agent', ?, '2026-08-18T00:00:00+00:00')",
            (session_id, plan_id, json.dumps(active_snapshot, ensure_ascii=False)),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = 'ver_direction_active' WHERE id = ?",
            (session_id,),
        )
        connection.execute(
            "UPDATE agent_plan_portfolios "
            "SET selected_proposal_id = ?, expected_base_version_id = 'ver_direction_active' WHERE id = ?",
            (proposal_id, portfolio_id),
        )
        connection.commit()
    return proposal_id, portfolio_id


def test_save_active_direction_api_forbids_client_snapshot_and_is_idempotent() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "单方向保存 API"},
        )
        assert created.status_code == 200
        session = created.json()
        session_id = str(session["sessionId"])
        proposal_id, portfolio_id = _seed_confirmed_direction(
            session_id=session_id,
            plan_id=str(session["activePlanId"]),
        )
        endpoint = f"/api/agent/sessions/{session_id}/directions/{proposal_id}/save-active"
        payload = {
            "baseVersionId": "ver_direction_active",
            "planningSelectionRootTurnId": "turn_direction_root",
            "rootPortfolioId": portfolio_id,
        }

        rejected = client.post(endpoint, json={**payload, "snapshot": {"title": "客户端伪造快照"}})
        assert rejected.status_code == 422
        assert any(error["loc"][-1] == "snapshot" for error in rejected.json()["validationErrors"])

        identity_conflict = client.post(
            endpoint,
            json={**payload, "planningSelectionRootTurnId": "turn_foreign_root"},
        )
        assert identity_conflict.status_code == 409
        assert identity_conflict.json()["detail"] == {
            "code": "direction_save_identity_mismatch",
            "message": "当前编辑方案与行程对比中的方案身份不一致，请保留当前页面并刷新后重试。",
        }

        stale = client.post(endpoint, json={**payload, "baseVersionId": "ver_stale"})
        assert stale.status_code == 409
        assert stale.json()["detail"] == {
            "code": "direction_save_version_stale",
            "message": "行程版本已变化，当前编辑尚未保存到对比方案；请刷新版本后重试。",
        }

        saved = client.post(endpoint, json=payload)
        repeated = client.post(endpoint, json=payload)
        assert saved.status_code == 200, saved.json()
        assert repeated.status_code == 200
        assert saved.json()["proposalId"] == proposal_id
        assert saved.json()["activeVersionId"] == "ver_direction_active"
        assert saved.json()["saved"] is True
        assert saved.json()["unchanged"] is False
        assert saved.json()["comparisonProjection"]["proposalId"] == proposal_id
        assert repeated.json()["saved"] is True
        assert repeated.json()["unchanged"] is True

        with _open_db() as connection:
            stored = connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            assert stored is not None
            assert json.loads(stored["snapshot_json"])["days"][0]["segments"][0]["startTime"] == "08:30"
            assert connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0] == 0


def test_save_active_direction_api_returns_existing_session_not_found_contract() -> None:
    response = TestClient(app).post(
        "/api/agent/sessions/sess_missing/directions/proposal_missing/save-active",
        json={
            "baseVersionId": "ver_missing",
            "planningSelectionRootTurnId": "turn_missing",
            "rootPortfolioId": "portfolio_missing",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation session not found"


def test_save_active_direction_api_exposes_server_activation_failure_without_mutation() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "单方向保存安全阻断"},
        )
        assert created.status_code == 200
        session = created.json()
        session_id = str(session["sessionId"])
        proposal_id, portfolio_id = _seed_confirmed_direction(
            session_id=session_id,
            plan_id=str(session["activePlanId"]),
        )
        with _open_db() as connection:
            version_row = connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = 'ver_direction_active'"
            ).fetchone()
            snapshot = json.loads(version_row["snapshot_json"])
            snapshot["days"][0]["segments"][0]["poi"]["city"] = "上海"
            connection.execute(
                "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = 'ver_direction_active'",
                (json.dumps(snapshot, ensure_ascii=False),),
            )
            proposal_before = connection.execute(
                "SELECT snapshot_json, verifier_json, evidence_json FROM agent_plan_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            proposal_before = tuple(proposal_before)
            connection.commit()

        response = client.post(
            f"/api/agent/sessions/{session_id}/directions/{proposal_id}/save-active",
            json={
                "baseVersionId": "ver_direction_active",
                "planningSelectionRootTurnId": "turn_direction_root",
                "rootPortfolioId": portfolio_id,
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "direction_save_activation_failed",
            "message": "当前行程中的地点城市与本次规划城市不一致，未保存到对比方案；请留在编辑页修正后重试。",
        }
        with _open_db() as connection:
            assert tuple(
                connection.execute(
                    "SELECT snapshot_json, verifier_json, evidence_json FROM agent_plan_proposals WHERE id = ?",
                    (proposal_id,),
                ).fetchone()
            ) == proposal_before
            assert connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ? AND status = 'internal_capability'",
                (session_id,),
            ).fetchone()[0] == 0


def test_save_active_direction_api_rejects_concurrent_session_run_without_mutation() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "单方向保存并发 CAS"},
        )
        assert created.status_code == 200
        session = created.json()
        session_id = str(session["sessionId"])
        proposal_id, portfolio_id = _seed_confirmed_direction(
            session_id=session_id,
            plan_id=str(session["activePlanId"]),
        )
        with _open_db() as connection:
            before = connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()["snapshot_json"]

        assert acquire_session_run(session_id) is True
        try:
            response = client.post(
                f"/api/agent/sessions/{session_id}/directions/{proposal_id}/save-active",
                json={
                    "baseVersionId": "ver_direction_active",
                    "planningSelectionRootTurnId": "turn_direction_root",
                    "rootPortfolioId": portfolio_id,
                },
            )
        finally:
            release_session_run(session_id)

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "direction_save_in_progress",
            "message": "当前方案正在确认或保存，请停留在编辑页，稍后再试。",
        }
        with _open_db() as connection:
            assert connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()["snapshot_json"] == before
            assert connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ? AND status = 'internal_capability'",
                (session_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0] == 0

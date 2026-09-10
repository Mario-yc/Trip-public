import copy
import hashlib
import sqlite3
import json
import os
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
import src.services.conversation_service as conversation_service_module

from backend.tests.intent_contract_support import IntentContractProviderMixin
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app
from src.api.routes.agent import _reasoning_terminal_from_response
from src.api.schemas.agent import AgentMessageResponse
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.models.route_option import RouteOption
from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.route_service import RouteService
from src.runtime.agent_runtime import TripAgentRuntime
from src.services.agent_service import AgentService
from src.services.agent_run_control import acquire_session_run, release_session_run
from src.services.creative_portfolio_provider_service import (
    CreativePortfolioProviderService,
    InitialCreativePortfolio,
)
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
from src.services.creative_planning_models import PlanCandidate
from src.services.deepseek_agent_provider import AgentToolLoopError, AgentToolLoopResult, DeepSeekAgentProvider
from src.services.map_poi_service import MapPoiService
from src.services.pareto_portfolio_selector import ParetoPortfolioSelector
from src.services.plan_proposal_commit_service import PlanProposalCommitService
from src.services.poi_discovery_service import PoiDiscoveryService


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM agent_choice_executions;
            DELETE FROM timeline_mutation_transactions;
            DELETE FROM agent_plan_proposals;
            DELETE FROM agent_plan_portfolios;
            DELETE FROM amap_poi_candidates;
            DELETE FROM session_preference_memories;
            DELETE FROM travel_preference_memories;
            DELETE FROM planning_runs;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM plan_comparisons;
            DELETE FROM route_options;
            DELETE FROM weather_signals;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM poi_risk_alerts;
            DELETE FROM ticket_lookup_results;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            """
        )


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def recorded_provider_routes(
    route_service,
    plan_id,
    pois,
    transport_mode="transit",
    segments=None,
    route_pairs=None,
    **_kwargs,
):
    """Return fresh, complete Provider route evidence for contract tests."""

    mode = "transit" if transport_mode in {"", "unspecified"} else str(transport_mode)
    endpoint = {
        "transit": "/v3/direction/transit/integrated",
        "walking": "/v3/direction/walking",
        "bicycling": "/v4/direction/bicycling",
        "driving": "/v3/direction/driving",
    }.get(mode, f"/v3/direction/{mode}")
    requested_pairs = set(route_pairs) if route_pairs is not None else None
    groups = route_service._route_groups(list(pois), list(segments or []))
    if requested_pairs is not None:
        groups = [
            group
            for group in groups
            if group[0] is not None and group[1] is not None and (group[0].id, group[1].id) in requested_pairs
        ]
    queried_at = datetime.now(timezone.utc)
    routes = []
    for from_segment, to_segment, from_poi, to_poi in groups:
        assert from_segment is not None and to_segment is not None
        route_id = (
            "route_recorded_"
            + hashlib.sha256(f"{plan_id}:{from_segment.id}:{to_segment.id}:{mode}".encode("utf-8")).hexdigest()[:20]
        )
        routes.append(
            RouteOption(
                id=route_id,
                plan_id=plan_id,
                from_segment_id=from_segment.id,
                to_segment_id=to_segment.id,
                from_poi_id=from_poi.id,
                to_poi_id=to_poi.id,
                provider="amap-webservice",
                mode=mode,
                is_selected=True,
                distance_meters=1800,
                duration_seconds=900,
                polyline=[
                    [float(from_poi.longitude), float(from_poi.latitude)],
                    [float(to_poi.longitude), float(to_poi.latitude)],
                ],
                steps=[
                    {
                        "instruction": "Walk to the recorded transit stop",
                        "mode": "walking",
                        "distance": 240,
                    },
                    {
                        "instruction": "Take the recorded transit service",
                        "mode": "transit",
                        "distance": 1560,
                    },
                ],
                provider_payload={
                    "fixture": "agent-session-contract-route-matrix-v1",
                    "status": "1",
                    "endpoint": endpoint,
                    "queriedAt": queried_at.isoformat(),
                    "walkingDistanceMeters": 240,
                    "transferCount": 1,
                    "waitSeconds": 120,
                    "riskPenaltyMinutes": 0.0,
                    "costComponentProvenance": {
                        "walkingDistance": "recorded_provider_payload",
                        "transferCount": "recorded_provider_payload",
                        "wait": "recorded_provider_payload",
                        "risk": "recorded_route_fixture",
                    },
                    "provenance": {
                        "provider": "amap-webservice",
                        "endpoint": endpoint,
                        "recording": "deterministic_test_fixture",
                    },
                },
                queried_at=queried_at,
            )
        )
    return routes


def route_ready_request(content: str) -> str:
    """Add explicit user semantics from which the server owns the route contract."""

    return f"{content}，公交地铁优先，绕行最多30分钟，绕行比例最多35%"


def install_recorded_provider_route_matrix(monkeypatch) -> None:
    """Install the deterministic, complete AMap Provider route matrix."""

    monkeypatch.setattr(RouteService, "build_routes", recorded_provider_routes)


def test_create_and_get_agent_session():
    clear_database()
    with TestClient(app) as client:
        created = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京 2 日自由行"})
        assert created.status_code == 200
        body = created.json()

        loaded = client.get(f"/api/agent/sessions/{body['sessionId']}")

    assert body["city"] == "北京"
    assert body["title"] == "北京 2 日自由行"
    assert body["activePlanId"].startswith("plan_")
    assert body["activeVersionId"] is None
    assert body["turns"] == []
    assert body["itinerary"]["templateType"] == "agent_mvp"
    assert body["itinerary"]["days"][0]["dayNumber"] == 1
    assert body["itinerary"]["days"][0]["segments"] == []
    assert loaded.status_code == 200
    assert loaded.json()["sessionId"] == body["sessionId"]
    assert loaded.json()["itinerary"]["days"][0]["title"] == "Day 1 待规划"


def test_get_agent_session_restores_persisted_structured_choice_trace():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "trace refresh"}).json()
        selected_choice = {
            "sourceAssistantTurnId": "turn_source_choice",
            "choiceId": "portfolio_more_plans_1",
            "requestChoiceId": "portfolio_more_plans_1",
            "persistedChoiceId": "portfolio_more_plans_1",
            "persistedChoiceAction": "retry_model_planning",
        }
        continuation = {
            "kind": "expand_partial_portfolio",
            "planningSelectionRootTurnId": "turn_root_choice",
            "rootPortfolioId": "portfolio_trace",
            "requestContractFingerprint": "fingerprint_trace",
            "activeVersionId": None,
        }
        outcome = {
            "succeeded": True,
            "proposalDelta": 1,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "planningSelectionRootTurnId": "turn_root_choice",
            "rootPortfolioId": "portfolio_trace",
            "requestContractFingerprint": "fingerprint_trace",
        }
        with open_db() as connection:
            connection.execute(
                "INSERT INTO conversation_turns "
                "(id, session_id, role, content, turn_index, status, agent_request_json, created_at, updated_at) "
                "VALUES (?, ?, 'assistant', 'choose', 0, 'active', '{}', ?, ?)",
                ("turn_source_choice", session["sessionId"], "2026-07-26", "2026-07-26"),
            )
            connection.execute(
                "INSERT INTO conversation_turns "
                "(id, session_id, role, content, turn_index, status, agent_request_json, created_at, updated_at) "
                "VALUES (?, ?, 'user', '继续生成其他方案', 1, 'active', ?, ?, ?)",
                (
                    "turn_choice_request",
                    session["sessionId"],
                    json.dumps({"selectedAgentChoice": selected_choice}),
                    "2026-07-26",
                    "2026-07-26",
                ),
            )
            connection.execute(
                "INSERT INTO agent_choice_executions "
                "(id, session_id, source_turn_id, source_user_turn_id, choice_id, action, status, "
                "continuation_json, checkpoint_fingerprint, outcome_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'retry_model_planning', 'succeeded', ?, ?, ?, ?, ?)",
                (
                    "choice_exec_trace",
                    session["sessionId"],
                    "turn_source_choice",
                    "turn_root_choice",
                    "portfolio_more_plans_1",
                    json.dumps(continuation),
                    "checkpoint_trace",
                    json.dumps(outcome),
                    "2026-07-26",
                    "2026-07-26",
                ),
            )
            connection.commit()

        refreshed = client.get(f"/api/agent/sessions/{session['sessionId']}")

    assert refreshed.status_code == 200
    trace = next(turn for turn in refreshed.json()["turns"] if turn["id"] == "turn_choice_request")[
        "structuredChoiceTrace"
    ]
    assert trace["executionId"] == "choice_exec_trace"
    assert trace["executionStatus"] == "succeeded"
    assert trace["rootPortfolioId"] == "portfolio_trace"
    assert trace["requestContractFingerprint"] == "fingerprint_trace"
    assert trace["outcome"]["proposalDelta"] == 1
    assert (trace["versionDelta"], trace["patchDelta"], trace["routeWriteDelta"]) == (0, 0, 0)


def test_list_and_delete_agent_sessions_hard_deletes_session_owned_records(tmp_path, monkeypatch):
    clear_database()
    monkeypatch.setattr(conversation_service_module, "PROJECT_ROOT", tmp_path)
    upload_dir = tmp_path / "backend" / "data" / "uploads"
    upload_dir.mkdir(parents=True)
    original = upload_dir / "original.jpg"
    thumbnail = upload_dir / "thumbnail.txt"
    original.write_bytes(b"original")
    thumbnail.write_text("thumbnail", encoding="utf-8")
    with TestClient(app) as client:
        first = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        second = client.post("/api/agent/sessions", json={"city": "上海", "title": "上海会话"}).json()
        listed = client.get("/api/agent/sessions")
        with open_db() as connection:
            session_id = second["sessionId"]
            plan_id = second["activePlanId"]
            connection.execute(
                "INSERT INTO conversation_turns (id, session_id, role, content, turn_index, status, created_at, updated_at) "
                "VALUES ('turn_delete', ?, 'user', 'delete me', 1, 'active', '2026-07-22', '2026-07-22')",
                (session_id,),
            )
            connection.execute(
                "INSERT INTO itinerary_versions (id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
                "VALUES ('version_delete', ?, ?, 1, 'agent', '{}', '2026-07-22')",
                (session_id, plan_id),
            )
            connection.execute(
                "INSERT INTO itinerary_patches (id, session_id, plan_id, result_version_id, source_type, operations_json, "
                "validation_status, validation_errors_json, created_at) "
                "VALUES ('patch_delete', ?, ?, 'version_delete', 'agent', '[]', 'passed', '[]', '2026-07-22')",
                (session_id, plan_id),
            )
            connection.execute(
                "INSERT INTO amap_poi_candidates (id, session_id, query, city, category, status, candidates_json, created_at) "
                "VALUES ('candidate_delete', ?, 'test', '上海', 'test', 'pending', '[]', '2026-07-22')",
                (session_id,),
            )
            connection.execute(
                "INSERT INTO source_materials (id, inspiration_set_id, kind, thumbnail_path, original_path, "
                "original_retention, cache_status, created_at) VALUES "
                "('material_delete', ?, 'screenshot', ?, ?, 'temporary_cache', 'active', '2026-07-22')",
                (
                    session_id,
                    str(thumbnail.relative_to(tmp_path)),
                    str(original.relative_to(tmp_path)),
                ),
            )
            connection.commit()
        deleted = client.delete(f"/api/agent/sessions/{second['sessionId']}")
        current = client.get("/api/agent/sessions/current")

    assert listed.status_code == 200
    assert [item["sessionId"] for item in listed.json()["sessions"]] == [second["sessionId"], first["sessionId"]]
    assert listed.json()["sessions"][0]["turnCount"] == 0
    assert deleted.status_code == 200
    assert [item["sessionId"] for item in deleted.json()["sessions"]] == [first["sessionId"]]
    assert current.status_code == 200
    assert current.json()["sessionId"] == first["sessionId"]
    assert not original.exists()
    assert not thumbnail.exists()
    with open_db() as connection:
        for table, column in (
            ("conversation_sessions", "id"),
            ("conversation_turns", "session_id"),
            ("itinerary_versions", "session_id"),
            ("itinerary_patches", "session_id"),
            ("amap_poi_candidates", "session_id"),
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (second["sessionId"],)
                ).fetchone()[0]
                == 0
            )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_plans WHERE id = ?", (second["activePlanId"],)
            ).fetchone()[0]
            == 0
        )


def test_delete_agent_session_rejects_foreign_owner_and_active_run():
    clear_database()
    with TestClient(app) as client:
        foreign = client.post("/api/agent/sessions", json={"city": "北京", "title": "foreign"}).json()
        with open_db() as connection:
            connection.execute(
                "UPDATE conversation_sessions SET user_id = 'other-user' WHERE id = ?",
                (foreign["sessionId"],),
            )
            connection.commit()
        rejected_owner = client.delete(f"/api/agent/sessions/{foreign['sessionId']}")
        active = client.post("/api/agent/sessions", json={"city": "上海", "title": "running"}).json()
        assert acquire_session_run(active["sessionId"]) is True
        try:
            rejected_running = client.delete(f"/api/agent/sessions/{active['sessionId']}")
        finally:
            release_session_run(active["sessionId"])

    assert rejected_owner.status_code == 404
    assert rejected_running.status_code == 409
    assert rejected_running.json()["code"] == "agent_run_in_progress"
    with open_db() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversation_sessions WHERE id IN (?, ?)",
                (foreign["sessionId"], active["sessionId"]),
            ).fetchone()[0]
            == 2
        )


def test_reject_pending_poi_candidate_is_persisted_and_scoped_to_session():
    clear_database()
    with TestClient(app) as client:
        created = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        other = client.post("/api/agent/sessions", json={"city": "上海", "title": "上海会话"}).json()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO amap_poi_candidates (
                    id, session_id, turn_id, query, city, category, status,
                    candidates_json, selected_amap_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cand_reject",
                    created["sessionId"],
                    None,
                    "胡同餐厅",
                    "北京",
                    "food",
                    "pending",
                    json.dumps([amap_poi("B000FOOD", "胡同餐厅", "东城区", "116.398,39.919")], ensure_ascii=False),
                    None,
                    "2026-06-11T00:00:00Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO amap_poi_candidates (
                    id, session_id, turn_id, query, city, category, status,
                    candidates_json, selected_amap_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cand_other",
                    other["sessionId"],
                    None,
                    "外滩餐厅",
                    "上海",
                    "food",
                    "pending",
                    json.dumps([amap_poi("B000SH", "外滩餐厅", "黄浦区", "121.49,31.24")], ensure_ascii=False),
                    None,
                    "2026-06-11T00:00:01Z",
                ),
            )
            connection.commit()

        wrong_session = client.post(
            f"/api/agent/sessions/{created['sessionId']}/pending-poi-candidates/cand_other/reject"
        )
        rejected = client.post(f"/api/agent/sessions/{created['sessionId']}/pending-poi-candidates/cand_reject/reject")
        repeated = client.post(f"/api/agent/sessions/{created['sessionId']}/pending-poi-candidates/cand_reject/reject")
        loaded = client.get(f"/api/agent/sessions/{created['sessionId']}")

    assert wrong_session.status_code == 404
    assert rejected.status_code == 200
    assert rejected.json()["pendingPoiCandidates"] == []
    assert repeated.status_code == 409
    assert loaded.json()["pendingPoiCandidates"] == []
    with open_db() as connection:
        candidate = connection.execute(
            "SELECT status FROM amap_poi_candidates WHERE id = ?",
            ("cand_reject",),
        ).fetchone()
        other_candidate = connection.execute(
            "SELECT status FROM amap_poi_candidates WHERE id = ?",
            ("cand_other",),
        ).fetchone()
    assert candidate["status"] == "rejected"
    assert other_candidate["status"] == "pending"


def test_agent_message_api_generates_itinerary_with_mocked_provider_and_amap(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={
                "content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园"),
                "context": {"selectedDayNumber": 1},
            },
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}")
    with open_db() as connection:
        active_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (response.json()["version"]["id"],),
            ).fetchone()["snapshot_json"]
        )

    assert response.status_code == 200
    body = response.json()
    assistant_content = body["assistantTurn"]["content"]
    assert assistant_content.startswith("已完成：")
    assert "路线 1/1" in assistant_content
    assert "当前是 route-partial" in assistant_content
    assert "版本已保存，右侧时间轴可继续编辑。" in assistant_content
    assert assistant_content.endswith(f"已核对持久化行程快照：共 1 天，当前版本为 {body['version']['id']}。")
    assert body["version"]["sourceType"] in {"agent", "agent_enrichment"}
    assert body["itinerary"]["title"] == "北京可执行1日规划草案"
    assert body["itinerary"]["days"][0]["segments"][0]["poi"]["amapId"] == "B000PALACE"
    assert body["itinerary"]["days"][0]["segments"][0]["poi"]["longitude"] == 116.397026
    assert body["itinerary"]["days"][0]["segments"][0]["poi"]["latitude"] == 39.918058
    assert body["itinerary"]["days"][0]["segments"][0]["poi"]["source"] == "amap-place-search"
    assert len(body["itinerary"]["routeOptions"]) == 1
    assert body["itinerary"]["routeOptions"][0]["provider"] == "amap-webservice"
    route_contract = active_snapshot["routeDecisionContract"]
    assert route_contract["source"] == "request_intent_contract"
    assert len(route_contract["fingerprint"]) == 64
    assert route_contract["detourTolerance"] == {
        "maxGeneralizedCostDelta": 30.0,
        "maxDetourRatio": 0.35,
    }
    assert route_contract["provenance"]["transportMode"] == "transit"
    assert loaded.json()["activeVersionId"] == body["version"]["id"]
    assert len(loaded.json()["turns"]) == 2
    assert body["planningRun"]["runType"] == "agent_staged_pipeline"
    assert body["assistantTurn"]["planningRunId"] == body["planningRun"]["id"]
    assert loaded.json()["turns"][-1]["planningRunId"] == body["planningRun"]["id"]
    assert body["planningRun"]["feasibilityReport"]["score"] <= 100
    assert body["planningRun"]["understoodRequirements"]["isCompleteEnoughToPlan"] is True
    assert body["planningRun"]["understoodRequirements"]["clarificationQuestions"] == []
    assert "待核验" in body["planningRun"]["finalSummary"]
    assert body["warnings"]
    assert body["planningSteps"]
    assert body["toolEvents"] == []
    step_types = {step["type"] for step in body["planningSteps"]}
    assert {"agent_observation", "agent_decision", "agent_policy_gate", "agent_action_outcome"}.issubset(step_types)
    assert {"collect_candidates", "create_itinerary_version", "basic_verifier"}.issubset(step_types)


def test_agent_message_api_keeps_controller_retry_recoverable_without_rule_safe_draft(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "true")
    get_settings.cache_clear()
    provider = DeepSeekAgentProvider(api_key="test-key", model="contract-controller", timeout_seconds=7)
    inherited_lite = provider.decide_autonomy_lite

    def contract_lite(context, *, timeout_seconds):
        if context.get("schemaVersion") == "conversation-intent-context-v1":
            return {
                "intent": "create_itinerary",
                "confidence": 0.99,
                "requestedScope": "new_itinerary",
                "isQuestion": False,
                "isNegated": False,
            }
        return inherited_lite(context, timeout_seconds=timeout_seconds)

    provider.decide_autonomy_lite = contract_lite  # type: ignore[method-assign]
    controller_calls = 0

    def unavailable_controller(_context, **_kwargs):
        nonlocal controller_calls
        controller_calls += 1
        raise HTTPException(status_code=400, detail="controller unavailable")

    provider.decide_autonomy = unavailable_controller  # type: ignore[method-assign]
    provider.generate_initial_plan = lambda _context: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("rule-safe confirmation must not call the model planner")
    )
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "exact choice"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={
                "content": "今年国庆参观北京故宫一日游，10月1日，1天，中等预算，1人，公交地铁优先",
                "context": {},
            },
        ).json()
        retry_option = next(
            option
            for option in first["assistantTurn"]["choiceOptions"]
            if option.get("action") == "retry_model_planning"
        )
        assert not any(
            option.get("action") == "confirm_rule_safe_draft" for option in first["assistantTurn"]["choiceOptions"]
        )
        second_response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={
                "content": "选择 Agent 选项",
                "context": {
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": first["assistantTurn"]["id"],
                        "choiceId": retry_option["id"],
                    }
                },
            },
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert second_response.status_code == 200
    second = second_response.json()
    with open_db() as connection:
        execution = connection.execute(
            "SELECT choice_id, action, status, result_version_id FROM agent_choice_executions WHERE session_id = ?",
            (session["sessionId"],),
        ).fetchone()
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session["sessionId"],)
        ).fetchone()[0]
        user_request = json.loads(
            connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?", (second["userTurn"]["id"],)
            ).fetchone()["agent_request_json"]
        )
    selected = user_request["selectedAgentChoice"]
    assert selected["requestChoiceId"] == retry_option["id"]
    assert selected["persistedChoiceId"] == retry_option["id"]
    assert selected["persistedChoiceAction"] == "retry_model_planning"
    assert selected["executionChoiceId"] == retry_option["id"]
    assert selected["executionAction"] == "retry_model_planning"
    assert execution["choice_id"] == retry_option["id"]
    assert execution["action"] == "retry_model_planning"
    assert execution["status"] == "failed_retryable"
    assert execution["result_version_id"] is None
    assert second["version"] is None
    assert second["terminalStatus"] == "needs_confirmation"
    assert loaded["activeVersionId"] is None
    assert version_count == 0
    assert controller_calls == 2
    assert not any(
        option.get("action") == "confirm_rule_safe_draft" for option in second["assistantTurn"]["choiceOptions"]
    )


def test_controller_timeout_only_offers_retry_and_preserves_zero_write_from_empty_session(
    monkeypatch,
):
    RouteService.clear_cache()
    monkeypatch.setattr(
        "src.services.itinerary_diversity_policy.ItineraryDiversityPolicy.variant_seed",
        lambda _self, *_parts: 0,
    )
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "true")
    get_settings.cache_clear()
    provider = DeepSeekAgentProvider(api_key="test-key", model="contract-controller", timeout_seconds=7)

    def contract_intent_lite(context, *, timeout_seconds):
        if context.get("schemaVersion") == "conversation-intent-context-v1":
            return {
                "intent": "create_itinerary",
                "confidence": 0.99,
                "requestedScope": "new_itinerary",
                "isQuestion": False,
                "isNegated": False,
            }
        return {
            "schemaVersion": "agent-decision-lite-v1",
            "primaryAction": "ask_user",
            "confidence": 1.0,
            "reasonCode": "controller_timeout_retry_required",
            "userVisibleReason": "完整 Controller 本轮超时，请重试模型规划。",
        }

    provider.decide_autonomy_lite = contract_intent_lite  # type: ignore[method-assign]
    real_worker_slots = AgentAutonomyController._decision_worker_slots

    class ContractWorkerSlots:
        """Let intent and Full run, then make only the initial autonomy Lite unstarted."""

        def __init__(self):
            self.denied_initial_lite = False

        def acquire(self, *, blocking):
            if controller_calls == 1 and not controller_recovered and not self.denied_initial_lite:
                self.denied_initial_lite = True
                return False
            return real_worker_slots.acquire(blocking=blocking)

        def release(self):
            real_worker_slots.release()

    contract_worker_slots = ContractWorkerSlots()
    monkeypatch.setattr(
        AgentAutonomyController,
        "_decision_worker_slots",
        contract_worker_slots,
    )
    controller_calls = 0
    initial_plan_calls = 0
    portfolio_fallback_calls = 0
    portfolio_provider_calls = 0
    controller_recovered = False
    allow_meal_candidates = False
    search_calls = []
    route_calls = []
    stage_brief_batches = []
    stage_brief_details = []
    stage_metric_snapshots = []

    original_stage = CreativePortfolioStagingService.stage

    def tracked_stage(stage_service, *args, **kwargs):
        generated = kwargs.get("generated")
        stage_brief_batches.append(
            [str(item.brief.brief_id or "") for item in (generated.proposals if generated is not None else [])]
        )
        stage_brief_details.append(
            [
                {
                    "briefId": str(item.brief.brief_id or ""),
                    "primaryAxis": str(item.brief.primary_axis or ""),
                    "themeFamilies": list(item.brief.theme_families),
                    "optionalFamilies": [str(experience.family) for experience in item.brief.optional_experiences],
                    "candidateSupply": dict(item.brief.candidate_supply),
                }
                for item in (generated.proposals if generated is not None else [])
            ]
        )
        result = original_stage(stage_service, *args, **kwargs)
        stage_metric_snapshots.append(copy.deepcopy(stage_service.last_staging_metrics))
        return result

    monkeypatch.setattr(CreativePortfolioStagingService, "stage", tracked_stage)

    def recoverable_controller(context, **_kwargs):
        nonlocal controller_calls, controller_recovered
        controller_calls += 1
        if not controller_recovered:
            raise TimeoutError("controller_decision_timeout")
        required = [
            item for item in context.get("goalRequirements") or [] if isinstance(item, dict) and item.get("goalId")
        ]
        goal_by_intent = {str(item.get("intentType") or ""): str(item["goalId"]) for item in required}
        campus_goal = goal_by_intent["campus_visit"]
        night_goal = goal_by_intent["night_view"]
        meal_goal = goal_by_intent["meal"]
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": [campus_goal, night_goal, meal_goal],
                "dayStrategies": [
                    {
                        "dayNumber": 1,
                        "theme": "高校与夜景",
                        "requiredGoalIds": [campus_goal, night_goal],
                        "requiredGoalCounts": {campus_goal: 1, night_goal: 1},
                        "optionalGoalIds": [meal_goal],
                        "pace": "standard",
                        "maxRouteAnchors": 4,
                    },
                    {
                        "dayNumber": 2,
                        "theme": "高校与夜景",
                        "requiredGoalIds": [campus_goal, night_goal],
                        "requiredGoalCounts": {campus_goal: 1, night_goal: 1},
                        "optionalGoalIds": [meal_goal],
                        "pace": "standard",
                        "maxRouteAnchors": 4,
                    },
                ],
                "optionalExperienceBudget": 2,
            },
        }

    def poi(
        amap_id: str,
        name: str,
        longitude: float,
        latitude: float,
        provider_type: str = "科教文化服务;学校;高等院校",
    ) -> MapPoiResponse:
        provider_text = provider_type.casefold()
        if provider_type.startswith("餐饮服务"):
            provider_type_code = "050100"
            claim_key = "local_food"
        elif provider_type.startswith("购物服务"):
            provider_type_code = "060703"
            claim_key = "market_walk"
        elif "艺术" in provider_type:
            provider_type_code = "140500"
            claim_key = "art_walk"
        elif "社区" in provider_type:
            provider_type_code = "110200"
            claim_key = "local_life"
        elif "历史文化" in provider_type or "胡同" in provider_type:
            provider_type_code = "110200"
            claim_key = "heritage_walk"
        elif "夜景" in provider_type or "观景" in provider_type:
            provider_type_code = "110205"
            claim_key = "night_view"
        else:
            provider_type_code = "141201"
            claim_key = None
        return MapPoiResponse(
            id=amap_id,
            name=name,
            type=provider_type,
            city="北京",
            district="海淀区",
            address="高德地址",
            longitude=longitude,
            latitude=latitude,
            category=("餐饮服务" if provider_type.startswith("餐饮服务") else "科教文化服务"),
            providerTypeCode=provider_type_code,
            tags=[item for item in provider_text.split(";") if item],
            businessArea=f"fixture-area-{claim_key or 'shared-required'}",
            openTimeToday=("18:00-22:00" if claim_key == "night_view" else None),
            sourceClaims=(
                [
                    {
                        "claimKey": claim_key,
                        "stance": "support",
                        "summary": f"录制夹具已核验 {name} 与 {claim_key} 体验相符",
                        "sourceName": f"recorded-contract-fixture-{amap_id}",
                        "sourceUrlHash": (amap_id[-1:].lower() or "b") * 64,
                    }
                ]
                if claim_key
                else []
            ),
            photos=[],
            source="amap-place-search",
            sourceNote="高德 WebService POI 搜索",
            confidence=0.94,
        )

    def safe_search(_self, city, keyword, category="all", limit=10, bypass_cache=False):
        keyword_text = str(keyword)
        search_calls.append((city, keyword_text, category, limit, bypass_cache))
        candidates = []
        portfolio_experience_pois = {
            "南锣鼓巷历史文化街区": poi(
                "B000HER01",
                "南锣鼓巷历史文化街区",
                116.403,
                39.937,
                "风景名胜;历史文化街区;胡同",
            ),
            "798艺术区": poi(
                "B000ART01",
                "798艺术区",
                116.495,
                39.984,
                "风景名胜;文化园区;艺术区",
            ),
            "白塔寺社区生活街区": poi(
                "B000LOC01",
                "白塔寺社区生活街区",
                116.363,
                39.924,
                "风景名胜;特色街区;社区",
            ),
            "什刹海历史文化街区": poi(
                "B000HER02",
                "什刹海历史文化街区",
                116.386,
                39.941,
                "风景名胜;历史文化街区;胡同",
            ),
            "三源里菜市场": poi(
                "B000MKT01",
                "三源里菜市场",
                116.462,
                39.956,
                "购物服务;综合市场;菜市场",
            ),
            "潘家园旧货市场": poi(
                "B000MKT02",
                "潘家园旧货市场",
                116.458,
                39.875,
                "购物服务;综合市场;旧货市场",
            ),
            "朝阳门南小街菜市场": poi(
                "B000MKT03",
                "朝阳门南小街菜市场",
                116.433,
                39.922,
                "购物服务;综合市场;菜市场",
            ),
            "东四社区生活街区": poi(
                "B000LOC02",
                "东四社区生活街区",
                116.423,
                39.932,
                "风景名胜;特色街区;社区",
            ),
            "奥林匹克森林公园": poi(
                "B000PRK01",
                "奥林匹克森林公园",
                116.396,
                40.017,
                "风景名胜;公园广场;森林公园",
            ),
            "玉渊潭公园": poi(
                "B000PRK02",
                "玉渊潭公园",
                116.321,
                39.916,
                "风景名胜;公园广场;城市公园",
            ),
            "琉璃厂历史文化街区": poi(
                "B000HER03",
                "琉璃厂历史文化街区",
                116.381,
                39.899,
                "风景名胜;历史文化街区;老街",
            ),
            "二河开21号艺术区": poi(
                "B000ART02",
                "二河开21号艺术区",
                116.307,
                40.014,
                "风景名胜;文化园区;艺术区",
            ),
        }
        if keyword_text in portfolio_experience_pois:
            primary = portfolio_experience_pois[keyword_text]
            # One real AMap category response may contain several area-walk
            # families. The shared universe keeps those facts, while each
            # consumer still has to re-run its own family admission contract.
            primary_claim_keys = {
                str(claim.get("claimKey") or "") for claim in primary.source_claims if isinstance(claim, dict)
            }
            same_family_candidates = [
                item
                for item in portfolio_experience_pois.values()
                if item.id != primary.id
                and any(
                    isinstance(claim, dict) and str(claim.get("claimKey") or "") in primary_claim_keys
                    for claim in item.source_claims
                )
            ]
            other_family_candidates = [
                item
                for item in portfolio_experience_pois.values()
                if item.id != primary.id and item not in same_family_candidates
            ]
            candidates = [
                primary,
                *same_family_candidates,
                *other_family_candidates,
            ]
        elif any(token in keyword_text for token in ("历史街区", "胡同漫步", "文化街区")):
            candidates = [
                item
                for item in portfolio_experience_pois.values()
                if any(
                    isinstance(claim, dict) and str(claim.get("claimKey") or "") == "heritage_walk"
                    for claim in item.source_claims
                )
            ]
        elif any(token in keyword_text for token in ("市井市场", "传统市集")):
            candidates = [
                item
                for item in portfolio_experience_pois.values()
                if any(
                    isinstance(claim, dict) and str(claim.get("claimKey") or "") == "market_walk"
                    for claim in item.source_claims
                )
            ]
        elif any(token in keyword_text for token in ("艺术文化空间", "艺术空间", "艺术街区", "创意园区", "文化园区")):
            candidates = [
                item
                for item in portfolio_experience_pois.values()
                if any(
                    isinstance(claim, dict) and str(claim.get("claimKey") or "") == "art_walk"
                    for claim in item.source_claims
                )
            ]
        elif any(token in keyword_text for token in ("本地生活街区", "本地生活", "社区生活", "生活街区", "社区市场")):
            candidates = [
                item
                for item in portfolio_experience_pois.values()
                if any(
                    isinstance(claim, dict) and str(claim.get("claimKey") or "") == "local_life"
                    for claim in item.source_claims
                )
            ]
        elif keyword_text == "牛街清真餐厅" and allow_meal_candidates:
            candidates = [
                poi(
                    "B000NIUJIE01",
                    "牛街清真餐厅",
                    116.3160,
                    39.9820,
                    "餐饮服务;中餐厅;特色小吃",
                )
            ]
        elif keyword_text == "姚记炒肝店" and allow_meal_candidates:
            candidates = [
                poi(
                    "B000YJC02",
                    "姚记炒肝店",
                    116.3463,
                    39.9825,
                    "餐饮服务;中餐厅;烤鸭店",
                )
            ]
        elif allow_meal_candidates and (category == "food" or re.search(r"(午餐|美食|餐饮|小吃|老字号)", keyword_text)):
            candidates = [
                poi("B000NIUJIE01", "牛街清真餐厅", 116.3160, 39.9820, "餐饮服务;中餐厅;清真菜"),
                poi("B000YJC02", "姚记炒肝店", 116.3463, 39.9825, "餐饮服务;中餐厅;特色小吃"),
            ]
        elif category == "campus" or re.search(r"(高校|大学|校园|校区)", keyword_text):
            candidates = [
                poi("B00000PKU", "北京大学", 116.3109, 39.9929),
                poi("B00000THU", "清华大学", 116.3268, 40.003),
                poi("B00000RUC", "中国人民大学", 116.3214, 39.9709),
                poi("B00000BNU", "北京师范大学", 116.3658, 39.9619),
            ]
        elif re.search(r"(中央电视塔|中央广播电视塔)", keyword_text):
            candidates = [
                poi(
                    "B000TVTWR",
                    "中央电视塔",
                    116.3000,
                    39.9180,
                    "风景名胜;观景点;电视塔",
                )
            ]
        elif category == "scenic" or re.search(r"(夜景|夜游|观景|电视塔|奥林匹克塔)", keyword_text):
            candidates = [
                poi(
                    "B000TOWER",
                    "奥林匹克塔",
                    116.3946,
                    40.0086,
                    "风景名胜;观景点;城市地标",
                )
            ]
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword_text,
            category=category,
            providerName="fake-amap",
            queriedAt=datetime.now(timezone.utc),
            pois=candidates,
        )

    def empty_nearby(*_args, **_kwargs):
        return MapPoiSearchResponse(
            city="北京",
            keyword="餐饮",
            category="food",
            providerName="fake-amap",
            queriedAt=datetime.now(timezone.utc),
            pois=(
                [
                    poi(
                        "B000NIUJIE01",
                        "牛街清真餐厅",
                        116.3160,
                        39.9820,
                        "餐饮服务;中餐厅;清真菜",
                    ).model_copy(update={"distance_meters": 280}),
                    poi("B000YJC02", "姚记炒肝店", 116.3463, 39.9825, "餐饮服务;中餐厅;特色小吃").model_copy(
                        update={"distance_meters": 360}
                    ),
                ]
                if allow_meal_candidates
                else []
            ),
        )

    def recorded_routes(
        _self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        **_kwargs,
    ):
        route_calls.append({"planId": str(plan_id), "segmentCount": len(segments or []), "poiCount": len(pois or [])})
        if not segments or not (
            str(plan_id).startswith("mutation_preflight_")
            or str(plan_id).startswith("partial:")
            or "portfolio" in str(plan_id)
        ):
            return []
        return [
            RouteOption(
                id=f"route_{left.id}_{right.id}",
                plan_id=plan_id,
                from_segment_id=left.id,
                to_segment_id=right.id,
                from_poi_id=left.poi_id,
                to_poi_id=right.poi_id,
                distance_meters=900,
                duration_seconds=720,
                mode=transport_mode,
                provider="amap-webservice",
                is_selected=True,
                polyline=[[116.30, 39.90], [116.31, 39.91]],
                steps=[{"instruction": "公交换乘"}],
                provider_payload={"fixture": "recorded-offline-route"},
            )
            for left, right in zip(segments or [], (segments or [])[1:])
        ]

    def recoverable_initial_plan(_context):
        nonlocal initial_plan_calls
        initial_plan_calls += 1
        if not controller_recovered:
            raise AssertionError("confirmed rule-safe draft must not call the model planner")
        day_slots = [
            {
                "slotId": "recovered_day1_campus",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
                "priority": 90,
            },
            {
                "slotId": "recovered_day1_meal",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "12:00-13:00",
                "startTime": "12:00",
                "durationMinutes": 60,
                "kind": "meal",
                "rawNeed": "当地特色午餐",
                "routeAnchor": True,
                "priority": 75,
            },
            {
                "slotId": "recovered_day1_night",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "19:00-20:30",
                "startTime": "19:00",
                "durationMinutes": 90,
                "kind": "night_view",
                "rawNeed": "夜景观景点",
                "routeAnchor": True,
                "priority": 88,
            },
            {
                "slotId": "recovered_day2_campus",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
                "priority": 90,
            },
            {
                "slotId": "recovered_day2_meal",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "12:00-13:00",
                "startTime": "12:00",
                "durationMinutes": 60,
                "kind": "meal",
                "rawNeed": "当地特色午餐",
                "routeAnchor": True,
                "priority": 75,
            },
            {
                "slotId": "recovered_day2_night",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "19:00-20:30",
                "startTime": "19:00",
                "durationMinutes": 90,
                "kind": "night_view",
                "rawNeed": "夜景观景点",
                "routeAnchor": True,
                "priority": 88,
            },
        ]
        return json.dumps(
            {
                "reply": "已恢复同一规划根的 DaySlot/IntentPool。",
                "mode": "day_slots",
                "daySlots": day_slots,
                "intentPools": [
                    {
                        "poolId": "recovered_campus_pool",
                        "rawNeed": "高校参观",
                        "city": "北京",
                        "intentType": "campus_visit",
                        "targetCount": 2,
                        "preferredTypes": ["大学", "学院", "高等院校"],
                        "rejectedTypes": ["酒店", "住宅"],
                        "routePreference": {"sameDayUnique": True},
                        "assignToSlots": ["recovered_day1_campus", "recovered_day2_campus"],
                        "candidateHints": ["北京大学", "清华大学"],
                        "hintPolicy": "llm_common_knowledge_hint",
                    },
                    {
                        "poolId": "recovered_meal_pool",
                        "rawNeed": "当地特色午餐",
                        "city": "北京",
                        "intentType": "meal",
                        "targetCount": 2,
                        "preferredTypes": ["餐厅", "小吃", "老字号"],
                        "rejectedTypes": ["酒店", "住宅"],
                        "routePreference": {"sameDayUnique": True},
                        "assignToSlots": ["recovered_day1_meal", "recovered_day2_meal"],
                        "candidateHints": ["护国寺小吃（民族园店）", "姚记炒肝店"],
                        "hintPolicy": "llm_common_knowledge_hint",
                    },
                    {
                        "poolId": "recovered_night_pool",
                        "rawNeed": "夜景观景点",
                        "city": "北京",
                        "intentType": "night_view",
                        "targetCount": 2,
                        "preferredTypes": ["观景点", "城市地标"],
                        "rejectedTypes": ["酒店", "住宅"],
                        "routePreference": {"sameDayUnique": True},
                        "assignToSlots": ["recovered_day1_night", "recovered_day2_night"],
                        "candidateHints": ["奥林匹克塔", "中央电视塔"],
                        "hintPolicy": "llm_common_knowledge_hint",
                    },
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        )

    original_portfolio_fallback = CreativePortfolioProviderService.deterministic_fallback

    def recovered_portfolio_fallback(self, **kwargs):
        nonlocal portfolio_fallback_calls
        portfolio_fallback_calls += 1
        generated = original_portfolio_fallback(self, **kwargs)
        payload = generated.model_dump(by_alias=True)
        optional_hints = {
            ("fallback_1_culture_deep_dive", "heritage_walk"): "南锣鼓巷历史文化街区",
            ("fallback_1_culture_deep_dive", "art_walk"): "798艺术区",
            ("fallback_2_local_immersion", "local_life"): "白塔寺社区生活街区",
            ("fallback_2_local_immersion", "heritage_walk"): "什刹海历史文化街区",
            ("fallback_3_food_led", "market_walk"): "三源里菜市场",
            ("fallback_3_food_led", "local_life"): "东四社区生活街区",
            ("fallback_4_nature_relaxed", "park_relax"): "奥林匹克森林公园",
            ("fallback_4_nature_relaxed", "heritage_walk"): "琉璃厂历史文化街区",
            ("fallback_1_photo_night", "art_walk"): "798艺术区",
            ("fallback_1_photo_night", "heritage_walk"): "南锣鼓巷历史文化街区",
            ("fallback_2_citywalk_hidden_gems", "heritage_walk"): "什刹海历史文化街区",
            ("fallback_2_citywalk_hidden_gems", "local_life"): "东四社区生活街区",
            ("fallback_3_family_light", "park_relax"): "奥林匹克森林公园",
            ("fallback_3_family_light", "art_walk"): "二河开21号艺术区",
        }
        family_hints = {
            "heritage_walk": "南锣鼓巷历史文化街区",
            "art_walk": "798艺术区",
            "local_life": "白塔寺社区生活街区",
            "market_walk": "三源里菜市场",
            "park_relax": "奥林匹克森林公园",
            "night_view": "奥林匹克塔",
            "local_food": "牛街清真餐厅",
        }
        family_hint_candidates = {
            "heritage_walk": [
                "南锣鼓巷历史文化街区",
                "什刹海历史文化街区",
                "琉璃厂历史文化街区",
            ],
            "market_walk": ["三源里菜市场", "潘家园旧货市场"],
            "local_life": ["白塔寺社区生活街区", "东四社区生活街区"],
            "art_walk": ["798艺术区", "二河开21号艺术区"],
            "park_relax": ["奥林匹克森林公园", "玉渊潭公园"],
        }
        for proposal in payload["proposals"]:
            day_slots = list(proposal["daySlots"])
            slot_ids = {item["slotId"] for item in day_slots}
            slots_by_id = {item["slotId"]: item for item in day_slots}
            family_hint_offsets = {}
            for pool in proposal["intentPools"]:
                family = str(pool.get("optionalExperienceFamily") or "")
                if family:
                    hinted = family_hint_candidates.get(family) or [
                        optional_hints.get(
                            (proposal["brief"]["briefId"], family),
                            family_hints[family],
                        )
                    ]
                    hint_index = int(family_hint_offsets.get(family) or 0)
                    hint = hinted[hint_index % len(hinted)]
                    family_hint_offsets[family] = hint_index + 1
                    pool["candidateHints"] = [hint]
                    pool["hintPolicy"] = "llm_common_knowledge_hint"
                    pool["entityBindingMode"] = "category"
                    pool["exactEntity"] = None
                    continue
                source_goal_id = pool.get("sourceGoalId") or pool.get("goalId") or pool.get("softGoalId")
                if source_goal_id == "goal_campus_visit":
                    pool["candidateHints"] = [
                        "北京大学",
                        "清华大学",
                        "中国人民大学",
                        "北京师范大学",
                    ]
                    pool["hintPolicy"] = "llm_common_knowledge_hint"
                    pool["entityBindingMode"] = "category"
                    pool["exactEntity"] = None
                if source_goal_id == "goal_night_view":
                    pool["candidateHints"] = ["奥林匹克塔", "中央电视塔"]
                    pool["hintPolicy"] = "llm_common_knowledge_hint"
                    pool["entityBindingMode"] = "category"
                    pool["exactEntity"] = None
                if source_goal_id != "goal_meal":
                    continue
                assigned = next(iter(pool.get("assignToSlots") or []), "")
                day_number = int((slots_by_id.get(assigned) or {}).get("dayNumber") or 0)
                pool["candidateHints"] = ["牛街清真餐厅" if day_number == 1 else "姚记炒肝店"]
                pool["hintPolicy"] = "user_explicit_hint"
                pool["entityBindingMode"] = "exact_entity"
                pool["exactEntity"] = pool["candidateHints"][0]
            for role in proposal["brief"]["dayRoles"]:
                day_number = int(role["dayNumber"])
                target = sum(
                    1 for item in day_slots if int(item["dayNumber"]) == day_number and item.get("routeAnchor")
                )
                role["targetRouteAnchors"] = target
                role["densityEvidence"] = [f"authoritativeSlotCount={target}"]
            route_slot_ids = {item["slotId"] for item in day_slots if item.get("routeAnchor")}
            missing_hint_pools = [
                item.get("poolId")
                for item in proposal["intentPools"]
                if route_slot_ids.intersection(item.get("assignToSlots") or []) and not item.get("candidateHints")
            ]
            assert not missing_hint_pools, {
                "briefId": proposal["brief"]["briefId"],
                "missingHintPools": missing_hint_pools,
            }
        return InitialCreativePortfolio.model_validate(payload)

    provider.decide_autonomy = recoverable_controller  # type: ignore[method-assign]
    provider.generate_initial_plan = recoverable_initial_plan  # type: ignore[method-assign]

    def forbidden_portfolio_provider(*_args, **_kwargs):
        nonlocal portfolio_provider_calls
        portfolio_provider_calls += 1
        raise AssertionError("partial expansion must not call the Creative Portfolio provider")

    provider.generate_initial_portfolio = forbidden_portfolio_provider  # type: ignore[method-assign]
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr(
        CreativePortfolioProviderService,
        "deterministic_fallback",
        recovered_portfolio_fallback,
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService.search", safe_search)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService.search_nearby", empty_nearby)
    monkeypatch.setattr("src.services.route_service.RouteService.build_routes", recorded_routes)

    request_text = (
        "今年国庆参观北京高校两日游，每晚都看北京夜景。10月1日到2日，2天，"
        "中等预算，1人，公交地铁优先。每天午餐想体验当地特色美食。"
    )
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "timeout partial"}).json()
        all_stream_lines = []

        def write_counts():
            with open_db() as connection:
                return (
                    connection.execute(
                        "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                        (session["sessionId"],),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                        (session["sessionId"],),
                    ).fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0],
                )

        def snapshot_for(version_id):
            with open_db() as connection:
                row = connection.execute(
                    "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                    (version_id,),
                ).fetchone()
            assert row is not None
            return json.loads(row["snapshot_json"])

        before_initial = write_counts()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": request_text, "context": {}},
        ) as first_response:
            first_lines = [json.loads(line) for line in first_response.iter_lines() if line]
        all_stream_lines.extend(first_lines)
        first = next(line["data"] for line in reversed(first_lines) if line.get("event") == "message_response")
        first_event_names = [str(line.get("event") or "") for line in first_lines]
        assert first_event_names[-1] == "message_response"
        assert first_event_names.count("message_response") == 1
        assert first_event_names.count("user_turn") == 1
        assert all(name in {"execution_event", "reasoning_status", "user_turn"} for name in first_event_names[:-1])
        assert "reasoning_status" in first_event_names
        assert first["userTurn"]["id"]
        streamed_user_turn = next(line["data"] for line in first_lines if line.get("event") == "user_turn")
        assert streamed_user_turn["id"] == first["userTurn"]["id"]
        first_execution_events = [
            line["data"]
            for line in first_lines
            if line.get("event") == "execution_event" and isinstance(line.get("data"), dict)
        ]
        assert first_execution_events
        assert all(event.get("timestamp") for event in first_execution_events)
        timeout_decision = next(
            event for event in first["assistantTurn"]["planningSteps"] if event.get("type") == "agent_decision"
        )
        timeout_failures = [
            item
            for item in (timeout_decision.get("metadata") or {}).get("controllerFailures") or []
            if isinstance(item, dict) and item.get("failureClass") == "provider_timeout"
        ]
        assert len(timeout_failures) >= 1, timeout_decision
        timeout_performance = [
            item
            for item in (timeout_decision.get("metadata") or {}).get("controllerPerformance") or []
            if isinstance(item, dict)
        ]
        assert any(
            item.get("callKind") == "full" and item.get("providerInvoked") is True for item in timeout_performance
        ), timeout_decision
        assert any(
            item.get("callKind") == "lite"
            and item.get("providerInvoked") is False
            and item.get("captureState") == "worker_queue_saturated"
            for item in timeout_performance
        ), timeout_decision
        assert contract_worker_slots.denied_initial_lite is True
        controller_calls_after_timeout = controller_calls
        option = next(
            (item for item in first["assistantTurn"]["choiceOptions"] if item.get("action") == "retry_model_planning"),
            None,
        )
        assert option is not None, json.dumps(
            {
                "terminalStatus": first.get("terminalStatus"),
                "assistantContent": first.get("assistantTurn", {}).get("content"),
                "choiceOptions": [
                    {
                        "kind": item.get("kind"),
                        "action": item.get("action"),
                        "reasonCode": item.get("reasonCode"),
                    }
                    for item in first.get("assistantTurn", {}).get("choiceOptions") or []
                    if isinstance(item, dict)
                ],
                "controllerFailures": (timeout_decision.get("metadata") or {}).get("controllerFailures"),
                "failureSteps": [
                    {
                        "type": item.get("type"),
                        "status": item.get("status"),
                        "failureReason": item.get("failureReason"),
                    }
                    for item in first.get("assistantTurn", {}).get("planningSteps") or []
                    if isinstance(item, dict) and item.get("failureReason")
                ],
            },
            ensure_ascii=False,
            default=str,
        )
        with open_db() as connection:
            first_assistant_row = connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ? AND session_id = ?",
                (first["assistantTurn"]["id"], session["sessionId"]),
            ).fetchone()
        assert first_assistant_row is not None
        persisted_first_response = json.loads(first_assistant_row["agent_response_json"])
        persisted_safe_choices = [
            item
            for item in persisted_first_response.get("choiceOptions") or []
            if isinstance(item, dict)
            and item.get("id") == option["id"]
            and item.get("action") == "retry_model_planning"
        ]
        assert len(persisted_safe_choices) == 1
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={
                "content": "选择 Agent 选项",
                "context": {
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": first["assistantTurn"]["id"],
                        "choiceId": option["id"],
                    }
                },
            },
        ) as second_response:
            second_lines = [json.loads(line) for line in second_response.iter_lines() if line]
        all_stream_lines.extend(second_lines)
        second = next(line["data"] for line in reversed(second_lines) if line.get("event") == "message_response")
        after_partial = write_counts()
        preview_counts_before = write_counts()
        preview_session = client.get(f"/api/agent/sessions/{session['sessionId']}")
        assert preview_session.status_code == 200
        preview_counts_after = write_counts()

        # A controller timeout may only offer another Controller attempt. It
        # must not publish a deterministic business question or partial draft.
        assert second["terminalStatus"] == "needs_confirmation"
        assert second["version"] is None
        assert after_partial == before_initial
        assert preview_counts_after == preview_counts_before == before_initial
        assert all(
            item.get("action") != "confirm_rule_safe_draft"
            for item in second["assistantTurn"].get("choiceOptions") or []
        )
        return

        def post_choice(source_turn_id, choice_id, *, manual_value=None):
            selected = {
                "sourceAssistantTurnId": source_turn_id,
                "choiceId": choice_id,
            }
            if manual_value is not None:
                selected["manualValue"] = manual_value
            with client.stream(
                "POST",
                f"/api/agent/sessions/{session['sessionId']}/messages/stream",
                json={
                    "content": "选择 Agent 选项",
                    "context": {"selectedAgentChoice": selected},
                },
            ) as response:
                lines = [json.loads(line) for line in response.iter_lines() if line]
                all_stream_lines.extend(lines)
                assert response.status_code == 200, lines
            terminal = next(
                (line["data"] for line in reversed(lines) if line.get("event") == "message_response"),
                None,
            )
            assert terminal is not None, json.dumps(lines[-5:], ensure_ascii=False, default=str)
            return terminal

        def post_expansion_text(content):
            with client.stream(
                "POST",
                f"/api/agent/sessions/{session['sessionId']}/messages/stream",
                json={"content": content, "context": {}},
            ) as response:
                lines = [json.loads(line) for line in response.iter_lines() if line]
                all_stream_lines.extend(lines)
                assert response.status_code == 200, lines
            terminal = next(
                (line["data"] for line in reversed(lines) if line.get("event") == "message_response"),
                None,
            )
            assert terminal is not None, lines[-5:]
            return terminal

        assert not any(
            item.get("action") == "adopt_active_partial" for item in second["assistantTurn"]["choiceOptions"]
        )
        readonly_current_draft = next(
            item
            for item in second["assistantTurn"]["choiceOptions"]
            if item.get("kind") == "portfolio_comparison_readonly"
        )
        assert readonly_current_draft["comparisonProjection"]["comparisonRole"] == "current_active_draft"
        adopted = second
        after_adoption = write_counts()
        with open_db() as connection:
            adopted_row = connection.execute(
                "SELECT agent_request_json, agent_response_json FROM conversation_turns WHERE id = ?",
                (adopted["assistantTurn"]["id"],),
            ).fetchone()
            adopted_request = json.loads(adopted_row["agent_request_json"])
            adopted_response = json.loads(adopted_row["agent_response_json"])
        adopted_grounding = adopted_response.get("grounding") or {}
        assert isinstance(
            adopted_response.get("initialPlan") or adopted_grounding.get("initialPlan"),
            dict,
        ), sorted(adopted_response)
        assert isinstance(
            adopted_response.get("pipelineContext") or adopted_grounding.get("pipelineContext"),
            dict,
        ), sorted(adopted_response)
        assert isinstance(
            adopted_request.get("planningDirective") or adopted_response.get("planningDirective"),
            dict,
        ), {
            "requestKeys": sorted(adopted_request),
            "responseKeys": sorted(adopted_response),
            "groundingKeys": sorted(adopted_grounding),
        }

        controller_recovered = True
        allow_meal_candidates = True
        before_expansion = write_counts()
        controller_calls_before_expansion = controller_calls
        initial_plan_calls_before_expansion = initial_plan_calls
        portfolio_fallback_calls_before_expansion = portfolio_fallback_calls
        portfolio_provider_calls_before_expansion = portfolio_provider_calls
        stage_calls_before_expansion = len(stage_brief_batches)
        comparison_current = adopted
        expansion_focus_brief_ids = []
        expansion_write_deltas = []
        expansion_root_portfolio_ids = []
        checkpoint_expansion_resume_count = 0
        next_brief_id_sequence = []
        later_projections = []
        later_proposal_ids = []
        expansion_trace_records = []

        for _expansion_index in range(5):
            expansion_option = next(
                item
                for item in comparison_current["assistantTurn"]["choiceOptions"]
                if item.get("kind") in {"portfolio_partial_more_plans", "portfolio_more_plans"}
            )
            with open_db() as connection:
                root_summary_row = connection.execute(
                    "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                    (expansion_option["rootPortfolioId"],),
                ).fetchone()
            if _expansion_index == 0:
                # RuleSafeDraftExecutor owns only the committed partial and a
                # neutral, turn-scoped direction checkpoint. Creative Portfolio
                # rows and skeletons are created only after this opaque choice
                # enters DraftItineraryExecutor.
                assert root_summary_row is None
                assert not isinstance(adopted_response.get("creativePortfolio"), dict)
                persisted_next_brief_id = str(expansion_option.get("focusBriefId") or "")
                assert expansion_option["expansionFocusMode"] == "discover_next"
                assert str(expansion_option.get("expiresAt") or "")
            else:
                assert root_summary_row is not None
                root_summary_before_expansion = json.loads(root_summary_row["summary_json"] or "{}")
                persisted_next_brief_id = str(root_summary_before_expansion.get("nextBriefId") or "")
                persisted_frontier = root_summary_before_expansion.get("creativeExplorationFrontier") or {}
                authoritative_focus = persisted_next_brief_id or str(
                    persisted_frontier.get("currentFocusBriefId")
                    or root_summary_before_expansion.get("focusBriefId")
                    or ""
                )
                assert str(expansion_option.get("focusBriefId") or "") == authoritative_focus, {
                    "option": expansion_option,
                    "summaryFocusBriefId": root_summary_before_expansion.get("focusBriefId"),
                    "frontier": persisted_frontier,
                }
                if persisted_next_brief_id:
                    assert expansion_option["expansionFocusMode"] == "exact"
                else:
                    assert expansion_option["expansionFocusMode"] == "discover_next"
                assert root_summary_before_expansion["supersededBriefIds"] == []
            if persisted_next_brief_id:
                assert persisted_next_brief_id not in expansion_focus_brief_ids
            expansion_root_portfolio_ids.append(str(expansion_option["rootPortfolioId"]))

            counts_before_one_expansion = write_counts()
            expanded = post_choice(
                comparison_current["assistantTurn"]["id"],
                expansion_option["id"],
            )
            counts_after_one_expansion = write_counts()
            expansion_write_deltas.append(
                tuple(
                    after - before
                    for before, after in zip(
                        counts_before_one_expansion,
                        counts_after_one_expansion,
                    )
                )
            )
            assert expansion_write_deltas[-1] == (0, 0, 0)
            assert controller_calls == controller_calls_before_expansion, json.dumps(
                {
                    "expansionIndex": _expansion_index,
                    "option": expansion_option,
                    "choiceTrace": expanded.get("userTurn", {}).get("structuredChoiceTrace"),
                    "content": expanded.get("assistantTurn", {}).get("content"),
                    "steps": expanded.get("assistantTurn", {}).get("planningSteps", [])[-8:],
                },
                ensure_ascii=False,
                default=str,
            )
            assert initial_plan_calls == initial_plan_calls_before_expansion
            assert (
                portfolio_fallback_calls_before_expansion + 1
                <= portfolio_fallback_calls
                <= portfolio_fallback_calls_before_expansion + _expansion_index + 1
            )

            with open_db() as connection:
                expanded_row = connection.execute(
                    "SELECT agent_request_json, agent_response_json FROM conversation_turns WHERE id = ?",
                    (expanded["assistantTurn"]["id"],),
                ).fetchone()
            assert expanded_row is not None
            expanded_pipeline_context = json.loads(expanded_row["agent_request_json"] or "{}")
            expanded_response_payload = json.loads(expanded_row["agent_response_json"] or "{}")
            if _expansion_index == 1:
                rebound_choice = expanded_pipeline_context.get("selectedAgentChoice") or {}
                assert rebound_choice.get("sourceAssistantTurnId") == comparison_current["assistantTurn"]["id"]
                assert rebound_choice.get("choiceId") == expansion_option["id"]
                assert (rebound_choice.get("option") or {}).get("kind") in {
                    "portfolio_partial_more_plans",
                    "portfolio_more_plans",
                }
            expanded_grounding = (
                expanded_response_payload.get("grounding")
                if isinstance(expanded_response_payload.get("grounding"), dict)
                else {}
            )
            expanded_report_context = (
                expanded_response_payload.get("pipelineContext")
                if isinstance(expanded_response_payload.get("pipelineContext"), dict)
                else expanded_grounding.get("pipelineContext")
            )
            assert expanded_pipeline_context.get("_partialPlanExpansionAuthorized") is True
            assert expanded_pipeline_context.get("explicitRuleSafeDraft") is False
            assert isinstance(expanded_report_context, dict), json.dumps(
                {
                    "content": expanded.get("assistantTurn", {}).get("content"),
                    "terminalStatus": expanded.get("terminalStatus"),
                    "warnings": expanded.get("warnings"),
                    "responseKeys": sorted(expanded_response_payload),
                    "requestClaim": expanded_pipeline_context.get("partialPlanExpansionClaim"),
                    "frontierBeforeExpansion": {
                        key: persisted_frontier.get(key)
                        for key in (
                            "frontierState",
                            "remainingBudget",
                            "continuationRound",
                            "visibleDirectionSignatures",
                            "attemptedDirectionSignatures",
                            "acceptedDirectionSignatures",
                            "rejectedDirectionSignatures",
                            "inFlightDirectionSignatures",
                            "generatedDirectionSignatures",
                            "nextBriefId",
                        )
                    },
                    "expansionOption": expansion_option,
                    "steps": expanded.get("assistantTurn", {}).get("planningSteps", [])[-10:],
                },
                ensure_ascii=False,
                default=str,
            )
            if expansion_option["expansionFocusMode"] == "exact":
                assert expanded_report_context.get("checkpointExpansionResume") is True
                checkpoint_expansion_resume_count += 1
            else:
                assert expanded_report_context.get("checkpointExpansionResume") is not True
            request_focus_brief_id = str(expanded_pipeline_context.get("focusBriefId") or "")
            assert len(stage_brief_batches) == stage_calls_before_expansion + _expansion_index + 1, json.dumps(
                {
                    "expansionIndex": _expansion_index,
                    "requestFocusBriefId": request_focus_brief_id,
                    "stageBriefBatches": stage_brief_batches,
                    "content": expanded.get("assistantTurn", {}).get("content"),
                    "terminalStatus": expanded.get("terminalStatus"),
                    "choices": [
                        {
                            "kind": item.get("kind"),
                            "action": item.get("action"),
                            "focusBriefId": item.get("focusBriefId"),
                        }
                        for item in expanded.get("assistantTurn", {}).get("choiceOptions", [])
                        if isinstance(item, dict)
                    ],
                    "steps": expanded.get("assistantTurn", {}).get("planningSteps", [])[-10:],
                },
                ensure_ascii=False,
                default=str,
            )
            assert len(stage_brief_batches[-1]) == 1
            actual_focus_brief_id = stage_brief_batches[-1][0]
            assert actual_focus_brief_id
            assert request_focus_brief_id == actual_focus_brief_id
            if expansion_option["expansionFocusMode"] == "exact":
                assert actual_focus_brief_id == persisted_next_brief_id
            else:
                assert actual_focus_brief_id != persisted_next_brief_id
            assert actual_focus_brief_id not in expansion_focus_brief_ids
            expansion_focus_brief_ids.append(actual_focus_brief_id)
            next_brief_id_sequence.append(actual_focus_brief_id)

            new_projections = [
                item["comparisonProjection"]
                for item in expanded["assistantTurn"]["choiceOptions"]
                if isinstance(item.get("comparisonProjection"), dict)
            ]
            assert expanded["assistantTurn"]["comparisonProjectionUpdateMode"] == "append", (
                f"expansionIndex={_expansion_index}; "
                f"terminalStatus={expanded.get('terminalStatus')}; "
                f"outcome={(expanded['assistantTurn'].get('structuredChoiceTrace') or {}).get('outcome')}; "
                f"failureReason={expanded_response_payload.get('failureReason')}; "
                f"briefDetails={stage_brief_details[-1:]}; "
                f"stageMetrics={stage_metric_snapshots[-1:]}; "
                f"content={expanded['assistantTurn'].get('content')}"
            )
            assert expanded["assistantTurn"]["comparisonProjectionUpdateMode"] == "append", json.dumps(
                {
                    "expansionIndex": _expansion_index,
                    "content": expanded["assistantTurn"].get("content"),
                    "choiceTrace": expanded["assistantTurn"].get("structuredChoiceTrace"),
                    "newProjections": new_projections,
                    "terminalStatus": expanded.get("terminalStatus"),
                    "choices": [
                        {
                            "kind": item.get("kind"),
                            "description": item.get("description"),
                        }
                        for item in expanded["assistantTurn"].get("choiceOptions") or []
                    ],
                    "steps": [
                        {
                            "type": step.get("type"),
                            "status": step.get("status"),
                            "detail": step.get("detail"),
                            "failureReason": step.get("failureReason"),
                        }
                        for step in expanded["assistantTurn"].get("planningSteps", [])
                    ],
                },
                ensure_ascii=False,
                default=str,
            )
            assert expanded["assistantTurn"]["structuredChoiceTrace"]["outcome"]["reason"] in {
                "new_verified_proposal",
                "new_adoption_ready_partial_proposal",
            }
            hydrated = client.get(f"/api/agent/sessions/{session['sessionId']}")
            assert hydrated.status_code == 200
            hydrated_expansion_turn = next(
                turn for turn in hydrated.json()["turns"] if turn["id"] == expanded["assistantTurn"]["id"]
            )
            assert hydrated_expansion_turn["comparisonProjectionUpdateMode"] == "append"
            assert new_projections, {
                "content": expanded["assistantTurn"]["content"],
                "warnings": expanded.get("warnings"),
                "terminalStatus": expanded.get("terminalStatus"),
                "steps": expanded["assistantTurn"].get("planningSteps", [])[-8:],
                "choices": [
                    {
                        "action": item.get("action"),
                        "kind": item.get("kind"),
                        "label": item.get("label"),
                    }
                    for item in expanded["assistantTurn"]["choiceOptions"]
                ],
            }
            assert all(
                projection["planningSelectionRootTurnId"] == adopted_request["planningSelectionRootTurnId"]
                for projection in new_projections
            )
            new_proposal_ids = [
                str(projection.get("proposalId") or "")
                for projection in new_projections
                if str(projection.get("proposalId") or "")
            ]
            assert new_proposal_ids
            later_projections.extend(new_projections)
            later_proposal_ids.extend(new_proposal_ids)
            expansion_trace_records.append(
                {
                    "index": _expansion_index + 1,
                    "sourceAssistantTurnId": comparison_current["assistantTurn"]["id"],
                    "choiceId": expansion_option["id"],
                    "action": expansion_option.get("action"),
                    "expansionFocusMode": expansion_option.get("expansionFocusMode"),
                    "requestedFocusBriefId": expansion_option.get("focusBriefId"),
                    "resolvedFocusBriefId": actual_focus_brief_id,
                    "planningSelectionRootTurnId": expansion_option.get("planningSelectionRootTurnId"),
                    "rootPortfolioId": expansion_option.get("rootPortfolioId"),
                    "requestContractFingerprint": expansion_option.get("requestContractFingerprint"),
                    "expectedBaseVersionId": expansion_option.get("expectedBaseVersionId"),
                    "checkpointExpansionResume": bool(expanded_report_context.get("checkpointExpansionResume")),
                    "stagedBriefIds": [actual_focus_brief_id],
                    "proposalIds": new_proposal_ids,
                    "writeDelta": list(expansion_write_deltas[-1]),
                }
            )
            comparison_current = expanded

        after_expansion = write_counts()
        assert after_expansion == before_expansion
        assert checkpoint_expansion_resume_count == sum(
            1 for item in expansion_trace_records if item["expansionFocusMode"] == "exact"
        )
        exact_focus_stage_batches = stage_brief_batches[stage_calls_before_expansion:]
        assert exact_focus_stage_batches == [[item] for item in expansion_focus_brief_ids]
        with open_db() as connection:
            root_summary_after_expansion = json.loads(
                connection.execute(
                    "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                    (expansion_root_portfolio_ids[0],),
                ).fetchone()["summary_json"]
                or "{}"
            )
            later_proposal_rows = connection.execute(
                f"""SELECT id, portfolio_id, choice_id, status, brief_json, snapshot_json,
                    verifier_json, score_json, evidence_json, generation_lineage_json,
                    canonical_signature
                FROM agent_plan_proposals
                WHERE id IN ({",".join("?" for _ in later_proposal_ids)})""",
                tuple(later_proposal_ids),
            ).fetchall()
        assert len(later_proposal_rows) == len(set(later_proposal_ids)) == 5
        assert {str(json.loads(row["brief_json"]).get("briefId") or "") for row in later_proposal_rows} == set(
            expansion_focus_brief_ids
        )
        assert all(
            row["status"]
            in {
                "offered",
                "selected",
                "partial_preview",
                "route_pending",
                "route_partial",
                "route_ready",
                "adoption_ready",
            }
            and json.loads(row["score_json"]).get("hardConstraintPassed") is True
            and bool(row["canonical_signature"])
            for row in later_proposal_rows
        ), json.dumps(
            [
                {
                    "id": row["id"],
                    "status": row["status"],
                    "verifierPassed": json.loads(row["verifier_json"]).get("passed"),
                    "blockingReasons": json.loads(row["verifier_json"]).get("blockingReasons"),
                    "readiness": json.loads(row["verifier_json"]).get("readiness"),
                    "verifier": json.loads(row["verifier_json"]),
                    "hardConstraintPassed": json.loads(row["score_json"]).get("hardConstraintPassed"),
                }
                for row in later_proposal_rows
            ],
            ensure_ascii=False,
            default=str,
        ) + json.dumps(stage_metric_snapshots[-2:], ensure_ascii=False, default=str)
        assert all(json.loads(row["verifier_json"]).get("passed") is True for row in later_proposal_rows), json.dumps(
            {
                "searchCalls": search_calls,
                "stageMetrics": stage_metric_snapshots,
                "proposalSummaries": [
                    {
                        "briefId": json.loads(row["brief_json"]).get("briefId"),
                        "families": json.loads(row["brief_json"]).get("themeFamilies"),
                        "passed": json.loads(row["verifier_json"]).get("passed"),
                        "strictFailures": json.loads(row["verifier_json"]).get("strictFailures"),
                        "pending": len(json.loads(row["snapshot_json"]).get("portfolioPendingSlots") or []),
                    }
                    for row in later_proposal_rows
                ],
            },
            ensure_ascii=False,
            default=str,
        )
        assert len({row["canonical_signature"] for row in later_proposal_rows}) == 5
        proposal_briefs = [json.loads(row["brief_json"]) for row in later_proposal_rows]
        proposal_snapshots = [json.loads(row["snapshot_json"]) for row in later_proposal_rows]
        proposal_verifiers = [json.loads(row["verifier_json"]) for row in later_proposal_rows]
        assert len({str(brief.get("title") or "") for brief in proposal_briefs}) == 5
        assert all(not snapshot.get("portfolioPendingSlots") for snapshot in proposal_snapshots)
        assert all(
            verifier.get("pendingHardSlotCount") == 0
            and verifier.get("pendingSoftSlotCount") == 0
            and verifier.get("dayAnchorActuals") == verifier.get("dayAnchorTargets")
            and (verifier.get("admissionMaterializationAudit") or {}).get("countInvariantPassed") is True
            and (verifier.get("admissionMaterializationAudit") or {}).get("selectedAdmittedCandidateCount")
            == (verifier.get("admissionMaterializationAudit") or {}).get("actualProposalAnchorCount")
            for verifier in proposal_verifiers
        )
        flexible_physical_id_sets = []
        for snapshot in proposal_snapshots:
            flexible_physical_id_sets.append(
                {
                    str((segment.get("poi") or {}).get("amapId") or "").upper()
                    for day in snapshot.get("days") or []
                    for segment in day.get("segments") or []
                    if isinstance(segment, dict)
                    and isinstance(segment.get("semanticMetadata"), dict)
                    and (
                        segment["semanticMetadata"].get("portfolioOptional") is True
                        or str(segment["semanticMetadata"].get("optionalExperienceFamily") or "").strip()
                    )
                    and isinstance(segment.get("poi"), dict)
                    and str((segment.get("poi") or {}).get("source") or "") == "amap-place-search"
                    and str((segment.get("poi") or {}).get("amapId") or "")
                }
            )
        assert all(flexible_physical_id_sets)
        assert len(set().union(*flexible_physical_id_sets)) == sum(len(item) for item in flexible_physical_id_sets)
        final_brief_state = [
            item
            for item in root_summary_after_expansion.get("briefGenerationState") or []
            if isinstance(item, dict) and str(item.get("briefId") or "")
        ]
        final_ordered_brief_ids = [str(item["briefId"]) for item in final_brief_state]
        persisted_completed_brief_ids = [
            str(item["briefId"]) for item in final_brief_state if item.get("status") == "completed"
        ]
        final_remaining_brief_ids = [
            str(item["briefId"]) for item in final_brief_state if item.get("status") == "remaining"
        ]
        final_failed_brief_ids = [str(item["briefId"]) for item in final_brief_state if item.get("status") == "failed"]
        assert len(final_ordered_brief_ids) >= 3
        assert len(persisted_completed_brief_ids) >= 2
        assert set(expansion_focus_brief_ids).issubset(set(persisted_completed_brief_ids))
        assert all(item in final_ordered_brief_ids for item in next_brief_id_sequence)
        assert comparison_current["version"]["id"] == adopted["version"]["id"]

        current = adopted
        current_version_id = adopted["version"]["id"]
        committed_versions = []
        pending_slot_count_sequence = [len(snapshot_for(current_version_id).get("portfolioPendingSlots") or [])]
        choice_write_deltas = []
        slot_choice_trace_records = []
        choice_counts_after_commit = after_expansion
        last_candidate_source = None
        last_candidate_id = None
        for day_number, manual_name in (
            (1, "护国寺小吃（民族园店）"),
            (2, "姚记炒肝店"),
        ):
            manual_option = next(
                (
                    item
                    for item in current["assistantTurn"]["choiceOptions"]
                    if item.get("action") == "manual_continuation" and item.get("dayNumber") == day_number
                ),
                None,
            )
            assert manual_option is not None, [
                {
                    "action": item.get("action"),
                    "kind": item.get("kind"),
                    "dayNumber": item.get("dayNumber"),
                    "planningSlotId": item.get("planningSlotId"),
                }
                for item in current["assistantTurn"]["choiceOptions"]
            ]
            searched = post_choice(
                current["assistantTurn"]["id"],
                manual_option["id"],
                manual_value=manual_name,
            )
            counts_after_search = write_counts()
            expected_before_search = choice_counts_after_commit
            assert counts_after_search == expected_before_search
            assert searched["version"]["id"] == current_version_id
            assert all(
                item.get("expectedBaseVersionId") == current_version_id
                for item in searched["assistantTurn"]["choiceOptions"]
                if item.get("action")
                in {
                    "resume_density_candidate",
                    "refresh_density_candidates",
                    "expand_density_nearby",
                    "manual_continuation",
                }
            )
            candidate = next(
                item
                for item in searched["assistantTurn"]["choiceOptions"]
                if item.get("action") == "resume_density_candidate"
                and item.get("dayNumber") == day_number
                and item.get("amapId")
            )
            last_candidate_source = searched["assistantTurn"]["id"]
            last_candidate_id = candidate["id"]
            choice_counts_before_commit = write_counts()
            current = post_choice(last_candidate_source, last_candidate_id)
            if current.get("version") is None:
                raise AssertionError(
                    json.dumps(
                        {
                            "content": current.get("assistantTurn", {}).get("content"),
                            "warnings": current.get("warnings"),
                            "terminalStatus": current.get("terminalStatus"),
                            "choiceTrace": current.get("userTurn", {}).get("structuredChoiceTrace"),
                            "routeCalls": route_calls[-12:],
                        },
                        ensure_ascii=False,
                    )
                )
            assert current.get("version") is not None, {
                "content": current.get("assistantTurn", {}).get("content"),
                "warnings": current.get("warnings"),
                "terminalStatus": current.get("terminalStatus"),
                "choiceTrace": current.get("userTurn", {}).get("structuredChoiceTrace"),
            }
            current_version_id = current["version"]["id"]
            committed_versions.append(current["version"]["id"])
            choice_counts_after_commit = write_counts()
            choice_write_deltas.append(
                tuple(
                    after - before
                    for before, after in zip(
                        choice_counts_before_commit,
                        choice_counts_after_commit,
                    )
                )
            )
            slot_choice_trace_records.append(
                {
                    "sourceAssistantTurnId": last_candidate_source,
                    "choiceId": last_candidate_id,
                    "candidateRecordId": candidate.get("candidateRecordId"),
                    "amapId": candidate.get("amapId"),
                    "briefId": candidate.get("briefId"),
                    "poolId": candidate.get("poolId"),
                    "planningSlotId": candidate.get("planningSlotId"),
                    "dayNumber": candidate.get("dayNumber"),
                    "expectedBaseVersionId": candidate.get("expectedBaseVersionId"),
                    "resultVersionId": current_version_id,
                    "writeDelta": list(choice_write_deltas[-1]),
                }
            )
            pending_slot_count_sequence.append(len(snapshot_for(current_version_id).get("portfolioPendingSlots") or []))

        with open_db() as connection:
            before_duplicate = (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                    (session["sessionId"],),
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                    (session["sessionId"],),
                ).fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
                    (session["activePlanId"],),
                ).fetchone()[0],
            )
        duplicate = post_choice(last_candidate_source, last_candidate_id)
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second["version"] is not None, json.dumps(
        {
            "searchCalls": search_calls,
            "warnings": second["warnings"],
            "content": second["assistantTurn"]["content"],
            "lastSteps": second["assistantTurn"]["planningSteps"][-5:],
        },
        ensure_ascii=False,
        indent=2,
    )
    projections = [
        item["comparisonProjection"]
        for item in second["assistantTurn"]["choiceOptions"]
        if isinstance(item.get("comparisonProjection"), dict)
    ]
    assert len(projections) == 1
    assert projections[0]["activeVersionId"] == second["version"]["id"]
    assert [slot["dayNumber"] for slot in projections[0]["pendingSlots"]] == [1, 2], json.dumps(
        projections[0]["pendingSlots"], ensure_ascii=False, indent=2
    )
    assert any(
        line["event"] == "execution_event" and line["data"]["type"] == "portfolio_plan_visible" for line in second_lines
    )
    exact_controls = [
        item
        for item in second["assistantTurn"]["choiceOptions"]
        if item.get("kind") in {"portfolio_density_retry", "custom_input"}
    ]
    assert len(exact_controls) == 6
    assert all(item.get("expectedBaseVersionId") == second["version"]["id"] for item in exact_controls)
    assert all(
        item.get(key)
        for item in exact_controls
        for key in (
            "planningSelectionRootTurnId",
            "rootPortfolioId",
            "focusBriefId",
            "requestContractFingerprint",
            "briefId",
            "poolId",
            "planningSlotId",
            "dayNumber",
        )
    )
    assert controller_calls >= 1
    assert controller_calls_after_timeout >= 1
    assert tuple(after - before for before, after in zip(before_initial, after_partial)) == (1, 1, 0)
    assert after_adoption == after_partial
    assert after_expansion == before_expansion
    assert pending_slot_count_sequence == [2, 1, 0]
    assert choice_write_deltas == [(1, 1, 0), (1, 1, 0)]
    with open_db() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()[0]
            == 3
        )
        final_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (committed_versions[-1],),
            ).fetchone()["snapshot_json"]
        )
        assert final_snapshot.get("portfolioPendingSlots") == []
        assert loaded["activeVersionId"] == committed_versions[-1]
        assert duplicate["version"]["id"] == committed_versions[-1]
        after_duplicate = (
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
                (session["activePlanId"],),
            ).fetchone()[0],
        )
        assert tuple(after - before for before, after in zip(before_duplicate, after_duplicate)) == (0, 0, 0)
        persisted_pois = connection.execute(
            """
            SELECT p.source, p.amap_id, p.longitude, p.latitude
            FROM itinerary_segments AS segment
            JOIN pois AS p ON p.id = segment.poi_id
            WHERE segment.plan_id = ?
            """,
            (session["activePlanId"],),
        ).fetchall()
        assert persisted_pois
        assert all(
            row["source"] == "amap-place-search"
            and row["amap_id"]
            and row["longitude"] is not None
            and row["latitude"] is not None
            for row in persisted_pois
        )

    safe_partial_write_delta = tuple(after - before for before, after in zip(before_initial, after_partial))
    preview_write_delta = tuple(after - before for before, after in zip(preview_counts_before, preview_counts_after))
    adoption_write_delta = tuple(after - before for before, after in zip(after_partial, after_adoption))
    expansion_write_delta = tuple(after - before for before, after in zip(before_expansion, after_expansion))
    duplicate_write_delta = tuple(after - before for before, after in zip(before_duplicate, after_duplicate))
    comparison_scope_drift_count = sum(
        1
        for projection in [*projections, *later_projections]
        if projection.get("planningSelectionRootTurnId") != adopted_request.get("planningSelectionRootTurnId")
    )
    comparison_portfolio_count = len(
        {
            str(projection.get("rootPortfolioId") or "")
            for projection in [*projections, *later_projections]
            if str(projection.get("rootPortfolioId") or "")
        }
    )
    comparison_proposal_ids = {
        str(projection.get("proposalId") or projection.get("versionId") or "")
        for projection in [*projections, *later_projections]
        if str(projection.get("proposalId") or projection.get("versionId") or "")
    }
    initial_partial_brief_id = str(
        (snapshot_for(adopted["version"]["id"]).get("portfolioSelectionContext") or {}).get("focusBriefId") or ""
    )
    assert initial_partial_brief_id
    final_completed_brief_ids = [initial_partial_brief_id, *persisted_completed_brief_ids]
    initial_visible_brief_ids = [initial_partial_brief_id]
    visible_brief_ids = {
        initial_visible_brief_ids[0],
        *expansion_focus_brief_ids,
    }
    root_portfolio_id = str(projections[0].get("rootPortfolioId") or "")
    root_portfolio_drift_count = sum(
        1 for portfolio_id in expansion_root_portfolio_ids if portfolio_id != root_portfolio_id
    )
    brief_scope_drift_count = sum(
        1
        for expected, actual in zip(
            next_brief_id_sequence,
            expansion_focus_brief_ids,
        )
        if expected != actual or actual not in final_ordered_brief_ids
    )
    canonical_duplicate_expansion_count = len(later_proposal_rows) - len(
        {str(row["canonical_signature"] or "") for row in later_proposal_rows}
    )
    assert comparison_scope_drift_count == 0
    assert comparison_portfolio_count == 1
    assert len(comparison_proposal_ids) >= 2
    pending_keys = {
        (
            str(slot.get("briefId") or ""),
            str(slot.get("poolId") or ""),
            str(slot.get("planningSlotId") or ""),
            int(slot.get("dayNumber") or 0),
        )
        for slot in projections[0]["pendingSlots"]
    }
    control_keys = {
        (
            str(item.get("briefId") or ""),
            str(item.get("poolId") or ""),
            str(item.get("planningSlotId") or ""),
            int(item.get("dayNumber") or 0),
        )
        for item in exact_controls
    }
    pending_slot_scope_drift_count = len(pending_keys.symmetric_difference(control_keys))
    active_timeline_projection_mismatch_count = sum(
        int(mismatch)
        for mismatch in (
            projections[0].get("activeVersionId") != second["version"]["id"],
            expanded["version"]["id"] != adopted["version"]["id"],
            loaded.get("activeVersionId") != committed_versions[-1],
            duplicate["version"]["id"] != committed_versions[-1],
            final_snapshot != snapshot_for(loaded["activeVersionId"]),
        )
    )
    invalid_persisted_anchor_count = sum(
        1
        for row in persisted_pois
        if row["source"] != "amap-place-search"
        or not row["amap_id"]
        or row["longitude"] is None
        or row["latitude"] is None
    )
    serialized_stream = json.dumps(all_stream_lines, ensure_ascii=False, default=str)
    raw_internal_error_count = sum(
        serialized_stream.count(marker)
        for marker in (
            "portfolio_partial_anchor_grounding_evidence_missing",
            "Traceback (most recent call last)",
            "KeyError: 'timeWindow'",
        )
    )
    first_visible_plan_blocked = bool(
        expansion_write_delta != (0, 0, 0)
        or expanded["version"]["id"] != second["version"]["id"]
        or len(later_proposal_rows) == 0
    )
    later_verified_plan_appended = bool(later_proposal_rows) and any(
        json.loads(row["verifier_json"]).get("passed") is True
        and json.loads(row["score_json"]).get("hardConstraintPassed") is True
        for row in later_proposal_rows
    )
    assert "部分时间轴" in second["assistantTurn"]["content"]
    assert raw_internal_error_count == 0
    persisted_partial = snapshot_for(adopted["version"]["id"])
    persisted_partial_anchor_metadata = [
        segment.get("semanticMetadata") or {}
        for day in persisted_partial.get("days") or []
        for segment in day.get("segments") or []
        if (segment.get("semanticMetadata") or {}).get("routeAnchor") is True
    ]
    assert persisted_partial_anchor_metadata
    assert all(
        metadata.get("creativeBriefId")
        and metadata.get("poolId")
        and metadata.get("planningSlotId")
        and metadata.get("sourceGoalId")
        for metadata in persisted_partial_anchor_metadata
    )

    representative_trace = None
    if os.getenv("TRIP_STABILITY_TRACE_EXPORT") == "1":

        def json_object(value):
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            return {}

        def scoped_anchor_evidence(snapshot):
            evidence = []
            for day in snapshot.get("days") or []:
                day_number = int(day.get("dayNumber") or 0)
                for segment in day.get("segments") or []:
                    semantics = segment.get("semanticMetadata") or {}
                    poi = segment.get("poi") or {}
                    if not semantics.get("routeAnchor"):
                        continue
                    evidence.append(
                        {
                            "segmentId": segment.get("id"),
                            "briefId": semantics.get("creativeBriefId") or semantics.get("briefId"),
                            "poolId": semantics.get("poolId"),
                            "planningSlotId": semantics.get("slotId") or semantics.get("planningSlotId"),
                            "dayNumber": day_number,
                            "sourceGoalId": semantics.get("sourceGoalId") or semantics.get("goalId"),
                            "amapId": poi.get("amapId"),
                            "source": poi.get("source"),
                            "longitude": poi.get("longitude"),
                            "latitude": poi.get("latitude"),
                        }
                    )
            return evidence

        proposal_trace = []
        proposal_candidates = []
        for row in later_proposal_rows:
            brief = json_object(row["brief_json"])
            proposal_snapshot = json_object(row["snapshot_json"])
            verifier = json_object(row["verifier_json"])
            score = json_object(row["score_json"])
            evidence = json_object(row["evidence_json"])
            lineage = json_object(row["generation_lineage_json"])
            grounded = []
            grounded_evidence = evidence.get("grounded") or []
            for item in grounded_evidence:
                if not isinstance(item, dict):
                    continue
                grounded.append(
                    {
                        "briefId": item.get("briefId"),
                        "poolId": item.get("poolId"),
                        "planningSlotId": item.get("planningSlotId"),
                        "dayNumber": item.get("dayNumber"),
                        "sourceGoalId": item.get("sourceGoalId") or item.get("goalId"),
                        "amapId": item.get("amapId") or item.get("id"),
                        "source": item.get("source"),
                        "longitude": item.get("longitude"),
                        "latitude": item.get("latitude"),
                    }
                )
            proposal_trace.append(
                {
                    "proposalId": row["id"],
                    "choiceId": row["choice_id"],
                    "status": row["status"],
                    "briefId": brief.get("briefId"),
                    "canonicalSignature": row["canonical_signature"],
                    "score": {
                        "hardConstraintPassed": score.get("hardConstraintPassed"),
                        "routeEfficiency": score.get("routeEfficiency"),
                        "preferenceFit": score.get("preferenceFit"),
                        "pacingQuality": score.get("pacingQuality"),
                        "thematicCoherence": score.get("thematicCoherence"),
                        "experienceDiversity": score.get("experienceDiversity"),
                        "novelty": score.get("novelty"),
                        "robustness": score.get("robustness"),
                        "uncertaintyPenalty": score.get("uncertaintyPenalty"),
                        "estimatedCostCny": score.get("estimatedCostCny"),
                        "evidence": score.get("evidence"),
                    },
                    "verifier": {
                        "passed": verifier.get("passed"),
                        "requiredGoalCoverage": verifier.get("requiredGoalCoverage"),
                        "dayAnchorTargets": verifier.get("dayAnchorTargets"),
                        "dayAnchorActuals": verifier.get("dayAnchorActuals"),
                        "requiredCandidateBindingExpectedCount": verifier.get("requiredCandidateBindingExpectedCount"),
                        "requiredCandidateBindingActualCount": verifier.get("requiredCandidateBindingActualCount"),
                        "requiredCandidateLineageCoverage": verifier.get("requiredCandidateLineageCoverage"),
                        "routeCoverageFailures": verifier.get("routeCoverageFailures"),
                        "routeQualityFailures": verifier.get("routeQualityFailures"),
                        "hardFailures": verifier.get("hardFailures"),
                    },
                    "lineage": {
                        "briefPlanningProjection": lineage.get("briefPlanningProjection"),
                        "paretoSelection": lineage.get("paretoSelection"),
                        "optimizer": lineage.get("optimizer"),
                        "assignmentIndex": lineage.get("assignmentIndex"),
                        "requiredCandidateBindings": lineage.get("portfolioRequiredCandidateBindings"),
                        "repair": lineage.get("repair"),
                    },
                    "groundedAnchors": grounded,
                }
            )
            proposal_candidates.append(
                PlanCandidate.model_validate(
                    {
                        "proposalId": row["id"],
                        "portfolioId": row["portfolio_id"],
                        "brief": brief,
                        "itinerarySnapshot": proposal_snapshot,
                        "groundedEvidence": grounded_evidence,
                        "score": score,
                        "verifier": verifier,
                        "canonicalSignature": row["canonical_signature"],
                        "generationLineage": lineage,
                    }
                )
            )

        pairwise_distance = ParetoPortfolioSelector().min_pairwise_distance(proposal_candidates)
        physical_sets = [
            {
                str(anchor.get("amapId") or "")
                for anchor in plan.get("groundedAnchors") or []
                if str(anchor.get("amapId") or "")
            }
            for plan in [
                {"groundedAnchors": scoped_anchor_evidence(snapshot_for(adopted["version"]["id"]))},
                *proposal_trace,
            ]
        ]
        physical_pairwise_distances = []
        physical_directional_new_fractions = []
        for left_index, left_ids in enumerate(physical_sets):
            for right_ids in physical_sets[left_index + 1 :]:
                union = left_ids | right_ids
                physical_pairwise_distances.append(
                    round(1.0 - len(left_ids & right_ids) / len(union), 4) if union else 0.0
                )
                physical_directional_new_fractions.append(
                    round(len(right_ids - left_ids) / len(right_ids), 4) if right_ids else 0.0
                )
        min_physical_distance = min(physical_pairwise_distances, default=1.0)
        min_physical_directional_new_fraction = min(
            physical_directional_new_fractions,
            default=1.0,
        )

        with open_db() as connection:
            execution_rows = connection.execute(
                """SELECT source_turn_id, choice_id, action, status,
                    expected_base_version_id, result_version_id, attempt,
                    continuation_json, outcome_json
                FROM agent_choice_executions
                WHERE session_id = ?
                ORDER BY created_at, id""",
                (session["sessionId"],),
            ).fetchall()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()[0]
            patch_count = connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()[0]
            route_count = connection.execute(
                "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
                (session["activePlanId"],),
            ).fetchone()[0]

        choice_execution_trace = []
        scope_keys = (
            "planningSelectionRootTurnId",
            "rootPortfolioId",
            "requestContractFingerprint",
            "expectedBaseVersionId",
            "focusBriefId",
            "briefId",
            "poolId",
            "planningSlotId",
            "dayNumber",
            "expansionFocusMode",
            "resolvedFocusBriefId",
        )
        outcome_keys = (
            "reasonCode",
            "versionDelta",
            "patchDelta",
            "routeWriteDelta",
            "selectedProposalId",
            "duplicate",
        )
        for row in execution_rows:
            continuation = json_object(row["continuation_json"])
            outcome = json_object(row["outcome_json"])
            choice_execution_trace.append(
                {
                    "sourceAssistantTurnId": row["source_turn_id"],
                    "choiceId": row["choice_id"],
                    "action": row["action"],
                    "status": row["status"],
                    "expectedBaseVersionId": row["expected_base_version_id"],
                    "resultVersionId": row["result_version_id"],
                    "attempt": row["attempt"],
                    "scope": {key: continuation.get(key) for key in scope_keys if continuation.get(key) is not None},
                    "outcome": {key: outcome.get(key) for key in outcome_keys if outcome.get(key) is not None},
                }
            )

        phase_trace = []
        for line in all_stream_lines:
            if line.get("event") != "execution_event":
                continue
            data = line.get("data") if isinstance(line.get("data"), dict) else {}
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            phase_trace.append(
                {
                    key: value
                    for key, value in {
                        "captureOrder": len(phase_trace),
                        "type": data.get("type"),
                        "phase": data.get("phase") or metadata.get("phase"),
                        "status": data.get("status"),
                        "sequence": data.get("sequence"),
                        "timestamp": data.get("timestamp") or data.get("createdAt"),
                        "durationMs": data.get("durationMs") or metadata.get("durationMs"),
                        "focusBriefId": data.get("focusBriefId") or metadata.get("focusBriefId"),
                        "briefId": data.get("briefId") or metadata.get("briefId"),
                        "poolId": data.get("poolId") or metadata.get("poolId"),
                        "planningSlotId": data.get("planningSlotId") or metadata.get("planningSlotId"),
                        "dayNumber": data.get("dayNumber") or metadata.get("dayNumber"),
                        "reasonCode": data.get("reasonCode") or metadata.get("reasonCode"),
                    }.items()
                    if value is not None
                }
            )

        partial_snapshot = snapshot_for(adopted["version"]["id"])
        partial_grounded_anchors = scoped_anchor_evidence(partial_snapshot)
        assert partial_grounded_anchors
        assert all(
            anchor.get("briefId")
            and anchor.get("poolId")
            and anchor.get("planningSlotId")
            and anchor.get("dayNumber")
            and anchor.get("sourceGoalId")
            for anchor in partial_grounded_anchors
        )
        assert [event.get("captureOrder") for event in phase_trace] == list(range(len(phase_trace)))
        representative_trace = {
            "schemaVersion": "trip-rule-safe-comparison-trace-v1",
            "scope": {
                "sessionId": session["sessionId"],
                "planningSelectionRootTurnId": adopted_request.get("planningSelectionRootTurnId"),
                "rootPortfolioId": root_portfolio_id,
                "requestContractFingerprint": adopted_request.get("requestContractFingerprint"),
                "activeVersionId": loaded.get("activeVersionId"),
            },
            "briefGenerationState": {
                "orderedBriefIds": final_ordered_brief_ids,
                "completedBriefIds": final_completed_brief_ids,
                "remainingBriefIds": final_remaining_brief_ids,
                "failedBriefIds": final_failed_brief_ids,
            },
            "plans": [
                {
                    "kind": "safe_partial",
                    "versionId": adopted["version"]["id"],
                    "briefId": initial_partial_brief_id,
                    "pendingSlots": [
                        {
                            key: slot.get(key)
                            for key in (
                                "briefId",
                                "poolId",
                                "planningSlotId",
                                "dayNumber",
                                "sourceGoalId",
                                "timeWindow",
                            )
                            if slot.get(key) is not None
                        }
                        for slot in projections[0].get("pendingSlots") or []
                    ],
                    "groundedAnchors": partial_grounded_anchors,
                },
                *[{"kind": "verified_proposal", **item} for item in proposal_trace],
            ],
            "pairwiseEvidence": {
                "proposalIds": [item["proposalId"] for item in proposal_trace],
                "canonicalSignatures": [item["canonicalSignature"] for item in proposal_trace],
                "minPairwiseStructuralDistance": pairwise_distance,
                "minPairwisePhysicalPoiJaccardDistance": min_physical_distance,
                "minDirectionalNewPoiFraction": min_physical_directional_new_fraction,
                "uncommittedProposalCount": sum(item["status"] != "committed" for item in proposal_trace),
                "uncommittedProposalWriteDelta": [0, 0, 0],
            },
            "expansions": expansion_trace_records,
            "slotChoices": [
                *slot_choice_trace_records,
                {
                    "sourceAssistantTurnId": last_candidate_source,
                    "choiceId": last_candidate_id,
                    "resultVersionId": duplicate["version"]["id"],
                    "duplicate": True,
                    "writeDelta": list(duplicate_write_delta),
                },
            ],
            "choiceExecutions": choice_execution_trace,
            "phaseEvents": phase_trace,
            "writes": {
                "safePartial": list(safe_partial_write_delta),
                "preview": list(preview_write_delta),
                "adoption": list(adoption_write_delta),
                "expansions": [list(item) for item in expansion_write_deltas],
                "exactSlotChoices": [list(item) for item in choice_write_deltas],
                "duplicate": list(duplicate_write_delta),
                "finalCounts": {
                    "versions": version_count,
                    "patches": patch_count,
                    "routes": route_count,
                },
            },
            "providerCalls": {
                "controllerExpansionDelta": controller_calls - controller_calls_before_expansion,
                "initialPlanExpansionDelta": initial_plan_calls - initial_plan_calls_before_expansion,
                "creativePortfolioProviderExpansionDelta": portfolio_provider_calls
                - portfolio_provider_calls_before_expansion,
                "deterministicPortfolioFallbackExpansionDelta": portfolio_fallback_calls
                - portfolio_fallback_calls_before_expansion,
            },
        }

    metrics = {
        "initialProviderTimeoutObserved": bool(timeout_failures),
        "timeoutFailureCount": len(timeout_failures),
        "firstStreamEventSequenceValid": first_event_names[-1] == "message_response"
        and first_event_names.count("message_response") == 1
        and first_event_names.count("user_turn") == 1
        and all(name in {"execution_event", "user_turn"} for name in first_event_names[:-1])
        and streamed_user_turn["id"] == first["userTurn"]["id"],
        "persistedSafeChoiceCount": len(persisted_safe_choices),
        "safePartialCreated": second.get("version") is not None,
        "timelineCreated": loaded.get("activeVersionId") == committed_versions[-1],
        "comparisonProjectionCount": len([*projections, *later_projections]),
        "comparisonPlanCount": len(comparison_proposal_ids),
        "distinctCompletedBriefCount": len(set(final_completed_brief_ids)),
        "distinctVisibleProposalBriefCount": len(visible_brief_ids),
        "remainingBriefCount": len(final_remaining_brief_ids),
        "failedBriefCount": len(final_failed_brief_ids),
        "firstVisiblePlanBlockedByLaterPlans": first_visible_plan_blocked,
        "laterVerifiedPlanAppended": later_verified_plan_appended,
        "laterVerifiedProposalCount": len(later_proposal_rows),
        "portfolioPendingSlotCountSequence": pending_slot_count_sequence,
        "safePartialWriteDelta": list(safe_partial_write_delta),
        "previewWriteDelta": list(preview_write_delta),
        "adoptionWriteDelta": list(adoption_write_delta),
        "expansionWriteDelta": list(expansion_write_delta),
        "expansionWriteDeltas": [list(item) for item in expansion_write_deltas],
        "expansionFocusBriefIds": expansion_focus_brief_ids,
        "persistedExpansionChoiceCount": len(expansion_focus_brief_ids),
        "checkpointExpansionResumeCount": checkpoint_expansion_resume_count,
        "expansionControllerCallDelta": controller_calls - controller_calls_before_expansion,
        "expansionInitialPlanProviderCallDelta": initial_plan_calls - initial_plan_calls_before_expansion,
        "expansionCreativePortfolioProviderCallDelta": (
            portfolio_provider_calls - portfolio_provider_calls_before_expansion
        ),
        "expansionDeterministicPortfolioFallbackCount": (
            portfolio_fallback_calls - portfolio_fallback_calls_before_expansion
        ),
        "globalPortfolioRebuildCount": sum(1 for batch in exact_focus_stage_batches if len(batch) != 1),
        "briefReissueCount": len(expansion_focus_brief_ids) - len(set(expansion_focus_brief_ids)),
        "briefScopeDriftCount": brief_scope_drift_count,
        "rootPortfolioDriftCount": root_portfolio_drift_count,
        "canonicalDuplicateExpansionCount": canonical_duplicate_expansion_count,
        "exactChoiceWriteDeltas": [list(item) for item in choice_write_deltas],
        "duplicateChoiceWriteDelta": list(duplicate_write_delta),
        "fakeOrNonAmapAnchorCount": invalid_persisted_anchor_count,
        "comparisonScopeDriftCount": comparison_scope_drift_count,
        "comparisonPortfolioCount": comparison_portfolio_count,
        "pendingSlotScopeDriftCount": pending_slot_scope_drift_count,
        "activeTimelineProjectionMismatchCount": active_timeline_projection_mismatch_count,
        "rawInternalErrorCount": raw_internal_error_count,
    }
    assert all(
        metrics[key]
        for key in (
            "initialProviderTimeoutObserved",
            "safePartialCreated",
            "timelineCreated",
            "laterVerifiedPlanAppended",
        )
    )
    print("TRIP_RULE_SAFE_COMPARISON_STABILITY_METRICS=" + json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    if representative_trace is not None:
        print(
            "TRIP_RULE_SAFE_COMPARISON_REPRESENTATIVE_TRACE="
            + json.dumps(representative_trace, ensure_ascii=False, sort_keys=True)
        )


def test_current_agent_session_recovers_turns_itinerary_version_and_preference_memory(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        generated = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        current = client.get("/api/agent/sessions/current")

    assert current.status_code == 200
    body = current.json()
    assert body["sessionId"] == session["sessionId"]
    assert body["activeVersionId"] == generated["version"]["id"]
    assert body["itinerary"]["title"] == generated["itinerary"]["title"]
    assert len(body["turns"]) == 2
    assert body["turns"][1]["planningSteps"]
    assert body["turns"][1]["toolEvents"] == []
    assert any(step["type"] == "agent_decision" for step in body["turns"][1]["planningSteps"])
    assert body["preferenceMemory"]["memoryText"].startswith("# 我的旅行偏好")
    assert body["planningRun"]["id"] == generated["planningRun"]["id"]
    assert body["planningRun"]["itineraryVersionId"] == generated["version"]["id"]
    assert body["planningRun"]["feasibilityReport"]["score"] <= 100


def test_current_agent_session_does_not_attach_mismatched_planning_run(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        generated = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        with open_db() as connection:
            connection.execute(
                "UPDATE planning_runs SET itinerary_version_id = ? WHERE id = ?",
                ("ver_other_active_state", generated["planningRun"]["id"]),
            )
            connection.commit()
        current = client.get("/api/agent/sessions/current")

    assert current.status_code == 200
    body = current.json()
    assert body["activeVersionId"] == generated["version"]["id"]
    assert body["itinerary"]["title"] == generated["itinerary"]["title"]
    assert body["planningRun"] is None


def test_current_agent_session_normalizes_legacy_planning_events():
    clear_database()
    created_at = "2026-06-10T00:00:00Z"
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status,
                    parent_turn_id, itinerary_version_id, agent_request_json,
                    agent_response_json, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "turn_legacy_event",
                    session["sessionId"],
                    "assistant",
                    "缺少候选语义提示，等待补充。",
                    1,
                    "failed",
                    None,
                    None,
                    None,
                    json.dumps(
                        {
                            "mode": "semantic_candidate_hint_missing",
                            "planningSteps": [
                                {
                                    "label": "生成候选",
                                    "status": "failed",
                                    "metadata": {"resultState": "semantic_candidate_hint_missing"},
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    None,
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
        response = client.get("/api/agent/sessions/current")

    assert response.status_code == 200
    turn = response.json()["turns"][0]
    assert turn["planningSteps"][0]["type"] == "legacy_event"
    assert turn["planningSteps"][0]["timestamp"] == created_at
    assert turn["planningSteps"][0]["label"] == "生成候选"


def test_http_session_and_runtime_inspect_read_same_core_state(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": "帮我安排北京一天，10月1日出发，轻松一点"},
        )
        http_session = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    with open_db() as connection:
        runtime_state = TripAgentRuntime(connection).inspect_session(session["sessionId"])

    assert runtime_state["readOnly"] is True
    assert runtime_state["activePlanId"] == http_session["activePlanId"]
    assert runtime_state["activeVersionId"] == http_session["activeVersionId"]
    assert [turn["id"] for turn in runtime_state["turns"]] == [turn["id"] for turn in http_session["turns"]]
    assert runtime_state["pendingPoiCandidates"] == http_session["pendingPoiCandidates"]


def test_agent_message_api_clarifies_vague_initial_request_without_provider(monkeypatch):
    clear_database()
    provider = CountingProvider(full_itinerary_output())
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": "想出去玩"},
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}")

    assert response.status_code == 200
    body = response.json()
    assert provider.calls == 0
    assert body["version"] is None
    assert body["itinerary"] is None
    assert "当前没有修改行程，请重试本轮" in body["assistantTurn"]["content"]
    assert body["terminalStatus"] == "needs_confirmation"
    assert [item["action"] for item in body["assistantTurn"]["choiceOptions"]] == ["retry_model_planning"]
    assert len(re.findall(r"(?m)^\d+\. ", body["assistantTurn"]["content"])) == 0
    assert "2 天标准版" not in body["assistantTurn"]["content"]
    assert body["planningRun"]["runType"] == "agent_clarification"
    assert body["planningRun"]["understoodRequirements"]["isCompleteEnoughToPlan"] is False
    assert "travelDate" in body["planningRun"]["understoodRequirements"]["missingFields"]
    assert loaded.json()["activeVersionId"] is None


def test_agent_session_reload_projects_dynamic_clarification_checkpoint():
    clear_database()
    checkpoint = {
        "schemaVersion": "clarification-checkpoint-v1",
        "checkpointId": "clarify_reload",
        "planningSelectionRootTurnId": "turn_reload_user",
        "requestFingerprint": "a" * 64,
        "fingerprint": "b" * 64,
        "status": "awaiting_answer",
        "resolvedAnswers": [
            {
                "dimensionId": "night_view.frequency",
                "semanticValue": {"frequency": "every_available_evening"},
            }
        ],
        "question": {
            "dimensionId": "night_view.experience_family",
            "question": "第二晚更偏向哪类公共夜景体验？",
            "whyItMatters": "用于约束下一轮地点发现与路线矩阵。",
            "allowFreeText": True,
            "options": [],
        },
    }
    specs = [
        {
            "intentType": "night_view",
            "frequency": "every_available_evening",
            "experienceFamilies": ["public_city_view"],
        }
    ]
    gap = {
        "status": "candidate_refresh_required",
        "missingRequiredCandidateCount": 2,
        "rejectedCandidateCount": 7,
    }
    with TestClient(app) as client:
        session = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "澄清刷新"},
        ).json()
        now = datetime.now(timezone.utc).isoformat()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status,
                    agent_response_json, created_at, updated_at
                ) VALUES (?, ?, 'assistant', ?, 1, 'active', ?, ?, ?)
                """,
                (
                    "turn_reload_assistant",
                    session["sessionId"],
                    "请继续回答当前问题。",
                    json.dumps(
                        {
                            "clarificationCheckpoint": checkpoint,
                            "experienceSpecs": specs,
                            "candidateGapSummary": gap,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
            connection.commit()
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}")

    assert loaded.status_code == 200
    turn = loaded.json()["turns"][0]
    assert turn["clarificationCheckpoint"] == checkpoint
    assert turn["experienceSpecs"] == specs
    assert turn["candidateGapSummary"] == gap
    assert loaded.json()["activeVersionId"] is None


def test_current_agent_session_prefers_latest_turn_even_without_version(monkeypatch):
    clear_database()
    provider = CountingProvider(full_itinerary_output())
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)

    with TestClient(app) as client:
        first = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        second = client.post("/api/agent/sessions", json={"city": "上海", "title": "上海会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{first['sessionId']}/messages",
            json={"content": "想出去玩"},
        )
        current = client.get("/api/agent/sessions/current")

    assert response.status_code == 200
    assert response.json()["version"] is None
    assert provider.calls == 0
    assert current.status_code == 200
    assert current.json()["sessionId"] == first["sessionId"]
    assert current.json()["sessionId"] != second["sessionId"]
    assert len(current.json()["turns"]) == 2


def test_empty_preference_memory_is_filtered_before_agent_prompt(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = RecordingProvider([full_itinerary_output()])
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        )

    assert response.status_code == 200
    context = provider.contexts[0]
    assert context["memoryText"] == ""
    assert context["currentPreferenceSummary"] == ""
    assert "暂无明确记录" not in json.dumps(context, ensure_ascii=False)
    assert "# 我的旅行偏好" not in json.dumps(context, ensure_ascii=False)


def test_actual_preference_memory_only_sends_meaningful_content(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = RecordingProvider([full_itinerary_output()])
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        client.patch(
            f"/api/preferences/memory?sessionId={session['sessionId']}",
            json={
                "memoryText": "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n- 喜欢轻松不赶路。\n\n## 交通偏好\n- 公共交通优先。\n",
            },
        )
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        )

    assert response.status_code == 200
    context = provider.contexts[0]
    assert context["memoryText"] == "旅行节奏：喜欢轻松不赶路。\n交通偏好：公共交通优先。"
    assert context["currentPreferenceSummary"] == context["memoryText"]
    assert "暂无明确记录" not in context["memoryText"]
    assert "# 我的旅行偏好" not in context["memoryText"]


def test_agent_message_context_includes_curated_skill_context(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = RecordingProvider([full_itinerary_output()])
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={
                "content": route_ready_request(
                    "帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园，并用高德 POI 校验"
                ),
                "context": {
                    "selectedDayNumber": 1,
                    "candidateMapPois": [{"amapId": "B000PALACE", "name": "故宫博物院"}],
                },
            },
        )

    assert response.status_code == 200
    context = provider.contexts[0]
    assert 1 <= len(context["selectedSkills"]) <= 2
    assert context["skillContext"]
    assert "AMap POI Grounding" in context["skillContext"]
    assert any(skill["name"] == "amap_poi_grounding" for skill in context["selectedSkills"])
    assert context["agentPlan"]["plannerVersion"] == "model-decision-v2"
    assert {"policy_gate", "versioned_write_guard", "verifier"}.issubset(context["agentPlan"]["riskControls"])
    assert "memoryText" in context
    assert "travelPreferenceMemory" in context


def test_local_replan_suggestion_endpoint_returns_visible_planning_run(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        generated = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        response = client.post(
            f"/api/itineraries/{generated['itinerary']['id']}/local-replan/suggestions",
            json={"userInput": "帮我降低密度", "preferenceSummary": "用户偏好轻松不赶路"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["runType"] == "local_replan_suggestion"
    assert body["toolCalls"]
    assert body["feasibilityReport"]["preferenceAlignment"]
    assert body["finalSummary"] == "已生成局部优化建议，等待用户确认后再应用。"


def test_agent_full_itinerary_rate_limit_stays_retryable_without_placeholder_write(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )

    def rate_limited_fetch(_service, _params):
        raise Exception("CUQPS_HAS_EXCEEDED_THE_LIMIT")

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", rate_limited_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert response.status_code == 200
    body = response.json()
    with open_db() as connection:
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session["sessionId"],)
        ).fetchone()[0]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session["sessionId"],)
        ).fetchone()[0]

    assert body["version"] is None
    assert body["itinerary"] is None
    assert body["pendingPoiCandidates"] == []
    assert body["assistantTurn"]["status"] == "active"
    assert body["terminalStatus"] == "needs_confirmation"
    create_step = next(step for step in body["planningSteps"] if step["type"] == "create_itinerary_version")
    result_preview = create_step["metadata"]["resultPreview"]
    assert result_preview["versionCreated"] is False
    assert "retry_after_map_provider_recovers" in result_preview["nextActions"]
    assert loaded["activeVersionId"] is None
    assert all(not day["segments"] for day in loaded["itinerary"]["days"])
    assert version_count == 0
    assert patch_count == 0


def test_agent_full_itinerary_unresolved_text_pois_fail_closed_without_timeline_write(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )

    def ambiguous_fetch(_service, _params):
        return {
            "status": "1",
            "pois": [
                amap_poi("B000ALT1", "东城文化公园", "东城区", "116.41,39.91"),
                amap_poi("B000ALT2", "西城城市广场", "西城区", "116.38,39.92"),
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", ambiguous_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    with open_db() as connection:
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM amap_poi_candidates WHERE status = 'pending'"
        ).fetchone()[0]
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        patch_count = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]
        route_count = connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0]

    assert response.status_code == 200
    body = response.json()
    assert body["version"] is None
    assert body["itinerary"] is None
    assert body["pendingPoiCandidates"] == []
    assert pending_count == 0
    assert body["assistantTurn"]["status"] == "active", {
        "terminalStatus": body.get("terminalStatus"),
        "failureReason": body["assistantTurn"].get("failureReason"),
        "warnings": body.get("warnings"),
        "planningRun": body.get("planningRun"),
    }
    assert body["assistantTurn"]["planningRunId"] == body["planningRun"]["id"]
    assert body["terminalStatus"] == "candidate_refresh_required", {
        "failureReason": body["assistantTurn"].get("failureReason"),
        "warnings": body.get("warnings"),
        "planningRun": body.get("planningRun"),
    }
    assert loaded["turns"][-1]["planningRunId"] == body["planningRun"]["id"]
    assert loaded["activeVersionId"] is None
    assert loaded["pendingPoiCandidates"] == []
    assert all(not day["segments"] for day in loaded["itinerary"]["days"])
    assert (version_count, patch_count, route_count) == (0, 0, 0)


def test_agent_message_api_accepts_exact_amap_poi_among_unrelated_candidates(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(single_palace_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，只安排故宫博物院，不要添加其他地点")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] is not None
    assert body["pendingPoiCandidates"] == []
    palace = body["itinerary"]["days"][0]["segments"][0]["poi"]
    assert palace["name"] == "故宫博物院"
    assert palace["amapId"] == "B000PALACE"


def test_agent_message_api_applies_patch_modification(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    outputs = [full_itinerary_output(), patch_output()]
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: FakeProvider(outputs.pop(0)))
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        )
        second = client.post(f"/api/agent/sessions/{session['sessionId']}/messages", json={"content": "标题改轻松"})

    assert first.status_code == 200
    assert second.status_code == 200
    body = second.json()
    assert body["itinerary"]["title"] == "北京轻松慢游"
    assert body["version"]["versionNumber"] == first.json()["version"]["versionNumber"] + 1
    assert body["assistantTurn"]["itineraryVersionId"] == body["version"]["id"]


def test_edit_user_message_rolls_back_supersedes_later_turns_and_regenerates(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = RecordingProvider(
        [
            full_itinerary_output(),
            patch_output("北京第二版"),
            patch_output("北京第三版"),
            patch_output("北京重新分支"),
        ]
    )
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        second = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages", json={"content": "改成第二版"}
        ).json()
        third = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages", json={"content": "改成第三版"}
        ).json()
        edited = client.patch(
            f"/api/agent/sessions/{session['sessionId']}/messages/{first['userTurn']['id']}",
            json={
                "content": "把2026年10月1日北京一日游的标题改成北京重新分支，保留故宫博物院和景山公园",
                "regenerate": True,
            },
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert edited.status_code == 200
    body = edited.json()
    assert body["restoredVersionId"] == first["version"]["id"]
    assert body["version"]["versionNumber"] > third["version"]["versionNumber"]
    assert body["itinerary"]["title"] == "北京重新分支"
    assert body["editedTurn"]["id"] != first["userTurn"]["id"]
    assert body["editedTurn"]["parentTurnId"] == first["userTurn"]["id"]
    assert set(body["supersededTurnIds"]) == {
        first["userTurn"]["id"],
        first["assistantTurn"]["id"],
        second["userTurn"]["id"],
        second["assistantTurn"]["id"],
        third["userTurn"]["id"],
        third["assistantTurn"]["id"],
    }
    assert loaded["activeVersionId"] == body["version"]["id"]
    statuses = {turn["id"]: turn["status"] for turn in loaded["turns"]}
    assert statuses[first["userTurn"]["id"]] == "superseded"
    assert statuses[body["editedTurn"]["id"]] == "active"
    assert statuses[first["assistantTurn"]["id"]] == "superseded"
    assert body["assistantTurn"]["status"] == "active"
    assert body["assistantTurn"]["planningRunId"] == body["planningRun"]["id"]
    assert loaded["turns"][-1]["planningRunId"] == body["planningRun"]["id"]
    latest_context = provider.contexts[-1]
    assert [turn["content"] for turn in latest_context["activeConversationTurns"]] == [
        "把2026年10月1日北京一日游的标题改成北京重新分支，保留故宫博物院和景山公园"
    ]


def test_edit_user_message_regenerate_false_restores_without_provider_call(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = RecordingProvider([full_itinerary_output(), patch_output("北京第二版")])
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        second = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages", json={"content": "改成第二版"}
        ).json()
        edited = client.patch(
            f"/api/agent/sessions/{session['sessionId']}/messages/{first['userTurn']['id']}",
            json={"content": "只回滚不重新生成", "regenerate": False},
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert edited.status_code == 200
    body = edited.json()
    assert len(provider.contexts) == 2
    assert body["assistantTurn"] is None
    assert body["version"]["id"] == first["version"]["id"]
    assert body["itinerary"]["title"] == first["itinerary"]["title"]
    assert body["supersededTurnIds"] == [
        first["userTurn"]["id"],
        first["assistantTurn"]["id"],
        second["userTurn"]["id"],
        second["assistantTurn"]["id"],
    ]
    assert body["editedTurn"]["id"] != first["userTurn"]["id"]
    assert body["editedTurn"]["parentTurnId"] == first["userTurn"]["id"]
    assert loaded["activeVersionId"] == first["version"]["id"]
    assert loaded["itinerary"]["title"] == first["itinerary"]["title"]


def test_edit_non_user_turn_is_rejected(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages", json={"content": "生成10月1日一天"}
        ).json()
        edited = client.patch(
            f"/api/agent/sessions/{session['sessionId']}/messages/{first['assistantTurn']['id']}",
            json={"content": "试图编辑 assistant", "regenerate": False},
        )

    assert edited.status_code == 400
    assert edited.json()["detail"] == "Only user messages can be edited"


def test_edit_user_message_without_version_restores_empty_state_without_error():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status,
                    parent_turn_id, itinerary_version_id, agent_request_json,
                    agent_response_json, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "turn_without_version",
                    session["sessionId"],
                    "user",
                    "尚未生成版本",
                    1,
                    "active",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "2026-06-10T00:00:00Z",
                    "2026-06-10T00:00:00Z",
                ),
            )
            connection.commit()
        edited = client.patch(
            f"/api/agent/sessions/{session['sessionId']}/messages/turn_without_version",
            json={"content": "无法回滚", "regenerate": False},
        )

    assert edited.status_code == 200
    body = edited.json()
    assert body["restoredVersionId"] is None
    assert body["version"] is None
    assert body["itinerary"] is None
    assert body["assistantTurn"] is None
    assert body["editedTurn"]["id"] != "turn_without_version"
    assert body["editedTurn"]["parentTurnId"] == "turn_without_version"
    assert "turn_without_version" in body["supersededTurnIds"]
    assert {"plan", "resolve_poi", "apply_patch", "verify", "respond"}.issubset(
        {step["type"] for step in body["planningSteps"]}
    )
    assert body["planningSteps"][0]["turnId"] == body["editedTurn"]["id"]


def test_edit_user_message_respects_session_run_lease():
    clear_database()
    with TestClient(app) as client:
        session = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "北京会话"},
        ).json()
        with open_db() as connection:
            service = AgentService(connection, provider=SimpleNamespace())
            turn_id = service._insert_turn(
                session["sessionId"],
                "user",
                "尚未生成版本",
                "active",
            )
            connection.commit()
        assert acquire_session_run(session["sessionId"]) is True
        try:
            edited = client.patch(
                f"/api/agent/sessions/{session['sessionId']}/messages/{turn_id}",
                json={"content": "编辑后重跑", "regenerate": False},
            )
        finally:
            release_session_run(session["sessionId"])

    assert edited.status_code == 409
    assert edited.json()["detail"]["code"] == "run_in_progress"


def test_edit_regeneration_uses_new_source_user_turn_and_preserves_old_portfolio_audit():
    clear_database()
    now = datetime.now(timezone.utc).isoformat()
    with TestClient(app) as client:
        session = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "北京会话"},
        ).json()
        with open_db() as connection:
            connection.execute(
                """INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status,
                    parent_turn_id, itinerary_version_id, agent_request_json,
                    agent_response_json, error_json, created_at, updated_at
                ) VALUES (?, ?, 'user', ?, 1, 'active', NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                ("turn_original_root", session["sessionId"], "原始北京两日游", now, now),
            )
            connection.execute(
                """INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status,
                    parent_turn_id, itinerary_version_id, agent_request_json,
                    agent_response_json, error_json, created_at, updated_at
                ) VALUES (?, ?, 'assistant', ?, 2, 'active', NULL, NULL, NULL, ?, NULL, ?, ?)""",
                (
                    "turn_original_assistant",
                    session["sessionId"],
                    "没有方案",
                    json.dumps({"mode": "creative_plan_portfolio"}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO agent_plan_portfolios (
                    id, session_id, source_user_turn_id, source_assistant_turn_id,
                    expected_base_version_id, source_observation_fingerprint,
                    request_contract_fingerprint, status, selected_proposal_id,
                    dominant_proposal_id, summary_json, failure_reason, expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'failed', NULL, NULL, '{}', ?, NULL, ?, ?)""",
                (
                    "portfolio_original",
                    session["sessionId"],
                    "turn_original_root",
                    "turn_original_assistant",
                    "o" * 64,
                    "r" * 64,
                    "portfolio_anchor_target_shortfall:day_1:0/1",
                    now,
                    now,
                ),
            )
            connection.commit()

        edited = client.patch(
            f"/api/agent/sessions/{session['sessionId']}/messages/turn_original_root",
            json={"content": "编辑后的北京三日游", "regenerate": False},
        )

        assert edited.status_code == 200
        body = edited.json()
        with open_db() as connection:
            original = connection.execute(
                "SELECT content, status FROM conversation_turns WHERE id = 'turn_original_root'"
            ).fetchone()
            old_portfolio = connection.execute(
                "SELECT source_user_turn_id FROM agent_plan_portfolios WHERE id = 'portfolio_original'"
            ).fetchone()
            connection.execute(
                """INSERT INTO agent_plan_portfolios (
                    id, session_id, source_user_turn_id, source_assistant_turn_id,
                    expected_base_version_id, source_observation_fingerprint,
                    request_contract_fingerprint, status, selected_proposal_id,
                    dominant_proposal_id, summary_json, failure_reason, expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, 'failed', NULL, NULL, '{}', ?, NULL, ?, ?)""",
                (
                    "portfolio_regenerated",
                    session["sessionId"],
                    body["editedTurn"]["id"],
                    "n" * 64,
                    "f" * 64,
                    "test_regenerated_branch",
                    now,
                    now,
                ),
            )
            connection.commit()

    assert original["content"] == "原始北京两日游"
    assert original["status"] == "superseded"
    assert old_portfolio["source_user_turn_id"] == "turn_original_root"
    assert body["editedTurn"]["id"] != "turn_original_root"
    assert body["editedTurn"]["parentTurnId"] == "turn_original_root"


def test_invalid_agent_output_does_not_update_active_version(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    outputs = [full_itinerary_output(), "not-json"]
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: FakeProvider(outputs.pop(0)))
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        invalid = client.post(f"/api/agent/sessions/{session['sessionId']}/messages", json={"content": "输出坏 JSON"})
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert invalid.status_code == 200
    assert invalid.json()["assistantTurn"]["status"] == "failed"
    assert invalid.json()["version"]["id"] == first["version"]["id"]
    assert loaded["activeVersionId"] == first["version"]["id"]


def test_agent_verifier_failure_restores_previous_active_version(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = RecordingProvider([full_itinerary_output(), patch_output("不应生效")])
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    install_recorded_provider_route_matrix(monkeypatch)

    from src.services.agent_verifier_service import AgentVerifierReport

    call_count = {"count": 0}

    def reject_second_write(*_args, **_kwargs):
        call_count["count"] += 1
        if call_count["count"] <= 2:
            return AgentVerifierReport(passed=True, checks=[{"name": "test", "status": "passed"}])
        return AgentVerifierReport(
            passed=False,
            hard_failures=["forced verifier failure"],
            checks=[{"name": "forced", "status": "failed"}],
        )

    monkeypatch.setattr("src.services.agent_service.AgentVerifierService.verify_agent_write", reject_second_write)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        first = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": route_ready_request("帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园")},
        ).json()
        rejected = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": "把标题改成不应生效"},
        )
        loaded = client.get(f"/api/agent/sessions/{session['sessionId']}").json()

    assert rejected.status_code == 200
    body = rejected.json()
    assert body["assistantTurn"]["status"] == "failed"
    assert body["version"]["id"] == first["version"]["id"]
    assert "verifier rejected" in body["warnings"][0]
    verifier_step = next(step for step in body["planningSteps"] if step["type"] == "verify")
    assert verifier_step["status"] == "failed"
    assert "forced verifier failure" in verifier_step["metadata"]["hardFailures"]
    assert loaded["activeVersionId"] == first["version"]["id"]
    assert loaded["itinerary"]["title"] == first["itinerary"]["title"]


class FakeProvider(IntentContractProviderMixin):
    def __init__(self, payload):
        self.payload = payload

    def decide_autonomy(self, context: dict, *, timeout_seconds: float, repair_feedback: str = ""):
        return controller_decision_for_provider(context, self.payload)

    def generate(self, _context: dict) -> str:
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)

    def generate_initial_plan(self, context: dict) -> str:
        return initial_plan_payload(self.payload, context)

    def run_tool_loop(self, context: dict, tool_registry) -> AgentToolLoopResult:
        raw = self.generate(context)
        structured = json.loads(raw)
        tool_registry.execute("contract_read", "read_itinerary", {})
        operations = list(structured.get("operations") or [])
        if structured.get("fullItinerary") is not None:
            operations = [{"op": "replace_itinerary", "fullItinerary": structured["fullItinerary"]}]
        tool_registry.execute(
            "contract_patch",
            "patch_itinerary",
            {"baseVersionId": context.get("activeVersionId"), "operations": operations},
        )
        return AgentToolLoopResult(
            reply=str(structured.get("reply") or "已更新行程。"), tool_events=tool_registry.events
        )


class CountingProvider(FakeProvider):
    def __init__(self, payload):
        super().__init__(payload)
        self.calls = 0

    def generate(self, context: dict) -> str:
        self.calls += 1
        return super().generate(context)


class RecordingProvider(IntentContractProviderMixin):
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.contexts = []

    def generate(self, context: dict) -> str:
        self.contexts.append(context)
        payload = self.payloads.pop(0)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)

    def generate_initial_plan(self, context: dict) -> str:
        self.contexts.append(context)
        payload = self.payloads.pop(0)
        return initial_plan_payload(payload, context)

    def decide_autonomy(self, context: dict, *, timeout_seconds: float, repair_feedback: str = ""):
        payload = self.payloads[0] if self.payloads else None
        return controller_decision_for_provider(context, payload)

    def run_tool_loop(self, context: dict, tool_registry) -> AgentToolLoopResult:
        raw = self.generate(context)
        structured = json.loads(raw)
        tool_registry.execute("contract_read", "read_itinerary", {})
        operations = list(structured.get("operations") or [])
        if structured.get("fullItinerary") is not None:
            operations = [{"op": "replace_itinerary", "fullItinerary": structured["fullItinerary"]}]
        tool_registry.execute(
            "contract_patch",
            "patch_itinerary",
            {"baseVersionId": context.get("activeVersionId"), "operations": operations},
        )
        return AgentToolLoopResult(
            reply=str(structured.get("reply") or "已更新行程。"), tool_events=tool_registry.events
        )


def _goal_requirements_from_context(context: dict) -> list[dict]:
    goal_requirements = context.get("goalRequirements") if isinstance(context.get("goalRequirements"), list) else []
    if goal_requirements:
        return [item for item in goal_requirements if isinstance(item, dict) and str(item.get("goalId") or "").strip()]
    request_contract = context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
    return [
        item
        for item in request_contract.get("requiredIntents") or []
        if isinstance(item, dict) and str(item.get("goalId") or "").strip()
    ]


def _text_for_goal_matching(*parts: object) -> str:
    joined = " ".join(str(part or "") for part in parts if str(part or "").strip())
    return joined.replace("（", "(").replace("）", ")").lower()


def _goal_match_score(goal: dict, text: str) -> int:
    intent_type = str(goal.get("intentType") or "").strip()
    exact_entity = str(goal.get("exactEntity") or "").strip()
    score = 0
    if exact_entity and exact_entity.lower() in text:
        score += 100
    if intent_type == "museum" and any(marker in text for marker in ("博物院", "博物馆", "美术馆", "展览馆", "故宫")):
        score += 20
    if intent_type == "park" and any(marker in text for marker in ("公园", "园林", "景山", "颐和园", "圆明园")):
        score += 20
    if intent_type == "campus_visit" and any(marker in text for marker in ("大学", "学院", "高校", "校园", "校区")):
        score += 20
    if intent_type == "meal" and any(marker in text for marker in ("餐", "饭", "小吃", "美食", "菜")):
        score += 20
    if exact_entity:
        exact_tokens = [token for token in (exact_entity, exact_entity.replace("博物院", ""), exact_entity.replace("公园", "")) if token]
        if any(token.lower() and token.lower() in text for token in exact_tokens):
            score += 10
    return score


def _match_goal_for_payload_item(
    *,
    text: str,
    day_number: int,
    goal_requirements: list[dict],
    used_goal_ids: set[str],
) -> Optional[dict]:
    best_goal = None
    best_key = None
    for goal in goal_requirements:
        goal_id = str(goal.get("goalId") or "").strip()
        if not goal_id or goal_id in used_goal_ids:
            continue
        allowed_days = {
            int(item)
            for item in goal.get("allowedDayNumbers") or []
            if isinstance(item, int) and not isinstance(item, bool) and int(item) > 0
        }
        if allowed_days and day_number not in allowed_days:
            continue
        score = _goal_match_score(goal, text)
        if score <= 0:
            continue
        key = (
            score,
            int(goal.get("requiredMin") or 0),
            len(str(goal.get("exactEntity") or "")),
            -len(goal_id),
        )
        if best_key is None or key > best_key:
            best_goal = goal
            best_key = key
    return best_goal


def _align_initial_plan_payload_to_context(payload: dict, context: dict) -> dict:
    if not isinstance(payload, dict) or str(payload.get("mode") or "") != "day_slots":
        return payload
    goal_requirements = _goal_requirements_from_context(context)
    if not goal_requirements:
        return payload
    aligned = copy.deepcopy(payload)
    slots = [item for item in aligned.get("daySlots") or [] if isinstance(item, dict)]
    pools = [item for item in aligned.get("intentPools") or [] if isinstance(item, dict)]
    slots_by_id = {
        str(item.get("slotId") or "").strip(): item
        for item in slots
        if str(item.get("slotId") or "").strip()
    }
    used_goal_ids: set[str] = set()
    kind_by_intent = {
        "campus_visit": "campus",
        "meal": "meal",
        "museum": "museum",
        "park": "park",
        "night_view": "night_view",
    }
    for pool in pools:
        assigned_slot_ids = [str(item) for item in pool.get("assignToSlots") or [] if str(item)]
        assigned_slots = [slots_by_id[slot_id] for slot_id in assigned_slot_ids if slot_id in slots_by_id]
        day_number = int((assigned_slots[0].get("dayNumber") if assigned_slots else 0) or 0)
        text = _text_for_goal_matching(
            pool.get("rawNeed"),
            *(pool.get("candidateHints") or []),
            *[slot.get("rawNeed") for slot in assigned_slots],
        )
        matched_goal = _match_goal_for_payload_item(
            text=text,
            day_number=day_number,
            goal_requirements=goal_requirements,
            used_goal_ids=used_goal_ids,
        )
        if matched_goal is None:
            continue
        goal_id = str(matched_goal.get("goalId") or "")
        intent_type = str(matched_goal.get("intentType") or "")
        used_goal_ids.add(goal_id)
        pool["goalId"] = goal_id
        pool["softGoalId"] = None
        pool["intentType"] = intent_type or str(pool.get("intentType") or "")
        pool["requirementLevel"] = "required"
        exact_entity = str(matched_goal.get("exactEntity") or "").strip()
        if exact_entity:
            pool["candidateHints"] = list(dict.fromkeys([exact_entity, *(pool.get("candidateHints") or [])]))
        for slot in assigned_slots:
            slot["kind"] = kind_by_intent.get(intent_type, slot.get("kind"))
            if exact_entity:
                slot["rawNeed"] = exact_entity
    return aligned


def _synthetic_required_goals_from_payload(payload) -> tuple[list[str], dict[int, list[str]]]:
    goal_priority: list[str] = []
    required_goals_by_day: dict[int, list[str]] = {}
    if isinstance(payload, dict):
        itinerary = payload.get("fullItinerary")
        if isinstance(itinerary, dict):
            for day in itinerary.get("days") or []:
                day_number = int(day.get("dayNumber") or 1)
                for index, segment in enumerate(day.get("segments") or [], start=1):
                    goal_id = f"goal_day{day_number}_segment{index}"
                    goal_priority.append(goal_id)
                    required_goals_by_day.setdefault(day_number, []).append(goal_id)
            return goal_priority, required_goals_by_day
        day_slots = [item for item in payload.get("daySlots") or [] if isinstance(item, dict)]
        if day_slots:
            for slot in day_slots:
                slot_id = str(slot.get("slotId") or "").strip()
                if not slot_id:
                    continue
                day_number = int(slot.get("dayNumber") or 1)
                goal_id = f"goal_{slot_id}"
                goal_priority.append(goal_id)
                required_goals_by_day.setdefault(day_number, []).append(goal_id)
    return goal_priority, required_goals_by_day


def controller_decision_for_provider(context: dict, payload=None) -> dict:
    lifecycle = context.get("itineraryLifecycle") if isinstance(context.get("itineraryLifecycle"), dict) else {}
    active_version_id = str(lifecycle.get("activeVersionId") or "")
    message = str(context.get("latestUserMessage") or "")
    if int(lifecycle.get("cycleIndex") or 0) > 0:
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "finish",
            "actionDirective": {"type": "finish", "assistantReply": "本轮执行完成。"},
        }
    if not active_version_id and message == "想出去玩":
        question = "我需要先补齐这些信息：目的地、出行日期和同行人数。"
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "type": "ask_user",
                "question": question,
                "choiceIds": ["provide_details", "manual_input"],
            },
        }
    if not active_version_id:
        request_contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        resolved_dates = context.get("resolvedTripDates") if isinstance(context.get("resolvedTripDates"), dict) else {}
        goal_requirements = _goal_requirements_from_context(context)
        if goal_requirements:
            goal_priority = [str(item.get("goalId") or "") for item in goal_requirements if str(item.get("goalId") or "")]
            required_goals_by_day = {}
            for item in goal_requirements:
                goal_id = str(item.get("goalId") or "").strip()
                if not goal_id:
                    continue
                allowed_days = [
                    int(day)
                    for day in item.get("allowedDayNumbers") or []
                    if isinstance(day, int) and not isinstance(day, bool) and int(day) > 0
                ]
                if not allowed_days:
                    allowed_days = [1]
                requested = max(
                    1,
                    int(item.get("requiredMin") or item.get("preferredCount") or item.get("target") or 1),
                )
                for day_number in allowed_days[: max(1, min(requested, len(allowed_days)))]:
                    required_goals_by_day.setdefault(day_number, []).append(goal_id)
        else:
            goal_priority, required_goals_by_day = _synthetic_required_goals_from_payload(payload)
        required_day_numbers = [
            int(item)
            for item in request_contract.get("requiredPlanningDayNumbers") or []
            if int(item) > 0
        ]
        if not required_day_numbers:
            required_day_numbers = [
                index for index, _date in enumerate(resolved_dates.get("dates") or [], start=1)
            ] or sorted(required_goals_by_day) or [1]
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": {
                "type": "draft_itinerary",
                "goalPriority": goal_priority,
                "dayStrategies": [
                    {
                        "dayNumber": day_number,
                        "theme": "按用户请求规划",
                        "requiredGoalIds": required_goals_by_day.get(day_number, []),
                        "requiredGoalCounts": {
                            goal_id: 1 for goal_id in required_goals_by_day.get(day_number, [])
                        },
                        "optionalGoalIds": [],
                        "pace": "standard",
                        "maxRouteAnchors": 4,
                    }
                    for day_number in required_day_numbers
                ],
                "optionalExperienceBudget": 0,
                "searchPriority": ["required"],
                "candidateSelectionPolicy": {
                    "autoSelectWhenDominant": True,
                    "askWhenMaterialTradeoff": True,
                    "preferLowDetour": True,
                    "avoidRecentEntities": True,
                },
                "schedulePolicy": {
                    "respectOpeningWindowsWhenKnown": True,
                    "allowProvisionalWhenUnknown": True,
                },
            },
        }
    target_scope = context.get("targetScope") if isinstance(context.get("targetScope"), dict) else {}
    segment_ids = [str(item) for item in target_scope.get("segmentIds") or [] if str(item)]
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "patch_itinerary",
        "actionDirective": {
            "type": "patch_itinerary",
            "operationIntent": "complex_patch",
            "baseVersionId": active_version_id,
            "targetSegmentIds": segment_ids[:1],
            "requestedOutcome": message or "更新当前行程",
        },
    }


def initial_plan_payload(payload, context: Optional[dict] = None) -> str:
    if isinstance(payload, str):
        return payload
    itinerary = payload.get("fullItinerary") if isinstance(payload, dict) else None
    if not isinstance(itinerary, dict):
        adapted = _align_initial_plan_payload_to_context(payload, context or {}) if isinstance(payload, dict) else payload
        return json.dumps(adapted, ensure_ascii=False)
    day_slots = []
    intent_pools = []
    for day in itinerary.get("days") or []:
        day_number = int(day.get("dayNumber") or 1)
        for index, segment in enumerate(day.get("segments") or [], start=1):
            name = str(segment.get("poiName") or f"第{index}站")
            slot_id = f"day{day_number}_segment{index}"
            day_slots.append(
                {
                    "slotId": slot_id,
                    "dayNumber": day_number,
                    "date": "2026-10-01",
                    "timeWindow": f"{segment.get('startTime') or '09:00'}-待定",
                    "startTime": segment.get("startTime") or "09:00",
                    "durationMinutes": int(segment.get("durationMinutes") or 90),
                    "kind": "landmark",
                    "rawNeed": name,
                    "routeAnchor": True,
                    "priority": 90 - index,
                    "notes": segment.get("notes") or "",
                }
            )
            intent_pools.append(
                {
                    "poolId": f"pool_{day_number}_{index}",
                    "rawNeed": name,
                    "city": itinerary.get("city") or "北京",
                    "intentType": "landmark",
                    "targetCount": 1,
                    "preferredTypes": [segment.get("category") or "风景名胜"],
                    "rejectedTypes": ["停车场", "公司"],
                    "routePreference": {"sameDayUnique": True},
                    "assignToSlots": [slot_id],
                    "candidateHints": [name],
                    "hintPolicy": "user_explicit_hint",
                }
            )
    generated = {
            "reply": payload.get("reply") or "已生成可编辑行程。",
            "mode": "day_slots",
            "daySlots": day_slots,
            "intentPools": intent_pools,
            "warnings": payload.get("warnings") or [],
        }
    return json.dumps(
        _align_initial_plan_payload_to_context(generated, context or {}),
        ensure_ascii=False,
    )


def fake_amap_fetch(_service, params):
    keyword = params["keywords"]
    if keyword == "故宫博物院":
        return {
            "status": "1",
            "pois": [
                amap_poi("B000TIANTAN", "天坛公园", "东城区", "116.410886,39.881949"),
                amap_poi("B000DITAN", "地坛公园", "东城区", "116.417296,39.949558"),
                amap_poi("B000SHICHAHAI", "什刹海", "西城区", "116.386,39.941"),
                amap_poi(
                    "B000PALACE",
                    "故宫博物院",
                    "东城区",
                    "116.397026,39.918058",
                    provider_type="科教文化服务;博物馆;风景名胜",
                ),
                amap_poi("B000YUANMING", "圆明园遗址公园", "海淀区", "116.309,40.008"),
                amap_poi("B000HUANGJIDIAN", "故宫博物院-皇极殿", "东城区", "116.3975,39.9185"),
                amap_poi("B000BEIHAI", "北海公园", "西城区", "116.3895,39.9255"),
            ],
        }
    if keyword == "故宫":
        return {
            "status": "1",
            "pois": [
                {
                    "id": "B000PALACE",
                    "name": "故宫博物院",
                    "type": "科教文化服务;博物馆;风景名胜",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "景山前街4号",
                    "location": "116.397026,39.918058",
                    "photos": [],
                }
            ],
        }
    return {
        "status": "1",
        "pois": [
            {
                "id": "B000JINGSHAN",
                "name": "景山公园",
                "type": "风景名胜",
                "cityname": "北京市",
                "adname": "西城区",
                "address": "景山西街",
                "location": "116.3969,39.9236",
                "photos": [],
            }
        ],
    }


def amap_poi(
    amap_id: str,
    name: str,
    district: str,
    location: str,
    *,
    provider_type: str = "风景名胜",
) -> dict:
    return {
        "id": amap_id,
        "name": name,
        "type": provider_type,
        "cityname": "北京市",
        "adname": district,
        "address": "高德地址",
        "location": location,
        "photos": [],
    }


def full_itinerary_output() -> dict:
    return {
        "reply": "已生成北京 1 日行程。",
        "mode": "full_itinerary",
        "operations": [],
        "fullItinerary": {
            "title": "北京轻松 1 日游",
            "city": "北京",
            "days": [
                {
                    "dayNumber": 1,
                    "title": "故宫与景山",
                    "segments": [
                        {
                            "poiName": "故宫",
                            "category": "scenic",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "notes": "上午游览故宫",
                            "estimatedCost": 60,
                        },
                        {
                            "poiName": "景山",
                            "category": "scenic",
                            "startTime": "12:30",
                            "durationMinutes": 60,
                            "notes": "登高看中轴线",
                            "estimatedCost": 10,
                        },
                    ],
                }
            ],
        },
        "poiResolutionRequests": [],
        "warnings": [],
    }


def single_palace_itinerary_output() -> dict:
    return {
        "reply": "已生成北京一日游行程，仅包含故宫博物院。",
        "mode": "full_itinerary",
        "operations": [],
        "fullItinerary": {
            "title": "北京故宫 1 日游",
            "city": "北京",
            "days": [
                {
                    "dayNumber": 1,
                    "title": "故宫博物院",
                    "segments": [
                        {
                            "poiName": "故宫博物院",
                            "category": "scenic",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "notes": "只安排故宫博物院",
                            "estimatedCost": 60,
                        }
                    ],
                }
            ],
        },
        "poiResolutionRequests": [
            {"name": "天坛公园", "category": "scenic"},
            {"name": "地坛公园", "category": "scenic"},
            {"name": "什刹海", "category": "scenic"},
            {"name": "故宫博物院", "category": "scenic"},
            {"name": "圆明园遗址公园", "category": "scenic"},
        ],
        "warnings": [],
    }


def patch_output(title: str = "北京轻松慢游") -> dict:
    return {
        "reply": f"已把标题改成{title}。",
        "mode": "patch",
        "operations": [{"op": "replace_trip_title", "value": title}],
        "fullItinerary": None,
        "poiResolutionRequests": [],
        "warnings": [],
    }


class MissingWriteRetryStreamProvider(IntentContractProviderMixin):
    def __init__(self):
        self.calls = 0

    def run_tool_loop(self, _context: dict, tool_registry) -> AgentToolLoopResult:
        self.calls += 1
        if self.calls == 1:
            tool_registry.execute("call_read_first", "read_itinerary", {})
            return AgentToolLoopResult(reply="先读取但暂未写入时间轴。", tool_events=tool_registry.events)
        tool_registry.execute("call_read_retry", "read_itinerary", {})
        tool_registry.execute(
            "call_patch_retry",
            "patch_itinerary",
            {"operations": [replace_itinerary_tool_operation()]},
        )
        return AgentToolLoopResult(reply="已重试并写入北京 1 日行程。", tool_events=tool_registry.events)


class ToolLoopDiagnosticsStreamProvider(IntentContractProviderMixin):
    def run_tool_loop(self, _context: dict, tool_registry) -> AgentToolLoopResult:
        tool_registry.execute("call_read_diag", "read_itinerary", {})
        diagnostics = {
            "reason": "Agent tool loop exceeded maxToolRounds=5",
            "maxToolRounds": 5,
            "roundCount": 5,
            "requiredToolSequence": ["read_itinerary", "patch_itinerary"],
            "completedRequiredToolCount": 1,
            "nextRequiredTool": "patch_itinerary",
            "successfulPatchActiveVersionId": None,
        }
        diagnostic_event = {
            "id": "tool_loop_diagnostics",
            "toolName": "agent_tool_loop_diagnostics",
            "type": "agent",
            "label": "Agent 工具循环诊断",
            "status": "failed",
            "inputSummary": "",
            "outputSummary": diagnostics["reason"],
            "providerName": "deepseek-tool-loop",
            "fallbackUsed": False,
            "failureReason": diagnostics["reason"],
            "startedAt": "2026-06-10T10:00:05Z",
            "finishedAt": "2026-06-10T10:00:05Z",
            "timestamp": "2026-06-10T10:00:05Z",
            "detail": "Agent tool loop exceeded maxToolRounds=5；nextRequiredTool=patch_itinerary",
            "metadata": {"resultPreview": diagnostics},
        }
        tool_registry.events.append(diagnostic_event)
        raise AgentToolLoopError(diagnostics["reason"], tool_registry.events, diagnostics=diagnostics)


def replace_itinerary_tool_operation() -> dict:
    return {
        "op": "replace_itinerary",
        "fullItinerary": {
            "title": "北京故宫 1 日游",
            "city": "北京",
            "templateType": "agent_mvp",
            "budgetEstimate": 60,
            "budgetDeltaExplanation": "Agent 工具写入的估算。",
            "decisionRationale": "DeepSeek tool calling patch_itinerary 写入。",
            "status": "draft",
            "days": [
                {
                    "id": "day_retry_1",
                    "dayNumber": 1,
                    "title": "故宫博物院",
                    "weatherSummary": "",
                    "riskSummary": "",
                    "totalEstimatedCost": 60,
                    "segments": [
                        {
                            "id": "seg_retry_1",
                            "startTime": "09:00",
                            "endTime": "11:00",
                            "kind": "activity",
                            "poi": {
                                "id": "poi_retry_1",
                                "amapId": "B000PALACE",
                                "name": "故宫博物院",
                                "city": "北京市",
                                "category": "scenic",
                                "latitude": 39.918058,
                                "longitude": 116.397026,
                                "source": "amap-place-search",
                                "confidence": 0.91,
                                "type": "风景名胜",
                                "district": "东城区",
                                "address": "高德地址",
                                "sourceNote": "高德 WebService POI 搜索",
                                "photos": [],
                            },
                            "durationMinutes": 120,
                            "estimatedCost": 60,
                            "notes": "上午游览故宫",
                            "sourceAttribution": "Agent tool loop",
                            "confidence": 0.91,
                        }
                    ],
                }
            ],
            "routeOptions": [],
            "weatherSignals": [],
            "trafficCrowdingSignals": [],
            "ticketLookupResults": [],
        },
    }


def test_agent_message_stream_emits_execution_events_before_final_response(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.services.agent_service.create_agent_provider", lambda: FakeProvider(full_itinerary_output())
    )
    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_amap_fetch)
    monkeypatch.setattr(RouteService, "build_routes", recorded_provider_routes)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={
                "content": route_ready_request(
                    "帮我安排北京一天，10月1日出发，想去故宫博物院和景山公园"
                ),
                "context": {"selectedDayNumber": 1},
            },
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[0]["event"] == "reasoning_status"
    assert lines[0]["data"]["status"] == "running"
    assert lines[0]["data"]["semanticKey"] == "request_understanding"
    assert any(item["event"] == "user_turn" for item in lines[:-1])
    reasoning_events = [item["data"] for item in lines if item["event"] == "reasoning_status"]
    assert reasoning_events
    assert reasoning_events[-1]["phase"] == "result"
    assert reasoning_events[-1]["status"] == "completed"
    assert lines[-1]["event"] == "message_response"
    user_turn_line = next(item for item in lines if item["event"] == "user_turn")
    assert user_turn_line["data"]["role"] == "user"
    assert user_turn_line["data"]["content"].startswith("帮我安排北京一天，10月1日出发")
    execution_events = [item["data"] for item in lines if item["event"] == "execution_event"]
    labels = [event["label"] for event in execution_events]
    assert "接收用户消息" in labels
    assert "构建 Agent 请求上下文" in labels
    assert "Controller Full 决策开始" in labels
    assert "Controller 模型决策" in labels
    assert "生成每日行程结构" in labels
    assert "检索真实地点候选" in labels
    assert "评估地点与路线可行性" in labels
    assert all("status" in event and "timestamp" in event for event in execution_events)
    assert all(isinstance(event.get("durationMs"), int) for event in execution_events)
    visible_sequences = [int(event["sequence"]) for event in execution_events if bool(event.get("userVisible"))]
    assert visible_sequences == list(range(1, len(visible_sequences) + 1))
    run_elapsed_values = [
        event["metadata"].get("runElapsedMs")
        for event in execution_events
        if isinstance(event.get("metadata"), dict) and "runElapsedMs" in event["metadata"]
    ]
    assert run_elapsed_values
    assert run_elapsed_values == sorted(run_elapsed_values)
    terminal_event = next(
        event
        for event in reversed(execution_events)
        if event["type"] == "agent_run" and event["metadata"].get("phase") == "run_finished"
    )
    assert terminal_event["durationMs"] >= 0
    assert terminal_event["metadata"]["runDurationMs"] >= terminal_event["durationMs"]
    final = lines[-1]["data"]
    final_content = final["assistantTurn"]["content"]
    assert final_content
    assert final["terminalStatus"] == "partial_success"
    assert final["version"]["sourceType"] in {"agent", "agent_enrichment"}
    assert final["itinerary"]["title"] == "北京可执行1日规划草案"
    assert len(final["itinerary"]["routeOptions"]) == 1
    assert final["itinerary"]["routeOptions"][0]["provider"] == "amap-webservice"
    assert final["itinerary"]["routeOptions"][0]["distanceMeters"] > 0
    assert final["itinerary"]["routeOptions"][0]["durationSeconds"] > 0
    persisted_terminal_event = final["assistantTurn"]["planningSteps"][-1]
    assert persisted_terminal_event["type"] == "agent_run"
    assert persisted_terminal_event["durationMs"] == terminal_event["durationMs"]
    assert persisted_terminal_event["metadata"]["runDurationMs"] == terminal_event["metadata"]["runDurationMs"]
    assert final["reasoningStatuses"] == final["assistantTurn"]["reasoningStatuses"]
    status_snapshot = client.get(
        f"/api/agent/sessions/{session['sessionId']}/reasoning-statuses",
        params={"turnId": final["userTurn"]["id"], "afterSequence": 0},
    )
    assert status_snapshot.status_code == 200
    status_payload = status_snapshot.json()
    assert status_payload["active"] is False
    assert status_payload["sourceUserTurnId"] == final["userTurn"]["id"]
    assert status_payload["assistantTurnId"] == final["assistantTurn"]["id"]
    assert status_payload["terminalStatus"] == "completed"
    assert status_payload["statuses"][-1]["phase"] == "result"


@pytest.mark.parametrize(
    ("terminal_status", "assistant_status", "expected"),
    [
        ("success", "active", "completed"),
        ("no_safe_action", "active", "completed"),
        ("needs_confirmation", "active", "needs_confirmation"),
        ("read_only", "active", "completed"),
        ("cancelled", "active", "cancelled"),
        ("failed", "failed", "failed"),
    ],
)
def test_reasoning_terminal_distinguishes_safe_noop_from_runtime_failure(
    terminal_status,
    assistant_status,
    expected,
):
    response_payload = {
        "assistantTurn": {
            "status": assistant_status,
            "content": "本轮没有可安全执行的明确动作，行程未修改。",
        }
    }

    assert _reasoning_terminal_from_response(response_payload, terminal_status) == expected


def test_agent_message_endpoint_redacts_private_controller_metadata_across_events(monkeypatch):
    clear_database()
    secret = "PRIVATE_CHAIN_SENTINEL_ORDINARY_RESPONSE"
    private_event = {
        "type": "controller_full_started",
        "label": "Controller Full 开始",
        "status": "completed",
        "detail": "已请求模型决策。",
        "metadata": {
            "hiddenPrompt": "PRIVATE_HIDDEN_PROMPT",
            "internalPrompt": "PRIVATE_INTERNAL_PROMPT",
            "analysis": secret,
        },
        "timestamp": "2026-08-20T00:00:00Z",
    }
    echoed_event = {
        "type": "controller_full_completed",
        "label": "Controller Full 完成",
        "status": "completed",
        "detail": f"safe prefix {secret} safe suffix",
        "metadata": {"prompt": "PRIVATE_DIRECT_PROMPT"},
        "timestamp": "2026-08-20T00:00:01Z",
    }

    def response_with_private_metadata(self, session_id, payload, **_kwargs):
        del self, session_id, payload
        return AgentMessageResponse.model_validate(
            {
                "userTurn": {
                    "id": "turn_private_user",
                    "role": "user",
                    "content": "生成行程",
                    "turnIndex": 1,
                    "status": "active",
                    "createdAt": "2026-08-20T00:00:00Z",
                    "updatedAt": "2026-08-20T00:00:00Z",
                },
                "assistantTurn": {
                    "id": "turn_private_assistant",
                    "role": "assistant",
                    "content": "已完成。",
                    "turnIndex": 2,
                    "status": "active",
                    "parentTurnId": "turn_private_user",
                    "planningSteps": [private_event, echoed_event],
                    "toolEvents": [private_event, echoed_event],
                    "createdAt": "2026-08-20T00:00:01Z",
                    "updatedAt": "2026-08-20T00:00:01Z",
                },
                "planningSteps": [private_event, echoed_event],
                "toolEvents": [private_event, echoed_event],
                "terminalStatus": "success",
            }
        )

    monkeypatch.setattr(TripAgentRuntime, "send_agent_message", response_with_private_metadata)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        response = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json={"content": "生成行程", "context": {}},
        )

    assert response.status_code == 200
    serialized = json.dumps(response.json(), ensure_ascii=False)
    assert secret not in serialized
    assert "PRIVATE_HIDDEN_PROMPT" not in serialized
    assert "PRIVATE_INTERNAL_PROMPT" not in serialized
    assert "PRIVATE_DIRECT_PROMPT" not in serialized
    assert "hiddenPrompt" not in serialized
    assert "internalPrompt" not in serialized
    assert '"prompt"' not in serialized
    assert "[redacted]" in serialized


def test_agent_message_stream_does_not_restore_legacy_tool_loop_without_controller(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    provider = MissingWriteRetryStreamProvider()
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": "帮我安排北京一天，10月1日出发，轻松一点", "context": {"selectedDayNumber": 1}},
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert provider.calls == 0
    execution_events = [item["data"] for item in lines if item["event"] == "execution_event"]
    labels = [event["label"] for event in execution_events]
    assert "执行 Agent 工具循环" not in labels
    assert "Controller Full 决策开始" in labels
    assert lines[-1]["event"] == "message_response"
    assert lines[-1]["data"]["version"] is None
    assert lines[-1]["data"]["terminalStatus"] == "needs_confirmation"


def test_agent_message_stream_does_not_run_legacy_diagnostics_provider_without_controller(monkeypatch):
    clear_database()
    provider = ToolLoopDiagnosticsStreamProvider()
    monkeypatch.setattr("src.services.agent_service.create_agent_provider", lambda: provider)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": "帮我安排北京一天，10月1日出发，轻松一点", "context": {"selectedDayNumber": 1}},
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    execution_labels = [item["data"]["label"] for item in lines if item["event"] == "execution_event"]
    assert "Agent 工具循环诊断" not in execution_labels
    assert "Controller Full 决策开始" in execution_labels
    assert lines[-1]["event"] == "message_response"
    assert lines[-1]["data"]["assistantTurn"]["status"] == "active"
    assert lines[-1]["data"]["version"] is None
    assert lines[-1]["data"]["terminalStatus"] == "needs_confirmation"


def test_agent_message_stream_preserves_structured_route_quality_error(monkeypatch):
    clear_database()

    def fail_route_quality(self, *args, **kwargs):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_proposal_route_quality_failed",
                "message": "所选方案中的餐饮与相邻路线偏绕，未创建正式行程。",
                "details": {
                    "routeQualityIssues": [
                        {
                            "fromPoiName": "清华大学",
                            "toPoiName": "老北京炸酱面",
                            "distanceKm": 15,
                            "durationMinutes": 69,
                        }
                    ],
                    "recommendedNextActions": ["choose_nearby_meal"],
                },
            },
        )

    monkeypatch.setattr("src.runtime.agent_runtime.TripAgentRuntime.send_agent_message", fail_route_quality)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": "选择方案", "context": {}},
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[0]["event"] == "reasoning_status"
    assert lines[0]["data"]["status"] == "running"
    assert lines[-2]["event"] == "reasoning_status"
    assert lines[-2]["data"]["status"] == "failed"
    assert lines[-2]["data"]["phase"] == "result"
    assert lines[-1] == {
        "event": "error",
        "data": {
            "message": "所选方案中的餐饮与相邻路线偏绕，未创建正式行程。",
            "statusCode": 409,
            "code": "plan_proposal_route_quality_failed",
            "details": {
                "routeQualityIssues": [
                    {
                        "fromPoiName": "清华大学",
                        "toPoiName": "老北京炸酱面",
                        "distanceKm": 15,
                        "durationMinutes": 69,
                    }
                ],
                "recommendedNextActions": ["choose_nearby_meal"],
            },
        },
    }


def test_agent_message_stream_preserves_refreshable_proposal_scope_error_code(monkeypatch):
    clear_database()

    def fail_stale_proposal(self, *args, **kwargs):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_proposal_request_scope_invalid",
                "message": "该方案采用能力不属于当前请求合同，请刷新方案后重试。",
            },
        )

    monkeypatch.setattr("src.runtime.agent_runtime.TripAgentRuntime.send_agent_message", fail_stale_proposal)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": "确认编辑方案", "context": {}},
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[-1] == {
        "event": "error",
        "data": {
            "message": "方案确认入口已更新，请加载当前会话的最新方案后重新选择。",
            "statusCode": 409,
            "code": "plan_proposal_request_scope_invalid",
        },
    }


def test_agent_message_stream_redacts_unclassified_internal_error(monkeypatch):
    clear_database()

    def fail_internal(self, *args, **kwargs):
        raise RuntimeError("portfolio_partial_anchor_grounding_evidence_missing: SECRET_STACK")

    monkeypatch.setattr("src.runtime.agent_runtime.TripAgentRuntime.send_agent_message", fail_internal)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": "生成行程", "context": {}},
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[0]["event"] == "reasoning_status"
    assert lines[0]["data"]["status"] == "running"
    assert lines[-2]["data"]["status"] == "failed"
    assert "SECRET_STACK" not in json.dumps(lines, ensure_ascii=False)
    assert lines[-1] == {
        "event": "error",
        "data": {
            "message": "行程规划暂时未完成，请稍后重试。",
            "statusCode": 500,
            "code": "agent_internal_error",
        },
    }


def test_agent_message_endpoints_fail_closed_on_untrusted_http_exception_detail(monkeypatch):
    clear_database()
    secret = "PRIVATE_CHAIN_SENTINEL_HTTP_DETAIL"

    def fail_untrusted_validation(self, *args, **kwargs):
        raise HTTPException(
            status_code=422,
            detail={"code": secret, "message": secret},
        )

    monkeypatch.setattr("src.runtime.agent_runtime.TripAgentRuntime.send_agent_message", fail_untrusted_validation)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        payload = {"content": "生成行程", "context": {}}
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json=payload,
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]
        ordinary = client.post(
            f"/api/agent/sessions/{session['sessionId']}/messages",
            json=payload,
        )

    assert response.status_code == 200
    serialized_lines = json.dumps(lines, ensure_ascii=False).casefold()
    assert secret.casefold() not in serialized_lines
    assert lines[-1] == {
        "event": "error",
        "data": {
            "message": "当前请求未能安全执行，请检查输入后重试。",
            "statusCode": 422,
            "code": "agent_request_rejected",
        },
    }
    assert ordinary.status_code == 422
    assert secret.casefold() not in ordinary.text.casefold()
    assert ordinary.json()["detail"] == {
        "message": "当前请求未能安全执行，请检查输入后重试。",
        "code": "agent_request_rejected",
    }


def test_agent_message_stream_closes_reasoning_as_cancelled_for_http_499(monkeypatch):
    clear_database()

    def cancel_run(self, *args, **kwargs):
        raise HTTPException(status_code=499, detail="agent_run_cancelled")

    monkeypatch.setattr("src.runtime.agent_runtime.TripAgentRuntime.send_agent_message", cancel_run)

    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        with client.stream(
            "POST",
            f"/api/agent/sessions/{session['sessionId']}/messages/stream",
            json={"content": "停止本轮", "context": {}},
        ) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert lines[0]["event"] == "reasoning_status"
    assert lines[0]["data"]["status"] == "running"
    assert lines[-2]["data"]["status"] == "cancelled"
    assert lines[-1]["event"] == "error"
    assert lines[-1]["data"]["statusCode"] == 499


def test_export_planning_trace_is_scoped_redacted_and_read_only(monkeypatch):
    clear_database()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("trace export must not invoke controller, providers, route, or writer")

    monkeypatch.setattr(DeepSeekAgentProvider, "run_tool_loop", unexpected_call)
    monkeypatch.setattr(CreativePortfolioProviderService, "generate", unexpected_call)
    monkeypatch.setattr(PoiDiscoveryService, "discover", unexpected_call)
    monkeypatch.setattr(MapPoiService, "search", unexpected_call)
    monkeypatch.setattr(MapPoiService, "search_nearby", unexpected_call)
    monkeypatch.setattr(RouteService, "build_routes", unexpected_call)
    monkeypatch.setattr(PlanProposalCommitService, "commit", unexpected_call)
    now = "2026-07-27T12:00:00+00:00"
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "trace export"}).json()
        session_id = session["sessionId"]
        plan_id = session["activePlanId"]
        other_session = client.post(
            "/api/agent/sessions",
            json={"city": "上海", "title": "other trace scope"},
        ).json()
        assistant_turn_id = "turn_trace_export"
        planning_run_id = "run_trace_export"
        user_turn_id = "turn_trace_export_user"
        missing_run_turn_id = "turn_trace_export_missing_run"
        missing_planning_run_id = "run_trace_export_missing"
        reason_code_poisons = {
            "providerPayload": {"reasonCode": "provider_payload_reason_must_not_export"},
            "headers": {"reasonCodes": ["header_reason_must_not_export"]},
            "reasoning": {"nested": {"reasonCode": "reasoning_reason_must_not_export"}},
        }
        string_poisons = (
            "load .env.production",
            "DEEPSEEK_API_KEY=trace-secret-value",
            "SECRET_KEY=trace-value",
            "Set-Cookie: trip_session=trace-secret-value",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0cmFjZSJ9.signature",
            "sk-proj-trace-secret-value",
            "sk-x",
            "AIzaSyTraceSecretValue",
            "data:image/png;base64,AAAA",
            "blob:opaque-trace-export",
            "file:private-trace.txt",
            "javascript:alert(1)",
            "mailto:trace@example.invalid",
            "phase C:/Users/Thinkpad/private.txt",
            r"phase C:\Users\TestUser\private.txt",
            "phase /home/thinkpad/private.txt",
            "phase /用户/秘密/trace.json",
            "phase ~/private.txt",
            "//cdn.example.invalid/private-trace",
            "https:example.invalid/private-trace",
            "custom+trace:private-reference",
            "x:private-trace",
            "cdn.example.invalid/private-image.png",
            "www.example.invalid/private-trace",
        )
        response_payload = {
            "planningSteps": [
                {
                    "type": "portfolio_staging",
                    "reasonCode": "phase_source_reason",
                    "status": "completed",
                    "timestamp": now,
                    "durationMs": 123,
                    "sequence": 7,
                    "metadata": {
                        "resultPreview": {
                            "briefId": "brief_trace",
                            "poolId": "pool_trace",
                            "planningSlotId": "slot_trace",
                            "dayNumber": 2,
                            "candidateCount": 4,
                            "amapTextCount": 2,
                            "amapRouteCount": 1,
                            "providerCallCount": 1,
                            "routePreflightMs": 9,
                            "compiledSearchProfileCount": 1,
                            "distinctSearchProfileFingerprintCount": 1,
                            "familySearchProfileCoverageCount": 1,
                            "familySearchSemanticMismatchCount": 0,
                            "genericScenicCollapseCount": 0,
                            "profileCoverageShortcutHitCount": 1,
                            "invalidCoverageShortcutCount": 0,
                            "semanticCandidateAcceptedCount": 2,
                            "semanticCandidateRejectedCount": 1,
                            "familySpecificAmapCandidateCount": 2,
                            "webSeedGroundedCandidateCount": 1,
                            "duplicateExcludedBeforeRouteCount": 1,
                            "routePreflightAvoidedBySemanticFilterCount": 1,
                            "profileMetrics": [
                                {
                                    "briefId": "brief_trace",
                                    "poolId": "pool_trace",
                                    "planningSlotId": "slot_trace",
                                    "searchProfileId": "profile_trace",
                                    "searchProfileFingerprint": "a" * 64,
                                    "experienceFamily": "local_life",
                                    "activityMode": "observe_walk",
                                    "queryPlanCount": 4,
                                    "fallbackLevel": 3,
                                    "excludedPhysicalPoiCount": 2,
                                    "queryModes": ["amap_text", "web_seed_then_amap"],
                                    "providerKeys": ["local_service", "market"],
                                    "keyword": "SECRET_PROFILE_QUERY_MUST_NOT_EXPORT",
                                    "providerPayload": {"token": "SECRET_PROFILE_TOKEN"},
                                }
                            ],
                            "briefMetrics": [
                                {
                                    "briefId": "brief_trace",
                                    "briefIndex": 0,
                                    "status": "failed",
                                    "durationMs": 19,
                                    "candidateCount": 4,
                                    "webDiscoveryMs": 3,
                                    "amapGroundingMs": 4,
                                    "routePreflightMs": 9,
                                    "routePreflightCallCount": 1,
                                    "repairMs": 2,
                                    "repairCallCount": 1,
                                    "verifierMs": 1,
                                    "reasonCodes": ["portfolio_anchor_target_shortfall"],
                                    "prompt": "SECRET_BRIEF_PROMPT_MUST_NOT_EXPORT",
                                    "providerPayload": {"token": "SECRET_BRIEF_TOKEN"},
                                }
                            ],
                            "webDiscoveryAttempts": [
                                {
                                    "query": "北京 高校 官方 地点",
                                    "providerName": "recorded-web",
                                    "providerStatus": "success",
                                    "status": "grounded",
                                    "reasonCode": "web_discovery_grounded",
                                    "durationMs": 3.25,
                                    "webDurationMs": 3.25,
                                    "amapGroundingMs": 5.5,
                                    "resultCount": 2,
                                    "scope": {
                                        "briefId": "brief_trace",
                                        "poolId": "pool_trace",
                                        "planningSlotId": "slot_trace",
                                        "dayNumber": 2,
                                        "sourceGoalId": "goal_trace",
                                    },
                                    "seedGroundings": [
                                        {
                                            "seedName": "清华大学",
                                            "providerName": "amap-place-search",
                                            "status": "grounded",
                                            "reasonCode": "amap_seed_grounded",
                                            "durationMs": 5.5,
                                            "candidateCount": 3,
                                            "selectedCandidates": [
                                                {
                                                    "amapId": "B0TRACEAMAP",
                                                    "name": "清华大学",
                                                    "url": "https://example.invalid/poi",
                                                    "snippet": "SECRET_SNIPPET_MUST_NOT_EXPORT",
                                                }
                                            ],
                                            "headers": {"Authorization": "Bearer SECRET_GROUNDING_HEADER"},
                                        }
                                    ],
                                    "selectedCandidates": [
                                        {
                                            "amapId": "B0TRACEAMAP",
                                            "name": "清华大学",
                                            "scope": {
                                                "briefId": "brief_trace",
                                                "poolId": "pool_trace",
                                                "planningSlotId": "slot_trace",
                                                "dayNumber": 2,
                                                "sourceGoalId": "goal_trace",
                                            },
                                            "localPath": "C:\\Users\\Thinkpad\\private.json",
                                        }
                                    ],
                                    "prompt": "SECRET_DISCOVERY_PROMPT_MUST_NOT_EXPORT",
                                    "providerPayload": {"token": "SECRET_DISCOVERY_PAYLOAD_MUST_NOT_EXPORT"},
                                    "responseHeaders": {"Set-Cookie": "SECRET_DISCOVERY_COOKIE_MUST_NOT_EXPORT"},
                                }
                            ],
                            "controllerPerformance": [
                                {
                                    "callKind": "full",
                                    "payloadBytes": 456,
                                    "preHeaderWaitDurationMs": 7,
                                    "prompt": "SECRET_PROMPT_MUST_NOT_EXPORT",
                                    "authorization": "Bearer secret",
                                    "responseHeaders": {"Set-Cookie": "SECRET_HEADER_MUST_NOT_EXPORT"},
                                }
                            ],
                            "staging": {"reasonCode": "portfolio_anchor_target_shortfall"},
                            "reasonCodes": [
                                "phase_preview_reason",
                                "goal_occurrence_identity_reused:distinct:goal_campus_visit:B0001",
                                "authorization:secret",
                            ],
                            "providerPayload": {
                                "apiKey": "SECRET_KEY_MUST_NOT_EXPORT",
                                "reasonCode": "preview_provider_reason_must_not_export",
                            },
                            "headers": {"reasonCode": "preview_header_reason_must_not_export"},
                            "reasoning": {"reasonCodes": ["preview_reasoning_reason_must_not_export"]},
                            "imageUrl": "https://example.invalid/image.png",
                            "localPath": "C:\\Users\\Thinkpad\\private.txt",
                        },
                        **reason_code_poisons,
                    },
                }
            ],
            "toolEvents": [
                {
                    "type": "https://example.invalid/malicious-phase",
                    "status": "completed",
                    "timestamp": "SECRET_PROMPT_SHOULD_NOT_EXPORT",
                    "metadata": {"briefId": "C:\\Users\\Thinkpad\\malicious-brief"},
                }
            ],
            "timelineMutationOutcome": {"versionDelta": 1, "patchDelta": 1, "routeWriteDelta": 0},
            "prompt": "SECRET_PROMPT_MUST_NOT_EXPORT",
        }
        response_payload["toolEvents"].append(dict(response_payload["planningSteps"][0]))
        response_payload["toolEvents"].extend(
            {
                "type": poison,
                "status": "completed",
                "timestamp": now,
            }
            for poison in string_poisons
        )
        with open_db() as connection:
            connection.execute(
                """INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status, planning_run_id,
                    agent_response_json, created_at, updated_at
                ) VALUES (?, ?, 'assistant', 'trace result', 1, 'active', ?, ?, ?, ?)""",
                (assistant_turn_id, session_id, planning_run_id, json.dumps(response_payload), now, now),
            )
            connection.execute(
                """INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status, planning_run_id,
                    agent_request_json, agent_response_json, created_at, updated_at
                ) VALUES (?, ?, 'user', 'trace user scope', 2, 'active', ?, ?, '{}', ?, ?)""",
                (
                    user_turn_id,
                    session_id,
                    planning_run_id,
                    json.dumps(
                        {
                            "selectedAgentChoice": {
                                "sourceAssistantTurnId": assistant_turn_id,
                                "choiceId": "choice_trace",
                                "persistedChoiceId": "choice_trace",
                                "persistedChoiceAction": "retry_model_planning",
                            }
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO conversation_turns (
                    id, session_id, role, content, turn_index, status, planning_run_id,
                    agent_response_json, created_at, updated_at
                ) VALUES (?, ?, 'assistant', 'missing run scope', 3, 'active', ?, '{}', ?, ?)""",
                (missing_run_turn_id, session_id, missing_planning_run_id, now, now),
            )
            connection.execute(
                """INSERT INTO planning_runs (
                    id, run_type, user_input, preference_summary, itinerary_plan_id,
                    itinerary_version_id, understood_requirements_json, constraint_summary_json,
                    tool_calls_json, source_assessments_json, feasibility_report_json,
                    final_summary, created_at
                ) VALUES (?, 'agent_message', ?, '', ?, NULL, '{}', '[]', ?, '[]', NULL, '', ?)""",
                (
                    planning_run_id,
                    "PRIVATE USER PROMPT MUST NOT EXPORT",
                    plan_id,
                    json.dumps(
                        [
                            {
                                "id": "tool_trace",
                                "toolName": "候选检索",
                                "status": "completed",
                                "queriedAt": now,
                                "metadata": {
                                    "briefId": "brief_trace",
                                    "poolId": "pool_trace",
                                    "planningSlotId": "slot_trace",
                                    "dayNumber": 2,
                                    "candidateCount": 4,
                                    "amapTextCount": 2,
                                    "fullProviderPayload": {"token": "SECRET"},
                                },
                                "traceSummary": {
                                    "sequence": 8,
                                    "briefId": "brief_trace",
                                    "poolId": "pool_trace",
                                    "planningSlotId": "slot_trace",
                                    "dayNumber": 2,
                                    "providerCallCount": 1,
                                    "controllerCalls": 0,
                                },
                            }
                        ]
                    ),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO agent_plan_portfolios (
                    id, session_id, source_user_turn_id, source_assistant_turn_id,
                    expected_base_version_id, source_observation_fingerprint,
                    request_contract_fingerprint, status, summary_json, failure_reason,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, 'turn_user_trace', ?, NULL, 'o', 'f', 'awaiting_selection', ?, NULL, NULL, ?, ?)""",
                (
                    "portfolio_trace",
                    session_id,
                    "turn_previous_portfolio_assistant",
                    json.dumps(
                        {
                            "visibleProposalIds": ["proposal_trace"],
                            "briefGenerationState": [
                                {
                                    "briefId": "brief_trace",
                                    "order": 0,
                                    "status": "completed",
                                    "resultType": "proposal",
                                    "reasonCodes": [],
                                    "localPath": "C:\\private\\trace.json",
                                }
                            ],
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO agent_plan_proposals (
                    id, portfolio_id, choice_id, rank_index, status, brief_json,
                    snapshot_json, score_json, verifier_json, evidence_json,
                    canonical_signature, generation_lineage_json, created_at, updated_at
                ) VALUES (?, ?, 'choice_trace', 0, 'offered', ?, '{}', ?, ?, '{}', 'signature_trace', '{}', ?, ?)""",
                (
                    "proposal_trace",
                    "portfolio_trace",
                    json.dumps({"briefId": "brief_trace"}),
                    json.dumps({"hardConstraintPassed": True}),
                    json.dumps(
                        {
                            "passed": True,
                            "reasonCodes": ["proposal_verifier_reason"],
                            **reason_code_poisons,
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO agent_choice_executions (
                    id, session_id, source_turn_id, source_user_turn_id, choice_id, action,
                    status, expected_base_version_id, result_version_id, execution_turn_id,
                    request_turn_id, attempt, continuation_json, checkpoint_fingerprint,
                    outcome_json, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'turn_user_trace', 'choice_trace', 'retry_model_planning',
                    'succeeded', NULL, NULL, ?, ?, 1, ?, 'fingerprint', ?, NULL, ?, ?)""",
                (
                    "choice_exec_trace",
                    session_id,
                    assistant_turn_id,
                    user_turn_id,
                    user_turn_id,
                    json.dumps(
                        {
                            "briefId": "brief_trace",
                            "poolId": "pool_trace",
                            "planningSlotId": "slot_trace",
                            "dayNumber": 2,
                            "kind": "expand_partial_portfolio",
                            "planningSelectionRootTurnId": "turn_root_trace",
                            "rootPortfolioId": "portfolio_trace",
                            "requestContractFingerprint": "f" * 64,
                            "expectedBaseVersionId": "ver_trace_base",
                            "focusBriefId": "brief_trace",
                            "requestedFocusBriefId": "brief_previous",
                            "resolvedDirectionSignature": "direction_signature_trace",
                        }
                    ),
                    json.dumps(
                        {
                            "versionDelta": 0,
                            "patchDelta": 0,
                            "routeWriteDelta": 0,
                            "reasonCode": "choice_outcome_reason",
                            "planningSelectionRootTurnId": "turn_root_trace",
                            "rootPortfolioId": "portfolio_trace",
                            "requestContractFingerprint": "f" * 64,
                            "focusBriefId": "brief_trace",
                            "cursorFocusBriefId": "brief_previous",
                            "resolvedFocusBriefId": "brief_trace",
                            "expansionFocusMode": "discover_next",
                            "nextBriefId": "brief_next",
                            "cursorAdvanced": True,
                            "cursorExhausted": False,
                            **reason_code_poisons,
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """UPDATE agent_choice_executions
                SET error_json = ?
                WHERE id = ?""",
                (
                    json.dumps(
                        {
                            "reasonCodes": ["choice_error_reason"],
                            **reason_code_poisons,
                        }
                    ),
                    "choice_exec_trace",
                ),
            )
            connection.commit()
            tracked_tables = (
                "conversation_turns",
                "planning_runs",
                "agent_plan_portfolios",
                "agent_plan_proposals",
                "agent_choice_executions",
                "itinerary_versions",
                "itinerary_patches",
                "route_options",
            )
            before = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
                for table in tracked_tables
            }

        exported = client.get(
            f"/api/agent/sessions/{session_id}/turns/{assistant_turn_id}/planning-runs/{planning_run_id}/trace-export"
        )
        wrong_scope = client.get(
            f"/api/agent/sessions/{session_id}/turns/{assistant_turn_id}/planning-runs/run_other/trace-export"
        )
        wrong_session = client.get(
            f"/api/agent/sessions/{other_session['sessionId']}/turns/{assistant_turn_id}"
            f"/planning-runs/{planning_run_id}/trace-export"
        )
        wrong_assistant_turn = client.get(
            f"/api/agent/sessions/{session_id}/turns/turn_trace_export_other"
            f"/planning-runs/{planning_run_id}/trace-export"
        )
        user_turn = client.get(
            f"/api/agent/sessions/{session_id}/turns/{user_turn_id}/planning-runs/{planning_run_id}/trace-export"
        )
        missing_run = client.get(
            f"/api/agent/sessions/{session_id}/turns/{missing_run_turn_id}"
            f"/planning-runs/{missing_planning_run_id}/trace-export"
        )

        with open_db() as connection:
            after = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
                for table in before
            }

    assert exported.status_code == 200
    trace = exported.json()
    assert trace["scope"] == {
        "sessionId": session_id,
        "assistantTurnId": assistant_turn_id,
        "planningRunId": planning_run_id,
    }
    assert trace["continuationScope"] == {
        "sourceAssistantTurnId": assistant_turn_id,
        "requestChoiceId": "choice_trace",
        "persistedChoiceId": "choice_trace",
        "executionId": "choice_exec_trace",
        "executionStatus": "succeeded",
        "executionAction": "retry_model_planning",
        "planningSelectionRootTurnId": "turn_root_trace",
        "rootPortfolioId": "portfolio_trace",
        "requestContractFingerprint": "f" * 64,
        "expectedBaseVersionId": "ver_trace_base",
        "checkpointFingerprint": "fingerprint",
        "focusBriefId": "brief_trace",
        "cursorFocusBriefId": "brief_previous",
        "requestedFocusBriefId": "brief_previous",
        "resolvedFocusBriefId": "brief_trace",
        "resolvedDirectionSignature": "direction_signature_trace",
        "expansionFocusMode": "discover_next",
        "nextBriefId": "brief_next",
        "cursorAdvanced": True,
        "cursorExhausted": False,
        "controllerCalled": False,
        "executionRoute": "controller_choice_resume",
    }
    assert len(trace["phases"]) == 4
    assert [phase["captureOrder"] for phase in trace["phases"]] == list(range(4))
    assert [phase["exportSequence"] for phase in trace["phases"]] == [1, 2, 3, 4]
    assert trace["phases"][0]["sequence"] == 7
    malformed_phase = next(
        phase for phase in trace["phases"] if phase["phase"] == "planning_event" and "executedAt" not in phase
    )
    assert "executedAt" not in malformed_phase
    assert malformed_phase.get("scope") == {}
    assert trace["phases"][0]["scope"] == {
        "briefId": "brief_trace",
        "poolId": "pool_trace",
        "planningSlotId": "slot_trace",
        "dayNumber": 2,
    }
    assert trace["phases"][0]["candidateCounts"]["candidateCount"] == 4
    assert trace["phases"][0]["callMetrics"]["amapTextCount"] == 2
    assert trace["phases"][0]["callMetrics"]["controllerCalls"] == 1
    assert trace["phases"][0]["callMetrics"]["compiledSearchProfileCount"] == 1
    assert trace["phases"][0]["callMetrics"]["routePreflightAvoidedBySemanticFilterCount"] == 1
    assert trace["phases"][0]["profileMetrics"] == [
        {
            "briefId": "brief_trace",
            "poolId": "pool_trace",
            "planningSlotId": "slot_trace",
            "searchProfileId": "profile_trace",
            "searchProfileFingerprint": "a" * 64,
            "experienceFamily": "local_life",
            "activityMode": "observe_walk",
            "queryPlanCount": 4,
            "fallbackLevel": 3,
            "excludedPhysicalPoiCount": 2,
            "queryModes": ["amap_text", "web_seed_then_amap"],
            "providerKeys": ["local_service", "market"],
        }
    ]
    tool_phase = next(phase for phase in trace["phases"] if phase["phase"] == "候选检索")
    assert tool_phase["sequence"] == 8
    assert tool_phase["scope"] == {
        "briefId": "brief_trace",
        "poolId": "pool_trace",
        "planningSlotId": "slot_trace",
        "dayNumber": 2,
    }
    assert tool_phase["callMetrics"] == {
        "amapTextCount": 2,
        "controllerCalls": 0,
        "providerCallCount": 1,
    }
    assert trace["phases"][0]["reasonCodes"] == [
        "phase_source_reason",
        "phase_preview_reason",
        "goal_occurrence_identity_reused:distinct:goal_campus_visit:B0001",
        "portfolio_anchor_target_shortfall",
    ]
    assert trace["phases"][0]["briefMetrics"] == [
        {
            "briefId": "brief_trace",
            "briefIndex": 0,
            "status": "failed",
            "durationMs": 19,
            "candidateCount": 4,
            "webDiscoveryMs": 3,
            "amapGroundingMs": 4,
            "routePreflightMs": 9,
            "routePreflightCallCount": 1,
            "repairMs": 2,
            "repairCallCount": 1,
            "verifierMs": 1,
            "reasonCodes": ["portfolio_anchor_target_shortfall"],
        }
    ]
    assert trace["phases"][0]["webDiscoveryAttempts"] == [
        {
            "queryFingerprint": hashlib.sha256("北京 高校 官方 地点".encode("utf-8")).hexdigest(),
            "providerName": "recorded-web",
            "providerStatus": "success",
            "status": "grounded",
            "reasonCodes": ["web_discovery_grounded"],
            "durationMs": 3.25,
            "webDurationMs": 3.25,
            "amapGroundingMs": 5.5,
            "resultCount": 2,
            "scope": {
                "briefId": "brief_trace",
                "poolId": "pool_trace",
                "planningSlotId": "slot_trace",
                "dayNumber": 2,
                "sourceGoalId": "goal_trace",
            },
            "seedGroundings": [
                {
                    "seedName": "清华大学",
                    "providerName": "amap-place-search",
                    "status": "grounded",
                    "reasonCodes": ["amap_seed_grounded"],
                    "durationMs": 5.5,
                    "candidateCount": 3,
                    "selectedCandidates": [{"amapId": "B0TRACEAMAP", "name": "清华大学"}],
                }
            ],
            "selectedCandidates": [
                {
                    "amapId": "B0TRACEAMAP",
                    "name": "清华大学",
                    "scope": {
                        "briefId": "brief_trace",
                        "poolId": "pool_trace",
                        "planningSlotId": "slot_trace",
                        "dayNumber": 2,
                        "sourceGoalId": "goal_trace",
                    },
                }
            ],
        }
    ]
    assert trace["portfolio"]["portfolios"][0]["briefGenerationState"][0]["status"] == "completed"
    assert trace["portfolio"]["portfolios"][0]["id"] == "portfolio_trace"
    assert trace["portfolio"]["proposals"][0]["status"] == "offered"
    assert trace["portfolio"]["proposals"][0]["reasonCodes"] == ["proposal_verifier_reason"]
    assert trace["choiceExecutions"][0]["scope"]["planningSlotId"] == "slot_trace"
    assert trace["choiceExecutions"][0]["continuationScope"]["planningSelectionRootTurnId"] == "turn_root_trace"
    assert trace["choiceExecutions"][0]["continuationScope"]["rootPortfolioId"] == "portfolio_trace"
    assert trace["choiceExecutions"][0]["continuationScope"]["focusBriefId"] == "brief_trace"
    assert trace["choiceExecutions"][0]["continuationScope"]["requestedFocusBriefId"] == "brief_previous"
    assert (
        trace["choiceExecutions"][0]["continuationScope"]["resolvedDirectionSignature"] == "direction_signature_trace"
    )
    assert trace["choiceExecutions"][0]["continuationScope"]["cursorAdvanced"] is True
    assert trace["choiceExecutions"][0]["reasonCodes"] == [
        "choice_outcome_reason",
        "choice_error_reason",
    ]
    assert trace["writeDeltas"] == {"versionDelta": 1, "patchDelta": 1, "routeWriteDelta": 0}
    exported_text = json.dumps(trace, ensure_ascii=False)
    for forbidden in (
        "SECRET_PROMPT_MUST_NOT_EXPORT",
        "SECRET_KEY_MUST_NOT_EXPORT",
        "PRIVATE USER PROMPT MUST NOT EXPORT",
        "Bearer secret",
        "SECRET_HEADER_MUST_NOT_EXPORT",
        "C:\\Users\\Thinkpad",
        "https://example.invalid/image.png",
        "https://example.invalid/malicious-phase",
        "providerPayload",
        "authorization",
        "SECRET_BRIEF_PROMPT_MUST_NOT_EXPORT",
        "SECRET_BRIEF_TOKEN",
        "authorization:secret",
        "provider_payload_reason_must_not_export",
        "header_reason_must_not_export",
        "reasoning_reason_must_not_export",
        "preview_provider_reason_must_not_export",
        "preview_header_reason_must_not_export",
        "preview_reasoning_reason_must_not_export",
        "SECRET_SNIPPET_MUST_NOT_EXPORT",
        "SECRET_GROUNDING_HEADER",
        "SECRET_DISCOVERY_PROMPT_MUST_NOT_EXPORT",
        "SECRET_DISCOVERY_PAYLOAD_MUST_NOT_EXPORT",
        "SECRET_DISCOVERY_COOKIE_MUST_NOT_EXPORT",
        "SECRET_PROFILE_QUERY_MUST_NOT_EXPORT",
        "SECRET_PROFILE_TOKEN",
        *string_poisons,
    ):
        assert forbidden not in exported_text
    assert before == after
    assert wrong_scope.status_code == 409
    assert wrong_scope.json()["detail"]["code"] == "planning_trace_scope_mismatch"
    for response in (wrong_session, wrong_assistant_turn, user_turn):
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "planning_trace_turn_not_found"
    assert missing_run.status_code == 404
    assert missing_run.json()["detail"]["code"] == "planning_trace_not_found"

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.api.schemas.maps import MapPoiPhotoResponse, MapPoiResponse, MapPoiSearchResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.models.route_option import RouteOption
from src.services.agent_run_control import (
    acquire_session_run,
    assert_session_run_active,
    release_session_run,
    request_session_run_cancel,
)
from src.services.conversation_service import ConversationService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.itinerary_service import ItineraryService
from src.services.agent_verifier_service import AgentVerifierService
from src.services.provider_route_insertion_service import (
    ProviderRouteInsertionResult,
    ProviderRouteInsertionService,
)
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_optimization_service import RouteOptimizationResult, RouteOptimizationService
from src.services.route_service import RouteProviderError


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM planning_runs;
            DELETE FROM amap_poi_candidates;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM ticket_lookup_results;
            DELETE FROM weather_signals;
            DELETE FROM route_options;
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


def test_server_sealed_zero_target_days_require_matching_explicit_rest_contract() -> None:
    snapshot = {
        "simpleOpenExecutionProfile": "simple_open_v1",
        "requiredPlanningDayNumbers": [1, 2],
        "explicitRestDayNumbers": [],
        "desiredDensityAnchorTargets": {"1": 1, "2": 0},
    }
    normalized = {
        "days": [
            {"dayNumber": 1, "segments": [{"id": "seg_day_1"}]},
            {"dayNumber": 2, "segments": []},
        ]
    }

    assert (
        ItineraryPatchService._server_sealed_simple_open_zero_target_days(
            snapshot,
            normalized,
            enabled=True,
        )
        == set()
    )

    snapshot["requiredPlanningDayNumbers"] = [1]
    snapshot["explicitRestDayNumbers"] = [2]

    assert ItineraryPatchService._server_sealed_simple_open_zero_target_days(
        snapshot,
        normalized,
        enabled=True,
    ) == {2}


def test_patch_service_marks_route_affecting_operations_for_refresh():
    service = ItineraryPatchService(sqlite3.connect(":memory:"))

    for operation in [
        "replace_itinerary",
        "add_segment",
        "remove_segment",
        "move_segment",
        "reorder_segments",
        "replace_segment_poi",
        "replace_segment_poi_from_candidate",
        "refresh_routes_for_day",
        "replace_transport_mode",
    ]:
        assert service._requires_route_refresh([ItineraryPatchOperation(op=operation)]) is True

    assert (
        service._requires_route_refresh(
            [ItineraryPatchOperation(op="replace_segment_start_time", segmentId="seg_1", value="10:00")]
        )
        is False
    )
    assert (
        service._requires_route_refresh([ItineraryPatchOperation(op="confirm_poi_anchor", segmentId="seg_1")]) is False
    )
    assert (
        service._requires_route_refresh([ItineraryPatchOperation(op="refresh_ticket_for_segment", segmentId="seg_1")])
        is False
    )
    assert (
        service._requires_route_refresh([ItineraryPatchOperation(op="expand_meal_poi_candidates", segmentId="seg_1")])
        is False
    )
    assert service._requires_route_refresh([ItineraryPatchOperation(op="replace_trip_title", value="新标题")]) is False


def test_simple_open_route_assignment_survives_time_edits_but_not_pair_identity_changes():
    service = ItineraryPatchService(sqlite3.connect(":memory:"))

    assert (
        service._invalidates_simple_open_route_assignment(
            [ItineraryPatchOperation(op="replace_segment_start_time", segmentId="seg_1", value="10:00")]
        )
        is False
    )
    assert (
        service._invalidates_simple_open_route_assignment(
            [ItineraryPatchOperation(op="replace_segment_duration", segmentId="seg_1", value="90")]
        )
        is False
    )
    for operation in (
        "replace_itinerary",
        "replace_segment_poi",
        "replace_segment_poi_from_candidate",
        "remove_segment",
        "move_segment",
        "reorder_segments",
        "replace_transport_mode",
        "refresh_routes_for_day",
    ):
        assert service._invalidates_simple_open_route_assignment([ItineraryPatchOperation(op=operation)]) is True


def test_patch_service_reads_explicit_route_mode_from_planning_context():
    service = ItineraryPatchService(sqlite3.connect(":memory:"))

    assert (
        service._preferred_transport_mode(
            [ItineraryPatchOperation(op="refresh_routes_for_day", dayId="day_patch")],
            {"preferredRouteMode": "driving", "includeModes": ["driving", "taxi"]},
        )
        == "driving"
    )
    assert (
        service._preferred_transport_mode(
            [ItineraryPatchOperation(op="refresh_routes_for_day", dayId="day_patch")],
            {"includeModes": ["taxi"]},
        )
        == "taxi"
    )


def test_patch_service_local_agent_context_defaults_to_touched_route_refresh():
    service = ItineraryPatchService(sqlite3.connect(":memory:"))

    assert (
        service._route_refresh_policy({"agentPlan": {"taskType": "local_modification"}}, "agent")
        == "touched_pairs_only"
    )


def test_agent_patch_rejects_late_cancel_after_irreversible_write_fence():
    clear_database()
    cancel_results: list[bool] = []
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="取消栅栏")
        seed_timeline(connection, session.active_plan_id)
        assert acquire_session_run(session.session_id) is True
        try:
            result = ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_trip_title", value="安全写入已完成")],
                source_type="agent",
                planning_context={
                    "sessionId": session.session_id,
                    "toolRefreshPolicy": {"route": "skip"},
                    "timelineMutationFailureInjector": lambda stage: (
                        cancel_results.append(request_session_run_cancel(session.session_id))
                        if stage == "after core patch"
                        else None
                    ),
                },
            )
            assert_session_run_active(session.session_id)
        finally:
            release_session_run(session.session_id)
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert cancel_results == [False]
    assert result.itinerary.title == "安全写入已完成"
    assert active_version_id == result.version.id
    assert patch_count == 1


def test_agent_patch_cancelled_before_write_fence_is_zero_write():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="取消前置")
        seed_timeline(connection, session.active_plan_id)
        baseline_title = connection.execute(
            "SELECT title FROM itinerary_plans WHERE id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        assert acquire_session_run(session.session_id) is True
        try:
            assert request_session_run_cancel(session.session_id) is True
            with pytest.raises(HTTPException) as error:
                ItineraryPatchService(connection).apply_patch(
                    session.active_plan_id,
                    [ItineraryPatchOperation(op="replace_trip_title", value="不得写入")],
                    source_type="agent",
                    planning_context={
                        "sessionId": session.session_id,
                        "toolRefreshPolicy": {"route": "skip"},
                    },
                )
        finally:
            release_session_run(session.session_id)
        title = connection.execute(
            "SELECT title FROM itinerary_plans WHERE id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert error.value.status_code == 499
    assert title == baseline_title
    assert active_version_id is None
    assert patch_count == 0
    assert version_count == 0


def test_agent_route_optimization_cancelled_before_write_fence_is_zero_write():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="取消路线优化")
        seed_timeline(connection, session.active_plan_id)
        baseline_segments = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, start_time, end_time FROM itinerary_segments ORDER BY id"
            ).fetchall()
        ]
        assert acquire_session_run(session.session_id) is True
        try:
            assert request_session_run_cancel(session.session_id) is True
            with pytest.raises(HTTPException) as error:
                ItineraryPatchService(connection).optimize_routes(
                    session.active_plan_id,
                    source_type="agent",
                    source_turn_id="turn_cancelled_route_optimization",
                    planning_context={
                        "sessionId": session.session_id,
                        "scheduleOnly": True,
                    },
                )
        finally:
            release_session_run(session.session_id)
        resulting_segments = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, start_time, end_time FROM itinerary_segments ORDER BY id"
            ).fetchall()
        ]
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert error.value.status_code == 499
    assert error.value.detail == "agent_run_cancelled"
    assert resulting_segments == baseline_segments
    assert active_version_id is None
    assert patch_count == 0
    assert version_count == 0


def test_agent_route_optimization_fences_before_optimizer_mutation(monkeypatch):
    clear_database()
    cancel_results: list[bool] = []
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="路线优化栅栏")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        connection.execute(
            "UPDATE pois SET amap_id = 'B000A8UIN9' WHERE id = 'poi_patch_2'",
        )
        ItineraryService(connection)._insert_route(
            RouteOption(
                id="route_agent_fence",
                plan_id=session.active_plan_id,
                from_segment_id="seg_patch",
                to_segment_id="seg_patch_2",
                from_poi_id="poi_patch",
                to_poi_id="poi_patch_2",
                provider="amap-webservice",
                mode="walking",
                is_selected=True,
                distance_meters=900,
                duration_seconds=600,
                polyline=[[116.397026, 39.918058], [116.3969, 39.9236]],
                steps=[{"mode": "walking", "distance": 900}],
                provider_payload={
                    "status": "1",
                    "walkingDistanceMeters": 900,
                    "transferCount": 0,
                    "waitSeconds": 0,
                    "riskPenaltyMinutes": 0,
                },
                queried_at=datetime.now(timezone.utc),
            )
        )
        connection.commit()

        def observe_write_fence(_self, _plan_id, **_kwargs):
            cancel_results.append(request_session_run_cancel(session.session_id))
            return RouteOptimizationResult()

        monkeypatch.setattr(
            RouteOptimizationService,
            "optimize_plan_routes",
            observe_write_fence,
        )
        assert acquire_session_run(session.session_id) is True
        try:
            result = ItineraryPatchService(connection).optimize_routes(
                session.active_plan_id,
                base_version_id=base_version_id,
                source_type="agent",
                source_turn_id="turn_route_optimization",
                planning_context={"sessionId": session.session_id},
            )
            assert_session_run_active(session.session_id)
        finally:
            release_session_run(session.session_id)
        patch_row = connection.execute(
            "SELECT source_type, source_turn_id FROM itinerary_patches WHERE id = ?",
            (result.patch.id,),
        ).fetchone()

    assert cancel_results == [False]
    assert patch_row["source_type"] == "agent"
    assert patch_row["source_turn_id"] == "turn_route_optimization"


def test_agent_patch_cancelled_before_rejected_patch_audit_is_zero_write():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="取消非法写入")
        seed_timeline(connection, session.active_plan_id)
        assert acquire_session_run(session.session_id) is True
        try:
            assert request_session_run_cancel(session.session_id) is True
            with pytest.raises(HTTPException) as error:
                ItineraryPatchService(connection).apply_patch(
                    session.active_plan_id,
                    [ItineraryPatchOperation(op="remove_segment", segmentId="missing_segment")],
                    source_type="agent",
                    planning_context={
                        "sessionId": session.session_id,
                        "toolRefreshPolicy": {"route": "skip"},
                    },
                )
        finally:
            release_session_run(session.session_id)
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert error.value.status_code == 499
    assert error.value.detail == "agent_run_cancelled"
    assert patch_count == 0
    assert version_count == 0


def test_agent_patch_cancelled_before_prepare_failure_audit_is_zero_write(monkeypatch):
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="取消准备失败")
        seed_timeline(connection, session.active_plan_id)
        service = ItineraryPatchService(connection)

        def fail_prepare(*_args, **_kwargs):
            raise HTTPException(status_code=422, detail="prepare failed")

        monkeypatch.setattr(service, "_prepare_poi_candidate_expansions", fail_prepare)
        assert acquire_session_run(session.session_id) is True
        try:
            assert request_session_run_cancel(session.session_id) is True
            with pytest.raises(HTTPException) as error:
                service.apply_patch(
                    session.active_plan_id,
                    [ItineraryPatchOperation(op="replace_trip_title", value="不得写入")],
                    source_type="agent",
                    planning_context={
                        "sessionId": session.session_id,
                        "toolRefreshPolicy": {"route": "skip"},
                    },
                )
        finally:
            release_session_run(session.session_id)
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert error.value.status_code == 499
    assert error.value.detail == "agent_run_cancelled"
    assert patch_count == 0
    assert version_count == 0


def test_patch_service_rejects_shopping_complex_as_meal_poi():
    clear_database()
    mall = MapPoiResponse(
        id="B0MALL",
        name="中关村ARTPARK大融城",
        type="购物服务;商场;商场",
        city="北京市",
        district="海淀区",
        address="中关村大街",
        longitude=116.315,
        latitude=39.986,
        category="shopping",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索",
        confidence=0.95,
    )
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="餐饮校验")
        seed_timeline(connection, session.active_plan_id)
        connection.execute("UPDATE itinerary_segments SET kind = 'meal' WHERE id = 'seg_patch_2'")
        errors = ItineraryPatchService(connection)._validate(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_segment_poi", segmentId="seg_patch_2", amapPoi=mall)],
        )

    assert errors == ["Meal segments must use a food-service AMap POI, not a shopping complex"]
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="新增餐饮校验")
        seed_timeline(connection, session.active_plan_id)
        errors = ItineraryPatchService(connection)._validate(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="add_segment",
                    dayId="day_patch",
                    kind="meal",
                    amapPoi=mall,
                    startTime="15:00",
                    durationMinutes=45,
                )
            ],
        )

    assert errors == ["Meal segments must use a food-service AMap POI, not a shopping complex"]


def test_add_segment_uses_structured_intent_type_without_poi_name_regex():
    operation = ItineraryPatchOperation(
        op="add_segment",
        dayId="day_patch",
        kind="visit",
        intentType="campus_visit",
        title="University of Foo",
        notes="model-owned semantic intent",
        amapPoi=MapPoiResponse(
            id="B0UNIVERSITY",
            name="University of Foo",
            type="Education;University",
            city="Beijing",
            district="Haidian",
            address="Test Road",
            longitude=116.3,
            latitude=40.0,
            category="campus",
            source="amap-place-search",
            sourceNote="recorded fixture",
            confidence=0.95,
        ),
    )

    metadata = ItineraryPatchService._new_segment_semantic_metadata(operation, "visit", operation.notes)

    assert metadata["intentType"] == "campus_visit"
    assert metadata["required"] is True


def test_replace_restaurant_with_campus_cafeteria_reestimates_duration_and_cost(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    cafeteria = MapPoiResponse(
        id="B0CAFETERIA",
        name="清华大学学生食堂",
        type="餐饮服务;中餐厅;校园食堂",
        city="北京市",
        district="海淀区",
        address="清华园",
        longitude=116.326,
        latitude=40.003,
        category="campus_cafeteria",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索",
        confidence=0.95,
    )
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="食堂替换")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        connection.execute(
            "UPDATE itinerary_segments SET kind = 'meal', start_time = '12:00', end_time = '13:15', estimated_cost = 120, estimate_metadata_json = '{}' WHERE id = 'seg_patch_2'"
        )
        response = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_segment_poi", segmentId="seg_patch_2", amapPoi=cafeteria)],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={
                "understoodRequirements": {"fields": {"budget": "medium"}},
                "latestUserMessage": "中等预算，改成校园食堂",
                "toolRefreshPolicy": {"route": "skip"},
            },
        )
        row = connection.execute(
            "SELECT start_time, end_time, estimated_cost, estimate_metadata_json FROM itinerary_segments WHERE id = 'seg_patch_2'"
        ).fetchone()

    metadata = json.loads(row["estimate_metadata_json"])
    assert (
        ItineraryPatchService(sqlite3.connect(":memory:"))._minutes(row["end_time"])
        - ItineraryPatchService(sqlite3.connect(":memory:"))._minutes(row["start_time"])
        == 45
    )
    assert row["estimated_cost"] < 120
    assert metadata["duration"]["preferredMinutes"] == 45
    assert metadata["duration"]["userLocked"] is False
    assert metadata["cost"]["budgetTier"] == "medium"
    assert metadata["cost"]["provisional"] is True
    assert response.patch.metadata["scheduleUpdatedCount"] == 1
    assert metadata["schedule"]["status"] == "committed_route_evidence"


def test_patch_service_refresh_routes_for_day_targets_all_adjacent_day_legs(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="刷新路线")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        poi_2 = _amap_poi_fixture("景山公园", 116.397, 39.918)
        ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="add_segment",
                    dayId="day_patch",
                    amapPoi=poi_2,
                    startTime="14:30",
                    durationMinutes=60,
                )
            ],
            source_type="manual",
            base_version_id=base_version_id,
            planning_context={"skipOptionalToolRefresh": True},
        )
        service = ItineraryPatchService(connection)
        scope = service._route_scope_before(
            session.active_plan_id,
            [ItineraryPatchOperation(op="refresh_routes_for_day", dayId="day_patch")],
        )
        route_pairs = service._route_pairs_after(session.active_plan_id, scope)
        ordered_ids = [row["id"] for row in service._segments_for_day(session.active_plan_id, "day_patch")]
        expected_pairs = set(zip(ordered_ids, ordered_ids[1:]))

    assert route_pairs == expected_pairs
    assert len(route_pairs) == 2


def test_patch_service_rejects_agent_text_timeline_coordinates():
    service = ItineraryPatchService(sqlite3.connect(":memory:"))

    assert (
        service._validate_snapshot_poi(
            {
                "name": "待定景点",
                "source": "agent-text-timeline",
                "longitude": None,
                "latitude": None,
                "confidence": 0.5,
            }
        )
        == ""
    )
    assert (
        service._validate_snapshot_poi(
            {
                "name": "待定景点",
                "source": "agent-text-timeline",
                "longitude": 0,
                "latitude": 0,
            }
        )
        == "Agent text timeline POI coordinates must stay empty until map grounding"
    )


def seed_timeline(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    with_route_contract: bool = False,
) -> str | None:
    connection.execute(
        """
        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude, photo_url,
            source, confidence, amap_id, type, district, address, source_note,
            source_url, photos_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "poi_patch",
            plan_id,
            "故宫博物院",
            "北京",
            "scenic",
            39.918058,
            116.397026,
            None,
            "amap-place-search",
            0.91,
            "B000A8UIN8",
            "风景名胜",
            "东城区",
            "景山前街4号",
            "高德 WebService POI 搜索",
            None,
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO itinerary_days (
            id, plan_id, day_number, date, title, weather_summary,
            risk_summary, total_estimated_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("day_patch", plan_id, 1, None, "初始 Day", "晴", "低风险", 60),
    )
    connection.execute(
        """
        INSERT INTO itinerary_segments (
            id, plan_id, day_id, segment_order, kind, start_time, end_time,
            poi_id, transport_mode, estimated_cost, notes,
            weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "seg_patch",
            plan_id,
            "day_patch",
            1,
            "activity",
            "09:00",
            "11:00",
            "poi_patch",
            "walk",
            60,
            "初始说明",
            None,
            None,
            None,
        ),
    )
    connection.execute(
        """
        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude, photo_url,
            source, confidence, amap_id, type, district, address, source_note,
            source_url, photos_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "poi_patch_2",
            plan_id,
            "景山公园",
            "北京",
            "scenic",
            39.9236,
            116.3969,
            None,
            "amap-place-search",
            0.9,
            "B0景山",
            "风景名胜",
            "西城区",
            "景山西街",
            "高德 WebService POI 搜索",
            None,
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO itinerary_segments (
            id, plan_id, day_id, segment_order, kind, start_time, end_time,
            poi_id, transport_mode, estimated_cost, notes,
            weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "seg_patch_2",
            plan_id,
            "day_patch",
            2,
            "activity",
            "12:30",
            "13:30",
            "poi_patch_2",
            "walk",
            10,
            "第二段说明",
            None,
            None,
            None,
        ),
    )
    connection.commit()
    if with_route_contract:
        return _seed_server_route_contract(connection, plan_id)
    return None


def _seed_server_route_contract(connection: sqlite3.Connection, plan_id: str) -> str:
    """Give successful writer fixtures the same accepted-version carrier as production."""
    session = connection.execute("SELECT id FROM conversation_sessions WHERE active_plan_id = ?", (plan_id,)).fetchone()
    assert session is not None
    snapshots = ItinerarySnapshotService(connection)
    snapshot = snapshots.capture_snapshot(plan_id)
    snapshot["routeDecisionContract"] = deepcopy(_route_decision_contract_context()["routeDecisionContract"])
    version = snapshots.save_version(str(session["id"]), plan_id, "fixture", snapshot=snapshot)
    connection.commit()
    return version.id


def add_third_timeline_segment(connection: sqlite3.Connection, plan_id: str) -> None:
    connection.execute(
        """
        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude, photo_url,
            source, confidence, amap_id, type, district, address, source_note,
            source_url, photos_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "poi_patch_3",
            plan_id,
            "北海公园",
            "北京",
            "scenic",
            39.9301,
            116.3901,
            None,
            "amap-place-search",
            0.9,
            "B0北海",
            "风景名胜",
            "西城区",
            "文津街",
            "高德 WebService POI 搜索",
            None,
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO itinerary_segments (
            id, plan_id, day_id, segment_order, kind, start_time, end_time,
            poi_id, transport_mode, estimated_cost, notes,
            weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "seg_patch_3",
            plan_id,
            "day_patch",
            3,
            "activity",
            "15:00",
            "16:00",
            "poi_patch_3",
            "walk",
            10,
            "第三段说明",
            None,
            None,
            None,
        ),
    )
    connection.commit()


def test_patch_service_applies_manual_edits_and_records_patch_and_version(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(op="replace_trip_title", value="北京轻松 2 日游"),
                ItineraryPatchOperation(op="replace_day_title", dayId="day_patch", value="故宫与胡同"),
                ItineraryPatchOperation(op="replace_segment_start_time", segmentId="seg_patch", value="10:00"),
            ],
            base_version_id=base_version_id,
        )
        patch_count = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        patch = connection.execute("SELECT * FROM itinerary_patches").fetchone()
        active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()

    assert result.itinerary.title == "北京轻松 2 日游"
    assert result.itinerary.days[0].title == "故宫与胡同"
    assert result.itinerary.days[0].segments[0].start_time == "10:00"
    assert result.itinerary.days[0].segments[0].end_time == "12:00"
    assert result.itinerary.days[0].total_estimated_cost == 70
    assert result.itinerary.budget_estimate == 70
    assert patch_count == 1
    assert version_count == 2
    assert patch["validation_status"] == "accepted"
    assert patch["result_version_id"] == result.version.id
    assert active["active_version_id"] == result.version.id


def test_patch_service_rejects_invalid_segment_time_without_new_version():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)

        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_segment_start_time", segmentId="seg_patch", value="25:00")],
            )
        patch = connection.execute("SELECT * FROM itinerary_patches").fetchone()
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert error.value.status_code == 400
    assert patch["validation_status"] == "rejected"
    assert version_count == 0


def test_patch_service_rejects_time_conflict_without_new_version():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)

        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_segment_start_time", segmentId="seg_patch", value="12:00")],
            )
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert error.value.status_code == 400
    assert version_count == 0


def test_patch_service_add_day_creates_version():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="add_day", title="第二天轻松安排")],
        )

    assert len(result.itinerary.days) == 2
    assert result.itinerary.days[1].day_number == 2
    assert result.itinerary.days[1].title == "第二天轻松安排"
    assert result.version.version_number == 1


def test_patch_service_add_day_and_move_segment_placeholder_is_atomic(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(op="add_day", title="新增轻松安排"),
                ItineraryPatchOperation(
                    op="move_segment",
                    segmentId="seg_patch_2",
                    targetDayId="__new_day__",
                    startTime="09:30",
                ),
            ],
            base_version_id=base_version_id,
        )
        moved_row = connection.execute(
            "SELECT day_id, start_time, end_time, segment_order FROM itinerary_segments WHERE id = ?",
            ("seg_patch_2",),
        ).fetchone()
        new_day = connection.execute(
            "SELECT id, title FROM itinerary_days WHERE plan_id = ? AND day_number = 2",
            (session.active_plan_id,),
        ).fetchone()

    assert result.itinerary.days[1].title == "新增轻松安排"
    assert result.itinerary.days[1].segments[0].id == "seg_patch_2"
    assert result.itinerary.days[1].segments[0].start_time == "09:30"
    assert result.itinerary.days[1].segments[0].end_time == "10:30"
    assert moved_row["day_id"] == new_day["id"]
    assert moved_row["start_time"] == "09:30"
    assert moved_row["end_time"] == "10:30"
    assert moved_row["segment_order"] == 1


def test_patch_service_rejects_new_day_placeholder_without_preceding_add_day():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)

        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="move_segment",
                        segmentId="seg_patch_2",
                        targetDayId="__new_day__",
                        startTime="09:30",
                    ),
                ],
            )

    assert error.value.status_code == 400
    assert "move_segment targetDayId __new_day__ requires a preceding add_day" in error.value.detail["validationErrors"]


def test_patch_service_add_segment_without_resolved_amap_poi_is_rejected():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="add_segment",
                        dayId="day_patch",
                        startTime="14:30",
                        title="待定咖啡馆",
                        notes="下午休息",
                        durationMinutes=45,
                    )
                ],
            )
        patch = connection.execute("SELECT * FROM itinerary_patches").fetchone()
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert error.value.status_code == 400
    assert patch["validation_status"] == "rejected"
    assert version_count == 0


def test_patch_service_add_segment_allows_unresolved_meal_placeholder_only_when_explicit():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="add_segment",
                    dayId="day_patch",
                    startTime="14:30",
                    title="晚餐 当地特色美食",
                    kind="meal",
                    notes="groundingStatus：waiting_for_poi_grounding；intentType=meal；高德 POI 待校验",
                    durationMinutes=75,
                    estimatedCost=120,
                    transportMode="public_transit",
                    allowUnresolved=True,
                )
            ],
            source_type="agent",
            planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
        )
        added_poi = connection.execute(
            "SELECT p.* FROM pois p JOIN itinerary_segments s ON s.poi_id = p.id WHERE s.kind = 'meal'"
        ).fetchone()
        semantic_metadata = json.loads(
            connection.execute("SELECT semantic_metadata_json FROM itinerary_segments WHERE kind = 'meal'").fetchone()[
                0
            ]
        )

    added = next(segment for segment in result.itinerary.days[0].segments if segment.kind == "meal")
    assert added.start_time == "14:30"
    assert added.end_time == "15:45"
    assert added.estimated_cost == 120
    assert added.transport_mode == "public_transit"
    assert "waiting_for_poi_grounding" in added.notes
    assert added_poi["source"] == "itinerary-skeleton"
    assert added_poi["category"] == "meal"
    assert "waiting_for_poi_grounding" in added_poi["source_note"]
    assert semantic_metadata["intentType"] == "meal"
    assert semantic_metadata["groundingStatus"] == "waiting_for_poi_grounding"
    assert semantic_metadata["routeAnchor"] is False
    assert result.version.version_number == 1


def test_patch_service_add_segment_persists_resolved_amap_poi_fields(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    source_claims = [
        {
            "claimKey": "local_food",
            "stance": "support",
            "locality": "北京",
            "summary": "北京本地餐饮证据",
        }
    ]
    structured_poi = amap_poi_fixture().model_copy(
        update={
            "provider_type_code": "050100",
            "tags": ["地方风味", "咖啡厅"],
            "source_claims": source_claims,
        }
    )
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="add_segment",
                    dayId="day_patch",
                    startTime="14:30",
                    title="故宫旁咖啡馆",
                    notes="下午休息",
                    durationMinutes=45,
                    amapPoi=structured_poi,
                )
            ],
            base_version_id=base_version_id,
        )
        added_poi = connection.execute(
            "SELECT * FROM pois WHERE amap_id = ?",
            ("B000COFFEE",),
        ).fetchone()
        semantic_metadata = json.loads(
            connection.execute(
                "SELECT semantic_metadata_json FROM itinerary_segments WHERE poi_id = ?", (added_poi["id"],)
            ).fetchone()[0]
        )
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.version.id,),
            ).fetchone()[0]
        )

    added = result.itinerary.days[0].segments[2]
    snapshot_poi = snapshot["days"][0]["segments"][2]["poi"]
    assert added.start_time == "12:30"
    assert added.end_time == "13:15"
    assert added.poi.name == "故宫旁咖啡馆"
    assert added.poi.source == "amap-place-search"
    assert added.notes == "下午休息"
    assert added_poi["amap_id"] == "B000COFFEE"
    assert added_poi["type"] == "餐饮服务;咖啡厅"
    assert added_poi["district"] == "东城区"
    assert added_poi["address"] == "景山前街附近"
    assert added_poi["confidence"] == 0.91
    assert "coffee.jpg" in added_poi["photos_json"]
    assert added_poi["provider_type_code"] == "050100"
    assert json.loads(added_poi["tags_json"]) == ["地方风味", "咖啡厅"]
    assert json.loads(added_poi["source_claims_json"]) == source_claims
    assert added.poi.provider_type_code == "050100"
    assert added.poi.tags == ["地方风味", "咖啡厅"]
    assert added.poi.source_claims == source_claims
    assert snapshot_poi["providerTypeCode"] == "050100"
    assert snapshot_poi["tags"] == ["地方风味", "咖啡厅"]
    assert snapshot_poi["sourceClaims"] == source_claims
    assert semantic_metadata["intentType"] == "meal"
    assert semantic_metadata["groundingStatus"] == "verified_amap"
    assert semantic_metadata["routeAnchor"] is True
    assert result.version.version_number == 2


def test_patch_service_confirm_poi_anchor_rejects_already_verified_exact_anchor():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="确认锚点")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE pois
            SET source = ?, confidence = ?, source_note = ?
            WHERE id = 'poi_patch'
            """,
            (
                "agent-text-timeline",
                0.4,
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。地图锚点：故宫博物院；高德 POI 待校验",
            ),
        )
        connection.commit()

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="confirm_poi_anchor", segmentId="seg_patch")],
            )

    assert exc_info.value.status_code == 400
    assert "confirm_poi_anchor only supports exact routeable anchors" in str(exc_info.value.detail)


def test_patch_service_confirm_poi_anchor_rejects_area_or_composite_anchor():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="拒绝区域锚点确认")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, source = ?, confidence = ?, source_note = ?
            WHERE id = 'poi_patch'
            """,
            (
                "奥林匹克公园夜景",
                "agent-text-timeline",
                0.4,
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。POI意图：composite_poi；地图锚点：奥林匹克公园；高德 POI 待校验",
            ),
        )
        connection.commit()

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="confirm_poi_anchor", segmentId="seg_patch")],
            )
        row = connection.execute("SELECT source, confidence FROM pois WHERE id = 'poi_patch'").fetchone()

    assert exc_info.value.status_code == 400
    assert "area/function POIs must be replaced" in str(exc_info.value.detail)
    assert row["source"] == "agent-text-timeline"
    assert row["confidence"] == 0.4


def test_patch_service_expand_area_poi_candidates_records_pending_candidates(monkeypatch):
    clear_database()
    fake_service = _FakeNearbyMapService([amap_poi_fixture()])
    monkeypatch.setattr("src.services.itinerary_patch_service.MapPoiService", lambda: fake_service)
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="展开区域候选")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, source = ?, confidence = ?, source_note = ?
            WHERE id = 'poi_patch'
            """,
            (
                "奥林匹克公园夜景",
                "agent-text-timeline",
                0.4,
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。POI意图：composite_poi；地图锚点：奥林匹克公园；建议选择具体场馆/入口/区域后再查询票务预约。",
            ),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="expand_area_poi_candidates", segmentId="seg_patch", radius=1500)],
        )
        row = connection.execute(
            "SELECT * FROM amap_poi_candidates WHERE session_id = ?", (session.session_id,)
        ).fetchone()

    assert fake_service.calls[0]["keyword"] == "奥林匹克公园"
    assert fake_service.calls[0]["radius"] == 1500
    assert row["status"] == "pending"
    assert row["query"] == "奥林匹克公园夜景"
    assert row["segment_id"] == "seg_patch"
    assert json.loads(row["candidates_json"])[0]["id"] == "B000COFFEE"
    assert result.pending_poi_candidates[0].id == row["id"]
    assert result.pending_poi_candidates[0].source_segment_id == "seg_patch"


def test_patch_service_expand_area_poi_failure_records_rejected_patch_without_pending_candidate(monkeypatch):
    clear_database()

    class FailingNearbyMapService:
        def search_nearby(self, **_kwargs):
            raise HTTPException(status_code=502, detail="AMap nearby failed")

    monkeypatch.setattr("src.services.itinerary_patch_service.MapPoiService", lambda: FailingNearbyMapService())
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="候选失败")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, source = ?, confidence = ?, source_note = ?
            WHERE id = 'poi_patch'
            """,
            (
                "奥林匹克公园夜景",
                "agent-text-timeline",
                0.4,
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。POI意图：composite_poi；地图锚点：奥林匹克公园；建议选择具体场馆/入口/区域后再查询票务预约。",
            ),
        )
        connection.commit()

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="expand_area_poi_candidates", segmentId="seg_patch", radius=1500)],
            )
        patch = connection.execute(
            "SELECT validation_status, validation_errors_json FROM itinerary_patches WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()
        pending_count = connection.execute(
            "SELECT COUNT(*) AS count FROM amap_poi_candidates WHERE session_id = ?", (session.session_id,)
        ).fetchone()["count"]

    assert exc_info.value.status_code == 502
    assert patch["validation_status"] == "rejected"
    assert "AMap nearby failed" in patch["validation_errors_json"]
    assert pending_count == 0


def test_patch_service_expand_functional_poi_uses_adjacent_routeable_anchor(monkeypatch):
    clear_database()
    fake_service = _FakeNearbyMapService([amap_poi_fixture()])
    monkeypatch.setattr("src.services.itinerary_patch_service.MapPoiService", lambda: fake_service)
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="功能型候选")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE pois
            SET name = ?, source = ?, confidence = ?, amap_id = NULL, latitude = NULL, longitude = NULL, source_note = ?
            WHERE id = 'poi_patch_2'
            """,
            ("午餐", "agent-text-timeline", 0.35, "高德 POI 待校验"),
        )
        connection.commit()

        ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="expand_area_poi_candidates", segmentId="seg_patch_2", radius=1000)],
        )

    assert fake_service.calls[0]["keyword"] == "餐厅"
    assert fake_service.calls[0]["category"] == "food"
    assert fake_service.calls[0]["longitude"] == pytest.approx(116.397026)
    assert fake_service.calls[0]["latitude"] == pytest.approx(39.918058)


def test_patch_service_expand_meal_poi_candidates_records_food_pending_candidates(monkeypatch):
    clear_database()
    hotel_candidate = MapPoiResponse(
        id="B000HOTEL",
        name="景山附近酒店",
        type="住宿服务;宾馆酒店",
        city="北京市",
        district="东城区",
        address="景山前街附近",
        longitude=116.399,
        latitude=39.920,
        category="lodging",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索",
        confidence=0.91,
        photos=[],
    )
    fake_service = _FakeNearbyMapService([amap_poi_fixture(), hotel_candidate])
    monkeypatch.setattr("src.services.itinerary_patch_service.MapPoiService", lambda: fake_service)
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="展开午餐候选")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE itinerary_segments
            SET kind = ?, estimated_cost = ?, notes = ?
            WHERE id = 'seg_patch_2'
            """,
            ("meal", 80, "午餐时间，餐厅可稍后搜索。"),
        )
        connection.execute(
            """
            UPDATE pois
            SET name = ?, category = ?, type = ?, source = ?, confidence = ?, amap_id = NULL,
                latitude = NULL, longitude = NULL, source_note = ?
            WHERE id = 'poi_patch_2'
            """,
            (
                "午餐",
                "meal",
                "餐饮时间",
                "agent-text-timeline",
                0.35,
                "午餐时间，餐厅可稍后搜索。groundingStatus：not_required；routeAnchor=false。",
            ),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="expand_meal_poi_candidates", segmentId="seg_patch_2", radius=2200)],
        )
        row = connection.execute(
            "SELECT * FROM amap_poi_candidates WHERE session_id = ?", (session.session_id,)
        ).fetchone()
        candidates = json.loads(row["candidates_json"])

    assert fake_service.calls[0]["keyword"] == "餐厅"
    assert fake_service.calls[0]["category"] == "food"
    assert fake_service.calls[0]["radius"] == 2200
    assert fake_service.calls[0]["longitude"] == pytest.approx(116.397026)
    assert fake_service.calls[0]["latitude"] == pytest.approx(39.918058)
    assert row["status"] == "pending"
    assert row["category"] == "food"
    assert row["segment_id"] == "seg_patch_2"
    assert [candidate["id"] for candidate in candidates] == ["B000COFFEE"]
    assert candidates[0]["routeImpact"]["previousAnchor"] == "故宫博物院"
    assert candidates[0]["routeImpact"]["searchMode"] == "near_previous_anchor"
    assert candidates[0]["routeImpact"]["detourLevel"] in {"low", "medium"}
    assert candidates[0]["routeImpact"]["networkVerified"] is False
    assert candidates[0]["routeImpact"]["decisionRole"] == "geometry_coarse_ordering_only"
    assert "实际路线待 Provider 核验" in candidates[0]["reason"]
    assert "顺路" not in candidates[0]["reason"]
    assert result.pending_poi_candidates[0].source_segment_id == "seg_patch_2"


def test_patch_service_expand_meal_poi_candidates_excludes_previous_pending_candidates(monkeypatch):
    clear_database()
    first_candidate = amap_poi_fixture()
    second_candidate = MapPoiResponse(
        id="B000NOODLE",
        name="顺路面馆",
        type="餐饮服务;中餐厅",
        city="北京市",
        district="东城区",
        address="景山前街旁",
        longitude=116.3985,
        latitude=39.9193,
        category="food",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索",
        confidence=0.9,
        photos=[],
    )
    fake_service = _FakeNearbyMapService([first_candidate, second_candidate])
    monkeypatch.setattr("src.services.itinerary_patch_service.MapPoiService", lambda: fake_service)
    with open_db() as connection:
        session = ConversationService(connection).create_session(city="北京", title="重复午餐候选")
        seed_timeline(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE itinerary_segments
            SET kind = ?, estimated_cost = ?, notes = ?
            WHERE id = 'seg_patch_2'
            """,
            ("meal", 80, "午餐时间，餐厅可稍后搜索。"),
        )
        connection.execute(
            """
            UPDATE pois
            SET name = ?, category = ?, type = ?, source = ?, confidence = ?, amap_id = NULL,
                latitude = NULL, longitude = NULL, source_note = ?
            WHERE id = 'poi_patch_2'
            """,
            (
                "午餐",
                "meal",
                "餐饮时间",
                "agent-text-timeline",
                0.35,
                "groundingStatus：optional_waiting；routeAnchor=false。",
            ),
        )
        service = ItineraryPatchService(connection)
        first_result = service.apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="expand_meal_poi_candidates", segmentId="seg_patch_2", radius=2200)],
        )
        with pytest.raises(HTTPException) as error:
            service.apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="expand_meal_poi_candidates", segmentId="seg_patch_2", radius=2200)],
                base_version_id=first_result.version.id,
            )
        rows = connection.execute(
            "SELECT candidates_json FROM amap_poi_candidates WHERE session_id = ? ORDER BY created_at ASC",
            (session.session_id,),
        ).fetchall()

    assert error.value.status_code == 400
    assert "手动输入餐厅或扩大范围" in str(error.value.detail)
    assert len(rows) == 1


def test_patch_service_rejects_multiple_pending_candidate_ids_in_one_patch():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "多候选拒绝")
        seed_timeline(connection, session.active_plan_id)
        candidate_poi = amap_poi_fixture()
        for candidate_id in ["cand_one", "cand_two"]:
            connection.execute(
                """
                INSERT INTO amap_poi_candidates (
                    id, session_id, turn_id, query, segment_id, city, category, status,
                    candidates_json, selected_amap_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    session.session_id,
                    None,
                    "区域候选",
                    None,
                    "北京",
                    "scenic",
                    "pending",
                    json.dumps([candidate_poi.model_dump(by_alias=True)], ensure_ascii=False),
                    None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        connection.commit()

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi_from_candidate",
                        segmentId="seg_patch",
                        candidateId="cand_one",
                        amapPoi=candidate_poi,
                    ),
                    ItineraryPatchOperation(
                        op="replace_segment_poi_from_candidate",
                        segmentId="seg_patch_2",
                        candidateId="cand_two",
                        amapPoi=candidate_poi,
                    ),
                ],
                source_type="agent",
                planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
            )

    assert exc_info.value.status_code == 400
    assert "Only one pending POI candidate" in str(exc_info.value.detail)


def test_patch_service_replace_segment_poi_from_candidate_requires_candidate_membership(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "候选替换")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        candidate_id = "cand_test_replace"
        candidate_poi = amap_poi_fixture()
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session.session_id,
                None,
                "奥林匹克公园夜景",
                "seg_patch",
                "北京",
                "scenic",
                "pending",
                json.dumps([candidate_poi.model_dump(by_alias=True)], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi_from_candidate",
                        segmentId="seg_patch",
                        candidateId=candidate_id,
                        amapPoi=_amap_poi_fixture("错误候选", 116.41, 39.91),
                    )
                ],
                source_type="agent",
                base_version_id=base_version_id,
                planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
            )
        with pytest.raises(HTTPException) as segment_exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi_from_candidate",
                        segmentId="seg_patch_2",
                        candidateId=candidate_id,
                        amapPoi=candidate_poi,
                    )
                ],
                source_type="agent",
                base_version_id=base_version_id,
                planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
            )

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi_from_candidate",
                    segmentId="seg_patch",
                    candidateId=candidate_id,
                    amapPoi=candidate_poi,
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
        )
        row = connection.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    assert exc_info.value.status_code == 400
    assert "not part of the pending candidate set" in str(exc_info.value.detail)
    assert segment_exc_info.value.status_code == 400
    assert "does not belong to the target segment" in str(segment_exc_info.value.detail)
    assert row["status"] == "selected"
    assert row["selected_amap_id"] == "B000COFFEE"
    assert result.itinerary.days[0].segments[0].poi.name == "故宫旁咖啡馆"


def test_patch_service_pending_candidate_same_amap_selection_is_idempotent(monkeypatch):
    clear_database()
    provider_calls = _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "候选幂等")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        candidate_id = "cand_idempotent"
        candidate_poi = amap_poi_fixture()
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session.session_id,
                None,
                "故宫旁咖啡馆",
                "seg_patch",
                "北京",
                "food",
                "pending",
                json.dumps([candidate_poi.model_dump(by_alias=True)], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

        operation = ItineraryPatchOperation(
            op="replace_segment_poi_from_candidate",
            segmentId="seg_patch",
            candidateId=candidate_id,
            amapPoi=candidate_poi,
        )
        service = ItineraryPatchService(connection)
        before_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        )
        first = service.apply_patch(
            session.active_plan_id,
            [operation],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
        )
        after_first_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        )
        after_first_routes = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                       provider, distance_meters, duration_seconds, queried_at
                FROM route_options
                ORDER BY id
                """
            ).fetchall()
        ]
        after_first_provider_calls = len(provider_calls)
        second = service.apply_patch(
            session.active_plan_id,
            [operation],
            source_type="agent",
            base_version_id=first.version.id,
            planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
        )
        third = service.apply_patch(
            session.active_plan_id,
            [operation],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
        )
        after_second_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        )
        after_second_routes = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                       provider, distance_meters, duration_seconds, queried_at
                FROM route_options
                ORDER BY id
                """
            ).fetchall()
        ]
        row = connection.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    assert first.patch.validation_status == "accepted"
    assert second.patch.validation_status == "accepted"
    assert second.patch.id == first.patch.id
    assert second.version.id == first.version.id
    assert third.patch.id == first.patch.id
    assert third.version.id == first.version.id
    assert second.patch.metadata == {
        "idempotentReplay": True,
        "candidateId": candidate_id,
        "selectedAmapId": "B000COFFEE",
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
    }
    assert tuple(after_first - before for before, after_first in zip(before_counts, after_first_counts)) == (1, 1, 1)
    assert after_second_counts == after_first_counts
    assert after_second_routes == after_first_routes
    assert len(provider_calls) == after_first_provider_calls
    assert row["status"] == "selected"
    assert row["selected_amap_id"] == "B000COFFEE"


def test_public_poi_rebind_failure_does_not_create_session_for_orphan_plan():
    clear_database()

    class FailingDetailService:
        def detail(self, _amap_id: str):
            raise HTTPException(status_code=502, detail="recorded provider unavailable")

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "孤立行程身份核验")
        plan_id = session.active_plan_id
        connection.execute("DELETE FROM conversation_sessions WHERE id = ?", (session.session_id,))
        connection.commit()
        before_sessions = connection.execute("SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).rebind_public_amap_poi_identities(
                plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi",
                        segmentId="seg_orphan",
                        amapPoi=amap_poi_fixture(),
                    )
                ],
                map_poi_service=FailingDetailService(),
            )
        after_sessions = connection.execute("SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]

    assert exc_info.value.status_code == 502
    assert after_sessions == before_sessions == 0


def test_patch_service_pending_candidate_different_selected_amap_returns_conflict():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "候选冲突")
        seed_timeline(connection, session.active_plan_id)
        candidate_id = "cand_conflict"
        candidate_poi = amap_poi_fixture()
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session.session_id,
                None,
                "故宫旁咖啡馆",
                "seg_patch",
                "北京",
                "food",
                "selected",
                json.dumps([candidate_poi.model_dump(by_alias=True)], ensure_ascii=False),
                "B000OTHER",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi_from_candidate",
                        segmentId="seg_patch",
                        candidateId=candidate_id,
                        amapPoi=candidate_poi,
                    )
                ],
                source_type="agent",
                planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
            )

    assert exc_info.value.status_code == 409
    assert "different AMap POI" in str(exc_info.value.detail)


def test_patch_service_pending_candidate_claim_rolls_back_to_pending_on_failure(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)

    def fail_save_version(*_args, **_kwargs):
        raise RuntimeError("snapshot write failed")

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "候选回滚")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        monkeypatch.setattr(
            "src.services.itinerary_snapshot_service.ItinerarySnapshotService.save_version",
            fail_save_version,
        )
        candidate_id = "cand_rollback"
        candidate_poi = amap_poi_fixture()
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session.session_id,
                None,
                "故宫旁咖啡馆",
                "seg_patch",
                "北京",
                "food",
                "pending",
                json.dumps([candidate_poi.model_dump(by_alias=True)], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

        with pytest.raises(RuntimeError):
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi_from_candidate",
                        segmentId="seg_patch",
                        candidateId=candidate_id,
                        amapPoi=candidate_poi,
                    )
                ],
                source_type="agent",
                base_version_id=base_version_id,
                planning_context={"stagedPlanningPipeline": {"enabled": True}, "skipOptionalToolRefresh": True},
            )
        row = connection.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    assert row["status"] == "pending"
    assert row["selected_amap_id"] is None


def test_patch_service_replace_segment_poi_requires_and_persists_resolved_amap_poi(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)

        with pytest.raises(HTTPException):
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_segment_poi", segmentId="seg_patch")],
            )

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch",
                    notes="换成高德确认的咖啡馆",
                    amapPoi=amap_poi_fixture(),
                )
            ],
            base_version_id=base_version_id,
        )
        replaced_poi = connection.execute(
            "SELECT p.* FROM pois p JOIN itinerary_segments s ON s.poi_id = p.id WHERE s.id = ?",
            ("seg_patch",),
        ).fetchone()

    assert result.itinerary.days[0].segments[0].poi.name == "故宫旁咖啡馆"
    assert result.itinerary.days[0].segments[0].notes == "换成高德确认的咖啡馆"
    assert replaced_poi["amap_id"] == "B000COFFEE"
    assert replaced_poi["source"] == "amap-place-search"
    assert "groundingStatus：user_confirmed" in replaced_poi["source_note"]


def test_patch_service_rejects_agent_overwrite_of_user_confirmed_segment(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)

        manual_result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch",
                    amapPoi=amap_poi_fixture(),
                )
            ],
            source_type="manual",
            base_version_id=base_version_id,
        )

        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi",
                        segmentId="seg_patch",
                        amapPoi=amap_poi_fixture(),
                    )
                ],
                source_type="agent",
                base_version_id=manual_result.version.id,
            )

    assert "user_confirmed" in str(error.value.detail)


def test_patch_service_replace_segment_poi_clears_stale_ticket_result(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO ticket_lookup_results (
                id, segment_id, ticket_type, status, price_estimate, booking_url,
                source_name, source_url, credibility_rank, queried_at, caveat,
                provider_name, fallback_used, provider_failure_reason, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ticket_old_palace",
                "seg_patch",
                "reservation",
                "available",
                60,
                "https://old.example.com/palace",
                "旧故宫票务来源",
                "https://old.example.com/palace",
                "official",
                now,
                "旧票务来源",
                "bocha-web-search",
                0,
                None,
                0.9,
            ),
        )
        connection.execute(
            "UPDATE itinerary_segments SET ticket_lookup_result_id = ? WHERE id = ?",
            ("ticket_old_palace", "seg_patch"),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch",
                    notes="换成高德确认的咖啡馆",
                    amapPoi=amap_poi_fixture(),
                )
            ],
            base_version_id=base_version_id,
        )
        rows = connection.execute(
            "SELECT * FROM ticket_lookup_results WHERE segment_id = ? ORDER BY queried_at",
            ("seg_patch",),
        ).fetchall()
        segment = connection.execute(
            "SELECT ticket_lookup_result_id FROM itinerary_segments WHERE id = ?",
            ("seg_patch",),
        ).fetchone()

    assert all(row["id"] != "ticket_old_palace" for row in rows)
    assert all(row["source_url"] != "https://old.example.com/palace" for row in rows)
    assert segment["ticket_lookup_result_id"] is None
    replacement_ticket = next(
        (ticket for ticket in result.itinerary.ticket_lookup_results if ticket.segment_id == "seg_patch"), None
    )
    assert replacement_ticket is None


def test_patch_service_remove_segment_reorders_remaining_segments(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO route_options (
                id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                provider, mode, label, is_selected, sort_order, transport_mode,
                distance_meters, duration_seconds, duration_minutes, cost_amount,
                cost_currency, cost_estimate, crowding_risk, source, polyline_json,
                steps_json, provider_payload_json, error_json, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "route_removed_segment",
                session.active_plan_id,
                "seg_patch",
                "seg_patch_2",
                "poi_patch",
                "poi_patch_2",
                "test",
                "walking",
                "步行",
                1,
                1,
                "walking",
                1200,
                20 * 60,
                20,
                0,
                "CNY",
                0,
                "low",
                "test",
                "[[116,39],[116.1,39.1]]",
                "[]",
                "{}",
                None,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO traffic_crowding_signals (
                id, route_option_id, real_data_available, crowding_level,
                estimated_reason, recommended_departure_adjustment, source, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("traffic_removed_segment", "route_removed_segment", 0, "low", "test", "", "test", now),
        )
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="remove_segment", segmentId="seg_patch")],
            base_version_id=base_version_id,
            planning_context={"toolRefreshPolicy": {"route": "skip"}},
        )
        remaining = connection.execute(
            "SELECT id, segment_order FROM itinerary_segments WHERE plan_id = ? ORDER BY segment_order",
            (session.active_plan_id,),
        ).fetchall()
        route_count = connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE id = ?",
            ("route_removed_segment",),
        ).fetchone()[0]
        traffic_count = connection.execute(
            "SELECT COUNT(*) FROM traffic_crowding_signals WHERE id = ?",
            ("traffic_removed_segment",),
        ).fetchone()[0]

    assert [row["id"] for row in remaining] == ["seg_patch_2"]
    assert [row["segment_order"] for row in remaining] == [1]
    assert result.itinerary.days[0].segments[0].id == "seg_patch_2"
    assert result.itinerary.route_options == []
    assert route_count == 0
    assert traffic_count == 0


def test_patch_service_route_pairs_skip_pending_visit_anchor():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        add_third_timeline_segment(connection, session.active_plan_id)
        connection.execute(
            """
            UPDATE pois
            SET source = ?, amap_id = NULL, latitude = NULL, longitude = NULL,
                confidence = ?, source_note = ?
            WHERE id = ?
            """,
            (
                "agent-text-timeline",
                0.35,
                "groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true",
                "poi_patch_2",
            ),
        )
        service = ItineraryPatchService(connection)
        pairs = service._adjacent_pairs_for_days(session.active_plan_id, {"day_patch"})

    assert pairs == {("seg_patch", "seg_patch_3")}


def test_patch_service_move_segment_validates_target_day_ordering(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        initial_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        add_day_result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="add_day", title="第二天")],
            base_version_id=initial_version_id,
        )
        second_day_id = connection.execute(
            "SELECT id FROM itinerary_days WHERE plan_id = ? AND day_number = 2",
            (session.active_plan_id,),
        ).fetchone()["id"]
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="move_segment",
                    segmentId="seg_patch_2",
                    targetDayId=second_day_id,
                    startTime="09:00",
                )
            ],
            base_version_id=add_day_result.version.id,
        )

        with pytest.raises(HTTPException):
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="move_segment",
                        segmentId="seg_patch",
                        targetDayId=second_day_id,
                        startTime="09:15",
                    )
                ],
                base_version_id=result.version.id,
            )

    assert result.itinerary.days[1].segments[0].id == "seg_patch_2"
    assert result.itinerary.days[1].segments[0].start_time == "09:00"


def test_patch_service_update_segment_notes_persists():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="update_segment_notes", segmentId="seg_patch", notes="新的说明")],
        )

    assert result.itinerary.days[0].segments[0].notes == "新的说明"
    assert result.version.version_number == 1


def _amap_poi_fixture(name: str, longitude: float, latitude: float) -> MapPoiResponse:
    return MapPoiResponse(
        id=f"B000{name.replace(' ', '').upper()}",
        name=name,
        type="Scenic",
        city="Beijing",
        district="Dongcheng",
        address=f"{name} Road",
        longitude=longitude,
        latitude=latitude,
        category="scenic",
        source="amap-place-search",
        sourceNote="source: AMap",
        confidence=0.91,
        photos=[],
    )


def _install_fake_amap_search(monkeypatch, fixtures: dict[str, MapPoiResponse]) -> None:
    def fake_search(_self, city, keyword, category="all", limit=12):
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[fixtures[keyword]] if keyword in fixtures else [],
        )

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService.search", fake_search)


def _install_fake_route_service(monkeypatch, *, fail: bool = False) -> None:
    def fake_route_init(self, map_provider_key=None, timeout_seconds=5.0):
        self.map_provider_key = "test-amap-key"
        self.timeout_seconds = timeout_seconds
        self.warnings = []

    def fake_fetch_route(_self, from_poi, to_poi, mode):
        if fail:
            raise RouteProviderError("INVALID_USER_KEY")
        return {}

    def fake_route_from_payload(
        _self,
        plan_id,
        group_index,
        sort_order,
        from_poi,
        to_poi,
        mode,
        payload,
        from_segment_id=None,
        to_segment_id=None,
    ):
        durations = {"walking": 1800, "bicycling": 1260, "transit": 1080, "driving": 960, "taxi": 780}
        costs = {"walking": 0, "bicycling": 0, "transit": 4, "driving": 18, "taxi": 32}
        return RouteOption(
            id=f"route_{from_segment_id}_{to_segment_id}_{mode}",
            plan_id=plan_id,
            from_segment_id=from_segment_id,
            to_segment_id=to_segment_id,
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            provider="amap-webservice",
            mode=mode,
            label=mode,
            is_selected=False,
            sort_order=sort_order,
            distance_meters=1200 + sort_order,
            duration_seconds=durations[mode],
            cost_amount=costs[mode],
            cost_currency="CNY",
            polyline=[[from_poi.longitude, from_poi.latitude], [to_poi.longitude, to_poi.latitude]],
            steps=[],
            provider_payload={},
        )

    monkeypatch.setattr("src.services.route_service.RouteService.__init__", fake_route_init)
    monkeypatch.setattr("src.services.route_service.RouteService._fetch_amap_route", fake_fetch_route)
    monkeypatch.setattr("src.services.route_service.RouteService._route_from_payload", fake_route_from_payload)


def test_patch_service_replace_itinerary_grounds_agent_text_pois_and_routes(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")

    def fake_search(_self, city, keyword, category="all", limit=12):
        fixtures = {
            "Alpha Park": MapPoiResponse(
                id="B000ALPHA",
                name="Alpha Park",
                type="Scenic",
                city="Beijing",
                district="Dongcheng",
                address="Alpha Road",
                longitude=116.397026,
                latitude=39.918058,
                category="scenic",
                source="amap-place-search",
                sourceNote="source: AMap",
                confidence=0.91,
                photos=[MapPoiPhotoResponse(title="Alpha Park", url="https://example.com/alpha.jpg")],
            ),
            "Beta Museum": MapPoiResponse(
                id="B000BETA",
                name="Beta Museum",
                type="Museum",
                city="Beijing",
                district="Dongcheng",
                address="Beta Road",
                longitude=116.410886,
                latitude=39.881949,
                category="scenic",
                source="amap-place-search",
                sourceNote="source: AMap",
                confidence=0.9,
                photos=[],
            ),
        }
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[fixtures[keyword]] if keyword in fixtures else [],
        )

    def fake_build_route(
        _self,
        plan_id,
        group_index,
        sort_order,
        from_poi,
        to_poi,
        mode,
        from_segment_id=None,
        to_segment_id=None,
    ):
        durations = {"walking": 1800, "bicycling": 1260, "transit": 1080, "driving": 960, "taxi": 780}
        costs = {"walking": 0, "bicycling": 0, "transit": 4, "driving": 18, "taxi": 32}
        return RouteOption(
            id=f"route_{group_index}_{mode}",
            plan_id=plan_id,
            from_segment_id=from_segment_id,
            to_segment_id=to_segment_id,
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            provider="amap-webservice",
            mode=mode,
            label=mode,
            is_selected=False,
            sort_order=sort_order,
            distance_meters=1200 + sort_order,
            duration_seconds=durations[mode],
            cost_amount=costs[mode],
            cost_currency="CNY",
            polyline=[[from_poi.longitude, from_poi.latitude], [to_poi.longitude, to_poi.latitude]],
            steps=[],
            provider_payload={},
        )

    def fake_route_init(self, map_provider_key=None, timeout_seconds=5.0):
        self.map_provider_key = "test-amap-key"
        self.timeout_seconds = timeout_seconds
        self.warnings = []

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService.search", fake_search)
    _install_fake_route_service(monkeypatch)

    with open_db() as connection:
        session = ConversationService(connection).create_session("Beijing", "Agent route session")
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=agent_text_route_snapshot())],
            source_type="agent",
            planning_context={"requestIntentContract": _route_decision_contract_context()},
        )
        rows = connection.execute(
            "SELECT name, source, amap_id, longitude, latitude FROM pois WHERE plan_id = ? ORDER BY name",
            (session.active_plan_id,),
        ).fetchall()
        route_count = connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (session.active_plan_id,)
        ).fetchone()[0]

    assert [row["source"] for row in rows] == ["amap-place-search", "amap-place-search"]
    assert [row["amap_id"] for row in rows] == ["B000ALPHA", "B000BETA"]
    assert all(row["longitude"] is not None and row["latitude"] is not None for row in rows)
    assert route_count == 1
    assert len(result.itinerary.route_options) == 1
    assert result.itinerary.route_options[0].from_segment_id == "seg_alpha"
    assert result.itinerary.route_options[0].to_segment_id == "seg_beta"
    assert result.itinerary.route_options[0].mode == "walking"
    assert result.itinerary.route_options[0].sort_order == 1
    assert result.itinerary.route_options[0].polyline == [[116.397026, 39.918058], [116.410886, 39.881949]]
    assert result.itinerary.route_options[0].is_selected is True
    assert result.itinerary.days[0].segments[0].poi.source == "amap-place-search"
    assert result.itinerary.days[0].segments[0].poi.grounding_status == "verified_amap"
    assert result.itinerary.days[0].segments[0].poi.confidence == 0.91
    assert result.itinerary.days[0].segments[0].poi.amap_id == "B000ALPHA"
    assert result.itinerary.days[0].segments[1].poi.amap_id == "B000BETA"
    assert result.itinerary.days[0].segments[0].start_time == "09:00"
    assert result.itinerary.days[0].segments[1].start_time == "11:10"
    assert result.patch.metadata["scheduleUpdatedCount"] == 1


def test_patch_service_routes_only_same_day_adjacent_segments_and_respects_transport_preference(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    _install_fake_amap_search(
        monkeypatch,
        {
            "Alpha Park": _amap_poi_fixture("Alpha Park", 116.397026, 39.918058),
            "Beta Museum": _amap_poi_fixture("Beta Museum", 116.410886, 39.881949),
            "Gamma Gallery": _amap_poi_fixture("Gamma Gallery", 116.420886, 39.891949),
            "Delta Tower": _amap_poi_fixture("Delta Tower", 121.5001, 31.2361),
            "Epsilon Garden": _amap_poi_fixture("Epsilon Garden", 121.5101, 31.2461),
        },
    )
    _install_fake_route_service(monkeypatch)

    with open_db() as connection:
        session = ConversationService(connection).create_session("Beijing", "Agent multiday route session")
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=agent_text_multiday_route_snapshot())],
            source_type="agent",
            planning_context={"requestIntentContract": _route_decision_contract_context()},
        )
        route_rows = connection.execute(
            """
            SELECT from_segment_id, to_segment_id, mode, is_selected, sort_order
            FROM route_options
            WHERE plan_id = ?
            ORDER BY from_segment_id, sort_order
            """,
            (session.active_plan_id,),
        ).fetchall()

    selected_pairs = [
        (route.from_segment_id, route.to_segment_id) for route in result.itinerary.route_options if route.is_selected
    ]
    assert selected_pairs == [
        ("seg_alpha", "seg_beta"),
        ("seg_beta", "seg_gamma"),
        ("seg_delta", "seg_epsilon"),
    ]
    assert all(
        route.mode == "walking" and route.sort_order == 1
        for route in result.itinerary.route_options
        if route.is_selected
    )
    assert ("seg_gamma", "seg_delta") not in selected_pairs
    assert len(route_rows) == 3
    assert sum(1 for row in route_rows if row["is_selected"]) == 3


def test_patch_service_grounding_failure_is_zero_write(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    _install_fake_amap_search(monkeypatch, {})

    with open_db() as connection:
        session = ConversationService(connection).create_session("Beijing", "Agent grounding failure session")
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=agent_text_route_snapshot())],
                source_type="agent",
                planning_context={"requestIntentContract": _route_decision_contract_context()},
            )
        poi_count = connection.execute(
            "SELECT COUNT(*) FROM pois WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        route_count = connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (session.active_plan_id,)
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert "initial_route_matrix_requires_grounded_amap_anchors" in str(error.value.detail)
    assert poi_count == 0
    assert route_count == 0
    assert version_count == 0


def test_patch_service_area_like_agent_poi_is_zero_write_when_identity_is_not_exact(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")

    def fake_search(_self, city, keyword, category="all", limit=12):
        fixtures = {
            "五道口 清华 餐厅": _amap_poi_fixture("五道口餐厅", 116.3372, 39.9929),
            "Beta Museum": _amap_poi_fixture("Beta Museum", 116.410886, 39.881949),
        }
        poi = fixtures.get(keyword)
        if poi and keyword == "五道口 清华 餐厅":
            poi.type = "餐饮服务;中餐厅"
            poi.category = "food"
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=[poi] if poi else [],
        )

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService.search", fake_search)
    _install_fake_route_service(monkeypatch)

    snapshot = deepcopy(agent_text_route_snapshot())
    snapshot["days"][0]["segments"][0]["poi"]["name"] = "五道口/清华周边午餐"
    snapshot["days"][0]["segments"][0]["poi"]["category"] = "food"

    with open_db() as connection:
        session = ConversationService(connection).create_session("Beijing", "Agent area route session")
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=snapshot)],
                source_type="agent",
                planning_context={"requestIntentContract": _route_decision_contract_context()},
            )
        poi_count = connection.execute(
            "SELECT COUNT(*) FROM pois WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert "initial_route_matrix_requires_grounded_amap_anchors" in str(error.value.detail)
    assert poi_count == 0
    assert version_count == 0


def test_patch_service_route_provider_failure_is_zero_write(monkeypatch):
    clear_database()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    _install_fake_amap_search(
        monkeypatch,
        {
            "Alpha Park": _amap_poi_fixture("Alpha Park", 116.397026, 39.918058),
            "Beta Museum": _amap_poi_fixture("Beta Museum", 116.410886, 39.881949),
        },
    )
    _install_fake_route_service(monkeypatch, fail=True)

    with open_db() as connection:
        session = ConversationService(connection).create_session("Beijing", "Agent route failure session")
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=agent_text_route_snapshot())],
                source_type="agent",
            )
        poi_count = connection.execute(
            "SELECT COUNT(*) FROM pois WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        route_count = connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (session.active_plan_id,)
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert "provider_route_matrix_preflight_failed" in str(error.value.detail)
    assert poi_count == 0
    assert route_count == 0
    assert version_count == 0


def test_patch_service_commits_base_patch_before_slow_planning_refresh(monkeypatch):
    clear_database()
    refresh_transaction_states = []

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        refresh_transaction_states.append(self.db.in_transaction)
        assert commit_between_tools is True
        return ["路线刷新已延后到基础 patch 提交之后。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        _install_passed_provider_matrix(monkeypatch)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_transport_mode", segmentId="seg_patch", value="taxi")],
            base_version_id=base_version_id,
        )

    # The topology preflight owns the Provider evidence now.  Re-running the
    # slow refresh after commit could replace that verified matrix with a
    # late failure, so it is intentionally skipped.
    assert refresh_transaction_states == []
    assert result.itinerary.route_warnings == []


def test_apply_edit_commits_transport_change_before_planning_refresh(monkeypatch):
    clear_database()
    refresh_transaction_states = []

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        refresh_transaction_states.append((self.db.in_transaction, preferred_mode))
        assert commit_between_tools is True
        return ["交通方式已提交后再刷新路线。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        result = ItineraryService(connection).apply_edit_with_context(
            session.active_plan_id,
            "replace_transport_mode",
            "seg_patch",
            "taxi",
        )
        transport_mode = connection.execute(
            "SELECT transport_mode FROM itinerary_segments WHERE id = ? AND plan_id = ?",
            ("seg_patch", session.active_plan_id),
        ).fetchone()["transport_mode"]

    assert transport_mode == "taxi"
    assert refresh_transaction_states == [(False, "taxi")]
    assert result.days[0].segments[0].transport_mode == "taxi"


def test_patch_service_replace_segment_poi_refreshes_only_adjacent_legs(monkeypatch):
    clear_database()
    captured_route_pairs = []

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        captured_route_pairs.append(route_pairs)
        return ["只刷新替换地点前后路线。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        add_third_timeline_segment(connection, session.active_plan_id)
        _install_passed_provider_matrix(monkeypatch)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch_2",
                    amapPoi=_amap_poi_fixture("天坛公园", 116.4102, 39.9201),
                )
            ],
            base_version_id=base_version_id,
        )

    assert captured_route_pairs == []
    assert result.itinerary.route_warnings == []


def test_patch_service_timeline_policy_uses_guarded_matrix_without_post_write_route_retry(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    captured_route_pairs = []

    def fake_refresh_routes(self, plan_id, preferred_mode=None, route_pairs=None):
        captured_route_pairs.append(route_pairs)
        return ["局部路线已刷新。"]

    def fail_optional_refresh(*_args, **_kwargs):
        raise AssertionError("timeline local edit must not refresh weather, ticket, or risk tools")

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_routes", fake_refresh_routes)
    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fail_optional_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        add_third_timeline_segment(connection, session.active_plan_id)
        _install_passed_provider_matrix(monkeypatch)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch_2",
                    amapPoi=_amap_poi_fixture("天坛公园", 116.4102, 39.9201),
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={
                "stagedPlanningPipeline": {"enabled": True, "mode": "timeline_edit"},
                "toolRefreshPolicy": {
                    "route": "touched_pairs_only",
                    "weather": "skip",
                    "ticket": "skip",
                    "risk": "skip",
                },
            },
        )

    assert captured_route_pairs == []
    assert result.itinerary.route_warnings == []
    assert {
        (route.from_segment_id, route.to_segment_id) for route in result.itinerary.route_options if route.is_selected
    } == {("seg_patch", "seg_patch_2"), ("seg_patch_2", "seg_patch_3")}


def _install_passed_provider_matrix(
    monkeypatch,
    calls: list[dict] | None = None,
) -> list[dict]:
    recorded = calls if calls is not None else []

    def leg(left: dict, right: dict, *, distance: int, duration: int) -> dict:
        return {
            "fromSegmentId": left["segmentId"],
            "toSegmentId": right["segmentId"],
            "fromAmapId": left["amapId"],
            "toAmapId": right["amapId"],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "walking",
            "distanceMeters": distance,
            "durationSeconds": duration,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
        }

    def combined(*items: dict) -> dict:
        return {
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "walking",
            "distanceMeters": sum(item["distanceMeters"] for item in items),
            "durationSeconds": sum(item["durationSeconds"] for item in items),
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "composite": True,
            "componentCount": len(items),
        }

    def fake_evaluate(_self, **kwargs):
        recorded.append(kwargs)
        previous = kwargs.get("previous")
        candidate = kwargs["candidate"]
        following = kwargs.get("following")
        baseline_candidate = kwargs.get("baseline_candidate")
        legs: dict[str, dict] = {}
        if previous is not None:
            legs["previousToCandidate"] = leg(
                previous,
                candidate,
                distance=500,
                duration=300,
            )
        if following is not None:
            legs["candidateToNext"] = leg(
                candidate,
                following,
                distance=500,
                duration=300,
            )
        if baseline_candidate is not None:
            baseline_parts = []
            if previous is not None:
                incoming = leg(
                    previous,
                    baseline_candidate,
                    distance=500,
                    duration=300,
                )
                legs["baselinePreviousToCurrent"] = incoming
                baseline_parts.append(incoming)
            if following is not None:
                outgoing = leg(
                    baseline_candidate,
                    following,
                    distance=500,
                    duration=300,
                )
                legs["baselineCurrentToNext"] = outgoing
                baseline_parts.append(outgoing)
            legs["previousToNext"] = combined(*baseline_parts) if len(baseline_parts) == 2 else baseline_parts[0]
        elif previous is not None and following is not None:
            legs["previousToNext"] = leg(
                previous,
                following,
                distance=800,
                duration=480,
            )
        score = RouteInsertionScorer().score_from_route_matrix(
            previous_to_candidate=legs.get("previousToCandidate"),
            candidate_to_next=legs.get("candidateToNext"),
            previous_to_next=legs.get("previousToNext"),
            detour_tolerance=kwargs.get("detour_tolerance"),
            schedule_slack_minutes=None,
            time_window_feasible=True,
            mobility_profile=kwargs.get("mobility_profile"),
        )
        assert score is not None and score.detour_level != "unacceptable"
        return ProviderRouteInsertionResult(
            status="passed",
            score=score,
            legs=legs,
            time_window_feasible=True,
        )

    def fake_verified_leg(_self, **kwargs):
        return leg(
            kwargs["left"],
            kwargs["right"],
            distance=500,
            duration=300,
        )

    monkeypatch.setattr(
        ProviderRouteInsertionService,
        "evaluate",
        fake_evaluate,
    )
    monkeypatch.setattr(
        ProviderRouteInsertionService,
        "verified_leg",
        fake_verified_leg,
    )
    return recorded


def _install_verified_provider_leg(
    monkeypatch,
    calls: list[dict] | None = None,
) -> list[dict]:
    recorded = calls if calls is not None else []

    def verified_leg(_self, **kwargs):
        recorded.append(kwargs)
        left = kwargs["left"]
        right = kwargs["right"]
        return {
            "fromSegmentId": left["segmentId"],
            "toSegmentId": right["segmentId"],
            "fromAmapId": left["amapId"],
            "toAmapId": right["amapId"],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": kwargs.get("transport_mode") or "walking",
            "distanceMeters": 800,
            "durationSeconds": 600,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
        }

    monkeypatch.setattr(
        ProviderRouteInsertionService,
        "verified_leg",
        verified_leg,
    )
    return recorded


def test_patch_service_persists_decision_and_final_provider_matrix_coverage(monkeypatch):
    clear_database()
    queried_at = datetime.now(timezone.utc)
    candidate = _amap_poi_fixture("天坛公园", 116.4102, 39.9201)

    def leg(from_segment_id, to_segment_id, from_amap_id, to_amap_id, *, distance, duration):
        return {
            "fromSegmentId": from_segment_id,
            "toSegmentId": to_segment_id,
            "fromAmapId": from_amap_id,
            "toAmapId": to_amap_id,
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "walking",
            "distanceMeters": distance,
            "durationSeconds": duration,
            "queriedAt": queried_at.isoformat(),
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
        }

    incoming = leg(
        "seg_patch",
        "seg_patch_2",
        "B000A8UIN8",
        candidate.id,
        distance=1000,
        duration=600,
    )

    outgoing = leg(
        "seg_patch_2",
        "seg_patch_3",
        candidate.id,
        "B0北海",
        distance=1000,
        duration=600,
    )
    baseline_incoming = leg(
        "seg_patch",
        "seg_patch_2",
        "B000A8UIN8",
        "B0景山",
        distance=900,
        duration=450,
    )
    baseline_outgoing = leg(
        "seg_patch_2",
        "seg_patch_3",
        "B0景山",
        "B0北海",
        distance=900,
        duration=450,
    )
    proof = {
        "status": "passed",
        "failureReason": None,
        "networkVerified": True,
        "detourLevel": "high",
        "generalizedCostDelta": 5.0,
        "detourRatio": 0.3333,
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35.0,
            "maxDetourRatio": 0.35,
        },
        "timeWindowFeasible": True,
        "scheduleSlackMinutes": None,
        "mobilityProfile": {
            "source": "provider_route_matrix",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
        "legs": {
            "previousToCandidate": incoming,
            "candidateToNext": outgoing,
            "baselinePreviousToCurrent": baseline_incoming,
            "baselineCurrentToNext": baseline_outgoing,
            "previousToNext": {
                "provider": "amap-webservice",
                "source": "amap-webservice",
                "mode": "walking",
                "distanceMeters": 1800,
                "durationSeconds": 900,
                "walkingDistanceMeters": 0,
                "transferCount": 0,
                "waitSeconds": 0,
                "riskPenaltyMinutes": 0,
                "composite": True,
                "componentCount": 2,
            },
        },
        "operation": "replace_poi",
        "segmentId": "seg_patch_2",
        "baseVersionId": "",
        "candidateAmapId": candidate.id,
    }
    route_decision_contract = _route_decision_contract_context()["routeDecisionContract"]
    proof["routeDecisionContract"] = deepcopy(route_decision_contract)

    def fake_refresh_routes(self, plan_id, preferred_mode=None, route_pairs=None):
        points = {
            row["id"]: row
            for row in self.db.execute(
                """
                SELECT s.id, s.poi_id, p.amap_id
                FROM itinerary_segments s
                JOIN pois p ON p.id = s.poi_id
                WHERE s.plan_id = ?
                """,
                (plan_id,),
            ).fetchall()
        }
        for index, matrix_leg in enumerate((incoming, outgoing), start=1):
            left = points[matrix_leg["fromSegmentId"]]
            right = points[matrix_leg["toSegmentId"]]
            self._insert_route(
                RouteOption(
                    id=f"route_matrix_final_{index}",
                    plan_id=plan_id,
                    from_segment_id=left["id"],
                    to_segment_id=right["id"],
                    from_poi_id=left["poi_id"],
                    to_poi_id=right["poi_id"],
                    provider="amap-webservice",
                    mode="walking",
                    is_selected=True,
                    sort_order=1,
                    distance_meters=matrix_leg["distanceMeters"],
                    duration_seconds=matrix_leg["durationSeconds"],
                    polyline=[[116.39, 39.91], [116.40, 39.92]],
                    queried_at=queried_at,
                )
            )
        return []

    monkeypatch.setattr(
        "src.services.itinerary_service.ItineraryService.refresh_routes",
        fake_refresh_routes,
    )

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        add_third_timeline_segment(connection, session.active_plan_id)
        _install_passed_provider_matrix(monkeypatch)
        proof["baseVersionId"] = base_version_id
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch_2",
                    amapPoi=candidate,
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={
                "routeDecisionContract": route_decision_contract,
                "stagedPlanningPipeline": {"enabled": True, "mode": "timeline_edit"},
                "toolRefreshPolicy": {"route": "touched_pairs_only"},
                "routeInsertionProofs": [proof],
            },
        )
        snapshot_row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (result.version.id,),
        ).fetchone()
        snapshot = json.loads(snapshot_row["snapshot_json"])
        soft_failures = []
        hard_failures = []
        quality = AgentVerifierService(connection)._check_route_quality(
            session.active_plan_id,
            soft_failures,
            hard_failures=hard_failures,
            version_snapshot=snapshot,
        )

    assert snapshot["routeInsertionProofs"][0] == proof
    assert snapshot["routeInsertionProofs"][0]["routeDecisionContract"] == route_decision_contract
    final_coverage = [
        item for item in snapshot["routeInsertionProofs"] if item.get("operation") == "final_route_pair_coverage"
    ]
    assert len(final_coverage) == 2
    assert quality["blockingIssues"] == []
    assert hard_failures == []


def test_patch_service_rejects_unacceptable_provider_matrix_proof_before_write():
    clear_database()
    candidate = _amap_poi_fixture("天坛公园", 116.4102, 39.9201)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)

        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi",
                        segmentId="seg_patch_2",
                        amapPoi=candidate,
                    )
                ],
                source_type="agent",
                planning_context={
                    "routeInsertionProofs": [
                        {
                            "status": "failed",
                            "networkVerified": True,
                            "detourLevel": "unacceptable",
                            "timeWindowFeasible": True,
                            "segmentId": "seg_patch_2",
                            "baseVersionId": "",
                            "candidateAmapId": candidate.id,
                        }
                    ]
                },
            )
        current = connection.execute(
            """
            SELECT p.amap_id
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.id = 'seg_patch_2'
            """
        ).fetchone()
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert error.value.status_code == 400
    assert current["amap_id"] == "B0景山"
    assert version_count == 0


def test_agent_replace_without_supplied_proof_generates_provider_matrix_before_write(monkeypatch):
    clear_database()
    calls = []
    queried_at = datetime.now(timezone.utc).isoformat()
    candidate = _amap_poi_fixture("天坛公园", 116.4102, 39.9201)

    class PassedResult:
        passed = True
        failure_reason = None

        def to_dict(self):
            return {
                "status": "passed",
                "failureReason": None,
                "networkVerified": True,
                "detourLevel": "low",
                "generalizedCostDelta": 0.0,
                "detourRatio": 0.0,
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 35.0,
                    "maxDetourRatio": 0.35,
                },
                "timeWindowFeasible": True,
                "scheduleSlackMinutes": None,
                "mobilityProfile": {
                    "source": "provider_route_matrix",
                    "walkingPenaltyMinutesPerKm": 1.8,
                    "transferPenaltyMinutes": 6.0,
                    "waitTimeMultiplier": 1.0,
                    "riskPenaltyMultiplier": 1.0,
                },
                "legs": {
                    "previousToCandidate": {
                        "fromSegmentId": "seg_patch",
                        "toSegmentId": "seg_patch_2",
                        "fromAmapId": "B000A8UIN8",
                        "toAmapId": candidate.id,
                        "provider": "amap-webservice",
                        "source": "amap-webservice",
                        "mode": "walking",
                        "distanceMeters": 1000,
                        "durationSeconds": 600,
                        "queriedAt": queried_at,
                        "walkingDistanceMeters": 0,
                        "transferCount": 0,
                        "waitSeconds": 0,
                        "riskPenaltyMinutes": 0,
                    },
                    "baselinePreviousToCurrent": {
                        "fromSegmentId": "seg_patch",
                        "toSegmentId": "seg_patch_2",
                        "fromAmapId": "B000A8UIN8",
                        "toAmapId": "B0景山",
                        "provider": "amap-webservice",
                        "source": "amap-webservice",
                        "mode": "walking",
                        "distanceMeters": 900,
                        "durationSeconds": 600,
                        "queriedAt": queried_at,
                        "walkingDistanceMeters": 0,
                        "transferCount": 0,
                        "waitSeconds": 0,
                        "riskPenaltyMinutes": 0,
                    },
                    "previousToNext": {
                        "fromSegmentId": "seg_patch",
                        "toSegmentId": "seg_patch_2",
                        "fromAmapId": "B000A8UIN8",
                        "toAmapId": "B0景山",
                        "provider": "amap-webservice",
                        "source": "amap-webservice",
                        "mode": "walking",
                        "distanceMeters": 900,
                        "durationSeconds": 600,
                        "queriedAt": queried_at,
                        "walkingDistanceMeters": 0,
                        "transferCount": 0,
                        "waitSeconds": 0,
                        "riskPenaltyMinutes": 0,
                    },
                },
            }

    def fake_evaluate(_self, **kwargs):
        calls.append(kwargs)
        return PassedResult()

    monkeypatch.setattr(ProviderRouteInsertionService, "evaluate", fake_evaluate)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch_2",
                    amapPoi=candidate,
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"toolRefreshPolicy": {"route": "skip"}},
        )
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.version.id,),
            ).fetchone()[0]
        )

    assert len(calls) == 1
    assert calls[0]["baseline_candidate"]["amapId"] == "B0景山"
    assert snapshot["routeInsertionProofs"][0]["candidateAmapId"] == candidate.id


def test_agent_add_with_amap_poi_generates_provider_matrix_before_write(monkeypatch):
    clear_database()
    calls = _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("北海公园", 116.3901, 39.9301)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="add_segment",
                    dayId="day_patch",
                    startTime="15:00",
                    durationMinutes=60,
                    kind="activity",
                    amapPoi=candidate,
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"toolRefreshPolicy": {"route": "skip"}},
        )
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.version.id,),
            ).fetchone()[0]
        )

    assert len(calls) == 1
    generated_segment_id = calls[0]["candidate"]["segmentId"]
    assert generated_segment_id.startswith("seg_")
    assert any(segment.id == generated_segment_id for day in result.itinerary.days for segment in day.segments)
    assert snapshot["routeInsertionProofs"][0]["operation"] == "add_segment"
    assert snapshot["routeInsertionProofs"][0]["candidateAmapId"] == candidate.id


def test_agent_candidate_replacement_generates_provider_matrix_before_write(monkeypatch):
    clear_database()
    calls = _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("天坛公园", 116.4102, 39.9201)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        candidate_id = "cand_provider_preflight"
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session.session_id,
                None,
                candidate.name,
                "seg_patch_2",
                "北京",
                "scenic",
                "pending",
                json.dumps([candidate.model_dump(by_alias=True)], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi_from_candidate",
                    segmentId="seg_patch_2",
                    candidateId=candidate_id,
                    amapPoi=candidate,
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"toolRefreshPolicy": {"route": "skip"}},
        )
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.version.id,),
            ).fetchone()[0]
        )

    assert len(calls) == 1
    assert calls[0]["baseline_candidate"]["amapId"] == "B0景山"
    assert snapshot["routeInsertionProofs"][0]["operation"] == "replace_segment_poi_from_candidate"
    assert snapshot["routeInsertionProofs"][0]["candidateAmapId"] == candidate.id


@pytest.mark.parametrize(
    "operation_name",
    ["replace_segment_poi", "replace_segment_poi_from_candidate", "add_segment"],
)
def test_agent_route_decision_provider_failure_is_zero_business_write(monkeypatch, operation_name):
    clear_database()
    calls = []
    candidate = _amap_poi_fixture("天坛公园", 116.4102, 39.9201)

    class FailedResult:
        passed = False
        status = "failed"
        failure_reason = "provider_route_matrix_incomplete"

        @staticmethod
        def to_dict():
            return {
                "status": "failed",
                "failureReason": "provider_route_matrix_incomplete",
                "networkVerified": False,
                "detourLevel": None,
                "timeWindowFeasible": False,
                "legs": {},
            }

    def fail_evaluate(_self, **kwargs):
        calls.append(kwargs)
        return FailedResult()

    monkeypatch.setattr(ProviderRouteInsertionService, "evaluate", fail_evaluate)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        candidate_id = None
        if operation_name == "replace_segment_poi_from_candidate":
            candidate_id = "cand_provider_failure"
            connection.execute(
                """
                INSERT INTO amap_poi_candidates (
                    id, session_id, turn_id, query, segment_id, city, category, status,
                    candidates_json, selected_amap_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    session.session_id,
                    None,
                    candidate.name,
                    "seg_patch_2",
                    "北京",
                    "scenic",
                    "pending",
                    json.dumps([candidate.model_dump(by_alias=True)], ensure_ascii=False),
                    None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        if operation_name == "add_segment":
            operation = ItineraryPatchOperation(
                op="add_segment",
                dayId="day_patch",
                startTime="15:00",
                durationMinutes=60,
                amapPoi=candidate,
            )
        else:
            operation = ItineraryPatchOperation(
                op=operation_name,
                segmentId="seg_patch_2",
                candidateId=candidate_id,
                amapPoi=candidate,
            )
        baseline = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT s.id, p.amap_id
                FROM itinerary_segments s
                JOIN pois p ON p.id = s.poi_id
                WHERE s.plan_id = ?
                ORDER BY s.id
                """,
                (session.active_plan_id,),
            ).fetchall()
        ]
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [operation],
                source_type="agent",
                base_version_id=base_version_id,
                planning_context={"toolRefreshPolicy": {"route": "skip"}},
            )
        after = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT s.id, p.amap_id
                FROM itinerary_segments s
                JOIN pois p ON p.id = s.poi_id
                WHERE s.plan_id = ?
                ORDER BY s.id
                """,
                (session.active_plan_id,),
            ).fetchall()
        ]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert len(calls) == 1
    assert error.value.status_code == 400
    assert baseline == after
    assert version_count == 1


def _two_anchor_amap_snapshot() -> dict:
    left = _amap_poi_fixture("故宫博物院", 116.397026, 39.918058)
    right = _amap_poi_fixture("景山公园", 116.3969, 39.9236)
    left_payload = {**left.model_dump(by_alias=True), "amapId": left.id}
    right_payload = {**right.model_dump(by_alias=True), "amapId": right.id}
    return {
        "title": "Provider 路线预检",
        "city": "北京",
        "days": [
            {
                "id": "day_provider_preflight",
                "dayNumber": 1,
                "title": "Provider 路线日",
                "segments": [
                    {
                        "id": "seg_provider_left",
                        "startTime": "09:00",
                        "endTime": "10:00",
                        "kind": "activity",
                        "poi": left_payload,
                        "transportMode": "walking",
                        "estimatedCost": 0,
                    },
                    {
                        "id": "seg_provider_right",
                        "startTime": "11:00",
                        "endTime": "12:00",
                        "kind": "activity",
                        "poi": right_payload,
                        "transportMode": "walking",
                        "estimatedCost": 0,
                    },
                ],
            }
        ],
    }


def test_agent_initial_snapshot_generates_adjacent_provider_matrix_before_write(monkeypatch):
    clear_database()
    calls = []

    def verified_leg(_self, **kwargs):
        calls.append(kwargs)
        left = kwargs["left"]
        right = kwargs["right"]
        return {
            "fromSegmentId": left["segmentId"],
            "toSegmentId": right["segmentId"],
            "fromAmapId": left["amapId"],
            "toAmapId": right["amapId"],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "mode": "walking",
            "distanceMeters": 800,
            "durationSeconds": 600,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
        }

    monkeypatch.setattr(ProviderRouteInsertionService, "verified_leg", verified_leg)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_itinerary",
                    fullItinerary=_two_anchor_amap_snapshot(),
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={"toolRefreshPolicy": {"route": "skip"}},
        )
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.version.id,),
            ).fetchone()[0]
        )

    assert len(calls) == 1
    assert snapshot["routeInsertionProofs"][0]["proofType"] == "adjacent_route_coverage"
    assert snapshot["routeInsertionProofs"][0]["fromSegmentId"] == "seg_provider_left"
    assert snapshot["routeInsertionProofs"][0]["segmentId"] == "seg_provider_right"
    assert snapshot["routeMatrixExpectedPairs"] == [["seg_provider_left", "seg_provider_right"]]


def test_agent_initial_snapshot_provider_failure_is_zero_business_write(monkeypatch):
    clear_database()
    monkeypatch.setattr(
        ProviderRouteInsertionService,
        "verified_leg",
        lambda _self, **_kwargs: None,
    )
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        baseline_title = connection.execute(
            "SELECT title FROM itinerary_plans WHERE id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        baseline_segments = connection.execute(
            "SELECT COUNT(*) FROM itinerary_segments WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_itinerary",
                        fullItinerary=_two_anchor_amap_snapshot(),
                    )
                ],
                source_type="agent",
                base_version_id=base_version_id,
                planning_context={"toolRefreshPolicy": {"route": "skip"}},
            )
        after_title = connection.execute(
            "SELECT title FROM itinerary_plans WHERE id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        after_segments = connection.execute(
            "SELECT COUNT(*) FROM itinerary_segments WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert error.value.status_code == 400
    assert after_title == baseline_title
    assert after_segments == baseline_segments
    assert version_count == 1


def test_patch_service_concrete_meal_replacement_refreshes_crossed_and_adjacent_legs(monkeypatch):
    clear_database()
    captured_route_pairs = []

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        captured_route_pairs.append(route_pairs)
        return ["午餐已作为具体地点刷新上下路线。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        _install_passed_provider_matrix(monkeypatch)
        connection.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude, photo_url,
                source, confidence, amap_id, type, district, address, source_note,
                source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_lunch",
                session.active_plan_id,
                "午餐",
                "北京",
                "food",
                None,
                None,
                None,
                "agent-text-timeline",
                0.35,
                None,
                "餐饮时间",
                "",
                "",
                "午餐时间，餐厅可稍后搜索。groundingStatus：not_required；routeAnchor=false。",
                None,
                "[]",
            ),
        )
        connection.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time,
                poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_lunch",
                session.active_plan_id,
                "day_patch",
                2,
                "meal",
                "12:00",
                "13:00",
                "poi_lunch",
                "walk",
                60,
                "午餐时间，餐厅可稍后搜索。",
                None,
                None,
                None,
            ),
        )
        connection.execute(
            "UPDATE itinerary_segments SET segment_order = ?, start_time = ?, end_time = ? WHERE id = ? AND plan_id = ?",
            (3, "14:00", "16:00", "seg_patch_2", session.active_plan_id),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_lunch",
                    amapPoi=_amap_poi_fixture("麦当劳(清华大学店)", 116.332, 39.995),
                )
            ],
            base_version_id=base_version_id,
        )

    assert captured_route_pairs == []
    assert result.itinerary.route_warnings == []


def test_patch_service_replace_segment_poi_from_candidate_materializes_only_adjacent_matrix_legs(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    captured_route_pairs = []

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        captured_route_pairs.append(route_pairs)
        return ["只刷新候选替换地点前后路线。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        add_third_timeline_segment(connection, session.active_plan_id)
        _install_passed_provider_matrix(monkeypatch)
        candidate_id = "cand_route_scope"
        candidate_poi = _amap_poi_fixture("天坛公园", 116.4102, 39.9201)
        connection.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session.session_id,
                None,
                "天坛公园",
                "seg_patch_2",
                "北京",
                "scenic",
                "pending",
                json.dumps([candidate_poi.model_dump(by_alias=True)], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi_from_candidate",
                    segmentId="seg_patch_2",
                    candidateId=candidate_id,
                    amapPoi=candidate_poi,
                )
            ],
            source_type="agent",
            base_version_id=base_version_id,
        )

    assert captured_route_pairs == []
    assert result.itinerary.route_warnings == []
    assert {
        (route.from_segment_id, route.to_segment_id) for route in result.itinerary.route_options if route.is_selected
    } == {("seg_patch", "seg_patch_2"), ("seg_patch_2", "seg_patch_3")}


@pytest.mark.parametrize(
    "operation, expected_pairs",
    [
        (
            ItineraryPatchOperation(op="remove_segment", segmentId="seg_patch_2"),
            {("seg_patch", "seg_patch_2"), ("seg_patch_2", "seg_patch_3"), ("seg_patch", "seg_patch_3")},
        ),
        (
            ItineraryPatchOperation(
                op="reorder_segments",
                dayId="day_patch",
                orderedSegmentIds=["seg_patch_3", "seg_patch", "seg_patch_2"],
            ),
            {("seg_patch_2", "seg_patch_3"), ("seg_patch_3", "seg_patch")},
        ),
        (
            ItineraryPatchOperation(op="replace_transport_mode", segmentId="seg_patch_2", value="taxi"),
            {("seg_patch", "seg_patch_2"), ("seg_patch_2", "seg_patch_3")},
        ),
    ],
)
def test_patch_service_refreshes_only_impacted_route_pairs(monkeypatch, operation, expected_pairs):
    clear_database()
    captured_route_pairs = []

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        captured_route_pairs.append(route_pairs)
        return ["已按受影响路线局部刷新。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        add_third_timeline_segment(connection, session.active_plan_id)
        _install_passed_provider_matrix(monkeypatch)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [operation],
            base_version_id=base_version_id,
        )

    assert captured_route_pairs == []
    assert result.itinerary.route_warnings == []


def test_patch_service_replace_segment_start_time_does_not_refresh_routes(monkeypatch):
    clear_database()
    refresh_calls = []

    def fake_refresh(self, *args, **kwargs):
        refresh_calls.append((args, kwargs))
        return []

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO route_options (
                id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                provider, mode, label, is_selected, sort_order, transport_mode,
                distance_meters, duration_seconds, duration_minutes, cost_amount,
                cost_currency, cost_estimate, crowding_risk, source, polyline_json,
                steps_json, provider_payload_json, error_json, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "route_patch_time",
                session.active_plan_id,
                "seg_patch",
                "seg_patch_2",
                "poi_patch",
                "poi_patch_2",
                "test",
                "walking",
                "步行",
                1,
                1,
                "walking",
                1200,
                30 * 60,
                30,
                0,
                "CNY",
                0,
                "low",
                "test",
                "[[116,39],[116.1,39.1]]",
                "[]",
                "{}",
                None,
                now,
            ),
        )
        connection.commit()
        _install_passed_provider_matrix(monkeypatch)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_segment_start_time", segmentId="seg_patch", value="10:00")],
            base_version_id=base_version_id,
        )

    assert refresh_calls == []
    assert result.itinerary.days[0].segments[0].start_time == "10:00"
    assert result.itinerary.days[0].segments[1].start_time == "12:15"
    assert result.patch.metadata["scheduleUpdatedCount"] == 1


def test_user_timeline_start_edit_replaces_nonhard_controller_schedule_floor(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "显式时间覆盖")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        connection.execute(
            "UPDATE itinerary_segments SET semantic_metadata_json = ? WHERE id = 'seg_patch'",
            (
                json.dumps(
                    {
                        "intentType": "museum",
                        "scheduleConstraints": {
                            "earliestStart": "09:00",
                            "latestStart": "10:00",
                            "source": "controller_schedule_hint_daypart",
                            "hard": False,
                        },
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_start_time",
                    segmentId="seg_patch",
                    value="08:00",
                )
            ],
            source_type="user_timeline_mutation",
            base_version_id=base_version_id,
        )
        row = connection.execute(
            "SELECT start_time, end_time, semantic_metadata_json FROM itinerary_segments WHERE id = 'seg_patch'"
        ).fetchone()

    semantic = json.loads(row["semantic_metadata_json"])
    schedule = semantic["scheduleConstraints"]
    assert result.itinerary.days[0].segments[0].start_time == "08:00"
    assert (row["start_time"], row["end_time"]) == ("08:00", "10:00")
    assert schedule == {
        "earliestStart": "08:00",
        "latestStart": "08:00",
        "source": "user_explicit_clock",
        "hard": True,
        "windowEnd": "10:00",
        "confidence": 1.0,
        "explicitStartTime": "08:00",
        "userLocked": True,
    }
    assert semantic["userLocked"] is True
    assert semantic["timeWindowLocked"] is True


def test_later_user_timeline_start_edit_replaces_prior_user_explicit_clock(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "连续显式时间编辑")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        connection.execute(
            "UPDATE itinerary_segments SET semantic_metadata_json = ? WHERE id = 'seg_patch'",
            (
                json.dumps(
                    {
                        "intentType": "museum",
                        "scheduleConstraints": {
                            "earliestStart": "09:00",
                            "latestStart": "10:00",
                            "source": "controller_schedule_hint_daypart",
                            "hard": False,
                        },
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()

        first = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_start_time",
                    segmentId="seg_patch",
                    value="08:00",
                )
            ],
            source_type="user_timeline_mutation",
            base_version_id=base_version_id,
        )
        second = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_start_time",
                    segmentId="seg_patch",
                    value="07:30",
                )
            ],
            source_type="user_timeline_mutation",
            base_version_id=first.version.id,
        )
        row = connection.execute(
            "SELECT start_time, end_time, semantic_metadata_json FROM itinerary_segments WHERE id = 'seg_patch'"
        ).fetchone()

    schedule = json.loads(row["semantic_metadata_json"])["scheduleConstraints"]
    assert second.itinerary.days[0].segments[0].start_time == "07:30"
    assert (row["start_time"], row["end_time"]) == ("07:30", "09:30")
    assert schedule["source"] == "user_explicit_clock"
    assert schedule["earliestStart"] == "07:30"
    assert schedule["latestStart"] == "07:30"
    assert schedule["windowEnd"] == "09:30"
    assert schedule["hard"] is True


def test_user_timeline_start_edit_cannot_override_night_view_domain_floor(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "夜景硬约束")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        connection.execute(
            "UPDATE itinerary_segments SET semantic_metadata_json = ? WHERE id = 'seg_patch'",
            (
                json.dumps(
                    {
                        "intentType": "night_view",
                        "scheduleConstraints": {
                            "earliestStart": "18:00",
                            "latestStart": "21:00",
                            "windowEnd": "23:00",
                            "source": "intent_time_policy",
                            "hard": True,
                        },
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()

        with pytest.raises(HTTPException) as raised:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_start_time",
                        segmentId="seg_patch",
                        value="08:00",
                    )
                ],
                source_type="user_timeline_mutation",
                base_version_id=base_version_id,
            )
        row = connection.execute(
            "SELECT start_time, end_time FROM itinerary_segments WHERE id = 'seg_patch'"
        ).fetchone()
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]

    assert raised.value.status_code == 400
    assert "provider_route_matrix_preflight_failed:schedule.window.lower_bound" in str(raised.value.detail)
    assert (row["start_time"], row["end_time"]) == ("09:00", "11:00")
    assert active_version_id == base_version_id


def test_replace_poi_preserves_explicit_duration_override_during_same_timeline_mutation(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    museum = MapPoiResponse(
        id="B0TSINGHUAART",
        name="清华大学艺术博物馆",
        type="科教文化服务;博物馆;美术馆",
        city="北京市",
        district="海淀区",
        address="清华园1号",
        longitude=116.326,
        latitude=40.003,
        category="museum",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索，限定当前城市，extensions=all",
        confidence=0.98,
        photos=[],
    )
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "替换地点保留时长")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(op="replace_segment_poi", segmentId="seg_patch", amapPoi=museum),
                ItineraryPatchOperation(op="replace_segment_duration", segmentId="seg_patch", durationMinutes=120),
            ],
            source_type="user_timeline_mutation",
            base_version_id=base_version_id,
            planning_context={"toolRefreshPolicy": {"route": "touched_pairs_only"}},
        )
        row = connection.execute(
            "SELECT start_time, end_time, estimate_metadata_json FROM itinerary_segments WHERE id = 'seg_patch'"
        ).fetchone()

    metadata = json.loads(row["estimate_metadata_json"])
    duration_metadata = metadata["duration"]
    assert result.itinerary.days[0].segments[0].poi.name == "清华大学艺术博物馆"
    assert (row["start_time"], row["end_time"]) == ("09:00", "11:00")
    assert duration_metadata["minutes"] == 120
    assert duration_metadata["userLocked"] is True
    assert duration_metadata["source"] == "user_locked"


def test_non_replacement_patch_carries_forward_active_version_metadata(monkeypatch):
    """A local time edit must not erase proposal-only state from the active version."""

    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "版本元数据继承")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        base_row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (base_version_id,),
        ).fetchone()
        base_snapshot = json.loads(base_row["snapshot_json"])
        expected_metadata = {
            "creativeBrief": {"briefId": "simple_direction_brief_a", "title": "高校夜景方向"},
            "portfolioPartialTimeline": {
                "status": "partial",
                "pendingSlotCount": 1,
                "pendingSlotsStatus": "needs_confirmation",
            },
            "portfolioPendingSlots": [
                {
                    "id": "pending:day1_evening_night",
                    "planningSlotId": "day1_evening_night",
                    "dayNumber": 1,
                    "intentType": "night_view",
                    "requirementLevel": "required",
                    "groundingStatus": "unresolved",
                    "simpleDirectionProviderExhausted": True,
                    "simpleDirectionRequirementLineageConflict": False,
                    "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
                }
            ],
            "portfolioSelectionContext": {
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "simple_direction_portfolio_a",
                "focusBriefId": "simple_direction_brief_a",
            },
        }
        base_snapshot.update(deepcopy(expected_metadata))
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(base_snapshot, ensure_ascii=False), base_version_id),
        )
        connection.commit()

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_start_time",
                    segmentId="seg_patch",
                    value="08:00",
                )
            ],
            source_type="user_timeline_mutation",
            base_version_id=base_version_id,
        )
        result_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.version.id,),
            ).fetchone()["snapshot_json"]
        )

    assert result_snapshot["days"][0]["segments"][0]["startTime"] == "08:00"
    for key, value in expected_metadata.items():
        assert result_snapshot[key] == value


def test_partial_portfolio_replacement_allows_an_empty_day_only_with_a_pending_slot():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "部分时间轴")
        service = ItineraryPatchService(connection)
        snapshot = agent_text_route_snapshot()
        snapshot["days"].append(
            {
                "id": "day_pending",
                "dayNumber": 2,
                "title": "待补日程",
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 0,
                "segments": [],
            }
        )

        normal_errors = service._validate_replacement_snapshot(
            session.active_plan_id,
            snapshot,
        )
        snapshot["portfolioPartialTimeline"] = {
            "status": "partial",
            "pendingSlotCount": 1,
        }
        snapshot["portfolioPendingSlots"] = [
            {
                "id": "pending_day_2",
                "planningSlotId": "day-2-walk",
                "dayNumber": 2,
                "timeWindow": "14:00-16:00",
                "rawNeed": "街区漫步",
            }
        ]
        partial_errors = service._validate_replacement_snapshot(
            session.active_plan_id,
            snapshot,
        )

    assert "Day 2 requires at least one segment" in normal_errors
    assert "Day 2 requires at least one segment" not in partial_errors


def test_editable_draft_empty_day_requires_server_authorized_soft_pending_slot():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("深圳", "可编辑草案")
        service = ItineraryPatchService(connection)
        snapshot = agent_text_route_snapshot()
        snapshot["days"].append(
            {
                "id": "day_pending",
                "dayNumber": 2,
                "title": "待选择体验",
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 0,
                "segments": [],
            }
        )
        snapshot["portfolioPendingSlots"] = [
            {
                "planningSlotId": "day-2-local-life",
                "dayNumber": 2,
                "requirementLevel": "soft",
                "status": "pending_evidence",
            }
        ]

        normal_errors = service._validate_replacement_snapshot(session.active_plan_id, snapshot)
        editable_errors = service._validate_replacement_snapshot(
            session.active_plan_id,
            snapshot,
            allow_soft_pending_days=True,
        )
        snapshot["portfolioPendingSlots"][0]["requirementLevel"] = "required"
        hard_pending_errors = service._validate_replacement_snapshot(
            session.active_plan_id,
            snapshot,
            allow_soft_pending_days=True,
        )

    assert "Day 2 requires at least one segment" in normal_errors
    assert "Day 2 requires at least one segment" not in editable_errors
    assert "Day 2 requires at least one segment" in hard_pending_errors


def test_simple_open_zero_target_day_requires_server_capability_and_exact_density_contract():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "零目标日写入边界")
        service = ItineraryPatchService(connection)
        snapshot = agent_text_route_snapshot()
        snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
        snapshot["requiredPlanningDayNumbers"] = [1]
        snapshot["explicitRestDayNumbers"] = [2]
        snapshot["desiredDensityAnchorTargets"] = {"1": 2, "2": 0}
        snapshot["days"].append(
            {
                "id": "day_zero_target",
                "dayNumber": 2,
                "title": "待继续编辑",
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 0,
                "segments": [],
            }
        )
        operation = ItineraryPatchOperation(op="replace_itinerary", fullItinerary=snapshot)

        public_flag_errors = service._validate(
            session.active_plan_id,
            [operation],
            planning_context={
                "simpleDirectionCommit": True,
                "simpleOpenNonBlockingRoutes": True,
            },
        )
        authorized_errors = service._validate_replacement_snapshot(
            session.active_plan_id,
            snapshot,
            allow_server_sealed_zero_target_days=True,
        )

        invalid_contract_errors = []
        for invalid_targets in (
            {"1": 2, "2": 1},
            {"1": 0, "2": 0},
            {"1": 2},
            {"1": 2, "2": False},
        ):
            invalid_snapshot = deepcopy(snapshot)
            invalid_snapshot["desiredDensityAnchorTargets"] = invalid_targets
            invalid_contract_errors.append(
                service._validate_replacement_snapshot(
                    session.active_plan_id,
                    invalid_snapshot,
                    allow_server_sealed_zero_target_days=True,
                )
            )

    day_error = "Day 2 requires at least one segment"
    assert day_error in public_flag_errors
    assert day_error not in authorized_errors
    assert all(day_error in errors for errors in invalid_contract_errors)


def test_partial_portfolio_version_returns_pending_slots_with_confirmed_timeline(monkeypatch):
    clear_database()
    _install_verified_provider_leg(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "部分时间轴")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        snapshot = ItineraryService(connection).get_plan(session.active_plan_id).model_dump(by_alias=True, mode="json")
        snapshot["status"] = "partial"
        snapshot["days"].append(
            {
                "id": "day_pending",
                "dayNumber": 2,
                "title": "待补日程",
                "date": None,
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 0,
                "segments": [],
            }
        )
        snapshot["portfolioPartialTimeline"] = {
            "status": "partial",
            "pendingSlotCount": 1,
        }
        snapshot["portfolioPendingSlots"] = [
            {
                "id": "pending_day_2",
                "planningSlotId": "day-2-walk",
                "briefId": "local",
                "poolId": "walk-pool",
                "dayNumber": 2,
                "timeWindow": "14:00-16:00",
                "startTime": "14:00",
                "endTime": "16:00",
                "durationMinutes": 120,
                "rawNeed": "街区漫步",
                "intentType": "neighborhood_walk",
                "kind": "activity",
                "state": "pending",
                "label": "待补：街区漫步",
            }
        ]

        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_itinerary", fullItinerary=snapshot)],
            source_type="agent",
            base_version_id=base_version_id,
            planning_context={
                "portfolioPartialTimeline": True,
                "timelinePersistencePolicy": "draft_first",
                "toolRefreshPolicy": {"route": "skip"},
            },
        )

    assert [segment.poi.name for segment in result.itinerary.days[0].segments] == [
        "故宫博物院",
        "景山公园",
    ]
    assert result.itinerary.days[1].segments == []
    assert len(result.itinerary.days[1].pending_slots) == 1
    assert result.itinerary.days[1].pending_slots[0].planning_slot_id == "day-2-walk"
    assert result.itinerary.days[1].pending_slots[0].label == "待补：街区漫步"


def agent_text_route_snapshot():
    return {
        "title": "Agent grounded route test",
        "city": "Beijing",
        "templateType": "agent_mvp",
        "budgetEstimate": 0,
        "budgetDeltaExplanation": "Agent estimate.",
        "decisionRationale": "Agent patch should be grounded before route refresh.",
        "status": "draft",
        "days": [
            {
                "id": "day_agent_route",
                "dayNumber": 1,
                "title": "Agent day",
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 0,
                "segments": [
                    {
                        "id": "seg_alpha",
                        "startTime": "09:00",
                        "endTime": "10:00",
                        "kind": "activity",
                        "poi": {
                            "id": "poi_alpha_text",
                            "amapId": None,
                            "name": "Alpha Park",
                            "city": "Beijing",
                            "category": "scenic",
                            "latitude": None,
                            "longitude": None,
                            "source": "agent-text-timeline",
                            "sourceNote": "高德 POI 待校验",
                            "confidence": 0.45,
                        },
                        "transportMode": "walk",
                        "estimatedCost": 0,
                        "notes": "Agent draft.",
                    },
                    {
                        "id": "seg_beta",
                        "startTime": "10:30",
                        "endTime": "11:30",
                        "kind": "activity",
                        "poi": {
                            "id": "poi_beta_text",
                            "amapId": None,
                            "name": "Beta Museum",
                            "city": "Beijing",
                            "category": "scenic",
                            "latitude": None,
                            "longitude": None,
                            "source": "agent-text-timeline",
                            "sourceNote": "高德 POI 待校验",
                            "confidence": 0.45,
                        },
                        "transportMode": "walk",
                        "estimatedCost": 0,
                        "notes": "Agent draft.",
                    },
                ],
            }
        ],
        "routeOptions": [],
        "weatherSignals": [],
        "trafficCrowdingSignals": [],
        "ticketLookupResults": [],
    }


def agent_text_multiday_route_snapshot():
    snapshot = agent_text_route_snapshot()
    snapshot["title"] = "Agent multiday grounded route test"
    snapshot["days"] = [
        {
            "id": "day_agent_route_1",
            "dayNumber": 1,
            "title": "Agent day 1",
            "weatherSummary": "",
            "riskSummary": "",
            "totalEstimatedCost": 0,
            "segments": [
                _agent_text_segment("seg_alpha", "poi_alpha_text", "Alpha Park", "09:00", "10:00"),
                _agent_text_segment("seg_beta", "poi_beta_text", "Beta Museum", "10:30", "11:30"),
                _agent_text_segment("seg_gamma", "poi_gamma_text", "Gamma Gallery", "12:00", "13:00"),
            ],
        },
        {
            "id": "day_agent_route_2",
            "dayNumber": 2,
            "title": "Agent day 2",
            "weatherSummary": "",
            "riskSummary": "",
            "totalEstimatedCost": 0,
            "segments": [
                _agent_text_segment("seg_delta", "poi_delta_text", "Delta Tower", "09:00", "10:00"),
                _agent_text_segment("seg_epsilon", "poi_epsilon_text", "Epsilon Garden", "10:30", "11:30"),
            ],
        },
    ]
    return snapshot


def _agent_text_segment(segment_id: str, poi_id: str, name: str, start_time: str, end_time: str):
    return {
        "id": segment_id,
        "startTime": start_time,
        "endTime": end_time,
        "kind": "activity",
        "poi": {
            "id": poi_id,
            "amapId": None,
            "name": name,
            "city": "Beijing",
            "category": "scenic",
            "latitude": None,
            "longitude": None,
            "source": "agent-text-timeline",
            "sourceNote": "高德 POI 待校验",
            "confidence": 0.45,
        },
        "transportMode": "walk",
        "estimatedCost": 0,
        "notes": "Agent draft.",
    }


def test_patch_service_reorder_segments_updates_order_and_refreshes_routes(monkeypatch):
    clear_database()
    calls = []

    def fake_refresh(self, plan_id, preferred_mode=None, route_pairs=None):
        calls.append((plan_id, route_pairs))
        return ["路线已按新的游览顺序重新规划。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_routes", fake_refresh)
    _install_passed_provider_matrix(monkeypatch)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        base_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="reorder_segments",
                    dayId="day_patch",
                    orderedSegmentIds=["seg_patch_2", "seg_patch"],
                )
            ],
            base_version_id=base_version_id,
        )
        rows = connection.execute(
            "SELECT id, segment_order, start_time, end_time FROM itinerary_segments WHERE plan_id = ? ORDER BY segment_order ASC",
            (session.active_plan_id,),
        ).fetchall()

    assert [row["id"] for row in rows] == ["seg_patch_2", "seg_patch"]
    assert [row["segment_order"] for row in rows] == [1, 2]
    assert [(row["start_time"], row["end_time"]) for row in rows] == [("09:00", "10:00"), ("11:30", "13:30")]
    assert [segment.id for segment in result.itinerary.days[0].segments] == ["seg_patch_2", "seg_patch"]
    assert [(segment.start_time, segment.end_time) for segment in result.itinerary.days[0].segments] == [
        ("09:00", "10:00"),
        ("11:30", "13:30"),
    ]
    assert result.itinerary.route_warnings == []
    assert calls == []


@pytest.mark.parametrize(
    "ordered_ids, expected_error",
    [
        (["seg_patch"], "orderedSegmentIds must include every segment in the day exactly once"),
        (["seg_patch", "seg_patch"], "orderedSegmentIds must not contain duplicates"),
        (["seg_patch", "seg_missing"], "orderedSegmentIds must include every segment in the day exactly once"),
    ],
)
def test_patch_service_reorder_segments_rejects_invalid_lists(ordered_ids, expected_error):
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_timeline(connection, session.active_plan_id)
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [ItineraryPatchOperation(op="reorder_segments", dayId="day_patch", orderedSegmentIds=ordered_ids)],
            )
        rows = connection.execute(
            "SELECT id, segment_order FROM itinerary_segments WHERE plan_id = ? ORDER BY segment_order ASC",
            (session.active_plan_id,),
        ).fetchall()

    assert error.value.status_code == 400
    assert expected_error in error.value.detail["validationErrors"]
    assert [row["id"] for row in rows] == ["seg_patch", "seg_patch_2"]


def test_patch_service_reorder_segments_rejects_cross_day_segment(monkeypatch):
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        initial_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        _install_passed_provider_matrix(monkeypatch)
        add_day_result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="add_day", title="第二天")],
            base_version_id=initial_version_id,
        )
        second_day_id = connection.execute(
            "SELECT id FROM itinerary_days WHERE plan_id = ? AND day_number = 2",
            (session.active_plan_id,),
        ).fetchone()["id"]
        move_result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="move_segment",
                    segmentId="seg_patch_2",
                    targetDayId=second_day_id,
                    startTime="14:30",
                )
            ],
            base_version_id=add_day_result.version.id,
        )
        with pytest.raises(HTTPException) as error:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="reorder_segments", dayId="day_patch", orderedSegmentIds=["seg_patch", "seg_patch_2"]
                    )
                ],
                base_version_id=move_result.version.id,
            )

    assert error.value.status_code == 400
    assert "orderedSegmentIds cannot include segments from another day" in error.value.detail["validationErrors"]


class _FakeNearbyMapService:
    def __init__(self, pois: list[MapPoiResponse]):
        self.pois = pois
        self.calls = []

    def search_nearby(self, city, longitude, latitude, keyword, category="all", radius=1500, limit=12):
        self.calls.append(
            {
                "city": city,
                "longitude": longitude,
                "latitude": latitude,
                "keyword": keyword,
                "category": category,
                "radius": radius,
                "limit": limit,
            }
        )
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=self.pois,
        )


def amap_poi_fixture() -> MapPoiResponse:
    return MapPoiResponse(
        id="B000COFFEE",
        name="故宫旁咖啡馆",
        type="餐饮服务;咖啡厅",
        city="北京市",
        district="东城区",
        address="景山前街附近",
        longitude=116.398,
        latitude=39.919,
        category="food",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索，限定当前城市，extensions=all",
        confidence=0.91,
        photos=[MapPoiPhotoResponse(title="故宫旁咖啡馆", url="https://example.com/coffee.jpg")],
    )


def _route_decision_contract_context(context: dict | None = None) -> dict:
    mobility_profile = {
        "source": "patch_test_request_contract",
        "walkingPenaltyMinutesPerKm": 1.8,
        "transferPenaltyMinutes": 6.0,
        "waitTimeMultiplier": 1.0,
        "riskPenaltyMultiplier": 1.0,
    }
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"sourceAssistantTurnId": "turn_patch_test"},
        detour_tolerance={
            "maxGeneralizedCostDelta": 35.0,
            "maxDetourRatio": 0.35,
        },
        mobility_profile=mobility_profile,
    )
    assert contract is not None
    return {"routeDecisionContract": contract, **(context or {})}


def test_agent_route_write_without_explicit_contract_is_zero_write(monkeypatch):
    clear_database()
    calls = _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("合同缺失候选", 116.4102, 39.9201)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "合同缺失")
        seed_timeline(connection, session.active_plan_id)
        _install_passed_provider_matrix(monkeypatch)
        before_versions = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi",
                        segmentId="seg_patch_2",
                        amapPoi=candidate,
                    )
                ],
                source_type="agent",
                planning_context={"toolRefreshPolicy": {"route": "skip"}},
            )
        after_versions = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert "route_decision_contract_missing_or_invalid" in str(exc_info.value.detail)
    assert after_versions == before_versions
    assert calls == []


def test_manual_source_type_cannot_bypass_route_gate_with_client_contract(monkeypatch):
    clear_database()
    calls = _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("客户端伪造合同候选", 116.4102, 39.9201)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "客户端绕过")
        seed_timeline(connection, session.active_plan_id)
        before_versions = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi",
                        segmentId="seg_patch_2",
                        amapPoi=candidate,
                    )
                ],
                source_type="manual",
                planning_context=_route_decision_contract_context(),
            )
        after_versions = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert "route_decision_contract_missing_or_invalid" in str(exc_info.value.detail)
    assert after_versions == before_versions
    assert calls == []


def test_conflicting_server_route_contracts_fail_closed_before_business_write(monkeypatch):
    clear_database()
    calls = _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("冲突合同候选", 116.4102, 39.9201)
    nested = _route_decision_contract_context()["routeDecisionContract"]
    conflicting = _route_decision_contract_context()["routeDecisionContract"]
    conflicting["provenance"] = {"sourceAssistantTurnId": "tampered"}
    # A fingerprint no longer matching its body is also invalid; rebuild it so
    # this asserts the stricter conflict branch rather than simple corruption.
    conflicting = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"sourceAssistantTurnId": "different-server-turn"},
        detour_tolerance=conflicting["detourTolerance"],
        mobility_profile=conflicting["mobilityProfile"],
    )
    assert conflicting is not None
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "合同冲突")
        seed_timeline(connection, session.active_plan_id)
        before_versions = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        with pytest.raises(HTTPException) as exc_info:
            ItineraryPatchService(connection).apply_patch(
                session.active_plan_id,
                [
                    ItineraryPatchOperation(
                        op="replace_segment_poi",
                        segmentId="seg_patch_2",
                        amapPoi=candidate,
                    )
                ],
                source_type="agent",
                planning_context={
                    "requestIntentContract": {"routeDecisionContract": nested},
                },
                server_route_decision_contract=conflicting,
            )
        after_versions = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

    assert "route_decision_contract_conflict" in str(exc_info.value.detail)
    assert after_versions == before_versions
    assert calls == []


def test_nested_server_request_contract_is_lifted_for_route_write(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("服务器合同候选", 116.4102, 39.9201)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "服务器合同")
        seed_timeline(connection, session.active_plan_id)
        result = ItineraryPatchService(connection).apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch_2",
                    amapPoi=candidate,
                )
            ],
            source_type="agent",
            planning_context={
                "requestIntentContract": _route_decision_contract_context(),
                "toolRefreshPolicy": {"route": "skip"},
            },
        )

    assert result.version.id


def test_provider_matrix_rejects_naive_query_timestamp_before_write():
    leg = {
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "fromSegmentId": "seg_a",
        "toSegmentId": "seg_b",
        "fromAmapId": "B0A",
        "toAmapId": "B0B",
        "distanceMeters": 500,
        "durationSeconds": 300,
        "walkingDistanceMeters": 0,
        "transferCount": 0,
        "waitSeconds": 0,
        "riskPenaltyMinutes": 0,
        "queriedAt": "2026-08-09T10:00:00",
    }

    assert ItineraryPatchService._provider_matrix_leg_complete_before_write(leg) is False


def test_provider_matrix_rejects_missing_transport_mode_before_write():
    leg = {
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "fromSegmentId": "seg_a",
        "toSegmentId": "seg_b",
        "fromAmapId": "B0A",
        "toAmapId": "B0B",
        "distanceMeters": 500,
        "durationSeconds": 300,
        "walkingDistanceMeters": 0,
        "transferCount": 0,
        "waitSeconds": 0,
        "riskPenaltyMinutes": 0,
        "queriedAt": datetime.now(timezone.utc).isoformat(),
    }

    assert ItineraryPatchService._provider_matrix_leg_complete_before_write(leg) is False


def test_missing_plan_city_never_defaults_to_beijing_for_provider_or_skeleton_write():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    service = ItineraryPatchService(connection)
    connection.execute("CREATE TABLE itinerary_plans (id TEXT PRIMARY KEY, city TEXT)")
    connection.execute("INSERT INTO itinerary_plans (id, city) VALUES ('plan_missing_city', '')")

    with pytest.raises(HTTPException, match="itinerary_plan_city_missing_for_provider_preflight"):
        service._plan_city("plan_missing_city")
    with pytest.raises(HTTPException, match="itinerary_plan_city_missing_for_skeleton_poi"):
        service._insert_skeleton_poi("plan_missing_city", "待定地点")


def test_non_route_patch_preserves_active_route_contract_for_next_topology_write(monkeypatch):
    clear_database()
    _install_passed_provider_matrix(monkeypatch)
    candidate = _amap_poi_fixture("合同继承候选", 116.4102, 39.9201)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "合同继承")
        initial_version_id = seed_timeline(connection, session.active_plan_id, with_route_contract=True)
        service = ItineraryPatchService(connection)
        title_result = service.apply_patch(
            session.active_plan_id,
            [ItineraryPatchOperation(op="replace_trip_title", value="仍保留路线合同")],
            base_version_id=initial_version_id,
        )
        carried_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (title_result.version.id,),
            ).fetchone()[0]
        )
        topology_result = service.apply_patch(
            session.active_plan_id,
            [
                ItineraryPatchOperation(
                    op="replace_segment_poi",
                    segmentId="seg_patch_2",
                    amapPoi=candidate,
                )
            ],
            base_version_id=title_result.version.id,
        )

    assert carried_snapshot["routeDecisionContract"] == _route_decision_contract_context()["routeDecisionContract"]
    assert topology_result.version.id

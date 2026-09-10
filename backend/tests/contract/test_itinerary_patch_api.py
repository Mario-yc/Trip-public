import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.schemas.maps import MapPoiResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app
from src.models.route_option import RouteOption
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.map_poi_service import MapPoiService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import RouteService


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
            DELETE FROM poi_risk_alerts;
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


@pytest.fixture(autouse=True)
def recorded_public_amap_detail(monkeypatch):
    names = {
        "B000BREAKFAST": "高德早餐店",
        "B000DINNER": "高德晚餐店",
        "B000000001": "高德详情餐厅",
    }

    def detail(_self, amap_id: str, *, bypass_cache: bool = False) -> MapPoiResponse:
        del bypass_cache
        normalized = str(amap_id or "").strip().upper()
        if normalized not in names:
            raise AssertionError(f"Missing recorded AMap detail fixture for {normalized}")
        return MapPoiResponse.model_validate(amap_poi_payload(normalized, names[normalized]))

    monkeypatch.setattr(MapPoiService, "detail", detail)


def business_write_counts() -> tuple[int, int, int]:
    with open_db() as connection:
        return (
            int(connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]),
            int(connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]),
            int(connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0]),
        )


def canonical_route_decision_contract() -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="contract_test_active_version",
        provenance={"issuer": "test_itinerary_patch_api"},
        detour_tolerance={
            "maxGeneralizedCostDelta": 60.0,
            "maxDetourRatio": 3.0,
        },
        mobility_profile={
            "source": "contract_test_active_version",
            "walkingPenaltyMinutesPerKm": 2.0,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert contract is not None
    return contract


def persist_active_route_decision_contract(plan_id: str) -> str:
    """Install the server fixture policy on the exact active version."""

    with open_db() as connection:
        session = connection.execute(
            "SELECT id, active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
            (plan_id,),
        ).fetchone()
        assert session is not None
        snapshot_service = ItinerarySnapshotService(connection)
        snapshot = snapshot_service.capture_snapshot(plan_id)
        snapshot["routeDecisionContract"] = canonical_route_decision_contract()
        version = snapshot_service.save_version(
            str(session["id"]),
            plan_id,
            "contract_test_server_fixture",
            snapshot=snapshot,
        )
        connection.commit()
        return version.id


def install_deterministic_provider_matrix(monkeypatch) -> list:
    """Return fresh canonical Provider legs for route-contract API tests."""

    calls = []

    def build_routes(
        _self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **_kwargs,
    ):
        segments = list(segments or [])
        segment_by_id = {segment.id: segment for segment in segments}
        pairs = list(route_pairs or [])
        if not pairs:
            pairs = [(left.id, right.id) for left, right in zip(segments, segments[1:])]
        calls.append((str(plan_id), list(pairs)))
        poi_by_id = {poi.id: poi for poi in pois or []}
        routes = []
        for index, (left_id, right_id) in enumerate(pairs):
            left = segment_by_id[left_id]
            right = segment_by_id[right_id]
            left_poi = poi_by_id[left.poi_id]
            right_poi = poi_by_id[right.poi_id]
            routes.append(
                RouteOption(
                    id=f"route_contract_fixture_{index}_{left_id}_{right_id}",
                    plan_id=plan_id,
                    from_segment_id=left_id,
                    to_segment_id=right_id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=240,
                    duration_seconds=120,
                    mode=transport_mode,
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[
                        [left_poi.longitude, left_poi.latitude],
                        [right_poi.longitude, right_poi.latitude],
                    ],
                    steps=[],
                    provider_payload={
                        "walkingDistanceMeters": 0,
                        "transferCount": 0,
                        "waitSeconds": 0,
                        "riskPenaltyMinutes": 0,
                    },
                    queried_at=datetime.now(timezone.utc),
                )
            )
        return routes

    monkeypatch.setattr(RouteService, "build_routes", build_routes)
    return calls


def seed_timeline(plan_id: str) -> None:
    with open_db() as connection:
        connection.execute("DELETE FROM itinerary_segments WHERE plan_id = ?", (plan_id,))
        connection.execute("DELETE FROM itinerary_days WHERE plan_id = ?", (plan_id,))
        connection.execute("DELETE FROM pois WHERE plan_id = ?", (plan_id,))
        connection.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence, amap_id, type, district,
                address, source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_contract",
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
            ("day_contract", plan_id, 1, None, "初始 Day", "晴", "低风险", 60),
        )
        connection.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time,
                end_time, poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_contract",
                plan_id,
                "day_contract",
                1,
                "activity",
                "09:00",
                "11:00",
                "poi_contract",
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
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence, amap_id, type, district,
                address, source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "poi_contract_2",
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
                id, plan_id, day_id, segment_order, kind, start_time,
                end_time, poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "seg_contract_2",
                plan_id,
                "day_contract",
                2,
                "activity",
                "12:30",
                "13:30",
                "poi_contract_2",
                "walk",
                10,
                "第二段说明",
                None,
                None,
                None,
            ),
        )
        connection.commit()


def test_patch_and_restore_itinerary_version_api(monkeypatch):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)

        first_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "sourceType": "manual",
                "baseVersionId": base_version_id,
                "operations": [
                    {"op": "replace_trip_title", "value": "北京第一版"},
                    {"op": "replace_day_title", "dayId": "day_contract", "value": "故宫上午"},
                    {"op": "replace_segment_start_time", "segmentId": "seg_contract", "value": "10:00"},
                ],
            },
        )
        assert first_patch.status_code == 200
        first_body = first_patch.json()

        second_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "sourceType": "manual",
                "baseVersionId": first_body["version"]["id"],
                "operations": [{"op": "replace_trip_title", "value": "北京第二版"}],
            },
        )
        assert second_patch.status_code == 200

        restored = client.post(
            f"/api/itineraries/{plan_id}/restore-version",
            json={"versionId": first_body["version"]["id"], "reason": "contract_test"},
        )

    assert first_body["itinerary"]["title"] == "北京第一版"
    assert first_body["itinerary"]["days"][0]["title"] == "故宫上午"
    assert first_body["itinerary"]["days"][0]["segments"][0]["startTime"] == "10:00"
    assert first_body["patch"]["validationStatus"] == "accepted"
    assert restored.status_code == 200
    restored_body = restored.json()
    assert restored_body["itinerary"]["title"] == "北京第一版"
    assert restored_body["itinerary"]["days"][0]["segments"][0]["startTime"] == "10:00"
    assert restored_body["version"]["sourceType"] == "restore"
    with open_db() as connection:
        plan = connection.execute("SELECT title FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        segment = connection.execute(
            "SELECT start_time FROM itinerary_segments WHERE id = ?", ("seg_contract",)
        ).fetchone()
        session = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?", (plan_id,)
        ).fetchone()
    assert plan["title"] == "北京第一版"
    assert segment["start_time"] == "10:00"
    assert session["active_version_id"] == restored_body["version"]["id"]


def test_replace_transport_mode_uses_versioned_patch_and_rejects_stale_base_version(
    monkeypatch,
):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)

        first_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "operations": [{"op": "replace_trip_title", "value": "北京交通测试"}],
            },
        )
        assert first_patch.status_code == 200

        transport_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "sourceType": "manual",
                "baseVersionId": first_patch.json()["version"]["id"],
                "operations": [{"op": "replace_transport_mode", "segmentId": "seg_contract", "value": "taxi"}],
            },
        )
        assert transport_patch.status_code == 200
        body = transport_patch.json()

        stale_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "sourceType": "manual",
                "baseVersionId": first_patch.json()["version"]["id"],
                "operations": [{"op": "replace_transport_mode", "segmentId": "seg_contract", "value": "walk"}],
            },
        )

    assert body["patch"]["validationStatus"] == "accepted"
    assert body["version"]["id"] != first_patch.json()["version"]["id"]
    assert body["itinerary"]["days"][0]["segments"][0]["transportMode"] == "taxi"
    assert stale_patch.status_code == 409
    with open_db() as connection:
        segment = connection.execute(
            "SELECT transport_mode FROM itinerary_segments WHERE id = ?",
            ("seg_contract",),
        ).fetchone()
        session = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
            (plan_id,),
        ).fetchone()
        patch = connection.execute(
            "SELECT operations_json, result_version_id FROM itinerary_patches WHERE result_version_id = ?",
            (body["version"]["id"],),
        ).fetchone()
    assert segment["transport_mode"] == "taxi"
    assert session["active_version_id"] == body["version"]["id"]
    assert patch["result_version_id"] == body["version"]["id"]
    assert json.loads(patch["operations_json"])[0]["op"] == "replace_transport_mode"


def test_active_patch_rejects_missing_base_version_without_changing_active_version():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)

        first_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={"operations": [{"op": "replace_trip_title", "value": "北京第一版"}]},
        )
        assert first_patch.status_code == 200
        first_version_id = first_patch.json()["version"]["id"]

        missing_base_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={"operations": [{"op": "replace_trip_title", "value": "不应写入"}]},
        )

    assert missing_base_patch.status_code == 409
    with open_db() as connection:
        session_row = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
            (plan_id,),
        ).fetchone()
        plan = connection.execute("SELECT title FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
    assert session_row["active_version_id"] == first_version_id
    assert plan["title"] == "北京第一版"


def test_get_itinerary_is_read_only_and_does_not_advance_active_version():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        first_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={"operations": [{"op": "replace_trip_title", "value": "北京只读测试"}]},
        )
        assert first_patch.status_code == 200
        active_version_id = first_patch.json()["version"]["id"]
        with open_db() as connection:
            before = {
                "versions": connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0],
                "patches": connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0],
                "planning_runs": connection.execute("SELECT COUNT(*) FROM planning_runs").fetchone()[0],
                "active_version_id": connection.execute(
                    "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
                    (plan_id,),
                ).fetchone()["active_version_id"],
            }

        response = client.get(f"/api/itineraries/{plan_id}")

    assert response.status_code == 200
    assert response.json()["plan"]["title"] == "北京只读测试"
    with open_db() as connection:
        after = {
            "versions": connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0],
            "patches": connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0],
            "planning_runs": connection.execute("SELECT COUNT(*) FROM planning_runs").fetchone()[0],
            "active_version_id": connection.execute(
                "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
                (plan_id,),
            ).fetchone()["active_version_id"],
        }
    assert before == after
    assert after["active_version_id"] == active_version_id


def test_invalid_patch_operation_is_rejected_before_audit_insert():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        with open_db() as connection:
            before_count = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]

        invalid = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={"operations": [{"op": "unsupported_direct_write", "value": "x"}]},
        )
        with open_db() as connection:
            after_count = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]

    assert invalid.status_code == 422
    assert invalid.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert invalid.json()["validationErrors"]
    assert after_count == before_count


def test_patch_request_rejects_extra_top_level_and_operation_fields():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)

        extra_top_level = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "operations": [{"op": "replace_trip_title", "value": "多余字段测试"}],
                "directWrite": True,
            },
        )
        extra_operation_field = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "operations": [
                    {
                        "op": "replace_trip_title",
                        "value": "多余字段测试",
                        "unexpectedColumn": "should-not-pass",
                    }
                ],
            },
        )

    assert extra_top_level.status_code == 422
    assert extra_top_level.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert extra_top_level.json()["validationErrors"]
    assert extra_operation_field.status_code == 422
    assert extra_operation_field.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert extra_operation_field.json()["validationErrors"]


def test_public_patch_cannot_promote_client_route_contract_or_source_type(
    monkeypatch,
):
    clear_database()
    provider_calls = install_deterministic_provider_matrix(monkeypatch)
    with TestClient(app) as client:
        session = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "北京会话"},
        ).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        with open_db() as connection:
            before = {
                "versions": connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0],
                "routes": connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0],
                "transportMode": connection.execute(
                    "SELECT transport_mode FROM itinerary_segments WHERE id = ?",
                    ("seg_contract",),
                ).fetchone()[0],
            }

        client_contract = canonical_route_decision_contract()
        spoofed_source = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "sourceType": "agent",
                "operations": [
                    {
                        "op": "replace_transport_mode",
                        "segmentId": "seg_contract",
                        "value": "taxi",
                    }
                ],
            },
        )
        response = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "planningContext": {
                    "routeDecisionContract": client_contract,
                    "requestIntentContract": {
                        "routeDecisionContract": client_contract,
                    },
                },
                "operations": [
                    {
                        "op": "replace_transport_mode",
                        "segmentId": "seg_contract",
                        "value": "taxi",
                    }
                ],
            },
        )

    assert spoofed_source.status_code == 422
    assert response.status_code == 400
    assert "route_decision_contract_missing_or_invalid" in json.dumps(response.json(), ensure_ascii=False)
    with open_db() as connection:
        after = {
            "versions": connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0],
            "routes": connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0],
            "transportMode": connection.execute(
                "SELECT transport_mode FROM itinerary_segments WHERE id = ?",
                ("seg_contract",),
            ).fetchone()[0],
        }
        rejected_patch = connection.execute(
            """
            SELECT source_type, validation_status, result_version_id
            FROM itinerary_patches
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert after == before
    assert provider_calls == []
    assert tuple(rejected_patch) == ("manual", "rejected", None)


def test_pending_poi_confirm_is_persisted_with_successful_patch_and_not_on_rejected_patch(
    monkeypatch,
):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        session_id = session["sessionId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)
        with open_db() as connection:
            connection.execute(
                """
                INSERT INTO amap_poi_candidates (
                    id, session_id, turn_id, query, city, category, status,
                    candidates_json, selected_amap_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cand_food",
                    session_id,
                    None,
                    "老胡同餐厅",
                    "北京",
                    "food",
                    "pending",
                    json.dumps([amap_poi_payload("B0000FOOD", "老胡同餐厅")], ensure_ascii=False),
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
                    "cand_invalid",
                    session_id,
                    None,
                    "错误候选",
                    "北京",
                    "food",
                    "pending",
                    json.dumps([amap_poi_payload("B000BAD", "错误候选")], ensure_ascii=False),
                    None,
                    "2026-06-11T00:00:01Z",
                ),
            )
            connection.commit()

        rejected = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "planningContext": {"pendingPoiCandidateId": "cand_invalid"},
                "operations": [{"op": "add_segment", "dayId": "day_contract", "title": "错误候选"}],
            },
        )
        accepted = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "planningContext": {"pendingPoiCandidateId": "cand_food"},
                "operations": [
                    {
                        "op": "add_segment",
                        "dayId": "day_contract",
                        "startTime": "15:00",
                        "title": "老胡同餐厅",
                        "durationMinutes": 60,
                        "amapPoi": amap_poi_payload("B0000FOOD", "老胡同餐厅"),
                    }
                ],
            },
        )
        with open_db() as connection:
            after_accepted_counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("itinerary_versions", "itinerary_patches", "route_options")
            )
        duplicate = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": accepted.json()["version"]["id"],
                "planningContext": {"pendingPoiCandidateId": "cand_food"},
                "operations": [
                    {
                        "op": "add_segment",
                        "dayId": "day_contract",
                        "startTime": "15:00",
                        "title": "老胡同餐厅",
                        "durationMinutes": 60,
                        "amapPoi": amap_poi_payload("B0000FOOD", "老胡同餐厅"),
                    }
                ],
            },
        )
        loaded_session = client.get(f"/api/agent/sessions/{session_id}")

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["patch"]["id"] == accepted.json()["patch"]["id"]
    assert duplicate.json()["version"]["id"] == accepted.json()["version"]["id"]
    assert duplicate.json()["patch"]["metadata"] == {
        "idempotentReplay": True,
        "candidateId": "cand_food",
        "selectedAmapId": "B0000FOOD",
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
    }
    assert loaded_session.status_code == 200
    assert [candidate["id"] for candidate in loaded_session.json()["pendingPoiCandidates"]] == ["cand_invalid"]
    with open_db() as connection:
        selected = connection.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ?",
            ("cand_food",),
        ).fetchone()
        still_pending = connection.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ?",
            ("cand_invalid",),
        ).fetchone()
        after_duplicate_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        )
    assert selected["status"] == "selected"
    assert selected["selected_amap_id"] == "B0000FOOD"
    assert still_pending["status"] == "pending"
    assert still_pending["selected_amap_id"] is None
    assert after_duplicate_counts == after_accepted_counts


def test_pending_poi_reject_is_persisted_and_hidden_after_reload():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        session_id = session["sessionId"]
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
                    session_id,
                    None,
                    "不去的餐厅",
                    "北京",
                    "food",
                    "pending",
                    json.dumps([amap_poi_payload("B000REJECT", "不去的餐厅")], ensure_ascii=False),
                    None,
                    "2026-06-11T00:00:00Z",
                ),
            )
            connection.commit()

        rejected = client.post(f"/api/agent/sessions/{session_id}/pending-poi-candidates/cand_reject/reject")
        reloaded = client.get(f"/api/agent/sessions/{session_id}")

    assert rejected.status_code == 200
    assert rejected.json()["pendingPoiCandidates"] == []
    assert reloaded.status_code == 200
    assert reloaded.json()["pendingPoiCandidates"] == []
    with open_db() as connection:
        row = connection.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ?",
            ("cand_reject",),
        ).fetchone()
    assert row["status"] == "rejected"
    assert row["selected_amap_id"] is None


def test_patch_api_supports_phase_two_manual_operations_and_rejected_audit(
    monkeypatch,
):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)

        add_day = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "operations": [{"op": "add_day", "title": "第二天"}],
            },
        )
        assert add_day.status_code == 200
        second_day_id = add_day.json()["itinerary"]["days"][1]["id"]

        added = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": add_day.json()["version"]["id"],
                "operations": [
                    {
                        "op": "add_segment",
                        "dayId": second_day_id,
                        "startTime": "10:30",
                        "title": "待定早餐",
                        "notes": "先占位",
                        "amapPoi": amap_poi_payload("B000BREAKFAST", "高德早餐店"),
                    },
                    {
                        "op": "update_segment_notes",
                        "segmentId": "seg_contract",
                        "notes": "已更新说明",
                    },
                ],
            },
        )
        assert added.status_code == 200, added.json()
        breakfast_segment_id = next(
            segment["id"]
            for segment in added.json()["itinerary"]["days"][1]["segments"]
            if segment["poi"]["name"] == "高德早餐店"
        )

        combined = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": added.json()["version"]["id"],
                "operations": [
                    {
                        "op": "move_segment",
                        "segmentId": "seg_contract_2",
                        "targetDayId": second_day_id,
                        "startTime": "12:00",
                    },
                    {"op": "remove_segment", "segmentId": "seg_contract"},
                ],
            },
        )
        assert combined.status_code == 200, combined.json()
        second_day_segments = combined.json()["itinerary"]["days"][1]["segments"]

        reordered = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": combined.json()["version"]["id"],
                "operations": [
                    {
                        "op": "reorder_segments",
                        "dayId": second_day_id,
                        "orderedSegmentIds": [breakfast_segment_id, "seg_contract_2"],
                    }
                ],
            },
        )
        assert reordered.status_code == 200

        skeleton_segment = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": reordered.json()["version"]["id"],
                "operations": [
                    {"op": "add_segment", "dayId": second_day_id, "startTime": "14:30", "title": "未解析地点"}
                ],
            },
        )
        assert skeleton_segment.status_code == 400

        replace_poi = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": reordered.json()["version"]["id"],
                "operations": [
                    {
                        "op": "replace_segment_poi",
                        "segmentId": "seg_contract_2",
                        "notes": "换成高德确认餐厅",
                        "amapPoi": amap_poi_payload("B000DINNER", "高德晚餐店"),
                    }
                ],
            },
        )
        assert replace_poi.status_code == 200, replace_poi.json()

        rejected = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": replace_poi.json()["version"]["id"],
                "operations": [{"op": "replace_segment_start_time", "segmentId": "seg_contract_2", "value": "25:00"}],
            },
        )

    assert combined.json()["itinerary"]["days"][1]["segments"][0]["poi"]["source"] == "amap-place-search"
    assert combined.json()["itinerary"]["days"][1]["segments"][1]["id"] == "seg_contract_2"
    assert [segment["id"] for segment in reordered.json()["itinerary"]["days"][1]["segments"]] == [
        breakfast_segment_id,
        "seg_contract_2",
    ]
    assert "add_segment requires resolved AMap POI" in skeleton_segment.json()["detail"]["validationErrors"]
    replaced_segment = next(
        segment
        for segment in replace_poi.json()["itinerary"]["days"][1]["segments"]
        if segment["id"] == "seg_contract_2"
    )
    assert replaced_segment["poi"]["name"] == "高德晚餐店"
    assert rejected.status_code == 400
    with open_db() as connection:
        rejected_patch = connection.execute(
            "SELECT * FROM itinerary_patches WHERE validation_status = 'rejected' ORDER BY created_at LIMIT 1"
        ).fetchone()
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        dinner_poi = connection.execute("SELECT * FROM pois WHERE amap_id = ?", ("B000DINNER",)).fetchone()
    assert rejected_patch is not None
    assert dinner_poi["source"] == "amap-place-search"
    assert dinner_poi["district"] == "东城区"
    assert version_count == 6


def test_local_replan_patch_returns_planning_run_and_applies_suggestion(monkeypatch):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    captured_refresh = {}

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        captured_refresh["plan_id"] = plan_id
        captured_refresh["preference_summary"] = preference_summary
        captured_refresh["planning_context"] = planning_context
        captured_refresh["preferred_mode"] = preferred_mode
        return ["工具刷新已接收局部重规划上下文。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)

        response = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "sourceType": "local_replan",
                "baseVersionId": base_version_id,
                "preferenceSummary": "用户偏好轻松不赶路，公共交通优先。",
                "planningContext": {
                    "currentPreferenceSummary": "用户偏好轻松不赶路，公共交通优先。",
                    "tripPurpose": "拍照",
                },
                "operations": [
                    {"op": "add_day", "title": "新增轻松安排"},
                    {
                        "op": "move_segment",
                        "segmentId": "seg_contract_2",
                        "targetDayId": "__new_day__",
                        "startTime": "09:30",
                    },
                ],
            },
        )
        body = response.json()

    assert response.status_code == 200
    assert body["itinerary"]["days"][1]["title"] == "新增轻松安排"
    assert body["itinerary"]["days"][1]["segments"][0]["id"] == "seg_contract_2"
    assert body["itinerary"]["days"][1]["segments"][0]["startTime"] == "09:30"
    assert body["planningRun"]["runType"] == "local_replan_apply"
    assert body["planningRun"]["itineraryVersionId"] == body["version"]["id"]
    assert body["planningRun"]["preferenceSummary"] == "用户偏好轻松不赶路，公共交通优先。"
    assert body["planningRun"]["feasibilityReport"]["score"] > 0
    assert captured_refresh["plan_id"] == plan_id
    assert captured_refresh["preference_summary"] == ("用户偏好轻松不赶路，公共交通优先。")
    assert captured_refresh["preferred_mode"] is None
    refresh_context = captured_refresh["planning_context"]
    assert refresh_context["currentPreferenceSummary"] == ("用户偏好轻松不赶路，公共交通优先。")
    assert refresh_context["tripPurpose"] == "拍照"
    assert refresh_context["routeDecisionContract"] == (
        RouteInsertionScorer.normalized_route_decision_contract(canonical_route_decision_contract())
    )
    assert refresh_context["routeDecisionContractSource"] == ("active_version_snapshot")
    assert refresh_context["routeInsertionProofs"] == []
    assert refresh_context["routeMatrixExpectedPairs"] == []
    with open_db() as connection:
        run = connection.execute("SELECT * FROM planning_runs WHERE id = ?", (body["planningRun"]["id"],)).fetchone()
    assert run is not None
    assert run["run_type"] == "local_replan_apply"


def test_legacy_itinerary_edit_is_deprecated_but_uses_versioned_patch(monkeypatch):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    captured_refresh = {}

    def fake_refresh(
        self,
        plan_id,
        preference_summary=None,
        planning_context=None,
        preferred_mode=None,
        commit_between_tools=False,
        route_pairs=None,
    ):
        captured_refresh["plan_id"] = plan_id
        captured_refresh["preference_summary"] = preference_summary
        captured_refresh["planning_context"] = planning_context
        captured_refresh["preferred_mode"] = preferred_mode
        return ["工具刷新已接收交通方式调整上下文。"]

    monkeypatch.setattr("src.services.itinerary_service.ItineraryService.refresh_planning_tools", fake_refresh)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)
        first_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "operations": [{"op": "replace_trip_title", "value": "北京兼容接口测试"}],
            },
        )
        assert first_patch.status_code == 200

        response = client.patch(
            f"/api/itineraries/{plan_id}",
            json={
                "operation": "replace_transport_mode",
                "segmentId": "seg_contract",
                "value": "taxi",
                "baseVersionId": first_patch.json()["version"]["id"],
                "preferenceSummary": "用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。",
                "planningContext": {
                    "currentPreferenceSummary": "用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。"
                },
            },
        )
        body = response.json()

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert body["plan"]["days"][0]["segments"][0]["transportMode"] == "taxi"
    assert body["plan"]["days"][0]["segments"][1]["transportMode"] == "walk"
    assert body["planningRun"]["runType"] == "itinerary_edit"
    assert body["planningRun"]["itineraryVersionId"]
    assert body["planningRun"]["preferenceSummary"] == "用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。"
    assert body["planningRun"]["feasibilityReport"]["score"] <= 100
    assert any(tool["id"] == "map-route" for tool in body["planningRun"]["toolCalls"])
    # The guarded Provider matrix has already persisted the selected legs;
    # issuing the legacy route refresh again would create a second authority.
    assert captured_refresh == {}
    with open_db() as connection:
        run = connection.execute("SELECT * FROM planning_runs WHERE id = ?", (body["planningRun"]["id"],)).fetchone()
        patch = connection.execute(
            """
            SELECT * FROM itinerary_patches
            WHERE source_type = 'legacy_edit'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        session = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
            (plan_id,),
        ).fetchone()
        version_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (body["planningRun"]["itineraryVersionId"],),
            ).fetchone()["snapshot_json"]
        )
        provider_routes = connection.execute(
            """
            SELECT provider, distance_meters, duration_seconds
            FROM route_options
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchall()
    assert run is not None
    assert run["run_type"] == "itinerary_edit"
    assert patch is not None
    assert patch["validation_status"] == "accepted"
    assert patch["result_version_id"] == body["planningRun"]["itineraryVersionId"]
    assert patch["planning_run_id"] == body["planningRun"]["id"]
    assert session["active_version_id"] == body["planningRun"]["itineraryVersionId"]
    proofs = version_snapshot["routeInsertionProofs"]
    assert proofs
    assert all(proof["networkVerified"] is True for proof in proofs)
    assert all(proof["timeWindowFeasible"] is True for proof in proofs)
    expected_fingerprint = canonical_route_decision_contract()["fingerprint"]
    assert all(proof["routeDecisionContract"]["fingerprint"] == expected_fingerprint for proof in proofs)
    assert provider_routes
    assert all(row["provider"] == "amap-webservice" for row in provider_routes)
    assert all(row["distance_meters"] > 0 for row in provider_routes)
    assert all(row["duration_seconds"] > 0 for row in provider_routes)


def test_legacy_itinerary_edit_rejects_missing_base_version_without_changing_active_version():
    clear_database()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        first_patch = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={"operations": [{"op": "replace_trip_title", "value": "北京兼容接口测试"}]},
        )
        assert first_patch.status_code == 200
        first_version_id = first_patch.json()["version"]["id"]

        response = client.patch(
            f"/api/itineraries/{plan_id}",
            json={
                "operation": "replace_transport_mode",
                "segmentId": "seg_contract",
                "value": "taxi",
            },
        )

    assert response.status_code == 409
    with open_db() as connection:
        session_row = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
            (plan_id,),
        ).fetchone()
        segment = connection.execute(
            "SELECT transport_mode FROM itinerary_segments WHERE id = ?",
            ("seg_contract",),
        ).fetchone()
    assert session_row["active_version_id"] == first_version_id
    assert segment["transport_mode"] == "walk"


def test_public_patch_rebinds_direct_amap_poi_to_provider_detail(monkeypatch):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    canonical_payload = amap_poi_payload("B000000001", "高德详情餐厅")
    canonical_payload["address"] = "高德详情权威地址"

    def detail(_self, amap_id: str, *, bypass_cache: bool = False) -> MapPoiResponse:
        del bypass_cache
        assert amap_id == "B000000001"
        return MapPoiResponse.model_validate(canonical_payload)

    monkeypatch.setattr(MapPoiService, "detail", detail)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)
        requested = amap_poi_payload("B000000001", "高德详情餐厅")
        requested["address"] = "客户端不可采信地址"
        response = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "operations": [
                    {
                        "op": "replace_segment_poi",
                        "segmentId": "seg_contract_2",
                        "amapPoi": requested,
                    }
                ],
            },
        )

    assert response.status_code == 200, response.json()
    with open_db() as connection:
        stored = connection.execute(
            "SELECT name, address, longitude, latitude, source FROM pois WHERE amap_id = ? ORDER BY rowid DESC LIMIT 1",
            ("B000000001",),
        ).fetchone()
    assert stored is not None
    assert stored["name"] == "高德详情餐厅"
    assert stored["address"] == "高德详情权威地址"
    assert stored["longitude"] == pytest.approx(116.398)
    assert stored["latitude"] == pytest.approx(39.919)
    assert stored["source"] == "amap-place-search"


@pytest.mark.parametrize(
    ("failure_mode", "expected_status"),
    [("identity_mismatch", 400), ("provider_failure", 502)],
)
def test_public_patch_poi_identity_failure_has_zero_business_writes(
    monkeypatch,
    failure_mode: str,
    expected_status: int,
):
    clear_database()
    install_deterministic_provider_matrix(monkeypatch)
    canonical = MapPoiResponse.model_validate(amap_poi_payload("B000000001", "高德详情餐厅"))

    def detail(_self, _amap_id: str, *, bypass_cache: bool = False) -> MapPoiResponse:
        del bypass_cache
        if failure_mode == "provider_failure":
            raise HTTPException(status_code=502, detail="recorded AMap detail unavailable")
        return canonical

    monkeypatch.setattr(MapPoiService, "detail", detail)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)
        before = business_write_counts()
        requested = amap_poi_payload("B000000001", "伪造餐厅")
        requested["longitude"] = 121.500001
        requested["latitude"] = 31.200001
        response = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "operations": [
                    {
                        "op": "replace_segment_poi",
                        "segmentId": "seg_contract_2",
                        "amapPoi": requested,
                    }
                ],
            },
        )
        after = business_write_counts()

    assert response.status_code == expected_status
    assert after == before


def test_public_patch_rejects_noncanonical_amap_id_before_provider_call(monkeypatch):
    clear_database()
    provider_calls = []

    def detail(_self, amap_id: str, *, bypass_cache: bool = False) -> MapPoiResponse:
        provider_calls.append((amap_id, bypass_cache))
        raise AssertionError("provider detail must not receive a noncanonical identity")

    monkeypatch.setattr(MapPoiService, "detail", detail)
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京", "title": "北京会话"}).json()
        plan_id = session["activePlanId"]
        seed_timeline(plan_id)
        base_version_id = persist_active_route_decision_contract(plan_id)
        before = business_write_counts()
        response = client.post(
            f"/api/itineraries/{plan_id}/patch",
            json={
                "baseVersionId": base_version_id,
                "operations": [
                    {
                        "op": "replace_segment_poi",
                        "segmentId": "seg_contract_2",
                        "amapPoi": amap_poi_payload("amap_forged", "伪造地点"),
                    }
                ],
            },
        )
        after = business_write_counts()

    assert response.status_code == 400
    assert provider_calls == []
    assert after == before


def amap_poi_payload(amap_id: str, name: str) -> dict:
    return {
        "id": amap_id,
        "name": name,
        "type": "餐饮服务;中餐厅",
        "city": "北京市",
        "district": "东城区",
        "address": "东城高德地址",
        "longitude": 116.398,
        "latitude": 39.919,
        "category": "food",
        "source": "amap-place-search",
        "sourceNote": "高德 WebService POI 搜索，限定当前城市，extensions=all",
        "confidence": 0.91,
        "photos": [{"title": name, "url": f"https://example.com/{amap_id}.jpg"}],
    }

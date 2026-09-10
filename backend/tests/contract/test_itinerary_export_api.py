import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM saved_itinerary_versions;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_sessions;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM route_options;
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


def test_itinerary_save_marker_and_export_json_markdown_persist():
    clear_database()
    seed_export_plan("plan_export")
    client = TestClient(app)

    save_response = client.post("/api/itineraries/plan_export/versions/ver_export/save")
    list_response = client.get("/api/itineraries/plan_export/versions/saved")
    json_response = client.get("/api/itineraries/plan_export/export?format=json")
    markdown_response = client.get("/api/itineraries/plan_export/export?format=markdown")

    assert save_response.status_code == 200
    assert save_response.json()["versionId"] == "ver_export"
    assert list_response.status_code == 200
    assert list_response.json()["savedVersions"][0]["versionId"] == "ver_export"
    assert json_response.status_code == 200
    export_json = json_response.json()
    assert export_json["activeVersionId"] == "ver_export"
    assert export_json["planId"] == "plan_export"
    assert export_json["itineraryPlan"]["title"] == "北京导出测试"
    assert export_json["routeOptions"][0]["id"] == "route_export"
    budget = export_json["itineraryPlan"]["budgetBreakdown"]
    assert budget == {
        "tier": "unknown",
        "numericTarget": None,
        "knownActivityCost": 100.0,
        "knownMealCost": 0.0,
        "knownTransportCost": 5.0,
        "knownTotal": 105.0,
        "provisionalMin": 105.0,
        "provisionalPreferred": 105.0,
        "provisionalMax": 105.0,
        "unknownItems": ["未核验门票/预约相关收费"],
            "isComplete": False,
            "invariantValid": True,
        }
    assert "riskSignals" in export_json
    assert markdown_response.status_code == 200
    assert "# 北京导出测试" in markdown_response.text
    assert "Active Version：ver_export" in markdown_response.text
    assert "故宫博物院" in markdown_response.text
    assert "已知活动费用：¥100" in markdown_response.text
    assert "已知交通费用：¥5" in markdown_response.text
    assert "已知合计：¥105" in markdown_response.text
    assert "当前可估（暂估）：¥105（范围 ¥105–¥105）" in markdown_response.text
    assert "未知项：未核验门票/预约相关收费" in markdown_response.text
    assert "预算估算：¥220" not in markdown_response.text

    with open_db() as connection:
        saved = connection.execute("SELECT * FROM saved_itinerary_versions WHERE version_id = ?", ("ver_export",)).fetchall()
    assert len(saved) == 1


def seed_export_plan(plan_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "id": plan_id,
        "title": "北京导出测试",
        "city": "北京",
        "templateType": "custom",
        "budgetTarget": None,
        "budgetEstimate": 220,
        "budgetDeltaExplanation": "test",
        "decisionRationale": "test",
        "status": "draft",
        "days": [],
        "routeOptions": [],
        "weatherSignals": [],
        "trafficCrowdingSignals": [],
        "poiRiskAlerts": [],
        "ticketLookupResults": [],
        "routeWarnings": [],
        "localReplanSuggestions": [],
    }
    with open_db() as connection:
        connection.execute(
            """
            INSERT INTO itinerary_plans (
                id, user_id, inspiration_set_id, template_type, title, city,
                budget_target, budget_estimate, budget_delta_explanation,
                decision_rationale, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                get_settings().default_user_id,
                "insp_export",
                "custom",
                "北京导出测试",
                "北京",
                None,
                220,
                "test",
                "test",
                "draft",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_sessions (
                id, user_id, title, city, active_plan_id, active_version_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_export", get_settings().default_user_id, "北京导出测试", "北京", plan_id, "ver_export", "active", now, now),
        )
        connection.execute(
            """
            INSERT INTO itinerary_versions (
                id, session_id, plan_id, version_number, source_type,
                source_turn_id, source_patch_id, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("ver_export", "sess_export", plan_id, 1, "agent", None, None, json.dumps(snapshot, ensure_ascii=False), now),
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
                "poi_export_1",
                plan_id,
                "故宫博物院",
                "北京",
                "scenic",
                39.918,
                116.397,
                None,
                "amap-place-search",
                0.95,
                "B0001",
                "风景名胜",
                "东城区",
                "景山前街",
                "",
                None,
                "[]",
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
                "poi_export_2",
                plan_id,
                "天坛公园",
                "北京",
                "scenic",
                39.882,
                116.406,
                None,
                "amap-place-search",
                0.95,
                "B0002",
                "风景名胜",
                "东城区",
                "天坛路",
                "",
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
            ("day_export", plan_id, 1, "2026-10-01", "北京经典", "", "", 220),
        )
        for order, segment_id, poi_id, start, end, cost in [
            (1, "seg_export_1", "poi_export_1", "09:00", "12:00", 60),
            (2, "seg_export_2", "poi_export_2", "14:00", "16:00", 40),
        ]:
            connection.execute(
                """
                INSERT INTO itinerary_segments (
                    id, plan_id, day_id, segment_order, kind, start_time,
                    end_time, poi_id, transport_mode, estimated_cost, notes,
                    weather_signal_id, traffic_crowding_signal_id,
                    ticket_lookup_result_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (segment_id, plan_id, "day_export", order, "visit", start, end, poi_id, "public_transit", cost, "", None, None, None),
            )
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
                "route_export",
                plan_id,
                "seg_export_1",
                "seg_export_2",
                "poi_export_1",
                "poi_export_2",
                "amap-webservice",
                "transit",
                "公交/地铁",
                1,
                1,
                "transit",
                3200,
                1800,
                30,
                5,
                "CNY",
                5,
                "medium",
                "amap-webservice",
                "[]",
                "[]",
                "{}",
                None,
                now,
            ),
        )
        connection.commit()

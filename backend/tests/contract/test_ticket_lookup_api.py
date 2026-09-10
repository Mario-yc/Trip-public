import sqlite3

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app


def configure_amap_stub(monkeypatch) -> None:
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_route(_service, _from_poi, _to_poi, transport_mode):
        if transport_mode == "public_transit":
            return {
                "status": "1",
                "route": {"transits": [{"distance": "1800", "duration": "900", "cost": "4"}]},
            }
        return {
            "status": "1",
            "route": {"paths": [{"distance": "1800", "duration": "900"}]},
        }

    monkeypatch.setattr(
        "src.services.route_service.RouteService._fetch_amap_route",
        fake_route,
    )

    def fake_place(_service, _params):
        return {
            "status": "1",
            "pois": [
                {
                    "id": "B000A8UIN8",
                    "name": "故宫博物院",
                    "type": "风景名胜;风景名胜;世界遗产",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "景山前街4号",
                    "location": "116.397026,39.918058",
                    "photos": [{"title": "故宫博物院", "url": "https://example.com/palace.jpg"}],
                }
            ],
        }

    monkeypatch.setattr(
        "src.services.map_poi_service.MapPoiService._fetch_amap_place",
        fake_place,
    )


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM planning_runs;
            DELETE FROM ticket_lookup_results;
            DELETE FROM plan_comparisons;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM weather_signals;
            DELETE FROM route_options;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            DELETE FROM extraction_results;
            DELETE FROM source_materials;
            DELETE FROM inspiration_sets;
            """
        )


def test_compare_itineraries_returns_ticket_sources_and_fallback_metadata(monkeypatch):
    configure_amap_stub(monkeypatch)
    clear_database()
    with TestClient(app) as client:
        inspiration_response = client.post(
            "/api/inspirations",
            json={"cityHint": "北京", "textItems": ["北京 故宫博物院 拍照 预算 3000 元"], "socialLinks": []},
        )
        inspiration_id = inspiration_response.json()["inspirationSetId"]
        client.post(f"/api/inspirations/{inspiration_id}/extract")

        compare_response = client.post(
            "/api/itineraries/compare",
            json={"inspirationSetId": inspiration_id, "city": "北京"},
        )

    assert compare_response.status_code == 200
    body = compare_response.json()
    assert body["fallbackUsed"] is False
    assert body["providerName"] == "bocha-web-search"
    assert [plan["templateType"] for plan in body["plans"]] == ["low_budget", "photo_first", "relaxed_pace"]
    ticket_results = body["plans"][0]["ticketLookupResults"]
    assert [item["credibilityRank"] for item in ticket_results] == ["unavailable"]
    assert all(item["sourceName"] == "联网搜索失败" for item in ticket_results)
    assert all(item["sourceUrl"] == "" for item in ticket_results)
    assert all(item["bookingUrl"] == "" for item in ticket_results)
    assert all(item["priceEstimate"] == 0 for item in ticket_results)
    assert all(item["queriedAt"] for item in ticket_results)
    assert all("联网搜索未返回可用真实来源" in item["caveat"] for item in ticket_results)
    assert body["planningRun"]["sourceAssessments"]
    assert body["planningRun"]["sourceAssessments"][0]["credibilityRank"] == "unavailable"
    assert "联网查询失败" in body["planningRun"]["sourceAssessments"][0]["recommendation"]


def test_get_itinerary_is_read_only_and_does_not_refresh_ticket_status(monkeypatch):
    configure_amap_stub(monkeypatch)
    clear_database()
    with TestClient(app) as client:
        inspiration_response = client.post(
            "/api/inspirations",
            json={"cityHint": "北京", "textItems": ["北京 故宫博物院 拍照 预算 3000 元"], "socialLinks": []},
        )
        inspiration_id = inspiration_response.json()["inspirationSetId"]
        client.post(f"/api/inspirations/{inspiration_id}/extract")
        generated = client.post("/api/itineraries/generate", json={"inspirationSetId": inspiration_id, "city": "北京"})
        plan_id = generated.json()["plan"]["id"]
        db_path = sqlite_path_from_url(get_settings().database_url)
        with sqlite3.connect(db_path) as connection:
            before_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM planning_runs) AS planning_run_count,
                    (SELECT COUNT(*) FROM ticket_lookup_results) AS ticket_count
                """
            ).fetchone()

        itinerary_response = client.get(f"/api/itineraries/{plan_id}")
        with sqlite3.connect(db_path) as connection:
            after_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM planning_runs) AS planning_run_count,
                    (SELECT COUNT(*) FROM ticket_lookup_results) AS ticket_count
                """
            ).fetchone()

    assert itinerary_response.status_code == 200
    body = itinerary_response.json()
    plan = body["plan"]
    assert plan["ticketLookupResults"]
    assert plan["ticketLookupResults"][0]["providerName"] == "local-ticket-guard"
    assert plan["ticketLookupResults"][0]["status"] == "not_checked"
    assert plan["ticketLookupResults"][0]["fallbackUsed"] is False
    assert plan["ticketLookupResults"][0]["sourceUrl"] == ""
    assert plan["ticketLookupResults"][0]["bookingUrl"] == ""
    assert plan["ticketLookupResults"][0]["priceEstimate"] == 0
    assert "初始行程未全量查询预约状态" in plan["ticketLookupResults"][0]["caveat"]
    assert body["planningRun"] is None
    assert after_counts == before_counts


def test_explicit_ticket_refresh_endpoint_refreshes_ticket_status(monkeypatch):
    configure_amap_stub(monkeypatch)
    clear_database()
    with TestClient(app) as client:
        inspiration_response = client.post(
            "/api/inspirations",
            json={"cityHint": "北京", "textItems": ["北京 故宫博物院 拍照 预算 3000 元"], "socialLinks": []},
        )
        inspiration_id = inspiration_response.json()["inspirationSetId"]
        client.post(f"/api/inspirations/{inspiration_id}/extract")
        generated = client.post("/api/itineraries/generate", json={"inspirationSetId": inspiration_id, "city": "北京"})
        plan_id = generated.json()["plan"]["id"]

        refresh_response = client.post(f"/api/itineraries/{plan_id}/tickets/refresh")

    assert refresh_response.status_code == 200
    body = refresh_response.json()
    plan = body["plan"]
    assert plan["ticketLookupResults"]
    assert plan["ticketLookupResults"][0]["providerName"] == "bocha-web-search"
    assert plan["ticketLookupResults"][0]["fallbackUsed"] is False
    assert body["planningRun"]["runType"] == "ticket_lookup_refresh"
    assert body["planningRun"]["toolCalls"]
    ticket_tool = next(item for item in body["planningRun"]["toolCalls"] if item["id"] == "web-search-ticket")
    assert ticket_tool["status"] == "failed"
    assert ticket_tool["fallbackUsed"] is False
    assert ticket_tool["providerName"] == "bocha-web-search"
    assert body["planningRun"]["sourceAssessments"]
    assert body["planningRun"]["sourceAssessments"][0]["credibilityRank"] == "unavailable"

    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        run = connection.execute(
            "SELECT * FROM planning_runs WHERE run_type = 'ticket_lookup_refresh'"
        ).fetchone()
    assert run is not None

import json
import sqlite3

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.api.schemas.itineraries import ItineraryPlanResponse
from src.models.inspiration_set import InspirationSet
from src.services.plan_comparison_service import PlanComparisonService


def configure_amap_stub(monkeypatch) -> None:
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_place(_service, params):
        keyword = params.get("keywords", "高德地点")
        return {
            "status": "1",
            "pois": [
                {
                    "id": f"amap_{keyword}",
                    "name": keyword,
                    "type": "风景名胜",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": f"{keyword} 高德地址",
                    "location": "116.3972,39.9163" if keyword == "故宫博物院" else "116.4074,39.9042",
                    "photos": [],
                }
            ],
        }

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
        "src.services.map_poi_service.MapPoiService._fetch_amap_place",
        fake_place,
    )
    monkeypatch.setattr(
        "src.services.route_service.RouteService._fetch_amap_route",
        fake_route,
    )


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
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


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def seed_extraction(connection: sqlite3.Connection) -> InspirationSet:
    inspiration = InspirationSet(id="insp_us3", user_id=get_settings().default_user_id, city="北京", status="ready")
    connection.execute(
        """
        INSERT INTO inspiration_sets (
            id, user_id, city, status, theme_summary, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inspiration.id,
            inspiration.user_id,
            inspiration.city,
            inspiration.status,
            inspiration.theme_summary,
            inspiration.created_at.isoformat(),
            inspiration.updated_at.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO extraction_results (
            id, inspiration_set_id, city_candidates, poi_candidates, style_tags,
            budget_clues, route_clues, confidence, needs_user_confirmation,
            source_links, provider_name, fallback_used, provider_failure_reason,
            user_visible_caveat, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ext_us3",
            inspiration.id,
            json.dumps(["北京"], ensure_ascii=False),
            json.dumps(
                [
                    {"name": "故宫博物院", "confidence": 0.9, "sourceLinks": []},
                    {"name": "北京热门景点", "confidence": 0.55, "sourceLinks": []},
                ],
                ensure_ascii=False,
            ),
            json.dumps(["拍照优先"], ensure_ascii=False),
            json.dumps(["预算 3000 元"], ensure_ascii=False),
            json.dumps(["市中心一天"], ensure_ascii=False),
            0.82,
            0,
            json.dumps(["https://example.com/guide"], ensure_ascii=False),
            "mock-vision-provider",
            0,
            None,
            None,
            inspiration.created_at.isoformat(),
        ),
    )
    connection.commit()
    return inspiration


def test_plan_comparison_generates_three_distinct_templates_with_tickets(monkeypatch):
    configure_amap_stub(monkeypatch)
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        comparison = PlanComparisonService(connection).generate(inspiration.id, "北京")

    templates = [plan.template_type for plan in comparison.plans]
    assert templates == ["low_budget", "photo_first", "relaxed_pace"]
    assert len({plan.decision_rationale for plan in comparison.plans}) == 3
    assert comparison.plans[0].budget_estimate < comparison.plans[1].budget_estimate
    assert comparison.plans[2].budget_estimate >= comparison.plans[1].budget_estimate
    for plan in comparison.plans:
        assert plan.ticket_lookup_results
        assert all(result.caveat for result in plan.ticket_lookup_results)
        assert all(result.queried_at for result in plan.ticket_lookup_results)


def test_ticket_sources_are_sorted_by_credibility(monkeypatch):
    configure_amap_stub(monkeypatch)
    clear_database()
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        comparison = PlanComparisonService(connection).generate(inspiration.id, "北京")

    results = comparison.plans[0].ticket_lookup_results
    ranks = [result.credibility_rank for result in results]
    assert results
    assert all(rank == "unavailable" for rank in ranks)
    assert comparison.fallback_used is False
    assert comparison.provider_name == "bocha-web-search"
    assert all(result.fallback_used is False for result in results)
    assert all(result.provider_failure_reason for result in results)
    assert all(result.source_name == "联网搜索失败" for result in results)
    assert all(result.booking_url == "" for result in results)
    assert all(result.price_estimate == 0 for result in results)
    assert all("联网搜索未返回可用真实来源" in result.caveat for result in results)
    assert comparison.planning_run is not None
    assert comparison.planning_run.source_assessments
    assert comparison.planning_run.source_assessments[0].credibility_rank == "unavailable"
    assert "联网查询失败" in comparison.planning_run.source_assessments[0].recommendation


def test_plan_comparison_passes_weather_and_preference_context_to_each_template(monkeypatch):
    clear_database()
    captured_calls = []

    class FakeItineraryService:
        def __init__(self, _connection):
            pass

        def generate_for_inspiration(self, inspiration_set_id, city, **kwargs):
            captured_calls.append(
                {
                    "inspiration_set_id": inspiration_set_id,
                    "city": city,
                    **kwargs,
                }
            )
            return comparison_plan_fixture(str(kwargs["template_type"]))

        def get_plan(self, plan_id):
            return comparison_plan_fixture(plan_id.replace("plan_", ""))

    class FakeTicketService:
        def refresh_for_plan(self, _plan_id):
            return []

    monkeypatch.setattr("src.services.plan_comparison_service.ItineraryService", FakeItineraryService)
    with open_db() as connection:
        inspiration = seed_extraction(connection)
        PlanComparisonService(connection, ticket_service=FakeTicketService()).generate(
            inspiration.id,
            "北京",
            date_range={"start": "2026-10-16", "end": "2026-10-17"},
            preference_summary="用户偏好拍照优先，也担心下雨影响户外体验。",
            planning_context={"tripPurpose": "拍照", "weatherSensitivity": "怕雨"},
        )

    assert [call["template_type"] for call in captured_calls] == ["low_budget", "photo_first", "relaxed_pace"]
    for call in captured_calls:
        assert call["date_range"] == {"start": "2026-10-16", "end": "2026-10-17"}
        assert call["preference_summary"] == "用户偏好拍照优先，也担心下雨影响户外体验。"
        assert call["planning_context"]["tripPurpose"] == "拍照"
        assert call["planning_context"]["weatherSensitivity"] == "怕雨"


def comparison_plan_fixture(template_type: str) -> ItineraryPlanResponse:
    return ItineraryPlanResponse(
        id=f"plan_{template_type}",
        title=f"北京{template_type}",
        city="北京",
        templateType=template_type,
        budgetEstimate=120,
        budgetDeltaExplanation="demo",
        decisionRationale="demo",
        status="draft",
        days=[],
        routeOptions=[],
        weatherSignals=[],
        trafficCrowdingSignals=[],
        ticketLookupResults=[],
    )

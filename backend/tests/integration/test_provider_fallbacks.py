import sqlite3

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.models.ticket_lookup_result import TicketLookupResult
from src.providers.travel_tools import WebSearchItem, WebSearchResponse
from src.services.source_ranking_service import SourceRankingService
from src.services.ticket_service import TicketService


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM ticket_lookup_results;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            """
        )


def seed_single_segment_plan(connection: sqlite3.Connection, plan_id: str = "plan_fallback") -> None:
    connection.execute(
        """
        INSERT INTO itinerary_plans (
            id, user_id, inspiration_set_id, template_type, title, city,
            budget_target, budget_estimate, budget_delta_explanation,
            decision_rationale, status, created_at, updated_at
        ) VALUES (
            ?, 'local-demo-user', 'insp_fallback', 'custom',
            '北京地图行程草案', '北京', NULL, 120, 'demo', 'demo', 'draft',
            '2026-05-31T00:00:00+00:00', '2026-05-31T00:00:00+00:00'
        )
        """,
        (plan_id,),
    )
    connection.execute(
        """
        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude, source, confidence
        ) VALUES ('poi_fallback', ?, '故宫博物院', '北京', 'attraction', 39.9, 116.3, 'mock', 0.9)
        """,
        (plan_id,),
    )
    connection.execute(
        """
        INSERT INTO itinerary_segments (
            id, plan_id, day_id, segment_order, kind, start_time, end_time,
            poi_id, transport_mode, estimated_cost, notes
        ) VALUES (
            'seg_fallback', ?, 'day_fallback', 1, 'activity',
            '09:30', '11:30', 'poi_fallback', 'public_transit', 60, 'demo'
        )
        """,
        (plan_id,),
    )
    connection.commit()


def test_ticket_provider_failure_keeps_ticket_results_viewable_without_mock_data():
    clear_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_single_segment_plan(connection)

        results = TicketService(connection, default_available=False).refresh_for_plan("plan_fallback")

    assert results
    assert all(result.provider_name == "bocha-web-search" for result in results)
    assert all(result.fallback_used is False for result in results)
    assert all(result.credibility_rank == "unavailable" for result in results)
    assert all(result.source_name == "联网搜索失败" for result in results)
    assert all(result.provider_failure_reason for result in results)


def test_ticket_provider_success_uses_only_real_search_sources_without_example_com():
    clear_database()
    db_path = sqlite_path_from_url(get_settings().database_url)

    class FakeSearchProvider:
        observed_freshness = ""

        def search(self, _query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
            self.observed_freshness = freshness
            return WebSearchResponse(
                query="故宫博物院 开放时间 预约 门票 官方购票入口",
                results=[
                    WebSearchItem(
                        title="故宫博物院官方订票",
                        url="https://ticket.dpm.org.cn/",
                        snippet="故宫博物院门票预约入口。",
                        source_name="故宫博物院",
                        credibility_rank="official",
                        provider_name="duckduckgo-html-search",
                        confidence=0.82,
                    )
                ],
                provider_name="duckduckgo-html-search",
                confidence=0.82,
            )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_single_segment_plan(connection, "plan_real_sources")

        fake_provider = FakeSearchProvider()
        results = TicketService(connection, web_search_provider=fake_provider).refresh_for_plan("plan_real_sources")

    assert fake_provider.observed_freshness == "oneYear"
    assert len(results) == 1
    assert results[0].provider_name == "duckduckgo-html-search"
    assert results[0].source_url == "https://ticket.dpm.org.cn/"
    assert results[0].booking_url == "https://ticket.dpm.org.cn/"
    assert "example.com" not in results[0].source_url
    assert results[0].credibility_rank == "official"


def test_ticket_provider_preserves_chained_search_provider_name():
    clear_database()
    db_path = sqlite_path_from_url(get_settings().database_url)

    class FakeChainSearchProvider:
        def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
            return WebSearchResponse(
                query=query,
                results=[
                    WebSearchItem(
                        title="故宫博物院官方订票",
                        url="https://ticket.dpm.org.cn/",
                        snippet="故宫博物院门票预约入口。",
                        source_name="故宫博物院",
                        credibility_rank="official",
                        provider_name="baidu-html-search",
                        confidence=0.82,
                    )
                ],
                provider_name="chained-web-search",
                provider_diagnostics=[
                    {
                        "providerName": "baidu-html-search",
                        "status": "success",
                        "resultCount": 1,
                    }
                ],
                confidence=0.82,
            )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_single_segment_plan(connection, "plan_chain_sources")

        results = TicketService(connection, web_search_provider=FakeChainSearchProvider()).refresh_for_plan("plan_chain_sources")

    assert len(results) == 1
    assert results[0].provider_name == "chained-web-search"
    assert results[0].source_url == "https://ticket.dpm.org.cn/"
    assert results[0].credibility_rank == "official"


def test_ticket_provider_does_not_promote_nonofficial_or_irrelevant_sources_to_reservation_entry():
    clear_database()
    db_path = sqlite_path_from_url(get_settings().database_url)

    class SupplementalOnlyProvider:
        def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
            return WebSearchResponse(
                query=query,
                results=[
                    WebSearchItem(
                        title="北京大学访客参观预约攻略",
                        url="https://www.zhihu.com/question/123",
                        snippet="攻略称访客参观需要提前预约。",
                        source_name="知乎",
                        credibility_rank="community",
                        confidence=0.7,
                    ),
                    WebSearchItem(
                        title="北京大学夏令营招生报名",
                        url="https://www.pku.edu.cn/summer-camp",
                        snippet="研究生夏令营招生报名入口。",
                        source_name="北京大学",
                        credibility_rank="official",
                        confidence=0.9,
                    ),
                ],
                provider_name="test-search",
                confidence=0.8,
            )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_single_segment_plan(connection, "plan_supplemental_sources")
        connection.execute("UPDATE pois SET name = '北京大学' WHERE plan_id = ?", ("plan_supplemental_sources",))
        connection.commit()
        results = TicketService(connection, web_search_provider=SupplementalOnlyProvider()).refresh_for_plan(
            "plan_supplemental_sources"
        )

    assert len(results) == 1
    assert results[0].status == "unknown"
    assert results[0].booking_url == ""
    assert results[0].source_url == "https://www.zhihu.com/question/123"


def test_ticket_lookup_defers_non_concrete_area_without_search_or_fake_url():
    clear_database()
    db_path = sqlite_path_from_url(get_settings().database_url)

    class FailingIfCalledSearchProvider:
        called = False

        def search(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("area ticket guard should not call web search")

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_single_segment_plan(connection, "plan_area_ticket")
        connection.execute(
            "UPDATE pois SET name = ?, category = ? WHERE id = 'poi_fallback'",
            ("奥林匹克公园夜景", "area"),
        )
        connection.commit()

        fake_provider = FailingIfCalledSearchProvider()
        results = TicketService(connection, web_search_provider=fake_provider).refresh_for_plan("plan_area_ticket")

    assert fake_provider.called is False
    assert len(results) == 1
    assert results[0].status == "needs_concrete_poi"
    assert results[0].booking_url == ""
    assert results[0].source_url == ""
    assert "请选择具体场馆/入口/区域" in results[0].caveat
    assert "节假日管控需以官方公告为准" in results[0].caveat


def test_ticket_lookup_defers_functional_poi_without_search():
    clear_database()
    db_path = sqlite_path_from_url(get_settings().database_url)

    class FailingIfCalledSearchProvider:
        called = False

        def search(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("functional ticket guard should not call web search")

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_single_segment_plan(connection, "plan_functional_ticket")
        connection.execute(
            "UPDATE pois SET name = ?, category = ? WHERE id = 'poi_fallback'",
            ("晚餐", "pending"),
        )
        connection.commit()

        fake_provider = FailingIfCalledSearchProvider()
        results = TicketService(connection, web_search_provider=fake_provider).refresh_for_plan("plan_functional_ticket")

    assert fake_provider.called is False
    assert len(results) == 1
    assert results[0].status == "needs_concrete_poi"
    assert results[0].booking_url == ""
    assert "等待 Agent 按附近搜索补全具体地点" in results[0].caveat


def test_source_ranking_orders_known_credibility_levels():
    results = [
        TicketLookupResult("s", "seg", "attraction", "unknown", 0, "#", "search", "#", "search", "c", "mock", True, 0.4),
        TicketLookupResult("o", "seg", "attraction", "available", 0, "#", "official", "#", "official", "c", "mock", True, 0.8),
        TicketLookupResult("m", "seg", "attraction", "available", 0, "#", "map", "#", "map", "c", "mock", True, 0.75),
        TicketLookupResult("a", "seg", "attraction", "available", 0, "#", "aggregator", "#", "ota_aggregator", "c", "mock", True, 0.7),
    ]

    assert [item.id for item in SourceRankingService().sort_by_credibility(results)] == ["o", "m", "a", "s"]

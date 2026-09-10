from urllib.parse import unquote

from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.models.route_option import RouteOption
from src.models.ticket_lookup_result import TicketLookupResult
from src.models.traffic_crowding_signal import TrafficCrowdingSignal
from src.models.weather_signal import WeatherSignal
from src.providers.travel_tools import CheetahDuckDuckGoSearchProvider, WebSearchItem, WebSearchResponse
from src.services.poi_risk_service import AgentRiskSynthesisError, POIRiskService


def test_poi_risk_service_without_search_key_returns_unavailable_search_status():
    alerts = POIRiskService(search_provider_key="").build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture()],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-06-10",
            hourly_forecast=[],
            daily_summary="待 Agent 查询",
            risk_level="unavailable",
            purpose_impact_reason="天气不可用",
        ),
        traffic_signals=[],
        ticket_results=[],
    )

    assert len(alerts) == 1
    assert alerts[0].status == "unavailable"
    assert alerts[0].source_name == "搜索结果"
    assert alerts[0].source_url is None
    assert alerts[0].confidence == 0
    assert alerts[0].failure_reason == "search_provider_unavailable"
    assert "无法联网搜索" in alerts[0].user_visible_caveat
    assert alerts[0].sources[0]["type"] == "riskSearchDiagnostics"
    assert alerts[0].sources[1]["type"] == "webSearchProviderDiagnostics"
    assert alerts[0].sources[1]["failedProviders"] == ["bocha-web-search"]


def test_poi_risk_service_uses_search_result_sources_when_available():
    def fake_http_get(url: str, _timeout: float) -> dict:
        assert "query=" in url
        assert "%E5%8C%97%E4%BA%AC" in url
        return {
            "webPages": {
                "value": [
                    {
                        "name": "故宫博物院预约公告",
                        "url": "https://example.com/palace-notice",
                        "snippet": "故宫博物院暑期参观需要提前预约，部分入口施工绕行。",
                    },
                    {
                        "name": "北京交通提示",
                        "url": "https://example.com/traffic",
                        "snippet": "景山前街周边周末人流较大，建议错峰。",
                    },
                ]
            }
        }

    alerts = POIRiskService(
        search_provider_key="test-key",
        http_get=fake_http_get,
        risk_agent_provider=FakeRiskAgentProvider("Agent 判断：预约与施工会影响老人同行节奏。", 0.81),
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture()],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-06-10",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
    )

    assert alerts[0].status == "available"
    assert alerts[0].source_url == "https://example.com/palace-notice"
    assert alerts[0].confidence > 0
    assert "提前预约" in alerts[0].summary
    assert "Agent 判断" in alerts[0].summary
    assert alerts[0].sources[0]["title"] == "故宫博物院预约公告"


def test_poi_risk_service_degrades_when_agent_synthesis_is_unavailable_after_search():
    def fake_http_get(_url: str, _timeout: float) -> dict:
        return {
            "webPages": {
                "value": [
                    {
                        "name": "故宫博物院预约公告",
                        "url": "https://example.com/palace-notice",
                        "snippet": "故宫博物院参观需要提前预约。",
                    }
                ]
            }
        }

    alerts = POIRiskService(
        search_provider_key="test-key",
        http_get=fake_http_get,
        risk_agent_provider=FailingRiskAgentProvider(),
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture()],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-06-10",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
    )

    assert alerts[0].status == "degraded"
    assert "故宫博物院参观需要提前预约" in alerts[0].summary
    assert "Agent 风险判断服务暂不可用" in alerts[0].failure_reason
    assert "风险判断不完整" in alerts[0].user_visible_caveat


def test_poi_risk_service_search_query_and_summary_include_agent_context():
    captured_urls: list[str] = []

    def fake_http_get(url: str, _timeout: float) -> dict:
        captured_urls.append(url)
        return {
            "webPages": {
                "value": [
                    {
                        "name": "故宫博物院雨天排队与预约提示",
                        "url": "https://example.com/weather-ticket",
                        "snippet": "故宫博物院雨天安检排队时间变长，老人同行建议提前预约并减少户外排队。",
                    }
                ]
            }
        }

    risk_agent_provider = FakeRiskAgentProvider("结合老人、雨天和预约状态，建议提前到场并减少户外排队。", 0.76)
    alerts = POIRiskService(
        search_provider_key="test-key",
        http_get=fake_http_get,
        risk_agent_provider=risk_agent_provider,
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture()],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-16",
            hourly_forecast=[{"time": "09:00", "weather": "小雨"}],
            daily_summary="小雨，户外排队和拍照体验受影响",
            risk_level="bad_weather",
            purpose_impact_reason="拍照和老人同行会受降雨影响",
        ),
        traffic_signals=[
            TrafficCrowdingSignal(
                id="traffic_1",
                route_option_id="route_1",
                real_data_available=False,
                crowding_level="medium",
                estimated_reason="上午热门景区人流较高",
                recommended_departure_adjustment="提前 20 分钟出发",
            )
        ],
        ticket_results=[
            TicketLookupResult(
                id="ticket_1",
                segment_id="seg_1",
                ticket_type="reservation",
                status="需提前预约",
                price_estimate=60,
                booking_url="https://example.com/ticket",
                source_name="官方预约入口",
                source_url="https://example.com/ticket",
                credibility_rank="official",
                caveat="查询结果仅供参考，请以购票平台为准",
                provider_name="ticket-provider",
                fallback_used=False,
                confidence=0.82,
            )
        ],
        route_options=[route_fixture()],
        risk_context={
            "preferenceSummary": "两个大人一个老人，预算 3000 左右，不想太赶，拍照优先",
            "partySize": 3,
            "travelerTypes": ["老人"],
            "budgetRange": "3000 左右",
            "pacePreference": "轻松不赶路",
            "travelDateRange": {"start": "2026-10-16", "end": "2026-10-17"},
            "tripPurpose": "拍照和历史文化",
            "riskPriorityTerms": ["步行强度", "排队强度"],
            "travelerSensitivity": "high",
        },
    )

    decoded_url = unquote(captured_urls[0])
    assert len(decoded_url) < 260
    assert "故宫博物院" in decoded_url
    assert "2026" in decoded_url
    assert "官方公告" in decoded_url
    assert "老人" not in decoded_url
    assert "3000" not in decoded_url
    assert "需提前预约" not in decoded_url
    assert "bad_weather" not in decoded_url
    assert "public_transit" not in decoded_url
    assert "18分钟" not in decoded_url
    assert "1900米" not in decoded_url
    assert "步行强度" in decoded_url
    assert "排队强度" in decoded_url
    assert "high" not in decoded_url
    assert alerts[0].status == "degraded"
    assert "结合当前行程上下文" in alerts[0].summary
    assert "老人" in alerts[0].summary
    assert "预算 3000 左右" in alerts[0].summary
    assert "建议提前到场" in alerts[0].summary
    assert risk_agent_provider.payload["routes"][0] == {
        "id": "route_1",
        "mode": "transit",
        "label": "公交/地铁",
        "durationMinutes": 18,
        "distanceMeters": 1900,
        "costAmount": 4.0,
        "crowdingRisk": "medium",
        "isSelected": True,
    }


def test_poi_risk_service_writes_provider_diagnostics_to_alert_sources():
    search_provider = FakeWebSearchProvider(
        WebSearchResponse(
            query="北京大学 2026 国庆 官方公告",
            results=[
                WebSearchItem(
                    title="北京大学 2026 国庆校园参观预约官方公告",
                    url="https://www.pku.edu.cn/notice/2026",
                    snippet="2026年国庆期间校园参观需提前预约。",
                    source_name="北京大学",
                    confidence=0.82,
                    credibility_rank="official",
                    provider_name="brave-web-search",
                )
            ],
            provider_name="chained-web-search",
            provider_diagnostics=[
                {"providerName": "tavily", "status": "failed", "reason": "timeout", "resultCount": 0},
                {"providerName": "brave-web-search", "status": "success", "reason": "ok", "resultCount": 1},
            ],
            attempted_providers=["tavily", "brave-web-search"],
            successful_providers=["brave-web-search"],
            failed_providers=["tavily"],
            skipped_providers=[],
        )
    )

    alerts = POIRiskService(
        web_search_provider=search_provider,
        risk_agent_provider=FakeRiskAgentProvider("建议提前预约。", 0.8),
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(name="北京大学", category="education")],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    diagnostics = [source for source in alerts[0].sources if source.get("type") == "webSearchProviderDiagnostics"][0]
    risk_diagnostics = [source for source in alerts[0].sources if source.get("type") == "riskSearchDiagnostics"][0]
    assert alerts[0].status == "degraded"
    assert diagnostics["attemptedProviders"] == ["tavily", "brave-web-search"]
    assert diagnostics["successfulProviders"] == ["brave-web-search"]
    assert diagnostics["providerDiagnostics"][0]["reason"] == "timeout"
    assert risk_diagnostics["riskStatusReason"] == "search_success_degraded"
    assert risk_diagnostics["queryLength"] <= 180


def test_cheetah_ddg_success_makes_risk_search_available():
    html = """
    <html><body>
      <h2 class="result__title">
        <a href="https://www.pku.edu.cn/notice/2026">北京大学 2026 国庆校园参观预约官方公告</a>
      </h2>
      <a class="result__snippet">2026年国庆期间校园参观需提前预约。</a>
    </body></html>
    """

    def fake_http_get(_url: str, _timeout: float, _headers=None, _params=None, _proxy_url: str = "") -> str:
        return html

    search_provider = CheetahDuckDuckGoSearchProvider(http_get=fake_http_get)
    alerts = POIRiskService(
        web_search_provider=search_provider,
        risk_agent_provider=FakeRiskAgentProvider("建议提前预约。", 0.8),
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(name="北京大学", category="education")],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    provider_diagnostics = [source for source in alerts[0].sources if source.get("type") == "webSearchProviderDiagnostics"][0]
    assert alerts[0].status in {"available", "degraded"}
    assert provider_diagnostics["attemptedProviders"] == ["cheetah-duckduckgo-html-search"]
    assert provider_diagnostics["successfulProviders"] == ["cheetah-duckduckgo-html-search"]
    assert provider_diagnostics["providerDiagnostics"][0]["parserName"] == "cheetahclaws_result_regex"


def test_poi_risk_service_marks_stale_or_low_credibility_results_without_losing_diagnostics():
    search_provider = FakeWebSearchProvider(
        WebSearchResponse(
            query="北京大学 2026 国庆 官方公告",
            results=[
                WebSearchItem(
                    title="北京大学 2024 国庆参观攻略",
                    url="https://travel.example.com/pku-2024",
                    snippet="2024年国庆校园参观攻略。",
                    source_name="旅行攻略",
                    confidence=0.7,
                    credibility_rank="guide",
                    provider_name="tavily",
                )
            ],
            provider_name="chained-web-search",
            provider_diagnostics=[{"providerName": "tavily", "status": "success", "reason": "ok", "resultCount": 1}],
            attempted_providers=["tavily"],
            successful_providers=["tavily"],
        )
    )

    alerts = POIRiskService(web_search_provider=search_provider, risk_agent_provider=FakeRiskAgentProvider("", 0)).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(name="北京大学", category="education")],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    risk_diagnostics = [source for source in alerts[0].sources if source.get("type") == "riskSearchDiagnostics"][0]
    provider_diagnostics = [source for source in alerts[0].sources if source.get("type") == "webSearchProviderDiagnostics"][0]
    assert alerts[0].status == "unavailable"
    assert alerts[0].failure_reason == "search_results_all_stale"
    assert risk_diagnostics["riskStatusReason"] == "search_results_all_stale"
    assert provider_diagnostics["providerDiagnostics"][0]["providerName"] == "tavily"


def test_poi_risk_service_distinguishes_no_results_from_provider_unavailable():
    search_provider = FakeWebSearchProvider(
        WebSearchResponse(
            query="北京大学 2026 国庆 官方公告",
            results=[],
            provider_name="chained-web-search",
            provider_diagnostics=[{"providerName": "tavily", "status": "no_results", "reason": "no_usable_results", "resultCount": 0}],
            attempted_providers=["tavily"],
            failed_providers=["tavily"],
            failure_reason="all_web_search_providers_failed_or_empty",
        )
    )

    alerts = POIRiskService(web_search_provider=search_provider, risk_agent_provider=FakeRiskAgentProvider("", 0)).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(name="北京大学", category="education")],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    risk_diagnostics = [source for source in alerts[0].sources if source.get("type") == "riskSearchDiagnostics"][0]
    provider_diagnostics = [source for source in alerts[0].sources if source.get("type") == "webSearchProviderDiagnostics"][0]
    assert alerts[0].status == "unavailable"
    assert alerts[0].failure_reason == "search_no_results"
    assert risk_diagnostics["riskStatusReason"] == "search_no_results"
    assert provider_diagnostics["providerDiagnostics"][0]["status"] == "no_results"


def test_poi_risk_service_continues_followup_pois_after_provider_unavailable():
    search_provider = FakeWebSearchProvider(
        WebSearchResponse(
            query="北京大学 2026 国庆 官方公告",
            results=[],
            provider_name="chained-web-search",
            provider_diagnostics=[{"providerName": "tavily", "status": "failed", "reason": "timeout", "resultCount": 0}],
            attempted_providers=["tavily"],
            failed_providers=["tavily"],
            failure_reason="all_web_search_providers_failed_or_empty",
        )
    )
    second_segment = ItinerarySegment(
        id="seg_2",
        day_id="day_1",
        segment_order=2,
        kind="activity",
        start_time="14:00",
        end_time="15:30",
        poi_id="poi_2",
        transport_mode="public_transit",
        estimated_cost=0,
        notes="风险待查",
    )

    alerts = POIRiskService(web_search_provider=search_provider, risk_agent_provider=FakeRiskAgentProvider("", 0)).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[
            poi_fixture(name="北京大学", category="education"),
            POI(id="poi_2", name="清华大学", city="北京", category="education", latitude=40.0, longitude=116.32, confidence=0.9),
        ],
        segments=[segment_fixture(), second_segment],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    assert len(search_provider.queries) >= 2
    assert [alert.failure_reason for alert in alerts] == [
        "search_provider_unavailable",
        "search_provider_unavailable",
    ]
    skipped_diagnostics = [source for source in alerts[1].sources if source.get("type") == "riskSearchDiagnostics"][0]
    assert skipped_diagnostics["riskStatusReason"] == "search_provider_unavailable"
    assert skipped_diagnostics["queryAttempts"][0]["attemptedProviders"] == ["tavily"]
    provider_diagnostics = [source for source in alerts[1].sources if source.get("type") == "webSearchProviderDiagnostics"][0]
    assert provider_diagnostics["attemptedProviders"] == ["tavily"]


def test_poi_risk_service_tries_fallback_query_after_no_results():
    search_provider = SequenceWebSearchProvider(
        [
            WebSearchResponse(
                query="北京大学 2026 国庆 官方公告",
                results=[],
                provider_name="chained-web-search",
                provider_diagnostics=[{"providerName": "tavily", "status": "no_results", "reason": "no_usable_results", "resultCount": 0}],
                attempted_providers=["tavily"],
                failed_providers=["tavily"],
                failure_reason="all_web_search_providers_failed_or_empty",
            ),
            WebSearchResponse(
                query="北京大学 2026 国庆 文旅",
                results=[
                    WebSearchItem(
                        title="北京大学 2026 国庆校园参观预约官方公告",
                        url="https://www.pku.edu.cn/notice/2026",
                        snippet="2026年国庆期间校园参观需预约。",
                        source_name="北京大学",
                        confidence=0.82,
                        credibility_rank="official",
                        provider_name="brave-web-search",
                    )
                ],
                provider_name="chained-web-search",
                provider_diagnostics=[{"providerName": "brave-web-search", "status": "success", "reason": "ok", "resultCount": 1}],
                attempted_providers=["brave-web-search"],
                successful_providers=["brave-web-search"],
            ),
        ]
    )

    alerts = POIRiskService(
        web_search_provider=search_provider,
        risk_agent_provider=FakeRiskAgentProvider("建议提前预约。", 0.8),
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(name="北京大学", category="education")],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    risk_diagnostics = [source for source in alerts[0].sources if source.get("type") == "riskSearchDiagnostics"][0]
    assert alerts[0].status in {"available", "degraded"}
    assert len(search_provider.queries) == 2
    assert risk_diagnostics["acceptedSourceCount"] == 1
    assert [item["riskStatusReason"] for item in risk_diagnostics["queryAttempts"]] == [
        "search_no_results",
        "search_success_available",
    ]


def test_poi_risk_service_tries_fallback_query_after_provider_unavailable():
    search_provider = SequenceWebSearchProvider(
        [
            WebSearchResponse(
                query="北京大学 2026 国庆 官方公告",
                results=[],
                provider_name="chained-web-search",
                provider_diagnostics=[{"providerName": "searxng", "status": "failed", "reason": "timeout", "resultCount": 0}],
                attempted_providers=["searxng"],
                failed_providers=["searxng"],
                failure_reason="all_web_search_providers_failed_or_empty",
            ),
            WebSearchResponse(
                query="北京大学 2026 国庆 文旅",
                results=[
                    WebSearchItem(
                        title="北京大学 2026 国庆校园参观预约官方公告",
                        url="https://www.pku.edu.cn/notice/2026",
                        snippet="2026年国庆期间校园参观需预约。",
                        source_name="北京大学",
                        confidence=0.82,
                        credibility_rank="official",
                        provider_name="ddgs",
                    )
                ],
                provider_name="chained-web-search",
                provider_diagnostics=[{"providerName": "ddgs", "status": "success", "reason": "ok", "resultCount": 1}],
                attempted_providers=["ddgs"],
                successful_providers=["ddgs"],
            ),
        ]
    )

    alerts = POIRiskService(
        web_search_provider=search_provider,
        risk_agent_provider=FakeRiskAgentProvider("建议提前预约。", 0.8),
    ).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(name="北京大学", category="education")],
        segments=[segment_fixture()],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-10-01",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={"resolvedTripDates": {"dates": ["2026-10-01"], "holidayInferred": True, "holidayName": "国庆"}},
    )

    risk_diagnostics = [source for source in alerts[0].sources if source.get("type") == "riskSearchDiagnostics"][0]
    assert alerts[0].status in {"available", "degraded"}
    assert len(search_provider.queries) == 2
    assert risk_diagnostics["acceptedSourceCount"] == 1
    assert [item["riskStatusReason"] for item in risk_diagnostics["queryAttempts"]] == [
        "search_provider_unavailable",
        "search_success_available",
    ]


def test_poi_risk_service_enforces_shared_query_and_provider_attempt_budget():
    response = WebSearchResponse(
        query="",
        results=[],
        confidence=0.0,
        provider_name="budget-test",
        failure_reason="no result",
        attempted_providers=["p1", "p2"],
        failed_providers=["p1", "p2"],
    )
    search_provider = FakeWebSearchProvider(response)
    second_poi = POI(
        id="poi_2",
        name="景山公园",
        city="北京",
        category="attraction",
        latitude=39.923,
        longitude=116.396,
        confidence=0.9,
    )
    second_segment = ItinerarySegment(
        id="seg_2",
        day_id="day_1",
        segment_order=2,
        kind="activity",
        start_time="13:00",
        end_time="15:00",
        poi_id="poi_2",
        transport_mode="walk",
        estimated_cost=0,
        notes="风险待查",
    )

    POIRiskService(web_search_provider=search_provider).build_alerts(
        plan_id="plan_risk",
        city="北京",
        pois=[poi_fixture(), second_poi],
        segments=[segment_fixture(), second_segment],
        weather=WeatherSignal(
            id="weather_1",
            city="北京",
            date="2026-06-10",
            hourly_forecast=[],
            daily_summary="多云",
            risk_level="neutral",
            purpose_impact_reason="天气影响较低",
        ),
        traffic_signals=[],
        ticket_results=[],
        risk_context={
            "maxRiskSearchQueries": 1,
            "maxRiskProviderAttempts": 2,
            "maxRiskSearchSeconds": 1,
            "maxRiskSynthesisCalls": 0,
        },
    )

    assert len(search_provider.queries) == 1


class FakeWebSearchProvider:
    def __init__(self, response: WebSearchResponse):
        self.response = response
        self.queries: list[str] = []

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        self.queries.append(query)
        self.response.query = query
        return self.response


class SequenceWebSearchProvider:
    def __init__(self, responses: list[WebSearchResponse]):
        self.responses = responses
        self.queries: list[str] = []

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        self.queries.append(query)
        index = min(len(self.queries) - 1, len(self.responses) - 1)
        response = self.responses[index]
        response.query = query
        return response


class FakeRiskAgentProvider:
    def __init__(self, summary: str, confidence: float):
        self.summary = summary
        self.confidence = confidence
        self.payload = None

    def synthesize(self, payload: dict) -> dict:
        self.payload = payload
        assert payload["sources"][0]["url"].startswith("https://")
        return {
            "summary": self.summary,
            "confidence": self.confidence,
            "userVisibleCaveat": "Agent 已结合公开来源和当前行程上下文生成风险判断。",
        }


class FailingRiskAgentProvider:
    def synthesize(self, _payload: dict) -> dict:
        raise AgentRiskSynthesisError("Agent 风险判断服务暂不可用")


def poi_fixture(name: str = "故宫博物院", category: str = "attraction") -> POI:
    return POI(
        id="poi_1",
        name=name,
        city="北京",
        category=category,
        latitude=39.9163,
        longitude=116.3972,
        confidence=0.9,
    )


def segment_fixture() -> ItinerarySegment:
    return ItinerarySegment(
        id="seg_1",
        day_id="day_1",
        segment_order=1,
        kind="activity",
        start_time="09:30",
        end_time="11:30",
        poi_id="poi_1",
        transport_mode="public_transit",
        estimated_cost=60,
        notes="门票/预约状态待查",
    )


def route_fixture() -> RouteOption:
    return RouteOption(
        id="route_1",
        plan_id="plan_risk",
        from_segment_id="seg_1",
        to_segment_id="seg_2",
        from_poi_id="poi_1",
        to_poi_id="poi_2",
        mode="transit",
        label="公交/地铁",
        is_selected=True,
        distance_meters=1900,
        duration_seconds=1080,
        cost_amount=4,
        crowding_risk="medium",
        source="amap-webservice",
    )

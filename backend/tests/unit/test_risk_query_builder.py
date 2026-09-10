from types import SimpleNamespace

from src.services.risk_query_builder import RiskQueryBuilder, RiskSourceScorer


def test_national_day_risk_query_contains_holiday_official_and_limit_terms():
    query = RiskQueryBuilder().build(
        city="北京",
        poi_name="故宫",
        segment_start_time="09:00",
        segment_end_time="11:00",
        risk_context={
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-07",
                "dates": ["2026-10-01", "2026-10-07"],
                "holidayName": "国庆节",
                "holidayInferred": True,
            }
        },
    )

    for token in ["北京", "故宫", "2026", "10月1日 10月7日", "国庆", "十一", "黄金周", "官方公告", "限流", "预约"]:
        assert token in query.query
    assert query.target_year == 2026
    assert query.holiday_name == "国庆节"


def test_risk_query_builder_emits_short_multi_query_plan():
    query = RiskQueryBuilder().build(
        city="北京",
        poi_name="北京大学",
        risk_context={
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-03",
                "dates": ["2026-10-01", "2026-10-03"],
                "holidayName": "国庆节",
                "holidayInferred": True,
            }
        },
    )

    assert 3 <= len(query.queries) <= 4
    assert query.query == query.queries[0]
    assert all(len(item) <= 180 for item in query.queries)
    assert "官方公告" in query.queries[0]
    assert "文旅" in query.queries[1]
    assert query.queries[2] == "北京大学 预约 开放时间 官方"
    assert any("site:edu.cn" in item for item in query.queries)


def test_stale_year_source_is_not_accepted_for_future_holiday():
    item = SimpleNamespace(
        title="2023 国庆 故宫 限流",
        snippet="旧年十一假期预约提示",
        url="https://example.com/old",
        source_name="攻略",
        confidence=0.8,
    )

    score = RiskSourceScorer().score(item, target_year=2026)

    assert score["targetDateRelevance"] == "stale_for_target_date"
    assert score["accepted"] is False
    assert score["confidence"] < 0.45


def test_undated_non_official_source_is_low_confidence_reference_only():
    item = SimpleNamespace(
        title="故宫游玩攻略",
        snippet="假期人很多，建议早去。",
        url="https://travel.example.com/guide",
        source_name="游记",
        confidence=0.7,
    )

    score = RiskSourceScorer().score(item, target_year=2026)

    assert score["targetDateRelevance"] == "undated"
    assert score["accepted"] is False
    assert score["stalenessReason"] is None


def test_social_or_unrelated_sources_are_rejected_for_poi_risk():
    social_sources = [
        SimpleNamespace(
            title="Peking University campus photos",
            snippet="Instagram travel photos from an old trip.",
            url="https://www.instagram.com/pku/photos",
            source_name="Instagram",
            confidence=0.92,
        ),
        SimpleNamespace(
            title="Peking University visitor photos",
            snippet="Facebook post about campus photos.",
            url="https://www.facebook.com/pku/photos",
            source_name="Facebook",
            confidence=0.9,
        ),
        SimpleNamespace(
            title="Peking University travel video",
            snippet="TikTok travel video, not an official reservation notice.",
            url="https://www.tiktok.com/@travel/video/123",
            source_name="TikTok",
            confidence=0.9,
        ),
        SimpleNamespace(
            title="Peking University travel board",
            snippet="Pinterest inspiration board.",
            url="https://www.pinterest.com/pin/123",
            source_name="Pinterest",
            confidence=0.9,
        ),
    ]
    unrelated = SimpleNamespace(
        title="Shanghai international student culture festival 2026",
        snippet="An English campus event article unrelated to visitor reservation or National Day access.",
        url="https://example.edu/news/culture-festival-2026",
        source_name="Example University",
        confidence=0.85,
    )

    scorer = RiskSourceScorer()
    social_scores = [scorer.score(item, target_year=2026, city="北京", poi_name="北京大学") for item in social_sources]
    unrelated_score = scorer.score(unrelated, target_year=2026, city="北京", poi_name="北京大学")

    assert all(score["credibilityRank"] == "social" for score in social_scores)
    assert all(score["accepted"] is False for score in social_scores)
    assert unrelated_score["accepted"] is False
    assert unrelated_score["relevanceReason"] in {"poi_mismatch", "city_mismatch", "low_risk_relevance"}

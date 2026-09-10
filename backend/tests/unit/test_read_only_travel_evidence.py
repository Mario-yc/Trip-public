import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.providers.travel_tools import ChainedWebSearchProvider, WebSearchItem, WebSearchResponse
from src.core.database import get_db
from src.services.segment_visit_facts_service import SegmentVisitFactsService
from src.services.travel_guide_advice_service import TravelGuideAdviceService


class RecordedSearchProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def search(self, query, **_kwargs):
        self.queries.append(query)
        return self.responses.pop(0) if self.responses else WebSearchResponse(query=query, results=[])


class RawGuideSearchProvider:
    provider_name = "raw-guide-search"

    def __init__(self, results):
        self.results = list(results)
        self.queries = []

    def search(self, query, **_kwargs):
        self.queries.append(query)
        return WebSearchResponse(
            query=query,
            results=list(self.results),
            provider_name=self.provider_name,
        )


class RecordedMapPoiService:
    def __init__(self, *, opening_week="周一至周日 09:00-17:00", opening_today=None):
        self.opening_week = opening_week
        self.opening_today = opening_today
        self.ids = []

    def detail(self, amap_id):
        self.ids.append(amap_id)
        return SimpleNamespace(
            name="测试景点",
            open_time_week=self.opening_week,
            open_time_today=self.opening_today,
            provider_queried_at=datetime.now(timezone.utc),
        )


def item(title, url, snippet, rank="official"):
    return WebSearchItem(
        title=title,
        url=url,
        snippet=snippet,
        source_name="记录来源",
        queried_at=datetime.now(timezone.utc),
        credibility_rank=rank,
    )


def test_visit_facts_keep_unknown_distinct_from_free_or_no_reservation(isolated_test_database):
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[item("官方公告", "https://example.gov.cn/notice", "开放时间：09:00-17:00。")],
            ),
            WebSearchResponse(query="q2", results=[]),
        ]
    )
    database = get_db()
    connection = next(database)
    try:
        service = SegmentVisitFactsService(connection, provider=provider)
        now = datetime.now(timezone.utc)
        facts, _refs = service._extract(provider.responses[:1], now, now)
    finally:
        database.close()

    assert facts["openingHours"]["status"] == "verified"
    assert facts["reservation"]["status"] == "unknown"
    assert facts["ticketPrice"]["valueText"] == "待核验"


def test_visit_facts_use_amap_opening_metadata_after_official_web_evidence(isolated_test_database):
    provider = RecordedSearchProvider([WebSearchResponse(query="q1", results=[])])
    map_service = RecordedMapPoiService()
    database = get_db()
    connection = next(database)
    try:
        service = SegmentVisitFactsService(connection, provider=provider, map_poi_service=map_service)
        now = datetime.now(timezone.utc)
        amap_opening = service._amap_opening_evidence("B000A0001")
        facts, refs = service._extract(provider.responses[:1], now, now, amap_opening=amap_opening)
    finally:
        database.close()

    assert map_service.ids == ["B000A0001"]
    assert facts["openingHours"]["status"] == "verified"
    assert facts["openingHours"]["valueText"] == "周一至周日 09:00-17:00"
    assert facts["openingHours"]["sourceRefs"][0]["sourceName"] == "高德地图"
    assert refs[-1]["amapPoiId"] == "B000A0001"


def test_visit_facts_show_conflict_instead_of_overwriting_official_hours_with_amap(isolated_test_database):
    official = WebSearchResponse(
        query="q1",
        results=[item("官方公告", "https://example.gov.cn/notice", "开放时间：08:00-16:00。")],
    )
    database = get_db()
    connection = next(database)
    try:
        service = SegmentVisitFactsService(connection, provider=RecordedSearchProvider([]))
        now = datetime.now(timezone.utc)
        facts, _refs = service._extract(
            [official],
            now,
            now,
            amap_opening=(
                "周一至周日 09:00-17:00",
                {"sourceName": "高德地图", "amapPoiId": "B000A0001"},
            ),
        )
    finally:
        database.close()

    assert facts["openingHours"]["status"] == "conflicting"
    assert facts["openingHours"]["valueText"].startswith("08:00-16:00")


def test_future_visit_does_not_promote_amap_today_hours(isolated_test_database):
    database = get_db()
    connection = next(database)
    try:
        service = SegmentVisitFactsService(
            connection,
            provider=RecordedSearchProvider([]),
            map_poi_service=RecordedMapPoiService(opening_week="", opening_today="09:00-17:00"),
        )
        evidence = service._amap_opening_evidence("B000A0001", visit_date="2026-10-01")
    finally:
        database.close()

    assert evidence is None


def test_official_date_specific_closure_becomes_verified_for_that_visit_date(isolated_test_database):
    response = WebSearchResponse(
        query="q1",
        results=[item("国庆开放公告", "https://example.gov.cn/notice", "2026年10月1日闭馆，暂停开放。")],
    )
    database = get_db()
    connection = next(database)
    try:
        service = SegmentVisitFactsService(connection, provider=RecordedSearchProvider([]))
        now = datetime.now(timezone.utc)
        facts, _refs = service._extract(
            [response],
            now,
            now,
            visit_date="2026-10-01",
        )
    finally:
        database.close()

    assert facts["openingHours"]["status"] == "verified"
    assert facts["openingHours"]["valueText"] in {"闭馆", "暂停开放"}
    assert facts["openingHours"]["effectiveForDate"] == "2026-10-01"


def test_guide_advice_uses_at_most_two_queries_and_marks_pois_unverified():
    first = WebSearchResponse(query="q1", results=[])
    second = WebSearchResponse(
        query="q2",
        results=[
            item("北京高校攻略", "https://travel.example/guide", "注意预约，并预留高校之间的交通时间。", "search")
        ],
    )
    provider = RecordedSearchProvider([first, second])
    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": "campus_visit"}]},
    )

    assert len(provider.queries) == 2
    assert advice["queryCount"] == 2
    assert advice["recommendations"][0]["poiVerificationStatus"] == "unverified_advice"


class StructuredConclusionProvider:
    def __init__(self, *, invalid_ref=False, overview=None):
        self.invalid_ref = invalid_ref
        self.overview = overview or "高校参观应先核对访客预约规则，并为公共交通预留时间。"
        self.calls = []

    def synthesize_travel_guide_conclusion(self, context, *, timeout_seconds):
        self.calls.append((context, timeout_seconds))
        evidence = context["evidence"][0]
        return json.dumps(
            {
                "overview": self.overview,
                "takeaways": [
                    {
                        "intentType": "campus_visit",
                        "themeLabel": "高校参观",
                        "text": "出发前核对高校访客预约规则。",
                        "sourceRefIds": ["unknown_ref" if self.invalid_ref else evidence["refId"]],
                        "evidenceQuote": "提前预约",
                    }
                ],
                "conflicts": [],
            },
            ensure_ascii=False,
        )


class PlaceMentionConclusionProvider(StructuredConclusionProvider):
    def __init__(self, *, mention_text="北京大学", invalid_ref=False, place_mentions=None):
        super().__init__()
        self.mention_text = mention_text
        self.invalid_ref = invalid_ref
        self.place_mentions = place_mentions

    def synthesize_travel_guide_conclusion(self, context, *, timeout_seconds):
        self.calls.append((context, timeout_seconds))
        evidence = context["evidence"][0]
        source_ref_id = "unknown_ref" if self.invalid_ref else evidence["refId"]
        place_mentions = (
            [
                {
                    **mention,
                    "sourceRefIds": mention.get("sourceRefIds") or [evidence["refId"]],
                }
                for mention in self.place_mentions
            ]
            if self.place_mentions
            else [
                {
                    "mentionText": self.mention_text,
                    "intentType": "campus_visit",
                    "sourceRefIds": [source_ref_id],
                }
            ]
        )
        return json.dumps(
            {
                "overview": "高校参观应先核对访客预约规则。",
                "takeaways": [
                    {
                        "intentType": "campus_visit",
                        "themeLabel": "高校参观",
                        "text": "出发前核对高校访客预约规则。",
                        "sourceRefIds": [evidence["refId"]],
                        "evidenceQuote": "提前预约",
                    }
                ],
                "conflicts": [],
                "placeMentions": place_mentions,
            },
            ensure_ascii=False,
        )


def test_guide_conclusion_uses_only_admitted_refs_and_validates_quote():
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[item("北京高校参观攻略", "https://travel.example/guide", "参观高校需要提前预约。", "guide")],
            )
        ]
    )
    conclusion_provider = StructuredConclusionProvider()

    advice = TravelGuideAdviceService(
        provider=provider,
        conclusion_provider=conclusion_provider,
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})

    assert advice["conclusion"]["generationMethod"] == "deepseek_structured_v1"
    assert advice["conclusion"]["takeaways"][0]["sourceRefIds"] == [advice["sourceRefs"][0]["refId"]]
    assert conclusion_provider.calls[0][1] == 6.0


def test_guide_advice_projects_model_place_mentions_with_full_evidence_binding():
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[
                    item(
                        "北京大学参观攻略",
                        "https://travel.example/peking-university-guide",
                        "北京大学参观需要提前预约。",
                        "guide",
                    )
                ],
            )
        ]
    )
    conclusion_provider = PlaceMentionConclusionProvider()

    advice = TravelGuideAdviceService(
        provider=provider,
        conclusion_provider=conclusion_provider,
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})

    source = advice["sourceRefs"][0]
    assert len(conclusion_provider.calls) == 1
    assert advice["placeHints"] == [
        {
            "mentionText": "北京大学",
            "intentType": "campus_visit",
            "sourceRefIds": [source["refId"]],
            "sourceFingerprints": [source["sourceFingerprint"]],
            "guideEvidenceFingerprint": advice["evidenceFingerprint"],
            "verificationStatus": "unresolved_amap_grounding",
        }
    ]
    assert advice["placeHints"][0]["mentionText"] in advice["recommendations"][0]["text"]
    assert len(advice["placeHints"]) <= len(advice["sourceRefs"])
    assert TravelGuideAdviceService.extract_place_hints(advice) == advice["placeHints"]

    tampered = json.loads(json.dumps(advice, ensure_ascii=False))
    tampered["placeHints"][0]["sourceFingerprints"] = ["0" * 64]
    assert TravelGuideAdviceService.extract_place_hints(tampered) == []


@pytest.mark.parametrize(
    ("mention_text", "invalid_ref"),
    [("清华大学", False), ("北京大学", True)],
)
def test_guide_advice_invalid_model_place_mentions_fail_closed_to_deterministic_conclusion(
    mention_text,
    invalid_ref,
):
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[
                    item(
                        "北京大学参观攻略",
                        "https://travel.example/peking-university-guide",
                        "北京大学参观需要提前预约。",
                        "guide",
                    )
                ],
            )
        ]
    )

    advice = TravelGuideAdviceService(
        provider=provider,
        conclusion_provider=PlaceMentionConclusionProvider(
            mention_text=mention_text,
            invalid_ref=invalid_ref,
        ),
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})

    assert advice["conclusion"]["generationMethod"] == "deterministic_fallback_v1"
    assert advice["conclusion"]["fallbackReasonCode"] == "model_output_invalid_or_unavailable"
    assert all(hint["mentionText"] != "清华大学" for hint in advice["placeHints"])
    assert all("unknown_ref" not in hint["sourceRefIds"] for hint in advice["placeHints"])


def test_guide_advice_never_emits_more_place_hints_than_admitted_sources():
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[
                    item(
                        "北京大学和清华大学参观攻略",
                        "https://travel.example/two-campus-guide",
                        "北京大学和清华大学参观都需要提前预约。",
                        "guide",
                    )
                ],
            )
        ]
    )
    model_mentions = [
        {
            "mentionText": name,
            "intentType": "campus_visit",
            "sourceRefIds": [],
        }
        for name in ("北京大学", "清华大学")
    ]
    conclusion_provider = PlaceMentionConclusionProvider(place_mentions=model_mentions)

    advice = TravelGuideAdviceService(
        provider=provider,
        conclusion_provider=conclusion_provider,
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})

    assert advice["conclusion"]["generationMethod"] == "deterministic_fallback_v1"
    assert len(advice["placeHints"]) <= len(advice["sourceRefs"]) == 1


def test_extract_place_hints_normalizes_legacy_conclusion_only_when_source_material_verifies():
    advice = TravelGuideAdviceService(
        provider=RecordedSearchProvider(
            [
                WebSearchResponse(
                    query="q1",
                    results=[
                        item(
                            "北京大学参观攻略",
                            "https://travel.example/peking-university-guide",
                            "北京大学参观需要提前预约。",
                            "guide",
                        )
                    ],
                )
            ]
        )
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})
    legacy = {**advice, "placeHints": []}
    legacy["conclusion"] = {
        **advice["conclusion"],
        "placeMentions": [
            {
                "mentionText": "北京大学",
                "intentType": "campus_visit",
                "sourceRefIds": [advice["sourceRefs"][0]["refId"]],
            }
        ],
    }

    hints = TravelGuideAdviceService.extract_place_hints(legacy)

    assert hints[0]["mentionText"] == "北京大学"
    assert hints[0]["sourceFingerprints"] == [advice["sourceRefs"][0]["sourceFingerprint"]]
    assert hints[0]["guideEvidenceFingerprint"] == advice["evidenceFingerprint"]
    assert hints[0]["verificationStatus"] == "unresolved_amap_grounding"

    older_payload = json.loads(json.dumps(legacy, ensure_ascii=False))
    older_payload["conclusion"].pop("placeMentions")
    assert TravelGuideAdviceService.extract_place_hints(older_payload)[0]["mentionText"] == "北京大学"

    unsafe = json.loads(json.dumps(legacy, ensure_ascii=False))
    unsafe["conclusion"]["placeMentions"][0]["mentionText"] = "清华大学"
    assert TravelGuideAdviceService.extract_place_hints(unsafe) == []

    unsafe = json.loads(json.dumps(legacy, ensure_ascii=False))
    unsafe["sourceRefs"][0]["sourceFingerprint"] = "0" * 64
    assert TravelGuideAdviceService.extract_place_hints(unsafe) == []


def test_guide_conclusion_invalid_reference_falls_back_without_failing_search():
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[item("北京高校参观攻略", "https://travel.example/guide", "参观高校需要提前预约。", "guide")],
            )
        ]
    )

    advice = TravelGuideAdviceService(
        provider=provider,
        conclusion_provider=StructuredConclusionProvider(invalid_ref=True),
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})

    assert advice["status"] == "completed"
    assert advice["conclusion"]["generationMethod"] == "deterministic_fallback_v1"
    assert advice["conclusion"]["fallbackReasonCode"] == "model_output_invalid_or_unavailable"


def test_guide_conclusion_keeps_overview_server_owned_even_when_model_adds_claims():
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[item("北京高校参观攻略", "https://travel.example/guide", "参观高校需要提前预约。", "guide")],
            )
        ]
    )
    malicious_overview = "所有高校都必须实名预约并且建议购买联票。"

    advice = TravelGuideAdviceService(
        provider=provider,
        conclusion_provider=StructuredConclusionProvider(overview=malicious_overview),
    ).search(city="北京", request_contract={"requiredIntents": [{"intentType": "campus_visit"}]})

    assert advice["conclusion"]["generationMethod"] == "deepseek_structured_v1"
    assert malicious_overview not in advice["conclusion"]["overview"]
    assert advice["conclusion"]["overview"] == "现有攻略摘要主要覆盖高校参观。"


def test_guide_advice_keeps_the_available_search_summary_and_strips_relative_time_prefix():
    summary = "北京高校参观攻略建议提前预约并预留公共交通时间。" * 24 + "GUIDE_TAIL_987"
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[
                    item(
                        "北京高校参观攻略",
                        "https://travel.example/long-campus-guide",
                        f"14 小时之前 · {summary}",
                        "guide",
                    )
                ],
            )
        ]
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": "campus_visit"}]},
    )

    recommendation = advice["recommendations"][0]
    assert recommendation["text"].endswith("GUIDE_TAIL_987")
    assert not recommendation["text"].startswith("14 小时之前")
    assert recommendation["summaryKind"] == "search_result_snippet"


def test_guide_advice_translates_internal_intents_before_production_relevance_filter():
    raw_provider = RawGuideSearchProvider(
        [
            item(
                "北京高校、公园和美食两日游攻略",
                "https://travel.example/beijing-campus-guide",
                "高校参观建议提前确认访客规则，午餐可体验当地美食，晚上安排公园散步。",
                "guide",
            )
        ]
    )
    provider = ChainedWebSearchProvider(
        providers=[raw_provider],
        min_accepted_results=1,
        max_provider_attempts=1,
        total_deadline_seconds=1,
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={
            "requiredIntents": [
                {"intentType": "campus_visit"},
                {"intentType": "park"},
                {"intentType": "meal"},
            ]
        },
    )

    assert advice["queryCount"] == 1
    assert advice["sourceRefs"][0]["url"] == "https://travel.example/beijing-campus-guide"
    assert advice["recommendations"][0]["poiVerificationStatus"] == "unverified_advice"
    assert len(raw_provider.queries) == 1
    assert "高校" in raw_provider.queries[0]
    assert "公园" in raw_provider.queries[0]
    assert "美食" in raw_provider.queries[0]
    assert all(code not in raw_provider.queries[0] for code in ("campus_visit", "park", "meal"))


def test_guide_advice_filters_off_topic_policy_news_after_provider_validation():
    policy_news = item(
        "关于北京城市副中心支持专精特新企业高质量发展实施细则",
        "https://www.bjtzh.gov.cn/policy/industry-support",
        "14 小时之前 · 北京市通州区经济和信息化局发布企业申报通知。",
        "official",
    )
    relevant_guide = item(
        "北京高校参观与城市公园游玩攻略",
        "https://travel.example/beijing-campus-park",
        "北京大学参观需要预约，下午可前往城市公园散步，并预留公共交通时间。",
        "guide",
    )
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(query="q1", results=[policy_news, relevant_guide]),
            WebSearchResponse(query="q2", results=[]),
        ]
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={
            "requiredIntents": [
                {"intentType": "campus_visit"},
                {"intentType": "park"},
                {"intentType": "meal"},
            ]
        },
    )

    assert advice["status"] == "completed"
    assert [source["url"] for source in advice["sourceRefs"]] == ["https://travel.example/beijing-campus-park"]
    assert advice["relevanceFilter"]["rejectedResultCount"] == 1
    assert advice["relevanceFilter"]["reasonCounts"] == {"theme_mismatch": 1}
    assert "专精特新" not in json.dumps(advice, ensure_ascii=False)
    assert "游玩攻略" in provider.queries[0]
    assert "经验 建议" not in provider.queries[0]


def test_guide_query_does_not_let_city_policy_stop_provider_chain_before_real_guide():
    policy_provider = RawGuideSearchProvider(
        [
            item(
                "关于北京城市副中心支持专精特新企业高质量发展实施细则",
                "https://www.bjtzh.gov.cn/policy/industry-support",
                "北京市通州区经济和信息化局发布企业申报通知。",
                "official",
            )
        ]
    )
    guide_provider = RawGuideSearchProvider(
        [
            item(
                "北京高校、公园和美食两日游攻略",
                "https://travel.example/beijing-two-day-guide",
                "北京高校参观需要预约，下午可游玩公园，晚餐体验当地美食。",
                "guide",
            )
        ]
    )
    provider = ChainedWebSearchProvider(
        providers=[policy_provider, guide_provider],
        min_accepted_results=1,
        max_provider_attempts=2,
        total_deadline_seconds=1,
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={
            "requiredIntents": [
                {"intentType": "campus_visit"},
                {"intentType": "park"},
                {"intentType": "meal"},
            ]
        },
    )

    assert len(policy_provider.queries) == 1
    assert len(guide_provider.queries) == 1
    assert [source["url"] for source in advice["sourceRefs"]] == ["https://travel.example/beijing-two-day-guide"]
    assert "专精特新" not in json.dumps(advice, ensure_ascii=False)


def test_guide_query_accepts_natural_travel_wording_without_exact_long_theme_phrases():
    raw_provider = RawGuideSearchProvider(
        [
            item(
                "#旅游 北京三天旅游攻略",
                "https://travel.example/beijing-natural-wording",
                "北京高校、公园和美食三日游路线建议。",
                "guide",
            )
        ]
    )
    provider = ChainedWebSearchProvider(
        providers=[raw_provider],
        min_accepted_results=1,
        max_provider_attempts=1,
        total_deadline_seconds=1,
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={
            "requiredIntents": [
                {"intentType": "campus_visit"},
                {"intentType": "park"},
                {"intentType": "meal"},
            ]
        },
    )

    assert advice["queryCount"] == 1
    assert [source["url"] for source in advice["sourceRefs"]] == ["https://travel.example/beijing-natural-wording"]


@pytest.mark.parametrize(
    ("title", "snippet", "expected_reason"),
    [
        (
            "北京城市副中心产业政策发布",
            "北京市通州区经济和信息化局发布专精特新企业申报通知。",
            "theme_mismatch",
        ),
        (
            "上海大学参观攻略",
            "上海高校游客入校参观指南。",
            "destination_mismatch",
        ),
        (
            "北京大学科研成果发布",
            "北京大学发布最新科研成果和学术活动安排。",
            "travel_advice_signal_missing",
        ),
    ],
)
def test_guide_advice_rejects_results_without_destination_theme_and_travel_advice_alignment(
    title,
    snippet,
    expected_reason,
):
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[item(title, "https://example.com/result", snippet, "search")],
            ),
            WebSearchResponse(query="q2", results=[]),
        ]
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": "campus_visit"}]},
    )

    assert advice["status"] == "no_results"
    assert advice["recommendations"] == []
    assert advice["sourceRefs"] == []
    assert advice["relevanceFilter"]["reasonCounts"] == {expected_reason: 1}


@pytest.mark.parametrize(
    ("intent_type", "expected_theme", "result_text"),
    [
        ("campus_visit", "高校参观", "北京高校参观攻略"),
        ("night_view", "城市夜景", "北京城市夜景攻略"),
        ("landmark", "城市地标", "北京城市地标攻略"),
        ("museum", "博物馆", "北京博物馆攻略"),
        ("park", "城市公园", "北京城市公园攻略"),
        ("meal", "当地美食", "北京当地美食攻略"),
        ("rest", "休闲休息", "北京休闲休息攻略"),
        ("shopping", "本地购物", "北京本地购物攻略"),
        ("area_walk", "城市漫步", "北京城市漫步攻略"),
        ("local_culture", "本地人文", "北京本地人文历史攻略"),
    ],
)
def test_guide_advice_translates_every_production_poi_intent(
    intent_type,
    expected_theme,
    result_text,
):
    raw_provider = RawGuideSearchProvider(
        [item(result_text, f"https://travel.example/{intent_type}", result_text, "guide")]
    )
    provider = ChainedWebSearchProvider(
        providers=[raw_provider],
        min_accepted_results=1,
        max_provider_attempts=1,
        total_deadline_seconds=1,
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": intent_type}]},
    )

    assert advice["queryCount"] == 1
    assert advice["sourceRefs"][0]["url"].endswith(f"/{intent_type}")
    assert expected_theme in raw_provider.queries[0]
    assert intent_type not in raw_provider.queries[0]


def test_guide_advice_projects_truthful_provider_failure_diagnostics():
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[],
                failure_reason="all_web_search_providers_failed_or_empty",
                attempted_providers=["bing"],
                failed_providers=["bing"],
                provider_diagnostics=[
                    {
                        "providerName": "bing",
                        "status": "failed",
                        "reasonCode": "filtered_empty",
                        "rejectionReasons": ["query_mismatch"],
                    }
                ],
            ),
            WebSearchResponse(
                query="q2",
                results=[],
                failure_reason="all_web_search_providers_failed_or_empty",
                attempted_providers=["bing"],
                failed_providers=["bing"],
                provider_diagnostics=[
                    {
                        "providerName": "bing",
                        "status": "failed",
                        "reasonCode": "filtered_empty",
                        "rejectionReasons": ["query_mismatch"],
                    }
                ],
            ),
        ]
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": "campus_visit"}]},
    )

    assert advice["status"] == "failed"
    assert advice["failureReason"] == "all_web_search_providers_failed_or_empty"
    assert advice["attemptedProviders"] == ["bing-html-search"]
    assert advice["providerDiagnostics"][0]["reasonCode"] == "filtered_empty"


def test_guide_advice_sanitizes_untrusted_provider_diagnostics_before_persistence():
    secret_url = "https://user:SECRET@example.com/path?token=abc"
    provider = RecordedSearchProvider(
        [
            WebSearchResponse(
                query="q1",
                results=[],
                failure_reason=secret_url,
                attempted_providers=[secret_url],
                failed_providers=["Authorization: Bearer SECRET"],
                provider_diagnostics=[
                    {
                        "providerName": secret_url,
                        "status": "Authorization: Bearer SECRET",
                        "reason": secret_url,
                        "rejectionReasons": ["Authorization: Bearer SECRET", secret_url],
                    }
                ],
            ),
            WebSearchResponse(query="q2", results=[], failure_reason=secret_url),
        ]
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": "campus_visit"}]},
    )
    persisted_text = json.dumps(advice, ensure_ascii=False)

    assert advice["failureReason"] == "provider_error"
    assert advice["attemptedProviders"] == ["web-search-provider"]
    assert advice["failedProviders"] == ["web-search-provider"]
    assert advice["providerDiagnostics"][0]["providerName"] == "web-search-provider"
    assert advice["providerDiagnostics"][0]["reasonCode"] == "provider_error"
    assert "SECRET" not in persisted_text
    assert "token=abc" not in persisted_text
    assert "Authorization" not in persisted_text


def test_guide_advice_rejects_source_urls_with_credentials_or_sensitive_query():
    raw_provider = RawGuideSearchProvider(
        [
            item(
                "北京高校参观攻略",
                "https://user:SECRET@example.com/guide",
                "北京高校参观建议。",
                "guide",
            ),
            item(
                "北京高校参观攻略",
                "https://example.com/guide?token=SECRET",
                "北京高校参观建议。",
                "guide",
            ),
            item(
                "北京高校参观攻略",
                "https://example.com/guide?sig=SECRET",
                "北京高校参观建议。",
                "guide",
            ),
            item(
                "北京高校参观攻略",
                "https://travel.example/guide?topic=campus#section",
                "北京高校参观建议。",
                "guide",
            ),
        ]
    )
    provider = ChainedWebSearchProvider(
        providers=[raw_provider],
        min_accepted_results=1,
        max_provider_attempts=1,
        total_deadline_seconds=1,
    )

    advice = TravelGuideAdviceService(provider=provider).search(
        city="北京",
        request_contract={"requiredIntents": [{"intentType": "campus_visit"}]},
    )

    assert [source["url"] for source in advice["sourceRefs"]] == ["https://travel.example/guide?topic=campus"]
    assert advice["recommendations"][0]["sourceUrl"] == ("https://travel.example/guide?topic=campus")
    assert "SECRET" not in json.dumps(advice, ensure_ascii=False)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://localhost/guide",
        "http://127.0.0.1/guide",
        "http://10.0.0.8/guide",
        "https://travel.example/guide?sig=SECRET",
    ],
)
def test_guide_advice_source_url_rejects_local_private_or_signed_urls(unsafe_url):
    assert TravelGuideAdviceService._safe_source_url(unsafe_url) == ""


def test_visit_facts_reuse_unexpired_matching_segment_identity(isolated_test_database):
    provider = RecordedSearchProvider([])
    database = get_db()
    connection = next(database)
    try:
        service = SegmentVisitFactsService(connection, provider=provider)
        now = datetime.now(timezone.utc)
        service._persist(
            "plan-1",
            "segment-1",
            "B000A",
            "2026-10-01",
            "partial",
            service._empty_facts("待核验", now, now.replace(year=now.year + 1)),
            [],
            now,
            now.replace(year=now.year + 1),
        )
        service._refresh_segment(
            plan_id="plan-1",
            segment_id="segment-1",
            amap_poi_id="B000A",
            poi_name="测试景点",
            visit_date="2026-10-01",
            queried_at=now,
            expires_at=now.replace(year=now.year + 1),
        )
    finally:
        database.close()

    assert provider.queries == []

import json
import os
import sys
import time
from typing import Optional
from urllib.error import URLError

from src.providers.travel_tools import (
    BAIDU_HTML_SEARCH_URL,
    BING_HTML_SEARCH_URL,
    BOCHA_WEB_SEARCH_URL,
    ANYSEARCH_WEB_SEARCH_URL,
    BRAVE_WEB_SEARCH_URL,
    CHEETAH_DUCKDUCKGO_HTML_SEARCH_URL,
    DUCKDUCKGO_HTML_SEARCH_URL,
    DUCKDUCKGO_LITE_SEARCH_URL,
    TAVILY_WEB_SEARCH_URL,
    AmapWeatherProvider,
    AnySearchWebSearchProvider,
    BaiduWebSearchProvider,
    BingHTMLSearchProvider,
    BochaWebSearchProvider,
    BraveWebSearchProvider,
    CheetahDuckDuckGoSearchProvider,
    ChainedWebSearchProvider,
    DDGSWebSearchProvider,
    DuckDuckGoWebSearchProvider,
    GoogleProgrammableSearchProvider,
    MultiFreeWebSearchProvider,
    ResilientAmapWeatherProvider,
    ResilientWebSearchProvider,
    SearXNGWebSearchProvider,
    TavilyWebSearchProvider,
    ToolProviderError,
    WebSearchProvider,
    clear_web_search_runtime_state,
    web_search_provider_config_diagnostics,
)
from src.core.config import get_settings


def test_bocha_config_uses_search_provider_key_when_web_search_api_key_empty(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "")
    monkeypatch.setenv("SEARCH_PROVIDER_KEY", "search-provider-key")
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        settings = get_settings()
        diagnostics = web_search_provider_config_diagnostics("bocha")
    finally:
        get_settings.cache_clear()

    assert settings.search_provider_key == "search-provider-key"
    assert settings.bocha_api_key == "search-provider-key"
    bocha = diagnostics["providers"][0]
    assert bocha["providerName"] == "bocha-web-search"
    assert bocha["configured"] is True
    assert bocha["envAliases"] == {
        "BOCHA_API_KEY": "missing",
        "WEB_SEARCH_API_KEY": "empty",
        "SEARCH_PROVIDER_KEY": "present",
    }


def test_default_web_search_config_uses_free_first_chain(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_MODE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_CHAIN", raising=False)
    monkeypatch.setenv("WEB_SEARCH_MAX_PROVIDER_ATTEMPTS", "4")
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ANYSEARCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ANYSEARCH_PROXY_MODE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_CHAIN_DEADLINE_SECONDS", raising=False)
    get_settings.cache_clear()

    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    assert settings.web_search_provider_mode == "chain"
    assert settings.web_search_provider == "multi-free"
    assert settings.web_search_provider_chain == ("anysearch,ddgs,bing,duckduckgo,baidu,multi-free")
    assert settings.web_search_max_provider_attempts == 4
    assert settings.web_search_provider_timeout_seconds == 6.0
    assert settings.anysearch_timeout_seconds == 6.0
    assert settings.anysearch_proxy_mode == "auto"
    assert settings.web_search_chain_deadline_seconds == 20.0


def test_anysearch_timeout_defaults_to_provider_timeout(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("ANYSEARCH_TIMEOUT_SECONDS", "")
    get_settings.cache_clear()

    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    assert settings.anysearch_timeout_seconds == 7.5


def test_default_web_search_attempt_budget_covers_the_default_chain(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_CHAIN", raising=False)
    monkeypatch.delenv("WEB_SEARCH_MAX_PROVIDER_ATTEMPTS", raising=False)
    get_settings.cache_clear()

    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    assert len(settings.web_search_provider_chain.split(",")) == 6
    assert settings.web_search_max_provider_attempts == 6


def test_anysearch_provider_posts_travel_query_with_optional_authorization(monkeypatch):
    monkeypatch.setenv("ANYSEARCH_API_KEY", "anysearch-key")
    monkeypatch.setenv("ANYSEARCH_DOMAIN", "travel")
    monkeypatch.setenv("ANYSEARCH_TAG", "")
    monkeypatch.setenv("ANYSEARCH_ZONE", "cn")
    monkeypatch.setenv("ANYSEARCH_LANGUAGE", "zh-CN")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    def fake_http_post(url: str, body: dict, _timeout: float, headers: Optional[dict] = None) -> dict:
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return {
            "code": 0,
            "message": "success",
            "data": {
                "results": [
                    {
                        "title": "故宫博物院参观须知",
                        "url": "https://www.dpm.org.cn/visit/",
                        "snippet": "请通过官方渠道预约。",
                    }
                ],
                "metadata": {"request_id": "req_test"},
            },
        }

    try:
        result = WebSearchProvider(provider_name="anysearch", http_post=fake_http_post).search("故宫预约", count=3)
    finally:
        get_settings.cache_clear()

    assert captured["url"] == ANYSEARCH_WEB_SEARCH_URL
    assert captured["body"] == {
        "query": "故宫预约",
        "max_results": 3,
        "domain": "travel",
        "zone": "cn",
        "language": "zh-CN",
    }
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer anysearch-key",
    }
    assert result.provider_name == "anysearch"
    assert result.results[0].url == "https://www.dpm.org.cn/visit/"
    assert result.provider_diagnostics[0]["authentication"] == "api_key"
    assert "故宫预约" not in str(result.provider_diagnostics)


def test_anysearch_provider_uses_anonymous_request_without_api_key(monkeypatch):
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    def fake_http_post(_url: str, _body: dict, _timeout: float, headers: Optional[dict] = None) -> dict:
        captured["headers"] = headers
        return {
            "code": 0,
            "data": {"results": [{"title": "北京旅游官网", "url": "https://visitbeijing.com/", "snippet": "官方信息"}]},
        }

    try:
        result = WebSearchProvider(provider_name="any-search", http_post=fake_http_post).search("北京旅游", count=1)
        diagnostics = web_search_provider_config_diagnostics("any-search")
    finally:
        get_settings.cache_clear()

    assert captured["headers"] == {"Content-Type": "application/json"}
    assert result.provider_diagnostics[0]["authentication"] == "anonymous"
    assert diagnostics["providers"][0]["providerName"] == "anysearch"
    assert diagnostics["providers"][0]["configured"] is True


def test_anysearch_system_transport_uses_process_proxy_without_exposing_it(monkeypatch):
    import src.providers.travel_tools as travel_tools_module

    proxy_value = "http://proxy-user:proxy-secret@127.0.0.1:8899"
    monkeypatch.setenv("HTTPS_PROXY", proxy_value)
    monkeypatch.setenv("ANYSEARCH_PROXY_MODE", "system")
    monkeypatch.setenv("ANYSEARCH_TIMEOUT_SECONDS", "2.5")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "results": [
                            {
                                "title": "故宫博物院官方预约",
                                "url": "https://www.dpm.org.cn/visit/",
                                "snippet": "故宫预约官方信息",
                            }
                        ]
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(_request, timeout):
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(travel_tools_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        travel_tools_module,
        "build_opener",
        lambda *_args: (_ for _ in ()).throw(AssertionError("system mode must use urlopen")),
    )

    try:
        result = AnySearchWebSearchProvider().search("故宫预约", count=1)
    finally:
        get_settings.cache_clear()

    diagnostic = result.provider_diagnostics[0]
    assert captured["timeout"] == 2.5
    assert diagnostic["transportRoute"] == "system"
    assert diagnostic["proxyConfigured"] is True
    assert diagnostic["timeoutSeconds"] == 2.5
    assert diagnostic["reasonCode"] == "ok"
    assert proxy_value not in str(diagnostic)


def test_anysearch_direct_transport_uses_empty_proxy_handler_without_mutating_environment(monkeypatch):
    import src.providers.travel_tools as travel_tools_module

    proxy_value = "http://proxy-user:proxy-secret@127.0.0.1:8899"
    monkeypatch.setenv("HTTP_PROXY", proxy_value)
    monkeypatch.setenv("HTTPS_PROXY", proxy_value)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("ANYSEARCH_PROXY_MODE", "direct")
    get_settings.cache_clear()
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "data": {
                        "results": [
                            {
                                "title": "北京旅游官方信息",
                                "url": "https://visitbeijing.com/",
                                "snippet": "北京旅游官方信息",
                            }
                        ]
                    },
                }
            ).encode("utf-8")

    class FakeOpener:
        def open(self, _request, timeout):
            captured["timeout"] = timeout
            return FakeResponse()

    def fake_build_opener(handler):
        captured["proxies"] = handler.proxies
        return FakeOpener()

    monkeypatch.setattr(travel_tools_module, "build_opener", fake_build_opener)
    monkeypatch.setattr(
        travel_tools_module,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct mode must not use process-level urlopen")
        ),
    )

    try:
        result = AnySearchWebSearchProvider(timeout_seconds=1.25).search("北京旅游", count=1)
    finally:
        get_settings.cache_clear()

    diagnostic = result.provider_diagnostics[0]
    assert captured["proxies"] == {}
    assert captured["timeout"] == 1.25
    assert diagnostic["transportRoute"] == "direct"
    assert diagnostic["proxyConfigured"] is False
    assert os.environ["HTTP_PROXY"] == proxy_value
    assert os.environ["HTTPS_PROXY"] == proxy_value
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:1080"
    assert proxy_value not in str(diagnostic)


def test_anysearch_auto_tries_direct_then_system_only_after_transport_failure(monkeypatch):
    provider = AnySearchWebSearchProvider(proxy_mode="auto", timeout_seconds=1.0)
    calls: list[tuple[str, float]] = []

    def fake_post(_url, _body, _headers, *, timeout_seconds=None, transport_route=None):
        calls.append((str(transport_route), float(timeout_seconds or 0)))
        if transport_route == "direct":
            raise ToolProviderError("transport_error")
        return {
            "code": 0,
            "data": {
                "results": [
                    {
                        "title": "故宫博物院参观须知",
                        "url": "https://www.dpm.org.cn/visit/",
                        "snippet": "故宫博物院官方参观信息。",
                    }
                ]
            },
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    result = ChainedWebSearchProvider(
        providers=[provider],
        max_provider_attempts=1,
        total_deadline_seconds=1.5,
    ).search("故宫博物院 参观 信息", count=1)

    assert [route for route, _timeout in calls] == ["direct", "system"]
    assert all(0 < timeout <= 1.0 for _route, timeout in calls)
    transport_attempts = result.provider_diagnostics[0]["providerDiagnostics"]
    assert [item["transportRoute"] for item in transport_attempts] == [
        "direct",
        "system",
    ]
    assert transport_attempts[0]["reasonCode"] == "transport_error"
    assert transport_attempts[1]["reasonCode"] == "ok"


def test_anysearch_auto_does_not_switch_proxy_for_filtered_empty(monkeypatch):
    provider = AnySearchWebSearchProvider(proxy_mode="auto", timeout_seconds=1.0)
    calls: list[str] = []

    def fake_post(_url, _body, _headers, *, timeout_seconds=None, transport_route=None):
        del timeout_seconds
        calls.append(str(transport_route))
        return {
            "code": 0,
            "data": {
                "results": [
                    {
                        "title": "完全无关页面",
                        "url": "https://example.com/unrelated",
                        "snippet": "没有任何目标地点信息。",
                    }
                ]
            },
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    result = ChainedWebSearchProvider(
        providers=[provider],
        max_provider_attempts=1,
        total_deadline_seconds=1.5,
    ).search("故宫博物院 参观 信息", count=1)

    assert calls == ["direct"]
    assert result.results == []
    assert result.provider_diagnostics[0]["reasonCode"] == "filtered_empty"


def test_no_limit_keeps_old_entity_discovery_results_while_one_year_filters_them():
    old_result = {
        "title": "老城夜景地点 2020 年资料",
        "url": "https://example.com/old-night-place",
        "snippet": "2020年发布的老城夜景地点介绍。",
        "confidence": 0.8,
        "credibilityRank": "guide",
    }
    no_limit = ChainedWebSearchProvider(
        providers=[CountingSearchProvider("recorded-web", [old_result])],
        max_provider_attempts=1,
    ).search("老城 夜景 地点", count=1, freshness="noLimit")
    one_year = ChainedWebSearchProvider(
        providers=[CountingSearchProvider("recorded-web", [old_result])],
        max_provider_attempts=1,
    ).search("老城 夜景 地点", count=1, freshness="oneYear")

    assert len(no_limit.results) == 1
    assert one_year.results == []


def test_anysearch_nested_urlerror_timeout_is_classified_as_timeout(monkeypatch):
    import src.providers.travel_tools as travel_tools_module

    class TimedOutOpener:
        def open(self, _request, timeout):
            assert timeout == 0.25
            raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(
        travel_tools_module,
        "build_opener",
        lambda _handler: TimedOutOpener(),
    )
    result = ChainedWebSearchProvider(
        providers=[
            AnySearchWebSearchProvider(
                proxy_mode="direct",
                timeout_seconds=0.25,
            )
        ],
        max_provider_attempts=1,
        total_deadline_seconds=1.0,
    ).search("故宫博物院 官方 预约", count=1, freshness="oneYear")

    diagnostic = result.provider_diagnostics[0]
    assert diagnostic["reasonCode"] == "timeout"
    assert diagnostic["transportRoute"] == "direct"
    assert diagnostic["proxyConfigured"] is False


def test_chain_falls_back_after_anysearch_timeout_with_safe_transport_trace(monkeypatch):
    proxy_value = "http://proxy-secret@127.0.0.1:8899"
    monkeypatch.setenv("HTTPS_PROXY", proxy_value)
    monkeypatch.setenv("ANYSEARCH_PROXY_MODE", "system")
    get_settings.cache_clear()
    captured: dict[str, float] = {}

    def timed_out_post(_url, _body, timeout, _headers=None):
        captured["timeout"] = timeout
        raise TimeoutError(f"{proxy_value}/?q=PRIVATE")

    fallback = CountingSearchProvider(
        "bing-html-search",
        [
            {
                "title": "北京大学官方参观公告",
                "url": "https://www.pku.edu.cn/visit/",
                "snippet": "北京大学官方参观信息。",
                "confidence": 0.8,
                "credibilityRank": "official",
            }
        ],
    )
    provider = ChainedWebSearchProvider(
        providers=[
            AnySearchWebSearchProvider(timeout_seconds=0.25, http_post=timed_out_post),
            fallback,
        ],
        max_provider_attempts=2,
        min_accepted_results=1,
        total_deadline_seconds=1.0,
    )

    try:
        result = provider.search("北京大学 官方 参观 公告", freshness="oneYear")
    finally:
        get_settings.cache_clear()

    anysearch_attempt = result.provider_diagnostics[0]
    assert captured["timeout"] <= 0.25
    assert fallback.call_count == 1
    assert result.results[0].provider_name == "bing-html-search"
    assert result.fallback_used is True
    assert anysearch_attempt == {
        "providerName": "anysearch",
        "status": "failed",
        "reason": "timeout",
        "reasonCode": "timeout",
        "durationMs": anysearch_attempt["durationMs"],
        "resultCount": 0,
        "transportRoute": "system",
        "proxyConfigured": True,
        "timeoutSeconds": anysearch_attempt["timeoutSeconds"],
    }
    diagnostics_text = str(result.provider_diagnostics)
    assert "proxy-secret" not in diagnostics_text
    assert "PRIVATE" not in diagnostics_text


def test_chain_caps_later_provider_timeout_to_remaining_deadline():
    captured: dict[str, float] = {}

    def slow_timeout(_url, _body, _timeout, _headers=None):
        time.sleep(0.02)
        raise TimeoutError("simulated AnySearch timeout")

    def bing_get(_url, timeout, _headers=None):
        captured["timeout"] = timeout
        return """
        <html><body><li class="b_algo">
          <h2><a href="https://www.pku.edu.cn/visit/">北京大学官方参观公告</a></h2>
          <div class="b_caption"><p>北京大学官方参观信息。</p></div>
        </li></body></html>
        """

    provider = ChainedWebSearchProvider(
        providers=[
            AnySearchWebSearchProvider(
                timeout_seconds=0.05,
                proxy_mode="direct",
                http_post=slow_timeout,
            ),
            BingHTMLSearchProvider(timeout_seconds=0.5, http_get=bing_get),
        ],
        max_provider_attempts=2,
        min_accepted_results=1,
        total_deadline_seconds=0.06,
    )

    result = provider.search("北京大学 官方 参观 公告", freshness="oneYear")

    assert result.results
    assert 0 < captured["timeout"] < 0.05
    assert result.provider_diagnostics[1]["timeoutSeconds"] < 0.05


def test_effective_web_search_timeout_never_exceeds_submillisecond_remaining_deadline(
    monkeypatch,
):
    import src.providers.travel_tools as travel_tools_module

    token = travel_tools_module._WEB_SEARCH_CHAIN_DEADLINE.set(100.0004)
    monkeypatch.setattr(travel_tools_module.time, "perf_counter", lambda: 100.0)
    try:
        timeout_seconds = travel_tools_module._effective_web_search_timeout(6.0)
    finally:
        travel_tools_module._WEB_SEARCH_CHAIN_DEADLINE.reset(token)

    assert 0 < timeout_seconds <= 0.0004


def test_submillisecond_timeout_trace_remains_positive_and_auditable():
    import src.providers.travel_tools as travel_tools_module

    provider = AnySearchWebSearchProvider(
        timeout_seconds=0.0004,
        proxy_mode="direct",
        http_post=lambda *_args, **_kwargs: {},
    )
    trace = ChainedWebSearchProvider._attempt_trace(provider, 0.0004)
    sanitized = travel_tools_module._safe_web_provider_diagnostics(
        [{"providerName": "anysearch", "status": "failed", **trace}]
    )

    assert trace["timeoutSeconds"] == 0.0004
    assert sanitized[0]["timeoutSeconds"] == 0.0004


def test_chain_stops_before_next_provider_when_total_deadline_is_exhausted():
    class SlowFailureProvider:
        provider_name = "slow-provider"

        def search(self, _query, count=5, freshness="noLimit"):
            del count, freshness
            time.sleep(0.025)
            raise TimeoutError("simulated slow provider")

    fallback = CountingSearchProvider(
        "bing-html-search",
        [
            {
                "title": "不应调用的结果",
                "url": "https://example.com/not-called",
                "snippet": "fallback",
            }
        ],
    )
    provider = ChainedWebSearchProvider(
        providers=[SlowFailureProvider(), fallback],
        max_provider_attempts=2,
        total_deadline_seconds=0.01,
    )
    started = time.perf_counter()

    result = provider.search("北京大学 官方 参观 公告", freshness="oneYear")

    assert time.perf_counter() - started < 0.08
    assert fallback.call_count == 0
    assert result.results == []
    assert result.provider_diagnostics[-1]["reasonCode"] == "chain_deadline_exhausted"


def test_multi_free_diagnostics_and_caveat_do_not_export_child_exception_payload():
    secret = "http://proxy-secret@127.0.0.1:8899/?q=PRIVATE"
    failed = FakeSearchProvider("baidu-html-search", error=TimeoutError(secret))
    successful = FakeSearchProvider(
        "duckduckgo-html-search",
        [
            {
                "title": "故宫博物院官方预约",
                "url": "https://www.dpm.org.cn/visit",
                "snippet": "故宫博物院官方预约信息。",
                "credibilityRank": "official",
            }
        ],
    )

    result = MultiFreeWebSearchProvider(providers=[failed, successful]).search(
        "故宫博物院 官方 预约",
        count=1,
    )

    exported = f"{result.provider_diagnostics} {result.user_visible_caveat}"
    assert "proxy-secret" not in exported
    assert "PRIVATE" not in exported
    assert result.provider_diagnostics[1]["reasonCode"] == "timeout"


def test_bing_html_search_provider_parses_result_and_decodes_redirect():
    html = """
    <html><body>
      <li class="b_algo">
        <h2><a href="https://www.bing.com/ck/a?!&amp;&amp;u=a1aHR0cHM6Ly93d3cucGt1LmVkdS5jbi8&amp;ntb=1">北京大学</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">北京大学官方主页。</p></div>
      </li>
    </body></html>
    """
    captured: dict[str, object] = {}

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        captured["url"] = url
        captured["headers"] = headers
        return html

    result = BingHTMLSearchProvider(http_get=fake_http_get).search("北京大学 官方", count=1)

    assert str(captured["url"]).startswith(BING_HTML_SEARCH_URL + "?")
    assert result.provider_name == "bing-html-search"
    assert result.results[0].url == "https://www.pku.edu.cn/"
    assert result.results[0].provider_name == "bing-html-search"
    assert result.provider_diagnostics[0]["endpointHost"] == "www.bing.com"
    assert "北京大学" not in str(result.provider_diagnostics)


def test_bing_html_search_provider_ignores_environment_proxy_without_config(monkeypatch):
    import src.providers.travel_tools as travel_tools_module

    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9999")
    monkeypatch.delenv("BING_PROXY_URL", raising=False)
    monkeypatch.delenv("BING_HTTP_PROXY", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROXY_URL", raising=False)
    monkeypatch.delenv("WEB_SEARCH_HTTP_PROXY", raising=False)
    get_settings.cache_clear()
    html = """
    <html><body>
      <li class="b_algo">
        <h2><a href="https://www.pku.edu.cn/">北京大学</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">北京大学官方主页。</p></div>
      </li>
    </body></html>
    """
    proxy_calls: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return html.encode("utf-8")

    class FakeOpener:
        def open(self, _request, timeout: float):
            assert timeout == get_settings().web_search_provider_timeout_seconds
            return FakeResponse()

    def fake_proxy_handler(proxies):
        proxy_calls.append(proxies)
        return object()

    def fake_build_opener(_proxy_handler):
        return FakeOpener()

    monkeypatch.setattr(travel_tools_module, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(travel_tools_module, "build_opener", fake_build_opener)
    try:
        result = BingHTMLSearchProvider().search("北京大学 官方", count=1)
    finally:
        get_settings.cache_clear()

    assert proxy_calls == [{}]
    assert result.provider_diagnostics[0]["proxyUsed"] is False
    assert result.results[0].url == "https://www.pku.edu.cn/"


def test_bing_html_search_filters_irrelevant_official_results():
    html = """
    <html><body>
      <li class="b_algo">
        <h2><a href="https://www.beijing.gov.cn/">Beijing - 北京市人民政府门户网站</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">北京概况。</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://news.pku.edu.cn/xwzh/fc6726b809ab40289346b7c9d9ccb4b7.htm">北京大学2026年校园开放日系列活动举行</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">北京大学校园开放日和参观预约信息。</p></div>
      </li>
    </body></html>
    """

    def fake_http_get(_url: str, _timeout: float, _headers: Optional[dict] = None) -> str:
        return html

    result = BingHTMLSearchProvider(http_get=fake_http_get).search("北京大学 2026 国庆 预约 官方公告", count=2)

    assert [item.url for item in result.results] == [
        "https://news.pku.edu.cn/xwzh/fc6726b809ab40289346b7c9d9ccb4b7.htm"
    ]


def test_bocha_web_search_provider_returns_structured_results():
    def fake_http_post(url: str, body: dict, _timeout: float, headers: Optional[dict] = None) -> dict:
        assert url == BOCHA_WEB_SEARCH_URL
        assert body["query"] == "故宫博物院 预约 门票"
        assert body["summary"] is True
        assert body["freshness"] == "noLimit"
        assert body["count"] == 5
        assert headers and headers["Authorization"] == "Bearer test-key"
        return {
            "code": 200,
            "webPages": {
                "value": [
                    {
                        "name": "故宫博物院官方预约",
                        "url": "https://www.dpm.org.cn/visit",
                        "snippet": "故宫博物院实行实名预约购票。",
                        "summary": "故宫博物院实行实名预约购票，需通过官方渠道预约。",
                        "datePublished": "2026-06-01T00:00:00+08:00",
                        "siteName": "故宫博物院",
                    }
                ]
            },
        }

    result = BochaWebSearchProvider(api_key="test-key", http_post=fake_http_post).search("故宫博物院 预约 门票")

    assert result.query == "故宫博物院 预约 门票"
    assert result.provider_name == "bocha-web-search"
    assert result.fallback_used is False
    assert result.confidence > 0
    assert result.results[0].title == "故宫博物院官方预约"
    assert result.results[0].snippet == "故宫博物院实行实名预约购票，需通过官方渠道预约。"
    assert result.results[0].summary == "故宫博物院实行实名预约购票，需通过官方渠道预约。"
    assert result.results[0].published_at == "2026-06-01T00:00:00+08:00"
    assert result.results[0].source_name == "故宫博物院"
    assert result.results[0].provider_name == "bocha-web-search"
    assert result.results[0].fallback_used is False
    assert result.results[0].credibility_rank == "official"
    assert result.results[0].queried_at
    assert result.results[0].user_visible_caveat
    assert result.provider_diagnostics[0]["requestOptions"]["summary"] is True


def test_web_search_provider_failure_returns_error_metadata_without_mock_results():
    def failing_http_post(_url: str, _body: dict, _timeout: float, _headers: Optional[dict] = None) -> dict:
        raise RuntimeError("network down")

    result = ResilientWebSearchProvider(
        default_provider=WebSearchProvider(api_key="test-key", provider_name="bocha", http_post=failing_http_post)
    ).search("上海博物馆 开放时间")

    assert result.provider_name == "bocha-web-search"
    assert result.fallback_used is False
    assert result.failure_reason == "provider_error"
    assert result.user_visible_caveat
    assert result.results == []


def test_tavily_web_search_provider_parses_results_without_answer():
    def fake_http_post(url: str, body: dict, _timeout: float, headers: Optional[dict] = None) -> dict:
        assert url == TAVILY_WEB_SEARCH_URL
        assert body["query"] == "北京大学 2026 国庆 官方公告"
        assert body["include_answer"] is False
        assert body["include_raw_content"] is False
        assert body["time_range"] == "year"
        assert headers and headers["Authorization"] == "Bearer tavily-key"
        return {
            "answer": "should not be used as evidence",
            "results": [
                {
                    "title": "北京大学 2026 国庆参观预约官方公告",
                    "url": "https://www.pku.edu.cn/notice/2026-national-day",
                    "content": "2026年国庆期间校园参观需预约。",
                    "score": 0.91,
                }
            ],
        }

    result = TavilyWebSearchProvider(api_key="tavily-key", http_post=fake_http_post).search(
        "北京大学 2026 国庆 官方公告",
        freshness="oneYear",
    )

    assert result.provider_name == "tavily"
    assert result.results[0].title == "北京大学 2026 国庆参观预约官方公告"
    assert result.results[0].provider_name == "tavily"
    assert result.results[0].credibility_rank == "official"


def test_brave_web_search_provider_parses_results():
    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> dict:
        assert url.startswith(BRAVE_WEB_SEARCH_URL)
        assert "freshness=pm" in url
        assert "country=CN" in url
        assert "search_lang=zh" in url
        assert "ui_lang=zh-CN" in url
        assert "extra_snippets=true" in url
        assert headers and headers["X-Subscription-Token"] == "brave-key"
        return {
            "web": {
                "results": [
                    {
                        "title": "故宫博物院开放预约公告",
                        "url": "https://www.dpm.org.cn/visit",
                        "description": "故宫博物院开放时间与预约规则。",
                    }
                ]
            }
        }

    result = BraveWebSearchProvider(api_key="brave-key", http_get=fake_http_get).search(
        "故宫博物院 官方公告", freshness="oneMonth"
    )

    assert result.provider_name == "brave-web-search"
    assert result.results[0].provider_name == "brave-web-search"
    assert result.results[0].source_name == "www.dpm.org.cn"
    assert result.provider_diagnostics[0]["requestOptions"] == {
        "country": "CN",
        "searchLang": "zh",
        "uiLang": "zh-CN",
        "extraSnippets": True,
    }


def test_chained_web_search_bocha_success_stops_before_html_fallback():
    bocha = CountingSearchProvider(
        "bocha-web-search",
        [
            {
                "title": "北京大学 2026 国庆预约官方公告",
                "url": "https://www.pku.edu.cn/notice/2026",
                "snippet": "2026年国庆期间校园参观需预约。",
                "confidence": 0.74,
                "credibilityRank": "official",
            }
        ],
    )
    html_fallback = CountingSearchProvider(
        "multi-free-search",
        [
            {
                "title": "不应调用的 fallback",
                "url": "https://example.com/fallback",
                "snippet": "fallback",
            }
        ],
    )
    provider = ChainedWebSearchProvider(providers=[bocha, html_fallback], min_accepted_results=1)

    result = provider.search("北京大学 2026 国庆 官方公告", freshness="oneYear")

    assert result.successful_providers == ["bocha-web-search"]
    assert result.results[0].provider_name == "bocha-web-search"
    assert bocha.call_count == 1
    assert html_fallback.call_count == 0


def test_chain_attempts_cheetah_ddg_before_multi_free_bocha():
    cheetah = CountingSearchProvider(
        "cheetah-duckduckgo-html-search",
        [
            {
                "title": "北京大学 2026 国庆预约官方公告",
                "url": "https://www.pku.edu.cn/notice/2026",
                "snippet": "2026年国庆期间校园参观需预约。",
                "confidence": 0.74,
                "credibilityRank": "official",
            }
        ],
    )
    multi_free = CountingSearchProvider(
        "multi-free-search",
        [{"title": "不应调用的 multi-free", "url": "https://example.com/multi", "snippet": "fallback"}],
    )
    bocha = CountingSearchProvider(
        "bocha-web-search",
        [{"title": "不应调用的 bocha", "url": "https://example.com/bocha", "snippet": "fallback"}],
    )
    provider = ChainedWebSearchProvider(providers=[cheetah, multi_free, bocha], min_accepted_results=1)

    result = provider.search("北京大学 2026 国庆 官方公告", freshness="oneYear")

    assert result.successful_providers == ["cheetah-duckduckgo-html-search"]
    assert result.results[0].provider_name == "cheetah-duckduckgo-html-search"
    assert cheetah.call_count == 1
    assert multi_free.call_count == 0
    assert bocha.call_count == 0


def test_bocha_circuit_does_not_skip_cheetah_ddg():
    bocha = CountingSearchProvider("bocha-web-search", error=RuntimeError("auth_failed: HTTP 403"))
    cheetah = CountingSearchProvider(
        "cheetah-duckduckgo-html-search",
        [
            {
                "title": "上海博物馆 2026 国庆预约官方公告",
                "url": "https://www.shanghaimuseum.net/mu/frontend/pg/index",
                "snippet": "2026年国庆期间开放预约信息。",
                "confidence": 0.74,
                "credibilityRank": "official",
            }
        ],
    )
    provider = ChainedWebSearchProvider(providers=[bocha, cheetah], max_provider_attempts=2, min_accepted_results=1)

    first = provider.search("上海博物馆 2026 国庆 官方公告", freshness="oneYear")
    second = provider.search("上海博物馆 2026 国庆 开放 官方公告", freshness="oneYear")

    assert first.failed_providers == ["bocha-web-search"]
    assert first.successful_providers == ["cheetah-duckduckgo-html-search"]
    assert second.skipped_providers == ["bocha-web-search"]
    assert second.provider_diagnostics[0]["reason"] == "circuit_open"
    assert second.successful_providers == ["cheetah-duckduckgo-html-search"]
    assert bocha.call_count == 1
    assert cheetah.call_count == 2


def test_chained_web_search_classifies_bocha_auth_failure():
    provider = ChainedWebSearchProvider(
        providers=[FakeSearchProvider("bocha-web-search", error=RuntimeError("auth_failed: HTTP 403"))]
    )

    result = provider.search("北京大学 官方公告")

    assert result.failed_providers == ["bocha-web-search"]
    assert result.provider_diagnostics[0]["reason"] == "auth_failed"


def test_searxng_web_search_provider_parses_results():
    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> dict:
        assert url.startswith("http://localhost:8888/search?")
        assert "format=json" in url
        assert "time_range=week" in url
        assert headers and headers["Accept"] == "application/json"
        return {
            "results": [
                {
                    "title": "上海博物馆官方预约",
                    "url": "https://www.shanghaimuseum.net/visit",
                    "content": "上海博物馆参观需要预约。",
                    "engine": "searxng-bing",
                }
            ]
        }

    result = SearXNGWebSearchProvider(base_url="http://localhost:8888", http_get=fake_http_get).search(
        "上海博物馆 官方预约",
        freshness="oneWeek",
    )

    assert result.provider_name == "searxng"
    assert result.results[0].source_name == "searxng-bing"
    assert result.results[0].provider_name == "searxng"


def test_ddgs_web_search_provider_parses_api_results_without_full_query_in_diagnostics():
    requested_urls: list[str] = []

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> dict:
        requested_urls.append(url)
        assert url.startswith("http://localhost:4479/search/text?")
        assert "max_results=2" in url or "count=2" in url
        assert headers and headers["Accept"] == "application/json"
        return {
            "results": [
                {
                    "title": "北京大学官方参观公告",
                    "href": "https://www.pku.edu.cn/visit",
                    "body": "北京大学校园参观预约信息以官方公告为准。",
                }
            ]
        }

    result = DDGSWebSearchProvider(api_base_url="http://localhost:4479", http_get=fake_http_get).search(
        "北京大学 预约 官方公告",
        count=2,
    )

    assert requested_urls
    assert result.provider_name == "ddgs"
    assert result.results[0].provider_name == "ddgs"
    assert result.provider_diagnostics[0]["endpointHost"] == "localhost:4479"
    assert result.provider_diagnostics[0]["endpointPath"] == "/search/text"
    assert "北京大学" not in str(result.provider_diagnostics)


def test_ddgs_web_search_provider_falls_back_to_legacy_search_endpoint():
    requested_urls: list[str] = []

    def fake_http_get(url: str, _timeout: float, _headers: Optional[dict] = None) -> dict:
        requested_urls.append(url)
        if url.startswith("http://localhost:4479/search/text?"):
            raise ToolProviderError("HTTP 404")
        assert url.startswith("http://localhost:4479/search?")
        return [
            {
                "title": "故宫官方预约公告",
                "href": "https://www.dpm.org.cn/visit",
                "body": "故宫预约信息以官方公告为准。",
            }
        ]

    result = DDGSWebSearchProvider(api_base_url="http://localhost:4479", http_get=fake_http_get).search(
        "故宫 预约 官方公告",
        count=1,
    )

    assert len(requested_urls) == 2
    assert result.results[0].title == "故宫官方预约公告"
    assert result.provider_diagnostics[0]["endpointPath"] == "/search"


def test_ddgs_legacy_package_without_timeout_support_falls_back_without_unbounded_client(
    monkeypatch,
):
    init_calls: list[dict[str, object]] = []

    class LegacyDDGS:
        def __init__(self, **kwargs):
            init_calls.append(kwargs)
            if kwargs:
                raise TypeError("timeout keyword unsupported")
            raise AssertionError("unbounded DDGS client must not be constructed")

    monkeypatch.setitem(sys.modules, "ddgs", type("DDGSModule", (), {"DDGS": LegacyDDGS})())
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "ddgs")
    provider = DDGSWebSearchProvider(api_base_url="", timeout_seconds=0.05)
    cli_calls: list[tuple[str, int, str]] = []

    def bounded_cli(_path, query, count, freshness):
        cli_calls.append((query, count, freshness))
        return [], [{"providerName": "ddgs", "status": "no_results"}]

    monkeypatch.setattr(provider, "_search_cli", bounded_cli)

    provider._search_package_or_cli("北京大学 官方公告", 1, "oneYear")

    assert len(init_calls) == 1
    assert 0 < float(init_calls[0]["timeout"]) <= 0.05
    assert cli_calls == [("北京大学 官方公告", 1, "oneYear")]


def test_ddgs_legacy_package_without_timeout_or_cli_reports_accurate_reason(
    monkeypatch,
):
    class LegacyDDGS:
        def __init__(self, **kwargs):
            assert "timeout" in kwargs
            raise TypeError("timeout keyword unsupported")

    monkeypatch.setitem(sys.modules, "ddgs", type("DDGSModule", (), {"DDGS": LegacyDDGS})())
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "")
    provider = DDGSWebSearchProvider(api_base_url="", timeout_seconds=0.05)

    try:
        provider._search_package_or_cli("北京大学 官方公告", 1, "oneYear")
    except ToolProviderError as error:
        assert str(error) == "ddgs_package_timeout_unsupported"
    else:
        raise AssertionError("Expected a bounded DDGS compatibility error")


def test_ddgs_api_unreachable_returns_startup_diagnostics(monkeypatch):
    def fake_urlopen(_request, timeout: float):
        raise URLError("connection refused")

    monkeypatch.setattr("src.providers.travel_tools.urlopen", fake_urlopen)
    monkeypatch.setattr("src.providers.travel_tools._ddgs_package_available", lambda: False)
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "")

    try:
        DDGSWebSearchProvider(api_base_url="http://localhost:4479").search("故宫 官方公告", count=1)
    except ToolProviderError as error:
        message = str(error)
        assert "ddgs_api_unreachable" in message
        assert "ddgs api --host 127.0.0.1 --port 4479" in message
        assert "key=" not in message
    else:
        raise AssertionError("Expected ToolProviderError")


def test_ddgs_web_search_provider_falls_back_to_cli_when_api_unreachable(monkeypatch):
    requested_urls: list[str] = []
    cli_args: list[str] = []

    def fake_http_get(url: str, _timeout: float, _headers: Optional[dict] = None) -> dict:
        requested_urls.append(url)
        raise ToolProviderError("ddgs_api_unreachable: connection refused")

    def fake_run(args, capture_output: bool, text: bool, timeout: float, check: bool):
        cli_args.extend(args)
        output_path = args[args.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {
                        "title": "北京大学 2026 国庆校园参观预约官方公告",
                        "href": "https://www.pku.edu.cn/notice/2026",
                        "body": "2026年国庆期间校园参观需提前预约。",
                    }
                ],
                handle,
                ensure_ascii=False,
            )
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("src.providers.travel_tools._ddgs_package_available", lambda: False)
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "ddgs")
    monkeypatch.setattr("src.providers.travel_tools.subprocess.run", fake_run)

    result = DDGSWebSearchProvider(api_base_url="http://localhost:4479", http_get=fake_http_get).search(
        "北京大学 2026 国庆 预约 官方公告",
        count=1,
    )

    assert len(requested_urls) == 2
    assert cli_args[:3] == ["ddgs", "text", "-q"]
    assert result.results[0].provider_name == "ddgs"
    assert result.provider_diagnostics[0]["status"] == "failed"
    assert result.provider_diagnostics[0]["reason"] == "ddgs_api_unreachable"
    assert result.provider_diagnostics[1]["status"] == "success"
    assert result.provider_diagnostics[1]["cliAvailable"] is True
    assert "北京大学" not in str(result.provider_diagnostics)


def test_ddgs_web_search_provider_recovers_cli_json_from_stdout(monkeypatch):
    def fake_run(args, capture_output: bool, text: bool, timeout: float, check: bool):
        output_path = args[args.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("non-json cli banner")
        stdout = json.dumps(
            [
                {
                    "title": "北京大学 2026 国庆校园参观预约官方公告",
                    "href": "https://www.pku.edu.cn/notice/2026",
                    "body": "2026年国庆期间校园参观需提前预约。",
                }
            ],
            ensure_ascii=False,
        )
        return type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr("src.providers.travel_tools._ddgs_package_available", lambda: False)
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "ddgs")
    monkeypatch.setattr("src.providers.travel_tools.subprocess.run", fake_run)

    result = DDGSWebSearchProvider(api_base_url="").search(
        "北京大学 2026 国庆 预约 官方公告",
        count=1,
    )

    assert result.results[0].url == "https://www.pku.edu.cn/notice/2026"
    assert result.provider_diagnostics[0]["cliAvailable"] is True


def test_ddgs_web_search_provider_retries_cli_after_invalid_json(monkeypatch):
    call_count = 0

    def fake_run(args, capture_output: bool, text: bool, timeout: float, check: bool):
        nonlocal call_count
        call_count += 1
        output_path = args[args.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            if call_count == 1:
                handle.write("non-json cli banner")
            else:
                json.dump(
                    [
                        {
                            "title": "北京大学 2026 国庆校园参观预约官方公告",
                            "href": "https://www.pku.edu.cn/notice/2026",
                            "body": "2026年国庆期间校园参观需提前预约。",
                        }
                    ],
                    handle,
                    ensure_ascii=False,
                )
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("src.providers.travel_tools._ddgs_package_available", lambda: False)
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "ddgs")
    monkeypatch.setattr("src.providers.travel_tools.subprocess.run", fake_run)

    result = DDGSWebSearchProvider(api_base_url="").search(
        "北京大学 2026 国庆 预约 官方公告",
        count=1,
    )

    assert call_count == 2
    assert result.results[0].title == "北京大学 2026 国庆校园参观预约官方公告"
    assert result.provider_diagnostics[0]["attemptCount"] == 2


def test_searxng_json_disabled_returns_explicit_reason():
    def fake_http_get(_url: str, _timeout: float, _headers: Optional[dict] = None) -> dict:
        raise ToolProviderError("HTTP 403")

    try:
        SearXNGWebSearchProvider(base_url="http://localhost:8080", http_get=fake_http_get).search("故宫 官方公告")
    except ToolProviderError as error:
        assert "searxng_json_format_disabled" in str(error)
    else:
        raise AssertionError("Expected ToolProviderError")


def test_chained_web_search_skips_google_cse_missing_config():
    provider = ChainedWebSearchProvider(
        providers=[
            GoogleProgrammableSearchProvider(api_key="", cx=""),
            FakeSearchProvider(
                "brave-web-search",
                [
                    {
                        "title": "故宫博物院官方公告",
                        "url": "https://www.dpm.org.cn/notice",
                        "snippet": "2026年开放预约信息。",
                        "confidence": 0.7,
                        "credibilityRank": "official",
                    }
                ],
            ),
        ]
    )

    result = provider.search("故宫博物院 2026 官方公告", freshness="oneYear")

    assert result.provider_name == "chained-web-search"
    assert result.skipped_providers == ["google-cse"]
    assert "google-cse" not in result.failed_providers
    assert result.successful_providers == ["brave-web-search"]
    assert result.provider_diagnostics[0]["reason"] == "skipped_missing_config"


def test_chained_web_search_continues_after_tavily_failure_to_brave_success():
    provider = ChainedWebSearchProvider(
        providers=[
            FakeSearchProvider("tavily", error=TimeoutError("timed out")),
            FakeSearchProvider(
                "brave-web-search",
                [
                    {
                        "title": "北京大学 2026 国庆预约官方公告",
                        "url": "https://www.pku.edu.cn/notice",
                        "snippet": "2026年国庆期间校园预约规则。",
                        "confidence": 0.72,
                        "credibilityRank": "official",
                    }
                ],
            ),
        ]
    )

    result = provider.search("北京大学 2026 国庆 官方公告", freshness="oneYear")

    assert result.results
    assert result.failed_providers == ["tavily"]
    assert result.successful_providers == ["brave-web-search"]
    assert result.provider_diagnostics[0]["reason"] == "timeout"
    assert result.fallback_used is True


def test_chained_web_search_no_results_does_not_open_provider_circuit():
    baidu = CountingSearchProvider(
        "baidu-html-search",
        error=ToolProviderError("Baidu search returned no usable fresh public result URLs."),
    )
    duckduckgo = CountingSearchProvider(
        "duckduckgo-html-search",
        [
            {
                "title": "北京大学 2026 国庆预约官方公告",
                "url": "https://www.pku.edu.cn/notice/2026",
                "snippet": "2026年国庆期间校园参观需预约。",
                "confidence": 0.74,
                "credibilityRank": "official",
            }
        ],
    )
    provider = ChainedWebSearchProvider(providers=[baidu, duckduckgo], max_provider_attempts=2)

    first = provider.search("北京大学 2026 国庆 官方公告", freshness="oneYear")
    second = provider.search("北京大学 2026 国庆 官方公告 预约", freshness="oneYear")

    assert first.successful_providers == ["duckduckgo-html-search"]
    assert second.successful_providers == ["duckduckgo-html-search"]
    assert baidu.call_count == 2
    assert duckduckgo.call_count == 2
    assert first.provider_diagnostics[0]["providerName"] == "baidu-html-search"
    assert first.provider_diagnostics[0]["reason"] == "no_usable_results"


def test_chained_web_search_does_not_open_multi_free_global_circuit():
    multi_free = CountingSearchProvider("multi-free-search", error=TimeoutError("duckduckgo: timed out"))
    duckduckgo = CountingSearchProvider(
        "duckduckgo-html-search",
        [
            {
                "title": "上海博物馆官方预约",
                "url": "https://www.shanghaimuseum.net/mu/frontend/pg/index",
                "snippet": "2026年开放预约信息。",
                "confidence": 0.66,
                "credibilityRank": "official",
            }
        ],
    )
    provider = ChainedWebSearchProvider(providers=[multi_free, duckduckgo], max_provider_attempts=2)

    provider.search("上海博物馆 预约 官方公告", freshness="oneYear")
    provider.search("上海博物馆 国庆 开放 官方公告", freshness="oneYear")

    assert multi_free.call_count == 2
    assert duckduckgo.call_count == 2


def test_clear_web_search_runtime_state_clears_cache_and_circuit():
    timeout_provider = CountingSearchProvider("duckduckgo-html-search", error=TimeoutError("timed out"))
    success_provider = CountingSearchProvider(
        "baidu-html-search",
        [
            {
                "title": "北京大学官方公告",
                "url": "https://www.pku.edu.cn/notice/2026",
                "snippet": "官方公告。",
                "confidence": 0.8,
                "credibilityRank": "official",
            }
        ],
    )
    provider = ChainedWebSearchProvider(providers=[timeout_provider, success_provider], max_provider_attempts=2)

    first = provider.search("北京大学 国庆 官方公告", freshness="oneYear")
    second = provider.search("北京大学 国庆 官方公告", freshness="oneYear")
    cleared = clear_web_search_runtime_state()

    assert first.successful_providers == ["baidu-html-search"]
    assert second.provider_diagnostics[0]["status"] == "cache_hit"
    assert cleared["providerInstances"] >= 1
    assert cleared["cacheEntriesCleared"] >= 1
    assert cleared["circuitEntriesCleared"] >= 1
    assert provider._cache == {}
    assert provider._circuit_open_until == {}


def test_chained_web_search_all_failed_keeps_diagnostics():
    provider = ChainedWebSearchProvider(
        providers=[
            FakeSearchProvider("tavily", error=RuntimeError("tavily down")),
            FakeSearchProvider("brave-web-search", error=RuntimeError("brave down")),
        ]
    )

    result = provider.search("北京大学 官方公告")

    assert result.results == []
    assert result.failure_reason == "all_web_search_providers_failed_or_empty"
    assert result.failed_providers == ["tavily", "brave-web-search"]
    assert [item["status"] for item in result.provider_diagnostics] == ["failed", "failed"]


def test_chained_web_search_cache_avoids_repeated_provider_call():
    provider = CountingSearchProvider(
        "brave-web-search",
        [
            {
                "title": "故宫博物院官方预约",
                "url": "https://www.dpm.org.cn/visit",
                "snippet": "2026年预约规则。",
                "confidence": 0.72,
                "credibilityRank": "official",
            }
        ],
    )
    chain = ChainedWebSearchProvider(providers=[provider])

    first = chain.search("故宫博物院 2026 官方预约", freshness="oneYear")
    second = chain.search("故宫博物院 2026 官方预约", freshness="oneYear")

    assert provider.call_count == 1
    assert first.results[0].url == second.results[0].url
    assert second.provider_diagnostics[0]["status"] == "cache_hit"


def test_cheetah_ddg_search_parses_result_title():
    html = """
    <html>
      <body>
        <div class="result">
          <h2 class="result__title">
            <a href="/l/?kh=-1&amp;uddg=https%3A%2F%2Fwww.dpm.org.cn%2Fvisit">
              故宫博物院官方预约
            </a>
          </h2>
          <a class="result__snippet">故宫博物院实行实名预约购票。</a>
        </div>
      </body>
    </html>
    """
    captured: dict[str, object] = {}

    def fake_http_get(
        url: str, timeout: float, headers: Optional[dict] = None, params: Optional[dict] = None, proxy_url: str = ""
    ) -> str:
        captured["url"] = url
        captured["timeout"] = timeout
        captured["headers"] = headers
        captured["params"] = params
        captured["proxyUrl"] = proxy_url
        return html

    result = CheetahDuckDuckGoSearchProvider(http_get=fake_http_get).search("故宫 预约 门票", count=2)

    assert captured["url"] == CHEETAH_DUCKDUCKGO_HTML_SEARCH_URL
    assert captured["params"] == {"q": "故宫 预约 门票"}
    assert captured["headers"] == {"User-Agent": "Mozilla/5.0 (compatible)"}
    assert result.provider_name == "cheetah-duckduckgo-html-search"
    assert result.results[0].title == "故宫博物院官方预约"
    assert result.results[0].url == "https://www.dpm.org.cn/visit"
    assert result.results[0].provider_name == "cheetah-duckduckgo-html-search"
    assert result.provider_diagnostics[0]["parserName"] == "cheetahclaws_result_regex"


def test_cheetah_ddg_uses_proxy_10793(monkeypatch):
    monkeypatch.setenv("DUCKDUCKGO_PROXY_URL", "http://user:secret@127.0.0.1:10793")
    get_settings.cache_clear()
    html = """
    <html><body>
      <h2 class="result__title"><a href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a></h2>
      <a class="result__snippet">故宫博物院实行实名预约购票。</a>
    </body></html>
    """
    captured: dict[str, object] = {}

    def fake_http_get(
        url: str, _timeout: float, headers: Optional[dict] = None, params: Optional[dict] = None, proxy_url: str = ""
    ) -> str:
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["proxyUrl"] = proxy_url
        return html

    try:
        result = CheetahDuckDuckGoSearchProvider(http_get=fake_http_get).search("故宫 预约 门票", count=1)
    finally:
        get_settings.cache_clear()

    assert captured["proxyUrl"] == "http://user:secret@127.0.0.1:10793"
    diagnostics = result.provider_diagnostics[0]
    assert diagnostics["proxyUsed"] is True
    assert diagnostics["proxyHost"] == "127.0.0.1:10793"
    serialized = json.dumps(result.provider_diagnostics, ensure_ascii=False)
    assert "user:secret" not in serialized
    assert "q=" not in serialized
    assert "故宫 预约 门票" not in serialized


def test_cheetah_ddg_ignores_environment_proxy_without_config(monkeypatch):
    import httpx

    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9999")
    monkeypatch.delenv("DUCKDUCKGO_PROXY_URL", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROXY_URL", raising=False)
    get_settings.cache_clear()
    html = """
    <html><body>
      <h2 class="result__title"><a href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a></h2>
      <a class="result__snippet">故宫博物院实行实名预约购票。</a>
    </body></html>
    """
    captured: dict[str, object] = {}

    class FakeResponse:
        text = html

        def raise_for_status(self):
            return None

    def fake_httpx_get(_url: str, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_httpx_get)
    try:
        result = CheetahDuckDuckGoSearchProvider().search("故宫 预约 门票", count=1)
    finally:
        get_settings.cache_clear()

    assert captured["trust_env"] is False
    assert "proxy" not in captured
    assert result.results[0].provider_name == "cheetah-duckduckgo-html-search"


def test_cheetah_ddg_config_diagnostics_uses_web_search_proxy_url(monkeypatch):
    monkeypatch.delenv("DUCKDUCKGO_PROXY_URL", raising=False)
    monkeypatch.delenv("DUCKDUCKGO_HTTP_PROXY", raising=False)
    monkeypatch.setenv("WEB_SEARCH_PROXY_URL", "http://user:secret@127.0.0.1:10793")
    get_settings.cache_clear()

    try:
        diagnostics = web_search_provider_config_diagnostics("cheetah-ddg")
    finally:
        get_settings.cache_clear()

    provider = diagnostics["providers"][0]
    assert provider["providerName"] == "cheetah-duckduckgo-html-search"
    assert provider["configured"] is True
    assert provider["endpointHost"] == "html.duckduckgo.com"
    assert provider["proxyUsed"] is True
    assert provider["proxyHost"] == "127.0.0.1:10793"
    serialized = json.dumps(diagnostics, ensure_ascii=False)
    assert "user:secret" not in serialized
    assert "http://user:secret@127.0.0.1:10793" not in serialized


def test_duckduckgo_web_search_provider_parses_html_results_without_api_key():
    html = """
    <html>
      <body>
        <div class="result">
          <a class="result__a" href="/l/?kh=-1&amp;uddg=https%3A%2F%2Fwww.dpm.org.cn%2Fvisit">
            故宫博物院官方预约
          </a>
          <a class="result__snippet">故宫博物院实行实名预约购票。</a>
        </div>
        <div class="result">
          <a class="result__a" href="https://example.com/guide">故宫游览攻略</a>
          <a class="result__snippet">开放时间和路线参考。</a>
        </div>
      </body>
    </html>
    """

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        assert url.startswith(DUCKDUCKGO_HTML_SEARCH_URL)
        assert "q=%E6%95%85%E5%AE%AB" in url
        assert headers and "User-Agent" in headers
        return html

    result = DuckDuckGoWebSearchProvider(http_get=fake_http_get).search("故宫 预约 门票", count=2)

    assert result.provider_name == "duckduckgo-html-search"
    assert result.fallback_used is False
    assert result.failure_reason is None
    assert len(result.results) == 2
    assert result.results[0].title == "故宫博物院官方预约"
    assert result.results[0].url == "https://www.dpm.org.cn/visit"
    assert result.results[0].provider_name == "duckduckgo-html-search"
    assert result.results[0].credibility_rank == "official"


def test_duckduckgo_web_search_provider_falls_back_to_lite_after_html_timeout():
    lite_html = """
    <html>
      <body>
        <table>
          <tr>
            <td>
              <a rel="nofollow" class="result-link" href="https://www.pku.edu.cn/notice/2026">
                北京大学 2026 国庆预约官方公告
              </a>
            </td>
          </tr>
          <tr><td class="result-snippet">国庆期间校园参观需提前预约。</td></tr>
        </table>
      </body>
    </html>
    """
    calls: list[str] = []

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        calls.append(url)
        assert headers and "User-Agent" in headers
        if url.startswith(DUCKDUCKGO_HTML_SEARCH_URL):
            raise TimeoutError("timed out")
        assert url.startswith(DUCKDUCKGO_LITE_SEARCH_URL)
        return lite_html

    result = DuckDuckGoWebSearchProvider(http_get=fake_http_get).search("北京大学 国庆 预约 官方公告", count=2)

    assert len(calls) == 2
    assert result.provider_name == "duckduckgo-lite-search"
    assert result.fallback_used is True
    assert result.results[0].url == "https://www.pku.edu.cn/notice/2026"
    assert result.results[0].provider_name == "duckduckgo-lite-search"
    assert result.provider_diagnostics[0]["providerName"] == "duckduckgo-html-search"
    assert result.provider_diagnostics[0]["reason"] == "timeout"
    assert result.provider_diagnostics[1]["providerName"] == "duckduckgo-lite-search"
    assert result.provider_diagnostics[1]["status"] == "success"


def test_duckduckgo_web_search_provider_uses_configured_base_url_and_only_reports_endpoint_host(monkeypatch):
    monkeypatch.setenv("DUCKDUCKGO_SEARCH_BASE_URL", "http://127.0.0.1:10793")
    monkeypatch.delenv("DUCKDUCKGO_HTML_SEARCH_URL", raising=False)
    monkeypatch.delenv("DUCKDUCKGO_LITE_SEARCH_URL", raising=False)
    get_settings.cache_clear()
    html = """
    <html>
      <body>
        <div class="result">
          <a class="result__a" href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a>
          <a class="result__snippet">故宫博物院实行实名预约购票。</a>
        </div>
      </body>
    </html>
    """
    calls: list[str] = []

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        calls.append(url)
        assert headers and "User-Agent" in headers
        return html

    result = DuckDuckGoWebSearchProvider(http_get=fake_http_get).search("故宫 预约 门票", count=1)
    get_settings.cache_clear()

    assert calls and calls[0].startswith("http://127.0.0.1:10793/html/?")
    assert result.provider_diagnostics[0]["endpointHost"] == "127.0.0.1:10793"
    diagnostics_text = str(result.provider_diagnostics)
    assert "http://127.0.0.1:10793/html/?" not in diagnostics_text
    assert "q=" not in diagnostics_text


def test_duckduckgo_web_search_provider_uses_explicit_html_and_lite_urls(monkeypatch):
    monkeypatch.setenv("DUCKDUCKGO_SEARCH_BASE_URL", "http://127.0.0.1:10793")
    monkeypatch.setenv("DUCKDUCKGO_HTML_SEARCH_URL", "http://127.0.0.1:10793/custom-html/")
    monkeypatch.setenv("DUCKDUCKGO_LITE_SEARCH_URL", "http://127.0.0.1:10793/custom-lite/")
    get_settings.cache_clear()
    lite_html = """
    <html><body><a rel="nofollow" class="result-link" href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a></body></html>
    """
    calls: list[str] = []

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        calls.append(url)
        if url.startswith("http://127.0.0.1:10793/custom-html/"):
            raise TimeoutError("timed out")
        assert url.startswith("http://127.0.0.1:10793/custom-lite/")
        return lite_html

    result = DuckDuckGoWebSearchProvider(http_get=fake_http_get).search("故宫 预约 门票", count=1)
    get_settings.cache_clear()

    assert calls[0].startswith("http://127.0.0.1:10793/custom-html/?")
    assert calls[1].startswith("http://127.0.0.1:10793/custom-lite/?")
    assert result.provider_name == "duckduckgo-lite-search"
    assert result.provider_diagnostics[0]["endpointHost"] == "127.0.0.1:10793"
    assert result.provider_diagnostics[1]["endpointHost"] == "127.0.0.1:10793"


def test_duckduckgo_web_search_provider_uses_proxy_and_reports_safe_host(monkeypatch):
    import src.providers.travel_tools as travel_tools_module

    monkeypatch.delenv("DUCKDUCKGO_SEARCH_BASE_URL", raising=False)
    monkeypatch.delenv("DUCKDUCKGO_HTML_SEARCH_URL", raising=False)
    monkeypatch.delenv("DUCKDUCKGO_LITE_SEARCH_URL", raising=False)
    monkeypatch.setenv("DUCKDUCKGO_PROXY_URL", "http://user:secret@127.0.0.1:10793")
    get_settings.cache_clear()
    html = """
    <html><body><a rel="nofollow" class="result-link" href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a></body></html>
    """
    opened_urls: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return html.encode("utf-8")

    class FakeOpener:
        def open(self, request, timeout: float):
            opened_urls.append(request.full_url)
            assert timeout == get_settings().web_search_provider_timeout_seconds
            return FakeResponse()

    build_calls = []

    def fake_build_opener(proxy_handler):
        build_calls.append(proxy_handler)
        return FakeOpener()

    monkeypatch.setattr(travel_tools_module, "build_opener", fake_build_opener)
    try:
        result = DuckDuckGoWebSearchProvider().search("故宫 预约 门票", count=1)
    finally:
        get_settings.cache_clear()

    assert build_calls
    assert opened_urls and opened_urls[0].startswith(DUCKDUCKGO_HTML_SEARCH_URL)
    diagnostics = result.provider_diagnostics[0]
    assert diagnostics["proxyUsed"] is True
    assert diagnostics["proxyHost"] == "127.0.0.1:10793"
    diagnostics_text = str(result.provider_diagnostics)
    assert "secret" not in diagnostics_text
    assert "DUCKDUCKGO_PROXY_URL" not in diagnostics_text
    assert "q=" not in diagnostics_text


def test_duckduckgo_web_search_provider_ignores_environment_proxy_without_config(monkeypatch):
    import src.providers.travel_tools as travel_tools_module

    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9999")
    monkeypatch.delenv("DUCKDUCKGO_PROXY_URL", raising=False)
    monkeypatch.delenv("DUCKDUCKGO_HTTP_PROXY", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROXY_URL", raising=False)
    monkeypatch.delenv("WEB_SEARCH_HTTP_PROXY", raising=False)
    get_settings.cache_clear()
    html = """
    <html><body><a rel="nofollow" class="result-link" href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a></body></html>
    """
    proxy_calls: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return html.encode("utf-8")

    class FakeOpener:
        def open(self, _request, timeout: float):
            assert timeout == get_settings().web_search_provider_timeout_seconds
            return FakeResponse()

    def fake_proxy_handler(proxies):
        proxy_calls.append(proxies)
        return object()

    def fake_build_opener(_proxy_handler):
        return FakeOpener()

    monkeypatch.setattr(travel_tools_module, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(travel_tools_module, "build_opener", fake_build_opener)
    try:
        result = DuckDuckGoWebSearchProvider().search("故宫 预约 门票", count=1)
    finally:
        get_settings.cache_clear()

    assert proxy_calls == [{}]
    assert result.provider_diagnostics[0]["proxyUsed"] is False
    assert result.results[0].provider_name == "duckduckgo-html-search"


def test_baidu_web_search_provider_reports_proxy_host_without_query(monkeypatch):
    monkeypatch.setenv("BAIDU_PROXY_URL", "http://127.0.0.1:10793")
    get_settings.cache_clear()
    html = r"""
    <html>
      <script>
        bds.comm.iaurl=["https:\/\/www.dpm.org.cn\/subject_booking\/index.html"];
      </script>
    </html>
    """

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        assert url.startswith(BAIDU_HTML_SEARCH_URL)
        assert "wd=dpm" in url
        assert headers and "User-Agent" in headers
        return html

    try:
        result = BaiduWebSearchProvider(http_get=fake_http_get).search("dpm", count=1)
    finally:
        get_settings.cache_clear()

    diagnostics = result.provider_diagnostics[0]
    assert diagnostics["providerName"] == "baidu-html-search"
    assert diagnostics["proxyUsed"] is True
    assert diagnostics["proxyHost"] == "127.0.0.1:10793"
    diagnostics_text = str(result.provider_diagnostics)
    assert "wd=" not in diagnostics_text
    assert "http://127.0.0.1:10793" not in diagnostics_text


def test_web_search_provider_error_redacts_query_and_credential_names():
    import src.providers.travel_tools as travel_tools_module

    message = travel_tools_module._safe_provider_error(
        RuntimeError(
            "failed https://search.example.test/html/?q=故宫&key=real-key&access_token=secret-token "
            "Authorization: Bearer bearer-secret"
        )
    )

    assert "<query-redacted>" in message
    assert "q=" not in message
    assert "key=" not in message.lower()
    assert "access_token=" not in message.lower()
    assert "real-key" not in message
    assert "secret-token" not in message
    assert "bearer-secret" not in message


def test_baidu_web_search_provider_extracts_public_urls_without_api_key():
    html = r"""
    <html>
      <script>
        bds.comm.iaurl=["https:\/\/intl.dpm.org.cn\/visit.html?l=tc","https:\/\/www.dpm.org.cn\/subject_booking\/index.html"];
      </script>
      <a href="https://www.baidu.com/link?url=internal">百度跳转</a>
      <link href="https://pss.bdstatic.com/static/result.css" />
    </html>
    """

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        assert url.startswith(BAIDU_HTML_SEARCH_URL)
        assert "wd=dpm" in url
        assert headers and "User-Agent" in headers
        return html

    result = BaiduWebSearchProvider(http_get=fake_http_get).search("dpm", count=2)

    assert result.provider_name == "baidu-html-search"
    assert result.fallback_used is False
    assert result.failure_reason is None
    assert [item.url for item in result.results] == [
        "https://intl.dpm.org.cn/visit.html?l=tc",
        "https://www.dpm.org.cn/subject_booking/index.html",
    ]
    assert all(item.provider_name == "baidu-html-search" for item in result.results)
    assert all(item.credibility_rank == "official" for item in result.results)


def test_baidu_web_search_provider_extracts_dom_title_mu_and_redirect_url():
    html = """
    <html>
      <body>
        <div class="result c-container" mu="https://www.pku.edu.cn/notice/2026">
          <h3><a href="https://www.baidu.com/link?url=token">北京大学 2026 国庆预约官方公告</a></h3>
          <div class="c-abstract">国庆期间校园参观需提前预约。</div>
        </div>
        <div class="result">
          <h3><a href="/link?url=opaque-token">百度跳转保留</a></h3>
          <div>无法解析真实目标时保留百度跳转。</div>
        </div>
      </body>
    </html>
    """

    def fake_http_get(url: str, _timeout: float, headers: Optional[dict] = None) -> str:
        assert url.startswith(BAIDU_HTML_SEARCH_URL)
        assert "ie=utf-8" in url
        assert "tn=baiduhome_pg" in url
        assert headers and "User-Agent" in headers
        return html

    result = BaiduWebSearchProvider(http_get=fake_http_get).search("北京大学 国庆 预约 官方公告", count=2)

    assert result.results[0].url == "https://www.pku.edu.cn/notice/2026"
    assert result.results[0].title == "北京大学 2026 国庆预约官方公告"
    assert "国庆期间校园参观需提前预约" in result.results[0].snippet
    assert all(not item.url.startswith("https://www.baidu.com/link?url=") for item in result.results)


def test_baidu_web_search_provider_marks_blocked_html():
    def fake_http_get(_url: str, _timeout: float, _headers: Optional[dict] = None) -> str:
        return "<html><title>百度安全验证</title><body>请输入验证码</body></html>"

    provider = BaiduWebSearchProvider(http_get=fake_http_get)

    try:
        provider.search("北京大学 国庆 预约 官方公告", count=2)
    except ToolProviderError as error:
        assert "blocked_or_captcha" in str(error)
    else:
        raise AssertionError("blocked Baidu HTML should raise a classified provider error")


def test_bing_provider_returns_html_results_without_mock_data():
    html = """
    <html><body>
      <li class="b_algo">
        <h2><a href="https://www.dpm.org.cn/visit">故宫博物院官方预约</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">故宫博物院实行实名预约购票。</p></div>
      </li>
    </body></html>
    """

    def fake_http_get(_url: str, _timeout: float, _headers: Optional[dict] = None) -> str:
        return html

    result = ResilientWebSearchProvider(
        default_provider=WebSearchProvider(provider_name="bing", http_get=fake_http_get)
    ).search("故宫 门票")

    assert result.provider_name == "bing-html-search"
    assert result.fallback_used is False
    assert result.results[0].url == "https://www.dpm.org.cn/visit"
    assert result.failure_reason is None


class FakeSearchProvider:
    def __init__(self, provider_name: str, results: Optional[list[dict]] = None, error: Optional[Exception] = None):
        self.provider_name = provider_name
        self.results = results or []
        self.error = error

    def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
        if self.error:
            raise self.error
        from src.providers.travel_tools import WebSearchItem, WebSearchResponse

        return WebSearchResponse(
            query=query,
            results=[
                WebSearchItem(
                    title=item["title"],
                    url=item["url"],
                    snippet=item.get("snippet", ""),
                    source_name=item.get("sourceName", "测试来源"),
                    confidence=item.get("confidence", 0.5),
                    credibility_rank=item.get("credibilityRank", "search"),
                    provider_name=self.provider_name,
                    fallback_used=False,
                )
                for item in self.results
            ],
            confidence=0.7,
            provider_name=self.provider_name,
            fallback_used=False,
        )


class CountingSearchProvider(FakeSearchProvider):
    def __init__(self, provider_name: str, results: Optional[list[dict]] = None, error: Optional[Exception] = None):
        super().__init__(provider_name, results=results, error=error)
        self.call_count = 0

    def search(self, query: str, count: int = 5, freshness: str = "noLimit"):
        self.call_count += 1
        return super().search(query, count=count, freshness=freshness)


def test_resilient_single_provider_uses_injected_truthful_fallback_after_anysearch_timeout():
    primary = CountingSearchProvider("anysearch", error=TimeoutError("AnySearch timed out"))
    fallback = CountingSearchProvider(
        "bing-html-search",
        [
            {
                "title": "北京大学官方参观公告",
                "url": "https://www.pku.edu.cn/visit/",
                "snippet": "北京大学官方参观信息。",
                "confidence": 0.8,
                "credibilityRank": "official",
            }
        ],
    )

    result = ResilientWebSearchProvider(
        default_provider=primary,
        fallback_provider=fallback,
    ).search("北京大学 官方 参观 公告", freshness="oneYear")

    assert primary.call_count == 1
    assert fallback.call_count == 1
    assert result.results[0].provider_name == "bing-html-search"
    assert result.fallback_used is True
    assert result.attempted_providers == ["anysearch", "bing-html-search"]
    assert result.failed_providers == ["anysearch"]
    assert result.successful_providers == ["bing-html-search"]
    assert result.provider_diagnostics[0]["reasonCode"] == "timeout"


def test_resilient_single_mode_wires_configured_real_fallback_chain(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_MODE", "single")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "anysearch")
    monkeypatch.setenv(
        "WEB_SEARCH_PROVIDER_CHAIN",
        "anysearch,ddgs,bing,duckduckgo,baidu,multi-free",
    )
    get_settings.cache_clear()

    try:
        provider = ResilientWebSearchProvider()
        provider_names = [item.provider_name for item in provider.chain_provider.providers]
    finally:
        get_settings.cache_clear()

    assert provider_names[0] == "anysearch"
    assert provider_names.count("anysearch") == 1
    assert "ddgs" in provider_names
    assert "bing-html-search" in provider_names


def test_resilient_single_provider_honors_explicit_total_deadline(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_MODE", "single")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "bing")
    get_settings.cache_clear()

    try:
        provider = ResilientWebSearchProvider(total_deadline_seconds=0.25)

        assert provider.total_deadline_seconds == 0.25
        assert provider.chain_provider is not None
        assert provider.chain_provider.total_deadline_seconds == 0.25
        assert [item.provider_name for item in provider.chain_provider.providers] == ["bing-html-search"]
    finally:
        get_settings.cache_clear()


def test_resilient_fallback_uses_remaining_shared_chain_deadline(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_CHAIN_DEADLINE_SECONDS", "0.06")
    get_settings.cache_clear()
    captured: dict[str, float] = {}

    def slow_timeout(_url, _body, _timeout, _headers=None):
        time.sleep(0.02)
        raise TimeoutError("simulated AnySearch timeout")

    def bing_get(_url, timeout, _headers=None):
        captured["timeout"] = timeout
        return """
        <html><body><li class="b_algo">
          <h2><a href="https://www.pku.edu.cn/visit/">北京大学官方参观公告</a></h2>
          <div class="b_caption"><p>北京大学官方参观信息。</p></div>
        </li></body></html>
        """

    try:
        result = ResilientWebSearchProvider(
            default_provider=AnySearchWebSearchProvider(
                timeout_seconds=0.05,
                proxy_mode="direct",
                http_post=slow_timeout,
            ),
            fallback_provider=BingHTMLSearchProvider(
                timeout_seconds=0.5,
                http_get=bing_get,
            ),
        ).search("北京大学 官方 参观 公告", freshness="oneYear")
    finally:
        get_settings.cache_clear()

    assert result.results
    assert 0 < captured["timeout"] < 0.05
    assert result.provider_diagnostics[1]["timeoutSeconds"] < 0.05


def test_resilient_failure_diagnostics_redact_query_proxy_url_headers_and_credentials():
    query = "PRIVATE USER QUERY"
    secret = "super-secret-token"
    error = RuntimeError(
        "request failed for "
        f"{query} via socks5://proxy-user:proxy-pass@127.0.0.1:1080 "
        "at https://api.anysearch.com/v1/search "
        f"Authorization: Bearer {secret}"
    )

    result = ResilientWebSearchProvider(default_provider=FakeSearchProvider("anysearch", error=error)).search(query)

    exported = f"{result.failure_reason} {result.provider_diagnostics}"
    assert query not in exported
    assert "127.0.0.1" not in exported
    assert "api.anysearch.com" not in exported
    assert "proxy-user" not in exported
    assert "proxy-pass" not in exported
    assert secret not in exported
    assert result.failure_reason == "provider_error"
    assert result.provider_diagnostics[0]["reasonCode"] == "provider_error"


def test_anysearch_classifies_filtered_empty_with_auditable_counts():
    provider = AnySearchWebSearchProvider(
        proxy_mode="direct",
        http_post=lambda *_args, **_kwargs: {
            "code": 0,
            "data": {
                "results": [
                    {
                        "title": "Unrelated official sounding page",
                        "url": "https://www.zhihu.com/question/1",
                        "snippet": "This page does not discuss the requested campus visit.",
                    },
                    {"title": "missing url", "snippet": "ignored"},
                ]
            },
        },
    )

    result = ResilientWebSearchProvider(default_provider=provider).search(
        "北京大学 参观 公告",
        freshness="oneYear",
    )

    diagnostic = result.provider_diagnostics[0]
    assert diagnostic["reasonCode"] == "filtered_empty"
    assert diagnostic["rawResultCount"] == 2
    assert diagnostic["normalizedResultCount"] == 1
    assert diagnostic["acceptedResultCount"] == 0
    assert diagnostic["rejectedResultCount"] == 2
    assert set(diagnostic["rejectionReasons"]) == {
        "missing_required_fields",
        "query_mismatch",
    }


def test_credibility_rank_uses_controlled_host_rules_not_official_keywords():
    import src.providers.travel_tools as travel_tools_module

    assert (
        travel_tools_module._credibility_rank(
            "https://www.zhihu.com/question/1",
            "北京大学官方参观说明",
            "官网预约与官方公告",
        )
        == "search"
    )
    assert (
        travel_tools_module._credibility_rank(
            "https://www.pku.edu.cn/visit/",
            "参观说明",
            "校园开放安排",
        )
        == "official"
    )
    assert (
        travel_tools_module._credibility_rank(
            "https://you.ctrip.com/sight/beijing1.html",
            "景点门票",
            "预约入口",
        )
        == "ota_aggregator"
    )


def test_chain_does_not_stop_on_high_confidence_uncontrolled_search_host():
    ordinary = CountingSearchProvider(
        "anysearch",
        [
            {
                "title": "北京大学参观信息",
                "url": "https://www.zhihu.com/question/1",
                "snippet": "北京大学参观经验与开放信息。",
                "confidence": 0.95,
                "credibilityRank": "search",
            }
        ],
    )
    official = CountingSearchProvider(
        "bing-html-search",
        [
            {
                "title": "北京大学参观公告",
                "url": "https://www.pku.edu.cn/visit/",
                "snippet": "北京大学校园参观公告。",
                "confidence": 0.7,
                "credibilityRank": "official",
            }
        ],
    )

    result = ChainedWebSearchProvider(
        providers=[ordinary, official],
        min_accepted_results=1,
        max_provider_attempts=2,
    ).search("北京大学 参观 信息", freshness="oneYear")

    assert ordinary.call_count == 1
    assert official.call_count == 1
    assert result.successful_providers == ["anysearch", "bing-html-search"]


def test_multi_free_web_search_ranks_confidence_and_filters_stale_results():
    provider = MultiFreeWebSearchProvider(
        providers=[
            FakeSearchProvider(
                "baidu-html-search",
                [
                    {
                        "title": "故宫博物院 2021 年预约规则",
                        "url": "https://old.example.com/dpm-ticket",
                        "snippet": "2021年1月1日发布的旧预约说明。",
                        "confidence": 0.95,
                        "credibilityRank": "official",
                    },
                    {
                        "title": "故宫博物院官方预约",
                        "url": "https://www.dpm.org.cn/visit",
                        "snippet": "2026年6月1日更新，故宫博物院实行实名预约购票。",
                        "confidence": 0.62,
                        "credibilityRank": "official",
                    },
                ],
            ),
            FakeSearchProvider(
                "duckduckgo-html-search",
                [
                    {
                        "title": "故宫门票攻略",
                        "url": "https://travel.example.com/dpm-guide",
                        "snippet": "2026年5月攻略，含开放时间参考。",
                        "confidence": 0.72,
                        "credibilityRank": "guide",
                    }
                ],
            ),
        ]
    )

    result = provider.search("故宫 预约 门票", count=3, freshness="oneYear")

    assert result.provider_name == "multi-free-search"
    assert result.fallback_used is False
    assert [item.url for item in result.results] == [
        "https://www.dpm.org.cn/visit",
        "https://travel.example.com/dpm-guide",
    ]
    assert all("old.example.com" not in item.url for item in result.results)
    assert result.results[0].confidence >= result.results[1].confidence
    assert "多个免费搜索源" in result.user_visible_caveat


def test_multi_free_web_search_keeps_results_when_one_free_provider_fails():
    provider = MultiFreeWebSearchProvider(
        providers=[
            FakeSearchProvider("baidu-html-search", error=RuntimeError("baidu blocked")),
            FakeSearchProvider(
                "duckduckgo-html-search",
                [
                    {
                        "title": "上海博物馆官方预约",
                        "url": "https://www.shanghaimuseum.net/mu/frontend/pg/index",
                        "snippet": "2026年开放预约信息。",
                        "confidence": 0.66,
                        "credibilityRank": "official",
                    }
                ],
            ),
        ]
    )

    result = provider.search("上海博物馆 预约", count=2, freshness="oneYear")

    assert result.provider_name == "multi-free-search"
    assert result.results[0].provider_name == "duckduckgo-html-search"
    assert result.failure_reason is None
    assert "baidu-html-search" in result.user_visible_caveat
    assert "baidu blocked" not in result.user_visible_caveat


def test_amap_weather_provider_returns_structured_weather():
    def fake_http_get(url: str, _timeout: float) -> dict:
        assert "city=110000" in url
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-10-16",
                            "dayweather": "小雨",
                            "nightweather": "阴",
                            "daytemp": "22",
                            "nighttemp": "15",
                            "daywind": "东北",
                            "nightwind": "东北",
                            "daypower": "3",
                            "nightpower": "3",
                        }
                    ],
                }
            ],
        }

    result = AmapWeatherProvider(api_key="test-key", http_get=fake_http_get).query(
        "北京",
        travel_date="2026-10-16",
        purpose_tags=["拍照优先", "老人同行"],
        context={"preferenceSummary": "拍照优先，老人同行"},
    )

    assert result.provider_name == "amap-weather-provider"
    assert result.fallback_used is False
    assert result.city == "北京市"
    assert result.date == "2026-10-16"
    assert result.weather == "小雨转阴"
    assert result.temperature_range == "15-22°C"
    assert result.risk_level == "risky"
    assert "拍照" in result.risk_reason


def test_amap_weather_provider_failure_returns_error_metadata_without_mock_weather():
    def failing_http_get(_url: str, _timeout: float) -> dict:
        raise RuntimeError("timeout")

    result = ResilientAmapWeatherProvider(
        default_provider=AmapWeatherProvider(api_key="test-key", http_get=failing_http_get)
    ).query("深圳", travel_date="2026-10-16", purpose_tags=["户外"], context={})

    assert result.provider_name == "amap-weather-provider"
    assert result.fallback_used is False
    assert "timeout" in (result.failure_reason or "")
    assert "未使用 mock" in result.user_visible_caveat
    assert result.weather == "待确认"
    assert result.confidence == 0.0

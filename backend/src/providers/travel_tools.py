import json
import copy
import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
import weakref
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from threading import Semaphore
from typing import Callable, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener, getproxies, urlopen
from uuid import uuid4

from src.core.config import get_settings
from src.services.amap_rate_limiter import AMAP_BASIC_WEB_SERVICE_QPS, AMAP_WEB_SERVICE_RATE_LIMITER


BOCHA_WEB_SEARCH_URL = "https://api.bochaai.com/v1/web-search"
TAVILY_WEB_SEARCH_URL = "https://api.tavily.com/search"
ANYSEARCH_WEB_SEARCH_URL = "https://api.anysearch.com/v1/search"
BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
GOOGLE_CSE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
BING_HTML_SEARCH_URL = "https://www.bing.com/search"
DUCKDUCKGO_HTML_SEARCH_URL = "https://duckduckgo.com/html/"
DUCKDUCKGO_LITE_SEARCH_URL = "https://lite.duckduckgo.com/lite/"
CHEETAH_DUCKDUCKGO_HTML_SEARCH_URL = "https://html.duckduckgo.com/html/"
BAIDU_HTML_SEARCH_URL = "https://www.baidu.com/s"
AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
AMAP_BASIC_WEATHER_PARALLELISM = AMAP_BASIC_WEB_SERVICE_QPS
AMAP_BASIC_WEATHER_SEMAPHORE = Semaphore(AMAP_BASIC_WEATHER_PARALLELISM)
MAX_SEARCH_RESULT_AGE_DAYS = 730
CITY_ADCODE = {
    "北京": "110000",
    "北京市": "110000",
    "上海": "310000",
    "上海市": "310000",
    "广州": "440100",
    "广州市": "440100",
    "深圳": "440300",
    "深圳市": "440300",
}
BAD_WEATHER_KEYWORDS = {"雨", "雪", "雷", "大风", "沙尘", "冰雹", "雾", "霾", "暴"}
WEATHER_PURPOSE_TAGS = {"拍照优先", "拍照", "打卡", "徒步", "亲子", "夜景", "户外", "排队", "老人", "步行"}
STABLE_WEB_SEARCH_PROVIDERS = {
    "bocha-web-search",
    "tavily",
    "anysearch",
    "brave-web-search",
    "searxng",
    "ddgs",
    "google-cse",
    "bing-html-search",
    "cheetah-duckduckgo-html-search",
}
WEB_SEARCH_CIRCUIT_BREAKER_SECONDS = 60
_WEB_SEARCH_PROVIDER_INSTANCES: "weakref.WeakSet[ChainedWebSearchProvider]" = weakref.WeakSet()
_WEB_SEARCH_CHAIN_DEADLINE: ContextVar[Optional[float]] = ContextVar(
    "web_search_chain_deadline",
    default=None,
)


def _remaining_web_search_deadline_seconds() -> Optional[float]:
    deadline = _WEB_SEARCH_CHAIN_DEADLINE.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.perf_counter())


def _effective_web_search_timeout(configured_seconds: float) -> float:
    configured = max(0.001, float(configured_seconds or 0.001))
    remaining = _remaining_web_search_deadline_seconds()
    if remaining is None:
        return configured
    if remaining <= 0:
        raise TimeoutError("web_search_chain_deadline_exhausted")
    return min(configured, remaining)


def _system_proxy_configured() -> bool:
    return any(
        str(scheme or "").strip().lower() in {"http", "https", "all"} and bool(str(value or "").strip())
        for scheme, value in getproxies().items()
    )


def _safe_web_provider_diagnostics(
    diagnostics: list[dict],
    *,
    limit: int = 8,
) -> list[dict[str, object]]:
    queue = [item for item in diagnostics if isinstance(item, dict)]
    result: list[dict[str, object]] = []
    allowed_statuses = {"success", "failed", "skipped", "no_results", "cache_hit"}
    while queue and len(result) < max(1, limit):
        raw = queue.pop(0)
        provider_name = _provider_result_name(str(raw.get("providerName") or "web"))
        status = str(raw.get("status") or "").strip().lower()
        if status not in allowed_statuses:
            status = "failed"
        raw_reason = str(raw.get("reasonCode") or raw.get("reason") or status).strip().lower()
        reason_code = re.sub(r"[^a-z0-9_\-]", "_", raw_reason)[:64] or status
        row: dict[str, object] = {
            "providerName": provider_name,
            "status": status,
            "reason": reason_code,
            "reasonCode": reason_code,
        }
        for key in (
            "durationMs",
            "resultCount",
            "rawResultCount",
            "normalizedResultCount",
            "acceptedResultCount",
            "acceptedSourceCount",
            "rejectedResultCount",
        ):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row[key] = min(value, 1_000_000)
        rejection_reasons = raw.get("rejectionReasons")
        if isinstance(rejection_reasons, list):
            row["rejectionReasons"] = list(
                dict.fromkeys(
                    re.sub(r"[^a-z0-9_\-]", "_", str(value).strip().lower())[:64]
                    for value in rejection_reasons[:12]
                    if str(value).strip()
                )
            )
        timeout_seconds = raw.get("timeoutSeconds")
        if (
            isinstance(timeout_seconds, (int, float))
            and not isinstance(timeout_seconds, bool)
            and 0 <= timeout_seconds <= 60
        ):
            row["timeoutSeconds"] = round(float(timeout_seconds), 6)
        transport_route = str(raw.get("transportRoute") or "").strip().lower()
        if transport_route in {"system", "direct"}:
            row["transportRoute"] = transport_route
        proxy_configured = raw.get("proxyConfigured")
        if isinstance(proxy_configured, bool):
            row["proxyConfigured"] = proxy_configured
        result.append(row)
        nested = raw.get("providerDiagnostics")
        if isinstance(nested, list):
            queue.extend(item for item in nested if isinstance(item, dict))
    return result


class ToolProviderError(RuntimeError):
    pass


class WebSearchAttemptError(ToolProviderError):
    def __init__(self, reason_code: str, diagnostics: Optional[dict[str, object]] = None):
        self.reason_code = reason_code
        self.diagnostics = diagnostics or {}
        super().__init__(reason_code)


class ProviderConfigMissingError(ToolProviderError):
    pass


@dataclass
class WebSearchItem:
    title: str
    url: str
    snippet: str
    source_name: str
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = 0.0
    credibility_rank: str = "unknown"
    provider_name: str = "unknown"
    fallback_used: bool = False
    failure_reason: Optional[str] = None
    user_visible_caveat: str = ""
    summary: str = ""
    published_at: Optional[str] = None


@dataclass
class WebSearchResponse:
    query: str
    results: list[WebSearchItem]
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = 0.0
    provider_name: str = "duckduckgo-html-search"
    fallback_used: bool = False
    failure_reason: Optional[str] = None
    user_visible_caveat: str = ""
    provider_diagnostics: list[dict] = field(default_factory=list)
    attempted_providers: list[str] = field(default_factory=list)
    successful_providers: list[str] = field(default_factory=list)
    failed_providers: list[str] = field(default_factory=list)
    skipped_providers: list[str] = field(default_factory=list)


@dataclass
class AmapWeatherResponse:
    city: str
    date: str
    weather: str
    temperature_range: str
    wind: str
    humidity: Optional[str] = None
    risk_level: str = "neutral"
    risk_reason: str = "天气对本次行程影响较低。"
    source_name: str = "高德天气"
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = 0.0
    provider_name: str = "amap-weather-provider"
    fallback_used: bool = False
    failure_reason: Optional[str] = None
    user_visible_caveat: str = ""
    raw: dict = field(default_factory=dict)


class BochaWebSearchProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        provider_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], dict]] = None,
        http_post: Optional[Callable[[str, dict, float, Optional[dict]], dict]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.bocha_api_key
        self.provider_name = provider_name or "bocha"
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.provider_timeout_seconds
        self.http_get = http_get
        self.http_post = http_post

    def missing_config_reason(self) -> str:
        return "" if self.api_key else "BOCHA_API_KEY/WEB_SEARCH_API_KEY/SEARCH_PROVIDER_KEY is not configured."

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        if not self.api_key:
            raise ProviderConfigMissingError("BOCHA_API_KEY/WEB_SEARCH_API_KEY/SEARCH_PROVIDER_KEY is not configured.")
        if self.provider_name.lower() not in {"bocha", "bocha-web-search", "bochaai"}:
            raise ToolProviderError(
                f"Unsupported WEB_SEARCH_PROVIDER: {self.provider_name}. Configure WEB_SEARCH_PROVIDER=bocha."
            )
        safe_count = max(1, min(int(count or 5), 50))
        safe_freshness = (
            freshness if freshness in {"noLimit", "oneYear", "oneMonth", "oneWeek", "oneDay"} else "noLimit"
        )
        payload = self._post(
            BOCHA_WEB_SEARCH_URL,
            {
                "query": query,
                "summary": True,
                "freshness": safe_freshness,
                "count": safe_count,
            },
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        code = payload.get("code")
        if code not in (None, 200, "200"):
            reason = _bocha_failure_reason_from_payload(payload)
            message = str(payload.get("msg") or payload.get("message") or f"Bocha web search returned code {code}")
            raise ToolProviderError(f"{reason}: {message}")
        values = ((payload.get("webPages") or {}).get("value") or [])[:safe_count]
        results = [
            WebSearchItem(
                title=str(item.get("name") or item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("summary") or item.get("snippet") or "").strip(),
                source_name=str(item.get("siteName") or "").strip() or _source_name(str(item.get("url") or "")),
                confidence=_web_search_item_confidence(item, index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("url") or ""),
                    str(item.get("name") or item.get("title") or ""),
                    str(item.get("summary") or item.get("snippet") or ""),
                ),
                provider_name="bocha-web-search",
                fallback_used=False,
                user_visible_caveat="联网搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
                summary=str(item.get("summary") or "").strip(),
                published_at=str(item.get("datePublished") or item.get("date") or "").strip() or None,
            )
            for index, item in enumerate(values, start=1)
            if str(item.get("name") or item.get("title") or "").strip() and str(item.get("url") or "").strip()
        ]
        results = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not results:
            raise ToolProviderError("Bocha web search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=results,
            confidence=min(0.92, 0.54 + 0.08 * len(results)),
            provider_name="bocha-web-search",
            fallback_used=False,
            user_visible_caveat="联网搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            provider_diagnostics=[
                {
                    "providerName": "bocha-web-search",
                    "status": "success",
                    "requestOptions": {
                        "summary": True,
                        "freshness": safe_freshness,
                        "count": safe_count,
                    },
                    "resultCount": len(results),
                }
            ],
        )

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_post is not None:
            return self.http_post(url, body, timeout_seconds, headers)
        if self.http_get is not None:
            # Test compatibility for older call sites that injected a generic request fake.
            query_url = (
                f"{url}?{urlencode({'query': str(body.get('query') or ''), 'count': str(body.get('count') or '')})}"
            )
            return self.http_get(query_url, timeout_seconds, headers)
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("invalid_response: Bocha search returned invalid JSON.") from error


class TavilyWebSearchProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_post: Optional[Callable[[str, dict, float, Optional[dict]], dict]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.tavily_api_key
        self.provider_name = "tavily"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_post = http_post

    def missing_config_reason(self) -> str:
        return "" if self.api_key else "TAVILY_API_KEY is not configured."

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        if not self.api_key:
            raise ProviderConfigMissingError("TAVILY_API_KEY is not configured.")
        safe_count = max(1, min(int(count or 5), 20))
        payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": safe_count,
            "topic": "general",
            "country": "china",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        time_range = _tavily_time_range(freshness)
        if time_range:
            payload["time_range"] = time_range
        body = self._post(
            TAVILY_WEB_SEARCH_URL,
            payload,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        values = body.get("results") or []
        results = [
            WebSearchItem(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("content") or item.get("snippet") or "").strip(),
                source_name=_source_name(str(item.get("url") or "")),
                confidence=_score_confidence(item.get("score"), index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("url") or ""),
                    str(item.get("title") or ""),
                    str(item.get("content") or item.get("snippet") or ""),
                ),
                provider_name="tavily",
                fallback_used=False,
                user_visible_caveat="Tavily 联网搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(values[:safe_count], start=1)
            if str(item.get("title") or "").strip() and str(item.get("url") or "").strip()
        ]
        ranked = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not ranked:
            raise ToolProviderError("Tavily search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.93, max(item.confidence for item in ranked)),
            provider_name="tavily",
            fallback_used=False,
            user_visible_caveat="Tavily 联网搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
        )

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_post is not None:
            return self.http_post(url, body, timeout_seconds, headers)
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"{_http_failure_reason(error.code)}: HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("Tavily search returned invalid JSON.") from error


class AnySearchWebSearchProvider:
    """Official AnySearch REST provider; anonymous access is intentionally supported."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        proxy_mode: Optional[str] = None,
        http_post: Optional[Callable[[str, dict, float, Optional[dict]], dict]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.anysearch_api_key
        self.domain = settings.anysearch_domain.strip()
        self.tag = settings.anysearch_tag.strip()
        self.zone = settings.anysearch_zone.strip()
        self.language = settings.anysearch_language.strip()
        self.provider_name = "anysearch"
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.anysearch_timeout_seconds
        configured_proxy_mode = (
            str(proxy_mode if proxy_mode is not None else settings.anysearch_proxy_mode).strip().lower()
        )
        self.proxy_mode = configured_proxy_mode if configured_proxy_mode in {"auto", "system", "direct"} else "auto"
        self.http_post = http_post

    def missing_config_reason(self) -> str:
        # AnySearch has an anonymous free tier. A key only raises quota and concurrency limits.
        return ""

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        transport_routes = ("direct", "system") if self.proxy_mode == "auto" else (self.proxy_mode,)
        transport_diagnostics: list[dict[str, object]] = []
        for route_index, transport_route in enumerate(transport_routes):
            started = time.perf_counter()
            request_timeout_seconds = 0.0
            try:
                request_timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
                response = self._search_once(
                    query,
                    count=count,
                    freshness=freshness,
                    transport_route=transport_route,
                    request_timeout_seconds=request_timeout_seconds,
                )
            except Exception as error:
                reason_code = _provider_failure_reason(error)
                error_diagnostics = dict(error.diagnostics) if isinstance(error, WebSearchAttemptError) else {}
                transport_diagnostics.append(
                    {
                        "providerName": "anysearch",
                        "status": "failed",
                        "reasonCode": reason_code,
                        "resultCount": 0,
                        "transportRoute": transport_route,
                        "proxyConfigured": bool(transport_route == "system" and _system_proxy_configured()),
                        "timeoutSeconds": round(request_timeout_seconds, 6),
                        "durationMs": int((time.perf_counter() - started) * 1000),
                        **error_diagnostics,
                    }
                )
                remaining_seconds = _remaining_web_search_deadline_seconds()
                may_switch_transport = bool(
                    self.proxy_mode == "auto"
                    and transport_route == "direct"
                    and reason_code in {"timeout", "transport_error"}
                    and route_index + 1 < len(transport_routes)
                    and (remaining_seconds is None or remaining_seconds > 0)
                )
                if may_switch_transport:
                    continue
                raise WebSearchAttemptError(
                    reason_code,
                    {
                        **error_diagnostics,
                        "providerDiagnostics": transport_diagnostics,
                    },
                ) from error
            response.provider_diagnostics = [
                *transport_diagnostics,
                *response.provider_diagnostics,
            ]
            response.fallback_used = bool(response.fallback_used or route_index > 0)
            return response
        raise WebSearchAttemptError(
            "transport_error",
            {"providerDiagnostics": transport_diagnostics},
        )

    def _search_once(
        self,
        query: str,
        *,
        count: int,
        freshness: str,
        transport_route: str,
        request_timeout_seconds: float,
    ) -> WebSearchResponse:
        started = time.perf_counter()
        safe_count = max(1, min(int(count or 5), 100))
        payload: dict[str, object] = {"query": query, "max_results": safe_count}
        if self.domain:
            payload["domain"] = self.domain
        if self.tag:
            payload["tag"] = self.tag
        if self.zone:
            payload["zone"] = self.zone
        if self.language:
            payload["language"] = self.language
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = self._post(
            ANYSEARCH_WEB_SEARCH_URL,
            payload,
            headers,
            timeout_seconds=request_timeout_seconds,
            transport_route=transport_route,
        )
        code = body.get("code", 0)
        if code not in {0, "0", None}:
            raise WebSearchAttemptError("http_error")
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise WebSearchAttemptError("invalid_response")
        values = data.get("results") or []
        if not isinstance(values, list):
            raise WebSearchAttemptError("invalid_response")
        raw_result_count = len(values)
        results = [
            WebSearchItem(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("snippet") or item.get("content") or "").strip(),
                source_name=_source_name(str(item.get("url") or "")),
                confidence=_web_search_item_confidence(item, index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("url") or ""),
                    str(item.get("title") or ""),
                    str(item.get("snippet") or item.get("content") or ""),
                ),
                provider_name="anysearch",
                fallback_used=False,
                user_visible_caveat="AnySearch 联网搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
                summary=str(item.get("content") or item.get("snippet") or "").strip(),
                published_at=str(item.get("published_at") or item.get("publishedAt") or item.get("date") or "").strip()
                or None,
            )
            for index, item in enumerate(values[:safe_count], start=1)
            if isinstance(item, dict) and str(item.get("title") or "").strip() and str(item.get("url") or "").strip()
        ]
        normalized_result_count = len(results)
        rejection_reasons = [
            "missing_required_fields"
            for item in values[:safe_count]
            if not isinstance(item, dict)
            or not str(item.get("title") or "").strip()
            or not str(item.get("url") or "").strip()
        ]
        rejection_reasons.extend(
            reason for item in results for reason in [_search_result_rejection_reason(query, item, freshness)] if reason
        )
        ranked = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        accepted_result_count = len(ranked)
        unclassified_rejection_count = max(
            0,
            raw_result_count - accepted_result_count - len(rejection_reasons),
        )
        rejection_reasons.extend(["deduplicated"] * unclassified_rejection_count)
        attempt_diagnostics = {
            "rawResultCount": raw_result_count,
            "normalizedResultCount": normalized_result_count,
            "acceptedResultCount": accepted_result_count,
            "acceptedSourceCount": len({urlparse(item.url).netloc.casefold() for item in ranked if item.url}),
            "rejectedResultCount": max(0, raw_result_count - accepted_result_count),
            "rejectionReasons": list(dict.fromkeys(rejection_reasons)),
        }
        if raw_result_count == 0:
            raise WebSearchAttemptError("raw_empty", attempt_diagnostics)
        if not ranked:
            raise WebSearchAttemptError("filtered_empty", attempt_diagnostics)
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.94, max(item.confidence for item in ranked)),
            provider_name="anysearch",
            fallback_used=False,
            user_visible_caveat="AnySearch 联网搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            provider_diagnostics=[
                {
                    "providerName": "anysearch",
                    "status": "success",
                    "endpointHost": urlparse(ANYSEARCH_WEB_SEARCH_URL).netloc,
                    "authentication": "api_key" if self.api_key else "anonymous",
                    "resultCount": len(ranked),
                    "transportRoute": transport_route,
                    "proxyConfigured": bool(transport_route == "system" and _system_proxy_configured()),
                    "timeoutSeconds": round(request_timeout_seconds, 6),
                    "durationMs": int((time.perf_counter() - started) * 1000),
                    "reasonCode": "ok",
                    **attempt_diagnostics,
                    **({"requestId": str(metadata.get("request_id"))} if metadata.get("request_id") else {}),
                }
            ],
        )

    def _post(
        self,
        url: str,
        body: dict,
        headers: dict,
        *,
        timeout_seconds: Optional[float] = None,
        transport_route: Optional[str] = None,
    ) -> dict:
        timeout_seconds = (
            _effective_web_search_timeout(self.timeout_seconds) if timeout_seconds is None else timeout_seconds
        )
        if self.http_post is not None:
            return self.http_post(url, body, timeout_seconds, headers)
        request = Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
        )
        route = str(transport_route or self.proxy_mode).strip().lower()
        open_request = build_opener(ProxyHandler({})).open if route == "direct" else urlopen
        try:
            with open_request(request, timeout=timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"{_http_failure_reason(error.code)}: HTTP {error.code}") from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ToolProviderError("请求超时") from error
            raise ToolProviderError("transport_error") from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error
        except OSError as error:
            raise ToolProviderError("transport_error") from error
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as error:
            raise WebSearchAttemptError("invalid_response") from error
        if not isinstance(payload, dict):
            raise WebSearchAttemptError("invalid_response")
        return payload


class BraveWebSearchProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.brave_search_api_key
        self.provider_name = "brave-web-search"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get

    def missing_config_reason(self) -> str:
        return "" if self.api_key else "BRAVE_SEARCH_API_KEY is not configured."

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        if not self.api_key:
            raise ProviderConfigMissingError("BRAVE_SEARCH_API_KEY is not configured.")
        safe_count = max(1, min(int(count or 5), 20))
        params = {
            "q": query,
            "count": str(safe_count),
            "country": "CN",
            "search_lang": "zh",
            "ui_lang": "zh-CN",
            "extra_snippets": "true",
        }
        freshness_code = _brave_freshness(freshness)
        if freshness_code:
            params["freshness"] = freshness_code
        payload = self._get_json(
            f"{BRAVE_WEB_SEARCH_URL}?{urlencode(params)}",
            {
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
            },
        )
        values = ((payload.get("web") or {}).get("results") or [])[:safe_count]
        results = [
            WebSearchItem(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("description") or item.get("snippet") or "").strip(),
                source_name=_source_name(str(item.get("url") or "")),
                confidence=_web_search_item_confidence(item, index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("url") or ""),
                    str(item.get("title") or ""),
                    str(item.get("description") or item.get("snippet") or ""),
                ),
                provider_name="brave-web-search",
                fallback_used=False,
                user_visible_caveat="Brave Search 结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(values, start=1)
            if str(item.get("title") or "").strip() and str(item.get("url") or "").strip()
        ]
        ranked = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not ranked:
            raise ToolProviderError("Brave search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.91, max(item.confidence for item in ranked)),
            provider_name="brave-web-search",
            fallback_used=False,
            user_visible_caveat="Brave Search 结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            provider_diagnostics=[
                {
                    "providerName": "brave-web-search",
                    "status": "success",
                    "requestOptions": {
                        "country": "CN",
                        "searchLang": "zh",
                        "uiLang": "zh-CN",
                        "extraSnippets": True,
                    },
                    "resultCount": len(ranked),
                }
            ],
        )

    def _get_json(self, url: str, headers: dict) -> dict:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            return _json_payload_from_adapter(payload, "Brave search test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"{_http_failure_reason(error.code)}: HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("invalid_response: Brave search returned invalid JSON.") from error


class SearXNGWebSearchProvider:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.searxng_base_url).rstrip("/")
        self.provider_name = "searxng"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get

    def missing_config_reason(self) -> str:
        return "" if self.base_url else "SEARXNG_BASE_URL is not configured."

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        if not self.base_url:
            raise ProviderConfigMissingError("SEARXNG_BASE_URL is not configured.")
        safe_count = max(1, min(int(count or 5), 20))
        params = {
            "q": query,
            "format": "json",
            "language": "zh-CN",
            "categories": "general",
            "safesearch": "1",
        }
        time_range = _searxng_time_range(freshness)
        if time_range:
            params["time_range"] = time_range
        try:
            payload = self._get_json(f"{self.base_url}/search?{urlencode(params)}", {"Accept": "application/json"})
        except ToolProviderError as error:
            if "HTTP 403" in str(error):
                raise ToolProviderError("searxng_json_format_disabled") from error
            raise
        values = (payload.get("results") or [])[:safe_count]
        results = [
            WebSearchItem(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("content") or item.get("snippet") or "").strip(),
                source_name=str(item.get("engine") or "").strip() or _source_name(str(item.get("url") or "")),
                confidence=_web_search_item_confidence(item, index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("url") or ""),
                    str(item.get("title") or ""),
                    str(item.get("content") or item.get("snippet") or ""),
                ),
                provider_name="searxng",
                fallback_used=False,
                user_visible_caveat="SearXNG 自托管搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(values, start=1)
            if str(item.get("title") or "").strip() and str(item.get("url") or "").strip()
        ]
        ranked = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not ranked:
            raise ToolProviderError("SearXNG search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.9, max(item.confidence for item in ranked)),
            provider_name="searxng",
            fallback_used=False,
            user_visible_caveat="SearXNG 自托管搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            provider_diagnostics=[
                {
                    "providerName": "searxng",
                    "status": "success",
                    "endpointHost": urlparse(self.base_url).netloc,
                    "resultCount": len(ranked),
                }
            ],
        )

    def _get_json(self, url: str, headers: dict) -> dict:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            return _json_payload_from_adapter(payload, "SearXNG search test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(
                "ddgs_api_unreachable: DDGS local API is not reachable. "
                "Start it with: ddgs api --host 127.0.0.1 --port 4479; "
                "with proxy: ddgs api --host 127.0.0.1 --port 4479 -pr http://127.0.0.1:10793. "
                f"reason={error.reason}"
            ) from error
        except TimeoutError as error:
            raise ToolProviderError(
                "ddgs_api_unreachable: DDGS local API timed out. Start it with: ddgs api --host 127.0.0.1 --port 4479."
            ) from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("SearXNG search returned invalid JSON.") from error


class DDGSWebSearchProvider:
    def __init__(
        self,
        api_base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.api_base_url = (api_base_url if api_base_url is not None else settings.ddgs_api_base_url).rstrip("/")
        self.provider_name = "ddgs"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get

    def missing_config_reason(self) -> str:
        if self.api_base_url or _ddgs_package_available() or _ddgs_cli_path():
            return ""
        return "DDGS_API_BASE_URL is not configured and neither ddgs package nor ddgs CLI is installed."

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 20))
        api_failure_diagnostics: list[dict] = []
        if self.api_base_url:
            try:
                values, diagnostics = self._search_api(query, safe_count, freshness)
            except ToolProviderError as error:
                if not (_ddgs_package_available() or _ddgs_cli_path()):
                    raise
                api_failure_diagnostics = [
                    {
                        "providerName": "ddgs",
                        "status": "failed",
                        "reason": _provider_failure_reason(error),
                        "message": _safe_provider_error(error),
                        "apiBaseConfigured": True,
                        "resultCount": 0,
                    }
                ]
                values, diagnostics = self._search_package_or_cli(query, safe_count, freshness)
        else:
            values, diagnostics = self._search_package_or_cli(query, safe_count, freshness)
        results = self._items_from_values(query, values, safe_count, freshness)
        if not results:
            raise ToolProviderError("DDGS search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=results,
            confidence=min(0.86, 0.5 + 0.07 * len(results)),
            provider_name="ddgs",
            fallback_used=False,
            user_visible_caveat="DDGS 免费搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            provider_diagnostics=api_failure_diagnostics + diagnostics,
        )

    def _search_api(self, query: str, count: int, freshness: str) -> tuple[list[dict], list[dict]]:
        params = {
            "q": query,
            "query": query,
            "max_results": str(count),
            "count": str(count),
            "region": "cn-zh",
            "safesearch": "moderate",
        }
        freshness_code = _duckduckgo_freshness(freshness)
        if freshness_code:
            params["timelimit"] = freshness_code
        try:
            payload = self._get_json(
                f"{self.api_base_url}/search/text?{urlencode(params)}", {"Accept": "application/json"}
            )
            endpoint_path = "/search/text"
        except ToolProviderError:
            payload = self._get_json(f"{self.api_base_url}/search?{urlencode(params)}", {"Accept": "application/json"})
            endpoint_path = "/search"
        values = self._extract_values(payload)
        return values, [
            {
                "providerName": "ddgs",
                "status": "success",
                "endpointHost": urlparse(self.api_base_url).netloc,
                "endpointPath": endpoint_path,
                "apiBaseConfigured": True,
                "resultCount": len(values),
            }
        ]

    def _search_package_or_cli(self, query: str, count: int, freshness: str) -> tuple[list[dict], list[dict]]:
        try:
            return self._search_package(query, count, freshness)
        except Exception as error:
            cli_path = _ddgs_cli_path()
            if not cli_path:
                if str(error) == "ddgs_package_timeout_unsupported":
                    raise error
                raise ProviderConfigMissingError(
                    "DDGS_API_BASE_URL is not configured and neither ddgs package nor ddgs CLI is installed."
                ) from error
            return self._search_cli(cli_path, query, count, freshness)

    def _search_package(self, query: str, count: int, freshness: str) -> tuple[list[dict], list[dict]]:
        from ddgs import DDGS  # type: ignore

        kwargs = {
            "max_results": count,
            "region": "cn-zh",
            "safesearch": "moderate",
        }
        freshness_code = _duckduckgo_freshness(freshness)
        if freshness_code:
            kwargs["timelimit"] = freshness_code
        try:
            client = DDGS(timeout=_effective_web_search_timeout(self.timeout_seconds))
        except TypeError as error:
            raise ToolProviderError("ddgs_package_timeout_unsupported") from error
        try:
            values = list(client.text(query, **kwargs))
        except TypeError:
            kwargs.pop("region", None)
            values = list(client.text(query, **kwargs))
        return values, [
            {
                "providerName": "ddgs",
                "status": "success",
                "apiBaseConfigured": False,
                "packageAvailable": True,
                "resultCount": len(values),
            }
        ]

    def _search_cli(self, cli_path: str, query: str, count: int, freshness: str) -> tuple[list[dict], list[dict]]:
        freshness_code = _duckduckgo_freshness(freshness)
        last_error: Optional[Exception] = None
        values: list[dict] = []
        selected_backend = ""
        for attempt, backend in enumerate(("yahoo", "bing", "auto"), start=1):
            with tempfile.TemporaryDirectory(prefix="trip-ddgs-") as tmpdir:
                output_path = Path(tmpdir) / "results.json"
                args = [
                    cli_path,
                    "text",
                    "-q",
                    query,
                    "-m",
                    str(count),
                    "-o",
                    str(output_path),
                    "--no-color",
                ]
                if freshness_code:
                    args.extend(["-t", freshness_code])
                args.extend(["-b", backend])
                try:
                    timeout_seconds = _effective_web_search_timeout(max(float(self.timeout_seconds or 0), 20.0))
                    completed = subprocess.run(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    last_error = ToolProviderError("DDGS CLI search timed out.")
                    continue
                if completed.returncode != 0:
                    message = (completed.stderr or completed.stdout or "DDGS CLI search failed.").strip()
                    last_error = ToolProviderError(message)
                    continue
                raw_payload = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
                try:
                    payload = self._load_cli_payload(raw_payload, completed.stdout)
                except ToolProviderError as error:
                    last_error = error
                    continue
                values = self._extract_values(payload)
                if not values:
                    last_error = ToolProviderError("DDGS CLI search returned no usable results.")
                    continue
                selected_backend = backend
                break
        else:
            if isinstance(last_error, ToolProviderError):
                raise last_error
            raise ToolProviderError("DDGS CLI search failed.")
        return values, [
            {
                "providerName": "ddgs",
                "status": "success",
                "apiBaseConfigured": False,
                "packageAvailable": False,
                "cliAvailable": True,
                "backend": selected_backend,
                "resultCount": len(values),
                "attemptCount": attempt,
            }
        ]

    def _items_from_values(self, query: str, values: list[dict], count: int, freshness: str) -> list[WebSearchItem]:
        results = [
            WebSearchItem(
                title=str(item.get("title") or item.get("name") or "").strip(),
                url=str(item.get("href") or item.get("url") or item.get("link") or "").strip(),
                snippet=str(item.get("body") or item.get("snippet") or item.get("content") or "").strip(),
                source_name=_source_name(str(item.get("href") or item.get("url") or item.get("link") or "")),
                confidence=_web_search_item_confidence(item, index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("href") or item.get("url") or item.get("link") or ""),
                    str(item.get("title") or item.get("name") or ""),
                    str(item.get("body") or item.get("snippet") or item.get("content") or ""),
                ),
                provider_name="ddgs",
                fallback_used=False,
                user_visible_caveat="DDGS 免费搜索结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(values[:count], start=1)
            if str(item.get("title") or item.get("name") or "").strip()
            and str(item.get("href") or item.get("url") or item.get("link") or "").strip()
        ]
        return _validated_ranked_search_results(query, results, freshness)[:count]

    def _get_json(self, url: str, headers: dict) -> dict:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            if isinstance(payload, list):
                return payload
            return _json_payload_from_adapter(payload, "DDGS search test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(
                "ddgs_api_unreachable: DDGS local API is not reachable. "
                "Start it with: ddgs api --host 127.0.0.1 --port 4479; "
                "with proxy: ddgs api --host 127.0.0.1 --port 4479 -pr http://127.0.0.1:10793. "
                f"reason={error.reason}"
            ) from error
        except TimeoutError as error:
            raise ToolProviderError(
                "ddgs_api_unreachable: DDGS local API timed out. Start it with: ddgs api --host 127.0.0.1 --port 4479."
            ) from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("DDGS search returned invalid JSON.") from error

    @staticmethod
    def _load_cli_payload(primary_text: str, fallback_text: str) -> object:
        for text in (primary_text, fallback_text):
            for candidate in DDGSWebSearchProvider._json_candidates(text):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        raise ToolProviderError("DDGS CLI search returned invalid JSON.")

    @staticmethod
    def _json_candidates(text: str) -> list[str]:
        value = (text or "").strip()
        if not value:
            return []
        candidates = [value]
        array_start = value.find("[")
        array_end = value.rfind("]")
        if 0 <= array_start < array_end:
            candidates.append(value[array_start : array_end + 1])
        object_start = value.find("{")
        object_end = value.rfind("}")
        if 0 <= object_start < object_end:
            candidates.append(value[object_start : object_end + 1])
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _extract_values(payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        for key in ("results", "items", "data", "value"):
            values = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
        return []


class GoogleProgrammableSearchProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        cx: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.google_cse_api_key
        self.cx = cx if cx is not None else settings.google_cse_cx
        self.provider_name = "google-cse"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get

    def missing_config_reason(self) -> str:
        missing = []
        if not self.api_key:
            missing.append("GOOGLE_CSE_API_KEY")
        if not self.cx:
            missing.append("GOOGLE_CSE_CX")
        return "" if not missing else f"{' and '.join(missing)} is not configured."

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        missing = self.missing_config_reason()
        if missing:
            raise ProviderConfigMissingError(missing)
        safe_count = max(1, min(int(count or 5), 10))
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            "num": str(safe_count),
            "lr": "lang_zh-CN",
        }
        payload = self._get_json(f"{GOOGLE_CSE_SEARCH_URL}?{urlencode(params)}", {"Accept": "application/json"})
        values = (payload.get("items") or [])[:safe_count]
        results = [
            WebSearchItem(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("link") or "").strip(),
                snippet=str(item.get("snippet") or "").strip(),
                source_name=str(
                    ((item.get("pagemap") or {}).get("metatags") or [{}])[0].get("og:site_name") or ""
                ).strip()
                or _source_name(str(item.get("link") or "")),
                confidence=_web_search_item_confidence(item, index, len(values)),
                credibility_rank=_credibility_rank(
                    str(item.get("link") or ""), str(item.get("title") or ""), str(item.get("snippet") or "")
                ),
                provider_name="google-cse",
                fallback_used=False,
                user_visible_caveat="Google Programmable Search 结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(values, start=1)
            if str(item.get("title") or "").strip() and str(item.get("link") or "").strip()
        ]
        ranked = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not ranked:
            raise ToolProviderError("Google CSE returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.9, max(item.confidence for item in ranked)),
            provider_name="google-cse",
            fallback_used=False,
            user_visible_caveat="Google Programmable Search 结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
        )

    def _get_json(self, url: str, headers: dict) -> dict:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            return _json_payload_from_adapter(payload, "Google CSE test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("Google CSE returned invalid JSON.") from error


class CheetahDuckDuckGoSearchProvider:
    provider_name = "cheetah-duckduckgo-html-search"
    parser_name = "cheetahclaws_result_regex"

    def __init__(
        self,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[..., Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else 30.0
        self.http_get = http_get
        self.search_url = CHEETAH_DUCKDUCKGO_HTML_SEARCH_URL
        self.proxy_url = str(settings.duckduckgo_proxy_url or settings.web_search_proxy_url or "").strip()

    def missing_config_reason(self) -> str:
        return ""

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 8))
        params = {"q": query}
        headers = {"User-Agent": "Mozilla/5.0 (compatible)"}
        endpoint_host = urlparse(self.search_url).netloc
        html = self._get_text(params, headers)
        extracted_items = _extract_cheetah_duckduckgo_results(html, safe_count)
        if not extracted_items:
            raise ToolProviderError("parser_empty: Cheetah DuckDuckGo search returned no usable results.")
        results = [
            WebSearchItem(
                title=item["title"],
                url=item["url"],
                snippet=item.get("snippet", ""),
                source_name=_source_name(item["url"]),
                confidence=_web_search_item_confidence(item, index, len(extracted_items)),
                credibility_rank=_credibility_rank(item["url"], item["title"], item.get("snippet", "")),
                provider_name=self.provider_name,
                fallback_used=False,
                user_visible_caveat=(
                    "Cheetah DuckDuckGo HTML 搜索为免费非官方联网搜索，结果仅供规划参考，请以景区官方公告和官方预约入口为准。"
                ),
            )
            for index, item in enumerate(extracted_items[:safe_count], start=1)
            if item.get("title") and item.get("url")
        ]
        results = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not results:
            raise ToolProviderError("parser_empty: Cheetah DuckDuckGo search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=results,
            confidence=min(0.86, 0.5 + 0.07 * len(results)),
            provider_name=self.provider_name,
            fallback_used=False,
            user_visible_caveat=(
                "Cheetah DuckDuckGo HTML 搜索为免费非官方联网搜索，可能受页面结构、网络或反爬限制影响；"
                "重要票务和预约信息请以官方页面为准。"
            ),
            provider_diagnostics=[
                {
                    "providerName": self.provider_name,
                    "status": "success",
                    "reason": "ok",
                    "endpointHost": endpoint_host,
                    "parserName": self.parser_name,
                    **_proxy_diagnostics(self.proxy_url),
                    "resultCount": len(results),
                }
            ],
            attempted_providers=[self.provider_name],
            successful_providers=[self.provider_name],
        )

    def _get_text(self, params: dict[str, str], headers: dict[str, str]) -> str:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            try:
                payload = self.http_get(self.search_url, timeout_seconds, headers, params, self.proxy_url)
            except TypeError:
                payload = self.http_get(f"{self.search_url}?{urlencode(params)}", timeout_seconds, headers)
            if isinstance(payload, str):
                return payload
            if isinstance(payload, dict):
                text = payload.get("html") or payload.get("body") or payload.get("text")
                if isinstance(text, str):
                    return text
            raise ToolProviderError("Cheetah DuckDuckGo search test adapter returned unsupported payload.")
        try:
            import httpx
        except ImportError as error:
            raise ToolProviderError("httpx_missing: install httpx>=0.27.0 to use Cheetah DuckDuckGo search.") from error
        request_kwargs: dict[str, object] = {
            "params": params,
            "headers": headers,
            "timeout": timeout_seconds,
            "follow_redirects": True,
            "trust_env": False,
        }
        if self.proxy_url:
            request_kwargs["proxy"] = self.proxy_url
        try:
            response = httpx.get(self.search_url, **request_kwargs)
            response.raise_for_status()
            return response.text
        except httpx.TimeoutException as error:
            raise ToolProviderError("timeout: Cheetah DuckDuckGo search timed out.") from error
        except httpx.HTTPStatusError as error:
            raise ToolProviderError(f"HTTP {error.response.status_code}") from error
        except httpx.RequestError as error:
            raise ToolProviderError(f"network_error: {error.__class__.__name__}") from error


class BingHTMLSearchProvider:
    provider_name = "bing-html-search"

    def __init__(
        self,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get
        self.proxy_url = str(settings.bing_proxy_url or settings.web_search_proxy_url or "").strip()

    def missing_config_reason(self) -> str:
        return ""

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 10))
        params = {"q": query}
        freshness_filter = _bing_freshness_filter(freshness)
        if freshness_filter:
            params["filters"] = freshness_filter
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        html = self._get_text(f"{BING_HTML_SEARCH_URL}?{urlencode(params)}", headers)
        parser = _BingHTMLSearchParser()
        parser.feed(html)
        parser.close()
        results = [
            WebSearchItem(
                title=item["title"],
                url=item["url"],
                snippet=item.get("snippet", ""),
                source_name=_source_name(item["url"]),
                confidence=_web_search_item_confidence(item, index, len(parser.results)),
                credibility_rank=_credibility_rank(item["url"], item["title"], item.get("snippet", "")),
                provider_name=self.provider_name,
                fallback_used=False,
                user_visible_caveat="Bing HTML 搜索为免费非官方联网搜索，结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(parser.results[:safe_count], start=1)
            if item.get("title") and item.get("url")
        ]
        results = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not results:
            raise ToolProviderError("Bing HTML search returned no usable fresh public results.")
        return WebSearchResponse(
            query=query,
            results=results,
            confidence=min(0.86, 0.5 + 0.07 * len(results)),
            provider_name=self.provider_name,
            fallback_used=False,
            user_visible_caveat="Bing HTML 搜索为免费非官方联网搜索，可能受页面结构、网络或反爬限制影响；重要票务和预约信息请以官方页面为准。",
            provider_diagnostics=[
                {
                    "providerName": self.provider_name,
                    "status": "success",
                    "reason": "ok",
                    "endpointHost": urlparse(BING_HTML_SEARCH_URL).netloc,
                    **_proxy_diagnostics(self.proxy_url),
                    "resultCount": len(results),
                }
            ],
            attempted_providers=[self.provider_name],
            successful_providers=[self.provider_name],
        )

    def _get_text(self, url: str, headers: dict) -> str:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            if isinstance(payload, str):
                return payload
            if isinstance(payload, dict):
                text = payload.get("html") or payload.get("body") or payload.get("text")
                if isinstance(text, str):
                    return text
            raise ToolProviderError("Bing HTML search test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {}
            open_request = build_opener(ProxyHandler(proxies)).open
            with open_request(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error


class DuckDuckGoWebSearchProvider:
    def __init__(
        self,
        provider_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.provider_name = provider_name or "duckduckgo"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get
        self.proxy_url = str(settings.duckduckgo_proxy_url or "").strip()
        base_url = str(settings.duckduckgo_search_base_url or "").strip().rstrip("/")
        derived_html_url = f"{base_url}/html/" if base_url else ""
        derived_lite_url = f"{base_url}/lite/" if base_url else ""
        self.html_search_url = (
            str(settings.duckduckgo_html_search_url or "").strip() or derived_html_url or DUCKDUCKGO_HTML_SEARCH_URL
        )
        self.lite_search_url = (
            str(settings.duckduckgo_lite_search_url or "").strip() or derived_lite_url or DUCKDUCKGO_LITE_SEARCH_URL
        )

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 10))
        params = {
            "q": query,
            "kl": "cn-zh",
            "kp": "-1",
        }
        freshness_code = _duckduckgo_freshness(freshness)
        if freshness_code:
            params["df"] = freshness_code
        diagnostics: list[dict] = []
        parser, provider_name, fallback_used = self._search_html_or_lite(params, diagnostics)
        results = [
            WebSearchItem(
                title=item["title"],
                url=item["url"],
                snippet=item.get("snippet", ""),
                source_name=_source_name(item["url"]),
                confidence=_web_search_item_confidence(item, index, len(parser.results)),
                credibility_rank=_credibility_rank(item["url"], item["title"], item.get("snippet", "")),
                provider_name=provider_name,
                fallback_used=fallback_used,
                user_visible_caveat="DuckDuckGo HTML 搜索为免费非官方联网搜索，结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(parser.results[:safe_count], start=1)
            if item.get("title") and item.get("url")
        ]
        results = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not results:
            raise ToolProviderError("DuckDuckGo search returned no usable fresh results.")
        return WebSearchResponse(
            query=query,
            results=results,
            confidence=min(0.86, 0.5 + 0.07 * len(results)),
            provider_name=provider_name,
            fallback_used=fallback_used,
            user_visible_caveat="DuckDuckGo HTML 搜索为免费非官方联网搜索，可能受页面结构或反爬限制影响；重要票务和预约信息请以官方页面为准。",
            provider_diagnostics=diagnostics,
        )

    def _search_html_or_lite(
        self, params: dict[str, str], diagnostics: list[dict]
    ) -> tuple["_DuckDuckGoHTMLSearchParser", str, bool]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        html_error = ""
        html_host = urlparse(self.html_search_url).netloc
        lite_host = urlparse(self.lite_search_url).netloc
        try:
            html = self._get_text(f"{self.html_search_url}?{urlencode(params)}", headers)
            parser = _DuckDuckGoHTMLSearchParser()
            parser.feed(html)
            parser.close()
            if parser.results:
                diagnostics.append(
                    {
                        "providerName": "duckduckgo-html-search",
                        "status": "success",
                        "endpointHost": html_host,
                        **_proxy_diagnostics(self.proxy_url),
                        "resultCount": len(parser.results),
                    }
                )
                return parser, "duckduckgo-html-search", False
            html_error = "parser_empty"
            diagnostics.append(
                {
                    "providerName": "duckduckgo-html-search",
                    "status": "failed",
                    "endpointHost": html_host,
                    **_proxy_diagnostics(self.proxy_url),
                    "reason": "parser_empty",
                    "resultCount": 0,
                }
            )
        except Exception as error:
            html_error = _safe_provider_error(error)
            diagnostics.append(
                {
                    "providerName": "duckduckgo-html-search",
                    "status": "failed",
                    "endpointHost": html_host,
                    **_proxy_diagnostics(self.proxy_url),
                    "reason": _provider_failure_reason(error),
                    "message": html_error,
                    "resultCount": 0,
                }
            )
        lite_params = dict(params)
        try:
            html = self._get_text(f"{self.lite_search_url}?{urlencode(lite_params)}", headers)
            parser = _DuckDuckGoHTMLSearchParser()
            parser.feed(html)
            parser.close()
            if parser.results:
                diagnostics.append(
                    {
                        "providerName": "duckduckgo-lite-search",
                        "status": "success",
                        "endpointHost": lite_host,
                        **_proxy_diagnostics(self.proxy_url),
                        "reason": "html_fallback",
                        "resultCount": len(parser.results),
                    }
                )
                return parser, "duckduckgo-lite-search", True
            diagnostics.append(
                {
                    "providerName": "duckduckgo-lite-search",
                    "status": "failed",
                    "endpointHost": lite_host,
                    **_proxy_diagnostics(self.proxy_url),
                    "reason": "parser_empty",
                    "resultCount": 0,
                }
            )
        except Exception as error:
            diagnostics.append(
                {
                    "providerName": "duckduckgo-lite-search",
                    "status": "failed",
                    "endpointHost": lite_host,
                    **_proxy_diagnostics(self.proxy_url),
                    "reason": _provider_failure_reason(error),
                    "message": _safe_provider_error(error),
                    "resultCount": 0,
                }
            )
        raise ToolProviderError(
            f"DuckDuckGo search returned no usable results; html={html_error or 'empty'}, lite=empty."
        )

    def _get_text(self, url: str, headers: dict) -> str:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            if isinstance(payload, str):
                return payload
            if isinstance(payload, dict):
                text = payload.get("html") or payload.get("body") or payload.get("text")
                if isinstance(text, str):
                    return text
            raise ToolProviderError("DuckDuckGo search test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {}
            open_request = build_opener(ProxyHandler(proxies)).open
            with open_request(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error


class BaiduWebSearchProvider:
    def __init__(
        self,
        provider_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
    ):
        settings = get_settings()
        self.provider_name = provider_name or "baidu"
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.http_get = http_get
        self.proxy_url = str(settings.baidu_proxy_url or "").strip()

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 10))
        params = {
            "wd": query,
            "rn": str(max(safe_count, 5)),
            "ie": "utf-8",
            "tn": "baiduhome_pg",
        }
        html = self._get_text(
            f"{BAIDU_HTML_SEARCH_URL}?{urlencode(params)}",
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            },
        )
        extracted_items = _extract_baidu_public_urls(html, safe_count)
        if not extracted_items and _looks_like_baidu_blocked(html):
            raise ToolProviderError("blocked_or_captcha: Baidu search returned verification page.")
        results = [
            WebSearchItem(
                title=item.get("title") or _title_from_url(item["url"]),
                url=item["url"],
                snippet=item.get("snippet", ""),
                source_name=_source_name(item["url"]),
                confidence=_web_search_item_confidence(item, index, len(extracted_items)),
                credibility_rank=_credibility_rank(item["url"], item.get("title", ""), item.get("snippet", "")),
                provider_name="baidu-html-search",
                fallback_used=False,
                user_visible_caveat="Baidu HTML 搜索为免费非官方联网搜索，结果仅供规划参考，请以景区官方公告和官方预约入口为准。",
            )
            for index, item in enumerate(extracted_items, start=1)
        ]
        results = _validated_ranked_search_results(query, results, freshness)[:safe_count]
        if not results:
            raise ToolProviderError("Baidu search returned no usable fresh public result URLs.")
        return WebSearchResponse(
            query=query,
            results=results,
            confidence=min(0.84, 0.48 + 0.07 * len(results)),
            provider_name="baidu-html-search",
            fallback_used=False,
            user_visible_caveat="Baidu HTML 搜索为免费非官方联网搜索，可能受页面结构或反爬限制影响；重要票务和预约信息请以官方页面为准。",
            provider_diagnostics=[
                {
                    "providerName": "baidu-html-search",
                    "status": "success",
                    "endpointHost": urlparse(BAIDU_HTML_SEARCH_URL).netloc,
                    **_proxy_diagnostics(self.proxy_url),
                    "resultCount": len(results),
                }
            ],
        )

    def _get_text(self, url: str, headers: dict) -> str:
        timeout_seconds = _effective_web_search_timeout(self.timeout_seconds)
        if self.http_get is not None:
            payload = self.http_get(url, timeout_seconds, headers)
            if isinstance(payload, str):
                return payload
            if isinstance(payload, dict):
                text = payload.get("html") or payload.get("body") or payload.get("text")
                if isinstance(text, str):
                    return text
            raise ToolProviderError("Baidu search test adapter returned unsupported payload.")
        request = Request(url, headers=headers, method="GET")
        try:
            proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {}
            open_request = build_opener(ProxyHandler(proxies)).open
            with open_request(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            raise ToolProviderError(f"HTTP {error.code}") from error
        except URLError as error:
            raise ToolProviderError(str(error.reason)) from error
        except TimeoutError as error:
            raise ToolProviderError("请求超时") from error


class MultiFreeWebSearchProvider:
    def __init__(
        self,
        providers: Optional[list[object]] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
        max_child_attempts: Optional[int] = None,
    ):
        settings = get_settings()
        self.provider_name = "multi-free-search"
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.provider_timeout_seconds
        self.http_get = http_get
        self.providers = providers or [
            BaiduWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=http_get),
            DuckDuckGoWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=http_get),
        ]
        self.max_child_attempts = max(1, min(int(max_child_attempts or len(self.providers)), len(self.providers)))

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 10))
        collected: list[WebSearchItem] = []
        failures: list[str] = []
        child_diagnostics: list[dict] = []
        for provider in self.providers[: self.max_child_attempts]:
            try:
                response = provider.search(query, count=safe_count, freshness=freshness)
            except Exception as error:
                provider_name = _provider_result_name(getattr(provider, "provider_name", provider.__class__.__name__))
                failures.append(provider_name)
                reason_code = _provider_failure_reason(error)
                child_diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "failed",
                        "reason": reason_code,
                        "reasonCode": reason_code,
                        "resultCount": 0,
                    }
                )
                continue
            collected.extend(response.results)
            if response.provider_diagnostics:
                child_diagnostics.extend(
                    _safe_web_provider_diagnostics(
                        response.provider_diagnostics,
                        limit=6,
                    )
                )
            else:
                child_diagnostics.append(
                    {
                        "providerName": _provider_result_name(response.provider_name),
                        "status": "success",
                        "resultCount": len(response.results),
                    }
                )
        ranked = _validated_ranked_search_results(query, collected, freshness)[:safe_count]
        if not ranked:
            raise ToolProviderError("multi_free_all_children_failed_or_empty")
        caveats = [
            "已聚合多个免费搜索源并按来源可信度、结果相关性和信息时效排序。",
            "免费 HTML 搜索可能受页面结构、网络或反爬限制影响；关键票务和预约信息请以官方页面为准。",
        ]
        if failures:
            caveats.append(f"部分搜索源失败：{'、'.join(failures[:2])}")
        return WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.9, max(item.confidence for item in ranked)),
            provider_name="multi-free-search",
            fallback_used=False,
            failure_reason=None,
            user_visible_caveat=" ".join(caveats),
            provider_diagnostics=[
                {
                    "providerName": "multi-free-search",
                    "status": "success",
                    "reason": "ok",
                    "reasonCode": "ok",
                    "resultCount": len(ranked),
                    "failureCount": len(failures),
                    "providerDiagnostics": _safe_web_provider_diagnostics(
                        child_diagnostics,
                        limit=8,
                    ),
                }
            ]
            + _safe_web_provider_diagnostics(child_diagnostics, limit=8),
            attempted_providers=[
                getattr(provider, "provider_name", provider.__class__.__name__)
                for provider in self.providers[: self.max_child_attempts]
            ],
            successful_providers=sorted({item.provider_name for item in ranked if item.provider_name}),
            failed_providers=failures,
        )


class ChainedWebSearchProvider:
    def __init__(
        self,
        providers: Optional[list[object]] = None,
        provider_chain: Optional[str] = None,
        min_accepted_results: Optional[int] = None,
        max_provider_attempts: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        total_deadline_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
        http_post: Optional[Callable[[str, dict, float, Optional[dict]], dict]] = None,
    ):
        settings = get_settings()
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.web_search_provider_timeout_seconds
        )
        self.timeout_seconds_explicit = timeout_seconds is not None
        self.min_accepted_results = max(1, int(min_accepted_results or settings.web_search_min_accepted_results or 1))
        self.max_provider_attempts = max(
            1, int(max_provider_attempts or settings.web_search_max_provider_attempts or 4)
        )
        self.total_deadline_seconds = max(
            0.001,
            float(
                total_deadline_seconds
                if total_deadline_seconds is not None
                else settings.web_search_chain_deadline_seconds
            ),
        )
        self.http_get = http_get
        self.http_post = http_post
        self.provider_names = _provider_chain_names(provider_chain or settings.web_search_provider_chain)
        self.providers = (
            providers if providers is not None else [self._provider_from_name(name) for name in self.provider_names]
        )
        self.provider_name = "chained-web-search"
        self._cache: dict[tuple[str, str, int, str], tuple[float, WebSearchResponse]] = {}
        self._circuit_open_until: dict[str, float] = {}
        _WEB_SEARCH_PROVIDER_INSTANCES.add(self)

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        if _WEB_SEARCH_CHAIN_DEADLINE.get() is not None:
            return self._search_with_deadline(
                query,
                count=count,
                freshness=freshness,
            )
        token = _WEB_SEARCH_CHAIN_DEADLINE.set(time.perf_counter() + self.total_deadline_seconds)
        try:
            return self._search_with_deadline(
                query,
                count=count,
                freshness=freshness,
            )
        finally:
            _WEB_SEARCH_CHAIN_DEADLINE.reset(token)

    def _search_with_deadline(
        self,
        query: str,
        count: int = 5,
        freshness: str = "noLimit",
    ) -> WebSearchResponse:
        safe_count = max(1, min(int(count or 5), 20))
        cache_key = (query, freshness, safe_count, ",".join(self._provider_names_for_cache()))
        cached = self._cache_get(cache_key, freshness)
        if cached is not None:
            return cached

        diagnostics: list[dict] = []
        collected: list[WebSearchItem] = []
        attempted: list[str] = []
        successful: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        attempt_count = 0

        for provider in self.providers:
            provider_name = _provider_result_name(getattr(provider, "provider_name", provider.__class__.__name__))
            remaining_seconds = _remaining_web_search_deadline_seconds()
            if remaining_seconds is not None and remaining_seconds <= 0:
                skipped.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "skipped",
                        "reason": "chain_deadline_exhausted",
                        "reasonCode": "chain_deadline_exhausted",
                        "durationMs": 0,
                        "timeoutSeconds": 0.0,
                        "resultCount": 0,
                    }
                )
                break
            missing = _provider_missing_config(provider)
            if missing:
                skipped.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "skipped",
                        "reason": "skipped_missing_config",
                        "message": missing,
                        "resultCount": 0,
                    }
                )
                continue
            if self._is_circuit_open(provider_name):
                skipped.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "skipped",
                        "reason": "circuit_open",
                        "resultCount": 0,
                    }
                )
                continue
            if attempt_count >= self.max_provider_attempts:
                skipped.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "skipped",
                        "reason": "max_provider_attempts_reached",
                        "resultCount": 0,
                    }
                )
                continue

            attempted.append(provider_name)
            attempt_timeout_seconds = min(
                max(
                    0.001,
                    float(getattr(provider, "timeout_seconds", self.timeout_seconds) or self.timeout_seconds),
                ),
                float(self.total_deadline_seconds if remaining_seconds is None else remaining_seconds),
            )
            attempt_trace = self._attempt_trace(
                provider,
                attempt_timeout_seconds,
            )
            attempt_weight = 1
            if isinstance(provider, MultiFreeWebSearchProvider):
                remaining_attempts = max(1, self.max_provider_attempts - attempt_count)
                provider.max_child_attempts = min(len(provider.providers), remaining_attempts)
                attempt_weight = provider.max_child_attempts
            attempt_count += attempt_weight
            started = time.perf_counter()
            try:
                response = provider.search(query, count=safe_count, freshness=freshness)
            except ProviderConfigMissingError as error:
                skipped.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "skipped",
                        "reason": "skipped_missing_config",
                        "reasonCode": "skipped_missing_config",
                        "durationMs": int((time.perf_counter() - started) * 1000),
                        "resultCount": 0,
                        **attempt_trace,
                    }
                )
                continue
            except Exception as error:
                failed.append(provider_name)
                reason = _provider_failure_reason(error)
                error_diagnostics = dict(error.diagnostics if isinstance(error, WebSearchAttemptError) else {})
                child_diagnostics = error_diagnostics.pop(
                    "providerDiagnostics",
                    [],
                )
                if _should_open_web_search_circuit(provider_name, reason):
                    self._open_circuit(provider_name)
                safe_attempt = _safe_web_provider_diagnostics(
                    [
                        {
                            "providerName": provider_name,
                            "status": "failed",
                            "reason": reason,
                            "reasonCode": reason,
                            "durationMs": int((time.perf_counter() - started) * 1000),
                            "resultCount": 0,
                            **error_diagnostics,
                            **attempt_trace,
                        }
                    ]
                )[0]
                if (
                    isinstance(provider, AnySearchWebSearchProvider)
                    and provider.proxy_mode == "auto"
                    and isinstance(child_diagnostics, list)
                ):
                    safe_children = _safe_web_provider_diagnostics(
                        child_diagnostics,
                        limit=2,
                    )
                    if safe_children:
                        safe_attempt["providerDiagnostics"] = safe_children
                diagnostics.append(safe_attempt)
                continue

            provider_results = _validated_ranked_search_results(query, response.results, freshness)[:safe_count]
            collected.extend(provider_results)
            duration_ms = int((time.perf_counter() - started) * 1000)
            if provider_results:
                successful.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "success",
                        "reason": "ok",
                        "reasonCode": "ok",
                        "durationMs": duration_ms,
                        "resultCount": len(provider_results),
                        "credibleResultCount": _credible_result_count(provider_results),
                        "providerDiagnostics": _safe_web_provider_diagnostics(
                            response.provider_diagnostics,
                            limit=6,
                        ),
                        **attempt_trace,
                    }
                )
            else:
                failed.append(provider_name)
                diagnostics.append(
                    {
                        "providerName": provider_name,
                        "status": "no_results",
                        "reason": "no_usable_results",
                        "reasonCode": "no_usable_results",
                        "durationMs": duration_ms,
                        "resultCount": 0,
                        "providerDiagnostics": _safe_web_provider_diagnostics(
                            response.provider_diagnostics,
                            limit=6,
                        ),
                        **attempt_trace,
                    }
                )

            ranked = _validated_ranked_search_results(query, collected, freshness)[:safe_count]
            if self._should_stop_after_provider(provider_name, ranked):
                collected = ranked
                break

        ranked = _validated_ranked_search_results(query, collected, freshness)[:safe_count]
        fallback_used = bool(
            ranked
            and (failed or skipped or (successful and successful[0] != _first_configured_provider_name(diagnostics)))
        )
        failure_reason = None if ranked else "all_web_search_providers_failed_or_empty"
        response = WebSearchResponse(
            query=query,
            results=ranked,
            confidence=min(0.94, max((item.confidence for item in ranked), default=0.0)),
            provider_name="chained-web-search",
            fallback_used=fallback_used,
            failure_reason=failure_reason,
            user_visible_caveat=(
                "联网搜索 provider chain 已返回可用结果；关键票务、预约和限流信息仍以官方公告为准。"
                if ranked
                else "所有已配置联网搜索供应商均失败、跳过或未返回可用结果；未使用 mock 搜索结果。"
            ),
            provider_diagnostics=diagnostics,
            attempted_providers=attempted,
            successful_providers=list(dict.fromkeys(successful)),
            failed_providers=list(dict.fromkeys(failed)),
            skipped_providers=list(dict.fromkeys(skipped)),
        )
        self._cache_set(cache_key, freshness, response)
        return response

    @staticmethod
    def _attempt_trace(provider: object, timeout_seconds: float) -> dict[str, object]:
        trace: dict[str, object] = {
            "timeoutSeconds": round(max(0.0, timeout_seconds), 6),
        }
        if isinstance(provider, AnySearchWebSearchProvider):
            if provider.proxy_mode in {"system", "direct"}:
                trace.update(
                    {
                        "transportRoute": provider.proxy_mode,
                        "proxyConfigured": bool(provider.proxy_mode == "system" and _system_proxy_configured()),
                    }
                )
        return trace

    def _provider_from_name(self, name: str) -> object:
        provider = name.strip().lower()
        if provider in {"bocha", "bocha-web-search", "bochaai"}:
            return BochaWebSearchProvider(
                timeout_seconds=self.timeout_seconds, http_get=self.http_get, http_post=self.http_post
            )
        if provider == "tavily":
            return TavilyWebSearchProvider(timeout_seconds=self.timeout_seconds, http_post=self.http_post)
        if provider in {"anysearch", "any-search"}:
            return AnySearchWebSearchProvider(
                timeout_seconds=(self.timeout_seconds if self.timeout_seconds_explicit else None),
                http_post=self.http_post,
            )
        if provider in {"brave", "brave-web-search"}:
            return BraveWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider == "searxng":
            return SearXNGWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider in {"ddgs", "duckduckgo-search"}:
            return DDGSWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider in {"bing", "bing-html", "bing-html-search"}:
            return BingHTMLSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider in {
            "cheetah-ddg",
            "cheetah-duckduckgo",
            "cheetah-duckduckgo-html",
            "cheetah-duckduckgo-html-search",
        }:
            return CheetahDuckDuckGoSearchProvider(
                timeout_seconds=max(float(self.timeout_seconds or 0), 30.0), http_get=self.http_get
            )
        if provider in {"google", "google-cse", "google-programmable-search"}:
            return GoogleProgrammableSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider in {"multi", "multi-free", "multi-free-search", "free", "free-search", "html-multi"}:
            return MultiFreeWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider in {"baidu", "baidu-html", "baidu-html-search"}:
            return BaiduWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        if provider in {"duckduckgo", "duckduckgo-html", "duckduckgo-html-search", "ddg"}:
            return DuckDuckGoWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get)
        return WebSearchProvider(
            provider_name=provider,
            timeout_seconds=self.timeout_seconds,
            http_get=self.http_get,
            http_post=self.http_post,
        )

    def _provider_names_for_cache(self) -> list[str]:
        return [
            _provider_result_name(getattr(provider, "provider_name", provider.__class__.__name__))
            for provider in self.providers
        ]

    def _cache_get(self, key: tuple[str, str, int, str], freshness: str) -> Optional[WebSearchResponse]:
        item = self._cache.get(key)
        if not item:
            return None
        created_at, response = item
        if time.time() - created_at > _web_search_cache_ttl_seconds(freshness):
            self._cache.pop(key, None)
            return None
        cached = copy.deepcopy(response)
        cached.provider_diagnostics = [
            {
                "providerName": "chained-web-search",
                "status": "cache_hit",
                "reason": "query_freshness_provider_chain_cache_hit",
                "resultCount": len(cached.results),
            },
            *cached.provider_diagnostics,
        ]
        return cached

    def _cache_set(self, key: tuple[str, str, int, str], freshness: str, response: WebSearchResponse) -> None:
        if _web_search_cache_ttl_seconds(freshness) <= 0:
            return
        self._cache[key] = (time.time(), copy.deepcopy(response))

    def _is_circuit_open(self, provider_name: str) -> bool:
        until = self._circuit_open_until.get(provider_name)
        if not until:
            return False
        if time.time() >= until:
            self._circuit_open_until.pop(provider_name, None)
            return False
        return True

    def _open_circuit(self, provider_name: str) -> None:
        self._circuit_open_until[provider_name] = time.time() + WEB_SEARCH_CIRCUIT_BREAKER_SECONDS

    def clear_runtime_state(self) -> dict[str, int]:
        cache_entries = len(self._cache)
        circuit_entries = len(self._circuit_open_until)
        self._cache.clear()
        self._circuit_open_until.clear()
        return {"cacheEntriesCleared": cache_entries, "circuitEntriesCleared": circuit_entries}

    def _should_stop_after_provider(self, provider_name: str, ranked: list[WebSearchItem]) -> bool:
        if provider_name not in STABLE_WEB_SEARCH_PROVIDERS:
            return False
        return _credible_result_count(ranked) >= self.min_accepted_results


def clear_web_search_runtime_state() -> dict[str, int]:
    provider_instances = list(_WEB_SEARCH_PROVIDER_INSTANCES)
    cache_entries = 0
    circuit_entries = 0
    for provider in provider_instances:
        cleared = provider.clear_runtime_state()
        cache_entries += int(cleared.get("cacheEntriesCleared") or 0)
        circuit_entries += int(cleared.get("circuitEntriesCleared") or 0)
    return {
        "providerInstances": len(provider_instances),
        "cacheEntriesCleared": cache_entries,
        "circuitEntriesCleared": circuit_entries,
    }


class WebSearchProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        provider_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float, Optional[dict]], Union[dict, str]]] = None,
        http_post: Optional[Callable[[str, dict, float, Optional[dict]], dict]] = None,
    ):
        settings = get_settings()
        self.provider_name = provider_name or settings.web_search_provider or "multi-free"
        self.api_key = api_key if api_key is not None else settings.search_provider_key
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.provider_timeout_seconds
        self.timeout_seconds_explicit = timeout_seconds is not None
        self.http_get = http_get
        self.http_post = http_post

    def search(self, query: str, count: int = 5, freshness: str = "noLimit") -> WebSearchResponse:
        provider = self.provider_name.lower()
        if provider in {"multi", "multi-free", "multi-free-search", "free", "free-search", "html-multi"}:
            return MultiFreeWebSearchProvider(timeout_seconds=self.timeout_seconds, http_get=self.http_get).search(
                query,
                count=count,
                freshness=freshness,
            )
        if provider in {"bocha", "bocha-web-search", "bochaai"}:
            return BochaWebSearchProvider(
                api_key=self.api_key,
                provider_name=provider,
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
                http_post=self.http_post,
            ).search(query, count=count, freshness=freshness)
        if provider == "tavily":
            return TavilyWebSearchProvider(
                timeout_seconds=self.timeout_seconds,
                http_post=self.http_post,
            ).search(query, count=count, freshness=freshness)
        if provider in {"anysearch", "any-search"}:
            return AnySearchWebSearchProvider(
                timeout_seconds=(self.timeout_seconds if self.timeout_seconds_explicit else None),
                http_post=self.http_post,
            ).search(query, count=count, freshness=freshness)
        if provider in {"brave", "brave-web-search"}:
            return BraveWebSearchProvider(
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider == "searxng":
            return SearXNGWebSearchProvider(
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider in {"ddgs", "duckduckgo-search"}:
            return DDGSWebSearchProvider(
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider in {"bing", "bing-html", "bing-html-search"}:
            return BingHTMLSearchProvider(
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider in {
            "cheetah-ddg",
            "cheetah-duckduckgo",
            "cheetah-duckduckgo-html",
            "cheetah-duckduckgo-html-search",
        }:
            return CheetahDuckDuckGoSearchProvider(
                timeout_seconds=max(float(self.timeout_seconds or 0), 30.0),
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider in {"google", "google-cse", "google-programmable-search"}:
            return GoogleProgrammableSearchProvider(
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider in {"duckduckgo", "duckduckgo-html", "duckduckgo-html-search", "ddg"}:
            return DuckDuckGoWebSearchProvider(
                provider_name=provider,
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        if provider in {"baidu", "baidu-html", "baidu-html-search"}:
            return BaiduWebSearchProvider(
                provider_name=provider,
                timeout_seconds=self.timeout_seconds,
                http_get=self.http_get,
            ).search(query, count=count, freshness=freshness)
        raise ToolProviderError(
            f"Unsupported WEB_SEARCH_PROVIDER: {self.provider_name}. Configure anysearch, bing, cheetah-ddg, multi-free, baidu, duckduckgo, ddgs, bocha, tavily, brave or searxng."
        )


class ResilientWebSearchProvider:
    def __init__(
        self,
        default_provider: Optional[object] = None,
        fallback_provider: Optional[object] = None,
        total_deadline_seconds: Optional[float] = None,
    ):
        settings = get_settings()
        explicit_total_deadline = total_deadline_seconds is not None
        self.total_deadline_seconds = max(
            0.001,
            float(
                total_deadline_seconds
                if total_deadline_seconds is not None
                else settings.web_search_chain_deadline_seconds
            ),
        )
        single_mode = str(settings.web_search_provider_mode).strip().lower() == "single"
        if default_provider is None and single_mode:
            primary_chain = ChainedWebSearchProvider(
                provider_chain=settings.web_search_provider,
                max_provider_attempts=1,
                total_deadline_seconds=self.total_deadline_seconds,
            )
            default_provider = primary_chain.providers[0]
            primary_name = _provider_result_name(getattr(default_provider, "provider_name", ""))
            if fallback_provider is None and primary_name == "anysearch":
                fallback_chain = ChainedWebSearchProvider(
                    total_deadline_seconds=self.total_deadline_seconds,
                )
                fallback_providers = [
                    provider
                    for provider in fallback_chain.providers
                    if _provider_result_name(getattr(provider, "provider_name", "")) != primary_name
                ]
                if fallback_providers:
                    fallback_provider = ChainedWebSearchProvider(
                        providers=fallback_providers,
                        total_deadline_seconds=self.total_deadline_seconds,
                    )

        self.fallback_provider = fallback_provider
        if default_provider is None:
            self.default_provider = None
            self.chain_provider = ChainedWebSearchProvider(
                total_deadline_seconds=self.total_deadline_seconds,
            )
        elif fallback_provider is None:
            if explicit_total_deadline:
                self.default_provider = None
                self.chain_provider = ChainedWebSearchProvider(
                    providers=[default_provider],
                    total_deadline_seconds=self.total_deadline_seconds,
                )
            else:
                self.default_provider = default_provider
                self.chain_provider = None
        else:
            fallback_providers = (
                list(fallback_provider.providers)
                if isinstance(fallback_provider, ChainedWebSearchProvider)
                else [fallback_provider]
            )
            self.default_provider = None
            self.chain_provider = ChainedWebSearchProvider(
                providers=[default_provider, *fallback_providers],
                total_deadline_seconds=self.total_deadline_seconds,
            )

    def search(
        self,
        query: str,
        count: int = 5,
        freshness: str = "noLimit",
    ) -> WebSearchResponse:
        if self.chain_provider is not None:
            return self.chain_provider.search(
                query,
                count=count,
                freshness=freshness,
            )

        provider = self.default_provider
        started = time.perf_counter()
        try:
            return provider.search(query, count=count, freshness=freshness)  # type: ignore[union-attr]
        except Exception as error:
            provider_name = _provider_result_name(getattr(provider, "provider_name", "duckduckgo"))
            reason_code = _provider_failure_reason(error)
            error_diagnostics = error.diagnostics if isinstance(error, WebSearchAttemptError) else {}
            return WebSearchResponse(
                query=query,
                results=[],
                confidence=0.0,
                provider_name=provider_name,
                fallback_used=False,
                failure_reason=reason_code,
                user_visible_caveat=(
                    f"{provider_name} 联网搜索失败，未使用 mock 数据；请检查网络、provider 配置或稍后重试。"
                ),
                provider_diagnostics=[
                    {
                        "providerName": provider_name,
                        "status": "failed",
                        "reason": reason_code,
                        "reasonCode": reason_code,
                        "durationMs": int((time.perf_counter() - started) * 1000),
                        "resultCount": 0,
                        **error_diagnostics,
                    }
                ],
                attempted_providers=[provider_name],
                failed_providers=[provider_name],
            )


class AmapWeatherProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_get: Optional[Callable[[str, float], dict]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.weather_provider_key
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.provider_timeout_seconds
        self.http_get = http_get

    def query(
        self,
        city: str,
        travel_date: Optional[str] = None,
        purpose_tags: Optional[list[str]] = None,
        context: Optional[dict] = None,
    ) -> AmapWeatherResponse:
        if not self.api_key:
            raise ToolProviderError("AMAP_WEB_SERVICE_KEY is not configured.")
        params = {
            "key": self.api_key,
            "city": CITY_ADCODE.get(city, city),
            "extensions": "all",
            "output": "JSON",
        }
        payload = self._get(f"{AMAP_WEATHER_URL}?{urlencode(params)}")
        if payload.get("status") != "1":
            raise ToolProviderError(str(payload.get("info") or payload.get("infocode") or "未知错误"))
        forecasts = payload.get("forecasts") or []
        if not forecasts:
            raise ToolProviderError("未返回天气预报")
        forecast = forecasts[0]
        casts = forecast.get("casts") or []
        if not casts:
            raise ToolProviderError("未返回可用天气时段")
        selected = _select_weather_cast(casts, travel_date)
        return _weather_response_from_cast(
            city, forecast, selected, purpose_tags or [], context or {}, fallback_used=False
        )

    def _get(self, url: str) -> dict:
        if self.http_get is not None:
            return self.http_get(url, self.timeout_seconds)
        with AMAP_BASIC_WEATHER_SEMAPHORE:
            AMAP_WEB_SERVICE_RATE_LIMITER.acquire()
            try:
                with urlopen(url, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8")
            except HTTPError as error:
                raise ToolProviderError(f"HTTP {error.code}") from error
            except URLError as error:
                raise ToolProviderError(str(error.reason)) from error
            except TimeoutError as error:
                raise ToolProviderError("请求超时") from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ToolProviderError("高德天气返回了无法解析的数据") from error


class ResilientAmapWeatherProvider:
    def __init__(
        self,
        default_provider: Optional[AmapWeatherProvider] = None,
    ):
        self.default_provider = default_provider or AmapWeatherProvider()

    def query(
        self,
        city: str,
        travel_date: Optional[str] = None,
        purpose_tags: Optional[list[str]] = None,
        context: Optional[dict] = None,
    ) -> AmapWeatherResponse:
        try:
            return self.default_provider.query(
                city, travel_date=travel_date, purpose_tags=purpose_tags, context=context
            )
        except Exception as error:
            error_text = str(error)
            if travel_date and ("未覆盖旅行日期" in error_text or "outside supported forecast" in error_text):
                return AmapWeatherResponse(
                    city=city,
                    date=travel_date,
                    weather="旅行日期对应天气预报尚不可用",
                    temperature_range="待出行前刷新",
                    wind="待出行前刷新",
                    humidity=None,
                    risk_level="unknown",
                    risk_reason="forecast_not_supported_yet: 当前不使用今日天气推断未来行程。",
                    source_name="高德天气",
                    confidence=0.0,
                    provider_name="amap-weather-provider",
                    fallback_used=True,
                    failure_reason="forecast_not_supported_yet",
                    user_visible_caveat="当前未查询/未使用今日天气，请在出行前 3 天内刷新天气。",
                    raw={"requestedDate": travel_date, "weatherStatus": "forecast_not_supported_yet"},
                )
            return AmapWeatherResponse(
                city=city,
                date=travel_date or date.today().isoformat(),
                weather="待确认",
                temperature_range="待确认",
                wind="待确认",
                humidity=None,
                risk_level="unknown",
                risk_reason="高德天气查询失败，无法判断天气是否影响本次旅行目的。",
                source_name="高德天气",
                confidence=0.0,
                provider_name="amap-weather-provider",
                fallback_used=False,
                failure_reason=str(error),
                user_visible_caveat="高德天气查询失败，未使用 mock 天气数据；请检查 AMAP_WEB_SERVICE_KEY、网络或稍后重试。",
                raw={},
            )


class _DuckDuckGoHTMLSearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: Optional[dict[str, str]] = None
        self._capture: Optional[str] = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        class_names = set(attr_map.get("class", "").split())
        href = attr_map.get("href", "")
        rel_values = set(attr_map.get("rel", "").split())
        if tag == "a" and (
            "result__a" in class_names
            or "result-link" in class_names
            or ("nofollow" in rel_values and href)
            or (href and _is_public_search_url(_normalize_duckduckgo_url(href)))
        ):
            self._flush_current()
            href = _normalize_duckduckgo_url(href)
            self._current = {"url": href, "title": "", "snippet": ""}
            self._capture = "title"
            self._buffer = []
            return
        if (
            self._current is not None
            and tag in {"a", "div", "td", "span"}
            and ("result__snippet" in class_names or "result-snippet" in class_names or "snippet" in class_names)
        ):
            self._capture = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture == "title" and tag == "a":
            self._commit_capture()
        elif self._capture == "snippet" and tag in {"a", "div"}:
            self._commit_capture()

    def close(self) -> None:
        self._commit_capture()
        self._flush_current()
        super().close()

    def _commit_capture(self) -> None:
        if self._current is None or self._capture is None:
            self._buffer = []
            self._capture = None
            return
        text = _clean_text(" ".join(self._buffer))
        if text:
            self._current[self._capture] = text
        self._buffer = []
        self._capture = None

    def _flush_current(self) -> None:
        if self._current and self._current.get("title") and self._current.get("url"):
            if not any(item.get("url") == self._current["url"] for item in self.results):
                self.results.append(self._current)
        self._current = None


def _normalize_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    redirect = query.get("uddg", [""])[0]
    if redirect:
        return unquote(redirect)
    return url


def _extract_cheetah_duckduckgo_results(html: str, limit: int = 8) -> list[dict[str, str]]:
    titles = re.findall(
        r'class=["\']result__title["\'][^>]*>.*?<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html or "",
        re.DOTALL | re.IGNORECASE,
    )
    snippets = re.findall(
        r'class=["\']result__snippet["\'][^>]*>(.*?)</(?:a|div|span)>',
        html or "",
        re.DOTALL | re.IGNORECASE,
    )
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, (link, title_html) in enumerate(titles[: max(1, limit)], start=0):
        url = _normalize_duckduckgo_url(unescape(link).strip())
        title = _clean_text(re.sub(r"<[^>]+>", " ", unescape(title_html)))
        snippet_html = snippets[index] if index < len(snippets) else ""
        snippet = _clean_text(re.sub(r"<[^>]+>", " ", unescape(snippet_html)))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


class _BaiduHTMLSearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: Optional[dict[str, object]] = None
        self._result_depth = 0
        self._capture_title = False
        self._title_buffer: list[str] = []
        self._text_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        class_names = set(attr_map.get("class", "").split())
        is_result_container = tag == "div" and (
            "result" in class_names or "c-container" in class_names or bool(attr_map.get("mu"))
        )
        if is_result_container:
            self._flush_current()
            self._current = {"url": "", "title": "", "snippet": "", "candidateUrls": []}
            self._result_depth = 1
            self._add_candidate_url(attr_map.get("mu", ""))
            self._add_candidate_url(_url_from_baidu_data_click(attr_map.get("data-click", "")))
            return

        if self._current is None:
            return
        self._result_depth += 1
        self._add_candidate_url(attr_map.get("mu", ""))
        self._add_candidate_url(_url_from_baidu_data_click(attr_map.get("data-click", "")))
        if tag == "a" and attr_map.get("href"):
            href = _normalize_baidu_result_url(attr_map.get("href", ""))
            if href:
                if not self._current.get("url"):
                    self._current["url"] = href
                self._add_candidate_url(href)
                self._capture_title = True
                self._title_buffer = []

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._capture_title:
            self._title_buffer.append(data)
        self._text_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if self._capture_title and tag == "a":
            title = _clean_text(" ".join(self._title_buffer))
            if title and not self._current.get("title"):
                self._current["title"] = title
            self._capture_title = False
            self._title_buffer = []
        self._result_depth -= 1
        if self._result_depth <= 0:
            self._flush_current()

    def close(self) -> None:
        self._flush_current()
        super().close()

    def _add_candidate_url(self, value: str) -> None:
        if self._current is None:
            return
        url = _normalize_baidu_result_url(value)
        if not url:
            return
        candidates = self._current.setdefault("candidateUrls", [])
        if isinstance(candidates, list) and url not in candidates:
            candidates.append(url)

    def _flush_current(self) -> None:
        if not self._current:
            self._reset_current()
            return
        candidates = self._current.get("candidateUrls")
        url = str(self._current.get("url") or "")
        if isinstance(candidates, list):
            public_candidates = [candidate for candidate in candidates if _is_public_search_url(str(candidate))]
            if public_candidates:
                url = str(public_candidates[0])
        if url and _is_public_search_url(url):
            title = _clean_text(str(self._current.get("title") or "")) or _title_from_url(url)
            full_text = _clean_text(" ".join(self._text_buffer))
            snippet = full_text.replace(title, "", 1).strip()[:240] if full_text else ""
            if not any(item.get("url") == url for item in self.results):
                self.results.append({"url": url, "title": title, "snippet": snippet})
        self._reset_current()

    def _reset_current(self) -> None:
        self._current = None
        self._result_depth = 0
        self._capture_title = False
        self._title_buffer = []
        self._text_buffer = []


class _BingHTMLSearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: Optional[dict[str, str]] = None
        self._result_depth = 0
        self._capture: Optional[str] = None
        self._buffer: list[str] = []
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        class_names = set(attr_map.get("class", "").split())
        if tag == "li" and "b_algo" in class_names:
            self._flush_current()
            self._current = {"url": "", "title": "", "snippet": ""}
            self._result_depth = 1
            return
        if self._current is None:
            return
        self._result_depth += 1
        if tag == "h2":
            self._heading_depth = 1
            return
        if self._heading_depth > 0:
            self._heading_depth += 1
        href = attr_map.get("href", "")
        if self._heading_depth > 0 and tag == "a" and href and not self._current.get("url"):
            url = _normalize_bing_result_url(href)
            if _is_public_search_url(url):
                self._current["url"] = url
                self._capture = "title"
                self._buffer = []
        if tag in {"p", "div"} and (
            "b_lineclamp2" in class_names or "b_caption" in class_names or "b_snippet" in class_names
        ):
            self._capture = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if self._capture == "title" and tag == "a":
            self._commit_capture()
        elif self._capture == "snippet" and tag in {"p", "div"}:
            self._commit_capture()
        if self._heading_depth > 0:
            self._heading_depth -= 1
        if tag == "li":
            self._result_depth -= 1
            if self._result_depth <= 0:
                self._flush_current()
        elif self._result_depth > 0:
            self._result_depth -= 1

    def close(self) -> None:
        self._commit_capture()
        self._flush_current()
        super().close()

    def _commit_capture(self) -> None:
        if self._current is None or self._capture is None:
            self._buffer = []
            self._capture = None
            return
        text = _clean_text(" ".join(self._buffer))
        if text and not self._current.get(self._capture):
            self._current[self._capture] = text
        self._buffer = []
        self._capture = None

    def _flush_current(self) -> None:
        if self._current and self._current.get("title") and self._current.get("url"):
            if not any(item.get("url") == self._current["url"] for item in self.results):
                self.results.append(self._current)
        self._current = None
        self._result_depth = 0
        self._capture = None
        self._buffer = []
        self._heading_depth = 0


def _duckduckgo_freshness(freshness: str) -> Optional[str]:
    return {
        "oneYear": "y",
        "oneMonth": "m",
        "oneWeek": "w",
        "oneDay": "d",
    }.get(freshness)


def _bing_freshness_filter(freshness: str) -> Optional[str]:
    return {
        "oneYear": 'ex1:"ez5"',
        "oneMonth": 'ex1:"ez3"',
        "oneWeek": 'ex1:"ez2"',
        "oneDay": 'ex1:"ez1"',
    }.get(freshness)


def _normalize_bing_result_url(url: str) -> str:
    value = unescape(str(url or "")).strip()
    if not value:
        return ""
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    encoded = query.get("u", [""])[0]
    if encoded:
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        try:
            padding = "=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8", errors="replace")
            if decoded.startswith(("http://", "https://")):
                return decoded
        except Exception:
            pass
    if value.startswith("/"):
        return ""
    return value


def _tavily_time_range(freshness: str) -> Optional[str]:
    return {
        "oneYear": "year",
        "oneMonth": "month",
        "oneWeek": "week",
        "oneDay": "day",
    }.get(freshness)


def _brave_freshness(freshness: str) -> Optional[str]:
    return {
        "oneYear": "py",
        "oneMonth": "pm",
        "oneWeek": "pw",
        "oneDay": "pd",
    }.get(freshness)


def _searxng_time_range(freshness: str) -> Optional[str]:
    return {
        "oneYear": "year",
        "oneMonth": "month",
        "oneWeek": "week",
        "oneDay": "day",
    }.get(freshness)


def _json_payload_from_adapter(payload: Union[dict, str], error_message: str) -> dict:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError as error:
            raise ToolProviderError(error_message) from error
    raise ToolProviderError(error_message)


def _safe_proxy_host(proxy_url: str) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE) else f"http://{value}")
    host = parsed.hostname or ""
    if not host:
        return ""
    return f"{host}:{parsed.port}" if parsed.port else host


def _proxy_diagnostics(proxy_url: str) -> dict[str, object]:
    host = _safe_proxy_host(proxy_url)
    return {
        "proxyUsed": bool(host),
        **({"proxyHost": host} if host else {}),
    }


def _provider_chain_names(provider_chain: str) -> list[str]:
    names = [item.strip() for item in str(provider_chain or "").split(",") if item.strip()]
    return names or ["anysearch", "bing", "duckduckgo", "ddgs", "baidu", "multi-free", "cheetah-ddg"]


def web_search_provider_config_diagnostics(provider_chain: Optional[str] = None) -> dict:
    settings = get_settings()
    chain = _provider_chain_names(provider_chain or settings.web_search_provider_chain)
    providers = []
    for name in chain:
        provider_name = _provider_result_name(name)
        aliases = _web_search_env_aliases(provider_name)
        proxy_url = ""
        endpoint_host = ""
        if provider_name in {"cheetah-duckduckgo-html-search", "duckduckgo-html-search"}:
            proxy_url = settings.duckduckgo_proxy_url or settings.web_search_proxy_url
            if provider_name == "cheetah-duckduckgo-html-search":
                endpoint_host = urlparse(CHEETAH_DUCKDUCKGO_HTML_SEARCH_URL).netloc
        elif provider_name == "baidu-html-search":
            proxy_url = settings.baidu_proxy_url
        elif provider_name == "bing-html-search":
            proxy_url = settings.bing_proxy_url or settings.web_search_proxy_url
            endpoint_host = urlparse(BING_HTML_SEARCH_URL).netloc
        elif provider_name == "searxng":
            endpoint_host = urlparse(settings.searxng_base_url).netloc
        elif provider_name == "anysearch":
            endpoint_host = urlparse(ANYSEARCH_WEB_SEARCH_URL).netloc
        elif provider_name == "ddgs":
            endpoint_host = urlparse(settings.ddgs_api_base_url).netloc
            package_available = _ddgs_package_available()
            cli_available = bool(_ddgs_cli_path())
        providers.append(
            {
                "providerName": provider_name,
                "configured": _provider_configured(provider_name, settings),
                "envAliases": {alias: _env_status(alias) for alias in aliases},
                **_proxy_diagnostics(proxy_url),
                **({"endpointHost": endpoint_host} if endpoint_host else {}),
                **(
                    {"packageAvailable": package_available, "cliAvailable": cli_available}
                    if provider_name == "ddgs"
                    else {}
                ),
            }
        )
    return {
        "providerMode": settings.web_search_provider_mode,
        "providerChain": chain,
        "providers": providers,
    }


def _web_search_env_aliases(provider_name: str) -> list[str]:
    if provider_name == "bocha-web-search":
        return ["BOCHA_API_KEY", "WEB_SEARCH_API_KEY", "SEARCH_PROVIDER_KEY"]
    if provider_name == "tavily":
        return ["TAVILY_API_KEY"]
    if provider_name == "anysearch":
        return ["ANYSEARCH_API_KEY", "ANYSEARCH_DOMAIN", "ANYSEARCH_TAG", "ANYSEARCH_ZONE", "ANYSEARCH_LANGUAGE"]
    if provider_name == "brave-web-search":
        return ["BRAVE_SEARCH_API_KEY"]
    if provider_name == "searxng":
        return ["SEARXNG_BASE_URL"]
    if provider_name == "ddgs":
        return ["DDGS_API_BASE_URL"]
    if provider_name == "google-cse":
        return ["GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX"]
    if provider_name == "bing-html-search":
        return ["BING_PROXY_URL", "WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY"]
    if provider_name in {"cheetah-duckduckgo-html-search", "duckduckgo-html-search"}:
        return ["DUCKDUCKGO_PROXY_URL", "DUCKDUCKGO_HTTP_PROXY", "WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY"]
    if provider_name == "baidu-html-search":
        return ["BAIDU_PROXY_URL", "BAIDU_HTTP_PROXY", "WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY"]
    return []


def _provider_configured(provider_name: str, settings) -> bool:
    if provider_name == "bocha-web-search":
        return bool(settings.bocha_api_key)
    if provider_name == "tavily":
        return bool(settings.tavily_api_key)
    if provider_name == "anysearch":
        return True
    if provider_name == "brave-web-search":
        return bool(settings.brave_search_api_key)
    if provider_name == "searxng":
        return bool(settings.searxng_base_url)
    if provider_name == "ddgs":
        return bool(settings.ddgs_api_base_url or _ddgs_package_available() or _ddgs_cli_path())
    if provider_name == "google-cse":
        return bool(settings.google_cse_api_key and settings.google_cse_cx)
    return provider_name in {
        "bing-html-search",
        "multi-free-search",
        "baidu-html-search",
        "duckduckgo-html-search",
        "cheetah-duckduckgo-html-search",
    }


def _env_status(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        return "missing"
    if not str(value).strip():
        return "empty"
    return "present"


def _ddgs_package_available() -> bool:
    try:
        from ddgs import DDGS  # noqa: F401
    except Exception:
        return False
    return True


def _ddgs_cli_path() -> str:
    return shutil.which("ddgs") or ""


def _provider_missing_config(provider: object) -> str:
    checker = getattr(provider, "missing_config_reason", None)
    if not callable(checker):
        return ""
    try:
        return str(checker() or "")
    except Exception:
        return ""


def _safe_provider_error(
    error: Exception,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    text = str(error)
    for value in sensitive_values:
        if value:
            text = text.replace(value, "[query-redacted]")
    text = re.sub(
        r"(?i)\b(?:https?|socks5h?|ftp)://[^\s]+",
        "[url-redacted]?<query-redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\b(key|token|access_token|refresh_token|secret|api_key|x-subscription-token)=([^&\s]+)",
        "credential=[redacted]",
        text,
    )
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(X-Subscription-Token:\s*)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    return text[:500]


def _provider_failure_reason(error: Exception) -> str:
    if isinstance(error, WebSearchAttemptError):
        return error.reason_code
    message = str(error).lower()
    if isinstance(error, ProviderConfigMissingError) or "not configured" in message or "未配置" in message:
        return "skipped_missing_config"
    if isinstance(error, TimeoutError):
        return "timeout"
    if "blocked_or_captcha" in message or "captcha" in message or "安全验证" in message or "验证码" in message:
        return "blocked_or_captcha"
    if "ddgs_api_unreachable" in message:
        return "ddgs_api_unreachable"
    if "ddgs_package_timeout_unsupported" in message:
        return "ddgs_package_timeout_unsupported"
    if "auth_failed" in message or "http 401" in message or "http 403" in message:
        return "auth_failed"
    if "quota_exceeded" in message or "quota" in message or "额度" in message or "配额" in message:
        return "quota_exceeded"
    if "rate_limited" in message or "http 429" in message or "too many requests" in message or "限流" in message:
        return "rate_limited"
    if "timeout" in message or "timed out" in message or "超时" in message:
        return "timeout"
    if "no usable" in message or "no results" in message or "无可用" in message:
        return "no_usable_results"
    if "invalid_response" in message or "invalid json" in message or "无法解析" in message:
        return "invalid_response"
    if "transport_error" in message:
        return "transport_error"
    if "raw_empty" in message:
        return "raw_empty"
    if "filtered_empty" in message:
        return "filtered_empty"
    if "http " in message:
        return "http_error"
    return "provider_error"


def _should_open_web_search_circuit(provider_name: str, reason: str) -> bool:
    if provider_name == "multi-free-search":
        return False
    return reason in {"timeout", "auth_failed", "rate_limited", "quota_exceeded", "blocked_or_captcha"}


def _http_failure_reason(status_code: int) -> str:
    if status_code in {401, 403}:
        return "auth_failed"
    if status_code == 429:
        return "rate_limited"
    return "http_error"


def _bocha_failure_reason_from_payload(payload: dict) -> str:
    text = " ".join(str(payload.get(key) or "") for key in ("code", "msg", "message", "error", "errorCode")).lower()
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text or "auth" in text:
        return "auth_failed"
    if "429" in text or "rate" in text or "too many" in text or "频率" in text or "限流" in text:
        return "rate_limited"
    if "quota" in text or "额度" in text or "配额" in text or "insufficient" in text:
        return "quota_exceeded"
    return "http_error"


def _credible_result_count(results: list[WebSearchItem]) -> int:
    return sum(1 for item in results if item.credibility_rank in {"official", "ota_aggregator", "map", "guide"})


def _first_configured_provider_name(diagnostics: list[dict]) -> str:
    for item in diagnostics:
        if item.get("status") in {"success", "failed", "no_results"}:
            return str(item.get("providerName") or "")
    return ""


def _web_search_cache_ttl_seconds(freshness: str) -> int:
    if freshness in {"oneDay", "oneWeek"}:
        return 15 * 60
    return 60 * 60


def _score_confidence(score: object, index: int, result_count: int) -> float:
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric > 1:
        numeric = numeric / 100
    return max(0.25, min(0.92, 0.45 + numeric * 0.35 + min(result_count, 6) * 0.02 - max(index - 1, 0) * 0.015))


def _provider_result_name(provider_name: str) -> str:
    provider = provider_name.lower()
    if provider in {"multi", "multi-free", "multi-free-search", "free", "free-search", "html-multi"}:
        return "multi-free-search"
    if provider in {"bocha", "bocha-web-search", "bochaai"}:
        return "bocha-web-search"
    if provider == "tavily":
        return "tavily"
    if provider in {"anysearch", "any-search"}:
        return "anysearch"
    if provider in {"brave", "brave-web-search"}:
        return "brave-web-search"
    if provider == "searxng":
        return "searxng"
    if provider in {"ddgs", "duckduckgo-search"}:
        return "ddgs"
    if provider in {"google", "google-cse", "google-programmable-search"}:
        return "google-cse"
    if provider in {
        "cheetah-ddg",
        "cheetah-duckduckgo",
        "cheetah-duckduckgo-html",
        "cheetah-duckduckgo-html-search",
    }:
        return "cheetah-duckduckgo-html-search"
    if provider in {"duckduckgo", "duckduckgo-html", "duckduckgo-html-search", "ddg"}:
        return "duckduckgo-html-search"
    if provider in {"duckduckgo-lite", "duckduckgo-lite-search", "ddg-lite"}:
        return "duckduckgo-lite-search"
    if provider in {"baidu", "baidu-html", "baidu-html-search"}:
        return "baidu-html-search"
    if provider in {"bing", "bing-html", "bing-html-search"}:
        return "bing-html-search"
    return provider_name or "web-search-provider"


def _clean_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _source_name(url: str) -> str:
    host = urlparse(url).netloc.strip()
    return host or "公开搜索结果"


def _title_from_url(url: str) -> str:
    host = _source_name(url)
    parsed = urlparse(url)
    path = unquote(parsed.path or "").strip("/")
    if not path:
        return host
    return f"{host} / {path[:48]}"


def _extract_baidu_public_urls(html: str, limit: int) -> list[dict[str, str]]:
    normalized = unescape(html).replace("\\/", "/")
    urls: list[dict[str, str]] = []
    seen: set[str] = set()
    parser = _BaiduHTMLSearchParser()
    parser.feed(normalized)
    parser.close()
    for item in parser.results:
        url = _clean_baidu_url(item.get("url", ""))
        if not url or url in seen or not _is_public_search_url(url):
            continue
        seen.add(url)
        urls.append(
            {
                "url": url,
                "title": _clean_text(item.get("title", "")) or _title_from_url(url),
                "snippet": _clean_text(item.get("snippet", "")) or "来自百度公开搜索结果页提取的来源链接。",
            }
        )
        if len(urls) >= max(limit * 8, 30):
            break
    for match in re.finditer(r"https?://[^\\\"'<>\s]+", normalized):
        url = _clean_baidu_url(match.group(0))
        if not url or url in seen or not _is_public_search_url(url):
            continue
        seen.add(url)
        urls.append(
            {
                "url": url,
                "title": _title_from_url(url),
                "snippet": "来自百度公开搜索结果页提取的外部来源链接。",
            }
        )
        if len(urls) >= max(limit * 8, 30):
            break
    return sorted(
        urls, key=lambda item: _search_url_priority(item["url"], item.get("title", ""), item.get("snippet", ""))
    )[:limit]


def _normalize_baidu_result_url(value: str) -> str:
    raw = unquote(unescape(str(value or ""))).replace("\\/", "/").strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if raw.startswith("/link"):
        raw = f"https://www.baidu.com{raw}"
    url = _clean_baidu_url(raw)
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.netloc and parsed.path.startswith("/link"):
        url = f"https://www.baidu.com{url}"
        parsed = urlparse(url)
    if parsed.netloc.lower().endswith("baidu.com") and parsed.path.startswith("/link"):
        query = parse_qs(parsed.query)
        embedded = query.get("url", [""])[0]
        if embedded.startswith(("http://", "https://")):
            return _clean_baidu_url(embedded)
        return url
    return url


def _url_from_baidu_data_click(value: str) -> str:
    if not value:
        return ""
    text = unescape(value).replace("&quot;", '"')
    for pattern in [
        r'"(?:url|mu)"\s*:\s*"([^"]+)"',
        r"'(?:url|mu)'\s*:\s*'([^']+)'",
        r"(https?://[^'\"\s{}]+)",
    ]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _looks_like_baidu_blocked(html: str) -> bool:
    text = _clean_text(unescape(str(html or "")))
    return any(token in text for token in ("百度安全验证", "请输入验证码", "安全验证", "网络不给力"))


def _clean_baidu_url(url: str) -> str:
    url = url.strip().rstrip(").,;，。；")
    if "\\u" in url:
        try:
            url = url.encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _is_public_search_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return False
    if host.endswith("baidu.com") and parsed.path.startswith("/link"):
        return True
    blocked_tokens = (
        "baidu.com",
        "bdstatic.com",
        "bdimg.com",
        "bcebos.com",
        "baidustatic.com",
        "hao123.com",
        "w3.org",
        "redirect.simba.taobao.com",
        "uland.taobao.com",
        "google.com",
        "gstatic.com",
    )
    if any(token in host for token in blocked_tokens):
        return False
    ad_tokens = ("unionsem", "utm_", "adid=", "clickid=", "refpid=")
    if any(token in url.lower() for token in ad_tokens):
        return False
    path = parsed.path.lower()
    if path.endswith((".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2")):
        return False
    return True


def _search_url_priority(url: str, title: str = "", snippet: str = "") -> tuple[int, int, str]:
    rank = _credibility_rank(url, title, snippet)
    rank_score = {
        "official": 0,
        "ota_aggregator": 1,
        "map": 2,
        "guide": 3,
        "search": 4,
        "unknown": 5,
    }.get(rank, 5)
    host = urlparse(url).netloc.lower()
    depth = len([part for part in urlparse(url).path.split("/") if part])
    return (rank_score, depth, host)


def _search_result_rejection_reason(
    query: str,
    item: WebSearchItem,
    freshness: str = "noLimit",
) -> str:
    if not item.url or not _is_public_search_url(item.url):
        return "non_public_url"
    if _is_stale_search_result(item, freshness):
        return "stale"
    if _reject_nonofficial_stale_for_query_year(query, item):
        return "query_year_stale"
    if not _search_result_matches_query(query, item):
        return "query_mismatch"
    return ""


def _validated_ranked_search_results(
    query: str, results: list[WebSearchItem], freshness: str = "noLimit"
) -> list[WebSearchItem]:
    deduped: dict[str, WebSearchItem] = {}
    for item in results:
        if _search_result_rejection_reason(query, item, freshness):
            continue
        key = _canonical_search_url(item.url)
        adjusted = _with_adjusted_search_confidence(item, query)
        existing = deduped.get(key)
        if existing is None or adjusted.confidence > existing.confidence:
            deduped[key] = adjusted
    return sorted(
        deduped.values(),
        key=lambda item: (
            -item.confidence,
            _search_url_priority(item.url, item.title, item.snippet)[0],
            _search_url_priority(item.url, item.title, item.snippet)[1],
            item.source_name,
        ),
    )


def _canonical_search_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _with_adjusted_search_confidence(item: WebSearchItem, query: str) -> WebSearchItem:
    rank_bonus = {
        "official": 0.12,
        "ota_aggregator": 0.07,
        "map": 0.05,
        "guide": 0.02,
        "search": 0.0,
        "unknown": -0.08,
    }.get(item.credibility_rank, -0.08)
    provider_bonus = {
        "bocha-web-search": 0.04,
        "tavily": 0.04,
        "brave-web-search": 0.04,
        "searxng": 0.03,
        "google-cse": 0.03,
        "bing-html-search": 0.02,
        "cheetah-duckduckgo-html-search": 0.02,
        "baidu-html-search": 0.02,
        "duckduckgo-html-search": 0.02,
        "duckduckgo-lite-search": 0.02,
    }.get(item.provider_name, 0.0)
    relevance_bonus = 0.04 if _search_result_matches_query(query, item) else -0.05
    recency_bonus = 0.03 if _extract_result_dates(f"{item.title} {item.snippet}") else 0.0
    item.confidence = max(
        0.1, min(0.96, item.confidence + rank_bonus + provider_bonus + relevance_bonus + recency_bonus)
    )
    return item


_TRAVEL_GUIDE_QUERY_MARKERS = ("游玩攻略", "游客攻略", "旅游攻略", "旅行攻略")
_TRAVEL_GUIDE_ADVICE_SIGNALS = (
    "旅游",
    "旅行",
    "攻略",
    "指南",
    "行程",
    "游玩",
    "游览",
    "游客",
    "出行",
    "路线",
    "避坑",
    "打卡",
    "参观",
    "访客",
    "入校",
    "预约",
    "散步",
    "推荐",
    "探店",
    "必吃",
)
_TRAVEL_GUIDE_THEME_GROUPS = (
    (("高校参观", "高校", "大学", "校园"), ("高校", "大学", "校园", "访客", "入校")),
    (("当地美食", "美食", "餐厅", "餐饮", "小吃"), ("美食", "餐厅", "餐饮", "小吃", "午餐", "晚餐")),
    (("城市公园", "公园", "绿地", "园林"), ("公园", "绿地", "园林")),
    (("城市夜景", "夜景", "夜游"), ("夜景", "夜游", "夜晚")),
    (("博物馆", "博物院", "展览"), ("博物馆", "博物院", "展览")),
    (("城市地标", "地标", "景点"), ("地标", "景点", "建筑")),
    (("休闲休息", "休闲", "放松"), ("休闲", "休息", "放松")),
    (("城市漫步", "漫步", "街区"), ("漫步", "步行", "街区", "街巷")),
    (("本地人文", "人文体验", "人文", "文化"), ("人文", "文化", "历史", "民俗")),
    (("本地购物", "购物", "商场", "市集"), ("购物", "商场", "市集")),
    (("景点游览", "景点", "景区"), ("景点", "景区", "游览")),
)


def _travel_guide_result_matches_query(query: str, item: WebSearchItem) -> bool:
    lowered_query = query.lower()
    haystack = f"{item.title} {item.snippet} {item.summary}".lower()
    city_tokens = ("北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "西安", "重庆", "武汉", "苏州")
    requested_cities = [city for city in city_tokens if city in lowered_query]
    if requested_cities and not any(city in haystack for city in requested_cities):
        return False
    requested_theme_groups = [
        result_aliases
        for query_aliases, result_aliases in _TRAVEL_GUIDE_THEME_GROUPS
        if any(alias in lowered_query for alias in query_aliases)
    ]
    if requested_theme_groups and not any(
        any(alias in haystack for alias in result_aliases)
        for result_aliases in requested_theme_groups
    ):
        return False
    return any(signal in haystack for signal in _TRAVEL_GUIDE_ADVICE_SIGNALS)


def _search_result_matches_query(query: str, item: WebSearchItem) -> bool:
    if any(marker in query.lower() for marker in _TRAVEL_GUIDE_QUERY_MARKERS):
        return _travel_guide_result_matches_query(query, item)
    tokens = [token for token in re.split(r"[\s,，。/|]+", query) if len(token) >= 2]
    if not tokens:
        return True
    haystack = f"{item.title} {item.snippet} {item.source_name} {item.url}".lower()
    lowered_tokens = [token.lower() for token in tokens]
    semantic_tokens = {
        "官方",
        "官网",
        "公告",
        "预约",
        "开放",
        "限流",
        "闭园",
        "施工",
        "文旅",
        "交通管制",
        "安全提示",
        "门票",
        "购票",
        "国庆",
        "十一",
        "黄金周",
        "天气",
        "拥挤",
        "人流",
    }
    city_tokens = {"北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "西安", "重庆", "武汉", "苏州"}
    generic_query_tokens = {
        "城市",
        "访客",
        "游客",
        "校园",
        "校区",
        "时间",
        "规则",
        "信息",
        "查询",
        "入口",
        "网页",
        "页面",
    }
    year_tokens = {token for token in lowered_tokens if re.fullmatch(r"20\d{2}", token)}
    poi_tokens: list[str] = []
    requested_context_tokens: list[str] = []
    for token in lowered_tokens:
        if token in city_tokens:
            requested_context_tokens.append(token)
            continue
        if token in year_tokens or re.fullmatch(r"\d+月\d+日", token):
            continue
        if token.startswith("site:") or token.startswith("inurl:") or ":" in token:
            continue
        stripped = token
        for semantic in sorted(semantic_tokens, key=len, reverse=True):
            if semantic in token:
                requested_context_tokens.append(semantic)
                stripped = stripped.replace(semantic, "")
        stripped = stripped.strip("-_（）()[]【】")
        if stripped and stripped not in city_tokens and stripped not in generic_query_tokens and len(stripped) >= 2:
            poi_tokens.append(stripped)
    core_tokens = [
        token
        for token in _core_query_tokens(poi_tokens[:3])
        if token not in city_tokens and token not in generic_query_tokens
    ]
    has_poi_match = not core_tokens or any(token and token in haystack for token in core_tokens)
    has_context_match = not requested_context_tokens or any(token in haystack for token in requested_context_tokens)
    if core_tokens:
        return has_poi_match
    return has_context_match


def _core_query_tokens(tokens: list[str]) -> list[str]:
    core: list[str] = []
    suffixes = ("博物院", "博物馆", "公园", "景区", "大学", "学院", "广场", "官网", "官方")
    for token in tokens:
        normalized = token
        for suffix in suffixes:
            normalized = normalized.replace(suffix, "")
        core.append(token)
        if len(normalized) >= 2:
            core.append(normalized)
    return list(dict.fromkeys(core))


def _reject_nonofficial_stale_for_query_year(query: str, item: WebSearchItem) -> bool:
    target_years = [int(match.group(1)) for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", query)]
    if not target_years:
        return False
    target_year = max(target_years)
    item_years = [value.year for value in _extract_result_dates(f"{item.title} {item.snippet}")]
    if not item_years:
        return False
    return max(item_years) < target_year and item.credibility_rank != "official"


def _is_stale_search_result(item: WebSearchItem, freshness: str) -> bool:
    if freshness == "noLimit":
        return False
    text = f"{item.title} {item.snippet}"
    dates = _extract_result_dates(text)
    if not dates:
        return False
    latest = max(dates)
    max_age_days = {
        "oneDay": 1,
        "oneWeek": 7,
        "oneMonth": 31,
        "oneYear": 366,
    }.get(freshness, MAX_SEARCH_RESULT_AGE_DAYS)
    return (date.today() - latest).days > max_age_days


def _extract_result_dates(text: str) -> list[date]:
    dates: list[date] = []
    for match in re.finditer(r"(20\d{2})[-/.年](\d{1,2})(?:[-/.月](\d{1,2})日?)?", text):
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3) or "1")
        try:
            dates.append(date(year, month, day))
        except ValueError:
            continue
    for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", text):
        year = int(match.group(1))
        try:
            dates.append(date(year, 1, 1))
        except ValueError:
            continue
    return dates


def _credibility_rank(url: str, title: str = "", snippet: str = "") -> str:
    del title, snippet
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    official_suffixes = (".gov.cn", ".edu.cn", ".dpm.org.cn")
    official_exact_hosts = {"gov.cn", "edu.cn", "dpm.org.cn"}
    if host in official_exact_hosts or host.endswith(official_suffixes):
        return "official"
    ota_suffixes = (
        "ctrip.com",
        "trip.com",
        "meituan.com",
        "fliggy.com",
        "qunar.com",
        "dianping.com",
    )
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ota_suffixes):
        return "ota_aggregator"
    guide_suffixes = ("mafengwo.cn", "mafengwo.com.cn", "xiaohongshu.com")
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in guide_suffixes):
        return "guide"
    if host:
        return "search"
    return "unknown"


def _web_search_item_confidence(item: dict, index: int, result_count: int) -> float:
    score = 0.42
    if item.get("url"):
        score += 0.14
    if item.get("name") or item.get("title"):
        score += 0.1
    if item.get("summary") or item.get("snippet"):
        score += 0.1
    if item.get("siteName"):
        score += 0.06
    score += min(result_count, 6) * 0.02
    score -= max(index - 1, 0) * 0.015
    return max(0.25, min(score, 0.9))


def _select_weather_cast(casts: list[dict], travel_date: Optional[str]) -> dict:
    if travel_date:
        for item in casts:
            if str(item.get("date") or "") == travel_date:
                return item
        raise ToolProviderError(f"高德天气预报未覆盖旅行日期 {travel_date}")
    return casts[0]


def _weather_response_from_cast(
    city: str,
    forecast: dict,
    selected: dict,
    purpose_tags: list[str],
    context: dict,
    fallback_used: bool,
) -> AmapWeatherResponse:
    day_weather = str(selected.get("dayweather") or "")
    night_weather = str(selected.get("nightweather") or "")
    day_temp = str(selected.get("daytemp") or "")
    night_temp = str(selected.get("nighttemp") or "")
    wind = " / ".join(item for item in [selected.get("daywind"), selected.get("nightwind")] if item) or "待查询"
    weather = (
        f"{day_weather}转{night_weather}"
        if night_weather and night_weather != day_weather
        else day_weather or night_weather or "待查询"
    )
    risk_level, risk_reason = _classify_weather(weather, purpose_tags, context)
    return AmapWeatherResponse(
        city=str(forecast.get("city") or city),
        date=str(selected.get("date") or date.today().isoformat()),
        weather=weather,
        temperature_range=f"{night_temp}-{day_temp}°C" if night_temp or day_temp else "待查询",
        wind=wind,
        humidity=str(selected.get("humidity")) if selected.get("humidity") is not None else None,
        risk_level=risk_level,
        risk_reason=risk_reason,
        source_name="高德天气",
        confidence=0.72 if not fallback_used else 0.5,
        provider_name="amap-weather-provider" if not fallback_used else "mock-amap-weather-provider",
        fallback_used=fallback_used,
        user_visible_caveat="天气服务当前返回日级预报，逐小时预报待接入；提醒会按旅行目的保守判断。",
        raw=forecast,
    )


def _classify_weather(weather_text: str, purpose_tags: list[str], context: dict) -> tuple[str, str]:
    tags = [str(tag) for tag in purpose_tags]
    for key in ("tripPurpose", "preferenceSummary", "weatherSensitivity"):
        if context.get(key):
            tags.append(str(context[key]))
    has_bad_weather = any(keyword in weather_text for keyword in BAD_WEATHER_KEYWORDS)
    purpose_sensitive = any(keyword in tag for tag in tags for keyword in WEATHER_PURPOSE_TAGS)
    weather_sensitive = any(keyword in " ".join(tags) for keyword in ("天气敏感", "怕雨", "高", "老人", "亲子", "儿童"))
    time_suffix = _time_window_suffix(context)
    if has_bad_weather and purpose_sensitive:
        sensitive_note = "用户对天气敏感，" if weather_sensitive else ""
        return (
            "risky",
            f"{sensitive_note}天气可能影响拍照、徒步、亲子、老人同行、户外排队或步行强度{time_suffix}，请预留室内替代方案。",
        )
    if has_bad_weather and weather_sensitive:
        return "risky", f"用户对天气敏感，当前天气可能影响体验{time_suffix}，请预留室内替代方案并关注交通衔接。"
    if has_bad_weather:
        return "risky", f"天气存在降雨、强风、雾霾或其他不利因素{time_suffix}，请关注出行安全和交通衔接。"
    if purpose_sensitive:
        return "ideal", "天气对当前旅行目的影响较低，提醒频率可降低。"
    return "neutral", "天气对本次行程影响较低。"


def _time_window_suffix(context: dict) -> str:
    windows = context.get("travelTimeWindows")
    if not isinstance(windows, list):
        return ""
    labels: list[str] = []
    for window in windows[:4]:
        if not isinstance(window, dict):
            continue
        start_time = str(window.get("startTime") or "").strip()
        end_time = str(window.get("endTime") or "").strip()
        if start_time and end_time:
            labels.append(f"{start_time}-{end_time}")
    if not labels:
        return ""
    return f"（影响行程时段：{'、'.join(labels)}）"

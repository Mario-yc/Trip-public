import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from src.core.config import get_settings
from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.models.poi_risk_alert import POIRiskAlert
from src.models.route_option import RouteOption
from src.models.ticket_lookup_result import TicketLookupResult
from src.models.traffic_crowding_signal import TrafficCrowdingSignal
from src.models.weather_signal import WeatherSignal
from src.providers.travel_tools import ResilientWebSearchProvider, WebSearchProvider
from src.services.risk_query_builder import RiskQueryBuilder, RiskSourceScorer


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
SEARCH_SOURCE_NAME = "搜索结果"


class POIRiskSearchError(RuntimeError):
    pass


class AgentRiskSynthesisError(RuntimeError):
    pass


@dataclass
class RiskSearchBudget:
    max_queries: int = 4
    max_provider_attempts: int = 2
    max_seconds: float = 12.0
    max_syntheses: int = 1
    started_at: float = 0.0
    query_count: int = 0
    provider_attempt_count: int = 0
    synthesis_count: int = 0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()

    @property
    def deadline_at(self) -> float:
        return self.started_at + max(0.0, self.max_seconds)

    def can_search(self) -> bool:
        return (
            self.query_count < self.max_queries
            and self.provider_attempt_count < self.max_provider_attempts
            and time.monotonic() < self.deadline_at
        )

    def record_search(self, provider_attempts: int) -> None:
        self.query_count += 1
        self.provider_attempt_count += max(1, int(provider_attempts or 0))

    def can_synthesize(self) -> bool:
        return self.synthesis_count < self.max_syntheses and time.monotonic() < self.deadline_at


class DeepSeekRiskSynthesisProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        http_post: Optional[Callable[[str, dict, float], dict]] = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.deepseek_api_key
        self.model = model or settings.deepseek_model
        self.timeout_seconds = timeout_seconds or settings.deepseek_timeout_seconds
        self.http_post = http_post

    def synthesize(self, payload: dict) -> dict:
        if not self.api_key:
            raise AgentRiskSynthesisError("Agent 风险判断服务未配置。")
        response = self._post(self._chat_payload(payload))
        try:
            content = response["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise AgentRiskSynthesisError("Agent 风险判断服务返回了无法解析的数据。") from error
        summary = str(parsed.get("summary") or "").strip()
        if not summary:
            raise AgentRiskSynthesisError("Agent 风险判断服务未返回有效摘要。")
        confidence = parsed.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "summary": summary,
            "confidence": max(0.0, min(confidence, 1.0)),
            "userVisibleCaveat": str(parsed.get("userVisibleCaveat") or "").strip(),
        }

    def _chat_payload(self, payload: dict) -> dict:
        system_prompt = (
            "你是旅行风险判断 Agent。只基于用户行程上下文和公开搜索来源判断风险，"
            "不要编造未在来源或上下文中出现的信息。返回且只返回 JSON："
            "{\"summary\": string, \"confidence\": number, \"userVisibleCaveat\": string}。"
            "summary 用中文，说明风险、影响对象和建议动作；没有充分证据时明确说判断不完整。"
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

    def _post(self, payload: dict) -> dict:
        if self.http_post is not None:
            return self.http_post(DEEPSEEK_CHAT_COMPLETIONS_URL, payload, self.timeout_seconds)
        request = Request(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise AgentRiskSynthesisError(f"Agent 风险判断服务请求失败：HTTP {error.code}") from error
        except URLError as error:
            raise AgentRiskSynthesisError(f"Agent 风险判断服务请求失败：{error.reason}") from error
        except TimeoutError as error:
            raise AgentRiskSynthesisError("Agent 风险判断服务请求超时。") from error
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise AgentRiskSynthesisError("Agent 风险判断服务返回了无法解析的数据。") from error


class POIRiskService:
    def __init__(
        self,
        search_provider_key: Optional[str] = None,
        timeout_seconds: float = 5.0,
        http_get: Optional[Callable[[str, float], dict]] = None,
        risk_agent_provider: Optional[Any] = None,
        web_search_provider: Optional[ResilientWebSearchProvider] = None,
    ):
        settings = get_settings()
        self.search_provider_key = search_provider_key if search_provider_key is not None else settings.search_provider_key
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get
        self.risk_agent_provider = risk_agent_provider or DeepSeekRiskSynthesisProvider()
        self.web_search_provider = web_search_provider or (
            ResilientWebSearchProvider(
                default_provider=WebSearchProvider(
                    api_key=self.search_provider_key,
                    timeout_seconds=self.timeout_seconds,
                    http_get=self._http_get_with_headers if self.http_get is not None else None,
                )
            )
            if self.http_get is not None or search_provider_key is not None
            else ResilientWebSearchProvider()
        )
        self.query_builder = RiskQueryBuilder()
        self.source_scorer = RiskSourceScorer()

    def build_alerts(
        self,
        plan_id: str,
        city: str,
        pois: list[POI],
        segments: list[ItinerarySegment],
        weather: WeatherSignal,
        traffic_signals: list[TrafficCrowdingSignal],
        ticket_results: list[TicketLookupResult],
        route_options: Optional[list[RouteOption]] = None,
        risk_context: Optional[dict[str, Any]] = None,
    ) -> list[POIRiskAlert]:
        context = risk_context or {}
        budget = RiskSearchBudget(
            max_queries=max(1, int(context.get("maxRiskSearchQueries") or context.get("riskSearchBudget") or 8)),
            max_provider_attempts=max(1, int(context.get("maxRiskProviderAttempts") or 8)),
            max_seconds=max(0.1, float(context.get("maxRiskSearchSeconds") or 12.0)),
            max_syntheses=max(0, int(context.get("maxRiskSynthesisCalls") if context.get("maxRiskSynthesisCalls") is not None else 1)),
        )
        chain = getattr(self.web_search_provider, "chain_provider", None)
        if chain is not None and hasattr(chain, "max_provider_attempts"):
            chain.max_provider_attempts = min(int(chain.max_provider_attempts), budget.max_provider_attempts)
        pois_by_id = {poi.id: poi for poi in pois}
        alerts = []
        seen_queries: set[str] = set()
        for segment in segments:
            poi = pois_by_id.get(segment.poi_id)
            if poi is None:
                continue
            alert = self._build_segment_alert(
                plan_id,
                city,
                poi,
                segment,
                weather,
                traffic_signals,
                ticket_results,
                route_options or [],
                risk_context or {},
                seen_queries,
                budget,
            )
            if alert is not None:
                alerts.append(alert)
        return alerts

    def _build_segment_alert(
        self,
        plan_id: str,
        city: str,
        poi: POI,
        segment: ItinerarySegment,
        weather: WeatherSignal,
        traffic_signals: list[TrafficCrowdingSignal],
        ticket_results: list[TicketLookupResult],
        route_options: list[RouteOption],
        risk_context: dict[str, Any],
        seen_queries: Optional[set[str]] = None,
        budget: Optional[RiskSearchBudget] = None,
    ) -> Optional[POIRiskAlert]:
        risk_query = self.query_builder.build(
            city=city,
            poi_name=poi.name,
            segment_date=risk_context.get("segmentDate") or risk_context.get("travelDate"),
            segment_start_time=segment.start_time,
            segment_end_time=segment.end_time,
            risk_context=risk_context,
            poi_category=poi.category,
        )
        query_plan = self._query_plan(risk_query)
        if seen_queries is not None:
            query_plan = [query for query in query_plan if query not in seen_queries]
        if not query_plan:
            return None
        try:
            max_results = int(risk_context.get("maxRiskSearchResults") or 5)
        except (TypeError, ValueError):
            max_results = 5
        evaluations: list[dict[str, Any]] = []
        for candidate_query in query_plan:
            if budget is not None and not budget.can_search():
                break
            search_result = self.web_search_provider.search(candidate_query, count=max(1, min(max_results, 5)), freshness=risk_query.freshness)
            if budget is not None:
                budget.record_search(len(getattr(search_result, "attempted_providers", []) or []))
            scored_items = [
                (
                    item,
                    self.source_scorer.score(
                        item,
                        risk_query.target_year,
                        bool(risk_context.get("officialSourceFirst", True)),
                        city=city,
                        poi_name=poi.name,
                    ),
                )
                for item in search_result.results
            ]
            accepted_items = [(item, score) for item, score in scored_items if score["accepted"]]
            stale_count = len([score for _item, score in scored_items if score["targetDateRelevance"] == "stale_for_target_date"])
            official_count = len([score for _item, score in scored_items if score["credibilityRank"] == "official"])
            risk_status_reason = self._risk_status_reason(search_result, scored_items, accepted_items, stale_count)
            evaluations.append(
                {
                    "query": candidate_query,
                    "searchResult": search_result,
                    "scoredItems": scored_items,
                    "acceptedItems": accepted_items,
                    "staleCount": stale_count,
                    "officialCount": official_count,
                    "riskStatusReason": risk_status_reason,
                }
            )
            if seen_queries is not None:
                seen_queries.add(candidate_query)
            if accepted_items:
                break
        selected_evaluation = self._select_search_evaluation(evaluations)
        if selected_evaluation is None:
            return None
        query = selected_evaluation["query"]
        search_result = selected_evaluation["searchResult"]
        scored_items = selected_evaluation["scoredItems"]
        accepted_items = selected_evaluation["acceptedItems"]
        stale_count = selected_evaluation["staleCount"]
        official_count = selected_evaluation["officialCount"]
        risk_status_reason = selected_evaluation["riskStatusReason"]
        query_attempts = self._query_attempts(evaluations)
        sources = [
            {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "sourceName": item.source_name,
                "queriedAt": item.queried_at.isoformat(),
                "confidence": score["confidence"],
                "credibilityRank": score["credibilityRank"],
                "sourceDate": score["sourceDate"],
                "targetDateRelevance": score["targetDateRelevance"],
                "stalenessReason": score["stalenessReason"],
                "providerName": item.provider_name or search_result.provider_name,
                "fallbackUsed": item.fallback_used or search_result.fallback_used,
                "failureReason": item.failure_reason or search_result.failure_reason,
                "userVisibleCaveat": item.user_visible_caveat or search_result.user_visible_caveat,
            }
            for item, score in accepted_items
        ]

        if not sources:
            alert = self._unavailable_alert(
                plan_id,
                segment.id,
                poi.name,
                risk_status_reason,
            )
            alert.sources = [
                self._risk_search_diagnostics_source(
                    query,
                    search_result,
                    scored_items,
                    accepted_items,
                    stale_count,
                    official_count,
                    "unavailable",
                    risk_query.target_year,
                    risk_query.target_date_range,
                    risk_status_reason,
                    query_attempts,
                ),
                self._provider_diagnostics_source(query, search_result),
            ]
            if not scored_items and search_result.failure_reason:
                return alert
            alert.sources.extend(
                {
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "sourceName": item.source_name,
                    **score,
                }
                for item, score in scored_items[:5]
            )
            if risk_status_reason == "search_results_all_stale":
                alert.user_visible_caveat = "找到公开结果，但未满足当前出行日期要求；请核对景区官方公告。"
            elif risk_status_reason == "search_results_low_credibility":
                alert.user_visible_caveat = "找到公开结果，但未满足官方来源或可信度要求；请核对景区官方公告。"
            else:
                alert.user_visible_caveat = "只找到过期、无日期或低可信来源，不能形成确定性风险结论；请核对景区官方公告。"
            alert.failure_reason = risk_status_reason
            return alert

        synthesis_payload = self._synthesis_payload(
            city,
            poi,
            segment,
            weather,
            traffic_signals,
            ticket_results,
            route_options,
            sources,
            risk_context,
        )
        summary = self._summary_from_sources(sources, risk_context)
        confidence = min(0.86, max(source.get("confidence", 0.0) for source in sources))
        strict_target_date = bool(risk_query.target_year)
        status = "degraded" if risk_status_reason == "search_success_degraded" or search_result.fallback_used or (strict_target_date and (not official_count or stale_count)) else "available"
        failure_reason = None if risk_status_reason == "search_success_available" else risk_status_reason
        caveat = search_result.user_visible_caveat or "风险提醒基于公开搜索结果和当前行程上下文，仍需以景区官方公告和现场管理为准。"
        if status == "degraded":
            caveat = "当前风险搜索来源不完整或存在旧信息，不能给出确定性风险；请以景区官方公告为准。"
        if not search_result.fallback_used and (budget is None or budget.can_synthesize()):
            try:
                if budget is not None:
                    budget.synthesis_count += 1
                agent_result = self.risk_agent_provider.synthesize(synthesis_payload)
                agent_summary = str(agent_result.get("summary") or "").strip()
                if agent_summary:
                    summary = self._merge_agent_summary(summary, agent_summary)
                confidence = max(confidence, float(agent_result.get("confidence") or 0))
                caveat = str(agent_result.get("userVisibleCaveat") or caveat)
            except (AgentRiskSynthesisError, RuntimeError, ValueError, TypeError) as error:
                status = "degraded"
                failure_reason = "; ".join(item for item in [failure_reason, str(error)] if item)
                caveat = "Agent 风险判断服务暂不可用，已保留公开搜索摘要；风险判断不完整，请核对来源。"
        alert = POIRiskAlert(
            id=f"risk_{uuid4().hex[:12]}",
            plan_id=plan_id,
            segment_id=segment.id,
            poi_name=poi.name,
            status=status,
            summary=summary,
            source_name=SEARCH_SOURCE_NAME,
            source_url=sources[0].get("url"),
            sources=sources,
            confidence=min(confidence, 0.95),
            failure_reason=failure_reason,
            user_visible_caveat=caveat,
        )
        alert.sources.append(
            self._risk_search_diagnostics_source(
                query,
                search_result,
                scored_items,
                accepted_items,
                stale_count,
                official_count,
                status,
                risk_query.target_year,
                risk_query.target_date_range,
                risk_status_reason,
                query_attempts,
            )
        )
        alert.sources.append(self._provider_diagnostics_source(query, search_result))
        return alert

    def _query_plan(self, risk_query: Any) -> list[str]:
        raw_queries = getattr(risk_query, "queries", None) or [getattr(risk_query, "query", "")]
        queries: list[str] = []
        seen: set[str] = set()
        for query in raw_queries:
            normalized = " ".join(str(query or "").split())
            key = normalized.casefold()
            if normalized and key not in seen:
                queries.append(normalized)
                seen.add(key)
        return queries

    def _select_search_evaluation(self, evaluations: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        for evaluation in evaluations:
            if evaluation.get("acceptedItems"):
                return evaluation
        if not evaluations:
            return None
        return max(
            evaluations,
            key=lambda item: (
                len(item.get("scoredItems") or []),
                -int(item.get("staleCount") or 0),
                len(item.get("searchResult").results if item.get("searchResult") is not None else []),
            ),
        )

    def _query_attempts(self, evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for evaluation in evaluations:
            search_result = evaluation.get("searchResult")
            attempts.append(
                {
                    "query": evaluation.get("query"),
                    "riskStatusReason": evaluation.get("riskStatusReason"),
                    "acceptedSourceCount": len(evaluation.get("acceptedItems") or []),
                    "sourceCount": len(evaluation.get("scoredItems") or []),
                    "providerName": getattr(search_result, "provider_name", ""),
                    "attemptedProviders": getattr(search_result, "attempted_providers", []),
                    "successfulProviders": getattr(search_result, "successful_providers", []),
                    "failedProviders": getattr(search_result, "failed_providers", []),
                    "skippedProviders": getattr(search_result, "skipped_providers", []),
                }
            )
        return attempts

    def _risk_status_reason(
        self,
        search_result: Any,
        scored_items: list[tuple[Any, dict[str, Any]]],
        accepted_items: list[tuple[Any, dict[str, Any]]],
        stale_count: int,
    ) -> str:
        if accepted_items:
            return "search_success_degraded" if search_result.fallback_used or search_result.failed_providers or search_result.skipped_providers else "search_success_available"
        if not search_result.results:
            if self._empty_search_result_is_no_results(search_result):
                return "search_no_results"
            if search_result.attempted_providers or search_result.failed_providers or search_result.skipped_providers:
                return "search_provider_unavailable"
            return "search_no_results"
        if scored_items and stale_count == len(scored_items):
            return "search_results_all_stale"
        if scored_items:
            return "search_results_low_credibility"
        return "search_no_results"

    def _empty_search_result_is_no_results(self, search_result: Any) -> bool:
        raw_diagnostics = getattr(search_result, "provider_diagnostics", [])
        diagnostics = raw_diagnostics if isinstance(raw_diagnostics, list) else []
        has_no_result_signal = False
        has_hard_failure = False
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            reason = str(item.get("reason") or "")
            if status == "no_results" or reason == "no_usable_results":
                has_no_result_signal = True
                continue
            if status == "failed" and reason not in {"no_usable_results"}:
                has_hard_failure = True
            if status == "skipped" and reason in {"circuit_open", "max_provider_attempts_reached"}:
                has_hard_failure = True
        return has_no_result_signal and not has_hard_failure

    def _risk_search_diagnostics_source(
        self,
        query: str,
        search_result: Any,
        scored_items: list[tuple[Any, dict[str, Any]]],
        accepted_items: list[tuple[Any, dict[str, Any]]],
        stale_count: int,
        official_count: int,
        status: str,
        target_year: Optional[int],
        target_date_range: Optional[str],
        risk_status_reason: str,
        query_attempts: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        return {
            "type": "riskSearchDiagnostics",
            "query": query,
            "queryAttempts": query_attempts or [],
            "queryLength": len(query),
            "sourceCount": len(scored_items),
            "acceptedSourceCount": len(accepted_items),
            "rejectedStaleSourceCount": stale_count,
            "officialSourceCount": official_count,
            "riskStatus": status,
            "riskStatusReason": risk_status_reason,
            "targetYear": target_year,
            "targetDateRange": target_date_range,
            "providerName": search_result.provider_name,
            "fallbackUsed": search_result.fallback_used,
        }

    def _provider_diagnostics_source(self, query: str, search_result: Any) -> dict[str, Any]:
        return {
            "type": "webSearchProviderDiagnostics",
            "query": query,
            "providerName": search_result.provider_name,
            "attemptedProviders": search_result.attempted_providers,
            "successfulProviders": search_result.successful_providers,
            "failedProviders": search_result.failed_providers,
            "skippedProviders": search_result.skipped_providers,
            "providerDiagnostics": search_result.provider_diagnostics,
        }

    def _skipped_after_provider_unavailable_alert(
        self,
        plan_id: str,
        city: str,
        poi: POI,
        segment: ItinerarySegment,
    ) -> POIRiskAlert:
        query = f"{city} {poi.name} 风险搜索跳过"
        alert = self._unavailable_alert(
            plan_id,
            segment.id,
            poi.name,
            "skipped_due_to_provider_unavailable",
        )
        alert.user_visible_caveat = "本轮联网搜索 provider 已不可用，已停止后续 POI 风险搜索；请稍后重试或核对官方公告。"
        alert.sources = [
            {
                "type": "riskSearchDiagnostics",
                "query": query,
                "queryAttempts": [],
                "queryLength": len(query),
                "sourceCount": 0,
                "acceptedSourceCount": 0,
                "rejectedStaleSourceCount": 0,
                "officialSourceCount": 0,
                "riskStatus": "unavailable",
                "riskStatusReason": "skipped_due_to_provider_unavailable",
                "targetYear": None,
                "targetDateRange": None,
                "providerName": "",
                "fallbackUsed": False,
            },
            {
                "type": "webSearchProviderDiagnostics",
                "query": query,
                "providerName": "",
                "attemptedProviders": [],
                "successfulProviders": [],
                "failedProviders": [],
                "skippedProviders": [],
                "providerDiagnostics": [
                    {
                        "providerName": "",
                        "status": "skipped",
                        "reason": "skipped_due_to_provider_unavailable",
                        "resultCount": 0,
                    }
                ],
            },
        ]
        return alert

    def _http_get_with_headers(self, url: str, timeout: float, _headers: Optional[dict] = None) -> dict:
        if self.http_get is None:
            raise POIRiskSearchError("搜索请求函数未配置。")
        return self.http_get(url, timeout)

    def _query(
        self,
        city: str,
        poi: POI,
        segment: ItinerarySegment,
        weather: WeatherSignal,
        traffic_signals: list[TrafficCrowdingSignal],
        ticket_results: list[TicketLookupResult],
        route_options: list[RouteOption],
        risk_context: dict[str, Any],
    ) -> str:
        ticket_status = " ".join(result.status for result in ticket_results if result.segment_id == segment.id)
        traffic_text = " ".join(
            f"{signal.crowding_level} {signal.estimated_reason} {signal.recommended_departure_adjustment}"
            for signal in traffic_signals
        )
        route_text = " ".join(
            f"{route.label or route.mode} {route.duration_minutes}分钟 {route.distance_meters}米 {route.cost_amount:g}元 {route.crowding_risk}"
            for route in self._routes_for_segment(segment, route_options)
        )
        context_text = " ".join(
            [
                self._string_value(risk_context.get("preferenceSummary")),
                self._string_value(risk_context.get("partySize")),
                self._list_value(risk_context.get("travelerTypes")),
                self._string_value(risk_context.get("budgetRange")),
                self._string_value(risk_context.get("pacePreference")),
                self._date_range_value(risk_context.get("travelDateRange")),
                self._string_value(risk_context.get("tripPurpose")),
                self._list_value(risk_context.get("riskPriorityTerms")),
                self._string_value(risk_context.get("travelerSensitivity")),
            ]
        )
        official_hint = "官网 官方公告 政务 教育机构 site:gov.cn site:edu.cn" if risk_context.get("officialSourceFirst") else ""
        return " ".join(
            item
            for item in [
                city,
                poi.name,
                official_hint,
                "近期 临时闭园 施工 限流 预约变化 交通管制 重大活动 人流异常 安全提示",
                segment.start_time,
                segment.end_time,
                segment.transport_mode,
                poi.category,
                weather.date,
                weather.daily_summary,
                weather.risk_level,
                weather.purpose_impact_reason,
                traffic_text,
                ticket_status,
                route_text,
                context_text,
            ]
            if item
        )

    def _summary_from_sources(self, sources: list[dict], risk_context: dict[str, Any]) -> str:
        source_summary = "；".join(source["snippet"] for source in sources[:2] if source.get("snippet"))
        context_bits = self._context_summary_bits(risk_context)
        if context_bits:
            prefix = f"结合当前行程上下文（{'，'.join(context_bits)}）"
            if source_summary:
                return f"{prefix}，公开来源提示：{source_summary}"
            return f"{prefix}，已检索到公开来源，但摘要为空，请打开来源核对。"
        return source_summary or "已检索到公开来源，但摘要为空，请打开来源核对。"

    def _merge_agent_summary(self, source_summary: str, agent_summary: str) -> str:
        if source_summary:
            return f"{source_summary} Agent 判断：{agent_summary}"
        return f"Agent 判断：{agent_summary}"

    def _synthesis_payload(
        self,
        city: str,
        poi: POI,
        segment: ItinerarySegment,
        weather: WeatherSignal,
        traffic_signals: list[TrafficCrowdingSignal],
        ticket_results: list[TicketLookupResult],
        route_options: list[RouteOption],
        sources: list[dict],
        risk_context: dict[str, Any],
    ) -> dict:
        return {
            "city": city,
            "poi": {
                "name": poi.name,
                "category": poi.category,
                "latitude": poi.latitude,
                "longitude": poi.longitude,
            },
            "segment": {
                "startTime": segment.start_time,
                "endTime": segment.end_time,
                "transportMode": segment.transport_mode,
                "estimatedCost": segment.estimated_cost,
                "notes": segment.notes,
            },
            "weather": {
                "date": weather.date,
                "dailySummary": weather.daily_summary,
                "riskLevel": weather.risk_level,
                "purposeImpactReason": weather.purpose_impact_reason,
                "dataStatus": weather.data_status,
                "confidence": weather.confidence,
            },
            "traffic": [
                {
                    "crowdingLevel": signal.crowding_level,
                    "estimatedReason": signal.estimated_reason,
                    "recommendedDepartureAdjustment": signal.recommended_departure_adjustment,
                    "realDataAvailable": signal.real_data_available,
                }
                for signal in traffic_signals
            ],
            "tickets": [
                {
                    "ticketType": result.ticket_type,
                    "status": result.status,
                    "sourceName": result.source_name,
                    "credibilityRank": result.credibility_rank,
                    "confidence": result.confidence,
                }
                for result in ticket_results
                if result.segment_id == segment.id
            ],
            "routes": [
                {
                    "id": route.id,
                    "mode": route.mode,
                    "label": route.label,
                    "durationMinutes": route.duration_minutes,
                    "distanceMeters": route.distance_meters,
                    "costAmount": route.cost_amount,
                    "crowdingRisk": route.crowding_risk,
                    "isSelected": route.is_selected,
                }
                for route in self._routes_for_segment(segment, route_options)
            ],
            "riskContext": risk_context,
            "sources": sources,
        }

    def _routes_for_segment(self, segment: ItinerarySegment, route_options: list[RouteOption]) -> list[RouteOption]:
        return [
            route
            for route in route_options
            if route.from_segment_id == segment.id
            or route.to_segment_id == segment.id
            or route.from_poi_id == segment.poi_id
            or route.to_poi_id == segment.poi_id
        ]

    def _context_summary_bits(self, risk_context: dict[str, Any]) -> list[str]:
        bits: list[str] = []
        party_size = self._string_value(risk_context.get("partySize"))
        traveler_types = self._list_value(risk_context.get("travelerTypes"))
        budget = self._string_value(risk_context.get("budgetRange"))
        pace = self._string_value(risk_context.get("pacePreference"))
        purpose = self._string_value(risk_context.get("tripPurpose"))
        date_range = self._date_range_value(risk_context.get("travelDateRange"))
        if party_size:
            bits.append(f"{party_size}人")
        if traveler_types:
            bits.append(traveler_types)
        if budget:
            bits.append(f"预算 {budget}")
        if pace:
            bits.append(pace)
        if purpose:
            bits.append(purpose)
        if date_range:
            bits.append(date_range)
        return bits[:6]

    def _string_value(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _list_value(self, value: Any) -> str:
        if isinstance(value, list):
            return " ".join(str(item).strip() for item in value if str(item).strip())
        return self._string_value(value)

    def _date_range_value(self, value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(str(value.get(key) or "").strip() for key in ("start", "end") if value.get(key))
        return self._string_value(value)

    def _unavailable_alert(self, plan_id: str, segment_id: str, poi_name: str, reason: str) -> POIRiskAlert:
        return POIRiskAlert(
            id=f"risk_{uuid4().hex[:12]}",
            plan_id=plan_id,
            segment_id=segment_id,
            poi_name=poi_name,
            status="unavailable",
            summary="未完成近期公开信息搜索，景点风险判断不完整。",
            source_name=SEARCH_SOURCE_NAME,
            confidence=0.0,
            failure_reason=reason,
            user_visible_caveat="无法联网搜索近期景点信息，当前风险判断不完整；请在出行前核对景区官方公告、预约状态和交通管制。",
        )

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from src.providers.travel_tools import ResilientWebSearchProvider


GUIDE_RESTAURANT_NAME_SUFFIXES = ("餐厅", "饭店", "酒楼", "食府", "小馆", "烤鸭店")


class TravelGuideAdviceService:
    """Produce bounded, read-only advice from ordinary travel pages."""

    _INTENT_THEMES: dict[str, tuple[str, tuple[str, ...]]] = {
        "campus_visit": ("高校参观", ("高校", "大学", "校园", "访客")),
        "meal": ("当地美食", ("美食", "餐厅", "餐饮", "小吃", "午餐", "晚餐")),
        "food_experience": ("当地美食", ("美食", "餐厅", "餐饮", "小吃")),
        "park": ("城市公园", ("公园", "绿地", "园林")),
        "night_view": ("城市夜景", ("夜景", "夜游", "夜晚", "晚上")),
        "museum": ("博物馆", ("博物馆", "博物院", "展览")),
        "landmark": ("城市地标", ("地标", "景点", "建筑")),
        "rest": ("休闲休息", ("休闲", "休息", "放松")),
        "area_walk": ("城市漫步", ("漫步", "步行", "街区", "街巷")),
        "local_culture": ("本地人文", ("人文", "文化", "历史", "民俗")),
        "culture": ("人文体验", ("人文", "文化", "历史")),
        "shopping": ("本地购物", ("购物", "商场", "市集")),
        "scenic": ("景点游览", ("景点", "景区", "游览")),
    }
    _SAFE_PROVIDER_NAMES = {
        "anysearch",
        "baidu-html-search",
        "bing-html-search",
        "bocha-web-search",
        "brave-web-search",
        "cheetah-duckduckgo-html-search",
        "chained-web-search",
        "ddgs",
        "duckduckgo-html-search",
        "duckduckgo-lite-search",
        "google-cse",
        "multi-free-search",
        "searxng",
        "tavily",
        "web-search-provider",
    }
    _SAFE_REASON_CODES = {
        "all_web_search_providers_failed_or_empty",
        "auth_failed",
        "blocked_or_captcha",
        "cache_hit",
        "chain_deadline_exhausted",
        "circuit_open",
        "ddgs_api_unreachable",
        "ddgs_package_timeout_unsupported",
        "filtered_empty",
        "http_error",
        "invalid_response",
        "max_provider_attempts_reached",
        "missing_config_reason",
        "multi_free_all_children_failed_or_empty",
        "no_results",
        "no_usable_results",
        "non_public_url",
        "parser_empty",
        "provider_error",
        "query_mismatch",
        "query_year_stale",
        "quota_exceeded",
        "rate_limited",
        "raw_empty",
        "searxng_json_format_disabled",
        "skipped_missing_config",
        "timeout",
        "transport_error",
        "unknown",
    }
    _SENSITIVE_QUERY_KEY_PARTS = {
        "accesstoken",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "credential",
        "password",
        "passwd",
        "refreshtoken",
        "secret",
        "sessionid",
        "sig",
        "signature",
        "token",
    }
    _TRAVEL_ADVICE_SIGNALS = (
        "攻略",
        "指南",
        "游玩",
        "游览",
        "参观",
        "打卡",
        "行程",
        "路线",
        "游客",
        "访客",
        "门票",
        "预约",
        "开放时间",
        "入校",
        "一日游",
        "两日游",
        "三日游",
        "周末游",
        "散步",
        "探店",
        "必吃",
        "避坑",
        "怎么去",
    )
    _CITY_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "城市", "市", "州", "盟")
    _RELATIVE_TIME_PREFIX = re.compile(
        r"^\s*(?:(?:\d+\s*(?:分钟|小时|天|周|个月|年)\s*(?:前|之前))|今天|昨天|前天)\s*[·•|｜\-—]\s*"
    )
    _PLACE_SUFFIXES_BY_INTENT: dict[str, tuple[str, ...]] = {
        "campus_visit": ("大学", "学院"),
        "meal": GUIDE_RESTAURANT_NAME_SUFFIXES,
        "food_experience": GUIDE_RESTAURANT_NAME_SUFFIXES,
        "park": ("国家公园", "森林公园", "湿地公园", "公园", "植物园", "动物园"),
        "night_view": ("观景台", "电视塔", "大厦", "广场", "步道", "桥", "塔"),
        "museum": ("博物馆", "博物院", "美术馆", "纪念馆", "科技馆", "展览馆"),
        "landmark": ("观景台", "电视塔", "大厦", "广场", "中心", "城楼", "体育场", "桥", "塔"),
        "rest": ("公园", "咖啡馆", "茶馆"),
        "area_walk": ("步道", "胡同", "大街", "街区", "街", "巷", "公园", "广场"),
        "local_culture": ("博物馆", "博物院", "美术馆", "纪念馆", "故居", "胡同", "寺", "庙", "宫", "祠"),
        "culture": ("博物馆", "博物院", "美术馆", "纪念馆", "故居", "胡同", "寺", "庙", "宫", "祠"),
        "shopping": ("购物中心", "商场", "市集", "市场"),
        "scenic": ("国家公园", "森林公园", "湿地公园", "风景区", "景区", "公园", "植物园", "山", "湖", "岛"),
    }
    _GENERIC_PLACE_MENTIONS = {
        "大学",
        "高校",
        "高校校园",
        "当地大学",
        "当地高校",
        "城市大学",
        "城市高校",
        "公园",
        "城市公园",
        "当地公园",
        "附近公园",
        "博物馆",
        "城市博物馆",
        "当地博物馆",
        "餐厅",
        "当地餐厅",
        "特色餐厅",
        "城市地标",
        "地标",
        "观景台",
        "城市夜景观景台",
        "购物中心",
        "商场",
        "市集",
        "市场",
    }
    _PLACE_HINT_LIMIT = 5
    _PLACE_MENTION_MAX_CHARS = 80
    _PLACE_EXTRACTION_VERSION = "source_entity_span_v2"
    _PLACE_ACTION_BOUNDARIES = (
        "可以前往",
        "建议前往",
        "推荐前往",
        "可前往",
        "前往",
        "推荐",
        "建议",
        "参观",
        "游览",
        "游玩",
        "打卡",
        "探访",
        "走进",
        "体验",
        "安排",
        "选择",
        "登上",
        "登临",
        "到达",
        "抵达",
        "途经",
        "包括",
        "位于",
        "坐落在",
    )
    _PLACE_NON_ENTITY_TEXT = re.compile(
        r"各个|各大|各类|各所|多所|全国|全市|所有|一些|这些|那些|某个|某家|某所|"
        r"只需|需要|就能|即可|能够|可以|适合|值得|提供|开放时间|预约|门票|"
        r"(?:花|花费|支付)?\d+(?:块|元|分钟|小时)|攻略|指南|推荐|附近|周边|待确认"
    )
    _PLACE_ENTITY_RIGHT_BOUNDARY = re.compile(
        r"^(?:$|[^\u3400-\u9fffA-Za-z0-9]|参观|游览|游玩|攻略|指南|需要|适合|预约|开放|位于|以及|还有|或者|和|与|及)"
    )

    def __init__(
        self,
        *,
        provider: Optional[ResilientWebSearchProvider] = None,
        conclusion_provider: Optional[Any] = None,
    ):
        self.provider = provider or ResilientWebSearchProvider()
        self.conclusion_provider = conclusion_provider

    def search(self, *, city: str, request_contract: dict[str, Any]) -> dict[str, Any]:
        intent_types = [
            str(item.get("intentType") or "")
            for item in request_contract.get("requiredIntents") or []
            if isinstance(item, dict) and str(item.get("intentType") or "")
        ]
        themes = self._theme_specs(intent_types)
        theme_text = self._query_theme_text(themes)
        queries = [f"{city} {theme_text} 游玩攻略 行程推荐"]
        first = self.provider.search(queries[0], count=5, freshness="oneYear")
        responses = [first]
        first_relevant, first_rejections = self._relevant_items(
            first.results,
            city=city,
            themes=themes,
        )
        accepted_results = list(first_relevant)
        rejection_counts = dict(first_rejections)
        missing_themes = self._missing_themes(first_relevant, themes)
        if not first_relevant or missing_themes:
            missing_theme_text = self._query_theme_text(missing_themes or themes)
            queries.append(f"{city} {missing_theme_text} 游客攻略 避坑提示")
            responses.append(self.provider.search(queries[1], count=4, freshness="oneYear"))
            second_relevant, second_rejections = self._relevant_items(
                responses[1].results,
                city=city,
                themes=missing_themes or themes,
            )
            accepted_results.extend(second_relevant)
            for reason, count in second_rejections.items():
                rejection_counts[reason] = rejection_counts.get(reason, 0) + count
        items = self._safe_dedupe_items([item for item in accepted_results if item.url])
        admitted = self._admitted_evidence(items[:5])
        queried_at = datetime.now(timezone.utc).isoformat()
        query_fingerprint = hashlib.sha256(
            json.dumps(queries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        evidence_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "queryFingerprint": query_fingerprint,
                    "sourceFingerprints": [item["sourceFingerprint"] for item in admitted],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        source_refs = [
            {
                "refId": item["refId"],
                "sourceFingerprint": item["sourceFingerprint"],
                "title": item["title"],
                "url": item["url"],
                "sourceName": item["sourceName"],
                "queriedAt": item["queriedAt"],
                "credibilityRank": item["credibilityRank"],
            }
            for item in admitted
        ]
        recommendations = [
            {
                "refId": item["refId"],
                "sourceFingerprint": item["sourceFingerprint"],
                "title": item["title"],
                "text": item["summary"],
                "sourceUrl": item["url"],
                "sourceName": item["sourceName"],
                "queriedAt": item["queriedAt"],
                "credibilityRank": item["credibilityRank"],
                "summaryKind": "search_result_snippet",
                "poiVerificationStatus": "unverified_advice",
            }
            for item in admitted
            if item["summary"]
        ]
        cautions = [
            {"text": item["summary"][:600], "sourceUrl": item["url"], "sourceRefIds": [item["refId"]]}
            for item in admitted
            if any(token in item["searchText"] for token in ("注意", "避坑", "预约", "关闭", "闭馆", "排队"))
        ][:4]
        conclusion = self._build_conclusion(
            admitted=admitted,
            themes=themes,
            query_fingerprint=query_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
        )
        raw_failure_reason = next(
            (
                str(response.failure_reason)
                for response in reversed(responses)
                if str(response.failure_reason or "").strip()
            ),
            None,
        )
        failure_reason = self._safe_reason_code(raw_failure_reason) if raw_failure_reason else None
        attempted_providers = self._provider_names(responses, "attempted_providers")
        successful_providers = self._provider_names(responses, "successful_providers")
        failed_providers = self._provider_names(responses, "failed_providers")
        skipped_providers = self._provider_names(responses, "skipped_providers")
        provider_diagnostics = self._safe_provider_diagnostics(responses)
        payload = {
            "status": "completed" if source_refs else "failed" if failure_reason else "no_results",
            "failureReason": None if source_refs else failure_reason,
            "recommendations": recommendations,
            "cautions": cautions,
            "sourceRefs": source_refs,
            "queryFingerprint": query_fingerprint,
            "evidenceFingerprint": evidence_fingerprint,
            "queriedAt": queried_at,
            "conclusion": conclusion,
            "caveat": "以下为搜索结果摘要，不是网页全文；普通攻略只作经验性建议。请打开原文核对上下文，新地点在经过高德 POI 与路线核验前不会自动写入行程。",
            "queryCount": len(responses),
            "relevanceFilter": {
                "acceptedResultCount": len(admitted),
                "rejectedResultCount": sum(rejection_counts.values()),
                "reasonCounts": {
                    reason: rejection_counts[reason]
                    for reason in (
                        "destination_mismatch",
                        "theme_mismatch",
                        "travel_advice_signal_missing",
                    )
                    if rejection_counts.get(reason)
                },
            },
            "attemptedProviders": attempted_providers,
            "successfulProviders": successful_providers,
            "failedProviders": failed_providers,
            "skippedProviders": skipped_providers,
            "providerDiagnostics": provider_diagnostics,
        }
        payload["placeHints"] = self.extract_place_hints(payload)
        return payload

    @classmethod
    def build_choice(
        cls,
        *,
        source_assistant_turn_id: str,
        portfolio_id: str,
        planning_root_id: str,
        request_fingerprint: str,
        expected_base_version_id: Optional[str],
        retry: bool = False,
        workflow_mode: str = "simple_direction_v1",
    ) -> dict[str, Any]:
        choice_id = (
            "guide_"
            + hashlib.sha256(
                f"{source_assistant_turn_id}\n{portfolio_id}\n{request_fingerprint}".encode("utf-8")
            ).hexdigest()[:18]
        )
        return {
            "id": choice_id,
            "choiceId": choice_id,
            "action": "search_travel_guide_advice",
            "kind": "travel_guide_advice",
            "scopeKind": "comparison",
            "label": "重新搜索普通攻略" if retry else "搜索普通攻略并给我建议",
            "description": "最多检索两次普通旅游攻略，只返回经验性建议，不修改行程。",
            "sourceAssistantTurnId": source_assistant_turn_id,
            "sourceUserTurnId": planning_root_id,
            "planningSelectionRootTurnId": planning_root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": request_fingerprint,
            "expectedBaseVersionId": expected_base_version_id,
            "workflowMode": workflow_mode,
        }

    @classmethod
    def _theme_specs(cls, intent_types: list[str]) -> list[tuple[str, str, tuple[str, ...]]]:
        specs: list[tuple[str, str, tuple[str, ...]]] = []
        seen: set[str] = set()
        for intent_type in intent_types:
            spec = cls._INTENT_THEMES.get(intent_type)
            if spec is None or spec[0] in seen:
                continue
            seen.add(spec[0])
            specs.append((intent_type, spec[0], spec[1]))
        return specs

    @classmethod
    def _query_theme_text(cls, themes: list[tuple[str, str, tuple[str, ...]]]) -> str:
        return " ".join(label for _intent_type, label, _aliases in themes) or "攻略"

    @classmethod
    def _relevant_items(
        cls,
        results: list[Any],
        *,
        city: str,
        themes: list[tuple[str, str, tuple[str, ...]]],
    ) -> tuple[list[Any], dict[str, int]]:
        accepted: list[Any] = []
        rejected: dict[str, int] = {}
        destination_terms = cls._destination_terms(city)
        theme_aliases = tuple(
            dict.fromkeys(alias.lower() for _intent_type, _label, aliases in themes for alias in aliases)
        )
        for item in results:
            haystack = cls._clean_text(
                f"{item.title} {item.snippet} {item.summary} {item.source_name}",
                limit=4000,
            ).lower()
            reason: Optional[str] = None
            if destination_terms and not any(term in haystack for term in destination_terms):
                reason = "destination_mismatch"
            elif theme_aliases and not any(alias in haystack for alias in theme_aliases):
                reason = "theme_mismatch"
            elif not any(signal in haystack for signal in cls._TRAVEL_ADVICE_SIGNALS):
                reason = "travel_advice_signal_missing"
            if reason is not None:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            accepted.append(item)
        return accepted, rejected

    @classmethod
    def _destination_terms(cls, city: str) -> tuple[str, ...]:
        normalized = cls._clean_text(city, limit=80).lower()
        if not normalized:
            return ()
        stripped = normalized
        for suffix in cls._CITY_SUFFIXES:
            if stripped.endswith(suffix) and len(stripped) > len(suffix):
                stripped = stripped[: -len(suffix)]
                break
        return tuple(dict.fromkeys(term for term in (normalized, stripped) if len(term) >= 2))

    @classmethod
    def _result_summary(cls, item: Any) -> str:
        value = cls._clean_text(item.snippet or item.summary or item.title, limit=1200)
        return cls._RELATIVE_TIME_PREFIX.sub("", value, count=1).strip()

    @classmethod
    def _admitted_evidence(cls, items: list[tuple[Any, str]]) -> list[dict[str, Any]]:
        admitted: list[dict[str, Any]] = []
        for item, safe_url in items:
            title = cls._clean_text(item.title, limit=240)
            summary = cls._result_summary(item)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "title": title,
                        "summary": summary,
                        "url": safe_url,
                        "queriedAt": item.queried_at.isoformat(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            admitted.append(
                {
                    "refId": f"guide_ref_{fingerprint[:12]}",
                    "sourceFingerprint": fingerprint,
                    "title": title,
                    "summary": summary,
                    "url": safe_url,
                    "sourceName": cls._clean_text(item.source_name, limit=160),
                    "queriedAt": item.queried_at.isoformat(),
                    "credibilityRank": item.credibility_rank,
                    "searchText": cls._clean_text(f"{title} {summary}", limit=1800),
                }
            )
        return admitted

    def _build_conclusion(
        self,
        *,
        admitted: list[dict[str, Any]],
        themes: list[tuple[str, str, tuple[str, ...]]],
        query_fingerprint: str,
        evidence_fingerprint: str,
    ) -> dict[str, Any]:
        deterministic = self._deterministic_conclusion(
            admitted=admitted,
            themes=themes,
            evidence_fingerprint=evidence_fingerprint,
        )
        if (
            not admitted
            or self.conclusion_provider is None
            or not hasattr(self.conclusion_provider, "synthesize_travel_guide_conclusion")
        ):
            deterministic["fallbackReasonCode"] = "provider_unavailable"
            return deterministic
        context = {
            "schemaVersion": "travel-guide-conclusion-input-v1",
            "queryFingerprint": query_fingerprint,
            "evidenceFingerprint": evidence_fingerprint,
            "allowedThemes": [
                {"intentType": intent_type, "themeLabel": label} for intent_type, label, _aliases in themes
            ],
            "deterministicSummary": {
                "status": deterministic["status"],
                "overview": deterministic["overview"],
                "missingThemes": deterministic["missingThemes"],
                "conflicts": deterministic["conflicts"],
            },
            "evidence": [
                {
                    "refId": item["refId"],
                    "title": item["title"],
                    "snippet": item["summary"],
                }
                for item in admitted
            ],
            "placeMentionContract": {
                "optional": True,
                "maxItems": min(len(admitted), self._PLACE_HINT_LIMIT),
                "allowedIntentTypes": [intent_type for intent_type, _label, _aliases in themes],
                "requiredFields": ["mentionText", "intentType", "sourceRefIds"],
                "verificationStatus": "unresolved_amap_grounding",
            },
        }
        try:
            raw = self.conclusion_provider.synthesize_travel_guide_conclusion(
                context,
                timeout_seconds=6.0,
            )
            parsed = raw if isinstance(raw, dict) else json.loads(raw)
            return self._validated_model_conclusion(
                parsed,
                admitted=admitted,
                themes=themes,
                deterministic=deterministic,
                evidence_fingerprint=evidence_fingerprint,
            )
        except Exception:
            deterministic["fallbackReasonCode"] = "model_output_invalid_or_unavailable"
            return deterministic

    @classmethod
    def _deterministic_conclusion(
        cls,
        *,
        admitted: list[dict[str, Any]],
        themes: list[tuple[str, str, tuple[str, ...]]],
        evidence_fingerprint: str,
    ) -> dict[str, Any]:
        takeaways: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        for intent_type, label, aliases in themes:
            matches = [
                item for item in admitted if any(alias.lower() in item["searchText"].lower() for alias in aliases)
            ]
            if not matches:
                missing.append({"intentType": intent_type, "themeLabel": label})
                continue
            takeaways.append(
                {
                    "intentType": intent_type,
                    "themeLabel": label,
                    "text": f"{label}：{matches[0]['summary'][:180]}",
                    "sourceRefIds": [item["refId"] for item in matches[:2]],
                }
            )
        conflicts = cls._evidence_conflicts(admitted)
        place_mentions = cls._deterministic_place_mentions(admitted=admitted, themes=themes)
        if conflicts:
            status = "conflicting"
        elif not admitted or not takeaways:
            status = "insufficient_evidence"
        elif missing:
            status = "partial"
        else:
            status = "ready"
        covered_labels = [item["themeLabel"] for item in takeaways]
        overview = (
            f"现有攻略摘要主要覆盖{'、'.join(covered_labels)}。"
            if covered_labels
            else "本轮没有获得足以归纳当前旅行主题的攻略证据。"
        )
        if missing:
            overview += f" {'、'.join(item['themeLabel'] for item in missing)}仍缺少可靠摘要。"
        if conflicts:
            overview += " 部分来源说法存在冲突，出发前应以官方最新信息为准。"
        return {
            "status": status,
            "overview": overview[:240],
            "takeaways": takeaways[:5],
            "conflicts": conflicts,
            "missingThemes": missing,
            "evidenceBasis": "search_result_snippets",
            "generationMethod": "deterministic_fallback_v1",
            "evidenceFingerprint": evidence_fingerprint,
            "placeMentions": place_mentions,
            "placeMentionExtractionVersion": cls._PLACE_EXTRACTION_VERSION,
        }

    @classmethod
    def _validated_model_conclusion(
        cls,
        parsed: Any,
        *,
        admitted: list[dict[str, Any]],
        themes: list[tuple[str, str, tuple[str, ...]]],
        deterministic: dict[str, Any],
        evidence_fingerprint: str,
    ) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError("travel_guide_conclusion_not_object")
        model_overview = cls._clean_text(parsed.get("overview"), limit=240)
        if not model_overview:
            raise ValueError("travel_guide_conclusion_overview_missing")
        evidence_by_id = {item["refId"]: item for item in admitted}
        allowed_themes = {(intent_type, label) for intent_type, label, _aliases in themes}
        takeaways: list[dict[str, Any]] = []
        raw_takeaways = parsed.get("takeaways")
        if not isinstance(raw_takeaways, list) or not raw_takeaways:
            raise ValueError("travel_guide_conclusion_takeaways_missing")
        for raw in raw_takeaways[:5]:
            if not isinstance(raw, dict):
                raise ValueError("travel_guide_conclusion_takeaway_invalid")
            intent_type = str(raw.get("intentType") or "")
            label = cls._clean_text(raw.get("themeLabel"), limit=40)
            if (intent_type, label) not in allowed_themes:
                raise ValueError("travel_guide_conclusion_theme_invalid")
            ref_ids = [str(value) for value in raw.get("sourceRefIds") or []]
            if not ref_ids or any(ref_id not in evidence_by_id for ref_id in ref_ids):
                raise ValueError("travel_guide_conclusion_ref_invalid")
            quote = cls._clean_text(raw.get("evidenceQuote"), limit=160)
            if not quote or not any(quote in evidence_by_id[ref_id]["searchText"] for ref_id in ref_ids):
                raise ValueError("travel_guide_conclusion_quote_invalid")
            text = cls._clean_text(raw.get("text"), limit=180)
            if not text:
                raise ValueError("travel_guide_conclusion_text_missing")
            takeaways.append(
                {
                    "intentType": intent_type,
                    "themeLabel": label,
                    "text": text,
                    "sourceRefIds": list(dict.fromkeys(ref_ids))[:3],
                }
            )
        conflicts: list[dict[str, Any]] = []
        for raw in parsed.get("conflicts") or []:
            if not isinstance(raw, dict):
                raise ValueError("travel_guide_conclusion_conflict_invalid")
            ref_ids = [str(value) for value in raw.get("sourceRefIds") or []]
            if not ref_ids or any(ref_id not in evidence_by_id for ref_id in ref_ids):
                raise ValueError("travel_guide_conclusion_conflict_ref_invalid")
            conflicts.append(
                {
                    "topic": cls._clean_text(raw.get("topic"), limit=60),
                    "summary": cls._clean_text(raw.get("summary"), limit=180),
                    "sourceRefIds": list(dict.fromkeys(ref_ids))[:3],
                }
            )
        raw_place_mentions = parsed.get("placeMentions")
        if raw_place_mentions is None or raw_place_mentions == []:
            place_mentions = list(deterministic.get("placeMentions") or [])
        else:
            place_hints = cls._normalize_place_mentions(
                raw_place_mentions,
                admitted=admitted,
                allowed_intent_types={intent_type for intent_type, _label, _aliases in themes},
                guide_evidence_fingerprint=evidence_fingerprint,
                require_explicit_bindings=False,
            )
            place_mentions = [
                {
                    "mentionText": item["mentionText"],
                    "intentType": item["intentType"],
                    "sourceRefIds": item["sourceRefIds"],
                }
                for item in place_hints
            ]
        return {
            **deterministic,
            # The top-level summary has no quote/ref fields in the public
            # contract. Keep it server-owned so model prose cannot introduce
            # an untraceable reservation, ticket, POI, or opening-hours claim.
            "overview": deterministic["overview"],
            "takeaways": takeaways,
            "conflicts": deterministic["conflicts"],
            "generationMethod": "deepseek_structured_v1",
            "evidenceFingerprint": evidence_fingerprint,
            "placeMentions": place_mentions,
        }

    @classmethod
    def extract_place_hints(
        cls, guide_advice: Any, *, allow_legacy_source_derivation: bool = False
    ) -> list[dict[str, Any]]:
        """Return only place hints that can be re-verified from one guide payload.

        This is intentionally usable by callers loading historical assistant
        turns. It performs no search and no model call. Historical payloads
        without complete source text and fingerprint lineage yield no hints.
        """

        evidence = cls._guide_payload_evidence(guide_advice)
        if evidence is None:
            return []
        admitted, guide_evidence_fingerprint = evidence
        if not admitted:
            return []
        allowed_intent_types = set(cls._INTENT_THEMES)
        conclusion = guide_advice.get("conclusion")
        legacy_source_derivation = (
            allow_legacy_source_derivation
            and isinstance(conclusion, dict)
            and "placeMentionExtractionVersion" not in conclusion
        )
        explicit_hints = guide_advice.get("placeHints")
        if explicit_hints not in (None, []):
            if not isinstance(explicit_hints, list):
                return []
            return cls._filter_verified_place_mentions(
                explicit_hints,
                admitted=admitted,
                allowed_intent_types=allowed_intent_types,
                guide_evidence_fingerprint=guide_evidence_fingerprint,
                require_explicit_bindings=True,
                allow_legacy_span_recovery=legacy_source_derivation,
            )

        if not isinstance(conclusion, dict):
            return []
        raw_mentions = conclusion.get("placeMentions")
        if "placeMentions" in conclusion:
            if not isinstance(raw_mentions, list):
                return []
            if not raw_mentions and legacy_source_derivation:
                raw_mentions = cls._legacy_conclusion_place_mentions(conclusion=conclusion, admitted=admitted)
            return cls._filter_verified_place_mentions(
                raw_mentions,
                admitted=admitted,
                allowed_intent_types=allowed_intent_types,
                guide_evidence_fingerprint=guide_evidence_fingerprint,
                require_explicit_bindings=False,
                allow_legacy_span_recovery=legacy_source_derivation,
            )

        legacy_mentions = cls._legacy_conclusion_place_mentions(
            conclusion=conclusion,
            admitted=admitted,
        )
        return cls._filter_verified_place_mentions(
            legacy_mentions,
            admitted=admitted,
            allowed_intent_types=allowed_intent_types,
            guide_evidence_fingerprint=guide_evidence_fingerprint,
            require_explicit_bindings=False,
        )

    @classmethod
    def _guide_payload_evidence(
        cls,
        guide_advice: Any,
    ) -> Optional[tuple[list[dict[str, Any]], str]]:
        if not isinstance(guide_advice, dict):
            return None
        query_fingerprint = cls._strict_fingerprint(guide_advice.get("queryFingerprint"))
        guide_evidence_fingerprint = cls._strict_fingerprint(guide_advice.get("evidenceFingerprint"))
        source_refs = guide_advice.get("sourceRefs")
        recommendations = guide_advice.get("recommendations")
        if (
            not query_fingerprint
            or not guide_evidence_fingerprint
            or not isinstance(source_refs, list)
            or not isinstance(recommendations, list)
            or not 1 <= len(source_refs) <= cls._PLACE_HINT_LIMIT
        ):
            return None

        recommendation_by_ref: dict[str, dict[str, Any]] = {}
        for recommendation in recommendations[: cls._PLACE_HINT_LIMIT]:
            if not isinstance(recommendation, dict):
                return None
            ref_id = str(recommendation.get("refId") or "").strip()
            if not ref_id or ref_id in recommendation_by_ref:
                return None
            recommendation_by_ref[ref_id] = recommendation

        admitted: list[dict[str, Any]] = []
        seen_ref_ids: set[str] = set()
        seen_source_fingerprints: set[str] = set()
        for raw_source in source_refs:
            if not isinstance(raw_source, dict):
                return None
            ref_id = str(raw_source.get("refId") or "").strip()
            source_fingerprint = cls._strict_fingerprint(raw_source.get("sourceFingerprint"))
            title = cls._clean_text(raw_source.get("title"), limit=240)
            source_url = cls._safe_source_url(raw_source.get("url"))
            queried_at = str(raw_source.get("queriedAt") or "").strip()
            recommendation = recommendation_by_ref.get(ref_id)
            if (
                not ref_id
                or ref_id in seen_ref_ids
                or not source_fingerprint
                or source_fingerprint in seen_source_fingerprints
                or not title
                or not source_url
                or not cls._timezone_aware_iso(queried_at)
                or recommendation is None
            ):
                return None
            summary = cls._clean_text(recommendation.get("text"), limit=1200)
            if (
                not summary
                or str(recommendation.get("refId") or "").strip() != ref_id
                or cls._strict_fingerprint(recommendation.get("sourceFingerprint")) != source_fingerprint
                or cls._clean_text(recommendation.get("title"), limit=240) != title
                or cls._safe_source_url(recommendation.get("sourceUrl")) != source_url
                or str(recommendation.get("queriedAt") or "").strip() != queried_at
            ):
                return None
            expected_source_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "title": title,
                        "summary": summary,
                        "url": source_url,
                        "queriedAt": queried_at,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if expected_source_fingerprint != source_fingerprint:
                return None
            seen_ref_ids.add(ref_id)
            seen_source_fingerprints.add(source_fingerprint)
            admitted.append(
                {
                    "refId": ref_id,
                    "sourceFingerprint": source_fingerprint,
                    "title": title,
                    "summary": summary,
                    "searchText": cls._clean_text(f"{title} {summary}", limit=1800),
                }
            )

        expected_guide_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "queryFingerprint": query_fingerprint,
                    "sourceFingerprints": [item["sourceFingerprint"] for item in admitted],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if expected_guide_fingerprint != guide_evidence_fingerprint:
            return None
        return admitted, guide_evidence_fingerprint

    @classmethod
    def _filter_verified_place_mentions(
        cls,
        raw_mentions: list[Any],
        *,
        admitted: list[dict[str, Any]],
        allowed_intent_types: set[str],
        guide_evidence_fingerprint: str,
        require_explicit_bindings: bool,
        allow_legacy_span_recovery: bool = False,
    ) -> list[dict[str, Any]]:
        limit = min(len(admitted), cls._PLACE_HINT_LIMIT)
        verified: list[dict[str, Any]] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for raw in raw_mentions[:limit]:
            try:
                normalized = cls._normalize_place_mentions(
                    [raw],
                    admitted=admitted,
                    allowed_intent_types=allowed_intent_types,
                    guide_evidence_fingerprint=guide_evidence_fingerprint,
                    require_explicit_bindings=require_explicit_bindings,
                    allow_legacy_span_recovery=allow_legacy_span_recovery,
                )[0]
            except (IndexError, TypeError, ValueError):
                continue
            key = (
                normalized["mentionText"],
                normalized["intentType"],
                tuple(normalized["sourceRefIds"]),
            )
            if key in seen:
                continue
            seen.add(key)
            verified.append(normalized)
        return verified[:limit]

    @classmethod
    def _normalize_place_mentions(
        cls,
        raw_mentions: Any,
        *,
        admitted: list[dict[str, Any]],
        allowed_intent_types: set[str],
        guide_evidence_fingerprint: str,
        require_explicit_bindings: bool,
        allow_legacy_span_recovery: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_mentions, list):
            raise ValueError("travel_guide_place_mentions_not_list")
        limit = min(len(admitted), cls._PLACE_HINT_LIMIT)
        if len(raw_mentions) > limit:
            raise ValueError("travel_guide_place_mentions_limit_exceeded")
        evidence_by_id = {item["refId"]: item for item in admitted}
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for raw in raw_mentions:
            if not isinstance(raw, dict):
                raise ValueError("travel_guide_place_mention_invalid")
            mention_text = cls._clean_text(raw.get("mentionText"), limit=cls._PLACE_MENTION_MAX_CHARS)
            intent_type = str(raw.get("intentType") or "").strip()
            raw_ref_ids = raw.get("sourceRefIds")
            if allow_legacy_span_recovery and not cls._valid_structured_place_mention(mention_text):
                mention_text = cls._recover_legacy_entity_span(
                    mention_text, intent_type=intent_type, raw_ref_ids=raw_ref_ids, evidence_by_id=evidence_by_id
                )
            if (
                not cls._valid_structured_place_mention(mention_text)
                or intent_type not in allowed_intent_types
                or not isinstance(raw_ref_ids, list)
            ):
                raise ValueError("travel_guide_place_mention_contract_invalid")
            source_ref_ids = list(dict.fromkeys(str(value).strip() for value in raw_ref_ids if str(value).strip()))
            if (
                not 1 <= len(source_ref_ids) <= 3
                or any(ref_id not in evidence_by_id for ref_id in source_ref_ids)
                or any(
                    not cls._source_contains_mention(evidence_by_id[ref_id], mention_text, intent_type)
                    for ref_id in source_ref_ids
                )
            ):
                raise ValueError("travel_guide_place_mention_ref_invalid")
            source_fingerprints = [evidence_by_id[ref_id]["sourceFingerprint"] for ref_id in source_ref_ids]
            if require_explicit_bindings:
                if raw.get("sourceFingerprints") != source_fingerprints:
                    raise ValueError("travel_guide_place_mention_source_fingerprint_invalid")
                if raw.get("guideEvidenceFingerprint") != guide_evidence_fingerprint:
                    raise ValueError("travel_guide_place_mention_evidence_fingerprint_invalid")
                if raw.get("verificationStatus") != "unresolved_amap_grounding":
                    raise ValueError("travel_guide_place_mention_status_invalid")
            else:
                supplied_fingerprints = raw.get("sourceFingerprints")
                if supplied_fingerprints is not None and supplied_fingerprints != source_fingerprints:
                    raise ValueError("travel_guide_place_mention_source_fingerprint_invalid")
                supplied_guide_fingerprint = raw.get("guideEvidenceFingerprint")
                if supplied_guide_fingerprint is not None and supplied_guide_fingerprint != guide_evidence_fingerprint:
                    raise ValueError("travel_guide_place_mention_evidence_fingerprint_invalid")
                supplied_status = raw.get("verificationStatus")
                if supplied_status is not None and supplied_status != "unresolved_amap_grounding":
                    raise ValueError("travel_guide_place_mention_status_invalid")
            key = (mention_text, intent_type, tuple(source_ref_ids))
            if key in seen:
                raise ValueError("travel_guide_place_mention_duplicate")
            seen.add(key)
            normalized.append(
                {
                    "mentionText": mention_text,
                    "intentType": intent_type,
                    "sourceRefIds": source_ref_ids,
                    "sourceFingerprints": source_fingerprints,
                    "guideEvidenceFingerprint": guide_evidence_fingerprint,
                    "verificationStatus": "unresolved_amap_grounding",
                }
            )
        return normalized

    @classmethod
    def _recover_legacy_entity_span(
        cls, original: str, *, intent_type: str, raw_ref_ids: Any, evidence_by_id: dict[str, dict[str, Any]]
    ) -> str:
        """Repair only a source-backed prose span; bindings are still checked by normalization."""
        if not original or not isinstance(raw_ref_ids, list) or not 1 <= len(raw_ref_ids) <= 3:
            return ""
        sources = [evidence_by_id.get(str(ref_id).strip()) for ref_id in raw_ref_ids]
        if any(
            source is None or not any(original in str(source.get(field) or "") for field in ("title", "summary"))
            for source in sources
        ):
            return ""
        candidates = list(
            dict.fromkeys(
                mention
                for source in sources
                for field in ("summary", "title")
                for mention in cls._place_mentions_in_text(source[field], intent_type)
                if mention in original
            )
        )
        return candidates[0] if len(candidates) == 1 else ""

    @classmethod
    def _deterministic_place_mentions(
        cls,
        *,
        admitted: list[dict[str, Any]],
        themes: list[tuple[str, str, tuple[str, ...]]],
    ) -> list[dict[str, Any]]:
        mentions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        limit = min(len(admitted), cls._PLACE_HINT_LIMIT)
        for source in admitted:
            for intent_type, _label, _aliases in themes:
                candidates = list(
                    dict.fromkeys(
                        cls._place_mentions_in_text(source["summary"], intent_type)
                        + cls._place_mentions_in_text(source["title"], intent_type)
                    )
                )
                if not candidates:
                    continue
                mention_text = candidates[0]
                key = (mention_text, intent_type)
                if key in seen:
                    continue
                seen.add(key)
                mentions.append(
                    {
                        "mentionText": mention_text,
                        "intentType": intent_type,
                        "sourceRefIds": [source["refId"]],
                    }
                )
                break
            if len(mentions) >= limit:
                break
        return mentions

    @classmethod
    def _legacy_conclusion_place_mentions(
        cls,
        *,
        conclusion: dict[str, Any],
        admitted: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        evidence_by_id = {item["refId"]: item for item in admitted}
        mentions: list[dict[str, Any]] = []
        limit = min(len(admitted), cls._PLACE_HINT_LIMIT)
        for takeaway in (conclusion.get("takeaways") or [])[: cls._PLACE_HINT_LIMIT]:
            if not isinstance(takeaway, dict):
                continue
            intent_type = str(takeaway.get("intentType") or "").strip()
            raw_ref_ids = takeaway.get("sourceRefIds")
            if intent_type not in cls._INTENT_THEMES or not isinstance(raw_ref_ids, list):
                continue
            source_ref_ids = list(dict.fromkeys(str(value).strip() for value in raw_ref_ids if str(value).strip()))[:3]
            if not source_ref_ids or any(ref_id not in evidence_by_id for ref_id in source_ref_ids):
                continue
            # Historical prose is only a ref/theme carrier. Derive names from
            # the validated source material using the same entity-span rules.
            candidates = list(
                dict.fromkeys(
                    mention
                    for ref_id in source_ref_ids
                    for field in ("summary", "title")
                    for mention in cls._place_mentions_in_text(evidence_by_id[ref_id][field], intent_type)
                )
            )
            for mention_text in candidates:
                if all(
                    cls._source_contains_mention(evidence_by_id[ref_id], mention_text, intent_type)
                    for ref_id in source_ref_ids
                ):
                    mentions.append(
                        {
                            "mentionText": mention_text,
                            "intentType": intent_type,
                            "sourceRefIds": source_ref_ids,
                        }
                    )
                    break
            if len(mentions) >= limit:
                break
        return mentions

    @classmethod
    def _place_mentions_in_text(cls, value: Any, intent_type: str) -> list[str]:
        text = cls._clean_text(value, limit=1600)
        # A timetable may omit a space after HH:mm. Keep the clock outside
        # the entity span without stripping legitimate numeric venue names.
        text = re.sub(r"(?<!\d)((?:[01]?\d|2[0-3])[:：][0-5]\d)(?=[\u3400-\u9fffA-Za-z])", r"\1 ", text)
        suffixes = cls._PLACE_SUFFIXES_BY_INTENT.get(intent_type) or ()
        if not text or not suffixes:
            return []
        suffix_pattern = "|".join(re.escape(value) for value in sorted(suffixes, key=len, reverse=True))
        pattern = re.compile(rf"(?P<name>[\u3400-\u9fffA-Za-z0-9·&（）()\-]{{1,40}}?(?:{suffix_pattern}))")
        mentions: list[str] = []
        previous_end = -1
        for match in pattern.finditer(text):
            raw_candidate = match.group("name")
            if match.start() == previous_end:
                raw_candidate = re.sub(r"^(?:以及|还有|或者|和|与|及)", "", raw_candidate)
            candidate = cls._trim_place_candidate(raw_candidate)
            previous_end = match.end()
            if (
                cls._specific_place_mention(candidate, intent_type)
                and cls._safe_entity_span(
                    candidate,
                    source_text=text,
                    suffix_end=match.end(),
                )
                and candidate in text
                and candidate not in mentions
            ):
                mentions.append(candidate)
        return mentions[:3]

    @classmethod
    def _safe_entity_span(cls, candidate: str, *, source_text: str, suffix_end: int) -> bool:
        """Keep only spans whose structural boundaries are unambiguous.

        The extractor must not turn sentence grammar into a place name, or
        truncate an attached facility into a different venue.  These checks
        intentionally reject uncertain text instead of maintaining a growing
        list of Chinese context words or venue names.
        """
        if re.search(r"[\u3400-\u9fff](?:的|有)[\u3400-\u9fff]", candidate):
            return False
        if any(
            len(generic) >= 4 and candidate != generic and candidate.endswith(generic)
            for generic in cls._GENERIC_PLACE_MENTIONS
        ):
            return False
        # Unknown continuous name components may identify a different venue or
        # facility. Accept only a closed span or an explicit prose boundary,
        # uniformly for every intent and for structured brand bindings.
        return cls._PLACE_ENTITY_RIGHT_BOUNDARY.match(source_text[suffix_end:]) is not None

    @classmethod
    def _trim_place_candidate(cls, value: str) -> str:
        candidate = str(value or "").strip("-—_·，。；：、()（） ")
        for connector in ("以及", "还有", "或者"):
            position = candidate.rfind(connector)
            if position >= 0 and position + len(connector) < len(candidate):
                candidate = candidate[position + len(connector) :]
        for marker in cls._PLACE_ACTION_BOUNDARIES:
            position = candidate.rfind(marker)
            if position >= 0 and position + len(marker) < len(candidate):
                candidate = candidate[position + len(marker) :]
        candidate = re.sub(
            r"^(?:上午|中午|下午|傍晚|晚上|夜间|首先|随后|然后|最后|适合|热门|可以|可)+",
            "",
            candidate,
        )
        # Single characters and numerals may be part of the proper name
        # (e.g. 和平公园 or 四季民福); never lstrip a bag of characters.
        return re.sub(r"^(?:第[一二三四五六七八九十\d]+站|[在从向]此|去逛|去|逛)", "", candidate).strip()

    @classmethod
    def _specific_place_mention(cls, mention_text: str, intent_type: str) -> bool:
        if (
            not mention_text
            or len(mention_text) < 3
            or len(mention_text) > cls._PLACE_MENTION_MAX_CHARS
            or mention_text in cls._GENERIC_PLACE_MENTIONS
            or not cls._valid_structured_place_mention(mention_text)
        ):
            return False
        suffixes = cls._PLACE_SUFFIXES_BY_INTENT.get(intent_type) or ()
        return bool(suffixes) and any(mention_text.endswith(suffix) for suffix in suffixes)

    @classmethod
    def _valid_structured_place_mention(cls, mention_text: str) -> bool:
        if (
            not mention_text
            or len(mention_text) < 2
            or len(mention_text) > cls._PLACE_MENTION_MAX_CHARS
            or mention_text in cls._GENERIC_PLACE_MENTIONS
            or cls._PLACE_NON_ENTITY_TEXT.search(mention_text)
            or cls._trim_place_candidate(mention_text) != mention_text
        ):
            return False
        return re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9·&（）()\- ]+", mention_text) is not None

    @classmethod
    def _source_contains_mention(cls, source: dict[str, Any], mention_text: str, intent_type: str) -> bool:
        suffixes = cls._PLACE_SUFFIXES_BY_INTENT.get(intent_type, ())
        if (
            intent_type in {"meal", "food_experience"}
            and mention_text.endswith(("店", "店家", "店铺"))
            and not any(mention_text.endswith(suffix) for suffix in suffixes)
        ):
            # A bare shop type does not establish a restaurant proper name;
            # do not re-admit it through the suffix-free brand fallback.
            return False
        for field in ("title", "summary"):
            text = str(source.get(field) or "")
            if mention_text in cls._place_mentions_in_text(text, intent_type):
                return True
            # Structured hints may name a brand without a venue suffix. Require
            # a complete source span, never an arbitrary substring of a name.
            if any(mention_text.endswith(suffix) for suffix in suffixes):
                continue
            for match in re.finditer(re.escape(mention_text), text):
                prefix = re.split(r"[^\u3400-\u9fffA-Za-z0-9·&（）()\-]", text[: match.start()])[-1]
                tail = text[match.end() :]
                if cls._trim_place_candidate(prefix + mention_text) != mention_text:
                    continue
                if cls._safe_entity_span(mention_text, source_text=text, suffix_end=match.end()):
                    return True
                if intent_type in {"meal", "food_experience"}:
                    for suffix in GUIDE_RESTAURANT_NAME_SUFFIXES:
                        if tail.startswith(suffix) and cls._safe_entity_span(
                            mention_text + suffix,
                            source_text=text,
                            suffix_end=match.end() + len(suffix),
                        ):
                            return True
        return False

    @staticmethod
    def _strict_fingerprint(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else ""

    @staticmethod
    def _timezone_aware_iso(value: str) -> bool:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    @classmethod
    def _evidence_conflicts(cls, admitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        pairs = (
            ("预约要求", ("需要预约", "需预约", "须预约"), ("无需预约", "免预约")),
            ("开放状态", ("开放", "正常开放"), ("闭馆", "关闭", "暂停开放")),
        )
        for topic, positive, negative in pairs:
            right = [item for item in admitted if any(token in item["searchText"] for token in negative)]
            right_ids = {item["refId"] for item in right}
            left = [
                item
                for item in admitted
                if item["refId"] not in right_ids and any(token in item["searchText"] for token in positive)
            ]
            if left and right:
                conflicts.append(
                    {
                        "topic": topic,
                        "summary": f"不同攻略摘要对{topic}的说法不一致。",
                        "sourceRefIds": list(dict.fromkeys([left[0]["refId"], right[0]["refId"]])),
                    }
                )
        return conflicts

    @staticmethod
    def _clean_text(value: Any, *, limit: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    @staticmethod
    def _missing_themes(
        results: list[Any],
        themes: list[tuple[str, str, tuple[str, ...]]],
    ) -> list[tuple[str, str, tuple[str, ...]]]:
        haystack = " ".join(f"{item.title} {item.snippet} {item.summary}" for item in results).lower()
        return [theme for theme in themes if not any(token.lower() in haystack for token in theme[2])]

    @classmethod
    def _safe_dedupe_items(cls, items: list[Any]) -> list[tuple[Any, str]]:
        deduped: list[tuple[Any, str]] = []
        seen: set[str] = set()
        for item in items:
            safe_url = cls._safe_source_url(item.url)
            key = safe_url.strip().rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append((item, safe_url))
        return deduped

    @classmethod
    def _safe_source_url(cls, value: Any) -> str:
        raw_url = str(value or "").strip()
        if not raw_url or len(raw_url) > 2048:
            return ""
        try:
            parsed = urlsplit(raw_url)
            hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
            if parsed.scheme.lower() not in {"http", "https"} or not hostname:
                return ""
            if parsed.username is not None or parsed.password is not None:
                return ""
            if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local")):
                return ""
            try:
                address = ipaddress.ip_address(hostname.strip("[]"))
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                return ""
            for key, _value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=64,
            ):
                normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(part in normalized_key for part in cls._SENSITIVE_QUERY_KEY_PARTS):
                    return ""
            return urlunsplit(
                (
                    parsed.scheme.lower(),
                    parsed.netloc,
                    parsed.path,
                    parsed.query,
                    "",
                )
            )
        except (UnicodeError, ValueError):
            return ""

    @staticmethod
    def _provider_names(responses: list[Any], attribute: str) -> list[str]:
        return list(
            dict.fromkeys(
                TravelGuideAdviceService._safe_provider_name(value)
                for response in responses
                for value in getattr(response, attribute, []) or []
                if str(value).strip()
            )
        )[:12]

    @staticmethod
    def _safe_provider_diagnostics(responses: list[Any]) -> list[dict[str, Any]]:
        safe: list[dict[str, Any]] = []
        allowed_statuses = {"success", "failed", "skipped", "no_results", "cache_hit"}
        count_keys = (
            "durationMs",
            "resultCount",
            "rawResultCount",
            "normalizedResultCount",
            "acceptedResultCount",
            "acceptedSourceCount",
            "rejectedResultCount",
        )
        for response in responses:
            for raw in getattr(response, "provider_diagnostics", []) or []:
                if not isinstance(raw, dict):
                    continue
                status = str(raw.get("status") or "").strip().lower()
                if status not in allowed_statuses:
                    status = "failed"
                reason_code = TravelGuideAdviceService._safe_reason_code(
                    raw.get("reasonCode") or raw.get("reason") or status
                )
                row: dict[str, Any] = {
                    "providerName": TravelGuideAdviceService._safe_provider_name(raw.get("providerName")),
                    "status": status,
                    "reason": reason_code,
                    "reasonCode": reason_code,
                }
                for key in count_keys:
                    value = raw.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        row[key] = min(value, 1_000_000)
                rejection_reasons = raw.get("rejectionReasons")
                if isinstance(rejection_reasons, list):
                    row["rejectionReasons"] = list(
                        dict.fromkeys(
                            TravelGuideAdviceService._safe_reason_code(value)
                            for value in rejection_reasons[:12]
                            if str(value).strip()
                        )
                    )
                if row:
                    safe.append(row)
                if len(safe) >= 16:
                    return safe
        return safe

    @classmethod
    def _safe_provider_name(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        aliases = {
            "any-search": "anysearch",
            "baidu": "baidu-html-search",
            "baidu-html": "baidu-html-search",
            "bing": "bing-html-search",
            "bing-html": "bing-html-search",
            "bocha": "bocha-web-search",
            "bochaai": "bocha-web-search",
            "brave": "brave-web-search",
            "ddg": "duckduckgo-html-search",
            "ddg-lite": "duckduckgo-lite-search",
            "duckduckgo": "duckduckgo-html-search",
            "duckduckgo-html": "duckduckgo-html-search",
            "duckduckgo-lite": "duckduckgo-lite-search",
            "duckduckgo-search": "ddgs",
            "free": "multi-free-search",
            "free-search": "multi-free-search",
            "google": "google-cse",
            "google-programmable-search": "google-cse",
            "html-multi": "multi-free-search",
            "multi": "multi-free-search",
            "multi-free": "multi-free-search",
        }
        normalized = aliases.get(normalized, normalized)
        return normalized if normalized in cls._SAFE_PROVIDER_NAMES else "web-search-provider"

    @classmethod
    def _safe_reason_code(cls, value: Any) -> str:
        normalized = re.sub(r"[^a-z0-9_\-]", "_", str(value or "").strip().lower())[:64]
        return normalized if normalized in cls._SAFE_REASON_CODES else "provider_error"

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional
from urllib.parse import urlparse


HOLIDAY_RISK_TERMS = ["国庆", "十一", "黄金周", "官方公告", "限流", "预约", "临时闭园", "施工", "交通管制", "人流", "安全提示"]
DEFAULT_RISK_TERMS = ["近期", "官方公告", "临时闭园", "施工", "限流", "预约", "交通管制", "安全提示"]


@dataclass(frozen=True)
class RiskQuery:
    query: str
    freshness: str
    source_preference: str
    reason: str
    target_year: Optional[int] = None
    target_date_range: Optional[str] = None
    holiday_name: Optional[str] = None
    queries: tuple[str, ...] = ()


class RiskQueryBuilder:
    def build(
        self,
        city: str,
        poi_name: str,
        segment_date: Optional[str] = None,
        segment_start_time: Optional[str] = None,
        segment_end_time: Optional[str] = None,
        risk_context: Optional[dict[str, Any]] = None,
        poi_category: str = "",
    ) -> RiskQuery:
        context = risk_context or {}
        resolved = context.get("resolvedTripDates") if isinstance(context.get("resolvedTripDates"), dict) else {}
        travel_date_range = context.get("travelDateRange") if isinstance(context.get("travelDateRange"), dict) else {}
        dates = [str(item) for item in (resolved.get("dates") or []) if item]
        start = resolved.get("startDate") or travel_date_range.get("start") or segment_date or (dates[0] if dates else None)
        end = resolved.get("endDate") or travel_date_range.get("end") or (dates[-1] if dates else start)
        target_year = self._year(start)
        holiday_name = str(resolved.get("holidayName") or "")
        is_holiday = bool(resolved.get("holidayInferred")) or holiday_name or self._is_national_day(dates or [str(start or "")])
        range_text = self._range_text(start, end)
        user_terms = [str(item) for item in (context.get("riskPriorityTerms") or []) if str(item).strip()]
        year_text = str(target_year or "")
        if is_holiday:
            official_terms = [holiday_name or "国庆", "十一", "黄金周", "官方公告", "预约", "限流", *user_terms[:2]]
            policy_terms = [holiday_name or "国庆", "文旅", "交通管制", "人流", "安全提示"]
        else:
            official_terms = ["官方公告", "预约", "开放时间", "限流", *user_terms[:2]]
            policy_terms = ["文旅", "交通管制", "人流", "安全提示"]
        queries = self._dedupe_queries(
            [
                self._limit_query(" ".join(part for part in [city, poi_name, year_text, range_text, *official_terms] if part)),
                self._limit_query(" ".join(part for part in [city, poi_name, year_text, *policy_terms] if part)),
                self._limit_query(" ".join(part for part in [poi_name, "预约", "开放时间", "官方"] if part)),
                self._site_query(city, poi_name, year_text, is_holiday, poi_category),
            ]
        )
        return RiskQuery(
            query=queries[0],
            freshness="oneYear" if target_year and target_year >= date.today().year else "noLimit",
            source_preference="official_first",
            reason="holiday_risk_query" if is_holiday else "poi_risk_query",
            target_year=target_year,
            target_date_range=range_text or None,
            holiday_name=holiday_name or ("国庆节" if is_holiday else None),
            queries=tuple(queries),
        )

    def _dedupe_queries(self, queries: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for query in queries:
            normalized = " ".join(str(query or "").split())
            key = normalized.casefold()
            if normalized and key not in seen:
                deduped.append(normalized)
                seen.add(key)
        return deduped

    def _range_text(self, start: Any, end: Any) -> str:
        start_text = str(start or "")
        end_text = str(end or "")
        if not start_text:
            return ""
        start_match = re.match(r"(20\d{2})-(\d{2})-(\d{2})", start_text)
        end_match = re.match(r"(20\d{2})-(\d{2})-(\d{2})", end_text)
        if not start_match:
            return start_text
        if not end_match or start_text == end_text:
            return f"{int(start_match.group(2))}月{int(start_match.group(3))}日"
        return f"{int(start_match.group(2))}月{int(start_match.group(3))}日 {int(end_match.group(2))}月{int(end_match.group(3))}日"

    def _limit_query(self, query: str, max_chars: int = 180) -> str:
        query = " ".join(str(query or "").split())
        if len(query) <= max_chars:
            return query
        parts: list[str] = []
        total = 0
        for token in query.split():
            next_total = total + len(token) + (1 if parts else 0)
            if next_total > max_chars:
                break
            parts.append(token)
            total = next_total
        return " ".join(parts) or query[:max_chars]

    def _site_query(self, city: str, poi_name: str, year_text: str, is_holiday: bool, poi_category: str) -> str:
        text = f"{poi_name} {poi_category}"
        if re.search(r"(大学|学院|高校|校园|高等院校|education)", text, re.IGNORECASE):
            return self._limit_query(" ".join(part for part in [poi_name, year_text, "国庆" if is_holiday else "", "访客预约", "校园开放", "site:edu.cn"] if part))
        if re.search(r"(博物馆|景区|公园|场馆|风景名胜)", text):
            return self._limit_query(" ".join(part for part in [city, poi_name, year_text, "官方公告", "预约", "限流", "临时闭园"] if part))
        return ""

    def _year(self, value: Any) -> Optional[int]:
        match = re.match(r"(20\d{2})", str(value or ""))
        return int(match.group(1)) if match else None

    def _is_national_day(self, dates: list[str]) -> bool:
        return any(str(item)[5:10] in {"10-01", "10-02", "10-03", "10-04", "10-05", "10-06", "10-07"} for item in dates)


class RiskSourceScorer:
    def score(
        self,
        item: Any,
        target_year: Optional[int],
        official_source_first: bool = True,
        city: str = "",
        poi_name: str = "",
    ) -> dict[str, Any]:
        text = " ".join(
            str(value or "")
            for value in [
                getattr(item, "title", ""),
                getattr(item, "snippet", ""),
                getattr(item, "url", ""),
                getattr(item, "source_name", ""),
            ]
        )
        years = self._extract_years(text)
        credibility = self._credibility_rank(getattr(item, "url", ""), text)
        stale = bool(target_year and years and max(years) < target_year)
        undated = not years
        poi_mismatch = bool(poi_name and not self._matches_poi(text, poi_name))
        city_mismatch = bool(city and self._mentions_other_city_without_target(text, city))
        risk_relevance = self._has_risk_relevance(text)
        confidence = float(getattr(item, "confidence", 0.0) or 0.0)
        if official_source_first and credibility == "official":
            confidence += 0.18
        if stale:
            confidence -= 0.45
        elif target_year and target_year in years:
            confidence += 0.12
        elif undated:
            confidence -= 0.18
        if credibility in {"guide", "social", "unknown"}:
            confidence -= 0.12
        if credibility == "social":
            confidence -= 0.45
        if poi_mismatch:
            confidence -= 0.4
        if city_mismatch:
            confidence -= 0.25
        if not risk_relevance:
            confidence -= 0.18
        confidence = max(0.0, min(0.95, confidence))
        accepted = (
            confidence >= 0.45
            and not stale
            and not (undated and credibility not in {"official", "ota_aggregator"})
            and credibility != "social"
            and not poi_mismatch
            and not city_mismatch
            and risk_relevance
        )
        return {
            "sourceDate": str(max(years)) if years else None,
            "targetDateRelevance": "stale_for_target_date" if stale else "target_year" if target_year and target_year in years else "undated" if undated else "dated",
            "credibilityRank": credibility,
            "stalenessReason": f"source_year_before_target_{target_year}" if stale else None,
            "relevanceReason": "poi_mismatch" if poi_mismatch else "city_mismatch" if city_mismatch else "low_risk_relevance" if not risk_relevance else "risk_relevant",
            "confidence": confidence,
            "accepted": accepted,
        }

    def _extract_years(self, text: str) -> list[int]:
        return [int(match.group(1)) for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", text)]

    def _credibility_rank(self, url: str, text: str) -> str:
        host = urlparse(str(url or "")).netloc.lower()
        lowered = f"{host} {text}".lower()
        if any(token in lowered for token in ("gov.cn", ".gov", "官方", "官网", "文旅", "管理处", "管理委员会")):
            return "official"
        if any(token in lowered for token in ("ticket", "ctrip", "meituan", "fliggy", "携程", "美团", "飞猪", "预约", "购票", "门票")):
            return "ota_aggregator"
        if any(token in lowered for token in ("xiaohongshu", "mafengwo", "攻略", "游记", "小红书", "马蜂窝")):
            return "guide"
        if any(token in lowered for token in ("weibo", "douyin", "bilibili", "instagram", "facebook", "tiktok", "pinterest", "twitter", "x.com", "微博", "抖音")):
            return "social"
        return "search" if host else "unknown"

    def _matches_poi(self, text: str, poi_name: str) -> bool:
        lowered = text.lower()
        tokens = [token for token in re.split(r"[\s,，。/|()（）-]+", str(poi_name or "")) if len(token) >= 2]
        expanded: list[str] = []
        for token in tokens:
            expanded.append(token)
            stripped = re.sub(r"(大学|学院|博物馆|公园|景区|广场|故宫|长城)$", "", token)
            if len(stripped) >= 2:
                expanded.append(stripped)
        return any(token.lower() in lowered for token in expanded)

    def _mentions_other_city_without_target(self, text: str, city: str) -> bool:
        cities = {"北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "西安", "重庆", "武汉", "苏州"}
        target = str(city or "").replace("市", "")
        mentioned = {item for item in cities if item in text}
        return bool(mentioned and target and target not in mentioned)

    def _has_risk_relevance(self, text: str) -> bool:
        return bool(
            re.search(
                r"(官方|公告|预约|开放|限流|闭园|闭馆|交通管制|管控|安全提示|国庆|十一|黄金周|访客|参观|门票|购票|营业|排队|"
                r"reservation|visitor|opening|closed|closure|ticket)",
                text,
                re.IGNORECASE,
            )
        )

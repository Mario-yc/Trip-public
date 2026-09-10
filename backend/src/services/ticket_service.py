import re
import sqlite3
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from src.models.ticket_lookup_result import TicketLookupResult
from src.providers.travel_tools import ResilientWebSearchProvider, WebSearchResponse
from src.services.source_ranking_service import SourceRankingService


TICKET_CAVEAT = "查询结果仅供参考，请以官方预约渠道为准。"
NO_TRUSTED_SOURCE_CAVEAT = "联网搜索未返回可用真实来源，请以官方渠道确认为准。"
INITIAL_TICKET_CAVEAT = "初始行程未全量查询预约状态；可在需要时手动刷新，或由 Agent 针对高风险/明确要求的地点查询。"
AREA_TICKET_CAVEAT = "请选择具体场馆/入口/区域后再查询预约。开放区域未发现统一预约入口，节假日管控需以官方公告为准。"
FUNCTIONAL_TICKET_CAVEAT = "等待 Agent 按附近搜索补全具体地点后再查询预约。"
FUNCTIONAL_POI_RE = re.compile(r"(午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶|夜景|休息|购物|漫步)")
AREA_POI_RE = re.compile(r"(周边|附近|周围|一带|区域|商圈|片区|街区|胡同|园区)")
KNOWN_AREA_POI_NAMES = (
    "奥林匹克公园",
    "什刹海",
    "后海",
    "南锣鼓巷",
    "前门大街",
    "王府井",
    "国贸",
    "陆家嘴",
    "外滩",
    "珠江新城",
    "深圳湾",
    "华强北",
    "五道口",
)


class TicketService:
    def __init__(
        self,
        db: sqlite3.Connection,
        default_available: bool = False,
        web_search_provider: Optional[ResilientWebSearchProvider] = None,
    ):
        self.db = db
        self.default_available = default_available
        self.ranking = SourceRankingService()
        self.web_search_provider = web_search_provider or ResilientWebSearchProvider()

    def refresh_for_plan(self, plan_id: str) -> list[TicketLookupResult]:
        segment_rows = self.db.execute(
            """
            SELECT s.id AS segment_id, s.estimated_cost, p.name AS poi_name, p.category
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ?
            ORDER BY s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        segment_ids = [row["segment_id"] for row in segment_rows]
        if not segment_ids:
            return []

        placeholders = ",".join("?" for _ in segment_ids)
        self.db.execute(f"DELETE FROM ticket_lookup_results WHERE segment_id IN ({placeholders})", segment_ids)

        results: list[TicketLookupResult] = []
        for row in segment_rows:
            results.extend(self._lookup_segment(row["segment_id"], row["poi_name"], row["category"], row["estimated_cost"]))

        sorted_results = self.ranking.sort_by_credibility(results)
        self.persist_results(sorted_results)
        self.db.commit()
        return sorted_results

    def build_for_segments(self, segments: list, pois: list) -> list[TicketLookupResult]:
        pois_by_id = {poi.id: poi for poi in pois}
        results: list[TicketLookupResult] = []
        for segment in segments:
            poi = pois_by_id.get(segment.poi_id)
            if poi is None:
                continue
            results.extend(self._lookup_segment(segment.id, poi.name, poi.category, segment.estimated_cost))
        return self.ranking.sort_by_credibility(results)

    def build_pending_for_segments(self, segments: list, pois: list) -> list[TicketLookupResult]:
        pois_by_id = {poi.id: poi for poi in pois}
        results: list[TicketLookupResult] = []
        for segment in segments:
            poi = pois_by_id.get(segment.poi_id)
            if poi is None:
                continue
            guarded = self._non_concrete_ticket_result(segment.id, poi.name, poi.category)
            if guarded is not None:
                results.append(guarded)
                continue
            results.append(self._pending_ticket_result(segment.id, poi.category))
        return results

    def persist_results(self, results: list[TicketLookupResult]) -> None:
        for result in results:
            self._insert(result)

    def _lookup_segment(
        self, segment_id: str, poi_name: str, category: str, estimated_cost: float
    ) -> list[TicketLookupResult]:
        non_concrete = self._non_concrete_ticket_result(segment_id, poi_name, category)
        if non_concrete is not None:
            return [non_concrete]
        search = self.web_search_provider.search(f"{poi_name} 开放时间 预约 官方公告", count=3, freshness="oneYear")
        provider_name = search.provider_name
        fallback_used = search.fallback_used
        failure_reason = search.failure_reason
        now = datetime.now(timezone.utc)
        ticket_type = "reservation"
        price = max(float(estimated_cost), 0.0)
        source_rows = self._ticket_sources(search, poi_name)
        if failure_reason or not search.results:
            return [
                TicketLookupResult(
                    id=f"ticket_{uuid4().hex[:12]}",
                    segment_id=segment_id,
                    ticket_type=ticket_type,
                    status="unknown",
                    price_estimate=0.0,
                    booking_url="",
                    source_name="联网搜索失败",
                    source_url="",
                    credibility_rank="unavailable",
                    caveat=NO_TRUSTED_SOURCE_CAVEAT,
                    provider_name=provider_name,
                    fallback_used=False,
                    confidence=min(0.35, search.confidence),
                    provider_failure_reason=failure_reason or search.user_visible_caveat,
                    queried_at=now,
                )
            ]
        if not source_rows:
            return [
                TicketLookupResult(
                    id=f"ticket_{uuid4().hex[:12]}",
                    segment_id=segment_id,
                    ticket_type=ticket_type,
                    status="unknown",
                    price_estimate=0.0,
                    booking_url="",
                    source_name="联网搜索失败",
                    source_url="",
                    credibility_rank="unavailable",
                    caveat=NO_TRUSTED_SOURCE_CAVEAT,
                    provider_name=provider_name,
                    fallback_used=False,
                    confidence=min(0.35, search.confidence),
                    provider_failure_reason="联网搜索结果缺少可用 URL。",
                    queried_at=now,
                )
            ]

        ticket_results: list[TicketLookupResult] = []
        for index, source in enumerate(source_rows):
            is_primary = index == 0
            official = source["official"] == "true"
            ticket_results.append(
                TicketLookupResult(
                    id=f"ticket_{uuid4().hex[:12]}",
                    segment_id=segment_id,
                    ticket_type=ticket_type,
                    # Category is not evidence that an attraction requires a booking.
                    status="reservation_required" if official and source["reservation_evidence"] == "true" else "unknown",
                    price_estimate=price,
                    booking_url=source["url"] if official else "",
                    source_name=source["name"],
                    source_url=source["url"],
                    credibility_rank=source["credibility_rank"],
                    caveat=search.user_visible_caveat or TICKET_CAVEAT,
                    provider_name=provider_name,
                    fallback_used=fallback_used,
                    confidence=float(source["confidence"]),
                    provider_failure_reason=failure_reason,
                    queried_at=now,
                )
            )
        return ticket_results

    def _non_concrete_ticket_result(self, segment_id: str, poi_name: str, category: str) -> Optional[TicketLookupResult]:
        specificity = self._ticket_specificity(poi_name, category)
        if specificity == "exact_entity":
            return None
        now = datetime.now(timezone.utc)
        caveat = FUNCTIONAL_TICKET_CAVEAT if specificity == "functional_poi" else AREA_TICKET_CAVEAT
        source_name = "等待具体地点" if specificity == "functional_poi" else "需要选择具体场馆/入口/区域"
        return TicketLookupResult(
            id=f"ticket_{uuid4().hex[:12]}",
            segment_id=segment_id,
            ticket_type="reservation",
            status="needs_concrete_poi",
            price_estimate=0.0,
            booking_url="",
            source_name=source_name,
            source_url="",
            credibility_rank="unavailable",
            caveat=caveat,
            provider_name="local-ticket-guard",
            fallback_used=False,
            confidence=0.0,
            provider_failure_reason=None,
            queried_at=now,
        )

    def _pending_ticket_result(self, segment_id: str, category: str) -> TicketLookupResult:
        ticket_type = "reservation" if self._requires_reservation(category) else "attraction"
        return TicketLookupResult(
            id=f"ticket_{uuid4().hex[:12]}",
            segment_id=segment_id,
            ticket_type=ticket_type,
            status="not_checked",
            price_estimate=0.0,
            booking_url="",
            source_name="预约未查询",
            source_url="",
            credibility_rank="unavailable",
            caveat=INITIAL_TICKET_CAVEAT,
            provider_name="local-ticket-guard",
            fallback_used=False,
            confidence=0.0,
            provider_failure_reason=None,
            queried_at=datetime.now(timezone.utc),
        )

    def _ticket_specificity(self, poi_name: str, category: str) -> str:
        name = str(poi_name or "")
        normalized_category = str(category or "").lower()
        has_functional = bool(FUNCTIONAL_POI_RE.search(name))
        normalized_name = re.sub(r"[\s\-_,.()（）·・，。]+", "", name).casefold()
        has_area = (
            bool(AREA_POI_RE.search(name))
            or "area" in normalized_category
            or "pending" in normalized_category
            or any(re.sub(r"[\s\-_,.()（）·・，。]+", "", area).casefold() in normalized_name for area in KNOWN_AREA_POI_NAMES)
        )
        functional_only = {"午餐", "晚餐", "早餐", "早饭", "中饭", "午饭", "吃饭", "用餐", "餐厅", "美食", "咖啡", "下午茶", "夜景", "休息", "购物", "漫步"}
        if normalized_name in {re.sub(r"[\s\-_,.()（）·・，。]+", "", value).casefold() for value in functional_only}:
            return "functional_poi"
        if has_functional and (has_area or len(normalized_name) > 4):
            return "composite_poi"
        if has_area:
            return "area_poi"
        return "exact_entity"

    def _ticket_sources(self, search: WebSearchResponse, poi_name: str) -> list[dict[str, str]]:
        rows = []
        for item in search.results[:3]:
            if not item.url:
                continue
            evidence_text = " ".join([str(item.title or ""), str(item.snippet or ""), str(item.summary or "")])
            if re.search(r"(夏令营|招生|研究生报名|本科招生|留学|培训班|招聘)", evidence_text):
                continue
            reservation_evidence = bool(re.search(r"(访客参观|校园开放|参观预约|预约参观|实名登记|提前预约|门票预约)", evidence_text))
            host = (urlparse(item.url).hostname or "").lower()
            normalized_poi_name = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", poi_name.casefold())
            normalized_evidence = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", evidence_text.casefold())
            identity_matches = bool(normalized_poi_name and normalized_poi_name in normalized_evidence)
            official = (
                str(item.credibility_rank or "") == "official"
                and identity_matches
                and (host.endswith(".gov.cn") or host.endswith(".edu.cn") or host.endswith(".org.cn") or "official" in host or host.endswith("dpm.org.cn"))
            )
            rows.append(
                {
                    "name": item.title or item.source_name or f"{poi_name}公开搜索结果",
                    "url": item.url,
                    "credibility_rank": item.credibility_rank or "search",
                    "confidence": str(item.confidence or search.confidence or 0.45),
                    "reservation_evidence": "true" if reservation_evidence else "false",
                    "official": "true" if official else "false",
                }
            )
        return rows

    def _requires_reservation(self, category: str) -> bool:
        normalized = (category or "").lower()
        return any(
            keyword in normalized
            for keyword in (
                "attraction",
                "scenic",
                "museum",
                "park",
                "景点",
                "风景",
                "名胜",
                "博物馆",
                "公园",
            )
        )

    def _insert(self, result: TicketLookupResult) -> None:
        self.db.execute(
            """
            INSERT INTO ticket_lookup_results (
                id, segment_id, ticket_type, status, price_estimate,
                booking_url, source_name, source_url, credibility_rank,
                queried_at, caveat, provider_name, fallback_used,
                provider_failure_reason, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.id,
                result.segment_id,
                result.ticket_type,
                result.status,
                result.price_estimate,
                result.booking_url,
                result.source_name,
                result.source_url,
                result.credibility_rank,
                result.queried_at.isoformat(),
                result.caveat,
                result.provider_name,
                1 if result.fallback_used else 0,
                result.provider_failure_reason,
                result.confidence,
            ),
        )

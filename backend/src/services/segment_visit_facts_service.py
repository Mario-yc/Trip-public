import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

from src.core.config import get_settings
from src.providers.travel_tools import ResilientWebSearchProvider, WebSearchItem, WebSearchResponse
from src.services.map_poi_service import MapPoiService


FACT_KEYS = ("openingHours", "reservation", "ticketPrice", "ticketRelease")


class SegmentVisitFactsService:
    """Refresh expiring visit facts outside itinerary version writes."""

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        provider: Optional[ResilientWebSearchProvider] = None,
        map_poi_service: Optional[MapPoiService] = None,
    ):
        self.db = db
        self.provider = provider or ResilientWebSearchProvider()
        self.map_poi_service = map_poi_service or MapPoiService()

    def refresh_for_plan(self, plan_id: str) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT s.id AS segment_id, p.amap_id, p.name AS poi_name, d.date AS visit_date
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            JOIN itinerary_days d ON d.id = s.day_id
            WHERE s.plan_id = ?
            ORDER BY d.day_number, s.segment_order
            """,
            (plan_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail={"code": "itinerary_not_found", "message": "行程不存在。"})
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=get_settings().visit_facts_max_age_hours)
        for row in rows:
            self._refresh_segment(
                plan_id=plan_id,
                segment_id=str(row["segment_id"]),
                amap_poi_id=str(row["amap_id"] or ""),
                poi_name=str(row["poi_name"] or ""),
                visit_date=str(row["visit_date"] or "date_pending"),
                queried_at=now,
                expires_at=expires_at,
            )
        self.db.commit()
        return self.load_for_plan(plan_id)

    def load_for_plan(self, plan_id: str) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT * FROM segment_visit_facts
            WHERE plan_id = ? ORDER BY queried_at DESC
            """,
            (plan_id,),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        now = datetime.now(timezone.utc)
        for row in rows:
            if str(row["segment_id"]) in result:
                continue
            expires_at = self._parse_time(str(row["expires_at"]))
            facts = self._json_object(row["facts_json"])
            if expires_at <= now:
                facts = {
                    key: {**dict(value), "status": "unknown", "caveat": "该信息已过期，请刷新后再确认。"}
                    for key, value in facts.items()
                    if isinstance(value, dict)
                }
            result[str(row["segment_id"])] = {
                "segmentId": str(row["segment_id"]),
                "amapPoiId": str(row["amap_poi_id"]),
                "visitDate": str(row["visit_date"]),
                "refreshStatus": "expired" if expires_at <= now else str(row["refresh_status"]),
                "facts": facts,
                "sourceRefs": self._json_list(row["source_refs_json"]),
                "evidenceFingerprint": str(row["evidence_fingerprint"]),
                "queriedAt": str(row["queried_at"]),
                "expiresAt": str(row["expires_at"]),
            }
        return result

    def resolve_visit_facts(
        self,
        *,
        amap_poi_id: str,
        poi_name: str,
        visit_date: str,
        queried_at: Optional[datetime] = None,
        expires_at: Optional[datetime] = None,
        web_query_budget: int = 2,
    ) -> dict[str, Any]:
        """Resolve one POI/date without performing any itinerary write."""

        queried_at = queried_at or datetime.now(timezone.utc)
        expires_at = expires_at or (queried_at + timedelta(hours=get_settings().visit_facts_max_age_hours))
        if not amap_poi_id:
            facts = self._empty_facts(
                "地点尚未绑定唯一高德 POI，不能查询到访事实。",
                queried_at,
                expires_at,
            )
            return self._resolved_payload(
                amap_poi_id=amap_poi_id,
                visit_date=visit_date,
                refresh_status="failed",
                facts=facts,
                refs=[],
                queried_at=queried_at,
                expires_at=expires_at,
            )
        amap_opening = self._amap_opening_evidence(amap_poi_id, visit_date=visit_date)
        searches: list[WebSearchResponse] = []
        if web_query_budget > 0:
            searches.append(
                self.provider.search(
                    f"{poi_name} 官方 开放时间 预约 门票 放票 {visit_date}",
                    count=5,
                    freshness="oneMonth",
                )
            )
        facts, refs = self._extract(
            searches,
            queried_at,
            expires_at,
            amap_opening=amap_opening,
            visit_date=visit_date,
        )
        if web_query_budget > 1 and any(facts[key]["status"] in {"unknown", "failed"} for key in FACT_KEYS):
            searches.append(
                self.provider.search(
                    f"{poi_name} 官方 预约 门票 放票时间",
                    count=4,
                    freshness="oneMonth",
                )
            )
            facts, refs = self._extract(
                searches,
                queried_at,
                expires_at,
                amap_opening=amap_opening,
                visit_date=visit_date,
            )
        failed = bool(searches) and all(not search.results for search in searches) and amap_opening is None
        refresh_status = (
            "failed"
            if failed
            else "partial"
            if any(facts[key]["status"] in {"unknown", "failed"} for key in FACT_KEYS)
            else "completed"
        )
        return self._resolved_payload(
            amap_poi_id=amap_poi_id,
            visit_date=visit_date,
            refresh_status=refresh_status,
            facts=facts,
            refs=refs,
            queried_at=queried_at,
            expires_at=expires_at,
        )

    def _refresh_segment(
        self,
        *,
        plan_id: str,
        segment_id: str,
        amap_poi_id: str,
        poi_name: str,
        visit_date: str,
        queried_at: datetime,
        expires_at: datetime,
    ) -> None:
        cached = self.db.execute(
            """
            SELECT amap_poi_id, visit_date, expires_at
            FROM segment_visit_facts
            WHERE plan_id = ? AND segment_id = ?
            ORDER BY queried_at DESC
            LIMIT 1
            """,
            (plan_id, segment_id),
        ).fetchone()
        if (
            cached is not None
            and str(cached["amap_poi_id"] or "") == amap_poi_id
            and str(cached["visit_date"] or "") == visit_date
            and self._parse_time(str(cached["expires_at"])) > queried_at
        ):
            return
        if not amap_poi_id:
            facts = self._empty_facts("地点尚未绑定唯一高德 POI，不能查询到访事实。", queried_at, expires_at)
            self._persist(plan_id, segment_id, "", visit_date, "failed", facts, [], queried_at, expires_at)
            return
        resolved = self.resolve_visit_facts(
            amap_poi_id=amap_poi_id,
            poi_name=poi_name,
            visit_date=visit_date,
            queried_at=queried_at,
            expires_at=expires_at,
        )
        self._persist(
            plan_id,
            segment_id,
            amap_poi_id,
            visit_date,
            str(resolved["refreshStatus"]),
            dict(resolved["facts"]),
            list(resolved["sourceRefs"]),
            queried_at,
            expires_at,
        )

    def _extract(
        self,
        searches: list[WebSearchResponse],
        queried_at: datetime,
        expires_at: datetime,
        *,
        amap_opening: Optional[tuple[str, dict[str, Any], dict[str, Any]]] = None,
        visit_date: Optional[str] = None,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        items = [item for search in searches for item in search.results if item.url]
        refs = [self._source_ref(item) for item in items[:8]]
        if amap_opening is not None:
            refs.append(amap_opening[1])
        facts = self._empty_facts("联网证据未明确说明该项，请以官方页面为准。", queried_at, expires_at)
        patterns = {
            "openingHours": re.compile(r"(?:开放时间|营业时间)[：:\s]*([^。；;]{3,80})"),
            "reservation": re.compile(r"((?:需要|需|须|提前|实名).{0,12}预约|无需预约|免预约)"),
            "ticketPrice": re.compile(r"((?:门票|票价)[：:\s]*(?:免费|免票|¥?￥?\s*\d+(?:\.\d+)?\s*元?))"),
            "ticketRelease": re.compile(r"((?:放票|开票|预约开放).{0,30}(?:每天|每日|提前|点|时|日))"),
        }
        # Lower priority number wins when sources agree. Disagreement is never
        # silently overwritten: official web evidence, AMap place metadata and
        # advisory pages are all retained in the conflicting result.
        candidates: dict[str, list[tuple[str, dict[str, Any], int, Optional[str]]]] = {key: [] for key in FACT_KEYS}
        for item in items:
            text = re.sub(r"\s+", " ", f"{item.title} {item.snippet} {item.summary}").strip()
            official = self._official(item)
            source_ref = self._source_ref(item)
            for key, pattern in patterns.items():
                match = pattern.search(text)
                if match:
                    effective_date = (
                        visit_date
                        if key == "openingHours"
                        and official
                        and visit_date
                        and self._mentions_visit_date(text, visit_date)
                        else None
                    )
                    candidates[key].append((match.group(1).strip(), source_ref, 0 if official else 2, effective_date))
            if (
                official
                and visit_date
                and self._mentions_visit_date(text, visit_date)
                and any(token in text for token in ("闭馆", "暂停开放", "不开放"))
            ):
                closure = next(token for token in ("闭馆", "暂停开放", "不开放") if token in text)
                candidates["openingHours"].append((closure, source_ref, 0, visit_date))
        if amap_opening is not None:
            candidates["openingHours"].append((amap_opening[0], amap_opening[1], 1, visit_date))
        for key in FACT_KEYS:
            values = sorted(candidates[key], key=lambda value: value[2])
            if not values:
                continue
            distinct = list(dict.fromkeys(value for value, _ref, _priority, _date in values))
            selected, _source_ref, selected_priority, selected_effective_date = values[0]
            status = "conflicting" if len(distinct) > 1 else "advisory" if selected_priority == 2 else "verified"
            structured_opening = (
                amap_opening[2]
                if key == "openingHours" and amap_opening is not None and selected == amap_opening[0]
                else {"intervals": self._opening_intervals_for_date(selected, selected_effective_date)}
                if key == "openingHours" and selected_effective_date
                else {}
            )
            facts[key] = {
                "status": status,
                "valueText": "；".join(distinct[:3]) if status == "conflicting" else selected,
                "structuredValue": (
                    {
                        "providerText": selected,
                        **(structured_opening),
                    }
                    if key == "openingHours"
                    else None
                ),
                "effectiveForDate": (selected_effective_date if key == "openingHours" else None),
                "sourceRefs": [ref for _value, ref, _priority, _date in values[:3]],
                "queriedAt": queried_at.isoformat(),
                "expiresAt": expires_at.isoformat(),
                "caveat": (
                    "不同来源存在冲突，请在出发前查看官方最新公告。"
                    if status == "conflicting"
                    else None
                    if status == "verified"
                    else "来自非官方网页摘要，仅作经验性提示。"
                ),
            }
        return facts, refs

    @staticmethod
    def _mentions_visit_date(text: str, visit_date: str) -> bool:
        try:
            parsed = datetime.fromisoformat(visit_date)
        except ValueError:
            return False
        variants = (
            visit_date,
            visit_date.replace("-", "/"),
            f"{parsed.year}年{parsed.month}月{parsed.day}日",
            f"{parsed.month}月{parsed.day}日",
            f"{parsed.month}月{parsed.day}号",
        )
        return any(value in text for value in variants)

    def _amap_opening_evidence(
        self,
        amap_poi_id: str,
        *,
        visit_date: Optional[str] = None,
    ) -> Optional[tuple[str, dict[str, Any], dict[str, Any]]]:
        try:
            poi = self.map_poi_service.detail(amap_poi_id)
        except (HTTPException, OSError, RuntimeError, ValueError):
            return None
        # `open_time_today` describes the provider's query day, not the
        # itinerary visit day. Without a same-day proof it must not be promoted
        # to verified evidence for a future visit.
        opening = str(poi.open_time_week or "").strip()
        if not opening:
            return None
        effective_date = visit_date or datetime.now(timezone.utc).date().isoformat()
        intervals = self._opening_intervals_for_date(opening, effective_date)
        return (
            opening,
            {
                "title": f"{poi.name or amap_poi_id} 高德场所详情",
                "url": None,
                "sourceName": "高德地图",
                "credibilityRank": "map_provider",
                "amapPoiId": amap_poi_id,
                "queriedAt": poi.provider_queried_at.isoformat() if poi.provider_queried_at else None,
                "effectiveForDate": effective_date,
            },
            {"intervals": intervals},
        )

    @staticmethod
    def _opening_intervals_for_date(value: str, visit_date: str) -> list[dict[str, str]]:
        """Conservatively expose machine-checkable HH:mm ranges from weekly text."""

        try:
            weekday = datetime.fromisoformat(visit_date).weekday() + 1
        except ValueError:
            return []
        weekday_tokens = {
            1: ("周一", "星期一"),
            2: ("周二", "星期二"),
            3: ("周三", "星期三"),
            4: ("周四", "星期四"),
            5: ("周五", "星期五"),
            6: ("周六", "星期六"),
            7: ("周日", "周天", "星期日", "星期天"),
        }
        chunks = [chunk.strip() for chunk in re.split(r"[；;]", value) if chunk.strip()]
        relevant = [
            chunk
            for chunk in chunks
            if any(token in chunk for token in weekday_tokens[weekday])
            or "周一至周日" in chunk
            or "周一到周日" in chunk
            or "每天" in chunk
            or "每日" in chunk
        ]
        candidates = relevant or chunks
        intervals: list[dict[str, str]] = []
        for chunk in candidates:
            for start, end in re.findall(r"([0-2]?\d:[0-5]\d)\s*[-—至到]\s*([0-2]?\d:[0-5]\d)", chunk):
                intervals.append({"start": start.zfill(5), "end": end.zfill(5)})
        return intervals[:4]

    @staticmethod
    def _resolved_payload(
        *,
        amap_poi_id: str,
        visit_date: str,
        refresh_status: str,
        facts: dict[str, Any],
        refs: list[dict[str, Any]],
        queried_at: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        evidence = hashlib.sha256(
            json.dumps({"facts": facts, "refs": refs}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "amapPoiId": amap_poi_id,
            "visitDate": visit_date,
            "refreshStatus": refresh_status,
            "facts": facts,
            "sourceRefs": refs,
            "evidenceFingerprint": evidence,
            "queriedAt": queried_at.isoformat(),
            "expiresAt": expires_at.isoformat(),
        }

    @staticmethod
    def _official(item: WebSearchItem) -> bool:
        host = str(urlparse(item.url).hostname or "").lower()
        return item.credibility_rank == "official" or host.endswith(".gov.cn") or host.endswith(".edu.cn")

    @staticmethod
    def _source_ref(item: WebSearchItem) -> dict[str, Any]:
        return {
            "title": item.title,
            "url": item.url,
            "sourceName": item.source_name,
            "credibilityRank": item.credibility_rank,
        }

    @staticmethod
    def _empty_facts(caveat: str, queried_at: datetime, expires_at: datetime) -> dict[str, dict[str, Any]]:
        return {
            key: {
                "status": "unknown",
                "valueText": "待核验",
                "effectiveForDate": None,
                "sourceRefs": [],
                "queriedAt": queried_at.isoformat(),
                "expiresAt": expires_at.isoformat(),
                "caveat": caveat,
            }
            for key in FACT_KEYS
        }

    def _persist(
        self,
        plan_id: str,
        segment_id: str,
        amap_poi_id: str,
        visit_date: str,
        refresh_status: str,
        facts: dict[str, Any],
        refs: list[dict[str, Any]],
        queried_at: datetime,
        expires_at: datetime,
    ) -> None:
        identity = f"{segment_id}\n{amap_poi_id}\n{visit_date}"
        row_id = f"visitfact_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:18]}"
        evidence = hashlib.sha256(
            json.dumps({"facts": facts, "refs": refs}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.db.execute(
            "DELETE FROM segment_visit_facts WHERE plan_id = ? AND segment_id = ?",
            (plan_id, segment_id),
        )
        self.db.execute(
            """
            INSERT INTO segment_visit_facts (
                id, plan_id, segment_id, amap_poi_id, visit_date, refresh_status,
                facts_json, source_refs_json, evidence_fingerprint, queried_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                plan_id,
                segment_id,
                amap_poi_id,
                visit_date,
                refresh_status,
                json.dumps(facts, ensure_ascii=False, sort_keys=True),
                json.dumps(refs, ensure_ascii=False, sort_keys=True),
                evidence,
                queried_at.isoformat(),
                expires_at.isoformat(),
            ),
        )

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(value: Any) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

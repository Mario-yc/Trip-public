from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import sqlite3
from typing import Any, Optional

from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy


_TARGET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("museum", re.compile(r"(美术馆|博物馆|艺术馆|展览馆|展馆)")),
    ("campus_visit", re.compile(r"(985|211|高校|大学|校园|校区)")),
    ("night_view", re.compile(r"(夜景|夜游|观景)")),
    ("meal", re.compile(r"(特色美食|餐厅|餐饮|午餐|晚餐|吃饭)")),
    ("park", re.compile(r"(公园|园林|绿地)")),
    ("local_culture", re.compile(r"(当地文化|民俗|非遗|胡同|历史街区)")),
)
_TIMELINE_QUERY = re.compile(
    r"(哪个行程|哪(?:个|一)天|第几天|几点|什么时间|安排在哪|在哪里|有(?:没有|无)|当前.*安排|行程.*(?:是|有))"
)


@dataclass(frozen=True)
class TimelineQueryResult:
    query_type: str
    target: str
    status: str
    matches: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    invalid_claims: list[dict[str, Any]] = field(default_factory=list)
    active_version_id: Optional[str] = None

    def to_camel_dict(self) -> dict[str, Any]:
        return {
            "queryType": self.query_type,
            "target": self.target,
            "status": self.status,
            "matches": self.matches,
            "unresolved": self.unresolved,
            "invalidClaims": self.invalid_claims,
            "activeVersionId": self.active_version_id,
        }


class TimelineQueryService:
    """Answers current-timeline questions from the persisted snapshot only."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.semantic_policy = IntentCandidateSemanticPolicy()

    @classmethod
    def classify(cls, message: str) -> Optional[str]:
        text = str(message or "").strip()
        if not _TIMELINE_QUERY.search(text):
            return None
        for intent_type, pattern in _TARGET_PATTERNS:
            if pattern.search(text):
                return intent_type
        if re.search(r"(当前|现在).*(行程|时间轴)|(行程|时间轴).*(是什么|有哪些|怎么样)", text):
            return "summary"
        return None

    def execute(self, session_id: str, message: str) -> TimelineQueryResult:
        target = self.classify(message)
        if not target:
            return TimelineQueryResult("unknown", "", "not_found")
        session = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        active_version_id = session["active_version_id"] if session is not None else None
        snapshot = self._snapshot(active_version_id)
        if target == "summary":
            segments = [
                segment
                for day in snapshot.get("days") or []
                if isinstance(day, dict)
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
            ]
            return TimelineQueryResult(
                "summarize_timeline",
                target,
                "found" if segments else "not_found",
                [{"segmentCount": len(segments)}] if segments else [],
                active_version_id=active_version_id,
            )
        matches: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                intent_type = self._intent_type(segment, poi)
                if intent_type != target:
                    continue
                item = {
                    "dayNumber": day.get("dayNumber"),
                    "startTime": segment.get("startTime"),
                    "segmentId": segment.get("id"),
                    "poiName": poi.get("name"),
                }
                pending = bool(poi.get("needsConcretePoi")) or str(poi.get("groundingStatus") or "") in {
                    "waiting_for_poi_grounding",
                    "pending",
                }
                if pending:
                    unresolved.append({**item, "semanticValid": False, "reasonCode": "waiting_for_poi_grounding"})
                    continue
                decision = self.semantic_policy.evaluate(target, poi)
                if decision.passed:
                    matches.append({**item, "semanticValid": True})
                else:
                    invalid.append({**item, "semanticValid": False, **decision.to_camel_dict()})
        status = "found" if matches else "invalid_claim" if invalid else "unresolved" if unresolved else "not_found"
        return TimelineQueryResult("locate_intent", target, status, matches, unresolved, invalid, active_version_id)

    def reply(self, result: TimelineQueryResult) -> str:
        if result.target == "summary":
            if not result.matches:
                return "当前还没有可读取的行程。"
            return f"当前时间轴共有 {int(result.matches[0].get('segmentCount') or 0)} 个行程段。"
        label = {
            "museum": "美术馆",
            "campus_visit": "高校参观",
            "night_view": "夜景",
            "meal": "特色餐饮",
            "park": "公园",
            "local_culture": "当地文化体验",
        }.get(result.target, result.target or "目标行程")
        if result.matches:
            item = result.matches[0]
            return f"{label}安排在第 {item.get('dayNumber')} 天 {item.get('startTime')}，地点是「{item.get('poiName')}」。"
        if result.invalid_claims:
            names = "、".join(f"「{item.get('poiName')}」" for item in result.invalid_claims)
            return f"当前没有合格的{label}安排。{names}不符合{label}语义，不能计为已完成；该目标仍待补全。"
        if result.unresolved:
            item = result.unresolved[0]
            return f"当前{label}仍待补全，已预留第 {item.get('dayNumber')} 天 {item.get('startTime')} 的时间段，但尚未确认真实地图地点。"
        return f"当前行程中没有找到{label}安排。"

    def _snapshot(self, version_id: Optional[str]) -> dict[str, Any]:
        if not version_id:
            return {}
        row = self.db.execute("SELECT snapshot_json FROM itinerary_versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _intent_type(segment: dict[str, Any], poi: dict[str, Any]) -> str:
        grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
        if str(segment.get("intentType") or "").strip():
            return str(segment.get("intentType")).strip()
        text = " ".join(str(value or "") for value in (segment.get("notes"), poi.get("sourceNote")))
        match = re.search(r"intentType\s*[：:=]\s*([a-z_]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
        for value in (poi.get("intentType"), grounding.get("intentType")):
            if str(value or "").strip():
                return str(value).strip()
        return ""

import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.tests.intent_contract_support import IntentContractProviderMixin
from src.api.schemas.agent import AgentMessageRequest
from src.api.schemas.maps import (
    MapPoiResolveResponse,
    MapPoiResolvedItemResponse,
    MapPoiResponse,
    MapPoiSearchResponse,
)
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.agent_service import AgentService
from src.services.agent_verifier_service import AgentVerifierReport, AgentVerifierService
from src.services.conversation_service import ConversationService
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.route_service import RouteService
from src.runtime.agent_runtime import TripAgentRuntime
from src.runtime.runtime_models import RuntimeRunOptions


TURN_1 = (
    "今年国庆参观985大学两日游，然后去体验下北京当地博物馆陶冶情操。"
    "10月1日到2日，2天，中等预算，1人，公交地铁优先。"
    "路途中能品尝北京当地特色美食。绕行最多30分钟，绕行比例最多35%"
)
TURN_2 = "重试一次，将第一天美术馆修改为清华美术馆"


def _open_db() -> sqlite3.Connection:
    initialize_database()
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _poi(
    amap_id: str,
    name: str,
    *,
    poi_type: str,
    category: str,
    longitude: float,
    latitude: float,
) -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type=poi_type,
        city="北京",
        district="测试区",
        address=f"{name} 测试地址",
        longitude=longitude,
        latitude=latitude,
        category=category,
        source=AMAP_PLACE_SOURCE,
        sourceNote="脱敏 recorded AMap fixture",
        providerTypeCode="050100" if category == "food" else None,
        tags=["地方风味"] if category == "food" else [],
        sourceClaims=(
            [{"claimKey": "local_food", "stance": "support", "locality": "北京"}] if category == "food" else []
        ),
        confidence=0.96,
        photos=[],
    )


POIS = {
    "清华大学": _poi(
        "B0REACTTSINGHUA",
        "清华大学",
        poi_type="科教文化服务;学校;高等院校",
        category="campus",
        longitude=116.3269,
        latitude=40.0036,
    ),
    "北京大学": _poi(
        "B0REACTPEKING",
        "北京大学",
        poi_type="科教文化服务;学校;高等院校",
        category="campus",
        longitude=116.3109,
        latitude=39.9928,
    ),
    "中国美术馆": _poi(
        "B0REACTNAMOC",
        "中国美术馆",
        poi_type="科教文化服务;博物馆;美术馆",
        category="museum",
        longitude=116.4166,
        latitude=39.9240,
    ),
    "清华大学艺术博物馆": _poi(
        "B0REACTTSINGHUAART",
        "清华大学艺术博物馆",
        poi_type="科教文化服务;博物馆;美术馆",
        category="museum",
        longitude=116.3306,
        latitude=40.0030,
    ),
    "方砖厂69号炸酱面": _poi(
        "B0REACTFOOD1",
        "老北京方砖厂69号炸酱面",
        poi_type="餐饮服务;中餐厅;地方风味餐厅",
        category="food",
        longitude=116.4050,
        latitude=39.9300,
    ),
    "护国寺小吃": _poi(
        "B0REACTFOOD2",
        "北京护国寺传统小吃",
        poi_type="餐饮服务;中餐厅;地方风味餐厅",
        category="food",
        longitude=116.3730,
        latitude=39.9340,
    ),
    "东来顺饭庄": _poi(
        "B0REACTFOOD3",
        "北京东来顺老字号涮肉",
        poi_type="餐饮服务;中餐厅;地方风味餐厅;涮肉",
        category="food",
        longitude=116.4100,
        latitude=39.9200,
    ),
    "故宫博物院": _poi(
        "B0REACTPALACE",
        "故宫博物院",
        poi_type="风景名胜;世界遗产;博物馆",
        category="scenic",
        longitude=116.3970,
        latitude=39.9180,
    ),
    "天坛公园": _poi(
        "B0REACTTEMPLE",
        "天坛公园",
        poi_type="风景名胜;公园广场;公园",
        category="scenic",
        longitude=116.4108,
        latitude=39.8819,
    ),
}


class RecordedPoiResolver:
    def __init__(self) -> None:
        self.queries: list[list[str]] = []

    def resolve(self, payload, **_kwargs) -> MapPoiResolveResponse:
        names = [query.name for query in payload.queries]
        self.queries.append(names)
        canonical_names = {
            "清华美术馆": "清华大学艺术博物馆",
        }
        return MapPoiResolveResponse(
            resolved=[
                MapPoiResolvedItemResponse(
                    query=name,
                    status="accepted",
                    poi=POIS[canonical_names.get(name, name)],
                )
                for name in names
                if canonical_names.get(name, name) in POIS
            ],
            pending=[],
        )


class TwoTurnReactProvider(IntentContractProviderMixin):
    """Script model decisions, but leave every write to the production executors."""

    def __init__(self) -> None:
        self.decision_calls = 0
        self.decision_contexts: list[dict] = []
        self.initial_plan_calls = 0
        self.tool_loop_calls = 0

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        self.decision_calls += 1
        self.decision_contexts.append(copy.deepcopy(context))
        if self.decision_calls == 1:
            required_items = (context.get("observation") or {}).get("requirementCoverage", {}).get("required", [])
            goal_requirements = {str(item["goalId"]): item for item in required_items if item.get("goalId")}
            hard_counts = {
                goal_id: int(item.get("requiredMin") or item.get("target") or item.get("requiredCount") or 1)
                for goal_id, item in goal_requirements.items()
                if item.get("requirementLevel") not in {"soft_experience", "optional"}
            }
            optional_goal_ids = [
                goal_id
                for goal_id, item in goal_requirements.items()
                if item.get("requirementLevel") in {"soft_experience", "optional"}
            ]
            assert set(hard_counts) == {"goal_campus_visit", "goal_museum"}
            assert optional_goal_ids == ["goal_meal"]
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "draft_itinerary",
                "actionDirective": {
                    "type": "draft_itinerary",
                    "goalPriority": ["goal_campus_visit", "goal_museum", "goal_meal"],
                    "dayStrategies": [
                        {
                            "dayNumber": 1,
                            "theme": "高校、美术馆与京味美食",
                            "requiredGoalIds": ["goal_campus_visit", "goal_museum"],
                            "requiredGoalCounts": hard_counts,
                            "optionalGoalIds": optional_goal_ids,
                            "pace": "standard",
                            "maxRouteAnchors": 4,
                        },
                        {
                            "dayNumber": 2,
                            "theme": "北京文化地标",
                            "requiredGoalIds": [],
                            "requiredGoalCounts": {},
                            "optionalGoalIds": [],
                            "pace": "standard",
                            "maxRouteAnchors": 3,
                        },
                    ],
                    "optionalExperienceBudget": len(optional_goal_ids),
                    "routePlanningPolicy": {
                        "objective": "least_generalized_cost",
                        "source": "user_explicit",
                        "allowExperienceDetour": True,
                        "mobilityProfile": {
                            "transportMode": "transit",
                            "paceClass": "standard",
                        },
                        "detourEnvelope": {
                            "maxGeneralizedCostDelta": 30.0,
                            "maxDetourRatio": 0.35,
                        },
                    },
                    "occurrenceScheduleHints": [
                        {
                            "goalId": "goal_campus_visit",
                            "dayNumber": 1,
                            "dayPart": "morning",
                            "sequence": 1,
                            "preferredStartTime": "09:00",
                            "durationEstimate": {"min": 90, "preferred": 120, "max": 150},
                            "estimateSource": "controller_estimate",
                            "confidence": 0.95,
                        },
                        {
                            "goalId": "goal_meal",
                            "dayNumber": 1,
                            "dayPart": "noon",
                            "sequence": 2,
                            "preferredStartTime": "12:00",
                            "durationEstimate": {"min": 45, "preferred": 60, "max": 90},
                            "estimateSource": "controller_estimate",
                            "confidence": 0.9,
                        },
                        {
                            "goalId": "goal_museum",
                            "dayNumber": 1,
                            "dayPart": "afternoon",
                            "sequence": 3,
                            "preferredStartTime": "16:15",
                            "durationEstimate": {"min": 120, "preferred": 150, "max": 180},
                            "estimateSource": "controller_estimate",
                            "confidence": 0.95,
                        },
                    ],
                    "searchPriority": ["hard_constraint", "required"],
                    "candidateSelectionPolicy": {
                        "autoSelectWhenDominant": True,
                        "askWhenMaterialTradeoff": True,
                        "preferLowDetour": True,
                        "avoidRecentEntities": True,
                    },
                    "schedulePolicy": {
                        "respectOpeningWindowsWhenKnown": True,
                        "allowProvisionalWhenUnknown": True,
                    },
                },
            }
        if self.decision_calls == 2:
            return self._finish("Turn 1 已形成可验证行程。")
        if self.decision_calls == 3:
            observation = context.get("observation") or {}
            museum_refs = [
                item
                for item in observation.get("segmentRefs") or []
                if item.get("dayNumber") == 1 and item.get("intentType") == "museum"
            ]
            assert len(museum_refs) == 1
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "resolve_poi",
                "actionDirective": {
                    "type": "resolve_poi",
                    "targetGoalId": "goal_museum",
                    "targetSegmentIds": [museum_refs[0]["segmentId"]],
                    "searchIntent": "清华美术馆",
                    "searchMode": "text",
                    "maxCandidates": 4,
                    "autoSelectPolicy": "dominant_safe_candidate_only",
                    "askUserPolicy": "material_tradeoff_only",
                },
            }
        if self.decision_calls == 4:
            observation = context.get("observation") or {}
            outcome = observation.get("lastOutcome") or {}
            summary = outcome.get("candidateSummary") or {}
            candidate_ids = summary.get("selectedCandidateRecordIds") or []
            amap_ids = summary.get("resolvedAmapPoiIds") or []
            segment_ids = summary.get("targetSegmentIds") or []
            assert len(candidate_ids) == len(amap_ids) == len(segment_ids) == 1
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "patch_itinerary",
                "actionDirective": {
                    "type": "patch_itinerary",
                    "operationIntent": "replace_segment_poi_from_candidate",
                    "baseVersionId": observation["versionLineage"]["currentVersionId"],
                    "targetSegmentIds": segment_ids,
                    "requestedOutcome": "将第一天美术馆替换为清华大学艺术博物馆并保持原时长",
                    "candidateId": candidate_ids[0],
                    "amapPoiId": amap_ids[0],
                    "preserve": ["target_duration", "other_segments", "other_days"],
                    "maxChangedSegmentCount": 1,
                },
            }
        if self.decision_calls == 5:
            observation = context.get("observation") or {}
            assert (observation.get("lastOutcome") or {}).get("status") == "success"
            return self._finish("已将第一天美术馆替换为清华大学艺术博物馆，原停留时长保持不变。")
        raise AssertionError(f"unexpected Controller call {self.decision_calls}")

    @staticmethod
    def _finish(reply: str) -> dict:
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "finish",
            "actionDirective": {"type": "finish", "assistantReply": reply},
        }

    def generate_initial_plan(self, _context):
        self.initial_plan_calls += 1
        return json.dumps(_turn_1_initial_plan(), ensure_ascii=False)

    def run_tool_loop(self, _context, _tool_registry):
        self.tool_loop_calls += 1
        raise AssertionError("simple ReAct timeline edit must not enter generic tool loop")


def _turn_1_initial_plan() -> dict:
    return {
        "reply": "已把高校、美术馆和第二天文化地标拆成可落地 DaySlot。",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "day1_985_campus",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "985高校参观",
                "routeAnchor": True,
                "priority": 100,
                "notes": "必须满足一所 985 大学。",
            },
            {
                "slotId": "day1_art_museum",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "16:15-18:45",
                "startTime": "16:15",
                "durationMinutes": 150,
                "kind": "museum",
                "rawNeed": "美术馆参观",
                "routeAnchor": True,
                "priority": 95,
                "notes": "必须安排一所真实美术馆。",
            },
            {
                "slotId": "day2_palace",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "09:00-11:30",
                "startTime": "09:00",
                "durationMinutes": 150,
                "kind": "landmark",
                "rawNeed": "故宫博物院",
                "routeAnchor": True,
                "priority": 70,
                "notes": "第二天上午文化地标。",
            },
            {
                "slotId": "day2_temple",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "14:00-16:00",
                "startTime": "14:00",
                "durationMinutes": 120,
                "kind": "landmark",
                "rawNeed": "天坛公园",
                "routeAnchor": True,
                "priority": 65,
                "notes": "第二天下午文化地标。",
            },
        ],
        "intentPools": [
            {
                "poolId": "campus_visit_pool",
                "rawNeed": "985高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 1,
                "preferredTypes": ["大学", "高等院校"],
                "rejectedTypes": ["学院附属", "停车场", "公司"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day1_985_campus"],
                "candidateHints": ["清华大学"],
                "hintPolicy": "user_explicit_hint",
            },
            {
                "poolId": "museum_pool",
                "rawNeed": "美术馆参观",
                "city": "北京",
                "intentType": "museum",
                "targetCount": 1,
                "preferredTypes": ["美术馆", "艺术博物馆"],
                "rejectedTypes": ["公园", "商场", "停车场"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day1_art_museum"],
                "candidateHints": ["中国美术馆"],
                "hintPolicy": "llm_common_knowledge_hint",
            },
            {
                "poolId": "palace_pool",
                "rawNeed": "故宫博物院",
                "city": "北京",
                "intentType": "landmark",
                "targetCount": 1,
                "preferredTypes": ["风景名胜", "博物馆"],
                "rejectedTypes": ["停车场", "公司"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day2_palace"],
                "candidateHints": ["故宫博物院"],
                "hintPolicy": "user_explicit_hint",
            },
            {
                "poolId": "temple_pool",
                "rawNeed": "天坛公园",
                "city": "北京",
                "intentType": "landmark",
                "targetCount": 1,
                "preferredTypes": ["风景名胜", "公园"],
                "rejectedTypes": ["停车场", "公司"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day2_temple"],
                "candidateHints": ["天坛公园"],
                "hintPolicy": "llm_common_knowledge_hint",
            },
        ],
        "warnings": [],
    }


def _recorded_map_search(
    _self,
    city,
    keyword,
    category="all",
    limit=10,
    bypass_cache=False,
    *,
    query_scope_fingerprint=None,
    page=1,
    offset=None,
) -> MapPoiSearchResponse:
    del bypass_cache, query_scope_fingerprint, page, offset
    keyword_text = str(keyword)
    if category == "food":
        candidates = [POIS["方砖厂69号炸酱面"], POIS["护国寺小吃"], POIS["东来顺饭庄"]]
    elif "清华大学艺术博物馆" in keyword_text or "清华美术馆" in keyword_text:
        candidates = [POIS["清华大学艺术博物馆"]]
    elif any(marker in keyword_text for marker in ("美术馆", "艺术博物馆")):
        candidates = [POIS["中国美术馆"]]
    elif any(marker in keyword_text for marker in ("清华", "985", "高校", "高等院校", "大学")):
        candidates = [POIS["清华大学"], POIS["北京大学"]]
    elif any(marker in keyword_text for marker in ("炸酱", "北京菜", "特色美食", "护国寺", "东来顺", "晚餐", "午餐")):
        candidates = [POIS["方砖厂69号炸酱面"], POIS["护国寺小吃"], POIS["东来顺饭庄"]]
    elif "故宫" in keyword_text:
        candidates = [POIS["故宫博物院"]]
    elif "天坛" in keyword_text:
        candidates = [POIS["天坛公园"]]
    elif any(marker in keyword_text for marker in ("文化地标", "风景名胜", "公园")):
        candidates = [POIS["故宫博物院"], POIS["天坛公园"]]
    else:
        candidates = []
    return MapPoiSearchResponse(
        city=city,
        keyword=keyword,
        category=category,
        providerName="recorded-amap-fixture",
        queriedAt=datetime.now(timezone.utc),
        pois=candidates[:limit],
    )


def _recorded_route(_self, from_poi, to_poi, mode):
    polyline = f"{from_poi.longitude},{from_poi.latitude};{to_poi.longitude},{to_poi.latitude}"
    if mode == "transit":
        return {
            "status": "1",
            "route": {
                "transits": [
                    {
                        "distance": "1200",
                        "duration": "900",
                        "cost": "4",
                        "segments": [
                            {
                                "bus": {
                                    "buslines": [
                                        {
                                            "name": "recorded metro",
                                            "distance": "1200",
                                            "duration": "900",
                                            "polyline": polyline,
                                        }
                                    ]
                                }
                            }
                        ],
                    }
                ]
            },
        }
    return {
        "status": "1",
        "route": {
            "taxi_cost": "18",
            "paths": [
                {
                    "distance": "1200",
                    "duration": "900",
                    "steps": [
                        {
                            "instruction": f"recorded {mode}",
                            "distance": "1200",
                            "duration": "900",
                            "polyline": polyline,
                        }
                    ],
                }
            ],
        },
    }


def _recorded_map_search_nearby(
    _self,
    city,
    longitude,
    latitude,
    keyword,
    category="all",
    radius=1500,
    limit=12,
    bypass_cache=False,
    *,
    query_scope_fingerprint=None,
    page=1,
    offset=None,
) -> MapPoiSearchResponse:
    del bypass_cache, query_scope_fingerprint, page, offset
    requested_name = str(keyword).removeprefix("北京 ").strip() or "北京特色美食"
    base_candidates = [
        _poi(
            f"B0REACTNEAR{index}",
            requested_name if index == 1 else f"{requested_name}{index}店",
            poi_type="餐饮服务;中餐厅;地方风味餐厅",
            category="food",
            longitude=float(longitude) + index * 0.001,
            latitude=float(latitude) + index * 0.001,
        )
        for index in range(1, 4)
    ]
    nearby_candidates = [
        poi.model_copy(
            update={
                "longitude": float(longitude) + index * 0.0005,
                "latitude": float(latitude) + index * 0.0005,
            }
        )
        for index, poi in enumerate(base_candidates)
    ]
    return MapPoiSearchResponse(
        city=city,
        keyword=keyword,
        category=category,
        providerName="recorded-amap-around-fixture",
        queriedAt=datetime.now(timezone.utc),
        pois=nearby_candidates[:limit],
    )


def _duration_minutes(start_time: str, end_time: str) -> int:
    start = datetime.strptime(start_time, "%H:%M")
    end = datetime.strptime(end_time, "%H:%M")
    return int((end - start).total_seconds() // 60)


def _segment_by_intent(itinerary, intent_type: str, *, day_number: Optional[int] = None):
    matches = []
    for day in itinerary.days:
        for segment in day.segments:
            metadata = segment.semantic_metadata or {}
            if day_number is not None and day.day_number != day_number:
                continue
            if metadata.get("intentType") == intent_type or getattr(segment.poi, "intent_type", None) == intent_type:
                matches.append((day, segment))
    assert len(matches) == 1, [(day.day_number, segment.poi.name) for day, segment in matches]
    return matches[0]


def _non_time_projection(segment) -> dict:
    payload = segment.model_dump(by_alias=True)
    payload.pop("startTime", None)
    payload.pop("endTime", None)
    return payload


def _target_direct_content_projection(segment) -> dict:
    payload = _non_time_projection(segment)
    payload.pop("userLocked", None)
    estimate_metadata = payload.get("estimateMetadata")
    if isinstance(estimate_metadata, dict):
        estimate_metadata.pop("schedule", None)
        duration_metadata = estimate_metadata.get("duration")
        if isinstance(duration_metadata, dict):
            duration_metadata.pop("source", None)
            duration_metadata.pop("userLocked", None)
    semantic_metadata = payload.get("semanticMetadata")
    if isinstance(semantic_metadata, dict):
        semantic_metadata.pop("userLocked", None)
    return payload


def test_p0_golden_same_session_draft_resolve_patch_reload_finish(monkeypatch):
    monkeypatch.setattr(AgentService, "_creative_portfolio_enabled", lambda _self, _context: False)
    provider = TwoTurnReactProvider()
    captured_operations = []
    nearby_calls = []
    search_calls = []
    original_apply_patch = ItineraryPatchService.apply_patch

    def capture_apply_patch(self, plan_id, operations, *args, **kwargs):
        captured_operations.extend(
            operation.model_dump(by_alias=True, exclude_none=True)
            if hasattr(operation, "model_dump")
            else copy.deepcopy(operation)
            for operation in operations
        )
        return original_apply_patch(self, plan_id, operations, *args, **kwargs)

    def capture_nearby(*args, **kwargs):
        nearby_calls.append({"args": args[1:], "kwargs": kwargs})
        return _recorded_map_search_nearby(*args, **kwargs)

    def capture_search(*args, **kwargs):
        search_calls.append({"args": args[1:], "kwargs": kwargs})
        return _recorded_map_search(*args, **kwargs)

    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(MapPoiService, "search", capture_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", capture_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)
    monkeypatch.setattr(ItineraryPatchService, "apply_patch", capture_apply_patch)
    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "ReAct 同会话多轮")
        service = AgentService(connection, provider=provider)

        turn_1 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_1))
        captured_schedule = " | ".join(
            f"D{day.get('dayNumber')}:{segment.get('poi', {}).get('name')}:{segment.get('startTime')}-{segment.get('endTime')}"
            for operation in captured_operations
            for day in (operation.get("fullItinerary") or {}).get("days", [])
            for segment in day.get("segments", [])
        )
        pool_report = " | ".join(
            f"{item.get('intentType')}:{item.get('selectedCount')}/{item.get('targetCount')}:{item.get('providerState')}:{item.get('reason')}"
            for event in turn_1.planning_steps
            for item in (event.metadata.get("resultPreview", {}).get("poolReports") or [])
        )
        causal_report = json.dumps(
            [
                {
                    "type": event.type,
                    "status": event.status,
                    "primaryAction": event.metadata.get("primaryAction"),
                    "controllerError": event.metadata.get("controllerError"),
                    "providerRawDecisions": event.metadata.get("providerRawDecisions"),
                    "normalizedDecision": event.metadata.get("normalizedDecision"),
                    "cycleIndex": event.metadata.get("cycleIndex"),
                    "resultPreview": event.metadata.get("resultPreview"),
                    "reason": event.metadata.get("reason"),
                }
                for event in turn_1.planning_steps
                if event.type
                in {
                    "agent_decision",
                    "agent_action_outcome",
                    "simple_open_tool_call",
                    "simple_open_result_classified",
                    "simple_open_terminal",
                }
            ],
            ensure_ascii=False,
            default=str,
        )
        assert turn_1.version is not None, (
            f"{turn_1.assistant_turn.failure_reason or turn_1.assistant_turn.content}; {captured_schedule}; "
            f"{pool_report}; nearby={nearby_calls}; causal={causal_report}"
        )
        assert turn_1.terminal_status == "draft_pending_grounding", {
            "terminalStatus": turn_1.terminal_status,
            "reply": turn_1.assistant_turn.content,
            "warnings": turn_1.warnings,
            "choices": turn_1.assistant_turn.choice_options,
            "pendingPoiCount": len(turn_1.pending_poi_candidates),
            "capturedSchedule": captured_schedule,
            "poolReport": pool_report,
        }
        assert "draft_pending_grounding" in turn_1.assistant_turn.content
        assert "可选餐饮地点待补全" in turn_1.assistant_turn.content
        assert "必需地点待补全" not in turn_1.assistant_turn.content
        turn_1_active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        assert turn_1_active == turn_1.version.id
        turn_1_observation = provider.decision_contexts[1]["observation"]
        goal_ledger = {item["goalId"]: item for item in turn_1_observation["requirementCoverage"]["required"]}
        assert set(goal_ledger) == {"goal_campus_visit", "goal_museum", "goal_meal"}
        assert goal_ledger["goal_campus_visit"]["requirementLevel"] == "required"
        assert goal_ledger["goal_museum"]["requirementLevel"] == "required"
        assert goal_ledger["goal_meal"]["requirementLevel"] == "soft_experience"
        segment_refs_by_goal = {}
        for item in turn_1_observation["segmentRefs"]:
            segment_refs_by_goal.setdefault(item.get("goalId"), []).append(item)
        assert len(segment_refs_by_goal["goal_campus_visit"]) == 2
        assert {item["dayNumber"] for item in segment_refs_by_goal["goal_campus_visit"]} == {1, 2}
        assert len(segment_refs_by_goal["goal_museum"]) == 1
        assert segment_refs_by_goal["goal_museum"][0]["dayNumber"] == 1
        assert segment_refs_by_goal["goal_museum"][0]["intentType"] == "museum"
        assert segment_refs_by_goal["goal_meal"]
        turn_1_version_row = connection.execute(
            "SELECT source_turn_id, source_patch_id FROM itinerary_versions WHERE id = ?",
            (turn_1.version.id,),
        ).fetchone()
        assert turn_1_version_row["source_patch_id"]
        turn_1_patch_row = connection.execute(
            "SELECT result_version_id, validation_status FROM itinerary_patches WHERE id = ?",
            (turn_1_version_row["source_patch_id"],),
        ).fetchone()
        assert turn_1_patch_row["result_version_id"] == turn_1.version.id
        assert turn_1_patch_row["validation_status"] == "accepted"
        before_itinerary = ItineraryService(connection).get_plan(session.active_plan_id)
        assert len(before_itinerary.days) == 2
        assert [str(day.date) for day in before_itinerary.days] == ["2026-10-01", "2026-10-02"]
        before_day, before_museum = _segment_by_intent(before_itinerary, "museum", day_number=1)
        assert before_day.day_number == 1
        before_duration = _duration_minutes(before_museum.start_time, before_museum.end_time)
        assert before_duration == 120
        initial_slot_event = next(
            event for event in turn_1.planning_steps if event.type == "initial_day_slot_provider"
        )
        assert initial_slot_event.status == "fallback"
        assert (
            initial_slot_event.metadata["resultPreview"]["fallbackReason"]
            == "authoritative_occurrence_coverage_incomplete"
        )
        versions_before = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        patches_before = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]
        segments_before = {
            segment.id: segment.model_dump(by_alias=True) for day in before_itinerary.days for segment in day.segments
        }

        search_calls_before_turn_2 = len(search_calls)
        turn_2 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_2))
        after_itinerary = ItineraryService(connection).get_plan(session.active_plan_id)
        after_day, after_museum = _segment_by_intent(after_itinerary, "museum", day_number=1)
        versions_after = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
        patches_after = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]
        session_row = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        persisted_turns = connection.execute(
            "SELECT role, content, agent_request_json, agent_response_json FROM conversation_turns "
            "WHERE session_id = ? ORDER BY turn_index",
            (session.session_id,),
        ).fetchall()
        candidate_rows = connection.execute(
            "SELECT id, segment_id, status, selected_amap_id, candidates_json "
            "FROM amap_poi_candidates WHERE session_id = ? ORDER BY created_at",
            (session.session_id,),
        ).fetchall()
    assert provider.initial_plan_calls == 1
    turn_2_decision_actions = [
        event.metadata.get("primaryAction") for event in turn_2.planning_steps if event.type == "agent_decision"
    ]
    turn_2_outcome_previews = [
        event.metadata.get("resultPreview") for event in turn_2.planning_steps if event.type == "agent_action_outcome"
    ]
    assert provider.decision_calls == 5, (
        f"actions={turn_2_decision_actions}; outcomes={turn_2_outcome_previews}; "
        f"reply={turn_2.assistant_turn.content}; searchCalls={search_calls}"
    )
    assert provider.tool_loop_calls == 0
    assert any("清华美术馆" in str(call) for call in search_calls[search_calls_before_turn_2:])
    matching_candidate_rows = [
        row
        for row in candidate_rows
        if row["segment_id"] == before_museum.id and row["selected_amap_id"] == "B0REACTTSINGHUAART"
    ]
    assert len(matching_candidate_rows) == 1
    assert any(
        item.get("name") == "清华大学艺术博物馆" for item in json.loads(matching_candidate_rows[0]["candidates_json"])
    )
    assert versions_after - versions_before == 1
    assert patches_after - patches_before == 1
    assert turn_2.version is not None
    assert session_row["active_version_id"] == turn_2.version.id
    assert turn_2.version.id != turn_1_active

    # The model selects semantics, never an internal segment id.
    patch_decision_context = provider.decision_contexts[2]
    observation = patch_decision_context["observation"]
    assert observation["versionLineage"]["currentVersionId"] == turn_1_active
    assert observation["latestMessage"] == TURN_2
    assert any(item.get("content") == TURN_1 for item in observation["recentTurns"])
    museum_refs = [
        item for item in observation["segmentRefs"] if item.get("dayNumber") == 1 and item.get("intentType") == "museum"
    ]
    assert len(museum_refs) == 1
    post_resolve_observation = provider.decision_contexts[3]["observation"]
    pending_groups = post_resolve_observation["candidateState"]["pendingGroups"]
    assert any(
        group.get("sourceSegmentId") == before_museum.id and group.get("selectedAmapId") == "B0REACTTSINGHUAART"
        for group in pending_groups
    )
    finish_observation = provider.decision_contexts[4]["observation"]
    assert finish_observation["versionLineage"]["currentVersionId"] == turn_2.version.id
    assert finish_observation["lastOutcome"]["status"] == "success"
    route_check = next(
        check
        for check in finish_observation["lastOutcome"]["verifier"]["checks"]
        if check["name"] == "route_quality_verifier"
    )
    route_coverage = route_check["routeCoverage"]
    assert finish_observation["routeState"] == {
        "confirmedAnchorRequiredLegs": route_coverage["requiredLegCount"],
        "confirmedAnchorCoveredLegs": route_coverage["coveredLegCount"],
        "confirmedAnchorRouteReady": route_coverage["missingLegCount"] == 0,
        "completeDoorToDoorStatus": "ready",
    }

    assert after_day.day_number == 1
    assert after_museum.id == before_museum.id
    assert after_museum.poi.name == "清华大学艺术博物馆"
    assert after_museum.poi.amap_id == "B0REACTTSINGHUAART"
    assert _duration_minutes(after_museum.start_time, after_museum.end_time) == before_duration
    assert after_museum.start_time == before_museum.start_time
    assert after_museum.end_time == before_museum.end_time
    for day in after_itinerary.days:
        for segment in day.segments:
            if segment.kind == "meal":
                assert "costBasis=per_person" in segment.notes
                assert "partySize=1" in segment.notes
                assert f"totalCost={segment.estimated_cost:.0f}" in segment.notes
    for day in after_itinerary.days:
        for segment in day.segments:
            if segment.id != before_museum.id:
                assert segment.model_dump(by_alias=True) == segments_before[segment.id]

    decisions = [event for event in turn_2.planning_steps if event.type == "agent_decision"]
    outcomes = [event for event in turn_2.planning_steps if event.type == "agent_action_outcome"]
    observations = [event for event in turn_2.planning_steps if event.type == "agent_observation"]
    policy_gates = [event for event in turn_2.planning_steps if event.type == "agent_policy_gate"]
    action_starts = [event for event in turn_2.planning_steps if event.type == "agent_action_started"]
    assert [event.metadata["primaryAction"] for event in decisions] == [
        "resolve_poi",
        "patch_itinerary",
        "finish",
    ]
    assert decisions[0].metadata["schemaVersion"] == "agent-decision-v3"
    assert decisions[0].metadata["actionDirectiveSource"] == "model"
    assert decisions[0].metadata["controllerFullCalled"] is True
    assert decisions[0].metadata["preControllerDomainRouterCount"] == 0
    assert decisions[0].metadata["localMutationBypassCount"] == 0
    assert decisions[0].metadata["timelineParserCalledBeforeDecision"] is False
    assert decisions[1].metadata["postObservationDecision"] is True
    assert decisions[2].metadata["postObservationDecision"] is True
    assert len(observations) >= 3
    assert observations[-1].metadata["cycleIndex"] == 2
    assert [event.metadata["cycleIndex"] for event in policy_gates] == [0, 1, 2]
    assert all(event.metadata["accepted"] is True for event in policy_gates)
    assert [event.metadata["cycleIndex"] for event in action_starts] == [0, 1, 2]
    assert action_starts[0].metadata["executionRoute"] == "poi_grounding"
    assert action_starts[1].metadata["executionRoute"] == "timeline_mutation_executor"
    assert action_starts[2].metadata["executionRoute"] == "terminal_response"
    resolve_preview = outcomes[0].metadata["resultPreview"]
    patch_preview = outcomes[1].metadata["resultPreview"]
    assert resolve_preview["executionRoute"] == "poi_grounding"
    assert resolve_preview["candidateSummary"]["selectedCandidateRecordIds"]
    assert resolve_preview["candidateSummary"]["dominance"]["dominant"] is True
    assert patch_preview["executionRoute"] == "timeline_mutation_executor"
    assert patch_preview["verifierPassed"] is True
    assert patch_preview["patchIds"]
    assert patch_preview["changedSegmentIds"] == [before_museum.id]

    # The persisted trace is causal evidence, not an after-the-fact summary.  It
    # must retain the actual observe -> decide -> gate -> act -> verify ->
    # outcome -> reload -> finish order, with monotonic timestamps and a real
    # non-zero transaction duration.
    causal_types = {
        "agent_observation",
        "agent_decision",
        "agent_policy_gate",
        "agent_action_started",
        "timeline_patch_started",
        "structural_verifier",
        "mutation_postcondition_verifier",
        "timeline_mutation_committed",
        "agent_action_outcome",
        "candidate_patch_started",
        "candidate_patch_applied",
        "candidate_patch_verifier",
        "candidate_patch_committed",
    }
    causal_events = [event for event in turn_2.planning_steps if event.type in causal_types]

    def causal_index(event_type: str, *, cycle_index: Optional[int] = None) -> int:
        return next(
            index
            for index, event in enumerate(causal_events)
            if event.type == event_type and (cycle_index is None or event.metadata.get("cycleIndex") == cycle_index)
        )

    assert causal_index("agent_observation", cycle_index=0) < causal_index("agent_decision", cycle_index=0)
    assert causal_index("agent_decision", cycle_index=0) < causal_index("agent_policy_gate", cycle_index=0)
    assert causal_index("agent_policy_gate", cycle_index=0) < causal_index("agent_action_started", cycle_index=0)
    assert causal_index("agent_action_started", cycle_index=0) < causal_index("agent_action_outcome", cycle_index=0)
    assert causal_index("agent_action_outcome", cycle_index=0) < causal_index("agent_observation", cycle_index=1)
    assert causal_index("agent_observation", cycle_index=1) < causal_index("agent_decision", cycle_index=1)
    assert causal_index("agent_decision", cycle_index=1) < causal_index("agent_policy_gate", cycle_index=1)
    assert causal_index("agent_policy_gate", cycle_index=1) < causal_index("agent_action_started", cycle_index=1)
    assert causal_index("agent_action_started", cycle_index=1) < causal_index("candidate_patch_started")
    assert causal_index("candidate_patch_started") < causal_index("candidate_patch_applied")
    assert causal_index("candidate_patch_applied") < causal_index("candidate_patch_verifier")
    assert causal_index("candidate_patch_verifier") < causal_index("candidate_patch_committed")
    assert causal_index("candidate_patch_committed") < causal_index("agent_action_outcome", cycle_index=1)
    assert causal_index("agent_action_outcome", cycle_index=1) < causal_index("agent_observation", cycle_index=2)
    assert causal_index("agent_observation", cycle_index=2) < causal_index("agent_decision", cycle_index=2)
    causal_timestamps = [event.timestamp for event in causal_events]
    assert causal_timestamps == sorted(causal_timestamps)
    transaction_events = [
        event
        for event in causal_events
        if event.type
        in {
            "timeline_patch_started",
            "candidate_patch_started",
            "candidate_patch_applied",
            "candidate_patch_verifier",
            "candidate_patch_committed",
        }
    ]
    assert len({event.timestamp for event in transaction_events}) > 1
    assert any(event.duration_ms > 0 for event in transaction_events)

    turn_2_response_payload = json.loads(persisted_turns[-1]["agent_response_json"])
    persisted_patch_outcome = turn_2_response_payload["cycleTrace"][1]["outcome"]
    assert persisted_patch_outcome["executionRoute"] == "timeline_mutation_executor"
    assert persisted_patch_outcome["patchIds"] == patch_preview["patchIds"]
    assert persisted_patch_outcome["resultVersionId"] == turn_2.version.id
    assert persisted_patch_outcome["verifier"]["passed"] is True
    assert turn_2_response_payload["planningRun"]["id"]
    assert turn_2_response_payload["cycleTrace"]
    persisted_planning = [
        event for event in turn_2_response_payload["planningSteps"] if event.get("type") != "agent_run"
    ]
    assert persisted_planning[0]["type"] == "agent_observation"
    persisted_timestamps = [event["timestamp"] for event in persisted_planning]
    mixed_format_events = [
        (event["type"], event["timestamp"]) for event in persisted_planning if "T" not in event["timestamp"]
    ]
    assert mixed_format_events == [], mixed_format_events
    assert persisted_timestamps == sorted(persisted_timestamps), [
        (event["type"], event["timestamp"]) for event in persisted_planning
    ]
    assert [event["sequence"] for event in persisted_planning] == list(range(1, len(persisted_planning) + 1))
    assert turn_2_response_payload["toolEvents"] == []
    assert not any(
        event.type == "tool"
        and event.metadata.get("toolName")
        in {
            "web_search",
            "ticket_lookup",
            "amap_weather",
        }
        for event in turn_2.tool_events
    )
    assert turn_2.assistant_turn.content == (
        "已将第一天美术馆替换为清华大学艺术博物馆，原停留时长保持不变。修改已写入持久化版本并通过校验。"
    )


def test_p0_turn1_hard_verifier_failure_does_not_advance_active_version(monkeypatch):
    provider = TwoTurnReactProvider()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(MapPoiService, "search", _recorded_map_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", _recorded_map_search_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)
    monkeypatch.setattr(
        AgentVerifierService,
        "verify_agent_write",
        lambda *_args, **_kwargs: AgentVerifierReport(
            passed=False,
            hard_failures=["golden_injected_hard_failure"],
            checks=[{"name": "golden_injected", "status": "failed"}],
        ),
    )

    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "P0 hard failure 原子性")
        before_active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        response = AgentService(
            connection,
            provider=provider,
        ).send_message(session.session_id, AgentMessageRequest(content=TURN_1))
        after_active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        accepted_patches = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ? AND validation_status = 'accepted'",
            (session.session_id,),
        ).fetchone()[0]

    assert before_active is None
    assert after_active is None
    assert response.version is None
    assert accepted_patches == 0
    assert response.assistant_turn.status != "completed"
    assert "完成" not in response.assistant_turn.content


def test_p0_real_shaped_missing_required_campus_is_atomic_in_runtime_response(monkeypatch, tmp_path):
    monkeypatch.setattr(AgentService, "_creative_portfolio_enabled", lambda _self, _context: False)
    provider = TwoTurnReactProvider()

    def unresolved_hard_campus_search(self, city, keyword, category="all", limit=10):
        keyword_text = str(keyword)
        if category == "campus" or any(
            marker in keyword_text
            for marker in ("985", "高校", "高等院校", "大学", "学院", "校区")
        ):
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="recorded-amap-hard-campus-miss",
                queriedAt=datetime.now(timezone.utc),
                pois=[],
            )
        return _recorded_map_search(self, city, keyword, category, limit)

    class RecordedControllerRuntime(TripAgentRuntime):
        def send_agent_message(self, session_id, payload, event_sink=None, **_kwargs):
            return AgentService(self.db, provider=provider).send_message(
                session_id,
                payload,
                event_sink=event_sink,
            )

    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "recorded-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(MapPoiService, "search", unresolved_hard_campus_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", _recorded_map_search_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)

    with _open_db() as connection:
        final, _exit_code = RecordedControllerRuntime(connection).run_once(
            RuntimeRunOptions(
                input=TURN_1,
                stateDir=str(tmp_path / "runs"),
                mockProviders=False,
            )
        )
        session_row = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (final.session_id,),
        ).fetchone()
        accepted_patches = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ? AND validation_status = 'accepted'",
            (final.session_id,),
        ).fetchone()[0]
        candidate_rows = connection.execute(
            "SELECT id, segment_id, status, candidates_json FROM amap_poi_candidates "
            "WHERE session_id = ? ORDER BY created_at",
            (final.session_id,),
        ).fetchall()

    verifier = json.loads((Path(final.artifact_path_absolute) / "verifier_report.json").read_text(encoding="utf-8"))
    # A missing required qualified campus is not a repairable direction.  The
    # runtime must stop before the Single-Writer instead of publishing a partial
    # timeline that silently drops the user's 985 requirement.
    assert verifier["passed"] is None
    assert verifier["checks"] == []
    assert verifier["hardFailures"] == []
    assert candidate_rows == []
    write_evidence = {
        "databaseActiveVersionId": session_row["active_version_id"],
        "responseActiveVersionId": final.active_version_id,
        "activeVersionChanged": final.active_version_changed,
        "acceptedPatchCount": accepted_patches,
    }
    assert write_evidence == {
        "databaseActiveVersionId": None,
        "responseActiveVersionId": None,
        "activeVersionChanged": False,
        "acceptedPatchCount": 0,
    }
    assert final.status == "candidate_refresh_required"
    assert final.terminal_status == "candidate_refresh_required"
    assert "核心意图地点未完成" in final.assistant_reply
    assert "暂未创建正式时间轴" in final.assistant_reply


class NoWriteRecordedController:
    model = "recorded-provider"

    def __init__(self) -> None:
        self.calls = 0
        self.tool_loop_calls = 0

    def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
        self.calls += 1
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "type": "ask_user",
                "question": "请选择是否继续。",
                "choiceIds": ["retry", "manual"],
            },
        }

    def run_tool_loop(self, *_args, **_kwargs):
        self.tool_loop_calls += 1
        raise AssertionError("no-write journey must not enter generic tool loop")


def test_p0_no_write_response_and_trace_do_not_claim_version_change():
    provider = NoWriteRecordedController()
    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "P0 no-write truth")
        response = AgentService(connection, provider=provider).send_message(
            session.session_id,
            AgentMessageRequest(content="重试本次修改"),
        )
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert provider.calls == 0
    assert provider.tool_loop_calls == 0
    assert active_version_id is None
    assert patch_count == 0
    assert response.version is None
    assert response.itinerary is None
    assert response.assistant_turn.status == "active"
    outcomes = [event for event in response.planning_steps if event.type == "agent_action_outcome"]
    assert outcomes
    assert all(event.metadata["resultPreview"].get("resultVersionId") is None for event in outcomes)
    assert all(not event.metadata["resultPreview"].get("patchIds") for event in outcomes)

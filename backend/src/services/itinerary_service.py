import inspect
import json
from hashlib import sha256
import math
import re
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.itineraries import (
    BudgetBreakdownResponse,
    EnrichmentDimensionStatusResponse,
    ItineraryDayResponse,
    ItineraryPlanResponse,
    ItinerarySegmentResponse,
    OnlineEnrichmentStatusResponse,
    PendingTimelineSlotResponse,
    POIRiskAlertResponse,
    PoiResponse,
    RouteOptionResponse,
    RouteCoverageResponse,
    TicketLookupResultResponse,
    TrafficCrowdingSignalResponse,
    WeatherSignalResponse,
)
from src.models.itinerary_day import ItineraryDay
from src.models.itinerary_plan import ItineraryPlan
from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.models.poi_intent import CandidateScore, GroundingRunContext, PersistableSegmentPlan, PoiIntent
from src.models.poi_risk_alert import POIRiskAlert
from src.models.route_option import RouteOption, normalize_route_mode, route_evidence_status
from src.models.ticket_lookup_result import TicketLookupResult
from src.models.traffic_crowding_signal import TrafficCrowdingSignal
from src.models.weather_signal import WeatherSignal
from src.services.amap_call_budget import current_amap_call_budget
from src.services.amap_poi_search_plan_adapter import (
    AmapPoiSearchPlan,
    AmapPoiSearchPlanAdapter,
)
from src.services.experience_search_profile_compiler import (
    search_profile_provider_rejection_reason,
)
from src.services.campus_candidate_policy import CampusCandidatePolicy
from src.services.creative_exploration_frontier_service import (
    CreativeExplorationFrontierService,
)
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.meal_candidate_quality_policy import MealCandidateQualityPolicy
from src.services.meal_experience_assignment import MealExperienceAssignmentPolicy
from src.services.night_view_candidate_policy import NightViewCandidatePolicy
from src.services.budget_invariant_policy import BudgetInvariantPolicy
from src.services.poi_trust_policy import PoiTrustPolicy
from src.services.poi_risk_service import POIRiskService
from src.services.route_service import RouteService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.ticket_service import TicketService
from src.services.traffic_service import TrafficService
from src.services.weather_service import WeatherService
from src.services.trip_date_resolver import TripDateResolver
from src.services.feasibility_service import FeasibilityService
from src.services.functional_slot_context_service import FunctionalSlotContext


TEMPLATE_CONFIG = {
    "custom": {
        "title": "地图行程草案",
        "transport_mode": "public_transit",
        "segment_cost": 30.0,
        "decision_rationale": "按识别 POI 生成空间顺序，并叠加路线、天气和拥挤风险。",
        "budget_delta_explanation": "预算为软约束，当前为初版估算。",
        "note": "按用户灵感生成，可继续编辑顺序和交通方式。",
    },
    "low_budget": {
        "title": "低预算",
        "transport_mode": "walk",
        "segment_cost": 18.0,
        "decision_rationale": "优先减少交通开销和高价项目，保留核心 POI。",
        "budget_delta_explanation": "尽量贴近预算，仍保留必要门票或预约成本。",
        "note": "优先步行和低价景点，适合控制预算。",
    },
    "photo_first": {
        "title": "拍照优先",
        "transport_mode": "public_transit",
        "segment_cost": 36.0,
        "decision_rationale": "优先安排适合拍照的 POI 和白天光线窗口。",
        "budget_delta_explanation": "拍照优先会保留更高价值机位，费用可能略高于低预算方案。",
        "note": "优先保留打卡点和白天拍摄窗口。",
    },
    "relaxed_pace": {
        "title": "轻松不赶路",
        "transport_mode": "taxi",
        "segment_cost": 48.0,
        "decision_rationale": "降低换乘和步行强度，减少赶路感并增加缓冲时间。",
        "budget_delta_explanation": "轻松不赶路使用更省力交通，预算可能高于目标值。",
        "note": "减少赶路和换乘，预留更宽松的停留时间。",
    },
}


AGENT_TEXT_TIMELINE_SOURCE = "agent-text-timeline"
AGENT_TEXT_TIMELINE_GROUNDING_NOTE = "高德 POI 待校验"
POI_SPECIFICITY_EXACT = "exact_entity"
POI_SPECIFICITY_AREA = "area_poi"
POI_SPECIFICITY_FUNCTIONAL = "functional_poi"
POI_SPECIFICITY_COMPOSITE = "composite_poi"
FUNCTIONAL_POI_RE = re.compile(r"(午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶|夜景|休息|购物|漫步)")
AREA_POI_RE = re.compile(r"(周边|附近|周围|一带|区域|商圈|片区|街区|胡同|园区)")
KNOWN_AREA_POI_NAMES: tuple[str, ...] = ()

AMAP_RATE_LIMIT_RE = re.compile(
    r"(provider_rate_limited|limit|quota|frequency|too many|HTTP\s*429|status\s*429|USER_DAILY_QUERY_OVER_LIMIT|USER_OVER_QUOTA|CUQPS_HAS_EXCEEDED|DAILY_QUERY_OVER_LIMIT|频率超限|查询频率|请求过于频繁|限流|超限)",
    re.IGNORECASE,
)
AMAP_RATE_LIMIT_CLASSIFICATIONS = {
    "qps_limited",
    "daily_quota_limited",
    "account_quota_limited",
    "http_429",
    "local_cooldown",
    "unknown_rate_limited",
}
AMAP_BUDGET_EXCEEDED_CODES = {"amap_budget_exceeded", "budget_exceeded"}
NON_LODGING_REJECTED_TYPE_RE = re.compile(r"(酒店|宾馆|旅馆|民宿|公寓|住宿|公司|写字楼|停车场|住宅|小区|房地产)")
NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE = re.compile(
    r"(售票|票务|门票|入口|出入口|游客中心|服务中心|管理处|管理中心|管理局|办公室|办事处|办公|办公楼|公交站|地铁站|"
    r"停车场|餐厅|饭店|酒店|公寓|公司|超市|商店|专卖店|儿童用品|水站|卫生间|拍照|留念|摄影|冲印|图书馆|"
    r"打卡|航拍|剪影|文化中心|文化馆|医院|学校内部|教学楼|食堂|宿舍)"
)
NIGHT_VIEW_WEAK_ENTITY_RE = re.compile(
    r"(酒店|宾馆|旅馆|民宿|公寓|住宿|图书馆|文化中心|文化馆|公司|写字楼|办公楼|服务中心|游客中心|管理处|管理中心|"
    r"售票|票务|门票|拍照|摄影|留念|购物小店|商店|专卖店|水站|停车场|医院|学校内部|教学楼|食堂|宿舍|管理局|办事处)"
)
NIGHT_VIEW_PUBLIC_ACCESS_RE = re.compile(
    r"(地标|广场|公园|桥|塔|观景|观景台|步道|滨水|江边|河岸|体育场|体育馆|商圈|购物中心|夜游|夜景|景区|"
    r"风景名胜|城市公共空间|公共区域)"
)
WEAK_ROUTE_ANCHOR_SUBENTITY_RE = re.compile(
    r"(售票|票务|门票|入口|出入口|游客中心|服务中心|客服|管理处|管理中心|管理局|办公室|办事处|办公|"
    r"停车场|卫生间|厕所|水站|拍照|摄影|留念|服务台|咨询台|售卖亭|摊位|收银|行政)"
)
CAMPUS_AFFILIATED_SUBENTITY_RE = re.compile(
    r"(国际学院|继续教育学院|网络教育|开放大学|广播电视大学|职业学院|培训|招生办|办公室|服务中心|附属|食堂|宿舍|医院)"
)
CAMPUS_REMOTE_BRANCH_RE = re.compile(r"(远郊|分校区|新校区|大学城|校区东区|校区西区|校区南区|校区北区)")
CAMPUS_REMOTE_ROUTE_LOCATION_RE = re.compile(
    r"(通州|良乡|昌平|沙河|远郊|大学城|东校区|西校区|校区东区|校区西区|东区校区|西区校区)"
)
INTENT_PREFERRED_TYPE_RE = {
    "night_view": re.compile(r"(风景名胜|地标|桥|塔|广场|商圈|购物中心|体育|观景|商务住宅;楼宇|摩天|公园|景点)"),
    "campus_visit": re.compile(r"(大学|学院|高等院校|学校|科教文化服务)"),
    "meal": re.compile(r"(餐饮服务|中餐厅|西餐厅|小吃|咖啡|美食|饭店|餐厅)"),
    "museum": re.compile(r"(博物馆|展览馆|科教文化服务|风景名胜)"),
    "park": re.compile(r"(公园|风景名胜|广场)"),
    "shopping": re.compile(r"(购物|商场|购物中心|商圈|商业)"),
    "landmark": re.compile(r"(风景名胜|地标|广场|建筑|景点|名胜)"),
    "area_walk": re.compile(r"(风景名胜|街区|步行街|商圈|公园|广场|桥)"),
    "local_culture": re.compile(r"(历史街区|胡同|社区|市场|非遗|民俗|文化馆|博物馆|纪念馆|古迹|步行街|风景名胜)"),
    "rest": re.compile(r"(咖啡|茶馆|休闲|餐饮服务)"),
}


class ItineraryService:
    def __init__(
        self,
        db: sqlite3.Connection,
        route_service: Optional[RouteService] = None,
        weather_service: Optional[WeatherService] = None,
        traffic_service: Optional[TrafficService] = None,
        map_poi_service: Optional[MapPoiService] = None,
        poi_risk_service: Optional[POIRiskService] = None,
        ticket_service: Optional[TicketService] = None,
    ):
        self.db = db
        self.route_service = route_service or RouteService()
        self.weather_service = weather_service or WeatherService()
        self.traffic_service = traffic_service or TrafficService()
        self.map_poi_service = map_poi_service or MapPoiService()
        self.poi_risk_service = poi_risk_service or POIRiskService()
        self.ticket_service = ticket_service or TicketService(db)
        self.route_warnings: list[str] = []
        self._poi_grounding_rate_limited = False
        self._poi_grounding_rate_limit_reason = ""
        self._route_aware_request_budget = 4
        self._route_aware_request_count = 0
        self._last_candidate_collection_stats: dict[str, Any] = {}
        self._force_candidate_refresh = False
        self._expand_density_nearby = False
        # Continuation-only callbacks supplied by AgentService.  The data plane
        # remains usable for cursor=0 initial planning without a persisted root;
        # retries never manufacture a local cursor or fall back to a cache key.
        self._night_view_query_progress: Optional[dict[str, Any]] = None

        self.campus_candidate_policy = CampusCandidatePolicy()
        self.intent_candidate_semantic_policy = IntentCandidateSemanticPolicy()
        self.meal_candidate_quality_policy = MealCandidateQualityPolicy()
        self.meal_experience_assignment_policy = MealExperienceAssignmentPolicy()
        self.night_view_candidate_policy = NightViewCandidatePolicy()
        self.poi_trust_policy = PoiTrustPolicy()

    def configure_night_view_query_progress(
        self,
        *,
        scope_identity: dict[str, Any],
        attempt_identity: str,
        claim_query: Any,
        complete_query: Any,
    ) -> None:
        """Install the existing persisted-frontier callbacks for one retry only.

        No initial request is configured here.  The opaque execution identity is
        claimed by AgentService before this service is constructed, and callers
        must pass the existing Portfolio-store CAS methods rather than a cache.
        """

        if not isinstance(scope_identity, dict) or not callable(claim_query) or not callable(complete_query):
            raise ValueError("night_view_progress_configuration_invalid")
        self._night_view_query_progress = {
            "scopeIdentity": dict(scope_identity),
            "attemptIdentity": str(attempt_identity or ""),
            "claimQuery": claim_query,
            "completeQuery": complete_query,
        }

    def generate_for_inspiration(
        self,
        inspiration_set_id: str,
        city: str,
        template_type: str = "custom",
        preference_profile_id: Optional[str] = None,
        preference_summary: Optional[str] = None,
        planning_context: Optional[dict] = None,
        date_range: Optional[dict] = None,
    ) -> ItineraryPlanResponse:
        inspiration = self.db.execute("SELECT * FROM inspiration_sets WHERE id = ?", (inspiration_set_id,)).fetchone()
        if inspiration is None:
            raise HTTPException(status_code=404, detail="Inspiration set not found")

        extraction = self._latest_extraction(inspiration_set_id)
        city = city or inspiration["city"] or self._first_json_value(extraction, "city_candidates") or "北京"
        style_tags = self._json_list(extraction, "style_tags")
        poi_candidates = self._json_list(extraction, "poi_candidates")
        self.route_warnings = []
        pois = self._build_pois(city, poi_candidates)

        template = TEMPLATE_CONFIG.get(template_type, TEMPLATE_CONFIG["custom"])
        transport_mode = str(template["transport_mode"])
        segment_cost = float(template["segment_cost"])
        decision_rationale = str(template["decision_rationale"])
        budget_delta_explanation = str(template["budget_delta_explanation"])
        budget_target = None
        preference = self._load_preference(preference_profile_id)
        risk_context = self._risk_context(preference, preference_summary, planning_context, date_range)
        risk_context = {
            **risk_context,
            "travelTimeWindows": self._planned_time_windows(pois),
        }
        if preference is not None:
            budget_target = self._budget_number(preference["budget_range"])
            if "轻松" in preference["pace_preference"] and template_type == "custom":
                transport_mode = "taxi"
                segment_cost = max(segment_cost, 42.0)
            decision_rationale = (
                f"{decision_rationale} 已应用偏好：{preference['party_size']}人，{preference['pace_preference']}。"
            )
            if preference["budget_range"]:
                budget_delta_explanation = f"已参考偏好预算 {preference['budget_range']}，预算仍作为软约束。"
        plan = ItineraryPlan(
            id=f"plan_{uuid4().hex[:12]}",
            user_id=inspiration["user_id"],
            inspiration_set_id=inspiration_set_id,
            template_type=template_type,
            title=f"{city}{template['title']}",
            city=city,
            budget_target=budget_target,
            budget_estimate=segment_cost * max(1, len(pois)),
            budget_delta_explanation=budget_delta_explanation,
            decision_rationale=decision_rationale,
        )
        day = ItineraryDay(
            id=f"day_{uuid4().hex[:12]}",
            plan_id=plan.id,
            day_number=1,
            title="Day 1 初版可编辑行程",
            weather_summary="",
            risk_summary="",
            total_estimated_cost=plan.budget_estimate,
        )
        weather = self.weather_service.build_weather_signal(city, style_tags, risk_context)
        segments = self._build_segments(
            day.id,
            pois,
            weather,
            [],
            transport_mode=transport_mode,
            segment_cost=segment_cost,
            note=str(template["note"]),
        )
        routes = self.route_service.build_routes(plan.id, pois, transport_mode, segments=segments)
        traffic_signals = self.traffic_service.build_signals(routes)
        ticket_results = self.ticket_service.build_pending_for_segments(segments, pois)
        poi_risk_alerts = self.poi_risk_service.build_alerts(
            plan.id,
            city,
            pois,
            segments,
            weather,
            traffic_signals,
            ticket_results,
            route_options=routes,
            risk_context=risk_context,
        )
        day.weather_summary = weather.daily_summary
        day.risk_summary = self._risk_summary(weather, traffic_signals)

        self._persist_plan(plan, pois, day, segments, routes, weather, traffic_signals, poi_risk_alerts, ticket_results)
        self.db.commit()
        return self._to_response(
            plan,
            [day],
            segments,
            pois,
            routes,
            [weather],
            traffic_signals,
            poi_risk_alerts,
            ticket_results,
            [*self.route_warnings, *self.route_service.warnings],
        )

    def get_plan(self, plan_id: str) -> ItineraryPlanResponse:
        plan = self._load_plan(plan_id)
        days = self._load_days(plan_id)
        segments = self._load_segments(plan_id)
        pois = self._load_pois(plan_id)
        routes = self._load_routes(plan_id)
        weather = self._load_weather(plan_id)
        traffic = self._load_traffic([route.id for route in routes])
        poi_risk_alerts = self._load_poi_risk_alerts(plan_id)
        tickets = self._load_tickets(plan_id)
        return self._to_response(plan, days, segments, pois, routes, weather, traffic, poi_risk_alerts, tickets, [])

    def apply_edit(self, plan_id: str, operation: str, segment_id: str, value: str) -> ItineraryPlanResponse:
        return self.apply_edit_with_context(plan_id, operation, segment_id, value)

    def apply_edit_with_context(
        self,
        plan_id: str,
        operation: str,
        segment_id: str,
        value: str,
        preference_summary: str = "",
        planning_context: Optional[dict] = None,
    ) -> ItineraryPlanResponse:
        if operation != "replace_transport_mode":
            raise HTTPException(status_code=400, detail=f"Unsupported itinerary edit operation: {operation}")

        self._load_plan(plan_id)
        segment = self.db.execute(
            "SELECT * FROM itinerary_segments WHERE id = ? AND plan_id = ?",
            (segment_id, plan_id),
        ).fetchone()
        if segment is None:
            raise HTTPException(status_code=404, detail="Itinerary segment not found")

        self.db.execute(
            "UPDATE itinerary_segments SET transport_mode = ? WHERE id = ? AND plan_id = ?",
            (value, segment_id, plan_id),
        )
        self.db.commit()
        self.refresh_planning_tools(
            plan_id,
            preference_summary=preference_summary,
            planning_context=planning_context,
            preferred_mode=normalize_route_mode(value),
            commit_between_tools=True,
        )
        self.db.commit()
        return self.get_plan(plan_id)

    def refresh_routes(
        self,
        plan_id: str,
        preferred_mode: Optional[str] = None,
        route_pairs: Optional[set[tuple[str, str]]] = None,
        preferred_mode_only: bool = False,
        include_compact_fallbacks: bool = False,
        allow_semantic_route_anchors: bool = False,
    ) -> list[str]:
        pois = self._load_pois(plan_id)
        segments = self._load_segments(plan_id)
        transport_mode = preferred_mode or segments[0].transport_mode if segments else "walking"
        segments = self._route_consumer_segments(segments, pois)
        build_kwargs = {"segments": segments, "route_pairs": route_pairs}
        if preferred_mode_only:
            build_kwargs["preferred_mode_only"] = True
        if include_compact_fallbacks:
            build_kwargs["include_compact_fallbacks"] = True
        if allow_semantic_route_anchors:
            build_kwargs["allow_semantic_route_anchors"] = True
        routes = self.route_service.build_routes(plan_id, pois, transport_mode, **build_kwargs)
        traffic = self.traffic_service.build_signals(routes)
        self._delete_routes(plan_id, route_pairs)
        for route in routes:
            self._insert_route(route)
        for signal in traffic:
            self._insert_traffic(signal)
        return [*self.route_service.warnings, *self._route_quality_user_warnings(plan_id)]

    def refresh_unfinished_pois(self, plan_id: str, preferred_mode: Optional[str] = None) -> list[str]:
        warnings: list[str] = []
        plan = self._load_plan(plan_id)
        pois = self._load_pois(plan_id)
        draft_poi_ids = {
            poi.id
            for poi in pois
            if self._poi_grounding_metadata(poi)["groundingStatus"] == "draft_only"
            and self._poi_has_required_visit_segment(plan_id, poi.id)
        }
        if not draft_poi_ids:
            return ["没有需要重试的 draft_only POI，已跳过高德查询和路线重算。"]
        grounded, grounding_warnings = self._ground_agent_text_timeline_pois(plan, pois, only_poi_ids=draft_poi_ids)
        warnings.extend(grounding_warnings)
        if grounded:
            route_pairs = self._route_pairs_touching_pois(plan_id, draft_poi_ids)
            if route_pairs:
                warnings.extend(self.refresh_routes(plan_id, preferred_mode=preferred_mode, route_pairs=route_pairs))
            self._refresh_plan_totals(plan_id)
        return warnings

    def _route_pairs_touching_pois(self, plan_id: str, poi_ids: set[str]) -> set[tuple[str, str]]:
        rows = self.db.execute(
            """
            SELECT
                s.id, s.day_id, s.poi_id, s.kind, s.semantic_metadata_json, s.notes,
                p.amap_id, p.latitude, p.longitude, p.source, p.confidence,
                p.name, p.category, p.source_note
            FROM itinerary_segments s
            JOIN itinerary_days d ON d.id = s.day_id
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ?
            ORDER BY d.day_number ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        pairs: set[tuple[str, str]] = set()
        by_day: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            metadata = self._poi_grounding_metadata_from_values(
                source=row["source"],
                amap_id=row["amap_id"],
                longitude=row["longitude"],
                latitude=row["latitude"],
                confidence=row["confidence"],
                name=row["name"],
                source_note=row["source_note"],
            )
            if not (
                self._is_grounded_park_route_anchor_from_row(row)
                if row["kind"] == "park"
                else self._is_route_anchor_kind(row["kind"], metadata)
            ):
                continue
            by_day.setdefault(row["day_id"], []).append(row)
        for day_rows in by_day.values():
            for from_segment, to_segment in zip(day_rows, day_rows[1:]):
                if from_segment["poi_id"] in poi_ids or to_segment["poi_id"] in poi_ids:
                    pairs.add((from_segment["id"], to_segment["id"]))
        return pairs

    def refresh_planning_tools(
        self,
        plan_id: str,
        preference_summary: Optional[str] = None,
        planning_context: Optional[dict] = None,
        preferred_mode: Optional[str] = None,
        commit_between_tools: bool = False,
        route_pairs: Optional[set[tuple[str, str]]] = None,
    ) -> list[str]:
        warnings: list[str] = []
        plan = self._load_plan(plan_id)
        pois = self._load_pois(plan_id)
        segments = self._load_segments(plan_id)
        if not segments:
            return ["行程暂无可查询的日程段，已跳过路线、天气、票务和风险工具刷新。"]

        grounded, grounding_warnings = self._ground_agent_text_timeline_pois(plan, pois)
        warnings.extend(grounding_warnings)
        if grounded:
            if commit_between_tools:
                self.db.commit()
            pois = self._load_pois(plan_id)
        if self._poi_grounding_rate_limited:
            warnings.append("provider_rate_limited: 地图服务限流，已停止本轮路线、天气、票务和风险刷新，稍后重试。")
            self._refresh_plan_totals(plan_id)
            return warnings

        try:
            if preferred_mode is None:
                warnings.extend(self.refresh_routes(plan_id, route_pairs=route_pairs))
            else:
                warnings.extend(self.refresh_routes(plan_id, preferred_mode=preferred_mode, route_pairs=route_pairs))
            if commit_between_tools:
                self.db.commit()
        except Exception as error:
            self._rollback_failed_tool_step(commit_between_tools)
            warnings.append(f"地图/路线工具失败，行程草案仍保留：{error}")

        weather: Optional[WeatherSignal] = None
        try:
            weather = self._refresh_weather_signal(plan, pois, segments, preference_summary, planning_context)
            if commit_between_tools:
                self.db.commit()
        except Exception as error:
            self._rollback_failed_tool_step(commit_between_tools)
            warnings.append(f"高德天气工具失败，行程草案仍保留：{error}")

        ticket_results: list[TicketLookupResult] = []
        try:
            ticket_results = self._ensure_pending_ticket_results(plan_id, segments, pois)
            if commit_between_tools:
                self.db.commit()
        except Exception as error:
            self._rollback_failed_tool_step(commit_between_tools)
            warnings.append(f"票务/预约待查询状态刷新失败，行程草案仍保留：{error}")

        try:
            if weather is None:
                weather_rows = self._load_weather(plan_id)
                weather = weather_rows[0] if weather_rows else None
            if weather is not None:
                routes = self._load_routes(plan_id)
                traffic = self._load_traffic([route.id for route in routes])
                self._refresh_poi_risk_alerts(
                    plan,
                    pois,
                    segments,
                    weather,
                    traffic,
                    ticket_results or self._load_tickets(plan_id),
                    routes,
                    preference_summary,
                    planning_context,
                )
                if commit_between_tools:
                    self.db.commit()
        except Exception as error:
            self._rollback_failed_tool_step(commit_between_tools)
            warnings.append(f"景点风险搜索失败，行程草案仍保留：{error}")

        self._refresh_plan_totals(plan_id)
        return warnings

    def _route_quality_user_warnings(self, plan_id: str) -> list[str]:
        try:
            from src.services.agent_verifier_service import AgentVerifierService

            report = AgentVerifierService(self.db).verify_route_feasibility(plan_id, "route_refresh")
        except Exception:
            return []
        warnings = list(report.metadata.get("routeQualityWarnings") or [])
        return [warning for warning in warnings if warning]

    def refresh_staged_enrichment(
        self,
        plan_id: str,
        session_id: str,
        planning_context: Optional[dict] = None,
        preference_summary: Optional[str] = None,
        preferred_mode: Optional[str] = None,
        include_optional_enrichment: bool = True,
    ) -> dict:
        report: dict[str, Any] = {
            "state": "draft_created",
            "mapReady": False,
            "routeReady": False,
            "riskChecked": False,
            "riskStatus": "not_required",
            "weatherStatus": "not_checked",
            "reservationStatus": "not_requested",
            "warnings": [],
            "providerFailures": [],
        }
        warnings: list[str] = report["warnings"]
        deadline = (planning_context or {}).get("runtimeDeadlineMonotonic")
        deadline_budget = (planning_context or {}).get("stagedDeadlineBudget")
        report["deadlineBudgetMs"] = (
            int((deadline_budget or {}).get("totalMs") or 0) if isinstance(deadline_budget, dict) else 0
        )
        report["deadlineExceeded"] = False
        plan = self._load_plan(plan_id)
        pois = self._load_pois(plan_id)
        segments = self._load_segments(plan_id)
        if not segments:
            warnings.append("行程暂无可 enrichment 的日程段。")
            return report

        if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
            report["deadlineExceeded"] = True
            report["routeStatus"] = "deadline_exceeded"
            report["riskStatus"] = "pending"
            report["optionalEnrichmentDeferred"] = True
            warnings.append("规划总时限已到，保留已创建的可编辑版本并停止 enrichment。")
            return report

        try:
            grounded, grounding_warnings = self._ground_agent_text_timeline_pois(plan, pois)
            warnings.extend(grounding_warnings)
            if grounded:
                self.db.commit()
                pois = self._load_pois(plan_id)
        except Exception as error:
            self._rollback_failed_tool_step(True)
            warnings.append(f"AMap POI grounding 失败，保留可编辑草稿：{error}")
            report["providerFailures"].append({"stage": "poi_grounding", "reason": str(error)})

        map_report = self._map_readiness_summary(plan_id)
        report["mapReadiness"] = map_report
        report["mapReady"] = bool(map_report.get("mapReady"))
        map_ready_count = len([item for item in map_report.get("items") or [] if item.get("status") == "map_ready"])
        if report["mapReady"]:
            report["state"] = "map_ready"
        elif map_ready_count:
            report["state"] = "map_partial"
        else:
            report["state"] = "draft_created"

        grounding_context = (
            planning_context.get("candidateFirstGrounding")
            if isinstance((planning_context or {}).get("candidateFirstGrounding"), dict)
            else {}
        )
        if grounding_context.get("rateLimited"):
            report["state"] = "provider_rate_limited"
            report["resultState"] = "provider_rate_limited"
            report["routeStatus"] = "waiting_for_poi_grounding"
            report["routeReady"] = False
            report["partialSuccess"] = True
            warnings.append("地图服务限流，部分地点未完成，稍后重试。")
            return report

        routes = []
        expected_route_legs = self._expected_route_leg_count(segments, planning_context, pois)
        if expected_route_legs > 0:
            if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
                report["deadlineExceeded"] = True
                report["routeStatus"] = "deadline_exceeded"
                report["optionalEnrichmentDeferred"] = True
                warnings.append("路线阶段开始前已达到总时限；路线待后续刷新。")
                self._refresh_plan_totals(plan_id)
                return report
            try:
                route_warnings = self.refresh_routes(
                    plan_id,
                    preferred_mode=preferred_mode,
                    preferred_mode_only=True,
                )
                warnings.extend(route_warnings)
                from src.services.itinerary_schedule_service import ItineraryScheduleService

                report["scheduleUpdatedCount"] = ItineraryScheduleService(self.db).recompute_plan_schedule(plan_id)
                self.db.commit()
            except Exception as error:
                self._rollback_failed_tool_step(True)
                warnings.append(f"路线工具失败，已保留行程草案：{error}")
                report["providerFailures"].append({"stage": "route", "reason": str(error)})
            routes = self._load_routes(plan_id)
            usable_routes = [route for route in routes if not route.error]
            coverage = self._route_coverage_summary(plan_id, segments, pois)
            report["routeCoverage"] = coverage
            report["routeReady"] = bool(coverage["routeReady"])
            from src.services.agent_verifier_service import AgentVerifierService

            schedule_report = AgentVerifierService(self.db).verify_schedule_feasibility(plan_id, "staged_enrichment")
            report["scheduleVerifier"] = schedule_report.as_metadata()
            report["scheduleConflictCount"] = int(schedule_report.metadata.get("scheduleConflictCount") or 0)
            if not schedule_report.passed:
                report["routeReady"] = False
            route_decision_contract = self._planning_route_decision_contract(planning_context)
            provider_matrix_verified = self._planning_provider_matrix_verified(
                planning_context,
                route_decision_contract,
            )
            if report["routeReady"] and not provider_matrix_verified:
                report["routeReady"] = False
                report["routeStatus"] = "pending_provider_verification"
                warnings.append(
                    "路线已返回 Provider 路段，但缺少同一合同下可重验的插入矩阵；当前仅作诊断，不声明已完成路线可行性。"
                )
            else:
                report["routeStatus"] = (
                    "route_ready"
                    if report["routeReady"]
                    else "route_partial"
                    if usable_routes
                    else "needs_verification"
                )
            if report["routeReady"]:
                report["state"] = "route_ready"
            elif usable_routes:
                report["state"] = "route_partial"
                if report["routeStatus"] != "pending_provider_verification":
                    warnings.append("已生成可用路段，缺少坐标的路段待补全。")
            else:
                warnings.append("路线未完全生成，已保留行程并标记 route.status=needs_verification。")
        else:
            report["routeStatus"] = "not_required"

        if not include_optional_enrichment:
            report["riskStatus"] = "pending"
            report["reservationStatus"] = "pending"
            availability = WeatherService().availability_contract(planning_context)
            report["weatherAvailability"] = availability
            report["weatherStatus"] = availability["status"]
            if availability["status"] == "outside_forecast_window":
                self._refresh_staged_weather(plan, pois, segments, preference_summary, planning_context, report)
            report["optionalEnrichmentDeferred"] = True
            report["warnings"].append("预约、风险和天气核验未在初始关键路径执行；可按需刷新。")
            report["partialSuccess"] = bool(
                report["providerFailures"]
                or not report["mapReady"]
                or (expected_route_legs > 0 and not report["routeReady"])
            )
            self._refresh_plan_totals(plan_id)
            return report

        weather = self._refresh_staged_weather(plan, pois, segments, preference_summary, planning_context, report)
        risk_segments = self._staged_risk_segments(pois, segments, planning_context)
        if risk_segments and weather is not None:
            try:
                routes = self._load_routes(plan_id)
                traffic = self._load_traffic([route.id for route in routes])
                risk_context = {
                    **(planning_context or {}),
                    "officialSourceFirst": True,
                    "maxRiskSearchResults": 5 if self._is_holiday_risk_context(planning_context) else 3,
                    "riskSearchBudget": len(risk_segments),
                    "maxRiskSearchQueries": min(4, max(1, len(risk_segments) * 2)),
                    "maxRiskProviderAttempts": 2,
                    "maxRiskSearchSeconds": 12,
                    "maxRiskSynthesisCalls": 1,
                }
                alerts = self._refresh_poi_risk_alerts(
                    plan,
                    pois,
                    risk_segments,
                    weather,
                    traffic,
                    self._load_tickets(plan_id),
                    routes,
                    preference_summary,
                    risk_context,
                )
                self.db.commit()
                usable_alerts = [
                    alert
                    for alert in alerts
                    if alert.status in {"available", "degraded"} and (alert.source_url or alert.sources)
                ]
                usable_segment_ids = {alert.segment_id for alert in usable_alerts}
                expected_risk_segment_ids = {segment.id for segment in risk_segments}
                all_risk_segments_checked = bool(expected_risk_segment_ids) and expected_risk_segment_ids.issubset(
                    usable_segment_ids
                )
                report["riskChecked"] = all_risk_segments_checked
                report["riskStatus"] = "checked" if all_risk_segments_checked else "pending"
                if report["routeReady"] and all_risk_segments_checked:
                    report["state"] = "full_success"
                elif report["mapReady"] and all_risk_segments_checked:
                    report["state"] = "risk_checked"
                elif risk_segments and not all_risk_segments_checked:
                    if report["state"] in {"map_ready", "route_ready", "full_success"}:
                        report["state"] = "risk_pending"
                    warnings.append("高风险 POI 尚未取得可用官方/近期来源，风险状态保持 pending。")
            except Exception as error:
                self._rollback_failed_tool_step(True)
                warnings.append(f"高风险 POI 官方来源检索失败，行程保留为 needs_verification：{error}")
                report["providerFailures"].append({"stage": "risk", "reason": str(error)})
        else:
            report["riskChecked"] = False
            report["riskStatus"] = "pending" if risk_segments else "not_required"
            if risk_segments:
                if report["state"] in {"map_ready", "route_ready", "full_success"}:
                    report["state"] = "risk_pending"
                warnings.append("天气基准不可用，已跳过风险综合判断并保留 needs_verification。")

        report["partialSuccess"] = bool(
            report["providerFailures"]
            or not report["mapReady"]
            or (self._expected_route_leg_count(segments, planning_context, pois) > 0 and not report["routeReady"])
            or report.get("riskStatus") == "pending"
        )
        if report["providerFailures"] and report["state"] == "full_success":
            report["state"] = "partial_success"
        self._refresh_plan_totals(plan_id)
        return report

    @staticmethod
    def _planning_route_decision_contract(
        planning_context: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        context = planning_context if isinstance(planning_context, dict) else {}
        request_contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        raw = context.get("routeDecisionContract")
        if not isinstance(raw, dict):
            raw = request_contract.get("routeDecisionContract")
        return RouteInsertionScorer.normalized_route_decision_contract(raw)

    @staticmethod
    def _planning_provider_matrix_verified(
        planning_context: Optional[dict[str, Any]],
        route_decision_contract: Optional[dict[str, Any]],
    ) -> bool:
        if route_decision_contract is None:
            return False
        context = planning_context if isinstance(planning_context, dict) else {}
        proofs = context.get("routeInsertionProofs")
        if not isinstance(proofs, list) or not proofs:
            return False
        for proof in proofs:
            if not isinstance(proof, dict):
                return False
            proof_contract = RouteInsertionScorer.normalized_route_decision_contract(proof.get("routeDecisionContract"))
            legs = proof.get("routeMatrix")
            if not isinstance(legs, dict):
                legs = proof.get("legs")
            if (
                proof.get("status") != "passed"
                or proof.get("networkVerified") is not True
                or proof.get("timeWindowFeasible") is not True
                or proof_contract is None
                or proof_contract.get("fingerprint") != route_decision_contract.get("fingerprint")
                or not isinstance(legs, dict)
                or not legs
            ):
                return False
        return True

    def _route_coverage_summary(
        self, plan_id: str, segments: list[ItinerarySegment], pois: list[POI]
    ) -> dict[str, Any]:
        route_group_service = (
            self.route_service if hasattr(self.route_service, "_route_groups") else RouteService(map_provider_key="")
        )
        route_segments = self._route_consumer_segments(segments, pois)
        required = [
            (str(a.id), str(b.id), from_poi.name, to_poi.name)
            for a, b, from_poi, to_poi in route_group_service._route_groups(pois, route_segments)
            if a is not None and b is not None
        ]
        rows = self.db.execute(
            "SELECT from_segment_id, to_segment_id FROM route_options "
            "WHERE plan_id = ? AND error_json IS NULL GROUP BY from_segment_id, to_segment_id",
            (plan_id,),
        ).fetchall()
        covered = {(str(row["from_segment_id"]), str(row["to_segment_id"])) for row in rows}
        missing = [
            {"fromSegmentId": a, "toSegmentId": b, "fromPoiName": c, "toPoiName": d}
            for a, b, c, d in required
            if (a, b) not in covered
        ]
        required_count = len(required)
        covered_count = required_count - len(missing)
        return {
            "requiredLegCount": required_count,
            "coveredLegCount": covered_count,
            "missingLegCount": len(missing),
            "missingLegs": missing,
            "coverageRatio": covered_count / required_count if required_count else 1.0,
            "routeReady": bool(required_count) and not missing,
        }

    def _expected_route_leg_count(
        self,
        segments: list[ItinerarySegment],
        planning_context: Optional[dict] = None,
        pois: Optional[list[POI]] = None,
    ) -> int:
        excluded_kinds = self._memory_excluded_route_segment_kinds(planning_context)
        pois_by_id = {poi.id: poi for poi in pois or []}
        by_day: dict[str, list[ItinerarySegment]] = {}
        for segment in segments:
            if not self._is_route_anchor_segment(segment, pois_by_id.get(segment.poi_id), excluded_kinds):
                continue
            by_day.setdefault(segment.day_id, []).append(segment)
        return sum(max(0, len(day_segments) - 1) for day_segments in by_day.values())

    def _memory_excluded_route_segment_kinds(self, planning_context: Optional[dict]) -> set[str]:
        context = planning_context if isinstance(planning_context, dict) else {}
        rules = context.get("memoryRules") if isinstance(context.get("memoryRules"), dict) else {}
        route_rules = rules.get("routePlanning") if isinstance(rules.get("routePlanning"), dict) else {}
        excluded = (
            route_rules.get("excludedSegmentKinds") if isinstance(route_rules.get("excludedSegmentKinds"), list) else []
        )
        return {str(item) for item in excluded} or {"rest", "note", "transport", "buffer"}

    def _map_readiness_summary(self, plan_id: str) -> dict:
        rows = self.db.execute(
            """
            SELECT
                s.id AS segment_id, s.kind AS segment_kind, p.id AS poi_id,
                s.semantic_metadata_json, s.notes,
                p.name, p.amap_id, p.latitude, p.longitude, p.source,
                p.confidence, p.category, p.source_note
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ?
            ORDER BY s.day_id ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        items = []
        missing = []
        for row in rows:
            metadata = self._poi_grounding_metadata_from_values(
                source=row["source"],
                amap_id=row["amap_id"],
                longitude=row["longitude"],
                latitude=row["latitude"],
                confidence=row["confidence"],
                name=row["name"],
                source_note=row["source_note"],
            )
            if not (
                self._is_grounded_park_route_anchor_from_row(row)
                if row["segment_kind"] == "park"
                else self._is_route_anchor_kind(row["segment_kind"], metadata)
            ):
                continue
            item = {
                "segmentId": row["segment_id"],
                "poiId": row["poi_id"],
                "poiName": row["name"],
                "status": metadata["groundingStatus"],
                "groundingStatus": metadata["groundingStatus"],
                "mapReady": metadata["mapReady"],
                "routeable": metadata["routeable"],
                "matchedAmapName": metadata["matchedAmapName"],
                "poiSpecificity": metadata["poiSpecificity"],
                "intentType": metadata["intentType"],
                "needsConcretePoi": metadata["needsConcretePoi"],
                "hasProviderPoiId": bool(row["amap_id"]),
                "hasCoordinates": self._valid_coordinate(row["longitude"], row["latitude"]),
                "source": row["source"],
            }
            items.append(item)
            if not metadata["mapReady"]:
                missing.append(f"{row['name']} missing confirmed or routeable AMap anchor")
        required_ready = not missing
        return {
            "mapReady": required_ready,
            "requiredMapReady": required_ready,
            "optionalMapReady": True,
            "checked": len(rows),
            "missing": missing,
            "items": items,
        }

    def _refresh_staged_weather(
        self,
        plan: ItineraryPlan,
        pois: list[POI],
        segments: list[ItinerarySegment],
        preference_summary: Optional[str],
        planning_context: Optional[dict],
        report: dict,
    ) -> Optional[WeatherSignal]:
        resolved_dates = (
            planning_context.get("resolvedTripDates")
            if isinstance((planning_context or {}).get("resolvedTripDates"), dict)
            else {}
        )
        if resolved_dates.get("status") == "resolved" and not resolved_dates.get("weatherForecastSupported"):
            weather = WeatherSignal(
                id=f"weather_{uuid4().hex[:10]}",
                city=plan.city,
                date=str(resolved_dates.get("startDate") or resolved_dates.get("dates", [""])[0] or ""),
                hourly_forecast=[],
                daily_summary="已识别出行日期，尚未进入天气预报窗口；当前不使用今日天气推断未来行程。",
                risk_level="unknown",
                purpose_impact_reason="forecast_not_supported_yet: 等临近出行日再查询天气，避免把今日天气误用于未来行程。",
                source="weather-forecast-window",
                data_status="outside_forecast_window",
                confidence=0.0,
                failure_reason="forecast_not_supported_yet",
                source_url=None,
                user_visible_caveat="当前未查询/未使用今日天气，请在出行前 3 天内刷新天气。",
                provider_name="local-date-guard",
                fallback_used=False,
            )
            self.db.execute("DELETE FROM weather_signals WHERE plan_id = ?", (plan.id,))
            self._insert_weather(plan.id, weather)
            self.db.execute(
                "UPDATE itinerary_segments SET weather_signal_id = ? WHERE plan_id = ?", (weather.id, plan.id)
            )
            self.db.execute(
                "UPDATE itinerary_days SET weather_summary = ?, risk_summary = ? WHERE plan_id = ?",
                (weather.daily_summary, weather.daily_summary, plan.id),
            )
            self.db.commit()
            report["weatherStatus"] = weather.data_status
            report["weatherAvailability"] = WeatherService().availability_contract(planning_context)
            return weather
        try:
            weather = self._refresh_weather_signal(plan, pois, segments, preference_summary, planning_context)
            self.db.commit()
            report["weatherStatus"] = weather.data_status
            return weather
        except Exception as error:
            self._rollback_failed_tool_step(True)
            report["weatherStatus"] = "provider_down"
            report["providerFailures"].append({"stage": "weather", "reason": str(error)})
            report["warnings"].append(f"天气工具不可用，行程保留为 needs_verification：{error}")
            return None

    def _staged_risk_segments(
        self,
        pois: list[POI],
        segments: list[ItinerarySegment],
        planning_context: Optional[dict],
    ) -> list[ItinerarySegment]:
        pois_by_id = {poi.id: poi for poi in pois}
        resolved_dates = (
            planning_context.get("resolvedTripDates")
            if isinstance((planning_context or {}).get("resolvedTripDates"), dict)
            else {}
        )
        national_day = any(
            str(item)[5:10] in {"10-01", "10-02", "10-03", "10-04", "10-05", "10-06", "10-07"}
            for item in resolved_dates.get("dates") or []
        )
        terms = ("大学", "学院", "博物馆", "美术馆", "体育场", "体育馆", "演唱会", "景区", "故宫", "长城", "迪士尼")
        ranked: list[tuple[int, int, ItinerarySegment]] = []
        for segment in segments:
            poi = pois_by_id.get(segment.poi_id)
            text = " ".join(
                [poi.name if poi else "", poi.category if poi else "", poi.type if poi else "", segment.notes or ""]
            )
            priority = self._risk_segment_priority(segment, poi, text, holiday_context=national_day)
            if priority <= 0:
                continue
            if national_day or any(term in text for term in terms):
                ranked.append((priority, segment.segment_order, segment))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [segment for _priority, _order, segment in ranked[:3]]

    def _risk_segment_priority(
        self,
        segment: ItinerarySegment,
        poi: Optional[POI],
        text: str,
        *,
        holiday_context: bool,
    ) -> int:
        explicit_risk = bool(re.search(r"(预约|限流|管控|闭馆|闭园|排队|营业|关门|热门|演唱会|赛事|大型活动)", text))
        if segment.kind in {"rest", "note", "buffer", "area_walk"}:
            return 0
        if segment.kind == "meal" and not self._meal_needs_risk_check(poi, segment, text):
            return 0
        if not explicit_risk and not self._risk_poi_has_provider_identity(poi):
            return 0
        if explicit_risk:
            return 100
        if re.search(r"(大学|学院|高校|校园|高等院校)", text):
            return 90
        if re.search(r"(景区|风景名胜|公园|博物馆|美术馆|展览馆|纪念馆|故宫|长城|迪士尼)", text):
            return 80
        if re.search(r"(体育场|体育馆|广场|大型公共空间|奥林匹克|奥体)", text):
            return 75
        if re.search(r"(夜景|夜游|观景|商圈|步行街|美食街|夜市)", text):
            return 70
        return 20 if holiday_context and segment.kind in {"visit", "activity"} else 0

    def _meal_needs_risk_check(self, poi: Optional[POI], segment: ItinerarySegment, text: str) -> bool:
        if segment.kind != "meal":
            return True
        return bool(re.search(r"(夜市|美食街|步行街|商圈|热门|排队|预约|演出|大型活动)", text))

    def _risk_poi_has_provider_identity(self, poi: Optional[POI]) -> bool:
        if poi is None:
            return False
        if not poi.latitude or not poi.longitude:
            return False
        if poi.amap_id:
            return True
        return poi.source == AMAP_PLACE_SOURCE

    def _is_holiday_risk_context(self, planning_context: Optional[dict]) -> bool:
        resolved_dates = (
            planning_context.get("resolvedTripDates")
            if isinstance((planning_context or {}).get("resolvedTripDates"), dict)
            else {}
        )
        return bool(resolved_dates.get("holidayInferred") or resolved_dates.get("holidayName"))

    def _delete_routes(self, plan_id: str, route_pairs: Optional[set[tuple[str, str]]] = None) -> None:
        if route_pairs is None:
            self.db.execute(
                "DELETE FROM traffic_crowding_signals WHERE route_option_id IN (SELECT id FROM route_options WHERE plan_id = ?)",
                (plan_id,),
            )
            self.db.execute("DELETE FROM route_options WHERE plan_id = ?", (plan_id,))
            return
        if not route_pairs:
            return
        clauses = []
        params: list[str] = []
        for from_segment_id, to_segment_id in sorted(route_pairs):
            clauses.append("(from_segment_id = ? AND to_segment_id = ?)")
            params.extend([from_segment_id, to_segment_id])
        rows = self.db.execute(
            f"SELECT id FROM route_options WHERE plan_id = ? AND ({' OR '.join(clauses)})",
            [plan_id, *params],
        ).fetchall()
        route_ids = [row["id"] for row in rows]
        if not route_ids:
            return
        placeholders = ",".join("?" for _ in route_ids)
        self.db.execute(f"DELETE FROM traffic_crowding_signals WHERE route_option_id IN ({placeholders})", route_ids)
        self.db.execute(
            f"DELETE FROM route_options WHERE id IN ({placeholders}) AND plan_id = ?", [*route_ids, plan_id]
        )

    def _rollback_failed_tool_step(self, commit_between_tools: bool) -> None:
        if commit_between_tools and self.db.in_transaction:
            self.db.rollback()

    def _refresh_weather_signal(
        self,
        plan: ItineraryPlan,
        pois: list[POI],
        segments: list[ItinerarySegment],
        preference_summary: Optional[str],
        planning_context: Optional[dict],
    ) -> WeatherSignal:
        risk_context = self._risk_context(None, preference_summary, planning_context, None)
        risk_context = {
            **risk_context,
            "travelTimeWindows": self._planned_time_windows_from_segments(pois, segments),
        }
        weather = self.weather_service.build_weather_signal(
            plan.city,
            self._purpose_tags_from_context(preference_summary, planning_context),
            risk_context,
        )
        self.db.execute("DELETE FROM weather_signals WHERE plan_id = ?", (plan.id,))
        self._insert_weather(plan.id, weather)
        self.db.execute(
            "UPDATE itinerary_segments SET weather_signal_id = ? WHERE plan_id = ?",
            (weather.id, plan.id),
        )
        self.db.execute(
            "UPDATE itinerary_days SET weather_summary = ?, risk_summary = ? WHERE plan_id = ?",
            (weather.daily_summary, weather.daily_summary, plan.id),
        )
        return weather

    def refresh_ticket_results_for_segments(self, plan_id: str, segment_ids: list[str]) -> list[TicketLookupResult]:
        if not segment_ids:
            return []
        requested = set(segment_ids)
        segments = [segment for segment in self._load_segments(plan_id) if segment.id in requested]
        pois = self._load_pois(plan_id)
        return self._refresh_ticket_results(plan_id, segments, pois)

    def _refresh_ticket_results(
        self,
        plan_id: str,
        segments: list[ItinerarySegment],
        pois: list[POI],
    ) -> list[TicketLookupResult]:
        ticket_results = self.ticket_service.build_for_segments(segments, pois)
        segment_ids = [segment.id for segment in segments]
        if segment_ids:
            placeholders = ",".join("?" for _ in segment_ids)
            self.db.execute(f"DELETE FROM ticket_lookup_results WHERE segment_id IN ({placeholders})", segment_ids)
        self.ticket_service.persist_results(ticket_results)
        return ticket_results

    def _ensure_pending_ticket_results(
        self,
        plan_id: str,
        segments: list[ItinerarySegment],
        pois: list[POI],
    ) -> list[TicketLookupResult]:
        existing_rows = self.db.execute(
            "SELECT segment_id FROM ticket_lookup_results WHERE segment_id IN (SELECT id FROM itinerary_segments WHERE plan_id = ?)",
            (plan_id,),
        ).fetchall()
        existing_segment_ids = {row["segment_id"] for row in existing_rows}
        missing_segments = [segment for segment in segments if segment.id not in existing_segment_ids]
        if not missing_segments:
            return self._load_tickets(plan_id)
        ticket_results = self.ticket_service.build_pending_for_segments(missing_segments, pois)
        self.ticket_service.persist_results(ticket_results)
        return self._load_tickets(plan_id)

    def _refresh_poi_risk_alerts(
        self,
        plan: ItineraryPlan,
        pois: list[POI],
        segments: list[ItinerarySegment],
        weather: WeatherSignal,
        traffic: list[TrafficCrowdingSignal],
        ticket_results: list[TicketLookupResult],
        routes: list[RouteOption],
        preference_summary: Optional[str],
        planning_context: Optional[dict],
    ) -> list[POIRiskAlert]:
        risk_context = self._risk_context(None, preference_summary, planning_context, None)
        risk_context = {
            **risk_context,
            "travelTimeWindows": self._planned_time_windows_from_segments(pois, segments),
        }
        for key in (
            "officialSourceFirst",
            "maxRiskSearchResults",
            "riskSearchBudget",
            "maxRiskSearchQueries",
            "maxRiskProviderAttempts",
            "maxRiskSearchSeconds",
            "maxRiskSynthesisCalls",
        ):
            if key in (planning_context or {}):
                risk_context[key] = planning_context[key]
        alerts = self.poi_risk_service.build_alerts(
            plan.id,
            plan.city,
            pois,
            segments,
            weather,
            traffic,
            ticket_results,
            route_options=routes,
            risk_context=risk_context,
        )
        self.db.execute("DELETE FROM poi_risk_alerts WHERE plan_id = ?", (plan.id,))
        for alert in alerts:
            self._insert_poi_risk_alert(alert)
        return alerts

    def _planned_time_windows_from_segments(
        self,
        pois: list[POI],
        segments: list[ItinerarySegment],
    ) -> list[dict[str, str]]:
        pois_by_id = {poi.id: poi for poi in pois}
        return [
            {
                "poiName": pois_by_id.get(segment.poi_id).name if pois_by_id.get(segment.poi_id) else "待定地点",
                "startTime": segment.start_time,
                "endTime": segment.end_time,
            }
            for segment in segments[:6]
        ]

    def _purpose_tags_from_context(
        self,
        preference_summary: Optional[str],
        planning_context: Optional[dict],
    ) -> list[str]:
        tags: list[str] = []
        if preference_summary:
            tags.append(str(preference_summary))
        context = planning_context or {}
        requirements = (
            context.get("understoodRequirements") if isinstance(context.get("understoodRequirements"), dict) else {}
        )
        fields = requirements.get("fields") if isinstance(requirements.get("fields"), dict) else {}
        for value in (
            context.get("tripPurpose"),
            context.get("currentPreferenceSummary"),
            fields.get("travelPurpose"),
            fields.get("transportPreference"),
        ):
            if value:
                tags.append(str(value))
        return tags

    def _refresh_plan_totals(self, plan_id: str) -> None:
        day_rows = self.db.execute("SELECT id FROM itinerary_days WHERE plan_id = ?", (plan_id,)).fetchall()
        total = 0.0
        for day in day_rows:
            row = self.db.execute(
                "SELECT COALESCE(SUM(estimated_cost), 0) AS total FROM itinerary_segments WHERE plan_id = ? AND day_id = ?",
                (plan_id, day["id"]),
            ).fetchone()
            route_row = self.db.execute(
                """
                SELECT COALESCE(SUM(route_options.cost_amount), 0) AS total
                FROM route_options
                JOIN itinerary_segments source_segment ON source_segment.id = route_options.from_segment_id
                WHERE route_options.plan_id = ? AND source_segment.day_id = ?
                  AND route_options.is_selected = 1 AND route_options.error_json IS NULL
                """,
                (plan_id, day["id"]),
            ).fetchone()
            day_total = float(row["total"]) + float(route_row["total"] or 0)
            total += day_total
            self.db.execute(
                "UPDATE itinerary_days SET total_estimated_cost = ? WHERE id = ? AND plan_id = ?",
                (day_total, day["id"], plan_id),
            )
        self.db.execute(
            "UPDATE itinerary_plans SET budget_estimate = ?, updated_at = ? WHERE id = ?",
            (total, datetime.now(timezone.utc).isoformat(), plan_id),
        )

    def _latest_extraction(self, inspiration_set_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT * FROM extraction_results
            WHERE inspiration_set_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (inspiration_set_id,),
        ).fetchone()

    def _build_pois(self, city: str, poi_candidates: list[dict]) -> list[POI]:
        pois = []
        for index, candidate in enumerate(poi_candidates[:4], start=1):
            name = candidate.get("name", f"{city}候选地点")
            resolved = self._resolve_amap_poi(city, name)
            if resolved is not None:
                pois.append(resolved)

        if not pois:
            self.route_warnings.append(
                "地图服务未确认任何 POI，当前只保留空行程草案，请搜索或选择真实地图地点后继续规划。"
            )
        elif len(pois) == 1:
            self.route_warnings.append("地图服务只确认 1 个 POI，路线候选需至少 2 个真实地点后生成。")
        return pois

    def _resolve_amap_poi(
        self,
        city: str,
        name: str,
        warnings: Optional[list[str]] = None,
        plan: Optional[ItineraryPlan] = None,
        original: Optional[POI] = None,
        pois_by_id: Optional[dict[str, POI]] = None,
        resolved_by_id: Optional[dict[str, POI]] = None,
    ) -> Optional[POI]:
        warning_target = warnings if warnings is not None else self.route_warnings
        intent = self._plan_poi_intent(city, name, plan=plan, original=original)
        if intent.specificity == "functional":
            warning_target.append(f"功能型地点「{name}」需要结合前后行程做附近搜索，不会直接写成最终 POI。")
            return None

        candidates, provider_state = self._collect_poi_candidates(
            intent,
            plan=plan,
            original=original,
            pois_by_id=pois_by_id or {},
            resolved_by_id=resolved_by_id or {},
            warnings=warning_target,
        )
        selected = self._select_ranked_candidate(
            intent,
            candidates,
            plan=plan,
            original=original,
            pois_by_id=pois_by_id or {},
            resolved_by_id=resolved_by_id or {},
        )
        if selected is not None:
            poi = self._poi_from_ranked_candidate(intent, selected)
            if provider_state == "rate_limited":
                poi.source_note = f"{poi.source_note}；groundingStatus：provider_rate_limited；provider_rate_limited: 部分候选检索触发高德限流，已保留已成功候选。"
            return poi
        if provider_state == "rate_limited":
            return self._provider_rate_limited_anchor(city, name)

        if self._is_area_like_poi_name(name):
            area_resolved = self._resolve_area_like_poi(city, name, candidates, warning_target)
            if area_resolved is not None:
                return area_resolved
            if intent.specificity in {"area", "composite"}:
                return self._area_unresolved_anchor(city, name)

        if not candidates:
            warning_target.append(f"AMap POI resolve returned no candidates for {name}.")
        else:
            warning_target.append(
                f"AMap POI resolve returned no confident candidate-first selection for {name}: {candidates[0].name}."
            )
        return None

    def _poi_from_amap_response(
        self,
        poi,
        city: str = "",
        display_name: Optional[str] = None,
        source_note: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> POI:
        return POI(
            id=f"poi_{uuid4().hex[:12]}",
            amap_id=poi.id,
            parent_poi_id=str(getattr(poi, "parent_poi_id", "") or "").strip().upper() or None,
            name=display_name or poi.name,
            city=poi.city or city,
            category=poi.category,
            latitude=poi.latitude,
            longitude=poi.longitude,
            photo_url=poi.photos[0].url if poi.photos else None,
            source=AMAP_PLACE_SOURCE,
            confidence=confidence if confidence is not None else poi.confidence,
            type=poi.type,
            district=poi.district,
            address=poi.address,
            source_note=source_note if source_note is not None else poi.source_note,
            source_url=None,
            photos=[photo.model_dump() for photo in poi.photos],
            provider_type_code=str(getattr(poi, "provider_type_code", "") or "") or None,
            tags=[str(item) for item in getattr(poi, "tags", []) or [] if str(item)],
            source_claims=[
                dict(item)
                for item in getattr(poi, "source_claims", []) or []
                if isinstance(item, dict)
            ],
        )

    def _plan_poi_intent(
        self,
        city: str,
        raw_need: str,
        plan: Optional[ItineraryPlan] = None,
        original: Optional[POI] = None,
    ) -> PoiIntent:
        old_specificity = self._poi_specificity_from_text(raw_need, original.source_note if original else "")
        specificity = {
            POI_SPECIFICITY_EXACT: "exact_entity",
            POI_SPECIFICITY_AREA: "area",
            POI_SPECIFICITY_FUNCTIONAL: "functional",
            POI_SPECIFICITY_COMPOSITE: "composite",
        }.get(old_specificity, "exact_entity")
        intent_type = self._candidate_intent_type(raw_need, old_specificity)
        queries = self._candidate_search_queries(city, raw_need, intent_type, specificity)
        preferred = self._candidate_preferred_types(intent_type)
        rejected = self._candidate_rejected_types(intent_type)
        return PoiIntent(
            raw_need=raw_need,
            city=city,
            day_number=self._day_number_for_poi(plan.id, original.id) if plan and original else None,
            time_window=self._time_window_for_poi(plan.id, original.id) if plan and original else "",
            intent_type=intent_type,
            specificity=specificity,
            search_queries=queries,
            preferred_types=preferred,
            rejected_types=rejected,
            selection_rules=[
                "filter_non_lodging_noise",
                "prefer_intent_matched_amap_types",
                "avoid_same_day_duplicate_poi",
                "penalize_missing_coordinate_or_wrong_city",
                "penalize_route_failure_or_large_detour",
            ],
            ask_user_only_if=[
                "top1_not_clearly_better_than_top2",
                "all_candidates_low_confidence",
                "provider_rate_limited_or_unavailable",
            ],
            candidate_hints=[],
            hint_policy="no_hint",
            semantic_context=str(raw_need or ""),
            target_count=1,
        )

    def _candidate_intent_type(self, raw_need: str, specificity: str) -> str:
        text = str(raw_need or "")
        if re.search(r"(高校|大学|学院|校园|校区|985|211)", text):
            return "campus_visit"
        if re.search(r"(夜景|夜游|夜间|灯光|观景)", text):
            return "night_view"
        if re.search(r"(博物馆|美术馆|展览|纪念馆)", text):
            return "museum"
        if re.search(r"(公园|园区|绿地)", text):
            return "park"
        if re.search(r"(午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶)", text):
            return "meal"
        if re.search(r"(休息|歇脚|坐一会)", text):
            return "rest"
        if re.search(r"(购物|商场|商圈|买东西)", text):
            return "shopping"
        if re.search(r"(风土人情|当地文化|本地生活|民俗|非遗|胡同文化|历史街区|社区市场)", text):
            return "local_culture"
        if re.search(r"(漫步|步行|逛|街区|胡同|周边|附近|一带|区域)", text) or specificity in {
            POI_SPECIFICITY_AREA,
            POI_SPECIFICITY_COMPOSITE,
        }:
            return "area_walk"
        return "landmark"

    def _candidate_search_queries(self, city: str, raw_need: str, intent_type: str, specificity: str) -> list[str]:
        cleaned = re.sub(r"[/／、,，;；]+", " ", str(raw_need or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        canonical = self._canonical_poi_query_name(cleaned)
        city_prefix = city if city and city not in canonical else ""
        queries: list[str] = []
        if intent_type == "night_view":
            base = re.sub(r"(夜景|夜游|夜间|灯光|观景点?|地标|商圈)", " ", canonical)
            base = re.sub(r"\s+", " ", base).strip()
            if specificity != "functional" and base and not re.fullmatch(r"(夜景|观景点|地标景点|区域漫步)", base):
                queries.extend([f"{city_prefix} {base} 夜景 地标".strip(), f"{city_prefix} {base} 夜景 观景点".strip()])
            else:
                queries.extend(["地标", "观景点", "夜景", "商圈"])
        elif intent_type == "campus_visit":
            if specificity == "exact_entity" and re.search(r"(大学|学院|校区)", canonical):
                queries.append(canonical)
            else:
                queries.extend(["大学", "学院", "高等院校"])
        elif intent_type == "museum":
            # Broad phrases such as “北京当地博物馆” are not stable AMap
            # entity queries. Start with the provider-searchable venue class,
            # then retain the user's phrase for any later bounded expansion.
            queries.append("博物馆")
        elif intent_type == "meal":
            queries.append(f"{city_prefix} {canonical} 餐厅".strip())
        elif intent_type == "shopping":
            queries.append(f"{city_prefix} {canonical} 购物中心 商场".strip())
        elif intent_type == "rest":
            queries.append(f"{city_prefix} {canonical} 咖啡 休息".strip())
        elif intent_type == "local_culture":
            queries.extend(
                [
                    f"{city_prefix} {canonical} 历史文化街区".strip(),
                    f"{city_prefix} {canonical} 民俗 非遗".strip(),
                ]
            )
        elif specificity in {"area", "composite"}:
            queries.extend([f"{city_prefix} {canonical} 地标".strip(), f"{city_prefix} {canonical} 景点".strip()])
        else:
            queries.append(canonical)
        if intent_type not in {"campus_visit", "night_view"}:
            queries.append(canonical)
        return self._dedupe_queries(queries)

    @staticmethod
    def _candidate_hint_search_keyword(intent_type: str, hint: str) -> str:
        keyword = re.sub(r"\s+", " ", str(hint or "")).strip()
        if intent_type == "night_view":
            entity_keyword = re.sub(
                r"\s*(?:夜景|夜游|观景(?:点|平台)?)$",
                "",
                keyword,
            ).strip()
            if entity_keyword:
                return entity_keyword
        return keyword

    def _candidate_preferred_types(self, intent_type: str) -> list[str]:
        values = {
            "night_view": ["地标", "桥", "塔", "广场", "商圈", "购物中心", "体育场馆", "观景点"],
            "campus_visit": ["大学", "学院", "高等院校", "校区"],
            "meal": ["餐饮服务"],
            "museum": ["博物馆", "展览馆"],
            "park": ["公园", "风景名胜"],
            "shopping": ["购物中心", "商场", "商圈"],
            "area_walk": ["街区", "步行街", "商圈", "公园", "广场"],
            "local_culture": ["历史街区", "胡同", "社区市场", "非遗", "民俗", "文化馆", "博物馆", "步行街"],
            "rest": ["咖啡", "休闲"],
            "landmark": ["风景名胜", "地标", "广场"],
        }
        return values.get(intent_type, values["landmark"])

    def _candidate_rejected_types(self, intent_type: str) -> list[str]:
        if intent_type == "rest":
            return ["公司", "停车场", "住宅", "小区"]
        return ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区", "写字楼"]

    def _dedupe_queries(self, queries: list[str]) -> list[str]:
        deduped: list[str] = []
        seen = set()
        for query in queries:
            normalized = self._normalize_poi_name(query)
            if normalized and normalized not in seen:
                deduped.append(query.strip())
                seen.add(normalized)
        return deduped

    def _collect_poi_candidates(
        self,
        intent: PoiIntent,
        *,
        plan: Optional[ItineraryPlan],
        original: Optional[POI],
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
        warnings: list[str],
        slot_context: Optional[FunctionalSlotContext] = None,
        max_provider_queries: Optional[int] = None,
    ) -> tuple[list, str]:
        candidates = []
        seen_ids = set()
        provider_state = "ok"
        nearby_center = self._intent_nearby_center(plan, original, pois_by_id, resolved_by_id)
        target_count = max(1, int(getattr(intent, "target_count", 1) or 1))
        hint_queries = self._dedupe_queries(
            [str(item).strip() for item in getattr(intent, "candidate_hints", []) or [] if str(item).strip()]
        )
        functional_context = (
            slot_context
            if slot_context is not None
            and (intent.intent_type in {"meal", "rest", "shopping"} or self._expand_density_nearby)
            else None
        )
        profile_evidence_target = (
            int(intent.search_profile.coveragePolicy.evidenceTargetCount) if intent.search_profile is not None else None
        )
        pool_budget = self._poi_pool_budget(
            intent.intent_type,
            target_count,
            evidence_target_count=(getattr(intent, "candidate_evidence_target_count", None) or profile_evidence_target),
        )
        stats = {
            "intentType": intent.intent_type,
            "slotId": functional_context.slot_id if functional_context else None,
            "nearbyCandidateCount": 0,
            "citywideCandidateCount": 0,
            "rawCandidateCount": 0,
            "eligibleCandidateCount": 0,
            "uniqueEligibleEntityCount": 0,
            "consumerAdmissionPendingCount": 0,
            "rejectedWeakEntityCount": 0,
            "rejectedDuplicateCount": 0,
            "rejectedReasonCounts": {},
            "cacheHitCount": 0,
            "providerDebug": [],
            "searchModes": [],
            "poolBudget": pool_budget,
            "amapCallBudget": self._amap_call_budget_snapshot(),
        }
        self._last_candidate_collection_stats = stats

        if intent.search_profile is not None:
            return self._collect_search_profile_candidates(
                intent,
                nearby_center=nearby_center,
                slot_context=slot_context,
                candidates=candidates,
                seen_ids=seen_ids,
                warnings=warnings,
                stats=stats,
                max_provider_queries=max_provider_queries,
            )

        if functional_context is not None and functional_context.has_route_anchor:
            provider_state = self._collect_functional_nearby_candidates(
                intent,
                functional_context,
                candidates,
                seen_ids,
                warnings,
                stats,
            )
            if provider_state == "rate_limited":
                return candidates, provider_state
            if stats.get("earlyStopReason") in {
                "unique_eligible_stop_count_reached",
                "raw_candidate_budget_reached",
            }:
                return candidates, provider_state
            assigned_meal_family_missing = (
                intent.intent_type == "meal"
                and bool(str(getattr(intent, "assigned_meal_family", "") or "").strip())
                and int(stats.get("usedAroundSearch") or 0) > 0
                and int(stats.get("uniqueEligibleEntityCount") or 0) == 0
            )
            if assigned_meal_family_missing:
                nearby_center = None
                stats.pop("earlyStopReason", None)
            if stats.get("earlyStopReason") in {"per_slot_around_max_reached", "around_search_max_reached"}:
                if candidates:
                    return candidates, provider_state
                nearby_center = None
            elif nearby_center is None and not assigned_meal_family_missing:
                nearby_center = self._functional_slot_search_center(functional_context)
        text_calls = 0
        text_search_max = int(pool_budget.get("textSearchMax") or 0)
        raw_candidate_max = int(pool_budget.get("rawCandidateMax") or 0)
        # A concrete night-view hint may be classified by AMap as a commercial
        # street, park, tower, or mixed-use landmark rather than ``scenic``.
        # Hint matching below remains exact and fail-closed, so removing the
        # provider category filter broadens retrieval without broadening
        # admission.
        hint_search_category = (
            "all" if intent.intent_type == "night_view" else self._intent_search_category(intent.intent_type)
        )
        for hint in hint_queries:
            if text_calls >= text_search_max:
                self._mark_pool_budget_stop(stats, "text_search_max_reached", used_text_search=text_calls)
                return candidates, provider_state
            if self._poi_grounding_rate_limited:
                return candidates, "rate_limited"
            try:
                text_calls += 1
                stats["usedTextSearch"] = text_calls
                hint_keyword = self._candidate_hint_search_keyword(
                    intent.intent_type,
                    hint,
                )
                if functional_context is not None and nearby_center is not None:
                    response = self.map_poi_service.search_nearby(
                        intent.city,
                        float(nearby_center.longitude),
                        float(nearby_center.latitude),
                        hint_keyword,
                        category=hint_search_category,
                        radius=functional_context.search_radius_meters,
                        limit=max(3, target_count) if intent.intent_type == "meal" else 3,
                        **({"bypass_cache": True} if self._force_candidate_refresh else {}),
                    )
                else:
                    response = self.map_poi_service.search(
                        intent.city,
                        hint_keyword,
                        hint_search_category,
                        limit=max(3, target_count) if intent.intent_type == "meal" else 3,
                        **({"bypass_cache": True} if self._force_candidate_refresh else {}),
                    )
            except HTTPException as error:
                self._append_provider_debug(stats, error)
                raw_detail = error.detail
                detail = str(raw_detail)
                provider_state = self._amap_provider_state(raw_detail)
                if provider_state == "rate_limited":
                    self._mark_poi_rate_limited(raw_detail)
                    warnings.append(
                        f"AMap POI candidate hint search failed for {intent.raw_need}: rate_limited: {detail}"
                    )
                    return candidates, provider_state
                if provider_state == "budget_exceeded":
                    warnings.append(f"AMap POI candidate hint search stopped for {intent.raw_need}: budget_exceeded")
                    return candidates, provider_state
                warnings.append(f"AMap POI candidate hint search failed for {intent.raw_need}: {detail}")
                continue
            except Exception as error:
                detail = str(error)
                provider_state = "rate_limited" if self._is_amap_rate_limit_detail(detail) else "provider_down"
                if provider_state == "rate_limited":
                    self._mark_poi_rate_limited(detail)
                    warnings.append(
                        f"AMap POI candidate hint search failed for {intent.raw_need}: rate_limited: {detail}"
                    )
                    return candidates, provider_state
                warnings.append(f"AMap POI candidate hint search failed for {intent.raw_need}: {detail}")
                continue
            if intent.intent_type == "meal":
                strong_matches = list(response.pois[: max(3, target_count)])
            else:
                strong_matches = [poi for poi in response.pois[:3] if self._candidate_matches_hint(hint, poi)]
            if not strong_matches:
                strong_matches = [
                    poi for poi in response.pois[:3] if self._candidate_matches_thematic_hint_result(intent, hint, poi)
                ]
            if not strong_matches and intent.intent_type == "night_view":
                strong_matches = [poi for poi in response.pois[:3] if self._candidate_is_broad_night_view_result(poi)]
            if getattr(response, "cache_hit", False):
                stats["cacheHitCount"] += 1
            stats["amapCallBudget"] = self._amap_call_budget_snapshot()
            for poi in strong_matches:
                search_mode = (
                    "near_route_context_hint"
                    if functional_context is not None and nearby_center is not None
                    else "citywide_fallback"
                )
                if self._append_unique_candidate(
                    candidates,
                    seen_ids,
                    poi,
                    search_mode=search_mode,
                    matched_hint=hint,
                ):
                    self._record_collection_candidate(intent, stats, poi)
                    if functional_context is not None and nearby_center is not None:
                        stats["nearbyCandidateCount"] += 1
                    else:
                        stats["citywideCandidateCount"] += 1
                    if self._collection_reached_unique_eligible_stop(stats):
                        self._mark_pool_budget_stop(
                            stats, "unique_eligible_stop_count_reached", used_text_search=text_calls
                        )
                        return candidates, provider_state
                    if raw_candidate_max and len(candidates) >= raw_candidate_max:
                        self._mark_pool_budget_stop(stats, "raw_candidate_budget_reached", used_text_search=text_calls)
                        return candidates, provider_state
        query_limit = 4 if intent.intent_type in {"campus_visit", "night_view"} else 2
        if hint_queries and self._collection_reached_unique_eligible_stop(stats) and functional_context is None:
            self._mark_pool_budget_stop(stats, "unique_eligible_stop_count_reached", used_text_search=text_calls)
            return candidates, provider_state
        for query in intent.search_queries[:query_limit]:
            if text_calls >= text_search_max:
                self._mark_pool_budget_stop(stats, "text_search_max_reached", used_text_search=text_calls)
                return candidates, provider_state
            if self._poi_grounding_rate_limited:
                return candidates, "rate_limited"
            try:
                text_calls += 1
                stats["usedTextSearch"] = text_calls
                if nearby_center is not None and intent.intent_type in {"meal", "rest", "shopping"}:
                    response = self.map_poi_service.search_nearby(
                        intent.city,
                        float(nearby_center.longitude),
                        float(nearby_center.latitude),
                        query,
                        category=self._intent_search_category(intent.intent_type),
                        radius=1500,
                        limit=8,
                        **({"bypass_cache": True} if self._force_candidate_refresh else {}),
                    )
                else:
                    response = self.map_poi_service.search(
                        intent.city,
                        query,
                        self._intent_search_category(intent.intent_type),
                        limit=8,
                        **({"bypass_cache": True} if self._force_candidate_refresh else {}),
                    )
            except HTTPException as error:
                self._append_provider_debug(stats, error)
                raw_detail = error.detail
                detail = str(raw_detail)
                provider_state = self._amap_provider_state(raw_detail)
                if provider_state == "rate_limited":
                    self._mark_poi_rate_limited(raw_detail)
                    warnings.append(f"AMap POI candidate search failed for {intent.raw_need}: rate_limited: {detail}")
                    return candidates, provider_state
                if provider_state == "budget_exceeded":
                    warnings.append(f"AMap POI candidate search stopped for {intent.raw_need}: budget_exceeded")
                    return candidates, provider_state
                warnings.append(f"AMap POI candidate search failed for {intent.raw_need}: {detail}")
                continue
            except Exception as error:
                detail = str(error)
                provider_state = "rate_limited" if self._is_amap_rate_limit_detail(detail) else "provider_down"
                if provider_state == "rate_limited":
                    self._mark_poi_rate_limited(detail)
                    warnings.append(f"AMap POI candidate search failed for {intent.raw_need}: rate_limited: {detail}")
                    return candidates, provider_state
                warnings.append(f"AMap POI candidate search failed for {intent.raw_need}: {detail}")
                continue
            if getattr(response, "cache_hit", False):
                stats["cacheHitCount"] += 1
            stats["amapCallBudget"] = self._amap_call_budget_snapshot()
            for poi in response.pois:
                search_mode = (
                    "near_existing_context"
                    if functional_context is not None and nearby_center is not None
                    else "citywide_fallback"
                )
                if self._append_unique_candidate(
                    candidates,
                    seen_ids,
                    poi,
                    search_mode=search_mode,
                    matched_hint=query,
                ):
                    self._record_collection_candidate(intent, stats, poi)
                    if functional_context is not None and nearby_center is not None:
                        stats["nearbyCandidateCount"] += 1
                    else:
                        stats["citywideCandidateCount"] += 1
                    if self._collection_reached_unique_eligible_stop(stats):
                        self._mark_pool_budget_stop(
                            stats, "unique_eligible_stop_count_reached", used_text_search=text_calls
                        )
                        return candidates, provider_state
                    if raw_candidate_max and len(candidates) >= raw_candidate_max:
                        self._mark_pool_budget_stop(stats, "raw_candidate_budget_reached", used_text_search=text_calls)
                        return candidates, provider_state
        return candidates, provider_state

    @staticmethod
    def _route_scope_fingerprint(value: dict[str, Any]) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _profile_query_scope_fingerprint(cls, profile: Any, plan: Any) -> str:
        """Bind one Provider request to its exact profile occurrence and plan."""

        source_plan = next(
            (
                item
                for item in profile.queryPlans
                if str(item.planId or "") == str(plan.sourcePlanId or "")
            ),
            None,
        )
        if source_plan is None:
            raise ValueError("search_profile_source_plan_missing")
        occurrence = {
            "profileId": str(profile.profileId or ""),
            "profileFingerprint": str(profile.profileFingerprint or ""),
            "executionFingerprint": str(profile.executionFingerprint or ""),
            "briefId": str(profile.briefId or ""),
            "poolId": str(profile.poolId or ""),
            "planningSlotId": str(profile.planningSlotId or ""),
            "dayNumber": int(profile.dayNumber or 0),
        }
        occurrence_fingerprint = cls._route_scope_fingerprint(occurrence).upper()
        source_plan_fingerprint = cls._route_scope_fingerprint(
            source_plan.model_dump(by_alias=True)
        ).upper()
        provider_plan_fingerprint = cls._route_scope_fingerprint(
            plan.model_dump(by_alias=True)
        ).upper()
        return cls._route_scope_fingerprint(
            {
                "occurrenceFingerprint": occurrence_fingerprint,
                "sourcePlanFingerprint": source_plan_fingerprint,
                "providerPlanFingerprint": provider_plan_fingerprint,
                "requestShape": {
                    "endpoint": str(plan.endpoint or ""),
                    "city": str(plan.city or ""),
                    "keyword": str(plan.keyword or ""),
                    "category": str(plan.category or ""),
                    "limit": int(plan.resultLimit or 0),
                    "radius": (
                        int(plan.radiusMeters or 0)
                        if str(plan.endpoint or "") == "place/around"
                        else 0
                    ),
                },
            }
        ).upper()

    @staticmethod
    def _map_search_supports_query_scope(method: Any) -> bool:
        """Check the declared call shape before sending the optional scope."""

        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            return False
        return "query_scope_fingerprint" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    def _verified_profile_route_scope(
        self,
        profile: Any,
        slot_context: Optional[FunctionalSlotContext],
    ) -> Optional[dict[str, Any]]:
        """Bind one density-nearby query to trusted route and policy state.

        A Search Profile is compiled before adjacent anchors are selected.  The
        density-nearby continuation therefore supplies those anchors at
        execution time.  Only canonical AMap anchors and a self-verifying
        request route contract may authorize this scoped query.
        """

        if slot_context is None or not slot_context.has_route_anchor:
            return None

        def anchor_payload(poi: Optional[POI]) -> Optional[dict[str, Any]]:
            if poi is None:
                return None
            amap_id = str(poi.amap_id or "").strip().upper()
            if (
                poi.source != AMAP_PLACE_SOURCE
                or re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id) is None
                or not self._is_verifier_grade_amap_poi(poi)
            ):
                return None
            return {
                "amapId": amap_id,
                "longitude": round(float(poi.longitude), 6),
                "latitude": round(float(poi.latitude), 6),
            }

        previous = anchor_payload(slot_context.previous_anchor)
        following = anchor_payload(slot_context.next_anchor)
        if slot_context.previous_anchor is not None and previous is None:
            return None
        if slot_context.next_anchor is not None and following is None:
            return None
        if previous is None and following is None:
            return None

        route_context = dict(getattr(profile, "routeContext", {}) or {})
        route_contract = RouteInsertionScorer.normalized_route_decision_contract(
            route_context.get("routeDecisionContract")
        )
        if route_contract is None:
            return None
        transport_mode = normalize_route_mode(str(slot_context.transport_mode or "").strip())
        contract_transport_mode = normalize_route_mode(
            str((route_contract.get("provenance") or {}).get("transportMode") or "").strip()
        )
        if not transport_mode or not contract_transport_mode or transport_mode != contract_transport_mode:
            return None

        anchor_policy = (
            "between_adjacent"
            if previous is not None and following is not None
            else "previous_only"
            if previous is not None
            else "next_only"
        )
        corridor_material = {
            "previous": previous,
            "next": following,
            "transportMode": transport_mode,
        }
        route_corridor_hash = self._route_scope_fingerprint(corridor_material)
        evidence_requirement_fingerprint = self._route_scope_fingerprint(
            {
                "evidencePolicy": dict(getattr(profile, "evidencePolicy", {}) or {}),
                "groundingPolicy": dict(getattr(profile, "groundingPolicy", {}) or {}),
                "routeContractFingerprint": route_contract["fingerprint"],
            }
        )
        contract_version = str(
            route_context.get("clarificationContractVersion")
            or route_context.get("contractVersion")
            or route_contract["fingerprint"]
        )
        scope = {
            "city": str(getattr(profile, "city", "") or ""),
            "intentType": str(getattr(profile, "intentType", "") or ""),
            "experienceFamily": str(getattr(profile, "experienceFamily", "") or ""),
            "dayNumber": int(slot_context.day_number),
            "occurrenceId": str(getattr(profile, "planningSlotId", "") or slot_context.slot_id),
            "routeCorridorHash": route_corridor_hash,
            "transportMode": transport_mode,
            "evidenceRequirementFingerprint": evidence_requirement_fingerprint,
            "contractVersion": contract_version,
            "anchorPolicy": anchor_policy,
        }
        return {
            **scope,
            "queryFingerprint": self._route_scope_fingerprint(scope),
        }

    def _route_scoped_profile_plan(
        self,
        adapted_plans: list[AmapPoiSearchPlan],
        route_scope: dict[str, Any],
        slot_context: FunctionalSlotContext,
        *,
        profile: Any,
    ) -> Optional[AmapPoiSearchPlan]:
        if not adapted_plans:
            return None
        source_plan = next(
            (
                plan
                for plan in adapted_plans
                if plan.endpoint == "place/text" and plan.mode not in {"exact_entity", "web_seed_then_amap"}
            ),
            None,
        )
        if source_plan is None:
            return None
        recall_keyword = self._route_scoped_recall_keyword(
            profile,
            fallback=source_plan.keyword,
        )
        query_fingerprint = self._route_scope_fingerprint(
            {
                "routeQueryFingerprint": str(route_scope["queryFingerprint"]),
                "keyword": recall_keyword,
                "category": source_plan.category,
            }
        )
        return source_plan.model_copy(
            update={
                "planId": "amap_route_plan_" + query_fingerprint[:24],
                "priority": 1000,
                "endpoint": "place/around",
                "mode": "amap_around",
                "keyword": recall_keyword,
                "anchorPolicy": str(route_scope["anchorPolicy"]),
                "radiusMeters": max(
                    50,
                    min(int(slot_context.search_radius_meters), 5000),
                ),
            }
        )

    def _route_scoped_profile_plans(
        self,
        adapted_plans: list[AmapPoiSearchPlan],
        route_scope: dict[str, Any],
        slot_context: FunctionalSlotContext,
        *,
        profile: Any,
    ) -> list[AmapPoiSearchPlan]:
        """Build a bounded provider-vocabulary ladder for one route scope.

        A candidate that passes the cheap collection precheck can still fail
        final Consumer Admission.  Route-bounded recall therefore starts with
        one provider catalogue noun for every experience family.  Meals and
        markets keep a second noun because both provider catalogues are narrow:
        one query can contain only closed or mismatched results even when a
        valid route-local entity exists under the sibling catalogue noun.  The
        existing per-pool call limit remains authoritative.
        """

        first = self._route_scoped_profile_plan(
            adapted_plans,
            route_scope,
            slot_context,
            profile=profile,
        )
        if first is None:
            return []
        family = str(getattr(profile, "experienceFamily", "") or "").strip()
        city = str(getattr(profile, "city", "") or "").strip()
        city_root = re.sub(r"(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|自治州|地区|盟|市)$", "", city)
        local_meal_keyword = f"{city_root}菜" if city_root else "地方菜"
        family_keywords: dict[str, tuple[str, ...]] = {
            # Generic terms such as ``餐厅`` retrieve many real but semantically
            # unqualified POIs.  Start with the current city's cuisine around
            # the verified same-day route anchors, then use a city-agnostic
            # local-cuisine fallback.  Retrieval remains recall-only: final
            # Consumer Admission and provider route evidence still decide.
            "meal": (local_meal_keyword, "地方菜"),
            "market": ("菜市场", "农贸市场"),
            "local_life": ("生活街区", "传统市场"),
            "art": ("艺术园区", "文化创意园"),
            "night_view": ("夜景观景台",),
        }
        max_keywords = 2 if family in {"meal", "market"} else 1
        keywords: list[str] = []
        # Prefer the stable provider noun.  Abstract LLM wording remains a
        # later city-wide fallback in the compiled profile and must not spend
        # the scarce route-scoped request first.
        for keyword in (*family_keywords.get(family, ()), first.keyword):
            cleaned = str(keyword or "").strip()
            if cleaned and cleaned not in keywords:
                keywords.append(cleaned)
            if len(keywords) >= max_keywords:
                break
        result: list[AmapPoiSearchPlan] = []
        for priority_offset, keyword in enumerate(keywords):
            query_fingerprint = self._route_scope_fingerprint(
                {
                    "routeQueryFingerprint": str(route_scope["queryFingerprint"]),
                    "keyword": keyword,
                    "category": first.category,
                }
            )
            result.append(
                first.model_copy(
                    update={
                        "planId": "amap_route_plan_" + query_fingerprint[:24],
                        "priority": 1000 - priority_offset,
                        "keyword": keyword,
                    }
                )
            )
        return result

    @staticmethod
    def _route_scoped_recall_keyword(profile: Any, *, fallback: str) -> str:
        """Use provider-recognizable nouns for route-bounded recall.

        Creative briefs describe experiences, while AMap around-search is more
        reliable with concrete catalog nouns.  This only widens retrieval;
        Consumer Admission and the provider route matrix remain authoritative.
        """

        variants = [
            str(item or "").strip()
            for item in list(getattr(profile, "keywordVariants", []) or [])
            if str(item or "").strip()
        ]
        family = str(getattr(profile, "experienceFamily", "") or "").strip()
        preferred_tokens: dict[str, tuple[str, ...]] = {
            "meal": ("餐厅", "餐饮", "美食"),
            "market": ("菜市场", "农贸市场", "传统市场", "市集"),
            "local_life": ("菜市场", "农贸市场", "生活街区", "胡同"),
            "art": ("艺术园区", "文化创意园", "艺术街区", "画廊"),
            "night_view": ("观景台", "夜景", "公园", "广场", "滨水"),
        }
        provider_catalog_defaults: dict[str, str] = {
            # AMap around-search is a catalogue lookup.  When an Agent brief
            # only says an abstract goal such as "当地特色美食", use the broad
            # provider noun for recall and leave local-food semantics to
            # Consumer Admission.  This is city- and POI-agnostic.
            "meal": "餐厅",
            "market": "菜市场",
            "local_life": "生活街区",
            "art": "艺术园区",
            "night_view": "夜景观景台",
        }
        for token in preferred_tokens.get(family, ()):
            for variant in variants:
                if token in variant:
                    return variant
        return provider_catalog_defaults.get(family, str(fallback or "").strip())

    def _prepare_night_view_query_progress(
        self,
        *,
        intent: PoiIntent,
        profile: Any,
        adapted_plans: list[AmapPoiSearchPlan],
        route_scope: Optional[dict[str, Any]],
        slot_context: Optional[FunctionalSlotContext],
        stats: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Claim one exact night-view query from the persisted Portfolio frontier.

        Initial generation deliberately has no configuration, so it continues to
        execute its frozen cursor=0 query sequence.  Only a continuation with an
        opaque, already-authorized attempt may ask the existing root ledger for a
        next query.  The raw query never enters that ledger; it is reduced to a
        canonical fingerprint before persistence.
        """

        configured = self._night_view_query_progress
        if intent.intent_type != "night_view" or configured is None:
            return None
        scope_identity = configured.get("scopeIdentity")
        attempt_identity = str(configured.get("attemptIdentity") or "")
        if not isinstance(scope_identity, dict) or not attempt_identity:
            raise ValueError("night_view_progress_configuration_invalid")
        scope = {
            **scope_identity,
            "briefId": str(getattr(profile, "briefId", "") or ""),
            "poolId": str(getattr(profile, "poolId", "") or ""),
            "planningSlotId": str(getattr(profile, "planningSlotId", "") or ""),
            "dayNumber": int(getattr(profile, "dayNumber", 0) or getattr(intent, "day_number", 0) or 0),
            "timeWindow": str(getattr(intent, "time_window", "") or ""),
        }
        raw_route_contract = (
            getattr(profile, "routeContext", {}).get("routeDecisionContract")
            if isinstance(getattr(profile, "routeContext", None), dict)
            else None
        )
        normalized_route_contract = RouteInsertionScorer.normalized_route_decision_contract(raw_route_contract)
        if normalized_route_contract is None:
            raise ValueError("night_view_progress_route_contract_invalid")
        semantic_fingerprint = CreativeExplorationFrontierService.night_view_semantic_fingerprint(
            goal={
                "goalId": getattr(getattr(profile, "sourceEvidence", None), "goalId", None),
                "softGoalId": getattr(getattr(profile, "sourceEvidence", None), "softGoalId", None),
                "rawNeed": intent.raw_need,
                "intentType": intent.intent_type,
                "experienceGoal": str(getattr(profile, "experienceGoal", "") or ""),
            },
            city=str(getattr(profile, "city", "") or intent.city),
            adcode=(
                str(getattr(profile, "routeContext", {}).get("adcode") or "")
                if isinstance(getattr(profile, "routeContext", None), dict)
                else ""
            ),
            experienceSpec={
                "experienceFamily": str(getattr(profile, "experienceFamily", "") or ""),
                "semanticFacets": list(getattr(profile, "semanticFacets", []) or []),
                "desiredSignals": list(getattr(profile, "desiredSignals", []) or []),
                "avoidSignals": list(getattr(profile, "avoidSignals", []) or []),
                "evidencePolicy": dict(getattr(profile, "evidencePolicy", {}) or {}),
                "groundingPolicy": dict(getattr(profile, "groundingPolicy", {}) or {}),
            },
            briefPoolSlotDayTime={
                key: scope[key]
                for key in ("briefId", "poolId", "planningSlotId", "dayNumber", "timeWindow")
            },
            routeContract={"fingerprint": normalized_route_contract["fingerprint"]},
            candidateHints=list(getattr(intent, "candidate_hints", []) or []),
        )
        descriptors: list[dict[str, Any]] = []
        descriptor_by_plan_id: dict[str, dict[str, Any]] = {}
        for plan in adapted_plans:
            if plan.mode == "web_seed_then_amap":
                continue
            center = (
                self._functional_slot_search_center(slot_context)
                if plan.endpoint == "place/around" and slot_context is not None
                else None
            )
            if plan.endpoint == "place/around" and center is None:
                continue
            descriptor = {
                "providerPlanId": str(plan.planId or ""),
                "sourcePlanId": str(plan.sourcePlanId or ""),
                "mode": str(plan.mode or ""),
                "endpoint": str(plan.endpoint or ""),
                "city": str(plan.city or ""),
                "keyword": str(plan.keyword or ""),
                "category": str(plan.category or ""),
                "radiusMeters": int(plan.radiusMeters or 0),
                "resultLimit": int(plan.resultLimit or 0),
                "routeScopeFingerprint": (
                    str(route_scope.get("queryFingerprint") or "")
                    if route_scope is not None and str(plan.planId or "")
                    else ""
                ),
            }
            if center is not None:
                descriptor["center"] = {
                    "longitude": float(center.longitude),
                    "latitude": float(center.latitude),
                }
            plan_id = descriptor["providerPlanId"]
            if not plan_id:
                raise ValueError("night_view_progress_query_plan_identity_invalid")
            if plan_id in descriptor_by_plan_id:
                # A route-scoped insertion may project the same compiled plan
                # twice.  It is still one exact outbound request; retaining the
                # first position avoids creating a second cursor entry.
                continue
            descriptor_by_plan_id[plan_id] = descriptor
            descriptors.append(descriptor)
        # Route-scoped insertion can surface the same compiled plan twice.  It
        # is one outbound query semantics, so retain its first actual position.
        deduplicated: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for descriptor in descriptors:
            fingerprint = CreativeExplorationFrontierService.night_view_query_fingerprint(descriptor)
            if fingerprint not in seen_fingerprints:
                seen_fingerprints.add(fingerprint)
                deduplicated.append(descriptor)
        if not deduplicated:
            stats["nightViewQueryProgress"] = {
                "status": "NO_PROGRESS",
                "reason": "night_view_query_set_empty",
                "queryCursor": 0,
            }
            return {
                "claim": {"status": "NO_PROGRESS", "queryCursor": 0},
                "scope": scope,
                "semanticFingerprint": semantic_fingerprint,
                "queries": [],
                "attemptIdentity": attempt_identity,
            }
        claim = configured["claimQuery"](
            scope=scope,
            semantic_fingerprint=semantic_fingerprint,
            queries=deduplicated,
            attempt_identity=attempt_identity,
        )
        if not isinstance(claim, dict):
            raise ValueError("night_view_progress_claim_invalid")
        stats["nightViewQueryProgress"] = {
            "status": str(claim.get("status") or ""),
            "queryCursor": int(claim.get("queryCursor") or 0),
            "semanticFingerprint": semantic_fingerprint,
            "executedQueryFingerprints": list(claim.get("executedQueryFingerprints") or []),
        }
        return {
            "claim": claim,
            "scope": scope,
            "semanticFingerprint": semantic_fingerprint,
            "queries": deduplicated,
            "attemptIdentity": attempt_identity,
        }

    def _complete_night_view_query_progress(
        self,
        progress_context: dict[str, Any],
        *,
        provider_completed: bool,
        raw_candidate_count: int = 0,
        provider_name: str = "",
    ) -> bool:
        """Store only a safe, query-bound receipt after the provider boundary."""

        configured = self._night_view_query_progress
        claim = progress_context.get("claim") if isinstance(progress_context, dict) else None
        if configured is None or not isinstance(claim, dict) or claim.get("status") != "CLAIMED":
            return False
        receipt = None
        if provider_completed:
            receipt = self._route_scope_fingerprint(
                {
                    "queryFingerprint": str(claim.get("queryFingerprint") or ""),
                    "provider": str(provider_name or ""),
                    "candidateCount": max(0, int(raw_candidate_count or 0)),
                }
            )
        try:
            completion = configured["completeQuery"](
                scope=progress_context["scope"],
                semantic_fingerprint=progress_context["semanticFingerprint"],
                queries=progress_context["queries"],
                attempt_identity=progress_context["attemptIdentity"],
                provider_completed=provider_completed,
                provider_receipt_fingerprint=receipt,
            )
        except (KeyError, TypeError, ValueError):
            return False
        return isinstance(completion, dict) and str(completion.get("status") or "") in {
            "COMPLETED",
            "FAILED_NO_PROGRESS",
        }

    def _collect_search_profile_candidates(
        self,
        intent: PoiIntent,
        *,
        nearby_center: Optional[POI],
        slot_context: Optional[FunctionalSlotContext],
        candidates: list[Any],
        seen_ids: set[str],
        warnings: list[str],
        stats: dict[str, Any],
        max_provider_queries: Optional[int] = None,
    ) -> tuple[list[Any], str]:
        """Execute only bounded AMap plans compiled from one semantic profile."""

        profile = intent.search_profile
        if profile is None:
            return candidates, "ok"
        rejection_reason = search_profile_provider_rejection_reason(profile)
        if rejection_reason:
            stats.update(
                {
                    "searchProfileId": profile.profileId,
                    "searchProfileFingerprint": profile.profileFingerprint,
                    "executionFingerprint": profile.executionFingerprint,
                    "experienceFamily": profile.experienceFamily,
                    "activityMode": profile.activityMode,
                    "queryPlanCount": len(profile.queryPlans),
                    "adapterPlanCount": 0,
                    "executedQueryPlanCount": 0,
                    "skippedQueryPlanCount": len(profile.queryPlans),
                    "duplicateExcludedCount": 0,
                    "semanticAcceptedCount": 0,
                    "semanticRejectedCount": 0,
                    "queryPlanEvidence": [],
                    "providerPreflightRejected": True,
                    "providerPreflightReasonCode": rejection_reason,
                }
            )
            return candidates, "semantic_rejected"
        adapted_plans = AmapPoiSearchPlanAdapter().adapt(profile)
        # A server-built slot context already proves that the query belongs to
        # one day and is adjacent to canonical AMap anchors.  Use that evidence
        # on the first attempt as well as explicit density retries; otherwise
        # meals, market walks and night experiences fall back to unrelated
        # city-wide POIs and only fail much later at the route matrix.
        route_scope = self._verified_profile_route_scope(profile, slot_context)
        route_scoped_plans = (
            self._route_scoped_profile_plans(
                adapted_plans,
                route_scope,
                slot_context,
                profile=profile,
            )
            if route_scope is not None and slot_context is not None
            else []
        )
        route_scoped_plan_ids = {plan.planId for plan in route_scoped_plans}
        if route_scoped_plans:
            adapted_plans = [*route_scoped_plans, *adapted_plans]
        night_progress_context = self._prepare_night_view_query_progress(
            intent=intent,
            profile=profile,
            adapted_plans=adapted_plans,
            route_scope=route_scope,
            slot_context=slot_context,
            stats=stats,
        )
        if night_progress_context is not None:
            claim = night_progress_context["claim"]
            if claim["status"] != "CLAIMED":
                # REPLAY and an exhausted/contended frontier are all deliberately
                # Provider-free.  The caller may preserve its pending state, but
                # cannot manufacture a second night query from a retry.
                return candidates, "no_progress"
            selected_plan_id = str(claim["query"].get("providerPlanId") or "").casefold()
            selected_plans = [
                plan
                for plan in adapted_plans
                if str(plan.planId or "").casefold() == selected_plan_id and plan.mode != "web_seed_then_amap"
            ]
            if not selected_plans:
                raise ValueError("night_view_progress_selected_plan_missing")
            # Route-scoped assembly can project the same compiled plan twice;
            # after the exact frontier claim it remains one Provider request.
            adapted_plans = [selected_plans[0]]
        pool_budget = stats.get("poolBudget") or {}
        call_limit = min(
            int(profile.budgetPolicy.maxAmapCalls),
            max(
                1,
                int(pool_budget.get("textSearchMax") or 0) + int(pool_budget.get("aroundSearchMax") or 0),
            ),
        )
        if max_provider_queries is not None:
            call_limit = min(call_limit, max(1, int(max_provider_queries)))
        excluded_ids = {str(item).strip().upper() for item in profile.excludedPhysicalPoiIds if str(item).strip()}
        stats.update(
            {
                "searchProfileId": profile.profileId,
                "searchProfileFingerprint": profile.profileFingerprint,
                "executionFingerprint": profile.executionFingerprint,
                "experienceFamily": profile.experienceFamily,
                "activityMode": profile.activityMode,
                "queryPlanCount": len(profile.queryPlans),
                "adapterPlanCount": len(adapted_plans),
                "executedQueryPlanCount": 0,
                "skippedQueryPlanCount": 0,
                "duplicateExcludedCount": 0,
                "semanticAcceptedCount": 0,
                "semanticRejectedCount": 0,
                "queryPlanEvidence": [],
                "routeContextApplied": bool(route_scoped_plans),
                "routeContextPlanId": (route_scoped_plans[0].planId if route_scoped_plans else None),
                "routeContextPlanIds": [plan.planId for plan in route_scoped_plans],
                "routeContextQueryScope": dict(route_scope or {}),
            }
        )
        provider_state = "ok"
        executed = 0
        for plan in adapted_plans:
            if plan.mode == "web_seed_then_amap":
                stats["skippedQueryPlanCount"] += 1
                continue
            if executed >= call_limit:
                self._mark_pool_budget_stop(
                    stats,
                    "profile_amap_call_limit_reached",
                    used_text_search=executed,
                )
                break
            is_route_scoped_plan = plan.planId in route_scoped_plan_ids and slot_context is not None
            center = self._functional_slot_search_center(slot_context) if is_route_scoped_plan else nearby_center
            if plan.endpoint == "place/around" and center is None and slot_context is not None:
                center = self._functional_slot_search_center(slot_context)
            if plan.endpoint == "place/around" and center is None:
                stats["skippedQueryPlanCount"] += 1
                continue
            if self._poi_grounding_rate_limited:
                return candidates, "rate_limited"
            try:
                executed += 1
                stats["usedTextSearch"] = executed
                request_options: dict[str, Any] = {}
                if self._force_candidate_refresh:
                    request_options["bypass_cache"] = True
                request_method = (
                    self.map_poi_service.search_nearby
                    if plan.endpoint == "place/around" and center is not None
                    else self.map_poi_service.search
                )
                if self._map_search_supports_query_scope(request_method):
                    if is_route_scoped_plan and route_scope is not None:
                        request_options["query_scope_fingerprint"] = str(
                            route_scope["queryFingerprint"]
                        )
                    else:
                        request_options["query_scope_fingerprint"] = (
                            self._profile_query_scope_fingerprint(profile, plan)
                        )
                if plan.endpoint == "place/around" and center is not None:
                    response = self.map_poi_service.search_nearby(
                        plan.city,
                        float(center.longitude),
                        float(center.latitude),
                        plan.keyword,
                        category=plan.category,
                        radius=plan.radiusMeters,
                        limit=plan.resultLimit,
                        **request_options,
                    )
                else:
                    response = self.map_poi_service.search(
                        plan.city,
                        plan.keyword,
                        category=plan.category,
                        limit=plan.resultLimit,
                        **request_options,
                    )
            except HTTPException as error:
                if night_progress_context is not None and not self._complete_night_view_query_progress(
                    night_progress_context,
                    provider_completed=False,
                ):
                    return candidates, "no_progress"
                self._append_provider_debug(stats, error)
                raw_detail = error.detail
                provider_state = self._amap_provider_state(raw_detail)
                if provider_state == "rate_limited":
                    self._mark_poi_rate_limited(raw_detail)
                    warnings.append(f"AMap profile search failed for {intent.raw_need}: rate_limited")
                    return candidates, provider_state
                if provider_state == "budget_exceeded":
                    warnings.append(f"AMap profile search stopped for {intent.raw_need}: budget_exceeded")
                    return candidates, provider_state
                warnings.append(f"AMap profile search failed for {intent.raw_need}: {error.detail}")
                continue
            except Exception as error:
                if night_progress_context is not None and not self._complete_night_view_query_progress(
                    night_progress_context,
                    provider_completed=False,
                ):
                    return candidates, "no_progress"
                detail = str(error)
                provider_state = "rate_limited" if self._is_amap_rate_limit_detail(detail) else "provider_down"
                if provider_state == "rate_limited":
                    self._mark_poi_rate_limited(detail)
                    return candidates, provider_state
                warnings.append(f"AMap profile search failed for {intent.raw_need}: {detail}")
                continue

            raw_pois = list(getattr(response, "pois", []) or [])
            if night_progress_context is not None:
                provider_completed = not bool(getattr(response, "cache_hit", False))
                if not self._complete_night_view_query_progress(
                    night_progress_context,
                    provider_completed=provider_completed,
                    raw_candidate_count=len(raw_pois),
                    provider_name=str(getattr(response, "provider_name", "") or ""),
                ):
                    return candidates, "no_progress"
            accepted_count = 0
            semantic_rejected_count = 0
            duplicate_excluded_count = 0
            if getattr(response, "cache_hit", False):
                stats["cacheHitCount"] += 1
            stats["amapCallBudget"] = self._amap_call_budget_snapshot()
            for poi in raw_pois:
                physical_id = str(getattr(poi, "amap_id", "") or getattr(poi, "id", "") or "").strip().upper()
                if physical_id and physical_id in excluded_ids:
                    duplicate_excluded_count += 1
                    stats["duplicateExcludedCount"] += 1
                    continue
                if not self._append_unique_candidate(
                    candidates,
                    seen_ids,
                    poi,
                    search_mode=plan.mode,
                    matched_hint=plan.keyword,
                ):
                    continue
                self._set_candidate_profile_metadata(
                    poi,
                    profile=profile,
                    query_plan_id=plan.sourcePlanId,
                    search_mode=plan.mode,
                    fallback_level=plan.fallbackLevel,
                    matched_keyword=plan.keyword,
                )
                if is_route_scoped_plan and route_scope is not None:
                    self._set_candidate_route_query_scope_metadata(
                        poi,
                        route_scope=route_scope,
                    )
                if plan.endpoint == "place/around":
                    stats["nearbyCandidateCount"] += 1
                else:
                    stats["citywideCandidateCount"] += 1
                reasons = self._collection_candidate_rejection_reasons(intent, poi)
                if is_route_scoped_plan and not reasons:
                    # This candidate passed only provider and source-profile
                    # gates.  The target pool/slot consumer still has to apply
                    # ExperienceSpec and evidence policy before it may count as
                    # eligible coverage.
                    stats["rawCandidateCount"] = int(stats.get("rawCandidateCount") or 0) + 1
                    stats["consumerAdmissionPendingCount"] = int(stats.get("consumerAdmissionPendingCount") or 0) + 1
                else:
                    self._record_collection_candidate(intent, stats, poi)
                if reasons:
                    semantic_rejected_count += 1
                else:
                    accepted_count += 1
            stats["executedQueryPlanCount"] += 1
            stats["semanticAcceptedCount"] += accepted_count
            stats["semanticRejectedCount"] += semantic_rejected_count
            stats["queryPlanEvidence"].append(
                {
                    "queryPlanId": plan.sourcePlanId,
                    "providerPlanId": plan.planId,
                    "mode": plan.mode,
                    "providerCategoryKey": plan.providerCategoryKey,
                    "category": plan.category,
                    "fallbackLevel": plan.fallbackLevel,
                    "candidateRawCount": len(raw_pois),
                    "semanticAcceptedCount": accepted_count,
                    "semanticRejectedCount": semantic_rejected_count,
                    "duplicateExcludedCount": duplicate_excluded_count,
                    "routeContextQueryFingerprint": (
                        route_scope.get("queryFingerprint")
                        if route_scope is not None and is_route_scoped_plan
                        else None
                    ),
                }
            )
            if is_route_scoped_plan and accepted_count:
                # A real route-corridor hit is the only bounded candidate set
                # that belongs to this day and its adjacent anchors.  Do not
                # dilute it with an unrelated citywide fallback before the
                # shared Consumer Admission pass decides whether it is usable.
                # If Admission rejects it, the next retry must carry a new
                # query/entity/pair cursor rather than silently broaden scope.
                stats["routeScopedCandidateFound"] = True
                break
            # A route-scoped result is only a retrieval candidate.  It must not
            # consume the slot before the shared Consumer Admission pass.  Keep
            # searching within the existing call_limit instead of treating a
            # cheap precheck as final coverage.
            if self._collection_reached_unique_eligible_stop(stats):
                self._mark_pool_budget_stop(
                    stats,
                    "unique_eligible_stop_count_reached",
                    used_text_search=executed,
                )
                break
        return candidates, provider_state

    def _functional_slot_search_center(self, slot_context: FunctionalSlotContext) -> Optional[POI]:
        previous_coord = self._poi_coord(slot_context.previous_anchor)
        next_coord = self._poi_coord(slot_context.next_anchor)
        if previous_coord is not None and next_coord is not None:
            longitude = (previous_coord[0] + next_coord[0]) / 2
            latitude = (previous_coord[1] + next_coord[1]) / 2
            name = "route_midpoint"
        elif previous_coord is not None:
            longitude, latitude = previous_coord
            name = "route_previous_anchor"
        elif next_coord is not None:
            longitude, latitude = next_coord
            name = "route_next_anchor"
        else:
            return None
        return POI(
            id=f"functional_center_{slot_context.slot_id}",
            name=name,
            city="",
            category="route_context",
            latitude=latitude,
            longitude=longitude,
            source="route-context",
            confidence=1.0,
        )

    def _poi_pool_budget(
        self,
        intent_type: str,
        target_count: int,
        *,
        evidence_target_count: Optional[int] = None,
    ) -> dict[str, Any]:
        target_count = max(1, int(target_count or 1))
        evidence_target = max(1, int(evidence_target_count)) if evidence_target_count is not None else None
        if intent_type == "campus_visit":
            return {
                "textSearchMax": 4,
                "aroundSearchMax": 0,
                "uniqueEligibleStopCount": evidence_target or 8,
                "rawCandidateMax": 32,
            }
        if intent_type == "night_view":
            return {
                "textSearchMax": 4,
                "aroundSearchMax": 2,
                "uniqueEligibleStopCount": evidence_target or 5,
                "rawCandidateMax": 12,
            }
        if intent_type == "meal":
            return {
                "textSearchMax": 2,
                "aroundSearchMax": 8,
                "uniqueEligibleStopCount": 10,
                "rawCandidateMax": 32,
                "perSlotAroundMax": 2,
            }
        return {
            "textSearchMax": 2,
            "aroundSearchMax": 0,
            "uniqueEligibleStopCount": max(3, target_count + 2),
            "rawCandidateMax": max(8, target_count * 6),
        }

    def _mark_pool_budget_stop(
        self,
        stats: dict[str, Any],
        reason: str,
        *,
        used_text_search: Optional[int] = None,
    ) -> None:
        if used_text_search is not None:
            stats["usedTextSearch"] = used_text_search
        stats["earlyStopReason"] = reason
        stats["amapCallBudget"] = self._amap_call_budget_snapshot()

    def _collection_reached_unique_eligible_stop(self, stats: dict[str, Any]) -> bool:
        stop_count = int((stats.get("poolBudget") or {}).get("uniqueEligibleStopCount") or 0)
        return bool(stop_count and int(stats.get("uniqueEligibleEntityCount") or 0) >= stop_count)

    def _record_collection_candidate(
        self,
        intent: PoiIntent,
        stats: dict[str, Any],
        candidate: Any,
        *,
        count_raw: bool = True,
    ) -> None:
        if count_raw:
            stats["rawCandidateCount"] = int(stats.get("rawCandidateCount") or 0) + 1
        reasons = self._collection_candidate_rejection_reasons(intent, candidate)
        entity_key = self._candidate_canonical_entity_key_for_intent(intent.intent_type, candidate)
        unique_keys = [str(item) for item in stats.get("_uniqueEligibleEntityKeys") or [] if str(item)]
        if not reasons and entity_key and entity_key in unique_keys:
            reasons.append("duplicate_canonical_entity")
        if reasons:
            reason_counts = stats.setdefault("rejectedReasonCounts", {})
            if isinstance(reason_counts, dict):
                for reason in reasons:
                    reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1
            weak_reasons = {
                "weak_campus_entity",
                "campus_affiliated_subentity",
                "campus_remote_branch",
                "campus_type_missing",
                "institutional_meal",
                "hotel_meal",
                "out_of_city_food_brand",
                "local_food_relevance_missing",
                "weak_night_view_entity",
                "night_view_subpoi_requires_parent",
                "night_view_signal_missing",
                "functional_subpoi",
                "weak_subentity",
                "generic_intent_echo",
            }
            if any(reason in weak_reasons for reason in reasons):
                stats["rejectedWeakEntityCount"] = int(stats.get("rejectedWeakEntityCount") or 0) + 1
            if any("duplicate" in reason for reason in reasons):
                stats["rejectedDuplicateCount"] = int(stats.get("rejectedDuplicateCount") or 0) + 1
            return
        stats["eligibleCandidateCount"] = int(stats.get("eligibleCandidateCount") or 0) + 1
        if entity_key and entity_key not in unique_keys:
            unique_keys.append(entity_key)
        stats["_uniqueEligibleEntityKeys"] = unique_keys
        stats["uniqueEligibleEntityCount"] = len(unique_keys)

    def _collection_candidate_rejection_reasons(self, intent: PoiIntent, candidate: Any) -> list[str]:
        reasons: list[str] = []
        profile = intent.search_profile
        physical_id = str(getattr(candidate, "amap_id", "") or getattr(candidate, "id", "") or "").strip().upper()
        if profile is not None and physical_id in {
            str(item).strip().upper() for item in profile.excludedPhysicalPoiIds if str(item).strip()
        }:
            reasons.append("excluded_flexible_physical_poi")
        if not self._valid_coordinate(getattr(candidate, "longitude", None), getattr(candidate, "latitude", None)):
            reasons.append("missing_coordinates")
        if not self._candidate_city_matches(intent.city, candidate):
            reasons.append("city_mismatch")
        source = str(getattr(candidate, "source", "") or "")
        if source and source != AMAP_PLACE_SOURCE:
            reasons.append("non_amap_source")
        try:
            confidence = float(getattr(candidate, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.8:
            reasons.append("low_confidence")
        if self._candidate_is_generic_intent_echo(intent, candidate):
            reasons.append("generic_intent_echo")
        text = self._candidate_text(candidate)
        optional_experience_family = str(getattr(intent, "optional_experience_family", "") or "").strip()
        if optional_experience_family:
            semantic = self.intent_candidate_semantic_policy.evaluate(
                intent.intent_type,
                candidate,
                raw_need=intent.raw_need,
                exact_entity=getattr(intent, "exact_entity", None),
                optional_experience_family=optional_experience_family,
            )
            if not semantic.passed:
                reasons.append(semantic.reason_code)
        if intent.intent_type == "campus_visit":
            reason = self.campus_candidate_policy.reject_reason(candidate, intent.raw_need)
            if reason:
                reasons.append(reason)
        elif intent.intent_type == "meal":
            assigned_meal_family = str(getattr(intent, "assigned_meal_family", "") or "").strip()
            if assigned_meal_family:
                candidate_family = self.meal_experience_assignment_policy.family_for_candidate(
                    candidate,
                    preferred_family=assigned_meal_family,
                )
                if candidate_family != assigned_meal_family:
                    reasons.append("assigned_meal_family_mismatch")
            trust_reason = self.poi_trust_policy.meal_candidate_rejection_reason(candidate)
            if trust_reason:
                reasons.append(trust_reason)
            quality = self.meal_candidate_quality_policy.evaluate(
                intent.raw_need, intent.candidate_hints, candidate, city=intent.city
            )
            reasons.extend(quality.hard_reject_reasons)
        elif intent.intent_type == "night_view":
            reason = self.night_view_candidate_policy.reject_reason(
                candidate,
                amap_identity=getattr(candidate, "id", None),
            )
            if reason:
                reasons.append(reason)
        elif intent.intent_type in {
            "landmark",
            "area_walk",
            "local_culture",
        } and NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE.search(text):
            reasons.append("functional_subpoi")
        elif intent.intent_type not in {"rest", "meal"} and WEAK_ROUTE_ANCHOR_SUBENTITY_RE.search(text):
            reasons.append("weak_subentity")
        return list(dict.fromkeys(reasons))

    def _collect_functional_nearby_candidates(
        self,
        intent: PoiIntent,
        slot_context: FunctionalSlotContext,
        candidates: list,
        seen_ids: set[str],
        warnings: list[str],
        stats: dict[str, Any],
    ) -> str:
        hint_queries = [str(item).strip() for item in intent.candidate_hints or [] if str(item).strip()]
        raw_need_queries = [str(intent.raw_need or "").strip()]
        search_queries = [str(item).strip() for item in intent.search_queries or [] if str(item).strip()]
        if self._expand_density_nearby and not hint_queries:
            # Legacy checkpoints did not persist the original hint values.
            # Preserve their semantic request before broad category fallbacks.
            ordered_queries = [*raw_need_queries, *search_queries]
        else:
            ordered_queries = [*hint_queries, *search_queries, *raw_need_queries]
        queries = self._dedupe_queries(ordered_queries)[:2]
        if not queries:
            queries = ["餐厅" if intent.intent_type == "meal" else intent.raw_need or intent.intent_type]

        centers: list[tuple[str, float, float]] = []
        previous_coord = self._poi_coord(slot_context.previous_anchor)
        next_coord = self._poi_coord(slot_context.next_anchor)
        if previous_coord is not None and next_coord is not None:
            centers.append(
                ("near_midpoint", (previous_coord[0] + next_coord[0]) / 2, (previous_coord[1] + next_coord[1]) / 2)
            )
            centers.append(("near_next", next_coord[0], next_coord[1]))
        elif previous_coord is not None:
            centers.append(("near_previous", previous_coord[0], previous_coord[1]))
        elif next_coord is not None:
            centers.append(("near_next", next_coord[0], next_coord[1]))

        provider_state = "ok"
        around_calls = 0
        max_around_calls = 2 if intent.intent_type == "meal" else 4
        base_budget = self._poi_pool_budget(intent.intent_type, int(getattr(intent, "target_count", 1) or 1))
        raw_candidate_max = min(
            int(base_budget.get("rawCandidateMax") or 0),
            16 if intent.intent_type == "meal" else max(8, int(getattr(intent, "target_count", 1) or 1) * 6),
        )
        target_count = max(1, int(getattr(intent, "target_count", 1) or 1))
        unique_stop_count = target_count if intent.intent_type == "meal" else max(3, target_count + 2)
        stats["poolBudget"] = {
            **dict(stats.get("poolBudget") or {}),
            "aroundSearchMax": max_around_calls,
            "perSlotAroundMax": max_around_calls if intent.intent_type == "meal" else None,
            "uniqueEligibleStopCount": unique_stop_count,
            "rawCandidateMax": raw_candidate_max,
        }
        for mode, longitude, latitude in centers:
            for query in queries:
                if around_calls >= max_around_calls:
                    stats["skippedBecauseBudget"] = int(stats.get("skippedBecauseBudget") or 0) + 1
                    stats["usedAroundSearch"] = around_calls
                    stats["earlyStopReason"] = (
                        "per_slot_around_max_reached" if intent.intent_type == "meal" else "around_search_max_reached"
                    )
                    stats["amapCallBudget"] = self._amap_call_budget_snapshot()
                    return provider_state
                if self._poi_grounding_rate_limited:
                    return "rate_limited"
                try:
                    around_calls += 1
                    stats["usedAroundSearch"] = around_calls
                    response = self.map_poi_service.search_nearby(
                        intent.city,
                        longitude,
                        latitude,
                        query,
                        category=self._intent_search_category(intent.intent_type),
                        radius=slot_context.search_radius_meters,
                        limit=8,
                        **({"bypass_cache": True} if self._force_candidate_refresh else {}),
                    )
                except HTTPException as error:
                    self._append_provider_debug(stats, error)
                    raw_detail = error.detail
                    detail = str(raw_detail)
                    provider_state = self._amap_provider_state(raw_detail)
                    if provider_state == "rate_limited":
                        self._mark_poi_rate_limited(raw_detail)
                        warnings.append(
                            f"AMap nearby POI candidate search failed for {intent.raw_need}: rate_limited: {detail}"
                        )
                        return provider_state
                    if provider_state == "budget_exceeded":
                        warnings.append(
                            f"AMap nearby POI candidate search stopped for {intent.raw_need}: budget_exceeded"
                        )
                        return provider_state
                    warnings.append(f"AMap nearby POI candidate search failed for {intent.raw_need}: {detail}")
                    continue
                except Exception as error:
                    detail = str(error)
                    provider_state = "rate_limited" if self._is_amap_rate_limit_detail(detail) else "provider_down"
                    if provider_state == "rate_limited":
                        self._mark_poi_rate_limited(detail)
                        warnings.append(
                            f"AMap nearby POI candidate search failed for {intent.raw_need}: rate_limited: {detail}"
                        )
                        return provider_state
                    warnings.append(f"AMap nearby POI candidate search failed for {intent.raw_need}: {detail}")
                    continue
                if getattr(response, "cache_hit", False):
                    stats["cacheHitCount"] += 1
                stats["amapCallBudget"] = self._amap_call_budget_snapshot()
                for poi in response.pois:
                    if self._skip_low_relevance_explicit_local_meal_candidate(intent, poi):
                        continue
                    if self._append_unique_candidate(
                        candidates,
                        seen_ids,
                        poi,
                        search_mode=mode,
                        anchor_names=self._functional_anchor_names(slot_context),
                        matched_hint=query,
                    ):
                        self._record_collection_candidate(intent, stats, poi)
                        stats["nearbyCandidateCount"] += 1
                        if mode not in stats["searchModes"]:
                            stats["searchModes"].append(mode)
                        if self._collection_reached_unique_eligible_stop(stats):
                            stats["earlyStopReason"] = "unique_eligible_stop_count_reached"
                            stats["usedAroundSearch"] = around_calls
                            return provider_state
                        if raw_candidate_max and len(candidates) >= raw_candidate_max:
                            stats["earlyStopReason"] = "raw_candidate_budget_reached"
                            stats["usedAroundSearch"] = around_calls
                            return provider_state
        return provider_state

    def _skip_low_relevance_explicit_local_meal_candidate(self, intent: PoiIntent, candidate: Any) -> bool:
        if intent.intent_type != "meal":
            return False
        intent_text = " ".join(
            [
                str(intent.raw_need or ""),
                " ".join(str(item) for item in intent.candidate_hints or []),
                " ".join(str(item) for item in intent.search_queries or []),
            ]
        )
        if not re.search(r"(当地|本地|地方|特色|风味|老字号|小吃|菜系|夜市)", intent_text):
            return False
        candidate_text = self._candidate_text(candidate)
        current_city = re.sub(r"[市县区省]$", "", str(intent.city or "").strip())
        out_of_city_markers = [
            marker
            for marker in (
                "北京",
                "南京",
                "上海",
                "杭州",
                "苏州",
                "广州",
                "深圳",
                "长沙",
                "武汉",
                "西安",
                "成都",
                "重庆",
            )
            if marker and marker != current_city
        ]
        return any(marker in candidate_text for marker in out_of_city_markers)

    def _append_provider_debug(self, stats: dict[str, Any], error: HTTPException) -> None:
        detail = error.detail
        debug = detail.get("debug") if isinstance(detail, dict) else None
        if not isinstance(debug, dict):
            return
        provider_debug = stats.setdefault("providerDebug", [])
        if isinstance(provider_debug, list):
            provider_debug.append(debug)
        stats["amapCallBudget"] = self._amap_call_budget_snapshot()

    def _amap_call_budget_snapshot(self) -> dict[str, Any]:
        budget = current_amap_call_budget()
        return budget.snapshot() if budget is not None else {}

    def _append_unique_candidate(
        self,
        candidates: list,
        seen_ids: set[str],
        poi: Any,
        *,
        search_mode: str,
        anchor_names: Optional[list[str]] = None,
        matched_hint: str = "",
    ) -> bool:
        candidate_id = str(getattr(poi, "id", "") or "")
        key = candidate_id or self._normalize_poi_name(getattr(poi, "name", ""))
        if key and key not in seen_ids:
            self._set_candidate_search_metadata(
                poi,
                search_mode,
                anchor_names or [],
                matched_hint=matched_hint,
            )
            candidates.append(poi)
            seen_ids.add(key)
            return True
        return False

    def _set_candidate_search_metadata(
        self,
        poi: Any,
        search_mode: str,
        anchor_names: list[str],
        *,
        matched_hint: str = "",
    ) -> None:
        try:
            object.__setattr__(poi, "_trip_search_mode", search_mode)
            object.__setattr__(poi, "_trip_anchor_names", anchor_names)
            object.__setattr__(poi, "_trip_matched_hint", str(matched_hint or "").strip())
            object.__setattr__(poi, "_trip_matched_hint_binding", "search_query")
        except Exception:
            return

    @staticmethod
    def _set_candidate_profile_metadata(
        poi: Any,
        *,
        profile: Any,
        query_plan_id: str,
        search_mode: str,
        fallback_level: int,
        matched_keyword: str,
    ) -> None:
        try:
            object.__setattr__(poi, "_trip_search_profile_id", profile.profileId)
            object.__setattr__(
                poi,
                "_trip_search_profile_fingerprint",
                profile.profileFingerprint,
            )
            object.__setattr__(
                poi,
                "_trip_exclusion_fingerprint",
                profile.exclusionFingerprint,
            )
            object.__setattr__(poi, "_trip_experience_family", profile.experienceFamily)
            object.__setattr__(poi, "_trip_activity_mode", profile.activityMode)
            object.__setattr__(poi, "_trip_query_plan_id", query_plan_id)
            object.__setattr__(poi, "_trip_profile_search_mode", search_mode)
            object.__setattr__(poi, "_trip_fallback_level", int(fallback_level))
            object.__setattr__(poi, "_trip_matched_keyword", matched_keyword)
            object.__setattr__(poi, "_trip_source_brief_id", profile.briefId)
            object.__setattr__(poi, "_trip_source_pool_id", profile.poolId)
            object.__setattr__(
                poi,
                "_trip_source_planning_slot_id",
                profile.planningSlotId,
            )
        except Exception:
            return

    @staticmethod
    def _set_candidate_route_query_scope_metadata(
        poi: Any,
        *,
        route_scope: dict[str, Any],
    ) -> None:
        """Mark server-derived route recall as pending target-consumer policy."""

        scope = dict(route_scope)
        try:
            object.__setattr__(poi, "_trip_route_query_scope", scope)
            object.__setattr__(
                poi,
                "_trip_route_query_scope_fingerprint",
                str(scope.get("queryFingerprint") or ""),
            )
            object.__setattr__(poi, "_trip_route_query_scope_verified", True)
            object.__setattr__(poi, "_trip_consumer_admission_pending", True)
        except Exception:
            return

    def _functional_anchor_names(self, slot_context: FunctionalSlotContext) -> list[str]:
        names = []
        if slot_context.previous_anchor is not None:
            names.append(slot_context.previous_anchor.name)
        if slot_context.next_anchor is not None:
            names.append(slot_context.next_anchor.name)
        return names

    def _poi_coord(self, poi: Optional[POI]) -> Optional[tuple[float, float]]:
        if poi is None:
            return None
        try:
            longitude = float(poi.longitude)
            latitude = float(poi.latitude)
        except (TypeError, ValueError):
            return None
        if not self._valid_coordinate(longitude, latitude):
            return None
        return longitude, latitude

    def _intent_search_category(self, intent_type: str) -> str:
        if intent_type == "meal":
            return "food"
        if intent_type == "shopping":
            return "shopping"
        if intent_type == "campus_visit":
            return "campus"
        if intent_type == "museum":
            return "museum"
        if intent_type == "night_view":
            return "scenic"
        if intent_type in {"park", "landmark", "area_walk", "local_culture"}:
            return "scenic"
        return "all"

    def _score_poi_candidates(
        self,
        intent: PoiIntent,
        candidates: list,
        *,
        day_number: Optional[int] = None,
        same_day_used_keys: Optional[set[str]] = None,
    ) -> tuple[list[CandidateScore], list[CandidateScore]]:
        scored: list[CandidateScore] = []
        rejected: list[CandidateScore] = []
        preferred_re = INTENT_PREFERRED_TYPE_RE.get(intent.intent_type)
        rejected_terms = [term for term in intent.rejected_types or [] if str(term).strip()]
        rejected_re = (
            re.compile("|".join(map(re.escape, rejected_terms))) if rejected_terms else NON_LODGING_REJECTED_TYPE_RE
        )
        same_day_used_keys = same_day_used_keys or set()
        for candidate in candidates:
            text = " ".join(
                [
                    str(getattr(candidate, "name", "") or ""),
                    str(getattr(candidate, "type", "") or ""),
                    str(getattr(candidate, "category", "") or ""),
                    str(getattr(candidate, "address", "") or ""),
                    str(getattr(candidate, "district", "") or ""),
                ]
            )
            name_type_text = " ".join(
                [
                    str(getattr(candidate, "name", "") or ""),
                    str(getattr(candidate, "type", "") or ""),
                    str(getattr(candidate, "category", "") or ""),
                ]
            )
            location_text = " ".join(
                [
                    str(getattr(candidate, "address", "") or ""),
                    str(getattr(candidate, "district", "") or ""),
                ]
            )
            reasons: list[str] = []
            duplicate_key = self._candidate_duplicate_key(candidate)
            if self._candidate_rejected_by_type_blacklist(intent, rejected_re, name_type_text):
                reasons.append("type_blacklist")
            if intent.intent_type in {
                "night_view",
                "landmark",
                "area_walk",
                "local_culture",
            } and NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE.search(name_type_text):
                reasons.append("functional_subpoi")
            if intent.intent_type == "night_view":
                night_reason = self.night_view_candidate_policy.reject_reason(
                    candidate,
                    amap_identity=getattr(candidate, "id", None),
                )
                if night_reason:
                    reasons.append(night_reason)
            if intent.intent_type in {
                "night_view",
                "landmark",
                "area_walk",
                "local_culture",
                "campus_visit",
                "meal",
                "museum",
                "park",
            } and WEAK_ROUTE_ANCHOR_SUBENTITY_RE.search(name_type_text):
                reasons.append("weak_subentity")
            if intent.intent_type == "campus_visit":
                campus_reason = self.campus_candidate_policy.reject_reason(candidate, intent.raw_need)
                if campus_reason:
                    reasons.append(campus_reason)
            if intent.intent_type == "meal":
                trust_reason = self.poi_trust_policy.meal_candidate_rejection_reason(candidate)
                if trust_reason:
                    reasons.append(trust_reason)
                quality = self.meal_candidate_quality_policy.evaluate(
                    intent.raw_need, intent.candidate_hints, candidate, city=intent.city
                )
                reasons.extend(quality.hard_reject_reasons)
            if not self._valid_coordinate(getattr(candidate, "longitude", None), getattr(candidate, "latitude", None)):
                reasons.append("missing_coordinates")
            if not self._candidate_city_matches(intent.city, candidate):
                reasons.append("city_mismatch")
            if duplicate_key and duplicate_key in same_day_used_keys:
                reasons.append("duplicate_same_day")
            if self._candidate_is_generic_intent_echo(intent, candidate):
                reasons.append("generic_intent_echo")
            profile = intent.search_profile
            semantic_passed = False
            semantic_confidence = 0.0
            if profile is not None:
                semantic = self.intent_candidate_semantic_policy.evaluate(
                    intent.intent_type,
                    candidate,
                    raw_need=intent.raw_need,
                    exact_entity=getattr(intent, "exact_entity", None),
                    optional_experience_family=str(getattr(intent, "optional_experience_family", "") or ""),
                )
                semantic_passed = bool(semantic.passed)
                semantic_confidence = float(semantic.confidence or 0.0)
                try:
                    object.__setattr__(
                        candidate,
                        "_trip_semantic_match_score",
                        semantic_confidence,
                    )
                    object.__setattr__(
                        candidate,
                        "_trip_semantic_passed",
                        semantic_passed,
                    )
                    object.__setattr__(
                        candidate,
                        "_trip_matched_semantic_facets",
                        list(semantic.positive_signals),
                    )
                except Exception:
                    pass
                if not semantic_passed:
                    reasons.append(semantic.reason_code)
            physical_id = str(getattr(candidate, "amap_id", "") or getattr(candidate, "id", "") or "").strip().upper()
            if profile is not None and physical_id in {
                str(item).strip().upper() for item in profile.excludedPhysicalPoiIds if str(item).strip()
            }:
                reasons.append("excluded_flexible_physical_poi")
            components = {
                "typeMatch": 0.22 if preferred_re and preferred_re.search(text) else 0.0,
                "cityMatch": 0.20 if self._candidate_city_matches(intent.city, candidate) else 0.0,
                "nameIntentMatch": 0.18
                if self._candidate_matches_need(intent.raw_need, candidate)
                else self._keyword_overlap_score(intent.raw_need, candidate),
                "hintMatch": self._candidate_hint_match_score(intent, candidate),
                "semanticPreference": self._candidate_semantic_preference_score(intent, candidate),
                "routeFeasibility": 0.10
                if self._valid_coordinate(getattr(candidate, "longitude", None), getattr(candidate, "latitude", None))
                else 0.0,
                "uniqueness": 0.15 if not duplicate_key or duplicate_key not in same_day_used_keys else 0.0,
                "sourceReliability": self._candidate_source_reliability(candidate),
                "entityVariantPenalty": -self._candidate_entity_variant_penalty(candidate),
                "locationTextPenalty": -self._candidate_location_text_penalty(intent, location_text),
                "nightViewTypeQuality": self._night_view_type_quality(intent, candidate),
                "weakNightViewEntityPenalty": -self._night_view_weak_entity_penalty(intent, candidate),
                "nightViewPublicAccessScore": self._night_view_public_access_score(intent, candidate),
                "semanticFamilyMatch": (
                    round(0.2 * semantic_confidence, 4) if semantic_passed and profile is not None else 0.0
                ),
                "profileFallbackPenalty": -min(
                    0.15,
                    0.05 * int(getattr(candidate, "_trip_fallback_level", 0) or 0),
                ),
            }
            score = round(sum(components.values()), 4)
            reasons = list(dict.fromkeys(reasons))
            result = CandidateScore(candidate=candidate, score=score, components=components, rejected_reasons=reasons)
            if reasons:
                rejected.append(result)
            else:
                scored.append(result)
        scored.sort(key=lambda item: (-item.score, getattr(item.candidate, "name", "")))
        rejected.sort(key=lambda item: (getattr(item.candidate, "name", ""), item.rejected_reasons))
        return scored, rejected

    def _candidate_rejected_by_type_blacklist(
        self, intent: PoiIntent, rejected_re: re.Pattern, name_type_text: str
    ) -> bool:
        if intent.intent_type == "rest":
            return False
        if not rejected_re.search(name_type_text):
            return False
        if intent.intent_type == "meal":
            food_re = INTENT_PREFERRED_TYPE_RE.get("meal")
            food_like = bool(food_re and food_re.search(name_type_text))
            hard_non_food_subject = re.search(r"(酒店|宾馆|旅馆|民宿|公寓|住宿|写字楼|房地产|停车场)", name_type_text)
            if food_like and not hard_non_food_subject:
                return False
        return True

    def _candidate_is_generic_intent_echo(self, intent: PoiIntent, candidate) -> bool:
        if intent.specificity == "exact_entity":
            return False
        raw = self._normalize_poi_name(intent.raw_need)
        name = self._normalize_poi_name(getattr(candidate, "name", ""))
        generic_values = {
            self._normalize_poi_name(value)
            for value in [
                "高校参观",
                "夜景观景点",
                "区域漫步",
                "风土人情与本地生活体验",
                "地标景点",
                "核心地点参观",
                "餐厅",
            ]
        }
        return bool(
            name
            and name in generic_values
            and (
                not raw
                or raw == name
                or intent.intent_type
                in {"campus_visit", "night_view", "area_walk", "local_culture", "landmark", "meal"}
            )
        )

    def _candidate_hint_match_score(self, intent: PoiIntent, candidate) -> float:
        hints = [hint for hint in getattr(intent, "candidate_hints", []) or [] if str(hint).strip()]
        if not hints:
            return 0.0
        return 0.18 if any(self._candidate_matches_hint(str(hint), candidate) for hint in hints) else 0.0

    def _candidate_semantic_preference_score(self, intent: PoiIntent, candidate) -> float:
        if intent.intent_type != "campus_visit":
            return 0.0
        context = f"{getattr(intent, 'semantic_context', '')} {intent.raw_need}"
        if not re.search(r"(985|211|名校|高校|知名高校)", context):
            return 0.0
        if getattr(intent, "hint_policy", "no_hint") != "llm_common_knowledge_hint":
            return 0.0
        return 0.10 if self._candidate_hint_match_score(intent, candidate) > 0 else 0.0

    def _candidate_matches_hint(self, hint: str, candidate) -> bool:
        hint_name = self._canonical_entity_name(self._canonical_poi_query_name(hint))
        candidate_name = self._canonical_entity_name(getattr(candidate, "name", ""))
        if not hint_name or not candidate_name:
            return False
        raw_hint_name = self._normalize_poi_name(self._canonical_poi_query_name(hint))
        raw_candidate_name = self._normalize_poi_name(getattr(candidate, "name", ""))
        if (
            raw_hint_name
            and raw_candidate_name
            and (raw_hint_name in raw_candidate_name or raw_candidate_name in raw_hint_name)
        ):
            return True
        if hint_name in candidate_name or candidate_name in hint_name:
            return True
        return self._common_prefix_length(hint_name, candidate_name) >= 3

    def _candidate_matches_thematic_hint_result(self, intent: PoiIntent, hint: str, candidate) -> bool:
        if intent.intent_type not in {"area_walk", "local_culture", "landmark", "park", "museum"}:
            return False
        hint_text = f"{hint} {intent.raw_need}"
        if not re.search(
            r"(公园|地标|广场|街区|景点|景区|风景|自然|户外|亲子|博物馆|湿地|森林|核心|胡同|非遗|民俗|社区市场|文化馆)",
            hint_text,
        ):
            return False
        if not self._candidate_city_matches(intent.city, candidate):
            return False
        text = self._candidate_text(candidate)
        preferred_re = INTENT_PREFERRED_TYPE_RE.get(intent.intent_type)
        if preferred_re is not None and not preferred_re.search(text):
            return False
        if self._candidate_is_generic_intent_echo(intent, candidate):
            return False
        if intent.intent_type in {"landmark", "area_walk", "local_culture"} and NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE.search(
            text
        ):
            return False
        if WEAK_ROUTE_ANCHOR_SUBENTITY_RE.search(text):
            return False
        return True

    def _candidate_is_broad_night_view_result(self, candidate) -> bool:
        text = self._candidate_text(candidate)
        if NIGHT_VIEW_WEAK_ENTITY_RE.search(text) or NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE.search(text):
            return False
        return bool(NIGHT_VIEW_PUBLIC_ACCESS_RE.search(text))

    def _keyword_overlap_score(self, raw_need: str, candidate) -> float:
        raw_tokens = {
            token
            for token in re.split(r"[\s/／、,，;；]+", self._normalize_poi_name(raw_need))
            if len(token) >= 2 and token not in {"参观", "游览", "观景点", "夜景", "高校"}
        }
        candidate_text = self._normalize_poi_name(
            " ".join(
                [
                    str(getattr(candidate, "name", "") or ""),
                    str(getattr(candidate, "type", "") or ""),
                    str(getattr(candidate, "address", "") or ""),
                    str(getattr(candidate, "district", "") or ""),
                ]
            )
        )
        if not raw_tokens or not candidate_text:
            return 0.0
        overlap = sum(1 for token in raw_tokens if token in candidate_text)
        return min(0.18, 0.06 * overlap)

    def _candidate_source_reliability(self, candidate) -> float:
        if self.poi_trust_policy.is_mock_or_synthetic_candidate(candidate):
            return 0.0
        source = str(getattr(candidate, "source", "") or "")
        provider_score = 0.10 if source == AMAP_PLACE_SOURCE else 0.0
        id_score = 0.03 if str(getattr(candidate, "id", "") or getattr(candidate, "amap_id", "") or "") else 0.0
        confidence_score = 0.02 if float(getattr(candidate, "confidence", 0.0) or 0.0) >= 0.8 else 0.0
        return provider_score + id_score + confidence_score

    def _candidate_location_text_penalty(self, intent: PoiIntent, location_text: str) -> float:
        if intent.intent_type == "campus_visit" and CAMPUS_REMOTE_BRANCH_RE.search(location_text):
            return 0.06
        if intent.intent_type == "campus_visit" and CAMPUS_REMOTE_ROUTE_LOCATION_RE.search(location_text):
            return 0.06
        if intent.intent_type in {
            "night_view",
            "landmark",
            "area_walk",
            "local_culture",
        } and NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE.search(location_text):
            return 0.04
        return 0.0

    def _night_view_type_quality(self, intent: PoiIntent, candidate) -> float:
        if intent.intent_type != "night_view":
            return 0.0
        text = self._candidate_text(candidate)
        if NIGHT_VIEW_PUBLIC_ACCESS_RE.search(text):
            return 0.12
        return 0.0

    def _night_view_weak_entity_penalty(self, intent: PoiIntent, candidate) -> float:
        if intent.intent_type != "night_view":
            return 0.0
        text = self._candidate_text(candidate)
        if NIGHT_VIEW_WEAK_ENTITY_RE.search(text):
            return 0.24
        if NIGHT_VIEW_FUNCTIONAL_SUBPOI_RE.search(text):
            return 0.18
        return 0.0

    def _night_view_public_access_score(self, intent: PoiIntent, candidate) -> float:
        if intent.intent_type != "night_view":
            return 0.0
        text = self._candidate_text(candidate)
        return 0.08 if NIGHT_VIEW_PUBLIC_ACCESS_RE.search(text) else 0.0

    def _candidate_text(self, candidate) -> str:
        return " ".join(
            [
                str(getattr(candidate, "name", "") or ""),
                str(getattr(candidate, "type", "") or ""),
                str(getattr(candidate, "category", "") or ""),
                str(getattr(candidate, "address", "") or ""),
                str(getattr(candidate, "district", "") or ""),
            ]
        )

    def _select_ranked_candidate(
        self,
        intent: PoiIntent,
        candidates: list,
        *,
        plan: Optional[ItineraryPlan],
        original: Optional[POI],
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
    ) -> Optional[object]:
        ranked = self._rank_poi_candidates(
            intent, candidates, plan=plan, original=original, pois_by_id=pois_by_id, resolved_by_id=resolved_by_id
        )
        if not ranked:
            return None
        best_poi, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score < 0.72:
            return None
        if second_score and best_score - second_score < 0.1:
            return None
        return best_poi

    def _rank_poi_candidates(
        self,
        intent: PoiIntent,
        candidates: list,
        *,
        plan: Optional[ItineraryPlan],
        original: Optional[POI],
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
    ) -> list[tuple[object, float]]:
        duplicate_keys = self._same_day_duplicate_keys(plan.id, original.id) if plan and original else set()
        scored, _rejected = self._score_poi_candidates(
            intent, candidates, day_number=intent.day_number, same_day_used_keys=duplicate_keys
        )
        ranked = [(item.candidate, item.score) for item in scored]
        if len(ranked) >= 2 and self._route_aware_should_call(ranked):
            reranked: list[tuple[object, float]] = []
            for index, (candidate, score) in enumerate(ranked):
                if index < 2 and self._route_aware_request_count < self._route_aware_request_budget:
                    route_penalty = self._route_context_penalty(
                        intent,
                        candidate,
                        plan=plan,
                        original=original,
                        pois_by_id=pois_by_id,
                        resolved_by_id=resolved_by_id,
                    )
                    if route_penalty:
                        self._route_aware_request_count += 1
                    score -= route_penalty
                reranked.append((candidate, score))
            ranked = sorted(reranked, key=lambda item: (-item[1], getattr(item[0], "name", "")))
        return ranked

    def _route_aware_should_call(self, ranked: list[tuple[object, float]]) -> bool:
        if len(ranked) < 2:
            return False
        if not getattr(self.route_service, "map_provider_key", ""):
            return False
        top_score = ranked[0][1]
        second_score = ranked[1][1]
        return top_score - second_score < 0.16

    def _poi_from_ranked_candidate(self, intent: PoiIntent, candidate) -> POI:
        if intent.intent_type == "meal" and self.poi_trust_policy.meal_candidate_rejection_reason(candidate):
            return self._pending_poi_from_untrusted_meal_candidate(intent, candidate)
        exact_strong_match = intent.specificity == "exact_entity" and self._candidate_exact_entity_strong_match(
            intent.raw_need, candidate
        )
        grounding_status = "verified_amap" if exact_strong_match else "agent_selected_candidate"
        candidate_note = str(
            getattr(candidate, "source_note", "") or getattr(candidate, "sourceNote", "") or ""
        ).strip()
        matched_hint = str(getattr(candidate, "_trip_matched_hint", "") or "").strip()
        provider_note = f"；providerSourceNote：{candidate_note}" if candidate_note else ""
        hint_note = (
            f"；matchedCandidateHint：{matched_hint}；matchedCandidateHintBinding：search_query" if matched_hint else ""
        )
        poi = self._poi_from_amap_response(
            candidate,
            city=intent.city,
            source_note=(
                f"Agent 已根据 PoiIntent 从高德候选自动选择「{candidate.name}」。"
                f"groundingStatus：{grounding_status}；"
                f"rawNeed：{intent.raw_need}；intentType：{intent.intent_type}；来源：高德地图"
                f"{provider_note}{hint_note}"
            ),
            confidence=min(
                0.98, max(float(getattr(candidate, "confidence", 0.0) or 0.0), 0.92 if exact_strong_match else 0.86)
            ),
        )
        if hasattr(candidate, "model_dump"):
            evidence = candidate.model_dump(by_alias=True, exclude_none=True)
        elif isinstance(candidate, dict):
            evidence = dict(candidate)
        else:
            evidence = {
                name: getattr(candidate, name, None)
                for name in (
                    "id",
                    "name",
                    "type",
                    "category",
                    "city",
                    "source",
                    "providerTypeCode",
                    "provider_type_code",
                    "tags",
                    "sourceClaims",
                    "source_claims",
                )
            }
        # Provider facts are current-turn admission evidence. Keep them
        # ephemeral instead of serializing a second truth into POI.sourceNote.
        object.__setattr__(poi, "_trip_candidate_evidence", evidence)
        return poi

    def _pending_poi_from_untrusted_meal_candidate(self, intent: PoiIntent, candidate: Any) -> POI:
        name = str(getattr(candidate, "name", "") or intent.raw_need or "当地特色美食").strip()
        reason = (
            self.poi_trust_policy.meal_candidate_rejection_reason(candidate) or "untrusted_mock_or_synthetic_candidate"
        )
        return POI(
            id=f"poi_{uuid4().hex[:12]}",
            name=f"待选择顺路餐馆：{intent.raw_need or name}",
            city=intent.city,
            category="food",
            latitude=None,
            longitude=None,
            source=AGENT_TEXT_TIMELINE_SOURCE,
            confidence=0.35,
            amap_id=None,
            type=str(getattr(candidate, "type", "") or getattr(candidate, "category", "") or "餐饮服务"),
            district=str(getattr(candidate, "district", "") or ""),
            address=str(getattr(candidate, "address", "") or ""),
            source_note=(
                "pendingMeal=true；groundingStatus：waiting_for_poi_grounding；intentType：meal；"
                "routeAnchor=false；needsConcretePoi=true；"
                f"pendingReason={reason}；rejectedCandidate={name}；"
                "userAction=请基于当前高校/夜景路线，在地图上手动选择顺路餐馆；"
                "该餐饮是待补全占位，不是高德确认地点；已暂时从路线计算中排除，避免用错误位置误导路线。"
            ),
        )

    def _candidate_exact_entity_strong_match(self, raw_need: str, candidate) -> bool:
        raw = self._normalize_poi_name(self._canonical_poi_query_name(raw_need))
        name = self._normalize_poi_name(getattr(candidate, "name", ""))
        if not raw or not name:
            return False
        return raw == name or raw in name or name in raw

    def _candidate_city_matches(self, city: str, candidate) -> bool:
        if not city:
            return True
        candidate_city = str(getattr(candidate, "city", "") or "")
        if not candidate_city:
            return True
        return self._normalize_poi_name(city) in self._normalize_poi_name(candidate_city) or self._normalize_poi_name(
            candidate_city
        ) in self._normalize_poi_name(city)

    def _candidate_matches_need(self, raw_need: str, candidate) -> bool:
        name = self._normalize_poi_name(getattr(candidate, "name", ""))
        if not name:
            return False
        raw_values = {
            self._normalize_poi_name(raw_need),
            self._normalize_poi_name(self._canonical_poi_query_name(raw_need)),
        }
        cleaned = re.sub(r"(夜景|夜游|夜间|灯光|观景点|观景|参观|游览|核心地点|地标景点)", "", str(raw_need or ""))
        cleaned = self._normalize_poi_name(cleaned)
        if cleaned:
            raw_values.add(cleaned)
        for raw in raw_values:
            if raw and (raw in name or name in raw):
                return True
            if raw and self._common_prefix_length(raw, name) >= 3:
                return True
        return False

    def _common_prefix_length(self, left: str, right: str) -> int:
        count = 0
        for left_char, right_char in zip(left, right):
            if left_char != right_char:
                break
            count += 1
        return count

    def _candidate_duplicate_key(self, candidate) -> str:
        amap_id = str(getattr(candidate, "id", "") or getattr(candidate, "amap_id", "") or "")
        return amap_id or self._normalize_poi_name(getattr(candidate, "name", ""))

    def _candidate_canonical_entity_key(self, candidate) -> str:
        return self._canonical_entity_name(getattr(candidate, "name", ""))

    def _candidate_canonical_entity_key_for_intent(self, intent_type: str, candidate) -> str:
        if not self._entity_like_intent(intent_type):
            return self._candidate_duplicate_key(candidate)
        return self._candidate_canonical_entity_key(candidate) or self._candidate_duplicate_key(candidate)

    def _entity_like_intent(self, intent_type: str) -> bool:
        return intent_type in {
            "campus_visit",
            "museum",
            "park",
            "landmark",
            "night_view",
            "shopping",
            "area_walk",
            "local_culture",
        }

    def _canonical_entity_name(self, value: str) -> str:
        raw = str(value or "")
        bracket_stripped = re.sub(r"[\(（【\[].*?[\)）】\]]", "", raw)
        normalized = self._normalize_poi_name(bracket_stripped)
        original_normalized = self._normalize_poi_name(raw)
        if not normalized and not original_normalized:
            return ""
        cleaned = normalized or original_normalized
        campus_anchor = self._first_entity_anchor(cleaned, ("大学", "高等院校", "学院"))
        if campus_anchor:
            return campus_anchor
        suffix_terms = "校区|分校区|分校|分馆|入口|出入口|南院|北院|东院|西院|东门|西门|南门|北门|停车场|售票处|游客中心|服务中心|国际学院|继续教育学院|附属机构|管理处|票务中心"
        entity_anchor_re = re.compile(
            r"(?P<body>.+?(?:博物馆|纪念馆|美术馆|科技馆|展览馆|公园|景区|风景区|商圈|步行街|购物中心|商业中心|广场|塔|桥|体育场|体育馆|剧院|寺|宫|城|湖|山|园))"
            rf"(?:[\u4e00-\u9fa5]{{0,12}})?(?:{suffix_terms})$"
        )
        for _ in range(3):
            previous = cleaned
            match = entity_anchor_re.match(cleaned)
            if match and len(match.group("body")) >= 2:
                cleaned = match.group("body")
            cleaned = re.sub(rf"(?:{suffix_terms})$", "", cleaned)
            if cleaned == previous:
                break
        return cleaned or normalized or original_normalized

    def _first_entity_anchor(self, normalized: str, anchors: tuple[str, ...]) -> str:
        positions = [(normalized.find(anchor), anchor) for anchor in anchors if anchor and normalized.find(anchor) >= 0]
        if not positions:
            return ""
        position, anchor = min(positions, key=lambda item: item[0])
        return normalized[: position + len(anchor)]

    def _candidate_entity_variant_penalty(self, candidate) -> float:
        name = self._normalize_poi_name(getattr(candidate, "name", ""))
        canonical = self._candidate_canonical_entity_key(candidate)
        if not name or not canonical or name == canonical:
            return 0.0
        text = self._normalize_poi_name(
            " ".join(
                [
                    str(getattr(candidate, "name", "") or ""),
                    str(getattr(candidate, "address", "") or ""),
                    str(getattr(candidate, "district", "") or ""),
                ]
            )
        )
        if CAMPUS_REMOTE_BRANCH_RE.search(text):
            return 0.18
        if CAMPUS_REMOTE_ROUTE_LOCATION_RE.search(text):
            return 0.16
        if CAMPUS_AFFILIATED_SUBENTITY_RE.search(text):
            return 0.16
        return 0.045

    def _same_day_duplicate_keys(self, plan_id: str, poi_id: str) -> set[str]:
        rows = self.db.execute(
            """
            SELECT p.amap_id, p.name
            FROM itinerary_segments current_segment
            JOIN itinerary_segments s ON s.day_id = current_segment.day_id
            JOIN pois p ON p.id = s.poi_id
            WHERE current_segment.plan_id = ?
              AND current_segment.poi_id = ?
              AND s.poi_id != current_segment.poi_id
              AND s.kind IN ('visit', 'activity')
            """,
            (plan_id, poi_id),
        ).fetchall()
        keys = set()
        for row in rows:
            if row["amap_id"]:
                keys.add(str(row["amap_id"]))
            keys.add(self._normalize_poi_name(row["name"]))
        return keys

    def _intent_nearby_center(
        self,
        plan: Optional[ItineraryPlan],
        original: Optional[POI],
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
    ) -> Optional[POI]:
        if plan is None or original is None:
            return None
        return self._functional_context_center(plan.id, original.id, pois_by_id, resolved_by_id)

    def _route_context_penalty(
        self,
        intent: PoiIntent,
        candidate,
        *,
        plan: Optional[ItineraryPlan],
        original: Optional[POI],
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
    ) -> float:
        if plan is None or original is None or not getattr(self.route_service, "map_provider_key", ""):
            return 0.0
        candidate_poi = self._poi_from_amap_response(candidate, city=intent.city)
        neighbors = self._route_context_neighbors(plan.id, original.id, pois_by_id, resolved_by_id)
        if not neighbors:
            return 0.0
        penalty = 0.0
        for neighbor in neighbors:
            if neighbor.amap_id and neighbor.amap_id == candidate_poi.amap_id:
                return 1.0
            if self._normalize_poi_name(neighbor.name) == self._normalize_poi_name(candidate_poi.name):
                return 1.0
            try:
                payload = self.route_service._fetch_amap_route(neighbor, candidate_poi, "walking")
                distance, duration, _cost, _polyline, _steps = self.route_service._parse_amap_route(payload, "walking")
            except Exception:
                penalty += 0.18
                continue
            if distance > 12000 or duration > 5400:
                penalty += 0.12
        return min(0.4, penalty)

    def _route_context_neighbors(
        self,
        plan_id: str,
        poi_id: str,
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
    ) -> list[POI]:
        rows = self.db.execute(
            """
            SELECT s.day_id, s.segment_order, s.poi_id
            FROM itinerary_segments s
            WHERE s.plan_id = ? AND s.kind IN ('visit', 'activity')
            ORDER BY s.day_id, s.segment_order
            """,
            (plan_id,),
        ).fetchall()
        current_index = next((index for index, row in enumerate(rows) if row["poi_id"] == poi_id), -1)
        if current_index < 0:
            return []
        current_day_id = rows[current_index]["day_id"]
        neighbors = []
        for index in (current_index - 1, current_index + 1):
            if index < 0 or index >= len(rows) or rows[index]["day_id"] != current_day_id:
                continue
            neighbor_id = rows[index]["poi_id"]
            neighbor = resolved_by_id.get(neighbor_id) or pois_by_id.get(neighbor_id)
            if neighbor is not None and self._has_routeable_amap_anchor(neighbor):
                neighbors.append(neighbor)
        return neighbors

    def _day_number_for_poi(self, plan_id: str, poi_id: str) -> Optional[int]:
        row = self.db.execute(
            """
            SELECT d.day_number
            FROM itinerary_segments s
            JOIN itinerary_days d ON d.id = s.day_id
            WHERE s.plan_id = ? AND s.poi_id = ?
            LIMIT 1
            """,
            (plan_id, poi_id),
        ).fetchone()
        return int(row["day_number"]) if row else None

    def _time_window_for_poi(self, plan_id: str, poi_id: str) -> str:
        row = self.db.execute(
            """
            SELECT start_time, end_time
            FROM itinerary_segments
            WHERE plan_id = ? AND poi_id = ?
            LIMIT 1
            """,
            (plan_id, poi_id),
        ).fetchone()
        if not row:
            return ""
        return f"{row['start_time']}-{row['end_time']}"

    def _provider_rate_limited_anchor(self, city: str, name: str) -> POI:
        return POI(
            id=f"poi_{uuid4().hex[:12]}",
            name=name,
            city=city,
            category="unresolved",
            latitude=None,
            longitude=None,
            source=AGENT_TEXT_TIMELINE_SOURCE,
            confidence=0.0,
            source_note="groundingStatus：provider_rate_limited；高德限流，稍后重试。",
        )

    def _area_unresolved_anchor(self, city: str, name: str) -> POI:
        return POI(
            id=f"poi_{uuid4().hex[:12]}",
            name=name,
            city=city,
            category="unresolved",
            latitude=None,
            longitude=None,
            source=AGENT_TEXT_TIMELINE_SOURCE,
            confidence=0.0,
            source_note="groundingStatus：area_unresolved；区域意图未细化为具体高德地点。",
        )

    def _is_amap_rate_limit_detail(self, detail: object) -> bool:
        classified_reason = self._amap_debug_classified_reason(detail)
        if classified_reason:
            return classified_reason in AMAP_RATE_LIMIT_CLASSIFICATIONS
        if isinstance(detail, dict):
            code = str(detail.get("code") or "")
            if code == "provider_rate_limited":
                return True
            if code == "provider_down":
                return False
        return bool(AMAP_RATE_LIMIT_RE.search(str(detail or "")))

    def _amap_provider_state(self, detail: object) -> str:
        if self._is_amap_budget_exceeded_detail(detail):
            return "budget_exceeded"
        return "rate_limited" if self._is_amap_rate_limit_detail(detail) else "provider_down"

    def _is_amap_budget_exceeded_detail(self, detail: object) -> bool:
        if isinstance(detail, dict):
            code = str(detail.get("code") or "")
            if code in AMAP_BUDGET_EXCEEDED_CODES:
                return True
            classified_reason = self._amap_debug_classified_reason(detail)
            if classified_reason in AMAP_BUDGET_EXCEEDED_CODES:
                return True
        return bool(re.search(r"\b(?:amap_)?budget_exceeded\b", str(detail or ""), re.IGNORECASE))

    def _amap_debug_classified_reason(self, detail: object) -> str:
        if not isinstance(detail, dict):
            return ""
        debug = detail.get("debug")
        if not isinstance(debug, dict):
            return ""
        return str(debug.get("classifiedReason") or "").strip()

    def _mark_poi_rate_limited(self, detail: object) -> None:
        self._poi_grounding_rate_limited = True
        self._poi_grounding_rate_limit_reason = str(detail or "高德 POI 查询频率受限")

    def _canonical_poi_query_name(self, name: str) -> str:
        return name

    def _poi_specificity_from_text(self, name: object, source_note: object = "") -> str:
        note = str(source_note or "")
        note_match = re.search(r"POI意图[:：]\s*(exact_entity|area_poi|functional_poi|composite_poi)", note)
        if note_match:
            return note_match.group(1)
        raw_name = str(name or "").strip()
        normalized = self._normalize_poi_name(raw_name)
        has_functional = bool(FUNCTIONAL_POI_RE.search(raw_name))
        has_area_marker = bool(AREA_POI_RE.search(raw_name)) or any(
            self._normalize_poi_name(area) in normalized for area in KNOWN_AREA_POI_NAMES
        )
        functional_only_values = {
            "午餐",
            "晚餐",
            "早餐",
            "早饭",
            "中饭",
            "午饭",
            "吃饭",
            "用餐",
            "餐厅",
            "美食",
            "咖啡",
            "下午茶",
            "夜景",
            "休息",
            "购物",
            "漫步",
        }
        if normalized in {self._normalize_poi_name(value) for value in functional_only_values}:
            return POI_SPECIFICITY_FUNCTIONAL
        if has_functional and (has_area_marker or len(normalized) > 4):
            return POI_SPECIFICITY_COMPOSITE
        if has_area_marker:
            return POI_SPECIFICITY_AREA
        return POI_SPECIFICITY_EXACT

    def _poi_intent_type(self, name: object, specificity: str) -> Optional[str]:
        raw_name = str(name or "")
        if re.search(r"(午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶)", raw_name):
            return "dining"
        if re.search(r"(夜景|漫步|拍照|打卡)", raw_name):
            return "experience"
        if re.search(r"(休息|酒店|住宿)", raw_name):
            return "rest"
        if re.search(r"(购物|商场|商圈)", raw_name):
            return "shopping"
        if specificity in {POI_SPECIFICITY_AREA, POI_SPECIFICITY_COMPOSITE}:
            return "area"
        return None

    def _resolve_area_like_poi(
        self, city: str, name: str, initial_candidates: list, warnings: list[str]
    ) -> Optional[POI]:
        anchor = self._select_area_anchor(name, initial_candidates)
        if anchor is None:
            for keyword, category in self._area_grounding_queries(name):
                try:
                    response = self.map_poi_service.search(city, keyword, category, limit=5)
                except HTTPException as error:
                    warnings.append(f"AMap area POI resolve failed for {name}: {error.detail}")
                    continue
                except Exception as error:
                    warnings.append(f"AMap area POI resolve failed for {name}: {error}")
                    continue
                anchor = self._select_area_anchor(name, response.pois)
                if anchor is not None:
                    break
        if anchor is None:
            return None
        specificity = self._poi_specificity_from_text(name)
        warnings.append(f"非具体地点「{name}」已按高德地点「{anchor.name}」附近范围定位，路线按该范围锚点规划。")
        resolved = self._poi_from_amap_response(
            anchor,
            city=city,
            display_name=name,
            source_note=(
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。"
                + f"POI意图：{specificity}；"
                + f"地图锚点：{anchor.name}；"
                + "建议选择具体场馆/入口/区域后再查询票务预约。"
            ),
            confidence=min(anchor.confidence, 0.45),
        )
        return replace(resolved, source=AGENT_TEXT_TIMELINE_SOURCE)

    def _is_area_like_poi_name(self, name: str) -> bool:
        return self._poi_specificity_from_text(name) in {POI_SPECIFICITY_AREA, POI_SPECIFICITY_COMPOSITE}

    def _area_grounding_queries(self, name: str) -> list[tuple[str, str]]:
        meal_like = bool(re.search(r"(午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶)", name))
        category = "food" if meal_like else "all"
        cleaned = re.sub(
            r"(周边|附近|周围|一带|区域|商圈|午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶)", " ", name
        )
        cleaned = re.sub(r"[/／、,，;；]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        suffix = " 餐厅" if meal_like else ""
        queries: list[tuple[str, str]] = []
        if cleaned:
            queries.append((f"{cleaned}{suffix}".strip(), category))
            for token in cleaned.split()[:2]:
                queries.append((f"{token}{suffix}".strip(), category))
        queries.append((name, category))
        deduped = []
        seen = set()
        for keyword, item_category in queries:
            key = (keyword, item_category)
            if keyword and key not in seen:
                deduped.append(key)
                seen.add(key)
        return deduped

    def _select_area_anchor(self, name: str, candidates: list) -> Optional[object]:
        if not candidates:
            return None
        candidates = [
            candidate
            for candidate in candidates
            if not NON_LODGING_REJECTED_TYPE_RE.search(
                " ".join(
                    [
                        str(candidate.name or ""),
                        str(candidate.address or ""),
                        str(candidate.district or ""),
                        str(candidate.type or ""),
                    ]
                )
            )
        ]
        if not candidates:
            return None
        area_terms = [
            self._normalize_poi_name(term)
            for term in re.split(r"[/／、,，;\s]+", name)
            if self._normalize_poi_name(term)
        ]
        wants_food = bool(re.search(r"(午餐|晚餐|早餐|饭|餐|美食|咖啡)", name))

        def score(candidate) -> tuple[int, int, str]:
            candidate_text = self._normalize_poi_name(
                " ".join([candidate.name, candidate.address, candidate.district, candidate.type])
            )
            term_match = any(term and (term in candidate_text or candidate_text in term) for term in area_terms)
            food_match = bool(re.search(r"(餐饮|餐厅|美食|咖啡|小吃|饭店)", candidate.type + candidate.name))
            return (0 if term_match else 1, 0 if not wants_food or food_match else 1, candidate.name)

        return sorted(candidates, key=score)[0]

    def _amap_poi_name_matches(self, query_name: str, poi_name: str) -> bool:
        normalized_query = self._normalize_poi_name(query_name)
        normalized_name = self._normalize_poi_name(poi_name)
        return bool(
            normalized_query
            and normalized_name
            and (
                normalized_query == normalized_name
                or normalized_query in normalized_name
                or normalized_name in normalized_query
            )
        )

    def _normalize_poi_name(self, value: str) -> str:
        return re.sub(r"[\s\-_,.()（）·・，。]+", "", str(value or "")).casefold()

    def _ground_agent_text_timeline_pois(
        self,
        plan: ItineraryPlan,
        pois: list[POI],
        only_poi_ids: Optional[set[str]] = None,
    ) -> tuple[bool, list[str]]:
        warnings: list[str] = []
        resolved_pois: list[tuple[str, POI]] = []
        self._poi_grounding_rate_limited = False
        self._poi_grounding_rate_limit_reason = ""
        targets = [
            poi
            for poi in pois
            if self._needs_agent_text_grounding(poi)
            and self._poi_has_required_visit_segment(plan.id, poi.id)
            and (only_poi_ids is None or poi.id in only_poi_ids)
        ]
        if not targets:
            return False, warnings

        ordered_targets = self._ordered_grounding_targets(plan.id, targets)
        non_functional_targets = [
            poi
            for poi in ordered_targets
            if self._poi_specificity_from_text(poi.name, poi.source_note) != POI_SPECIFICITY_FUNCTIONAL
        ]
        functional_targets = [
            poi
            for poi in ordered_targets
            if self._poi_specificity_from_text(poi.name, poi.source_note) == POI_SPECIFICITY_FUNCTIONAL
        ]
        pois_by_id = {poi.id: poi for poi in pois}
        resolved_by_id: dict[str, POI] = {}

        for original in [*non_functional_targets, *functional_targets]:
            if self._poi_grounding_rate_limited:
                warnings.append(
                    f"高德 POI 查询触发频率限制，已停止继续请求剩余地点：{self._poi_grounding_rate_limit_reason}"
                )
                break
            target_warnings: list[str] = []
            original_name = original.name
            specificity = self._poi_specificity_from_text(original.name, original.source_note)
            if specificity == POI_SPECIFICITY_FUNCTIONAL:
                resolved = self._resolve_functional_poi_near_context(
                    plan, original, pois_by_id, resolved_by_id, target_warnings
                )
            else:
                resolved = self._resolve_amap_poi(
                    plan.city,
                    original.name,
                    target_warnings,
                    plan=plan,
                    original=original,
                    pois_by_id=pois_by_id,
                    resolved_by_id=resolved_by_id,
                )
            warnings.extend(target_warnings)
            intent_type = self._intent_type_from_source_note(original.source_note)
            if resolved is not None and intent_type:
                semantic = self.intent_candidate_semantic_policy.evaluate(intent_type, resolved)
                if not semantic.passed:
                    warnings.append(
                        f"Rejected {resolved.name}: intent semantic gate {semantic.reason_code}; "
                        f"保留原待补全地点「{original_name}」。"
                    )
                    resolved = None
            if resolved is not None and self._is_verifier_grade_amap_poi(resolved):
                resolved_pois.append((original.id, resolved))
                resolved_by_id[original.id] = resolved
            elif resolved is not None and self._has_routeable_amap_anchor(resolved):
                confidence = self._draft_anchor_confidence(original)
                anchor = self._agent_text_timeline_anchor(original_name, resolved, confidence, specificity)
                resolved_pois.append((original.id, anchor))
                resolved_by_id[original.id] = anchor
                warnings.append(
                    f"Agent 文本地点「{original_name}」已用高德地点「{resolved.name}」作为待校验地图锚点；路线按该锚点规划，仍需用户核对。"
                )
            elif resolved is not None:
                warnings.append(
                    f"Agent 文本地点「{original_name}」高德 grounding 置信度不足，已保留可编辑草稿，待地图确认。"
                )
        if self._poi_grounding_rate_limited and not any("频率限制" in warning for warning in warnings):
            warnings.append(
                f"高德 POI 查询触发频率限制，已停止继续请求剩余地点：{self._poi_grounding_rate_limit_reason}"
            )
        grounded = bool(resolved_pois)
        for poi_id, resolved in resolved_pois:
            self.db.execute(
                """
                UPDATE pois
                SET name = ?, city = ?, category = ?, latitude = ?, longitude = ?,
                    photo_url = ?, source = ?, confidence = ?, amap_id = ?,
                    type = ?, district = ?, address = ?, source_note = ?,
                    source_url = ?, photos_json = ?, provider_type_code = ?,
                    tags_json = ?, source_claims_json = ?
                WHERE id = ? AND plan_id = ?
                """,
                (
                    resolved.name,
                    resolved.city,
                    resolved.category,
                    resolved.latitude,
                    resolved.longitude,
                    resolved.photo_url,
                    resolved.source,
                    resolved.confidence,
                    resolved.amap_id,
                    resolved.type,
                    resolved.district,
                    resolved.address,
                    resolved.source_note,
                    resolved.source_url,
                    json.dumps(resolved.photos, ensure_ascii=False),
                    resolved.provider_type_code,
                    json.dumps(resolved.tags, ensure_ascii=False),
                    json.dumps(resolved.source_claims, ensure_ascii=False),
                    poi_id,
                    plan.id,
                ),
            )
        if not grounded and not self._poi_grounding_rate_limited:
            warnings.append(
                "Agent 生成的地点尚未被高德地图确认，地图 POI 与路线暂不能显示；请稍后重试或从地图搜索选择真实地点。"
            )
        return grounded, warnings

    def _ordered_grounding_targets(self, plan_id: str, targets: list[POI]) -> list[POI]:
        order_rows = self.db.execute(
            """
            SELECT s.poi_id
            FROM itinerary_segments s
            JOIN itinerary_days d ON d.id = s.day_id
            WHERE s.plan_id = ? AND s.kind IN ('visit', 'activity')
            ORDER BY d.day_number ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        order = {row["poi_id"]: index for index, row in enumerate(order_rows)}
        return sorted(targets, key=lambda poi: order.get(poi.id, 9999))

    def _resolve_functional_poi_near_context(
        self,
        plan: ItineraryPlan,
        poi: POI,
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
        warnings: list[str],
    ) -> Optional[POI]:
        center = self._functional_context_center(plan.id, poi.id, pois_by_id, resolved_by_id)
        if center is None:
            warnings.append(f"功能型地点「{poi.name}」缺少前后已确认地图地点，暂不直接写成最终 POI。")
            return None
        intent = self._plan_poi_intent(plan.city, poi.name, plan=plan, original=poi)
        candidates, provider_state = self._collect_poi_candidates(
            intent,
            plan=plan,
            original=poi,
            pois_by_id=pois_by_id,
            resolved_by_id=resolved_by_id,
            warnings=warnings,
        )
        if provider_state == "rate_limited":
            return None
        candidate = self._select_ranked_candidate(
            intent,
            candidates,
            plan=plan,
            original=poi,
            pois_by_id=pois_by_id,
            resolved_by_id=resolved_by_id,
        )
        if candidate is None:
            warnings.append(f"功能型地点「{poi.name}」附近未找到可用高德候选。")
            return None
        resolved = self._poi_from_amap_response(
            candidate,
            city=plan.city,
            source_note=f"已结合前后行程把功能型需求「{poi.name}」自动补全为高德具体地点「{candidate.name}」。来源：高德地图",
            confidence=max(candidate.confidence, 0.86),
        )
        warnings.append(f"功能型地点「{poi.name}」已结合前后行程自动补全为「{resolved.name}」。")
        return resolved

    def _functional_context_center(
        self,
        plan_id: str,
        poi_id: str,
        pois_by_id: dict[str, POI],
        resolved_by_id: dict[str, POI],
    ) -> Optional[POI]:
        rows = self.db.execute(
            """
            SELECT s.id, s.day_id, s.segment_order, s.poi_id
            FROM itinerary_segments s
            JOIN itinerary_days d ON d.id = s.day_id
            WHERE s.plan_id = ? AND s.kind IN ('visit', 'activity')
            ORDER BY d.day_number ASC, s.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        current_index = next((index for index, row in enumerate(rows) if row["poi_id"] == poi_id), -1)
        if current_index < 0:
            return None
        current_day_id = rows[current_index]["day_id"]
        neighbor_indices = [current_index - 1, current_index + 1]
        for index in neighbor_indices:
            if index < 0 or index >= len(rows) or rows[index]["day_id"] != current_day_id:
                continue
            neighbor_poi_id = rows[index]["poi_id"]
            candidate = resolved_by_id.get(neighbor_poi_id) or pois_by_id.get(neighbor_poi_id)
            if candidate is not None and self._has_routeable_amap_anchor(candidate):
                return candidate
        return None

    def _functional_nearby_query(self, name: str) -> tuple[str, str]:
        intent = self._poi_intent_type(name, POI_SPECIFICITY_FUNCTIONAL)
        if intent == "dining":
            return "餐厅", "food"
        if intent == "shopping":
            return "购物", "shopping"
        if intent == "rest":
            return "咖啡", "food"
        return "景点", "scenic"

    def _is_auto_concrete_resolution(self, poi: POI) -> bool:
        return "自动细化" in str(poi.source_note or "") or "自动补全" in str(poi.source_note or "")

    def _must_keep_pending_agent_anchor(self, poi: POI) -> bool:
        try:
            confidence = float(poi.confidence or 0)
        except (TypeError, ValueError):
            return True
        return confidence < 0.8

    def _draft_anchor_confidence(self, original: POI) -> float:
        try:
            confidence = float(original.confidence or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return min(0.45, confidence if confidence > 0 else 0.4)

    def _agent_text_timeline_anchor(
        self, original_name: str, resolved: POI, confidence: float, specificity: str = POI_SPECIFICITY_EXACT
    ) -> POI:
        confidence_percent = round(confidence * 100)
        return POI(
            id=resolved.id,
            amap_id=resolved.amap_id,
            name=original_name,
            city=resolved.city,
            category=resolved.category,
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            photo_url=resolved.photo_url,
            source=AGENT_TEXT_TIMELINE_SOURCE,
            confidence=confidence,
            type=resolved.type,
            district=resolved.district,
            address=resolved.address,
            source_note=(
                "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。"
                + f"POI意图：{specificity}；"
                + f"地图锚点：{resolved.name}；"
                + AGENT_TEXT_TIMELINE_GROUNDING_NOTE.format(confidence_percent=confidence_percent)
            ),
            source_url=resolved.source_url,
            photos=resolved.photos,
        )

    def _is_verifier_grade_amap_poi(self, poi: POI) -> bool:
        if poi.source != AMAP_PLACE_SOURCE or not poi.amap_id:
            return False
        if self.poi_trust_policy.is_mock_or_synthetic_poi_values(
            source=poi.source,
            amap_id=poi.amap_id,
            source_note=poi.source_note,
            name=poi.name,
            intent_type="meal" if poi.category == "food" else "",
        ):
            return False
        if poi.latitude is None or poi.longitude is None:
            return False
        try:
            latitude = float(poi.latitude)
            longitude = float(poi.longitude)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and (latitude != 0 or longitude != 0)
            and float(poi.confidence or 0) >= 0.8
        )

    def _has_routeable_amap_anchor(self, poi: POI) -> bool:
        if not poi.amap_id or poi.latitude is None or poi.longitude is None:
            return False
        if self.poi_trust_policy.is_mock_or_synthetic_poi_values(
            source=poi.source,
            amap_id=poi.amap_id,
            source_note=poi.source_note,
            name=poi.name,
            intent_type="meal" if poi.category == "food" else "",
        ):
            return False
        try:
            latitude = float(poi.latitude)
            longitude = float(poi.longitude)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and (latitude != 0 or longitude != 0)
        )

    def _poi_has_required_visit_segment(self, plan_id: str, poi_id: str) -> bool:
        row = self.db.execute(
            """
            SELECT 1
            FROM itinerary_segments
            WHERE plan_id = ? AND poi_id = ? AND kind IN ('visit', 'activity')
            LIMIT 1
            """,
            (plan_id, poi_id),
        ).fetchone()
        return row is not None

    def _is_route_anchor_segment(
        self,
        segment: ItinerarySegment,
        poi: Optional[POI] = None,
        excluded_kinds: Optional[set[str]] = None,
    ) -> bool:
        excluded = excluded_kinds or set()
        if segment.kind in excluded:
            return False
        if self._is_pending_route_anchor_segment(segment):
            return False
        if segment.kind in {"visit", "activity"}:
            return True
        if segment.kind == "park":
            return self._is_grounded_park_route_anchor(segment.semantic_metadata, poi, notes=segment.notes)
        metadata = self._poi_grounding_metadata(poi) if poi is not None else None
        return self._is_route_anchor_kind(segment.kind, metadata)

    def _is_grounded_park_route_anchor_from_row(
        self, row: sqlite3.Row, *, name_key: str = "name", category_key: str = "category"
    ) -> bool:
        try:
            semantic = json.loads(row["semantic_metadata_json"] or "{}")
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        poi = POI(
            id=str(row["poi_id"] or ""),
            name=row[name_key],
            city="",
            category=row[category_key],
            source=row["source"],
            source_note=row["source_note"],
            amap_id=row["amap_id"],
            longitude=row["longitude"],
            latitude=row["latitude"],
            confidence=row["confidence"],
        )
        return self._is_grounded_park_route_anchor(semantic, poi, notes=row["notes"])

    def _route_consumer_segments(
        self, segments: list[ItinerarySegment], pois: list[POI]
    ) -> list[ItinerarySegment]:
        """Project only admitted parks for the legacy route consumer.

        These copies never persist. Keep rejected segments as non-anchors so
        an empty segment list cannot activate RouteService's POI-only fallback.
        Other kinds retain their existing admission and feature-flag behavior.
        """
        pois_by_id = {poi.id: poi for poi in pois}
        projected = []
        for segment in segments:
            if segment.kind != "park":
                projected.append(segment)
                continue
            admitted = self._is_route_anchor_segment(segment, pois_by_id.get(segment.poi_id))
            projected.append(
                replace(
                    segment,
                    kind="activity" if admitted else "note",
                    semantic_metadata=segment.semantic_metadata if admitted else {},
                )
            )
        return projected

    def _is_grounded_park_route_anchor(self, semantic_metadata: object, poi: Optional[POI], *, notes: str = "") -> bool:
        """Admit a persisted park through the existing semantic route policy.

        The kind alone grants no routing eligibility. Keep legacy visit/activity
        and meal behavior separate, while requiring explicit semantic admission
        and canonical, grounded Provider identity for this additional kind.
        """
        if (
            not isinstance(semantic_metadata, dict)
            or semantic_metadata.get("routeAnchor") is not True
            or poi is None
            or poi.source != AMAP_PLACE_SOURCE
            or not re.fullmatch(r"B[0-9A-Z]{8,31}", str(poi.amap_id or ""))
        ):
            return False
        grounding = self._poi_grounding_metadata(poi)
        if not grounding.get("routeable") or not grounding.get("mapReady") or grounding.get("needsConcretePoi"):
            return False
        return RouteService(map_provider_key="")._is_route_anchor_segment(
            SimpleNamespace(kind="park", semantic_metadata=semantic_metadata, notes=notes),
            poi,
            allow_semantic_route_anchor=True,
        )

    def _is_pending_route_anchor_segment(self, segment: ItinerarySegment) -> bool:
        notes = str(segment.notes or "")
        return any(
            token in notes
            for token in (
                "groundingStatus：waiting_for_poi_grounding",
                "groundingStatus: waiting_for_poi_grounding",
                "groundingStatus：provider_rate_limited",
                "groundingStatus: provider_rate_limited",
            )
        )

    def _is_route_anchor_kind(self, kind: object, metadata: Optional[dict]) -> bool:
        normalized_kind = str(kind or "")
        if normalized_kind in {"visit", "activity"}:
            return True
        if normalized_kind != "meal" or metadata is None:
            return False
        return bool(metadata.get("routeable")) and metadata.get("groundingStatus") not in {
            "optional_waiting",
            "not_required",
            "draft_only",
            "waiting_for_poi_grounding",
            "area_unresolved",
            "provider_rate_limited",
            POI_SPECIFICITY_AREA,
            POI_SPECIFICITY_FUNCTIONAL,
            POI_SPECIFICITY_COMPOSITE,
        }

    def _needs_agent_text_grounding(self, poi: POI) -> bool:
        return (
            poi.source == AGENT_TEXT_TIMELINE_SOURCE or not poi.amap_id or poi.longitude is None or poi.latitude is None
        )

    def _valid_coordinate(self, longitude: object, latitude: object) -> bool:
        try:
            lon = float(longitude)
            lat = float(latitude)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(lon)
            and math.isfinite(lat)
            and -180 <= lon <= 180
            and -90 <= lat <= 90
            and (lon != 0 or lat != 0)
        )

    def _poi_grounding_metadata(self, poi: POI) -> dict:
        return self._poi_grounding_metadata_from_values(
            source=poi.source,
            amap_id=poi.amap_id,
            longitude=poi.longitude,
            latitude=poi.latitude,
            confidence=poi.confidence,
            name=poi.name,
            source_note=poi.source_note,
        )

    def _poi_grounding_metadata_from_values(
        self,
        *,
        source: object,
        amap_id: object,
        longitude: object,
        latitude: object,
        confidence: object,
        name: object,
        source_note: object,
    ) -> dict:
        has_provider_poi_id = bool(amap_id)
        has_coordinates = self._valid_coordinate(longitude, latitude)
        routeable = has_provider_poi_id and has_coordinates
        normalized_source = str(source or "")
        specificity = self._poi_specificity_from_text(name, source_note)
        source_note_text = str(source_note or "")
        intent_type = self._intent_type_from_source_note(source_note_text) or self._candidate_intent_type(
            str(name or ""), specificity
        )
        night_view_placeholder_reason = (
            self.night_view_candidate_policy.placeholder_reject_reason(name, source_note_text)
            if intent_type == "night_view"
            else ""
        )
        untrusted = self.poi_trust_policy.is_mock_or_synthetic_poi_values(
            source=source,
            amap_id=amap_id,
            source_note=source_note,
            name=name,
            intent_type=intent_type,
        )
        if untrusted:
            routeable = False
        grounding_status = "draft_only"
        matched_amap_name = None
        if untrusted:
            grounding_status = "waiting_for_poi_grounding"
        elif (
            "groundingStatus：optional_waiting" in source_note_text
            or "groundingStatus: optional_waiting" in source_note_text
        ):
            grounding_status = "optional_waiting"
        elif "groundingStatus：not_required" in source_note_text or "groundingStatus: not_required" in source_note_text:
            grounding_status = "not_required"
        elif (
            "groundingStatus：waiting_for_poi_grounding" in source_note_text
            or "groundingStatus: waiting_for_poi_grounding" in source_note_text
        ):
            grounding_status = "waiting_for_poi_grounding"
        elif (
            "groundingStatus：provider_rate_limited" in source_note_text
            or "groundingStatus: provider_rate_limited" in source_note_text
        ):
            grounding_status = "provider_rate_limited"
        elif (
            "groundingStatus：area_unresolved" in source_note_text
            or "groundingStatus: area_unresolved" in source_note_text
        ):
            grounding_status = "area_unresolved"
        elif (
            routeable
            and normalized_source == AMAP_PLACE_SOURCE
            and (
                "groundingStatus：user_confirmed" in source_note_text
                or "groundingStatus: user_confirmed" in source_note_text
            )
        ):
            grounding_status = "user_confirmed"
            matched_amap_name = str(name or "") or None
            specificity = POI_SPECIFICITY_EXACT
        elif (
            routeable
            and normalized_source == AMAP_PLACE_SOURCE
            and (
                "groundingStatus：agent_selected_candidate" in source_note_text
                or "groundingStatus: agent_selected_candidate" in source_note_text
            )
        ):
            grounding_status = "agent_selected_candidate"
            matched_amap_name = str(name or "") or None
            specificity = POI_SPECIFICITY_EXACT
        elif routeable and normalized_source == AGENT_TEXT_TIMELINE_SOURCE:
            matched_amap_name = self._matched_amap_name_from_source_note(source_note)
            if specificity == POI_SPECIFICITY_EXACT and self._amap_poi_name_matches(
                str(name or ""), matched_amap_name or str(name or "")
            ):
                grounding_status = "verified_amap"
            elif specificity in {POI_SPECIFICITY_AREA, POI_SPECIFICITY_COMPOSITE}:
                grounding_status = specificity
            else:
                grounding_status = "routeable_anchor"
        elif routeable and normalized_source == AMAP_PLACE_SOURCE and self._safe_float(confidence) >= 0.8:
            grounding_status = "verified_amap"
            matched_amap_name = str(name or "") or None
            specificity = POI_SPECIFICITY_EXACT
        elif specificity == POI_SPECIFICITY_FUNCTIONAL:
            grounding_status = POI_SPECIFICITY_FUNCTIONAL
        elif specificity in {POI_SPECIFICITY_AREA, POI_SPECIFICITY_COMPOSITE}:
            grounding_status = specificity
        if grounding_status == "optional_waiting":
            routeable = False
        night_view_concrete_required = False
        if intent_type == "night_view":
            night_view_concrete_required = bool(
                normalized_source != AMAP_PLACE_SOURCE
                or night_view_placeholder_reason
                or "needsConcretePoi=true" in source_note_text
                or specificity in {POI_SPECIFICITY_AREA, POI_SPECIFICITY_COMPOSITE, POI_SPECIFICITY_FUNCTIONAL}
            )
            if night_view_concrete_required:
                routeable = False
                if night_view_placeholder_reason:
                    matched_amap_name = None
                if night_view_placeholder_reason or grounding_status in {
                    "agent_selected_candidate",
                    "verified_amap",
                    "user_confirmed",
                    "routeable_anchor",
                }:
                    grounding_status = "waiting_for_poi_grounding"
        map_ready = (
            routeable
            and grounding_status
            not in {
                POI_SPECIFICITY_FUNCTIONAL,
                "optional_waiting",
                "provider_rate_limited",
                "waiting_for_poi_grounding",
                "area_unresolved",
            }
            and not night_view_concrete_required
        )
        return {
            "groundingStatus": grounding_status,
            "mapReady": map_ready,
            "routeable": routeable,
            "matchedAmapName": matched_amap_name,
            "poiSpecificity": specificity,
            "intentType": intent_type,
            "needsConcretePoi": night_view_concrete_required
            or grounding_status
            in {
                POI_SPECIFICITY_AREA,
                POI_SPECIFICITY_COMPOSITE,
                POI_SPECIFICITY_FUNCTIONAL,
                "area_unresolved",
                "optional_waiting",
                "provider_rate_limited",
                "waiting_for_poi_grounding",
            },
            "nightViewConcreteRequired": night_view_concrete_required,
            "untrustedMockOrSynthetic": untrusted,
        }

    def _intent_type_from_source_note(self, source_note: object) -> str:
        match = re.search(r"intentType[：:=]\s*([A-Za-z_]+)", str(source_note or ""))
        return match.group(1) if match else ""

    def _matched_amap_name_from_source_note(self, source_note: object) -> Optional[str]:
        text = str(source_note or "")
        match = re.search(r"地图锚点[:：]\s*([^；;。]+)", text)
        if not match:
            return None
        value = match.group(1).strip()
        return value or None

    def _safe_float(self, value: object) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _build_segments(
        self,
        day_id: str,
        pois: list[POI],
        weather: WeatherSignal,
        traffic_signals: list[TrafficCrowdingSignal],
        transport_mode: str = "public_transit",
        segment_cost: float = 30.0,
        note: str = "",
    ) -> list[ItinerarySegment]:
        segments = []
        for index, poi in enumerate(pois[:4], start=1):
            segment = ItinerarySegment(
                id=f"seg_{uuid4().hex[:12]}",
                day_id=day_id,
                segment_order=index,
                kind="activity",
                start_time=f"{8 + index:02d}:30",
                end_time=f"{10 + index:02d}:30",
                poi_id=poi.id,
                transport_mode=transport_mode,
                estimated_cost=segment_cost if poi.category == "attraction" else max(0.0, segment_cost - 12.0),
                notes=f"门票/预约状态待票务查询确认。{note}",
                weather_signal_id=weather.id,
                traffic_crowding_signal_id=traffic_signals[index - 1].id if index - 1 < len(traffic_signals) else None,
            )
            segment.semantic_metadata = self._segment_semantic_metadata(segment, poi)
            segments.append(segment)
        return segments

    def _planned_time_windows(self, pois: list[POI]) -> list[dict[str, str]]:
        return [
            {
                "poiName": poi.name,
                "startTime": f"{8 + index:02d}:30",
                "endTime": f"{10 + index:02d}:30",
            }
            for index, poi in enumerate(pois[:4], start=1)
        ]

    def _risk_summary(self, weather: WeatherSignal, traffic_signals: list[TrafficCrowdingSignal]) -> str:
        if any(signal.crowding_level == "medium" for signal in traffic_signals):
            return f"{weather.daily_summary}；早高峰可能拥挤"
        return weather.daily_summary

    def _persist_plan(
        self,
        plan: ItineraryPlan,
        pois: list[POI],
        day: ItineraryDay,
        segments: list[ItinerarySegment],
        routes: list[RouteOption],
        weather: WeatherSignal,
        traffic_signals: list[TrafficCrowdingSignal],
        poi_risk_alerts: list[POIRiskAlert],
        ticket_results: list[TicketLookupResult],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO itinerary_plans (
                id, user_id, inspiration_set_id, template_type, title, city,
                budget_target, budget_estimate, budget_delta_explanation,
                decision_rationale, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.id,
                plan.user_id,
                plan.inspiration_set_id,
                plan.template_type,
                plan.title,
                plan.city,
                plan.budget_target,
                plan.budget_estimate,
                plan.budget_delta_explanation,
                plan.decision_rationale,
                plan.status,
                plan.created_at.isoformat(),
                plan.updated_at.isoformat(),
            ),
        )
        for poi in pois:
            self._insert_poi(plan.id, poi)
        self._insert_day(day)
        for segment in segments:
            self._insert_segment(plan.id, segment)
        self._insert_weather(plan.id, weather)
        for route in routes:
            self._insert_route(route)
        for signal in traffic_signals:
            self._insert_traffic(signal)
        for alert in poi_risk_alerts:
            self._insert_poi_risk_alert(alert)
        self.ticket_service.persist_results(ticket_results)

    def _insert_poi(self, plan_id: str, poi: POI) -> None:
        self.db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence, amap_id, type, district, address,
                parent_poi_id, source_note, source_url, photos_json,
                provider_type_code, tags_json, source_claims_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                poi.id,
                plan_id,
                poi.name,
                poi.city,
                poi.category,
                poi.latitude,
                poi.longitude,
                poi.photo_url,
                poi.source,
                poi.confidence,
                poi.amap_id,
                poi.type,
                poi.district,
                poi.address,
                poi.parent_poi_id,
                poi.source_note,
                poi.source_url,
                json.dumps(poi.photos, ensure_ascii=False),
                poi.provider_type_code,
                json.dumps(poi.tags, ensure_ascii=False),
                json.dumps(poi.source_claims, ensure_ascii=False),
            ),
        )

    def _insert_day(self, day: ItineraryDay) -> None:
        self.db.execute(
            """
            INSERT INTO itinerary_days (
                id, plan_id, day_number, date, title, weather_summary, risk_summary, total_estimated_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day.id,
                day.plan_id,
                day.day_number,
                day.date,
                day.title,
                day.weather_summary,
                day.risk_summary,
                day.total_estimated_cost,
            ),
        )

    def _insert_segment(self, plan_id: str, segment: ItinerarySegment) -> None:
        self.db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time,
                poi_id, transport_mode, estimated_cost, estimate_metadata_json, semantic_metadata_json, notes, weather_signal_id,
                traffic_crowding_signal_id, ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment.id,
                plan_id,
                segment.day_id,
                segment.segment_order,
                segment.kind,
                segment.start_time,
                segment.end_time,
                segment.poi_id,
                segment.transport_mode,
                segment.estimated_cost,
                json.dumps(segment.estimate_metadata or {}, ensure_ascii=False),
                json.dumps(segment.semantic_metadata or {}, ensure_ascii=False),
                segment.notes,
                segment.weather_signal_id,
                segment.traffic_crowding_signal_id,
                segment.ticket_lookup_result_id,
            ),
        )

    def _insert_route(self, route: RouteOption) -> None:
        self.db.execute(
            """
            INSERT INTO route_options (
                id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
                provider, mode, label, is_selected, sort_order, transport_mode,
                distance_meters, duration_seconds, duration_minutes, cost_amount,
                cost_currency, cost_estimate, crowding_risk, source, polyline_json,
                steps_json, provider_payload_json, error_json, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route.id,
                route.plan_id,
                route.from_segment_id,
                route.to_segment_id,
                route.from_poi_id,
                route.to_poi_id,
                route.provider,
                route.mode,
                route.label,
                1 if route.is_selected else 0,
                route.sort_order,
                route.transport_mode,
                route.distance_meters,
                route.duration_seconds,
                route.duration_minutes,
                route.cost_amount,
                route.cost_currency,
                route.cost_estimate,
                route.crowding_risk,
                route.source,
                json.dumps(route.polyline, ensure_ascii=False),
                json.dumps(route.steps, ensure_ascii=False),
                json.dumps(route.provider_payload, ensure_ascii=False),
                json.dumps(route.error, ensure_ascii=False) if route.error else None,
                route.queried_at.isoformat(),
            ),
        )

    def _insert_weather(self, plan_id: str, weather: WeatherSignal) -> None:
        self.db.execute(
            """
            INSERT INTO weather_signals (
                id, plan_id, city, date, hourly_forecast, daily_summary,
                risk_level, purpose_impact_reason, source, data_status,
                confidence, failure_reason, source_url, user_visible_caveat,
                provider_name, fallback_used, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                weather.id,
                plan_id,
                weather.city,
                weather.date,
                json.dumps(weather.hourly_forecast, ensure_ascii=False),
                weather.daily_summary,
                weather.risk_level,
                weather.purpose_impact_reason,
                weather.source,
                weather.data_status,
                weather.confidence,
                weather.failure_reason,
                weather.source_url,
                weather.user_visible_caveat,
                weather.provider_name,
                1 if weather.fallback_used else 0,
                weather.queried_at.isoformat(),
            ),
        )

    def _insert_traffic(self, signal: TrafficCrowdingSignal) -> None:
        self.db.execute(
            """
            INSERT INTO traffic_crowding_signals (
                id, route_option_id, real_data_available, crowding_level,
                estimated_reason, recommended_departure_adjustment, source, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.id,
                signal.route_option_id,
                1 if signal.real_data_available else 0,
                signal.crowding_level,
                signal.estimated_reason,
                signal.recommended_departure_adjustment,
                signal.source,
                signal.queried_at.isoformat(),
            ),
        )

    def _insert_poi_risk_alert(self, alert: POIRiskAlert) -> None:
        self.db.execute(
            """
            INSERT INTO poi_risk_alerts (
                id, plan_id, segment_id, poi_name, status, summary,
                source_name, source_url, sources_json, confidence,
                failure_reason, user_visible_caveat, queried_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.id,
                alert.plan_id,
                alert.segment_id,
                alert.poi_name,
                alert.status,
                alert.summary,
                alert.source_name,
                alert.source_url,
                json.dumps(alert.sources, ensure_ascii=False),
                alert.confidence,
                alert.failure_reason,
                alert.user_visible_caveat,
                alert.queried_at.isoformat(),
            ),
        )

    def _load_plan(self, plan_id: str) -> ItineraryPlan:
        row = self.db.execute("SELECT * FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Itinerary plan not found")
        return ItineraryPlan(
            id=row["id"],
            user_id=row["user_id"],
            inspiration_set_id=row["inspiration_set_id"],
            template_type=row["template_type"],
            title=row["title"],
            city=row["city"],
            budget_target=row["budget_target"],
            budget_tier=row["budget_tier"],
            budget_estimate=row["budget_estimate"],
            budget_delta_explanation=row["budget_delta_explanation"],
            decision_rationale=row["decision_rationale"],
            status=row["status"],
        )

    def _load_pois(self, plan_id: str) -> list[POI]:
        rows = self.db.execute(
            """
            SELECT p.*
            FROM pois p
            LEFT JOIN itinerary_segments s ON s.poi_id = p.id AND s.plan_id = p.plan_id
            WHERE p.plan_id = ?
            ORDER BY COALESCE(s.segment_order, 999), p.id
            """,
            (plan_id,),
        ).fetchall()
        return [
            POI(
                id=row["id"],
                name=row["name"],
                city=row["city"],
                category=row["category"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                photo_url=row["photo_url"],
                source=row["source"],
                confidence=row["confidence"],
                amap_id=row["amap_id"],
                parent_poi_id=row["parent_poi_id"],
                type=row["type"],
                district=row["district"],
                address=row["address"],
                source_note=row["source_note"],
                source_url=row["source_url"],
                photos=json.loads(row["photos_json"] or "[]"),
                provider_type_code=row["provider_type_code"],
                tags=json.loads(row["tags_json"] or "[]"),
                source_claims=json.loads(row["source_claims_json"] or "[]"),
            )
            for row in rows
        ]

    def _load_days(self, plan_id: str) -> list[ItineraryDay]:
        rows = self.db.execute(
            "SELECT * FROM itinerary_days WHERE plan_id = ? ORDER BY day_number ASC", (plan_id,)
        ).fetchall()
        return [
            ItineraryDay(
                id=row["id"],
                plan_id=row["plan_id"],
                day_number=row["day_number"],
                date=row["date"],
                title=row["title"],
                weather_summary=row["weather_summary"],
                risk_summary=row["risk_summary"],
                total_estimated_cost=row["total_estimated_cost"],
            )
            for row in rows
        ]

    def _load_segments(self, plan_id: str) -> list[ItinerarySegment]:
        rows = self.db.execute(
            """
            SELECT itinerary_segments.*
            FROM itinerary_segments
            JOIN itinerary_days ON itinerary_days.id = itinerary_segments.day_id
            WHERE itinerary_segments.plan_id = ?
            ORDER BY itinerary_days.day_number ASC, itinerary_segments.segment_order ASC
            """,
            (plan_id,),
        ).fetchall()
        return [
            ItinerarySegment(
                id=row["id"],
                day_id=row["day_id"],
                segment_order=row["segment_order"],
                kind=row["kind"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                poi_id=row["poi_id"],
                transport_mode=row["transport_mode"],
                estimated_cost=row["estimated_cost"],
                notes=row["notes"],
                estimate_metadata=json.loads(row["estimate_metadata_json"] or "{}"),
                semantic_metadata=json.loads(row["semantic_metadata_json"] or "{}"),
                weather_signal_id=row["weather_signal_id"],
                traffic_crowding_signal_id=row["traffic_crowding_signal_id"],
                ticket_lookup_result_id=row["ticket_lookup_result_id"],
            )
            for row in rows
        ]

    def _segment_semantic_metadata(self, segment: ItinerarySegment, poi) -> dict:
        metadata = dict(segment.semantic_metadata or {})
        estimate = segment.estimate_metadata if isinstance(segment.estimate_metadata, dict) else {}
        duration = estimate.get("duration") if isinstance(estimate.get("duration"), dict) else {}
        if not metadata.get("intentType"):
            if segment.kind in {"meal", "rest"}:
                metadata["intentType"] = segment.kind
            else:
                provider_identity = " ".join(
                    str(value or "")
                    for value in (
                        getattr(poi, "name", ""),
                        getattr(poi, "type", ""),
                        getattr(poi, "category", ""),
                    )
                )
                metadata["intentType"] = self._candidate_intent_type(provider_identity, "exact_entity")
        metadata.setdefault("intentSlotId", None)
        metadata.setdefault("rawNeed", getattr(poi, "name", ""))
        trusted_amap = bool(
            getattr(poi, "source", "") == AMAP_PLACE_SOURCE
            and getattr(poi, "amap_id", None)
            and getattr(poi, "longitude", None) is not None
            and getattr(poi, "latitude", None) is not None
        )
        metadata.setdefault("groundingStatus", "verified_amap" if trusted_amap else "draft_only")
        metadata.setdefault("routeAnchor", trusted_amap)
        metadata.setdefault("requirementLevel", "optional")
        metadata.setdefault("required", False)
        metadata.setdefault("userLocked", bool(duration.get("userLocked")))
        metadata.setdefault("aliases", [getattr(poi, "name", "")] if getattr(poi, "name", "") else [])
        return metadata

    def _load_routes(self, plan_id: str) -> list[RouteOption]:
        rows = self.db.execute(
            """
            SELECT route_options.*
            FROM route_options
            LEFT JOIN itinerary_segments from_segment ON from_segment.id = route_options.from_segment_id
            LEFT JOIN itinerary_segments to_segment ON to_segment.id = route_options.to_segment_id
            LEFT JOIN itinerary_days from_day ON from_day.id = from_segment.day_id
            WHERE route_options.plan_id = ?
              AND (route_options.from_segment_id IS NULL OR from_segment.id IS NOT NULL)
              AND (route_options.to_segment_id IS NULL OR to_segment.id IS NOT NULL)
            ORDER BY
                COALESCE(from_day.day_number, 9999) ASC,
                COALESCE(from_segment.segment_order, 9999) ASC,
                route_options.sort_order ASC,
                route_options.id ASC
            """,
            (plan_id,),
        ).fetchall()
        return [
            RouteOption(
                id=row["id"],
                plan_id=row["plan_id"],
                from_segment_id=row["from_segment_id"],
                to_segment_id=row["to_segment_id"],
                from_poi_id=row["from_poi_id"],
                to_poi_id=row["to_poi_id"],
                provider=row["provider"] or row["source"],
                mode=row["mode"] or row["transport_mode"],
                label=row["label"],
                is_selected=bool(row["is_selected"]),
                sort_order=row["sort_order"],
                distance_meters=row["distance_meters"],
                duration_seconds=row["duration_seconds"] or int(row["duration_minutes"] or 0) * 60,
                cost_amount=row["cost_amount"] if row["cost_amount"] is not None else row["cost_estimate"],
                cost_currency=row["cost_currency"],
                polyline=json.loads(row["polyline_json"] or "[]"),
                steps=json.loads(row["steps_json"] or "[]"),
                provider_payload=json.loads(row["provider_payload_json"] or "{}"),
                error=json.loads(row["error_json"]) if row["error_json"] else None,
                crowding_risk=row["crowding_risk"],
                queried_at=self._parse_datetime(row["queried_at"]),
            )
            for row in rows
        ]

    def _load_weather(self, plan_id: str) -> list[WeatherSignal]:
        rows = self.db.execute("SELECT * FROM weather_signals WHERE plan_id = ? ORDER BY id ASC", (plan_id,)).fetchall()
        return [
            WeatherSignal(
                id=row["id"],
                city=row["city"],
                date=row["date"],
                hourly_forecast=json.loads(row["hourly_forecast"]),
                daily_summary=row["daily_summary"],
                risk_level=row["risk_level"],
                purpose_impact_reason=row["purpose_impact_reason"],
                source=row["source"],
                data_status=row["data_status"],
                confidence=row["confidence"],
                failure_reason=row["failure_reason"],
                source_url=row["source_url"],
                user_visible_caveat=row["user_visible_caveat"],
                provider_name=row["provider_name"],
                fallback_used=bool(row["fallback_used"]),
                queried_at=self._parse_datetime(row["queried_at"]),
            )
            for row in rows
        ]

    def _load_traffic(self, route_ids: list[str]) -> list[TrafficCrowdingSignal]:
        if not route_ids:
            return []
        placeholders = ",".join("?" for _ in route_ids)
        rows = self.db.execute(
            f"SELECT * FROM traffic_crowding_signals WHERE route_option_id IN ({placeholders}) ORDER BY id ASC",
            route_ids,
        ).fetchall()
        return [
            TrafficCrowdingSignal(
                id=row["id"],
                route_option_id=row["route_option_id"],
                real_data_available=bool(row["real_data_available"]),
                crowding_level=row["crowding_level"],
                estimated_reason=row["estimated_reason"],
                recommended_departure_adjustment=row["recommended_departure_adjustment"],
                source=row["source"],
                queried_at=self._parse_datetime(row["queried_at"]),
            )
            for row in rows
        ]

    def _load_poi_risk_alerts(self, plan_id: str) -> list[POIRiskAlert]:
        rows = self.db.execute(
            "SELECT * FROM poi_risk_alerts WHERE plan_id = ? ORDER BY queried_at ASC, id ASC",
            (plan_id,),
        ).fetchall()
        return [
            POIRiskAlert(
                id=row["id"],
                plan_id=row["plan_id"],
                segment_id=row["segment_id"],
                poi_name=row["poi_name"],
                status=row["status"],
                summary=row["summary"],
                source_name=row["source_name"],
                source_url=row["source_url"],
                sources=json.loads(row["sources_json"] or "[]"),
                confidence=row["confidence"],
                failure_reason=row["failure_reason"],
                user_visible_caveat=row["user_visible_caveat"],
                queried_at=self._parse_datetime(row["queried_at"]),
            )
            for row in rows
        ]

    def _load_tickets(self, plan_id: str) -> list[TicketLookupResult]:
        rows = self.db.execute(
            """
            SELECT t.*
            FROM ticket_lookup_results t
            JOIN itinerary_segments s ON s.id = t.segment_id
            WHERE s.plan_id = ?
            ORDER BY s.segment_order ASC,
                CASE t.credibility_rank
                    WHEN 'official' THEN 0
                    WHEN 'aggregator' THEN 1
                    WHEN 'search' THEN 2
                    WHEN 'mock' THEN 3
                    ELSE 4
                END
            """,
            (plan_id,),
        ).fetchall()
        return [
            TicketLookupResult(
                id=row["id"],
                segment_id=row["segment_id"],
                ticket_type=row["ticket_type"],
                status=row["status"],
                price_estimate=row["price_estimate"],
                booking_url=row["booking_url"],
                source_name=row["source_name"],
                source_url=row["source_url"],
                credibility_rank=row["credibility_rank"],
                caveat=row["caveat"],
                provider_name=row["provider_name"],
                fallback_used=bool(row["fallback_used"]),
                confidence=row["confidence"],
                provider_failure_reason=row["provider_failure_reason"],
                queried_at=self._parse_datetime(row["queried_at"]),
            )
            for row in rows
        ]

    def _to_response(
        self,
        plan: ItineraryPlan,
        days: list[ItineraryDay],
        segments: list[ItinerarySegment],
        pois: list[POI],
        routes: list[RouteOption],
        weather: list[WeatherSignal],
        traffic: list[TrafficCrowdingSignal],
        poi_risk_alerts: list[POIRiskAlert],
        tickets: list[TicketLookupResult],
        route_warnings: list[str],
    ) -> ItineraryPlanResponse:
        pois_by_id = {poi.id: poi for poi in pois}
        pending_slots_by_day = self._active_portfolio_pending_slots(plan.id)
        response = ItineraryPlanResponse(
            id=plan.id,
            title=plan.title,
            city=plan.city,
            template_type=plan.template_type,
            budget_target=plan.budget_target,
            budget_tier=plan.budget_tier,
            budget_estimate=plan.budget_estimate,
            budget_breakdown=self._budget_breakdown(plan, segments, routes, tickets),
            route_coverage=self._route_coverage_contract(plan.id, segments, pois),
            schedule_diagnostics=self._schedule_diagnostics_contract(segments),
            online_enrichment=self._online_enrichment_contract(days, weather, poi_risk_alerts, tickets),
            budget_delta_explanation=plan.budget_delta_explanation,
            decision_rationale=plan.decision_rationale,
            status=plan.status,
            days=[
                ItineraryDayResponse(
                    id=day.id,
                    day_number=day.day_number,
                    title=day.title,
                    date=day.date,
                    weather_summary=day.weather_summary,
                    risk_summary=day.risk_summary,
                    total_estimated_cost=day.total_estimated_cost,
                    segments=[
                        ItinerarySegmentResponse(
                            id=segment.id,
                            start_time=segment.start_time,
                            end_time=segment.end_time,
                            kind=segment.kind,
                            poi=self._poi_response(pois_by_id[segment.poi_id]),
                            transport_mode=segment.transport_mode,
                            estimated_cost=segment.estimated_cost,
                            estimate_metadata=segment.estimate_metadata,
                            semantic_metadata=self._segment_semantic_metadata(
                                segment, self._poi_response(pois_by_id[segment.poi_id])
                            ),
                            intent_type=self._segment_semantic_metadata(
                                segment, self._poi_response(pois_by_id[segment.poi_id])
                            ).get("intentType"),
                            intent_slot_id=self._segment_semantic_metadata(
                                segment, self._poi_response(pois_by_id[segment.poi_id])
                            ).get("intentSlotId"),
                            raw_need=str(
                                self._segment_semantic_metadata(
                                    segment, self._poi_response(pois_by_id[segment.poi_id])
                                ).get("rawNeed")
                                or ""
                            ),
                            grounding_status=str(
                                self._segment_semantic_metadata(
                                    segment, self._poi_response(pois_by_id[segment.poi_id])
                                ).get("groundingStatus")
                                or "draft_only"
                            ),
                            route_anchor=bool(
                                self._segment_semantic_metadata(
                                    segment, self._poi_response(pois_by_id[segment.poi_id])
                                ).get("routeAnchor")
                            ),
                            required=bool(
                                self._segment_semantic_metadata(
                                    segment, self._poi_response(pois_by_id[segment.poi_id])
                                ).get("required")
                            ),
                            user_locked=bool(
                                self._segment_semantic_metadata(
                                    segment, self._poi_response(pois_by_id[segment.poi_id])
                                ).get("userLocked")
                            ),
                            notes=segment.notes,
                        )
                        for segment in segments
                        if segment.day_id == day.id
                    ],
                    pending_slots=[
                        PendingTimelineSlotResponse.model_validate(item)
                        for item in pending_slots_by_day.get(day.day_number, [])
                    ],
                )
                for day in days
            ],
            route_options=[self._route_response(route) for route in routes],
            weather_signals=[self._weather_response(signal) for signal in weather],
            traffic_crowding_signals=[self._traffic_response(signal) for signal in traffic],
            poi_risk_alerts=[self._poi_risk_response(alert) for alert in poi_risk_alerts],
            ticket_lookup_results=[self._ticket_response(result) for result in tickets],
            visit_facts_by_segment=self._load_visit_facts(plan.id),
            route_warnings=route_warnings,
        )
        feasibility_report = FeasibilityService().evaluate(response)
        response.feasibility_report = feasibility_report
        response.local_replan_suggestions = feasibility_report.local_replan_suggestions
        return response

    def _load_visit_facts(self, plan_id: str) -> dict[str, dict[str, Any]]:
        from src.services.segment_visit_facts_service import SegmentVisitFactsService

        return SegmentVisitFactsService(self.db).load_for_plan(plan_id)

    def _active_portfolio_pending_slots(
        self,
        plan_id: str,
    ) -> dict[int, list[dict[str, Any]]]:
        row = self.db.execute(
            """
            SELECT v.snapshot_json
            FROM conversation_sessions s
            JOIN itinerary_versions v
              ON v.id = s.active_version_id
             AND v.session_id = s.id
             AND v.plan_id = s.active_plan_id
            WHERE s.active_plan_id = ?
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(snapshot.get("portfolioPartialTimeline"), dict):
            return {}
        by_day: dict[int, list[dict[str, Any]]] = {}
        for item in snapshot.get("portfolioPendingSlots") or []:
            if not isinstance(item, dict):
                continue
            day_number = int(item.get("dayNumber") or 0)
            if day_number < 1:
                continue
            by_day.setdefault(day_number, []).append(dict(item))
        for values in by_day.values():
            values.sort(
                key=lambda item: (
                    str(item.get("startTime") or item.get("timeWindow") or ""),
                    str(item.get("planningSlotId") or ""),
                )
            )
        return by_day

    def _route_coverage_contract(
        self,
        plan_id: str,
        segments: list[ItinerarySegment],
        pois: list[POI],
    ) -> RouteCoverageResponse:
        coverage = self._route_coverage_summary(plan_id, segments, pois)
        pois_by_id = {poi.id: poi for poi in pois}
        unresolved = 0
        for segment in segments:
            poi = pois_by_id.get(segment.poi_id)
            if poi is None:
                unresolved += 1
                continue
            metadata = self._poi_grounding_metadata(poi)
            notes = f"{segment.notes or ''} {poi.source_note or ''}"
            if bool(metadata.get("needsConcretePoi")) or re.search(
                r"(waiting_for_poi_grounding|pendingMeal=true|provider_rate_limited|area_unresolved)",
                notes,
            ):
                unresolved += 1
        required = int(coverage.get("requiredLegCount") or 0)
        covered = int(coverage.get("coveredLegCount") or 0)
        if unresolved:
            door_to_door = "waiting_for_poi_grounding"
        elif required and covered < required:
            door_to_door = "partial"
        else:
            door_to_door = "ready" if required else "not_required"
        return RouteCoverageResponse(
            grounded_anchor_required=required,
            grounded_anchor_covered=covered,
            unresolved_slot_count=unresolved,
            complete_door_to_door_status=door_to_door,
        )

    def _schedule_diagnostics_contract(self, segments: list[ItinerarySegment]) -> dict[str, Any]:
        by_day: dict[str, list[ItinerarySegment]] = {}
        for segment in segments:
            by_day.setdefault(segment.day_id, []).append(segment)
        chronology = 0
        overlap_count = 0
        overlap_minutes = 0
        user_locked_conflicts: set[str] = set()
        provisional = 0
        automatic_allowance = 0
        explicit_buffer = 0
        for day_segments in by_day.values():
            intervals: list[tuple[int, int, ItinerarySegment, bool]] = []
            previous_start: Optional[int] = None
            for segment in day_segments:
                start = self._minutes(segment.start_time)
                end = self._minutes(segment.end_time)
                metadata = segment.estimate_metadata if isinstance(segment.estimate_metadata, dict) else {}
                duration = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
                schedule = metadata.get("schedule") if isinstance(metadata.get("schedule"), dict) else {}
                locked = bool(duration.get("userLocked"))
                if str(schedule.get("status") or "") == "provisional_missing_route":
                    provisional += 1
                automatic_allowance += max(0, int(schedule.get("routeBufferMinutes") or 0))
                if segment.kind in {"buffer", "travel_buffer"}:
                    explicit_buffer += max(0, end - start)
                if previous_start is not None and start < previous_start:
                    chronology += 1
                previous_start = start
                intervals.append((start, end, segment, locked))
            for left_index, (left_start, left_end, left, left_locked) in enumerate(intervals):
                for right_start, right_end, right, right_locked in intervals[left_index + 1 :]:
                    overlap = min(left_end, right_end) - max(left_start, right_start)
                    if overlap <= 0:
                        continue
                    overlap_count += 1
                    if left_locked:
                        user_locked_conflicts.add(left.id)
                    if right_locked:
                        user_locked_conflicts.add(right.id)
            events: dict[int, int] = {}
            for start, end, _segment, _locked in intervals:
                events[start] = events.get(start, 0) + 1
                events[end] = events.get(end, 0) - 1
            active = 0
            previous_time: Optional[int] = None
            for event_time in sorted(events):
                if previous_time is not None and active > 1:
                    overlap_minutes += event_time - previous_time
                active += events[event_time]
                previous_time = event_time
        status = "conflict" if chronology or overlap_count else "provisional" if provisional else "executable"
        return {
            "status": status,
            "chronologyViolationCount": chronology,
            "overlapCount": overlap_count,
            "overlapMinutes": overlap_minutes,
            "userLockedConflictCount": len(user_locked_conflicts),
            "provisionalSegmentCount": provisional,
            "automaticArrivalAllowanceMinutes": automatic_allowance,
            "explicitBufferMinutes": explicit_buffer,
        }

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)

    def _online_enrichment_contract(
        self,
        days: list[ItineraryDay],
        weather: list[WeatherSignal],
        risk_alerts: list[POIRiskAlert],
        tickets: list[TicketLookupResult],
    ) -> OnlineEnrichmentStatusResponse:
        dates = [str(day.date) for day in days if day.date]
        resolver = TripDateResolver()
        resolved = resolver.resolve(
            f"{dates[0]} 到 {dates[-1]}" if dates else "",
            source="itinerary.days",
        )
        availability = resolver.weather_availability_contract(resolved)
        latest_weather = max(weather, key=lambda item: item.queried_at) if weather else None
        if latest_weather is not None:
            weather_status = str(latest_weather.data_status or "")
            if weather_status == "forecast_not_supported_yet":
                weather_status = "outside_forecast_window"
            elif weather_status == "outside_forecast_window":
                pass
            elif latest_weather.failure_reason and not latest_weather.confidence:
                weather_status = "unavailable"
            elif weather_status not in {"outside_forecast_window", "stale", "unavailable"}:
                weather_status = "available"
        else:
            weather_status = str(availability["status"])

        usable_risk = [item for item in risk_alerts if not item.failure_reason and item.confidence > 0]
        risk_status = (
            "pending"
            if not risk_alerts
            else "checked"
            if len(usable_risk) == len(risk_alerts)
            else "partial"
            if usable_risk
            else "unavailable"
        )
        usable_tickets = [item for item in tickets if not item.provider_failure_reason and bool(item.source_url)]
        reservation_status = (
            "pending"
            if not tickets
            else "checked"
            if len(usable_tickets) == len(tickets)
            else "partial"
            if usable_tickets
            else "unavailable"
        )
        return OnlineEnrichmentStatusResponse(
            weather=EnrichmentDimensionStatusResponse(
                status=weather_status,
                trip_start_date=availability.get("tripStartDate"),
                forecast_available_from=availability.get("forecastAvailableFrom"),
                queried_at=latest_weather.queried_at if latest_weather else None,
                next_action=str(availability.get("nextAction") or "refresh_now"),
                source=latest_weather.source if latest_weather else None,
                item_count=len(weather),
            ),
            risk=EnrichmentDimensionStatusResponse(
                status=risk_status,
                trip_start_date=availability.get("tripStartDate"),
                queried_at=max((item.queried_at for item in risk_alerts), default=None),
                next_action="refresh_now" if risk_status != "checked" else "none",
                source=next((item.source_name for item in risk_alerts if item.source_name), None),
                item_count=len(risk_alerts),
            ),
            reservation=EnrichmentDimensionStatusResponse(
                status=reservation_status,
                trip_start_date=availability.get("tripStartDate"),
                queried_at=max((item.queried_at for item in tickets), default=None),
                next_action="refresh_now" if reservation_status != "checked" else "none",
                source=next((item.source_name for item in tickets if item.source_name), None),
                item_count=len(tickets),
            ),
        )

    def _budget_breakdown(
        self,
        plan: ItineraryPlan,
        segments: list[ItinerarySegment],
        routes: list[RouteOption],
        tickets: list[TicketLookupResult],
    ) -> BudgetBreakdownResponse:
        meal_cost = sum(float(segment.estimated_cost or 0) for segment in segments if segment.kind == "meal")
        activity_cost = sum(float(segment.estimated_cost or 0) for segment in segments if segment.kind != "meal")
        transport_cost = sum(
            float(route.cost_amount or 0) for route in routes if route.is_selected and not getattr(route, "error", None)
        )
        provisional_min = transport_cost
        provisional_preferred = transport_cost
        provisional_max = transport_cost
        for segment in segments:
            metadata = segment.estimate_metadata if isinstance(segment.estimate_metadata, dict) else {}
            cost = metadata.get("cost") if isinstance(metadata.get("cost"), dict) else {}
            known = float(segment.estimated_cost or 0)
            provisional_min += float(cost.get("min") if cost.get("min") is not None else known)
            provisional_preferred += float(cost.get("preferred") if cost.get("preferred") is not None else known)
            provisional_max += float(cost.get("max") if cost.get("max") is not None else known)
        known_total = activity_cost + meal_cost + transport_cost
        normalized_budget = BudgetInvariantPolicy.normalize(
            known_total=known_total,
            provisional_min=provisional_min,
            provisional_preferred=provisional_preferred,
            provisional_max=provisional_max,
        )
        unknown_items: list[str] = []
        if not tickets or any(
            str(getattr(ticket, "status", "")) in {"not_checked", "unknown", "unavailable"} for ticket in tickets
        ):
            unknown_items.append("未核验门票/预约相关收费")
        return BudgetBreakdownResponse(
            tier=plan.budget_tier,
            numeric_target=plan.budget_target,
            known_activity_cost=round(activity_cost, 2),
            known_meal_cost=round(meal_cost, 2),
            known_transport_cost=round(transport_cost, 2),
            known_total=normalized_budget.known_total,
            provisional_min=normalized_budget.provisional_min,
            provisional_preferred=normalized_budget.provisional_preferred,
            provisional_max=normalized_budget.provisional_max,
            unknown_items=unknown_items,
            is_complete=not unknown_items,
            invariant_valid=normalized_budget.invariant_valid,
        )

    def _poi_response(self, poi: POI) -> PoiResponse:
        metadata = self._poi_grounding_metadata(poi)
        has_coordinates = self._valid_coordinate(poi.longitude, poi.latitude)
        has_provider_poi_id = bool(poi.amap_id)
        grounding = {
            "status": metadata["groundingStatus"],
            "groundingStatus": metadata["groundingStatus"],
            "mapReady": metadata["mapReady"],
            "routeable": metadata["routeable"],
            "matchedAmapName": metadata["matchedAmapName"],
            "poiSpecificity": metadata["poiSpecificity"],
            "intentType": metadata["intentType"],
            "needsConcretePoi": metadata["needsConcretePoi"],
            "untrustedMockOrSynthetic": metadata.get("untrustedMockOrSynthetic", False),
            "hasProviderPoiId": has_provider_poi_id,
            "hasCoordinates": has_coordinates,
            "needsVerification": metadata["groundingStatus"] != "verified_amap",
        }
        return PoiResponse(
            id=poi.id,
            amap_id=poi.amap_id,
            parent_poi_id=poi.parent_poi_id,
            name=poi.name,
            type=poi.type,
            city=poi.city,
            district=poi.district,
            address=poi.address,
            category=poi.category,
            latitude=poi.latitude,
            longitude=poi.longitude,
            photo_url=poi.photo_url,
            source=poi.source,
            source_note=poi.source_note,
            source_url=poi.source_url,
            confidence=poi.confidence,
            photos=poi.photos,
            provider_type_code=poi.provider_type_code,
            tags=poi.tags,
            source_claims=poi.source_claims,
            grounding_status=metadata["groundingStatus"],
            map_ready=metadata["mapReady"],
            routeable=metadata["routeable"],
            matched_amap_name=metadata["matchedAmapName"],
            poi_specificity=metadata["poiSpecificity"],
            intent_type=metadata["intentType"],
            needs_concrete_poi=metadata["needsConcretePoi"],
            grounding=grounding,
        )

    def _route_response(self, route: RouteOption) -> RouteOptionResponse:
        provider_status = (
            str(route.provider_payload.get("routeStatus") or route.provider_payload.get("status") or "").strip().lower()
        )
        status = route_evidence_status(
            provider_payload=route.provider_payload,
            error=route.error,
            explicit_status=provider_status,
        )
        return RouteOptionResponse(
            id=route.id,
            from_segment_id=route.from_segment_id,
            to_segment_id=route.to_segment_id,
            from_poi_id=route.from_poi_id,
            to_poi_id=route.to_poi_id,
            provider=route.provider,
            mode=route.mode,
            label=route.label,
            is_selected=route.is_selected,
            sort_order=route.sort_order,
            transport_mode=route.transport_mode,
            distance_meters=route.distance_meters,
            duration_seconds=route.duration_seconds,
            duration_minutes=route.duration_minutes,
            cost_amount=route.cost_amount,
            cost_currency=route.cost_currency,
            cost_estimate=route.cost_estimate,
            crowding_risk=route.crowding_risk,
            source=route.source,
            polyline=route.polyline,
            steps=route.steps,
            provider_payload=route.provider_payload,
            error=route.error,
            status=status,
            queried_at=route.queried_at,
        )

    def _weather_response(self, signal: WeatherSignal) -> WeatherSignalResponse:
        return WeatherSignalResponse(
            id=signal.id,
            city=signal.city,
            date=signal.date,
            hourly_forecast=signal.hourly_forecast,
            daily_summary=signal.daily_summary,
            risk_level=signal.risk_level,
            purpose_impact_reason=signal.purpose_impact_reason,
            source=signal.source,
            data_status=signal.data_status,
            confidence=signal.confidence,
            failure_reason=signal.failure_reason,
            source_url=signal.source_url,
            user_visible_caveat=signal.user_visible_caveat,
            provider_name=signal.provider_name,
            fallback_used=signal.fallback_used,
            queried_at=signal.queried_at,
        )

    def _traffic_response(self, signal: TrafficCrowdingSignal) -> TrafficCrowdingSignalResponse:
        return TrafficCrowdingSignalResponse(
            id=signal.id,
            route_option_id=signal.route_option_id,
            real_data_available=signal.real_data_available,
            crowding_level=signal.crowding_level,
            estimated_reason=signal.estimated_reason,
            recommended_departure_adjustment=signal.recommended_departure_adjustment,
            source=signal.source,
            queried_at=signal.queried_at,
        )

    def _poi_risk_response(self, alert: POIRiskAlert) -> POIRiskAlertResponse:
        return POIRiskAlertResponse(
            id=alert.id,
            plan_id=alert.plan_id,
            segment_id=alert.segment_id,
            poi_name=alert.poi_name,
            status=alert.status,
            summary=alert.summary,
            source_name=alert.source_name,
            source_url=alert.source_url,
            sources=alert.sources,
            confidence=alert.confidence,
            failure_reason=alert.failure_reason,
            user_visible_caveat=alert.user_visible_caveat,
            queried_at=alert.queried_at,
        )

    def _ticket_response(self, result: TicketLookupResult) -> TicketLookupResultResponse:
        return TicketLookupResultResponse(
            id=result.id,
            segment_id=result.segment_id,
            ticket_type=result.ticket_type,
            status=result.status,
            price_estimate=result.price_estimate,
            booking_url=result.booking_url,
            source_name=result.source_name,
            source_url=result.source_url,
            credibility_rank=result.credibility_rank,
            queried_at=result.queried_at,
            caveat=result.caveat,
            provider_name=result.provider_name,
            fallback_used=result.fallback_used,
            provider_failure_reason=result.provider_failure_reason,
            confidence=result.confidence,
        )

    def _json_list(self, row: Optional[sqlite3.Row], column: str) -> list:
        if row is None:
            return []
        value = row[column]
        return json.loads(value) if value else []

    def _first_json_value(self, row: Optional[sqlite3.Row], column: str) -> Optional[str]:
        values = self._json_list(row, column)
        return values[0] if values else None

    def _load_preference(self, preference_profile_id: Optional[str]) -> Optional[sqlite3.Row]:
        if not preference_profile_id:
            return None
        return self.db.execute("SELECT * FROM preference_profiles WHERE id = ?", (preference_profile_id,)).fetchone()

    def _risk_context(
        self,
        preference: Optional[sqlite3.Row],
        preference_summary: Optional[str],
        planning_context: Optional[dict],
        date_range: Optional[dict],
    ) -> dict[str, Any]:
        planning_context = planning_context or {}
        preference_card = (
            planning_context.get("preferenceCard") if isinstance(planning_context.get("preferenceCard"), dict) else {}
        )
        context_date_range = (
            planning_context.get("dateRange") if isinstance(planning_context.get("dateRange"), dict) else None
        )
        traveler_types = self._json_list_from_value(preference["traveler_types"]) if preference is not None else []
        context_travelers = preference_card.get("travelerTypes")
        if not traveler_types and isinstance(context_travelers, list):
            traveler_types = [str(item) for item in context_travelers]
        memory_rules = (
            planning_context.get("memoryRules") if isinstance(planning_context.get("memoryRules"), dict) else {}
        )
        risk_rules = memory_rules.get("riskChecks") if isinstance(memory_rules.get("riskChecks"), dict) else {}
        context = {
            "preferenceSummary": preference_summary
            or str(preference_card.get("summaryText") or planning_context.get("currentPreferenceSummary") or ""),
            "partySize": preference["party_size"] if preference is not None else preference_card.get("partySize"),
            "travelerTypes": traveler_types,
            "budgetRange": preference["budget_range"] if preference is not None else preference_card.get("budgetRange"),
            "pacePreference": preference["pace_preference"]
            if preference is not None
            else preference_card.get("pacePreference"),
            "travelDateRange": date_range or context_date_range or planning_context.get("travelDateRange"),
            "tripPurpose": planning_context.get("tripPurpose") or self._infer_trip_purpose(preference_summary or ""),
            "weatherSensitivity": str(
                planning_context.get("weatherSensitivity")
                or preference_card.get("weatherSensitivity")
                or ("高" if risk_rules.get("checkCrowdingWeather") else "")
                or ""
            ),
            "memoryRiskRules": risk_rules,
            "officialSourceFirst": bool(
                risk_rules.get("officialSourceFirst", planning_context.get("officialSourceFirst", False))
            ),
            "riskPriorityTerms": risk_rules.get("priorityTerms") or [],
            "travelerSensitivity": risk_rules.get("travelerSensitivity") or "normal",
        }
        if isinstance(planning_context.get("resolvedTripDates"), dict):
            context["resolvedTripDates"] = planning_context["resolvedTripDates"]
        return context

    def _json_list_from_value(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if not value:
            return []
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return [str(value)]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [str(parsed)]

    def _infer_trip_purpose(self, text: str) -> str:
        markers = []
        if "拍照" in text or "照片" in text:
            markers.append("拍照")
        if "亲子" in text or "孩子" in text:
            markers.append("亲子")
        if "老人" in text:
            markers.append("老人同行")
        if "徒步" in text or "户外" in text:
            markers.append("户外活动")
        return " ".join(markers)

    def _budget_number(self, budget_range: str) -> Optional[float]:
        match = re.search(r"(\d+)", budget_range or "")
        return float(match.group(1)) if match else None

    def _parse_datetime(self, value: str) -> datetime:
        return datetime.fromisoformat(value) if value else datetime.now(timezone.utc)

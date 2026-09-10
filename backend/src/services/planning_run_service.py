import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from src.api.schemas.itineraries import ItineraryPlanResponse
from src.api.schemas.planning import PlanningRunResponse, PlanningToolCallResponse, SourceAssessmentResponse
from src.services.feasibility_service import FeasibilityService
from src.services.source_ranking_service import SourceRankingService


class PlanningRunService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.source_ranking = SourceRankingService()

    def create_run(
        self,
        run_type: str,
        user_input: str = "",
        preference_summary: str = "",
        itinerary: Optional[ItineraryPlanResponse] = None,
        itinerary_version_id: Optional[str] = None,
        understood_requirements: Optional[dict] = None,
        final_summary: str = "",
        tool_calls_override: Optional[list[PlanningToolCallResponse]] = None,
    ) -> PlanningRunResponse:
        feasibility_report = FeasibilityService().evaluate(itinerary, preference_summary) if itinerary is not None else None
        if itinerary is not None and feasibility_report is not None:
            itinerary.feasibility_report = feasibility_report
            itinerary.local_replan_suggestions = feasibility_report.local_replan_suggestions
        source_assessments = self._source_assessments_for_itinerary(itinerary)
        tool_calls = tool_calls_override if tool_calls_override is not None else self._tool_calls_for_itinerary(itinerary, source_assessments)
        run = PlanningRunResponse(
            id=f"run_{uuid4().hex[:12]}",
            run_type=run_type,
            user_input=user_input,
            preference_summary=preference_summary,
            itinerary_plan_id=itinerary.id if itinerary is not None else None,
            itinerary_version_id=itinerary_version_id,
            understood_requirements=understood_requirements or self._understood_requirements(user_input, itinerary, preference_summary),
            constraint_summary=self._constraint_summary(itinerary, preference_summary),
            tool_calls=tool_calls,
            source_assessments=source_assessments,
            feasibility_report=feasibility_report,
            final_summary=final_summary or self._final_summary(run_type, itinerary, feasibility_report),
            created_at=datetime.now(timezone.utc),
        )
        self._persist(run)
        return run

    def _persist(self, run: PlanningRunResponse) -> None:
        self.db.execute(
            """
            INSERT INTO planning_runs (
                id, run_type, user_input, preference_summary, itinerary_plan_id,
                itinerary_version_id, understood_requirements_json,
                constraint_summary_json, tool_calls_json, source_assessments_json,
                feasibility_report_json,
                final_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.run_type,
                run.user_input,
                run.preference_summary,
                run.itinerary_plan_id,
                run.itinerary_version_id,
                json.dumps(run.understood_requirements, ensure_ascii=False, default=str),
                json.dumps(run.constraint_summary, ensure_ascii=False, default=str),
                json.dumps(
                    [self._persisted_tool_call(tool) for tool in run.tool_calls],
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps([source.model_dump(by_alias=True) for source in run.source_assessments], ensure_ascii=False, default=str),
                json.dumps(run.feasibility_report.model_dump(by_alias=True) if run.feasibility_report else None, ensure_ascii=False, default=str),
                run.final_summary,
                run.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _persisted_tool_call(tool: PlanningToolCallResponse) -> dict:
        payload = tool.model_dump(by_alias=True)
        trace_summary = getattr(tool, "_trace_summary", None)
        if isinstance(trace_summary, dict) and trace_summary:
            payload["traceSummary"] = trace_summary
        return payload

    def _tool_calls_for_itinerary(
        self,
        itinerary: Optional[ItineraryPlanResponse],
        source_assessments: Optional[list[SourceAssessmentResponse]] = None,
    ) -> list[PlanningToolCallResponse]:
        now = datetime.now(timezone.utc).isoformat()
        if itinerary is None:
            return [
                PlanningToolCallResponse(
                    id="agent-understanding",
                    toolName="Agent 目标理解",
                    status="completed",
                    providerName="agent-service",
                    queriedAt=now,
                    confidence=0.7,
                    summary="已进入澄清或降级路径，尚未生成可检查行程。",
                )
            ]

        weather = itinerary.weather_signals[0] if itinerary.weather_signals else None
        first_ticket = itinerary.ticket_lookup_results[0] if itinerary.ticket_lookup_results else None
        first_route = itinerary.route_options[0] if itinerary.route_options else None
        first_traffic = itinerary.traffic_crowding_signals[0] if itinerary.traffic_crowding_signals else None
        ticket_conflict = any(source.conflict_detected for source in (source_assessments or []))

        return [
            PlanningToolCallResponse(
                id="agent-understanding",
                toolName="Agent 目标理解",
                status="completed",
                providerName="agent-service",
                queriedAt=now,
                confidence=0.86,
                summary="已提取城市、天数、偏好和当前行程状态。",
            ),
            PlanningToolCallResponse(
                id="map-route",
                toolName="地图/路线能力",
                status="failed" if first_route and first_route.error else "completed" if first_route else "fallback",
                providerName=first_route.provider if first_route else "amap-route-provider",
                sourceName=first_route.source if first_route else "",
                queriedAt=str(first_route.queried_at) if first_route else now,
                confidence=0.82 if first_route and not first_route.error else 0.35,
                fallbackUsed=not bool(first_route) or bool(first_route.error),
                failureReason=str(first_route.error) if first_route and first_route.error else None,
                userVisibleCaveat="" if first_route and not first_route.error else "路线信息缺失或失败，行程仍可查看。",
                summary="已计算 POI 空间关系、路线距离和交通方式。" if first_route else "路线暂未形成，保留可编辑草案。",
            ),
            PlanningToolCallResponse(
                id="amap-weather",
                toolName="高德天气",
                status="fallback" if weather and weather.fallback_used else "completed" if weather else "fallback",
                providerName=weather.provider_name if weather else "amap-weather-provider",
                sourceName=weather.source if weather else "",
                sourceUrl=weather.source_url if weather else None,
                queriedAt=str(weather.queried_at) if weather else now,
                confidence=weather.confidence if weather else 0.35,
                fallbackUsed=weather.fallback_used if weather else True,
                failureReason=weather.failure_reason if weather else "weather signal missing",
                userVisibleCaveat=weather.user_visible_caveat if weather else "天气缺失，需用户确认。",
                summary=weather.daily_summary if weather else "未拿到天气信号。",
            ),
            PlanningToolCallResponse(
                id="web-search-ticket",
                toolName="联网搜索票务/预约",
                status="failed"
                if first_ticket and first_ticket.provider_failure_reason
                else "fallback"
                if first_ticket and first_ticket.fallback_used
                else "completed"
                if first_ticket
                else "fallback",
                providerName=first_ticket.provider_name if first_ticket else "web-search-provider",
                sourceName=first_ticket.source_name if first_ticket else "",
                sourceUrl=first_ticket.source_url if first_ticket else None,
                queriedAt=str(first_ticket.queried_at) if first_ticket else now,
                confidence=first_ticket.confidence if first_ticket else 0.3,
                fallbackUsed=first_ticket.fallback_used if first_ticket else True,
                failureReason=first_ticket.provider_failure_reason if first_ticket else "ticket lookup missing",
                userVisibleCaveat=first_ticket.caveat if first_ticket else "票务/预约缺失，需用户确认。",
                summary=(
                    "已查询票务、预约入口和费用估算，并按来源可信度排序；检测到多来源冲突，建议以官方来源为准。"
                    if ticket_conflict
                    else "已查询票务、预约入口和费用估算，并按来源可信度排序。"
                    if first_ticket
                    else "暂无票务查询结果。"
                ),
            ),
            PlanningToolCallResponse(
                id="traffic-crowding",
                toolName="交通/拥挤提示",
                status="completed" if first_traffic else "fallback",
                providerName="traffic-crowding-service",
                sourceName=first_traffic.source if first_traffic else "",
                queriedAt=str(first_traffic.queried_at) if first_traffic else now,
                confidence=0.76 if first_traffic else 0.35,
                fallbackUsed=not bool(first_traffic),
                userVisibleCaveat="" if first_traffic else "拥挤提示缺失，使用行程草案继续。",
                summary=first_traffic.estimated_reason if first_traffic else "暂无拥挤提示。",
            ),
        ]

    def _source_assessments_for_itinerary(self, itinerary: Optional[ItineraryPlanResponse]) -> list[SourceAssessmentResponse]:
        if itinerary is None or not itinerary.ticket_lookup_results:
            return []
        return [
            SourceAssessmentResponse(
                sourceName=assessment.source_name,
                sourceUrl=assessment.source_url,
                credibilityRank=assessment.credibility_rank,
                credibilityLabel=assessment.credibility_label,
                providerName=assessment.provider_name,
                confidence=assessment.confidence,
                fallbackUsed=assessment.fallback_used,
                conflictDetected=assessment.conflict_detected,
                conflictReason=assessment.conflict_reason,
                recommendation=assessment.recommendation,
            )
            for assessment in self.source_ranking.assess_ticket_sources(itinerary.ticket_lookup_results)[:8]
        ]

    def _understood_requirements(self, user_input: str, itinerary: Optional[ItineraryPlanResponse], preference_summary: str = "") -> dict:
        city = itinerary.city if itinerary is not None else ""
        days = len(itinerary.days) if itinerary is not None else 0
        combined = f"{user_input}\n{preference_summary}"
        fields = {
            "destination": city or "待确认",
            "travelDays": f"{days} 天" if days else self._extract_travel_days(combined),
            "travelDate": self._travel_date_from_itinerary(itinerary) or self._extract_travel_date(combined),
            "budget": self._extract_budget(combined),
            "partySize": self._extract_party_size(combined),
            "transportPreference": self._extract_transport_preference(combined, itinerary),
            "travelPurpose": self._extract_travel_purpose(combined),
        }
        missing = []
        if not city:
            missing.append("destination")
        if fields["travelDays"] == "待确认":
            missing.append("travelDays")
        if fields["travelDate"] == "待确认":
            missing.append("travelDate")
        known_parts = [f"{label}：{value}" for key, label in [
            ("destination", "目的地"),
            ("travelDays", "天数"),
            ("travelDate", "出行日期"),
            ("budget", "预算"),
            ("partySize", "同行人数"),
            ("transportPreference", "交通"),
            ("travelPurpose", "旅行目的"),
        ] for value in [fields[key]] if value != "待确认"]
        return {
            "summary": "；".join([*known_parts, f"用户输入：{user_input or '系统生成'}"]),
            "fields": fields,
            "missingFields": missing,
            "isCompleteEnoughToPlan": not missing,
            "clarificationQuestions": self._clarification_questions(missing),
        }

    def _clarification_questions(self, missing: list[str]) -> list[str]:
        labels = {
            "destination": "你想先去哪个城市或区域？",
            "travelDays": "这次大概玩几天？我可以先按 1-2 天给你起草。",
            "travelDate": "大概哪几天出发？日期会影响天气、开放时间和预约风险。",
        }
        return [labels[item] for item in missing if item in labels]

    def _travel_date_from_itinerary(self, itinerary: Optional[ItineraryPlanResponse]) -> str:
        if itinerary is None:
            return ""
        for day in itinerary.days:
            if day.date:
                return str(day.date)
        return ""

    def _extract_travel_days(self, text: str) -> str:
        match = re.search(r"(\d+)\s*[天日]", text)
        if match:
            return f"{match.group(1)} 天"
        for keyword, value in {"半天": "半天", "一天": "1 天", "两天": "2 天", "三天": "3 天"}.items():
            if keyword in text:
                return value
        return "待确认"

    def _extract_travel_date(self, text: str) -> str:
        for pattern in [
            r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?",
            r"\d{1,2}\s*月\s*(上旬|中旬|下旬|\d{1,2}\s*日?)",
            r"(今天|明天|后天|本周末|周末|下周|暑假|寒假|春节|清明|五一|端午|中秋|国庆)",
        ]:
            match = re.search(pattern, text)
            if match:
                return match.group(0).replace(" ", "")
        return "待确认"

    def _extract_budget(self, text: str) -> str:
        for pattern in [
            r"(预算|人均|总预算|大概|约)\s*[¥￥]?\s*(\d{2,6})\s*(元|块|rmb|RMB)?",
            r"[¥￥]\s*(\d{2,6})",
            r"(\d{2,6})\s*(元|块|rmb|RMB)",
        ]:
            match = re.search(pattern, text)
            if not match:
                continue
            amount = next((group for group in reversed(match.groups()) if group and group.isdigit()), "")
            if amount:
                return f"{amount} 元"
        return "待确认"

    def _extract_party_size(self, text: str) -> str:
        match = re.search(r"(\d+)\s*(人|位)", text)
        if match:
            return f"{match.group(1)} 人"
        for keyword, value in {"一个人": "1 人", "独自": "1 人", "两个人": "2 人", "情侣": "2 人", "亲子": "亲子同行", "家庭": "家庭同行", "老人": "含老人同行"}.items():
            if keyword in text:
                return value
        return "待确认"

    def _extract_transport_preference(self, text: str, itinerary: Optional[ItineraryPlanResponse]) -> str:
        options = []
        for keyword, label in [("公共交通", "公共交通"), ("地铁", "地铁"), ("公交", "公交"), ("打车", "打车"), ("自驾", "自驾"), ("步行", "步行")]:
            if keyword in text and label not in options:
                options.append(label)
        return "、".join(options) if options else "待确认"

    def _extract_travel_purpose(self, text: str) -> str:
        purposes = []
        for keyword, label in [("拍照", "拍照打卡"), ("照片", "拍照打卡"), ("亲子", "亲子"), ("老人", "照顾同行节奏"), ("美食", "美食"), ("历史", "历史文化"), ("博物馆", "历史文化"), ("轻松", "轻松不赶路"), ("不赶", "轻松不赶路"), ("户外", "户外体验")]:
            if keyword in text and label not in purposes:
                purposes.append(label)
        return "、".join(purposes) if purposes else "待确认"

    def _constraint_summary(self, itinerary: Optional[ItineraryPlanResponse], preference_summary: str) -> list[dict]:
        constraints = []
        if itinerary is not None:
            constraints.extend([
                {"label": "城市", "value": itinerary.city},
                {"label": "天数", "value": f"{len(itinerary.days)} 天"},
            ])
            if itinerary.route_options:
                constraints.append({"label": "路线", "value": f"{len(itinerary.route_options)} 条路线候选"})
            if itinerary.ticket_lookup_results:
                constraints.append({"label": "票务/预约", "value": f"{len(itinerary.ticket_lookup_results)} 条来源"})
            if itinerary.weather_signals:
                constraints.append({"label": "天气", "value": itinerary.weather_signals[0].daily_summary})
        if preference_summary:
            constraints.append({"label": "偏好摘要", "value": preference_summary})
        return constraints

    def _final_summary(
        self,
        run_type: str,
        itinerary: Optional[ItineraryPlanResponse],
        feasibility_report,
    ) -> str:
        if itinerary is None:
            return "已生成澄清问题或降级提示，当前未覆盖既有行程。"
        risk = feasibility_report.risk_level if feasibility_report else "unknown"
        score = feasibility_report.score if feasibility_report else 0
        return f"{run_type} 已完成：{itinerary.title}，可行性评分 {score}/100，风险等级 {risk}。"

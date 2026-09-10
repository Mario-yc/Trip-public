from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from src.api.schemas.itineraries import ItineraryDayResponse, ItineraryPlanResponse, ItinerarySegmentResponse
from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.api.schemas.planning import (
    FeasibilityIssueResponse,
    FeasibilityReportResponse,
    LocalReplanSuggestionResponse,
)


class FeasibilityService:
    def evaluate(self, plan: ItineraryPlanResponse, preference_summary: str = "") -> FeasibilityReportResponse:
        issues: list[FeasibilityIssueResponse] = []
        for day in plan.days:
            issues.extend(self._day_density_issues(day, preference_summary))
            issues.extend(self._time_issues(day))
        issues.extend(self._route_issues(plan))
        issues.extend(self._ticket_issues(plan))
        issues.extend(self._weather_issues(plan, preference_summary))
        issues.extend(self._crowding_issues(plan))
        issues.extend(self._budget_issues(plan))
        issues.extend(self._preference_issues(plan, preference_summary))

        score = self._score(issues)
        suggestions = [issue.recommendation for issue in issues[:5]]
        replan_suggestions = self._local_replan_suggestions(plan, issues)
        return FeasibilityReportResponse(
            score=score,
            risk_level=self._risk_level(score, issues),
            route_status=self._route_status(plan),
            issues=issues,
            suggestions=suggestions,
            local_replan_suggestions=replan_suggestions,
            preference_alignment=self._preference_alignment(plan, preference_summary, issues),
            checked_at=datetime.now(timezone.utc),
        )

    def _day_density_issues(self, day: ItineraryDayResponse, preference_summary: str) -> list[FeasibilityIssueResponse]:
        max_segments = 3 if "轻松" in preference_summary or "不赶" in preference_summary else 4
        if len(day.segments) <= max_segments:
            return []
        return [
            FeasibilityIssueResponse(
                code="day_density_high",
                severity="medium",
                dimension="schedule_density",
                message=f"Day {day.day_number} 安排了 {len(day.segments)} 个 POI/活动，可能偏赶。",
                recommendation="建议把最后一个活动移到新增日期，或删除低优先级 POI。",
                affected_day_id=day.id,
                affected_segment_id=day.segments[-1].id if day.segments else None,
                evidence=[f"maxRecommended={max_segments}", f"actual={len(day.segments)}"],
            )
        ]

    def _time_issues(self, day: ItineraryDayResponse) -> list[FeasibilityIssueResponse]:
        issues: list[FeasibilityIssueResponse] = []
        ordered = sorted(day.segments, key=lambda segment: segment.start_time)
        previous: ItinerarySegmentResponse | None = None
        for segment in ordered:
            start = self._minutes(segment.start_time)
            end = self._minutes(segment.end_time)
            if end <= start:
                issues.append(
                    FeasibilityIssueResponse(
                        code="invalid_time_window",
                        severity="high",
                        dimension="time",
                        message=f"{segment.poi.name} 的结束时间不晚于开始时间。",
                        recommendation="请调整该活动开始时间或停留时长。",
                        affected_day_id=day.id,
                        affected_segment_id=segment.id,
                        evidence=[segment.start_time, segment.end_time],
                    )
                )
            if previous is not None:
                previous_end = self._minutes(previous.end_time)
                gap = start - previous_end
                if gap < 15:
                    issues.append(
                        FeasibilityIssueResponse(
                            code="transfer_buffer_low",
                            severity="diagnostic",
                            dimension="time",
                            message=f"{previous.poi.name} 到 {segment.poi.name} 的缓冲时间不足 15 分钟。",
                            recommendation="建议延后后一个活动或调整访问顺序。",
                            affected_day_id=day.id,
                            affected_segment_id=segment.id,
                            evidence=[f"gapMinutes={gap}"],
                        )
                    )
            previous = segment
        return issues

    def _route_issues(self, plan: ItineraryPlanResponse) -> list[FeasibilityIssueResponse]:
        issues: list[FeasibilityIssueResponse] = []
        selected_routes = [route for route in plan.route_options if route.is_selected or route.sort_order == 0]
        for route in selected_routes:
            if route.error:
                issues.append(
                    FeasibilityIssueResponse(
                        code="route_provider_failed",
                        severity="medium",
                        dimension="route",
                        message="路线 provider 返回失败，地图路线需要用户确认。",
                        recommendation="建议在地图中重新选择交通方式或稍后重试路线查询。",
                        affected_segment_id=route.from_segment_id,
                        evidence=[str(route.error)],
                    )
                )
                continue
            if route.duration_minutes >= 90 or route.distance_meters >= 18000:
                issues.append(
                    FeasibilityIssueResponse(
                        code="route_commute_high",
                        severity="diagnostic",
                        dimension="route",
                        message=(
                            f"诊断提示：该路段约 {route.duration_minutes} 分钟，"
                            f"{route.distance_meters / 1000:.1f} 公里；固定阈值不作为"
                            "路线可行性或低风险结论。"
                        ),
                        recommendation="建议调整访问顺序或改用更省时交通方式。",
                        affected_segment_id=route.from_segment_id,
                        evidence=[route.provider or route.source],
                    )
                )
        return issues

    def _ticket_issues(self, plan: ItineraryPlanResponse) -> list[FeasibilityIssueResponse]:
        issues = []
        for ticket in plan.ticket_lookup_results:
            if ticket.fallback_used or ticket.status not in {"available", "open", "estimated"}:
                issues.append(
                    FeasibilityIssueResponse(
                        code="ticket_reservation_uncertain",
                        severity="medium" if ticket.fallback_used else "high",
                        dimension="ticket_reservation",
                        message="票务/预约信息需要用户二次确认。",
                        recommendation="建议打开来源链接确认官方预约状态和价格。",
                        affected_segment_id=ticket.segment_id,
                        evidence=[ticket.source_name, ticket.credibility_rank, ticket.caveat],
                    )
                )
        return issues

    def _weather_issues(self, plan: ItineraryPlanResponse, preference_summary: str) -> list[FeasibilityIssueResponse]:
        issues = []
        weather_sensitive = any(keyword in preference_summary for keyword in ["拍照", "户外", "老人", "亲子", "步行"])
        first_segment = self._first_segment(plan)
        for signal in plan.weather_signals:
            if signal.risk_level in {"medium", "high"} or (weather_sensitive and signal.fallback_used):
                issues.append(
                    FeasibilityIssueResponse(
                        code="weather_purpose_risk",
                        severity="high" if signal.risk_level == "high" else "medium",
                        dimension="weather",
                        message=f"{signal.city} 天气可能影响旅行目的：{signal.purpose_impact_reason}",
                        recommendation="建议准备室内替代点或降低户外停留强度。",
                        affected_day_id=self._day_id_for_segment(plan, first_segment) if first_segment else None,
                        affected_segment_id=first_segment.id if first_segment else None,
                        evidence=[signal.provider_name or signal.source, signal.daily_summary],
                    )
                )
        return issues

    def _crowding_issues(self, plan: ItineraryPlanResponse) -> list[FeasibilityIssueResponse]:
        issues: list[FeasibilityIssueResponse] = []
        route_by_id = {route.id: route for route in plan.route_options}
        for signal in plan.traffic_crowding_signals:
            if signal.crowding_level not in {"medium", "high"}:
                continue
            route = route_by_id.get(signal.route_option_id)
            segment_id = route.to_segment_id or route.from_segment_id if route else None
            issues.append(
                FeasibilityIssueResponse(
                    code="crowding_medium_or_high",
                    severity="medium" if signal.crowding_level == "medium" else "high",
                    dimension="crowding",
                    message=f"路线或景点可能拥挤：{signal.estimated_reason}",
                    recommendation=signal.recommended_departure_adjustment or "建议错峰访问。",
                    affected_day_id=self._day_id_for_segment_id(plan, segment_id),
                    affected_segment_id=segment_id,
                    evidence=[signal.source],
                )
            )
        return issues

    def _budget_issues(self, plan: ItineraryPlanResponse) -> list[FeasibilityIssueResponse]:
        if plan.budget_target is None or plan.budget_target <= 0:
            return []
        ratio = plan.budget_estimate / plan.budget_target
        if ratio < 0.9:
            return []
        severity = "high" if ratio >= 1 else "medium"
        return [
            FeasibilityIssueResponse(
                code="budget_near_or_over",
                severity=severity,
                dimension="budget",
                message=f"当前估算费用约为目标预算的 {ratio:.0%}。",
                recommendation="建议优先替换高价 POI、降低交通成本或接受预算软超出。",
                evidence=[f"budgetTarget={plan.budget_target}", f"budgetEstimate={plan.budget_estimate}"],
            )
        ]

    def _preference_issues(
        self, plan: ItineraryPlanResponse, preference_summary: str
    ) -> list[FeasibilityIssueResponse]:
        if not preference_summary:
            return []
        issues = []
        if ("轻松" in preference_summary or "不赶" in preference_summary) and any(
            len(day.segments) > 3 for day in plan.days
        ):
            issues.append(
                FeasibilityIssueResponse(
                    code="preference_pace_conflict",
                    severity="medium",
                    dimension="preference",
                    message="当前行程密度和“轻松不赶路”的偏好存在冲突。",
                    recommendation="建议减少单日 POI 数量，并增加通勤缓冲。",
                    evidence=[preference_summary],
                )
            )
        if "公共交通" in preference_summary:
            taxi_segments = [
                segment
                for day in plan.days
                for segment in day.segments
                if segment.transport_mode in {"taxi", "driving"}
            ]
            if taxi_segments:
                issues.append(
                    FeasibilityIssueResponse(
                        code="preference_transport_conflict",
                        severity="low",
                        dimension="preference",
                        message="部分活动交通方式与公共交通偏好不一致。",
                        recommendation="建议查询公共交通路线，或说明为何保留打车/自驾。",
                        affected_day_id=self._day_id_for_segment(plan, taxi_segments[0]),
                        affected_segment_id=taxi_segments[0].id,
                        evidence=[preference_summary],
                    )
                )
        return issues

    def _local_replan_suggestions(
        self,
        plan: ItineraryPlanResponse,
        issues: list[FeasibilityIssueResponse],
    ) -> list[LocalReplanSuggestionResponse]:
        suggestions: list[LocalReplanSuggestionResponse] = []
        for issue in issues:
            if issue.code == "day_density_high" and issue.affected_day_id and issue.affected_segment_id:
                suggestions.append(
                    LocalReplanSuggestionResponse(
                        id=f"sug_{uuid4().hex[:12]}",
                        issue_code=issue.code,
                        action_type="reduce_day_density",
                        summary="把当天最后一个活动移动到新的一天，降低当天密度。",
                        rationale="该修改只影响单个高密度问题，不会全量重生成行程。",
                        operations=[
                            ItineraryPatchOperation(op="add_day", title="新增轻松安排").model_dump(by_alias=True),
                            ItineraryPatchOperation(
                                op="move_segment",
                                segmentId=issue.affected_segment_id,
                                targetDayId="__new_day__",
                                startTime="09:30",
                            ).model_dump(by_alias=True),
                        ],
                    )
                )
            elif issue.code == "route_commute_high" and issue.affected_segment_id:
                reorder_operations = self._reorder_operations_for_segment(plan, issue.affected_segment_id)
                if reorder_operations:
                    suggestions.append(
                        LocalReplanSuggestionResponse(
                            id=f"sug_{uuid4().hex[:12]}",
                            issue_code=issue.code,
                            action_type="adjust_visit_order",
                            summary="调整当天访问顺序，优先降低绕行和长通勤风险。",
                            rationale="只重排受影响当天的活动顺序，保留已确认 POI 和费用信息。",
                            operations=reorder_operations,
                        )
                    )
                suggestions.append(
                    LocalReplanSuggestionResponse(
                        id=f"sug_{uuid4().hex[:12]}",
                        issue_code=issue.code,
                        action_type="adjust_transport_mode",
                        summary="针对长通勤段改用更省时交通，并重新查询路线。",
                        rationale="只修改受影响路段的交通假设，保留已确认 POI。",
                        operations=[
                            ItineraryPatchOperation(
                                op="update_segment_notes",
                                segmentId=issue.affected_segment_id,
                                notes="建议改用更省时交通方式并重新查询路线。",
                            ).model_dump(by_alias=True)
                        ],
                    )
                )
            elif issue.code == "weather_purpose_risk" and issue.affected_segment_id:
                suggestions.append(
                    LocalReplanSuggestionResponse(
                        id=f"sug_{uuid4().hex[:12]}",
                        issue_code=issue.code,
                        action_type="indoor_weather_alternative",
                        summary="保留行程主线，给受影响活动加入室内替代方案提示。",
                        rationale="天气风险先以局部备注方式处理，避免静默替换用户已确认 POI。",
                        operations=[
                            ItineraryPatchOperation(
                                op="update_segment_notes",
                                segmentId=issue.affected_segment_id,
                                notes="天气可能影响当前旅行目的；建议在地图中选择一个室内替代点，或降低户外停留强度。",
                            ).model_dump(by_alias=True)
                        ],
                    )
                )
            elif issue.code == "crowding_medium_or_high" and issue.affected_segment_id:
                segment = self._segment_by_id(plan, issue.affected_segment_id)
                shifted_start = self._crowding_shift_start(plan, segment) if segment else None
                suggestions.append(
                    LocalReplanSuggestionResponse(
                        id=f"sug_{uuid4().hex[:12]}",
                        issue_code=issue.code,
                        action_type="avoid_crowding_time",
                        summary="把拥挤风险较高的活动调整到更早时段。",
                        rationale="只调整一个活动开始时间，用户确认后再写入行程。",
                        operations=[
                            ItineraryPatchOperation(
                                op="replace_segment_start_time",
                                segmentId=issue.affected_segment_id,
                                startTime=shifted_start,
                            ).model_dump(by_alias=True)
                        ]
                        if shifted_start
                        else [],
                    )
                )
            elif issue.code == "ticket_reservation_uncertain" and issue.affected_segment_id:
                suggestions.append(
                    LocalReplanSuggestionResponse(
                        id=f"sug_{uuid4().hex[:12]}",
                        issue_code=issue.code,
                        action_type="replace_high_risk_poi",
                        summary="该 POI 存在票务或预约不确定性，建议从地图候选中选择可预约替代点。",
                        rationale="替换 POI 需要真实高德候选，当前先给出待确认替换建议，不静默覆盖。",
                        operations=[],
                    )
                )
            if len(suggestions) >= 6:
                break
        return suggestions

    def _first_segment(self, plan: ItineraryPlanResponse) -> Optional[ItinerarySegmentResponse]:
        for day in plan.days:
            if day.segments:
                return day.segments[0]
        return None

    def _segment_by_id(
        self, plan: ItineraryPlanResponse, segment_id: Optional[str]
    ) -> Optional[ItinerarySegmentResponse]:
        if not segment_id:
            return None
        for day in plan.days:
            for segment in day.segments:
                if segment.id == segment_id:
                    return segment
        return None

    def _day_for_segment_id(
        self, plan: ItineraryPlanResponse, segment_id: Optional[str]
    ) -> Optional[ItineraryDayResponse]:
        if not segment_id:
            return None
        for day in plan.days:
            if any(segment.id == segment_id for segment in day.segments):
                return day
        return None

    def _day_id_for_segment(
        self, plan: ItineraryPlanResponse, segment: Optional[ItinerarySegmentResponse]
    ) -> Optional[str]:
        return self._day_id_for_segment_id(plan, segment.id if segment else None)

    def _day_id_for_segment_id(self, plan: ItineraryPlanResponse, segment_id: Optional[str]) -> Optional[str]:
        day = self._day_for_segment_id(plan, segment_id)
        return day.id if day else None

    def _reorder_operations_for_segment(self, plan: ItineraryPlanResponse, segment_id: str) -> list[dict]:
        day = self._day_for_segment_id(plan, segment_id)
        if day is None or len(day.segments) < 2:
            return []
        ordered_ids = [segment.id for segment in day.segments]
        if segment_id not in ordered_ids:
            return []
        reordered = [segment_id, *[item for item in ordered_ids if item != segment_id]]
        if reordered == ordered_ids:
            return []
        return [
            ItineraryPatchOperation(
                op="reorder_segments",
                dayId=day.id,
                orderedSegmentIds=reordered,
            ).model_dump(by_alias=True)
        ]

    def _crowding_shift_start(self, plan: ItineraryPlanResponse, segment: ItinerarySegmentResponse) -> Optional[str]:
        day = self._day_for_segment_id(plan, segment.id)
        if day is None:
            return None
        start = self._minutes(segment.start_time)
        duration = max(30, self._minutes(segment.end_time) - start)
        latest_end = max(self._minutes(item.end_time) for item in day.segments)
        candidate_starts = [start - 60, start + 90, 8 * 60 + 30, latest_end + 30]
        for candidate in candidate_starts:
            if candidate < 8 * 60 or candidate + duration > 22 * 60:
                continue
            if self._time_slot_is_free(day, segment.id, candidate, candidate + duration):
                return self._format_minutes(candidate)
        return None

    def _time_slot_is_free(self, day: ItineraryDayResponse, ignore_segment_id: str, start: int, end: int) -> bool:
        for segment in day.segments:
            if segment.id == ignore_segment_id:
                continue
            existing_start = self._minutes(segment.start_time)
            existing_end = self._minutes(segment.end_time)
            if start < existing_end and end > existing_start:
                return False
        return True

    def _score(self, issues: list[FeasibilityIssueResponse]) -> int:
        penalty = 0
        for issue in issues:
            penalty += {"high": 18, "medium": 10, "low": 5}.get(issue.severity, 0)
        return max(0, 100 - penalty)

    @staticmethod
    def _route_status(plan: ItineraryPlanResponse) -> str:
        segment_count = sum(len(day.segments) for day in plan.days)
        if segment_count <= 1 and not plan.route_options:
            return "not_required"
        # Legacy feasibility has no portable routeDecisionContract or raw
        # insertion matrix proof.  Even when RouteOption rows exist, it may
        # only expose diagnostics and must not claim final feasibility.
        return "pending_provider_verification"

    def _risk_level(self, score: int, issues: list[FeasibilityIssueResponse]) -> str:
        if any(issue.severity == "high" for issue in issues) or score < 60:
            return "high"
        if any(issue.severity == "medium" for issue in issues) or score < 82:
            return "medium"
        return "low"

    def _preference_alignment(
        self,
        plan: ItineraryPlanResponse,
        preference_summary: str,
        issues: list[FeasibilityIssueResponse],
    ) -> str:
        if not preference_summary:
            return "尚未保存明确偏好，本次按通用可执行性检查。"
        preference_issue_count = sum(1 for issue in issues if issue.dimension == "preference")
        if preference_issue_count:
            return f"已读取偏好摘要，但发现 {preference_issue_count} 个偏好冲突，需要确认后局部优化。"
        if "轻松" in preference_summary or "不赶" in preference_summary:
            return "偏好摘要已用于降低单日密度和识别赶路风险。"
        if "拍照" in preference_summary:
            return "偏好摘要已用于提高天气和户外体验风险权重。"
        return "偏好摘要已作为预算、交通和旅行目的检查上下文。"

    def _minutes(self, value: str) -> int:
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)

    def _format_minutes(self, value: int) -> str:
        hours = value // 60
        minutes = value % 60
        return f"{hours:02d}:{minutes:02d}"

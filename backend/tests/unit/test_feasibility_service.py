from datetime import datetime, timezone

from src.api.schemas.itineraries import (
    ItineraryDayResponse,
    ItineraryPlanResponse,
    ItinerarySegmentResponse,
    PoiResponse,
    RouteOptionResponse,
    TicketLookupResultResponse,
    TrafficCrowdingSignalResponse,
    WeatherSignalResponse,
)
from src.services.feasibility_service import FeasibilityService


def test_feasibility_flags_dense_relaxed_plan_and_returns_local_suggestion():
    plan = plan_fixture(segment_count=4)

    report = FeasibilityService().evaluate(plan, "用户偏好轻松不赶路，预算约 300 元，拍照优先。")

    assert report.score < 100
    assert report.risk_level in {"medium", "high"}
    assert any(issue.code == "day_density_high" for issue in report.issues)
    assert any(issue.code == "weather_purpose_risk" for issue in report.issues)
    assert report.preference_alignment
    assert report.local_replan_suggestions
    assert report.local_replan_suggestions[0].requires_confirmation is True
    assert report.local_replan_suggestions[0].action_type == "reduce_day_density"


def test_feasibility_budget_ticket_and_route_issues_are_user_visible():
    plan = plan_fixture(segment_count=2)

    report = FeasibilityService().evaluate(plan, "用户偏好公共交通，预算 100 元。")

    issue_codes = {issue.code for issue in report.issues}
    assert "budget_near_or_over" in issue_codes
    assert "ticket_reservation_uncertain" in issue_codes
    assert "route_commute_high" in issue_codes


def test_feasibility_returns_local_replan_suggestions_for_core_risk_types():
    plan = plan_fixture(segment_count=4)

    report = FeasibilityService().evaluate(plan, "用户偏好轻松不赶路，预算 300 元，拍照优先，公共交通。")

    suggestions_by_action = {suggestion.action_type: suggestion for suggestion in report.local_replan_suggestions}
    assert "reduce_day_density" in suggestions_by_action
    assert "adjust_transport_mode" in suggestions_by_action
    assert "indoor_weather_alternative" in suggestions_by_action
    assert "avoid_crowding_time" in suggestions_by_action
    assert "replace_high_risk_poi" in suggestions_by_action
    assert suggestions_by_action["indoor_weather_alternative"].operations[0]["op"] == "update_segment_notes"
    assert suggestions_by_action["avoid_crowding_time"].operations[0]["op"] == "replace_segment_start_time"
    assert suggestions_by_action["avoid_crowding_time"].operations[0]["startTime"] == "13:00"
    assert suggestions_by_action["replace_high_risk_poi"].operations == []


def test_legacy_route_thresholds_are_diagnostic_and_route_stays_pending():
    plan = plan_fixture(segment_count=2).model_copy(
        update={
            "budget_target": None,
            "budget_estimate": 0,
            "ticket_lookup_results": [],
            "weather_signals": [],
            "traffic_crowding_signals": [],
        }
    )
    plan.days[0].segments[0].start_time = "08:00"
    plan.days[0].segments[0].end_time = "09:00"
    plan.days[0].segments[1].start_time = "09:30"
    plan.days[0].segments[1].end_time = "10:30"

    report = FeasibilityService().evaluate(plan)

    assert report.route_status == "pending_provider_verification"
    assert report.score == 100
    assert report.risk_level == "low"
    commute = next(issue for issue in report.issues if issue.code == "route_commute_high")
    assert commute.severity == "diagnostic"
    assert "不作为路线可行性" in commute.message


def plan_fixture(segment_count: int) -> ItineraryPlanResponse:
    now = datetime.now(timezone.utc)
    poi = PoiResponse(
        id="poi_1",
        amapId="B0001",
        name="故宫博物院",
        city="北京",
        category="scenic",
        latitude=39.9,
        longitude=116.3,
        source="amap-place-search",
        confidence=0.91,
    )
    segments = [
        ItinerarySegmentResponse(
            id=f"seg_{index}",
            startTime=f"{8 + index:02d}:00",
            endTime=f"{9 + index:02d}:30",
            kind="activity",
            poi=poi,
            transportMode="taxi",
            estimatedCost=60,
            notes="测试活动",
        )
        for index in range(segment_count)
    ]
    return ItineraryPlanResponse(
        id="plan_1",
        title="北京测试行程",
        city="北京",
        templateType="custom",
        budgetTarget=100,
        budgetEstimate=240,
        budgetDeltaExplanation="预算软约束。",
        decisionRationale="测试。",
        status="draft",
        days=[
            ItineraryDayResponse(
                id="day_1",
                dayNumber=1,
                title="测试日",
                weatherSummary="小雨",
                riskSummary="天气影响拍照",
                totalEstimatedCost=240,
                segments=segments,
            )
        ],
        routeOptions=[
            RouteOptionResponse(
                id="route_1",
                fromSegmentId="seg_0",
                toSegmentId="seg_1",
                fromPoiId="poi_1",
                toPoiId="poi_2",
                provider="amap-route-provider",
                mode="taxi",
                label="驾车",
                isSelected=True,
                sortOrder=0,
                transportMode="taxi",
                distanceMeters=21000,
                durationSeconds=6000,
                durationMinutes=100,
                costAmount=80,
                costCurrency="CNY",
                costEstimate=80,
                crowdingRisk="medium",
                source="amap-route",
                queriedAt=now,
            )
        ],
        weatherSignals=[
            WeatherSignalResponse(
                id="weather_1",
                city="北京",
                date="2026-06-11",
                hourlyForecast=[],
                dailySummary="小雨，户外拍照受影响",
                riskLevel="medium",
                purposeImpactReason="雨天影响拍照和户外步行。",
                source="高德天气",
                dataStatus="degraded",
                confidence=0.72,
                providerName="mock-amap-weather-provider",
                fallbackUsed=True,
                userVisibleCaveat="天气 fallback。",
                queriedAt=now,
            )
        ],
        trafficCrowdingSignals=[
            TrafficCrowdingSignalResponse(
                id="traffic_1",
                routeOptionId="route_1",
                realDataAvailable=False,
                crowdingLevel="medium",
                estimatedReason="热门景区可能排队。",
                recommendedDepartureAdjustment="建议错峰。",
                source="estimated-crowding",
                queriedAt=now,
            )
        ],
        ticketLookupResults=[
            TicketLookupResultResponse(
                id="ticket_1",
                segmentId="seg_0",
                ticketType="attraction",
                status="estimated",
                priceEstimate=60,
                bookingUrl="https://example.com",
                sourceName="mock",
                sourceUrl="https://example.com",
                credibilityRank="mock",
                queriedAt=now,
                caveat="查询结果仅供参考，请以购票平台为准",
                providerName="mock-web-search-provider",
                fallbackUsed=True,
                providerFailureReason="missing key",
                confidence=0.4,
            )
        ],
        routeWarnings=[],
    )

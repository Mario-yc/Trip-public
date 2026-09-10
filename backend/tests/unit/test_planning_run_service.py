import sqlite3

from src.api.schemas.itineraries import ItineraryDayResponse, ItineraryPlanResponse, ItinerarySegmentResponse, PoiResponse
from src.services.planning_run_service import PlanningRunService


def test_planning_run_understood_requirements_include_key_trip_fields():
    service = PlanningRunService(sqlite3.connect(":memory:"))
    requirements = service._understood_requirements(
        "用户确认切换路线方案。",
        plan_fixture(),
        "2人同行，预算 3000 元，10月中旬出发，偏好轻松不赶路，地铁公交为主，拍照优先。",
    )

    assert requirements["isCompleteEnoughToPlan"] is True
    assert requirements["missingFields"] == []
    assert requirements["fields"]["destination"] == "北京"
    assert requirements["fields"]["travelDays"] == "1 天"
    assert requirements["fields"]["travelDate"] == "10月中旬"
    assert requirements["fields"]["budget"] == "3000 元"
    assert requirements["fields"]["partySize"] == "2 人"
    assert requirements["fields"]["transportPreference"] == "地铁、公交"
    assert set(requirements["fields"]["travelPurpose"].split("、")) == {"轻松不赶路", "拍照打卡"}


def test_planning_run_understood_requirements_keeps_non_blocking_unknowns_out_of_questions():
    service = PlanningRunService(sqlite3.connect(":memory:"))
    requirements = service._understood_requirements("用户打开行程，系统重新查询票务/预约状态。", plan_fixture(), "")

    assert "travelDate" in requirements["missingFields"]
    assert "budget" not in requirements["missingFields"]
    assert "partySize" not in requirements["missingFields"]
    assert any("哪几天出发" in question for question in requirements["clarificationQuestions"])
    assert not any("几个人出行" in question for question in requirements["clarificationQuestions"])


def test_planning_run_constraint_summary_does_not_treat_plan_budget_target_as_user_constraint():
    service = PlanningRunService(sqlite3.connect(":memory:"))
    plan = plan_fixture()
    plan.budget_target = 3000

    constraints = service._constraint_summary(plan, "")

    assert {"label": "城市", "value": "北京"} in constraints
    assert {"label": "天数", "value": "1 天"} in constraints
    assert not any(item["label"] == "预算" for item in constraints)


def test_planning_run_understood_requirements_does_not_infer_budget_from_plan_target():
    service = PlanningRunService(sqlite3.connect(":memory:"))
    plan = plan_fixture()
    plan.budget_target = 3000

    requirements = service._understood_requirements("用户只说想去北京故宫。", plan, "")

    assert requirements["fields"]["budget"] == "待确认"
    assert "budget" not in requirements["missingFields"]
    assert "3000 元" not in requirements["summary"]


def test_planning_run_understood_requirements_does_not_infer_transport_from_plan_segments():
    service = PlanningRunService(sqlite3.connect(":memory:"))
    plan = plan_fixture()

    requirements = service._understood_requirements("用户只说想去北京故宫。", plan, "")

    assert requirements["fields"]["transportPreference"] == "待确认"
    assert "transportPreference" not in requirements["missingFields"]
    assert "public_transit" not in requirements["summary"]


def plan_fixture() -> ItineraryPlanResponse:
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
    return ItineraryPlanResponse(
        id="plan_1",
        title="北京测试行程",
        city="北京",
        templateType="custom",
        budgetTarget=None,
        budgetEstimate=120,
        budgetDeltaExplanation="预算软约束。",
        decisionRationale="测试。",
        status="draft",
        days=[
            ItineraryDayResponse(
                id="day_1",
                dayNumber=1,
                title="测试日",
                weatherSummary="晴",
                riskSummary="低风险",
                totalEstimatedCost=120,
                segments=[
                    ItinerarySegmentResponse(
                        id="seg_1",
                        startTime="09:00",
                        endTime="11:00",
                        kind="activity",
                        poi=poi,
                        transportMode="public_transit",
                        estimatedCost=60,
                        notes="测试活动",
                    )
                ],
            )
        ],
        routeOptions=[],
        weatherSignals=[],
        trafficCrowdingSignals=[],
        ticketLookupResults=[],
        routeWarnings=[],
    )

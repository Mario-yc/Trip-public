from src.services.agent_planner_service import AgentPlannerService, PLANNER_VERSION


def active_itinerary_observation() -> dict:
    return {"itinerary": {"activeVersionId": "version_1", "meaningfulSegmentCount": 1}}


def test_planner_marks_initial_poi_draft_as_versioned_patch_with_grounding():
    plan = AgentPlannerService().plan(
        "帮我安排北京一天，想去故宫和胡同",
        {"currentItinerarySnapshot": None, "timelineContext": None, "pendingAmapPoiCandidates": []},
    )

    assert plan["plannerVersion"] == PLANNER_VERSION
    assert plan["intent"] == "draft_itinerary"
    assert plan["writeIntent"] == "versioned_patch"
    assert plan["requiresPoiResolution"] is True
    assert plan["requiresPatch"] is True
    assert plan["expectedHarnessStages"] == ["plan", "resolve_poi", "apply_patch", "verify", "respond"]
    assert "amap_poi_grounding" in plan["riskControls"]
    assert any(item["requiresTool"] == "poi_resolution_service" for item in plan["items"])
    assert any(item["requiresTool"] == "itinerary_patch_service" for item in plan["items"])


def test_planner_blocks_write_when_pending_poi_needs_confirmation():
    plan = AgentPlannerService().plan(
        "这个候选餐厅可以吗",
        {
            "currentItinerarySnapshot": {"days": []},
            "pendingAmapPoiCandidates": [{"id": "cand_1"}],
        },
    )

    assert plan["intent"] == "patch"
    assert plan["writeIntent"] == "no_write_until_clarified"
    assert plan["requiresPatch"] is False
    assert "pending_poi_state_machine" in plan["riskControls"]
    assert any("pending AMap POI" in item["goal"] for item in plan["items"])


def test_planner_detects_rollback_supersession_intent():
    plan = AgentPlannerService().plan(
        "编辑之前那条消息并从那里重新生成",
        {"currentItinerarySnapshot": {"days": []}},
    )

    assert plan["intent"] == "rollback"
    assert plan["requiresRollback"] is True
    assert "rollback_supersession" in plan["riskControls"]
    assert any(item["requiresTool"] == "conversation_service" for item in plan["items"])


def test_planner_detects_clarification_before_patch_write():
    plan = AgentPlannerService().plan(
        "想出去玩",
        {"understoodRequirements": {"isCompleteEnoughToPlan": False, "missingFields": ["travelDays"]}},
    )

    assert plan["intent"] == "clarification"
    assert plan["writeIntent"] == "no_write_until_clarified"
    assert plan["requiresPatch"] is False
    assert any("Clarify missing trip requirements" in item["goal"] for item in plan["items"])


def test_planner_detects_ticket_weather_source_tool_need():
    plan = AgentPlannerService().plan(
        "查一下故宫门票预约和天气",
        {"currentItinerarySnapshot": {"days": []}},
    )

    assert "source_transparency" in plan["riskControls"]
    assert any(item["requiresTool"] == "travel_tool_registry" for item in plan["items"])


def test_planner_routes_plan_comparison_as_read_only():
    plan = AgentPlannerService().plan(
        "帮我比较三个方案，先不要改当前行程",
        {"currentItinerarySnapshot": {"days": []}, "pendingAmapPoiCandidates": []},
    )

    assert plan["intent"] == "comparison"
    assert plan["taskType"] == "plan_comparison"
    assert plan["readOnly"] is True
    assert plan["requiresPatch"] is False
    assert plan["writeIntent"] == "read_only_comparison"
    assert "generate_plan_comparison" in plan["allowedTools"]
    assert "read_only_comparison" in plan["riskControls"]


def test_planner_routes_route_optimization_to_minimal_versioned_patch():
    plan = AgentPlannerService().plan(
        "第二天太赶了，按公交地铁优化路线",
        {"agentObservation": active_itinerary_observation(), "pendingAmapPoiCandidates": []},
    )

    assert plan["taskType"] == "route_optimization"
    assert plan["requiresPatch"] is True
    assert plan["writeIntent"] == "versioned_patch"
    assert plan["toolStrategy"]["modificationsUseMinimalPatch"] is True
    assert "route_feasibility_soft_check" in plan["riskControls"]


def test_planner_routes_ticket_weather_lookup_as_realtime_read_only():
    plan = AgentPlannerService().plan(
        "查一下故宫门票预约和天气，先不要改行程",
        {"agentObservation": active_itinerary_observation(), "pendingAmapPoiCandidates": []},
    )

    assert plan["taskType"] == "ticket_source_lookup"
    assert plan["requiresRealtimeInfo"] is True
    assert plan["readOnly"] is True
    assert plan["requiresPatch"] is False
    assert {"web_search", "ticket_lookup", "amap_weather"}.issubset(set(plan["allowedTools"]))
    assert plan["toolStrategy"]["realtimeFactsRequireTools"] is True


def test_planner_routes_rollback_before_clarification_when_editing_prior_turn():
    plan = AgentPlannerService().plan(
        "编辑之前那条消息并从那里重新生成",
        {
            "currentItinerarySnapshot": {"days": []},
            "understoodRequirements": {"isCompleteEnoughToPlan": False, "missingFields": ["travelDays"]},
        },
    )

    assert plan["taskType"] == "rollback"
    assert plan["intent"] == "rollback"
    assert plan["requiresRollback"] is True
    assert plan["expectedHarnessStages"] == ["plan", "restore_version", "verify", "respond"]


def test_planner_routes_mixed_route_and_weather_request_to_write_flow():
    plan = AgentPlannerService().plan(
        "第二天太赶了，按公交地铁优化路线并查天气",
        {"agentObservation": active_itinerary_observation(), "pendingAmapPoiCandidates": []},
    )

    assert plan["taskType"] == "route_optimization"
    assert plan["requiresRealtimeInfo"] is True
    assert plan["readOnly"] is False
    assert plan["requiresPatch"] is True
    assert {"amap_weather", "patch_itinerary"}.issubset(set(plan["allowedTools"]))


def test_planner_routes_national_day_limit_question_to_realtime_read_only():
    plan = AgentPlannerService().plan(
        "国庆去故宫会不会限流？",
        {"currentItinerarySnapshot": {"days": []}, "pendingAmapPoiCandidates": []},
    )

    assert plan["requiresRealtimeInfo"] is True
    assert plan["readOnly"] is True
    assert plan["requiresPatch"] is False
    assert {"web_search", "ticket_lookup", "amap_weather"}.issubset(set(plan["allowedTools"]))


def test_planner_routes_national_day_initial_planning_with_realtime_context():
    plan = AgentPlannerService().plan(
        "十一黄金周帮我安排北京一天",
        {"currentItinerarySnapshot": None, "timelineContext": None, "pendingAmapPoiCandidates": []},
    )

    assert plan["taskType"] == "initial_planning"
    assert plan["requiresPatch"] is True
    assert plan["requiresRealtimeInfo"] is True
    assert {"web_search", "ticket_lookup", "amap_weather", "patch_itinerary"}.issubset(set(plan["allowedTools"]))


def test_planner_treats_latest_local_night_view_grounding_as_non_realtime_edit():
    plan = AgentPlannerService().plan(
        "第二天的夜景观景点不是具体的地点，改为真实的地点",
        {
            "effectiveUserMessage": "国庆帮我安排北京两天，需要考虑热门景点",
            "agentObservation": active_itinerary_observation(),
            "pendingAmapPoiCandidates": [],
        },
    )

    assert plan["taskType"] == "poi_grounding"
    assert plan["requiresRealtimeInfo"] is False
    assert plan["requiresPatch"] is True
    assert "source_transparency" not in plan["riskControls"]
    assert {"web_search", "ticket_lookup", "amap_weather"}.isdisjoint(set(plan["allowedTools"]))


def test_planner_keeps_explicit_latest_night_view_source_lookup_realtime():
    plan = AgentPlannerService().plan(
        "第二天夜景点改成真实地点，并查官方开放时间和预约",
        {
            "effectiveUserMessage": "国庆帮我安排北京两天，需要考虑热门景点",
            "currentItinerarySnapshot": {"days": [{"dayNumber": 1}, {"dayNumber": 2}]},
            "pendingAmapPoiCandidates": [],
        },
    )

    assert plan["requiresRealtimeInfo"] is True
    assert "source_transparency" in plan["riskControls"]
    assert {"web_search", "ticket_lookup", "amap_weather"}.issubset(set(plan["allowedTools"]))


def test_planner_does_not_modify_timeline_for_closure_risk_lookup():
    plan = AgentPlannerService().plan(
        "查一下这个景点国庆有没有闭园风险",
        {"currentItinerarySnapshot": {"days": []}, "pendingAmapPoiCandidates": []},
    )

    assert plan["taskType"] == "ticket_source_lookup"
    assert plan["requiresRealtimeInfo"] is True
    assert plan["requiresPatch"] is False

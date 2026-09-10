import json
from pathlib import Path


REQUIRED_SCENARIO_IDS = {
    "01_complete_initial_planning",
    "02_missing_information_clarification",
    "03_read_only_query",
    "04_search_candidates_for_user_choice",
    "05_structured_candidate_click",
    "06a_natural_language_react_local_edit",
    "06b_typed_or_executor_local_edit",
    "07_route_optimization",
    "08_far_future_weather_uncertainty",
    "09_trip_only_vs_long_term_memory",
    "10_controller_timeout",
    "11_schema_invalid",
    "12_unauthorized_tool",
    "13_write_risk_miscalculation",
    "14_version_conflict",
    "15_duplicate_request",
    "16_verifier_failure_and_rollback",
    "17_singular_night_cardinality_and_partial_timeline",
    "18_every_night_cardinality_distribution",
    "19_invalid_daily_cardinality_repair_and_fallback",
    "20_pending_slot_read_only_navigation",
}


def test_autonomy_required_scenario_manifest_is_complete_and_points_to_collected_tests():
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (root / "backend/evals/autonomy_controller_required_scenarios.json").read_text(encoding="utf-8")
    )
    scenarios = manifest["scenarios"]

    assert manifest["schemaVersion"] == "agent-autonomy-required-scenarios-v1"
    assert {item["id"] for item in scenarios} == REQUIRED_SCENARIO_IDS
    for scenario in scenarios:
        assert scenario["evidenceTests"], scenario["id"]
        for node_id in scenario["evidenceTests"]:
            relative_path, test_name = node_id.split("::", 1)
            source = (root / relative_path).read_text(encoding="utf-8")
            assert f"def {test_name}(" in source, node_id


def test_night_cardinality_candidate_first_cases_are_executable_offline_contracts():
    root = Path(__file__).resolve().parents[3]
    cases_dir = root / "backend/evals/cases"
    singular = json.loads((cases_dir / "beijing_university_night_candidate_first.json").read_text(encoding="utf-8"))
    every_night = json.loads(
        (cases_dir / "beijing_university_every_night_candidate_first.json").read_text(encoding="utf-8")
    )

    assert singular["schemaVersion"] == "react-offline-harness-v2"
    assert every_night["schemaVersion"] == "react-offline-harness-v2"
    expected_proposal_only_metrics = {
        "expectedLifecycle": "proposal_only",
        "expectedNightViewCount": 2,
        "expectedNightViewDayNumbers": [1, 2],
        "expectedVisibleProposalCount": 1,
        "expectedAdoptionReadyProposalCount": 1,
        "expectedRouteProviderAttemptCount": 2,
        "requireRouteCoverageComplete": True,
        "requirePersistedSelectionCapability": True,
        "expectedVersionWriteCount": 0,
        "expectedPatchWriteCount": 0,
        "expectedRouteWriteCount": 0,
    }
    assert singular["stabilityMetrics"] == expected_proposal_only_metrics
    assert every_night["stabilityMetrics"] == expected_proposal_only_metrics
    request_content = singular["steps"][1]["content"]
    assert request_content.startswith(
        "今年国庆参观北京高校两日游，每晚都看北京夜景。10月1日到2日，2天，"
        "中等预算，1人，公交地铁优先。"
    )
    assert "景点间的距离不要间隔太远" in request_content
    assert "绕行最多15分钟" in request_content
    assert "绕行比例最多15%" in request_content

    singular_plan = singular["initialPlanPayloads"][0]
    singular_night_slots = [slot for slot in singular_plan["daySlots"] if slot["kind"] == "night_view"]
    singular_night_pool = next(pool for pool in singular_plan["intentPools"] if pool["intentType"] == "night_view")
    assert [slot["dayNumber"] for slot in singular_night_slots] == [1, 2]
    assert singular_night_pool["targetCount"] == 2
    assert singular_night_pool["assignToSlots"] == [slot["slotId"] for slot in singular_night_slots]
    assert any(
        assertion.get("path") == "first.assistantTurn.comparisonProjections[].adoptionReady"
        and assertion.get("contains") is True
        for assertion in singular["assertions"]
    )
    assert any(
        assertion.get("path") == "first.assistantTurn.choiceOptions[].action"
        and assertion.get("contains") == "select_plan_proposal"
        for assertion in singular["assertions"]
    )

    every_plan = every_night["initialPlanPayloads"][0]
    every_night_slots = [slot for slot in every_plan["daySlots"] if slot["kind"] == "night_view"]
    every_night_pool = next(pool for pool in every_plan["intentPools"] if pool["intentType"] == "night_view")
    assert [slot["dayNumber"] for slot in every_night_slots] == [1, 2]
    assert every_night_pool["targetCount"] == 2
    assert every_night_pool["assignToSlots"] == [slot["slotId"] for slot in every_night_slots]

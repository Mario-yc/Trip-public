import pytest

from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.controller_failure_classifier import classify_controller_failure
from src.services.request_activity_coverage_service import RequestActivityCoverageError


@pytest.mark.parametrize("code", [
    "request_activity_coverage_duplicate_activity:request_clause_1:goal_a,goal_b",
    "request_activity_coverage_directive_omitted",
])
def test_coverage_rejection_is_a_non_retrying_contract_failure(code):
    failure = classify_controller_failure(
        RequestActivityCoverageError(code), stage="full", provider="deepseek", model="test",
        duration_ms=5, timeout_seconds=10,
    )
    assert failure.failure_class == "schema_validation_failed"
    assert failure.schema_error_summary == code
    assert failure.retryable is False


def test_duplicate_activity_message_explains_model_error_not_missing_user_requirements():
    decision = AgentAutonomyController._request_coverage_failure_decision(
        "RequestActivityCoverageError:request_activity_coverage_duplicate_activity:request_clause_1:goal_a,goal_b"
    )
    assert "同一项活动重复定义" in decision.user_visible_reason
    assert "原始需求已保留" in decision.user_visible_reason
    assert "尚未查询地点" in decision.user_visible_reason
    assert decision.expected_outcome["timelineWriteDelta"] == 0
    assert decision.action_directive.choice_ids == []

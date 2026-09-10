import json

from backend.evals.replay import extract_trace_events, replay_agent_response, replay_trace
from backend.evals.replay_trace import replay_eval_output


def test_replay_trace_accepts_ordered_harness_stages():
    report = replay_trace(
        [
            {"type": "plan", "status": "completed"},
            {"type": "resolve_poi", "status": "completed"},
            {"type": "apply_patch", "status": "completed"},
            {"type": "verify", "status": "completed", "metadata": {"passed": True}},
            {"type": "respond", "status": "completed"},
        ]
    )

    assert report["passed"] is True
    assert report["missingStages"] == []
    assert report["outOfOrder"] == []


def test_replay_trace_rejects_missing_or_failed_stage():
    report = replay_trace(
        [
            {"type": "plan", "status": "completed"},
            {"type": "apply_patch", "status": "failed", "failureReason": "bad patch"},
            {"type": "respond", "status": "completed"},
        ]
    )

    assert report["passed"] is False
    assert "resolve_poi" in report["missingStages"]
    assert report["failedStages"][0]["type"] == "apply_patch"


def test_replay_trace_uses_tool_name_for_wrapped_pipeline_stage():
    report = replay_trace(
        [
            {
                "type": "tool",
                "toolName": "normalize_request",
                "status": "completed",
            }
        ],
        expected_stages=["normalize_request"],
    )

    assert report["passed"] is True
    assert report["observedStages"] == ["normalize_request"]


def test_replay_trace_uses_tool_name_when_exported_tool_call_has_no_type():
    report = replay_trace(
        [{"id": "normalize_request", "toolName": "normalize_request", "status": "completed"}],
        expected_stages=["normalize_request"],
    )

    assert report["passed"] is True


def test_replay_eval_output_summarizes_case_reports():
    report = replay_eval_output(
        {
            "cases": [
                {"id": "ok", "traceReplay": {"passed": True, "missingStages": [], "outOfOrder": [], "failedStages": []}},
                {"id": "bad", "traceReplay": {"passed": False, "missingStages": ["verify"], "outOfOrder": [], "failedStages": []}},
            ]
        }
    )

    assert report["total"] == 2
    assert report["passed"] == 1
    assert report["failed"] == 1
    assert report["failures"][0]["id"] == "bad"


def test_replay_eval_output_honors_allowed_failed_stages_from_saved_trace():
    report = replay_eval_output(
        {
            "cases": [
                {
                    "id": "invalid_patch_safe_reject",
                    "traceReplay": {
                        "events": [{"type": "respond", "status": "failed", "failureReason": "invalid patch"}],
                        "expectedStages": ["respond"],
                        "allowedFailedStages": ["respond"],
                    },
                }
            ]
        }
    )

    assert report["total"] == 1
    assert report["passed"] == 1
    assert report["failed"] == 0


def test_replay_agent_response_summarizes_successful_write_trace():
    response_json = {
        "planningSteps": [
            {"type": "plan", "status": "completed"},
            {"type": "resolve_poi", "status": "completed"},
            {"type": "apply_patch", "status": "completed"},
            {"type": "verify", "status": "completed", "metadata": {"passed": True}},
            {"type": "respond", "status": "completed"},
            {"type": "tool", "label": "patch_itinerary", "status": "completed"},
        ]
    }

    summary = replay_agent_response(response_json)

    assert summary["passed"] is True
    assert summary["eventCount"] == 6
    assert summary["toolCallCount"] == 1
    assert summary["failedToolCallCount"] == 0
    assert summary["verifierPassed"] is True


def test_replay_agent_response_summarizes_pending_poi_trace():
    response_json = json.dumps(
        {
            "planningSteps": [
                {"type": "plan", "status": "completed"},
                {"type": "resolve_poi", "status": "waiting"},
                {"type": "apply_patch", "status": "fallback"},
                {"type": "verify", "status": "completed", "metadata": {"passed": True}},
                {"type": "respond", "status": "completed"},
            ]
        }
    )

    summary = replay_agent_response(response_json)

    assert summary["passed"] is True
    assert summary["eventCount"] == 5
    assert summary["stageCounts"]["resolve_poi"] == 1
    assert extract_trace_events(response_json)[1]["status"] == "waiting"


def test_replay_agent_response_summarizes_verifier_failed_trace():
    summary = replay_agent_response(
        {
            "planningSteps": [
                {"type": "plan", "status": "completed"},
                {"type": "resolve_poi", "status": "completed"},
                {"type": "apply_patch", "status": "completed"},
                {"type": "verify", "status": "failed", "metadata": {"passed": False}},
                {"type": "respond", "status": "failed"},
            ]
        },
        allowed_failed_stages=["respond"],
    )

    assert summary["passed"] is False
    assert summary["verifierPassed"] is False
    assert summary["failedStages"][0]["type"] == "verify"

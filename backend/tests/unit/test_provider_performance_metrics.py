import json

import pytest

from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.deepseek_agent_provider import DeepSeekAgentProvider


@pytest.mark.parametrize("call_kind", ["full", "repair", "lite", "semantic_action"])
def test_performance_snapshot_preserves_registered_call_kind(call_kind):
    assert AgentAutonomyController._controller_performance_snapshot({"callKind": call_kind})["callKind"] == call_kind


def _record_response(monkeypatch, message, usage=None):
    body = {"choices": [{"finish_reason": "tool_calls", "message": message}]}
    if usage is not None:
        body["usage"] = usage
    encoded = json.dumps(body, ensure_ascii=True).encode("utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return encoded

    monkeypatch.setattr("src.services.deepseek_agent_provider.urlopen", lambda *_args, **_kwargs: Response())
    provider = DeepSeekAgentProvider(api_key="test-only-secret", model="performance-test")
    shared = {}
    provider.prepare_controller_performance({}, shared, call_kind="semantic_action")
    assert provider._post_json({"messages": []}, timeout_seconds=2.5) == body
    return shared, AgentAutonomyController._controller_performance_snapshot(shared)


def test_semantic_tool_output_metrics_survive_empty_content_without_retaining_arguments(monkeypatch):
    arguments = ['{"city":"北京"}', '{"question":"请保留来源🧭"}']
    shared, snapshot = _record_response(
        monkeypatch,
        {
            "content": None,
            "tool_calls": [
                {"type": "function", "function": {"name": "clarify", "arguments": value}} for value in arguments
            ],
        },
        {"prompt_tokens": 2847, "completion_tokens": 74, "total_tokens": 2921},
    )

    for evidence in (shared, snapshot):
        assert evidence["callKind"] == "semantic_action"
        assert evidence["contentLength"] == evidence["contentBytes"] == 0
        assert evidence["toolCallCount"] == 2
        assert evidence["toolCallArgumentsChars"] == sum(len(value) for value in arguments)
        assert evidence["toolCallArgumentsBytes"] == sum(len(value.encode("utf-8")) for value in arguments)
        assert evidence["tokenUsage"]["completion_tokens"] == 74
        assert evidence["finishReason"] == "tool_calls"
        serialized = json.dumps(evidence, ensure_ascii=False)
        assert "北京" not in serialized
        assert "请保留来源" not in serialized
        assert "test-only-secret" not in serialized


@pytest.mark.parametrize(
    "tool_calls", [None, {}, [None, {"function": None}, {"function": {"arguments": {"secret": 1}}}]]
)
def test_tool_output_metrics_do_not_serialize_malformed_arguments_or_invent_tokens(monkeypatch, tool_calls):
    shared, snapshot = _record_response(monkeypatch, {"content": "普通正文", "tool_calls": tool_calls})

    for evidence in (shared, snapshot):
        assert evidence["toolCallCount"] == (len(tool_calls) if isinstance(tool_calls, list) else 0)
        assert evidence["toolCallArgumentsChars"] == evidence["toolCallArgumentsBytes"] == 0
        assert evidence["tokenUsage"] == {}
        assert evidence["contentLength"] == 4
        assert evidence["contentBytes"] == 12


def test_tool_output_snapshot_sanitizes_counts_and_keeps_unknown_call_kinds_closed():
    snapshot = AgentAutonomyController._controller_performance_snapshot(
        {
            "callKind": "unregistered-kind",
            "toolCallCount": True,
            "toolCallArgumentsChars": -4,
            "toolCallArgumentsBytes": "12",
            "tool_calls": [{"function": {"arguments": "must-not-leak"}}],
            "tokenUsage": {"completion_tokens": False, "prompt_tokens": 0, "total_tokens": "1"},
        }
    )

    assert snapshot["callKind"] == "unknown"
    assert snapshot["toolCallCount"] is None
    assert snapshot["toolCallArgumentsChars"] == 0
    assert snapshot["toolCallArgumentsBytes"] is None
    assert snapshot["tokenUsage"] == {"prompt_tokens": 0}
    assert "must-not-leak" not in json.dumps(snapshot)


def test_tool_output_metrics_do_not_reject_json_escaped_invalid_unicode(monkeypatch):
    shared, snapshot = _record_response(
        monkeypatch, {"tool_calls": [{"function": {"arguments": "\ud800"}}]}, {"completion_tokens": 1}
    )

    for evidence in (shared, snapshot):
        assert evidence["toolCallCount"] == evidence["toolCallArgumentsChars"] == 1
        assert evidence["toolCallArgumentsBytes"] is None
        assert evidence["tokenUsage"]["completion_tokens"] == 1

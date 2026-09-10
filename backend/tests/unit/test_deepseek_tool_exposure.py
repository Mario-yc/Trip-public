from __future__ import annotations

from typing import Any

import pytest

from src.services.deepseek_agent_provider import AgentToolLoopError, DeepSeekAgentProvider


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} test tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


class _Registry:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            _tool("read_itinerary"),
            _tool("patch_itinerary"),
            _tool("web_search"),
            _tool("amap_weather"),
        ]

    def execute(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("No tool should execute in this test")


class _CapturingProvider(DeepSeekAgentProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test-key", model="deepseek-test", timeout_seconds=7)
        self.payloads: list[dict[str, Any]] = []

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "只读取当前行程。"}}]}


class _StopAfterCaptureProvider(_CapturingProvider):
    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        raise RuntimeError("stop_after_payload_capture")


def _payload_tool_names(payload: dict[str, Any]) -> list[str]:
    return [str((item.get("function") or {}).get("name") or "") for item in payload.get("tools") or []]


def test_run_tool_loop_applies_agent_plan_tool_exposure_before_provider_request():
    provider = _CapturingProvider()
    context = {
        "latestUserMessage": "只看当前行程",
        "activeConversationTurns": [],
        "agentPlan": {"allowedTools": ["read_itinerary"]},
    }

    result = provider.run_tool_loop(context, _Registry())

    assert result.reply == "只读取当前行程。"
    assert len(provider.payloads) == 1
    assert _payload_tool_names(provider.payloads[0]) == ["read_itinerary"]
    decision_event = next(item for item in result.tool_events if item.get("toolName") == "agent_decision")
    schema_hashes = decision_event["metadata"]["resultPreview"]["toolSchemaHashes"]
    assert set(schema_hashes) == {"read_itinerary"}


def test_run_tool_loop_keeps_required_tool_exposed_when_plan_is_narrower():
    provider = _StopAfterCaptureProvider()
    context = {
        "latestUserMessage": "只看当前行程",
        "activeConversationTurns": [],
        "agentPlan": {"allowedTools": ["read_itinerary"]},
        "requiredToolSequence": ["patch_itinerary"],
    }

    with pytest.raises(RuntimeError, match="stop_after_payload_capture"):
        provider.run_tool_loop(context, _Registry())

    # A required sequence narrows the current round further: the required
    # writer is exposed by itself until that step succeeds.
    assert _payload_tool_names(provider.payloads[0]) == ["patch_itinerary"]


def test_run_tool_loop_rejects_explicit_unknown_allowlist_before_provider_request():
    provider = _CapturingProvider()
    context = {
        "latestUserMessage": "只看当前行程",
        "activeConversationTurns": [],
        "agentPlan": {"allowedTools": ["not_a_registered_tool"]},
    }

    with pytest.raises(AgentToolLoopError, match="allowedTools did not match any registered tool"):
        provider.run_tool_loop(context, _Registry())

    assert provider.payloads == []


def test_run_tool_loop_rejects_unknown_required_tool_before_provider_request():
    provider = _CapturingProvider()
    context = {
        "latestUserMessage": "只看当前行程",
        "activeConversationTurns": [],
        "agentPlan": {"allowedTools": ["read_itinerary"]},
        "requiredToolSequence": ["not_a_registered_tool"],
    }

    with pytest.raises(AgentToolLoopError, match="requiredToolSequence contains unregistered tool"):
        provider.run_tool_loop(context, _Registry())

    assert provider.payloads == []

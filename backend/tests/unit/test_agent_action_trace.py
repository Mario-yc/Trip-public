from typing import Optional

from src.api.schemas.agent import AgentPlanningEventResponse
from src.services.agent_action_trace import enrich_action_trace_events


def _event(event_type: str, label: str, metadata: Optional[dict] = None) -> AgentPlanningEventResponse:
    return AgentPlanningEventResponse(
        type=event_type,
        label=label,
        status="succeeded",
        metadata=metadata or {},
        timestamp="2026-07-10T00:00:00Z",
    )


def test_action_trace_hides_internal_steps_and_never_exposes_reasoning_content():
    internal = _event("plan", "读取当前 itinerary", {"reasoning_content": "private chain"})
    resolve = _event(
        "resolve_poi",
        "核验地图地点",
        {"resultPreview": {"pendingCount": 1}, "reasoning_content": "private chain"},
    )

    events = enrich_action_trace_events([internal, resolve], goal="安排北京两日游")

    assert events[0].user_visible is False
    assert events[0].category == "internal"
    assert events[1].user_visible is True
    assert events[1].sequence == 1
    assert events[1].effect_summary == "存在 1 组待确认候选，当前不写入时间轴"
    assert "reasoning_content" not in events[1].metadata["actionTrace"]
    assert "reasoning_content" not in events[0].metadata
    assert "reasoning_content" not in events[1].metadata


def test_generic_tool_events_use_real_tool_name_for_action_and_effect():
    event = _event(
        "tool",
        "patch_itinerary",
        {
            "toolName": "patch_itinerary",
            "inputPreview": {"operations": [{"op": "replace_segment_poi", "segmentId": "seg_1"}]},
            "resultPreview": {"activeVersionId": "ver_2", "changedSegmentIds": ["seg_1"]},
        },
    )

    [enriched] = enrich_action_trace_events([event], goal="替换第一天午餐")

    assert enriched.category == "timeline_effect"
    assert enriched.action_label == "写入时间轴"
    assert enriched.goal == "替换第一天午餐"
    assert "ver_2" in enriched.result_summary
    assert "修改 1 个时间轴节点" in enriched.effect_summary

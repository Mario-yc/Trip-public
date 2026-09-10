"""Public, auditable Agent action trace derived from existing planning events.

It deliberately never receives model reasoning content. Existing server and tool
facts become short user-safe summaries; the original technical event remains in
metadata for the debug view.
"""

from __future__ import annotations

from typing import Any


_INTERNAL_TYPES = {
    "plan", "memory", "context", "agent_run", "heartbeat", "session_lease", "context_compaction",
}
_DECISION_TYPES = {
    "normalize_request", "generate_day_slots", "decompose_intent_pools", "initial_day_slot_provider",
    "candidate_hints", "candidate_hints_provider", "timeline_edit", "agent_plan",
    "agent_decision",
}
_VALIDATION_TYPES = {"basic_verifier", "verify", "agent_verifier", "tool_loop_diagnostics"}
_TIMELINE_TYPES = {"patch_itinerary", "create_itinerary_version", "apply_patch", "restore_version"}


def enrich_action_trace_events(events: list[Any], *, goal: str = "") -> list[Any]:
    """Attach normalized public trace fields to Pydantic planning events."""
    visible_sequence = 0
    for event in events:
        event_type = str(getattr(event, "type", "") or "")
        label = str(getattr(event, "label", "") or event_type or "Agent 操作")
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = _without_private_reasoning(metadata)
        setattr(event, "metadata", metadata)
        effective_type = _effective_type(event_type, label, metadata)
        category, user_visible = _category_for(effective_type, label)
        if user_visible:
            visible_sequence += 1
        payload = {
            "sequence": visible_sequence if user_visible else 0,
            "category": category,
            "goal": goal or "完成本轮旅行规划请求",
            "actionLabel": _public_label(effective_type, label),
            "inputSummary": _summary(metadata.get("inputPreview") or metadata.get("inputSummary")),
            "resultSummary": _result_summary(effective_type, metadata, getattr(event, "detail", "")),
            "decisionSummary": _decision_summary(effective_type, metadata),
            "effectSummary": _effect_summary(effective_type, metadata),
            "userVisible": user_visible,
        }
        metadata["actionTrace"] = payload
        event.sequence = payload["sequence"]
        event.category = payload["category"]
        event.goal = payload["goal"]
        event.action_label = payload["actionLabel"]
        event.input_summary = payload["inputSummary"]
        event.result_summary = payload["resultSummary"]
        event.decision_summary = payload["decisionSummary"]
        event.effect_summary = payload["effectSummary"]
        event.user_visible = payload["userVisible"]
    return events


def _category_for(event_type: str, label: str) -> tuple[str, bool]:
    normalized = event_type.lower()
    if normalized in _INTERNAL_TYPES or "读取" in label:
        return "internal", False
    if normalized in _VALIDATION_TYPES or "verif" in normalized:
        return "validation", True
    if normalized in _TIMELINE_TYPES or "patch" in normalized or "version" in normalized:
        return "timeline_effect", True
    if normalized in _DECISION_TYPES:
        return "decision", True
    if normalized in {"resolve_poi", "collect_candidates", "web_search", "amap_weather", "ticket_lookup", "generate_plan_comparison"}:
        return "tool_action", True
    return "tool_result", True


def _effective_type(event_type: str, label: str, metadata: dict[str, Any]) -> str:
    if event_type not in {"tool", "agent", "execution_event"}:
        return event_type
    tool_name = str(metadata.get("toolName") or metadata.get("tool_name") or "").strip()
    return tool_name or label.strip().lower().replace(" ", "_") or event_type


def _public_label(event_type: str, fallback: str) -> str:
    return {
        "normalize_request": "理解本轮目标",
        "initial_day_slot_provider": "制定行程策略",
        "generate_day_slots": "规划每日节奏",
        "decompose_intent_pools": "分配景点与餐饮方向",
        "collect_candidates": "搜索并筛选真实地图候选",
        "resolve_poi": "核验地图地点",
        "patch_itinerary": "写入时间轴",
        "create_itinerary_version": "保存行程版本",
        "basic_verifier": "校验行程结果",
        "deterministic_enrichment": "生成路线与风险信息",
    }.get(event_type, fallback)


def _effect_summary(event_type: str, metadata: dict[str, Any]) -> str:
    preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
    if event_type in _TIMELINE_TYPES or "patch" in event_type:
        changed = preview.get("changedSegmentIds") or preview.get("changedSegmentId") or []
        version = preview.get("activeVersionId") or preview.get("versionId")
        chunks: list[str] = []
        if changed:
            chunks.append(f"修改 {len(changed) if isinstance(changed, list) else 1} 个时间轴节点")
        if version:
            chunks.append(f"新版本 {version}")
        return "；".join(chunks) or "未发现版本或节点变更证据，本步未写入时间轴"
    if event_type == "resolve_poi":
        if preview.get("pendingCount"):
            return f"存在 {preview['pendingCount']} 组待确认候选，当前不写入时间轴"
        if preview.get("resolvedCount"):
            return f"已确认 {preview['resolvedCount']} 个地图地点"
    return "本步未直接修改时间轴"


def _decision_summary(event_type: str, metadata: dict[str, Any]) -> str:
    preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
    if event_type == "initial_day_slot_provider" and preview.get("providerReturnedMode"):
        return f"执行模式：{preview['providerReturnedMode']}"
    if event_type == "resolve_poi" and preview.get("pendingCount"):
        return "候选存在歧义，等待用户选择；不构造地图地点、不修改版本"
    if event_type == "resolve_poi" and preview.get("resolvedCount"):
        return "地点已由地图结果确认，可继续执行受版本保护的最小修改"
    if event_type == "patch_itinerary" and preview.get("activeVersionId"):
        return "时间轴已写入新版本，下一步校验地点、路线与版本约束"
    if event_type in _VALIDATION_TYPES and preview.get("passed") is True:
        return "校验通过，本轮可以安全结束"
    if preview.get("nextAction"):
        return f"下一步：{preview['nextAction']}"
    return "根据当前工具结果继续下一步"


def _result_summary(event_type: str, metadata: dict[str, Any], detail: Any) -> str:
    preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
    if event_type == "resolve_poi":
        return _summary(
            {
                "resolvedCount": preview.get("resolvedCount", 0),
                "pendingCount": preview.get("pendingCount", 0),
                "candidateCount": preview.get("candidateCount")
                or sum(int(item.get("candidateCount") or 0) for item in preview.get("pending", []) if isinstance(item, dict)),
            }
        )
    if event_type == "patch_itinerary" and preview.get("activeVersionId"):
        changed = preview.get("changedSegmentIds") or []
        return f"写入版本 {preview['activeVersionId']}；修改 {len(changed)} 个时间轴节点"
    return _summary(metadata.get("resultSummary") or metadata.get("outputSummary") or detail)


def _summary(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        return " · ".join(
            f"{key}={value[key]}" for key in ("query", "city", "category", "resolvedCount", "pendingCount", "candidateCount") if key in value
        )[:500]
    return str(value)[:500]


def _without_private_reasoning(value: Any) -> Any:
    """Remove provider-private reasoning recursively before API persistence/copy."""
    if isinstance(value, dict):
        return {
            str(key): _without_private_reasoning(item)
            for key, item in value.items()
            if str(key).casefold() not in {"reasoning_content", "reasoningcontent", "chain_of_thought", "chainofthought"}
        }
    if isinstance(value, list):
        return [_without_private_reasoning(item) for item in value]
    return value

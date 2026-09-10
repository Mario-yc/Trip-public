from __future__ import annotations

import json
from typing import Any, Optional


DEFAULT_TRACE_STAGES = ("plan", "resolve_poi", "apply_patch", "verify", "respond")


def replay_trace(
    events: list[dict[str, Any]],
    expected_stages: Optional[list[str]] = None,
    allowed_failed_stages: Optional[list[str]] = None,
) -> dict[str, Any]:
    stages = list(DEFAULT_TRACE_STAGES) if expected_stages is None else expected_stages
    allowed_failures = set(allowed_failed_stages or [])
    observed = [_event_stage(event) for event in events if _event_stage(event)]
    stage_counts = {stage: observed.count(stage) for stage in stages}
    missing = [stage for stage in stages if stage_counts.get(stage, 0) == 0]
    out_of_order = _out_of_order(observed, stages)
    failed = [
        {
            "type": _event_stage(event),
            "label": event.get("label"),
            "status": event.get("status"),
            "failureReason": event.get("failureReason"),
        }
        for event in events
        if event.get("status") == "failed"
        and _event_stage(event) in stages
        and _event_stage(event) not in allowed_failures
    ]
    return {
        "passed": not missing and not out_of_order and not failed,
        "expectedStages": stages,
        "events": events,
        "observedStages": observed,
        "stageCounts": stage_counts,
        "missingStages": missing,
        "outOfOrder": out_of_order,
        "failedStages": failed,
        "durationMs": sum(int(event.get("durationMs") or 0) for event in events),
    }


def extract_trace_events(source: Any) -> list[dict[str, Any]]:
    if source is None:
        return []
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except json.JSONDecodeError:
            return []
    if hasattr(source, "keys") and "agent_response_json" in source.keys():
        return extract_trace_events(source["agent_response_json"])
    if not isinstance(source, dict):
        return []
    events = source.get("planningSteps")
    if not isinstance(events, list) or not events:
        events = source.get("toolEvents")
    return [event for event in events or [] if isinstance(event, dict)]


def _event_stage(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "").strip()
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    if not event_type:
        return str(
            event.get("toolName")
            or event.get("id")
            or metadata.get("toolName")
            or metadata.get("tool_name")
            or ""
        ).strip()
    if event_type not in {"tool", "agent", "execution_event"}:
        return event_type
    return str(
        event.get("toolName")
        or event.get("id")
        or metadata.get("toolName")
        or metadata.get("tool_name")
        or event_type
    ).strip()


def replay_agent_response(
    source: Any,
    expected_stages: Optional[list[str]] = None,
    allowed_failed_stages: Optional[list[str]] = None,
) -> dict[str, Any]:
    events = extract_trace_events(source)
    report = replay_trace(events, expected_stages, allowed_failed_stages)
    tool_events = [event for event in events if event.get("type") == "tool" or event.get("toolName")]
    verifier_events = [event for event in events if event.get("type") == "verify"]
    return {
        "passed": report["passed"],
        "eventCount": len(events),
        "toolCallCount": len(tool_events),
        "failedToolCallCount": sum(1 for event in tool_events if event.get("status") == "failed"),
        "verifierPassed": all((event.get("metadata") or {}).get("passed") is not False for event in verifier_events),
        "missingStages": report["missingStages"],
        "outOfOrder": report["outOfOrder"],
        "failedStages": report["failedStages"],
        "expectedStages": report["expectedStages"],
        "stageCounts": report["stageCounts"],
    }


def _out_of_order(observed: list[str], expected: list[str]) -> list[dict[str, Any]]:
    last_index = -1
    problems = []
    for stage in observed:
        if stage not in expected:
            continue
        index = expected.index(stage)
        if index < last_index:
            problems.append({"stage": stage, "expectedAfter": expected[last_index]})
            continue
        last_index = index
    return problems

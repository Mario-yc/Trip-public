from __future__ import annotations

from typing import Any


def normalize_persisted_planning_events(events: Any, fallback_timestamp: str) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            event["type"] = "legacy_event"
        label = event.get("label")
        if not isinstance(label, str) or not label.strip():
            event["label"] = str(event.get("type") or f"event_{index + 1}")
        status = event.get("status")
        if not isinstance(status, str) or not status.strip():
            event["status"] = "completed"
        detail = event.get("detail")
        if not isinstance(detail, str):
            event["detail"] = "" if detail is None else str(detail)
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            event["metadata"] = {}
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp.strip():
            event["timestamp"] = fallback_timestamp
        normalized.append(event)
    return normalized

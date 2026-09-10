from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any, Optional, Protocol
from urllib.error import HTTPError, URLError

from fastapi import HTTPException


@dataclass(frozen=True)
class AgentRuntimeLimits:
    max_tool_rounds: int = 5
    max_tool_calls_per_round: int = 3
    max_context_turns: int = 12
    max_context_chars: int = 24000
    max_tool_result_chars: int = 12000
    max_run_seconds: int = 45
    max_tool_seconds: int = 20
    max_patch_seconds: int = 12


@dataclass(frozen=True)
class AgentRunContext:
    session: Any
    request_context: dict[str, Any]
    source_turn_id: Optional[str]
    active_version_id: Optional[str]
    preference_memory: Any = None
    runtime_limits: AgentRuntimeLimits = field(default_factory=AgentRuntimeLimits)
    started_monotonic: float = field(default_factory=time.monotonic)

    def provider_context(self) -> dict[str, Any]:
        context = compact_agent_context(self.request_context, self.runtime_limits)
        context["runtimeLimits"] = {
            "maxToolRounds": self.runtime_limits.max_tool_rounds,
            "maxToolCallsPerRound": self.runtime_limits.max_tool_calls_per_round,
            "maxContextTurns": self.runtime_limits.max_context_turns,
            "maxRunSeconds": self.runtime_limits.max_run_seconds,
            "maxToolSeconds": self.runtime_limits.max_tool_seconds,
            "maxPatchSeconds": self.runtime_limits.max_patch_seconds,
        }
        context["runtimeDeadlineMonotonic"] = self.started_monotonic + self.runtime_limits.max_run_seconds
        return context


@dataclass(frozen=True)
class AgentEvent:
    type: str
    label: str
    status: str
    detail: str = ""
    provider_name: Optional[str] = None
    fallback_used: bool = False
    failure_reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentProviderProtocol(Protocol):
    def generate(self, context: dict[str, Any]) -> str:
        ...


@dataclass(frozen=True)
class ProviderErrorInfo:
    category: str
    retryable: bool
    should_compact: bool
    user_message: str


def compact_agent_context(context: dict[str, Any], limits: AgentRuntimeLimits) -> dict[str, Any]:
    compacted = dict(context)
    turns = compacted.get("activeConversationTurns")
    if isinstance(turns, list) and len(turns) > limits.max_context_turns:
        old_turns = turns[: -limits.max_context_turns]
        compacted["compactedTurnSummary"] = _summarize_turns(old_turns)
        compacted["activeConversationTurns"] = turns[-limits.max_context_turns :]

    if _json_length(compacted) <= limits.max_context_chars:
        return compacted

    for key in ("candidateMapPois", "pendingAmapPoiCandidates", "selectedSkills"):
        if key in compacted:
            compacted[key] = _trim_value(compacted[key], max_items=8, max_string=800)
    if isinstance(compacted.get("skillContext"), str):
        compacted["skillContext"] = _trim_string(compacted["skillContext"], 1200)

    for key in ("timelineContext", "currentItinerarySnapshot"):
        value = compacted.get(key)
        if isinstance(value, dict) and _json_length(compacted) > limits.max_context_chars:
            compacted[key] = _compact_itinerary_like(value)

    if _json_length(compacted) > limits.max_context_chars:
        compacted["contextCompactionWarning"] = "Agent context was compacted to preserve current request, itinerary identity, and required tool contract."
    return compacted


class ProviderCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 30):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state: dict[str, tuple[int, float]] = {}

    def assert_allowed(self, key: str) -> None:
        failures, opened_at = self._state.get(key, (0, 0.0))
        if failures < self.failure_threshold:
            return
        if time.monotonic() - opened_at >= self.cooldown_seconds:
            self._state.pop(key, None)
            return
        raise HTTPException(status_code=503, detail=f"Provider circuit is open for {key}; please retry later.")

    def record_success(self, key: str) -> None:
        self._state.pop(key, None)

    def record_failure(self, key: str) -> None:
        failures, _opened_at = self._state.get(key, (0, 0.0))
        next_failures = failures + 1
        opened_at = time.monotonic() if next_failures >= self.failure_threshold else 0.0
        self._state[key] = (next_failures, opened_at)


def classify_provider_error(error: Exception) -> ProviderErrorInfo:
    text = str(error)
    lowered = text.lower()
    status_code = None
    if isinstance(error, HTTPException):
        status_code = error.status_code
        text = str(error.detail)
        lowered = text.lower()
    elif isinstance(error, HTTPError):
        status_code = error.code
    else:
        status_code = getattr(error, "status_code", None)
    if isinstance(error, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return ProviderErrorInfo("timeout", True, False, "模型服务请求超时，行程未被覆盖。")
    if status_code in {401, 403} or "api_key" in lowered or "authorization" in lowered:
        return ProviderErrorInfo("auth", False, False, "模型服务凭据不可用，行程未被覆盖。")
    if status_code == 400:
        return ProviderErrorInfo("bad_request", False, False, "模型服务请求格式被拒绝，行程未被覆盖。")
    if status_code == 429 or "rate limit" in lowered:
        return ProviderErrorInfo("rate_limit", True, False, "模型服务限流，行程未被覆盖。")
    if status_code and int(status_code) >= 500:
        return ProviderErrorInfo("provider_server", True, False, "模型服务暂时不可用，行程未被覆盖。")
    if isinstance(error, URLError) or "network" in lowered:
        return ProviderErrorInfo("network", True, False, "网络请求失败，行程未被覆盖。")
    if "context" in lowered and ("long" in lowered or "length" in lowered or "too large" in lowered):
        return ProviderErrorInfo("context_too_long", True, True, "上下文过长，已压缩后可重试。")
    if isinstance(error, JSONDecodeError) or "json" in lowered:
        return ProviderErrorInfo("invalid_json", False, False, "模型返回不是合法 JSON，行程未被覆盖。")
    return ProviderErrorInfo("unknown", False, False, "模型服务调用失败，行程未被覆盖。")


def _summarize_turns(turns: list[Any]) -> dict[str, Any]:
    previews = []
    for turn in turns[-4:]:
        if not isinstance(turn, dict):
            continue
        previews.append(
            {
                "role": turn.get("role"),
                "turnIndex": turn.get("turnIndex"),
                "itineraryVersionId": turn.get("itineraryVersionId"),
                "contentPreview": _trim_string(str(turn.get("content") or ""), 160),
            }
        )
    return {"compactedTurnCount": len(turns), "recentCompactedTurns": previews}


def _compact_itinerary_like(value: dict[str, Any]) -> dict[str, Any]:
    plan = value.get("itineraryPlan") if isinstance(value.get("itineraryPlan"), dict) else value
    days = plan.get("days") if isinstance(plan, dict) else []
    compact_days = []
    if isinstance(days, list):
        for day in days[:5]:
            if not isinstance(day, dict):
                continue
            segments = day.get("segments") if isinstance(day.get("segments"), list) else []
            compact_days.append(
                {
                    "id": day.get("id"),
                    "dayNumber": day.get("dayNumber"),
                    "title": day.get("title"),
                    "date": day.get("date"),
                    "segments": [
                        {
                            "id": segment.get("id"),
                            "startTime": segment.get("startTime"),
                            "poiName": (segment.get("poi") or {}).get("name") if isinstance(segment.get("poi"), dict) else segment.get("poiName"),
                            "transportMode": segment.get("transportMode"),
                        }
                        for segment in segments[:8]
                        if isinstance(segment, dict)
                    ],
                }
            )
    return {
        "activeVersionId": value.get("activeVersionId") or plan.get("activeVersionId") if isinstance(plan, dict) else value.get("activeVersionId"),
        "id": plan.get("id") if isinstance(plan, dict) else value.get("id"),
        "title": plan.get("title") if isinstance(plan, dict) else value.get("title"),
        "city": plan.get("city") if isinstance(plan, dict) else value.get("city"),
        "days": compact_days,
        "compacted": True,
    }


def _trim_value(value: Any, max_items: int, max_string: int, depth: int = 0) -> Any:
    if depth >= 4:
        return "..."
    if isinstance(value, dict):
        return {str(key): _trim_value(item, max_items, max_string, depth + 1) for key, item in list(value.items())[:max_items]}
    if isinstance(value, list):
        items = [_trim_value(item, max_items, max_string, depth + 1) for item in value[:max_items]]
        if len(value) > max_items:
            items.append(f"... compacted {len(value) - max_items} items")
        return items
    if isinstance(value, str):
        return _trim_string(value, max_string)
    return value


def _trim_string(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else value[: max(0, max_chars - 3)].rstrip() + "..."


def _json_length(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))

"""User-facing execution progress derived from real Agent events.

The projector deliberately consumes only server execution metadata that has
already crossed the public action-trace boundary.  It never receives or
forwards provider reasoning tokens, prompts, messages, or hidden chain of
thought.  Repeated low-level events are coalesced into a short phase timeline
that can be streamed independently from the final assistant answer.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4


_PRIVATE_KEY_TOKENS = {
    "accesstoken",
    "analysis",
    "apikey",
    "authorization",
    "chainofthought",
    "cot",
    "hiddenprompt",
    "internalprompt",
    "prompt",
    "reasoning",
    "reasoningcontent",
    "reasoningtext",
    "systemprompt",
    "secret",
    "thinking",
    "thought",
    "token",
}

_PUBLIC_TERMINAL_DETAIL_BY_CODE = {
    "agent_internal_error": "行程规划暂时未完成，请稍后重试。",
    "agent_rate_limited": "当前服务请求较多，请稍后重试。",
    "agent_request_forbidden": "当前请求不能在此会话中执行。",
    "agent_request_invalid": "当前请求格式不完整，请检查后重试。",
    "agent_request_rejected": "当前请求未能安全执行，请检查输入后重试。",
    "agent_resource_not_found": "当前会话所需的信息不存在或已失效。",
    "agent_run_cancelled": "本轮处理已停止。",
    "agent_state_conflict": "当前行程状态已变化，请刷新后重试。",
    "plan_proposal_route_quality_failed": "所选方案未通过真实路线质量校验，未创建正式行程。",
    "route_provider_unavailable": "路线服务暂时不可用",
}

_PHASE_ORDER = {
    "understanding": 1,
    "constraints": 2,
    "places": 3,
    "feasibility": 4,
    "result": 5,
}


class AgentExecutionEventSanitizer:
    """Run-scoped redaction for public execution events and response payloads."""

    def __init__(self) -> None:
        self._private_values: set[str] = set()

    def observe(self, event: Any) -> None:
        payload = _event_dict(event)
        self._private_values.update(_private_strings(payload))

    def sanitize(self, event: Any) -> dict[str, Any]:
        payload = _event_dict(event)
        self._private_values.update(_private_strings(payload))
        private_values = tuple(sorted(self._private_values, key=len, reverse=True))
        sanitized = _without_private_reasoning(payload, private_values)
        return sanitized if isinstance(sanitized, dict) else {}


class AgentReasoningStatusProjector:
    """Coalesce real execution events into a safe, user-readable timeline."""

    MAX_VISIBLE_ITEMS = 5

    def __init__(self, *, session_id: str, turn_id: str = "", run_id: str = "") -> None:
        self.session_id = str(session_id or "")
        self.turn_id = str(turn_id or "")
        self.root_user_turn_id = ""
        self.assistant_turn_id = ""
        self.run_id = str(run_id or f"reasoning_run_{uuid4().hex}")
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._started_monotonic = time.monotonic()
        self.statuses: list[dict[str, Any]] = []
        # Retain only the recursively sanitized event shape for diagnostics.
        # Provider-private text is never kept on this object.
        self.raw_events: list[dict[str, Any]] = []
        self._index_by_key: dict[str, int] = {}
        self._workers_by_key: dict[str, dict[str, str]] = {}
        self._latest_source_sequence_by_key: dict[str, int] = {}
        self._event_sanitizer = AgentExecutionEventSanitizer()
        self._next_sequence = 1
        self._next_item_id = 1
        self._finished = False
        self._terminal_status: Optional[dict[str, Any]] = None
        self._max_phase_order = 0

    def bind_turn(self, turn_id: str, *, role: str = "") -> None:
        if str(turn_id or "").strip():
            self.turn_id = str(turn_id)
            normalized_role = str(role or "").casefold()
            if normalized_role == "assistant":
                self.assistant_turn_id = self.turn_id
            elif normalized_role == "user" or not self.root_user_turn_id:
                self.root_user_turn_id = self.turn_id
            for status in self.statuses:
                status["turnId"] = self.turn_id
                status["rootUserTurnId"] = self.root_user_turn_id or None
                status["assistantTurnId"] = self.assistant_turn_id or None

    def sanitize_event(self, event: Any) -> dict[str, Any]:
        """Sanitize one event while retaining run-local private echo knowledge."""

        return self._event_sanitizer.sanitize(event)

    def begin(self) -> dict[str, Any]:
        """Publish the request-accepted fact before any Provider worker starts."""

        status = self.consume(
            {
                "type": "normalize_request",
                "status": "running",
                "label": "正在理解需求",
                "userVisible": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {"phase": "request_accepted"},
            }
        )
        assert status is not None
        return status

    def consume(self, event: Any) -> Optional[dict[str, Any]]:
        if self._finished:
            return None
        payload = _event_dict(event)
        if not payload:
            return None
        payload = self.sanitize_event(payload)
        self.raw_events.append(copy.deepcopy(payload))
        if payload.get("turnId"):
            self.bind_turn(str(payload["turnId"]))
        projected = _project_event(payload)
        if projected is None:
            return None
        phase_order = _PHASE_ORDER.get(str(projected.get("phase") or ""), 0)
        if phase_order < self._max_phase_order:
            return None
        self._max_phase_order = max(self._max_phase_order, phase_order)
        key = str(projected.pop("semanticKey"))
        worker_id, parent_execution_id = _worker_identity(payload)
        if parent_execution_id:
            key = f"{key}:parent:{parent_execution_id}"
        worker_count: Optional[int] = None
        completed_worker_count: Optional[int] = None
        if worker_id:
            workers = self._workers_by_key.setdefault(key, {})
            workers[worker_id] = str(projected["status"])
            worker_count = len(workers)
            completed_worker_count = sum(value != "running" for value in workers.values())
            if worker_count > 1:
                projected["summary"] = _parallel_worker_summary(
                    str(projected["phase"]),
                    workers,
                )
        timestamp = str(payload.get("timestamp") or datetime.now(timezone.utc).isoformat())
        explicit_sequence = payload.get("sequence")
        source_sequence = (
            int(explicit_sequence)
            if isinstance(explicit_sequence, int) and not isinstance(explicit_sequence, bool) and explicit_sequence > 0
            else self._next_sequence
        )
        prior_source_sequence = self._latest_source_sequence_by_key.get(key, 0)
        if source_sequence <= prior_source_sequence:
            return None
        prior_index = self._index_by_key.get(key)
        prior = self.statuses[prior_index] if prior_index is not None else None
        if prior is not None and not _status_transition_allowed(
            str(prior.get("status") or ""), str(projected["status"])
        ):
            return None
        sequence = source_sequence
        self._next_sequence = max(self._next_sequence, sequence + 1)
        self._latest_source_sequence_by_key[key] = source_sequence
        elapsed_ms = max(0, int((time.monotonic() - self._started_monotonic) * 1000))
        status = {
            "messageType": "reasoning_status",
            "id": self._allocate_item_id(),
            "sequence": sequence,
            "runId": self.run_id,
            "semanticKey": key,
            "phase": projected["phase"],
            "status": projected["status"],
            "summary": projected["summary"],
            "detail": projected.get("detail") or None,
            "sourceEventType": str(payload.get("type") or "execution_event"),
            "sessionId": self.session_id or None,
            "turnId": self.turn_id or None,
            "rootUserTurnId": self.root_user_turn_id or None,
            "assistantTurnId": self.assistant_turn_id or None,
            "startedAt": self.started_at,
            "completedAt": timestamp if projected["status"] != "running" else None,
            "elapsedMs": elapsed_ms,
            "firstSequence": int(prior.get("firstSequence") or prior.get("sequence") or sequence)
            if prior is not None
            else sequence,
            "latestSequence": sequence,
            "timestamp": timestamp,
        }
        if worker_count is not None:
            status["workerCount"] = worker_count
            status["completedWorkerCount"] = completed_worker_count
        if prior_index is not None:
            assert prior is not None
            status["id"] = prior["id"]
            self.statuses[prior_index] = status
        else:
            self._index_by_key[key] = len(self.statuses)
            self.statuses.append(status)
            self._trim_visible_items()
        return copy.deepcopy(status)

    def _allocate_item_id(self) -> str:
        item_id = f"reasoning_{_safe_identifier(self.session_id or 'session')}_{self._next_item_id}"
        self._next_item_id += 1
        return item_id

    def _trim_visible_items(self) -> None:
        if len(self.statuses) <= self.MAX_VISIBLE_ITEMS:
            return
        removable_index = next(
            (
                index
                for index, item in enumerate(self.statuses)
                if item.get("status") != "running" and item.get("semanticKey") != "result_assembly"
            ),
            0,
        )
        self.statuses.pop(removable_index)
        self._index_by_key = {str(item.get("semanticKey") or ""): index for index, item in enumerate(self.statuses)}

    def finish(self, terminal: str, *, detail: str = "") -> dict[str, Any]:
        if self._terminal_status is not None:
            return copy.deepcopy(self._terminal_status)
        normalized = str(terminal or "completed").casefold()
        if normalized in {"cancelled", "canceled", "stopped", "interrupted"}:
            status = "cancelled"
            summary = "方案整理已停止"
        elif normalized in {"failed", "error"}:
            status = "failed"
            summary = "方案结果未完成"
        elif normalized in {"needs_confirmation", "waiting", "awaiting_confirmation"}:
            status = "completed"
            summary = "等待你确认关键约束"
        else:
            status = "completed"
            summary = "方案结果已整理"
        # A terminal response must never retain an earlier spinner.  Completed
        # and fallback facts stay available for the disclosure; an unfinished
        # item is superseded by the single sanitized terminal summary.
        self.statuses = [item for item in self.statuses if item.get("status") != "running"]
        self._index_by_key = {str(item.get("semanticKey") or ""): index for index, item in enumerate(self.statuses)}
        prior_index = self._index_by_key.get("result_assembly")
        prior = self.statuses[prior_index] if prior_index is not None else None
        terminal_status = {
            "messageType": "reasoning_status",
            "id": prior["id"] if prior is not None else self._allocate_item_id(),
            "sequence": self._next_sequence,
            "runId": self.run_id,
            "semanticKey": "result_assembly",
            "phase": "result",
            "status": status,
            "summary": summary,
            "detail": _public_terminal_detail(detail),
            "sourceEventType": "agent_run_terminal",
            "sessionId": self.session_id or None,
            "turnId": self.turn_id or None,
            "rootUserTurnId": self.root_user_turn_id or None,
            "assistantTurnId": self.assistant_turn_id or None,
            "startedAt": self.started_at,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "elapsedMs": max(0, int((time.monotonic() - self._started_monotonic) * 1000)),
            "firstSequence": int(prior.get("firstSequence") or prior.get("sequence") or self._next_sequence)
            if prior is not None
            else self._next_sequence,
            "latestSequence": self._next_sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._next_sequence += 1
        if prior_index is not None:
            self.statuses[prior_index] = terminal_status
        else:
            self._index_by_key["result_assembly"] = len(self.statuses)
            self.statuses.append(terminal_status)
        self._trim_visible_items()
        self._finished = True
        self._terminal_status = copy.deepcopy(terminal_status)
        return copy.deepcopy(self._terminal_status)


def reasoning_statuses_from_events(
    events: Iterable[Any],
    *,
    session_id: str = "",
    turn_id: str = "",
    terminal: str = "completed",
) -> list[dict[str, Any]]:
    projector = AgentReasoningStatusProjector(session_id=session_id, turn_id=turn_id)
    for event in events:
        projector.consume(event)
    projector.finish(terminal)
    return copy.deepcopy(projector.statuses)


def persist_reasoning_statuses(
    db: sqlite3.Connection,
    turn_id: str,
    statuses: list[dict[str, Any]],
) -> None:
    """Persist reconnect-safe progress without committing unrelated work."""

    if not str(turn_id or "").strip():
        return
    row = db.execute(
        "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
        (str(turn_id),),
    ).fetchone()
    if row is None:
        return
    try:
        payload = json.loads(row["agent_response_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["reasoningStatuses"] = [
        sanitize_execution_event(copy.deepcopy(item)) for item in statuses if isinstance(item, dict)
    ]
    transaction_was_open = bool(db.in_transaction)
    db.execute(
        "UPDATE conversation_turns SET agent_response_json = ?, updated_at = ? WHERE id = ?",
        (
            json.dumps(payload, ensure_ascii=False, default=str),
            datetime.now(timezone.utc).isoformat(),
            str(turn_id),
        ),
    )
    if not transaction_was_open:
        db.commit()


def reasoning_status_snapshot(
    db: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str = "",
    after_sequence: int = 0,
    active: bool = False,
    active_turn_id: str = "",
) -> dict[str, Any]:
    """Return a read-only, cursor-based snapshot for stream recovery.

    During execution statuses live on the durable user turn.  Once the final
    assistant turn exists, the same timeline moves to that turn.  Resolving
    both locations behind one endpoint lets a browser reconnect without
    replaying the user request or starting another Agent run.
    """

    rows = db.execute(
        """
        SELECT id, role, parent_turn_id, turn_index, agent_response_json
        FROM conversation_turns
        WHERE session_id = ? AND status != 'superseded'
        ORDER BY turn_index DESC, created_at DESC
        """,
        (str(session_id),),
    ).fetchall()
    selected: Optional[sqlite3.Row] = None
    resolved_source_user_id = ""
    explicit_requested = str(turn_id or "").strip()
    active_requested = str(active_turn_id or "").strip() if active and not explicit_requested else ""
    requested = explicit_requested or active_requested
    if active_requested:
        resolved_source_user_id = active_requested
    if requested:
        requested_row = next((row for row in rows if str(row["id"]) == requested), None)
        if requested_row is not None and str(requested_row["role"]) == "user":
            resolved_source_user_id = requested
            requested_index = int(requested_row["turn_index"] or 0)
            next_user_index = min(
                (
                    int(row["turn_index"] or 0)
                    for row in rows
                    if str(row["role"]) == "user" and int(row["turn_index"] or 0) > requested_index
                ),
                default=None,
            )
            assistant_candidates = [
                row
                for row in rows
                if str(row["role"]) == "assistant"
                and int(row["turn_index"] or 0) > requested_index
                and (next_user_index is None or int(row["turn_index"] or 0) < next_user_index)
                and _row_reasoning_statuses(row)
            ]
            selected = (
                min(assistant_candidates, key=lambda row: int(row["turn_index"] or 0))
                if assistant_candidates
                else requested_row
            )
        else:
            selected = requested_row
    if selected is None and not requested and not active:
        selected = next((row for row in rows if _row_reasoning_statuses(row)), None)

    statuses = _row_reasoning_statuses(selected) if selected is not None else []
    statuses.sort(key=lambda item: int(item.get("sequence") or 0))
    next_sequence = max((int(item.get("sequence") or 0) for item in statuses), default=0)
    visible = [item for item in statuses if int(item.get("sequence") or 0) > max(0, int(after_sequence))]
    terminal_status = next(
        (
            str(item.get("status") or "")
            for item in reversed(statuses)
            if str(item.get("semanticKey") or "") == "result_assembly"
        ),
        None,
    )
    role = str(selected["role"] or "") if selected is not None else ""
    selected_id = str(selected["id"] or "") if selected is not None else ""
    parent_turn_id = str(selected["parent_turn_id"] or "") if selected is not None else ""
    if role == "user":
        resolved_source_user_id = selected_id
    elif role == "assistant" and not resolved_source_user_id:
        resolved_source_user_id = parent_turn_id or _preceding_user_turn_id(rows, selected)
    return {
        "sessionId": str(session_id),
        "sourceUserTurnId": resolved_source_user_id or None,
        "assistantTurnId": selected_id if role == "assistant" else None,
        "statuses": visible,
        "active": bool(active),
        "nextSequence": next_sequence,
        "terminalStatus": terminal_status,
    }


def _preceding_user_turn_id(
    rows: list[sqlite3.Row],
    selected: Optional[sqlite3.Row],
) -> str:
    if selected is None:
        return ""
    selected_index = int(selected["turn_index"] or 0)
    candidates = [row for row in rows if str(row["role"]) == "user" and int(row["turn_index"] or 0) < selected_index]
    if not candidates:
        return ""
    nearest = max(candidates, key=lambda row: int(row["turn_index"] or 0))
    return str(nearest["id"] or "")


def clear_reasoning_statuses(db: sqlite3.Connection, turn_id: str) -> None:
    if not str(turn_id or "").strip():
        return
    row = db.execute(
        "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
        (str(turn_id),),
    ).fetchone()
    if row is None:
        return
    try:
        payload = json.loads(row["agent_response_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or "reasoningStatuses" not in payload:
        return
    payload.pop("reasoningStatuses", None)
    transaction_was_open = bool(db.in_transaction)
    db.execute(
        "UPDATE conversation_turns SET agent_response_json = ?, updated_at = ? WHERE id = ?",
        (
            json.dumps(payload, ensure_ascii=False, default=str),
            datetime.now(timezone.utc).isoformat(),
            str(turn_id),
        ),
    )
    if not transaction_was_open:
        db.commit()


def _project_event(payload: dict[str, Any]) -> Optional[dict[str, str]]:
    event_type = str(payload.get("type") or "").casefold()
    status = _public_status(payload.get("status"))
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    action_trace = metadata.get("actionTrace") if isinstance(metadata.get("actionTrace"), dict) else {}
    category = str(payload.get("category") or action_trace.get("category") or "").casefold()
    if event_type in {"heartbeat", "session_lease"} or category == "internal" or payload.get("userVisible") is False:
        return None

    if event_type in {
        "normalize_request",
        "parse_request",
        "understand_request",
        "context",
        "memory",
        "read_itinerary",
        "agent_run",
    }:
        return _status(
            "understanding",
            status,
            _event_summary(payload, action_trace, status, "正在理解需求", "已解析本轮需求与上下文"),
            "request_understanding",
        )
    if "route" in event_type:
        summary = "正在校验路线与可行性" if status == "running" else "已完成路线与可行性检查"
        summary = _event_summary(payload, action_trace, status, summary, summary)
        return _status("feasibility", status, summary, "route_feasibility")
    if (
        event_type
        in {
            "agent_decision",
            "initial_day_slot_provider",
            "generate_day_slots",
            "decompose_intent_pools",
            "view_context_resolved",
        }
        or (
            category == "decision"
            and event_type
            not in {
                "collect_candidates",
                "resolve_poi",
                "web_search",
                "amap_weather",
                "ticket_lookup",
            }
        )
    ):
        summary = _event_summary(payload, action_trace, status, "正在核对请求与执行约束", "已确认本轮执行策略")
        return _status("constraints", status, summary, "constraint_confirmation")
    if event_type in {
        "collect_candidates",
        "resolve_poi",
        "web_search",
        "amap_weather",
        "ticket_lookup",
    }:
        count = _candidate_count(metadata, action_trace)
        if status == "completed" and count is not None:
            summary = f"已找到 {count} 个候选，正在比较"
        elif event_type == "resolve_poi":
            summary = "正在核验真实地图地点"
        else:
            summary = "正在调用搜索工具"
        if not (status == "completed" and count is not None):
            summary = _event_summary(payload, action_trace, status, summary, summary)
        return _status("places", status, summary, "poi_verification")
    if category == "validation" or "verif" in event_type:
        summary = "正在校验方案完整性" if status == "running" else "已完成方案完整性校验"
        summary = _event_summary(payload, action_trace, status, summary, summary)
        return _status("feasibility", status, summary, "route_feasibility")
    if category in {"tool_action", "tool_result"}:
        label = str(action_trace.get("actionLabel") or payload.get("actionLabel") or payload.get("label") or "工具")
        summary = f"正在{label}" if status == "running" and not label.startswith("正在") else label
        return _status("places", status, summary[:80], "poi_verification")
    if category == "timeline_effect":
        summary = "正在安全更新行程" if status == "running" else "已完成行程更新校验"
        summary = _event_summary(payload, action_trace, status, summary, summary)
        return _status("result", status, summary, "result_assembly")
    if payload.get("userVisible") is True:
        summary = str(action_trace.get("actionLabel") or payload.get("label") or "正在处理当前请求")[:80]
        summary = _event_summary(payload, action_trace, status, summary, summary)
        return _status("result", status, summary, "result_assembly")
    return None


def _status(phase: str, status: str, summary: str, key: str) -> dict[str, str]:
    return {
        "phase": phase,
        "status": status,
        "summary": summary,
        "semanticKey": key,
    }


def _status_transition_allowed(previous: str, incoming: str) -> bool:
    if previous == incoming:
        return True
    if previous == "running":
        return incoming in {"completed", "fallback", "failed", "cancelled"}
    if previous == "fallback":
        return incoming == "completed"
    return False


def _event_summary(
    payload: dict[str, Any],
    action_trace: dict[str, Any],
    status: str,
    running_fallback: str,
    completed_fallback: str,
) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidates = (
        action_trace.get("decisionSummary"),
        action_trace.get("resultSummary"),
        action_trace.get("effectSummary"),
        payload.get("decisionSummary"),
        payload.get("resultSummary"),
        payload.get("effectSummary"),
        action_trace.get("actionLabel"),
        payload.get("actionLabel"),
        payload.get("goal"),
        metadata.get("userVisibleSummary"),
    )
    for candidate in candidates:
        summary = _clean_public_summary(candidate)
        if summary:
            return summary
    return running_fallback if status == "running" else completed_fallback


def _clean_public_summary(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if re.search(
        r"(?:sql|traceback|fingerprint|checkpoint|token|prompt|stack|providerRaw|candidateCount=|[a-f0-9]{24,})",
        text,
        re.I,
    ):
        return ""
    return text[:120]


def _public_terminal_detail(value: Any) -> Optional[str]:
    """Map a server-owned error code to a bounded public terminal detail.

    The terminal projector is a public persistence/transport boundary.  Raw
    exception strings are therefore never accepted here, even when a caller
    accidentally passes one as ``detail``.
    """

    code = str(value or "").strip().casefold()
    return _PUBLIC_TERMINAL_DETAIL_BY_CODE.get(code)


def _public_status(value: Any) -> str:
    normalized = str(value or "").casefold()
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized in {"cancelled", "canceled", "stopped", "interrupted"}:
        return "cancelled"
    if normalized == "fallback":
        return "fallback"
    if normalized in {"completed", "success"}:
        return "completed"
    return "running"


def _candidate_count(metadata: dict[str, Any], action_trace: dict[str, Any]) -> Optional[int]:
    preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), dict) else {}
    for value in (
        preview.get("candidateCount"),
        metadata.get("candidateCount"),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    result_summary = str(action_trace.get("resultSummary") or "")
    for token in result_summary.replace(" · ", " ").split():
        if token.startswith("candidateCount="):
            raw = token.split("=", 1)[1]
            if raw.isdigit():
                return int(raw)
    return None


def sanitize_execution_event(event: Any) -> dict[str, Any]:
    """Remove provider-private reasoning before persistence or transport."""

    return AgentExecutionEventSanitizer().sanitize(event)


def sanitize_execution_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Redact a complete event collection with one shared private-value scope."""

    sanitizer = AgentExecutionEventSanitizer()
    materialized = [_event_dict(event) for event in events]
    for event in materialized:
        sanitizer.observe(event)
    return [sanitizer.sanitize(event) for event in materialized]


def _event_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return copy.deepcopy(event)
    if hasattr(event, "model_dump"):
        dumped = event.model_dump(by_alias=True)
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _without_private_reasoning(value: Any, private_values: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_private_reasoning(item, private_values)
            for key, item in value.items()
            if _normalized_key(key) not in _PRIVATE_KEY_TOKENS
        }
    if isinstance(value, list):
        return [_without_private_reasoning(item, private_values) for item in value]
    if isinstance(value, tuple):
        return [_without_private_reasoning(item, private_values) for item in value]
    if isinstance(value, str):
        result = value
        for private_value in private_values:
            if private_value and private_value in result:
                result = result.replace(private_value, "[redacted]")
        return _redact_sensitive_text(result)
    return value


_SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"(?i)\b(authorization|auth|api[ _-]?key|access[ _-]?token|"
        r"refresh[ _-]?token|auth[ _-]?token|token|client[ _-]?secret|password|"
        r"secret|prompt|analysis)\b\s*(?:[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def _redact_sensitive_text(value: str) -> str:
    """Fail closed when a producer embeds credential syntax in a public field."""

    redacted = value
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}: [redacted]"
                if match.lastindex and match.group(1)
                else "[redacted]"
            ),
            redacted,
        )
    return redacted


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _private_strings(value: Any, *, private: bool = False) -> tuple[str, ...]:
    collected: set[str] = set()

    def visit(item: Any, inherited_private: bool = False) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, inherited_private or _normalized_key(key) in _PRIVATE_KEY_TOKENS)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, inherited_private)
        elif inherited_private and isinstance(item, str) and len(item.strip()) >= 4:
            collected.add(item)

    visit(value, private)
    return tuple(sorted(collected, key=len, reverse=True))


def _worker_identity(payload: dict[str, Any]) -> tuple[str, str]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    action_trace = metadata.get("actionTrace") if isinstance(metadata.get("actionTrace"), dict) else {}
    containers = (payload, metadata, action_trace)
    worker = next(
        (
            str(container.get(key) or "").strip()
            for container in containers
            for key in ("workerId", "subagentId", "agentId")
            if str(container.get(key) or "").strip()
        ),
        "",
    )
    parent = next(
        (
            str(container.get("parentExecutionId") or "").strip()
            for container in containers
            if str(container.get("parentExecutionId") or "").strip()
        ),
        "",
    )
    return worker, parent


def _parallel_worker_summary(phase: str, workers: dict[str, str]) -> str:
    count = len(workers)
    failed = sum(status in {"failed", "cancelled"} for status in workers.values())
    completed = sum(status != "running" for status in workers.values())
    label = "工具任务" if phase in {"tool", "places"} else "执行任务"
    if failed:
        return f"{count} 个并行{label}中有 {failed} 个未完成"
    if completed == count:
        return f"已完成 {count} 个并行{label}"
    return f"正在并行处理 {count} 个{label}"


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-") or "session"


def _row_reasoning_statuses(row: Optional[sqlite3.Row]) -> list[dict[str, Any]]:
    if row is None:
        return []
    try:
        payload = json.loads(row["agent_response_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    return [sanitize_execution_event(item) for item in payload.get("reasoningStatuses") or [] if isinstance(item, dict)]

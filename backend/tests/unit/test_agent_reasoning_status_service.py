from __future__ import annotations

import json
import sqlite3

from src.services.agent_reasoning_status_service import (
    AgentReasoningStatusProjector,
    reasoning_status_snapshot,
    reasoning_statuses_from_events,
    sanitize_execution_event,
)


def _event(
    event_type: str,
    *,
    label: str,
    status: str = "completed",
    category: str = "decision",
    metadata: dict | None = None,
    sequence: int | None = None,
) -> dict:
    payload = {
        "type": event_type,
        "label": label,
        "status": status,
        "category": category,
        "userVisible": True,
        "turnId": "turn_reasoning",
        "timestamp": "2026-08-20T00:00:00+00:00",
        "metadata": metadata or {},
    }
    if sequence is not None:
        payload["sequence"] = sequence
    return payload


def test_one_run_upserts_one_item_per_semantic_key_without_state_regression() -> None:
    projector = AgentReasoningStatusProjector(
        session_id="sess_reasoning",
        turn_id="turn_reasoning",
        run_id="run_reasoning",
    )

    active = projector.consume(
        _event(
            "agent_decision",
            label="Controller 决策",
            status="querying",
            sequence=4,
            metadata={"actionTrace": {"decisionSummary": "正在核对两日行程约束"}},
        )
    )
    completed = projector.consume(
        _event(
            "agent_policy_gate",
            label="策略校验",
            status="completed",
            sequence=5,
            metadata={"actionTrace": {"resultSummary": "已确认两日高校与晚间公园要求"}},
        )
    )
    duplicate = projector.consume(
        _event(
            "agent_policy_gate",
            label="策略校验",
            status="completed",
            sequence=5,
            metadata={"actionTrace": {"resultSummary": "已确认两日高校与晚间公园要求"}},
        )
    )
    stale = projector.consume(
        _event(
            "agent_decision",
            label="Controller 决策",
            status="querying",
            sequence=3,
            metadata={"actionTrace": {"decisionSummary": "正在核对两日行程约束"}},
        )
    )

    assert active is not None and completed is not None
    assert active["id"] == completed["id"]
    assert completed["runId"] == "run_reasoning"
    assert completed["semanticKey"] == "constraint_confirmation"
    assert completed["status"] == "completed"
    assert completed["firstSequence"] == 4
    assert completed["latestSequence"] == 5
    assert duplicate is None
    assert stale is None
    assert len(projector.statuses) == 1


def test_request_accepted_fact_is_immediate_and_late_lower_phase_cannot_regress() -> None:
    projector = AgentReasoningStatusProjector(
        session_id="sess_reasoning",
        run_id="run_reasoning",
    )

    first = projector.begin()
    feasibility = projector.consume(
        _event(
            "route_validation",
            label="正在校验真实路线",
            status="querying",
            category="validation",
            sequence=4,
        )
    )
    late_understanding = projector.consume(
        _event(
            "normalize_request",
            label="迟到的需求解析",
            status="completed",
            sequence=5,
        )
    )

    assert first["semanticKey"] == "request_understanding"
    assert first["status"] == "running"
    assert first["summary"] == "正在理解需求"
    assert feasibility is not None and feasibility["phase"] == "feasibility"
    assert late_understanding is None
    assert projector.statuses[-1]["phase"] == "feasibility"


def test_internal_and_heartbeat_events_never_create_disclosure_items() -> None:
    projector = AgentReasoningStatusProjector(
        session_id="sess_reasoning",
        turn_id="turn_reasoning",
        run_id="run_reasoning",
    )

    assert projector.consume(_event("heartbeat", label="still alive", sequence=1)) is None
    assert (
        projector.consume(
            {
                **_event("internal_probe", label="private internal detail", sequence=2),
                "category": "internal",
                "userVisible": False,
            }
        )
        is None
    )
    assert projector.statuses == []


def test_reasoning_statuses_are_safe_high_level_summaries_separate_from_execution_payload() -> None:
    projector = AgentReasoningStatusProjector(session_id="sess_reasoning", turn_id="turn_reasoning")

    status = projector.consume(
        _event(
            "normalize_request",
            label="解析用户需求",
            metadata={
                "reasoning_content": "private hidden chain of thought",
                "chain_of_thought": ["secret step"],
                "actionTrace": {
                    "actionLabel": "理解本轮目标",
                    "resultSummary": "已识别两日高校、午餐和晚间公园约束",
                },
            },
        )
    )

    assert status is not None
    assert status["messageType"] == "reasoning_status"
    assert status["phase"] == "understanding"
    assert status["summary"] == "已识别两日高校、午餐和晚间公园约束"
    assert "private" not in str(status)
    assert "secret" not in str(status)
    assert "reasoning_content" not in str(status)
    assert projector.statuses[0] != projector.raw_events[0]


def test_tool_progress_uses_real_result_counts_and_coalesces_repeated_updates() -> None:
    projector = AgentReasoningStatusProjector(session_id="sess_reasoning", turn_id="turn_reasoning")

    first = projector.consume(
        _event(
            "collect_candidates",
            label="搜索候选",
            status="querying",
            category="tool_action",
            metadata={"actionTrace": {"actionLabel": "搜索并筛选真实地图候选"}},
        )
    )
    second = projector.consume(
        _event(
            "collect_candidates",
            label="搜索候选",
            status="completed",
            category="tool_result",
            metadata={
                "resultPreview": {"candidateCount": 3},
                "actionTrace": {
                    "actionLabel": "搜索并筛选真实地图候选",
                    "resultSummary": "candidateCount=3",
                },
            },
        )
    )

    assert first is not None and first["status"] == "running"
    assert second is not None
    assert second["status"] == "completed"
    assert second["summary"] == "已找到 3 个候选，正在比较"
    assert len(projector.statuses) == 1
    assert first["id"] == second["id"]
    assert projector.statuses[0]["sequence"] == 2


def test_reasoning_terminal_status_closes_cleanly_for_success_failure_and_cancel() -> None:
    completed = AgentReasoningStatusProjector(session_id="sess", turn_id="turn")
    completed.consume(_event("basic_verifier", label="校验结果", category="validation"))
    final = completed.finish("completed")
    assert final["status"] == "completed"
    assert final["phase"] == "result"
    assert final["summary"] == "方案结果已整理"

    failed = AgentReasoningStatusProjector(session_id="sess", turn_id="turn")
    failure = failed.finish("failed", detail="route_provider_unavailable")
    assert failure["status"] == "failed"
    assert failure["summary"] == "方案结果未完成"
    assert failure["detail"] == "路线服务暂时不可用"

    cancelled = AgentReasoningStatusProjector(session_id="sess", turn_id="turn")
    cancelled.consume(_event("collect_candidates", label="搜索候选", status="querying", category="tool_action"))
    cancellation = cancelled.finish("cancelled")
    assert cancellation["status"] == "cancelled"
    assert cancellation["summary"] == "方案整理已停止"
    assert [item for item in cancelled.statuses if item["status"] == "running"] == []
    assert cancelled.finish("failed") == cancellation

    clarification = AgentReasoningStatusProjector(session_id="sess", turn_id="turn")
    clarification.consume(_event("agent_decision", label="等待批量澄清", status="querying"))
    waiting = clarification.finish("needs_confirmation")
    assert waiting["status"] == "completed"
    assert waiting["semanticKey"] == "result_assembly"
    assert waiting["summary"] == "等待你确认关键约束"
    assert [item for item in clarification.statuses if item["status"] == "running"] == []


def test_failure_reasoning_terminal_never_echoes_raw_exception_detail() -> None:
    secret = "PRIVATE_CHAIN_SENTINEL_FAILURE_DETAIL"
    projector = AgentReasoningStatusProjector(session_id="sess_private_terminal")

    terminal = projector.finish("failed", detail=secret)

    assert terminal["detail"] is None
    assert secret not in json.dumps(projector.statuses, ensure_ascii=False)


def test_finished_run_keeps_unique_item_ids_after_visible_history_is_bounded() -> None:
    projector = AgentReasoningStatusProjector(
        session_id="sess_bounded",
        turn_id="turn_bounded",
        run_id="run_bounded",
    )

    for sequence, event_type in enumerate(
        (
            "normalize_request",
            "context",
            "agent_decision",
            "collect_candidates",
            "route_validation",
            "basic_verifier",
        ),
        start=1,
    ):
        projector.consume(
            _event(
                event_type,
                label=event_type,
                status="completed",
                category="validation" if event_type == "basic_verifier" else "decision",
                sequence=sequence,
            )
        )

    projector.finish("completed")

    assert len(projector.statuses) == projector.MAX_VISIBLE_ITEMS
    assert len({item["id"] for item in projector.statuses}) == len(projector.statuses)
    assert [item["semanticKey"] for item in projector.statuses] == [
        "request_understanding",
        "constraint_confirmation",
        "poi_verification",
        "route_feasibility",
        "result_assembly",
    ]


def test_reasoning_sanitizer_removes_private_key_variants_and_echoed_private_text() -> None:
    secret = "private model reasoning must never cross the public stream"

    sanitized = sanitize_execution_event(
        _event(
            "agent_decision",
            label="Controller 决策",
            metadata={
                "Reasoning-Text": secret,
                "hiddenPrompt": {"content": "SYSTEM PRIVATE PROMPT"},
                "internalPrompt": "INTERNAL PRIVATE PROMPT",
                "analysis": "PRIVATE ANALYSIS",
                "thinking": "PRIVATE THINKING",
                "thought": "PRIVATE THOUGHT",
                "credentials": {
                    "authorization": "Bearer PRIVATE AUTH",
                    "api_key": "PRIVATE API KEY",
                    "accessToken": "PRIVATE ACCESS TOKEN",
                    "secret": "PRIVATE SECRET",
                },
                "actionTrace": {
                    "actionLabel": "检查约束",
                    "resultSummary": f"safe prefix {secret} safe suffix",
                },
            },
        )
    )

    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert "private model reasoning" not in serialized
    assert "SYSTEM PRIVATE PROMPT" not in serialized
    assert "Reasoning-Text" not in serialized
    assert "hiddenPrompt" not in serialized
    assert "INTERNAL PRIVATE PROMPT" not in serialized
    assert "PRIVATE ANALYSIS" not in serialized
    assert "PRIVATE THINKING" not in serialized
    assert "PRIVATE THOUGHT" not in serialized
    assert "PRIVATE AUTH" not in serialized
    assert "PRIVATE API KEY" not in serialized
    assert "PRIVATE ACCESS TOKEN" not in serialized
    assert "PRIVATE SECRET" not in serialized
    assert "[redacted]" in serialized


def test_reasoning_projector_redacts_private_text_echoed_by_a_later_event() -> None:
    secret = "PRIVATE_CHAIN_SENTINEL_1234"
    projector = AgentReasoningStatusProjector(session_id="sess_echo", turn_id="turn_echo")

    projector.consume(
        _event(
            "agent_decision",
            label="Controller 决策",
            metadata={"reasoning_content": secret},
        )
    )
    projector.consume(
        _event(
            "context",
            label=secret,
            metadata={"actionTrace": {"resultSummary": f"safe prefix {secret} safe suffix"}},
        )
    )

    serialized = json.dumps(projector.raw_events, ensure_ascii=False)
    assert secret not in serialized
    assert "[redacted]" in serialized


def test_reasoning_sanitizer_redacts_credentials_embedded_in_public_text_fields() -> None:
    sanitized = sanitize_execution_event(
        {
            **_event("context", label="Authorization: Bearer SECRET_TOKEN_1234"),
            "detail": "api_key=SECRET_TOKEN_1234 password=hunter2",
            "metadata": {
                "actionTrace": {
                    "resultSummary": "access_token=ACCESS_TOKEN_5678",
                }
            },
        }
    )

    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert "SECRET_TOKEN_1234" not in serialized
    assert "hunter2" not in serialized
    assert "ACCESS_TOKEN_5678" not in serialized
    assert serialized.count("[redacted]") >= 3


def test_reasoning_sanitizer_redacts_private_values_across_plain_text_syntax() -> None:
    private_values = [
        "auth=AUTHSECRET",
        "token=TOKENSECRET",
        "api key APISECRET",
        "Authorization=AUTHZSECRET",
        "secret SECRETWORD",
        "prompt SECRETPROMPT",
        "analysis SECRETANALYSIS",
    ]

    sanitized = sanitize_execution_event(
        {
            **_event("context", label="公开事实"),
            "detail": " ".join(private_values),
        }
    )

    serialized = json.dumps(sanitized, ensure_ascii=False)
    for value in (
        "AUTHSECRET",
        "TOKENSECRET",
        "APISECRET",
        "AUTHZSECRET",
        "SECRETWORD",
        "SECRETPROMPT",
        "SECRETANALYSIS",
    ):
        assert value not in serialized
    assert serialized.count("[redacted]") >= len(private_values)


def test_parallel_workers_are_aggregated_into_one_user_facing_status() -> None:
    projector = AgentReasoningStatusProjector(session_id="sess_workers", turn_id="turn_workers")

    first = projector.consume(
        _event(
            "resolve_poi",
            label="核验地点",
            status="querying",
            category="tool_action",
            metadata={"workerId": "worker_a", "parentExecutionId": "batch_1"},
        )
    )
    second = projector.consume(
        _event(
            "resolve_poi",
            label="核验地点",
            status="querying",
            category="tool_action",
            metadata={"workerId": "worker_b", "parentExecutionId": "batch_1"},
        )
    )

    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert len(projector.statuses) == 1
    assert second["workerCount"] == 2
    assert second["completedWorkerCount"] == 0
    assert second["summary"] == "正在并行处理 2 个工具任务"


def test_reasoning_snapshot_follows_user_turn_to_final_assistant_and_honors_cursor() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE conversation_turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            parent_turn_id TEXT,
            agent_response_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    projector = AgentReasoningStatusProjector(session_id="sess_snapshot", turn_id="turn_user")
    projector.bind_turn("turn_user", role="user")
    projector.consume(_event("collect_candidates", label="搜索候选", status="querying", category="tool_action"))
    projector.consume(_event("collect_candidates", label="搜索候选", status="completed", category="tool_result"))
    user_payload = json.dumps({"reasoningStatuses": projector.statuses}, ensure_ascii=False)
    db.execute(
        "INSERT INTO conversation_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("turn_user", "sess_snapshot", "user", 1, "active", None, user_payload, "2026-08-20T00:00:00Z"),
    )

    live = reasoning_status_snapshot(
        db,
        session_id="sess_snapshot",
        turn_id="turn_user",
        after_sequence=1,
        active=True,
    )
    assert live["active"] is True
    assert live["sourceUserTurnId"] == "turn_user"
    assert [item["sequence"] for item in live["statuses"]] == [2]

    projector.bind_turn("turn_assistant", role="assistant")
    projector.finish("completed")
    assistant_payload = json.dumps({"reasoningStatuses": projector.statuses}, ensure_ascii=False)
    db.execute(
        "INSERT INTO conversation_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "turn_assistant",
            "sess_snapshot",
            "assistant",
            2,
            "active",
            "turn_user",
            assistant_payload,
            "2026-08-20T00:00:01Z",
        ),
    )

    completed = reasoning_status_snapshot(
        db,
        session_id="sess_snapshot",
        turn_id="turn_user",
        after_sequence=2,
        active=False,
    )
    assert completed["assistantTurnId"] == "turn_assistant"
    assert completed["sourceUserTurnId"] == "turn_user"
    assert completed["terminalStatus"] == "completed"
    assert [item["phase"] for item in completed["statuses"]] == ["result"]
    assert completed["statuses"][0]["rootUserTurnId"] == "turn_user"
    assert completed["statuses"][0]["assistantTurnId"] == "turn_assistant"


def test_active_reasoning_snapshot_never_reuses_a_prior_completed_turn_before_first_status() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE conversation_turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            parent_turn_id TEXT,
            agent_response_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    old_status = reasoning_statuses_from_events(
        [_event("basic_verifier", label="旧轮校验", category="validation")],
        session_id="sess_active",
        turn_id="turn_old_assistant",
    )
    db.execute(
        "INSERT INTO conversation_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "turn_old_assistant",
            "sess_active",
            "assistant",
            2,
            "active",
            "turn_old_user",
            json.dumps({"reasoningStatuses": old_status}, ensure_ascii=False),
            "2026-08-20T00:00:00Z",
        ),
    )
    db.execute(
        "INSERT INTO conversation_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "turn_new_user",
            "sess_active",
            "user",
            3,
            "active",
            None,
            json.dumps({}, ensure_ascii=False),
            "2026-08-20T00:00:01Z",
        ),
    )

    snapshot = reasoning_status_snapshot(
        db,
        session_id="sess_active",
        active=True,
        active_turn_id="turn_new_user",
    )

    assert snapshot["active"] is True
    assert snapshot["sourceUserTurnId"] == "turn_new_user"
    assert snapshot["assistantTurnId"] is None
    assert snapshot["statuses"] == []
    assert snapshot["nextSequence"] == 0
    assert snapshot["terminalStatus"] is None

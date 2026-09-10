import json
import queue
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.schemas.agent import (
    AgentDirectionSaveActiveRequest,
    AgentDirectionSaveActiveResponse,
    AgentMessageEditRequest,
    AgentMessageEditResponse,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentReasoningStatusSnapshotResponse,
    AgentSpatialMapSelectionBindRequest,
    AgentSpatialMapSelectionBindResponse,
    AgentSessionCreateRequest,
    AgentSessionListResponse,
    AgentSessionResponse,
)
from src.core.config import get_settings
from src.core.database import get_db
from src.runtime.agent_runtime import TripAgentRuntime
from src.services.agent_service import AgentService
from src.services.agent_debug_bundle_service import AgentDebugBundleService
from src.services.agent_run_control import (
    acquire_session_run,
    active_session_run_turn,
    is_session_run_active,
    release_session_run,
)
from src.services.agent_reasoning_status_service import (
    AgentExecutionEventSanitizer,
    AgentReasoningStatusProjector,
    clear_reasoning_statuses,
    persist_reasoning_statuses,
    reasoning_status_snapshot,
)
from src.services.planning_trace_export_service import PlanningTraceExportService
from src.services.proposal_visit_facts_service import ProposalVisitFactsService
from src.services.simple_open_direction_service import SimpleOpenDirectionService


router = APIRouter(prefix="/agent", tags=["agent"])

_PUBLIC_AGENT_ERROR_CODES = {
    "guide_source_read_interrupted",
    "guide_source_content_unavailable",
    "guide_source_places_missing",
    "simple_direction_original_schedule_contract_outdated",
    "plan_proposal_original_schedule_mismatch",
    "plan_proposal_source_context_missing",
    "plan_proposal_request_scope_invalid",
    "plan_proposal_route_quality_failed",
    "request_id_payload_conflict",
    "request_outcome_pending",
    "request_superseded",
}


_DIRECTION_SAVE_ERRORS: dict[str, dict[str, str]] = {
    "simple_direction_save_identity_mismatch": {
        "code": "direction_save_identity_mismatch",
        "message": "当前编辑方案与行程对比中的方案身份不一致，请保留当前页面并刷新后重试。",
    },
    "simple_direction_save_base_stale": {
        "code": "direction_save_version_stale",
        "message": "行程版本已变化，当前编辑尚未保存到对比方案；请刷新版本后重试。",
    },
    "simple_direction_save_in_progress": {
        "code": "direction_save_in_progress",
        "message": "当前方案正在确认或保存，请停留在编辑页，稍后再试。",
    },
    "simple_direction_save_route_contract_missing": {
        "code": "direction_save_route_preferences_missing",
        "message": "当前方案缺少已确认的路线偏好，无法安全保存；请先在对话中确认路线偏好。",
    },
    "simple_direction_save_material_missing": {
        "code": "direction_save_material_missing",
        "message": "未找到当前方案或版本材料，尚未切换页面；请刷新后重试。",
    },
    "simple_direction_save_active_snapshot_invalid": {
        "code": "direction_save_snapshot_invalid",
        "message": "当前行程数据不完整，尚未保存；请刷新或恢复版本后重试。",
    },
    "simple_direction_save_activation_failed": {
        "code": "direction_save_activation_failed",
        "message": "当前行程未通过保存前的地点真实性、用途或需求一致性校验，未保存到对比方案；请留在编辑页查看待处理项并修正后重试。",
    },
}


def _direction_save_error(reason: str) -> dict[str, str]:
    reason_text = str(reason or "")
    reason_code, _, detail = reason_text.partition(":")
    if reason_code == "simple_direction_save_activation_failed":
        failures = {item for item in detail.split(",") if item}
        if "simple_direction_materialized_poi_city_mismatch" in failures:
            return {
                "code": "direction_save_activation_failed",
                "message": "当前行程中的地点城市与本次规划城市不一致，未保存到对比方案；请留在编辑页修正后重试。",
            }
        if "simple_direction_materialized_poi_duplicate" in failures:
            return {
                "code": "direction_save_activation_failed",
                "message": "当前行程包含重复地点，未保存到对比方案；请留在编辑页删除或替换重复安排后重试。",
            }
        if failures & {
            "simple_direction_materialized_pending_lineage_conflict",
            "simple_direction_materialized_lineage_invalid",
            "simple_direction_authoritative_lineage_mismatch",
            "simple_direction_pending_slot_lineage_conflict",
            "simple_direction_pending_hard_slot",
        }:
            return {
                "code": "direction_save_activation_failed",
                "message": "当前行程的必选需求与待补槽位不一致，未保存到对比方案；请留在编辑页修正冲突后重试。",
            }
        if "simple_direction_verified_semantic_mismatch" in failures:
            return {
                "code": "direction_save_activation_failed",
                "message": "当前行程中的地点类型与体验需求不一致，未保存到对比方案；请留在编辑页替换相关地点后重试。",
            }
        if failures & {
            "simple_direction_no_verified_amap_anchor",
            "simple_direction_materialized_map_identity_invalid",
        }:
            return {
                "code": "direction_save_activation_failed",
                "message": "当前行程缺少可核验的高德地点身份，未保存到对比方案；请留在编辑页补充或替换相关地点后重试。",
            }
    return _DIRECTION_SAVE_ERRORS.get(
        reason_code,
        {
            "code": "direction_save_failed",
            "message": "当前方案未能保存，仍停留在编辑页；请刷新后重试。",
        },
    )


def _planning_trace_filename(planning_run_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", planning_run_id).strip(".-")[:80]
    return safe_id or "planning-run"


def _reasoning_terminal_from_response(response_payload: dict, terminal_status: str) -> str:
    normalized = str(terminal_status or "").casefold()
    assistant = response_payload.get("assistantTurn") if isinstance(response_payload.get("assistantTurn"), dict) else {}
    if normalized in {"cancelled", "canceled", "stopped", "interrupted"}:
        return "cancelled"
    if normalized in {"failed", "error"} or str(assistant.get("status") or "").casefold() == "failed":
        return "failed"
    if normalized in {"needs_confirmation", "waiting", "awaiting_confirmation"}:
        return "needs_confirmation"
    return "completed"


def _sanitize_agent_response_payload(response_payload: dict) -> dict:
    sanitizer = AgentExecutionEventSanitizer()
    sanitizer.observe(response_payload)
    return sanitizer.sanitize(response_payload)


def _public_route_quality_details(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    issues: list[dict[str, object]] = []
    for raw_issue in value.get("routeQualityIssues") or []:
        if not isinstance(raw_issue, dict):
            continue
        issue: dict[str, object] = {}
        for key in ("fromPoiName", "toPoiName"):
            text = str(raw_issue.get(key) or "").strip()
            if text:
                issue[key] = text[:80]
        for key in ("distanceKm", "durationMinutes"):
            metric = raw_issue.get(key)
            if isinstance(metric, (int, float)) and not isinstance(metric, bool) and metric >= 0:
                issue[key] = metric
        if issue:
            issues.append(issue)
    actions = [
        action
        for raw_action in value.get("recommendedNextActions") or []
        if (action := str(raw_action or "").strip()) and re.fullmatch(r"[a-z0-9_:-]{1,80}", action)
    ]
    details: dict[str, object] = {}
    if issues:
        details["routeQualityIssues"] = issues[:8]
    if actions:
        details["recommendedNextActions"] = actions[:8]
    return details or None


def _public_agent_error_payload(error: Exception) -> dict:
    status_code = int(getattr(error, "status_code", 500) or 500)
    detail = getattr(error, "detail", None)
    detail_dict = detail if isinstance(detail, dict) else {}
    raw_code = str(detail_dict.get("code") or "").strip().casefold()
    code = raw_code if raw_code in _PUBLIC_AGENT_ERROR_CODES else ""

    if status_code >= 500:
        return {
            "message": "行程规划暂时未完成，请稍后重试。",
            "statusCode": status_code,
            "code": "agent_internal_error",
        }
    if status_code == 499:
        return {
            "message": "本轮处理已停止。",
            "statusCode": status_code,
            "code": "agent_run_cancelled",
        }
    request_messages = {
        "guide_source_read_interrupted": "攻略原文读取未完成或证据状态已变化，尚未调用规划器，也未修改方案；请重新获取有效攻略操作。",
        "guide_source_content_unavailable": "已尝试重新打开攻略来源，但未取得可读取的网页正文（可能需要登录、存在访问限制或读取失败）；没有退回摘要规划，也未修改方案。请提供正文或重新搜索可访问的攻略。",
        "guide_source_places_missing": "已读取攻略网页正文，但其中没有可安全提取的具体地点；尚未调用规划器，也未修改方案。请提供更具体的攻略正文。",
        "simple_direction_original_schedule_contract_outdated": "旧方案未完整保留原旅行需求中的到访时段或每日安排，暂不能继续沿用；请按原需求重新生成方案。本轮尚未调用规划器，未改写方案。",
        "plan_proposal_original_schedule_mismatch": "所选旧方案的到访时段或每日安排不符合原旅行需求，不能直接采用；请按原需求重新生成方案。未写入正式行程。",
        "plan_proposal_source_context_missing": "方案所依据的原旅行需求已失效或缺失，无法核对是否满足你的要求；请重新提供原需求并生成方案。",
        "request_id_payload_conflict": "同一请求编号已用于不同内容；没有再次执行，请以新的请求编号发送新内容。",
        "request_outcome_pending": "原请求仍在处理或结果待核对；没有重复调用规划器，也不会因重试而重新执行。",
        "request_superseded": "原请求已被编辑替代；没有重放旧结果，也没有恢复旧操作。",
    }
    if code in request_messages:
        return {"message": request_messages[code], "statusCode": status_code, "code": code}
    if code == "plan_proposal_route_quality_failed":
        payload: dict[str, object] = {
            "message": "所选方案中的餐饮与相邻路线偏绕，未创建正式行程。",
            "statusCode": status_code,
            "code": code,
        }
        safe_details = _public_route_quality_details(detail_dict.get("details"))
        if safe_details:
            payload["details"] = safe_details
        return payload
    if code == "plan_proposal_request_scope_invalid":
        return {
            "message": "方案确认入口已更新，请加载当前会话的最新方案后重新选择。",
            "statusCode": status_code,
            "code": code,
        }

    status_defaults = {
        400: ("当前请求格式不完整，请检查后重试。", "agent_request_invalid"),
        403: ("当前请求不能在此会话中执行。", "agent_request_forbidden"),
        404: ("当前会话所需的信息不存在或已失效。", "agent_resource_not_found"),
        409: ("当前行程状态已变化，请刷新后重试。", "agent_state_conflict"),
        422: ("当前请求未能安全执行，请检查输入后重试。", "agent_request_rejected"),
        429: ("当前服务请求较多，请稍后重试。", "agent_rate_limited"),
    }
    message, fallback_code = status_defaults.get(
        status_code,
        ("当前请求未能安全执行，请稍后重试。", "agent_request_rejected"),
    )
    return {
        "message": message,
        "statusCode": status_code,
        "code": code or fallback_code,
    }


@router.get("/sessions", response_model=AgentSessionListResponse)
def list_agent_sessions(
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSessionListResponse:
    return TripAgentRuntime(db).list_sessions()


@router.post("/sessions", response_model=AgentSessionResponse)
def create_agent_session(
    payload: AgentSessionCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSessionResponse:
    return TripAgentRuntime(db).create_session(payload)


@router.get("/sessions/current", response_model=AgentSessionResponse)
def get_current_agent_session(
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSessionResponse:
    return TripAgentRuntime(db).get_current_session()


@router.get("/sessions/{session_id}", response_model=AgentSessionResponse)
def get_agent_session(
    session_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSessionResponse:
    return TripAgentRuntime(db).get_session(session_id)


@router.get(
    "/sessions/{session_id}/reasoning-statuses",
    response_model=AgentReasoningStatusSnapshotResponse,
)
def get_agent_reasoning_statuses(
    session_id: str,
    turn_id: str = Query(default="", alias="turnId"),
    after_sequence: int = Query(default=0, alias="afterSequence", ge=0),
    db: sqlite3.Connection = Depends(get_db),
) -> AgentReasoningStatusSnapshotResponse:
    session_exists = db.execute(
        "SELECT 1 FROM conversation_sessions WHERE id = ? AND user_id = ?",
        (session_id, get_settings().default_user_id),
    ).fetchone()
    if session_exists is None:
        raise HTTPException(status_code=404, detail="Conversation session not found")
    run_active = is_session_run_active(session_id)
    snapshot = reasoning_status_snapshot(
        db,
        session_id=session_id,
        turn_id=turn_id,
        after_sequence=after_sequence,
        active=run_active,
        active_turn_id=active_session_run_turn(session_id) if run_active else "",
    )
    return AgentReasoningStatusSnapshotResponse.model_validate(snapshot)


@router.delete("/sessions/{session_id}", response_model=AgentSessionListResponse)
def delete_agent_session(
    session_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSessionListResponse:
    return TripAgentRuntime(db).delete_session(session_id)


@router.post("/sessions/{session_id}/pending-poi-candidates/{candidate_id}/reject", response_model=AgentSessionResponse)
def reject_pending_poi_candidate(
    session_id: str,
    candidate_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSessionResponse:
    return TripAgentRuntime(db).reject_pending_poi_candidate(session_id, candidate_id)


@router.post(
    "/sessions/{session_id}/clarification-spatial-map-selections",
    response_model=AgentSpatialMapSelectionBindResponse,
)
def bind_spatial_map_selection(
    session_id: str,
    payload: AgentSpatialMapSelectionBindRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentSpatialMapSelectionBindResponse:
    result = AgentService(db).bind_spatial_map_selection(
        session_id=session_id,
        source_assistant_turn_id=payload.source_assistant_turn_id,
        checkpoint_id=payload.checkpoint_id,
        checkpoint_fingerprint=payload.checkpoint_fingerprint,
        amap_poi_id=payload.amap_poi_id,
        label=payload.label,
        radius_meters=payload.radius_meters,
    )
    return AgentSpatialMapSelectionBindResponse.model_validate(result)


@router.post(
    "/sessions/{session_id}/directions/{proposal_id}/save-active",
    response_model=AgentDirectionSaveActiveResponse,
)
def save_active_agent_direction(
    session_id: str,
    proposal_id: str,
    payload: AgentDirectionSaveActiveRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentDirectionSaveActiveResponse:
    session_exists = db.execute(
        "SELECT 1 FROM conversation_sessions WHERE id = ? AND user_id = ?",
        (session_id, get_settings().default_user_id),
    ).fetchone()
    if session_exists is None:
        raise HTTPException(status_code=404, detail="Conversation session not found")
    if not acquire_session_run(session_id):
        raise HTTPException(
            status_code=409,
            detail=_direction_save_error("simple_direction_save_in_progress"),
        )
    try:
        try:
            result = SimpleOpenDirectionService(db).save_active_direction(
                session_id=session_id,
                proposal_id=proposal_id,
                planning_root_id=payload.planning_selection_root_turn_id,
                portfolio_id=payload.root_portfolio_id,
                base_version_id=payload.base_version_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=_direction_save_error(str(exc)),
            ) from exc
    finally:
        release_session_run(session_id)
    return AgentDirectionSaveActiveResponse.model_validate(result)


@router.get("/sessions/{session_id}/debug-bundle")
def export_agent_debug_bundle(
    session_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    try:
        bundle = AgentDebugBundleService(db).export(session_id=session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        content=bundle,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="trip-agent-debug-{_planning_trace_filename(session_id)}.json"',
        },
    )


@router.get("/sessions/{session_id}/turns/{assistant_turn_id}/planning-runs/{planning_run_id}/trace-export")
def export_planning_trace(
    session_id: str,
    assistant_turn_id: str,
    planning_run_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    trace = PlanningTraceExportService(db).export(
        session_id=session_id,
        assistant_turn_id=assistant_turn_id,
        planning_run_id=planning_run_id,
    )
    return JSONResponse(
        content=trace,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="trip-planning-trace-{_planning_trace_filename(planning_run_id)}.json"'
            ),
        },
    )


@router.post("/sessions/{session_id}/messages", response_model=AgentMessageResponse)
def send_agent_message(
    session_id: str,
    payload: AgentMessageRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentMessageResponse:
    try:
        response = TripAgentRuntime(db).send_agent_message(session_id, payload)
    except HTTPException as error:
        public_error = _public_agent_error_payload(error)
        raise HTTPException(
            status_code=int(public_error["statusCode"]),
            detail={key: value for key, value in public_error.items() if key != "statusCode"},
        ) from error
    return AgentMessageResponse.model_validate(_sanitize_agent_response_payload(response.model_dump(by_alias=True)))


@router.post("/sessions/{session_id}/messages/stream")
def stream_agent_message(
    session_id: str,
    payload: AgentMessageRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    event_queue: queue.Queue = queue.Queue()
    done = object()
    visible_sequence = 0
    reasoning = AgentReasoningStatusProjector(session_id=session_id)
    event_queue.put({"event": "reasoning_status", "data": reasoning.begin()})
    live_user_turn_id = ""

    def push_event(name: str, data: dict) -> None:
        nonlocal visible_sequence, live_user_turn_id
        if name == "execution_event":
            data = reasoning.sanitize_event(data)
        if name == "execution_event" and bool(data.get("userVisible")):
            visible_sequence += 1
            data["sequence"] = visible_sequence
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            action_trace = metadata.get("actionTrace") if isinstance(metadata.get("actionTrace"), dict) else {}
            action_trace["sequence"] = visible_sequence
            metadata["actionTrace"] = action_trace
            data["metadata"] = metadata
        if name == "user_turn":
            live_user_turn_id = str(data.get("id") or "")
            reasoning.bind_turn(live_user_turn_id, role="user")
        event_queue.put({"event": name, "data": data})
        if name == "user_turn" and live_user_turn_id and reasoning.statuses:
            for status in reasoning.statuses:
                event_queue.put({"event": "reasoning_status", "data": status})
            persist_reasoning_statuses(db, live_user_turn_id, reasoning.statuses)
        if name == "execution_event":
            reasoning_status = reasoning.consume(data)
            if reasoning_status is not None:
                event_queue.put({"event": "reasoning_status", "data": reasoning_status})
                if live_user_turn_id:
                    persist_reasoning_statuses(db, live_user_turn_id, reasoning.statuses)

    def run_agent() -> None:
        try:
            response = TripAgentRuntime(db).send_agent_message(
                session_id,
                payload,
                event_sink=lambda event: push_event("execution_event", event.model_dump(by_alias=True)),
                user_turn_sink=lambda turn: push_event("user_turn", turn.model_dump(by_alias=True)),
            )
            response_payload = _sanitize_agent_response_payload(response.model_dump(by_alias=True))
            assistant_turn = (
                response_payload.get("assistantTurn") if isinstance(response_payload.get("assistantTurn"), dict) else {}
            )
            reasoning.bind_turn(str(assistant_turn.get("id") or reasoning.turn_id), role="assistant")
            terminal_status = str(response_payload.get("terminalStatus") or "completed")
            terminal = _reasoning_terminal_from_response(response_payload, terminal_status)
            final_status = reasoning.finish(terminal)
            event_queue.put({"event": "reasoning_status", "data": final_status})
            response_payload["reasoningStatuses"] = reasoning.statuses
            assistant_turn["reasoningStatuses"] = reasoning.statuses
            response_payload["assistantTurn"] = assistant_turn
            assistant_turn_id = str(assistant_turn.get("id") or "")
            if assistant_turn_id:
                persist_reasoning_statuses(db, assistant_turn_id, reasoning.statuses)
            if live_user_turn_id:
                clear_reasoning_statuses(db, live_user_turn_id)
            push_event("message_response", response_payload)
        except Exception as error:
            error_payload = _public_agent_error_payload(error)
            status_code = int(error_payload["statusCode"])
            reasoning_terminal = "cancelled" if status_code == 499 else "failed"
            failure_status = reasoning.finish(
                reasoning_terminal,
                detail=str(error_payload.get("code") or ""),
            )
            event_queue.put({"event": "reasoning_status", "data": failure_status})
            if live_user_turn_id:
                persist_reasoning_statuses(db, live_user_turn_id, reasoning.statuses)
            push_event("error", error_payload)
        finally:
            event_queue.put(done)

    def body():
        worker = threading.Thread(target=run_agent, daemon=True)
        worker.start()
        while True:
            try:
                item = event_queue.get(timeout=2)
            except queue.Empty:
                yield (
                    json.dumps(
                        {
                            "event": "execution_event",
                            "data": {
                                "type": "heartbeat",
                                "label": "Agent 执行中",
                                "userVisible": False,
                                "status": "querying",
                                "detail": "后端仍在等待当前安全阶段完成。",
                                "sessionId": session_id,
                                "turnId": None,
                                "providerName": "agent-stream",
                                "fallbackUsed": False,
                                "failureReason": None,
                                "durationMs": 0,
                                "metadata": {"phase": "stream_wait"},
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                continue
            if item is done:
                break
            yield json.dumps(item, ensure_ascii=False, default=str) + "\n"
        worker.join(timeout=1)

    return StreamingResponse(
        body(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/runs/cancel")
def cancel_agent_run(session_id: str) -> dict:
    return {"cancelRequested": AgentService.cancel_session_run(session_id)}


@router.post("/sessions/{session_id}/plan-proposals/{proposal_id}/visit-facts/refresh")
def refresh_plan_proposal_visit_facts(
    session_id: str,
    proposal_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Refresh online facts without mutating proposal or itinerary snapshots."""

    return ProposalVisitFactsService(db).refresh(
        session_id=session_id,
        proposal_id=proposal_id,
    )


@router.patch("/sessions/{session_id}/messages/{turn_id}", response_model=AgentMessageEditResponse)
def edit_agent_message(
    session_id: str,
    turn_id: str,
    payload: AgentMessageEditRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentMessageEditResponse:
    return TripAgentRuntime(db).edit_user_message(session_id, turn_id, payload)


@router.post("/sessions/{session_id}/turns/{turn_id}/resume", response_model=AgentMessageResponse)
def resume_failed_agent_turn(
    session_id: str,
    turn_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> AgentMessageResponse:
    return TripAgentRuntime(db).resume_failed_turn(session_id, turn_id)

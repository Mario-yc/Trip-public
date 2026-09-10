from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from src.services.retry_recovery_service import RetrySemanticClassifier
from src.services.planning_resume_contract import resumable_planning_checkpoint

SHORT_RETRY_INTENT_RE = re.compile(
    r"^\s*(重新构建|重新生成|重新规划|重新来|重做|换一版|不满意|重试模型规划|重试本次修改|重试一次|重试一遍|重试|继续|再试一次|再试一遍|再来一遍|重新试试|继续生成|继续规划|继续创建|创建时间轴|生成时间轴|地图恢复后重试|刷新候选)\s*[。.!！?？]*\s*$"
)
CONTEXTUAL_RETRY_CHANGE_RE = re.compile(
    r"(改成|改为|改到|换成|换掉|替换|不要|别要|取消|删除|删掉|增加|减少|预算\s*(改|调|降|提)|改低|改高|少安排|多安排|\d+\s*天|[一二两三四五六七八九十]+天)"
)
CONTEXTUAL_RETRY_CONTEXT_RE = re.compile(r"(上面|前面|之前|刚才|刚刚|上一轮|原来|原先|刚才那个|前面的|上一次)")
CONTEXTUAL_RETRY_ACTION_RE = re.compile(r"(重新|重做|重来|再试|再来|再来一遍|来一遍|生成一遍|规划一遍|重新生成|重新规划|换一版|继续|重试)")


class PlanningAttemptResumeService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @staticmethod
    def is_short_retry_intent(message: object) -> bool:
        return bool(SHORT_RETRY_INTENT_RE.search(str(message or "")))

    @staticmethod
    def is_contextual_retry_intent(message: object) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        if PlanningAttemptResumeService.is_short_retry_intent(text):
            return True
        if CONTEXTUAL_RETRY_CHANGE_RE.search(text):
            return False
        return bool(CONTEXTUAL_RETRY_CONTEXT_RE.search(text) and CONTEXTUAL_RETRY_ACTION_RE.search(text))

    @staticmethod
    def is_retry_intent(message: object) -> bool:
        typed = RetrySemanticClassifier().classify(message)
        if typed.intent_class in {
            "retry_contextual",
            "retry_failed_component",
            "continue_pending_choice",
            "regenerate_from_scratch",
        }:
            return True
        return PlanningAttemptResumeService.is_short_retry_intent(
            message
        ) or PlanningAttemptResumeService.is_contextual_retry_intent(message)

    def latest_resumable_attempt(self, session_id: str) -> Optional[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT *
            FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status IN ('active', 'failed')
              AND agent_response_json IS NOT NULL
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 16
            """,
            (session_id,),
        ).fetchall()
        for row in rows:
            payload = self._json_dict(row["agent_response_json"])
            if not self._is_resumable_payload(row, payload):
                continue
            source_user = self._previous_user_turn(session_id, int(row["turn_index"]))
            original_message = self._original_user_message(payload, row, source_user)
            if not original_message:
                continue
            return {
                "sourceAssistantTurnId": row["id"],
                "sourceUserTurnId": source_user["id"] if source_user is not None else None,
                "sourceAssistantTurnIndex": row["turn_index"],
                "sourceUserTurnIndex": source_user["turn_index"] if source_user is not None else None,
                "resultState": self._result_state(payload),
                "resultReason": self._result_reason(payload),
                "originalUserMessage": original_message,
                "initialPlan": self._initial_plan(payload),
                "pipelineContext": self._pipeline_context(payload),
                "constraintLedger": self._checkpoint_dict(payload, "constraintLedger"),
                "creativePortfolio": self._checkpoint_dict(payload, "creativePortfolio"),
                "groundingCheckpoint": self._checkpoint_dict(payload, "groundingCheckpoint"),
                "planningPreview": payload.get("planningPreview") or (payload.get("grounding") or {}).get("planningPreview"),
                "unresolvedSlots": payload.get("unresolvedSlots") or (payload.get("grounding") or {}).get("unresolvedSlots") or [],
                "nextActions": payload.get("nextActions") or [],
            }
        return None

    def resume_context_for_retry(
        self,
        session_id: str,
        latest_message: str,
        *,
        is_retry_intent: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        retry_requested = (
            self.is_retry_intent(latest_message)
            if is_retry_intent is None
            else is_retry_intent
        )
        if not retry_requested:
            return None
        if self._session_has_active_version(session_id):
            return None
        attempt = self.latest_resumable_attempt(session_id)
        if attempt is None:
            return None
        return {
            "enabled": True,
            "sourceAssistantTurnId": attempt["sourceAssistantTurnId"],
            "sourceUserTurnId": attempt["sourceUserTurnId"],
            "sourceAssistantTurnIndex": attempt["sourceAssistantTurnIndex"],
            "sourceUserTurnIndex": attempt["sourceUserTurnIndex"],
            "reason": "user_retry_after_provider_rate_limited"
            if attempt.get("resultState") == "provider_rate_limited"
            else "user_retry_after_resumable_planning_attempt",
            "originalUserMessage": attempt["originalUserMessage"],
            "latestRetryMessage": latest_message,
            "resumeFromStage": "collect_candidates",
            "reuseInitialPlan": bool(attempt.get("initialPlan")),
            "retryOnlyUnresolvedSlots": True,
            "resultState": attempt.get("resultState"),
            "resultReason": attempt.get("resultReason"),
            "initialPlan": attempt.get("initialPlan"),
            "pipelineContext": attempt.get("pipelineContext") or {},
            "constraintLedger": attempt.get("constraintLedger"),
            "creativePortfolio": attempt.get("creativePortfolio"),
            "groundingCheckpoint": attempt.get("groundingCheckpoint"),
            "planningPreview": attempt.get("planningPreview"),
            "unresolvedSlots": attempt.get("unresolvedSlots") or [],
            "nextActions": attempt.get("nextActions") or [],
        }

    def clarification_context_for_answer(
        self,
        session_id: str,
        latest_message: str,
        *,
        is_retry_intent: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        """Join a short free-form clarification answer to its original request.

        The user turn has already been persisted when context is built.  Only
        the immediately preceding assistant clarification may supply context;
        a new complete trip request remains authoritative on its own.
        """
        answer = str(latest_message or "").strip()
        if (
            not answer
            or self._session_has_active_version(session_id)
            or (
                self.is_retry_intent(answer)
                if is_retry_intent is None
                else is_retry_intent
            )
            or self._looks_like_complete_trip_request(answer)
        ):
            return None
        rows = self.db.execute(
            """
            SELECT *
            FROM conversation_turns
            WHERE session_id = ? AND status = 'active'
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 2
            """,
            (session_id,),
        ).fetchall()
        if len(rows) != 2 or rows[0]["role"] != "user" or rows[1]["role"] != "assistant":
            return None
        if str(rows[0]["content"] or "").strip() != answer:
            return None
        response = self._json_dict(rows[1]["agent_response_json"])
        if str(response.get("mode") or "") != "clarification":
            return None
        if str(response.get("terminalStatus") or "needs_confirmation") != "needs_confirmation":
            return None
        request = self._json_dict(rows[1]["agent_request_json"])
        original = str(request.get("effectiveUserMessage") or request.get("latestUserMessage") or "").strip()
        if not original or original == answer:
            return None
        return {
            "enabled": True,
            "sourceAssistantTurnId": rows[1]["id"],
            "sourceAssistantTurnIndex": rows[1]["turn_index"],
            "sourceUserTurnId": rows[0]["id"],
            "sourceUserTurnIndex": rows[0]["turn_index"],
            "originalUserMessage": original,
            "clarificationAnswer": answer,
            "effectiveUserMessage": f"{original}\n补充信息：{answer}",
        }

    def regenerate_context_for_active_version(
        self,
        session_id: str,
        latest_message: str,
        *,
        is_retry_intent: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        retry_requested = (
            self.is_retry_intent(latest_message)
            if is_retry_intent is None
            else is_retry_intent
        )
        if not retry_requested:
            return None
        active_version_id = self._active_version_id(session_id)
        if not active_version_id:
            return None
        source_user = (
            self._latest_complete_user_request_for_version(session_id, active_version_id)
            or self._latest_complete_versioned_user_request(session_id)
        )
        if source_user is None:
            return None
        return {
            "enabled": True,
            "reason": "user_regenerate_from_last_complete_trip_request",
            "sourceUserTurnId": source_user["id"],
            "sourceUserTurnIndex": source_user["turn_index"],
            "sourceActiveVersionId": active_version_id,
            "originalUserMessage": str(source_user["content"] or "").strip(),
            "latestRetryMessage": latest_message,
        }

    def regenerate_context_from_request_history(
        self,
        session_id: str,
        latest_message: str,
        *,
        is_retry_intent: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        """Reuse request text for a full rerun without claiming a checkpoint."""

        retry_requested = (
            self.is_retry_intent(latest_message)
            if is_retry_intent is None
            else is_retry_intent
        )
        if not retry_requested:
            return None
        source = self.latest_complete_user_request(session_id)
        if source is None:
            return None
        return {
            "enabled": True,
            "reason": "user_regenerate_from_latest_complete_request_history",
            "sourceUserTurnId": source["sourceUserTurnId"],
            "sourceUserTurnIndex": source["sourceUserTurnIndex"],
            "originalUserMessage": source["originalUserMessage"],
            "latestRetryMessage": latest_message,
        }

    def latest_complete_user_request(self, session_id: str) -> Optional[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT *
            FROM conversation_turns
            WHERE session_id = ? AND role = 'user' AND status = 'active'
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 32
            """,
            (session_id,),
        ).fetchall()
        for row in rows:
            content = str(row["content"] or "").strip()
            if self.is_retry_intent(content):
                continue
            if self._looks_like_complete_trip_request(content):
                return {
                    "sourceUserTurnId": row["id"],
                    "sourceUserTurnIndex": row["turn_index"],
                    "originalUserMessage": content,
                }
        return None

    def _is_resumable_payload(self, row: sqlite3.Row, payload: dict[str, Any]) -> bool:
        return resumable_planning_checkpoint(
            payload,
            itinerary_version_id=row["itinerary_version_id"],
        )

    def _previous_user_turn(self, session_id: str, before_turn_index: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT *
            FROM conversation_turns
            WHERE session_id = ? AND role = 'user' AND turn_index < ?
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 1
            """,
            (session_id, before_turn_index),
        ).fetchone()

    def _session_has_active_version(self, session_id: str) -> bool:
        return bool(self._active_version_id(session_id))

    def _active_version_id(self, session_id: str) -> str:
        row = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return str(row["active_version_id"] or "") if row and row["active_version_id"] else ""

    def _latest_complete_user_request_for_version(self, session_id: str, version_id: str) -> Optional[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT *
            FROM conversation_turns
            WHERE session_id = ? AND role = 'user' AND status = 'active'
              AND itinerary_version_id = ?
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 24
            """,
            (session_id, version_id),
        ).fetchall()
        for row in rows:
            content = str(row["content"] or "").strip()
            if self.is_retry_intent(content):
                continue
            if self._looks_like_complete_trip_request(content):
                return row
        return None

    def _latest_complete_versioned_user_request(self, session_id: str) -> Optional[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT *
            FROM conversation_turns
            WHERE session_id = ? AND role = 'user' AND status = 'active'
              AND itinerary_version_id IS NOT NULL
            ORDER BY turn_index DESC, created_at DESC
            LIMIT 32
            """,
            (session_id,),
        ).fetchall()
        for row in rows:
            content = str(row["content"] or "").strip()
            if self.is_retry_intent(content):
                continue
            if self._looks_like_complete_trip_request(content):
                return row
        return None

    def _looks_like_complete_trip_request(self, content: str) -> bool:
        if len(content.strip()) < 12:
            return False
        has_trip_topic = bool(re.search(r"(行程|旅行|旅游|日游|参观|游览|高校|夜景|美食|亲子|地标|博物馆|公园)", content))
        has_scope = bool(re.search(r"(\d+\s*天|[一二两三四五六七八九十]+日|[一二两三四五六七八九十]+天|\d+\s*月|国庆|春节|五一|清明|端午|中秋)", content))
        return has_trip_topic and has_scope

    def _original_user_message(
        self,
        payload: dict[str, Any],
        row: sqlite3.Row,
        source_user: Optional[sqlite3.Row],
    ) -> str:
        pipeline_context = self._pipeline_context(payload)
        request_context = self._json_dict(row["agent_request_json"])
        for value in (
            pipeline_context.get("effectiveUserMessage"),
            pipeline_context.get("latestUserMessage"),
            request_context.get("effectiveUserMessage"),
            request_context.get("latestUserMessage"),
            source_user["content"] if source_user is not None else "",
        ):
            text = str(value or "").strip()
            if text and not self.is_retry_intent(text):
                return text
        return ""

    def _result_state(self, payload: dict[str, Any]) -> str:
        grounding = payload.get("grounding") if isinstance(payload.get("grounding"), dict) else {}
        return str(
            payload.get("resultState")
            or payload.get("planningStatus")
            or grounding.get("resultState")
            or ""
        )

    def _result_reason(self, payload: dict[str, Any]) -> str:
        grounding = payload.get("grounding") if isinstance(payload.get("grounding"), dict) else {}
        finalization = grounding.get("finalization") if isinstance(grounding.get("finalization"), dict) else {}
        direct = str(
            payload.get("noVersionReason")
            or payload.get("reason")
            or grounding.get("noVersionReason")
            or grounding.get("reason")
            or finalization.get("unresolvedPolicy")
            or ""
        )
        if direct == "complete_itinerary_quality_contract_blocked":
            return "quality_contract_failed"
        return direct

    def _initial_plan(self, payload: dict[str, Any]) -> Any:
        grounding = payload.get("grounding") if isinstance(payload.get("grounding"), dict) else {}
        return payload.get("initialPlan") or grounding.get("initialPlan")

    def _pipeline_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        grounding = payload.get("grounding") if isinstance(payload.get("grounding"), dict) else {}
        context = payload.get("pipelineContext") or grounding.get("pipelineContext") or {}
        return context if isinstance(context, dict) else {}

    def _checkpoint_dict(self, payload: dict[str, Any], key: str) -> Optional[dict[str, Any]]:
        grounding = payload.get("grounding") if isinstance(payload.get("grounding"), dict) else {}
        value = payload.get(key) or grounding.get(key)
        return value if isinstance(value, dict) else None

    def _json_dict(self, value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            payload = json.loads(str(value))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

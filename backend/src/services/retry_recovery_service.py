"""Typed retry semantics and persisted-state recovery arbitration.

The classifier only describes the user's semantic intent.  It never selects an
executor or authorizes a write.  The resolver is the server-authoritative state
machine which maps that intent plus persisted state to one bounded execution
plan.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.services.planning_resume_contract import resumable_planning_checkpoint


RetryIntentClass = Literal[
    "retry_contextual",
    "retry_failed_component",
    "regenerate_from_scratch",
    "modify_request",
    "continue_pending_choice",
    "continue_plan_expansion",
    "not_retry",
]

RetryExecutionKind = Literal[
    "repair_last_failure",
    "resume_portfolio",
    "continue_partial_slot",
    "resume_incomplete_stage",
    "regenerate_full",
    "restart_last_incomplete_stage",
    "start_modified_request",
    "ask_retry_scope",
    "none",
]


class RetryIntentClassification(BaseModel):
    schema_version: Literal["retry-intent-v1"] = Field(
        default="retry-intent-v1", alias="schemaVersion"
    )
    intent_class: RetryIntentClass = Field(alias="intentClass")
    source: Literal["lexical_hint", "explicit_scope", "structured_choice", "none"]
    literal_message: str = Field(alias="literalMessage")
    outcome_scope: Literal[
        "full_task",
        "exact_component",
        "pending_choice",
        "portfolio_expansion",
        "none",
    ] = Field(
        alias="outcomeScope"
    )
    material_change_fields: list[str] = Field(
        default_factory=list, alias="materialChangeFields"
    )
    confidence: float = Field(ge=0, le=1)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @property
    def handoff_class(self) -> str:
        return {
            "retry_contextual": "retry_last_task",
            "modify_request": "new_or_modified_request",
            "continue_pending_choice": "retry_failed_component",
            "continue_plan_expansion": "continue_plan_expansion",
            "not_retry": "none",
        }.get(self.intent_class, self.intent_class)


class RetryExecutionPlan(BaseModel):
    schema_version: Literal["retry-execution-plan-v1"] = Field(
        default="retry-execution-plan-v1", alias="schemaVersion"
    )
    kind: RetryExecutionKind
    reason_code: str = Field(alias="reasonCode")
    intent_class: RetryIntentClass = Field(alias="intentClass")
    portfolio_id: Optional[str] = Field(default=None, alias="portfolioId")
    proposal_ids: list[str] = Field(default_factory=list, alias="proposalIds")
    source_user_turn_id: Optional[str] = Field(default=None, alias="sourceUserTurnId")
    source_assistant_turn_id: Optional[str] = Field(
        default=None, alias="sourceAssistantTurnId"
    )
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    request_contract_fingerprint: Optional[str] = Field(
        default=None, alias="requestContractFingerprint"
    )
    choice_options: list[dict[str, Any]] = Field(
        default_factory=list, alias="choiceOptions"
    )
    allowed_actions: list[str] = Field(default_factory=list, alias="allowedActions")
    allowed_tools: list[str] = Field(default_factory=list, alias="allowedTools")
    write_budget: int = Field(default=0, alias="writeBudget", ge=0, le=1)
    controller_allowed: bool = Field(default=False, alias="controllerAllowed")
    compatibility_failures: list[str] = Field(
        default_factory=list, alias="compatibilityFailures"
    )
    preconditions: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_recovery_scope(self) -> "RetryExecutionPlan":
        if self.kind == "resume_portfolio":
            if not all(
                (
                    self.portfolio_id,
                    self.source_user_turn_id,
                    self.source_assistant_turn_id,
                    self.request_contract_fingerprint,
                    self.proposal_ids,
                    self.choice_options,
                )
            ):
                raise ValueError("resume_portfolio_identity_incomplete")
        if self.kind == "repair_last_failure":
            required = {
                "failureCode",
                "proposalId",
                "briefId",
                "poolId",
                "planningSlotId",
                "dayNumber",
                "segmentId",
                "amapId",
                "requestContractFingerprint",
            }
            if any(not self.preconditions.get(key) for key in required):
                raise ValueError("repair_failure_identity_incomplete")
        if self.kind == "continue_partial_slot" and not self.choice_options:
            raise ValueError("partial_slot_choice_identity_missing")
        if self.kind in {
            "resume_portfolio",
            "repair_last_failure",
            "continue_partial_slot",
            "ask_retry_scope",
        } and (self.controller_allowed or self.write_budget != 0):
            raise ValueError("recovery_plan_write_or_controller_escalation")
        return self


class RetrySemanticClassifier:
    """Return a strict semantic class; downstream state decides execution."""

    _FULL_REGENERATE = (
        "全部从头",
        "整套从头",
        "从头重新",
        "全部重做",
        "整套重做",
        "不要上一版",
        "不要沿用",
        "重新生成一套",
        "换一套",
    )
    _COMPONENTS = (
        "午餐",
        "晚餐",
        "餐厅",
        "餐饮",
        "夜景",
        "高校",
        "大学",
        "博物馆",
        "街区",
        "路线",
        "day 1",
        "day 2",
        "第一天",
        "第二天",
    )
    _PENDING = ("待选", "待补", "候选", "缺失槽位", "继续补齐", "继续选择")
    _PLAN_EXPANSION = (
        "继续生成其他方案",
        "继续生成一个方案",
        "继续新增一个方案",
        "继续增加一个方案",
        "继续新增方案",
        "继续增加方案",
        "再生成一个方案",
        "再新增一个方案",
        "再增加一个方案",
        "生成另一个方案",
        "生成其他方案",
        "新增一个方案",
        "增加一个方案",
        "追加一个方案",
        "再来一个方案",
    )
    _PLAN_EXPANSION_CUES = (
        "继续",
        "再",
        "还想",
        "多",
        "其他",
        "更多",
        "别的",
        "另",
        "新增",
        "增加",
        "追加",
    )
    _PLAN_EXPANSION_ACTIONS = (
        "生成",
        "新增",
        "增加",
        "追加",
        "出",
        "做",
        "给",
        "来",
        "看",
    )
    _PLAN_EXPANSION_OBJECTS = ("方案", "行程方案", "一套", "几套")
    _PLAN_EXPANSION_META_PREFIXES = ("为什么", "为何", "怎么", "不能", "无法")
    _PLAN_EXPANSION_META_MARKERS = (
        "为什么",
        "为何",
        "怎么",
        "想知道",
        "是什么意思",
        "按钮写着",
        "这个按钮",
        "讨论",
        "故障",
        "失败原因",
        "报错为",
        "不要执行",
        "不执行",
    )
    _PLAN_EXPANSION_FAILURE_SUFFIXES = (
        "失败",
        "失败了",
        "超时",
        "超时了",
        "报错",
        "报错了",
        "出错",
        "出错了",
    )
    _PLAN_EXPANSION_NEGATORS = (
        "不要",
        "不需要",
        "无需",
        "不想",
        "不打算",
        "别",
        "停止",
        "取消",
        "不再",
        "不继续",
    )
    _RETRY = (
        "重试",
        "再试",
        "再来",
        "重新试",
        "再规划",
        "继续规划",
        "继续生成",
        "继续创建",
        "重新构建",
        "重新生成",
        "重新规划",
        "重新来",
        "重做",
        "创建时间轴",
        "生成时间轴",
        "地图恢复后重试",
        "刷新候选",
    )
    _ITINERARY_CHANGES = (
        "修改",
        "改为",
        "替换",
        "换成",
        "新增",
        "增加",
        "删除",
        "移到",
        "调整到",
        "补充",
    )
    _MATERIAL_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("day_count", ("改成三天", "改成3天", "三日游", "3天", "增加一天", "减少一天")),
        ("date", ("改日期", "换日期", "延期", "提前到")),
        ("city", ("改去", "换城市", "上海", "广州", "深圳", "杭州")),
        ("budget", ("预算改", "降低预算", "提高预算", "更便宜", "预算增加")),
        ("party", ("增加一人", "改成2人", "改成两人", "带孩子", "父母加入")),
        ("transport", ("改自驾", "改打车", "不要地铁", "改交通")),
        ("hard_preference", ("不要高校", "取消夜景", "必须增加", "删掉")),
    )

    def classify(self, message: object) -> RetryIntentClassification:
        literal = str(message or "").strip()
        text = " ".join(literal.lower().split())
        if not text:
            return self._result("not_retry", "none", literal, "none", [], 1.0)
        if any(marker in text for marker in self._FULL_REGENERATE):
            return self._result(
                "regenerate_from_scratch",
                "explicit_scope",
                literal,
                "full_task",
                [],
                1.0,
            )
        if self.is_rejected_plan_expansion(text):
            return self._result("not_retry", "explicit_scope", literal, "none", [], 1.0)
        if self._is_plan_expansion(text):
            return self._result(
                "continue_plan_expansion",
                "explicit_scope",
                literal,
                "portfolio_expansion",
                [],
                1.0,
            )
        material = [
            field
            for field, markers in self._MATERIAL_FIELDS
            if any(marker in text for marker in markers)
        ]
        retry_hint = any(marker in text for marker in self._RETRY)
        if retry_hint and any(marker in text for marker in self._ITINERARY_CHANGES):
            material.append("itinerary_change")
        if material:
            return self._result(
                "modify_request",
                "explicit_scope",
                literal,
                "full_task",
                material,
                0.99,
            )
        component_hint = any(marker in text for marker in self._COMPONENTS)
        if any(marker in text for marker in self._PENDING) and (
            retry_hint or text.startswith("继续")
        ):
            return self._result(
                "continue_pending_choice",
                "explicit_scope",
                literal,
                "pending_choice",
                [],
                0.98,
            )
        if component_hint and (
            retry_hint
            or text.startswith("只修复")
            or text.startswith("只换")
            or text.startswith("修复")
        ):
            return self._result(
                "retry_failed_component",
                "explicit_scope",
                literal,
                "exact_component",
                [],
                0.98,
            )
        if retry_hint:
            return self._result(
                "retry_contextual",
                "lexical_hint",
                literal,
                "full_task",
                [],
                0.96,
            )
        return self._result("not_retry", "none", literal, "none", [], 1.0)

    @classmethod
    def _is_plan_expansion(cls, text: str) -> bool:
        compact = cls._compact_plan_expansion_text(text)
        if cls.is_rejected_plan_expansion(text):
            return False
        if any(marker in compact for marker in cls._PLAN_EXPANSION):
            return True
        return (
            any(marker in compact for marker in cls._PLAN_EXPANSION_CUES)
            and any(marker in compact for marker in cls._PLAN_EXPANSION_ACTIONS)
            and any(marker in compact for marker in cls._PLAN_EXPANSION_OBJECTS)
        )

    @classmethod
    def is_rejected_plan_expansion(cls, text: str) -> bool:
        stripped = text.strip()
        compact = cls._compact_plan_expansion_text(text)
        if not (
            any(marker in compact for marker in cls._PLAN_EXPANSION_ACTIONS)
            and any(marker in compact for marker in cls._PLAN_EXPANSION_OBJECTS)
        ):
            return False
        quoted_only = any(
            stripped.startswith(opening) and stripped.endswith(closing)
            for opening, closing in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))
        )
        return quoted_only or compact.startswith(cls._PLAN_EXPANSION_META_PREFIXES) or any(
            marker in compact for marker in cls._PLAN_EXPANSION_META_MARKERS
        ) or compact.endswith(cls._PLAN_EXPANSION_FAILURE_SUFFIXES) or any(
            f"{negator}{connector}{action}" in compact
            for negator in cls._PLAN_EXPANSION_NEGATORS
            for connector in ("", "继续", "再", "多", "再多")
            for action in cls._PLAN_EXPANSION_ACTIONS
        ) or any(
            marker in compact for marker in ("只想查看", "只是查看")
        )

    @staticmethod
    def _compact_plan_expansion_text(text: str) -> str:
        return text.translate(
            str.maketrans("", "", " \t\r\n，,。！？!?；;：:“”\"'‘’（）()")
        )

    @staticmethod
    def _result(
        intent_class: RetryIntentClass,
        source: Literal["lexical_hint", "explicit_scope", "structured_choice", "none"],
        literal: str,
        outcome_scope: Literal[
            "full_task",
            "exact_component",
            "pending_choice",
            "portfolio_expansion",
            "none",
        ],
        material_fields: list[str],
        confidence: float,
    ) -> RetryIntentClassification:
        return RetryIntentClassification(
            intentClass=intent_class,
            source=source,
            literalMessage=literal,
            outcomeScope=outcome_scope,
            materialChangeFields=material_fields,
            confidence=confidence,
        )


class RetryExecutionPlanService:
    """Resolve one retry intent against server-persisted recovery state."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def resolve(
        self,
        *,
        session_id: str,
        intent: RetryIntentClassification,
        latest_user_turn_id: str | None = None,
        request_contract_fingerprint: str | None = None,
    ) -> RetryExecutionPlan:
        active_version_id = self._active_version_id(session_id)
        if intent.intent_class == "not_retry":
            return self._plan("none", "not_retry", intent, active_version_id)
        if intent.intent_class == "modify_request":
            return self._plan(
                "start_modified_request",
                "material_request_change",
                intent,
                active_version_id,
                controller_allowed=True,
                write_budget=1,
                allowed_actions=["draft_itinerary", "patch_itinerary"],
            )
        if intent.intent_class == "regenerate_from_scratch":
            return self._plan(
                "regenerate_full",
                "explicit_full_regeneration",
                intent,
                active_version_id,
                controller_allowed=True,
                write_budget=1,
                allowed_actions=["draft_itinerary"],
            )
        if intent.intent_class == "continue_plan_expansion":
            return self._plan(
                "ask_retry_scope",
                "persisted_portfolio_expansion_capability_required",
                intent,
                active_version_id,
                allowed_actions=["ask_user"],
                preconditions={
                    "persistedOpaqueChoiceRequired": True,
                    "externalCallsAllowed": False,
                },
            )
        if active_version_id:
            partial = self._active_partial_state(session_id, active_version_id)
            partial_choices = (
                self._partial_slot_choices(session_id, partial) if partial else []
            )
            if (
                intent.intent_class
                in {"continue_pending_choice", "retry_contextual"}
                and partial
                and partial_choices
            ):
                return self._plan(
                    "continue_partial_slot",
                    "active_partial_reoffers_exact_persisted_slot_choices",
                    intent,
                    active_version_id,
                    choice_options=partial_choices,
                    allowed_actions=["ask_user"],
                    preconditions={
                        "exactSlotIdentityRequired": True,
                        "pendingSlotCount": len(partial.get("portfolioPendingSlots") or []),
                        "checkpointReused": True,
                        "stagedPipelineAllowed": False,
                        "externalCallsAllowed": False,
                    },
                )
            if partial:
                return self._plan(
                    "ask_retry_scope",
                    "active_partial_persisted_choices_missing",
                    intent,
                    active_version_id,
                    allowed_actions=["ask_user"],
                )
            return self._plan(
                "restart_last_incomplete_stage",
                "active_complete_timeline_retry_delegated_to_controller",
                intent,
                active_version_id,
                controller_allowed=True,
                write_budget=1,
                allowed_actions=[
                    "read_itinerary",
                    "resolve_poi",
                    "patch_itinerary",
                    "finish",
                ],
            )
        portfolio, failures = self._compatible_portfolio(
            session_id=session_id,
            latest_user_turn_id=latest_user_turn_id,
            request_contract_fingerprint=request_contract_fingerprint,
        )
        if intent.intent_class == "retry_contextual" and portfolio is not None:
            return self._portfolio_plan(intent, portfolio)
        if intent.intent_class == "retry_failed_component":
            failure = self._latest_exact_failure(session_id)
            if failure:
                return self._plan(
                    "repair_last_failure",
                    "exact_failed_component_available",
                    intent,
                    active_version_id,
                    portfolio_id=failure.get("portfolioId"),
                    source_user_turn_id=failure.get("sourceUserTurnId"),
                    source_assistant_turn_id=failure.get("sourceAssistantTurnId"),
                    allowed_actions=["ask_user"],
                    allowed_tools=[
                        "amap_place_around",
                        "amap_route",
                        "web_search",
                    ],
                    preconditions=failure,
                )
            return self._plan(
                "ask_retry_scope",
                "failed_component_identity_missing",
                intent,
                active_version_id,
                allowed_actions=["ask_user"],
            )
        if intent.intent_class == "continue_pending_choice":
            return self._plan(
                "ask_retry_scope",
                "no_active_partial_slot",
                intent,
                active_version_id,
                allowed_actions=["ask_user"],
                compatibility_failures=failures,
            )
        if intent.intent_class == "retry_contextual" and self._latest_complete_request(
            session_id
        ):
            if self._has_resumable_planning_attempt(session_id):
                return self._plan(
                    "resume_incomplete_stage",
                    "persisted_incomplete_stage_available",
                    intent,
                    active_version_id,
                    controller_allowed=False,
                    write_budget=1,
                    allowed_actions=["draft_itinerary"],
                    preconditions={
                        "checkpointReused": True,
                        "resumeOnly": True,
                    },
                )
            return self._plan(
                "regenerate_full",
                "no_recoverable_checkpoint",
                intent,
                active_version_id,
                controller_allowed=True,
                write_budget=1,
                allowed_actions=["draft_itinerary"],
                compatibility_failures=failures,
            )
        return self._plan(
            "ask_retry_scope",
            "no_recoverable_retry_target",
            intent,
            active_version_id,
            allowed_actions=["ask_user"],
            compatibility_failures=failures,
        )

    def _compatible_portfolio(
        self,
        *,
        session_id: str,
        latest_user_turn_id: str | None,
        request_contract_fingerprint: str | None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        row = self.db.execute(
            """SELECT * FROM agent_plan_portfolios
            WHERE session_id = ? AND status = 'awaiting_selection'
            ORDER BY created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None, ["awaiting_portfolio_missing"]
        raw = dict(row)
        failures: list[str] = []
        if self._expired(raw.get("expires_at")):
            failures.append("portfolio_expired")
        if str(raw.get("expected_base_version_id") or "") != str(
            self._active_version_id(session_id) or ""
        ):
            failures.append("portfolio_base_version_mismatch")
        if (
            request_contract_fingerprint
            and str(raw.get("request_contract_fingerprint") or "")
            != request_contract_fingerprint
        ):
            failures.append("portfolio_request_fingerprint_mismatch")
        if self._later_material_request(
            session_id,
            source_user_turn_id=str(raw.get("source_user_turn_id") or ""),
            latest_user_turn_id=latest_user_turn_id,
        ):
            failures.append("portfolio_superseded_by_material_request")
        source_assistant_turn_id = str(raw.get("source_assistant_turn_id") or "")
        source_assistant = self.db.execute(
            """SELECT status FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant'""",
            (source_assistant_turn_id, session_id),
        ).fetchone()
        if source_assistant is None:
            failures.append("portfolio_source_assistant_missing")
        elif str(source_assistant["status"] or "") != "active":
            failures.append("portfolio_source_assistant_superseded")
        try:
            summary = json.loads(raw.get("summary_json") or "{}")
        except (TypeError, ValueError):
            summary = {}
        visible_ids = {
            str(item) for item in summary.get("visibleProposalIds") or [] if str(item)
        }
        proposals = self.db.execute(
            """SELECT id, choice_id, status, brief_json, verifier_json
            FROM agent_plan_proposals
            WHERE portfolio_id = ? ORDER BY rank_index ASC LIMIT 4""",
            (raw["id"],),
        ).fetchall()
        offered: list[dict[str, Any]] = []
        for proposal in proposals:
            if visible_ids and str(proposal["id"]) not in visible_ids:
                continue
            try:
                verifier = json.loads(proposal["verifier_json"] or "{}")
                brief = json.loads(proposal["brief_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if str(proposal["status"] or "") != "offered" or verifier.get("passed") is not True:
                continue
            offered.append(
                {
                    "proposalId": str(proposal["id"]),
                    "choiceId": str(proposal["choice_id"]),
                    "title": str(brief.get("title") or "已验证方案"),
                    "primaryAxis": str(brief.get("primaryAxis") or ""),
                }
            )
        if not offered:
            failures.append("visible_verified_proposal_missing")
        if failures:
            return None, failures
        return {
            "portfolioId": str(raw["id"]),
            "sourceUserTurnId": str(raw["source_user_turn_id"]),
            "sourceAssistantTurnId": source_assistant_turn_id,
            "expectedBaseVersionId": raw.get("expected_base_version_id"),
            "requestContractFingerprint": str(
                raw.get("request_contract_fingerprint") or ""
            ),
            "proposals": offered,
        }, []

    def _portfolio_plan(
        self, intent: RetryIntentClassification, portfolio: dict[str, Any]
    ) -> RetryExecutionPlan:
        choices = [
            {
                "id": item["choiceId"],
                "choiceId": item["choiceId"],
                "action": "select_plan_proposal",
                "kind": "plan_proposal",
                "label": f"使用已验证方案：{item['title']}",
                "description": "复用已持久化且通过校验的方案；点击后才创建正式时间轴。",
                "sourceUserTurnId": portfolio["sourceUserTurnId"],
                "expectedBaseVersionId": portfolio.get("expectedBaseVersionId"),
                "summary": {
                    "primaryAxis": item.get("primaryAxis"),
                    "checkpointReused": True,
                },
            }
            for item in portfolio["proposals"]
        ]
        return self._plan(
            "resume_portfolio",
            "compatible_visible_portfolio",
            intent,
            None,
            portfolio_id=portfolio["portfolioId"],
            proposal_ids=[item["proposalId"] for item in portfolio["proposals"]],
            source_user_turn_id=portfolio["sourceUserTurnId"],
            source_assistant_turn_id=portfolio.get("sourceAssistantTurnId"),
            request_contract_fingerprint=portfolio.get(
                "requestContractFingerprint"
            ),
            choice_options=choices,
            allowed_actions=["ask_user"],
            preconditions={
                "checkpointReused": True,
                "stagedPipelineAllowed": False,
                "externalCallsAllowed": False,
            },
        )

    def _later_material_request(
        self,
        session_id: str,
        *,
        source_user_turn_id: str,
        latest_user_turn_id: str | None,
    ) -> bool:
        source = self.db.execute(
            "SELECT turn_index FROM conversation_turns WHERE id = ? AND session_id = ?",
            (source_user_turn_id, session_id),
        ).fetchone()
        if source is None:
            return True
        rows = self.db.execute(
            """SELECT id, content FROM conversation_turns
            WHERE session_id = ? AND role = 'user' AND status = 'active'
              AND turn_index > ? ORDER BY turn_index ASC""",
            (session_id, int(source["turn_index"])),
        ).fetchall()
        classifier = RetrySemanticClassifier()
        for row in rows:
            if latest_user_turn_id and str(row["id"]) == latest_user_turn_id:
                continue
            classification = classifier.classify(row["content"])
            if classification.intent_class in {
                "retry_contextual",
                "retry_failed_component",
                "continue_pending_choice",
                "regenerate_from_scratch",
            }:
                continue
            return True
        return False

    def _latest_exact_failure(self, session_id: str) -> dict[str, Any]:
        rows = self.db.execute(
            """SELECT id, agent_response_json FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant'
              AND status = 'active'
              AND agent_response_json IS NOT NULL
            ORDER BY turn_index DESC LIMIT 12""",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["agent_response_json"] or "{}")
            except (TypeError, ValueError):
                continue
            recovery = payload.get("failedComponentCheckpoint")
            if isinstance(recovery, dict) and recovery.get("failureCode"):
                return {
                    **recovery,
                    "sourceAssistantTurnId": str(row["id"]),
                }
        return {}

    def _active_partial_state(
        self, session_id: str, active_version_id: str
    ) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT snapshot_json FROM itinerary_versions
            WHERE id = ? AND session_id = ?""",
            (active_version_id, session_id),
        ).fetchone()
        if row is None:
            return {}
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, ValueError):
            return {}
        if (
            snapshot.get("status") == "partial"
            or snapshot.get("portfolioPartialTimeline")
            or snapshot.get("portfolioPendingSlots")
        ):
            return snapshot
        return {}

    def _partial_slot_choices(
        self, session_id: str, partial: dict[str, Any]
    ) -> list[dict[str, Any]]:
        selection_context = (
            partial.get("portfolioSelectionContext")
            if isinstance(partial.get("portfolioSelectionContext"), dict)
            else {}
        )
        required_context = {
            "planningSelectionRootTurnId": str(
                selection_context.get("planningSelectionRootTurnId") or ""
            ),
            "rootPortfolioId": str(selection_context.get("rootPortfolioId") or ""),
            "focusBriefId": str(selection_context.get("focusBriefId") or ""),
            "requestContractFingerprint": str(
                selection_context.get("requestContractFingerprint") or ""
            ),
        }
        if not all(required_context.values()):
            return []
        pending_keys = {
            (
                str(item.get("briefId") or ""),
                str(item.get("poolId") or ""),
                str(item.get("planningSlotId") or item.get("slotId") or ""),
                int(item.get("dayNumber") or 0),
            )
            for item in partial.get("portfolioPendingSlots") or []
            if isinstance(item, dict)
        }
        if not pending_keys:
            return []
        rows = self.db.execute(
            """SELECT agent_response_json FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant'
              AND status = 'active'
              AND agent_response_json IS NOT NULL
            ORDER BY turn_index DESC LIMIT 16""",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["agent_response_json"] or "{}")
            except (TypeError, ValueError):
                continue
            choices: list[dict[str, Any]] = []
            manual_choices: list[dict[str, Any]] = []
            for option in payload.get("choiceOptions") or []:
                if not isinstance(option, dict):
                    continue
                if str(option.get("kind") or "") not in {
                    "portfolio_density_candidate",
                    "portfolio_density_retry",
                    "portfolio_density_map",
                    "custom_input",
                }:
                    continue
                if any(
                    str(option.get(key) or "") != expected
                    for key, expected in required_context.items()
                ):
                    continue
                if str(option.get("kind") or "") == "custom_input":
                    manual_choices.append(dict(option))
                    continue
                key = (
                    str(option.get("briefId") or ""),
                    str(option.get("poolId") or ""),
                    str(
                        option.get("planningSlotId")
                        or option.get("slotId")
                        or ""
                    ),
                    int(option.get("dayNumber") or 0),
                )
                if key in pending_keys and str(option.get("id") or ""):
                    choices.append(dict(option))
            if choices:
                return [*choices, *manual_choices]
        return []

    def _latest_complete_request(self, session_id: str) -> bool:
        rows = self.db.execute(
            """SELECT content FROM conversation_turns
            WHERE session_id = ? AND role = 'user' AND status = 'active'
            ORDER BY turn_index DESC LIMIT 24""",
            (session_id,),
        ).fetchall()
        classifier = RetrySemanticClassifier()
        return any(
            classifier.classify(row["content"]).intent_class
            in {"not_retry", "modify_request"}
            for row in rows
        )

    def _has_resumable_planning_attempt(self, session_id: str) -> bool:
        rows = self.db.execute(
            """SELECT itinerary_version_id, agent_response_json FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant'
              AND status IN ('active', 'failed')
              AND agent_response_json IS NOT NULL
            ORDER BY turn_index DESC LIMIT 16""",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["agent_response_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if resumable_planning_checkpoint(
                payload,
                itinerary_version_id=row["itinerary_version_id"],
            ):
                return True
        return False

    def _active_version_id(self, session_id: str) -> str | None:
        row = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        value = str(row["active_version_id"] or "") if row is not None else ""
        return value or None

    @staticmethod
    def _expired(value: object) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")) <= datetime.now(
                timezone.utc
            )
        except ValueError:
            return True

    @staticmethod
    def _plan(
        kind: RetryExecutionKind,
        reason: str,
        intent: RetryIntentClassification,
        active_version_id: str | None,
        **updates: Any,
    ) -> RetryExecutionPlan:
        return RetryExecutionPlan(
            kind=kind,
            reasonCode=reason,
            intentClass=intent.intent_class,
            activeVersionId=active_version_id,
            **updates,
        )

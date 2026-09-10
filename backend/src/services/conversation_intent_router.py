from __future__ import annotations

import json
import copy
import re
import sqlite3
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from src.services.itinerary_snapshot_service import ItinerarySnapshotService


ConversationIntent = Literal[
    "create_itinerary",
    "modify_itinerary",
    "continue_plan_expansion",
    "adopt_plan",
    "continue_pending_slot",
    "manual_candidate_search",
    "search_travel_guide_advice",
    "retry_current_stage",
    "regenerate_from_scratch",
    "inspect_or_explain",
    "cancel_action",
    "clarification_answer",
]

RequestedScope = Literal[
    "none",
    "new_itinerary",
    "active_itinerary",
    "planning_root",
    "portfolio",
    "pending_slot",
    "candidate_search",
    "current_stage",
    "full_task",
    "current_action",
    "clarification",
]

ExecutionDisposition = Literal[
    "execute",
    "read_only",
    "no_write",
    "clarify",
    "direct_choice",
]

ContinuationMode = Literal["guide_grounded"]
IntentRoutingMode = Literal["legacy-only", "shadow", "active-read", "active-all", "kill-switch"]
IntentRiskTier = Literal["read_only", "proposal_only", "mutation", "commit"]
TargetStatus = Literal["not_required", "unique", "none", "ambiguous", "stale"]
TargetKind = Literal[
    "none",
    "current_itinerary",
    "planning_root",
    "portfolio",
    "pending_slot",
    "proposal_ordinal",
    "day",
    "time_window",
    "latest_guide",
    "current_stage",
    "clarification",
]


class SemanticTargetReference(BaseModel):
    """Model-safe reference language; execution identities are intentionally absent."""

    kind: TargetKind = "none"
    ordinal: Optional[int] = Field(default=None, ge=1, le=12)
    day_number: Optional[int] = Field(default=None, alias="dayNumber", ge=1, le=31)
    time_bucket: Optional[Literal["morning", "afternoon", "evening"]] = Field(
        default=None,
        alias="timeBucket",
    )
    mention_text: Optional[str] = Field(default=None, alias="mentionText", max_length=120)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class SemanticIntentHypothesis(BaseModel):
    intent: ConversationIntent
    confidence: float = Field(ge=0, le=1)
    requested_scope: RequestedScope = Field(alias="requestedScope")
    is_question: bool = Field(alias="isQuestion")
    is_negated: bool = Field(alias="isNegated")
    continuation_mode: Optional[Literal["general", "guide_grounded"]] = Field(
        default=None,
        alias="continuationMode",
    )
    target_reference: SemanticTargetReference = Field(
        default_factory=SemanticTargetReference,
        alias="targetReference",
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


class SemanticSignals(BaseModel):
    quoted_command: bool = Field(default=False, alias="quotedCommand")
    hypothetical: bool = False
    correction_after_negation: bool = Field(default=False, alias="correctionAfterNegation")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ConversationIntentHypothesisV2(BaseModel):
    schema_version: Literal["conversation-intent-hypothesis-v2"] = Field(alias="schemaVersion")
    primary: SemanticIntentHypothesis
    alternatives: list[SemanticIntentHypothesis] = Field(default_factory=list, max_length=2)
    semantic_signals: SemanticSignals = Field(default_factory=SemanticSignals, alias="semanticSignals")

    model_config = {"populate_by_name": True, "extra": "forbid"}


@dataclass(frozen=True)
class IntentRoutingSnapshotV1:
    """One-turn immutable state used by semantics, target and capability gates.

    ``server_state`` may contain opaque identities and is never sent to a model.
    ``model_projection`` is bounded and contains semantic summaries only.
    """

    fingerprint: str
    model_projection: dict[str, Any]
    server_state: dict[str, Any]

    def __post_init__(self) -> None:
        for name in ("model_projection", "server_state"):
            object.__setattr__(self, name, copy.deepcopy(object.__getattribute__(self, name)))

    def __getattribute__(self, name: str) -> Any:
        value = object.__getattribute__(self, name)
        return copy.deepcopy(value) if name in {"model_projection", "server_state"} else value


@dataclass(frozen=True)
class ConversationTargetResolution:
    status: TargetStatus
    reason_code: str
    target: Optional[dict[str, Any]] = None

    def to_context(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasonCode": self.reason_code,
            "target": dict(self.target or {}),
        }


class ConversationIntentClassification(BaseModel):
    """The complete model-facing intent contract.

    Execution identities are deliberately absent. ``extra=forbid`` makes any
    model attempt to emit a choice, portfolio, version, or write target fail
    closed before capability resolution.
    """

    intent: ConversationIntent
    confidence: float = Field(ge=0, le=1)
    requested_scope: RequestedScope = Field(alias="requestedScope")
    is_question: bool = Field(alias="isQuestion")
    is_negated: bool = Field(alias="isNegated")

    model_config = {"populate_by_name": True, "extra": "forbid"}


@dataclass(frozen=True)
class ConversationIntentRoutingResult:
    classification: Optional[ConversationIntentClassification]
    source: Literal[
        "persisted_opaque_choice",
        "deterministic_fast_path",
        "lite_controller",
        "state_aware_lite",
        "legacy_fallback",
        "safe_fallback",
    ]
    model_attempted: bool
    model_called: bool
    model_succeeded: bool
    requires_clarification: bool
    reason_code: str
    execution_disposition: ExecutionDisposition
    model_performance_evidence: tuple[dict[str, Any], ...] = ()
    continuation_mode: Optional[ContinuationMode] = None
    target_reference: Optional[dict[str, Any]] = None
    risk_tier: Optional[IntentRiskTier] = None
    state_fingerprint: Optional[str] = None
    shadow_evaluation: Optional[dict[str, Any]] = None
    invocation_ledger: Optional[dict[str, int]] = None
    execution_intent: Optional[dict[str, Any]] = None
    semantic_action: Optional[dict[str, Any]] = None

    def to_context(self) -> dict[str, Any]:
        context = {
            "classification": (
                self.classification.model_dump(by_alias=True) if self.classification is not None else None
            ),
            "source": self.source,
            "modelAttempted": self.model_attempted,
            "modelCalled": self.model_called,
            "modelSucceeded": self.model_succeeded,
            "modelPerformanceEvidence": [dict(item) for item in self.model_performance_evidence],
            "requiresClarification": self.requires_clarification,
            "reasonCode": self.reason_code,
            "executionDisposition": self.execution_disposition,
        }
        if self.continuation_mode is not None:
            context["continuationMode"] = self.continuation_mode
        if self.target_reference is not None:
            context["targetReference"] = dict(self.target_reference)
        if self.risk_tier is not None:
            context["riskTier"] = self.risk_tier
        if self.state_fingerprint is not None:
            context["stateFingerprint"] = self.state_fingerprint
        if self.shadow_evaluation is not None:
            context["shadowEvaluation"] = dict(self.shadow_evaluation)
        if self.invocation_ledger is not None:
            context["invocationLedger"] = dict(self.invocation_ledger)
        if self.execution_intent is not None:
            context["executionIntent"] = dict(self.execution_intent)
        if self.semantic_action is not None:
            context["semanticAction"] = copy.deepcopy(self.semantic_action)
        return context


@dataclass(frozen=True)
class ConversationCapabilityResolution:
    status: Literal["unique", "none", "ambiguous", "not_required"]
    capability: Optional[ConversationIntent]
    selected_choice_request: Optional[dict[str, str]]
    reason_code: str
    planning_root_id: Optional[str] = None
    root_portfolio_id: Optional[str] = None
    request_contract_fingerprint: Optional[str] = None
    expected_base_version_id: Optional[str] = None
    focus_brief_id: Optional[str] = None
    target_resolution: Optional[dict[str, Any]] = None

    @property
    def requires_clarification(self) -> bool:
        return self.status in {"none", "ambiguous"}

    def to_context(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "capability": self.capability,
            "selectedChoiceRequest": self.selected_choice_request,
            "reasonCode": self.reason_code,
            "planningSelectionRootTurnId": self.planning_root_id,
            "rootPortfolioId": self.root_portfolio_id,
            "requestContractFingerprint": self.request_contract_fingerprint,
            "expectedBaseVersionId": self.expected_base_version_id,
            "focusBriefId": self.focus_brief_id,
            "targetResolution": dict(self.target_resolution or {}),
        }


@dataclass(frozen=True)
class ConversationIntentModelOutcome:
    value: Any = None
    error_code: Optional[str] = None
    provider_invoked: bool = False
    performance_evidence: tuple[dict[str, Any], ...] = ()


LiteIntentClassifier = Callable[[dict[str, Any]], Any]


class ConversationIntentRouter:
    """Classify one conversational turn without selecting an execution target."""

    CONTEXT_SCHEMA_VERSION = "conversation-intent-context-v1"
    STATE_AWARE_CONTEXT_SCHEMA_VERSION = "conversation-intent-context-v2"
    MODEL_CONTEXT_BYTE_LIMIT = 5_500
    MIN_MODEL_CONFIDENCE = 0.82
    RISK_THRESHOLDS: dict[IntentRiskTier, float] = {
        "read_only": 0.72,
        "proposal_only": 0.82,
        "mutation": 0.90,
        "commit": 0.95,
    }

    _QUESTION_PREFIX = re.compile(
        r"^\s*(?:请问)?(?:为什么|为何|怎么会|怎么|请解释|解释一下|说明一下|"
        r"what\b|why\b|how\b)",
        re.IGNORECASE,
    )
    _CANCEL_PREFIX = re.compile(
        r"^\s*(?:(?:停止|取消|撤销)(?:当前|本轮|这个|该)?(?:动作|操作|任务)?|"
        r"(?:不要|别再?|不用|先别|无需)\s*(?:继续|再|重新)?"
        r"(?:生成|新增|创建|执行|采用|选择|重试|补全|搜索))",
        re.IGNORECASE,
    )
    _PURE_CANCELLATION = re.compile(
        r"\s*(?:(?:停止|取消|撤销)(?:当前|本轮|这个|该)?(?:动作|操作|任务|规划)?|"
        r"(?:不要|别再?|不用|先别|无需)\s*(?:继续|再|重新)?"
        r"(?:生成|新增|创建|执行|采用|选择|重试|补全|搜索)"
        r"(?:当前|本轮|这个|该|整个|其他|新的?|不同)?(?:的)?(?:方案|行程|规划|攻略|动作|操作|任务)?)"
        r"[。.!！?？]?\s*",
        re.IGNORECASE,
    )
    _EXPLICIT_EXPANSION = re.compile(
        r"^\s*(?:请)?继续(?:新增|生成|创建|探索)"
        r"(?:一个|一种|一套)?(?:其他|新的|不同)?(?:方向)?方案[。.!！?？]?\s*$"
        r"|^\s*(?:请)?继续探索(?:其他|新的|不同)?方向[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_REGENERATE = re.compile(
        r"^\s*(?:请)?(?:全部|整个|从头|重新从头)?"
        r"(?:从头重新生成|全部重新生成|重新生成整个行程|推倒重来|"
        r"重新规划.{1,24}(?:行程|[一二三四五六七八九十\d]+天))"
        r"[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_RETRY = re.compile(
        r"^\s*(?:请)?(?:(?:再)?(?:重试|再试)(?:一遍|一次|一下)?|"
        r"继续(?:当前|刚才|上次)?(?:阶段|步骤|失败项))"
        r"[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_CREATE = re.compile(
        r"^\s*(?:请)?(?:创建|规划|生成)(?:一个|一份|一套)?"
        r".{0,24}(?:行程|旅行计划)[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _REFINE_AND_CONTINUE = re.compile(
        r"(?=.*(?:指的是|改为|改成|限定|只要|必须|最好|尽量|优先|不要|取消))"
        r"(?=.*(?:继续|重新)(?:生成|规划).*(?:方案|行程))",
        re.IGNORECASE,
    )
    _EXPLICIT_MODIFY = re.compile(
        r"^\s*(?:请)?(?:修改|调整|更新)(?:当前|这个|现有)?"
        r"(?:行程|旅行计划)[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_ADOPT = re.compile(
        r"^\s*(?:请)?(?:采用|选用|选择|使用)(?:这个|当前|该)?方案"
        r"[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_PENDING = re.compile(
        r"^\s*(?:请)?继续(?:补全|补充|处理)(?:当前|这个|该)?"
        r"(?:待补槽位|缺失槽位|缺失地点)[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_MANUAL_SEARCH = re.compile(
        r"^\s*(?:请)?(?:手动|按名称|用名称)(?:搜索|查找)(?:候选|地点)?"
        r"[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _EXPLICIT_GUIDE_SEARCH = re.compile(
        r"^\s*(?:请)?(?:重新|再次|再)?(?:搜索|检索|查找|搜)"
        r"(?:一遍|一次|一下)?(?:普通|旅游|旅行)?攻略"
        r"(?:并?给我(?:一些)?建议)?[。.!！?？]?\s*$",
        re.IGNORECASE,
    )
    _GUIDE_GROUNDED_TOPIC = re.compile(
        r"(?=.*(?:(?:刚才|之前|上次|上述|上面|这份|这个)(?:的)?\s*(?:普通|旅游|旅行)?攻略|"
        r"(?:普通|旅游|旅行)?攻略.{0,32}"
        r"(?:(?:建议|推荐|提到|列出|整理).{0,12})?"
        r"(?:这些?|上述|上面)?(?:地点|地方|景点|餐厅|目的地|去处)))"
        r"(?=.*(?:生成|规划|创建|设计|安排|做|产出|给(?:我)?出|给我|给).{0,24}"
        r"(?:方案|行程|旅行计划))",
        re.IGNORECASE | re.DOTALL,
    )
    _GUIDE_GROUNDED_REQUIREMENT_PLAN = re.compile(
        r"^\s*(?:(?:请|请你|麻烦(?:你)?|帮我|请帮我)\s*)?"
        r"(?:参考|根据|基于|采用|使用|用|结合|按照?|依照)"
        r".{0,40}(?:普通|旅游|旅行)?攻略"
        r".{0,32}(?:地点|地方|景点|餐厅|目的地|去处)"
        r".{0,48}(?:生成|规划|创建|设计|安排|做|产出|给(?:我)?出|给我|给)"
        r"(?:一|另)?(?:个|种|套|份)?\s*"
        r"(?:(?:新的?|其他|不同|另一(?:个|种|套|份)?)(?:的)?(?:方向)?\s*)?"
        r"(?:旅行)?(?:方案|行程|计划)(?:吧|一下)?[。.!！]?\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    _GUIDE_GROUNDED_EXPANSION = re.compile(
        r"^\s*(?:(?:请|请你|麻烦(?:你)?|帮我|请帮我)\s*)?"
        r"(?:(?:那就|就|接下来|然后)\s*)?"
        r"(?:参考|根据|基于|采用|使用|用|结合|按照?|依照)"
        r".{0,24}(?:普通|旅游|旅行)?攻略"
        r"(?:"
        r".{0,24}(?:建议|推荐|提到|列出|整理)"
        r"(?:的|中|里|里的|中的)?(?:这些?|上述|上面)?\s*"
        r"|(?:中|里|里的|中的|给出的|包含的|所列的)\s*(?:这些?)?"
        r")"
        r"(?:地点|地方|景点|餐厅|目的地|去处)"
        r"[,，、；;:\s]*"
        r"(?:(?:请|帮我|再|继续|重新|接着|然后|并|来|以此|用于|作为参考)\s*)?"
        r"(?:(?:再|继续|重新)\s*)?"
        r"(?:生成|规划|创建|设计|安排|做|产出)"
        r"(?:一|另)?(?:个|种|套|份)?\s*"
        r"(?:新的?|其他|不同|另一(?:个|种|套|份)?)"
        r"(?:的)?(?:方向)?(?:旅行)?(?:方案|行程|计划)"
        r"(?:吧|一下)?[。.!！]?\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    _GUIDE_GROUNDED_CONTEXTUAL_EXPANSION = re.compile(
        r"^\s*(?:(?:请|请你|麻烦(?:你)?|帮我|请帮我)\s*)?"
        r"(?:参考|根据|基于|采用|使用|用|结合|按照?|依照)"
        r".{0,12}(?:刚才|之前|上次|上述|上面|这份|这个)(?:的)?\s*(?:普通|旅游|旅行)?攻略"
        r".{0,16}(?:再|继续|重新)?\s*(?:生成|规划|创建|设计|安排|做|产出)"
        r"(?:一|另)?(?:个|种|套|份)?\s*(?:新的?|其他|不同|另一(?:个|种|套|份)?)"
        r"(?:的)?(?:方向)?(?:旅行)?(?:方案|行程|计划)(?:吧|一下)?[。.!！]?\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    _GUIDE_GROUNDED_NEGATION = re.compile(
        r"(?:不要|别(?:再)?|不用|无需|不(?:再|想|需要|必)?|停止|取消|放弃)"
        r".{0,8}(?:参考|根据|基于|采用|使用|结合|按照?|依照|"
        r"生成|规划|创建|设计|安排|做|产出|给(?:我)?出|给我|给)",
        re.IGNORECASE | re.DOTALL,
    )
    _GUIDE_GROUNDED_DISCUSSION = re.compile(
        r"(?:为什么|为何|怎么(?:会|没|不|还)?|如何|是否|是不是|能否|可否|"
        r"要不要|什么(?:意思|含义)|失败|报错|错误|故障|无法|不能|"
        r"没成功|没有成功|没生成|没有生成|这句话|这个(?:指令|说法|文案|按钮|标签)|"
        r"引用|讨论|解释|分析|评价|复述|改写|翻译|你(?:刚才)?说(?:的)?|"
        r"(?:用户|他|她|文档|需求).{0,6}(?:说|写|提到))"
        r"|(?:系统|规划器|控制器|模型|助手|Agent|你)"
        r"(?:已经|已|刚刚|刚才|曾经|此前|可以|能够|会|将要|将)"
        r".{0,8}(?:给出|生成|规划|创建|设计|安排|产出)"
        r"|^\s*[\"'“‘].*[\"'”’]\s*[。.!！]?\s*$"
        r"|[?？]\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    _NON_EXECUTION_CONTEXT = re.compile(
        r"(?:如果|假如|假设).{0,64}(?:会|是否|怎么样|发生什么)"
        r"|(?:文档|需求|说明|代码|日志|你刚才说).{0,24}[‘'\"“].+[’'\"”]"
        r"|[‘'\"“].+[’'\"”].{0,24}(?:什么意思|含义|为什么|如何|是否|会发生什么)"
        r"|(?:这句话|这个指令|这个说法).{0,24}(?:意思|解释|分析|讨论)",
        re.IGNORECASE | re.DOTALL,
    )
    _CORRECTION_AFTER_NEGATION = re.compile(
        r"(?:不是|不用|不要|别).{0,32}(?:而是|是要|改成|换成|调整为|直接)",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(
        self,
        lite_classifier: Optional[LiteIntentClassifier] = None,
        *,
        min_model_confidence: float = MIN_MODEL_CONFIDENCE,
        routing_mode: IntentRoutingMode = "legacy-only",
    ) -> None:
        self.lite_classifier = lite_classifier
        self.min_model_confidence = min_model_confidence
        self.routing_mode = routing_mode

    def bypass_persisted_choice(self) -> ConversationIntentRoutingResult:
        return ConversationIntentRoutingResult(
            classification=None,
            source="persisted_opaque_choice",
            model_attempted=False,
            model_called=False,
            model_succeeded=False,
            requires_clarification=False,
            reason_code="persisted_opaque_choice_bypasses_intent_model",
            execution_disposition="direct_choice",
        )

    def classify(
        self,
        message: str,
        *,
        state_summary: Optional[dict[str, Any]] = None,
        routing_snapshot: Optional[IntentRoutingSnapshotV1] = None,
    ) -> ConversationIntentRoutingResult:
        literal_message = str(message or "").strip()
        if self.routing_mode in {"legacy-only", "kill-switch"}:
            return self._classify_legacy(literal_message, state_summary=state_summary or {})

        if self.routing_mode == "active-all":
            safety = self._safety_classification(literal_message)
            # A prefix only proves that one clause is negated. In a compound
            # request the user may be cancelling generation but asking for an
            # explanation, or correcting one requirement. Let the same semantic
            # entry interpret the whole message; do not discard later clauses.
            if (safety is not None and safety.intent == "cancel_action"
                    and self._CANCEL_PREFIX.search(literal_message)
                    and not self._PURE_CANCELLATION.fullmatch(literal_message)):
                safety = None
            if safety is not None and safety.intent in {"cancel_action", "inspect_or_explain"}:
                return self._accepted_result(safety, source="deterministic_fast_path", model_called=False,
                    reason_code="deterministic_safety_short_circuit", state_fingerprint=(routing_snapshot.fingerprint if routing_snapshot else None),
                    invocation_ledger={"semanticActionCallCount": 0, "controllerLiteCallCount": 0})
            return self._classify_action(literal_message, routing_snapshot or self._compatibility_snapshot(state_summary or {}))

        return self._classify_hypothesis(literal_message, state_summary=state_summary, routing_snapshot=routing_snapshot)

    def _classify_hypothesis(self, literal_message: str, *, state_summary: Optional[dict[str, Any]] = None,
                            routing_snapshot: Optional[IntentRoutingSnapshotV1] = None) -> ConversationIntentRoutingResult:
        """Retained V2 hypothesis contract for shadow/active-read compatibility."""

        safety = self._safety_classification(literal_message)
        if safety is not None:
            continuation_mode = self._continuation_mode(literal_message, safety)
            return self._accepted_result(
                safety,
                source="deterministic_fast_path",
                model_called=False,
                reason_code="deterministic_safety_short_circuit",
                continuation_mode=continuation_mode,
                state_fingerprint=(routing_snapshot.fingerprint if routing_snapshot is not None else None),
                risk_tier=self._risk_tier(safety.intent),
                invocation_ledger=self._intent_ledger(attempted=False, called=False),
            )

        shadow_legacy: Optional[ConversationIntentClassification] = None
        if self.routing_mode == "shadow":
            shadow_legacy = self._deterministic_classification(literal_message)
            if shadow_legacy is None:
                # A true shadow must not replace the existing Lite authority with
                # an observational model.  When the legacy deterministic router
                # has no answer, keep the rolling V1 Lite contract exactly as it
                # was.  This also preserves the one-intent-model-call invariant.
                return self._classify_legacy(literal_message, state_summary=state_summary or {})

        snapshot = routing_snapshot or self._compatibility_snapshot(state_summary or {})
        model_context = self._state_aware_model_context(literal_message, snapshot)
        raw, called, evidence, failure = self._invoke_classifier(model_context)
        if failure is not None:
            return self._legacy_after_model_failure(
                literal_message,
                failure_reason=failure,
                model_called=called,
                performance_evidence=evidence,
                snapshot=snapshot,
            )

        if self.routing_mode == "shadow" and isinstance(raw, dict) and "schemaVersion" not in raw:
            try:
                legacy_model = self._enforce_non_execution_signals(
                    ConversationIntentClassification.model_validate(raw)
                )
            except ValidationError:
                return self._legacy_after_model_failure(
                    literal_message,
                    failure_reason="intent_model_contract_invalid",
                    model_called=called,
                    performance_evidence=evidence,
                    snapshot=snapshot,
                    allow_legacy_fallback=False,
                )
            assert shadow_legacy is not None
            return self._accepted_result(
                shadow_legacy,
                source="deterministic_fast_path",
                model_attempted=True,
                model_called=called,
                model_succeeded=True,
                reason_code="shadow_mode_legacy_authoritative",
                model_performance_evidence=evidence,
                continuation_mode=self._continuation_mode(literal_message, shadow_legacy),
                risk_tier=self._risk_tier(shadow_legacy.intent),
                state_fingerprint=snapshot.fingerprint,
                shadow_evaluation={
                    "schemaVersion": "conversation-intent-classification-v1",
                    **legacy_model.model_dump(by_alias=True),
                },
                invocation_ledger=self._intent_ledger(attempted=True, called=called),
            )

        try:
            hypothesis = self._parse_state_aware_hypothesis(raw)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError):
            return self._legacy_after_model_failure(
                literal_message,
                failure_reason="intent_model_contract_invalid",
                model_called=called,
                performance_evidence=evidence,
                snapshot=snapshot,
                allow_legacy_fallback=not self._contains_forbidden_execution_authority(raw),
            )

        primary = self._enforce_state_aware_non_execution(hypothesis)
        classification = ConversationIntentClassification(
            intent=primary.intent,
            confidence=primary.confidence,
            requestedScope=primary.requested_scope,
            isQuestion=primary.is_question,
            isNegated=primary.is_negated,
        )
        risk_tier = self._risk_tier(classification.intent)
        continuation_mode: Optional[ContinuationMode] = (
            "guide_grounded" if primary.continuation_mode == "guide_grounded" else None
        )
        target_reference = primary.target_reference.model_dump(by_alias=True, exclude_none=True)
        shadow = {
            "intent": classification.intent,
            "requestedScope": classification.requested_scope,
            "confidence": classification.confidence,
            "continuationMode": continuation_mode,
            "targetReference": target_reference,
        }
        if self.routing_mode == "shadow":
            assert shadow_legacy is not None
            accepted = self._accepted_result(
                shadow_legacy,
                source="deterministic_fast_path",
                model_attempted=True,
                model_called=called,
                model_succeeded=True,
                reason_code="shadow_mode_legacy_authoritative",
                model_performance_evidence=evidence,
                continuation_mode=self._continuation_mode(literal_message, shadow_legacy),
                state_fingerprint=snapshot.fingerprint,
                risk_tier=self._risk_tier(shadow_legacy.intent),
                shadow_evaluation=shadow,
                invocation_ledger=self._intent_ledger(attempted=True, called=called),
            )
            return accepted

        if self._hypothesis_is_ambiguous(primary, hypothesis.alternatives):
            return ConversationIntentRoutingResult(
                classification=classification,
                source="state_aware_lite",
                model_attempted=True,
                model_called=called,
                model_succeeded=True,
                requires_clarification=True,
                reason_code="intent_semantic_ambiguity",
                execution_disposition="clarify",
                model_performance_evidence=evidence,
                continuation_mode=continuation_mode,
                target_reference=target_reference,
                risk_tier=risk_tier,
                state_fingerprint=snapshot.fingerprint,
                invocation_ledger=self._intent_ledger(attempted=True, called=called),
            )
        threshold = self.RISK_THRESHOLDS[risk_tier]
        if classification.confidence < threshold:
            return ConversationIntentRoutingResult(
                classification=classification,
                source="state_aware_lite",
                model_attempted=True,
                model_called=called,
                model_succeeded=True,
                requires_clarification=True,
                reason_code="intent_confidence_below_risk_threshold",
                execution_disposition="clarify",
                model_performance_evidence=evidence,
                continuation_mode=continuation_mode,
                target_reference=target_reference,
                risk_tier=risk_tier,
                state_fingerprint=snapshot.fingerprint,
                invocation_ledger=self._intent_ledger(attempted=True, called=called),
            )
        if self.routing_mode == "active-read" and risk_tier in {
            "proposal_only",
            "mutation",
            "commit",
        }:
            legacy = self._deterministic_classification(literal_message)
            if legacy is None:
                return ConversationIntentRoutingResult(
                    classification=classification,
                    source="safe_fallback",
                    model_attempted=True,
                    model_called=called,
                    model_succeeded=True,
                    requires_clarification=True,
                    reason_code="intent_routing_mode_write_disabled",
                    execution_disposition="clarify",
                    model_performance_evidence=evidence,
                    target_reference=target_reference,
                    risk_tier=risk_tier,
                    state_fingerprint=snapshot.fingerprint,
                    shadow_evaluation=shadow,
                    invocation_ledger=self._intent_ledger(attempted=True, called=called),
                )
            return self._accepted_result(
                legacy,
                source="legacy_fallback",
                model_attempted=True,
                model_called=called,
                model_succeeded=True,
                reason_code="active_read_legacy_write_fallback",
                model_performance_evidence=evidence,
                continuation_mode=self._continuation_mode(literal_message, legacy),
                risk_tier=self._risk_tier(legacy.intent),
                state_fingerprint=snapshot.fingerprint,
                shadow_evaluation=shadow,
                invocation_ledger=self._intent_ledger(attempted=True, called=called),
            )
        return self._accepted_result(
            classification,
            source="state_aware_lite",
            model_attempted=True,
            model_called=called,
            model_succeeded=True,
            reason_code="state_aware_semantics_accepted",
            model_performance_evidence=evidence,
            continuation_mode=continuation_mode,
            target_reference=target_reference,
            risk_tier=risk_tier,
            state_fingerprint=snapshot.fingerprint,
            invocation_ledger=self._intent_ledger(attempted=True, called=called),
        )

    def _classify_legacy(
        self,
        literal_message: str,
        *,
        state_summary: dict[str, Any],
    ) -> ConversationIntentRoutingResult:
        deterministic = self._deterministic_classification(literal_message)
        if deterministic is not None:
            return self._accepted_result(
                deterministic,
                source="deterministic_fast_path",
                model_called=False,
                reason_code="high_confidence_explicit_command",
                continuation_mode=self._continuation_mode(
                    literal_message,
                    deterministic,
                ),
            )

        if self.lite_classifier is None:
            return self._clarification_result(
                model_attempted=False,
                model_called=False,
                reason_code="intent_model_unavailable",
            )

        model_context = self._model_context(literal_message, state_summary or {})
        model_called = False
        performance_evidence: tuple[dict[str, Any], ...] = ()
        try:
            raw = self.lite_classifier(model_context)
            if isinstance(raw, ConversationIntentModelOutcome):
                model_called = raw.provider_invoked
                performance_evidence = raw.performance_evidence
                if raw.error_code:
                    return self._clarification_result(
                        model_attempted=True,
                        model_called=model_called,
                        reason_code="intent_model_failed",
                        model_performance_evidence=performance_evidence,
                    )
                raw = raw.value
            else:
                # A direct classifier that returned has crossed its invocation
                # boundary. Controller-backed classifiers return the typed
                # outcome above so queue/pre-submit failures stay distinguishable.
                model_called = True
            if isinstance(raw, str):
                raw = json.loads(raw)
            classification = ConversationIntentClassification.model_validate(raw)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError):
            return self._clarification_result(
                model_attempted=True,
                model_called=model_called,
                reason_code="intent_model_contract_invalid",
                model_performance_evidence=performance_evidence,
            )
        except Exception:
            return self._clarification_result(
                model_attempted=True,
                model_called=False,
                reason_code="intent_model_failed",
                model_performance_evidence=performance_evidence,
            )

        classification = self._enforce_non_execution_signals(classification)
        if classification.confidence < self.min_model_confidence:
            return ConversationIntentRoutingResult(
                classification=classification,
                source="lite_controller",
                model_attempted=True,
                model_called=model_called,
                model_succeeded=True,
                requires_clarification=True,
                reason_code="intent_confidence_below_threshold",
                execution_disposition="clarify",
                model_performance_evidence=performance_evidence,
            )
        return self._accepted_result(
            classification,
            source="lite_controller",
            model_attempted=True,
            model_called=model_called,
            model_succeeded=True,
            reason_code="intent_model_contract_accepted",
            model_performance_evidence=performance_evidence,
            continuation_mode=self._continuation_mode(
                literal_message,
                classification,
            ),
        )

    def _classify_action(self, message: str, snapshot: IntentRoutingSnapshotV1) -> ConversationIntentRoutingResult:
        from src.services.conversation_action_catalog import ConversationActionCatalog, parse_action_response

        catalog = ConversationActionCatalog(snapshot)
        raw, called, evidence, failure = self._invoke_classifier({
            "schemaVersion": "conversation-action-context-v1",
            "userMessage": message,
            "state": snapshot.model_projection,
            "tools": catalog.tools(),
        })
        ledger = {**self._intent_ledger(attempted=True, called=called),
                  "semanticActionCallCount": int(called), "controllerLiteCallCount": 0}
        if failure is None:
            try:
                action, arguments = parse_action_response(raw, catalog)
                if not called:
                    failure = "semantic_action_provider_not_invoked"
                # Existing high precision rules are vetoes, never a new
                # positive execution path. Known guide obligations cannot be
                # downgraded by a syntactically valid model proposal.
                known = self._deterministic_classification(message)
                if known is not None and self._continuation_mode(message, known) == "guide_grounded":
                    if action.name not in {"continue_with_guide", "clarify", "unavailable"} and not (
                        action.name == "create_from_shared_guide" and snapshot.server_state.get("sharedInitialSource")
                    ):
                        failure = "semantic_action_guide_requirement_mismatch"
            except ValueError as error:
                failure = str(error)
        if failure is not None:
            # Only transport/unavailability may use existing rules. Malformed or
            # injected output is not a reason to execute a different action.
            fallback = self._deterministic_classification(message) if failure in {
                "intent_provider_unavailable", "intent_model_unavailable"
            } else None
            compatible = [item for item in catalog.actions if fallback is not None and item.intent == fallback.intent
                          and item.continuation_mode == self._continuation_mode(message, fallback)]
            if len(compatible) == 1:
                return self._accepted_result(fallback, source="legacy_fallback", model_attempted=True,
                    model_called=called, reason_code=failure, model_performance_evidence=evidence,
                    continuation_mode=compatible[0].continuation_mode,
                    target_reference={"kind": compatible[0].target}, state_fingerprint=snapshot.fingerprint,
                    invocation_ledger=ledger)
            return replace(self._clarification_result(model_attempted=True, model_called=called,
                reason_code=failure, model_performance_evidence=evidence),
                state_fingerprint=snapshot.fingerprint, invocation_ledger=ledger)
        target = arguments.target.model_dump(by_alias=True, exclude_none=True)
        if target.get("kind") == "none":
            target = {"kind": action.target}
        result = self._accepted_result(
            ConversationIntentClassification(intent=action.intent, confidence=1.0, requestedScope=action.scope,
                isQuestion=action.name in {"explain", "clarify", "unavailable"}, isNegated=False),
            source="state_aware_lite", model_attempted=True, model_called=called, model_succeeded=True,
            reason_code="semantic_action_accepted", model_performance_evidence=evidence,
            continuation_mode=action.continuation_mode, target_reference=target,
            state_fingerprint=snapshot.fingerprint, invocation_ledger=ledger,
            risk_tier=self._risk_tier(action.intent),
        )
        # Semantic adoption only requests an explicit confirmation; do not turn
        # a model-selected ordinal into permission to commit.
        result = replace(result, semantic_action={
            "name": action.name,
            "requestedAction": arguments.requested_action,
            "guideUnavailableReason": (snapshot.server_state.get("guideUnavailableReason")
                                       if arguments.requested_action == "continue_with_guide" else None),
        })
        if action.name in {"clarify", "unavailable", "request_adoption"}:
            result = replace(result, requires_clarification=True, execution_disposition="clarify",
                reason_code={"clarify": "intent_semantic_ambiguity", "unavailable": "semantic_action_unavailable",
                             "request_adoption": "proposal_confirmation_required"}[action.name])
        return result

    def _invoke_classifier(
        self,
        model_context: dict[str, Any],
    ) -> tuple[Any, bool, tuple[dict[str, Any], ...], Optional[str]]:
        if self.lite_classifier is None:
            return None, False, (), "intent_model_unavailable"
        called = False
        evidence: tuple[dict[str, Any], ...] = ()
        try:
            raw = self.lite_classifier(model_context)
            if isinstance(raw, ConversationIntentModelOutcome):
                called = raw.provider_invoked
                evidence = raw.performance_evidence
                if raw.error_code:
                    return None, called, evidence, "intent_provider_unavailable"
                raw = raw.value
            else:
                called = True
            if isinstance(raw, str):
                raw = json.loads(raw)
            return raw, called, evidence, None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, called, evidence, "intent_model_contract_invalid"
        except Exception:
            return None, False, evidence, "intent_provider_unavailable"

    def _legacy_after_model_failure(
        self,
        literal_message: str,
        *,
        failure_reason: str,
        model_called: bool,
        performance_evidence: tuple[dict[str, Any], ...],
        snapshot: IntentRoutingSnapshotV1,
        allow_legacy_fallback: bool = True,
    ) -> ConversationIntentRoutingResult:
        legacy = self._deterministic_classification(literal_message) if allow_legacy_fallback else None
        if legacy is not None:
            return self._accepted_result(
                legacy,
                source="legacy_fallback",
                model_attempted=True,
                model_called=model_called,
                model_succeeded=False,
                reason_code=f"{failure_reason}_legacy_fallback",
                model_performance_evidence=performance_evidence,
                continuation_mode=self._continuation_mode(literal_message, legacy),
                risk_tier=self._risk_tier(legacy.intent),
                state_fingerprint=snapshot.fingerprint,
                invocation_ledger=self._intent_ledger(attempted=True, called=model_called),
            )
        return ConversationIntentRoutingResult(
            classification=None,
            source="safe_fallback",
            model_attempted=True,
            model_called=model_called,
            model_succeeded=False,
            requires_clarification=True,
            reason_code=failure_reason,
            execution_disposition="clarify",
            model_performance_evidence=performance_evidence,
            state_fingerprint=snapshot.fingerprint,
            invocation_ledger=self._intent_ledger(attempted=True, called=model_called),
        )

    def _state_aware_model_context(
        self,
        message: str,
        snapshot: IntentRoutingSnapshotV1,
    ) -> dict[str, Any]:
        context = {
            "schemaVersion": self.STATE_AWARE_CONTEXT_SCHEMA_VERSION,
            "message": message[:2000],
            "stateFingerprint": snapshot.fingerprint,
            "state": json.loads(json.dumps(snapshot.model_projection, ensure_ascii=False, default=str)),
            "allowedIntents": list(ConversationIntentClassification.model_fields["intent"].annotation.__args__),
            "allowedRequestedScopes": list(
                ConversationIntentClassification.model_fields["requested_scope"].annotation.__args__
            ),
            "allowedTargetKinds": list(SemanticTargetReference.model_fields["kind"].annotation.__args__),
            "allowedContinuationModes": ["general", "guide_grounded"],
        }
        if self._encoded_size(context) > self.MODEL_CONTEXT_BYTE_LIMIT:
            state = context["state"]
            references = state.get("references") if isinstance(state.get("references"), dict) else {}
            references["segments"] = list(references.get("segments") or [])[:8]
            for segment in references["segments"]:
                if isinstance(segment, dict):
                    segment.pop("name", None)
            state["references"] = references
            state["compaction"] = ["segment_names_removed", "segments_capped_8"]
        if self._encoded_size(context) > self.MODEL_CONTEXT_BYTE_LIMIT:
            context["state"] = {
                "schemaVersion": "intent-routing-model-state-v1",
                "workflowPhase": snapshot.model_projection.get("workflowPhase", "unknown"),
                "lifecycle": snapshot.model_projection.get("lifecycle", "unknown"),
                "latestAssistant": snapshot.model_projection.get("latestAssistant", {}),
                "conflicts": list(snapshot.model_projection.get("conflicts") or [])[:4],
                "compaction": ["minimal_state_projection"],
            }
        if self._encoded_size(context) > self.MODEL_CONTEXT_BYTE_LIMIT:
            raise ValueError("intent_model_context_too_large")
        return context

    @staticmethod
    def _encoded_size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))

    @staticmethod
    def _compatibility_snapshot(state_summary: dict[str, Any]) -> IntentRoutingSnapshotV1:
        safe_state = {
            key: value
            for key, value in state_summary.items()
            if isinstance(value, (bool, int, str, list, dict))
            and not key.lower().endswith("id")
        }
        projection = {
            "schemaVersion": "intent-routing-model-state-v1",
            "workflowPhase": "unknown",
            "lifecycle": "active" if safe_state.get("hasActiveVersion") else "empty",
            "legacyState": safe_state,
            "latestAssistant": {},
            "references": {},
            "conflicts": [],
        }
        fingerprint = sha256(
            json.dumps(projection, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return IntentRoutingSnapshotV1(
            fingerprint=fingerprint,
            model_projection=projection,
            server_state=dict(state_summary),
        )

    @staticmethod
    def _parse_state_aware_hypothesis(raw: Any) -> ConversationIntentHypothesisV2:
        if isinstance(raw, dict) and raw.get("schemaVersion") == "conversation-intent-hypothesis-v2":
            return ConversationIntentHypothesisV2.model_validate(raw)
        # Compatibility for internal tests and rolling deploys where an older
        # Lite worker can briefly return the strict five-field v1 contract.
        legacy = ConversationIntentClassification.model_validate(raw)
        target_kind: TargetKind = {
            "new_itinerary": "none",
            "active_itinerary": "current_itinerary",
            "planning_root": "planning_root",
            "portfolio": "portfolio",
            "pending_slot": "pending_slot",
            "candidate_search": "pending_slot",
            "current_stage": "current_stage",
            "clarification": "clarification",
        }.get(legacy.requested_scope, "none")  # type: ignore[assignment]
        return ConversationIntentHypothesisV2(
            schemaVersion="conversation-intent-hypothesis-v2",
            primary=SemanticIntentHypothesis(
                intent=legacy.intent,
                confidence=legacy.confidence,
                requestedScope=legacy.requested_scope,
                isQuestion=legacy.is_question,
                isNegated=legacy.is_negated,
                targetReference={"kind": target_kind},
            ),
            alternatives=[],
            semanticSignals={},
        )

    @classmethod
    def _contains_forbidden_execution_authority(cls, value: Any) -> bool:
        forbidden_keys = {
            "choiceId",
            "turnId",
            "versionId",
            "segmentId",
            "portfolioId",
            "nonce",
            "tool",
            "toolName",
            "patch",
            "patchPayload",
            "capabilityAuthorized",
            "authorized",
            "writeAllowed",
        }
        if isinstance(value, dict):
            return any(
                str(key) in forbidden_keys or cls._contains_forbidden_execution_authority(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(cls._contains_forbidden_execution_authority(item) for item in value)
        return False

    def _safety_classification(self, message: str) -> Optional[ConversationIntentClassification]:
        if not message:
            return None
        if self._QUESTION_PREFIX.search(message) or self._NON_EXECUTION_CONTEXT.search(message):
            return self._classification("inspect_or_explain", "current_action", is_question=True)
        guide_requirement = self._GUIDE_GROUNDED_REQUIREMENT_PLAN.search(message)
        guide_topic = self._GUIDE_GROUNDED_TOPIC.search(message) or guide_requirement
        if guide_topic and self._GUIDE_GROUNDED_NEGATION.search(message):
            return self._classification("cancel_action", "current_action", is_negated=True)
        if guide_topic and self._GUIDE_GROUNDED_DISCUSSION.search(message):
            return self._classification("inspect_or_explain", "current_action", is_question=True)
        if self._CANCEL_PREFIX.search(message) and not self._CORRECTION_AFTER_NEGATION.search(message):
            return self._classification("cancel_action", "current_action", is_negated=True)
        if (
            guide_requirement
            or self._GUIDE_GROUNDED_EXPANSION.search(message)
            or self._GUIDE_GROUNDED_CONTEXTUAL_EXPANSION.search(message)
        ):
            return self._classification("continue_plan_expansion", "planning_root")
        return None

    @classmethod
    def _enforce_state_aware_non_execution(
        cls,
        hypothesis: ConversationIntentHypothesisV2,
    ) -> SemanticIntentHypothesis:
        primary = hypothesis.primary
        if primary.is_question or hypothesis.semantic_signals.quoted_command or hypothesis.semantic_signals.hypothetical:
            return SemanticIntentHypothesis(
                intent="inspect_or_explain",
                confidence=primary.confidence,
                requestedScope="current_action",
                isQuestion=True,
                isNegated=False,
                targetReference={"kind": "none"},
            )
        if primary.intent == "cancel_action" or (
            primary.is_negated
            and not hypothesis.semantic_signals.correction_after_negation
            and primary.intent
            in {
                "continue_plan_expansion",
                "adopt_plan",
                "continue_pending_slot",
                "manual_candidate_search",
                "search_travel_guide_advice",
                "retry_current_stage",
                "regenerate_from_scratch",
            }
        ):
            return SemanticIntentHypothesis(
                intent="cancel_action",
                confidence=primary.confidence,
                requestedScope="current_action",
                isQuestion=False,
                isNegated=True,
                targetReference={"kind": "none"},
            )
        return primary

    @staticmethod
    def _hypothesis_is_ambiguous(
        primary: SemanticIntentHypothesis,
        alternatives: list[SemanticIntentHypothesis],
    ) -> bool:
        if not alternatives:
            return False
        competing = alternatives[0]
        return competing.intent != primary.intent and primary.confidence - competing.confidence < 0.12

    @staticmethod
    def _risk_tier(intent: ConversationIntent) -> IntentRiskTier:
        if intent in {"inspect_or_explain", "cancel_action", "clarification_answer", "search_travel_guide_advice"}:
            return "read_only"
        if intent in {
            "continue_plan_expansion",
            "continue_pending_slot",
            "manual_candidate_search",
            "retry_current_stage",
        }:
            return "proposal_only"
        if intent == "adopt_plan":
            return "commit"
        return "mutation"

    @staticmethod
    def _intent_ledger(*, attempted: bool, called: bool) -> dict[str, int]:
        return {
            "intentModelAttemptCount": int(attempted),
            "intentProviderCallCount": int(called),
            "fullControllerCallCount": 0,
            "fallbackControllerLiteCallCount": 0,
            "plannerCallCount": 0,
        }

    def compile_execution_intent(
        self,
        route: ConversationIntentRoutingResult,
        *,
        target_status: TargetStatus,
        capability: ConversationCapabilityResolution,
        state_fingerprint: str,
    ) -> ConversationIntentRoutingResult:
        classification = route.classification
        risk_tier = route.risk_tier or (
            self._risk_tier(classification.intent) if classification is not None else "read_only"
        )
        reason_code = route.reason_code
        authorized = not route.requires_clarification
        if authorized and target_status in {"none", "ambiguous", "stale"}:
            authorized = False
            reason_code = {
                "none": "intent_target_unresolved",
                "ambiguous": "intent_target_ambiguous",
                "stale": "intent_target_stale",
            }[target_status]
        if authorized and capability.status in {"none", "ambiguous"}:
            authorized = False
            reason_code = (
                "intent_capability_ambiguous" if capability.status == "ambiguous" else "intent_capability_none"
            )
        read_or_no_write = route.execution_disposition in {"read_only", "no_write"}
        execution_intent = {
            "schemaVersion": "conversation-execution-intent-v1",
            "status": "authorized" if authorized else "clarification_required",
            "semanticIntent": classification.intent if classification is not None else None,
            "riskTier": risk_tier,
            "targetStatus": target_status,
            "capabilityStatus": capability.status,
            "stateFingerprint": state_fingerprint,
            "writeAllowed": bool(authorized and not read_or_no_write),
            "reasonCode": reason_code,
        }
        return replace(
            route,
            requires_clarification=not authorized,
            reason_code=reason_code,
            execution_disposition=("clarify" if not authorized else route.execution_disposition),
            risk_tier=risk_tier,
            state_fingerprint=state_fingerprint,
            execution_intent=execution_intent,
        )

    def _model_context(
        self,
        message: str,
        state_summary: dict[str, Any],
    ) -> dict[str, Any]:
        allowed_state = {
            key: value
            for key, value in state_summary.items()
            if key
            in {
                "hasActiveVersion",
                "hasPlanningRoot",
                "hasPortfolio",
                "hasPendingSlots",
                "hasUnansweredClarification",
                "pendingCapabilityCount",
                "expansionCapabilityCount",
                "adoptionCapabilityCount",
                "pendingSlotCapabilityCount",
                "manualSearchCapabilityCount",
                "guideSearchCapabilityCount",
            }
            and isinstance(value, (bool, int))
        }
        return {
            "schemaVersion": self.CONTEXT_SCHEMA_VERSION,
            "message": message[:2000],
            "state": allowed_state,
            "allowedIntents": list(ConversationIntentClassification.model_fields["intent"].annotation.__args__),
            "allowedRequestedScopes": list(
                ConversationIntentClassification.model_fields["requested_scope"].annotation.__args__
            ),
        }

    def _deterministic_classification(
        self,
        message: str,
    ) -> Optional[ConversationIntentClassification]:
        if not message:
            return None
        if self._QUESTION_PREFIX.search(message):
            return self._classification(
                "inspect_or_explain",
                "current_action",
                is_question=True,
            )
        if self._CANCEL_PREFIX.search(message):
            return self._classification(
                "cancel_action",
                "current_action",
                is_negated=True,
            )
        guide_grounded_requirement = self._GUIDE_GROUNDED_REQUIREMENT_PLAN.search(message)
        guide_grounded_topic = self._GUIDE_GROUNDED_TOPIC.search(message) or guide_grounded_requirement
        if guide_grounded_topic and self._GUIDE_GROUNDED_NEGATION.search(message):
            return self._classification(
                "cancel_action",
                "current_action",
                is_negated=True,
            )
        if guide_grounded_topic and self._GUIDE_GROUNDED_DISCUSSION.search(message):
            return self._classification(
                "inspect_or_explain",
                "current_action",
                is_question=True,
            )
        deterministic_patterns: tuple[
            tuple[re.Pattern[str], ConversationIntent, RequestedScope],
            ...,
        ] = (
            (
                self._GUIDE_GROUNDED_REQUIREMENT_PLAN,
                "continue_plan_expansion",
                "planning_root",
            ),
            (
                self._GUIDE_GROUNDED_EXPANSION,
                "continue_plan_expansion",
                "planning_root",
            ),
            (
                self._GUIDE_GROUNDED_CONTEXTUAL_EXPANSION,
                "continue_plan_expansion",
                "planning_root",
            ),
            (
                self._REFINE_AND_CONTINUE,
                "create_itinerary",
                "planning_root",
            ),
            (
                self._EXPLICIT_EXPANSION,
                "continue_plan_expansion",
                "planning_root",
            ),
            (
                self._EXPLICIT_REGENERATE,
                "regenerate_from_scratch",
                "full_task",
            ),
            (self._EXPLICIT_RETRY, "retry_current_stage", "current_stage"),
            (self._EXPLICIT_CREATE, "create_itinerary", "new_itinerary"),
            (self._EXPLICIT_MODIFY, "modify_itinerary", "active_itinerary"),
            (self._EXPLICIT_ADOPT, "adopt_plan", "portfolio"),
            (
                self._EXPLICIT_PENDING,
                "continue_pending_slot",
                "pending_slot",
            ),
            (
                self._EXPLICIT_MANUAL_SEARCH,
                "manual_candidate_search",
                "candidate_search",
            ),
            (
                self._EXPLICIT_GUIDE_SEARCH,
                "search_travel_guide_advice",
                "planning_root",
            ),
        )
        for pattern, intent, requested_scope in deterministic_patterns:
            if pattern.search(message):
                return self._classification(intent, requested_scope)
        return None

    @classmethod
    def _continuation_mode(
        cls,
        message: str,
        classification: ConversationIntentClassification,
    ) -> Optional[ContinuationMode]:
        if (
            classification.intent == "continue_plan_expansion"
            and classification.requested_scope == "planning_root"
            and not classification.is_question
            and not classification.is_negated
            and (
                cls._GUIDE_GROUNDED_REQUIREMENT_PLAN.search(message)
                or cls._GUIDE_GROUNDED_EXPANSION.search(message)
                or cls._GUIDE_GROUNDED_CONTEXTUAL_EXPANSION.search(message)
            )
        ):
            return "guide_grounded"
        return None

    @staticmethod
    def _classification(
        intent: ConversationIntent,
        requested_scope: RequestedScope,
        *,
        is_question: bool = False,
        is_negated: bool = False,
    ) -> ConversationIntentClassification:
        return ConversationIntentClassification(
            intent=intent,
            confidence=1.0,
            requestedScope=requested_scope,
            isQuestion=is_question,
            isNegated=is_negated,
        )

    @classmethod
    def _enforce_non_execution_signals(
        cls,
        classification: ConversationIntentClassification,
    ) -> ConversationIntentClassification:
        if classification.is_question:
            return cls._classification(
                "inspect_or_explain",
                "current_action",
                is_question=True,
            )
        if classification.intent == "cancel_action" or (
            classification.is_negated
            and classification.intent
            in {
                "continue_plan_expansion",
                "adopt_plan",
                "continue_pending_slot",
                "manual_candidate_search",
                "search_travel_guide_advice",
                "retry_current_stage",
                "regenerate_from_scratch",
            }
        ):
            return cls._classification(
                "cancel_action",
                "current_action",
                is_negated=True,
            )
        return classification

    @staticmethod
    def _accepted_result(
        classification: ConversationIntentClassification,
        *,
        source: Literal[
            "deterministic_fast_path",
            "lite_controller",
            "state_aware_lite",
            "legacy_fallback",
        ],
        model_attempted: bool = False,
        model_called: bool,
        model_succeeded: bool = False,
        reason_code: str,
        model_performance_evidence: tuple[dict[str, Any], ...] = (),
        continuation_mode: Optional[ContinuationMode] = None,
        target_reference: Optional[dict[str, Any]] = None,
        risk_tier: Optional[IntentRiskTier] = None,
        state_fingerprint: Optional[str] = None,
        shadow_evaluation: Optional[dict[str, Any]] = None,
        invocation_ledger: Optional[dict[str, int]] = None,
    ) -> ConversationIntentRoutingResult:
        disposition: ExecutionDisposition
        if classification.intent == "inspect_or_explain":
            disposition = "read_only"
        elif classification.intent == "cancel_action":
            disposition = "no_write"
        else:
            disposition = "execute"
        effective_target_reference = target_reference
        if continuation_mode == "guide_grounded" and effective_target_reference is None:
            effective_target_reference = {"kind": "latest_guide"}
        return ConversationIntentRoutingResult(
            classification=classification,
            source=source,
            model_attempted=model_attempted,
            model_called=model_called,
            model_succeeded=model_succeeded,
            requires_clarification=False,
            reason_code=reason_code,
            execution_disposition=disposition,
            model_performance_evidence=model_performance_evidence,
            continuation_mode=continuation_mode,
            target_reference=effective_target_reference,
            risk_tier=risk_tier,
            state_fingerprint=state_fingerprint,
            shadow_evaluation=shadow_evaluation,
            invocation_ledger=invocation_ledger,
        )

    @staticmethod
    def _clarification_result(
        *,
        model_attempted: bool,
        model_called: bool,
        reason_code: str,
        model_performance_evidence: tuple[dict[str, Any], ...] = (),
    ) -> ConversationIntentRoutingResult:
        return ConversationIntentRoutingResult(
            classification=None,
            source="safe_fallback",
            model_attempted=model_attempted,
            model_called=model_called,
            model_succeeded=False,
            requires_clarification=True,
            reason_code=reason_code,
            execution_disposition="clarify",
            model_performance_evidence=model_performance_evidence,
        )


class ConversationCapabilityResolver:
    """Bind a classified intent to one current server-issued operation.

    The resolver is read-only. It never claims a nonce, mutates a choice, or
    writes an itinerary. Labels, display names, and free-form values are never
    consulted.
    """

    _CHOICE_REQUIRED_INTENTS = {
        "continue_plan_expansion",
        "adopt_plan",
        "continue_pending_slot",
        "manual_candidate_search",
        "search_travel_guide_advice",
        "retry_current_stage",
    }
    _EXPANSION_KINDS = {
        "portfolio_partial_more_plans",
        "portfolio_more_plans",
    }
    _PENDING_ACTIONS = {
        "refresh_density_candidates",
        "search_density_nearby",
        "select_density_candidate",
        "continue_pending_slot",
    }

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def routing_snapshot(self, session: sqlite3.Row) -> IntentRoutingSnapshotV1:
        """Build the single authoritative read-only state view for this turn."""

        active_snapshot = self._active_snapshot(session)
        selection = self._selection_context(active_snapshot)
        pending_slots = [
            dict(item)
            for item in active_snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict)
        ]
        latest = self._latest_assistant_carrier(str(session["id"]))
        latest_response = latest.get("response") if isinstance(latest.get("response"), dict) else {}
        if not selection.get("rootPortfolioId"):
            lineages = []
            for option in latest_response.get("choiceOptions") or []:
                if not isinstance(option, dict):
                    continue
                lineage = self._option_lineage(option)
                persisted = self._portfolio_lineage(str(session["id"]), lineage.get("rootPortfolioId"))
                if persisted and all(lineage.get(key) == value for key, value in persisted.items()):
                    if persisted not in lineages:
                        lineages.append(persisted)
            if len(lineages) == 1:
                selection = lineages[0]
        capability_matches = {
            intent: self._matching_candidates(
                session,
                intent,
                snapshot=active_snapshot,
                selection=selection,
                latest_carrier=latest,
            )
            for intent in self._CHOICE_REQUIRED_INTENTS
        }
        capability_matches = {intent: [candidate for candidate in candidates
                                       if self._catalog_candidate_current(session, candidate)]
                              for intent, candidates in capability_matches.items()}
        declared_counts = {
            intent: sum(
                1
                for option in latest_response.get("choiceOptions") or []
                if isinstance(option, dict) and self._matches_intent(option, intent)
            )
            for intent in self._CHOICE_REQUIRED_INTENTS
        }
        proposal_refs = self._proposal_references(
            session_id=str(session["id"]),
            root_portfolio_id=self._optional_text(selection.get("rootPortfolioId")),
        )
        segment_refs = self._segment_references(active_snapshot)
        mode = str(latest_response.get("mode") or "unknown")[:80]
        terminal_status = str(latest_response.get("terminalStatus") or "unknown")[:80]
        guide_advice = latest_response.get("guideAdvice") if isinstance(latest_response.get("guideAdvice"), dict) else {}
        guide_requirement = None
        guide_unavailable_reason = "guide_evidence_missing"
        guide_matches = capability_matches.get("continue_plan_expansion") or []
        if len(guide_matches) == 1:
            from src.services.guide_continuation_requirement_service import (
                GuideContinuationRequirementService, GuideContinuationRequirementError,
            )
            try:
                guide_requirement = GuideContinuationRequirementService(self.db).build(
                    session_id=str(session["id"]), selected_choice=guide_matches[0],
                    active_version_id=str(session["active_version_id"] or "") or None,
                    require_place_hints=False,
                )
                from src.services.conversation_operation_identity import ConversationOperationIdentity
                identity_service = ConversationOperationIdentity(self.db)
                match = guide_matches[0]
                existing_guide_operation = identity_service.execution(str(session["id"]), match["sourceAssistantTurnId"],
                    match["choiceId"], guide_evidence=guide_requirement["evidenceFingerprint"])
                if existing_guide_operation is not None and not identity_service.explicitly_unstarted(existing_guide_operation):
                    guide_requirement = None
                    guide_unavailable_reason = "guide_operation_not_available"
            except (GuideContinuationRequirementError, ValueError) as error:
                guide_requirement = None
                guide_unavailable_reason = str(error)
        # A failed semantic selection is not a new missing travel field. Only
        # a persisted business checkpoint may supersede an existing root here.
        has_clarification = self._response_has_business_clarification(latest_response) and bool(
            not selection.get("planningSelectionRootTurnId")
            or isinstance(latest_response.get("clarificationCheckpoint"), dict)
        )
        if has_clarification:
            workflow_phase = "clarification"
        elif guide_requirement:
            workflow_phase = "guide_advice"
        elif pending_slots:
            workflow_phase = "pending_slot"
        elif proposal_refs or selection.get("rootPortfolioId"):
            workflow_phase = "proposal_selection"
        elif session["active_version_id"]:
            workflow_phase = "active_itinerary"
        else:
            workflow_phase = "new_session"
        artifacts: list[str] = []
        if guide_requirement:
            artifacts.append("guide_advice")
        if proposal_refs:
            artifacts.append("proposal_set")
        if pending_slots:
            artifacts.append("pending_slots")
        if has_clarification:
            artifacts.append("clarification")
        capability_projection = [
            {"semanticKind": intent, "count": len(capability_matches[intent])}
            for intent in sorted(self._CHOICE_REQUIRED_INTENTS)
            if capability_matches[intent] or declared_counts[intent]
        ]
        conflicts = [
            f"multiple_{intent}"
            for intent, matches in capability_matches.items()
            if len(matches) > 1
        ]
        conflicts.extend(
            f"stale_{intent}"
            for intent, count in declared_counts.items()
            if count and not capability_matches[intent]
        )
        model_projection = {
            "schemaVersion": "intent-routing-model-state-v1",
            "workflowPhase": workflow_phase,
            "lifecycle": "active" if session["active_version_id"] else "preview" if selection.get("planningSelectionRootTurnId") else "empty",
            "latestAssistant": {
                "mode": mode,
                "terminalStatus": terminal_status,
                "artifactKinds": artifacts,
                "capabilities": capability_projection,
                "hasPendingClarification": has_clarification,
            },
            "planningContext": {
                "hasFrozenTravelRequest": bool(selection.get("planningSelectionRootTurnId")),
                "visibleProposalCount": len(proposal_refs),
                "requestPolicy": "reuse_original_contract" if selection.get("planningSelectionRootTurnId") else "collect_travel_requirements",
            },
            "references": {
                "dayNumbers": sorted(
                    {int(item["dayNumber"]) for item in segment_refs if int(item.get("dayNumber") or 0) > 0}
                )[:14],
                "proposalOrdinals": [int(item["ordinal"]) for item in proposal_refs[:12]],
                "segments": [
                    {
                        "ordinal": index,
                        "dayNumber": int(item.get("dayNumber") or 0),
                        "timeBucket": item.get("timeBucket"),
                        "name": str(item.get("name") or "")[:80],
                    }
                    for index, item in enumerate(segment_refs[:24], start=1)
                ],
                "pendingSlotCount": len(pending_slots),
            },
            "conflicts": sorted(set(conflicts))[:12],
            "guideContinuation": {"available": bool(guide_requirement),
                                  "evidenceStatus": "validated" if guide_requirement else "unavailable",
                                  "placeHintCount": len((guide_requirement or {}).get("placeHints") or []),
                                  "sourceRelation": ("current_reply" if (guide_requirement or {}).get("sourceAssistantTurnId") == latest.get("turnId") else "validated_retained_guide") if guide_requirement else None,
                                  "reason": None if guide_requirement else guide_unavailable_reason},
        }
        # Server-local hash input only. It is NOT included in model_projection
        # or _classify_action's Provider payload (which explicitly projects state).
        internal_fingerprint_material = {
            "session": str(session["id"]),
            "activeVersion": str(session["active_version_id"] or ""),
            "latestTurn": str(latest.get("turnId") or ""),
            "selection": selection,
            "projection": model_projection,
            "capabilities": capability_matches,
            "choices": latest_response.get("choiceOptions") or [],
            "consumed": latest_response.get("consumedChoiceIds") or [],
            "executions": [dict(row) for row in self.db.execute(
                "SELECT id, source_turn_id, choice_id, status, attempt FROM agent_choice_executions WHERE session_id = ? ORDER BY id",
                (str(session["id"]),),
            ).fetchall()],
            "proposals": proposal_refs,
            "guideRequirement": guide_requirement,
        }
        fingerprint = sha256(
            json.dumps(
                internal_fingerprint_material,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return IntentRoutingSnapshotV1(
            fingerprint=fingerprint,
            model_projection=model_projection,
            server_state={
                "sessionId": str(session["id"]),
                "activeVersionId": self._optional_text(session["active_version_id"]),
                "hasActiveVersion": bool(session["active_version_id"]),
                "hasPlanningRoot": bool(selection.get("planningSelectionRootTurnId")),
                "hasPortfolio": bool(selection.get("rootPortfolioId")),
                "planningSelectionRootTurnId": self._optional_text(selection.get("planningSelectionRootTurnId")),
                "rootPortfolioId": self._optional_text(selection.get("rootPortfolioId")),
                "requestContractFingerprint": self._optional_text(selection.get("requestContractFingerprint")),
                "expectedBaseVersionId": self._optional_text(session["active_version_id"]),
                "latestAssistant": latest,
                "latestArtifactKind": "guide_advice" if "guide_advice" in artifacts else artifacts[0] if artifacts else None,
                "pendingClarification": has_clarification,
                "pendingSlots": pending_slots,
                "proposalReferences": proposal_refs,
                "segmentReferences": segment_refs,
                "capabilityMatches": capability_matches,
                "declaredCapabilityCounts": declared_counts,
                "conflicts": sorted(set(conflicts)),
                "activeSnapshot": active_snapshot,
                "guideRequirement": guide_requirement,
                "guideUnavailableReason": guide_unavailable_reason,
            },
        )

    def _catalog_candidate_current(self, session: sqlite3.Row, candidate: dict[str, Any]) -> bool:
        """Pure eligibility read; never call portfolio hydration/expiry writers."""
        option = candidate.get("option") or {}
        if option.get("action") not in {"continue_plan_expansion", "search_travel_guide_advice", "select_plan_proposal"}:
            # Compatibility resolvers may still expose old retry shapes. The
            # native catalog must match the stricter execution claim boundary:
            # a retryable label alone does not prove the business never began.
            from src.services.conversation_operation_identity import ConversationOperationIdentity
            execution = self.db.execute(
                "SELECT * FROM agent_choice_executions WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?",
                (session["id"], candidate.get("sourceAssistantTurnId"), candidate.get("choiceId")),
            ).fetchone()
            return execution is None or ConversationOperationIdentity.explicitly_unstarted(dict(execution))
        from src.services.plan_portfolio_store import PlanPortfolioStore
        row = self.db.execute("SELECT * FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
                              (option.get("rootPortfolioId"), session["id"])).fetchone()
        if (row is None or row["status"] != "awaiting_selection" or PlanPortfolioStore._is_expired(row["expires_at"])
                or str(row["source_assistant_turn_id"] or "") != str(candidate.get("sourceAssistantTurnId") or "")
                or str(row["expected_base_version_id"] or "") != str(session["active_version_id"] or "")):
            return False
        root = self.db.execute("SELECT 1 FROM conversation_turns WHERE id = ? AND session_id = ? AND role = 'user' AND status != 'superseded'",
                               (row["source_user_turn_id"], session["id"])).fetchone()
        if root is None:
            return False
        if option.get("action") == "select_plan_proposal":
            proposal = self.db.execute("SELECT status, choice_id FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
                                       (option.get("proposalId"), row["id"])).fetchone()
            return bool(proposal and proposal["status"] == "adoption_ready" and proposal["choice_id"] == option.get("id"))
        return True

    def state_summary(self, session: sqlite3.Row) -> dict[str, Any]:
        return self.state_summary_from_snapshot(self.routing_snapshot(session))

    @classmethod
    def state_summary_from_snapshot(cls, routing_snapshot: IntentRoutingSnapshotV1) -> dict[str, Any]:
        state = routing_snapshot.server_state
        pending_slots = state.get("pendingSlots") or []
        matches = state.get("capabilityMatches") or {}
        counts = {intent: len(matches.get(intent) or []) for intent in cls._CHOICE_REQUIRED_INTENTS}
        return {
            "hasActiveVersion": bool(state.get("hasActiveVersion")),
            "hasPlanningRoot": bool(state.get("hasPlanningRoot")),
            "hasPortfolio": bool(state.get("hasPortfolio")),
            "hasPendingSlots": bool(pending_slots),
            "hasUnansweredClarification": bool(state.get("pendingClarification")),
            "pendingCapabilityCount": sum(counts.values()),
            "expansionCapabilityCount": counts["continue_plan_expansion"],
            "adoptionCapabilityCount": counts["adopt_plan"],
            "pendingSlotCapabilityCount": counts["continue_pending_slot"],
            "manualSearchCapabilityCount": counts["manual_candidate_search"],
            "guideSearchCapabilityCount": counts["search_travel_guide_advice"],
        }

    def resolve(
        self,
        *,
        session: sqlite3.Row,
        classification: ConversationIntentClassification,
        routing_snapshot: Optional[IntentRoutingSnapshotV1] = None,
        target_resolution: Optional[ConversationTargetResolution] = None,
        continuation_mode: Optional[ContinuationMode] = None,
    ) -> ConversationCapabilityResolution:
        intent = classification.intent
        if intent not in self._CHOICE_REQUIRED_INTENTS:
            return self._resolve_state_capability(session, classification)

        matches = (
            list((routing_snapshot.server_state.get("capabilityMatches") or {}).get(intent) or [])
            if routing_snapshot is not None
            else self._matching_candidates(session, intent)
        )
        if continuation_mode == "guide_grounded":
            matches = [
                match
                for match in matches
                if isinstance(match.get("option"), dict)
                and str(match["option"].get("action") or "") == "continue_plan_expansion"
                and str(match["option"].get("kind") or "") == "simple_direction_more_plans"
            ]
        target_choice_id = self._optional_text((target_resolution.target or {}).get("choiceId")) if target_resolution else None
        if target_choice_id:
            matches = [match for match in matches if str(match.get("choiceId") or "") == target_choice_id]
        if not matches:
            if intent == "retry_current_stage" and not self._declares_intent_candidate(session, intent):
                return self._resolve_state_capability(session, classification)
            return ConversationCapabilityResolution(
                status="none",
                capability=intent,
                selected_choice_request=None,
                reason_code=(
                    "stale_state_or_lineage"
                    if routing_snapshot is not None
                    and int((routing_snapshot.server_state.get("declaredCapabilityCounts") or {}).get(intent) or 0) > 0
                    else "no_matching_capability"
                ),
                target_resolution=(target_resolution.to_context() if target_resolution else None),
            )
        if len(matches) != 1:
            return ConversationCapabilityResolution(
                status="ambiguous",
                capability=intent,
                selected_choice_request=None,
                reason_code="multiple_matching_capabilities",
                target_resolution=(target_resolution.to_context() if target_resolution else None),
            )
        match = matches[0]
        option = match["option"]
        bound_state = match["boundState"]
        return ConversationCapabilityResolution(
            status="unique",
            capability=intent,
            selected_choice_request={
                "sourceAssistantTurnId": match["sourceAssistantTurnId"],
                "choiceId": match["choiceId"],
            },
            reason_code="unique_server_capability",
            planning_root_id=self._optional_text(bound_state.get("planningSelectionRootTurnId")),
            root_portfolio_id=self._optional_text(bound_state.get("rootPortfolioId")),
            request_contract_fingerprint=self._optional_text(bound_state.get("requestContractFingerprint")),
            expected_base_version_id=self._optional_text(bound_state.get("expectedBaseVersionId")),
            focus_brief_id=self._optional_text(option.get("focusBriefId")),
            target_resolution=(target_resolution.to_context() if target_resolution else None),
        )

    def _resolve_state_capability(
        self,
        session: sqlite3.Row,
        classification: ConversationIntentClassification,
    ) -> ConversationCapabilityResolution:
        intent = classification.intent
        has_active_version = bool(session["active_version_id"])
        unavailable = (intent == "create_itinerary" and has_active_version) or (
            intent == "modify_itinerary" and not has_active_version
        )
        if unavailable:
            return ConversationCapabilityResolution(
                status="none",
                capability=intent,
                selected_choice_request=None,
                reason_code="required_active_state_unavailable",
            )
        return ConversationCapabilityResolution(
            status="not_required",
            capability=intent,
            selected_choice_request=None,
            reason_code="no_persisted_choice_required",
            expected_base_version_id=self._optional_text(session["active_version_id"]),
        )

    def resolve_target(
        self,
        *,
        snapshot: IntentRoutingSnapshotV1,
        classification: ConversationIntentClassification,
        target_reference: Optional[dict[str, Any]],
    ) -> ConversationTargetResolution:
        if target_reference is None:
            return ConversationTargetResolution(
                status="not_required",
                reason_code="legacy_target_resolution_deferred_to_capability_gate",
            )
        try:
            reference = SemanticTargetReference.model_validate(target_reference)
        except ValidationError:
            return ConversationTargetResolution(status="none", reason_code="target_contract_invalid")
        state = snapshot.server_state
        kind = reference.kind
        if kind == "none":
            if classification.intent in {
                "create_itinerary",
                "regenerate_from_scratch",
                "inspect_or_explain",
                "cancel_action",
            }:
                return ConversationTargetResolution(status="not_required", reason_code="semantic_target_not_required")
            return ConversationTargetResolution(status="none", reason_code="semantic_target_missing")
        if kind == "current_itinerary":
            if not state.get("hasActiveVersion"):
                return ConversationTargetResolution(status="none", reason_code="active_itinerary_target_missing")
            return ConversationTargetResolution(
                status="unique",
                reason_code="active_itinerary_target_unique",
                target={"kind": kind, "activeVersionId": state.get("activeVersionId")},
            )
        if kind == "planning_root":
            root_id = self._optional_text(state.get("planningSelectionRootTurnId"))
            if not root_id:
                return ConversationTargetResolution(status="none", reason_code="planning_root_target_missing")
            return ConversationTargetResolution(
                status="unique",
                reason_code="planning_root_target_unique",
                target={"kind": kind, "planningSelectionRootTurnId": root_id},
            )
        if kind == "portfolio":
            portfolio_id = self._optional_text(state.get("rootPortfolioId"))
            if not portfolio_id:
                return ConversationTargetResolution(status="none", reason_code="portfolio_target_missing")
            return ConversationTargetResolution(
                status="unique",
                reason_code="portfolio_target_unique",
                target={"kind": kind, "rootPortfolioId": portfolio_id},
            )
        if kind == "latest_guide":
            if state.get("guideRequirement"):
                return ConversationTargetResolution(status="unique", reason_code="guide_evidence_target_unique",
                    target={"kind": kind, "sourceAssistantTurnId": state["guideRequirement"].get("sourceAssistantTurnId"),
                            "capabilitySourceAssistantTurnId": state["latestAssistant"].get("turnId")})
            latest = state.get("latestAssistant") if isinstance(state.get("latestAssistant"), dict) else {}
            response = latest.get("response") if isinstance(latest.get("response"), dict) else {}
            guide = response.get("guideAdvice") if isinstance(response.get("guideAdvice"), dict) else {}
            recovery = (
                response.get("guideContinuationRecovery")
                if isinstance(response.get("guideContinuationRecovery"), dict)
                else {}
            )
            direct_guide = bool(
                response.get("mode") == "travel_guide_advice"
                and guide.get("status") == "completed"
                and guide.get("evidenceFingerprint")
            )
            recovered_guide = bool(
                response.get("mode") == "clarification"
                and recovery.get("schemaVersion") == "guide-continuation-recovery-v1"
                and recovery.get("status") == "reissued"
                and recovery.get("guideEvidenceSourceAssistantTurnId")
                and recovery.get("guideEvidenceFingerprint")
                and all(
                    recovery.get(field) in (None, "", 0, "0")
                    for field in ("proposalDelta", "versionDelta", "patchDelta", "routeWriteDelta")
                )
            )
            if not direct_guide and not recovered_guide:
                return ConversationTargetResolution(status="stale", reason_code="latest_guide_target_stale")
            return ConversationTargetResolution(
                status="unique",
                reason_code="latest_guide_target_unique",
                target={
                    "kind": kind,
                    "sourceAssistantTurnId": latest.get("turnId"),
                    "guideEvidenceSourceAssistantTurnId": (
                        recovery.get("guideEvidenceSourceAssistantTurnId") if recovered_guide else latest.get("turnId")
                    ),
                    "evidenceFingerprint": (
                        recovery.get("guideEvidenceFingerprint")
                        if recovered_guide
                        else guide.get("evidenceFingerprint")
                    ),
                },
            )
        if kind == "proposal_ordinal":
            if reference.ordinal is None:
                return ConversationTargetResolution(status="none", reason_code="proposal_ordinal_missing")
            matches = [
                item
                for item in state.get("proposalReferences") or []
                if int(item.get("ordinal") or 0) == reference.ordinal
            ]
            return self._target_from_matches(
                matches,
                missing_reason="proposal_ordinal_target_missing",
                ambiguous_reason="proposal_ordinal_target_ambiguous",
            )
        if kind == "pending_slot":
            matches = [dict(item) for item in state.get("pendingSlots") or [] if isinstance(item, dict)]
            if reference.day_number is not None:
                matches = [item for item in matches if int(item.get("dayNumber") or 0) == reference.day_number]
            return self._target_from_matches(
                matches,
                missing_reason="pending_slot_target_missing",
                ambiguous_reason="pending_slot_target_ambiguous",
            )
        if kind == "day":
            if reference.day_number is None:
                return ConversationTargetResolution(status="none", reason_code="day_target_missing")
            day_exists = any(
                int(item.get("dayNumber") or 0) == reference.day_number
                for item in state.get("segmentReferences") or []
                if isinstance(item, dict)
            )
            return ConversationTargetResolution(
                status="unique" if day_exists else "none",
                reason_code="day_target_unique" if day_exists else "day_target_missing",
                target={"kind": kind, "dayNumber": reference.day_number} if day_exists else None,
            )
        if kind == "time_window":
            matches = [
                dict(item)
                for item in state.get("segmentReferences") or []
                if isinstance(item, dict)
                and (reference.day_number is None or int(item.get("dayNumber") or 0) == reference.day_number)
                and (reference.time_bucket is None or item.get("timeBucket") == reference.time_bucket)
                and (
                    not reference.mention_text
                    or reference.mention_text.casefold() in str(item.get("name") or "").casefold()
                )
            ]
            return self._target_from_matches(
                matches,
                missing_reason="time_window_target_missing",
                ambiguous_reason="time_window_target_ambiguous",
            )
        if kind == "current_stage":
            latest = state.get("latestAssistant") if isinstance(state.get("latestAssistant"), dict) else {}
            if not latest.get("turnId"):
                return ConversationTargetResolution(status="none", reason_code="current_stage_target_missing")
            return ConversationTargetResolution(
                status="unique",
                reason_code="current_stage_target_unique",
                target={"kind": kind, "sourceAssistantTurnId": latest.get("turnId")},
            )
        if kind == "clarification":
            if not state.get("pendingClarification"):
                return ConversationTargetResolution(status="none", reason_code="clarification_target_missing")
            return ConversationTargetResolution(
                status="unique",
                reason_code="clarification_target_unique",
                target={"kind": kind, "sourceAssistantTurnId": (state.get("latestAssistant") or {}).get("turnId")},
            )
        return ConversationTargetResolution(status="none", reason_code="semantic_target_unresolved")

    @staticmethod
    def _target_from_matches(
        matches: list[dict[str, Any]],
        *,
        missing_reason: str,
        ambiguous_reason: str,
    ) -> ConversationTargetResolution:
        if not matches:
            return ConversationTargetResolution(status="none", reason_code=missing_reason)
        if len(matches) != 1:
            return ConversationTargetResolution(status="ambiguous", reason_code=ambiguous_reason)
        return ConversationTargetResolution(
            status="unique",
            reason_code=f"{matches[0].get('kind') or 'semantic'}_target_unique",
            target=dict(matches[0]),
        )

    def _matching_candidates(
        self,
        session: sqlite3.Row,
        intent: ConversationIntent,
        *,
        snapshot: Optional[dict[str, Any]] = None,
        selection: Optional[dict[str, Any]] = None,
        latest_carrier: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        session_id = str(session["id"])
        active_version_id = str(session["active_version_id"] or "")
        active_snapshot = snapshot if snapshot is not None else self._active_snapshot(session)
        active_selection = selection if selection is not None else self._selection_context(active_snapshot)
        carrier = latest_carrier if latest_carrier is not None else self._latest_assistant_carrier(session_id)
        if carrier:
            row_matches: list[dict[str, Any]] = []
            response = carrier.get("response") if isinstance(carrier.get("response"), dict) else {}
            consumed_ids = {str(item) for item in response.get("consumedChoiceIds") or []}
            for option in response.get("choiceOptions") or []:
                if not isinstance(option, dict):
                    continue
                choice_id = str(option.get("id") or "").strip()
                if (
                    not choice_id
                    or choice_id in consumed_ids
                    or not self._matches_intent(option, intent)
                    or not self._execution_available(
                        session_id,
                        str(carrier.get("turnId") or ""),
                        choice_id,
                    )
                ):
                    continue
                bound_state = self._bound_active_state(
                    option,
                    intent=intent,
                    session_id=session_id,
                    active_version_id=active_version_id,
                    snapshot=active_snapshot,
                    selection=active_selection,
                )
                if bound_state is None:
                    continue
                row_matches.append(
                    {
                        "sourceAssistantTurnId": str(carrier.get("turnId") or ""),
                        "choiceId": choice_id,
                        "option": option,
                        "boundState": bound_state,
                    }
                )
            # Only the newest active assistant turn is authoritative for
            # free-text capability binding. Older choices remain clickable by
            # their explicit opaque identity, but are never guessed here.
            return row_matches
        return []

    def _declares_intent_candidate(
        self,
        session: sqlite3.Row,
        intent: ConversationIntent,
    ) -> bool:
        """Distinguish ordinary retry from a server-issued scoped retry."""

        row = self.db.execute(
            """SELECT agent_response_json
            FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status = 'active'
            ORDER BY turn_index DESC, created_at DESC LIMIT 1""",
            (str(session["id"]),),
        ).fetchone()
        if row is None:
            return False
        response = self._json_object(row["agent_response_json"])
        return any(
            isinstance(option, dict) and self._matches_intent(option, intent)
            for option in response.get("choiceOptions") or []
        )

    def _execution_available(
        self,
        session_id: str,
        source_turn_id: str,
        choice_id: str,
    ) -> bool:
        from src.services.conversation_operation_identity import ConversationOperationIdentity
        identities = ConversationOperationIdentity(self.db)
        try:
            option = identities.canonical_option(session_id, source_turn_id, choice_id)
            if option.get("action") in identities.ACTIONS:
                execution = identities.offered_execution(session_id, source_turn_id, choice_id)
                return execution is None or identities.explicitly_unstarted(execution)
        except ValueError:
            return False
        row = self.db.execute(
            """SELECT status FROM agent_choice_executions
            WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?""",
            (session_id, source_turn_id, choice_id),
        ).fetchone()
        return row is None or str(row["status"] or "") == "failed_retryable"

    def _matches_intent(
        self,
        option: dict[str, Any],
        intent: ConversationIntent,
    ) -> bool:
        action = str(option.get("action") or "")
        kind = str(option.get("kind") or "")
        if intent == "continue_plan_expansion":
            if action == "continue_plan_expansion" and kind == "simple_direction_more_plans":
                return all(
                    self._optional_text(option.get(key))
                    for key in (
                        "planningSelectionRootTurnId",
                        "rootPortfolioId",
                        "requestContractFingerprint",
                    )
                )
            return (
                action == "retry_model_planning"
                and kind in self._EXPANSION_KINDS
                and all(
                    self._optional_text(option.get(key))
                    for key in (
                        "planningSelectionRootTurnId",
                        "rootPortfolioId",
                        "requestContractFingerprint",
                        "focusBriefId",
                    )
                )
            )
        if intent == "retry_current_stage":
            return (
                action == "retry_model_planning"
                and kind in self._EXPANSION_KINDS
                and option.get("retryCurrentStageEligible") is True
                and all(
                    self._optional_text(option.get(key))
                    for key in (
                        "planningSelectionRootTurnId",
                        "rootPortfolioId",
                        "requestContractFingerprint",
                        "focusBriefId",
                    )
                )
            )
        if intent == "adopt_plan":
            return action in {"select_plan_proposal", "adopt_active_partial"}
        if intent == "continue_pending_slot":
            return action in self._PENDING_ACTIONS and self._has_slot_identity(option)
        if intent == "manual_candidate_search":
            return action == "manual_continuation" and kind == "custom_input" and self._has_slot_identity(option)
        if intent == "search_travel_guide_advice":
            return action == "search_travel_guide_advice" and kind == "travel_guide_advice"
        return False

    def _bound_active_state(
        self,
        option: dict[str, Any],
        *,
        intent: ConversationIntent,
        session_id: str,
        active_version_id: str,
        snapshot: dict[str, Any],
        selection: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if "expectedBaseVersionId" not in option or str(option.get("expectedBaseVersionId") or "") != active_version_id:
            return None

        option_lineage = self._option_lineage(option)
        active_lineage = {
            key: self._optional_text(selection.get(key))
            for key in (
                "planningSelectionRootTurnId",
                "rootPortfolioId",
                "requestContractFingerprint",
            )
        }
        if not all(active_lineage.values()):
            portfolio_lineage = self._portfolio_lineage(
                session_id,
                option_lineage.get("rootPortfolioId"),
            )
            if portfolio_lineage is not None:
                active_lineage = portfolio_lineage
        if not all(active_lineage.values()):
            return None

        if intent in {
            "continue_plan_expansion",
            "adopt_plan",
            "retry_current_stage",
            "search_travel_guide_advice",
        } and not all(
            option_lineage.get(key)
            for key in (
                "planningSelectionRootTurnId",
                "rootPortfolioId",
            )
        ):
            return None
        for key in (
            "planningSelectionRootTurnId",
            "rootPortfolioId",
            "requestContractFingerprint",
        ):
            option_value = option_lineage.get(key)
            if option_value and option_value != active_lineage[key]:
                return None
        if intent in {"continue_pending_slot", "manual_candidate_search"} and not self._slot_is_active(
            option,
            snapshot,
        ):
            return None
        return {
            **active_lineage,
            "expectedBaseVersionId": active_version_id or None,
        }

    def _portfolio_lineage(
        self,
        session_id: str,
        root_portfolio_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not root_portfolio_id:
            return None
        row = self.db.execute(
            """SELECT id, source_user_turn_id, request_contract_fingerprint
            FROM agent_plan_portfolios
            WHERE id = ? AND session_id = ?""",
            (root_portfolio_id, session_id),
        ).fetchone()
        if row is None:
            return None
        lineage = {
            "planningSelectionRootTurnId": self._optional_text(row["source_user_turn_id"]),
            "rootPortfolioId": self._optional_text(row["id"]),
            "requestContractFingerprint": self._optional_text(row["request_contract_fingerprint"]),
        }
        return lineage if all(lineage.values()) else None

    @classmethod
    def _option_lineage(cls, option: dict[str, Any]) -> dict[str, Optional[str]]:
        projection = option.get("comparisonProjection") if isinstance(option.get("comparisonProjection"), dict) else {}
        return {
            key: cls._optional_text(option.get(key) or projection.get(key))
            for key in (
                "planningSelectionRootTurnId",
                "rootPortfolioId",
                "requestContractFingerprint",
            )
        }

    @classmethod
    def _slot_is_active(
        cls,
        option: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> bool:
        option_key = cls._slot_key(option)
        if option_key is None:
            return False
        return any(
            cls._slot_key(slot) == option_key
            for slot in snapshot.get("portfolioPendingSlots") or []
            if isinstance(slot, dict)
        )

    @classmethod
    def _slot_key(
        cls,
        value: dict[str, Any],
    ) -> Optional[tuple[str, str, str, int]]:
        brief_id = cls._optional_text(value.get("briefId") or value.get("creativeBriefId"))
        pool_id = cls._optional_text(value.get("poolId"))
        slot_id = cls._optional_text(value.get("planningSlotId") or value.get("slotId"))
        try:
            day_number = int(value.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return None
        if not all((brief_id, pool_id, slot_id)) or day_number <= 0:
            return None
        return brief_id, pool_id, slot_id, day_number

    @staticmethod
    def _has_slot_identity(option: dict[str, Any]) -> bool:
        return all(
            ConversationCapabilityResolver._optional_text(option.get(key))
            for key in ("briefId", "poolId", "planningSlotId", "dayNumber")
        )

    def _latest_assistant_carrier(self, session_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT id, turn_index, agent_response_json
            FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status != 'superseded'
            ORDER BY turn_index DESC, created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return {}
        return {
            "turnId": str(row["id"]),
            "turnIndex": int(row["turn_index"] or 0),
            "response": self._json_object(row["agent_response_json"]),
        }

    def _proposal_references(
        self,
        *,
        session_id: str,
        root_portfolio_id: Optional[str],
    ) -> list[dict[str, Any]]:
        if not root_portfolio_id:
            return []
        rows = self.db.execute(
            """SELECT proposal.choice_id, proposal.rank_index, proposal.status
            FROM agent_plan_proposals AS proposal
            JOIN agent_plan_portfolios AS portfolio ON portfolio.id = proposal.portfolio_id
            WHERE proposal.portfolio_id = ? AND portfolio.session_id = ?
            ORDER BY proposal.rank_index ASC, proposal.created_at ASC""",
            (root_portfolio_id, session_id),
        ).fetchall()
        return [
            {
                "kind": "proposal_ordinal",
                "ordinal": int(row["rank_index"] or 0) + 1,
                "choiceId": str(row["choice_id"] or ""),
                "status": str(row["status"] or ""),
            }
            for row in rows
            if str(row["choice_id"] or "")
        ]

    @classmethod
    def _segment_references(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        for day_index, day in enumerate(snapshot.get("days") or [], start=1):
            if not isinstance(day, dict):
                continue
            try:
                day_number = int(day.get("dayNumber") or day_index)
            except (TypeError, ValueError):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                start_time = str(segment.get("startTime") or segment.get("arrivalTime") or "")
                try:
                    hour = int(start_time[:2])
                except (TypeError, ValueError):
                    hour = -1
                bucket = "morning" if 0 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                name = str(
                    segment.get("poiName")
                    or segment.get("title")
                    or segment.get("name")
                    or poi.get("name")
                    or ""
                ).strip()
                segment_id = cls._optional_text(segment.get("id") or segment.get("segmentId"))
                if not segment_id:
                    continue
                references.append(
                    {
                        "kind": "time_window",
                        "segmentId": segment_id,
                        "dayNumber": day_number,
                        "timeBucket": bucket,
                        "name": name,
                    }
                )
        return references

    @staticmethod
    def _response_has_business_clarification(response: dict[str, Any]) -> bool:
        # `mode=clarification` is also the envelope for entry/provider failures.
        # It is not evidence of an unanswered business question, even before a
        # planning root exists. Only an actual persisted question/checkpoint
        # makes answer_clarification available in the state-aware catalog.
        return response.get("terminalStatus") == "needs_confirmation" and any(
            isinstance(response.get(key), dict) and bool(response[key])
            for key in ("clarificationCheckpoint", "clarification")
        )

    @staticmethod
    def _response_has_pending_clarification(response: dict[str, Any]) -> bool:
        if response.get("terminalStatus") != "needs_confirmation":
            return False
        return bool(
            response.get("mode") == "clarification"
            or isinstance(response.get("clarificationCheckpoint"), dict)
            or isinstance(response.get("clarification"), dict)
        )

    def _active_snapshot(self, session: sqlite3.Row) -> dict[str, Any]:
        if not session["active_version_id"]:
            return {}
        try:
            snapshot = ItinerarySnapshotService(self.db).capture_active_version_snapshot(
                session["active_plan_id"],
                session["active_version_id"],
            )
        except (KeyError, TypeError, ValueError):
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    @staticmethod
    def _selection_context(snapshot: dict[str, Any]) -> dict[str, Any]:
        selection = snapshot.get("portfolioSelectionContext")
        return selection if isinstance(selection, dict) else {}

    def _has_unanswered_clarification(self, session_id: str) -> bool:
        row = self.db.execute(
            """SELECT agent_response_json FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status = 'active'
            ORDER BY turn_index DESC, created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        response = self._json_object(row["agent_response_json"])
        return self._response_has_pending_clarification(response)

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _optional_text(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text or None

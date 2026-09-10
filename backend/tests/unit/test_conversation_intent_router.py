from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_service import AgentService
from src.services.agent_context_builder_service import AgentContextBuilder
from src.services.conversation_intent_router import (
    ConversationCapabilityResolver,
    ConversationIntentModelOutcome,
    ConversationIntentRouter,
)
from src.services.conversation_service import ConversationService
from src.services.deepseek_agent_provider import DeepSeekAgentProvider
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.retry_recovery_service import RetrySemanticClassifier


class RecordingLiteClassifier:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, context: dict[str, Any]) -> Any:
        self.calls.append(context)
        return self.response


class ProviderMustNotRun:
    def decide_autonomy_lite(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("intent or controller model must not run")

    def decide_autonomy(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("full controller must not run")


class IntentOnlyProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.model = "deepseek-chat"

    def decide_autonomy_lite(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> str:
        assert timeout_seconds > 0
        self.calls.append(context)
        return json.dumps(
            {
                "intent": "continue_plan_expansion",
                "confidence": 0.96,
                "requestedScope": "planning_root",
                "isQuestion": False,
                "isNegated": False,
            },
            ensure_ascii=False,
        )

    def decide_autonomy(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("business controller must not run while routing")


class StateAwareIntentOnlyProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.model = "deepseek-chat"

    def decide_autonomy_lite(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> str:
        assert timeout_seconds > 0
        assert context["schemaVersion"] == "conversation-intent-context-v2"
        self.calls.append(context)
        return json.dumps(
            {
                "schemaVersion": "conversation-intent-hypothesis-v2",
                "primary": {
                    "intent": "continue_plan_expansion",
                    "confidence": 0.96,
                    "requestedScope": "planning_root",
                    "isQuestion": False,
                    "isNegated": False,
                    "continuationMode": "guide_grounded",
                    "targetReference": {"kind": "latest_guide"},
                },
                "alternatives": [],
                "semanticSignals": {
                    "quotedCommand": False,
                    "hypothetical": False,
                    "correctionAfterNegation": False,
                },
            },
            ensure_ascii=False,
        )

    def decide_autonomy(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("business controller must not run while routing")


class FailingStateAwareIntentProvider:
    model = "deepseek-chat"

    def decide_autonomy_lite(self, *_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("intent provider unavailable")

    def decide_autonomy(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("full controller must not run after intent provider failure")


@pytest.fixture
def db_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def test_router_uses_deterministic_fast_path_for_explicit_plan_expansion() -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))
    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "继续新增一个方案",
        state_summary={"hasActiveVersion": True, "pendingCapabilityCount": 1},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.classification.requested_scope == "planning_root"
    assert routed.source == "deterministic_fast_path"
    assert routed.model_called is False
    assert routed.continuation_mode is None
    assert "continuationMode" not in routed.to_context()
    assert lite.calls == []


def test_router_classifies_guide_grounded_plan_expansion_from_original_phrase() -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))

    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "参考攻略建议的地点，生成新的方案",
        state_summary={"hasPlanningRoot": True, "expansionCapabilityCount": 1},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.classification.requested_scope == "planning_root"
    assert routed.continuation_mode == "guide_grounded"
    assert routed.to_context()["continuationMode"] == "guide_grounded"
    assert routed.source == "deterministic_fast_path"
    assert routed.model_called is False
    assert lite.calls == []


@pytest.mark.parametrize(
    "routing_mode",
    ["legacy-only", "shadow", "active-read", "active-all", "kill-switch"],
)
def test_router_classifies_guide_grounded_plan_from_requirements_phrase(routing_mode: str) -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))

    routed = ConversationIntentRouter(lite_classifier=lite, routing_mode=routing_mode).classify(
        "参考攻略里面的景点，按照我的需求给出个方案",
        state_summary={"hasPlanningRoot": True, "expansionCapabilityCount": 1},
    )

    if routing_mode == "active-all":
        # Boolean legacy summaries are not an authoritative guide snapshot;
        # a non-tool Provider fixture cannot authorize the new active entry.
        assert routed.requires_clarification is True
        assert routed.execution_disposition == "clarify"
        assert len(lite.calls) == 1
        return

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.classification.requested_scope == "planning_root"
    assert routed.continuation_mode == "guide_grounded"
    assert routed.execution_disposition == "execute"
    assert routed.source == "deterministic_fast_path"
    assert routed.model_called is False
    assert lite.calls == []


@pytest.mark.parametrize(
    "message",
    [
        "根据这份攻略推荐的景点，再生成一套不同方案",
        "结合上面的旅行攻略里提到的地点，继续规划另一套方案",
        "请参考攻略建议的餐厅，设计一份新的旅行方案",
        "参考攻略里的地点来生成一个新的方案",
        "参考攻略里的景点，按我的偏好给我个方案",
        "用攻略里提到的地点，结合我的偏好给个旅行方案",
        "按照刚才攻略再做一个不同方案",
    ],
)
def test_router_accepts_only_explicit_guide_grounded_expansion_commands(
    message: str,
) -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))

    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        message,
        state_summary={"hasPlanningRoot": True, "expansionCapabilityCount": 1},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.classification.requested_scope == "planning_root"
    assert routed.continuation_mode == "guide_grounded"
    assert routed.execution_disposition == "execute"
    assert lite.calls == []


@pytest.mark.parametrize(
    ("message", "expected_intent", "expected_disposition"),
    [
        (
            "不要参考攻略建议的地点生成新的方案",
            "cancel_action",
            "no_write",
        ),
        (
            "参考攻略建议的地点，但不要生成新的方案",
            "cancel_action",
            "no_write",
        ),
        (
            "参考攻略里面的景点，但不要给出方案",
            "cancel_action",
            "no_write",
        ),
        (
            "不需要参考攻略里的地点生成新方案",
            "cancel_action",
            "no_write",
        ),
        (
            "参考攻略建议的地点生成新的方案为什么失败了？",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "参考攻略里面的景点，按照我的需求给出方案为什么失败了？",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "参考攻略建议的地点生成新的方案时报错了",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "你刚才说“参考攻略建议的地点，生成新的方案”，是什么意思？",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "讨论一下‘参考攻略建议的地点，生成新的方案’这句话",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "“参考攻略建议的地点，生成新的方案”",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "“参考攻略里面的景点，按照我的需求给出个方案”",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "参考攻略里的景点，系统已经给出个方案",
            "inspect_or_explain",
            "read_only",
        ),
        (
            "参考攻略里的景点，系统可以给出个方案",
            "inspect_or_explain",
            "read_only",
        ),
    ],
)
@pytest.mark.parametrize("routing_mode", ["legacy-only", "active-all"])
def test_router_never_executes_negated_failed_or_quoted_guide_expansion(
    message: str,
    expected_intent: str,
    expected_disposition: str,
    routing_mode: str,
) -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))

    routed = ConversationIntentRouter(lite_classifier=lite, routing_mode=routing_mode).classify(
        message,
        state_summary={"hasPlanningRoot": True, "expansionCapabilityCount": 1},
    )

    assert routed.classification is not None
    assert routed.classification.intent == expected_intent
    assert routed.execution_disposition == expected_disposition
    assert routed.continuation_mode is None
    assert routed.model_called is False
    assert lite.calls == []


@pytest.mark.parametrize("message", ["给出个方案", "给我个方案", "给个旅行方案"])
def test_router_does_not_treat_bare_plan_wording_as_guide_continuation(message: str) -> None:
    assert ConversationIntentRouter()._deterministic_classification(message) is None


def test_router_keeps_default_lite_confidence_threshold_at_point_82() -> None:
    lite = RecordingLiteClassifier(
        {
            "intent": "continue_plan_expansion",
            "confidence": 0.81,
            "requestedScope": "planning_root",
            "isQuestion": False,
            "isNegated": False,
        }
    )

    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "再给我一种不同玩法",
        state_summary={"hasPlanningRoot": True, "expansionCapabilityCount": 1},
    )

    assert ConversationIntentRouter.MIN_MODEL_CONFIDENCE == 0.82
    assert routed.requires_clarification is True
    assert routed.reason_code == "intent_confidence_below_threshold"
    assert routed.execution_disposition == "clarify"


def test_router_uses_lite_contract_for_different_play_style() -> None:
    lite = RecordingLiteClassifier(
        {
            "intent": "continue_plan_expansion",
            "confidence": 0.94,
            "requestedScope": "planning_root",
            "isQuestion": False,
            "isNegated": False,
        }
    )
    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "再给我一种不同玩法",
        state_summary={
            "hasActiveVersion": True,
            "hasPlanningRoot": True,
            "hasPortfolio": True,
            "pendingCapabilityCount": 2,
        },
    )

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.source == "lite_controller"
    assert routed.model_called is True
    assert routed.requires_clarification is False
    assert len(lite.calls) == 1
    assert lite.calls[0]["schemaVersion"] == "conversation-intent-context-v1"
    serialized = json.dumps(lite.calls[0], ensure_ascii=False)
    for forbidden in (
        "choiceId",
        "portfolioId",
        "versionId",
        "planningSelectionRootTurnId",
        "requestContractFingerprint",
    ):
        assert forbidden not in serialized


def test_router_preserves_negative_preference_as_modify_intent() -> None:
    lite = RecordingLiteClassifier(
        {
            "intent": "modify_itinerary",
            "confidence": 0.97,
            "requestedScope": "active_itinerary",
            "isQuestion": False,
            "isNegated": True,
        }
    )

    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "我不要骑行，改成公交地铁",
        state_summary={"hasActiveVersion": True},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "modify_itinerary"
    assert routed.classification.is_negated is True
    assert routed.execution_disposition == "execute"


def test_router_preserves_provider_invocation_truth_on_lite_failure() -> None:
    evidence = ({"callKind": "lite", "providerInvoked": False},)
    routed = ConversationIntentRouter(
        lite_classifier=lambda _context: ConversationIntentModelOutcome(
            error_code="controller_worker_queue_saturated",
            provider_invoked=False,
            performance_evidence=evidence,
        )
    ).classify("再给我一种不同玩法", state_summary={})

    assert routed.requires_clarification is True
    assert routed.model_attempted is True
    assert routed.model_called is False
    assert routed.model_succeeded is False
    assert routed.model_performance_evidence == evidence


def test_router_classifies_expansion_failure_question_as_inspect_without_model() -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))
    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "为什么继续新增方案失败",
        state_summary={"hasActiveVersion": True},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "inspect_or_explain"
    assert routed.classification.is_question is True
    assert routed.execution_disposition == "read_only"
    assert lite.calls == []


def test_router_classifies_negated_expansion_as_cancel_without_model() -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))
    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "不要继续生成其他方案",
        state_summary={"hasActiveVersion": True},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "cancel_action"
    assert routed.classification.is_negated is True
    assert routed.execution_disposition == "no_write"
    assert lite.calls == []


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("创建一个北京行程", "create_itinerary"),
        ("修改当前行程", "modify_itinerary"),
        ("继续新增一个方案", "continue_plan_expansion"),
        ("采用这个方案", "adopt_plan"),
        ("继续补全待补槽位", "continue_pending_slot"),
        ("手动搜索地点", "manual_candidate_search"),
        ("重新搜索一遍攻略", "search_travel_guide_advice"),
        ("重试一遍", "retry_current_stage"),
        ("从头重新生成", "regenerate_from_scratch"),
        ("为什么刚才失败", "inspect_or_explain"),
        ("不要继续生成其他方案", "cancel_action"),
    ],
)
def test_router_unifies_high_confidence_intent_contract(
    message: str,
    expected_intent: str,
) -> None:
    routed = ConversationIntentRouter().classify(message, state_summary={})

    assert routed.classification is not None
    assert routed.classification.intent == expected_intent


def test_router_accepts_clarification_answer_only_through_five_field_contract() -> None:
    lite = RecordingLiteClassifier(
        {
            "intent": "clarification_answer",
            "confidence": 0.93,
            "requestedScope": "clarification",
            "isQuestion": False,
            "isNegated": False,
        }
    )

    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "10月2日出发，两个人",
        state_summary={"hasUnansweredClarification": True},
    )

    assert routed.classification is not None
    assert routed.classification.intent == "clarification_answer"
    assert routed.source == "lite_controller"


def test_router_rejects_model_output_that_contains_execution_identity() -> None:
    lite = RecordingLiteClassifier(
        {
            "intent": "continue_plan_expansion",
            "confidence": 0.99,
            "requestedScope": "planning_root",
            "isQuestion": False,
            "isNegated": False,
            "choiceId": "model_must_not_choose",
        }
    )
    routed = ConversationIntentRouter(lite_classifier=lite).classify(
        "再给我一种不同玩法",
        state_summary={"hasPlanningRoot": True},
    )

    assert routed.model_called is True
    assert routed.requires_clarification is True
    assert routed.reason_code == "intent_model_contract_invalid"


def test_persisted_opaque_choice_bypasses_intent_model() -> None:
    lite = RecordingLiteClassifier(AssertionError("Lite should not run"))
    routed = ConversationIntentRouter(lite_classifier=lite).bypass_persisted_choice()

    assert routed.classification is None
    assert routed.source == "persisted_opaque_choice"
    assert routed.model_called is False
    assert lite.calls == []


def test_capability_resolver_binds_unique_expansion_without_reading_label(
    db_connection,
) -> None:
    session_id, expected_version_id = _seed_capability_session(
        db_connection,
        options=[
            _expansion_option(
                "choice_expand",
                label="采用方案 B（该 label 不能参与路由）",
            ),
            _pending_slot_option("choice_pending"),
        ],
    )
    session = _session_row(db_connection, session_id)
    classification = (
        ConversationIntentRouter()
        .classify(
            "继续新增一个方案",
            state_summary={"hasActiveVersion": True},
        )
        .classification
    )
    assert classification is not None

    resolved = ConversationCapabilityResolver(db_connection).resolve(
        session=session,
        classification=classification,
    )

    assert resolved.status == "unique"
    assert resolved.capability == "continue_plan_expansion"
    assert resolved.selected_choice_request == {
        "sourceAssistantTurnId": "turn_capability_assistant",
        "choiceId": "choice_expand",
    }
    assert resolved.planning_root_id == "planning_root"
    assert resolved.root_portfolio_id == "portfolio_root"
    assert resolved.request_contract_fingerprint == "f" * 64
    assert resolved.expected_base_version_id == expected_version_id


def test_capability_resolver_rejects_multiple_expansion_capabilities(
    db_connection,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[
            _expansion_option("choice_expand_a", focus_brief_id="brief_a"),
            _expansion_option("choice_expand_b", focus_brief_id="brief_b"),
        ],
    )
    classification = (
        ConversationIntentRouter()
        .classify(
            "继续新增一个方案",
            state_summary={"hasActiveVersion": True},
        )
        .classification
    )
    assert classification is not None

    resolved = ConversationCapabilityResolver(db_connection).resolve(
        session=_session_row(db_connection, session_id),
        classification=classification,
    )

    assert resolved.status == "ambiguous"
    assert resolved.selected_choice_request is None
    assert resolved.reason_code == "multiple_matching_capabilities"


def test_capability_resolver_never_falls_back_to_older_assistant_turn(
    db_connection,
) -> None:
    session_id, version_id = _seed_capability_session(
        db_connection,
        options=[_expansion_option("choice_expand_old")],
    )
    _insert_turn(
        db_connection,
        session_id=session_id,
        turn_id="turn_latest_user",
        role="user",
        content="为什么刚才失败",
        turn_index=3,
    )
    latest_pending = _pending_slot_option("choice_pending_latest")
    latest_pending["expectedBaseVersionId"] = version_id
    _insert_turn(
        db_connection,
        session_id=session_id,
        turn_id="turn_latest_assistant",
        role="assistant",
        content="请使用最新待补槽位操作。",
        turn_index=4,
        response={"choiceOptions": [latest_pending], "consumedChoiceIds": []},
    )
    db_connection.commit()
    classification = ConversationIntentRouter().classify("继续新增一个方案", state_summary={}).classification
    assert classification is not None

    resolved = ConversationCapabilityResolver(db_connection).resolve(
        session=_session_row(db_connection, session_id),
        classification=classification,
    )

    assert resolved.status == "none"
    assert resolved.selected_choice_request is None


@pytest.mark.parametrize(
    ("include_selection", "attach_expected_base_version"),
    [(False, True), (True, False)],
)
def test_pending_capability_requires_authoritative_lineage_and_base_version(
    db_connection,
    include_selection: bool,
    attach_expected_base_version: bool,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[_pending_slot_option("choice_pending")],
        include_selection=include_selection,
        attach_expected_base_version=attach_expected_base_version,
    )
    classification = (
        ConversationIntentRouter(
            lite_classifier=lambda _context: {
                "intent": "continue_pending_slot",
                "confidence": 0.97,
                "requestedScope": "pending_slot",
                "isQuestion": False,
                "isNegated": False,
            }
        )
        .classify("把缺的那个时段接着处理", state_summary={})
        .classification
    )
    assert classification is not None

    resolved = ConversationCapabilityResolver(db_connection).resolve(
        session=_session_row(db_connection, session_id),
        classification=classification,
    )

    assert resolved.status == "none"


def test_expansion_capability_rejects_cross_root_identity(db_connection) -> None:
    option = _expansion_option("choice_cross_root")
    option["planningSelectionRootTurnId"] = "different_root"
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[option],
    )
    classification = ConversationIntentRouter().classify("继续新增一个方案", state_summary={}).classification
    assert classification is not None

    resolved = ConversationCapabilityResolver(db_connection).resolve(
        session=_session_row(db_connection, session_id),
        classification=classification,
    )

    assert resolved.status == "none"


def test_agent_service_lite_route_binds_unique_expansion_over_pending_slot(
    db_connection,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[
            _expansion_option("choice_expand"),
            _pending_slot_option("choice_pending"),
        ],
    )
    provider = IntentOnlyProvider()
    service = AgentService(db_connection, provider=provider)

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content="再给我一种不同玩法",
        payload=AgentMessageRequest(content="再给我一种不同玩法", context={}),
    )

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.model_called is True
    assert len(provider.calls) == 1
    assert resolved.status == "unique"
    assert resolved.planning_root_id == "planning_root"
    selected = routed_payload.context.selected_agent_choice
    assert selected is not None
    assert selected.source_assistant_turn_id == "turn_capability_assistant"
    assert selected.choice_id == "choice_expand"


@pytest.mark.parametrize(
    ("message", "routing_mode"),
    [
        ("参考攻略建议的地点，生成新的方案", "legacy-only"),
        ("参考攻略里面的景点，按照我的需求给出个方案", "active-all"),
    ],
)
def test_agent_service_guide_grounded_route_binds_latest_opaque_expansion(
    db_connection,
    message: str,
    routing_mode: str,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[
            _guide_expansion_option(
                "choice_guide_grounded_expand",
                label="这个显示文案与攻略无关",
            ),
            _pending_slot_option("choice_pending"),
        ],
    )
    carrier = db_connection.execute(
        "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
        ("turn_capability_assistant",),
    ).fetchone()
    carrier_payload = json.loads(carrier[0])
    carrier_payload.update(
        {
            "mode": "travel_guide_advice",
            "guideAdvice": {
                "status": "completed",
                "evidenceFingerprint": "e" * 64,
                "placeHints": [{"mentionText": "北海公园"}],
            },
        }
    )
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (json.dumps(carrier_payload, ensure_ascii=False), "turn_capability_assistant"),
    )
    db_connection.commit()
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    service.conversation_intent_router.routing_mode = routing_mode

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content=message,
        payload=AgentMessageRequest(content=message, context={}),
    )

    if routing_mode == "active-all":
        # This historical fixture deliberately has no succeeded guide
        # execution/query fingerprint. It is invalid in the new catalog.
        assert routed.requires_clarification is True
        assert routed_payload.context.selected_agent_choice is None
        return

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.continuation_mode == "guide_grounded"
    assert routed.model_called is False
    assert routed.source == "deterministic_fast_path"
    assert resolved.status == "unique"
    selected = routed_payload.context.selected_agent_choice
    assert selected is not None
    assert selected.source_assistant_turn_id == "turn_capability_assistant"
    assert selected.choice_id == "choice_guide_grounded_expand"


def test_agent_service_active_all_replays_guide_continuation_from_one_snapshot(
    db_connection,
) -> None:
    from backend.tests.unit.test_guide_grounded_continuation import _persist_guide_carrier
    from backend.tests.unit.test_conversation_action_catalog import response
    session_id, selected = _persist_guide_carrier(db_connection)
    classifier = RecordingLiteClassifier(response("continue_with_guide"))
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    service.conversation_intent_router.routing_mode = "active-all"
    service.conversation_intent_router.lite_classifier = classifier

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content="基于搜索到的建议，给我生成一个方案出来",
        payload=AgentMessageRequest(content="基于搜索到的建议，给我生成一个方案出来", context={}),
    )

    assert routed.source == "state_aware_lite"
    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.classification.requested_scope == "planning_root"
    assert routed.continuation_mode == "guide_grounded"
    assert routed.execution_intent == {
        "schemaVersion": "conversation-execution-intent-v1",
        "status": "authorized",
        "semanticIntent": "continue_plan_expansion",
        "riskTier": "proposal_only",
        "targetStatus": "unique",
        "capabilityStatus": "unique",
        "stateFingerprint": routed.state_fingerprint,
        "writeAllowed": True,
        "reasonCode": "semantic_action_accepted",
    }
    assert resolved.status == "unique"
    assert resolved.selected_choice_request == {
        "sourceAssistantTurnId": selected["sourceAssistantTurnId"],
        "choiceId": selected["choiceId"],
    }
    assert routed_payload.context.selected_agent_choice is not None
    assert routed_payload.context.selected_agent_choice.choice_id == selected["choiceId"]
    assert len(classifier.calls) == 1
    model_state = classifier.calls[0]["state"]
    assert model_state["workflowPhase"] == "guide_advice"
    assert model_state["guideContinuation"]["available"] is True
    assert model_state["guideContinuation"]["evidenceStatus"] == "validated"
    assert model_state["guideContinuation"]["sourceRelation"] == "current_reply"
    assert model_state["planningContext"]["requestPolicy"] == "reuse_original_contract"
    assert "guideAdvice" not in model_state["latestAssistant"]
    serialized = json.dumps(classifier.calls[0], ensure_ascii=False)
    assert selected["sourceAssistantTurnId"] not in serialized
    assert selected["choiceId"] not in serialized


def test_agent_service_guide_grounded_route_rejects_generic_portfolio_retry(
    db_connection,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[_expansion_option("choice_generic_retry")],
    )
    row = db_connection.execute(
        "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
        ("turn_capability_assistant",),
    ).fetchone()
    response = json.loads(row[0])
    response.update(
        {
            "mode": "travel_guide_advice",
            "guideAdvice": {
                "status": "completed",
                "evidenceFingerprint": "e" * 64,
                "placeHints": [{"mentionText": "北海公园"}],
            },
        }
    )
    db_connection.execute(
        "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
        (json.dumps(response, ensure_ascii=False), "turn_capability_assistant"),
    )
    db_connection.commit()
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    service.conversation_intent_router.routing_mode = "active-all"
    from backend.tests.unit.test_conversation_action_catalog import response as action_response
    service.conversation_intent_router.lite_classifier = RecordingLiteClassifier(action_response("continue_with_guide"))

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content="参考攻略里面的景点，按照我的需求给出个方案",
        payload=AgentMessageRequest(content="参考攻略里面的景点，按照我的需求给出个方案", context={}),
    )

    assert routed.requires_clarification is True
    assert routed.execution_disposition == "clarify"
    assert routed.reason_code == "semantic_action_unavailable"
    assert resolved.status == "not_required"
    assert routed_payload.context.selected_agent_choice is None


def test_agent_service_generate_short_retry_binds_route_degraded_opaque_choice(
    db_connection,
    monkeypatch,
) -> None:
    option = _expansion_option("choice_retry_next", focus_brief_id="brief_next")
    option["retryCurrentStageEligible"] = True
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[option],
    )
    _insert_turn(
        db_connection,
        session_id=session_id,
        turn_id="turn_retry_user",
        role="user",
        content="重试",
        turn_index=3,
    )
    db_connection.commit()
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    captured: dict[str, Any] = {}

    def capture_context(_session, _content, routed_payload, **kwargs):
        captured["payload"] = routed_payload
        captured["allowPlanExpansionRebind"] = kwargs.get("allow_plan_expansion_rebind")
        raise RuntimeError("captured_routed_payload")

    monkeypatch.setattr(service, "_build_request_context", capture_context)

    with pytest.raises(RuntimeError, match="captured_routed_payload"):
        service._generate_for_user_turn(
            session_id,
            "turn_retry_user",
            "重试",
            AgentMessageRequest(content="重试", context={}),
        )

    assert captured["allowPlanExpansionRebind"] is False
    selected = captured["payload"].context.selected_agent_choice
    assert selected is not None
    assert selected.source_assistant_turn_id == "turn_capability_assistant"
    assert selected.choice_id == "choice_retry_next"


def test_agent_service_persisted_choice_bypasses_model_even_when_text_is_negated(
    db_connection,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[_expansion_option("choice_expand")],
    )
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    payload = AgentMessageRequest(
        content="不要继续生成其他方案",
        context={
            "selectedAgentChoice": {
                "sourceAssistantTurnId": "turn_capability_assistant",
                "choiceId": "choice_expand",
            }
        },
    )

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content=payload.content,
        payload=payload,
    )

    assert routed.source == "persisted_opaque_choice"
    assert routed.model_called is False
    assert resolved.status == "not_required"
    assert routed_payload.context.selected_agent_choice is not None
    assert routed_payload.context.selected_agent_choice.choice_id == "choice_expand"


def test_agent_service_manual_search_binds_literal_only_after_unique_capability(
    db_connection,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[_manual_search_option("choice_manual")],
    )
    service = AgentService(db_connection, provider=ProviderMustNotRun())

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content="手动搜索地点",
        payload=AgentMessageRequest(content="手动搜索地点", context={}),
    )

    assert routed.classification is not None
    assert routed.classification.intent == "manual_candidate_search"
    assert resolved.status == "unique"
    selected = routed_payload.context.selected_agent_choice
    assert selected is not None
    assert selected.choice_id == "choice_manual"
    assert selected.manual_value == "手动搜索地点"


def test_agent_service_explicit_guide_retry_binds_fresh_opaque_choice_without_controller(
    db_connection,
) -> None:
    session_id, _version_id = _seed_capability_session(
        db_connection,
        options=[_guide_search_option("choice_guide_retry")],
    )
    service = AgentService(db_connection, provider=ProviderMustNotRun())

    routed_payload, routed, resolved = service._route_conversation_turn(
        session=_session_row(db_connection, session_id),
        content="重新搜索一遍攻略",
        payload=AgentMessageRequest(content="重新搜索一遍攻略", context={}),
    )

    assert routed.classification is not None
    assert routed.classification.intent == "search_travel_guide_advice"
    assert routed.source == "deterministic_fast_path"
    assert routed.model_called is False
    assert resolved.status == "unique"
    selected = routed_payload.context.selected_agent_choice
    assert selected is not None
    assert selected.source_assistant_turn_id == "turn_capability_assistant"
    assert selected.choice_id == "choice_guide_retry"


def test_deepseek_lite_transport_uses_strict_read_only_intent_prompt(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    provider = DeepSeekAgentProvider(
        api_key="unit-test-key",
        model="deepseek-chat",
    )

    def fake_post(payload: dict[str, Any], *, timeout_seconds: float) -> str:
        captured.update(payload)
        assert timeout_seconds > 0
        return "{}"

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.decide_autonomy_lite(
        {
            "schemaVersion": "conversation-intent-context-v1",
            "message": "再给我一种不同玩法",
            "state": {"hasActiveVersion": True},
        },
        timeout_seconds=1.5,
    )

    system_prompt = captured["messages"][0]["content"]
    assert "exactly one JSON object with only these five keys" in system_prompt
    for key in (
        "intent",
        "confidence",
        "requestedScope",
        "isQuestion",
        "isNegated",
    ):
        assert key in system_prompt
    assert "tools" not in captured
    assert captured["max_tokens"] == 180
    assert captured["thinking"] == {"type": "disabled"}
    assert "does not itself cancel" in system_prompt


def test_deepseek_lite_transport_uses_state_aware_v2_semantic_prompt(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    provider = DeepSeekAgentProvider(api_key="unit-test-key", model="deepseek-chat")

    def fake_post(payload: dict[str, Any], *, timeout_seconds: float) -> str:
        captured.update(payload)
        assert timeout_seconds > 0
        return "{}"

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.decide_autonomy_lite(
        {
            "schemaVersion": "conversation-intent-context-v2",
            "message": "基于刚才建议规划",
            "stateFingerprint": "f" * 64,
            "state": {"workflowPhase": "guide_advice"},
        },
        timeout_seconds=1.5,
    )

    system_prompt = captured["messages"][0]["content"]
    assert "conversation-intent-hypothesis-v2" in system_prompt
    assert "targetReference" in system_prompt
    assert "guide_grounded" in system_prompt
    assert "Never invent" in system_prompt
    assert "choice id" in system_prompt
    assert "tools" not in captured
    assert captured["thinking"] == {"type": "disabled"}


def test_context_builder_does_not_reclassify_when_unified_route_exists(
    db_connection,
    monkeypatch,
) -> None:
    session = ConversationService(db_connection).create_session(
        "北京",
        "unified route owns latest message",
    )
    session_row = _session_row(db_connection, session.session_id)
    route = ConversationIntentRouter().classify(
        "创建一个北京行程",
        state_summary={"hasActiveVersion": False},
    )

    def forbidden_legacy_classifier(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("RetrySemanticClassifier must not classify the current turn")

    monkeypatch.setattr(
        RetrySemanticClassifier,
        "classify",
        forbidden_legacy_classifier,
    )
    context = AgentContextBuilder(db_connection).build(
        session_row,
        "创建一个北京行程",
        AgentMessageRequest(content="创建一个北京行程", context={}),
        conversation_intent_route=route.to_context(),
        conversation_capability_resolution={"status": "not_required"},
    )

    assert context["effectiveUserMessage"] == "创建一个北京行程"
    assert context["conversationIntent"]["classification"]["intent"] == "create_itinerary"


def test_pending_spatial_checkpoint_precedes_view_context_but_not_explicit_new_root(
    db_connection,
) -> None:
    session = ConversationService(db_connection).create_session(
        "北京",
        "pending spatial clarification",
    )
    checkpoint = {
        "schemaVersion": "clarification-checkpoint-v2",
        "checkpointId": "checkpoint_spatial",
        "planningRootId": "planning_root_spatial",
        "fingerprint": "f" * 64,
    }
    _insert_turn(
        db_connection,
        session_id=session.session_id,
        turn_id="turn_spatial_assistant",
        role="assistant",
        content="还需确认活动区域",
        turn_index=1,
        request={
            "planningSelectionRootTurnId": "planning_root_spatial",
            "requestIntentContract": {
                "spatialPreference": {"status": "grounding_pending"},
            },
        },
        response={"clarificationCheckpoint": checkpoint},
    )
    db_connection.commit()
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    request = AgentMessageRequest(
        content="活动区域在一条命名环线以内",
        context={
            "viewContext": {
                "schemaVersion": "agent-view-context-v1",
                "activeView": "comparison",
            }
        },
    )

    routed_request, route, capability = service._route_conversation_turn(
        session=_session_row(db_connection, session.session_id),
        content=request.content,
        payload=request,
    )

    answer = routed_request.context.model_dump(by_alias=True)["spatialClarificationAnswer"]
    assert route.classification is not None
    assert route.classification.intent == "clarification_answer"
    assert route.reason_code == "pending_spatial_checkpoint_precedes_view_context"
    assert capability.capability == "clarification_answer"
    assert answer["sourceAssistantTurnId"] == "turn_spatial_assistant"
    assert answer["checkpointId"] == "checkpoint_spatial"
    assert answer["manualValue"] == request.content

    new_root_request = AgentMessageRequest(content="重新规划上海三天", context={})
    _, new_root_route, _ = service._route_conversation_turn(
        session=_session_row(db_connection, session.session_id),
        content=new_root_request.content,
        payload=new_root_request,
    )
    assert new_root_route.classification is not None
    assert new_root_route.classification.intent != "clarification_answer"


def test_lite_failure_is_zero_write_even_in_mock_mode(db_connection) -> None:
    session = ConversationService(db_connection).create_session(
        "北京",
        "intent lite failure",
    )
    before = _write_counts(db_connection, session.session_id)

    response = AgentService(
        db_connection,
        provider=ProviderMustNotRun(),
    ).send_message(
        session.session_id,
        AgentMessageRequest(content="给我安排北京两日游", context={}),
    )

    assert _write_counts(db_connection, session.session_id) == before
    assert response.version is None
    assert response.terminal_status == "needs_confirmation"
    assert "行程未发生变化" in response.assistant_turn.content
    assert "规划控制器本轮未形成" not in response.assistant_turn.content


def test_active_all_intent_provider_failure_is_zero_write_end_to_end(
    db_connection,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_INTENT_ROUTING_MODE", "active-all")
    get_settings.cache_clear()
    try:
        session = ConversationService(db_connection).create_session(
            "北京",
            "state aware intent provider failure",
        )
        before = _write_counts(db_connection, session.session_id)
        service = AgentService(
            db_connection,
            provider=FailingStateAwareIntentProvider(),
        )
        request = AgentMessageRequest(content="北京两天，慢一点，校园和公园都想去", context={})
        _, routed, _ = service._route_conversation_turn(
            session=_session_row(db_connection, session.session_id),
            content=request.content,
            payload=request,
        )
        response = service.send_message(
            session.session_id,
            request,
        )
    finally:
        get_settings.cache_clear()

    assert _write_counts(db_connection, session.session_id) == before
    assert routed.reason_code == "intent_provider_unavailable"
    assert routed.execution_disposition == "clarify"
    assert routed.invocation_ledger["intentModelAttemptCount"] == 1
    assert response.version is None
    assert response.terminal_status == "needs_confirmation"
    assert "行程未发生变化" in response.assistant_turn.content
    assert "通用意图解释服务本轮不可用" in response.assistant_turn.content
    assert "尚未调用规划控制器" in response.assistant_turn.content


def test_explicit_other_direction_phrase_bypasses_controller() -> None:
    routed = ConversationIntentRouter().classify("继续生成其他方向方案", state_summary={})

    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.execution_disposition == "execute"
    assert routed.source == "deterministic_fast_path"
    assert routed.model_called is False


def test_intent_decision_reports_lite_source_and_performance_truth(
    db_connection,
) -> None:
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    result = service._conversation_intent_decision(
        {
            "conversationIntent": {
                "classification": {
                    "intent": "inspect_or_explain",
                    "confidence": 0.96,
                    "requestedScope": "current_action",
                    "isQuestion": True,
                    "isNegated": False,
                },
                "source": "lite_controller",
                "modelAttempted": True,
                "modelCalled": True,
                "modelSucceeded": True,
                "modelPerformanceEvidence": [{"callKind": "lite", "providerInvoked": True}],
                "requiresClarification": False,
                "reasonCode": "intent_model_contract_accepted",
                "executionDisposition": "read_only",
            },
            "conversationCapability": {"status": "not_required"},
            "retryExecutionPlan": {},
        },
        cycle_index=0,
    )

    assert result is not None
    assert result.source == "controller"
    assert result.decision_path == "lite"
    assert result.controller_lite_called is True
    assert result.controller_lite_succeeded is True
    assert result.controller_performance == ({"callKind": "lite", "providerInvoked": True},)


def test_state_aware_intent_decision_preserves_server_invocation_ledger(
    db_connection,
) -> None:
    service = AgentService(db_connection, provider=ProviderMustNotRun())
    ledger = {
        "intentModelAttemptCount": 1,
        "intentProviderCallCount": 1,
        "fullControllerCallCount": 0,
        "fallbackControllerLiteCallCount": 0,
        "plannerCallCount": 0,
    }
    result = service._conversation_intent_decision(
        {
            "conversationIntent": {
                "classification": {
                    "intent": "inspect_or_explain",
                    "confidence": 0.91,
                    "requestedScope": "current_action",
                    "isQuestion": True,
                    "isNegated": False,
                },
                "source": "state_aware_lite",
                "modelAttempted": True,
                "modelCalled": True,
                "modelSucceeded": True,
                "requiresClarification": False,
                "reasonCode": "state_aware_semantics_accepted",
                "executionDisposition": "read_only",
                "invocationLedger": ledger,
            },
            "conversationCapability": {"status": "not_required"},
            "retryExecutionPlan": {},
        },
        cycle_index=0,
    )

    assert result is not None
    assert result.invocation_ledger == ledger
    assert result.controller_lite_called is False
    metadata = result.to_event_metadata()
    assert metadata["invocationLedger"] == ledger


def test_expansion_without_capability_has_zero_execution_and_itinerary_writes(
    db_connection,
) -> None:
    session = ConversationService(db_connection).create_session(
        "北京",
        "intent router no capability",
    )
    before = _write_counts(db_connection, session.session_id)

    response = AgentService(
        db_connection,
        provider=ProviderMustNotRun(),
    ).send_message(
        session.session_id,
        AgentMessageRequest(content="继续新增一个方案", context={}),
    )

    after = _write_counts(db_connection, session.session_id)
    assert after == before
    assert response.version is None
    assert response.terminal_status == "no_safe_action"
    assert response.assistant_turn.clarification_checkpoint is None


@pytest.mark.parametrize(
    ("message", "expected_fragment"),
    [
        ("为什么继续新增方案失败", "本轮未写入行程"),
        ("不要继续生成其他方案", "本轮未写入行程"),
    ],
)
def test_question_and_negation_have_zero_itinerary_writes(
    db_connection,
    message: str,
    expected_fragment: str,
) -> None:
    session = ConversationService(db_connection).create_session(
        "北京",
        "intent router no write",
    )
    before = _write_counts(db_connection, session.session_id)

    response = AgentService(
        db_connection,
        provider=ProviderMustNotRun(),
    ).send_message(
        session.session_id,
        AgentMessageRequest(content=message, context={}),
    )

    assert _write_counts(db_connection, session.session_id) == before
    assert response.version is None
    assert expected_fragment in response.assistant_turn.content


def _seed_capability_session(
    connection,
    *,
    options: list[dict[str, Any]],
    include_selection: bool = True,
    attach_expected_base_version: bool = True,
) -> tuple[str, str]:
    session = ConversationService(connection).create_session(
        "北京",
        "intent router capability",
    )
    snapshot_service = ItinerarySnapshotService(connection)
    snapshot = snapshot_service.capture_snapshot(session.active_plan_id)
    snapshot_update = {
        "portfolioPartialTimeline": {"status": "partial", "pendingSlotCount": 1},
        "portfolioPendingSlots": [
            {
                "creativeBriefId": "brief_current",
                "poolId": "pool_pending",
                "planningSlotId": "slot_pending",
                "dayNumber": 1,
            }
        ],
    }
    if include_selection:
        snapshot_update["portfolioSelectionContext"] = {
            "planningSelectionRootTurnId": "planning_root",
            "rootPortfolioId": "portfolio_root",
            "focusBriefId": "brief_current",
            "requestContractFingerprint": "f" * 64,
        }
    snapshot.update(snapshot_update)
    version = snapshot_service.save_version(
        session.session_id,
        session.active_plan_id,
        "portfolio_partial",
        snapshot=snapshot,
    )
    if attach_expected_base_version:
        for option in options:
            option["expectedBaseVersionId"] = version.id
    _insert_turn(
        connection,
        session_id=session.session_id,
        turn_id="turn_capability_user",
        role="user",
        content="北京两日游",
        turn_index=1,
    )
    _insert_turn(
        connection,
        session_id=session.session_id,
        turn_id="turn_capability_assistant",
        role="assistant",
        content="当前方案可继续操作。",
        turn_index=2,
        response={"choiceOptions": options, "consumedChoiceIds": []},
    )
    if any(option.get("action") in {"continue_plan_expansion", "search_travel_guide_advice", "select_plan_proposal"} for option in options):
        # This constructed capability fixture must include the authoritative
        # root/portfolio now required by the read-only action catalog.
        from src.services.creative_planning_models import PlanPortfolio
        from src.services.plan_portfolio_store import PlanPortfolioStore
        _insert_turn(connection, session_id=session.session_id, turn_id="planning_root", role="user",
                     content="北京两日游", turn_index=0)
        PlanPortfolioStore(connection).create(PlanPortfolio(
            portfolioId="portfolio_root", sessionId=session.session_id, sourceUserTurnId="planning_root",
            sourceAssistantTurnId="turn_capability_assistant", expectedBaseVersionId=version.id,
            sourceObservationFingerprint="o" * 64, requestContractFingerprint="f" * 64,
            status="awaiting_selection"), [])
    connection.commit()
    return session.session_id, version.id


def _expansion_option(
    choice_id: str,
    *,
    focus_brief_id: str = "brief_next",
    label: str = "继续生成其他方案",
) -> dict[str, Any]:
    return {
        "id": choice_id,
        "kind": "portfolio_more_plans",
        "action": "retry_model_planning",
        "label": label,
        "planningSelectionRootTurnId": "planning_root",
        "rootPortfolioId": "portfolio_root",
        "requestContractFingerprint": "f" * 64,
        "focusBriefId": focus_brief_id,
        "expansionFocusMode": "exact",
    }


def _guide_expansion_option(
    choice_id: str,
    *,
    label: str = "参考攻略继续生成方案",
) -> dict[str, Any]:
    return {
        "id": choice_id,
        "choiceId": choice_id,
        "kind": "simple_direction_more_plans",
        "action": "continue_plan_expansion",
        "label": label,
        "planningSelectionRootTurnId": "planning_root",
        "rootPortfolioId": "portfolio_root",
        "requestContractFingerprint": "f" * 64,
    }


def _pending_slot_option(choice_id: str) -> dict[str, Any]:
    return {
        "id": choice_id,
        "kind": "portfolio_density_retry",
        "action": "refresh_density_candidates",
        "label": "继续新增一个方案（恶意相同 label）",
        "briefId": "brief_current",
        "poolId": "pool_pending",
        "planningSlotId": "slot_pending",
        "dayNumber": 1,
    }


def _manual_search_option(choice_id: str) -> dict[str, Any]:
    return {
        "id": choice_id,
        "kind": "custom_input",
        "action": "manual_continuation",
        "label": "label must not be parsed",
        "briefId": "brief_current",
        "poolId": "pool_pending",
        "planningSlotId": "slot_pending",
        "dayNumber": 1,
    }


def _guide_search_option(choice_id: str) -> dict[str, Any]:
    return {
        "id": choice_id,
        "choiceId": choice_id,
        "kind": "travel_guide_advice",
        "action": "search_travel_guide_advice",
        "label": "重新搜索普通攻略",
        "planningSelectionRootTurnId": "planning_root",
        "rootPortfolioId": "portfolio_root",
        "requestContractFingerprint": "f" * 64,
    }


def _session_row(connection, session_id: str):
    return connection.execute(
        "SELECT * FROM conversation_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()


def _write_counts(connection, session_id: str) -> tuple[int, int, int, int]:
    plan_id = connection.execute(
        "SELECT active_plan_id FROM conversation_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()[0]
    return (
        connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM agent_choice_executions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0],
    )


def _insert_turn(
    connection,
    *,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    turn_index: int,
    request: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO conversation_turns (
            id, session_id, role, content, turn_index, status,
            parent_turn_id, itinerary_version_id, agent_request_json,
            agent_response_json, error_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?, '{}', ?, ?)
        """,
        (
            turn_id,
            session_id,
            role,
            content,
            turn_index,
            json.dumps(request or {}, ensure_ascii=False),
            json.dumps(response or {}, ensure_ascii=False),
            now,
            now,
        ),
    )

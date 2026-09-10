from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from src.core.config import Settings, get_settings
from typing import Any

from src.services.conversation_intent_router import (
    ConversationCapabilityResolution,
    ConversationCapabilityResolver,
    ConversationIntentClassification,
    ConversationIntentModelOutcome,
    ConversationIntentRouter as ProductionConversationIntentRouter,
    IntentRoutingSnapshotV1,
)


class ConversationIntentRouter(ProductionConversationIntentRouter):
    """Characterize the retained V2 hypothesis path, not the new active entry.

    Native action-selection regressions live in test_conversation_action_catalog.
    These old risk/confidence assertions remain useful for compatibility modes.
    """
    def classify(self, message, *, state_summary=None, routing_snapshot=None):
        if self.routing_mode == "active-all":
            return self._classify_hypothesis(message.strip(), state_summary=state_summary, routing_snapshot=routing_snapshot)
        return super().classify(message, state_summary=state_summary, routing_snapshot=routing_snapshot)


class RecordingClassifier:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, context: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(context)
        return self.response


def _snapshot(*, phase: str = "guide_advice", expansion_count: int = 1) -> IntentRoutingSnapshotV1:
    projection = {
        "schemaVersion": "intent-routing-model-state-v1",
        "workflowPhase": phase,
        "lifecycle": "active",
        "latestAssistant": {
            "mode": "travel_guide_advice" if phase == "guide_advice" else "creative_portfolio",
            "terminalStatus": "completed",
            "artifactKinds": ["guide_advice"] if phase == "guide_advice" else ["proposal_set"],
            "capabilities": [
                {
                    "semanticKind": "continue_plan_expansion",
                    "count": expansion_count,
                }
            ],
        },
        "references": {
            "dayNumbers": [1, 2],
            "proposalOrdinals": [1, 2],
            "segments": [
                {"ordinal": 1, "dayNumber": 1, "timeBucket": "afternoon", "name": "北海公园"}
            ],
        },
        "conflicts": [],
    }
    return IntentRoutingSnapshotV1(
        fingerprint="a" * 64,
        model_projection=projection,
        server_state={
            "hasActiveVersion": True,
            "hasPlanningRoot": True,
            "hasPortfolio": True,
            "latestArtifactKind": "guide_advice" if phase == "guide_advice" else "proposal_set",
        },
    )


def _v2_response(
    *,
    intent: str,
    scope: str,
    confidence: float = 0.94,
    target_kind: str = "none",
    continuation_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": "conversation-intent-hypothesis-v2",
        "primary": {
            "intent": intent,
            "confidence": confidence,
            "requestedScope": scope,
            "isQuestion": False,
            "isNegated": False,
            "continuationMode": continuation_mode,
            "targetReference": {"kind": target_kind},
        },
        "alternatives": [],
        "semanticSignals": {
            "quotedCommand": False,
            "hypothetical": False,
            "correctionAfterNegation": False,
        },
    }


def test_active_all_uses_state_aware_model_before_legacy_positive_regex() -> None:
    classifier = RecordingClassifier(
        _v2_response(
            intent="continue_plan_expansion",
            scope="planning_root",
            target_kind="latest_guide",
            continuation_mode="guide_grounded",
        )
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-all")

    routed = router.classify(
        "基于搜索到的建议，给我生成一个方案出来",
        routing_snapshot=_snapshot(),
    )

    assert routed.classification is not None
    assert routed.classification.intent == "continue_plan_expansion"
    assert routed.classification.requested_scope == "planning_root"
    assert routed.continuation_mode == "guide_grounded"
    assert routed.source == "state_aware_lite"
    assert routed.model_called is True
    assert routed.reason_code == "state_aware_semantics_accepted"
    assert len(classifier.calls) == 1


def test_v2_model_projection_is_bounded_fingerprinted_and_identity_free() -> None:
    classifier = RecordingClassifier(
        _v2_response(
            intent="inspect_or_explain",
            scope="current_action",
            confidence=0.76,
        )
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-all")

    routed = router.classify("当前两个方案有什么区别", routing_snapshot=_snapshot(phase="proposal_selection"))

    assert routed.execution_disposition == "read_only"
    assert len(classifier.calls) == 1
    context = classifier.calls[0]
    assert context["schemaVersion"] == "conversation-intent-context-v2"
    assert context["stateFingerprint"] == "a" * 64
    assert len(json.dumps(context, ensure_ascii=False).encode("utf-8")) <= router.MODEL_CONTEXT_BYTE_LIMIT
    serialized = json.dumps(context, ensure_ascii=False)
    for forbidden in (
        "choiceId",
        "turnId",
        "versionId",
        "segmentId",
        "portfolioId",
        "nonce",
        "patch_itinerary",
    ):
        assert forbidden not in serialized


def test_v2_rejects_model_execution_identity_and_does_not_use_it_as_fallback() -> None:
    response = _v2_response(
        intent="modify_itinerary",
        scope="active_itinerary",
        confidence=0.99,
        target_kind="current_itinerary",
    )
    response["primary"]["versionId"] = "forged-version"
    classifier = RecordingClassifier(response)

    routed = ConversationIntentRouter(
        lite_classifier=classifier,
        routing_mode="active-all",
    ).classify("把下午的安排换掉", routing_snapshot=_snapshot(phase="active_itinerary"))

    assert routed.requires_clarification is True
    assert routed.execution_disposition == "clarify"
    assert routed.reason_code == "intent_model_contract_invalid"
    assert routed.model_called is True


@pytest.mark.parametrize(
    ("forged_field", "forged_value"),
    [
        ("choiceId", "choice-forged"),
        ("turnId", "turn-forged"),
        ("segmentId", "segment-forged"),
        ("portfolioId", "portfolio-forged"),
        ("tool", "patch_itinerary"),
        ("patch", {"op": "remove", "path": "/days/0"}),
        ("capabilityAuthorized", True),
    ],
)
def test_v2_rejects_every_model_execution_authority_field(
    forged_field: str,
    forged_value: Any,
) -> None:
    response = _v2_response(
        intent="adopt_plan",
        scope="portfolio",
        confidence=0.99,
        target_kind="proposal_ordinal",
    )
    response["primary"][forged_field] = forged_value

    routed = ConversationIntentRouter(
        lite_classifier=RecordingClassifier(response),
        routing_mode="active-all",
    ).classify("就按第二个", routing_snapshot=_snapshot(phase="proposal_selection"))

    assert routed.requires_clarification is True
    assert routed.reason_code == "intent_model_contract_invalid"
    assert routed.execution_disposition == "clarify"


def test_risk_threshold_is_not_one_global_number() -> None:
    read_classifier = RecordingClassifier(
        _v2_response(
            intent="inspect_or_explain",
            scope="current_action",
            confidence=0.76,
        )
    )
    mutation_classifier = RecordingClassifier(
        _v2_response(
            intent="modify_itinerary",
            scope="active_itinerary",
            confidence=0.89,
            target_kind="current_itinerary",
        )
    )

    read_route = ConversationIntentRouter(
        lite_classifier=read_classifier,
        routing_mode="active-all",
    ).classify("解释一下当前方案", routing_snapshot=_snapshot(phase="active_itinerary"))
    mutation_route = ConversationIntentRouter(
        lite_classifier=mutation_classifier,
        routing_mode="active-all",
    ).classify("调整当前行程", routing_snapshot=_snapshot(phase="active_itinerary"))

    assert read_route.requires_clarification is False
    assert read_route.risk_tier == "read_only"
    assert mutation_route.requires_clarification is True
    assert mutation_route.reason_code == "intent_confidence_below_risk_threshold"
    assert mutation_route.risk_tier == "mutation"


def test_shadow_mode_records_semantics_without_authorizing_them() -> None:
    classifier = RecordingClassifier(
        _v2_response(
            intent="continue_plan_expansion",
            scope="planning_root",
            target_kind="planning_root",
        )
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="shadow")

    routed = router.classify("继续新增一个方案", routing_snapshot=_snapshot(phase="proposal_selection"))

    assert routed.requires_clarification is False
    assert routed.source == "deterministic_fast_path"
    assert routed.reason_code == "shadow_mode_legacy_authoritative"
    assert routed.shadow_evaluation is not None
    assert routed.shadow_evaluation["intent"] == "continue_plan_expansion"
    assert len(classifier.calls) == 1


def test_shadow_mode_preserves_legacy_lite_authority_when_no_deterministic_rule_matches() -> None:
    classifier = RecordingClassifier(
        {
            "intent": "continue_plan_expansion",
            "confidence": 0.84,
            "requestedScope": "planning_root",
            "isQuestion": False,
            "isNegated": False,
        }
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="shadow")

    routed = router.classify("继续", routing_snapshot=_snapshot(phase="proposal_selection"))

    assert routed.source == "lite_controller"
    assert routed.requires_clarification is False
    assert routed.reason_code == "intent_model_contract_accepted"
    assert routed.shadow_evaluation is None
    assert len(classifier.calls) == 1
    assert classifier.calls[0]["schemaVersion"] == "conversation-intent-context-v1"


def test_active_read_does_not_authorize_proposal_generation_from_state_aware_model() -> None:
    classifier = RecordingClassifier(
        _v2_response(
            intent="continue_pending_slot",
            scope="pending_slot",
            target_kind="pending_slot",
        )
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-read")

    routed = router.classify(
        "把缺的那个晚间点接着补上",
        routing_snapshot=_snapshot(phase="pending_slot"),
    )

    assert routed.risk_tier == "proposal_only"
    assert routed.requires_clarification is True
    assert routed.execution_disposition == "clarify"
    assert routed.reason_code == "intent_routing_mode_write_disabled"
    assert len(classifier.calls) == 1


def test_compiler_fails_closed_on_target_or_capability_failure() -> None:
    classifier = RecordingClassifier(
        _v2_response(
            intent="continue_plan_expansion",
            scope="planning_root",
            target_kind="planning_root",
        )
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-all")
    route = router.classify("继续", routing_snapshot=_snapshot(phase="proposal_selection"))

    no_target = router.compile_execution_intent(
        route,
        target_status="none",
        capability=ConversationCapabilityResolution(
            status="unique",
            capability="continue_plan_expansion",
            selected_choice_request={"sourceAssistantTurnId": "server", "choiceId": "opaque"},
            reason_code="unique_server_capability",
        ),
        state_fingerprint="a" * 64,
    )
    no_capability = router.compile_execution_intent(
        route,
        target_status="unique",
        capability=ConversationCapabilityResolution(
            status="none",
            capability="continue_plan_expansion",
            selected_choice_request=None,
            reason_code="no_matching_capability",
        ),
        state_fingerprint="a" * 64,
    )

    assert no_target.requires_clarification is True
    assert no_target.reason_code == "intent_target_unresolved"
    assert no_target.execution_intent["writeAllowed"] is False
    assert no_capability.requires_clarification is True
    assert no_capability.reason_code == "intent_capability_none"
    assert no_capability.execution_intent["writeAllowed"] is False


def test_generic_quote_and_hypothetical_are_zero_model_read_only() -> None:
    classifier = RecordingClassifier(_v2_response(intent="adopt_plan", scope="portfolio", confidence=1.0))
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-all")

    for message in (
        "你刚才说‘采用第二个方案’是什么意思？",
        "如果我说采用第二个方案会发生什么？",
        "文档里写着‘采用第二个方案’",
    ):
        routed = router.classify(message, routing_snapshot=_snapshot(phase="proposal_selection"))
        assert routed.execution_disposition == "read_only"
        assert routed.model_called is False
    assert classifier.calls == []


def test_state_aware_replay_corpus() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "evals"
        / "cases"
        / "state_aware_intent_router_replay_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["schemaVersion"] == "state-aware-intent-replay-v1"
    assert len(fixture["cases"]) >= 18

    for case in fixture["cases"]:
        if case.get("safety"):
            classifier = RecordingClassifier({"unexpected": True})
        else:
            classifier = RecordingClassifier(
                {
                    "schemaVersion": "conversation-intent-hypothesis-v2",
                    "primary": {
                        "intent": case["intent"],
                        "confidence": case["confidence"],
                        "requestedScope": case["scope"],
                        "isQuestion": bool(case.get("isQuestion")),
                        "isNegated": bool(case.get("isNegated")),
                        "continuationMode": case.get("continuationMode"),
                        "targetReference": case["target"],
                    },
                    "alternatives": [],
                    "semanticSignals": {
                        "quotedCommand": False,
                        "hypothetical": False,
                        "correctionAfterNegation": bool(case.get("correctionAfterNegation")),
                    },
                }
            )
        routed = ConversationIntentRouter(
            lite_classifier=classifier,
            routing_mode="active-all",
        ).classify(case["message"], routing_snapshot=_snapshot(phase=case["phase"]))

        expected_intent = case.get("expectedIntent") or case.get("intent")
        assert routed.classification is not None, case["id"]
        assert routed.classification.intent == expected_intent, case["id"]
        assert routed.execution_disposition == case["expectedDisposition"], case["id"]
        if case.get("safety"):
            assert classifier.calls == [], case["id"]
        else:
            assert len(classifier.calls) == 1, case["id"]


def test_state_projection_compacts_to_deterministic_budget() -> None:
    snapshot = _snapshot(phase="active_itinerary")
    projection = snapshot.model_projection
    projection["references"]["segments"] = [
        {
            "ordinal": index,
            "dayNumber": (index % 7) + 1,
            "timeBucket": "afternoon",
            "name": "超长地点名称" * 60,
        }
        for index in range(100)
    ]
    snapshot = IntentRoutingSnapshotV1(snapshot.fingerprint, projection, snapshot.server_state)
    classifier = RecordingClassifier(
        _v2_response(
            intent="modify_itinerary",
            scope="active_itinerary",
            confidence=0.96,
            target_kind="current_itinerary",
        )
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-all")

    routed = router.classify("调整当前行程", routing_snapshot=snapshot)

    assert routed.requires_clarification is False
    assert len(classifier.calls) == 1
    context = classifier.calls[0]
    assert len(json.dumps(context, ensure_ascii=False).encode("utf-8")) <= router.MODEL_CONTEXT_BYTE_LIMIT
    assert context["state"]["compaction"] == ["segment_names_removed", "segments_capped_8"]
    assert len(context["state"]["references"]["segments"]) == 8


def test_low_margin_is_semantic_ambiguity_even_above_threshold() -> None:
    response = _v2_response(
        intent="continue_plan_expansion",
        scope="planning_root",
        confidence=0.93,
        target_kind="planning_root",
    )
    response["alternatives"] = [
        {
            "intent": "regenerate_from_scratch",
            "confidence": 0.87,
            "requestedScope": "full_task",
            "isQuestion": False,
            "isNegated": False,
            "targetReference": {"kind": "none"},
        }
    ]
    routed = ConversationIntentRouter(
        lite_classifier=RecordingClassifier(response),
        routing_mode="active-all",
    ).classify("换一个", routing_snapshot=_snapshot(phase="proposal_selection"))

    assert routed.requires_clarification is True
    assert routed.reason_code == "intent_semantic_ambiguity"
    assert routed.execution_disposition == "clarify"


def test_provider_not_invoked_is_reported_truthfully() -> None:
    outcome = ConversationIntentModelOutcome(
        value=_v2_response(
            intent="continue_plan_expansion",
            scope="planning_root",
            target_kind="planning_root",
        ),
        provider_invoked=False,
        performance_evidence=({"providerInvoked": False, "failureClass": "queue_saturated"},),
    )
    routed = ConversationIntentRouter(
        lite_classifier=lambda _context: outcome,
        routing_mode="active-all",
    ).classify("继续", routing_snapshot=_snapshot(phase="proposal_selection"))

    assert routed.model_attempted is True
    assert routed.model_called is False
    assert routed.model_succeeded is True
    assert routed.invocation_ledger["intentProviderCallCount"] == 0


def test_server_target_resolution_distinguishes_missing_ambiguous_and_unique() -> None:
    resolver = ConversationCapabilityResolver(sqlite3.connect(":memory:"))
    classification = ConversationIntentClassification(
        intent="modify_itinerary",
        confidence=0.96,
        requestedScope="active_itinerary",
        isQuestion=False,
        isNegated=False,
    )
    snapshot = IntentRoutingSnapshotV1(
        fingerprint="b" * 64,
        model_projection={},
        server_state={
            "hasActiveVersion": True,
            "activeVersionId": "server-version",
            "segmentReferences": [
                {
                    "kind": "time_window",
                    "segmentId": "segment-1",
                    "dayNumber": 1,
                    "timeBucket": "afternoon",
                    "name": "北海公园",
                },
                {
                    "kind": "time_window",
                    "segmentId": "segment-2",
                    "dayNumber": 1,
                    "timeBucket": "afternoon",
                    "name": "景山公园",
                },
            ],
        },
    )

    ambiguous = resolver.resolve_target(
        snapshot=snapshot,
        classification=classification,
        target_reference={"kind": "time_window", "dayNumber": 1, "timeBucket": "afternoon"},
    )
    unique = resolver.resolve_target(
        snapshot=snapshot,
        classification=classification,
        target_reference={
            "kind": "time_window",
            "dayNumber": 1,
            "timeBucket": "afternoon",
            "mentionText": "北海",
        },
    )
    missing = resolver.resolve_target(
        snapshot=snapshot,
        classification=classification,
        target_reference={"kind": "time_window", "dayNumber": 2, "timeBucket": "evening"},
    )

    assert ambiguous.status == "ambiguous"
    assert unique.status == "unique"
    assert unique.target["segmentId"] == "segment-1"
    assert missing.status == "none"


@pytest.mark.parametrize(
    "mode",
    ["legacy-only", "shadow", "active-read", "active-all", "kill-switch"],
)
def test_all_intent_routing_rollout_modes_are_configurable(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_INTENT_ROUTING_MODE", mode)
    get_settings.cache_clear()
    try:
        assert get_settings().agent_intent_routing_mode == mode
    finally:
        get_settings.cache_clear()


def test_default_entry_is_state_aware_with_explicit_legacy_rollback(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_INTENT_ROUTING_MODE", raising=False)
    get_settings.cache_clear()
    try:
        assert Settings().agent_intent_routing_mode == "active-all"
        assert get_settings().agent_intent_routing_mode == "active-all"
    finally:
        get_settings.cache_clear()

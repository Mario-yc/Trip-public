import copy
import json
import sqlite3
from typing import Optional

import pytest

from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_service import AgentService
from src.services.agent_action_directive import AskUserDirective
from src.services.conversation_service import ConversationService


REQUEST = (
    "今年国庆参观北京高校两日游，晚上看北京城市夜景。10月1日到2日，"
    "中等预算，1人，公交地铁优先。每天午餐想体验当地特色美食。"
)


class RecoveryClarificationProvider:
    """Controller fixture whose wording is model output, never a server fallback."""

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def decide_autonomy_lite(self, context, *, timeout_seconds):
        assert timeout_seconds > 0
        state = context.get("state") if isinstance(context.get("state"), dict) else {}
        message = str(context.get("message") or "")
        if state.get("hasUnansweredClarification"):
            intent = "clarification_answer"
            scope = "clarification"
        elif any(marker in message for marker in ("继续", "重试", "再试")):
            intent = "retry_current_stage"
            scope = "current_stage"
        else:
            intent = "create_itinerary"
            scope = "new_itinerary"
        return json.dumps(
            {
                "intent": intent,
                "confidence": 0.99,
                "requestedScope": scope,
                "isQuestion": False,
                "isNegated": False,
            },
            ensure_ascii=False,
        )

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        assert timeout_seconds > 0
        serialized = json.loads(json.dumps(dict(context), ensure_ascii=False))
        self.contexts.append(serialized)
        checkpoint = (
            serialized.get("clarificationCheckpoint")
            if isinstance(serialized.get("clarificationCheckpoint"), dict)
            else {}
        )
        answers = [item for item in checkpoint.get("resolvedAnswers") or [] if isinstance(item, dict)]
        if answers:
            directive = {
                "type": "ask_user",
                "question": "已确认夜间次数；下一轮真实地点发现应采用哪类公共体验边界？",
                "dimensionId": "night_view.experience_mode",
                "whyItMatters": "体验边界决定候选准入、开放证据和交通网络插入核验。",
                "allowFreeText": True,
                "options": [
                    {
                        "id": "public_outdoor",
                        "label": "开放公共空间优先",
                        "semanticValue": {
                            "experienceFamilies": [
                                "public_city_view",
                                "waterfront_evening",
                            ],
                            "accessPolicy": "public_outdoor",
                            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
                            "timeWindow": {"start": "18:30", "end": "22:00"},
                            "detourTolerance": {
                                "maxGeneralizedCostDelta": 35,
                                "maxDetourRatio": 0.35,
                            },
                            "evidenceFreshness": {
                                "maxAgeHours": 24,
                                "requiredForControlledAccess": True,
                            },
                            "confidence": 0.95,
                        },
                    },
                    {
                        "id": "verified_controlled",
                        "label": "允许有当期证据的受控入口",
                        "semanticValue": {
                            "experienceFamilies": ["verified_city_view"],
                            "accessPolicy": "verified_controlled_access",
                            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
                            "timeWindow": {"start": "18:30", "end": "21:30"},
                            "detourTolerance": {
                                "maxGeneralizedCostDelta": 30,
                                "maxDetourRatio": 0.3,
                            },
                            "evidenceFreshness": {
                                "maxAgeHours": 12,
                                "requiredForControlledAccess": True,
                            },
                            "confidence": 0.9,
                        },
                    },
                ],
            }
            supported_aliases = {field.alias or name for name, field in AskUserDirective.model_fields.items()}
            identity_sources = {
                "checkpointId": "checkpointId",
                "planningRootId": "planningRootId",
                "requestFingerprint": "requestFingerprint",
                "checkpointFingerprint": "fingerprint",
            }
            for output_key, checkpoint_key in identity_sources.items():
                if output_key in supported_aliases and checkpoint.get(checkpoint_key) is not None:
                    directive[output_key] = checkpoint[checkpoint_key]
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": directive,
            }
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "type": "ask_user",
                "question": "两天的夜间安排希望覆盖几个不同体验？",
                "dimensionId": "night_view.cardinality",
                "whyItMatters": "频次决定需要几个不同真实地点和几组路线证据。",
                "allowFreeText": True,
                "options": [
                    {
                        "id": "each_evening",
                        "label": "每个可用晚上各安排一次",
                        "semanticValue": {"occurrencePolicy": "every_available_evening"},
                    },
                    {
                        "id": "one_evening",
                        "label": "只安排其中一个晚上",
                        "semanticValue": {"occurrencePolicy": "one_evening"},
                    },
                ],
            },
        }

    def generate(self, context):
        raise AssertionError("clarification flow must not enter legacy generation")


def open_db() -> sqlite3.Connection:
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection


def interrupted_answer(
    connection: sqlite3.Connection,
    *,
    manual_value: Optional[str] = None,
):
    session = ConversationService(connection).create_session("北京", "澄清恢复")
    provider = RecoveryClarificationProvider()
    service = AgentService(connection, provider=provider)
    first = service.send_message(
        session.session_id,
        AgentMessageRequest(content=REQUEST),
    )
    option = (
        next(item for item in first.assistant_turn.choice_options if item.get("kind") == "custom_input")
        if manual_value is not None
        else first.assistant_turn.choice_options[0]
    )

    def interrupt_after_answer_persisted(**_kwargs):
        raise RuntimeError("controller_interrupted_after_user_envelope")

    service._run_coordinated_natural_language_turn = interrupt_after_answer_persisted
    with pytest.raises(
        RuntimeError,
        match="controller_interrupted_after_user_envelope",
    ):
        service.send_message(
            session.session_id,
            AgentMessageRequest(
                content=manual_value or "每晚各安排一次",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": first.assistant_turn.id,
                        "choiceId": option["id"],
                        **({"manualValue": manual_value} if manual_value is not None else {}),
                    }
                },
            ),
        )
    answer_row = connection.execute(
        """
        SELECT * FROM conversation_turns
        WHERE session_id = ? AND role = 'user'
        ORDER BY turn_index DESC LIMIT 1
        """,
        (session.session_id,),
    ).fetchone()
    return session, provider, first, option, answer_row


def test_controller_interrupt_recovers_latest_user_clarification_envelope():
    with open_db() as connection:
        session, _provider, first, option, answer_row = interrupted_answer(connection)
        persisted = json.loads(answer_row["agent_request_json"])
        assert persisted["clarificationCheckpoint"]["status"] == "awaiting_agent_resolution"

        loaded = ConversationService(connection).get_session(session.session_id)
        source_turn = next(turn for turn in loaded.turns if turn.id == first.assistant_turn.id)
        answer_turn = next(turn for turn in loaded.turns if turn.id == answer_row["id"])
        version_count = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]

        selected = next(item for item in source_turn.choice_options if item["id"] == option["id"])
        assert selected["lifecycle"] == "failed_retryable"
        assert all(
            item["lifecycle"] == "offered"
            for item in source_turn.choice_options
            if item.get("action") == "continue_clarification" and item["id"] != option["id"]
        )
        assert persisted["clarificationCheckpoint"]["resolvedAnswers"][-1]["semanticValue"] == {
            "occurrencePolicy": "every_available_evening"
        }
        assert source_turn.clarification_checkpoint["resolvedAnswers"] == []
        assert answer_turn.clarification_checkpoint is None
        assert version_count == 0

        resumed_provider = RecoveryClarificationProvider()
        resumed = AgentService(connection, provider=resumed_provider).send_message(
            session.session_id,
            AgentMessageRequest(
                content="每晚各安排一次",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": first.assistant_turn.id,
                        "choiceId": option["id"],
                    }
                },
            ),
        )
        resumed_request = json.loads(
            connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
                (resumed.user_turn.id,),
            ).fetchone()["agent_request_json"]
        )

        assert resumed.version is None
        assert resumed.assistant_turn.clarification_checkpoint is not None, {
            "content": resumed.assistant_turn.content,
            "choices": resumed.assistant_turn.choice_options,
            "contexts": resumed_provider.contexts,
        }
        assert (
            resumed.assistant_turn.clarification_checkpoint["checkpointId"]
            == (first.assistant_turn.clarification_checkpoint["checkpointId"])
        )
        assert resumed.assistant_turn.clarification_checkpoint["resolvedAnswers"][-1]["semanticValue"] == {
            "occurrencePolicy": "every_available_evening"
        }
        assert (
            resumed.assistant_turn.clarification_checkpoint["nextQuestionDimensionId"] == "night_view.experience_mode"
        ), {
            "content": resumed.assistant_turn.content,
            "choices": resumed.assistant_turn.choice_options,
            "providerDecisionCount": len(resumed_provider.contexts),
            "controllerCheckpointStatus": (
                resumed_provider.contexts[0].get("clarificationCheckpoint", {}).get("status")
            ),
            "dimensions": (resumed_request.get("requestIntentContract", {}).get("clarificationDimensions")),
        }
        assert resumed_provider.contexts[0]["clarificationCheckpoint"]["status"] == "awaiting_agent_resolution"
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert (
            ConversationService(connection).latest_clarification_recovery_envelope(
                session.session_id,
                unmaterialized_only=True,
            )
            is None
        )


def test_controller_interrupt_recovers_pending_free_text_envelope():
    with open_db() as connection:
        manual_value = "两个晚上都安排，但地点要不同"
        session, provider, first, option, answer_row = interrupted_answer(
            connection,
            manual_value=manual_value,
        )
        persisted = json.loads(answer_row["agent_request_json"])
        persisted_checkpoint = persisted["clarificationCheckpoint"]

        assert persisted_checkpoint["status"] == "awaiting_agent_resolution"
        assert persisted_checkpoint["resolvedAnswers"] == []
        assert persisted_checkpoint["pendingFreeTextAnswer"] == {
            "dimensionId": "night_view.cardinality",
            "source": "free_text",
            "sourceUserTurnId": answer_row["id"],
            "text": manual_value,
        }

        conversation_service = ConversationService(connection)
        recovered = conversation_service.latest_clarification_recovery_envelope(
            session.session_id,
            unmaterialized_only=True,
        )
        assert recovered is not None
        recovered_context = recovered["requestContext"]
        assert recovered_context["clarificationCheckpoint"] == persisted_checkpoint
        assert recovered_context["requestIntentContract"] == persisted["requestIntentContract"]
        assert recovered_context["clarificationFreeTextCandidate"] == persisted["clarificationFreeTextCandidate"]
        assert recovered_context["canonicalRequestContext"]["clarificationCheckpoint"] == persisted_checkpoint

        loaded = conversation_service.get_session(session.session_id)
        source_turn = next(turn for turn in loaded.turns if turn.id == first.assistant_turn.id)
        answer_turn = next(turn for turn in loaded.turns if turn.id == answer_row["id"])
        manual_after = next(item for item in source_turn.choice_options if item["id"] == option["id"])

        assert manual_after["lifecycle"] == "consumed"
        assert all(
            item["lifecycle"] in {"consumed", "stale"}
            for item in source_turn.choice_options
            if item.get("action") == "continue_clarification"
        )
        assert source_turn.clarification_checkpoint == persisted_checkpoint
        assert answer_turn.clarification_checkpoint == persisted_checkpoint

        restored_context = {
            "latestUserMessage": "继续当前澄清",
            "activeConversationTurns": [{"role": "user"}, {"role": "assistant"}],
        }
        AgentService(connection, provider=provider)._restore_request_intent_contract(
            session.session_id,
            restored_context,
        )

        assert restored_context["goalLedgerSource"] == "persisted_clarification_user_envelope"
        assert restored_context["planningSelectionRootTurnId"] == persisted["planningSelectionRootTurnId"]
        assert restored_context["requestIntentContract"] == persisted["requestIntentContract"]
        assert restored_context["clarificationCheckpoint"] == persisted_checkpoint
        assert restored_context["canonicalRequestContext"]["clarificationCheckpoint"] == persisted_checkpoint
        assert (
            restored_context["canonicalRequestContext"]["requestIntentContract"] == persisted["requestIntentContract"]
        )
        if "candidateGapSummary" in persisted:
            assert restored_context["candidateGapSummary"] == persisted["candidateGapSummary"]
            assert (
                restored_context["canonicalRequestContext"]["candidateGapSummary"] == persisted["candidateGapSummary"]
            )
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0

        split_pending = copy.deepcopy(persisted)
        split_pending["clarificationFreeTextCandidate"]["text"] = "只安排一个晚上"
        injected_diagnostic = copy.deepcopy(persisted)
        injected_diagnostic["canonicalRequestContext"]["clarificationCheckpoint"] = {
            "diagnostic": "controller_interrupted"
        }
        for payload in (split_pending, injected_diagnostic):
            connection.execute(
                "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), answer_row["id"]),
            )
            connection.commit()
            assert conversation_service.latest_clarification_recovery_envelope(session.session_id) is None


def test_clarification_user_recovery_envelope_rejects_identity_or_canonical_drift():
    with open_db() as connection:
        session, _provider, _first, _option, answer_row = interrupted_answer(
            connection,
            manual_value="两个晚上都安排，但地点要不同",
        )
        valid = json.loads(answer_row["agent_request_json"])
        service = ConversationService(connection)

        recovered = service.latest_clarification_recovery_envelope(session.session_id)
        assert recovered is not None
        assert recovered["sourceUserTurnId"] == answer_row["id"]

        tampered_payloads = []
        wrong_root = copy.deepcopy(valid)
        wrong_root["planningSelectionRootTurnId"] = "turn_other_root"
        tampered_payloads.append(wrong_root)

        wrong_checkpoint_fingerprint = copy.deepcopy(valid)
        wrong_checkpoint_fingerprint["clarificationCheckpoint"]["pendingFreeTextAnswer"]["text"] = "只安排一个晚上"
        tampered_payloads.append(wrong_checkpoint_fingerprint)

        split_canonical = copy.deepcopy(valid)
        split_canonical["canonicalRequestContext"]["requestIntentContract"] = {"requiredIntents": []}
        tampered_payloads.append(split_canonical)

        for payload in tampered_payloads:
            connection.execute(
                "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), answer_row["id"]),
            )
            connection.commit()
            assert service.latest_clarification_recovery_envelope(session.session_id) is None

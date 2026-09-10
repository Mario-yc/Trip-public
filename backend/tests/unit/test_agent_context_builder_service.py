import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import pytest
from fastapi import HTTPException

from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.agent_context_builder_service import AgentContextBuilder, SUPPORTED_AGENT_PATCH_OPS
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.preference_service import PreferenceService


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM agent_choice_executions;
            DELETE FROM agent_plan_proposals;
            DELETE FROM agent_plan_portfolios;
            DELETE FROM planning_runs;
            DELETE FROM amap_poi_candidates;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM weather_signals;
            DELETE FROM route_options;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            DELETE FROM session_preference_memories;
            DELETE FROM travel_preference_memories;
            """
        )


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def test_plan_portfolio_state_ignores_superseded_source_assistant():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        service = AgentService(connection)
        user_turn_id = service._insert_turn(session.session_id, "user", "北京两日游", "active")
        assistant_turn_id = service._insert_turn(
            session.session_id,
            "assistant",
            "没有可用方案",
            "superseded",
        )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO agent_plan_portfolios (
                id, session_id, source_user_turn_id, source_assistant_turn_id,
                expected_base_version_id, source_observation_fingerprint,
                request_contract_fingerprint, status, selected_proposal_id,
                dominant_proposal_id, summary_json, failure_reason, expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'failed', NULL, NULL, '{}', ?, NULL, ?, ?)
            """,
            (
                "portfolio_superseded",
                session.session_id,
                user_turn_id,
                assistant_turn_id,
                "o" * 64,
                "r" * 64,
                "portfolio_anchor_target_shortfall:day_1:0/1",
                now,
                now,
            ),
        )
        connection.commit()

        state = AgentContextBuilder(connection)._plan_portfolio_state(session.session_id)

    assert state == {"status": "none"}


def test_plan_portfolio_state_ignores_superseded_source_user_without_assistant():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        service = AgentService(connection)
        user_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "北京两日游",
            "superseded",
        )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO agent_plan_portfolios (
                id, session_id, source_user_turn_id, source_assistant_turn_id,
                expected_base_version_id, source_observation_fingerprint,
                request_contract_fingerprint, status, selected_proposal_id,
                dominant_proposal_id, summary_json, failure_reason, expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, 'failed', NULL, NULL, '{}', ?, NULL, ?, ?)
            """,
            (
                "portfolio_superseded_user",
                session.session_id,
                user_turn_id,
                "o" * 64,
                "r" * 64,
                "portfolio_anchor_target_shortfall:day_1:0/1",
                now,
                now,
            ),
        )
        connection.commit()

        state = AgentContextBuilder(connection)._plan_portfolio_state(session.session_id)
        audit_row = connection.execute(
            "SELECT id FROM agent_plan_portfolios WHERE id = ?",
            ("portfolio_superseded_user",),
        ).fetchone()

    assert state == {"status": "none"}
    assert audit_row is not None


class FakePlannerService:
    def plan(self, latest_message: str, request_context: dict) -> dict:
        return {
            "plannerVersion": "unit-test-planner",
            "latestUserMessage": latest_message,
            "hasMemory": bool(request_context["memoryText"]),
            "riskControls": ["amap_poi_grounding"],
        }


class FakeSkillService:
    def select_skills(self, latest_message: str, request_context: dict) -> list[dict]:
        return [{"name": "amap_poi_grounding", "score": 3}]

    def build_skill_context(self, selected_skills: list[dict]) -> str:
        return "AMap POI Grounding: final POIs must be resolved before writes."


def requirement_analyzer(
    latest_message: str, city: str, current_snapshot: Optional[dict], preference_summary: str
) -> dict:
    return {
        "summary": f"{city}:{latest_message}",
        "missingFields": [],
        "clarificationQuestions": ["是否需要安排餐厅？"],
        "isCompleteEnoughToPlan": True,
        "hasSnapshot": current_snapshot is not None,
        "preferenceSummary": preference_summary,
    }


def preference_impact(preference_summary: str) -> dict:
    return {"hasPreferenceSummary": bool(preference_summary), "appliedRules": ["pace"]}


def test_agent_context_restores_active_version_portfolio_metadata():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "Portfolio 续跑")
        snapshot_service = ItinerarySnapshotService(connection)
        snapshot = snapshot_service.capture_snapshot(session.active_plan_id)
        snapshot.update(
            {
                "portfolioPartialTimeline": {"status": "partial", "pendingSlotCount": 2},
                "portfolioPendingSlots": [
                    {"planningSlotId": "meal_day_1"},
                    {"planningSlotId": "meal_day_2"},
                ],
                "portfolioSelectionContext": {
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                    "focusBriefId": "brief_campus",
                    "requestContractFingerprint": "fp_request",
                },
                "creativeBrief": {"briefId": "brief_campus", "title": "高校夜景"},
                "portfolioDayAnchorTargets": {"1": 3, "2": 3},
                "portfolioScheduleProjection": {"status": "verified"},
            }
        )
        version = snapshot_service.save_version(
            session.session_id,
            session.active_plan_id,
            "portfolio_partial",
            snapshot=snapshot,
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()

        context = AgentContextBuilder(connection).build(
            row,
            "继续生成其他方案",
            AgentMessageRequest(content="继续生成其他方案", context={}),
        )

    current = context["currentItinerarySnapshot"]
    assert current["activeVersionId"] == version.id
    assert current["portfolioPartialTimeline"]["status"] == "partial"
    assert [item["planningSlotId"] for item in current["portfolioPendingSlots"]] == [
        "meal_day_1",
        "meal_day_2",
    ]
    assert current["portfolioSelectionContext"] == snapshot["portfolioSelectionContext"]
    assert current["creativeBrief"] == snapshot["creativeBrief"]
    assert context["timelineContext"] == current


@pytest.mark.parametrize(
    ("message", "raw_choice", "expected_rebind"),
    [
        ("继续生成其他方案", None, True),
        ("继续新增其他方案", None, True),
        ("多给几个方案", None, True),
        (
            "继续新增一个方案",
            {
                "sourceAssistantTurnId": "turn_more_plans_assistant",
                "choiceId": "portfolio_manual_portfolio_root",
                "manualValue": "继续新增一个方案",
            },
            True,
        ),
        ("不要继续生成其他方案", None, False),
        ("请不要“继续生成其他方案”", None, False),
        (
            "“继续生成其他方案”",
            {
                "sourceAssistantTurnId": "turn_more_plans_assistant",
                "choiceId": "portfolio_manual_portfolio_root",
                "manualValue": "“继续生成其他方案”",
            },
            False,
        ),
        (
            "“继续生成其他方案”",
            {
                "sourceAssistantTurnId": "turn_missing_assistant",
                "choiceId": "portfolio_manual_missing",
                "manualValue": "“继续生成其他方案”",
            },
            False,
        ),
        (
            "请问为什么继续生成其他方案",
            {
                "sourceAssistantTurnId": "turn_more_plans_assistant",
                "choiceId": "portfolio_manual_portfolio_root",
            },
            False,
        ),
        (
            "继续生成其他方案失败",
            {
                "sourceAssistantTurnId": "turn_more_plans_assistant",
                "choiceId": "portfolio_manual_consumed",
                "manualValue": "继续生成其他方案失败",
            },
            False,
        ),
        ("继续生成其他方案失败", None, False),
        ("请问为什么继续生成方案", None, False),
        ("我不想生成其他方案", None, False),
    ],
)
def test_agent_context_rebinds_explicit_more_plan_request_to_persisted_capability(
    message: str,
    raw_choice: Optional[dict],
    expected_rebind: bool,
) -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "继续生成第三个方案")
        snapshot_service = ItinerarySnapshotService(connection)
        snapshot = snapshot_service.capture_snapshot(session.active_plan_id)
        snapshot.update(
            {
                "portfolioPartialTimeline": {
                    "status": "partial",
                    "pendingSlotCount": 1,
                },
                "portfolioPendingSlots": [
                    {
                        "creativeBriefId": "fallback_1_culture_deep_dive",
                        "poolId": "pool_meal",
                        "planningSlotId": "slot_meal",
                        "dayNumber": 1,
                    }
                ],
                "portfolioSelectionContext": {
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                    "focusBriefId": "fallback_1_culture_deep_dive",
                    "requestContractFingerprint": "f" * 64,
                },
            }
        )
        version = snapshot_service.save_version(
            session.session_id,
            session.active_plan_id,
            "portfolio_partial",
            snapshot=snapshot,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_more_plans_user",
            role="user",
            content="北京高校两日游",
            turn_index=1,
        )
        more_plans_option = {
            "id": "portfolio_more_plans_portfolio_root_1",
            "action": "retry_model_planning",
            "kind": "portfolio_more_plans",
            "label": "label is not parsed",
            "expectedBaseVersionId": version.id,
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": "portfolio_root",
            "requestContractFingerprint": "f" * 64,
            "focusBriefId": "fallback_3_food_led",
            "mode": "exact",
        }
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_more_plans_assistant",
            role="assistant",
            content="已有两个方案，还可以继续生成。",
            turn_index=2,
            agent_response_json={
                "choiceOptions": [
                    more_plans_option,
                    {
                        "id": "portfolio_manual_portfolio_root",
                        "action": "manual_continuation",
                        "kind": "custom_input",
                        "label": "告诉我你想调整的方向",
                        "expectedBaseVersionId": version.id,
                    },
                    {
                        "id": "portfolio_manual_consumed",
                        "action": "manual_continuation",
                        "kind": "custom_input",
                        "label": "已处理的补充输入",
                        "expectedBaseVersionId": version.id,
                    },
                ],
                "consumedChoiceIds": ["portfolio_manual_consumed"],
            },
        )
        expected_turn_id = "turn_more_plans_assistant"
        expected_option = more_plans_option
        if raw_choice is not None:
            expected_turn_id = "turn_latest_more_plans_assistant"
            expected_option = {
                **more_plans_option,
                "id": "portfolio_more_plans_portfolio_root_2",
                "focusBriefId": "fallback_4_relaxed_landmarks",
            }
            insert_context_turn(
                connection,
                session_id=session.session_id,
                turn_id=expected_turn_id,
                role="assistant",
                content="已追加一个方案，还可以继续生成。",
                turn_index=3,
                agent_response_json={"choiceOptions": [expected_option]},
            )
        rejected_user_turn_id = None
        if raw_choice is not None and not expected_rebind:
            rejected_user_turn_id = "turn_rejected_expansion_user"
            insert_context_turn(
                connection,
                session_id=session.session_id,
                turn_id=rejected_user_turn_id,
                role="user",
                content=message,
                turn_index=4,
            )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        payload = AgentMessageRequest(
            content=message,
            context=({"selectedAgentChoice": raw_choice} if raw_choice is not None else {}),
        )

        if rejected_user_turn_id:
            context = AgentService(connection)._build_request_context(
                row,
                message,
                payload,
                current_user_turn_id=rejected_user_turn_id,
                defer_agent_decision=True,
            )
            rejected_execution_count = connection.execute(
                "SELECT COUNT(*) FROM agent_choice_executions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
        else:
            context = AgentContextBuilder(connection).build(row, message, payload)
            rejected_execution_count = 0

    selected = context["selectedAgentChoice"]
    if not expected_rebind:
        assert selected is None
        assert rejected_execution_count == 0
        assert context["retryIntent"]["intentClass"] == "not_retry"
        return
    assert selected["sourceAssistantTurnId"] == expected_turn_id
    assert selected["choiceId"] == expected_option["id"]
    assert selected["action"] == "retry_model_planning"
    assert selected["option"]["kind"] == "portfolio_more_plans"
    assert context["retryIntent"]["intentClass"] == "continue_plan_expansion"


def test_retry_current_stage_binds_only_server_eligible_portfolio_continuation() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "route degraded retry capability")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_route_degraded_user",
            role="user",
            content="北京高校两日游，晚上看夜景",
            turn_index=1,
        )
        option = {
            "id": "portfolio_more_plans_route_degraded_1",
            "action": "retry_model_planning",
            "kind": "portfolio_more_plans",
            "label": "label is not parsed",
            "expectedBaseVersionId": None,
            "planningSelectionRootTurnId": "turn_route_degraded_user",
            "rootPortfolioId": "portfolio_route_degraded",
            "requestContractFingerprint": "f" * 64,
            "focusBriefId": "fallback_2_local_immersion",
            "expansionFocusMode": "exact",
            "retryCurrentStageEligible": True,
        }
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_route_degraded_assistant",
            role="assistant",
            content="首方向路线质量未通过，仍可继续下一个方向。",
            turn_index=2,
            agent_response_json={"choiceOptions": [option]},
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        context = AgentContextBuilder(connection).build(
            row,
            "重试",
            AgentMessageRequest(content="重试"),
            conversation_intent_route={
                "source": "deterministic_fast_path",
                "classification": {
                    "intent": "retry_current_stage",
                    "confidence": 0.99,
                    "requestedScope": "current_stage",
                    "isQuestion": False,
                    "isNegated": False,
                },
            },
        )

        selected = context["selectedAgentChoice"]
        assert selected["sourceAssistantTurnId"] == "turn_route_degraded_assistant"
        assert selected["choiceId"] == option["id"]
        assert selected["option"]["focusBriefId"] == "fallback_2_local_immersion"
        assert context["resumePlanningAttempt"] == {"enabled": False}

        option["retryCurrentStageEligible"] = False
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps({"choiceOptions": [option]}), "turn_route_degraded_assistant"),
        )
        connection.commit()
        unbound = AgentContextBuilder(connection).build(
            row,
            "重试",
            AgentMessageRequest(content="重试"),
            conversation_intent_route={
                "source": "deterministic_fast_path",
                "classification": {
                    "intent": "retry_current_stage",
                    "confidence": 0.99,
                    "requestedScope": "current_stage",
                    "isQuestion": False,
                    "isNegated": False,
                },
            },
        )
        assert unbound["selectedAgentChoice"] is None


def insert_context_turn(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    turn_index: int,
    agent_request_json: Optional[dict] = None,
    agent_response_json: Optional[dict] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO conversation_turns (
            id, session_id, role, content, turn_index, status,
            parent_turn_id, itinerary_version_id, agent_request_json,
            agent_response_json, error_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            session_id,
            role,
            content,
            turn_index,
            "active",
            None,
            None,
            json.dumps(agent_request_json, ensure_ascii=False) if agent_request_json is not None else None,
            json.dumps(agent_response_json, ensure_ascii=False) if agent_response_json is not None else None,
            None,
            now,
            now,
        ),
    )


def test_agent_context_builder_includes_active_turns_memory_ui_context_and_skills():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_active",
                session.session_id,
                "user",
                "想去故宫",
                1,
                "active",
                None,
                "ver_1",
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_superseded",
                session.session_id,
                "assistant",
                "旧回复",
                2,
                "superseded",
                None,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        PreferenceService(connection).update_memory(
            "# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n",
            auto_update_enabled=False,
            session_id=session.session_id,
            commit=False,
        )
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="帮我安排北京一天",
            context={
                "selectedDayNumber": 1,
                "selectedSegmentId": "seg_1",
                "selectedMapPoi": {"id": "B000PALACE", "name": "故宫博物院"},
                "candidateMapPois": [{"id": "B000PALACE", "name": "故宫博物院"}],
                "timelineContext": {"id": session.active_plan_id, "days": []},
                "preferenceCard": {
                    "id": "card_1",
                    "profileId": "profile_1",
                    "partySize": 2,
                    "travelerTypes": ["adult"],
                    "summaryText": "# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n",
                    "status": "draft",
                },
            },
        )

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "帮我安排北京一天", payload)

    assert [turn["content"] for turn in context["activeConversationTurns"]] == ["想去故宫"]
    assert "喜欢轻松不赶路" in context["memoryText"]
    assert context["travelPreferenceMemory"]["autoUpdateEnabled"] is False
    assert context["timelineContext"] == {"id": session.active_plan_id, "days": []}
    assert "selectedMapPoi" not in context
    assert "selectedMapPoiId" not in context
    assert context["candidateMapPois"][0]["id"] == "B000PALACE"
    assert "pendingPoiCandidateId" not in context
    assert context["preferenceCard"] == {
        "id": "card_1",
        "profileId": "profile_1",
        "summaryText": "旅行节奏：喜欢轻松不赶路。",
        "status": "draft",
    }
    assert "partySize" not in context["preferenceCard"]
    assert "travelerTypes" not in context["preferenceCard"]
    assert context["activeDay"] == 1
    assert context["activeSegment"] == "seg_1"
    assert context["supportedPatchOperations"] == SUPPORTED_AGENT_PATCH_OPS
    assert context["unresolvedQuestions"] == ["是否需要安排餐厅？"]
    assert context["preferenceImpact"]["hasPreferenceSummary"] is True
    assert context["preferenceImpact"]["compiledRulesApplied"] is True
    assert context["preferenceImpact"]["pace"]["maxVisitSegmentsPerDay"] == 3
    assert context["memoryRules"]["mealHandling"]["pureMealLabelsAreNotPois"] is True
    assert context["travelPreferenceMemory"]["structuredMemory"]["facts"]
    assert "agentPlan" not in context
    assert "agentPlan" not in context["understoodRequirements"]
    assert "selectedSkills" not in context
    assert "skillContext" not in context


def test_agent_context_builder_uses_effective_message_from_resumable_attempt_for_short_retry():
    clear_database()
    original_message = (
        "今年国庆参观北京高校两日游，晚上看北京夜景。10月1日到2日，2天，中等预算，1人，公交地铁优先。想体验当地特色美食"
    )
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_user_original",
                session.session_id,
                "user",
                original_message,
                1,
                "active",
                None,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_assistant_waiting",
                session.session_id,
                "assistant",
                "地图服务暂时限流，本轮没有写入正式时间轴。",
                2,
                "active",
                None,
                None,
                json.dumps({"latestUserMessage": original_message}, ensure_ascii=False),
                json.dumps(
                    {
                        "mode": "staged_initial_pipeline_waiting",
                        "resultState": "provider_rate_limited",
                        "initialPlan": {"reply": "", "mode": "plan", "daySlots": [], "intentPools": []},
                        "pipelineContext": {"latestUserMessage": original_message},
                        "planningPreview": {"days": []},
                        "unresolvedSlots": [{"slotId": "day1_morning", "reason": "provider_rate_limited"}],
                    },
                    ensure_ascii=False,
                ),
                None,
                now,
                now,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "重新构建", AgentMessageRequest(content="重新构建"))

    assert context["latestUserMessage"] == "重新构建"
    assert context["effectiveUserMessage"] == original_message
    assert context["latestMessageIsRetryIntent"] is True
    assert context["resumePlanningAttempt"]["enabled"] is True
    assert context["resumePlanningAttempt"]["sourceAssistantTurnId"] == "turn_assistant_waiting"
    assert context["understoodRequirements"]["summary"] == f"北京:{original_message}"
    assert "agentPlan" not in context


def test_agent_context_builder_uses_contextual_retry_effective_message_and_retry_hints():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user_original",
            role="user",
            content=original_message,
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant_waiting_contextual",
            role="assistant",
            content="候选语义提示缺失，尚未创建正式时间轴。",
            turn_index=2,
            agent_request_json={"latestUserMessage": original_message},
            agent_response_json={
                "mode": "staged_initial_pipeline_waiting",
                "resultState": "semantic_candidate_hint_missing",
                "initialPlan": {"reply": "", "mode": "day_slots", "daySlots": [], "intentPools": []},
                "pipelineContext": {"latestUserMessage": original_message, "effectiveUserMessage": original_message},
                "planningPreview": {"days": []},
                "unresolvedSlots": [{"slotId": "day1_morning", "reason": "semantic_candidate_hint_missing"}],
                "nextActions": ["retry_candidate_hints"],
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "还是上面要求，重新生成一遍", AgentMessageRequest(content="还是上面要求，重新生成一遍"))

    assert context["latestUserMessage"] == "还是上面要求，重新生成一遍"
    assert context["effectiveUserMessage"] == original_message
    assert context["latestMessageIsRetryIntent"] is True
    assert context["retryCandidateHints"] is True
    assert context["conversationIntentContext"]["ignoredRetryMessage"] == "还是上面要求，重新生成一遍"
    assert context["conversationIntentContext"]["sourceAssistantTurnIndex"] == 2
    assert context["conversationIntentContext"]["sourceUserTurnIndex"] == 1
    assert context["resumePlanningAttempt"]["enabled"] is True


def test_agent_context_builder_falls_back_to_latest_complete_request_for_contextual_retry():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user_original",
            role="user",
            content=original_message,
            turn_index=1,
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "按刚才那个重新来", AgentMessageRequest(content="按刚才那个重新来"))

    assert context["latestUserMessage"] == "按刚才那个重新来"
    assert context["effectiveUserMessage"] == original_message
    assert context["resumePlanningAttempt"] == {"enabled": False}
    assert context["regeneratePlanningRequest"]["enabled"] is True
    assert context["regeneratePlanningRequest"]["sourceUserTurnId"] == "turn_user_original"
    assert context["conversationIntentContext"]["sourceUserTurnIndex"] == 1
    assert "agentPlan" not in context


def test_agent_context_builder_does_not_resume_old_attempt_when_session_has_active_version():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，国庆出发，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_user_old",
                session.session_id,
                "user",
                original_message,
                1,
                "active",
                None,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_assistant_old_waiting",
                session.session_id,
                "assistant",
                "地图服务暂时限流，本轮没有写入正式时间轴。",
                2,
                "active",
                None,
                None,
                json.dumps({"latestUserMessage": original_message}, ensure_ascii=False),
                json.dumps(
                    {
                        "mode": "staged_initial_pipeline_waiting",
                        "resultState": "provider_rate_limited",
                        "initialPlan": {"reply": "", "mode": "plan", "daySlots": [], "intentPools": []},
                        "pipelineContext": {"latestUserMessage": original_message},
                        "planningPreview": {"days": []},
                        "unresolvedSlots": [{"slotId": "day1_morning", "reason": "provider_rate_limited"}],
                    },
                    ensure_ascii=False,
                ),
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
            ("ver_existing", session.session_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "重新构建", AgentMessageRequest(content="重新构建"))

    assert context["latestUserMessage"] == "重新构建"
    assert context["effectiveUserMessage"] == "重新构建"
    assert context["latestMessageIsRetryIntent"] is True
    assert context["resumePlanningAttempt"]["enabled"] is False
    assert context["understoodRequirements"]["summary"] == "北京:重新构建"


def test_agent_context_builder_regenerates_from_active_version_complete_request():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_user_version",
                session.session_id,
                "user",
                original_message,
                1,
                "active",
                None,
                "ver_existing",
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
            ("ver_existing", session.session_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "重新生成", AgentMessageRequest(content="重新生成"))

    assert context["latestUserMessage"] == "重新生成"
    assert context["effectiveUserMessage"] == original_message
    assert context["resumePlanningAttempt"]["enabled"] is False
    assert context["regeneratePlanningRequest"]["enabled"] is True
    assert context["conversationIntentContext"]["source"] == "latest_complete_user_request_for_regenerate"
    assert context["understoodRequirements"]["summary"] == f"北京:{original_message}"
    assert "agentPlan" not in context


def test_agent_context_builder_regenerates_from_latest_complete_request_after_active_version_moves():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_user_initial_version",
                session.session_id,
                "user",
                original_message,
                1,
                "active",
                None,
                "ver_initial",
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                id, session_id, role, content, turn_index, status,
                parent_turn_id, itinerary_version_id, agent_request_json,
                agent_response_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn_user_minor_edit",
                session.session_id,
                "user",
                "把第一天路线改顺一点",
                3,
                "active",
                None,
                "ver_manual_edit",
                None,
                None,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
            ("ver_manual_edit", session.session_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(
            connection,
            planner_service=FakePlannerService(),
            skill_service=FakeSkillService(),
            requirement_analyzer=requirement_analyzer,
            preference_impact_analyzer=preference_impact,
        ).build(row, "重新规划", AgentMessageRequest(content="重新规划"))

    assert context["effectiveUserMessage"] == original_message
    assert context["regeneratePlanningRequest"]["enabled"] is True
    assert context["regeneratePlanningRequest"]["sourceActiveVersionId"] == "ver_manual_edit"
    assert context["regeneratePlanningRequest"]["sourceUserTurnId"] == "turn_user_initial_version"
    assert context["conversationIntentContext"]["source"] == "latest_complete_user_request_for_regenerate"


def test_agent_context_builder_filters_empty_preference_templates():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        PreferenceService(connection).update_memory(
            "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n",
            auto_update_enabled=True,
            session_id=session.session_id,
            commit=False,
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="帮我安排北京一天",
            context={
                "currentPreferenceSummary": "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n",
                "preferenceCard": {
                    "id": "card_1",
                    "profileId": "profile_1",
                    "partySize": 2,
                    "travelerTypes": ["adult"],
                    "summaryText": "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n",
                    "status": "draft",
                },
            },
        )

        context = AgentContextBuilder(connection).build(row, "帮我安排北京一天", payload)

    assert context["currentPreferenceSummary"] == ""
    assert context["memoryText"] == ""
    assert context["preferenceCard"] is None
    assert context["travelPreferenceMemory"]["memoryText"] == ""
    assert context["memoryRules"]["mealHandling"]["pureMealLabelsAreNotPois"] is True


def test_agent_context_builder_includes_planning_quality_contract_for_relaxed_requests():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="帮我安排北京两天，轻松不赶路",
            context={
                "currentPreferenceSummary": "旅行节奏：喜欢轻松不赶路。",
            },
        )

        context = AgentContextBuilder(connection).build(row, "帮我安排北京两天，轻松不赶路", payload)

    contract = context["planningQualityContract"]
    assert contract["version"] == "planning-quality-p1-v1"
    assert contract["dailyStructure"]["requiredAnchors"] == ["morning", "noon", "evening"]
    assert contract["paceRules"]["relaxedRequested"] is True
    assert contract["paceRules"]["maxVisitSegmentsPerDay"] == 3
    assert contract["segmentRequirements"]["pureMealLabelsAreNotPois"] is True
    assert contract["memoryExecutionRules"]["routePlanning"]["excludedSegmentKinds"] == [
        "meal",
        "rest",
        "note",
        "transport",
        "buffer",
    ]
    assert "missing_practical_notes" in contract["verifierSoftChecks"]
    assert "agentPlan" not in context


def test_agent_context_builder_compiles_memory_into_risk_rules():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        PreferenceService(connection).update_memory(
            memory_text=(
                "# 我的旅行偏好\n\n"
                "## 兴趣偏好\n- 拍照打卡优先。\n\n"
                "## 同行与特殊需求\n- 同行中有老人，需控制步行和排队强度。\n"
            ),
            session_id=session.session_id,
            commit=False,
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(content="帮我安排北京一天", context={})

        context = AgentContextBuilder(connection).build(row, "帮我安排北京一天", payload)

    risk_rules = context["planningQualityContract"]["memoryExecutionRules"]["riskChecks"]
    assert risk_rules["checkCrowdingWeather"] is True
    assert risk_rules["travelerSensitivity"] == "high"
    assert "步行强度" in risk_rules["priorityTerms"]
    assert context["preferenceImpact"]["riskChecks"] == risk_rules


def test_selected_agent_choice_is_resolved_from_source_turn_and_consumed_once():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "选择会话")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user",
            role="user",
            content="北京两日游",
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant",
            role="assistant",
            content="选天数",
            turn_index=2,
            agent_response_json={"choiceOptions": [{"id": "choice_two_days", "index": 2, "value": "2 天", "label": "展示文案"}]},
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="选择 Agent 选项",
            context={"selectedAgentChoice": {"sourceAssistantTurnId": "turn_assistant", "choiceId": "choice_two_days"}},
        )
        context = AgentContextBuilder(connection).build(row, payload.content, payload)
        connection.commit()

        assert context["selectedAgentChoice"]["option"]["value"] == "2 天"
        assert context["effectiveUserMessage"] == "北京两日游\n2 天"
        persisted = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = 'turn_assistant'"
            ).fetchone()[0]
        )
        assert "consumedChoiceIds" not in persisted


def test_consumed_density_choice_is_resolved_for_idempotent_replay() -> None:
    clear_database()
    option = {
        "id": "density_choice_replay",
        "action": "resume_density_candidate",
        "kind": "portfolio_density_candidate",
        "briefId": "brief_local",
        "poolId": "pool_meal",
        "planningSlotId": "slot_meal",
        "dayNumber": 1,
        "intentType": "meal",
    }
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "密度选择幂等重放")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_density_user",
            role="user",
            content="北京两日游",
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_density_assistant",
            role="assistant",
            content="请选择缺失餐饮",
            turn_index=2,
            agent_response_json={
                "choiceOptions": [option],
                "consumedChoiceIds": [option["id"]],
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "turn_density_assistant",
                    "choiceId": option["id"],
                }
            },
        )

        context = AgentContextBuilder(connection).build(row, payload.content, payload)

    assert context["selectedAgentChoice"]["action"] == "resume_density_candidate"
    assert context["selectedAgentChoice"]["option"]["planningSlotId"] == "slot_meal"


def test_consumed_legacy_density_choice_is_resolved_by_persisted_kind() -> None:
    clear_database()
    option = {
        "id": "legacy_density_choice_replay",
        "action": "manual_continuation",
        "kind": "portfolio_density_candidate",
        "briefId": "brief_local",
        "poolId": "pool_meal",
        "planningSlotId": "slot_meal",
        "dayNumber": 1,
        "intentType": "meal",
    }
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "历史密度选择幂等重放")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_legacy_density_user",
            role="user",
            content="北京两日游",
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_legacy_density_assistant",
            role="assistant",
            content="请选择缺失餐饮",
            turn_index=2,
            agent_response_json={
                "choiceOptions": [option],
                "consumedChoiceIds": [option["id"]],
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "turn_legacy_density_assistant",
                    "choiceId": option["id"],
                }
            },
        )

        context = AgentContextBuilder(connection).build(row, payload.content, payload)

    assert context["selectedAgentChoice"]["action"] == "manual_continuation"
    assert context["selectedAgentChoice"]["option"]["kind"] == "portfolio_density_candidate"


def test_context_builder_keeps_original_date_when_user_answers_clarification_in_free_text():
    clear_database()
    original_message = "10月1日北京一日游，1人，中等预算，公交地铁优先，参观故宫博物院。"
    answer = "轻松参观，保留故宫博物院，上午开始。"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "澄清会话")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user",
            role="user",
            content=original_message,
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant",
            role="assistant",
            content="更想怎样游览？",
            turn_index=2,
            agent_request_json={"latestUserMessage": original_message, "effectiveUserMessage": original_message},
            agent_response_json={"mode": "clarification", "terminalStatus": "needs_confirmation"},
        )
        insert_context_turn(
            connection, session_id=session.session_id, turn_id="turn_answer", role="user", content=answer, turn_index=3
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()

        context = AgentContextBuilder(connection).build(row, answer, AgentMessageRequest(content=answer, context={}))

    assert context["latestUserMessage"] == answer
    assert context["effectiveUserMessage"] == f"{original_message}\n补充信息：{answer}"
    assert context["resolvedTripDates"]["startDate"] == "2026-10-01"
    assert context["conversationIntentContext"]["source"] == "clarification_answer"


def test_custom_choice_keeps_source_identity_and_manual_value_without_label_parsing():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "自填选择会话")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user",
            role="user",
            content="北京夜游",
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant",
            role="assistant",
            content="自填地点",
            turn_index=2,
            agent_response_json={
                "choiceOptions": [{"id": "custom:night", "kind": "custom_input", "value": "夜景地点"}]
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="用户输入地点",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "turn_assistant",
                    "choiceId": "custom:night",
                    "manualValue": "景山公园",
                }
            },
        )

        context = AgentContextBuilder(connection).build(row, payload.content, payload)

    assert context["selectedAgentChoice"]["choiceId"] == "custom:night"
    assert context["effectiveUserMessage"] == "北京夜游\n夜景地点\n景山公园"


def test_material_tradeoff_choice_keeps_persisted_identity_without_manual_value():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "实体选择会话")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user",
            role="user",
            content="北京两日游，参观一所985高校",
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant",
            role="assistant",
            content="请选择具体高校",
            turn_index=2,
            agent_response_json={
                "choiceOptions": [
                    {
                        "id": "choice_campus_opaque",
                        "kind": "material_tradeoff",
                        "action": "manual_continuation",
                        "label": "清华大学",
                        "value": "清华大学",
                        "allowsManualInput": False,
                        "candidateRecordId": "cand_campus",
                        "amapId": "B0CAMPUS1",
                        "sourceGoalId": "goal_campus_visit",
                        "expectedBaseVersionId": None,
                    }
                ]
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "turn_assistant",
                    "choiceId": "choice_campus_opaque",
                }
            },
        )

        context = AgentContextBuilder(connection).build(row, payload.content, payload)

    selected = context["selectedAgentChoice"]
    assert selected["action"] == "manual_continuation"
    assert selected["manualValue"] is None
    assert selected["option"]["candidateRecordId"] == "cand_campus"
    assert selected["option"]["amapId"] == "B0CAMPUS1"
    assert context["effectiveUserMessage"] == "北京两日游，参观一所985高校\n清华大学"


def test_safe_fallback_choice_rejects_stale_active_version_before_controller_or_write():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "stale fallback")
        service = AgentService(connection)
        service._insert_turn(session.session_id, "user", "北京两日游", "active")
        service._insert_turn(
            session.session_id,
            "assistant",
            "选择安全降级方式",
            "active",
            agent_response_json={
                "choiceOptions": [
                    {
                        "id": "fallback:rule:nonce",
                        "kind": "safe_fallback_action",
                        "action": "confirm_rule_safe_draft",
                        "label": "使用规则安全草稿",
                        "value": "使用规则安全草稿",
                        "expectedBaseVersionId": None,
                    }
                ]
            },
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
            ("ver_changed_elsewhere", session.session_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": connection.execute(
                        "SELECT id FROM conversation_turns WHERE role = 'assistant'"
                    ).fetchone()[0],
                    "choiceId": "fallback:rule:nonce",
                }
            },
        )

        with pytest.raises(HTTPException) as captured:
            AgentContextBuilder(connection).build(row, payload.content, payload)

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "agent_choice_version_stale"


@pytest.mark.parametrize(
    "action",
    [
        "confirm_portfolio_theme_upgrade",
        "confirm_portfolio_theme_replacement",
    ],
)
def test_consumed_theme_confirmation_choice_is_resolved_for_idempotent_replay(action):
    clear_database()
    option = {
        "id": f"theme_confirm_{action}",
        "action": action,
        "kind": "portfolio_theme_upgrade_confirmation",
        "expectedBaseVersionId": None,
    }
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "主题确认幂等重放")
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_theme_user",
            role="user",
            content="北京两日游",
            turn_index=1,
        )
        insert_context_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_theme_assistant",
            role="assistant",
            content="请确认主题升级",
            turn_index=2,
            agent_response_json={
                "choiceOptions": [option],
                "consumedChoiceIds": [option["id"]],
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
        payload = AgentMessageRequest(
            content="选择 Agent 选项",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "turn_theme_assistant",
                    "choiceId": option["id"],
                }
            },
        )

        context = AgentContextBuilder(connection).build(row, payload.content, payload)

    assert context["selectedAgentChoice"]["action"] == action

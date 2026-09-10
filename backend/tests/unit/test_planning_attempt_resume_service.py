import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.conversation_service import ConversationService
from src.services.planning_attempt_resume_service import PlanningAttemptResumeService


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM planning_runs;
            DELETE FROM amap_poi_candidates;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM traffic_crowding_signals;
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


def insert_turn(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    turn_index: int,
    status: str = "active",
    itinerary_version_id: Optional[str] = None,
    agent_request_json: Optional[dict] = None,
    agent_response_json: Optional[dict] = None,
    error_json: Optional[dict] = None,
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
            status,
            None,
            itinerary_version_id,
            json.dumps(agent_request_json, ensure_ascii=False) if agent_request_json is not None else None,
            json.dumps(agent_response_json, ensure_ascii=False) if agent_response_json is not None else None,
            json.dumps(error_json, ensure_ascii=False) if error_json is not None else None,
            now,
            now,
        ),
    )


def waiting_payload(original_message: str, *, result_state: str = "provider_rate_limited") -> dict:
    return {
        "mode": "staged_initial_pipeline_waiting",
        "resultState": result_state,
        "initialPlan": {"reply": "", "mode": "day_slots", "daySlots": [], "intentPools": []},
        "pipelineContext": {"latestUserMessage": original_message, "effectiveUserMessage": original_message},
        "planningPreview": {"days": []},
        "unresolvedSlots": [{"slotId": "day1_morning", "reason": result_state}],
        "nextActions": ["retry_after_map_provider_recovers"],
    }


def test_contextual_retry_intent_matches_but_does_not_swallow_real_changes():
    assert PlanningAttemptResumeService.is_retry_intent("还是上面要求，重新生成一遍")
    assert PlanningAttemptResumeService.is_retry_intent("按刚才那个重新来")
    assert PlanningAttemptResumeService.is_retry_intent("用刚才的要求再试一次")
    assert PlanningAttemptResumeService.is_retry_intent("按刚才要求换一版")
    assert not PlanningAttemptResumeService.is_retry_intent("还是上面要求，但改成3天")
    assert not PlanningAttemptResumeService.is_retry_intent("按上面要求重新来，但是不要高校了")
    assert not PlanningAttemptResumeService.is_retry_intent("用刚才的要求再试一次，预算改低")


def test_draft_needs_completion_attempt_is_resumable():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_turn(connection, session_id=session.session_id, turn_id="turn_user", role="user", content=original_message, turn_index=1)
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant_waiting",
            role="assistant",
            content="完整行程质量门槛未通过，暂未创建正式时间轴。",
            turn_index=2,
            agent_request_json={"latestUserMessage": original_message},
            agent_response_json=waiting_payload(original_message, result_state="draft_needs_completion"),
        )
        connection.commit()

        context = PlanningAttemptResumeService(connection).resume_context_for_retry(
            session.session_id,
            "按上面要求重新来",
        )

    assert context is not None
    assert context["enabled"] is True
    assert context["resultState"] == "draft_needs_completion"
    assert context["reuseInitialPlan"] is True
    assert context["originalUserMessage"] == original_message
    assert context["initialPlan"]["mode"] == "day_slots"


def test_latest_resumable_attempt_skips_failed_tool_loop_without_initial_plan():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_turn(connection, session_id=session.session_id, turn_id="turn_user", role="user", content=original_message, turn_index=1)
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant_waiting",
            role="assistant",
            content="地点候选未完成。",
            turn_index=2,
            agent_request_json={"latestUserMessage": original_message},
            agent_response_json=waiting_payload(original_message, result_state="waiting_for_poi_grounding"),
        )
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant_failed_tool_loop",
            role="assistant",
            content="Agent 失败：provider_rate_limited。",
            turn_index=3,
            status="failed",
            agent_request_json={"latestUserMessage": "重新构建"},
            agent_response_json={"mode": "tool_loop_failed", "failureReason": "provider_rate_limited"},
            error_json={"message": "provider_rate_limited"},
        )
        connection.commit()

        attempt = PlanningAttemptResumeService(connection).latest_resumable_attempt(session.session_id)

    assert attempt is not None
    assert attempt["sourceAssistantTurnId"] == "turn_assistant_waiting"
    assert attempt["originalUserMessage"] == original_message


def test_resume_retry_does_not_invent_empty_checkpoint_from_complete_request():
    clear_database()
    original_message = "北京高校两日游，晚上看夜景，10月1日到2日，2天，中等预算，1人，公交地铁优先"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_turn(connection, session_id=session.session_id, turn_id="turn_user", role="user", content=original_message, turn_index=1)
        insert_turn(connection, session_id=session.session_id, turn_id="turn_retry", role="user", content="还是上面要求，重新生成一遍", turn_index=2)
        connection.commit()

        context = PlanningAttemptResumeService(connection).resume_context_for_retry(
            session.session_id,
            "还是上面要求，重新生成一遍",
        )

    assert context is None


def test_short_free_form_clarification_answer_inherits_immediately_preceding_request():
    clear_database()
    original_message = "10月1日北京一日游，1人，中等预算，公交地铁优先，参观故宫博物院。"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_turn(connection, session_id=session.session_id, turn_id="turn_user", role="user", content=original_message, turn_index=1)
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant",
            role="assistant",
            content="更想怎样游览？",
            turn_index=2,
            agent_request_json={"latestUserMessage": original_message, "effectiveUserMessage": original_message},
            agent_response_json={"mode": "clarification", "terminalStatus": "needs_confirmation"},
        )
        insert_turn(connection, session_id=session.session_id, turn_id="turn_answer", role="user", content="轻松参观，上午开始。", turn_index=3)
        connection.commit()

        context = PlanningAttemptResumeService(connection).clarification_context_for_answer(
            session.session_id, "轻松参观，上午开始。"
        )

    assert context is not None
    assert context["sourceAssistantTurnId"] == "turn_assistant"
    assert context["effectiveUserMessage"] == f"{original_message}\n补充信息：轻松参观，上午开始。"


def test_new_complete_request_after_clarification_is_not_swallowed_as_an_answer():
    clear_database()
    original_message = "10月1日北京一日游，1人，中等预算，公交地铁优先，参观故宫博物院。"
    new_request = "11月2日至3日上海两日游，2人，低预算，公交地铁优先，参观上海博物馆和外滩。"
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_user",
            role="user",
            content=original_message,
            turn_index=1,
        )
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_assistant",
            role="assistant",
            content="更想怎样游览？",
            turn_index=2,
            agent_request_json={"latestUserMessage": original_message, "effectiveUserMessage": original_message},
            agent_response_json={"mode": "clarification", "terminalStatus": "needs_confirmation"},
        )
        insert_turn(
            connection,
            session_id=session.session_id,
            turn_id="turn_new_request",
            role="user",
            content=new_request,
            turn_index=3,
        )
        connection.commit()

        context = PlanningAttemptResumeService(connection).clarification_context_for_answer(
            session.session_id,
            new_request,
        )
        latest_complete = PlanningAttemptResumeService(connection).latest_complete_user_request(session.session_id)

    assert context is None
    assert latest_complete is not None
    assert latest_complete["sourceUserTurnId"] == "turn_new_request"
    assert latest_complete["originalUserMessage"] == new_request

from __future__ import annotations

import json

from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.runtime.agent_runtime import TripAgentRuntime
from src.runtime.runtime_models import RuntimeRunOptions
from src.services.agent_service import AgentService
from src.services.agent_verifier_service import AgentVerifierReport, AgentVerifierService
from src.services.conversation_service import ConversationService
from src.services.map_poi_service import MapPoiService
from src.services.route_service import RouteService

from backend.tests.integration.test_react_agent_multiturn import (
    TURN_1,
    NoWriteRecordedController,
    TwoTurnReactProvider,
    _open_db,
    _recorded_map_search,
    _recorded_map_search_nearby,
    _recorded_route,
)


_FALSE_NO_WRITE_EVENT_TYPES = {
    "candidate_patch_applied",
    "candidate_patch_committed",
    "timeline_mutation_committed",
    "timeline_patch_committed",
}


def _persisted_response(connection, session_id: str) -> dict:
    row = connection.execute(
        "SELECT agent_response_json FROM conversation_turns "
        "WHERE session_id = ? AND role = 'assistant' ORDER BY turn_index DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["agent_response_json"])


def test_no_write_trace_has_no_synthetic_tool_effect_or_success_claim():
    provider = NoWriteRecordedController()
    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "trace no-write contract")
        response = AgentService(connection, provider=provider).send_message(
            session.session_id,
            AgentMessageRequest(content="重试本次修改"),
        )
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        persisted = _persisted_response(connection, session.session_id)

    assert active_version_id is None
    assert patch_count == 0
    assert response.version is None
    assert response.itinerary is None
    assert response.assistant_turn.status == "active"
    assert response.tool_events == []
    assert persisted["toolEvents"] == []
    planning = persisted["planningSteps"]
    assert not ({event["type"] for event in planning} & _FALSE_NO_WRITE_EVENT_TYPES)
    assert [event["sequence"] for event in planning] == list(range(1, len(planning) + 1))
    assert [event["timestamp"] for event in planning] == sorted(
        event["timestamp"] for event in planning
    )
    for event in planning:
        preview = (event.get("metadata") or {}).get("resultPreview") or {}
        assert preview.get("resultVersionId") is None
        assert not preview.get("patchIds")
        assert preview.get("transactionRecorded") is not True
    ask_outcomes = [
        event
        for event in planning
        if event.get("type") == "agent_action_outcome"
        and ((event.get("metadata") or {}).get("resultPreview") or {}).get("action") == "ask_user"
    ]
    assert len(ask_outcomes) == 1
    ask_preview = ask_outcomes[0]["metadata"]["resultPreview"]
    assert ask_outcomes[0]["category"] == "internal"
    assert ask_preview["resultVersionId"] is None
    assert ask_preview["patchIds"] == []
    assert ask_preview["rollbackPerformed"] is False
    assert ask_preview["verifier"] == {
        "passed": None,
        "notApplicable": True,
        "reason": "no_itinerary_write",
    }
    assert "已写入" not in response.assistant_turn.content
    assert "修改成功" not in response.assistant_turn.content


def test_no_write_runtime_truth_reports_active_version_unchanged(tmp_path):
    provider = NoWriteRecordedController()

    class _RecordedRuntime(TripAgentRuntime):
        def send_agent_message(self, session_id, payload, event_sink=None, **_kwargs):
            return AgentService(self.db, provider=provider).send_message(
                session_id,
                payload,
                event_sink=event_sink,
            )

    with _open_db() as connection:
        final, exit_code = _RecordedRuntime(connection).run_once(
            RuntimeRunOptions(
                input="重试本次修改",
                stateDir=str(tmp_path / "runs"),
                mockProviders=True,
            )
        )
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (final.session_id,),
        ).fetchone()[0]

    assert exit_code != 0
    assert final.active_version_changed is False
    assert final.active_version_id is None
    assert active_version_id is None
    assert final.status not in {"success", "partial_success"}
    assert final.terminal_status not in {"success", "partial_success", "completed"}


def test_hard_verifier_failure_rolls_back_and_trace_never_claims_commit(monkeypatch):
    provider = TwoTurnReactProvider()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(MapPoiService, "search", _recorded_map_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", _recorded_map_search_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)
    monkeypatch.setattr(
        AgentVerifierService,
        "verify_agent_write",
        lambda *_args, **_kwargs: AgentVerifierReport(
            passed=False,
            hard_failures=["trace_injected_hard_failure"],
            checks=[{"name": "trace_injected", "status": "failed"}],
        ),
    )

    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "trace hard-fail contract")
        response = AgentService(connection, provider=provider).send_message(
            session.session_id,
            AgentMessageRequest(content=TURN_1),
        )
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]
        accepted_patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches "
            "WHERE session_id = ? AND validation_status = 'accepted'",
            (session.session_id,),
        ).fetchone()[0]
        persisted = _persisted_response(connection, session.session_id)

    assert active_version_id is None
    assert accepted_patch_count == 0
    assert response.version is None
    assert response.assistant_turn.status != "completed"
    assert response.terminal_status not in {"success", "partial_success", "completed"}
    planning = persisted["planningSteps"]
    assert not ({event["type"] for event in planning} & _FALSE_NO_WRITE_EVENT_TYPES)
    assert not any(
        (event.get("metadata") or {}).get("resultVersionId")
        for event in planning
        if event.get("status") == "completed"
    )
    assert "完成" not in response.assistant_turn.content
    assert "版本已保存" not in response.assistant_turn.content

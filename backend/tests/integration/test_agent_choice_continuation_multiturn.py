from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from backend.tests.integration.test_react_agent_multiturn import (
    TURN_1,
    TwoTurnReactProvider,
    _recorded_map_search,
    _recorded_map_search_nearby,
    _recorded_route,
)
from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.map_poi_service import MapPoiService
from src.services.route_service import RouteService


MODIFICATION = "重试一次，将第一天美术馆修改为清华美术馆"


class RecordedChoiceContinuationProvider(TwoTurnReactProvider):
    """Create a real Turn 1, fail one source turn, then record continuation context."""

    def __init__(self):
        super().__init__()
        self.source_failure_calls = 0
        self.continuation_calls = 0

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        continuation = context.get("continuationContext") if isinstance(context, dict) else None
        latest_message = str(context.get("latestUserMessage") or "") if isinstance(context, dict) else ""
        if not continuation and latest_message != MODIFICATION:
            return super().decide_autonomy(
                context,
                timeout_seconds=timeout_seconds,
                repair_feedback=repair_feedback,
            )

        self.decision_calls += 1
        self.decision_contexts.append(copy.deepcopy(context))
        if latest_message == MODIFICATION and self.source_failure_calls < 2:
            self.source_failure_calls += 1
            # Both the initial response and the single reserved repair are
            # irreparably invalid, forcing a persisted retry/manual choice.
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "resolve_poi",
                "actionDirective": {"type": "resolve_poi"},
            }
        if continuation:
            self.continuation_calls += 1
            return self._finish("已从持久化上下文继续处理。")
        raise AssertionError(f"unexpected Controller call {self.decision_calls}: {latest_message}")


@pytest.fixture
def fresh_sqlite(monkeypatch, tmp_path):
    database_path = tmp_path / "choice-continuation.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    get_settings.cache_clear()
    initialize_database()
    yield database_path
    get_settings.cache_clear()


@pytest.fixture
def recorded_providers(monkeypatch):
    monkeypatch.setattr(MapPoiService, "search", _recorded_map_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", _recorded_map_search_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)


def _open_fresh_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _prepare_persisted_source_choice(
    connection: sqlite3.Connection,
    provider: RecordedChoiceContinuationProvider,
    *,
    choice_action: str,
) -> tuple[AgentService, str, str, dict, str, dict]:
    session = ConversationService(connection).create_session("北京", f"{choice_action} continuation")
    service = AgentService(connection, provider=provider)

    turn_1 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_1))
    assert turn_1.version is not None, {
        "reply": turn_1.assistant_turn.content,
        "choices": turn_1.assistant_turn.choice_options,
        "events": [
            {
                "type": event.type,
                "status": event.status,
                "detail": event.detail,
                "metadata": event.metadata,
            }
            for event in turn_1.planning_steps
            if event.type in {"agent_decision", "agent_action_outcome", "simple_open_terminal"}
        ],
    }
    active_version_id = turn_1.version.id

    source_turn = service.send_message(
        session.session_id,
        AgentMessageRequest(content=MODIFICATION),
    )
    option = next(
        (item for item in source_turn.assistant_turn.choice_options if item["action"] == choice_action),
        None,
    )
    assert option is not None, {
        "choices": source_turn.assistant_turn.choice_options,
        "steps": [
            {"type": step.type, "metadata": step.metadata}
            for step in source_turn.planning_steps
            if step.type in {"agent_decision", "agent_policy_gate"}
        ],
    }
    source_request = json.loads(
        connection.execute(
            "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
            (source_turn.user_turn.id,),
        ).fetchone()[0]
    )
    assert source_request["activeVersionId"] == active_version_id
    assert source_request["resolvedTripDates"]["dates"] == ["2026-10-01", "2026-10-02"]
    assert source_request["requestIntentContract"]["requiredIntents"]
    return (
        service,
        session.session_id,
        source_turn.assistant_turn.id,
        option,
        active_version_id,
        source_request,
    )


def _assert_root_coordinator_context(
    *,
    provider: RecordedChoiceContinuationProvider,
    response,
    request_payload: dict,
    source_request: dict,
    active_version_id: str,
    expected_latest_message: str,
    expected_kind: str,
) -> None:
    assert provider.source_failure_calls == 2
    assert provider.continuation_calls == 1
    controller_context = provider.decision_contexts[-1]
    assert {
        "persistedLatestUserMessage": request_payload["latestUserMessage"],
        "controllerLatestUserMessage": controller_context["latestUserMessage"],
        "activeVersionId": controller_context["activeVersionId"],
        "resolvedTripDates": controller_context["resolvedTripDates"],
        "requestIntentContract": controller_context["requestIntentContract"],
        "continuationKind": controller_context["continuationContext"]["kind"],
    } == {
        "persistedLatestUserMessage": expected_latest_message,
        "controllerLatestUserMessage": source_request["effectiveUserMessage"],
        "activeVersionId": active_version_id,
        "resolvedTripDates": source_request["resolvedTripDates"],
        "requestIntentContract": source_request["requestIntentContract"],
        "continuationKind": expected_kind,
    }
    assert TURN_1 in controller_context["effectiveUserMessage"]
    assert MODIFICATION in controller_context["effectiveUserMessage"]
    assert controller_context["continuationContext"]["sourceAssistantTurnId"]
    assert controller_context["continuationContext"]["sourceUserTurnId"]
    assert controller_context["continuationContext"]["sourceActiveVersionId"] == active_version_id

    decisions = [event for event in response.planning_steps if event.type == "agent_decision"]
    assert len(decisions) == 1
    assert decisions[0].metadata["source"] == "controller"
    assert decisions[0].metadata["controlOwner"] == "model_controller"
    assert not any(
        event.metadata.get("legacyAdapterCalled") is True
        or event.metadata.get("preControllerDomainRouterCount", 0) != 0
        or event.metadata.get("deterministicArbitratorCalled") is True
        for event in response.planning_steps
    )


def test_retry_model_planning_reenters_root_coordinator_with_canonical_source_context(
    fresh_sqlite,
    recorded_providers,
    monkeypatch,
):
    del fresh_sqlite, recorded_providers
    monkeypatch.setattr(AgentService, "_creative_portfolio_enabled", lambda _self, _context: False)
    monkeypatch.setattr(AgentService, "_active_partial_pending_snapshot", lambda _self, _session_id: None)
    provider = RecordedChoiceContinuationProvider()
    with _open_fresh_connection() as connection:
        (
            service,
            session_id,
            source_assistant_turn_id,
            option,
            active_version_id,
            source_request,
        ) = _prepare_persisted_source_choice(connection, provider, choice_action="retry_model_planning")
        response = service.send_message(
            session_id,
            AgentMessageRequest(
                content="重试本次修改",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_assistant_turn_id,
                        "choiceId": option["id"],
                    }
                },
            ),
        )
        request_payload = json.loads(
            connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
                (response.user_turn.id,),
            ).fetchone()[0]
        )

    _assert_root_coordinator_context(
        provider=provider,
        response=response,
        request_payload=request_payload,
        source_request=source_request,
        active_version_id=active_version_id,
        expected_latest_message="重试本次修改",
        expected_kind="retry_model_planning",
    )


def test_existing_itinerary_controller_failure_does_not_offer_unscoped_manual_continuation(
    fresh_sqlite,
    recorded_providers,
    monkeypatch,
):
    del fresh_sqlite, recorded_providers
    monkeypatch.setattr(AgentService, "_creative_portfolio_enabled", lambda _self, _context: False)
    monkeypatch.setattr(AgentService, "_active_partial_pending_snapshot", lambda _self, _session_id: None)
    provider = RecordedChoiceContinuationProvider()
    with _open_fresh_connection() as connection:
        session = ConversationService(connection).create_session("北京", "unscoped manual continuation")
        service = AgentService(connection, provider=provider)
        initial = service.send_message(
            session.session_id,
            AgentMessageRequest(content=TURN_1),
        )
        assert initial.version is not None
        before_counts = (
            connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0],
        )
        source_turn = service.send_message(
            session.session_id,
            AgentMessageRequest(content=MODIFICATION),
        )
        after_counts = (
            connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0],
        )
        active_after = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert provider.source_failure_calls == 2
    assert [item["action"] for item in source_turn.assistant_turn.choice_options] == ["retry_model_planning"]
    assert all(item.get("action") != "manual_continuation" for item in source_turn.assistant_turn.choice_options)
    assert source_turn.version is None
    assert active_after == initial.version.id
    assert after_counts == before_counts

from __future__ import annotations

import json

from src.api.schemas.agent import AgentMessageRequest
from src.api.schemas.maps import MapPoiSearchResponse
from src.core.config import get_settings
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.map_poi_service import MapPoiService
from src.services.route_service import RouteService

from backend.tests.integration.test_react_agent_multiturn import (
    POIS,
    TURN_1,
    TURN_2,
    TwoTurnReactProvider,
    _open_db,
    _poi,
    _recorded_map_search,
    _recorded_map_search_nearby,
    _recorded_route,
)


def _decision_actions(response) -> list[str]:
    return [event.metadata["primaryAction"] for event in response.planning_steps if event.type == "agent_decision"]


def test_dominant_museum_alias_persists_evidence_before_patch_and_finish(monkeypatch):
    """Exercise the production Coordinator and executors against a fresh DB."""
    provider = TwoTurnReactProvider()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(MapPoiService, "search", _recorded_map_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", _recorded_map_search_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)

    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "POI autonomy contract")
        service = AgentService(connection, provider=provider)
        turn_1 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_1))
        assert turn_1.version is not None
        base_version_id = turn_1.version.id
        versions_before = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patches_before = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

        turn_2 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_2))
        session_row = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        candidate_rows = connection.execute(
            "SELECT id, segment_id, selected_amap_id, candidates_json "
            "FROM amap_poi_candidates WHERE session_id = ? ORDER BY created_at",
            (session.session_id,),
        ).fetchall()
        versions_after = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patches_after = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert _decision_actions(turn_2) == ["resolve_poi", "patch_itinerary", "finish"]
    assert provider.tool_loop_calls == 0
    assert turn_2.version is not None
    assert session_row["active_version_id"] == turn_2.version.id
    assert session_row["active_version_id"] != base_version_id
    assert versions_after - versions_before == 1
    assert patches_after - patches_before == 1

    matching = [row for row in candidate_rows if row["selected_amap_id"] == "B0REACTTSINGHUAART"]
    assert len(matching) == 1
    assert matching[0]["segment_id"]
    assert any(candidate["name"] == "清华大学艺术博物馆" for candidate in json.loads(matching[0]["candidates_json"]))

    post_resolve = provider.decision_contexts[3]["observation"]
    assert post_resolve["lastAction"]["action"] == "resolve_poi"
    assert post_resolve["lastOutcome"]["candidateSummary"]["dominance"]["dominant"] is True
    assert any(
        group["id"] == matching[0]["id"] and group["selectedAmapId"] == "B0REACTTSINGHUAART"
        for group in post_resolve["candidateState"]["pendingGroups"]
    )
    post_patch = provider.decision_contexts[4]["observation"]
    assert post_patch["lastAction"]["action"] == "patch_itinerary"
    assert post_patch["lastOutcome"]["status"] == "success"
    assert post_patch["versionLineage"]["currentVersionId"] == turn_2.version.id


class _MaterialTradeoffProvider(TwoTurnReactProvider):
    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        if self.decision_calls < 3:
            return super().decide_autonomy(
                context,
                timeout_seconds=timeout_seconds,
                repair_feedback=repair_feedback,
            )
        self.decision_calls += 1
        self.decision_contexts.append(context)
        observation = context.get("observation") or {}
        groups = observation.get("candidateState", {}).get("pendingGroups") or []
        assert len(groups) == 1
        assert groups[0]["candidateCount"] == 2
        assert groups[0]["selectedAmapId"] is None
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "ask_user",
            "actionDirective": {
                "type": "ask_user",
                "question": "两座美术馆同样可信，请选择一座。",
                "choiceIds": ["museum-east", "museum-west"],
            },
        }


def _material_tradeoff_search(_self, city, keyword, category="all", limit=10):
    if "清华美术馆" not in str(keyword):
        return _recorded_map_search(_self, city, keyword, category, limit)
    candidates = [
        _poi(
            "B0REACTARTEAST",
            "清华艺术博物馆东馆",
            poi_type="科教文化服务;博物馆;美术馆",
            category="museum",
            longitude=116.3306,
            latitude=40.0030,
        ),
        _poi(
            "B0REACTARTWEST",
            "清华艺术博物馆西馆",
            poi_type="科教文化服务;博物馆;美术馆",
            category="museum",
            longitude=116.3310,
            latitude=40.0032,
        ),
    ]
    return MapPoiSearchResponse(
        city=city,
        keyword=keyword,
        category=category,
        providerName="recorded-amap-material-tradeoff",
        queriedAt=_recorded_map_search(_self, city, keyword, category, 0).queried_at,
        pois=candidates[:limit],
    )


def test_material_museum_tradeoff_asks_once_without_patch_or_version(monkeypatch):
    provider = _MaterialTradeoffProvider()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "recorded-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(MapPoiService, "search", _material_tradeoff_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", _recorded_map_search_nearby)
    monkeypatch.setattr(RouteService, "_fetch_amap_route", _recorded_route)

    with _open_db() as connection:
        session = ConversationService(connection).create_session("北京", "POI material tradeoff")
        service = AgentService(connection, provider=provider)
        turn_1 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_1))
        assert turn_1.version is not None
        active_before = turn_1.version.id
        versions_before = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patches_before = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

        turn_2 = service.send_message(session.session_id, AgentMessageRequest(content=TURN_2))
        active_after = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()[0]
        versions_after = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patches_after = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        pending = connection.execute(
            "SELECT status, selected_amap_id, candidates_json FROM amap_poi_candidates "
            "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session.session_id,),
        ).fetchone()

    assert _decision_actions(turn_2) == ["resolve_poi", "ask_user"]
    assert provider.tool_loop_calls == 0
    assert turn_2.terminal_status == "needs_confirmation"
    assert active_after == active_before
    assert versions_after == versions_before
    assert patches_after == patches_before
    assert pending["status"] == "pending"
    assert pending["selected_amap_id"] is None
    assert len(json.loads(pending["candidates_json"])) == 2
    assert not any(event.type == "timeline_edit" for event in turn_2.planning_steps)

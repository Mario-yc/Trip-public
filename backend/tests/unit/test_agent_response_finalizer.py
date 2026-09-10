from src.api.schemas.agent import AgentMessageResponse, ConversationTurnResponse
from src.api.schemas.itinerary_patches import ItineraryVersionResponse
from src.services.agent_response_finalizer import AgentResponseFinalizer
from typing import Optional


def _turn(
    turn_id: str,
    role: str,
    status: str = "active",
    itinerary_version_id: Optional[str] = None,
) -> ConversationTurnResponse:
    return ConversationTurnResponse(
        id=turn_id,
        role=role,
        content="message",
        turnIndex=1,
        status=status,
        parentTurnId=None,
        itineraryVersionId=itinerary_version_id,
        failureReason=None,
        planningSteps=[],
        toolEvents=[],
        createdAt="2026-07-09T00:00:00+00:00",
        updatedAt="2026-07-09T00:00:00+00:00",
    )


def _finalizer(calls: dict[str, int]) -> AgentResponseFinalizer:
    return AgentResponseFinalizer(
        auto_update_preference_memory=lambda _user, _assistant: calls.__setitem__("autoUpdate", calls.get("autoUpdate", 0) + 1),
        current_preference_memory=lambda: None,
        current_pending_candidates=lambda: [],
        current_active_state=lambda: {"activePlanId": "plan_1", "activeVersionId": "ver_active"},
        load_itinerary=lambda _plan_id: {"id": "plan_1"},
        load_version=lambda _version_id: ItineraryVersionResponse(id="ver_active", versionNumber=2, sourceType="agent"),
        commit=lambda: calls.__setitem__("commit", calls.get("commit", 0) + 1),
    )


def test_finalizer_does_not_hydrate_failed_or_noop_response_version_without_turn_version():
    calls: dict[str, int] = {}
    response = AgentMessageResponse(
        userTurn=_turn("turn_user", "user"),
        assistantTurn=_turn("turn_assistant", "assistant", status="failed", itinerary_version_id=None),
        itinerary=None,
        version=ItineraryVersionResponse(id="ver_old", versionNumber=1, sourceType="agent"),
        pendingPoiCandidates=[],
        warnings=[],
        planningRun=None,
        planningSteps=[],
        toolEvents=[],
    )

    finalized = _finalizer(calls).finalize(response)

    assert finalized.version.id == "ver_old"
    assert finalized.itinerary is None
    assert calls == {}


def test_finalizer_only_refreshes_version_when_assistant_turn_matches_active_version():
    calls: dict[str, int] = {}
    response = AgentMessageResponse(
        userTurn=_turn("turn_user", "user"),
        assistantTurn=_turn("turn_assistant", "assistant", status="active", itinerary_version_id="ver_active"),
        itinerary=None,
        version=ItineraryVersionResponse(id="ver_old", versionNumber=1, sourceType="agent"),
        pendingPoiCandidates=[],
        warnings=[],
        planningRun=None,
        planningSteps=[],
        toolEvents=[],
    )

    finalized = _finalizer(calls).finalize(response)

    assert finalized.version.id == "ver_active"
    assert finalized.itinerary == {"id": "plan_1"}
    assert calls == {"autoUpdate": 1, "commit": 1}

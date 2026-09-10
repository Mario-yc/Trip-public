import json
import sqlite3

import pytest

from src.api.schemas.agent import AgentMessageEditRequest, AgentMessageRequest
from src.api.schemas.maps import MapPoiResolveResponse, MapPoiResolvedItemResponse, MapPoiResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.itinerary_service import ItineraryService


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM amap_poi_candidates;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            """
        )


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


@pytest.mark.xfail(
    reason="legacy provider.generate branch regeneration no longer owns ordinary-language control",
    strict=True,
)
def test_editing_first_user_turn_rolls_back_supersedes_and_regenerates_branch():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        provider = SequenceProvider(
            [
                full_itinerary_output(),
                patch_output("北京第二版"),
                patch_output("北京第三版"),
                patch_output("北京历史重写版"),
            ]
        )
        service = AgentService(
            connection,
            provider=provider,
            poi_resolution_service=FakePoiResolver(
                {
                    "故宫": poi_fixture("B000PALACE", "故宫博物院"),
                    "景山": poi_fixture("B000JINGSHAN", "景山公园"),
                }
            ),
        )

        first = service.send_message(session.session_id, AgentMessageRequest(content="生成一天"))
        second = service.send_message(session.session_id, AgentMessageRequest(content="改成第二版"))
        third = service.send_message(session.session_id, AgentMessageRequest(content="改成第三版"))
        edited = service.edit_user_message(
            session.session_id,
            first.user_turn.id,
            AgentMessageEditRequest(content="改成三天但别太满", regenerate=True),
        )
        active = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT id, status FROM conversation_turns WHERE session_id = ? ORDER BY turn_index ASC",
            (session.session_id,),
        ).fetchall()
        plan = ItineraryService(connection).get_plan(session.active_plan_id)

    statuses = {row["id"]: row["status"] for row in rows}
    assert edited.restored_version_id == first.version.id
    assert active["active_version_id"] == edited.version.id
    assert plan.title == "北京历史重写版"
    assert statuses[first.user_turn.id] == "active"
    assert statuses[first.assistant_turn.id] == "superseded"
    assert statuses[second.user_turn.id] == "superseded"
    assert statuses[second.assistant_turn.id] == "superseded"
    assert statuses[third.user_turn.id] == "superseded"
    assert statuses[third.assistant_turn.id] == "superseded"
    assert edited.assistant_turn.status == "active"
    assert provider.contexts[-1]["currentItinerarySnapshot"]["title"] == "北京轻松 1 日游"


class SequenceProvider:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.contexts = []

    def generate(self, context: dict) -> str:
        self.contexts.append(context)
        return json.dumps(self.payloads.pop(0), ensure_ascii=False)


class FakePoiResolver:
    def __init__(self, pois_by_query: dict[str, MapPoiResponse]):
        self.pois_by_query = pois_by_query

    def resolve(self, payload) -> MapPoiResolveResponse:
        return MapPoiResolveResponse(
            resolved=[
                MapPoiResolvedItemResponse(query=query.name, status="accepted", poi=self.pois_by_query[query.name])
                for query in payload.queries
                if query.name in self.pois_by_query
            ],
            pending=[],
        )


def full_itinerary_output() -> dict:
    return {
        "reply": "已生成北京 1 日行程。",
        "mode": "full_itinerary",
        "operations": [],
        "fullItinerary": {
            "title": "北京轻松 1 日游",
            "city": "北京",
            "days": [
                {
                    "dayNumber": 1,
                    "title": "故宫与景山",
                    "segments": [
                        {
                            "poiName": "故宫",
                            "category": "scenic",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "notes": "上午游览故宫",
                            "estimatedCost": 60,
                        },
                        {
                            "poiName": "景山",
                            "category": "scenic",
                            "startTime": "12:30",
                            "durationMinutes": 60,
                            "notes": "登高看中轴线",
                            "estimatedCost": 10,
                        },
                    ],
                }
            ],
        },
        "poiResolutionRequests": [],
        "warnings": [],
    }


def patch_output(title: str) -> dict:
    return {
        "reply": f"已把标题改成{title}。",
        "mode": "patch",
        "operations": [{"op": "replace_trip_title", "value": title}],
        "fullItinerary": None,
        "poiResolutionRequests": [],
        "warnings": [],
    }


def poi_fixture(amap_id: str, name: str) -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type="风景名胜",
        city="北京市",
        district="东城区",
        address="高德地址",
        longitude=116.397026,
        latitude=39.918058,
        category="scenic",
        source="amap-place-search",
        sourceNote="高德 WebService POI 搜索",
        confidence=0.91,
        photos=[],
    )

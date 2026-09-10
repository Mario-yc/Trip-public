from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.conversation_service import ConversationService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.route_insertion_scorer import RouteInsertionScorer


def open_db() -> sqlite3.Connection:
    initialize_database()
    connection = sqlite3.connect(sqlite_path_from_url(get_settings().database_url), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def museum_candidate(amap_id: str = "B0MUSEUM", name: str = "清华大学艺术博物馆") -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type="科教文化服务;博物馆;美术馆",
        city="北京市",
        district="海淀区",
        address="清华园1号",
        longitude=116.326,
        latitude=40.003,
        category="museum",
        source=AMAP_PLACE_SOURCE,
        sourceNote="recorded AMap place/text fixture",
        confidence=0.98,
        photos=[],
    )


def campus_candidate(amap_id: str = "B0PKU", name: str = "北京大学") -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name=name,
        type="科教文化服务;学校;高等院校",
        city="北京市",
        district="海淀区",
        address="颐和园路5号",
        longitude=116.311,
        latitude=39.993,
        category="campus",
        source=AMAP_PLACE_SOURCE,
        sourceNote="recorded AMap place/text fixture",
        confidence=0.98,
        photos=[],
    )


class RecordedMapPoiService:
    def __init__(self, pois=None, error: Exception | None = None):
        self.pois = list(pois if pois is not None else [museum_candidate()])
        self.error = error
        self.calls: list[tuple[str, str, str, int]] = []

    def search(self, city: str, keyword: str, category: str = "all", limit: int = 12):
        self.calls.append((city, keyword, category, limit))
        if self.error:
            raise self.error
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName=AMAP_PLACE_SOURCE,
            queriedAt=datetime.now(timezone.utc),
            pois=self.pois,
        )

    def search_nearby(
        self,
        city: str,
        longitude: float,
        latitude: float,
        keyword: str,
        category: str = "all",
        radius: int = 2500,
        limit: int = 12,
    ):
        """Match the production nearby-search contract used by incremental edits."""
        del longitude, latitude, radius
        return self.search(city=city, keyword=keyword, category=category, limit=limit)


def seed_timeline(connection: sqlite3.Connection, *, duplicate_museum: bool = False, grounded_museum=False):
    connection.executescript(
        """
        DELETE FROM timeline_mutation_transactions;
        DELETE FROM itinerary_patches;
        DELETE FROM itinerary_versions;
        DELETE FROM conversation_turns;
        DELETE FROM conversation_sessions;
        DELETE FROM traffic_crowding_signals;
        DELETE FROM ticket_lookup_results;
        DELETE FROM weather_signals;
        DELETE FROM route_options;
        DELETE FROM itinerary_segments;
        DELETE FROM itinerary_days;
        DELETE FROM itinerary_plans;
        DELETE FROM pois;
        """
    )
    connection.commit()
    session = ConversationService(connection).create_session("北京", "事务化时间轴测试")
    snapshot = {
        "id": session.active_plan_id,
        "title": "北京测试行程",
        "city": "北京",
        "templateType": "agent_mvp",
        "budgetTarget": 1000,
        "budgetTier": "medium",
        "budgetEstimate": 100,
        "budgetDeltaExplanation": "测试",
        "decisionRationale": "测试",
        "status": "draft",
        "days": [
            {
                "id": "day_mutation_1",
                "dayNumber": 1,
                "date": "2026-10-01",
                "title": "Day 1",
                "weatherSummary": "",
                "riskSummary": "",
                "totalEstimatedCost": 100,
                "segments": [
                    segment("seg_before", "09:00", "10:30", "清华大学", "B0THU", "campus_visit"),
                    segment("seg_mid", "12:00", "13:00", "午餐", "B0LUNCH", "meal", kind="meal"),
                    segment(
                        "seg_831b98d1cb6e",
                        "16:15",
                        "17:45",
                        "清华大学艺术博物馆" if grounded_museum else "美术馆",
                        "B0MUSEUM" if grounded_museum else None,
                        "museum",
                        source=AMAP_PLACE_SOURCE if grounded_museum else "agent-text-timeline",
                    ),
                    segment("seg_after", "19:30", "21:00", "景山公园", "B0PARK", "night_view"),
                ],
            }
        ],
        "routeOptions": [],
        "weatherSignals": [],
        "trafficCrowdingSignals": [],
        "poiRiskAlerts": [],
        "ticketLookupResults": [],
    }
    if duplicate_museum:
        snapshot["days"][0]["segments"].insert(
            3,
            segment("seg_second_museum", "18:00", "19:00", "博物馆", None, "museum", source="agent-text-timeline"),
        )
        snapshot["days"][0]["segments"][4]["startTime"] = "21:00"
        snapshot["days"][0]["segments"][4]["endTime"] = "22:30"
    service = ItinerarySnapshotService(connection)
    service.apply_snapshot(session.active_plan_id, snapshot)
    normalized = service.capture_snapshot(session.active_plan_id)
    normalized["routeDecisionContract"] = server_route_decision_contract()
    version = service.save_version(session.session_id, session.active_plan_id, "fixture", snapshot=normalized)
    connection.commit()
    return session, version, normalized


def append_second_day(connection: sqlite3.Connection, session, snapshot: dict):
    snapshot["days"].append(
        {
            "id": "day_mutation_2",
            "dayNumber": 2,
            "date": "2026-10-02",
            "title": "Day 2",
            "weatherSummary": "",
            "riskSummary": "",
            "totalEstimatedCost": 0,
            "segments": [segment("seg_day2_museum", "09:00", "11:00", "中国国家博物馆", "B0NMC", "museum")],
        }
    )
    service = ItinerarySnapshotService(connection)
    service.apply_snapshot(session.active_plan_id, snapshot)
    normalized = service.capture_snapshot(session.active_plan_id)
    normalized["routeDecisionContract"] = server_route_decision_contract()
    version = service.save_version(session.session_id, session.active_plan_id, "fixture_day_2", snapshot=normalized)
    connection.commit()
    return version, normalized


def server_route_decision_contract() -> dict:
    portable = RouteInsertionScorer.build_route_decision_contract(
        source="timeline_mutation_fixture_server",
        provenance={
            "issuer": "timeline_mutation_test_support",
            "transportMode": "walking",
            "confirmed": True,
            "confirmationSource": "explicit_fixture_user_preference",
        },
        detour_tolerance={
            "maxGeneralizedCostDelta": 35.0,
            "maxDetourRatio": 1.5,
        },
        mobility_profile={
            "source": "timeline_mutation_fixture_server",
            "walkingPenaltyMinutesPerKm": 2.0,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert portable is not None
    return {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        "detourToleranceSource": "explicit_fixture_user_preference",
        **portable,
    }


def segment(
    segment_id: str,
    start: str,
    end: str,
    name: str,
    amap_id: str | None,
    intent_type: str,
    *,
    kind: str = "visit",
    source: str = AMAP_PLACE_SOURCE,
):
    grounded = bool(amap_id)
    provider_evidence = {
        "campus_visit": ("科教文化服务;学校;高等院校", "university"),
        "museum": ("科教文化服务;博物馆;美术馆", "museum"),
        "meal": ("餐饮服务;中餐厅", "food"),
        "night_view": ("风景名胜;公园广场;公园", "scenic"),
    }
    poi_type, category = provider_evidence.get(intent_type, ("风景名胜", "scenic"))
    return {
        "id": segment_id,
        "startTime": start,
        "endTime": end,
        "kind": kind,
        "poi": {
            "id": f"poi_{segment_id}",
            "amapId": amap_id,
            "name": name,
            "type": poi_type,
            "city": "北京",
            "district": "海淀区",
            "address": "测试地址",
            "category": category,
            "latitude": 40.0 if grounded else None,
            "longitude": 116.3 if grounded else None,
            "photoUrl": None,
            "source": source,
            "sourceNote": "recorded fixture" if grounded else "高德 POI 待校验",
            "sourceUrl": None,
            "confidence": 0.95 if grounded else 0.2,
            "photos": [],
            "groundingStatus": "selected" if grounded else "waiting_for_poi_grounding",
            "routeable": grounded,
            "intentType": intent_type,
        },
        "transportMode": "walking",
        "estimatedCost": 0,
        "estimateMetadata": {"duration": {"userLocked": False}},
        "semanticMetadata": {
            "intentType": intent_type,
            "intentSlotId": f"day1_{intent_type}",
            "rawNeed": name,
            "groundingStatus": "selected" if grounded else "waiting_for_poi_grounding",
            "routeAnchor": True,
            "requirementLevel": "optional" if intent_type == "meal" else "required",
            "required": intent_type != "meal",
            "userLocked": False,
            "aliases": [name, "美术馆", "博物馆", "艺术馆"] if intent_type == "museum" else [name],
        },
        "notes": name,
    }

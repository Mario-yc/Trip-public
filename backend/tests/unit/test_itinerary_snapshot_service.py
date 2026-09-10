import sqlite3

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.conversation_service import ConversationService
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService


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
            DELETE FROM traffic_crowding_signals;
            DELETE FROM weather_signals;
            DELETE FROM route_options;
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


def seed_day(connection: sqlite3.Connection, plan_id: str) -> tuple[str, str]:
    connection.execute(
        """
        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude, photo_url,
            source, confidence, amap_id, type, district, address, source_note,
            source_url, photos_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "poi_snapshot",
            plan_id,
            "故宫博物院",
            "北京",
            "scenic",
            39.918058,
            116.397026,
            None,
            "amap-place-search",
            0.91,
            "B000A8UIN8",
            "风景名胜",
            "东城区",
            "景山前街4号",
            "高德 WebService POI 搜索",
            None,
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO itinerary_days (
            id, plan_id, day_number, date, title, weather_summary,
            risk_summary, total_estimated_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("day_snapshot", plan_id, 1, None, "历史中轴线", "晴", "低风险", 60),
    )
    connection.execute(
        """
        INSERT INTO itinerary_segments (
            id, plan_id, day_id, segment_order, kind, start_time, end_time,
            poi_id, transport_mode, estimated_cost, notes,
            weather_signal_id, traffic_crowding_signal_id, ticket_lookup_result_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "seg_snapshot",
            plan_id,
            "day_snapshot",
            1,
            "activity",
            "09:00",
            "11:00",
            "poi_snapshot",
            "walk",
            60,
            "初始说明",
            None,
            None,
            None,
        ),
    )
    connection.commit()
    return "day_snapshot", "seg_snapshot"


def test_snapshot_restore_rebuilds_active_itinerary_read_model():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_day(connection, session.active_plan_id)
        snapshot_service = ItinerarySnapshotService(connection)
        first_snapshot = snapshot_service.capture_snapshot(session.active_plan_id)
        first_version = snapshot_service.save_version(
            session.session_id,
            session.active_plan_id,
            "manual",
            snapshot=first_snapshot,
        )
        connection.commit()

        connection.execute(
            "UPDATE itinerary_plans SET title = ? WHERE id = ?", ("被覆盖的标题", session.active_plan_id)
        )
        connection.execute("UPDATE itinerary_days SET title = ? WHERE id = ?", ("被覆盖的 Day", "day_snapshot"))
        connection.execute("UPDATE itinerary_segments SET start_time = ? WHERE id = ?", ("15:00", "seg_snapshot"))
        connection.commit()

        restored_snapshot, restored_version = snapshot_service.restore_existing_version(
            session.active_plan_id,
            first_version.id,
        )
        connection.commit()
        restored = ItineraryService(connection).get_plan(session.active_plan_id)

    assert restored.title == "北京会话"
    assert restored.days[0].title == "历史中轴线"
    assert restored.days[0].segments[0].start_time == "09:00"
    assert restored.days[0].segments[0].poi.amap_id == "B000A8UIN8"
    assert restored_snapshot["title"] == "北京会话"
    assert restored_version.version_number == 2


def test_snapshot_round_trip_preserves_amap_parent_identity():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_day(connection, session.active_plan_id)
        service = ItinerarySnapshotService(connection)
        snapshot = service.capture_snapshot(session.active_plan_id)
        snapshot["days"][0]["segments"][0]["poi"]["parentPoiId"] = "B000PARENT1"

        service.apply_snapshot(session.active_plan_id, snapshot)
        connection.commit()
        restored = service.capture_snapshot(session.active_plan_id)

    assert restored["days"][0]["segments"][0]["poi"]["parentPoiId"] == "B000PARENT1"


def test_active_version_snapshot_overlays_all_portfolio_metadata_without_replacing_live_plan():
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "北京会话")
        seed_day(connection, session.active_plan_id)
        service = ItinerarySnapshotService(connection)
        version_snapshot = service.capture_snapshot(session.active_plan_id)
        version_snapshot.update(
            {
                "portfolioPartialTimeline": {"status": "partial", "pendingSlotCount": 1},
                "portfolioPendingSlots": [{"planningSlotId": "slot_meal_day_2"}],
                "portfolioSelectionContext": {
                    "planningSelectionRootTurnId": "turn_root",
                    "rootPortfolioId": "portfolio_root",
                    "focusBriefId": "brief_food",
                    "requestContractFingerprint": "fp_request",
                },
                "portfolioPlanningDirections": [
                    {"briefId": "brief_food", "title": "高校与美食"},
                    {"briefId": "brief_night", "title": "高校与夜景"},
                ],
                "creativeBrief": {"briefId": "brief_food", "title": "高校与美食"},
                "portfolioGoalOccurrencePlan": [{"planningSlotId": "slot_meal_day_2"}],
                "portfolioDailyCapacityPlan": {"2": {"targetRouteAnchors": 3}},
                "portfolioRequiredCandidateBindings": {"goal_meal": ["amap_food"]},
                "portfolioPendingSlotScheduleConstraints": {"slot_meal_day_2": {"dayNumber": 2}},
                "routeDecisionContract": {
                    "schemaVersion": "route-decision-contract-v1",
                    "source": "request_intent_contract",
                    "fingerprint": "fp_route_contract",
                },
                "routeInsertionProofs": [{"contractFingerprint": "fp_route_contract"}],
                "routeMatrixExpectedPairs": [{"fromSegmentId": "seg_a", "toSegmentId": "seg_b"}],
            }
        )
        version = service.save_version(
            session.session_id,
            session.active_plan_id,
            "portfolio_partial",
            snapshot=version_snapshot,
        )
        connection.execute(
            "UPDATE itinerary_plans SET title = ? WHERE id = ?",
            ("实时标题", session.active_plan_id),
        )
        connection.commit()

        active = service.capture_active_version_snapshot(session.active_plan_id, version.id)

    assert active["title"] == "实时标题"
    assert active["activeVersionId"] == version.id
    for key in ItinerarySnapshotService.VERSION_METADATA_KEYS:
        if key in version_snapshot:
            assert active[key] == version_snapshot[key]

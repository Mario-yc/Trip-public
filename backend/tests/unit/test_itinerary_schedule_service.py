import json
import sqlite3
from datetime import datetime, timezone

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.itinerary_schedule_service import ItineraryScheduleService


def test_materialization_timing_uses_exact_clock_and_rejects_unknown_daypart_labels():
    exact = ItineraryScheduleService.compiled_materialization_timing(
        time_window="10:20-11:35",
        start_time=None,
        duration=75,
        intent_type="campus_visit",
    )
    unknown = ItineraryScheduleService.compiled_materialization_timing(
        time_window="flexible_daypart",
        start_time=None,
        duration=75,
        intent_type="campus_visit",
    )

    assert exact is not None
    assert exact["startMinutes"] == 10 * 60 + 20
    assert exact["durationMinutes"] == 75
    assert unknown is None


def test_materialization_timing_consumes_server_compiled_semantic_window():
    compiled = ItineraryScheduleService.compiled_materialization_timing(
        time_window="night",
        start_time=None,
        duration=90,
        intent_type="night_view",
    )

    assert compiled is not None
    assert compiled["startMinutes"] == 18 * 60
    assert compiled["scheduleConstraints"]["earliestStart"] == "18:00"


def test_materialization_timing_requires_server_duration_instead_of_defaulting():
    assert (
        ItineraryScheduleService.compiled_materialization_timing(
            time_window="19:10-20:40",
            start_time="19:10",
            duration=None,
            intent_type="night_view",
        )
        is None
    )


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
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


def test_recompute_day_schedule_uses_selected_routes_to_shift_downstream_segments():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection)

        updated = ItineraryScheduleService(connection).recompute_day_schedule("plan_schedule", "day_1")
        rows = connection.execute(
            """
            SELECT id, start_time, end_time
            FROM itinerary_segments
            WHERE plan_id = ?
            ORDER BY segment_order
            """,
            ("plan_schedule",),
        ).fetchall()

    assert updated == 2
    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "09:00", "10:00"),
        ("seg_2", "11:10", "12:10"),
        ("seg_3", "12:40", "13:40"),
    ]


def test_recompute_after_route_selection_uses_the_new_selected_route_duration():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection, first_route_duration_seconds=15 * 60)

        updated = ItineraryScheduleService(connection).recompute_after_route_selection("plan_schedule", "route_1")
        rows = connection.execute(
            """
            SELECT id, start_time, end_time
            FROM itinerary_segments
            WHERE plan_id = ?
            ORDER BY segment_order
            """,
            ("plan_schedule",),
        ).fetchall()

    assert updated == 2
    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "09:00", "10:00"),
        ("seg_2", "10:25", "11:25"),
        ("seg_3", "11:55", "12:55"),
    ]


def test_recompute_day_schedule_lets_faster_route_pull_downstream_before_min_gap():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection, first_route_duration_seconds=4 * 60)

        updated = ItineraryScheduleService(connection).recompute_day_schedule("plan_schedule", "day_1")
        rows = connection.execute(
            """
            SELECT id, start_time, end_time
            FROM itinerary_segments
            WHERE plan_id = ?
            ORDER BY segment_order
            """,
            ("plan_schedule",),
        ).fetchall()

    assert updated == 2
    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "09:00", "10:00"),
        ("seg_2", "10:14", "11:14"),
        ("seg_3", "11:44", "12:44"),
    ]


def test_recompute_day_schedule_manual_first_segment_time_shifts_downstream():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection, first_route_duration_seconds=15 * 60)
        connection.execute(
            "UPDATE itinerary_segments SET start_time = ?, end_time = ? WHERE id = ?",
            ("10:00", "11:00", "seg_1"),
        )
        connection.commit()

        updated = ItineraryScheduleService(connection).recompute_day_schedule("plan_schedule", "day_1")
        rows = connection.execute(
            """
            SELECT id, start_time, end_time
            FROM itinerary_segments
            WHERE plan_id = ?
            ORDER BY segment_order
            """,
            ("plan_schedule",),
        ).fetchall()

    assert updated == 2
    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "10:00", "11:00"),
        ("seg_2", "11:25", "12:25"),
        ("seg_3", "12:55", "13:55"),
    ]


def test_recompute_day_schedule_route_bridge_keeps_pending_meal_non_overlapping():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection, first_route_duration_seconds=20 * 60)
        connection.execute(
            "UPDATE itinerary_segments SET kind = ?, notes = ? WHERE id = ?",
            ("meal", "groundingStatus：optional_waiting；routeAnchor=false。", "seg_2"),
        )
        connection.execute("DELETE FROM route_options WHERE id IN (?, ?)", ("route_1", "route_2"))
        insert_route(connection, "route_direct", "seg_1", "seg_3", 20 * 60)
        connection.commit()

        ItineraryScheduleService(connection).recompute_day_schedule("plan_schedule", "day_1")
        rows = connection.execute(
            """
            SELECT id, start_time, end_time
            FROM itinerary_segments
            WHERE plan_id = ?
            ORDER BY segment_order
            """,
            ("plan_schedule",),
        ).fetchall()

    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "09:00", "10:00"),
        ("seg_3", "10:30", "11:30"),
        ("seg_2", "12:00", "13:00"),
    ]


def test_recompute_day_schedule_reorders_route_shifted_segments_by_start_time():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection, first_route_duration_seconds=20 * 60)
        connection.execute(
            "UPDATE itinerary_segments SET start_time = ?, end_time = ? WHERE id = ?",
            ("17:30", "18:30", "seg_2"),
        )
        connection.execute(
            "UPDATE itinerary_segments SET kind = ?, notes = ? WHERE id = ?",
            ("meal", "groundingStatus：optional_waiting；routeAnchor=false。", "seg_2"),
        )
        connection.execute(
            "UPDATE itinerary_segments SET start_time = ?, end_time = ? WHERE id = ?",
            ("19:00", "20:00", "seg_3"),
        )
        connection.execute("DELETE FROM route_options WHERE id IN (?, ?)", ("route_1", "route_2"))
        insert_route(connection, "route_direct", "seg_1", "seg_3", 20 * 60)
        connection.commit()

        ItineraryScheduleService(connection).recompute_day_schedule("plan_schedule", "day_1")
        rows = connection.execute(
            "SELECT id, segment_order, start_time, end_time FROM itinerary_segments WHERE plan_id = ? ORDER BY segment_order",
            ("plan_schedule",),
        ).fetchall()

    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "09:00", "10:00"),
        ("seg_3", "10:30", "11:30"),
        ("seg_2", "17:30", "18:30"),
    ]
    assert [row["segment_order"] for row in rows] == [1, 2, 3]


def test_recompute_plan_schedule_cascades_impacted_day_when_route_is_temporarily_missing():
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection)
        connection.execute("DELETE FROM route_options WHERE plan_id = ?", ("plan_schedule",))
        connection.commit()

        updated = ItineraryScheduleService(connection).recompute_plan_schedule(
            "plan_schedule", route_pairs={("seg_1", "seg_2")}
        )
        rows = connection.execute(
            "SELECT id, start_time, end_time, estimate_metadata_json FROM itinerary_segments WHERE plan_id = ? ORDER BY segment_order",
            ("plan_schedule",),
        ).fetchall()

    assert updated == 0
    assert [(row["id"], row["start_time"], row["end_time"]) for row in rows] == [
        ("seg_1", "09:00", "10:00"),
        ("seg_2", "12:00", "13:00"),
        ("seg_3", "14:00", "15:00"),
    ]
    assert json.loads(rows[1]["estimate_metadata_json"])["schedule"] == {
        "status": "provisional_missing_route",
        "routeBufferMinutes": 0,
    }


def test_project_snapshot_schedule_waits_for_hard_night_window() -> None:
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "campus",
                        "startTime": "09:00",
                        "endTime": "10:00",
                        "kind": "visit",
                        "semanticMetadata": {"intentType": "campus_visit"},
                    },
                    {
                        "id": "night",
                        "startTime": "20:00",
                        "endTime": "21:15",
                        "kind": "night_view",
                        "semanticMetadata": {
                            "intentType": "night_view",
                            "scheduleConstraints": {
                                "earliestStart": "20:00",
                                "latestStart": "20:00",
                                "windowEnd": "21:15",
                                "source": "planning_slot",
                                "hard": True,
                            },
                        },
                    },
                ],
            }
        ]
    }
    ItineraryScheduleService.project_snapshot_schedule(
        snapshot,
        [
            {
                "status": "verified",
                "fromSegmentId": "campus",
                "toSegmentId": "night",
                "durationMinutes": 4,
            }
        ],
    )

    night = snapshot["days"][0]["segments"][1]
    assert (night["startTime"], night["endTime"]) == ("20:00", "21:15")
    assert night["semanticMetadata"]["schedule"]["constraintPassed"] is True


def test_project_snapshot_schedule_rejects_route_arrival_after_latest_start() -> None:
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "campus",
                        "startTime": "19:00",
                        "endTime": "20:00",
                        "kind": "visit",
                        "semanticMetadata": {"intentType": "campus_visit"},
                    },
                    {
                        "id": "night",
                        "startTime": "20:00",
                        "endTime": "21:15",
                        "kind": "night_view",
                        "semanticMetadata": {
                            "intentType": "night_view",
                            "scheduleConstraints": {
                                "earliestStart": "20:00",
                                "latestStart": "20:00",
                                "windowEnd": "21:15",
                                "source": "planning_slot",
                                "hard": True,
                            },
                        },
                    },
                ],
            }
        ]
    }

    try:
        ItineraryScheduleService.project_snapshot_schedule(
            snapshot,
            [
                {
                    "status": "verified",
                    "fromSegmentId": "campus",
                    "toSegmentId": "night",
                    "durationMinutes": 20,
                }
            ],
        )
    except ValueError as error:
        assert "schedule_window_latest_start_exceeded:night" in str(error)
    else:
        raise AssertionError("late route arrival must not confirm the night segment")


def test_project_snapshot_schedule_shifts_soft_experience_after_verified_route() -> None:
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "campus",
                        "startTime": "09:00",
                        "endTime": "11:45",
                        "kind": "visit",
                        "semanticMetadata": {"intentType": "campus_visit"},
                    },
                    {
                        "id": "meal",
                        "startTime": "12:00",
                        "endTime": "13:15",
                        "kind": "meal",
                        "semanticMetadata": {
                            "intentType": "local_food",
                            "requirementLevel": "soft_experience",
                            "scheduleConstraints": {
                                "earliestStart": "12:00",
                                "latestStart": "12:00",
                                "windowEnd": "13:15",
                                "source": "planning_slot",
                                "hard": False,
                            },
                        },
                    },
                ],
            }
        ]
    }

    ItineraryScheduleService.project_snapshot_schedule(
        snapshot,
        [
            {
                "status": "verified",
                "fromSegmentId": "campus",
                "toSegmentId": "meal",
                "durationMinutes": 20,
            }
        ],
    )

    meal = snapshot["days"][0]["segments"][1]
    assert (meal["startTime"], meal["endTime"]) == ("12:15", "13:30")
    assert meal["semanticMetadata"]["schedule"]["status"] == "committed_route_evidence"


def test_project_snapshot_schedule_keeps_controller_daypart_floor_when_real_route_exceeds_estimate() -> None:
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "campus",
                        "startTime": "09:00",
                        "endTime": "12:00",
                        "kind": "visit",
                        "semanticMetadata": {"intentType": "campus_visit"},
                    },
                    {
                        "id": "meal",
                        "startTime": "12:00",
                        "endTime": "13:30",
                        "kind": "meal",
                        "semanticMetadata": {
                            "intentType": "local_food",
                            "routeAnchor": False,
                            "scheduleConstraints": {
                                "earliestStart": "12:00",
                                "latestStart": "12:30",
                                "windowEnd": "14:00",
                                "source": "controller_schedule_hint_daypart",
                                "hard": False,
                            },
                        },
                    },
                    {
                        "id": "park",
                        "startTime": "18:00",
                        "endTime": "20:00",
                        "kind": "park",
                        "semanticMetadata": {
                            "intentType": "park",
                            "scheduleConstraints": {
                                "earliestStart": "18:00",
                                "latestStart": "20:00",
                                "windowEnd": "22:00",
                                "source": "controller_schedule_hint_daypart",
                                "hard": False,
                            },
                        },
                    },
                ],
            }
        ]
    }

    ItineraryScheduleService.project_snapshot_schedule(
        snapshot,
        [
            {
                "status": "verified",
                "fromSegmentId": "campus",
                "toSegmentId": "meal",
                "durationMinutes": 31,
            },
            {
                "status": "verified",
                "fromSegmentId": "meal",
                "toSegmentId": "park",
                "durationMinutes": 20,
            },
        ],
    )

    meal, park = snapshot["days"][0]["segments"][1:]
    assert (meal["startTime"], meal["endTime"]) == ("12:41", "14:11")
    assert (park["startTime"], park["endTime"]) == ("18:00", "20:00")
    assert meal["semanticMetadata"]["schedule"]["constraintPassed"] is True
    assert park["semanticMetadata"]["schedule"]["constraintPassed"] is True


def test_recompute_day_schedule_uses_same_hard_window_kernel_as_projection() -> None:
    clear_database()
    with open_db() as connection:
        seed_schedule_fixture(connection, first_route_duration_seconds=4 * 60)
        constraint = {
            "intentType": "night_view",
            "scheduleConstraints": {
                "earliestStart": "20:00",
                "latestStart": "20:00",
                "windowEnd": "21:00",
                "source": "planning_slot",
                "hard": True,
            },
        }
        connection.execute(
            "UPDATE itinerary_segments SET start_time = ?, end_time = ?, kind = ?, estimate_metadata_json = ? WHERE id = ?",
            ("20:00", "21:00", "night_view", json.dumps(constraint), "seg_2"),
        )
        connection.commit()

        ItineraryScheduleService(connection).recompute_day_schedule("plan_schedule", "day_1")
        row = connection.execute(
            "SELECT start_time, end_time, estimate_metadata_json FROM itinerary_segments WHERE id = ?",
            ("seg_2",),
        ).fetchone()

    assert (row["start_time"], row["end_time"]) == ("20:00", "21:00")
    assert json.loads(row["estimate_metadata_json"])["schedule"]["constraintPassed"] is True


def seed_schedule_fixture(connection: sqlite3.Connection, *, first_route_duration_seconds: int = 60 * 60) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO itinerary_plans (
            id, user_id, inspiration_set_id, template_type, title, city,
            budget_target, budget_estimate, budget_delta_explanation,
            decision_rationale, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "plan_schedule",
            "user_test",
            "insp_test",
            "custom",
            "Schedule test",
            "Beijing",
            None,
            0,
            "",
            "",
            "draft",
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO itinerary_days (
            id, plan_id, day_number, date, weather_summary,
            risk_summary, total_estimated_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("day_1", "plan_schedule", 1, None, "", "", 0),
    )
    for poi_id, name in [("poi_1", "A"), ("poi_2", "B"), ("poi_3", "C")]:
        connection.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (poi_id, "plan_schedule", name, "Beijing", "scenic", None, None, None, "test", 1),
        )
    for segment_id, order, start_time, end_time, poi_id in [
        ("seg_1", 1, "09:00", "10:00", "poi_1"),
        ("seg_2", 2, "12:00", "13:00", "poi_2"),
        ("seg_3", 3, "14:00", "15:00", "poi_3"),
    ]:
        connection.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time,
                end_time, poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                "plan_schedule",
                "day_1",
                order,
                "activity",
                start_time,
                end_time,
                poi_id,
                "walking",
                0,
                "",
                None,
                None,
                None,
            ),
        )
    insert_route(connection, "route_1", "seg_1", "seg_2", first_route_duration_seconds)
    insert_route(connection, "route_2", "seg_2", "seg_3", 20 * 60)
    connection.commit()


def insert_route(
    connection: sqlite3.Connection,
    route_id: str,
    from_segment_id: str,
    to_segment_id: str,
    duration_seconds: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO route_options (
            id, plan_id, from_segment_id, to_segment_id, from_poi_id, to_poi_id,
            provider, mode, label, is_selected, sort_order, transport_mode,
            distance_meters, duration_seconds, duration_minutes, cost_amount,
            cost_currency, cost_estimate, crowding_risk, source, polyline_json,
            steps_json, provider_payload_json, error_json, queried_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            route_id,
            "plan_schedule",
            from_segment_id,
            to_segment_id,
            "poi_1" if from_segment_id == "seg_1" else "poi_2",
            "poi_2" if to_segment_id == "seg_2" else "poi_3",
            "test",
            "taxi",
            "Taxi",
            1,
            1,
            "taxi",
            1000,
            duration_seconds,
            round(duration_seconds / 60),
            0,
            "CNY",
            0,
            "low",
            "test",
            "[]",
            "[]",
            "{}",
            None,
            now,
        ),
    )

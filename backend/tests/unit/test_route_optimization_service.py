import sqlite3
from datetime import datetime, timezone

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.route_optimization_service import RouteOptimizationService


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


def test_route_optimization_switches_similar_distance_shorter_duration():
    clear_database()
    with open_db() as connection:
        seed_route_fixture(connection)

        result = RouteOptimizationService(connection).optimize_plan_routes("plan_opt", preference={"preferredMode": "transit"})
        selected = connection.execute(
            "SELECT id FROM route_options WHERE plan_id = ? AND is_selected = 1",
            ("plan_opt",),
        ).fetchall()

    assert result.changed_count == 1
    assert result.changes[0].from_route_id == "route_transit"
    assert result.changes[0].to_route_id == "route_taxi"
    assert "similar_distance" in result.changes[0].reasons
    assert "shorter_duration" in result.changes[0].reasons
    assert [row["id"] for row in selected] == ["route_taxi"]


def test_route_optimization_fastest_selects_fastest_acceptable_route_and_warns_cost():
    clear_database()
    with open_db() as connection:
        seed_route_fixture(connection)
        insert_route(connection, "route_fast_expensive", "driving", 5400, 18 * 60, 60, False)

        result = RouteOptimizationService(connection).optimize_plan_routes(
            "plan_opt",
            preference={"preferredMode": "transit"},
            objective="fastest",
        )
        selected = connection.execute(
            "SELECT id FROM route_options WHERE plan_id = ? AND is_selected = 1",
            ("plan_opt",),
        ).fetchall()

    assert result.as_metadata()["objective"] == "fastest"
    assert result.changed_count == 1
    assert result.changes[0].to_route_id == "route_fast_expensive"
    assert result.warnings == ["faster_route_costs_more"]
    assert "objective_fastest" in result.changes[0].reasons
    assert [row["id"] for row in selected] == ["route_fast_expensive"]


def test_route_optimization_cheapest_selects_lower_cost_without_large_time_penalty():
    clear_database()
    with open_db() as connection:
        seed_route_fixture(connection, selected_route_id="route_taxi")

        result = RouteOptimizationService(connection).optimize_plan_routes(
            "plan_opt",
            preference={"preferredMode": "transit"},
            objective="cheapest",
        )
        selected = connection.execute(
            "SELECT id FROM route_options WHERE plan_id = ? AND is_selected = 1",
            ("plan_opt",),
        ).fetchall()

    assert result.as_metadata()["objective"] == "cheapest"
    assert result.changed_count == 1
    assert result.changes[0].from_route_id == "route_taxi"
    assert result.changes[0].to_route_id == "route_transit"
    assert "objective_cheapest" in result.changes[0].reasons
    assert [row["id"] for row in selected] == ["route_transit"]


def test_route_optimization_cheapest_does_not_auto_replace_with_long_free_walk():
    clear_database()
    with open_db() as connection:
        seed_route_fixture(connection, selected_route_id="route_taxi")
        insert_route(connection, "route_walk_long", "walking", 3200, 40 * 60, 0, False)
        connection.execute("UPDATE route_options SET cost_amount = 30, cost_estimate = 30 WHERE id = ?", ("route_taxi",))
        connection.execute("UPDATE route_options SET duration_seconds = ?, duration_minutes = ? WHERE id = ?", (50 * 60, 50, "route_transit"))
        connection.commit()

        result = RouteOptimizationService(connection).optimize_plan_routes(
            "plan_opt",
            preference={"preferredMode": "transit"},
            objective="cheapest",
        )
        selected = connection.execute(
            "SELECT id FROM route_options WHERE plan_id = ? AND is_selected = 1",
            ("plan_opt",),
        ).fetchall()

    assert result.changed_count == 0
    assert [row["id"] for row in selected] == ["route_taxi"]


def test_route_optimization_keeps_selected_when_alternative_is_much_farther():
    clear_database()
    with open_db() as connection:
        seed_route_fixture(connection, taxi_distance=9000)

        result = RouteOptimizationService(connection).optimize_plan_routes("plan_opt", preference={"preferredMode": "transit"})
        selected = connection.execute(
            "SELECT id FROM route_options WHERE plan_id = ? AND is_selected = 1",
            ("plan_opt",),
        ).fetchall()

    assert result.changed_count == 0
    assert [row["id"] for row in selected] == ["route_transit"]


def seed_route_fixture(connection: sqlite3.Connection, *, taxi_distance: int = 5400, selected_route_id: str = "route_transit") -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO itinerary_plans (
            id, user_id, inspiration_set_id, template_type, title, city,
            budget_target, budget_estimate, budget_delta_explanation,
            decision_rationale, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("plan_opt", "user_test", "insp_test", "custom", "Route optimize", "北京", None, 0, "", "", "draft", now, now),
    )
    connection.execute(
        """
        INSERT INTO itinerary_days (
            id, plan_id, day_number, date, weather_summary,
            risk_summary, total_estimated_cost
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("day_1", "plan_opt", 1, None, "", "", 0),
    )
    for poi_id, name in [("poi_1", "A"), ("poi_2", "B")]:
        connection.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (poi_id, "plan_opt", name, "北京", "scenic", None, None, None, "test", 1),
        )
    for segment_id, order, poi_id in [("seg_1", 1, "poi_1"), ("seg_2", 2, "poi_2")]:
        connection.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time,
                end_time, poi_id, transport_mode, estimated_cost, notes,
                weather_signal_id, traffic_crowding_signal_id,
                ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (segment_id, "plan_opt", "day_1", order, "activity", "09:00", "10:00", poi_id, "transit", 0, "", None, None, None),
        )
    insert_route(connection, "route_transit", "transit", 5000, 35 * 60, 4, selected_route_id == "route_transit")
    insert_route(connection, "route_taxi", "taxi", taxi_distance, 25 * 60, 20, selected_route_id == "route_taxi")
    connection.commit()


def insert_route(
    connection: sqlite3.Connection,
    route_id: str,
    mode: str,
    distance_meters: int,
    duration_seconds: int,
    cost: float,
    selected: bool,
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
            "plan_opt",
            "seg_1",
            "seg_2",
            "poi_1",
            "poi_2",
            "test",
            mode,
            mode,
            1 if selected else 0,
            1,
            mode,
            distance_meters,
            duration_seconds,
            round(duration_seconds / 60),
            cost,
            "CNY",
            cost,
            "low",
            "test",
            "[[116,39],[116.1,39.1]]",
            "[]",
            "{}",
            None,
            now,
        ),
    )

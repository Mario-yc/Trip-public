import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app
from src.services.agent_verifier_service import AgentVerifierService
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.route_insertion_scorer import RouteInsertionScorer


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM planning_runs;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_sessions;
            DELETE FROM traffic_crowding_signals;
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


def test_route_selection_persists_selected_route_and_version():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_select")
    client = TestClient(app)

    stale = client.post(
        "/api/itineraries/plan_select/routes/route_taxi/select",
        json={"baseVersionId": "ver_old"},
    )
    assert stale.status_code == 409

    response = client.post(
        "/api/itineraries/plan_select/routes/route_taxi/select",
        json={
            "baseVersionId": base_version_id,
            "preferenceSummary": "用户偏好公共交通，但这段可接受打车省力。",
            "planningContext": {
                "routeGroup": {
                    "label": "驾车/打车",
                    "rawRouteIds": ["route_taxi", "route_drive"],
                    "representativeRouteId": "route_taxi",
                    "selectedRawMode": "taxi",
                },
                "routeDecisionContract": route_decision_contract("client_attempt"),
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    route_options = body["itinerary"]["routeOptions"]
    assert all(route["status"] == ("failed" if route["error"] else "verified") for route in route_options)
    assert next(route for route in route_options if route["id"] == "route_taxi")["isSelected"] is True
    assert next(route for route in route_options if route["id"] == "route_walk")["isSelected"] is False
    assert body["itinerary"]["days"][0]["segments"][1]["startTime"] == "11:25"
    assert body["itinerary"]["days"][0]["segments"][1]["endTime"] == "13:25"
    assert body["version"]["id"].startswith("ver_")
    assert body["planningRun"]["runType"] == "route_select"
    assert body["planningRun"]["preferenceSummary"] == "用户偏好公共交通，但这段可接受打车省力。"
    assert any(tool["id"] == "map-route" for tool in body["planningRun"]["toolCalls"])
    assert body["planningRun"]["feasibilityReport"]["score"] <= 100

    with open_db() as connection:
        selected_rows = connection.execute(
            "SELECT id FROM route_options WHERE plan_id = ? AND is_selected = 1",
            ("plan_select",),
        ).fetchall()
        patch = connection.execute("SELECT * FROM itinerary_patches ORDER BY created_at DESC LIMIT 1").fetchone()
        version = connection.execute("SELECT * FROM itinerary_versions ORDER BY version_number DESC LIMIT 1").fetchone()
        run = connection.execute("SELECT * FROM planning_runs WHERE run_type = 'route_select'").fetchone()

    assert [row["id"] for row in selected_rows] == ["route_taxi"]
    assert "select_route" in patch["operations_json"]
    assert "routeGroup" in patch["operations_json"]
    assert "route_taxi" in patch["operations_json"]
    assert run is not None
    snapshot = json.loads(version["snapshot_json"])
    assert next(route for route in snapshot["routeOptions"] if route["id"] == "route_taxi")["isSelected"] is True
    assert next(route for route in snapshot["routeOptions"] if route["id"] == "route_walk")["isSelected"] is False
    assert snapshot["days"][0]["segments"][1]["startTime"] == "11:25"
    assert snapshot["routeDecisionContract"] == route_decision_contract("server_active_version")
    assert snapshot["routeMatrixExpectedPairs"] == [["seg_1", "seg_2"]]
    assert len(snapshot["routeInsertionProofs"]) == 1
    proof = snapshot["routeInsertionProofs"][0]
    assert proof["operation"] == "select_route_final_route_pair"
    assert proof["status"] == "passed"
    assert proof["detourLevel"] != "not_applicable"
    assert proof["routeSelectionChanged"] is True
    assert proof["scheduleSlackMinutes"] == 10
    assert proof["scheduleProjectionRequired"] is False
    assert proof["routeDecisionContract"] == snapshot["routeDecisionContract"]
    leg = proof["legs"]["previousToCandidate"]
    assert {
        key: leg[key]
        for key in (
            "fromSegmentId",
            "toSegmentId",
            "fromAmapId",
            "toAmapId",
            "provider",
            "distanceMeters",
            "durationSeconds",
        )
    } == {
        "fromSegmentId": "seg_1",
        "toSegmentId": "seg_2",
        "fromAmapId": "B000A8UIN8",
        "toAmapId": "B000A8UIN9",
        "provider": "amap-webservice",
        "distanceMeters": 1800,
        "durationSeconds": 900,
    }
    assert_route_proof_score_recomputes(proof)


def test_route_response_fails_closed_for_unknown_provider_status():
    clear_database()
    seed_plan_with_routes("plan_status")
    with open_db() as connection:
        connection.execute(
            "UPDATE route_options SET provider_payload_json = ? WHERE id = ?",
            (json.dumps({"status": "mystery_status"}), "route_taxi"),
        )
        connection.execute(
            "UPDATE route_options SET provider_payload_json = ? WHERE id = ?",
            (json.dumps({"status": "1"}), "route_walk"),
        )
        connection.commit()
        plan = ItineraryService(connection).get_plan("plan_status")

    status_by_id = {route.id: route.status for route in plan.route_options}
    assert status_by_id["route_taxi"] == "needs_refresh"
    assert status_by_id["route_walk"] == "verified"


def test_public_route_selection_cannot_promote_client_route_contract_and_is_zero_write():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_public_contract", with_route_contract=False)
    with open_db() as connection:
        before = route_write_state(connection, "plan_public_contract")

    response = TestClient(app).post(
        "/api/itineraries/plan_public_contract/routes/route_taxi/select",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {
                "routeDecisionContract": {
                    "contractVersion": "client-supplied-must-not-be-trusted",
                }
            },
        },
    )

    assert response.status_code == 409
    with open_db() as connection:
        assert route_write_state(connection, "plan_public_contract") == before


def test_public_route_selection_rejects_stale_provider_row_and_rolls_back_every_write():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_stale_select")
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with open_db() as connection:
        connection.execute(
            "UPDATE route_options SET queried_at = ? WHERE id = 'route_taxi'",
            (stale,),
        )
        connection.commit()
        before = route_write_state(connection, "plan_stale_select")

    response = TestClient(app).post(
        "/api/itineraries/plan_stale_select/routes/route_taxi/select",
        json={"baseVersionId": base_version_id},
    )

    assert response.status_code == 409
    assert "provider_route_query_stale" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_stale_select") == before


def test_public_route_selection_rolls_back_when_final_matrix_verifier_rejects(monkeypatch):
    clear_database()
    base_version_id = seed_plan_with_routes("plan_verifier_rollback")
    monkeypatch.setattr(
        AgentVerifierService,
        "validate_route_matrix_snapshot_before_write",
        lambda *_args, **_kwargs: ["forced_final_matrix_failure"],
    )
    with open_db() as connection:
        before = route_write_state(connection, "plan_verifier_rollback")

    response = TestClient(app).post(
        "/api/itineraries/plan_verifier_rollback/routes/route_taxi/select",
        json={"baseVersionId": base_version_id},
    )

    assert response.status_code == 409
    assert "forced_final_matrix_failure" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_verifier_rollback") == before


def test_public_route_selection_rejects_invalid_active_contract_without_mutation():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_invalid_contract")
    with open_db() as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (base_version_id,),
        ).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["routeDecisionContract"] = {"contractVersion": "tampered"}
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), base_version_id),
        )
        connection.commit()
        before = route_write_state(connection, "plan_invalid_contract")

    response = TestClient(app).post(
        "/api/itineraries/plan_invalid_contract/routes/route_taxi/select",
        json={"baseVersionId": base_version_id},
    )

    assert response.status_code == 409
    assert "active_version_route_decision_contract_invalid" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_invalid_contract") == before


def test_route_optimization_switches_better_route_and_recomputes_schedule():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_select")
    client = TestClient(app)

    response = client.post(
        "/api/itineraries/plan_select/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "preferenceSummary": "用户公交地铁优先，但可接受明显更快路线。",
            "optimizationObjective": "fastest",
        },
    )

    assert response.status_code == 200
    body = response.json()
    selected = [route["id"] for route in body["itinerary"]["routeOptions"] if route["isSelected"]]
    assert selected == ["route_taxi"]
    assert body["itinerary"]["days"][0]["segments"][1]["startTime"] == "11:25"
    assert body["patch"]["metadata"]["routeOptimization"]["changedCount"] == 1
    assert body["patch"]["metadata"]["routeOptimization"]["objective"] == "fastest"
    assert body["patch"]["metadata"]["scheduleUpdatedCount"] > 0
    change = body["patch"]["metadata"]["routeOptimization"]["changes"][0]
    assert change["fromRouteId"] == "route_walk"
    assert change["toRouteId"] == "route_taxi"
    assert "shorter_duration" in change["reasons"]
    assert body["planningRun"]["runType"] == "route_optimize"
    assert body["planningRun"]["understoodRequirements"]["routeOptimization"]["changedCount"] == 1
    assert body["planningRun"]["understoodRequirements"]["optimizationObjective"] == "fastest"

    with open_db() as connection:
        patch = connection.execute("SELECT * FROM itinerary_patches ORDER BY created_at DESC LIMIT 1").fetchone()
        version = connection.execute("SELECT * FROM itinerary_versions ORDER BY version_number DESC LIMIT 1").fetchone()

    assert "optimize_routes" in patch["operations_json"]
    snapshot = json.loads(version["snapshot_json"])
    assert next(route for route in snapshot["routeOptions"] if route["id"] == "route_taxi")["isSelected"] is True
    assert snapshot["days"][0]["segments"][1]["startTime"] == "11:25"
    assert snapshot["routeMatrixExpectedPairs"] == [["seg_1", "seg_2"]]
    assert snapshot["routeInsertionProofs"][0]["operation"] == "optimize_routes_final_route_pair"
    assert snapshot["routeInsertionProofs"][0]["scheduleSlackMinutes"] == 10
    assert snapshot["routeInsertionProofs"][0]["legs"]["previousToCandidate"]["mode"] == "taxi"
    assert snapshot["routeInsertionProofs"][0]["routeSelectionChanged"] is True
    assert_route_proof_score_recomputes(snapshot["routeInsertionProofs"][0])


@pytest.mark.parametrize(
    ("column", "value", "expected_code"),
    [
        ("provider", "haversine", "provider_route_source_invalid"),
        ("distance_meters", 0, "provider_route_metrics_invalid"),
        ("provider_payload_json", '{"status":"mystery_status"}', "provider_route_status_invalid"),
    ],
)
def test_public_route_optimization_rejects_invalid_selected_candidate_with_zero_delta(
    column,
    value,
    expected_code,
):
    clear_database()
    base_version_id = seed_plan_with_routes("plan_invalid_optimize")
    with open_db() as connection:
        connection.execute(
            f"UPDATE route_options SET {column} = ? WHERE id = 'route_taxi'",
            (value,),
        )
        connection.commit()
        before = route_write_state(connection, "plan_invalid_optimize")

    response = TestClient(app).post(
        "/api/itineraries/plan_invalid_optimize/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "optimizationObjective": "fastest",
        },
    )

    assert response.status_code == 409
    assert expected_code in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_invalid_optimize") == before


def test_public_route_optimization_rejects_endpoint_drift_with_zero_delta():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_endpoint_drift")
    with open_db() as connection:
        connection.execute(
            "UPDATE itinerary_segments SET poi_id = 'poi_2' WHERE id = 'seg_1'",
        )
        connection.commit()
        before = route_write_state(connection, "plan_endpoint_drift")

    response = TestClient(app).post(
        "/api/itineraries/plan_endpoint_drift/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "optimizationObjective": "fastest",
        },
    )

    assert response.status_code == 409
    assert "provider_route_endpoints_mismatch" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_endpoint_drift") == before


def test_public_schedule_only_rebuilds_current_slack_instead_of_carrying_old_proof():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_schedule_only")
    with open_db() as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (base_version_id,),
        ).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["routeInsertionProofs"] = [
            {
                "proofType": "adjacent_route_coverage",
                "operation": "stale_schedule_proof",
                "scheduleSlackMinutes": 999,
            }
        ]
        snapshot["routeMatrixExpectedPairs"] = [["seg_1", "seg_2"]]
        connection.execute(
            "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), base_version_id),
        )
        connection.commit()

    response = TestClient(app).post(
        "/api/itineraries/plan_schedule_only/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {"scheduleOnly": True},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["itinerary"]["days"][0]["segments"][1]["startTime"] == "12:10"
    with open_db() as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (body["version"]["id"],),
        ).fetchone()
    current = json.loads(row["snapshot_json"])
    assert current["routeInsertionProofs"][0]["operation"] == "auto_schedule_final_route_pair"
    assert current["routeInsertionProofs"][0]["scheduleSlackMinutes"] == 10
    assert current["routeInsertionProofs"][0]["scheduleProjectionRequired"] is False


def test_public_schedule_only_stale_route_rolls_back_schedule_and_all_writes():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_schedule_stale")
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with open_db() as connection:
        connection.execute(
            "UPDATE route_options SET queried_at = ? WHERE id = 'route_walk'",
            (stale,),
        )
        connection.commit()
        before = route_write_state(connection, "plan_schedule_stale")

    response = TestClient(app).post(
        "/api/itineraries/plan_schedule_stale/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {"scheduleOnly": True},
        },
    )

    assert response.status_code == 409
    assert "provider_route_query_stale" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_schedule_stale") == before


def test_public_schedule_only_rebuilds_exact_three_anchor_adjacent_pairs():
    clear_database()
    base_version_id = seed_plan_with_routes(
        "plan_three_anchor_complete",
        route_topology="complete_adjacent",
    )

    response = TestClient(app).post(
        "/api/itineraries/plan_three_anchor_complete/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {"scheduleOnly": True},
        },
    )

    assert response.status_code == 200
    version_id = response.json()["version"]["id"]
    with open_db() as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["routeMatrixExpectedPairs"] == [
        ["seg_1", "seg_2"],
        ["seg_2", "seg_3"],
    ]
    proofs = snapshot["routeInsertionProofs"]
    assert [(proof["fromSegmentId"], proof["segmentId"]) for proof in proofs] == [
        ("seg_1", "seg_2"),
        ("seg_2", "seg_3"),
    ]
    assert all(proof["routeSelectionChanged"] is False for proof in proofs)
    assert all(proof["generalizedCostDelta"] == 0 for proof in proofs)
    assert all(proof["detourRatio"] == 0 for proof in proofs)
    for proof in proofs:
        assert_route_proof_score_recomputes(proof)


def test_public_schedule_only_rejects_extra_non_adjacent_selected_pair_with_zero_delta():
    clear_database()
    base_version_id = seed_plan_with_routes(
        "plan_extra_selected_pair",
        route_topology="extra_non_adjacent",
    )
    with open_db() as connection:
        before = route_write_state(connection, "plan_extra_selected_pair")

    response = TestClient(app).post(
        "/api/itineraries/plan_extra_selected_pair/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {"scheduleOnly": True},
        },
    )

    assert response.status_code == 409
    assert "provider_route_selected_pairs_mismatch" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_extra_selected_pair") == before


def test_public_schedule_only_rejects_duplicate_adjacent_selected_pair_with_zero_delta():
    clear_database()
    base_version_id = seed_plan_with_routes(
        "plan_duplicate_selected_pair",
        route_topology="duplicate_adjacent",
    )
    with open_db() as connection:
        before = route_write_state(connection, "plan_duplicate_selected_pair")

    response = TestClient(app).post(
        "/api/itineraries/plan_duplicate_selected_pair/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {"scheduleOnly": True},
        },
    )

    assert response.status_code == 409
    assert "provider_route_selected_pair_ambiguous" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_duplicate_selected_pair") == before


def test_public_schedule_only_rejects_missing_adjacent_selected_pair_with_zero_delta():
    clear_database()
    base_version_id = seed_plan_with_routes(
        "plan_missing_selected_pair",
        route_topology="missing_adjacent",
    )
    with open_db() as connection:
        before = route_write_state(connection, "plan_missing_selected_pair")

    response = TestClient(app).post(
        "/api/itineraries/plan_missing_selected_pair/routes/optimize",
        json={
            "baseVersionId": base_version_id,
            "planningContext": {"scheduleOnly": True},
        },
    )

    assert response.status_code == 409
    assert "provider_route_selected_pairs_mismatch" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_missing_selected_pair") == before


def test_public_route_selection_cannot_repair_pair_without_selected_provider_baseline():
    clear_database()
    base_version_id = seed_plan_with_routes(
        "plan_missing_baseline",
        route_topology="missing_adjacent",
    )
    with open_db() as connection:
        before = route_write_state(connection, "plan_missing_baseline")

    response = TestClient(app).post(
        "/api/itineraries/plan_missing_baseline/routes/route_bc/select",
        json={"baseVersionId": base_version_id},
    )

    assert response.status_code == 409
    assert "provider_route_baseline_pairs_mismatch" in response.text
    with open_db() as connection:
        assert route_write_state(connection, "plan_missing_baseline") == before


@pytest.mark.parametrize("operation", ["select", "optimize"])
def test_public_route_writer_rejects_provider_route_exceeding_server_contract_with_zero_delta(operation):
    clear_database()
    plan_id = f"plan_contract_exceeded_{operation}"
    strict_contract = route_decision_contract(
        "strict_server_contract",
        max_detour_ratio=0.05,
    )
    base_version_id = seed_plan_with_routes(
        plan_id,
        route_contract=strict_contract,
    )
    set_route_cost_components(
        "route_taxi",
        walking_distance_meters=3_000,
        transfer_count=8,
    )
    with open_db() as connection:
        before = route_write_state(connection, plan_id)

    if operation == "select":
        response = TestClient(app).post(
            f"/api/itineraries/{plan_id}/routes/route_taxi/select",
            json={"baseVersionId": base_version_id},
        )
    else:
        response = TestClient(app).post(
            f"/api/itineraries/{plan_id}/routes/optimize",
            json={
                "baseVersionId": base_version_id,
                "optimizationObjective": "fastest",
            },
        )

    assert response.status_code == 409
    assert "provider_route_decision_contract_exceeded" in response.text
    with open_db() as connection:
        assert route_write_state(connection, plan_id) == before


def test_public_route_selection_accepts_wider_server_contract_with_recomputable_proof():
    clear_database()
    wide_contract = route_decision_contract(
        "wide_server_contract",
        max_detour_ratio=0.2,
    )
    base_version_id = seed_plan_with_routes(
        "plan_wide_contract",
        route_contract=wide_contract,
    )
    set_route_cost_components(
        "route_taxi",
        walking_distance_meters=3_000,
        transfer_count=8,
    )

    response = TestClient(app).post(
        "/api/itineraries/plan_wide_contract/routes/route_taxi/select",
        json={"baseVersionId": base_version_id},
    )

    assert response.status_code == 200
    version_id = response.json()["version"]["id"]
    with open_db() as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
    snapshot = json.loads(row["snapshot_json"])
    proof = snapshot["routeInsertionProofs"][0]
    assert proof["routeDecisionContract"] == wide_contract
    assert proof["detourLevel"] == "medium"
    assert proof["generalizedCostDelta"] == pytest.approx(5.16)
    assert proof["detourRatio"] == pytest.approx(0.0816)
    assert_route_proof_score_recomputes(proof)


def test_route_selection_rejects_unavailable_route():
    clear_database()
    base_version_id = seed_plan_with_routes("plan_select")
    client = TestClient(app)

    response = client.post(
        "/api/itineraries/plan_select/routes/route_error/select",
        json={"baseVersionId": base_version_id},
    )

    assert response.status_code == 400
    assert "unavailable route" in response.json()["detail"]


def route_decision_contract(
    source_turn_id: str,
    *,
    max_generalized_cost_delta: float = 35.0,
    max_detour_ratio: float = 0.35,
) -> dict:
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"sourceAssistantTurnId": source_turn_id},
        detour_tolerance={
            "maxGeneralizedCostDelta": max_generalized_cost_delta,
            "maxDetourRatio": max_detour_ratio,
        },
        mobility_profile={
            "source": "route_selection_contract_fixture",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert contract is not None
    return contract


def assert_route_proof_score_recomputes(proof: dict) -> None:
    contract = proof["routeDecisionContract"]
    legs = proof["legs"]
    score = RouteInsertionScorer().score_from_route_matrix(
        previous_to_candidate=legs["previousToCandidate"],
        candidate_to_next=None,
        previous_to_next=legs["previousToNext"],
        detour_tolerance=contract["detourTolerance"],
        schedule_slack_minutes=proof["scheduleSlackMinutes"],
        time_window_feasible=proof["timeWindowFeasible"],
        mobility_profile=contract["mobilityProfile"],
    )
    assert score is not None
    assert score.detour_level == proof["detourLevel"]
    assert score.generalized_cost_delta == pytest.approx(proof["generalizedCostDelta"])
    assert score.detour_ratio == pytest.approx(proof["detourRatio"])


def set_route_cost_components(
    route_id: str,
    *,
    walking_distance_meters: int,
    transfer_count: int,
) -> None:
    with open_db() as connection:
        row = connection.execute(
            "SELECT provider_payload_json FROM route_options WHERE id = ?",
            (route_id,),
        ).fetchone()
        payload = json.loads(row["provider_payload_json"])
        payload["walkingDistanceMeters"] = walking_distance_meters
        payload["transferCount"] = transfer_count
        connection.execute(
            """
            UPDATE route_options
            SET provider_payload_json = ?, distance_meters = ?
            WHERE id = ?
            """,
            (json.dumps(payload, ensure_ascii=False), walking_distance_meters, route_id),
        )
        connection.commit()


def seed_plan_with_routes(
    plan_id: str,
    *,
    with_route_contract: bool = True,
    route_topology: str = "two_anchor",
    route_contract: Optional[dict] = None,
) -> str:
    if route_topology not in {
        "two_anchor",
        "complete_adjacent",
        "extra_non_adjacent",
        "duplicate_adjacent",
        "missing_adjacent",
    }:
        raise ValueError(f"Unsupported route topology: {route_topology}")
    now = datetime.now(timezone.utc).isoformat()
    with open_db() as connection:
        connection.execute(
            """
            INSERT INTO itinerary_plans (
                id, user_id, inspiration_set_id, template_type, title, city,
                budget_target, budget_estimate, budget_delta_explanation,
                decision_rationale, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                get_settings().default_user_id,
                "insp_select",
                "custom",
                "北京路线选择",
                "北京",
                None,
                100,
                "soft",
                "test",
                "draft",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_sessions (
                id, user_id, title, city, active_plan_id, active_version_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("sess_select", get_settings().default_user_id, "北京路线选择", "北京", plan_id, None, "active", now, now),
        )
        poi_rows = [
            ("poi_1", "故宫博物院", "B000A8UIN8", 39.918, 116.397),
            ("poi_2", "天坛公园", "B000A8UIN9", 39.882, 116.406),
        ]
        if route_topology != "two_anchor":
            poi_rows.append(("poi_3", "前门大街", "B000A8UINA", 39.899, 116.397))
        for poi_id, name, amap_id, latitude, longitude in poi_rows:
            connection.execute(
                """
                INSERT INTO pois (
                    id, plan_id, name, city, category, latitude, longitude,
                    photo_url, source, confidence, amap_id, type, district,
                    address, source_note, source_url, photos_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    poi_id,
                    plan_id,
                    name,
                    "北京",
                    "scenic",
                    latitude,
                    longitude,
                    None,
                    "amap-place-search",
                    0.9,
                    amap_id,
                    "风景名胜",
                    "东城区",
                    "",
                    "高德地图",
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
            ("day_1", plan_id, 1, None, "Day 1", "晴", "低", 100),
        )
        segment_rows = [
            ("seg_1", 1, "poi_1", "09:00", "11:00"),
            ("seg_2", 2, "poi_2", "14:00", "16:00"),
        ]
        if route_topology != "two_anchor":
            segment_rows.append(("seg_3", 3, "poi_3", "17:00", "19:00"))
        for segment_id, order, poi_id, start_time, end_time in segment_rows:
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
                    plan_id,
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
        insert_route(connection, plan_id, "route_walk", "walking", "步行", True, None, duration_seconds=3600)
        insert_route(connection, plan_id, "route_taxi", "taxi", "打车", False, None, duration_seconds=900)
        insert_route(
            connection,
            plan_id,
            "route_error",
            "driving",
            "驾车",
            False,
            '{"message":"AMap failed"}',
            duration_seconds=1200,
        )
        if route_topology != "two_anchor":
            insert_route(
                connection,
                plan_id,
                "route_bc",
                "transit",
                "公交",
                route_topology != "missing_adjacent",
                None,
                duration_seconds=900,
                from_segment_id="seg_2",
                to_segment_id="seg_3",
                from_poi_id="poi_2",
                to_poi_id="poi_3",
            )
        if route_topology == "extra_non_adjacent":
            insert_route(
                connection,
                plan_id,
                "route_ac_extra",
                "transit",
                "非邻接公交",
                True,
                None,
                duration_seconds=1200,
                from_segment_id="seg_1",
                to_segment_id="seg_3",
                from_poi_id="poi_1",
                to_poi_id="poi_3",
            )
        if route_topology == "duplicate_adjacent":
            insert_route(
                connection,
                plan_id,
                "route_ab_duplicate",
                "transit",
                "重复公交",
                True,
                None,
                duration_seconds=1800,
            )
        snapshot = ItinerarySnapshotService(connection).capture_snapshot(plan_id)
        if with_route_contract:
            snapshot["routeDecisionContract"] = route_contract or route_decision_contract("server_active_version")
        version = ItinerarySnapshotService(connection).save_version(
            "sess_select",
            plan_id,
            "fixture",
            snapshot=snapshot,
        )
        connection.commit()
        return version.id


def insert_route(
    connection: sqlite3.Connection,
    plan_id: str,
    route_id: str,
    mode: str,
    label: str,
    selected: bool,
    error_json: Optional[str],
    *,
    duration_seconds: int = 900,
    distance_meters: int = 1800,
    walking_distance_meters: Optional[int] = None,
    transfer_count: int = 0,
    wait_seconds: int = 0,
    from_segment_id: str = "seg_1",
    to_segment_id: str = "seg_2",
    from_poi_id: str = "poi_1",
    to_poi_id: str = "poi_2",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    walking_distance = (
        distance_meters if walking_distance_meters is None and mode == "walking" else walking_distance_meters or 0
    )
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
            plan_id,
            from_segment_id,
            to_segment_id,
            from_poi_id,
            to_poi_id,
            "amap-webservice",
            mode,
            label,
            1 if selected else 0,
            1,
            mode,
            distance_meters,
            duration_seconds,
            round(duration_seconds / 60),
            28 if mode == "taxi" else 0,
            "CNY",
            28 if mode == "taxi" else 0,
            "low",
            "amap-webservice",
            "[[116.397,39.918],[116.406,39.882]]",
            json.dumps(
                (
                    [{"instruction": "步行", "mode": "walking", "distance": walking_distance}]
                    if walking_distance
                    else []
                ),
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "status": "1",
                    "walkingDistanceMeters": walking_distance,
                    "transferCount": transfer_count,
                    "waitSeconds": wait_seconds,
                    "riskPenaltyMinutes": 0,
                    "costComponentProvenance": {
                        "walkingDistance": "recorded_provider_payload",
                        "transferCount": "recorded_provider_payload",
                        "wait": "recorded_provider_payload",
                        "risk": "recorded_route_fixture",
                    },
                },
                ensure_ascii=False,
            ),
            error_json,
            now,
        ),
    )


def route_write_state(connection: sqlite3.Connection, plan_id: str) -> dict:
    session = connection.execute(
        "SELECT active_version_id FROM conversation_sessions WHERE active_plan_id = ?",
        (plan_id,),
    ).fetchone()
    return {
        "activeVersionId": session["active_version_id"] if session else None,
        "versionCount": connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0],
        "patchCount": connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0],
        "routes": [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM route_options WHERE plan_id = ? ORDER BY id",
                (plan_id,),
            ).fetchall()
        ],
        "schedule": [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM itinerary_segments WHERE plan_id = ? ORDER BY id",
                (plan_id,),
            ).fetchall()
        ],
    }

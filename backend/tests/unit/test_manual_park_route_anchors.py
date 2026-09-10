"""Offline replay of UI10's recorded park/activity edit topology.

The fixture contains recorded POI facts, not generated route evidence. These
tests do not call a Provider or write the recorded acceptance database.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.conversation_service import ConversationService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.route_service import RouteService


@pytest.fixture
def recorded_snapshot():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures" / "ui10_manual_park_route.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def replay_db(recorded_snapshot):
    # The suite's autouse fixture binds this path to a new per-test directory.
    connection = sqlite3.connect(sqlite_path_from_url(get_settings().database_url))
    connection.row_factory = sqlite3.Row
    session = ConversationService(connection).create_session("北京", "UI10 offline replay")
    ItinerarySnapshotService(connection).apply_snapshot(session.active_plan_id, recorded_snapshot)
    yield connection, session.active_plan_id
    connection.close()


def test_recorded_park_survives_manual_row_and_route_matrix_readers(replay_db, recorded_snapshot):
    connection, plan_id = replay_db
    service = ItineraryPatchService(connection)
    day = recorded_snapshot["days"][0]
    expected_ids = [segment["id"] for segment in day["segments"]]

    rows = service._route_anchor_rows_for_day(plan_id, day["id"])
    assert [row["id"] for row in rows] == expected_ids
    assert service._adjacent_pairs_for_days(plan_id, {day["id"]}) == {tuple(expected_ids)}
    points, errors = service._simulate_final_route_matrix_points(
        plan_id,
        [ItineraryPatchOperation(op="refresh_routes_for_day", dayId=day["id"])],
        {},
    )
    assert errors == []
    assert [point["amapId"] for point in points[day["id"]]] == ["B000A7I1OL", "B000A80UL1"]


def test_recorded_park_survives_snapshot_route_selection_reader(replay_db, recorded_snapshot):
    connection, _ = replay_db
    expected_pair = tuple(segment["id"] for segment in recorded_snapshot["days"][0]["segments"])
    assert set(ItineraryPatchService(connection)._snapshot_adjacent_route_matrix_pairs(recorded_snapshot)) == {
        expected_pair
    }


def test_second_canonical_poi_creates_exactly_one_touched_route_pair(replay_db, recorded_snapshot):
    connection, plan_id = replay_db
    snapshots = ItinerarySnapshotService(connection)
    single_point = deepcopy(recorded_snapshot)
    single_point["days"][0]["segments"] = single_point["days"][0]["segments"][:1]
    snapshots.apply_snapshot(plan_id, single_point)
    patch = ItineraryPatchService(connection)
    day_id = recorded_snapshot["days"][0]["id"]
    scope = patch._route_scope_before(plan_id, [ItineraryPatchOperation(op="add_segment", dayId=day_id)])
    assert scope["before_pairs"] == set()

    snapshots.apply_snapshot(plan_id, recorded_snapshot)
    expected_pair = tuple(segment["id"] for segment in recorded_snapshot["days"][0]["segments"])
    assert patch._route_pairs_after(plan_id, scope) == {expected_pair}
    assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == 0


def test_recorded_park_is_in_expected_coverage_and_map_readiness(replay_db, recorded_snapshot):
    connection, plan_id = replay_db
    service = ItineraryService(connection)
    segments = service._load_segments(plan_id)
    pois = service._load_pois(plan_id)
    assert service._expected_route_leg_count(segments, pois=pois) == 1
    assert service._route_coverage_summary(plan_id, segments, pois)["missingLegCount"] == 1
    assert len(service._map_readiness_summary(plan_id)["items"]) == 2
    expected_pair = tuple(segment["id"] for segment in recorded_snapshot["days"][0]["segments"])
    assert service._route_pairs_touching_pois(plan_id, {segments[1].poi_id}) == {expected_pair}


def test_refresh_passes_qualified_park_to_existing_route_consumer(replay_db, recorded_snapshot):
    connection, plan_id = replay_db
    service = ItineraryService(connection)
    observed_pairs = []

    class ObserveRouteGroups(RouteService):
        def build_routes(self, plan_id, pois, transport_mode, **kwargs):
            observed_pairs.extend(
                (left.id, right.id)
                for left, right, _, _ in self._route_groups(
                    pois,
                    kwargs.get("segments"),
                    allow_semantic_route_anchors=kwargs.get("allow_semantic_route_anchors", False),
                )
            )
            return []  # Deliberately no fabricated successful route.

    service.route_service = ObserveRouteGroups(map_provider_key="")
    service.refresh_routes(plan_id)
    expected_pair = tuple(segment["id"] for segment in recorded_snapshot["days"][0]["segments"])
    assert observed_pairs == [expected_pair]
    assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == 0
    assert (
        connection.execute("SELECT kind FROM itinerary_segments WHERE id = ?", (expected_pair[0],)).fetchone()[0]
        == "park"
    )


def test_rejected_parks_do_not_activate_poi_only_route_fallback(replay_db, recorded_snapshot):
    connection, plan_id = replay_db
    for segment in recorded_snapshot["days"][0]["segments"]:
        segment["kind"] = "park"
        segment["semanticMetadata"]["routeAnchor"] = False
    ItinerarySnapshotService(connection).apply_snapshot(plan_id, recorded_snapshot)
    service = ItineraryService(connection)
    observed = []

    class ObserveRejectedParks(RouteService):
        def build_routes(self, plan_id, pois, transport_mode, **kwargs):
            assert len(kwargs["segments"]) == 2
            observed.extend(self._route_groups(pois, kwargs["segments"], allow_semantic_route_anchors=True))
            return []

    service.route_service = ObserveRejectedParks(map_provider_key="")
    service.refresh_routes(plan_id, allow_semantic_route_anchors=True)
    assert observed == []
    assert (
        service._route_coverage_summary(plan_id, service._load_segments(plan_id), service._load_pois(plan_id))[
            "requiredLegCount"
        ]
        == 0
    )


def test_park_projection_does_not_admit_other_semantic_kinds(replay_db, recorded_snapshot):
    connection, plan_id = replay_db
    recorded_snapshot["days"][0]["segments"][1]["kind"] = "note"
    ItinerarySnapshotService(connection).apply_snapshot(plan_id, recorded_snapshot)
    service = ItineraryService(connection)
    segments = service._load_segments(plan_id)
    pois = service._load_pois(plan_id)
    projected = service._route_consumer_segments(segments, pois)
    assert RouteService(map_provider_key="")._route_groups(pois, projected) == []
    assert [segment.kind for segment in service._load_segments(plan_id)] == ["park", "note"]


@pytest.mark.parametrize(
    "mutation",
    [
        "no_semantic_metadata",
        "no_route_anchor",
        "route_anchor_false",
        "route_anchor_string",
        "no_semantic_grounding",
        "semantic_pending",
        "poi_pending",
        "no_canonical_id",
        "noncanonical_id",
        "untrusted_source",
        "no_coordinates",
        "invalid_coordinates",
    ],
)
def test_park_requires_explicit_grounded_canonical_admission(replay_db, recorded_snapshot, mutation):
    connection, plan_id = replay_db
    park = recorded_snapshot["days"][0]["segments"][0]
    semantic = park["semanticMetadata"]
    poi = park["poi"]
    if mutation == "no_semantic_metadata":
        park["semanticMetadata"] = {}
    elif mutation == "no_route_anchor":
        semantic.pop("routeAnchor")
    elif mutation == "route_anchor_false":
        semantic["routeAnchor"] = False
    elif mutation == "route_anchor_string":
        semantic["routeAnchor"] = "true"
    elif mutation == "no_semantic_grounding":
        semantic.pop("groundingStatus")
    elif mutation == "semantic_pending":
        semantic["groundingStatus"] = "waiting_for_poi_grounding"
    elif mutation == "poi_pending":
        poi["sourceNote"] = "groundingStatus: waiting_for_poi_grounding"
    elif mutation == "no_canonical_id":
        poi["amapId"] = None
    elif mutation == "noncanonical_id":
        poi["amapId"] = "not-a-provider-id"
    elif mutation == "untrusted_source":
        poi["source"] = "agent-text-timeline"
    elif mutation == "no_coordinates":
        poi["longitude"] = None
    elif mutation == "invalid_coordinates":
        poi["longitude"] = 181
    ItinerarySnapshotService(connection).apply_snapshot(plan_id, recorded_snapshot)
    patch = ItineraryPatchService(connection)
    day_id = recorded_snapshot["days"][0]["id"]
    assert [row["id"] for row in patch._route_anchor_rows_for_day(plan_id, day_id)] == [
        recorded_snapshot["days"][0]["segments"][1]["id"]
    ]
    assert patch._snapshot_adjacent_route_matrix_pairs(recorded_snapshot) == {}
    itinerary = ItineraryService(connection)
    assert (
        itinerary._expected_route_leg_count(itinerary._load_segments(plan_id), pois=itinerary._load_pois(plan_id)) == 0
    )
    assert (
        itinerary._route_coverage_summary(plan_id, itinerary._load_segments(plan_id), itinerary._load_pois(plan_id))[
            "requiredLegCount"
        ]
        == 0
    )

    class RejectUnexpectedPark(RouteService):
        def build_routes(self, plan_id, pois, transport_mode, **kwargs):
            assert all(segment.kind != "park" for segment in kwargs["segments"])
            return []

    itinerary.route_service = RejectUnexpectedPark(map_provider_key="")
    itinerary.refresh_routes(plan_id, allow_semantic_route_anchors=True)


@pytest.mark.parametrize("kind", ["visit", "activity", "meal"])
def test_existing_route_kinds_keep_their_admission_semantics(replay_db, recorded_snapshot, kind):
    connection, plan_id = replay_db
    first = recorded_snapshot["days"][0]["segments"][0]
    first["kind"] = kind
    first["semanticMetadata"] = {}
    ItinerarySnapshotService(connection).apply_snapshot(plan_id, recorded_snapshot)
    assert (
        len(ItineraryPatchService(connection)._route_anchor_rows_for_day(plan_id, recorded_snapshot["days"][0]["id"]))
        == 2
    )

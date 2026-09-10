from __future__ import annotations

import copy
import json
import socket

import pytest

from src.core.database import get_db
from src.services.conversation_service import ConversationService
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
from src.services.simple_open_direction_service import SimpleOpenDirectionService


@pytest.fixture
def connection():
    database = get_db()
    db = next(database)
    try:
        yield db
    finally:
        database.close()


def _contract() -> dict:
    return {
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
        "requiredIntents": [{"intentType": "campus_visit", "target": 2}],
    }


def _ensure_root(
    db, *, root_id: str, session_id: str = "", contract: dict | None = None, allow_exploration_ordering: bool = True
) -> dict:
    if not session_id:
        session_id = ConversationService(db).create_session(city="北京").session_id
    return SimpleOpenDirectionService(db).ensure_root(
        session_id=session_id,
        planning_root_id=root_id,
        source_assistant_turn_id=f"assistant_{root_id}",
        expected_base_version_id=None,
        source_observation_fingerprint="o" * 64,
        request_contract_fingerprint="r" * 64,
        request_contract=contract or _contract(),
        locality="北京",
        max_pages_per_query=3,
        allow_exploration_ordering=allow_exploration_ordering,
    )


def _names(frontier: dict) -> list[str]:
    attempt = SimpleDirectionFrontierService.begin_attempt(
        frontier,
        campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
    )
    return [item["canonicalName"] for item in attempt["campusAssignments"]]


def test_production_new_roots_use_independent_exploration_input(connection, monkeypatch) -> None:
    def no_network(*_args, **_kwargs):
        raise AssertionError("frontier selection must not call a Provider")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    seeds = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(
        SimpleOpenDirectionService,
        "_new_exploration_seed",
        staticmethod(lambda: next(seeds)),
        raising=False,
    )
    first = _ensure_root(connection, root_id="root_one")
    second = _ensure_root(connection, root_id="root_two")

    assert _names(first["simpleDirectionFrontier"]) != _names(second["simpleDirectionFrontier"])
    assert first["simpleDirectionFrontier"]["explorationOrdering"]["selectionSeed"] == "a" * 32
    assert second["simpleDirectionFrontier"]["explorationOrdering"]["selectionSeed"] == "b" * 32
    for table in ("agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_restart_and_execution_replay_reuse_frozen_root_order(connection, monkeypatch) -> None:
    seeds: list[str] = []

    def new_seed() -> str:
        seeds.append("c" * 32)
        return seeds[-1]

    monkeypatch.setattr(SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(new_seed), raising=False)
    first = _ensure_root(connection, root_id="root_replay")
    frontier = first["simpleDirectionFrontier"]
    original_summary = connection.execute(
        "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?", (first["id"],)
    ).fetchone()[0]
    restarted = _ensure_root(connection, root_id="root_replay", session_id=first["sessionId"])
    assert restarted["simpleDirectionFrontier"] == frontier
    assert seeds == ["c" * 32]
    assert (
        connection.execute("SELECT summary_json FROM agent_plan_portfolios WHERE id = ?", (first["id"],)).fetchone()[0]
        == original_summary
    )

    def claim() -> dict:
        return SimpleOpenDirectionService(connection).claim_frontier_assignment(
            portfolio_id=first["id"],
            execution_id="execution_replay",
            campus_slots=[{"dayNumber": 1, "slotId": "d1"}, {"dayNumber": 2, "slotId": "d2"}],
            request_contract_fingerprint="r" * 64,
        )

    initial_attempt = claim()
    replay_attempt = claim()
    assert initial_attempt == replay_attempt
    assert [item["canonicalName"] for item in initial_attempt["campusAssignments"]] == _names(frontier)


def test_seeded_order_uses_qualification_membership_and_not_audit_identity() -> None:
    evidence = EntityQualificationEvidenceService.qualified_entities(
        locality="北京", scheme="moe_project_classification", value="985"
    )
    assert evidence is not None
    original = copy.deepcopy(evidence)
    frontiers = [
        SimpleDirectionFrontierService.create(
            planning_root_id=root,
            request_contract_fingerprint=request,
            evidence=evidence,
            locality="北京",
            max_pages_per_query=3,
            exploration_seed="d" * 32,
        )
        for root, request in (("audit_a", "a" * 64), ("audit_b", "b" * 64))
    ]
    assert _names(frontiers[0]) == _names(frontiers[1])
    assert evidence == original
    for frontier in frontiers:
        assert [item["canonicalName"] for item in frontier["qualifiedEntityFrontier"]] == [
            item["canonicalName"] for item in evidence["entities"]
        ]
        assert set(_names(frontier)) <= {item["canonicalName"] for item in evidence["entities"]}


@pytest.mark.parametrize("tamper", ["extra", "missing", "duplicate", "reordered", "renamed", "missing_binding"])
def test_tampered_exploration_membership_fails_closed(connection, tamper: str) -> None:
    root = _ensure_root(connection, root_id=f"root_tamper_{tamper}")
    frontier = copy.deepcopy(root["simpleDirectionFrontier"])
    order = frontier["explorationOrdering"]["orderedEntityFingerprints"]
    if tamper == "extra":
        order.append("f" * 64)
    elif tamper == "missing":
        order.pop()
    elif tamper == "duplicate":
        order[-1] = order[0]
    elif tamper == "reordered":
        order.reverse()
    elif tamper == "missing_binding":
        frontier["qualifiedEntityFrontier"][0].pop("qualificationBinding")
    else:
        frontier["qualifiedEntityFrontier"][0]["canonicalName"] = "伪造大学"
    # A caller recomputing the outer checksum still cannot change frozen membership.
    frontier["frontierFingerprint"] = SimpleDirectionFrontierService._frontier_fingerprint(frontier)
    with pytest.raises(ValueError, match="simple_direction_exploration"):
        _names(frontier)


def test_historical_frontier_is_not_reordered_or_given_a_new_seed(connection, monkeypatch) -> None:
    root = _ensure_root(connection, root_id="root_legacy")
    summary = json.loads(
        connection.execute("SELECT summary_json FROM agent_plan_portfolios WHERE id = ?", (root["id"],)).fetchone()[0]
    )
    legacy = summary["simpleDirectionFrontier"]
    legacy.pop("explorationOrdering", None)
    legacy["frontierFingerprint"] = SimpleDirectionFrontierService._frontier_fingerprint(legacy)
    connection.execute(
        "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ?", (json.dumps(summary), root["id"])
    )
    connection.commit()

    def forbidden_seed() -> str:
        raise AssertionError("existing roots must not receive a new exploration seed")

    monkeypatch.setattr(
        SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(forbidden_seed), raising=False
    )
    restored = _ensure_root(connection, root_id="root_legacy", session_id=root["sessionId"])
    assert restored["simpleDirectionFrontier"] == legacy
    assert _names(legacy) == ["北京大学", "清华大学"]


def test_frontier_is_durable_if_initialization_stops_after_root_insert(connection, monkeypatch) -> None:
    original_mark_workflow = SimpleOpenDirectionService._mark_workflow

    def stop_after_insert(_self, _portfolio_id: str) -> None:
        raise RuntimeError("stop_after_root_insert")

    monkeypatch.setattr(SimpleOpenDirectionService, "_mark_workflow", stop_after_insert)
    session = ConversationService(connection).create_session(city="北京")
    with pytest.raises(RuntimeError, match="stop_after_root_insert"):
        _ensure_root(connection, root_id="root_interrupted", session_id=session.session_id)
    summary = json.loads(
        connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE source_user_turn_id = 'root_interrupted'"
        ).fetchone()[0]
    )
    frozen = summary["simpleDirectionFrontier"]
    assert frozen["explorationOrdering"]["selectionSeed"]
    monkeypatch.setattr(SimpleOpenDirectionService, "_mark_workflow", original_mark_workflow)

    def forbidden_seed() -> str:
        raise AssertionError("restart must use the seed persisted with root creation")

    monkeypatch.setattr(SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(forbidden_seed))
    restored = _ensure_root(connection, root_id="root_interrupted", session_id=session.session_id)
    assert restored["simpleDirectionFrontier"] == frozen
    assert restored["workflowMode"] == SimpleOpenDirectionService.WORKFLOW_MODE


@pytest.mark.parametrize("mismatch", ["", "request", "base"])
def test_concurrent_root_loser_uses_only_same_scope_winner(connection, monkeypatch, mismatch: str) -> None:
    winner = _ensure_root(connection, root_id="root_concurrent")
    if mismatch == "request":
        connection.execute(
            "UPDATE agent_plan_portfolios SET request_contract_fingerprint = ? WHERE id = ?",
            ("z" * 64, winner["id"]),
        )
    elif mismatch == "base":
        connection.execute(
            "UPDATE agent_plan_portfolios SET expected_base_version_id = ? WHERE id = ?",
            ("other_base", winner["id"]),
        )
    connection.commit()
    original_root = SimpleOpenDirectionService._root
    calls = 0

    def simulate_preinsert_read(self, **kwargs):
        nonlocal calls
        calls += 1
        return None if calls == 1 else original_root(self, **kwargs)

    monkeypatch.setattr(SimpleOpenDirectionService, "_root", simulate_preinsert_read)
    if mismatch:
        with pytest.raises(ValueError, match="simple_direction_frontier_root_identity_mismatch"):
            _ensure_root(connection, root_id="root_concurrent", session_id=winner["sessionId"])
    else:
        loser = _ensure_root(connection, root_id="root_concurrent", session_id=winner["sessionId"])
        assert loser["id"] == winner["id"]
        assert loser["simpleDirectionFrontier"] == winner["simpleDirectionFrontier"]
    assert connection.execute("SELECT COUNT(*) FROM agent_plan_portfolios").fetchone()[0] == 1


@pytest.mark.parametrize(
    "constraint",
    [
        {"spatialPreference": {"status": "resolved", "strength": "required"}},
        {"lockedEntities": ["中国农业大学"]},
        {"requiredIntents": [{"intentType": "campus_visit", "exactEntity": "中国农业大学"}]},
        {"negativeConstraints": ["不去清华大学"]},
    ],
)
def test_restricted_request_keeps_existing_selection_boundary(connection, monkeypatch, constraint: dict) -> None:
    def forbidden_seed() -> str:
        raise AssertionError("the exploration tie-break cannot override explicit constraints")

    monkeypatch.setattr(
        SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(forbidden_seed), raising=False
    )
    root = _ensure_root(connection, root_id="root_restricted", contract={**_contract(), **constraint})
    assert "explorationOrdering" not in root["simpleDirectionFrontier"]


def test_server_compiled_exact_pool_can_disable_new_root_ordering(connection, monkeypatch) -> None:
    def forbidden_seed() -> str:
        raise AssertionError("compiled exact-entity pools must retain their selection boundary")

    monkeypatch.setattr(SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(forbidden_seed))
    root = _ensure_root(connection, root_id="root_exact_pool", allow_exploration_ordering=False)
    assert "explorationOrdering" not in root["simpleDirectionFrontier"]

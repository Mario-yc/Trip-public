"""Regression from the isolated UI10 run; no live Provider or invented POIs."""
import copy
import json
from pathlib import Path
import sqlite3

import pytest

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_service import AgentService
from src.services.route_insertion_scorer import RouteInsertionScorer


def recorded_context():
    fixture = Path(__file__).resolve().parents[1] / "fixtures/ui10_initial_contract_context.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_current_issuer_quality_floor_is_accepted_by_initial_fallback():
    context = recorded_context()
    route = context["requestIntentContract"]["routeDecisionContract"]
    issued = AgentService._compile_route_decision_contract(
        request_text=context["effectiveUserMessage"],
        experience_specs=[],
        existing_contract={
            "detourTolerance": route["detourTolerance"],
            "detourToleranceSource": route["detourToleranceSource"],
        },
    )
    assert issued["status"] == "ready"
    assert issued["adjacentLegConstraintSource"] == "versioned_product_travel_quality_floor"
    assert issued["adjacentLegConstraint"] == route["adjacentLegConstraint"]
    assert RouteInsertionScorer.normalized_route_decision_contract(issued) is not None
    context["requestIntentContract"]["routeDecisionContract"] = issued
    before = copy.deepcopy(context)
    assert AgentService.__new__(AgentService)._can_fallback_initial_day_slots(context) is True
    assert context == before


def test_invalid_initial_output_can_compile_recorded_unresolved_slots_without_writes(monkeypatch):
    context = recorded_context()
    with sqlite3.connect(sqlite_path_from_url(get_settings().database_url)) as connection:
        connection.row_factory = sqlite3.Row
        service = AgentService(connection)
        monkeypatch.setattr(service, "_call_initial_plan_provider", lambda _: "{invalid-json")
        monkeypatch.setattr(service.output_repair_service, "repair", lambda *a, **k: (None, {"status": "skipped"}))
        tables = ("agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options")
        counts = lambda: {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        before = counts()
        events = []
        result = service._generate_initial_day_slot_output(context, events)
        assert result.mode == "day_slots"
        assert result.day_slots and result.intent_pools
        assert "DAY_SLOT_FALLBACK_UNAVAILABLE" not in result.warnings
        assert counts() == before
        event = next(e for e in events if e["type"] == "initial_day_slot_provider")
        assert event["metadata"]["resultPreview"]["fallbackType"] == "simple_open_contract_slot_fallback"


@pytest.mark.parametrize("damage", ["fingerprint", "floor_provenance", "radius", "topology", "missing_route", "insufficient", "clarification"])
def test_quality_floor_recovery_keeps_rejection_boundaries(damage):
    context = recorded_context()
    route = context["requestIntentContract"]["routeDecisionContract"]
    if damage == "fingerprint":
        route["fingerprint"] = "0" * 64
    elif damage == "floor_provenance":
        route["provenance"].pop("travelQualityFloor")
    elif damage == "radius":
        route["adjacentLegConstraint"]["candidateSearchRadiusMeters"] += 1000
    elif damage == "topology":
        route["topologyConstraint"]["maxBacktrackRatio"] += 0.1
    elif damage == "missing_route":
        context["requestIntentContract"].pop("routeDecisionContract")
    elif damage == "insufficient":
        context["simpleOpenInitialRequestSufficient"] = False
    else:
        context["requestIntentContract"]["clarificationRequired"] = True
    if damage in {"floor_provenance", "radius", "topology"}:
        portable = RouteInsertionScorer.build_route_decision_contract(
            source=route["source"], provenance=route["provenance"],
            detour_tolerance=route["detourTolerance"], mobility_profile=route["mobilityProfile"],
            adjacent_leg_constraint=route["adjacentLegConstraint"], topology_constraint=route["topologyConstraint"],
        )
        assert portable is not None
        route["fingerprint"] = portable["fingerprint"]
    assert AgentService.__new__(AgentService)._can_fallback_initial_day_slots(context) is False

"""Regression for the recorded UI10 named-place substitution, without live calls."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_service import AgentService
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
from src.services.request_activity_coverage_service import RequestActivityCoverageError, RequestActivityCoverageService


def context():
    result = json.loads((Path(__file__).parents[1] / "fixtures/ui10_named_place_context.json").read_text(encoding="utf-8"))
    # Executor dispatch binds this exact accepted directive before projection.
    result["planningDirective"] = deepcopy(result["agentDecisionState"]["actionDirective"])
    return result


def coverage_case(name="景山公园", exact=True):
    pending = RequestActivityCoverageService.prepare(
        {"dayCount": 1, "requiredIntents": [], "lockedEntities": []}, f"只去{name}"
    )
    activity = {"goalId": "goal_request_1_1", "sourceText": name, "intentType": "park",
                "polarity": "required", "allowedDayNumbers": [1], "minCount": 1, "dayPart": "morning"}
    if exact:
        activity["exactEntity"] = name
    return pending, [{"clauseId": "request_clause_1", "classification": "activity", "activities": [activity]}]


def test_full_coverage_can_bind_source_named_place_without_inventing_poi():
    pending, proposal = coverage_case()
    before = deepcopy(pending)
    result = RequestActivityCoverageService.compile(pending, proposal)
    goal = result["requiredIntents"][0]
    assert goal["exactEntity"] == "景山公园"
    assert goal["entityBindingMode"] == "exact_entity"
    assert result["lockedEntities"] == ["景山公园"]
    assert "amapId" not in goal
    assert pending == before


@pytest.mark.parametrize("damage", ["not_in_source", "blank", "excluded", "changed_legacy"])
def test_invalid_or_conflicting_named_binding_fails_closed(damage):
    pending, proposal = coverage_case()
    activity = proposal[0]["activities"][0]
    if damage == "not_in_source":
        activity["exactEntity"] = "城市绿心森林公园"
    elif damage == "blank":
        activity["exactEntity"] = " "
    elif damage == "excluded":
        activity["polarity"] = "excluded"
    else:
        pending["requiredIntents"] = [{"goalId": "old", "intentType": "park", "requiredMin": 1,
                                      "exactEntity": "另一个指定公园"}]
    before = deepcopy(pending)
    with pytest.raises(RequestActivityCoverageError):
        RequestActivityCoverageService.compile(pending, proposal)
    assert pending == before


def test_generic_park_remains_category_without_name_inference():
    pending, proposal = coverage_case("一处城市公园", exact=False)
    result = RequestActivityCoverageService.compile(pending, proposal)
    assert not result["requiredIntents"][0].get("exactEntity")
    assert result["lockedEntities"] == []


@pytest.mark.parametrize("path", ["fallback", "provider_projection"])
def test_authoritative_projection_preserves_goal_identity_and_rejects_other_park(path):
    ctx = context()
    goal = next(item for item in ctx["requestIntentContract"]["requiredIntents"] if item["intentType"] == "park")
    goal.update(exactEntity="景山公园", entityBindingMode="exact_entity", explicitlyNamed=True)
    contract_before = deepcopy(ctx["requestIntentContract"])
    with sqlite3.connect(sqlite_path_from_url(get_settings().database_url)) as connection:
        connection.row_factory = sqlite3.Row
        service = AgentService(connection)
        tables = ["itinerary_versions", "itinerary_patches", "route_options", "agent_plan_proposals"]
        counts = lambda: [connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables]
        before = counts()
        initial = service._server_generic_day_slots_fallback(ctx, [])
        if path == "provider_projection":
            payload = initial.model_dump(by_alias=True)
            for pool in payload["intentPools"]:
                pool.update(entityBindingMode="exact_entity", exactEntity="城市绿心森林公园",
                            candidateHints=["城市绿心森林公园"])
            initial = service._simple_open_project_initial_plan_to_authoritative_occurrences(
                type(initial).model_validate(payload), ctx
            )
        pool = next(pool for pool in initial.intent_pools if pool.intent_type == "park")
        assert pool.entity_binding_mode == "exact_entity"
        assert pool.exact_entity == "景山公园"
        assert pool.candidate_hints == ["景山公园"]
        assert not ConsumerCandidateAdmissionService._matches_exact_entity(
            {"name": "城市绿心森林公园", "aliases": []}, pool.exact_entity
        )
        assert ConsumerCandidateAdmissionService._matches_exact_entity(
            {"name": "景山公园", "aliases": []}, pool.exact_entity
        )
        assert ctx["requestIntentContract"] == contract_before
        assert counts() == before


def test_existing_hard_named_binding_cannot_be_silently_dropped_by_coverage():
    pending, proposal = coverage_case(exact=False)
    pending["requiredIntents"] = [{"goalId": "old", "intentType": "park", "requiredMin": 1,
                                  "exactEntity": "景山公园", "entityBindingMode": "exact_entity"}]
    result = RequestActivityCoverageService.compile(pending, proposal)
    assert result["requiredIntents"][0]["exactEntity"] == "景山公园"
    assert result["lockedEntities"] == ["景山公园"]

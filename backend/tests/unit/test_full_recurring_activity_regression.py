"""Recorded Full failures plus a separately hand-authored recurring control.

Source: user-supplied trip-debug-bundle-v4, captured 2026-09-06T16:53:36.198Z.
Bundle SHA256: 73fe0dd374c5abfcef07d4df318734d6fbfb204fbbcdaa79b9bbf0d2a8772b1c
The two distinct AGENT_REQUEST_CONTEXTS user values contain identical pending
contracts and providerRawDecisions[0]. Their recorded Full runs both finished
with stop/complete and 1075/1087 completion tokens. The JSON fixture preserves
only their original request, pending contract and two original decisions.
No account, conversation IDs, URLs, history or performance payload is copied.

Recorded decisions are never repaired here. The positive decision is built
independently by hand and is explicitly NOT a successful real model response.
"""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace
import urllib.request

import httpx
import pytest

from src.services.agent_autonomy_service import (
    AgentAutonomyController,
    AutonomyContextProjector,
    ModelDecisionV3,
)
from src.services.agent_observation_service import AgentObservationBuilder
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.request_activity_coverage_service import (
    RequestActivityCoverageError,
    RequestActivityCoverageService,
)


FIXTURE_PATH = Path(__file__).with_name("full_recurring_activity_bundle.json")
FIXTURE_FINGERPRINT = "4ebfb56bb22772e86458e3f2416127a1477a896b13f4415d9bebedefb720349d"
ORIGINAL_DECISION_FINGERPRINTS = (
    "f278791cd699f771bf7b001547d806633ec76195db7dba8c63b8b7ce333c0079",
    "33dcb0a34e4c162178c3c8f5b897fdaa6b2e3d096ce86304879952b5424f1bf6",
)


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def recorded_bundle():
    value = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fingerprint(value) == FIXTURE_FINGERPRINT, "Original evidence changed; never silently correct the recording"
    return value


@pytest.fixture(autouse=True)
def no_network_or_application_database(monkeypatch):
    # The repository conftest creates its usual isolated test schema before
    # this fixture. The replay/compilation itself must never open a database.
    def forbidden(*args, **kwargs):
        pytest.fail("Offline Full replay must not perform network or application database I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def request_context(recording):
    return {
        "latestUserMessage": recording["request"],
        "effectiveUserMessage": recording["request"],
        "selectedCity": "北京",
        "serverExecutionProfile": "simple_open_v1",
        "requestIntentContract": deepcopy(recording["pendingRequestContract"]),
        "resolvedTripDates": {
            "status": "resolved",
            "startDate": "2026-10-01",
            "endDate": "2026-10-02",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
    }


def run_actual_controller(recording, decision):
    """Replace only the outbound model boundary, not validation or authority."""
    calls = []

    def replay_recorded_or_hand_authored(provider_context, *, repair_feedback=None, **kwargs):
        assert not repair_feedback, "No schema repair is authorized for this coverage replay"
        assert not calls, "Exactly one in-memory Full response may be consumed"
        calls.append(deepcopy(dict(provider_context)))
        return deepcopy(decision)

    def forbidden(*args, **kwargs):
        pytest.fail("Coverage replay must not call Lite, Planner, fallback or a place executor")

    context = request_context(recording)
    observation = AgentObservationBuilder().build(context)
    context["agentObservation"] = observation.model_dump(by_alias=True)
    provider = SimpleNamespace(decide_autonomy=replay_recorded_or_hand_authored, decide_autonomy_lite=forbidden)
    controller = AgentAutonomyController(
        provider=provider, fallback_decision_resolver=SimpleNamespace(resolve=forbidden)
    )
    result = controller.decide(
        recording["request"],
        context,
        AutonomyContextProjector().project(context),
        available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        observation=observation,
    )
    assert len(calls) == 1, result.controller_error
    assert result.controller_full_called and not result.controller_lite_called
    assert not result.planner_called and not result.deterministic_arbitrator_called
    assert result.schema_repair_attempts == 0
    assert result.provider_raw_decisions == (decision,)
    expected_clauses = [
        {key: clause[key] for key in ("clauseId", "text", "goalIds")}
        for clause in recording["pendingRequestContract"]["requestActivityCoverage"]["clauses"]
    ]
    assert calls[0]["requestActivityClauses"] == expected_clauses
    return result, context


@pytest.mark.parametrize("index", [0, 1], ids=["recorded-stop-1075-tokens", "recorded-stop-1087-tokens"])
def test_original_complete_full_decisions_fail_closed_without_mutation(recorded_bundle, index, monkeypatch):
    before = deepcopy(recorded_bundle)
    raw = recorded_bundle["originalModelDecisions"][index]
    assert fingerprint(raw) == ORIGINAL_DECISION_FINGERPRINTS[index]
    ModelDecisionV3.model_validate(raw, strict=True)  # Complete/schema-valid is not semantic correctness.
    directive = raw["actionDirective"]
    coverage = directive["requestCoverage"]
    assert len(coverage) == 8
    pairs = [entry["activities"] for entry in coverage if entry.get("activities")]
    assert len(pairs) == 3 and all(len(pair) == 2 for pair in pairs)
    for first, second in pairs:
        assert first["goalId"] != second["goalId"]
        assert {key: value for key, value in first.items() if key != "goalId"} == {
            key: value for key, value in second.items() if key != "goalId"
        }
        assert first["minCount"] == 2 and first["allowedDayNumbers"] == [1, 2]
    placed = Counter(goal for day in directive["dayStrategies"] for goal in day["requiredGoalIds"])
    assert len(placed) == 6 and set(placed.values()) == {1}
    with pytest.raises(RequestActivityCoverageError, match=r"^request_activity_coverage_duplicate_activity:"):
        RequestActivityCoverageService.compile(recorded_bundle["pendingRequestContract"], coverage)

    def forbidden_occurrence_execution(*args, **kwargs):
        pytest.fail("Rejected source coverage must not enter occurrence/itinerary execution")

    monkeypatch.setattr(GoalOccurrenceCompiler, "compile", forbidden_occurrence_execution)
    result, context = run_actual_controller(recorded_bundle, raw)
    assert not result.controller_full_succeeded
    assert "request_activity_coverage_duplicate_activity:" in result.controller_error
    assert result.decision.primary_action == "ask_user"
    assert "request_activity_coverage_incomplete" in result.decision.reason_codes
    assert result.decision.required_tools == [] and result.gated_decision.effective_tools == []
    assert result.to_event_metadata()["proposedExecutionRoute"] == "no_safe_action"
    assert context["requestIntentContract"] == before["pendingRequestContract"]
    assert recorded_bundle == before


def hand_authored_correct_decision(recording):
    """Independent synthetic control: three stable goals, two days each."""
    claims = {
        "request_clause_1": ("参观北京985高校", "campus_visit", "flexible"),
        "request_clause_2": ("每晚都去逛公园", "park", "evening"),
        "request_clause_3": ("中午想体验当地特色美食", "meal", "noon"),
    }
    coverage, activities = [], []
    for clause in recording["pendingRequestContract"]["requestActivityCoverage"]["clauses"]:
        entry = {"clauseId": clause["clauseId"], "classification": "constraint"}
        if clause["clauseId"] in claims:
            source, intent, part = claims[clause["clauseId"]]
            activity = {
                "goalId": clause["goalIds"][0],
                "sourceText": source,
                "intentType": intent,
                "polarity": "required",
                "allowedDayNumbers": [1, 2],
                "minCount": 2,
                "dayPart": part,
            }
            activities.append(activity)
            entry.update(classification="activity", activities=[activity])
        coverage.append(entry)
    goals = [activity["goalId"] for activity in activities]
    hints = []
    for day in (1, 2):
        for activity in activities:
            sequence, start = {"campus_visit": (1, "09:00"), "meal": (2, "12:00"), "park": (3, "18:00")}[
                activity["intentType"]
            ]
            hints.append(
                {
                    "goalId": activity["goalId"],
                    "dayNumber": day,
                    "dayPart": activity["dayPart"],
                    "sequence": sequence,
                    "preferredStartTime": start,
                    "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                    "estimateSource": "controller_estimate",
                    "confidence": 0.9,
                }
            )
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "requestCoverage": coverage,
            "dayStrategies": [
                {
                    "dayNumber": day,
                    "theme": "高校、当地午餐与晚间公园",
                    "requiredGoalIds": goals.copy(),
                    "maxRouteAnchors": 3,
                }
                for day in (1, 2)
            ],
            "occurrenceScheduleHints": hints,
            "routePlanningPolicy": {
                "source": "controller_estimate",
                "objective": "least_generalized_cost",
                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
                "detourEnvelope": deepcopy(
                    recording["pendingRequestContract"]["routeDecisionContract"]["detourTolerance"]
                ),
            },
        },
    }


def test_hand_authored_control_retains_constraints_and_compiles_six_occurrences(recorded_bundle):
    before = deepcopy(recorded_bundle)
    synthetic = hand_authored_correct_decision(recorded_bundle)
    synthetic_before = deepcopy(synthetic)
    assert all(synthetic != raw for raw in recorded_bundle["originalModelDecisions"])
    result, context = run_actual_controller(recorded_bundle, synthetic)
    assert result.controller_full_succeeded, result.controller_error
    assert result.gated_decision.accepted, result.gated_decision.policy_reason_codes
    assert result.decision.primary_action == "draft_itinerary"
    compiled = context["requestIntentContract"]
    assert compiled["requestActivityCoverage"]["status"] == "complete"
    assert compiled["requestActivityCoverage"]["sourceText"] == recorded_bundle["request"]
    assert (
        compiled["requestActivityCoverage"]["sourceFingerprint"]
        == sha256(recorded_bundle["request"].encode()).hexdigest()
    )
    for key in (
        "hardConstraints",
        "entityQualificationConstraint",
        "localExperienceConstraint",
        "routeDecisionContract",
        "experienceSpecs",
    ):
        assert compiled[key] == before["pendingRequestContract"][key]
    assert compiled["entityQualificationConstraint"]["qualificationValue"] == "985"
    assert compiled["localExperienceConstraint"]["experienceType"] == "local_cuisine"
    assert compiled["localExperienceConstraint"]["occurrencesByDay"] == {"1": 1, "2": 1}
    goals = {item["intentType"]: item for item in compiled["requiredIntents"]}
    assert set(goals) == {"campus_visit", "park", "meal"}
    for intent, part in (("campus_visit", "flexible"), ("park", "evening"), ("meal", "noon")):
        goal = goals[intent]
        assert goal["requiredMin"] == goal["minCount"] == goal["maxCount"] == 2
        assert goal["allowedDayNumbers"] == [1, 2]
        assert goal["schedulePreference"]["dayPart"] == part
        assert goal["userExplicit"] is True
    directive = result.decision.action_directive.model_dump(by_alias=True)
    ledger = ConstraintLedgerCompiler().compile(context, directive)
    occurrence_plan = GoalOccurrenceCompiler().compile(ledger, directive)
    occurrences = occurrence_plan.model_dump(by_alias=True)["occurrences"]
    assert len(occurrences) == len({item["occurrenceId"] for item in occurrences}) == 6
    assert Counter((item["intentType"], item["dayNumber"]) for item in occurrences) == Counter(
        {(intent, day): 1 for intent in ("campus_visit", "park", "meal") for day in (1, 2)}
    )
    for item in occurrences:
        goal = goals[item["intentType"]]
        assert item["sourceGoalId"] == goal["goalId"]
        assert item["allowedDayNumbers"] == [1, 2]
        assert item["schedulePreference"]["dayPart"] == goal["schedulePreference"]["dayPart"]
        assert item["requirementLevel"] == "hard" and item["userExplicit"] is True
    assert synthetic == synthetic_before and recorded_bundle == before

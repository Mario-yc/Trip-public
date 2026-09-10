import copy
import json
import sqlite3

import pytest
from fastapi import HTTPException

from src.services.agent_service import AgentService


@pytest.fixture
def source_case():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE conversation_turns (id TEXT, session_id TEXT, role TEXT, status TEXT, content TEXT, agent_request_json TEXT)")
    dates = {"dates": ["2026-10-01", "2026-10-02"], "dayCount": 2}
    db.execute("INSERT INTO conversation_turns VALUES (?, ?, 'user', 'active', ?, ?)", (
        "root", "session", "北京两日游，每晚都去逛公园，10月1日到2日，2天。", json.dumps({"resolvedTripDates": dates}),
    ))
    db.commit()
    service = object.__new__(AgentService)
    service.db = db
    portfolio = {"source_user_turn_id": "root", "summary": {"requestIntentContract": {
        "planningRequestEnvelope": {"city": "北京", "sourcePlanningRootTurnId": "root", "resolvedTripDates": dates},
    }}}
    snapshot = {"days": [{"dayNumber": number, "date": date, "segments": [{
        "id": f"park-{number}", "kind": "park", "startTime": "18:30", "endTime": "20:00",
        "poi": {"latitude": 39.9, "longitude": 116.3},
        # Reproduce the old material: it lost the original evening obligation.
        "semanticMetadata": {"intentType": "park", "schedulePreference": {"dayPart": "afternoon"},
                             "scheduleConstraints": {"explicitStartTime": "14:00"}},
    }]} for number, date in enumerate(dates["dates"], 1)]}
    yield service, portfolio, snapshot
    db.close()


def verify(case):
    service, portfolio, snapshot = case
    service._assert_proposal_original_time_requirements(session_id="session", portfolio=portfolio, snapshot=snapshot)


def test_old_afternoon_metadata_does_not_override_original_evening_request(source_case):
    source_case[2]["days"][0]["segments"][0]["startTime"] = "14:00"
    original = copy.deepcopy(source_case[1:])
    with pytest.raises(HTTPException) as error:
        verify(source_case)
    assert error.value.detail["code"] == "plan_proposal_original_schedule_mismatch"
    assert error.value.detail["details"]["versionDelta"] == 0
    assert source_case[1:] == original
    assert not source_case[0].db.in_transaction


@pytest.mark.parametrize("change", ["date", "missing_day", "relabel", "latitude"])
def test_original_time_check_cannot_pass_with_missing_or_relabelled_occurrence(source_case, change):
    snapshot = source_case[2]
    if change == "date":
        snapshot["days"][0]["date"] = "2026-10-03"
    elif change == "missing_day":
        snapshot["days"].pop()
    elif change == "relabel":
        snapshot["days"][0]["segments"][0].update(kind="scenic", semanticMetadata={})
    else:
        snapshot["days"][0]["segments"][0]["poi"].pop("latitude")
    with pytest.raises(HTTPException, match="409"):
        verify(source_case)


def test_original_time_validation_is_read_only_for_compatible_preview(source_case):
    original = copy.deepcopy(source_case[1:])
    before = source_case[0].db.total_changes
    verify(source_case)
    verify(source_case)
    assert source_case[1:] == original
    assert source_case[0].db.total_changes == before


def test_missing_exact_source_root_is_not_replaced_by_another_turn(source_case):
    source_case[1]["source_user_turn_id"] = "missing"
    with pytest.raises(HTTPException) as error:
        verify(source_case)
    assert error.value.detail["code"] == "plan_proposal_source_context_missing"


def test_continuation_rejects_lost_frozen_time_contract_without_rotating_it(source_case):
    service, portfolio, _snapshot = source_case
    original = copy.deepcopy(portfolio)
    before = service.db.total_changes
    with pytest.raises(HTTPException) as error:
        service._assert_frozen_original_time_requirements(session_id="session", portfolio=portfolio)
    assert error.value.detail["code"] == "simple_direction_original_schedule_contract_outdated"
    assert error.value.detail["details"]["plannerCalled"] is False
    assert portfolio == original and service.db.total_changes == before


def test_continuation_accepts_matching_frozen_time_contract(source_case):
    service, portfolio, _snapshot = source_case
    goals, _dates = service._original_time_requirements(session_id="session", portfolio=portfolio)
    portfolio["summary"]["requestIntentContract"]["requiredIntents"] = goals
    service._assert_frozen_original_time_requirements(session_id="session", portfolio=portfolio)


@pytest.mark.parametrize("change", ["root", "fingerprint"])
def test_original_time_source_rejects_tampered_contract(source_case, change):
    summary = source_case[1]["summary"]
    if change == "root":
        summary["requestIntentContract"]["planningRequestEnvelope"]["sourcePlanningRootTurnId"] = "foreign-root"
    else:
        summary["requestIntentContractMaterialFingerprint"] = "tampered"
    with pytest.raises(HTTPException) as error:
        verify(source_case)
    assert error.value.detail["code"] == "plan_proposal_request_scope_invalid"

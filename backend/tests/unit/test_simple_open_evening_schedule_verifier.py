from __future__ import annotations

import copy

import pytest

from backend.tests.unit.test_simple_open_direction_workflow import _snapshot
from src.services.simple_open_direction_service import SimpleOpenDirectionService


def _evening_snapshot(start_time: str, end_time: str) -> dict:
    snapshot = _snapshot("plan_evening", "公园晚间散步", "B000A")
    segment = snapshot["days"][0]["segments"][0]
    segment.update(startTime=start_time, endTime=end_time, kind="park")
    segment["poi"].update(
        name="测试公园",
        category="scenic",
        type="风景名胜;公园广场;公园",
        providerType="风景名胜;公园广场;公园",
        providerTypeCode="110101",
    )
    segment["semanticMetadata"].update(
        intentType="park",
        rawNeed="每晚都去逛公园",
        schedulePreference={"dayPart": "evening", "userExplicit": True},
        scheduleDecision={
            "constraintPassed": True,
            "startTime": "18:30",
            "endTime": "20:30",
            "scheduleConfidence": "provisional",
            "openingEvidenceStatus": "unverified",
        },
    )
    return snapshot


def test_evening_activation_checks_materialized_clock_despite_claimed_schedule_success() -> None:
    snapshot = _evening_snapshot("14:00", "16:00")
    original = copy.deepcopy(snapshot)

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert "simple_direction_semantic_evening_clock_mismatch" in result["hardFailures"]
    assert snapshot == original


def test_evening_activation_keeps_unknown_opening_provisional_after_local_sunset() -> None:
    result = SimpleOpenDirectionService.activation_verifier(_evening_snapshot("18:30", "20:30"))

    assert result["passed"] is True
    assert result["scheduleSemanticFailureCount"] == 0


@pytest.mark.parametrize("source", [None, "controller_schedule_hint"])
def test_inferred_clock_cannot_bypass_evening_verification(source):
    snapshot = _evening_snapshot("14:00", "16:00")
    snapshot["days"][0]["segments"][0]["semanticMetadata"]["scheduleConstraints"] = {
        "explicitStartTime": "14:00", "source": source,
    }
    assert SimpleOpenDirectionService.activation_verifier(snapshot)["passed"] is False


def test_user_explicit_clock_requires_materialized_clock_to_match():
    snapshot = _evening_snapshot("17:00", "19:00")
    metadata = snapshot["days"][0]["segments"][0]["semanticMetadata"]
    metadata["scheduleConstraints"] = {"explicitStartTime": "17:00", "source": "user_explicit_clock"}
    assert SimpleOpenDirectionService.activation_verifier(snapshot)["passed"] is True
    snapshot["days"][0]["segments"][0]["startTime"] = "14:00"
    assert SimpleOpenDirectionService.activation_verifier(snapshot)["passed"] is False


@pytest.mark.parametrize("missing", ["date", "latitude", "longitude"])
def test_evening_activation_requires_evidence_for_local_semantic_boundary(missing: str) -> None:
    snapshot = _evening_snapshot("18:30", "20:30")
    day = snapshot["days"][0]
    if missing == "date":
        day.pop("date")
    else:
        day["segments"][0]["poi"].pop(missing)

    result = SimpleOpenDirectionService.activation_verifier(snapshot)

    assert result["passed"] is False
    assert "simple_direction_semantic_evening_boundary_unavailable" in result["hardFailures"]

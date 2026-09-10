from __future__ import annotations

import copy
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

from src.services.entity_qualification_evidence_service import (
    EntityQualificationEvidenceService,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER_PATH = REPO_ROOT / "scripts" / "verify_live_two_day_meal_e2e.py"
SPEC = importlib.util.spec_from_file_location("verify_live_two_day_meal_e2e", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _compiled_route_contract_snapshot() -> dict:
    return {
        "routeDecisionContract": {
            "schemaVersion": "route-decision-contract-v2",
            "status": "ready",
            "missingFields": [],
            "mobilityProfile": {
                "source": "controller_semantic_choice",
                "walkingPenaltyMinutesPerKm": 1.8,
                "transferPenaltyMinutes": 6.0,
                "waitTimeMultiplier": 1.0,
                "riskPenaltyMultiplier": 1.0,
            },
            "detourTolerance": {
                "maxGeneralizedCostDelta": 15.0,
                "maxDetourRatio": 0.15,
            },
            "detourToleranceSource": "controller_semantic_choice",
            "provenance": {"transportMode": "transit"},
        }
    }


def _qualification_binding(canonical_name: str) -> dict:
    evidence = EntityQualificationEvidenceService.qualified_entities(
        locality="北京",
        scheme="moe_project_classification",
        value="985",
    )
    assert evidence is not None
    entity = next(item for item in evidence["entities"] if item["canonicalName"] == canonical_name)
    return EntityQualificationEvidenceService.build_binding(
        evidence=evidence,
        entity=entity,
        planning_root_id="root_live_quality",
        request_contract_fingerprint="a" * 64,
    )


def _segment(
    *,
    day_number: int,
    intent_type: str,
    start_time: str,
    poi_name: str,
    amap_id: str,
    latitude: float,
    longitude: float,
    qualification_binding: dict | None = None,
    planning_slot_id: str = "",
    poi_type: str = "",
    poi_category: str = "",
) -> dict:
    inferred_type = poi_type or {
        "campus": "科教文化服务;学校;高等院校",
        "meal": "餐饮服务;中餐厅",
        "park": "风景名胜;公园广场;公园",
        "night_view": "风景名胜;公园广场",
    }.get(intent_type, "")
    return {
        "dayNumber": day_number,
        "intentType": intent_type,
        "startTime": start_time,
        "poiName": poi_name,
        "amapId": amap_id,
        "poiSource": "amap-place-search",
        "latitude": latitude,
        "longitude": longitude,
        "qualificationBinding": qualification_binding or {},
        "planningSlotId": planning_slot_id or f"slot_d{day_number}_{intent_type}",
        "poiType": inferred_type,
        "poiCategory": poi_category or inferred_type,
        "groundingStatus": "verified_amap",
    }


def _reasonable_two_day_evidence() -> dict:
    return {
        "days": [
            {"dayNumber": 1, "date": "2026-10-01"},
            {"dayNumber": 2, "date": "2026-10-02"},
        ],
        "segments": [
            _segment(
                day_number=1,
                intent_type="campus",
                start_time="09:00",
                poi_name="北京大学",
                amap_id="B000A7O5PK",
                latitude=39.9929,
                longitude=116.3109,
                qualification_binding=_qualification_binding("北京大学"),
            ),
            _segment(
                day_number=1,
                intent_type="meal",
                start_time="12:00",
                poi_name="海淀本地餐厅",
                amap_id="B000MEAL001",
                latitude=39.9820,
                longitude=116.3180,
            ),
            _segment(
                day_number=1,
                intent_type="park",
                start_time="18:00",
                poi_name="海淀公园",
                amap_id="B000PARK001",
                latitude=39.9930,
                longitude=116.2990,
            ),
            _segment(
                day_number=2,
                intent_type="campus",
                start_time="09:00",
                poi_name="清华大学",
                amap_id="B000A6EA36",
                latitude=40.0036,
                longitude=116.3269,
                qualification_binding=_qualification_binding("清华大学"),
            ),
            _segment(
                day_number=2,
                intent_type="meal",
                start_time="12:00",
                poi_name="五道口本地餐厅",
                amap_id="B000MEAL002",
                latitude=39.9940,
                longitude=116.3370,
            ),
            _segment(
                day_number=2,
                intent_type="park",
                start_time="18:00",
                poi_name="元大都城垣遗址公园",
                amap_id="B000PARK002",
                latitude=39.9780,
                longitude=116.3460,
            ),
        ],
    }


def _verified_route_snapshot() -> dict:
    snapshot = _compiled_route_contract_snapshot()
    contract = snapshot["routeDecisionContract"]
    contract["fingerprint"] = "f" * 64
    contract["adjacentLegConstraint"] = {
        "candidateSearchRadiusMeters": 8000,
        "maxProviderTravelMinutes": 60,
    }
    contract["topologyConstraint"] = {"maxBacktrackRatio": 0.15}
    evidence = _reasonable_two_day_evidence()
    snapshot["days"] = []
    for day_number in (1, 2):
        day_segments = [segment for segment in evidence["segments"] if segment["dayNumber"] == day_number]
        snapshot["days"].append(
            {
                "dayNumber": day_number,
                "date": f"2026-10-0{day_number}",
                "segments": [
                    {
                        "id": f"seg_d{day_number}_{index}",
                        "startTime": segment["startTime"],
                        "poi": {
                            "name": segment["poiName"],
                            "amapId": segment["amapId"],
                            "source": segment["poiSource"],
                            "latitude": segment["latitude"],
                            "longitude": segment["longitude"],
                            "type": segment["poiType"],
                            "category": segment["poiCategory"],
                        },
                        "semanticMetadata": {
                            "intentType": segment["intentType"],
                            "planningSlotId": segment["planningSlotId"],
                            "groundingStatus": segment["groundingStatus"],
                            "scheduleConstraints": {"qualificationBinding": segment["qualificationBinding"]},
                        },
                    }
                    for index, segment in enumerate(day_segments, start=1)
                ],
            }
        )
    pair_ids = [
        ("B000A7O5PK", "B000MEAL001"),
        ("B000MEAL001", "B000PARK001"),
        ("B000A6EA36", "B000MEAL002"),
        ("B000MEAL002", "B000PARK002"),
    ]
    verified_pairs = [
        {
            "fromAmapId": from_amap_id,
            "toAmapId": to_amap_id,
            "durationSeconds": 1800,
            "distanceMeters": 3500,
            "transportMode": "transit",
        }
        for from_amap_id, to_amap_id in pair_ids
    ]
    snapshot["simpleOpenRouteAssignment"] = {
        "schemaVersion": "simple-open-route-evidence-v2",
        "routeContractFingerprint": "f" * 64,
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "expectedPairs": [{"fromAmapId": item["fromAmapId"], "toAmapId": item["toAmapId"]} for item in verified_pairs],
        "verifiedPairs": verified_pairs,
    }
    return snapshot


def test_two_day_verifier_accepts_server_compiled_mobility_contract() -> None:
    assert verifier.route_contract_errors(_compiled_route_contract_snapshot(), "snapshot") == []


def test_two_day_verifier_rejects_unattributed_or_untyped_mobility_contract() -> None:
    snapshot = copy.deepcopy(_compiled_route_contract_snapshot())
    snapshot["routeDecisionContract"]["mobilityProfile"] = {
        "source": "",
        "transportMode": "transit",
        "paceClass": "standard",
    }

    assert verifier.route_contract_errors(snapshot, "snapshot") == [
        "snapshot:mobility_clarification_not_attributed",
        "snapshot:mobility_clarification_not_typed",
    ]


def test_two_day_verifier_requires_one_noon_meal_on_each_day() -> None:
    evidence = {
        "days": [
            {"dayNumber": 1, "date": "2026-10-01"},
            {"dayNumber": 2, "date": "2026-10-02"},
        ],
        "segments": [
            {"dayNumber": 1, "intentType": "campus", "startTime": "09:00"},
            {"dayNumber": 1, "intentType": "meal", "startTime": "12:00"},
            {"dayNumber": 2, "intentType": "campus", "startTime": "09:00"},
            {"dayNumber": 2, "intentType": "park", "startTime": "18:00"},
        ],
    }

    errors = verifier.journey_semantic_errors(evidence, "snapshot")

    assert "snapshot:explicit_daily_meal_not_distributed_across_both_days" in errors


def test_two_day_verifier_accepts_daily_qualified_compact_journey() -> None:
    assert verifier.journey_semantic_errors(_reasonable_two_day_evidence(), "snapshot") == []


def test_itinerary_evidence_reads_qualification_binding_from_schedule_constraints() -> None:
    binding = _qualification_binding("北京大学")
    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "seg_campus",
                        "startTime": "09:00",
                        "poi": {
                            "name": "北京大学",
                            "amapId": "B000A7O5PK",
                            "source": "amap-place-search",
                            "latitude": 39.9929,
                            "longitude": 116.3109,
                        },
                        "semanticMetadata": {
                            "intentType": "campus",
                            "scheduleConstraints": {"qualificationBinding": binding},
                        },
                    }
                ],
            }
        ]
    }

    evidence = verifier.itinerary_evidence(snapshot)

    assert evidence["segments"][0]["qualificationBinding"] == binding


def test_two_day_verifier_rejects_non_985_binding_and_missing_second_night() -> None:
    evidence = _reasonable_two_day_evidence()
    day_two_campus = next(
        segment for segment in evidence["segments"] if segment["dayNumber"] == 2 and segment["intentType"] == "campus"
    )
    day_two_campus["poiName"] = "北京信息科技大学"
    day_two_campus["qualificationBinding"]["canonicalName"] = "北京信息科技大学"
    evidence["segments"] = [
        segment
        for segment in evidence["segments"]
        if not (segment["dayNumber"] == 2 and segment["intentType"] == "park")
    ]

    errors = verifier.journey_semantic_errors(evidence, "snapshot")

    assert "snapshot:campus_985_qualification_invalid" in errors
    assert "snapshot:explicit_daily_night_not_distributed_across_both_days" in errors


def test_two_day_verifier_rejects_non_campus_poi_reusing_valid_985_binding() -> None:
    evidence = _reasonable_two_day_evidence()
    campus = next(segment for segment in evidence["segments"] if segment["intentType"] == "campus")
    campus["poiName"] = "北京大学附近商场"
    campus["poiType"] = "购物服务;商场"
    campus["poiCategory"] = "shopping"

    errors = verifier.journey_semantic_errors(evidence, "snapshot")

    assert "snapshot:campus_985_qualification_invalid" in errors


def test_two_day_verifier_rejects_adjacent_poi_over_eight_kilometers() -> None:
    evidence = _reasonable_two_day_evidence()
    day_one_meal = next(
        segment for segment in evidence["segments"] if segment["dayNumber"] == 1 and segment["intentType"] == "meal"
    )
    day_one_meal["latitude"] = 39.90
    day_one_meal["longitude"] = 116.50

    errors = verifier.journey_semantic_errors(evidence, "snapshot")

    assert "snapshot:adjacent_poi_distance_exceeds_quality_floor" in errors


def test_two_day_verifier_requires_complete_topology_and_sixty_minute_route_cap() -> None:
    snapshot = _verified_route_snapshot()
    assert verifier.route_quality_errors(snapshot, "snapshot") == []

    snapshot["simpleOpenRouteAssignment"]["topologyCompliance"] = "pending"
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]["durationSeconds"] = 3601

    errors = verifier.route_quality_errors(snapshot, "snapshot")

    assert "snapshot:route_topology_not_verified" in errors
    assert "snapshot:selected_route_exceeds_quality_floor" in errors


def test_two_day_verifier_rejects_empty_route_contract_fingerprints() -> None:
    snapshot = _verified_route_snapshot()
    snapshot["routeDecisionContract"]["fingerprint"] = ""
    snapshot["simpleOpenRouteAssignment"]["routeContractFingerprint"] = ""

    errors = verifier.route_quality_errors(snapshot, "snapshot")

    assert "snapshot:route_contract_fingerprint_mismatch" in errors


def test_two_day_verifier_binds_route_pairs_to_actual_adjacent_segments() -> None:
    snapshot = _verified_route_snapshot()
    duplicate_pair = {
        "fromAmapId": "B000FAKE000",
        "toAmapId": "B000FAKE001",
    }
    snapshot["simpleOpenRouteAssignment"]["expectedPairs"] = [duplicate_pair] * 4
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = [
        {
            **duplicate_pair,
            "durationSeconds": 1800,
            "distanceMeters": 3500,
            "transportMode": "transit",
        }
    ] * 4

    errors = verifier.route_quality_errors(snapshot, "snapshot")

    assert "snapshot:route_pair_identity_mismatch" in errors


def test_two_day_verifier_binds_every_materialized_poi_to_its_live_amap_event() -> None:
    snapshot = _verified_route_snapshot()
    events = []
    for day in snapshot["days"]:
        for segment in day["segments"]:
            events.append(
                {
                    "type": "simple_open_tool_call",
                    "providerName": "amap-place-search",
                    "status": "completed",
                    "fallbackUsed": False,
                    "metadata": {
                        "providerOutcome": "success",
                        "cacheHit": False,
                        "resultCount": 1,
                        "queryFingerprint": "a" * 16,
                        "selectedAmapId": segment["poi"]["amapId"],
                        "slotKey": segment["semanticMetadata"]["planningSlotId"],
                    },
                }
            )
    assert verifier.amap_lineage_errors(snapshot, events, "snapshot") == []

    snapshot["days"][1]["segments"][1]["poi"]["amapId"] = "B999FAKE001"

    errors = verifier.amap_lineage_errors(snapshot, events, "snapshot")

    assert "snapshot:materialized_amap_identity_without_live_search" in errors


def test_two_day_verifier_rejects_empty_or_placeholder_night_experience() -> None:
    evidence = _reasonable_two_day_evidence()
    day_two_night = next(
        segment for segment in evidence["segments"] if segment["dayNumber"] == 2 and segment["intentType"] == "park"
    )
    day_two_night["poiName"] = ""

    errors = verifier.journey_semantic_errors(evidence, "snapshot")

    assert "snapshot:night_public_experience_invalid" in errors


def test_two_day_verifier_rejects_generic_night_observation_deck() -> None:
    evidence = _reasonable_two_day_evidence()
    day_two_night = next(
        segment for segment in evidence["segments"] if segment["dayNumber"] == 2 and segment["intentType"] == "park"
    )
    day_two_night["poiName"] = "夜景观景台"
    day_two_night["poiType"] = "风景名胜;观景台"
    day_two_night["poiCategory"] = "scenic"

    errors = verifier.journey_semantic_errors(evidence, "snapshot")

    assert "snapshot:night_public_experience_invalid" in errors


def test_two_day_verifier_rejects_non_finite_route_metrics() -> None:
    snapshot = _verified_route_snapshot()
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]["durationSeconds"] = math.nan
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]["distanceMeters"] = math.nan

    errors = verifier.route_quality_errors(snapshot, "snapshot")

    assert "snapshot:selected_route_exceeds_quality_floor" in errors


@pytest.mark.parametrize(
    (
        "journey_commit",
        "summary_commit",
        "expected_commit",
        "expected_failures",
    ),
    [
        (
            "b" * 40,
            "a" * 40,
            "a" * 40,
            {"artifact_git_commit_mismatch", "journey_git_commit_mismatch"},
        ),
        (
            "a" * 40,
            "b" * 40,
            "a" * 40,
            {"artifact_git_commit_mismatch", "run_summary_git_commit_mismatch"},
        ),
        (
            "a" * 40,
            "a" * 40,
            "b" * 40,
            {"journey_git_commit_mismatch", "run_summary_git_commit_mismatch"},
        ),
    ],
)
def test_two_day_verifier_main_rejects_any_artifact_commit_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journey_commit: str,
    summary_commit: str,
    expected_commit: str,
    expected_failures: set[str],
) -> None:
    journey_path = tmp_path / "journey-result.json"
    summary_path = tmp_path / "run-summary.json"
    output_path = tmp_path / "database-verification.json"
    journey_path.write_text(
        json.dumps({"gitCommit": journey_commit}),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps({"gitCommit": summary_commit}),
        encoding="utf-8",
    )

    def fail_if_database_is_read(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("commit mismatch must fail before SQLite verification")

    monkeypatch.setattr(verifier, "verify_database", fail_if_database_is_read)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER_PATH),
            "--database",
            str(tmp_path / "trip-e2e.db"),
            "--journey-result",
            str(journey_path),
            "--run-summary",
            str(summary_path),
            "--expected-git-commit",
            expected_commit,
            "--output",
            str(output_path),
            "--deepseek-endpoint-host",
            "api.deepseek.com",
        ],
    )

    assert verifier.main() == 1
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert set(report["failures"]) == expected_failures
    assert report["gitCommit"] == expected_commit


def test_two_day_verifier_main_records_matching_artifact_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_commit = "a" * 40
    journey_path = tmp_path / "journey-result.json"
    summary_path = tmp_path / "run-summary.json"
    output_path = tmp_path / "database-verification.json"
    journey_path.write_text(
        json.dumps({"gitCommit": git_commit}),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps({"gitCommit": git_commit}),
        encoding="utf-8",
    )

    def passing_database_report(
        _database: Path,
        _journey: dict,
        _deepseek_endpoint_host: str,
    ) -> dict:
        return {"passed": True, "failures": []}

    monkeypatch.setattr(verifier, "verify_database", passing_database_report)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER_PATH),
            "--database",
            str(tmp_path / "trip-e2e.db"),
            "--journey-result",
            str(journey_path),
            "--run-summary",
            str(summary_path),
            "--expected-git-commit",
            git_commit,
            "--output",
            str(output_path),
            "--deepseek-endpoint-host",
            "api.deepseek.com",
        ],
    )

    assert verifier.main() == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report == {"passed": True, "failures": [], "gitCommit": git_commit}


def test_live_runner_passes_frozen_commit_to_both_authorized_verifiers() -> None:
    runner = (REPO_ROOT / "scripts" / "run-live-portfolio-e2e.ps1").read_text(encoding="utf-8")
    block_start = runner.index('if (\n            $JourneyMode -eq "simple_direction"')
    block_end = runner.index("        if ($isSimpleDirectionJourney)", block_start)
    commit_binding_block = runner[block_start:block_end]

    assert '$JourneyMode -eq "two_day_meal_regression"' in commit_binding_block
    assert '$JourneyMode -eq "simple_direction"' in commit_binding_block
    assert '"--run-summary"' in commit_binding_block
    assert "$runSummary" in commit_binding_block
    assert '"--expected-git-commit"' in commit_binding_block
    assert "$frozenGitCommit" in commit_binding_block

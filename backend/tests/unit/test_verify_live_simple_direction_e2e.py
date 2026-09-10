from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER_PATH = REPO_ROOT / "scripts" / "verify_live_simple_direction_e2e.py"
SPEC = importlib.util.spec_from_file_location("verify_live_simple_direction_e2e", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_source_attribution_probe(
    *,
    powershell: str,
    tmp_path: Path,
    repo: Path,
    expected_commit: str,
    probe_name: str,
    required_source_paths: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    helper = REPO_ROOT / "scripts" / "live-e2e-source-attribution.ps1"
    probe = tmp_path / f"source-attribution-{probe_name}.ps1"
    required_paths_literal = (
        "@()"
        if not required_source_paths
        else "@(" + ", ".join(_powershell_literal(path) for path in required_source_paths) + ")"
    )
    probe.write_text(
        "\n".join(
            (
                f". {_powershell_literal(helper)}",
                "$result = Test-LiveE2ESourceAttribution "
                f"-RepoRoot {_powershell_literal(repo)} "
                f"-ExpectedGitCommit {_powershell_literal(expected_commit)} "
                f"-RequiredSourcePaths {required_paths_literal}",
                "$result | ConvertTo-Json -Compress",
                "if (-not $result.verified) { exit 1 }",
            )
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _probe_payload(result: subprocess.CompletedProcess[str]) -> dict:
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("{")]
    assert lines, result.stderr or result.stdout
    return json.loads(lines[-1])


def _compact_route_snapshot(*, duration_seconds: int = 1800) -> dict:
    days = [
        {
            "id": "day_1",
            "dayNumber": 1,
            "segments": [
                _materialized_route_segment(
                    segment_id="seg_route_1",
                    amap_id="B000000001",
                    planning_slot_id="slot_route_1",
                ),
                _materialized_route_segment(
                    segment_id="seg_route_2",
                    amap_id="B000000002",
                    planning_slot_id="slot_route_2",
                ),
            ],
        }
    ]
    return {
        "days": days,
        "portfolioPendingSlots": [],
        "routeDecisionContract": {
            "schemaVersion": "route-decision-contract-v2",
            "status": "ready",
            "missingFields": [],
            "mobilityProfile": {"transportMode": "transit"},
            "detourTolerance": {
                "maxGeneralizedCostDelta": 10,
                "maxDetourRatio": 0.2,
            },
            "adjacentLegConstraint": {
                "candidateSearchRadiusMeters": 5000,
                "maxProviderTravelMinutes": 45,
            },
            "topologyConstraint": {"maxBacktrackRatio": 0.15},
            "fingerprint": "route-contract-fingerprint",
        },
        "simpleOpenRouteAssignment": {
            "schemaVersion": "simple-open-route-evidence-v2",
            "routeContractFingerprint": "route-contract-fingerprint",
            "routeCoverageComplete": True,
            "adjacentLegCompliance": "verified",
            "topologyCompliance": "verified",
            "providerBaselineCompared": False,
            "detourCompliance": "not_evaluated",
            "expectedPairs": [
                {
                    "dayNumber": 1,
                    "pairOrdinal": 1,
                    "fromSegmentId": "seg_route_1",
                    "toSegmentId": "seg_route_2",
                    "fromAmapId": "B000000001",
                    "toAmapId": "B000000002",
                }
            ],
            "verifiedPairs": [
                {
                    "dayNumber": 1,
                    "pairOrdinal": 1,
                    "fromSegmentId": "seg_route_1",
                    "toSegmentId": "seg_route_2",
                    "fromAmapId": "B000000001",
                    "toAmapId": "B000000002",
                    "transportMode": "transit",
                    "durationSeconds": duration_seconds,
                    "distanceMeters": 3200,
                }
            ],
        },
    }


def _materialized_route_segment(
    *,
    segment_id: str,
    amap_id: str,
    planning_slot_id: str,
    start_time: str = "09:00",
    end_time: str = "10:00",
    kind: str = "visit",
    day_number: int | None = None,
    requires_route_edge: bool | None = True,
    route_anchor: bool = True,
    intent_type: str = "visit",
    goal_id: str = "",
    occurrence_id: str = "",
    completion_required: bool | None = None,
    day_completion_required: bool | None = None,
    lineage_authority: str = "goal_occurrence_compiler",
    user_explicit: bool | None = None,
) -> dict:
    semantic = {
        "intentType": intent_type,
        "planningSlotId": planning_slot_id,
        "groundingStatus": "verified_amap",
        "routeAnchor": route_anchor,
    }
    if day_number is not None:
        semantic["dayNumber"] = day_number
    if goal_id:
        semantic["goalId"] = goal_id
        semantic["sourceGoalId"] = goal_id
    if occurrence_id:
        semantic["occurrenceId"] = occurrence_id
    if requires_route_edge is not None:
        semantic["requiresRouteEdge"] = requires_route_edge
    if completion_required is not None:
        semantic["completionRequired"] = completion_required
    if day_completion_required is not None:
        semantic["dayCompletionRequired"] = day_completion_required
    if lineage_authority:
        semantic["lineageAuthority"] = lineage_authority
    if user_explicit is not None:
        semantic["userExplicit"] = user_explicit
    return {
        "id": segment_id,
        "kind": kind,
        "startTime": start_time,
        "endTime": end_time,
        "durationMinutes": verifier.minutes_between(start_time, end_time) or 0,
        "poi": {
            "id": f"poi_{segment_id}",
            "amapId": amap_id,
            "name": f"POI {segment_id}",
            "latitude": 39.9,
            "longitude": 116.4,
            "source": "amap-place-search",
            "groundingStatus": "verified_amap",
            "intentType": intent_type,
        },
        "semanticMetadata": semantic,
    }


def _route_pair(
    *,
    day_number: int,
    pair_ordinal: int,
    from_segment_id: str,
    to_segment_id: str,
    from_amap_id: str,
    to_amap_id: str,
    duration_seconds: int = 1800,
    distance_meters: int = 3200,
) -> dict:
    return {
        "dayNumber": day_number,
        "pairOrdinal": pair_ordinal,
        "fromSegmentId": from_segment_id,
        "toSegmentId": to_segment_id,
        "fromAmapId": from_amap_id,
        "toAmapId": to_amap_id,
        "transportMode": "transit",
        "durationSeconds": duration_seconds,
        "distanceMeters": distance_meters,
    }


def test_live_verifier_accepts_complete_compact_route_evidence() -> None:
    snapshot = _compact_route_snapshot()

    assert verifier.route_contract_ready(snapshot) is True
    assert verifier.compact_route_evidence_errors(snapshot) == []


def test_live_verifier_rejects_transit_leg_over_45_minutes() -> None:
    snapshot = _compact_route_snapshot(duration_seconds=2701)

    assert verifier.compact_route_evidence_errors(snapshot) == ["verified_route_pair_invalid"]


def test_route_alternative_budget_evidence_reconstructs_per_day_calls() -> None:
    snapshot = _compact_route_snapshot()
    snapshot["simpleOpenRouteAssignment"].update(
        {
            "routeProviderAttemptCount": 6,
            "topologyCandidateAttempts": [
                {"dayNumber": 1, "candidateRank": 1, "providerAttemptCountAfter": 2},
                {"dayNumber": 2, "candidateRank": 1, "providerAttemptCountAfter": 4},
                {"dayNumber": 1, "candidateRank": 2, "providerAttemptCountAfter": 6},
                {
                    "dayNumber": 1,
                    "candidateRank": 3,
                    "status": "not_attempted_day_alternative_route_budget_exhausted",
                    "dayAlternativeProviderAttemptCount": 2,
                    "dayAlternativeProviderAttemptLimit": 2,
                },
            ],
        }
    )

    evidence = verifier.route_alternative_budget_evidence(snapshot)

    assert evidence["verified"] is True
    assert evidence["alternativeProviderAttemptCountByDay"] == {"1": 2}


def test_route_alternative_budget_evidence_rejects_more_than_two_calls_per_day() -> None:
    snapshot = _compact_route_snapshot()
    snapshot["simpleOpenRouteAssignment"].update(
        {
            "routeProviderAttemptCount": 5,
            "topologyCandidateAttempts": [
                {"dayNumber": 1, "candidateRank": 1, "providerAttemptCountAfter": 2},
                {"dayNumber": 1, "candidateRank": 2, "providerAttemptCountAfter": 5},
            ],
        }
    )

    evidence = verifier.route_alternative_budget_evidence(snapshot)

    assert evidence["verified"] is False
    assert "day_alternative_route_budget_exceeded:1:3" in evidence["errors"]


def test_route_alternative_budget_evidence_rejects_total_over_eight_and_missing_identity() -> None:
    snapshot = _compact_route_snapshot()
    snapshot["simpleOpenRouteAssignment"].update(
        {
            "routeProviderAttemptCount": 9,
            "topologyCandidateAttempts": [
                {"dayNumber": 1, "providerAttemptCountAfter": 9},
            ],
        }
    )

    evidence = verifier.route_alternative_budget_evidence(snapshot)

    assert evidence["verified"] is False
    assert "route_provider_attempt_budget_exceeded:9" in evidence["errors"]
    assert "route_attempt_identity_invalid:0" in evidence["errors"]
    assert "route_provider_attempt_audit_missing" in evidence["errors"]


def _explicit_every_day_meal_contract() -> dict:
    return {
        "requiredIntents": [
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "userExplicit": True,
                "allowedDayNumbers": [1, 2],
                "distributionPolicy": "every_allowed_day",
                "cardinalitySource": "explicit_every_day",
            }
        ]
    }


def test_live_verifier_rejects_collapsed_route_pair_identity_across_days() -> None:
    snapshot = _compact_route_snapshot()
    snapshot["days"] = [
        {
            "id": "day_1",
            "dayNumber": 1,
            "segments": [
                _materialized_route_segment(
                    segment_id="seg_day1_a",
                    amap_id="B000DAYPAIR",
                    planning_slot_id="slot_day1_a",
                ),
                _materialized_route_segment(
                    segment_id="seg_day1_b",
                    amap_id="B000DAYPAIR2",
                    planning_slot_id="slot_day1_b",
                ),
            ],
        },
        {
            "id": "day_2",
            "dayNumber": 2,
            "segments": [
                _materialized_route_segment(
                    segment_id="seg_day2_a",
                    amap_id="B000DAYPAIR",
                    planning_slot_id="slot_day2_a",
                ),
                _materialized_route_segment(
                    segment_id="seg_day2_b",
                    amap_id="B000DAYPAIR2",
                    planning_slot_id="slot_day2_b",
                ),
            ],
        },
    ]
    snapshot["simpleOpenRouteAssignment"]["expectedPairs"] = [
        _route_pair(
            day_number=1,
            pair_ordinal=1,
            from_segment_id="seg_day1_a",
            to_segment_id="seg_day1_b",
            from_amap_id="B000DAYPAIR",
            to_amap_id="B000DAYPAIR2",
        ),
        _route_pair(
            day_number=2,
            pair_ordinal=1,
            from_segment_id="seg_day2_a",
            to_segment_id="seg_day2_b",
            from_amap_id="B000DAYPAIR",
            to_amap_id="B000DAYPAIR2",
        ),
    ]
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = [
        _route_pair(
            day_number=1,
            pair_ordinal=1,
            from_segment_id="seg_day1_a",
            to_segment_id="seg_day1_b",
            from_amap_id="B000DAYPAIR",
            to_amap_id="B000DAYPAIR2",
        )
    ]

    assert "route_pair_identity_mismatch" in verifier.compact_route_evidence_errors(snapshot)


def test_live_verifier_derives_actual_pairs_from_requires_route_edge_without_legacy_override() -> None:
    snapshot = _compact_route_snapshot()
    snapshot["days"] = [
        {
            "id": "day_1",
            "dayNumber": 1,
            "segments": [
                _materialized_route_segment(
                    segment_id="seg_museum",
                    amap_id="B000MUSEUM1",
                    planning_slot_id="slot_museum",
                    start_time="09:00",
                    end_time="10:00",
                ),
                _materialized_route_segment(
                    segment_id="seg_lunch",
                    amap_id="B000LUNCH01",
                    planning_slot_id="day1_lunch",
                    start_time="12:00",
                    end_time="13:00",
                    kind="meal",
                    intent_type="meal",
                    requires_route_edge=False,
                    route_anchor=True,
                ),
                _materialized_route_segment(
                    segment_id="seg_night",
                    amap_id="B000NIGHT01",
                    planning_slot_id="slot_night",
                    start_time="19:00",
                    end_time="20:00",
                ),
            ],
        }
    ]
    snapshot["simpleOpenRouteAssignment"]["expectedPairs"] = [
        _route_pair(
            day_number=1,
            pair_ordinal=1,
            from_segment_id="seg_museum",
            to_segment_id="seg_lunch",
            from_amap_id="B000MUSEUM1",
            to_amap_id="B000LUNCH01",
        ),
        _route_pair(
            day_number=1,
            pair_ordinal=2,
            from_segment_id="seg_lunch",
            to_segment_id="seg_night",
            from_amap_id="B000LUNCH01",
            to_amap_id="B000NIGHT01",
        ),
    ]
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = copy.deepcopy(
        snapshot["simpleOpenRouteAssignment"]["expectedPairs"]
    )

    assert "route_pair_identity_mismatch" in verifier.compact_route_evidence_errors(snapshot)


def test_live_verifier_accepts_legacy_real_amap_stop_without_requires_route_edge_field() -> None:
    snapshot = _compact_route_snapshot()
    snapshot["days"] = [
        {
            "id": "day_1",
            "dayNumber": 1,
            "segments": [
                _materialized_route_segment(
                    segment_id="seg_museum",
                    amap_id="B000MUSEUM1",
                    planning_slot_id="slot_museum",
                ),
                _materialized_route_segment(
                    segment_id="seg_lunch",
                    amap_id="B000LUNCH01",
                    planning_slot_id="day1_lunch",
                    kind="meal",
                    intent_type="meal",
                    requires_route_edge=None,
                    route_anchor=False,
                ),
                _materialized_route_segment(
                    segment_id="seg_night",
                    amap_id="B000NIGHT01",
                    planning_slot_id="slot_night",
                ),
            ],
        }
    ]
    expected_pairs = [
        _route_pair(
            day_number=1,
            pair_ordinal=1,
            from_segment_id="seg_museum",
            to_segment_id="seg_lunch",
            from_amap_id="B000MUSEUM1",
            to_amap_id="B000LUNCH01",
        ),
        _route_pair(
            day_number=1,
            pair_ordinal=2,
            from_segment_id="seg_lunch",
            to_segment_id="seg_night",
            from_amap_id="B000LUNCH01",
            to_amap_id="B000NIGHT01",
        ),
    ]
    snapshot["simpleOpenRouteAssignment"]["expectedPairs"] = expected_pairs
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = copy.deepcopy(expected_pairs)

    assert verifier.compact_route_evidence_errors(snapshot) == []


def test_live_verifier_accepts_explicit_every_day_meal_completion_when_each_day_materialized_once() -> None:
    snapshot = {
        "days": [
            {
                "id": "day_1",
                "dayNumber": 1,
                "segments": [
                    _materialized_route_segment(
                        segment_id="seg_day1_lunch",
                        amap_id="B000MEAL01A",
                        planning_slot_id="day1_lunch",
                        start_time="12:00",
                        end_time="13:00",
                        kind="meal",
                        intent_type="meal",
                        goal_id="goal_meal",
                        occurrence_id="occ:goal_meal:day:1",
                        completion_required=True,
                    )
                ],
            },
            {
                "id": "day_2",
                "dayNumber": 2,
                "segments": [
                    _materialized_route_segment(
                        segment_id="seg_day2_lunch",
                        amap_id="B000MEAL02A",
                        planning_slot_id="day2_lunch",
                        start_time="12:30",
                        end_time="13:30",
                        kind="meal",
                        intent_type="meal",
                        goal_id="goal_meal",
                        occurrence_id="occ:goal_meal:day:2",
                        completion_required=True,
                    )
                ],
            },
        ],
        "portfolioPendingSlots": [],
    }

    assert verifier.explicit_every_day_meal_errors(snapshot, _explicit_every_day_meal_contract()) == []


def test_live_verifier_rejects_explicit_every_day_meal_when_day_missing_or_pending() -> None:
    snapshot = {
        "days": [
            {
                "id": "day_1",
                "dayNumber": 1,
                "segments": [
                    _materialized_route_segment(
                        segment_id="seg_day1_lunch",
                        amap_id="B000MEAL01A",
                        planning_slot_id="day1_lunch",
                        start_time="12:00",
                        end_time="13:00",
                        kind="meal",
                        intent_type="meal",
                        goal_id="goal_meal",
                        occurrence_id="occ:goal_meal:day:1",
                    )
                ],
            },
            {"id": "day_2", "dayNumber": 2, "segments": []},
        ],
        "portfolioPendingSlots": [
            {
                "id": "pending:day2_lunch",
                "planningSlotId": "day2_lunch",
                "poolId": "meal_pool",
                "dayNumber": 2,
                "startTime": "12:00",
                "endTime": "13:00",
                "timeWindow": "12:00-13:00",
                "intentType": "meal",
                "goalId": "goal_meal",
                "sourceGoalId": "goal_meal",
                "occurrenceId": "occ:goal_meal:day:2",
                "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
                "requirementLevel": "explicit_soft",
                "groundingStatus": "unresolved",
                "userExplicit": True,
                "allowedDayNumbers": [1, 2],
                "distributionPolicy": "every_allowed_day",
                "cardinalitySource": "explicit_every_day",
                "completionRequired": True,
                "lineageAuthority": "simple_open_request_contract_every_day_meal",
            }
        ],
    }

    assert verifier.explicit_every_day_meal_errors(snapshot, _explicit_every_day_meal_contract()) == [
        "explicit_every_day_meal_occurrence_count_invalid:goal_meal:2:0",
        "explicit_every_day_meal_pending_completion_required:goal_meal:2",
    ]


def _standard_two_day_daily_completion_snapshot() -> dict:
    day_1 = [
        _materialized_route_segment(
            segment_id="seg_day1_campus",
            amap_id="B000DAY1CAMP",
            planning_slot_id="day1_campus",
            start_time="09:00",
            end_time="10:30",
            kind="campus",
            day_number=1,
            intent_type="campus_visit",
            goal_id="goal_campus",
            occurrence_id="occ:goal_campus:day:1",
            completion_required=True,
            user_explicit=True,
        ),
        _materialized_route_segment(
            segment_id="seg_day1_meal",
            amap_id="B000DAY1MEAL",
            planning_slot_id="day1_lunch",
            start_time="12:00",
            end_time="13:00",
            kind="meal",
            day_number=1,
            intent_type="meal",
            goal_id="goal_meal",
            occurrence_id="occ:goal_meal:day:1",
            completion_required=True,
            lineage_authority="simple_open_request_contract_every_day_meal",
            user_explicit=True,
        ),
        _materialized_route_segment(
            segment_id="seg_day1_night",
            amap_id="B000DAY1NGHT",
            planning_slot_id="day1_night",
            start_time="18:30",
            end_time="20:00",
            kind="night_view",
            day_number=1,
            intent_type="night_view",
            goal_id="goal_night",
            occurrence_id="occ:goal_night:day:1",
            completion_required=True,
            user_explicit=True,
        ),
    ]
    day_2 = [
        _materialized_route_segment(
            segment_id="seg_day2_campus",
            amap_id="B000DAY2CAMP",
            planning_slot_id="day2_campus",
            start_time="09:00",
            end_time="10:30",
            kind="campus",
            day_number=2,
            intent_type="campus_visit",
            goal_id="goal_campus",
            occurrence_id="occ:goal_campus:day:2",
            completion_required=True,
            user_explicit=True,
        ),
        _materialized_route_segment(
            segment_id="seg_day2_meal",
            amap_id="B000DAY2MEAL",
            planning_slot_id="day2_lunch",
            start_time="12:00",
            end_time="13:00",
            kind="meal",
            day_number=2,
            intent_type="meal",
            goal_id="goal_meal",
            occurrence_id="occ:goal_meal:day:2",
            completion_required=True,
            lineage_authority="simple_open_request_contract_every_day_meal",
            user_explicit=True,
        ),
        _materialized_route_segment(
            segment_id="seg_day2_completion",
            amap_id="B000DAY2PARK",
            planning_slot_id="day2_daily_completion_1",
            start_time="14:00",
            end_time="15:00",
            kind="park",
            day_number=2,
            intent_type="park",
            goal_id="goal_daily_completion_day_2",
            occurrence_id="occ:goal_daily_completion_day_2:day:2",
            completion_required=False,
            day_completion_required=True,
            lineage_authority="simple_open_daily_completion_policy",
            user_explicit=False,
        ),
    ]
    snapshot = _compact_route_snapshot()
    snapshot.update(
        {
            "simpleOpenExecutionProfile": "simple_open_v1",
            "desiredDensityAnchorTargets": {"1": 3, "2": 3},
            "days": [
                {"id": "day_1", "dayNumber": 1, "segments": day_1},
                {"id": "day_2", "dayNumber": 2, "segments": day_2},
            ],
            "portfolioPendingSlots": [],
        }
    )
    snapshot["routeDecisionContract"]["mobilityProfile"] = {
        "transportMode": "transit",
        "paceClass": "standard",
    }
    expected_pairs = [
        _route_pair(
            day_number=day_number,
            pair_ordinal=pair_ordinal,
            from_segment_id=left["id"],
            to_segment_id=right["id"],
            from_amap_id=left["poi"]["amapId"],
            to_amap_id=right["poi"]["amapId"],
        )
        for day_number, segments in ((1, day_1), (2, day_2))
        for pair_ordinal, (left, right) in enumerate(zip(segments, segments[1:]), start=1)
    ]
    verified_pairs = copy.deepcopy(expected_pairs)
    for pair in verified_pairs:
        pair.update(
            {
                "provider": "amap-webservice",
                "queriedAt": "2026-08-30T10:00:00+00:00",
            }
        )
        pair["providerEvidenceFingerprint"] = verifier.route_provider_evidence_fingerprint(pair)
    snapshot["simpleOpenRouteAssignment"].update({"expectedPairs": expected_pairs, "verifiedPairs": verified_pairs})
    return snapshot


def test_live_verifier_accepts_standard_two_day_daily_completion_with_exact_route_lineage() -> None:
    evidence = verifier.standard_two_day_daily_completion_evidence(_standard_two_day_daily_completion_snapshot())

    assert evidence["verified"] is True
    assert evidence["errors"] == []
    assert evidence["dayAnchorTargets"] == {"1": 3, "2": 3}
    assert evidence["dayAnchorActuals"] == {"1": 3, "2": 3}
    assert evidence["day2CompletionSegmentId"] == "seg_day2_completion"


def test_live_verifier_rejects_latest_sparse_day2_shape_even_when_remaining_routes_are_real() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    snapshot["days"][1]["segments"] = snapshot["days"][1]["segments"][:2]
    remaining_pairs = [
        pair
        for pair in snapshot["simpleOpenRouteAssignment"]["expectedPairs"]
        if not (pair["dayNumber"] == 2 and pair["toSegmentId"] == "seg_day2_completion")
    ]
    snapshot["simpleOpenRouteAssignment"]["expectedPairs"] = remaining_pairs
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = [
        pair
        for pair in snapshot["simpleOpenRouteAssignment"]["verifiedPairs"]
        if not (pair["dayNumber"] == 2 and pair["toSegmentId"] == "seg_day2_completion")
    ]

    assert verifier.compact_route_evidence_errors(snapshot) == []
    evidence = verifier.standard_two_day_daily_completion_evidence(snapshot)
    assert evidence["verified"] is False
    assert "day2_anchor_target_mismatch:3:2" in evidence["errors"]
    assert "day2_daily_completion_materialized_count_invalid:0" in evidence["errors"]


def test_live_verifier_does_not_count_pending_daily_completion_as_success() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    snapshot["portfolioPendingSlots"] = [
        {
            "id": "pending:day2_daily_completion_1",
            "planningSlotId": "day2_daily_completion_1",
            "poolId": "daily_completion_day_2_pool",
            "dayNumber": 2,
            "intentType": "park",
            "goalId": "goal_daily_completion_day_2",
            "sourceGoalId": "goal_daily_completion_day_2",
            "occurrenceId": "occ:goal_daily_completion_day_2:day:2",
            "lineageAuthority": "simple_open_daily_completion_policy",
            "completionRequired": False,
            "dayCompletionRequired": True,
            "groundingStatus": "unresolved",
        }
    ]

    evidence = verifier.standard_two_day_daily_completion_evidence(snapshot)
    assert evidence["verified"] is False
    assert "day2_daily_completion_pending" in evidence["errors"]


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        (
            "lineageAuthority",
            "goal_occurrence_compiler",
            "day2_daily_completion_lineage_authority_invalid",
        ),
        (
            "dayCompletionRequired",
            False,
            "day2_daily_completion_required_flag_missing",
        ),
        (
            "completionRequired",
            True,
            "day2_daily_completion_user_completion_flag_invalid",
        ),
    ],
)
def test_live_verifier_rejects_daily_completion_lineage_drift(
    field: str,
    value: object,
    expected_error: str,
) -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    completion_metadata = snapshot["days"][1]["segments"][2]["semanticMetadata"]
    completion_metadata[field] = value

    evidence = verifier.standard_two_day_daily_completion_evidence(snapshot)
    assert evidence["verified"] is False
    assert expected_error in evidence["errors"]


def test_live_verifier_rejects_daily_completion_route_without_official_provider_receipt() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    day_2_completion_pair = next(
        pair
        for pair in snapshot["simpleOpenRouteAssignment"]["verifiedPairs"]
        if pair["dayNumber"] == 2 and pair["pairOrdinal"] == 2
    )
    day_2_completion_pair["provider"] = "fixture-route"

    evidence = verifier.standard_two_day_daily_completion_evidence(snapshot)
    assert evidence["verified"] is False
    assert "day2_daily_completion_route_provider_evidence_invalid:2" in evidence["errors"]


def test_live_verifier_rejects_day2_completion_before_noon_meal_even_with_matching_routes() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    day_2_segments = snapshot["days"][1]["segments"]
    snapshot["days"][1]["segments"] = [
        day_2_segments[0],
        day_2_segments[2],
        day_2_segments[1],
    ]
    reordered = snapshot["days"][1]["segments"]
    day_1_pairs = [pair for pair in snapshot["simpleOpenRouteAssignment"]["expectedPairs"] if pair["dayNumber"] == 1]
    day_2_pairs = [
        _route_pair(
            day_number=2,
            pair_ordinal=pair_ordinal,
            from_segment_id=left["id"],
            to_segment_id=right["id"],
            from_amap_id=left["poi"]["amapId"],
            to_amap_id=right["poi"]["amapId"],
        )
        for pair_ordinal, (left, right) in enumerate(zip(reordered, reordered[1:]), start=1)
    ]
    snapshot["simpleOpenRouteAssignment"]["expectedPairs"] = [*day_1_pairs, *day_2_pairs]
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = copy.deepcopy(
        snapshot["simpleOpenRouteAssignment"]["expectedPairs"]
    )
    for pair in snapshot["simpleOpenRouteAssignment"]["verifiedPairs"]:
        pair.update(
            {
                "provider": "amap-webservice",
                "queriedAt": "2026-08-30T10:00:00+00:00",
            }
        )
        pair["providerEvidenceFingerprint"] = verifier.route_provider_evidence_fingerprint(pair)

    assert verifier.compact_route_evidence_errors(snapshot) == []
    evidence = verifier.standard_two_day_daily_completion_evidence(snapshot)
    assert evidence["verified"] is False
    assert "day2_semantic_route_order_invalid" in evidence["errors"]


def _current_direction_adjacent_search_response(
    snapshot: dict,
    *,
    seed_overrides: dict[str, str] | None = None,
    include_claimed_scope_mismatch: bool = False,
) -> dict:
    seed_overrides = seed_overrides or {}
    campus_by_day = {
        int(day["dayNumber"]): next(
            segment["poi"]["amapId"]
            for segment in day["segments"]
            if segment["semanticMetadata"]["intentType"] == "campus_visit"
        )
        for day in snapshot["days"]
    }
    steps = []
    for day in snapshot["days"]:
        day_number = int(day["dayNumber"])
        for segment in day["segments"]:
            metadata = segment["semanticMetadata"]
            if metadata["intentType"] not in {"meal", "park"}:
                continue
            slot_id = metadata["planningSlotId"]
            steps.append(
                {
                    "type": "simple_open_tool_call",
                    "providerName": "amap-place-search",
                    "status": "completed",
                    "metadata": {
                        "providerOutcome": "success",
                        "cacheHit": False,
                        "resultCount": 3,
                        "queryFingerprint": "a" * 16,
                        "selectedAmapId": segment["poi"]["amapId"],
                        "slotKey": slot_id,
                        "searchScope": "nearby_low_detour",
                        "daySeedAmapId": seed_overrides.get(slot_id, campus_by_day[day_number]),
                        "anchorAmapId": campus_by_day[day_number],
                        "queryScopeFingerprint": "b" * 64,
                        "radiusMeters": 5000,
                    },
                }
            )
    if include_claimed_scope_mismatch:
        steps.append(
            {
                "type": "simple_open_slot_unresolved",
                "providerName": None,
                "status": "fallback",
                "metadata": {
                    "slotKey": "day2_daily_completion_1",
                    "reasonCode": "simple_direction_claimed_adjacent_scope_mismatch",
                    "providerCalled": False,
                },
            }
        )
    return {"planningSteps": steps}


def test_live_verifier_accepts_each_adjacent_search_bound_to_current_direction_campus_seed() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    evidence = verifier.current_direction_adjacent_search_evidence(
        _current_direction_adjacent_search_response(snapshot),
        snapshot,
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []
    assert evidence["targetCount"] == 3
    assert all(binding["daySeedAmapId"] == binding["currentCampusAmapId"] for binding in evidence["bindings"])


def test_live_verifier_rejects_later_direction_adjacent_search_using_prior_campus_seed() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    prior_direction_seed = snapshot["days"][0]["segments"][0]["poi"]["amapId"]
    response = _current_direction_adjacent_search_response(
        snapshot,
        seed_overrides={"day2_daily_completion_1": prior_direction_seed},
    )

    evidence = verifier.current_direction_adjacent_search_evidence(response, snapshot)
    assert evidence["verified"] is False
    assert ("adjacent_search_day_seed_mismatch:day2_daily_completion_1:B000DAY2CAMP:B000DAY1CAMP") in evidence["errors"]


def test_live_verifier_rejects_claimed_adjacent_scope_mismatch_reason_before_provider() -> None:
    snapshot = _standard_two_day_daily_completion_snapshot()
    response = _current_direction_adjacent_search_response(
        snapshot,
        include_claimed_scope_mismatch=True,
    )

    evidence = verifier.current_direction_adjacent_search_evidence(response, snapshot)
    assert evidence["verified"] is False
    assert "claimed_adjacent_scope_mismatch_present:1" in evidence["errors"]
    assert evidence["mismatchReasonCodeCount"] == 1


def _controller_decision(
    *,
    performance: list[dict] | None = None,
    raw_decisions: list[dict] | None = None,
    decision_id: str = "decision_live_1",
) -> dict:
    return {
        "decisionId": decision_id,
        "source": "controller",
        "decisionPath": "full",
        "controllerSucceeded": True,
        "accepted": True,
        "controllerPerformance": performance
        if performance is not None
        else [
            {
                "callKind": "full",
                "captureState": "completed",
                "providerInvoked": True,
                "responseHeadersReceived": True,
                "httpStatus": 200,
                "responseBytes": 420,
            }
        ],
        "providerRawDecisions": raw_decisions
        if raw_decisions is not None
        else [
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "draft_itinerary",
            }
        ],
    }


def _hard_night_contract(*, count: int = 1) -> dict:
    return {
        "requiredIntents": [
            {
                "goalId": "goal_night_view",
                "intentType": "night_view",
                "requiredMin": count,
                "requirementLevel": "required",
            }
        ]
    }


def _night_segment(
    *,
    goal_id: str = "goal_night_view",
    occurrence_id: str = "night_occurrence_1",
    planning_slot_id: str = "night_slot_1",
) -> dict:
    return {
        "id": "seg_night_1",
        "kind": "visit",
        "startTime": "19:00",
        "endTime": "20:30",
        "poi": {
            "id": "poi_night_1",
            "amapId": "B000A1B2C3",
            "name": "真实城市夜景地点",
            "latitude": 39.91,
            "longitude": 116.40,
            "source": "amap-place-search",
            "groundingStatus": "provisional",
            "intentType": "night_view",
        },
        "semanticMetadata": {
            "intentType": "night_view",
            "goalId": goal_id,
            "sourceGoalId": goal_id,
            "occurrenceId": occurrence_id,
            "planningSlotId": planning_slot_id,
            "poolId": "night_pool",
            "requirementLevel": "required",
            "required": True,
            "groundingStatus": "provisional",
            "routeAnchor": True,
        },
    }


def _night_pending(
    *,
    goal_id: str = "goal_night_view",
    occurrence_id: str = "night_occurrence_1",
    planning_slot_id: str = "night_slot_1",
) -> dict:
    return {
        "id": f"pending:{planning_slot_id}",
        "planningSlotId": planning_slot_id,
        "poolId": "night_pool",
        "dayNumber": 1,
        "startTime": "19:00",
        "endTime": "20:30",
        "timeWindow": "19:00-20:30",
        "intentType": "night_view",
        "goalId": goal_id,
        "sourceGoalId": goal_id,
        "occurrenceId": occurrence_id,
        "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
        "requirementLevel": "required",
        "groundingStatus": "unresolved",
        "simpleDirectionProviderExhausted": True,
        "simpleDirectionRequirementLineageConflict": False,
        "futureRouteAnchor": True,
        "routeAnchorExpected": True,
    }


def _snapshot(
    *,
    segments: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> dict:
    return {
        "days": [
            {
                "id": "day_1",
                "dayNumber": 1,
                "segments": segments or [],
            }
        ],
        "portfolioPendingSlots": pending or [],
    }


def _scheduled_segment(
    *,
    segment_id: str,
    start_time: str,
    end_time: str,
) -> dict:
    duration_minutes = verifier.minutes_between(start_time, end_time)
    return {
        "id": segment_id,
        "kind": "visit",
        "startTime": start_time,
        "endTime": end_time,
        "durationMinutes": duration_minutes or 0,
    }


def test_adoption_ready_commit_readiness_rejects_live_empty_segment_times() -> None:
    snapshot = _snapshot(
        segments=[
            _scheduled_segment(
                segment_id="seg_live_empty",
                start_time="",
                end_time="",
            )
        ]
    )

    evidence = verifier.proposal_commit_readiness_evidence(
        snapshot,
        adoption_ready=True,
    )

    assert evidence == {
        "schemaVersion": "trip-proposal-commit-readiness-v1",
        "evaluated": True,
        "verified": False,
        "adoptionReady": True,
        "materializedSegmentCount": 1,
        "validSegmentCount": 0,
        "invalidSegmentCount": 1,
        "derivedDurationCount": 0,
        "failureCounts": {
            "duration_minutes_not_positive": 1,
            "start_time_format_invalid": 1,
            "end_time_format_invalid": 1,
        },
        "failures": [
            {
                "dayNumber": 1,
                "segmentIndex": 1,
                "codes": [
                    "duration_minutes_not_positive",
                    "end_time_format_invalid",
                    "start_time_format_invalid",
                ],
            }
        ],
    }
    assert snapshot["days"][0]["segments"][0]["startTime"] == ""
    assert snapshot["days"][0]["segments"][0]["endTime"] == ""


def test_adoption_ready_commit_readiness_requires_positive_nonoverlapping_intervals() -> None:
    snapshot = _snapshot(
        segments=[
            _scheduled_segment(
                segment_id="seg_first",
                start_time="09:00",
                end_time="10:00",
            ),
            _scheduled_segment(
                segment_id="seg_overlap",
                start_time="09:30",
                end_time="10:30",
            ),
            _scheduled_segment(
                segment_id="seg_zero",
                start_time="11:00",
                end_time="11:00",
            ),
        ]
    )

    evidence = verifier.proposal_commit_readiness_evidence(
        snapshot,
        adoption_ready=True,
    )

    assert evidence["verified"] is False
    assert evidence["materializedSegmentCount"] == 3
    assert evidence["validSegmentCount"] == 1
    assert evidence["invalidSegmentCount"] == 2
    assert evidence["failureCounts"] == {
        "duration_minutes_not_positive": 1,
        "end_time_not_after_start": 1,
        "same_day_overlap": 1,
    }
    assert evidence["failures"] == [
        {
            "dayNumber": 1,
            "segmentIndex": 2,
            "codes": ["same_day_overlap"],
            "conflictsWithSegmentIndex": 1,
        },
        {
            "dayNumber": 1,
            "segmentIndex": 3,
            "codes": [
                "duration_minutes_not_positive",
                "end_time_not_after_start",
            ],
        },
    ]


def test_adoption_ready_commit_readiness_rejects_duration_drift() -> None:
    segment = _scheduled_segment(
        segment_id="seg_duration_drift",
        start_time="09:00",
        end_time="10:30",
    )
    segment["durationMinutes"] = 1

    evidence = verifier.proposal_commit_readiness_evidence(
        _snapshot(segments=[segment]),
        adoption_ready=True,
    )

    assert evidence["verified"] is False
    assert evidence["validSegmentCount"] == 0
    assert evidence["invalidSegmentCount"] == 1
    assert evidence["failureCounts"] == {"duration_minutes_mismatch": 1}
    assert evidence["failures"] == [
        {
            "dayNumber": 1,
            "segmentIndex": 1,
            "codes": ["duration_minutes_mismatch"],
        }
    ]


def test_adoption_ready_commit_readiness_derives_missing_duration_from_strict_interval() -> None:
    segment = _scheduled_segment(
        segment_id="seg_duration_derived",
        start_time="09:00",
        end_time="10:30",
    )
    segment.pop("durationMinutes")

    evidence = verifier.proposal_commit_readiness_evidence(
        _snapshot(segments=[segment]),
        adoption_ready=True,
    )

    assert evidence["verified"] is True
    assert evidence["validSegmentCount"] == 1
    assert evidence["invalidSegmentCount"] == 0
    assert evidence["derivedDurationCount"] == 1
    assert evidence["failureCounts"] == {}


def test_non_adoption_ready_projection_is_not_subject_to_commit_readiness() -> None:
    evidence = verifier.proposal_commit_readiness_evidence(
        _snapshot(
            segments=[
                _scheduled_segment(
                    segment_id="seg_pending",
                    start_time="",
                    end_time="",
                )
            ]
        ),
        adoption_ready=False,
    )

    assert evidence["evaluated"] is False
    assert evidence["verified"] is True
    assert evidence["materializedSegmentCount"] == 0
    assert evidence["derivedDurationCount"] == 0
    assert evidence["failureCounts"] == {}
    assert evidence["failures"] == []


def test_adoption_ready_commit_readiness_accepts_ordered_materialized_segments() -> None:
    evidence = verifier.proposal_commit_readiness_evidence(
        _snapshot(
            segments=[
                _scheduled_segment(
                    segment_id="seg_morning",
                    start_time="09:00",
                    end_time="10:00",
                ),
                _scheduled_segment(
                    segment_id="seg_noon",
                    start_time="10:00",
                    end_time="11:15",
                ),
            ]
        ),
        adoption_ready=True,
    )

    assert evidence["evaluated"] is True
    assert evidence["verified"] is True
    assert evidence["materializedSegmentCount"] == 2
    assert evidence["validSegmentCount"] == 2
    assert evidence["invalidSegmentCount"] == 0
    assert evidence["failureCounts"] == {}
    assert evidence["failures"] == []


def test_real_deepseek_evidence_rejects_silent_local_stub_host() -> None:
    response = {"agentDecision": _controller_decision()}

    evidence = verifier.deepseek_decision_evidence(
        response,
        endpoint_host="127.0.0.1:18080",
    )

    assert evidence["verified"] is False
    assert evidence["reasonCode"] == "deepseek_endpoint_host_not_allowed"
    assert evidence["endpointHost"] == "127.0.0.1:18080"
    assert "http" not in str(evidence).casefold()


def test_real_deepseek_evidence_does_not_join_success_and_raw_decision_across_containers() -> None:
    response = {
        "successOnly": _controller_decision(raw_decisions=[]),
        "rawOnly": {
            "decisionId": "decision_other",
            "controllerPerformance": [],
            "providerRawDecisions": [
                {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "draft_itinerary",
                }
            ],
        },
    }

    evidence = verifier.deepseek_decision_evidence(
        response,
        endpoint_host="api.deepseek.com",
    )

    assert evidence["verified"] is False
    assert evidence["reasonCode"] == "deepseek_attempt_decision_binding_missing"


def test_real_deepseek_evidence_binds_raw_decision_to_completed_attempt_order() -> None:
    decision = _controller_decision(
        performance=[
            {
                "callKind": "full",
                "captureState": "provider_failed",
                "providerInvoked": True,
                "responseHeadersReceived": False,
                "httpStatus": None,
            },
            {
                "callKind": "repair",
                "captureState": "completed",
                "providerInvoked": True,
                "responseHeadersReceived": True,
                "httpStatus": 200,
                "responseBytes": 512,
            },
        ]
    )

    evidence = verifier.deepseek_decision_evidence(
        {"agentDecision": decision},
        endpoint_host="API.DeepSeek.com.",
    )

    assert evidence["verified"] is True
    assert evidence["endpointHost"] == "api.deepseek.com"
    assert evidence["attempts"] == [
        {
            "decisionId": "decision_live_1",
            "performanceAttemptIndex": 1,
            "rawDecisionIndex": 0,
            "callKind": "repair",
            "captureState": "completed",
            "httpStatus": 200,
            "rawDecisionSchemaVersion": "agent-decision-v3",
            "rawPrimaryAction": "draft_itinerary",
            "endpointHost": "api.deepseek.com",
        }
    ]


def test_real_deepseek_evidence_rejects_http_success_when_final_decision_fell_back() -> None:
    decision = _controller_decision()
    decision.update(
        {
            "source": "safe_fallback",
            "decisionPath": "fallback",
            "controllerSucceeded": False,
            "accepted": True,
        }
    )

    evidence = verifier.deepseek_decision_evidence(
        {"agentDecision": decision},
        endpoint_host="api.deepseek.com",
    )

    assert evidence["verified"] is False
    assert evidence["reasonCode"] == "deepseek_accepted_controller_decision_missing"


def test_clarification_preflight_allows_controller_and_manual_normalization_calls() -> None:
    response = {
        "planningSteps": [
            {
                "type": "controller_full_completed",
                "providerName": "agent-autonomy-controller",
            },
            {
                "type": "clarification_manual_normalization_completed",
                "providerName": "DeepSeekAgentProvider",
            },
        ],
        "clarificationManualNormalization": {"transport": {"providerInvoked": True, "httpStatus": 200}},
    }

    assert verifier.planning_external_evidence(response) == []


def test_clarification_preflight_rejects_poi_or_route_work() -> None:
    response = {
        "planningSteps": [
            {
                "type": "simple_open_tool_call",
                "providerName": "amap-place-search",
            },
            {
                "type": "proposal_route_leg_started",
                "providerName": "route_service",
            },
        ]
    }

    assert verifier.planning_external_evidence(response) == [
        "proposal_route_leg_started:route_service",
        "simple_open_tool_call:amap-place-search",
    ]


def _refingerprint(checkpoint: dict) -> dict:
    checkpoint.pop("fingerprint", None)
    checkpoint["fingerprint"] = verifier.canonical_json_fingerprint(checkpoint)
    return checkpoint


def _detour_option_identity_fixture() -> dict:
    dimension = "route_decision.detour_tolerance"
    source_turn_id = "turn_assistant_detour"
    request_contract = {
        "schemaVersion": "request-intent-contract-v1",
        "clarificationDimensions": [
            {
                "dimensionId": dimension,
                "allowedSemanticFields": ["detourTolerance"],
            }
        ],
    }
    strict_semantic = {
        "detourTolerance": {
            "maxGeneralizedCostDelta": 20,
            "maxDetourRatio": 0.2,
        }
    }
    source_checkpoint = _refingerprint(
        {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "clarify_detour",
            "planningRootId": "turn_root",
            "sourceAssistantTurnId": source_turn_id,
            "requestFingerprint": verifier.canonical_json_fingerprint(request_contract),
            "submissionMode": "batch_atomic",
            "status": "awaiting_answer",
            "questions": [
                {
                    "dimensionId": dimension,
                    "question": "您对绕行和额外行程时间的接受程度如何？",
                    "allowFreeText": False,
                    "options": [
                        {
                            "id": "strict",
                            "label": "严格",
                            "semanticValue": copy.deepcopy(strict_semantic),
                        },
                        {
                            "id": "moderate",
                            "label": "适中",
                            "semanticValue": {
                                "detourTolerance": {
                                    "maxGeneralizedCostDelta": 35,
                                    "maxDetourRatio": 0.35,
                                }
                            },
                        },
                    ],
                }
            ],
        }
    )
    artifact_selection = {
        "checkpointId": source_checkpoint["checkpointId"],
        "checkpointFingerprint": source_checkpoint["fingerprint"],
        "requestFingerprint": source_checkpoint["requestFingerprint"],
        "planningRootId": source_checkpoint["planningRootId"],
        "sourceAssistantTurnId": source_turn_id,
        "dimensionId": dimension,
        "optionId": "strict",
        "semanticValue": copy.deepcopy(strict_semantic),
        "submissionMode": "persisted_option",
    }
    submitted_selection = {
        "dimensionId": dimension,
        "optionId": "strict",
        "manualValue": None,
    }
    selected_agent_choice = {
        "sourceAssistantTurnId": source_turn_id,
        "option": {
            "id": "clarification-batch:clarify_detour",
            "index": 0,
            "label": "确认并开始规划",
            "kind": "clarification_batch_submit",
            "action": "submit_clarification_batch",
            "scopeKind": "clarification",
            "checkpointId": source_checkpoint["checkpointId"],
            "checkpointFingerprint": source_checkpoint["fingerprint"],
            "sourceUserTurnId": "turn_root",
            "planningSelectionRootTurnId": source_checkpoint["planningRootId"],
            "allowsManualInput": False,
            "sourceAssistantTurnId": source_turn_id,
        },
        "batchSelections": [copy.deepcopy(submitted_selection)],
    }
    resolved_checkpoint = copy.deepcopy(source_checkpoint)
    resolved_checkpoint.update(
        {
            "status": "answered",
            "resolvedAnswers": [
                {
                    "dimensionId": dimension,
                    "optionId": "strict",
                    "source": "structured_option",
                    "sourceUserTurnId": "turn_user_detour",
                    "manualValue": None,
                    "semanticValue": copy.deepcopy(strict_semantic),
                    "label": "严格",
                }
            ],
        }
    )
    _refingerprint(resolved_checkpoint)
    return {
        "source_checkpoint": source_checkpoint,
        "source_turn_id": source_turn_id,
        "artifact_selection": artifact_selection,
        "selected_agent_choice": selected_agent_choice,
        "submitted_selection": submitted_selection,
        "resolved_checkpoint": resolved_checkpoint,
        "request_contract": request_contract,
    }


def test_detour_option_identity_accepts_one_source_signed_structured_option() -> None:
    evidence = verifier.detour_option_identity_evidence(**_detour_option_identity_fixture())

    assert evidence["verified"] is True
    assert evidence["errors"] == []
    assert evidence["optionId"] == "strict"
    assert evidence["submissionMode"] == "persisted_option"


def test_detour_option_identity_rejects_option_missing_from_source_checkpoint() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["artifact_selection"]["optionId"] = "not-source-signed"
    fixture["submitted_selection"]["optionId"] = "not-source-signed"
    fixture["resolved_checkpoint"]["resolvedAnswers"][0]["optionId"] = "not-source-signed"
    _refingerprint(fixture["resolved_checkpoint"])

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "artifact_option_not_uniquely_source_signed" in evidence["errors"]


def test_detour_option_identity_rejects_artifact_semantic_drift() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["artifact_selection"]["semanticValue"]["detourTolerance"]["maxDetourRatio"] = 0.21

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "artifact_semantic_value_mismatch" in evidence["errors"]


def test_detour_option_identity_rejects_resolved_answer_semantic_drift() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["resolved_checkpoint"]["resolvedAnswers"][0]["semanticValue"]["detourTolerance"][
        "maxGeneralizedCostDelta"
    ] = 21
    _refingerprint(fixture["resolved_checkpoint"])

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "resolved_answer_semantic_value_mismatch" in evidence["errors"]


def test_detour_option_identity_rejects_invalid_or_duplicate_source_options() -> None:
    fixture = _detour_option_identity_fixture()
    options = fixture["source_checkpoint"]["questions"][0]["options"]
    options[0]["semanticValue"]["unexpected"] = True
    options[1]["id"] = "strict"
    _refingerprint(fixture["source_checkpoint"])
    fixture["artifact_selection"]["checkpointFingerprint"] = fixture["source_checkpoint"]["fingerprint"]
    fixture["selected_agent_choice"]["option"]["checkpointFingerprint"] = fixture["source_checkpoint"]["fingerprint"]

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "source_option_identity_duplicate" in evidence["errors"]
    assert "source_option_strict_semantic_fields_invalid" in evidence["errors"]


def test_detour_option_identity_rejects_manual_or_semantic_browser_submission() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["submitted_selection"]["manualValue"] = "手填绕行偏好"
    fixture["submitted_selection"]["semanticValue"] = copy.deepcopy(fixture["artifact_selection"]["semanticValue"])

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "browser_submission_identity_invalid" in evidence["errors"]


def test_detour_option_identity_rejects_unknown_artifact_fields() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["artifact_selection"]["unexpected"] = True

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "artifact_fields_invalid" in evidence["errors"]


def test_detour_option_identity_rejects_unknown_capability_fields() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["selected_agent_choice"]["option"]["unexpected"] = True

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "submitted_capability_fields_invalid" in evidence["errors"]


def test_detour_option_identity_rejects_unknown_resolved_answer_fields() -> None:
    fixture = _detour_option_identity_fixture()
    fixture["resolved_checkpoint"]["resolvedAnswers"][0]["unexpected"] = True
    _refingerprint(fixture["resolved_checkpoint"])

    evidence = verifier.detour_option_identity_evidence(**fixture)

    assert evidence["verified"] is False
    assert "resolved_answer_fields_invalid" in evidence["errors"]


def test_detour_option_identity_sqlite_producer_to_consumer_replay() -> None:
    fixture = _detour_option_identity_fixture()
    request_turn_id = "turn_user_detour"
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE conversation_turns ("
        "id TEXT PRIMARY KEY, role TEXT NOT NULL, "
        "agent_request_json TEXT, agent_response_json TEXT)"
    )
    connection.executemany(
        "INSERT INTO conversation_turns (id, role, agent_request_json, agent_response_json) VALUES (?, ?, ?, ?)",
        [
            (
                fixture["source_turn_id"],
                "assistant",
                None,
                json.dumps(
                    {"clarificationCheckpoint": fixture["source_checkpoint"]},
                    ensure_ascii=False,
                ),
            ),
            (
                request_turn_id,
                "user",
                json.dumps(
                    {
                        "selectedAgentChoice": fixture["selected_agent_choice"],
                        "clarificationCheckpoint": fixture["resolved_checkpoint"],
                        "requestIntentContract": fixture["request_contract"],
                    },
                    ensure_ascii=False,
                ),
                None,
            ),
        ],
    )

    evidence = verifier.detour_option_identity_from_sqlite(
        connection=connection,
        choice_execution={
            "source_turn_id": fixture["source_turn_id"],
            "request_turn_id": request_turn_id,
        },
        journey_batch={"selections": [fixture["artifact_selection"]]},
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []
    assert evidence["optionId"] == "strict"


def test_real_amap_text_evidence_requires_success_non_cache_and_materialized_identity() -> None:
    snapshot = _snapshot(segments=[_night_segment()])
    response = {
        "planningSteps": [
            {
                "type": "simple_open_tool_call",
                "providerName": "amap-place-search",
                "status": "completed",
                "metadata": {
                    "providerOutcome": "success",
                    "cacheHit": False,
                    "resultCount": 3,
                    "queryFingerprint": "a" * 16,
                    "selectedAmapId": "B000A1B2C3",
                    "slotKey": "night_slot_1",
                },
            }
        ]
    }

    evidence = verifier.real_amap_text_evidence(response, snapshot)

    assert evidence["verified"] is True
    assert evidence["materializedAmapIds"] == ["B000A1B2C3"]
    assert evidence["boundAmapIds"] == ["B000A1B2C3"]


def test_real_amap_text_evidence_rejects_legacy_completed_exception_shape() -> None:
    snapshot = _snapshot(segments=[_night_segment()])
    response = {
        "planningSteps": [
            {
                "type": "simple_open_tool_call",
                "providerName": "amap-place-search",
                "status": "completed",
                "metadata": {
                    "cacheHit": None,
                    "queryFingerprint": "a" * 16,
                },
            }
        ]
    }

    evidence = verifier.real_amap_text_evidence(response, snapshot)

    assert evidence["verified"] is False
    assert "amap_materialized_identity_without_live_search:B000A1B2C3" in evidence["errors"]


def test_real_amap_text_evidence_rejects_success_for_identity_not_in_proposal() -> None:
    snapshot = _snapshot(segments=[_night_segment()])
    response = {
        "planningSteps": [
            {
                "type": "simple_open_tool_call",
                "providerName": "amap-place-search",
                "status": "completed",
                "metadata": {
                    "providerOutcome": "success",
                    "cacheHit": False,
                    "resultCount": 1,
                    "queryFingerprint": "b" * 16,
                    "selectedAmapId": "B000ZZZZZZ",
                    "slotKey": "other_slot",
                },
            }
        ]
    }

    evidence = verifier.real_amap_text_evidence(response, snapshot)

    assert evidence["verified"] is False
    assert "amap_search_identity_not_materialized:B000ZZZZZZ" in evidence["errors"]
    assert "amap_materialized_identity_without_live_search:B000A1B2C3" in evidence["errors"]


def test_hard_night_occurrence_accepts_exactly_one_materialized_branch() -> None:
    evidence = verifier.hard_night_occurrence_evidence(
        _snapshot(segments=[_night_segment()]),
        _hard_night_contract(),
    )

    assert evidence["errors"] == []
    assert evidence["branch"] == "materialized"
    assert evidence["coveredOccurrenceCount"] == 1


def test_hard_night_occurrence_accepts_exactly_one_typed_provider_exhausted_branch() -> None:
    evidence = verifier.hard_night_occurrence_evidence(
        _snapshot(pending=[_night_pending()]),
        _hard_night_contract(),
    )

    assert evidence["errors"] == []
    assert evidence["branch"] == "provider_exhausted_pending"
    assert evidence["coveredOccurrenceCount"] == 1


def test_hard_night_occurrence_rejects_missing_or_dual_representation() -> None:
    missing = verifier.hard_night_occurrence_evidence(
        _snapshot(),
        _hard_night_contract(),
    )
    dual = verifier.hard_night_occurrence_evidence(
        _snapshot(segments=[_night_segment()], pending=[_night_pending()]),
        _hard_night_contract(),
    )

    assert "hard_night_occurrence_missing:goal_night_view:1" in missing["errors"]
    assert "hard_night_occurrence_dual_representation:goal_night_view:1" in dual["errors"]


def test_hard_night_occurrence_rejects_pending_from_another_goal() -> None:
    evidence = verifier.hard_night_occurrence_evidence(
        _snapshot(pending=[_night_pending(goal_id="goal_other_night")]),
        _hard_night_contract(),
    )

    assert "hard_night_occurrence_missing:goal_night_view:1" in evidence["errors"]
    assert "hard_night_unexpected_goal_lineage:goal_other_night" in evidence["errors"]


def test_adoption_execution_binding_requires_exact_opaque_choice_and_source_turn() -> None:
    proposal = {
        "id": "proposal_a",
        "choice_id": "choice_a",
    }
    source_response = {
        "choiceOptions": [
            {
                "choiceId": "choice_a",
                "action": "select_plan_proposal",
                "proposalId": "proposal_a",
            }
        ]
    }
    turns = {
        "assistant_source": {
            "id": "assistant_source",
            "role": "assistant",
            "response": source_response,
        },
        "user_request": {
            "id": "user_request",
            "role": "user",
            "response": {},
        },
        "assistant_execution": {
            "id": "assistant_execution",
            "role": "assistant",
            "response": {},
        },
    }
    execution = {
        "id": "execution_a",
        "session_id": "session_a",
        "source_turn_id": "assistant_source",
        "choice_id": "choice_a",
        "request_turn_id": "user_request",
        "execution_turn_id": "assistant_execution",
        "result_version_id": "version_a",
    }

    assert (
        verifier.adoption_execution_binding_errors(
            execution,
            proposal,
            turns,
            session_id="session_a",
            expected_version_id="version_a",
        )
        == []
    )

    tampered = {**execution, "choice_id": "choice_b"}
    errors = verifier.adoption_execution_binding_errors(
        tampered,
        proposal,
        turns,
        session_id="session_a",
        expected_version_id="version_a",
    )
    assert "adoption_choice_not_proposal_choice:execution_a" in errors
    assert "adoption_source_choice_missing:execution_a" in errors


def test_simple_direction_launcher_forbids_seeded_live_gate() -> None:
    source = (REPO_ROOT / "scripts" / "run-live-portfolio-e2e.ps1").read_text(encoding="utf-8")

    assert "Simple Direction live E2E forbids SeedDatabasePath and SeedBundlePath" in source
    assert '$frontendUrl = "http://localhost:$FrontendPort"' in source
    assert '$apiBaseUrl = "http://localhost:$BackendPort/api"' in source
    assert '$serviceBindHost = "127.0.0.1"' in source
    assert "$env:TRIP_E2E_VITE_HOST = $serviceBindHost" in source
    assert '$backendReadinessUrl = "http://$serviceBindHost`:$BackendPort/api/providers/status"' in source
    assert '$frontendReadinessUrl = "http://$serviceBindHost`:$FrontendPort"' in source
    assert "Wait-HttpReady $backendReadinessUrl 120 $backendProcess" in source
    assert "Wait-HttpReady $frontendReadinessUrl 120 $frontendProcess" in source
    assert '$env:DEEPSEEK_BASE_URL = "https://$normalizedDeepSeekEndpointHost"' in source
    assert '"--deepseek-endpoint-host"' in source
    assert "deepSeekEndpointHost = $normalizedDeepSeekEndpointHost" in source
    assert '"simple_direction_frontier"' in source
    assert "e2e/simple-direction-frontier-user-journey.spec.ts" in source
    assert "scripts\\verify_live_simple_direction_frontier_e2e.py" in source
    assert "$isSimpleDirectionJourney = $JourneyMode -in $simpleDirectionJourneyModes" in source


def test_live_launcher_freezes_only_a_clean_tracked_source_tree_before_run_setup() -> None:
    source = (REPO_ROOT / "scripts" / "run-live-portfolio-e2e.ps1").read_text(encoding="utf-8")

    freeze_index = source.index("$frozenGitCommit = (& git -C $repoRoot rev-parse HEAD)")
    preflight_index = source.index("$preflightSourceAttribution = Test-LiveE2ESourceAttribution")
    run_root_index = source.index('$runRoot = Join-Path $repoRoot ".ai-runs\\live-e2e\\$RunId"')
    post_journey_index = source.index("$postJourneySourceAttribution = Test-LiveE2ESourceAttribution")
    verifier_index = source.index("& $python @verifierArguments")
    final_index = source.index("$finalSourceAttribution = Test-LiveE2ESourceAttribution")
    summary_index = source.index("$summary = [ordered]@{")

    assert '. (Join-Path $PSScriptRoot "live-e2e-source-attribution.ps1")' in source
    assert freeze_index < preflight_index < run_root_index
    assert post_journey_index < verifier_index
    assert verifier_index < final_index < summary_index
    assert "tracked worktree or index differs from frozen HEAD" in source
    assert "create an exact-path local evidence commit before consuming the one-shot Run ID" in source
    assert "Get-LiveE2ERequiredSourcePaths" in source
    assert source.count("-RequiredSourcePaths $requiredSourcePaths") == 3
    assert "requiredSourcePaths = $requiredSourcePaths" in source
    assert "requiredSourceResults = $preflightSourceAttribution.requiredSourceResults" in source
    assert "sourceAttributionVerified = $sourceAttributionVerified" in source
    assert "-or -not $sourceAttributionVerified" in source


def test_live_source_attribution_helper_supports_windows_powershell_51(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the compatibility regression")

    repo = tmp_path / "windows-powershell-source-repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "trip-tests@example.invalid")
    _run_git(repo, "config", "user.name", "Trip Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-qm", "initial")
    expected_commit = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="windows-powershell-51",
        required_source_paths=("tracked.txt",),
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = _probe_payload(result)
    assert payload["verified"] is True
    assert payload["requiredSourceResults"] == [
        {
            "path": "tracked.txt",
            "existsOnDisk": True,
            "tracked": True,
            "presentInExpectedCommit": True,
            "verified": True,
            "failureReason": None,
        }
    ]


def test_live_source_attribution_helper_rejects_tracked_drift_but_allows_untracked(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required to exercise the source attribution helper")
    repo = tmp_path / "source-repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "trip-tests@example.invalid")
    _run_git(repo, "config", "user.name", "Trip Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-qm", "initial")
    expected_commit = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

    clean = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="clean",
    )
    assert clean.returncode == 0
    assert _probe_payload(clean)["verified"] is True

    (repo / "user-note.txt").write_text("untracked user file\n", encoding="utf-8")
    untracked = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="untracked",
    )
    assert untracked.returncode == 0
    assert _probe_payload(untracked)["verified"] is True

    tracked.write_text("drifted\n", encoding="utf-8")
    dirty = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="dirty",
    )
    assert dirty.returncode == 1
    assert _probe_payload(dirty)["failureReason"] == "tracked_source_dirty"

    tracked.write_text("frozen\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "--allow-empty", "-qm", "new head")
    moved_head = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="moved-head",
    )
    assert moved_head.returncode == 1
    assert _probe_payload(moved_head)["failureReason"] == "head_commit_mismatch"


def test_live_source_attribution_helper_requires_tracked_sources_in_frozen_commit(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required to exercise the source attribution helper")
    repo = tmp_path / "required-source-repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "trip-tests@example.invalid")
    _run_git(repo, "config", "user.name", "Trip Tests")
    tracked = repo / "tracked.ts"
    tracked.write_text("export const tracked = true;\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.ts")
    _run_git(repo, "commit", "-qm", "initial")
    expected_commit = _run_git(repo, "rev-parse", "HEAD").stdout.strip()

    tracked_ok = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="tracked-required",
        required_source_paths=("tracked.ts",),
    )
    assert tracked_ok.returncode == 0
    tracked_payload = _probe_payload(tracked_ok)
    assert tracked_payload["verified"] is True
    assert tracked_payload["requiredSourcePaths"] == ["tracked.ts"]
    assert tracked_payload["requiredSourceResults"][0]["verified"] is True

    (repo / "untracked.ts").write_text("export const untracked = true;\n", encoding="utf-8")
    untracked = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="untracked-required",
        required_source_paths=("untracked.ts",),
    )
    assert untracked.returncode == 1
    untracked_payload = _probe_payload(untracked)
    assert untracked_payload["failureReason"] == "required_source_untracked"
    assert untracked_payload["requiredSourceResults"][0]["verified"] is False

    missing = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="missing-required",
        required_source_paths=("missing.ts",),
    )
    assert missing.returncode == 1
    missing_payload = _probe_payload(missing)
    assert missing_payload["failureReason"] == "required_source_missing"
    assert missing_payload["requiredSourceResults"][0]["verified"] is False

    staged_only = repo / "staged-only.ts"
    staged_only.write_text("export const staged = true;\n", encoding="utf-8")
    _run_git(repo, "add", "staged-only.ts")
    missing_from_commit = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="required-not-in-frozen-commit",
        required_source_paths=("staged-only.ts",),
    )
    assert missing_from_commit.returncode == 1
    missing_from_commit_payload = _probe_payload(missing_from_commit)
    assert missing_from_commit_payload["failureReason"] == "required_source_missing_from_frozen_commit"
    assert missing_from_commit_payload["requiredSourceResults"][0]["presentInExpectedCommit"] is False

    _run_git(repo, "reset", "-q", "HEAD", "--", "staged-only.ts")
    staged_only.unlink()
    sibling = tmp_path / "required-source-repo-outside.ts"
    sibling.write_text("export const outside = true;\n", encoding="utf-8")
    outside_repo = _run_source_attribution_probe(
        powershell=powershell,
        tmp_path=tmp_path,
        repo=repo,
        expected_commit=expected_commit,
        probe_name="required-outside-repo",
        required_source_paths=(str(sibling),),
    )
    assert outside_repo.returncode == 1
    assert "Required source path must stay within the repository" in (outside_repo.stdout + outside_repo.stderr)


def test_simple_direction_launcher_rejects_non_official_deepseek_host_before_run_setup() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required to exercise the Windows live gate launcher")
    run_id = f"launcher-contract-{uuid.uuid4().hex}"
    run_root = REPO_ROOT / ".ai-runs" / "live-e2e" / run_id

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts" / "run-live-portfolio-e2e.ps1"),
            "-RunId",
            run_id,
            "-JourneyMode",
            "simple_direction",
            "-DeepSeekEndpointHost",
            "127.0.0.1:18080",
            "-PlaywrightSpec",
            "e2e/contract-test-missing.spec.ts",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    normalized_output = " ".join(combined_output.split())
    assert result.returncode != 0
    assert (
        "Simple Direction live E2E requires the official DeepSeek endpoint host api.deepseek.com"
    ) in normalized_output
    assert "Playwright spec is missing" not in normalized_output
    assert not run_root.exists()


def test_verify_database_rejects_local_deepseek_host_before_opening_database(tmp_path) -> None:
    database_path = tmp_path / "must-not-be-opened.db"

    report = verifier.verify_database(
        database_path,
        {"acceptedTelemetry": _controller_decision()},
        deepseek_endpoint_host="127.0.0.1:18080",
    )

    assert report["passed"] is False
    assert report["failures"] == ["deepseek_endpoint_host_not_allowed"]
    assert report["deepSeekEndpointHost"] == "127.0.0.1:18080"
    assert report["allowedDeepSeekEndpointHost"] == "api.deepseek.com"
    assert not database_path.exists()


def test_verifier_cli_rejects_local_host_before_reading_journey_or_database(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "must-not-be-opened.db"
    journey_path = tmp_path / "must-not-be-read.json"
    output_path = tmp_path / "verification.json"
    journey_path.write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER_PATH),
            "--database",
            str(database_path),
            "--journey-result",
            str(journey_path),
            "--output",
            str(output_path),
            "--deepseek-endpoint-host",
            "127.0.0.1:18080",
        ],
    )

    assert verifier.main() == 1

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["failures"] == ["deepseek_endpoint_host_not_allowed"]
    assert report["deepSeekEndpointHost"] == "127.0.0.1:18080"
    assert report["allowedDeepSeekEndpointHost"] == "api.deepseek.com"
    assert not database_path.exists()

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services.daily_route_overlap_service import DailyRouteOverlapService


def _leg(
    identity: str,
    polyline: list[list[float]] | None,
    *,
    mode: str = "walking",
    exception: dict | str | None = None,
) -> dict:
    value = {
        "routeOptionId": identity,
        "fromAmapId": f"{identity}-FROM",
        "toAmapId": f"{identity}-TO",
        "provider": "amap-webservice",
        "source": "amap-webservice",
        "mode": mode,
        "distanceMeters": 120,
        "durationSeconds": 180,
        "queriedAt": datetime.now(timezone.utc).isoformat(),
        "polyline": polyline or [],
        "steps": [],
    }
    if exception is not None:
        value["overlapException"] = exception
    return value


def _evaluate(*legs: dict) -> dict:
    return DailyRouteOverlapService().evaluate(day_number=1, route_legs=list(legs))


def test_no_overlap_is_zero_and_geometry_is_replayable() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
        _leg("b", [[116.002, 39.0], [116.003, 39.0]]),
    )

    assert evidence["status"] == "overlap_evaluated"
    assert evidence["repeatedMeters"] == 0
    assert evidence["nonExemptRepeatedMeters"] == 0
    assert evidence["overlapRatio"] == 0
    assert evidence["geometryFingerprint"]
    assert evidence["evidenceFingerprint"]
    assert len(evidence["selectedRouteGeometry"]) == 2
    assert evidence["selectedRouteGeometry"][0]["geometryParts"] == [
        {
            "mode": "walking",
            "polyline": [[116.0, 39.0], [116.001, 39.0]],
            "exceptionProvenance": None,
        }
    ]
    assert evidence["selectedRouteGeometry"][0]["providerEvidenceFingerprint"]


def test_same_direction_overlap_is_counted() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
        _leg("b", [[116.0, 39.0], [116.001, 39.0]]),
    )

    assert evidence["sameDirectionRepeatedMeters"] > 70
    assert evidence["reverseDirectionRepeatedMeters"] == 0
    assert evidence["repeatedMeters"] == pytest.approx(evidence["sameDirectionRepeatedMeters"], abs=0.02)


def test_reverse_direction_overlap_is_counted_separately() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
        _leg("b", [[116.001, 39.0], [116.0, 39.0]]),
    )

    assert evidence["reverseDirectionRepeatedMeters"] > 70
    assert evidence["sameDirectionRepeatedMeters"] == 0
    assert evidence["repeatedMeters"] == pytest.approx(evidence["reverseDirectionRepeatedMeters"], abs=0.02)


def test_partial_overlap_counts_only_the_shared_projection() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.002, 39.0]]),
        _leg("b", [[116.001, 39.0], [116.003, 39.0]]),
    )

    assert 70 < evidence["repeatedMeters"] < 120
    assert 0 < evidence["overlapRatio"] < 0.5


def test_intersection_only_is_not_route_overlap() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.002, 39.0]]),
        _leg("b", [[116.001, 38.999], [116.001, 39.001]]),
    )

    assert evidence["repeatedMeters"] == 0


def test_nearby_parallel_road_is_not_merged() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.002, 39.0]]),
        _leg("b", [[116.0, 39.0001], [116.002, 39.0001]]),
    )

    assert evidence["repeatedMeters"] == 0


def test_small_provider_coordinate_jitter_still_matches() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.002, 39.0]]),
        _leg("b", [[116.0, 39.00001], [116.002, 39.00001]]),
    )

    assert evidence["repeatedMeters"] > 140


def test_day_uses_one_projection_even_when_legs_have_different_remote_branches() -> None:
    evidence = _evaluate(
        _leg("north-branch", [[116.0, 39.0], [116.001, 39.0], [116.001, 39.1]]),
        _leg("south-branch", [[116.0, 39.0], [116.001, 39.0], [116.001, 38.9]]),
    )

    assert evidence["sameDirectionRepeatedMeters"] > 70


def test_missing_geometry_fails_closed() -> None:
    evidence = _evaluate(
        _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
        _leg("missing", None),
    )

    assert evidence["status"] == "route_geometry_pending"
    assert evidence["geometryComplete"] is False
    assert evidence["geometryFingerprint"] == ""
    assert evidence["failureReason"] == "verified_route_geometry_missing"
    assert evidence["missingGeometryRouteOptionIds"] == ["missing"]


def test_partially_missing_provider_steps_fail_closed_instead_of_using_top_level_fallback() -> None:
    leg = _leg("partial", [[116.0, 39.0], [116.002, 39.0]])
    leg["steps"] = [
        {"mode": "walking", "polyline": "116.0,39.0;116.001,39.0"},
        {"mode": "walking", "polyline": ""},
    ]

    evidence = _evaluate(leg)

    assert evidence["status"] == "route_geometry_pending"
    assert evidence["geometryComplete"] is False
    assert evidence["failureReason"] == "verified_route_geometry_missing"
    assert evidence["missingGeometryRouteOptionIds"] == ["partial"]


def test_oversized_geometry_is_not_truncated_or_claimed_complete(monkeypatch) -> None:
    monkeypatch.setattr(DailyRouteOverlapService, "MAX_GEOMETRY_POINTS_PER_DAY", 3)

    evidence = _evaluate(_leg("large", [[116.0, 39.0], [116.001, 39.0], [116.002, 39.0], [116.003, 39.0]]))

    assert evidence["status"] == "route_geometry_pending"
    assert evidence["failureReason"] == "verified_route_geometry_too_large"
    assert evidence["oversizedGeometryRouteOptionIds"] == ["large"]
    assert evidence["selectedRouteGeometry"][0]["geometryStatus"] == "too_large"
    assert evidence["selectedRouteGeometry"][0]["geometryParts"] == []


def test_sealed_geometry_material_round_trips_to_identical_evidence() -> None:
    service = DailyRouteOverlapService()
    original = service.evaluate(
        day_number=1,
        route_legs=[
            _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
            _leg("b", [[116.001, 39.0], [116.0, 39.0]]),
        ],
        alternatives_evaluated=2,
        selected_alternative_ids=["a", "b"],
    )

    replay = service.evaluate(
        day_number=1,
        route_legs=original["selectedRouteGeometry"],
        alternatives_evaluated=original["alternativesEvaluated"],
        selected_alternative_ids=original["selectedAlternativeIds"],
    )

    assert replay == original
    assert DailyRouteOverlapService.verify_evidence_fingerprint(replay) is True


def test_provider_fingerprint_binds_complete_day_local_edge_identity_and_replays() -> None:
    service = DailyRouteOverlapService()
    base = _leg("same-physical-pair", [[116.0, 39.0], [116.001, 39.0]])
    day_one = {
        **base,
        "dayNumber": 1,
        "pairOrdinal": 1,
        "fromSegmentId": "slot_day1_from",
        "toSegmentId": "slot_day1_to",
    }
    day_two = {
        **base,
        "dayNumber": 2,
        "pairOrdinal": 1,
        "fromSegmentId": "slot_day2_from",
        "toSegmentId": "slot_day2_to",
    }

    assert service.provider_evidence_fingerprint(day_one) != service.provider_evidence_fingerprint(day_two)
    assert service.provider_evidence_fingerprint({key: value for key, value in base.items()})
    assert service.provider_evidence_fingerprint({**base, "dayNumber": 1}) == ""

    evidence = service.evaluate(day_number=1, route_legs=[day_one])
    selected = evidence["selectedRouteGeometry"][0]
    assert {key: selected[key] for key in ("dayNumber", "pairOrdinal", "fromSegmentId", "toSegmentId")} == {
        "dayNumber": 1,
        "pairOrdinal": 1,
        "fromSegmentId": "slot_day1_from",
        "toSegmentId": "slot_day1_to",
    }
    assert selected["providerEvidenceFingerprint"] == service.provider_evidence_fingerprint(day_one)
    replay = service.evaluate(
        day_number=1,
        route_legs=evidence["selectedRouteGeometry"],
        alternatives_evaluated=evidence["alternativesEvaluated"],
        selected_alternative_ids=evidence["selectedAlternativeIds"],
    )
    assert replay == evidence


def test_only_bounded_structured_exception_provenance_can_exempt_overlap() -> None:
    ignored_text = _evaluate(
        _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
        _leg("b", [[116.0, 39.0], [116.001, 39.0]], exception="meal spur on same road"),
    )
    assert ignored_text["exemptRepeatedMeters"] == 0
    assert ignored_text["nonExemptRepeatedMeters"] > 70

    structured = _evaluate(
        _leg("a", [[116.0, 39.0], [116.001, 39.0]]),
        _leg(
            "b",
            [[116.0, 39.0], [116.001, 39.0]],
            exception={
                "schemaVersion": "route-overlap-exception-v1",
                "reasonCode": "bounded_meal_spur",
                "source": "server_route_policy",
                "maxExemptMeters": 200,
            },
        ),
    )
    assert structured["status"] == "passed_with_exempt_overlap"
    assert structured["exemptRepeatedMeters"] > 70
    assert structured["nonExemptRepeatedMeters"] == 0
    assert structured["exceptionApplications"][0]["reasonCode"] == "bounded_meal_spur"

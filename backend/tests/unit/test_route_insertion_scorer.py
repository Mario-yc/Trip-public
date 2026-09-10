from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.provider_route_insertion_service import (
    ProviderRouteInsertionService,
)
from src.services.route_insertion_scorer import RouteInsertionScorer


MOBILITY_PROFILE = {
    "source": "request_contract",
    "walkingPenaltyMinutesPerKm": 1.8,
    "transferPenaltyMinutes": 6.0,
    "waitTimeMultiplier": 1.0,
    "riskPenaltyMultiplier": 1.0,
}


def _route_contract(tolerance=None, mobility_profile=None):
    tolerance = tolerance or {
        "maxGeneralizedCostDelta": 40,
        "maxDetourRatio": 1.0,
    }
    mobility_profile = mobility_profile or MOBILITY_PROFILE
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"sourceAssistantTurnId": "turn_route_contract_test"},
        detour_tolerance=tolerance,
        mobility_profile=mobility_profile,
    )
    assert contract is not None
    return contract


def poi(latitude: float, longitude: float):
    return SimpleNamespace(latitude=latitude, longitude=longitude)


def test_geometry_is_only_a_non_decisive_precheck():
    scorer = RouteInsertionScorer()

    score = scorer.score(poi(30.0, 120.0), poi(30.0, 120.05), poi(30.0, 120.1), transport_mode="transit")

    assert score is not None
    assert score.detour_level == "low"
    assert score.added_distance_km <= 2.0
    assert score.reason == "geometry_precheck:between_previous_and_next"
    assert score.network_verified is False


def test_geometry_does_not_reject_a_far_candidate_before_provider_routes():
    scorer = RouteInsertionScorer()

    score = scorer.score(poi(30.0, 120.0), poi(30.4, 120.4), poi(30.0, 120.1), transport_mode="transit")

    assert score is not None
    assert score.detour_level == "high"
    assert score.added_distance_km > 7.0


def test_provider_route_matrix_decides_generalized_insertion_cost():
    scorer = RouteInsertionScorer()

    score = scorer.score_from_route_matrix(
        previous_to_candidate={
            "distanceMeters": 1800,
            "durationSeconds": 900,
            "walkingDistanceMeters": 250,
            "transferCount": 1,
            "waitSeconds": 120,
            "riskPenaltyMinutes": 0,
        },
        candidate_to_next={
            "distanceMeters": 1600,
            "durationSeconds": 840,
            "walkingDistanceMeters": 180,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
        },
        previous_to_next={
            "distanceMeters": 2100,
            "durationSeconds": 1020,
            "walkingDistanceMeters": 120,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
        },
        detour_tolerance={"maxGeneralizedCostDelta": 25, "maxDetourRatio": 1.5},
        schedule_slack_minutes=30,
        time_window_feasible=True,
        mobility_profile=MOBILITY_PROFILE,
    )

    assert score is not None
    assert score.network_verified is True
    assert score.reason == "provider_route_matrix"
    assert score.detour_level in {"medium", "high"}
    assert score.generalized_cost_delta is not None and score.generalized_cost_delta > 0


def test_two_sided_provider_matrix_fails_closed_without_direct_baseline():
    scorer = RouteInsertionScorer()

    score = scorer.score_from_route_matrix(
        previous_to_candidate={"distanceMeters": 800, "durationSeconds": 480},
        candidate_to_next={"distanceMeters": 900, "durationSeconds": 540},
        previous_to_next=None,
    )

    assert score is None


def _leg(duration: int, distance: int, *, walking: int = 0) -> dict:
    return {
        "distanceMeters": distance,
        "durationSeconds": duration,
        "walkingDistanceMeters": walking,
        "transferCount": 0,
        "waitSeconds": 0,
        "riskPenaltyMinutes": 0,
    }


def test_provider_matrix_fails_closed_on_missing_cost_or_time_window_fields():
    scorer = RouteInsertionScorer()
    incomplete = _leg(600, 1000)
    incomplete.pop("riskPenaltyMinutes")

    assert (
        scorer.score_from_route_matrix(
            previous_to_candidate=incomplete,
            candidate_to_next=_leg(600, 1000),
            previous_to_next=_leg(900, 1500),
            time_window_feasible=True,
            mobility_profile=MOBILITY_PROFILE,
        )
        is None
    )
    assert (
        scorer.score_from_route_matrix(
            previous_to_candidate=_leg(600, 1000),
            candidate_to_next=_leg(600, 1000),
            previous_to_next=_leg(900, 1500),
            time_window_feasible=None,
            mobility_profile=MOBILITY_PROFILE,
        )
        is None
    )


def test_dynamic_tolerance_and_mobility_profile_change_the_provider_decision():
    scorer = RouteInsertionScorer()
    kwargs = {
        "previous_to_candidate": _leg(600, 1500, walking=1200),
        "candidate_to_next": _leg(600, 1500, walking=1200),
        "previous_to_next": _leg(900, 2000, walking=0),
        "time_window_feasible": True,
    }
    strict = scorer.score_from_route_matrix(
        **kwargs,
        detour_tolerance={"maxGeneralizedCostDelta": 4, "maxDetourRatio": 0.2},
        mobility_profile={**MOBILITY_PROFILE, "walkingPenaltyMinutesPerKm": 4},
    )
    relaxed = scorer.score_from_route_matrix(
        **kwargs,
        detour_tolerance={"maxGeneralizedCostDelta": 30, "maxDetourRatio": 2},
        mobility_profile={**MOBILITY_PROFILE, "walkingPenaltyMinutesPerKm": 0.5},
    )

    assert strict is not None and strict.detour_level == "unacceptable"
    assert relaxed is not None and relaxed.detour_level != "unacceptable"
    assert strict.generalized_cost_delta > relaxed.generalized_cost_delta
    assert relaxed.detour_tolerance == {
        "maxGeneralizedCostDelta": 30.0,
        "maxDetourRatio": 2.0,
    }


def test_provider_matrix_refuses_missing_tolerance_or_partial_mobility_contract():
    scorer = RouteInsertionScorer()
    kwargs = {
        "previous_to_candidate": _leg(600, 1000),
        "candidate_to_next": _leg(600, 1000),
        "previous_to_next": _leg(900, 1500),
        "time_window_feasible": True,
    }

    assert scorer.score_from_route_matrix(**kwargs, mobility_profile=MOBILITY_PROFILE) is None
    assert (
        scorer.score_from_route_matrix(
            **kwargs,
            detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1},
            mobility_profile={"source": "partial"},
        )
        is None
    )


def test_provider_insertion_refuses_unsigned_or_policy_mismatched_contract_before_route_call():
    routes = _RecordedRouteService()
    service = ProviderRouteInsertionService(route_service=routes)
    tolerance = {"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0}
    result = service.evaluate(
        plan_id="missing_contract",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        following=_matrix_point("following", "B00000003", "13:00", "14:00"),
        candidate_duration_minutes=60,
        detour_tolerance=tolerance,
        mobility_profile=MOBILITY_PROFILE,
    )

    assert result.failure_reason == "route_decision_contract_missing_or_invalid"
    assert routes.calls == []


def _matrix_point(segment_id: str, amap_id: str, start: str, end: str) -> dict:
    return {
        "segmentId": segment_id,
        "dayId": "day_1",
        "amapId": amap_id,
        "name": segment_id,
        "city": "北京",
        "type": "route_anchor",
        "source": "amap-place-search",
        "longitude": 116.3,
        "latitude": 39.9,
        "startTime": start,
        "endTime": end,
    }


class _RecordedRouteService:
    def __init__(self, *, omit_baseline: bool = False, queried_at=None):
        self.omit_baseline = omit_baseline
        self.queried_at = queried_at
        self.calls: list[tuple[str, str]] = []

    def build_routes(self, _plan_id, _pois, *_args, segments=None, **_kwargs):
        left, right = segments
        self.calls.append((left.id, right.id))
        if self.omit_baseline and left.id == "previous" and right.id == "following":
            return []
        duration = {
            ("previous", "candidate"): 720,
            ("candidate", "following"): 780,
            ("previous", "following"): 960,
            ("previous", "current"): 1800,
            ("current", "following"): 1800,
        }[(left.id, right.id)]
        return [
            SimpleNamespace(
                from_segment_id=left.id,
                to_segment_id=right.id,
                provider="amap-webservice",
                mode="transit",
                is_selected=True,
                distance_meters=1600,
                duration_seconds=duration,
                provider_payload={},
                steps=[
                    {"mode": "walking", "distance": 180},
                    {"mode": "transit", "distance": 1200},
                    {"mode": "transit", "distance": 220},
                ],
                queried_at=self.queried_at or datetime.now(timezone.utc),
            )
        ]


def test_provider_route_insertion_service_requires_three_legs_and_schedule_window():
    routes = _RecordedRouteService()
    service = ProviderRouteInsertionService(route_service=routes)

    result = service.evaluate(
        plan_id="matrix",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        following=_matrix_point("following", "B00000003", "13:00", "14:00"),
        candidate_duration_minutes=60,
        detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0},
        mobility_profile=MOBILITY_PROFILE,
        route_decision_contract=_route_contract(),
    )

    assert result.passed is True
    assert set(result.legs) == {
        "previousToCandidate",
        "candidateToNext",
        "previousToNext",
    }
    assert routes.calls == [
        ("previous", "candidate"),
        ("candidate", "following"),
        ("previous", "following"),
    ]
    assert result.to_dict()["mobilityProfile"]["walkingDistanceMeters"] == 540
    assert result.legs["previousToCandidate"]["transferCount"] == 1
    assert result.legs["previousToCandidate"]["costComponentProvenance"]["wait"] == ("included_in_provider_duration")


def test_provider_route_insertion_service_rejects_missing_baseline_before_write():
    service = ProviderRouteInsertionService(route_service=_RecordedRouteService(omit_baseline=True))

    result = service.evaluate(
        plan_id="matrix",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        following=_matrix_point("following", "B00000003", "13:00", "14:00"),
        candidate_duration_minutes=60,
        detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0},
        mobility_profile=MOBILITY_PROFILE,
        route_decision_contract=_route_contract(),
    )

    assert result.passed is False
    assert result.failure_reason == "provider_route_leg_missing:previous_to_next"


def test_provider_route_insertion_does_not_retry_through_mode_expanding_legacy_signature():
    class LegacyRouteService:
        def __init__(self):
            self.calls = 0

        def build_routes(self, _plan_id, _pois, _transport_mode, *, segments=None):
            self.calls += 1
            return []

    routes = LegacyRouteService()
    service = ProviderRouteInsertionService(route_service=routes)

    leg = service._provider_leg(
        plan_id="legacy_signature",
        left=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        right=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        transport_mode="transit",
    )

    assert leg is None
    assert routes.calls == 0


def test_provider_replacement_uses_old_two_leg_baseline_and_rejects_stale_routes():
    routes = _RecordedRouteService()
    service = ProviderRouteInsertionService(route_service=routes)

    result = service.evaluate(
        plan_id="replacement",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        following=_matrix_point("following", "B00000003", "13:00", "14:00"),
        baseline_candidate=_matrix_point("current", "B00000004", "10:30", "11:30"),
        candidate_duration_minutes=60,
        detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0},
        mobility_profile=MOBILITY_PROFILE,
        route_decision_contract=_route_contract(),
    )

    assert result.passed is True
    assert result.score is not None and result.score.generalized_cost_delta < 0
    assert routes.calls == [
        ("previous", "candidate"),
        ("candidate", "following"),
        ("previous", "current"),
        ("current", "following"),
    ]
    assert set(result.legs) == {
        "previousToCandidate",
        "candidateToNext",
        "baselinePreviousToCurrent",
        "baselineCurrentToNext",
        "previousToNext",
    }

    stale = ProviderRouteInsertionService(
        route_service=_RecordedRouteService(queried_at=datetime.now(timezone.utc) - timedelta(days=2))
    ).evaluate(
        plan_id="stale",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        following=_matrix_point("following", "B00000003", "13:00", "14:00"),
        candidate_duration_minutes=60,
        detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0},
        mobility_profile=MOBILITY_PROFILE,
        route_decision_contract=_route_contract(),
    )
    assert stale.passed is False
    assert stale.failure_reason == "provider_route_leg_missing:previous_to_candidate"


def test_terminal_replacement_uses_old_provider_leg_as_signed_baseline():
    routes = _RecordedRouteService()
    service = ProviderRouteInsertionService(route_service=routes)

    result = service.evaluate(
        plan_id="terminal_replacement",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:30", "11:30"),
        following=None,
        baseline_candidate=_matrix_point("current", "B00000004", "10:30", "11:30"),
        candidate_duration_minutes=60,
        detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0},
        mobility_profile=MOBILITY_PROFILE,
        route_decision_contract=_route_contract(),
    )

    assert result.passed is True
    assert result.score is not None
    assert result.score.generalized_cost_delta == -18.0
    assert routes.calls == [
        ("previous", "candidate"),
        ("previous", "current"),
    ]
    assert result.legs["previousToNext"] == result.legs["baselinePreviousToCurrent"]


def test_one_sided_provider_leg_fails_closed_when_endpoint_gap_is_too_short():
    service = ProviderRouteInsertionService(route_service=_RecordedRouteService())

    result = service.evaluate(
        plan_id="terminal_time_window",
        previous=_matrix_point("previous", "B00000001", "09:00", "10:00"),
        candidate=_matrix_point("candidate", "B00000002", "10:05", "11:05"),
        following=None,
        candidate_duration_minutes=60,
        detour_tolerance={"maxGeneralizedCostDelta": 40, "maxDetourRatio": 1.0},
        mobility_profile=MOBILITY_PROFILE,
        route_decision_contract=_route_contract(),
    )

    assert result.passed is False
    assert result.failure_reason == "route_time_window_infeasible"
    assert result.time_window_feasible is False

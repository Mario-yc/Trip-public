from types import SimpleNamespace

from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.models.route_option import RouteOption
from src.services.adjacent_insertion_candidate_service import AdjacentInsertionCandidateService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.timeline_mutation_poi_resolver import TimelineMutationPoiResolver
from src.services.timeline_mutation_models import (
    BoundTimelineMutation,
    TimelineMutationIntent,
    TimelineMutationReplacement,
    TimelineMutationSelector,
    TimelineSegmentDescriptor,
)


class VerifiedRoute:
    def build_routes(self, plan_id, pois, transport_mode="transit", segments=None, **_kwargs):
        has_candidate = any(str(item.id).startswith("preflight_candidate_") for item in segments or [])
        return [
            RouteOption(
                id=f"route_{left.id}_{right.id}",
                plan_id=plan_id,
                from_segment_id=left.id,
                to_segment_id=right.id,
                from_poi_id=left.poi_id,
                to_poi_id=right.poi_id,
                distance_meters=400 if has_candidate else 750,
                duration_seconds=300 if has_candidate else 550,
                mode=transport_mode,
                provider="amap-webservice",
                is_selected=True,
            )
            for left, right in zip(segments or [], (segments or [])[1:])
        ]


class NearbyMap:
    def __init__(self, pois):
        self.pois = pois
        self.nearby_calls = 0

    def search_nearby(self, **_kwargs):
        self.nearby_calls += 1
        return SimpleNamespace(pois=self.pois)


def poi(identifier, longitude, latitude):
    return MapPoiResponse(
        id=identifier,
        name="北京博物馆",
        city="北京市",
        district="东城",
        address="测试地址",
        longitude=longitude,
        latitude=latitude,
        type="科教文化服务;博物馆;博物馆",
        category="museum",
        source="amap-place-search",
        sourceNote="来源：高德地图",
        confidence=0.9,
    )


def route_decision_contract():
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="test_server_request_contract",
        provenance={"issuer": "test", "transportMode": "transit"},
        detour_tolerance={
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 1.5,
        },
        mobility_profile={
            "source": "test_server_request_contract",
            "walkingPenaltyMinutesPerKm": 2,
            "transferPenaltyMinutes": 6,
            "waitTimeMultiplier": 1,
            "riskPenaltyMultiplier": 1,
        },
    )
    assert contract is not None
    return contract


def bound(*, contract=True, following_start="17:00"):
    intent = TimelineMutationIntent(
        operation="add_segment",
        selector=TimelineMutationSelector(dayNumber=1, intentType="museum", timeWindow="14:00"),
        replacement=TimelineMutationReplacement(
            poiQuery="博物馆", poiQueryMode="category_intent", startTime="14:00", durationMinutes=60
        ),
    )
    previous = TimelineSegmentDescriptor(
        segmentId="previous",
        dayId="day1",
        dayNumber=1,
        segmentOrder=1,
        startTime="09:00",
        endTime="11:00",
        kind="visit",
        poiName="前锚点",
        poiCanonicalName="前锚点",
        poiLongitude=116.40,
        poiLatitude=39.90,
        poiAmapId="B0PREVIOUS",
        transportMode="transit",
        routeAnchor=True,
    )
    following = TimelineSegmentDescriptor(
        segmentId="next",
        dayId="day1",
        dayNumber=1,
        segmentOrder=2,
        startTime=following_start,
        endTime="18:00",
        kind="visit",
        poiName="后锚点",
        poiCanonicalName="后锚点",
        poiLongitude=116.42,
        poiLatitude=39.90,
        poiAmapId="B0FOLLOWING",
        transportMode="transit",
        routeAnchor=True,
    )
    return BoundTimelineMutation(
        mutationId="mutation",
        sessionId="session",
        planId="plan",
        baseVersionId="version",
        baseSnapshotFingerprint="a" * 64,
        intent=intent,
        targetDayId="day1",
        bindingStatus="unique",
        adjacentDescriptors=[previous, following],
        insertionIndex=1,
        routeDecisionContract=route_decision_contract() if contract else None,
    )


def test_nearby_candidate_is_selected_before_any_mutation_write():
    service = AdjacentInsertionCandidateService(NearbyMap([poi("near", 116.41, 39.90)]), VerifiedRoute())
    candidates, evidence, failure = service.resolve(bound(), city="北京")
    assert failure is None
    assert [item.id for item in candidates] == ["near"]
    assert evidence[0]["insertion"]["detourLevel"] == "low"


def test_far_geometry_precheck_is_not_a_hard_rejection_when_provider_route_is_feasible():
    map_service = NearbyMap([poi("far", 116.90, 40.50)])
    candidates, evidence, failure = AdjacentInsertionCandidateService(map_service, VerifiedRoute()).resolve(
        bound(), city="北京"
    )
    assert [candidate.id for candidate in candidates] == ["far"]
    assert failure is None
    assert map_service.nearby_calls >= 1
    assert evidence[0]["insertion"]["detourLevel"] == "high"


def test_exact_entity_never_substitutes_a_different_poi_and_requires_route_evidence():
    exact = bound()
    exact.intent.replacement.poi_query_mode = "exact_entity"
    exact.intent.replacement.poi_query = "北京目标博物馆"

    class ExactMap(NearbyMap):
        def search(self, **_kwargs):
            return SimpleNamespace(pois=[poi("different", 116.41, 39.90)])

    candidates, evidence, failure = AdjacentInsertionCandidateService(ExactMap([]), VerifiedRoute()).resolve(
        exact, city="北京"
    )
    assert candidates == []
    assert failure == "no_adjacent_route_feasible_candidate"
    assert evidence[0]["identity"]["reason"] == "exact_entity_identity_mismatch"


def test_nearby_candidate_without_positive_route_evidence_is_zero_write_preflight_failure():
    class EmptyRoute:
        def build_routes(self, *_args, **_kwargs):
            return []

    candidates, evidence, failure = AdjacentInsertionCandidateService(
        NearbyMap([poi("near", 116.41, 39.90)]), EmptyRoute()
    ).resolve(bound(), city="北京")
    assert candidates == []
    assert failure == "route_provider_matrix_missing"


def test_route_preflight_rejects_missing_explicit_transport_provenance_before_provider_call():
    class NeverCalledRoute:
        def build_routes(self, *_args, **_kwargs):
            raise AssertionError("provider must not be called without an authoritative mode")

    mutation = bound()
    mutation.route_decision_contract["provenance"].pop("transportMode")
    mutation.adjacent_descriptors[0].transport_mode = ""
    mutation.adjacent_descriptors[1].transport_mode = ""
    candidates, evidence, failure = AdjacentInsertionCandidateService(
        NearbyMap([poi("near", 116.41, 39.90)]), NeverCalledRoute()
    ).resolve(mutation, city="北京")

    assert candidates == []
    assert failure == "route_transport_mode_missing"
    assert evidence[0]["routeVerification"]["failureReason"] == failure


def test_route_preflight_rejects_missing_real_candidate_duration_before_provider_call():
    class NeverCalledRoute:
        def build_routes(self, *_args, **_kwargs):
            raise AssertionError("provider must not be called without real duration")

    mutation = bound()
    mutation.intent.replacement.duration_minutes = None
    candidates, evidence, failure = AdjacentInsertionCandidateService(
        NearbyMap([poi("near", 116.41, 39.90)]), NeverCalledRoute()
    ).resolve(mutation, city="北京")

    assert candidates == []
    assert failure == "candidate_duration_missing"
    assert evidence[0]["routeVerification"]["failureReason"] == failure
    assert evidence[0]["routeVerification"]["status"] == "failed"


def test_route_provider_unavailable_is_reported_as_provider_failure_not_missing_candidate():
    class UnavailableRoute:
        def build_routes(self, *_args, **_kwargs):
            raise RuntimeError("route provider unavailable")

    map_service = NearbyMap([poi("near", 116.41, 39.90)])
    adjacent = AdjacentInsertionCandidateService(map_service, UnavailableRoute())
    outcome = TimelineMutationPoiResolver(
        map_poi_service=map_service,
        adjacent_insertion_service=adjacent,
    ).resolve(bound(), city="北京")

    assert outcome.status == "provider_failure"
    assert outcome.failure_reason == "route_provider_unavailable"


def test_adjacent_route_decision_contract_is_required_before_candidate_admission():
    candidates, evidence, failure = AdjacentInsertionCandidateService(
        NearbyMap([poi("near", 116.41, 39.90)]), VerifiedRoute()
    ).resolve(bound(contract=False), city="北京")

    assert candidates == []
    assert failure == "route_decision_contract_missing_or_invalid"
    assert evidence[0]["routeVerification"]["failureReason"] == failure


def test_adjacent_schedule_projection_rejects_a_candidate_that_cannot_fit_real_gap():
    candidates, evidence, failure = AdjacentInsertionCandidateService(
        NearbyMap([poi("near", 116.41, 39.90)]), VerifiedRoute()
    ).resolve(bound(following_start="11:05"), city="北京")

    assert candidates == []
    assert failure == "route_time_window_infeasible"
    assert evidence[0]["routeVerification"]["timeWindowFeasible"] is False

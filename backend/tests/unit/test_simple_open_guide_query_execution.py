from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.schemas.agent import AgentInitialPlanOutput
from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.models.poi import POI
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor


def _hint(name="景山公园", intent="park"):
    return {
        "schemaVersion": "guide-place-hint-v1",
        "mentionText": name,
        "intentType": intent,
        "sourceRefIds": ["guide-park"],
        "sourceFingerprints": ["a" * 64],
        "guideEvidenceFingerprint": "b" * 64,
        "verificationStatus": "unresolved_amap_grounding",
    }


def _initial(slots):
    return AgentInitialPlanOutput.model_validate(
        {
            "reply": "guide execution regression",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": key,
                    "dayNumber": day,
                    "date": f"2026-10-0{day}",
                    "startTime": "",
                    "timeWindow": "",
                    "durationMinutes": 0,
                    "kind": "campus" if intent == "campus_visit" else intent,
                    "rawNeed": "高校参观" if intent == "campus_visit" else "城市公园",
                    "routeAnchor": True,
                }
                for key, day, intent in slots
            ],
            "intentPools": [
                {
                    "poolId": f"pool-{key}",
                    "rawNeed": "高校参观" if intent == "campus_visit" else "城市公园",
                    "city": "北京",
                    "intentType": intent,
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "candidateHints": [],
                    "assignToSlots": [key],
                }
                for key, _, intent in slots
            ],
        }
    )


def _poi(name, identity, *, campus=False):
    return MapPoiResponse(
        id=identity.ljust(10, "0"),
        name=name,
        city="北京",
        district="海淀区",
        address=f"{name}地址",
        category="campus" if campus else "park",
        longitude=116.31 if campus else 116.32,
        latitude=39.99,
        source="amap-place-search",
        sourceNote="recorded provider shape",
        type="科教文化服务;学校;高等院校" if campus else "风景名胜;公园广场;公园",
        providerTypeCode="141201" if campus else "110101",
        confidence=1.0,
        providerQueriedAt=datetime.now(timezone.utc),
        providerQueryReceiptFingerprint="d" * 64,
        openTimeToday="00:00-24:00",
    )


class _Provider:
    def __init__(self, responses=None, failure=False, campus=True):
        self.calls = []
        self.responses = responses or []
        self.failure = failure
        self.campus = campus

    def _response(self, city, keyword, category, scope, radius=None):
        self.calls.append((keyword, scope, radius))
        if category == "campus":
            pois = (
                (
                    self.responses
                    if self.responses and all(poi.category == "campus" for poi in self.responses)
                    else [_poi("北京大学", "B0CAMPUS", campus=True)]
                )
                if self.campus
                else []
            )
        elif self.failure:
            raise TimeoutError("recorded timeout")
        else:
            pois = self.responses
        return MapPoiSearchResponse(
            city=city,
            keyword=keyword,
            category=category,
            providerName="amap-place-search",
            queriedAt=datetime.now(timezone.utc),
            pois=pois,
        )

    def search(self, city, keyword, category="all", **_kwargs):
        return self._response(city, keyword, category, "city_text")

    def search_nearby(self, city, longitude, latitude, keyword, category="all", radius=None, **_kwargs):
        return self._response(city, keyword, category, "nearby_low_detour", radius)


class _NoRouteWrites:
    def assign(self, plans, _candidates, **_kwargs):
        return SimpleNamespace(plans=plans, audit={})


def _build(provider, *, slots=None, hints=None, nearby=True, frontier=None):
    executor = SimpleOpenItineraryExecutor(provider, route_assignment_service=_NoRouteWrites())
    return executor.build_segment_plans(
        _initial(slots or [("campus", 1, "campus_visit"), ("park", 1, "park")]),
        city="北京",
        transport_mode="public_transit",
        guide_place_hints=hints or [_hint()],
        route_decision_contract={"status": "ready", "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000}}
        if nearby
        else None,
        frontier_assignment=frontier,
    )


@pytest.mark.parametrize("failure", [False, True])
def test_real_build_records_actual_nearby_query_outcome_in_guide_attempt(failure):
    provider = _Provider(failure=failure)
    plans, _ = _build(provider)
    attempt = plans[1].schedule_constraints["guideEvidenceAttempt"]
    assert provider.calls == [("高校参观", "city_text", None), ("景山公园", "nearby_low_detour", 5000)]
    assert attempt["providerCalled"] is True
    assert attempt["providerOutcome"] == ("failure" if failure else "success")
    assert attempt["queryText"] == "景山公园"
    assert attempt["searchScope"] == "nearby_low_detour"
    assert attempt["nearbyRadiusMeters"] == 5000
    assert attempt["providerResultCount"] == 0
    assert attempt["reasonCode"] == ("provider_failure" if failure else "no_match_in_search_scope")
    assert "guideEvidence" not in plans[1].schedule_constraints


def test_missing_frontier_day_seed_is_not_reported_as_an_amap_no_match():
    provider = _Provider(campus=False)
    frontier = {
        "schemaVersion": "simple-direction-frontier-attempt-v1",
        "campusAssignments": [
            {"slotId": "campus", "dayNumber": 1, "canonicalName": "北京大学", "evidenceEntityFingerprint": "c" * 64}
        ],
    }
    plans, _ = _build(provider, frontier=frontier)
    attempt = plans[1].schedule_constraints["guideEvidenceAttempt"]
    assert provider.calls == [("北京大学", "city_text", None)]
    assert attempt["providerCalled"] is False
    assert attempt["providerOutcome"] == "not_called"
    assert attempt["reasonCode"] == "query_not_executed"
    assert attempt["queryNotExecutedReason"] == "missing_day_seed"


def test_guided_slot_does_not_silently_reuse_generic_candidate_queue():
    provider = _Provider([_poi("北京大学", "B0CAMPUS1", campus=True), _poi("清华大学", "B0CAMPUS2", campus=True)])
    plans, events = _build(
        provider,
        slots=[("campus1", 1, "campus_visit"), ("campus2", 2, "campus_visit")],
        hints=[_hint("北京大学", "campus_visit"), _hint("清华大学", "campus_visit")],
        nearby=False,
    )
    assert [call[0] for call in provider.calls] == ["北京大学", "清华大学"]
    assert [plan.selected_poi.name for plan in plans] == ["北京大学", "清华大学"]
    assert not any(event["type"] == "simple_open_candidate_pool_reused" for event in events)


def test_generic_primary_result_cannot_displace_the_guide_entity_or_claim_its_evidence():
    provider = _Provider([_poi("南海子公园", "B0PARK1"), _poi("景山公园", "B0PARK2")])
    plans, events = _build(provider)
    assert plans[1].selected_poi is not None, [
        event.get("metadata", {}).get("candidateAdmissionRejectionReasonCounts")
        for event in events
        if event.get("type") == "simple_open_tool_call"
    ]
    assert plans[1].selected_poi.name == "景山公园"
    evidence = plans[1].schedule_constraints["guideEvidence"]
    assert evidence["amapPoiId"] == "B0PARK2000"
    attempt = plans[1].schedule_constraints["guideEvidenceAttempt"]
    assert attempt["providerCalled"] is True
    assert attempt["guideMatchCount"] == 1
    assert attempt["status"] == "grounded"


def test_same_guide_place_returned_only_as_a_facility_does_not_acquire_identity():
    provider = _Provider([_poi("景山公园停车场", "B0FACILITY")])
    plans, _ = _build(provider)
    assert plans[1].selected_poi is None
    assert plans[1].schedule_constraints["guideEvidenceAttempt"]["reasonCode"] == "no_match_in_search_scope"
    assert len(provider.calls) == 2


def test_budget_pressure_never_rewrites_guide_query_to_generic_search(monkeypatch):
    monkeypatch.setattr("src.services.simple_open_itinerary_executor.MAX_SIMPLE_OPEN_POI_SEARCHES", 2)
    provider = _Provider()
    plans, _ = _build(provider, slots=[("park1", 1, "park"), ("park2", 2, "park")], nearby=False)
    assert [call[0] for call in provider.calls] == ["景山公园", "景山公园"]
    assert all(plan.schedule_constraints["guideEvidenceAttempt"]["providerCalled"] for plan in plans)


def test_budget_exhaustion_is_reported_without_an_extra_guide_query(monkeypatch):
    monkeypatch.setattr("src.services.simple_open_itinerary_executor.MAX_SIMPLE_OPEN_POI_SEARCHES", 1)
    provider = _Provider()
    plans, _ = _build(provider, slots=[("park1", 1, "park"), ("park2", 2, "park")], nearby=False)
    assert len(provider.calls) == 1
    attempt = plans[1].schedule_constraints["guideEvidenceAttempt"]
    assert attempt["providerCalled"] is False
    assert attempt["reasonCode"] == "query_not_executed"
    assert attempt["queryNotExecutedReason"] == "budget_exhausted"


def test_primary_ambiguity_is_measured_before_candidate_admission_filters():
    foreign = _poi("景山公园", "B0OTHER")
    foreign.city = "上海"
    provider = _Provider([_poi("景山公园", "B0PARK"), foreign])
    plans, _ = _build(provider)
    attempt = plans[1].schedule_constraints["guideEvidenceAttempt"]
    assert attempt["guideMatchCount"] == 2
    assert attempt["reasonCode"] == "ambiguous_amap_match"
    assert "guideEvidence" not in plans[1].schedule_constraints


def test_admission_exception_preserves_successful_provider_query_evidence(monkeypatch):
    def admission_failed(*_args):
        raise RuntimeError("recorded candidate processing failure")

    monkeypatch.setattr(SimpleOpenItineraryExecutor, "_park_independence_evidence", admission_failed)
    provider = _Provider([_poi("景山公园", "B0PARK")])
    plans, events = _build(provider)
    attempt = plans[1].schedule_constraints["guideEvidenceAttempt"]
    assert attempt["providerCalled"] is True
    assert attempt["providerOutcome"] == "success"
    assert attempt["providerResultCount"] == 1
    assert attempt["guideMatchCount"] == 1
    assert attempt["reasonCode"] == "candidate_processing_failure"
    assert attempt["candidateProcessingErrorType"] == "RuntimeError"
    assert attempt["providerErrorType"] is None
    query = next(
        event
        for event in events
        if event.get("metadata", {}).get("slotKey") == "park" and event["type"] == "simple_open_tool_call"
    )
    assert query["metadata"]["providerOutcome"] == "success"
    assert query["metadata"]["resultCount"] == 1


def test_same_guide_place_can_produce_two_distinct_day_scoped_empty_query_attempts():
    class TwoDayProvider(_Provider):
        def _response(self, city, keyword, category, scope, radius=None):
            if category != "campus":
                return super()._response(city, keyword, category, scope, radius)
            self.calls.append((keyword, scope, radius))
            campus_ordinal = sum(call[1] == "city_text" for call in self.calls)
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    _poi("北京大学" if campus_ordinal == 1 else "清华大学", f"B0CAMPUS0{campus_ordinal}", campus=True)
                ],
            )

    provider = TwoDayProvider()
    plans, _ = _build(
        provider,
        slots=[
            ("campus1", 1, "campus_visit"),
            ("park1", 1, "park"),
            ("campus2", 2, "campus_visit"),
            ("park2", 2, "park"),
        ],
    )
    attempts = [plan.schedule_constraints["guideEvidenceAttempt"] for plan in plans if plan.intent_type == "park"]
    assert [(attempt["dayNumber"], attempt["planningSlotId"]) for attempt in attempts] == [(1, "park1"), (2, "park2")]
    assert all(attempt["mentionText"] == "景山公园" and attempt["queryText"] == "景山公园" for attempt in attempts)
    assert all(
        attempt["searchScope"] == "nearby_low_detour" and attempt["nearbyRadiusMeters"] == 5000 for attempt in attempts
    )
    assert all(
        attempt["providerCalled"] and attempt["reasonCode"] == "no_match_in_search_scope" for attempt in attempts
    )
    assert len(provider.calls) == 4


@pytest.mark.parametrize("name", ["海子公园", "南海子公园南门", "南海子公园停车场"])
def test_substring_or_facility_name_is_not_guide_place_identity(name):
    selected = POI(
        id="poi-guide",
        amap_id="B0PARK0000",
        name=name,
        city="北京",
        category="park",
        latitude=39.99,
        longitude=116.31,
        source="amap-place-search",
        confidence=1.0,
    )
    assert not SimpleOpenItineraryExecutor._guide_hint_matches_selected(_hint("南海子公园"), selected)


@pytest.mark.parametrize(
    "name",
    [
        "四季民福",
        "四季民福烤鸭店",
        "四季民福烤鸭店(故宫店)",
        "四季民福饭店（王府井店）",
        "四季民福小馆",
    ],
)
def test_complete_restaurant_brand_matches_one_qualified_type_and_optional_branch(name):
    assert SimpleOpenItineraryExecutor._guide_hint_matches_name(_hint("四季民福", "meal"), name)


@pytest.mark.parametrize(
    "name",
    [
        "季民福烤鸭店",
        "新四季民福烤鸭店",
        "四季民福特色店",
        "四季民福门店",
        "四季民福烤鸭店停车场",
        "四季民福餐厅烤鸭店",
        "四季民福烤鸭店(故宫店)(王府井店)",
        "四季民福烤鸭店家",
        "四季民福小馆子",
    ],
)
def test_restaurant_brand_matching_does_not_strip_multiple_words_or_facility_names(name):
    assert not SimpleOpenItineraryExecutor._guide_hint_matches_name(_hint("四季民福", "meal"), name)


@pytest.mark.parametrize(
    "names",
    [
        ["四季民福烤鸭店"],
        ["四季民福烤鸭店(故宫店)"],
        ["四季民福烤鸭店(故宫店)", "四季民福烤鸭店(王府井店)"],
        ["其他品牌烤鸭店"],
    ],
)
def test_actual_meal_build_preserves_single_brand_identity_and_raw_multiple_branch_ambiguity(names):
    responses = [
        _poi(name, f"B0MEAL000{index}").model_copy(
            update={
                "category": "food",
                "type": "餐饮服务;中餐厅;北京菜",
                "provider_type_code": "050111",
            }
        )
        for index, name in enumerate(names, start=1)
    ]
    provider = _Provider(responses)
    initial = _initial([("campus", 1, "campus_visit"), ("meal", 1, "meal")])
    initial.day_slots[1].raw_need = "午餐"
    initial.intent_pools[1].raw_need = "午餐"
    executor = SimpleOpenItineraryExecutor(provider, route_assignment_service=_NoRouteWrites())
    plans, _ = executor.build_segment_plans(
        initial,
        city="北京",
        transport_mode="public_transit",
        guide_place_hints=[_hint("四季民福", "meal")],
        route_decision_contract={"status": "ready", "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000}},
    )
    assert provider.calls == [("高校参观", "city_text", None), ("四季民福", "nearby_low_detour", 5000)]
    constraints = plans[1].schedule_constraints
    attempt = constraints["guideEvidenceAttempt"]
    expected_matches = len(names) if names[0].startswith("四季民福") else 0
    assert attempt["providerCalled"] is True
    assert attempt["guideMatchCount"] == expected_matches
    if expected_matches == 1:
        assert constraints["guideEvidence"]["amapPoiId"] == "B0MEAL0001"
        assert attempt["status"] == "grounded"
    else:
        assert "guideEvidence" not in constraints
        assert attempt["reasonCode"] == ("ambiguous_amap_match" if expected_matches > 1 else "no_match_in_search_scope")

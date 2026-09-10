import copy
import importlib.util
import json
import os
import re
import shutil
import sys

import pytest

import backend.evals.recorded_fixture_capture as recorded_fixture_capture_module
import backend.evals.run_offline as offline_eval_module
from backend.evals.recorded_fixture_capture import (
    build_capture_preflight_manifest,
    canonical_sha256,
    validate_capture_preflight_manifest,
)
from backend.evals.run_offline import CASES_DIR, run_offline_eval
from src.core.config import get_settings
from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.services.amap_call_budget import AmapCallBudget
from src.services.creative_planning_models import canonical_fingerprint


def test_offline_full_requires_explicit_pending_coverage_fixture():
    provider = offline_eval_module.OfflineAgentProvider([])
    context = {"requestActivityClauses": [{"clauseId": "request_clause_1"}]}
    with pytest.raises(AssertionError, match="explicit source-clause coverage fixture"):
        provider.decide_autonomy(context, timeout_seconds=10)


def test_offline_full_coverage_fixture_uses_same_goal_ids_and_is_consumed_once():
    coverage = [{"clauseId": "request_clause_1", "classification": "activity", "activities": [{
        "goalId": "goal_request_1_1", "sourceText": "参观高校", "intentType": "campus_visit",
        "polarity": "required", "allowedDayNumbers": [1], "minCount": 1, "dayPart": "morning",
    }]}]
    provider = offline_eval_module.OfflineAgentProvider([], request_coverage_payloads=[coverage])
    context = {"requestActivityClauses": [{"clauseId": "request_clause_1"}], "availableDayNumbers": [1],
               "goalRequirements": [{"goalId": "legacy_goal", "intentType": "campus_visit", "requiredMin": 1}]}
    before = copy.deepcopy(context)
    directive = json.loads(provider.decide_autonomy(context, timeout_seconds=10))["actionDirective"]
    assert directive["requestCoverage"] == coverage
    assert directive["dayStrategies"][0]["requiredGoalIds"] == ["goal_request_1_1"]
    assert directive["occurrenceScheduleHints"][0]["dayPart"] == "morning"
    assert context == before
    with pytest.raises(AssertionError, match="explicit source-clause coverage fixture"):
        provider.decide_autonomy(context, timeout_seconds=10)


def _write_strict_recorded_amap_fixture(
    tmp_path,
    *,
    omit_place_parameter: str = "",
    route_pair=None,
    tamper_response_hash: bool = False,
    record_case_id: str = "",
    add_other_case_record: bool = False,
):
    from src.services.map_poi_service import MapPoiService, clear_map_poi_runtime_state
    from src.services.route_service import RouteService

    recorded_fixture = json.loads(
        (CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json").read_text(encoding="utf-8")
    )
    route_record = next(
        item
        for item in recorded_fixture["responses"]
        if isinstance(item, dict)
        and str((item.get("request") or {}).get("endpoint") or "").startswith("/v3/direction/")
        and isinstance(item.get("requestPair"), dict)
    )
    original_pair = dict(route_record["requestPair"])
    poi_by_amap_id = {
        str(poi.get("id") or ""): poi
        for item in recorded_fixture["responses"]
        if isinstance(item, dict)
        for poi in ((item.get("response") or {}).get("pois") or [])
        if isinstance(poi, dict) and str(poi.get("id") or "")
    }
    from_raw = poi_by_amap_id[original_pair["fromAmapId"]]
    to_raw = poi_by_amap_id[original_pair["toAmapId"]]
    place_record = next(
        item
        for item in recorded_fixture["responses"]
        if isinstance(item, dict)
        and str((item.get("request") or {}).get("endpoint") or "") == "/v3/place/text"
        and any(
            isinstance(poi, dict) and str(poi.get("id") or "") == original_pair["fromAmapId"]
            for poi in ((item.get("response") or {}).get("pois") or [])
        )
    )

    def poi_from_record(raw_poi, local_id):
        longitude, latitude = str(raw_poi["location"]).split(",", 1)
        return POI(
            id=local_id,
            name=str(raw_poi["name"]),
            city=str(raw_poi.get("cityname") or raw_poi.get("pname") or ""),
            category="scenic",
            latitude=float(latitude),
            longitude=float(longitude),
            source="amap-place-search",
            amap_id=str(raw_poi["id"]),
            type=str(raw_poi.get("type") or ""),
            district=str(raw_poi.get("adname") or ""),
            address=str(raw_poi.get("address") or ""),
        )

    from_poi = poi_from_record(from_raw, "from-poi")
    to_poi = poi_from_record(to_raw, "to-poi")
    search_city = str(from_raw.get("pname") or from_poi.city)
    search_keyword = str((place_record.get("request") or {}).get("params", {}).get("keywords") or "")

    # Freeze the complete request metadata from the original production
    # request builder while keeping raw POI/route response evidence untouched.
    captured_place_params = {}

    class _CapturedMapRequest(RuntimeError):
        pass

    clear_map_poi_runtime_state()
    request_builder = MapPoiService(map_provider_key="request-metadata-capture")

    def capture_place_request(params):
        captured_place_params.update(params)
        raise _CapturedMapRequest()

    request_builder._fetch_amap_place = capture_place_request
    with pytest.raises(_CapturedMapRequest):
        request_builder.search(search_city, keyword=search_keyword, category="all", limit=12)
    route_endpoint, route_params = RouteService(map_provider_key="request-metadata-capture")._amap_request_params(
        from_poi,
        to_poi,
        original_pair["mode"],
    )

    payload = {key: copy.deepcopy(value) for key, value in recorded_fixture.items() if key != "responses"}
    selected_place_record = copy.deepcopy(place_record)
    selected_route_record = copy.deepcopy(route_record)
    selected_place_record["request"]["params"] = {
        key: value for key, value in captured_place_params.items() if key != "key"
    }
    selected_place_record["request"]["params"].pop(omit_place_parameter, None)
    selected_route_record["request"] = {"endpoint": route_endpoint, "params": route_params}
    selected_route_record["requestPair"] = route_pair or original_pair
    payload["responses"] = [selected_place_record, selected_route_record]
    if record_case_id:
        for record in payload["responses"]:
            record["caseId"] = record_case_id
    if add_other_case_record:
        other_record = copy.deepcopy(payload["responses"][0])
        other_record["caseId"] = "other-recorded-case"
        payload["responses"].append(other_record)
    if tamper_response_hash:
        payload["responses"][0]["responseSha256"] = "0" * 64
    fixture_path = tmp_path / "strict-recorded-amap.json"
    fixture_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return fixture_path, search_city, search_keyword, from_poi, to_poi, selected_route_record["response"], original_pair


def _strict_replay_scope_args():
    from src.services.map_poi_service import MapPoiService
    from src.services.route_service import RouteService

    return {
        "production_map_search": MapPoiService.search,
        "production_map_fetch_place": MapPoiService._fetch_amap_place,
        "production_map_fetch_around": MapPoiService._fetch_amap_around,
        "production_build_routes": RouteService.build_routes,
    }


def _strict_replay_route_inputs(from_poi, to_poi):
    segments = [
        ItinerarySegment(
            id="from-segment",
            day_id="day-1",
            segment_order=1,
            kind="visit",
            start_time="09:00",
            end_time="11:00",
            poi_id=from_poi.id,
            transport_mode="transit",
            estimated_cost=0.0,
            notes="",
        ),
        ItinerarySegment(
            id="to-segment",
            day_id="day-1",
            segment_order=2,
            kind="visit",
            start_time="11:30",
            end_time="13:00",
            poi_id=to_poi.id,
            transport_mode="transit",
            estimated_cost=0.0,
            notes="",
        ),
    ]
    return [from_poi, to_poi], segments


def _exact_dry_route_repair_scope(
    *,
    planning_slot_id: str = "slot-recorded",
) -> dict:
    scope = {
        "continuationMode": "repair_exact_slot",
        "rootPortfolioId": "portfolio-recorded",
        "planningSelectionRootTurnId": "planning-root-recorded",
        "briefId": "brief-recorded",
        "poolId": "pool-recorded",
        "dayNumber": 1,
        "planningSlotId": planning_slot_id,
        "candidatePhysicalId": "B000A816R6",
        "adjacentAnchorIds": ["from-segment"],
        "adjacentRouteLedgerKeys": [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
            }
        ],
        "routeContractFingerprint": canonical_fingerprint({"preferredMode": "transit", "status": "ready"}),
    }
    scope["scopeFingerprint"] = canonical_fingerprint(scope)
    return scope


def test_source_fingerprint_never_reads_local_secret_env(monkeypatch, tmp_path):
    read_paths: list[str] = []

    def fake_git(_project_root, *args):
        if args == ("rev-parse", "HEAD"):
            return b"frozen-head\n"
        return b""

    def fake_file_sha(path):
        read_paths.append(path.relative_to(tmp_path).as_posix())
        return "0" * 64

    monkeypatch.setattr(recorded_fixture_capture_module, "_git_stdout", fake_git)
    monkeypatch.setattr(recorded_fixture_capture_module, "_file_sha256", fake_file_sha)

    recorded_fixture_capture_module.compute_source_fingerprint(project_root=tmp_path)

    assert "backend/.env" not in read_paths
    assert "backend/.env.example" in read_paths
    assert "frontend/.env.example" in read_paths


def test_unified_recorded_replay_uses_production_map_and_route_services(monkeypatch, tmp_path):
    """The strict branch bypasses legacy eval wrappers and consumes only its case records."""

    from src.services.map_poi_service import MapPoiService
    from src.services.route_service import RouteService

    (
        fixture_path,
        search_city,
        search_keyword,
        from_poi,
        to_poi,
        recorded_route_response,
        recorded_pair,
    ) = _write_strict_recorded_amap_fixture(
        tmp_path,
        record_case_id="strict-case",
        add_other_case_record=True,
    )
    scope_args = _strict_replay_scope_args()
    legacy_calls: list[str] = []

    def legacy_map_search(*_args, **_kwargs):
        legacy_calls.append("map-search")
        raise AssertionError("strict replay must not use the legacy map wrapper")

    def legacy_map_fetch(*_args, **_kwargs):
        legacy_calls.append("map-fetch")
        raise AssertionError("strict replay must not use the legacy fetch wrapper")

    def legacy_route_build(*_args, **_kwargs):
        legacy_calls.append("route-build")
        raise AssertionError("strict replay must not use the legacy route wrapper")

    monkeypatch.setattr(offline_eval_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(MapPoiService, "search", legacy_map_search)
    monkeypatch.setattr(MapPoiService, "_fetch_amap_place", legacy_map_fetch)
    monkeypatch.setattr(RouteService, "build_routes", legacy_route_build)

    with offline_eval_module._offline_strict_recorded_amap_replay_scope(
        fixture_path.name,
        case_id="strict-case",
        **scope_args,
    ) as replay:
        places = MapPoiService().search(search_city, keyword=search_keyword, category="all", limit=12)
        pois, segments = _strict_replay_route_inputs(from_poi, to_poi)
        routes = RouteService().build_routes(
            "strict-plan",
            pois,
            transport_mode="transit",
            segments=segments,
            preferred_mode_only=True,
        )
        trace = replay.trace()

    assert legacy_calls == []
    assert places.pois[0].id == from_poi.amap_id
    assert len(routes) == 1
    expected_transit = recorded_route_response["route"]["transits"][0]
    assert routes[0].duration_seconds == int(expected_transit["duration"])
    assert routes[0].distance_meters == int(expected_transit["distance"])
    assert routes[0].provider_payload["recordedProviderEvidence"]["requestPair"] == recorded_pair
    assert trace["failureCount"] == 0
    assert len(trace["matched"]) == 2
    assert trace["selector"] == {
        "caseId": "strict-case",
        "usesFixtureCaseIds": True,
        "expectedRecordKeyCount": 0,
        "allowedRecordCount": 2,
        "fixtureRecordCount": 3,
    }


def test_unified_recorded_replay_rejects_complete_parameter_mismatch_and_bad_hash(monkeypatch, tmp_path):
    from src.services.map_poi_service import MapPoiService

    fixture_path, search_city, search_keyword, _from_poi, _to_poi, _route_response, _pair = (
        _write_strict_recorded_amap_fixture(
            tmp_path,
            omit_place_parameter="offset",
        )
    )
    monkeypatch.setattr(offline_eval_module, "PROJECT_ROOT", tmp_path)
    scope_args = _strict_replay_scope_args()

    with offline_eval_module._offline_strict_recorded_amap_replay_scope(
        fixture_path.name,
        **scope_args,
    ) as replay:
        with pytest.raises(offline_eval_module._OfflineRecordedAmapReplayError, match="request_not_found"):
            MapPoiService().search(search_city, keyword=search_keyword, category="all", limit=12)
        trace = replay.trace()

    assert trace["unmatched"][0]["reason"] == "request_not_found"
    assert trace["unmatched"][0]["params"]["offset"] == "12"
    assert trace["failureCount"] >= 1

    hash_fixture_path, _search_city, _search_keyword, _from_poi, _to_poi, _route_response, _pair = (
        _write_strict_recorded_amap_fixture(
            tmp_path,
            tamper_response_hash=True,
        )
    )
    bad_transport = offline_eval_module._OfflineStrictRecordedAmapTransport(hash_fixture_path.name)
    assert bad_transport.trace()["failures"] == [
        {"fixture": hash_fixture_path.name, "reason": "fixture_response_hash_mismatch"}
    ]


def test_unified_recorded_replay_route_pair_mode_miss_is_empty_and_auditable(monkeypatch, tmp_path):
    from src.services.map_poi_service import MapPoiService
    from src.services.route_service import RouteService

    (
        _bootstrap_path,
        _bootstrap_city,
        _bootstrap_keyword,
        _bootstrap_from_poi,
        _bootstrap_to_poi,
        _bootstrap_route_response,
        original_pair,
    ) = _write_strict_recorded_amap_fixture(tmp_path)
    wrong_pair = dict(original_pair)
    wrong_pair["mode"] = "walking" if original_pair["mode"] != "walking" else "transit"
    (
        fixture_path,
        search_city,
        search_keyword,
        from_poi,
        to_poi,
        _route_response,
        _pair,
    ) = _write_strict_recorded_amap_fixture(
        tmp_path,
        route_pair=wrong_pair,
    )
    monkeypatch.setattr(offline_eval_module, "PROJECT_ROOT", tmp_path)
    scope_args = _strict_replay_scope_args()

    with offline_eval_module._offline_strict_recorded_amap_replay_scope(
        fixture_path.name,
        **scope_args,
    ) as replay:
        # Consume the only POI record; the remaining route record has matching
        # URL parameters but an incompatible authoritative request pair/mode.
        MapPoiService().search(search_city, keyword=search_keyword, category="all", limit=12)
        pois, segments = _strict_replay_route_inputs(from_poi, to_poi)
        route_service = RouteService()
        routes = route_service.build_routes(
            "strict-plan",
            pois,
            transport_mode="transit",
            segments=segments,
            preferred_mode_only=True,
        )
        trace = replay.trace()

    assert routes == []
    assert any("recorded_amap_replay_failed:route_pair_not_found" in warning for warning in route_service.warnings)
    assert trace["unmatched"][-1]["reason"] == "route_pair_not_found"
    assert trace["failureCount"] >= 1
    result = {"passed": True, "failureReason": ""}
    offline_eval_module._strict_replay_result_failure(result, replay)
    assert result["passed"] is False
    assert "route_pair_not_found" in result["failureReason"]
    assert result["recordedAmapReplay"]["unmatched"][-1]["requestPair"]["mode"] == "transit"


def test_recorded_provider_route_response_requires_exact_pair_and_hash_binding():
    fixture_path = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    records = [
        item for item in fixture["responses"] if isinstance(item, dict) and isinstance(item.get("requestPair"), dict)
    ]

    response, failure = offline_eval_module._recorded_route_response(
        records,
        from_amap_id="B000A7BD6C",
        to_amap_id="B000A816R6",
        mode="transit",
    )

    assert failure is None
    assert response is not None
    assert response["route"]["transits"][0]["duration"] == "2230"
    assert response["route"]["transits"][0]["distance"] == "2406"

    missing, missing_failure = offline_eval_module._recorded_route_response(
        records,
        from_amap_id="B000A816R6",
        to_amap_id="B000A7BD6C",
        mode="transit",
    )
    assert missing is None
    assert missing_failure == "recorded_route_pair_not_found"

    tampered = copy.deepcopy(records)
    tampered[0]["response"]["route"]["transits"][0]["duration"] = "1"
    invalid, invalid_failure = offline_eval_module._recorded_route_response(
        tampered,
        from_amap_id="B000A7BD6C",
        to_amap_id="B000A816R6",
        mode="transit",
    )
    assert invalid is None
    assert invalid_failure == "recorded_route_response_hash_mismatch"


def test_dry_route_discovery_keeps_the_exact_recorded_pair_contract():
    fixture_path = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    record = next(item for item in fixture["responses"] if isinstance(item.get("requestPair"), dict))
    pair = record["requestPair"]
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    budget.authorize_route_work(
        [
            {
                "fromPhysicalId": str(pair["fromAmapId"]),
                "toPhysicalId": str(pair["toAmapId"]),
                "mode": str(pair["mode"]),
                "reason": "baseline_adjacent",
                "condition": "always",
            }
        ]
    )
    from_poi = POI(
        id="from-poi",
        name="recorded from",
        city="北京",
        category="scenic",
        latitude=39.0,
        longitude=116.0,
        source="amap-place-search",
        amap_id=str(pair["fromAmapId"]),
    )
    to_poi = POI(
        id="to-poi",
        name="recorded to",
        city="北京",
        category="scenic",
        latitude=39.1,
        longitude=116.1,
        source="amap-place-search",
        amap_id=str(pair["toAmapId"]),
    )

    entry = offline_eval_module._route_discovery_entry(
        case_id="fixture-contract",
        plan_id="plan-recorded",
        from_segment_id="from-segment",
        to_segment_id="to-segment",
        from_poi=from_poi,
        to_poi=to_poi,
        mode=str(pair["mode"]),
        route_budget=budget,
        uses_offline_mock_amap=False,
    )
    blocked_entry = offline_eval_module._route_discovery_entry(
        case_id="fixture-contract",
        plan_id="plan-recorded",
        from_segment_id="from-segment",
        to_segment_id="to-segment",
        from_poi=from_poi,
        to_poi=to_poi,
        mode=str(pair["mode"]),
        route_budget=budget,
        uses_offline_mock_amap=True,
    )
    summary = offline_eval_module._summarize_route_discovery([entry, blocked_entry])

    assert entry["captureEligible"] is True
    assert {
        "fromCanonicalAmapId": entry["fromCanonicalAmapId"],
        "toCanonicalAmapId": entry["toCanonicalAmapId"],
        "mode": entry["mode"],
    } == {
        "fromCanonicalAmapId": pair["fromAmapId"],
        "toCanonicalAmapId": pair["toAmapId"],
        "mode": pair["mode"],
    }
    assert summary["captureEligiblePairs"] == []
    assert summary["captureReady"] is False
    assert "offline_mock_amap_poi_provenance" in summary["captureBlockedReasons"]
    assert entry["routeBudget"]["usedRoute"] == 0
    assert budget.used_route == 0
    assert budget.used_total_external == 0
    assert "recordedProviderEvidence" not in entry
    assert "recordedAmapReplay" not in entry
    assert "fixture" not in entry
    assert "externalCaptureSession" not in entry


def test_dry_route_discovery_rejects_pair_without_preissued_exact_route_lease(monkeypatch):
    unscoped_budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    unscoped_budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
                "reason": "baseline_adjacent",
                "condition": "always",
            }
        ]
    )
    unsigned_budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    validation_unavailable_budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    validation_unavailable_budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
                "reason": "baseline_adjacent",
                "condition": "always",
            }
        ]
    )
    monkeypatch.setattr(validation_unavailable_budget, "validate_route_work", None)
    generic_capacity_budget = AmapCallBudget(
        route_refresh_max=1,
        total_external_max=1,
        source="generic-route-budget",
    )
    exact_scope = _exact_dry_route_repair_scope()
    scoped_budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    scoped_budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
                "reason": "replacement_adjacent",
                "condition": "always",
                "candidatePhysicalId": "B000A816R6",
                "repairScopeCertificate": exact_scope,
            }
        ]
    )

    class AggregateOnlyBudget:
        route_refresh_max = 1
        used_route = 0
        used_total_external = 0
        source = "creative_portfolio_route_preflight"

        @staticmethod
        def validate_route_work(**_kwargs):
            return True

    aggregate_only_budget = AggregateOnlyBudget()

    def reject_late_authorization(*_args, **_kwargs):
        raise AssertionError("dry discovery must never authorize or consume route work")

    monkeypatch.setattr(AmapCallBudget, "authorize_route_work", reject_late_authorization)
    monkeypatch.setattr(AmapCallBudget, "try_acquire", reject_late_authorization)

    from_poi = POI(
        id="recorded-from-poi",
        name="recorded from",
        city="北京",
        category="scenic",
        latitude=39.0,
        longitude=116.0,
        source="amap-place-search",
        amap_id="B000A7BD6C",
    )
    to_poi = POI(
        id="recorded-to-poi",
        name="recorded to",
        city="北京",
        category="scenic",
        latitude=39.1,
        longitude=116.1,
        source="amap-place-search",
        amap_id="B000A816R6",
    )
    other_poi = POI(
        id="other-recorded-poi",
        name="other recorded endpoint",
        city="北京",
        category="scenic",
        latitude=39.2,
        longitude=116.2,
        source="amap-place-search",
        amap_id="B000ROUTEC",
    )

    def discover(
        label,
        *,
        budget,
        source_poi=from_poi,
        target_poi=to_poi,
        mode="transit",
        scope=None,
        uses_mock=False,
    ):
        return offline_eval_module._route_discovery_entry(
            case_id="exact-lease-contract",
            plan_id="plan-recorded",
            from_segment_id=f"{label}-from-segment",
            to_segment_id=f"{label}-to-segment",
            from_poi=source_poi,
            to_poi=target_poi,
            mode=mode,
            route_budget=budget,
            uses_offline_mock_amap=uses_mock,
            repair_scope_certificate=scope,
        )

    reverse_entry = discover(
        "reverse",
        budget=unscoped_budget,
        source_poi=to_poi,
        target_poi=from_poi,
    )
    wrong_pair_entry = discover(
        "wrong-pair",
        budget=unscoped_budget,
        target_poi=other_poi,
    )
    wrong_mode_entry = discover("wrong-mode", budget=unscoped_budget, mode="walking")
    unsigned_entry = discover("unsigned", budget=unsigned_budget)
    generic_capacity_entry = discover(
        "generic-capacity",
        budget=generic_capacity_budget,
    )
    validation_unavailable_entry = discover(
        "validation-unavailable",
        budget=validation_unavailable_budget,
    )
    forged_validator_entry = discover(
        "forged-validator",
        budget=aggregate_only_budget,
    )
    exact_entry = discover("exact", budget=unscoped_budget)
    exact_scoped_entry = discover("exact-scoped", budget=scoped_budget, scope=exact_scope)
    missing_scope_entry = discover("missing-scope", budget=scoped_budget)
    empty_scope_entry = discover("empty-scope", budget=scoped_budget, scope={})
    tampered_scope = copy.deepcopy(exact_scope)
    tampered_scope["planningSlotId"] = "slot-tampered-with-stale-fingerprint"
    tampered_scope_entry = discover(
        "tampered-scope",
        budget=scoped_budget,
        scope=tampered_scope,
    )
    mismatched_scope = _exact_dry_route_repair_scope(planning_slot_id="slot-other")
    mismatched_scope_entry = discover(
        "mismatched-scope",
        budget=scoped_budget,
        scope=mismatched_scope,
    )
    mock_entry = discover("mock", budget=unscoped_budget, uses_mock=True)

    assert reverse_entry["captureEligible"] is False
    assert "route_work_unauthorized" in reverse_entry["captureBlockedReasons"]
    assert wrong_pair_entry["captureEligible"] is False
    assert "route_work_unauthorized" in wrong_pair_entry["captureBlockedReasons"]
    assert wrong_mode_entry["captureEligible"] is False
    assert "route_work_unauthorized" in wrong_mode_entry["captureBlockedReasons"]
    assert unsigned_entry["captureEligible"] is False
    assert "route_work_unauthorized" in unsigned_entry["captureBlockedReasons"]
    assert generic_capacity_entry["captureEligible"] is False
    assert "route_budget_source_not_exact_lease" in generic_capacity_entry["captureBlockedReasons"]
    assert validation_unavailable_entry["captureEligible"] is False
    assert "route_budget_validation_unavailable" in validation_unavailable_entry["captureBlockedReasons"]
    assert forged_validator_entry["captureEligible"] is False
    assert "route_budget_not_production_exact_lease" in forged_validator_entry["captureBlockedReasons"]
    assert exact_entry["captureEligible"] is True
    assert exact_scoped_entry["captureEligible"] is True
    assert missing_scope_entry["captureEligible"] is False
    assert "route_scope_missing" in missing_scope_entry["captureBlockedReasons"]
    assert empty_scope_entry["captureEligible"] is False
    assert "route_scope_fingerprint_mismatch" in empty_scope_entry["captureBlockedReasons"]
    assert tampered_scope_entry["captureEligible"] is False
    assert "route_scope_fingerprint_mismatch" in tampered_scope_entry["captureBlockedReasons"]
    assert mismatched_scope_entry["captureEligible"] is False
    assert "route_scope_unauthorized" in mismatched_scope_entry["captureBlockedReasons"]
    assert mock_entry["captureEligible"] is False
    assert "offline_mock_amap_poi_provenance" in mock_entry["captureBlockedReasons"]

    entries = [
        reverse_entry,
        wrong_pair_entry,
        wrong_mode_entry,
        unsigned_entry,
        generic_capacity_entry,
        validation_unavailable_entry,
        forged_validator_entry,
        exact_entry,
        exact_scoped_entry,
        missing_scope_entry,
        empty_scope_entry,
        tampered_scope_entry,
        mismatched_scope_entry,
        mock_entry,
    ]
    summary = offline_eval_module._summarize_route_discovery(entries)
    assert summary["networkCalls"] == 0
    assert summary["captureReady"] is False
    for entry in entries:
        assert entry["routeBudget"]["usedRoute"] == 0
        assert entry["discoveryOnly"] is True
        for forbidden_key in (
            "recordedAmapReplay",
            "recordedProviderEvidence",
            "fixture",
            "externalCaptureSession",
            "activeVersionId",
            "versionDelta",
            "patchDelta",
            "routeWriteDelta",
        ):
            assert forbidden_key not in entry
    for budget in (
        unscoped_budget,
        unsigned_budget,
        validation_unavailable_budget,
        generic_capacity_budget,
        scoped_budget,
    ):
        assert budget.used_route == 0
        assert budget.used_total_external == 0
    assert aggregate_only_budget.used_route == 0
    assert aggregate_only_budget.used_total_external == 0


def test_dry_route_discovery_never_replays_and_rejects_exhausted_budget():
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000A7BD6C",
                "toPhysicalId": "B000A816R6",
                "mode": "transit",
                "reason": "baseline_adjacent",
                "condition": "always",
            }
        ]
    )
    from_poi = POI(
        id="from-poi",
        name="from",
        city="北京",
        category="scenic",
        latitude=39.0,
        longitude=116.0,
        source="amap-place-search",
        amap_id="B000A7BD6C",
    )
    to_poi = POI(
        id="to-poi",
        name="to",
        city="北京",
        category="scenic",
        latitude=39.1,
        longitude=116.1,
        source="amap-place-search",
        amap_id="B000A816R6",
    )
    repeated_entries = [
        offline_eval_module._route_discovery_entry(
            case_id="budget-contract",
            plan_id="plan",
            from_segment_id=f"from-{index}",
            to_segment_id=f"to-{index}",
            from_poi=from_poi,
            to_poi=to_poi,
            mode="transit",
            route_budget=budget,
            uses_offline_mock_amap=False,
        )
        for index in range(9)
    ]
    summary = offline_eval_module._summarize_route_discovery(repeated_entries)

    assert (
        offline_eval_module._recorded_route_replay_failure(
            from_poi,
            to_poi,
            dry_route_discovery=True,
        )
        == "dry_route_discovery_no_replay"
    )
    assert summary["captureEligiblePairs"]
    assert summary["routeBudget"]["exceeded"] is True
    assert summary["routeBudget"]["scopes"][0]["actualRequestCount"] == 9
    assert summary["routeBudget"]["scopes"][0]["actualRequestCountExceeded"] is True
    assert summary["captureReady"] is False
    assert summary["networkCalls"] == 0
    assert budget.used_route == 0
    assert budget.used_total_external == 0
    assert "recordedProviderEvidence" not in json.dumps(summary, ensure_ascii=False)
    assert "recordedAmapReplay" not in json.dumps(summary, ensure_ascii=False)
    assert "externalCaptureSession" not in json.dumps(summary, ensure_ascii=False)


def test_dry_route_discovery_cli_keeps_a_failed_eval_nonzero(monkeypatch):
    monkeypatch.setattr(
        offline_eval_module,
        "run_offline_eval",
        lambda *_args, **_kwargs: {"summary": {"failed": 4}},
    )
    monkeypatch.setattr(sys, "argv", ["run_offline.py", "--dry-route-discovery"])

    assert offline_eval_module.main() == 1


def test_agent_offline_eval_runner_covers_p0_harness_cases(monkeypatch):
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "true")
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT", "3")
    result = run_offline_eval()

    summary = result["summary"]
    case_ids = {item["id"] for item in result["cases"]}
    assert case_ids == {
        "beijing_university_every_night_candidate_first",
        "beijing_university_night_candidate_first",
        "creative_portfolio_search_profile_recorded",
        "react_controller_staged_draft",
        "simple_open_guide_grounded_continuation",
    }
    assert summary["total"] == 5
    assert summary["passed"] == 5
    assert summary["failed"] == 0
    assert summary["passRate"] == 1.0
    assert summary["passAtK"] == 1.0
    assert summary["repeat"] == 1
    assert summary["traceReplayFailures"] == 0
    assert summary["fakeOrNonAmapRouteAnchorCount"] == 0
    case_by_id = {item["id"]: item for item in result["cases"]}
    for item in result["cases"]:
        assert item["passed"] is True
        assert "recordedProviderEvidence" not in json.dumps(item, ensure_ascii=False)
    for case_id in {
        "beijing_university_every_night_candidate_first",
        "beijing_university_night_candidate_first",
    }:
        proposal_case = case_by_id[case_id]
        assert proposal_case["proposalVisibleCount"] == 1
        assert proposal_case["proposalAdoptionReadyCount"] == 1
        assert proposal_case["proposalPersistedSelectionCapabilityCount"] == 1
        assert proposal_case["proposalRouteProviderAttemptCount"] == 2
        assert proposal_case["proposalRouteCoverageComplete"] is True
        assert proposal_case["proposalRouteEvidenceFingerprintBound"] is True
        assert proposal_case["unexpectedWrite"] is False
    assert os.environ["AGENT_CREATIVE_PORTFOLIO_ENABLED"] == "true"
    assert os.environ["AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT"] == "3"
    get_settings.cache_clear()
    assert get_settings().agent_creative_portfolio_enabled is True
    assert get_settings().agent_creative_portfolio_target_count == 3


def test_simple_open_guide_grounded_continuation_passes_with_required_profile_case(tmp_path):
    cases_dir = tmp_path / "simple-open-guide-cases"
    cases_dir.mkdir()

    guide_case = CASES_DIR / "simple_open_guide_grounded_continuation.json"
    required_profile_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    shutil.copyfile(guide_case, cases_dir / guide_case.name)
    shutil.copyfile(required_profile_case, cases_dir / required_profile_case.name)

    result = run_offline_eval(cases_dir=cases_dir)

    case_by_id = {item["id"]: item for item in result["cases"]}
    assert "creative_portfolio_search_profile_recorded" in case_by_id
    guide_result = case_by_id["simple_open_guide_grounded_continuation"]
    assert guide_result["passed"] is True, guide_result.get("failureReason")
    assert guide_result["realExternalCallLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
    }


def test_dry_route_discovery_records_mock_requests_without_making_them_capturable():
    result = run_offline_eval(dry_route_discovery=True)

    assert result["summary"]["passed"] == 1
    assert result["summary"]["failed"] == 4
    case_by_id = {item["id"]: item for item in result["cases"]}
    assert case_by_id["creative_portfolio_search_profile_recorded"]["passed"] is True
    for case_id in {
        "beijing_university_every_night_candidate_first",
        "beijing_university_night_candidate_first",
        "react_controller_staged_draft",
        "simple_open_guide_grounded_continuation",
    }:
        assert case_by_id[case_id]["passed"] is False
    expected_pair_counts = {
        # The fixed route budget is scheduled coverage-first across days, so
        # both two-day Simple directions expose one dry request per day instead
        # of spending the first request only on Day 1.
        "beijing_university_every_night_candidate_first": 2,
        "beijing_university_night_candidate_first": 2,
        "creative_portfolio_search_profile_recorded": 4,
        "react_controller_staged_draft": 1,
    }
    for case_id, expected_count in expected_pair_counts.items():
        discovery = case_by_id[case_id]["recordedRouteDiscovery"]
        assert discovery["mode"] == "dry_route_discovery"
        assert discovery["networkCalls"] == 0
        assert len(discovery["actualRouteRequests"]) == expected_count
        assert len(discovery["uniqueRoutePairs"]) == expected_count
        assert discovery["captureEligiblePairs"] == []
        assert discovery["captureReady"] is False
        assert "offline_mock_amap_poi_provenance" in discovery["captureBlockedReasons"]
        assert all(item["captureEligible"] is False for item in discovery["actualRouteRequests"])
        assert "recordedProviderEvidence" not in json.dumps(discovery, ensure_ascii=False)


def test_capture_semantic_trace_never_upgrades_mock_identity_to_a_capture_query():
    result = run_offline_eval(
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )

    case_by_id = {item["id"]: item for item in result["cases"]}
    legacy_case_ids = {
        "beijing_university_every_night_candidate_first",
        "beijing_university_night_candidate_first",
        "react_controller_staged_draft",
    }
    for case_id in legacy_case_ids:
        trace = case_by_id[case_id]["recordedCaptureSemanticTrace"]
        assert trace["profileExecutionStatus"] == "not_executed"
        assert trace["profileRuns"] == []
        assert trace["identityCapturePrerequisite"] == "missing_production_search_profile"

    creative_trace = case_by_id["creative_portfolio_search_profile_recorded"]["recordedCaptureSemanticTrace"]
    assert creative_trace["profileExecutionStatus"] == "executed"
    assert creative_trace["profileRuns"]
    assert creative_trace["identityCapturePrerequisite"] == "production_search_profile_executed"
    assert all("fromAmapId" not in item and "toAmapId" not in item for item in creative_trace["logicalRouteTopology"])
    assert "B0ARTMUSEUM1" not in json.dumps(creative_trace, ensure_ascii=False)

    manifest = build_capture_preflight_manifest(
        runtime_evidence=result,
    )
    assert manifest["captureState"] == "preflight_blocked"
    assert manifest["terminalStatus"] == "STOPPED_AT_RECORDED_CAPTURE_GAP"
    assert manifest["allowlist"] == []
    assert manifest["externalCaptureSession"] == {
        "id": None,
        "consumed": False,
        "recordedFixtureCaptureUsed": 0,
        "externalPlaceCalls": 0,
        "externalRouteCalls": 0,
        "ledgerDelta": 0,
    }
    assert "AMAP_A_UNIV_MAIN" not in json.dumps(manifest, ensure_ascii=False)


def test_dedicated_capture_trace_uses_persisted_server_initial_plan_without_identity_inference(tmp_path):
    cases_dir = tmp_path / "fixed-goal-case"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    dedicated_payload = json.loads(dedicated_case.read_text(encoding="utf-8"))
    dedicated_payload["recordedAmapReplayFixture"] = "dry-mode-must-not-read-or-project-this-fixture.json"
    (cases_dir / dedicated_case.name).write_text(
        json.dumps(dedicated_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )

    assert result["summary"]["total"] == 1
    assert result["summary"]["passed"] == 0
    assert result["summary"]["failed"] == 1
    assert result["summary"]["traceReplayFailures"] == 0
    assert result["summary"]["fakeOrNonAmapRouteAnchorCount"] == 0
    case = result["cases"][0]
    rendered_case = json.dumps(case, ensure_ascii=False)
    assert "recordedAmapReplay" not in case
    assert "recordedProviderEvidence" not in rendered_case
    assert "dry-mode-must-not-read-or-project-this-fixture.json" not in rendered_case
    assert "externalCaptureSession" not in rendered_case
    trace = case["recordedCaptureSemanticTrace"]
    assert trace["profileExecutionStatus"] == "executed"
    assert trace["logicalPlanSource"] == "persisted_server_initial_plan"
    assert trace["logicalSlots"]
    assert trace["logicalRouteTopologyErrors"] == []
    assert all(slot["profileBindingStatus"] == "matched" for slot in trace["logicalSlots"])
    profile_scopes = {
        (
            profile["scope"]["briefId"],
            profile["scope"]["poolId"],
            profile["scope"]["planningSlotId"],
            profile["scope"]["dayNumber"],
        )
        for profile in trace["profileRuns"]
    }
    for slot in trace["logicalSlots"]:
        assert (
            slot["briefId"],
            slot["poolId"],
            slot["planningSlotId"],
            slot["dayNumber"],
        ) in profile_scopes
    route_anchors_by_day = {}
    for slot in trace["logicalSlots"]:
        if slot["routeAnchor"]:
            route_anchors_by_day.setdefault((slot["briefId"], slot["dayNumber"]), []).append(slot)
    assert route_anchors_by_day
    assert len(trace["logicalRouteTopology"]) == sum(max(0, len(slots) - 1) for slots in route_anchors_by_day.values())
    assert all(edge["mode"] == "" for edge in trace["logicalRouteTopology"])
    assert all(
        edge["modeReason"] == "server_initial_plan_route_anchor_adjacency" for edge in trace["logicalRouteTopology"]
    )
    assert trace["logicalRouteBudget"] == {
        "plannedAdjacentPairCount": len(trace["logicalRouteTopology"]),
        "actualLedgerKnown": False,
        "state": "not_instantiated",
        "reason": "portfolio_staging_not_reached",
        "scopes": [],
    }
    assert trace["logicalRouteAuthorization"] == {
        "status": "ready",
        "preferredMode": "transit",
        "modeSource": "request_intent_contract.route_decision_contract",
        "contractFingerprint": trace["logicalRouteAuthorization"]["contractFingerprint"],
        "budgetState": "not_derived",
        "budgetReason": "canonical_adjacent_pairs_not_materialized",
    }
    assert len(trace["logicalRouteAuthorization"]["contractFingerprint"]) == 64
    assert case["recordedRouteDiscovery"]["routeBudget"]["known"] is False
    assert case["recordedRouteDiscovery"]["actualRouteRequests"] == []
    observed_adcode = trace["observedAmapAdcode"]
    assert observed_adcode["status"] == "observed"
    assert observed_adcode["adcode"] == "110000"
    assert observed_adcode["source"] == "server_observed_amap_request_param"
    assert observed_adcode["observedRequestCount"] > 0
    rendered = json.dumps(trace, ensure_ascii=False)
    assert "amapId" not in rendered
    assert "fromAmapId" not in rendered
    assert "toAmapId" not in rendered
    assert trace["legacyMockAmapPresent"] is False


def test_fixed_goal_place_request_semantics_do_not_depend_on_audit_identity():
    """A fresh audit/root must not rotate the fixed Goal's provider requests."""

    cases_dir = CASES_DIR / "fixed_creative_portfolio_goal_recorded"

    def semantic_projection(runtime_evidence):
        trace = runtime_evidence["cases"][0]["recordedCaptureSemanticTrace"]
        manifest = build_capture_preflight_manifest(
            runtime_evidence=runtime_evidence,
            cases_dir=cases_dir,
        )
        profiles = []
        for profile in trace["profileRuns"]:
            scope = profile["scope"]
            profiles.append(
                {
                    "scope": {key: scope[key] for key in ("briefId", "poolId", "planningSlotId", "dayNumber")},
                    "semanticRole": profile["semanticRole"],
                    "queryPlans": [
                        {
                            key: plan[key]
                            for key in (
                                "anchorPolicy",
                                "category",
                                "city",
                                "endpoint",
                                "keyword",
                                "mode",
                                "providerCategoryKey",
                                "radiusMeters",
                                "resultLimit",
                            )
                        }
                        for plan in profile["queryPlans"]
                    ],
                }
            )
        requests = []
        for ordinal, request in enumerate(
            manifest["phase1PlaceRequestManifest"]["requests"],
            start=1,
        ):
            scope = request["profileOccurrence"]
            receipt = request["budgetReceipt"]
            requests.append(
                {
                    "ordinal": ordinal,
                    "endpoint": request["endpoint"],
                    "sanitizedParams": request["sanitizedParams"],
                    "scope": {key: scope[key] for key in ("briefId", "poolId", "planningSlotId", "dayNumber")},
                    "budgetReceipt": {
                        key: receipt[key] for key in ("acquired", "acquisitionOrdinal", "endpoint", "fetchOrdinal")
                    },
                }
            )
        return {"profiles": profiles, "requests": requests}

    first = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    second = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )

    assert first["summary"]["failed"] == second["summary"]["failed"] == 1
    assert (
        first["cases"][0]["recordedCaptureSemanticTrace"]["sourceFingerprint"]
        == second["cases"][0]["recordedCaptureSemanticTrace"]["sourceFingerprint"]
    )
    assert semantic_projection(first) == semantic_projection(second)

    manifest = build_capture_preflight_manifest(
        runtime_evidence=first,
        cases_dir=cases_dir,
    )
    assert manifest["captureState"] == "ready_for_place_identity_capture"
    assert manifest["terminalStatus"] is None
    assert [item["caseId"] for item in manifest["cases"]] == ["creative_portfolio_search_profile_recorded"]
    manifest_case = manifest["cases"][0]
    assert manifest_case["logicalPlanSource"] == "persisted_server_initial_plan"
    assert manifest_case["persistedInitialPlanStatus"] == "observed"
    assert manifest_case["adcode"] == "110000"
    assert manifest_case["adcodeSource"] == "server_observed_amap_request_param"
    assert manifest_case["semanticSlots"]
    assert all(
        slot["briefId"] and slot["poolId"] and slot["planningSlotId"] and slot["dayNumber"] > 0
        for slot in manifest_case["semanticSlots"]
    )
    assert all(
        edge["briefId"] and edge["dayNumber"] > 0 and edge["fromLogicalNode"] and edge["toLogicalNode"]
        for edge in manifest_case["logicalRouteTopology"]
    )
    assert manifest_case["placeIdentityCaptureEligible"] is True
    assert "route_authorization_not_ready" not in manifest_case["captureBlockedReasons"]
    assert "route_budget_not_derived" not in manifest_case["captureBlockedReasons"]
    assert manifest_case["deferredRoutePrerequisites"] == ["route_budget_not_derived"]
    assert "capture_adcode_not_observed" not in manifest_case["captureBlockedReasons"]


def test_night_view_query_progress_is_semantic_exactly_once_and_scope_isolated():
    """A persistent Portfolio frontier, not a session-seeded cache, owns night progress."""

    from src.services.creative_exploration_frontier_service import (
        CreativeExplorationFrontierService,
    )

    scope = {
        "tenantId": "tenant-a",
        "userId": "user-a",
        "planningRoot": "planning-root-a",
        "rootPortfolioId": "portfolio-a",
        "briefId": "brief-night",
        "poolId": "pool-night",
        "planningSlotId": "slot-night",
        "dayNumber": 1,
        "timeWindow": "19:00-21:00",
    }
    semantic_inputs = {
        "goal": {
            "rawNeed": "公共城市夜景",
            "intentType": "night_view",
            "session_id": "audit-session-a",
            "turnId": "audit-turn-a",
        },
        "city": " 北京 ",
        "adcode": "110000",
        "experienceSpec": {
            "experienceFamily": "public_city_view",
            "evidencePolicy": {
                "minimumEvidence": "amap_place_search",
                "random_seed": "audit-seed-a",
                "timestamp": "2026-08-14T00:00:00Z",
            },
        },
        "briefPoolSlotDayTime": {
            "briefId": "brief-night",
            "poolId": "pool-night",
            "planningSlotId": "slot-night",
            "dayNumber": 1,
            "timeWindow": "19:00-21:00",
        },
        "routeContract": {"fingerprint": "f" * 64, "sourceAssistantTurnId": "audit-turn-a"},
        "candidateHints": ["  城市广场  ", "城市天际线", "城市广场"],
    }
    semantic_fingerprint = CreativeExplorationFrontierService.night_view_semantic_fingerprint(**semantic_inputs)
    assert semantic_fingerprint == CreativeExplorationFrontierService.night_view_semantic_fingerprint(
        **{
            **semantic_inputs,
            "city": "北京",
            "goal": {
                "rawNeed": "公共城市夜景",
                "intentType": "night_view",
                "session_id": "audit-session-b",
                "turnId": "audit-turn-b",
            },
            "experienceSpec": {
                "experienceFamily": "public_city_view",
                "evidencePolicy": {
                    "minimumEvidence": "amap_place_search",
                    "random_seed": "audit-seed-b",
                    "timestamp": "2030-01-01T00:00:00Z",
                },
            },
            "routeContract": {"fingerprint": "f" * 64, "sourceAssistantTurnId": "audit-turn-b"},
            "candidateHints": ["城市天际线", "城市广场", "\u3000城市广场"],
        }
    ), "semanticFingerprint must ignore whitespace, Unicode-equivalent hint order, and audit identities"

    frontier_service = CreativeExplorationFrontierService()
    frontier = frontier_service.initial(
        planning_root_id=scope["planningRoot"],
        portfolio_id=scope["rootPortfolioId"],
        fingerprint="request-contract-a",
    )
    queries = [
        {"queryPlanId": "night-1", "endpoint": "place/text", "keyword": "城市广场"},
        {"queryPlanId": "night-2", "endpoint": "place/text", "keyword": "城市天际线"},
    ]
    first = CreativeExplorationFrontierService.claim_night_view_query(
        frontier,
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-a",
    )
    assert first["status"] == "CLAIMED", "the existing frontier has no exact night query cursor/claim ledger"
    assert first["queryCursor"] == 0
    assert first["providerCallAllowed"] is True

    replay = CreativeExplorationFrontierService.claim_night_view_query(
        first["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-a",
    )
    contender = CreativeExplorationFrontierService.claim_night_view_query(
        first["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-b",
    )
    assert replay["status"] == "REPLAY"
    assert replay["queryFingerprint"] == first["queryFingerprint"]
    assert replay["queryCursor"] == 0
    assert contender["status"] == "CLAIM_IN_FLIGHT"
    assert contender["providerCallAllowed"] is False

    failed = CreativeExplorationFrontierService.complete_night_view_query(
        first["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-a",
        provider_completed=False,
    )
    assert failed["status"] == "FAILED_NO_PROGRESS"
    assert failed["queryCursor"] == 0
    assert failed["executedQueryFingerprints"] == []
    failed_replay = CreativeExplorationFrontierService.claim_night_view_query(
        failed["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-a",
    )
    assert failed_replay["status"] == "REPLAY"
    assert failed_replay["queryFingerprint"] == first["queryFingerprint"]
    assert failed_replay["providerCallAllowed"] is False

    second_attempt = CreativeExplorationFrontierService.claim_night_view_query(
        failed["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-b",
    )
    assert second_attempt["status"] == "CLAIMED"
    assert second_attempt["queryFingerprint"] == first["queryFingerprint"]
    completed_first = CreativeExplorationFrontierService.complete_night_view_query(
        second_attempt["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-b",
        provider_completed=True,
        provider_receipt_fingerprint="a" * 64,
    )
    assert completed_first["status"] == "COMPLETED"
    assert completed_first["queryCursor"] == 1
    assert completed_first["executedQueryFingerprints"] == [first["queryFingerprint"]]

    next_attempt = CreativeExplorationFrontierService.claim_night_view_query(
        completed_first["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-c",
    )
    assert next_attempt["status"] == "CLAIMED"
    assert next_attempt["queryFingerprint"] != first["queryFingerprint"]
    completed_all = CreativeExplorationFrontierService.complete_night_view_query(
        next_attempt["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-c",
        provider_completed=True,
        provider_receipt_fingerprint="b" * 64,
    )
    exhausted = CreativeExplorationFrontierService.claim_night_view_query(
        completed_all["frontier"],
        scope=scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-d",
    )
    assert exhausted["status"] == "NO_PROGRESS"
    assert exhausted["providerCallAllowed"] is False
    assert exhausted["queryCursor"] == 2
    assert exhausted["executedQueryFingerprints"] == [
        first["queryFingerprint"],
        next_attempt["queryFingerprint"],
    ]

    other_user_scope = {**scope, "userId": "user-b"}
    other_user = CreativeExplorationFrontierService.claim_night_view_query(
        completed_all["frontier"],
        scope=other_user_scope,
        semantic_fingerprint=semantic_fingerprint,
        queries=queries,
        attempt_identity="attempt-user-b",
    )
    assert other_user["status"] == "CLAIMED"
    assert other_user["queryFingerprint"] == first["queryFingerprint"]
    assert other_user["queryCursor"] == 0


def test_night_view_progress_claims_before_fake_provider_and_persists_only_completed_receipts():
    """The actual search boundary uses the existing Portfolio CAS, never a hint cache."""

    import sqlite3
    from types import SimpleNamespace

    from fastapi import HTTPException

    from src.core.config import get_settings
    from src.core.database import sqlite_path_from_url
    from src.models.poi_intent import PoiIntent
    from src.models.poi_search_profile import ExperienceSemanticInput
    from src.services.creative_exploration_frontier_service import (
        CreativeExplorationFrontierService,
    )
    from src.services.creative_planning_models import PlanPortfolio
    from src.services.experience_search_profile_compiler import ExperienceSearchProfileCompiler
    from src.services.itinerary_service import ItineraryService
    from src.services.plan_portfolio_store import PlanPortfolioStore
    from src.services.route_insertion_scorer import RouteInsertionScorer

    connection = sqlite3.connect(sqlite_path_from_url(get_settings().database_url))
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio-night-progress",
        sessionId="session-night-progress",
        sourceUserTurnId="planning-root-night-progress",
        sourceAssistantTurnId="assistant-night-progress",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])
    frontier = CreativeExplorationFrontierService().initial(
        planning_root_id=portfolio.source_user_turn_id,
        portfolio_id=portfolio.portfolio_id,
        fingerprint=portfolio.request_contract_fingerprint,
    )
    store.update_exploration_frontier(
        portfolio_id=portfolio.portfolio_id,
        frontier=frontier,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )
    route_contract = RouteInsertionScorer.build_route_decision_contract(
        source="night_view_progress_test",
        provenance={"issuer": "integration-test", "transportMode": "transit"},
        detour_tolerance={"maxGeneralizedCostDelta": 35, "maxDetourRatio": 1.5},
        mobility_profile={
            "source": "integration-test",
            "walkingPenaltyMinutesPerKm": 2,
            "transferPenaltyMinutes": 6,
            "waitTimeMultiplier": 1,
            "riskPenaltyMultiplier": 1,
        },
    )
    assert route_contract is not None
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-night-progress",
            briefId="brief-night-progress",
            planningSlotId="slot-night-progress",
            dayNumber=1,
            requirementLevel="required",
            rawNeed="公共城市夜景",
            intentType="night_view",
            candidateHints=["城市广场", "城市天际线"],
            routeContext={"adcode": "110000", "routeDecisionContract": route_contract},
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )
    intent = PoiIntent(
        raw_need="公共城市夜景",
        city="北京",
        day_number=1,
        time_window="19:00-21:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        candidate_hints=["城市广场", "城市天际线"],
        search_profile=profile,
    )
    scope = {
        "tenantId": "default",
        "userId": "user-night-progress",
        "planningRoot": portfolio.source_user_turn_id,
        "rootPortfolioId": portfolio.portfolio_id,
    }

    class ClaimedMapService:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.expected_attempt = "night-attempt-1"
            self.expected_cursor = 0

        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            entries = store.summary(portfolio_id=portfolio.portfolio_id)["creativeExplorationFrontier"][
                "nightViewQueryProgress"
            ]["entries"]
            assert len(entries) == 1
            entry = next(iter(entries.values()))
            assert entry["inFlightAttemptIdentity"] == self.expected_attempt
            assert entry["queryCursor"] == self.expected_cursor
            self.calls.append(keyword)
            return SimpleNamespace(provider_name="amap-place-search", cache_hit=False, pois=[])

        def search_nearby(self, *args, **kwargs):
            raise AssertionError("the selected exact text plan must not be widened")

    map_service = ClaimedMapService()

    def configure(service, attempt):
        service.configure_night_view_query_progress(
            scope_identity=scope,
            attempt_identity=attempt,
            claim_query=lambda **kwargs: store.claim_night_view_query(
                portfolio_id=portfolio.portfolio_id,
                expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
                **kwargs,
            ),
            complete_query=lambda **kwargs: store.complete_night_view_query(
                portfolio_id=portfolio.portfolio_id,
                expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
                **kwargs,
            ),
        )

    first_service = ItineraryService(connection, map_poi_service=map_service)
    configure(first_service, "night-attempt-1")
    _candidates, first_state = first_service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )
    assert first_state == "ok"
    assert len(map_service.calls) == 1
    first_stats = first_service._last_candidate_collection_stats["nightViewQueryProgress"]
    assert first_stats["status"] == "CLAIMED"
    persisted = store.summary(portfolio_id=portfolio.portfolio_id)["creativeExplorationFrontier"]
    persisted_entry = next(iter(persisted["nightViewQueryProgress"]["entries"].values()))
    assert persisted_entry["queryCursor"] == 1
    assert len(persisted_entry["executedQueryFingerprints"]) == 1
    assert persisted_entry["attempts"]["night-attempt-1"]["receiptFingerprint"]
    replay_row_before = connection.execute(
        "SELECT summary_json, updated_at FROM agent_plan_portfolios WHERE id = ?",
        (portfolio.portfolio_id,),
    ).fetchone()

    replay_service = ItineraryService(connection, map_poi_service=map_service)
    configure(replay_service, "night-attempt-1")
    _candidates, replay_state = replay_service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )
    assert replay_state == "no_progress"
    assert len(map_service.calls) == 1
    assert replay_service._last_candidate_collection_stats["nightViewQueryProgress"]["status"] == "REPLAY"
    replay_row_after = connection.execute(
        "SELECT summary_json, updated_at FROM agent_plan_portfolios WHERE id = ?",
        (portfolio.portfolio_id,),
    ).fetchone()
    assert tuple(replay_row_after) == tuple(replay_row_before)

    second_service = ItineraryService(connection, map_poi_service=map_service)
    map_service.expected_attempt = "night-attempt-2"
    map_service.expected_cursor = 1
    configure(second_service, "night-attempt-2")
    _candidates, second_state = second_service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )
    assert second_state == "ok"
    assert len(map_service.calls) == 2
    assert map_service.calls[0] != map_service.calls[1]

    exhausted_state = None
    # Do not assert a test-owned query count: each attempt consumes the next
    # production-compiled plan until the exact set is exhausted. The final
    # authorized attempt must stop before the fake Provider and without a row
    # write.
    for attempt_index in range(3, len(profile.queryPlans) + 3):
        map_service.expected_attempt = f"night-attempt-{attempt_index}"
        map_service.expected_cursor = len(map_service.calls)
        row_before_attempt = connection.execute(
            "SELECT summary_json, updated_at FROM agent_plan_portfolios WHERE id = ?",
            (portfolio.portfolio_id,),
        ).fetchone()
        calls_before_attempt = len(map_service.calls)
        next_service = ItineraryService(connection, map_poi_service=map_service)
        configure(next_service, f"night-attempt-{attempt_index}")
        _candidates, state = next_service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )
        if state == "no_progress":
            exhausted_state = state
            row_after_attempt = connection.execute(
                "SELECT summary_json, updated_at FROM agent_plan_portfolios WHERE id = ?",
                (portfolio.portfolio_id,),
            ).fetchone()
            assert len(map_service.calls) == calls_before_attempt
            assert tuple(row_after_attempt) == tuple(row_before_attempt)
            break
        assert state == "ok"
    assert exhausted_state == "no_progress"

    class FailingMapService:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, *args, **kwargs):
            self.calls += 1
            raise HTTPException(status_code=503, detail="safe_test_provider_failure")

        def search_nearby(self, *args, **kwargs):
            raise AssertionError("no fallback request is authorized")

    failing_scope = {**scope, "userId": "user-night-failure"}
    failing_map = FailingMapService()
    failing_service = ItineraryService(connection, map_poi_service=failing_map)
    failing_service.configure_night_view_query_progress(
        scope_identity=failing_scope,
        attempt_identity="night-failure-1",
        claim_query=lambda **kwargs: store.claim_night_view_query(
            portfolio_id=portfolio.portfolio_id,
            expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
            **kwargs,
        ),
        complete_query=lambda **kwargs: store.complete_night_view_query(
            portfolio_id=portfolio.portfolio_id,
            expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
            **kwargs,
        ),
    )
    _candidates, failure_state = failing_service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )
    assert failure_state == "provider_down"
    assert failing_map.calls == 1
    failure_entry = next(
        entry
        for entry in store.summary(portfolio_id=portfolio.portfolio_id)["creativeExplorationFrontier"][
            "nightViewQueryProgress"
        ]["entries"].values()
        if entry["scope"]["userId"] == "user-night-failure"
    )
    assert failure_entry["queryCursor"] == 0
    assert failure_entry["executedQueryFingerprints"] == []
    assert failure_entry["attempts"]["night-failure-1"]["state"] == "failed_before_progress"

    concurrency_scope = {
        **scope,
        "userId": "user-night-concurrent",
        "briefId": "brief-night-concurrent",
        "poolId": "pool-night-concurrent",
        "planningSlotId": "slot-night-concurrent",
        "dayNumber": 1,
        "timeWindow": "19:00-21:00",
    }
    concurrency_semantic = CreativeExplorationFrontierService.night_view_semantic_fingerprint(
        goal={"rawNeed": "公共城市夜景", "intentType": "night_view"},
        city="beijing",
        adcode="110000",
        experienceSpec={"experienceFamily": "public_city_view"},
        briefPoolSlotDayTime={
            key: concurrency_scope[key] for key in ("briefId", "poolId", "planningSlotId", "dayNumber", "timeWindow")
        },
        routeContract={"fingerprint": "e" * 64},
        candidateHints=["plaza"],
    )
    concurrency_queries = [{"providerPlanId": "night-concurrent-1", "endpoint": "place/text"}]
    first_concurrent_claim = store.claim_night_view_query(
        portfolio_id=portfolio.portfolio_id,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
        scope=concurrency_scope,
        semantic_fingerprint=concurrency_semantic,
        queries=concurrency_queries,
        attempt_identity="night-concurrent-1",
    )
    second_concurrent_claim = store.claim_night_view_query(
        portfolio_id=portfolio.portfolio_id,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
        scope=concurrency_scope,
        semantic_fingerprint=concurrency_semantic,
        queries=concurrency_queries,
        attempt_identity="night-concurrent-2",
    )
    isolated_user_claim = store.claim_night_view_query(
        portfolio_id=portfolio.portfolio_id,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
        scope={**concurrency_scope, "userId": "user-night-concurrent-b"},
        semantic_fingerprint=concurrency_semantic,
        queries=concurrency_queries,
        attempt_identity="night-concurrent-user-b",
    )
    assert first_concurrent_claim["status"] == "CLAIMED"
    assert second_concurrent_claim["status"] == "CLAIM_IN_FLIGHT"
    assert isolated_user_claim["status"] == "CLAIMED"
    assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_night_view_candidate_hint_cache_is_canonical_user_isolated_and_never_a_cursor():
    """The shared cache may retain hints, never cross-user progress or a rotation seed."""

    import sqlite3
    from types import SimpleNamespace

    from src.services.agent_service import (
        AgentService,
        _CANDIDATE_HINT_CACHE,
        _CANDIDATE_HINT_CACHE_LOCK,
    )

    service = AgentService(sqlite3.connect(":memory:"))
    pool = SimpleNamespace(
        pool_id="night-cache-pool",
        city="北京",
        intent_type="night_view",
        raw_need="公共城市夜景",
    )
    plan = SimpleNamespace(intent_pools=[pool])
    semantic_context = {
        "candidateHintCacheScope": {"tenantId": "tenant-night", "userId": "user-night-a"},
        "selectedCity": "北京",
        "effectiveUserMessage": "北京高校两日游，每晚公共城市夜景",
        "requestIntentContract": {
            "nightView": True,
            "dayCount": 2,
            "sessionId": "embedded-session-a",
            "sourceUserTurnId": "embedded-turn-a",
            "turn_id": "embedded-turn-a",
            "randomSeed": "seed-a",
            "timestamp": "2026-08-14T00:00:00Z",
        },
        "providerModel": "offline-test-model",
        "candidateHintProviderConfigFingerprint": "a" * 64,
        "sessionId": "session-a",
        "userTurnId": "turn-a",
    }
    changed_audit_context = {
        **semantic_context,
        "sessionId": "session-b",
        "userTurnId": "turn-b",
        "requestIntentContract": {
            "nightView": True,
            "dayCount": 2,
            "sessionId": "embedded-session-b",
            "sourceUserTurnId": "embedded-turn-b",
            "turn_id": "embedded-turn-b",
            "randomSeed": "seed-b",
            "timestamp": "2030-01-01T00:00:00Z",
        },
    }
    other_user_context = {
        **semantic_context,
        "candidateHintCacheScope": {"tenantId": "tenant-night", "userId": "user-night-b"},
    }
    changed_provider_config_context = {
        **semantic_context,
        "candidateHintProviderConfigFingerprint": "b" * 64,
    }
    unsealed_config_context = {
        key: value for key, value in semantic_context.items() if key != "candidateHintProviderConfigFingerprint"
    }

    key = service._candidate_hint_cache_key(pool, semantic_context)
    assert key is not None
    assert service._candidate_hint_cache_key(pool, changed_audit_context) == key
    assert service._candidate_hint_cache_key(pool, other_user_context) != key
    assert service._candidate_hint_cache_key(pool, changed_provider_config_context) != key
    unsealed_config_key = service._candidate_hint_cache_key(pool, unsealed_config_context)
    assert unsealed_config_key is not None
    original_settings = service.settings
    provider_had_base_url = hasattr(service.provider, "base_url")
    original_provider_base_url = getattr(service.provider, "base_url", None)
    service.settings = SimpleNamespace(
        agent_experience_grounding_v2_mode=original_settings.agent_experience_grounding_v2_mode,
        agent_creative_output_quality_v2_mode=original_settings.agent_creative_output_quality_v2_mode,
        agent_soft_slot_draft_adoption_enabled=original_settings.agent_soft_slot_draft_adoption_enabled,
        provider_mode=original_settings.provider_mode,
        deepseek_base_url="https://cache-config-change.invalid/v1",
        deepseek_timeout_seconds=original_settings.deepseek_timeout_seconds,
        deepseek_tool_strict_mode=original_settings.deepseek_tool_strict_mode,
        deepseek_thinking_mode=original_settings.deepseek_thinking_mode,
        deepseek_reasoning_effort=original_settings.deepseek_reasoning_effort,
    )
    try:
        service.provider.base_url = "https://cache-config-change.invalid/v1"
        assert service._candidate_hint_cache_key(pool, unsealed_config_context) != unsealed_config_key
    finally:
        if provider_had_base_url:
            service.provider.base_url = original_provider_base_url
        else:
            delattr(service.provider, "base_url")
        service.settings = original_settings

    with _CANDIDATE_HINT_CACHE_LOCK:
        _CANDIDATE_HINT_CACHE.clear()
    try:
        for unsafe_origin in (
            "https://user:password@cache-config.invalid/v1",
            "https://cache-config.invalid/v1?query=not-cache-data",
            "https://cache-config.invalid/v1#fragment",
            "https://cache-config.invalid:bad-port/v1",
        ):
            service.provider.base_url = unsafe_origin
            assert service._candidate_hint_cache_key(pool, unsealed_config_context) is None
        with _CANDIDATE_HINT_CACHE_LOCK:
            assert _CANDIDATE_HINT_CACHE == {}
    finally:
        if provider_had_base_url:
            service.provider.base_url = original_provider_base_url
        else:
            delattr(service.provider, "base_url")
    assert service._candidate_hint_cache_key(pool, {**semantic_context, "candidateHintCacheScope": {}}) is None

    with _CANDIDATE_HINT_CACHE_LOCK:
        _CANDIDATE_HINT_CACHE.clear()
    service._store_candidate_hints_cache(
        plan,
        semantic_context,
        {
            pool.pool_id: {
                "candidateHints": [" 城市天际线 ", "公共城市夜景", "城市天际线"],
                "hintPolicy": "must_not_be_cached",
                "queryCursor": 9,
                "nightHintRotationSeed": 123,
            }
        },
    )
    with _CANDIDATE_HINT_CACHE_LOCK:
        assert list(_CANDIDATE_HINT_CACHE) == [key]
        _created_at, cached_payload = _CANDIDATE_HINT_CACHE[key]
    assert cached_payload == {"candidateHintSet": ["公共城市夜景", "城市天际线"]}


def test_zero_network_route_https_transport_is_exact_and_bound_without_network(
    tmp_path,
    monkeypatch,
):
    """Z0R accepts only one exact, durably claimed Route HTTP sequence."""

    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from urllib.parse import parse_qs, urlsplit

    module_name = "backend.evals.recorded_fixture_route_capture_transport"
    assert importlib.util.find_spec(module_name) is not None, (
        "exact bounded AMap Route HTTPS transport contract is missing"
    )
    route_capture = import_module("backend.evals.recorded_fixture_route_capture")
    transport_module = import_module(module_name)

    lease = {
        "planningRoot": "root-z0r",
        "rootPortfolioId": "portfolio-z0r",
        "briefId": "brief-z0r",
        "dayNumber": 1,
        "planningSlotId": "slot-z0r",
        "candidatePhysicalId": "B000000002",
        "adjacentAnchorIds": ["anchor-z0r-a", "anchor-z0r-b"],
        "fromAmapId": "B000000001",
        "toAmapId": "B000000002",
        "mode": "transit",
        "routeContractFingerprint": "a" * 64,
        "reason": "preferred_adjacent",
        "condition": "preferred",
        "fromResponseSha256": "b" * 64,
        "toResponseSha256": "c" * 64,
        "providerRequest": {
            "method": "GET",
            "scheme": "https",
            "host": "restapi.amap.com",
            "path": "/v3/direction/transit/integrated",
            "params": {
                "origin": "116.1,39.1",
                "destination": "116.2,39.2",
                "city": "110000",
                "cityd": "110000",
                "strategy": "0",
            },
        },
    }
    lease["leaseFingerprint"] = canonical_sha256(lease)
    route_manifest = {
        "schemaVersion": "trip-recorded-exact-route-manifest-v1",
        "sourceBindings": {"sourceFingerprint": "d" * 64},
        "productionDryRouteTraceFingerprint": "e" * 64,
        "orderedRouteLeases": [lease],
        "pairCount": 1,
        "routeBudget": {"maxCalls": 1, "hardCap": 24},
        "conditionalWalkingCapturePolicy": {
            "status": "not_authorized_until_preferred_transit_unavailable",
            "routeCaptureAuthorized": False,
            "maxCalls": 0,
        },
    }
    route_manifest["routeManifestFingerprint"] = canonical_sha256(route_manifest)
    route_envelope = route_capture.build_zero_network_route_capture_envelope(route_manifest=route_manifest)
    root = tmp_path / "route-z0r-staging"
    root.mkdir()
    root_binding = route_capture._root_binding_fingerprint(root)
    now = datetime.now(timezone.utc)
    route_authorization = {
        "authorizationId": "route-auth-z0r-0001",
        "scope": "route_only",
        "sourceFingerprint": route_manifest["sourceBindings"]["sourceFingerprint"],
        "routeEnvelopeFingerprint": route_envelope["envelopeFingerprint"],
        "routeManifestFingerprint": route_manifest["routeManifestFingerprint"],
        "exactRouteRequestAllowlistFingerprint": route_envelope["exactRouteRequestAllowlist"]["allowlistFingerprint"],
        "maxCalls": 1,
        "issuedAt": (now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }
    network_authorization = {
        "schemaVersion": "trip-zero-network-route-network-authorization-v1",
        "networkAuthorizationId": "route-network-z0r-0001",
        "scope": "route_only",
        "transportKind": "amap_route_https",
        "routeAuthorizationId": route_authorization["authorizationId"],
        "routeAuthorizationFingerprint": canonical_sha256(route_authorization),
        "sourceFingerprint": route_authorization["sourceFingerprint"],
        "routeEnvelopeFingerprint": route_authorization["routeEnvelopeFingerprint"],
        "routeManifestFingerprint": route_authorization["routeManifestFingerprint"],
        "exactRouteRequestAllowlistFingerprint": route_authorization["exactRouteRequestAllowlistFingerprint"],
        "outboundRequestSequenceFingerprint": canonical_sha256(
            route_envelope["exactRouteRequestAllowlist"]["requests"]
        ),
        "maxCalls": 1,
        "sessionDirectoryName": "route-session-z0r-0001",
        "stagingRootBindingFingerprint": root_binding,
        "transportProfile": transport_module.ROUTE_TRANSPORT_PROFILE,
        "issuedAt": (now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }

    class StubResponse:
        def __init__(self, body, *, final_url=None, status=200, content_type="application/json"):
            self._body = body
            self._final_url = final_url
            self._status = status
            self.headers = {"Content-Type": content_type}
            self.read_limits = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self._status

        def geturl(self):
            return self._final_url

        def read(self, limit):
            self.read_limits.append(limit)
            return self._body

    class StubOpener:
        def __init__(self, response):
            self.response = response
            self.calls = []
            self.state_path = None

        def open(self, request, *, timeout):
            assert self.state_path is not None
            persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
            assert persisted["state"] == "consumed_in_progress"
            assert persisted["effectLedgerStatus"] == "transport_attempt_pending"
            assert persisted["attemptedRouteCalls"] == len(self.calls) + 1
            self.calls.append({"request": request, "timeout": timeout})
            if self.response._final_url is None:
                self.response._final_url = request.full_url
            return self.response

    provider_payload = {
        "status": "1",
        "infocode": "10000",
        "route": {"transits": [{"duration": "600", "distance": "900"}]},
    }
    opener = StubOpener(StubResponse(json.dumps(provider_payload).encode("utf-8")))
    opener.state_path = root / network_authorization["sessionDirectoryName"] / "route-session-state.json"
    monkeypatch.setattr(transport_module, "_build_hardened_opener", lambda: opener)
    transport = transport_module.AmapWebServiceRouteCaptureTransport(network_authorization=network_authorization)
    socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        result = route_capture.execute_zero_network_route_capture(
            envelope=route_envelope,
            route_manifest=route_manifest,
            route_authorization=route_authorization,
            network_authorization=network_authorization,
            staging_root=root,
            transport=transport,
            credential="route-credential-sentinel",
            now=now,
        )
    assert socket_trace["realExternalCalls"] == []
    assert result["status"] == "completed", result["reasonCode"]
    assert result["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    assert result["realTransportAttempts"] == result["realTransportCalls"] == 1
    assert result["stubTransportCalls"] == 1
    assert result["externalRouteCalls"] == 0
    assert result["realExternalCapture"] is False
    assert len(opener.calls) == 1
    observed = opener.calls[0]
    parsed = urlsplit(observed["request"].full_url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https",
        "restapi.amap.com",
        "/v3/direction/transit/integrated",
    )
    assert parse_qs(parsed.query)["key"] == ["route-credential-sentinel"]
    assert observed["request"].get_header("User-agent") == "trip-route-capture/1"
    assert observed["timeout"] == 5.0
    session_state_path = root / network_authorization["sessionDirectoryName"] / "route-session-state.json"
    persisted = json.loads(session_state_path.read_text(encoding="utf-8"))
    assert "route-credential-sentinel" not in json.dumps(persisted)
    bundle = json.loads(
        (root / network_authorization["sessionDirectoryName"] / "route-capture-quarantine.json").read_text(
            encoding="utf-8"
        )
    )
    assert bundle["recordedProvider"] == "AMap Web Service"
    assert "route-credential-sentinel" not in json.dumps(bundle)
    route_capture.validate_route_capture_quarantine(
        bundle=bundle,
        route_envelope=route_envelope,
    )

    replay_transport = transport_module.AmapWebServiceRouteCaptureTransport(network_authorization=network_authorization)
    with pytest.raises(route_capture.RouteCaptureError, match="route_envelope_already_consumed"):
        route_capture.execute_zero_network_route_capture(
            envelope=route_envelope,
            route_manifest=route_manifest,
            route_authorization=route_authorization,
            network_authorization=network_authorization,
            staging_root=root,
            transport=replay_transport,
            credential="route-credential-sentinel",
            now=now,
        )
    assert replay_transport.attempted_count == 0

    wrong_request = copy.deepcopy(route_envelope)
    wrong_request["exactRouteRequestAllowlist"]["requests"][0]["mode"] = "walking"
    with pytest.raises(route_capture.RouteCaptureError, match="tampered"):
        route_capture.validate_zero_network_route_capture_envelope(
            envelope=wrong_request,
            route_manifest=route_manifest,
        )

    safe_url = "https://restapi.amap.com/v3/direction/transit/integrated"
    with pytest.raises(transport_module.RealRouteTransportError) as unsuccessful:
        transport_module._read_response(
            StubResponse(
                json.dumps(
                    {
                        "status": "0",
                        "infocode": "10001",
                        "info": "route-secret-sentinel https://unsafe.invalid/?q=secret",
                    }
                ).encode("utf-8"),
                final_url=safe_url,
            ),
            request=route_envelope["exactRouteRequestAllowlist"]["requests"][0],
            original_url=safe_url,
        )
    assert unsuccessful.value.reason_code == "amap_route_response_unsuccessful"
    assert unsuccessful.value.provider_diagnostic == {
        "schemaVersion": "trip-amap-provider-diagnostic-v1",
        "provider": "amap_web_service",
        "status": "0",
        "infocode": "10001",
    }
    assert "route-secret-sentinel" not in str(unsuccessful.value)
    for malformed in (
        {"status": "0", "infocode": "too-long"},
        {"status": "0", "infocode": "１２３４５"},
        {"status": 0, "infocode": "10001"},
    ):
        with pytest.raises(transport_module.RealRouteTransportError) as malformed_error:
            transport_module._read_response(
                StubResponse(json.dumps(malformed).encode("utf-8"), final_url=safe_url),
                request=route_envelope["exactRouteRequestAllowlist"]["requests"][0],
                original_url=safe_url,
            )
        assert malformed_error.value.reason_code == "amap_route_response_unsuccessful"
        assert malformed_error.value.provider_diagnostic is None
    with pytest.raises(
        transport_module.RealRouteTransportError,
        match="route_transport_redirect_forbidden",
    ):
        transport_module._read_response(
            StubResponse(
                json.dumps(provider_payload).encode("utf-8"),
                final_url="https://redirect.invalid/blocked",
            ),
            request=route_envelope["exactRouteRequestAllowlist"]["requests"][0],
            original_url=safe_url,
        )


def test_phase1_place_request_manifest_binds_budgeted_low_level_fetches_and_fails_closed(
    tmp_path,
):
    cases_dir = tmp_path / "fixed-goal-phase1-place-manifest"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=result,
        cases_dir=cases_dir,
    )

    phase1 = manifest.get("phase1PlaceRequestManifest")
    assert isinstance(phase1, dict), "exact Phase-1 Place request manifest is missing"
    assert phase1["status"] == "ready", {
        "blockers": phase1["blockers"],
        "failureReason": result["cases"][0].get("failureReason"),
        "traceErrors": result["cases"][0].get("recordedCaptureSemanticTrace", {}).get("phase1PlaceRequestErrors"),
    }
    assert phase1["placeRequestClosureStatus"] == "phase1_exact_requests_frozen"
    assert phase1["placeIdentityCaptureEligible"] is True
    assert phase1["placeIdentityClosureComplete"] is False
    assert phase1["routePairFreezeEligible"] is False
    requests = phase1["requests"]
    assert phase1["requestCount"] == len(requests) > 0
    assert len(requests) >= 2
    assert sum(item["count"] for item in phase1["requestMultiset"]) == len(requests)
    assert {item["endpoint"] for item in requests} <= {"place/text", "place/around"}
    assert len(
        {
            (
                item["profileOccurrence"]["occurrenceFingerprint"],
                item["budgetReceipt"]["receiptFingerprint"],
                item["requestFingerprint"],
            )
            for item in requests
        }
    ) == len(requests)

    trace = result["cases"][0]["recordedCaptureSemanticTrace"]
    for profile_run in trace["profileRuns"]:
        scope = profile_run["scope"]
        consumer_input = profile_run["consumerAdmissionInput"]
        assert {
            "briefId": consumer_input["briefId"],
            "poolId": consumer_input["poolId"],
            "planningSlotId": consumer_input["planningSlotId"],
            "dayNumber": consumer_input["dayNumber"],
        } == scope
        assert consumer_input["family"] == profile_run["semanticRole"]["experienceFamily"]
        assert consumer_input["activityMode"] == profile_run["semanticRole"]["intentType"]
        assert consumer_input["requirementLevel"] == profile_run["semanticRole"]["requirementLevel"]
        policy = consumer_input.get("experienceSpecPolicy") or {}
        if policy:
            assert re.fullmatch(
                r"[0-9a-f]{64}",
                policy["specFingerprint"],
            )
            assert re.fullmatch(r"[0-9a-f]{64}", consumer_input["specFingerprint"])
            assert consumer_input.get("experienceSpecPolicyError") is None
        if profile_run["semanticRole"]["experienceFamily"] in {
            "meal",
            "public_city_view",
        }:
            assert policy["unresolvedDimensions"] == []
            assert policy["experienceFamilies"]
            assert policy["allowedDayNumbers"]
        assert (
            consumer_input["routeContext"]["routeDecisionContract"]["fingerprint"] == trace["routeContractFingerprint"]
        )
    explicit_meal_runs = [item for item in trace["profileRuns"] if item["semanticRole"]["experienceFamily"] == "meal"]
    assert {item["semanticRole"]["requirementLevel"] for item in explicit_meal_runs} == {"soft"}
    explicit_meal_profiles = {str(item["profileId"]): int(item["scope"]["dayNumber"]) for item in explicit_meal_runs}
    assert set(explicit_meal_profiles.values()) == {1, 2}
    requested_profile_ids = {str(item["profileOccurrence"]["profileId"]) for item in requests}
    requested_profile_scopes = [
        (
            next(
                profile["semanticRole"]["experienceFamily"]
                for profile in trace["profileRuns"]
                if profile["profileId"] == request["profileOccurrence"]["profileId"]
            ),
            int(request["profileOccurrence"]["dayNumber"]),
        )
        for request in requests
    ]
    requested_scope_counts = {
        f"{family}:day{day_number}": requested_profile_scopes.count((family, day_number))
        for family, day_number in set(requested_profile_scopes)
    }
    assert {
        day_number for profile_id, day_number in explicit_meal_profiles.items() if profile_id in requested_profile_ids
    } == {1, 2}, {
        "reason": "each explicit daily meal occurrence must retain one exact Place request before variants",
        "requestedProfileScopeCounts": requested_scope_counts,
    }
    meal_requests_by_profile = {
        profile_id: [request for request in requests if str(request["profileOccurrence"]["profileId"]) == profile_id]
        for profile_id in explicit_meal_profiles
    }
    assert all(meal_requests_by_profile.values())
    assert {
        explicit_meal_profiles[profile_id]: len(profile_requests)
        for profile_id, profile_requests in meal_requests_by_profile.items()
    } == {1: 1, 2: 1}, {
        "reason": "each explicit daily meal occurrence authorizes exactly one primary Place request",
        "requestedProfileScopeCounts": requested_scope_counts,
    }
    primary_ordinals = [
        min(request["budgetReceipt"]["acquisitionOrdinal"] for request in profile_requests)
        for profile_requests in meal_requests_by_profile.values()
    ]
    variant_ordinals = [
        request["budgetReceipt"]["acquisitionOrdinal"]
        for profile_requests in meal_requests_by_profile.values()
        for request in profile_requests
        if request["budgetReceipt"]["acquisitionOrdinal"]
        != min(item["budgetReceipt"]["acquisitionOrdinal"] for item in profile_requests)
    ]
    assert not variant_ordinals or max(primary_ordinals) < min(variant_ordinals), {
        "reason": "every explicit meal occurrence must receive its primary request before variants",
        "primaryOrdinals": primary_ordinals,
        "variantOrdinals": variant_ordinals,
    }
    text_allowed = {
        "keywords",
        "city",
        "citylimit",
        "offset",
        "page",
        "extensions",
        "output",
        "types",
    }
    around_allowed = text_allowed | {"location", "radius", "sortrule"}
    for request in requests:
        endpoint = request["endpoint"]
        params = request["sanitizedParams"]
        assert set(params) <= (text_allowed if endpoint == "place/text" else around_allowed)
        assert not (
            {str(key).casefold() for key in params}
            & {"key", "api_key", "apikey", "authorization", "cookie", "proxy", "token", "url"}
        )
        assert request["requestFingerprint"] == canonical_sha256({"endpoint": endpoint, "sanitizedParams": params})
        occurrence = request["profileOccurrence"]
        occurrence_material = dict(occurrence)
        occurrence_fingerprint = occurrence_material.pop("occurrenceFingerprint")
        assert occurrence_fingerprint == canonical_sha256(occurrence_material)
        assert all(
            occurrence.get(field)
            for field in (
                "profileId",
                "profileFingerprint",
                "executionFingerprint",
                "briefId",
                "poolId",
                "planningSlotId",
                "dayNumber",
            )
        )
        lineage = request["queryPlanLineage"]
        assert all(
            lineage.get(field)
            for field in (
                "sourcePlanId",
                "sourcePlanFingerprint",
                "providerPlanId",
                "providerPlanFingerprint",
                "queryScopeFingerprint",
            )
        )
        receipt = request["budgetReceipt"]
        assert receipt["acquired"] is True
        assert receipt["endpoint"] == endpoint
        assert receipt["requestFingerprint"] == request["requestFingerprint"]
        assert receipt["profileOccurrenceFingerprint"] == canonical_sha256(occurrence)
        assert receipt["queryPlanLineageFingerprint"] == canonical_sha256(lineage)
        assert receipt["queryScopeFingerprint"] == lineage["queryScopeFingerprint"]
        assert receipt["acquisitionOrdinal"] < receipt["fetchOrdinal"]
        assert receipt["after"]["usedCalls"] - receipt["before"]["usedCalls"] == 1
        assert receipt["after"]["newQueryCalls"] - receipt["before"]["newQueryCalls"] == 1
        endpoint_counter = "textSearchCalls" if endpoint == "place/text" else "aroundSearchCalls"
        assert receipt["after"][endpoint_counter] - receipt["before"][endpoint_counter] == 1
        receipt_material = dict(receipt)
        receipt_fingerprint = receipt_material.pop("receiptFingerprint")
        assert receipt_fingerprint == canonical_sha256(receipt_material)
        low_level_fetch = request["productionLowLevelFetch"]
        low_level_material = dict(low_level_fetch)
        low_level_fingerprint = low_level_material.pop("markerFingerprint")
        assert low_level_fingerprint == canonical_sha256(low_level_material)
        assert low_level_fetch["endpoint"] == endpoint
        assert low_level_fetch["productionMethod"] == (
            "_fetch_amap_place" if endpoint == "place/text" else "_fetch_amap_around"
        )
        assert receipt["acquisitionOrdinal"] < low_level_fetch["enteredOrdinal"] < receipt["fetchOrdinal"]
        assert low_level_fetch["profileOccurrenceFingerprint"] == canonical_sha256(occurrence)
        assert low_level_fetch["queryPlanLineageFingerprint"] == canonical_sha256(lineage)
        audit_material = dict(request)
        audit_fingerprint = audit_material.pop("auditFingerprint")
        assert audit_fingerprint == canonical_sha256(audit_material)
        if endpoint == "place/around":
            assert params["location"]
            assert int(params["radius"]) > 0
            assert request["anchorLineage"]["queryScopeFingerprint"] == lineage["queryScopeFingerprint"]

    assert phase1["webInvocationCount"] == trace["phase1WebInvocationCount"]
    assert phase1["webSeedCount"] == trace["phase1WebSeedCount"]
    assert phase1["deferredDependencies"] == trace["phase1DeferredDependencies"]
    for field in (
        "sourceFingerprint",
        "dedicatedCaseSha256",
        "runtimeEvidenceSha256",
        "opaqueChoiceCheckpointFingerprint",
        "routeContractFingerprint",
        "placeRequestMultisetFingerprint",
        "manifestFingerprint",
    ):
        assert len(str(manifest[field])) == 64
    assert manifest["sourceFingerprint"] == trace["sourceFingerprint"]
    assert manifest["dedicatedCaseSha256"] == trace["dedicatedCaseSha256"]
    assert manifest["runtimeEvidenceSha256"] == trace["runtimeEvidenceSha256"]
    assert manifest["opaqueChoiceCheckpointFingerprint"] == trace["opaqueChoiceCheckpointFingerprint"]
    assert manifest["routeContractFingerprint"] == trace["routeContractFingerprint"]
    assert manifest["placeIdentityCaptureEligible"] is True
    assert manifest["placeIdentityClosureComplete"] is False
    assert manifest["routePairFreezeEligible"] is False
    assert manifest["networkCalls"] == 0
    assert manifest["externalCaptureSession"] == {
        "id": None,
        "consumed": False,
        "recordedFixtureCaptureUsed": 0,
        "externalPlaceCalls": 0,
        "externalRouteCalls": 0,
        "ledgerDelta": 0,
    }
    case_result = result["cases"][0]
    assert case_result["realExternalCallLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
    }
    assert case_result["persistedClarificationChoiceReplay"]["writeDelta"] == {
        "version": 0,
        "patch": 0,
        "route": 0,
    }
    assert case_result["persistedClarificationChoiceReplay"]["recordedFixtureCaptureDelta"] == 0
    assert (
        validate_capture_preflight_manifest(
            manifest=manifest,
            runtime_evidence=result,
            cases_dir=cases_dir,
        )
        is None
    )
    rebuilt = build_capture_preflight_manifest(
        runtime_evidence=result,
        cases_dir=cases_dir,
    )
    assert rebuilt["manifestFingerprint"] == manifest["manifestFingerprint"]

    def blocked_after(mutator, reason_fragment):
        mutated = copy.deepcopy(result)
        mutated_trace = mutated["cases"][0]["recordedCaptureSemanticTrace"]
        mutator(mutated_trace)
        blocked = build_capture_preflight_manifest(
            runtime_evidence=mutated,
            cases_dir=cases_dir,
        )
        assert blocked["phase1PlaceRequestManifest"]["status"] == "blocked"
        assert blocked["placeIdentityCaptureEligible"] is False
        assert any(reason_fragment in reason for reason in blocked["phase1PlaceRequestManifest"]["blockers"])

    def blocked_result_after(mutator, reason_fragment):
        mutated = copy.deepcopy(result)
        mutator(mutated)
        blocked = build_capture_preflight_manifest(
            runtime_evidence=mutated,
            cases_dir=cases_dir,
        )
        assert blocked["phase1PlaceRequestManifest"]["status"] == "blocked"
        assert blocked["placeIdentityCaptureEligible"] is False
        assert any(reason_fragment in reason for reason in blocked["phase1PlaceRequestManifest"]["blockers"])

    for field in (
        "sourceFingerprint",
        "dedicatedCaseSha256",
        "runtimeEvidenceSha256",
        "opaqueChoiceCheckpointFingerprint",
        "routeContractFingerprint",
    ):
        blocked_after(
            lambda mutated_trace, field=field: mutated_trace.__setitem__(field, "0" * 64),
            f"{field}_stale_or_invalid",
        )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["sanitizedParams"].__setitem__(
            "key", "must-not-be-recorded"
        ),
        "secret_parameter_present",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["sanitizedParams"].__setitem__(
            "unexpected", "value"
        ),
        "unknown_parameter_present",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["profileOccurrence"].pop("profileId"),
        "profile_or_query_plan_lineage_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["queryPlanLineage"].pop("sourcePlanId"),
        "profile_or_query_plan_lineage_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["budgetReceipt"].__setitem__(
            "acquired", False
        ),
        "budget_receipt_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0].pop("productionLowLevelFetch"),
        "production_low_level_fetch_provenance_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["budgetReceipt"]["beforeSnapshot"][
            "used"
        ].__setitem__("usedPlaceText", "0"),
        "budget_receipt_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"][0]["budgetReceipt"]["after"].__setitem__(
            "usedCalls",
            mutated_trace["phase1PlaceRequestEvidence"][0]["budgetReceipt"]["before"]["usedCalls"],
        ),
        "budget_receipt_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["phase1PlaceRequestEvidence"].append(
            copy.deepcopy(mutated_trace["phase1PlaceRequestEvidence"][0])
        ),
        "unexplained_duplicate_place_request_audit",
    )

    def reuse_receipt(mutated_trace):
        first, second = mutated_trace["phase1PlaceRequestEvidence"][:2]
        second["budgetReceipt"] = copy.deepcopy(first["budgetReceipt"])
        audit_material = copy.deepcopy(second)
        audit_material.pop("auditFingerprint", None)
        second["auditFingerprint"] = canonical_sha256(audit_material)

    blocked_after(reuse_receipt, "budget_receipt_reused_or_invalid")

    def make_invalid_around(mutated_trace):
        request = mutated_trace["phase1PlaceRequestEvidence"][0]
        request["endpoint"] = "place/around"
        request["sanitizedParams"].update({"location": "116.397,39.908", "radius": "500", "sortrule": "distance"})
        request.pop("anchorLineage", None)

    blocked_after(make_invalid_around, "around_anchor_lineage_invalid")

    def make_noncanonical_around_anchor(mutated_trace):
        request = mutated_trace["phase1PlaceRequestEvidence"][0]
        request["endpoint"] = "place/around"
        request["sanitizedParams"].update({"location": "116.397,39.908", "radius": "500", "sortrule": "distance"})
        request["anchorLineage"] = {
            "slotId": "slot",
            "previous": {
                "amapId": "NOT_CANONICAL",
                "longitude": 116.397,
                "latitude": 39.908,
            },
            "next": None,
            "queryScopeFingerprint": request["queryPlanLineage"]["queryScopeFingerprint"],
        }

    blocked_after(make_noncanonical_around_anchor, "around_anchor_lineage_invalid")
    blocked_after(
        lambda mutated_trace: (
            mutated_trace.__setitem__("phase1WebSeedCount", 1),
            mutated_trace.__setitem__(
                "phase1DeferredDependencies",
                [
                    {
                        "kind": "web_seed_place_requests",
                        "status": "blocked",
                        "seedCount": 1,
                    }
                ],
            ),
        ),
        "web_seed_place_request_dependency_unclosed",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace.pop("phase1WebInvocationCount"),
        "web_dependency_ledger_missing_or_invalid",
    )
    blocked_after(
        lambda mutated_trace: mutated_trace["observedAmapAdcode"].__setitem__("adcode", "310000"),
        "observed_adcode_place_request_binding_invalid",
    )
    blocked_result_after(
        lambda mutated: mutated["cases"][0]["realExternalCallLedger"].pop("network"),
        "external_call_ledger_nonzero_or_missing",
    )
    blocked_result_after(
        lambda mutated: mutated["cases"][0]["persistedClarificationChoiceReplay"]["writeDelta"].pop("route"),
        "formal_write_ledger_nonzero_or_missing",
    )
    blocked_result_after(
        lambda mutated: mutated["cases"][0]["persistedClarificationChoiceReplay"].pop("recordedFixtureCaptureDelta"),
        "recorded_capture_ledger_nonzero_or_missing",
    )
    blocked_result_after(
        lambda mutated: mutated["cases"][0]["recordedRouteDiscovery"].pop("networkCalls"),
        "dry_route_discovery_network_ledger_missing_or_invalid",
    )
    tampered_manifest = copy.deepcopy(manifest)
    tampered_manifest["phase1PlaceRequestManifest"]["requests"][0]["sanitizedParams"]["keywords"] = "tampered"
    with pytest.raises(ValueError, match="tampered"):
        validate_capture_preflight_manifest(
            manifest=tampered_manifest,
            runtime_evidence=result,
            cases_dir=cases_dir,
        )


def test_zero_network_capture_session_envelope_seals_place_only_and_cannot_enter_route_phase(
    tmp_path,
):
    from importlib import import_module

    cases_dir = tmp_path / "fixed-goal-zero-network-capture-envelope"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    assert manifest["phase1PlaceRequestManifest"]["status"] == "ready"

    build_envelope = getattr(
        recorded_fixture_capture_module,
        "build_zero_network_capture_session_envelope",
        None,
    )
    assert callable(build_envelope), (
        "sealed Place-only capture session envelope API is missing after the Phase-1 request manifest becomes ready"
    )

    validate_envelope = getattr(
        recorded_fixture_capture_module,
        "validate_zero_network_capture_session_envelope",
        None,
    )
    require_phase = getattr(
        recorded_fixture_capture_module,
        "require_capture_session_phase",
        None,
    )
    assert callable(validate_envelope)
    assert callable(require_phase)

    envelope = build_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    phase1 = manifest["phase1PlaceRequestManifest"]
    allowlist = envelope["exactPlaceRequestAllowlist"]
    assert envelope["sessionId"] is None
    assert envelope["consumed"] is False
    assert envelope["status"] == "prepared"
    assert envelope["authorizationStatus"] == "awaiting_explicit_authorization"
    assert envelope["captureScope"] == "place_only"
    assert allowlist["requests"] == copy.deepcopy(phase1["requests"])
    assert allowlist["requests"] is not phase1["requests"]
    assert allowlist["requestCount"] == len(allowlist["requests"]) > 0
    assert allowlist["requestCount"] == phase1["requestCount"]
    assert allowlist["requestMultiset"] == phase1["requestMultiset"]
    assert allowlist["requestMultisetFingerprint"] == manifest["placeRequestMultisetFingerprint"]
    allowlist_material = copy.deepcopy(allowlist)
    allowlist_fingerprint = allowlist_material.pop("allowlistFingerprint")
    assert allowlist_fingerprint == canonical_sha256(allowlist_material)

    receipt_references = envelope["budgetReceiptReferences"]
    assert len(receipt_references) == allowlist["requestCount"]
    for request, reference in zip(allowlist["requests"], receipt_references):
        receipt = request["budgetReceipt"]
        assert reference == {
            "endpoint": request["endpoint"],
            "requestFingerprint": request["requestFingerprint"],
            "auditFingerprint": request["auditFingerprint"],
            "profileOccurrenceFingerprint": request["profileOccurrence"]["occurrenceFingerprint"],
            "queryPlanLineageFingerprint": receipt["queryPlanLineageFingerprint"],
            "queryScopeFingerprint": request["queryPlanLineage"]["queryScopeFingerprint"],
            "budgetObjectId": receipt["budgetObjectId"],
            "acquisitionOrdinal": receipt["acquisitionOrdinal"],
            "fetchOrdinal": receipt["fetchOrdinal"],
            "receiptFingerprint": receipt["receiptFingerprint"],
        }

    assert envelope["sourceBindings"] == {
        "sourceFingerprint": manifest["sourceFingerprint"],
        "dedicatedCaseSha256": manifest["dedicatedCaseSha256"],
        "runtimeEvidenceSha256": manifest["runtimeEvidenceSha256"],
        "opaqueChoiceCheckpointFingerprint": manifest["opaqueChoiceCheckpointFingerprint"],
        "routeContractFingerprint": manifest["routeContractFingerprint"],
        "placeRequestMultisetFingerprint": manifest["placeRequestMultisetFingerprint"],
        "capturePreflightManifestFingerprint": manifest["manifestFingerprint"],
    }
    assert envelope["contentFingerprints"] == {
        "phase1PlaceRequestManifest": canonical_sha256(phase1),
        "exactPlaceRequestAllowlist": allowlist_fingerprint,
        "budgetReceiptReferences": canonical_sha256(receipt_references),
    }
    assert envelope["phaseOrdering"] == {
        "currentPhase": "place_identity_capture",
        "placePhaseStatus": "prepared",
        "placeAuthorizationStatus": "awaiting_explicit_authorization",
        "productionReplayStatus": "not_started",
        "canonicalIdentityBindingCertificate": None,
        "exactRoutePairModeManifest": None,
        "routePhaseStatus": "blocked_prerequisites_missing",
        "routePhaseBlockers": [
            "canonical_identity_binding_certificate_missing",
            "exact_route_pair_mode_manifest_missing",
            "route_capture_not_authorized",
        ],
    }
    assert envelope["placeIdentityClosureComplete"] is False
    assert envelope["routePairFreezeEligible"] is False
    assert envelope["routeCaptureAuthorized"] is False
    assert envelope["externalCaptureSession"] == {
        "id": None,
        "consumed": False,
        "recordedFixtureCaptureUsed": 0,
        "externalPlaceCalls": 0,
        "externalRouteCalls": 0,
        "ledgerDelta": 0,
    }
    assert envelope["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }

    def fingerprint_material(value):
        material = copy.deepcopy(value)
        material.pop("captureSessionDisplayId", None)
        material.pop("contentFingerprint", None)
        material.pop("envelopeFingerprint", None)
        return material

    assert len(envelope["contentFingerprint"]) == 64
    assert envelope["contentFingerprint"] == envelope["envelopeFingerprint"]
    assert envelope["contentFingerprint"] == canonical_sha256(fingerprint_material(envelope))
    validate_envelope(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )

    rebuilt = build_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    assert rebuilt["captureSessionDisplayId"] != envelope["captureSessionDisplayId"]
    assert rebuilt["contentFingerprint"] == envelope["contentFingerprint"]
    assert rebuilt["envelopeFingerprint"] == envelope["envelopeFingerprint"]
    first_without_display_id = copy.deepcopy(envelope)
    second_without_display_id = copy.deepcopy(rebuilt)
    first_without_display_id.pop("captureSessionDisplayId")
    second_without_display_id.pop("captureSessionDisplayId")
    assert first_without_display_id == second_without_display_id

    before_place_guard = copy.deepcopy(envelope)
    before_manifest = copy.deepcopy(manifest)
    before_runtime = copy.deepcopy(runtime_evidence)
    require_phase(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        requested_phase="place_only_prepared",
        cases_dir=cases_dir,
    )
    assert envelope == before_place_guard
    assert manifest == before_manifest
    assert runtime_evidence == before_runtime

    with pytest.raises(ValueError) as place_capture_blocked:
        require_phase(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            requested_phase="place_capture",
            cases_dir=cases_dir,
        )
    assert str(place_capture_blocked.value) == "place_capture_not_authorized"
    assert envelope == before_place_guard

    with pytest.raises(ValueError) as route_blocked:
        require_phase(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            requested_phase="route_capture",
            cases_dir=cases_dir,
        )
    assert str(route_blocked.value) == (
        "route_phase_blocked:canonical_identity_binding_certificate_missing,"
        "exact_route_pair_mode_manifest_missing,route_capture_not_authorized"
    )
    assert envelope == before_place_guard

    for field in (
        "sourceFingerprint",
        "dedicatedCaseSha256",
        "runtimeEvidenceSha256",
        "opaqueChoiceCheckpointFingerprint",
        "routeContractFingerprint",
        "placeRequestMultisetFingerprint",
        "manifestFingerprint",
    ):
        stale_manifest = copy.deepcopy(manifest)
        stale_manifest[field] = "0" * 64
        with pytest.raises(ValueError, match="tampered"):
            build_envelope(
                manifest=stale_manifest,
                runtime_evidence=runtime_evidence,
                cases_dir=cases_dir,
            )

    for parameter in ("key", "unexpected"):
        unsafe_manifest = copy.deepcopy(manifest)
        unsafe_manifest["phase1PlaceRequestManifest"]["requests"][0]["sanitizedParams"][parameter] = "must-fail-closed"
        with pytest.raises(ValueError, match="tampered"):
            build_envelope(
                manifest=unsafe_manifest,
                runtime_evidence=runtime_evidence,
                cases_dir=cases_dir,
            )

    # The same production-built runtime/manifest/envelope must also remain a
    # candidate universe when Place returns more than one physical identity.
    # Route-conditioned evaluation, not AMap result order, owns final selection.
    from importlib import import_module

    binding = import_module("backend.evals.recorded_fixture_canonical_binding")
    cases_dir = tmp_path / "fixed-goal-candidate-universe"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    envelope = recorded_fixture_capture_module.build_zero_network_capture_session_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    allowlist = envelope["exactPlaceRequestAllowlist"]
    records = []
    for ordinal, request in enumerate(allowlist["requests"], start=1):
        response = {
            "status": "1",
            "infocode": "10000",
            "count": "2",
            "pois": [
                {
                    "id": f"B{ordinal:08d}A",
                    "name": f"Recorded campus candidate {ordinal}-A",
                    "type": "科教文化服务;学校;高等院校",
                    "typecode": "141201",
                    "tag": "高校;公共空间",
                    "cityname": "北京",
                    "adname": "海淀区",
                    "address": "recorded-shaped",
                    "location": "116.310000,39.990000",
                    "business_area": "学院路",
                    "biz_ext": {
                        "rating": "4.7",
                        "cost": "42",
                        "opentime_today": "09:00-21:00",
                        "opentime_week": "周一至周日 09:00-21:00",
                    },
                    "parent": "B000PARENT1",
                    "children": [
                        {
                            "id": "B000CHILD01",
                            "name": "Recorded child",
                            "type": "科教文化服务",
                            "typecode": "140000",
                            "internalDebug": "must-not-persist",
                        }
                    ],
                    "photos": [],
                },
                {
                    "id": f"B{ordinal:08d}B",
                    "name": f"Recorded campus candidate {ordinal}-B",
                    "type": "科教文化服务;学校;高等院校",
                    "typecode": "141201",
                    "cityname": "北京",
                    "adname": "海淀区",
                    "address": "recorded-shaped",
                    "location": "116.320000,39.980000",
                    "photos": [],
                },
            ],
        }
        records.append(
            {
                "ordinal": ordinal,
                "request": {
                    "endpoint": {
                        "place/text": "/v3/place/text",
                        "place/around": "/v3/place/around",
                    }[request["endpoint"]],
                    "params": copy.deepcopy(request["sanitizedParams"]),
                },
                "requestFingerprint": request["requestFingerprint"],
                "auditFingerprint": request["auditFingerprint"],
                "responseSha256": canonical_sha256(response),
                "response": response,
            }
        )
    quarantine = {
        "schemaVersion": "trip-recorded-amap-v1",
        "recordingType": "recorded/non-live",
        "recordedProvider": "Synthetic recorded-shaped AMap",
        "promotable": False,
        "quarantine": {
            "kind": "place_only_capture",
            "requestCount": len(records),
            "completedResponseCount": len(records),
            "routeCaptureAuthorized": False,
            "routeCalls": 0,
        },
        "responses": records,
    }
    quarantine["bundleFingerprint"] = canonical_sha256(quarantine)

    certificate = binding.build_canonical_candidate_universe_certificate(
        place_quarantine=quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    binding.validate_canonical_candidate_universe_certificate(
        certificate=certificate,
        place_quarantine=quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    assert certificate["finalIdentitySelectionComplete"] is False
    assert certificate["routeMatrixRequired"] is True
    assert certificate["occurrenceCount"] > 0
    assert any(row["candidateCounts"]["total"] > 1 for row in certificate["occurrences"])
    assert all(row["candidateCounts"]["total"] == len(row["candidates"]) for row in certificate["occurrences"])
    assert all(row["selectionStatus"] == "pending_exact_route_matrix" for row in certificate["occurrences"])
    first_payload = next(
        candidate["candidatePayload"]
        for row in certificate["occurrences"]
        for candidate in row["candidates"]
        if candidate["canonicalIdentity"]["amapId"] == "B00000001A"
    )
    assert first_payload["district"] == "海淀区"
    assert first_payload["businessArea"] == "学院路"
    assert first_payload["rating"] == 4.7
    assert first_payload["cost"] == 42.0
    assert first_payload["openTimeToday"] == "09:00-21:00"
    assert first_payload["openTimeWeek"] == "周一至周日 09:00-21:00"
    assert first_payload["parentPoiId"] == "B000PARENT1"
    assert first_payload["children"] == [
        {
            "id": "B000CHILD01",
            "name": "Recorded child",
            "type": "科教文化服务",
            "typecode": "140000",
        }
    ]
    assert "must-not-persist" not in json.dumps(
        certificate,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert all(
        candidate["classification"]
        in {
            "admitted_final_anchor",
            "admitted_anchor_set_member",
            "pending_evidence",
            "area_seed_only",
            "rejected",
        }
        and candidate["admissionDisposition"] in {"admitted", "pending", "rejected"}
        and (candidate["admissionDisposition"] == "admitted") is candidate["scoreEligible"]
        for row in certificate["occurrences"]
        for candidate in row["candidates"]
    )
    pool_reports = binding.canonical_candidate_universe_pool_reports(certificate)
    from src.services.shared_candidate_universe_service import (
        SharedCandidateUniverseBuilder,
    )

    shared_universe = SharedCandidateUniverseBuilder().build(pool_reports)
    assert shared_universe.unique_query_count > 0
    assert shared_universe.unique_query_count + shared_universe.deduped_query_count == len(pool_reports)
    assert shared_universe.pool_candidates
    assert all(
        "consumerAdmissionInput" not in candidate
        and "consumerAdmissionReport" not in candidate
        and "scoreEligible" not in candidate
        for candidates in shared_universe.pool_candidates.values()
        for candidate in candidates
    )
    with pytest.raises(
        binding.CanonicalBindingError,
        match="consumer_admission_rejected|place_identity_ambiguous_or_missing",
    ):
        binding.build_canonical_identity_binding_certificate(
            place_quarantine=quarantine,
            preflight_manifest=manifest,
            envelope=envelope,
            runtime_evidence=runtime_evidence,
            cases_dir=cases_dir,
        )
    tampered = copy.deepcopy(certificate)
    tampered["occurrences"][0]["selectionStatus"] = "selected"
    with pytest.raises(
        binding.CanonicalBindingError,
        match="canonical_candidate_universe_tampered",
    ):
        binding.validate_canonical_candidate_universe_certificate(
            certificate=tampered,
            place_quarantine=quarantine,
            preflight_manifest=manifest,
            envelope=envelope,
            runtime_evidence=runtime_evidence,
            cases_dir=cases_dir,
        )

    def resign(value):
        fingerprint = canonical_sha256(fingerprint_material(value))
        value["contentFingerprint"] = fingerprint
        value["envelopeFingerprint"] = fingerprint

    envelope_mutators = (
        lambda value: value["exactPlaceRequestAllowlist"]["requests"][0]["sanitizedParams"].__setitem__(
            "keywords", "tampered"
        ),
        lambda value: value["budgetReceiptReferences"][0].__setitem__("receiptFingerprint", "0" * 64),
        lambda value: value["zeroEffectLedger"].__setitem__("network", 1),
        lambda value: value["phaseOrdering"].__setitem__("routePhaseStatus", "ready"),
        lambda value: value["sourceBindings"].__setitem__("sourceFingerprint", "0" * 64),
    )
    for mutate in envelope_mutators:
        tampered_envelope = copy.deepcopy(envelope)
        mutate(tampered_envelope)
        resign(tampered_envelope)
        with pytest.raises(ValueError, match="content or preflight binding was tampered"):
            validate_envelope(
                envelope=tampered_envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                cases_dir=cases_dir,
            )

    forged_route_envelope = copy.deepcopy(envelope)
    forged_route_envelope["phaseOrdering"]["canonicalIdentityBindingCertificate"] = {"fingerprint": "a" * 64}
    forged_route_envelope["phaseOrdering"]["exactRoutePairModeManifest"] = {"fingerprint": "b" * 64}
    forged_route_envelope["phaseOrdering"]["routePhaseStatus"] = "ready"
    forged_route_envelope["routePairFreezeEligible"] = True
    forged_route_envelope["routeCaptureAuthorized"] = True
    resign(forged_route_envelope)
    with pytest.raises(ValueError, match="content or preflight binding was tampered"):
        require_phase(
            envelope=forged_route_envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            requested_phase="route_capture",
            cases_dir=cases_dir,
        )

    rendered_envelope = json.dumps(envelope, ensure_ascii=False)
    assert "recordedProviderEvidence" not in rendered_envelope
    assert "recordedAmapReplay" not in rendered_envelope
    assert '"fixture"' not in rendered_envelope
    assert envelope["phaseOrdering"]["productionReplayStatus"] == "not_started"
    case_result = runtime_evidence["cases"][0]
    assert case_result["realExternalCallLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
    }
    assert case_result["persistedClarificationChoiceReplay"]["writeDelta"] == {
        "version": 0,
        "patch": 0,
        "route": 0,
    }
    assert case_result["persistedClarificationChoiceReplay"]["recordedFixtureCaptureDelta"] == 0

    binding = import_module("backend.evals.recorded_fixture_canonical_binding")
    assert binding._validated_allowlist(copy.deepcopy(envelope)) == envelope["exactPlaceRequestAllowlist"]
    with pytest.raises(
        binding.CanonicalBindingError,
        match="place_quarantine_schema_invalid",
    ):
        binding.build_canonical_identity_binding_certificate(
            place_quarantine={
                "schemaVersion": "trip-place-capture-partial-quarantine-v1",
                "status": "failed_partial",
                "promotable": False,
            },
            preflight_manifest=manifest,
            envelope=envelope,
            runtime_evidence=runtime_evidence,
            cases_dir=cases_dir,
        )


def test_real_place_transport_is_exact_bounded_no_redirect_and_executor_ready_without_network(
    tmp_path,
    monkeypatch,
):
    import hashlib
    import os
    import ssl
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from importlib.util import find_spec
    from pathlib import Path
    from urllib.parse import parse_qs, urlsplit

    cases_dir = tmp_path / "fixed-goal-zero-network-real-place-transport"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    build_envelope = getattr(
        recorded_fixture_capture_module,
        "build_zero_network_capture_session_envelope",
    )
    validate_envelope = getattr(
        recorded_fixture_capture_module,
        "validate_zero_network_capture_session_envelope",
    )
    envelope = build_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    validate_envelope(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    assert envelope["status"] == "prepared"
    assert envelope["captureScope"] == "place_only"

    module_name = "backend.evals.recorded_fixture_place_capture_transport"
    module_spec = find_spec(module_name)
    transport_module = import_module(module_name) if module_spec is not None else None
    real_transport_type = getattr(
        transport_module,
        "AmapWebServicePlaceCaptureTransport",
        None,
    )
    validate_network_authorization = getattr(
        transport_module,
        "validate_real_place_network_authorization",
        None,
    )
    assert isinstance(real_transport_type, type) and callable(validate_network_authorization), (
        "production-shaped exact AMap Place transport / bounded network "
        "authorization contract is missing after the sealed Place-only "
        "envelope validates"
    )
    transport_error_type = getattr(transport_module, "RealPlaceTransportError")
    sequence_fingerprint = getattr(
        transport_module,
        "exact_outbound_request_sequence_fingerprint",
    )
    executor_module = import_module("backend.evals.recorded_fixture_place_capture_executor")
    execute_place_capture = getattr(
        executor_module,
        "execute_zero_network_place_capture",
    )
    place_execution_error = getattr(executor_module, "PlaceCaptureExecutionError")

    fixed_now = datetime.now(timezone.utc)
    allowlist = envelope["exactPlaceRequestAllowlist"]
    request_count = allowlist["requestCount"]
    endpoint_paths = {
        "place/text": "/v3/place/text",
        "place/around": "/v3/place/around",
    }
    outbound_requests = [
        {
            "method": "GET",
            "scheme": "https",
            "host": "restapi.amap.com",
            "path": endpoint_paths[request["endpoint"]],
            "params": copy.deepcopy(request["sanitizedParams"]),
            "allowRedirects": False,
            "ordinal": ordinal,
            "requestFingerprint": request["requestFingerprint"],
            "auditFingerprint": request["auditFingerprint"],
        }
        for ordinal, request in enumerate(allowlist["requests"], start=1)
    ]
    outbound_fingerprint = sequence_fingerprint(outbound_requests)
    secret = "test-only-real-transport-credential-MUST-NOT-PERSIST"

    def root_binding(root):
        details = os.stat(root, follow_symlinks=False)
        return recorded_fixture_capture_module.canonical_sha256(
            {
                "stagingRootIdentity": {
                    "device": details.st_dev,
                    "file": details.st_ino,
                    "createdAtNs": details.st_ctime_ns,
                }
            }
        )

    def place_authorization(authorization_id):
        return {
            "authorizationId": authorization_id,
            "scope": "place_only",
            "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
            "envelopeFingerprint": envelope["envelopeFingerprint"],
            "contentFingerprint": envelope["contentFingerprint"],
            "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
            "maxCalls": request_count,
            "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
            "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
        }

    def session_directory_name(place_auth):
        return (
            "place-session-"
            + recorded_fixture_capture_module.canonical_sha256({"authorizationId": place_auth["authorizationId"]})[
                :32
            ].lower()
        )

    def network_authorization(
        place_auth,
        staging_root,
        *,
        maximum_bytes=1_000_000,
        requests=None,
    ):
        authorized_requests = requests or outbound_requests
        return {
            "schemaVersion": "trip-amap-place-network-authorization-v1",
            "networkAuthorizationId": (f"network-{place_auth['authorizationId']}"),
            "scope": "place_only",
            "transportKind": "amap_place_https",
            "placeAuthorizationId": place_auth["authorizationId"],
            "placeAuthorizationFingerprint": (recorded_fixture_capture_module.canonical_sha256(place_auth)),
            "sourceFingerprint": place_auth["sourceFingerprint"],
            "envelopeFingerprint": place_auth["envelopeFingerprint"],
            "contentFingerprint": place_auth["contentFingerprint"],
            "exactPlaceRequestAllowlistFingerprint": place_auth["exactPlaceRequestAllowlistFingerprint"],
            "outboundRequestSequenceFingerprint": sequence_fingerprint(authorized_requests),
            "maxCalls": place_auth["maxCalls"],
            "sessionDirectoryName": session_directory_name(place_auth),
            "stagingRootBindingFingerprint": root_binding(staging_root),
            "transportProfile": {
                "kind": "amap_place_https_v1",
                "host": "restapi.amap.com",
                "paths": sorted({row["path"] for row in authorized_requests}),
                "proxyMode": "system",
                "timeoutSeconds": 5.0,
                "userAgentProfile": "trip-place-capture-v1",
                "maxResponseBytes": maximum_bytes,
            },
            "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
            "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
        }

    class StubResponse:
        def __init__(
            self,
            *,
            body,
            status=200,
            content_type="application/json; charset=utf-8",
            final_url=None,
            content_length=None,
        ):
            self.status = status
            self._body = body
            self._final_url = final_url
            self.read_limits = []
            self.headers = {"Content-Type": content_type}
            if content_length is not None:
                self.headers["Content-Length"] = str(content_length)

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def getcode(self):
            return self.status

        def geturl(self):
            return self._final_url

        def read(self, limit):
            self.read_limits.append(limit)
            return self._body

    class StubOpener:
        def __init__(self, responses):
            self._responses = list(responses)
            self.calls = []
            self.claim_state_path = None

        def open(self, request, *, timeout):
            if self.claim_state_path is not None:
                pending_state = json.loads(self.claim_state_path.read_text(encoding="utf-8"))
                assert pending_state["state"] == "consumed_in_progress"
                assert pending_state["consumed"] is True
                assert pending_state["promotable"] is False
                assert pending_state["effectLedgerStatus"] == ("transport_attempt_pending")
                assert "zeroEffectLedger" not in pending_state
                assert pending_state["attemptedPlaceCalls"] == len(self.calls) + 1
            self.calls.append({"request": request, "timeout": timeout})
            if not self._responses:
                raise AssertionError("unexpected extra HTTP opener call")
            response = self._responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if response._final_url is None:
                response._final_url = request.full_url
            return response

    response_bodies = []
    for ordinal in range(1, request_count + 1):
        response_bodies.append(
            json.dumps(
                {
                    "status": "1",
                    "info": "OK",
                    "infocode": "10000",
                    "count": "1",
                    "pois": [
                        {
                            "id": f"B000R{ordinal:05d}",
                            "name": f"stub-recorded-place-{ordinal}",
                            "location": f"116.{ordinal:06d},39.{ordinal:06d}",
                        }
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    success_responses = [
        StubResponse(
            body=body,
            content_length=len(body),
        )
        for body in response_bodies
    ]
    success_opener = StubOpener(success_responses)
    opener_factory_calls = []

    def stub_opener_factory(proxy_mode):
        opener_factory_calls.append(proxy_mode)
        return success_opener

    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        stub_opener_factory,
    )

    fixture_path = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixture_sha256_before = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    case_sha256_before = hashlib.sha256(dedicated_case.read_bytes()).hexdigest()
    runtime_before = copy.deepcopy(runtime_evidence)
    success_root = tmp_path / "real-place-transport-success"
    success_root.mkdir()
    success_place_authorization = place_authorization("real-place-http-success")
    success_network_authorization = network_authorization(
        success_place_authorization,
        success_root,
    )
    validated_network_authorization = validate_network_authorization(
        authorization=success_network_authorization,
        place_authorization=success_place_authorization,
        source_fingerprint=success_place_authorization["sourceFingerprint"],
        envelope_fingerprint=success_place_authorization["envelopeFingerprint"],
        content_fingerprint=success_place_authorization["contentFingerprint"],
        allowlist_fingerprint=success_place_authorization["exactPlaceRequestAllowlistFingerprint"],
        outbound_request_sequence_fingerprint=outbound_fingerprint,
        max_calls=request_count,
        session_directory_name=session_directory_name(success_place_authorization),
        staging_root_binding_fingerprint=root_binding(success_root),
        expected_paths=sorted({row["path"] for row in outbound_requests}),
        now=fixed_now,
    )
    assert validated_network_authorization == {
        "authorization": success_network_authorization,
        "authorizationFingerprint": recorded_fixture_capture_module.canonical_sha256(success_network_authorization),
    }
    success_transport = real_transport_type(
        network_authorization=success_network_authorization,
    )
    success_opener.claim_state_path = (
        success_root / session_directory_name(success_place_authorization) / "session-state.json"
    )
    socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        result = execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=success_place_authorization,
            staging_root=success_root,
            credential=secret,
            transport=success_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )

    assert socket_trace["realExternalCalls"] == []
    assert opener_factory_calls == ["system"]
    assert len(success_opener.calls) == request_count
    for expected, observed in zip(outbound_requests, success_opener.calls):
        request = observed["request"]
        split = urlsplit(request.full_url)
        query = parse_qs(split.query, keep_blank_values=True)
        assert request.get_method() == "GET"
        assert split.scheme == "https"
        assert split.hostname == "restapi.amap.com"
        assert split.path == expected["path"]
        assert query == {
            **{key: [value] for key, value in expected["params"].items()},
            "key": [secret],
        }
        headers = {key.casefold(): value for key, value in request.header_items()}
        assert headers == {
            "accept": "application/json",
            "accept-encoding": "identity",
            "user-agent": "trip-recorded-place-capture/1.0",
            "connection": "close",
        }
        assert observed["timeout"] == 5.0
    assert all(response.read_limits == [1_000_001] for response in success_responses)

    assert result["status"] == "completed"
    assert result["consumed"] is True
    assert result["promotable"] is False
    assert result["captureKind"] == "recorded_fixture_acquisition"
    assert result["acquisitionMode"] == "amap_web_service_https_authorized"
    assert result["transportKind"] == "amap_place_https"
    assert result["realExternalCapture"] is False
    assert result["attemptedPlaceCalls"] == request_count
    assert result["externalPlaceCalls"] == 0
    assert result["stubTransportCalls"] == request_count
    assert result["fakeTransportCalls"] == 0
    assert result["routeCaptureAuthorized"] is False
    assert result["routeCalls"] == 0
    assert result["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    session_directory = success_root / result["sessionDirectoryName"]
    bundle_path = session_directory / result["quarantineBundleFile"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["promotable"] is False
    assert bundle["quarantine"]["captureKind"] == ("recorded_fixture_acquisition")
    assert bundle["quarantine"]["acquisitionMode"] == ("amap_web_service_https_authorized")
    assert bundle["quarantine"]["transportKind"] == "amap_place_https"
    assert bundle["quarantine"]["transportRoute"] == "system"
    assert bundle["quarantine"]["realExternalCapture"] is False
    assert bundle["quarantine"]["externalPlaceCalls"] == 0
    assert bundle["quarantine"]["stubTransportCalls"] == request_count
    assert bundle["quarantine"]["routeCalls"] == 0
    assert len(bundle["responses"]) == request_count
    for expected, record in zip(outbound_requests, bundle["responses"]):
        assert record["request"] == {
            "endpoint": expected["path"],
            "params": expected["params"],
        }
        canonical_response = json.dumps(
            record["response"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert record["responseSha256"] == hashlib.sha256(canonical_response).hexdigest().upper()

    persisted = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in session_directory.iterdir()
        if path.is_file()
    }
    rendered_persisted = json.dumps(persisted, ensure_ascii=False, sort_keys=True)
    assert secret not in rendered_persisted
    assert "https://" not in rendered_persisted
    assert "?key=" not in rendered_persisted
    assert str(tmp_path).casefold() not in rendered_persisted.casefold()
    assert runtime_evidence == runtime_before
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == (fixture_sha256_before)
    assert hashlib.sha256(dedicated_case.read_bytes()).hexdigest() == (case_sha256_before)

    replay_transport = real_transport_type(
        network_authorization=success_network_authorization,
    )
    with pytest.raises(place_execution_error, match="authorization_already_consumed"):
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=success_place_authorization,
            staging_root=success_root,
            credential=secret,
            transport=replay_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert replay_transport.attempted_place_calls == 0
    assert len(success_opener.calls) == request_count

    assert {request["path"] for request in outbound_requests}.issubset({"/v3/place/text", "/v3/place/around"})
    assert "/v3/place/text" in {request["path"] for request in outbound_requests}

    around_root = tmp_path / "standalone-around-transport"
    around_root.mkdir()
    around_request = {
        "method": "GET",
        "scheme": "https",
        "host": "restapi.amap.com",
        "path": "/v3/place/around",
        "params": {
            "location": "116.397428,39.90923",
            "radius": "1200",
            "keywords": "博物馆",
            "types": "140000",
            "city": "110000",
            "citylimit": "true",
            "sortrule": "distance",
            "offset": "20",
            "page": "1",
            "extensions": "all",
            "output": "json",
        },
        "allowRedirects": False,
        "ordinal": 1,
        "requestFingerprint": "4" * 64,
        "auditFingerprint": "5" * 64,
    }
    around_requests = [around_request]
    around_fingerprint = sequence_fingerprint(around_requests)
    around_place_auth = place_authorization("standalone-around")
    around_place_auth["maxCalls"] = 1
    around_network_auth = network_authorization(
        around_place_auth,
        around_root,
        requests=around_requests,
    )
    around_validation = validate_network_authorization(
        authorization=around_network_auth,
        place_authorization=around_place_auth,
        source_fingerprint=around_place_auth["sourceFingerprint"],
        envelope_fingerprint=around_place_auth["envelopeFingerprint"],
        content_fingerprint=around_place_auth["contentFingerprint"],
        allowlist_fingerprint=around_place_auth["exactPlaceRequestAllowlistFingerprint"],
        outbound_request_sequence_fingerprint=around_fingerprint,
        max_calls=1,
        session_directory_name=session_directory_name(around_place_auth),
        staging_root_binding_fingerprint=root_binding(around_root),
        expected_paths=["/v3/place/around"],
        now=fixed_now,
    )
    around_session_name = session_directory_name(around_place_auth)
    around_session_directory = around_root / around_session_name
    around_claim_receipt = {
        "state": "consumed_in_progress",
        "networkAuthorizationId": around_network_auth["networkAuthorizationId"],
        "networkAuthorizationFingerprint": around_validation["authorizationFingerprint"],
        "placeAuthorizationId": around_place_auth["authorizationId"],
        "placeAuthorizationFingerprint": (recorded_fixture_capture_module.canonical_sha256(around_place_auth)),
        "sessionDirectoryName": around_session_name,
        "stagingRootBindingFingerprint": root_binding(around_root),
        "outboundRequestSequenceFingerprint": around_fingerprint,
        "maxCalls": 1,
    }
    around_state_path = around_session_directory / "session-state.json"
    unclaimed_around_transport = real_transport_type(network_authorization=around_network_auth)
    with pytest.raises(
        transport_error_type,
        match="real_transport_claim_state_invalid",
    ):
        unclaimed_around_transport._bind_claimed_execution(
            validated_authorization=around_validation,
            claim_receipt=around_claim_receipt,
            claim_state_path=around_state_path,
            expected_requests=around_requests,
            now=fixed_now,
        )
    assert unclaimed_around_transport.attempted_place_calls == 0

    around_session_directory.mkdir()
    around_state = {
        "schemaVersion": "trip-place-capture-session-state-v1",
        "state": "consumed_in_progress",
        "consumed": True,
        "promotable": False,
        "authorizationId": around_place_auth["authorizationId"],
        "authorizationFingerprint": around_claim_receipt["placeAuthorizationFingerprint"],
        "networkAuthorizationId": around_network_auth["networkAuthorizationId"],
        "networkAuthorizationFingerprint": around_validation["authorizationFingerprint"],
        "stagingRootBindingFingerprint": root_binding(around_root),
        "outboundRequestSequenceFingerprint": around_fingerprint,
        "maxCalls": 1,
        "attemptedPlaceCalls": 0,
        "externalPlaceCalls": 0,
        "stubTransportCalls": 0,
        "fakeTransportCalls": 0,
        "completedResponses": 0,
        "zeroEffectLedger": {
            "network": 0,
            "amap": 0,
            "web": 0,
            "controller": 0,
            "capture": 0,
            "version": 0,
            "patch": 0,
            "routeWrite": 0,
        },
    }
    around_state_path.write_text(
        json.dumps(around_state, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    around_transport = real_transport_type(network_authorization=around_network_auth)
    around_transport._bind_claimed_execution(
        validated_authorization=around_validation,
        claim_receipt=around_claim_receipt,
        claim_state_path=around_state_path,
        expected_requests=around_requests,
        now=fixed_now,
    )
    around_response = StubResponse(
        body=response_bodies[0],
        content_length=len(response_bodies[0]),
    )
    around_opener = StubOpener([around_response])
    around_opener.claim_state_path = around_state_path
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda proxy_mode: around_opener if proxy_mode == "system" else pytest.fail("unexpected proxy mode"),
    )
    around_result = around_transport(
        request=copy.deepcopy(around_request),
        credential=secret,
    )
    assert around_result["statusCode"] == 200
    assert len(around_opener.calls) == 1
    around_url = urlsplit(around_opener.calls[0]["request"].full_url)
    assert around_url.path == "/v3/place/around"
    assert parse_qs(around_url.query, keep_blank_values=True) == {
        **{key: [value] for key, value in around_request["params"].items()},
        "key": [secret],
    }
    with pytest.raises(TypeError, match="amap_place_capture_transport_is_final"):
        type("ForgedAmapPlaceTransport", (real_transport_type,), {})

    invalid_sequences = []
    wrong_host = copy.deepcopy(outbound_requests)
    wrong_host[0]["host"] = "example.invalid"
    invalid_sequences.append(wrong_host)
    wrong_path = copy.deepcopy(outbound_requests)
    wrong_path[0]["path"] = "/v3/direction/transit/integrated"
    invalid_sequences.append(wrong_path)
    unknown_parameter = copy.deepcopy(outbound_requests)
    unknown_parameter[0]["params"]["unexpected"] = "forbidden"
    invalid_sequences.append(unknown_parameter)
    secret_parameter = copy.deepcopy(outbound_requests)
    secret_parameter[0]["params"]["key"] = secret
    invalid_sequences.append(secret_parameter)
    for invalid_sequence in invalid_sequences:
        factory_count_before = len(opener_factory_calls)
        with pytest.raises(transport_error_type, match="real_transport_request"):
            sequence_fingerprint(invalid_sequence)
        assert len(opener_factory_calls) == factory_count_before

    def assert_preclaim_rejected(label, mutate, expected_reason):
        staging_root = tmp_path / f"preclaim-rejected-{label}"
        staging_root.mkdir()
        place_auth = place_authorization(f"preclaim-{label}")
        network_auth = network_authorization(place_auth, staging_root)
        mutate(network_auth)
        candidate = real_transport_type(network_authorization=network_auth)
        factory_count_before = len(opener_factory_calls)
        with pytest.raises(place_execution_error, match=expected_reason):
            execute_place_capture(
                envelope=envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                authorization=place_auth,
                staging_root=staging_root,
                credential=secret,
                transport=candidate,
                cases_dir=cases_dir,
                now=fixed_now,
            )
        assert list(staging_root.iterdir()) == []
        assert candidate.attempted_place_calls == 0
        assert len(opener_factory_calls) == factory_count_before

    preclaim_cases = [
        (
            "source",
            lambda value: value.__setitem__("sourceFingerprint", "0" * 64),
            "network_authorization_sourceFingerprint_mismatch",
        ),
        (
            "envelope",
            lambda value: value.__setitem__("envelopeFingerprint", "1" * 64),
            "network_authorization_envelopeFingerprint_mismatch",
        ),
        (
            "allowlist",
            lambda value: value.__setitem__("exactPlaceRequestAllowlistFingerprint", "2" * 64),
            "network_authorization_exactPlaceRequestAllowlistFingerprint_mismatch",
        ),
        (
            "scope",
            lambda value: value.__setitem__("scope", "route"),
            "network_authorization_scope_invalid",
        ),
        (
            "transport",
            lambda value: value.__setitem__("transportKind", "arbitrary_callable"),
            "network_authorization_transport_invalid",
        ),
        (
            "max-calls",
            lambda value: value.__setitem__("maxCalls", request_count + 1),
            "network_authorization_maxCalls_mismatch",
        ),
        (
            "session",
            lambda value: value.__setitem__("sessionDirectoryName", "different-session"),
            "network_authorization_sessionDirectoryName_mismatch",
        ),
        (
            "staging-root",
            lambda value: value.__setitem__("stagingRootBindingFingerprint", "3" * 64),
            "network_authorization_stagingRootBindingFingerprint_mismatch",
        ),
        (
            "proxy-profile",
            lambda value: value["transportProfile"].__setitem__("proxyMode", "http://proxy.invalid"),
            "network_authorization_profile_invalid",
        ),
        (
            "expired",
            lambda value: value.__setitem__("expiresAt", (fixed_now - timedelta(seconds=1)).isoformat()),
            "network_authorization_expired_or_not_yet_valid",
        ),
    ]
    for label, mutate, expected_reason in preclaim_cases:
        assert_preclaim_rejected(label, mutate, expected_reason)

    arbitrary_root = tmp_path / "arbitrary-transport-rejected"
    arbitrary_root.mkdir()
    arbitrary_auth = place_authorization("arbitrary-transport")
    with pytest.raises(place_execution_error, match="exact_place_transport_required"):
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=arbitrary_auth,
            staging_root=arbitrary_root,
            credential=secret,
            transport=lambda **_kwargs: {},
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert list(arbitrary_root.iterdir()) == []

    def run_partial_failure(
        label,
        response_or_error,
        expected_reason,
        *,
        maximum_bytes=1_000_000,
        expected_provider_diagnostic=None,
    ):
        staging_root = tmp_path / f"partial-{label}"
        staging_root.mkdir()
        place_auth = place_authorization(f"partial-{label}")
        network_auth = network_authorization(
            place_auth,
            staging_root,
            maximum_bytes=maximum_bytes,
        )
        candidate = real_transport_type(network_authorization=network_auth)
        opener = StubOpener([response_or_error])
        opener.claim_state_path = staging_root / session_directory_name(place_auth) / "session-state.json"
        local_factory_calls = []

        def local_factory(proxy_mode):
            local_factory_calls.append(proxy_mode)
            return opener

        monkeypatch.setattr(
            transport_module,
            "_build_hardened_opener",
            local_factory,
        )
        local_socket_trace = {"realExternalCalls": []}
        with offline_eval_module._offline_external_network_sentinel(local_socket_trace):
            failure = execute_place_capture(
                envelope=envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                authorization=place_auth,
                staging_root=staging_root,
                credential=secret,
                transport=candidate,
                cases_dir=cases_dir,
                now=fixed_now,
                max_response_bytes=maximum_bytes,
            )
        assert local_socket_trace["realExternalCalls"] == []
        assert local_factory_calls == ["system"]
        assert len(opener.calls) == 1
        assert failure["status"] == "failed"
        assert failure["reasonCode"] == expected_reason
        assert failure["consumed"] is True
        assert failure["promotable"] is False
        assert failure["completedResponseCount"] == 0
        assert failure["quarantineBundleFile"] is None
        assert failure["partialQuarantineFile"] is not None
        assert failure["attemptedPlaceCalls"] == 1
        assert failure["externalPlaceCalls"] == 0
        assert failure["stubTransportCalls"] == 1
        assert failure["fakeTransportCalls"] == 0
        assert failure["zeroEffectLedger"] == {
            "network": 0,
            "amap": 0,
            "web": 0,
            "controller": 0,
            "capture": 0,
            "version": 0,
            "patch": 0,
            "routeWrite": 0,
        }
        failure_session = staging_root / failure["sessionDirectoryName"]
        failure_payloads = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in failure_session.iterdir()
            if path.is_file()
        }
        failure_rendered = json.dumps(
            failure_payloads,
            ensure_ascii=False,
            sort_keys=True,
        )
        assert secret not in failure_rendered
        assert "https://" not in failure_rendered
        assert "?key=" not in failure_rendered
        assert "secret-error-body" not in failure_rendered
        if expected_provider_diagnostic is None:
            assert "providerDiagnostic" not in failure
            assert all("providerDiagnostic" not in payload for payload in failure_payloads.values())
        else:
            assert failure["providerDiagnostic"] == expected_provider_diagnostic
            assert all(
                payload["providerDiagnostic"] == expected_provider_diagnostic for payload in failure_payloads.values()
            )

        replay_candidate = real_transport_type(network_authorization=network_auth)
        call_count_before = len(opener.calls)
        with pytest.raises(
            place_execution_error,
            match="authorization_already_consumed",
        ):
            execute_place_capture(
                envelope=envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                authorization=place_auth,
                staging_root=staging_root,
                credential=secret,
                transport=replay_candidate,
                cases_dir=cases_dir,
                now=fixed_now,
                max_response_bytes=maximum_bytes,
            )
        assert replay_candidate.attempted_place_calls == 0
        assert len(opener.calls) == call_count_before

    valid_failure_body = response_bodies[0]
    failure_cases = [
        (
            "redirect",
            StubResponse(
                body=valid_failure_body,
                final_url="https://example.invalid/redirect-target",
            ),
            "transport_redirect_forbidden",
            None,
        ),
        (
            "timeout",
            offline_eval_module.socket.timeout("secret-error-body must never be persisted"),
            "transport_timeout",
            None,
        ),
        (
            "tls",
            ssl.SSLError("secret-tls-detail must never be persisted"),
            "transport_tls_error",
            None,
        ),
        (
            "http-status",
            StubResponse(body=valid_failure_body, status=503),
            "transport_http_status_invalid",
            None,
        ),
        (
            "provider",
            StubResponse(body=b'{"status":"0","infocode":"10001"}'),
            "amap_response_unsuccessful",
            {
                "schemaVersion": "trip-amap-provider-diagnostic-v1",
                "provider": "amap_web_service",
                "status": "0",
                "infocode": "10001",
            },
        ),
        (
            "provider-missing-status",
            StubResponse(body=b'{"infocode":"10001"}'),
            "amap_response_unsuccessful",
            None,
        ),
        (
            "provider-missing-infocode",
            StubResponse(body=b'{"status":"0"}'),
            "amap_response_unsuccessful",
            None,
        ),
        (
            "provider-non-string-status",
            StubResponse(body=b'{"status":0,"infocode":"10001"}'),
            "amap_response_unsuccessful",
            None,
        ),
        (
            "provider-non-string-infocode",
            StubResponse(body=b'{"status":"0","infocode":10001}'),
            "amap_response_unsuccessful",
            None,
        ),
        (
            "provider-non-ascii-infocode",
            StubResponse(body='{"status":"0","infocode":"１０００１"}'.encode("utf-8")),
            "amap_response_unsuccessful",
            None,
        ),
        (
            "provider-overlong-infocode",
            StubResponse(body=b'{"status":"0","infocode":"100001"}'),
            "amap_response_unsuccessful",
            None,
        ),
        (
            "json",
            StubResponse(body=b"{not-json"),
            "transport_json_invalid",
            None,
        ),
        (
            "content-type",
            StubResponse(body=valid_failure_body, content_type="text/plain"),
            "transport_content_type_invalid",
            None,
        ),
    ]
    for (
        label,
        response_or_error,
        expected_reason,
        expected_provider_diagnostic,
    ) in failure_cases:
        run_partial_failure(
            label,
            response_or_error,
            expected_reason,
            expected_provider_diagnostic=expected_provider_diagnostic,
        )
    run_partial_failure(
        "body-size",
        StubResponse(body=valid_failure_body, content_length=257),
        "transport_body_size_invalid",
        maximum_bytes=256,
    )

    assert runtime_evidence == runtime_before
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == (fixture_sha256_before)
    assert hashlib.sha256(dedicated_case.read_bytes()).hexdigest() == (case_sha256_before)


def test_real_place_capture_preserves_only_safe_amap_failure_diagnostic(
    monkeypatch,
    tmp_path,
    capsys,
):
    """A typed AMap business failure preserves only the two whitelisted codes."""

    import hashlib
    import stat
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from urllib.parse import parse_qs, urlsplit

    cases_dir = tmp_path / "provider-diagnostic-case"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source_fixture = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixture_sha256_before = hashlib.sha256(source_fixture.read_bytes()).hexdigest()
    case_sha256_before = hashlib.sha256(dedicated_case.read_bytes()).hexdigest()
    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    envelope = recorded_fixture_capture_module.build_zero_network_capture_session_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    runtime_before = copy.deepcopy(runtime_evidence)
    transport_module = import_module("backend.evals.recorded_fixture_place_capture_transport")
    executor_module = import_module("backend.evals.recorded_fixture_place_capture_executor")
    cli = import_module("backend.evals.recorded_fixture_place_capture_cli")
    transport_type = transport_module.AmapWebServicePlaceCaptureTransport
    execute = executor_module.execute_zero_network_place_capture
    execution_error = executor_module.PlaceCaptureExecutionError
    fixed_now = datetime.now(timezone.utc)
    credential = "provider-diagnostic-credential-sentinel"
    info_sentinel = "secret-and-url-sentinel?token=must-not-persist"
    expected_diagnostic = {
        "schemaVersion": "trip-amap-provider-diagnostic-v1",
        "provider": "amap_web_service",
        "status": "0",
        "infocode": "10001",
    }
    allowlist = envelope["exactPlaceRequestAllowlist"]
    outbound = executor_module._validated_outbound_requests(allowlist)

    def root_binding(root):
        details = os.stat(root, follow_symlinks=False)
        return recorded_fixture_capture_module.canonical_sha256(
            {
                "stagingRootIdentity": {
                    "device": details.st_dev,
                    "file": details.st_ino,
                    "createdAtNs": details.st_ctime_ns,
                }
            }
        )

    def place_authorization(identifier):
        return {
            "authorizationId": identifier,
            "scope": "place_only",
            "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
            "envelopeFingerprint": envelope["envelopeFingerprint"],
            "contentFingerprint": envelope["contentFingerprint"],
            "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
            "maxCalls": allowlist["requestCount"],
            "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
            "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
        }

    def network_authorization(place_auth, identifier, root):
        session_name = (
            "place-session-"
            + recorded_fixture_capture_module.canonical_sha256({"authorizationId": place_auth["authorizationId"]})[
                :32
            ].lower()
        )
        return {
            "schemaVersion": "trip-amap-place-network-authorization-v1",
            "networkAuthorizationId": identifier,
            "scope": "place_only",
            "transportKind": "amap_place_https",
            "placeAuthorizationId": place_auth["authorizationId"],
            "placeAuthorizationFingerprint": recorded_fixture_capture_module.canonical_sha256(place_auth),
            "sourceFingerprint": place_auth["sourceFingerprint"],
            "envelopeFingerprint": place_auth["envelopeFingerprint"],
            "contentFingerprint": place_auth["contentFingerprint"],
            "exactPlaceRequestAllowlistFingerprint": place_auth["exactPlaceRequestAllowlistFingerprint"],
            "outboundRequestSequenceFingerprint": transport_module.exact_outbound_request_sequence_fingerprint(
                outbound
            ),
            "maxCalls": place_auth["maxCalls"],
            "sessionDirectoryName": session_name,
            "stagingRootBindingFingerprint": root_binding(root),
            "transportProfile": {
                "kind": "amap_place_https_v1",
                "host": "restapi.amap.com",
                "paths": sorted({request["path"] for request in outbound}),
                "proxyMode": "direct",
                "timeoutSeconds": 5.0,
                "userAgentProfile": "trip-place-capture-v1",
                "maxResponseBytes": 1_000_000,
            },
            "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
            "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
        }

    class StubResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self._body = body
            self._url = ""

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def getcode(self):
            return self.status

        def geturl(self):
            return self._url

        def read(self, _limit):
            return self._body

    def response_body(*, status="1", infocode="10000", info="OK"):
        return json.dumps(
            {
                "status": status,
                "infocode": infocode,
                "info": info,
                "count": "0",
                "pois": [],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    class StubOpener:
        def __init__(self, bodies):
            self.calls = []
            self._bodies = list(bodies)

        def open(self, request, *, timeout):
            self.calls.append({"request": request, "timeout": timeout})
            if not self._bodies:
                raise AssertionError("unexpected opener call")
            response = StubResponse(self._bodies.pop(0))
            response._url = request.full_url
            return response

    direct_root = tmp_path / "provider-diagnostic-direct"
    direct_root.mkdir()
    direct_auth = place_authorization("provider-diagnostic-direct")
    direct_transport = transport_type(
        network_authorization=network_authorization(
            direct_auth,
            "network-provider-diagnostic-direct",
            direct_root,
        )
    )
    opener = StubOpener([response_body()] * 3 + [response_body(status="0", infocode="10001", info=info_sentinel)])
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda proxy_mode: opener if proxy_mode == "direct" else pytest.fail("unexpected proxy"),
    )
    socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=direct_auth,
            staging_root=direct_root,
            credential=credential,
            transport=direct_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert socket_trace["realExternalCalls"] == []
    assert result["status"] == "failed"
    assert result["reasonCode"] == "amap_response_unsuccessful"
    assert result["providerDiagnostic"] == expected_diagnostic
    assert result["completedResponseCount"] == 3
    assert result["failedOrdinal"] == 4
    assert result["consumed"] is True
    assert result["promotable"] is False
    assert result["routeCalls"] == 0
    assert direct_transport.stub_place_calls == 4
    session_dir = direct_root / result["sessionDirectoryName"]
    partial = json.loads((session_dir / result["partialQuarantineFile"]).read_text(encoding="utf-8"))
    state = json.loads((session_dir / "session-state.json").read_text(encoding="utf-8"))
    assert partial["providerDiagnostic"] == expected_diagnostic
    assert state["providerDiagnostic"] == expected_diagnostic
    rendered = json.dumps(
        {"result": result, "partial": partial, "state": state},
        ensure_ascii=False,
        sort_keys=True,
    )
    for forbidden in (credential, info_sentinel, "https://", "http://", "header", "query"):
        assert forbidden not in rendered

    replay_auth = place_authorization("provider-diagnostic-replay")
    replay_transport = transport_type(
        network_authorization=network_authorization(
            replay_auth,
            "network-provider-diagnostic-replay",
            direct_root,
        )
    )
    with pytest.raises(execution_error, match="capture_envelope_already_consumed"):
        execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=replay_auth,
            staging_root=direct_root,
            credential=credential,
            transport=replay_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert replay_transport.stub_place_calls == 0
    assert len(opener.calls) == 4

    # The recorded acquisition transport must reuse key-level pacing and turn
    # AMap 10021 into a transport-scope cooldown before any later opener call.
    from src.services.amap_rate_limiter import (
        AmapPlaceCaptureCallGate,
        SlidingWindowRateLimiter,
    )

    pacing_now = 100.0
    pacing_sleeps: list[float] = []

    def pacing_time():
        return pacing_now

    def pacing_sleep(seconds):
        nonlocal pacing_now
        pacing_sleeps.append(seconds)
        pacing_now += seconds

    capture_gate = AmapPlaceCaptureCallGate(
        limiter=SlidingWindowRateLimiter(
            3,
            window_seconds=1.0,
            min_interval_seconds=0.35,
            time_fn=pacing_time,
            sleep_fn=pacing_sleep,
        ),
        cooldown_seconds=90.0,
        time_fn=pacing_time,
        include_stub_calls=True,
    )
    monkeypatch.setattr(
        transport_module,
        "AMAP_PLACE_CAPTURE_CALL_GATE",
        capture_gate,
    )
    rate_root = tmp_path / "provider-diagnostic-rate-limit"
    rate_root.mkdir()
    rate_auth = place_authorization("provider-diagnostic-rate-limit")
    rate_transport = transport_type(
        network_authorization=network_authorization(
            rate_auth,
            "network-provider-diagnostic-rate-limit",
            rate_root,
        )
    )
    rate_opener = StubOpener([response_body()] * 6 + [response_body(status="0", infocode="10021", info=info_sentinel)])
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda proxy_mode: rate_opener if proxy_mode == "direct" else pytest.fail("unexpected proxy"),
    )
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        rate_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=rate_auth,
            staging_root=rate_root,
            credential=credential,
            transport=rate_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert rate_result["reasonCode"] == "amap_response_unsuccessful"
    assert rate_result["providerDiagnostic"]["infocode"] == "10021"
    assert rate_result["attemptedPlaceCalls"] == 7
    assert rate_result["completedResponseCount"] == 6
    assert rate_result["failedOrdinal"] == 7
    assert rate_result["consumed"] is True
    assert rate_result["promotable"] is False
    assert rate_result["routeCalls"] == 0
    assert rate_result["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    assert len(rate_opener.calls) == 7
    assert pacing_sleeps == pytest.approx([0.35] * 6)
    assert [parse_qs(urlsplit(call["request"].full_url).query)["keywords"][0] for call in rate_opener.calls] == [
        request["params"]["keywords"] for request in outbound[:7]
    ]
    rate_session = rate_root / rate_result["sessionDirectoryName"]
    assert len(list(rate_session.glob("failed-partial-quarantine.json"))) == 1

    cooldown_root = tmp_path / "provider-diagnostic-cooldown"
    cooldown_root.mkdir()
    cooldown_auth = place_authorization("provider-diagnostic-cooldown")
    cooldown_transport = transport_type(
        network_authorization=network_authorization(
            cooldown_auth,
            "network-provider-diagnostic-cooldown",
            cooldown_root,
        )
    )
    cooldown_opener = StubOpener([response_body()] * len(outbound))
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda proxy_mode: cooldown_opener if proxy_mode == "direct" else pytest.fail("unexpected proxy"),
    )
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        cooldown_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=cooldown_auth,
            staging_root=cooldown_root,
            credential=credential,
            transport=cooldown_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert cooldown_result["reasonCode"] == "amap_key_transport_cooldown_active"
    assert cooldown_result["attemptedPlaceCalls"] == 0
    assert cooldown_result["completedResponseCount"] == 0
    assert cooldown_result["consumed"] is True
    assert cooldown_result["promotable"] is False
    assert cooldown_result["routeCalls"] == 0
    assert cooldown_opener.calls == []

    pacing_now += 90.0
    success_root = tmp_path / "provider-diagnostic-paced-success"
    success_root.mkdir()
    success_auth = place_authorization("provider-diagnostic-paced-success")
    success_transport = transport_type(
        network_authorization=network_authorization(
            success_auth,
            "network-provider-diagnostic-paced-success",
            success_root,
        )
    )
    success_opener = StubOpener([response_body()] * len(outbound))
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda proxy_mode: success_opener if proxy_mode == "direct" else pytest.fail("unexpected proxy"),
    )
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        success_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=success_auth,
            staging_root=success_root,
            credential=credential,
            transport=success_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert success_result["status"] == "completed"
    assert success_result["attemptedPlaceCalls"] == len(outbound)
    assert success_result["completedResponseCount"] == len(outbound)
    assert success_result["requestCount"] == len(outbound) == allowlist["requestCount"]
    assert len(outbound) > 0
    assert success_result["routeCalls"] == 0
    assert len(success_opener.calls) == len(outbound)
    assert [parse_qs(urlsplit(call["request"].full_url).query)["keywords"][0] for call in success_opener.calls] == [
        request["params"]["keywords"] for request in outbound
    ]

    nonlimit_gate = AmapPlaceCaptureCallGate(
        limiter=SlidingWindowRateLimiter(
            3,
            window_seconds=1.0,
            min_interval_seconds=0.35,
            time_fn=pacing_time,
            sleep_fn=pacing_sleep,
        ),
        cooldown_seconds=90.0,
        time_fn=pacing_time,
        include_stub_calls=True,
    )
    monkeypatch.setattr(
        transport_module,
        "AMAP_PLACE_CAPTURE_CALL_GATE",
        nonlimit_gate,
    )
    nonlimit_root = tmp_path / "provider-diagnostic-non-rate-limit"
    nonlimit_root.mkdir()
    nonlimit_auth = place_authorization("provider-diagnostic-non-rate-limit")
    nonlimit_transport = transport_type(
        network_authorization=network_authorization(
            nonlimit_auth,
            "network-provider-diagnostic-non-rate-limit",
            nonlimit_root,
        )
    )
    nonlimit_opener = StubOpener([response_body(status="0", infocode="10020", info=info_sentinel)])
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda proxy_mode: nonlimit_opener if proxy_mode == "direct" else pytest.fail("unexpected proxy"),
    )
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        nonlimit_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=nonlimit_auth,
            staging_root=nonlimit_root,
            credential=credential,
            transport=nonlimit_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert nonlimit_result["reasonCode"] == "amap_response_unsuccessful"
    assert nonlimit_result["providerDiagnostic"]["infocode"] == "10020"
    assert len(nonlimit_opener.calls) == 1
    # 10020 is still a terminal business failure for this session, but it
    # must not be guessed to be the key-level 10021 cooldown condition.
    nonlimit_gate.before_external_call(production_http_active=False)

    preparation_root = tmp_path / "provider-diagnostic-cli-preparation"
    preparation_root.mkdir()
    cli_root = preparation_root / "empty-child"
    cli_root.mkdir()
    cli_auth = place_authorization("provider-diagnostic-cli")
    cli_network = network_authorization(
        cli_auth,
        "network-provider-diagnostic-cli",
        cli_root,
    )

    def write_json_input(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        details = path.lstat()
        assert stat.S_ISREG(details.st_mode)
        assert not path.is_symlink()
        return path

    input_paths = {
        "--runtime-evidence": write_json_input("provider-diagnostic-runtime.json", runtime_evidence),
        "--manifest": write_json_input("provider-diagnostic-manifest.json", manifest),
        "--envelope": write_json_input("provider-diagnostic-envelope.json", envelope),
        "--place-authorization": write_json_input("provider-diagnostic-place.json", cli_auth),
        "--network-authorization": write_json_input("provider-diagnostic-network.json", cli_network),
    }
    cli_argv = [item for flag, path in input_paths.items() for item in (flag, str(path))] + [
        "--staging-root",
        str(cli_root),
        "--execute-place-capture",
    ]
    monkeypatch.setattr(cli, "CASES_DIR", cases_dir)
    monkeypatch.setattr(cli, "ACQUISITION_PREPARATION_ROOT", preparation_root)
    monkeypatch.setattr(cli, "_read_map_provider_key", lambda: credential)
    monkeypatch.setattr(
        cli,
        "execute_zero_network_place_capture",
        lambda **_kwargs: copy.deepcopy(result),
    )
    cli_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(cli_socket_trace):
        assert cli.main(cli_argv) == 1
    cli_lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(cli_lines) == 1
    cli_payload = json.loads(cli_lines[0])
    assert cli_socket_trace["realExternalCalls"] == []
    assert cli_payload["status"] == "PLACE_CAPTURE_EXECUTION_FAILED"
    assert cli_payload["reasonCode"] == "amap_response_unsuccessful"
    assert cli_payload["providerDiagnostic"] == expected_diagnostic
    cli_rendered = json.dumps(cli_payload, ensure_ascii=False, sort_keys=True)
    for forbidden in (credential, info_sentinel, "https://", "http://", "header", "query"):
        assert forbidden not in cli_rendered
    assert runtime_evidence == runtime_before
    assert hashlib.sha256(source_fixture.read_bytes()).hexdigest() == fixture_sha256_before
    assert hashlib.sha256(dedicated_case.read_bytes()).hexdigest() == case_sha256_before


def test_zero_network_place_capture_executor_consumes_exact_envelope_once_and_quarantines_sanitized_responses(
    tmp_path,
    monkeypatch,
):
    import hashlib
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from importlib.util import find_spec
    from pathlib import Path

    cases_dir = tmp_path / "fixed-goal-zero-network-place-executor"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    build_envelope = getattr(
        recorded_fixture_capture_module,
        "build_zero_network_capture_session_envelope",
    )
    validate_envelope = getattr(
        recorded_fixture_capture_module,
        "validate_zero_network_capture_session_envelope",
    )
    envelope = build_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    validate_envelope(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    assert envelope["status"] == "prepared"
    assert envelope["authorizationStatus"] == "awaiting_explicit_authorization"
    assert envelope["captureScope"] == "place_only"

    module_name = "backend.evals.recorded_fixture_place_capture_executor"
    module_spec = find_spec(module_name)
    executor_module = import_module(module_name) if module_spec is not None else None
    execute_place_capture = getattr(
        executor_module,
        "execute_zero_network_place_capture",
        None,
    )
    assert callable(execute_place_capture), (
        "independent zero-network Place executor / one-shot transport contract "
        "is missing after the sealed Place-only envelope validates"
    )
    execution_error = getattr(executor_module, "PlaceCaptureExecutionError", None)
    assert isinstance(execution_error, type)
    scripted_transport_type = getattr(
        executor_module,
        "ScriptedFakePlaceTransport",
        None,
    )
    assert isinstance(scripted_transport_type, type)

    fixed_now = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)
    allowlist = envelope["exactPlaceRequestAllowlist"]
    request_count = allowlist["requestCount"]
    authorization = {
        "authorizationId": "place-only-auth-success",
        "scope": "place_only",
        "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
        "envelopeFingerprint": envelope["envelopeFingerprint"],
        "contentFingerprint": envelope["contentFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
        "maxCalls": request_count,
        "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
    }
    secret = "test-only-amap-credential-MUST-NOT-PERSIST"

    def scripted_responses(*, redirect_at=None):
        responses = []
        for ordinal in range(1, request_count + 1):
            payload = {
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "count": "1",
                "pois": [
                    {
                        "id": f"B000A{ordinal:06d}",
                        "name": f"fake-place-{ordinal}",
                        "location": f"116.{ordinal:06d},39.{ordinal:06d}",
                    }
                ],
            }
            responses.append(
                {
                    "statusCode": 200,
                    "contentType": "application/json",
                    "redirected": redirect_at == ordinal,
                    "body": json.dumps(payload, ensure_ascii=False),
                }
            )
        return responses

    fixture_path = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixture_sha256_before = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    runtime_before = copy.deepcopy(runtime_evidence)
    success_root = tmp_path / "successful-place-quarantine"
    success_root.mkdir()
    success_transport = scripted_transport_type(
        responses=scripted_responses(),
    )

    result = execute_place_capture(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        authorization=authorization,
        staging_root=success_root,
        credential=secret,
        transport=success_transport,
        cases_dir=cases_dir,
        now=fixed_now,
    )

    endpoint_paths = {
        "place/text": "/v3/place/text",
        "place/around": "/v3/place/around",
    }
    expected_outbound = []
    for ordinal, request in enumerate(allowlist["requests"], start=1):
        expected_outbound.append(
            {
                "method": "GET",
                "scheme": "https",
                "host": "restapi.amap.com",
                "path": endpoint_paths[request["endpoint"]],
                "params": copy.deepcopy(request["sanitizedParams"]),
                "allowRedirects": False,
                "ordinal": ordinal,
                "requestFingerprint": request["requestFingerprint"],
                "auditFingerprint": request["auditFingerprint"],
            }
        )
    assert success_transport.calls == expected_outbound
    assert len(success_transport.calls) == request_count
    assert success_transport.credentials == [secret] * request_count
    assert success_transport.first_call_state["state"] == "consumed_in_progress"
    assert success_transport.first_call_state["consumed"] is True
    assert success_transport.first_call_state["attemptedFakeTransportCalls"] == 0
    assert result["status"] == "completed"
    assert result["consumed"] is True
    assert result["promotable"] is False
    assert result["requestCount"] == request_count
    assert result["completedResponseCount"] == request_count
    assert result["routeCaptureAuthorized"] is False
    assert result["routeCalls"] == 0
    assert result["fakeTransportCalls"] == request_count
    assert result["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    assert Path(result["sessionDirectoryName"]).name == result["sessionDirectoryName"]
    assert not Path(result["sessionDirectoryName"]).is_absolute()

    session_directory = success_root / result["sessionDirectoryName"]
    success_bundle_path = session_directory / result["quarantineBundleFile"]
    assert success_bundle_path.is_file()
    assert result["partialQuarantineFile"] is None
    bundle = json.loads(success_bundle_path.read_text(encoding="utf-8"))
    assert bundle["promotable"] is False
    assert bundle["quarantine"]["transportKind"] == "injected_fake"
    assert bundle["quarantine"]["realExternalCapture"] is False
    assert bundle["quarantine"]["routeCaptureAuthorized"] is False
    assert bundle["quarantine"]["routeCalls"] == 0
    assert bundle["quarantine"]["fakeTransportCalls"] == request_count
    assert len(bundle["responses"]) == request_count
    for ordinal, (request, response_record) in enumerate(
        zip(allowlist["requests"], bundle["responses"]),
        start=1,
    ):
        assert response_record["ordinal"] == ordinal
        assert response_record["requestFingerprint"] == request["requestFingerprint"]
        assert response_record["auditFingerprint"] == request["auditFingerprint"]
        assert response_record["request"] == {
            "endpoint": endpoint_paths[request["endpoint"]],
            "params": request["sanitizedParams"],
        }
        canonical_response = json.dumps(
            response_record["response"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        recomputed_hash = hashlib.sha256(canonical_response).hexdigest().upper()
        assert response_record["responseSha256"] == recomputed_hash
        assert response_record["responseSha256"] == response_record["responseSha256"].upper()

    forbidden_persisted_keys = {
        "key",
        "api_key",
        "apikey",
        "access_key",
        "accesskey",
        "secret",
        "authorization",
        "cookie",
        "cookies",
        "proxy",
        "proxies",
        "token",
        "access_token",
        "headers",
    }

    def persisted_keys(value):
        keys = set()
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                keys.update(str(key).casefold() for key in item)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return keys

    persisted_payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in session_directory.iterdir() if path.is_file()
    ]
    persisted_rendered = json.dumps(
        {"result": result, "files": persisted_payloads},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert secret not in persisted_rendered
    assert "https://" not in persisted_rendered
    assert "?key=" not in persisted_rendered
    assert str(tmp_path).casefold() not in persisted_rendered.casefold()
    assert forbidden_persisted_keys.isdisjoint(persisted_keys({"result": result, "files": persisted_payloads}))
    assert runtime_evidence == runtime_before
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == (fixture_sha256_before)

    calls_before_replay = len(success_transport.calls)
    with pytest.raises(execution_error) as replay_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=authorization,
            staging_root=success_root,
            credential=secret,
            transport=success_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert replay_error.value.reason_code == "authorization_already_consumed"
    assert len(success_transport.calls) == calls_before_replay

    class ForgedTransportMarker:
        transport_kind = "injected_fake"

        def __init__(self):
            self.calls = []

        def __call__(self, *, request, credential):
            self.calls.append((request, credential))
            raise AssertionError("a forged transport marker must never run")

    forged_root = tmp_path / "forged-transport-marker"
    forged_root.mkdir()
    forged_transport = ForgedTransportMarker()
    forged_authorization = {
        **authorization,
        "authorizationId": "place-only-auth-forged-transport",
    }
    with pytest.raises(execution_error) as forged_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=forged_authorization,
            staging_root=forged_root,
            credential=secret,
            transport=forged_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert forged_error.value.reason_code == "exact_place_transport_required"
    assert forged_transport.calls == []
    assert list(forged_root.iterdir()) == []

    collision_root = tmp_path / "credential-binding-collision"
    collision_root.mkdir()
    collision_transport = scripted_transport_type(responses=scripted_responses())
    collision_authorization = {
        **authorization,
        "authorizationId": secret,
    }
    with pytest.raises(execution_error) as collision_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=collision_authorization,
            staging_root=collision_root,
            credential=secret,
            transport=collision_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert collision_error.value.reason_code == "credential_binding_collision"
    assert collision_transport.calls == []
    assert list(collision_root.iterdir()) == []

    sync_failure_root = tmp_path / "durable-claim-sync-failure"
    sync_failure_root.mkdir()
    sync_transport = scripted_transport_type(responses=scripted_responses())
    sync_authorization = {
        **authorization,
        "authorizationId": "place-only-auth-sync-failure",
    }
    directory_lease_type = getattr(executor_module, "_DirectoryLease")
    original_sync = directory_lease_type.sync

    def fail_directory_sync(_lease):
        raise OSError("injected-directory-sync-failure")

    monkeypatch.setattr(directory_lease_type, "sync", fail_directory_sync)
    with pytest.raises(execution_error) as sync_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=sync_authorization,
            staging_root=sync_failure_root,
            credential=secret,
            transport=sync_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert sync_error.value.reason_code == "session_claim_or_sync_failed"
    assert sync_transport.calls == []
    assert len(list(sync_failure_root.glob("place-envelope-claim-*.json"))) == 1
    assert len(list(sync_failure_root.glob("place-session-*"))) == 0
    monkeypatch.setattr(directory_lease_type, "sync", original_sync)
    sync_replay_transport = scripted_transport_type(responses=scripted_responses())
    with pytest.raises(execution_error) as sync_replay_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=sync_authorization,
            staging_root=sync_failure_root,
            credential=secret,
            transport=sync_replay_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert sync_replay_error.value.reason_code == "capture_envelope_already_consumed"
    assert sync_replay_transport.calls == []

    symlink_target = tmp_path / "symlink-staging-target"
    symlink_target.mkdir()
    symlink_root = tmp_path / "symlink-staging-root"
    try:
        symlink_root.symlink_to(symlink_target, target_is_directory=True)
    except OSError:
        symlink_created = False
    else:
        symlink_created = True
    if symlink_created:
        symlink_transport = scripted_transport_type(responses=scripted_responses())
        symlink_authorization = {
            **authorization,
            "authorizationId": "place-only-auth-symlink-root",
        }
        with pytest.raises(execution_error) as symlink_error:
            execute_place_capture(
                envelope=envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                authorization=symlink_authorization,
                staging_root=symlink_root,
                credential=secret,
                transport=symlink_transport,
                cases_dir=cases_dir,
                now=fixed_now,
            )
        assert symlink_error.value.reason_code == "staging_root_invalid"
        assert symlink_transport.calls == []
        assert list(symlink_target.iterdir()) == []

    identity_root = tmp_path / "identity-bound-staging-root"
    identity_root.mkdir()
    displaced_root = tmp_path / "identity-bound-staging-root-displaced"
    identity_transport = scripted_transport_type(responses=scripted_responses())
    identity_authorization = {
        **authorization,
        "authorizationId": "place-only-auth-root-identity-swap",
    }
    original_enter = directory_lease_type.__enter__
    identity_swap_performed = False

    def replace_root_before_lease(lease):
        nonlocal identity_swap_performed
        if not identity_swap_performed and lease.path == identity_root and lease._parent is None:
            identity_root.rename(displaced_root)
            identity_root.mkdir()
            identity_swap_performed = True
        return original_enter(lease)

    monkeypatch.setattr(directory_lease_type, "__enter__", replace_root_before_lease)
    with pytest.raises(execution_error) as identity_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=identity_authorization,
            staging_root=identity_root,
            credential=secret,
            transport=identity_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert identity_error.value.reason_code == "session_claim_or_sync_failed"
    assert identity_swap_performed is True
    assert identity_transport.calls == []
    assert list(identity_root.iterdir()) == []
    monkeypatch.setattr(directory_lease_type, "__enter__", original_enter)
    identity_root.rmdir()
    displaced_root.rename(identity_root)

    def wrong_source(value):
        value["sourceFingerprint"] = "0" * 64

    def wrong_envelope(value):
        value["envelopeFingerprint"] = "1" * 64

    def wrong_content(value):
        value["contentFingerprint"] = "2" * 64

    def wrong_allowlist(value):
        value["exactPlaceRequestAllowlistFingerprint"] = "3" * 64

    def wrong_scope(value):
        value["scope"] = "route"

    def wrong_max_calls(value):
        value["maxCalls"] = request_count + 1

    def expired(value):
        value["issuedAt"] = (fixed_now - timedelta(hours=2)).isoformat()
        value["expiresAt"] = (fixed_now - timedelta(hours=1)).isoformat()

    def missing_authorization_id(value):
        value.pop("authorizationId")

    invalid_authorizations = (
        ("wrong-source", wrong_source),
        ("wrong-envelope", wrong_envelope),
        ("wrong-content", wrong_content),
        ("wrong-allowlist", wrong_allowlist),
        ("wrong-scope", wrong_scope),
        ("wrong-max-calls", wrong_max_calls),
        ("expired", expired),
        ("missing-id", missing_authorization_id),
    )
    for label, mutate in invalid_authorizations:
        invalid_root = tmp_path / f"invalid-authorization-{label}"
        invalid_root.mkdir()
        invalid_authorization = copy.deepcopy(authorization)
        invalid_authorization["authorizationId"] = f"invalid-{label}"
        mutate(invalid_authorization)
        rejecting_transport = scripted_transport_type(responses=scripted_responses())
        with pytest.raises(execution_error):
            execute_place_capture(
                envelope=envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                authorization=invalid_authorization,
                staging_root=invalid_root,
                credential=secret,
                transport=rejecting_transport,
                cases_dir=cases_dir,
                now=fixed_now,
            )
        assert rejecting_transport.calls == []
        assert list(invalid_root.iterdir()) == []

    def resign_envelope(value):
        fingerprint_material = copy.deepcopy(value)
        fingerprint_material.pop("captureSessionDisplayId", None)
        fingerprint_material.pop("contentFingerprint", None)
        fingerprint_material.pop("envelopeFingerprint", None)
        fingerprint = canonical_sha256(fingerprint_material)
        value["contentFingerprint"] = fingerprint
        value["envelopeFingerprint"] = fingerprint

    def unknown_endpoint(value):
        value["exactPlaceRequestAllowlist"]["requests"][0]["endpoint"] = "place/unknown"

    def unknown_parameter(value):
        value["exactPlaceRequestAllowlist"]["requests"][0]["sanitizedParams"]["unexpected"] = "fail-closed"

    for label, mutate in (
        ("unknown-endpoint", unknown_endpoint),
        ("unknown-param", unknown_parameter),
    ):
        tampered_root = tmp_path / f"tampered-envelope-{label}"
        tampered_root.mkdir()
        tampered_envelope = copy.deepcopy(envelope)
        mutate(tampered_envelope)
        resign_envelope(tampered_envelope)
        rejecting_transport = scripted_transport_type(responses=scripted_responses())
        with pytest.raises(ValueError, match="tampered"):
            execute_place_capture(
                envelope=tampered_envelope,
                manifest=manifest,
                runtime_evidence=runtime_evidence,
                authorization=authorization,
                staging_root=tampered_root,
                credential=secret,
                transport=rejecting_transport,
                cases_dir=cases_dir,
                now=fixed_now,
            )
        assert rejecting_transport.calls == []
        assert list(tampered_root.iterdir()) == []

    failed_root = tmp_path / "failed-place-quarantine"
    failed_root.mkdir()
    failed_ordinal = min(2, request_count)
    failing_transport = scripted_transport_type(
        responses=scripted_responses(),
        fail_at=failed_ordinal,
    )
    failed_authorization = {
        **authorization,
        "authorizationId": "place-only-auth-partial-failure",
    }
    failed_result = execute_place_capture(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        authorization=failed_authorization,
        staging_root=failed_root,
        credential=secret,
        transport=failing_transport,
        cases_dir=cases_dir,
        now=fixed_now,
    )
    assert failed_result["status"] == "failed"
    assert failed_result["reasonCode"] == "transport_error"
    assert failed_result["consumed"] is True
    assert failed_result["promotable"] is False
    assert failed_result["fakeTransportCalls"] == failed_ordinal
    assert len(failing_transport.calls) == failed_ordinal
    assert failing_transport.calls == expected_outbound[:failed_ordinal]
    failed_session = failed_root / failed_result["sessionDirectoryName"]
    partial_path = failed_session / failed_result["partialQuarantineFile"]
    assert partial_path.is_file()
    assert not (failed_session / "place-capture-quarantine.json").exists()
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    partial_rendered = json.dumps(partial, ensure_ascii=False, sort_keys=True)
    assert partial["promotable"] is False
    assert partial["completedResponseCount"] == failed_ordinal - 1
    assert partial["fakeTransportCalls"] == failed_ordinal
    assert secret not in partial_rendered
    assert "fake failure leaked" not in partial_rendered
    failed_persisted_payloads = [
        json.loads(path.read_text(encoding="utf-8")) for path in failed_session.iterdir() if path.is_file()
    ]
    failed_persisted_rendered = json.dumps(
        {"result": failed_result, "files": failed_persisted_payloads},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert secret not in failed_persisted_rendered
    assert "fake failure leaked" not in failed_persisted_rendered
    assert "https://" not in failed_persisted_rendered
    assert str(tmp_path).casefold() not in failed_persisted_rendered.casefold()
    assert forbidden_persisted_keys.isdisjoint(
        persisted_keys({"result": failed_result, "files": failed_persisted_payloads})
    )

    failed_call_count = len(failing_transport.calls)
    with pytest.raises(execution_error) as failed_replay_error:
        execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=failed_authorization,
            staging_root=failed_root,
            credential=secret,
            transport=failing_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert failed_replay_error.value.reason_code == "authorization_already_consumed"
    assert len(failing_transport.calls) == failed_call_count

    redirect_root = tmp_path / "redirect-place-quarantine"
    redirect_root.mkdir()
    redirect_transport = scripted_transport_type(responses=scripted_responses(redirect_at=1))
    redirect_authorization = {
        **authorization,
        "authorizationId": "place-only-auth-redirect-failure",
    }
    redirect_result = execute_place_capture(
        envelope=envelope,
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        authorization=redirect_authorization,
        staging_root=redirect_root,
        credential=secret,
        transport=redirect_transport,
        cases_dir=cases_dir,
        now=fixed_now,
    )
    assert redirect_result["status"] == "failed"
    assert redirect_result["reasonCode"] == "transport_redirect_forbidden"
    assert redirect_result["fakeTransportCalls"] == 1
    assert len(redirect_transport.calls) == 1
    assert redirect_result["partialQuarantineFile"]
    assert not (redirect_root / redirect_result["sessionDirectoryName"] / "place-capture-quarantine.json").exists()

    def add_response_field(responses, key, value):
        payload = json.loads(responses[0]["body"])
        payload[key] = value
        responses[0]["body"] = json.dumps(payload, ensure_ascii=False)

    unsafe_response_cases = (
        ("set-cookie", "Set-Cookie", "cookie-value", "response_secret_field_forbidden"),
        ("x-api-key", "x-api-key", "api-key-value", "response_secret_field_forbidden"),
        ("access-token", "accessToken", "token-value", "response_secret_field_forbidden"),
        ("proxy-url", "proxyUrl", "proxy-value", "response_secret_field_forbidden"),
        (
            "full-url",
            "providerReference",
            "https://example.invalid/place?key=must-not-persist",
            "response_full_url_forbidden",
        ),
    )
    for label, key, value, expected_reason in unsafe_response_cases:
        unsafe_root = tmp_path / f"unsafe-response-{label}"
        unsafe_root.mkdir()
        unsafe_responses = scripted_responses()
        add_response_field(unsafe_responses, key, value)
        unsafe_transport = scripted_transport_type(responses=unsafe_responses)
        unsafe_authorization = {
            **authorization,
            "authorizationId": f"place-only-auth-unsafe-{label}",
        }
        unsafe_result = execute_place_capture(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=unsafe_authorization,
            staging_root=unsafe_root,
            credential=secret,
            transport=unsafe_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
        assert unsafe_result["status"] == "failed"
        assert unsafe_result["reasonCode"] == expected_reason
        assert unsafe_result["fakeTransportCalls"] == 1
        assert len(unsafe_transport.calls) == 1
        unsafe_session = unsafe_root / unsafe_result["sessionDirectoryName"]
        assert not (unsafe_session / "place-capture-quarantine.json").exists()
        unsafe_persisted = "".join(
            path.read_text(encoding="utf-8") for path in unsafe_session.iterdir() if path.is_file()
        )
        assert value not in unsafe_persisted

    case_result = runtime_evidence["cases"][0]
    assert case_result["realExternalCallLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
    }
    assert case_result["persistedClarificationChoiceReplay"]["writeDelta"] == {
        "version": 0,
        "patch": 0,
        "route": 0,
    }
    assert case_result["persistedClarificationChoiceReplay"]["recordedFixtureCaptureDelta"] == 0
    assert runtime_evidence == runtime_before
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == (fixture_sha256_before)


def test_capture_preflight_allows_place_identity_phase_before_route_budget_derivation(tmp_path):
    cases_dir = tmp_path / "fixed-goal-place-identity-preflight"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )

    manifest = build_capture_preflight_manifest(
        runtime_evidence=result,
        cases_dir=cases_dir,
    )
    manifest_case = manifest["cases"][0]
    assert manifest_case["logicalRouteAuthorization"]["status"] == "ready"
    assert manifest_case["logicalRouteAuthorization"]["budgetState"] == "not_derived"
    assert manifest_case["logicalRouteAuthorization"]["budgetReason"] == ("canonical_adjacent_pairs_not_materialized")
    assert manifest_case["placeIdentityCaptureEligible"] is True
    assert manifest_case["routePairFreezeEligible"] is False
    assert manifest_case["captureBlockedReasons"] == []
    assert manifest_case["deferredRoutePrerequisites"] == ["route_budget_not_derived"]
    assert manifest["captureState"] == "ready_for_place_identity_capture"
    assert manifest["externalCaptureSession"] == {
        "id": None,
        "consumed": False,
        "recordedFixtureCaptureUsed": 0,
        "externalPlaceCalls": 0,
        "externalRouteCalls": 0,
        "ledgerDelta": 0,
    }
    assert manifest["networkCalls"] == 0

    inconsistent_route_discovery = copy.deepcopy(result)
    inconsistent_case = inconsistent_route_discovery["cases"][0]
    inconsistent_case["recordedCaptureSemanticTrace"]["legacyMockAmapPresent"] = False
    inconsistent_case["recordedRouteDiscovery"]["captureEligiblePairs"] = []
    inconsistent_case["recordedRouteDiscovery"]["captureBlockedReasons"] = ["offline_mock_amap_poi_provenance"]
    blocked_manifest = build_capture_preflight_manifest(
        runtime_evidence=inconsistent_route_discovery,
        cases_dir=cases_dir,
    )
    assert blocked_manifest["captureState"] == "preflight_blocked"
    assert blocked_manifest["cases"][0]["placeIdentityCaptureEligible"] is False
    assert "offline_mock_amap_poi_provenance" in blocked_manifest["cases"][0]["captureBlockedReasons"]


def test_dedicated_capture_trace_consumes_route_detour_opaque_choice_without_network(tmp_path):
    cases_dir = tmp_path / "fixed-goal-route-choice"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    replay_db = tmp_path / "fixed-goal-route-choice.db"
    result = run_offline_eval(
        cases_dir=cases_dir,
        database_url=f"sqlite:///{replay_db}",
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )

    case = result["cases"][0]
    replay = case["persistedClarificationChoiceReplay"]
    assert replay["persistedChoiceCount"] >= 2
    assert replay["sourceAssistantTurnId"] == replay["checkpointSourceAssistantTurnId"]
    assert replay["sourceAssistantTurnId"] == replay["submittedContext"]["selectedAgentChoice"]["sourceAssistantTurnId"]
    assert replay["choiceId"] == replay["submittedContext"]["selectedAgentChoice"]["choiceId"]
    assert replay["submittedContext"] == {
        "selectedAgentChoice": {
            "sourceAssistantTurnId": replay["sourceAssistantTurnId"],
            "choiceId": replay["choiceId"],
        }
    }
    assert replay["selectedSemanticValueFingerprint"] == replay["requestedSemanticValueFingerprint"]
    assert replay["resolvedAnswer"]["source"] == "structured_option"
    assert replay["resolvedAnswerMatchesPersistedOption"] is True
    assert replay["resolvedAnswer"]["semanticValueFingerprint"] == replay["selectedSemanticValueFingerprint"]
    route_contract = replay["routeDecisionContract"]
    assert route_contract["status"] == "ready"
    assert route_contract["missingFields"] == []
    assert route_contract["preferredMode"] == "transit"
    assert route_contract["detourToleranceSource"] == "controller_semantic_choice"
    assert len(route_contract["fingerprint"]) == 64
    assert replay["nightViewOccurrenceCount"] == 1
    assert replay["publicCityViewExperienceSpecCount"] == 1
    # The resolved route choice now completes the server-authored meal
    # ExperienceSpec; the public-city-view spec remains independently scoped.
    assert replay["initialExperienceSpecDetourToleranceCount"] == 0
    assert replay["experienceSpecDetourToleranceCount"] == 1
    assert replay["choiceExecutionStatus"] == "succeeded"
    assert replay["persistedRequestChoiceMatchesSubmittedIds"] is True
    assert replay["activeVersionId"] is None
    assert replay["writeDelta"] == {"version": 0, "patch": 0, "route": 0}
    assert replay["realExternalNetworkCallCount"] == 0
    assert replay["recordedFixtureCaptureDelta"] == 0
    assert case["recordedRouteDiscovery"]["networkCalls"] == 0
    assert case["offlineControllerStubInvocationCount"] >= 2
    assert case["realExternalControllerCallCount"] == 0
    assert all(case["offlineProviderIsolation"].values())
    assert case["globalNetworkSentinelAttemptCount"] == 0
    assert case["realExternalCallLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
    }
    rendered_submission = json.dumps(replay["submittedContext"], ensure_ascii=False)
    assert '"maxGeneralizedCostDelta"' not in rendered_submission
    assert '"maxDetourRatio"' not in rendered_submission
    for profile_run in case["recordedCaptureSemanticTrace"]["profileRuns"]:
        consumer_route_contract = profile_run["consumerAdmissionInput"]["routeContext"]["routeDecisionContract"]
        assert consumer_route_contract["fingerprint"] == route_contract["fingerprint"]
        assert set(consumer_route_contract["detourTolerance"]) == {
            "maxGeneralizedCostDelta",
            "maxDetourRatio",
        }
        assert all(float(value) > 0 for value in consumer_route_contract["detourTolerance"].values())
    rendered_case = json.dumps(case, ensure_ascii=False)
    assert offline_eval_module._opaque_clarification_wire_payload(
        replay["sourceAssistantTurnId"], replay["choiceId"]
    ) == {"content": "", "context": replay["submittedContext"]}

    socket_trace = {"realExternalCalls": []}
    original_create_connection = offline_eval_module.socket.create_connection
    with pytest.raises(AssertionError, match="blocked real external network"):
        with offline_eval_module._offline_external_network_sentinel(socket_trace) as sentinel:
            assert sentinel["active"] is True
            offline_eval_module.socket.create_connection(("127.0.0.1", 9))
    assert offline_eval_module.socket.create_connection is original_create_connection
    assert socket_trace["realExternalCalls"] == [{"provider": "network", "operation": "socket.create_connection"}]

    socket_send_trace = {"realExternalCalls": []}
    original_send = offline_eval_module.socket.socket.send
    with offline_eval_module.socket.socket() as blocked_socket:
        with pytest.raises(AssertionError, match="blocked real external network"):
            with offline_eval_module._offline_external_network_sentinel(socket_send_trace) as sentinel:
                assert sentinel["active"] is True
                blocked_socket.send(b"offline-must-not-send")
    assert offline_eval_module.socket.socket.send is original_send
    assert socket_send_trace["realExternalCalls"] == [{"provider": "network", "operation": "socket.send"}]

    with offline_eval_module._open_db(replay_db) as connection:
        source_row = connection.execute(
            """SELECT session_id, agent_response_json, agent_request_json
               FROM conversation_turns WHERE id = ?""",
            (replay["sourceAssistantTurnId"],),
        ).fetchone()
        execution = connection.execute(
            """SELECT execution_turn_id FROM agent_choice_executions
               WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?""",
            (
                source_row["session_id"],
                replay["sourceAssistantTurnId"],
                replay["choiceId"],
            ),
        ).fetchone()
    source_response = json.loads(str(source_row["agent_response_json"] or "{}"))
    source_request = json.loads(str(source_row["agent_request_json"] or "{}"))
    selected_option = next(
        item
        for item in source_response["choiceOptions"]
        if isinstance(item, dict) and str(item.get("id") or "") == replay["choiceId"]
    )
    source_labels = [
        str(item.get("label") or "") for item in source_response["choiceOptions"] if isinstance(item, dict)
    ]
    assert all(label and label not in rendered_case for label in source_labels)
    persisted = {
        "choiceId": replay["choiceId"],
        "dimensionId": replay["dimensionId"],
        "checkpointFingerprint": source_response["clarificationCheckpoint"]["fingerprint"],
        "sourceUserTurnId": source_response["clarificationCheckpoint"]["sourceUserTurnId"],
        "checkpointSourceAssistantTurnId": replay["checkpointSourceAssistantTurnId"],
        "persistedChoiceCount": replay["persistedChoiceCount"],
        "semanticValue": selected_option["semanticValue"],
        "requestedSemanticValueFingerprint": replay["requestedSemanticValueFingerprint"],
        "selectedSemanticValueFingerprint": replay["selectedSemanticValueFingerprint"],
        "selectedOptionFingerprint": offline_eval_module._canonical_response_sha256(selected_option),
        "initialRequestIntentContract": source_request["requestIntentContract"],
    }
    post_kwargs = {
        "session_id": str(source_row["session_id"]),
        "source_assistant_turn_id": replay["sourceAssistantTurnId"],
        "response_assistant_turn_id": str(execution["execution_turn_id"]),
        "persisted": persisted,
        "submitted_context": replay["submittedContext"],
        "before_counts": {"version": 0, "patch": 0, "route": 0},
    }
    assert offline_eval_module._persisted_clarification_choice_result(replay_db, **post_kwargs)

    with offline_eval_module._open_db(replay_db) as connection:
        connection.execute(
            """UPDATE agent_choice_executions SET status = 'failed'
               WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?""",
            (str(source_row["session_id"]), replay["sourceAssistantTurnId"], replay["choiceId"]),
        )
        connection.commit()
    with pytest.raises(AssertionError, match="execution identity or persisted request is invalid"):
        offline_eval_module._persisted_clarification_choice_result(replay_db, **post_kwargs)

    with offline_eval_module._open_db(replay_db) as connection:
        connection.execute(
            """UPDATE agent_choice_executions SET status = 'succeeded'
               WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?""",
            (str(source_row["session_id"]), replay["sourceAssistantTurnId"], replay["choiceId"]),
        )
        response_row = connection.execute(
            "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
            (str(execution["execution_turn_id"]),),
        ).fetchone()
        tampered_request = json.loads(str(response_row["agent_request_json"] or "{}"))
        tampered_request["selectedAgentChoice"]["option"]["checkpointId"] = "checkpoint_tampered"
        connection.execute(
            "UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
            (json.dumps(tampered_request, ensure_ascii=False), str(execution["execution_turn_id"])),
        )
        connection.commit()
    with pytest.raises(AssertionError, match="execution identity or persisted request is invalid"):
        offline_eval_module._persisted_clarification_choice_result(replay_db, **post_kwargs)

    prepost_cases_dir = tmp_path / "fixed-goal-route-choice-prepost"
    prepost_cases_dir.mkdir()
    prepost_case = json.loads(dedicated_case.read_text(encoding="utf-8"))
    prepost_case["steps"] = prepost_case["steps"][:2]
    prepost_case["assertions"] = [
        {"path": "first.statusCode", "equals": 200},
        {"path": "first.version", "isNull": True},
    ]
    (prepost_cases_dir / dedicated_case.name).write_text(
        json.dumps(prepost_case, ensure_ascii=False),
        encoding="utf-8",
    )
    prepost_db = tmp_path / "route-choice-prepost.db"
    run_offline_eval(
        cases_dir=prepost_cases_dir,
        database_url=f"sqlite:///{prepost_db}",
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    with offline_eval_module._open_db(prepost_db) as connection:
        source_row = connection.execute(
            """SELECT id, session_id, turn_index, agent_response_json, created_at, updated_at
               FROM conversation_turns
               WHERE role = 'assistant' AND status = 'active'
               ORDER BY turn_index DESC LIMIT 1"""
        ).fetchone()
    source_id = str(source_row["id"])
    session_id = str(source_row["session_id"])
    selector = (
        prepost_case["steps"][2]["semanticValueFingerprint"]
        if len(prepost_case["steps"]) > 2
        else (json.loads(dedicated_case.read_text(encoding="utf-8"))["steps"][2]["semanticValueFingerprint"])
    )
    valid_persisted = offline_eval_module._persisted_clarification_choice_for_replay(
        prepost_db,
        session_id=session_id,
        source_assistant_turn_id=source_id,
        dimension_id="route_decision.detour_tolerance",
        semantic_value_fingerprint=selector,
    )
    assert valid_persisted["choiceId"]

    original_payload = json.loads(str(source_row["agent_response_json"]))
    tampered_fingerprint = copy.deepcopy(original_payload)
    tampered_fingerprint["clarificationCheckpoint"]["fingerprint"] = "0" * 64
    with offline_eval_module._open_db(prepost_db) as connection:
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(tampered_fingerprint, ensure_ascii=False), source_id),
        )
        connection.commit()
    with pytest.raises(AssertionError, match="checkpoint or request fingerprint is invalid"):
        offline_eval_module._persisted_clarification_choice_for_replay(
            prepost_db,
            session_id=session_id,
            source_assistant_turn_id=source_id,
            dimension_id="route_decision.detour_tolerance",
            semantic_value_fingerprint=selector,
        )

    tampered_request_fingerprint = copy.deepcopy(original_payload)
    checkpoint = tampered_request_fingerprint["clarificationCheckpoint"]
    checkpoint["requestFingerprint"] = "0" * 64
    checkpoint["fingerprint"] = offline_eval_module._canonical_response_sha256(
        {key: value for key, value in checkpoint.items() if key != "fingerprint"}
    )
    with offline_eval_module._open_db(prepost_db) as connection:
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(tampered_request_fingerprint, ensure_ascii=False), source_id),
        )
        connection.commit()
    with pytest.raises(AssertionError, match="checkpoint or request fingerprint is invalid"):
        offline_eval_module._persisted_clarification_choice_for_replay(
            prepost_db,
            session_id=session_id,
            source_assistant_turn_id=source_id,
            dimension_id="route_decision.detour_tolerance",
            semantic_value_fingerprint=selector,
        )

    tampered_question = copy.deepcopy(original_payload)
    checkpoint = tampered_question["clarificationCheckpoint"]
    checkpoint["question"]["options"][0].pop("semanticValue", None)
    checkpoint["fingerprint"] = offline_eval_module._canonical_response_sha256(
        {key: value for key, value in checkpoint.items() if key != "fingerprint"}
    )
    with offline_eval_module._open_db(prepost_db) as connection:
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(tampered_question, ensure_ascii=False), source_id),
        )
        connection.commit()
    with pytest.raises(AssertionError, match="option identity or lifecycle is invalid"):
        offline_eval_module._persisted_clarification_choice_for_replay(
            prepost_db,
            session_id=session_id,
            source_assistant_turn_id=source_id,
            dimension_id="route_decision.detour_tolerance",
            semantic_value_fingerprint=selector,
        )

    with offline_eval_module._open_db(prepost_db) as connection:
        connection.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (json.dumps(original_payload, ensure_ascii=False), source_id),
        )
        connection.execute(
            """INSERT INTO conversation_turns (
                   id, session_id, role, content, turn_index, status, created_at, updated_at
               ) VALUES (?, ?, 'assistant', 'newer offline turn', ?, 'active', ?, ?)""",
            (
                "turn_newer_offline_choice",
                session_id,
                int(source_row["turn_index"]) + 1,
                source_row["created_at"],
                source_row["updated_at"],
            ),
        )
        connection.commit()
    with pytest.raises(AssertionError, match="not the latest active assistant turn"):
        offline_eval_module._persisted_clarification_choice_for_replay(
            prepost_db,
            session_id=session_id,
            source_assistant_turn_id=source_id,
            dimension_id="route_decision.detour_tolerance",
            semantic_value_fingerprint=selector,
        )


def test_capture_trace_malformed_or_cross_scope_reused_profile_fails_closed_without_throwing():
    profile = {
        "profileId": "profile-shared",
        "profileFingerprint": "a" * 64,
        "executionFingerprint": "b" * 64,
        "sourceEvidence": {},
        "budgetPolicy": {},
    }
    trace = offline_eval_module._capture_semantic_trace(
        case={"id": "creative_portfolio_search_profile_recorded"},
        runtime_trace={
            "adapterCalls": [
                {
                    "profile": {
                        **profile,
                        "briefId": "brief-a",
                        "poolId": "pool-a",
                        "planningSlotId": "slot-a",
                        "dayNumber": 1,
                    },
                    "adaptedPlans": [],
                },
                {
                    "profile": {
                        **profile,
                        "briefId": "brief-b",
                        "poolId": "pool-b",
                        "planningSlotId": "slot-b",
                        "dayNumber": 2,
                    },
                    "adaptedPlans": [],
                },
                {
                    "profile": {
                        **profile,
                        "profileId": "profile-malformed",
                        "briefId": "brief-c",
                        "poolId": "pool-c",
                        "planningSlotId": "slot-c",
                        "dayNumber": "not-an-integer",
                    },
                    "adaptedPlans": [],
                },
            ]
        },
        route_discovery=None,
    )

    assert "capture_metadata_invalid:profile.dayNumber" in trace["semanticTraceErrors"]
    assert "profile_identity_reused_across_scopes" in trace["profileScopeIntegrityErrors"]
    assert trace["logicalPlanSource"] == "not_observed"
    manifest = build_capture_preflight_manifest(
        runtime_evidence={
            "cases": [
                {
                    "id": "creative_portfolio_search_profile_recorded",
                    "recordedCaptureSemanticTrace": trace,
                }
            ]
        }
    )
    manifest_case = manifest["cases"][0]
    assert manifest["captureState"] == "preflight_blocked"
    assert manifest_case["placeIdentityCaptureEligible"] is False
    assert "semantic_trace_integrity_invalid" in manifest_case["captureBlockedReasons"]


def test_capture_trace_rejects_ambiguous_or_invalid_observed_amap_adcode():
    ambiguous = offline_eval_module._capture_semantic_trace(
        case={"id": "creative_portfolio_search_profile_recorded"},
        runtime_trace={
            "amapCalls": [
                {"endpoint": "place/text", "params": {"city": "110000"}},
                {"endpoint": "place/around", "params": {"city": "310000"}},
            ]
        },
        route_discovery=None,
    )
    assert ambiguous["observedAmapAdcode"] == {
        "adcode": "",
        "source": "not_observed",
        "observedRequestCount": 2,
        "status": "ambiguous",
    }

    invalid = offline_eval_module._capture_semantic_trace(
        case={"id": "creative_portfolio_search_profile_recorded"},
        runtime_trace={"amapCalls": [{"endpoint": "place/text", "params": {"city": "北京"}}]},
        route_discovery=None,
    )
    assert invalid["observedAmapAdcode"] == {
        "adcode": "",
        "source": "not_observed",
        "observedRequestCount": 1,
        "status": "invalid",
    }


def test_database_url_capture_semantic_manifest_keeps_dedicated_trace(tmp_path):
    cases_dir = tmp_path / "fixed-goal-case"
    cases_dir.mkdir()
    dedicated_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / dedicated_case.name).write_text(
        dedicated_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = run_offline_eval(
        cases_dir=cases_dir,
        database_url=f"sqlite:///{tmp_path / 'dedicated-capture.db'}",
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )

    trace = result["cases"][0]["recordedCaptureSemanticTrace"]
    assert trace["logicalPlanSource"] == "persisted_server_initial_plan"
    assert trace["profileExecutionStatus"] == "executed"
    assert trace["logicalRouteAuthorization"]["status"] == "ready"
    assert trace["logicalRouteAuthorization"]["preferredMode"] == "transit"
    assert len(trace["logicalRouteAuthorization"]["contractFingerprint"]) == 64


def test_single_profile_case_restores_case_environment(monkeypatch, tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    fixture = CASES_DIR / "creative_portfolio_search_profile_recorded.json"
    (cases_dir / fixture.name).write_text(
        fixture.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    sentinels = {
        "DATABASE_URL": "sqlite:///sentinel-before-eval.db",
        "PROVIDER_MODE": "default",
        "AGENT_CREATIVE_PORTFOLIO_ENABLED": "false",
        "AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT": "3",
        "DEFAULT_USER_ID": "sentinel-user",
        "MAP_PROVIDER_KEY": "sentinel-map-key",
        "DEEPSEEK_API_KEY": "sentinel-deepseek-key",
        "AMAP_WEB_SERVICE_KEY": "sentinel-amap-key",
        "WEB_SEARCH_API_KEY": "sentinel-web-key",
        "SEARCH_PROVIDER_KEY": "sentinel-search-key",
        "TICKET_PROVIDER_KEY": "sentinel-ticket-key",
        "WEATHER_PROVIDER_KEY": "sentinel-weather-key",
    }
    for key, value in sentinels.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    result = run_offline_eval(cases_dir=cases_dir)

    assert result["summary"]["total"] == 1
    assert result["summary"]["passed"] == 1
    assert result["cases"][0]["id"] == "creative_portfolio_search_profile_recorded"
    assert {key: os.environ.get(key) for key in sentinels} == sentinels
    get_settings.cache_clear()
    assert get_settings().agent_creative_portfolio_enabled is False
    assert get_settings().agent_creative_portfolio_target_count == 3


def test_offline_environment_restores_when_configured_runner_raises(
    monkeypatch,
):
    sentinels = {
        key: ("default" if key == "PROVIDER_MODE" else f"sentinel-{index}")
        for index, key in enumerate(offline_eval_module._OFFLINE_ENV_KEYS)
    }
    sentinels["AGENT_CREATIVE_PORTFOLIO_ENABLED"] = "false"
    sentinels["AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT"] = "3"
    for key, value in sentinels.items():
        monkeypatch.setenv(key, value)

    def fail_before_harness(*_args, **_kwargs):
        raise RuntimeError("configured runner failed")

    monkeypatch.setattr(
        offline_eval_module,
        "_run_with_database_configured",
        fail_before_harness,
    )

    with pytest.raises(RuntimeError, match="configured runner failed"):
        offline_eval_module._run_with_database(
            [],
            "sqlite:///must-not-leak.db",
            1,
        )

    assert {key: os.environ.get(key) for key in sentinels} == sentinels


def test_required_profile_case_missing_fails_the_runner(tmp_path):
    empty_cases_dir = tmp_path / "empty-cases"
    empty_cases_dir.mkdir()

    result = run_offline_eval(cases_dir=empty_cases_dir)

    assert result["summary"]["total"] == 1
    assert result["summary"]["failed"] == 1
    assert result["cases"][0]["id"] == "creative_portfolio_search_profile_recorded"
    assert "required non-scenarios Creative Portfolio Search Profile" in result["cases"][0]["failureReason"]


def test_required_profile_case_with_zero_metrics_fails_the_runner(tmp_path):
    cases_dir = tmp_path / "zero-profile-metrics"
    cases_dir.mkdir()
    generic_case = json.loads((CASES_DIR / "react_controller_offline_smoke.json").read_text(encoding="utf-8"))
    generic_case.update(
        {
            "id": "creative_portfolio_search_profile_recorded",
            "scenario": "required_profile_metrics_missing",
            "creativePortfolioEnabled": False,
            "searchProfileEval": {
                "required": True,
                "minimumCompiledProfileCount": 2,
                "expectedPreAdoptionVersionWriteCount": 0,
                "expectedPreAdoptionPatchWriteCount": 0,
                "expectedPreAdoptionRouteWriteCount": 0,
            },
        }
    )
    (cases_dir / "required_profile_metrics_missing.json").write_text(
        json.dumps(generic_case, ensure_ascii=False),
        encoding="utf-8",
    )

    result = run_offline_eval(cases_dir=cases_dir)

    assert result["summary"]["failed"] == 1
    assert "required Search Profile compile metrics are all zero" in result["cases"][0]["failureReason"]


def test_required_profile_case_rejects_incomplete_verifier_evidence(
    monkeypatch,
    tmp_path,
):
    cases_dir = tmp_path / "incomplete-verifier-evidence"
    cases_dir.mkdir()
    fixture = CASES_DIR / "creative_portfolio_search_profile_recorded.json"
    profile_case = json.loads(fixture.read_text(encoding="utf-8"))
    (cases_dir / fixture.name).write_text(json.dumps(profile_case, ensure_ascii=False), encoding="utf-8")
    original = offline_eval_module._persisted_planning_run_event_batches
    tamper_state = {"maxEventCount": 0, "callCount": 0}

    def incomplete_verifier_evidence(db_path, session_id):
        run_ids, event_batches = original(db_path, session_id)
        tampered_batches = copy.deepcopy(event_batches)
        event_count = 0
        for batch in tampered_batches:
            for event in batch:
                stage = str(event.get("type") or event.get("toolName") or event.get("id") or "")
                if stage != "portfolio_staging_performance":
                    continue
                trace_summary = event.get("traceSummary")
                if not isinstance(trace_summary, dict):
                    continue
                trace_summary["briefMetrics"] = [{"verifierPassed": True}, {}]
                event_count += 1
        tamper_state["maxEventCount"] = max(tamper_state["maxEventCount"], event_count)
        tamper_state["callCount"] += 1
        return run_ids, tampered_batches

    monkeypatch.setattr(
        offline_eval_module,
        "_persisted_planning_run_event_batches",
        incomplete_verifier_evidence,
    )
    result = run_offline_eval(cases_dir=cases_dir)

    assert tamper_state["callCount"] >= 1
    assert tamper_state["maxEventCount"] >= 2
    assert result["summary"]["failed"] == 1
    profile_case = result["cases"][0]
    assert profile_case["profilePortfolioVerifierFailureCount"] == tamper_state["maxEventCount"]
    assert "Search Profile per-run staging/verifier cycle coverage is incomplete" in profile_case["failureReason"]
    assert "production Creative Portfolio verifier evidence is incomplete" in profile_case["failureReason"]


def test_tampered_profile_checkpoint_lineage_fails_the_runner(
    monkeypatch,
    tmp_path,
):
    cases_dir = tmp_path / "tampered-profile-lineage"
    cases_dir.mkdir()
    fixture = CASES_DIR / "creative_portfolio_search_profile_recorded.json"
    profile_case = json.loads(fixture.read_text(encoding="utf-8"))
    (cases_dir / fixture.name).write_text(json.dumps(profile_case, ensure_ascii=False), encoding="utf-8")
    original = offline_eval_module._persisted_profile_checkpoints
    tamper_state = {"applied": False}

    def tampered_checkpoints(db_path, session_id):
        checkpoints = copy.deepcopy(original(db_path, session_id))
        for checkpoint in checkpoints:
            for report in checkpoint.get("poolReports") or []:
                trace = report.get("searchProfileTrace")
                if not isinstance(trace, dict) or not trace.get("semanticContractFingerprint"):
                    continue
                trace["semanticContractFingerprint"] = "0" * 64
                tamper_state["applied"] = True
                return checkpoints
        return checkpoints

    monkeypatch.setattr(
        offline_eval_module,
        "_persisted_profile_checkpoints",
        tampered_checkpoints,
    )

    result = run_offline_eval(cases_dir=cases_dir)

    assert tamper_state["applied"] is True
    assert result["summary"]["failed"] == 1
    assert "Search Profile event/checkpoint/provider lineage mismatch" in result["cases"][0]["failureReason"]


def test_real_place_capture_sanitizes_realistic_amap_photo_urls_and_claims_envelope_once_without_network(
    monkeypatch,
    tmp_path,
):
    import hashlib
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from urllib.parse import urlencode
    from urllib.request import Request

    cases_dir = tmp_path / "real-place-capture-sanitization-red"
    cases_dir.mkdir()
    fixture = CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    (cases_dir / fixture.name).write_text(
        fixture.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source_fixture = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixture_sha256_before = hashlib.sha256(source_fixture.read_bytes()).hexdigest()
    case_sha256_before = hashlib.sha256(fixture.read_bytes()).hexdigest()
    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    runtime_evidence_before = copy.deepcopy(runtime_evidence)
    envelope = recorded_fixture_capture_module.build_zero_network_capture_session_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )

    transport_module = import_module("backend.evals.recorded_fixture_place_capture_transport")
    executor_module = import_module("backend.evals.recorded_fixture_place_capture_executor")
    transport_type = transport_module.AmapWebServicePlaceCaptureTransport
    execute = executor_module.execute_zero_network_place_capture
    execution_error = executor_module.PlaceCaptureExecutionError
    fixed_now = datetime.now(timezone.utc)
    allowlist = envelope["exactPlaceRequestAllowlist"]
    endpoint_paths = {
        "place/text": "/v3/place/text",
        "place/around": "/v3/place/around",
    }
    outbound_requests = [
        {
            "method": "GET",
            "scheme": "https",
            "host": "restapi.amap.com",
            "path": endpoint_paths[request["endpoint"]],
            "params": copy.deepcopy(request["sanitizedParams"]),
            "allowRedirects": False,
            "ordinal": ordinal,
            "requestFingerprint": request["requestFingerprint"],
            "auditFingerprint": request["auditFingerprint"],
        }
        for ordinal, request in enumerate(allowlist["requests"], start=1)
    ]
    staging_root = tmp_path / "shared-real-place-capture-root"
    staging_root.mkdir()

    def root_binding(root):
        details = os.stat(root, follow_symlinks=False)
        return recorded_fixture_capture_module.canonical_sha256(
            {
                "stagingRootIdentity": {
                    "device": details.st_dev,
                    "file": details.st_ino,
                    "createdAtNs": details.st_ctime_ns,
                }
            }
        )

    def authorization(authorization_id):
        return {
            "authorizationId": authorization_id,
            "scope": "place_only",
            "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
            "envelopeFingerprint": envelope["envelopeFingerprint"],
            "contentFingerprint": envelope["contentFingerprint"],
            "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
            "maxCalls": allowlist["requestCount"],
            "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
            "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
        }

    def network_authorization(
        place_authorization,
        network_authorization_id,
        root,
    ):
        session_directory_name = (
            "place-session-"
            + recorded_fixture_capture_module.canonical_sha256(
                {"authorizationId": place_authorization["authorizationId"]}
            )[:32].lower()
        )
        return {
            "schemaVersion": "trip-amap-place-network-authorization-v1",
            "networkAuthorizationId": network_authorization_id,
            "scope": "place_only",
            "transportKind": "amap_place_https",
            "placeAuthorizationId": place_authorization["authorizationId"],
            "placeAuthorizationFingerprint": recorded_fixture_capture_module.canonical_sha256(place_authorization),
            "sourceFingerprint": place_authorization["sourceFingerprint"],
            "envelopeFingerprint": place_authorization["envelopeFingerprint"],
            "contentFingerprint": place_authorization["contentFingerprint"],
            "exactPlaceRequestAllowlistFingerprint": place_authorization["exactPlaceRequestAllowlistFingerprint"],
            "outboundRequestSequenceFingerprint": transport_module.exact_outbound_request_sequence_fingerprint(
                outbound_requests
            ),
            "maxCalls": place_authorization["maxCalls"],
            "sessionDirectoryName": session_directory_name,
            "stagingRootBindingFingerprint": root_binding(root),
            "transportProfile": {
                "kind": "amap_place_https_v1",
                "host": "restapi.amap.com",
                "paths": sorted({request["path"] for request in outbound_requests}),
                "proxyMode": "system",
                "timeoutSeconds": 5.0,
                "userAgentProfile": "trip-place-capture-v1",
                "maxResponseBytes": 1_000_000,
            },
            "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
            "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
        }

    class StubResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self._body = body
            self._url = ""

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def getcode(self):
            return self.status

        def geturl(self):
            return self._url

        def read(self, _limit):
            return self._body

    def response_body(*, poi_fields=None, status="1", infocode="10000"):
        poi = {
            "id": "B000REALPHOTO",
            "name": "生产形状地点",
            "location": "116.397428,39.90923",
            "photos": [{"url": "https://store.is.autonavi.com/realistic-photo.jpg"}],
        }
        poi.update(poi_fields or {})
        return json.dumps(
            {
                "status": status,
                "info": "OK",
                "infocode": infocode,
                "count": "1",
                "pois": [poi],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    class StubOpener:
        def __init__(self, bodies):
            self.calls = []
            self._bodies = list(bodies)

        def open(self, request, *, timeout):
            self.calls.append({"request": request, "timeout": timeout})
            if not self._bodies:
                raise AssertionError("unexpected extra opener call")
            response = StubResponse(self._bodies.pop(0))
            response._url = request.full_url
            return response

    opener = StubOpener([response_body()] * allowlist["requestCount"])
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda _proxy_mode: opener,
    )
    credential = "test-only-real-place-capture-credential"
    first_authorization = authorization("place-photo-url-first")
    second_authorization = authorization("place-photo-url-second")
    first_transport = transport_type(
        network_authorization=network_authorization(
            first_authorization,
            "network-photo-url-first",
            staging_root,
        )
    )
    second_transport = transport_type(
        network_authorization=network_authorization(
            second_authorization,
            "network-photo-url-second",
            staging_root,
        )
    )

    def capture_outcome(**kwargs):
        try:
            return {"result": execute(**kwargs)}
        except execution_error as error:
            return {"exception": error.reason_code}

    socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(socket_trace):
        first_outcome = capture_outcome(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=first_authorization,
            staging_root=staging_root,
            credential=credential,
            transport=first_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
        second_outcome = capture_outcome(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=second_authorization,
            staging_root=staging_root,
            credential=credential,
            transport=second_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )

    assert (
        first_outcome.get("result", {}).get("status"),
        first_outcome.get("result", {}).get("reasonCode"),
        first_transport.stub_place_calls,
        second_outcome.get("exception"),
        second_transport.stub_place_calls,
        len(opener.calls),
    ) == (
        "completed",
        None,
        allowlist["requestCount"],
        "capture_envelope_already_consumed",
        0,
        allowlist["requestCount"],
    )
    assert socket_trace["realExternalCalls"] == []

    zero_effect_ledger = {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    first_result = first_outcome["result"]
    assert first_result["promotable"] is False
    assert first_result["zeroEffectLedger"] == zero_effect_ledger
    success_session = staging_root / first_result["sessionDirectoryName"]
    success_bundle = json.loads((success_session / first_result["quarantineBundleFile"]).read_text(encoding="utf-8"))
    assert success_bundle["promotable"] is False
    for record in success_bundle["responses"]:
        assert all(poi["photos"] == [] for poi in record["response"]["pois"])
        canonical_response = json.dumps(
            record["response"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        assert record["responseSha256"] == hashlib.sha256(canonical_response).hexdigest().upper()
    success_rendered = json.dumps(
        {"result": first_result, "bundle": success_bundle},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "https://store.is.autonavi.com/realistic-photo.jpg" not in success_rendered
    assert credential not in success_rendered
    assert "https://" not in success_rendered

    monkeypatch.setattr(offline_eval_module, "PROJECT_ROOT", staging_root)
    strict_loader = offline_eval_module._OfflineStrictRecordedAmapTransport(
        f"{first_result['sessionDirectoryName']}/{first_result['quarantineBundleFile']}"
    )
    for record in success_bundle["responses"]:
        request = record["request"]
        strict_loader.urlopen(Request(f"https://restapi.amap.com{request['endpoint']}?{urlencode(request['params'])}"))
    strict_trace = strict_loader.trace()
    assert strict_trace["failureCount"] == 0
    assert len(strict_trace["matched"]) == allowlist["requestCount"]

    unexpected_url_root = tmp_path / "unexpected-full-url-root"
    unexpected_url_root.mkdir()
    unexpected_url_auth = authorization("place-unexpected-full-url")
    unexpected_url_transport = transport_type(
        network_authorization=network_authorization(
            unexpected_url_auth,
            "network-unexpected-full-url",
            unexpected_url_root,
        )
    )
    unexpected_url_opener = StubOpener(
        [response_body(poi_fields={"website": "https://unexpected.example.invalid/place"})]
    )
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda _proxy_mode: unexpected_url_opener,
    )
    unexpected_url_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(unexpected_url_socket_trace):
        unexpected_url_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=unexpected_url_auth,
            staging_root=unexpected_url_root,
            credential=credential,
            transport=unexpected_url_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert unexpected_url_socket_trace["realExternalCalls"] == []
    assert unexpected_url_result["status"] == "failed"
    assert unexpected_url_result["reasonCode"] == "response_full_url_forbidden"
    assert unexpected_url_result["stubTransportCalls"] == 1
    assert unexpected_url_result["zeroEffectLedger"] == zero_effect_ledger

    secret_field_root = tmp_path / "secret-field-root"
    secret_field_root.mkdir()
    secret_field_auth = authorization("place-secret-field")
    secret_field_transport = transport_type(
        network_authorization=network_authorization(
            secret_field_auth,
            "network-secret-field",
            secret_field_root,
        )
    )
    secret_field_opener = StubOpener([response_body(poi_fields={"api_key": "not-a-credential-to-persist"})])
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda _proxy_mode: secret_field_opener,
    )
    secret_field_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(secret_field_socket_trace):
        secret_field_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=secret_field_auth,
            staging_root=secret_field_root,
            credential=credential,
            transport=secret_field_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert secret_field_socket_trace["realExternalCalls"] == []
    assert secret_field_result["status"] == "failed"
    assert secret_field_result["reasonCode"] == "response_secret_field_forbidden"
    assert secret_field_result["stubTransportCalls"] == 1
    assert secret_field_result["zeroEffectLedger"] == zero_effect_ledger

    provider_failure_root = tmp_path / "provider-failure-envelope-root"
    provider_failure_root.mkdir()
    provider_failure_auth = authorization("place-provider-failure-first")
    provider_failure_transport = transport_type(
        network_authorization=network_authorization(
            provider_failure_auth,
            "network-provider-failure-first",
            provider_failure_root,
        )
    )
    provider_failure_ordinal = min(2, allowlist["requestCount"])
    provider_failure_opener = StubOpener(
        [response_body()] * (provider_failure_ordinal - 1) + [response_body(status="0", infocode="10001")]
    )
    monkeypatch.setattr(
        transport_module,
        "_build_hardened_opener",
        lambda _proxy_mode: provider_failure_opener,
    )
    provider_failure_socket_trace = {"realExternalCalls": []}
    retry_provider_auth = authorization("place-provider-failure-retry")
    retry_provider_transport = transport_type(
        network_authorization=network_authorization(
            retry_provider_auth,
            "network-provider-failure-retry",
            provider_failure_root,
        )
    )
    with offline_eval_module._offline_external_network_sentinel(provider_failure_socket_trace):
        provider_failure_result = execute(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=provider_failure_auth,
            staging_root=provider_failure_root,
            credential=credential,
            transport=provider_failure_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
        retry_provider_outcome = capture_outcome(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime_evidence,
            authorization=retry_provider_auth,
            staging_root=provider_failure_root,
            credential=credential,
            transport=retry_provider_transport,
            cases_dir=cases_dir,
            now=fixed_now,
        )
    assert provider_failure_socket_trace["realExternalCalls"] == []
    assert provider_failure_result["status"] == "failed"
    assert provider_failure_result["reasonCode"] == "amap_response_unsuccessful"
    assert provider_failure_result["partialQuarantineFile"] is not None
    assert provider_failure_result["completedResponseCount"] == provider_failure_ordinal - 1
    assert provider_failure_result["stubTransportCalls"] == provider_failure_ordinal
    assert provider_failure_result["zeroEffectLedger"] == zero_effect_ledger
    assert retry_provider_outcome == {"exception": "capture_envelope_already_consumed"}
    assert retry_provider_transport.stub_place_calls == 0
    assert len(provider_failure_opener.calls) == provider_failure_ordinal
    assert runtime_evidence == runtime_evidence_before
    assert hashlib.sha256(source_fixture.read_bytes()).hexdigest() == fixture_sha256_before
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == case_sha256_before


def test_real_place_capture_cli_requires_explicit_execute_and_delegates_exactly_once_without_network(
    monkeypatch,
    tmp_path,
    capsys,
):
    from pathlib import Path

    """The CLI validates a frozen request before one explicitly authorized stub delegation."""

    import hashlib
    import stat
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from importlib.util import find_spec

    from src.core.database import sqlite_path_from_url

    module_name = "backend.evals.recorded_fixture_place_capture_cli"
    module_spec = find_spec(module_name)
    cli = import_module(module_name) if module_spec is not None else None
    main = getattr(cli, "main", None)

    assert callable(main), f"missing callable main in {module_name}"

    transport_module = import_module("backend.evals.recorded_fixture_place_capture_transport")
    from backend.evals import recorded_fixture_place_capture_executor as executor_module

    cases_dir = tmp_path / "fixed-real-place-capture-cli-case"
    cases_dir.mkdir()
    fixed_case = (
        CASES_DIR / "fixed_creative_portfolio_goal_recorded" / "creative_portfolio_search_profile_recorded.json"
    )
    (cases_dir / fixed_case.name).write_text(
        fixed_case.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source_fixture = CASES_DIR.parent / "fixtures" / "beijing_amap_sanitized_recording.json"
    fixed_case_sha256_before = hashlib.sha256(fixed_case.read_bytes()).hexdigest()
    source_fixture_sha256_before = hashlib.sha256(source_fixture.read_bytes()).hexdigest()
    production_db = sqlite_path_from_url(get_settings().database_url)
    if not production_db.is_absolute():
        production_db = (Path.cwd() / production_db).resolve()
    production_db_before = (
        (production_db.stat().st_mtime_ns, hashlib.sha256(production_db.read_bytes()).hexdigest())
        if production_db.is_file()
        else None
    )
    runtime_evidence = run_offline_eval(
        cases_dir=cases_dir,
        dry_route_discovery=True,
        capture_semantic_manifest=True,
    )
    manifest = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    envelope = recorded_fixture_capture_module.build_zero_network_capture_session_envelope(
        manifest=manifest,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    runtime_evidence_before = copy.deepcopy(runtime_evidence)

    fixed_now = datetime.now(timezone.utc)
    allowlist = envelope["exactPlaceRequestAllowlist"]
    outbound_requests = [
        {
            "method": "GET",
            "scheme": "https",
            "host": "restapi.amap.com",
            "path": "/v3/" + str(request["endpoint"]),
            "params": copy.deepcopy(request["sanitizedParams"]),
            "allowRedirects": False,
            "ordinal": ordinal,
            "requestFingerprint": request["requestFingerprint"],
            "auditFingerprint": request["auditFingerprint"],
        }
        for ordinal, request in enumerate(allowlist["requests"], start=1)
    ]
    preparation_root = tmp_path / "acquisition-preparation"
    preparation_root.mkdir()
    staging_root = preparation_root / "immediate-empty-child"
    staging_root.mkdir()

    def root_binding(root):
        details = os.stat(root, follow_symlinks=False)
        return recorded_fixture_capture_module.canonical_sha256(
            {
                "stagingRootIdentity": {
                    "device": details.st_dev,
                    "file": details.st_ino,
                    "createdAtNs": details.st_ctime_ns,
                }
            }
        )

    place_authorization = {
        "authorizationId": "cli-place-authorization",
        "scope": "place_only",
        "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
        "envelopeFingerprint": envelope["envelopeFingerprint"],
        "contentFingerprint": envelope["contentFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
        "maxCalls": allowlist["requestCount"],
        "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
    }
    session_directory_name = (
        "place-session-"
        + recorded_fixture_capture_module.canonical_sha256({"authorizationId": place_authorization["authorizationId"]})[
            :32
        ].lower()
    )
    network_authorization = {
        "schemaVersion": "trip-amap-place-network-authorization-v1",
        "networkAuthorizationId": "cli-network-authorization",
        "scope": "place_only",
        "transportKind": "amap_place_https",
        "placeAuthorizationId": place_authorization["authorizationId"],
        "placeAuthorizationFingerprint": recorded_fixture_capture_module.canonical_sha256(place_authorization),
        "sourceFingerprint": place_authorization["sourceFingerprint"],
        "envelopeFingerprint": place_authorization["envelopeFingerprint"],
        "contentFingerprint": place_authorization["contentFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": place_authorization["exactPlaceRequestAllowlistFingerprint"],
        "outboundRequestSequenceFingerprint": transport_module.exact_outbound_request_sequence_fingerprint(
            outbound_requests
        ),
        "maxCalls": place_authorization["maxCalls"],
        "sessionDirectoryName": session_directory_name,
        "stagingRootBindingFingerprint": root_binding(staging_root),
        "transportProfile": {
            "kind": "amap_place_https_v1",
            "host": "restapi.amap.com",
            "paths": sorted({request["path"] for request in outbound_requests}),
            "proxyMode": "system",
            "timeoutSeconds": 5.0,
            "userAgentProfile": "trip-place-capture-v1",
            "maxResponseBytes": 1_000_000,
        },
        "issuedAt": (fixed_now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (fixed_now + timedelta(hours=1)).isoformat(),
    }

    def write_json_input(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        details = path.lstat()
        assert path.is_absolute()
        assert stat.S_ISREG(details.st_mode)
        assert not path.is_symlink()
        return path

    input_paths = {
        "--runtime-evidence": write_json_input("runtime-evidence.json", runtime_evidence),
        "--manifest": write_json_input("manifest.json", manifest),
        "--envelope": write_json_input("envelope.json", envelope),
        "--place-authorization": write_json_input("place-authorization.json", place_authorization),
        "--network-authorization": write_json_input("network-authorization.json", network_authorization),
    }
    base_argv = [item for flag in input_paths for item in (flag, str(input_paths[flag]))] + [
        "--staging-root",
        str(staging_root),
    ]

    def argv_with(flag, value):
        argv = list(base_argv)
        index = argv.index(flag)
        argv[index + 1] = str(value)
        return argv

    def read_single_json_stdout():
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert len(lines) == 1
        return json.loads(lines[0]), lines[0]

    monkeypatch.setattr(cli, "CASES_DIR", cases_dir)
    monkeypatch.setattr(cli, "ACQUISITION_PREPARATION_ROOT", preparation_root)
    original_key_reader = cli._read_map_provider_key
    readiness_calls = {"key": 0, "transport": 0, "executor": 0}

    def unexpected_key_read():
        readiness_calls["key"] += 1
        raise AssertionError("readiness must not read MAP_PROVIDER_KEY")

    class UnexpectedTransport:
        def __init__(self, **_kwargs):
            readiness_calls["transport"] += 1
            raise AssertionError("readiness must not construct a transport")

    def unexpected_executor(**_kwargs):
        readiness_calls["executor"] += 1
        raise AssertionError("readiness must not delegate execution")

    monkeypatch.setattr(cli, "_read_map_provider_key", unexpected_key_read)
    monkeypatch.setattr(cli, "AmapWebServicePlaceCaptureTransport", UnexpectedTransport)
    monkeypatch.setattr(cli, "execute_zero_network_place_capture", unexpected_executor)
    readiness_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(readiness_socket_trace):
        assert main(base_argv) == 0
        readiness_first, readiness_first_text = read_single_json_stdout()
        assert main(base_argv) == 0
        readiness_second, readiness_second_text = read_single_json_stdout()

    allowed_readiness_fields = {
        "status",
        "sourceFingerprint",
        "envelopeFingerprint",
        "contentFingerprint",
        "exactPlaceRequestAllowlistFingerprint",
        "outboundRequestSequenceFingerprint",
        "requestCount",
        "allowedPaths",
        "captureScope",
        "consumed",
        "promotable",
        "zeroEffectLedger",
        "challengeFingerprint",
    }
    assert readiness_first == readiness_second
    assert readiness_first_text == readiness_second_text
    assert set(readiness_first) == allowed_readiness_fields
    assert readiness_first["status"] == "READY_FOR_EXPLICIT_PLACE_CAPTURE_EXECUTION"
    assert readiness_first["challengeFingerprint"] == readiness_second["challengeFingerprint"]
    assert readiness_first["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    assert readiness_calls == {"key": 0, "transport": 0, "executor": 0}
    assert readiness_socket_trace["realExternalCalls"] == []
    assert list(staging_root.iterdir()) == []
    for forbidden in (
        str(tmp_path),
        str(staging_root),
        "authorizationId",
        "issuedAt",
        "expiresAt",
        "credential",
        "https://",
        "http://",
        "header",
        "proxy",
    ):
        assert forbidden not in readiness_first_text

    credential_sentinel = "MAP_PROVIDER_KEY_CLI_SENTINEL_NEVER_RENDER"
    credential_reads = []

    class TrackingEnvironment(dict):
        def __getitem__(self, key):
            if key == "MAP_PROVIDER_KEY":
                credential_reads.append(key)
            return super().__getitem__(key)

    transport_instances = []
    delegated = {}

    class StubTransport:
        def __init__(self, *, network_authorization):
            self.network_authorization = network_authorization
            transport_instances.append(self)

    def stub_executor(**kwargs):
        delegated.update(kwargs)
        return {
            "status": "completed",
            "reasonCode": None,
            "consumed": True,
            "promotable": False,
            "zeroEffectLedger": copy.deepcopy(readiness_first["zeroEffectLedger"]),
        }

    place_authorization_before = copy.deepcopy(place_authorization)
    network_authorization_before = copy.deepcopy(network_authorization)
    monkeypatch.setattr(cli, "_read_map_provider_key", original_key_reader)
    monkeypatch.setattr(cli.os, "environ", TrackingEnvironment({"MAP_PROVIDER_KEY": credential_sentinel}))
    monkeypatch.setattr(cli, "AmapWebServicePlaceCaptureTransport", StubTransport)
    monkeypatch.setattr(cli, "execute_zero_network_place_capture", stub_executor)
    explicit_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(explicit_socket_trace):
        assert main([*base_argv, "--execute-place-capture"]) == 0
        explicit_summary, explicit_text = read_single_json_stdout()

    assert credential_reads == ["MAP_PROVIDER_KEY"]
    assert len(transport_instances) == 1
    assert len(delegated) == 9
    assert transport_instances[0].network_authorization == network_authorization_before
    assert delegated == {
        "envelope": envelope,
        "manifest": manifest,
        "runtime_evidence": runtime_evidence,
        "authorization": place_authorization,
        "staging_root": staging_root,
        "credential": credential_sentinel,
        "transport": transport_instances[0],
        "cases_dir": cases_dir,
        "max_response_bytes": network_authorization["transportProfile"]["maxResponseBytes"],
    }
    assert place_authorization == place_authorization_before
    assert network_authorization == network_authorization_before
    assert explicit_socket_trace["realExternalCalls"] == []
    assert explicit_summary["status"] == "PLACE_CAPTURE_EXECUTION_COMPLETED"
    assert explicit_summary["consumed"] is True
    assert explicit_summary["promotable"] is False
    assert explicit_summary["zeroEffectLedger"] == readiness_first["zeroEffectLedger"]
    for forbidden in (
        credential_sentinel,
        str(tmp_path),
        str(staging_root),
        "https://",
        "http://",
        "query",
        "header",
        "proxy",
    ):
        assert forbidden not in explicit_text

    rejected_calls = {"key": 0, "transport": 0, "executor": 0}

    def forbidden_key_read():
        rejected_calls["key"] += 1
        raise AssertionError("invalid inputs must not read MAP_PROVIDER_KEY")

    class ForbiddenTransport:
        def __init__(self, **_kwargs):
            rejected_calls["transport"] += 1
            raise AssertionError("invalid inputs must not construct a transport")

    def forbidden_executor(**_kwargs):
        rejected_calls["executor"] += 1
        raise AssertionError("invalid inputs must not delegate execution")

    monkeypatch.setattr(cli, "_read_map_provider_key", forbidden_key_read)
    monkeypatch.setattr(cli, "AmapWebServicePlaceCaptureTransport", ForbiddenTransport)
    monkeypatch.setattr(cli, "execute_zero_network_place_capture", forbidden_executor)
    rejected_sentinel = "UNRECOGNIZED_ARGUMENT_VALUE_MUST_NOT_LEAK"

    def assert_rejected(argv, *, expected_reason_code=None):
        rejection_socket_trace = {"realExternalCalls": []}
        with offline_eval_module._offline_external_network_sentinel(rejection_socket_trace):
            assert main(argv) == 1
            payload, rendered = read_single_json_stdout()
        assert payload["status"] == "PLACE_CAPTURE_CLI_FAILED"
        if expected_reason_code is not None:
            assert payload["reasonCode"] == expected_reason_code
        assert payload["zeroEffectLedger"] == readiness_first["zeroEffectLedger"]
        assert rejected_sentinel not in rendered
        assert credential_sentinel not in rendered
        assert str(tmp_path) not in rendered
        assert rejection_socket_trace["realExternalCalls"] == []
        assert rejected_calls == {"key": 0, "transport": 0, "executor": 0}

    for unknown_flag in (
        "--key",
        "--credential",
        "--cases-dir",
        "--url",
        "--proxy",
        "--timeout",
        "--maxCalls",
    ):
        assert_rejected([*base_argv, unknown_flag, rejected_sentinel])

    no_execute_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(no_execute_socket_trace):
        assert main(base_argv) == 0
        no_execute_summary, _no_execute_text = read_single_json_stdout()
    assert no_execute_summary["status"] == "READY_FOR_EXPLICIT_PLACE_CAPTURE_EXECUTION"
    assert no_execute_socket_trace["realExternalCalls"] == []
    assert rejected_calls == {"key": 0, "transport": 0, "executor": 0}

    outside_root = tmp_path / "outside-staging-root"
    outside_root.mkdir()
    nonempty_staging_root = preparation_root / "nonempty-child"
    nonempty_staging_root.mkdir()
    (nonempty_staging_root / "already-present").write_text("x", encoding="utf-8")
    for invalid_staging_root in (
        staging_root.name,
        outside_root,
        preparation_root,
        nonempty_staging_root,
    ):
        assert_rejected(argv_with("--staging-root", invalid_staging_root))

    staging_symlink = preparation_root / "staging-symlink"
    try:
        staging_symlink.symlink_to(staging_root, target_is_directory=True)
    except OSError:
        pass
    else:
        assert_rejected(argv_with("--staging-root", staging_symlink))

    malformed_root = tmp_path / "malformed-cli-inputs"
    malformed_root.mkdir()
    duplicate_json = malformed_root / "duplicate.json"
    duplicate_json.write_text('{"duplicate":1,"duplicate":2}', encoding="utf-8")
    nonfinite_json = malformed_root / "nonfinite.json"
    nonfinite_json.write_text('{"value":NaN}', encoding="utf-8")
    nonobject_json = malformed_root / "nonobject.json"
    nonobject_json.write_text("[]", encoding="utf-8")
    nested_value = {}
    for _depth in range(65):
        nested_value = {"child": nested_value}
    deep_json = write_json_input("deep-invalid.json", nested_value)
    for invalid_runtime_input in (
        input_paths["--runtime-evidence"].name,
        "https://example.invalid/runtime-evidence.json",
        duplicate_json,
        nonfinite_json,
        nonobject_json,
        deep_json,
    ):
        assert_rejected(argv_with("--runtime-evidence", invalid_runtime_input))

    input_symlink = malformed_root / "runtime-symlink.json"
    try:
        input_symlink.symlink_to(input_paths["--runtime-evidence"])
    except OSError:
        pass
    else:
        assert_rejected(argv_with("--runtime-evidence", input_symlink))

    oversized_json = malformed_root / "oversized.json"
    oversized_json.write_bytes(b"x" * ((16 * 1024 * 1024) + 1))
    too_many_nodes_json = malformed_root / "too-many-nodes.json"
    too_many_nodes_json.write_text(
        json.dumps({"items": [0] * 100_001}),
        encoding="utf-8",
    )
    assert_rejected(argv_with("--runtime-evidence", oversized_json))
    assert_rejected(
        argv_with("--runtime-evidence", too_many_nodes_json),
        expected_reason_code="json_input_shape_invalid",
    )

    tampered_place_authorization = copy.deepcopy(place_authorization)
    tampered_place_authorization["maxCalls"] += 1
    tampered_network_authorization = copy.deepcopy(network_authorization)
    tampered_network_authorization["maxCalls"] += 1
    tampered_place_authorization_path = write_json_input(
        "tampered-place-authorization.json",
        tampered_place_authorization,
    )
    tampered_network_authorization_path = write_json_input(
        "tampered-network-authorization.json",
        tampered_network_authorization,
    )
    assert_rejected(argv_with("--place-authorization", tampered_place_authorization_path))
    assert_rejected(argv_with("--network-authorization", tampered_network_authorization_path))

    class TypedFailureTransport:
        def __init__(self, **_kwargs):
            self.real_external_place_calls = 0

    def pre_claim_failure_executor(**_kwargs):
        raise cli.PlaceCaptureExecutionError("typed_pre_claim_failure")

    monkeypatch.setattr(cli, "_read_map_provider_key", lambda: credential_sentinel)
    monkeypatch.setattr(cli, "AmapWebServicePlaceCaptureTransport", TypedFailureTransport)
    monkeypatch.setattr(
        cli,
        "execute_zero_network_place_capture",
        pre_claim_failure_executor,
    )
    pre_claim_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(pre_claim_socket_trace):
        assert main([*base_argv, "--execute-place-capture"]) == 1
        pre_claim_failure, pre_claim_failure_text = read_single_json_stdout()
    assert pre_claim_failure == {
        "status": "PLACE_CAPTURE_CLI_FAILED",
        "reasonCode": "typed_pre_claim_failure",
        "consumed": False,
        "promotable": False,
        "zeroEffectLedger": readiness_first["zeroEffectLedger"],
    }
    assert pre_claim_socket_trace["realExternalCalls"] == []
    assert credential_sentinel not in pre_claim_failure_text
    assert str(staging_root) not in pre_claim_failure_text

    claim_identity = {
        "envelopeFingerprint": envelope["envelopeFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
    }
    claim_key = recorded_fixture_capture_module.canonical_sha256(claim_identity)
    claim_filename = f"{executor_module._ENVELOPE_CLAIM_PREFIX}{claim_key[:32].lower()}.json"
    claim_path = staging_root / claim_filename

    def post_claim_failure_executor(**_kwargs):
        _kwargs["transport"].real_external_place_calls = 1
        temporary_claim_path = staging_root / (claim_filename + ".tmp")
        temporary_claim_path.write_text(
            json.dumps({"claimKey": claim_key, "consumed": True}),
            encoding="utf-8",
        )
        temporary_claim_path.replace(claim_path)
        raise cli.PlaceCaptureExecutionError("typed_post_claim_failure")

    monkeypatch.setattr(
        cli,
        "execute_zero_network_place_capture",
        post_claim_failure_executor,
    )
    post_claim_socket_trace = {"realExternalCalls": []}
    with offline_eval_module._offline_external_network_sentinel(post_claim_socket_trace):
        assert main([*base_argv, "--execute-place-capture"]) == 1
        post_claim_failure, post_claim_failure_text = read_single_json_stdout()
    assert post_claim_failure == {
        "status": "PLACE_CAPTURE_CLI_FAILED",
        "reasonCode": "typed_post_claim_failure",
        "consumed": True,
        "promotable": False,
        "externalEffectLedger": {
            **readiness_first["zeroEffectLedger"],
            "network": 1,
            "amap": 1,
            "capture": 1,
        },
    }
    assert claim_path.is_file()
    assert post_claim_socket_trace["realExternalCalls"] == []
    assert credential_sentinel not in post_claim_failure_text
    assert str(staging_root) not in post_claim_failure_text
    claim_path.unlink()
    assert list(staging_root.iterdir()) == []

    assert runtime_evidence == runtime_evidence_before
    assert hashlib.sha256(fixed_case.read_bytes()).hexdigest() == fixed_case_sha256_before
    assert hashlib.sha256(source_fixture.read_bytes()).hexdigest() == source_fixture_sha256_before
    if production_db_before is not None:
        assert production_db.is_file()
        assert (
            production_db.stat().st_mtime_ns,
            hashlib.sha256(production_db.read_bytes()).hexdigest(),
        ) == production_db_before


def test_place_response_hash_contract_round_trips_executor_output_into_canonical_evidence():
    from importlib import import_module

    binding = import_module("backend.evals.recorded_fixture_canonical_binding")
    combined = import_module("backend.evals.recorded_fixture_combined_evidence")
    executor = import_module("backend.evals.recorded_fixture_place_capture_executor")
    response = {
        "status": "1",
        "infocode": "10000",
        "pois": [
            {
                "id": "B000000001",
                "name": "Recorded contract candidate",
                "location": "116.300000,39.900000",
                "photos": [],
            }
        ],
    }
    response_sha256 = executor._recorded_response_sha256(response)
    assert response_sha256 == canonical_sha256(response)

    allowlist = {
        "requestCount": 1,
        "requests": [
            {
                "endpoint": "place/text",
                "sanitizedParams": {"keywords": "contract"},
                "requestFingerprint": "1" * 64,
                "auditFingerprint": "2" * 64,
            }
        ],
    }

    def bundle_with_hash(fingerprint):
        bundle = {
            "schemaVersion": "trip-recorded-amap-v1",
            "recordingType": "recorded/non-live",
            "promotable": False,
            "quarantine": {
                "kind": "place_only_capture",
                "requestCount": 1,
                "completedResponseCount": 1,
                "routeCaptureAuthorized": False,
                "routeCalls": 0,
            },
            "responses": [
                {
                    "ordinal": 1,
                    "requestFingerprint": "1" * 64,
                    "auditFingerprint": "2" * 64,
                    "request": {
                        "endpoint": "/v3/place/text",
                        "params": {"keywords": "contract"},
                    },
                    "responseSha256": fingerprint,
                    "response": copy.deepcopy(response),
                }
            ],
        }
        bundle["bundleFingerprint"] = canonical_sha256(bundle)
        return bundle

    produced = bundle_with_hash(response_sha256)
    assert binding._validated_complete_place_quarantine(produced, allowlist) == produced["responses"]
    assert (
        combined._place_record(
            produced["responses"][0],
            case_id="hash-contract",
            captured_at="2026-08-15T00:00:00Z",
            provider="AMap Web Service",
        )["responseSha256"]
        == response_sha256
    )

    legacy_lowercase = bundle_with_hash(response_sha256.lower())
    assert (
        binding._validated_complete_place_quarantine(
            legacy_lowercase,
            allowlist,
        )
        == legacy_lowercase["responses"]
    )
    assert (
        combined._place_record(
            legacy_lowercase["responses"][0],
            case_id="hash-contract",
            captured_at="2026-08-15T00:00:00Z",
            provider="AMap Web Service",
        )["responseSha256"]
        == response_sha256.lower()
    )

    for invalid_hash in (
        response_sha256[:-1],
        response_sha256[:-1] + "Z",
        ("0" if response_sha256[0] != "0" else "1") + response_sha256[1:],
    ):
        invalid = bundle_with_hash(invalid_hash)
        with pytest.raises(
            binding.CanonicalBindingError,
            match="response_(invalid|hash_mismatch)",
        ):
            binding._validated_complete_place_quarantine(invalid, allowlist)
        with pytest.raises(combined.CombinedEvidenceError, match="place_record_hash_invalid"):
            combined._place_record(
                invalid["responses"][0],
                case_id="hash-contract",
                captured_at="2026-08-15T00:00:00Z",
                provider="AMap Web Service",
            )


def test_canonical_binding_collapses_only_structurally_identical_profile_runs():
    from dataclasses import replace
    from importlib import import_module

    binding = import_module("backend.evals.recorded_fixture_canonical_binding")
    from src.services.consumer_candidate_admission_service import (
        ConsumerCandidateAdmissionService,
    )

    profile_fingerprint = "1" * 64
    occurrence_fingerprint = "2" * 64
    plan_fingerprint = "3" * 64
    source_plan_fingerprint = "4" * 64
    scope = {
        "briefId": "brief-contract",
        "poolId": "pool-contract",
        "planningSlotId": "slot-contract",
        "dayNumber": 1,
    }
    route_contract_fingerprint = "b" * 64
    experience_spec_policy = {
        "intentType": "meal",
        "allowedDayNumbers": [1],
        "experienceFamilies": ["meal"],
        "accessPolicy": "verified_amap_food_service",
        "evidenceFreshness": {"maxAgeHours": 24},
        "unresolvedDimensions": [],
        "specFingerprint": "c" * 64,
    }
    consumer_input = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id=scope["briefId"],
        pool_id=scope["poolId"],
        planning_slot_id=scope["planningSlotId"],
        day_number=scope["dayNumber"],
        city="北京",
        family="meal",
        activity_mode="meal",
        requirement_level="soft",
        experience_shape="single_poi",
        experience_goal="当地特色午餐",
        route_context={
            "routeDecisionContract": {
                "fingerprint": route_contract_fingerprint,
            }
        },
        experience_spec_policy=experience_spec_policy,
        spec_fingerprint=experience_spec_policy["specFingerprint"],
    )
    profile = {
        "profileFingerprint": profile_fingerprint,
        "executionFingerprint": "5" * 64,
        "scope": copy.deepcopy(scope),
        "semanticRole": {
            "experienceFamily": "meal",
            "intentType": "meal",
            "requirementLevel": "soft",
        },
        "city": "北京",
        "consumerAdmissionInput": consumer_input,
        "queryPlans": [
            {
                "planId": "provider-plan-contract",
                "sourcePlanId": "source-plan-contract",
                "sourcePlanFingerprint": source_plan_fingerprint,
                "providerPlanFingerprint": plan_fingerprint,
                "endpoint": "place/text",
                "city": "北京",
                "keyword": "午餐",
                "category": "中餐厅",
                "resultLimit": 12,
                "radiusMeters": 1000,
            }
        ],
    }
    scope_fingerprint = binding.canonical_sha256(
        {
            "occurrenceFingerprint": occurrence_fingerprint,
            "sourcePlanFingerprint": source_plan_fingerprint,
            "providerPlanFingerprint": plan_fingerprint,
            "requestShape": {
                "endpoint": "place/text",
                "city": "北京",
                "keyword": "午餐",
                "category": "中餐厅",
                "limit": 12,
                "radius": 0,
            },
        }
    )
    allowlist = {
        "requests": [
            {
                "profileOccurrence": {
                    **copy.deepcopy(scope),
                    "profileFingerprint": profile_fingerprint,
                    "occurrenceFingerprint": occurrence_fingerprint,
                },
                "queryPlanLineage": {
                    "sourcePlanId": "source-plan-contract",
                    "sourcePlanFingerprint": source_plan_fingerprint,
                    "providerPlanId": "provider-plan-contract",
                    "providerPlanFingerprint": plan_fingerprint,
                    "queryScopeFingerprint": scope_fingerprint,
                },
            }
        ]
    }
    logical_slot = {**copy.deepcopy(scope), "startTime": "12:00"}
    manifest = {"sourceFingerprint": "6" * 64}

    def runtime_with(profile_runs):
        return {
            "cases": [
                {
                    "recordedCaptureSemanticTrace": {
                        "sourceFingerprint": manifest["sourceFingerprint"],
                        "routeContractFingerprint": route_contract_fingerprint,
                        "profileRuns": profile_runs,
                        "logicalSlots": [copy.deepcopy(logical_slot)],
                    }
                }
            ]
        }

    single = binding._derive_occurrences(
        allowlist=allowlist,
        runtime_evidence=runtime_with([copy.deepcopy(profile)]),
        manifest=manifest,
    )
    identical_duplicate = binding._derive_occurrences(
        allowlist=allowlist,
        runtime_evidence=runtime_with([copy.deepcopy(profile), copy.deepcopy(profile)]),
        manifest=manifest,
    )
    assert identical_duplicate == single

    wrong_scope = copy.deepcopy(allowlist)
    wrong_scope["requests"][0]["queryPlanLineage"]["queryScopeFingerprint"] = "8" * 64
    with pytest.raises(
        binding.CanonicalBindingError,
        match="place_request_query_plan_lineage_mismatch",
    ):
        binding._derive_occurrences(
            allowlist=wrong_scope,
            runtime_evidence=runtime_with([copy.deepcopy(profile)]),
            manifest=manifest,
        )

    conflicting = copy.deepcopy(profile)
    conflicting["executionFingerprint"] = "7" * 64
    with pytest.raises(
        binding.CanonicalBindingError,
        match="runtime_profile_conflicting_duplicate",
    ):
        binding._derive_occurrences(
            allowlist=allowlist,
            runtime_evidence=runtime_with([copy.deepcopy(profile), conflicting]),
            manifest=manifest,
        )

    variant = replace(
        single[0],
        ordinal=2,
        query_plan_fingerprint="9" * 64,
        query_scope_fingerprint="a" * 64,
    )
    grouped = binding._group_occurrence_evidence(
        [single[0], variant],
        [{"ordinal": 1}, {"ordinal": 2}],
    )
    assert len(grouped) == 1
    assert [item[0].ordinal for item in grouped[0]] == [1, 2]

    conflicting_variant = replace(variant, city="上海")
    with pytest.raises(
        binding.CanonicalBindingError,
        match="place_request_occurrence_conflicting_duplicate",
    ):
        binding._group_occurrence_evidence(
            [single[0], conflicting_variant],
            [{"ordinal": 1}, {"ordinal": 2}],
        )

    valid_variant = {
        "ordinal": 1,
        "response": {"pois": [{"id": "B000000001"}]},
    }
    empty_variant = {"ordinal": 2, "response": {"pois": []}}
    grouped_with_empty_variant = binding._group_occurrence_evidence(
        [single[0], variant],
        [valid_variant, empty_variant],
    )
    with pytest.raises(
        binding.CanonicalBindingError,
        match="place_identity_ambiguous_or_missing",
    ):
        for _occurrence, record in grouped_with_empty_variant[0]:
            binding._validated_place_response_pois(record)


def test_canonical_binding_renumbers_folded_query_variants_before_route_validation(
    monkeypatch,
):
    from dataclasses import replace
    from importlib import import_module
    from types import SimpleNamespace

    binding = import_module("backend.evals.recorded_fixture_canonical_binding")
    occurrence_one = binding._Occurrence(
        ordinal=1,
        brief_id="brief-z0",
        pool_id="pool-z0",
        planning_slot_id="slot-z0-1",
        day_number=1,
        start_time="10:00",
        profile_fingerprint="1" * 64,
        occurrence_fingerprint="2" * 64,
        query_plan_fingerprint="3" * 64,
        query_scope_fingerprint="4" * 64,
        family="meal",
        intent_type="meal",
        requirement_level="soft",
        city="北京",
        consumer_admission_input={},
    )
    occurrence_one_variant = replace(
        occurrence_one,
        ordinal=2,
        query_plan_fingerprint="5" * 64,
        query_scope_fingerprint="6" * 64,
    )
    occurrence_two = binding._Occurrence(
        ordinal=3,
        brief_id="brief-z0",
        pool_id="pool-z0",
        planning_slot_id="slot-z0-2",
        day_number=1,
        start_time="12:00",
        profile_fingerprint="7" * 64,
        occurrence_fingerprint="8" * 64,
        query_plan_fingerprint="9" * 64,
        query_scope_fingerprint="a" * 64,
        family="meal",
        intent_type="meal",
        requirement_level="soft",
        city="北京",
        consumer_admission_input={},
    )
    occurrences = [occurrence_one, occurrence_one_variant, occurrence_two]
    records = [
        {
            "ordinal": ordinal,
            "requestFingerprint": f"{ordinal}" * 64,
            "auditFingerprint": f"{ordinal + 3}" * 64,
            "responseSha256": f"{ordinal + 6}" * 64,
            "response": {
                "pois": [
                    {
                        "id": "B000000001" if ordinal < 3 else "B000000002",
                    }
                ]
            },
        }
        for ordinal in range(1, 4)
    ]
    bindings = {
        "sourceFingerprint": "1" * 64,
        "dedicatedCaseSha256": "2" * 64,
        "runtimeEvidenceSha256": "3" * 64,
        "captureSemanticTraceFingerprint": "4" * 64,
        "capturePreflightManifestFingerprint": "5" * 64,
        "exactPlaceRequestAllowlistFingerprint": "6" * 64,
        "routeContractFingerprint": "7" * 64,
    }

    monkeypatch.setattr(binding, "_validate_preflight_envelope_bindings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(binding, "_validated_allowlist", lambda _envelope: {})
    monkeypatch.setattr(
        binding,
        "_validated_complete_place_quarantine",
        lambda _quarantine, _allowlist: copy.deepcopy(records),
    )
    monkeypatch.setattr(
        binding,
        "_derive_occurrences",
        lambda **_kwargs: copy.deepcopy(occurrences),
    )
    monkeypatch.setattr(binding, "_source_bindings", lambda *_args, **_kwargs: bindings)
    monkeypatch.setattr(
        binding.MapPoiService,
        "_parse_poi",
        lambda _self, poi, _category: SimpleNamespace(
            id=poi["id"],
            name=f"candidate-{poi['id']}",
            type="餐饮服务",
            category="餐饮服务",
            provider_type_code="050000",
            tags=[],
            business_area="",
            city="北京",
            address="",
            longitude=116.3 if poi["id"] == "B000000001" else 116.4,
            latitude=39.9,
        ),
    )
    monkeypatch.setattr(
        binding.ConsumerCandidateAdmissionService,
        "build_consumer_context",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        binding.ConsumerCandidateAdmissionService,
        "evaluate",
        lambda _self, candidate, _consumer: {
            "schemaVersion": "consumer-candidate-admission-v2",
            "consumerFingerprint": binding.canonical_sha256({"slot": candidate["planningSlotId"]}),
            "candidateEvidenceFingerprint": binding.canonical_sha256({"candidate": candidate["amapId"]}),
            "classification": "admitted",
            "scoreEligible": True,
        },
    )

    certificate = binding.build_canonical_identity_binding_certificate(
        place_quarantine={},
        preflight_manifest={},
        envelope={},
        runtime_evidence={},
    )

    assert [row["ordinal"] for row in certificate["occurrences"]] == [1, 2]
    assert [row["responseOrdinal"] for row in certificate["occurrences"]] == [1, 3]
    assert [evidence["ordinal"] for evidence in certificate["occurrences"][0]["supportingEvidence"]] == [1, 2]
    binding._validate_certificate_shape(certificate)


def test_zero_network_downstream_tooling_seals_synthetic_recorded_evidence_and_replays_exactly_once(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Exercise Z0 only with recorded-shaped synthetic, never-live data."""

    import copy
    import os
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from importlib.util import find_spec

    module_name = "backend.evals.recorded_fixture_canonical_binding"
    assert find_spec(module_name) is not None, (
        "zero-network canonical identity binding / exact route manifest / "
        "one-shot route control and immutable overlay tooling is missing"
    )
    binding = import_module(module_name)
    route_capture = import_module("backend.evals.recorded_fixture_route_capture")
    combined = import_module("backend.evals.recorded_fixture_combined_evidence")

    production_validator_calls = []
    monkeypatch.setattr(
        binding,
        "validate_capture_preflight_manifest",
        lambda **kwargs: production_validator_calls.append(("preflight", kwargs)),
    )
    monkeypatch.setattr(
        binding,
        "validate_zero_network_capture_session_envelope",
        lambda **kwargs: production_validator_calls.append(("envelope", kwargs)),
    )

    canonical_sha256 = recorded_fixture_capture_module.canonical_sha256
    values = {
        "source": "1" * 64,
        "case": "2" * 64,
        "runtime": "3" * 64,
        "preflight": "4" * 64,
        "allowlist": "5" * 64,
        "route": "6" * 64,
        "profile_one": "7" * 64,
        "profile_two": "8" * 64,
        "occurrence_one": "9" * 64,
        "occurrence_two": "a" * 64,
        "plan_one": "b" * 64,
        "plan_two": "c" * 64,
        "source_plan_one": "d" * 64,
        "source_plan_two": "e" * 64,
        "spec_one": "f" * 64,
        "spec_two": "0" * 64,
    }
    request_rows = []
    for ordinal, suffix in enumerate(("one", "two"), start=1):
        query_scope_fingerprint = canonical_sha256(
            {
                "occurrenceFingerprint": values[f"occurrence_{suffix}"],
                "sourcePlanFingerprint": values[f"source_plan_{suffix}"],
                "providerPlanFingerprint": values[f"plan_{suffix}"],
                "requestShape": {
                    "endpoint": "place/text",
                    "city": "北京",
                    "keyword": f"synthetic-{ordinal}",
                    "category": "餐饮服务",
                    "limit": 12,
                    "radius": 0,
                },
            }
        )
        values[f"scope_{suffix}"] = query_scope_fingerprint
        request_rows.append(
            {
                "endpoint": "place/text",
                "sanitizedParams": {"keywords": f"synthetic-{ordinal}", "city": "北京"},
                "requestFingerprint": canonical_sha256({"request": ordinal}),
                "auditFingerprint": canonical_sha256({"audit": ordinal}),
                "profileOccurrence": {
                    "briefId": "brief-z0",
                    "poolId": f"pool-z0-{ordinal}",
                    "planningSlotId": f"slot-z0-{ordinal}",
                    "dayNumber": 1,
                    "profileFingerprint": values[f"profile_{suffix}"],
                    "occurrenceFingerprint": values[f"occurrence_{suffix}"],
                },
                "queryPlanLineage": {
                    "sourcePlanId": f"source-plan-{ordinal}",
                    "sourcePlanFingerprint": values[f"source_plan_{suffix}"],
                    "providerPlanId": f"provider-plan-{ordinal}",
                    "providerPlanFingerprint": values[f"plan_{suffix}"],
                    "queryScopeFingerprint": query_scope_fingerprint,
                },
            }
        )
    request_multiset = [
        {
            "auditFingerprint": request["auditFingerprint"],
            "requestFingerprint": request["requestFingerprint"],
            "count": 1,
        }
        for request in sorted(
            request_rows,
            key=lambda item: (
                item["auditFingerprint"],
                item["requestFingerprint"],
            ),
        )
    ]
    request_multiset_fingerprint = canonical_sha256(request_multiset)
    allowlist_material = {
        "requestCount": len(request_rows),
        "requests": request_rows,
        "requestMultiset": request_multiset,
        "requestMultisetFingerprint": request_multiset_fingerprint,
    }
    allowlist = {
        **allowlist_material,
        "allowlistFingerprint": canonical_sha256(allowlist_material),
    }
    values["allowlist"] = allowlist["allowlistFingerprint"]
    manifest = {
        "captureState": "ready_for_place_identity_capture",
        "terminalStatus": None,
        "manifestFingerprint": values["preflight"],
        "sourceFingerprint": values["source"],
        "dedicatedCaseSha256": values["case"],
        "runtimeEvidenceSha256": values["runtime"],
        "routeContractFingerprint": values["route"],
    }
    envelope = {
        "status": "prepared",
        "captureScope": "place_only",
        "routeCaptureAuthorized": False,
        "envelopeFingerprint": "f" * 64,
        "sourceBindings": {
            "sourceFingerprint": values["source"],
            "dedicatedCaseSha256": values["case"],
            "runtimeEvidenceSha256": values["runtime"],
            "capturePreflightManifestFingerprint": values["preflight"],
        },
        "exactPlaceRequestAllowlist": allowlist,
    }
    profiles = []
    slots = []
    from src.services.consumer_candidate_admission_service import (
        ConsumerCandidateAdmissionService,
    )
    from src.services.creative_planning_models import (
        ConstraintLedger,
        canonical_fingerprint,
    )
    from src.services.creative_portfolio_provider_service import InitialCreativePortfolio
    from src.services.creative_portfolio_staging_service import (
        CreativePortfolioStagingService,
    )
    from src.services.shared_candidate_universe_service import (
        SharedCandidateUniverseBuilder,
    )

    for ordinal, suffix in enumerate(("one", "two"), start=1):
        profile_scope = {
            "briefId": "brief-z0",
            "poolId": f"pool-z0-{ordinal}",
            "planningSlotId": f"slot-z0-{ordinal}",
            "dayNumber": 1,
        }
        experience_spec_policy = {
            "allowedDayNumbers": [1],
            "experienceFamilies": ["meal"],
            "accessPolicy": "verified_amap_food_service",
            "evidenceFreshness": {"maxAgeHours": 24},
            "unresolvedDimensions": [],
        }
        values[f"spec_{suffix}"] = canonical_fingerprint(experience_spec_policy)
        route_context = {
            "routeDecisionContract": {"fingerprint": values["route"]},
            "experienceSpecPolicy": copy.deepcopy(experience_spec_policy),
            "specFingerprint": values[f"spec_{suffix}"],
            "requiresProviderInsertionDecision": True,
        }
        profiles.append(
            {
                "profileFingerprint": values[f"profile_{suffix}"],
                "scope": profile_scope,
                "semanticRole": {
                    "experienceFamily": "meal",
                    "intentType": "meal",
                    "requirementLevel": "required",
                },
                "city": "北京",
                "consumerAdmissionInput": (
                    ConsumerCandidateAdmissionService.build_consumer_context(
                        brief_id=profile_scope["briefId"],
                        pool_id=profile_scope["poolId"],
                        planning_slot_id=profile_scope["planningSlotId"],
                        day_number=profile_scope["dayNumber"],
                        city="北京",
                        family="meal",
                        activity_mode="meal",
                        requirement_level="required",
                        experience_shape="single_poi",
                        experience_goal="当地特色午餐",
                        route_context=route_context,
                        experience_spec_policy=experience_spec_policy,
                        spec_fingerprint=values[f"spec_{suffix}"],
                    )
                ),
                "queryPlans": [
                    {
                        "planId": f"provider-plan-{ordinal}",
                        "sourcePlanId": f"source-plan-{ordinal}",
                        "sourcePlanFingerprint": values[f"source_plan_{suffix}"],
                        "providerPlanFingerprint": values[f"plan_{suffix}"],
                        "endpoint": "place/text",
                        "city": "北京",
                        "keyword": f"synthetic-{ordinal}",
                        "category": "餐饮服务",
                        "resultLimit": 12,
                        "radiusMeters": 1000,
                    }
                ],
            }
        )
        slots.append(
            {
                "briefId": "brief-z0",
                "poolId": f"pool-z0-{ordinal}",
                "planningSlotId": f"slot-z0-{ordinal}",
                "dayNumber": 1,
                "startTime": f"{9 + ordinal}:00",
            }
        )
    runtime_evidence = {
        "cases": [
            {
                "recordedCaptureSemanticTrace": {
                    "sourceFingerprint": values["source"],
                    "profileRuns": profiles,
                    "logicalSlots": slots,
                }
            }
        ]
    }
    semantic_trace = runtime_evidence["cases"][0]["recordedCaptureSemanticTrace"]
    semantic_trace.update(
        {
            "schemaVersion": "recorded-capture-semantic-trace-v1",
            "caseId": "synthetic-z0-case",
            "dedicatedCaseSha256": values["case"],
            "routeContractFingerprint": values["route"],
            "logicalRouteTopology": [
                {
                    "briefId": "brief-z0",
                    "dayNumber": 1,
                    "fromLogicalNode": "brief-z0::slot-z0-1",
                    "toLogicalNode": "brief-z0::slot-z0-2",
                    "mode": "",
                    "modeReason": "server_initial_plan_route_anchor_adjacency",
                }
            ],
            "logicalRouteTopologyErrors": [],
            "logicalRouteAuthorization": {
                "status": "ready",
                "preferredMode": "transit",
                "contractFingerprint": values["route"],
            },
        }
    )
    values["runtime"] = canonical_sha256(semantic_trace)
    semantic_trace["runtimeEvidenceSha256"] = values["runtime"]
    manifest["runtimeEvidenceSha256"] = values["runtime"]
    envelope["sourceBindings"]["runtimeEvidenceSha256"] = values["runtime"]
    records = []
    for ordinal, request in enumerate(request_rows, start=1):
        response = {
            "status": "1",
            "infocode": "10000",
            "pois": [
                {
                    "id": f"B0000000{ordinal:02d}",
                    "name": f"Synthetic district {ordinal}",
                    "type": "餐饮服务;中餐厅;北京菜",
                    "typecode": "050118",
                    "cityname": "北京",
                    "adname": "东城",
                    "address": "synthetic-safe-address",
                    "location": f"116.{ordinal},39.{ordinal}",
                    "photos": [],
                }
            ],
        }
        records.append(
            {
                "ordinal": ordinal,
                "requestFingerprint": request["requestFingerprint"],
                "auditFingerprint": request["auditFingerprint"],
                "request": {
                    "endpoint": "/v3/place/text",
                    "params": copy.deepcopy(request["sanitizedParams"]),
                },
                "response": response,
                "responseSha256": canonical_sha256(response),
            }
        )
    place_quarantine = {
        "schemaVersion": "trip-recorded-amap-v1",
        "recordedAt": "2026-08-14T00:00:00+00:00",
        "recordingType": "recorded/non-live",
        "recordedProvider": "AMap Web Service",
        "promotable": False,
        "quarantine": {
            "kind": "place_only_capture",
            "requestCount": 2,
            "completedResponseCount": 2,
            "routeCaptureAuthorized": False,
            "routeCalls": 0,
        },
        "responses": records,
    }
    place_quarantine["bundleFingerprint"] = canonical_sha256(place_quarantine)

    certificate = binding.build_canonical_identity_binding_certificate(
        place_quarantine=place_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
    )
    assert [name for name, _kwargs in production_validator_calls] == [
        "preflight",
        "envelope",
    ]
    assert certificate["recordingType"] == "recorded/non-live"
    assert certificate["promotable"] is False
    assert [row["canonicalIdentity"]["amapId"] for row in certificate["occurrences"]] == [
        "B000000001",
        "B000000002",
    ]
    binding.validate_canonical_identity_binding_certificate(
        certificate=certificate,
        place_quarantine=place_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
    )

    multi_place_quarantine = copy.deepcopy(place_quarantine)
    for ordinal, record in enumerate(multi_place_quarantine["responses"], start=1):
        alternate = copy.deepcopy(record["response"]["pois"][0])
        alternate.update(
            {
                "id": f"B0000001{ordinal:02d}",
                "name": f"Synthetic alternate district {ordinal}",
                "location": f"116.{ordinal + 2},39.{ordinal + 2}",
            }
        )
        record["response"]["pois"].append(alternate)
        record["responseSha256"] = canonical_sha256(record["response"])
    multi_place_quarantine["bundleFingerprint"] = canonical_sha256(
        {key: value for key, value in multi_place_quarantine.items() if key != "bundleFingerprint"}
    )

    candidate_universe = binding.build_canonical_candidate_universe_certificate(
        place_quarantine=multi_place_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
    )
    assert all(occurrence["candidateCounts"]["admitted"] >= 2 for occurrence in candidate_universe["occurrences"])
    universe = SharedCandidateUniverseBuilder().build(
        binding.canonical_candidate_universe_pool_reports(candidate_universe)
    )
    ledger = ConstraintLedger.model_validate(
        {
            "schemaVersion": "constraint-ledger-v1",
            "city": "北京",
            "dayCount": 1,
            "hardGoals": [
                {
                    "goalId": f"goal-z0-{ordinal}",
                    "intentType": "meal",
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "allowedDayNumbers": [1],
                    "userExplicit": True,
                    "priorityTier": "hard",
                    "accessPolicy": "verified_amap_food_service",
                    "evidenceFreshness": {"maxAgeHours": 24},
                    "experienceFamilies": ["meal"],
                    "unresolvedDimensions": [],
                }
                for ordinal in (1, 2)
            ],
            "transportPreferences": ["public_transit"],
            "sourceFingerprint": values["source"],
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "brief-z0",
                        "title": "Synthetic production staging",
                        "primaryAxis": "food_led",
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "two scoped meal anchors",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["two explicit hard occurrences"],
                            }
                        ],
                        "requiredGoalIds": ["goal-z0-1", "goal-z0-2"],
                    },
                    "daySlots": [
                        {
                            "slotId": f"slot-z0-{ordinal}",
                            "dayNumber": 1,
                            "timeWindow": "lunch",
                            "startTime": f"{9 + ordinal}:00",
                            "durationMinutes": 60,
                            "kind": "meal",
                            "rawNeed": "当地特色午餐",
                            "routeAnchor": True,
                            "requiredGoalId": f"goal-z0-{ordinal}",
                            "requirementLevel": "required",
                            "experienceShape": "single_poi",
                            "experienceGoal": "当地特色午餐",
                            "routeContract": {
                                "routeDecisionContract": {
                                    "fingerprint": values["route"],
                                }
                            },
                        }
                        for ordinal in (1, 2)
                    ],
                    "intentPools": [
                        {
                            "poolId": f"pool-z0-{ordinal}",
                            "briefId": "brief-z0",
                            "rawNeed": "当地特色午餐",
                            "city": "北京",
                            "intentType": "meal",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": f"goal-z0-{ordinal}",
                            "assignToSlots": [f"slot-z0-{ordinal}"],
                            "routePreference": {
                                "routeDecisionContract": {
                                    "fingerprint": values["route"],
                                }
                            },
                        }
                        for ordinal in (1, 2)
                    ],
                }
            ],
        }
    )
    occurrence_plan = {
        "schemaVersion": "goal-occurrence-plan-v1",
        "avoidRecentEntities": True,
        "sourceFingerprint": values["source"],
        "occurrences": [
            {
                "occurrenceId": f"occ:goal-z0-{ordinal}:day:1",
                "sourceGoalId": f"goal-z0-{ordinal}",
                "intentType": "meal",
                "dayNumber": 1,
                "requirementLevel": "hard",
                "userExplicit": True,
                "accessPolicy": "verified_amap_food_service",
                "evidenceFreshness": {"maxAgeHours": 24},
                "allowedDayNumbers": [1],
                "experienceFamilies": ["meal"],
                "unresolvedDimensions": [],
            }
            for ordinal in (1, 2)
        ],
    }

    class _NoWriteStore:
        def create(self, *_args, **_kwargs):
            raise AssertionError("recorded evidence bridge must remain zero-write")

        def offer_repaired_proposal(self, **_kwargs):
            raise AssertionError("recorded evidence bridge must not offer a proposal")

        def visible_comparison_projections(self, *, portfolio_id):
            del portfolio_id
            return []

    def build_staged_snapshot(required, _soft, _optional, _brief_id):
        segments = []
        for candidate in sorted(required.values(), key=lambda item: str(item["planningSlotId"])):
            segments.append(
                {
                    "startTime": str(candidate["startTime"]),
                    "poi": copy.deepcopy(candidate),
                    "semanticMetadata": {
                        "routeAnchor": True,
                        "creativeBriefId": str(candidate["briefId"]),
                        "poolId": str(candidate["poolId"]),
                        "planningSlotId": str(candidate["planningSlotId"]),
                        "occurrenceId": str(candidate["occurrenceId"]),
                        "startTime": str(candidate["startTime"]),
                        "consumerAdmissionReport": copy.deepcopy(candidate["consumerAdmissionReport"]),
                    },
                }
            )
        return {"city": "北京", "days": [{"dayNumber": 1, "segments": segments}]}

    staging_service = CreativePortfolioStagingService(_NoWriteStore(), experience_grounding_v2_mode="enforce")
    _portfolio, _visible = staging_service.stage(
        session_id="session-z0",
        source_user_turn_id="user-turn-z0",
        source_assistant_turn_id="assistant-turn-z0",
        expected_base_version_id=None,
        observation_fingerprint="1" * 16,
        request_fingerprint="2" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=build_staged_snapshot,
        city="北京",
        goal_occurrence_plan=occurrence_plan,
        persist_result=False,
    )
    assert len(staging_service.last_pre_route_selected_snapshots) == 1
    staged_snapshot = staging_service.last_pre_route_selected_snapshots[0]
    staged_ids = [segment["poi"]["amapId"] for segment in staged_snapshot["days"][0]["segments"]]
    assert staged_ids == ["B000000001", "B000000002"]
    staged_certificate = binding.build_provisional_staged_anchor_binding_certificate(
        place_quarantine=multi_place_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        staged_itinerary_snapshot=staged_snapshot,
    )
    binding.validate_provisional_staged_anchor_binding_certificate(
        certificate=staged_certificate,
        place_quarantine=multi_place_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        staged_itinerary_snapshot=staged_snapshot,
    )
    assert (
        staged_certificate["candidateUniverseCertificateFingerprint"] == (candidate_universe["certificateFingerprint"])
    )
    assert [row["canonicalIdentity"]["amapId"] for row in staged_certificate["occurrences"]] == [
        "B000000001",
        "B000000002",
    ]
    assert [
        [alternative["canonicalIdentity"]["amapId"] for alternative in row["candidateAlternatives"]]
        for row in staged_certificate["occurrences"]
    ] == [
        ["B000000001", "B000000101"],
        ["B000000002", "B000000102"],
    ]
    assert staged_certificate["finalIdentitySelectionComplete"] is False
    assert staged_certificate["routeMatrixRequired"] is True

    offline_evaluator = import_module("backend.evals.run_offline")
    provisional_dry_trace = offline_evaluator.issue_production_dry_route_trace(
        capture_semantic_trace=semantic_trace,
        anchor_binding_certificate_fingerprint=staged_certificate["certificateFingerprint"],
        planning_root={
            "rootPortfolioId": "portfolio-z0",
            "planningRoot": "root-turn-z0",
            "briefId": "brief-z0",
        },
    )
    provisional_route_manifest = binding.build_exact_route_pair_mode_manifest(
        anchor_binding_certificate=staged_certificate,
        dry_route_trace=provisional_dry_trace,
    )
    binding.validate_exact_route_pair_mode_manifest(
        route_manifest=provisional_route_manifest,
        anchor_binding_certificate=staged_certificate,
        dry_route_trace=provisional_dry_trace,
    )
    assert provisional_route_manifest["pairCount"] == 4
    assert {
        (lease["fromAmapId"], lease["toAmapId"], lease["mode"])
        for lease in provisional_route_manifest["orderedRouteLeases"]
    } == {
        ("B000000001", "B000000002", "transit"),
        ("B000000001", "B000000102", "transit"),
        ("B000000101", "B000000002", "transit"),
        ("B000000101", "B000000102", "transit"),
    }
    assert provisional_route_manifest["finalIdentitySelectionComplete"] is False
    assert provisional_route_manifest["canonicalIdentityCertificateFingerprint"] is None
    assert (
        provisional_route_manifest["selectedAnchorBindingCertificateFingerprint"]
        == staged_certificate["certificateFingerprint"]
    )
    assert "canonicalIdentityCertificateFingerprint" not in provisional_dry_trace
    assert (
        provisional_dry_trace["anchorBindingCertificateFingerprint"] == (staged_certificate["certificateFingerprint"])
    )

    reordered_quarantine = copy.deepcopy(multi_place_quarantine)
    for record in reordered_quarantine["responses"]:
        record["response"]["pois"].reverse()
        record["responseSha256"] = canonical_sha256(record["response"])
    reordered_quarantine["bundleFingerprint"] = canonical_sha256(
        {key: value for key, value in reordered_quarantine.items() if key != "bundleFingerprint"}
    )
    reordered_certificate = binding.build_provisional_staged_anchor_binding_certificate(
        place_quarantine=reordered_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        staged_itinerary_snapshot=staged_snapshot,
    )
    reordered_trace = offline_evaluator.issue_production_dry_route_trace(
        capture_semantic_trace=semantic_trace,
        anchor_binding_certificate_fingerprint=reordered_certificate["certificateFingerprint"],
        planning_root={
            "rootPortfolioId": "portfolio-z0",
            "planningRoot": "root-turn-z0",
            "briefId": "brief-z0",
        },
    )
    reordered_manifest = binding.build_exact_route_pair_mode_manifest(
        anchor_binding_certificate=reordered_certificate,
        dry_route_trace=reordered_trace,
    )
    assert [
        {key: lease[key] for key in ("fromAmapId", "toAmapId", "mode", "providerRequest")}
        for lease in reordered_manifest["orderedRouteLeases"]
    ] == [
        {key: lease[key] for key in ("fromAmapId", "toAmapId", "mode", "providerRequest")}
        for lease in provisional_route_manifest["orderedRouteLeases"]
    ]

    over_budget_quarantine = copy.deepcopy(place_quarantine)
    for ordinal, record in enumerate(over_budget_quarantine["responses"], start=1):
        seed = copy.deepcopy(record["response"]["pois"][0])
        record["response"]["pois"] = []
        for candidate_ordinal in range(5):
            candidate = copy.deepcopy(seed)
            candidate.update(
                {
                    "id": f"B{ordinal:02d}{candidate_ordinal:07d}",
                    "name": f"Synthetic budget candidate {ordinal}-{candidate_ordinal}",
                    "location": (f"116.{ordinal}{candidate_ordinal},39.{ordinal}{candidate_ordinal}"),
                }
            )
            record["response"]["pois"].append(candidate)
        record["responseSha256"] = canonical_sha256(record["response"])
    over_budget_quarantine["bundleFingerprint"] = canonical_sha256(
        {key: value for key, value in over_budget_quarantine.items() if key != "bundleFingerprint"}
    )
    over_budget_universe = binding.build_canonical_candidate_universe_certificate(
        place_quarantine=over_budget_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
    )
    over_budget_shared_universe = SharedCandidateUniverseBuilder().build(
        binding.canonical_candidate_universe_pool_reports(over_budget_universe)
    )
    over_budget_staging = CreativePortfolioStagingService(_NoWriteStore(), experience_grounding_v2_mode="enforce")
    over_budget_staging.stage(
        session_id="session-z0-budget",
        source_user_turn_id="user-turn-z0-budget",
        source_assistant_turn_id="assistant-turn-z0-budget",
        expected_base_version_id=None,
        observation_fingerprint="3" * 16,
        request_fingerprint="4" * 16,
        ledger=ledger,
        generated=generated,
        universe=over_budget_shared_universe,
        snapshot_builder=build_staged_snapshot,
        city="北京",
        goal_occurrence_plan=occurrence_plan,
        persist_result=False,
    )
    over_budget_certificate = binding.build_provisional_staged_anchor_binding_certificate(
        place_quarantine=over_budget_quarantine,
        preflight_manifest=manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        staged_itinerary_snapshot=(over_budget_staging.last_pre_route_selected_snapshots[0]),
    )
    over_budget_trace = offline_evaluator.issue_production_dry_route_trace(
        capture_semantic_trace=semantic_trace,
        anchor_binding_certificate_fingerprint=over_budget_certificate["certificateFingerprint"],
        planning_root={
            "rootPortfolioId": "portfolio-z0",
            "planningRoot": "root-turn-z0",
            "briefId": "brief-z0",
        },
    )
    with pytest.raises(
        binding.CanonicalBindingError,
        match="candidate_route_manifest_budget_exceeded",
    ):
        binding.build_exact_route_pair_mode_manifest(
            anchor_binding_certificate=over_budget_certificate,
            dry_route_trace=over_budget_trace,
        )

    stale_staged_snapshot = copy.deepcopy(staged_snapshot)
    stale_staged_snapshot["days"][0]["segments"][0]["semanticMetadata"]["consumerAdmissionReport"][
        "consumerFingerprint"
    ] = "0" * 64
    with pytest.raises(
        binding.CanonicalBindingError,
        match="staged_consumer_admission_stale",
    ):
        binding.build_provisional_staged_anchor_binding_certificate(
            place_quarantine=multi_place_quarantine,
            preflight_manifest=manifest,
            envelope=envelope,
            runtime_evidence=runtime_evidence,
            staged_itinerary_snapshot=stale_staged_snapshot,
        )
    tampered_multiset_envelope = copy.deepcopy(envelope)
    tampered_multiset_allowlist = tampered_multiset_envelope["exactPlaceRequestAllowlist"]
    tampered_multiset_allowlist["requestMultiset"][0]["count"] = 2
    tampered_multiset_allowlist["allowlistFingerprint"] = canonical_sha256(
        {key: value for key, value in tampered_multiset_allowlist.items() if key != "allowlistFingerprint"}
    )
    with pytest.raises(binding.CanonicalBindingError, match="allowlist_multiset"):
        binding.build_canonical_identity_binding_certificate(
            place_quarantine=place_quarantine,
            preflight_manifest=manifest,
            envelope=tampered_multiset_envelope,
            runtime_evidence=runtime_evidence,
        )

    tampered_allowlist_fingerprint_envelope = copy.deepcopy(envelope)
    tampered_allowlist_fingerprint_envelope["exactPlaceRequestAllowlist"]["allowlistFingerprint"] = "0" * 64
    with pytest.raises(binding.CanonicalBindingError, match="allowlist_fingerprint"):
        binding.build_canonical_identity_binding_certificate(
            place_quarantine=place_quarantine,
            preflight_manifest=manifest,
            envelope=tampered_allowlist_fingerprint_envelope,
            runtime_evidence=runtime_evidence,
        )

    duplicate_place = copy.deepcopy(place_quarantine)
    duplicate_place["responses"][1]["response"]["pois"][0]["id"] = "B000000001"
    duplicate_place["responses"][1]["responseSha256"] = canonical_sha256(duplicate_place["responses"][1]["response"])
    duplicate_place["bundleFingerprint"] = canonical_sha256(
        {key: value for key, value in duplicate_place.items() if key != "bundleFingerprint"}
    )
    with pytest.raises(binding.CanonicalBindingError, match="duplicate"):
        binding.build_canonical_identity_binding_certificate(
            place_quarantine=duplicate_place,
            preflight_manifest=manifest,
            envelope=envelope,
            runtime_evidence=runtime_evidence,
        )

    dry_trace = offline_evaluator.issue_production_dry_route_trace(
        capture_semantic_trace=semantic_trace,
        anchor_binding_certificate_fingerprint=certificate["certificateFingerprint"],
        planning_root={
            "rootPortfolioId": "portfolio-z0",
            "planningRoot": "root-turn-z0",
            "briefId": "brief-z0",
        },
    )
    missing_topology = copy.deepcopy(dry_trace)
    missing_topology["captureSemanticTraceFingerprint"] = "0" * 64
    with pytest.raises(binding.CanonicalBindingError, match="production_dry_route_trace"):
        binding.build_exact_route_pair_mode_manifest(
            anchor_binding_certificate=certificate,
            dry_route_trace=missing_topology,
        )
    route_manifest = binding.build_exact_route_pair_mode_manifest(
        anchor_binding_certificate=certificate,
        dry_route_trace=dry_trace,
    )
    assert route_manifest["pairCount"] == 1
    assert route_manifest["orderedRouteLeases"][0]["mode"] == "transit"
    assert route_manifest["productionDryRouteTraceFingerprint"] == dry_trace["productionDryRouteTraceFingerprint"]
    assert route_manifest["conditionalWalkingCapturePolicy"] == {
        "status": "not_authorized_until_preferred_transit_unavailable",
        "routeCaptureAuthorized": False,
        "maxCalls": 0,
    }
    binding.validate_exact_route_pair_mode_manifest(
        route_manifest=route_manifest,
        anchor_binding_certificate=certificate,
        dry_route_trace=dry_trace,
    )
    wrong_mode = copy.deepcopy(route_manifest)
    wrong_mode["orderedRouteLeases"][0]["mode"] = "walking"
    with pytest.raises(binding.CanonicalBindingError, match="tampered"):
        binding.validate_exact_route_pair_mode_manifest(
            route_manifest=wrong_mode,
            anchor_binding_certificate=certificate,
            dry_route_trace=dry_trace,
        )

    route_envelope = route_capture.build_zero_network_route_capture_envelope(route_manifest=route_manifest)
    route_capture.validate_zero_network_route_capture_envelope(
        envelope=route_envelope,
        route_manifest=route_manifest,
    )
    for field, bad_value in (
        ("host", "unexpected.example"),
        ("path", "/v3/direction/driving"),
        ("fromAmapId", "B000000099"),
        ("mode", "walking"),
    ):
        tampered_route_envelope = copy.deepcopy(route_envelope)
        tampered_route_envelope["exactRouteRequestAllowlist"]["requests"][0][field] = bad_value
        with pytest.raises(route_capture.RouteCaptureError, match="tampered"):
            route_capture.validate_zero_network_route_capture_envelope(
                envelope=tampered_route_envelope,
                route_manifest=route_manifest,
            )
    extra_param_envelope = copy.deepcopy(route_envelope)
    extra_param_envelope["exactRouteRequestAllowlist"]["requests"][0]["params"]["unexpected"] = "must-fail"
    with pytest.raises(route_capture.RouteCaptureError, match="tampered"):
        route_capture.validate_zero_network_route_capture_envelope(
            envelope=extra_param_envelope,
            route_manifest=route_manifest,
        )
    staging_root = tmp_path / "z0-route-staging"
    staging_root.mkdir()
    root_details = os.stat(staging_root, follow_symlinks=False)
    root_binding = canonical_sha256(
        {
            "stagingRootIdentity": {
                "device": root_details.st_dev,
                "file": root_details.st_ino,
                "createdAtNs": root_details.st_ctime_ns,
            }
        }
    )
    now = datetime.now(timezone.utc)
    route_authorization = {
        "authorizationId": "route-auth-z0-0001",
        "scope": "route_only",
        "sourceFingerprint": values["source"],
        "routeEnvelopeFingerprint": route_envelope["envelopeFingerprint"],
        "routeManifestFingerprint": route_manifest["routeManifestFingerprint"],
        "exactRouteRequestAllowlistFingerprint": route_envelope["exactRouteRequestAllowlist"]["allowlistFingerprint"],
        "maxCalls": 1,
        "issuedAt": (now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }
    network_authorization = {
        "schemaVersion": "trip-zero-network-route-network-authorization-v1",
        "networkAuthorizationId": "route-network-z0-0001",
        "scope": "route_only",
        "transportKind": "amap_route_https",
        "routeAuthorizationId": route_authorization["authorizationId"],
        "routeAuthorizationFingerprint": canonical_sha256(route_authorization),
        "sourceFingerprint": values["source"],
        "routeEnvelopeFingerprint": route_envelope["envelopeFingerprint"],
        "routeManifestFingerprint": route_manifest["routeManifestFingerprint"],
        "exactRouteRequestAllowlistFingerprint": route_envelope["exactRouteRequestAllowlist"]["allowlistFingerprint"],
        "outboundRequestSequenceFingerprint": canonical_sha256(
            route_envelope["exactRouteRequestAllowlist"]["requests"]
        ),
        "maxCalls": 1,
        "sessionDirectoryName": "route-session-z0-0001",
        "stagingRootBindingFingerprint": root_binding,
        "transportProfile": route_capture.ROUTE_TRANSPORT_PROFILE,
        "issuedAt": (now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }
    cli_root = tmp_path / "z0-route-cli-readiness"
    cli_root.mkdir()
    cli_details = os.stat(cli_root, follow_symlinks=False)
    cli_network_authorization = copy.deepcopy(network_authorization)
    cli_network_authorization["stagingRootBindingFingerprint"] = canonical_sha256(
        {
            "stagingRootIdentity": {
                "device": cli_details.st_dev,
                "file": cli_details.st_ino,
                "createdAtNs": cli_details.st_ctime_ns,
            }
        }
    )
    cli_inputs = tmp_path / "z0-route-cli-inputs"
    cli_inputs.mkdir()
    cli_paths = {}
    for name, value in {
        "manifest": route_manifest,
        "envelope": route_envelope,
        "route-auth": route_authorization,
        "network-auth": cli_network_authorization,
    }.items():
        path = cli_inputs / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        cli_paths[name] = path
    route_capture.validate_zero_network_route_capture_envelope(envelope=route_envelope, route_manifest=route_manifest)
    route_auth_fingerprint = route_capture._validate_route_authorization(
        route_authorization, envelope=route_envelope, now=now
    )
    assert route_capture._validate_route_network_authorization(
        network_authorization,
        route_authorization=route_authorization,
        place_authorization_fingerprint=route_auth_fingerprint,
        envelope=route_envelope,
        now=now,
    ) == canonical_sha256(network_authorization)
    assert route_capture._validated_staging_root(cli_root) == cli_root
    too_long_route_authorization = {
        **route_authorization,
        "expiresAt": (now + timedelta(minutes=11)).isoformat(),
    }
    with pytest.raises(route_capture.RouteCaptureError, match="expired_or_not_yet_valid"):
        route_capture._validate_route_authorization(too_long_route_authorization, envelope=route_envelope, now=now)
    cli_exit = route_capture.main(
        [
            "--route-manifest",
            str(cli_paths["manifest"]),
            "--route-envelope",
            str(cli_paths["envelope"]),
            "--route-authorization",
            str(cli_paths["route-auth"]),
            "--network-authorization",
            str(cli_paths["network-auth"]),
            "--staging-root",
            str(cli_root),
        ]
    )
    assert cli_exit == 0, capsys.readouterr().out
    cli_ready = json.loads(capsys.readouterr().out)
    assert cli_ready["status"] == "READY_FOR_EXPLICIT_ZERO_NETWORK_ROUTE_CAPTURE"
    assert cli_ready["requestCount"] == 1
    assert list(cli_root.iterdir()) == []
    fake_route_transport = route_capture.ScriptedFakeRouteTransport([{"durationSeconds": 600, "distanceMeters": 900}])
    route_result = route_capture.execute_zero_network_route_capture(
        envelope=route_envelope,
        route_manifest=route_manifest,
        route_authorization=route_authorization,
        network_authorization=network_authorization,
        staging_root=staging_root,
        transport=fake_route_transport,
        now=now,
    )
    assert route_result["status"] == "completed"
    assert route_result["zeroEffectLedger"] == {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    assert len(fake_route_transport.calls) == 1
    route_bundle = json.loads(
        (staging_root / route_result["sessionDirectoryName"] / route_result["quarantineBundleFile"]).read_text(
            encoding="utf-8"
        )
    )
    route_capture.validate_route_capture_quarantine(
        bundle=route_bundle,
        route_envelope=route_envelope,
    )
    replay_transport = route_capture.ScriptedFakeRouteTransport([{"durationSeconds": 600, "distanceMeters": 900}])
    with pytest.raises(route_capture.RouteCaptureError, match="route_envelope_already_consumed"):
        route_capture.execute_zero_network_route_capture(
            envelope=route_envelope,
            route_manifest=route_manifest,
            route_authorization=route_authorization,
            network_authorization=network_authorization,
            staging_root=staging_root,
            transport=replay_transport,
            now=now,
        )
    assert replay_transport.calls == []

    partial_root = tmp_path / "z0-route-partial"
    partial_root.mkdir()
    partial_details = os.stat(partial_root, follow_symlinks=False)
    partial_route_authorization = {
        **route_authorization,
        "authorizationId": "route-auth-z0-0002",
    }
    partial_network_authorization = copy.deepcopy(network_authorization)
    partial_network_authorization["networkAuthorizationId"] = "route-network-z0-0002"
    partial_network_authorization["routeAuthorizationId"] = partial_route_authorization["authorizationId"]
    partial_network_authorization["routeAuthorizationFingerprint"] = canonical_sha256(partial_route_authorization)
    partial_network_authorization["sessionDirectoryName"] = "route-session-z0-0002"
    partial_network_authorization["stagingRootBindingFingerprint"] = canonical_sha256(
        {
            "stagingRootIdentity": {
                "device": partial_details.st_dev,
                "file": partial_details.st_ino,
                "createdAtNs": partial_details.st_ctime_ns,
            }
        }
    )
    partial_transport = route_capture.ScriptedFakeRouteTransport([{"durationSeconds": 0, "distanceMeters": 900}])
    partial_result = route_capture.execute_zero_network_route_capture(
        envelope=route_envelope,
        route_manifest=route_manifest,
        route_authorization=partial_route_authorization,
        network_authorization=partial_network_authorization,
        staging_root=partial_root,
        transport=partial_transport,
        now=now,
    )
    assert partial_result["status"] == "failed"
    assert partial_result["promotable"] is False
    assert partial_result["reasonCode"] == "route_response_nonpositive"

    overlay_root = tmp_path / "z0-combined-overlay"
    overlay_root.mkdir()
    sealed = combined.seal_combined_recorded_evidence(
        output_directory=overlay_root,
        case_id="synthetic-z0-case",
        place_quarantine=place_quarantine,
        route_quarantine=route_bundle,
        preflight_manifest=manifest,
        place_envelope=envelope,
        route_envelope=route_envelope,
        runtime_evidence=runtime_evidence,
        canonical_identity_certificate=certificate,
        route_manifest=route_manifest,
        dry_route_trace=dry_trace,
    )
    assert sealed["promotable"] is False
    stateful_replay = combined.StrictRecordedOverlayReplaySession(overlay_path=overlay_root / sealed["overlayFile"])
    first_stateful = stateful_replay.commit_opaque_choice(
        source_assistant_turn_id="turn-z0-0001",
        choice_id="choice-z0-0001",
    )
    duplicate_stateful = stateful_replay.commit_opaque_choice(
        source_assistant_turn_id="turn-z0-0001",
        choice_id="choice-z0-0001",
    )
    assert first_stateful["writeDelta"] == {"version": 1, "patch": 1, "routeWrite": 0}
    assert duplicate_stateful["writeDelta"] == {"version": 0, "patch": 0, "routeWrite": 0}
    assert stateful_replay.reload()["activeVersionId"] == first_stateful["activeVersionId"]
    with pytest.raises(combined.CombinedEvidenceError, match="choice_conflict"):
        stateful_replay.commit_opaque_choice(
            source_assistant_turn_id="turn-z0-0002",
            choice_id="choice-z0-0002",
        )
    replay = combined.run_strict_recorded_overlay_replay(
        overlay_path=overlay_root / sealed["overlayFile"],
        source_assistant_turn_id="turn-z0-0001",
        choice_id="choice-z0-0001",
    )
    assert replay["syntheticContractOnly"] is True
    assert replay["networkSentinel"] == {"network": 0, "amap": 0, "web": 0, "controller": 0}
    assert replay["preAdoptionWriteDelta"] == {"version": 0, "patch": 0, "routeWrite": 0}
    assert replay["firstOpaqueChoiceCommit"]["writeDelta"] == {"version": 1, "patch": 1, "routeWrite": 0}
    assert replay["duplicateOpaqueChoiceCommit"]["writeDelta"] == {"version": 0, "patch": 0, "routeWrite": 0}
    overlay_path = overlay_root / sealed["overlayFile"]
    tampered_overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    tampered_overlay["expectedRecords"][0]["responseSha256"] = "0" * 64
    overlay_path.write_text(json.dumps(tampered_overlay), encoding="utf-8")
    with pytest.raises(combined.CombinedEvidenceError, match="fingerprint"):
        combined.validate_combined_recorded_evidence_overlay(overlay_path=overlay_path)

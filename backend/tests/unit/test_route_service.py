from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Event, Lock
from urllib.parse import urlencode
from urllib.request import Request

import pytest

from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.services.amap_call_budget import (
    AmapCallBudget,
    amap_call_budget_scope,
    amap_route_repair_scope,
)
from src.services.amap_rate_limiter import SlidingWindowRateLimiter
from src.services.creative_planning_models import canonical_fingerprint
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.route_service import ROUTE_MAP_REQUEST_PARALLELISM, RouteProviderError, RouteService


@pytest.fixture(autouse=True)
def clear_route_service_cache():
    RouteService.clear_cache()
    yield
    RouteService.clear_cache()


class FakeAmapResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


def test_route_service_does_not_route_agent_text_night_view_anchor():
    service = RouteService(map_provider_key="amap-key")
    poi = POI(
        id="poi_draft_night",
        amap_id="B0DRAFTNIGHT",
        name="夜景观景点",
        city="北京",
        category="scenic",
        latitude=39.91,
        longitude=116.39,
        source="agent-text-timeline",
        confidence=0.91,
        source_note="groundingStatus：composite_poi；intentType：night_view；needsConcretePoi=true",
    )

    assert service._is_routeable_poi(poi) is False


def test_route_service_keeps_verified_amap_night_anchor_when_provenance_mentions_nearby_scope():
    service = RouteService(map_provider_key="amap-key")
    segment = ItinerarySegment(
        id="seg_verified_night",
        day_id="day_1",
        segment_order=3,
        kind="night_view",
        start_time="18:00",
        end_time="19:30",
        poi_id="poi_verified_night",
        transport_mode="transit",
        estimated_cost=0,
        notes="夜景开放状态待核验。",
        semantic_metadata={
            "intentType": "night_view",
            "routeAnchor": True,
            "routeAnchorExpected": True,
            "groundingStatus": "verified_amap",
        },
    )
    poi = POI(
        id="poi_verified_night",
        amap_id="B0VERIFIEDNIGHT",
        name="亮马河国际风情水岸夜景观景台",
        city="北京",
        category="scenic",
        latitude=39.952,
        longitude=116.474,
        source=AMAP_PLACE_SOURCE,
        confidence=0.95,
        source_note="simple_open_v1: 高德周边搜索结果身份绑定；周边范围仅约束候选，不代表路线已核验。",
    )

    assert (
        service._is_route_anchor_segment(
            segment,
            poi,
            allow_semantic_route_anchor=True,
        )
        is True
    )

    range_placeholder = replace(poi, name="景山公园夜景附近范围")
    assert (
        service._is_route_anchor_segment(
            segment,
            range_placeholder,
            allow_semantic_route_anchor=True,
        )
        is False
    )


def test_recorded_real_amap_transit_fixture_is_hash_bound_and_parsed_without_synthetic_route_data():
    """Exercise the production parser against an attributed, non-live capture."""

    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    recording = json.loads(fixture.read_text(encoding="utf-8"))
    route_record = next(
        item
        for item in recording["responses"]
        if item["request"]["endpoint"] == "/v3/direction/transit/integrated"
    )
    assert recording["recordingType"] == "recorded/non-live"
    assert recording["recordedAt"]
    assert route_record["requestPair"] == {
        "fromAmapId": "B000A7BD6C",
        "toAmapId": "B000A816R6",
        "mode": "transit",
    }
    assert route_record["responseSha256"] == sha256(
        json.dumps(route_record["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        route = RouteService()._build_amap_route(
            "recorded_real_transit",
            1,
            1,
            _recorded_poi(tsinghua),
            _recorded_poi(pku),
            "transit",
        )

    assert route.mode == "transit"
    assert route.distance_meters > 0
    assert route.duration_seconds > 0
    assert route.provider_payload["recordedProviderEvidence"] == {
        "recordingType": "recorded/non-live",
        "recordedAt": recording["recordedAt"],
        "fixture": fixture.name,
        "responseSha256": route_record["responseSha256"],
        "requestPair": route_record["requestPair"],
    }
    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
        "/v3/direction/transit/integrated",
    ]


def test_recorded_real_transit_build_routes_does_not_prefetch_walking():
    """The production matrix path must stop at a successful recorded transit leg."""

    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        routes = RouteService().build_routes(
            "recorded_real_transit_matrix",
            [_recorded_poi(tsinghua), _recorded_poi(pku)],
            transport_mode="transit",
            segments=[
                ItinerarySegment(
                    id="recorded_tsinghua",
                    day_id="recorded_day",
                    segment_order=1,
                    kind="visit",
                    start_time="09:00",
                    end_time="10:00",
                    poi_id=tsinghua.id,
                    transport_mode="transit",
                    estimated_cost=0,
                    notes="recorded transit source anchor",
                ),
                ItinerarySegment(
                    id="recorded_pku",
                    day_id="recorded_day",
                    segment_order=2,
                    kind="visit",
                    start_time="11:00",
                    end_time="12:00",
                    poi_id=pku.id,
                    transport_mode="transit",
                    estimated_cost=0,
                    notes="recorded transit destination anchor",
                ),
            ],
            preferred_mode_only=True,
        )

    assert len(routes) == 1
    assert routes[0].mode == "transit"
    assert routes[0].distance_meters > 0
    assert routes[0].duration_seconds > 0
    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
        "/v3/direction/transit/integrated",
    ]


def test_recorded_amap_replay_rejects_missing_metadata_and_response_hash_mismatch(tmp_path):
    from src.runtime.recorded_amap_replay import RecordedAmapReplayError, RecordedAmapTransport

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    original = json.loads(fixture.read_text(encoding="utf-8"))
    mutations = [
        ("recordingType", "live"),
        ("recordedAt", "not-a-timestamp"),
        ("responseSha256", "0" * 64),
    ]
    for field, value in mutations:
        mutated = json.loads(json.dumps(original))
        if field == "responseSha256":
            mutated["responses"][0][field] = value
        else:
            mutated[field] = value
        candidate = tmp_path / f"recorded-{field}.json"
        candidate.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(RecordedAmapReplayError):
            RecordedAmapTransport(candidate)

    route_index = next(
        index
        for index, record in enumerate(original["responses"])
        if str((record.get("request") or {}).get("endpoint") or "").startswith("/v3/direction/")
    )
    for label, mutate_pair in (
        ("missing-request-pair", lambda pair: pair.pop("requestPair")),
        ("wrong-request-pair-mode", lambda pair: pair["requestPair"].update({"mode": "walking"})),
        ("forged-request-pair-origin", lambda pair: pair["requestPair"].update({"fromAmapId": "B000A816R6"})),
    ):
        mutated = json.loads(json.dumps(original))
        mutate_pair(mutated["responses"][route_index])
        candidate = tmp_path / f"recorded-{label}.json"
        candidate.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(RecordedAmapReplayError, match="requestPair"):
            RecordedAmapTransport(candidate)

    route_request = original["responses"][route_index]["request"]
    transport = RecordedAmapTransport(fixture)
    unexpected_params = dict(route_request["params"])
    unexpected_params["unexpected"] = "route-replay-secret-sentinel"
    request = Request(
        "https://restapi.amap.com"
        + route_request["endpoint"]
        + "?"
        + urlencode(unexpected_params)
    )
    with pytest.raises(RecordedAmapReplayError, match="request not found") as unmatched_error:
        transport.urlopen(request)
    assert "route-replay-secret-sentinel" not in str(unmatched_error.value)

    wrong_host_request = Request(
        "https://recorded-example.invalid"
        + route_request["endpoint"]
        + "?"
        + urlencode(route_request["params"])
    )
    with pytest.raises(RecordedAmapReplayError, match="target is not allowed"):
        transport.urlopen(wrong_host_request)

    for disallowed_base in (
        "https://user:password@restapi.amap.com",
        "https://restapi.amap.com:443",
        "https://restapi.amap.com",
    ):
        disallowed_request = Request(
            disallowed_base
            + route_request["endpoint"]
            + "?"
            + urlencode(route_request["params"])
            + ("#fragment" if disallowed_base == "https://restapi.amap.com" else "")
        )
        with pytest.raises(RecordedAmapReplayError, match="target is not allowed"):
            transport.urlopen(disallowed_request)


def test_recorded_real_transit_pair_can_form_a_portfolio_adoption_ready_proposal_without_adoption_writes():
    """Bind a complete Portfolio proposal to exact recorded AMap evidence.

    The proposal store may persist comparison evidence, but it must not create
    itinerary/version/route rows before an opaque adoption choice is executed.
    """

    from src.core.config import get_settings
    from src.core.database import sqlite_path_from_url
    from src.services.creative_planning_models import (
        CreativeBrief,
        PlanCandidate,
        PlanPortfolio,
        PlanScoreVector,
        proposal_canonical_signature,
    )
    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
    from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
    from src.services.creative_proposal_title_service import CreativeProposalTitleService
    from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
    from src.services.map_poi_service import MapPoiService
    from src.services.plan_portfolio_store import PlanPortfolioStore
    from src.services.plan_proposal_verifier import PlanProposalVerifier
    from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer
    from src.services.proposal_readiness_service import ProposalReadinessService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    recording = json.loads(fixture.read_text(encoding="utf-8"))
    route_record = next(
        item
        for item in recording["responses"]
        if item["request"].get("endpoint") == "/v3/direction/transit/integrated"
    )
    brief = CreativeBrief(
        briefId="recorded_real_transit_brief",
        title="双校园公共交通访学",
        primaryAxis="classic",
        requiredGoalIds=["campus_tsinghua", "campus_pku"],
        dayRoles=[
            {
                "dayNumber": 1,
                "role": "高校访学",
                "targetRouteAnchors": 2,
                "densityEvidence": ["recorded_transit_pair=2"],
            }
        ],
    )

    with recorded_amap_replay_scope(fixture) as replay:
        places = MapPoiService()
        tsinghua = places.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = places.search("北京", "北京大学", "campus", limit=5).pois[0]
        route = RouteService()._build_amap_route(
            "recorded_real_ready",
            1,
            1,
            _recorded_poi(tsinghua),
            _recorded_poi(pku),
            "transit",
            from_segment_id="seg_tsinghua",
            to_segment_id="seg_pku",
        )

    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
        "/v3/direction/transit/integrated",
    ]
    assert route_record["requestPair"] == {
        "fromAmapId": tsinghua.id,
        "toAmapId": pku.id,
        "mode": route.mode,
    }
    assert route_record["responseSha256"] == sha256(
        json.dumps(route_record["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    admissions = ConsumerCandidateAdmissionService()

    def candidate_from_recorded_poi(poi):
        return {
            **poi.model_dump(by_alias=True),
            "amapId": poi.id,
            "briefId": brief.brief_id,
        }

    def segment(
        *,
        segment_id,
        poi,
        goal_id,
        start_time,
        end_time,
        estimated_cost=0.0,
        cost_evidence=None,
    ):
        occurrence_id = f"occ:{goal_id}:day:1"
        pool_id = f"pool:{goal_id}"
        planning_slot_id = f"slot:{goal_id}:day:1"
        candidate = {
            **candidate_from_recorded_poi(poi),
            "poolId": pool_id,
            "planningSlotId": planning_slot_id,
            "dayNumber": 1,
            "sourceGoalId": goal_id,
            "goalId": goal_id,
            "occurrenceId": occurrence_id,
            "intentType": "campus_visit",
            "requirementLevel": "required",
        }
        admission = admissions.evaluate(
            candidate,
            ConsumerCandidateAdmissionService.build_consumer_context(
                brief_id=brief.brief_id,
                pool_id=pool_id,
                planning_slot_id=planning_slot_id,
                day_number=1,
                city="北京",
                family="campus_visit",
                activity_mode="campus_visit",
                requirement_level="required",
                experience_shape="single_poi",
                experience_goal=goal_id,
            ),
        )
        assert admission["scoreEligible"] is True, admission
        candidate["consumerAdmissionReport"] = admission
        return {
            "id": segment_id,
            "kind": "visit",
            "startTime": start_time,
            "endTime": end_time,
            "estimatedCost": estimated_cost,
            "costEvidence": cost_evidence,
            "poi": candidate_from_recorded_poi(poi),
            "semanticMetadata": {
                "goalId": goal_id,
                "occurrenceId": occurrence_id,
                "intentType": "campus_visit",
                "poolId": pool_id,
                "planningSlotId": planning_slot_id,
                "creativeBriefId": brief.brief_id,
                "required": True,
                "routeAnchor": True,
                "groundingStatus": "selected",
                "consumerAdmissionReport": admission,
            },
        }, candidate

    route_cost_evidence = {
        "status": "recorded_provider_route_cost",
        "recordingType": recording["recordingType"],
        "recordedAt": recording["recordedAt"],
        "responseSha256": route_record["responseSha256"],
    }
    tsinghua_segment, tsinghua_candidate = segment(
        segment_id="seg_tsinghua",
        poi=tsinghua,
        goal_id="campus_tsinghua",
        start_time="09:00",
        end_time="11:00",
    )
    pku_segment, pku_candidate = segment(
        segment_id="seg_pku",
        poi=pku,
        goal_id="campus_pku",
        start_time="12:00",
        end_time="14:00",
        estimated_cost=route.cost_amount,
        cost_evidence=route_cost_evidence,
    )
    snapshot = {
        "city": "北京",
        "budgetTier": "medium",
        "creativeBrief": brief.model_dump(by_alias=True),
        "portfolioDayAnchorTargets": {"1": 2},
        "portfolioGoalOccurrencePlan": {
            "schemaVersion": "goal-occurrence-plan-v1",
            "occurrences": [
                {
                    "occurrenceId": "occ:campus_tsinghua:day:1",
                    "sourceGoalId": "campus_tsinghua",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
                {
                    "occurrenceId": "occ:campus_pku:day:1",
                    "sourceGoalId": "campus_pku",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
            ],
        },
        "portfolioDailyCapacityPlan": {
            "1": {
                "usableMinutes": 300,
                "plannedMinutes": 240,
                "routeReserveMinutes": 60,
                "bufferMinutes": 0,
                "intentionalFreeMinutes": 0,
                "unexplainedGapMinutes": 0,
                "targetRouteAnchors": 2,
            }
        },
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    tsinghua_segment,
                    pku_segment,
                ],
            }
        ],
        "portfolioRequiredCandidateBindings": [tsinghua_candidate, pku_candidate],
        "portfolioPendingSlots": [],
        "portfolioRouteVerificationRequired": True,
        "portfolioDensityDecisionSource": "recorded_real_transit_fixture",
        "portfolioTransportPreference": "transit",
        "routeOptions": [route],
    }
    snapshot["routeOptions"] = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
    snapshot = CreativePortfolioStagingService._reconcile_admitted_anchor_materialization(
        snapshot,
        admitted_candidates=[tsinghua_candidate, pku_candidate],
        skeleton=type("RecordedPortfolioSkeleton", (), {"brief": brief, "day_slots": []})(),
        admission_enforced=True,
    )
    snapshot = CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=lambda _context: {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {
                    "title": "清华北大公交研学",
                    "evidenceAmapIds": [tsinghua.id, pku.id],
                },
                {
                    "title": "清华学府公交行程",
                    "evidenceAmapIds": [tsinghua.id, pku.id],
                },
                {
                    "title": "北大清华访学路线",
                    "evidenceAmapIds": [tsinghua.id, pku.id],
                },
            ],
        },
        context={},
    )
    ledger = ConstraintLedgerCompiler().compile(
        {
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus_tsinghua", "intentType": "campus_visit"},
                    {"goalId": "campus_pku", "intentType": "campus_visit"},
                ]
            }
        }
    )

    verifier = PlanProposalVerifier(experience_grounding_v2_mode="enforce").verify(snapshot, ledger, brief)
    readiness = ProposalReadinessService.compute(snapshot, verifier=verifier)

    assert verifier["passed"] is True, "; ".join(str(item) for item in verifier.get("hardFailures") or [])
    assert readiness["adoptionReady"] is True, {
        "blockingReasons": readiness.get("blockingReasons"),
        "budgetStatus": readiness.get("budgetStatus"),
        "unknownCostSegmentCount": readiness.get("unknownCostSegmentCount"),
        "semanticCoverage": readiness.get("semanticCoverage"),
        "routeStatus": readiness.get("routeStatus"),
    }
    assert readiness["strictlyVerified"] is True
    assert verifier["dayAnchorTargets"] == {"1": brief.day_roles[0].target_route_anchors}
    assert verifier["dayAnchorActuals"] == {"1": brief.day_roles[0].target_route_anchors}
    assert verifier["requiredCandidateBindingActualCount"] == 2
    assert verifier["admissionMaterializationAudit"]["countInvariantPassed"] is True
    assert snapshot["routeOptions"][0]["distanceMeters"] == route.distance_meters > 0
    assert snapshot["routeOptions"][0]["durationSeconds"] == route.duration_seconds > 0
    assert {item["fromSegmentId"] for item in snapshot["routeOptions"]} == {"seg_tsinghua"}
    assert {item["toSegmentId"] for item in snapshot["routeOptions"]} == {"seg_pku"}
    assert {item["provider"] for item in snapshot["routeOptions"]} == {"amap-webservice"}
    assert {item["source"] for item in snapshot["routeOptions"]} == {"amap-webservice"}
    assert snapshot["portfolioPendingSlots"] == []

    candidate = PlanCandidate(
        proposalId="recorded_real_transit_proposal",
        portfolioId="recorded_real_transit_portfolio",
        brief=brief,
        itinerarySnapshot=snapshot,
        groundedEvidence=[
            {
                "amapIds": [tsinghua.id, pku.id],
                "routeResponseSha256": route_record["responseSha256"],
                "recordingType": recording["recordingType"],
            }
        ],
        score=PlanScoreVector(
            hardConstraintPassed=verifier["passed"],
            preferenceFit=100,
            thematicCoherence=100,
            experienceDiversity=100,
            routeEfficiency=100,
            pacingQuality=100,
            novelty=100,
            robustness=100,
            uncertaintyPenalty=0,
            estimatedCostCny=route.cost_amount,
        ),
        verifier=verifier,
        canonicalSignature=proposal_canonical_signature(snapshot),
        generationLineage={
            "provider": "recorded-amap",
            "recordingType": recording["recordingType"],
            "recordedAt": recording["recordedAt"],
            "routePair": route_record["requestPair"],
            "routeResponseSha256": route_record["responseSha256"],
        },
    )
    portfolio = PlanPortfolio(
        portfolioId=candidate.portfolio_id,
        sessionId="recorded_real_transit_session",
        sourceUserTurnId="recorded_real_transit_turn",
        sourceAssistantTurnId="recorded_real_transit_assistant",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=[candidate.proposal_id],
        visibleProposalIds=[candidate.proposal_id],
    )
    connection = sqlite3.connect(sqlite_path_from_url(get_settings().database_url))
    connection.row_factory = sqlite3.Row
    try:
        store = PlanPortfolioStore(connection)
        protected_tables = ("itinerary_versions", "itinerary_patches", "route_options")

        def table_content_fingerprint(table):
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            return sha256(
                json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()

        zero_write_before = {table: table_content_fingerprint(table) for table in protected_tables}
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in protected_tables
        )

        store.create(portfolio, [candidate])

        zero_write_after = {table: table_content_fingerprint(table) for table in protected_tables}
        assert zero_write_after == zero_write_before
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0]
            == 1
        )
        assert store.classification_counts(portfolio_id=portfolio.portfolio_id) == {
            "visibleComparisonProposalCount": 1,
            "partialComparisonProposalCount": 0,
            "verifiedComparisonProposalCount": 1,
            "adoptionReadyProposalCount": 1,
        }
        persisted = connection.execute(
            "SELECT snapshot_json, generation_lineage_json FROM agent_plan_proposals WHERE id = ?",
            (candidate.proposal_id,),
        ).fetchone()
        assert persisted is not None
        persisted_snapshot = json.loads(persisted["snapshot_json"])
        persisted_lineage = json.loads(persisted["generation_lineage_json"])
        assert persisted_snapshot["portfolioDensityDecisionSource"] == "recorded_real_transit_fixture"
        assert persisted_snapshot["portfolioDayAnchorTargets"] == {"1": 2}
        assert persisted_snapshot["creativeBrief"]["briefId"] == brief.brief_id
        assert persisted_snapshot["routeOptions"][0]["providerPayload"]["recordedProviderEvidence"] == {
            "recordingType": recording["recordingType"],
        "recordedAt": recording["recordedAt"],
        "fixture": fixture.name,
        "responseSha256": route_record["responseSha256"],
        "requestPair": route_record["requestPair"],
    }
        assert persisted_lineage["routeResponseSha256"] == route_record["responseSha256"]
    finally:
        connection.close()


def test_route_service_builds_multi_mode_candidates_with_selected_polyline(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        if "transit" in url:
            return FakeAmapResponse(
                b'{"status":"1","route":{"transits":[{"distance":"2100","duration":"1200","cost":"4","segments":[{"walking":{"steps":[{"instruction":"walk","distance":"100","duration":"60","polyline":"116.10,39.10;116.11,39.11"}]},"bus":{"buslines":[{"name":"metro","distance":"2000","duration":"1140","polyline":"116.11,39.11;116.20,39.20"}]}}]}]}}'
            )
        if "walking" in url:
            return FakeAmapResponse(
                b'{"status":"1","route":{"paths":[{"distance":"1800","duration":"1500","steps":[{"instruction":"walk","distance":"1800","duration":"1500","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
            )
        if "bicycling" in url:
            return FakeAmapResponse(
                b'{"errcode":0,"errmsg":"OK","data":{"paths":[{"distance":"2200","duration":"1100","steps":[{"instruction":"cycle","distance":"2200","duration":"1100","polyline":"116.10,39.10;116.16,39.16;116.20,39.20"}]}]}}'
            )
        return FakeAmapResponse(
            b'{"status":"1","route":{"taxi_cost":"28","paths":[{"distance":"3200","duration":"900","tolls":"0","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.15,39.15;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes("plan_route", amap_pois(), "walking", segments=segments())

    assert [route.mode for route in routes] == ["walking", "bicycling"]
    assert [route.is_selected for route in routes] == [True, False]
    assert [route.sort_order for route in routes] == [1, 2]
    assert all(route.from_segment_id == "seg_1" and route.to_segment_id == "seg_2" for route in routes)
    assert all(route.polyline for route in routes)
    assert routes[0].duration_seconds == 1500
    assert routes[0].cost_amount == 0
    assert len(fetched_urls) == 2
    assert sum("/v3/direction/driving" in url for url in fetched_urls) == 0
    assert sum("/v4/direction/bicycling" in url for url in fetched_urls) == 1
    assert sum("/v3/direction/walking" in url for url in fetched_urls) == 1


def test_initial_preferred_mode_coverage_submits_only_one_mode_per_pair(monkeypatch):
    calls = []

    def fake_fetch(_self, _from_poi, _to_poi, mode):
        calls.append(mode)
        return {
            "status": "1",
            "route": {
                "transits": [
                    {
                        "distance": "1000",
                        "duration": "600",
                        "cost": "3",
                        "segments": [
                            {
                                "walking": {"steps": []},
                                "bus": {
                                    "buslines": [
                                        {
                                            "name": "metro",
                                            "distance": "1000",
                                            "duration": "600",
                                            "polyline": "116.10,39.10;116.20,39.20",
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ]
            },
        }

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")
    routes = service.build_routes("plan_route", amap_pois(), "transit", segments=segments(), preferred_mode_only=True)

    assert calls == ["transit"]
    assert len(routes) == 1
    assert routes[0].mode == "transit"


def test_bounded_provider_alternatives_share_one_http_response_and_legacy_stays_first(monkeypatch):
    calls = []

    def fake_fetch(_self, _from_poi, _to_poi, mode):
        calls.append(mode)
        return {
            "status": "1",
            "route": {
                "paths": [
                    {
                        "distance": str(1000 + index * 100),
                        "duration": str(600 + index * 60),
                        "steps": [
                            {
                                "instruction": f"option-{index}",
                                "distance": str(1000 + index * 100),
                                "duration": str(600 + index * 60),
                                "polyline": f"116.10,39.10;116.{20 + index},39.20",
                            }
                        ],
                    }
                    for index in range(1, 6)
                ]
            },
        }

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")

    legacy = service.build_routes(
        "legacy",
        amap_pois(),
        "walking",
        segments=segments(),
        preferred_mode_only=True,
    )
    bounded = service.build_routes(
        "bounded",
        amap_pois(),
        "walking",
        segments=segments(),
        preferred_mode_only=True,
        include_provider_alternatives=True,
    )

    # One request for each explicit build call; the three bounded options are
    # all parsed from the second call's single Provider response.
    assert calls == ["walking", "walking"]
    assert len(legacy) == 1
    assert legacy[0].steps[0]["instruction"] == "option-1"
    assert len(bounded) == 3
    assert [route.steps[0]["instruction"] for route in bounded] == ["option-1", "option-2", "option-3"]
    assert [route.provider_payload["providerAlternativeIndex"] for route in bounded] == [1, 2, 3]
    assert all(route.provider_payload["providerAlternativeCount"] == 5 for route in bounded)
    assert all(route.provider_payload["providerAlternativesTruncated"] is True for route in bounded)
    assert all(len(route.provider_payload["providerAlternativeFingerprint"]) == 64 for route in bounded)
    assert all(
        route.id.endswith(route.provider_payload["providerAlternativeFingerprint"][:16])
        for route in bounded
    )


def test_provider_alternative_fingerprint_is_stable_when_response_order_changes() -> None:
    service = RouteService(map_provider_key="amap-key")
    options = [
        {
            "distance": str(1000 + index * 100),
            "duration": str(600 + index * 60),
            "steps": [{"polyline": f"116.10,39.10;116.{20 + index},39.20"}],
        }
        for index in range(1, 4)
    ]
    forward = service._bounded_provider_alternative_payloads(
        {"status": "1", "route": {"paths": options}},
        "walking",
    )
    reversed_order = service._bounded_provider_alternative_payloads(
        {"status": "1", "route": {"paths": list(reversed(options))}},
        "walking",
    )

    def by_distance(values):
        return {
            item["route"]["paths"][0]["distance"]: item["_tripProviderAlternative"]["fingerprint"]
            for item in values
        }

    assert by_distance(forward) == by_distance(reversed_order)


def test_compact_transit_refresh_includes_only_safe_fallback_modes(monkeypatch):
    calls = []

    def fake_fetch(_self, _from_poi, _to_poi, mode):
        calls.append(mode)
        raise RouteProviderError("capture only")

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes(
        "plan_route",
        amap_pois(),
        "transit",
        segments=segments(),
        include_compact_fallbacks=True,
    )

    assert sorted(calls) == ["transit", "walking"]
    assert routes == []


def test_route_service_prefers_transit_over_faster_taxi_when_requested(monkeypatch):
    def fake_fetch(_self, _from_poi, _to_poi, mode: str):
        if mode == "transit":
            return {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "distance": "3000",
                            "duration": "1800",
                            "cost": "5",
                            "segments": [
                                {
                                    "walking": {
                                        "steps": [
                                            {
                                                "instruction": "walk",
                                                "distance": "100",
                                                "duration": "60",
                                                "polyline": "116.10,39.10;116.11,39.11",
                                            }
                                        ]
                                    },
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "metro",
                                                "distance": "2900",
                                                "duration": "1740",
                                                "polyline": "116.11,39.11;116.20,39.20",
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ]
                },
            }
        path = {
            "distance": "3200" if mode == "taxi" else "2600",
            "duration": "900" if mode == "taxi" else "1400",
            "steps": [
                {"instruction": mode, "distance": "100", "duration": "60", "polyline": "116.10,39.10;116.20,39.20"}
            ],
        }
        if mode == "bicycling":
            return {"errcode": 0, "errmsg": "OK", "data": {"paths": [path]}}
        return {"status": "1", "route": {"taxi_cost": "28", "paths": [path]}}

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes("plan_route", amap_pois(), "transit", segments=segments())

    assert [route.mode for route in routes] == ["transit", "walking", "taxi", "driving"]
    assert routes[0].is_selected is True
    assert routes[0].mode == "transit"
    assert routes[1].duration_seconds == 1400
    assert routes[2].mode == "taxi"
    assert routes[3].mode == "driving"


def test_compact_transit_refresh_selects_provider_verified_short_walk_when_faster(monkeypatch):
    def fake_fetch(_self, _from_poi, _to_poi, mode: str):
        if mode == "transit":
            return {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "distance": "1800",
                            "duration": "1800",
                            "cost": "4",
                            "segments": [
                                {
                                    "walking": {"steps": []},
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "short transfer",
                                                "distance": "1800",
                                                "duration": "1800",
                                                "polyline": "116.10,39.10;116.20,39.20",
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ]
                },
            }
        distance = "900" if mode == "walking" else "1200"
        duration = "650" if mode == "walking" else "400"
        return {
            "status": "1",
            "route": {
                "taxi_cost": "20",
                "paths": [
                    {
                        "distance": distance,
                        "duration": duration,
                        "steps": [
                            {
                                "instruction": mode,
                                "distance": distance,
                                "duration": duration,
                                "polyline": "116.10,39.10;116.20,39.20",
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)

    routes = RouteService(map_provider_key="amap-key").build_routes(
        "plan_route",
        amap_pois(),
        "transit",
        segments=segments(),
        include_compact_fallbacks=True,
    )

    assert [route.mode for route in routes] == ["walking", "transit"]
    assert routes[0].is_selected is True
    assert routes[0].provider_payload["fallbackReason"] == "compact_walk_faster_than_transit"
    assert routes[0].provider_payload["preferredMode"] == "transit"
    assert routes[0].provider_payload["selectedMode"] == "walking"
    assert "高德实测步行比公交/地铁更省时" in routes[0].provider_payload["userVisibleCaveat"]


def test_route_service_marks_non_preferred_route_with_visible_caveat_when_transit_unavailable(monkeypatch):
    def fake_fetch(_self, _from_poi, _to_poi, mode: str):
        if mode == "transit":
            raise RuntimeError("transit unavailable")
        path = {
            "distance": "2200",
            "duration": "900",
            "steps": [
                {"instruction": mode, "distance": "2200", "duration": "900", "polyline": "116.10,39.10;116.20,39.20"}
            ],
        }
        if mode == "bicycling":
            return {"errcode": 0, "errmsg": "OK", "data": {"paths": [path]}}
        return {"status": "1", "route": {"paths": [path]}}

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes("plan_route", amap_pois(), "transit", segments=segments())

    assert routes[0].mode == "walking"
    assert routes[0].provider_payload["fallbackFromPreferredMode"] == "transit"
    assert routes[0].provider_payload["fallbackReason"] == "route_unavailable"
    assert routes[0].provider_payload["preferredMode"] == "transit"
    assert (
        routes[0].provider_payload["userVisibleCaveat"]
        == "用户偏好公交/地铁，但该段没有可用公交/地铁路线，暂用步行候选。"
    )


def test_route_service_keeps_provider_preferred_meal_mode_pending_matrix_decision(monkeypatch):
    def fake_fetch(_self, _from_poi, _to_poi, mode: str):
        if mode == "transit":
            return {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "distance": "16000",
                            "duration": "4800",
                            "cost": "4",
                            "segments": [
                                {
                                    "walking": {
                                        "steps": [
                                            {
                                                "instruction": "walk",
                                                "distance": "100",
                                                "duration": "60",
                                                "polyline": "116.10,39.10;116.11,39.11",
                                            }
                                        ]
                                    },
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "detour bus",
                                                "distance": "15900",
                                                "duration": "4740",
                                                "polyline": "116.11,39.11;116.20,39.20",
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ]
                },
            }
        path = {
            "distance": "3000",
            "duration": "900",
            "steps": [
                {"instruction": mode, "distance": "3000", "duration": "900", "polyline": "116.10,39.10;116.20,39.20"}
            ],
        }
        if mode == "bicycling":
            return {"errcode": 0, "errmsg": "OK", "data": {"paths": [path]}}
        return {"status": "1", "route": {"paths": [path]}}

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")
    route_segments = segments()
    route_segments[1].kind = "meal"

    routes = service.build_routes("plan_route", amap_pois(), "transit", segments=route_segments)

    assert routes[0].mode == "transit"
    boundary = routes[0].provider_payload["mealRouteSelectionBoundary"]
    assert boundary["decisionRole"] == "route_mode_candidate_ordering_only"
    assert boundary["providerRouteMatrixRequiredForDetourDecision"] is True
    assert boundary["fixedAbsoluteDetourThresholdApplied"] is False
    assert "fallbackFromPreferredMode" not in routes[0].provider_payload


def test_compact_routes_do_not_replace_transit_from_fixed_meal_threshold(monkeypatch):
    def fake_fetch(_self, _from_poi, _to_poi, mode: str):
        if mode == "transit":
            return {
                "status": "1",
                "route": {
                    "transits": [
                        {
                            "distance": "16000",
                            "duration": "4800",
                            "cost": "4",
                            "segments": [
                                {
                                    "walking": {"steps": []},
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "detour bus",
                                                "distance": "16000",
                                                "duration": "4800",
                                                "polyline": "116.10,39.10;116.20,39.20",
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                },
            }
        distance = "13000" if mode == "walking" else "9000"
        duration = "5400" if mode == "walking" else "1500"
        return {
            "status": "1",
            "route": {
                "paths": [
                    {
                        "distance": distance,
                        "duration": duration,
                        "steps": [
                            {
                                "instruction": mode,
                                "distance": distance,
                                "duration": duration,
                                "polyline": "116.10,39.10;116.20,39.20",
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    route_segments = segments()
    route_segments[1].kind = "meal"

    routes = RouteService(map_provider_key="amap-key").build_routes(
        "plan_route",
        amap_pois(),
        "transit",
        segments=route_segments,
        include_compact_fallbacks=True,
    )

    assert routes[0].mode == "transit"
    assert routes[0].provider_payload["mealRouteSelectionBoundary"] == {
        "preferredMode": "transit",
        "selectedMode": "transit",
        "decisionRole": "route_mode_candidate_ordering_only",
        "providerRouteMatrixRequiredForDetourDecision": True,
        "fixedAbsoluteDetourThresholdApplied": False,
    }


def test_route_service_builds_bicycling_route_candidate(monkeypatch):
    fetched_modes: list[str] = []

    def fake_fetch(_self, _from_poi, _to_poi, mode: str):
        fetched_modes.append(mode)
        path = {
            "distance": "1800",
            "duration": "500" if mode == "bicycling" else "900",
            "steps": [
                {
                    "instruction": "cycle" if mode == "bicycling" else "walk",
                    "distance": "1800",
                    "duration": "500" if mode == "bicycling" else "900",
                    "polyline": "116.10,39.10;116.20,39.20",
                }
            ],
        }
        if mode == "bicycling":
            return {"errcode": 0, "errmsg": "OK", "data": {"paths": [path]}}
        return {"status": "1", "route": {"paths": [path]}}

    monkeypatch.setattr(RouteService, "_fetch_amap_route", fake_fetch)
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes("plan_route", amap_pois(), "bicycling", segments=segments())

    assert set(fetched_modes) == {"bicycling", "walking"}
    assert [route.mode for route in routes] == ["bicycling", "walking"]
    assert [route.is_selected for route in routes] == [True, False]
    assert all(route.polyline for route in routes)
    assert routes[0].duration_seconds == 500
    assert routes[0].mode == "bicycling"


def test_route_service_bicycling_request_path_is_used():
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = amap_pois()[:2]
    endpoint, params = service._amap_request_params(from_poi, to_poi, "bicycling")

    assert endpoint == "/v4/direction/bicycling"
    assert list(params.keys()) == ["origin", "destination"]


def test_route_service_uses_three_concurrent_amap_requests():
    assert ROUTE_MAP_REQUEST_PARALLELISM == 3


def test_route_service_uses_shared_webservice_limiter(monkeypatch):
    acquire_calls: list[str] = []

    class FakeLimiter:
        def acquire(self):
            acquire_calls.append("acquire")

    monkeypatch.setattr("src.services.route_service.AMAP_WEB_SERVICE_RATE_LIMITER", FakeLimiter())
    monkeypatch.setattr(
        "src.services.route_service.urlopen",
        lambda _url, timeout: FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3200","duration":"900","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        ),
    )
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = amap_pois()[:2]

    assert service._fetch_amap_route(from_poi, to_poi, "driving")["status"] == "1"
    assert acquire_calls == ["acquire"]


def test_route_service_waits_on_fourth_webservice_request_per_second(monkeypatch):
    now = 20.0
    sleeps: list[float] = []

    def time_fn() -> float:
        return now

    def sleep_fn(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = SlidingWindowRateLimiter(3, window_seconds=1.0, time_fn=time_fn, sleep_fn=sleep_fn)
    monkeypatch.setattr("src.services.route_service.AMAP_WEB_SERVICE_RATE_LIMITER", limiter)
    monkeypatch.setattr(
        "src.services.route_service.urlopen",
        lambda _url, timeout: FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3200","duration":"900","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        ),
    )
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = amap_pois()[:2]

    for index in range(4):
        next_to_poi = POI(
            id=f"poi_to_{index}",
            name=f"终点{index}",
            city="北京",
            category="scenic",
            latitude=to_poi.latitude + index * 0.001,
            longitude=to_poi.longitude + index * 0.001,
            source=AMAP_PLACE_SOURCE,
            amap_id=f"amap_to_{index}",
        )
        service._fetch_amap_route(from_poi, next_to_poi, "driving")

    assert sleeps == [1.0]


def test_route_service_semaphore_limits_concurrent_amap_requests(monkeypatch):
    active_requests = 0
    max_active_requests = 0
    lock = Lock()
    three_requests_started = Event()
    release_requests = Event()

    def fake_urlopen(_url: str, timeout: float):
        nonlocal active_requests, max_active_requests
        with lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            if active_requests == ROUTE_MAP_REQUEST_PARALLELISM:
                three_requests_started.set()
        release_requests.wait(timeout=2)
        with lock:
            active_requests -= 1
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3200","duration":"900","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = amap_pois()[:2]

    with ThreadPoolExecutor(max_workers=ROUTE_MAP_REQUEST_PARALLELISM + 2) as executor:
        futures = [
            executor.submit(service._fetch_amap_route, from_poi, to_poi, f"unknown_{index}")
            for index in range(ROUTE_MAP_REQUEST_PARALLELISM + 2)
        ]
        assert three_requests_started.wait(timeout=2)
        assert max_active_requests == ROUTE_MAP_REQUEST_PARALLELISM
        release_requests.set()
        assert [future.result(timeout=2)["status"] for future in futures] == ["1"] * (ROUTE_MAP_REQUEST_PARALLELISM + 2)
        assert max_active_requests == ROUTE_MAP_REQUEST_PARALLELISM


def test_route_service_parses_amap_v4_bicycling_payload():
    service = RouteService(map_provider_key="amap-key")
    payload = {
        "errcode": 0,
        "errmsg": "OK",
        "data": {
            "paths": [
                {
                    "distance": "2600",
                    "duration": "780",
                    "steps": [
                        {
                            "instruction": "沿东长安街骑行",
                            "road": "东长安街",
                            "distance": "2600",
                            "duration": "780",
                            "polyline": "116.10,39.10;116.18,39.18;116.20,39.20",
                        }
                    ],
                }
            ]
        },
    }

    distance, duration, cost_amount, polyline, steps = service._parse_amap_route(payload, "bicycling")

    assert distance == 2600
    assert duration == 780
    assert cost_amount == 0.0
    assert polyline == [[116.10, 39.10], [116.18, 39.18], [116.20, 39.20]]
    assert steps[0]["mode"] == "bicycling"


def test_route_service_unknown_transport_uses_transit_plus_single_fallback():
    service = RouteService(map_provider_key="amap-key")

    assert service._candidate_modes("公共交通/步行待确认") == ["transit", "walking"]


def test_route_service_reuses_short_ttl_cache_for_successful_routes(monkeypatch):
    RouteService.clear_cache()
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        if "bicycling" in url:
            return FakeAmapResponse(
                b'{"errcode":0,"errmsg":"OK","data":{"paths":[{"distance":"2600","duration":"1000","steps":[{"instruction":"cycle","distance":"2600","duration":"1000","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
            )
        return FakeAmapResponse(
            b'{"status":"1","route":{"taxi_cost":"28","paths":[{"distance":"3200","duration":"900","tolls":"0","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")

    first = service.build_routes(
        "plan_route_cache", amap_pois(), "taxi", segments=segments(), preferred_mode_only=True
    )
    second = service.build_routes(
        "plan_route_cache", amap_pois(), "taxi", segments=segments(), preferred_mode_only=True
    )

    assert first[0].polyline == second[0].polyline
    assert len(fetched_urls) == 1
    assert sum("/v3/direction/driving" in url for url in fetched_urls) == 1
    assert sum("/v4/direction/bicycling" in url for url in fetched_urls) == 0
    assert sum("/v3/direction/walking" in url for url in fetched_urls) == 0
    RouteService.clear_cache()


def test_creative_route_preflight_cannot_use_an_unauthorized_cached_pair(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"taxi_cost":"28","paths":[{"distance":"3200","duration":"900","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = canonical_budget_pois()

    service._fetch_amap_route(from_poi, to_poi, "taxi")
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    with amap_call_budget_scope(budget):
        with pytest.raises(RouteProviderError):
            service._fetch_amap_route(from_poi, to_poi, "taxi")

    snapshot = budget.snapshot()
    assert len(fetched_urls) == 1
    assert snapshot["usedRoute"] == 0
    assert snapshot["usedTotalExternal"] == 0
    assert snapshot["cacheHitCount"] == 0
    assert snapshot["skippedBecauseBudget"] == 0
    assert budget.last_denial_reason == "route_work_unauthorized"


def test_creative_route_preflight_exact_cache_hit_does_not_consume_or_repeat_external_work(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"taxi_cost":"28","paths":[{"distance":"3200","duration":"900","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = canonical_budget_pois()
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    budget.authorize_route_work(
        [
            {
                "fromAmapId": from_poi.amap_id,
                "toAmapId": to_poi.amap_id,
                "mode": "taxi",
                "reason": "baseline_adjacent",
                "condition": "always",
            }
        ]
    )

    with amap_call_budget_scope(budget):
        first = service._fetch_amap_route(from_poi, to_poi, "taxi")
        second = service._fetch_amap_route(from_poi, to_poi, "taxi")

    snapshot = budget.snapshot()
    assert first == second
    assert len(fetched_urls) == 1
    assert snapshot["usedRoute"] == 1
    assert snapshot["usedTotalExternal"] == 1
    assert snapshot["cacheHitCount"] == 1
    assert snapshot["reusedQueryCount"] == 1
    assert snapshot["skipped"] == []


def test_creative_repair_non_cache_provider_call_records_full_exact_scope(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","count":"1","route":{"transits":[{"distance":"3200","duration":"900","cost":"4","segments":[{"walking":{"distance":"0","duration":"0","steps":[]},"bus":{"buslines":[{"name":"subway","distance":"3200","duration":"900","departure_stop":{"name":"from","location":"116.10,39.10"},"arrival_stop":{"name":"to","location":"116.20,39.20"},"polyline":"116.10,39.10;116.20,39.20"}]}}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = canonical_budget_pois()
    route_contract_fingerprint = canonical_fingerprint(
        {"preferredMode": "transit", "status": "ready"}
    )
    exact_scope = {
        "continuationMode": "repair_exact_slot",
        "rootPortfolioId": "portfolio_root_1",
        "planningSelectionRootTurnId": "planning_root_turn_1",
        "briefId": "brief_1",
        "poolId": "pool_1",
        "dayNumber": 1,
        "planningSlotId": "slot_1",
        "candidatePhysicalId": to_poi.amap_id,
        "adjacentAnchorIds": ["segment_anchor_1"],
        "adjacentRouteLedgerKeys": [
            {
                "fromPhysicalId": from_poi.amap_id,
                "toPhysicalId": to_poi.amap_id,
                "mode": "transit",
            }
        ],
        "routeContractFingerprint": route_contract_fingerprint,
    }
    exact_scope["scopeFingerprint"] = canonical_fingerprint(exact_scope)
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    budget.authorize_route_work(
        [
            {
                "fromAmapId": from_poi.amap_id,
                "toAmapId": to_poi.amap_id,
                "mode": "transit",
                "reason": "replacement_adjacent",
                "condition": "always",
                "candidatePhysicalId": to_poi.amap_id,
                "repairScopeCertificate": exact_scope,
            }
        ]
    )

    with amap_call_budget_scope(budget):
        with pytest.raises(RouteProviderError):
            service._fetch_amap_route(from_poi, to_poi, "transit")
        assert budget.used_route == 0
        assert budget.cache_hit_count == 0
        assert fetched_urls == []
        with amap_route_repair_scope(exact_scope):
            service._fetch_amap_route(from_poi, to_poi, "transit")

    assert [
        (call["cacheHit"], call.get("repairScopeCertificate"))
        for call in budget.snapshot()["calls"]
    ] == [(False, exact_scope)]
    assert len(fetched_urls) == 1


def test_creative_repair_cache_hit_requires_and_records_the_current_exact_scope(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","count":"1","route":{"transits":[{"distance":"3200","duration":"900","cost":"4","segments":[{"walking":{"distance":"0","duration":"0","steps":[]},"bus":{"buslines":[{"name":"subway","distance":"3200","duration":"900","departure_stop":{"name":"from","location":"116.10,39.10"},"arrival_stop":{"name":"to","location":"116.20,39.20"},"polyline":"116.10,39.10;116.20,39.20"}]}}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = canonical_budget_pois()
    first_scope = exact_route_repair_scope(
        from_poi,
        to_poi,
        planning_root="planning_root_1",
        planning_slot_id="slot_1",
        adjacent_anchor_id="anchor_segment_1",
    )
    second_scope = exact_route_repair_scope(
        from_poi,
        to_poi,
        planning_root="planning_root_2",
        planning_slot_id="slot_2",
        adjacent_anchor_id="anchor_segment_2",
    )
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    assert budget.authorize_route_work(
        [
            {
                "fromAmapId": from_poi.amap_id,
                "toAmapId": to_poi.amap_id,
                "mode": "transit",
                "reason": "replacement_adjacent",
                "candidatePhysicalId": to_poi.amap_id,
                "repairScopeCertificate": scope,
            }
            for scope in (first_scope, second_scope)
        ]
    )

    with amap_call_budget_scope(budget):
        with amap_route_repair_scope(first_scope):
            first = service._fetch_amap_route(from_poi, to_poi, "transit")
            repeated = service._fetch_amap_route(from_poi, to_poi, "transit")
        with amap_route_repair_scope(second_scope):
            second = service._fetch_amap_route(from_poi, to_poi, "transit")
        tampered_scope = copy.deepcopy(second_scope)
        tampered_scope["planningSlotId"] = "tampered_slot"
        tampered_scope.pop("scopeFingerprint")
        tampered_scope["scopeFingerprint"] = canonical_fingerprint(tampered_scope)
        with amap_route_repair_scope(tampered_scope):
            with pytest.raises(RouteProviderError):
                service._fetch_amap_route(from_poi, to_poi, "transit")

    snapshot = budget.snapshot()
    assert first == repeated == second
    assert len(fetched_urls) == 2
    assert snapshot["usedRoute"] == 2
    assert snapshot["cacheHitCount"] == 1
    assert [
        (call["cacheHit"], call["repairScopeCertificate"])
        for call in snapshot["calls"]
    ] == [(False, first_scope), (True, first_scope), (False, second_scope)]


def test_creative_route_preflight_cache_is_bound_to_canonical_pair_not_coordinates(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"taxi_cost":"28","paths":[{"distance":"3200","duration":"900","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    from_poi, to_poi = canonical_budget_pois()
    same_coordinates_other_identity = replace(
        to_poi,
        id="poi_3",
        amap_id="B000000003",
    )
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    budget.authorize_route_work(
        [
            {
                "fromAmapId": from_poi.amap_id,
                "toAmapId": destination.amap_id,
                "mode": "taxi",
                "reason": "baseline_adjacent",
                "condition": "always",
            }
            for destination in (to_poi, same_coordinates_other_identity)
        ]
    )

    with amap_call_budget_scope(budget):
        service._fetch_amap_route(from_poi, to_poi, "taxi")
        service._fetch_amap_route(from_poi, same_coordinates_other_identity, "taxi")

    snapshot = budget.snapshot()
    assert len(fetched_urls) == 2
    assert snapshot["usedRoute"] == 2
    assert snapshot["cacheHitCount"] == 0


def test_route_service_records_amap_budget_across_thread_pool(monkeypatch):
    RouteService.clear_cache()
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        if "bicycling" in url:
            return FakeAmapResponse(
                b'{"errcode":0,"errmsg":"OK","data":{"paths":[{"distance":"2600","duration":"1000","steps":[{"instruction":"cycle","distance":"2600","duration":"1000","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
            )
        if "walking" in url:
            return FakeAmapResponse(
                b'{"status":"1","route":{"paths":[{"distance":"1800","duration":"1500","steps":[{"instruction":"walk","distance":"1800","duration":"1500","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
            )
        return FakeAmapResponse(
            b'{"status":"1","route":{"taxi_cost":"28","paths":[{"distance":"3200","duration":"900","tolls":"0","steps":[{"instruction":"drive","distance":"3200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    budget = AmapCallBudget(route_refresh_max=1, total_external_max=1, source="test_route_budget")

    with amap_call_budget_scope(budget):
        routes = service.build_routes(
            "plan_route_budget", amap_pois(), "taxi", segments=segments(), preferred_mode_only=True
        )

    assert routes
    assert len(fetched_urls) == 1
    assert budget.used_route == 1
    assert budget.skipped_because_budget == 0
    assert budget.snapshot()["calls"][0]["endpoint"] == "route/taxi"
    assert budget.snapshot()["skipped"] == []
    RouteService.clear_cache()


def test_route_service_reuses_short_ttl_cache_for_provider_failures(monkeypatch):
    RouteService.clear_cache()
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(b'{"status":"0","info":"INVALID_USER_KEY"}')

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")

    first = service.build_routes(
        "plan_route_failure_cache",
        amap_pois(),
        "taxi",
        segments=segments(),
        preferred_mode_only=True,
    )
    second = service.build_routes(
        "plan_route_failure_cache",
        amap_pois(),
        "taxi",
        segments=segments(),
        preferred_mode_only=True,
    )

    assert first == []
    assert second == []
    assert len(fetched_urls) == 1
    assert service.warnings.count("AMap taxi route failed for 故宫博物院 -> 天坛公园: INVALID_USER_KEY") == 1
    RouteService.clear_cache()


def test_route_service_parses_amap_transit_fare_when_returned():
    service = RouteService(map_provider_key="amap-key")
    payload = {
        "route": {
            "transits": [
                {
                    "distance": "2100",
                    "duration": "1200",
                    "cost": "4.5",
                    "segments": [
                        {
                            "bus": {
                                "buslines": [
                                    {
                                        "name": "metro",
                                        "distance": "2100",
                                        "duration": "1200",
                                        "polyline": "116.10,39.10;116.20,39.20",
                                    }
                                ]
                            }
                        }
                    ],
                }
            ]
        }
    }

    _distance, _duration, cost_amount, _polyline, _steps = service._parse_amap_route(payload, "transit")

    assert cost_amount == 4.5


def test_route_service_keeps_zero_transit_fare_when_amap_omits_cost():
    service = RouteService(map_provider_key="amap-key")
    payload = {
        "route": {
            "transits": [
                {
                    "distance": "2100",
                    "duration": "1200",
                    "segments": [
                        {
                            "bus": {
                                "buslines": [
                                    {
                                        "name": "metro",
                                        "distance": "2100",
                                        "duration": "1200",
                                        "polyline": "116.10,39.10;116.20,39.20",
                                    }
                                ]
                            }
                        }
                    ],
                }
            ]
        }
    }

    _distance, _duration, cost_amount, _polyline, _steps = service._parse_amap_route(payload, "transit")

    assert cost_amount == 0.0


def test_route_service_skips_failed_mode_without_fake_route(monkeypatch):
    def fake_urlopen(url: str, timeout: float):
        if "walking" in url:
            return FakeAmapResponse(b'{"status":"0","info":"INVALID_PARAMS"}')
        if "bicycling" in url:
            return FakeAmapResponse(
                b'{"errcode":0,"errmsg":"OK","data":{"paths":[{"distance":"2200","duration":"900","steps":[{"instruction":"cycle","distance":"2200","duration":"900","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
            )
        if "transit" in url:
            return FakeAmapResponse(
                b'{"status":"1","route":{"transits":[{"distance":"2100","duration":"1200","cost":"4","segments":[{"bus":{"buslines":[{"name":"metro","distance":"2100","duration":"1200","polyline":"116.10,39.10;116.20,39.20"}]}}]}]}}'
            )
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3000","duration":"800","steps":[{"instruction":"drive","distance":"3000","duration":"800","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes("plan_route", amap_pois(), "walking", segments=segments())

    assert "walking" not in {route.mode for route in routes}
    assert routes[0].is_selected is True
    assert routes[0].mode == "bicycling"
    assert any("walking route failed" in warning for warning in service.warnings)


def test_route_service_does_not_route_unconfirmed_pois():
    service = RouteService(map_provider_key="amap-key")
    pois = amap_pois()
    pois[1].amap_id = None

    routes = service.build_routes("plan_route", pois, "walking", segments=segments())

    assert routes == []
    assert "route_skipped_waiting_for_poi_grounding" in service.warnings[0]


def test_route_service_excludes_pending_meal_from_route_pairs(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3000","duration":"800","steps":[{"instruction":"walk","distance":"3000","duration":"800","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    pois = [
        amap_pois()[0],
        POI(
            id="poi_meal_pending",
            amap_id=None,
            name="午餐 当地特色美食（待选择顺路餐馆）",
            city="北京",
            category="food",
            latitude=None,
            longitude=None,
            source="agent-text-timeline",
            confidence=0.35,
            source_note="pendingMeal=true；groundingStatus：waiting_for_poi_grounding；routeAnchor=false；needsConcretePoi=true",
        ),
        amap_pois()[1],
    ]
    route_segments = [
        segments()[0],
        ItinerarySegment(
            id="seg_meal_pending",
            day_id="day_1",
            segment_order=2,
            kind="meal",
            start_time="12:00",
            end_time="13:00",
            poi_id="poi_meal_pending",
            transport_mode="walking",
            estimated_cost=0,
            notes="pendingMeal=true；groundingStatus：waiting_for_poi_grounding；routeAnchor=false；needsConcretePoi=true",
        ),
        ItinerarySegment(
            id="seg_3",
            day_id="day_1",
            segment_order=3,
            kind="activity",
            start_time="14:00",
            end_time="16:00",
            poi_id="poi_2",
            transport_mode="walking",
            estimated_cost=0,
            notes="",
        ),
    ]

    routes = service.build_routes("plan_route_pending_meal", pois, "walking", segments=route_segments)

    assert routes
    assert {(route.from_segment_id, route.to_segment_id) for route in routes} == {("seg_1", "seg_3")}
    assert all("seg_meal_pending" not in {route.from_segment_id, route.to_segment_id} for route in routes)
    assert fetched_urls


def test_route_service_excludes_mock_meal_from_route_pairs(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3000","duration":"800","steps":[{"instruction":"walk","distance":"3000","duration":"800","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    pois = [
        amap_pois()[0],
        POI(
            id="poi_meal_mock",
            amap_id="mock_amap_food_1",
            name="北京本地菜餐厅",
            city="北京",
            category="food",
            latitude=39.91,
            longitude=116.39,
            source=AMAP_PLACE_SOURCE,
            confidence=0.92,
            source_note="CLI eval mock AMap candidate; only used when mockMapProvider is enabled. groundingStatus：agent_selected_candidate",
        ),
        amap_pois()[1],
    ]
    route_segments = [
        segments()[0],
        ItinerarySegment(
            id="seg_meal_mock",
            day_id="day_1",
            segment_order=2,
            kind="meal",
            start_time="12:00",
            end_time="13:00",
            poi_id="poi_meal_mock",
            transport_mode="walking",
            estimated_cost=0,
            notes="groundingStatus：agent_selected_candidate；routeAnchor=true",
        ),
        ItinerarySegment(
            id="seg_3",
            day_id="day_1",
            segment_order=3,
            kind="activity",
            start_time="14:00",
            end_time="16:00",
            poi_id="poi_2",
            transport_mode="walking",
            estimated_cost=0,
            notes="",
        ),
    ]

    routes = service.build_routes("plan_route_mock_meal", pois, "walking", segments=route_segments)

    assert routes
    assert {(route.from_segment_id, route.to_segment_id) for route in routes} == {("seg_1", "seg_3")}
    assert all("seg_meal_mock" not in {route.from_segment_id, route.to_segment_id} for route in routes)
    assert fetched_urls


def test_route_service_routes_agent_text_timeline_anchor_with_warning(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3000","duration":"800","steps":[{"instruction":"drive","distance":"3000","duration":"800","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    pois = amap_pois()
    pois[1].source = "agent-text-timeline"
    pois[1].confidence = 0.4
    pois[1].source_note = "高德 POI 待校验"

    routes = service.build_routes("plan_route", pois, "taxi", segments=segments())

    assert routes
    assert routes[0].polyline == [[116.10, 39.10], [116.20, 39.20]]
    assert any("uses pending Agent POI anchor" in warning for warning in service.warnings)
    assert any("/v3/direction/driving" in url for url in fetched_urls)


def test_route_service_does_not_route_waiting_route_anchor_even_with_fallback_anchor(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3000","duration":"800","steps":[{"instruction":"drive","distance":"3000","duration":"800","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    pois = amap_pois()
    pois[1].source = "agent-text-timeline"
    pois[1].confidence = 0.4
    pois[1].source_note = "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。"
    route_segments = segments()
    route_segments[1].notes = (
        "groundingRequiredReason=route_anchor_pending; retryReason=route_detour_penalty; "
        "groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true"
    )

    routes = service.build_routes("plan_route", pois, "taxi", segments=route_segments)

    assert routes == []
    assert fetched_urls == []


def test_route_service_does_not_route_agent_text_timeline_without_anchor():
    service = RouteService(map_provider_key="amap-key")
    pois = amap_pois()
    pois[1].source = "agent-text-timeline"
    pois[1].amap_id = None
    pois[1].latitude = None
    pois[1].longitude = None

    routes = service.build_routes("plan_route", pois, "taxi", segments=segments())

    assert routes == []
    assert "route_skipped_waiting_for_poi_grounding" in service.warnings[0]


def test_route_service_skips_meal_segments_when_grouping_routes(monkeypatch):
    fetched_urls = []

    def fake_urlopen(url: str, timeout: float):
        fetched_urls.append(url)
        return FakeAmapResponse(
            b'{"status":"1","route":{"paths":[{"distance":"3000","duration":"800","steps":[{"instruction":"drive","distance":"3000","duration":"800","polyline":"116.10,39.10;116.20,39.20"}]}]}}'
        )

    monkeypatch.setattr("src.services.route_service.urlopen", fake_urlopen)
    service = RouteService(map_provider_key="amap-key")
    pois = [
        *amap_pois(),
        POI(
            id="poi_meal",
            amap_id=None,
            name="午餐",
            city="北京",
            category="food",
            latitude=None,
            longitude=None,
            source="agent-text-timeline",
            confidence=0.4,
        ),
    ]
    route_segments = segments()
    route_segments.insert(
        1,
        ItinerarySegment(
            id="seg_meal",
            day_id="day_1",
            segment_order=2,
            kind="meal",
            start_time="11:30",
            end_time="12:30",
            poi_id="poi_meal",
            transport_mode="walking",
            estimated_cost=60,
            notes="午餐不作为 AMap route endpoint。",
        ),
    )
    route_segments[2].segment_order = 3

    routes = service.build_routes("plan_route_meal", pois, "taxi", segments=route_segments)

    assert routes
    assert all(route.from_segment_id == "seg_1" and route.to_segment_id == "seg_2" for route in routes)
    assert not any(route.from_poi_id == "poi_meal" or route.to_poi_id == "poi_meal" for route in routes)
    assert fetched_urls


def test_route_service_includes_concrete_meal_segments_when_grouping_routes():
    service = RouteService(map_provider_key="amap-key")
    pois = [
        amap_pois()[0],
        POI(
            id="poi_meal",
            amap_id="B000MEAL",
            name="麦当劳(清华大学店)",
            city="北京",
            category="food",
            latitude=39.995,
            longitude=116.332,
            source=AMAP_PLACE_SOURCE,
            confidence=0.91,
            source_note="高德 WebService POI 搜索，限定当前城市，extensions=all",
        ),
        amap_pois()[1],
    ]
    route_segments = segments()
    route_segments.insert(
        1,
        ItinerarySegment(
            id="seg_meal",
            day_id="day_1",
            segment_order=2,
            kind="meal",
            start_time="12:00",
            end_time="13:00",
            poi_id="poi_meal",
            transport_mode="walking",
            estimated_cost=60,
            notes="已替换为具体餐厅，应参与上下路线。",
        ),
    )
    route_segments[2].segment_order = 3

    groups = service._route_groups(pois, route_segments)

    assert [(from_segment.id, to_segment.id) for from_segment, to_segment, _from_poi, _to_poi in groups] == [
        ("seg_1", "seg_meal"),
        ("seg_meal", "seg_2"),
    ]


def test_unresolved_poi_default_source_is_not_mock_map_provider():
    poi = POI(
        id="poi_unresolved",
        name="待确认地点",
        city="北京",
        category="candidate",
        latitude=39.9,
        longitude=116.4,
    )

    assert poi.source == "unresolved-map-poi"


def test_route_service_skips_unconfirmed_endpoint_without_fake_zero_route():
    pois = [
        amap_pois()[0],
        POI(
            id="poi_unresolved",
            name="待确认地点",
            city="北京",
            category="candidate",
            latitude=None,
            longitude=None,
            source="agent-text-timeline",
            confidence=0.35,
        ),
    ]
    route_segments = segments()
    route_segments[1].poi_id = "poi_unresolved"
    service = RouteService(map_provider_key="amap-key")

    routes = service.build_routes("plan_route_unresolved", pois, "walking", segments=route_segments)

    assert routes == []
    assert any("route_skipped_waiting_for_poi_grounding" in warning for warning in service.warnings)


def amap_pois() -> list[POI]:
    return [
        POI(
            id="poi_1",
            amap_id="B0001",
            name="故宫博物院",
            city="北京",
            category="scenic",
            latitude=39.918,
            longitude=116.397,
            source=AMAP_PLACE_SOURCE,
            confidence=0.9,
        ),
        POI(
            id="poi_2",
            amap_id="B0002",
            name="天坛公园",
            city="北京",
            category="scenic",
            latitude=39.882,
            longitude=116.406,
            source=AMAP_PLACE_SOURCE,
            confidence=0.9,
        ),
    ]


def canonical_budget_pois() -> list[POI]:
    pois = amap_pois()
    pois[0].amap_id = "B000000001"
    pois[1].amap_id = "B000000002"
    return pois


def exact_route_repair_scope(
    from_poi: POI,
    to_poi: POI,
    *,
    planning_root: str,
    planning_slot_id: str,
    adjacent_anchor_id: str,
) -> dict:
    scope = {
        "continuationMode": "repair_exact_slot",
        "rootPortfolioId": "portfolio_root_1",
        "planningSelectionRootTurnId": planning_root,
        "briefId": "brief_1",
        "poolId": "pool_1",
        "dayNumber": 1,
        "planningSlotId": planning_slot_id,
        "candidatePhysicalId": to_poi.amap_id,
        "adjacentAnchorIds": [adjacent_anchor_id],
        "adjacentRouteLedgerKeys": [
            {
                "fromPhysicalId": from_poi.amap_id,
                "toPhysicalId": to_poi.amap_id,
                "mode": "transit",
            }
        ],
        "routeContractFingerprint": canonical_fingerprint(
            {"preferredMode": "transit", "status": "ready"}
        ),
    }
    scope["scopeFingerprint"] = canonical_fingerprint(scope)
    return scope


def _recorded_poi(item) -> POI:
    return POI(
        id=item.id,
        amap_id=item.id,
        name=item.name,
        city=item.city,
        category=item.category,
        latitude=item.latitude,
        longitude=item.longitude,
        source=item.source,
        confidence=item.confidence,
        type=item.type,
        district=item.district,
        address=item.address,
    )


def segments() -> list[ItinerarySegment]:
    return [
        ItinerarySegment(
            id="seg_1",
            day_id="day_1",
            segment_order=1,
            kind="activity",
            start_time="09:00",
            end_time="11:00",
            poi_id="poi_1",
            transport_mode="walking",
            estimated_cost=0,
            notes="",
        ),
        ItinerarySegment(
            id="seg_2",
            day_id="day_1",
            segment_order=2,
            kind="activity",
            start_time="14:00",
            end_time="16:00",
            poi_id="poi_2",
            transport_mode="walking",
            estimated_cost=0,
            notes="",
        ),
    ]

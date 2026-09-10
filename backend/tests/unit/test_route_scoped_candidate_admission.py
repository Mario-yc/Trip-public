from __future__ import annotations

from src.api.schemas.maps import MapPoiResponse
from src.models.poi_intent import DaySlot, IntentPool, PoiIntent
from src.models.poi_search_profile import ExperienceSemanticInput
from src.services.agent_service import AgentService
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
)
from src.services.itinerary_service import ItineraryService
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.route_insertion_scorer import RouteInsertionScorer


def _route_profile():
    route_contract = RouteInsertionScorer.build_route_decision_contract(
        source="route_scope_consumer_test",
        provenance={"transportMode": "transit", "contractVersion": 3},
        detour_tolerance={
            "maxGeneralizedCostDelta": 30.0,
            "maxDetourRatio": 0.3,
        },
        mobility_profile={
            "source": "route_scope_consumer_test",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert route_contract is not None
    return ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-night",
            briefId="brief-night",
            planningSlotId="slot-night",
            requirementLevel="required",
            rawNeed="户外公共夜景",
            intentType="night_view",
            candidateHints=["滨水公共夜景"],
            hintPolicy="controller_experience_spec",
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
            experienceGoal="户外公共夜景",
            routeContext={
                "routeDecisionContract": route_contract,
                "clarificationContractVersion": 3,
            },
            evidencePolicy={"openingEvidence": "outdoor_public_policy"},
            groundingPolicy={"accessPolicy": "public_outdoor"},
        )
    )


def _candidate(amap_id: str) -> MapPoiResponse:
    return MapPoiResponse(
        id=amap_id,
        name="滨水公共夜景步道",
        type="风景名胜;水域景观;滨水步道",
        city="北京",
        district="朝阳区",
        address="亮马河沿岸",
        longitude=116.471,
        latitude=39.952,
        category="scenic",
        source=AMAP_PLACE_SOURCE,
        sourceNote="来源：高德地图",
        providerTypeCode="110102",
        confidence=0.94,
        photos=[],
    )


def _optional_market_profile():
    route_contract = RouteInsertionScorer.build_route_decision_contract(
        source="route_scope_optional_consumer_test",
        provenance={"transportMode": "transit", "contractVersion": 3},
        detour_tolerance={
            "maxGeneralizedCostDelta": 30.0,
            "maxDetourRatio": 0.3,
        },
        mobility_profile={
            "source": "route_scope_optional_consumer_test",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert route_contract is not None
    return ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-market",
            briefId="brief-market",
            planningSlotId="slot-market",
            dayNumber=1,
            requirementLevel="optional",
            rawNeed="传统市场漫步",
            intentType="area_walk",
            optionalExperienceFamily="market_walk",
            candidateHints=["传统市场"],
            hintPolicy="server_optional_experience",
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
            experienceShape="area",
            experienceGoal="市井市场与传统市集",
            evidencePolicy={"minimumIndependentClaims": 1},
            groundingPolicy={"consumerRecheckRequired": True},
            routeContext={
                "routeDecisionContract": route_contract,
                "clarificationContractVersion": 3,
            },
        )
    )


def _optional_market_candidate(profile):
    route_contract = RouteInsertionScorer.normalized_route_decision_contract(
        profile.routeContext.get("routeDecisionContract")
    )
    assert route_contract is not None
    scope_material = {
        "city": "北京",
        "intentType": "area_walk",
        "experienceFamily": profile.experienceFamily,
        "dayNumber": 1,
        "occurrenceId": "slot-market",
        "routeCorridorHash": "b" * 64,
        "transportMode": "transit",
        "evidenceRequirementFingerprint": (
            ItineraryService._route_scope_fingerprint(
                {
                    "evidencePolicy": dict(profile.evidencePolicy),
                    "groundingPolicy": dict(profile.groundingPolicy),
                    "routeContractFingerprint": route_contract["fingerprint"],
                }
            )
        ),
        "contractVersion": "3",
        "anchorPolicy": "previous_only",
    }
    scope = {
        **scope_material,
        "queryFingerprint": ItineraryService._route_scope_fingerprint(scope_material),
    }
    candidate = MapPoiResponse(
        id="B0MARKET1",
        name="社区传统市场",
        type="购物服务;综合市场;农贸市场",
        city="北京",
        district="朝阳区",
        address="测试市场地址",
        longitude=116.471,
        latitude=39.952,
        category="market",
        source=AMAP_PLACE_SOURCE,
        sourceNote="deterministic canonical-shape contract candidate",
        providerTypeCode="060703",
        confidence=0.94,
        tags=["农贸市场", "社区商业", "传统市集"],
        sourceClaims=[
            {
                "claimKey": "resident_market",
                "stance": "support",
                "sourceName": "deterministic-contract-source",
                "sourceUrlHash": "c" * 64,
                "freshness": "2026-08-16T00:00:00+00:00",
            }
        ],
        photos=[],
    )
    for name, value in (
        ("_trip_route_query_scope", scope),
        ("_trip_route_query_scope_fingerprint", scope["queryFingerprint"]),
        ("_trip_route_query_scope_verified", True),
        ("_trip_consumer_admission_pending", True),
        ("_trip_search_profile_fingerprint", profile.profileFingerprint),
        ("_trip_source_brief_id", profile.briefId),
        ("_trip_source_pool_id", profile.poolId),
        ("_trip_source_planning_slot_id", profile.planningSlotId),
    ):
        object.__setattr__(candidate, name, value)
    return candidate


def _route_scoped_candidate(profile):
    route_contract = RouteInsertionScorer.normalized_route_decision_contract(
        profile.routeContext.get("routeDecisionContract")
    )
    assert route_contract is not None
    scope_material = {
        "city": "北京",
        "intentType": "night_view",
        "experienceFamily": profile.experienceFamily,
        "dayNumber": 1,
        "occurrenceId": "slot-night",
        "routeCorridorHash": "a" * 64,
        "transportMode": "transit",
        "evidenceRequirementFingerprint": (
            ItineraryService._route_scope_fingerprint(
                {
                    "evidencePolicy": dict(profile.evidencePolicy),
                    "groundingPolicy": dict(profile.groundingPolicy),
                    "routeContractFingerprint": route_contract["fingerprint"],
                }
            )
        ),
        "contractVersion": "3",
        "anchorPolicy": "previous_only",
    }
    scope = {
        **scope_material,
        "queryFingerprint": ItineraryService._route_scope_fingerprint(scope_material),
    }
    candidate = _candidate("B0ROUTE01")
    for name, value in (
        ("_trip_route_query_scope", scope),
        ("_trip_route_query_scope_fingerprint", scope["queryFingerprint"]),
        ("_trip_route_query_scope_verified", True),
        ("_trip_consumer_admission_pending", True),
        ("_trip_search_profile_fingerprint", profile.profileFingerprint),
        ("_trip_source_brief_id", profile.briefId),
        ("_trip_source_pool_id", profile.poolId),
        ("_trip_source_planning_slot_id", profile.planningSlotId),
    ):
        object.__setattr__(candidate, name, value)
    return candidate


def test_formal_occurrence_missing_experience_policy_rejects_all_candidates():
    profile = _route_profile()
    intent = PoiIntent(
        raw_need="户外公共夜景",
        city="北京",
        day_number=1,
        time_window="19:00-22:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        preferred_types=["滨水空间", "公共夜景"],
        rejected_types=["餐饮", "售票处"],
        candidate_hints=["滨水公共夜景"],
        target_count=1,
        search_profile=profile,
    )
    pool = IntentPool(
        pool_id="pool-night",
        brief_id="brief-night",
        raw_need="户外公共夜景",
        city="北京",
        intent_type="night_view",
        target_count=1,
        requirement_level="required",
        goal_id="night",
        assign_to_slots=["slot-night"],
    )
    slot = DaySlot(
        slot_id="slot-night",
        day_number=1,
        date="2026-10-01",
        time_window="night",
        start_time="20:00",
        duration_minutes=75,
        kind="night_view",
        raw_need="户外公共夜景",
        route_anchor=True,
    )
    scoped = _route_scoped_candidate(profile)
    ordinary = _candidate("B0NORMAL1")

    admitted, reports = AgentService.__new__(AgentService)._route_scoped_consumer_admitted_candidates(
        [scoped, ordinary],
        intent=intent,
        pool=pool,
        slot=slot,
        pipeline_context={
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "night",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    }
                ]
            }
        },
    )

    assert admitted == []
    assert len(reports) == 2
    assert all(report["scoreEligible"] is False for report in reports)
    assert all(report["classification"] == "rejected" for report in reports)
    assert all(
        report["reasonCodes"] == ["consumer_experience_policy_missing"]
        for report in reports
    )


def test_formal_occurrence_unresolved_experience_policy_rejects_ordinary_candidate():
    profile = _route_profile()
    intent = PoiIntent(
        raw_need="户外公共夜景",
        city="北京",
        day_number=1,
        time_window="19:00-22:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        preferred_types=["滨水空间", "公共夜景"],
        rejected_types=["餐饮", "售票处"],
        candidate_hints=["滨水公共夜景"],
        target_count=1,
        search_profile=profile,
    )
    pool = IntentPool(
        pool_id="pool-night",
        brief_id="brief-night",
        raw_need="户外公共夜景",
        city="北京",
        intent_type="night_view",
        target_count=1,
        requirement_level="required",
        goal_id="night",
        assign_to_slots=["slot-night"],
    )
    slot = DaySlot(
        slot_id="slot-night",
        day_number=1,
        date="2026-10-01",
        time_window="night",
        start_time="20:00",
        duration_minutes=75,
        kind="night_view",
        raw_need="户外公共夜景",
        route_anchor=True,
    )

    admitted, reports = AgentService.__new__(AgentService)._route_scoped_consumer_admitted_candidates(
        [_candidate("B0NORMAL1")],
        intent=intent,
        pool=pool,
        slot=slot,
        pipeline_context={
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "night", "intentType": "night_view"}],
                "experienceSpecs": [
                    {
                        "intentType": "night_view",
                        "allowedDayNumbers": [1],
                        "experienceFamilies": ["public_city_view"],
                        "unresolvedDimensions": ["accessPolicy"],
                    }
                ],
            }
        },
    )

    assert admitted == []
    assert reports[0]["reasonCodes"] == ["consumer_experience_policy_unresolved"]


def test_formal_occurrence_material_policy_without_semantic_scope_rejects_ordinary_candidate():
    profile = _route_profile()
    intent = PoiIntent(
        raw_need="户外公共夜景",
        city="北京",
        day_number=1,
        time_window="19:00-22:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        preferred_types=["滨水空间", "公共夜景"],
        rejected_types=["餐饮", "售票处"],
        candidate_hints=["滨水公共夜景"],
        target_count=1,
        search_profile=profile,
    )
    pool = IntentPool(
        pool_id="pool-night",
        brief_id="brief-night",
        raw_need="户外公共夜景",
        city="北京",
        intent_type="night_view",
        target_count=1,
        requirement_level="required",
        goal_id="night",
        assign_to_slots=["slot-night"],
    )
    slot = DaySlot(
        slot_id="slot-night",
        day_number=1,
        date="2026-10-01",
        time_window="night",
        start_time="20:00",
        duration_minutes=75,
        kind="night_view",
        raw_need="户外公共夜景",
        route_anchor=True,
    )

    admitted, reports = AgentService.__new__(AgentService)._route_scoped_consumer_admitted_candidates(
        [_candidate("B0NORMAL1")],
        intent=intent,
        pool=pool,
        slot=slot,
        pipeline_context={
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "night", "intentType": "night_view"}],
                "experienceSpecs": [
                    {
                        "intentType": "night_view",
                        "accessPolicy": "public_outdoor",
                        "evidenceFreshness": {"maxAgeHours": 24},
                        "unresolvedDimensions": [],
                    }
                ],
            }
        },
    )

    assert admitted == []
    assert reports[0]["reasonCodes"] == ["consumer_experience_policy_incomplete"]


def test_route_scoped_optional_creative_slot_uses_its_server_sealed_profile_without_borrowing_goal_spec():
    profile = _optional_market_profile()
    intent = PoiIntent(
        raw_need="传统市场漫步",
        city="北京",
        day_number=1,
        time_window="afternoon",
        intent_type="area_walk",
        specificity="area",
        search_queries=list(profile.keywordVariants),
        preferred_types=["综合市场", "农贸市场"],
        rejected_types=["停车场", "住宅"],
        candidate_hints=["传统市场"],
        target_count=1,
        optional_experience_family="market_walk",
        search_profile=profile,
    )
    pool = IntentPool(
        pool_id="pool-market",
        brief_id="brief-market",
        raw_need="传统市场漫步",
        city="北京",
        intent_type="area_walk",
        target_count=1,
        requirement_level="optional",
        optional_experience_family="market_walk",
        assign_to_slots=["slot-market"],
    )
    slot = DaySlot(
        slot_id="slot-market",
        day_number=1,
        date="2026-10-01",
        time_window="afternoon",
        start_time="15:00",
        duration_minutes=75,
        kind="area_walk",
        raw_need="传统市场漫步",
        route_anchor=True,
        experience_shape="area",
        experience_goal="市井市场与传统市集",
        evidence_policy={"minimumIndependentClaims": 1},
        grounding_policy={"consumerRecheckRequired": True},
    )
    scoped = _optional_market_candidate(profile)

    admitted, reports = AgentService.__new__(AgentService)._route_scoped_consumer_admitted_candidates(
        [scoped],
        intent=intent,
        pool=pool,
        slot=slot,
        pipeline_context={
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "night",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    }
                ]
            }
        },
    )

    assert admitted == [scoped]
    assert len(reports) == 1
    assert reports[0]["scoreEligible"] is True
    assert reports[0]["classification"] == "admitted_anchor_set_member"
    assert reports[0].get("consumerAdmissionPrecondition") is None
    assert reports[0]["consumerScope"]["briefId"] == "brief-market"
    assert reports[0]["consumerScope"]["poolId"] == "pool-market"
    assert reports[0]["consumerScope"]["planningSlotId"] == "slot-market"
    assert reports[0]["consumerScope"]["dayNumber"] == 1
    assert "experienceSpecPolicy" not in reports[0]["consumerScope"]

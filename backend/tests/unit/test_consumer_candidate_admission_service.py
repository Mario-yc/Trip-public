from datetime import datetime, timedelta, timezone

import pytest

from src.services.candidate_provider_evidence_service import (
    CandidateProviderEvidenceService,
)
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService


def _candidate(**overrides):
    value = {
        "id": "B000000001",
        "amapId": "B000000001",
        "name": "朝阳社区农贸市场",
        "type": "购物服务;综合市场;农贸市场",
        "category": "农贸市场",
        "providerTypeCode": "060703",
        "tags": ["农贸市场", "社区商业"],
        "businessArea": "朝阳门",
        "city": "北京",
        "longitude": 116.42,
        "latitude": 39.92,
        "source": "amap-place-search",
        "sourceClaims": [
            {
                "claimType": "local_life_context",
                "stance": "support",
                "summary": "面向附近居民的日常采购市场",
                "sourceUrlHash": "a" * 64,
            }
        ],
    }
    value.update(overrides)
    return value


def _consumer(**overrides):
    value = {
        "briefId": "brief_local",
        "poolId": "pool_local",
        "slotId": "slot_local",
        "family": "local_life",
        "city": "北京",
        "requirementLevel": "soft",
        "experienceShape": "area",
        "goal": "体验本地日常生活",
        "desiredSignals": ["community_market", "resident_activity"],
        "avoidSignals": ["museum_only"],
        "evidenceRequirements": {"minimumIndependentClaims": 1},
        "groundingContract": {"consumerRecheckRequired": True},
        "routeContract": {"maxDetourMinutes": 20},
    }
    value.update(overrides)
    return value


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _night_candidate(**overrides):
    value = _candidate(
        name="中央广播电视塔",
        type="风景名胜;观景台;电视塔",
        category="观景台",
        providerTypeCode="110202",
        tags=["观景台", "城市夜景"],
        sourceClaims=[],
        openTimeToday="18:00-22:00",
        providerEvidenceQueriedAt=(NOW - timedelta(hours=2)).isoformat(),
    )
    value.update(overrides)
    return value


def _night_consumer(**overrides):
    value = _consumer(
        family="night_view",
        activityMode="night_view",
        experienceShape="single_poi",
        experienceGoal="城市夜景",
        goal="城市夜景",
        desiredSignals=["night_view"],
        evidenceRequirements={},
        experienceSpecPolicy={
            "accessPolicy": "public_outdoor_or_verified_controlled_access",
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForControlledAccess": True,
                "requiredForPublicOutdoor": False,
                "allowExplicitNoClosure": True,
            },
        },
        specFingerprint="spec-night-v1",
    )
    value.update(overrides)
    return value


def _meal_candidate(**overrides):
    value = _candidate(
        id="B0000MEAL1",
        amapId="B0000MEAL1",
        name="京味测试餐厅",
        type="餐饮服务;中餐厅;北京菜",
        category="food",
        providerTypeCode="050100",
        tags=["地方风味"],
        sourceClaims=[{"claimKey": "local_food", "stance": "support"}],
    )
    value.update(overrides)
    return value


def _meal_consumer(**overrides):
    value = _consumer(
        city="北京",
        planningSlotId="slot_local",
        dayNumber=1,
        family="local_food",
        activityMode="meal",
        experienceShape="single_poi",
        experienceGoal="当地特色美食",
        goal="当地特色美食",
        desiredSignals=["provider_grounded_local_food"],
        evidenceRequirements={},
        experienceSpecPolicy={
            "intentType": "meal",
            "frequency": "one",
            "allowedDayNumbers": [1, 2],
            "experienceFamilies": ["local_food"],
            "accessPolicy": "verified_amap_food_service",
            "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
            "timeWindow": {"start": "12:00", "end": "13:15"},
            "detourTolerance": {
                "maxGeneralizedCostDelta": 20.0,
                "maxDetourRatio": 0.2,
            },
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForControlledAccess": False,
                "requiredForPublicOutdoor": False,
                "allowExplicitNoClosure": False,
            },
            "confidence": 0.84,
            "unresolvedDimensions": [],
        },
    )
    value.update(overrides)
    return value


def test_provider_evidence_projection_does_not_overwrite_when_source_is_missing():
    assert CandidateProviderEvidenceService.project(None) == {}
    assert CandidateProviderEvidenceService.project({}, include_missing=False) == {}
    assert CandidateProviderEvidenceService.project(
        {"tags": ["观景台"], "category": None},
        include_missing=False,
    ) == {"tags": ["观景台"]}


@pytest.mark.parametrize(
    "field,changed",
    [
        ("experienceFamily", "waterfront"),
        ("distinctnessPolicy", "distinct_physical_identity"),
        ("timeWindow", {"start": "20:00", "end": "22:00"}),
        ("detourTolerance", {"maxGeneralizedCostDelta": 20, "maxDetourRatio": 0.2}),
    ],
)
def test_experience_spec_fields_are_all_bound_into_admission_fingerprint(field, changed):
    service = ConsumerCandidateAdmissionService()
    policy = {
        "experienceFamily": "public_city_view",
        "accessPolicy": "public_outdoor",
        "distinctnessPolicy": "allow_same_family",
        "timeWindow": {"start": "19:00", "end": "22:00"},
        "detourTolerance": {"maxGeneralizedCostDelta": 35, "maxDetourRatio": 0.35},
        "evidenceFreshness": {"maxAgeHours": 24, "allowExplicitNoClosure": True},
    }
    baseline = service.build_consumer_context(
        brief_id="brief-night",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="night_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="公共城市夜景",
        experience_spec_policy=policy,
    )
    mutated_policy = {**policy, field: changed}
    changed_consumer = service.build_consumer_context(
        brief_id="brief-night",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="night_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="公共城市夜景",
        experience_spec_policy=mutated_policy,
    )

    baseline_report = service.evaluate(_night_candidate(), baseline)
    changed_report = service.evaluate(_night_candidate(), changed_consumer)

    assert baseline_report["consumerFingerprint"] != changed_report["consumerFingerprint"]


def test_provider_evidence_materialization_uses_db_safe_missing_defaults():
    poi = CandidateProviderEvidenceService.materialize_poi(
        {
            "amapId": "B000000001",
            "name": "景山公园",
            "city": "北京",
            "category": None,
            "type": "风景名胜;公园广场;公园",
            "source": "amap-place-search",
            "longitude": 116.403,
            "latitude": 39.925,
        },
        local_id="poi_local_1",
    )

    assert poi["category"] == ""
    assert poi["confidence"] == 0.0
    projected = CandidateProviderEvidenceService.project(poi)
    assert projected["category"] is None
    assert projected["confidence"] is None


def test_shared_amap_fact_is_recomputed_for_each_consumer():
    source_approved = _candidate(
        name="史家胡同博物馆",
        type="科教文化服务;博物馆",
        category="博物馆",
        providerTypeCode="140100",
        tags=["博物馆"],
        sourceClaims=[],
        semanticPassed=True,
        consumerAdmission={"decision": "admitted_final_anchor", "intentFingerprint": "stale"},
    )
    report = ConsumerCandidateAdmissionService().evaluate(source_approved, _consumer())
    assert report["classification"] in {"pending_evidence", "rejected"}
    assert report["classification"] != "admitted_final_anchor"
    assert report["consumerIntentFingerprint"] != "stale"
    assert "semanticPassed" not in report["evidenceUsed"]


def test_structured_provider_detail_plus_independent_claim_can_admit_local_life_anchor():
    report = ConsumerCandidateAdmissionService().evaluate(_candidate(), _consumer())
    assert report["classification"] in {"admitted_final_anchor", "admitted_anchor_set_member"}
    assert report["hardGatePassed"] is True
    assert report["evidenceSufficient"] is True


def test_report_match_requires_parent_and_opening_evidence_to_survive_materialization():
    candidate = _candidate(
        parentPoiId="B000AA3ZCC",
        openTimeToday="18:00-22:00",
        openTimeWeek="周一至周日 18:00-22:00",
        aliases=["朝阳菜市场"],
        rating=4.6,
        cost=35,
        routeDetourMinutes=8,
        children=[{"id": "B0FF000001", "name": "东门", "type": "出入口"}],
    )
    report = ConsumerCandidateAdmissionService().evaluate(candidate, _consumer())

    assert ConsumerCandidateAdmissionService.report_matches_poi(report, dict(candidate)) is True
    mutations = {
        "parentPoiId": None,
        "openTimeToday": "暂停开放",
        "providerTypeCode": "050100",
        "type": "餐饮服务;中餐厅",
        "category": "中餐厅",
        "tags": ["餐厅"],
        "businessArea": "国贸",
        "rating": 4.1,
        "cost": 99,
        "routeDetourMinutes": 18,
        "aliases": ["另一个地点"],
        "children": [],
        "sourceClaims": [],
    }
    for field, changed_value in mutations.items():
        changed = {**candidate, field: changed_value}
        assert ConsumerCandidateAdmissionService.report_matches_poi(report, changed) is False, field


def test_noncanonical_parent_identity_is_rejected_before_admission():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(parentPoiId="NOT_AN_AMAP_ID"),
        _consumer(),
    )

    assert report["classification"] == "rejected"
    assert report["reasonCodes"] == ["amap_parent_identity_invalid"]


def test_name_or_query_keyword_alone_cannot_prove_experience():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="本地生活体验点",
            type="",
            category="",
            providerTypeCode=None,
            tags=[],
            sourceClaims=[],
            sourceNote="query=本地生活 社区 市场",
        ),
        _consumer(),
    )
    assert report["classification"] == "pending_evidence"
    assert report["evidenceSufficient"] is False


def test_exact_meal_constraint_cannot_be_satisfied_by_different_food_family():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(name="四季民福烤鸭店", type="餐饮服务;中餐厅", category="中餐厅", tags=["烤鸭"]),
        _consumer(
            family="meal",
            experienceShape="single_poi",
            exactEntity="卤煮",
            assignedMealFamily="卤煮",
            desiredSignals=["卤煮"],
        ),
    )
    assert report["classification"] == "rejected"
    assert "exact_entity_mismatch" in report["reasonCodes"] or "meal_family_mismatch" in report["reasonCodes"]


def test_night_view_search_hint_cannot_rebind_an_unrelated_entity():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="花溪谷",
            type="风景名胜;风景名胜",
            category="风景名胜",
            tags=["景区"],
            sourceClaims=[],
            sourceNote="matchedCandidateHint:亮马河夜游步道;matchedCandidateHintBinding:search_query",
        ),
        _consumer(
            family="night_view",
            experienceShape="single_poi",
            exactEntity="亮马河夜游步道",
            desiredSignals=["night_view"],
        ),
    )
    assert report["classification"] == "rejected"
    assert "exact_entity_mismatch" in report["reasonCodes"]


def test_exact_night_entity_does_not_accept_a_different_tower_from_hint_provenance():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="雨燕塔",
            type="风景名胜;观景点",
            category="观景点",
            tags=["观景"],
            sourceNote="matchedCandidateHint:奥林匹克塔",
        ),
        _consumer(
            family="night_view",
            experienceShape="single_poi",
            exactEntity="奥林匹克塔",
        ),
    )
    assert report["classification"] == "rejected"
    assert report["reasonCodes"] == ["exact_entity_mismatch"]


def test_generic_local_meal_uses_provider_detail_and_claim_without_assigned_family():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="顺路餐馆",
            type="餐饮服务;中餐厅;中餐厅",
            category="中餐厅",
            providerTypeCode="050100",
            tags=["地方风味"],
            sourceClaims=[
                {
                    "claimKey": "local_food_context",
                    "stance": "support",
                    "sourceName": "public-guide",
                    "sourceUrlHash": "b" * 64,
                    "locality": "北京",
                }
            ],
        ),
        _consumer(
            family="meal",
            experienceShape="single_poi",
            experienceGoal="顺路的当地特色午餐",
            goal="顺路的当地特色午餐",
            assignedMealFamily=None,
            evidenceRequirements={"minimumIndependentClaims": 1},
        ),
    )
    assert report["classification"] == "admitted_final_anchor"
    assert report["scoreEligible"] is True


def test_generic_amap_local_food_subtype_stays_pending_without_city_specific_evidence():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="嘉宴小厨饺子馆(五道口店)",
            type="餐饮服务;中餐厅;特色/地方风味餐厅",
            category="特色/地方风味餐厅",
            providerTypeCode="050118",
            tags=[],
            sourceClaims=[],
        ),
        _consumer(
            family="meal",
            experienceShape="single_poi",
            experienceGoal="当地特色午餐",
            goal="当地特色午餐",
            desiredSignals=["local_food"],
            evidenceRequirements={},
        ),
    )

    assert report["classification"] == "pending_evidence"
    assert report["scoreEligible"] is False
    assert report["reasonCodes"] == ["local_food_evidence_missing"]
    assert report["evidenceSummary"]["authoritativeAmapLocalFoodSubtype"] is False


@pytest.mark.parametrize(
    ("goal", "expected_reason"),
    [
        ("体验北京烤鸭", "consumer_evidence_insufficient"),
        ("安排一家老字号餐厅", "local_food_evidence_missing"),
        ("查找今天营业且可以预约的当地餐厅", "local_food_evidence_missing"),
    ],
)
def test_amap_local_food_subtype_does_not_bypass_strong_meal_claim_evidence(goal, expected_reason):
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="京味餐厅",
            type="餐饮服务;中餐厅;特色/地方风味餐厅",
            category="特色/地方风味餐厅",
            providerTypeCode="050118",
            tags=[],
            sourceClaims=[],
        ),
        _consumer(
            family="meal",
            experienceShape="single_poi",
            experienceGoal=goal,
            goal=goal,
            desiredSignals=["local_food"],
            evidenceRequirements={},
        ),
    )

    assert report["classification"] == "pending_evidence"
    assert report["scoreEligible"] is False
    assert report["reasonCodes"] == [expected_reason]


def test_admitted_report_preserves_authoritative_hard_gate_order():
    report = ConsumerCandidateAdmissionService().evaluate(_candidate(), _consumer())
    assert [item["gate"] for item in report["gateResults"]] == [
        "amap_identity",
        "consumer_fingerprint_freshness",
        "exact_entity_binding",
        "provider_type",
        "experience_shape_compatibility",
        "semantic_affordance",
        "anchor_eligibility",
        "specialized_policy",
        "evidence_sufficiency",
        "route_schedule_context",
        "required_goal_coverage",
    ]


def test_coarse_candidate_detour_metadata_cannot_reject_before_provider_matrix():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(routeDetourMinutes=45),
        _consumer(routeContract={"maxDetourMinutes": 20}),
    )

    assert report["hardGatePassed"] is True
    assert report["classification"] in {
        "admitted_final_anchor",
        "admitted_anchor_set_member",
    }
    route_gate = next(item for item in report["gateResults"] if item["gate"] == "route_schedule_context")
    assert route_gate["passed"] is True
    assert route_gate["coarseDetourExceeded"] is True
    assert route_gate["decisiveRouteEvidence"] == "provider_route_matrix_required"


def test_consumer_context_binds_experience_policy_and_spec_fingerprint():
    policy = {
        "accessPolicy": "public_outdoor_or_verified_controlled_access",
        "evidenceFreshness": {
            "maxAgeHours": 24,
            "requiredForControlledAccess": True,
            "allowExplicitNoClosure": True,
        },
    }

    context = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id="brief-night",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="night_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="城市夜景",
        experience_spec_policy=policy,
        spec_fingerprint="spec-night-v1",
    )

    assert context["experienceSpecPolicy"] == {**policy, "experienceFamily": "night_view"}
    assert context["accessPolicy"] == policy["accessPolicy"]
    assert context["evidenceFreshness"] == policy["evidenceFreshness"]
    assert context["specFingerprint"] != "spec-night-v1"
    assert len(context["specFingerprint"]) == 64


def test_unknown_experience_access_policy_is_rejected_and_fingerprinted():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    report = service.evaluate(
        _night_candidate(),
        _night_consumer(
            experienceSpecPolicy={
                "accessPolicy": "custom",
                "evidenceFreshness": {"maxAgeHours": 24},
            }
        ),
    )
    known = service.evaluate(_night_candidate(), _night_consumer())

    assert report["classification"] == "rejected"
    assert report["scoreEligible"] is False
    assert report["reasonCodes"] == ["experience_access_policy_unknown"]
    assert report["consumerFingerprint"] != known["consumerFingerprint"]
    assert report["consumerScope"]["specFingerprint"] == "spec-night-v1"


def test_complete_meal_spec_uses_meal_access_policy_not_night_classifier(monkeypatch):
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)

    def night_policy_must_not_run(*_args, **_kwargs):
        raise AssertionError("meal candidate entered night-view access policy")

    monkeypatch.setattr(service.night_policy, "evaluate_access_policy", night_policy_must_not_run)
    candidate = _meal_candidate()
    report = service.evaluate(candidate, _meal_consumer())

    assert report["classification"] == "admitted_final_anchor"
    assert report["scoreEligible"] is True
    assert report["experienceAccessPolicy"] == {
        "decision": "accepted",
        "reasonCode": "experience_access_policy_passed",
        "accessPolicy": "verified_amap_food_service",
        "policyFamily": "meal",
        "accessClass": "controlled_food_service",
        "evidenceTimestamp": None,
        "evidenceAgeHours": None,
        "policyExemption": "amap_food_service_opening_evidence_not_required",
        "specFingerprint": report["consumerScope"]["specFingerprint"],
    }
    assert report["evidenceSummary"]["authoritativeAmapLocalFoodSubtype"] is True
    assert service.report_matches_poi(report, candidate) is True


def test_meal_access_policy_does_not_treat_local_food_claim_as_food_service_class():
    report = ConsumerCandidateAdmissionService(now_provider=lambda: NOW).evaluate(
        _meal_candidate(providerTypeCode="110000", type="风景名胜;旅游景点"),
        _meal_consumer(),
    )

    assert report["classification"] == "rejected"
    assert report["scoreEligible"] is False
    assert report["reasonCodes"] == ["experience_access_policy_mismatch"]


def test_malformed_experience_freshness_policy_is_rejected():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)

    report = service.evaluate(
        _night_candidate(),
        _night_consumer(
            experienceSpecPolicy={
                "accessPolicy": "verified_controlled_access",
                "evidenceFreshness": {"maxAgeHours": "24"},
            }
        ),
    )

    assert report["classification"] == "rejected"
    assert report["reasonCodes"] == ["experience_evidence_freshness_invalid"]


def test_non_mapping_experience_policy_builds_an_auditable_rejection():
    context = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id="brief-night",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="night_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="城市夜景",
        experience_spec_policy="not-a-mapping",
        spec_fingerprint="spec-night-v1",
    )

    report = ConsumerCandidateAdmissionService(now_provider=lambda: NOW).evaluate(_night_candidate(), context)

    assert context["experienceSpecPolicyError"] == ("experience_spec_policy_invalid")
    assert report["classification"] == "rejected"
    assert report["reasonCodes"] == ["experience_spec_policy_invalid"]
    assert report["consumerScope"]["experienceSpecPolicyError"] == ("experience_spec_policy_invalid")


def test_non_mapping_grounding_policy_cannot_disable_experience_contract():
    consumer = _night_consumer()
    consumer.pop("experienceSpecPolicy")
    consumer.pop("specFingerprint")
    consumer["groundingPolicy"] = "not-a-mapping"

    report = ConsumerCandidateAdmissionService(now_provider=lambda: NOW).evaluate(_night_candidate(), consumer)

    assert report["classification"] == "rejected"
    assert report["reasonCodes"] == ["experience_grounding_policy_invalid"]


@pytest.mark.parametrize(
    ("queried_at", "reason"),
    [
        (None, "experience_access_evidence_timestamp_missing"),
        (
            (NOW - timedelta(hours=25)).isoformat(),
            "experience_access_evidence_stale",
        ),
    ],
)
def test_controlled_access_requires_current_verifiable_opening_evidence(queried_at, reason):
    candidate = _night_candidate(providerEvidenceQueriedAt=queried_at)
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)

    report = service.evaluate(candidate, _night_consumer())

    assert report["classification"] == "pending_evidence"
    assert report["scoreEligible"] is False
    assert report["reasonCodes"] == [reason]
    assert report["experienceAccessPolicy"]["accessClass"] == "controlled_access"


def test_naive_access_evidence_timestamp_is_not_assumed_to_be_utc():
    report = ConsumerCandidateAdmissionService(now_provider=lambda: NOW).evaluate(
        _night_candidate(providerEvidenceQueriedAt="2026-08-09T10:00:00"),
        _night_consumer(),
    )

    assert report["classification"] == "pending_evidence"
    assert report["reasonCodes"] == ["experience_access_evidence_timestamp_invalid"]


def test_controlled_access_with_fresh_opening_evidence_is_admitted():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    candidate = _night_candidate()

    report = service.evaluate(candidate, _night_consumer())
    changed_spec = service.evaluate(
        candidate,
        _night_consumer(specFingerprint="spec-night-v2"),
    )

    assert report["classification"] == "admitted_final_anchor"
    assert report["scoreEligible"] is True
    assert report["experienceAccessPolicy"]["evidenceAgeHours"] == 2.0
    assert report["experienceAccessPolicy"]["specFingerprint"] == "spec-night-v1"
    assert report["consumerFingerprint"] != changed_spec["consumerFingerprint"]
    assert service.validate_report(report) is True
    assert service.report_matches_poi(report, candidate) is True
    assert (
        service.report_matches_poi(
            report,
            {
                **candidate,
                "providerEvidenceQueriedAt": (NOW - timedelta(hours=3)).isoformat(),
            },
        )
        is False
    )


def test_public_city_view_night_activity_requires_city_view_evidence():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    consumer = service.build_consumer_context(
        brief_id="brief-public-city-view",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="public_city_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="公共城市夜景",
        experience_spec_policy={
            "experienceFamily": "public_city_view",
            "accessPolicy": "public_outdoor",
            "evidenceFreshness": {"maxAgeHours": 24, "allowExplicitNoClosure": True},
        },
    )
    candidate = _night_candidate(
        name="滨水夜游步道",
        type="风景名胜;水域景观;滨水步道",
        category="滨水步道",
        tags=["滨水", "夜游", "步道"],
        openTimeToday=None,
        providerEvidenceQueriedAt=None,
    )

    report = service.evaluate(candidate, consumer)

    assert report["classification"] == "rejected"
    assert report["scoreEligible"] is False
    assert report["reasonCodes"] == ["public_city_view_evidence_missing"]
    assert report["evidenceUsed"]["nightViewPolicy"]["rejectReason"] == "public_city_view_evidence_missing"


def test_semantic_only_experience_scope_does_not_activate_access_contract():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    consumer = service.build_consumer_context(
        brief_id="brief-public-city-view",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="public_city_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="公共城市夜景",
        experience_spec_policy={
            "intentType": "night_view",
            "frequency": "one",
            "allowedDayNumbers": [1, 2],
            "experienceFamilies": ["public_city_view"],
            "unresolvedDimensions": [],
        },
    )
    candidate = _night_candidate(
        name="滨水夜游步道",
        type="风景名胜;水域景观;滨水步道",
        category="滨水步道",
        tags=["滨水", "夜游", "步道"],
        openTimeToday=None,
        providerEvidenceQueriedAt=None,
    )

    report = service.evaluate(candidate, consumer)

    assert report["classification"] == "rejected"
    assert report["reasonCodes"] == ["public_city_view_evidence_missing"]
    assert "experienceSpecPolicy" not in report["consumerScope"]
    assert "experienceAccessPolicy" not in report


def test_public_city_view_accepts_canonical_outdoor_city_view_with_fresh_evidence():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    consumer = service.build_consumer_context(
        brief_id="brief-public-city-view",
        pool_id="pool-night",
        planning_slot_id="slot-night",
        day_number=1,
        city="北京",
        family="public_city_view",
        activity_mode="night_view",
        requirement_level="required",
        experience_shape="single_poi",
        experience_goal="公共城市夜景",
        evidence_requirements={"minimumIndependentClaims": 1},
        experience_spec_policy={
            "experienceFamily": "public_city_view",
            "accessPolicy": "public_outdoor",
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForPublicOutdoor": True,
                "allowExplicitNoClosure": False,
            },
        },
    )
    fresh_at = (NOW - timedelta(hours=1)).isoformat()
    candidate = _night_candidate(
        name="城市阳台夜间观景空间",
        type="风景名胜;旅游景点;城市阳台",
        category="城市阳台",
        providerTypeCode="110000",
        tags=["公共空间", "城市夜景", "天际线", "夜间观景"],
        nightAvailabilityStatus="verified_open",
        openTimeToday="18:00-23:00",
        providerEvidenceQueriedAt=fresh_at,
        sourceClaims=[
            {
                "claimKey": "public_city_view_access",
                "stance": "support",
                "summary": "公共户外城市阳台夜间正常开放，可观城市天际线。",
                "sourceName": "official-place-notice",
                "sourceUrlHash": "e" * 64,
                "freshness": fresh_at,
            }
        ],
    )

    report = service.evaluate(candidate, consumer)

    assert candidate["source"] == "amap-place-search"
    assert candidate["amapId"] == candidate["id"]
    assert report["classification"] == "admitted_final_anchor"
    assert report["scoreEligible"] is True
    assert report["reasonCodes"] == ["consumer_admission_passed"]
    night_policy = report["evidenceUsed"]["nightViewPolicy"]
    assert night_policy["decision"] == "accepted"
    assert night_policy["rejectReason"] is None
    assert night_policy["experienceFamily"] == "public_city_view"
    assert night_policy["publicAccessTypePassed"] is True
    assert report["experienceAccessPolicy"]["decision"] == "accepted"
    assert report["experienceAccessPolicy"]["evidenceTimestamp"] == fresh_at
    assert report["experienceAccessPolicy"]["policyExemption"] is None
    assert report["evidenceSummary"]["independentSourceCount"] == 1
    assert report["evidenceSummary"]["supportingClaimCount"] == 1


def test_controlled_access_can_use_a_fresh_attributable_access_claim():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    candidate = _night_candidate(
        openTimeToday=None,
        providerEvidenceQueriedAt=None,
        sourceClaims=[
            {
                "claimKey": "opening_access",
                "stance": "support",
                "summary": "观景平台正常开放，可入场",
                "sourceName": "official-notice",
                "sourceUrlHash": "d" * 64,
                "freshness": (NOW - timedelta(hours=2)).isoformat(),
            }
        ],
    )

    report = service.evaluate(candidate, _night_consumer())

    assert report["classification"] == "admitted_final_anchor"
    assert report["scoreEligible"] is True
    assert report["experienceAccessPolicy"]["evidenceTimestamp"] == (NOW - timedelta(hours=2)).isoformat()
    assert service.report_matches_poi(report, candidate) is True


def test_public_outdoor_access_is_exempt_only_when_policy_allows_no_closure():
    public_candidate = _night_candidate(
        name="亮马河国际风情水岸",
        type="风景名胜;水域景观;滨水步道",
        category="滨水步道",
        providerTypeCode="110102",
        tags=["水岸", "夜景", "公共空间"],
        openTimeToday=None,
        providerEvidenceQueriedAt=None,
    )
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)

    allowed = service.evaluate(public_candidate, _night_consumer())
    required = service.evaluate(
        public_candidate,
        _night_consumer(
            experienceSpecPolicy={
                "accessPolicy": "public_outdoor_or_verified_controlled_access",
                "evidenceFreshness": {
                    "maxAgeHours": 24,
                    "requiredForControlledAccess": True,
                    "requiredForPublicOutdoor": True,
                    "allowExplicitNoClosure": True,
                },
            }
        ),
    )

    assert allowed["classification"] == "admitted_final_anchor"
    assert allowed["experienceAccessPolicy"]["policyExemption"] == ("public_outdoor_no_explicit_closure")
    assert required["classification"] == "pending_evidence"
    assert required["reasonCodes"] == ["experience_access_evidence_missing"]


def test_public_outdoor_explicit_closure_and_controlled_policy_mismatch_reject():
    service = ConsumerCandidateAdmissionService(now_provider=lambda: NOW)
    public_closed = _night_candidate(
        name="亮马河国际风情水岸",
        type="风景名胜;水域景观;滨水步道",
        category="滨水步道",
        tags=["水岸", "夜景", "公共空间"],
        openTimeToday="暂停开放",
        providerEvidenceQueriedAt=NOW.isoformat(),
    )
    controlled_under_public_only = _night_consumer(
        experienceSpecPolicy={
            "accessPolicy": "public_outdoor",
            "evidenceFreshness": {
                "maxAgeHours": 24,
                "requiredForControlledAccess": True,
                "requiredForPublicOutdoor": False,
                "allowExplicitNoClosure": True,
            },
        }
    )

    closed_report = service.evaluate(public_closed, _night_consumer())
    mismatch_report = service.evaluate(_night_candidate(), controlled_under_public_only)

    assert closed_report["classification"] == "rejected"
    assert closed_report["reasonCodes"] == ["experience_access_explicitly_closed"]
    assert mismatch_report["classification"] == "rejected"
    assert mismatch_report["reasonCodes"] == ["experience_access_policy_mismatch"]


@pytest.mark.parametrize("city", ["深圳", "广州"])
def test_same_contract_and_gate_protocol_work_without_city_configuration(city):
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(city=city),
        _consumer(city=city),
    )
    assert report["classification"] == "admitted_anchor_set_member"
    assert report["consumerScope"]["city"] == city
    assert report["schemaVersion"] == "consumer-candidate-admission-v2"


def test_open_walk_without_boundary_contract_remains_pending_instead_of_fake_poi():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(),
        _consumer(experienceShape="open_walk", routeContext={}),
    )
    assert report["classification"] == "pending_evidence"
    assert "open_walk_boundary_evidence_missing" in report["reasonCodes"]


def test_city_suffix_variation_does_not_reject_same_amap_city():
    report = ConsumerCandidateAdmissionService().evaluate(_candidate(city="北京市"), _consumer(city="北京"))
    assert report["classification"] == "admitted_anchor_set_member"


def test_local_food_alias_cannot_bypass_meal_evidence_contract():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            name="西式简餐店",
            type="餐饮服务;外国餐厅;西餐厅",
            category="西餐厅",
            providerTypeCode="050200",
            tags=["西餐"],
            sourceClaims=[],
        ),
        _consumer(
            family="local_food",
            optionalExperienceFamily="local_food",
            experienceShape="single_poi",
            goal="体验当地特色午餐",
            desiredSignals=["local_food"],
        ),
    )
    assert report["classification"] in {"pending_evidence", "rejected"}
    assert report["scoreEligible"] is False


def test_unrelated_operational_claim_cannot_satisfy_local_life_evidence():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(
            sourceClaims=[
                {
                    "claimKey": "operational_info",
                    "stance": "neutral",
                    "summary": "营业时间每天九点到十七点",
                    "sourceUrlHash": "c" * 64,
                }
            ]
        ),
        _consumer(),
    )
    assert report["classification"] == "pending_evidence"
    assert report["evidenceSummary"]["relevantSupportingClaimCount"] == 0


def test_micro_route_requires_bounded_set_evidence_not_one_poi():
    report = ConsumerCandidateAdmissionService().evaluate(_candidate(), _consumer(experienceShape="micro_route"))
    assert report["classification"] == "pending_evidence"
    assert report["reasonCodes"] == ["micro_route_set_evidence_missing"]


def test_report_fingerprint_covers_identity_fields_and_is_self_validating():
    service = ConsumerCandidateAdmissionService()
    report = service.evaluate(_candidate(), _consumer())
    changed = service.evaluate(_candidate(address="另一条街"), _consumer())
    assert report["candidateEvidenceFingerprint"] != changed["candidateEvidenceFingerprint"]
    assert service.validate_report(report) is True
    report["evidenceUsed"]["name"] = "被篡改"
    assert service.validate_report(report) is False


def test_claim_without_auditable_source_identity_is_not_independent_evidence():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(sourceClaims=[{"claimKey": "local_life", "stance": "support"}]),
        _consumer(),
    )
    assert report["classification"] == "pending_evidence"
    assert report["evidenceSummary"]["independentSourceCount"] == 0


def test_open_walk_area_id_without_two_boundaries_or_geometry_remains_pending():
    report = ConsumerCandidateAdmissionService().evaluate(
        _candidate(),
        _consumer(experienceShape="open_walk", routeContext={"areaId": "area_1"}),
    )
    assert report["classification"] == "pending_evidence"
    assert report["reasonCodes"] == ["open_walk_boundary_evidence_missing"]

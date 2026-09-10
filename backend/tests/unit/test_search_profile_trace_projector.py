from __future__ import annotations

import hashlib
import json
import sqlite3

from src.api.schemas.agent import AgentInitialPlanOutput
from src.models.poi_search_profile import ExperienceSemanticInput
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
)
from src.services.agent_service import AgentService
from src.services.itinerary_service import ItineraryService
from src.services.portfolio_candidate_discovery_service import (
    PortfolioCandidateDiscoveryService,
)
from src.services.search_profile_trace_projector import (
    checkpoint_semantic_contract_evidence,
    checkpoint_semantic_contract_fingerprint,
    project_grounding_report_for_persistence,
    project_pool_report_for_checkpoint,
    project_pool_report_for_trace,
    project_search_profile,
)


def _expected_checkpoint_binding(
    profile: dict,
    *,
    required_slot_ids: tuple[str, ...] = ("slot-safe",),
    slot_day_numbers: dict[str, int] | None = None,
) -> dict:
    return {
        "bindingVersion": "portfolio-checkpoint-expected-binding-v1",
        "searchProfile": profile,
        "requiredSlotIds": list(required_slot_ids),
        "slotDayNumbers": slot_day_numbers
        or {slot_id: index for index, slot_id in enumerate(required_slot_ids, start=1)},
        "targetCount": profile["coveragePolicy"]["targetCount"],
    }


def test_trace_projection_excludes_user_text_keywords_and_provider_payloads():
    secret_text = "token=secret https://example.test C:/private/trace.json"
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-safe",
            briefId="brief-safe",
            planningSlotId="slot-safe",
            requirementLevel="optional",
            rawNeed=secret_text,
            intentType="area_walk",
            optionalExperienceFamily="local_life",
            semanticContext=secret_text,
            candidateHints=[secret_text],
            excludedPhysicalPoiIds=["B0OPAQUE"],
            targetCount=1,
            maxQueries=4,
        )
    )

    projected_profile = project_search_profile(profile)
    projected_report = project_pool_report_for_trace(
        {
            "poolId": "pool-safe",
            "rawNeed": secret_text,
            "candidateHints": [secret_text],
            "exactEntity": secret_text,
            "searchProfile": profile.model_dump(by_alias=True),
            "safeCandidates": [
                {
                    "amapId": "B0SAFE001",
                    "name": "东四社区文化市场",
                    "source": "amap-place-search",
                    "discoveryProvenance": {
                        "webUrl": "https://example.test/private",
                        "providerPayload": secret_text,
                    },
                }
            ],
        }
    )
    serialized = json.dumps(
        {"profile": projected_profile, "report": projected_report},
        ensure_ascii=False,
    )

    assert secret_text not in serialized
    assert "example.test" not in serialized
    assert "rawNeed" not in projected_report
    assert "candidateHints" not in projected_report
    assert "exactEntity" not in projected_report
    assert "sourceEvidence" not in projected_profile
    assert "keyword" not in serialized
    assert projected_profile["excludedPhysicalPoiCount"] == 1
    assert projected_report["safeCandidates"] == [
        {
            "amapId": "B0SAFE001",
            "name": "东四社区文化市场",
            "source": "amap-place-search",
        }
    ]


def test_candidate_trace_and_checkpoint_preserve_bounded_provider_replay_evidence():
    candidate = {
        "id": "B0SAFE001",
        "amapId": "B0SAFE001",
        "name": "独立城市公园",
        "type": "风景名胜;公园广场;公园",
        "providerTypeCode": "110200",
        "parentPoiId": "B0PARENT001",
        "indoorParentPoiId": "B0INDOOR001",
        "businessStatus": "营业中",
        "providerQueriedAt": "2026-08-24T08:00:00+00:00",
        "providerQueryReceiptFingerprint": "F" * 64,
        "city": "测试",
        "district": "中心区",
        "address": "公园路 1 号",
        "longitude": 116.42,
        "latitude": 39.93,
        "category": "park",
        "source": "amap-place-search",
        "providerPayload": {"authorization": "Bearer private"},
    }

    trace_candidate = project_pool_report_for_trace(
        {"safeCandidates": [candidate]}
    )["safeCandidates"][0]
    checkpoint_candidate = project_pool_report_for_checkpoint(
        {"safeCandidates": [candidate]}
    )["safeCandidates"][0]

    expected_provider_evidence = {
        "providerTypeCode": "110200",
        "parentPoiId": "B0PARENT001",
        "indoorParentPoiId": "B0INDOOR001",
        "businessStatus": "营业中",
        "providerQueriedAt": "2026-08-24T08:00:00+00:00",
        "providerQueryReceiptFingerprint": "f" * 64,
    }
    for projected in (trace_candidate, checkpoint_candidate):
        assert {
            key: projected[key] for key in expected_provider_evidence
        } == expected_provider_evidence
        assert "providerPayload" not in projected


def test_candidate_trace_drops_invalid_provider_receipt_but_keeps_parent_and_typecode():
    projected = project_pool_report_for_trace(
        {
            "safeCandidates": [
                {
                    "amapId": "B0SAFE001",
                    "parent_poi_id": "B0PARENT001",
                    "indoor_parent_poi_id": "B0INDOOR001",
                    "provider_type_code": "110200",
                    "provider_query_receipt_fingerprint": "token=private",
                    "source": "amap-place-search",
                }
            ]
        }
    )["safeCandidates"][0]

    assert projected["providerTypeCode"] == "110200"
    assert projected["parentPoiId"] == "B0PARENT001"
    assert projected["indoorParentPoiId"] == "B0INDOOR001"
    assert "providerQueryReceiptFingerprint" not in projected


def test_checkpoint_projection_is_resumable_but_not_an_executable_profile():
    secret_text = "token=secret https://example.test C:/private/trace.json"
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-safe",
            briefId="brief-safe",
            planningSlotId="slot-safe",
            requirementLevel="optional",
            rawNeed=secret_text,
            intentType="area_walk",
            optionalExperienceFamily="local_life",
            candidateHints=[secret_text],
            excludedPhysicalPoiIds=["B0OLD"],
            targetCount=1,
            maxQueries=4,
        )
    )
    candidate = {
        "id": "B0SAFE001",
        "amapId": "B0SAFE001",
        "name": "东四社区文化市场",
        "type": "购物服务;综合市场;农贸市场",
        "providerTypeCode": "060703",
        "tags": ["农贸市场", "社区商业"],
        "sourceClaims": [
            {
                "claimType": "local_life_context",
                "stance": "support",
                "summary": "面向附近居民的日常采购市场",
                "sourceUrlHash": "a" * 64,
            }
        ],
        "city": "北京",
        "district": "东城区",
        "address": "东四街道",
        "longitude": 116.42,
        "latitude": 39.93,
        "category": "market",
        "source": "amap-place-search",
        "sourceNote": "来自高德地图地点搜索",
        "confidence": 0.92,
        "planningSlotId": "slot-safe",
        "dayNumber": 1,
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "semanticPassed": True,
        "semanticMatchScore": 0.9,
        "searchProfileFingerprint": profile.profileFingerprint,
        "exclusionFingerprint": profile.exclusionFingerprint,
        "experienceFamily": profile.experienceFamily,
        "activityMode": profile.activityMode,
        "candidateSource": "web_seed_amap_grounded",
        "discoveryProvenance": {"providerPayload": secret_text},
    }
    scope = {
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "planningSlotId": "slot-safe",
        "dayNumber": 1,
    }
    safe_query = "北京 社区 市场"
    report = {
        "poolId": "pool-safe",
        "briefId": "brief-safe",
        "city": "北京",
        "intentType": "area_walk",
        "targetCount": 1,
        "requirementLevel": "optional",
        "optionalExperienceFamily": "local_life",
        "entityBindingMode": profile.entityBindingMode,
        "searchProfileFingerprint": profile.profileFingerprint,
        "exclusionFingerprint": profile.exclusionFingerprint,
        "experienceFamily": profile.experienceFamily,
        "activityMode": profile.activityMode,
        "rawNeed": secret_text,
        "candidateHints": [secret_text],
        "exactEntity": secret_text,
        "searchProfile": profile.model_dump(by_alias=True),
        "requiredSlotIds": ["slot-safe"],
        "slotDayNumbers": {"slot-safe": 1},
        "resolvedSlotIds": ["slot-safe"],
        "unresolvedSlotIds": [],
        "safeCandidates": [candidate],
        "selectedCandidates": [candidate],
        "webDiscovery": {
            "status": "grounded",
            "searchProfileFingerprint": profile.profileFingerprint,
            "exclusionFingerprint": profile.exclusionFingerprint,
            "experienceFamily": profile.experienceFamily,
            "activityMode": profile.activityMode,
            "attempts": [
                {
                    "status": "grounded",
                    "providerStatus": "success",
                    "providerName": "recorded-web-search",
                    "query": safe_query,
                    "scope": scope,
                    "selectedCandidates": [
                        {
                            "amapId": "B0SAFE001",
                            "name": "东四社区文化市场",
                            "scope": scope,
                        }
                    ],
                    "providerPayload": secret_text,
                }
            ],
        },
    }

    checkpoint = project_pool_report_for_checkpoint(report)
    persisted = project_grounding_report_for_persistence({"poolReports": [report], "resultState": "needs_confirmation"})
    serialized = json.dumps(
        {"checkpoint": checkpoint, "persisted": persisted},
        ensure_ascii=False,
    )

    assert secret_text not in serialized
    assert "searchProfile" not in checkpoint
    assert checkpoint["checkpointEvidenceProjectionVersion"] == ("portfolio-resume-evidence-v2")
    assert checkpoint["searchProfileTrace"]["profileFingerprint"] == (profile.profileFingerprint)
    assert checkpoint["selectedCandidates"][0]["address"] == "东四街道"
    assert "discoveryProvenance" not in checkpoint["selectedCandidates"][0]
    assert checkpoint["webDiscovery"]["attemptCount"] == 1
    assert checkpoint["webDiscovery"]["attempts"] == [
        {
            "status": "grounded",
            "providerStatus": "success",
            "providerName": "recorded-web-search",
            "queryFingerprint": hashlib.sha256(safe_query.encode("utf-8")).hexdigest(),
            "scope": scope,
            "selectedCandidates": [
                {
                    "amapId": "B0SAFE001",
                    "name": "东四社区文化市场",
                    "scope": scope,
                }
            ],
        }
    ]

    service_checkpoint = AgentService._portfolio_grounding_checkpoint(
        {"poolReports": [report], "resultState": "needs_confirmation"}
    )
    service_serialized = json.dumps(service_checkpoint, ensure_ascii=False)
    assert secret_text not in service_serialized
    assert "searchProfile" not in service_checkpoint["poolReports"][0]
    assert service_checkpoint["poolReports"][0]["searchProfileTrace"] == (checkpoint["searchProfileTrace"])

    class _NoDiscovery:
        max_web_queries = 1

        def discover(self, **_kwargs):
            raise AssertionError("a verified checkpoint pool must be reused")

    discovery_service = PortfolioCandidateDiscoveryService(discovery_service=_NoDiscovery())
    expected_profile = profile.model_dump(by_alias=True)
    expected_binding = _expected_checkpoint_binding(expected_profile)
    expected_profiles = {("brief-safe", "pool-safe", "slot-safe"): expected_binding}
    required_trace_fields = (
        "profileFingerprint",
        "experienceFamily",
        "activityMode",
        "fallbackPolicy",
        "coveragePolicy",
        "budgetPolicy",
        "exclusionFingerprint",
        "executionFingerprint",
        "semanticContractEvidence",
        "semanticContractFingerprint",
    )
    for missing_field in required_trace_fields:
        missing_contract = json.loads(
            json.dumps(
                service_checkpoint["poolReports"][0],
                ensure_ascii=False,
            )
        )
        missing_contract["searchProfileTrace"].pop(missing_field)
        invalid_result = discovery_service.augment(
            [missing_contract],
            reuse_existing_discovery=True,
            expected_checkpoint_profiles=expected_profiles,
        )
        assert invalid_result.metrics["webQueryCount"] == 0
        assert invalid_result.metrics["reusedDiscoveryQueryCount"] == 0
        assert invalid_result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
        assert invalid_result.pool_reports[0]["selectedCandidates"] == []
    for missing_query_field in (
        "planId",
        "mode",
        "providerCategoryKeys",
    ):
        missing_query_contract = json.loads(
            json.dumps(
                service_checkpoint["poolReports"][0],
                ensure_ascii=False,
            )
        )
        missing_query_contract["searchProfileTrace"]["queryPlans"][0].pop(missing_query_field)
        invalid_result = discovery_service.augment(
            [missing_query_contract],
            reuse_existing_discovery=True,
            expected_checkpoint_profiles=expected_profiles,
        )
        assert invalid_result.metrics["webQueryCount"] == 0
        assert invalid_result.metrics["reusedDiscoveryQueryCount"] == 0
        assert invalid_result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
        assert invalid_result.pool_reports[0]["selectedCandidates"] == []
    for missing_scope_field in ("requiredSlotIds", "slotDayNumbers"):
        missing_scope_contract = json.loads(
            json.dumps(
                service_checkpoint["poolReports"][0],
                ensure_ascii=False,
            )
        )
        missing_scope_contract.pop(missing_scope_field)
        invalid_result = discovery_service.augment(
            [missing_scope_contract],
            reuse_existing_discovery=True,
            expected_checkpoint_profiles=expected_profiles,
        )
        assert invalid_result.metrics["webQueryCount"] == 0
        assert invalid_result.metrics["reusedDiscoveryQueryCount"] == 0
        assert invalid_result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
        assert invalid_result.pool_reports[0]["selectedCandidates"] == []
    assert (
        discovery_service._checkpoint_profile_trace_is_consistent(
            service_checkpoint["poolReports"][0],
            service_checkpoint["poolReports"][0]["searchProfileTrace"],
            expected_checkpoint_binding=expected_binding,
        )
        is True
    )
    assert (
        discovery_service._checkpoint_profile_coverage_assessment(
            service_checkpoint["poolReports"][0],
            expected_checkpoint_binding=expected_binding,
        ).covered
        is True
    )
    assert (
        discovery_service._can_reuse_discovery(
            service_checkpoint["poolReports"][0],
            expected_checkpoint_binding=expected_binding,
        )
        is True
    )
    for source_scope_field in (
        "sourceBriefId",
        "sourcePoolId",
        "sourcePlanningSlotId",
    ):
        conflicting_report = json.loads(json.dumps(report, ensure_ascii=False))
        conflicting_report["selectedCandidates"][0][source_scope_field] = "conflicting-source-scope"
        assert (
            checkpoint_semantic_contract_evidence(
                conflicting_report,
                expected_profile,
            )
            == {}
        )

        conflicting_checkpoint = project_pool_report_for_checkpoint(conflicting_report)
        conflicting_trace = conflicting_checkpoint["searchProfileTrace"]
        assert (
            discovery_service._checkpoint_profile_trace_is_consistent(
                conflicting_checkpoint,
                conflicting_trace,
                expected_checkpoint_binding=expected_binding,
            )
            is False
        )
        assert (
            discovery_service._checkpoint_profile_coverage_assessment(
                conflicting_checkpoint,
                expected_checkpoint_binding=expected_binding,
            ).covered
            is False
        )
        assert (
            discovery_service._can_reuse_discovery(
                conflicting_checkpoint,
                expected_checkpoint_binding=expected_binding,
            )
            is False
        )
    malformed_source_report = json.loads(json.dumps(report, ensure_ascii=False))
    malformed_source_report["selectedCandidates"][0]["sourceBriefId"] = "conflicting source scope"
    assert (
        checkpoint_semantic_contract_evidence(
            malformed_source_report,
            expected_profile,
        )
        == {}
    )
    assert discovery_service._can_reuse_discovery(service_checkpoint["poolReports"][0]) is False
    missing_expected = discovery_service.augment(
        service_checkpoint["poolReports"],
        reuse_existing_discovery=True,
    )
    assert missing_expected.metrics["webQueryCount"] == 0
    assert missing_expected.metrics["reusedDiscoveryQueryCount"] == 0
    assert missing_expected.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
    tampered_report = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    tampered_report["selectedCandidates"][0]["searchProfileFingerprint"] = "0" * 64
    assert (
        discovery_service._can_reuse_discovery(
            tampered_report,
            expected_checkpoint_binding=expected_binding,
        )
        is False
    )
    tampered_result = discovery_service.augment(
        [tampered_report],
        reuse_existing_discovery=True,
        expected_checkpoint_profiles=expected_profiles,
    )
    assert tampered_result.metrics["webQueryCount"] == 0
    assert tampered_result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
    assert tampered_result.pool_reports[0]["selectedCandidates"] == []
    assert tampered_result.pool_reports[0]["resolvedSlotIds"] == []
    assert tampered_result.pool_reports[0]["unresolvedSlotIds"] == ["slot-safe"]

    missing_coverage = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    missing_coverage["searchProfileTrace"].pop("coveragePolicy")
    assert (
        discovery_service._can_reuse_discovery(
            missing_coverage,
            expected_checkpoint_binding=expected_binding,
        )
        is False
    )
    excluded_set_tampered = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    excluded_set_tampered["searchProfileTrace"]["excludedPhysicalPoiIds"] = []
    excluded_set_tampered["searchProfileTrace"]["excludedPhysicalPoiCount"] = 0
    assert (
        discovery_service._can_reuse_discovery(
            excluded_set_tampered,
            expected_checkpoint_binding=expected_binding,
        )
        is False
    )
    marker_tampered = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    marker_tampered["checkpointEvidenceProjectionVersion"] = "tampered-version"
    trace_deleted = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    trace_deleted.pop("searchProfileTrace")
    marker_deleted = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    marker_deleted.pop("checkpointEvidenceProjectionVersion")
    all_contract_fields_deleted = json.loads(json.dumps(service_checkpoint["poolReports"][0], ensure_ascii=False))
    all_contract_fields_deleted.pop("checkpointEvidenceProjectionVersion")
    all_contract_fields_deleted.pop("searchProfileTrace")
    for corrupted_checkpoint in (
        marker_tampered,
        trace_deleted,
        marker_deleted,
        all_contract_fields_deleted,
    ):
        corrupted_result = discovery_service.augment(
            [corrupted_checkpoint],
            reuse_existing_discovery=True,
            expected_checkpoint_profiles=expected_profiles,
        )
        assert corrupted_result.metrics["webQueryCount"] == 0
        assert corrupted_result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
        assert corrupted_result.pool_reports[0]["selectedCandidates"] == []

    reuse_result = discovery_service.augment(
        service_checkpoint["poolReports"],
        reuse_existing_discovery=True,
        expected_checkpoint_profiles=expected_profiles,
    )
    assert reuse_result.metrics["reusedDiscoveryQueryCount"] == 1
    assert reuse_result.metrics["webQueryCount"] == 0
    assert reuse_result.pool_reports[0]["selectedCandidates"][0]["amapId"] == "B0SAFE001"

    second_checkpoint = AgentService._portfolio_grounding_checkpoint({"poolReports": reuse_result.pool_reports})
    second_serialized = json.dumps(second_checkpoint, ensure_ascii=False)
    assert secret_text not in second_serialized
    assert safe_query not in second_serialized
    assert second_checkpoint["poolReports"][0]["checkpointEvidenceProjectionVersion"] == "portfolio-resume-evidence-v2"
    assert (
        second_checkpoint["poolReports"][0]["searchProfileTrace"]
        == (service_checkpoint["poolReports"][0]["searchProfileTrace"])
    )
    assert (
        second_checkpoint["poolReports"][0]["webDiscovery"]["attempts"][0]["queryFingerprint"]
        == hashlib.sha256(safe_query.encode("utf-8")).hexdigest()
    )
    second_reuse = discovery_service.augment(
        second_checkpoint["poolReports"],
        reuse_existing_discovery=True,
        expected_checkpoint_profiles=expected_profiles,
    )
    assert second_reuse.metrics["reusedDiscoveryQueryCount"] == 1
    assert second_reuse.metrics["webQueryCount"] == 0


def test_server_restored_expansion_plan_binds_legitimate_zero_provider_reuse():
    connection = sqlite3.connect(":memory:")
    initial_plan = AgentInitialPlanOutput.model_validate(
        {
            "reply": "",
            "mode": "plan",
            "daySlots": [
                {
                    "slotId": "slot-safe",
                    "dayNumber": 1,
                    "date": "2026-08-01",
                    "timeWindow": "afternoon",
                    "startTime": "14:00",
                    "durationMinutes": 90,
                    "kind": "visit",
                    "rawNeed": "体验社区生活与传统市场",
                    "routeAnchor": True,
                },
                {
                    "slotId": "slot-safe-2",
                    "dayNumber": 2,
                    "date": "2026-08-02",
                    "timeWindow": "afternoon",
                    "startTime": "14:00",
                    "durationMinutes": 90,
                    "kind": "visit",
                    "rawNeed": "体验另一处社区生活与传统市场",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "pool-safe",
                    "briefId": "brief-safe",
                    "rawNeed": "体验社区生活与传统市场",
                    "city": "北京",
                    "intentType": "area_walk",
                    "targetCount": 2,
                    "requirementLevel": "optional",
                    "optionalExperienceFamily": "local_life",
                    "assignToSlots": ["slot-safe", "slot-safe-2"],
                    "hintPolicy": "no_hint",
                    "entityBindingMode": "category",
                }
            ],
        }
    )
    pipeline_context = {
        "checkpointExpansionResume": True,
        "portfolioGroundingFocusBriefId": "brief-safe",
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-08-01", "2026-08-02"],
        },
    }
    service = AgentService.__new__(AgentService)
    reuse, expected_profiles = service._portfolio_checkpoint_discovery_binding(
        grounding_report={
            "initialPlan": initial_plan.model_dump(by_alias=True),
            "pipelineContext": pipeline_context,
        },
        itinerary_service=ItineraryService(connection),
    )
    expected_binding = expected_profiles[("brief-safe", "pool-safe", "slot-safe")]
    profile = expected_binding["searchProfile"]
    candidate = {
        "id": "B0LOCAL001",
        "amapId": "B0LOCAL001",
        "name": "东四社区文化市场",
        "type": "购物服务;综合市场;农贸市场",
        "providerTypeCode": "060703",
        "tags": ["农贸市场", "社区商业"],
        "sourceClaims": [
            {
                "claimType": "local_life_context",
                "stance": "support",
                "summary": "面向附近居民的日常采购市场",
                "sourceUrlHash": "a" * 64,
            }
        ],
        "providerType": "购物服务;综合市场;农贸市场",
        "city": "北京",
        "longitude": 116.42,
        "latitude": 39.93,
        "source": "amap-place-search",
        "planningSlotId": "slot-safe",
        "dayNumber": 1,
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "semanticPassed": True,
        "semanticMatchScore": 0.9,
        "searchProfileFingerprint": profile["profileFingerprint"],
        "exclusionFingerprint": profile["exclusionFingerprint"],
        "experienceFamily": profile["experienceFamily"],
        "activityMode": profile["activityMode"],
    }
    second_candidate = {
        **candidate,
        "id": "B0LOCAL002",
        "amapId": "B0LOCAL002",
        "name": "鼓楼社区便民市场",
        "longitude": 116.405,
        "latitude": 39.945,
        "planningSlotId": "slot-safe-2",
        "dayNumber": 2,
    }
    checkpoint = AgentService._portfolio_grounding_checkpoint(
        {
            "poolReports": [
                {
                    "poolId": "pool-safe",
                    "briefId": "brief-safe",
                    "city": "北京",
                    "intentType": "area_walk",
                    "targetCount": 2,
                    "requirementLevel": "optional",
                    "optionalExperienceFamily": "local_life",
                    "entityBindingMode": profile["entityBindingMode"],
                    "searchProfileFingerprint": profile["profileFingerprint"],
                    "exclusionFingerprint": profile["exclusionFingerprint"],
                    "experienceFamily": profile["experienceFamily"],
                    "activityMode": profile["activityMode"],
                    "searchProfile": profile,
                    "requiredSlotIds": ["slot-safe", "slot-safe-2"],
                    "slotDayNumbers": {
                        "slot-safe": 1,
                        "slot-safe-2": 2,
                    },
                    "resolvedSlotIds": ["slot-safe", "slot-safe-2"],
                    "unresolvedSlotIds": [],
                    "safeCandidates": [candidate, second_candidate],
                    "selectedCandidates": [candidate, second_candidate],
                    "webDiscovery": {
                        "status": "grounded",
                        "searchProfileFingerprint": profile["profileFingerprint"],
                        "exclusionFingerprint": profile["exclusionFingerprint"],
                        "experienceFamily": profile["experienceFamily"],
                        "activityMode": profile["activityMode"],
                        "attempts": [
                            {
                                "status": "grounded",
                                "providerStatus": "success",
                                "providerName": "recorded-web-search",
                                "query": "北京 社区 市场",
                                "scope": {
                                    "briefId": "brief-safe",
                                    "poolId": "pool-safe",
                                    "planningSlotId": "slot-safe",
                                    "dayNumber": 1,
                                },
                                "selectedCandidates": [
                                    {
                                        "amapId": "B0LOCAL001",
                                        "name": "东四社区文化市场",
                                        "scope": {
                                            "briefId": "brief-safe",
                                            "poolId": "pool-safe",
                                            "planningSlotId": "slot-safe",
                                            "dayNumber": 1,
                                        },
                                    }
                                ],
                            },
                            {
                                "status": "grounded",
                                "providerStatus": "success",
                                "providerName": "recorded-web-search",
                                "query": "北京 鼓楼 社区 市场",
                                "scope": {
                                    "briefId": "brief-safe",
                                    "poolId": "pool-safe",
                                    "planningSlotId": "slot-safe-2",
                                    "dayNumber": 2,
                                },
                                "selectedCandidates": [
                                    {
                                        "amapId": "B0LOCAL002",
                                        "name": "鼓楼社区便民市场",
                                        "scope": {
                                            "briefId": "brief-safe",
                                            "poolId": "pool-safe",
                                            "planningSlotId": "slot-safe-2",
                                            "dayNumber": 2,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                }
            ]
        }
    )

    class _NoDiscovery:
        max_web_queries = 1

        def discover(self, **_kwargs):
            raise AssertionError("legitimate resume must reuse without provider")

    discovery_service = PortfolioCandidateDiscoveryService(discovery_service=_NoDiscovery())
    assessment = discovery_service._checkpoint_profile_coverage_assessment(
        checkpoint["poolReports"][0],
        expected_checkpoint_binding=expected_binding,
    )
    assert assessment.covered is True, assessment
    assert (
        discovery_service._can_reuse_discovery(
            checkpoint["poolReports"][0],
            expected_checkpoint_binding=expected_binding,
        )
        is True
    )
    result = discovery_service.augment(
        checkpoint["poolReports"],
        reuse_existing_discovery=reuse,
        expected_checkpoint_profiles=expected_profiles,
    )

    assert reuse is True
    assert result.metrics["reusedDiscoveryQueryCount"] == 1, (
        result.metrics,
        result.pool_reports,
    )
    assert result.metrics["webQueryCount"] == 0
    assert result.pool_reports[0]["selectedCandidates"][0]["amapId"] == ("B0LOCAL001")

    cross_slot = json.loads(json.dumps(checkpoint["poolReports"][0]))
    cross_slot["requiredSlotIds"].append("slot-evil")
    cross_slot["slotDayNumbers"]["slot-evil"] = 3
    cross_slot["resolvedSlotIds"].append("slot-evil")
    evil_candidate = {
        **cross_slot["selectedCandidates"][1],
        "id": "B0EVIL",
        "amapId": "B0EVIL",
        "name": "越界社区市场",
        "longitude": 116.39,
        "latitude": 39.91,
        "planningSlotId": "slot-evil",
        "dayNumber": 3,
    }
    cross_slot["safeCandidates"].append(evil_candidate)
    cross_slot["selectedCandidates"].append(evil_candidate)
    evil_scope = {
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "planningSlotId": "slot-evil",
        "dayNumber": 3,
    }
    cross_slot["webDiscovery"]["attempts"].append(
        {
            "status": "grounded",
            "providerStatus": "success",
            "providerName": "recorded-web-search",
            "queryFingerprint": hashlib.sha256(b"attacker-added-slot").hexdigest(),
            "scope": evil_scope,
            "selectedCandidates": [
                {
                    "amapId": "B0EVIL",
                    "name": "越界社区市场",
                    "scope": evil_scope,
                }
            ],
        }
    )
    cross_slot_contract = checkpoint_semantic_contract_evidence(
        cross_slot,
        cross_slot["searchProfileTrace"],
    )
    assert cross_slot_contract
    cross_slot["searchProfileTrace"]["semanticContractEvidence"] = cross_slot_contract
    cross_slot["searchProfileTrace"]["semanticContractFingerprint"] = checkpoint_semantic_contract_fingerprint(
        cross_slot_contract
    )
    assert (
        discovery_service._checkpoint_profile_coverage_assessment(
            cross_slot,
            expected_checkpoint_binding=expected_binding,
        ).covered
        is False
    )
    cross_slot_result = discovery_service.augment(
        [cross_slot],
        reuse_existing_discovery=True,
        expected_checkpoint_profiles=expected_profiles,
    )
    assert cross_slot_result.metrics["reusedDiscoveryQueryCount"] == 0
    assert cross_slot_result.metrics["webQueryCount"] == 0
    assert cross_slot_result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")

    wrong_layer_reuse, wrong_layer_expected = service._portfolio_checkpoint_discovery_binding(
        grounding_report={
            "checkpointExpansionResume": True,
            "initialPlan": initial_plan.model_dump(by_alias=True),
            "pipelineContext": {
                key: value for key, value in pipeline_context.items() if key != "checkpointExpansionResume"
            },
        },
        itinerary_service=ItineraryService(connection),
    )
    assert wrong_layer_reuse is False
    assert wrong_layer_expected == {}
    connection.close()


def test_checkpoint_rejects_coordinated_semantic_contract_tamper_without_provider():
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-safe",
            briefId="brief-safe",
            planningSlotId="slot-safe",
            requirementLevel="optional",
            rawNeed="体验社区生活与传统市场",
            intentType="area_walk",
            optionalExperienceFamily="local_life",
            excludedPhysicalPoiIds=["B0OLD"],
            targetCount=1,
            maxQueries=4,
        )
    )
    original_candidate = {
        "id": "B0LOCAL001",
        "amapId": "B0LOCAL001",
        "name": "东四社区文化市场",
        "type": "购物服务;综合市场;农贸市场",
        "providerTypeCode": "060703",
        "tags": ["农贸市场", "社区商业"],
        "sourceClaims": [
            {
                "claimType": "local_life_context",
                "stance": "support",
                "summary": "面向附近居民的日常采购市场",
                "sourceUrlHash": "a" * 64,
            }
        ],
        "providerType": "购物服务;综合市场;农贸市场",
        "city": "北京",
        "longitude": 116.42,
        "latitude": 39.93,
        "source": "amap-place-search",
        "planningSlotId": "slot-safe",
        "dayNumber": 1,
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "semanticPassed": True,
        "semanticMatchScore": 0.9,
        "searchProfileFingerprint": profile.profileFingerprint,
        "exclusionFingerprint": profile.exclusionFingerprint,
        "experienceFamily": profile.experienceFamily,
        "activityMode": profile.activityMode,
    }
    checkpoint = AgentService._portfolio_grounding_checkpoint(
        {
            "poolReports": [
                {
                    "poolId": "pool-safe",
                    "briefId": "brief-safe",
                    "city": "北京",
                    "intentType": "area_walk",
                    "targetCount": 1,
                    "requirementLevel": "optional",
                    "optionalExperienceFamily": "local_life",
                    "entityBindingMode": profile.entityBindingMode,
                    "searchProfileFingerprint": profile.profileFingerprint,
                    "exclusionFingerprint": profile.exclusionFingerprint,
                    "experienceFamily": profile.experienceFamily,
                    "activityMode": profile.activityMode,
                    "searchProfile": profile.model_dump(by_alias=True),
                    "requiredSlotIds": ["slot-safe"],
                    "slotDayNumbers": {"slot-safe": 1},
                    "resolvedSlotIds": ["slot-safe"],
                    "unresolvedSlotIds": [],
                    "safeCandidates": [original_candidate],
                    "selectedCandidates": [original_candidate],
                    "webDiscovery": {
                        "status": "grounded",
                        "attempts": [],
                    },
                }
            ]
        }
    )
    tampered = json.loads(json.dumps(checkpoint["poolReports"][0]))
    tampered["optionalExperienceFamily"] = "heritage_walk"
    tampered["experienceFamily"] = "heritage"
    tampered["activityMode"] = "walk"
    tampered["searchProfileTrace"]["originalExperienceFamily"] = "heritage_walk"
    tampered["searchProfileTrace"]["experienceFamily"] = "heritage"
    tampered["searchProfileTrace"]["activityMode"] = "walk"
    heritage_candidate = {
        **original_candidate,
        "id": "B0HERITAGE",
        "amapId": "B0HERITAGE",
        "name": "南锣鼓巷历史文化街区",
        "type": "风景名胜;特色街区;历史文化街区",
        "providerType": "风景名胜;特色街区;历史文化街区",
        "longitude": 116.403,
        "latitude": 39.937,
        "experienceFamily": "heritage",
        "activityMode": "walk",
    }
    tampered["safeCandidates"] = [heritage_candidate]
    tampered["selectedCandidates"] = [heritage_candidate]
    attacker_contract = checkpoint_semantic_contract_evidence(
        tampered,
        tampered["searchProfileTrace"],
    )
    assert attacker_contract
    tampered["searchProfileTrace"]["semanticContractEvidence"] = attacker_contract
    tampered["searchProfileTrace"]["semanticContractFingerprint"] = checkpoint_semantic_contract_fingerprint(
        attacker_contract
    )

    class _NoDiscovery:
        max_web_queries = 1

        def discover(self, **_kwargs):
            raise AssertionError("an invalid checkpoint must fail closed before provider")

    service = PortfolioCandidateDiscoveryService(discovery_service=_NoDiscovery())
    expected_profile = profile.model_dump(by_alias=True)
    expected_binding = _expected_checkpoint_binding(expected_profile)
    expected_profiles = {("brief-safe", "pool-safe", "slot-safe"): expected_binding}
    assert (
        service._checkpoint_profile_coverage_assessment(
            tampered,
            expected_checkpoint_binding=expected_binding,
        ).covered
        is False
    )
    assert (
        service._can_reuse_discovery(
            tampered,
            expected_checkpoint_binding=expected_binding,
        )
        is False
    )

    result = service.augment(
        [tampered],
        reuse_existing_discovery=True,
        expected_checkpoint_profiles=expected_profiles,
    )

    assert result.metrics["webQueryCount"] == 0
    assert result.metrics["reusedDiscoveryQueryCount"] == 0
    assert result.pool_reports[0]["providerState"] == ("checkpoint_evidence_invalid")
    assert result.pool_reports[0]["safeCandidates"] == []
    assert result.pool_reports[0]["selectedCandidates"] == []
    assert result.pool_reports[0]["resolvedSlotIds"] == []
    assert result.pool_reports[0]["unresolvedSlotIds"] == ["slot-safe"]
    assert result.pool_reports[0]["coverageStatus"] == "unresolved"


def test_checkpoint_persists_typed_coverage_shortcut_evidence_only():
    sentinel = "PRIVATE_RAW_NEED token=secret https://example.test/provider C:/private/shortcut.json"
    checkpoint = project_pool_report_for_checkpoint(
        {
            "poolId": "pool-safe",
            "briefId": "brief-safe",
            "coverageShortcutEvidence": {
                "profileFingerprint": "a" * 64,
                "candidateCount": 3,
                "targetCount": 2,
                "evidenceTargetCount": 4,
                "excludedPhysicalPoiCount": 1,
                "rawNeed": sentinel,
                "providerUrl": sentinel,
                "nested": {
                    "credential": sentinel,
                    "candidateCount": 999,
                },
            },
        }
    )

    assert checkpoint["coverageShortcutEvidence"] == {
        "profileFingerprint": "a" * 64,
        "candidateCount": 3,
        "targetCount": 2,
        "evidenceTargetCount": 4,
        "excludedPhysicalPoiCount": 1,
    }
    serialized = json.dumps(checkpoint, ensure_ascii=False)
    for forbidden in (
        "PRIVATE_RAW_NEED",
        "token=secret",
        "example.test",
        "C:/private",
        "rawNeed",
        "providerUrl",
        "nested",
        "credential",
        "999",
    ):
        assert forbidden not in serialized


def test_grounding_persistence_boundaries_recursively_strip_sensitive_values():
    sentinel = (
        "PRIVATE_RAW_NEED token=secret https://example.test/provider?credential=hidden C:/private/checkpoint.json"
    )
    candidate = {
        "id": "B0SAFE001",
        "amapId": "B0SAFE001",
        "name": "东四社区文化市场",
        "type": "购物服务;综合市场;农贸市场",
        "providerTypeCode": "060703",
        "tags": ["农贸市场", "社区商业"],
        "sourceClaims": [
            {
                "claimType": "local_life_context",
                "stance": "support",
                "summary": "面向附近居民的日常采购市场",
                "sourceUrlHash": "a" * 64,
            }
        ],
        "providerType": "购物服务;综合市场;农贸市场",
        "city": "北京",
        "longitude": 116.42,
        "latitude": 39.93,
        "source": "amap-place-search",
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "planningSlotId": "slot-safe",
        "dayNumber": 1,
        "sourceNote": sentinel,
        "source_note": sentinel,
        "providerPayload": {"authorization": sentinel},
        "discoveryProvenance": {"url": sentinel},
    }
    poisoned = {
        "resultState": "needs_confirmation",
        "routeStatus": "waiting_for_poi_grounding",
        "warnings": ["checkpoint_profile_evidence_invalid", sentinel],
        "unresolvedSlots": [
            {
                "briefId": "brief-safe",
                "poolId": "pool-safe",
                "planningSlotId": "slot-safe",
                "dayNumber": 1,
                "reasonCode": "profile_coverage_missing",
                "rawNeed": sentinel,
                "query": sentinel,
                "providerPayload": {"token": sentinel},
            }
        ],
        "planningPreview": {
            "dayCount": 1,
            "rawNeed": sentinel,
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "poi": candidate,
                            "providerPayload": sentinel,
                        }
                    ],
                }
            ],
        },
        "routeRepairGroundingEvidence": [
            {
                **candidate,
                "query": sentinel,
            }
        ],
        "partialAnchorGroundingAudit": {
            "partialEligible": False,
            "anchorCount": 1,
            "reasonCode": "partial_anchor_scope_mismatch",
            "providerPayload": sentinel,
            "matchedAnchors": [candidate],
        },
        "partialAnchorGroundingProjectionAudit": {
            "matchedAnchorCount": 1,
            "discoveryProvenance": {"url": sentinel},
        },
        "pipelineContext": {
            "briefId": "brief-safe",
            "poolId": "pool-safe",
            "rawNeed": sentinel,
            "query": sentinel,
        },
        "performance": {
            "candidateGroundingMs": 12.5,
            "providerPayload": sentinel,
        },
        "portfolioStagingPerformance": {
            "candidateCount": 1,
            "providerPayload": sentinel,
        },
        "poolReports": [
            {
                "poolId": "pool-safe",
                "briefId": "brief-safe",
                "queryPlanIds": ["plan-safe", sentinel],
                "queryPlanModes": ["exact", sentinel],
                "queryProviderKeys": ["market", sentinel],
                "selectedCanonicalEntities": [
                    "B0SAFE001",
                    {"providerPayload": sentinel},
                ],
                "selectedMealFamilies": ["local", sentinel],
                "rejectedReasonCounts": {
                    "semantic_mismatch": 1,
                    "providerPayload": sentinel,
                },
                "amapCallBudget": {
                    "used": 1,
                    "providerPayload": sentinel,
                },
                "providerDebug": [
                    {
                        "status": "success",
                        "source": "place/text",
                        "providerPayload": sentinel,
                    }
                ],
                "webDiscovery": {
                    "status": "grounded",
                    "failureReason": sentinel,
                    "webQueryCount": {"providerPayload": sentinel},
                    "searchProfileFingerprint": {"providerPayload": sentinel},
                    "attempts": [
                        {
                            "status": "grounded",
                            "providerStatus": "success",
                            "providerName": sentinel,
                            "query": sentinel,
                            "providerPayload": sentinel,
                        }
                    ],
                },
                "safeCandidates": [candidate],
                "selectedCandidates": [candidate],
            }
        ],
    }

    persisted = project_grounding_report_for_persistence(poisoned)
    checkpoint = AgentService._portfolio_grounding_checkpoint(poisoned)
    trace = project_pool_report_for_trace(poisoned["poolReports"][0])
    serialized = json.dumps(
        {
            "persisted": persisted,
            "checkpoint": checkpoint,
            "trace": trace,
        },
        ensure_ascii=False,
    )

    for forbidden in (
        "PRIVATE_RAW_NEED",
        "example.test",
        "credential=hidden",
        "C:/private",
        "token=secret",
        "providerPayload",
        "discoveryProvenance",
        "sourceNote",
        "source_note",
        "rawNeed",
        '"query"',
    ):
        assert forbidden not in serialized
    assert persisted["resultState"] == "needs_confirmation"
    assert persisted["unresolvedSlots"][0] == {
        "briefId": "brief-safe",
        "poolId": "pool-safe",
        "planningSlotId": "slot-safe",
        "dayNumber": 1,
        "reasonCode": "profile_coverage_missing",
    }
    assert persisted["routeRepairGroundingEvidence"][0]["amapId"] == ("B0SAFE001")
    assert checkpoint["poolReports"][0]["selectedCandidates"][0]["amapId"] == "B0SAFE001"
    assert trace["providerDebug"][0]["source"] == "place/text"
    for removed_container in (
        "queryPlanIds",
        "queryPlanModes",
        "queryProviderKeys",
        "selectedCanonicalEntities",
        "selectedMealFamilies",
        "rejectedReasonCounts",
        "amapCallBudget",
        "providerDebug",
    ):
        assert removed_container not in checkpoint["poolReports"][0]

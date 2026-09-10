from __future__ import annotations

import json
from hashlib import sha256

from src.models.poi_search_profile import ExperienceSemanticInput
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
)
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.poi_discovery_service import PoiDiscoveryResult
from src.services.portfolio_candidate_discovery_service import (
    PortfolioCandidateDiscoveryService,
)


def _candidate(amap_id: str, name: str) -> dict:
    return {
        "amapId": amap_id,
        "id": amap_id,
        "name": name,
        "city": "北京",
        "providerType": "科教文化服务;学校;高等院校",
        "source": "amap-place-search",
        "longitude": 116.3,
        "latitude": 39.9,
        "confidence": 0.95,
        "semanticPassed": True,
    }


def _report(brief_id: str, slot_id: str, day_number: int) -> dict:
    return {
        "city": "北京",
        "briefId": brief_id,
        "poolId": f"{brief_id}-campus",
        "intentType": "campus_visit",
        "rawNeed": "北京高校",
        "entityBindingMode": "category",
        "exactEntity": None,
        "requirementLevel": "required",
        "goalId": "goal-campus",
        "requiredSlotIds": [slot_id],
        "unresolvedSlotIds": [slot_id],
        "slotDayNumbers": {slot_id: day_number},
        "safeCandidates": [_candidate("direct", "北京大学")],
    }


class RecordedDiscovery:
    def __init__(self, result: PoiDiscoveryResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def discover(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class LegacyRecordedDiscovery:
    def __init__(self, result: PoiDiscoveryResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def discover(
        self,
        *,
        city: str,
        intent_type: str,
        raw_need: str,
        trigger_reason: str,
    ) -> PoiDiscoveryResult:
        self.calls.append(
            {
                "city": city,
                "intent_type": intent_type,
                "raw_need": raw_need,
                "trigger_reason": trigger_reason,
            }
        )
        return self.result


def test_default_portfolio_discovery_allows_two_bounded_web_groups():
    service = PortfolioCandidateDiscoveryService()

    assert service.discovery_service.max_web_queries == 2
    assert service.discovery_service.max_amap_seed_queries == 2
    assert service.discovery_service.web_search_provider.total_deadline_seconds == 10.0


def test_always_on_web_discovery_isolates_day_and_brief_scope_before_rebinding():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[_candidate("web-grounded", "清华大学")],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            webDurationMs=12.5,
            amapGroundingMs=20.25,
        )
    )
    service = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
        semantic_policy=IntentCandidateSemanticPolicy(),
    )

    result = service.augment(
        [
            _report("brief-a", "day-1-campus", 1),
            _report("brief-b", "day-2-campus", 2),
        ]
    )

    assert len(discovery.calls) == 2
    assert result.metrics["uniqueDiscoveryQueryCount"] == 2
    assert result.metrics["webQueryCount"] == 2
    assert result.metrics["groundedWebCandidateCount"] == 1
    assert result.metrics["webOnlyFinalPoiCount"] == 0
    assert result.metrics["candidateDiscoveryMs"] >= 0
    assert result.metrics["webDiscoveryMs"] == 25.0
    assert result.metrics["amapGroundingMs"] == 40.5
    assert sum(item["webDiscoveryMs"] for item in result.metrics["discoveryByBrief"]) == 25.0
    assert sum(item["amapGroundingMs"] for item in result.metrics["discoveryByBrief"]) == 40.5
    for report in result.pool_reports:
        rebound = next(item for item in report["safeCandidates"] if item["amapId"] == "web-grounded")
        assert rebound["briefId"] == report["briefId"]
        assert rebound["poolId"] == report["poolId"]
        assert rebound["planningSlotId"] == report["requiredSlotIds"][0]
        assert rebound["dayNumber"] == report["slotDayNumbers"][rebound["planningSlotId"]]


def test_discovery_demand_key_binds_occurrence_route_evidence_and_contract_scope():
    service = PortfolioCandidateDiscoveryService()
    base = _report("brief-a", "day-1-night", 1)
    base.update(
        {
            "intentType": "night_view",
            "rawNeed": "公共户外夜景",
            "occurrenceId": "occ:night:day:1",
            "routeContext": {
                "corridorHash": "corridor-a",
                "transportMode": "transit",
                "contractVersion": 2,
                "specFingerprint": "a" * 64,
            },
            "evidencePolicy": {"maxAgeHours": 24},
        }
    )
    exact_same = json.loads(json.dumps(base))
    changed_day = json.loads(json.dumps(base))
    changed_day.update(
        {
            "requiredSlotIds": ["day-2-night"],
            "unresolvedSlotIds": ["day-2-night"],
            "slotDayNumbers": {"day-2-night": 2},
            "occurrenceId": "occ:night:day:2",
        }
    )
    changed_corridor = json.loads(json.dumps(base))
    changed_corridor["routeContext"]["corridorHash"] = "corridor-b"
    changed_transport = json.loads(json.dumps(base))
    changed_transport["routeContext"]["transportMode"] = "walking"
    changed_evidence = json.loads(json.dumps(base))
    changed_evidence["evidencePolicy"]["maxAgeHours"] = 6
    changed_contract = json.loads(json.dumps(base))
    changed_contract["routeContext"]["contractVersion"] = 3

    base_key = service._demand_key(base)

    assert service._demand_key(exact_same) == base_key
    assert (
        len(
            {
                service._demand_key(item)
                for item in (
                    changed_day,
                    changed_corridor,
                    changed_transport,
                    changed_evidence,
                    changed_contract,
                )
            }
        )
        == 5
    )
    assert all(
        service._demand_key(item) != base_key
        for item in (
            changed_day,
            changed_corridor,
            changed_transport,
            changed_evidence,
            changed_contract,
        )
    )


def test_hard_gap_discovery_receives_concrete_hints_and_selected_entity_exclusions():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="unresolved",
            webQueryCount=1,
            failureReason="web_discovery_no_entity_seed",
        )
    )
    service = PortfolioCandidateDiscoveryService(discovery_service=discovery)
    report = {
        "city": "北京",
        "briefId": "fallback-photo-night",
        "poolId": "night-view-pool",
        "intentType": "night_view",
        "rawNeed": "晚上看城市夜景",
        "requirementLevel": "required",
        "goalId": "goal-night-view",
        "targetCount": 2,
        "requiredSlotIds": ["day-1-night", "day-2-night"],
        "resolvedSlotIds": ["day-1-night"],
        "unresolvedSlotIds": ["day-2-night"],
        "slotDayNumbers": {"day-1-night": 1, "day-2-night": 2},
        "candidateHints": ["中央广播电视塔", "奥林匹克塔", "景山公园 观景"],
        "selectedCandidates": [
            {
                "amapId": "B0TVTOWER",
                "name": "中央广播电视塔",
                "source": "amap-place-search",
            }
        ],
        "safeCandidates": [],
    }

    service.augment([report])

    call = discovery.calls[0]
    assert call["candidate_hints"] == report["candidateHints"]
    assert call["excluded_candidate_names"] == ["中央广播电视塔"]
    assert isinstance(call["query_variant_index"], int)


def test_exact_trusted_amap_slot_coverage_skips_web_without_reuse_flag():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    report = _report("brief-covered", "day-1-campus", 1)
    report["resolvedSlotIds"] = ["day-1-campus"]
    report["unresolvedSlotIds"] = []
    report["selectedCandidates"] = [
        {
            **_candidate("B000CAMP1", "北京大学"),
            "type": "科教文化服务;学校;高等院校",
            "providerTypeCode": "141201",
            "tags": ["高等院校"],
            "briefId": "brief-covered",
            "poolId": "brief-covered-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
        }
    ]

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert discovery.calls == []
    assert result.metrics["uniqueDiscoveryQueryCount"] == 0
    assert result.metrics["webQueryCount"] == 0
    assert result.metrics["webSearchSkippedReasonCodes"] == ["trusted_amap_slot_coverage_already_resolved"]
    assert result.pool_reports[0]["webDiscovery"] == {
        "status": "skipped",
        "webQueryCount": 0,
        "webSeedCount": 0,
        "webSeedAmapGroundingCount": 0,
        "webDiscoveryMs": 0.0,
        "amapGroundingMs": 0.0,
        "reasonCode": "trusted_amap_slot_coverage_already_resolved",
        "attempts": [],
    }


def test_scoped_density_retry_bypasses_covered_slot_and_cached_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    report = _report("brief-covered", "day-2-night", 2)
    report.update(
        {
            "intentType": "night_view",
            "rawNeed": "城市夜景",
            "candidateHints": ["城市夜景观景点"],
            "resolvedSlotIds": ["day-2-night"],
            "unresolvedSlotIds": [],
            "forceCandidateDiscovery": True,
            "selectedCandidates": [
                {
                    **_candidate("B000NIGHT1", "城市阳台"),
                    "type": "风景名胜;观景点",
                    "providerTypeCode": "110200",
                    "tags": ["观景平台", "城市夜景"],
                    "openTimeToday": "18:00-23:00",
                    "briefId": "brief-covered",
                    "poolId": "brief-covered-campus",
                    "planningSlotId": "day-2-night",
                    "dayNumber": 2,
                }
            ],
            "webDiscovery": {
                "status": "grounded",
                "attempts": [],
            },
        }
    )

    result = PortfolioCandidateDiscoveryService(discovery_service=discovery).augment(
        [report],
        reuse_existing_discovery=True,
    )

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["excluded_candidate_names"] == ["城市阳台"]
    assert result.metrics["uniqueDiscoveryQueryCount"] == 1
    assert "checkpoint_web_discovery_reused" not in result.metrics["webSearchSkippedReasonCodes"]
    assert "trusted_amap_slot_coverage_already_resolved" not in result.metrics["webSearchSkippedReasonCodes"]


def test_semantic_only_selected_candidate_does_not_stop_bounded_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    report = _report("brief-meal", "day-1-meal", 1)
    report.update(
        {
            "poolId": "brief-meal-pool",
            "intentType": "meal",
            "rawNeed": "当地特色美食",
            "requirementLevel": "explicit_soft",
            "resolvedSlotIds": ["day-1-meal"],
            "unresolvedSlotIds": [],
            "selectedCandidates": [
                {
                    **_candidate("BMEAL0001", "四季民福烤鸭店(故宫店)"),
                    "providerType": "餐饮服务;中餐厅",
                    "providerTypeCode": "050100",
                    "tags": [],
                    "sourceClaims": [],
                    "briefId": "brief-meal",
                    "poolId": "brief-meal-pool",
                    "planningSlotId": "day-1-meal",
                    "dayNumber": 1,
                }
            ],
        }
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["evidence_candidate_names"] == ["四季民福烤鸭店(故宫店)"]
    assert discovery.calls[0]["excluded_candidate_names"] == []
    assert result.metrics["profileCoverageShortcutHitCount"] == 0
    assert result.metrics["consumerAdmissionCoverageRejectedCount"] == 1
    assert (
        PortfolioCandidateDiscoveryService(
            discovery_service=discovery,
        ).planning_event_preview(result.metrics)["consumerAdmissionCoverageRejectedCount"]
        == 1
    )
    assert result.pool_reports[0]["candidateRejections"] == [
        {
            "candidateId": "BMEAL0001",
            "stage": "consumer_admission_coverage",
            "reasonCode": "structured_provider_evidence_missing",
        }
    ]


def test_exact_scope_rejects_explicit_conflicting_source_scope_fields():
    service = PortfolioCandidateDiscoveryService(
        discovery_service=RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    )
    report = _report("brief-covered", "day-1-campus", 1)
    candidate = {
        **_candidate("B000CAMP1", "北京大学"),
        "briefId": "brief-covered",
        "poolId": "brief-covered-campus",
        "planningSlotId": "day-1-campus",
        "dayNumber": 1,
    }

    assert service._candidate_has_exact_scope(candidate, report) is True
    for source_scope_field in (
        "sourceBriefId",
        "sourcePoolId",
        "sourcePlanningSlotId",
    ):
        conflicting_candidate = {
            **candidate,
            source_scope_field: "conflicting-source-scope",
        }
        assert service._candidate_has_exact_scope(conflicting_candidate, report) is False
    assert (
        service._candidate_has_exact_scope(
            {
                **candidate,
                "sourceBriefId": "conflicting source scope",
            },
            report,
        )
        is False
    )
    numeric_source_values = {
        "sourceBriefId": 123,
        "sourcePoolId": 456,
        "sourcePlanningSlotId": 789,
    }
    numeric_candidate = {
        **candidate,
        "briefId": "123",
        "poolId": "456",
        "planningSlotId": "789",
        "dayNumber": 1,
        **numeric_source_values,
    }
    numeric_report = _report("123", "789", 1)
    numeric_report["poolId"] = "456"
    assert service._candidate_has_exact_scope(numeric_candidate, numeric_report) is False


def test_legacy_coverage_does_not_skip_conflicting_source_scope():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    report = _report("brief-covered", "day-1-campus", 1)
    report["resolvedSlotIds"] = ["day-1-campus"]
    report["unresolvedSlotIds"] = []
    report["selectedCandidates"] = [
        {
            **_candidate("B000CAMP1", "北京大学"),
            "briefId": "brief-covered",
            "poolId": "brief-covered-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
            "sourceBriefId": "conflicting-source-brief",
        }
    ]

    result = PortfolioCandidateDiscoveryService(discovery_service=discovery).augment([report])

    assert len(discovery.calls) == 1
    assert "trusted_amap_slot_coverage_already_resolved" not in result.metrics["webSearchSkippedReasonCodes"]


def _family_search_profile(
    *,
    family: str,
    activity_mode: str,
    fingerprint: str,
    excluded_ids: list[str] | None = None,
) -> dict:
    return {
        "schemaVersion": "1.0",
        "profileId": f"profile-{family}",
        "profileFingerprint": fingerprint,
        "experienceFamily": family,
        "activityMode": activity_mode,
        "excludedPhysicalPoiIds": list(excluded_ids or []),
        "exclusionFingerprint": (f"excluded-{family}" if excluded_ids else "excluded-none"),
        "coveragePolicy": {
            "targetCount": 1,
            "evidenceTargetCount": 1,
            "distinctPhysicalPoiRequired": True,
            "stopWhenTargetReached": True,
        },
        "queryPlans": [
            {
                "planId": f"{family}-text",
                "priority": 10,
                "mode": "amap_text",
                "keyword": family,
                "keywordVariants": [family],
                "providerCategoryKeys": [family],
                "preferredTypeGroups": [family],
                "rejectedTypeGroups": [],
                "anchorPolicy": "none",
                "radiusMeters": None,
                "resultLimit": 8,
                "fallbackLevel": 1,
                "requiresAmapGrounding": True,
                "stopWhenTargetReached": True,
            }
        ],
    }


def _resolved_family_report(
    *,
    optional_family: str,
    experience_family: str,
    activity_mode: str,
    fingerprint: str,
    candidate: dict,
    excluded_ids: list[str] | None = None,
) -> dict:
    report = _report(f"brief-{optional_family}", "day-1-flex", 1)
    report.update(
        {
            "poolId": f"brief-{optional_family}-flex",
            "intentType": "area_walk",
            "rawNeed": optional_family,
            "requirementLevel": "optional",
            "optionalExperienceFamily": optional_family,
            "resolvedSlotIds": ["day-1-flex"],
            "unresolvedSlotIds": [],
            "selectedCandidates": [
                {
                    **candidate,
                    "briefId": f"brief-{optional_family}",
                    "poolId": f"brief-{optional_family}-flex",
                    "planningSlotId": "day-1-flex",
                    "dayNumber": 1,
                }
            ],
            "searchProfile": _family_search_profile(
                family=experience_family,
                activity_mode=activity_mode,
                fingerprint=fingerprint,
                excluded_ids=excluded_ids,
            ),
            "searchProfileFingerprint": fingerprint,
            "exclusionFingerprint": (f"excluded-{experience_family}" if excluded_ids else "excluded-none"),
        }
    )
    return report


def test_named_area_candidate_rejected_only_for_evidence_gets_entity_specific_lookup():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    candidate = {
        **_candidate("B0K6VHYJ41", "模式口历史文化街区"),
        "type": "风景名胜;风景名胜相关;旅游景点",
        "providerType": "风景名胜;风景名胜相关;旅游景点",
        "providerTypeCode": "110200",
        "tags": ["历史文化街区", "老街"],
        "sourceClaims": [],
        "searchProfileFingerprint": "fp-heritage-evidence-repair",
        "experienceFamily": "heritage_walk",
        "activityMode": "walk",
        "semanticPassed": True,
        "semanticMatchScore": 0.94,
    }
    report = _resolved_family_report(
        optional_family="heritage_walk",
        experience_family="heritage_walk",
        activity_mode="walk",
        fingerprint="fp-heritage-evidence-repair",
        candidate=candidate,
    )
    report.update(
        {
            "experienceShape": "area",
            "experienceGoal": "在历史街区步行观察传统建筑与街巷",
            "desiredSignals": ["历史", "老街", "传统建筑"],
            "evidenceRequirements": {"minimumIndependentClaims": 1},
        }
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert discovery.calls[0]["evidence_candidate_names"] == ["模式口历史文化街区"]
    assert discovery.calls[0]["excluded_candidate_names"] == []
    assert result.pool_reports[0]["candidateRejections"] == [
        {
            "candidateId": "B0K6VHYJ41",
            "stage": "consumer_admission_coverage",
            "reasonCode": "consumer_evidence_insufficient",
        }
    ]


def test_evidence_repair_merges_same_amap_identity_without_scope_or_lineage_drift():
    profile_fingerprint = "fp-heritage-same-identity-repair"
    query_fingerprint = sha256("北京 模式口历史文化街区 历史步行".encode("utf-8")).hexdigest()
    first_claim = {
        "claimKey": "heritage_walk",
        "stance": "support",
        "sourceType": "web_snippet",
        "sourceName": "recorded-web-a",
        "sourceUrlHash": "a" * 64,
        "summary": "历史街区保留传统建筑与街巷格局",
    }
    second_claim = {
        "claimKey": "heritage_context",
        "stance": "support",
        "sourceType": "web_snippet",
        "sourceName": "recorded-web-b",
        "sourceUrlHash": "b" * 64,
        "summary": "可沿老街步行观察传统建筑",
    }
    direct = {
        **_candidate("B0K6VHYJ41", "模式口历史文化街区"),
        "type": "风景名胜;风景名胜相关;旅游景点",
        "providerType": "风景名胜;风景名胜相关;旅游景点",
        "providerTypeCode": "110200",
        "tags": ["历史文化街区", "老街"],
        "sourceClaims": [first_claim],
        "searchProfileFingerprint": profile_fingerprint,
        "exclusionFingerprint": "excluded-none",
        "experienceFamily": "heritage_walk",
        "activityMode": "walk",
        "briefId": "brief-heritage_walk",
        "poolId": "brief-heritage_walk-flex",
        "planningSlotId": "day-1-flex",
        "dayNumber": 1,
        "sourceGoalId": "goal-heritage",
        "consumerAdmissionInput": {"planningSlotId": "stale-slot"},
        "consumerAdmissionReport": {
            "consumerFingerprint": "stale-consumer",
            "scoreEligible": False,
        },
        "scoreEligible": False,
    }
    repaired = {
        **direct,
        "sourceClaims": [first_claim, second_claim],
        "candidateSource": "web_seed_amap_grounded",
        "searchProfileId": "profile-heritage_walk",
        "queryPlanId": "heritage_walk-text",
        "sourceBriefId": "brief-heritage_walk",
        "sourcePoolId": "brief-heritage_walk-flex",
        "sourcePlanningSlotId": "day-1-flex",
        "discoveryProvenance": {
            "triggerReason": "portfolio_always_on_candidate_discovery",
            "webQueryFingerprint": query_fingerprint,
            "amapQueryFingerprint": "c" * 64,
            "amapId": "B0K6VHYJ41",
            "mapProvider": "amap-place-search",
        },
    }
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[repaired],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            discoveryEvidence=[
                {
                    "queryFingerprint": query_fingerprint,
                    "providerName": "recorded-web",
                    "providerStatus": "success",
                    "status": "grounded",
                    "resultCount": 1,
                    "seedCount": 1,
                }
            ],
        )
    )
    report = _resolved_family_report(
        optional_family="heritage_walk",
        experience_family="heritage_walk",
        activity_mode="walk",
        fingerprint=profile_fingerprint,
        candidate=direct,
    )
    report.update(
        {
            "goalId": "goal-heritage",
            "safeCandidates": [direct],
            "experienceShape": "area",
            "experienceGoal": "在历史街区步行观察传统建筑与街巷",
            "desiredSignals": ["历史", "老街", "传统建筑"],
            "evidenceRequirements": {"minimumIndependentClaims": 2},
        }
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    candidates = [item for item in result.pool_reports[0]["safeCandidates"] if item.get("amapId") == "B0K6VHYJ41"]
    assert len(candidates) == 1
    merged = candidates[0]
    assert merged["sourceClaims"] == [first_claim, second_claim]
    assert {
        key: merged[key]
        for key in (
            "briefId",
            "poolId",
            "planningSlotId",
            "dayNumber",
            "sourceGoalId",
            "sourceBriefId",
            "sourcePoolId",
            "sourcePlanningSlotId",
        )
    } == {
        "briefId": "brief-heritage_walk",
        "poolId": "brief-heritage_walk-flex",
        "planningSlotId": "day-1-flex",
        "dayNumber": 1,
        "sourceGoalId": "goal-heritage",
        "sourceBriefId": "brief-heritage_walk",
        "sourcePoolId": "brief-heritage_walk-flex",
        "sourcePlanningSlotId": "day-1-flex",
    }
    assert merged["source"] == "amap-place-search"
    assert merged["amapId"] == merged["id"] == "B0K6VHYJ41"
    assert merged["searchProfileFingerprint"] == profile_fingerprint
    assert merged["queryPlanId"] == "heritage_walk-text"
    assert merged["discoveryProvenance"]["webQueryFingerprint"] == (query_fingerprint)
    assert merged["discoveryProvenance"]["amapId"] == "B0K6VHYJ41"
    for stale_key in (
        "consumerAdmissionInput",
        "consumerAdmissionReport",
        "scoreEligible",
    ):
        assert stale_key not in merged


def test_evidence_repair_rejects_forged_profile_and_query_plan_lineage():
    profile_fingerprint = "fp-heritage-lineage-guard"
    query_fingerprint = sha256(b"recorded heritage evidence").hexdigest()
    direct = {
        **_candidate("B0K6VHYJ41", "模式口历史文化街区"),
        "type": "风景名胜;风景名胜相关;旅游景点",
        "providerType": "风景名胜;风景名胜相关;旅游景点",
        "providerTypeCode": "110200",
        "tags": ["历史文化街区", "老街"],
        "searchProfileFingerprint": profile_fingerprint,
        "exclusionFingerprint": "excluded-none",
        "experienceFamily": "heritage_walk",
        "activityMode": "walk",
        "briefId": "brief-heritage_walk",
        "poolId": "brief-heritage_walk-flex",
        "planningSlotId": "day-1-flex",
        "dayNumber": 1,
    }
    evidence = {
        "sourceClaims": [
            {
                "claimKey": "heritage_walk",
                "stance": "support",
                "sourceUrlHash": "d" * 64,
                "summary": "不得移植的网页证据",
            }
        ],
        "candidateSource": "web_seed_amap_grounded",
        "searchProfileId": "profile-heritage_walk",
        "sourceBriefId": "brief-heritage_walk",
        "sourcePoolId": "brief-heritage_walk-flex",
        "sourcePlanningSlotId": "day-1-flex",
        "discoveryProvenance": {
            "webQueryFingerprint": query_fingerprint,
            "amapId": "B0K6VHYJ41",
        },
    }
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[
                {
                    **direct,
                    **evidence,
                    "searchProfileFingerprint": "forged-profile",
                    "queryPlanId": "heritage_walk-text",
                },
                {
                    **direct,
                    **evidence,
                    "searchProfileFingerprint": profile_fingerprint,
                    "queryPlanId": "forged-query-plan",
                },
            ],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            discoveryEvidence=[
                {
                    "queryFingerprint": query_fingerprint,
                    "providerName": "recorded-web",
                    "providerStatus": "success",
                    "status": "grounded",
                }
            ],
        )
    )
    report = _resolved_family_report(
        optional_family="heritage_walk",
        experience_family="heritage_walk",
        activity_mode="walk",
        fingerprint=profile_fingerprint,
        candidate=direct,
    )
    report.update(
        {
            "safeCandidates": [direct],
            "experienceShape": "area",
            "evidenceRequirements": {"minimumIndependentClaims": 1},
        }
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    candidates = [item for item in result.pool_reports[0]["safeCandidates"] if item.get("amapId") == "B0K6VHYJ41"]
    assert len(candidates) == 1
    assert "sourceClaims" not in candidates[0]
    assert "candidateSource" not in candidates[0]


def test_generic_scenic_candidate_does_not_cover_local_life_profile():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    generic = {
        **_candidate("generic-scenic", "城市热门景点"),
        "providerType": "风景名胜;风景名胜;观景点",
        "semanticPassed": True,
    }
    report = _resolved_family_report(
        optional_family="local_life",
        experience_family="local_life",
        activity_mode="observe_walk",
        fingerprint="fp-local-life",
        candidate=generic,
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["search_profile"]["profileFingerprint"] == ("fp-local-life")
    assert result.metrics["profileCoverageShortcutHitCount"] == 0
    assert result.metrics["invalidCoverageShortcutCount"] == 1
    assert result.metrics["semanticCandidateRejectedCount"] == 1
    assert result.metrics["routePreflightAvoidedBySemanticFilterCount"] == 1
    assert result.pool_reports[0]["webDiscovery"]["reasonCode"] != "trusted_amap_slot_coverage_already_resolved"


def test_generic_scenic_candidate_does_not_cover_market_walk_profile():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    generic = {
        **_candidate("generic-scenic", "著名观光景区"),
        "providerType": "风景名胜;风景名胜;观景点",
        "semanticPassed": True,
    }
    report = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market",
        candidate=generic,
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["search_profile"]["experienceFamily"] == "market"
    assert result.metrics["profileCoverageShortcutHitCount"] == 0
    assert result.metrics["invalidCoverageShortcutCount"] == 1


def test_two_optional_experience_gaps_share_the_bounded_two_query_round():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    discovery.max_web_queries = 2
    local_life = _resolved_family_report(
        optional_family="local_life",
        experience_family="local_life",
        activity_mode="observe_walk",
        fingerprint="fp-local-two-query",
        candidate={
            **_candidate("B0LOCALGEN", "城市热门景点"),
            "providerType": "风景名胜;风景名胜;观景点",
        },
    )
    market = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market-two-query",
        candidate={
            **_candidate("B0MARKETGEN", "著名观光景区"),
            "providerType": "风景名胜;风景名胜;观景点",
        },
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([local_life, market])

    assert len(discovery.calls) == 2
    assert result.metrics["uniqueDiscoveryQueryCount"] == 2
    assert result.metrics["webQueryCount"] == 2
    assert "poi_discovery_budget_exhausted" not in result.metrics["reasonCodes"]


def test_matching_profile_unexcluded_semantic_candidates_allow_safe_coverage_shortcut():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    market = {
        **_candidate("B0MARKET1", "三源里菜市场"),
        "type": "购物服务;综合市场;农副产品市场",
        "providerTypeCode": "060702",
        "tags": ["农副产品市场", "菜市场"],
        "sourceClaims": [
            {
                "claimType": "market_activity",
                "stance": "support",
                "summary": "面向附近居民的日常采购市场",
                "sourceUrlHash": "a" * 64,
            }
        ],
        "providerType": "购物服务;综合市场;农副产品市场",
        "searchProfileFingerprint": "fp-market",
        "experienceFamily": "market",
        "activityMode": "walk_eat",
        "semanticPassed": True,
        "semanticMatchScore": 0.92,
    }
    report = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market",
        candidate=market,
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert discovery.calls == []
    assert result.metrics["profileCoverageShortcutHitCount"] == 1
    assert result.metrics["invalidCoverageShortcutCount"] == 0
    assert result.metrics["semanticCandidateAcceptedCount"] == 1
    assert result.metrics["familySpecificAmapCandidateCount"] == 1
    assert result.pool_reports[0]["coverageShortcutEvidence"] == {
        "profileFingerprint": "fp-market",
        "candidateCount": 1,
        "targetCount": 1,
        "evidenceTargetCount": 1,
        "excludedPhysicalPoiCount": 0,
    }


def test_excluded_matching_profile_candidate_forces_new_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    market = {
        **_candidate("B0MARKET1", "三源里菜市场"),
        "providerType": "购物服务;综合市场;农副产品市场",
        "searchProfileFingerprint": "fp-market",
        "experienceFamily": "market",
        "activityMode": "walk_eat",
        "semanticPassed": True,
        "semanticMatchScore": 0.92,
    }
    report = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market",
        candidate=market,
        excluded_ids=["B0MARKET1"],
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert len(discovery.calls) == 1
    assert result.metrics["profileCoverageShortcutHitCount"] == 0
    assert result.metrics["invalidCoverageShortcutCount"] == 1
    assert result.metrics["duplicateExcludedBeforeRouteCount"] >= 1
    assert all(
        item.get("amapId") != "B0MARKET1"
        for key in ("safeCandidates", "selectedCandidates", "topCandidates")
        for item in result.pool_reports[0].get(key) or []
        if isinstance(item, dict)
    )


def test_profile_coverage_requires_maximum_target_and_evidence_count():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    market = {
        **_candidate("B0MARKET1", "三源里菜市场"),
        "providerType": "购物服务;综合市场;农副产品市场",
        "searchProfileFingerprint": "fp-market",
        "experienceFamily": "market",
        "activityMode": "walk_eat",
        "semanticPassed": True,
        "semanticMatchScore": 0.92,
    }
    report = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market",
        candidate=market,
    )
    report["searchProfile"]["coveragePolicy"].update({"targetCount": 1, "evidenceTargetCount": 2})

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert len(discovery.calls) == 1
    assert result.metrics["profileCoverageShortcutHitCount"] == 0
    assert result.metrics["invalidCoverageShortcutCount"] == 1


def test_same_legacy_need_with_different_profiles_does_not_share_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    local_life = _resolved_family_report(
        optional_family="local_life",
        experience_family="local_life",
        activity_mode="observe_walk",
        fingerprint="fp-local-life",
        candidate={
            **_candidate("local-generic", "城市热门景点"),
            "providerType": "风景名胜;风景名胜;观景点",
        },
    )
    market = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market",
        candidate={
            **_candidate("market-generic", "城市热门景点"),
            "providerType": "风景名胜;风景名胜;观景点",
        },
    )
    local_life["rawNeed"] = "城市漫步"
    market["rawNeed"] = "城市漫步"

    PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([local_life, market])

    assert len(discovery.calls) == 2
    assert {call["search_profile"]["profileFingerprint"] for call in discovery.calls} == {"fp-local-life", "fp-market"}


def test_profile_discovery_keeps_legacy_discovery_signature_compatible():
    discovery = LegacyRecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
    report = _resolved_family_report(
        optional_family="market_walk",
        experience_family="market",
        activity_mode="walk_eat",
        fingerprint="fp-market",
        candidate={
            **_candidate("generic-scenic", "城市热门景点"),
            "providerType": "风景名胜;风景名胜;观景点",
        },
    )

    PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert discovery.calls == [
        {
            "city": "北京",
            "intent_type": "area_walk",
            "raw_need": "market_walk",
            "trigger_reason": "portfolio_always_on_candidate_discovery",
        }
    ]


def test_unsupported_creative_profiles_skip_discovery_without_using_budget():
    compiler = ExperienceSearchProfileCompiler()
    cases = (
        (
            "tea_ceremony_walk",
            "area_walk",
            "creative_optional_family_unregistered",
            False,
        ),
        (
            "art_walk",
            "future_intent",
            "creative_optional_intent_unregistered",
            True,
        ),
    )

    for optional_family, intent_type, reason_code, use_legacy_service in cases:
        report = _report(
            f"brief-{optional_family}",
            f"slot-{optional_family}",
            1,
        )
        report["intentType"] = intent_type
        report["rawNeed"] = optional_family
        report["requirementLevel"] = "optional"
        profile = compiler.compile(
            ExperienceSemanticInput(
                city=report["city"],
                poolId=report["poolId"],
                briefId=report["briefId"],
                planningSlotId=report["requiredSlotIds"][0],
                requirementLevel=report["requirementLevel"],
                rawNeed=report["rawNeed"],
                intentType=intent_type,
                optionalExperienceFamily=optional_family,
                targetCount=1,
                maxQueries=4,
            )
        )
        report["searchProfile"] = profile.model_dump(by_alias=True)
        discovery = (
            LegacyRecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
            if use_legacy_service
            else RecordedDiscovery(PoiDiscoveryResult(status="unresolved"))
        )

        result = PortfolioCandidateDiscoveryService(
            discovery_service=discovery,
        ).augment([report], reuse_existing_discovery=True)

        assert discovery.calls == []
        assert result.metrics["webQueryCount"] == 0
        assert result.metrics["uniqueDiscoveryQueryCount"] == 0
        assert result.metrics["reusedDiscoveryQueryCount"] == 0
        assert result.pool_reports[0]["webDiscovery"] == {
            "status": "skipped",
            "webQueryCount": 0,
            "webSeedCount": 0,
            "webSeedAmapGroundingCount": 0,
            "webDiscoveryMs": 0.0,
            "amapGroundingMs": 0.0,
            "reasonCode": reason_code,
            "attempts": [],
        }


def test_hard_gap_defers_other_demand_until_the_gap_is_resolved():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[_candidate("night-grounded", "中央电视塔")],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
        )
    )
    covered_hard = _report("brief-covered", "day-1-campus", 1)
    covered_hard["resolvedSlotIds"] = ["day-1-campus"]
    covered_hard["unresolvedSlotIds"] = []
    covered_hard["selectedCandidates"] = [
        {
            **_candidate("B000CAMP1", "北京大学"),
            "type": "科教文化服务;学校;高等院校",
            "providerTypeCode": "141201",
            "tags": ["高等院校"],
            "briefId": "brief-covered",
            "poolId": "brief-covered-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
        }
    ]
    hard_gap = _report("brief-gap", "day-1-night", 1)
    hard_gap["intentType"] = "night_view"
    hard_gap["rawNeed"] = "北京夜景"
    hard_gap["poolId"] = "brief-gap-night"
    soft_gap = _report("brief-soft", "day-2-meal", 2)
    soft_gap["intentType"] = "meal"
    soft_gap["rawNeed"] = "北京特色午餐"
    soft_gap["poolId"] = "brief-soft-meal"
    soft_gap["requirementLevel"] = "soft_experience"
    soft_gap["softGoalId"] = soft_gap.pop("goalId")

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([covered_hard, soft_gap, hard_gap])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["intent_type"] == "night_view"
    assert discovery.calls[0]["raw_need"] == "北京夜景"
    assert result.metrics["uniqueDiscoveryQueryCount"] == 1
    by_brief = {item["briefId"]: item for item in result.pool_reports}
    assert by_brief["brief-covered"]["webDiscovery"]["reasonCode"] == ("trusted_amap_slot_coverage_already_resolved")
    assert by_brief["brief-soft"]["webDiscovery"]["reasonCode"] == ("web_discovery_deferred_until_hard_gaps_resolved")
    assert all(item.get("amapId") != "night-grounded" for item in by_brief["brief-soft"]["safeCandidates"])
    assert any(item.get("amapId") == "night-grounded" for item in by_brief["brief-gap"]["safeCandidates"])


def test_distinct_hard_gap_groups_share_one_round_web_query_budget():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    discovery.max_web_queries = 1
    campus = _report("brief-campus", "day-1-campus", 1)
    night = _report("brief-night", "day-1-night", 1)
    night["intentType"] = "night_view"
    night["rawNeed"] = "北京夜景"
    night["poolId"] = "brief-night-night"
    museum = _report("brief-museum", "day-2-museum", 2)
    museum["intentType"] = "museum"
    museum["rawNeed"] = "北京博物馆"
    museum["poolId"] = "brief-museum-museum"

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([campus, night, museum])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["raw_need"] == "北京高校"
    assert result.metrics["uniqueDiscoveryQueryCount"] == 1
    assert result.metrics["webQueryCount"] == 1
    assert result.metrics["requiredBudgetExceededCount"] == 2
    assert result.metrics["webSearchSkippedReasonCodes"] == ["poi_discovery_budget_exhausted"]
    by_brief = {item["briefId"]: item for item in result.pool_reports}
    for brief_id in ("brief-night", "brief-museum"):
        assert by_brief[brief_id]["webDiscovery"] == {
            "status": "budget_exhausted",
            "webQueryCount": 0,
            "webSeedCount": 0,
            "webSeedAmapGroundingCount": 0,
            "webDiscoveryMs": 0.0,
            "amapGroundingMs": 0.0,
            "failureReason": "poi_discovery_budget_exhausted",
            "reasonCode": "poi_discovery_budget_exhausted",
            "attempts": [],
        }


def test_required_resolved_metadata_drift_still_defers_soft_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    hard_gap = _report("brief-hard", "slot-a", 1)
    hard_gap["requiredSlotIds"] = ["slot-a", "slot-b"]
    hard_gap["resolvedSlotIds"] = ["slot-a"]
    hard_gap["unresolvedSlotIds"] = []
    hard_gap["slotDayNumbers"] = {"slot-a": 1, "slot-b": 2}
    soft_gap = _report("brief-soft", "slot-meal", 1)
    soft_gap["intentType"] = "meal"
    soft_gap["rawNeed"] = "北京特色午餐"
    soft_gap["requirementLevel"] = "soft_experience"

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([soft_gap, hard_gap])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["raw_need"] == "北京高校"
    by_brief = {item["briefId"]: item for item in result.pool_reports}
    assert by_brief["brief-soft"]["webDiscovery"]["reasonCode"] == ("web_discovery_deferred_until_hard_gaps_resolved")


def test_explicit_soft_is_deferred_while_hard_gap_exists():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    hard_gap = _report("brief-hard", "slot-hard", 1)
    explicit_soft = _report("brief-explicit-soft", "slot-meal", 1)
    explicit_soft["intentType"] = "meal"
    explicit_soft["rawNeed"] = "每天午餐体验当地特色美食"
    explicit_soft["requirementLevel"] = "explicit_soft"

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([explicit_soft, hard_gap])

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["raw_need"] == "北京高校"
    explicit_result = next(item for item in result.pool_reports if item["briefId"] == "brief-explicit-soft")
    assert explicit_result["webDiscovery"]["reasonCode"] == ("web_discovery_deferred_until_hard_gaps_resolved")


def test_partial_hard_group_rebinds_discovery_only_to_unresolved_slot():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[_candidate("night-grounded", "中央电视塔")],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            discoveryEvidence=[
                {
                    "query": "北京 夜景 官方 地点",
                    "providerName": "recorded-web",
                    "providerStatus": "success",
                    "status": "grounded",
                    "resultCount": 1,
                    "seedCount": 1,
                }
            ],
        )
    )
    report = _report("brief-partial", "day-1-campus", 1)
    report["intentType"] = "night_view"
    report["rawNeed"] = "北京夜景"
    report["poolId"] = "brief-partial-night"
    report["requiredSlotIds"] = ["day-1-campus", "day-2-night"]
    report["resolvedSlotIds"] = ["day-1-campus"]
    report["unresolvedSlotIds"] = ["day-2-night"]
    report["slotDayNumbers"] = {"day-1-campus": 1, "day-2-night": 2}

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    rebound = [item for item in result.pool_reports[0]["safeCandidates"] if item.get("amapId") == "night-grounded"]
    assert [item["planningSlotId"] for item in rebound] == ["day-2-night"]
    assert [item["dayNumber"] for item in rebound] == [2]
    assert [attempt["scope"]["planningSlotId"] for attempt in result.metrics["webDiscoveryAttempts"]] == ["day-2-night"]


def test_resolved_slot_without_exact_trusted_selection_still_runs_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    report = _report("brief-forged", "day-1-campus", 1)
    report["resolvedSlotIds"] = ["day-1-campus"]
    report["unresolvedSlotIds"] = []
    report["selectedCandidates"] = [
        {
            **_candidate("B000CAMP1", "北京大学"),
            "briefId": "another-brief",
            "poolId": "brief-forged-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
        }
    ]

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert len(discovery.calls) == 1
    assert result.metrics["uniqueDiscoveryQueryCount"] == 1


def test_discovery_evidence_is_rebound_to_each_exact_scope_and_tracks_selected_amap_identity():
    evidence = {
        "query": "北京 北京高校 官方 地点",
        "providerName": "recorded-web",
        "status": "grounded",
        "durationMs": 3.5,
        "webDurationMs": 3.5,
        "amapGroundingMs": 4.5,
        "resultCount": 1,
        "seedCount": 1,
        "seedGroundings": [
            {
                "seedName": "清华大学",
                "providerName": "amap-place-search",
                "status": "grounded",
                "durationMs": 4.5,
                "candidateCount": 1,
                "selectedCandidates": [{"amapId": "web-grounded", "name": "清华大学"}],
            }
        ],
    }
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[_candidate("web-grounded", "清华大学")],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            webDurationMs=3.5,
            amapGroundingMs=4.5,
            discoveryEvidence=[evidence],
        )
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
        semantic_policy=IntentCandidateSemanticPolicy(),
    ).augment(
        [
            _report("brief-a", "day-1-campus", 1),
            _report("brief-b", "day-2-campus", 2),
        ]
    )

    assert len(discovery.calls) == 2
    assert len(result.metrics["webDiscoveryAttempts"]) == 2
    by_scope = {
        (
            item["scope"]["briefId"],
            item["scope"]["poolId"],
            item["scope"]["planningSlotId"],
            item["scope"]["dayNumber"],
            item["scope"]["sourceGoalId"],
        ): item
        for item in result.metrics["webDiscoveryAttempts"]
    }
    assert set(by_scope) == {
        (
            "brief-a",
            "brief-a-campus",
            "day-1-campus",
            1,
            "goal-campus",
        ),
        (
            "brief-b",
            "brief-b-campus",
            "day-2-campus",
            2,
            "goal-campus",
        ),
    }
    for item in by_scope.values():
        assert item["queryFingerprint"] == sha256("北京 北京高校 官方 地点".encode("utf-8")).hexdigest()
        assert "query" not in item
        assert item["providerName"] == "recorded-web"
        assert item["status"] == "grounded"
        assert item["webDurationMs"] == 3.5
        assert item["amapGroundingMs"] == 4.5
        assert item["seedGroundings"][0]["selectedCandidates"] == [{"amapId": "web-grounded", "name": "清华大学"}]
        assert item["selectedCandidates"] == [
            {
                "amapId": "web-grounded",
                "name": "清华大学",
                "scope": item["scope"],
            }
        ]


def test_discovery_evidence_scope_projection_does_not_copy_sensitive_candidate_provenance():
    candidate = _candidate("web-grounded", "清华大学")
    candidate["discoveryProvenance"] = {
        "webUrl": "https://example.invalid/private",
        "webTitle": "PRIVATE TITLE MUST NOT EXPORT",
        "snippet": "PRIVATE SNIPPET MUST NOT EXPORT",
        "headers": {"Authorization": "Bearer secret"},
        "prompt": "PRIVATE PROMPT MUST NOT EXPORT",
        "reasoning": "PRIVATE REASONING MUST NOT EXPORT",
        "localPath": "C:\\Users\\Thinkpad\\private.txt",
    }
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[candidate],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            discoveryEvidence=[
                {
                    "query": "北京 北京高校 官方 地点",
                    "providerName": "recorded-web",
                    "status": "grounded",
                    "durationMs": 1.0,
                    "webDurationMs": 1.0,
                    "amapGroundingMs": 1.0,
                    "resultCount": 1,
                    "seedCount": 1,
                    "seedGroundings": [
                        {
                            "seedName": "清华大学",
                            "providerName": "amap-place-search",
                            "status": "grounded",
                            "durationMs": 1.0,
                            "candidateCount": 1,
                            "selectedCandidates": [{"amapId": "web-grounded", "name": "清华大学"}],
                            "headers": {"Authorization": "Bearer seed secret"},
                            "snippet": "PRIVATE SEED SNIPPET MUST NOT PERSIST",
                        }
                    ],
                    "providerPayload": {"token": "PRIVATE PAYLOAD MUST NOT PERSIST"},
                    "responseHeaders": {"Set-Cookie": "PRIVATE COOKIE MUST NOT PERSIST"},
                    "prompt": "PRIVATE DISCOVERY PROMPT MUST NOT PERSIST",
                    "reasoning": "PRIVATE DISCOVERY REASONING MUST NOT PERSIST",
                }
            ],
        )
    )

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
        semantic_policy=IntentCandidateSemanticPolicy(),
    ).augment([_report("brief-a", "day-1-campus", 1)])

    exported_text = json.dumps(
        result.metrics["webDiscoveryAttempts"],
        ensure_ascii=False,
    )
    for forbidden in (
        "https://",
        "PRIVATE TITLE",
        "PRIVATE SNIPPET",
        "Authorization",
        "Bearer secret",
        "PRIVATE PROMPT",
        "PRIVATE REASONING",
        "PRIVATE SEED SNIPPET",
        "PRIVATE PAYLOAD",
        "PRIVATE COOKIE",
        "PRIVATE DISCOVERY PROMPT",
        "PRIVATE DISCOVERY REASONING",
        "C:\\Users\\Thinkpad",
        "discoveryProvenance",
        "webUrl",
        "headers",
        "snippet",
        "prompt",
        "reasoning",
        "localPath",
    ):
        assert forbidden not in exported_text


def test_optional_discovery_evidence_keeps_exact_scope_without_inventing_goal_identity():
    report = _report("brief-optional", "day-1-walk", 1)
    report.pop("goalId")
    report["requirementLevel"] = "optional"
    result = PortfolioCandidateDiscoveryService(
        discovery_service=RecordedDiscovery(
            PoiDiscoveryResult(
                status="unresolved",
                webQueryCount=1,
                webSeedCount=1,
                discoveryEvidence=[
                    {
                        "query": "北京 文化街区 官方 地点",
                        "providerName": "recorded-web",
                        "status": "unresolved",
                        "resultCount": 1,
                        "seedCount": 1,
                        "seedGroundings": [],
                    }
                ],
            )
        )
    ).augment([report])

    assert result.metrics["webDiscoveryAttempts"][0]["scope"] == {
        "briefId": "brief-optional",
        "poolId": "brief-optional-campus",
        "planningSlotId": "day-1-walk",
        "dayNumber": 1,
    }


def test_web_provider_failure_keeps_direct_amap_candidates():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="provider_failure",
            webQueryCount=1,
            failureReason="web_discovery_provider_failure",
        )
    )
    report = _report("brief-a", "day-1-campus", 1)

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([report])

    assert [item["amapId"] for item in result.pool_reports[0]["safeCandidates"]] == ["direct"]
    assert result.metrics["providerFailureCount"] == 1
    assert result.metrics["directAmapCandidateCount"] == 1


def test_budget_exhaustion_is_reported_for_required_demand():
    result = PortfolioCandidateDiscoveryService(
        discovery_service=RecordedDiscovery(
            PoiDiscoveryResult(
                status="budget_exhausted",
                failureReason="poi_discovery_budget_exhausted",
            )
        )
    ).augment([_report("brief-a", "day-1-campus", 1)])

    assert result.metrics["requiredBudgetExceededCount"] == 1
    assert [item["amapId"] for item in result.pool_reports[0]["safeCandidates"]] == ["direct"]


def test_web_seed_results_must_still_have_canonical_amap_source_and_coordinates():
    spoofed = _candidate("spoof", "清华大学")
    spoofed["source"] = "web-search"
    invalid_coordinates = _candidate("invalid-coordinates", "北京师范大学")
    invalid_coordinates["longitude"] = float("nan")
    result = PortfolioCandidateDiscoveryService(
        discovery_service=RecordedDiscovery(
            PoiDiscoveryResult(
                status="grounded",
                candidates=[spoofed, invalid_coordinates],
                webQueryCount=1,
                amapQueryCount=2,
                webSeedCount=2,
            )
        )
    ).augment([_report("brief-a", "day-1-campus", 1)])

    assert [item["amapId"] for item in result.pool_reports[0]["safeCandidates"]] == ["direct"]
    assert result.metrics["rejectedNonAmapCandidateCount"] == 1
    assert result.metrics["rejectedInvalidCoordinateCount"] == 1


def test_web_seed_results_reject_explicitly_synthetic_amap_identity():
    synthetic = _candidate("fake-amap-id", "清华大学")
    result = PortfolioCandidateDiscoveryService(
        discovery_service=RecordedDiscovery(
            PoiDiscoveryResult(
                status="grounded",
                candidates=[synthetic],
                webQueryCount=1,
                amapQueryCount=1,
                webSeedCount=1,
            )
        )
    ).augment([_report("brief-a", "day-1-campus", 1)])

    assert [item["amapId"] for item in result.pool_reports[0]["safeCandidates"]] == ["direct"]
    assert result.metrics["rejectedSyntheticIdentityCount"] == 1


def test_checkpoint_expansion_reuses_completed_web_discovery_but_searches_gap_once():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="grounded",
            candidates=[_candidate("fresh-grounded", "北京师范大学")],
            webQueryCount=1,
            amapQueryCount=1,
            webSeedCount=1,
            webDurationMs=7.5,
            amapGroundingMs=4.25,
        )
    )
    completed = _report("brief-a", "day-1-campus", 1)
    completed["resolvedSlotIds"] = ["day-1-campus"]
    completed["unresolvedSlotIds"] = []
    completed["safeCandidates"].append(
        {
            **_candidate("web-grounded", "清华大学"),
            "candidateSource": "web_seed_amap_grounded",
            "briefId": "brief-a",
            "poolId": "brief-a-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
            "sourceGoalId": "goal-campus",
        }
    )
    completed["webDiscovery"] = {
        "status": "grounded",
        "webQueryCount": 1,
        "webSeedCount": 1,
        "webSeedAmapGroundingCount": 1,
        "webDiscoveryMs": 2.5,
        "amapGroundingMs": 1.25,
        "attempts": [
            {
                "queryFingerprint": sha256("北京 北京高校 官方 地点".encode("utf-8")).hexdigest(),
                "providerName": "recorded-web",
                "providerStatus": "success",
                "status": "grounded",
                "durationMs": 3.75,
                "webDurationMs": 2.5,
                "amapGroundingMs": 1.25,
                "resultCount": 1,
                "seedCount": 1,
                "scope": {
                    "briefId": "brief-a",
                    "poolId": "brief-a-campus",
                    "planningSlotId": "day-1-campus",
                    "dayNumber": 1,
                    "sourceGoalId": "goal-campus",
                },
                "seedGroundings": [],
                "selectedCandidates": [
                    {
                        "amapId": "web-grounded",
                        "name": "清华大学",
                        "scope": {
                            "briefId": "brief-a",
                            "poolId": "brief-a-campus",
                            "planningSlotId": "day-1-campus",
                            "dayNumber": 1,
                            "sourceGoalId": "goal-campus",
                        },
                    }
                ],
            }
        ],
    }
    gap = _report("brief-b", "day-2-campus", 2)

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
        semantic_policy=IntentCandidateSemanticPolicy(),
    ).augment(
        [completed, gap],
        reuse_existing_discovery=True,
    )

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["trigger_reason"] == "portfolio_always_on_candidate_discovery"
    assert result.metrics["reusedDiscoveryQueryCount"] == 1
    by_brief = {item["briefId"]: item for item in result.metrics["discoveryByBrief"]}
    assert by_brief["brief-a"]["poolCount"] == 1
    assert by_brief["brief-a"]["webSearchCount"] == 1
    assert by_brief["brief-a"]["webDiscoveryMs"] == 2.5
    assert by_brief["brief-a"]["amapGroundingMs"] == 1.25
    assert by_brief["brief-a"]["reasonCodes"] == ["checkpoint_web_discovery_reused"]
    assert by_brief["brief-b"]["webSearchCount"] == 1
    assert by_brief["brief-b"]["webDiscoveryMs"] == 7.5
    assert by_brief["brief-b"]["amapGroundingMs"] == 4.25
    assert result.metrics["webQueryCount"] == 1
    assert result.metrics["webSearchSkippedReasonCodes"] == ["checkpoint_web_discovery_reused"]
    assert result.metrics["webDiscoveryAttempts"] == [
        {
            "queryFingerprint": sha256("北京 北京高校 官方 地点".encode("utf-8")).hexdigest(),
            "providerName": "recorded-web",
            "providerStatus": "success",
            "status": "grounded",
            "reasonCodes": ["checkpoint_web_discovery_reused"],
            "durationMs": 3.75,
            "webDurationMs": 2.5,
            "amapGroundingMs": 1.25,
            "resultCount": 1,
            "seedCount": 1,
            "seedGroundings": [],
            "scope": {
                "briefId": "brief-a",
                "poolId": "brief-a-campus",
                "planningSlotId": "day-1-campus",
                "dayNumber": 1,
                "sourceGoalId": "goal-campus",
            },
            "selectedCandidates": [
                {
                    "amapId": "web-grounded",
                    "name": "清华大学",
                    "scope": {
                        "briefId": "brief-a",
                        "poolId": "brief-a-campus",
                        "planningSlotId": "day-1-campus",
                        "dayNumber": 1,
                        "sourceGoalId": "goal-campus",
                    },
                }
            ],
            "reused": True,
        }
    ]
    completed_result = next(item for item in result.pool_reports if item["briefId"] == "brief-a")
    gap_result = next(item for item in result.pool_reports if item["briefId"] == "brief-b")
    assert completed_result["webDiscovery"]["reused"] is True
    assert completed_result["webDiscovery"]["reasonCode"] == "checkpoint_web_discovery_reused"
    assert any(item["amapId"] == "fresh-grounded" for item in gap_result["safeCandidates"])


def test_reused_completed_hard_demand_does_not_defer_optional_discovery():
    discovery = RecordedDiscovery(PoiDiscoveryResult(status="unresolved", webQueryCount=1))
    completed = _report("brief-completed", "day-1-campus", 1)
    completed["resolvedSlotIds"] = ["day-1-campus"]
    completed["unresolvedSlotIds"] = []
    completed["safeCandidates"] = [
        {
            **_candidate("web-grounded", "清华大学"),
            "candidateSource": "web_seed_amap_grounded",
            "briefId": "brief-completed",
            "poolId": "brief-completed-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
            "sourceGoalId": "goal-campus",
        }
    ]
    scope = {
        "briefId": "brief-completed",
        "poolId": "brief-completed-campus",
        "planningSlotId": "day-1-campus",
        "dayNumber": 1,
        "sourceGoalId": "goal-campus",
    }
    completed["webDiscovery"] = {
        "status": "grounded",
        "attempts": [
            {
                "queryFingerprint": sha256("北京 北京高校 官方 地点".encode("utf-8")).hexdigest(),
                "providerName": "recorded-web",
                "providerStatus": "success",
                "status": "grounded",
                "scope": scope,
                "selectedCandidates": [
                    {
                        "amapId": "web-grounded",
                        "name": "清华大学",
                        "scope": scope,
                    }
                ],
            }
        ],
    }
    optional = _report("brief-optional", "day-1-walk", 1)
    optional["requirementLevel"] = "optional"
    optional["intentType"] = "cultural_walk"
    optional["rawNeed"] = "文化街区散步"

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
    ).augment([completed, optional], reuse_existing_discovery=True)

    assert len(discovery.calls) == 1
    assert discovery.calls[0]["raw_need"] == "文化街区散步"
    assert result.metrics["reusedDiscoveryQueryCount"] == 1
    optional_result = next(item for item in result.pool_reports if item["briefId"] == "brief-optional")
    assert optional_result["webDiscovery"]["webQueryCount"] == 1


def test_checkpoint_reuse_rejects_unscoped_claim_and_runs_bounded_discovery():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="unresolved",
            webQueryCount=1,
            webSeedCount=0,
            discoveryEvidence=[
                {
                    "query": "北京 北京高校 官方 地点",
                    "providerName": "recorded-web",
                    "providerStatus": "success",
                    "status": "unresolved",
                    "reasonCode": "web_discovery_no_entity_seed",
                    "resultCount": 0,
                    "seedCount": 0,
                }
            ],
        )
    )
    forged = _report("brief-a", "day-1-campus", 1)
    forged["resolvedSlotIds"] = ["day-1-campus"]
    forged["unresolvedSlotIds"] = []
    forged["safeCandidates"] = []
    forged["webDiscovery"] = {
        "status": "grounded",
        "webQueryCount": 999,
        "attempts": [
            {
                "query": "北京 北京高校 官方 地点",
                "providerName": "recorded-web",
                "status": "grounded",
                "scope": {
                    "briefId": "brief-other",
                    "poolId": "pool-other",
                    "planningSlotId": "slot-other",
                    "dayNumber": 9,
                },
            }
        ],
    }

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
        semantic_policy=IntentCandidateSemanticPolicy(),
    ).augment([forged], reuse_existing_discovery=True)

    assert len(discovery.calls) == 1
    assert result.metrics["reusedDiscoveryQueryCount"] == 0
    assert result.metrics["webQueryCount"] == 1


def test_checkpoint_reuse_rejects_unlinked_canonical_candidate_identity():
    discovery = RecordedDiscovery(
        PoiDiscoveryResult(
            status="unresolved",
            webQueryCount=1,
            webSeedCount=0,
        )
    )
    forged = _report("brief-a", "day-1-campus", 1)
    forged["resolvedSlotIds"] = ["day-1-campus"]
    forged["unresolvedSlotIds"] = []
    forged["safeCandidates"] = [
        {
            **_candidate("B0FAKE", "看似真实高校"),
            "candidateSource": "web_seed_amap_grounded",
            "briefId": "brief-a",
            "poolId": "brief-a-campus",
            "planningSlotId": "day-1-campus",
            "dayNumber": 1,
            "sourceGoalId": "goal-campus",
        }
    ]
    forged["webDiscovery"] = {
        "status": "grounded",
        "attempts": [
            {
                "query": "北京 北京高校 官方 地点",
                "providerName": "recorded-web",
                "providerStatus": "success",
                "status": "grounded",
                "scope": {
                    "briefId": "brief-a",
                    "poolId": "brief-a-campus",
                    "planningSlotId": "day-1-campus",
                    "dayNumber": 1,
                    "sourceGoalId": "goal-campus",
                },
                "selectedCandidates": [],
            }
        ],
    }

    result = PortfolioCandidateDiscoveryService(
        discovery_service=discovery,
        semantic_policy=IntentCandidateSemanticPolicy(),
    ).augment([forged], reuse_existing_discovery=True)

    assert len(discovery.calls) == 1
    assert result.metrics["reusedDiscoveryQueryCount"] == 0

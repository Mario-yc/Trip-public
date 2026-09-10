"""Always-on Web discovery with mandatory AMap re-grounding for Portfolio pools."""

from __future__ import annotations

import copy
from hashlib import sha256
import json
import math
import re
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from src.providers.travel_tools import ResilientWebSearchProvider
from src.services.consumer_candidate_admission_service import (
    ConsumerCandidateAdmissionService,
)
from src.services.experience_search_profile_compiler import (
    search_profile_provider_rejection_reason,
)
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.night_view_candidate_policy import NightViewCandidatePolicy
from src.services.poi_discovery_service import PoiDiscoveryService
from src.services.poi_trust_policy import PoiTrustPolicy
from src.services.search_profile_trace_projector import (
    checkpoint_semantic_contract_evidence,
    checkpoint_semantic_contract_fingerprint,
)


@dataclass(frozen=True)
class PortfolioCandidateDiscoveryResult:
    pool_reports: list[dict[str, Any]]
    metrics: dict[str, Any]


@dataclass(frozen=True)
class _ProfileCoverageAssessment:
    profile: Optional[dict[str, Any]]
    covered: bool
    candidate_count: int = 0
    target_count: int = 1
    evidence_target_count: int = 1
    semantic_accepted_ids: tuple[str, ...] = ()
    semantic_rejected_ids: tuple[str, ...] = ()
    consumer_admission_rejected_ids: tuple[str, ...] = ()
    qualifying_candidate_ids: tuple[str, ...] = ()
    excluded_candidate_ids: tuple[str, ...] = ()
    web_seed_candidate_ids: tuple[str, ...] = ()


class PortfolioCandidateDiscoveryService:
    _MAX_ATTEMPTS_PER_SCOPE = 2
    _MAX_SCOPES_PER_REQUIREMENT_GROUP = 8
    _MAX_SELECTED_CANDIDATES_PER_SCOPE = 4
    _MAX_DISCOVERY_BRIEFS_PER_EVENT = 8
    _MIN_PROFILE_SEMANTIC_MATCH_SCORE = 0.8
    _EVENT_NUMERIC_KEYS = (
        "uniqueDiscoveryQueryCount",
        "webQueryCount",
        "webSeedCount",
        "webSeedAmapGroundingCount",
        "directAmapCandidateCount",
        "groundedWebCandidateCount",
        "webOnlyFinalPoiCount",
        "fakeCoordinateCount",
        "providerFailureCount",
        "reusedDiscoveryQueryCount",
        "webProviderMs",
        "amapGroundingMs",
        "webDiscoveryAttemptOmittedCount",
        "rejectedNonAmapCandidateCount",
        "rejectedInvalidCoordinateCount",
        "rejectedSyntheticIdentityCount",
        "consumerAdmissionCoverageRejectedCount",
        "requiredBudgetExceededCount",
        "explicitMealBudgetExceededCount",
        "candidateDiscoveryMs",
        "webDiscoveryMs",
        "webSearchCount",
        "amapTextCount",
        "amapAroundCount",
        "amapRouteCount",
        "profileCoverageShortcutHitCount",
        "invalidCoverageShortcutCount",
        "semanticCandidateAcceptedCount",
        "semanticCandidateRejectedCount",
        "familySpecificAmapCandidateCount",
        "webSeedGroundedCandidateCount",
        "duplicateExcludedBeforeRouteCount",
        "routePreflightAvoidedBySemanticFilterCount",
        "amapDetailFetchCount",
        "amapDetailCacheHitCount",
        "webSnippetConsumedCount",
        "webClaimExtractedCount",
        "independentSourceCount",
        "supportingClaimCount",
        "contradictionClaimCount",
        "queryPlanPlannedCount",
        "queryPlanExecutedCount",
        "queryPlanReusedCount",
        "queryPlanSkippedCount",
        "amapDetailCount",
        "remainingBudget",
    )

    def __init__(
        self,
        *,
        discovery_service: Optional[object] = None,
        semantic_policy: Optional[IntentCandidateSemanticPolicy] = None,
    ) -> None:
        self.discovery_service = discovery_service or PoiDiscoveryService(
            web_search_provider=ResilientWebSearchProvider(
                total_deadline_seconds=10.0,
            ),
            max_web_queries=2,
            max_amap_seed_queries=2,
        )
        self.semantic_policy = semantic_policy or IntentCandidateSemanticPolicy()
        self.poi_trust_policy = PoiTrustPolicy()
        self.consumer_admission = ConsumerCandidateAdmissionService()
        self.night_view_policy = NightViewCandidatePolicy()

    def augment(
        self,
        pool_reports: list[dict[str, Any]],
        *,
        reuse_existing_discovery: bool = False,
        expected_checkpoint_profiles: Optional[Mapping[tuple[str, str, str], Mapping[str, Any]]] = None,
    ) -> PortfolioCandidateDiscoveryResult:
        started = perf_counter()
        reports = [copy.deepcopy(item) for item in pool_reports if isinstance(item, dict)]
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        direct_ids: set[str] = set()
        profile_assessments: dict[int, _ProfileCoverageAssessment] = {}
        semantic_accepted_keys: set[tuple[str, ...]] = set()
        semantic_rejected_keys: set[tuple[str, ...]] = set()
        family_specific_keys: set[tuple[str, ...]] = set()
        web_seed_grounded_keys: set[tuple[str, ...]] = set()
        excluded_before_route_keys: set[tuple[str, ...]] = set()
        expected_bindings_by_report: dict[int, Optional[dict[str, Any]]] = {}
        provider_rejection_reasons: dict[int, str] = {}
        invalid_coverage_shortcut_count = 0
        coverage_rejections: list[dict[str, str]] = []
        night_evidence_candidate_names_by_report: dict[int, list[str]] = {}
        for report in reports:
            report.pop("coverageShortcutEvidence", None)
            checkpoint_evidence_present = self._has_checkpoint_profile_evidence(report)
            expected_checkpoint_binding = (
                self._expected_checkpoint_binding(
                    report,
                    expected_checkpoint_profiles,
                )
                if checkpoint_evidence_present
                else None
            )
            expected_bindings_by_report[id(report)] = expected_checkpoint_binding
            assessment = self._profile_coverage_assessment(report)
            if assessment.profile is None:
                assessment = self._checkpoint_profile_coverage_assessment(
                    report,
                    expected_checkpoint_binding=expected_checkpoint_binding,
                )
            if self._search_profile(report) is None and checkpoint_evidence_present and not assessment.covered:
                self._invalidate_checkpoint_profile_evidence(report)
            profile_assessments[id(report)] = assessment
            if str(report.get("intentType") or "") == "night_view":
                repair_names: list[str] = []
                for candidate in report.get("selectedCandidates") or []:
                    if (
                        not isinstance(candidate, dict)
                        or not self._is_trusted_amap_candidate(candidate, report)
                        or not self._candidate_has_exact_scope(candidate, report)
                    ):
                        continue
                    policy = self.night_view_policy.evaluate(candidate)
                    name = str(candidate.get("name") or "").strip()
                    if (
                        name
                        and policy.get("rejectReason") == "night_view_signal_missing"
                        and policy.get("publicAccessTypePassed") is True
                        and name not in repair_names
                    ):
                        repair_names.append(name)
                night_evidence_candidate_names_by_report[id(report)] = repair_names[:4]
            if assessment.profile is not None:
                if (
                    self._has_legacy_exact_trusted_amap_slot_coverage(
                        report,
                        require_consumer_admission=False,
                    )
                    and not assessment.covered
                    and not (
                        assessment.semantic_accepted_ids
                        and set(assessment.semantic_accepted_ids).issubset(
                            set(assessment.consumer_admission_rejected_ids)
                        )
                        and not assessment.semantic_rejected_ids
                        and not assessment.excluded_candidate_ids
                        and len(set(assessment.semantic_accepted_ids))
                        >= max(
                            assessment.target_count,
                            assessment.evidence_target_count,
                        )
                    )
                ):
                    invalid_coverage_shortcut_count += 1
                for identity in assessment.semantic_accepted_ids:
                    semantic_accepted_keys.add(self._candidate_metric_key(report, identity))
                for identity in assessment.semantic_rejected_ids:
                    semantic_rejected_keys.add(self._candidate_metric_key(report, identity))
                for identity in assessment.qualifying_candidate_ids:
                    family_specific_keys.add(self._candidate_metric_key(report, identity))
                for identity in assessment.web_seed_candidate_ids:
                    web_seed_grounded_keys.add(self._candidate_metric_key(report, identity))
                for identity in assessment.excluded_candidate_ids:
                    excluded_before_route_keys.add(self._candidate_metric_key(report, identity))
                self._filter_profile_candidates_before_route(report)
            for candidate in report.get("safeCandidates") or []:
                if isinstance(candidate, dict) and str(candidate.get("source") or "") == "amap-place-search":
                    identity = str(candidate.get("amapId") or candidate.get("id") or "")
                    if identity:
                        direct_ids.add(identity)
            report_rejections: list[dict[str, str]] = []
            for candidate in report.get("selectedCandidates") or []:
                if (
                    not isinstance(candidate, dict)
                    or not self._is_trusted_amap_candidate(candidate, report)
                    or not self._candidate_has_exact_scope(candidate, report)
                ):
                    continue
                admission = self._consumer_admission_coverage_report(report, candidate)
                if admission.get("scoreEligible") is True:
                    continue
                diagnostic = {
                    "candidateId": self._safe_identifier(candidate.get("amapId") or candidate.get("id")),
                    "stage": "consumer_admission_coverage",
                    "reasonCode": str((admission.get("reasonCodes") or ["consumer_admission_rejected"])[0]),
                }
                if diagnostic["candidateId"] and diagnostic not in report_rejections:
                    report_rejections.append(diagnostic)
                    coverage_rejections.append(diagnostic)
            if report_rejections:
                existing_rejections = [
                    item for item in report.get("candidateRejections") or [] if isinstance(item, dict)
                ]
                report["candidateRejections"] = [
                    *existing_rejections,
                    *[item for item in report_rejections if item not in existing_rejections],
                ][:16]
            provider_rejection_reason = search_profile_provider_rejection_reason(self._search_profile(report))
            if provider_rejection_reason:
                provider_rejection_reasons[id(report)] = provider_rejection_reason
            else:
                groups.setdefault(self._demand_key(report), []).append(report)
        metrics: dict[str, Any] = {
            "uniqueDiscoveryQueryCount": 0,
            "webQueryCount": 0,
            "webSeedCount": 0,
            "webSeedAmapGroundingCount": 0,
            "directAmapCandidateCount": len(direct_ids),
            "groundedWebCandidateCount": 0,
            "webOnlyFinalPoiCount": 0,
            "fakeCoordinateCount": 0,
            "providerFailureCount": 0,
            "reusedDiscoveryQueryCount": 0,
            "webSearchSkippedReasonCodes": [],
            "webProviderMs": 0.0,
            "amapGroundingMs": 0.0,
            "webDiscoveryAttempts": [],
            "webDiscoveryAttemptOmittedCount": 0,
            "reasonCodes": [],
            "rejectedNonAmapCandidateCount": 0,
            "rejectedInvalidCoordinateCount": 0,
            "rejectedSyntheticIdentityCount": 0,
            "consumerAdmissionCoverageRejectedCount": len(coverage_rejections),
            "profileCoverageShortcutHitCount": 0,
            "invalidCoverageShortcutCount": invalid_coverage_shortcut_count,
            "semanticCandidateAcceptedCount": 0,
            "semanticCandidateRejectedCount": 0,
            "familySpecificAmapCandidateCount": 0,
            "webSeedGroundedCandidateCount": 0,
            "duplicateExcludedBeforeRouteCount": 0,
            "routePreflightAvoidedBySemanticFilterCount": 0,
            "amapDetailFetchCount": 0,
            "amapDetailCacheHitCount": 0,
            "webSnippetConsumedCount": 0,
            "webClaimExtractedCount": 0,
            "independentSourceCount": 0,
            "supportingClaimCount": 0,
            "contradictionClaimCount": 0,
            "requiredBudgetExceededCount": sum(
                1
                for report in reports
                if str(report.get("providerState") or "") == "budget_exceeded"
                and str(report.get("requirementLevel") or "") == "required"
            ),
            "explicitMealBudgetExceededCount": sum(
                1
                for report in reports
                if str(report.get("providerState") or "") == "budget_exceeded"
                and str(report.get("intentType") or "") == "meal"
                and bool(report.get("softGoalId") or report.get("goalId"))
            ),
        }
        for report in reports:
            provider_rejection_reason = provider_rejection_reasons.get(id(report))
            if provider_rejection_reason:
                self._mark_web_discovery_skipped(
                    report,
                    metrics,
                    provider_rejection_reason,
                )
        grounded_ids: set[str] = set()
        remaining_web_query_budget = self._round_web_query_budget()
        def unresolved_hard_gap_now() -> bool:
            return any(
                id(report) not in provider_rejection_reasons
                and self._is_unresolved_hard_gap(report)
                and not (
                    reuse_existing_discovery
                    and self._can_reuse_discovery(
                        report,
                        expected_checkpoint_binding=(expected_bindings_by_report[id(report)]),
                    )
                )
                for report in reports
            )

        ordered_demand_groups = sorted(
            groups.values(),
            key=lambda demand_reports: (
                min(self._discovery_priority(report) for report in demand_reports),
                self._demand_key(demand_reports[0]),
            ),
        )
        for demand_reports in ordered_demand_groups:
            # Earlier hard-gap groups may be repaired by real web discovery and
            # exact AMap rebinding in this same pass.  Recompute the gate after
            # every group so the newly resolved hard slot releases the remaining
            # budget to unresolved density/theme slots instead of permanently
            # deferring them based on a stale pre-loop snapshot.
            has_unresolved_hard_gap = unresolved_hard_gap_now()
            reusable_reports = [
                item
                for item in demand_reports
                if reuse_existing_discovery
                and not self._force_candidate_discovery(item)
                and self._can_reuse_discovery(
                    item,
                    expected_checkpoint_binding=(expected_bindings_by_report[id(item)]),
                )
            ]
            if reusable_reports:
                metrics["reusedDiscoveryQueryCount"] += 1
                if "checkpoint_web_discovery_reused" not in metrics["webSearchSkippedReasonCodes"]:
                    metrics["webSearchSkippedReasonCodes"].append("checkpoint_web_discovery_reused")
                for report in reusable_reports:
                    web_discovery = dict(report.get("webDiscovery") or {})
                    scoped_attempts, omitted_count = self._scoped_discovery_attempts(
                        report=report,
                        discovery_evidence=list(web_discovery.get("attempts") or []),
                        safe_candidates=[
                            dict(item) for item in report.get("safeCandidates") or [] if isinstance(item, dict)
                        ],
                        slot_ids=[
                            str(item)
                            for item in (report.get("requiredSlotIds") or report.get("resolvedSlotIds") or [])
                            if str(item)
                        ],
                    )
                    for attempt in scoped_attempts:
                        reason_codes = list(attempt.get("reasonCodes") or [])
                        if "checkpoint_web_discovery_reused" not in reason_codes:
                            reason_codes.append("checkpoint_web_discovery_reused")
                        attempt["reasonCodes"] = reason_codes
                        attempt["reused"] = True
                    metrics["webDiscoveryAttempts"].extend(scoped_attempts)
                    metrics["webDiscoveryAttemptOmittedCount"] += omitted_count
                    if "checkpoint_web_discovery_reused" not in metrics["reasonCodes"]:
                        metrics["reasonCodes"].append("checkpoint_web_discovery_reused")
                    report["webDiscovery"] = {
                        **web_discovery,
                        "reused": True,
                        "reasonCode": "checkpoint_web_discovery_reused",
                        "attempts": scoped_attempts,
                    }
            grouped_reports = [item for item in demand_reports if item not in reusable_reports]
            discoverable_reports: list[dict[str, Any]] = []
            for report in grouped_reports:
                assessment = profile_assessments[id(report)]
                force_candidate_discovery = self._force_candidate_discovery(report)
                if (
                    report.get("_checkpointProfileEvidenceInvalid") is True
                    and not force_candidate_discovery
                ):
                    self._mark_web_discovery_skipped(
                        report,
                        metrics,
                        "checkpoint_profile_evidence_invalid",
                    )
                    continue
                if self._has_admitted_slot_coverage(report) and not force_candidate_discovery:
                    self._mark_web_discovery_skipped(
                        report,
                        metrics,
                        "trusted_amap_slot_coverage_already_resolved",
                    )
                    if assessment.profile is not None:
                        metrics["profileCoverageShortcutHitCount"] += 1
                        report["coverageShortcutEvidence"] = {
                            "profileFingerprint": self._profile_fingerprint(
                                report,
                                assessment.profile,
                            ),
                            "candidateCount": assessment.candidate_count,
                            "targetCount": assessment.target_count,
                            "evidenceTargetCount": assessment.evidence_target_count,
                            "excludedPhysicalPoiCount": len(self._profile_excluded_physical_ids(assessment.profile)),
                        }
                    continue
                if has_unresolved_hard_gap and not self._is_unresolved_hard_gap(report):
                    self._mark_web_discovery_skipped(
                        report,
                        metrics,
                        "web_discovery_deferred_until_hard_gaps_resolved",
                    )
                    continue
                discoverable_reports.append(report)
            grouped_reports = discoverable_reports
            if not grouped_reports:
                continue
            if remaining_web_query_budget == 0:
                if any(str(item.get("requirementLevel") or "") == "required" for item in grouped_reports):
                    metrics["requiredBudgetExceededCount"] += 1
                if any(
                    str(item.get("intentType") or "") == "meal" and bool(item.get("softGoalId") or item.get("goalId"))
                    for item in grouped_reports
                ):
                    metrics["explicitMealBudgetExceededCount"] += 1
                for report in grouped_reports:
                    self._mark_web_discovery_budget_exhausted(report, metrics)
                continue
            metrics["uniqueDiscoveryQueryCount"] += 1
            representative = grouped_reports[0]
            discover_kwargs: dict[str, Any] = {
                "city": str(representative.get("city") or ""),
                "intent_type": str(representative.get("intentType") or ""),
                "raw_need": str(representative.get("rawNeed") or ""),
                "trigger_reason": "portfolio_always_on_candidate_discovery",
            }
            candidate_hints = [
                str(item).strip() for item in representative.get("candidateHints") or [] if str(item).strip()
            ][:8]
            selected_candidate_names = list(
                dict.fromkeys(
                    str(item.get("name") or "").strip()
                    for item in representative.get("selectedCandidates") or []
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                )
            )
            evidence_repairable_consumer_ids = {
                str(item.get("candidateId") or "").strip()
                for item in representative.get("candidateRejections") or []
                if isinstance(item, dict)
                and str(item.get("stage") or "") == "consumer_admission_coverage"
                and str(item.get("reasonCode") or "")
                in {
                    "consumer_evidence_insufficient",
                    "structured_provider_evidence_missing",
                    "experience_access_evidence_missing",
                    "experience_access_evidence_timestamp_missing",
                    "experience_access_evidence_stale",
                }
                and str(item.get("candidateId") or "").strip()
            }
            if str(representative.get("intentType") or "") == "night_view":
                # A canonical public AMap place that failed only because its
                # provider fields do not explicitly say "night view" is an
                # evidence gap, not an entity-discovery gap.  Search the Web by
                # that real place name and bind any supporting claim back to
                # the exact same AMap identity.  Dining, weak, closed and
                # incompatible-access candidates keep their original rejection
                # and never enter this repair set.
                for candidate in representative.get("selectedCandidates") or []:
                    if not isinstance(candidate, dict):
                        continue
                    assessment = self.night_view_policy.evaluate(candidate)
                    candidate_id = str(candidate.get("amapId") or candidate.get("id") or "").strip()
                    if (
                        candidate_id
                        and assessment.get("rejectReason") == "night_view_signal_missing"
                        and assessment.get("publicAccessTypePassed") is True
                    ):
                        evidence_repairable_consumer_ids.add(candidate_id)
            # A canonical AMap candidate that failed only the final evidence
            # gate is the best possible web-search seed.  Restricting this
            # repair path to meals made already-discovered markets and historic
            # areas fall through to generic article-title extraction, after
            # which exact AMap rebinding predictably failed.  Keep semantic,
            # identity, geometry and visit-anchor failures excluded: only an
            # independently attributable claim may repair these two evidence
            # failures.
            evidence_candidate_names = list(
                dict.fromkeys(
                    [
                        *night_evidence_candidate_names_by_report.get(id(representative), []),
                        *[
                            str(item.get("name") or "").strip()
                            for item in representative.get("selectedCandidates") or []
                            if isinstance(item, dict)
                            and str(item.get("amapId") or item.get("id") or "").strip()
                            in evidence_repairable_consumer_ids
                            and str(item.get("name") or "").strip()
                        ],
                    ]
                )
            )
            excluded_candidate_names = [
                name for name in selected_candidate_names if name not in evidence_candidate_names
            ]
            if candidate_hints or evidence_candidate_names:
                discover_kwargs.update(
                    {
                        "candidate_hints": candidate_hints,
                        "evidence_candidate_names": evidence_candidate_names,
                        "excluded_candidate_names": excluded_candidate_names,
                        "query_variant_index": int(
                            sha256(
                                "|".join(
                                    (
                                        str(representative.get("briefId") or ""),
                                        str(representative.get("poolId") or ""),
                                        str(representative.get("rawNeed") or ""),
                                    )
                                ).encode("utf-8")
                            ).hexdigest()[:8],
                            16,
                        ),
                    }
                )
            representative_profile = self._search_profile(representative)
            if representative_profile is not None:
                discover_kwargs["search_profile"] = copy.deepcopy(representative_profile)
            result = self._discover_with_profile_compatibility(
                discover_kwargs,
            )
            query_fingerprint = self._discovery_query_fingerprint(
                result,
                representative,
            )
            if remaining_web_query_budget is not None:
                remaining_web_query_budget = max(
                    0,
                    remaining_web_query_budget - 1,
                )
            metrics["webQueryCount"] += int(result.web_query_count or 0)
            metrics["webSeedCount"] += min(2, int(result.web_seed_count or 0))
            metrics["webSeedAmapGroundingCount"] += int(result.amap_query_count or 0)
            metrics["webProviderMs"] += float(result.web_duration_ms or 0.0)
            metrics["amapGroundingMs"] += float(result.amap_grounding_ms or 0.0)
            metrics["amapDetailFetchCount"] += int(result.amap_detail_fetch_count or 0)
            metrics["amapDetailCacheHitCount"] += int(result.amap_detail_cache_hit_count or 0)
            metrics["webSnippetConsumedCount"] += int(result.web_snippet_consumed_count or 0)
            metrics["webClaimExtractedCount"] += int(result.web_claim_extracted_count or 0)
            metrics["independentSourceCount"] += int(result.independent_source_count or 0)
            metrics["supportingClaimCount"] += int(result.supporting_claim_count or 0)
            metrics["contradictionClaimCount"] += int(result.contradiction_claim_count or 0)
            metrics["fakeCoordinateCount"] += int(result.fake_coordinate_count or 0)
            if result.status == "provider_failure":
                metrics["providerFailureCount"] += 1
            if result.status == "budget_exhausted":
                if any(str(item.get("requirementLevel") or "") == "required" for item in grouped_reports):
                    metrics["requiredBudgetExceededCount"] += 1
                if any(
                    str(item.get("intentType") or "") == "meal" and bool(item.get("softGoalId") or item.get("goalId"))
                    for item in grouped_reports
                ):
                    metrics["explicitMealBudgetExceededCount"] += 1
            for report_index, report in enumerate(grouped_reports):
                safe = [dict(item) for item in report.get("safeCandidates") or [] if isinstance(item, dict)]
                seen = {str(item.get("amapId") or item.get("id") or "") for item in safe}
                slots = [
                    str(item)
                    for item in report.get("unresolvedSlotIds") or report.get("requiredSlotIds") or []
                    if str(item)
                ]
                slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
                search_profile = self._search_profile(report)
                semantic_threshold = self._profile_semantic_threshold(search_profile)
                for raw in result.candidates:
                    if not isinstance(raw, dict):
                        continue
                    identity = str(raw.get("amapId") or raw.get("id") or "").strip()
                    if str(raw.get("source") or "") != "amap-place-search" or not identity:
                        metrics["rejectedNonAmapCandidateCount"] += 1
                        continue
                    if self.poi_trust_policy.is_mock_or_synthetic_poi_values(
                        source=raw.get("source"),
                        amap_id=identity,
                        source_note=raw.get("sourceNote"),
                        name=raw.get("name"),
                        intent_type=report.get("intentType"),
                    ):
                        metrics["rejectedSyntheticIdentityCount"] += 1
                        continue
                    try:
                        longitude = float(raw.get("longitude"))
                        latitude = float(raw.get("latitude"))
                    except (TypeError, ValueError):
                        metrics["rejectedInvalidCoordinateCount"] += 1
                        continue
                    if not (
                        math.isfinite(longitude)
                        and math.isfinite(latitude)
                        and -180 <= longitude <= 180
                        and -90 <= latitude <= 90
                        and (longitude != 0 or latitude != 0)
                    ):
                        metrics["rejectedInvalidCoordinateCount"] += 1
                        continue
                    metric_key = self._candidate_metric_key(report, identity)
                    if self._candidate_is_profile_excluded(
                        raw,
                        search_profile,
                    ):
                        excluded_before_route_keys.add(metric_key)
                        continue
                    semantic_candidate = {
                        **raw,
                        "type": str(raw.get("type") or raw.get("providerType") or ""),
                    }
                    decision = self.semantic_policy.evaluate(
                        str(report.get("intentType") or ""),
                        semantic_candidate,
                        raw_need=str(report.get("rawNeed") or ""),
                        exact_entity=(
                            str(report.get("exactEntity") or "") or None
                            if str(report.get("entityBindingMode") or "") == "exact_entity"
                            else None
                        ),
                        optional_experience_family=(str(report.get("optionalExperienceFamily") or "") or None),
                    )
                    if not decision.passed or (
                        search_profile is not None and float(decision.confidence) < semantic_threshold
                    ):
                        semantic_rejected_keys.add(metric_key)
                        continue
                    if not self._discovered_candidate_lineage_is_valid(
                        raw,
                        report=report,
                        search_profile=search_profile,
                        query_fingerprint=query_fingerprint,
                    ):
                        semantic_rejected_keys.add(metric_key)
                        continue
                    semantic_accepted_keys.add(metric_key)
                    profile_candidate_metadata: dict[str, Any] = {}
                    if search_profile is not None:
                        profile_candidate_metadata = {
                            "searchProfileFingerprint": self._profile_fingerprint(
                                report,
                                search_profile,
                            ),
                            "exclusionFingerprint": self._exclusion_fingerprint(
                                report,
                                search_profile,
                            ),
                            "experienceFamily": str(search_profile.get("experienceFamily") or ""),
                            "activityMode": str(search_profile.get("activityMode") or ""),
                        }
                        family_specific_keys.add(metric_key)
                    web_seed_grounded_keys.add(metric_key)
                    target_slots = slots or [str(raw.get("planningSlotId") or "")]
                    if identity in seen:
                        matching_indexes = [
                            index
                            for index, existing in enumerate(safe)
                            if self._safe_identifier(existing.get("amapId") or existing.get("id")) == identity
                        ]
                        for index in matching_indexes:
                            existing = safe[index]
                            existing_slot_id = self._safe_identifier(existing.get("planningSlotId"))
                            slot_id = (
                                existing_slot_id
                                if existing_slot_id in target_slots
                                else next(
                                    (
                                        self._safe_identifier(item)
                                        for item in target_slots
                                        if self._safe_identifier(item)
                                    ),
                                    "",
                                )
                            )
                            if not slot_id:
                                continue
                            safe[index] = self._rebind_discovered_candidate(
                                existing=existing,
                                discovered=raw,
                                identity=identity,
                                report=report,
                                slot_id=slot_id,
                                day_number=slot_days.get(
                                    slot_id,
                                    raw.get("dayNumber"),
                                ),
                                profile_candidate_metadata=(profile_candidate_metadata),
                                query_fingerprint=query_fingerprint,
                                semantic_decision=decision,
                            )
                        if matching_indexes:
                            grounded_ids.add(identity)
                        continue
                    for slot_id in target_slots:
                        if not slot_id:
                            continue
                        safe.append(
                            self._rebind_discovered_candidate(
                                existing={},
                                discovered=raw,
                                identity=identity,
                                report=report,
                                slot_id=slot_id,
                                day_number=slot_days.get(
                                    slot_id,
                                    raw.get("dayNumber"),
                                ),
                                profile_candidate_metadata=(profile_candidate_metadata),
                                query_fingerprint=query_fingerprint,
                                semantic_decision=decision,
                            )
                        )
                    seen.add(identity)
                    grounded_ids.add(identity)
                report["safeCandidates"] = safe
                scoped_attempts, omitted_count = self._scoped_discovery_attempts(
                    report=report,
                    discovery_evidence=list(result.discovery_evidence or []),
                    safe_candidates=safe,
                    slot_ids=slots,
                )
                metrics["webDiscoveryAttempts"].extend(scoped_attempts)
                metrics["webDiscoveryAttemptOmittedCount"] += omitted_count
                report["webDiscovery"] = {
                    **self._web_discovery_profile_metadata(
                        report,
                        search_profile,
                    ),
                    **(
                        {"reasonCode": str(result.failure_reason or "profile_coverage_discovery_required")}
                        if search_profile is not None
                        else {}
                    ),
                    "status": result.status,
                    "queryFingerprint": query_fingerprint,
                    "sharedQueryReference": report_index > 0,
                    "webQueryCount": int(result.web_query_count or 0),
                    "webSeedCount": min(2, int(result.web_seed_count or 0)),
                    "webSeedAmapGroundingCount": int(result.amap_query_count or 0),
                    "webDiscoveryMs": round(float(result.web_duration_ms or 0.0), 3),
                    "amapGroundingMs": round(float(result.amap_grounding_ms or 0.0), 3),
                    "failureReason": result.failure_reason,
                    "attempts": scoped_attempts,
                }
        metrics["groundedWebCandidateCount"] = len(grounded_ids)
        metrics["semanticCandidateAcceptedCount"] = len(semantic_accepted_keys)
        metrics["semanticCandidateRejectedCount"] = len(semantic_rejected_keys)
        metrics["routePreflightAvoidedBySemanticFilterCount"] = len(semantic_rejected_keys)
        metrics["familySpecificAmapCandidateCount"] = len(family_specific_keys)
        metrics["webSeedGroundedCandidateCount"] = len(web_seed_grounded_keys)
        metrics["duplicateExcludedBeforeRouteCount"] = len(excluded_before_route_keys)
        metrics["candidateDiscoveryMs"] = round((perf_counter() - started) * 1000, 3)
        metrics["webProviderMs"] = round(float(metrics["webProviderMs"]), 3)
        metrics["webDiscoveryMs"] = metrics["webProviderMs"]
        metrics["amapGroundingMs"] = round(float(metrics["amapGroundingMs"]), 3)
        by_brief: dict[str, dict[str, Any]] = {}
        for report in reports:
            brief_id = str(report.get("briefId") or "")
            if not brief_id:
                continue
            row = by_brief.setdefault(
                brief_id,
                {
                    "briefId": brief_id,
                    "poolCount": 0,
                    "candidateCount": 0,
                    "webSearchCount": 0,
                    "amapGroundingCount": 0,
                    "webDiscoveryMs": 0.0,
                    "amapGroundingMs": 0.0,
                    "reasonCodes": [],
                    "sharedQueryReferenceCount": 0,
                },
            )
            row["poolCount"] += 1
            row["candidateCount"] += len(
                [item for item in report.get("safeCandidates") or [] if isinstance(item, dict)]
            )
            web = report.get("webDiscovery") if isinstance(report.get("webDiscovery"), dict) else {}
            if web.get("sharedQueryReference") is True:
                row["sharedQueryReferenceCount"] += 1
            else:
                row["webSearchCount"] += int(web.get("webQueryCount") or 0)
                row["amapGroundingCount"] += int(web.get("webSeedAmapGroundingCount") or 0)
                row["webDiscoveryMs"] += float(web.get("webDiscoveryMs") or 0.0)
                row["amapGroundingMs"] += float(web.get("amapGroundingMs") or 0.0)
            reason_code = str(web.get("reasonCode") or web.get("failureReason") or "")
            if not reason_code and str(web.get("status") or "") in {"provider_failure", "budget_exhausted"}:
                reason_code = str(web.get("status"))
            if reason_code and reason_code not in row["reasonCodes"]:
                row["reasonCodes"].append(reason_code)
        metrics["discoveryByBrief"] = [
            {
                **row,
                "webDiscoveryMs": round(float(row["webDiscoveryMs"]), 3),
                "amapGroundingMs": round(float(row["amapGroundingMs"]), 3),
            }
            for row in by_brief.values()
        ]
        return PortfolioCandidateDiscoveryResult(reports, metrics)

    def planning_event_preview(self, metrics: Any) -> dict[str, Any]:
        """Project the fixed, safe discovery summary persisted in planning events.

        Discovery internals may grow over time. This boundary deliberately
        reconstructs the event payload instead of copying the metrics mapping,
        so provider payloads, URLs, prompts, headers, or provenance objects can
        never become persisted planning-trace fields by accident.
        """

        if not isinstance(metrics, dict):
            return {}
        preview: dict[str, Any] = {}
        for key in self._EVENT_NUMERIC_KEYS:
            value = self._safe_nonnegative_number(metrics.get(key))
            if value is not None:
                preview[key] = value
        for key in ("reasonCodes", "webSearchSkippedReasonCodes"):
            values = metrics.get(key)
            if isinstance(values, list):
                preview[key] = self._safe_reason_codes({"reasonCodes": values})

        attempts: list[dict[str, Any]] = []
        raw_attempts = metrics.get("webDiscoveryAttempts")
        if isinstance(raw_attempts, list):
            for raw in raw_attempts[
                : self._MAX_ATTEMPTS_PER_SCOPE
                * self._MAX_SCOPES_PER_REQUIREMENT_GROUP
                * self._MAX_DISCOVERY_BRIEFS_PER_EVENT
            ]:
                payload = self._safe_scoped_discovery_attempt_payload(raw)
                if payload is not None:
                    attempts.append(payload)
        preview["webDiscoveryAttempts"] = attempts
        preview["discoveryByBrief"] = self._safe_discovery_by_brief(metrics.get("discoveryByBrief"))
        return preview

    def _safe_scoped_discovery_attempt_payload(
        self,
        raw: Any,
    ) -> dict[str, Any] | None:
        payload = self._safe_discovery_attempt_payload(raw)
        if payload is None or not isinstance(raw, dict):
            return None
        query = str(payload.pop("query", "") or "").strip()
        if query:
            payload["queryFingerprint"] = sha256(query.encode("utf-8")).hexdigest()
        raw_scope = raw.get("scope")
        if not isinstance(raw_scope, dict):
            return None
        scope = {
            "briefId": self._safe_identifier(raw_scope.get("briefId")),
            "poolId": self._safe_identifier(raw_scope.get("poolId")),
            "planningSlotId": self._safe_identifier(raw_scope.get("planningSlotId")),
            "dayNumber": self._day_number(raw_scope.get("dayNumber")),
        }
        if not all(scope.values()):
            return None
        source_goal_id = self._safe_identifier(raw_scope.get("sourceGoalId"))
        if source_goal_id:
            scope["sourceGoalId"] = source_goal_id
        payload["scope"] = scope
        payload["selectedCandidates"] = self._safe_scoped_selected_candidates(
            raw.get("selectedCandidates"),
            expected_scope=scope,
        )
        if isinstance(raw.get("reused"), bool):
            payload["reused"] = raw["reused"]
        return payload

    def _safe_scoped_selected_candidates(
        self,
        value: Any,
        *,
        expected_scope: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in value[: self._MAX_SELECTED_CANDIDATES_PER_SCOPE]:
            if not isinstance(raw, dict) or not isinstance(raw.get("scope"), dict):
                continue
            candidate_scope = raw["scope"]
            normalized_scope = {
                "briefId": self._safe_identifier(candidate_scope.get("briefId")),
                "poolId": self._safe_identifier(candidate_scope.get("poolId")),
                "planningSlotId": self._safe_identifier(candidate_scope.get("planningSlotId")),
                "dayNumber": self._day_number(candidate_scope.get("dayNumber")),
            }
            source_goal_id = self._safe_identifier(candidate_scope.get("sourceGoalId"))
            if source_goal_id:
                normalized_scope["sourceGoalId"] = source_goal_id
            if normalized_scope != expected_scope:
                continue
            amap_id = self._safe_identifier(raw.get("amapId"))
            name = self._safe_name(raw.get("name"))
            if amap_id and name:
                result.append(
                    {
                        "amapId": amap_id,
                        "name": name,
                        "scope": normalized_scope,
                    }
                )
        return result

    def _safe_discovery_by_brief(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        numeric_keys = (
            "poolCount",
            "candidateCount",
            "webSearchCount",
            "amapGroundingCount",
            "webDiscoveryMs",
            "amapGroundingMs",
            "sharedQueryReferenceCount",
        )
        for raw in value[: self._MAX_DISCOVERY_BRIEFS_PER_EVENT]:
            if not isinstance(raw, dict):
                continue
            brief_id = self._safe_identifier(raw.get("briefId"))
            if not brief_id:
                continue
            row: dict[str, Any] = {"briefId": brief_id}
            for key in numeric_keys:
                number = self._safe_nonnegative_number(raw.get(key))
                if number is not None:
                    row[key] = number
            row["reasonCodes"] = self._safe_reason_codes(raw)
            result.append(row)
        return result

    def _scoped_discovery_attempts(
        self,
        *,
        report: dict[str, Any],
        discovery_evidence: list[Any],
        safe_candidates: list[dict[str, Any]],
        slot_ids: list[str],
    ) -> tuple[list[dict[str, Any]], int]:
        all_raw_attempts = list(discovery_evidence or [])
        raw_attempts = [
            payload
            for raw in all_raw_attempts[: self._MAX_ATTEMPTS_PER_SCOPE]
            if (payload := self._safe_discovery_attempt_payload(raw)) is not None
        ]
        if not raw_attempts:
            return [], 0
        brief_id = self._safe_identifier(report.get("briefId"))
        pool_id = self._safe_identifier(report.get("poolId"))
        source_goal_id = self._safe_identifier(report.get("goalId") or report.get("softGoalId"))
        if not brief_id or not pool_id:
            return [], 0
        slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
        exact_slots = [self._safe_identifier(item) for item in slot_ids if self._safe_identifier(item)]
        total_scope_count = len(exact_slots)
        exact_slots = exact_slots[: self._MAX_SCOPES_PER_REQUIREMENT_GROUP]
        attempts: list[dict[str, Any]] = []
        for slot_id in exact_slots:
            day_number = self._day_number(slot_days.get(slot_id))
            if day_number is None:
                continue
            selected_candidates = self._selected_candidates_for_scope(
                safe_candidates,
                slot_id=slot_id,
                day_number=day_number,
            )
            scope = {
                "briefId": brief_id,
                "poolId": pool_id,
                "planningSlotId": slot_id,
                "dayNumber": day_number,
            }
            if source_goal_id:
                scope["sourceGoalId"] = source_goal_id
            scoped_selected_candidates = [{**candidate, "scope": scope} for candidate in selected_candidates]
            for payload in raw_attempts:
                attempts.append(
                    {
                        **payload,
                        "scope": scope,
                        "selectedCandidates": scoped_selected_candidates,
                    }
                )
        emitted_scope_count = min(
            total_scope_count,
            self._MAX_SCOPES_PER_REQUIREMENT_GROUP,
        )
        omitted_count = (
            max(0, total_scope_count - emitted_scope_count) * len(raw_attempts)
            + max(0, len(all_raw_attempts) - len(raw_attempts)) * total_scope_count
        )
        return attempts, omitted_count

    def _discover_with_profile_compatibility(
        self,
        discover_kwargs: dict[str, Any],
    ) -> Any:
        """Call the evidence-aware API while preserving legacy implementations.

        A legacy service rejects an unknown keyword before doing provider work,
        so removing only that named compatibility keyword is safe. Any other
        ``TypeError`` still propagates instead of being mistaken for an
        API-version issue.
        """

        compatible_kwargs = dict(discover_kwargs)
        optional_keywords = (
            "candidate_hints",
            "evidence_candidate_names",
            "excluded_candidate_names",
            "query_variant_index",
            "search_profile",
        )
        while True:
            try:
                return self.discovery_service.discover(**compatible_kwargs)
            except TypeError as error:
                message = str(error)
                unsupported = next(
                    (
                        key
                        for key in optional_keywords
                        if key in compatible_kwargs and key in message and "unexpected keyword" in message
                    ),
                    "",
                )
                if not unsupported:
                    raise
                compatible_kwargs.pop(unsupported, None)

    @staticmethod
    def _search_profile(report: dict[str, Any]) -> Optional[dict[str, Any]]:
        raw = report.get("searchProfile")
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump(by_alias=True, exclude_none=True)
        return dict(raw) if isinstance(raw, dict) and raw else None

    @staticmethod
    def _checkpoint_search_profile_trace(
        report: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if str(report.get("checkpointEvidenceProjectionVersion") or "") != "portfolio-resume-evidence-v2":
            return None
        raw = report.get("searchProfileTrace")
        return dict(raw) if isinstance(raw, dict) and raw else None

    @classmethod
    def _expected_checkpoint_binding(
        cls,
        report: dict[str, Any],
        expected_profiles: Optional[Mapping[tuple[str, str, str], Mapping[str, Any]]],
    ) -> Optional[dict[str, Any]]:
        if not expected_profiles:
            return None
        trace = report.get("searchProfileTrace") if isinstance(report.get("searchProfileTrace"), dict) else {}
        key = (
            cls._safe_identifier(report.get("briefId")),
            cls._safe_identifier(report.get("poolId")),
            cls._safe_identifier(trace.get("planningSlotId")),
        )
        if not all(key):
            return None
        expected = expected_profiles.get(key)
        return dict(expected) if isinstance(expected, Mapping) else None

    @classmethod
    def _has_checkpoint_profile_evidence(
        cls,
        report: dict[str, Any],
    ) -> bool:
        if "checkpointEvidenceProjectionVersion" in report or "searchProfileTrace" in report:
            return True
        if cls._search_profile(report) is not None:
            return False
        if any(
            str(report.get(key) or "").strip()
            for key in (
                "searchProfileId",
                "searchProfileFingerprint",
                "exclusionFingerprint",
            )
        ):
            return True
        return any(
            isinstance(candidate, dict)
            and bool(
                str(candidate.get("searchProfileFingerprint") or "").strip()
                or str(candidate.get("exclusionFingerprint") or "").strip()
            )
            for key in ("safeCandidates", "selectedCandidates", "topCandidates")
            for candidate in report.get(key) or []
        )

    def _checkpoint_profile_trace_is_consistent(
        self,
        report: dict[str, Any],
        trace: dict[str, Any],
        *,
        expected_checkpoint_binding: Optional[dict[str, Any]] = None,
    ) -> bool:
        persisted_contract = trace.get("semanticContractEvidence")
        persisted_contract_fingerprint = str(trace.get("semanticContractFingerprint") or "")
        expected_contract = checkpoint_semantic_contract_evidence(
            report,
            trace,
        )
        binding = expected_checkpoint_binding or {}
        expected_profile = binding.get("searchProfile") if isinstance(binding.get("searchProfile"), dict) else {}
        required_slot_ids = binding.get("requiredSlotIds")
        slot_day_numbers = binding.get("slotDayNumbers")
        authoritative_target_count = binding.get("targetCount")
        authoritative_report = {
            **report,
            "requiredSlotIds": required_slot_ids,
            "slotDayNumbers": slot_day_numbers,
            "targetCount": authoritative_target_count,
        }
        authoritative_contract = checkpoint_semantic_contract_evidence(
            authoritative_report,
            expected_profile,
        )
        if (
            expected_checkpoint_binding is None
            or binding.get("bindingVersion") != "portfolio-checkpoint-expected-binding-v1"
            or not expected_profile
            or not isinstance(required_slot_ids, list)
            or not isinstance(slot_day_numbers, dict)
            or not isinstance(authoritative_target_count, int)
            or isinstance(authoritative_target_count, bool)
            or authoritative_target_count < 1
            or not isinstance(persisted_contract, dict)
            or not persisted_contract
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                persisted_contract_fingerprint,
            )
            or not expected_contract
            or not authoritative_contract
            or persisted_contract != expected_contract
            or persisted_contract != authoritative_contract
            or checkpoint_semantic_contract_fingerprint(persisted_contract) != persisted_contract_fingerprint
        ):
            return False
        coverage = trace.get("coveragePolicy") if isinstance(trace.get("coveragePolicy"), dict) else {}
        target_count = coverage.get("targetCount")
        evidence_target_count = coverage.get("evidenceTargetCount")
        report_target_count = report.get("targetCount")
        if (
            trace.get("schemaVersion") != "poi-search-profile-v1"
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(trace.get("profileFingerprint") or ""),
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(trace.get("exclusionFingerprint") or ""),
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(trace.get("executionFingerprint") or ""),
            )
            or not isinstance(target_count, int)
            or isinstance(target_count, bool)
            or target_count < 1
            or not isinstance(evidence_target_count, int)
            or isinstance(evidence_target_count, bool)
            or evidence_target_count < target_count
            or not isinstance(report_target_count, int)
            or isinstance(report_target_count, bool)
            or report_target_count != target_count
            or not isinstance(coverage.get("distinctPhysicalPoiRequired"), bool)
            or not isinstance(coverage.get("stopWhenTargetReached"), bool)
            or not isinstance(trace.get("queryPlans"), list)
            or not trace.get("queryPlans")
        ):
            return False
        excluded = trace.get("excludedPhysicalPoiIds")
        if not isinstance(excluded, list) or len(excluded) > 64:
            return False
        normalized_excluded = [self._safe_identifier(item) for item in excluded]
        if (
            any(not item for item in normalized_excluded)
            or len(set(normalized_excluded)) != len(normalized_excluded)
            or trace.get("excludedPhysicalPoiCount") != len(normalized_excluded)
        ):
            return False
        expected_exclusion_fingerprint = sha256(
            json.dumps(
                {
                    "schemaVersion": "poi-search-exclusions-v1",
                    "excludedPhysicalPoiIds": normalized_excluded,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if expected_exclusion_fingerprint != str(trace.get("exclusionFingerprint") or ""):
            return False
        for trace_key, report_key in (
            ("city", "city"),
            ("poolId", "poolId"),
            ("briefId", "briefId"),
            ("intentType", "intentType"),
            ("requirementLevel", "requirementLevel"),
        ):
            if self._normalize_profile_value(trace.get(trace_key)) != self._normalize_profile_value(
                report.get(report_key)
            ):
                return False
        optional_family = self._normalize_profile_value(report.get("optionalExperienceFamily"))
        if optional_family and optional_family != self._normalize_profile_value(trace.get("originalExperienceFamily")):
            return False
        return self._profile_metadata_is_consistent(report, trace)

    @staticmethod
    def _invalidate_checkpoint_profile_evidence(
        report: dict[str, Any],
    ) -> None:
        required_slots = [str(item) for item in report.get("requiredSlotIds") or [] if str(item)]
        report["resolvedSlotIds"] = []
        report["unresolvedSlotIds"] = list(dict.fromkeys(required_slots))
        for key in ("safeCandidates", "selectedCandidates", "topCandidates"):
            report[key] = []
        report["selectedCount"] = 0
        report["selectedCandidateCount"] = 0
        report["missingCount"] = max(
            1,
            int(report.get("targetCount") or len(required_slots) or 1),
        )
        report["coverageStatus"] = "unresolved"
        report["coverageReason"] = "checkpoint_profile_evidence_invalid"
        report["providerState"] = "checkpoint_evidence_invalid"
        report["_checkpointProfileEvidenceInvalid"] = True

    @staticmethod
    def _profile_fingerprint(
        report: dict[str, Any],
        profile: Optional[dict[str, Any]],
    ) -> str:
        return str(report.get("searchProfileFingerprint") or (profile or {}).get("profileFingerprint") or "").strip()

    @staticmethod
    def _exclusion_fingerprint(
        report: dict[str, Any],
        profile: Optional[dict[str, Any]],
    ) -> str:
        return str(report.get("exclusionFingerprint") or (profile or {}).get("exclusionFingerprint") or "").strip()

    @classmethod
    def _profile_excluded_physical_ids(
        cls,
        profile: Optional[dict[str, Any]],
    ) -> set[str]:
        if not isinstance(profile, dict):
            return set()
        values = profile.get("excludedPhysicalPoiIds")
        if not isinstance(values, list):
            return set()
        return {normalized for value in values if (normalized := cls._normalize_profile_value(value))}

    @classmethod
    def _candidate_physical_ids(
        cls,
        candidate: dict[str, Any],
    ) -> set[str]:
        return {
            normalized
            for key in (
                "physicalPoiId",
                "canonicalPhysicalPoiId",
                "amapId",
                "id",
            )
            if (normalized := cls._normalize_profile_value(candidate.get(key)))
        }

    @classmethod
    def _candidate_is_profile_excluded(
        cls,
        candidate: dict[str, Any],
        profile: Optional[dict[str, Any]],
    ) -> bool:
        excluded = cls._profile_excluded_physical_ids(profile)
        return bool(excluded.intersection(cls._candidate_physical_ids(candidate)))

    @classmethod
    def _profile_semantic_threshold(
        cls,
        profile: Optional[dict[str, Any]],
    ) -> float:
        scoring = (
            profile.get("scoringPolicy")
            if isinstance(profile, dict) and isinstance(profile.get("scoringPolicy"), dict)
            else {}
        )
        raw = scoring.get("minimumSemanticScore")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return cls._MIN_PROFILE_SEMANTIC_MATCH_SCORE
        value = float(raw)
        if not math.isfinite(value):
            return cls._MIN_PROFILE_SEMANTIC_MATCH_SCORE
        return min(1.0, max(cls._MIN_PROFILE_SEMANTIC_MATCH_SCORE, value))

    @staticmethod
    def _candidate_semantic_score(candidate: dict[str, Any]) -> Optional[float]:
        raw = candidate.get("semanticMatchScore")
        if raw is None and isinstance(candidate.get("semanticDecision"), dict):
            raw = candidate["semanticDecision"].get("confidence")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        score = float(raw)
        return score if math.isfinite(score) else None

    def _fresh_profile_semantic_decision(
        self,
        report: dict[str, Any],
        candidate: dict[str, Any],
        profile: Optional[dict[str, Any]],
    ) -> Any:
        semantic_candidate = {
            **candidate,
            "type": str(candidate.get("type") or candidate.get("providerType") or ""),
        }
        optional_family = str(report.get("optionalExperienceFamily") or "")
        if not optional_family and isinstance(profile, dict):
            optional_family = {
                "heritage": "heritage_walk",
                "market": "market_walk",
                "art": "art_walk",
            }.get(
                str(profile.get("experienceFamily") or ""),
                str(profile.get("experienceFamily") or ""),
            )
        return self.semantic_policy.evaluate(
            str(report.get("intentType") or ""),
            semantic_candidate,
            raw_need=str(report.get("rawNeed") or ""),
            exact_entity=(
                str(report.get("exactEntity") or "") or None
                if str(report.get("entityBindingMode") or "") == "exact_entity"
                else None
            ),
            optional_experience_family=optional_family or None,
        )

    def _candidate_has_exact_scope(
        self,
        candidate: dict[str, Any],
        report: dict[str, Any],
    ) -> bool:
        brief_id = self._safe_identifier(candidate.get("briefId"))
        pool_id = self._safe_identifier(candidate.get("poolId"))
        slot_id = self._safe_identifier(candidate.get("planningSlotId"))
        raw_source_brief_id = candidate.get("sourceBriefId")
        raw_source_pool_id = candidate.get("sourcePoolId")
        raw_source_slot_id = candidate.get("sourcePlanningSlotId")
        source_brief_id = self._safe_identifier(raw_source_brief_id) if isinstance(raw_source_brief_id, str) else ""
        source_pool_id = self._safe_identifier(raw_source_pool_id) if isinstance(raw_source_pool_id, str) else ""
        source_slot_id = self._safe_identifier(raw_source_slot_id) if isinstance(raw_source_slot_id, str) else ""
        slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
        expected_day_number = self._day_number(slot_days.get(slot_id))
        candidate_day_number = self._day_number(candidate.get("dayNumber"))
        candidate_goal_id = self._safe_identifier(candidate.get("sourceGoalId"))
        report_goal_id = self._safe_identifier(report.get("goalId") or report.get("softGoalId"))
        return bool(
            brief_id == self._safe_identifier(report.get("briefId"))
            and pool_id == self._safe_identifier(report.get("poolId"))
            and slot_id
            and ("sourceBriefId" not in candidate or bool(source_brief_id and source_brief_id == brief_id))
            and ("sourcePoolId" not in candidate or bool(source_pool_id and source_pool_id == pool_id))
            and ("sourcePlanningSlotId" not in candidate or bool(source_slot_id and source_slot_id == slot_id))
            and expected_day_number is not None
            and candidate_day_number == expected_day_number
            and not (candidate_goal_id and report_goal_id and candidate_goal_id != report_goal_id)
        )

    def _profile_metadata_is_consistent(
        self,
        report: dict[str, Any],
        profile: dict[str, Any],
    ) -> bool:
        profile_fingerprint = str(profile.get("profileFingerprint") or "").strip()
        exclusion_fingerprint = str(profile.get("exclusionFingerprint") or "").strip()
        if (
            not profile_fingerprint
            or self._profile_fingerprint(report, profile) != profile_fingerprint
            or not exclusion_fingerprint
            or self._exclusion_fingerprint(report, profile) != exclusion_fingerprint
            or not str(profile.get("experienceFamily") or "").strip()
            or not str(profile.get("activityMode") or "").strip()
        ):
            return False
        for profile_key, report_key in (
            ("briefId", "briefId"),
            ("poolId", "poolId"),
        ):
            profile_value = self._normalize_profile_value(profile.get(profile_key))
            report_value = self._normalize_profile_value(report.get(report_key))
            if profile_value and profile_value != report_value:
                return False
        profile_slot_id = self._safe_identifier(profile.get("planningSlotId"))
        expected_slots = {
            self._safe_identifier(item)
            for item in (report.get("requiredSlotIds") or report.get("resolvedSlotIds") or [])
            if self._safe_identifier(item)
        }
        if profile_slot_id and profile_slot_id not in expected_slots:
            return False
        return True

    def _candidate_matches_profile_metadata(
        self,
        candidate: dict[str, Any],
        report: dict[str, Any],
        profile: dict[str, Any],
    ) -> bool:
        if not self._profile_metadata_is_consistent(report, profile):
            return False
        expected_fingerprint = self._profile_fingerprint(report, profile)
        return bool(
            str(candidate.get("searchProfileFingerprint") or "").strip() == expected_fingerprint
            and self._normalize_profile_value(candidate.get("experienceFamily"))
            == self._normalize_profile_value(profile.get("experienceFamily"))
            and self._normalize_profile_value(candidate.get("activityMode"))
            == self._normalize_profile_value(profile.get("activityMode"))
        )

    def _profile_coverage_assessment(
        self,
        report: dict[str, Any],
    ) -> _ProfileCoverageAssessment:
        return self._profile_coverage_assessment_for_profile(
            report,
            self._search_profile(report),
        )

    def _checkpoint_profile_coverage_assessment(
        self,
        report: dict[str, Any],
        *,
        expected_checkpoint_binding: Optional[dict[str, Any]] = None,
    ) -> _ProfileCoverageAssessment:
        if str(report.get("checkpointEvidenceProjectionVersion") or "") != "portfolio-resume-evidence-v2":
            return _ProfileCoverageAssessment(profile=None, covered=False)
        trace = report.get("searchProfileTrace")
        if not isinstance(trace, dict) or not trace:
            return _ProfileCoverageAssessment(profile=None, covered=False)
        if not self._checkpoint_profile_trace_is_consistent(
            report,
            trace,
            expected_checkpoint_binding=expected_checkpoint_binding,
        ):
            return _ProfileCoverageAssessment(profile=trace, covered=False)
        return self._profile_coverage_assessment_for_profile(report, trace)

    def _profile_coverage_assessment_for_profile(
        self,
        report: dict[str, Any],
        profile: Optional[dict[str, Any]],
    ) -> _ProfileCoverageAssessment:
        if profile is None:
            return _ProfileCoverageAssessment(profile=None, covered=False)
        coverage = profile.get("coveragePolicy") if isinstance(profile.get("coveragePolicy"), dict) else {}
        target_count = self._positive_int(coverage.get("targetCount"), 1)
        evidence_target_count = self._positive_int(
            coverage.get("evidenceTargetCount"),
            target_count,
        )
        threshold = self._profile_semantic_threshold(profile)
        expected_slots = {
            self._safe_identifier(item)
            for item in (report.get("requiredSlotIds") or report.get("resolvedSlotIds") or [])
            if self._safe_identifier(item)
        }
        resolved_slots = {
            self._safe_identifier(item) for item in report.get("resolvedSlotIds") or [] if self._safe_identifier(item)
        }
        accepted: set[str] = set()
        rejected: set[str] = set()
        admission_rejected: set[str] = set()
        excluded: set[str] = set()
        qualifying_ids: list[str] = []
        qualifying_records: list[tuple[str, str]] = []
        web_seed_ids: set[str] = set()
        for candidate in report.get("selectedCandidates") or []:
            if not isinstance(candidate, dict):
                continue
            identity = self._safe_identifier(candidate.get("amapId") or candidate.get("id"))
            if not identity or not self._is_trusted_amap_candidate(
                candidate,
                report,
            ):
                continue
            decision = self._fresh_profile_semantic_decision(
                report,
                candidate,
                profile,
            )
            fresh_semantic_passed = bool(decision.passed and float(decision.confidence) >= threshold)
            if fresh_semantic_passed:
                accepted.add(identity)
            else:
                rejected.add(identity)
            is_excluded = self._candidate_is_profile_excluded(
                candidate,
                profile,
            )
            if is_excluded:
                excluded.add(identity)
            semantic_score = self._candidate_semantic_score(candidate)
            metadata_semantic_passed = bool(
                candidate.get("semanticPassed") is True and semantic_score is not None and semantic_score >= threshold
            )
            admission_score_eligible = bool(
                self._consumer_admission_coverage_report(report, candidate).get("scoreEligible")
            )
            if fresh_semantic_passed and not admission_score_eligible:
                admission_rejected.add(identity)

            if (
                not fresh_semantic_passed
                or is_excluded
                or not metadata_semantic_passed
                or not self._candidate_matches_profile_metadata(
                    candidate,
                    report,
                    profile,
                )
                or not self._candidate_has_exact_scope(candidate, report)
                or not admission_score_eligible
            ):
                continue
            slot_id = self._safe_identifier(candidate.get("planningSlotId"))
            qualifying_ids.append(identity)
            qualifying_records.append((identity, slot_id))
            if str(candidate.get("candidateSource") or "") == "web_seed_amap_grounded":
                web_seed_ids.add(identity)
        distinct_required = coverage.get("distinctPhysicalPoiRequired") is not False
        candidate_count = len(set(qualifying_ids)) if distinct_required else len(qualifying_ids)
        proven_slots = {slot_id for _identity, slot_id in qualifying_records}
        required_candidate_count = max(target_count, evidence_target_count)
        covered = bool(
            self._profile_metadata_is_consistent(report, profile)
            and not list(report.get("unresolvedSlotIds") or [])
            and expected_slots
            and expected_slots.issubset(resolved_slots)
            and expected_slots.issubset(proven_slots)
            and candidate_count >= required_candidate_count
        )
        return _ProfileCoverageAssessment(
            profile=profile,
            covered=covered,
            candidate_count=candidate_count,
            target_count=target_count,
            evidence_target_count=evidence_target_count,
            semantic_accepted_ids=tuple(sorted(accepted)),
            semantic_rejected_ids=tuple(sorted(rejected)),
            consumer_admission_rejected_ids=tuple(sorted(admission_rejected)),
            qualifying_candidate_ids=tuple(sorted(set(qualifying_ids))),
            excluded_candidate_ids=tuple(sorted(excluded)),
            web_seed_candidate_ids=tuple(sorted(web_seed_ids)),
        )

    def _filter_profile_candidates_before_route(
        self,
        report: dict[str, Any],
    ) -> None:
        profile = self._search_profile(report)
        if profile is None:
            return
        rejected_anchor_ids: set[str] = set()
        for key in ("safeCandidates", "selectedCandidates", "topCandidates"):
            raw_candidates = report.get(key)
            if not isinstance(raw_candidates, list):
                continue
            filtered: list[Any] = []
            for candidate in raw_candidates:
                if not isinstance(candidate, dict) or not self._is_trusted_amap_candidate(
                    candidate,
                    report,
                ):
                    filtered.append(candidate)
                    continue
                if self._candidate_is_profile_excluded(candidate, profile):
                    continue
                admission = self._consumer_admission_coverage_report(report, candidate)
                if admission.get("scoreEligible") is not True:
                    identity = self._safe_identifier(candidate.get("amapId") or candidate.get("id"))
                    reason_code = str(
                        (admission.get("reasonCodes") or ["consumer_admission_rejected"])[0]
                    )
                    diagnostic = {
                        "candidateId": identity,
                        "stage": "consumer_admission_coverage",
                        "reasonCode": reason_code,
                    }
                    existing_rejections = [
                        item for item in report.get("candidateRejections") or [] if isinstance(item, dict)
                    ]
                    if key == "selectedCandidates" and identity and diagnostic not in existing_rejections:
                        report["candidateRejections"] = [*existing_rejections, diagnostic][:16]
                    if self._pending_evidence_admission_reason(reason_code):
                        # The candidate remains a canonical, exact-scoped AMap
                        # identity, but it is not eligible for coverage or a
                        # route matrix until an independently bound evidence
                        # repair passes Consumer Admission.  Dropping it here
                        # loses the only safe entity for the repair query and
                        # turns a recoverable evidence gap into a permanent
                        # rejection.
                        filtered.append(
                            {
                                **candidate,
                                "consumerAdmissionPendingEvidence": True,
                            }
                        )
                        continue
                    anchor_gate_rejected = any(
                        isinstance(gate, dict)
                        and gate.get("gate") == "anchor_eligibility"
                        and gate.get("passed") is False
                        for gate in admission.get("gateResults") or []
                    )
                    if identity and anchor_gate_rejected:
                        rejected_anchor_ids.add(identity)
                    continue
                filtered.append(candidate)
            report[key] = filtered

        if rejected_anchor_ids:
            report["anchorEligibilityRejectedCandidateIds"] = sorted(rejected_anchor_ids)
            report["anchorEligibilityRejectedCandidateCount"] = len(rejected_anchor_ids)

    @staticmethod
    def _pending_evidence_admission_reason(reason_code: str) -> bool:
        return reason_code in {
            "consumer_evidence_insufficient",
            "structured_provider_evidence_missing",
            "experience_access_evidence_missing",
            "experience_access_evidence_timestamp_missing",
            "experience_access_evidence_stale",
        }

    def _candidate_metric_key(
        self,
        report: dict[str, Any],
        identity: object,
    ) -> tuple[str, ...]:
        profile = self._search_profile(report) or self._checkpoint_search_profile_trace(report)
        normalized_identity = self._normalize_profile_value(identity)
        if profile is not None:
            return (
                self._profile_fingerprint(report, profile),
                self._exclusion_fingerprint(report, profile),
                normalized_identity,
            )
        return (
            "legacy",
            self._normalize_profile_value(report.get("city")),
            self._normalize_profile_value(report.get("intentType")),
            self._normalize_profile_value(report.get("rawNeed")),
            normalized_identity,
        )

    def _discovered_candidate_lineage_is_valid(
        self,
        candidate: dict[str, Any],
        *,
        report: dict[str, Any],
        search_profile: Optional[dict[str, Any]],
        query_fingerprint: str,
    ) -> bool:
        """Fail closed before Web evidence is rebound to another consumer.

        AMap identity is the join key, but it is not authorization to import
        arbitrary Web claims. Evidence-bearing candidates must also prove that
        they came from this discovery query and from a query plan in the
        current Search Profile. Explicit lineage values are never silently
        overwritten when they conflict.
        """

        identity = self._safe_identifier(candidate.get("amapId") or candidate.get("id"))
        amap_id = self._safe_identifier(candidate.get("amapId"))
        candidate_id = self._safe_identifier(candidate.get("id"))
        if (
            not identity
            or (amap_id and candidate_id and amap_id != candidate_id)
            or (candidate.get("candidateSource") and str(candidate.get("candidateSource")) != "web_seed_amap_grounded")
        ):
            return False

        source_claims = candidate.get("sourceClaims")
        if not isinstance(source_claims, list):
            source_claims = candidate.get("source_claims")
        has_source_claims = bool(
            isinstance(source_claims, list) and any(isinstance(item, dict) and item for item in source_claims)
        )
        provenance = (
            candidate.get("discoveryProvenance") if isinstance(candidate.get("discoveryProvenance"), dict) else {}
        )
        provenance_amap_id = self._safe_identifier(provenance.get("amapId"))
        provenance_query_fingerprint = str(provenance.get("webQueryFingerprint") or "").strip()
        if provenance_amap_id and provenance_amap_id != identity:
            return False
        if provenance_query_fingerprint and (
            not re.fullmatch(r"[0-9a-f]{64}", provenance_query_fingerprint)
            or provenance_query_fingerprint != query_fingerprint
        ):
            return False
        if has_source_claims and (provenance_amap_id != identity or provenance_query_fingerprint != query_fingerprint):
            return False

        raw_profile_fingerprint = str(candidate.get("searchProfileFingerprint") or "").strip()
        raw_exclusion_fingerprint = str(candidate.get("exclusionFingerprint") or "").strip()
        raw_profile_id = str(candidate.get("searchProfileId") or "").strip()
        raw_query_plan_id = self._safe_identifier(candidate.get("queryPlanId"))
        if search_profile is None:
            return not any(
                (
                    raw_profile_fingerprint,
                    raw_exclusion_fingerprint,
                    raw_profile_id,
                    raw_query_plan_id,
                )
            )

        expected_profile_fingerprint = self._profile_fingerprint(
            report,
            search_profile,
        )
        expected_exclusion_fingerprint = self._exclusion_fingerprint(
            report,
            search_profile,
        )
        expected_profile_id = str(search_profile.get("profileId") or "").strip()
        allowed_query_plan_ids = {
            plan_id
            for raw_plan in search_profile.get("queryPlans") or []
            if isinstance(raw_plan, dict) and (plan_id := self._safe_identifier(raw_plan.get("planId")))
        }
        if (
            (raw_profile_fingerprint and raw_profile_fingerprint != expected_profile_fingerprint)
            or (raw_exclusion_fingerprint and raw_exclusion_fingerprint != expected_exclusion_fingerprint)
            or (raw_profile_id and expected_profile_id and raw_profile_id != expected_profile_id)
            or (raw_query_plan_id and raw_query_plan_id not in allowed_query_plan_ids)
        ):
            return False
        return bool(
            not has_source_claims
            or (raw_profile_fingerprint == expected_profile_fingerprint and raw_query_plan_id in allowed_query_plan_ids)
        )

    def _rebind_discovered_candidate(
        self,
        *,
        existing: dict[str, Any],
        discovered: dict[str, Any],
        identity: str,
        report: dict[str, Any],
        slot_id: str,
        day_number: object,
        profile_candidate_metadata: dict[str, Any],
        query_fingerprint: str,
        semantic_decision: Any,
    ) -> dict[str, Any]:
        """Merge fresh provider evidence while retaining consumer scope."""

        source_claims = self._deduplicated_source_claims(
            discovered.get("sourceClaims"),
            discovered.get("source_claims"),
            existing.get("sourceClaims"),
            existing.get("source_claims"),
        )
        rebound = {
            **copy.deepcopy(existing),
            **copy.deepcopy(discovered),
            **profile_candidate_metadata,
            "amapId": identity,
            "id": identity,
            "source": "amap-place-search",
            "briefId": self._safe_identifier(report.get("briefId")),
            "poolId": self._safe_identifier(report.get("poolId")),
            "planningSlotId": slot_id,
            "dayNumber": self._day_number(day_number),
            "sourceBriefId": self._safe_identifier(report.get("briefId")),
            "sourcePoolId": self._safe_identifier(report.get("poolId")),
            "sourcePlanningSlotId": slot_id,
            "semanticPassed": True,
            "semanticMatchScore": float(semantic_decision.confidence),
            "semanticDecision": semantic_decision.to_camel_dict(),
            "candidateSource": "web_seed_amap_grounded",
        }
        source_goal_id = self._safe_identifier(report.get("goalId") or report.get("softGoalId"))
        if source_goal_id:
            rebound["sourceGoalId"] = source_goal_id
        else:
            rebound.pop("sourceGoalId", None)
        if source_claims:
            rebound["sourceClaims"] = source_claims
        else:
            rebound.pop("sourceClaims", None)
        rebound.pop("source_claims", None)

        provenance = (
            copy.deepcopy(discovered.get("discoveryProvenance"))
            if isinstance(discovered.get("discoveryProvenance"), dict)
            else {}
        )
        if provenance:
            provenance["webQueryFingerprint"] = query_fingerprint
            provenance["amapId"] = identity
            rebound["discoveryProvenance"] = provenance
        for stale_key in (
            "consumerAdmissionInput",
            "consumerAdmissionReport",
            "scoreEligible",
        ):
            rebound.pop(stale_key, None)
        return rebound

    @staticmethod
    def _deduplicated_source_claims(*values: Any) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, list):
                continue
            for raw in value:
                if not isinstance(raw, dict) or not raw:
                    continue
                claim = copy.deepcopy(raw)
                source_url_hash = str(claim.get("sourceUrlHash") or "").strip()
                claim_key = str(claim.get("claimKey") or claim.get("claimType") or "").strip()
                stance = str(claim.get("stance") or "").strip()
                semantic_identity = (
                    {
                        "sourceUrlHash": source_url_hash,
                        "claimKey": claim_key,
                        "stance": stance,
                    }
                    if source_url_hash and claim_key
                    else claim
                )
                fingerprint = sha256(
                    json.dumps(
                        semantic_identity,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                claims.append(claim)
                if len(claims) >= 8:
                    return claims
        return claims

    def _web_discovery_profile_metadata(
        self,
        report: dict[str, Any],
        profile: Optional[dict[str, Any]],
    ) -> dict[str, str]:
        if profile is None:
            return {}
        return {
            "searchProfileFingerprint": self._profile_fingerprint(
                report,
                profile,
            ),
            "exclusionFingerprint": self._exclusion_fingerprint(
                report,
                profile,
            ),
            "experienceFamily": str(profile.get("experienceFamily") or ""),
            "activityMode": str(profile.get("activityMode") or ""),
        }

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return max(1, default)
        return value

    @staticmethod
    def _normalize_profile_value(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    def _can_reuse_discovery(
        self,
        report: dict[str, Any],
        *,
        expected_checkpoint_binding: Optional[dict[str, Any]] = None,
    ) -> bool:
        web_discovery = report.get("webDiscovery") if isinstance(report.get("webDiscovery"), dict) else {}
        if str(web_discovery.get("status") or "") != "grounded":
            return False
        profile = self._search_profile(report)
        checkpoint_trace = self._checkpoint_search_profile_trace(report)
        profile_evidence = profile or checkpoint_trace
        assessment = (
            self._profile_coverage_assessment(report)
            if profile is not None
            else self._checkpoint_profile_coverage_assessment(
                report,
                expected_checkpoint_binding=expected_checkpoint_binding,
            )
            if checkpoint_trace is not None
            else _ProfileCoverageAssessment(profile=None, covered=False)
        )
        if profile_evidence is not None:
            if (
                not assessment.covered
                or str(web_discovery.get("searchProfileFingerprint") or "")
                != self._profile_fingerprint(report, profile_evidence)
                or str(web_discovery.get("exclusionFingerprint") or "")
                != self._exclusion_fingerprint(report, profile_evidence)
                or self._normalize_profile_value(web_discovery.get("experienceFamily"))
                != self._normalize_profile_value(profile_evidence.get("experienceFamily"))
                or self._normalize_profile_value(web_discovery.get("activityMode"))
                != self._normalize_profile_value(profile_evidence.get("activityMode"))
            ):
                return False
        if list(report.get("unresolvedSlotIds") or []):
            return False
        brief_id = self._safe_identifier(report.get("briefId"))
        pool_id = self._safe_identifier(report.get("poolId"))
        source_goal_id = self._safe_identifier(report.get("goalId") or report.get("softGoalId"))
        slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
        expected_slots = {
            self._safe_identifier(item)
            for item in (report.get("requiredSlotIds") or report.get("resolvedSlotIds") or [])
            if self._safe_identifier(item)
        }
        resolved_slots = {
            self._safe_identifier(item) for item in report.get("resolvedSlotIds") or [] if self._safe_identifier(item)
        }
        trusted_candidate_ids = (
            set(assessment.qualifying_candidate_ids)
            if profile_evidence is not None
            else self._trusted_amap_candidate_ids(report)
        )
        if (
            not brief_id
            or not pool_id
            or not expected_slots
            or not expected_slots.issubset(resolved_slots)
            or not trusted_candidate_ids
        ):
            return False
        proven_slots: set[str] = set()
        for raw in web_discovery.get("attempts") or []:
            if not isinstance(raw, dict):
                continue
            if (
                str(raw.get("status") or "") != "grounded"
                or str(raw.get("providerStatus") or "") != "success"
                or not (
                    str(raw.get("query") or "").strip()
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(raw.get("queryFingerprint") or ""),
                    )
                )
                or not str(raw.get("providerName") or "").strip()
            ):
                continue
            scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
            slot_id = self._safe_identifier(scope.get("planningSlotId"))
            expected_scope: dict[str, Any] = {
                "briefId": brief_id,
                "poolId": pool_id,
                "planningSlotId": slot_id,
                "dayNumber": self._day_number(slot_days.get(slot_id)),
            }
            if source_goal_id:
                expected_scope["sourceGoalId"] = source_goal_id
            selected_candidates = self._safe_scoped_selected_candidates(
                raw.get("selectedCandidates"),
                expected_scope=expected_scope,
            )
            if (
                self._safe_identifier(scope.get("briefId")) != brief_id
                or self._safe_identifier(scope.get("poolId")) != pool_id
                or slot_id not in expected_slots
                or self._day_number(scope.get("dayNumber")) != self._day_number(slot_days.get(slot_id))
                or (source_goal_id and self._safe_identifier(scope.get("sourceGoalId")) != source_goal_id)
                or self._safe_discovery_attempt_payload(raw) is None
                or not any(candidate.get("amapId") in trusted_candidate_ids for candidate in selected_candidates)
            ):
                continue
            proven_slots.add(slot_id)
        return expected_slots.issubset(proven_slots)

    def _has_exact_trusted_amap_slot_coverage(
        self,
        report: dict[str, Any],
    ) -> bool:
        if self._search_profile(report) is not None:
            return self._profile_coverage_assessment(report).covered
        if self._checkpoint_search_profile_trace(report) is not None:
            return self._checkpoint_profile_coverage_assessment(report).covered
        return self._has_legacy_exact_trusted_amap_slot_coverage(report)

    def _has_admitted_slot_coverage(self, report: dict[str, Any]) -> bool:
        """Return whether every demanded slot already has a final-admitted POI.

        Search-profile evidence targets describe useful alternative depth, not
        whether the user's route slot is solved.  Reusing Consumer Admission
        here keeps discovery scheduling, staging and verification on the same
        coverage truth and prevents covered hard pools from starving genuinely
        missing route anchors.
        """

        return self._has_legacy_exact_trusted_amap_slot_coverage(
            report,
            require_consumer_admission=True,
        )

    def _has_legacy_exact_trusted_amap_slot_coverage(
        self,
        report: dict[str, Any],
        *,
        require_consumer_admission: bool = True,
    ) -> bool:
        if list(report.get("unresolvedSlotIds") or []):
            return False
        expected_slots = {
            self._safe_identifier(item)
            for item in (report.get("requiredSlotIds") or report.get("resolvedSlotIds") or [])
            if self._safe_identifier(item)
        }
        resolved_slots = {
            self._safe_identifier(item) for item in report.get("resolvedSlotIds") or [] if self._safe_identifier(item)
        }
        brief_id = self._safe_identifier(report.get("briefId"))
        pool_id = self._safe_identifier(report.get("poolId"))
        slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
        selected_candidates = [item for item in report.get("selectedCandidates") or [] if isinstance(item, dict)]
        if not brief_id or not pool_id or not expected_slots or not expected_slots.issubset(resolved_slots):
            return False
        for slot_id in expected_slots:
            day_number = self._day_number(slot_days.get(slot_id))
            if day_number is None or not any(
                self._is_trusted_amap_candidate(candidate, report)
                and self._candidate_has_exact_scope(candidate, report)
                and self._safe_identifier(candidate.get("briefId")) == brief_id
                and self._safe_identifier(candidate.get("poolId")) == pool_id
                and self._safe_identifier(candidate.get("planningSlotId")) == slot_id
                and self._day_number(candidate.get("dayNumber")) == day_number
                and (
                    not require_consumer_admission
                    or bool(self._consumer_admission_coverage_report(report, candidate).get("scoreEligible"))
                )
                for candidate in selected_candidates
            ):
                return False
        return True

    def _consumer_admission_coverage_report(
        self,
        report: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        profile = self._search_profile(report) or self._checkpoint_search_profile_trace(report) or {}
        slot_id = self._safe_identifier(candidate.get("planningSlotId"))
        slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
        family = str(
            report.get("optionalExperienceFamily") or profile.get("experienceFamily") or report.get("intentType") or ""
        )
        shape = str(report.get("experienceShape") or "")
        if not shape:
            shape = (
                "area"
                if family
                in {
                    "local_life",
                    "market",
                    "market_walk",
                    "heritage",
                    "heritage_walk",
                    "art",
                    "art_walk",
                }
                else "single_poi"
            )
        consumer = self.consumer_admission.build_consumer_context(
            brief_id=self._safe_identifier(report.get("briefId")),
            pool_id=self._safe_identifier(report.get("poolId")),
            planning_slot_id=slot_id,
            day_number=self._day_number(candidate.get("dayNumber") or slot_days.get(slot_id)),
            city=str(report.get("city") or ""),
            optional_experience_family=(str(report.get("optionalExperienceFamily") or "") or None),
            family=family,
            assigned_meal_family=report.get("assignedMealFamily"),
            # Admission evaluates semantic intent; Search Profile activityMode
            # describes how the user experiences the place and is not an
            # intent identifier.
            activity_mode=str(report.get("intentType") or ""),
            requirement_level=str(report.get("requirementLevel") or "required"),
            experience_shape=shape,
            experience_goal=str(
                report.get("experienceGoal")
                or report.get("rawNeed")
                or report.get("goalId")
                or report.get("sourceGoalId")
                or report.get("intentType")
                or ""
            ),
            desired_signals=report.get("desiredSignals"),
            avoid_signals=report.get("avoidSignals"),
            evidence_requirements=(report.get("evidenceRequirements") or report.get("evidencePolicy")),
            grounding_policy=(report.get("groundingPolicy") or report.get("groundingContract")),
            route_context=(report.get("routeContext") or report.get("routeContract")),
            intent_fingerprint=report.get("intentFingerprint"),
            exact_entity=(
                report.get("exactEntity") if str(report.get("entityBindingMode") or "") == "exact_entity" else None
            ),
            preferred_types=report.get("preferredTypes"),
            rejected_types=report.get("rejectedTypes"),
        )
        return self.consumer_admission.evaluate(candidate, consumer)

    @staticmethod
    def _is_hard_requirement(report: dict[str, Any]) -> bool:
        return str(report.get("requirementLevel") or "required") not in {
            "soft_experience",
            "optional",
            "explicit_soft",
            "soft",
            "preferred",
        }

    def _is_unresolved_hard_gap(self, report: dict[str, Any]) -> bool:
        return self._is_hard_requirement(report) and (
            self._force_candidate_discovery(report)
            or not self._has_admitted_slot_coverage(report)
        )

    @staticmethod
    def _force_candidate_discovery(report: dict[str, Any]) -> bool:
        """Treat an explicit scoped retry as unresolved until final feasibility.

        Per-slot Consumer Admission proves that a candidate is real and
        semantically usable.  It does not prove that the candidate is distinct
        from another required occurrence or that the complete route is
        executable.  A server-issued density retry therefore bypasses cached
        discovery and the local coverage shortcut only for its exact pool.
        """

        return report.get("forceCandidateDiscovery") is True

    def _discovery_priority(self, report: dict[str, Any]) -> int:
        # Search budget follows unsatisfied user value.  Final-admitted hard
        # slots stay first; missing meals and declared density/theme anchors
        # follow; evidence/diversity expansion for solved slots is last.
        if self._is_unresolved_hard_gap(report):
            return 0
        if self._has_admitted_slot_coverage(report):
            return 4
        if str(report.get("intentType") or "") == "meal" and bool(
            report.get("softGoalId") or report.get("goalId")
        ):
            return 1
        if list(report.get("unresolvedSlotIds") or []):
            return 2
        return 3

    @classmethod
    def _discovery_query_fingerprint(
        cls,
        result: Any,
        representative: dict[str, Any],
    ) -> str:
        for raw in getattr(result, "discovery_evidence", None) or []:
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(by_alias=True, exclude_none=True)
            if not isinstance(raw, dict):
                continue
            fingerprint = str(raw.get("queryFingerprint") or "").strip()
            if re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                return fingerprint
        payload = json.dumps(
            cls._demand_key(representative),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def _round_web_query_budget(self) -> int | None:
        value = getattr(self.discovery_service, "max_web_queries", None)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(0, value)

    @staticmethod
    def _mark_web_discovery_budget_exhausted(
        report: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        reason_code = "poi_discovery_budget_exhausted"
        for key in ("webSearchSkippedReasonCodes", "reasonCodes"):
            if reason_code not in metrics[key]:
                metrics[key].append(reason_code)
        report["webDiscovery"] = {
            "status": "budget_exhausted",
            "webQueryCount": 0,
            "webSeedCount": 0,
            "webSeedAmapGroundingCount": 0,
            "webDiscoveryMs": 0.0,
            "amapGroundingMs": 0.0,
            "failureReason": reason_code,
            "reasonCode": reason_code,
            "attempts": [],
        }

    @staticmethod
    def _mark_web_discovery_skipped(
        report: dict[str, Any],
        metrics: dict[str, Any],
        reason_code: str,
    ) -> None:
        for key in ("webSearchSkippedReasonCodes", "reasonCodes"):
            if reason_code not in metrics[key]:
                metrics[key].append(reason_code)
        report["webDiscovery"] = {
            "status": "skipped",
            "webQueryCount": 0,
            "webSeedCount": 0,
            "webSeedAmapGroundingCount": 0,
            "webDiscoveryMs": 0.0,
            "amapGroundingMs": 0.0,
            "reasonCode": reason_code,
            "attempts": [],
        }

    def _has_trusted_amap_candidate(self, report: dict[str, Any]) -> bool:
        return bool(self._trusted_amap_candidate_ids(report))

    def _trusted_amap_candidate_ids(self, report: dict[str, Any]) -> set[str]:
        candidates = [
            item
            for key in ("safeCandidates", "selectedCandidates", "topCandidates")
            for item in report.get(key) or []
            if isinstance(item, dict)
        ]
        trusted: set[str] = set()
        for candidate in candidates:
            identity = self._safe_identifier(candidate.get("amapId") or candidate.get("id"))
            if identity and self._is_trusted_amap_candidate(candidate, report):
                trusted.add(identity)
        return trusted

    def _is_trusted_amap_candidate(
        self,
        candidate: dict[str, Any],
        report: dict[str, Any],
    ) -> bool:
        identity = self._safe_identifier(candidate.get("amapId") or candidate.get("id"))
        if str(candidate.get("source") or "") != "amap-place-search" or not identity:
            return False
        if self.poi_trust_policy.is_mock_or_synthetic_poi_values(
            source=candidate.get("source"),
            amap_id=identity,
            source_note=candidate.get("sourceNote"),
            name=candidate.get("name"),
            intent_type=report.get("intentType"),
        ):
            return False
        try:
            longitude = float(candidate.get("longitude"))
            latitude = float(candidate.get("latitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and (longitude != 0 or latitude != 0)
        )

    def _safe_discovery_attempt_payload(
        self,
        raw: Any,
    ) -> dict[str, Any] | None:
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump(by_alias=True, exclude_none=True)
        if not isinstance(raw, dict):
            return None
        status = str(raw.get("status") or "")
        if status not in {
            "grounded",
            "unresolved",
            "provider_failure",
            "budget_exhausted",
        }:
            return None
        provider_status = str(raw.get("providerStatus") or "")
        if provider_status not in {"success", "failed", "skipped"}:
            provider_status = "failed" if status == "provider_failure" else "success"
        payload: dict[str, Any] = {
            "providerName": PoiDiscoveryService._safe_provider_name(raw.get("providerName")),
            "providerStatus": provider_status,
            "status": status,
            "reasonCodes": self._safe_reason_codes(raw),
            "seedGroundings": self._safe_seed_groundings(raw.get("seedGroundings")),
        }
        raw_query = raw.get("query")
        query_fingerprint = str(raw.get("queryFingerprint") or "").strip()
        if isinstance(raw_query, str) and raw_query.strip():
            query_fingerprint = sha256(raw_query.encode("utf-8")).hexdigest()
        if re.fullmatch(r"[0-9a-f]{64}", query_fingerprint):
            payload["queryFingerprint"] = query_fingerprint
        for key in ("durationMs", "webDurationMs", "amapGroundingMs"):
            value = self._safe_nonnegative_number(raw.get(key))
            if value is not None:
                payload[key] = value
        for key in ("resultCount", "seedCount"):
            value = self._safe_nonnegative_count(raw.get(key))
            if value is not None:
                payload[key] = value
        if isinstance(raw.get("seedRecordsTruncated"), bool):
            payload["seedRecordsTruncated"] = raw["seedRecordsTruncated"]
        provider_attempts = PoiDiscoveryService._safe_provider_attempts(raw.get("providerAttempts"))
        if provider_attempts:
            payload["providerAttempts"] = provider_attempts
        return payload

    def _safe_seed_groundings(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in value[:6]:
            if not isinstance(raw, dict):
                continue
            seed_name = self._safe_name(raw.get("seedName"))
            status = str(raw.get("status") or "")
            if not seed_name or status not in {
                "grounded",
                "unresolved",
                "provider_failure",
            }:
                continue
            row: dict[str, Any] = {
                "seedName": seed_name,
                "providerName": PoiDiscoveryService._safe_provider_name(raw.get("providerName")),
                "status": status,
                "reasonCodes": self._safe_reason_codes(raw),
                "selectedCandidates": self._safe_evidence_candidates(raw.get("selectedCandidates")),
            }
            duration = self._safe_nonnegative_number(raw.get("durationMs"))
            if duration is not None:
                row["durationMs"] = duration
            candidate_count = self._safe_nonnegative_count(raw.get("candidateCount"))
            if candidate_count is not None:
                row["candidateCount"] = candidate_count
            result.append(row)
        return result

    def _safe_evidence_candidates(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for raw in value[:2]:
            if not isinstance(raw, dict):
                continue
            amap_id = self._safe_identifier(raw.get("amapId"))
            name = self._safe_name(raw.get("name"))
            if amap_id and name:
                result.append({"amapId": amap_id, "name": name})
        return result

    @staticmethod
    def _safe_reason_codes(value: dict[str, Any]) -> list[str]:
        raw_values = [value.get("reasonCode")]
        if isinstance(value.get("reasonCodes"), list):
            raw_values.extend(value["reasonCodes"])
        result: list[str] = []
        for raw in raw_values:
            normalized = str(raw or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", normalized):
                continue
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _safe_nonnegative_number(value: Any) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        return value

    @staticmethod
    def _safe_nonnegative_count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return min(value, 1_000_000)

    def _selected_candidates_for_scope(
        self,
        candidates: list[dict[str, Any]],
        *,
        slot_id: str,
        day_number: int,
    ) -> list[dict[str, str]]:
        selected: list[dict[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if (
                str(candidate.get("candidateSource") or "") != "web_seed_amap_grounded"
                or str(candidate.get("planningSlotId") or "") != slot_id
                or self._day_number(candidate.get("dayNumber")) != day_number
            ):
                continue
            amap_id = self._safe_identifier(candidate.get("amapId") or candidate.get("id"))
            name = self._safe_name(candidate.get("name"))
            if not amap_id or not name or amap_id in seen:
                continue
            selected.append({"amapId": amap_id, "name": name})
            seen.add(amap_id)
            if len(selected) >= self._MAX_SELECTED_CANDIDATES_PER_SCOPE:
                break
        return selected

    @staticmethod
    def _safe_identifier(value: object) -> str:
        normalized = str(value or "").strip()
        return normalized[:160] if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", normalized) else ""

    @staticmethod
    def _safe_name(value: object) -> str:
        normalized = re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff\s（）()，,。·、_-]+",
            " ",
            str(value or ""),
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized[:120]

    @staticmethod
    def _day_number(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
        return None

    @classmethod
    def _demand_key(cls, report: dict[str, Any]) -> tuple[str, ...]:
        normalize = lambda value: re.sub(r"\s+", "", str(value or "")).casefold()
        profile = cls._search_profile(report) or cls._checkpoint_search_profile_trace(report)
        route_context = report.get("routeContext")
        if not isinstance(route_context, dict):
            route_context = (profile or {}).get("routeContext")
        if not isinstance(route_context, dict):
            route_context = {}
        evidence_policy = report.get("evidenceRequirements") or report.get("evidencePolicy")
        if evidence_policy in (None, "", [], {}):
            evidence_policy = (profile or {}).get("evidencePolicy")
        required_slot_ids = sorted(
            str(item)
            for item in (
                report.get("requiredSlotIds") or report.get("resolvedSlotIds") or report.get("unresolvedSlotIds") or []
            )
            if str(item)
        )
        raw_slot_days = report.get("slotDayNumbers")
        slot_day_numbers = (
            {str(slot_id): cls._day_number(day_number) for slot_id, day_number in raw_slot_days.items() if str(slot_id)}
            if isinstance(raw_slot_days, dict)
            else {}
        )
        explicit_occurrence_ids = sorted(
            str(item) for item in (report.get("occurrenceIds") or report.get("goalOccurrenceIds") or []) if str(item)
        )
        for key in ("occurrenceId", "goalOccurrenceId", "sourceOccurrenceId"):
            value = str(report.get(key) or route_context.get(key) or "").strip()
            if value and value not in explicit_occurrence_ids:
                explicit_occurrence_ids.append(value)
        explicit_occurrence_ids.sort()
        scope_payload = {
            "schemaVersion": "portfolio-discovery-demand-scope-v1",
            "briefId": report.get("briefId"),
            "poolId": report.get("poolId"),
            "planningSlotId": report.get("planningSlotId"),
            "requiredSlotIds": required_slot_ids,
            "dayNumber": cls._day_number(report.get("dayNumber")),
            "slotDayNumbers": slot_day_numbers,
            "occurrenceIds": explicit_occurrence_ids,
            "executionFingerprint": (profile or {}).get("executionFingerprint"),
            "routeCorridorHash": (
                report.get("routeCorridorHash")
                or report.get("corridorHash")
                or route_context.get("routeCorridorHash")
                or route_context.get("corridorHash")
            ),
            "routeContext": route_context,
            "transportMode": (
                report.get("transportMode")
                or report.get("canonicalTransport")
                or route_context.get("transportMode")
                or route_context.get("canonicalTransport")
            ),
            "evidencePolicy": evidence_policy,
            "contractVersion": (
                report.get("contractVersion")
                or report.get("requestContractVersion")
                or route_context.get("contractVersion")
            ),
            "experienceSpecFingerprint": (
                report.get("specFingerprint")
                or report.get("experienceSpecFingerprint")
                or route_context.get("specFingerprint")
            ),
        }
        scope_fingerprint = sha256(
            json.dumps(
                scope_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return (
            normalize(report.get("city")),
            normalize(report.get("intentType")),
            normalize(report.get("rawNeed") or report.get("optionalExperienceFamily")),
            normalize(report.get("entityBindingMode")),
            normalize(report.get("exactEntity")),
            normalize(cls._profile_fingerprint(report, profile)),
            normalize(cls._exclusion_fingerprint(report, profile)),
            normalize((profile or {}).get("experienceFamily")),
            normalize((profile or {}).get("activityMode")),
            scope_fingerprint,
        )

"""Read-only route feasibility checks for Creative Portfolio proposals.

The portfolio pipeline is intentionally write-free.  This service operates on
the proposal snapshot in memory, reuses the existing AMap route implementation
and applies the shared Provider-matrix insertion contract.  It never
creates itinerary versions, patches, route rows, or candidate claims.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Iterable, Optional

from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.models.route_option import RouteOption, normalize_route_mode
from src.services.amap_call_budget import amap_route_repair_scope, current_amap_call_budget
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
from src.services.creative_planning_models import canonical_fingerprint
from src.services.itinerary_schedule_service import ItineraryScheduleService
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.poi_discovery_service import PoiDiscoveryService
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import AMAP_ROUTE_SOURCE, ROUTE_CACHE_TTL_SECONDS, RouteService


class _RouteAuthorizationPreconditionError(RuntimeError):
    """Reject route work whose exact canonical Provider scope is not derivable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PortfolioRouteFeasibilityResult:
    status: str
    snapshot: dict[str, Any]
    route_evidence: list[dict[str, Any]] = field(default_factory=list)
    route_quality_issues: list[dict[str, Any]] = field(default_factory=list)
    recommended_next_actions: list[str] = field(default_factory=list)
    candidate_repairs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_route_verification: bool = False
    provider_state: str = "not_required"
    repair_duration_ms: float = 0.0
    route_call_count: int = 0
    nearby_search_count: int = 0
    route_execution_ledger: dict[str, Any] = field(default_factory=dict)
    repair_attempt_ledger: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class PortfolioRouteFeasibilityService:
    """Compute proposal route evidence without mutating persistence."""

    NEARBY_SEARCH_RADIUS_METERS = 2_500
    MAX_NEARBY_SEARCHES = 3
    MAX_NEARBY_CANDIDATE_EVALUATIONS = 3
    MAX_REPLACEMENT_CANDIDATE_PROBES = 3
    AREA_WALK_FAMILIES = {
        "local_life",
        "market_walk",
        "heritage_walk",
        "art_walk",
        "area_walk",
    }

    def __init__(
        self,
        *,
        route_service: Optional[RouteService] = None,
        map_poi_service: Optional[MapPoiService] = None,
        route_insertion_scorer: Optional[RouteInsertionScorer] = None,
        poi_discovery_service: Optional[PoiDiscoveryService] = None,
        consumer_admission_service: Optional[ConsumerCandidateAdmissionService] = None,
    ) -> None:
        self.route_service = route_service or RouteService()
        self.map_poi_service = map_poi_service or MapPoiService()
        self.route_insertion_scorer = route_insertion_scorer or RouteInsertionScorer()
        self.poi_discovery_service = poi_discovery_service
        self.consumer_admission_service = consumer_admission_service or ConsumerCandidateAdmissionService()

    def prepare(
        self,
        snapshot: dict[str, Any],
        *,
        candidate_pools: Optional[Iterable[dict[str, Any]]] = None,
        city: str = "",
        transport_mode: str = "transit",
        allow_nearby_search: bool = True,
        allow_web_discovery: bool = False,
        preview_id: str = "portfolio_preview",
    ) -> PortfolioRouteFeasibilityResult:
        work = copy.deepcopy(snapshot)
        call_metrics = {
            "routeCallCount": 0,
            "nearbySearchCount": 0,
            "expectedLegCount": 0,
            "cachedLegCount": 0,
            "requestedLegCount": 0,
            "completedLegCount": 0,
            "verifiedLegCount": 0,
            "failedLegCount": 0,
            "providerCallCount": 0,
            "providerCacheHitCount": 0,
        }
        repair_ledger: dict[str, Any] = {
            "phaseEntered": False,
            "issueCodes": [],
            "eligibleSegmentCount": 0,
            "candidateEvaluationCount": 0,
            "candidateProbeCount": 0,
            "candidateProbeKeys": [],
            "candidateProbeEvidence": [],
            "completedMatrixCandidateKeys": [],
            "completedMatrixEvidence": [],
            "existingPoolCandidateCount": 0,
            "nearbyQueryCount": 0,
            "rejectedReasonCounts": {},
            "routePairEvaluationCount": 0,
            "acceptedReplacementCount": 0,
            "touchedRoutePairs": [],
            "reusedRoutePairs": [],
            "routeBadCheckpoints": [],
            "rejectedCanonicalPhysicalIds": [],
            "rejectedCandidateAttempts": [],
            "usedQueryFingerprints": [],
            # These fields intentionally describe only executed or verified
            # evidence.  Planned candidate pools and display copy must never
            # be treated as route-repair progress.
            "executedQueryFingerprints": [],
            "admittedCanonicalPhysicalIds": [],
            "fullProviderMatrixProofFingerprints": [],
            "attemptCursor": 0,
            "queryCursor": 0,
            # Keep route-provider work distinct from local nearby discovery.
            # These call counts are deltas for this ``prepare`` invocation;
            # cumulative retry state is represented by the sealed evidence
            # sets and their cursors below.  Staging sums invocation deltas,
            # so carrying an earlier call count here would double-count it.
            # A repair can issue a nearby POI lookup without obtaining any
            # Provider route matrix, so folding the two into one count makes
            # the persisted ledger claim route progress that never happened.
            "repairCallCount": 0,
            "providerCallCount": 0,
            "nearbySearchCallCount": 0,
            "durationMs": 0.0,
            "terminalReason": "not_required",
        }
        # Route completeness is defined by every adjacent route anchor, not
        # only meal-detour-sensitive anchors.  The latter still controls the
        # bounded replacement loop below, but must never suppress ordinary
        # visit-to-visit route evidence for an otherwise complete proposal.
        route_anchors = [item for item in self._snapshot_segments(work) if item["route_anchor"]]
        repair_ledger["snapshotRepairScopeFingerprint"] = self._route_repair_snapshot_scope_fingerprint(
            work,
            transport_mode=transport_mode,
        )
        route_anchor_counts_by_day: dict[int, int] = {}
        for anchor in route_anchors:
            day_number = int(anchor.get("dayNumber") or 0)
            route_anchor_counts_by_day[day_number] = route_anchor_counts_by_day.get(day_number, 0) + 1
        expected_route_leg_count = sum(max(0, anchor_count - 1) for anchor_count in route_anchor_counts_by_day.values())
        declared_anchor_targets = {
            int(day): int(target)
            for day, target in (snapshot.get("portfolioDayAnchorTargets") or {}).items()
            if str(day).isdigit()
        }
        anchor_mismatches = [
            {
                "dayNumber": day,
                "targetRouteAnchors": target,
                "actualRouteAnchors": int(route_anchor_counts_by_day.get(day) or 0),
            }
            for day, target in sorted(declared_anchor_targets.items())
            if int(route_anchor_counts_by_day.get(day) or 0) != target
        ]
        pending_soft_anchor_counts_by_day: dict[int, int] = {}
        pending_hard_anchor_days: set[int] = set()
        pending_future_anchor_counts_by_day: dict[int, int] = {}
        for pending in snapshot.get("portfolioPendingSlots") or []:
            if not isinstance(pending, dict) or pending.get("futureRouteAnchor") is not True:
                continue
            state = str(pending.get("state") or pending.get("status") or "pending")
            if state in {"completed", "resolved", "selected"}:
                continue
            day_number = int(pending.get("dayNumber") or 0)
            if day_number <= 0:
                continue
            pending_future_anchor_counts_by_day[day_number] = pending_future_anchor_counts_by_day.get(day_number, 0) + 1
            requirement_level = str(pending.get("requirementLevel") or "soft").casefold()
            if requirement_level in {"hard", "required"}:
                pending_hard_anchor_days.add(day_number)
                continue
            pending_soft_anchor_counts_by_day[day_number] = pending_soft_anchor_counts_by_day.get(day_number, 0) + 1
        route_target_days = sorted(
            set(declared_anchor_targets) | set(route_anchor_counts_by_day) | set(pending_future_anchor_counts_by_day)
        )
        route_target_trace = {
            "desiredDensityAnchorTargets": {
                str(day): int(declared_anchor_targets[day])
                if day in declared_anchor_targets
                else int(route_anchor_counts_by_day.get(day) or 0)
                + int(pending_future_anchor_counts_by_day.get(day) or 0)
                for day in route_target_days
            },
            "groundedRouteAnchorTargets": {
                str(day): int(route_anchor_counts_by_day.get(day) or 0) for day in route_target_days
            },
            "pendingFutureAnchorTargets": {
                str(day): int(pending_future_anchor_counts_by_day.get(day) or 0) for day in route_target_days
            },
        }
        unexplained_anchor_mismatches = [
            mismatch
            for mismatch in anchor_mismatches
            if int(mismatch["dayNumber"]) in pending_hard_anchor_days
            or int(mismatch["actualRouteAnchors"])
            + int(pending_soft_anchor_counts_by_day.get(int(mismatch["dayNumber"])) or 0)
            != int(mismatch["targetRouteAnchors"])
        ]
        if unexplained_anchor_mismatches:
            issue = {
                "code": "route_anchor_target_mismatch",
                "mismatches": unexplained_anchor_mismatches,
            }
            ledger = {
                **route_target_trace,
                "expectedLegCount": expected_route_leg_count,
                "submittedLegCount": 0,
                "cachedLegCount": 0,
                "requestedLegCount": 0,
                "completedLegCount": 0,
                "verifiedLegCount": 0,
                "failedLegCount": expected_route_leg_count,
                "providerCallCount": 0,
                "providerCacheHitCount": 0,
                "cancelledLegCount": 0,
                "retainedEvidenceCount": 0,
                "workerFailureClass": None,
                "sanitizedWorkerFailureMessage": None,
                "workerTimedOut": False,
                "workerTimeoutMs": None,
            }
            work["portfolioRouteVerificationRequired"] = False
            work["portfolioRouteEvidence"] = []
            work["portfolioThemeWalkingEvidence"] = []
            work["routeOptions"] = []
            work["routeEvidence"] = []
            work["portfolioRouteQuality"] = {
                "status": "failed",
                "providerState": "not_started",
                "routeQualityIssues": [issue],
                "recommendedNextActions": ["continue_next_direction"],
                "warnings": [],
                "executionLedger": ledger,
                "repairAttemptLedger": copy.deepcopy(repair_ledger),
            }
            return PortfolioRouteFeasibilityResult(
                status="failed",
                snapshot=work,
                route_quality_issues=[issue],
                recommended_next_actions=["continue_next_direction"],
                requires_route_verification=False,
                provider_state="not_started",
                route_execution_ledger=ledger,
                repair_attempt_ledger=copy.deepcopy(repair_ledger),
            )
        sensitive = self._sensitive_segments(work)
        insertion_contract_issues = self._provider_insertion_contract_issues(work, route_anchors)
        if insertion_contract_issues:
            ledger = {
                **route_target_trace,
                "expectedLegCount": expected_route_leg_count,
                "submittedLegCount": 0,
                "cachedLegCount": 0,
                "requestedLegCount": 0,
                "completedLegCount": 0,
                "verifiedLegCount": 0,
                "failedLegCount": expected_route_leg_count,
                "providerCallCount": 0,
                "providerCacheHitCount": 0,
                "cancelledLegCount": 0,
                "retainedEvidenceCount": 0,
                "workerFailureClass": None,
                "sanitizedWorkerFailureMessage": None,
                "workerTimedOut": False,
                "workerTimeoutMs": None,
                "routePreconditionFailureReason": str(insertion_contract_issues[0]["code"]),
            }
            work["portfolioRouteVerificationRequired"] = True
            work["portfolioRouteEvidence"] = []
            work["portfolioThemeWalkingEvidence"] = []
            work["routeOptions"] = []
            work["routeEvidence"] = []
            work["portfolioRouteQuality"] = {
                "status": "failed",
                "providerState": "precondition_failed",
                "routeQualityIssues": copy.deepcopy(insertion_contract_issues),
                "recommendedNextActions": ["refresh_experience_spec"],
                "warnings": [],
                "executionLedger": ledger,
                "repairAttemptLedger": copy.deepcopy(repair_ledger),
            }
            return PortfolioRouteFeasibilityResult(
                status="failed",
                snapshot=work,
                route_quality_issues=insertion_contract_issues,
                recommended_next_actions=["refresh_experience_spec"],
                requires_route_verification=True,
                provider_state="precondition_failed",
                route_execution_ledger=ledger,
                repair_attempt_ledger=copy.deepcopy(repair_ledger),
            )
        if expected_route_leg_count == 0:
            work["portfolioRouteVerificationRequired"] = False
            work["portfolioThemeWalkingEvidence"] = []
            work.setdefault("portfolioRouteEvidence", [])
            work.setdefault("routeOptions", [])
            work.setdefault("routeEvidence", [])
            work["portfolioRouteQuality"] = {
                "status": "passed",
                "providerState": "not_required",
                "routeQualityIssues": [],
                "recommendedNextActions": [],
                "warnings": [],
                "repairAttemptLedger": copy.deepcopy(repair_ledger),
                "executionLedger": {
                    **route_target_trace,
                    "expectedLegCount": 0,
                    "submittedLegCount": 0,
                    "cachedLegCount": 0,
                    "requestedLegCount": 0,
                    "completedLegCount": 0,
                    "verifiedLegCount": 0,
                    "failedLegCount": 0,
                    "providerCallCount": 0,
                    "providerCacheHitCount": 0,
                    "cancelledLegCount": 0,
                    "retainedEvidenceCount": 0,
                    "workerFailureClass": None,
                    "sanitizedWorkerFailureMessage": None,
                    "workerTimedOut": False,
                    "workerTimeoutMs": None,
                },
            }
            return PortfolioRouteFeasibilityResult(
                status="passed",
                snapshot=work,
                requires_route_verification=False,
                provider_state="not_required",
                route_execution_ledger=copy.deepcopy(work["portfolioRouteQuality"]["executionLedger"]),
                repair_attempt_ledger=copy.deepcopy(repair_ledger),
            )

        candidate_pool = [dict(item) for item in (candidate_pools or []) if isinstance(item, dict)]
        persisted_no_progress = self._persisted_exact_repair_no_progress(
            work,
            transport_mode=transport_mode,
            snapshot_scope_fingerprint=str(repair_ledger["snapshotRepairScopeFingerprint"]),
            candidate_pools=candidate_pool,
        )
        if persisted_no_progress is not None:
            persisted_no_progress["phaseEntered"] = True
            persisted_no_progress["terminalReason"] = "no_progress_same_scope"
            for counter in (
                "candidateProbeCount",
                "candidateEvaluationCount",
                "existingPoolCandidateCount",
                "nearbyQueryCount",
                "routePairEvaluationCount",
                "acceptedReplacementCount",
                "repairCallCount",
                "providerCallCount",
                "nearbySearchCallCount",
            ):
                persisted_no_progress[counter] = 0
            persisted_no_progress["noProgressScopeFingerprints"] = sorted(
                self._repair_checkpoint_scope_fingerprints(persisted_no_progress)
            )
            prior_quality = (
                copy.deepcopy(work.get("portfolioRouteQuality"))
                if isinstance(work.get("portfolioRouteQuality"), dict)
                else {}
            )
            status = str(prior_quality.get("status") or "pending")
            if status not in {"failed", "pending"}:
                status = "pending"
            no_progress_execution_ledger = copy.deepcopy(
                prior_quality.get("executionLedger") or {}
            )
            for counter in (
                "submittedLegCount",
                "requestedLegCount",
                "completedLegCount",
                "verifiedLegCount",
                "providerCallCount",
                "providerCacheHitCount",
                "cancelledLegCount",
            ):
                no_progress_execution_ledger[counter] = 0
            prior_quality.update(
                {
                    "status": status,
                    "providerState": "no_progress",
                    "recommendedNextActions": [],
                    "repairAttemptLedger": copy.deepcopy(persisted_no_progress),
                    "executionLedger": no_progress_execution_ledger,
                }
            )
            work["portfolioRouteQuality"] = prior_quality
            return PortfolioRouteFeasibilityResult(
                status=status,
                snapshot=work,
                route_evidence=copy.deepcopy(work.get("portfolioRouteEvidence") or []),
                route_quality_issues=copy.deepcopy(prior_quality.get("routeQualityIssues") or []),
                warnings=copy.deepcopy(prior_quality.get("warnings") or []),
                requires_route_verification=bool(work.get("portfolioRouteVerificationRequired")),
                provider_state="no_progress",
                route_execution_ledger=copy.deepcopy(no_progress_execution_ledger),
                repair_attempt_ledger=copy.deepcopy(persisted_no_progress),
            )

        repairs: list[dict[str, Any]] = []
        route_result = self._evaluate_snapshot(
            work,
            city=city,
            transport_mode=transport_mode,
            preview_id=preview_id,
            call_metrics=call_metrics,
        )
        self._store_snapshot_route_evidence(work, route_result["evidence"])
        work["portfolioThemeWalkingEvidence"] = copy.deepcopy(route_result.get("themeWalkingEvidence") or [])
        repair_started: Optional[float] = None
        repair_provider_call_baseline: Optional[int] = None
        repair_nearby_call_baseline: Optional[int] = None

        # A portfolio meal candidate is a route-sensitive segment only when the
        # structured compiler explicitly requests adjacency.  Never inspect or
        # match the user's Chinese wording here.
        if route_result["status"] != "passed":
            repair_started = perf_counter()
            repair_provider_call_baseline = int(call_metrics["providerCallCount"])
            repair_nearby_call_baseline = int(call_metrics["nearbySearchCount"])
            repair_ledger["phaseEntered"] = True
            repair_ledger["issueCodes"] = list(
                dict.fromkeys(
                    str(item.get("code") or "route_quality_failed")
                    for item in route_result["issues"]
                    if isinstance(item, dict)
                )
            )
            self._record_route_bad_checkpoints(
                repair_ledger,
                snapshot=work,
                issues=route_result["issues"],
                candidate_pools=candidate_pool,
                transport_mode=transport_mode,
            )
            self._inherit_matching_repair_evidence(work, repair_ledger)
            eligible_segments = [
                segment
                for segment in sensitive
                if self._segment_replacement_allowed(segment)
                and self._has_issue_for_segment(route_result["issues"], segment["id"])
            ]
            repair_ledger["eligibleSegmentCount"] = len(eligible_segments)
            for segment in sensitive:
                if not self._segment_replacement_allowed(segment):
                    continue
                if not self._has_issue_for_segment(route_result["issues"], segment["id"]):
                    continue
                replacement = self._find_pool_replacement(
                    work,
                    segment,
                    candidate_pool,
                    city=city,
                    transport_mode=transport_mode,
                    preview_id=preview_id,
                    call_metrics=call_metrics,
                    repair_ledger=repair_ledger,
                )
                if replacement is None and allow_nearby_search:
                    replacement = self._find_nearby_replacement(
                        work,
                        segment,
                        city=city,
                        transport_mode=transport_mode,
                        preview_id=preview_id,
                        call_metrics=call_metrics,
                        repair_ledger=repair_ledger,
                    )
                if replacement is None and allow_web_discovery:
                    replacement = self._find_discovered_replacement(
                        work,
                        segment,
                        city=city,
                        transport_mode=transport_mode,
                        preview_id=preview_id,
                        call_metrics=call_metrics,
                        repair_ledger=repair_ledger,
                    )
                if replacement is None:
                    continue
                self._replace_segment_candidate(work, segment["id"], replacement)
                repair_ledger["acceptedReplacementCount"] += 1
                touched_pairs = self._touched_route_pairs(work, segment["id"])
                repair_ledger["touchedRoutePairs"] = self._merge_pairs(
                    repair_ledger["touchedRoutePairs"], touched_pairs
                )
                repairs.append(
                    {
                        "segmentId": segment["id"],
                        "candidateId": str(replacement.get("amapId") or replacement.get("id") or ""),
                        "candidateName": str(replacement.get("name") or ""),
                        "briefId": str(replacement.get("briefId") or segment.get("briefId") or ""),
                        "poolId": str(replacement.get("poolId") or segment.get("poolId") or ""),
                        "planningSlotId": str(replacement.get("planningSlotId") or segment.get("planningSlotId") or ""),
                        "source": str(replacement.get("candidateSource") or "portfolio_candidate"),
                        "discoveryProvenance": self._safe_discovery_provenance(replacement.get("discoveryProvenance")),
                        "groundingEvidence": {
                            "creativeBriefId": str(segment.get("briefId") or ""),
                            "poolId": str(segment.get("poolId") or ""),
                            "planningSlotId": str(segment.get("planningSlotId") or ""),
                            "dayNumber": int(segment.get("dayNumber") or 0),
                            "sourceGoalId": str((segment.get("semantic") or {}).get("sourceGoalId") or ""),
                            "amapId": str(replacement.get("amapId") or replacement.get("id") or ""),
                            "source": str(replacement.get("source") or ""),
                            "longitude": replacement.get("longitude"),
                            "latitude": replacement.get("latitude"),
                        },
                    }
                )
                route_result = self._evaluate_snapshot(
                    work,
                    city=city,
                    transport_mode=transport_mode,
                    preview_id=preview_id,
                    call_metrics=call_metrics,
                    repair_scope_certificate=(
                        copy.deepcopy(replacement.get("_exactRouteRepairScopeCertificate"))
                        if isinstance(
                            replacement.get("_exactRouteRepairScopeCertificate"),
                            dict,
                        )
                        else None
                    ),
                )
                if route_result["status"] == "passed":
                    break

        route_result.setdefault("executionLedger", {}).update(route_target_trace)
        status = str(route_result["status"])
        issues = list(route_result["issues"])
        warnings = list(route_result["warnings"])
        evidence = list(route_result["evidence"])
        requires = True
        work["portfolioThemeWalkingEvidence"] = copy.deepcopy(route_result.get("themeWalkingEvidence") or [])
        work["portfolioRouteVerificationRequired"] = requires
        work["portfolioRouteEvidence"] = evidence
        # RouteOption is the durable route DTO.  Keep the legacy name as an
        # alias for older snapshots, but never compress the formal evidence.
        work["routeOptions"] = copy.deepcopy(evidence)
        work["routeEvidence"] = self._day_route_evidence(work, evidence)
        work["portfolioRouteQuality"] = {
            "status": status,
            "routeQualityIssues": issues,
            "recommendedNextActions": list(route_result["actions"]),
            "candidateRepairs": repairs,
            "providerState": route_result["provider_state"],
            "warnings": warnings,
            "executionLedger": copy.deepcopy(route_result.get("executionLedger") or {}),
        }
        try:
            ItineraryScheduleService.project_snapshot_schedule(work, evidence)
        except Exception as error:
            if repair_started is None:
                repair_started = perf_counter()
                repair_provider_call_baseline = int(call_metrics["providerCallCount"])
                repair_nearby_call_baseline = int(call_metrics["nearbySearchCount"])
            repair_ledger["phaseEntered"] = True
            repair_ledger["issueCodes"] = list(
                dict.fromkeys(
                    [
                        *repair_ledger["issueCodes"],
                        "schedule_window_latest_start_exceeded"
                        if "schedule_window_latest_start_exceeded:" in str(error)
                        else "route_schedule_projection_failed",
                    ]
                )
            )
            repaired_schedule = self._repair_schedule_window(
                work,
                error,
                candidate_pool,
                city=city,
                transport_mode=transport_mode,
                preview_id=preview_id,
                call_metrics=call_metrics,
                repair_ledger=repair_ledger,
                allow_nearby_search=allow_nearby_search,
            )
            if repaired_schedule is not None:
                route_result, repair = repaired_schedule
                route_result.setdefault("executionLedger", {}).update(route_target_trace)
                status = str(route_result["status"])
                issues = list(route_result["issues"])
                warnings = list(route_result["warnings"])
                evidence = list(route_result["evidence"])
                repairs.append(repair)
                repair_ledger["acceptedReplacementCount"] += 1
                repair_ledger["touchedRoutePairs"] = self._merge_pairs(
                    repair_ledger["touchedRoutePairs"],
                    self._touched_route_pairs(work, str(repair.get("segmentId") or "")),
                )
                self._store_snapshot_route_evidence(work, evidence)
                work["portfolioThemeWalkingEvidence"] = copy.deepcopy(route_result.get("themeWalkingEvidence") or [])
                work["portfolioRouteQuality"].update(
                    {
                        "status": status,
                        "routeQualityIssues": issues,
                        "recommendedNextActions": list(route_result["actions"]),
                        "candidateRepairs": repairs,
                        "providerState": route_result["provider_state"],
                        "warnings": warnings,
                        "executionLedger": copy.deepcopy(route_result.get("executionLedger") or {}),
                    }
                )
            else:
                # Scheduling failures must not erase already verified route
                # legs or loosen hard time windows. Keep the exact sanitized
                # failure in trace and leave the proposal non-adoptable.
                status = "pending"
                issue = {
                    "code": "route_schedule_projection_failed",
                    "workerFailureClass": type(error).__name__,
                    "sanitizedWorkerFailureMessage": self._sanitized_error(error),
                }
                issues.append(issue)
                work["portfolioRouteQuality"]["status"] = status
                work["portfolioRouteQuality"]["routeQualityIssues"] = issues
                work["portfolioRouteQuality"]["scheduleFailure"] = issue
        repair_duration_ms = (perf_counter() - repair_started) * 1000 if repair_started is not None else 0.0
        repair_ledger["durationMs"] = repair_duration_ms
        repair_route_call_count = (
            max(
                0,
                int(call_metrics["providerCallCount"]) - int(repair_provider_call_baseline or 0),
            )
            if repair_ledger["phaseEntered"]
            else 0
        )
        repair_ledger["repairCallCount"] = repair_route_call_count
        # Retain the legacy key, but make it describe the same Provider-only
        # current-invocation delta. Nearby discovery is evidence collection,
        # not a completed route matrix attempt.
        repair_ledger["providerCallCount"] = repair_route_call_count
        repair_ledger["nearbySearchCallCount"] = (
            max(
                0,
                int(call_metrics["nearbySearchCount"]) - int(repair_nearby_call_baseline or 0),
            )
            if repair_ledger["phaseEntered"]
            else 0
        )
        all_pairs = self._route_pair_ids(work)
        repair_ledger["reusedRoutePairs"] = [
            pair for pair in all_pairs if pair not in repair_ledger["touchedRoutePairs"]
        ]
        if repair_ledger["acceptedReplacementCount"]:
            repair_ledger["terminalReason"] = "replacement_accepted"
        elif repair_ledger["phaseEntered"] and not repair_ledger["eligibleSegmentCount"]:
            repair_ledger["terminalReason"] = "no_eligible_segments"
        elif repair_ledger["phaseEntered"] and repair_ledger["candidateEvaluationCount"]:
            repair_ledger["terminalReason"] = "candidates_exhausted"
        elif repair_ledger["phaseEntered"]:
            repair_ledger["terminalReason"] = "no_candidate_evaluated"

        work.setdefault("portfolioRouteQuality", {})["repairAttemptLedger"] = copy.deepcopy(repair_ledger)

        return PortfolioRouteFeasibilityResult(
            status=status,
            snapshot=work,
            route_evidence=evidence,
            route_quality_issues=issues,
            recommended_next_actions=list(route_result["actions"]),
            candidate_repairs=repairs,
            warnings=warnings,
            requires_route_verification=requires,
            provider_state=str(route_result["provider_state"]),
            repair_duration_ms=repair_duration_ms,
            route_call_count=int(call_metrics["routeCallCount"]),
            nearby_search_count=int(call_metrics["nearbySearchCount"]),
            route_execution_ledger=copy.deepcopy(route_result.get("executionLedger") or {}),
            repair_attempt_ledger=copy.deepcopy(repair_ledger),
        )

    def verify_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        city: str = "",
        transport_mode: str = "transit",
        preview_id: str = "portfolio_commit",
    ) -> PortfolioRouteFeasibilityResult:
        """Recheck stored evidence before a proposal enters the writer."""
        result = self.prepare(
            snapshot,
            city=city,
            transport_mode=transport_mode,
            allow_nearby_search=False,
            preview_id=preview_id,
        )
        if result.passed:
            stored = snapshot.get("portfolioRouteEvidence")
            if result.requires_route_verification and not isinstance(stored, list):
                return PortfolioRouteFeasibilityResult(
                    status="pending",
                    snapshot=result.snapshot,
                    route_quality_issues=[
                        {
                            "code": "route_evidence_missing",
                            "message": "方案路线证据缺失，无法在提交前确认餐饮绕行质量。",
                        }
                    ],
                    recommended_next_actions=["retry_route_verification", "choose_nearby_meal"],
                    requires_route_verification=True,
                    provider_state="evidence_missing",
                    repair_attempt_ledger=copy.deepcopy(result.repair_attempt_ledger),
                )
        return result

    def _evaluate_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        city: str,
        transport_mode: str,
        preview_id: str,
        call_metrics: Optional[dict[str, int]] = None,
        repair_scope_certificate: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        call_metrics = (
            call_metrics
            if call_metrics is not None
            else {
                "routeCallCount": 0,
                "nearbySearchCount": 0,
                "expectedLegCount": 0,
                "cachedLegCount": 0,
                "requestedLegCount": 0,
                "completedLegCount": 0,
                "verifiedLegCount": 0,
                "failedLegCount": 0,
                "providerCallCount": 0,
                "providerCacheHitCount": 0,
            }
        )
        for metric in (
            "routeCallCount",
            "nearbySearchCount",
            "expectedLegCount",
            "cachedLegCount",
            "requestedLegCount",
            "completedLegCount",
            "verifiedLegCount",
            "failedLegCount",
            "providerCallCount",
            "providerCacheHitCount",
        ):
            call_metrics.setdefault(metric, 0)
        segments = self._snapshot_segments(snapshot)
        anchors = [item for item in segments if item["route_anchor"] and item["poi"] is not None]
        anchors_by_day: dict[int, list[dict[str, Any]]] = {}
        for item in anchors:
            anchors_by_day.setdefault(int(item["dayNumber"]), []).append(item)
        pairs = [pair for day_anchors in anchors_by_day.values() for pair in zip(day_anchors, day_anchors[1:])]
        call_metrics["expectedLegCount"] = max(int(call_metrics.get("expectedLegCount") or 0), len(pairs))
        if not pairs:
            return {
                "status": "passed",
                "evidence": [],
                "themeWalkingEvidence": [],
                "issues": [],
                "warnings": [],
                "actions": [],
                "provider_state": "not_required",
                "executionLedger": {
                    "expectedLegCount": 0,
                    "submittedLegCount": 0,
                    "cachedLegCount": 0,
                    "requestedLegCount": 0,
                    "completedLegCount": 0,
                    "verifiedLegCount": 0,
                    "failedLegCount": 0,
                    "providerCallCount": 0,
                    "providerCacheHitCount": 0,
                    "cancelledLegCount": 0,
                    "retainedEvidenceCount": 0,
                    "workerFailureClass": None,
                    "sanitizedWorkerFailureMessage": None,
                    "workerTimedOut": False,
                    "workerTimeoutMs": None,
                },
            }
        pois = [item["poi"] for item in anchors]
        itinerary_segments = [item["segment"] for item in anchors]
        pair_ids = {(left["id"], right["id"]) for left, right in pairs}
        segment_days = {item["id"]: int(item["dayNumber"]) for item in anchors}
        cached_routes = {
            (str(item.get("fromSegmentId") or ""), str(item.get("toSegmentId") or "")): item
            for item in ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
            if ProposalRouteEvidenceNormalizer.is_verified(item, segment_days)
        }
        cached_routes = {key: item for key, item in cached_routes.items() if key in pair_ids}
        call_metrics["cachedLegCount"] = max(int(call_metrics.get("cachedLegCount") or 0), len(cached_routes))
        missing_pairs = pair_ids - set(cached_routes)
        routes = [self._route_option_from_evidence(item) for item in cached_routes.values()]
        # The runtime may install a provider adapter after AgentService has
        # been constructed (the offline runner does this for its AMap replay).
        # Recreate an unconfigured production RouteService at the call boundary
        # so the active adapter and its context-local budget are observed. An
        # explicitly injected/fake service remains authoritative.
        route_service = self._active_route_service()

        def build_routes_for_scope(
            scope_certificate: Optional[dict[str, Any]],
            *args: Any,
            **kwargs: Any,
        ) -> list[RouteOption]:
            with amap_route_repair_scope(scope_certificate):
                return route_service.build_routes(*args, **kwargs)

        def build_routes(*args: Any, **kwargs: Any) -> list[RouteOption]:
            return build_routes_for_scope(repair_scope_certificate, *args, **kwargs)

        def provider_failure(error: Exception) -> dict[str, Any]:
            ledger = self._failure_ledger(
                pairs,
                cached_routes,
                call_metrics,
                error,
            )
            if int(ledger.get("providerCallCount") or 0) == 0:
                ledger["routePreconditionFailureReason"] = "route_provider_not_invoked"
                return {
                    "status": "failed",
                    "evidence": list(cached_routes.values()),
                    "issues": [
                        {
                            "code": "route_provider_precondition_failed",
                            "message": "路线 Provider 未实际调用；请先修复调用前置条件。",
                        }
                    ],
                    "warnings": [],
                    "actions": [],
                    "provider_state": "precondition_failed",
                    "executionLedger": ledger,
                }
            ledger["routePreconditionFailureReason"] = None
            return {
                "status": "pending",
                "evidence": list(cached_routes.values()),
                "issues": [
                    {
                        "code": "route_provider_unavailable",
                        "message": (f"路线服务暂不可用：{self._sanitized_error(error)}"),
                    }
                ],
                "warnings": [self._sanitized_error(error)],
                "actions": [
                    "retry_route_verification",
                    "choose_nearby_meal",
                ],
                "provider_state": "provider_error",
                "executionLedger": ledger,
            }

        def authorization_failure(
            error: _RouteAuthorizationPreconditionError,
        ) -> dict[str, Any]:
            ledger = self._failure_ledger(
                pairs,
                cached_routes,
                call_metrics,
                error,
            )
            ledger["routePreconditionFailureReason"] = error.reason
            return {
                "status": "failed",
                "evidence": list(cached_routes.values()),
                "themeWalkingEvidence": [],
                "issues": [
                    {
                        "code": "route_provider_precondition_failed",
                        "reason": error.reason,
                        "message": "路线授权范围无法从当前方案的真实高德地点拓扑中确定。",
                    }
                ],
                "warnings": [],
                "actions": [],
                "provider_state": "precondition_failed",
                "executionLedger": ledger,
            }

        scoped_adjacent_pairs: Optional[set[tuple[str, str]]] = None
        scoped_bypass_pairs: Optional[set[tuple[str, str]]] = None
        if repair_scope_certificate is not None:
            try:
                scoped_adjacent_pairs, scoped_bypass_pairs = self._repair_scope_route_pairs(
                    snapshot,
                    repair_scope_certificate,
                )
            except _RouteAuthorizationPreconditionError as error:
                return authorization_failure(error)
        provider_missing_pairs = (
            missing_pairs
            if scoped_adjacent_pairs is None
            else missing_pairs & scoped_adjacent_pairs
        )
        call_metrics["requestedLegCount"] = int(call_metrics.get("requestedLegCount") or 0) + len(
            provider_missing_pairs
        )

        if not provider_missing_pairs:
            route_service = self._active_route_service()
        else:
            try:
                self._authorize_route_work(
                    snapshot,
                    route_pairs=provider_missing_pairs,
                    mode=transport_mode,
                    reason="baseline_adjacent",
                    condition="always",
                    repair_scope_certificate=repair_scope_certificate,
                )
            except _RouteAuthorizationPreconditionError as error:
                return authorization_failure(error)
            route_accounting = self._route_accounting_start()
            try:
                # Ask AMap for the user's preferred mode first.  Walking is a
                # conditional second request below, limited to pairs for which
                # AMap returned no preferred-mode route; do not burn the
                # entire route budget by preflighting both modes.
                routes.extend(
                    build_routes(
                        preview_id,
                        pois,
                        transport_mode=transport_mode,
                        segments=itinerary_segments,
                        route_pairs=provider_missing_pairs,
                        preferred_mode_only=True,
                    )
                )
            except TypeError:
                # Keep lightweight fakes and older adapters compatible while the
                # production RouteService receives the full read-only contract.
                try:
                    routes.extend(
                        build_routes(
                            preview_id,
                            pois,
                            transport_mode,
                            segments=itinerary_segments,
                        )
                    )
                except Exception as error:  # provider failures are truthful pending state
                    self._record_route_accounting(
                        call_metrics,
                        route_accounting,
                        requested_count=len(provider_missing_pairs),
                    )
                    route_accounting = None
                    return provider_failure(error)
            except Exception as error:  # provider failures are truthful pending state
                self._record_route_accounting(
                    call_metrics,
                    route_accounting,
                    requested_count=len(provider_missing_pairs),
                )
                route_accounting = None
                return provider_failure(error)
            finally:
                if route_accounting is not None:
                    self._record_route_accounting(
                        call_metrics,
                        route_accounting,
                        requested_count=len(provider_missing_pairs),
                    )

            # A walking fallback is permitted only after the preferred mode
            # is absent for this exact provider pair.  RouteService still
            # validates that any selected walking leg is genuinely compact.
            preferred_pairs = {
                (str(route.from_segment_id or ""), str(route.to_segment_id or ""))
                for route in routes
                if normalize_route_mode(str(route.mode or "")) == normalize_route_mode(transport_mode)
            }
            walking_fallback_pairs = provider_missing_pairs - preferred_pairs
            if normalize_route_mode(transport_mode) == "transit" and walking_fallback_pairs:
                walking_scope_certificate = self._repair_scope_certificate_for_mode(
                    repair_scope_certificate,
                    mode="walking",
                )
                try:
                    self._authorize_route_work(
                        snapshot,
                        route_pairs=walking_fallback_pairs,
                        mode="walking",
                        reason="conditional_walking",
                        condition="preferred_mode_unavailable",
                        repair_scope_certificate=walking_scope_certificate,
                    )
                except _RouteAuthorizationPreconditionError as error:
                    return authorization_failure(error)
                fallback_accounting = self._route_accounting_start()
                try:
                    routes.extend(
                        build_routes_for_scope(
                            walking_scope_certificate,
                            f"{preview_id}_preferred_unavailable_walking",
                            pois,
                            transport_mode="walking",
                            segments=itinerary_segments,
                            route_pairs=walking_fallback_pairs,
                            preferred_mode_only=True,
                        )
                    )
                except TypeError:
                    # Older test adapters may not accept the newer explicit
                    # call shape; their legacy behavior remains provider-only.
                    routes.extend(
                        build_routes_for_scope(
                            walking_scope_certificate,
                            f"{preview_id}_preferred_unavailable_walking",
                            pois,
                            "walking",
                            segments=itinerary_segments,
                        )
                    )
                except Exception:
                    pass
                finally:
                    self._record_route_accounting(
                        call_metrics,
                        fallback_accounting,
                        requested_count=len(walking_fallback_pairs),
                    )

        grouped: dict[tuple[str, str], list[RouteOption]] = {}
        for route in routes or []:
            pair = (str(route.from_segment_id or ""), str(route.to_segment_id or ""))
            grouped.setdefault(pair, []).append(route)

        # Reserve route budget for every internal insertion decision's direct
        # bypass. Meals retain the established contract; formal ExperienceSpec
        # anchors use the same Provider-only ΔG seam.
        bypass_pairs: set[tuple[str, str]] = set()
        for day_anchors in anchors_by_day.values():
            for index, item in enumerate(day_anchors):
                insertion_decision = item["kind"] == "meal" or self._requires_provider_insertion_decision(item)
                if insertion_decision and 0 < index < len(day_anchors) - 1:
                    bypass_pairs.add((day_anchors[index - 1]["id"], day_anchors[index + 1]["id"]))
        bypass_segment_ids = {segment_id for pair in bypass_pairs for segment_id in pair}
        bypass_segments = [item["segment"] for item in anchors if item["id"] in bypass_segment_ids]
        bypass_grouped: dict[tuple[str, str], list[RouteOption]] = {}
        provider_bypass_pairs = (
            bypass_pairs
            if scoped_bypass_pairs is None
            else bypass_pairs & scoped_bypass_pairs
        )
        independent_bypass_scopes: dict[tuple[str, str], dict[str, Any]] = {}
        if repair_scope_certificate is not None:
            # Replacing one anchor can invalidate the sealed direct-bypass leg
            # of a neighbouring insertion (for example, a meal immediately
            # before the replaced stop).  That work belongs to the insertion's
            # own exact slot scope; never widen the replacement certificate or
            # combine both scopes into one Provider request.
            for day_anchors in anchors_by_day.values():
                for index, insertion in enumerate(day_anchors):
                    if not (
                        insertion["kind"] == "meal"
                        or self._requires_provider_insertion_decision(insertion)
                    ) or not (0 < index < len(day_anchors) - 1):
                        continue
                    previous_item = day_anchors[index - 1]
                    next_item = day_anchors[index + 1]
                    bypass_pair = (previous_item["id"], next_item["id"])
                    if bypass_pair in provider_bypass_pairs:
                        continue
                    retained = self._segment_semantic(insertion).get(
                        "routeInsertionMatrixProof"
                    )
                    if not isinstance(retained, dict):
                        continue
                    expected_endpoints = {
                        "previousSegmentId": previous_item["id"],
                        "previousAmapId": self._route_anchor_amap_id(previous_item),
                        "nextSegmentId": next_item["id"],
                        "nextAmapId": self._route_anchor_amap_id(next_item),
                    }
                    if retained.get("baselineEndpoints") == expected_endpoints:
                        continue
                    insertion_scope = self._route_repair_scope_certificate(
                        snapshot,
                        segment=insertion,
                        candidate=self._segment_poi_payload(insertion),
                        transport_mode=transport_mode,
                    )
                    if insertion_scope is None:
                        return authorization_failure(
                            _RouteAuthorizationPreconditionError(
                                "affected_insertion_scope_missing"
                            )
                        )
                    try:
                        _adjacent, insertion_bypass_pairs = (
                            self._repair_scope_route_pairs(snapshot, insertion_scope)
                        )
                    except _RouteAuthorizationPreconditionError as error:
                        return authorization_failure(error)
                    if insertion_bypass_pairs != {bypass_pair}:
                        return authorization_failure(
                            _RouteAuthorizationPreconditionError(
                                "affected_insertion_scope_mismatch"
                            )
                        )
                    independent_bypass_scopes[bypass_pair] = insertion_scope
        if provider_bypass_pairs:
            try:
                self._authorize_route_work(
                    snapshot,
                    route_pairs=provider_bypass_pairs,
                    mode=transport_mode,
                    reason="insertion_bypass",
                    condition="always",
                    repair_scope_certificate=repair_scope_certificate,
                )
            except _RouteAuthorizationPreconditionError as error:
                return authorization_failure(error)
            route_accounting = self._route_accounting_start()
            try:
                bypass_routes = build_routes(
                    f"{preview_id}_meal_bypass",
                    pois,
                    transport_mode=transport_mode,
                    segments=bypass_segments,
                    route_pairs=provider_bypass_pairs,
                    preferred_mode_only=True,
                )
            except TypeError:
                try:
                    bypass_routes = build_routes(
                        f"{preview_id}_meal_bypass",
                        pois,
                        transport_mode,
                        segments=bypass_segments,
                    )
                except Exception:
                    bypass_routes = []
            except Exception:
                bypass_routes = []
            finally:
                self._record_route_accounting(
                    call_metrics,
                    route_accounting,
                    requested_count=len(provider_bypass_pairs),
                )
            for route in bypass_routes or []:
                pair = (str(route.from_segment_id or ""), str(route.to_segment_id or ""))
                if pair in provider_bypass_pairs:
                    bypass_grouped.setdefault(pair, []).append(route)

        for bypass_pair, insertion_scope in independent_bypass_scopes.items():
            try:
                self._authorize_route_work(
                    snapshot,
                    route_pairs={bypass_pair},
                    mode=transport_mode,
                    reason="insertion_bypass",
                    condition="always",
                    repair_scope_certificate=insertion_scope,
                )
            except _RouteAuthorizationPreconditionError as error:
                return authorization_failure(error)
            route_accounting = self._route_accounting_start()
            try:
                independent_routes = build_routes_for_scope(
                    insertion_scope,
                    f"{preview_id}_affected_insertion_bypass",
                    pois,
                    transport_mode=transport_mode,
                    segments=bypass_segments,
                    route_pairs={bypass_pair},
                    preferred_mode_only=True,
                )
            except Exception:
                independent_routes = []
            finally:
                self._record_route_accounting(
                    call_metrics,
                    route_accounting,
                    requested_count=1,
                )
            for route in independent_routes or []:
                pair = (
                    str(route.from_segment_id or ""),
                    str(route.to_segment_id or ""),
                )
                if pair == bypass_pair:
                    bypass_grouped.setdefault(pair, []).append(route)

        try:
            theme_walking_evidence = self._theme_walking_evidence(
                snapshot,
                anchors_by_day=anchors_by_day,
                route_options=routes,
            )
        except _RouteAuthorizationPreconditionError as error:
            return authorization_failure(error)

        def selected_route(options: list[RouteOption]) -> Optional[RouteOption]:
            selected = next((route for route in options if route.is_selected), None)
            return selected or (
                sorted(options, key=lambda item: (item.duration_seconds, item.distance_meters))[0] if options else None
            )

        schedule_evidence = [
            self._route_evidence_dto(snapshot, selected, left, right)
            for left, right in pairs
            for selected in [selected_route(grouped.get((left["id"], right["id"]), []))]
            if selected is not None
        ]
        time_window_feasible = len(schedule_evidence) == len(pairs) and self._schedule_projection_feasible(
            snapshot, schedule_evidence
        )

        # Every final meal-detour decision is the Provider-network insertion
        # cost G(prev, meal) + G(meal, next) - G(prev, next).  The direct
        # bypass and all explicit cost components are mandatory for an
        # internal meal; Haversine never participates in this decision.
        meal_detours: dict[str, dict[str, Any]] = {}
        meal_bypass_missing: dict[str, dict[str, Any]] = {}
        for day_anchors in anchors_by_day.values():
            for index, meal in enumerate(day_anchors):
                if meal["kind"] != "meal":
                    continue
                previous_item = day_anchors[index - 1] if index > 0 else None
                next_item = day_anchors[index + 1] if index + 1 < len(day_anchors) else None
                incoming_route = (
                    selected_route(grouped.get((previous_item["id"], meal["id"]), []))
                    if previous_item is not None
                    else None
                )
                outgoing_route = (
                    selected_route(grouped.get((meal["id"], next_item["id"]), [])) if next_item is not None else None
                )
                meal_semantic = self._segment_semantic(meal)
                retained_without_adjacent = None
                if (
                    repair_scope_certificate is not None
                    and previous_item is not None
                    and next_item is not None
                    and scoped_bypass_pairs is not None
                    and (previous_item["id"], next_item["id"]) not in scoped_bypass_pairs
                ):
                    retained_matrix = meal_semantic.get("routeInsertionMatrixProof")
                    raw_matrix = (
                        retained_matrix.get("routeMatrix")
                        if isinstance(retained_matrix, dict)
                        else None
                    )
                    retained_without_adjacent = self._validated_retained_route_insertion_proof(
                        replacement_proof=meal_semantic.get("routeInsertionMatrixProof"),
                        previous=previous_item,
                        candidate=meal,
                        following=next_item,
                        incoming=(
                            self._provider_matrix_leg(raw_matrix.get("previousToCandidate"))
                            if isinstance(raw_matrix, dict)
                            else None
                        ),
                        outgoing=(
                            self._provider_matrix_leg(raw_matrix.get("candidateToNext"))
                            if isinstance(raw_matrix, dict)
                            else None
                        ),
                        snapshot=snapshot,
                    )
                if retained_without_adjacent is not None:
                    bypass_route_matrix = retained_without_adjacent["routeMatrix"]
                    incoming = self._provider_matrix_leg(
                        bypass_route_matrix["previousToCandidate"]
                    )
                    outgoing = self._provider_matrix_leg(
                        bypass_route_matrix["candidateToNext"]
                    )
                    direct = self._provider_matrix_leg(
                        bypass_route_matrix["previousToNext"]
                    )
                    if incoming is not None:
                        grouped.setdefault((previous_item["id"], meal["id"]), []).append(
                            self._route_option_from_evidence(
                                bypass_route_matrix["previousToCandidate"]
                            )
                        )
                    if outgoing is not None:
                        grouped.setdefault((meal["id"], next_item["id"]), []).append(
                            self._route_option_from_evidence(
                                bypass_route_matrix["candidateToNext"]
                            )
                        )
                    if direct is not None:
                        bypass_grouped.setdefault(
                            (previous_item["id"], next_item["id"]), []
                        ).append(
                            self._route_option_from_evidence(
                                bypass_route_matrix["previousToNext"]
                            )
                        )
                    continue
                if incoming_route is None and outgoing_route is None:
                    continue
                risk_penalty = self._route_risk_penalty(meal)
                incoming = self._provider_matrix_leg(incoming_route, risk_penalty=risk_penalty)
                outgoing = self._provider_matrix_leg(outgoing_route, risk_penalty=risk_penalty)
                direct_route: Optional[RouteOption] = None
                direct = None
                replacement_proof = (
                    meal_semantic.get("routeReplacementMatrixProof")
                    if isinstance(meal_semantic.get("routeReplacementMatrixProof"), dict)
                    else None
                )
                current_amap_id = str(meal["poi"].amap_id or meal["poi"].id)
                proof_baseline_legs = (
                    replacement_proof.get("baselineLegs")
                    if isinstance(replacement_proof, dict)
                    and str(replacement_proof.get("candidateAmapId") or "") == current_amap_id
                    and isinstance(replacement_proof.get("baselineLegs"), list)
                    else None
                )
                if proof_baseline_legs is not None and len(proof_baseline_legs) == 2:
                    normalized_proof_baseline = [self._provider_matrix_leg(item) for item in proof_baseline_legs]
                    if all(item is not None for item in normalized_proof_baseline):
                        direct = self._combine_provider_legs(*normalized_proof_baseline)
                elif previous_item is not None and next_item is not None:
                    direct_route = selected_route(bypass_grouped.get((previous_item["id"], next_item["id"]), []))
                    direct = self._provider_matrix_leg(direct_route)
                if previous_item is not None and next_item is not None:
                    if incoming is None or outgoing is None or direct is None:
                        meal_bypass_missing[meal["id"]] = {
                            "meal": meal,
                            "previous": previous_item,
                            "next": next_item,
                            "reason": "provider_route_matrix_incomplete",
                        }
                        continue
                decision_policy = self._provider_decision_policy(snapshot, meal)
                if decision_policy is None:
                    meal_bypass_missing[meal["id"]] = {
                        "meal": meal,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_decision_contract_invalid",
                    }
                    continue
                schedule_slack_minutes = self._replacement_schedule_slack(meal)
                matrix_score = self.route_insertion_scorer.score_from_route_matrix(
                    previous_to_candidate=incoming,
                    candidate_to_next=outgoing,
                    previous_to_next=direct,
                    detour_tolerance=decision_policy["detourTolerance"],
                    schedule_slack_minutes=schedule_slack_minutes,
                    time_window_feasible=time_window_feasible,
                    mobility_profile=decision_policy["mobilityProfile"],
                )
                if matrix_score is None:
                    meal_bypass_missing[meal["id"]] = {
                        "meal": meal,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_matrix_incomplete",
                    }
                    continue
                meal_proof = {
                    "basis": (
                        "replacement_two_leg_generalized_cost_delta"
                        if proof_baseline_legs is not None
                        else "provider_generalized_cost_delta"
                    ),
                    "generalizedCostDelta": matrix_score.generalized_cost_delta,
                    "detourRatio": matrix_score.detour_ratio,
                    "detourTolerance": copy.deepcopy(matrix_score.detour_tolerance),
                    "detourLevel": matrix_score.detour_level,
                    "mobilityProfile": copy.deepcopy(matrix_score.mobility_profile),
                    "networkVerified": bool(matrix_score.network_verified),
                    "timeWindowFeasible": time_window_feasible,
                    "scheduleSlackMinutes": schedule_slack_minutes,
                    "routeMatrix": self._proof_route_matrix(
                        previous=previous_item,
                        candidate=meal,
                        following=next_item,
                        previous_to_candidate=incoming,
                        candidate_to_next=outgoing,
                        previous_to_next=direct,
                    ),
                    "candidateEndpoint": {
                        "segmentId": meal["id"],
                        "amapId": self._route_anchor_amap_id(meal),
                    },
                    "baselineEndpoints": {
                        "previousSegmentId": (previous_item["id"] if previous_item is not None else None),
                        "previousAmapId": self._route_anchor_amap_id(previous_item),
                        "nextSegmentId": (next_item["id"] if next_item is not None else None),
                        "nextAmapId": self._route_anchor_amap_id(next_item),
                    },
                    **copy.deepcopy(decision_policy),
                    "verifiedAt": datetime.now(timezone.utc).isoformat(),
                }
                meal_proof = self._seal_route_insertion_proof(meal_proof)
                if meal_proof is None:
                    meal_bypass_missing[meal["id"]] = {
                        "meal": meal,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_proof_fingerprint_failed",
                    }
                    continue
                meal_semantic["routeInsertionMatrixProof"] = copy.deepcopy(meal_proof)
                # A schedule projection failure is handled by the bounded
                # schedule-repair phase with these Provider legs still marked
                # verified.  Do not mislabel it as a route detour here; after
                # repair, the matrix is recomputed and both gates must pass.
                if matrix_score.detour_level == "unacceptable" and time_window_feasible:
                    adjacent_distance = sum(
                        int(item.get("distanceMeters") or 0) for item in (incoming, outgoing) if item is not None
                    )
                    adjacent_duration = sum(
                        int(math.ceil(float(item.get("durationSeconds") or 0) / 60))
                        for item in (incoming, outgoing)
                        if item is not None
                    )
                    meal_detours[meal["id"]] = {
                        "code": "provider_route_matrix_unacceptable",
                        "basis": (
                            "replacement_two_leg_generalized_cost_delta"
                            if proof_baseline_legs is not None
                            else "provider_generalized_cost_delta"
                        ),
                        "previous": previous_item,
                        "next": next_item,
                        "assessedDistanceMeters": int(round(matrix_score.added_distance_km * 1000)),
                        "assessedDurationMinutes": matrix_score.added_duration_minutes,
                        "adjacentDistanceMeters": adjacent_distance,
                        "adjacentDurationMinutes": adjacent_duration,
                        "directDistanceMeters": int(direct.get("distanceMeters") or 0) if direct is not None else None,
                        "directDurationMinutes": (
                            int(math.ceil(float(direct.get("durationSeconds") or 0) / 60))
                            if direct is not None
                            else None
                        ),
                        "generalizedCostDelta": matrix_score.generalized_cost_delta,
                        "detourRatio": matrix_score.detour_ratio,
                        "detourTolerance": copy.deepcopy(matrix_score.detour_tolerance),
                        "mobilityProfile": matrix_score.mobility_profile,
                        "detourToleranceSource": decision_policy["detourToleranceSource"],
                        "detourToleranceFingerprint": decision_policy["detourToleranceFingerprint"],
                        "mobilityProfileSource": decision_policy["mobilityProfileSource"],
                        "mobilityProfileFingerprint": decision_policy["mobilityProfileFingerprint"],
                        "routeDecisionContractSource": decision_policy["routeDecisionContractSource"],
                        "contractFingerprint": decision_policy["contractFingerprint"],
                        "timeWindowFeasible": time_window_feasible,
                    }

        provider_anchor_decisions: dict[str, dict[str, Any]] = {}
        provider_anchor_missing: dict[str, dict[str, Any]] = {}
        for day_anchors in anchors_by_day.values():
            for index, anchor in enumerate(day_anchors):
                if anchor["kind"] == "meal" or not self._is_interior_provider_insertion(
                    day_anchors,
                    index,
                ):
                    continue
                previous_item = day_anchors[index - 1] if index > 0 else None
                next_item = day_anchors[index + 1] if index + 1 < len(day_anchors) else None
                incoming_route = (
                    selected_route(grouped.get((previous_item["id"], anchor["id"]), []))
                    if previous_item is not None
                    else None
                )
                outgoing_route = (
                    selected_route(grouped.get((anchor["id"], next_item["id"]), [])) if next_item is not None else None
                )
                risk_penalty = self._route_risk_penalty(anchor)
                incoming = self._provider_matrix_leg(incoming_route, risk_penalty=risk_penalty)
                outgoing = self._provider_matrix_leg(outgoing_route, risk_penalty=risk_penalty)
                direct = None
                if previous_item is not None and next_item is not None:
                    direct_route = selected_route(bypass_grouped.get((previous_item["id"], next_item["id"]), []))
                    direct = self._provider_matrix_leg(direct_route)
                anchor_semantic = self._segment_semantic(anchor)
                if (
                    repair_scope_certificate is not None
                    and previous_item is not None
                    and next_item is not None
                    and scoped_bypass_pairs is not None
                    and (previous_item["id"], next_item["id"]) not in scoped_bypass_pairs
                    and self._validated_retained_route_insertion_proof(
                        replacement_proof=anchor_semantic.get("routeInsertionMatrixProof"),
                        previous=previous_item,
                        candidate=anchor,
                        following=next_item,
                        incoming=incoming,
                        outgoing=outgoing,
                        snapshot=snapshot,
                    )
                    is not None
                ):
                    continue
                if (
                    incoming is None
                    and outgoing is None
                    or previous_item is not None
                    and next_item is not None
                    and (incoming is None or outgoing is None or direct is None)
                ):
                    provider_anchor_missing[anchor["id"]] = {
                        "anchor": anchor,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_matrix_incomplete",
                    }
                    continue
                decision_policy = self._provider_decision_policy(snapshot, anchor)
                if decision_policy is None:
                    provider_anchor_missing[anchor["id"]] = {
                        "anchor": anchor,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_decision_contract_invalid",
                    }
                    continue
                tolerance = decision_policy["detourTolerance"]
                normalized_window = self._strict_provider_time_window(anchor)
                schedule_slack_minutes = self._replacement_schedule_slack(anchor)
                matrix_score = self.route_insertion_scorer.score_from_route_matrix(
                    previous_to_candidate=incoming,
                    candidate_to_next=outgoing,
                    previous_to_next=direct,
                    detour_tolerance=tolerance,
                    schedule_slack_minutes=schedule_slack_minutes,
                    time_window_feasible=time_window_feasible,
                    mobility_profile=decision_policy["mobilityProfile"],
                )
                if matrix_score is None:
                    provider_anchor_missing[anchor["id"]] = {
                        "anchor": anchor,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_matrix_incomplete",
                    }
                    continue
                route_contract = self._provider_route_contract(anchor)
                proof = {
                    "basis": "provider_generalized_cost_delta",
                    "generalizedCostDelta": matrix_score.generalized_cost_delta,
                    "detourRatio": matrix_score.detour_ratio,
                    "detourTolerance": copy.deepcopy(matrix_score.detour_tolerance),
                    "detourLevel": matrix_score.detour_level,
                    "mobilityProfile": copy.deepcopy(matrix_score.mobility_profile),
                    "networkVerified": bool(matrix_score.network_verified),
                    "timeWindow": copy.deepcopy(normalized_window),
                    "timeWindowFeasible": time_window_feasible,
                    "scheduleSlackMinutes": schedule_slack_minutes,
                    "routeMatrix": self._proof_route_matrix(
                        previous=previous_item,
                        candidate=anchor,
                        following=next_item,
                        previous_to_candidate=incoming,
                        candidate_to_next=outgoing,
                        previous_to_next=direct,
                    ),
                    "candidateEndpoint": {
                        "segmentId": anchor["id"],
                        "amapId": self._route_anchor_amap_id(anchor),
                    },
                    "baselineEndpoints": {
                        "previousSegmentId": (previous_item["id"] if previous_item is not None else None),
                        "previousAmapId": self._route_anchor_amap_id(previous_item),
                        "nextSegmentId": (next_item["id"] if next_item is not None else None),
                        "nextAmapId": self._route_anchor_amap_id(next_item),
                    },
                    **copy.deepcopy(decision_policy),
                    "specFingerprint": str(route_contract.get("specFingerprint") or ""),
                    "verifiedAt": datetime.now(timezone.utc).isoformat(),
                }
                proof = self._seal_route_insertion_proof(proof)
                if proof is None:
                    provider_anchor_missing[anchor["id"]] = {
                        "anchor": anchor,
                        "previous": previous_item,
                        "next": next_item,
                        "reason": "provider_route_proof_fingerprint_failed",
                    }
                    continue
                self._segment_semantic(anchor)["routeInsertionMatrixProof"] = copy.deepcopy(proof)
                if matrix_score.detour_level == "unacceptable":
                    provider_anchor_decisions[anchor["id"]] = {
                        **proof,
                        "code": "provider_route_matrix_unacceptable",
                        "anchor": anchor,
                        "previous": previous_item,
                        "next": next_item,
                    }

        # A missing route after the context-local AMap envelope has denied a
        # route call is not route quality evidence. Keep it recoverable instead
        # of treating it as a genuine no-feasible-route decision.
        route_budget_exhausted = self._route_budget_exhausted()
        evidence: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        warnings: list[str] = []
        day_route_stats: dict[int, dict[str, float]] = {}
        provider_state = "ok"
        for missing in provider_anchor_missing.values():
            provider_state = "budget_exhausted" if route_budget_exhausted else "route_missing"
            anchor = missing["anchor"]
            previous_item = missing["previous"]
            next_item = missing["next"]
            issues.append(
                {
                    "code": "route_budget_exhausted" if route_budget_exhausted else "route_evidence_missing",
                    "evidenceKind": "provider_insertion_bypass",
                    "dayNumber": int(anchor["dayNumber"]),
                    "segmentId": anchor["id"],
                    "planningSlotId": str(anchor.get("planningSlotId") or ""),
                    "poolId": str(anchor.get("poolId") or ""),
                    "briefId": str(anchor.get("briefId") or ""),
                    "fromSegmentId": (previous_item["id"] if previous_item is not None else anchor["id"]),
                    "toSegmentId": (next_item["id"] if next_item is not None else anchor["id"]),
                    "failureReason": str(missing.get("reason") or "provider_route_matrix_incomplete"),
                    "message": "ExperienceSpec 路线决策缺少完整、可核验的 Provider 路线矩阵。",
                }
            )
        for missing in meal_bypass_missing.values():
            provider_state = "budget_exhausted" if route_budget_exhausted else "route_missing"
            meal = missing["meal"]
            previous_item = missing["previous"]
            next_item = missing["next"]
            issues.append(
                {
                    "code": "route_budget_exhausted" if route_budget_exhausted else "route_evidence_missing",
                    "evidenceKind": "meal_bypass",
                    "dayNumber": int(meal["dayNumber"]),
                    "mealSegmentId": meal["id"],
                    "mealPlanningSlotId": str(meal.get("planningSlotId") or ""),
                    "mealPoolId": str(meal.get("poolId") or ""),
                    "mealBriefId": str(meal.get("briefId") or ""),
                    "fromSegmentId": previous_item["id"] if previous_item is not None else meal["id"],
                    "toSegmentId": next_item["id"] if next_item is not None else meal["id"],
                    "fromPoiName": previous_item["poi"].name if previous_item is not None else meal["poi"].name,
                    "toPoiName": next_item["poi"].name if next_item is not None else meal["poi"].name,
                    "failureReason": str(missing.get("reason") or "provider_route_matrix_incomplete"),
                    "message": (f"{meal['poi'].name} 缺少完整、可核验的 Provider 路线矩阵，无法判断餐饮绕行。"),
                }
            )
        emitted_meal_issues: set[str] = set()
        emitted_anchor_issues: set[str] = set()
        for left, right in pairs:
            pair_id = (left["id"], right["id"])
            options = grouped.get(pair_id, [])
            selected = next((route for route in options if route.is_selected), None)
            selected = selected or (
                sorted(options, key=lambda item: (item.duration_seconds, item.distance_meters))[0] if options else None
            )
            if selected is None:
                provider_state = "budget_exhausted" if route_budget_exhausted else "route_missing"
                issues.append(
                    {
                    "code": "route_budget_exhausted" if route_budget_exhausted else "route_evidence_missing",
                        "fromSegmentId": left["id"],
                        "toSegmentId": right["id"],
                        "fromPoiName": left["poi"].name,
                        "toPoiName": right["poi"].name,
                        "message": f"{left['poi'].name} → {right['poi'].name} 缺少可核验路线。",
                    }
                )
                continue
            matrix_leg = self._provider_matrix_leg(selected)
            if matrix_leg is None:
                provider_state = "budget_exhausted" if route_budget_exhausted else "route_matrix_incomplete"
                issues.append(
                    {
                    "code": "route_budget_exhausted" if route_budget_exhausted else "provider_route_matrix_incomplete",
                        "fromSegmentId": left["id"],
                        "toSegmentId": right["id"],
                        "message": "路线 Provider 证据缺少新鲜时间戳或完整成本分量。",
                    }
                )
                continue
            distance_km = float(selected.distance_meters or 0) / 1000
            duration_minutes = max(0, int(math.ceil(float(selected.duration_seconds or 0) / 60)))
            if int(selected.distance_meters or 0) <= 0 or duration_minutes <= 0:
                provider_state = "budget_exhausted" if route_budget_exhausted else "route_missing"
                issues.append(
                    {
                    "code": "route_budget_exhausted" if route_budget_exhausted else "route_evidence_missing",
                        "fromSegmentId": left["id"],
                        "toSegmentId": right["id"],
                        "message": "路线证据缺少正向距离或时长。",
                    }
                )
                continue
            stats = day_route_stats.setdefault(
                int(left["dayNumber"]), {"distanceKm": 0.0, "durationMinutes": 0.0, "maxDistanceKm": 0.0}
            )
            stats["distanceKm"] += distance_km
            stats["durationMinutes"] += duration_minutes
            stats["maxDistanceKm"] = max(stats["maxDistanceKm"], distance_km)
            status = "verified"
            issue_code = ""
            meal_item = left if left["kind"] == "meal" else right if right["kind"] == "meal" else None
            detour = meal_detours.get(meal_item["id"]) if meal_item is not None else None
            if detour is not None:
                issue_code = str(detour["code"])
                status = "rejected"
                meal_semantic = meal_item.get("semantic") or {}
                if meal_item["id"] not in emitted_meal_issues:
                    emitted_meal_issues.add(meal_item["id"])
                    assessed_distance_km = float(detour["assessedDistanceMeters"] or 0) / 1000
                    issues.append(
                        {
                            "code": issue_code,
                            "failureCode": issue_code,
                            "dayNumber": int(meal_item["dayNumber"]),
                            "mealSegmentId": meal_item["id"],
                            "mealPlanningSlotId": str(meal_item.get("planningSlotId") or ""),
                            "mealPoolId": str(meal_item.get("poolId") or ""),
                            "mealBriefId": str(meal_item.get("briefId") or ""),
                            "mealAmapId": str(meal_item["poi"].amap_id or meal_item["poi"].id),
                            "previousSegmentId": (detour["previous"]["id"] if detour["previous"] is not None else None),
                            "nextSegmentId": (detour["next"]["id"] if detour["next"] is not None else None),
                            "occurrenceId": str(meal_semantic.get("occurrenceId") or ""),
                            "routePair": {
                                "fromSegmentId": left["id"],
                                "toSegmentId": right["id"],
                            },
                            "detourTolerance": copy.deepcopy(detour["detourTolerance"]),
                            "mobilityProfile": copy.deepcopy(detour["mobilityProfile"]),
                            "generalizedCostDelta": detour["generalizedCostDelta"],
                            "detourRatio": detour["detourRatio"],
                            "timeWindowFeasible": detour["timeWindowFeasible"],
                            "assessmentBasis": detour["basis"],
                            "adjacentDistanceMeters": detour["adjacentDistanceMeters"],
                            "adjacentDurationMinutes": detour["adjacentDurationMinutes"],
                            "directDistanceMeters": detour["directDistanceMeters"],
                            "directDurationMinutes": detour["directDurationMinutes"],
                            "addedDistanceMeters": detour["assessedDistanceMeters"],
                            "addedDurationMinutes": detour["assessedDurationMinutes"],
                            "fromSegmentId": left["id"],
                            "toSegmentId": right["id"],
                            "fromPoiName": left["poi"].name,
                            "toPoiName": right["poi"].name,
                            "distanceKm": round(assessed_distance_km, 2),
                            "durationMinutes": detour["assessedDurationMinutes"],
                            "message": (
                                f"{meal_item['poi'].name} 的 Provider 路线矩阵增量超出动态容忍度或时间窗不可行。"
                            ),
                        }
                    )
            anchor_item = next(
                (item for item in (left, right) if item["id"] in provider_anchor_decisions),
                None,
            )
            anchor_decision = provider_anchor_decisions.get(anchor_item["id"]) if anchor_item is not None else None
            if anchor_decision is not None:
                issue_code = str(anchor_decision["code"])
                status = "rejected"
                if anchor_item["id"] not in emitted_anchor_issues:
                    emitted_anchor_issues.add(anchor_item["id"])
                    semantic = self._segment_semantic(anchor_item)
                    issues.append(
                        {
                            "code": issue_code,
                            "failureCode": issue_code,
                            "dayNumber": int(anchor_item["dayNumber"]),
                            "segmentId": anchor_item["id"],
                            "planningSlotId": str(anchor_item.get("planningSlotId") or ""),
                            "poolId": str(anchor_item.get("poolId") or ""),
                            "briefId": str(anchor_item.get("briefId") or ""),
                            "occurrenceId": str(semantic.get("occurrenceId") or ""),
                            "previousSegmentId": (
                                anchor_decision["previous"]["id"]
                                if anchor_decision.get("previous") is not None
                                else None
                            ),
                            "nextSegmentId": (
                                anchor_decision["next"]["id"] if anchor_decision.get("next") is not None else None
                            ),
                            "routePair": {
                                "fromSegmentId": left["id"],
                                "toSegmentId": right["id"],
                            },
                            "detourTolerance": copy.deepcopy(anchor_decision["detourTolerance"]),
                            "generalizedCostDelta": anchor_decision["generalizedCostDelta"],
                            "detourRatio": anchor_decision["detourRatio"],
                            "timeWindow": copy.deepcopy(anchor_decision["timeWindow"]),
                            "timeWindowFeasible": anchor_decision["timeWindowFeasible"],
                            "specFingerprint": anchor_decision["specFingerprint"],
                            "assessmentBasis": anchor_decision["basis"],
                            "fromSegmentId": left["id"],
                            "toSegmentId": right["id"],
                            "fromPoiName": left["poi"].name,
                            "toPoiName": right["poi"].name,
                            "message": (
                                f"{anchor_item['poi'].name} 的 ExperienceSpec Provider 路线矩阵增量或时间窗不可接受。"
                            ),
                        }
                    )
            route_dto = self._route_evidence_dto(snapshot, selected, left, right)
            route_dto.update(
                {
                    "status": status,
                    "routeStatus": status,
                    "qualityIssue": issue_code or None,
                    "fromPoiName": left["poi"].name,
                    "toPoiName": right["poi"].name,
                }
            )
            evidence.append(route_dto)
        for day_number, stats in sorted(day_route_stats.items()):
            if stats["maxDistanceKm"] > 35:
                max_distance_km = stats["maxDistanceKm"]
                warnings.append(f"route_leg_distance_diagnostic:day={day_number}:distanceKm={max_distance_km:.2f}")
            if stats["distanceKm"] > 55 or stats["durationMinutes"] > 300:
                warnings.append(
                    "day_route_quality_diagnostic:"
                    f"day={day_number}:distanceKm={stats['distanceKm']:.2f}:"
                    f"durationMinutes={stats['durationMinutes']:.0f}"
                )
        hard_quality_codes = {
            "provider_route_matrix_unacceptable",
        }
        if issues:
            status = "failed" if any(item.get("code") in hard_quality_codes for item in issues) else "pending"
            actions = ["choose_nearby_meal", "retry_route_verification", "open_map_selection"]
        elif len(evidence) != len(pairs):
            status = "pending"
            actions = ["retry_route_verification", "open_map_selection"]
        else:
            status = "passed"
            actions = []
        execution_ledger = {
            "expectedLegCount": len(pairs),
            "submittedLegCount": int(call_metrics.get("requestedLegCount") or 0),
            "cachedLegCount": int(call_metrics.get("cachedLegCount") or 0),
            "requestedLegCount": int(call_metrics.get("requestedLegCount") or 0),
            "completedLegCount": len(
                {
                    (str(item.get("fromSegmentId") or ""), str(item.get("toSegmentId") or ""))
                    for item in evidence
                    if isinstance(item, dict)
                }
            ),
            "verifiedLegCount": len([item for item in evidence if ProposalRouteEvidenceNormalizer.is_verified(item)]),
            "failedLegCount": max(0, len(pairs) - len(evidence)),
            "providerCallCount": int(call_metrics.get("providerCallCount") or 0),
            "providerCacheHitCount": int(call_metrics.get("providerCacheHitCount") or 0),
            "cancelledLegCount": 0,
            "retainedEvidenceCount": len(evidence),
            "workerFailureClass": None,
            "sanitizedWorkerFailureMessage": None,
            "workerTimedOut": False,
            "workerTimeoutMs": None,
            "budgetExhausted": route_budget_exhausted,
        }
        call_metrics["completedLegCount"] = execution_ledger["completedLegCount"]
        call_metrics["verifiedLegCount"] = execution_ledger["verifiedLegCount"]
        call_metrics["failedLegCount"] = execution_ledger["failedLegCount"]
        return {
            "status": status,
            "evidence": evidence,
            "themeWalkingEvidence": theme_walking_evidence,
            "issues": issues,
            "warnings": warnings,
            "actions": actions,
            "provider_state": provider_state,
            "executionLedger": execution_ledger,
        }

    def _theme_walking_evidence(
        self,
        snapshot: dict[str, Any],
        *,
        anchors_by_day: dict[int, list[dict[str, Any]]],
        route_options: list[RouteOption],
    ) -> list[dict[str, Any]]:
        """Reuse verified walking fallback evidence without issuing extra work."""

        def admitted_area(item: dict[str, Any]) -> tuple[bool, str]:
            semantic = item.get("semantic") if isinstance(item.get("semantic"), dict) else {}
            family = str(semantic.get("optionalExperienceFamily") or semantic.get("family") or "").casefold()
            coverage_roles = {str(role).casefold() for role in semantic.get("coverageRoles") or []}
            report = (
                semantic.get("consumerAdmissionReport")
                if isinstance(semantic.get("consumerAdmissionReport"), dict)
                else {}
            )
            role_reports = (
                semantic.get("coverageRoleAdmissionReports")
                if isinstance(semantic.get("coverageRoleAdmissionReports"), dict)
                else {}
            )
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            if not report and isinstance(raw.get("consumerAdmissionReport"), dict):
                report = raw["consumerAdmissionReport"]
            admitted = bool(
                report.get("scoreEligible") is True or str(report.get("classification") or "").startswith("admitted_")
            )
            area_role_report = role_reports.get("area_walk_anchor")
            area_role_admitted = bool(
                isinstance(area_role_report, dict)
                and (
                    area_role_report.get("scoreEligible") is True
                    or str(area_role_report.get("classification") or "").startswith("admitted_")
                )
            )
            brief_id = str(
                semantic.get("creativeBriefId")
                or semantic.get("briefId")
                or semantic.get("brief_id")
                or item.get("briefId")
                or ""
            )
            return (
                admitted
                and (
                    family in self.AREA_WALK_FAMILIES or ("area_walk_anchor" in coverage_roles and area_role_admitted)
                ),
                brief_id,
            )

        target_items: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for day_anchors in anchors_by_day.values():
            admitted_by_brief: dict[str, list[dict[str, Any]]] = {}
            for item in day_anchors:
                is_admitted, brief_id = admitted_area(item)
                if is_admitted and brief_id:
                    admitted_by_brief.setdefault(brief_id, []).append(item)
            for scoped_items in admitted_by_brief.values():
                for left, right in zip(scoped_items, scoped_items[1:]):
                    left_amap_id = str(left["poi"].amap_id or left["poi"].id)
                    right_amap_id = str(right["poi"].amap_id or right["poi"].id)
                    if left_amap_id and left_amap_id != right_amap_id:
                        target_items[(left["id"], right["id"])] = (left, right)
        if not target_items:
            return []

        segment_days = {
            item["id"]: int(item["dayNumber"]) for day_anchors in anchors_by_day.values() for item in day_anchors
        }
        retained: dict[tuple[str, str], dict[str, Any]] = {}
        missing_pairs = set(target_items) - set(retained)
        if missing_pairs:
            # Walking evidence may only come from the conditional fallback in
            # `_evaluate_snapshot`, after the exact preferred transit pair was
            # unavailable. Theme completion must never authorize or request a
            # second walking route merely to improve presentation quality.
            for route in route_options:
                pair = (
                    str(route.from_segment_id or ""),
                    str(route.to_segment_id or ""),
                )
                if pair not in missing_pairs or normalize_route_mode(str(route.mode or "")) != "walking":
                    continue
                left, right = target_items[pair]
                route_dto = ProposalRouteEvidenceNormalizer.route_option_dto(
                    route,
                    from_segment_id=left["id"],
                    to_segment_id=right["id"],
                    from_poi_id=str(left["raw"].get("poi", {}).get("id") or left["poi"].id),
                    to_poi_id=str(right["raw"].get("poi", {}).get("id") or right["poi"].id),
                    from_amap_id=str(left["poi"].amap_id or ""),
                    to_amap_id=str(right["poi"].amap_id or ""),
                    day_number=int(left["dayNumber"]),
                    candidate_fingerprint=self._candidate_fingerprint(snapshot),
                    proposal_id=str(snapshot.get("proposalId") or ""),
                )
                route_dto.update(
                    {
                        "status": "verified",
                        "routeStatus": "verified",
                        "evidenceKind": "area_walk_relation",
                        "isSelected": False,
                        "fromPoiName": left["poi"].name,
                        "toPoiName": right["poi"].name,
                    }
                )
                if ProposalRouteEvidenceNormalizer.is_verified(route_dto, segment_days):
                    retained[pair] = route_dto

        return [copy.deepcopy(retained[pair]) for pair in target_items if pair in retained]

    def _active_route_service(self) -> RouteService:
        """Return the route adapter active for this execution context."""
        if type(self.route_service) is RouteService and not getattr(self.route_service, "map_provider_key", None):
            return RouteService()
        return self.route_service

    @staticmethod
    def _repair_scope_certificate_for_mode(
        certificate: Optional[dict[str, Any]],
        *,
        mode: str,
    ) -> Optional[dict[str, Any]]:
        """Re-sign one exact scope when a conditional route mode is materialized."""

        if certificate is None:
            return None
        normalized_mode = normalize_route_mode(str(mode or "").strip().casefold())
        material = {
            key: copy.deepcopy(value)
            for key, value in certificate.items()
            if key not in {"scopeFingerprint", "issueCodes"}
        }
        raw_keys = material.get("adjacentRouteLedgerKeys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise _RouteAuthorizationPreconditionError("route_scope_certificate_invalid")
        material["adjacentRouteLedgerKeys"] = [
            {**copy.deepcopy(item), "mode": normalized_mode}
            for item in raw_keys
            if isinstance(item, dict)
        ]
        if len(material["adjacentRouteLedgerKeys"]) != len(raw_keys):
            raise _RouteAuthorizationPreconditionError("route_scope_certificate_invalid")
        material["scopeFingerprint"] = canonical_fingerprint(material)
        if isinstance(certificate.get("issueCodes"), list):
            material["issueCodes"] = copy.deepcopy(certificate["issueCodes"])
        return material

    def _repair_scope_route_pairs(
        self,
        snapshot: dict[str, Any],
        certificate: dict[str, Any],
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        """Rebind a persisted physical certificate to its current logical legs."""

        if not isinstance(certificate, dict):
            raise _RouteAuthorizationPreconditionError("route_scope_certificate_invalid")
        planning_slot_id = str(certificate.get("planningSlotId") or "").strip()
        candidate_physical_id = str(certificate.get("candidatePhysicalId") or "").strip().upper()
        adjacent_anchor_ids = certificate.get("adjacentAnchorIds")
        if not planning_slot_id or not candidate_physical_id or not isinstance(adjacent_anchor_ids, list):
            raise _RouteAuthorizationPreconditionError("route_scope_certificate_invalid")
        candidates = [
            item
            for item in self._snapshot_segments(snapshot)
            if str(item.get("planningSlotId") or "").strip() == planning_slot_id
            and self._canonical_physical_id(self._segment_poi_payload(item))
            == candidate_physical_id
        ]
        if len(candidates) != 1:
            raise _RouteAuthorizationPreconditionError("route_scope_candidate_mismatch")
        candidate_segment_id = str(candidates[0].get("id") or "").strip()
        if not candidate_segment_id:
            raise _RouteAuthorizationPreconditionError("route_scope_candidate_mismatch")
        ordered_anchor_ids = [str(item or "").strip() for item in adjacent_anchor_ids]
        if (
            not ordered_anchor_ids
            or any(not item for item in ordered_anchor_ids)
            or len(set(ordered_anchor_ids)) != len(ordered_anchor_ids)
        ):
            raise _RouteAuthorizationPreconditionError("route_scope_anchor_mismatch")
        adjacent_pairs: set[tuple[str, str]] = set()
        if len(ordered_anchor_ids) == 1:
            all_segments = self._snapshot_segments(snapshot)
            segment_positions = {
                str(item.get("id") or ""): index for index, item in enumerate(all_segments)
            }
            anchor_id = ordered_anchor_ids[0]
            if anchor_id not in segment_positions or candidate_segment_id not in segment_positions:
                raise _RouteAuthorizationPreconditionError("route_scope_anchor_mismatch")
            adjacent_pairs.add(
                (anchor_id, candidate_segment_id)
                if segment_positions[anchor_id] < segment_positions[candidate_segment_id]
                else (candidate_segment_id, anchor_id)
            )
        elif len(ordered_anchor_ids) == 2:
            adjacent_pairs.update(
                {
                    (ordered_anchor_ids[0], candidate_segment_id),
                    (candidate_segment_id, ordered_anchor_ids[1]),
                }
            )
        else:
            raise _RouteAuthorizationPreconditionError("route_scope_anchor_mismatch")
        expected = self._route_repair_scope_certificate(
            snapshot,
            segment=candidates[0],
            candidate=self._segment_poi_payload(candidates[0]),
            transport_mode=normalize_route_mode(
                str((certificate.get("adjacentRouteLedgerKeys") or [{}])[0].get("mode") or "")
            ),
        )
        if expected is None or any(
            expected.get(key) != certificate.get(key)
            for key in (
                "rootPortfolioId",
                "planningSelectionRootTurnId",
                "briefId",
                "poolId",
                "dayNumber",
                "planningSlotId",
                "candidatePhysicalId",
                "adjacentAnchorIds",
                "adjacentRouteLedgerKeys",
                "routeContractFingerprint",
                "scopeFingerprint",
            )
        ):
            raise _RouteAuthorizationPreconditionError("route_scope_certificate_mismatch")
        bypass_pairs = (
            {(ordered_anchor_ids[0], ordered_anchor_ids[1])}
            if len(ordered_anchor_ids) == 2
            else set()
        )
        return adjacent_pairs, bypass_pairs

    def _authorize_route_work(
        self,
        snapshot: dict[str, Any],
        *,
        mode: str,
        reason: str,
        condition: str,
        route_pairs: Optional[Iterable[tuple[str, str]]] = None,
        route_ledger_keys: Optional[Iterable[dict[str, Any]]] = None,
        candidate_physical_id: str = "",
        repair_scope_certificate: Optional[dict[str, Any]] = None,
    ) -> None:
        """Authorize only exact canonical pair/mode work derived from this snapshot."""

        normalized_mode = normalize_route_mode(str(mode or "").strip().casefold())
        if normalized_mode not in {"walking", "bicycling", "transit", "driving", "taxi"}:
            raise _RouteAuthorizationPreconditionError("route_authorization_mode_invalid")
        if route_pairs is not None and route_ledger_keys is not None:
            raise _RouteAuthorizationPreconditionError("route_authorization_scope_ambiguous")

        requests: list[dict[str, Any]] = []
        normalized_candidate_id = str(candidate_physical_id or "").strip().upper()
        if route_pairs is not None:
            anchors = [
                item
                for item in self._snapshot_segments(snapshot)
                if item.get("route_anchor") and item.get("poi") is not None
            ]
            by_id = {str(item.get("id") or ""): item for item in anchors}
            if len(by_id) != len(anchors) or "" in by_id:
                raise _RouteAuthorizationPreconditionError("route_authorization_segment_identity_invalid")
            positions = {
                str(item.get("id") or ""): (int(item.get("dayNumber") or 0), index)
                for day_number in sorted({int(item.get("dayNumber") or 0) for item in anchors})
                for index, item in enumerate(
                    [anchor for anchor in anchors if int(anchor.get("dayNumber") or 0) == day_number]
                )
            }
            for raw_pair in sorted(
                route_pairs,
                key=lambda pair: (
                    str(pair[0]) if len(pair) > 0 else "",
                    str(pair[1]) if len(pair) > 1 else "",
                ),
            ):
                if not isinstance(raw_pair, (tuple, list)) or len(raw_pair) != 2:
                    raise _RouteAuthorizationPreconditionError("route_authorization_pair_invalid")
                from_segment_id = str(raw_pair[0] or "")
                to_segment_id = str(raw_pair[1] or "")
                left = by_id.get(from_segment_id)
                right = by_id.get(to_segment_id)
                if left is None or right is None:
                    raise _RouteAuthorizationPreconditionError("route_authorization_topology_missing")
                left_position = positions.get(from_segment_id)
                right_position = positions.get(to_segment_id)
                if (
                    left_position is None
                    or right_position is None
                    or left_position[0] != right_position[0]
                    or left_position[1] >= right_position[1]
                ):
                    raise _RouteAuthorizationPreconditionError("route_authorization_topology_invalid")
                position_delta = right_position[1] - left_position[1]
                if reason in {"baseline_adjacent", "conditional_walking", "replacement_adjacent"}:
                    if position_delta != 1:
                        raise _RouteAuthorizationPreconditionError("route_authorization_topology_not_adjacent")
                elif reason == "insertion_bypass":
                    day_anchors = [
                        anchor
                        for anchor in anchors
                        if int(anchor.get("dayNumber") or 0) == left_position[0]
                    ]
                    if position_delta != 2 or not (
                        day_anchors[left_position[1] + 1].get("kind") == "meal"
                        or self._requires_provider_insertion_decision(day_anchors[left_position[1] + 1])
                    ):
                        raise _RouteAuthorizationPreconditionError("route_authorization_bypass_topology_invalid")

                left_payload = self._segment_poi_payload(left)
                right_payload = self._segment_poi_payload(right)
                if PoiPhysicalIdentityService.invalid_parent_id(
                    left_payload
                ) or PoiPhysicalIdentityService.invalid_parent_id(right_payload):
                    raise _RouteAuthorizationPreconditionError("route_authorization_parent_identity_invalid")
                from_physical_id = self._canonical_physical_id(left_payload)
                to_physical_id = self._canonical_physical_id(right_payload)
                if not from_physical_id or not to_physical_id:
                    raise _RouteAuthorizationPreconditionError("route_authorization_identity_missing")
                if from_physical_id == to_physical_id:
                    raise _RouteAuthorizationPreconditionError("route_authorization_same_physical_endpoint")
                requests.append(
                    {
                        "fromPhysicalId": from_physical_id,
                        "toPhysicalId": to_physical_id,
                        "mode": normalized_mode,
                        "reason": reason,
                        "condition": condition,
                        **(
                            {"candidatePhysicalId": normalized_candidate_id}
                            if normalized_candidate_id
                            else {}
                        ),
                        **(
                            {"repairScopeCertificate": copy.deepcopy(repair_scope_certificate)}
                            if isinstance(repair_scope_certificate, dict)
                            else {}
                        ),
                    }
                )
        elif route_ledger_keys is not None:
            for raw in route_ledger_keys:
                if not isinstance(raw, dict):
                    raise _RouteAuthorizationPreconditionError("route_authorization_pair_invalid")
                from_physical_id = str(raw.get("fromPhysicalId") or "").strip().upper()
                to_physical_id = str(raw.get("toPhysicalId") or "").strip().upper()
                key_mode = normalize_route_mode(str(raw.get("mode") or normalized_mode).strip().casefold())
                if not from_physical_id or not to_physical_id:
                    raise _RouteAuthorizationPreconditionError("route_authorization_identity_missing")
                if from_physical_id == to_physical_id:
                    raise _RouteAuthorizationPreconditionError("route_authorization_same_physical_endpoint")
                if key_mode != normalized_mode:
                    raise _RouteAuthorizationPreconditionError("route_authorization_mode_mismatch")
                if normalized_candidate_id and normalized_candidate_id not in {
                    from_physical_id,
                    to_physical_id,
                }:
                    raise _RouteAuthorizationPreconditionError("route_authorization_candidate_topology_invalid")
                requests.append(
                    {
                        "fromPhysicalId": from_physical_id,
                        "toPhysicalId": to_physical_id,
                        "mode": normalized_mode,
                        "reason": reason,
                        "condition": condition,
                        **(
                            {"candidatePhysicalId": normalized_candidate_id}
                            if normalized_candidate_id
                            else {}
                        ),
                        **(
                            {"repairScopeCertificate": copy.deepcopy(repair_scope_certificate)}
                            if isinstance(repair_scope_certificate, dict)
                            else {}
                        ),
                    }
                )
        else:
            raise _RouteAuthorizationPreconditionError("route_authorization_scope_missing")

        if not requests:
            raise _RouteAuthorizationPreconditionError("route_authorization_scope_empty")
        budget = current_amap_call_budget()
        if (
            budget is not None
            and getattr(budget, "source", "") == "creative_portfolio_route_preflight"
        ):
            if budget.authorize_route_work(requests) is not True:
                raise _RouteAuthorizationPreconditionError(
                    str(getattr(budget, "last_denial_reason", "") or "route_authorization_invalid")
                )

    @staticmethod
    def _authorize_nearby_work(
        *,
        center: dict[str, float | str],
        keyword: str,
        category: str,
        radius: int,
    ) -> None:
        """Authorize one exact nearby query only when its loop iteration executes."""

        try:
            longitude = float(center["longitude"])
            latitude = float(center["latitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise _RouteAuthorizationPreconditionError("nearby_authorization_center_invalid") from error
        if (
            not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not keyword.strip()
            or radius <= 0
        ):
            raise _RouteAuthorizationPreconditionError("nearby_authorization_scope_invalid")
        budget = current_amap_call_budget()
        if (
            budget is not None
            and getattr(budget, "source", "") == "creative_portfolio_route_preflight"
        ):
            budget.authorize_place_around_work(
                [
                    {
                        "center": f"{longitude},{latitude}",
                        "keyword": keyword,
                        "category": category,
                        "radius": str(radius),
                        "reason": "route_repair_nearby",
                    }
                ]
            )

    @staticmethod
    def _route_accounting_start() -> tuple[object | None, int, int]:
        budget = current_amap_call_budget()
        if budget is None:
            return None, 0, 0
        snapshot = budget.snapshot()
        return (
            budget,
            int(snapshot.get("usedRoute") or 0),
            int(snapshot.get("cacheHitCount") or 0),
        )

    @staticmethod
    def _route_budget_exhausted() -> bool:
        budget = current_amap_call_budget()
        if budget is None:
            return False
        snapshot = budget.snapshot()
        return any(
            str(item.get("reason") or "") == "budget_exceeded"
            and str(item.get("endpoint") or "").startswith("route/")
            for item in snapshot.get("skipped") or []
            if isinstance(item, dict)
        )

    @staticmethod
    def _record_route_accounting(
        call_metrics: dict[str, int],
        started: tuple[object | None, int, int],
        *,
        requested_count: int,
    ) -> None:
        """Record actual provider/cache deltas; fakes retain request-count semantics."""

        started_budget, used_before, cache_before = started
        active_budget = current_amap_call_budget()
        if started_budget is not None and active_budget is started_budget:
            snapshot = active_budget.snapshot()
            provider_delta = max(
                0,
                int(snapshot.get("usedRoute") or 0) - used_before,
            )
            cache_delta = max(
                0,
                int(snapshot.get("cacheHitCount") or 0) - cache_before,
            )
        else:
            provider_delta = max(0, int(requested_count or 0))
            cache_delta = 0
        call_metrics["routeCallCount"] += provider_delta
        call_metrics["providerCallCount"] += provider_delta
        call_metrics["providerCacheHitCount"] += cache_delta

    def _find_pool_replacement(
        self,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        city: str,
        transport_mode: str,
        preview_id: str,
        call_metrics: dict[str, int],
        repair_ledger: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if not self._segment_replacement_allowed(segment):
            return None
        lineage = self._segment_lineage(segment)
        current_scope = self._route_repair_scope_certificate(
            snapshot,
            segment=segment,
            candidate=self._segment_poi_payload(segment),
            transport_mode=transport_mode,
        )
        if current_scope is None:
            self._record_repair_rejection(
                repair_ledger,
                "candidate_route_scope_missing",
                self._segment_poi_payload(segment),
            )
            return None
        # Establish the current, provider-backed route cost once.  Geometry is
        # used only to bound candidate discovery; the replacement decision
        # below compares actual route legs against this same itinerary state.
        baseline_result = self._evaluate_snapshot(
            copy.deepcopy(snapshot),
            city=city,
            transport_mode=transport_mode,
            preview_id=f"{preview_id}_baseline",
            call_metrics=call_metrics,
            repair_scope_certificate=current_scope,
        )
        baseline_legs = self._replacement_route_legs(
            baseline_result,
            snapshot=snapshot,
            segment_id=str(segment.get("id") or ""),
        )
        ranked: list[tuple[tuple[float, int, str], dict[str, Any]]] = []
        for candidate in candidates:
            if not self._candidate_matches_lineage(candidate, lineage):
                self._record_repair_rejection(repair_ledger, "lineage_mismatch")
                continue
            repair_ledger["existingPoolCandidateCount"] += 1
            candidate_id = str(candidate.get("amapId") or candidate.get("id") or "")
            current_poi = segment.get("poi")
            current_id = str(
                getattr(current_poi, "amap_id", "")
                or (current_poi.get("amapId") if isinstance(current_poi, dict) else "")
                or ""
            )
            if not candidate_id or candidate_id == current_id:
                self._record_repair_rejection(
                    repair_ledger,
                    "same_or_missing_identity",
                    candidate,
                    scope_fingerprint=self._route_bad_checkpoint_scope_for_segment(
                        repair_ledger,
                        snapshot=snapshot,
                        segment=segment,
                        transport_mode=transport_mode,
                    ),
                )
                continue
            if not self._candidate_grounded(candidate, city=city):
                self._record_repair_rejection(
                    repair_ledger,
                    "grounding_failed",
                    candidate,
                    scope_fingerprint=self._route_bad_checkpoint_scope_for_segment(
                        repair_ledger,
                        snapshot=snapshot,
                        segment=segment,
                        transport_mode=transport_mode,
                    ),
                )
                continue
            # Geometry is deliberately non-decisive; it may only influence a
            # bounded discovery order.  Pool candidates proceed to Provider
            # matrix evaluation regardless of straight-line distance.
            probe = self._reserve_replacement_candidate(
                repair_ledger,
                snapshot=snapshot,
                segment_id=str(segment.get("id") or ""),
                candidate=candidate,
                city=city,
                transport_mode=transport_mode,
            )
            if probe is None:
                continue
            repair_ledger["routePairEvaluationCount"] += len(self._touched_route_pairs(snapshot, segment["id"]))
            trial = copy.deepcopy(snapshot)
            self._replace_segment_candidate(trial, segment["id"], candidate)
            result = self._evaluate_snapshot(
                trial,
                city=city,
                transport_mode=transport_mode,
                preview_id=preview_id,
                call_metrics=call_metrics,
                repair_scope_certificate=self._repair_scope_certificate_from_probe(probe),
            )
            if self._replacement_route_matrix_complete(
                result,
                snapshot=trial,
                segment_id=str(segment.get("id") or ""),
            ):
                candidate_legs = self._replacement_route_legs(
                    result,
                    snapshot=trial,
                    segment_id=str(segment.get("id") or ""),
                )
                matrix_score = self._replacement_matrix_score(
                    baseline_legs,
                    candidate_legs,
                    segment=segment,
                    candidate=candidate,
                    trial_snapshot=trial,
                    route_evidence=list(result.get("evidence") or []),
                )
                if matrix_score is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                        scope_fingerprint=str(probe.get("routeBadCheckpointScopeFingerprint") or ""),
                    )
                    continue
                accepted_candidate = self._candidate_with_matrix_proof(
                    candidate,
                    baseline_legs=baseline_legs,
                    candidate_legs=candidate_legs,
                    matrix_score=matrix_score,
                    decision_policy=self._provider_decision_policy(trial, segment, candidate),
                )
                if accepted_candidate is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                        scope_fingerprint=str(probe.get("routeBadCheckpointScopeFingerprint") or ""),
                    )
                    continue
                scoped_matrix_proof = self._bind_matrix_proof_to_repair_scope(
                    accepted_candidate["_routeReplacementMatrixProof"],
                    probe,
                )
                if scoped_matrix_proof is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                        scope_fingerprint=str(
                            probe.get("routeBadCheckpointScopeFingerprint") or ""
                        ),
                    )
                    continue
                accepted_candidate["_routeReplacementMatrixProof"] = scoped_matrix_proof
                self._record_completed_candidate_matrix(
                    repair_ledger,
                    probe=probe,
                    candidate=candidate,
                    matrix_proof=accepted_candidate["_routeReplacementMatrixProof"],
                )
                if matrix_score.detour_level == "unacceptable":
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_unacceptable",
                        candidate,
                        scope_fingerprint=str(probe.get("routeBadCheckpointScopeFingerprint") or ""),
                    )
                    continue
                accepted_candidate["_exactRouteRepairScopeCertificate"] = (
                    self._repair_scope_certificate_from_probe(probe)
                )
                ranked.append(
                    (
                        (
                            float(matrix_score.generalized_cost_delta or 0.0),
                            int(matrix_score.estimated_duration_minutes),
                            candidate_id,
                        ),
                        accepted_candidate,
                    )
                )
            else:
                self._record_repair_rejection(
                    repair_ledger,
                    "route_quality_failed",
                    candidate,
                    scope_fingerprint=str(probe.get("routeBadCheckpointScopeFingerprint") or ""),
                )
        return min(ranked, key=lambda item: item[0])[1] if ranked else None

    def _replacement_route_matrix_complete(
        self,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        segment_id: str,
    ) -> bool:
        # Candidate ranking consumes Provider measurements, not the aggregate
        # status of the whole snapshot.  Requiring both exact touched legs here
        # keeps missing/partial matrices out; the unchanged insertion scorer and
        # the final verified re-evaluation remain the acceptance gates.
        return self._replacement_route_legs(
            result,
            snapshot=snapshot,
            segment_id=segment_id,
        ) is not None

    def _candidate_with_matrix_proof(
        self,
        candidate: dict[str, Any],
        *,
        baseline_legs: Optional[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]],
        candidate_legs: Optional[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]],
        matrix_score: Any,
        decision_policy: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if baseline_legs is None or candidate_legs is None or decision_policy is None:
            return None
        normalized_baseline = [
            normalized
            for item in baseline_legs
            if item is not None
            for normalized in [self._provider_matrix_leg(item)]
        ]
        normalized_candidate = [
            normalized
            for item in candidate_legs
            if item is not None
            for normalized in [self._provider_matrix_leg(item)]
        ]
        if (
            not normalized_baseline
            or len(normalized_baseline) != len(normalized_candidate)
            or any(item is None for item in (*normalized_baseline, *normalized_candidate))
        ):
            return None
        accepted = copy.deepcopy(candidate)
        proof = {
            "basis": (
                "old_two_leg_vs_candidate_two_leg_delta"
                if len(normalized_baseline) == 2
                else "old_one_leg_vs_candidate_one_leg_delta"
            ),
            "candidateAmapId": str(candidate.get("amapId") or candidate.get("id") or ""),
            "baselineLegs": normalized_baseline,
            "candidateLegs": normalized_candidate,
            "generalizedCostDelta": matrix_score.generalized_cost_delta,
            "detourRatio": matrix_score.detour_ratio,
            "detourTolerance": copy.deepcopy(matrix_score.detour_tolerance),
            "detourLevel": matrix_score.detour_level,
            "mobilityProfile": copy.deepcopy(matrix_score.mobility_profile),
            "networkVerified": bool(matrix_score.network_verified),
            "timeWindowFeasible": True,
            **copy.deepcopy(decision_policy),
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
        }
        fingerprint = self._replacement_matrix_proof_fingerprint(proof)
        if fingerprint is None:
            return None
        proof["proofFingerprint"] = fingerprint
        accepted["_routeReplacementMatrixProof"] = proof
        return accepted

    def _replacement_route_legs(
        self,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        segment_id: str,
    ) -> Optional[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]]:
        """Return verified Provider legs adjacent to one route anchor.

        Interior anchors have two legs.  A first/last meal has one real leg and
        must remain repairable: otherwise one bad endpoint candidate can make a
        complete day permanently unverifiable even when AMap has a nearby
        replacement along the route.
        """
        previous, following = self._adjacent_anchor_items(snapshot, segment_id)
        if previous is None and following is None:
            return None
        previous_id = str(previous.get("id") or "") if previous is not None else ""
        following_id = str(following.get("id") or "") if following is not None else ""
        incoming = next(
            (
                item
                for item in result.get("evidence") or []
                if isinstance(item, dict)
                and previous_id
                and str(item.get("fromSegmentId") or "") == previous_id
                and str(item.get("toSegmentId") or "") == segment_id
            ),
            None,
        )
        outgoing = next(
            (
                item
                for item in result.get("evidence") or []
                if isinstance(item, dict)
                and str(item.get("fromSegmentId") or "") == segment_id
                and following_id
                and str(item.get("toSegmentId") or "") == following_id
            ),
            None,
        )
        if previous is not None and not isinstance(incoming, dict):
            return None
        if following is not None and not isinstance(outgoing, dict):
            return None
        return (incoming if isinstance(incoming, dict) else None, outgoing if isinstance(outgoing, dict) else None)

    def _route_evidence_dto(
        self,
        snapshot: dict[str, Any],
        route: RouteOption,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> dict[str, Any]:
        dto = ProposalRouteEvidenceNormalizer.route_option_dto(
            route,
            from_segment_id=left["id"],
            to_segment_id=right["id"],
            from_poi_id=str(left["raw"].get("poi", {}).get("id") or left["poi"].id),
            to_poi_id=str(right["raw"].get("poi", {}).get("id") or right["poi"].id),
            from_amap_id=str(left["poi"].amap_id or ""),
            to_amap_id=str(right["poi"].amap_id or ""),
            day_number=int(left["dayNumber"]),
            candidate_fingerprint=self._candidate_fingerprint(snapshot),
            proposal_id=str(snapshot.get("proposalId") or ""),
        )
        risk_penalty = max(
            self._route_risk_penalty(left),
            self._route_risk_penalty(right),
        )
        matrix_leg = self._provider_matrix_leg(route, risk_penalty=risk_penalty)
        if matrix_leg is not None:
            dto.update(
                {
                    key: copy.deepcopy(matrix_leg[key])
                    for key in (
                        "walkingDistanceMeters",
                        "transferCount",
                        "waitSeconds",
                        "riskPenaltyMinutes",
                        "costComponentProvenance",
                    )
                }
            )
        return dto

    @classmethod
    def _provider_matrix_leg(
        cls,
        route: Optional[Any],
        *,
        risk_penalty: float = 0.0,
    ) -> Optional[dict[str, Any]]:
        """Normalize one fresh AMap route into explicit generalized-cost fields."""
        if route is None:
            return None
        if isinstance(route, RouteOption):
            provider = str(route.provider or route.source or "")
            source = str(route.source or route.provider or "")
            distance_raw = route.distance_meters
            duration_raw = route.duration_seconds
            queried_at_raw: Any = route.queried_at
            mode = normalize_route_mode(str(route.mode or route.transport_mode or ""))
            payload = route.provider_payload if isinstance(route.provider_payload, dict) else {}
            steps = [item for item in route.steps or [] if isinstance(item, dict)]
            raw: dict[str, Any] = {}
        elif isinstance(route, dict):
            provider = str(route.get("provider") or route.get("source") or "")
            source = str(route.get("source") or route.get("provider") or "")
            distance_raw = route.get("distanceMeters")
            duration_raw = route.get("durationSeconds")
            queried_at_raw = route.get("queriedAt") or route.get("queried_at")
            mode = normalize_route_mode(str(route.get("mode") or route.get("transportMode") or ""))
            payload = route.get("providerPayload") if isinstance(route.get("providerPayload"), dict) else {}
            steps = [item for item in route.get("steps") or [] if isinstance(item, dict)]
            raw = route
        else:
            return None
        canonical = ProposalRouteEvidenceNormalizer.CANONICAL_PROVIDERS
        if provider.casefold() not in canonical and source.casefold() not in canonical:
            return None
        try:
            distance = float(distance_raw)
            duration = float(duration_raw)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) and value > 0 for value in (distance, duration)):
            return None
        queried_at = cls._route_queried_at(queried_at_raw)
        if queried_at is None:
            return None

        def optional_number(*keys: str) -> Optional[float]:
            for container in (raw, payload):
                for key in keys:
                    if key not in container:
                        continue
                    try:
                        value = float(container[key])
                    except (TypeError, ValueError):
                        return None
                    return value if math.isfinite(value) and value >= 0 else None
            return None

        walking = optional_number(
            "walkingDistanceMeters",
            "walking_distance_meters",
            "walking_distance",
        )
        transfers = optional_number("transferCount", "transfer_count", "transfers")
        wait = optional_number("waitSeconds", "wait_seconds")
        explicit_risk = optional_number("riskPenaltyMinutes", "risk_penalty_minutes")
        recognized_steps = [
            item
            for item in steps
            if str(item.get("mode") or "").casefold()
            in {"walking", "transit", "bus", "subway", "rail", "driving", "taxi"}
        ]
        if walking is None:
            if mode == "walking":
                walking = distance
            elif mode in {"driving", "taxi"}:
                walking = 0.0
            elif recognized_steps:
                walking = sum(
                    cls._finite_nonnegative(item.get("distance")) or 0.0
                    for item in recognized_steps
                    if str(item.get("mode") or "").casefold() == "walking"
                )
            else:
                return None
        if transfers is None:
            if mode in {"walking", "driving", "taxi"}:
                transfers = 0.0
            elif recognized_steps:
                transit_legs = len(
                    [
                        item
                        for item in recognized_steps
                        if str(item.get("mode") or "").casefold() in {"transit", "bus", "subway", "rail"}
                    ]
                )
                transfers = float(max(0, transit_legs - 1))
            else:
                return None
        effective_risk = max(
            cls._finite_nonnegative(risk_penalty) or 0.0,
            explicit_risk or 0.0,
        )
        return {
            "provider": AMAP_ROUTE_SOURCE,
            "source": AMAP_ROUTE_SOURCE,
            "mode": mode,
            "distanceMeters": distance,
            "durationSeconds": duration,
            "queriedAt": queried_at.isoformat(),
            "walkingDistanceMeters": walking,
            "transferCount": transfers,
            "waitSeconds": wait if wait is not None else 0.0,
            "riskPenaltyMinutes": effective_risk,
            "costComponentProvenance": {
                "walkingDistance": "provider_payload"
                if optional_number("walkingDistanceMeters", "walking_distance_meters", "walking_distance") is not None
                else "normalized_route_steps",
                "transferCount": "provider_payload"
                if optional_number("transferCount", "transfer_count", "transfers") is not None
                else "normalized_route_steps",
                "wait": "provider_payload" if wait is not None else "included_in_provider_duration",
                "risk": "candidate_or_route_contract" if effective_risk > 0 else "no_explicit_risk_penalty",
            },
        }

    @staticmethod
    def _route_anchor_amap_id(item: Optional[dict[str, Any]]) -> str:
        if not isinstance(item, dict):
            return ""
        poi = item.get("poi")
        if poi is not None:
            return str(getattr(poi, "amap_id", None) or getattr(poi, "id", None) or "")
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else item
        raw_poi = raw.get("poi") if isinstance(raw.get("poi"), dict) else {}
        return str(raw_poi.get("amapId") or raw_poi.get("id") or "")

    @classmethod
    def _proof_route_matrix(
        cls,
        *,
        previous: Optional[dict[str, Any]],
        candidate: dict[str, Any],
        following: Optional[dict[str, Any]],
        previous_to_candidate: Optional[dict[str, Any]],
        candidate_to_next: Optional[dict[str, Any]],
        previous_to_next: Optional[dict[str, Any]],
    ) -> dict[str, Optional[dict[str, Any]]]:
        def leg(
            value: Optional[dict[str, Any]],
            left: Optional[dict[str, Any]],
            right: Optional[dict[str, Any]],
        ) -> Optional[dict[str, Any]]:
            if value is None or left is None or right is None:
                return None
            return {
                **copy.deepcopy(value),
                "fromSegmentId": str(left.get("id") or ""),
                "toSegmentId": str(right.get("id") or ""),
                "fromAmapId": cls._route_anchor_amap_id(left),
                "toAmapId": cls._route_anchor_amap_id(right),
            }

        return {
            "previousToCandidate": leg(previous_to_candidate, previous, candidate),
            "candidateToNext": leg(candidate_to_next, candidate, following),
            "previousToNext": leg(previous_to_next, previous, following),
        }

    @staticmethod
    def _seal_route_insertion_proof(
        proof: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        sealed = copy.deepcopy(proof)
        fingerprint = RouteInsertionScorer.route_proof_fingerprint(sealed)
        if fingerprint is None:
            return None
        sealed["proofFingerprint"] = fingerprint
        return sealed

    def _validated_retained_route_insertion_proof(
        self,
        *,
        replacement_proof: Any,
        previous: dict[str, Any],
        candidate: dict[str, Any],
        following: dict[str, Any],
        incoming: Optional[dict[str, Any]],
        outgoing: Optional[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Rebind a sealed, out-of-scope insertion proof to its logical slot."""

        if not isinstance(replacement_proof, dict) or replacement_proof.get(
            "networkVerified"
        ) is not True:
            return None
        fingerprint = str(replacement_proof.get("proofFingerprint") or "")
        if not fingerprint or fingerprint != RouteInsertionScorer.route_proof_fingerprint(
            replacement_proof
        ):
            return None
        policy = self._provider_decision_policy(snapshot, candidate)
        if policy is None or any(
            replacement_proof.get(key) != policy.get(key)
            for key in (
                "detourTolerance",
                "mobilityProfile",
                "detourToleranceSource",
                "detourToleranceFingerprint",
                "mobilityProfileSource",
                "mobilityProfileFingerprint",
                "routeDecisionContractSource",
                "contractFingerprint",
            )
        ):
            return None
        candidate_endpoint = replacement_proof.get("candidateEndpoint")
        baseline_endpoints = replacement_proof.get("baselineEndpoints")
        if not isinstance(candidate_endpoint, dict) or not isinstance(
            baseline_endpoints, dict
        ):
            return None
        if candidate_endpoint != {
            "segmentId": str(candidate.get("id") or ""),
            "amapId": self._route_anchor_amap_id(candidate),
        }:
            return None
        if baseline_endpoints != {
            "previousSegmentId": str(previous.get("id") or ""),
            "previousAmapId": self._route_anchor_amap_id(previous),
            "nextSegmentId": str(following.get("id") or ""),
            "nextAmapId": self._route_anchor_amap_id(following),
        }:
            return None
        route_matrix = replacement_proof.get("routeMatrix")
        if not isinstance(route_matrix, dict):
            return None
        expected_endpoints = {
            "previousToCandidate": (
                str(previous.get("id") or ""),
                str(candidate.get("id") or ""),
                self._route_anchor_amap_id(previous),
                self._route_anchor_amap_id(candidate),
            ),
            "candidateToNext": (
                str(candidate.get("id") or ""),
                str(following.get("id") or ""),
                self._route_anchor_amap_id(candidate),
                self._route_anchor_amap_id(following),
            ),
            "previousToNext": (
                str(previous.get("id") or ""),
                str(following.get("id") or ""),
                self._route_anchor_amap_id(previous),
                self._route_anchor_amap_id(following),
            ),
        }
        normalized_legs: dict[str, dict[str, Any]] = {}
        for key, endpoints in expected_endpoints.items():
            raw_leg = route_matrix.get(key)
            if not isinstance(raw_leg, dict) or (
                str(raw_leg.get("fromSegmentId") or ""),
                str(raw_leg.get("toSegmentId") or ""),
                str(raw_leg.get("fromAmapId") or ""),
                str(raw_leg.get("toAmapId") or ""),
            ) != endpoints:
                return None
            normalized = self._provider_matrix_leg(raw_leg)
            if normalized is None:
                return None
            normalized_legs[key] = normalized
        if incoming != normalized_legs["previousToCandidate"] or outgoing != normalized_legs[
            "candidateToNext"
        ]:
            comparable_fields = (
                "provider",
                "source",
                "mode",
                "distanceMeters",
                "durationSeconds",
                "walkingDistanceMeters",
                "transferCount",
                "waitSeconds",
                "riskPenaltyMinutes",
                "costComponentProvenance",
            )
            if incoming is None or outgoing is None or any(
                current.get(field) != normalized_legs[key].get(field)
                for current, key in (
                    (incoming, "previousToCandidate"),
                    (outgoing, "candidateToNext"),
                )
                for field in comparable_fields
            ):
                return None
        schedule_slack = self._replacement_schedule_slack(candidate)
        if replacement_proof.get("scheduleSlackMinutes") != schedule_slack:
            return None
        normalized_window = self._strict_provider_time_window(candidate)
        if "timeWindow" in replacement_proof and replacement_proof.get(
            "timeWindow"
        ) != normalized_window:
            return None
        time_window_feasible = replacement_proof.get("timeWindowFeasible")
        if not isinstance(time_window_feasible, bool):
            return None
        matrix_score = self.route_insertion_scorer.score_from_route_matrix(
            previous_to_candidate=normalized_legs["previousToCandidate"],
            candidate_to_next=normalized_legs["candidateToNext"],
            previous_to_next=normalized_legs["previousToNext"],
            detour_tolerance=policy["detourTolerance"],
            schedule_slack_minutes=schedule_slack,
            time_window_feasible=time_window_feasible,
            mobility_profile=policy["mobilityProfile"],
        )
        if matrix_score is None or any(
            replacement_proof.get(key) != value
            for key, value in {
                "generalizedCostDelta": matrix_score.generalized_cost_delta,
                "detourRatio": matrix_score.detour_ratio,
                "detourLevel": matrix_score.detour_level,
            }.items()
        ):
            return None
        return replacement_proof

    @staticmethod
    def _route_queried_at(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            queried_at = value
        else:
            try:
                queried_at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except ValueError:
                return None
        if queried_at.tzinfo is None:
            queried_at = queried_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - queried_at.astimezone(timezone.utc)).total_seconds()
        return queried_at if -60 <= age <= ROUTE_CACHE_TTL_SECONDS else None

    @staticmethod
    def _finite_nonnegative(value: Any) -> Optional[float]:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) and result >= 0 else None

    @classmethod
    def _provider_route_contract(cls, segment: dict[str, Any]) -> dict[str, Any]:
        semantic = cls._segment_semantic(segment)
        route_contract = semantic.get("routeContract")
        return route_contract if isinstance(route_contract, dict) else {}

    @classmethod
    def _provider_experience_policy(cls, segment: dict[str, Any]) -> dict[str, Any]:
        route_contract = cls._provider_route_contract(segment)
        policy = route_contract.get("experienceSpecPolicy")
        return policy if isinstance(policy, dict) else {}

    @classmethod
    def _requires_provider_insertion_decision(cls, segment: dict[str, Any]) -> bool:
        route_contract = cls._provider_route_contract(segment)
        policy = cls._provider_experience_policy(segment)
        return bool(
            route_contract.get("requiresProviderInsertionDecision") is True
            or policy
            or route_contract.get("detourTolerance") is not None
            or route_contract.get("timeWindow") is not None
            or policy.get("detourTolerance") is not None
            or policy.get("timeWindow") is not None
        )

    @staticmethod
    def _strict_clock_minutes(value: Any) -> Optional[int]:
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"([0-2]\d):([0-5]\d)", value.strip())
        if match is None:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23:
            return None
        return hour * 60 + minute

    @classmethod
    def _provider_time_window_value(cls, segment: dict[str, Any]) -> Any:
        policy = cls._provider_experience_policy(segment)
        route_contract = cls._provider_route_contract(segment)
        if "timeWindow" in policy:
            return policy.get("timeWindow")
        return route_contract.get("timeWindow")

    @classmethod
    def _strict_provider_time_window(cls, segment: dict[str, Any]) -> Optional[dict[str, str]]:
        value = cls._provider_time_window_value(segment)
        start: Any = None
        end: Any = None
        if isinstance(value, dict):
            start = value.get("start")
            end = value.get("end")
        elif isinstance(value, str):
            match = re.fullmatch(
                r"\s*([0-2]\d:[0-5]\d)\s*[-\u2013\u2014]\s*"
                r"([0-2]\d:[0-5]\d)\s*",
                value,
            )
            if match is not None:
                start, end = match.groups()
        start_minutes = cls._strict_clock_minutes(start)
        end_minutes = cls._strict_clock_minutes(end)
        if start_minutes is None or end_minutes is None or end_minutes <= start_minutes:
            return None
        return {"start": str(start).strip(), "end": str(end).strip()}

    @classmethod
    def _raw_route_decision_contract(
        cls,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
    ) -> tuple[Any, str]:
        if "routeDecisionContract" in snapshot:
            return snapshot.get("routeDecisionContract"), "snapshot.routeDecisionContract"
        route_contract = cls._provider_route_contract(segment)
        if "routeDecisionContract" in route_contract:
            return (
                route_contract.get("routeDecisionContract"),
                "segment.routeContract.routeDecisionContract",
            )
        if any(
            key in route_contract
            for key in (
                "schemaVersion",
                "source",
                "fingerprint",
                "provenance",
                "detourTolerance",
                "mobilityProfile",
            )
        ):
            return route_contract, "segment.routeContract"
        return None, ""

    @classmethod
    def _route_decision_contract_resolution(
        cls,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        raw, location = cls._raw_route_decision_contract(snapshot, segment)
        if not isinstance(raw, dict):
            return None
        normalized = RouteInsertionScorer.normalized_route_decision_contract(raw)
        if normalized is None:
            return None
        return {
            "schemaVersion": str(raw.get("schemaVersion") or "").strip(),
            "source": normalized["source"],
            "fingerprint": normalized["fingerprint"],
            "provenance": copy.deepcopy(normalized["provenance"]),
            "detourTolerance": copy.deepcopy(normalized["detourTolerance"]),
            "detourToleranceSource": f"{location}.detourTolerance",
            "detourToleranceFingerprint": canonical_fingerprint(normalized["detourTolerance"]),
            "mobilityProfile": copy.deepcopy(normalized["mobilityProfile"]),
            "mobilityProfileSource": f"{location}.mobilityProfile",
            "mobilityProfileFingerprint": canonical_fingerprint(normalized["mobilityProfile"]),
            "location": location,
        }

    @classmethod
    def _route_decision_contract_issue_code(
        cls,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
    ) -> Optional[str]:
        raw, _location = cls._raw_route_decision_contract(snapshot, segment)
        if raw is None:
            return "provider_route_decision_contract_missing"
        if not isinstance(raw, dict):
            return "provider_route_decision_contract_invalid"
        if raw.get("detourTolerance") is None:
            return "provider_insertion_detour_tolerance_missing"
        if raw.get("mobilityProfile") is None:
            return "provider_insertion_mobility_profile_missing"
        if cls._route_decision_contract_resolution(snapshot, segment) is None:
            return "provider_route_decision_contract_invalid"
        return None

    @classmethod
    def _provider_decision_policy(
        cls,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
        candidate: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        del candidate
        contract = cls._route_decision_contract_resolution(snapshot, segment)
        if contract is None:
            return None
        time_window = (
            cls._strict_provider_time_window(segment)
            if segment.get("kind") == "meal"
            or cls._requires_provider_insertion_decision(segment)
            else None
        )
        route_contract = cls._provider_route_contract(segment)
        experience_policy = cls._provider_experience_policy(segment)
        spec_fingerprint = str(route_contract.get("specFingerprint") or "") if experience_policy else ""
        return {
            "detourTolerance": contract["detourTolerance"],
            "detourToleranceSource": contract["detourToleranceSource"],
            "detourToleranceFingerprint": contract["detourToleranceFingerprint"],
            "mobilityProfile": contract["mobilityProfile"],
            "mobilityProfileSource": contract["mobilityProfileSource"],
            "mobilityProfileFingerprint": contract["mobilityProfileFingerprint"],
            "routeDecisionContractSource": contract["source"],
            "routeDecisionContractProvenance": contract["provenance"],
            "routeDecisionContractLocation": contract["location"],
            "contractFingerprint": contract["fingerprint"],
            "routeDecisionContract": {
                "source": contract["source"],
                "provenance": copy.deepcopy(contract["provenance"]),
                "detourTolerance": copy.deepcopy(contract["detourTolerance"]),
                "mobilityProfile": copy.deepcopy(contract["mobilityProfile"]),
                "fingerprint": contract["fingerprint"],
            },
            "specFingerprint": spec_fingerprint,
            "timeWindow": time_window,
        }

    @classmethod
    def _provider_insertion_contract_issues(
        cls,
        snapshot: dict[str, Any],
        route_anchors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        anchors_by_day: dict[int, list[dict[str, Any]]] = {}
        for anchor in route_anchors:
            anchors_by_day.setdefault(int(anchor.get("dayNumber") or 0), []).append(anchor)
        for anchor in route_anchors:
            day_anchors = anchors_by_day.get(int(anchor.get("dayNumber") or 0), [])
            anchor_index = next(
                (index for index, item in enumerate(day_anchors) if item.get("id") == anchor.get("id")),
                -1,
            )
            is_meal_decision = anchor.get("kind") == "meal" and len(day_anchors) >= 2
            is_experience_decision = anchor.get("kind") != "meal" and cls._is_interior_provider_insertion(
                day_anchors,
                anchor_index,
            )
            if not is_meal_decision and not is_experience_decision:
                continue
            common = {
                "dayNumber": int(anchor.get("dayNumber") or 0),
                "segmentId": str(anchor.get("id") or ""),
                **cls._segment_lineage(anchor),
            }
            if is_experience_decision:
                raw_window = cls._provider_time_window_value(anchor)
                if raw_window is None:
                    issues.append(
                        {
                            **common,
                            "code": "provider_insertion_time_window_missing",
                            "message": "ExperienceSpec Provider 路线决策缺少严格时间窗。",
                        }
                    )
                    continue
                if cls._strict_provider_time_window(anchor) is None:
                    issues.append(
                        {
                            **common,
                            "code": "provider_insertion_time_window_invalid",
                            "message": "ExperienceSpec Provider 路线决策时间窗未知或格式无效。",
                        }
                    )
                    continue
                policy = cls._provider_experience_policy(anchor)
                if policy:
                    fingerprint = str(cls._provider_route_contract(anchor).get("specFingerprint") or "")
                    if not fingerprint:
                        issues.append(
                            {
                                **common,
                                "code": "provider_insertion_spec_fingerprint_missing",
                                "message": "ExperienceSpec Provider 路线决策缺少策略指纹。",
                            }
                        )
                        continue
                    if fingerprint != canonical_fingerprint(policy):
                        issues.append(
                            {
                                **common,
                                "code": "provider_insertion_spec_fingerprint_invalid",
                                "message": "ExperienceSpec Provider 路线决策策略指纹不匹配。",
                            }
                        )
                        continue
            decision_issue = cls._route_decision_contract_issue_code(snapshot, anchor)
            if decision_issue is not None:
                messages = {
                    "provider_route_decision_contract_missing": ("Provider 路线决策缺少统一 routeDecisionContract。"),
                    "provider_insertion_detour_tolerance_missing": ("routeDecisionContract 缺少显式绕行容忍度。"),
                    "provider_insertion_mobility_profile_missing": ("routeDecisionContract 缺少显式 MobilityProfile。"),
                    "provider_route_decision_contract_invalid": ("routeDecisionContract 字段不完整或指纹不匹配。"),
                }
                issues.append(
                    {
                        **common,
                        "code": decision_issue,
                        "message": messages[decision_issue],
                    }
                )
                continue
            if is_experience_decision and len(day_anchors) < 2:
                issues.append(
                    {
                        **common,
                        "code": "provider_insertion_route_matrix_missing",
                        "message": "ExperienceSpec 锚点没有可用于 Provider 路线矩阵的相邻锚点。",
                    }
                )
        return issues

    def _apply_provider_insertion_time_windows(self, snapshot: dict[str, Any]) -> bool:
        for segment in self._snapshot_segments(snapshot):
            if segment.get("kind") == "meal" or not self._requires_provider_insertion_decision(segment):
                continue
            window = self._strict_provider_time_window(segment)
            if window is None:
                return False
            semantic = self._segment_semantic(segment)
            existing = semantic.get("scheduleConstraints")
            schedule_constraints = copy.deepcopy(existing) if isinstance(existing, dict) else {}
            schedule_constraints.update(
                {
                    "timeWindow": f"{window['start']}-{window['end']}",
                    "earliestStart": window["start"],
                    "windowEnd": window["end"],
                    "hard": True,
                    "source": "experience_spec_policy",
                }
            )
            semantic["scheduleConstraints"] = schedule_constraints
        return True

    @classmethod
    def _route_risk_penalty(cls, item: dict[str, Any]) -> float:
        semantic = cls._segment_semantic(item)
        route_contract = semantic.get("routeContract") if isinstance(semantic.get("routeContract"), dict) else {}
        return (
            cls._finite_nonnegative(route_contract.get("riskPenaltyMinutes", semantic.get("riskPenaltyMinutes"))) or 0.0
        )

    def _schedule_projection_feasible(
        self,
        snapshot: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> bool:
        return self._project_provider_schedule(snapshot, evidence, mutate=False)

    def _project_provider_schedule(
        self,
        snapshot: dict[str, Any],
        evidence: list[dict[str, Any]],
        *,
        mutate: bool,
    ) -> bool:
        verified_evidence: list[dict[str, Any]] = []
        for item in evidence:
            matrix_leg = self._provider_matrix_leg(item)
            if matrix_leg is None:
                return False
            copied = copy.deepcopy(item)
            copied["status"] = "verified"
            copied["routeStatus"] = "verified"
            copied["durationMinutes"] = max(
                1,
                int(math.ceil(float(matrix_leg["durationSeconds"]) / 60)),
            )
            verified_evidence.append(copied)
        target = snapshot if mutate else copy.deepcopy(snapshot)
        if not self._apply_provider_insertion_time_windows(target):
            return False
        try:
            ItineraryScheduleService.project_snapshot_schedule(
                target,
                verified_evidence,
            )
        except Exception:
            return False
        return True

    def _replacement_matrix_score(
        self,
        baseline_legs: Optional[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]],
        candidate_legs: Optional[tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]],
        *,
        segment: dict[str, Any],
        candidate: dict[str, Any],
        trial_snapshot: dict[str, Any],
        route_evidence: list[dict[str, Any]],
    ):
        if baseline_legs is None or candidate_legs is None:
            return None
        normalized_baseline = tuple(
            self._provider_matrix_leg(item) if item is not None else None for item in baseline_legs
        )
        normalized_candidate = tuple(
            self._provider_matrix_leg(
                item,
                risk_penalty=max(
                    self._route_risk_penalty(segment),
                    self._finite_nonnegative(candidate.get("riskPenaltyMinutes")) or 0.0,
                ),
            )
            if item is not None
            else None
            for item in candidate_legs
        )
        if any(
            item is None
            for raw, item in zip((*baseline_legs, *candidate_legs), (*normalized_baseline, *normalized_candidate))
            if raw is not None
        ):
            return None
        baseline_items = tuple(item for item in normalized_baseline if item is not None)
        candidate_items = tuple(item for item in normalized_candidate if item is not None)
        if not baseline_items or len(baseline_items) != len(candidate_items):
            return None
        baseline = self._combine_provider_legs(*baseline_items)
        time_window_feasible = self._schedule_projection_feasible(
            trial_snapshot,
            route_evidence,
        )
        decision_policy = self._provider_decision_policy(trial_snapshot, segment, candidate)
        if decision_policy is None:
            return None
        return self.route_insertion_scorer.score_from_route_matrix(
            previous_to_candidate=normalized_candidate[0],
            candidate_to_next=normalized_candidate[1],
            # For a replacement the existing two-leg path is the baseline;
            # the scorer's ΔG therefore reports the incremental network cost.
            previous_to_next=baseline,
            detour_tolerance=decision_policy["detourTolerance"],
            schedule_slack_minutes=self._replacement_schedule_slack(segment, candidate),
            time_window_feasible=time_window_feasible,
            mobility_profile=decision_policy["mobilityProfile"],
        )

    @staticmethod
    def _combine_provider_legs(*legs: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "durationSeconds",
            "distanceMeters",
            "walkingDistanceMeters",
            "transferCount",
            "waitSeconds",
            "riskPenaltyMinutes",
        )
        queried_at = min(str(item.get("queriedAt") or "") for item in legs if item.get("queriedAt"))
        return {
            "provider": AMAP_ROUTE_SOURCE,
            "source": AMAP_ROUTE_SOURCE,
            "mode": str(next((item.get("mode") for item in legs if item.get("mode")), "")),
            "queriedAt": queried_at,
            **{field: sum(float(item.get(field) or 0) for item in legs) for field in fields},
            "costComponentProvenance": {
                "composedFromProviderLegCount": len(legs),
                "source": "normalized_provider_route_matrix",
            },
        }

    @classmethod
    def _replacement_schedule_slack(
        cls,
        segment: dict[str, Any],
        candidate: Optional[dict[str, Any]] = None,
    ) -> Optional[float]:
        semantic = cls._segment_semantic(segment)
        candidate_semantic = (
            candidate.get("semanticMetadata")
            if isinstance(candidate, dict) and isinstance(candidate.get("semanticMetadata"), dict)
            else {}
        )
        value: Any = None
        found = False
        for container in (candidate or {}, candidate_semantic, semantic):
            schedule = container.get("schedule") if isinstance(container.get("schedule"), dict) else {}
            if "slackMinutes" in schedule:
                value = schedule["slackMinutes"]
                found = True
                break
            if "slackMinutes" in container:
                value = container["slackMinutes"]
                found = True
                break
        if not found:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    @classmethod
    def _record_repair_rejection(
        cls,
        ledger: dict[str, Any],
        reason: str,
        candidate: dict[str, Any] | None = None,
        *,
        scope_fingerprint: str = "",
    ) -> None:
        reasons = ledger.setdefault("rejectedReasonCounts", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1
        candidate_id = cls._canonical_physical_id(candidate)
        if not candidate_id:
            return
        rejected_ids = ledger.setdefault("rejectedCanonicalPhysicalIds", [])
        if candidate_id not in rejected_ids:
            rejected_ids.append(candidate_id)
        ledger["attemptCursor"] = int(ledger.get("attemptCursor") or 0) + 1
        attempt = {
            "attempt": int(ledger["attemptCursor"]),
            "candidatePhysicalId": candidate_id,
            "reason": str(reason),
        }
        if scope_fingerprint:
            attempt["scopeFingerprint"] = str(scope_fingerprint)
        ledger.setdefault("rejectedCandidateAttempts", []).append(attempt)
        if not scope_fingerprint:
            return
        for checkpoint in ledger.get("routeBadCheckpoints") or []:
            if not isinstance(checkpoint, dict):
                continue
            if str(checkpoint.get("scopeFingerprint") or "") != str(scope_fingerprint):
                continue
            checkpoint_ids = checkpoint.setdefault("rejectedCanonicalPhysicalIds", [])
            cls._append_unique(checkpoint_ids, candidate_id)
            checkpoint["attemptCursor"] = int(checkpoint.get("attemptCursor") or 0) + 1
            checkpoint_attempt = {
                "attempt": int(checkpoint["attemptCursor"]),
                "candidatePhysicalId": candidate_id,
                "reason": str(reason),
                "scopeFingerprint": str(scope_fingerprint),
            }
            checkpoint.setdefault("rejectedCandidateAttempts", []).append(checkpoint_attempt)
            break

    @staticmethod
    def _append_unique(values: list[str], value: str) -> None:
        if value and value not in values:
            values.append(value)

    @staticmethod
    def _snapshot_root_portfolio_id(snapshot: dict[str, Any]) -> str:
        return str(
            snapshot.get("rootPortfolioId")
            or snapshot.get("portfolioId")
            or snapshot.get("id")
            or ""
        ).strip()

    @staticmethod
    def _snapshot_planning_root_turn_id(snapshot: dict[str, Any]) -> str:
        selection = (
            snapshot.get("portfolioSelectionContext")
            if isinstance(snapshot.get("portfolioSelectionContext"), dict)
            else {}
        )
        return str(
            selection.get("planningSelectionRootTurnId")
            or snapshot.get("planningSelectionRootTurnId")
            or ""
        ).strip()

    @classmethod
    def _canonical_physical_id(cls, value: Any) -> str:
        """Return one canonical AMap physical identity without display fallbacks."""

        if isinstance(value, dict):
            if PoiPhysicalIdentityService.invalid_parent_id(value):
                return ""
            return PoiPhysicalIdentityService.canonical_amap_id(value)
        if isinstance(value, POI):
            return str(value.amap_id or value.id or "").strip().upper()
        return ""

    @classmethod
    def _segment_poi_payload(cls, segment: dict[str, Any]) -> dict[str, Any]:
        raw = segment.get("raw") if isinstance(segment.get("raw"), dict) else {}
        poi = raw.get("poi") if isinstance(raw.get("poi"), dict) else {}
        return poi

    def _adjacent_route_ledger_keys(
        self,
        snapshot: dict[str, Any],
        *,
        segment: dict[str, Any],
        candidate_physical_id: str,
        transport_mode: str,
    ) -> list[dict[str, str]]:
        """Build physical Provider-leg keys from the current snapshot only."""

        if not candidate_physical_id:
            return []
        day_number = int(segment.get("dayNumber") or 0)
        target_id = str(segment.get("id") or "")
        anchors = [
            item
            for item in self._snapshot_segments(snapshot)
            if int(item.get("dayNumber") or 0) == day_number and bool(item.get("route_anchor"))
        ]
        target_index = next(
            (index for index, item in enumerate(anchors) if str(item.get("id") or "") == target_id),
            None,
        )
        if target_index is None:
            return []
        mode = normalize_route_mode(str(transport_mode or ""))
        if not mode:
            return []
        keys: list[dict[str, str]] = []
        if target_index > 0:
            previous_id = self._canonical_physical_id(self._segment_poi_payload(anchors[target_index - 1]))
            if not previous_id:
                return []
            keys.append(
                {
                    "fromPhysicalId": previous_id,
                    "toPhysicalId": candidate_physical_id,
                    "mode": mode,
                }
            )
        if target_index + 1 < len(anchors):
            next_id = self._canonical_physical_id(self._segment_poi_payload(anchors[target_index + 1]))
            if not next_id:
                return []
            keys.append(
                {
                    "fromPhysicalId": candidate_physical_id,
                    "toPhysicalId": next_id,
                    "mode": mode,
                }
            )
        return keys

    def _adjacent_anchor_ids(
        self,
        snapshot: dict[str, Any],
        *,
        segment: dict[str, Any],
    ) -> list[str]:
        """Bind the repair certificate to exact neighboring segment identities."""

        day_number = int(segment.get("dayNumber") or 0)
        target_id = str(segment.get("id") or "").strip()
        anchors = [
            item
            for item in self._snapshot_segments(snapshot)
            if int(item.get("dayNumber") or 0) == day_number and bool(item.get("route_anchor"))
        ]
        target_indices = [
            index
            for index, item in enumerate(anchors)
            if str(item.get("id") or "").strip() == target_id
        ]
        if len(target_indices) != 1:
            return []
        target_index = target_indices[0]
        adjacent: list[str] = []
        if target_index > 0:
            adjacent.append(str(anchors[target_index - 1].get("id") or "").strip())
        if target_index + 1 < len(anchors):
            adjacent.append(str(anchors[target_index + 1].get("id") or "").strip())
        return adjacent if all(adjacent) and len(set(adjacent)) == len(adjacent) else []

    def _route_repair_scope_certificate(
        self,
        snapshot: dict[str, Any],
        *,
        segment: dict[str, Any],
        candidate: dict[str, Any],
        transport_mode: str,
        issue_code: str = "",
    ) -> Optional[dict[str, Any]]:
        """Derive the exact route-repair scope from server snapshot facts.

        The durable certificate binds both logical neighboring segment IDs and
        canonical physical Provider-leg identities. Neither can substitute for
        the other when a physical POI occurs more than once in a snapshot.
        """

        root_portfolio_id = self._snapshot_root_portfolio_id(snapshot)
        planning_root_turn_id = self._snapshot_planning_root_turn_id(snapshot)
        brief_id = str(segment.get("briefId") or "").strip()
        pool_id = str(segment.get("poolId") or "").strip()
        planning_slot_id = str(segment.get("planningSlotId") or "").strip()
        day_number = int(segment.get("dayNumber") or 0)
        candidate_physical_id = self._canonical_physical_id(candidate)
        contract = self._route_decision_contract_resolution(snapshot, segment)
        route_contract_fingerprint = str((contract or {}).get("fingerprint") or "").strip()
        adjacent_keys = self._adjacent_route_ledger_keys(
            snapshot,
            segment=segment,
            candidate_physical_id=candidate_physical_id,
            transport_mode=transport_mode,
        )
        adjacent_anchor_ids = self._adjacent_anchor_ids(
            snapshot,
            segment=segment,
        )
        if not all(
            (
                root_portfolio_id,
                planning_root_turn_id,
                brief_id,
                planning_slot_id,
                day_number > 0,
                candidate_physical_id,
                route_contract_fingerprint,
                adjacent_keys,
                adjacent_anchor_ids,
            )
        ):
            return None
        scope = {
            "continuationMode": "repair_exact_slot",
            "rootPortfolioId": root_portfolio_id,
            "briefId": brief_id,
            "poolId": pool_id,
            "dayNumber": day_number,
            "planningSlotId": planning_slot_id,
            "candidatePhysicalId": candidate_physical_id,
            "adjacentAnchorIds": adjacent_anchor_ids,
            "adjacentRouteLedgerKeys": adjacent_keys,
            "routeContractFingerprint": route_contract_fingerprint,
        }
        scope["planningSelectionRootTurnId"] = planning_root_turn_id
        scope["scopeFingerprint"] = canonical_fingerprint(scope)
        if issue_code:
            scope["issueCodes"] = [str(issue_code)]
        return scope

    @staticmethod
    def _issue_scope_matches_segment(issue: dict[str, Any], segment: dict[str, Any]) -> bool:
        """Match only the server-generated semantic scope, never display text."""

        brief_id = str(issue.get("mealBriefId") or issue.get("briefId") or "").strip()
        slot_id = str(issue.get("mealPlanningSlotId") or issue.get("planningSlotId") or "").strip()
        try:
            day_number = int(issue.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            brief_id
            and slot_id
            and day_number > 0
            and brief_id == str(segment.get("briefId") or "").strip()
            and slot_id == str(segment.get("planningSlotId") or "").strip()
            and day_number == int(segment.get("dayNumber") or 0)
        )

    @classmethod
    def _full_route_matrix_proof_fingerprint(
        cls,
        proof: Any,
        *,
        expected_candidate_physical_id: str,
        expected_contract_fingerprint: str,
    ) -> Optional[str]:
        if not isinstance(proof, dict) or proof.get("networkVerified") is not True:
            return None
        fingerprint = str(proof.get("proofFingerprint") or "").strip()
        if not fingerprint or fingerprint != RouteInsertionScorer.route_proof_fingerprint(proof):
            return None
        if str(proof.get("contractFingerprint") or "").strip() != expected_contract_fingerprint:
            return None
        endpoint = proof.get("candidateEndpoint") if isinstance(proof.get("candidateEndpoint"), dict) else {}
        if cls._canonical_physical_id(endpoint) != expected_candidate_physical_id:
            return None
        route_matrix = proof.get("routeMatrix")
        if not isinstance(route_matrix, dict):
            return None
        required_legs = ("previousToCandidate", "candidateToNext", "previousToNext")
        if any(cls._provider_matrix_leg(route_matrix.get(key)) is None for key in required_legs):
            return None
        return fingerprint

    @classmethod
    def _replacement_matrix_proof_fingerprint(cls, proof: Any) -> Optional[str]:
        if not isinstance(proof, dict) or proof.get("networkVerified") is not True:
            return None
        stored_fingerprint = str(proof.get("proofFingerprint") or "").strip()
        baseline = proof.get("baselineLegs")
        candidate = proof.get("candidateLegs")
        if (
            not isinstance(baseline, list)
            or not isinstance(candidate, list)
            or not baseline
            or len(baseline) != len(candidate)
            or any(cls._provider_matrix_leg(item) is None for item in [*baseline, *candidate])
        ):
            return None
        candidate_physical_id = cls._canonical_physical_id({"amapId": proof.get("candidateAmapId")})
        contract_fingerprint = str(proof.get("contractFingerprint") or "").strip()
        if not candidate_physical_id or not contract_fingerprint:
            return None
        fingerprint_material = {
            "schema": "portfolio-route-replacement-matrix-v1",
            "candidatePhysicalId": candidate_physical_id,
            "baselineLegs": baseline,
            "candidateLegs": candidate,
            "contractFingerprint": contract_fingerprint,
            "detourTolerance": proof.get("detourTolerance"),
            "mobilityProfile": proof.get("mobilityProfile"),
            "networkVerified": proof.get("networkVerified"),
            "timeWindowFeasible": proof.get("timeWindowFeasible"),
        }
        repair_scope_fingerprint = str(proof.get("repairScopeFingerprint") or "").strip()
        if repair_scope_fingerprint:
            fingerprint_material["repairScopeFingerprint"] = repair_scope_fingerprint
        computed_fingerprint = canonical_fingerprint(fingerprint_material)
        if stored_fingerprint and stored_fingerprint != computed_fingerprint:
            return None
        return computed_fingerprint

    @classmethod
    def _bind_matrix_proof_to_repair_scope(
        cls,
        proof: Any,
        probe: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if not isinstance(proof, dict):
            return None
        scope_fingerprint = str(probe.get("scopeFingerprint") or "").strip()
        if not scope_fingerprint:
            return None
        scoped = copy.deepcopy(proof)
        scoped["repairScopeFingerprint"] = scope_fingerprint
        scoped.pop("proofFingerprint", None)
        fingerprint = cls._replacement_matrix_proof_fingerprint(scoped)
        if fingerprint is None:
            return None
        scoped["proofFingerprint"] = fingerprint
        return scoped

    @staticmethod
    def _consumer_admitted(candidate: dict[str, Any]) -> bool:
        report = candidate.get("consumerAdmissionReport")
        if not isinstance(report, dict):
            return False
        return bool(
            report.get("scoreEligible") is True
            or str(report.get("classification") or "").startswith("admitted_")
        )

    def _record_executed_query(
        self,
        ledger: dict[str, Any],
        *,
        scope_fingerprint: str,
        query_fingerprint: str,
    ) -> None:
        """Record only a completed discovery call in its exact repair scope."""

        if not scope_fingerprint or not query_fingerprint:
            return
        executed = ledger.setdefault("executedQueryFingerprints", [])
        self._append_unique(executed, query_fingerprint)
        # ``queryCursor`` is an execution cursor, not a count of merely
        # planned query scopes.  This makes an interrupted repair replayable:
        # the persisted cursor always points to the exact number of hashed
        # discovery calls that were actually completed.
        ledger["queryCursor"] = len(executed)
        for checkpoint in ledger.get("routeBadCheckpoints") or []:
            if not isinstance(checkpoint, dict):
                continue
            if str(checkpoint.get("scopeFingerprint") or "") == scope_fingerprint:
                checkpoint_executed = checkpoint.setdefault("executedQueryFingerprints", [])
                self._append_unique(checkpoint_executed, query_fingerprint)
                checkpoint["queryCursor"] = len(checkpoint_executed)

    def _next_planned_query_fingerprint(
        self,
        ledger: dict[str, Any],
        *,
        scope_fingerprint: str,
    ) -> str:
        """Return the next persisted query token without advancing its cursor."""

        checkpoint = next(
            (
                item
                for item in ledger.get("routeBadCheckpoints") or []
                if isinstance(item, dict)
                and str(item.get("scopeFingerprint") or "") == scope_fingerprint
            ),
            None,
        )
        if not isinstance(checkpoint, dict):
            return ""
        executed = {
            str(value).strip()
            for value in checkpoint.get("executedQueryFingerprints") or []
            if isinstance(value, str) and value.strip()
        }
        return next(
            (
                str(value).strip()
                for value in checkpoint.get("usedQueryFingerprints") or []
                if isinstance(value, str) and value.strip() and value.strip() not in executed
            ),
            "",
        )

    def _record_route_bad_checkpoints(
        self,
        ledger: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        issues: Iterable[dict[str, Any]],
        candidate_pools: Iterable[dict[str, Any]],
        transport_mode: str,
    ) -> None:
        """Persist server-derived repair scope before discovery can mutate it."""

        segments = self._snapshot_segments(snapshot)
        query_fingerprints = sorted(
            {
                str(value).strip()
                for source in [*candidate_pools, *(item.get("semantic") or {} for item in segments)]
                if isinstance(source, dict)
                for value in [
                    source.get("queryFingerprint"),
                    source.get("routeQueryFingerprint"),
                    (source.get("routeQueryScope") or {}).get("queryFingerprint")
                    if isinstance(source.get("routeQueryScope"), dict)
                    else "",
                ]
                if isinstance(value, str) and value.strip()
            }
        )
        ledger["usedQueryFingerprints"] = query_fingerprints
        # Candidate/frontier query scopes are only planned inputs.  They do
        # not advance the executed-query cursor until a discovery call returns.
        ledger["queryCursor"] = 0
        checkpoints = ledger.setdefault("routeBadCheckpoints", [])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            matching_segments = [
                segment for segment in segments if self._issue_scope_matches_segment(issue, segment)
            ]
            if len(matching_segments) != 1:
                continue
            segment = matching_segments[0]
            candidate = self._segment_poi_payload(segment)
            checkpoint = self._route_repair_scope_certificate(
                snapshot,
                segment=segment,
                candidate=candidate,
                transport_mode=transport_mode,
                issue_code=str(issue.get("code") or "route_quality_failed"),
            )
            if checkpoint is None:
                continue
            proof_fingerprint = self._full_route_matrix_proof_fingerprint(
                (segment.get("semantic") or {}).get("routeInsertionMatrixProof"),
                expected_candidate_physical_id=str(checkpoint["candidatePhysicalId"]),
                expected_contract_fingerprint=str(checkpoint["routeContractFingerprint"]),
            )
            checkpoint["usedQueryFingerprints"] = self._planned_query_fingerprints_for_segment(
                segment,
                candidate_pools=candidate_pools,
            )
            checkpoint["executedQueryFingerprints"] = []
            checkpoint["admittedCanonicalPhysicalIds"] = []
            checkpoint["rejectedCanonicalPhysicalIds"] = []
            checkpoint["rejectedCandidateAttempts"] = []
            checkpoint["fullProviderMatrixProofFingerprints"] = (
                [proof_fingerprint] if proof_fingerprint else []
            )
            checkpoint["attemptCursor"] = int(ledger.get("attemptCursor") or 0)
            checkpoint["queryCursor"] = 0
            existing = next(
                (
                    item
                    for item in checkpoints
                    if str(item.get("scopeFingerprint") or "") == str(checkpoint["scopeFingerprint"])
                ),
                None,
            )
            if existing is None:
                checkpoints.append(checkpoint)
            else:
                existing["issueCodes"] = sorted(
                    set(existing.get("issueCodes") or []) | set(checkpoint.get("issueCodes") or [])
                )
                for field in (
                    "usedQueryFingerprints",
                    "executedQueryFingerprints",
                    "admittedCanonicalPhysicalIds",
                    "fullProviderMatrixProofFingerprints",
                    "rejectedCanonicalPhysicalIds",
                    "rejectedCandidateAttempts",
                ):
                    for value in checkpoint.get(field) or []:
                        self._append_unique(existing.setdefault(field, []), str(value))
                # The cursor is not a planned-query ordinal.  It is a compact
                # count of the exact executed evidence retained for this
                # server-derived scope, including evidence inherited from a
                # prior retry.
                existing["queryCursor"] = len(existing.get("executedQueryFingerprints") or [])
                existing["attemptCursor"] = len(existing.get("rejectedCandidateAttempts") or [])
            rejected_ids = ledger.setdefault("rejectedCanonicalPhysicalIds", [])
            self._append_unique(rejected_ids, str(checkpoint["candidatePhysicalId"]))
            for value in checkpoint["fullProviderMatrixProofFingerprints"]:
                self._append_unique(ledger.setdefault("fullProviderMatrixProofFingerprints", []), str(value))

    @classmethod
    def _planned_query_fingerprints_for_segment(
        cls,
        segment: dict[str, Any],
        *,
        candidate_pools: Iterable[dict[str, Any]],
    ) -> list[str]:
        """Return planned query hashes that belong to one exact repair slot."""

        semantic = cls._segment_semantic(segment)
        lineage = cls._segment_lineage(segment)
        values: set[str] = set()
        for source in (semantic,):
            if not isinstance(source, dict):
                continue
            for value in (
                source.get("queryFingerprint"),
                source.get("routeQueryFingerprint"),
                (source.get("routeQueryScope") or {}).get("queryFingerprint")
                if isinstance(source.get("routeQueryScope"), dict)
                else "",
            ):
                if isinstance(value, str) and value.strip():
                    values.add(value.strip())
        for candidate in candidate_pools:
            if not isinstance(candidate, dict) or not cls._candidate_matches_lineage(candidate, lineage):
                continue
            for value in (
                candidate.get("queryFingerprint"),
                candidate.get("routeQueryFingerprint"),
                (candidate.get("routeQueryScope") or {}).get("queryFingerprint")
                if isinstance(candidate.get("routeQueryScope"), dict)
                else "",
            ):
                if isinstance(value, str) and value.strip():
                    values.add(value.strip())
        return sorted(values)

    @staticmethod
    def _repair_checkpoint_scope_fingerprints(ledger: Any) -> set[str]:
        if not isinstance(ledger, dict):
            return set()
        return {
            str(item.get("scopeFingerprint") or "").strip()
            for item in ledger.get("routeBadCheckpoints") or []
            if isinstance(item, dict) and str(item.get("scopeFingerprint") or "").strip()
        }

    @staticmethod
    def _adjacent_route_anchor_signature(scope: dict[str, Any]) -> Optional[tuple[tuple[str, str, str], ...]]:
        """Compare physical neighbors while allowing the candidate identity to change."""

        candidate_physical_id = str(scope.get("candidatePhysicalId") or "").strip()
        adjacent_keys = scope.get("adjacentRouteLedgerKeys")
        if not candidate_physical_id or not isinstance(adjacent_keys, list) or not adjacent_keys:
            return None
        signature: list[tuple[str, str, str]] = []
        for item in adjacent_keys:
            if not isinstance(item, dict):
                return None
            from_physical_id = str(item.get("fromPhysicalId") or "").strip()
            to_physical_id = str(item.get("toPhysicalId") or "").strip()
            mode = normalize_route_mode(str(item.get("mode") or "").strip())
            if not from_physical_id or not to_physical_id or not mode:
                return None
            if to_physical_id == candidate_physical_id and from_physical_id != candidate_physical_id:
                signature.append(("previous", from_physical_id, mode))
            elif from_physical_id == candidate_physical_id and to_physical_id != candidate_physical_id:
                signature.append(("next", to_physical_id, mode))
            else:
                return None
        if len({side for side, _, _ in signature}) != len(signature):
            return None
        return tuple(signature)

    @staticmethod
    def _same_repair_slot_scope(checkpoint: dict[str, Any], scope: dict[str, Any]) -> bool:
        """Compare only durable slot facts shared by a failed and replacement POI."""

        keys = (
            "continuationMode",
            "rootPortfolioId",
            "planningSelectionRootTurnId",
            "briefId",
            "poolId",
            "dayNumber",
            "planningSlotId",
            "adjacentAnchorIds",
            "adjacentRouteLedgerKeys",
            "routeContractFingerprint",
        )
        for key in keys:
            if key == "adjacentAnchorIds":
                checkpoint_value = checkpoint.get(key)
                scope_value = scope.get(key)
                if (
                    not isinstance(checkpoint_value, list)
                    or not isinstance(scope_value, list)
                    or checkpoint_value != scope_value
                ):
                    return False
                continue
            if key == "adjacentRouteLedgerKeys":
                checkpoint_signature = PortfolioRouteFeasibilityService._adjacent_route_anchor_signature(
                    checkpoint
                )
                scope_signature = PortfolioRouteFeasibilityService._adjacent_route_anchor_signature(scope)
                if checkpoint_signature is None or checkpoint_signature != scope_signature:
                    return False
                continue
            checkpoint_value = str(checkpoint.get(key) or "")
            scope_value = str(scope.get(key) or "")
            # An older snapshot can lack the optional planning root.  It must
            # not become an accidental mismatch, but a present value is exact.
            if key == "planningSelectionRootTurnId" and not checkpoint_value and not scope_value:
                continue
            if checkpoint_value != scope_value:
                return False
        return True

    def _route_bad_checkpoint_scope_for_probe(
        self,
        ledger: dict[str, Any],
        scope: dict[str, Any],
    ) -> Optional[str]:
        matches = [
            item
            for item in ledger.get("routeBadCheckpoints") or []
            if isinstance(item, dict) and self._same_repair_slot_scope(item, scope)
        ]
        if len(matches) != 1:
            return None
        return str(matches[0].get("scopeFingerprint") or "").strip() or None

    def _route_bad_checkpoint_scope_for_segment(
        self,
        ledger: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
        transport_mode: str,
    ) -> str:
        """Locate the sealed checkpoint for one current server segment."""

        scope = self._route_repair_scope_certificate(
            snapshot,
            segment=segment,
            candidate=self._segment_poi_payload(segment),
            transport_mode=transport_mode,
        )
        if scope is None:
            return ""
        return self._route_bad_checkpoint_scope_for_probe(ledger, scope) or ""

    def _inherit_matching_repair_evidence(
        self,
        snapshot: dict[str, Any],
        ledger: dict[str, Any],
    ) -> None:
        """Carry only exact persisted repair evidence into the same scope.

        A legacy ledger without a certificate, or a certificate that does not
        exactly match the freshly derived snapshot scope, deliberately carries
        nothing.  This keeps another root/day/slot from suppressing a repair.
        """

        quality = snapshot.get("portfolioRouteQuality")
        prior = quality.get("repairAttemptLedger") if isinstance(quality, dict) else None
        prior_scopes = self._repair_checkpoint_scope_fingerprints(prior)
        current_scopes = self._repair_checkpoint_scope_fingerprints(ledger)
        if not prior_scopes or prior_scopes != current_scopes or not isinstance(prior, dict):
            return
        for field in (
            "candidateProbeKeys",
            "candidateProbeEvidence",
            "completedMatrixCandidateKeys",
            "completedMatrixEvidence",
            "rejectedCanonicalPhysicalIds",
            "rejectedCandidateAttempts",
            "executedQueryFingerprints",
            "admittedCanonicalPhysicalIds",
            "fullProviderMatrixProofFingerprints",
        ):
            values = prior.get(field)
            if not isinstance(values, list):
                continue
            target = ledger.setdefault(field, [])
            for value in values:
                if value not in target:
                    target.append(copy.deepcopy(value))
        for checkpoint in ledger.get("routeBadCheckpoints") or []:
            if not isinstance(checkpoint, dict):
                continue
            prior_checkpoint = next(
                (
                    item
                    for item in prior.get("routeBadCheckpoints") or []
                    if isinstance(item, dict)
                    and str(item.get("scopeFingerprint") or "")
                    == str(checkpoint.get("scopeFingerprint") or "")
                ),
                None,
            )
            if not isinstance(prior_checkpoint, dict):
                continue
            for field in (
                "executedQueryFingerprints",
                "admittedCanonicalPhysicalIds",
                "fullProviderMatrixProofFingerprints",
            ):
                for value in prior_checkpoint.get(field) or []:
                    self._append_unique(checkpoint.setdefault(field, []), str(value))
            checkpoint["queryCursor"] = len(checkpoint.get("executedQueryFingerprints") or [])
        ledger["queryCursor"] = len(ledger.get("executedQueryFingerprints") or [])
        ledger["candidateProbeCount"] = max(
            int(ledger.get("candidateProbeCount") or 0),
            int(prior.get("candidateProbeCount") or 0),
            len(ledger.get("candidateProbeKeys") or []),
        )
        ledger["candidateEvaluationCount"] = max(
            int(ledger.get("candidateEvaluationCount") or 0),
            int(prior.get("candidateEvaluationCount") or 0),
            len(ledger.get("completedMatrixCandidateKeys") or []),
        )
        ledger["attemptCursor"] = max(
            int(ledger.get("attemptCursor") or 0),
            int(prior.get("attemptCursor") or 0),
        )

    def _route_repair_snapshot_scope_fingerprint(
        self,
        snapshot: dict[str, Any],
        *,
        transport_mode: str,
    ) -> str:
        """Fingerprint only authoritative route-repair facts, never labels/ids."""

        anchors: list[dict[str, Any]] = []
        for segment in self._snapshot_segments(snapshot):
            if not bool(segment.get("route_anchor")):
                continue
            candidate_physical_id = self._canonical_physical_id(self._segment_poi_payload(segment))
            contract = self._route_decision_contract_resolution(snapshot, segment)
            adjacent_keys = self._adjacent_route_ledger_keys(
                snapshot,
                segment=segment,
                candidate_physical_id=candidate_physical_id,
                transport_mode=transport_mode,
            )
            adjacent_anchor_ids = self._adjacent_anchor_ids(
                snapshot,
                segment=segment,
            )
            anchors.append(
                {
                    "briefId": str(segment.get("briefId") or ""),
                    "poolId": str(segment.get("poolId") or ""),
                    "planningSlotId": str(segment.get("planningSlotId") or ""),
                    "dayNumber": int(segment.get("dayNumber") or 0),
                    "candidatePhysicalId": candidate_physical_id,
                    "adjacentAnchorIds": adjacent_anchor_ids,
                    "adjacentRouteLedgerKeys": adjacent_keys,
                    "routeContractFingerprint": str((contract or {}).get("fingerprint") or ""),
                }
            )
        return canonical_fingerprint(
            {
                "rootPortfolioId": self._snapshot_root_portfolio_id(snapshot),
                "planningSelectionRootTurnId": self._snapshot_planning_root_turn_id(snapshot),
                "anchors": sorted(
                    anchors,
                    key=lambda item: (
                        int(item["dayNumber"]),
                        str(item["briefId"]),
                        str(item["planningSlotId"]),
                        str(item["candidatePhysicalId"]),
                    ),
                ),
                "portfolioDayAnchorTargets": snapshot.get("portfolioDayAnchorTargets") or {},
            }
        )

    def _has_fresh_exact_repair_evidence(
        self,
        snapshot: dict[str, Any],
        *,
        transport_mode: str,
        candidate_pools: Iterable[dict[str, Any]],
        prior: dict[str, Any],
    ) -> bool:
        """Allow an exact retry only for new Admission or verified matrix evidence.

        A direction, budget, planned query fingerprint, label, or ordinal is
        deliberately not evidence.  Candidates are first rebound to the same
        authoritative snapshot slot before their canonical AMap identity is
        considered new.
        """

        checkpoints = [
            item for item in prior.get("routeBadCheckpoints") or [] if isinstance(item, dict)
        ]
        if not checkpoints:
            return False
        for candidate in candidate_pools:
            if not isinstance(candidate, dict):
                continue
            candidate_physical_id = self._canonical_physical_id(candidate)
            if not candidate_physical_id:
                continue
            for segment in self._snapshot_segments(snapshot):
                lineage = self._segment_lineage(segment)
                if not self._candidate_matches_lineage(candidate, lineage):
                    continue
                current_physical_id = self._canonical_physical_id(self._segment_poi_payload(segment))
                if candidate_physical_id == current_physical_id:
                    continue
                candidate_scope = self._route_repair_scope_certificate(
                    snapshot,
                    segment=segment,
                    candidate=candidate,
                    transport_mode=transport_mode,
                )
                if candidate_scope is None:
                    continue
                parent_scope = self._route_bad_checkpoint_scope_for_probe(
                    prior,
                    candidate_scope,
                )
                if parent_scope is None:
                    continue
                prior_checkpoint = next(
                    (
                        checkpoint
                        for checkpoint in checkpoints
                        if str(checkpoint.get("scopeFingerprint") or "") == parent_scope
                    ),
                    None,
                )
                if not isinstance(prior_checkpoint, dict):
                    continue
                prior_admitted = {
                    str(value).strip()
                    for value in prior_checkpoint.get("admittedCanonicalPhysicalIds") or []
                    if isinstance(value, str) and value.strip()
                }
                prior_matrix_proofs = {
                    str(value).strip()
                    for value in prior_checkpoint.get("fullProviderMatrixProofFingerprints") or []
                    if isinstance(value, str) and value.strip()
                }
                raw_matrix_proof = candidate.get("_routeReplacementMatrixProof")
                matrix_proof = self._replacement_matrix_proof_fingerprint(raw_matrix_proof)
                matrix_scope_fingerprint = (
                    str(raw_matrix_proof.get("repairScopeFingerprint") or "")
                    if isinstance(raw_matrix_proof, dict)
                    else ""
                )
                if (
                    matrix_proof
                    and matrix_scope_fingerprint
                    == str(candidate_scope.get("scopeFingerprint") or "")
                    and matrix_proof not in prior_matrix_proofs
                ):
                    return True
                if self._consumer_admitted(candidate) and candidate_physical_id not in prior_admitted:
                    return True
        return False

    def _persisted_exact_repair_no_progress(
        self,
        snapshot: dict[str, Any],
        *,
        transport_mode: str,
        snapshot_scope_fingerprint: str,
        candidate_pools: Iterable[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Return a prior exact repair ledger when the exact scope has no progress.

        This is intentionally scoped to the same persisted route certificate;
        it does not participate in global `discover_next` exploration.
        """

        quality = snapshot.get("portfolioRouteQuality")
        prior = quality.get("repairAttemptLedger") if isinstance(quality, dict) else None
        if not isinstance(prior, dict):
            return None
        if str(prior.get("snapshotRepairScopeFingerprint") or "") != snapshot_scope_fingerprint:
            return None
        checkpoints = [
            item for item in prior.get("routeBadCheckpoints") or [] if isinstance(item, dict)
        ]
        if not checkpoints:
            return None
        if any(
            set(
                str(value).strip()
                for value in checkpoint.get("usedQueryFingerprints") or []
                if isinstance(value, str) and value.strip()
            )
            - set(
                str(value).strip()
                for value in checkpoint.get("executedQueryFingerprints") or []
                if isinstance(value, str) and value.strip()
            )
            for checkpoint in checkpoints
        ):
            return None
        if self._has_fresh_exact_repair_evidence(
            snapshot,
            transport_mode=transport_mode,
            candidate_pools=candidate_pools,
            prior=prior,
        ):
            return None
        segments = self._snapshot_segments(snapshot)
        current_scope_fingerprints: set[str] = set()
        for checkpoint in checkpoints:
            matching = [
                segment
                for segment in segments
                if str(segment.get("briefId") or "") == str(checkpoint.get("briefId") or "")
                and str(segment.get("poolId") or "") == str(checkpoint.get("poolId") or "")
                and str(segment.get("planningSlotId") or "")
                == str(checkpoint.get("planningSlotId") or "")
                and int(segment.get("dayNumber") or 0) == int(checkpoint.get("dayNumber") or 0)
            ]
            if len(matching) != 1:
                return None
            segment = matching[0]
            current = self._route_repair_scope_certificate(
                snapshot,
                segment=segment,
                candidate=self._segment_poi_payload(segment),
                transport_mode=transport_mode,
            )
            if current is None or str(current.get("scopeFingerprint") or "") != str(
                checkpoint.get("scopeFingerprint") or ""
            ):
                return None
            current_scope_fingerprints.add(str(current["scopeFingerprint"]))
        if current_scope_fingerprints != self._repair_checkpoint_scope_fingerprints(prior):
            return None
        return copy.deepcopy(prior)

    def _reserve_replacement_candidate(
        self,
        ledger: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        segment_id: str,
        candidate: dict[str, Any],
        city: str,
        transport_mode: str,
    ) -> Optional[dict[str, Any]]:
        """Bound Provider probes by physical candidate and adjacent route scope."""

        segment = next(
            (
                item
                for item in self._snapshot_segments(snapshot)
                if str(item.get("id") or "") == str(segment_id or "")
            ),
            None,
        )
        if segment is None:
            self._record_repair_rejection(ledger, "candidate_route_scope_missing", candidate)
            return None
        admitted_candidate, admission_rejection = self._admit_replacement_candidate(
            candidate,
            segment=segment,
            city=city,
        )
        if admitted_candidate is None:
            self._record_repair_rejection(
                ledger,
                admission_rejection,
                candidate,
            )
            return None
        # Shared provider facts may carry an old consumer report. Replace it
        # with the report produced for this exact consuming segment before any
        # route authorization, probe accounting, or materialization.
        candidate.clear()
        candidate.update(admitted_candidate)
        scope = self._route_repair_scope_certificate(
            snapshot,
            segment=segment,
            candidate=candidate,
            transport_mode=transport_mode,
        )
        if scope is None:
            self._record_repair_rejection(ledger, "candidate_route_scope_missing", candidate)
            return None
        route_bad_checkpoint_scope = self._route_bad_checkpoint_scope_for_probe(ledger, scope)
        if ledger.get("routeBadCheckpoints") and route_bad_checkpoint_scope is None:
            self._record_repair_rejection(ledger, "candidate_route_scope_missing", candidate)
            return None
        current_physical_id = self._canonical_physical_id(self._segment_poi_payload(segment))
        candidate_id = str(scope["candidatePhysicalId"])
        if candidate_id == current_physical_id:
            self._record_repair_rejection(
                ledger,
                "same_or_missing_identity",
                candidate,
                scope_fingerprint=route_bad_checkpoint_scope or "",
            )
            return None
        key = canonical_fingerprint(
            {
                "scopeFingerprint": str(scope["scopeFingerprint"]),
                "candidatePhysicalId": candidate_id,
            }
        )
        probes = ledger.setdefault("candidateProbeKeys", [])
        if key in probes:
            self._record_repair_rejection(
                ledger,
                "duplicate_candidate_route_probe",
                candidate,
                scope_fingerprint=route_bad_checkpoint_scope or "",
            )
            return None
        if int(ledger.get("candidateProbeCount") or 0) >= self.MAX_REPLACEMENT_CANDIDATE_PROBES:
            self._record_repair_rejection(
                ledger,
                "replacement_candidate_budget_exhausted",
                candidate,
                scope_fingerprint=route_bad_checkpoint_scope or "",
            )
            return None
        try:
            self._authorize_route_work(
                snapshot,
                route_ledger_keys=scope["adjacentRouteLedgerKeys"],
                mode=transport_mode,
                reason="replacement_adjacent",
                condition="always",
                candidate_physical_id=candidate_id,
                repair_scope_certificate=scope,
            )
        except _RouteAuthorizationPreconditionError:
            self._record_repair_rejection(
                ledger,
                "candidate_route_authorization_precondition_failed",
                candidate,
                scope_fingerprint=route_bad_checkpoint_scope or "",
            )
            return None
        probes.append(key)
        evidence = {
            "candidateProbeKey": key,
            "scopeFingerprint": str(scope["scopeFingerprint"]),
            "candidatePhysicalId": candidate_id,
            "repairScopeCertificate": copy.deepcopy(scope),
        }
        if route_bad_checkpoint_scope:
            evidence["routeBadCheckpointScopeFingerprint"] = route_bad_checkpoint_scope
        ledger.setdefault("candidateProbeEvidence", []).append(evidence)
        ledger["candidateProbeCount"] = int(ledger.get("candidateProbeCount") or 0) + 1
        if self._consumer_admitted(candidate):
            self._append_unique(
                ledger.setdefault("admittedCanonicalPhysicalIds", []),
                candidate_id,
            )
            if route_bad_checkpoint_scope:
                for checkpoint in ledger.get("routeBadCheckpoints") or []:
                    if (
                        isinstance(checkpoint, dict)
                        and str(checkpoint.get("scopeFingerprint") or "")
                        == route_bad_checkpoint_scope
                    ):
                        self._append_unique(
                            checkpoint.setdefault("admittedCanonicalPhysicalIds", []),
                            candidate_id,
                        )
        return {
            "candidateProbeKey": key,
            **({"routeBadCheckpointScopeFingerprint": route_bad_checkpoint_scope} if route_bad_checkpoint_scope else {}),
            **scope,
        }

    @staticmethod
    def _repair_scope_certificate_from_probe(probe: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in probe.items()
            if key not in {"candidateProbeKey", "routeBadCheckpointScopeFingerprint"}
        }

    def _record_completed_candidate_matrix(
        self,
        ledger: dict[str, Any],
        *,
        probe: dict[str, Any],
        candidate: dict[str, Any],
        matrix_proof: Any,
    ) -> None:
        candidate_key = str(probe.get("candidateProbeKey") or "")
        if not candidate_key:
            return
        proof_fingerprint = self._replacement_matrix_proof_fingerprint(matrix_proof)
        if not proof_fingerprint:
            return
        completed = ledger.setdefault("completedMatrixCandidateKeys", [])
        if candidate_key not in completed:
            completed.append(candidate_key)
            ledger["candidateEvaluationCount"] = int(ledger.get("candidateEvaluationCount") or 0) + 1
        candidate_physical_id = self._canonical_physical_id(candidate)
        self._append_unique(
            ledger.setdefault("fullProviderMatrixProofFingerprints", []),
            proof_fingerprint,
        )
        if self._consumer_admitted(candidate):
            self._append_unique(
                ledger.setdefault("admittedCanonicalPhysicalIds", []),
                candidate_physical_id,
            )
        repair_scope_certificate = self._repair_scope_certificate_from_probe(probe)
        evidence = {
            "candidateProbeKey": candidate_key,
            "scopeFingerprint": str(probe.get("scopeFingerprint") or ""),
            "candidatePhysicalId": candidate_physical_id,
            "fullProviderMatrixProofFingerprint": proof_fingerprint,
            "consumerAdmissionPassed": self._consumer_admitted(candidate),
            "repairScopeCertificate": repair_scope_certificate,
        }
        parent_scope_fingerprint = str(probe.get("routeBadCheckpointScopeFingerprint") or "")
        if parent_scope_fingerprint:
            evidence["routeBadCheckpointScopeFingerprint"] = parent_scope_fingerprint
        completed_evidence = ledger.setdefault("completedMatrixEvidence", [])
        if evidence not in completed_evidence:
            completed_evidence.append(evidence)
        for checkpoint in ledger.get("routeBadCheckpoints") or []:
            if not isinstance(checkpoint, dict):
                continue
            if str(checkpoint.get("scopeFingerprint") or "") != parent_scope_fingerprint:
                continue
            self._append_unique(
                checkpoint.setdefault("fullProviderMatrixProofFingerprints", []),
                proof_fingerprint,
            )
            if self._consumer_admitted(candidate):
                self._append_unique(
                    checkpoint.setdefault("admittedCanonicalPhysicalIds", []),
                    candidate_physical_id,
                )
            checkpoint_evidence = checkpoint.setdefault("candidateEvidence", [])
            if evidence not in checkpoint_evidence:
                checkpoint_evidence.append(copy.deepcopy(evidence))

    @classmethod
    def _route_pair_ids(cls, snapshot: dict[str, Any]) -> list[list[str]]:
        pairs: list[list[str]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            anchor_ids = [
                str(segment.get("id") or "")
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
                and isinstance(segment.get("semanticMetadata"), dict)
                and bool(segment["semanticMetadata"].get("routeAnchor"))
                and str(segment.get("id") or "")
            ]
            pairs.extend([[left, right] for left, right in zip(anchor_ids, anchor_ids[1:])])
        return pairs

    @classmethod
    def _touched_route_pairs(cls, snapshot: dict[str, Any], segment_id: str) -> list[list[str]]:
        return [pair for pair in cls._route_pair_ids(snapshot) if segment_id and segment_id in pair]

    @staticmethod
    def _merge_pairs(existing: list[list[str]], additions: list[list[str]]) -> list[list[str]]:
        merged = [list(pair) for pair in existing]
        for pair in additions:
            normalized = [str(item) for item in pair]
            if normalized not in merged:
                merged.append(normalized)
        return merged

    def _find_nearby_replacement(
        self,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
        *,
        city: str,
        transport_mode: str,
        preview_id: str,
        call_metrics: dict[str, int],
        repair_ledger: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if not self._segment_replacement_allowed(segment):
            return None
        current_scope = self._route_repair_scope_certificate(
            snapshot,
            segment=segment,
            candidate=self._segment_poi_payload(segment),
            transport_mode=transport_mode,
        )
        if current_scope is None:
            self._record_repair_rejection(
                repair_ledger,
                "candidate_route_scope_missing",
                self._segment_poi_payload(segment),
            )
            return None
        baseline_result = self._evaluate_snapshot(
            copy.deepcopy(snapshot),
            city=city,
            transport_mode=transport_mode,
            preview_id=f"{preview_id}_nearby_baseline",
            call_metrics=call_metrics,
            repair_scope_certificate=current_scope,
        )
        baseline_legs = self._replacement_route_legs(
            baseline_result,
            snapshot=snapshot,
            segment_id=str(segment.get("id") or ""),
        )
        if baseline_legs is None:
            self._record_repair_rejection(repair_ledger, "provider_route_baseline_missing")
            return None
        anchors = self._adjacent_anchor_items(snapshot, segment["id"])
        search_centers = self._nearby_search_centers(anchors)
        if not search_centers:
            return None
        nearby_keyword = self._nearby_search_keyword(segment)
        nearby_category = self._nearby_search_category(segment)
        seen_candidate_ids: set[str] = set()
        for center in search_centers[: self.MAX_NEARBY_SEARCHES]:
            try:
                self._authorize_nearby_work(
                    center=center,
                    keyword=nearby_keyword,
                    category=nearby_category,
                    radius=self.NEARBY_SEARCH_RADIUS_METERS,
                )
            except _RouteAuthorizationPreconditionError:
                self._record_repair_rejection(
                    repair_ledger,
                    "nearby_authorization_precondition_failed",
                    scope_fingerprint=str(current_scope["scopeFingerprint"]),
                )
                return None
            try:
                call_metrics["nearbySearchCount"] += 1
                repair_ledger["nearbyQueryCount"] += 1
                query_scope_fingerprint = canonical_fingerprint(
                    {
                        "scopeFingerprint": current_scope["scopeFingerprint"],
                        "queryKind": "nearby",
                        "searchCenter": str(center["id"]),
                        "keyword": nearby_keyword,
                        "category": nearby_category,
                        "radiusMeters": self.NEARBY_SEARCH_RADIUS_METERS,
                    }
                )
                response = self.map_poi_service.search_nearby(
                    city,
                    center["longitude"],
                    center["latitude"],
                    nearby_keyword,
                    category=nearby_category,
                    radius=self.NEARBY_SEARCH_RADIUS_METERS,
                    limit=12,
                    query_scope_fingerprint=query_scope_fingerprint,
                )
            except Exception:
                continue
            self._record_executed_query(
                repair_ledger,
                scope_fingerprint=str(current_scope["scopeFingerprint"]),
                query_fingerprint=query_scope_fingerprint,
            )
            ranked_candidates: list[tuple[float, dict[str, Any]]] = []
            for poi in getattr(response, "pois", []) or []:
                candidate = self._nearby_candidate(poi, segment)
                candidate_id = str(candidate.get("amapId") or candidate.get("id") or "").upper()
                if not candidate_id or candidate_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(candidate_id)
                if not self._candidate_grounded(candidate, city=city):
                    self._record_repair_rejection(repair_ledger, "grounding_failed")
                    continue
                score = self._geometry_score(snapshot, segment, candidate, transport_mode)
                ranked_candidates.append((float(score.added_distance_km) if score is not None else math.inf, candidate))
            verified_candidates: list[tuple[tuple[float, int, str], dict[str, Any]]] = []
            for _added_distance_km, candidate in sorted(ranked_candidates, key=lambda item: item[0])[
                : self.MAX_NEARBY_CANDIDATE_EVALUATIONS
            ]:
                probe = self._reserve_replacement_candidate(
                    repair_ledger,
                    snapshot=snapshot,
                    segment_id=str(segment.get("id") or ""),
                    candidate=candidate,
                    city=city,
                    transport_mode=transport_mode,
                )
                if probe is None:
                    continue
                repair_ledger["routePairEvaluationCount"] += len(self._touched_route_pairs(snapshot, segment["id"]))
                trial = copy.deepcopy(snapshot)
                self._replace_segment_candidate(trial, segment["id"], candidate)
                result = self._evaluate_snapshot(
                    trial,
                    city=city,
                    transport_mode=transport_mode,
                    preview_id=preview_id,
                    call_metrics=call_metrics,
                    repair_scope_certificate=self._repair_scope_certificate_from_probe(probe),
                )
                if self._replacement_route_matrix_complete(
                    result,
                    snapshot=trial,
                    segment_id=str(segment.get("id") or ""),
                ):
                    candidate_id = str(candidate.get("amapId") or candidate.get("id") or "")
                    candidate_legs = self._replacement_route_legs(
                        result,
                        snapshot=trial,
                        segment_id=str(segment.get("id") or ""),
                    )
                    matrix_score = self._replacement_matrix_score(
                        baseline_legs,
                        candidate_legs,
                        segment=segment,
                        candidate=candidate,
                        trial_snapshot=trial,
                        route_evidence=list(result.get("evidence") or []),
                    )
                    if matrix_score is None:
                        self._record_repair_rejection(repair_ledger, "provider_route_matrix_missing")
                        continue
                    accepted_candidate = self._candidate_with_matrix_proof(
                        candidate,
                        baseline_legs=baseline_legs,
                        candidate_legs=candidate_legs,
                        matrix_score=matrix_score,
                        decision_policy=self._provider_decision_policy(trial, segment, candidate),
                    )
                    if accepted_candidate is None:
                        self._record_repair_rejection(
                            repair_ledger,
                            "provider_route_matrix_missing",
                            candidate,
                        )
                        continue
                    scoped_matrix_proof = self._bind_matrix_proof_to_repair_scope(
                        accepted_candidate["_routeReplacementMatrixProof"],
                        probe,
                    )
                    if scoped_matrix_proof is None:
                        self._record_repair_rejection(
                            repair_ledger,
                            "provider_route_matrix_missing",
                            candidate,
                        )
                        continue
                    accepted_candidate["_routeReplacementMatrixProof"] = scoped_matrix_proof
                    self._record_completed_candidate_matrix(
                        repair_ledger,
                        probe=probe,
                        candidate=candidate,
                        matrix_proof=accepted_candidate["_routeReplacementMatrixProof"],
                    )
                    if matrix_score.detour_level == "unacceptable":
                        self._record_repair_rejection(repair_ledger, "provider_route_matrix_unacceptable")
                        continue
                    accepted_candidate["_exactRouteRepairScopeCertificate"] = (
                        self._repair_scope_certificate_from_probe(probe)
                    )
                    verified_candidates.append(
                        (
                            (
                                float(matrix_score.generalized_cost_delta or 0.0),
                                int(matrix_score.estimated_duration_minutes),
                                candidate_id,
                            ),
                            accepted_candidate,
                        )
                    )
                else:
                    self._record_repair_rejection(repair_ledger, "route_quality_failed")
            if verified_candidates:
                return min(verified_candidates, key=lambda item: item[0])[1]
        return None

    @staticmethod
    def _nearby_search_centers(
        anchors: object,
    ) -> list[dict[str, float | str]]:
        """Return route-corridor discovery centers without inventing POIs.

        With two adjacent anchors, the geographic midpoint is searched first,
        followed by both endpoints.  The midpoint only broadens AMap discovery;
        every returned POI must still pass the full provider route matrix, so
        city-specific road and transit topology remains authoritative.
        """

        if not isinstance(anchors, (tuple, list)):
            return []
        valid: list[dict[str, float | str]] = []
        for raw in anchors:
            poi = raw.get("poi") if isinstance(raw, dict) else None
            if poi is None:
                continue
            try:
                longitude = float(poi.longitude)
                latitude = float(poi.latitude)
            except (TypeError, ValueError):
                continue
            valid.append(
                {
                    "id": str(poi.amap_id or poi.id or f"{longitude:.6f},{latitude:.6f}"),
                    "longitude": longitude,
                    "latitude": latitude,
                }
            )
        if len(valid) == 2:
            left, right = valid
            midpoint = {
                "id": f"corridor:{left['id']}:{right['id']}",
                "longitude": (float(left["longitude"]) + float(right["longitude"])) / 2,
                "latitude": (float(left["latitude"]) + float(right["latitude"])) / 2,
            }
            return [midpoint, left, right]
        return valid

    def _find_discovered_replacement(
        self,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
        *,
        city: str,
        transport_mode: str,
        preview_id: str,
        call_metrics: dict[str, int],
        repair_ledger: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        if not self._segment_replacement_allowed(segment):
            return None
        current_scope = self._route_repair_scope_certificate(
            snapshot,
            segment=segment,
            candidate=self._segment_poi_payload(segment),
            transport_mode=transport_mode,
        )
        if current_scope is None:
            self._record_repair_rejection(
                repair_ledger,
                "candidate_route_scope_missing",
                self._segment_poi_payload(segment),
            )
            return None
        baseline_result = self._evaluate_snapshot(
            copy.deepcopy(snapshot),
            city=city,
            transport_mode=transport_mode,
            preview_id=f"{preview_id}_web_baseline",
            call_metrics=call_metrics,
            repair_scope_certificate=current_scope,
        )
        baseline_legs = self._replacement_route_legs(
            baseline_result,
            snapshot=snapshot,
            segment_id=str(segment.get("id") or ""),
        )
        if baseline_legs is None:
            self._record_repair_rejection(repair_ledger, "provider_route_baseline_missing")
            return None
        semantic = self._segment_semantic(segment)
        discovery = self.poi_discovery_service or PoiDiscoveryService(map_poi_service=self.map_poi_service)
        scope_fingerprint = str(current_scope.get("scopeFingerprint") or "")
        discovery_query_fingerprint = self._next_planned_query_fingerprint(
            repair_ledger,
            scope_fingerprint=scope_fingerprint,
        ) or canonical_fingerprint(
            {
                "scopeFingerprint": scope_fingerprint,
                "provider": "poi_discovery",
                "city": city,
                "intentType": str(semantic.get("intentType") or "meal"),
                "rawNeed": str(semantic.get("rawNeed") or "当地特色餐饮"),
                "triggerReason": "route_repair_after_amap_candidate_shortage",
            }
        )
        try:
            result = discovery.discover(
                city=city,
                intent_type=str(semantic.get("intentType") or "meal"),
                raw_need=str(semantic.get("rawNeed") or "当地特色餐饮"),
                trigger_reason="route_repair_after_amap_candidate_shortage",
            )
        except Exception:
            return None
        self._record_executed_query(
            repair_ledger,
            scope_fingerprint=scope_fingerprint,
            query_fingerprint=discovery_query_fingerprint,
        )
        if result.status != "grounded":
            return None
        lineage = self._segment_lineage(segment)
        ranked: list[tuple[tuple[float, int, str], dict[str, Any]]] = []
        for raw in result.candidates:
            candidate = {
                **dict(raw),
                **lineage,
                "candidateSource": "web_seed_amap_grounded",
            }
            if not self._candidate_grounded(candidate, city=city):
                self._record_repair_rejection(repair_ledger, "grounding_failed")
                continue
            admitted_candidate, admission_rejection = self._admit_replacement_candidate(
                candidate,
                segment=segment,
                city=city,
            )
            if admitted_candidate is None:
                self._record_repair_rejection(
                    repair_ledger,
                    admission_rejection,
                    candidate,
                )
                continue
            candidate = admitted_candidate
            probe = self._reserve_replacement_candidate(
                repair_ledger,
                snapshot=snapshot,
                segment_id=str(segment.get("id") or ""),
                candidate=candidate,
                city=city,
                transport_mode=transport_mode,
            )
            if probe is None:
                continue
            repair_ledger["routePairEvaluationCount"] += len(self._touched_route_pairs(snapshot, segment["id"]))
            trial = copy.deepcopy(snapshot)
            self._replace_segment_candidate(trial, segment["id"], candidate)
            route_result = self._evaluate_snapshot(
                trial,
                city=city,
                transport_mode=transport_mode,
                preview_id=preview_id,
                call_metrics=call_metrics,
                repair_scope_certificate=self._repair_scope_certificate_from_probe(probe),
            )
            if self._replacement_route_matrix_complete(
                route_result,
                snapshot=trial,
                segment_id=str(segment.get("id") or ""),
            ):
                candidate_id = str(candidate.get("amapId") or candidate.get("id") or "")
                candidate_legs = self._replacement_route_legs(
                    route_result,
                    snapshot=trial,
                    segment_id=str(segment.get("id") or ""),
                )
                matrix_score = self._replacement_matrix_score(
                    baseline_legs,
                    candidate_legs,
                    segment=segment,
                    candidate=candidate,
                    trial_snapshot=trial,
                    route_evidence=list(route_result.get("evidence") or []),
                )
                if matrix_score is None:
                    self._record_repair_rejection(repair_ledger, "provider_route_matrix_missing")
                    continue
                accepted_candidate = self._candidate_with_matrix_proof(
                    candidate,
                    baseline_legs=baseline_legs,
                    candidate_legs=candidate_legs,
                    matrix_score=matrix_score,
                    decision_policy=self._provider_decision_policy(trial, segment, candidate),
                )
                if accepted_candidate is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                    )
                    continue
                scoped_matrix_proof = self._bind_matrix_proof_to_repair_scope(
                    accepted_candidate["_routeReplacementMatrixProof"],
                    probe,
                )
                if scoped_matrix_proof is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                    )
                    continue
                accepted_candidate["_routeReplacementMatrixProof"] = scoped_matrix_proof
                self._record_completed_candidate_matrix(
                    repair_ledger,
                    probe=probe,
                    candidate=candidate,
                    matrix_proof=accepted_candidate["_routeReplacementMatrixProof"],
                )
                if matrix_score.detour_level == "unacceptable":
                    self._record_repair_rejection(repair_ledger, "provider_route_matrix_unacceptable")
                    continue
                accepted_candidate["_exactRouteRepairScopeCertificate"] = (
                    self._repair_scope_certificate_from_probe(probe)
                )
                ranked.append(
                    (
                        (
                            float(matrix_score.generalized_cost_delta or 0.0),
                            int(matrix_score.estimated_duration_minutes),
                            candidate_id,
                        ),
                        accepted_candidate,
                    )
                )
            else:
                self._record_repair_rejection(repair_ledger, "route_quality_failed")
        return min(ranked, key=lambda item: item[0])[1] if ranked else None

    def _repair_schedule_window(
        self,
        snapshot: dict[str, Any],
        error: Exception,
        candidates: list[dict[str, Any]],
        *,
        city: str,
        transport_mode: str,
        preview_id: str,
        call_metrics: dict[str, int],
        repair_ledger: dict[str, Any],
        allow_nearby_search: bool,
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        message = str(error)
        prefix = "schedule_window_latest_start_exceeded:"
        if prefix not in message:
            return None
        target_segment_id = message.split(prefix, 1)[1].split()[0].strip()
        segments = self._snapshot_segments(snapshot)
        target = next(
            (item for item in segments if item["id"] == target_segment_id),
            None,
        )
        if target is None:
            return None
        target_day = int(target.get("dayNumber") or 0)
        repair_targets = [
            item
            for item in segments
            if int(item.get("dayNumber") or 0) == target_day
            and (
                item["id"] == target_segment_id
                or item.get("kind") == "meal"
                and (self._minutes(item["raw"].get("startTime")) or 0)
                <= (self._minutes(target["raw"].get("startTime")) or 0)
            )
        ]
        repair_targets.sort(key=lambda item: 0 if item.get("kind") == "meal" else 1)
        attempted = 0
        for repair_target in repair_targets:
            if not self._segment_replacement_allowed(repair_target):
                continue
            current_scope = self._route_repair_scope_certificate(
                snapshot,
                segment=repair_target,
                candidate=self._segment_poi_payload(repair_target),
                transport_mode=transport_mode,
            )
            if current_scope is None:
                self._record_repair_rejection(
                    repair_ledger,
                    "candidate_route_scope_missing",
                    self._segment_poi_payload(repair_target),
                )
                continue
            baseline_result = self._evaluate_snapshot(
                copy.deepcopy(snapshot),
                city=city,
                transport_mode=transport_mode,
                preview_id=f"{preview_id}_schedule_baseline",
                call_metrics=call_metrics,
                repair_scope_certificate=current_scope,
            )
            baseline_legs = self._replacement_route_legs(
                baseline_result,
                snapshot=snapshot,
                segment_id=str(repair_target.get("id") or ""),
            )
            if baseline_legs is None:
                self._record_repair_rejection(repair_ledger, "provider_route_baseline_missing")
                continue
            if allow_nearby_search:
                nearby_candidate = self._find_nearby_replacement(
                    snapshot,
                    repair_target,
                    city=city,
                    transport_mode=transport_mode,
                    preview_id=f"{preview_id}_schedule_nearby",
                    call_metrics=call_metrics,
                    repair_ledger=repair_ledger,
                )
                if nearby_candidate is not None:
                    trial = copy.deepcopy(snapshot)
                    self._replace_segment_candidate(
                        trial,
                        repair_target["id"],
                        nearby_candidate,
                    )
                    route_result = self._evaluate_snapshot(
                        trial,
                        city=city,
                        transport_mode=transport_mode,
                        preview_id=f"{preview_id}_schedule_nearby_verified",
                        call_metrics=call_metrics,
                        repair_scope_certificate=(
                            copy.deepcopy(
                                nearby_candidate.get("_exactRouteRepairScopeCertificate")
                            )
                            if isinstance(
                                nearby_candidate.get("_exactRouteRepairScopeCertificate"),
                                dict,
                            )
                            else None
                        ),
                    )
                    if route_result.get("status") == "passed" and self._project_provider_schedule(
                        trial,
                        list(route_result.get("evidence") or []),
                        mutate=True,
                    ):
                        snapshot.clear()
                        snapshot.update(trial)
                        return route_result, {
                            "segmentId": repair_target["id"],
                            "candidateId": str(
                                nearby_candidate.get("amapId") or nearby_candidate.get("id") or ""
                            ),
                            "candidateName": str(nearby_candidate.get("name") or ""),
                            "briefId": str(
                                nearby_candidate.get("briefId") or repair_target.get("briefId") or ""
                            ),
                            "poolId": str(
                                nearby_candidate.get("poolId") or repair_target.get("poolId") or ""
                            ),
                            "planningSlotId": str(
                                nearby_candidate.get("planningSlotId")
                                or repair_target.get("planningSlotId")
                                or ""
                            ),
                            "source": "nearby_adjacent_anchor",
                            "reason": "schedule_window_latest_start_repair",
                            "attempt": 1,
                        }
                    self._record_repair_rejection(
                        repair_ledger,
                        "nearby_schedule_projection_failed",
                    )
            lineage = self._segment_lineage(repair_target)
            current_id = self._canonical_physical_id(
                self._segment_poi_payload(repair_target)
            )
            ranked_candidates: list[tuple[float, dict[str, Any]]] = []
            for candidate in candidates:
                candidate_id = self._canonical_physical_id(candidate)
                if (
                    not candidate_id
                    or candidate_id == current_id
                    or not self._candidate_matches_lineage(candidate, lineage)
                    or not self._candidate_grounded(candidate, city=city)
                ):
                    continue
                score = self._geometry_score(
                    snapshot,
                    repair_target,
                    candidate,
                    transport_mode,
                )
                ranked_candidates.append((float(score.added_distance_km) if score is not None else math.inf, candidate))
            for _score, candidate in sorted(
                ranked_candidates,
                key=lambda item: item[0],
            )[:2]:
                if attempted >= 2:
                    break
                probe = self._reserve_replacement_candidate(
                    repair_ledger,
                    snapshot=snapshot,
                    segment_id=str(repair_target.get("id") or ""),
                    candidate=candidate,
                    city=city,
                    transport_mode=transport_mode,
                )
                if probe is None:
                    continue
                attempted += 1
                repair_ledger["routePairEvaluationCount"] += len(
                    self._touched_route_pairs(snapshot, repair_target["id"])
                )
                trial = copy.deepcopy(snapshot)
                self._replace_segment_candidate(
                    trial,
                    repair_target["id"],
                    candidate,
                )
                route_result = self._evaluate_snapshot(
                    trial,
                    city=city,
                    transport_mode=transport_mode,
                    preview_id=f"{preview_id}_schedule_repair_{attempted}",
                    call_metrics=call_metrics,
                    repair_scope_certificate=self._repair_scope_certificate_from_probe(probe),
                )
                if not self._replacement_route_matrix_complete(
                    route_result,
                    snapshot=trial,
                    segment_id=str(repair_target.get("id") or ""),
                ):
                    self._record_repair_rejection(repair_ledger, "route_quality_failed")
                    continue
                candidate_legs = self._replacement_route_legs(
                    route_result,
                    snapshot=trial,
                    segment_id=str(repair_target.get("id") or ""),
                )
                matrix_score = self._replacement_matrix_score(
                    baseline_legs,
                    candidate_legs,
                    segment=repair_target,
                    candidate=candidate,
                    trial_snapshot=trial,
                    route_evidence=list(route_result.get("evidence") or []),
                )
                if matrix_score is None:
                    self._record_repair_rejection(repair_ledger, "provider_route_matrix_missing")
                    continue
                accepted_candidate = self._candidate_with_matrix_proof(
                    candidate,
                    baseline_legs=baseline_legs,
                    candidate_legs=candidate_legs,
                    matrix_score=matrix_score,
                    decision_policy=self._provider_decision_policy(trial, repair_target, candidate),
                )
                if accepted_candidate is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                    )
                    continue
                scoped_matrix_proof = self._bind_matrix_proof_to_repair_scope(
                    accepted_candidate["_routeReplacementMatrixProof"],
                    probe,
                )
                if scoped_matrix_proof is None:
                    self._record_repair_rejection(
                        repair_ledger,
                        "provider_route_matrix_missing",
                        candidate,
                    )
                    continue
                accepted_candidate["_routeReplacementMatrixProof"] = scoped_matrix_proof
                self._record_completed_candidate_matrix(
                    repair_ledger,
                    probe=probe,
                    candidate=candidate,
                    matrix_proof=accepted_candidate["_routeReplacementMatrixProof"],
                )
                if matrix_score.detour_level == "unacceptable":
                    self._record_repair_rejection(repair_ledger, "provider_route_matrix_unacceptable")
                    continue
                self._store_route_replacement_proof(
                    trial,
                    str(repair_target.get("id") or ""),
                    accepted_candidate["_routeReplacementMatrixProof"],
                )
                route_result = self._evaluate_snapshot(
                    trial,
                    city=city,
                    transport_mode=transport_mode,
                    preview_id=f"{preview_id}_schedule_repair_verified_{attempted}",
                    call_metrics=call_metrics,
                    repair_scope_certificate=self._repair_scope_certificate_from_probe(probe),
                )
                if route_result.get("status") != "passed" or not self._project_provider_schedule(
                    trial,
                    list(route_result.get("evidence") or []),
                    mutate=True,
                ):
                    self._record_repair_rejection(repair_ledger, "replacement_proof_recheck_failed")
                    continue
                snapshot.clear()
                snapshot.update(trial)
                candidate = accepted_candidate
                candidate_id = str(candidate.get("amapId") or candidate.get("id") or "")
                return route_result, {
                    "segmentId": repair_target["id"],
                    "candidateId": candidate_id,
                    "candidateName": str(candidate.get("name") or ""),
                    "briefId": str(candidate.get("briefId") or repair_target.get("briefId") or ""),
                    "poolId": str(candidate.get("poolId") or repair_target.get("poolId") or ""),
                    "planningSlotId": str(candidate.get("planningSlotId") or repair_target.get("planningSlotId") or ""),
                    "source": str(candidate.get("candidateSource") or "portfolio_candidate"),
                    "reason": "schedule_window_latest_start_repair",
                    "attempt": attempted,
                }
            if attempted >= 2:
                break
        if attempted < 2:
            # Reordering two logical occurrences changes both anchors' adjacent
            # pair sets.  One repair-exact-slot certificate cannot authorize
            # that multi-scope closure, so do not mutate the snapshot or fall
            # through to an unscoped Provider recheck.  A future multi-scope
            # contract may re-enable this branch with one exact lease per slot.
            self._record_repair_rejection(
                repair_ledger,
                "schedule_reorder_exact_scope_unavailable",
            )
        return None

    def _move_before_previous_movable(
        self,
        snapshot: dict[str, Any],
        target_segment_id: str,
    ) -> bool:
        for day in snapshot.get("days") or []:
            segments = day.get("segments") if isinstance(day, dict) else None
            if not isinstance(segments, list):
                continue
            target_index = next(
                (
                    index
                    for index, item in enumerate(segments)
                    if isinstance(item, dict) and str(item.get("id") or "") == target_segment_id
                ),
                None,
            )
            if target_index is None or target_index < 1:
                continue
            target = segments[target_index]
            previous = segments[target_index - 1]
            if not isinstance(target, dict) or not isinstance(previous, dict):
                return False
            if not self._segment_replacement_allowed(target) or not self._segment_replacement_allowed(previous):
                return False
            previous_times = (previous.get("startTime"), previous.get("endTime"))
            target_times = (target.get("startTime"), target.get("endTime"))
            target["startTime"], target["endTime"] = previous_times
            previous["startTime"], previous["endTime"] = target_times
            segments[target_index - 1], segments[target_index] = target, previous
            self._invalidate_segment_route_evidence(snapshot, target_segment_id)
            self._invalidate_segment_route_evidence(
                snapshot,
                str(previous.get("id") or ""),
            )
            return True
        return False

    @classmethod
    def _segment_user_locked(cls, segment: dict[str, Any]) -> bool:
        raw = segment.get("raw") if isinstance(segment.get("raw"), dict) else segment
        semantic = cls._segment_semantic(segment)
        duration = semantic.get("duration") if isinstance(semantic.get("duration"), dict) else {}
        schedule = semantic.get("schedule") if isinstance(semantic.get("schedule"), dict) else {}
        return bool(
            raw.get("userLocked")
            or semantic.get("userLocked")
            or duration.get("userLocked")
            or schedule.get("userLocked")
        )

    @classmethod
    def _segment_replacement_allowed(cls, segment: dict[str, Any]) -> bool:
        if cls._segment_user_locked(segment):
            return False
        raw = segment.get("raw") if isinstance(segment.get("raw"), dict) else segment
        semantic = cls._segment_semantic(segment)
        if semantic.get("required") and not (
            semantic.get("portfolioOptional") or semantic.get("requirementLevel") == "soft_experience"
        ):
            return False
        intent_type = str(semantic.get("intentType") or raw.get("kind") or segment.get("kind") or "").strip()
        return intent_type not in {"night", "night_view"}

    @classmethod
    def _is_interior_provider_insertion(
        cls,
        day_anchors: list[dict[str, Any]],
        index: int,
    ) -> bool:
        """Return whether an anchor is an inserted stop with a real bypass baseline.

        A first or last required stop defines the route endpoint.  Charging its
        only adjacent Provider leg as an *incremental detour* compares the route
        with a non-existent baseline and can reject an otherwise valid day.
        Endpoint feasibility remains protected by the ordinary Provider route
        evidence and schedule/time-window gates.
        """

        return bool(
            0 < index < len(day_anchors) - 1
            and cls._requires_provider_insertion_decision(day_anchors[index])
        )

    @classmethod
    def _nearby_search_category(cls, segment: dict[str, Any]) -> str:
        semantic = cls._segment_semantic(segment)
        raw = segment.get("raw") if isinstance(segment.get("raw"), dict) else segment
        intent_type = str(
            semantic.get("intentType")
            or semantic.get("themeFamily")
            or raw.get("kind")
            or segment.get("kind")
            or ""
        ).strip().lower()
        if intent_type in {"meal", "food", "dining", "local_food"}:
            return "food"
        if intent_type in {"market", "local_market", "street_market"}:
            return "market"
        if intent_type in {"culture", "museum", "art", "heritage"}:
            return "culture"
        if intent_type in {"park", "garden"}:
            return "park"
        if intent_type in {"campus", "university"}:
            return "campus"
        if intent_type in {"night", "night_view", "scenic", "viewpoint"}:
            return "scenic"
        return "experience"

    @classmethod
    def _nearby_search_keyword(cls, segment: dict[str, Any]) -> str:
        """Project a provider-searchable keyword from an abstract planning slot.

        Portfolio slots deliberately carry user intent such as ``当地特色美食``.
        Passing that sentence verbatim to AMap nearby search is overly specific
        and can return an empty set even when suitable POIs surround the route.
        Nearby search already has a strict category/type filter, so use a stable
        category noun for generic slot labels and retain concrete user wording.
        """

        semantic = cls._segment_semantic(segment)
        raw_need = str(semantic.get("rawNeed") or semantic.get("label") or "").strip()
        category = cls._nearby_search_category(segment)
        generic_by_category = {
            "food": "餐厅",
            "market": "市场",
            "culture": "文化场馆",
            "park": "公园",
            "campus": "大学",
            "scenic": "景点",
            "experience": "体验",
        }
        compact = re.sub(r"[\s·，。；、｜|/]+", "", raw_need).lower()
        generic_markers = {
            "当地特色美食",
            "本地特色美食",
            "本地美食",
            "当地美食",
            "特色美食",
            "当地餐饮",
            "当地特色餐饮",
            "市井市场与传统市集",
            "历史街区与胡同漫步",
            "艺术街区与创意园区",
            "城市公园休憩",
            "晚上看城市夜景",
            "城市夜景",
        }
        if not compact or compact in {re.sub(r"[\s·，。；、｜|/]+", "", item).lower() for item in generic_markers}:
            return generic_by_category[category]
        return raw_need

    def _nearby_candidate(self, poi: Any, segment: dict[str, Any]) -> dict[str, Any]:
        semantic = self._segment_semantic(segment)
        return {
            "amapId": str(getattr(poi, "id", "") or ""),
            "id": str(getattr(poi, "id", "") or ""),
            "name": str(getattr(poi, "name", "") or ""),
            "city": str(getattr(poi, "city", "") or ""),
            "type": str(getattr(poi, "type", "") or ""),
            "providerType": str(getattr(poi, "type", "") or ""),
            "category": self._nearby_search_category(segment),
            "longitude": getattr(poi, "longitude", None),
            "latitude": getattr(poi, "latitude", None),
            "source": str(getattr(poi, "source", "") or ""),
            "sourceNote": str(getattr(poi, "source_note", "") or ""),
            "district": str(getattr(poi, "district", "") or ""),
            "address": str(getattr(poi, "address", "") or ""),
            "photos": [
                item.model_dump(by_alias=True) if hasattr(item, "model_dump") else dict(item)
                for item in (getattr(poi, "photos", []) or [])
            ],
            "confidence": float(getattr(poi, "confidence", 0.0) or 0.0),
            **(
                {"parentPoiId": str(getattr(poi, "parent_poi_id", "") or "")}
                if str(getattr(poi, "parent_poi_id", "") or "")
                else {}
            ),
            "semanticPassed": True,
            "briefId": semantic.get("creativeBriefId"),
            "poolId": semantic.get("poolId"),
            "planningSlotId": semantic.get("planningSlotId"),
            "dayNumber": int(segment.get("dayNumber") or 0),
            "sourceGoalId": semantic.get("sourceGoalId"),
            "candidateSource": "nearby_adjacent_anchor",
        }

    def _admit_replacement_candidate(
        self,
        candidate: dict[str, Any],
        *,
        segment: dict[str, Any],
        city: str,
    ) -> tuple[Optional[dict[str, Any]], str]:
        """Re-evaluate a replacement candidate in its consuming source slot.

        Route-conditioned discovery may share only factual AMap evidence.  The
        source segment's server-produced admission input is the authority for
        this consuming brief/pool/slot/day; a prior report is never reused.
        """

        semantic = self._segment_semantic(segment)
        consumer_input = semantic.get("consumerAdmissionInput")
        if not isinstance(consumer_input, dict):
            return (None, "consumer_admission_context_missing")
        if not self._consumer_admission_input_matches_segment(
            consumer_input,
            segment=segment,
            city=city,
        ):
            return (None, "consumer_admission_scope_mismatch")
        admitted_candidate = copy.deepcopy(candidate)
        try:
            report = self.consumer_admission_service.evaluate(
                admitted_candidate,
                copy.deepcopy(consumer_input),
            )
        except Exception:
            # Candidate discovery is read-only.  A malformed admission input or
            # evaluator defect must not fall through into a Provider probe.
            return (None, "consumer_admission_evaluation_failed")
        if not isinstance(report, dict) or report.get("scoreEligible") is not True:
            return (None, "consumer_admission_rejected")
        admitted_candidate["consumerAdmissionInput"] = copy.deepcopy(consumer_input)
        admitted_candidate["consumerAdmissionReport"] = copy.deepcopy(report)
        return (admitted_candidate, "")

    @classmethod
    def _consumer_admission_input_matches_segment(
        cls,
        consumer_input: dict[str, Any],
        *,
        segment: dict[str, Any],
        city: str,
    ) -> bool:
        """Reject stale or cross-slot inputs before any candidate route call."""

        try:
            day_number = int(consumer_input.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            str(consumer_input.get("briefId") or "") == str(segment.get("briefId") or "")
            and str(consumer_input.get("poolId") or "") == str(segment.get("poolId") or "")
            and str(consumer_input.get("planningSlotId") or consumer_input.get("slotId") or "")
            == str(segment.get("planningSlotId") or "")
            and day_number == int(segment.get("dayNumber") or 0)
            and cls._canonical_city(consumer_input.get("city")) == cls._canonical_city(city)
        )

    def _geometry_score(
        self,
        snapshot: dict[str, Any],
        segment: dict[str, Any],
        candidate: dict[str, Any],
        transport_mode: str,
    ):
        previous, next_anchor = self._adjacent_anchor_items(snapshot, segment["id"], include_poi_candidate=candidate)
        return self.route_insertion_scorer.score(
            previous["poi"] if previous else None,
            self._poi_from_candidate(candidate),
            next_anchor["poi"] if next_anchor else None,
            transport_mode=transport_mode,
        )

    def _snapshot_segments(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 1)
            raw_segments = sorted(day.get("segments") or [], key=lambda item: self._minutes(item.get("startTime")) or 0)
            for index, raw in enumerate(raw_segments, start=1):
                if not isinstance(raw, dict):
                    continue
                semantic = raw.get("semanticMetadata") if isinstance(raw.get("semanticMetadata"), dict) else {}
                poi_payload = raw.get("poi") if isinstance(raw.get("poi"), dict) else {}
                poi = self._poi_from_payload(poi_payload)
                segment = ItinerarySegment(
                    id=str(raw.get("id") or f"portfolio_segment_{day_number}_{index}"),
                    day_id=f"day_{day_number}",
                    segment_order=index,
                    kind=str(raw.get("kind") or "visit"),
                    start_time=str(raw.get("startTime") or "09:00"),
                    end_time=str(raw.get("endTime") or "10:00"),
                    poi_id=str(poi.id if poi else ""),
                    transport_mode=str(
                        raw.get("transportMode") or snapshot.get("portfolioTransportPreference") or "transit"
                    ),
                    estimated_cost=float(raw.get("estimatedCost") or 0),
                    notes=str(raw.get("notes") or ""),
                    semantic_metadata=semantic,
                )
                result.append(
                    {
                        "id": str(raw.get("id") or segment.id),
                        "dayNumber": day_number,
                        "kind": segment.kind,
                        "raw": raw,
                        "semantic": semantic,
                        "poi": poi,
                        "segment": segment,
                        "route_anchor": bool(
                            poi is not None
                            and str(raw.get("kind") or "") not in {"pending", "placeholder"}
                            and (semantic.get("routeAnchor") or raw.get("startTime") or raw.get("timeWindow"))
                        ),
                        "briefId": str(semantic.get("creativeBriefId") or ""),
                        "poolId": str(semantic.get("poolId") or ""),
                        "planningSlotId": str(semantic.get("planningSlotId") or semantic.get("slotId") or ""),
                    }
                )
        return result

    def _sensitive_segments(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in self._snapshot_segments(snapshot)
            if item["kind"] == "meal"
            and (
                bool((item.get("semantic") or {}).get("routePreference", {}).get("preferNearAdjacentAnchors"))
                or item["route_anchor"]
            )
        ]

    def _adjacent_anchor_items(
        self,
        snapshot: dict[str, Any],
        segment_id: str,
        *,
        include_poi_candidate: Optional[dict[str, Any]] = None,
    ):
        items = [item for item in self._snapshot_segments(snapshot) if item["route_anchor"]]
        target_index = next((index for index, item in enumerate(items) if item["id"] == segment_id), None)
        if target_index is None:
            return []
        previous = (
            items[target_index - 1]
            if target_index > 0 and items[target_index - 1]["dayNumber"] == items[target_index]["dayNumber"]
            else None
        )
        next_anchor = (
            items[target_index + 1]
            if target_index + 1 < len(items)
            and items[target_index + 1]["dayNumber"] == items[target_index]["dayNumber"]
            else None
        )
        if include_poi_candidate is not None:
            target = items[target_index]
            target["poi"] = self._poi_from_candidate(include_poi_candidate)
        return (previous, next_anchor)

    def _replace_segment_candidate(self, snapshot: dict[str, Any], segment_id: str, candidate: dict[str, Any]) -> None:
        self._invalidate_segment_route_evidence(snapshot, segment_id)
        for day in snapshot.get("days") or []:
            for segment in day.get("segments") or []:
                if isinstance(segment, dict) and str(segment.get("id") or "") == segment_id:
                    segment["poi"] = self._poi_payload(candidate)
                    semantic = segment.setdefault("semanticMetadata", {})
                    semantic.update(
                        {
                            "groundingStatus": "selected",
                            "routeCandidateSource": str(candidate.get("candidateSource") or "portfolio_candidate"),
                            "candidateFingerprint": self._candidate_fingerprint_from_dict(candidate),
                        }
                    )
                    replacement_matrix_proof = candidate.get("_routeReplacementMatrixProof")
                    if isinstance(replacement_matrix_proof, dict):
                        semantic["routeReplacementMatrixProof"] = copy.deepcopy(replacement_matrix_proof)
                    else:
                        semantic.pop("routeReplacementMatrixProof", None)
                    if isinstance(candidate.get("routeContract"), dict):
                        semantic["routeContract"] = copy.deepcopy(candidate["routeContract"])
                    for policy_key in (
                        "detourTolerance",
                        "mobilityProfile",
                        "riskPenaltyMinutes",
                    ):
                        if policy_key in candidate:
                            semantic[policy_key] = copy.deepcopy(candidate[policy_key])
                    replacement_report = candidate.get("consumerAdmissionReport")
                    if isinstance(replacement_report, dict):
                        semantic["consumerAdmissionReport"] = copy.deepcopy(replacement_report)
                    else:
                        semantic.pop("consumerAdmissionReport", None)
                    replacement_input = candidate.get("consumerAdmissionInput")
                    if isinstance(replacement_input, dict):
                        semantic["consumerAdmissionInput"] = copy.deepcopy(replacement_input)
                    else:
                        semantic.pop("consumerAdmissionInput", None)
                    if isinstance(candidate.get("discoveryProvenance"), dict):
                        semantic["discoveryProvenance"] = self._safe_discovery_provenance(
                            candidate["discoveryProvenance"]
                        )

    @staticmethod
    def _store_route_replacement_proof(
        snapshot: dict[str, Any],
        segment_id: str,
        proof: dict[str, Any],
    ) -> None:
        for day in snapshot.get("days") or []:
            for segment in day.get("segments") or []:
                if isinstance(segment, dict) and str(segment.get("id") or "") == segment_id:
                    semantic = segment.setdefault("semanticMetadata", {})
                    semantic["routeReplacementMatrixProof"] = copy.deepcopy(proof)
                    return

    def _store_snapshot_route_evidence(
        self,
        snapshot: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> None:
        canonical = [copy.deepcopy(item) for item in evidence if isinstance(item, dict)]
        snapshot["routeOptions"] = copy.deepcopy(canonical)
        snapshot["portfolioRouteEvidence"] = copy.deepcopy(canonical)
        snapshot["routeEvidence"] = copy.deepcopy(canonical)
        self._day_route_evidence(snapshot, canonical)

    @staticmethod
    def _invalidate_segment_route_evidence(
        snapshot: dict[str, Any],
        segment_id: str,
    ) -> None:
        def keep(item: Any) -> bool:
            return not (
                isinstance(item, dict)
                and segment_id
                in {
                    str(item.get("fromSegmentId") or ""),
                    str(item.get("toSegmentId") or ""),
                }
            )

        for key in (
            "routeOptions",
            "portfolioRouteEvidence",
            "routeEvidence",
            "portfolioThemeWalkingEvidence",
        ):
            raw = snapshot.get(key)
            if isinstance(raw, list):
                snapshot[key] = [copy.deepcopy(item) for item in raw if keep(item)]
        for day in snapshot.get("days") or []:
            if isinstance(day, dict) and isinstance(day.get("routeEvidence"), list):
                day["routeEvidence"] = [copy.deepcopy(item) for item in day["routeEvidence"] if keep(item)]

    @staticmethod
    def _actual_route_rank(
        result: dict[str, Any],
        segment_id: str,
        candidate_id: str,
    ) -> tuple[int, int, str]:
        adjacent = [
            item
            for item in result.get("evidence") or []
            if isinstance(item, dict)
            and segment_id
            in {
                str(item.get("fromSegmentId") or ""),
                str(item.get("toSegmentId") or ""),
            }
        ]
        return (
            sum(int(item.get("distanceMeters") or 0) for item in adjacent),
            sum(int(item.get("durationSeconds") or 0) for item in adjacent),
            candidate_id,
        )

    def _day_route_evidence(self, snapshot: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {
            str(item.get("id") or ""): int(day.get("dayNumber") or 1)
            for day in snapshot.get("days") or []
            for item in (day.get("segments") or [])
            if isinstance(item, dict)
        }
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in evidence:
            copied = dict(item)
            copied["dayNumber"] = by_id.get(
                str(item.get("fromSegmentId") or ""), by_id.get(str(item.get("toSegmentId") or ""), 1)
            )
            grouped.setdefault(int(copied["dayNumber"]), []).append(copied)
        for day in snapshot.get("days") or []:
            if isinstance(day, dict):
                day["routeEvidence"] = grouped.get(int(day.get("dayNumber") or 1), [])
        return evidence

    def _project_schedule(self, snapshot: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
        ItineraryScheduleService.project_snapshot_schedule(snapshot, evidence)

    @staticmethod
    def _sanitized_error(error: Exception) -> str:
        """Keep a short diagnostic without exposing credentials or payloads."""
        message = str(error).replace("\n", " ").replace("\r", " ").strip()
        for marker in ("authorization", "token", "api_key", "apikey", "cookie", "secret"):
            if marker in message.casefold():
                return "redacted_provider_error"
        return message[:280] or type(error).__name__

    @staticmethod
    def _route_option_from_evidence(evidence: dict[str, Any]) -> RouteOption:
        """Rehydrate only canonical normalized evidence for preflight reuse."""
        queried_at_raw = str(evidence.get("queriedAt") or "").strip()
        if not queried_at_raw:
            raise ValueError("route_evidence_queried_at_missing")
        try:
            queried_at = datetime.fromisoformat(queried_at_raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("route_evidence_queried_at_invalid") from error
        return RouteOption(
            id=str(evidence.get("id") or ""),
            plan_id=str(evidence.get("planId") or "portfolio_preview"),
            from_segment_id=str(evidence.get("fromSegmentId") or "") or None,
            to_segment_id=str(evidence.get("toSegmentId") or "") or None,
            from_poi_id=str(evidence.get("fromPoiId") or ""),
            to_poi_id=str(evidence.get("toPoiId") or ""),
            provider=str(evidence.get("provider") or evidence.get("source") or "amap-webservice"),
            mode=str(evidence.get("mode") or evidence.get("transportMode") or "transit"),
            label=str(evidence.get("label") or ""),
            is_selected=bool(evidence.get("isSelected", True)),
            sort_order=int(evidence.get("sortOrder") or 1),
            distance_meters=int(evidence.get("distanceMeters") or 0),
            duration_seconds=int(evidence.get("durationSeconds") or 0),
            cost_amount=float(evidence.get("costAmount") or 0),
            cost_currency=str(evidence.get("costCurrency") or "CNY"),
            polyline=copy.deepcopy(evidence.get("polyline") or []),
            steps=copy.deepcopy(evidence.get("steps") or []),
            provider_payload=copy.deepcopy(evidence.get("providerPayload") or {}),
            error=copy.deepcopy(evidence.get("error")) if isinstance(evidence.get("error"), dict) else None,
            queried_at=queried_at,
        )

    def _failure_ledger(
        self,
        pairs: list[tuple[dict[str, Any], dict[str, Any]]],
        cached_routes: dict[tuple[str, str], dict[str, Any]],
        call_metrics: dict[str, int],
        error: Exception,
    ) -> dict[str, Any]:
        return {
            "expectedLegCount": len(pairs),
            "submittedLegCount": int(call_metrics.get("requestedLegCount") or 0),
            "cachedLegCount": len(cached_routes),
            "requestedLegCount": int(call_metrics.get("requestedLegCount") or 0),
            "completedLegCount": len(cached_routes),
            "verifiedLegCount": len(cached_routes),
            "failedLegCount": max(0, len(pairs) - len(cached_routes)),
            "providerCallCount": int(call_metrics.get("providerCallCount") or 0),
            "providerCacheHitCount": int(call_metrics.get("providerCacheHitCount") or 0),
            "cancelledLegCount": 0,
            "retainedEvidenceCount": len(cached_routes),
            "workerFailureClass": type(error).__name__,
            "sanitizedWorkerFailureMessage": self._sanitized_error(error),
            "workerTimedOut": isinstance(error, TimeoutError),
            "workerTimeoutMs": None,
        }

    @staticmethod
    def _segment_semantic(segment: dict[str, Any]) -> dict[str, Any]:
        if isinstance(segment.get("semantic"), dict):
            return segment["semantic"]
        if isinstance(segment.get("semanticMetadata"), dict):
            return segment["semanticMetadata"]
        raw = segment.get("raw") if isinstance(segment.get("raw"), dict) else {}
        return raw.get("semanticMetadata") if isinstance(raw.get("semanticMetadata"), dict) else {}

    @classmethod
    def _segment_lineage(cls, segment: dict[str, Any]) -> dict[str, Any]:
        semantic = cls._segment_semantic(segment)
        return {
            "briefId": str(semantic.get("creativeBriefId") or ""),
            "poolId": str(semantic.get("poolId") or ""),
            "planningSlotId": str(semantic.get("planningSlotId") or semantic.get("slotId") or ""),
            "dayNumber": int(segment.get("dayNumber") or 0),
            "sourceGoalId": str(semantic.get("sourceGoalId") or ""),
        }

    @staticmethod
    def _candidate_matches_lineage(candidate: dict[str, Any], lineage: dict[str, Any]) -> bool:
        # A route-sensitive repair is safe only when all persisted
        # identities are present and equal. Missing lineage is not a wildcard:
        # it would allow a candidate from another brief to cross the writer
        # boundary.
        return bool(
            lineage["briefId"]
            and lineage["poolId"]
            and lineage["planningSlotId"]
            and lineage["dayNumber"] > 0
            and lineage["sourceGoalId"]
        ) and all(
            str(candidate.get(key) or "") == str(lineage[key])
            for key in ("briefId", "poolId", "planningSlotId", "dayNumber", "sourceGoalId")
        )

    @staticmethod
    def _canonical_city(value: Any) -> str:
        """Compare AMap municipality names with the session's canonical city."""
        return str(value or "").strip().removesuffix("市")

    @classmethod
    def _candidate_grounded(cls, candidate: dict[str, Any], *, city: str) -> bool:
        try:
            longitude = float(candidate.get("longitude"))
            latitude = float(candidate.get("latitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            (candidate.get("amapId") or candidate.get("id"))
            and math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and (candidate.get("providerType") or candidate.get("type"))
            and str(candidate.get("source") or "") == AMAP_PLACE_SOURCE
            and cls._canonical_city(candidate.get("city")) == cls._canonical_city(city)
            and candidate.get("semanticPassed", True) is True
        )

    @staticmethod
    def _safe_discovery_provenance(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        safe: dict[str, Any] = {}
        for key in (
            "triggerReason",
            "webProvider",
            "webUrlHash",
            "webSourceName",
            "webCredibilityRank",
            "webFreshness",
            "webTitle",
            "entitySeed",
            "entityAliases",
            "mapProvider",
            "amapId",
            "webQueryFingerprint",
            "amapQueryFingerprint",
        ):
            if key in value:
                safe[key] = copy.deepcopy(value[key])
        for raw_key, fingerprint_key in (
            ("webQuery", "webQueryFingerprint"),
            ("amapQuery", "amapQueryFingerprint"),
        ):
            raw = str(value.get(raw_key) or "").strip()
            if raw and fingerprint_key not in safe:
                safe[fingerprint_key] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return safe

    @staticmethod
    def _poi_from_payload(payload: dict[str, Any]) -> Optional[POI]:
        if (
            not payload
            or not payload.get("amapId")
            or payload.get("longitude") is None
            or payload.get("latitude") is None
        ):
            return None
        exact_amap_id = PoiPhysicalIdentityService.normalized_amap_id(payload)
        canonical_amap_id = PoiPhysicalIdentityService.canonical_amap_id(payload)
        route_amap_id = canonical_amap_id or exact_amap_id
        return POI(
            id=route_amap_id,
            amap_id=route_amap_id,
            name=str(payload.get("name") or ""),
            city=str(payload.get("city") or ""),
            category=str(payload.get("category") or ""),
            type=str(payload.get("type") or payload.get("providerType") or ""),
            latitude=float(payload.get("latitude")),
            longitude=float(payload.get("longitude")),
            source=str(payload.get("source") or "amap-place-search"),
            confidence=float(payload.get("confidence") or 0.0),
            district=str(payload.get("district") or ""),
            address=str(payload.get("address") or ""),
            source_note=str(payload.get("sourceNote") or ""),
            photos=list(payload.get("photos") or []),
        )

    @staticmethod
    def _poi_from_candidate(candidate: dict[str, Any]) -> POI:
        exact_amap_id = PoiPhysicalIdentityService.normalized_amap_id(candidate)
        canonical_amap_id = PoiPhysicalIdentityService.canonical_amap_id(candidate)
        route_amap_id = canonical_amap_id or exact_amap_id
        return POI(
            id=route_amap_id,
            amap_id=route_amap_id,
            name=str(candidate.get("name") or ""),
            city=str(candidate.get("city") or ""),
            category=str(candidate.get("category") or "food"),
            type=str(candidate.get("type") or candidate.get("providerType") or ""),
            latitude=float(candidate.get("latitude")),
            longitude=float(candidate.get("longitude")),
            source=str(candidate.get("source") or "amap-place-search"),
            confidence=float(candidate.get("confidence") or 0.0),
            district=str(candidate.get("district") or ""),
            address=str(candidate.get("address") or ""),
            source_note=str(candidate.get("sourceNote") or ""),
            photos=list(candidate.get("photos") or []),
        )

    @staticmethod
    def _poi_payload(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(candidate.get("id") or candidate.get("amapId") or ""),
            "amapId": str(candidate.get("amapId") or candidate.get("id") or ""),
            "name": str(candidate.get("name") or ""),
            "city": str(candidate.get("city") or ""),
            "category": str(candidate.get("category") or "food"),
            "type": str(candidate.get("type") or candidate.get("providerType") or ""),
            "providerType": str(candidate.get("providerType") or candidate.get("type") or ""),
            "latitude": candidate.get("latitude"),
            "longitude": candidate.get("longitude"),
            "source": str(candidate.get("source") or "amap-place-search"),
            "sourceNote": str(candidate.get("sourceNote") or "portfolio_route_candidate"),
            "district": str(candidate.get("district") or ""),
            "address": str(candidate.get("address") or ""),
            "confidence": float(candidate.get("confidence") or 0.0),
            "photos": list(candidate.get("photos") or []),
            **(
                {"parentPoiId": str(candidate.get("parentPoiId") or "")}
                if str(candidate.get("parentPoiId") or "")
                else {}
            ),
        }

    @staticmethod
    def _has_issue_for_segment(issues: list[dict[str, Any]], segment_id: str) -> bool:
        return any(
            segment_id
            in {
                str(item.get("segmentId") or ""),
                str(item.get("mealSegmentId") or ""),
                str(item.get("fromSegmentId") or ""),
                str(item.get("toSegmentId") or ""),
            }
            for item in issues
        )

    @staticmethod
    def _candidate_fingerprint(snapshot: dict[str, Any]) -> str:
        identities = [
            str((segment.get("poi") or {}).get("amapId") or "")
            for day in snapshot.get("days") or []
            for segment in (day.get("segments") or [])
            if isinstance(segment, dict)
        ]
        return hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _candidate_fingerprint_from_dict(candidate: dict[str, Any]) -> str:
        payload = {key: candidate.get(key) for key in ("amapId", "briefId", "poolId", "planningSlotId")}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _minutes(value: Any) -> Optional[int]:
        try:
            hour, minute = (int(item) for item in str(value).split(":", 1))
            return hour * 60 + minute
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_minutes(value: int) -> str:
        value = max(0, int(value))
        return f"{value // 60:02d}:{value % 60:02d}"

"""Deterministic verifier for proposal snapshots before they become choices."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from typing import Any

from src.services.creative_planning_models import (
    ConstraintLedger,
    CreativeBrief,
    canonical_fingerprint,
)
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.poi_trust_policy import PoiTrustPolicy
from src.services.itinerary_schedule_service import ItineraryScheduleService
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import ROUTE_CACHE_TTL_SECONDS


class PlanProposalVerifier:
    _GROUNDED_STATUSES = {"selected", "confirmed", "grounded", "agent_selected_candidate"}

    def __init__(self, *, experience_grounding_v2_mode: str = "off") -> None:
        self.semantic_policy = IntentCandidateSemanticPolicy()
        self.experience_grounding_v2_mode = (
            experience_grounding_v2_mode if experience_grounding_v2_mode in {"off", "shadow", "enforce"} else "off"
        )

    def verify(
        self, snapshot: dict[str, Any], ledger: ConstraintLedger, brief: CreativeBrief | None = None
    ) -> dict[str, Any]:
        days = [day for day in snapshot.get("days") or [] if isinstance(day, dict)]
        expected_city = str(snapshot.get("city") or "").strip()
        failures: list[str] = []
        covered_counts = {goal.goal_id: 0 for goal in ledger.hard_goals}
        goals_by_id = {goal.goal_id: goal for goal in ledger.hard_goals}
        covered_identities = {goal.goal_id: set() for goal in ledger.hard_goals}
        identity_goal_ids: dict[str, set[str]] = {}
        route_coverage_failures: list[str] = []
        route_quality_failures: list[str] = []
        route_quality_warnings: list[str] = []
        pacing_violations: list[str] = []
        budget_violations: list[str] = []
        theme_failures: list[str] = []
        theme_warnings: list[str] = []
        temporal_failures: list[dict[str, object]] = []
        admission_materialization = (
            snapshot.get("portfolioAdmissionMaterializationAudit")
            if isinstance(snapshot.get("portfolioAdmissionMaterializationAudit"), dict)
            else {}
        )
        if (
            self.experience_grounding_v2_mode == "enforce"
            and admission_materialization
            and admission_materialization.get("countInvariantPassed") is not True
        ):
            failures.append(
                "portfolio_admission_anchor_count_mismatch:"
                f"{admission_materialization.get('scoreEligibleAnchorCount') or 0}/"
                f"{admission_materialization.get('actualProposalAnchorCount') or 0}"
            )
        segment_days = {
            str(segment.get("id") or ""): int(day.get("dayNumber") or 0)
            for day in days
            for segment in day.get("segments") or []
            if isinstance(segment, dict) and str(segment.get("id") or "")
        }
        normalized_routes = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
        verified_route_pairs = {
            (
                str(route.get("fromSegmentId") or ""),
                str(route.get("toSegmentId") or ""),
            )
            for route in normalized_routes
            if ProposalRouteEvidenceNormalizer.is_verified(route, segment_days)
        }
        route_decision_proof_failures, route_decision_proof_evidence = self._verify_route_decision_proofs(
            snapshot, days
        )
        canonical_identity_required = self.experience_grounding_v2_mode == "enforce"
        occurrence_failures, occurrence_coverage = self._verify_goal_occurrences(
            snapshot,
            days,
            expected_city=expected_city,
            canonical_identity_required=canonical_identity_required,
        )
        binding_failures, binding_evidence = self._verify_required_candidate_bindings(
            snapshot,
            days,
            brief,
            expected_city=expected_city,
            canonical_identity_required=canonical_identity_required,
        )
        capacity_failures, capacity_evidence = self._verify_daily_capacity(snapshot)
        raw_targets = snapshot.get("portfolioDayAnchorTargets")
        day_anchor_targets = (
            {
                int(day): int(target)
                for day, target in raw_targets.items()
                if str(day).isdigit() and isinstance(target, (int, float))
            }
            if isinstance(raw_targets, dict)
            else {}
        )
        raw_capacity = snapshot.get("portfolioDailyCapacityPlan")
        if isinstance(raw_capacity, dict):
            for day, target in sorted(day_anchor_targets.items()):
                capacity_row = capacity_evidence.get(str(day))
                if capacity_row is None:
                    capacity_failures.append(f"daily_capacity_anchor_target_missing:day_{day}")
                    continue
                capacity_target = int(capacity_row.get("targetRouteAnchors") or 0)
                if capacity_target != target:
                    capacity_failures.append(
                        f"daily_capacity_anchor_target_mismatch:day_{day}:{capacity_target}/{target}"
                    )
            capacity_failures = list(dict.fromkeys(capacity_failures))
        day_anchor_actuals: dict[int, int] = {}
        day_anchor_shortfalls: list[str] = []
        expected_route_pairs: set[tuple[str, str]] = set()
        total_cost = 0.0
        for day in days:
            day_number = int(day.get("dayNumber") or 1)
            previous_end = -1
            previous_explicit_day_part_rank = -1
            previous_explicit_day_part = ""
            day_minutes = 0
            anchors: list[str] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                schedule_preference = (
                    semantic.get("schedulePreference")
                    if isinstance(semantic.get("schedulePreference"), dict)
                    else {}
                )
                day_part = str(schedule_preference.get("dayPart") or "").strip().casefold()
                day_part_rank = {
                    "early_morning": 0,
                    "morning": 1,
                    "noon": 2,
                    "afternoon": 3,
                    "evening": 4,
                    "night": 5,
                }.get(day_part)
                if schedule_preference.get("userExplicit") is True and day_part_rank is not None:
                    if day_part_rank < previous_explicit_day_part_rank:
                        failures.append("schedule_semantic_order")
                        temporal_failures.append(
                            {
                                "code": "schedule.semantic_day_part_order",
                                "segmentId": str(segment.get("id") or ""),
                                "intentType": str(semantic.get("intentType") or segment.get("kind") or ""),
                                "previousDayPart": previous_explicit_day_part,
                                "dayPart": day_part,
                                "dayNumber": day_number,
                            }
                        )
                    else:
                        previous_explicit_day_part_rank = day_part_rank
                        previous_explicit_day_part = day_part
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                grounded = (
                    self._has_grounded_identity(
                        poi,
                        city=expected_city,
                        canonical_identity_required=canonical_identity_required,
                    )
                    and semantic.get("groundingStatus") in self._GROUNDED_STATUSES
                )
                goal_id = str(semantic.get("goalId") or "")
                intent_type = str(semantic.get("intentType") or "")
                consumer_scoped_segment = bool(
                    semantic.get("portfolioOptional")
                    or (semantic.get("required") and semantic.get("creativeBriefId"))
                    or (semantic.get("requirementLevel") == "soft_experience" and semantic.get("creativeBriefId"))
                )
                if consumer_scoped_segment:
                    admission_scope = (
                        "optional"
                        if semantic.get("portfolioOptional") or semantic.get("requirementLevel") == "soft_experience"
                        else "required"
                    )
                    admission_report = semantic.get("consumerAdmissionReport")
                    if self.experience_grounding_v2_mode == "enforce":
                        if not isinstance(admission_report, dict):
                            failures.append(
                                f"portfolio_{admission_scope}_consumer_admission_missing:{segment.get('id')}"
                            )
                        elif not (
                            ConsumerCandidateAdmissionService.validate_report(admission_report)
                            and ConsumerCandidateAdmissionService.report_matches_poi(admission_report, poi)
                            and admission_report.get("scoreEligible") is True
                            and admission_report.get("classification")
                            in {"admitted_final_anchor", "admitted_anchor_set_member"}
                            and self._admission_scope_matches(admission_report, semantic, day_number)
                        ):
                            failures.append(
                                f"portfolio_{admission_scope}_consumer_admission_invalid:{segment.get('id')}"
                            )
                    elif semantic.get("portfolioOptional"):
                        semantic_decision = self.semantic_policy.evaluate(
                            intent_type,
                            poi,
                            raw_need=str(semantic.get("rawNeed") or ""),
                            optional_experience_family=str(semantic.get("optionalExperienceFamily") or ""),
                        )
                        if not semantic_decision.passed:
                            failures.append(
                                f"portfolio_optional_semantic_mismatch:{segment.get('id')}:{semantic_decision.reason_code}"
                            )
                if semantic.get("required") and goal_id in goals_by_id and not grounded:
                    failures.append(f"required_goal_ungrounded:{goal_id}")
                if grounded:
                    matching_goal_ids = self._matching_goal_ids(
                        goal_id=goal_id,
                        intent_type=intent_type,
                        goals_by_id=goals_by_id,
                        ledger=ledger,
                    )
                    identity = self._canonical_physical_identity(poi)
                    for matched_goal_id in matching_goal_ids:
                        assigned_goals = identity_goal_ids.setdefault(identity, set())
                        if assigned_goals and matched_goal_id not in assigned_goals:
                            failures.append(f"required_goal_identity_reused:{identity}")
                            continue
                        assigned_goals.add(matched_goal_id)
                        if identity not in covered_identities[matched_goal_id]:
                            covered_identities[matched_goal_id].add(identity)
                            covered_counts[matched_goal_id] += 1
                if semantic.get("routeAnchor"):
                    if not self._has_grounded_identity(
                        poi,
                        city=expected_city,
                        canonical_identity_required=canonical_identity_required,
                    ):
                        failures.append("route_anchor_identity_missing")
                    if semantic.get("groundingStatus") not in self._GROUNDED_STATUSES:
                        failures.append("route_anchor_semantic_evidence_missing")
                    anchors.append(str(segment.get("id") or ""))
                    if grounded:
                        day_anchor_actuals[day_number] = day_anchor_actuals.get(day_number, 0) + 1
                start, end = self._minutes(segment.get("startTime")), self._minutes(segment.get("endTime"))
                temporal_failures.extend(ItineraryScheduleService.temporal_failures(segment))
                if start is None or end is None or end <= start:
                    failures.append("schedule_interval_invalid")
                elif start < previous_end:
                    failures.append("schedule_overlap")
                else:
                    previous_end = end
                    day_minutes += end - start
                total_cost += float(segment.get("estimatedCost") or 0)
            route_pairs = {
                pair
                for pair in verified_route_pairs
                if segment_days.get(pair[0]) == day_number and segment_days.get(pair[1]) == day_number
            }
            for left, right in zip(anchors, anchors[1:]):
                expected_route_pairs.add((left, right))
                if snapshot.get("portfolioRouteVerificationRequired") and (left, right) not in route_pairs:
                    route_coverage_failures.append(f"route_coverage_missing:{left}:{right}")
            max_minutes = {"relaxed": 540, "standard": 660, "intensive": 780}[ledger.pace]
            if day_minutes > max_minutes:
                pacing_violations.append(f"pace_exceeded:day_{day.get('dayNumber')}:{day_minutes}/{max_minutes}")
        if snapshot.get("portfolioRouteVerificationRequired"):
            route_coverage_failures.extend(
                f"route_coverage_unexpected:{left}:{right}"
                for left, right in sorted(verified_route_pairs - expected_route_pairs)
            )
        for day_number, target in sorted(day_anchor_targets.items()):
            actual = day_anchor_actuals.get(day_number, 0)
            if actual != target:
                mismatch = f"day_{day_number}:{actual}/{target}"
                failures.append(f"route_anchor_target_mismatch:{mismatch}")
                if actual < target:
                    day_anchor_shortfalls.append(mismatch)
        omitted = [goal for goal in ledger.hard_goals if covered_counts[goal.goal_id] < goal.required_min]
        failures.extend(
            f"required_goal_count_insufficient:{goal.goal_id}:{covered_counts[goal.goal_id]}/{goal.required_min}"
            for goal in omitted
        )
        failures.extend(
            f"required_goal_omitted:{goal.goal_id}" for goal in sorted(omitted, key=lambda item: item.goal_id)
        )
        budget_limit = self._budget_limit(ledger)
        if budget_limit is not None and total_cost > budget_limit:
            budget_violations.append(f"budget_exceeded:{total_cost:.2f}/{budget_limit:.2f}")
        if brief is not None and brief.optional_experiences:
            expected = {item.family for item in brief.optional_experiences}
            actual = {
                str((segment.get("semanticMetadata") or {}).get("optionalExperienceFamily") or "")
                for day in days
                for segment in (day.get("segments") or [])
                if isinstance(segment, dict) and (segment.get("semanticMetadata") or {}).get("portfolioOptional")
            }
            if not actual:
                theme_failures.append("theme_optional_family_missing")
            elif not actual.issubset(expected) or not expected.issubset(actual):
                theme_failures.append("theme_optional_family_missing")
            role_by_day = {role.day_number: role.role for role in brief.day_roles}
            for day in days:
                day_number = int(day.get("dayNumber") or 1)
                for segment in day.get("segments") or []:
                    semantic = segment.get("semanticMetadata") if isinstance(segment, dict) else {}
                    if not isinstance(semantic, dict) or not semantic.get("portfolioOptional"):
                        continue
                    if str(semantic.get("dayRole") or "") != str(role_by_day.get(day_number) or ""):
                        theme_failures.append(f"theme_day_role_mismatch:day_{day_number}")
        if snapshot.get("portfolioRouteVerificationRequired"):
            route_quality = (
                snapshot.get("portfolioRouteQuality") if isinstance(snapshot.get("portfolioRouteQuality"), dict) else {}
            )
            for item in route_quality.get("routeQualityIssues") or []:
                if not isinstance(item, dict):
                    continue
                issue_code = str(item.get("code") or item.get("message") or "route_quality_failed")
                # The normalized RouteOption pairs are authoritative for the
                # current grounded anchors. A stale aggregate flag from an
                # earlier preflight must not contradict exact pair coverage.
                if issue_code == "route_evidence_missing" and not route_coverage_failures:
                    route_quality_warnings.append("stale_route_evidence_missing_ignored_after_exact_pair_coverage")
                    continue
                route_quality_failures.append(issue_code)
            for evidence in normalized_routes:
                if not ProposalRouteEvidenceNormalizer.is_verified(evidence, segment_days):
                    route_quality_failures.append(
                        f"route_evidence_not_verified:{evidence.get('fromSegmentId')}:{evidence.get('toSegmentId')}"
                    )
                issue = str(evidence.get("qualityIssue") or "")
                if issue:
                    route_quality_failures.append(issue)
            route_quality_failures = list(dict.fromkeys(route_quality_failures))
            route_quality_warnings.extend(str(item) for item in (route_quality.get("warnings") or []) if str(item))
        failures.extend(occurrence_failures)
        failures.extend(binding_failures)
        failures.extend(capacity_failures)
        failures.extend(route_coverage_failures)
        failures.extend(f"portfolio_route_decision_proof:{item}" for item in route_decision_proof_failures)
        failures.extend(f"portfolio_route_quality:{item}" for item in route_quality_failures)
        failures.extend(pacing_violations)
        failures.extend(budget_violations)
        failures.extend(theme_failures)
        failures.extend(f"{item.get('code')}:{item.get('segmentId')}" for item in temporal_failures)
        pending_slots = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        hard_pending_slots = [
            item for item in pending_slots if str(item.get("requirementLevel") or "required") in {"hard", "required"}
        ]
        soft_pending_slots = [item for item in pending_slots if item not in hard_pending_slots]
        soft_pending_by_day: dict[int, int] = {}
        for item in soft_pending_slots:
            day_number = int(item.get("dayNumber") or 0)
            soft_pending_by_day[day_number] = soft_pending_by_day.get(day_number, 0) + 1

        strict_failures = list(dict.fromkeys(failures))
        waived_for_editable_draft: list[str] = []
        draft_failures: list[str] = []
        for failure in strict_failures:
            if failure == "theme_optional_family_missing" and soft_pending_slots:
                waived_for_editable_draft.append(failure)
                continue
            match = re.fullmatch(r"route_anchor_target_mismatch:day_(\d+):(\d+)/(\d+)", failure)
            if match and int(match.group(3)) - int(match.group(2)) <= soft_pending_by_day.get(int(match.group(1)), 0):
                waived_for_editable_draft.append(failure)
                continue
            draft_failures.append(failure)
        draft_passed = bool(soft_pending_slots and not hard_pending_slots and not draft_failures)
        soft_warnings = list(
            dict.fromkeys(
                [
                    *theme_warnings,
                    *(
                        f"soft_slot_pending:{item.get('planningSlotId') or item.get('slotId') or item.get('id')}"
                        for item in soft_pending_slots
                    ),
                    *(f"editable_draft_waived:{item}" for item in waived_for_editable_draft),
                ]
            )
        )
        return {
            "passed": not strict_failures,
            "draftPassed": draft_passed,
            "hardFailures": draft_failures if draft_passed else strict_failures,
            "strictFailures": strict_failures,
            "pendingHardSlotCount": len(hard_pending_slots),
            "pendingSoftSlotCount": len(soft_pending_slots),
            "requiredGoalCoverage": {
                goal.goal_id: covered_counts[goal.goal_id] >= goal.required_min for goal in ledger.hard_goals
            },
            "goalOccurrenceCoverage": occurrence_coverage,
            "goalOccurrenceFailures": occurrence_failures,
            **binding_evidence,
            "dailyCapacityEvidence": capacity_evidence,
            "dailyCapacityFailures": capacity_failures,
            "routeCoverageFailures": route_coverage_failures,
            "routeDecisionProofFailures": route_decision_proof_failures,
            "routeDecisionProofEvidence": route_decision_proof_evidence,
            "routeQualityFailures": route_quality_failures,
            "normalizedRouteEvidenceCount": len(normalized_routes),
            "admissionMaterializationAudit": admission_materialization,
            "routeQualityWarnings": route_quality_warnings,
            "pacingViolations": pacing_violations,
            "hardBudgetViolations": budget_violations,
            "themeAlignmentFailures": theme_failures,
            "themeAlignmentWarnings": theme_warnings,
            "temporalFailures": temporal_failures,
            "softWarnings": soft_warnings,
            "dayAnchorTargets": {str(day): target for day, target in sorted(day_anchor_targets.items())},
            "dayAnchorActuals": {str(day): day_anchor_actuals.get(day, 0) for day in sorted(day_anchor_targets)},
            "dayAnchorShortfalls": day_anchor_shortfalls,
            "densityDecisionSource": snapshot.get("portfolioDensityDecisionSource"),
            "transportPreference": snapshot.get("portfolioTransportPreference"),
            "dayEvidence": (snapshot.get("portfolioPlanningProjection") or {}).get("dayEvidence", {}),
            "targetRouteAnchorCoveragePassed": not any(
                day_anchor_actuals.get(day, 0) != target for day, target in day_anchor_targets.items()
            ),
            "estimatedCostCny": round(total_cost, 2),
        }

    @classmethod
    def _verify_route_decision_proofs(
        cls,
        snapshot: dict[str, Any],
        days: list[dict[str, Any]],
    ) -> tuple[list[str], dict[str, Any]]:
        """Recompute every persisted insertion decision from raw Provider legs.

        Aggregate route-quality status is deliberately ignored here.  A proof
        is accepted only when its contract fingerprint, exact policy,
        adjacency endpoints, Provider freshness, generalized-cost delta and
        time-window result all reproduce under ``RouteInsertionScorer``.
        """

        failures: list[str] = []
        checked_segment_ids: list[str] = []
        normalized_contract = RouteInsertionScorer.normalized_route_decision_contract(
            snapshot.get("routeDecisionContract")
        )
        for day in days:
            anchors = [
                segment
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
                and isinstance(segment.get("semanticMetadata"), dict)
                and bool((segment.get("semanticMetadata") or {}).get("routeAnchor"))
            ]
            for index, segment in enumerate(anchors):
                semantic = segment.get("semanticMetadata") or {}
                route_contract = (
                    semantic.get("routeContract") if isinstance(semantic.get("routeContract"), dict) else {}
                )
                proof = (
                    semantic.get("routeInsertionMatrixProof")
                    if isinstance(semantic.get("routeInsertionMatrixProof"), dict)
                    else None
                )
                requires_provider_decision = bool(
                    (
                        route_contract.get("requiresProviderInsertionDecision")
                        and 0 < index < len(anchors) - 1
                    )
                    or (
                        str(segment.get("kind") or "") == "meal"
                        and len(anchors) >= 2
                        and snapshot.get("portfolioRouteVerificationRequired")
                    )
                )
                segment_id = str(segment.get("id") or "")
                if proof is None:
                    if requires_provider_decision:
                        failures.append(f"route_insertion_proof_missing:{segment_id}")
                    continue
                checked_segment_ids.append(segment_id)
                if normalized_contract is None:
                    failures.append(f"route_decision_contract_missing_or_invalid:{segment_id}")
                    continue
                previous = anchors[index - 1] if index > 0 else None
                following = anchors[index + 1] if index + 1 < len(anchors) else None
                failures.extend(
                    cls._route_decision_proof_issues(
                        proof,
                        contract=normalized_contract,
                        route_contract=route_contract,
                        previous=previous,
                        candidate=segment,
                        following=following,
                    )
                )
        return list(dict.fromkeys(failures)), {
            "checkedProofCount": len(checked_segment_ids),
            "checkedSegmentIds": checked_segment_ids,
            "contractFingerprint": (
                normalized_contract.get("fingerprint") if normalized_contract is not None else None
            ),
            "passed": not failures,
        }

    @classmethod
    def _route_decision_proof_issues(
        cls,
        proof: dict[str, Any],
        *,
        contract: dict[str, Any],
        route_contract: dict[str, Any],
        previous: dict[str, Any] | None,
        candidate: dict[str, Any],
        following: dict[str, Any] | None,
    ) -> list[str]:
        segment_id = str(candidate.get("id") or "")
        issues: list[str] = []
        expected_proof_fingerprint = RouteInsertionScorer.route_proof_fingerprint(proof)
        if expected_proof_fingerprint is None or str(proof.get("proofFingerprint") or "") != expected_proof_fingerprint:
            issues.append(f"route_proof_fingerprint_mismatch:{segment_id}")
        if str(proof.get("contractFingerprint") or "") != str(contract.get("fingerprint") or ""):
            issues.append(f"route_contract_fingerprint_mismatch:{segment_id}")
        if proof.get("detourTolerance") != contract.get("detourTolerance"):
            issues.append(f"route_detour_policy_mismatch:{segment_id}")
        if proof.get("mobilityProfile") != contract.get("mobilityProfile"):
            issues.append(f"route_mobility_policy_mismatch:{segment_id}")
        if str(proof.get("detourToleranceFingerprint") or "") != canonical_fingerprint(contract.get("detourTolerance")):
            issues.append(f"route_detour_policy_fingerprint_mismatch:{segment_id}")
        if str(proof.get("mobilityProfileFingerprint") or "") != canonical_fingerprint(contract.get("mobilityProfile")):
            issues.append(f"route_mobility_policy_fingerprint_mismatch:{segment_id}")
        embedded_contract = RouteInsertionScorer.normalized_route_decision_contract(proof.get("routeDecisionContract"))
        if embedded_contract is None or embedded_contract.get("fingerprint") != contract.get("fingerprint"):
            issues.append(f"route_embedded_contract_mismatch:{segment_id}")
        expected_spec_fingerprint = str(route_contract.get("specFingerprint") or "")
        if expected_spec_fingerprint and str(proof.get("specFingerprint") or "") != expected_spec_fingerprint:
            issues.append(f"route_spec_fingerprint_mismatch:{segment_id}")
        expected_time_window = cls._expected_route_time_window(route_contract)
        if expected_time_window is not None and proof.get("timeWindow") != expected_time_window:
            issues.append(f"route_time_window_mismatch:{segment_id}")
        if proof.get("networkVerified") is not True:
            issues.append(f"route_network_verification_missing:{segment_id}")
        if proof.get("timeWindowFeasible") is not True:
            issues.append(f"route_time_window_infeasible:{segment_id}")
        if not cls._fresh_timestamp(proof.get("verifiedAt")):
            issues.append(f"route_proof_stale_or_invalid:{segment_id}")

        matrix = next(
            (
                value
                for value in (
                    proof.get("routeMatrix"),
                    proof.get("matrixLegs"),
                    proof.get("legs"),
                )
                if isinstance(value, dict)
            ),
            None,
        )
        if matrix is None:
            return [*issues, f"route_matrix_legs_missing:{segment_id}"]

        previous_id = str((previous or {}).get("id") or "")
        candidate_id = segment_id
        following_id = str((following or {}).get("id") or "")
        previous_amap_id = cls._segment_amap_id(previous)
        candidate_amap_id = cls._segment_amap_id(candidate)
        following_amap_id = cls._segment_amap_id(following)
        incoming = matrix.get("previousToCandidate")
        outgoing = matrix.get("candidateToNext")
        baseline = matrix.get("previousToNext")
        candidate_endpoint = proof.get("candidateEndpoint")
        baseline_endpoints = proof.get("baselineEndpoints")
        if not isinstance(candidate_endpoint, dict) or (
            str(candidate_endpoint.get("segmentId") or "") != candidate_id
            or str(candidate_endpoint.get("amapId") or "").upper() != candidate_amap_id.upper()
        ):
            issues.append(f"route_candidate_endpoint_mismatch:{segment_id}")
        if not isinstance(baseline_endpoints, dict) or any(
            (str(baseline_endpoints.get(key) or "").upper() != str(expected or "").upper())
            for key, expected in (
                ("previousSegmentId", previous_id),
                ("previousAmapId", previous_amap_id),
                ("nextSegmentId", following_id),
                ("nextAmapId", following_amap_id),
            )
        ):
            issues.append(f"route_baseline_endpoints_mismatch:{segment_id}")
        expected_legs = [
            (
                "previousToCandidate",
                incoming,
                previous_id,
                candidate_id,
                previous_amap_id,
                candidate_amap_id,
                previous is not None,
            ),
            (
                "candidateToNext",
                outgoing,
                candidate_id,
                following_id,
                candidate_amap_id,
                following_amap_id,
                following is not None,
            ),
            (
                "previousToNext",
                baseline,
                previous_id,
                following_id,
                previous_amap_id,
                following_amap_id,
                previous is not None and following is not None,
            ),
        ]
        for name, leg, from_id, to_id, from_amap, to_amap, required in expected_legs:
            if not required:
                if leg is not None:
                    issues.append(f"route_matrix_unexpected_leg:{segment_id}:{name}")
                continue
            issues.extend(
                cls._provider_leg_issues(
                    leg,
                    segment_id=segment_id,
                    name=name,
                    from_segment_id=from_id,
                    to_segment_id=to_id,
                    from_amap_id=from_amap,
                    to_amap_id=to_amap,
                )
            )
        recomputed = RouteInsertionScorer().score_from_route_matrix(
            previous_to_candidate=incoming,
            candidate_to_next=outgoing,
            previous_to_next=baseline,
            detour_tolerance=contract["detourTolerance"],
            schedule_slack_minutes=proof.get("scheduleSlackMinutes"),
            time_window_feasible=proof.get("timeWindowFeasible"),
            mobility_profile=contract["mobilityProfile"],
        )
        if recomputed is None or not recomputed.network_verified:
            return [*issues, f"route_matrix_recompute_failed:{segment_id}"]
        if not cls._same_optional_number(proof.get("generalizedCostDelta"), recomputed.generalized_cost_delta):
            issues.append(f"route_generalized_cost_delta_mismatch:{segment_id}")
        if not cls._same_optional_number(proof.get("detourRatio"), recomputed.detour_ratio):
            issues.append(f"route_detour_ratio_mismatch:{segment_id}")
        if str(proof.get("detourLevel") or "") != recomputed.detour_level:
            issues.append(f"route_detour_level_mismatch:{segment_id}")
        if recomputed.detour_level == "unacceptable":
            issues.append(f"route_matrix_unacceptable:{segment_id}")
        return issues

    @classmethod
    def _provider_leg_issues(
        cls,
        leg: Any,
        *,
        segment_id: str,
        name: str,
        from_segment_id: str,
        to_segment_id: str,
        from_amap_id: str,
        to_amap_id: str,
    ) -> list[str]:
        if not isinstance(leg, dict):
            return [f"route_matrix_leg_missing:{segment_id}:{name}"]
        issues: list[str] = []
        if (
            str(leg.get("fromSegmentId") or "") != from_segment_id
            or str(leg.get("toSegmentId") or "") != to_segment_id
            or str(leg.get("fromAmapId") or "").upper() != from_amap_id.upper()
            or str(leg.get("toAmapId") or "").upper() != to_amap_id.upper()
        ):
            issues.append(f"route_matrix_endpoint_mismatch:{segment_id}:{name}")
        canonical_providers = ProposalRouteEvidenceNormalizer.CANONICAL_PROVIDERS
        provider = str(leg.get("provider") or "").casefold()
        source = str(leg.get("source") or "").casefold()
        if provider not in canonical_providers and source not in canonical_providers:
            issues.append(f"route_matrix_provider_invalid:{segment_id}:{name}")
        if not cls._fresh_timestamp(leg.get("queriedAt")):
            issues.append(f"route_matrix_leg_stale_or_invalid:{segment_id}:{name}")
        if (
            RouteInsertionScorer._generalized_cost(
                leg,
                {
                    "source": "proof_validation",
                    "walkingPenaltyMinutesPerKm": 0.0,
                    "transferPenaltyMinutes": 0.0,
                    "waitTimeMultiplier": 0.0,
                    "riskPenaltyMultiplier": 0.0,
                },
            )
            is None
        ):
            issues.append(f"route_matrix_leg_cost_invalid:{segment_id}:{name}")
        return issues

    @staticmethod
    def _segment_amap_id(segment: dict[str, Any] | None) -> str:
        if not isinstance(segment, dict):
            return ""
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return str(poi.get("amapId") or poi.get("id") or "")

    @staticmethod
    def _expected_route_time_window(
        route_contract: dict[str, Any],
    ) -> dict[str, str] | None:
        policy = route_contract.get("experienceSpecPolicy")
        raw = (
            policy.get("timeWindow")
            if isinstance(policy, dict) and "timeWindow" in policy
            else route_contract.get("timeWindow")
        )
        if not isinstance(raw, dict):
            return None
        start = raw.get("start") or raw.get("earliestStart")
        end = raw.get("end") or raw.get("latestEnd")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start.strip()) is None:
            return None
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end.strip()) is None:
            return None
        return {"start": start.strip(), "end": end.strip()}

    @staticmethod
    def _fresh_timestamp(value: Any) -> bool:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
        return -60 <= age <= ROUTE_CACHE_TTL_SECONDS

    @staticmethod
    def _same_optional_number(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return left is None and right is None
        try:
            return math.isclose(float(left), float(right), abs_tol=0.01)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _admission_scope_matches(
        report: dict[str, Any],
        semantic: dict[str, Any],
        day_number: int,
    ) -> bool:
        scope = report.get("consumerScope")
        if not isinstance(scope, dict):
            return False
        expected = {
            "briefId": semantic.get("creativeBriefId"),
            "poolId": semantic.get("poolId"),
            "planningSlotId": semantic.get("planningSlotId") or semantic.get("slotId"),
            "dayNumber": day_number,
        }
        for key, value in expected.items():
            if value not in (None, "") and str(scope.get(key) or "") != str(value):
                return False
        family = str(semantic.get("optionalExperienceFamily") or "")
        return not family or family in {
            str(scope.get("family") or ""),
            str(scope.get("optionalExperienceFamily") or ""),
        }

    @classmethod
    def _verify_goal_occurrences(
        cls,
        snapshot: dict[str, Any],
        days: list[dict[str, Any]],
        *,
        expected_city: str,
        canonical_identity_required: bool = False,
    ) -> tuple[list[str], dict[str, bool]]:
        plan = snapshot.get("portfolioGoalOccurrencePlan")
        if not isinstance(plan, dict):
            return [], {}
        occurrences = [item for item in plan.get("occurrences") or [] if isinstance(item, dict)]
        failures: list[str] = []
        coverage: dict[str, bool] = {}
        identities_by_group: dict[str, set[str]] = {}
        repeated_hard_night_groups: dict[str, str] = {}
        hard_night_goal_ids = {
            goal_id
            for goal_id in {
                str(item.get("sourceGoalId") or "")
                for item in occurrences
                if str(item.get("requirementLevel") or "") == "hard"
                and str(item.get("intentType") or "").strip().lower() == "night_view"
            }
            if goal_id
            and sum(
                1
                for item in occurrences
                if str(item.get("sourceGoalId") or "") == goal_id
                and str(item.get("requirementLevel") or "") == "hard"
                and str(item.get("intentType") or "").strip().lower() == "night_view"
            )
            > 1
        }
        for goal_id in hard_night_goal_ids:
            declared_groups = {
                str(item.get("distinctGroupId") or "")
                for item in occurrences
                if str(item.get("sourceGoalId") or "") == goal_id
                and str(item.get("requirementLevel") or "") == "hard"
                and str(item.get("intentType") or "").strip().lower() == "night_view"
                and str(item.get("distinctGroupId") or "")
            }
            repeated_hard_night_groups[goal_id] = (
                next(iter(declared_groups)) if len(declared_groups) == 1 else f"distinct:{goal_id}"
            )
        for occurrence in occurrences:
            occurrence_id = str(occurrence.get("occurrenceId") or "")
            goal_id = str(occurrence.get("sourceGoalId") or "")
            intent_type = str(occurrence.get("intentType") or "")
            day_number = int(occurrence.get("dayNumber") or 0)
            matches: list[dict[str, Any]] = []
            for day in days:
                if int(day.get("dayNumber") or 0) != day_number:
                    continue
                for segment in day.get("segments") or []:
                    if not isinstance(segment, dict):
                        continue
                    semantic = (
                        segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                    )
                    poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                    if (
                        str(semantic.get("occurrenceId") or "") == occurrence_id
                        and str(semantic.get("goalId") or semantic.get("softGoalId") or "") == goal_id
                        and str(semantic.get("intentType") or "") == intent_type
                        and semantic.get("groundingStatus") in cls._GROUNDED_STATUSES
                        and cls._has_grounded_identity(
                            poi,
                            city=expected_city,
                            canonical_identity_required=canonical_identity_required,
                        )
                    ):
                        matches.append(segment)
            covered = len(matches) == 1
            if occurrence_id:
                coverage[occurrence_id] = covered
            if not matches:
                if str(occurrence.get("requirementLevel") or "") == "hard":
                    failures.append(f"goal_occurrence_missing:{occurrence_id or goal_id}:day_{day_number}")
                continue
            if len(matches) != 1:
                failures.append(f"goal_occurrence_duplicate:{occurrence_id}:day_{day_number}:{len(matches)}")
                continue
            group = str(occurrence.get("distinctGroupId") or "")
            if goal_id in repeated_hard_night_groups:
                # Defense in depth for persisted or tampered occurrence plans:
                # repeated hard night views are always distinct physical
                # experiences, even if a generic reuse policy removed or split
                # the compiler-authored distinct group.
                group = repeated_hard_night_groups[goal_id]
            if group:
                identity = cls._canonical_physical_identity(matches[0].get("poi") or {})
                used = identities_by_group.setdefault(group, set())
                if identity in used:
                    failures.append(f"goal_occurrence_identity_reused:{group}:{identity}")
                used.add(identity)
        return list(dict.fromkeys(failures)), coverage

    @staticmethod
    def _canonical_physical_identity(poi: dict[str, Any]) -> str:
        """Collapse AMap parent/child records to one physical experience.

        AMap commonly exposes a landmark and its observation deck as separate
        IDs.  They remain useful search records, but cannot satisfy two
        distinct hard occurrences when the child points back to the same
        parent place.
        """

        return PoiPhysicalIdentityService.canonical_amap_id(poi)

    @classmethod
    def _verify_required_candidate_bindings(
        cls,
        snapshot: dict[str, Any],
        days: list[dict[str, Any]],
        brief: CreativeBrief | None,
        *,
        expected_city: str,
        canonical_identity_required: bool = False,
    ) -> tuple[list[str], dict[str, Any]]:
        bindings = [item for item in snapshot.get("portfolioRequiredCandidateBindings") or [] if isinstance(item, dict)]
        if not bindings:
            occurrence_plan = snapshot.get("portfolioGoalOccurrencePlan")
            expected_occurrences = (
                {
                    str(item.get("occurrenceId") or "")
                    for item in (occurrence_plan.get("occurrences") or [])
                    if isinstance(item, dict)
                    and str(item.get("requirementLevel") or "") == "hard"
                    and str(item.get("occurrenceId") or "")
                }
                if isinstance(occurrence_plan, dict)
                else set()
            )
            if brief is not None and expected_occurrences:
                failure = "required_candidate_bindings_missing"
                return [failure], {
                    "requiredCandidateBindingExpectedCount": len(expected_occurrences),
                    "requiredCandidateBindingActualCount": 0,
                    "requiredCandidateLineageFailures": [failure],
                    "requiredCandidateLineageCoverage": False,
                    "hardCandidateCrossBriefLeakCount": 0,
                    "hardCandidateLineageMissingCount": len(expected_occurrences),
                }
            return [], {
                "requiredCandidateBindingExpectedCount": 0,
                "requiredCandidateBindingActualCount": 0,
                "requiredCandidateLineageFailures": [],
                "requiredCandidateLineageCoverage": True,
                "hardCandidateCrossBriefLeakCount": 0,
                "hardCandidateLineageMissingCount": 0,
            }
        expected_brief_id = str((brief.brief_id if brief is not None else "") or "")
        failures: list[str] = []
        actual = 0
        cross_brief_segment_ids: set[str] = set()
        missing = 0
        occurrence_plan = snapshot.get("portfolioGoalOccurrencePlan")
        expected_occurrences = (
            {
                str(item.get("occurrenceId") or "")
                for item in (occurrence_plan.get("occurrences") or [])
                if isinstance(item, dict) and str(item.get("requirementLevel") or "") == "hard"
            }
            if isinstance(occurrence_plan, dict)
            else set()
        )
        binding_occurrences = {str(item.get("occurrenceId") or "") for item in bindings}
        if expected_occurrences and binding_occurrences != expected_occurrences:
            failures.append("required_candidate_binding_occurrence_set_mismatch")
        seen_binding_occurrences: set[str] = set()
        all_segments = [
            (int(day.get("dayNumber") or 0), segment)
            for day in days
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
        ]
        for binding in bindings:
            occurrence_id = str(binding.get("occurrenceId") or "")
            binding_brief_id = str(binding.get("briefId") or "")
            if not occurrence_id or occurrence_id in seen_binding_occurrences:
                failures.append(f"required_candidate_binding_duplicate_or_missing:{occurrence_id}")
                continue
            seen_binding_occurrences.add(occurrence_id)
            if not expected_brief_id or binding_brief_id != expected_brief_id:
                failures.append(f"required_candidate_binding_brief_mismatch:{occurrence_id}")
                continue
            matches = []
            for day_number, segment in all_segments:
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                if not semantic.get("required"):
                    continue
                if str(semantic.get("creativeBriefId") or "") != expected_brief_id:
                    cross_brief_segment_ids.add(str(segment.get("id") or ""))
                    continue
                if (
                    str(semantic.get("occurrenceId") or "") == occurrence_id
                    and str(semantic.get("goalId") or "") == str(binding.get("sourceGoalId") or "")
                    and str(semantic.get("intentType") or "") == str(binding.get("intentType") or "")
                    and str(semantic.get("poolId") or "") == str(binding.get("poolId") or "")
                    and str(semantic.get("planningSlotId") or semantic.get("slotId") or "")
                    == str(binding.get("planningSlotId") or "")
                    and day_number == int(binding.get("dayNumber") or 0)
                    and PoiPhysicalIdentityService.normalized_amap_id(poi)
                    == PoiPhysicalIdentityService.normalized_amap_id(binding)
                    and cls._has_grounded_identity(
                        poi,
                        city=expected_city,
                        canonical_identity_required=canonical_identity_required,
                    )
                ):
                    matches.append(segment)
            if len(matches) != 1:
                missing += 1
                failures.append(f"required_candidate_binding_segment_mismatch:{occurrence_id}:{len(matches)}")
            else:
                actual += 1
        for _day_number, segment in all_segments:
            semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            if (
                semantic.get("required")
                and expected_brief_id
                and str(semantic.get("creativeBriefId") or "") != expected_brief_id
            ):
                cross_brief_segment_ids.add(str(segment.get("id") or ""))
        cross_brief = len(cross_brief_segment_ids - {""})
        if cross_brief:
            failures.append(f"hard_candidate_cross_brief_leak:{cross_brief}")
        return list(dict.fromkeys(failures)), {
            "requiredCandidateBindingExpectedCount": len(expected_occurrences)
            if expected_occurrences
            else len(bindings),
            "requiredCandidateBindingActualCount": actual,
            "requiredCandidateLineageFailures": list(dict.fromkeys(failures)),
            "requiredCandidateLineageCoverage": actual
            == (len(expected_occurrences) if expected_occurrences else len(bindings)),
            "hardCandidateCrossBriefLeakCount": cross_brief,
            "hardCandidateLineageMissingCount": missing,
        }

    @staticmethod
    def _verify_daily_capacity(snapshot: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, int]]]:
        raw = snapshot.get("portfolioDailyCapacityPlan")
        if not isinstance(raw, dict):
            return [], {}
        failures: list[str] = []
        evidence: dict[str, dict[str, int]] = {}
        for day, value in raw.items():
            if not isinstance(value, dict):
                failures.append(f"daily_capacity_invalid:day_{day}")
                continue
            try:
                normalized = {
                    "usableMinutes": int(value.get("usableMinutes") or 0),
                    "plannedMinutes": int(value.get("plannedMinutes") or 0),
                    "routeReserveMinutes": int(value.get("routeReserveMinutes") or 0),
                    "bufferMinutes": int(value.get("bufferMinutes") or 0),
                    "intentionalFreeMinutes": int(value.get("intentionalFreeMinutes") or 0),
                    "unexplainedGapMinutes": int(value.get("unexplainedGapMinutes") or 0),
                    "targetRouteAnchors": int(value.get("targetRouteAnchors") or 0),
                }
            except (TypeError, ValueError):
                failures.append(f"daily_capacity_invalid:day_{day}")
                continue
            evidence[str(day)] = normalized
            allocated = (
                normalized["plannedMinutes"]
                + normalized["routeReserveMinutes"]
                + normalized["bufferMinutes"]
                + normalized["intentionalFreeMinutes"]
            )
            if normalized["unexplainedGapMinutes"] != 0:
                failures.append(f"daily_capacity_unexplained_gap:day_{day}")
            if allocated != normalized["usableMinutes"]:
                failures.append(f"daily_capacity_not_balanced:day_{day}:{allocated}/{normalized['usableMinutes']}")
        return list(dict.fromkeys(failures)), evidence

    @staticmethod
    def _budget_limit(ledger: ConstraintLedger) -> float | None:
        raw = ledger.preference_snapshot.get("budget") or ledger.preference_snapshot.get("budgetCny")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _matching_goal_ids(
        *,
        goal_id: str,
        intent_type: str,
        goals_by_id: dict[str, Any],
        ledger: ConstraintLedger,
    ) -> set[str]:
        """Project a grounded segment to one hard goal, never every same-intent goal.

        A persisted goalId is authoritative.  Intent-only projection remains a
        compatibility path only when that intent maps to exactly one hard goal;
        otherwise the proposal must carry an explicit goalId.
        """
        normalized_goal_id = str(goal_id or "").strip()
        goal = goals_by_id.get(normalized_goal_id)
        if goal is not None:
            if intent_type and intent_type != goal.intent_type:
                return set()
            return {normalized_goal_id}
        # A present goalId is persisted identity, not a hint.  Falling back by
        # intent after an unknown ID would let a forged proposal claim the only
        # hard goal of that intent.  Intent-only compatibility is reserved for
        # legacy segments that genuinely omitted goalId.
        if normalized_goal_id:
            return set()
        matches = [goal.goal_id for goal in ledger.hard_goals if goal.intent_type == intent_type]
        return set(matches) if len(matches) == 1 else set()

    @staticmethod
    def _has_grounded_identity(
        poi: dict[str, Any],
        *,
        city: str = "",
        canonical_identity_required: bool = False,
    ) -> bool:
        candidate_city = str(poi.get("city") or "").strip().removesuffix("市")
        expected_city = str(city or "").strip().removesuffix("市")
        amap_id = str(poi.get("amapId") or "").strip().upper()
        if canonical_identity_required and re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id) is None:
            return False
        if canonical_identity_required and PoiPhysicalIdentityService.invalid_parent_id(poi):
            return False
        if PoiTrustPolicy().is_mock_or_synthetic_poi_values(
            source=poi.get("source"),
            amap_id=poi.get("amapId"),
            source_note=poi.get("sourceNote"),
            name=poi.get("name"),
        ):
            return False
        return bool(
            amap_id
            and PlanProposalVerifier._has_valid_coordinates(poi)
            and (poi.get("providerType") or poi.get("type"))
            and str(poi.get("source") or "") == AMAP_PLACE_SOURCE
            and (not expected_city or candidate_city == expected_city)
        )

    @staticmethod
    def _has_valid_coordinates(poi: dict[str, Any]) -> bool:
        try:
            longitude = float(poi.get("longitude"))
            latitude = float(poi.get("latitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and (longitude != 0 or latitude != 0)
        )

    @staticmethod
    def _minutes(value: Any) -> int | None:
        try:
            hour, minute = (int(item) for item in str(value).split(":"))
            return hour * 60 + minute if 0 <= hour < 24 and 0 <= minute < 60 else None
        except (TypeError, ValueError):
            return None

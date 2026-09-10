"""Derive proposal readiness from persisted evidence instead of caller booleans."""

from __future__ import annotations

import math
import re
from typing import Any, Optional
from src.services.creative_output_quality_service import CreativeOutputQualityService
from src.services.meal_experience_portfolio import MealExperiencePortfolioPolicy
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer
from src.services.travel_visit_anchor_eligibility_policy import TravelVisitAnchorEligibilityPolicy


class ProposalReadinessService:
    @classmethod
    def compute(
        cls,
        snapshot: dict[str, Any],
        *,
        verifier: dict[str, Any] | None = None,
        soft_slot_draft_adoption_enabled: Optional[bool] = None,
        route_failures_non_blocking: bool = False,
    ) -> dict[str, Any]:
        if soft_slot_draft_adoption_enabled is None:
            from src.core.config import get_settings

            soft_slot_draft_adoption_enabled = get_settings().agent_soft_slot_draft_adoption_enabled
        verifier = verifier if isinstance(verifier, dict) else {}
        route_targets_by_day = cls.route_target_segments_by_day(snapshot)
        route_anchors_by_day: dict[int, list[str]] = {
            day_number: [
                str(segment.get("id") or "").strip() for segment in segments if str(segment.get("id") or "").strip()
            ]
            for day_number, segments in route_targets_by_day.items()
        }
        simple_open_profile = str(snapshot.get("simpleOpenExecutionProfile") or "") == "simple_open_v1"
        calendar_day_numbers = {
            int(day.get("dayNumber") or 0)
            for day in snapshot.get("days") or []
            if isinstance(day, dict) and int(day.get("dayNumber") or 0) > 0
        }
        daily_coverage_contract_invalid = False
        daily_coverage_source_invalid = False
        daily_anchor_target_contract_invalid = False
        daily_target_day_partition_invalid = False

        def normalized_day_contract(value: Any) -> set[int]:
            nonlocal daily_coverage_contract_invalid
            if not isinstance(value, list):
                daily_coverage_contract_invalid = True
                return set()
            result: set[int] = set()
            for raw_day in value:
                if isinstance(raw_day, bool) or not isinstance(raw_day, int) or raw_day <= 0:
                    daily_coverage_contract_invalid = True
                    return set()
                result.add(raw_day)
            if len(result) != len(value):
                daily_coverage_contract_invalid = True
                return set()
            return result

        required_planning_day_numbers: set[int] = set()
        explicit_rest_day_numbers: set[int] = set()
        if simple_open_profile:
            required_planning_day_numbers = normalized_day_contract(snapshot.get("requiredPlanningDayNumbers"))
            explicit_rest_day_numbers = normalized_day_contract(snapshot.get("explicitRestDayNumbers"))
            daily_coverage_source_invalid = (
                str(snapshot.get("dailyPlanningCoverageSource") or "")
                != "authoritative_goal_occurrences"
            )
            if (
                required_planning_day_numbers & explicit_rest_day_numbers
                or (required_planning_day_numbers | explicit_rest_day_numbers) != calendar_day_numbers
            ):
                daily_coverage_contract_invalid = True
            normalized_targets: dict[int, int] = {}
            raw_targets = snapshot.get("desiredDensityAnchorTargets")
            if isinstance(raw_targets, dict) and raw_targets:
                for raw_day, raw_target in raw_targets.items():
                    if (
                        not isinstance(raw_day, str)
                        or not raw_day.isdigit()
                        or str(int(raw_day)) != raw_day
                        or isinstance(raw_target, bool)
                        or not isinstance(raw_target, int)
                        or raw_target < 0
                    ):
                        daily_anchor_target_contract_invalid = True
                        normalized_targets = {}
                        break
                    normalized_targets[int(raw_day)] = raw_target
            else:
                daily_anchor_target_contract_invalid = True
            if set(normalized_targets) != calendar_day_numbers or not any(
                target > 0 for target in normalized_targets.values()
            ):
                daily_anchor_target_contract_invalid = True
            if not daily_anchor_target_contract_invalid and not daily_coverage_contract_invalid:
                positive_target_days = {
                    day_number for day_number, target in normalized_targets.items() if target > 0
                }
                zero_target_days = {
                    day_number for day_number, target in normalized_targets.items() if target == 0
                }
                daily_target_day_partition_invalid = bool(
                    positive_target_days != required_planning_day_numbers
                    or zero_target_days != explicit_rest_day_numbers
                )
        uncovered_day_numbers = {
            day_number
            for day_number in required_planning_day_numbers
            if not route_targets_by_day.get(day_number)
        }
        uncovered_day_numbers.update(
            int(day_number)
            for day_number in verifier.get("uncoveredDayNumbers") or []
            if isinstance(day_number, int) and not isinstance(day_number, bool) and day_number > 0
        )
        if daily_coverage_contract_invalid:
            # A calendar day omitted from both authoritative day sets is not a
            # rest day.  Keep it visible as uncovered so malformed historical
            # contracts cannot fail closed internally while looking complete
            # in the comparison projection.
            uncovered_day_numbers.update(
                calendar_day_numbers - (required_planning_day_numbers | explicit_rest_day_numbers)
            )
        all_segments: list[dict[str, Any]] = []
        invalid_identity_count = 0
        semantic_failure_count = 0
        eligibility_policy = TravelVisitAnchorEligibilityPolicy()
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                all_segments.append(segment)
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                is_route_target = cls._is_route_target_segment(segment)
                if (is_route_target and not str(segment.get("id") or "").strip()) or (
                    cls._declares_route_target_segment(segment) and not cls._real_amap_poi(segment.get("poi"))
                ):
                    invalid_identity_count += 1
                if semantic.get("portfolioOptional"):
                    family = str(semantic.get("optionalExperienceFamily") or "")
                    eligibility = eligibility_policy.evaluate(
                        segment.get("poi") if isinstance(segment.get("poi"), dict) else {},
                        family=family,
                    )
                    if semantic.get("semanticPassed") is False or eligibility.classification != "final_visit_anchor":
                        semantic_failure_count += 1

        expected_pairs = {
            (str(item["fromSegmentId"]), str(item["toSegmentId"])) for item in cls.expected_route_pairs(snapshot)
        }
        normalized_routes = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
        verified_pairs: set[tuple[str, str]] = set()
        route_quality = (
            snapshot.get("portfolioRouteQuality") if isinstance(snapshot.get("portfolioRouteQuality"), dict) else {}
        )
        provider_state = str(route_quality.get("providerState") or "").casefold()
        provider_failed = provider_state in {"provider_error", "worker_error", "timeout"}
        route_verifying = provider_state in {"verifying", "route_verifying", "generating"}
        failed_pairs: set[tuple[str, str]] = set()
        for route in normalized_routes:
            if not isinstance(route, dict):
                continue
            if route.get("error") or str(route.get("status") or route.get("routeStatus") or "").casefold() in {
                "failed",
                "error",
                "provider_error",
            }:
                provider_failed = True
                failed_pairs.add(
                    (
                        str(route.get("fromSegmentId") or route.get("from_segment_id") or ""),
                        str(route.get("toSegmentId") or route.get("to_segment_id") or ""),
                    )
                )
            if cls._verified_route(
                route,
                {segment_id: day for day, ids in route_anchors_by_day.items() for segment_id in ids},
            ):
                verified_pairs.add(
                    (
                        str(route.get("fromSegmentId") or route.get("from_segment_id") or ""),
                        str(route.get("toSegmentId") or route.get("to_segment_id") or ""),
                    )
                )
        if str(route_quality.get("status") or "") in {
            "provider_error",
            "precondition_failed",
        } or str(route_quality.get("providerState") or "") in {
            "provider_error",
            "precondition_failed",
        }:
            provider_failed = True
        verified_required = expected_pairs & verified_pairs
        unexpected_verified_pairs = verified_pairs - expected_pairs
        route_coverage_exact = expected_pairs == verified_pairs
        route_target_identity_incomplete = any(
            not str(segment.get("id") or "").strip()
            for segments in route_targets_by_day.values()
            for segment in segments
        )
        if route_target_identity_incomplete or unexpected_verified_pairs:
            route_status = "route_invalid"
        elif not expected_pairs:
            route_status = "route_not_required"
        elif route_coverage_exact:
            route_status = "route_ready"
        elif route_verifying:
            route_status = "route_verifying"
        elif provider_failed:
            route_status = "route_provider_failed"
        elif verified_required:
            route_status = "route_partial"
        else:
            route_status = "route_pending"

        known_total = 0.0
        evidence_count = 0
        unknown_cost_count = 0
        for segment in all_segments:
            raw_cost = segment.get("estimatedCost")
            evidence = segment.get("costEvidence") if isinstance(segment.get("costEvidence"), dict) else {}
            verified_free = str(evidence.get("status") or "") == "verified_free"
            if isinstance(raw_cost, (int, float)) and (float(raw_cost) > 0 or verified_free):
                known_total += float(raw_cost)
                evidence_count += 1
            else:
                unknown_cost_count += 1
        if not all_segments or unknown_cost_count == len(all_segments):
            budget_status = "pending"
            budget_estimate = None
        elif unknown_cost_count:
            budget_status = "partial"
            budget_estimate = known_total
        else:
            budget_status = "verified" if evidence_count == len(all_segments) else "estimated"
            budget_estimate = known_total

        pending_slots = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        density_targets = cls.density_targets(snapshot)
        hard_pending_slots = [
            item for item in pending_slots if str(item.get("requirementLevel") or "required") in {"hard", "required"}
        ]
        hard_pending_slot_count = len(hard_pending_slots)
        completion_required_slots = cls.completion_required_pending_slots(snapshot)
        completion_required_pending_slot_count = len(completion_required_slots)
        completion_required_soft_slot_count = len(
            [
                item
                for item in completion_required_slots
                if str(item.get("requirementLevel") or "required") not in {"hard", "required"}
            ]
        )
        provider_exhausted_required_slot_count = len(
            [
                item
                for item in pending_slots
                if str(item.get("requirementLevel") or "required") in {"hard", "required"}
                and item.get("simpleDirectionProviderExhausted") is True
                and item.get("simpleDirectionRequirementLineageConflict") is not True
                and str(item.get("groundingStatus") or "") == "unresolved"
                and str(item.get("reasonCode") or "") == "provider_candidates_exhausted_or_semantically_rejected"
                and not isinstance(item.get("poi"), dict)
            ]
        )
        reported_blocking_hard_slots = int(
            verifier.get("blockingPendingHardSlotCount")
            if verifier.get("blockingPendingHardSlotCount") is not None
            else hard_pending_slot_count
        )
        simple_provider_exhaustion_contract_valid = bool(
            route_failures_non_blocking
            and verifier.get("allPlannedDaysHaveVerifiedAnchor") is True
            and int(verifier.get("providerExhaustedRequiredSlotCount") or 0) == provider_exhausted_required_slot_count
            and reported_blocking_hard_slots == max(hard_pending_slot_count - provider_exhausted_required_slot_count, 0)
        )
        blocking_hard_pending_slot_count = (
            reported_blocking_hard_slots if simple_provider_exhaustion_contract_valid else hard_pending_slot_count
        )
        blocking_required_pending_slot_count = blocking_hard_pending_slot_count + completion_required_soft_slot_count
        soft_pending_slot_count = len(pending_slots) - hard_pending_slot_count
        editable_pending_slot_count = max(soft_pending_slot_count - completion_required_soft_slot_count, 0) + max(
            hard_pending_slot_count - blocking_hard_pending_slot_count, 0
        )
        pending_slot_count = len(pending_slots)
        output_quality_contract = snapshot.get("portfolioOutputQuality")
        requested_theme = (
            str(output_quality_contract.get("requestedTheme") or "")
            if isinstance(output_quality_contract, dict)
            else ""
        )
        output_quality = CreativeOutputQualityService.evaluate(
            snapshot,
            requested_theme=requested_theme,
        )
        blocking: list[str] = []
        hard_failures = [str(item) for item in verifier.get("hardFailures") or [] if str(item)]
        soft_warnings = [str(item) for item in verifier.get("softWarnings") or [] if str(item)]
        draft_verifier_passed = bool(verifier.get("draftPassed") is True)
        meal_quality = (
            MealExperiencePortfolioPolicy.snapshot_quality(snapshot)
            if simple_open_profile
            else {
                "mealQualityPassed": True,
                "mealDiversityPassed": True,
                "mealUnresolvedReasons": [],
                "mealSemanticEvidence": [],
                "mealThemeSignature": [],
            }
        )
        if simple_open_profile and (
            meal_quality.get("mealQualityPassed") is not True
            or meal_quality.get("mealDiversityPassed") is not True
        ):
            meal_failures = [
                str(item)
                for item in meal_quality.get("mealUnresolvedReasons") or []
                if str(item)
            ] or ["simple_direction_meal_theme_ungrounded"]
            hard_failures.extend(meal_failures)
            blocking.extend(meal_failures)
        if daily_coverage_contract_invalid:
            hard_failures.append("simple_direction_daily_planning_coverage_contract_invalid")
            blocking.append("simple_direction_daily_planning_coverage_contract_invalid")
        if daily_coverage_source_invalid:
            hard_failures.append("simple_direction_daily_planning_coverage_source_invalid")
            blocking.append("simple_direction_daily_planning_coverage_source_invalid")
        if daily_anchor_target_contract_invalid:
            hard_failures.append("simple_direction_daily_anchor_target_contract_invalid")
            blocking.append("simple_direction_daily_anchor_target_contract_invalid")
        if daily_target_day_partition_invalid:
            hard_failures.append("simple_direction_daily_anchor_target_day_partition_invalid")
            blocking.append("simple_direction_daily_anchor_target_day_partition_invalid")
        if uncovered_day_numbers:
            hard_failures.append("simple_direction_required_day_empty")
            blocking.append("simple_direction_required_day_empty")
        hard_failures = list(dict.fromkeys(hard_failures))
        if not any(route_anchors_by_day.values()):
            blocking.append("proposal_has_no_visit_anchors")
        if verifier.get("passed") is not True and not (
            (soft_slot_draft_adoption_enabled or route_failures_non_blocking)
            and draft_verifier_passed
            and not blocking_required_pending_slot_count
        ):
            blocking.extend(hard_failures or ["proposal_verifier_not_passed"])
        if invalid_identity_count:
            blocking.append("map_identity_incomplete")
        if semantic_failure_count:
            blocking.append("semantic_coverage_failed")
        if route_status not in {"route_ready", "route_not_required"} and not route_failures_non_blocking:
            blocking.append("route_evidence_incomplete")
        elif route_status not in {"route_ready", "route_not_required"}:
            soft_warnings.append("portfolio_route_quality:route_evidence_missing")
        if blocking_required_pending_slot_count or (soft_pending_slot_count and not soft_slot_draft_adoption_enabled):
            blocking.append("pending_slots_remaining")
        output_quality_mode = (
            str(output_quality_contract.get("mode") or "off") if isinstance(output_quality_contract, dict) else "off"
        )
        if (
            output_quality_mode == "enforce"
            and requested_theme
            and output_quality["neutralPartialQualityPassed"] is not True
        ):
            blocking.append("neutral_partial_quality_floor_failed")
        blocking = list(dict.fromkeys(blocking))
        structure_blockers = {
            "proposal_has_no_visit_anchors",
            "map_identity_incomplete",
            "semantic_coverage_failed",
        }
        if not route_failures_non_blocking:
            structure_blockers.add("route_evidence_incomplete")
        structure_ready = bool(
            not blocking
            and not any(item in blocking for item in structure_blockers)
            and not blocking_required_pending_slot_count
        )
        draft_adoption_ready = bool(
            (soft_slot_draft_adoption_enabled or route_failures_non_blocking)
            and draft_verifier_passed
            and structure_ready
            and (soft_pending_slot_count or route_failures_non_blocking)
            and not blocking
        )
        backend_confirmation_passed = bool(
            verifier.get("confirmationPassed") is True
            if simple_open_profile
            else verifier.get("passed") is True
        )
        adoption_ready = bool(
            not blocking
            and (verifier.get("passed") is True or draft_adoption_ready)
            and (not simple_open_profile or backend_confirmation_passed)
        )
        strictly_verified = bool(
            verifier.get("passed") is True
            and (not simple_open_profile or backend_confirmation_passed)
            and semantic_failure_count == 0
            and invalid_identity_count == 0
            and route_coverage_exact
            and not any("schedule_" in item for item in hard_failures)
            and pending_slot_count == 0
            and not blocking
        )
        preview_only = bool(
            output_quality.get("visibilityMode") == "skeleton_preview_only"
            and draft_verifier_passed
            and structure_ready
            and route_status in {"route_ready", "route_not_required"}
        )
        adoption_mode = (
            "complete"
            if strictly_verified
            else "editable_partial"
            if draft_adoption_ready and route_failures_non_blocking
            else "editable_draft"
            if draft_adoption_ready
            else "preview_only"
            if preview_only
            else "blocked"
        )
        comparison_role = str(snapshot.get("comparisonRole") or "candidate_proposal")
        if comparison_role not in {"candidate_proposal", "current_active_draft"}:
            comparison_role = "candidate_proposal"
        origin_projection_mode = str(snapshot.get("originProjectionMode") or "")
        if origin_projection_mode not in {"partial_preview", "full_proposal", "current_active_draft"}:
            origin_projection_mode = "partial_preview" if snapshot.get("portfolioPartialTimeline") else "full_proposal"
        execution_ledger = (
            route_quality.get("executionLedger") if isinstance(route_quality.get("executionLedger"), dict) else {}
        )
        route_provider_attempt_count = int(execution_ledger.get("providerCallCount") or 0)
        route_provider_cache_hit_count = int(execution_ledger.get("providerCacheHitCount") or 0)
        explicit_zero_provider_attempt = bool(
            "providerCallCount" in execution_ledger
            and route_provider_attempt_count == 0
            and route_provider_cache_hit_count == 0
        )
        route_retryable = bool(
            route_status in {"route_partial", "route_provider_failed"}
            and not explicit_zero_provider_attempt
            and (
                route_provider_attempt_count > 0
                or route_provider_cache_hit_count > 0
                or bool(verified_required)
                or provider_failed
            )
        )
        non_route_blockers = {
            item
            for item in blocking
            if item not in {"route_evidence_incomplete"}
            and not item.startswith("portfolio_route_quality:")
            and not item.startswith("route_")
        }
        if origin_projection_mode == "partial_preview":
            promotion_status = (
                "promoted"
                if adoption_ready
                else ("promotable" if not non_route_blockers and not pending_slot_count else "not_promotable")
            )
        else:
            promotion_status = "not_promotable"
        legacy_next_action = "none"
        if draft_adoption_ready:
            next_action, next_action_label = (
                "adopt_proposal",
                f"采用为可编辑草案（仍可补充 {editable_pending_slot_count} 项）",
            )
            legacy_next_action = "adopt_editable_draft"
        elif blocking_hard_pending_slot_count:
            hard_pending_intents = {
                str(item.get("intentType") or item.get("optionalExperienceFamily") or item.get("sourceGoalId") or "")
                for item in pending_slots
                if str(item.get("requirementLevel") or "required") in {"hard", "required"}
            }
            night_view_only = bool(hard_pending_intents) and all(
                intent in {"night_view", "goal_night_view"} for intent in hard_pending_intents
            )
            next_action, next_action_label = (
                "continue_grounding_hard_slots",
                (
                    f"补齐 {blocking_hard_pending_slot_count} 个必选夜景地点"
                    if night_view_only
                    else f"补齐 {blocking_hard_pending_slot_count} 个必选地点"
                ),
            )
        elif pending_slot_count:
            next_action, next_action_label = (
                "complete_pending_slots",
                f"补全 {soft_pending_slot_count or pending_slot_count} 个待选体验",
            )
            legacy_next_action = "complete_pending_slots"
        elif route_status == "route_provider_failed":
            next_action, next_action_label = (
                ("retry_route_verification", "重试路线核验") if route_retryable else ("none", "路线前置条件未满足")
            )
        elif route_status == "route_verifying":
            next_action, next_action_label = "none", "正在核验路线"
        elif route_status in {"route_pending", "route_partial"}:
            next_action, next_action_label = (
                ("verify_routes", "补全路线")
                if comparison_role == "current_active_draft"
                else ("verify_routes_and_adopt", "核验路线并采用")
            )
        elif route_status == "route_invalid":
            next_action, next_action_label = "none", "路线证据与行程不一致"
        elif adoption_ready:
            next_action, next_action_label = (
                ("continue_editing", "继续编辑")
                if comparison_role == "current_active_draft"
                else ("adopt_proposal", "采用此方案")
            )
            legacy_next_action = "continue_editing" if comparison_role == "current_active_draft" else "adopt"
        elif comparison_role == "current_active_draft":
            next_action, next_action_label = "continue_editing", "继续编辑"
        else:
            next_action, next_action_label = "none", "暂不可采用"
        if non_route_blockers or route_status == "route_invalid":
            current_readiness = "blocked"
        elif route_status in {"route_ready", "route_not_required"}:
            current_readiness = "route_ready"
        elif route_status in {"route_pending", "route_partial", "route_provider_failed", "route_verifying"}:
            current_readiness = "route_pending"
        else:
            current_readiness = "map_ready"
        return {
            "requiredGoalCoverage": verifier.get("requiredGoalCoverage") or {},
            "semanticCoverage": semantic_failure_count == 0 and blocking_required_pending_slot_count == 0,
            "mapIdentityCoverage": invalid_identity_count == 0,
            "flexibleNoveltyCoverage": verifier.get("materialNoveltyPassed", True),
            "routeCoverage": route_coverage_exact,
            "scheduleCoverage": not any("schedule_" in item for item in hard_failures),
            "budgetCoverage": budget_status in {"verified", "estimated", "partial"},
            "pendingSlotCount": pending_slot_count,
            "hardPendingSlotCount": hard_pending_slot_count,
            "softPendingSlotCount": soft_pending_slot_count,
            "pendingHardSlotCount": hard_pending_slot_count,
            "blockingPendingHardSlotCount": blocking_hard_pending_slot_count,
            "completionRequiredPendingSlotCount": completion_required_pending_slot_count,
            "blockingPendingCompletionSlotCount": completion_required_soft_slot_count,
            "blockingRequiredPendingSlotCount": blocking_required_pending_slot_count,
            "providerExhaustedRequiredSlotCount": provider_exhausted_required_slot_count,
            "pendingSoftSlotCount": soft_pending_slot_count,
            "structureReady": structure_ready,
            "draftAdoptionReady": draft_adoption_ready,
            "adoptionMode": adoption_mode,
            "hardFailures": hard_failures,
            "softWarnings": list(dict.fromkeys(soft_warnings)),
            "adoptionReady": adoption_ready,
            "confirmationPassed": bool(adoption_ready and backend_confirmation_passed),
            "strictlyVerified": strictly_verified,
            "blockingReasons": blocking,
            # Adoption readiness answers whether the proposal can be copied
            # into an editable timeline. Product completion is stricter: a
            # draft with pending soft slots remains PARTIAL even when the user
            # is allowed to adopt and continue editing it.
            "isPartial": not strictly_verified,
            "requiredPlanningDayNumbers": sorted(required_planning_day_numbers),
            "explicitRestDayNumbers": sorted(explicit_rest_day_numbers),
            "uncoveredDayNumbers": sorted(uncovered_day_numbers),
            "mealExperienceBriefs": [
                item.get("mealExperienceBrief")
                for item in meal_quality.get("mealSemanticEvidence") or []
                if isinstance(item, dict) and isinstance(item.get("mealExperienceBrief"), dict)
            ],
            "mealSemanticEvidence": meal_quality.get("mealSemanticEvidence") or [],
            "mealThemeSignature": meal_quality.get("mealThemeSignature") or [],
            "mealQualityPassed": meal_quality.get("mealQualityPassed") is True,
            "mealDiversityPassed": meal_quality.get("mealDiversityPassed") is True,
            "mealUnresolvedReasons": meal_quality.get("mealUnresolvedReasons") or [],
            "routeComfortEvidence": verifier.get("routeComfortEvidence") or {},
            "dailyPlanningCoverageContractInvalid": daily_coverage_contract_invalid,
            "dailyPlanningCoverageSourceInvalid": daily_coverage_source_invalid,
            "dailyAnchorTargetContractInvalid": daily_anchor_target_contract_invalid,
            "dailyTargetDayPartitionInvalid": daily_target_day_partition_invalid,
            "routeStatus": route_status,
            "routeRetryable": route_retryable,
            "routeProviderAttemptCount": route_provider_attempt_count,
            "routeProviderCacheHitCount": route_provider_cache_hit_count,
            "routePreconditionFailureReason": execution_ledger.get("routePreconditionFailureReason"),
            "routeEvidenceInvalidationReason": snapshot.get("routeEvidenceInvalidationReason"),
            "routeExpectedLegCount": len(expected_pairs),
            "routeVerifiedLegCount": len(verified_required),
            "routeUnexpectedLegCount": len(unexpected_verified_pairs),
            "routeErrorLegCount": len(expected_pairs & failed_pairs),
            **density_targets,
            "budgetStatus": budget_status,
            "budgetEstimate": budget_estimate,
            "budgetEvidenceCount": evidence_count,
            "unknownCostSegmentCount": unknown_cost_count,
            "comparisonRole": comparison_role,
            "originProjectionMode": origin_projection_mode,
            "currentReadiness": current_readiness,
            "promotionStatus": promotion_status,
            "nextAction": next_action,
            "legacyNextAction": legacy_next_action,
            "nextActionLabel": next_action_label,
            "normalizedRouteEvidence": normalized_routes,
            "partialAdoptionReady": draft_adoption_ready,
            **output_quality,
        }

    @classmethod
    def density_targets(cls, snapshot: dict[str, Any]) -> dict[str, dict[str, int]]:
        desired_raw = (
            snapshot.get("desiredDensityAnchorTargets")
            if isinstance(snapshot.get("desiredDensityAnchorTargets"), dict)
            else snapshot.get("portfolioDayAnchorTargets")
            if isinstance(snapshot.get("portfolioDayAnchorTargets"), dict)
            else {}
        )
        desired = {str(day): max(0, int(value or 0)) for day, value in desired_raw.items() if str(day)}
        grounded: dict[str, int] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_key = str(int(day.get("dayNumber") or 0))
            grounded[day_key] = sum(
                1
                for segment in day.get("segments") or []
                if isinstance(segment, dict) and cls._is_route_anchor_segment(segment)
            )
        pending_future: dict[str, int] = {day: 0 for day in set(desired) | set(grounded)}
        for slot in snapshot.get("portfolioPendingSlots") or []:
            if not isinstance(slot, dict) or not bool(slot.get("futureRouteAnchor") or slot.get("routeAnchorExpected")):
                continue
            day_key = str(int(slot.get("dayNumber") or 0))
            pending_future[day_key] = pending_future.get(day_key, 0) + 1
        for day_key in set(desired) | set(grounded) | set(pending_future):
            desired.setdefault(day_key, grounded.get(day_key, 0) + pending_future.get(day_key, 0))
            grounded.setdefault(day_key, 0)
            pending_future.setdefault(day_key, 0)
        return {
            "desiredDensityAnchorTargets": dict(sorted(desired.items())),
            "groundedRouteAnchorTargets": dict(sorted(grounded.items())),
            "pendingFutureAnchorTargets": dict(sorted(pending_future.items())),
        }

    @classmethod
    def with_density_targets(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        projected = dict(snapshot)
        projected.update(cls.density_targets(snapshot))
        return projected

    @staticmethod
    def _real_amap_poi(value: Any) -> bool:
        if not isinstance(value, dict) or str(value.get("source") or "") != "amap-place-search":
            return False
        amap_id = str(value.get("amapId") or "").strip().upper()
        if not re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id):
            return False
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and latitude != 0
            and longitude != 0
        )

    @classmethod
    def route_target_segments_by_day(cls, snapshot: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
        """Return the canonical materialized stops that affect physical routing.

        ``semanticMetadata.requiresRouteEdge`` is the authoritative physical
        route decision when present.  Legacy snapshots predate that field, so
        every materialized real AMap stop remains a route target regardless of
        density-only ``routeAnchor`` / ``routeAnchorExpected`` flags.
        """

        targets: dict[int, list[dict[str, Any]]] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            try:
                day_number = int(day.get("dayNumber") or 0)
            except (TypeError, ValueError):
                continue
            for segment in day.get("segments") or []:
                if isinstance(segment, dict) and cls._is_route_target_segment(segment):
                    targets.setdefault(day_number, []).append(segment)
        return targets

    @classmethod
    def expected_route_pairs(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Return chronologically adjacent, proposal-local route pairs.

        The same ordered derivation is used for readiness, retry preflight and
        trace emission.  Keeping it here avoids a second interpretation of
        which segments are route anchors while retaining RouteOption as the
        only route evidence contract.
        """

        pairs: list[dict[str, Any]] = []
        for day_number, segments in cls.route_target_segments_by_day(snapshot).items():
            anchors = [str(segment.get("id") or "").strip() for segment in segments]
            # A missing segment identity makes the whole day's physical
            # adjacency unverifiable.  Dropping only that stop would create a
            # fictitious shortcut between its neighbours, so fail closed and
            # emit no route pairs for the incomplete day.
            if any(not segment_id for segment_id in anchors):
                continue
            for from_segment_id, to_segment_id in zip(anchors, anchors[1:]):
                pairs.append(
                    {
                        "fromSegmentId": from_segment_id,
                        "toSegmentId": to_segment_id,
                        "dayNumber": day_number,
                    }
                )
        return pairs

    @classmethod
    def completion_required_pending_slots(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Return authoritative user or product completion occurrences still pending.

        This is deliberately narrower than ``explicit_soft``. A soft slot is
        completion-blocking only when its sealed lineage proves either an
        explicit every-day user occurrence or the server-owned daily-completion
        occurrence for that exact day.
        """

        return [
            item
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and cls._is_completion_required_pending_slot(item)
        ]

    @staticmethod
    def _is_completion_required_pending_slot(slot: dict[str, Any]) -> bool:
        try:
            day_number = int(slot.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return False
        raw_allowed_days = slot.get("allowedDayNumbers")
        if not isinstance(raw_allowed_days, list):
            return False
        try:
            allowed_days = {int(value) for value in raw_allowed_days if not isinstance(value, bool) and int(value) > 0}
        except (TypeError, ValueError):
            return False
        goal_id = str(slot.get("goalId") or "").strip()
        source_goal_id = str(slot.get("sourceGoalId") or "").strip()
        occurrence_id = str(slot.get("occurrenceId") or "").strip()
        planning_slot_id = str(slot.get("planningSlotId") or slot.get("slotId") or "").strip()
        pool_id = str(slot.get("poolId") or "").strip()
        common_lineage_valid = bool(
            slot.get("simpleDirectionRequirementLineageConflict") is not True
            and goal_id
            and goal_id == source_goal_id
            and occurrence_id == f"occ:{source_goal_id}:day:{day_number}"
            and planning_slot_id
            and pool_id
            and day_number in allowed_days
        )
        if not common_lineage_valid:
            return False

        lineage_authority = str(slot.get("lineageAuthority") or "")
        explicit_every_day = bool(
            slot.get("userExplicit") is True
            and str(slot.get("distributionPolicy") or "") == "every_allowed_day"
            and str(slot.get("cardinalitySource") or "") == "explicit_every_day"
            and lineage_authority
            in {
                "goal_occurrence_compiler",
                "simple_open_request_contract_every_day_meal",
            }
        )
        daily_completion = bool(
            slot.get("dayCompletionRequired") is True
            and slot.get("completionRequired") is not True
            and slot.get("userExplicit") is False
            and lineage_authority == "simple_open_daily_completion_policy"
            and str(slot.get("requirementLevel") or "") == "inferred_preferred"
            and goal_id == f"goal_daily_completion_day_{day_number}"
            and planning_slot_id == f"day{day_number}_daily_completion_1"
            and pool_id == f"daily_completion_day_{day_number}_pool"
            and allowed_days == {day_number}
        )
        return explicit_every_day or daily_completion

    @classmethod
    def _is_route_anchor_segment(cls, segment: dict[str, Any]) -> bool:
        if not cls._is_materialized_route_segment(segment):
            return False
        if not str(segment.get("id") or "").strip():
            return False
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        if "routeAnchor" in semantic:
            return semantic.get("routeAnchor") is True
        if "routeAnchorExpected" in semantic:
            return semantic.get("routeAnchorExpected") is True
        return bool(
            cls._real_amap_poi(segment.get("poi"))
            and bool(segment.get("startTime") or segment.get("timeWindow"))
            and str(segment.get("kind") or "") not in {"pending", "placeholder"}
        )

    @classmethod
    def _is_route_target_segment(cls, segment: dict[str, Any]) -> bool:
        """Return whether a materialized stop belongs in route feasibility.

        ``routeAnchor`` remains the density/creative-anchor contract.  A real
        stop such as a selected restaurant still changes the physical route
        even when it is not a density anchor.  New producers express the
        physical decision with ``requiresRouteEdge``; legacy density flags do
        not exclude materialized AMap stops.  Pending metadata never becomes a
        route target.
        """

        if not cls._is_materialized_route_segment(segment):
            return False
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        if "requiresRouteEdge" in semantic:
            return semantic.get("requiresRouteEdge") is True
        return True

    @classmethod
    def _is_materialized_route_segment(cls, segment: dict[str, Any]) -> bool:
        return bool(
            str(segment.get("kind") or "") not in {"pending", "placeholder"} and cls._real_amap_poi(segment.get("poi"))
        )

    @staticmethod
    def _declares_route_target_segment(segment: dict[str, Any]) -> bool:
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        if "requiresRouteEdge" in semantic:
            return semantic.get("requiresRouteEdge") is True
        if "routeAnchorExpected" in semantic:
            return semantic.get("routeAnchorExpected") is True
        return semantic.get("routeAnchor") is True

    @classmethod
    def _verified_route(cls, route: dict[str, Any], segment_days: dict[str, int]) -> bool:
        return ProposalRouteEvidenceNormalizer.is_verified(route, segment_days)

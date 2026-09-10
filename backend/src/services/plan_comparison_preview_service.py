"""Safe, read-only projection of Portfolio proposals and active partial timelines."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Optional

from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.simple_open_dynamic_schedule_service import SimpleOpenDynamicScheduleService


class PlanComparisonPreviewService:
    _COLOR_KEYS = ("ocean", "amber", "violet", "teal", "rose", "indigo", "lime", "slate")
    # A comparison card must change the physical trip, not only its narrative.
    # Jaccard distance 0.40 is the minimum pairwise separation for admitted
    # AMap anchors; two-day trips additionally need two new anchors or a
    # two-position day-sequence change.
    MATERIAL_NOVELTY_THRESHOLD = 0.40

    @classmethod
    def project_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        planning_selection_root_turn_id: str,
        root_portfolio_id: str,
        proposal_id: str,
        source_assistant_turn_id: str,
        choice_id: str,
        active_version_id: Optional[str],
        expected_base_version_id: Optional[str],
        is_partial: bool,
        is_adopted: bool,
        adoption_ready: bool = True,
        comparison_role: str | None = None,
        origin_projection_mode: str | None = None,
        simple_direction_scope: bool = False,
        route_failures_non_blocking: bool = False,
    ) -> dict[str, Any]:
        days: list[dict[str, Any]] = []
        segment_days: dict[str, int] = {}
        focus_brief_id = cls._focus_brief_id(snapshot)
        for raw_day in snapshot.get("days") or []:
            if not isinstance(raw_day, dict):
                continue
            segments: list[dict[str, Any]] = []
            for raw_segment in raw_day.get("segments") or []:
                if (
                    not isinstance(raw_segment, dict)
                    or (
                        not simple_direction_scope
                        and not cls._segment_in_scope(raw_segment, focus_brief_id)
                    )
                    or not cls._real_amap_poi(raw_segment.get("poi"))
                ):
                    continue
                segment = copy.deepcopy(raw_segment)
                segments.append(segment)
                segment_id = str(segment.get("id") or "")
                if segment_id:
                    segment_days[segment_id] = int(raw_day.get("dayNumber") or 0)
            if simple_direction_scope:
                segments = cls._ordered_simple_direction_segments(segments)
            days.append(
                {
                    "id": raw_day.get("id"),
                    "dayNumber": int(raw_day.get("dayNumber") or 0),
                    "title": raw_day.get("title") or f"Day {raw_day.get('dayNumber') or '?'}",
                    "date": raw_day.get("date"),
                    "segments": segments,
                }
            )
        comparison_role = comparison_role or ("current_active_draft" if active_version_id else "candidate_proposal")
        origin_projection_mode = origin_projection_mode or ("partial_preview" if is_partial else "full_proposal")
        normalized_routes = ProposalRouteEvidenceNormalizer.normalize_snapshot(
            snapshot,
            focus_brief_id=focus_brief_id,
        )
        routes = [
            copy.deepcopy(route)
            for route in normalized_routes
            if ProposalRouteEvidenceNormalizer.is_verified(route, segment_days)
        ]
        pending_slots = [
            cls._pending_slot(slot)
            for slot in snapshot.get("portfolioPendingSlots") or []
            if (
                cls._simple_pending_slot_in_scope(
                    slot,
                    {int(day.get("dayNumber") or 0) for day in days},
                )
                if simple_direction_scope
                else cls._pending_slot_in_scope(
                    slot,
                    focus_brief_id,
                    {int(day.get("dayNumber") or 0) for day in days},
                )
            )
        ]
        pending_slots.sort(key=cls._pending_sort_key)
        days_by_number = {int(day.get("dayNumber") or 0): day for day in days}
        for slot in pending_slots:
            day_number = int(slot.get("dayNumber") or 0)
            day = days_by_number.get(day_number)
            if day is not None:
                day.setdefault("pendingSlots", []).append(copy.deepcopy(slot))
        days.sort(key=lambda day: int(day.get("dayNumber") or 0))
        distance = sum(cls._route_distance_km(item) for item in routes)
        duration = sum(int(item.get("durationMinutes") or 0) for item in routes)
        readiness = ProposalReadinessService.compute(
            {
                **snapshot,
                "days": days,
                # Readiness must see failed/refreshable legs too; only verified
                # routes are returned below for actual map rendering.
                "routeOptions": normalized_routes,
                "portfolioRouteEvidence": [],
                "routeEvidence": [],
                "portfolioPendingSlots": pending_slots,
                "comparisonRole": comparison_role,
                "originProjectionMode": origin_projection_mode,
            },
            verifier=(
                snapshot.get("portfolioVerifier")
                if isinstance(snapshot.get("portfolioVerifier"), dict)
                else {"passed": bool(adoption_ready)}
            ),
            route_failures_non_blocking=route_failures_non_blocking,
        )
        budget = readiness["budgetEstimate"]
        tier = str(snapshot.get("budgetTier") or "unknown")
        tier_label = cls._budget_tier_label(tier)
        if readiness["budgetStatus"] == "pending":
            budget_summary = f"{tier_label} · 预算待核验"
        elif readiness["budgetStatus"] == "partial":
            budget_summary = (
                f"{tier_label} · 已估 ¥{float(budget or 0):.0f}，{readiness['unknownCostSegmentCount']} 项待核验"
            )
        else:
            budget_summary = f"{tier_label} · 预计 ¥{float(budget or 0):.0f}"
        route_summary = {
            "route_not_required": "路线无需核验",
            "route_pending": "路线待核验",
            "route_verifying": "路线核验中",
            "route_partial": "路线部分核验",
            "route_provider_failed": "地图路线服务暂不可用",
        }.get(
            readiness["routeStatus"],
            f"已验证路线 {len(routes)} 段 · {distance:.1f} km · {duration} 分钟",
        )
        pending_route_slots = [
            item for item in pending_slots if bool(item.get("futureRouteAnchor") or item.get("routeAnchorExpected"))
        ]
        if readiness["routeStatus"] == "route_ready" and pending_route_slots:
            meal_pending = sum(
                1
                for item in pending_route_slots
                if str(item.get("intentType") or item.get("kind") or "") in {"meal", "local_food", "food"}
            )
            route_summary = f"主路线已核验 {readiness['routeVerifiedLegCount']} 段；" + (
                f"餐饮插入后仍有 {meal_pending} 段待核验"
                if meal_pending
                else f"补入待选体验后仍有 {len(pending_route_slots)} 段待核验"
            )
        route_assignment_evidence = (
            copy.deepcopy(snapshot.get("simpleOpenRouteAssignment"))
            if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict)
            else {}
        )
        detour_compliance = str(route_assignment_evidence.get("detourCompliance") or "pending")
        if detour_compliance not in {"verified", "pending", "exceeded"}:
            detour_compliance = "pending"
        completion_action = copy.deepcopy(readiness.get("completionAction"))
        if isinstance(completion_action, dict):
            completion_action["choiceId"] = f"portfolio_theme_completion_{proposal_id}"
        simple_agent_title_ready = bool(
            simple_direction_scope
            and CreativeProposalTitleService.is_valid_agent_projection(
                snapshot,
                snapshot.get("portfolioTitleEvidence"),
            )
        )
        simple_server_title = (
            CreativeProposalTitleService.sealed_server_fallback_title(snapshot)
            if simple_direction_scope
            else ""
        )
        simple_fallback_title = CreativeProposalTitleService.server_fallback_title(
            {
                **snapshot,
                "days": days,
                "portfolioPendingSlots": pending_slots,
            }
        )["title"]
        if simple_agent_title_ready:
            display_title = str(snapshot.get("title") or "").strip()
        elif simple_server_title:
            display_title = simple_server_title
        elif simple_direction_scope:
            display_title = simple_fallback_title
        else:
            display_title = str(readiness.get("displayTitle") or snapshot.get("title") or "行程方案")
        material_fingerprint = cls.material_fingerprint(snapshot)
        repair_choice_id = f"simple_direction_repair_{hashlib.sha256(f'{root_portfolio_id}:{proposal_id}:{material_fingerprint}'.encode('utf-8')).hexdigest()[:24]}"
        return {
            "planningSelectionRootTurnId": planning_selection_root_turn_id,
            "rootPortfolioId": root_portfolio_id,
            "proposalId": proposal_id,
            "sourceAssistantTurnId": source_assistant_turn_id,
            "choiceId": choice_id,
            "materialFingerprint": material_fingerprint,
            "repairChoiceId": repair_choice_id,
            "status": "partial" if readiness["isPartial"] else "complete",
            "isPartial": readiness["isPartial"],
            "isAdopted": bool(is_adopted and active_version_id),
            "adoptionReady": readiness["adoptionReady"],
            "confirmationPassed": readiness["confirmationPassed"],
            "draftAdoptionReady": readiness["draftAdoptionReady"],
            "partialAdoptionReady": readiness["partialAdoptionReady"],
            "strictlyVerified": readiness["strictlyVerified"],
            "structureReady": readiness["structureReady"],
            "pendingHardSlotCount": readiness["pendingHardSlotCount"],
            "blockingPendingHardSlotCount": readiness.get("blockingPendingHardSlotCount", readiness["pendingHardSlotCount"]),
            "providerExhaustedRequiredSlotCount": readiness.get("providerExhaustedRequiredSlotCount", 0),
            "pendingSoftSlotCount": readiness["pendingSoftSlotCount"],
            "adoptionMode": readiness["adoptionMode"],
            "activeVersionId": active_version_id,
            "expectedBaseVersionId": expected_base_version_id,
            "title": display_title,
            "displayTitle": display_title,
            "themeEligible": readiness.get("themeEligible"),
            "visibilityMode": readiness.get("visibilityMode"),
            "completionAction": completion_action,
            "pendingRatio": readiness.get("pendingRatio"),
            "desiredDensityAnchorTargets": readiness.get("desiredDensityAnchorTargets") or {},
            "groundedRouteAnchorTargets": readiness.get("groundedRouteAnchorTargets") or {},
            "pendingFutureAnchorTargets": readiness.get("pendingFutureAnchorTargets") or {},
            "requiredPlanningDayNumbers": readiness.get("requiredPlanningDayNumbers") or [],
            "explicitRestDayNumbers": readiness.get("explicitRestDayNumbers") or [],
            "uncoveredDayNumbers": readiness.get("uncoveredDayNumbers") or [],
            "mealExperienceBriefs": copy.deepcopy(readiness.get("mealExperienceBriefs") or []),
            "mealSemanticEvidence": copy.deepcopy(readiness.get("mealSemanticEvidence") or []),
            "mealThemeSignature": list(readiness.get("mealThemeSignature") or []),
            "mealQualityPassed": readiness.get("mealQualityPassed"),
            "mealDiversityPassed": readiness.get("mealDiversityPassed"),
            "mealUnresolvedReasons": list(readiness.get("mealUnresolvedReasons") or []),
            "routeComfortEvidence": copy.deepcopy(readiness.get("routeComfortEvidence") or {}),
            "days": days,
            "pendingSlots": pending_slots,
            "routeEvidence": routes,
            "budgetSummary": budget_summary,
            "budgetTier": tier,
            "budgetTierLabel": tier_label,
            "budgetStatus": readiness["budgetStatus"],
            "budgetEvidenceCount": readiness["budgetEvidenceCount"],
            "unknownCostSegmentCount": readiness["unknownCostSegmentCount"],
            "routeSummary": route_summary,
            "routeStatus": readiness["routeStatus"],
            "routeExpectedLegCount": readiness["routeExpectedLegCount"],
            "routeVerifiedLegCount": readiness["routeVerifiedLegCount"],
            "routeErrorLegCount": readiness["routeErrorLegCount"],
            "routeRetryable": readiness["routeRetryable"],
            "routeProviderAttemptCount": readiness["routeProviderAttemptCount"],
            "routeProviderCacheHitCount": readiness["routeProviderCacheHitCount"],
            "routePreconditionFailureReason": readiness.get("routePreconditionFailureReason"),
            "routeEvidenceInvalidationReason": readiness.get("routeEvidenceInvalidationReason"),
            "detourCompliance": detour_compliance,
            "routeAssignmentEvidence": route_assignment_evidence,
            "blockingReasons": readiness["blockingReasons"],
            "blockingReasonLabels": [cls._blocking_reason_label(item) for item in readiness["blockingReasons"]],
            "softWarnings": readiness["softWarnings"],
            "comparisonRole": readiness["comparisonRole"],
            "originProjectionMode": readiness["originProjectionMode"],
            "currentReadiness": readiness["currentReadiness"],
            "promotionStatus": readiness["promotionStatus"],
            "nextAction": readiness["nextAction"],
            "nextActionLabel": readiness["nextActionLabel"],
            "tradeoffSummary": cls._difference_summary(days, pending_slots, snapshot),
            "colorKey": cls.color_key(proposal_id),
        }

    @staticmethod
    def _business_material_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Exclude presentation-only title state from opaque material identity."""

        material = copy.deepcopy(snapshot)
        for key in ("title", "displayTitle", "portfolioTitleEvidence", "portfolioTitleGeneration"):
            material.pop(key, None)
        creative_brief = material.get("creativeBrief")
        if isinstance(creative_brief, dict):
            creative_brief.pop("title", None)
        output_quality = material.get("portfolioOutputQuality")
        if isinstance(output_quality, dict):
            output_quality.pop("displayTitle", None)
            output_quality.pop("originalCreativeTitle", None)
        return material

    @staticmethod
    def _ordered_simple_direction_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Repair stale provider slot order in both new and persisted previews."""

        def metadata(segment: dict[str, Any]) -> dict[str, Any]:
            value = segment.get("semanticMetadata")
            return value if isinstance(value, dict) else {}

        def preference(segment: dict[str, Any]) -> dict[str, Any]:
            value = metadata(segment).get("schedulePreference")
            return value if isinstance(value, dict) else {}

        controller_sequence_authoritative = any(
            str(preference(segment).get("sequenceSource") or "") == "controller_schedule_hint"
            for segment in segments
        )
        return sorted(
            segments,
            key=lambda segment: SimpleOpenDynamicScheduleService.semantic_order_key(
                preference(segment),
                segment.get("startTime"),
                tie_breaker=str(
                    metadata(segment).get("planningSlotId") or segment.get("id") or ""
                ),
                controller_sequence_authoritative=controller_sequence_authoritative,
                explicit_start_time=(
                    metadata(segment).get("scheduleConstraints") or {}
                ).get("explicitStartTime")
                if isinstance(metadata(segment).get("scheduleConstraints"), dict)
                else None,
            ),
        )

    @classmethod
    def material_fingerprint(cls, snapshot: dict[str, Any]) -> str:
        encoded = json.dumps(
            cls._business_material_snapshot(snapshot),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _budget_tier_label(tier: str) -> str:
        return {
            "low": "低预算",
            "medium": "中等预算",
            "high": "较高预算",
        }.get(str(tier or "").casefold(), "预算档位待确认")

    @staticmethod
    def _blocking_reason_label(reason: str) -> str:
        value = str(reason or "")
        match = re.fullmatch(r"route_anchor_target_mismatch:day_(\d+):(\d+)/(\d+)", value)
        if match:
            return f"第 {match.group(1)} 天计划 {match.group(3)} 个地点，已确认 {match.group(2)} 个"
        match = re.fullmatch(r"required_goal_count_insufficient:([^:]+):(\d+)/(\d+)", value)
        if match:
            goal_label = "夜景必选地点" if match.group(1) == "goal_night_view" else "必选地点"
            return f"{goal_label}需要 {match.group(3)} 个，当前确认 {match.group(2)} 个"
        match = re.fullmatch(r"required_goal_omitted:([^:]+)", value)
        if match:
            return "夜景必选目标尚未加入" if match.group(1) == "goal_night_view" else "必选目标尚未加入"
        return {
            "theme_optional_family_missing": "该方案的主题体验尚未补齐",
            "portfolio_route_quality:route_evidence_missing": "仍缺与当前停靠顺序一致的路线核验",
            "route_evidence_incomplete": "仍缺与当前停靠顺序一致的路线核验",
            "provider_route_matrix_incomplete": "高德未返回完整的相邻路线证据",
            "adjacent_leg_limit_exceeded": "相邻地点的公共交通时间超过 45 分钟",
            "fixed_poi_route_constraint_conflict": "明确指定的地点与当前路线约束冲突",
            "topology_constraint_exceeded": "日内停靠顺序回折超过当前偏好",
            "pending_slots_remaining": "仍有待补时段",
            "map_identity_incomplete": "部分地点待地图确认",
            "semantic_coverage_failed": "地点用途尚未通过核验",
            "proposal_verifier_not_passed": "方案采用条件尚未满足",
            "proposal_title_generation_pending": "方案标题生成失败，可重试后再采用",
        }.get(value, "方案条件待核验")

    @classmethod
    def _difference_summary(
        cls,
        days: list[dict[str, Any]],
        pending_slots: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> str:
        required_names: list[str] = []
        flexible_names: list[str] = []
        families: list[str] = []
        for day in days:
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata")
                semantic = semantic if isinstance(semantic, dict) else {}
                poi = segment.get("poi")
                poi = poi if isinstance(poi, dict) else {}
                name = str(poi.get("name") or segment.get("title") or "").strip()
                family = str(semantic.get("optionalExperienceFamily") or "").strip()
                requirement = str(semantic.get("requirementLevel") or "").lower()
                if family:
                    if name and name not in flexible_names:
                        flexible_names.append(name)
                    if family not in families:
                        families.append(family)
                elif requirement in {"hard", "required"} and name and name not in required_names:
                    required_names.append(name)
        parts: list[str] = []
        if required_names:
            parts.append(f"共享必选：{'、'.join(required_names[:3])}")
        if flexible_names:
            parts.append(f"本方案新增：{'、'.join(flexible_names[:3])}")
        if families:
            parts.append(f"侧重：{' + '.join(families[:3])}")
        if pending_slots:
            parts.append(f"仍有 {len(pending_slots)} 个时段待补")
        if not parts:
            fallback = str(
                snapshot.get("decisionRationale") or snapshot.get("portfolioBriefTitle") or "方案差异证据待核验"
            ).strip()
            parts.append(fallback)
        return "；".join(parts)

    @classmethod
    def color_key(cls, identity: str) -> str:
        index = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16) % len(cls._COLOR_KEYS)
        return cls._COLOR_KEYS[index]

    @classmethod
    def material_novelty_audit(
        cls,
        candidate: dict[str, Any],
        prior_projections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Prove that a new comparison card changes the physical trip.

        Titles, creative axes, family labels, and duplicate occurrences are not
        user-visible evidence of a different itinerary. The audit compares
        canonical AMap identities. When Creative Portfolio has explicit theme
        anchors, novelty is measured on those flexible anchors so shared hard
        goals do not dilute one real theme change into a false duplicate.
        """

        candidate_ids = cls.physical_poi_ids(candidate)
        candidate_flexible_ids = cls.flexible_physical_poi_ids(candidate)
        if not candidate_ids:
            return {
                "passed": False,
                "noveltyMode": "physical",
                "threshold": cls.MATERIAL_NOVELTY_THRESHOLD,
                "minimumDistance": 0.0,
                "minimumNewPoiFraction": 0.0,
                "minimumNewPoiCount": 0,
                "candidatePhysicalPoiCount": 0,
                "priorProjectionCount": 0,
                "nearestProposalId": None,
                "minimumDistanceProposalId": None,
                "minimumNewPoiFractionProposalId": None,
                "overlapPoiIds": [],
                "perReferenceComparisons": [],
                "reasonCode": "candidate_physical_identity_missing",
            }

        comparisons: list[dict[str, Any]] = []
        candidate_proposal_id = str(candidate.get("proposalId") or "")
        for prior in prior_projections:
            if not isinstance(prior, dict):
                continue
            prior_proposal_id = str(prior.get("proposalId") or "")
            if candidate_proposal_id and prior_proposal_id == candidate_proposal_id:
                continue
            prior_ids = cls.physical_poi_ids(prior)
            if not prior_ids:
                continue
            prior_flexible_ids = cls.flexible_physical_poi_ids(prior)
            use_flexible_scope = bool(candidate_flexible_ids)
            scoped_candidate_ids = candidate_flexible_ids if use_flexible_scope else candidate_ids
            scoped_prior_ids = prior_flexible_ids if use_flexible_scope else prior_ids
            union = scoped_candidate_ids | scoped_prior_ids
            overlap = scoped_candidate_ids & scoped_prior_ids
            new_ids = scoped_candidate_ids - scoped_prior_ids
            distance = round(1.0 - len(overlap) / len(union), 4)
            new_poi_fraction = round(len(new_ids) / len(scoped_candidate_ids), 4)
            candidate_sequences = cls._physical_poi_sequences(candidate, flexible_only=use_flexible_scope)
            prior_sequences = cls._physical_poi_sequences(prior, flexible_only=use_flexible_scope)
            maximum_day_sequence_difference = max(
                (
                    cls._sequence_difference(
                        candidate_sequences.get(day_number, []),
                        prior_sequences.get(day_number, []),
                    )
                    for day_number in set(candidate_sequences) | set(prior_sequences)
                ),
                default=0,
            )
            is_multi_day = len(candidate_sequences) >= 2
            structure_passed = bool(not is_multi_day or len(new_ids) >= 2 or maximum_day_sequence_difference >= 2)
            comparisons.append(
                {
                    "proposalId": prior_proposal_id or None,
                    "distance": distance,
                    "newPoiFraction": new_poi_fraction,
                    "newPoiCount": len(new_ids),
                    "maximumDaySequenceDifference": maximum_day_sequence_difference,
                    "multiDayStructurePassed": structure_passed,
                    "overlapPoiIds": sorted(overlap),
                    "noveltyScope": "flexible_theme_anchors" if use_flexible_scope else "all_anchors",
                }
            )

        if not comparisons:
            return {
                "passed": True,
                "noveltyMode": "physical",
                "threshold": cls.MATERIAL_NOVELTY_THRESHOLD,
                "minimumDistance": 1.0,
                "minimumNewPoiFraction": 1.0,
                "minimumNewPoiCount": len(candidate_ids),
                "candidatePhysicalPoiCount": len(candidate_ids),
                "priorProjectionCount": 0,
                "nearestProposalId": None,
                "minimumDistanceProposalId": None,
                "minimumNewPoiFractionProposalId": None,
                "overlapPoiIds": [],
                "perReferenceComparisons": [],
                "reasonCode": "no_prior_physical_projection",
            }

        nearest = min(
            comparisons,
            key=lambda item: (
                float(item["newPoiFraction"]),
                float(item["distance"]),
                str(item.get("proposalId") or ""),
            ),
        )
        minimum_distance_reference = min(
            comparisons,
            key=lambda item: (float(item["distance"]), str(item.get("proposalId") or "")),
        )
        minimum_new_poi_reference = min(
            comparisons,
            key=lambda item: (
                float(item["newPoiFraction"]),
                str(item.get("proposalId") or ""),
            ),
        )
        minimum_distance = float(minimum_distance_reference["distance"])
        minimum_new_poi_fraction = float(minimum_new_poi_reference["newPoiFraction"])
        minimum_new_poi_count = min(int(item["newPoiCount"]) for item in comparisons)
        minimum_sequence_difference = min(int(item.get("maximumDaySequenceDifference") or 0) for item in comparisons)
        passed = bool(
            minimum_distance >= cls.MATERIAL_NOVELTY_THRESHOLD
            and minimum_new_poi_count >= 1
            and all(bool(item.get("multiDayStructurePassed")) for item in comparisons)
        )
        return {
            "passed": passed,
            "noveltyMode": "physical",
            "threshold": cls.MATERIAL_NOVELTY_THRESHOLD,
            "minimumDistance": minimum_distance,
            "minimumNewPoiFraction": minimum_new_poi_fraction,
            "minimumNewPoiCount": minimum_new_poi_count,
            "minimumDaySequenceDifference": minimum_sequence_difference,
            "candidatePhysicalPoiCount": len(candidate_ids),
            "candidateFlexiblePoiCount": len(candidate_flexible_ids),
            "priorProjectionCount": len(comparisons),
            "nearestProposalId": nearest.get("proposalId"),
            "minimumDistanceProposalId": minimum_distance_reference.get("proposalId"),
            "minimumNewPoiFractionProposalId": minimum_new_poi_reference.get("proposalId"),
            "overlapPoiIds": list(nearest.get("overlapPoiIds") or []),
            "perReferenceComparisons": comparisons,
            "reasonCode": (
                "materially_distinct_physical_projection" if passed else "partial_preview_not_materially_distinct"
            ),
        }

    @classmethod
    def concept_novelty_audit(
        cls,
        candidate: dict[str, Any],
        prior_projections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compare identity-free partial structures, never titles or family labels alone."""
        candidate_signature, candidate_facts = cls.concept_signature(candidate)
        comparisons: list[dict[str, Any]] = []
        candidate_id = str(candidate.get("proposalId") or "")
        for prior in prior_projections:
            if not isinstance(prior, dict) or (candidate_id and str(prior.get("proposalId") or "") == candidate_id):
                continue
            prior_signature, prior_facts = cls.concept_signature(prior)
            if not prior_facts:
                continue
            comparisons.append(
                {
                    "proposalId": str(prior.get("proposalId") or "") or None,
                    "sameConcept": prior_signature == candidate_signature,
                    "sharedFactCount": len(set(candidate_facts) & set(prior_facts)),
                    "candidateFactCount": len(candidate_facts),
                }
            )
        passed = bool(candidate_facts) and not any(item["sameConcept"] for item in comparisons)
        return {
            "passed": passed,
            "noveltyMode": "concept",
            "conceptSignature": candidate_signature,
            "conceptFactCount": len(candidate_facts),
            "perReferenceComparisons": comparisons,
            "reasonCode": (
                "no_prior_concept_projection"
                if passed and not comparisons
                else "materially_distinct_concept_projection"
                if passed
                else "partial_preview_not_materially_distinct"
                if candidate_facts
                else "candidate_concept_structure_missing"
            ),
        }

    @classmethod
    def concept_signature(cls, projection: dict[str, Any]) -> tuple[str, list[str]]:
        facts: list[str] = []
        for day in projection.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                facts.append(cls._concept_fact(day_number, segment, semantic))
        for slot in projection.get("pendingSlots") or projection.get("portfolioPendingSlots") or []:
            if not isinstance(slot, dict):
                continue
            facts.append(cls._concept_fact(int(slot.get("dayNumber") or 0), slot, slot))
        normalized = sorted(item for item in facts if item)
        payload = "\n".join(normalized)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest(), normalized

    @staticmethod
    def _concept_fact(day_number: int, value: dict[str, Any], semantic: dict[str, Any]) -> str:
        desired = sorted(str(item) for item in semantic.get("desiredSignals") or [] if str(item))
        avoid = sorted(str(item) for item in semantic.get("avoidSignals") or [] if str(item))
        return "|".join(
            [
                str(day_number),
                str(semantic.get("dayRole") or ""),
                str(value.get("startTime") or value.get("timeWindow") or semantic.get("timeWindow") or ""),
                str(semantic.get("experienceShape") or "single_poi"),
                ",".join(desired),
                ",".join(avoid),
            ]
        )

    @classmethod
    def physical_poi_ids(cls, projection: dict[str, Any]) -> set[str]:
        identities: set[str] = set()
        for day in projection.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi")
                if cls._real_amap_poi(poi):
                    identities.add(PoiPhysicalIdentityService.canonical_amap_id(poi))
        return identities

    @classmethod
    def flexible_physical_poi_ids(cls, projection: dict[str, Any]) -> set[str]:
        identities: set[str] = set()
        for day in projection.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                if not (
                    semantic.get("portfolioOptional") is True
                    or str(semantic.get("optionalExperienceFamily") or "").strip()
                ):
                    continue
                poi = segment.get("poi")
                if cls._real_amap_poi(poi):
                    identities.add(PoiPhysicalIdentityService.canonical_amap_id(poi))
        return identities

    @classmethod
    def _physical_poi_sequences(
        cls,
        projection: dict[str, Any],
        *,
        flexible_only: bool,
    ) -> dict[int, list[str]]:
        sequences: dict[int, list[str]] = {}
        for day in projection.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            if day_number <= 0:
                continue
            values: list[str] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                if flexible_only and not (
                    semantic.get("portfolioOptional") is True
                    or str(semantic.get("optionalExperienceFamily") or "").strip()
                ):
                    continue
                poi = segment.get("poi")
                if cls._real_amap_poi(poi):
                    values.append(PoiPhysicalIdentityService.canonical_amap_id(poi))
            if values:
                sequences[day_number] = values
        return sequences

    @staticmethod
    def _sequence_difference(left: list[str], right: list[str]) -> int:
        common_length = min(len(left), len(right))
        return abs(len(left) - len(right)) + sum(1 for index in range(common_length) if left[index] != right[index])

    @staticmethod
    def _real_amap_poi(value: Any) -> bool:
        if not isinstance(value, dict) or str(value.get("source") or "") != "amap-place-search":
            return False
        amap_id = str(value.get("amapId") or "").strip()
        if not re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id.upper()):
            return False
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and latitude != 0
            and longitude != 0
        )

    @staticmethod
    def _verified_route(route: Any, segment_days: dict[str, int]) -> bool:
        return ProposalRouteEvidenceNormalizer.is_verified(route, segment_days)

    @staticmethod
    def _route_distance_km(route: dict[str, Any]) -> float:
        try:
            distance_km = float(route.get("distanceKm") or 0)
            distance_meters = float(route.get("distanceMeters") or 0)
        except (TypeError, ValueError):
            return 0
        if distance_km > 0:
            return distance_km
        return distance_meters / 1000

    @staticmethod
    def _focus_brief_id(snapshot: dict[str, Any]) -> str:
        context = snapshot.get("portfolioSelectionContext")
        if isinstance(context, dict):
            value = str(context.get("focusBriefId") or "").strip()
            if value:
                return value
        brief = snapshot.get("creativeBrief")
        return str((brief or {}).get("briefId") or "").strip() if isinstance(brief, dict) else ""

    @staticmethod
    def _segment_in_scope(segment: dict[str, Any], focus_brief_id: str) -> bool:
        metadata = segment.get("semanticMetadata")
        if not focus_brief_id:
            return False
        if not isinstance(metadata, dict):
            return False
        segment_brief = str(metadata.get("creativeBriefId") or metadata.get("briefId") or "").strip()
        return segment_brief == focus_brief_id

    @staticmethod
    def _pending_slot_in_scope(slot: Any, focus_brief_id: str, day_numbers: set[int]) -> bool:
        if not isinstance(slot, dict):
            return False
        brief_id = str(slot.get("briefId") or "").strip()
        pool_id = str(slot.get("poolId") or "").strip()
        slot_id = str(slot.get("planningSlotId") or slot.get("slotId") or "").strip()
        try:
            day_number = int(slot.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            focus_brief_id
            and brief_id
            and pool_id
            and slot_id
            and day_number in day_numbers
            and brief_id == focus_brief_id
        )

    @staticmethod
    def _simple_pending_slot_in_scope(slot: Any, day_numbers: set[int]) -> bool:
        if not isinstance(slot, dict):
            return False
        slot_id = str(slot.get("planningSlotId") or slot.get("slotId") or "").strip()
        try:
            day_number = int(slot.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return False
        return bool(slot_id and day_number in day_numbers)

    @staticmethod
    def _pending_slot(slot: dict[str, Any]) -> dict[str, Any]:
        time_window = str(slot.get("timeWindow") or "").strip()
        start_time = str(slot.get("startTime") or "").strip() or None
        end_time = str(slot.get("endTime") or "").strip() or None
        base_time_label = time_window or (
            f"{start_time}-{end_time}" if start_time and end_time else start_time or "时间待定"
        )
        timing_status = str(slot.get("timingStatus") or "").strip()
        if timing_status == "awaiting_route_confirmation":
            time_label = f"可安排时段 {base_time_label}（选点和交通方式确认后自动重排）"
        elif timing_status == "schedule_conflict_pending":
            time_label = "排程待重算（选点和交通方式确认后自动重排）"
        elif timing_status == "time_pending":
            time_label = "时间待定（选点和交通方式确认后自动重排）"
        else:
            time_label = base_time_label
        return {
            "id": f"pending:{slot.get('planningSlotId') or slot.get('slotId') or ''}",
            "briefId": slot.get("briefId"),
            "poolId": slot.get("poolId"),
            "planningSlotId": str(slot.get("planningSlotId") or slot.get("slotId") or ""),
            "dayNumber": int(slot.get("dayNumber") or 0),
            "timeWindow": time_window or None,
            "startTime": start_time,
            "endTime": end_time,
            "timeLabel": time_label,
            "displayNeed": str(slot.get("displayNeed") or slot.get("rawNeed") or slot.get("intentType") or "待补地点"),
            "rawNeed": str(slot.get("rawNeed") or slot.get("displayNeed") or slot.get("intentType") or "待补地点"),
            "intentType": str(slot.get("intentType") or ""),
            "kind": str(slot.get("kind") or "visit"),
            "state": "pending",
            "label": str(slot.get("label") or f"待补：{slot.get('displayNeed') or slot.get('rawNeed') or '地点'}"),
            "durationMinutes": int(slot.get("durationMinutes") or 0) or None,
            "timingStatus": timing_status or None,
            "timingBasis": str(slot.get("timingBasis") or "").strip() or None,
            "constraintSummary": str(slot.get("constraintSummary") or "").strip() or None,
            "placementAfterSegmentId": slot.get("placementAfterSegmentId"),
            "placementBeforeSegmentId": slot.get("placementBeforeSegmentId"),
            "requirementLevel": str(slot.get("requirementLevel") or "required"),
            "goalId": slot.get("goalId"),
            "sourceGoalId": slot.get("sourceGoalId"),
            "occurrenceId": slot.get("occurrenceId"),
            "groundingStatus": str(slot.get("groundingStatus") or "").strip() or None,
            "reasonCode": str(slot.get("reasonCode") or "").strip() or None,
            "sourceReasonCode": str(slot.get("sourceReasonCode") or "").strip() or None,
            "reason": str(slot.get("reason") or "").strip() or None,
            "simpleDirectionProviderExhausted": slot.get("simpleDirectionProviderExhausted") is True,
            "simpleDirectionRequirementLineageConflict": (
                slot.get("simpleDirectionRequirementLineageConflict") is True
            ),
            "optionalExperienceFamily": slot.get("optionalExperienceFamily"),
            "futureRouteAnchor": bool(slot.get("futureRouteAnchor")),
            "routeAnchorExpected": bool(slot.get("routeAnchorExpected")),
        }

    @staticmethod
    def _pending_sort_key(slot: dict[str, Any]) -> tuple[int, int, str]:
        raw = str(slot.get("startTime") or slot.get("timeWindow") or "")
        match = re.search(r"\b(\d{1,2}):(\d{2})\b", raw)
        minutes = int(match.group(1)) * 60 + int(match.group(2)) if match else 24 * 60 + 1
        return int(slot.get("dayNumber") or 0), minutes, str(slot.get("planningSlotId") or "")

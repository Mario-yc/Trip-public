"""Proposal-only orchestration; this service has no itinerary write capability."""

from __future__ import annotations

import copy
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from src.services.bounded_portfolio_optimizer import BoundedPortfolioOptimizer, PortfolioRuntimeLimits
from src.services.amap_call_budget import current_amap_call_budget
from src.services.brief_planning_projection_service import BriefPlanningProjectionService
from src.services.creative_planning_models import (
    ConstraintLedger,
    PlanCandidate,
    PlanPortfolio,
    canonical_fingerprint,
    proposal_canonical_signature,
    proposal_structural_signature_material,
)
from src.services.creative_portfolio_provider_service import InitialCreativePortfolio
from src.services.daily_capacity_planner import DailyCapacityPlanner
from src.services.pareto_portfolio_selector import ParetoPortfolioSelector
from src.services.plan_critic_service import PlanCriticService
from src.services.plan_repair_service import PlanRepairService
from src.services.plan_score_service import PlanScoreService
from src.services.goal_occurrence_compiler import GoalOccurrencePlan
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.plan_proposal_verifier import PlanProposalVerifier
from src.services.portfolio_route_feasibility_service import PortfolioRouteFeasibilityService
from src.services.portfolio_partial_projection_service import (
    PortfolioPartialProjectionService,
)
from src.services.portfolio_brief_worker_pool import PortfolioBriefWorkerPool
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer
from src.services.shared_candidate_universe_service import SharedCandidateUniverse
from src.services.consumer_candidate_admission_service import ConsumerCandidateAdmissionService
from src.services.creative_candidate_inventory_service import (
    CreativeCandidateInventoryService,
)
from src.services.creative_output_quality_service import CreativeOutputQualityService
from src.services.creative_proposal_title_service import (
    CreativeProposalTitleService,
)
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


class CreativePortfolioStagingService:
    EXPERIENCE_SPEC_POLICY_FIELDS = (
        "allowedDayNumbers",
        "experienceFamilies",
        "accessPolicy",
        "distinctnessPolicy",
        "timeWindow",
        "detourTolerance",
        "evidenceFreshness",
        "confidence",
        "unresolvedDimensions",
    )
    EXPERIENCE_ACCESS_EVIDENCE_FIELDS = (
        "nightAvailabilityStatus",
        "night_availability_status",
        "providerEvidenceQueriedAt",
        "provider_evidence_queried_at",
        "accessEvidenceObservedAt",
        "access_evidence_observed_at",
        "evidenceObservedAt",
        "evidence_observed_at",
        "observedAt",
        "observed_at",
        "queriedAt",
        "queried_at",
        "fetchedAt",
        "fetched_at",
        "publishedAt",
        "published_at",
        "freshness",
    )

    def __init__(
        self,
        store: PlanPortfolioStore,
        route_feasibility_service: PortfolioRouteFeasibilityService | None = None,
        *,
        experience_grounding_v2_mode: str = "off",
        soft_slot_draft_adoption_enabled: bool = False,
        creative_output_quality_v2_mode: str = "off",
        title_candidate_generator: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.store = store
        self.route_feasibility_service = route_feasibility_service
        self.optimizer = BoundedPortfolioOptimizer()
        self.critic = PlanCriticService()
        self.repair = PlanRepairService()
        self.scorer = PlanScoreService()
        self.selector = ParetoPortfolioSelector()
        self.semantic_policy = IntentCandidateSemanticPolicy()
        self.consumer_admission = ConsumerCandidateAdmissionService()
        self.experience_grounding_v2_mode = (
            experience_grounding_v2_mode if experience_grounding_v2_mode in {"off", "shadow", "enforce"} else "off"
        )
        self.verifier = PlanProposalVerifier(experience_grounding_v2_mode=self.experience_grounding_v2_mode)
        self.soft_slot_draft_adoption_enabled = bool(soft_slot_draft_adoption_enabled)
        self.creative_output_quality_v2_mode = (
            creative_output_quality_v2_mode
            if creative_output_quality_v2_mode in {"off", "shadow", "enforce"}
            else "off"
        )
        self.worker_pool = PortfolioBriefWorkerPool()
        self.title_candidate_generator = title_candidate_generator
        self.last_staging_metrics: dict[str, Any] = {}
        self.last_admitted_candidate_inventory: dict[str, Any] = CreativeCandidateInventoryService.build([])
        self._admitted_inventory_entries: list[dict[str, Any]] = []
        # Read-only hand-off for AgentService. This is never persisted as an
        # offered proposal because its strict proposal verifier did not pass.
        self.partial_timeline_candidate: PlanCandidate | None = None
        # Read-only, pre-route snapshots emitted by the real optimizer.  The
        # recorded-evidence adapter consumes these exact selections instead of
        # reconstructing a successful snapshot from certificate rows in tests.
        # They are never persisted or treated as verified proposals.
        self.last_pre_route_selected_snapshots: list[dict[str, Any]] = []
        self._last_staged_candidates: list[PlanCandidate] = []
        self._last_existing_portfolio_id = ""
        self._last_proposal_visible_callback: Callable[[PlanCandidate, int], None] | None = None
        self._last_stage_persisted = True

    def persist_last_stage(
        self,
        portfolio: PlanPortfolio,
        visible: list[PlanCandidate],
    ) -> tuple[PlanPortfolio, list[PlanCandidate]]:
        """Persist one already-validated stage result exactly once.

        AgentService defers this boundary until it has checked root-hard gaps.
        That keeps a root blocker from appending proposals or moving the active
        exploration focus while preserving the legacy direct-stage contract.
        """

        if self._last_stage_persisted:
            return portfolio, visible
        candidates = list(self._last_staged_candidates)
        callback = self._last_proposal_visible_callback
        if self._last_existing_portfolio_id:
            appended_visible: list[PlanCandidate] = []
            for candidate in visible:
                _choice_id, _proposal_id, created = self.store.offer_repaired_proposal(
                    portfolio_id=portfolio.portfolio_id,
                    proposal=candidate,
                )
                if created:
                    appended_visible.append(candidate)
            visible = appended_visible
            portfolio = portfolio.model_copy(
                update={
                    "status": "awaiting_selection" if visible else "failed",
                    "proposal_ids": [item.proposal_id for item in visible],
                    "visible_proposal_ids": [item.proposal_id for item in visible],
                    "failure_reason": None if visible else portfolio.failure_reason,
                }
            )
        else:
            self.store.create(portfolio, candidates)
        self._last_stage_persisted = True
        if self.last_staging_metrics:
            self.last_staging_metrics.update(
                {
                    "visibleProposalCount": len(visible),
                    "visibleProposalBriefIds": [item.brief.brief_id for item in visible],
                    "sameDayDuplicatePoiCount": sum(
                        self._same_day_duplicate_poi_count(item.itinerary_snapshot) for item in visible
                    ),
                    "crossDayReuseCount": sum(self._cross_day_reuse_count(item.itinerary_snapshot) for item in visible),
                }
            )
        if callback is not None:
            for index, candidate in enumerate(visible, start=1):
                callback(candidate, index)
        return portfolio, visible

    @classmethod
    def _with_experience_spec_policy(
        cls,
        candidate: dict[str, Any],
        occurrence: Any,
        *,
        base_route_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind one formal occurrence policy without flattening policy fields."""

        bound = copy.deepcopy(candidate)
        if occurrence is None:
            return bound
        occurrence_payload = (
            occurrence.model_dump(by_alias=True, exclude_unset=True)
            if callable(getattr(occurrence, "model_dump", None))
            else dict(occurrence)
            if isinstance(occurrence, dict)
            else {}
        )
        policy: dict[str, Any] = {}
        has_explicit_scope = bool(
            occurrence_payload.get("allowedDayNumbers")
            or occurrence_payload.get("experienceFamilies")
        )
        for field in cls.EXPERIENCE_SPEC_POLICY_FIELDS:
            value = occurrence_payload.get(field)
            if value is None:
                continue
            # Empty model defaults are not an authored ExperienceSpec.  They
            # must not activate fail-closed policy validation for legacy or
            # generic occurrences.  An explicitly scoped spec is recognized by
            # non-empty day/family values; its empty unresolved list is then a
            # meaningful assertion that no dimension remains open.
            if field in {"allowedDayNumbers", "experienceFamilies"} and not value:
                continue
            if field == "unresolvedDimensions" and not value and not has_explicit_scope:
                continue
            policy[field] = copy.deepcopy(value)
        # ``allowedDayNumbers`` and ``experienceFamilies`` are also used by the
        # search-profile compiler.  On their own they are semantic scope, not a
        # complete consumer access contract.  Treating that projection as an
        # active ExperienceSpec makes ordinary campus occurrences look like an
        # unsupported access-policy family and turns the fixed public-city-view
        # semantic overlay into ``access_policy_unknown``.  A real consumer
        # policy starts only when it carries at least one policy dimension, or
        # explicitly records unresolved dimensions (which must remain
        # fail-closed).
        has_consumer_policy = any(
            field in policy and policy[field] is not None
            for field in (
                "accessPolicy",
                "distinctnessPolicy",
                "timeWindow",
                "detourTolerance",
                "evidenceFreshness",
                "confidence",
            )
        ) or bool(policy.get("unresolvedDimensions"))
        if not has_consumer_policy:
            return bound
        fingerprint = canonical_fingerprint(policy)
        route_contract = {
            **(copy.deepcopy(bound.get("routeContract")) if isinstance(bound.get("routeContract"), dict) else {}),
            **copy.deepcopy(base_route_contract or {}),
            "experienceSpecPolicy": copy.deepcopy(policy),
            "specFingerprint": fingerprint,
        }
        # Once an occurrence is governed by ExperienceSpec, a missing route
        # policy is not permission to skip the Provider insertion gate.  Mark
        # every non-empty spec as route-decision-bound; the feasibility layer
        # will then fail closed with a precise missing time-window/tolerance
        # reason before any Provider call.
        route_contract["requiresProviderInsertionDecision"] = True
        bound["experienceSpecPolicy"] = policy
        bound["specFingerprint"] = fingerprint
        bound["routeContract"] = route_contract
        return bound

    @staticmethod
    def _with_experience_spec_route_contracts(
        snapshot: dict[str, Any],
        *,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Project selected route and admission policy into materialized segments."""

        projected = copy.deepcopy(snapshot)
        by_occurrence: dict[str, dict[str, Any]] = {}
        by_scope: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        by_amap_id: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            if not isinstance(candidate, dict) or not (
                isinstance(candidate.get("routeContract"), dict)
                or isinstance(candidate.get("consumerAdmissionInput"), dict)
            ):
                continue
            occurrence_id = str(candidate.get("occurrenceId") or "")
            if occurrence_id:
                by_occurrence[occurrence_id] = candidate
            scope = (
                str(candidate.get("briefId") or ""),
                str(candidate.get("poolId") or ""),
                str(candidate.get("planningSlotId") or candidate.get("slotId") or ""),
                int(candidate.get("dayNumber") or 0),
            )
            if all(scope):
                by_scope[scope] = candidate
            amap_id = PoiPhysicalIdentityService.normalized_amap_id(candidate)
            if amap_id:
                by_amap_id.setdefault(amap_id, []).append(candidate)

        for day in projected.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                occurrence_id = str(semantic.get("occurrenceId") or "")
                candidate = by_occurrence.get(occurrence_id)
                if candidate is None:
                    candidate = by_scope.get(
                        (
                            str(semantic.get("creativeBriefId") or ""),
                            str(semantic.get("poolId") or ""),
                            str(semantic.get("planningSlotId") or semantic.get("slotId") or ""),
                            day_number,
                        )
                    )
                if candidate is None:
                    poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                    amap_matches = by_amap_id.get(PoiPhysicalIdentityService.normalized_amap_id(poi), [])
                    candidate = amap_matches[0] if len(amap_matches) == 1 else None
                if candidate is None:
                    continue
                route_contract = candidate.get("routeContract")
                if isinstance(route_contract, dict):
                    semantic["routeContract"] = {
                        **(
                            copy.deepcopy(semantic.get("routeContract"))
                            if isinstance(semantic.get("routeContract"), dict)
                            else {}
                        ),
                        **copy.deepcopy(route_contract),
                    }
                    poi = segment.get("poi")
                    if isinstance(poi, dict):
                        for field in CreativePortfolioStagingService.EXPERIENCE_ACCESS_EVIDENCE_FIELDS:
                            if field in candidate:
                                poi[field] = copy.deepcopy(candidate[field])
                consumer_input = candidate.get("consumerAdmissionInput")
                if isinstance(consumer_input, dict):
                    semantic["consumerAdmissionInput"] = copy.deepcopy(consumer_input)
                segment["semanticMetadata"] = semantic
        return projected

    @staticmethod
    def _reconcile_admitted_anchor_materialization(
        snapshot: dict[str, Any],
        *,
        admitted_candidates: list[dict[str, Any]],
        skeleton: Any,
        admission_enforced: bool,
    ) -> dict[str, Any]:
        """Make post-admission materialization loss explicit and recoverable."""

        projected = copy.deepcopy(snapshot)
        actual_keys: set[tuple[str, str, str, int, str]] = set()
        actual_admission_report_count = 0
        for day in projected.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                if semantic.get("routeAnchor") is not True:
                    continue
                report = (
                    semantic.get("consumerAdmissionReport")
                    if isinstance(semantic.get("consumerAdmissionReport"), dict)
                    else {}
                )
                if admission_enforced and report.get("scoreEligible") is not True:
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                key = (
                    str(semantic.get("creativeBriefId") or ""),
                    str(semantic.get("poolId") or ""),
                    str(semantic.get("planningSlotId") or semantic.get("slotId") or ""),
                    day_number,
                    PoiPhysicalIdentityService.normalized_amap_id(poi),
                )
                actual_keys.add(key)
                if report:
                    actual_admission_report_count += 1

        selected: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
        for candidate in admitted_candidates:
            if not isinstance(candidate, dict):
                continue
            report = (
                candidate.get("consumerAdmissionReport")
                if isinstance(candidate.get("consumerAdmissionReport"), dict)
                else {}
            )
            if admission_enforced and report.get("scoreEligible") is not True:
                continue
            key = (
                str(candidate.get("briefId") or skeleton.brief.brief_id or ""),
                str(candidate.get("poolId") or ""),
                str(candidate.get("planningSlotId") or candidate.get("slotId") or ""),
                int(candidate.get("dayNumber") or 0),
                PoiPhysicalIdentityService.normalized_amap_id(candidate),
            )
            if all((key[0], key[1], key[2], key[3], key[4])):
                selected[key] = candidate

        missing_keys = [key for key in selected if key not in actual_keys]
        pending = [
            copy.deepcopy(item) for item in projected.get("portfolioPendingSlots") or [] if isinstance(item, dict)
        ]
        pending_scope = {
            (
                str(item.get("briefId") or ""),
                str(item.get("poolId") or ""),
                str(item.get("planningSlotId") or item.get("slotId") or ""),
                int(item.get("dayNumber") or 0),
            )
            for item in pending
        }
        slots = {str(item.slot_id): item for item in skeleton.day_slots}
        for key in missing_keys:
            candidate = selected[key]
            scope = key[:4]
            if scope in pending_scope:
                continue
            slot = slots.get(key[2])
            requirement = str(candidate.get("requirementLevel") or getattr(slot, "requirement_level", "soft") or "soft")
            pending.append(
                {
                    "briefId": key[0],
                    "poolId": key[1],
                    "planningSlotId": key[2],
                    "dayNumber": key[3],
                    "timeWindow": str(candidate.get("timeWindow") or getattr(slot, "time_window", "") or ""),
                    "startTime": str(candidate.get("startTime") or getattr(slot, "start_time", "") or ""),
                    "durationMinutes": int(
                        candidate.get("durationMinutes") or getattr(slot, "duration_minutes", 0) or 0
                    ),
                    "requirementLevel": requirement,
                    "sourceGoalId": str(candidate.get("sourceGoalId") or candidate.get("goalId") or ""),
                    "intentType": str(candidate.get("intentType") or ""),
                    "rawNeed": str(candidate.get("rawNeed") or "待补地点"),
                    "displayNeed": str(candidate.get("rawNeed") or "待补地点"),
                    "optionalExperienceFamily": candidate.get("optionalExperienceFamily"),
                    "futureRouteAnchor": True,
                    "routeAnchorExpected": True,
                    "reason": "admitted_candidate_not_materialized",
                }
            )
            pending_scope.add(scope)
        if pending:
            projected["portfolioPendingSlots"] = pending
        projected["portfolioAdmissionMaterializationAudit"] = {
            "selectedAdmittedCandidateCount": len(selected),
            "actualProposalAnchorCount": len(actual_keys),
            "actualAdmissionReportCount": actual_admission_report_count,
            "materializationMissingCount": len(missing_keys),
            "materializationMissingSlotKeys": [
                {
                    "briefId": key[0],
                    "poolId": key[1],
                    "planningSlotId": key[2],
                    "dayNumber": key[3],
                }
                for key in missing_keys
            ],
            "scoreEligibleAnchorCount": (actual_admission_report_count if admission_enforced else len(actual_keys)),
            "countInvariantPassed": (actual_admission_report_count == len(actual_keys) if admission_enforced else True),
        }
        return projected

    @staticmethod
    def _with_soft_pending_slots(
        snapshot: dict[str, Any],
        skeleton: Any,
    ) -> dict[str, Any]:
        """Project ungrounded soft intents as metadata, never placeholder POIs."""

        projected = copy.deepcopy(snapshot)
        covered_slot_ids = {
            str(semantic.get("planningSlotId") or semantic.get("intentSlotId") or "")
            for day in projected.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
            for semantic in [
                segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            ]
        }
        pending = [
            copy.deepcopy(item) for item in projected.get("portfolioPendingSlots") or [] if isinstance(item, dict)
        ]
        pending_keys = {
            (
                str(item.get("briefId") or ""),
                str(item.get("planningSlotId") or ""),
            )
            for item in pending
        }
        pools_by_slot = {str(slot_id): pool for pool in skeleton.intent_pools for slot_id in pool.assign_to_slots}
        for slot in skeleton.day_slots:
            if slot.requirement_level != "soft" or slot.slot_id in covered_slot_ids:
                continue
            key = (str(skeleton.brief.brief_id), str(slot.slot_id))
            if key in pending_keys:
                continue
            pool = pools_by_slot.get(str(slot.slot_id))
            intent_payload = {
                "briefId": skeleton.brief.brief_id,
                "poolId": str(pool.pool_id) if pool is not None else "",
                "planningSlotId": slot.slot_id,
                "dayNumber": slot.day_number,
                "requirementLevel": "soft",
                "softGoalId": slot.soft_goal_id,
                "sourceGoalId": slot.soft_goal_id,
                "experienceShape": slot.experience_shape or "single_poi",
                "experienceGoal": slot.experience_goal or slot.raw_need,
                "desiredSignals": list(slot.desired_signals),
                "avoidSignals": list(slot.avoid_signals),
                "evidencePolicy": copy.deepcopy(slot.evidence_requirements),
                "groundingPolicy": copy.deepcopy(slot.grounding_contract),
                "routeContext": copy.deepcopy(slot.route_contract),
            }
            pending.append(
                {
                    **intent_payload,
                    "timeWindow": slot.time_window,
                    "startTime": slot.start_time,
                    "durationMinutes": slot.duration_minutes,
                    "optionalExperienceFamily": slot.optional_experience_family,
                    "futureRouteAnchor": bool(slot.route_anchor),
                    "routeAnchorExpected": bool(slot.route_anchor),
                    "intentFingerprint": slot.intent_fingerprint or canonical_fingerprint(intent_payload),
                    "reason": "consumer_evidence_pending",
                }
            )
            pending_keys.add(key)
        if pending:
            projected["portfolioPendingSlots"] = pending
        return projected

    def _with_output_quality(
        self,
        snapshot: dict[str, Any],
        brief: Any,
    ) -> dict[str, Any]:
        if self.creative_output_quality_v2_mode == "off":
            return snapshot
        requested_theme = (
            CreativeOutputQualityService.THEME if str(getattr(brief, "primary_axis", "") or "") == "food_led" else ""
        )
        if not requested_theme:
            return snapshot
        projected = copy.deepcopy(snapshot)
        original_title = str(projected.get("title") or getattr(brief, "title", "") or "行程方案")
        projected["portfolioOutputQuality"] = {
            "schemaVersion": "creative-output-quality-v2",
            "mode": self.creative_output_quality_v2_mode,
            "requestedTheme": requested_theme,
            "originalCreativeTitle": original_title,
        }
        quality = CreativeOutputQualityService.evaluate(
            projected,
            requested_theme=requested_theme,
        )
        projected["portfolioOutputQuality"].update(copy.deepcopy(quality))
        projected["continuationTheme"] = {
            "theme": requested_theme,
            "status": "complete" if quality["themeEligible"] else "selected_but_incomplete",
            "completionAttemptCount": 0,
            "evidenceFingerprint": canonical_fingerprint(
                {
                    "mealCount": quality["admittedLocalMealCount"],
                    "areaAnchorCount": quality["distinctAreaWalkPhysicalAnchorCount"],
                    "walkingRelation": quality["areaWalkWalkingRelationVerified"],
                }
            ),
        }
        if self.creative_output_quality_v2_mode == "enforce" and quality["themeEligible"] is not True:
            projected["title"] = quality["displayTitle"]
        return projected

    def _apply_consumer_admission(
        self,
        candidate: dict[str, Any],
        *,
        brief_id: str,
        pool: Any,
        slot: Any,
        family: str,
        metrics: dict[str, int],
    ) -> bool:
        if self.experience_grounding_v2_mode == "off":
            return True
        route_context = dict(
            candidate.get("routeContract")
            if isinstance(candidate.get("routeContract"), dict)
            else getattr(slot, "route_contract", None) or pool.route_preference or {}
        )
        consumer = self.consumer_admission.build_consumer_context(
            brief_id=brief_id,
            pool_id=str(pool.pool_id),
            planning_slot_id=str(slot.slot_id),
            day_number=int(slot.day_number),
            city=str(pool.city),
            optional_experience_family=(
                str(getattr(pool, "optional_experience_family", None) or "") or None
            ),
            family=family or str(pool.intent_type),
            assigned_meal_family=(
                getattr(slot, "assigned_meal_family", None)
                or (
                    pool.exact_entity
                    if family in {"meal", "local_food", "food"} and pool.entity_binding_mode == "exact_entity"
                    else None
                )
            ),
            activity_mode=str(pool.intent_type),
            requirement_level=str(
                getattr(slot, "requirement_level", None)
                or ("required" if pool.requirement_level == "required" else "soft")
            ),
            experience_shape=str(
                getattr(slot, "experience_shape", None)
                or ("area" if family.endswith("_walk") or family == "local_life" else "single_poi")
            ),
            experience_goal=str(getattr(slot, "experience_goal", None) or slot.raw_need),
            desired_signals=getattr(slot, "desired_signals", None),
            avoid_signals=getattr(slot, "avoid_signals", None),
            evidence_requirements=getattr(slot, "evidence_requirements", None),
            grounding_policy=getattr(slot, "grounding_contract", None),
            route_context=route_context,
            experience_spec_policy=(
                candidate.get("experienceSpecPolicy")
                if isinstance(candidate.get("experienceSpecPolicy"), dict)
                else None
            ),
            spec_fingerprint=str(candidate.get("specFingerprint") or ""),
            intent_fingerprint=getattr(slot, "intent_fingerprint", None),
            exact_entity=(pool.exact_entity if pool.entity_binding_mode == "exact_entity" else None),
            preferred_types=pool.preferred_types,
            rejected_types=pool.rejected_types,
        )
        report = self.consumer_admission.evaluate(candidate, consumer)
        candidate["consumerAdmissionInput"] = copy.deepcopy(consumer)
        candidate["consumerAdmissionReport"] = report
        candidate["scoreEligible"] = bool(report.get("scoreEligible"))
        components = report.get("scoreComponents") or {}
        candidate["consumerEvidenceScore"] = round(
            0.30 * float(components.get("evidenceStrength") or 0)
            + 0.15 * float(components.get("sourceFreshness") or 0)
            + 0.25 * float(components.get("localDistinctiveness") or 0)
            + 0.30 * float(components.get("userIntentFit") or 0)
            - 0.25 * float(components.get("uncertaintyPenalty") or 0),
            4,
        )
        metrics["consumerAdmissionEvaluatedCount"] += 1
        metrics["consumerReevaluationCount"] += 1
        legacy_accepted = bool(candidate.get("semanticPassed"))
        v2_accepted = bool(report.get("scoreEligible"))
        candidate["consumerAdmissionShadowComparison"] = {
            "legacyAccepted": legacy_accepted,
            "v2Accepted": v2_accepted,
            "diverged": legacy_accepted != v2_accepted,
            "divergenceReason": (str((report.get("reasonCodes") or [""])[0]) if legacy_accepted != v2_accepted else ""),
        }
        if legacy_accepted != v2_accepted:
            metrics["consumerAdmissionDivergenceCount"] = metrics.get("consumerAdmissionDivergenceCount", 0) + 1
        classification = str(report.get("classification") or "rejected")
        metric_key = {
            "admitted_final_anchor": "consumerAdmissionAdmittedCount",
            "admitted_anchor_set_member": "consumerAdmissionAdmittedCount",
            "pending_evidence": "consumerAdmissionPendingCount",
            "area_seed_only": "consumerAdmissionAreaSeedOnlyCount",
            "rejected": "consumerAdmissionRejectedCount",
        }.get(classification, "consumerAdmissionRejectedCount")
        metrics[metric_key] += 1
        inventory_item = CreativeCandidateInventoryService.capture(
            candidate,
            family=family or str(pool.intent_type),
            intent_type=str(pool.intent_type),
            day_number=int(slot.day_number),
            time_window=str(slot.time_window),
            brief_id=brief_id,
            pool_id=str(pool.pool_id),
            planning_slot_id=str(slot.slot_id),
        )
        if inventory_item is not None:
            self._admitted_inventory_entries.append(inventory_item)
            self.last_admitted_candidate_inventory = CreativeCandidateInventoryService.build(
                self._admitted_inventory_entries
            )
        if any(
            bool(item.get("staleAdmissionInvalidated"))
            for item in report.get("gateResults") or []
            if isinstance(item, dict)
        ):
            metrics["staleAdmissionInvalidatedCount"] += 1
            metrics["consumerFingerprintMismatchCount"] += 1
        first_failed_gate = next(
            (
                str(item.get("gate") or "")
                for item in report.get("gateResults") or []
                if isinstance(item, dict) and item.get("passed") is False
            ),
            "",
        )
        gate_metric = {
            "amap_identity": "identityGateRejectCount",
            "exact_entity_binding": "exactEntityGateRejectCount",
            "semantic_affordance": "semanticAffordanceRejectCount",
            "anchor_eligibility": "anchorEligibilityRejectCount",
            "specialized_policy": "specializedPolicyRejectCount",
            "evidence_sufficiency": "evidenceSufficiencyPendingCount",
        }.get(first_failed_gate)
        if gate_metric:
            metrics[gate_metric] += 1
        if bool((report.get("evidenceSummary") or {}).get("nameOnlyPositiveSignal")):
            metrics["nameOnlyPositiveSignalCount"] += 1
        if self.experience_grounding_v2_mode == "enforce":
            candidate["semanticPassed"] = bool(report.get("scoreEligible"))
            return bool(report.get("scoreEligible"))
        return True

    def build_admitted_candidate_inventory(
        self,
        generated: InitialCreativePortfolio,
        universe: SharedCandidateUniverse,
    ) -> dict[str, Any]:
        """Evaluate all search-supplied candidates before direction assembly.

        This is read-only and uses the exact same semantic and Consumer
        Admission functions as staging.  It deliberately does not optimize,
        route, persist, or count a candidate as proposal coverage.
        """

        self._admitted_inventory_entries = []
        self.last_admitted_candidate_inventory = CreativeCandidateInventoryService.build([])
        metrics = self._new_admission_metrics()
        for skeleton in generated.proposals:
            slots = {slot.slot_id: slot for slot in skeleton.day_slots}
            for pool in skeleton.intent_pools:
                family = str(pool.optional_experience_family or pool.intent_type)
                for slot_id in pool.assign_to_slots:
                    slot = slots.get(slot_id)
                    if slot is None:
                        continue
                    for raw in universe.candidates_for_pool(
                        pool.pool_id,
                        pool.intent_type,
                        skeleton.brief.brief_id,
                    ):
                        candidate = dict(raw)
                        semantic = self.semantic_policy.evaluate(
                            pool.intent_type,
                            candidate,
                            raw_need=pool.raw_need,
                            exact_entity=(pool.exact_entity if pool.entity_binding_mode == "exact_entity" else None),
                            optional_experience_family=(
                                str(pool.optional_experience_family) if pool.optional_experience_family else None
                            ),
                        )
                        candidate["semanticPassed"] = semantic.passed
                        candidate["semanticDecision"] = semantic.to_camel_dict()
                        if not semantic.passed:
                            continue
                        candidate.update(
                            {
                                "briefId": skeleton.brief.brief_id,
                                "poolId": pool.pool_id,
                                "planningSlotId": slot.slot_id,
                                "slotId": slot.slot_id,
                                "dayNumber": slot.day_number,
                                "timeWindow": slot.time_window,
                                "intentType": pool.intent_type,
                                "optionalExperienceFamily": (pool.optional_experience_family),
                            }
                        )
                        self._apply_consumer_admission(
                            candidate,
                            brief_id=skeleton.brief.brief_id,
                            pool=pool,
                            slot=slot,
                            family=family,
                            metrics=metrics,
                        )
        return copy.deepcopy(self.last_admitted_candidate_inventory)

    @staticmethod
    def _new_admission_metrics() -> dict[str, int]:
        return {
            "consumerAdmissionEvaluatedCount": 0,
            "consumerAdmissionAdmittedCount": 0,
            "consumerAdmissionPendingCount": 0,
            "consumerAdmissionRejectedCount": 0,
            "consumerAdmissionAreaSeedOnlyCount": 0,
            "consumerFingerprintMismatchCount": 0,
            "staleAdmissionInvalidatedCount": 0,
            "consumerReevaluationCount": 0,
            "identityGateRejectCount": 0,
            "exactEntityGateRejectCount": 0,
            "semanticAffordanceRejectCount": 0,
            "anchorEligibilityRejectCount": 0,
            "specializedPolicyRejectCount": 0,
            "evidenceSufficiencyPendingCount": 0,
            "nameOnlyPositiveSignalCount": 0,
            "consumerAdmissionDivergenceCount": 0,
        }

    @staticmethod
    def _scope_daily_capacity_plan(
        snapshot: dict[str, Any],
        *,
        projection: Any,
    ) -> dict[str, Any]:
        """Bind request-level capacity evidence to this brief's anchor target."""

        raw = snapshot.get("portfolioDailyCapacityPlan")
        if not isinstance(raw, dict):
            return snapshot
        scoped = copy.deepcopy(snapshot)
        capacity = copy.deepcopy(raw)
        default_usable = DailyCapacityPlanner.USABLE_MINUTES.get(
            str(projection.pace),
            DailyCapacityPlanner.USABLE_MINUTES["standard"],
        )
        for day_number, target in projection.day_anchor_targets:
            day_key = str(day_number)
            existing = capacity.get(day_key)
            row = copy.deepcopy(existing) if isinstance(existing, dict) else {}
            usable = int(row.get("usableMinutes") or default_usable)
            planned = int(row.get("plannedMinutes") or 0)
            baseline_target = int(row.get("targetRouteAnchors") or 0)
            route_reserve = max(0, int(target) - 1) * DailyCapacityPlanner.ROUTE_RESERVE_PER_TRANSIT_LEG
            buffer = int(target) * DailyCapacityPlanner.BUFFER_PER_ANCHOR
            allocated = planned + route_reserve + buffer
            row.update(
                {
                    "dayNumber": day_number,
                    "usableMinutes": usable,
                    "plannedMinutes": planned,
                    "routeReserveMinutes": route_reserve,
                    "bufferMinutes": buffer,
                    "intentionalFreeMinutes": max(0, usable - allocated),
                    "unexplainedGapMinutes": max(0, allocated - usable),
                    "targetRouteAnchors": int(target),
                    "evidence": [
                        "source=creative_brief_projection",
                        f"briefId={projection.brief_id}",
                        f"pace={projection.pace}",
                        f"occurrenceBaselineRouteAnchors={baseline_target}",
                    ],
                }
            )
            capacity[day_key] = row
        scoped["portfolioDailyCapacityPlan"] = capacity
        return scoped

    def stage(
        self,
        *,
        session_id: str,
        source_user_turn_id: str,
        source_assistant_turn_id: str,
        expected_base_version_id: str | None,
        observation_fingerprint: str,
        request_fingerprint: str,
        ledger: ConstraintLedger,
        generated: InitialCreativePortfolio,
        universe: SharedCandidateUniverse,
        snapshot_builder: Callable[
            [dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], str],
            dict[str, Any],
        ],
        max_route_anchors_by_day: dict[int, int] | None = None,
        brief_progress_callback: Callable[[str, int, int, float, bool], None] | None = None,
        proposal_visible_callback: Callable[[PlanCandidate, int], None] | None = None,
        city: str = "",
        goal_occurrence_plan: dict[str, Any] | None = None,
        focus_brief_id: str = "",
        existing_portfolio_id: str = "",
        prior_comparison_projections: list[dict[str, Any]] | None = None,
        persist_result: bool = True,
    ) -> tuple[PlanPortfolio, list[PlanCandidate]]:
        self._admitted_inventory_entries = []
        self.last_admitted_candidate_inventory = CreativeCandidateInventoryService.build([])
        stage_started = perf_counter()
        self.last_staging_metrics = {}
        admission_metrics = self._new_admission_metrics()
        self.partial_timeline_candidate = None
        self.last_pre_route_selected_snapshots = []
        self._last_staged_candidates = []
        self._last_existing_portfolio_id = str(existing_portfolio_id or "")
        self._last_proposal_visible_callback = proposal_visible_callback
        self._last_stage_persisted = False
        by_intent = universe.pools
        portfolio_id = str(existing_portfolio_id or f"portfolio_{uuid4().hex[:16]}")
        # A controller-owned occurrence is the optimizer unit. The same hard
        # goal may legitimately occur on more than one day, so using goal_id as
        # the dictionary key would silently collapse the later occurrence.
        occurrence_contract: GoalOccurrencePlan | None = None
        if isinstance(goal_occurrence_plan, dict):
            try:
                occurrence_contract = GoalOccurrencePlan.model_validate(goal_occurrence_plan)
            except ValueError:
                portfolio = PlanPortfolio(
                    portfolioId=portfolio_id,
                    sessionId=session_id,
                    sourceUserTurnId=source_user_turn_id,
                    sourceAssistantTurnId=source_assistant_turn_id,
                    expectedBaseVersionId=expected_base_version_id,
                    sourceObservationFingerprint=observation_fingerprint,
                    requestContractFingerprint=request_fingerprint,
                    status="failed",
                    failureReason="portfolio_goal_occurrence_contract_invalid",
                )
                if persist_result:
                    portfolio, _visible = self.persist_last_stage(portfolio, [])
                return portfolio, []
        if occurrence_contract is None:
            # Legacy callers have no day/slot identity with which to materialize
            # repeated hard goals. Keep those requests fail-closed.
            unsupported_cardinality = [goal.goal_id for goal in ledger.hard_goals if goal.required_min != 1]
            if unsupported_cardinality:
                portfolio = PlanPortfolio(
                    portfolioId=portfolio_id,
                    sessionId=session_id,
                    sourceUserTurnId=source_user_turn_id,
                    sourceAssistantTurnId=source_assistant_turn_id,
                    expectedBaseVersionId=expected_base_version_id,
                    sourceObservationFingerprint=observation_fingerprint,
                    requestContractFingerprint=request_fingerprint,
                    status="failed",
                    failureReason="required_goal_cardinality_not_supported:"
                    + ",".join(sorted(unsupported_cardinality)),
                )
                if persist_result:
                    portfolio, _visible = self.persist_last_stage(portfolio, [])
                return portfolio, []
        else:
            occurrence_failure = self._occurrence_contract_failure(ledger, occurrence_contract)
            if occurrence_failure:
                portfolio = PlanPortfolio(
                    portfolioId=portfolio_id,
                    sessionId=session_id,
                    sourceUserTurnId=source_user_turn_id,
                    sourceAssistantTurnId=source_assistant_turn_id,
                    expectedBaseVersionId=expected_base_version_id,
                    sourceObservationFingerprint=observation_fingerprint,
                    requestContractFingerprint=request_fingerprint,
                    status="failed",
                    failureReason=occurrence_failure,
                )
                if persist_result:
                    portfolio, _visible = self.persist_last_stage(portfolio, [])
                return portfolio, []
        has_occurrence_contract = occurrence_contract is not None
        if occurrence_contract is not None:
            required = {
                occurrence.occurrence_id: [
                    {
                        **dict(raw),
                        "goalId": occurrence.source_goal_id,
                        "sourceGoalId": occurrence.source_goal_id,
                        "occurrenceId": occurrence.occurrence_id,
                        "dayNumber": occurrence.day_number,
                        "intentType": occurrence.intent_type,
                        "requirementLevel": occurrence.requirement_level,
                        "distinctGroupId": occurrence.distinct_group_id,
                    }
                    for raw in by_intent.get(occurrence.intent_type, [])
                ]
                for occurrence in occurrence_contract.occurrences
                if occurrence.requirement_level == "hard"
            }
        else:
            required = {goal.goal_id: by_intent.get(goal.intent_type, []) for goal in ledger.hard_goals}
        density_decision_source = (
            "deterministic_fallback"
            if generated.parser_metadata.get("deterministicFallbackUsed")
            else "creative_portfolio_provider"
        )
        projections = {
            item.brief.brief_id: BriefPlanningProjectionService().project(
                item,
                ledger,
                max_route_anchors_by_day=max_route_anchors_by_day,
                density_decision_source=density_decision_source,
            )
            for item in generated.proposals
        }
        soft_goal_ids = {goal.goal_id for goal in ledger.soft_goals} | {
            occurrence.source_goal_id
            for occurrence in (occurrence_contract.occurrences if occurrence_contract is not None else [])
            if occurrence.requirement_level != "hard"
        }
        occurrence_by_id = {
            occurrence.occurrence_id: occurrence
            for occurrence in (occurrence_contract.occurrences if occurrence_contract is not None else [])
        }
        soft_pools: dict[str, dict[str, list[dict[str, Any]]]] = {}
        optional_pools: dict[str, dict[str, list[dict[str, Any]]]] = {}
        completion_candidates_by_brief: dict[str, dict[str, Any]] = {
            skeleton.brief.brief_id: {
                "schemaVersion": "portfolio-theme-completion-candidates-v1",
                "mealCandidatesBySlot": {},
                "areaCandidates": [],
            }
            for skeleton in generated.proposals
        }
        for skeleton in generated.proposals:
            projection = projections[skeleton.brief.brief_id]
            slot_by_id = {slot.slot_id: slot for slot in skeleton.day_slots}
            brief_soft_pools: dict[str, list[dict[str, Any]]] = {}
            pools: dict[str, list[dict[str, Any]]] = {}
            for pool in skeleton.intent_pools:
                soft_goal_id = str(pool.soft_goal_id or "")
                if soft_goal_id:
                    assigned_slots = [slot_by_id.get(slot_id) for slot_id in pool.assign_to_slots]
                    if (
                        soft_goal_id not in soft_goal_ids
                        or not assigned_slots
                        or any(slot is None or str(slot.soft_goal_id or "") != soft_goal_id for slot in assigned_slots)
                    ):
                        continue
                    slot = assigned_slots[0]
                    occurrence_id = (
                        f"occ:{soft_goal_id}:day:{int(slot.day_number)}" if has_occurrence_contract else soft_goal_id
                    )
                    soft_occurrence = occurrence_by_id.get(occurrence_id)
                    soft_candidates: list[dict[str, Any]] = []
                    for raw in universe.candidates_for_pool(pool.pool_id, pool.intent_type, skeleton.brief.brief_id):
                        candidate = dict(raw)
                        # A shared network result is evidence, not an approval
                        # for every consumer. Re-run the consuming pool's
                        # semantic contract before rebinding its lineage.
                        semantic_decision = self.semantic_policy.evaluate(
                            pool.intent_type,
                            candidate,
                            raw_need=pool.raw_need,
                            exact_entity=pool.exact_entity if pool.entity_binding_mode == "exact_entity" else None,
                            optional_experience_family=None,
                        )
                        candidate["semanticPassed"] = semantic_decision.passed
                        candidate["semanticDecision"] = semantic_decision.to_camel_dict()
                        if not semantic_decision.passed:
                            continue
                        candidate.update(
                            {
                                "briefId": skeleton.brief.brief_id,
                                "poolId": pool.pool_id,
                                "softGoalId": soft_goal_id,
                                "sourceGoalId": soft_goal_id,
                                "occurrenceId": occurrence_id,
                                "distinctGroupId": (
                                    soft_occurrence.distinct_group_id if soft_occurrence is not None else None
                                ),
                                "assignToSlots": list(pool.assign_to_slots),
                                "slotId": slot.slot_id,
                                "planningSlotId": slot.slot_id,
                                "dayNumber": slot.day_number,
                                "timeWindow": slot.time_window,
                                "startTime": slot.start_time,
                                "durationMinutes": slot.duration_minutes,
                                "kind": slot.kind,
                                "rawNeed": slot.raw_need,
                                "intentType": pool.intent_type,
                                "dayRole": next(
                                    (
                                        role.role
                                        for role in skeleton.brief.day_roles
                                        if role.day_number == slot.day_number
                                    ),
                                    "",
                                ),
                                "routePreference": dict(pool.route_preference or {}),
                                "entityBindingMode": pool.entity_binding_mode,
                                "exactEntity": pool.exact_entity,
                            }
                        )
                        candidate = self._with_experience_spec_policy(
                            candidate,
                            soft_occurrence,
                            base_route_contract=dict(
                                getattr(slot, "route_contract", None) or pool.route_preference or {}
                            ),
                        )
                        admitted = self._apply_consumer_admission(
                            candidate,
                            brief_id=skeleton.brief.brief_id,
                            pool=pool,
                            slot=slot,
                            family=str(pool.optional_experience_family or pool.intent_type),
                            metrics=admission_metrics,
                        )
                        if not admitted:
                            self._capture_completion_candidate(
                                completion_candidates_by_brief[skeleton.brief.brief_id],
                                candidate,
                                family=str(pool.optional_experience_family or pool.intent_type),
                                slot_id=str(slot.slot_id),
                            )
                            continue
                        soft_candidates.append(candidate)
                    brief_soft_pools[occurrence_id] = soft_candidates
                    continue
                if pool.requirement_level != "optional":
                    continue
                family = str(pool.optional_experience_family or "")
                # Projection is the executable brief contract, not merely
                # lineage.  It incorporates forbidden experience types and
                # constrains each optional pool to the brief-local slots.
                if (
                    not family
                    or family not in projection.optional_families
                    or pool.pool_id not in projection.optional_pool_ids
                    or not pool.assign_to_slots
                    or not set(pool.assign_to_slots) <= set(projection.optional_slot_ids)
                ):
                    continue
                candidates: list[dict[str, Any]] = []
                shared_evidence = universe.candidates_for_pool(pool.pool_id, pool.intent_type, skeleton.brief.brief_id)
                # A coarse `area_walk` query and its family-specific evidence
                # are both reusable facts.  Neither is an approval: every row
                # below is rebound to this slot and re-evaluated by the family
                # semantic/admission contract before optimizer eligibility.
                seen_evidence_ids = {str(item.get("amapId") or item.get("id") or "") for item in shared_evidence}
                shared_evidence.extend(
                    dict(item)
                    for item in universe.pools.get(family, [])
                    if str(item.get("amapId") or item.get("id") or "") not in seen_evidence_ids
                )
                direction_supply = (
                    skeleton.brief.candidate_supply if isinstance(skeleton.brief.candidate_supply, dict) else {}
                )
                permitted_direction_ids = {
                    str(item) for item in direction_supply.get("candidateIds") or [] if str(item)
                }
                if permitted_direction_ids:
                    # A supply-backed direction is an executable candidate
                    # contract.  Letting the optimizer choose another admitted
                    # POI would silently turn a novel direction into a duplicate
                    # of an already visible proposal.
                    shared_evidence = [
                        item
                        for item in shared_evidence
                        if str(item.get("amapId") or item.get("id") or "") in permitted_direction_ids
                    ]
                for raw in shared_evidence:
                    candidate = dict(raw)
                    semantic_decision = self.semantic_policy.evaluate(
                        pool.intent_type,
                        candidate,
                        raw_need=pool.raw_need,
                        exact_entity=pool.exact_entity if pool.entity_binding_mode == "exact_entity" else None,
                        optional_experience_family=family,
                    )
                    candidate["semanticPassed"] = semantic_decision.passed
                    candidate["semanticDecision"] = semantic_decision.to_camel_dict()
                    if not semantic_decision.passed:
                        continue
                    candidate.update(
                        {
                            "briefId": skeleton.brief.brief_id,
                            "poolId": pool.pool_id,
                            "optionalExperienceFamily": family,
                            # One physical POI cannot satisfy two separate creative
                            # experience slots, even when those slots occur on
                            # different days.  The optimizer already treats a
                            # shared distinct group as an identity reservation.
                            "distinctGroupId": (f"optional:{skeleton.brief.brief_id}"),
                            "assignToSlots": list(pool.assign_to_slots),
                            "slotId": pool.assign_to_slots[0],
                            "planningSlotId": pool.assign_to_slots[0],
                            "dayNumber": slot_by_id[pool.assign_to_slots[0]].day_number if pool.assign_to_slots else 1,
                            "timeWindow": slot_by_id[pool.assign_to_slots[0]].time_window,
                            "startTime": slot_by_id[pool.assign_to_slots[0]].start_time,
                            "durationMinutes": slot_by_id[pool.assign_to_slots[0]].duration_minutes,
                            "dayRole": next(
                                (
                                    role.role
                                    for role in skeleton.brief.day_roles
                                    if role.day_number
                                    == (slot_by_id[pool.assign_to_slots[0]].day_number if pool.assign_to_slots else 1)
                                ),
                                "",
                            ),
                            # One bounded repair path is exercised only for the
                            # food-led optional family; it is deterministic and
                            # never comes from test scenario IDs or provider data.
                            "repairSuggested": family == "local_food",
                            "routePreference": dict(pool.route_preference or {}),
                            "entityBindingMode": pool.entity_binding_mode,
                            "exactEntity": pool.exact_entity,
                        }
                    )
                    optional_slot = slot_by_id[pool.assign_to_slots[0]]
                    admitted = self._apply_consumer_admission(
                        candidate,
                        brief_id=skeleton.brief.brief_id,
                        pool=pool,
                        slot=optional_slot,
                        family=family,
                        metrics=admission_metrics,
                    )
                    if not admitted:
                        self._capture_completion_candidate(
                            completion_candidates_by_brief[skeleton.brief.brief_id],
                            candidate,
                            family=family,
                            slot_id=str(optional_slot.slot_id),
                        )
                        continue
                    candidates.append(candidate)
                # A family may occur on more than one day. Pool identity, not
                # family identity, owns optimizer capacity; the candidate keeps
                # optionalExperienceFamily for downstream materialization.
                pools[pool.pool_id] = candidates
            soft_pools[skeleton.brief.brief_id] = brief_soft_pools
            optional_pools[skeleton.brief.brief_id] = pools
        brief_ids = [item.brief.brief_id for item in generated.proposals]
        if existing_portfolio_id and focus_brief_id and focus_brief_id in brief_ids:
            # A same-root expansion is an identity-bound request for one planning
            # direction. Evaluating unrelated briefs can append the wrong direction
            # and spends real route-provider budget without serving that choice.
            brief_ids = [focus_brief_id]
        elif focus_brief_id and focus_brief_id in brief_ids:
            brief_ids = [focus_brief_id, *[brief_id for brief_id in brief_ids if brief_id != focus_brief_id]]
        optimizer_started = perf_counter()
        skeleton_by_brief = {item.brief.brief_id: item for item in generated.proposals}
        admitted_required_by_brief: dict[str, dict[str, list[dict[str, Any]]]] = {}
        required_admission_rejections: dict[str, list[str]] = {}
        for brief_id in brief_ids:
            skeleton = skeleton_by_brief[brief_id]
            admitted_pools: dict[str, list[dict[str, Any]]] = {}
            rejection_reasons: list[str] = []
            for occurrence_id, pool_candidates in required.items():
                admitted_candidates: list[dict[str, Any]] = []
                occurrence = occurrence_by_id.get(occurrence_id)
                scoped_candidates = self._required_candidates_for_occurrence(
                    universe=universe,
                    skeleton=skeleton,
                    occurrence=occurrence,
                    fallback_candidates=pool_candidates,
                )
                for raw_candidate in scoped_candidates[: PortfolioRuntimeLimits().beam_width]:
                    try:
                        rebound = self._bind_required_to_brief(
                            {occurrence_id: raw_candidate},
                            skeleton,
                            occurrence_contract=occurrence_contract,
                            admission_metrics=admission_metrics,
                        )
                    except ValueError as exc:
                        rejection_reasons.append(str(exc))
                        continue
                    admitted_candidates.append(rebound[occurrence_id])
                admitted_pools[occurrence_id] = admitted_candidates
            admitted_required_by_brief[brief_id] = admitted_pools
            if rejection_reasons:
                required_admission_rejections[brief_id] = list(dict.fromkeys(rejection_reasons))
        assignments = []
        for brief_index, brief_id in enumerate(brief_ids):
            scoped_assignments = self.optimizer.solve(
                brief_ids=brief_ids[: brief_index + 1],
                required_pools=admitted_required_by_brief[brief_id],
                soft_pools=soft_pools,
                optional_pools=optional_pools,
                limits=PortfolioRuntimeLimits(),
            )
            assignment = next(
                (item for item in reversed(scoped_assignments) if item.brief_id == brief_id),
                None,
            )
            if assignment is not None:
                assignments.append(assignment)
        self.last_staging_metrics["candidatePoolDiagnostics"] = {
            brief_id: {
                "softPoolCandidateCounts": {
                    pool_id: len(candidates) for pool_id, candidates in sorted(soft_pools.get(brief_id, {}).items())
                },
                "optionalPoolCandidateCounts": {
                    pool_id: len(candidates) for pool_id, candidates in sorted(optional_pools.get(brief_id, {}).items())
                },
                "requiredAdmissionRejectedReasons": required_admission_rejections.get(brief_id, []),
            }
            for brief_id in brief_ids
        }
        self.last_staging_metrics["assignmentDiagnostics"] = [
            {
                "briefId": assignment.brief_id,
                "requiredCandidateCount": len(assignment.required),
                "softCandidateCount": len(assignment.soft),
                "optionalCandidateCount": len(assignment.optional),
                "optionalPoolIds": [str(item.get("poolId") or "") for item in assignment.optional],
            }
            for assignment in assignments
        ]
        maximal_hard_subset_used = False
        if not assignments and occurrence_contract is not None:
            for brief_id in brief_ids:
                maximal_required = self._maximal_grounded_required_subset(admitted_required_by_brief[brief_id])
                if not maximal_required:
                    continue
                assignments.extend(
                    self.optimizer.solve(
                        brief_ids=[brief_id],
                        required_pools={
                            occurrence_id: [candidate] for occurrence_id, candidate in maximal_required.items()
                        },
                        soft_pools=soft_pools,
                        optional_pools=optional_pools,
                        limits=PortfolioRuntimeLimits(),
                    )
                )
            maximal_hard_subset_used = bool(assignments)
        optimizer_ms = (perf_counter() - optimizer_started) * 1000
        brief_by_id = {brief_id: skeleton.brief for brief_id, skeleton in skeleton_by_brief.items()}
        candidates: list[PlanCandidate] = []
        partial_timeline_candidates: list[PlanCandidate] = []
        target_shortfalls: list[str] = list(
            dict.fromkeys(
                shortfall
                for brief_id in brief_ids
                for shortfall in self._required_assignment_shortfalls(
                    admitted_required_by_brief[brief_id], occurrence_contract
                )
            )
        )
        route_failures: list[str] = []
        lineage_failures: list[str] = []
        priority_verifier_failures: list[str] = []
        prepared_rows: list[dict[str, Any]] = []
        for index, assignment in enumerate(assignments):
            skeleton = skeleton_by_brief.get(assignment.brief_id)
            if skeleton is None:
                lineage_failures.append(f"{assignment.brief_id}:portfolio_assignment_brief_missing")
                continue
            brief = skeleton.brief
            assignment_optional = assignment.optional[: projections[brief.brief_id].max_optional_segments]
            try:
                bound_required = self._bind_required_to_brief(
                    assignment.required,
                    skeleton,
                    occurrence_contract=occurrence_contract,
                    admission_metrics=admission_metrics,
                )
            except ValueError as exc:
                lineage_failures.append(f"{brief.brief_id}:{exc}")
                continue
            projection = projections[brief.brief_id]
            snapshot = snapshot_builder(
                bound_required,
                assignment.soft,
                assignment_optional,
                brief.brief_id,
            )
            snapshot = self._with_experience_spec_route_contracts(
                snapshot,
                candidates=[
                    *bound_required.values(),
                    *assignment.soft.values(),
                    *assignment_optional,
                ],
            )
            snapshot = self._reconcile_admitted_anchor_materialization(
                snapshot,
                admitted_candidates=[
                    *bound_required.values(),
                    *assignment.soft.values(),
                    *assignment_optional,
                ],
                skeleton=skeleton,
                admission_enforced=self.experience_grounding_v2_mode == "enforce",
            )
            snapshot = self._with_soft_pending_slots(snapshot, skeleton)
            snapshot = ProposalReadinessService.with_density_targets(snapshot)
            snapshot = self._scope_daily_capacity_plan(
                snapshot,
                projection=projection,
            )
            snapshot = {
                **snapshot,
                "portfolioDayAnchorTargets": {str(day): target for day, target in projection.day_anchor_targets},
                "portfolioDensityEvidence": {str(day): list(evidence) for day, evidence in projection.density_evidence},
                "portfolioDensityDecisionSource": density_decision_source,
                "portfolioPlanningProjection": projection.as_lineage(),
                "portfolioTransportPreference": projection.transport_preference,
                "portfolioRequiredCandidateBindings": self._binding_evidence(bound_required),
            }
            route_candidates: list[dict[str, Any]] = []
            if self.route_feasibility_service is not None:
                route_candidates = [
                    candidate
                    for pool_candidates in [
                        *soft_pools.get(brief.brief_id, {}).values(),
                        *optional_pools.get(brief.brief_id, {}).values(),
                    ]
                    for candidate in pool_candidates
                    if isinstance(candidate, dict)
                ]
            prepared_rows.append(
                {
                    "index": index,
                    "assignment": assignment,
                    "brief": brief,
                    "projection": projection,
                    "boundRequired": bound_required,
                    "assignmentOptional": assignment_optional,
                    "snapshot": snapshot,
                    "routeCandidates": route_candidates,
                }
            )

        self.last_pre_route_selected_snapshots = [
            copy.deepcopy(row["snapshot"])
            for row in prepared_rows
            if isinstance(row.get("snapshot"), dict)
        ]

        def report_brief_progress(result, completed: int, total: int) -> None:
            if brief_progress_callback is None:
                return
            row = prepared_rows[result.index]
            brief_progress_callback(
                str(row["brief"].brief_id),
                completed,
                total,
                float(result.duration_ms),
                result.error is None and result.value is not None,
            )

        def prepare_route(row):
            try:
                brief_id = str(row["brief"].brief_id)
                return self.route_feasibility_service.prepare(
                    row["snapshot"],
                    candidate_pools=row["routeCandidates"],
                    city=city or str(row["snapshot"].get("city") or ""),
                    transport_mode=str(row["projection"].transport_preference),
                    allow_nearby_search=True,
                    # Stage A already completed Web discovery and mandatory AMap
                    # re-grounding. Route-only preflight must not silently open a
                    # second non-route provider path when repair is unavailable.
                    allow_web_discovery=False,
                    preview_id=f"{portfolio_id}_{brief_id}",
                )
            except Exception as error:
                budget = current_amap_call_budget()
                if budget is not None:
                    setattr(
                        error,
                        "_portfolio_amap_budget_snapshot",
                        budget.snapshot(),
                    )
                raise

        route_run = (
            self.worker_pool.run(
                prepared_rows,
                prepare_route,
                on_complete=report_brief_progress,
            )
            if self.route_feasibility_service is not None
            else None
        )
        route_results = {
            prepared_rows[result.index]["index"]: result
            for result in (route_run.results if route_run is not None else [])
        }
        self.last_staging_metrics = {
            **self.last_staging_metrics,
            **admission_metrics,
            "experienceGroundingV2Mode": self.experience_grounding_v2_mode,
            "maxConcurrentBriefWorkers": (route_run.max_concurrent_workers if route_run is not None else 0),
            "routePreflightMs": route_run.duration_ms if route_run is not None else 0.0,
            "perBriefDurationMs": [
                round(result.duration_ms, 3) for result in (route_run.results if route_run is not None else [])
            ],
            "repairMs": round(
                sum(
                    float(getattr(result.value, "repair_duration_ms", 0.0) or 0.0)
                    for result in (route_run.results if route_run is not None else [])
                    if result.value is not None
                ),
                3,
            ),
        }
        brief_metrics_by_id: dict[str, dict[str, Any]] = {}
        for brief_index, row in enumerate(prepared_rows):
            brief_id = str(row["brief"].brief_id)
            worker_result = route_results.get(row["index"])
            route_duration_ms = float(worker_result.duration_ms) if worker_result is not None else 0.0
            candidate_count = len(row["boundRequired"]) + len(row["assignment"].soft) + len(row["assignmentOptional"])
            brief_metrics_by_id[brief_id] = {
                "briefId": brief_id,
                "briefIndex": brief_index,
                "status": "processing",
                "durationMs": round(route_duration_ms, 3),
                "candidateCount": candidate_count,
                "routePreflightMs": round(route_duration_ms, 3),
                "routePreflightInvocationCount": 1 if self.route_feasibility_service is not None else 0,
                "routePreflightCallCount": 0,
                "amapAroundCount": 0,
                "amapRouteCount": 0,
                "routeExpectedLegCount": 0,
                "routeCompletedLegCount": 0,
                "routeVerifiedLegCount": 0,
                "routeFailedLegCount": 0,
                "routeProviderCallCount": 0,
                "routeProviderState": "not_required",
                "repairMs": 0.0,
                "repairCallCount": 0,
                "repairAttemptCount": 0,
                "repairProviderCallCount": 0,
                "repairAcceptedCount": 0,
                "structuralRepairCallCount": 0,
                "structuralRepairMs": 0.0,
                "repairAttempted": False,
                "verifierMs": 0.0,
                "verifierPassed": False,
                "reasonCodes": [],
            }

        verifier_ms = 0.0
        for row in prepared_rows:
            index = row["index"]
            assignment = row["assignment"]
            brief = row["brief"]
            projection = row["projection"]
            bound_required = row["boundRequired"]
            assignment_optional = row["assignmentOptional"]
            snapshot = row["snapshot"]
            route_candidates = row["routeCandidates"]
            brief_metric = brief_metrics_by_id[str(brief.brief_id)]
            if self.route_feasibility_service is not None:
                worker_result = route_results.get(index)
                if worker_result is None or worker_result.error is not None or worker_result.value is None:
                    route_failures.append(f"{brief.brief_id}:route_preflight_worker_failed")
                    brief_metric["reasonCodes"].append("route_preflight_worker_failed")
                    brief_metric["routeProviderState"] = "worker_error"
                    snapshot = copy.deepcopy(snapshot)
                    retained_evidence = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
                    snapshot["portfolioRouteVerificationRequired"] = True
                    snapshot["routeOptions"] = copy.deepcopy(retained_evidence)
                    snapshot["portfolioRouteEvidence"] = copy.deepcopy(retained_evidence)
                    snapshot["routeEvidence"] = copy.deepcopy(retained_evidence)
                    expected_legs = sum(
                        max(
                            0,
                            len(
                                [
                                    segment
                                    for segment in day.get("segments") or []
                                    if isinstance(segment, dict)
                                    and isinstance(segment.get("semanticMetadata"), dict)
                                    and segment["semanticMetadata"].get("routeAnchor")
                                ]
                            )
                            - 1,
                        )
                        for day in snapshot.get("days") or []
                        if isinstance(day, dict)
                    )
                    verified_legs = len(
                        [item for item in retained_evidence if ProposalRouteEvidenceNormalizer.is_verified(item)]
                    )
                    failure_class = (
                        str(worker_result.error_class or type(worker_result.error).__name__)
                        if worker_result is not None and worker_result.error is not None
                        else "WorkerResultMissing"
                    )
                    failure_message = (
                        str(worker_result.sanitized_error_message or failure_class)
                        if worker_result is not None
                        else "route_preflight_worker_result_missing"
                    )
                    budget_snapshot = (
                        getattr(
                            worker_result.error,
                            "_portfolio_amap_budget_snapshot",
                            {},
                        )
                        if worker_result is not None and worker_result.error is not None
                        else {}
                    )
                    provider_call_count = int(budget_snapshot.get("usedRoute") or 0)
                    provider_cache_hit_count = int(budget_snapshot.get("cacheHitCount") or 0)
                    route_ledger = {
                        "expectedLegCount": expected_legs,
                        "submittedLegCount": provider_call_count,
                        "completedLegCount": verified_legs,
                        "verifiedLegCount": verified_legs,
                        "failedLegCount": max(0, expected_legs - verified_legs),
                        "retainedEvidenceCount": len(retained_evidence),
                        "providerCallCount": provider_call_count,
                        "providerCacheHitCount": provider_cache_hit_count,
                        "cancelledLegCount": 0,
                        "workerFailureClass": failure_class,
                        "sanitizedWorkerFailureMessage": failure_message,
                        "workerTimedOut": failure_class == "TimeoutError",
                        "workerTimeoutMs": None,
                    }
                    brief_metric.update(
                        {
                            "routePreflightCallCount": provider_call_count,
                            "amapRouteCount": provider_call_count,
                            "routeProviderCallCount": provider_call_count,
                            "providerCacheHitCount": provider_cache_hit_count,
                            "routeExpectedLegCount": expected_legs,
                            "routeCompletedLegCount": verified_legs,
                            "routeVerifiedLegCount": verified_legs,
                            "routeFailedLegCount": max(0, expected_legs - verified_legs),
                        }
                    )
                    snapshot["portfolioRouteQuality"] = {
                        "status": "provider_error",
                        "providerState": "worker_error",
                        "routeQualityIssues": [
                            {
                                "code": "route_preflight_worker_failed",
                                "workerFailureClass": failure_class,
                                "sanitizedWorkerFailureMessage": failure_message,
                            }
                        ],
                        "warnings": [
                            "route preflight worker failed; retained verified AMap anchors without route evidence"
                        ],
                        "executionLedger": route_ledger,
                    }
                    brief_metric["routeQualityIssueDetails"] = copy.deepcopy(
                        snapshot["portfolioRouteQuality"]["routeQualityIssues"]
                    )
                else:
                    route_result = worker_result.value
                    snapshot = route_result.snapshot
                    route_ledger = dict(route_result.route_execution_ledger or {})
                    brief_metric["routeProviderState"] = str(route_result.provider_state)
                    brief_metric["routePreflightCallCount"] = int(
                        route_ledger.get("providerCallCount") or route_result.route_call_count
                    )
                    brief_metric["amapRouteCount"] = int(
                        route_ledger.get("providerCallCount") or route_result.route_call_count
                    )
                    brief_metric["routeProviderCallCount"] = int(route_ledger.get("providerCallCount") or 0)
                    brief_metric["routeExpectedLegCount"] = int(route_ledger.get("expectedLegCount") or 0)
                    brief_metric["routeCompletedLegCount"] = int(route_ledger.get("completedLegCount") or 0)
                    brief_metric["routeVerifiedLegCount"] = int(route_ledger.get("verifiedLegCount") or 0)
                    brief_metric["routeFailedLegCount"] = int(route_ledger.get("failedLegCount") or 0)
                    brief_metric["amapAroundCount"] = int(route_result.nearby_search_count)
                    repair_ledger = dict(route_result.repair_attempt_ledger or {})
                    brief_metric["repairAttemptLedger"] = copy.deepcopy(repair_ledger)
                    brief_metric["repairAttemptCount"] = int(repair_ledger.get("candidateEvaluationCount") or 0)
                    # A completed candidate matrix can contain multiple
                    # Provider route legs.  Keep its count distinct from the
                    # Provider-only repair-call delta emitted by feasibility.
                    brief_metric["repairCallCount"] = int(
                        repair_ledger.get("repairCallCount")
                        or repair_ledger.get("providerCallCount")
                        or 0
                    )
                    brief_metric["repairProviderCallCount"] = int(repair_ledger.get("providerCallCount") or 0)
                    brief_metric["repairAcceptedCount"] = int(repair_ledger.get("acceptedReplacementCount") or 0)
                    brief_metric["repairAttempted"] = brief_metric["repairAttemptCount"] > 0
                    brief_metric["repairMs"] = round(
                        float(route_result.repair_duration_ms or 0.0),
                        3,
                    )
                    route_failures.extend(
                        f"{brief.brief_id}:{item.get('code') or item.get('message') or 'route_quality_failed'}"
                        for item in route_result.route_quality_issues
                    )
                    brief_metric["routeQualityIssueDetails"] = [
                        self._route_quality_issue_detail(item)
                        for item in route_result.route_quality_issues
                        if isinstance(item, dict)
                    ]
                brief_metric["routeExecutionLedger"] = copy.deepcopy(route_ledger)
            completion_pool = completion_candidates_by_brief.get(brief.brief_id) or {}
            if str(getattr(brief, "primary_axis", "") or "") == "food_led":
                snapshot["portfolioThemeCompletionCandidates"] = copy.deepcopy(completion_pool)
            snapshot = self._with_output_quality(snapshot, brief)
            brief_metric["outputQuality"] = copy.deepcopy(snapshot.get("portfolioOutputQuality") or {})
            verifier_started = perf_counter()
            verifier = self.verifier.verify(snapshot, ledger, brief)
            snapshot["portfolioVerifier"] = copy.deepcopy(verifier)
            brief_verifier_ms = (perf_counter() - verifier_started) * 1000
            verifier_ms += brief_verifier_ms
            brief_metric["verifierMs"] = round(brief_verifier_ms, 3)
            brief_metric["verifierPassed"] = bool(verifier.get("passed"))
            brief_metric["draftVerifierPassed"] = bool(verifier.get("draftPassed"))
            diagnostic = {
                "briefId": brief.brief_id,
                "passed": bool(verifier.get("passed")),
                "draftPassed": bool(verifier.get("draftPassed")),
                "dayAnchorTargets": copy.deepcopy(verifier.get("dayAnchorTargets") or {}),
                "dayAnchorActuals": copy.deepcopy(verifier.get("dayAnchorActuals") or {}),
                "dayAnchorShortfalls": list(verifier.get("dayAnchorShortfalls") or []),
                "hardFailures": list(verifier.get("hardFailures") or []),
                "routeCoverageFailures": list(verifier.get("routeCoverageFailures") or []),
                "routeQualityFailures": list(verifier.get("routeQualityFailures") or []),
                "routeQualityIssueDetails": copy.deepcopy(brief_metric.get("routeQualityIssueDetails") or []),
                "pendingSlotCount": len(snapshot.get("portfolioPendingSlots") or []),
            }
            self.last_staging_metrics.setdefault("perBriefDiagnostics", []).append(diagnostic)
            target_shortfalls.extend(str(item) for item in verifier.get("dayAnchorShortfalls") or [])
            score = self.scorer.score(snapshot=snapshot, ledger=ledger, brief=brief, verifier=verifier)
            candidate = PlanCandidate(
                proposalId=f"proposal_{uuid4().hex[:16]}",
                portfolioId=portfolio_id,
                brief=brief,
                itinerarySnapshot=snapshot,
                groundedEvidence=[*bound_required.values(), *assignment.soft.values(), *assignment_optional],
                score=score,
                verifier=verifier,
                # Narrative labels are deliberately excluded.  A portfolio can
                # only retain variants whose grounded itinerary differs.
                canonicalSignature=self.canonical_signature_for_snapshot(snapshot),
                generationLineage={
                    "optimizer": "bounded_portfolio_optimizer",
                    "assignmentIndex": index,
                    "maximalHardSubset": maximal_hard_subset_used,
                    "briefPlanningProjection": projections[brief.brief_id].as_lineage(),
                    "softCandidateCount": len(assignment.soft),
                    "optionalCandidateCount": len(assignment_optional),
                    "portfolioRequiredCandidateBindings": self._binding_evidence(bound_required),
                    "repair": {"attempted": False, "providerCalls": 0},
                    "providerParser": dict(generated.parser_metadata),
                },
            )
            diagnostic.update(
                {
                    "blockingPartialFailures": self._blocking_partial_failures(candidate),
                    "hardGapContractEligible": self._hard_gap_contract(candidate) is not None,
                }
            )
            defects = self.critic.review(candidate, ledger)
            repairable = [
                item for item in defects if item.repair_scope in {"remove_optional", "move_or_remove_optional"}
            ]
            hard_defects = [item for item in defects if item.repair_scope == "reject"]
            if self.soft_slot_draft_adoption_enabled and candidate.verifier.get("draftPassed"):
                hard_defects = [item for item in hard_defects if item.defect_type != "hard_constraint"]
            repair_requested = any(
                bool((segment.get("semanticMetadata") or {}).get("repairSuggested"))
                for day in snapshot.get("days") or []
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
            )
            if repairable and repair_requested and not hard_defects:
                repair_started = perf_counter()
                brief_metric["structuralRepairCallCount"] += 1
                repaired_snapshot, repair_lineage = self.repair.repair_once(snapshot, repairable)
                brief_metric["structuralRepairMs"] = round(
                    float(brief_metric["structuralRepairMs"]) + (perf_counter() - repair_started) * 1000,
                    3,
                )
                if repaired_snapshot is None:
                    brief_metric["status"] = "failed"
                    brief_metric["reasonCodes"].append("bounded_repair_failed")
                    continue
                if self.route_feasibility_service is not None:
                    repair_route_started = perf_counter()
                    route_result = self.route_feasibility_service.prepare(
                        repaired_snapshot,
                        candidate_pools=route_candidates,
                        city=city or str(repaired_snapshot.get("city") or ""),
                        transport_mode=str(projection.transport_preference),
                        allow_nearby_search=True,
                        allow_web_discovery=False,
                        preview_id=f"{portfolio_id}_{brief.brief_id}_repair",
                    )
                    repaired_snapshot = route_result.snapshot
                    brief_metric["routePreflightInvocationCount"] += 1
                    brief_metric["routePreflightMs"] = round(
                        float(brief_metric["routePreflightMs"]) + (perf_counter() - repair_route_started) * 1000,
                        3,
                    )
                    brief_metric["routePreflightCallCount"] += int(route_result.route_call_count)
                    brief_metric["amapRouteCount"] += int(route_result.route_call_count)
                    brief_metric["amapAroundCount"] += int(route_result.nearby_search_count)
                    brief_metric["routeProviderState"] = str(route_result.provider_state)
                    brief_metric["repairMs"] = round(
                        float(brief_metric["repairMs"]) + float(route_result.repair_duration_ms or 0.0),
                        3,
                    )
                    second_repair_ledger = dict(route_result.repair_attempt_ledger or {})
                    brief_metric["repairAttemptCount"] += int(second_repair_ledger.get("candidateEvaluationCount") or 0)
                    brief_metric["repairCallCount"] += int(
                        second_repair_ledger.get("repairCallCount")
                        or second_repair_ledger.get("providerCallCount")
                        or 0
                    )
                    brief_metric["repairProviderCallCount"] += int(second_repair_ledger.get("providerCallCount") or 0)
                    brief_metric["repairAcceptedCount"] += int(
                        second_repair_ledger.get("acceptedReplacementCount") or 0
                    )
                    brief_metric["repairAttempted"] = brief_metric["repairAttemptCount"] > 0
                repaired_snapshot = ProposalReadinessService.with_density_targets(repaired_snapshot)
                repaired_snapshot = self._with_output_quality(repaired_snapshot, brief)
                verifier_started = perf_counter()
                verifier = self.verifier.verify(repaired_snapshot, ledger, brief)
                repaired_snapshot["portfolioVerifier"] = copy.deepcopy(verifier)
                repair_verifier_ms = (perf_counter() - verifier_started) * 1000
                verifier_ms += repair_verifier_ms
                brief_metric["verifierMs"] = round(
                    float(brief_metric["verifierMs"]) + repair_verifier_ms,
                    3,
                )
                brief_metric["verifierPassed"] = bool(verifier.get("passed"))
                brief_metric["draftVerifierPassed"] = bool(verifier.get("draftPassed"))
                target_shortfalls.extend(str(item) for item in verifier.get("dayAnchorShortfalls") or [])
                score = self.scorer.score(snapshot=repaired_snapshot, ledger=ledger, brief=brief, verifier=verifier)
                candidate = candidate.model_copy(
                    update={
                        "brief": brief,
                        "itinerary_snapshot": repaired_snapshot,
                        "score": score,
                        "verifier": verifier,
                        "canonical_signature": self.canonical_signature_for_snapshot(repaired_snapshot),
                        "generation_lineage": {**candidate.generation_lineage, "repair": repair_lineage},
                    }
                )
                defects = self.critic.review(candidate, ledger)
                diagnostic.update(
                    {
                        "passed": bool(verifier.get("passed")),
                        "draftPassed": bool(verifier.get("draftPassed")),
                        "dayAnchorTargets": copy.deepcopy(verifier.get("dayAnchorTargets") or {}),
                        "dayAnchorActuals": copy.deepcopy(verifier.get("dayAnchorActuals") or {}),
                        "dayAnchorShortfalls": list(verifier.get("dayAnchorShortfalls") or []),
                        "hardFailures": list(verifier.get("hardFailures") or []),
                        "routeCoverageFailures": list(verifier.get("routeCoverageFailures") or []),
                        "routeQualityFailures": list(verifier.get("routeQualityFailures") or []),
                        "pendingSlotCount": len(repaired_snapshot.get("portfolioPendingSlots") or []),
                        "blockingPartialFailures": self._blocking_partial_failures(candidate),
                        "hardGapContractEligible": self._hard_gap_contract(candidate) is not None,
                    }
                )
            # Critic findings with an optional-only repair scope are advisory
            # after the single bounded repair; the deterministic verifier is
            # the final feasibility authority.
            priority_verifier_failures.extend(
                f"{brief.brief_id}:{failure}" for failure in self._priority_portfolio_failures(candidate)
            )
            if (
                not maximal_hard_subset_used
                and not hard_defects
                and (
                    candidate.verifier.get("passed")
                    or (self.soft_slot_draft_adoption_enabled and candidate.verifier.get("draftPassed"))
                )
            ):
                candidates.append(candidate)
                brief_metric["status"] = "completed"
            elif self._partial_timeline_eligible(candidate):
                brief_metric["status"] = "partial"
                partial_timeline_candidates.append(
                    candidate.model_copy(
                        update={
                            "generation_lineage": {
                                **candidate.generation_lineage,
                                "partialTimeline": {
                                    "eligible": True,
                                    "reason": self._partial_timeline_reason(candidate),
                                    "strictProposalVerifierPassed": False,
                                },
                            }
                        }
                    )
                )
            else:
                brief_metric["status"] = "failed"
            final_reason_codes = list(
                dict.fromkeys(
                    [*brief_metric["reasonCodes"]]
                    + [
                        str(item)
                        for key in (
                            "hardFailures",
                            "dayAnchorShortfalls",
                            "routeCoverageFailures",
                            "routeQualityFailures",
                        )
                        for item in diagnostic.get(key) or []
                        if str(item)
                    ]
                )
            )
            brief_metric["reasonCodes"] = final_reason_codes
            brief_metric["durationMs"] = round(
                float(brief_metric["routePreflightMs"])
                + float(brief_metric["repairMs"])
                + float(brief_metric["verifierMs"]),
                3,
            )
        if focus_brief_id:
            partial_timeline_candidates = [
                candidate for candidate in partial_timeline_candidates if candidate.brief.brief_id == focus_brief_id
            ]
        if partial_timeline_candidates:
            self.partial_timeline_candidate = max(
                partial_timeline_candidates,
                key=self._partial_timeline_sort_key,
            )
        if existing_portfolio_id:
            stored_prior_projections = self.store.visible_comparison_projections(
                portfolio_id=existing_portfolio_id,
            )
        else:
            stored_prior_projections = []
        reserved_titles = {
            str(projection.get("title") or "").strip()
            for projection in [
                *list(prior_comparison_projections or []),
                *stored_prior_projections,
            ]
            if isinstance(projection, dict) and str(projection.get("title") or "").strip()
        }
        candidates = self._ensure_agent_generated_titles(
            candidates,
            city=city,
            reserved_titles=reserved_titles,
        )
        visible = self.selector.select(candidates)
        unique_visible: list[PlanCandidate] = []
        visible_titles: set[str] = set(reserved_titles)
        title_collision_rejected_ids: list[str] = []
        for candidate in visible:
            title = str(candidate.itinerary_snapshot.get("title") or "").strip()
            title_generation = (
                candidate.itinerary_snapshot.get("portfolioTitleGeneration")
                if isinstance(candidate.itinerary_snapshot.get("portfolioTitleGeneration"), dict)
                else {}
            )
            title_is_final = title_generation.get("status") == "succeeded"
            if title_is_final and title and title in visible_titles:
                title_collision_rejected_ids.append(candidate.proposal_id)
                continue
            if title_is_final and title:
                visible_titles.add(title)
            unique_visible.append(candidate)
        visible = unique_visible
        self.last_staging_metrics["titleCollisionRejectedProposalIds"] = title_collision_rejected_ids
        material_novelty_audits: dict[str, dict[str, Any]] = {}
        prior_projections: list[dict[str, Any]] = []
        prior_projection_keys: set[str] = set()
        for projection in [
            *list(prior_comparison_projections or []),
            *stored_prior_projections,
        ]:
            if not isinstance(projection, dict):
                continue
            physical_ids = PlanComparisonPreviewService.physical_poi_ids(projection)
            _, concept_facts = PlanComparisonPreviewService.concept_signature(projection)
            if not physical_ids and not concept_facts:
                continue
            proposal_id = str(projection.get("proposalId") or "")
            key = proposal_id or "physical:" + "|".join(sorted(physical_ids))
            if key in prior_projection_keys:
                continue
            prior_projection_keys.add(key)
            prior_projections.append(copy.deepcopy(projection))
        if prior_projections:
            accepted_visible: list[PlanCandidate] = []
            comparison_baseline = list(prior_projections)
            diagnostics_by_brief = {
                str(item.get("briefId") or ""): item
                for item in self.last_staging_metrics.get("perBriefDiagnostics") or []
                if isinstance(item, dict) and str(item.get("briefId") or "")
            }
            for candidate in visible:
                candidate_projection = {
                    "proposalId": candidate.proposal_id,
                    "days": candidate.itinerary_snapshot.get("days") or [],
                    "portfolioPendingSlots": candidate.itinerary_snapshot.get("portfolioPendingSlots") or [],
                }
                is_editable_draft = bool(candidate.verifier.get("draftPassed") and not candidate.verifier.get("passed"))
                # Concept axes remain useful for discovery, but a formal card
                # is user-visible only when its admitted AMap anchors make the
                # physical itinerary materially different. Editable drafts are
                # not exempt: pending labels cannot manufacture a new plan.
                novelty_audit = PlanComparisonPreviewService.material_novelty_audit(
                    candidate_projection,
                    comparison_baseline,
                )
                novelty_audit["conceptNoveltyScore"] = 0.0
                novelty_audit["physicalNoveltyScore"] = 1.0 if novelty_audit.get("passed") else 0.0
                material_novelty_audits[candidate.proposal_id] = novelty_audit
                if novelty_audit.get("passed") is True:
                    accepted_visible.append(candidate)
                    comparison_baseline.append(candidate_projection)
                    continue
                brief_id = str(candidate.brief.brief_id or "")
                metric = brief_metrics_by_id.get(brief_id)
                if metric is not None:
                    metric["status"] = "failed"
                    metric["reasonCodes"] = list(
                        dict.fromkeys(
                            [
                                *list(metric.get("reasonCodes") or []),
                                (
                                    "partial_preview_not_materially_distinct"
                                    if is_editable_draft
                                    else "full_proposal_not_materially_distinct"
                                ),
                            ]
                        )
                    )
                diagnostic = diagnostics_by_brief.get(brief_id)
                if diagnostic is not None:
                    diagnostic["noveltyFailures"] = [
                        "partial_preview_not_materially_distinct"
                        if is_editable_draft
                        else "full_proposal_not_materially_distinct"
                    ]
                    diagnostic["materialNoveltyAudit"] = copy.deepcopy(novelty_audit)
            visible = accepted_visible
        visible_ids = {item.proposal_id for item in visible}
        candidates = [
            item.model_copy(
                update={
                    "generation_lineage": {
                        **item.generation_lineage,
                        "paretoSelection": {
                            "selector": "pareto_portfolio_selector",
                            "eligible": True,
                            "visible": item.proposal_id in visible_ids,
                            "visibleProposalIds": sorted(visible_ids),
                            "structuralDistancesToVisible": {
                                selected.proposal_id: self.selector.distance(item, selected)
                                for selected in visible
                                if selected.proposal_id != item.proposal_id
                            },
                            "materialNoveltyAudit": copy.deepcopy(material_novelty_audits.get(item.proposal_id)),
                            "conceptNoveltyScore": float(
                                (material_novelty_audits.get(item.proposal_id) or {}).get("conceptNoveltyScore", 0.0)
                            ),
                            "physicalNoveltyScore": float(
                                (material_novelty_audits.get(item.proposal_id) or {}).get("physicalNoveltyScore", 0.0)
                            ),
                        },
                    }
                }
            )
            for item in candidates
        ]
        candidates_by_id = {item.proposal_id: item for item in candidates}
        visible = [candidates_by_id[item.proposal_id] for item in visible]
        portfolio = PlanPortfolio(
            portfolioId=portfolio_id,
            sessionId=session_id,
            sourceUserTurnId=source_user_turn_id,
            sourceAssistantTurnId=source_assistant_turn_id,
            expectedBaseVersionId=expected_base_version_id,
            sourceObservationFingerprint=observation_fingerprint,
            requestContractFingerprint=request_fingerprint,
            status="awaiting_selection" if visible else "failed",
            proposalIds=[item.proposal_id for item in candidates],
            visibleProposalIds=[item.proposal_id for item in visible],
            failureReason=(
                None
                if visible
                else "portfolio_verifier_hard_failure:" + ";".join(sorted(set(priority_verifier_failures)))
                if priority_verifier_failures
                else "portfolio_anchor_target_shortfall:" + ";".join(sorted(set(target_shortfalls)))
                if target_shortfalls
                else "portfolio_route_quality_unresolved:" + ";".join(sorted(set(route_failures)))
                if route_failures
                else "portfolio_required_candidate_lineage_invalid:" + ";".join(sorted(set(lineage_failures)))
                if lineage_failures
                else "all_portfolio_proposals_infeasible"
            ),
        )
        self._last_staged_candidates = list(candidates)
        self.last_staging_metrics.update(
            {
                "briefMetrics": sorted(
                    brief_metrics_by_id.values(),
                    key=lambda item: int(item["briefIndex"]),
                ),
                "optimizerMs": round(optimizer_ms, 3),
                "verifierMs": round(verifier_ms, 3),
                "stagingTotalMs": round((perf_counter() - stage_started) * 1000, 3),
                "visibleProposalCount": len(visible),
                "visibleProposalBriefIds": [item.brief.brief_id for item in visible],
                "sameDayDuplicatePoiCount": sum(
                    self._same_day_duplicate_poi_count(item.itinerary_snapshot) for item in visible
                ),
                "crossDayReuseCount": sum(self._cross_day_reuse_count(item.itinerary_snapshot) for item in visible),
            }
        )
        if persist_result:
            portfolio, visible = self.persist_last_stage(portfolio, visible)
        return portfolio, visible

    @classmethod
    def _capture_completion_candidate(
        cls,
        target: dict[str, Any],
        candidate: dict[str, Any],
        *,
        family: str,
        slot_id: str,
    ) -> None:
        report = candidate.get("consumerAdmissionReport")
        classification = str(report.get("classification") or "") if isinstance(report, dict) else ""
        if classification not in {"pending_evidence", "area_seed_only"}:
            return
        payload = cls._completion_candidate_payload(candidate)
        if not payload:
            return
        if family in {"meal", "local_food", "food"}:
            slots = target.setdefault("mealCandidatesBySlot", {})
            rows = slots.setdefault(slot_id, [])
            if len(rows) < 2 and not any(
                str(item.get("amapId") or "") == str(payload.get("amapId") or "") for item in rows
            ):
                rows.append(payload)
            return
        if family in CreativeOutputQualityService.AREA_FAMILIES:
            rows = target.setdefault("areaCandidates", [])
            if len(rows) < 4 and not any(
                str(item.get("amapId") or "") == str(payload.get("amapId") or "") for item in rows
            ):
                rows.append(payload)

    @staticmethod
    def _completion_candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
        blocked_fragments = (
            "query",
            "url",
            "header",
            "authorization",
            "cookie",
            "apikey",
            "api_key",
            "secret",
            "token",
            "proxy",
        )

        def project(value: Any, *, depth: int = 0) -> Any:
            if depth > 4:
                return None
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for key, item in value.items():
                    normalized = str(key).replace("-", "_").casefold()
                    if any(fragment in normalized for fragment in blocked_fragments):
                        continue
                    projected = project(item, depth=depth + 1)
                    if projected is not None:
                        result[str(key)] = projected
                return result
            if isinstance(value, list):
                return [project(item, depth=depth + 1) for item in value[:8]]
            if isinstance(value, (str, int, float, bool)) or value is None:
                text = str(value) if isinstance(value, str) else value
                return text[:500] if isinstance(text, str) else text
            return str(value)[:500]

        payload = project(candidate)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _route_quality_issue_detail(issue: dict[str, Any]) -> dict[str, Any]:
        """Retain only the safe route failure contract in exported trace."""

        allowed = (
            "code",
            "dayNumber",
            "segmentId",
            "mealSegmentId",
            "previousSegmentId",
            "nextSegmentId",
            "generalizedCostDelta",
            "detourRatio",
            "addedDistanceMeters",
            "addedDurationMinutes",
            "timeWindowFeasible",
            "assessmentBasis",
            "workerFailureClass",
            "sanitizedWorkerFailureMessage",
            "workerTimedOut",
            "workerTimeoutMs",
        )
        return {key: copy.deepcopy(issue[key]) for key in allowed if key in issue and issue[key] is not None}

    @staticmethod
    def _same_day_duplicate_poi_count(snapshot: dict[str, Any]) -> int:
        duplicates = 0
        for day in snapshot.get("days") or []:
            seen: set[str] = set()
            for segment in day.get("segments") or []:
                poi = segment.get("poi") if isinstance(segment, dict) else None
                identity = PoiPhysicalIdentityService.canonical_amap_id(poi or {})
                if not identity:
                    continue
                if identity in seen:
                    duplicates += 1
                seen.add(identity)
        return duplicates

    @staticmethod
    def _cross_day_reuse_count(snapshot: dict[str, Any]) -> int:
        first_day: dict[str, int] = {}
        reused: set[str] = set()
        for day in snapshot.get("days") or []:
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                poi = segment.get("poi") if isinstance(segment, dict) else None
                identity = PoiPhysicalIdentityService.canonical_amap_id(poi or {})
                if not identity:
                    continue
                if identity in first_day and first_day[identity] != day_number:
                    reused.add(identity)
                first_day.setdefault(identity, day_number)
        return len(reused)

    def _maximal_grounded_required_subset(
        self,
        required: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        """Return one maximum trusted hard matching for a repeated-goal gap.

        The strict optimizer remains authoritative for offerable proposals. This
        recovery path may retain the distinct subset when one canonical AMap
        identity appears in multiple occurrence pools.  It never reuses that
        identity: one occurrence receives the real POI and the other remains an
        explicit pending slot.  Invalid candidates still cannot enter the
        subset, and a full matching continues through the strict optimizer.
        """

        if len(required) < 2:
            return {}
        trusted: dict[str, list[dict[str, Any]]] = {}
        for occurrence_id, candidates in required.items():
            trusted[occurrence_id] = sorted(
                [
                    dict(candidate)
                    for candidate in candidates[: PortfolioRuntimeLimits().beam_width]
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("sourcePrecheck"), dict)
                    and candidate["sourcePrecheck"].get("passed") is True
                    and self.verifier._has_grounded_identity(candidate)
                ],
                key=lambda candidate: (
                    -float(candidate.get("localRouteScore") or 0),
                    float(candidate.get("estimatedCost") or 0),
                    str(candidate.get("amapId") or candidate.get("id") or ""),
                ),
            )
        identity_owner: dict[str, str] = {}
        selected: dict[str, dict[str, Any]] = {}

        def repeated_goal_key(occurrence_id: str) -> str:
            return str(occurrence_id).rsplit(":day:", 1)[0]

        def assign(occurrence_id: str, visited: set[str]) -> bool:
            for candidate in trusted.get(occurrence_id, []):
                identity = PoiPhysicalIdentityService.canonical_amap_id(candidate)
                if not identity or identity in visited:
                    continue
                visited.add(identity)
                owner = identity_owner.get(identity)
                if owner is None or assign(owner, visited):
                    identity_owner[identity] = occurrence_id
                    selected[occurrence_id] = candidate
                    return True
            return False

        for occurrence_id in sorted(
            trusted,
            key=lambda key: (len(trusted[key]), key),
        ):
            assign(occurrence_id, set())
        if not selected or set(selected) >= set(required):
            return {}
        for occurrence_id in set(required) - set(selected):
            candidates = trusted.get(occurrence_id, [])
            if not candidates:
                continue
            owners = {
                identity_owner.get(PoiPhysicalIdentityService.canonical_amap_id(candidate), "")
                for candidate in candidates
            }
            if not owners or any(
                not owner or repeated_goal_key(owner) != repeated_goal_key(occurrence_id)
                for owner in owners
            ):
                return {}
        return selected

    @staticmethod
    def _explicit_soft_gap_has_exact_pending_slot(
        candidate: PlanCandidate,
        failure: str,
    ) -> bool:
        """Prove that one missing soft occurrence has an authoritative slot.

        The strict proposal verifier intentionally keeps the occurrence as a
        hard failure.  Partial publication may recover it only when the
        server-compiled occurrence plan classifies it as ``explicit_soft`` and
        the final snapshot retains a fully scoped pending slot for the same
        goal and day.  Labels and POI names are deliberately irrelevant.
        """

        snapshot = candidate.itinerary_snapshot
        occurrences = [
            item
            for item in ((snapshot.get("portfolioGoalOccurrencePlan") or {}).get("occurrences") or [])
            if isinstance(item, dict)
        ]
        pending_slots = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        brief_id = str(candidate.brief.brief_id or "")
        for occurrence in occurrences:
            occurrence_id = str(occurrence.get("occurrenceId") or "")
            source_goal_id = str(occurrence.get("sourceGoalId") or "")
            try:
                day_number = int(occurrence.get("dayNumber") or 0)
            except (TypeError, ValueError):
                continue
            expected_failure = f"goal_occurrence_missing:{occurrence_id}:day_{day_number}"
            if (
                not occurrence_id
                or not source_goal_id
                or day_number <= 0
                or str(occurrence.get("requirementLevel") or "") != "explicit_soft"
                or failure != expected_failure
            ):
                continue
            for slot in pending_slots:
                try:
                    slot_day_number = int(slot.get("dayNumber") or 0)
                except (TypeError, ValueError):
                    continue
                if (
                    str(slot.get("briefId") or "") == brief_id
                    and bool(str(slot.get("poolId") or ""))
                    and bool(str(slot.get("planningSlotId") or ""))
                    and str(slot.get("occurrenceId") or "") == occurrence_id
                    and str(slot.get("sourceGoalId") or "") == source_goal_id
                    and slot_day_number == day_number
                ):
                    return True
            return False
        return False

    @staticmethod
    def _hard_gap_contract(
        candidate: PlanCandidate,
    ) -> tuple[dict[str, dict[str, Any]], set[str]] | None:
        """Validate a strict hard-occurrence subset and all missing identities."""

        snapshot = candidate.itinerary_snapshot
        raw_plan = snapshot.get("portfolioGoalOccurrencePlan")
        if not isinstance(raw_plan, dict):
            return None
        expected: dict[str, dict[str, Any]] = {}
        for occurrence in raw_plan.get("occurrences") or []:
            if not isinstance(occurrence, dict):
                return None
            if str(occurrence.get("requirementLevel") or "") != "hard":
                continue
            occurrence_id = str(occurrence.get("occurrenceId") or "")
            source_goal_id = str(occurrence.get("sourceGoalId") or "")
            intent_type = str(occurrence.get("intentType") or "")
            try:
                day_number = int(occurrence.get("dayNumber") or 0)
            except (TypeError, ValueError):
                return None
            if (
                not occurrence_id
                or occurrence_id in expected
                or not source_goal_id
                or not intent_type
                or day_number <= 0
            ):
                return None
            expected[occurrence_id] = {
                "sourceGoalId": source_goal_id,
                "intentType": intent_type,
                "dayNumber": day_number,
            }
        if len(expected) < 2:
            return None

        brief_id = str(candidate.brief.brief_id or "")
        bindings = [item for item in snapshot.get("portfolioRequiredCandidateBindings") or [] if isinstance(item, dict)]
        binding_ids = [str(item.get("occurrenceId") or "") for item in bindings]
        binding_set = set(binding_ids)
        if not brief_id or not binding_set or len(binding_ids) != len(binding_set) or not binding_set < set(expected):
            return None
        for binding in bindings:
            occurrence_id = str(binding.get("occurrenceId") or "")
            occurrence = expected.get(occurrence_id)
            if occurrence is None:
                return None
            try:
                binding_day = int(binding.get("dayNumber") or 0)
            except (TypeError, ValueError):
                return None
            if (
                str(binding.get("briefId") or "") != brief_id
                or not str(binding.get("poolId") or "")
                or not str(binding.get("planningSlotId") or "")
                or not str(binding.get("amapId") or "")
                or str(binding.get("candidateSource") or "") != "amap-place-search"
                or str(binding.get("sourceGoalId") or "") != occurrence["sourceGoalId"]
                or str(binding.get("intentType") or "") != occurrence["intentType"]
                or binding_day != occurrence["dayNumber"]
            ):
                return None

        missing = {
            occurrence_id: occurrence
            for occurrence_id, occurrence in expected.items()
            if occurrence_id not in binding_set
        }
        hard_pending = [
            item
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and str(item.get("requirementLevel") or "") in {"hard", "required"}
        ]
        if len(hard_pending) != len(missing):
            return None
        pending_ids: set[str] = set()
        for pending in hard_pending:
            occurrence_id = str(pending.get("occurrenceId") or "")
            occurrence = missing.get(occurrence_id)
            if occurrence is None or occurrence_id in pending_ids:
                return None
            pending_ids.add(occurrence_id)
            try:
                pending_day = int(pending.get("dayNumber") or 0)
            except (TypeError, ValueError):
                return None
            if (
                str(pending.get("briefId") or "") != brief_id
                or not str(pending.get("poolId") or "")
                or not str(pending.get("planningSlotId") or "")
                or str(pending.get("sourceGoalId") or "") != occurrence["sourceGoalId"]
                or pending_day != occurrence["dayNumber"]
                or (
                    pending.get("intentType") is not None
                    and str(pending.get("intentType") or "") != occurrence["intentType"]
                )
            ):
                return None
        if pending_ids != set(missing):
            return None

        verifier = candidate.verifier
        lineage_failures = {str(item) for item in verifier.get("requiredCandidateLineageFailures") or []}
        if (
            int(verifier.get("requiredCandidateBindingExpectedCount") or 0) != len(expected)
            or int(verifier.get("requiredCandidateBindingActualCount") or 0) != len(binding_set)
            or int(verifier.get("hardCandidateLineageMissingCount") or 0) != 0
            or int(verifier.get("hardCandidateCrossBriefLeakCount") or 0) != 0
            or lineage_failures != {"required_candidate_binding_occurrence_set_mismatch"}
        ):
            return None
        occurrence_coverage = verifier.get("goalOccurrenceCoverage")
        if not isinstance(occurrence_coverage, dict) or any(
            bool(occurrence_coverage.get(occurrence_id)) != (occurrence_id in binding_set) for occurrence_id in expected
        ):
            return None
        return missing, binding_set

    @staticmethod
    def _hard_gap_failure_has_exact_pending_slot(
        candidate: PlanCandidate,
        failure: str,
    ) -> bool:
        contract = CreativePortfolioStagingService._hard_gap_contract(candidate)
        if contract is None:
            return False
        missing, _binding_set = contract
        if failure == "required_candidate_binding_occurrence_set_mismatch":
            return True
        for occurrence_id, occurrence in missing.items():
            source_goal_id = str(occurrence["sourceGoalId"])
            day_number = int(occurrence["dayNumber"])
            if failure in {
                f"goal_occurrence_missing:{occurrence_id}:day_{day_number}",
                f"required_goal_omitted:{source_goal_id}",
            }:
                return True
            if (
                failure.startswith(f"required_goal_count_insufficient:{source_goal_id}:")
                and candidate.verifier.get("requiredGoalCoverage", {}).get(source_goal_id) is False
            ):
                return True
        return False

    @staticmethod
    def _blocking_partial_failures(candidate: PlanCandidate) -> list[str]:
        """Return verifier failures that a partial timeline must never hide."""

        failures = list(
            dict.fromkeys(
                str(item)
                for key in ("hardFailures", "strictFailures")
                for item in candidate.verifier.get(key) or []
                if str(item)
            )
        )
        recoverable_prefixes = (
            "route_anchor_target_mismatch:",
            "theme_optional_family_missing",
            "route_coverage_missing:",
            "portfolio_route_quality:",
        )
        projection = PortfolioPartialProjectionService().sanitize(
            candidate.itinerary_snapshot
        )
        sanitizable_segment_ids = (
            set(projection.removed_segment_ids)
            if projection.status == "sanitized"
            else set()
        )
        return [
            item
            for item in failures
            if not item.startswith(recoverable_prefixes)
            and not (
                item.startswith("portfolio_route_decision_proof:")
                and item.rsplit(":", 1)[-1] in sanitizable_segment_ids
            )
            and not CreativePortfolioStagingService._explicit_soft_gap_has_exact_pending_slot(
                candidate,
                item,
            )
            and not CreativePortfolioStagingService._hard_gap_failure_has_exact_pending_slot(
                candidate,
                item,
            )
        ]

    @staticmethod
    def _priority_portfolio_failures(candidate: PlanCandidate) -> list[str]:
        """Expose integrity collisions before derived density shortfalls."""

        return [
            item
            for item in CreativePortfolioStagingService._blocking_partial_failures(candidate)
            if item.startswith(
                (
                    "goal_occurrence_identity_reused:",
                    "required_goal_identity_reused:",
                )
            )
        ]

    @staticmethod
    def _partial_timeline_eligible(candidate: PlanCandidate) -> bool:
        """Allow only a truthful recoverable data-plane gap to become a draft.

        Identity, schedule, budget, lineage, and route failures remain blocking.
        Only a concrete density/theme slot gap may publish grounded POIs as a
        partial timeline.  A route provider/worker failure must keep proposal
        material read-only and retryable; it cannot authorize a version write.
        """

        failures = list(
            dict.fromkeys(
                str(item)
                for key in ("hardFailures", "strictFailures")
                for item in candidate.verifier.get(key) or []
                if str(item)
            )
        )
        if not failures or CreativePortfolioStagingService._blocking_partial_failures(candidate):
            return False
        has_recoverable_gap = bool(
            candidate.verifier.get("dayAnchorShortfalls")
            or candidate.verifier.get("themeAlignmentFailures")
            or candidate.itinerary_snapshot.get("portfolioPendingSlots")
        )
        if not has_recoverable_gap:
            return False
        segments = [
            segment
            for day in candidate.itinerary_snapshot.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
        ]
        has_route_anchor = False
        has_bound_required_anchor = False
        binding_rows = [
            item
            for item in candidate.itinerary_snapshot.get("portfolioRequiredCandidateBindings") or []
            if isinstance(item, dict)
        ]
        for segment in segments:
            semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            if not bool(semantic.get("routeAnchor")):
                continue
            has_route_anchor = True
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            if not PlanProposalVerifier._has_grounded_identity(
                poi,
                city=str(candidate.itinerary_snapshot.get("city") or ""),
            ) or str((segment.get("semanticMetadata") or {}).get("groundingStatus") or "") not in {
                "selected",
                "confirmed",
                "grounded",
                "agent_selected_candidate",
            }:
                return False
            goal_id = str(semantic.get("sourceGoalId") or semantic.get("goalId") or "")
            planning_slot_id = str(semantic.get("planningSlotId") or "")
            brief_id = str(semantic.get("creativeBriefId") or "")
            amap_id = PoiPhysicalIdentityService.normalized_amap_id(poi)
            if bool(semantic.get("required")) and goal_id and planning_slot_id and brief_id:
                has_bound_required_anchor = any(
                    str(binding.get("sourceGoalId") or binding.get("goalId") or "") == goal_id
                    and str(binding.get("planningSlotId") or "") == planning_slot_id
                    and PoiPhysicalIdentityService.normalized_amap_id(binding) == amap_id
                    and str(binding.get("briefId") or "") == brief_id
                    for binding in binding_rows
                )
                if has_bound_required_anchor:
                    continue
        return bool(
            has_route_anchor
            and has_bound_required_anchor
            and int(candidate.verifier.get("requiredCandidateBindingActualCount") or 0) > 0
            and int(candidate.verifier.get("hardCandidateLineageMissingCount") or 0) == 0
        )

    @staticmethod
    def _partial_timeline_reason(candidate: PlanCandidate) -> str:
        verifier = candidate.verifier
        if verifier.get("routeQualityFailures") or verifier.get("routeCoverageFailures"):
            return "route_quality_unresolved"
        if verifier.get("dayAnchorShortfalls"):
            return "density_slots_unresolved"
        if verifier.get("themeAlignmentFailures"):
            return "theme_optional_slots_unresolved"
        return "recoverable_data_plane_gap"

    @staticmethod
    def _partial_timeline_sort_key(candidate: PlanCandidate) -> tuple[int, float, float]:
        actuals = candidate.verifier.get("dayAnchorActuals") or {}
        targets = candidate.verifier.get("dayAnchorTargets") or {}
        actual = sum(int(value or 0) for value in actuals.values())
        target = max(1, sum(int(value or 0) for value in targets.values()))
        return (
            actual,
            actual / target,
            float(candidate.score.route_efficiency),
        )

    @staticmethod
    def _required_assignment_shortfalls(
        required: dict[str, list[dict[str, Any]]],
        occurrence_contract: GoalOccurrencePlan | None,
    ) -> list[str]:
        """Explain an empty hard-assignment search as missing slot supply.

        The optimizer correctly requires distinct physical POIs for repeated
        occurrences, but an empty Cartesian product previously skipped the
        verifier and collapsed into a generic infeasible failure. A small
        bipartite match preserves the hard contract while exposing the
        unmatched day/occurrence to the continuation UI.
        """

        if not required:
            return []
        occurrence_by_id = {
            occurrence.occurrence_id: occurrence
            for occurrence in (occurrence_contract.occurrences if occurrence_contract else [])
        }
        candidate_owner: dict[str, str] = {}

        def assign(occurrence_id: str, visited: set[str]) -> bool:
            for candidate in required.get(occurrence_id, []):
                identity = PoiPhysicalIdentityService.canonical_amap_id(candidate)
                if not identity or identity in visited:
                    continue
                visited.add(identity)
                owner = candidate_owner.get(identity)
                if owner is None or assign(owner, visited):
                    candidate_owner[identity] = occurrence_id
                    return True
            return False

        unmatched: list[str] = []
        for occurrence_id in sorted(
            required,
            key=lambda key: (len(required[key]), key),
        ):
            if not assign(occurrence_id, set()):
                unmatched.append(occurrence_id)
        shortfalls: list[str] = []
        for occurrence_id in unmatched:
            occurrence = occurrence_by_id.get(occurrence_id)
            if occurrence is not None:
                shortfalls.append(f"day_{occurrence.day_number}:0/1")
            else:
                shortfalls.append(f"{occurrence_id}:0/1")
        return shortfalls

    @staticmethod
    def _occurrence_contract_failure(
        ledger: Any,
        occurrence_contract: GoalOccurrencePlan,
    ) -> str | None:
        hard = {goal.goal_id: goal for goal in ledger.hard_goals}
        soft = {goal.goal_id: goal for goal in ledger.soft_goals}
        hard_counts: dict[str, int] = {}
        for occurrence in occurrence_contract.occurrences:
            # The accepted occurrence compiler intentionally represents a
            # preferred extra occurrence of a hard goal as explicit-soft.  It
            # remains a known ledger goal, but must not raise the hard minimum
            # or become a required route anchor.  Validate identity against the
            # union while counting only genuinely hard occurrences.
            goal = (
                hard.get(occurrence.source_goal_id)
                if occurrence.requirement_level == "hard"
                else soft.get(occurrence.source_goal_id) or hard.get(occurrence.source_goal_id)
            )
            if goal is None or goal.intent_type != occurrence.intent_type:
                return f"portfolio_goal_occurrence_contract_mismatch:{occurrence.occurrence_id}"
            if occurrence.requirement_level == "hard":
                hard_counts[occurrence.source_goal_id] = hard_counts.get(occurrence.source_goal_id, 0) + 1
        mismatches = [
            f"{goal_id}:{hard_counts.get(goal_id, 0)}/{goal.required_min}"
            for goal_id, goal in sorted(hard.items())
            if hard_counts.get(goal_id, 0) < goal.required_min
        ]
        if mismatches:
            return "portfolio_goal_occurrence_cardinality_mismatch:" + ";".join(mismatches)
        return None

    def _bind_required_to_brief(
        self,
        required: dict[str, dict[str, Any]],
        skeleton: Any,
        *,
        occurrence_contract: GoalOccurrencePlan | None,
        admission_metrics: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Clone shared hard candidates and bind every local field from one brief.

        No pending-candidate or optimizer payload is trusted for proposal-local
        lineage. Occurrence mode uses the controller-owned plan; legacy mode is
        accepted only when the brief contains one unambiguous hard slot and pool.
        """
        brief_id = str(skeleton.brief.brief_id)
        slots = [slot for slot in skeleton.day_slots if slot.required_goal_id]
        required_pools = [pool for pool in skeleton.intent_pools if str(pool.requirement_level) == "required"]
        occurrence_by_id = {
            occurrence.occurrence_id: occurrence
            for occurrence in (occurrence_contract.occurrences if occurrence_contract else [])
            if occurrence.requirement_level == "hard"
        }
        result: dict[str, dict[str, Any]] = {}
        for key, raw_candidate in required.items():
            if not isinstance(raw_candidate, dict):
                raise ValueError("portfolio_required_candidate_lineage_invalid")
            candidate = dict(raw_candidate)
            if not str(candidate.get("amapId") or candidate.get("id") or ""):
                raise ValueError("portfolio_required_candidate_identity_invalid")
            occurrence = None
            if occurrence_contract is not None:
                occurrence = occurrence_by_id.get(str(key))
                if occurrence is None:
                    raise ValueError("portfolio_required_occurrence_not_found")
                goal_id = occurrence.source_goal_id
                intent_type = occurrence.intent_type
                day_number = occurrence.day_number
                occurrence_id = occurrence.occurrence_id
                requirement_level = occurrence.requirement_level
                distinct_group_id = occurrence.distinct_group_id
                matching_slots = [
                    slot
                    for slot in slots
                    if str(slot.required_goal_id) == goal_id and int(slot.day_number) == day_number
                ]
            else:
                goal_id = str(key)
                if not goal_id:
                    raise ValueError("portfolio_required_legacy_goal_missing")
                intent_type = str(candidate.get("intentType") or "")
                matching_slots = [slot for slot in slots if str(slot.required_goal_id) == goal_id]
                if len(matching_slots) != 1:
                    raise ValueError("portfolio_required_legacy_binding_ambiguous")
                day_number = int(matching_slots[0].day_number)
                occurrence_id = f"legacy:{goal_id}"
                requirement_level = "required"
                distinct_group_id = None
            if len(matching_slots) != 1:
                raise ValueError("portfolio_required_occurrence_slot_missing")
            slot = matching_slots[0]
            matching_pools = [
                pool
                for pool in required_pools
                if str(pool.brief_id) == brief_id
                and str(pool.goal_id or "") == goal_id
                and (not intent_type or str(pool.intent_type) == intent_type)
                and str(slot.slot_id) in {str(item) for item in pool.assign_to_slots}
            ]
            if not matching_pools:
                cross_brief = [
                    pool
                    for pool in required_pools
                    if str(pool.goal_id or "") == goal_id
                    and (not intent_type or str(pool.intent_type) == intent_type)
                    and str(slot.slot_id) in {str(item) for item in pool.assign_to_slots}
                ]
                if cross_brief:
                    raise ValueError("portfolio_required_cross_brief_pool")
                raise ValueError("portfolio_required_occurrence_pool_missing")
            if len(matching_pools) != 1:
                raise ValueError("portfolio_required_occurrence_pool_ambiguous")
            pool = matching_pools[0]
            intent_type = str(pool.intent_type)
            semantic_candidate = {
                **candidate,
                "type": str(candidate.get("type") or candidate.get("providerType") or ""),
            }
            semantic_decision = self.semantic_policy.evaluate(
                intent_type,
                semantic_candidate,
                raw_need=pool.raw_need,
                exact_entity=pool.exact_entity if pool.entity_binding_mode == "exact_entity" else None,
                optional_experience_family=None,
            )
            if not semantic_decision.passed:
                raise ValueError(f"portfolio_required_candidate_semantic_mismatch:{semantic_decision.reason_code}")
            candidate["semanticPassed"] = True
            candidate["semanticDecision"] = semantic_decision.to_camel_dict()
            candidate = self._with_experience_spec_policy(
                candidate,
                occurrence,
                base_route_contract=dict(getattr(slot, "route_contract", None) or pool.route_preference or {}),
            )
            metrics = (
                admission_metrics
                if admission_metrics is not None
                else {
                    "consumerAdmissionEvaluatedCount": 0,
                    "consumerAdmissionAdmittedCount": 0,
                    "consumerAdmissionPendingCount": 0,
                    "consumerAdmissionRejectedCount": 0,
                    "consumerAdmissionAreaSeedOnlyCount": 0,
                    "consumerFingerprintMismatchCount": 0,
                    "staleAdmissionInvalidatedCount": 0,
                    "consumerReevaluationCount": 0,
                    "nameOnlyPositiveSignalCount": 0,
                    "identityRejectCount": 0,
                    "providerTypeRejectCount": 0,
                    "shapeRejectCount": 0,
                    "semanticAffordanceRejectCount": 0,
                    "anchorEligibilityRejectCount": 0,
                    "specializedPolicyRejectCount": 0,
                    "evidenceSufficiencyPendingCount": 0,
                }
            )
            if not self._apply_consumer_admission(
                candidate,
                brief_id=brief_id,
                pool=pool,
                slot=slot,
                family=intent_type,
                metrics=metrics,
            ):
                report = candidate.get("consumerAdmissionReport") or {}
                reason = str(((report.get("reasonCodes") or ["consumer_admission_rejected"])[0]))
                raise ValueError(f"portfolio_required_candidate_consumer_admission_rejected:{reason}")
            day_role = next(
                (role.role for role in skeleton.brief.day_roles if int(role.day_number) == day_number),
                "",
            )
            result[str(key)] = {
                **candidate,
                "briefId": brief_id,
                "poolId": pool.pool_id,
                "assignToSlots": list(pool.assign_to_slots),
                "slotId": slot.slot_id,
                "planningSlotId": slot.slot_id,
                "dayNumber": day_number,
                "timeWindow": slot.time_window,
                "startTime": slot.start_time,
                "durationMinutes": slot.duration_minutes,
                "kind": slot.kind,
                "rawNeed": pool.raw_need,
                "intentType": intent_type,
                "goalId": goal_id,
                "sourceGoalId": goal_id,
                "occurrenceId": occurrence_id,
                "requirementLevel": requirement_level,
                "distinctGroupId": distinct_group_id,
                "dayRole": day_role,
                "routePreference": dict(pool.route_preference or {}),
                "entityBindingMode": pool.entity_binding_mode,
                "exactEntity": pool.exact_entity,
                "candidateSource": str(candidate.get("source") or candidate.get("candidateSource") or ""),
            }
        return result

    @staticmethod
    def _required_candidates_for_occurrence(
        *,
        universe: SharedCandidateUniverse,
        skeleton: Any,
        occurrence: Any,
        fallback_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Put the occurrence's own pool supply ahead of shared intent evidence.

        Network evidence may be shared, but the optimizer beam must first see
        the candidates discovered for this exact brief/day/slot.  Otherwise a
        candidate-rich Day 1 can fill the global beam and hide a valid Day 2
        candidate even though discovery already grounded it.
        """

        if occurrence is None:
            return [dict(item) for item in fallback_candidates if isinstance(item, dict)]
        matching_slots = [
            slot
            for slot in skeleton.day_slots
            if str(slot.required_goal_id or "") == str(occurrence.source_goal_id)
            and int(slot.day_number) == int(occurrence.day_number)
        ]
        if len(matching_slots) != 1:
            return [dict(item) for item in fallback_candidates if isinstance(item, dict)]
        slot_id = str(matching_slots[0].slot_id)
        matching_pools = [
            pool
            for pool in skeleton.intent_pools
            if str(pool.requirement_level) == "required"
            and str(pool.brief_id) == str(skeleton.brief.brief_id)
            and str(pool.goal_id or "") == str(occurrence.source_goal_id)
            and str(pool.intent_type) == str(occurrence.intent_type)
            and slot_id in {str(item) for item in pool.assign_to_slots}
        ]
        if len(matching_pools) != 1:
            return [dict(item) for item in fallback_candidates if isinstance(item, dict)]
        candidates = universe.candidates_for_pool(
            str(matching_pools[0].pool_id),
            str(occurrence.intent_type),
            str(skeleton.brief.brief_id),
        )
        if not candidates:
            return [dict(item) for item in fallback_candidates if isinstance(item, dict)]
        return [
            {
                **dict(item),
                "goalId": occurrence.source_goal_id,
                "sourceGoalId": occurrence.source_goal_id,
                "occurrenceId": occurrence.occurrence_id,
                "dayNumber": occurrence.day_number,
                "intentType": occurrence.intent_type,
                "requirementLevel": occurrence.requirement_level,
                "distinctGroupId": occurrence.distinct_group_id,
            }
            for item in candidates
            if isinstance(item, dict)
        ]

    @staticmethod
    def _binding_evidence(required: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "briefId": item.get("briefId"),
                "occurrenceId": item.get("occurrenceId"),
                "sourceGoalId": item.get("sourceGoalId"),
                "intentType": item.get("intentType"),
                "dayNumber": item.get("dayNumber"),
                "poolId": item.get("poolId"),
                "planningSlotId": item.get("planningSlotId"),
                "dayRole": item.get("dayRole"),
                "amapId": PoiPhysicalIdentityService.normalized_amap_id(item),
                "candidateSource": item.get("candidateSource"),
            }
            for _, item in sorted(required.items())
        ]

    def _ensure_agent_generated_titles(
        self,
        candidates: list[PlanCandidate],
        *,
        city: str,
        reserved_titles: set[str] | None = None,
    ) -> list[PlanCandidate]:
        """Generate titles only for already materialized and verified candidates."""

        used_titles = {str(item).strip() for item in reserved_titles or set() if str(item).strip()}
        result: list[PlanCandidate] = []
        failures: list[str] = []
        attempted = 0
        accepted = 0
        preserved_incomplete = 0
        signal_inventory = [
            CreativeProposalTitleService.required_title_signals(candidate.itinerary_snapshot)
            for candidate in candidates
        ]
        signal_counts: dict[str, int] = {}
        for signals in signal_inventory:
            for signal in set(signals):
                signal_counts[signal] = signal_counts.get(signal, 0) + 1
        for candidate_index, candidate in enumerate(candidates):
            snapshot = copy.deepcopy(candidate.itinerary_snapshot)
            candidate_signals = signal_inventory[candidate_index]
            unique_signals = [signal for signal in candidate_signals if signal_counts.get(signal) == 1]
            snapshot["proposalSpecificTitleSignals"] = unique_signals or candidate_signals
            if candidate.verifier.get("passed") is not True:
                preserved_incomplete += 1
                structured_snapshot, structured_title = self._with_incomplete_status_title(
                    snapshot,
                    candidate.verifier,
                )
                result.append(
                    candidate.model_copy(
                        update={
                            "brief": candidate.brief.model_copy(update={"title": structured_title}),
                            "itinerary_snapshot": structured_snapshot,
                        }
                    )
                )
                continue
            if not callable(self.title_candidate_generator):
                titled_snapshot = CreativeProposalTitleService.with_server_fallback_title(
                    snapshot,
                    reason_code="title_provider_unavailable",
                    reserved_titles=used_titles,
                )
                failures.append(candidate.proposal_id)
                fallback_title = str(titled_snapshot.get("title") or "城市方案待核验")
                used_titles.add(fallback_title)
                result.append(
                    candidate.model_copy(
                        update={
                            "brief": candidate.brief.model_copy(update={"title": fallback_title}),
                            "itinerary_snapshot": titled_snapshot,
                        }
                    )
                )
                continue
            context = CreativeProposalTitleService.agent_generation_context(
                snapshot,
                city=city or str(snapshot.get("city") or ""),
                primary_axis=candidate.brief.primary_axis,
                secondary_axes=candidate.brief.secondary_axes,
                optional_experiences=(item.family for item in candidate.brief.optional_experiences),
                reserved_titles=used_titles,
            )
            attempted += 1
            titled_snapshot = CreativeProposalTitleService.generate_and_apply_agent_title(
                snapshot,
                generator=self.title_candidate_generator,
                context=context,
                reserved_titles=used_titles,
            )
            title_generation = (
                titled_snapshot.get("portfolioTitleGeneration")
                if isinstance(titled_snapshot.get("portfolioTitleGeneration"), dict)
                else {}
            )
            if title_generation.get("status") != "succeeded":
                failures.append(candidate.proposal_id)
            else:
                accepted += 1
            title = str(titled_snapshot.get("title") or "城市方案待核验")
            used_titles.add(title)
            result.append(
                candidate.model_copy(
                    update={
                        "brief": candidate.brief.model_copy(update={"title": title}),
                        "itinerary_snapshot": titled_snapshot,
                    }
                )
            )
        self.last_staging_metrics["agentTitleGeneration"] = {
            "attempted": attempted,
            "accepted": accepted,
            "preservedIncomplete": preserved_incomplete,
            "failedProposalIds": failures,
        }
        return result

    @staticmethod
    def _with_incomplete_status_title(
        snapshot: dict[str, Any],
        verifier: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Remove marketing copy from an incomplete comparison card."""

        material = copy.deepcopy(snapshot)
        title = CreativeProposalTitleService.incomplete_status_title(material, verifier)
        material.pop("portfolioTitleEvidence", None)
        material["title"] = title
        if isinstance(material.get("creativeBrief"), dict):
            material["creativeBrief"] = {
                **copy.deepcopy(material["creativeBrief"]),
                "title": title,
            }
        if isinstance(material.get("portfolioOutputQuality"), dict):
            quality = copy.deepcopy(material["portfolioOutputQuality"])
            quality.pop("originalCreativeTitle", None)
            quality["displayTitle"] = title
            material["portfolioOutputQuality"] = quality
        return material, title

    @staticmethod
    def canonical_snapshot_material(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return the title-independent material used for proposal identity.

        Agent-generated titles are applied only after route materialization and
        verification.  Removing every title projection field keeps the stored
        canonical signature stable across that later presentation-only step and
        lets offline evaluators recompute the signature from persisted material.
        """

        return proposal_structural_signature_material(snapshot)

    @classmethod
    def canonical_signature_for_snapshot(cls, snapshot: dict[str, Any]) -> str:
        return proposal_canonical_signature(snapshot)

    @classmethod
    def _structural_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Compatibility alias for existing structural-distance callers."""

        return cls.canonical_snapshot_material(snapshot)

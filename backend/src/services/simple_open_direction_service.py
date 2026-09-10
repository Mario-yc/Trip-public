"""Sequential Simple Open direction persistence without itinerary writes.

One generated direction is stored as immutable proposal material.  It becomes
the active editable itinerary only after an opaque server-issued confirmation
is executed through the existing proposal commit/single-writer path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from src.models.poi import POI
from src.services.creative_planning_models import PlanPortfolio, proposal_structural_signature_material
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.daily_route_overlap_service import DailyRouteOverlapService
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.meal_experience_portfolio import MealExperiencePortfolioPolicy
from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
from src.services.simple_direction_execution_evidence_service import SimpleDirectionExecutionEvidenceService
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor
from src.services.guide_poi_identity_service import GuidePoiIdentityService
from src.services.simple_open_dynamic_schedule_service import SimpleOpenDynamicScheduleService
from src.services.travel_guide_advice_service import TravelGuideAdviceService
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
from src.services.guide_source_refresh_service import GuideSourceRefreshService


@dataclass(frozen=True)
class _PhysicalDirectionEvaluation:
    """Typed novelty outcome; a failure reason is never a proposal identity."""

    status: Literal["distinct", "collision", "incomplete"]
    matching_proposal_id: Optional[str] = None
    reason_code: Optional[str] = None


class SimpleOpenDirectionService:
    WORKFLOW_MODE = "simple_direction_v1"
    _FRONTIER_PROVIDER_INTEGRITY_ERRORS = frozenset(
        {
            "simple_direction_frontier_provider_outcome_invalid",
            "simple_direction_frontier_success_identity_missing",
            "simple_direction_frontier_reused_identity_mismatch",
            "simple_direction_frontier_selected_identity_not_in_snapshot",
        }
    )

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.store = PlanPortfolioStore(db)

    @classmethod
    def prepare_direction_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        request_contract: dict[str, Any],
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Freeze unresolved slots as pending metadata before presentation work."""

        if (request_contract.get("requestActivityCoverage") or {}).get("status") == "pending":
            raise ValueError("request_activity_coverage_not_complete")

        return cls._materialize_unresolved_slots_as_pending_metadata(
            snapshot,
            request_contract=request_contract,
            remaining_query_scopes=remaining_query_scopes,
        )

    @staticmethod
    def route_contract_requires_clarification(request_contract: Any) -> bool:
        if not isinstance(request_contract, dict):
            return True
        route = request_contract.get("routeDecisionContract")
        if not isinstance(route, dict):
            return True
        missing = [str(item) for item in route.get("missingFields") or [] if str(item)]
        normalized = RouteInsertionScorer.normalized_route_decision_contract(route)
        return bool(str(route.get("status") or "") != "ready" or missing or normalized is None)

    def ensure_root(
        self,
        *,
        session_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        expected_base_version_id: Optional[str],
        source_observation_fingerprint: str,
        request_contract_fingerprint: str,
        request_contract: dict[str, Any],
        locality: str,
        max_pages_per_query: int,
        allow_exploration_ordering: bool = True,
    ) -> dict[str, Any]:
        """Create/freeze the Simple root and qualification frontier pre-search."""

        if (request_contract.get("requestActivityCoverage") or {}).get("status") == "pending":
            raise ValueError("request_activity_coverage_not_complete")

        root = self._root(session_id=session_id, planning_root_id=planning_root_id)
        qualification = request_contract.get("entityQualificationConstraint")
        frontier = None
        if isinstance(qualification, dict):
            evidence = EntityQualificationEvidenceService.qualified_entities(
                locality=locality,
                scheme=str(qualification.get("qualificationScheme") or "").strip(),
                value=str(qualification.get("qualificationValue") or "").strip(),
            )
            if not isinstance(evidence, dict):
                raise ValueError("simple_direction_qualification_evidence_missing")
            # The qualification list supplies eligibility, not a preference
            # for its first rows. Existing roots retain their frozen frontier.
            frontier = SimpleDirectionFrontierService.create(
                planning_root_id=planning_root_id,
                request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                evidence=evidence,
                locality=locality,
                max_pages_per_query=max_pages_per_query,
                exploration_seed=(
                    self._new_exploration_seed()
                    if (
                        root is None
                        and allow_exploration_ordering
                        and self._allows_exploration_ordering(request_contract)
                    )
                    else None
                ),
            )
        if root is None:
            portfolio_id = f"simple_direction_portfolio_{uuid4().hex[:16]}"
            portfolio = PlanPortfolio(
                portfolioId=portfolio_id,
                sessionId=session_id,
                sourceUserTurnId=planning_root_id,
                sourceAssistantTurnId=source_assistant_turn_id,
                expectedBaseVersionId=expected_base_version_id,
                sourceObservationFingerprint=self._minimum_fingerprint(source_observation_fingerprint),
                requestContractFingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                status="awaiting_selection",
            )
            try:
                self.store.create(portfolio, [], simple_direction_frontier=frontier)
            except (ValueError, sqlite3.IntegrityError) as error:
                if not (
                    isinstance(error, ValueError) and str(error) == "portfolio_root_identity_conflict"
                    or isinstance(error, sqlite3.IntegrityError)
                    and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
                ):
                    raise
                self.db.rollback()
                winner = self._root(session_id=session_id, planning_root_id=planning_root_id)
                if (
                    winner is None
                    or winner.get("sessionId") != session_id
                    or winner.get("planningRootId") != planning_root_id
                    or winner.get("requestContractFingerprint") != portfolio.request_contract_fingerprint
                    or winner.get("expectedBaseVersionId") != expected_base_version_id
                ):
                    raise ValueError("simple_direction_frontier_root_identity_mismatch") from error
                portfolio_id = str(winner["id"])
            root = self._root_by_id(portfolio_id)
        if root is None:
            raise ValueError("simple_direction_portfolio_create_failed")
        portfolio_id = str(root["id"])
        self._mark_workflow(portfolio_id)
        self._freeze_request_contract(
            portfolio_id=portfolio_id,
            request_contract=request_contract,
            request_contract_fingerprint=request_contract_fingerprint,
        )
        if frontier is not None:
            self.store.initialize_simple_direction_frontier(
                portfolio_id=portfolio_id,
                frontier=frontier,
                expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
            )
        else:
            self.store.initialize_simple_direction_compatibility_frontier(
                portfolio_id=portfolio_id,
                expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                max_pages_per_query=max_pages_per_query,
            )
        frozen = self._root_by_id(portfolio_id)
        if frozen is None:
            raise ValueError("simple_direction_portfolio_not_found")
        return frozen

    @staticmethod
    def _new_exploration_seed() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _allows_exploration_ordering(request_contract: dict[str, Any]) -> bool:
        # This tie-break has no spatial or exact-entity admission machinery.
        # Keep constrained requests on their established selection path.
        if any(request_contract.get(key) for key in ("spatialPreference", "lockedEntities", "negativeConstraints")):
            return False
        return not any(
            isinstance(item, dict)
            and str(item.get("intentType") or "") == "campus_visit"
            and (
                item.get("exactEntity")
                or item.get("explicitlyNamed") is True
                or str(item.get("entityBindingMode") or "") == "exact_entity"
            )
            for item in request_contract.get("requiredIntents") or []
        )

    def claim_frontier_assignment(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        campus_slots: list[dict[str, Any]],
        request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        return self.store.claim_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id=execution_id,
            campus_slots=campus_slots,
            expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
        )

    def claim_compatibility_continuation(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        source_assistant_turn_id: str,
        request_turn_id: str,
        choice_id: str,
        request_contract_fingerprint: str,
        max_pages_per_query: int = 3,
        slot_query_scopes: Optional[list[dict[str, Any]]] = None,
        defer_slot_scope_claim: bool = False,
    ) -> dict[str, Any]:
        """Claim an exact no-qualification-frontier continuation pre-Provider."""

        return self.store.claim_simple_direction_compatibility_attempt(
            portfolio_id=portfolio_id,
            execution_id=execution_id,
            source_assistant_turn_id=source_assistant_turn_id,
            request_turn_id=request_turn_id,
            choice_id=choice_id,
            expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
            max_pages_per_query=max_pages_per_query,
            slot_query_scopes=slot_query_scopes,
            defer_slot_scope_claim=defer_slot_scope_claim,
        )

    def claim_compatibility_current_scopes(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        request_contract_fingerprint: str,
        slot_query_scopes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Attach current-anchor exact scopes to the claimed execution."""

        return self.store.attach_simple_direction_compatibility_slot_scopes(
            portfolio_id=portfolio_id,
            execution_id=execution_id,
            expected_request_contract_fingerprint=self._minimum_fingerprint(
                request_contract_fingerprint
            ),
            slot_query_scopes=slot_query_scopes,
        )

    def root_for_planning_root(self, *, session_id: str, planning_root_id: str) -> Optional[dict[str, Any]]:
        """Read one exact Simple Direction root without falling back to latest."""

        return self._root(session_id=session_id, planning_root_id=planning_root_id)

    def settle_unresolved_frontier_attempt(
        self,
        *,
        session_id: str,
        planning_root_id: str,
        source_assistant_turn_id: str,
        expected_base_version_id: Optional[str],
        request_contract_fingerprint: str,
        frontier_execution_id: str,
        frontier_outcomes: list[dict[str, Any]],
        slot_frontier_outcomes: Optional[list[dict[str, Any]]] = None,
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Settle a Provider batch that could not form any proposal snapshot.

        A successful AMap query whose candidates all fail admission is evidence
        that the exact entity page was checked.  It is not a transport retry.
        Persisting that fact lets the next opaque continuation advance to the
        next server-owned page while keeping proposal/timeline writes at zero.
        """

        root = self._root(session_id=session_id, planning_root_id=planning_root_id)
        if (
            root is None
            or root.get("workflowMode") != self.WORKFLOW_MODE
            or str(root.get("sessionId") or "") != session_id
            or str(root.get("requestContractFingerprint") or "")
            != self._minimum_fingerprint(request_contract_fingerprint)
        ):
            raise ValueError("simple_direction_frontier_root_identity_mismatch")
        rebound = self._frontier_attempt_for_novelty(
            root=root,
            execution_id=frontier_execution_id,
            outcomes=frontier_outcomes,
            request_contract_fingerprint=request_contract_fingerprint,
        )
        if not isinstance(rebound, dict):
            raise ValueError("simple_direction_frontier_attempt_missing")
        validated_outcomes = [
            copy.deepcopy(item) for item in rebound.get("validatedOutcomes") or [] if isinstance(item, dict)
        ]
        if not validated_outcomes:
            raise ValueError("simple_direction_frontier_outcomes_incomplete")
        normalized_slot_outcomes = [
            copy.deepcopy(item) for item in slot_frontier_outcomes or [] if isinstance(item, dict)
        ]
        provider_failure = next(
            (
                item
                for item in [*validated_outcomes, *normalized_slot_outcomes]
                if str(item.get("providerOutcome") or "") == "failure"
            ),
            None,
        )
        rejected_count = sum(str(item.get("providerOutcome") or "") == "rejected" for item in validated_outcomes)
        rejection_reason_counts: dict[str, int] = {}
        for outcome in validated_outcomes:
            if str(outcome.get("providerOutcome") or "") != "rejected":
                continue
            for raw_reason in outcome.get("rejectionReasonCodes") or []:
                reason = str(raw_reason or "").strip()
                if reason:
                    rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + 1
        if provider_failure is not None:
            settlement_reason = str(provider_failure.get("reasonCode") or "provider_unavailable")
            settlement = self._reconcile_frontier_offer(
                portfolio_id=str(root["id"]),
                execution_id=frontier_execution_id,
                outcomes=validated_outcomes,
                slot_query_outcomes=normalized_slot_outcomes,
                proposal_id=None,
                disposition="provider_pending",
                request_contract_fingerprint=request_contract_fingerprint,
                blocking_layer="provider",
                reason_code=settlement_reason,
                remaining_query_scopes=remaining_query_scopes,
            )
        else:
            settlement_reason = (
                "simple_direction_campus_candidate_page_rejected"
                if rejected_count == len(validated_outcomes)
                else "simple_direction_candidate_batch_incomplete"
            )
            settlement = self._reconcile_frontier_offer(
                portfolio_id=str(root["id"]),
                execution_id=frontier_execution_id,
                outcomes=validated_outcomes,
                slot_query_outcomes=normalized_slot_outcomes,
                proposal_id=None,
                disposition="assigned_partial",
                request_contract_fingerprint=request_contract_fingerprint,
                blocking_layer="poi",
                reason_code=settlement_reason,
                remaining_query_scopes=remaining_query_scopes,
            )
        frontier = settlement.get("frontier") if isinstance(settlement.get("frontier"), dict) else {}
        frontier_status = str(frontier.get("frontierStatus") or "")
        frontier_has_more = frontier_status == "has_more"
        response = self.response_material(
            portfolio_id=str(root["id"]),
            source_assistant_turn_id=source_assistant_turn_id,
            expected_base_version_id=expected_base_version_id,
            update_mode="append",
            proposal_ids=[],
            proposal_delta=0,
            reissue_all_capabilities=provider_failure is None,
            allow_empty_frontier_continuation=provider_failure is None and frontier_has_more,
        )
        response.update(
            {
                "status": "provider_pending" if provider_failure is not None else "frontier_advanced",
                "reasonCode": (
                    "simple_direction_frontier_provider_pending"
                    if provider_failure is not None
                    else f"{settlement_reason}_frontier_remaining"
                    if frontier_status == "has_more"
                    else f"{settlement_reason}_frontier_exhausted"
                ),
                "frontierAttemptConsumed": provider_failure is None,
                "frontierExecutionId": str(frontier_execution_id),
                "checkedCampusCount": len(validated_outcomes),
                "rejectedCampusCount": rejected_count,
                "campusRejectionReasonCounts": rejection_reason_counts,
            }
        )
        return response

    def offer_direction(
        self,
        *,
        session_id: str,
        planning_root_id: str,
        source_user_turn_id: str,
        source_assistant_turn_id: str,
        expected_base_version_id: Optional[str],
        source_observation_fingerprint: str,
        request_contract_fingerprint: str,
        snapshot: dict[str, Any],
        request_contract: dict[str, Any],
        title_generator: Any = None,
        frontier_execution_id: str = "",
        frontier_outcomes: Optional[list[dict[str, Any]]] = None,
        slot_frontier_outcomes: Optional[list[dict[str, Any]]] = None,
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if self.route_contract_requires_clarification(request_contract):
            raise ValueError("simple_direction_route_decision_clarification_required")
        route_contract = copy.deepcopy(request_contract["routeDecisionContract"])
        material = self.prepare_direction_snapshot(
            snapshot,
            request_contract=request_contract,
            remaining_query_scopes=remaining_query_scopes,
        )
        material["routeDecisionContract"] = route_contract
        material["spatialPreference"] = copy.deepcopy(request_contract.get("spatialPreference") or {})
        material["workflowMode"] = self.WORKFLOW_MODE
        material["comparisonRole"] = "candidate_proposal"
        material["originProjectionMode"] = "full_proposal"
        root = self._root(session_id=session_id, planning_root_id=planning_root_id)
        if root is None:
            portfolio_id = f"simple_direction_portfolio_{uuid4().hex[:16]}"
            portfolio = PlanPortfolio(
                portfolioId=portfolio_id,
                sessionId=session_id,
                sourceUserTurnId=planning_root_id,
                sourceAssistantTurnId=source_assistant_turn_id,
                expectedBaseVersionId=expected_base_version_id,
                sourceObservationFingerprint=self._minimum_fingerprint(source_observation_fingerprint),
                requestContractFingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                status="awaiting_selection",
            )
            self.store.create(portfolio, [])
            self._mark_workflow(portfolio_id)
            root = self._root(session_id=session_id, planning_root_id=planning_root_id)
        if root is None:
            raise ValueError("simple_direction_portfolio_create_failed")
        portfolio_id = str(root["id"])
        self._freeze_request_contract(
            portfolio_id=portfolio_id,
            request_contract=request_contract,
            request_contract_fingerprint=request_contract_fingerprint,
        )
        root = self._root_by_id(portfolio_id)
        if root is None:
            raise ValueError("simple_direction_portfolio_not_found")
        if not str(frontier_execution_id or "") and not root.get("simpleDirectionFrontier") and remaining_query_scopes:
            self.store.record_simple_direction_compatibility_query_scopes(
                portfolio_id=portfolio_id,
                expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                remaining_query_scopes=remaining_query_scopes,
            )
            root = self._root_by_id(portfolio_id)
            if root is None:
                raise ValueError("simple_direction_portfolio_not_found")
        status = str(root.get("status") or "")
        if status == "committing":
            raise ValueError("simple_direction_offer_in_progress")
        if status not in {"awaiting_selection", "committed", "failed"}:
            raise ValueError("simple_direction_portfolio_not_available")
        # Guide adoption promises a new place, even when global direction
        # novelty legitimately permits a required park/campus to be reused.
        # Read prior identities from this verified root before proposal writes;
        # never accept exclusion lists supplied in the candidate snapshot.
        prior_guide_aliases = (
            self.prior_direction_physical_aliases(
                session_id=session_id,
                portfolio_id=portfolio_id,
                include_required_entities=True,
            )
            if isinstance(material.get("guideContinuationRequirement"), dict)
            else frozenset()
        )
        verifier = self._proposal_verifier(
            material,
            prior_guide_physical_aliases=prior_guide_aliases,
            sanitize_reused_guide_annotations=True,
        )
        material["portfolioVerifier"] = copy.deepcopy(verifier)
        if isinstance(verifier.get("guideEvidenceUsage"), dict):
            material["guideEvidenceUsage"] = copy.deepcopy(verifier["guideEvidenceUsage"])
        try:
            compatibility_attempt = self._compatibility_attempt_for_execution(
                root=root,
                execution_id=frontier_execution_id,
                request_contract_fingerprint=request_contract_fingerprint,
            )
            frontier_attempt = self._frontier_attempt_for_novelty(
                root=root,
                execution_id=frontier_execution_id,
                outcomes=frontier_outcomes or [],
                request_contract_fingerprint=request_contract_fingerprint,
            )
            if isinstance(frontier_attempt, dict):
                validated_outcomes = [
                    copy.deepcopy(item)
                    for item in frontier_attempt.get("validatedOutcomes") or []
                    if isinstance(item, dict)
                ]
                successful_slots = {
                    str(item.get("slotId") or "")
                    for item in validated_outcomes
                    if str(item.get("providerOutcome") or "") == "success"
                    and str(item.get("slotId") or "")
                }
                if successful_slots:
                    assignments = [
                        copy.deepcopy(item)
                        for item in frontier_attempt.get("campusAssignments") or []
                        if isinstance(item, dict)
                    ]
                    _, assignment_evidence = self._campus_assignments_grounded(
                        material,
                        assignments=assignments,
                        outcomes=validated_outcomes,
                    )
                    grounded_success_slots = {
                        str(item.get("slotId") or "")
                        for item in assignment_evidence
                        if item.get("passed") is True and str(item.get("slotId") or "")
                    }
                    if not successful_slots.issubset(grounded_success_slots):
                        raise ValueError("simple_direction_frontier_selected_identity_not_in_snapshot")
        except ValueError as error:
            if str(error) not in self._FRONTIER_PROVIDER_INTEGRITY_ERRORS:
                raise
            failure_outcomes = self._frontier_failure_outcomes_from_claim(
                root=root,
                execution_id=frontier_execution_id,
                reason_code=str(error),
            )
            self._reconcile_frontier_offer(
                portfolio_id=portfolio_id,
                execution_id=frontier_execution_id,
                outcomes=failure_outcomes,
                slot_query_outcomes=[],
                proposal_id=None,
                disposition="provider_pending",
                request_contract_fingerprint=request_contract_fingerprint,
                blocking_layer="provider",
                reason_code=str(error),
                remaining_query_scopes=remaining_query_scopes,
            )
            pending_response = self.response_material(
                portfolio_id=portfolio_id,
                source_assistant_turn_id=source_assistant_turn_id,
                expected_base_version_id=expected_base_version_id,
                update_mode="append",
                proposal_ids=[],
                proposal_delta=0,
            )
            pending_response.update(
                {
                    "status": "provider_pending",
                    "reasonCode": "simple_direction_frontier_provider_schema_pending",
                }
            )
            return pending_response
        provider_failure = next(
            (
                item
                for item in [*(frontier_outcomes or []), *(slot_frontier_outcomes or [])]
                if isinstance(item, dict) and str(item.get("providerOutcome") or "") == "failure"
            ),
            None,
        )
        if str(frontier_execution_id or "") and provider_failure is not None:
            # A transport failure is not a partial proposal and is not evidence
            # that an entity/page was exhausted.  Settle only the server claim;
            # the exact assignment remains available to the controlled retry.
            if not isinstance(compatibility_attempt, dict):
                self._reconcile_frontier_offer(
                    portfolio_id=portfolio_id,
                    execution_id=frontier_execution_id,
                    outcomes=frontier_outcomes,
                    slot_query_outcomes=slot_frontier_outcomes,
                    proposal_id=None,
                    disposition="provider_pending",
                    request_contract_fingerprint=request_contract_fingerprint,
                    blocking_layer="provider",
                    reason_code=str(provider_failure.get("reasonCode") or "provider_unavailable"),
                    remaining_query_scopes=remaining_query_scopes,
                )
            pending_response = self.response_material(
                portfolio_id=portfolio_id,
                source_assistant_turn_id=source_assistant_turn_id,
                expected_base_version_id=expected_base_version_id,
                update_mode="append",
                proposal_ids=[],
                proposal_delta=0,
            )
            pending_response.update(
                {
                    "status": "provider_pending",
                    "reasonCode": "simple_direction_frontier_provider_pending",
                    "frontierExecutionId": str(frontier_execution_id),
                    "frontierAttemptConsumed": False,
                    "frontierStatus": "provider_pending",
                }
            )
            if isinstance(pending_response.get("comparisonSummary"), dict):
                pending_response["comparisonSummary"]["frontierStatus"] = "provider_pending"
            if isinstance(compatibility_attempt, dict):
                self.store.reconcile_simple_direction_compatibility_attempt(
                    portfolio_id=portfolio_id,
                    execution_id=frontier_execution_id,
                    result_assistant_turn_id=source_assistant_turn_id,
                    proposal_id=None,
                    proposal_delta=0,
                    disposition="provider_pending",
                    expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                    reason_code=str(provider_failure.get("reasonCode") or "provider_unavailable"),
                    slot_query_outcomes=slot_frontier_outcomes,
                    remaining_query_scopes=remaining_query_scopes,
                    route_progress=(
                        material.get("simpleOpenRouteAssignment")
                        if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                        else {}
                    ),
                    result_response_payload=self._compatibility_result_payload(
                        pending_response,
                        request_contract_fingerprint=request_contract_fingerprint,
                    ),
                )
            return pending_response
        guide_evidence_usage = (
            verifier.get("guideEvidenceUsage")
            if isinstance(verifier.get("guideEvidenceUsage"), dict)
            else {}
        )
        if guide_evidence_usage and guide_evidence_usage.get("status") != "satisfied":
            route_progress = (
                material.get("simpleOpenRouteAssignment")
                if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                else {}
            )
            zero_response = self.response_material(
                portfolio_id=portfolio_id,
                source_assistant_turn_id=source_assistant_turn_id,
                expected_base_version_id=expected_base_version_id,
                update_mode="replace",
                proposal_ids=[],
                proposal_delta=0,
                reissue_all_capabilities=True,
                allow_empty_frontier_continuation=True,
            )
            zero_response.update(
                {
                    "status": "guide_grounded_requirement_unsatisfied",
                    "reasonCode": "guide_grounded_requirement_unsatisfied",
                    "guideEvidenceUsage": copy.deepcopy(guide_evidence_usage),
                    "frontierExecutionId": str(frontier_execution_id or "") or None,
                    "frontierAttemptConsumed": bool(frontier_attempt or compatibility_attempt) and provider_failure is None,
                }
            )
            if isinstance(compatibility_attempt, dict):
                current_summary = self.store.simple_direction_comparison_summary(portfolio_id=portfolio_id)
                compatibility_frontier_status = (
                    "has_more"
                    if str(current_summary.get("frontierStatus") or "") == "has_more"
                    else "poi_exhausted"
                )
                zero_response["frontierStatus"] = compatibility_frontier_status
                if isinstance(zero_response.get("comparisonSummary"), dict):
                    zero_response["comparisonSummary"]["frontierStatus"] = compatibility_frontier_status
                self.store.reconcile_simple_direction_compatibility_attempt(
                    portfolio_id=portfolio_id,
                    execution_id=frontier_execution_id,
                    result_assistant_turn_id=source_assistant_turn_id,
                    proposal_id=None,
                    proposal_delta=0,
                    disposition="no_material_novelty",
                    expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                    frontier_status=compatibility_frontier_status,
                    reason_code="guide_grounded_requirement_unsatisfied",
                    slot_query_outcomes=slot_frontier_outcomes,
                    remaining_query_scopes=remaining_query_scopes,
                    route_progress=route_progress,
                    result_response_payload=self._compatibility_result_payload(
                        zero_response,
                        request_contract_fingerprint=request_contract_fingerprint,
                    ),
                )
            return zero_response
        direction_evaluation = self._matching_physical_direction(
            portfolio_id=portfolio_id,
            visible_proposal_ids=[str(item) for item in root.get("visibleProposalIds") or [] if str(item)],
            candidate_snapshot=material,
            frontier=copy.deepcopy(root.get("simpleDirectionFrontier") or {}),
            frontier_attempt=frontier_attempt,
            frontier_outcomes=[copy.deepcopy(item) for item in frontier_outcomes or [] if isinstance(item, dict)],
            candidate_confirmation_passed=verifier.get("confirmationPassed") is True,
        )
        if direction_evaluation.status == "incomplete":
            if not isinstance(frontier_attempt, dict):
                raise ValueError("simple_direction_frontier_attempt_missing")
            incomplete_response = self.settle_unresolved_frontier_attempt(
                session_id=session_id,
                planning_root_id=planning_root_id,
                source_assistant_turn_id=source_assistant_turn_id,
                expected_base_version_id=expected_base_version_id,
                request_contract_fingerprint=request_contract_fingerprint,
                frontier_execution_id=frontier_execution_id,
                frontier_outcomes=[
                    copy.deepcopy(item) for item in frontier_outcomes or [] if isinstance(item, dict)
                ],
                slot_frontier_outcomes=[
                    copy.deepcopy(item) for item in slot_frontier_outcomes or [] if isinstance(item, dict)
                ],
                remaining_query_scopes=remaining_query_scopes,
            )
            incomplete_response["simpleDirectionNoveltyEvidence"] = copy.deepcopy(
                material.get("simpleDirectionNoveltyEvidence") or {}
            )
            if guide_evidence_usage:
                incomplete_response["guideEvidenceUsage"] = copy.deepcopy(guide_evidence_usage)
            return incomplete_response
        duplicate_proposal_id = direction_evaluation.matching_proposal_id or ""
        if direction_evaluation.status == "collision":
            if isinstance(compatibility_attempt, dict):
                compatibility_route_progress = (
                    material.get("simpleOpenRouteAssignment")
                    if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                    else {}
                )
                compatibility_progress = self._compatibility_progress_evidence(
                    slot_query_outcomes=slot_frontier_outcomes,
                    proposal_delta=0,
                    route_progress=compatibility_route_progress,
                )
                compatibility_has_more = bool(
                    compatibility_progress["madeProgress"]
                    and (
                        self._compatibility_page_has_more(
                            compatibility_attempt,
                            remaining_query_scopes=remaining_query_scopes,
                            slot_query_outcomes=slot_frontier_outcomes,
                        )
                        or self._compatibility_route_has_more(compatibility_route_progress)
                    )
                )
                frontier_result = {
                    "frontier": {"frontierStatus": "has_more" if compatibility_has_more else "poi_exhausted"}
                }
            else:
                frontier_result = self._reconcile_frontier_offer(
                    portfolio_id=portfolio_id,
                    execution_id=frontier_execution_id,
                    outcomes=frontier_outcomes,
                    slot_query_outcomes=slot_frontier_outcomes,
                    proposal_id=None,
                    disposition="novelty_collision",
                    request_contract_fingerprint=request_contract_fingerprint,
                    blocking_layer="poi",
                    reason_code=str(
                        (
                            material.get("simpleDirectionNoveltyEvidence")
                            if isinstance(material.get("simpleDirectionNoveltyEvidence"), dict)
                            else {}
                        ).get("reasonCode")
                        or "novelty_collision"
                    ),
                    continuation_metadata=(
                        material.get("simpleOpenRouteAssignment")
                        if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                        else None
                    ),
                    remaining_query_scopes=remaining_query_scopes,
                )
            duplicate_response = self.response_material(
                portfolio_id=portfolio_id,
                source_assistant_turn_id=source_assistant_turn_id,
                expected_base_version_id=expected_base_version_id,
                update_mode="append",
                proposal_ids=[],
                proposal_delta=0,
                allow_empty_frontier_continuation=(isinstance(compatibility_attempt, dict) and compatibility_has_more),
            )
            frontier_after_collision = (
                frontier_result.get("frontier") if isinstance(frontier_result.get("frontier"), dict) else {}
            )
            frontier_has_more = str(frontier_after_collision.get("frontierStatus") or "") == "has_more"
            novelty_evidence = (
                material.get("simpleDirectionNoveltyEvidence")
                if isinstance(material.get("simpleDirectionNoveltyEvidence"), dict)
                else {}
            )
            novelty_reason = str(novelty_evidence.get("reasonCode") or "")
            remaining_reason = "candidate_collision_frontier_remaining" if frontier_has_more else "no_material_novelty"
            if isinstance(compatibility_attempt, dict) and compatibility_progress["madeProgress"] is not True:
                remaining_reason = "no_progress_no_query_candidate_or_route_delta"
            duplicate_response.update(
                {
                    "status": (
                        "no_progress"
                        if isinstance(compatibility_attempt, dict)
                        and compatibility_progress["madeProgress"] is not True
                        else "no_material_novelty"
                    ),
                    "reasonCode": (
                        "no_progress_no_query_candidate_or_route_delta"
                        if isinstance(compatibility_attempt, dict)
                        and compatibility_progress["madeProgress"] is not True
                        else remaining_reason
                        if frontier_has_more
                        else novelty_reason or remaining_reason
                    ),
                    "matchingProposalId": (
                        duplicate_proposal_id
                        if duplicate_proposal_id in set(root.get("visibleProposalIds") or [])
                        else None
                    ),
                    "simpleDirectionNoveltyEvidence": copy.deepcopy(novelty_evidence),
                    **(
                        {
                            "frontierExecutionId": str(frontier_execution_id),
                            "frontierAttemptConsumed": True,
                        }
                        if isinstance(compatibility_attempt, dict)
                        else {}
                    ),
                    **({"progress": compatibility_progress} if isinstance(compatibility_attempt, dict) else {}),
                }
            )
            if isinstance(compatibility_attempt, dict):
                compatibility_frontier_status = "has_more" if compatibility_has_more else "poi_exhausted"
                duplicate_response["frontierStatus"] = compatibility_frontier_status
                if isinstance(duplicate_response.get("comparisonSummary"), dict):
                    duplicate_response["comparisonSummary"]["frontierStatus"] = compatibility_frontier_status
                if not compatibility_has_more:
                    duplicate_response["choiceOptions"] = [
                        option
                        for option in duplicate_response.get("choiceOptions") or []
                        if not (
                            isinstance(option, dict)
                            and str(option.get("kind") or "") == "simple_direction_more_plans"
                            and str(option.get("action") or "") == "continue_plan_expansion"
                        )
                    ]
                self.store.reconcile_simple_direction_compatibility_attempt(
                    portfolio_id=portfolio_id,
                    execution_id=frontier_execution_id,
                    result_assistant_turn_id=source_assistant_turn_id,
                    proposal_id=None,
                    proposal_delta=0,
                    disposition="no_material_novelty",
                    expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                    frontier_status=compatibility_frontier_status,
                    reason_code=(
                        "no_progress_no_query_candidate_or_route_delta"
                        if compatibility_progress["madeProgress"] is not True
                        else "candidate_collision_frontier_remaining"
                        if compatibility_has_more
                        else "no_material_novelty"
                    ),
                    slot_query_outcomes=slot_frontier_outcomes,
                    remaining_query_scopes=remaining_query_scopes,
                    route_progress=compatibility_route_progress,
                    result_response_payload=self._compatibility_result_payload(
                        duplicate_response,
                        request_contract_fingerprint=request_contract_fingerprint,
                    ),
                )
            return duplicate_response
        self._reopen_for_direction_offer(
            portfolio_id=portfolio_id,
            expected_base_version_id=expected_base_version_id,
            source_assistant_turn_id=source_assistant_turn_id,
            request_contract_fingerprint=request_contract_fingerprint,
        )
        proposal_id = f"simple_direction_proposal_{uuid4().hex[:16]}"
        choice_id = f"simple_direction_confirm_{uuid4().hex[:16]}"
        rank = int(
            self.db.execute(
                "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()[0]
        )
        brief = {
            "briefId": f"simple_direction_brief_{uuid4().hex[:16]}",
            "title": str(material.get("title") or f"行程方向 {rank + 1}"),
            "directionSignature": self._fingerprint(material),
            "generationSource": "simple_open_v1",
        }
        material["creativeBrief"] = copy.deepcopy(brief)
        proposal_status = "adoption_ready" if verifier.get("confirmationPassed") is True else "blocked"
        proposal_score = {
            "hardConstraintPassed": verifier.get("confirmationPassed") is True,
            "scoreStatus": "not_scored_before_confirmation",
        }
        proposal_lineage = {
            "workflowMode": self.WORKFLOW_MODE,
            "sourceUserTurnId": source_user_turn_id,
            "sourceAssistantTurnId": source_assistant_turn_id,
            "requestContractFingerprint": self._minimum_fingerprint(request_contract_fingerprint),
            "frontierExecutionId": str(frontier_execution_id or "") or None,
            "frontierAttemptFingerprint": (
                str(frontier_attempt.get("attemptFingerprint") or "")
                if isinstance(frontier_attempt, dict)
                else str(compatibility_attempt.get("attemptFingerprint") or "")
                if isinstance(compatibility_attempt, dict)
                else None
            ),
            "itineraryWriteCount": 0,
        }
        guide_requirement = (
            material.get("guideContinuationRequirement")
            if isinstance(material.get("guideContinuationRequirement"), dict)
            else {}
        )
        if guide_requirement:
            proposal_lineage.update(
                {
                    "guideContinuationRequirementFingerprint": str(
                        guide_requirement.get("requirementFingerprint") or ""
                    ),
                    "guideEvidenceSourceAssistantTurnId": str(
                        guide_requirement.get("sourceAssistantTurnId") or ""
                    ),
                    "guideChoiceExecutionId": str(guide_requirement.get("guideChoiceExecutionId") or ""),
                    "guideEvidenceFingerprint": str(guide_requirement.get("evidenceFingerprint") or ""),
                }
            )
        proposal_evidence = {
            "workflowMode": self.WORKFLOW_MODE,
            "readiness": verifier,
            **(
                {"guideEvidenceUsage": copy.deepcopy(verifier.get("guideEvidenceUsage") or {})}
                if guide_requirement
                else {}
            ),
        }
        self.store.upsert_partial_preview(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            choice_id=choice_id,
            snapshot=material,
            brief=brief,
            verifier=verifier,
            score=proposal_score,
            generation_lineage=proposal_lineage,
            status=proposal_status,
            evidence=proposal_evidence,
        )
        titled_material, titled_brief = self._retitle_offered_direction(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            choice_id=choice_id,
            snapshot=material,
            brief=brief,
            verifier=verifier,
            score=proposal_score,
            generation_lineage=proposal_lineage,
            status=proposal_status,
            evidence=proposal_evidence,
            title_generator=title_generator,
        )
        material = titled_material
        brief = titled_brief
        proposal_lineage["proposalSnapshotFingerprint"] = self._fingerprint(material)
        self.store.upsert_partial_preview(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            choice_id=choice_id,
            snapshot=material,
            brief=brief,
            verifier=verifier,
            score=proposal_score,
            generation_lineage=proposal_lineage,
            status=proposal_status,
            evidence=proposal_evidence,
        )
        self._sync_proposal_snapshot_fingerprint(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
        )
        frontier_blocking_layer, frontier_reason_code = self._frontier_blocking_evidence(
            material,
            verifier,
        )
        response_args = {
            "portfolio_id": portfolio_id,
            "source_assistant_turn_id": source_assistant_turn_id,
            "expected_base_version_id": expected_base_version_id,
            "update_mode": "replace" if rank == 0 else "append",
            "proposal_ids": None if rank == 0 else [proposal_id],
            "proposal_delta": 1,
            "reissue_all_capabilities": rank > 0,
            "allow_empty_frontier_continuation": bool(
                isinstance(compatibility_attempt, dict)
                and (
                    self._compatibility_page_has_more(
                        compatibility_attempt,
                        remaining_query_scopes=remaining_query_scopes,
                        slot_query_outcomes=slot_frontier_outcomes,
                    )
                    or self._compatibility_route_has_more(
                        material.get("simpleOpenRouteAssignment")
                        if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                        else {}
                    )
                )
            ),
        }
        self._mark_workflow(portfolio_id)
        if isinstance(compatibility_attempt, dict):
            response = self.response_material(**response_args)
            compatibility_route_progress = (
                material.get("simpleOpenRouteAssignment")
                if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                else {}
            )
            compatibility_progress = self._compatibility_progress_evidence(
                slot_query_outcomes=slot_frontier_outcomes,
                proposal_delta=1,
                route_progress=compatibility_route_progress,
            )
            compatibility_has_more = bool(
                compatibility_progress["madeProgress"]
                and (
                    self._compatibility_page_has_more(
                        compatibility_attempt,
                        remaining_query_scopes=remaining_query_scopes,
                        slot_query_outcomes=slot_frontier_outcomes,
                    )
                    or self._compatibility_route_has_more(compatibility_route_progress)
                )
            )
            if not compatibility_has_more:
                response["choiceOptions"] = [
                    option
                    for option in response.get("choiceOptions") or []
                    if not (
                        isinstance(option, dict)
                        and str(option.get("kind") or "") == "simple_direction_more_plans"
                        and str(option.get("action") or "") == "continue_plan_expansion"
                    )
                ]
            signed_continuations = [
                option
                for option in response.get("choiceOptions") or []
                if isinstance(option, dict)
                and str(option.get("kind") or "") == "simple_direction_more_plans"
                and str(option.get("action") or "") == "continue_plan_expansion"
                and str(option.get("sourceAssistantTurnId") or "") == source_assistant_turn_id
                and str(option.get("rootPortfolioId") or "") == portfolio_id
                and str(option.get("requestContractFingerprint") or "")
                == self._minimum_fingerprint(request_contract_fingerprint)
            ]
            if len(signed_continuations) > 1:
                raise ValueError("simple_direction_compatibility_next_choice_not_unique")
            compatibility_frontier_status = (
                "has_more" if compatibility_has_more and signed_continuations else "poi_exhausted"
            )
            response["frontierStatus"] = compatibility_frontier_status
            if isinstance(response.get("comparisonSummary"), dict):
                response["comparisonSummary"]["frontierStatus"] = compatibility_frontier_status
            response.update(
                {
                    "frontierExecutionId": str(frontier_execution_id),
                    "frontierAttemptConsumed": True,
                    "progress": compatibility_progress,
                }
            )
            self.store.reconcile_simple_direction_compatibility_attempt(
                portfolio_id=portfolio_id,
                execution_id=frontier_execution_id,
                result_assistant_turn_id=source_assistant_turn_id,
                proposal_id=proposal_id,
                proposal_delta=1,
                disposition=("used_ready" if verifier.get("confirmationPassed") is True else "assigned_partial"),
                expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
                frontier_status=compatibility_frontier_status,
                reason_code=frontier_reason_code,
                slot_query_outcomes=slot_frontier_outcomes,
                remaining_query_scopes=remaining_query_scopes,
                route_progress=compatibility_route_progress,
                result_response_payload=self._compatibility_result_payload(
                    response,
                    request_contract_fingerprint=request_contract_fingerprint,
                ),
            )
        else:
            self._reconcile_frontier_offer(
                portfolio_id=portfolio_id,
                execution_id=frontier_execution_id,
                outcomes=frontier_outcomes,
                slot_query_outcomes=slot_frontier_outcomes,
                proposal_id=proposal_id,
                disposition=("used_ready" if verifier.get("confirmationPassed") is True else "assigned_partial"),
                request_contract_fingerprint=request_contract_fingerprint,
                blocking_layer=frontier_blocking_layer,
                reason_code=frontier_reason_code,
                continuation_metadata=(
                    material.get("simpleOpenRouteAssignment")
                    if isinstance(material.get("simpleOpenRouteAssignment"), dict)
                    else None
                ),
                remaining_query_scopes=remaining_query_scopes,
            )
            response = self.response_material(**response_args)
        if guide_evidence_usage:
            response["guideEvidenceUsage"] = copy.deepcopy(guide_evidence_usage)
        return response

    def _retitle_offered_direction(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        choice_id: str,
        snapshot: dict[str, Any],
        brief: dict[str, Any],
        verifier: dict[str, Any],
        score: dict[str, Any],
        generation_lineage: dict[str, Any],
        status: str,
        evidence: dict[str, Any],
        title_generator: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        material = copy.deepcopy(snapshot)
        sibling_snapshots = [
            self._json(row["snapshot_json"])
            for row in self._visible_rows(
                portfolio_id,
                [
                    str(item)
                    for item in (self._root_by_id(portfolio_id) or {}).get("visibleProposalIds") or []
                    if str(item) and str(item) != proposal_id
                ],
            )
        ]
        material["proposalSpecificTitleSignals"] = CreativeProposalTitleService.proposal_specific_title_signals(
            material,
            sibling_snapshots=sibling_snapshots,
        )
        if not material["proposalSpecificTitleSignals"] and verifier.get("confirmationPassed") is not True:
            factual_title = self._partial_direction_label(material)
            material.pop("portfolioTitleEvidence", None)
            material["title"] = factual_title
            material["portfolioTitleGeneration"] = {
                "schemaVersion": CreativeProposalTitleService.AGENT_GENERATION_STATUS_SCHEMA,
                "status": "not_applicable",
                "retryable": False,
                "attemptCount": 0,
                "maxAttempts": 2,
                "candidateCount": 0,
                "titleDecisionSource": "partial_fact_label",
                "requiredTitleSignals": [],
                "usedTitleSignal": None,
                "reasonCode": "proposal_specific_title_signal_missing",
            }
            titled_brief = copy.deepcopy(brief)
            titled_brief["title"] = factual_title
            material["creativeBrief"] = {
                **copy.deepcopy(material.get("creativeBrief") or {}),
                "title": factual_title,
            }
            return material, titled_brief
        existing = material.get("portfolioTitleEvidence")
        if CreativeProposalTitleService.is_valid_agent_projection(material, existing):
            return material, copy.deepcopy(brief)
        reserved_titles = {
            str(item.get("title") or "").strip()
            for item in self.store.visible_comparison_projections(portfolio_id=portfolio_id)
            if isinstance(item, dict)
            and str(item.get("proposalId") or "") != proposal_id
            and str(item.get("title") or "").strip()
        }
        if CreativeProposalTitleService.is_valid_server_fallback_title(
            material,
            reserved_titles=reserved_titles,
        ):
            return material, copy.deepcopy(brief)
        intents = {
            str(metadata.get("intentType") or "").strip()
            for day in material.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
            for metadata in [
                segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            ]
            if str(metadata.get("intentType") or "").strip()
        }
        if not callable(title_generator):
            titled = CreativeProposalTitleService.with_server_fallback_title(
                material,
                reason_code="title_provider_unavailable",
                reserved_titles=reserved_titles,
            )
        else:
            context = CreativeProposalTitleService.agent_generation_context(
                material,
                city=str(material.get("city") or ""),
                primary_axis="dynamic_simple_direction",
                secondary_axes=sorted(intents),
                reserved_titles=reserved_titles,
            )
            titled = CreativeProposalTitleService.generate_and_apply_agent_title(
                material,
                generator=title_generator,
                context=context,
                reserved_titles=reserved_titles,
            )
        if titled == material:
            return material, copy.deepcopy(brief)
        titled_brief = copy.deepcopy(brief)
        titled_brief["title"] = str(
            titled.get("creativeBrief", {}).get("title")
            if isinstance(titled.get("creativeBrief"), dict)
            else titled.get("title") or titled_brief.get("title") or f"行程方向 {proposal_id}"
        )
        self.store.upsert_partial_preview(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            choice_id=choice_id,
            snapshot=titled,
            brief=titled_brief,
            verifier=verifier,
            score=score,
            generation_lineage=generation_lineage,
            status=status,
            evidence=evidence,
        )
        return titled, titled_brief

    @staticmethod
    def _partial_direction_label(snapshot: dict[str, Any]) -> str:
        campus_name = "该高校"
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                if str(metadata.get("intentType") or "") == "campus_visit" and str(poi.get("name") or ""):
                    campus_name = str(poi.get("name") or "").strip()
                    break
            if campus_name != "该高校":
                break
        pending_labels = {
            "park": "公园",
            "meal": "餐饮",
            "local_food": "餐饮",
            "campus_visit": "高校",
        }
        missing = "路线"
        for slot in snapshot.get("portfolioPendingSlots") or []:
            if not isinstance(slot, dict):
                continue
            intent = str(slot.get("intentType") or slot.get("optionalExperienceFamily") or "")
            missing = pending_labels.get(intent, "地点")
            break
        return f"{campus_name}方向 · 待补全{missing}"

    def response_material(
        self,
        *,
        portfolio_id: str,
        source_assistant_turn_id: str,
        expected_base_version_id: Optional[str],
        update_mode: str = "append",
        proposal_ids: Optional[list[str]] = None,
        proposal_delta: int = 0,
        reissue_all_capabilities: bool = False,
        allow_empty_frontier_continuation: bool = False,
        read_only: bool = False,
    ) -> dict[str, Any]:
        root = self._root_by_id(portfolio_id)
        if root is None or root["workflowMode"] != self.WORKFLOW_MODE:
            raise ValueError("simple_direction_portfolio_not_found")
        selected_id = str(root.get("selectedProposalId") or "")
        visible_ids = [str(item) for item in root.get("visibleProposalIds") or [] if str(item)]
        total_visible_count = len(visible_ids)
        comparison_summary = self.store.simple_direction_comparison_summary(portfolio_id=portfolio_id, persist=not read_only)
        classification_counts = {
            "adoptionReadyProposalCount": int(comparison_summary.get("adoptionReadyCount") or 0),
            "partialComparisonProposalCount": int(comparison_summary.get("repairablePartialCount") or 0),
            "verifiedComparisonProposalCount": int(
                (self.store.summary(portfolio_id=portfolio_id)).get("verifiedComparisonProposalCount") or 0
            ),
        }
        frontier = root.get("simpleDirectionFrontier") if isinstance(root.get("simpleDirectionFrontier"), dict) else {}
        compatibility_frontier = (
            root.get("simpleDirectionCompatibilityFrontier")
            if isinstance(root.get("simpleDirectionCompatibilityFrontier"), dict)
            else {}
        )
        frontier_status = str(comparison_summary.get("frontierStatus") or "")
        remaining_qualified_count = int(comparison_summary.get("remainingQualifiedEntityCount") or 0)
        all_rows = self._visible_rows(portfolio_id, visible_ids)
        total_adoption_ready_count = 0
        requested_ids: Optional[set[str]] = None
        if proposal_ids is not None:
            requested_ids = {str(item) for item in proposal_ids if str(item)}
        projections: list[dict[str, Any]] = []
        choices: list[dict[str, Any]] = []
        for row in all_rows:
            proposal_id = str(row["id"])
            snapshot = self._json(row["snapshot_json"])
            activation_verifier = self.activation_verifier(snapshot)
            proposal_verifier = self._proposal_verifier(snapshot)
            lifecycle_status = str(row["status"] or "adoption_ready")
            selected_committed = proposal_id == selected_id and lifecycle_status == "committed"
            projection_snapshot = copy.deepcopy(snapshot)
            projection_snapshot["portfolioVerifier"] = copy.deepcopy(proposal_verifier)
            projection = PlanComparisonPreviewService.project_snapshot(
                projection_snapshot,
                planning_selection_root_turn_id=str(root["planningRootId"]),
                root_portfolio_id=portfolio_id,
                proposal_id=proposal_id,
                source_assistant_turn_id=source_assistant_turn_id,
                choice_id=str(row["choice_id"]),
                active_version_id=(expected_base_version_id if proposal_id == selected_id else None),
                expected_base_version_id=expected_base_version_id,
                is_partial=str(snapshot.get("status") or "") in {"partial", "draft"},
                is_adopted=proposal_id == selected_id,
                adoption_ready=proposal_verifier.get("confirmationPassed") is True,
                comparison_role=("current_active_draft" if proposal_id == selected_id else "candidate_proposal"),
                origin_projection_mode="full_proposal",
                simple_direction_scope=True,
                # A proposal may be displayed as a truthful read-only partial,
                # but incomplete Provider route evidence never grants an
                # adoption capability, regardless of whether the request used
                # the compact adjacency extension.
                route_failures_non_blocking=False,
            )
            adoption_ready = proposal_verifier.get("adoptionReady") is True
            strictly_verified = proposal_verifier.get("strictlyVerified") is True
            structure_ready = proposal_verifier.get("structureReady") is True
            canonical_blockers = list(
                dict.fromkeys(
                    [
                        *[str(item) for item in projection.get("blockingReasons") or [] if str(item)],
                        *[str(item) for item in proposal_verifier.get("hardFailures") or [] if str(item)],
                    ]
                )
            )
            projection.update(
                {
                    "adoptionReady": adoption_ready,
                    "confirmationPassed": proposal_verifier.get("confirmationPassed") is True,
                    "draftAdoptionReady": bool(adoption_ready and not strictly_verified),
                    "partialAdoptionReady": bool(adoption_ready and not strictly_verified),
                    "strictlyVerified": strictly_verified,
                    "structureReady": structure_ready,
                    "adoptionMode": str(proposal_verifier.get("adoptionMode") or "blocked"),
                    "isPartial": proposal_verifier.get("isPartial") is True,
                    "status": "partial" if proposal_verifier.get("isPartial") is True else "complete",
                    "requiredPlanningDayNumbers": copy.deepcopy(
                        proposal_verifier.get("requiredPlanningDayNumbers") or []
                    ),
                    "explicitRestDayNumbers": copy.deepcopy(
                        proposal_verifier.get("explicitRestDayNumbers") or []
                    ),
                    "uncoveredDayNumbers": copy.deepcopy(
                        proposal_verifier.get("uncoveredDayNumbers") or []
                    ),
                    "routeStatus": str(proposal_verifier.get("routeStatus") or "route_pending"),
                    "blockingReasons": canonical_blockers,
                    "blockingReasonLabels": [
                        PlanComparisonPreviewService._blocking_reason_label(item) for item in canonical_blockers
                    ],
                    "currentReadiness": "confirmation_ready" if adoption_ready else "blocked",
                    "guideEvidenceUsage": copy.deepcopy(
                        proposal_verifier.get("guideEvidenceUsage") or {}
                    ),
                }
            )
            if proposal_verifier.get("compactRouteContractRequired") is True:
                route_audit = (
                    snapshot.get("simpleOpenRouteAssignment")
                    if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict)
                    else {}
                )
                route_ready = str(proposal_verifier.get("routeStatus") or "") == "route_ready"
                frozen_expected_pairs = [
                    item for item in proposal_verifier.get("frozenExpectedPairs") or [] if isinstance(item, dict)
                ]
                expected_pair_count = len(
                    frozen_expected_pairs
                    or [item for item in route_audit.get("expectedPairs") or [] if isinstance(item, dict)]
                )
                verified_pair_count = len(
                    [item for item in route_audit.get("verifiedPairs") or [] if isinstance(item, dict)]
                )
                strict_blockers = [str(item) for item in proposal_verifier.get("hardFailures") or [] if str(item)]
                projection.update(
                    {
                        "routeStatus": "route_ready" if route_ready else "route_pending",
                        "routeExpectedLegCount": expected_pair_count,
                        "routeVerifiedLegCount": verified_pair_count,
                        "routeErrorLegCount": max(0, expected_pair_count - verified_pair_count),
                        "mealExperienceBriefs": copy.deepcopy(
                            proposal_verifier.get("mealExperienceBriefs") or []
                        ),
                        "mealSemanticEvidence": copy.deepcopy(
                            proposal_verifier.get("mealSemanticEvidence") or []
                        ),
                        "mealThemeSignature": copy.deepcopy(
                            proposal_verifier.get("mealThemeSignature") or []
                        ),
                        "mealQualityPassed": proposal_verifier.get("mealQualityPassed") is True,
                        "mealDiversityPassed": proposal_verifier.get("mealDiversityPassed") is True,
                        "mealUnresolvedReasons": copy.deepcopy(
                            proposal_verifier.get("mealUnresolvedReasons") or []
                        ),
                        "routeComfortEvidence": copy.deepcopy(
                            proposal_verifier.get("routeComfortEvidence") or {}
                        ),
                        "routeSummary": (
                            f"已核验相邻路线 {verified_pair_count}/{expected_pair_count} 段"
                            if route_ready
                            else f"相邻路线证据不足 {verified_pair_count}/{expected_pair_count} 段"
                        ),
                        "blockingReasons": strict_blockers,
                        "blockingReasonLabels": [
                            PlanComparisonPreviewService._blocking_reason_label(item) for item in strict_blockers
                        ],
                    }
                )
            if (
                proposal_verifier.get("compactRouteContractRequired") is True
                and proposal_verifier.get("confirmationPassed") is not True
            ):
                compact_route_blockers = [
                    str(item)
                    for item in proposal_verifier.get("hardFailures") or []
                    if str(item)
                    in {
                        "route_evidence_incomplete",
                        "provider_route_matrix_incomplete",
                        "adjacent_leg_limit_exceeded",
                        "fixed_poi_route_constraint_conflict",
                    }
                ]
                merged_blockers = list(
                    dict.fromkeys(
                        [
                            *[str(item) for item in projection.get("blockingReasons") or [] if str(item)],
                            *compact_route_blockers,
                        ]
                    )
                )
                projection["blockingReasons"] = merged_blockers
                projection["blockingReasonLabels"] = [
                    PlanComparisonPreviewService._blocking_reason_label(item) for item in merged_blockers
                ]
            projection = self._preserve_committed_activation_when_title_pending(
                projection,
                selected_committed=selected_committed,
                activation_verified=activation_verifier["passed"] is True,
            )
            capability_ready = bool(
                proposal_verifier.get("confirmationPassed") is True
                and projection.get("adoptionReady") is True
                and not projection.get("blockingReasons")
            )
            direction_title = str(projection.get("displayTitle") or projection.get("title") or "该行程方向").strip()
            confirm_label = f"确认编辑「{direction_title}」"
            if capability_ready:
                total_adoption_ready_count += 1
            projection.update(
                {
                    "workflowMode": self.WORKFLOW_MODE,
                    "proposalLifecycleStatus": lifecycle_status,
                    **(
                        {
                            # Every *adoptable* visible card receives a fresh
                            # opaque activation capability.  Blocked material
                            # keeps its truthful readiness action from the
                            # projection and cannot be confirmed by label.
                            "nextAction": "confirm_edit",
                            "nextActionLabel": confirm_label,
                        }
                        if capability_ready
                        else {}
                    ),
                }
            )
            if requested_ids is None or proposal_id in requested_ids:
                projections.append(projection)
            if capability_ready and (requested_ids is None or proposal_id in requested_ids or reissue_all_capabilities):
                choice = {
                    "id": str(row["choice_id"]),
                    "choiceId": str(row["choice_id"]),
                    "action": "select_plan_proposal",
                    "kind": "plan_proposal",
                    "scopeKind": "comparison",
                    "label": confirm_label,
                    "description": f"确认后将「{direction_title}」通过正式单写者创建为可编辑行程。",
                    "sourceAssistantTurnId": source_assistant_turn_id,
                    "sourceUserTurnId": str(root["planningRootId"]),
                    "planningSelectionRootTurnId": str(root["planningRootId"]),
                    "rootPortfolioId": portfolio_id,
                    "proposalId": proposal_id,
                    "requestContractFingerprint": str(root.get("requestContractFingerprint") or ""),
                    "expectedBaseVersionId": expected_base_version_id,
                    "workflowMode": self.WORKFLOW_MODE,
                    "comparisonProjection": copy.deepcopy(projection),
                }
                # Append responses carry the new card as their projection
                # payload while also refreshing prior capabilities.  Keep the
                # newly offered direction first so existing identity-only
                # consumers do not accidentally activate the refreshed A card.
                if requested_ids is not None and proposal_id in requested_ids:
                    choices.insert(0, choice)
                else:
                    choices.append(choice)
        frontier_controls_expansion = bool(frontier) or bool(compatibility_frontier)
        can_expand = bool(
            allow_empty_frontier_continuation
            or (frontier_status == "has_more" if frontier_controls_expansion else proposal_delta > 0)
        )
        provider_exhausted_hard_poi_partial_can_expand = bool(
            not frontier_controls_expansion
            and proposal_delta > 0
            and any(
                projection.get("adoptionReady") is not True
                and any(
                    isinstance(slot, dict)
                    and slot.get("simpleDirectionProviderExhausted") is True
                    and slot.get("simpleDirectionRequirementLineageConflict") is not True
                    and str(slot.get("requirementLevel") or "required") in {"hard", "required"}
                    for slot in projection.get("pendingSlots") or []
                )
                for projection in projections
                if isinstance(projection, dict)
            )
        )
        newly_visible_partial_can_expand = bool(
            proposal_delta > 0
            and int(classification_counts.get("partialComparisonProposalCount") or 0) > 0
            and (frontier_controls_expansion or provider_exhausted_hard_poi_partial_can_expand)
        )
        if (
            total_adoption_ready_count >= 1 or newly_visible_partial_can_expand or allow_empty_frontier_continuation
        ) and can_expand:
            continuation_choice = SimpleDirectionExecutionEvidenceService.build_continuation_choice(
                    portfolio_id=portfolio_id,
                    source_assistant_turn_id=source_assistant_turn_id,
                    planning_root_id=str(root["planningRootId"]),
                    request_fingerprint=str(root.get("requestContractFingerprint") or ""),
                    expected_base_version_id=expected_base_version_id,
                )
            choices.append(continuation_choice)
            choices.append(
                TravelGuideAdviceService.build_choice(
                    source_assistant_turn_id=source_assistant_turn_id,
                    portfolio_id=portfolio_id,
                    planning_root_id=str(root["planningRootId"]),
                    request_fingerprint=str(root.get("requestContractFingerprint") or ""),
                    expected_base_version_id=expected_base_version_id,
                    workflow_mode=self.WORKFLOW_MODE,
                )
            )
        return {
            "workflowMode": self.WORKFLOW_MODE,
            "planningSelectionRootTurnId": str(root["planningRootId"]),
            "rootPortfolioId": portfolio_id,
            "comparisonProjections": projections,
            "comparisonProjectionUpdateMode": update_mode,
            "choiceOptions": choices,
            "visibleProposalCount": total_visible_count,
            "payloadProposalCount": len(projections),
            "adoptionReadyProposalCount": comparison_summary["adoptionReadyCount"],
            "verifiedComparisonProposalCount": int(classification_counts.get("verifiedComparisonProposalCount") or 0),
            "partialComparisonProposalCount": comparison_summary["repairablePartialCount"],
            "remainingQualifiedEntityCount": remaining_qualified_count,
            "frontierStatus": comparison_summary["frontierStatus"],
            "comparisonSummary": comparison_summary,
            "proposalDelta": proposal_delta,
        }

    def rotate_capability_carrier(
        self,
        *,
        session_id: str,
        portfolio_id: str,
        expected_source_assistant_turn_id: str,
        next_source_assistant_turn_id: str,
        request_contract_fingerprint: str,
        retry_guide: bool = False,
    ) -> dict[str, Any]:
        """Move all current root capabilities onto one newer assistant turn."""

        row = self.db.execute(
            """SELECT session_id, source_user_turn_id, source_assistant_turn_id,
                expected_base_version_id, request_contract_fingerprint, status,
                expires_at
            FROM agent_plan_portfolios WHERE id = ?""",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("simple_direction_portfolio_not_found")
        if (
            str(row["session_id"] or "") != str(session_id or "")
            or str(row["request_contract_fingerprint"] or "")
            != str(request_contract_fingerprint or "")
            or str(row["status"] or "") != "awaiting_selection"
            or PlanPortfolioStore._is_expired(row["expires_at"])
        ):
            raise ValueError("simple_direction_capability_carrier_scope_invalid")
        current_source_turn_id = str(row["source_assistant_turn_id"] or "")
        if current_source_turn_id != str(expected_source_assistant_turn_id or ""):
            raise ValueError("simple_direction_capability_carrier_stale")
        next_turn = self.db.execute(
            """SELECT id FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (next_source_assistant_turn_id, session_id),
        ).fetchone()
        if next_turn is None:
            raise ValueError("simple_direction_capability_carrier_turn_missing")
        latest_turn = self.db.execute("SELECT id FROM conversation_turns WHERE session_id = ? AND role = 'assistant' AND status != 'superseded' ORDER BY turn_index DESC LIMIT 1", (session_id,)).fetchone()
        current_session = self.db.execute("SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        if (latest_turn is None or latest_turn[0] != next_source_assistant_turn_id or current_session is None
                or str(current_session[0] or "") != str(row["expected_base_version_id"] or "")):
            raise ValueError("simple_direction_capability_carrier_stale")

        comparison_summary = self.store.simple_direction_comparison_summary(portfolio_id=portfolio_id, persist=False)
        material = self.response_material(
            portfolio_id=portfolio_id,
            source_assistant_turn_id=next_source_assistant_turn_id,
            expected_base_version_id=(str(row["expected_base_version_id"] or "") or None),
            update_mode="replace",
            proposal_delta=0,
            reissue_all_capabilities=True,
            read_only=True,
            allow_empty_frontier_continuation=(
                str(comparison_summary.get("frontierStatus") or "") == "has_more"
            ),
        )
        choices = [dict(item) for item in material.get("choiceOptions") or [] if isinstance(item, dict)]
        choices = [
            item
            for item in choices
            if not (
                str(item.get("action") or "") == "search_travel_guide_advice"
                and str(item.get("kind") or "") == "travel_guide_advice"
            )
        ]
        choices.append(
            TravelGuideAdviceService.build_choice(
                source_assistant_turn_id=next_source_assistant_turn_id,
                portfolio_id=portfolio_id,
                planning_root_id=str(row["source_user_turn_id"] or ""),
                request_fingerprint=str(row["request_contract_fingerprint"] or ""),
                expected_base_version_id=(str(row["expected_base_version_id"] or "") or None),
                retry=retry_guide,
                workflow_mode=self.WORKFLOW_MODE,
            )
        )
        material["choiceOptions"] = choices
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios
            SET source_assistant_turn_id = ?, updated_at = ?
            WHERE id = ? AND session_id = ? AND source_assistant_turn_id = ?
              AND request_contract_fingerprint = ? AND status = 'awaiting_selection'""",
            (
                next_source_assistant_turn_id,
                datetime.now(timezone.utc).isoformat(),
                portfolio_id,
                session_id,
                expected_source_assistant_turn_id,
                request_contract_fingerprint,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("simple_direction_capability_carrier_stale")
        return material

    def reconcile_legacy_guide_capability_carrier(self, *, session_id: str) -> bool:
        self.db.execute("SAVEPOINT legacy_carrier_reconcile")
        try:
            changed = self._reconcile_legacy_guide_capability_carrier(session_id=session_id)
            self.db.execute("RELEASE SAVEPOINT legacy_carrier_reconcile")
            self.db.commit()
            return changed
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT legacy_carrier_reconcile")
            self.db.execute("RELEASE SAVEPOINT legacy_carrier_reconcile")
            raise

    def _reconcile_legacy_guide_capability_carrier(self, *, session_id: str) -> bool:
        """Repair a pre-rotation successful guide result before intent routing."""

        latest = self.db.execute(
            """SELECT * FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status != 'superseded'
            ORDER BY turn_index DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if latest is None:
            return False
        raw_payload = str(latest["agent_response_json"] or "")
        payload = self._json(raw_payload)
        if str(payload.get("mode") or "") != "travel_guide_advice":
            return self._reconcile_misrouted_guide_clarification(
                session_id=session_id,
                latest=latest,
                raw_payload=raw_payload,
                payload=payload,
            )
        existing_actions = {
            str(item.get("action") or "")
            for item in payload.get("choiceOptions") or []
            if isinstance(item, dict)
        }
        if existing_actions & {"continue_plan_expansion", "select_plan_proposal"}:
            return False
        execution = self.db.execute(
            """SELECT source_turn_id FROM agent_choice_executions
            WHERE session_id = ? AND execution_turn_id = ? AND action = 'search_travel_guide_advice'
              AND status = 'succeeded'
            ORDER BY updated_at DESC LIMIT 1""",
            (session_id, str(latest["id"])),
        ).fetchone()
        if execution is None:
            return False
        request_payload = self._json(latest["agent_request_json"])
        selected = (
            request_payload.get("selectedAgentChoice")
            if isinstance(request_payload.get("selectedAgentChoice"), dict)
            else {}
        )
        option = selected.get("option") if isinstance(selected.get("option"), dict) else {}
        portfolio_id = str(option.get("rootPortfolioId") or payload.get("rootPortfolioId") or "")
        request_fingerprint = str(
            option.get("requestContractFingerprint") or payload.get("requestContractFingerprint") or ""
        )
        expected_source_turn_id = str(execution["source_turn_id"] or option.get("sourceAssistantTurnId") or "")
        if not all((portfolio_id, request_fingerprint, expected_source_turn_id)):
            return False
        material = self.rotate_capability_carrier(
            session_id=session_id,
            portfolio_id=portfolio_id,
            expected_source_assistant_turn_id=expected_source_turn_id,
            next_source_assistant_turn_id=str(latest["id"]),
            request_contract_fingerprint=request_fingerprint,
            retry_guide=True,
        )
        from src.services.conversation_operation_identity import ConversationOperationIdentity
        material = ConversationOperationIdentity(self.db).reissue_material(session_id, expected_source_turn_id, material)
        payload.update(
            {
                "workflowMode": material.get("workflowMode"),
                "planningSelectionRootTurnId": material.get("planningSelectionRootTurnId"),
                "rootPortfolioId": material.get("rootPortfolioId"),
                "comparisonProjections": material.get("comparisonProjections") or [],
                "comparisonProjectionUpdateMode": "replace",
                "choiceOptions": material.get("choiceOptions") or [],
                "comparisonSummary": material.get("comparisonSummary"),
                "frontierStatus": material.get("frontierStatus"),
            }
        )
        updated = self.db.execute(
            """UPDATE conversation_turns SET agent_response_json = ?, updated_at = ?
            WHERE id = ? AND session_id = ? AND agent_response_json = ? AND status != 'superseded'""",
            (
                json.dumps(payload, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
                str(latest["id"]),
                session_id,
                raw_payload,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("simple_direction_capability_carrier_stale")
        return True

    def _reconcile_misrouted_guide_clarification(
        self,
        *,
        session_id: str,
        latest: sqlite3.Row,
        raw_payload: str,
        payload: dict[str, Any],
    ) -> bool:
        """Reissue one guide continuation from its exact failed request lineage.

        This compatibility path never searches backwards by time.  It accepts
        only the guide turn id already frozen into the failed request's
        continuationContext, and only the historic low-confidence routing
        short circuit that provably produced zero planning writes.
        """

        if str(payload.get("mode") or "") != "clarification":
            return False
        request_payload = self._json(latest["agent_request_json"])
        conversation_intent = (
            request_payload.get("conversationIntent")
            if isinstance(request_payload.get("conversationIntent"), dict)
            else {}
        )
        conversation_capability = (
            request_payload.get("conversationCapability")
            if isinstance(request_payload.get("conversationCapability"), dict)
            else {}
        )
        decision_state = (
            request_payload.get("agentDecisionState")
            if isinstance(request_payload.get("agentDecisionState"), dict)
            else {}
        )
        if (
            str(conversation_intent.get("reasonCode") or "") != "intent_confidence_below_threshold"
            or conversation_intent.get("requiresClarification") is not True
            or str(conversation_capability.get("status") or "") != "not_required"
            or str(conversation_capability.get("reasonCode") or "") != "intent_requires_clarification"
            or decision_state.get("controllerFullCalled") is True
            or any(
                int(payload.get(field) or 0) != 0
                for field in (
                    "newProposalDelta",
                    "proposalDelta",
                    "versionDelta",
                    "patchDelta",
                    "routeWriteDelta",
                )
            )
        ):
            return False
        planning_run_id = str(latest["planning_run_id"] or "")
        planning_run = (
            self.db.execute(
                "SELECT run_type FROM planning_runs WHERE id = ?",
                (planning_run_id,),
            ).fetchone()
            if planning_run_id
            else None
        )
        if planning_run is None or str(planning_run["run_type"] or "") != "agent_clarification":
            return False

        continuation = (
            request_payload.get("continuationContext")
            if isinstance(request_payload.get("continuationContext"), dict)
            else {}
        )
        capability_source_turn_id = str(continuation.get("sourceAssistantTurnId") or "")
        if not capability_source_turn_id or capability_source_turn_id == str(latest["id"] or ""):
            return False
        capability_row = self.db.execute(
            """SELECT id, agent_response_json FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (capability_source_turn_id, session_id),
        ).fetchone()
        if capability_row is None:
            return False
        capability_payload = self._json(capability_row["agent_response_json"])
        recovery_lineage = (
            capability_payload.get("guideContinuationRecovery")
            if isinstance(capability_payload.get("guideContinuationRecovery"), dict)
            else {}
        )
        recovery_evidence_fingerprint = ""
        if str(capability_payload.get("mode") or "") == "travel_guide_advice":
            guide_turn_id = capability_source_turn_id
            guide_payload = capability_payload
        else:
            if (
                str(capability_payload.get("mode") or "") != "clarification"
                or str(recovery_lineage.get("schemaVersion") or "")
                != "guide-continuation-recovery-v1"
                or str(recovery_lineage.get("status") or "") != "reissued"
                or any(
                    int(recovery_lineage.get(field) or 0) != 0
                    for field in (
                        "proposalDelta",
                        "versionDelta",
                        "patchDelta",
                        "routeWriteDelta",
                    )
                )
            ):
                return False
            guide_turn_id = str(recovery_lineage.get("guideEvidenceSourceAssistantTurnId") or "")
            recovery_evidence_fingerprint = str(
                recovery_lineage.get("guideEvidenceFingerprint") or ""
            )
            if (
                not guide_turn_id
                or guide_turn_id == capability_source_turn_id
                or not recovery_evidence_fingerprint
            ):
                return False
            guide_row = self.db.execute(
                """SELECT id, agent_response_json FROM conversation_turns
                WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
                (guide_turn_id, session_id),
            ).fetchone()
            if guide_row is None:
                return False
            guide_payload = self._json(guide_row["agent_response_json"])
        guide_advice = (
            guide_payload.get("guideAdvice") if isinstance(guide_payload.get("guideAdvice"), dict) else {}
        )
        if (
            str(guide_payload.get("mode") or "") != "travel_guide_advice"
            or str(guide_advice.get("status") or "") != "completed"
            or not TravelGuideAdviceService.extract_place_hints(guide_advice)
        ):
            return False
        source_refs = [item for item in guide_advice.get("sourceRefs") or [] if isinstance(item, dict)]
        query_fingerprint = str(guide_advice.get("queryFingerprint") or "")
        evidence_fingerprint = str(guide_advice.get("evidenceFingerprint") or "")
        if (
            not query_fingerprint
            or not evidence_fingerprint
            or GuideContinuationRequirementService.evidence_fingerprint(
                query_fingerprint=query_fingerprint,
                source_fingerprints=[str(item.get("sourceFingerprint") or "") for item in source_refs],
            )
            != evidence_fingerprint
            or (
                recovery_evidence_fingerprint
                and recovery_evidence_fingerprint != evidence_fingerprint
            )
        ):
            return False
        guide_execution = self.db.execute(
            """SELECT id, outcome_json FROM agent_choice_executions
            WHERE session_id = ? AND action = 'search_travel_guide_advice'
              AND status = 'succeeded' AND execution_turn_id = ?
            ORDER BY updated_at DESC LIMIT 1""",
            (session_id, guide_turn_id),
        ).fetchone()
        if guide_execution is None:
            return False
        execution_outcome = self._json(guide_execution["outcome_json"])
        execution_advice = (
            execution_outcome.get("guideAdvice")
            if isinstance(execution_outcome.get("guideAdvice"), dict)
            else {}
        )
        if (
            str(execution_outcome.get("mode") or "") != "travel_guide_advice"
            or str(execution_advice.get("evidenceFingerprint") or "") != evidence_fingerprint
            or any(
                int(execution_outcome.get(field) or 0) != 0
                for field in (
                    "newProposalDelta",
                    "proposalDelta",
                    "proposalWriteDelta",
                    "versionDelta",
                    "patchDelta",
                    "routeWriteDelta",
                )
            )
        ):
            return False

        source_continuations = [
            item
            for item in capability_payload.get("choiceOptions") or []
            if isinstance(item, dict)
            and str(item.get("action") or "") == "continue_plan_expansion"
            and str(item.get("kind") or "") == "simple_direction_more_plans"
            and str(item.get("sourceAssistantTurnId") or "") == capability_source_turn_id
        ]
        if len(source_continuations) != 1:
            return False
        source_option = source_continuations[0]
        portfolio_id = str(source_option.get("rootPortfolioId") or "")
        planning_root_id = str(source_option.get("planningSelectionRootTurnId") or "")
        request_fingerprint = str(source_option.get("requestContractFingerprint") or "")
        if recovery_lineage and (
            str(source_option.get("guideEvidenceSourceAssistantTurnId") or "") != guide_turn_id
            or str(source_option.get("guideChoiceExecutionId") or "") != str(guide_execution["id"] or "")
            or str(source_option.get("guideEvidenceFingerprint") or "") != evidence_fingerprint
        ):
            return False
        root = self._root_by_id(portfolio_id)
        session = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if (
            root is None
            or session is None
            or str(root.get("sessionId") or "") != session_id
            or str(root.get("planningRootId") or "") != planning_root_id
            or str(root.get("requestContractFingerprint") or "") != request_fingerprint
            or str(root.get("sourceAssistantTurnId") or "") != capability_source_turn_id
            or str(root.get("expectedBaseVersionId") or "") != str(session["active_version_id"] or "")
            or str(request_payload.get("planningSelectionRootTurnId") or planning_root_id) != planning_root_id
            or str(request_payload.get("rootPortfolioId") or portfolio_id) != portfolio_id
            or str(
                request_payload.get("_simpleDirectionRequestContractFingerprint")
                or request_payload.get("requestContractFingerprint")
                or request_fingerprint
            )
            != request_fingerprint
        ):
            return False
        old_choice_id = str(source_option.get("id") or source_option.get("choiceId") or "")
        if not old_choice_id:
            return False
        already_claimed = self.db.execute(
            """SELECT 1 FROM agent_choice_executions
            WHERE session_id = ? AND source_turn_id = ? AND choice_id = ? LIMIT 1""",
            (session_id, capability_source_turn_id, old_choice_id),
        ).fetchone()
        if already_claimed is not None:
            return False

        material = self.rotate_capability_carrier(
            session_id=session_id,
            portfolio_id=portfolio_id,
            expected_source_assistant_turn_id=capability_source_turn_id,
            next_source_assistant_turn_id=str(latest["id"]),
            request_contract_fingerprint=request_fingerprint,
            retry_guide=True,
        )
        fresh_continuations = [
            item
            for item in material.get("choiceOptions") or []
            if isinstance(item, dict)
            and str(item.get("action") or "") == "continue_plan_expansion"
            and str(item.get("kind") or "") == "simple_direction_more_plans"
            and str(item.get("sourceAssistantTurnId") or "") == str(latest["id"])
        ]
        if not fresh_continuations:
            # The exact unconsumed guide capability is itself proof that this
            # root was expandable before the historic clarification short
            # circuit.  Reissue its identity on the latest carrier even when a
            # legacy summary cannot reconstruct the old `proposalDelta=1`
            # condition.  No earlier turn is searched and no frontier is
            # advanced here.
            fresh = SimpleDirectionExecutionEvidenceService.build_continuation_choice(
                portfolio_id=portfolio_id,
                source_assistant_turn_id=str(latest["id"]),
                planning_root_id=planning_root_id,
                request_fingerprint=request_fingerprint,
                expected_base_version_id=(str(root.get("expectedBaseVersionId") or "") or None),
            )
            material.setdefault("choiceOptions", []).append(fresh)
            fresh_continuations = [fresh]
        if len(fresh_continuations) != 1:
            raise ValueError("guide_recovery_continuation_not_unique")
        fresh_continuations[0].update(
            {
                "guideEvidenceSourceAssistantTurnId": guide_turn_id,
                "guideChoiceExecutionId": str(guide_execution["id"] or ""),
                "guideQueryFingerprint": query_fingerprint,
                "guideEvidenceFingerprint": evidence_fingerprint,
            }
        )
        from src.services.conversation_operation_identity import ConversationOperationIdentity
        material = ConversationOperationIdentity(self.db).reissue_material(session_id, capability_source_turn_id, material)
        recovered_choices = [
            copy.deepcopy(item) for item in material.get("choiceOptions") or [] if isinstance(item, dict)
        ]
        retry_choices = [
            copy.deepcopy(item)
            for item in payload.get("choiceOptions") or []
            if isinstance(item, dict) and str(item.get("action") or "") == "retry_model_planning"
        ]
        recovered_choices.extend(retry_choices)
        payload.update(
            {
                "workflowMode": material.get("workflowMode"),
                "planningSelectionRootTurnId": material.get("planningSelectionRootTurnId"),
                "rootPortfolioId": material.get("rootPortfolioId"),
                "comparisonProjections": material.get("comparisonProjections") or [],
                "comparisonProjectionUpdateMode": "replace",
                "choiceOptions": recovered_choices,
                "comparisonSummary": material.get("comparisonSummary"),
                "frontierStatus": material.get("frontierStatus"),
                "guideContinuationRecovery": {
                    "schemaVersion": "guide-continuation-recovery-v1",
                    "status": "reissued",
                    "targetAssistantTurnId": str(latest["id"]),
                    "businessExecutionStarted": False,
                    "guideEvidenceSourceAssistantTurnId": guide_turn_id,
                    "guideEvidenceFingerprint": evidence_fingerprint,
                    "proposalDelta": 0,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                },
            }
        )
        updated = self.db.execute(
            """UPDATE conversation_turns SET agent_response_json = ?, updated_at = ?
            WHERE id = ? AND session_id = ? AND agent_response_json = ? AND status != 'superseded'""",
            (
                json.dumps(payload, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
                str(latest["id"]),
                session_id,
                raw_payload,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("guide_recovery_capability_carrier_stale")
        return True

    def repair_direction(
        self,
        *,
        session_id: str,
        portfolio_id: str,
        proposal_id: str,
        source_assistant_turn_id: str,
        expected_base_version_id: Optional[str],
        candidate_snapshot: dict[str, Any],
        request_contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Fill only typed gaps of one focused proposal, without itinerary writes."""

        root = self._root_by_id(portfolio_id)
        row = self.db.execute(
            "SELECT snapshot_json, generation_lineage_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        if (
            root is None
            or row is None
            or root.get("workflowMode") != self.WORKFLOW_MODE
            or str(root.get("sessionId") or "") != session_id
            or proposal_id not in set(root.get("visibleProposalIds") or [])
            or str(root.get("status") or "") == "committing"
        ):
            raise ValueError("simple_direction_repair_identity_mismatch")
        current = self._json(row["snapshot_json"])
        candidate = self.prepare_direction_snapshot(
            candidate_snapshot,
            request_contract=request_contract,
        )
        candidate["spatialPreference"] = copy.deepcopy(request_contract.get("spatialPreference") or {})
        current["spatialPreference"] = copy.deepcopy(candidate["spatialPreference"])
        pending = [item for item in current.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        candidate_segments: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        for day in candidate.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                occurrence_id = str(metadata.get("occurrenceId") or "")
                slot_id = str(metadata.get("planningSlotId") or "")
                if occurrence_id and slot_id:
                    candidate_segments[(occurrence_id, slot_id)] = (day_number, copy.deepcopy(segment))

        filled_keys: set[tuple[str, str]] = set()
        for slot in pending:
            key = (str(slot.get("occurrenceId") or ""), str(slot.get("planningSlotId") or ""))
            candidate_match = candidate_segments.get(key)
            if not all(key) or candidate_match is None:
                continue
            day_number, segment = candidate_match
            target_day = next(
                (
                    day
                    for day in current.get("days") or []
                    if isinstance(day, dict) and int(day.get("dayNumber") or 0) == day_number
                ),
                None,
            )
            if target_day is None:
                continue
            target_day.setdefault("segments", []).append(segment)
            target_day["segments"].sort(key=lambda item: str(item.get("startTime") or ""))
            filled_keys.add(key)
        current["portfolioPendingSlots"] = [
            slot
            for slot in pending
            if (str(slot.get("occurrenceId") or ""), str(slot.get("planningSlotId") or "")) not in filled_keys
        ]
        verifier = self._proposal_verifier(current)
        current["portfolioVerifier"] = copy.deepcopy(verifier)
        self.update_server_verified_proposal_material(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            snapshot=current,
            verifier=verifier,
            status="adoption_ready" if verifier.get("confirmationPassed") is True else "blocked",
            evidence={"workflowMode": self.WORKFLOW_MODE, "repairFilledSlotCount": len(filled_keys)},
        )
        self._sync_portfolio_source_assistant_turn(
            portfolio_id=portfolio_id,
            source_assistant_turn_id=source_assistant_turn_id,
        )
        self.db.commit()
        return self.response_material(
            portfolio_id=portfolio_id,
            source_assistant_turn_id=source_assistant_turn_id,
            expected_base_version_id=expected_base_version_id,
            update_mode="replace",
            proposal_ids=None,
            proposal_delta=0,
            reissue_all_capabilities=True,
        )

    @staticmethod
    def _preserve_committed_activation_when_title_pending(
        projection: dict[str, Any],
        *,
        selected_committed: bool,
        activation_verified: bool,
    ) -> dict[str, Any]:
        """Keep presentation-title failure from revoking an adopted draft.

        Title evidence remains a hard pre-adoption gate for a new complete
        candidate.  Once the exact proposal has already crossed the canonical
        single writer, however, a missing/retryable title is presentation debt:
        it must stay visible without invalidating the user's editable version.
        """

        blockers = [str(item) for item in projection.get("blockingReasons") or [] if str(item)]
        if not (selected_committed and activation_verified and blockers == ["proposal_title_generation_pending"]):
            return projection
        material = copy.deepcopy(projection)
        pending_count = sum(
            int(material.get(key) or 0) for key in ("blockingPendingHardSlotCount", "pendingSoftSlotCount")
        )
        complete = pending_count == 0 and str(material.get("routeStatus") or "") in {
            "route_ready",
            "route_not_required",
        }
        soft_warnings = [str(item) for item in material.get("softWarnings") or [] if str(item)]
        soft_warnings.append("proposal_title_generation_pending")
        material.update(
            {
                "status": "complete" if complete else "partial",
                "isPartial": not complete,
                "strictlyVerified": complete,
                "adoptionReady": True,
                "adoptionMode": "complete" if complete else "editable_partial",
                "blockingReasons": [],
                "blockingReasonLabels": [],
                "softWarnings": list(dict.fromkeys(soft_warnings)),
                "currentReadiness": (
                    "route_ready"
                    if str(material.get("routeStatus") or "") in {"route_ready", "route_not_required"}
                    else "route_pending"
                ),
            }
        )
        return material

    def save_active_direction(
        self,
        *,
        session_id: str,
        proposal_id: str,
        planning_root_id: str,
        portfolio_id: str,
        base_version_id: str,
    ) -> dict[str, Any]:
        root = self._root_by_id(portfolio_id)
        if (
            root is None
            or root["workflowMode"] != self.WORKFLOW_MODE
            or str(root["sessionId"]) != session_id
            or str(root["planningRootId"]) != planning_root_id
            or str(root.get("selectedProposalId") or "") != proposal_id
        ):
            raise ValueError("simple_direction_save_identity_mismatch")
        if str(root.get("status") or "") == "committing":
            raise ValueError("simple_direction_save_in_progress")
        if str(root.get("status") or "") != "awaiting_selection":
            raise ValueError("simple_direction_save_identity_mismatch")
        session = self.db.execute(
            "SELECT active_version_id, active_plan_id, city FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None or str(session["active_version_id"] or "") != str(base_version_id or ""):
            raise ValueError("simple_direction_save_base_stale")
        row = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ? AND plan_id = ?",
            (base_version_id, session_id, session["active_plan_id"]),
        ).fetchone()
        proposal = self.db.execute(
            "SELECT snapshot_json, verifier_json, evidence_json, generation_lineage_json, status "
            "FROM agent_plan_proposals "
            "WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        if row is None or proposal is None:
            raise ValueError("simple_direction_save_material_missing")
        active_snapshot = self._json(row["snapshot_json"])
        previous = self._json(proposal["snapshot_json"])
        if not active_snapshot.get("days") or str(active_snapshot.get("id") or "") != str(session["active_plan_id"]):
            raise ValueError("simple_direction_save_active_snapshot_invalid")
        route_contract = active_snapshot.get("routeDecisionContract")
        if self.route_contract_requires_clarification({"routeDecisionContract": route_contract}):
            raise ValueError("simple_direction_save_route_contract_missing")
        active_snapshot["workflowMode"] = self.WORKFLOW_MODE
        active_snapshot["comparisonRole"] = "current_active_draft"
        active_snapshot["originProjectionMode"] = "full_proposal"
        active_snapshot["creativeBrief"] = copy.deepcopy(previous.get("creativeBrief") or {})
        # Version snapshots intentionally keep the editable itinerary envelope
        # smaller than proposal material.  Preserve the last server-validated
        # title proof across a save-back so a harmless timeline edit does not
        # turn the direction into an untitled candidate when the user later
        # switches away and wants to return.  The readiness service still
        # revalidates the proof against current POI evidence, so a material POI
        # change cannot reuse a stale title capability.
        for key in ("portfolioTitleGeneration", "portfolioTitleEvidence"):
            if key in previous and key not in active_snapshot:
                active_snapshot[key] = copy.deepcopy(previous[key])
        # The editable version may have changed schedule, routes, provisional
        # material, or pending-slot metadata since the proposal was confirmed.
        # Never carry the pre-commit verifier forward as if it still described
        # the server-reloaded active snapshot.
        verifier = self._proposal_verifier(
            active_snapshot,
            authoritative_city=str(session["city"] or ""),
            authoritative_adcode=self._snapshot_authoritative_adcode(previous),
            authoritative_snapshot=previous,
        )
        active_snapshot["portfolioVerifier"] = copy.deepcopy(verifier)
        if verifier.get("draftPassed") is not True:
            hard_failures = ",".join(str(item) for item in verifier.get("hardFailures") or [] if str(item))
            raise ValueError("simple_direction_save_activation_failed" + (f":{hard_failures}" if hard_failures else ""))
        unchanged = self._business_fingerprint(previous) == self._business_fingerprint(active_snapshot)
        previous_expected_base = str(root.get("expectedBaseVersionId") or "")
        latest_capability = self._latest_capability_context(
            session_id=session_id,
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
        )
        latest_capability_base = self._capability_expected_base_version(latest_capability)
        needs_fresh_capability = bool(
            not unchanged or latest_capability_base != base_version_id or latest_capability is None
        )
        try:
            self._sync_expected_base_version(
                session_id=session_id,
                planning_root_id=planning_root_id,
                portfolio_id=portfolio_id,
                proposal_id=proposal_id,
                expected_previous_base_version_id=previous_expected_base,
                next_base_version_id=base_version_id,
            )
            saved_evidence = self._json(proposal["evidence_json"])
            save_marker_current = str(saved_evidence.get("savedFromActiveVersionId") or "") == base_version_id
            if not unchanged or not save_marker_current:
                # Even an unchanged first save records that this proposal is a
                # server-reloaded editable snapshot.  Activation can then
                # preserve its complete schedule instead of treating it like a
                # newly generated slot draft.  The store merges this evidence
                # with prior verifier data in the same root CAS transaction.
                self.store.update_proposal_material(
                    portfolio_id=portfolio_id,
                    proposal_id=proposal_id,
                    snapshot=active_snapshot,
                    verifier=verifier,
                    status="committed",
                    evidence={
                        "workflowMode": self.WORKFLOW_MODE,
                        "savedFromActiveVersionId": base_version_id,
                    },
                    commit=False,
                )
                self._sync_proposal_snapshot_fingerprint(
                    portfolio_id=portfolio_id,
                    proposal_id=proposal_id,
                )
            if needs_fresh_capability:
                # Persist the refreshed proposal/root and its opaque capability
                # carrier in one transaction.  A carrier insert failure must
                # not surface as a failed auto-save after the proposal was in
                # fact already mutated.
                material = self._persist_capability_carrier(
                    session_id=session_id,
                    portfolio_id=portfolio_id,
                    proposal_id=proposal_id,
                    base_version_id=base_version_id,
                    previous_capability=latest_capability,
                )
            else:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if not needs_fresh_capability:
            material = self.response_material(
                portfolio_id=portfolio_id,
                source_assistant_turn_id=str(latest_capability["turnId"]),
                expected_base_version_id=base_version_id,
                update_mode="replace",
            )
        projection = next(item for item in material["comparisonProjections"] if item.get("proposalId") == proposal_id)
        return {
            "proposalId": proposal_id,
            "activeVersionId": base_version_id,
            "comparisonProjection": projection,
            "saved": True,
            "unchanged": unchanged,
        }

    @classmethod
    def activation_verifier(
        cls,
        snapshot: dict[str, Any],
        *,
        authoritative_city: Optional[str] = None,
        authoritative_adcode: Optional[str] = None,
        authoritative_snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Return the narrow, evidence-derived Simple activation gate.

        A direction is editable only when every required occurrence is
        materially present, every planned day has a canonical AMap route
        anchor, and the server-compiled route-decision contract is complete.
        Provider exhaustion may explain a read-only partial proposal, but it
        never converts a missing hard occurrence into an adoptable direction.
        """

        verified_anchors_by_day = cls._verified_anchors_by_day(snapshot)
        verified_anchor_count = sum(verified_anchors_by_day.values())
        calendar_day_numbers = {
            int(day.get("dayNumber") or 0)
            for day in snapshot.get("days") or []
            if isinstance(day, dict) and int(day.get("dayNumber") or 0) > 0
        }
        planned_day_numbers = set(calendar_day_numbers)
        required_planning_day_numbers = set(calendar_day_numbers)
        explicit_rest_day_numbers: set[int] = set()
        daily_coverage_contract_invalid = False
        daily_coverage_source_invalid = False
        daily_target_day_partition_invalid = False

        def normalized_day_contract(value: Any) -> set[int]:
            nonlocal daily_coverage_contract_invalid
            if not isinstance(value, list):
                daily_coverage_contract_invalid = True
                return set()
            normalized: set[int] = set()
            for raw_day in value:
                if isinstance(raw_day, bool) or not isinstance(raw_day, int) or raw_day <= 0:
                    daily_coverage_contract_invalid = True
                    return set()
                normalized.add(raw_day)
            if len(normalized) != len(value):
                daily_coverage_contract_invalid = True
                return set()
            return normalized

        simple_open_profile = str(snapshot.get("simpleOpenExecutionProfile") or "") == "simple_open_v1"
        if simple_open_profile:
            required_planning_day_numbers = normalized_day_contract(snapshot.get("requiredPlanningDayNumbers"))
            explicit_rest_day_numbers = normalized_day_contract(snapshot.get("explicitRestDayNumbers"))
            daily_coverage_source_invalid = (
                str(snapshot.get("dailyPlanningCoverageSource") or "")
                != "authoritative_goal_occurrences"
            )
            if (
                required_planning_day_numbers & explicit_rest_day_numbers
                or required_planning_day_numbers | explicit_rest_day_numbers != calendar_day_numbers
            ):
                daily_coverage_contract_invalid = True
            planned_day_numbers = set(required_planning_day_numbers)
        explicit_anchor_targets_applied = False
        anchor_target_contract_invalid = False
        normalized_targets: dict[int, int] = {}
        explicit_anchor_targets = snapshot.get("desiredDensityAnchorTargets")
        if simple_open_profile:
            if isinstance(explicit_anchor_targets, dict) and explicit_anchor_targets:
                for day_number, target in explicit_anchor_targets.items():
                    if (
                        not isinstance(day_number, str)
                        or not day_number.isdigit()
                        or str(int(day_number)) != day_number
                        or isinstance(target, bool)
                        or not isinstance(target, int)
                        or target < 0
                    ):
                        anchor_target_contract_invalid = True
                        normalized_targets = {}
                        break
                    normalized_targets[int(day_number)] = target
            else:
                anchor_target_contract_invalid = True
            if (
                not anchor_target_contract_invalid
                and set(normalized_targets) == calendar_day_numbers
                and any(target > 0 for target in normalized_targets.values())
            ):
                explicit_anchor_targets_applied = True
            else:
                anchor_target_contract_invalid = True
        positive_target_day_numbers = {
            day_number for day_number, target in normalized_targets.items() if target > 0
        }
        zero_target_day_numbers = {
            day_number for day_number, target in normalized_targets.items() if target == 0
        }
        if (
            simple_open_profile
            and explicit_anchor_targets_applied
            and not daily_coverage_contract_invalid
            and (
                positive_target_day_numbers != required_planning_day_numbers
                or zero_target_day_numbers != explicit_rest_day_numbers
            )
        ):
            daily_target_day_partition_invalid = True
        required_day_target_missing = sorted(
            day_number
            for day_number in required_planning_day_numbers
            if normalized_targets.get(day_number, 0) <= 0
        ) if simple_open_profile else []
        planned_days_missing_verified_anchor = sorted(
            day_number for day_number in planned_day_numbers if verified_anchors_by_day.get(day_number, 0) <= 0
        )
        anchor_target_mismatch_days = (
            sorted(
                day_number
                for day_number, target in normalized_targets.items()
                if verified_anchors_by_day.get(day_number, 0) != target
            )
            if explicit_anchor_targets_applied
            else []
        )
        all_planned_days_have_verified_anchor = bool(planned_day_numbers) and not (
            planned_days_missing_verified_anchor or anchor_target_mismatch_days or required_day_target_missing
        )
        uncovered_day_numbers = sorted(
            set(required_day_target_missing)
            | set(planned_days_missing_verified_anchor)
            | {day for day in anchor_target_mismatch_days if day in required_planning_day_numbers}
            | (
                calendar_day_numbers - (required_planning_day_numbers | explicit_rest_day_numbers)
                if daily_coverage_contract_invalid
                else set()
            )
        )
        pending_slots = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        completion_required_pending_slots = ProposalReadinessService.completion_required_pending_slots(snapshot)
        completion_required_pending_slot_count = len(completion_required_pending_slots)
        pending_hard_slots = [
            item for item in pending_slots if str(item.get("requirementLevel") or "required") in {"hard", "required"}
        ]
        pending_occurrence_counts = Counter(
            str(item.get("occurrenceId") or "").strip()
            for item in pending_slots
            if str(item.get("occurrenceId") or "").strip()
        )
        pending_slot_counts = Counter(
            str(item.get("planningSlotId") or item.get("slotId") or "").strip()
            for item in pending_slots
            if str(item.get("planningSlotId") or item.get("slotId") or "").strip()
        )
        pending_duplicate_occurrence_count = sum(count - 1 for count in pending_occurrence_counts.values() if count > 1)
        pending_duplicate_planning_slot_count = sum(count - 1 for count in pending_slot_counts.values() if count > 1)

        def pending_lineage_valid(item: dict[str, Any]) -> bool:
            goal_id = str(item.get("goalId") or "").strip()
            source_goal_id = str(item.get("sourceGoalId") or "").strip()
            occurrence_id = str(item.get("occurrenceId") or "").strip()
            planning_slot_id = str(item.get("planningSlotId") or item.get("slotId") or "").strip()
            pool_id = str(item.get("poolId") or "").strip()
            lineage_authority = str(item.get("lineageAuthority") or "").strip()
            day_number = int(item.get("dayNumber") or 0)
            return bool(
                goal_id
                and goal_id == source_goal_id
                and occurrence_id == f"occ:{source_goal_id}:day:{day_number}"
                and planning_slot_id
                and pool_id
                and day_number > 0
                and lineage_authority
                        in {
                            "goal_occurrence_compiler",
                            "simple_open_request_contract_every_day_meal",
                            "simple_open_request_contract_every_day_park",
                            "simple_open_daily_completion_policy",
                        }
                and pending_occurrence_counts[occurrence_id] == 1
                and pending_slot_counts[planning_slot_id] == 1
            )

        invalid_pending_lineage_ids = {
            id(item)
            for item in pending_slots
            if item.get("simpleDirectionRequirementLineageConflict") is True
            or (
                str(item.get("requirementLevel") or "required") in {"hard", "required"}
                and not pending_lineage_valid(item)
            )
        }
        provider_exhausted_required_slots = [
            item
            for item in pending_hard_slots
            if id(item) not in invalid_pending_lineage_ids
            if item.get("simpleDirectionProviderExhausted") is True
            and item.get("simpleDirectionRequirementLineageConflict") is not True
            and str(item.get("groundingStatus") or "") == "unresolved"
            and str(item.get("reasonCode") or "") == "provider_candidates_exhausted_or_semantically_rejected"
            and not isinstance(item.get("poi"), dict)
        ]
        pending_hard_slot_count = len(pending_hard_slots)
        provider_exhausted_required_slot_count = (
            len(provider_exhausted_required_slots) if all_planned_days_have_verified_anchor else 0
        )
        blocking_pending_hard_slot_count = pending_hard_slot_count
        pending_slot_lineage_conflict_count = len(invalid_pending_lineage_ids)
        semantic_failure_count = 0
        invalid_materialized_identity_count = 0
        duplicate_materialized_identity_count = 0
        materialized_city_mismatch_count = 0
        materialized_pending_lineage_conflict_count = 0
        materialized_lineage_failure_count = 0
        provisional_segment_count = 0
        schedule_rejected_segment_count = 0
        schedule_invalid_segment_count = 0
        schedule_overlap_count = 0
        schedule_duration_mismatch_count = 0
        schedule_derived_duration_count = 0
        schedule_semantic_failures: list[dict[str, Any]] = []
        snapshot_city = cls._normalized_city(snapshot.get("city"))
        expected_city = cls._normalized_city(
            authoritative_city if authoritative_city is not None else snapshot.get("city")
        )
        snapshot_adcode = str(snapshot.get("adcode") or snapshot.get("cityAdcode") or "").strip()
        authoritative_adcode_supplied = authoritative_adcode is not None
        expected_adcode = str(authoritative_adcode if authoritative_adcode is not None else snapshot_adcode).strip()
        itinerary_city_mismatch_count = int(
            bool(
                (expected_city and snapshot_city != expected_city)
                or (expected_adcode and snapshot_adcode != expected_adcode)
                or (authoritative_adcode_supplied and not expected_adcode and bool(snapshot_adcode))
            )
        )
        pending_occurrence_ids = {
            str(item.get("occurrenceId") or "").strip()
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and str(item.get("occurrenceId") or "").strip()
        }
        pending_slot_ids = {
            str(item.get("planningSlotId") or item.get("slotId") or "").strip()
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and str(item.get("planningSlotId") or item.get("slotId") or "").strip()
        }
        materialized_identity_groups: list[set[str]] = []
        materialized_occurrence_ids: set[str] = set()
        materialized_slot_ids: set[str] = set()
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            day_intervals: list[tuple[int, int]] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                start_minutes = cls._strict_clock_minutes(segment.get("startTime"))
                end_minutes = cls._strict_clock_minutes(segment.get("endTime"))
                raw_duration = segment.get("durationMinutes")
                interval_valid = start_minutes is not None and end_minutes is not None and end_minutes > start_minutes
                duration_supplied = raw_duration is not None
                duration_minutes = (
                    raw_duration if isinstance(raw_duration, int) and not isinstance(raw_duration, bool) else None
                )
                if not duration_supplied and interval_valid:
                    duration_minutes = end_minutes - start_minutes
                    schedule_derived_duration_count += 1
                if not interval_valid or duration_minutes is None or duration_minutes <= 0:
                    schedule_invalid_segment_count += 1
                else:
                    if duration_minutes != end_minutes - start_minutes:
                        schedule_duration_mismatch_count += 1
                        schedule_invalid_segment_count += 1
                if interval_valid:
                    day_intervals.append((start_minutes, end_minutes))
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                grounding_status = str(metadata.get("groundingStatus") or "")
                if grounding_status == "provisional":
                    provisional_segment_count += 1
                schedule_decision = (
                    metadata.get("scheduleDecision") if isinstance(metadata.get("scheduleDecision"), dict) else {}
                )
                if (
                    str(schedule_decision.get("scheduleConfidence") or "") == "rejected"
                    or schedule_decision.get("constraintPassed") is False
                    and str(schedule_decision.get("failureReason") or "").strip()
                ):
                    schedule_rejected_segment_count += 1
                intent_type = str(metadata.get("intentType") or segment.get("kind") or "")
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                semantic_clock_failure = SimpleOpenDynamicScheduleService.semantic_evening_clock_failure(
                    trip_date=day.get("date"),
                    latitude=poi.get("latitude"),
                    longitude=poi.get("longitude"),
                    start_time=segment.get("startTime"),
                    schedule_preference=metadata.get("schedulePreference"),
                    schedule_constraints=metadata.get("scheduleConstraints"),
                )
                if semantic_clock_failure:
                    schedule_semantic_failures.append(
                        {
                            "segmentId": str(segment.get("id") or ""),
                            "dayNumber": day_number,
                            "reasonCode": semantic_clock_failure,
                        }
                    )
                # Upstream candidate admission prevents new mismatches, while
                # activation must also defend against already-persisted or
                # manually migrated provisional material.  An unresolved slot
                # without a POI is represented separately and is not evaluated
                # as an entity here.
                if not poi:
                    continue
                if not ProposalReadinessService._real_amap_poi(poi) or PoiPhysicalIdentityService.invalid_parent_id(
                    poi
                ):
                    invalid_materialized_identity_count += 1
                    continue
                aliases = cls._physical_poi_aliases(poi)
                overlapping = [index for index, group in enumerate(materialized_identity_groups) if group & aliases]
                if overlapping:
                    duplicate_materialized_identity_count += 1
                merged_aliases = set(aliases)
                for index in reversed(overlapping):
                    merged_aliases.update(materialized_identity_groups.pop(index))
                if merged_aliases:
                    materialized_identity_groups.append(merged_aliases)
                poi_city = cls._normalized_city(poi.get("city"))
                poi_adcode = str(poi.get("adcode") or poi.get("cityAdcode") or "").strip()
                if (
                    (expected_city and poi_city != expected_city)
                    or (expected_adcode and poi_adcode != expected_adcode)
                    or (authoritative_adcode_supplied and not expected_adcode and bool(poi_adcode))
                ):
                    materialized_city_mismatch_count += 1
                schedule_constraints = (
                    metadata.get("scheduleConstraints")
                    if isinstance(metadata.get("scheduleConstraints"), dict)
                    else {}
                )
                qualification_binding = (
                    schedule_constraints.get("qualificationBinding")
                    if isinstance(schedule_constraints.get("qualificationBinding"), dict)
                    else metadata.get("qualificationBinding")
                    if isinstance(metadata.get("qualificationBinding"), dict)
                    else None
                )
                exact_entity = str(metadata.get("exactEntity") or "").strip() or (
                    str(qualification_binding.get("canonicalName") or "").strip()
                    if isinstance(qualification_binding, dict)
                    else ""
                )
                semantic_reasons = SimpleOpenItineraryExecutor._candidate_semantic_rejection_reasons(
                    poi,
                    city=str(authoritative_city if authoritative_city is not None else snapshot.get("city") or ""),
                    intent_type=intent_type,
                    raw_need=str(metadata.get("rawNeed") or ""),
                    exact_entity=exact_entity or None,
                    optional_experience_family=str(metadata.get("optionalExperienceFamily") or ""),
                    qualification_binding=qualification_binding,
                    experience_policy=(
                        {"localFoodRequired": schedule_constraints.get("localFoodRequired") is True}
                        if intent_type == "meal"
                        else None
                    ),
                    enforce_intent_semantic_policy=bool(
                        intent_type == "campus_visit"
                        and (isinstance(qualification_binding, dict) or "985" in str(metadata.get("rawNeed") or ""))
                    ),
                )
                if semantic_reasons:
                    semantic_failure_count += 1
                requirement_level = str(metadata.get("requirementLevel") or "")
                lineage_required = bool(
                    metadata.get("required") is True
                    or requirement_level in {"hard", "required"}
                    or any(
                        str(metadata.get(key) or "").strip()
                        for key in (
                            "goalId",
                            "sourceGoalId",
                            "occurrenceId",
                            "lineageAuthority",
                        )
                    )
                )
                occurrence_id = str(metadata.get("occurrenceId") or "").strip()
                planning_slot_id = str(metadata.get("planningSlotId") or metadata.get("slotId") or "").strip()
                if lineage_required:
                    lineage_day_number = int(metadata.get("dayNumber") or 0)
                    goal_id = str(metadata.get("goalId") or "").strip()
                    source_goal_id = str(metadata.get("sourceGoalId") or "").strip()
                    lineage_authority = str(metadata.get("lineageAuthority") or "").strip()
                    lineage_complete = all(
                        str(metadata.get(key) or "").strip()
                        for key in (
                            "goalId",
                            "sourceGoalId",
                            "occurrenceId",
                            "planningSlotId",
                            "poolId",
                            "lineageAuthority",
                        )
                    )
                    if (
                        not lineage_complete
                        or lineage_day_number != day_number
                        or goal_id != source_goal_id
                        or occurrence_id != f"occ:{source_goal_id}:day:{day_number}"
                        or lineage_authority
                        not in {
                            "goal_occurrence_compiler",
                            "simple_open_request_contract_every_day_meal",
                            "simple_open_request_contract_every_day_park",
                            "simple_open_daily_completion_policy",
                        }
                        or occurrence_id in materialized_occurrence_ids
                        or planning_slot_id in materialized_slot_ids
                    ):
                        materialized_lineage_failure_count += 1
                if occurrence_id:
                    materialized_occurrence_ids.add(occurrence_id)
                if planning_slot_id:
                    materialized_slot_ids.add(planning_slot_id)
                if (occurrence_id and occurrence_id in pending_occurrence_ids) or (
                    planning_slot_id and planning_slot_id in pending_slot_ids
                ):
                    materialized_pending_lineage_conflict_count += 1
            previous_end: Optional[int] = None
            for start_minutes, end_minutes in sorted(day_intervals):
                if previous_end is not None and start_minutes < previous_end:
                    schedule_overlap_count += 1
                previous_end = max(previous_end or end_minutes, end_minutes)
        authoritative_lineage_mismatch_count = 0
        if authoritative_snapshot is not None:
            authoritative_records = cls._direction_lineage_records(authoritative_snapshot)
            active_records = cls._direction_lineage_records(snapshot)
            authoritative_required_signatures = Counter(
                cls._lineage_signature(item) for item in authoritative_records if item["required"] is True
            )
            active_signatures = Counter(cls._lineage_signature(item) for item in active_records)
            active_required_signatures = Counter(
                cls._lineage_signature(item) for item in active_records if item["required"] is True
            )
            for signature, count in active_required_signatures.items():
                if count > authoritative_required_signatures.get(signature, 0):
                    authoritative_lineage_mismatch_count += count - authoritative_required_signatures.get(signature, 0)
            for item in authoritative_records:
                if item["required"] is not True:
                    continue
                signature = cls._lineage_signature(item)
                if active_required_signatures.get(signature, 0) != 1:
                    authoritative_lineage_mismatch_count += 1
        route_contract_ready = not cls.route_contract_requires_clarification(
            {"routeDecisionContract": snapshot.get("routeDecisionContract")}
        )
        hard_failures: list[str] = []
        if verified_anchor_count <= 0:
            hard_failures.append("simple_direction_no_verified_amap_anchor")
        if planned_days_missing_verified_anchor:
            hard_failures.append("simple_direction_planned_day_missing_verified_amap_anchor")
            hard_failures.append("simple_direction_required_day_empty")
        if required_day_target_missing:
            hard_failures.append("simple_direction_required_day_target_missing")
        if daily_coverage_contract_invalid:
            hard_failures.append("simple_direction_daily_planning_coverage_contract_invalid")
        if daily_coverage_source_invalid:
            hard_failures.append("simple_direction_daily_planning_coverage_source_invalid")
        if anchor_target_contract_invalid:
            hard_failures.append("simple_direction_daily_anchor_target_contract_invalid")
        if daily_target_day_partition_invalid:
            hard_failures.append("simple_direction_daily_anchor_target_day_partition_invalid")
        if anchor_target_mismatch_days:
            hard_failures.append("simple_direction_daily_anchor_target_mismatch")
        if blocking_pending_hard_slot_count:
            hard_failures.append("simple_direction_pending_hard_slot")
        if completion_required_pending_slot_count:
            hard_failures.append("simple_direction_pending_completion_required_slot")
        if semantic_failure_count:
            hard_failures.append("simple_direction_verified_semantic_mismatch")
        if invalid_materialized_identity_count:
            hard_failures.append("simple_direction_materialized_map_identity_invalid")
        if duplicate_materialized_identity_count:
            hard_failures.append("simple_direction_materialized_poi_duplicate")
        if itinerary_city_mismatch_count or materialized_city_mismatch_count:
            hard_failures.append("simple_direction_materialized_poi_city_mismatch")
        if materialized_pending_lineage_conflict_count:
            hard_failures.append("simple_direction_materialized_pending_lineage_conflict")
        if materialized_lineage_failure_count:
            hard_failures.append("simple_direction_materialized_lineage_invalid")
        if schedule_rejected_segment_count:
            hard_failures.append("simple_direction_dynamic_schedule_constraint_failed")
        hard_failures.extend(
            f"simple_direction_{reason}"
            for reason in sorted({item["reasonCode"] for item in schedule_semantic_failures})
        )
        if schedule_invalid_segment_count:
            hard_failures.append("simple_direction_schedule_interval_invalid")
        if schedule_overlap_count:
            hard_failures.append("simple_direction_schedule_overlap")
        if authoritative_lineage_mismatch_count:
            hard_failures.append("simple_direction_authoritative_lineage_mismatch")
        if pending_slot_lineage_conflict_count:
            hard_failures.append("simple_direction_pending_slot_lineage_conflict")
        if not route_contract_ready:
            hard_failures.append("simple_direction_route_decision_contract_not_ready")
        passed = not hard_failures
        return {
            "passed": passed,
            "draftPassed": passed,
            "simpleDirectionActivationVerified": passed,
            "scheduleSemanticFailureCount": len(schedule_semantic_failures),
            "scheduleSemanticFailures": schedule_semantic_failures,
            "verifiedAmapRouteAnchorCount": verified_anchor_count,
            "pendingHardSlotCount": pending_hard_slot_count,
            "blockingPendingHardSlotCount": blocking_pending_hard_slot_count,
            "completionRequiredPendingSlotCount": completion_required_pending_slot_count,
            "providerExhaustedRequiredSlotCount": provider_exhausted_required_slot_count,
            "pendingSlotLineageConflictCount": pending_slot_lineage_conflict_count,
            "pendingDuplicateOccurrenceCount": pending_duplicate_occurrence_count,
            "pendingDuplicatePlanningSlotCount": pending_duplicate_planning_slot_count,
            "calendarDayNumbers": sorted(calendar_day_numbers),
            "plannedDayNumbers": sorted(planned_day_numbers),
            "requiredPlanningDayNumbers": sorted(required_planning_day_numbers),
            "explicitRestDayNumbers": sorted(explicit_rest_day_numbers),
            "uncoveredDayNumbers": uncovered_day_numbers,
            "dailyPlanningCoverageContractInvalid": daily_coverage_contract_invalid,
            "dailyPlanningCoverageSourceInvalid": daily_coverage_source_invalid,
            "dailyTargetDayPartitionInvalid": daily_target_day_partition_invalid,
            "positiveTargetDayNumbers": sorted(positive_target_day_numbers),
            "zeroTargetDayNumbers": sorted(zero_target_day_numbers),
            "requiredDayTargetMissing": required_day_target_missing,
            "explicitAnchorTargetsApplied": explicit_anchor_targets_applied,
            "dayAnchorTargets": (
                {str(day_number): normalized_targets[day_number] for day_number in sorted(normalized_targets)}
                if explicit_anchor_targets_applied
                else {}
            ),
            "dayAnchorActuals": {
                str(day_number): int(verified_anchors_by_day.get(day_number, 0))
                for day_number in sorted(calendar_day_numbers)
            },
            "anchorTargetContractInvalid": anchor_target_contract_invalid,
            "anchorTargetMismatchDays": anchor_target_mismatch_days,
            "plannedDaysMissingVerifiedAnchor": planned_days_missing_verified_anchor,
            "allPlannedDaysHaveVerifiedAnchor": all_planned_days_have_verified_anchor,
            "semanticFailureCount": semantic_failure_count,
            "invalidMaterializedIdentityCount": invalid_materialized_identity_count,
            "duplicateMaterializedIdentityCount": duplicate_materialized_identity_count,
            "materializedCityMismatchCount": materialized_city_mismatch_count,
            "itineraryCityMismatchCount": itinerary_city_mismatch_count,
            "materializedPendingLineageConflictCount": materialized_pending_lineage_conflict_count,
            "materializedLineageFailureCount": materialized_lineage_failure_count,
            "authoritativeLineageMismatchCount": authoritative_lineage_mismatch_count,
            "provisionalSegmentCount": provisional_segment_count,
            "scheduleRejectedSegmentCount": schedule_rejected_segment_count,
            "scheduleInvalidSegmentCount": schedule_invalid_segment_count,
            "scheduleDurationMismatchCount": schedule_duration_mismatch_count,
            "scheduleDerivedDurationCount": schedule_derived_duration_count,
            "scheduleOverlapCount": schedule_overlap_count,
            "routeDecisionContractReady": route_contract_ready,
            "hardFailures": hard_failures,
        }

    @staticmethod
    def _normalized_city(value: Any) -> str:
        normalized = re.sub(r"\s+", "", str(value or "")).casefold()
        for suffix in ("特别行政区", "自治区", "市"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return normalized

    @staticmethod
    def _strict_clock_minutes(value: Any) -> Optional[int]:
        text = str(value or "").strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text) is None:
            return None
        hour, minute = text.split(":", 1)
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _snapshot_authoritative_adcode(snapshot: dict[str, Any]) -> str:
        top_level = str(snapshot.get("adcode") or snapshot.get("cityAdcode") or "").strip()
        if top_level:
            return top_level
        poi_adcodes = {
            str((segment.get("poi") or {}).get("adcode") or "").strip()
            for day in snapshot.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict) and isinstance(segment.get("poi"), dict)
            if str((segment.get("poi") or {}).get("adcode") or "").strip()
        }
        if len(poi_adcodes) > 1:
            raise ValueError("simple_direction_save_activation_failed:simple_direction_materialized_poi_city_mismatch")
        return next(iter(poi_adcodes), "")

    @classmethod
    def _direction_lineage_records(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def append_record(payload: dict[str, Any], *, day_number: int) -> None:
            requirement_level = str(payload.get("requirementLevel") or "")
            required = bool(payload.get("required") is True or requirement_level in {"hard", "required"})
            if not required and not any(
                str(payload.get(key) or "").strip()
                for key in (
                    "goalId",
                    "sourceGoalId",
                    "occurrenceId",
                    "planningSlotId",
                    "slotId",
                    "lineageAuthority",
                )
            ):
                return
            records.append(
                {
                    "goalId": str(payload.get("goalId") or "").strip(),
                    "sourceGoalId": str(payload.get("sourceGoalId") or "").strip(),
                    "occurrenceId": str(payload.get("occurrenceId") or "").strip(),
                    "planningSlotId": str(payload.get("planningSlotId") or payload.get("slotId") or "").strip(),
                    "poolId": str(payload.get("poolId") or "").strip(),
                    "dayNumber": int(payload.get("dayNumber") or day_number or 0),
                    "lineageAuthority": str(payload.get("lineageAuthority") or "").strip(),
                    "required": required,
                }
            )

        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata")
                if isinstance(metadata, dict):
                    append_record(metadata, day_number=day_number)
        for item in snapshot.get("portfolioPendingSlots") or []:
            if isinstance(item, dict):
                append_record(item, day_number=int(item.get("dayNumber") or 0))
        return records

    @staticmethod
    def _lineage_signature(item: dict[str, Any]) -> tuple[str, str, str, str, str, int, str]:
        return (
            str(item.get("goalId") or ""),
            str(item.get("sourceGoalId") or ""),
            str(item.get("occurrenceId") or ""),
            str(item.get("planningSlotId") or ""),
            str(item.get("poolId") or ""),
            int(item.get("dayNumber") or 0),
            str(item.get("lineageAuthority") or ""),
        )

    @classmethod
    def _guide_evidence_usage(
        cls,
        snapshot: dict[str, Any],
        *,
        route_verified: bool,
        hard_constraints_passed: bool,
        prior_guide_physical_aliases: frozenset[str] | None = None,
        sanitize_reused_guide_annotations: bool = False,
    ) -> dict[str, Any]:
        requirement = (
            snapshot.get("guideContinuationRequirement")
            if isinstance(snapshot.get("guideContinuationRequirement"), dict)
            else {}
        )
        if str(requirement.get("schemaVersion") or "") != "guide-continuation-requirement-v1":
            return {}
        evidence_fingerprint = str(requirement.get("evidenceFingerprint") or "")
        required_minimum = max(1, int(requirement.get("minimumNovelGroundedPlaceCount") or 1))
        requirement_fingerprint = str(requirement.get("requirementFingerprint") or "")
        legacy_identity_policy = "identityPolicy" not in requirement
        signed_hints = {
            (
                str(item.get("mentionText") or "").strip().casefold(),
                SimpleOpenItineraryExecutor._guide_hint_intent(str(item.get("intentType") or "")),
                tuple(str(value) for value in item.get("sourceRefIds") or [] if str(value)),
                tuple(str(value) for value in item.get("sourceFingerprints") or [] if str(value)),
                str(item.get("guideEvidenceFingerprint") or ""),
            )
            for item in requirement.get("placeHints") or []
            if isinstance(item, dict)
            and str(item.get("schemaVersion") or "") == "guide-place-hint-v1"
            and str(item.get("verificationStatus") or "") == "unresolved_amap_grounding"
        }
        requirement_valid = bool(
            evidence_fingerprint
            and requirement_fingerprint
            and requirement_fingerprint
            == GuideContinuationRequirementService.requirement_fingerprint(requirement)
            and signed_hints
            and GuideSourceRefreshService.validate_documents(requirement)
            and (legacy_identity_policy or requirement.get("identityPolicy") == GuideContinuationRequirementService.IDENTITY_POLICY)
        )
        used_places: list[dict[str, Any]] = []
        rejection_counts: Counter[str] = Counter()
        attempt_details: list[dict[str, Any]] = []
        rejection_reasons = (
            "no_amap_match", "no_match_in_search_scope", "ambiguous_amap_match",
            "hard_constraint_mismatch", "already_used", "schedule_unfit", "route_unavailable",
            "query_not_executed", "provider_failure", "candidate_processing_failure", "guide_evidence_lineage_invalid",
        )
        grounded_but_route_blocked = 0
        grounded_but_schedule_blocked = 0
        seen_physical: set[str] = set()
        prior_physical = prior_guide_physical_aliases or frozenset()
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                constraints = metadata.get("scheduleConstraints")
                constraints = constraints if isinstance(constraints, dict) else {}
                attempt = constraints.get("guideEvidenceAttempt")
                valid_attempt_detail = None
                if isinstance(attempt, dict):
                    attempt_valid = bool(
                        requirement_valid
                        and str(attempt.get("schemaVersion") or "") == "guide-place-attempt-v1"
                        and int(attempt.get("dayNumber") or 0) == day_number
                        and str(attempt.get("planningSlotId") or "")
                        == str(metadata.get("planningSlotId") or segment.get("planningSlotId") or "")
                        and (
                            str(attempt.get("mentionText") or "").strip().casefold(),
                            SimpleOpenItineraryExecutor._guide_hint_intent(str(attempt.get("intentType") or "")),
                            tuple(str(item) for item in attempt.get("sourceRefIds") or [] if str(item)),
                            tuple(str(item) for item in attempt.get("sourceFingerprints") or [] if str(item)),
                            str(attempt.get("guideEvidenceFingerprint") or ""),
                        ) in signed_hints
                    )
                    if not attempt_valid:
                        rejection_counts["guide_evidence_lineage_invalid"] += 1
                    else:
                        valid_attempt_detail = {
                            key: copy.deepcopy(attempt[key])
                            for key in (
                                "mentionText", "intentType", "sourceRefIds", "dayNumber", "planningSlotId",
                                "status", "reasonCode", "providerCalled", "providerOutcome", "queryText",
                                "searchScope", "nearbyRadiusMeters", "providerResultCount", "guideMatchCount",
                                "providerErrorType", "queryNotExecutedReason", "candidateProcessingErrorType", "cacheHit",
                            ) if key in attempt
                        }
                        attempt_details.append(valid_attempt_detail)
                        if str(attempt.get("status") or "") == "rejected":
                            reason = str(attempt.get("reasonCode") or "no_amap_match")
                            if reason in rejection_reasons:
                                rejection_counts[reason] += 1
                evidence = constraints.get("guideEvidence")
                if not isinstance(evidence, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                amap_poi_id = str(poi.get("amapId") or "").strip()
                aliases = cls._guide_physical_poi_aliases(poi)
                physical_identity_key = next(
                    (
                        alias
                        for prefix in ("physical:", "group:", "amap:")
                        for alias in sorted(aliases)
                        if alias.startswith(prefix)
                    ),
                    "",
                )
                schedule_decision = metadata.get("scheduleDecision")
                schedule_decision = schedule_decision if isinstance(schedule_decision, dict) else {}
                schedule_fit = str(schedule_decision.get("scheduleConfidence") or "verified") not in {
                    "provisional",
                    "unresolved",
                }
                evidence_valid = bool(
                    requirement_valid
                    and str(evidence.get("schemaVersion") or "") == "guide-place-evidence-v1"
                    and str(evidence.get("guideEvidenceFingerprint") or "") == evidence_fingerprint
                    and str(evidence.get("verificationStatus") or "") == "verified_amap_grounding"
                    and amap_poi_id
                    and str(evidence.get("amapPoiId") or "").strip().upper() == amap_poi_id.upper()
                    and aliases
                    and physical_identity_key
                    and metadata.get("routeAnchor") is True
                    and int(evidence.get("dayNumber") or 0) == day_number
                    and str(evidence.get("planningSlotId") or "")
                    == str(metadata.get("planningSlotId") or segment.get("planningSlotId") or "")
                    and [str(item) for item in evidence.get("sourceRefIds") or [] if str(item)]
                    and (
                        str(evidence.get("mentionText") or "").strip().casefold(),
                        SimpleOpenItineraryExecutor._guide_hint_intent(str(evidence.get("intentType") or "")),
                        tuple(str(item) for item in evidence.get("sourceRefIds") or [] if str(item)),
                        tuple(str(item) for item in evidence.get("sourceFingerprints") or [] if str(item)),
                        str(evidence.get("guideEvidenceFingerprint") or ""),
                    )
                    in signed_hints
                    and (
                        (legacy_identity_policy and "identityMatch" not in evidence and SimpleOpenItineraryExecutor._guide_hint_matches_name(
                            evidence, str(poi.get("name") or ""),
                        ))
                        or GuidePoiIdentityService.validate_evidence(
                            hint=evidence, candidate=poi, evidence=evidence.get("identityMatch"),
                        )
                    )
                )
                if not evidence_valid:
                    rejection_counts["guide_evidence_lineage_invalid"] += 1
                    continue
                if aliases & (seen_physical | prior_physical):
                    already_reported = bool(
                        valid_attempt_detail
                        and valid_attempt_detail.get("status") == "rejected"
                        and valid_attempt_detail.get("reasonCode") == "already_used"
                    )
                    if not already_reported:
                        rejection_counts["already_used"] += 1
                    if valid_attempt_detail is not None:
                        valid_attempt_detail.update(status="rejected", reasonCode="already_used")
                    if sanitize_reused_guide_annotations:
                        # Only offer's private prepared copy is normalized.
                        # Otherwise a mixed old/new guide proposal could pass
                        # via the new place, then resurrect the old evidence
                        # when readback correctly avoids comparing with self.
                        constraints.pop("guideEvidence", None)
                        if valid_attempt_detail is not None:
                            attempt.update(status="rejected", reasonCode="already_used")
                        elif not isinstance(attempt, dict):
                            constraints["guideEvidenceAttempt"] = {
                                **{
                                    key: copy.deepcopy(evidence[key])
                                    for key in (
                                        "mentionText", "intentType", "sourceRefIds", "sourceFingerprints",
                                        "guideEvidenceFingerprint", "dayNumber", "planningSlotId",
                                    )
                                    if key in evidence
                                },
                                "schemaVersion": "guide-place-attempt-v1",
                                "status": "rejected",
                                "reasonCode": "already_used",
                            }
                    continue
                if not hard_constraints_passed:
                    rejection_counts["hard_constraint_mismatch"] += 1
                    continue
                if not schedule_fit:
                    rejection_counts["schedule_unfit"] += 1
                    grounded_but_schedule_blocked += 1
                    continue
                if not route_verified:
                    grounded_but_route_blocked += 1
                    continue
                seen_physical.update(aliases)
                used_places.append(
                    {
                        "mentionText": str(evidence.get("mentionText") or ""),
                        "intentType": str(evidence.get("intentType") or ""),
                        "sourceRefIds": [
                            str(item) for item in evidence.get("sourceRefIds") or [] if str(item)
                        ],
                        "amapPoiId": amap_poi_id,
                        "physicalIdentityKey": physical_identity_key,
                        "dayNumber": day_number,
                        "planningSlotId": str(evidence.get("planningSlotId") or ""),
                        "routeVerified": True,
                        **({"identityMatch": copy.deepcopy(evidence["identityMatch"])}
                           if isinstance(evidence.get("identityMatch"), dict) else {}),
                    }
                )
        if grounded_but_route_blocked:
            rejection_counts["route_unavailable"] += grounded_but_route_blocked
        if requirement_valid and isinstance(requirement.get("sourceDocumentEvidence"), dict):
            for used in used_places:
                hint = next((item for item in requirement["placeHints"]
                             if item.get("mentionText") == used["mentionText"]
                             and item.get("sourceRefIds") == used["sourceRefIds"]), None)
                if hint is not None:
                    used["sourceDocumentFingerprints"] = copy.deepcopy(hint.get("sourceDocumentFingerprints") or [])
                    used["sourceExcerpt"] = str(hint.get("sourceExcerpt") or "")
        if grounded_but_schedule_blocked:
            rejection_counts["schedule_unfit"] += 0
        satisfied = len(used_places) >= required_minimum
        source_documents = (requirement.get("sourceDocumentEvidence") or {}).get("documents") or []
        return {
            "schemaVersion": "guide-evidence-usage-v1",
            "status": "satisfied" if satisfied else "unsatisfied",
            "evidenceFingerprint": evidence_fingerprint,
            "requirementFingerprint": requirement_fingerprint,
            "requiredMinimum": required_minimum,
            **({"sourceReadSummary": {
                "attemptedCount": sum(d.get("readAttempted") is True for d in source_documents),
                "readableCount": sum(d.get("status") == "succeeded" for d in source_documents),
                "sourceDocumentEvidenceFingerprint": requirement["sourceDocumentEvidence"]["fingerprint"],
            }} if source_documents and requirement_valid else {}),
            "usedPlaces": used_places,
            "attemptDetails": attempt_details,
            "attemptedPlaceCount": len({
                (item["intentType"], item["mentionText"])
                for item in attempt_details if item.get("providerCalled") is True
            }),
            "providerQueryCount": sum(
                item.get("providerCalled") is True and item.get("cacheHit") is not True
                for item in attempt_details
            ),
            "cacheHitCount": sum(item.get("cacheHit") is True for item in attempt_details),
            "rejectionCounts": {
                reason: int(rejection_counts.get(reason) or 0)
                for reason in rejection_reasons
                if int(rejection_counts.get(reason) or 0) > 0
            },
        }

    @classmethod
    def _proposal_verifier(
        cls,
        snapshot: dict[str, Any],
        *,
        authoritative_city: Optional[str] = None,
        authoritative_adcode: Optional[str] = None,
        authoritative_snapshot: Optional[dict[str, Any]] = None,
        prior_guide_physical_aliases: frozenset[str] | None = None,
        sanitize_reused_guide_annotations: bool = False,
    ) -> dict[str, Any]:
        """Derive confirmation readiness from frozen canonical POI and route facts."""

        activation = cls.activation_verifier(
            snapshot,
            authoritative_city=authoritative_city,
            authoritative_adcode=authoritative_adcode,
            authoritative_snapshot=authoritative_snapshot,
        )
        readiness = ProposalReadinessService.compute(
            snapshot,
            verifier=activation,
            soft_slot_draft_adoption_enabled=True,
            route_failures_non_blocking=False,
        )
        route_status = str(readiness.get("routeStatus") or "route_pending")
        route_ready = route_status in {"route_ready", "route_not_required"}
        provisional_segment_count = int(activation.get("provisionalSegmentCount") or 0)
        schedule_provisional_count = sum(
            1
            for day in snapshot.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
            and isinstance(segment.get("semanticMetadata"), dict)
            and isinstance(segment["semanticMetadata"].get("scheduleDecision"), dict)
            and str(segment["semanticMetadata"]["scheduleDecision"].get("scheduleConfidence") or "")
            in {"provisional", "unresolved"}
        )
        route_audit = (
            snapshot.get("simpleOpenRouteAssignment")
            if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict)
            else {}
        )
        route_contract = (
            snapshot.get("routeDecisionContract") if isinstance(snapshot.get("routeDecisionContract"), dict) else {}
        )
        audit_fingerprint_matches = bool(
            str(route_audit.get("routeContractFingerprint") or "")
            and str(route_audit.get("routeContractFingerprint") or "") == str(route_contract.get("fingerprint") or "")
        )
        adjacent_verified = str(route_audit.get("adjacentLegCompliance") or "") == "verified"
        topology_verified = str(route_audit.get("topologyCompliance") or "") == "verified"
        provider_baseline_not_claimed = (
            route_audit.get("providerBaselineCompared") is False
            and str(route_audit.get("detourCompliance") or "") == "not_evaluated"
        )
        route_coverage_complete = route_audit.get("routeCoverageComplete") is True
        compact_route_contract = isinstance(route_contract.get("adjacentLegConstraint"), dict)
        frozen_pair_evidence = cls._verify_compact_route_pair_evidence(
            snapshot,
            route_audit=route_audit,
            route_contract=route_contract,
        )
        daily_overlap_evidence = cls._verify_daily_route_overlap_evidence(
            snapshot,
            route_audit=route_audit,
        )
        daily_overlap_policy = str(route_audit.get("dailyRouteOverlapPolicy") or "observe").strip().casefold()
        overlap_evidence_required = daily_overlap_policy == "rank"
        compact_route_verified = bool(
            audit_fingerprint_matches
            and route_coverage_complete
            and adjacent_verified
            and topology_verified
            and provider_baseline_not_claimed
            and frozen_pair_evidence.get("passed") is True
            and (not overlap_evidence_required or daily_overlap_evidence.get("passed") is True)
        )
        spatial_evidence = cls._verify_spatial_preference_evidence(snapshot)
        if compact_route_contract:
            route_ready = compact_route_verified
            route_status = "route_ready" if compact_route_verified else "route_pending"
        hard_failures = [str(item) for item in activation.get("hardFailures") or [] if str(item)]
        meal_quality = MealExperiencePortfolioPolicy.snapshot_quality(snapshot)
        meal_required = int(meal_quality.get("mealCount") or 0) > 0
        meal_quality_passed = bool(not meal_required or meal_quality.get("mealQualityPassed") is True)
        meal_diversity_passed = bool(not meal_required or meal_quality.get("mealDiversityPassed") is True)
        if meal_required and (not meal_quality_passed or not meal_diversity_passed):
            hard_failures.extend(
                str(item) for item in meal_quality.get("mealUnresolvedReasons") or [] if str(item)
            )
        if not route_ready:
            hard_failures.append("route_evidence_incomplete")
            if meal_required:
                hard_failures.append("simple_direction_meal_route_incomplete")
        if compact_route_contract and not compact_route_verified:
            route_failure_reason = str(route_audit.get("failureReason") or "")
            if route_failure_reason:
                hard_failures.append(route_failure_reason)
            if overlap_evidence_required and daily_overlap_evidence.get("passed") is not True:
                hard_failures.append(
                    str(daily_overlap_evidence.get("reason") or "daily_route_overlap_evidence_invalid")
                )
        guide_evidence_usage = cls._guide_evidence_usage(
            snapshot,
            route_verified=route_ready,
            hard_constraints_passed=activation.get("passed") is True,
            prior_guide_physical_aliases=prior_guide_physical_aliases,
            sanitize_reused_guide_annotations=sanitize_reused_guide_annotations,
        )
        guide_requirement_satisfied = bool(
            not guide_evidence_usage or guide_evidence_usage.get("status") == "satisfied"
        )
        if not guide_requirement_satisfied:
            hard_failures.append("guide_grounded_requirement_unsatisfied")
        hard_failures = list(dict.fromkeys(hard_failures))
        pending_slot_count = len(
            [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        )
        structure_ready = bool(
            activation.get("passed") is True
            and route_ready
            and (compact_route_contract or provisional_segment_count == 0)
            and (not compact_route_contract or compact_route_verified)
            and spatial_evidence.get("passed") is True
            and meal_quality_passed
            and meal_diversity_passed
            and guide_requirement_satisfied
        )
        adoption_ready = structure_ready
        strict_passed = bool(adoption_ready and pending_slot_count == 0)
        confirmation_passed = adoption_ready
        adoption_mode = "complete" if strict_passed else "editable_draft" if adoption_ready else "blocked"
        soft_warnings: list[str] = []
        if not route_ready:
            soft_warnings.append("portfolio_route_quality:route_evidence_missing")
        if provisional_segment_count:
            soft_warnings.append("simple_direction_provisional_candidate_requires_review")
        if schedule_provisional_count:
            soft_warnings.append("simple_direction_opening_hours_unverified")
        if int(activation.get("providerExhaustedRequiredSlotCount") or 0):
            soft_warnings.append("simple_direction_required_provider_slot_unresolved")
        if spatial_evidence.get("deviations"):
            soft_warnings.append("simple_direction_spatial_preference_deviation_disclosed")
        return {
            **copy.deepcopy(activation),
            "passed": strict_passed,
            "draftPassed": activation.get("passed") is True,
            "confirmationPassed": confirmation_passed,
            "simpleOpenDirectionVerified": confirmation_passed,
            "pendingSlotCount": pending_slot_count,
            "structureReady": structure_ready,
            "adoptionReady": adoption_ready,
            "strictlyVerified": strict_passed,
            "isPartial": not strict_passed,
            "adoptionMode": adoption_mode,
            "compactRouteContractRequired": compact_route_contract,
            "routeStatus": route_status,
            "routeContractFingerprintMatches": audit_fingerprint_matches,
            "routeCoverageComplete": route_coverage_complete,
            "frozenRoutePairEvidencePassed": frozen_pair_evidence.get("passed") is True,
            "frozenRoutePairEvidenceReason": frozen_pair_evidence.get("reason"),
            "frozenExpectedPairs": copy.deepcopy(frozen_pair_evidence.get("expectedPairs") or []),
            "dailyRouteOverlapPolicy": daily_overlap_policy,
            "dailyRouteOverlapEvidenceRequired": overlap_evidence_required,
            "dailyRouteOverlapEvidencePassed": daily_overlap_evidence.get("passed") is True,
            "dailyRouteOverlapEvidenceReason": daily_overlap_evidence.get("reason"),
            "dailyRouteOverlapStatus": daily_overlap_evidence.get("status"),
            "dailyRouteOverlapDayCount": int(daily_overlap_evidence.get("dayCount") or 0),
            "adjacentLegCompliance": str(route_audit.get("adjacentLegCompliance") or "pending"),
            "topologyCompliance": str(route_audit.get("topologyCompliance") or "pending"),
            "providerBaselineCompared": route_audit.get("providerBaselineCompared") is True,
            "detourCompliance": str(route_audit.get("detourCompliance") or "not_evaluated"),
            "spatialPreferenceStatus": spatial_evidence.get("status"),
            "spatialPreferencePassed": spatial_evidence.get("passed") is True,
            "spatialPreferenceDeviations": copy.deepcopy(spatial_evidence.get("deviations") or []),
            "scheduleProvisionalSegmentCount": schedule_provisional_count,
            "mealExperienceBriefs": [
                copy.deepcopy(item.get("mealExperienceBrief") or {})
                for item in meal_quality.get("mealSemanticEvidence") or []
                if isinstance(item, dict) and isinstance(item.get("mealExperienceBrief"), dict)
            ],
            "mealSemanticEvidence": copy.deepcopy(meal_quality.get("mealSemanticEvidence") or []),
            "mealThemeSignature": copy.deepcopy(meal_quality.get("mealThemeSignature") or []),
            "mealQualityPassed": meal_quality_passed,
            "mealDiversityPassed": meal_diversity_passed,
            "mealUnresolvedReasons": copy.deepcopy(meal_quality.get("mealUnresolvedReasons") or []),
            "routeComfortEvidence": copy.deepcopy(route_audit.get("routeComfortEvidence") or {}),
            "guideEvidenceUsage": copy.deepcopy(guide_evidence_usage),
            "hardFailures": hard_failures,
            "softWarnings": soft_warnings,
        }

    @classmethod
    def _verify_spatial_preference_evidence(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        spatial = snapshot.get("spatialPreference") if isinstance(snapshot.get("spatialPreference"), dict) else {}
        if not spatial:
            return {"status": "not_requested", "passed": True, "deviations": []}
        if str(spatial.get("status") or "") != "resolved":
            return {"status": "unresolved", "passed": False, "deviations": []}
        fingerprint = str(spatial.get("fingerprint") or "")
        strength = str(spatial.get("strength") or "preferred")
        deviations: list[dict[str, Any]] = []
        evidence_count = 0
        inside_count = 0
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                if not poi or not cls._real_amap_poi_for_spatial(poi):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                constraints = (
                    metadata.get("scheduleConstraints") if isinstance(metadata.get("scheduleConstraints"), dict) else {}
                )
                evidence = (
                    constraints.get("spatialPreferenceEvidence")
                    if isinstance(constraints.get("spatialPreferenceEvidence"), dict)
                    else {}
                )
                status = str(evidence.get("status") or "unknown")
                if fingerprint and str(evidence.get("spatialPreferenceFingerprint") or "") != fingerprint:
                    status = "unknown"
                evidence_count += 1
                if status == "inside":
                    inside_count += 1
                    continue
                deviations.append(
                    {
                        "dayNumber": int(day.get("dayNumber") or 0),
                        "poiAmapId": str(poi.get("amapId") or ""),
                        "poiName": str(poi.get("name") or ""),
                        "status": status,
                        "distanceMeters": evidence.get("distanceMeters"),
                        "radiusMeters": evidence.get("radiusMeters"),
                        "candidateAdcode": evidence.get("candidateAdcode"),
                    }
                )
        passed = bool(
            evidence_count > 0
            and (inside_count == evidence_count if strength == "required" else len(deviations) <= evidence_count)
        )
        return {
            "status": "verified" if passed and not deviations else "deviation_disclosed" if passed else "failed",
            "passed": passed,
            "strength": strength,
            "evidenceCount": evidence_count,
            "insideCount": inside_count,
            "deviations": deviations,
        }

    @staticmethod
    def _real_amap_poi_for_spatial(poi: dict[str, Any]) -> bool:
        return bool(str(poi.get("source") or "") == "amap-place-search" and str(poi.get("amapId") or "").strip())

    @classmethod
    def _verify_compact_route_pair_evidence(
        cls,
        snapshot: dict[str, Any],
        *,
        route_audit: dict[str, Any],
        route_contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate frozen AMap legs against the proposal's actual day order."""

        expected_pairs: list[dict[str, Any]] = []
        targets_by_day = ProposalReadinessService.route_target_segments_by_day(snapshot)
        for day_number in sorted(targets_by_day):
            segments = targets_by_day[day_number]
            anchors: list[tuple[str, str]] = []
            for stop_ordinal, segment in enumerate(segments, start=1):
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                amap_id = str(poi.get("amapId") or "").strip().upper()
                if not ProposalReadinessService._real_amap_poi(poi) or not amap_id:
                    return {
                        "passed": False,
                        "reason": "route_anchor_amap_identity_invalid",
                        "expectedPairs": expected_pairs,
                    }
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                segment_identity = str(semantic.get("planningSlotId") or semantic.get("occurrenceId") or "").strip()
                if not segment_identity:
                    segment_identity = f"simple-open-route-stop:day:{day_number}:ordinal:{stop_ordinal}"
                anchors.append((segment_identity, amap_id))
            if len({segment_id for segment_id, _amap_id in anchors}) != len(anchors):
                return {
                    "passed": False,
                    "reason": "route_segment_identity_invalid",
                    "expectedPairs": expected_pairs,
                }
            expected_pairs.extend(
                {
                    "dayNumber": int(day_number),
                    "pairOrdinal": pair_ordinal,
                    "fromSegmentId": left[0],
                    "toSegmentId": right[0],
                    "fromAmapId": left[1],
                    "toAmapId": right[1],
                }
                for pair_ordinal, (left, right) in enumerate(zip(anchors, anchors[1:]), start=1)
            )

        expected_by_amap_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for pair in expected_pairs:
            expected_by_amap_pair.setdefault(
                (str(pair["fromAmapId"]), str(pair["toAmapId"])),
                [],
            ).append(pair)

        def normalized_pairs(values: Any, *, verified: bool) -> Optional[list[dict[str, Any]]]:
            if not isinstance(values, list):
                return None
            result: list[dict[str, Any]] = []
            identities: set[tuple[int, int, str, str, str, str]] = set()
            for value in values:
                if not isinstance(value, dict):
                    return None
                from_amap_id = str(value.get("fromAmapId") or "").strip().upper()
                to_amap_id = str(value.get("toAmapId") or "").strip().upper()
                if not from_amap_id or not to_amap_id or from_amap_id == to_amap_id:
                    return None
                identity_keys = ("dayNumber", "pairOrdinal", "fromSegmentId", "toSegmentId")
                has_any_identity = any(key in value for key in identity_keys)
                has_complete_identity = all(key in value for key in identity_keys)
                if has_any_identity and not has_complete_identity:
                    return None
                if has_complete_identity:
                    try:
                        day_number = int(value.get("dayNumber"))
                        pair_ordinal = int(value.get("pairOrdinal"))
                    except (TypeError, ValueError):
                        return None
                    from_segment_id = str(value.get("fromSegmentId") or "").strip()
                    to_segment_id = str(value.get("toSegmentId") or "").strip()
                    if (
                        day_number <= 0
                        or pair_ordinal <= 0
                        or not from_segment_id
                        or not to_segment_id
                        or from_segment_id == to_segment_id
                    ):
                        return None
                else:
                    # Legacy evidence carried only the AMap pair. Rebind it to
                    # the current snapshot only when that AMap edge occurs
                    # exactly once; repeated edges across days or positions are
                    # intrinsically ambiguous and must fail closed.
                    candidates = expected_by_amap_pair.get((from_amap_id, to_amap_id)) or []
                    if len(candidates) != 1:
                        return None
                    candidate = candidates[0]
                    day_number = int(candidate["dayNumber"])
                    pair_ordinal = int(candidate["pairOrdinal"])
                    from_segment_id = str(candidate["fromSegmentId"])
                    to_segment_id = str(candidate["toSegmentId"])
                identity = (
                    day_number,
                    pair_ordinal,
                    from_segment_id,
                    to_segment_id,
                    from_amap_id,
                    to_amap_id,
                )
                if identity in identities:
                    return None
                identities.add(identity)
                normalized: dict[str, Any] = {
                    "dayNumber": day_number,
                    "pairOrdinal": pair_ordinal,
                    "fromSegmentId": from_segment_id,
                    "toSegmentId": to_segment_id,
                    "fromAmapId": from_amap_id,
                    "toAmapId": to_amap_id,
                }
                if verified:
                    try:
                        duration_seconds = float(value.get("durationSeconds"))
                        distance_meters = float(value.get("distanceMeters"))
                    except (TypeError, ValueError):
                        return None
                    if (
                        not math.isfinite(duration_seconds)
                        or not math.isfinite(distance_meters)
                        or duration_seconds <= 0
                        or distance_meters <= 0
                    ):
                        return None
                    provider = str(value.get("provider") or "")
                    queried_at = str(value.get("queriedAt") or "").strip()
                    fingerprint = str(value.get("providerEvidenceFingerprint") or "").strip()
                    if provider != "amap-webservice" or not queried_at or len(fingerprint) != 64:
                        return None
                    # Verify the producer-owned frozen payload before applying
                    # semantic aliases. Older persisted proposals hashed
                    # ``public_transit`` while newer producers hash canonical
                    # ``transit``; both bind the same mode, and neither path
                    # permits a caller to replace any route fact.
                    raw_fingerprint_material: dict[str, Any] = {
                        "fromAmapId": str(value.get("fromAmapId") or "").strip(),
                        "toAmapId": str(value.get("toAmapId") or "").strip(),
                        "transportMode": str(value.get("transportMode") or "").strip(),
                        "durationSeconds": duration_seconds,
                        "distanceMeters": distance_meters,
                        "provider": provider,
                        "queriedAt": queried_at,
                    }
                    if has_complete_identity:
                        raw_fingerprint_material.update(
                            {
                                "dayNumber": value.get("dayNumber"),
                                "pairOrdinal": value.get("pairOrdinal"),
                                "fromSegmentId": value.get("fromSegmentId"),
                                "toSegmentId": value.get("toSegmentId"),
                            }
                        )
                    canonical_fingerprint_material = {
                        **raw_fingerprint_material,
                        "dayNumber": day_number,
                        "pairOrdinal": pair_ordinal,
                        "fromSegmentId": from_segment_id,
                        "toSegmentId": to_segment_id,
                        "fromAmapId": from_amap_id,
                        "toAmapId": to_amap_id,
                        "transportMode": cls._canonical_transport_mode(value.get("transportMode")),
                    }
                    if has_complete_identity:
                        accepted_fingerprints = {
                            cls._provider_evidence_fingerprint(raw_fingerprint_material),
                            cls._provider_evidence_fingerprint(canonical_fingerprint_material),
                        }
                    else:
                        legacy_canonical_material = {
                            **raw_fingerprint_material,
                            "fromAmapId": from_amap_id,
                            "toAmapId": to_amap_id,
                            "transportMode": cls._canonical_transport_mode(value.get("transportMode")),
                        }
                        accepted_fingerprints = {
                            cls._legacy_provider_evidence_fingerprint(raw_fingerprint_material),
                            cls._legacy_provider_evidence_fingerprint(legacy_canonical_material),
                        }
                    if fingerprint not in accepted_fingerprints:
                        return None
                    normalized.update(
                        {
                            "transportMode": cls._canonical_transport_mode(value.get("transportMode")),
                            "durationSeconds": duration_seconds,
                            "distanceMeters": distance_meters,
                            "provider": provider,
                            "queriedAt": queried_at,
                            "providerEvidenceFingerprint": fingerprint,
                        }
                    )
                result.append(normalized)
            return result

        stored_expected = normalized_pairs(route_audit.get("expectedPairs"), verified=False)
        stored_verified = normalized_pairs(route_audit.get("verifiedPairs"), verified=True)
        if stored_expected is None or stored_expected != expected_pairs:
            return {"passed": False, "reason": "route_expected_pairs_mismatch", "expectedPairs": expected_pairs}
        if stored_verified is None:
            return {"passed": False, "reason": "route_verified_pairs_invalid", "expectedPairs": expected_pairs}
        verified_identities = [
            {
                key: item[key]
                for key in (
                    "dayNumber",
                    "pairOrdinal",
                    "fromSegmentId",
                    "toSegmentId",
                    "fromAmapId",
                    "toAmapId",
                )
            }
            for item in stored_verified
        ]
        if verified_identities != expected_pairs:
            return {"passed": False, "reason": "route_verified_pairs_mismatch", "expectedPairs": expected_pairs}

        provenance = route_contract.get("provenance") if isinstance(route_contract.get("provenance"), dict) else {}
        expected_mode = cls._canonical_transport_mode(provenance.get("transportMode"))
        adjacent = (
            route_contract.get("adjacentLegConstraint")
            if isinstance(route_contract.get("adjacentLegConstraint"), dict)
            else {}
        )
        try:
            max_seconds = float(adjacent.get("maxProviderTravelMinutes")) * 60
            max_distance_meters = float(adjacent.get("candidateSearchRadiusMeters"))
        except (TypeError, ValueError):
            max_seconds = 0
            max_distance_meters = 0
        if (
            not expected_mode
            or not math.isfinite(max_seconds)
            or max_seconds <= 0
            or not math.isfinite(max_distance_meters)
            or max_distance_meters <= 0
        ):
            return {"passed": False, "reason": "route_contract_envelope_invalid", "expectedPairs": expected_pairs}
        for item in stored_verified:
            if item.get("transportMode") != expected_mode:
                return {"passed": False, "reason": "route_transport_mode_mismatch", "expectedPairs": expected_pairs}
            if (
                float(item.get("durationSeconds") or 0) > max_seconds
                or float(item.get("distanceMeters") or 0) > max_distance_meters
            ):
                return {"passed": False, "reason": "adjacent_leg_limit_exceeded", "expectedPairs": expected_pairs}
        return {"passed": True, "reason": None, "expectedPairs": expected_pairs}

    @classmethod
    def _verify_daily_route_overlap_evidence(
        cls,
        snapshot: dict[str, Any],
        *,
        route_audit: dict[str, Any],
    ) -> dict[str, Any]:
        """Recompute bounded selected-route geometry without Provider or DB work.

        Observe-mode evidence is diagnostic and never changes the established
        compact-route adoption gate.  Rank mode is an explicit behavioral
        claim, so its frozen selection evidence must be complete, sealed, and
        reproducible before the proposal can remain adoptable.
        """

        policy = str(route_audit.get("dailyRouteOverlapPolicy") or "observe").strip().casefold()
        if policy not in {"observe", "rank"}:
            return {
                "passed": False,
                "reason": "daily_route_overlap_policy_invalid",
                "status": "invalid",
                "dayCount": 0,
            }
        container = route_audit.get("dailyRouteOverlapEvidence")
        if not isinstance(container, dict) or str(container.get("schemaVersion") or "") != (
            "daily-route-continuity-audit-v1"
        ):
            return {
                "passed": False,
                "reason": "daily_route_overlap_evidence_missing",
                "status": "route_geometry_pending",
                "dayCount": 0,
            }
        values = container.get("perDay")
        if not isinstance(values, list):
            return {
                "passed": False,
                "reason": "daily_route_overlap_days_invalid",
                "status": "invalid",
                "dayCount": 0,
            }

        expected_by_day: dict[int, list[dict[str, str]]] = {}
        for day_number, segments in ProposalReadinessService.route_target_segments_by_day(snapshot).items():
            anchors: list[str] = []
            for segment in segments:
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                amap_id = str(poi.get("amapId") or "").strip().upper()
                if not ProposalReadinessService._real_amap_poi(poi) or not amap_id:
                    return {
                        "passed": False,
                        "reason": "daily_route_overlap_anchor_identity_invalid",
                        "status": "invalid",
                        "dayCount": 0,
                    }
                anchors.append(amap_id)
            if anchors:
                expected_by_day[day_number] = [
                    {"fromAmapId": left, "toAmapId": right} for left, right in zip(anchors, anchors[1:])
                ]

        indexed: dict[int, dict[str, Any]] = {}
        for value in values:
            if not isinstance(value, dict):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_day_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            try:
                day_number = int(value.get("dayNumber"))
            except (TypeError, ValueError):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_day_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            if day_number in indexed:
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_day_duplicate",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            indexed[day_number] = value
        if set(indexed) != set(expected_by_day):
            return {
                "passed": False,
                "reason": "daily_route_overlap_day_coverage_mismatch",
                "status": "route_geometry_pending",
                "dayCount": len(indexed),
            }

        verified_pairs = route_audit.get("verifiedPairs")
        if not isinstance(verified_pairs, list):
            verified_pairs = []
        frozen_pair_fingerprints = [
            str(item.get("providerEvidenceFingerprint") or "") for item in verified_pairs if isinstance(item, dict)
        ]
        observed_pair_fingerprints: list[str] = []
        selection_statuses: list[str] = []
        pending_geometry = False
        rank_limit_invalid = False
        rank_limit_exceeded = False
        route_contract = snapshot.get("routeDecisionContract")
        route_contract = route_contract if isinstance(route_contract, dict) else {}
        topology = (
            route_contract.get("topologyConstraint")
            if isinstance(route_contract.get("topologyConstraint"), dict)
            else {}
        )
        tolerance = (
            route_contract.get("detourTolerance") if isinstance(route_contract.get("detourTolerance"), dict) else {}
        )
        try:
            rank_overlap_limit = min(
                float(topology.get("maxBacktrackRatio")),
                float(tolerance.get("maxDetourRatio")),
            )
        except (TypeError, ValueError):
            rank_overlap_limit = math.nan
        if policy == "rank" and (not math.isfinite(rank_overlap_limit) or not 0 <= rank_overlap_limit <= 1):
            rank_limit_invalid = True
        service = DailyRouteOverlapService()
        replay_keys = (
            "schemaVersion",
            "geometryPolicyVersion",
            "dayNumber",
            "routePairFingerprints",
            "geometryFingerprint",
            "geometryComplete",
            "totalTraversedMeters",
            "repeatedMeters",
            "exemptRepeatedMeters",
            "nonExemptRepeatedMeters",
            "overlapRatio",
            "sameDirectionRepeatedMeters",
            "reverseDirectionRepeatedMeters",
            "exceptionApplications",
            "alternativesEvaluated",
            "selectedAlternativeIds",
            "selectedRouteGeometry",
            "source",
            "status",
            "failureReason",
            "missingGeometryRouteOptionIds",
            "oversizedGeometryRouteOptionIds",
            "geometryMaterialLimits",
        )
        for day_number in sorted(expected_by_day):
            evidence = indexed[day_number]
            if not DailyRouteOverlapService.verify_evidence_fingerprint(evidence):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_fingerprint_mismatch",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            route_geometry = evidence.get("selectedRouteGeometry")
            if not isinstance(route_geometry, list):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_geometry_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            actual_pairs = [
                {
                    "fromAmapId": str(item.get("fromAmapId") or "").strip().upper(),
                    "toAmapId": str(item.get("toAmapId") or "").strip().upper(),
                }
                for item in route_geometry
                if isinstance(item, dict)
            ]
            if len(actual_pairs) != len(route_geometry) or actual_pairs != expected_by_day[day_number]:
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_pair_mismatch",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            try:
                alternatives_evaluated = int(evidence.get("alternativesEvaluated"))
                bounded_count = int(evidence.get("boundedRouteOptionCombinationCount"))
                available_count = int(evidence.get("availableRouteOptionCombinationCount"))
            except (TypeError, ValueError):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_selection_count_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            if (
                alternatives_evaluated < 1
                or bounded_count < 1
                or bounded_count > 32
                or available_count < bounded_count
                or alternatives_evaluated > bounded_count
                or (policy == "observe" and alternatives_evaluated != 1)
                or bool(evidence.get("routeOptionCombinationTruncated")) != (available_count > 32)
            ):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_selection_count_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            selected_ids = evidence.get("selectedAlternativeIds")
            if not isinstance(selected_ids, list) or any(not str(item).strip() for item in selected_ids):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_selected_identity_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            if len(selected_ids) != len(route_geometry):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_selected_identity_mismatch",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            if selected_ids != [str(item.get("routeOptionId") or "") for item in route_geometry]:
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_selected_identity_mismatch",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }

            recomputed = service.evaluate(
                day_number=day_number,
                route_legs=route_geometry,
                alternatives_evaluated=alternatives_evaluated,
                selected_alternative_ids=selected_ids,
            )
            if any(recomputed.get(key) != evidence.get(key) for key in replay_keys):
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_replay_mismatch",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            observed_pair_fingerprints.extend(
                str(item.get("providerEvidenceFingerprint") or "") for item in route_geometry if isinstance(item, dict)
            )
            evidence_status = str(evidence.get("status") or "")
            selection_status = str(evidence.get("selectionStatus") or "")
            expected_selection_status = (
                "route_geometry_pending"
                if evidence_status == "route_geometry_pending"
                else "evaluated_single_option"
                if available_count <= 1
                else "ranked_bounded_options"
                if policy == "rank"
                else "observed_first_option"
            )
            if selection_status != expected_selection_status:
                return {
                    "passed": False,
                    "reason": "daily_route_overlap_selection_status_invalid",
                    "status": "invalid",
                    "dayCount": len(indexed),
                }
            selection_statuses.append(selection_status)
            pending_geometry = pending_geometry or evidence_status == "route_geometry_pending"
            if policy == "rank":
                try:
                    overlap_ratio = float(evidence.get("overlapRatio"))
                except (TypeError, ValueError):
                    overlap_ratio = math.nan
                rank_limit_exceeded = rank_limit_exceeded or (
                    not math.isfinite(overlap_ratio)
                    or (math.isfinite(rank_overlap_limit) and overlap_ratio > rank_overlap_limit)
                )

        if observed_pair_fingerprints != frozen_pair_fingerprints:
            return {
                "passed": False,
                "reason": "daily_route_overlap_provider_evidence_mismatch",
                "status": "invalid",
                "dayCount": len(indexed),
            }
        aggregate_status = (
            "route_geometry_pending"
            if "route_geometry_pending" in selection_statuses
            else "evaluated_single_option"
            if selection_statuses and all(item == "evaluated_single_option" for item in selection_statuses)
            else "ranked_bounded_options"
            if policy == "rank"
            else "observed_first_option"
        )
        if str(route_audit.get("dailyRouteOverlapStatus") or "") != aggregate_status:
            return {
                "passed": False,
                "reason": "daily_route_overlap_aggregate_status_mismatch",
                "status": "invalid",
                "dayCount": len(indexed),
            }
        if pending_geometry:
            return {
                "passed": False,
                "reason": "daily_route_overlap_geometry_pending",
                "status": aggregate_status,
                "dayCount": len(indexed),
            }
        if rank_limit_invalid:
            return {
                "passed": False,
                "reason": "daily_route_overlap_contract_invalid",
                "status": aggregate_status,
                "dayCount": len(indexed),
            }
        if rank_limit_exceeded:
            return {
                "passed": False,
                "reason": "daily_route_backtrack_limit_exceeded",
                "status": aggregate_status,
                "dayCount": len(indexed),
            }
        return {
            "passed": True,
            "reason": None,
            "status": aggregate_status,
            "dayCount": len(indexed),
        }

    @staticmethod
    def _canonical_transport_mode(value: Any) -> str:
        normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        return {
            "public_transit": "transit",
            "public_transport": "transit",
            "bus_subway": "transit",
            "subway": "transit",
            "metro": "transit",
        }.get(normalized, normalized)

    @staticmethod
    def _provider_evidence_fingerprint(value: dict[str, Any]) -> str:
        return DailyRouteOverlapService.provider_evidence_fingerprint(value)

    @staticmethod
    def _legacy_provider_evidence_fingerprint(value: dict[str, Any]) -> str:
        material = {
            key: value.get(key)
            for key in (
                "fromAmapId",
                "toAmapId",
                "transportMode",
                "durationSeconds",
                "distanceMeters",
                "provider",
                "queriedAt",
            )
        }
        for key in ("durationSeconds", "distanceMeters"):
            try:
                material[key] = float(material[key])
            except (TypeError, ValueError):
                pass
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def latest_root(self, *, session_id: str) -> Optional[dict[str, Any]]:
        """Return the latest live Simple direction root for one session."""

        rows = self.db.execute(
            "SELECT id FROM agent_plan_portfolios WHERE session_id = ? "
            "AND status IN ('awaiting_selection', 'committing', 'committed', 'failed') "
            "ORDER BY updated_at DESC",
            (session_id,),
        ).fetchall()
        for row in rows:
            root = self._root_by_id(str(row["id"]))
            if root is not None and root.get("workflowMode") == self.WORKFLOW_MODE:
                return root
        return None

    def prior_direction_physical_aliases(
        self,
        *,
        session_id: str,
        portfolio_id: str,
        include_required_entities: bool = False,
    ) -> frozenset[str]:
        """Return server-owned physical aliases from every visible direction.

        This is an acquisition input, not a novelty verdict.  It lets the
        bounded Simple executor reject cached entities before Consumer
        Admission while the existing proposal-level novelty audit remains the
        final fail-closed defense. The guide-specific offer gate additionally
        includes required entities: a permitted hard-slot repeat is not a new
        guide place. Readback/adoption do not pass this history to verification,
        so a saved proposal is never compared with itself.
        """

        root = self._root_by_id(str(portfolio_id or ""))
        if (
            root is None
            or root.get("workflowMode") != self.WORKFLOW_MODE
            or str(root.get("sessionId") or "") != str(session_id or "")
            or str(root.get("status") or "")
            not in {
                "awaiting_selection",
                "committing",
                "committed",
                "failed",
            }
        ):
            raise ValueError("simple_direction_physical_exclusion_scope_invalid")
        visible_ids = [str(item) for item in root.get("visibleProposalIds") or [] if str(item)]
        aliases: set[str] = set()
        for row in self._visible_rows(str(root["id"]), visible_ids):
            snapshot = self._json(row["snapshot_json"])
            meal_quality = MealExperiencePortfolioPolicy.snapshot_quality(snapshot)
            for record in meal_quality.get("mealSemanticEvidence") or []:
                if not isinstance(record, dict):
                    continue
                brand = str(record.get("canonicalBrand") or "").strip()
                family = str(record.get("groundedFamilyKey") or "").strip()
                if brand:
                    aliases.add(f"meal-brand:{brand}")
                if family:
                    aliases.add(f"meal-family:{family}")
            replaceable = self._replaceable_physical_entities_by_day(snapshot)
            groups = (
                [
                    frozenset(self._guide_physical_poi_aliases(segment.get("poi")))
                    for day in snapshot.get("days") or []
                    if isinstance(day, dict)
                    for segment in day.get("segments") or []
                    if isinstance(segment, dict)
                ]
                if include_required_entities
                else [group for day_groups in replaceable.values() for group in day_groups]
                if self._has_replaceability_metadata(snapshot)
                else self._physical_entity_alias_groups(snapshot)
            )
            for group in groups:
                aliases.update(group)
        return frozenset(aliases)

    def prior_required_occurrence_candidates(
        self,
        *,
        session_id: str,
        portfolio_id: str,
    ) -> dict[str, tuple[POI, ...]]:
        """Rehydrate verified hard-occurrence POIs from visible proposal truth.

        Continuation pagination may legitimately yield no novel campus on a
        later Provider page.  Required-slot completeness can then reuse only a
        previously persisted canonical POI for the same occurrence; aliases,
        labels, and Controller text are insufficient identity evidence.
        """

        root = self._root_by_id(str(portfolio_id or ""))
        if (
            root is None
            or root.get("workflowMode") != self.WORKFLOW_MODE
            or str(root.get("sessionId") or "") != str(session_id or "")
            or str(root.get("status") or "")
            not in {
                "awaiting_selection",
                "committing",
                "committed",
                "failed",
            }
        ):
            raise ValueError("simple_direction_prior_candidate_scope_invalid")

        visible_ids = [str(item) for item in root.get("visibleProposalIds") or [] if str(item)]
        candidates: dict[str, list[POI]] = {}
        seen_by_occurrence: dict[str, set[str]] = {}
        for row in self._visible_rows(str(root["id"]), visible_ids):
            snapshot = self._json(row["snapshot_json"])
            for day in snapshot.get("days") or []:
                if not isinstance(day, dict):
                    continue
                for segment in day.get("segments") or []:
                    if not isinstance(segment, dict):
                        continue
                    semantic = (
                        segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                    )
                    if str(semantic.get("groundingStatus") or "") != "verified_amap" or str(
                        semantic.get("requirementLevel") or ""
                    ) not in {"hard", "required"}:
                        continue
                    occurrence_id = str(semantic.get("occurrenceId") or "").strip()
                    payload = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                    amap_id = str(payload.get("amapId") or "").strip().upper()
                    if (
                        not occurrence_id
                        or not ProposalReadinessService._real_amap_poi(payload)
                        or not str(payload.get("name") or "").strip()
                    ):
                        continue
                    seen = seen_by_occurrence.setdefault(occurrence_id, set())
                    if amap_id in seen:
                        continue
                    try:
                        latitude = float(payload.get("latitude"))
                        longitude = float(payload.get("longitude"))
                        confidence = float(payload.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    queried_at: datetime | None = None
                    raw_queried_at = payload.get("providerQueriedAt")
                    if isinstance(raw_queried_at, datetime):
                        queried_at = raw_queried_at
                    elif str(raw_queried_at or "").strip():
                        try:
                            queried_at = datetime.fromisoformat(str(raw_queried_at).strip().replace("Z", "+00:00"))
                        except ValueError:
                            queried_at = None
                    seen.add(amap_id)
                    candidates.setdefault(occurrence_id, []).append(
                        POI(
                            id=str(payload.get("id") or f"poi_prior_{amap_id.lower()}"),
                            amap_id=amap_id,
                            parent_poi_id=str(payload.get("parentPoiId") or "").strip().upper() or None,
                            indoor_parent_poi_id=(str(payload.get("indoorParentPoiId") or "").strip().upper() or None),
                            name=str(payload.get("name") or ""),
                            city=str(payload.get("city") or snapshot.get("city") or ""),
                            category=str(payload.get("category") or "all"),
                            latitude=latitude,
                            longitude=longitude,
                            photo_url=str(payload.get("photoUrl") or "") or None,
                            source=str(payload.get("source") or "amap-place-search"),
                            confidence=confidence,
                            type=str(payload.get("type") or ""),
                            district=str(payload.get("district") or ""),
                            adcode=str(payload.get("adcode") or "") or None,
                            address=str(payload.get("address") or ""),
                            source_note=str(payload.get("sourceNote") or ""),
                            source_url=str(payload.get("sourceUrl") or "") or None,
                            photos=copy.deepcopy(payload.get("photos") or []),
                            tags=[str(item) for item in payload.get("tags") or [] if str(item)],
                            source_claims=copy.deepcopy(payload.get("sourceClaims") or []),
                            provider_aliases=[str(item) for item in payload.get("providerAliases") or [] if str(item)],
                            provider_type_code=str(payload.get("providerTypeCode") or "") or None,
                            business_status=str(payload.get("businessStatus") or "") or None,
                            provider_queried_at=queried_at,
                            provider_query_receipt_fingerprint=(
                                str(payload.get("providerQueryReceiptFingerprint") or "") or None
                            ),
                            experience_independence_evidence=copy.deepcopy(
                                payload.get("experienceIndependenceEvidence") or {}
                            ),
                            open_time_today=str(payload.get("openTimeToday") or "") or None,
                            open_time_week=str(payload.get("openTimeWeek") or "") or None,
                        )
                    )
        return {occurrence_id: tuple(values) for occurrence_id, values in candidates.items()}

    def resolve_view_context(
        self,
        *,
        session_id: str,
        active_version_id: Optional[str],
        view_context: dict[str, Any],
        explicit_direction_intent: Optional[str],
        explicit_action: Optional[str],
    ) -> dict[str, Any]:
        """Bind an explicit edit/repair intent to a server-owned view identity.

        A browser tab is presentation context, never an authorization source.
        Creation and continuation therefore cannot enter through this method;
        they must be resolved from a persisted opaque capability or from the
        natural-language intent router before any view identity is considered.
        """

        active_view = str(view_context.get("activeView") or "")
        if active_view not in {"comparison", "overview"}:
            raise ValueError("simple_direction_view_context_invalid")
        editing = view_context.get("editingProposal")
        focused = view_context.get("focusedProposal")
        if editing is not None and not isinstance(editing, dict):
            raise ValueError("simple_direction_view_context_invalid")
        if focused is not None and not isinstance(focused, dict):
            raise ValueError("simple_direction_view_context_invalid")
        resolved_action = str(explicit_action or "")
        if resolved_action not in {"edit_active_direction", "repair_comparison_direction"}:
            raise ValueError("simple_direction_view_context_invalid")
        if resolved_action == "edit_active_direction" and not isinstance(editing, dict):
            raise ValueError("simple_direction_view_editing_identity_required")

        root = self.latest_root(session_id=session_id)
        if resolved_action == "repair_comparison_direction":
            if not isinstance(focused, dict):
                raise ValueError("simple_direction_view_focused_identity_required")
            root = self._validate_focused_identity(session_id=session_id, focused=focused)
            requested_ordinal = int(view_context.get("requestedProposalOrdinal") or 0)
            visible_ids = [str(item) for item in root.get("visibleProposalIds") or []]
            if requested_ordinal and (
                requested_ordinal > len(visible_ids)
                or visible_ids[requested_ordinal - 1] != str(focused.get("proposalId") or "")
            ):
                raise ValueError("simple_direction_view_focused_identity_required")
        if isinstance(editing, dict):
            root = self._validate_editing_identity(
                session_id=session_id,
                active_version_id=active_version_id,
                editing=editing,
            )
        elif resolved_action == "edit_active_direction":
            raise ValueError("simple_direction_view_editing_identity_required")

        return {
            "schemaVersion": "agent-view-resolution-v1",
            "inputViewContext": copy.deepcopy(view_context),
            "explicitDirectionIntent": explicit_direction_intent,
            "resolvedAction": resolved_action,
            "resolutionSource": "server_validated_view_context",
            "planningSelectionRootTurnId": root.get("planningRootId") if root else None,
            "rootPortfolioId": root.get("id") if root else None,
            "proposalId": (
                str(focused.get("proposalId") or "")
                if resolved_action == "repair_comparison_direction" and isinstance(focused, dict)
                else str(editing.get("proposalId") or "")
                if isinstance(editing, dict)
                else None
            ),
            "activeVersionId": str(active_version_id or "") or None,
        }

    def _validate_focused_identity(self, *, session_id: str, focused: dict[str, Any]) -> dict[str, Any]:
        planning_root_id = str(focused.get("planningSelectionRootTurnId") or "")
        portfolio_id = str(focused.get("rootPortfolioId") or "")
        proposal_id = str(focused.get("proposalId") or "")
        source_turn_id = str(focused.get("sourceAssistantTurnId") or "")
        material_fingerprint = str(focused.get("materialFingerprint") or "")
        repair_choice_id = str(focused.get("repairChoiceId") or "")
        if not all(
            (
                planning_root_id,
                portfolio_id,
                proposal_id,
                source_turn_id,
                material_fingerprint,
                repair_choice_id,
            )
        ):
            raise ValueError("simple_direction_view_focused_identity_required")
        root = self._root_by_id(portfolio_id)
        row = self.db.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        if (
            root is None
            or row is None
            or root.get("workflowMode") != self.WORKFLOW_MODE
            or str(root.get("sessionId") or "") != session_id
            or str(root.get("planningRootId") or "") != planning_root_id
            or proposal_id not in set(root.get("visibleProposalIds") or [])
        ):
            raise ValueError("simple_direction_view_identity_mismatch")
        snapshot = self._json(row["snapshot_json"])
        projection_snapshot = copy.deepcopy(snapshot)
        projection_snapshot["portfolioVerifier"] = self._proposal_verifier(snapshot)
        expected_fingerprint = PlanComparisonPreviewService.material_fingerprint(projection_snapshot)
        expected_choice = self._repair_choice_id(portfolio_id, proposal_id, expected_fingerprint)
        if material_fingerprint != expected_fingerprint or repair_choice_id != expected_choice:
            raise ValueError("simple_direction_view_base_stale")
        source = self.db.execute(
            "SELECT agent_response_json FROM conversation_turns "
            "WHERE id = ? AND session_id = ? AND role = 'assistant' "
            "AND status IN ('active', 'internal_capability')",
            (source_turn_id, session_id),
        ).fetchone()
        response = self._json(source["agent_response_json"]) if source is not None else {}
        signed_projection = next(
            (
                item
                for item in response.get("comparisonProjections") or []
                if isinstance(item, dict)
                and str(item.get("sourceAssistantTurnId") or "") == source_turn_id
                and str(item.get("planningSelectionRootTurnId") or "") == planning_root_id
                and str(item.get("rootPortfolioId") or "") == portfolio_id
                and str(item.get("proposalId") or "") == proposal_id
                and str(item.get("materialFingerprint") or "") == material_fingerprint
                and str(item.get("repairChoiceId") or "") == repair_choice_id
            ),
            None,
        )
        if signed_projection is None:
            raise ValueError("simple_direction_view_identity_mismatch")
        return root

    @staticmethod
    def _repair_choice_id(portfolio_id: str, proposal_id: str, material_fingerprint: str) -> str:
        digest = hashlib.sha256(f"{portfolio_id}:{proposal_id}:{material_fingerprint}".encode("utf-8")).hexdigest()
        return f"simple_direction_repair_{digest[:24]}"

    def _validate_editing_identity(
        self,
        *,
        session_id: str,
        active_version_id: Optional[str],
        editing: dict[str, Any],
    ) -> dict[str, Any]:
        planning_root_id = str(editing.get("planningSelectionRootTurnId") or "")
        portfolio_id = str(editing.get("rootPortfolioId") or "")
        proposal_id = str(editing.get("proposalId") or "")
        source_turn_id = str(editing.get("sourceAssistantTurnId") or "")
        editing_version_id = str(editing.get("activeVersionId") or "")
        if not all((planning_root_id, portfolio_id, proposal_id, source_turn_id, editing_version_id)):
            raise ValueError("simple_direction_view_identity_mismatch")
        if not active_version_id or editing_version_id != str(active_version_id):
            raise ValueError("simple_direction_view_base_stale")
        root = self._root_by_id(portfolio_id)
        if (
            root is None
            or root.get("workflowMode") != self.WORKFLOW_MODE
            or str(root.get("sessionId") or "") != session_id
            or str(root.get("planningRootId") or "") != planning_root_id
            or str(root.get("selectedProposalId") or "") != proposal_id
            or str(root.get("expectedBaseVersionId") or "") != editing_version_id
            or str(root.get("status") or "") != "awaiting_selection"
            or proposal_id not in set(root.get("visibleProposalIds") or [])
        ):
            raise ValueError("simple_direction_view_identity_mismatch")
        proposal = self.db.execute(
            "SELECT 1 FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        source = self.db.execute(
            "SELECT status, agent_response_json FROM conversation_turns "
            "WHERE id = ? AND session_id = ? AND role = 'assistant' "
            "AND status IN ('active', 'internal_capability')",
            (source_turn_id, session_id),
        ).fetchone()
        if proposal is None or source is None:
            raise ValueError("simple_direction_view_identity_mismatch")
        response = self._json(source["agent_response_json"])
        projection = next(
            (
                item
                for item in response.get("comparisonProjections") or []
                if isinstance(item, dict)
                and str(item.get("proposalId") or "") == proposal_id
                and str(item.get("rootPortfolioId") or "") == portfolio_id
                and str(item.get("planningSelectionRootTurnId") or "") == planning_root_id
                and str(item.get("activeVersionId") or item.get("expectedBaseVersionId") or "") == editing_version_id
            ),
            None,
        )
        if projection is None:
            raise ValueError("simple_direction_view_identity_mismatch")
        return root

    def _root(self, *, session_id: str, planning_root_id: str) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT id FROM agent_plan_portfolios WHERE session_id = ? AND source_user_turn_id = ?",
            (session_id, planning_root_id),
        ).fetchone()
        return self._root_by_id(str(row["id"])) if row is not None else None

    def _root_by_id(self, portfolio_id: str) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT * FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            return None
        summary = self._json(row["summary_json"])
        frontier = (
            copy.deepcopy(summary.get("simpleDirectionFrontier"))
            if isinstance(summary.get("simpleDirectionFrontier"), dict)
            else {}
        )
        attempts = (
            copy.deepcopy(summary.get("simpleDirectionFrontierAttempts"))
            if isinstance(summary.get("simpleDirectionFrontierAttempts"), dict)
            else {}
        )
        compatibility_attempts = (
            copy.deepcopy(summary.get("simpleDirectionCompatibilityAttempts"))
            if isinstance(summary.get("simpleDirectionCompatibilityAttempts"), dict)
            else {}
        )
        compatibility_frontier = (
            copy.deepcopy(summary.get("simpleDirectionCompatibilityFrontier"))
            if isinstance(summary.get("simpleDirectionCompatibilityFrontier"), dict)
            else {}
        )
        provider_pending = any(
            isinstance(item, dict) and str(item.get("status") or "") == "provider_pending" for item in attempts.values()
        ) or any(
            isinstance(item, dict) and str(item.get("status") or "") == "provider_pending"
            for item in compatibility_attempts.values()
        )
        return {
            "id": str(row["id"]),
            "sessionId": str(row["session_id"]),
            "planningRootId": str(row["source_user_turn_id"]),
            "sourceAssistantTurnId": str(row["source_assistant_turn_id"] or ""),
            "requestContractFingerprint": str(row["request_contract_fingerprint"] or ""),
            "selectedProposalId": str(row["selected_proposal_id"] or ""),
            "expectedBaseVersionId": str(row["expected_base_version_id"] or "") or None,
            "status": str(row["status"] or ""),
            "workflowMode": str(summary.get("workflowMode") or ""),
            "visibleProposalIds": [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)],
            "requestIntentContract": copy.deepcopy(summary.get("requestIntentContract") or {}),
            "requestIntentContractMaterialFingerprint": str(
                summary.get("requestIntentContractMaterialFingerprint") or ""
            ),
            "simpleDirectionFrontier": frontier,
            "simpleDirectionFrontierAttempts": attempts,
            "simpleDirectionCompatibilityFrontier": compatibility_frontier,
            "simpleDirectionCompatibilityAttempts": compatibility_attempts,
            "simpleDirectionFrontierStatus": (
                "provider_pending" if provider_pending else str(frontier.get("frontierStatus") or "")
            ),
            "comparisonSummary": copy.deepcopy(summary.get("comparisonSummary") or {}),
        }

    def _reconcile_frontier_offer(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        outcomes: Optional[list[dict[str, Any]]],
        slot_query_outcomes: Optional[list[dict[str, Any]]],
        proposal_id: Optional[str],
        disposition: str,
        request_contract_fingerprint: str,
        blocking_layer: str = "",
        reason_code: str = "",
        continuation_metadata: Optional[dict[str, Any]] = None,
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if not str(execution_id or ""):
            return {}
        root = self._root_by_id(portfolio_id)
        if root is None or not root.get("simpleDirectionFrontier"):
            raise ValueError("simple_direction_frontier_missing")
        attempts = root.get("simpleDirectionFrontierAttempts")
        record = attempts.get(execution_id) if isinstance(attempts, dict) else None
        attempt = record.get("attempt") if isinstance(record, dict) else None
        if not isinstance(attempt, dict):
            raise ValueError("simple_direction_frontier_attempt_missing")
        assignments = [item for item in attempt.get("campusAssignments") or [] if isinstance(item, dict)]
        normalized_outcomes = [copy.deepcopy(item) for item in outcomes or [] if isinstance(item, dict)]
        if assignments:
            assignment_by_slot = {str(item.get("slotId") or ""): item for item in assignments}
            outcome_by_slot = {
                str(item.get("slotId") or ""): item for item in normalized_outcomes if str(item.get("slotId") or "")
            }
            if set(assignment_by_slot) != set(outcome_by_slot):
                raise ValueError("simple_direction_frontier_outcomes_incomplete")
            for slot_id, assignment in assignment_by_slot.items():
                outcome = outcome_by_slot[slot_id]
                outcome_entity = str(outcome.get("evidenceEntityFingerprint") or "")
                if outcome_entity and outcome_entity != str(assignment.get("evidenceEntityFingerprint") or ""):
                    raise ValueError("simple_direction_frontier_outcome_identity_mismatch")
                expected_query_fingerprint = str(assignment.get("queryFingerprint") or "")
                if (
                    expected_query_fingerprint
                    and str(outcome.get("queryFingerprint") or "") != expected_query_fingerprint
                ):
                    raise ValueError("simple_direction_frontier_outcome_query_identity_mismatch")
                expected_page = int(assignment.get("page") or 0)
                if expected_page and int(outcome.get("page") or 0) != expected_page:
                    raise ValueError("simple_direction_frontier_outcome_page_identity_mismatch")
        provider_failure = next(
            (item for item in normalized_outcomes if str(item.get("providerOutcome") or "") == "failure"),
            None,
        )
        if provider_failure is not None:
            disposition = "provider_pending"
        return self.store.reconcile_simple_direction_frontier_attempt(
            portfolio_id=portfolio_id,
            execution_id=execution_id,
            outcomes=normalized_outcomes,
            slot_query_outcomes=[copy.deepcopy(item) for item in slot_query_outcomes or [] if isinstance(item, dict)],
            proposal_id=proposal_id,
            disposition=disposition,
            expected_request_contract_fingerprint=self._minimum_fingerprint(request_contract_fingerprint),
            reason_code=(
                str(reason_code or "")
                or (
                    str(provider_failure.get("reasonCode") or "provider_unavailable")
                    if isinstance(provider_failure, dict)
                    else ""
                )
            ),
            blocking_layer=str(blocking_layer or ""),
            continuation_metadata=(
                copy.deepcopy(continuation_metadata) if isinstance(continuation_metadata, dict) else None
            ),
            remaining_query_scopes=remaining_query_scopes,
        )

    @staticmethod
    def _frontier_blocking_evidence(
        snapshot: dict[str, Any],
        verifier: dict[str, Any],
    ) -> tuple[str, str]:
        """Describe why a persisted partial cannot yet be adopted.

        A selected route combination may fail while other topology-admitted
        combinations remain unverified under the frozen four-call budget.  In
        that case the route *frontier* is pending, not exhausted.
        """

        if verifier.get("confirmationPassed") is True:
            return "", ""
        hard_failures = [str(item) for item in verifier.get("hardFailures") or [] if str(item)]
        route_audit = (
            snapshot.get("simpleOpenRouteAssignment")
            if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict)
            else {}
        )
        route_reason = str(route_audit.get("failureReason") or "")
        # A route cannot be evaluated while an explicit route-anchor slot is
        # still unresolved.  Treat that as the upstream POI frontier, not a
        # Provider transport failure merely because the downstream route audit
        # is also incomplete.  This distinction keeps real Provider failures
        # retry-controlled while allowing a different qualified entity pair to
        # be explored after semantic candidate rejection.
        if "simple_direction_pending_hard_slot" in hard_failures:
            return (
                "poi",
                (
                    route_reason
                    if route_reason == "candidate_assignment_incomplete"
                    else "simple_direction_pending_hard_slot"
                ),
            )
        if route_reason or any(
            marker in failure for failure in hard_failures for marker in ("route", "topology", "adjacent_leg")
        ):
            if route_audit.get("routeFeasibilityExhausted") is True:
                return "route", route_reason or "route_feasibility_exhausted"
            return "provider", route_reason or "selected_combination_route_blocked"
        if any(
            failure
            in {
                "simple_direction_no_verified_amap_anchor",
                "simple_direction_planned_day_missing_verified_amap_anchor",
            }
            for failure in hard_failures
        ):
            return "qualification", hard_failures[0]
        return "poi", (hard_failures[0] if hard_failures else "proposal_incomplete")

    @staticmethod
    def _frontier_failure_outcomes_from_claim(
        *,
        root: dict[str, Any],
        execution_id: str,
        reason_code: str,
    ) -> list[dict[str, Any]]:
        """Project an integrity failure onto the exact persisted claim.

        Client/provider material is deliberately not reused here.  The failed
        outcome carries only server-frozen assignment identities so the store
        can keep the claim/cursor pending without consuming an entity or page.
        """

        attempts = root.get("simpleDirectionFrontierAttempts")
        record = attempts.get(execution_id) if isinstance(attempts, dict) else None
        attempt = record.get("attempt") if isinstance(record, dict) else None
        if not isinstance(attempt, dict):
            raise ValueError("simple_direction_frontier_attempt_missing")
        assignments = [item for item in attempt.get("campusAssignments") or [] if isinstance(item, dict)]
        return [
            {
                "slotId": str(item.get("slotId") or ""),
                "evidenceEntityFingerprint": str(item.get("evidenceEntityFingerprint") or ""),
                "queryFingerprint": str(item.get("queryFingerprint") or ""),
                "page": int(item.get("page") or 0),
                "providerOutcome": "failure",
                "selectedAmapId": None,
                "reasonCode": reason_code,
            }
            for item in assignments
        ]

    def _frontier_attempt_for_novelty(
        self,
        *,
        root: dict[str, Any],
        execution_id: str,
        outcomes: list[dict[str, Any]],
        request_contract_fingerprint: str,
    ) -> Optional[dict[str, Any]]:
        """Rebind novelty to the persisted server claim, never client cursors.

        The executor outcome is useful only after every identity field matches
        the assignment frozen before Provider calls.  Historical Simple Open
        roots without a frontier continue through the v2 compatibility path.
        """

        execution_identity = str(execution_id or "").strip()
        if not execution_identity:
            return None
        attempts = root.get("simpleDirectionFrontierAttempts")
        record = attempts.get(execution_identity) if isinstance(attempts, dict) else None
        if not isinstance(record, dict):
            if isinstance(
                self._compatibility_attempt_for_execution(
                    root=root,
                    execution_id=execution_identity,
                    request_contract_fingerprint=request_contract_fingerprint,
                ),
                dict,
            ):
                return None
            raise ValueError("simple_direction_frontier_attempt_missing")
        if str(record.get("schemaVersion") or "") != "simple-direction-frontier-claim-v1":
            raise ValueError("simple_direction_frontier_attempt_invalid")
        if str(record.get("executionId") or "") != execution_identity:
            raise ValueError("simple_direction_frontier_attempt_identity_mismatch")
        expected_request_fingerprint = self._minimum_fingerprint(request_contract_fingerprint)
        if str(record.get("requestContractFingerprint") or "") != expected_request_fingerprint:
            raise ValueError("simple_direction_frontier_attempt_identity_mismatch")
        if str(record.get("status") or "") not in {"claimed", "reconciled"}:
            raise ValueError("simple_direction_frontier_attempt_not_available")
        attempt = record.get("attempt")
        if (
            not isinstance(attempt, dict)
            or str(attempt.get("schemaVersion") or "") != SimpleDirectionFrontierService.ATTEMPT_SCHEMA_VERSION
            or str(attempt.get("executionId") or "") != execution_identity
            or str(attempt.get("requestContractFingerprint") or "") != expected_request_fingerprint
        ):
            raise ValueError("simple_direction_frontier_attempt_invalid")
        assignments = [item for item in attempt.get("campusAssignments") or [] if isinstance(item, dict)]
        persisted_outcomes = [copy.deepcopy(item) for item in record.get("outcomes") or [] if isinstance(item, dict)]
        supplied_outcomes = [copy.deepcopy(item) for item in outcomes if isinstance(item, dict)]
        normalized_outcomes = supplied_outcomes or persisted_outcomes
        if assignments:
            normalized_outcomes = SimpleDirectionFrontierService.validate_attempt_outcomes(
                attempt=attempt,
                outcomes=normalized_outcomes,
            )
        rebound = copy.deepcopy(attempt)
        rebound["validatedOutcomes"] = normalized_outcomes
        return rebound

    def _compatibility_result_payload(
        self,
        response: dict[str, Any],
        *,
        request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Project the minimum crash-recoverable assistant result evidence."""

        return {
            **copy.deepcopy(response),
            "mode": "simple_open_direction_proposal",
            "workflowMode": self.WORKFLOW_MODE,
            "requestContractFingerprint": self._minimum_fingerprint(request_contract_fingerprint),
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        }

    def _compatibility_attempt_for_execution(
        self,
        *,
        root: dict[str, Any],
        execution_id: str,
        request_contract_fingerprint: str,
    ) -> Optional[dict[str, Any]]:
        execution_identity = str(execution_id or "").strip()
        if not execution_identity:
            return None
        attempts = root.get("simpleDirectionCompatibilityAttempts")
        record = attempts.get(execution_identity) if isinstance(attempts, dict) else None
        if not isinstance(record, dict):
            return None
        expected_request_fingerprint = self._minimum_fingerprint(request_contract_fingerprint)
        if (
            str(record.get("schemaVersion") or "")
            not in {
                "simple-direction-compatibility-attempt-v1",
                "simple-direction-compatibility-attempt-v2",
                "simple-direction-compatibility-attempt-v3",
            }
            or str(record.get("executionId") or "") != execution_identity
            or str(record.get("planningSelectionRootTurnId") or "") != str(root.get("planningRootId") or "")
            or str(record.get("rootPortfolioId") or "") != str(root.get("id") or "")
            or str(record.get("requestContractFingerprint") or "") != expected_request_fingerprint
            or str(record.get("status") or "") not in {"claimed", "provider_pending", "reconciled", "no_progress"}
            or str(record.get("attemptFingerprint") or "")
            != PlanPortfolioStore.simple_direction_compatibility_attempt_fingerprint(record)
        ):
            raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
        return copy.deepcopy(record)

    @staticmethod
    def _compatibility_page_has_more(
        attempt: dict[str, Any],
        *,
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
        slot_query_outcomes: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        snapshot = attempt.get("slotFrontierSnapshot")
        if isinstance(snapshot, dict) and slot_query_outcomes is not None:
            projected = copy.deepcopy(snapshot)
            if remaining_query_scopes is not None:
                projected["remainingQueryScopes"] = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                    remaining_query_scopes
                )
                projected["remainingQueryScopesAuthoritative"] = True
            for outcome in slot_query_outcomes:
                if not isinstance(outcome, dict):
                    return False
                query = outcome.get("query") if isinstance(outcome.get("query"), dict) else outcome
                projected = SimpleDirectionFrontierService.record_slot_query(
                    projected,
                    query=query,
                    provider_outcome=str(outcome.get("providerOutcome") or ""),
                    admitted_physical_groups=outcome.get("admittedPhysicalGroups") or [],
                    rejected_physical_groups=outcome.get("rejectedPhysicalGroups") or [],
                )
            return bool(SimpleDirectionFrontierService.claim_slot_queries(projected))
        if remaining_query_scopes:
            return True
        slot_queries = attempt.get("slotQueries")
        if isinstance(slot_queries, dict) and slot_queries:
            try:
                max_pages = int(attempt.get("maxPagesPerQuery") or 0)
                return any(
                    int(item.get("page") or 0) >= 1 and int(item.get("page") or 0) < max_pages
                    for item in slot_queries.values()
                    if isinstance(item, dict)
                )
            except (TypeError, ValueError):
                return False
        try:
            page = int(attempt.get("providerPage") or 0)
            max_pages = int(attempt.get("maxPagesPerQuery") or 0)
        except (TypeError, ValueError):
            return False
        return page >= 2 and max_pages >= page and page < max_pages

    @staticmethod
    def _compatibility_progress_evidence(
        *,
        slot_query_outcomes: Optional[list[dict[str, Any]]],
        proposal_delta: int,
        route_progress: Optional[dict[str, Any]],
    ) -> dict[str, bool]:
        query_progress = any(
            isinstance(item, dict)
            and str(item.get("providerOutcome") or "").strip().casefold() in {"success", "rejected"}
            for item in slot_query_outcomes or []
        )
        route_material = route_progress if isinstance(route_progress, dict) else {}
        try:
            topology_attempt_count = int(route_material.get("topologyCandidateAttemptCount") or 0)
        except (TypeError, ValueError):
            topology_attempt_count = 0
        route_progress_made = bool(
            route_material.get("routeProgressMadeThisTurn") is True or topology_attempt_count > 0
        )
        candidate_progress = int(proposal_delta or 0) == 1
        return {
            "madeProgress": bool(query_progress or candidate_progress or route_progress_made),
            "queryProgress": bool(query_progress),
            "candidateProgress": bool(candidate_progress),
            "routeProgress": bool(route_progress_made),
        }

    @staticmethod
    def _compatibility_route_has_more(route_progress: Optional[dict[str, Any]]) -> bool:
        material = route_progress if isinstance(route_progress, dict) else {}
        if material.get("routeFeasibilityExhausted") is True:
            return False
        try:
            return int(material.get("unverifiedTopologyCombinationCount") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _freeze_request_contract(
        self,
        *,
        portfolio_id: str,
        request_contract: dict[str, Any],
        request_contract_fingerprint: str,
    ) -> None:
        for _attempt in range(4):
            row = self.db.execute(
                "SELECT summary_json, request_contract_fingerprint FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("simple_direction_portfolio_not_found")
            if str(row["request_contract_fingerprint"] or "") != self._minimum_fingerprint(
                request_contract_fingerprint
            ):
                raise ValueError("simple_direction_request_contract_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            summary = self._json(raw_summary)
            incoming_material_fingerprint = self._fingerprint(request_contract)
            stored_contract = summary.get("requestIntentContract")
            stored_material_fingerprint = str(summary.get("requestIntentContractMaterialFingerprint") or "")
            if isinstance(stored_contract, dict) and stored_contract:
                if (
                    not stored_material_fingerprint
                    or self._fingerprint(stored_contract) != stored_material_fingerprint
                    or incoming_material_fingerprint != stored_material_fingerprint
                ):
                    raise ValueError("simple_direction_request_contract_fingerprint_mismatch")
                return
            summary["requestIntentContract"] = copy.deepcopy(request_contract)
            summary["requestIntentContractMaterialFingerprint"] = incoming_material_fingerprint
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (
                    json.dumps(summary, ensure_ascii=False, default=str),
                    datetime.now(timezone.utc).isoformat(),
                    portfolio_id,
                    raw_summary,
                ),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return
            self.db.rollback()
        raise ValueError("simple_direction_frontier_stale")

    def _mark_workflow(self, portfolio_id: str) -> None:
        for _attempt in range(4):
            row = self.db.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("simple_direction_portfolio_not_found")
            raw_summary = str(row["summary_json"] or "{}")
            summary = self._json(raw_summary)
            if summary.get("workflowMode") == self.WORKFLOW_MODE:
                return
            summary["workflowMode"] = self.WORKFLOW_MODE
            updated = self.db.execute(
                "UPDATE agent_plan_portfolios SET summary_json = ? WHERE id = ? AND summary_json = ?",
                (json.dumps(summary, ensure_ascii=False, default=str), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return
            self.db.rollback()
        raise ValueError("simple_direction_frontier_stale")

    def _reopen_for_direction_offer(
        self,
        *,
        portfolio_id: str,
        expected_base_version_id: Optional[str],
        source_assistant_turn_id: str,
        request_contract_fingerprint: str,
    ) -> None:
        row = self.db.execute(
            "SELECT summary_json, status, request_contract_fingerprint FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("simple_direction_portfolio_not_found")
        if str(row["status"] or "") == "committing":
            raise ValueError("simple_direction_offer_in_progress")
        frozen_request_fingerprint = str(row["request_contract_fingerprint"] or "")
        if not frozen_request_fingerprint or frozen_request_fingerprint != self._minimum_fingerprint(
            request_contract_fingerprint
        ):
            raise ValueError("simple_direction_request_contract_fingerprint_mismatch")
        summary = self._json(row["summary_json"])
        summary.update(
            {
                "workflowMode": self.WORKFLOW_MODE,
                "status": "awaiting_selection",
                "expectedBaseVersionId": str(expected_base_version_id or ""),
            }
        )
        raw_summary = str(row["summary_json"] or "{}")
        updated = self.db.execute(
            "UPDATE agent_plan_portfolios SET status = 'awaiting_selection', expected_base_version_id = ?, "
            "source_assistant_turn_id = ?, failure_reason = NULL, summary_json = ? WHERE id = ? "
            "AND status IN ('awaiting_selection', 'committed', 'failed') AND summary_json = ?",
            (
                expected_base_version_id,
                source_assistant_turn_id,
                json.dumps(summary, ensure_ascii=False, default=str),
                portfolio_id,
                raw_summary,
            ),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            status_row = self.db.execute(
                "SELECT status FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
            if status_row is not None and str(status_row["status"] or "") == "committing":
                raise ValueError("simple_direction_offer_in_progress")
            raise ValueError("simple_direction_portfolio_not_available")
        self.db.commit()

    def _visible_rows(self, portfolio_id: str, visible_ids: list[str]) -> list[sqlite3.Row]:
        if not visible_ids:
            return []
        placeholders = ",".join("?" for _ in visible_ids)
        rows = self.db.execute(
            f"SELECT id, choice_id, status, snapshot_json FROM agent_plan_proposals "
            f"WHERE portfolio_id = ? AND id IN ({placeholders})",
            (portfolio_id, *visible_ids),
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        return [by_id[item] for item in visible_ids if item in by_id]

    def _matching_physical_direction(
        self,
        *,
        portfolio_id: str,
        visible_proposal_ids: list[str],
        candidate_snapshot: dict[str, Any],
        frontier: Optional[dict[str, Any]] = None,
        frontier_attempt: Optional[dict[str, Any]] = None,
        frontier_outcomes: Optional[list[dict[str, Any]]] = None,
        candidate_confirmation_passed: bool = False,
    ) -> _PhysicalDirectionEvaluation:
        """Classify physical novelty without overloading reasons as identities.

        Proposal titles, local segment ids, and other display material are not
        direction novelty.  AMap can also expose one place through sibling
        child records, so equality is resolved through exact ids, valid parent
        ids, and the normalized physical name/address/coordinate key.
        """

        if isinstance(frontier_attempt, dict):
            return self._matching_physical_direction_v3(
                portfolio_id=portfolio_id,
                visible_proposal_ids=visible_proposal_ids,
                candidate_snapshot=candidate_snapshot,
                frontier=frontier if isinstance(frontier, dict) else {},
                frontier_attempt=frontier_attempt,
                frontier_outcomes=frontier_outcomes or [],
                candidate_confirmation_passed=candidate_confirmation_passed,
            )

        candidate_entities = self._physical_entity_alias_groups(candidate_snapshot)
        if not candidate_entities:
            # An empty/blocked first direction still needs to remain visible so
            # the user can understand what grounding failed.  With no real POI
            # identity there is no safe physical-equivalence claim to make.
            return _PhysicalDirectionEvaluation(status="distinct")
        use_v2 = self._has_replaceability_metadata(candidate_snapshot)
        candidate_by_day = self._replaceable_physical_entities_by_day(candidate_snapshot)
        comparisons: list[dict[str, Any]] = []
        first_failed_proposal_id = ""
        for row in self._visible_rows(portfolio_id, visible_proposal_ids):
            prior_snapshot = self._json(row["snapshot_json"])
            prior_entities = self._physical_entity_alias_groups(prior_snapshot)
            if not use_v2:
                if self._same_physical_entity_set(candidate_entities, prior_entities):
                    return _PhysicalDirectionEvaluation(
                        status="collision",
                        matching_proposal_id=str(row["id"]),
                        reason_code="same_physical_direction",
                    )
                continue
            prior_by_day = self._replaceable_physical_entities_by_day(prior_snapshot)
            day_evidence: list[dict[str, Any]] = []
            total_changed = 0
            per_day_passed = True
            for day_number in sorted(set(candidate_by_day) | set(prior_by_day)):
                candidate_groups = candidate_by_day.get(day_number, [])
                prior_groups = prior_by_day.get(day_number, [])
                if not candidate_groups and not prior_groups:
                    continue
                changed = sum(
                    1 for group in candidate_groups if not any(group & prior_group for prior_group in prior_groups)
                )
                total_changed += changed
                # Novelty cannot be satisfied by simply dropping every
                # replaceable POI from a day. Compare both contracts so an
                # empty current day fails just like an unchanged current day.
                if (candidate_groups or prior_groups) and changed < 1:
                    per_day_passed = False
                day_evidence.append(
                    {
                        "dayNumber": day_number,
                        "changedCount": changed,
                        "candidateCanonicalIdentities": sorted(min(group) for group in candidate_groups if group),
                        "priorCanonicalIdentities": sorted(min(group) for group in prior_groups if group),
                    }
                )
            passed = per_day_passed and total_changed >= 2
            comparisons.append(
                {
                    "priorProposalId": str(row["id"]),
                    "dayEvidence": day_evidence,
                    "totalChangedCount": total_changed,
                    "passed": passed,
                }
            )
            if not passed and not first_failed_proposal_id:
                first_failed_proposal_id = str(row["id"])
        if use_v2:
            candidate_snapshot["simpleDirectionNoveltyEvidence"] = {
                "schemaVersion": "simple-direction-novelty-v2",
                "priorDirectionExclusionApplied": bool(visible_proposal_ids),
                "comparisons": comparisons,
                "passed": not first_failed_proposal_id,
                "fingerprint": self._fingerprint(comparisons),
            }
        if first_failed_proposal_id:
            return _PhysicalDirectionEvaluation(
                status="collision",
                matching_proposal_id=first_failed_proposal_id,
                reason_code="no_material_novelty",
            )
        return _PhysicalDirectionEvaluation(status="distinct")

    def _matching_physical_direction_v3(
        self,
        *,
        portfolio_id: str,
        visible_proposal_ids: list[str],
        candidate_snapshot: dict[str, Any],
        frontier: dict[str, Any],
        frontier_attempt: dict[str, Any],
        frontier_outcomes: list[dict[str, Any]],
        candidate_confirmation_passed: bool,
    ) -> _PhysicalDirectionEvaluation:
        """Apply server-bound campus rotation and standalone-route novelty.

        Storage novelty and adoption readiness are intentionally separate.  A
        fully grounded new campus pair may remain as a repairable partial, but
        only a complete proposal must also prove per-day standalone changes and
        at least one changed ordered adjacent pair against every prior ready or
        repairable direction.
        """

        assignments = [
            copy.deepcopy(item) for item in frontier_attempt.get("campusAssignments") or [] if isinstance(item, dict)
        ]
        validated_outcomes = [
            copy.deepcopy(item)
            for item in frontier_attempt.get("validatedOutcomes") or frontier_outcomes
            if isinstance(item, dict)
        ]
        campus_assignments_grounded, assignment_evidence = self._campus_assignments_grounded(
            candidate_snapshot,
            assignments=assignments,
            outcomes=validated_outcomes,
        )
        candidate_campus = self._campus_groups_by_day(candidate_snapshot)
        candidate_standalone = self._standalone_replaceable_groups_by_day(candidate_snapshot)
        candidate_replaceable_days = self._replaceable_days(candidate_snapshot)
        candidate_pairs = self._ordered_adjacent_pairs_by_day(candidate_snapshot)
        candidate_meal_quality = MealExperiencePortfolioPolicy.snapshot_quality(candidate_snapshot)
        candidate_meal_signature = tuple(
            str(item) for item in candidate_meal_quality.get("mealThemeSignature") or [] if str(item)
        )
        candidate_meal_brands = {
            str(item.get("canonicalBrand") or "")
            for item in candidate_meal_quality.get("mealSemanticEvidence") or []
            if isinstance(item, dict) and str(item.get("canonicalBrand") or "")
        }
        history_rows = self._novelty_history_rows(
            portfolio_id=portfolio_id,
            visible_proposal_ids=visible_proposal_ids,
            frontier=frontier,
        )
        two_new_required = frontier_attempt.get("twoNewAnchorsRequired") is True
        single_new_fallback = frontier_attempt.get("singleNewAnchorFallback") is True
        assignment_days = {
            int(item.get("dayNumber") or 0) for item in assignments if int(item.get("dayNumber") or 0) > 0
        }
        new_assignment_days = {
            int(item.get("dayNumber") or 0)
            for item in assignments
            if item.get("isNewQualificationEntity") is True and int(item.get("dayNumber") or 0) > 0
        }
        comparisons: list[dict[str, Any]] = []
        first_failed_proposal_id = ""
        all_ready_passed = campus_assignments_grounded
        all_partial_passed = campus_assignments_grounded
        for row in history_rows:
            prior_snapshot = self._json(row["snapshot_json"])
            prior_campus = self._campus_groups_by_day(prior_snapshot)
            prior_standalone = self._standalone_replaceable_groups_by_day(prior_snapshot)
            prior_pairs = self._ordered_adjacent_pairs_by_day(prior_snapshot)
            prior_meal_quality = MealExperiencePortfolioPolicy.snapshot_quality(prior_snapshot)
            prior_meal_signature = tuple(
                str(item) for item in prior_meal_quality.get("mealThemeSignature") or [] if str(item)
            )
            prior_meal_brands = {
                str(item.get("canonicalBrand") or "")
                for item in prior_meal_quality.get("mealSemanticEvidence") or []
                if isinstance(item, dict) and str(item.get("canonicalBrand") or "")
            }
            meal_theme_signature_distinct = bool(
                not candidate_meal_signature
                or not prior_meal_signature
                or candidate_meal_signature != prior_meal_signature
            )
            meal_brands_disjoint = not bool(candidate_meal_brands & prior_meal_brands)

            campus_day_evidence: list[dict[str, Any]] = []
            changed_campus_days = 0
            for day_number in sorted(assignment_days | set(candidate_campus) | set(prior_campus)):
                candidate_groups = candidate_campus.get(day_number, [])
                prior_groups = prior_campus.get(day_number, [])
                changed = bool(candidate_groups) and all(
                    not any(candidate_group & prior_group for prior_group in prior_groups)
                    for candidate_group in candidate_groups
                )
                if changed:
                    changed_campus_days += 1
                campus_day_evidence.append(
                    {
                        "dayNumber": day_number,
                        "changed": changed,
                        "candidatePhysicalGroups": self._group_evidence(candidate_groups),
                        "priorPhysicalGroups": self._group_evidence(prior_groups),
                    }
                )
            changed_assignment_days = {
                int(item["dayNumber"])
                for item in campus_day_evidence
                if item.get("changed") is True and int(item.get("dayNumber") or 0) in assignment_days
            }
            if two_new_required:
                campus_rotation_passed = bool(
                    len(assignment_days) >= 2 and assignment_days.issubset(changed_assignment_days)
                )
            elif single_new_fallback:
                campus_rotation_passed = bool(changed_assignment_days)
            else:
                required_changed_days = new_assignment_days or assignment_days
                campus_rotation_passed = bool(
                    required_changed_days and required_changed_days.issubset(changed_assignment_days)
                )

            standalone_day_evidence: list[dict[str, Any]] = []
            total_standalone_changed = 0
            per_day_standalone_passed = bool(candidate_replaceable_days)
            for day_number in sorted(candidate_replaceable_days):
                candidate_groups = candidate_standalone.get(day_number, [])
                prior_groups = prior_standalone.get(day_number, [])
                changed_groups = [
                    group for group in candidate_groups if not any(group & prior_group for prior_group in prior_groups)
                ]
                changed_count = len(changed_groups)
                total_standalone_changed += changed_count
                if changed_count < 1:
                    per_day_standalone_passed = False
                standalone_day_evidence.append(
                    {
                        "dayNumber": day_number,
                        "changedCount": changed_count,
                        "candidatePhysicalGroups": self._group_evidence(candidate_groups),
                        "priorPhysicalGroups": self._group_evidence(prior_groups),
                        "changedPhysicalGroups": self._group_evidence(changed_groups),
                    }
                )
            standalone_novelty_passed = per_day_standalone_passed and total_standalone_changed >= 2

            ordered_pair_evidence: list[dict[str, Any]] = []
            changed_ordered_pair_count = 0
            for day_number in sorted(set(candidate_pairs) | set(prior_pairs)):
                day_candidate_pairs = candidate_pairs.get(day_number, [])
                day_prior_pairs = prior_pairs.get(day_number, [])
                changed_pairs = [
                    pair
                    for pair in day_candidate_pairs
                    if not any(self._same_ordered_pair(pair, prior_pair) for prior_pair in day_prior_pairs)
                ]
                changed_ordered_pair_count += len(changed_pairs)
                ordered_pair_evidence.append(
                    {
                        "dayNumber": day_number,
                        "candidatePairs": self._pair_evidence(day_candidate_pairs),
                        "priorPairs": self._pair_evidence(day_prior_pairs),
                        "changedPairCount": len(changed_pairs),
                    }
                )
            ordered_pair_novelty_passed = changed_ordered_pair_count >= 1
            ready_novelty_passed = bool(
                campus_rotation_passed
                and standalone_novelty_passed
                and ordered_pair_novelty_passed
                and meal_theme_signature_distinct
                and meal_brands_disjoint
            )
            # A user-visible repairable partial must change more than its
            # campus names.  At least one grounded meal/park (standalone
            # replaceable family) must also differ within this planning root;
            # campus-only material remains an internal repair candidate.
            partial_identity_passed = bool(
                campus_assignments_grounded
                and campus_rotation_passed
                and total_standalone_changed >= 1
            )
            passed_for_storage = ready_novelty_passed if candidate_confirmation_passed else partial_identity_passed
            comparisons.append(
                {
                    "priorProposalId": str(row["id"]),
                    "campusDayEvidence": campus_day_evidence,
                    "changedCampusDayCount": changed_campus_days,
                    "standaloneDayEvidence": standalone_day_evidence,
                    "totalStandaloneChangedCount": total_standalone_changed,
                    "orderedPairEvidence": ordered_pair_evidence,
                    "changedOrderedPairCount": changed_ordered_pair_count,
                    "campusRotationPassed": campus_rotation_passed,
                    "standaloneNoveltyPassed": standalone_novelty_passed,
                    "orderedPairNoveltyPassed": ordered_pair_novelty_passed,
                    "mealThemeSignatureDistinct": meal_theme_signature_distinct,
                    "mealBrandsDisjoint": meal_brands_disjoint,
                    "candidateMealThemeSignature": list(candidate_meal_signature),
                    "priorMealThemeSignature": list(prior_meal_signature),
                    "repeatedMealBrands": sorted(candidate_meal_brands & prior_meal_brands),
                    "readyNoveltyPassed": ready_novelty_passed,
                    "partialIdentityPassed": partial_identity_passed,
                    "passedForStorage": passed_for_storage,
                }
            )
            all_ready_passed = all_ready_passed and ready_novelty_passed
            all_partial_passed = all_partial_passed and partial_identity_passed
            if not passed_for_storage and not first_failed_proposal_id:
                first_failed_proposal_id = str(row["id"])

        ready_novelty_passed = bool(candidate_confirmation_passed and campus_assignments_grounded and all_ready_passed)
        repairable_partial_accepted = bool(
            not candidate_confirmation_passed and campus_assignments_grounded and all_partial_passed
        )
        passed = ready_novelty_passed if candidate_confirmation_passed else repairable_partial_accepted
        if not campus_assignments_grounded:
            reason_code = "frontier_assignment_grounding_incomplete"
        elif not passed and not candidate_confirmation_passed:
            reason_code = (
                "partial_experience_family_not_novel"
                if any(
                    item.get("campusRotationPassed") is True
                    and int(item.get("totalStandaloneChangedCount") or 0) < 1
                    for item in comparisons
                )
                else "campus_pair_already_explored"
            )
        elif not passed:
            meal_novelty_failed = any(
                item.get("mealThemeSignatureDistinct") is not True
                or item.get("mealBrandsDisjoint") is not True
                for item in comparisons
            )
            reason_code = (
                "simple_direction_meal_portfolio_novelty_failed"
                if meal_novelty_failed
                else "no_material_novelty"
            )
        else:
            reason_code = None
        evidence_material = {
            "schemaVersion": "simple-direction-novelty-v3",
            "frontierAttemptFingerprint": str(frontier_attempt.get("attemptFingerprint") or ""),
            "twoNewAnchorsRequired": two_new_required,
            "singleNewAnchorFallback": single_new_fallback,
            "newQualifiedEntityCount": int(frontier_attempt.get("newQualifiedEntityCount") or 0),
            "campusAssignmentsGrounded": campus_assignments_grounded,
            "campusAssignmentEvidence": assignment_evidence,
            "readyNoveltyPassed": ready_novelty_passed,
            "repairablePartialAccepted": repairable_partial_accepted,
            "passed": passed,
            "reasonCode": reason_code,
            "comparisons": comparisons,
        }
        evidence_material["fingerprint"] = self._fingerprint(evidence_material)
        candidate_snapshot["simpleDirectionNoveltyEvidence"] = evidence_material
        if passed:
            return _PhysicalDirectionEvaluation(status="distinct")
        if not campus_assignments_grounded:
            return _PhysicalDirectionEvaluation(
                status="incomplete",
                reason_code="frontier_assignment_grounding_incomplete",
            )
        return _PhysicalDirectionEvaluation(
            status="collision",
            matching_proposal_id=first_failed_proposal_id or None,
            reason_code=reason_code or "no_material_novelty",
        )

    @classmethod
    def _campus_assignments_grounded(
        cls,
        snapshot: dict[str, Any],
        *,
        assignments: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
    ) -> tuple[bool, list[dict[str, Any]]]:
        outcome_by_slot = {
            str(item.get("slotId") or ""): item
            for item in outcomes
            if isinstance(item, dict) and str(item.get("slotId") or "")
        }
        segment_by_slot: dict[str, tuple[int, dict[str, Any]]] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                slot_id = str(metadata.get("planningSlotId") or "")
                if slot_id and str(metadata.get("intentType") or "") == "campus_visit":
                    segment_by_slot[slot_id] = (day_number, segment)
        evidence: list[dict[str, Any]] = []
        passed = bool(assignments)
        for assignment in assignments:
            slot_id = str(assignment.get("slotId") or "")
            outcome = outcome_by_slot.get(slot_id, {})
            day_segment = segment_by_slot.get(slot_id)
            selected_amap_id = str(outcome.get("selectedAmapId") or "").strip().upper()
            identity_passed = False
            snapshot_amap_id = ""
            physical_group_id = ""
            if day_segment is not None:
                day_number, segment = day_segment
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                snapshot_amap_id = PoiPhysicalIdentityService.normalized_amap_id(poi)
                physical_group_id = PoiPhysicalIdentityService.physical_group_id(poi)
                identity_passed = bool(
                    day_number == int(assignment.get("dayNumber") or 0)
                    and ProposalReadinessService._real_amap_poi(poi)
                    and selected_amap_id
                    and selected_amap_id in {snapshot_amap_id, physical_group_id}
                )
            item_passed = bool(
                outcome
                and str(outcome.get("evidenceEntityFingerprint") or "")
                == str(assignment.get("evidenceEntityFingerprint") or "")
                and str(outcome.get("providerOutcome") or "") == "success"
                and identity_passed
            )
            passed = passed and item_passed
            evidence.append(
                {
                    "slotId": slot_id,
                    "dayNumber": int(assignment.get("dayNumber") or 0),
                    "evidenceEntityFingerprint": str(assignment.get("evidenceEntityFingerprint") or ""),
                    "selectedAmapId": selected_amap_id or None,
                    "snapshotAmapId": snapshot_amap_id or None,
                    "physicalGroupId": physical_group_id or None,
                    "passed": item_passed,
                }
            )
        return passed, evidence

    def _novelty_history_rows(
        self,
        *,
        portfolio_id: str,
        visible_proposal_ids: list[str],
        frontier: dict[str, Any],
    ) -> list[sqlite3.Row]:
        repairable_partial_ids = {
            str(item.get("assignedProposalId") or "")
            for item in frontier.get("qualifiedEntityFrontier") or []
            if isinstance(item, dict)
            and str(item.get("state") or "") == "assigned_partial"
            and str(item.get("assignedProposalId") or "")
        }
        return [
            row
            for row in self._visible_rows(portfolio_id, visible_proposal_ids)
            if str(row["status"] or "") in {"adoption_ready", "committed"} or str(row["id"]) in repairable_partial_ids
        ]

    @staticmethod
    def _has_replaceability_metadata(snapshot: dict[str, Any]) -> bool:
        return any(
            "replaceablePoi" in constraints
            for day in snapshot.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
            for metadata in [
                segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            ]
            for constraints in [
                metadata.get("scheduleConstraints") if isinstance(metadata.get("scheduleConstraints"), dict) else {}
            ]
        )

    @classmethod
    def _campus_groups_by_day(
        cls,
        snapshot: dict[str, Any],
    ) -> dict[int, list[frozenset[str]]]:
        result: dict[int, list[frozenset[str]]] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                if str(metadata.get("intentType") or "") != "campus_visit":
                    continue
                aliases = cls._segment_physical_group(segment)
                if aliases:
                    result.setdefault(day_number, []).append(aliases)
        return result

    @classmethod
    def _standalone_replaceable_groups_by_day(
        cls,
        snapshot: dict[str, Any],
        *,
        include_campus: bool = False,
    ) -> dict[int, list[frozenset[str]]]:
        result: dict[int, list[frozenset[str]]] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                constraints = (
                    metadata.get("scheduleConstraints") if isinstance(metadata.get("scheduleConstraints"), dict) else {}
                )
                if constraints.get("replaceablePoi") is not True:
                    continue
                intent_type = str(metadata.get("intentType") or segment.get("kind") or "")
                if intent_type == "campus_visit" and not include_campus:
                    # Campus rotation is measured independently. Counting it
                    # again would let otherwise identical directions claim
                    # standalone experience novelty without changing a meal,
                    # park, or other non-campus experience.
                    continue
                independence_status = cls._segment_independence_status(segment)
                if independence_status in {"embedded_in_day_anchor", "independence_pending"}:
                    continue
                if intent_type == "park" and independence_status != "standalone_verified":
                    continue
                aliases = cls._segment_physical_group(segment)
                if aliases:
                    result.setdefault(day_number, []).append(aliases)
        return result

    @staticmethod
    def _replaceable_days(snapshot: dict[str, Any]) -> set[int]:
        return {
            int(day.get("dayNumber") or 0)
            for day in snapshot.get("days") or []
            if isinstance(day, dict)
            and any(
                isinstance(segment, dict)
                and isinstance(segment.get("semanticMetadata"), dict)
                and isinstance(segment["semanticMetadata"].get("scheduleConstraints"), dict)
                and segment["semanticMetadata"]["scheduleConstraints"].get("replaceablePoi") is True
                for segment in day.get("segments") or []
            )
        }

    @classmethod
    def _ordered_route_groups_by_day(
        cls,
        snapshot: dict[str, Any],
    ) -> dict[int, list[frozenset[str]]]:
        result: dict[int, list[frozenset[str]]] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            ordered: list[frozenset[str]] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                constraints = (
                    metadata.get("scheduleConstraints") if isinstance(metadata.get("scheduleConstraints"), dict) else {}
                )
                intent_type = str(metadata.get("intentType") or segment.get("kind") or "")
                include = intent_type == "campus_visit"
                if constraints.get("replaceablePoi") is True:
                    independence_status = cls._segment_independence_status(segment)
                    include = bool(
                        independence_status not in {"embedded_in_day_anchor", "independence_pending"}
                        and (intent_type != "park" or independence_status == "standalone_verified")
                    )
                if not include:
                    continue
                aliases = cls._segment_physical_group(segment)
                if not aliases or (ordered and ordered[-1] & aliases):
                    continue
                ordered.append(aliases)
            if ordered:
                result[day_number] = ordered
        return result

    @classmethod
    def _ordered_adjacent_pairs_by_day(
        cls,
        snapshot: dict[str, Any],
    ) -> dict[int, list[tuple[frozenset[str], frozenset[str]]]]:
        return {
            day_number: list(zip(groups, groups[1:]))
            for day_number, groups in cls._ordered_route_groups_by_day(snapshot).items()
            if len(groups) >= 2
        }

    @staticmethod
    def _segment_independence_status(segment: dict[str, Any]) -> str:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        evidence = (
            poi.get("experienceIndependenceEvidence")
            if isinstance(poi.get("experienceIndependenceEvidence"), dict)
            else poi.get("experience_independence_evidence")
            if isinstance(poi.get("experience_independence_evidence"), dict)
            else {}
        )
        if not evidence:
            metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            constraints = (
                metadata.get("scheduleConstraints") if isinstance(metadata.get("scheduleConstraints"), dict) else {}
            )
            evidence = (
                constraints.get("experienceIndependenceEvidence")
                if isinstance(constraints.get("experienceIndependenceEvidence"), dict)
                else {}
            )
        return str(evidence.get("status") or "")

    @classmethod
    def _segment_physical_group(cls, segment: dict[str, Any]) -> frozenset[str]:
        return frozenset(cls._physical_poi_aliases(segment.get("poi")))

    @staticmethod
    def _same_ordered_pair(
        left: tuple[frozenset[str], frozenset[str]],
        right: tuple[frozenset[str], frozenset[str]],
    ) -> bool:
        return bool(left[0] & right[0] and left[1] & right[1])

    @staticmethod
    def _group_evidence(groups: list[frozenset[str]]) -> list[dict[str, Any]]:
        material: list[dict[str, Any]] = []
        for group in groups:
            aliases = sorted(str(item) for item in group if str(item))
            group_alias = next((item for item in aliases if item.startswith("group:")), "")
            amap_alias = next((item for item in aliases if item.startswith("amap:")), "")
            material.append(
                {
                    "physicalGroupId": (group_alias or amap_alias).split(":", 1)[-1] or None,
                    "canonicalAliases": aliases,
                }
            )
        return material

    @classmethod
    def _pair_evidence(
        cls,
        pairs: list[tuple[frozenset[str], frozenset[str]]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "from": cls._group_evidence([pair[0]])[0],
                "to": cls._group_evidence([pair[1]])[0],
            }
            for pair in pairs
        ]

    @classmethod
    def _replaceable_physical_entities_by_day(
        cls,
        snapshot: dict[str, Any],
    ) -> dict[int, list[frozenset[str]]]:
        # Compatibility novelty v2 historically compares every replaceable
        # entity, including campuses. The v3 standalone axis deliberately
        # excludes campuses because it measures them on a separate axis.
        return cls._standalone_replaceable_groups_by_day(snapshot, include_campus=True)

    @classmethod
    def _physical_entity_alias_groups(cls, snapshot: dict[str, Any]) -> list[frozenset[str]]:
        """Collapse parent/child aliases into proposal-local physical entities."""

        groups: list[set[str]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                aliases = cls._physical_poi_aliases(segment.get("poi"))
                if not aliases:
                    continue
                overlapping = [index for index, group in enumerate(groups) if group & aliases]
                merged = set(aliases)
                for index in reversed(overlapping):
                    merged.update(groups.pop(index))
                groups.append(merged)
        return [frozenset(group) for group in groups]

    @staticmethod
    def _physical_poi_aliases(poi: Any) -> set[str]:
        if not ProposalReadinessService._real_amap_poi(poi):
            return set()
        exact_id = PoiPhysicalIdentityService.normalized_amap_id(poi)
        canonical_id = PoiPhysicalIdentityService.canonical_amap_id(poi)
        aliases = {f"amap:{identity}" for identity in (exact_id, canonical_id) if identity}
        if canonical_id:
            aliases.add(f"group:{canonical_id}")

        def normalized(value: Any) -> str:
            return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(value or "")).casefold()

        try:
            latitude = float(poi.get("latitude"))
            longitude = float(poi.get("longitude"))
        except (AttributeError, TypeError, ValueError):
            return aliases
        name = normalized(poi.get("name"))
        address = normalized(poi.get("address"))
        if name and math.isfinite(latitude) and math.isfinite(longitude) and latitude != 0 and longitude != 0:
            aliases.add(f"physical:{name}|{address}|{longitude:.6f}|{latitude:.6f}")
            raw_name = re.sub(r"[（(].*?[）)]", "", str(poi.get("name") or "")).strip()
            family_name = re.sub(r"(?:一期|二期|三期|分园|东园|西园|南园|北园)$", "", raw_name).strip()
            normalized_family = normalized(family_name)
            if normalized_family:
                # A name-only family is unsafe for common distant names. Bind
                # the conservative alias to either an address body or a nearby
                # coordinate cell; far-away homonyms cannot intersect it.
                address_body = re.sub(r"(?:一期|二期|三期|分园|东园|西园|南园|北园|\d+号?)", "", address)
                if len(address_body) >= 4:
                    aliases.add(f"venue-family:{normalized_family}|address:{address_body}")
                aliases.add(f"venue-family:{normalized_family}|cell:{longitude:.3f}|{latitude:.3f}")
        return aliases

    @classmethod
    def _guide_physical_poi_aliases(cls, poi: Any) -> set[str]:
        """Use every parent alias for guide novelty without changing hard-slot policy."""

        aliases = cls._physical_poi_aliases(poi)
        if not aliases:
            return aliases
        for key in ("parentPoiId", "parent_poi_id", "indoorParentPoiId", "indoor_parent_poi_id"):
            identity = str(poi.get(key) or "").strip().upper()
            if re.fullmatch(r"B[0-9A-Z]{8,31}", identity):
                aliases.add(f"amap:{identity}")
                aliases.add(f"group:{identity}")
        return aliases

    @staticmethod
    def _same_physical_entity_set(left: list[frozenset[str]], right: list[frozenset[str]]) -> bool:
        if not left or len(left) != len(right):
            return False

        def match(index: int, used_right: set[int]) -> bool:
            if index == len(left):
                return True
            for right_index, right_aliases in enumerate(right):
                if right_index in used_right or not (left[index] & right_aliases):
                    continue
                used_right.add(right_index)
                if match(index + 1, used_right):
                    return True
                used_right.remove(right_index)
            return False

        return match(0, set())

    def _latest_capability_context(
        self,
        *,
        session_id: str,
        portfolio_id: str,
        proposal_id: str,
    ) -> Optional[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, agent_request_json, agent_response_json FROM conversation_turns "
            "WHERE session_id = ? AND role = 'assistant' AND status != 'superseded' ORDER BY turn_index DESC",
            (session_id,),
        ).fetchall()
        decoded_rows = [(row, self._json(row["agent_response_json"])) for row in rows]
        continuation_authorized = any(
            isinstance(option, dict)
            and str(option.get("rootPortfolioId") or "") == portfolio_id
            and str(option.get("action") or "") == "continue_plan_expansion"
            and str(option.get("kind") or "") == "simple_direction_more_plans"
            for _row, payload in decoded_rows
            for option in payload.get("choiceOptions") or []
        )
        for row, payload in decoded_rows:
            for option in payload.get("choiceOptions") or []:
                if (
                    isinstance(option, dict)
                    and str(option.get("rootPortfolioId") or "") == portfolio_id
                    and str(option.get("proposalId") or "") == proposal_id
                    and str(option.get("action") or "") == "select_plan_proposal"
                ):
                    return {
                        "turnId": str(row["id"]),
                        "request": self._json(row["agent_request_json"]),
                        "response": payload,
                        "continuationAuthorized": continuation_authorized,
                    }
        return None

    def _sync_expected_base_version(
        self,
        *,
        session_id: str,
        planning_root_id: str,
        portfolio_id: str,
        proposal_id: str,
        expected_previous_base_version_id: str,
        next_base_version_id: str,
    ) -> None:
        row = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("simple_direction_save_material_missing")
        summary = self._json(row["summary_json"])
        summary.update(
            {
                "workflowMode": self.WORKFLOW_MODE,
                "status": "awaiting_selection",
                "selectedProposalId": proposal_id,
                "expectedBaseVersionId": next_base_version_id,
            }
        )
        updated = self.db.execute(
            "UPDATE agent_plan_portfolios SET expected_base_version_id = ?, summary_json = ?, updated_at = ? "
            "WHERE id = ? AND session_id = ? AND source_user_turn_id = ? AND selected_proposal_id = ? "
            "AND status = 'awaiting_selection' AND COALESCE(expected_base_version_id, '') = ?",
            (
                next_base_version_id,
                json.dumps(summary, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
                portfolio_id,
                session_id,
                planning_root_id,
                proposal_id,
                expected_previous_base_version_id,
            ),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            status_row = self.db.execute(
                "SELECT status FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
            if status_row is not None and str(status_row["status"] or "") == "committing":
                raise ValueError("simple_direction_save_in_progress")
            raise ValueError("simple_direction_save_base_stale")

    def _persist_capability_carrier(
        self,
        *,
        session_id: str,
        portfolio_id: str,
        proposal_id: str,
        base_version_id: str,
        previous_capability: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        turn_id = f"turn_{uuid4().hex[:12]}"
        prior_request = (
            copy.deepcopy(previous_capability.get("request") or {}) if isinstance(previous_capability, dict) else {}
        )
        prior_response = (
            copy.deepcopy(previous_capability.get("response") or {}) if isinstance(previous_capability, dict) else {}
        )
        prior_continue_authorized = bool(
            isinstance(previous_capability, dict) and previous_capability.get("continuationAuthorized") is True
        ) or any(
            isinstance(item, dict)
            and str(item.get("action") or "") == "continue_plan_expansion"
            and str(item.get("kind") or "") == "simple_direction_more_plans"
            for item in prior_response.get("choiceOptions") or []
        )
        material = self.response_material(
            portfolio_id=portfolio_id,
            source_assistant_turn_id=turn_id,
            expected_base_version_id=base_version_id,
            update_mode="replace",
            # A save-back rotates the capability carrier.  For roots without a
            # server frontier, preserve an already-issued expansion capability
            # rather than silently dropping it.  Qualified roots ignore this
            # delta and remain governed exclusively by their persisted frontier.
            proposal_delta=1 if prior_continue_authorized else 0,
        )
        self._sync_portfolio_source_assistant_turn(
            portfolio_id=portfolio_id,
            source_assistant_turn_id=turn_id,
        )
        self._sync_material_classification_counts(
            portfolio_id=portfolio_id,
            material=material,
        )
        prior_request.update(
            {
                "activeVersionId": base_version_id,
                "directionSaveActive": {
                    "workflowMode": self.WORKFLOW_MODE,
                    "rootPortfolioId": portfolio_id,
                    "proposalId": proposal_id,
                },
            }
        )
        reply = ""
        response_payload = {
            "mode": "simple_direction_save_active",
            "workflowMode": self.WORKFLOW_MODE,
            "internalCapabilityCarrier": True,
            "reply": reply,
            "comparisonProjections": material["comparisonProjections"],
            "comparisonProjectionUpdateMode": "replace",
            "choiceOptions": material["choiceOptions"],
            "planningSelectionRootTurnId": material["planningSelectionRootTurnId"],
            "rootPortfolioId": portfolio_id,
            "visibleProposalCount": len(material["comparisonProjections"]),
            "terminalStatus": "needs_confirmation",
        }
        for key in ("resolvedTripDates", "initialPlan", "pipelineContext", "grounding"):
            if prior_response.get(key) is not None:
                response_payload[key] = copy.deepcopy(prior_response[key])
        now = datetime.now(timezone.utc).isoformat()
        turn_index = (
            int(
                self.db.execute(
                    "SELECT COALESCE(MAX(turn_index), 0) FROM conversation_turns WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            + 1
        )
        self.db.execute(
            "INSERT INTO conversation_turns ("
            "id, session_id, role, content, turn_index, status, parent_turn_id, itinerary_version_id, "
            "agent_request_json, agent_response_json, error_json, created_at, updated_at"
            ") VALUES (?, ?, 'assistant', ?, ?, 'internal_capability', ?, ?, ?, ?, NULL, ?, ?)",
            (
                turn_id,
                session_id,
                reply,
                turn_index,
                (
                    str(previous_capability.get("turnId") or "") or None
                    if isinstance(previous_capability, dict)
                    else None
                ),
                base_version_id,
                json.dumps(prior_request, ensure_ascii=False, default=str),
                json.dumps(response_payload, ensure_ascii=False, default=str),
                now,
                now,
            ),
        )
        self.db.execute(
            "UPDATE conversation_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        self.db.commit()
        return material

    def _sync_proposal_snapshot_fingerprint(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
    ) -> None:
        """Keep the frozen commit identity aligned with a server-owned save/repair.

        Only these authenticated mutation paths may refresh the fingerprint.  A
        client cannot supply either the snapshot identity or the lineage value.
        """

        row = self.db.execute(
            "SELECT snapshot_json, generation_lineage_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        if row is None:
            raise ValueError("simple_direction_proposal_lineage_missing")
        stored_snapshot = self._json(row["snapshot_json"])
        lineage = self._json(row["generation_lineage_json"])
        lineage["proposalSnapshotFingerprint"] = self._fingerprint(stored_snapshot)
        updated = self.db.execute(
            "UPDATE agent_plan_proposals SET generation_lineage_json = ?, updated_at = ? "
            "WHERE id = ? AND portfolio_id = ?",
            (
                json.dumps(lineage, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
                proposal_id,
                portfolio_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("simple_direction_proposal_lineage_missing")

    def update_server_verified_proposal_material(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        snapshot: dict[str, Any],
        verifier: dict[str, Any],
        status: str,
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        """Atomically persist server verification and its frozen identity."""

        store = PlanPortfolioStore(self.db)
        try:
            store.update_proposal_material(
                portfolio_id=portfolio_id,
                proposal_id=proposal_id,
                snapshot=snapshot,
                verifier=verifier,
                status=status,
                evidence=evidence,
                commit=False,
            )
            self._sync_proposal_snapshot_fingerprint(
                portfolio_id=portfolio_id,
                proposal_id=proposal_id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _sync_portfolio_source_assistant_turn(
        self,
        *,
        portfolio_id: str,
        source_assistant_turn_id: str,
    ) -> None:
        updated = self.db.execute(
            "UPDATE agent_plan_portfolios SET source_assistant_turn_id = ?, updated_at = ? "
            "WHERE id = ? AND status != 'committing'",
            (
                source_assistant_turn_id,
                datetime.now(timezone.utc).isoformat(),
                portfolio_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("simple_direction_portfolio_not_available")

    def _sync_material_classification_counts(
        self,
        *,
        portfolio_id: str,
        material: dict[str, Any],
    ) -> None:
        """Keep root badges aligned with the signed save-back carrier."""

        row = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("simple_direction_portfolio_not_found")
        summary = self._json(row["summary_json"])
        projections = [item for item in material.get("comparisonProjections") or [] if isinstance(item, dict)]
        verified_count = sum(item.get("strictlyVerified") is True for item in projections)
        summary.update(
            {
                "visibleComparisonProposalCount": int(material.get("visibleProposalCount") or len(projections)),
                "verifiedComparisonProposalCount": verified_count,
                "partialComparisonProposalCount": max(len(projections) - verified_count, 0),
                "adoptionReadyProposalCount": int(material.get("adoptionReadyProposalCount") or 0),
            }
        )
        self.db.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(summary, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
                portfolio_id,
            ),
        )

    @staticmethod
    def _capability_expected_base_version(capability: Optional[dict[str, Any]]) -> str:
        if not isinstance(capability, dict):
            return ""
        response = capability.get("response") if isinstance(capability.get("response"), dict) else {}
        for option in response.get("choiceOptions") or []:
            if isinstance(option, dict) and str(option.get("action") or "") == "select_plan_proposal":
                return str(option.get("expectedBaseVersionId") or "")
        return ""

    @staticmethod
    def _minimum_fingerprint(value: str) -> str:
        normalized = str(value or "").strip()
        return normalized if len(normalized) >= 16 else hashlib.sha256(normalized.encode()).hexdigest()

    @staticmethod
    def _json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return copy.deepcopy(value)
        try:
            loaded = json.loads(str(value or "{}"))
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _business_fingerprint(cls, value: dict[str, Any]) -> str:
        material = proposal_structural_signature_material(copy.deepcopy(value))
        for key in (
            "workflowMode",
            "comparisonRole",
            "originProjectionMode",
            "creativeBrief",
            "portfolioVerifier",
        ):
            material.pop(key, None)
        return cls._fingerprint(material)

    @classmethod
    def _materialize_unresolved_slots_as_pending_metadata(
        cls,
        snapshot: dict[str, Any],
        *,
        request_contract: dict[str, Any],
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Remove fake unresolved POIs and retain their complete slot contract.

        Simple Open's executor may return an unresolved PersistableSegmentPlan
        after its single, bounded AMap search is exhausted.  That plan is useful
        scheduling metadata, not a place.  Persisting it in ``days[].segments``
        invents a POI identity and makes the four genuinely grounded anchors
        disappear behind a semantic failure.  This server-only conversion keeps
        the unresolved requirement visible without weakening entity admission.
        """

        material = copy.deepcopy(snapshot)
        remaining_scope_slot_ids = {
            str(item.get("slotId") or "").strip()
            for item in remaining_query_scopes or []
            if isinstance(item, dict) and str(item.get("slotId") or "").strip()
        }
        required_by_intent: dict[str, list[dict[str, Any]]] = {}
        for requirement in request_contract.get("requiredIntents") or []:
            if not isinstance(requirement, dict):
                continue
            intent_type = str(requirement.get("intentType") or "").strip()
            if intent_type:
                required_by_intent.setdefault(intent_type, []).append(requirement)

        pending_by_id: dict[str, dict[str, Any]] = {}
        pending_order: list[str] = []
        for existing in material.get("portfolioPendingSlots") or []:
            if not isinstance(existing, dict):
                continue
            slot_id = str(existing.get("planningSlotId") or existing.get("slotId") or "").strip()
            if not slot_id:
                continue
            pending_by_id[slot_id] = copy.deepcopy(existing)
            pending_order.append(slot_id)

        pending_reclassified = False
        if remaining_query_scopes is not None:
            for slot_id, pending in pending_by_id.items():
                provider_exhausted = slot_id not in remaining_scope_slot_ids
                pending.update(
                    {
                        "reasonCode": (
                            "provider_candidates_exhausted_or_semantically_rejected"
                            if provider_exhausted
                            else "provider_candidate_frontier_remaining"
                        ),
                        "timingBasis": (
                            "simple_direction_provider_exhausted_slot"
                            if provider_exhausted
                            else "simple_direction_candidate_frontier_remaining"
                        ),
                        "simpleDirectionProviderExhausted": provider_exhausted,
                    }
                )
                pending_reclassified = True

        removed_segment_ids: set[str] = set()
        for day in material.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            retained: list[dict[str, Any]] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                grounding_status = str(metadata.get("groundingStatus") or "").strip()
                if grounding_status != "unresolved" or ProposalReadinessService._real_amap_poi(segment.get("poi")):
                    retained.append(segment)
                    continue

                segment_id = str(segment.get("id") or "").strip()
                slot_id = str(
                    metadata.get("planningSlotId")
                    or metadata.get("slotId")
                    or (f"simple_pending_{segment_id}" if segment_id else f"simple_pending_{uuid4().hex[:16]}")
                )
                intent_type = str(metadata.get("intentType") or segment.get("kind") or "visit").strip()
                intent_requirements = required_by_intent.get(intent_type) or []
                metadata_goal_id = str(metadata.get("goalId") or metadata.get("sourceGoalId") or "").strip()
                matching_requirements = [
                    requirement
                    for requirement in intent_requirements
                    if metadata_goal_id and str(requirement.get("goalId") or "").strip() == metadata_goal_id
                ]
                authoritative = (
                    matching_requirements[0]
                    if len(matching_requirements) == 1
                    else intent_requirements[0]
                    if not metadata_goal_id and len(intent_requirements) == 1
                    else {}
                )
                metadata_source_goal_id = str(metadata.get("sourceGoalId") or "").strip()
                metadata_occurrence_id = str(metadata.get("occurrenceId") or "").strip()
                metadata_lineage_authority = str(metadata.get("lineageAuthority") or "").strip()
                metadata_requirement_level = str(metadata.get("requirementLevel") or "").strip()
                requirement_lineage_conflict = bool(
                    metadata_lineage_authority != "simple_open_daily_completion_policy"
                    and (
                        (metadata_goal_id and intent_requirements and not matching_requirements)
                        or (not metadata_goal_id and len(intent_requirements) > 1)
                    )
                )
                sealed_occurrence_lineage = bool(
                    metadata_goal_id
                    and metadata_goal_id == metadata_source_goal_id
                    and metadata_occurrence_id == f"occ:{metadata_source_goal_id}:day:{day_number}"
                    and str(metadata.get("poolId") or "").strip()
                    and str(metadata.get("planningSlotId") or "").strip() == slot_id
                    and int(metadata.get("dayNumber") or 0) == day_number
                    and metadata_requirement_level
                    and metadata_lineage_authority
                    in {
                        "goal_occurrence_compiler",
                        "simple_open_request_contract_every_day_meal",
                        "simple_open_daily_completion_policy",
                    }
                )
                allowed_day_numbers: list[int] = []
                allowed_day_source = authoritative.get("allowedDayNumbers") or metadata.get("allowedDayNumbers")
                if isinstance(allowed_day_source, list):
                    try:
                        allowed_day_numbers = sorted(
                            {
                                int(value)
                                for value in allowed_day_source
                                if not isinstance(value, bool) and int(value) > 0
                            }
                        )
                    except (TypeError, ValueError):
                        allowed_day_numbers = []
                user_explicit = authoritative.get("userExplicit") is True
                distribution_policy = str(authoritative.get("distributionPolicy") or "").strip()
                cardinality_source = str(authoritative.get("cardinalitySource") or "").strip()
                completion_required = bool(
                    authoritative
                    and not requirement_lineage_conflict
                    and sealed_occurrence_lineage
                    and user_explicit
                    and distribution_policy == "every_allowed_day"
                    and cardinality_source == "explicit_every_day"
                    and day_number in allowed_day_numbers
                )
                requirement_level = str(
                    metadata_requirement_level
                    if sealed_occurrence_lineage
                    else authoritative.get("requirementLevel")
                    or ("required" if intent_requirements else metadata.get("requirementLevel") or "optional")
                )
                goal_id = str(metadata_goal_id or authoritative.get("goalId") or "").strip()
                display_need = cls._pending_display_need(intent_type)
                start_time = str(segment.get("startTime") or "").strip() or None
                end_time = str(segment.get("endTime") or "").strip() or None
                time_window = (
                    f"{start_time}-{end_time}"
                    if start_time and end_time
                    else str(segment.get("timeWindow") or "").strip() or None
                )
                source_reason_code = str(segment.get("reasonCode") or metadata.get("reasonCode") or "").strip()
                source_reason = str(
                    segment.get("reason")
                    or metadata.get("reason")
                    or segment.get("notes")
                    or "未找到通过地图身份与用途校验的安全候选"
                ).strip()
                provider_exhausted = slot_id not in remaining_scope_slot_ids
                pending = {
                    **copy.deepcopy(pending_by_id.get(slot_id) or {}),
                    "id": f"pending:{slot_id}",
                    "slotId": slot_id,
                    "planningSlotId": slot_id,
                    "poolId": metadata.get("poolId"),
                    "dayNumber": day_number,
                    "startTime": start_time or "",
                    "endTime": end_time or "",
                    "timeWindow": time_window or "",
                    "durationMinutes": int(segment.get("durationMinutes") or 0),
                    "intentType": intent_type,
                    "kind": str(segment.get("kind") or "visit"),
                    "displayNeed": display_need,
                    "rawNeed": display_need,
                    "label": f"待补：{display_need}",
                    "state": "pending",
                    "requirementLevel": requirement_level,
                    "required": requirement_level in {"hard", "required"},
                    "goalId": goal_id or None,
                    "sourceGoalId": (metadata_source_goal_id if sealed_occurrence_lineage else goal_id) or None,
                    "occurrenceId": metadata_occurrence_id or None,
                    "lineageAuthority": metadata_lineage_authority or None,
                    "userExplicit": user_explicit,
                    "allowedDayNumbers": allowed_day_numbers,
                    "distributionPolicy": distribution_policy or None,
                    "cardinalitySource": cardinality_source or None,
                    "completionRequired": completion_required,
                    "dayCompletionRequired": metadata.get("dayCompletionRequired") is True,
                    "schedulePreference": copy.deepcopy(metadata.get("schedulePreference") or {}),
                    "scheduleConstraints": copy.deepcopy(metadata.get("scheduleConstraints") or {}),
                    "scheduleDecision": copy.deepcopy(metadata.get("scheduleDecision") or {}),
                    "groundingStatus": "unresolved",
                    "reasonCode": (
                        "provider_candidates_exhausted_or_semantically_rejected"
                        if provider_exhausted
                        else "provider_candidate_frontier_remaining"
                    ),
                    "sourceReasonCode": source_reason_code or None,
                    "reason": source_reason,
                    "timingStatus": "awaiting_route_confirmation",
                    "timingBasis": (
                        "simple_direction_provider_exhausted_slot"
                        if provider_exhausted
                        else "simple_direction_candidate_frontier_remaining"
                    ),
                    "futureRouteAnchor": metadata.get("futureRouteAnchor") is True,
                    "routeAnchorExpected": metadata.get("routeAnchorExpected") is True,
                    "simpleDirectionProviderExhausted": provider_exhausted,
                    "simpleDirectionRequirementLineageConflict": requirement_lineage_conflict,
                    "requirementEvidenceSource": (
                        "lineage_conflict"
                        if requirement_lineage_conflict
                        else "request_intent_contract"
                        if authoritative
                        else "persistable_segment_plan"
                    ),
                }
                pending.pop("poi", None)
                pending_by_id[slot_id] = pending
                if slot_id not in pending_order:
                    pending_order.append(slot_id)
                if segment_id:
                    removed_segment_ids.add(segment_id)
            day["segments"] = retained

        if removed_segment_ids or pending_reclassified:
            material["portfolioPendingSlots"] = [
                pending_by_id[slot_id] for slot_id in pending_order if slot_id in pending_by_id
            ]
            material["status"] = "partial"
            for route_key in ("routeOptions", "portfolioRouteEvidence", "routeEvidence"):
                material[route_key] = [
                    route
                    for route in material.get(route_key) or []
                    if isinstance(route, dict)
                    and str(route.get("fromSegmentId") or route.get("from_segment_id") or "") not in removed_segment_ids
                    and str(route.get("toSegmentId") or route.get("to_segment_id") or "") not in removed_segment_ids
                ]
        return material

    @staticmethod
    def _pending_display_need(intent_type: str) -> str:
        return {
            "night_view": "夜景地点",
            "campus_visit": "高校地点",
            "meal": "当地特色餐饮",
            "park": "公园",
            "museum": "博物馆",
            "shopping": "购物地点",
            "area_walk": "街区漫步地点",
            "local_culture": "当地文化体验",
        }.get(str(intent_type or ""), "待补地点")

    @classmethod
    def _verified_anchors_by_day(cls, snapshot: dict[str, Any]) -> dict[int, int]:
        counts: dict[int, int] = {}
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            counts.setdefault(day_number, 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                intent_type = str(metadata.get("intentType") or segment.get("kind") or "")
                if (
                    metadata.get("routeAnchor") is True
                    and str(metadata.get("groundingStatus") or "") == "verified_amap"
                    and ProposalReadinessService._real_amap_poi(poi)
                    and SimpleOpenItineraryExecutor._candidate_type_matches_intent(poi, intent_type)
                ):
                    counts[day_number] = counts.get(day_number, 0) + 1
        return counts

    @classmethod
    def _verified_anchor_count(cls, snapshot: dict[str, Any]) -> int:
        return sum(cls._verified_anchors_by_day(snapshot).values())

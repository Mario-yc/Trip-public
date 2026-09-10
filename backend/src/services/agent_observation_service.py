"""Canonical, bounded state used by the autonomy loop.

This module deliberately derives its facts from the persisted/request snapshot and
planning diagnostics, never from assistant prose.  It has no provider calls and
does not mutate itinerary state.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.core.config import get_settings
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.agent_observation_fact_service import AgentObservationFactService
from src.services.timeline_target_binder import TimelineTargetBinder
from src.services.portfolio_pending_slot_service import (
    PortfolioPendingSlotError,
    PortfolioPendingSlotService,
)
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer


LifecycleState = Literal["empty_scaffold", "draft", "partial", "map_ready", "route_ready", "complete"]
DoorToDoorStatus = Literal["not_ready", "waiting_for_poi_grounding", "partial", "ready"]


class ObservationRequest(BaseModel):
    latest_message: str = Field(default="", alias="latestMessage")
    effective_message: str = Field(default="", alias="effectiveMessage")
    intent_contract: dict[str, Any] = Field(default_factory=dict, alias="intentContract")
    explicit_no_write: bool = Field(default=False, alias="explicitNoWrite")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ObservationItinerary(BaseModel):
    lifecycle_state: LifecycleState = Field(alias="lifecycleState")
    plan_id: Optional[str] = Field(default=None, alias="planId")
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    day_count: int = Field(default=0, alias="dayCount", ge=0)
    segment_count: int = Field(default=0, alias="segmentCount", ge=0)
    meaningful_segment_count: int = Field(default=0, alias="meaningfulSegmentCount", ge=0)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class UnresolvedSlot(BaseModel):
    slot_id: str = Field(alias="slotId", min_length=1)
    segment_id: Optional[str] = Field(default=None, alias="segmentId")
    day_number: Optional[int] = Field(default=None, alias="dayNumber")
    time_window: str = Field(default="", alias="timeWindow")
    reason: str = Field(min_length=1)
    next_action: str = Field(default="ask_user", alias="nextAction")
    candidate_count: int = Field(default=0, alias="candidateCount", ge=0)
    required: bool = True
    goal_id: Optional[str] = Field(default=None, alias="goalId")
    intent_type: str = Field(default="", alias="intentType")
    raw_need: str = Field(default="", alias="rawNeed")
    candidate_evidence: dict[str, Any] = Field(default_factory=dict, alias="candidateEvidence")
    source_attempt_turn_id: Optional[str] = Field(default=None, alias="sourceAttemptTurnId")
    brief_id: Optional[str] = Field(default=None, alias="briefId")
    pool_id: Optional[str] = Field(default=None, alias="poolId")
    planning_slot_id: Optional[str] = Field(default=None, alias="planningSlotId")
    requirement_level: str = Field(default="", alias="requirementLevel")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def reason_must_be_truthful(self) -> "UnresolvedSlot":
        if self.reason.strip().lower() == "ok":
            raise ValueError("unresolved_slot_reason_must_not_be_ok")
        return self


class RequirementCoverage(BaseModel):
    required: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_required_slot_count: int = Field(default=0, alias="unresolvedRequiredSlotCount", ge=0)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class CandidateState(BaseModel):
    pending_groups: list[dict[str, Any]] = Field(default_factory=list, alias="pendingGroups")
    selected_candidate_identity: Optional[str] = Field(default=None, alias="selectedCandidateIdentity")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PlanningAttemptState(BaseModel):
    persisted: bool = False
    source_assistant_turn_id: Optional[str] = Field(default=None, alias="sourceAssistantTurnId")
    result_state: str = Field(default="", alias="resultState")
    initial_plan_available: bool = Field(default=False, alias="initialPlanAvailable")
    unresolved_slot_count: int = Field(default=0, alias="unresolvedSlotCount", ge=0)
    next_actions: list[str] = Field(default_factory=list, alias="nextActions")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class MapState(BaseModel):
    confirmed_required_poi_count: int = Field(default=0, alias="confirmedRequiredPoiCount", ge=0)
    unconfirmed_required_poi_count: int = Field(default=0, alias="unconfirmedRequiredPoiCount", ge=0)
    confirmed_anchor_map_ready: bool = Field(default=False, alias="confirmedAnchorMapReady")
    complete_intent_map_ready: bool = Field(default=False, alias="completeIntentMapReady")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class RouteState(BaseModel):
    confirmed_anchor_required_legs: int = Field(default=0, alias="confirmedAnchorRequiredLegs", ge=0)
    confirmed_anchor_covered_legs: int = Field(default=0, alias="confirmedAnchorCoveredLegs", ge=0)
    confirmed_anchor_route_ready: bool = Field(default=False, alias="confirmedAnchorRouteReady")
    complete_door_to_door_status: DoorToDoorStatus = Field(default="not_ready", alias="completeDoorToDoorStatus")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ScheduleState(BaseModel):
    status: str = "not_created"
    overlap_count: int = Field(default=0, alias="overlapCount", ge=0)
    chronology_violation_count: int = Field(default=0, alias="chronologyViolationCount", ge=0)
    provisional_segment_count: int = Field(default=0, alias="provisionalSegmentCount", ge=0)
    genuine_free_gap_minutes: int = Field(default=0, alias="genuineFreeGapMinutes", ge=0)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class VersionLineage(BaseModel):
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId")
    current_version_id: Optional[str] = Field(default=None, alias="currentVersionId")
    versions_created_this_run: list[str] = Field(default_factory=list, alias="versionsCreatedThisRun")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class TargetInventory(BaseModel):
    day_ids: list[str] = Field(default_factory=list, alias="dayIds")
    day_numbers: list[int] = Field(default_factory=list, alias="dayNumbers")
    segment_ids: list[str] = Field(default_factory=list, alias="segmentIds")
    candidate_ids: list[str] = Field(default_factory=list, alias="candidateIds")
    amap_poi_ids: list[str] = Field(default_factory=list, alias="amapPoiIds")
    planning_slot_ids: list[str] = Field(default_factory=list, alias="planningSlotIds")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class SegmentRef(BaseModel):
    segment_id: str = Field(alias="segmentId")
    day_id: str = Field(default="", alias="dayId")
    day_number: int = Field(alias="dayNumber")
    segment_order: int = Field(default=0, alias="segmentOrder")
    start_time: str = Field(default="", alias="startTime")
    end_time: str = Field(default="", alias="endTime")
    kind: str = ""
    poi_name: str = Field(default="", alias="poiName")
    poi_amap_id: Optional[str] = Field(default=None, alias="poiAmapId")
    intent_type: str = Field(default="", alias="intentType")
    goal_id: Optional[str] = Field(default=None, alias="goalId")
    requirement_level: str = Field(default="", alias="requirementLevel")
    raw_need: str = Field(default="", alias="rawNeed")
    aliases: list[str] = Field(default_factory=list)
    required: bool = False
    route_anchor: bool = Field(default=False, alias="routeAnchor")
    user_locked: bool = Field(default=False, alias="userLocked")
    grounding_status: str = Field(default="", alias="groundingStatus")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class RuntimeBudget(BaseModel):
    remaining_run_ms: int = Field(default=0, alias="remainingRunMs", ge=0)
    remaining_cycles: int = Field(default=0, alias="remainingCycles", ge=0)
    remaining_tools: dict[str, int] = Field(default_factory=dict, alias="remainingTools")
    remaining_external_calls: dict[str, int] = Field(default_factory=dict, alias="remainingExternalCalls")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PlanPortfolioState(BaseModel):
    """A bounded projection only; proposal snapshots remain server-side evidence."""

    portfolio_id: Optional[str] = Field(default=None, alias="portfolioId")
    status: str = "none"
    proposal_count: int = Field(default=0, alias="proposalCount", ge=0, le=24)
    feasible_proposal_count: int = Field(default=0, alias="feasibleProposalCount", ge=0, le=24)
    visible_proposal_count: int = Field(default=0, alias="visibleProposalCount", ge=0, le=12)
    selected_proposal_id: Optional[str] = Field(default=None, alias="selectedProposalId")
    expected_base_version_id: Optional[str] = Field(default=None, alias="expectedBaseVersionId")
    source_user_turn_id: Optional[str] = Field(default=None, alias="sourceUserTurnId")
    request_contract_fingerprint: str = Field(default="", alias="requestContractFingerprint")
    proposal_summaries: list[dict[str, Any]] = Field(default_factory=list, alias="proposalSummaries", max_length=12)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PartialTimelineState(BaseModel):
    active: bool = False
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    planning_selection_root_turn_id: Optional[str] = Field(default=None, alias="planningSelectionRootTurnId")
    root_portfolio_id: Optional[str] = Field(default=None, alias="rootPortfolioId")
    focus_brief_id: Optional[str] = Field(default=None, alias="focusBriefId")
    pending_slot_count: int = Field(default=0, alias="pendingSlotCount", ge=0)
    slot_keys: list[dict[str, Any]] = Field(default_factory=list, alias="slotKeys")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentObservation(BaseModel):
    schema_version: Literal["agent-observation-v2"] = Field(default="agent-observation-v2", alias="schemaVersion")
    cycle_index: int = Field(default=0, alias="cycleIndex", ge=0)
    latest_message: str = Field(default="", alias="latestMessage")
    effective_message: str = Field(default="", alias="effectiveMessage")
    recent_turns: list[dict[str, Any]] = Field(default_factory=list, alias="recentTurns")
    segment_refs: list[SegmentRef] = Field(default_factory=list, alias="segmentRefs")
    request: ObservationRequest
    itinerary: ObservationItinerary
    requirement_coverage: RequirementCoverage = Field(alias="requirementCoverage")
    unresolved_slots: list[UnresolvedSlot] = Field(default_factory=list, alias="unresolvedSlots")
    candidate_state: CandidateState = Field(default_factory=CandidateState, alias="candidateState")
    planning_attempt: PlanningAttemptState = Field(default_factory=PlanningAttemptState, alias="planningAttempt")
    map_state: MapState = Field(default_factory=MapState, alias="mapState")
    route_state: RouteState = Field(default_factory=RouteState, alias="routeState")
    schedule_state: ScheduleState = Field(default_factory=ScheduleState, alias="scheduleState")
    online_facts: dict[str, Any] = Field(default_factory=dict, alias="onlineFacts")
    version_lineage: VersionLineage = Field(default_factory=VersionLineage, alias="versionLineage")
    target_inventory: TargetInventory = Field(default_factory=TargetInventory, alias="targetInventory")
    runtime_budget: RuntimeBudget = Field(default_factory=RuntimeBudget, alias="runtimeBudget")
    plan_portfolio: PlanPortfolioState = Field(default_factory=PlanPortfolioState, alias="planPortfolio")
    partial_timeline_state: PartialTimelineState = Field(
        default_factory=PartialTimelineState, alias="partialTimelineState"
    )
    last_action: Optional[dict[str, Any]] = Field(default=None, alias="lastAction")
    last_outcome: Optional[dict[str, Any]] = Field(default=None, alias="lastOutcome")
    state_fingerprint: str = Field(default="", alias="stateFingerprint")
    invariant_errors: list[str] = Field(default_factory=list, alias="invariantErrors")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentObservationInvariantValidator:
    def validate(self, observation: AgentObservation) -> list[str]:
        errors: list[str] = []
        unresolved = observation.requirement_coverage.unresolved_required_slot_count
        if unresolved != len([slot for slot in observation.unresolved_slots if slot.required]):
            errors.append("unresolved_required_slot_count_mismatch")
        if unresolved and observation.map_state.complete_intent_map_ready:
            errors.append("unresolved_required_slot_with_complete_map")
        if unresolved and observation.route_state.complete_door_to_door_status == "ready":
            errors.append("unresolved_required_slot_with_complete_route")
        if observation.route_state.confirmed_anchor_route_ready and (
            observation.route_state.confirmed_anchor_covered_legs
            < observation.route_state.confirmed_anchor_required_legs
        ):
            errors.append("confirmed_anchor_route_ready_without_full_coverage")
        if observation.itinerary.active_version_id != observation.version_lineage.current_version_id:
            errors.append("active_version_lineage_mismatch")
        if observation.itinerary.lifecycle_state == "empty_scaffold" and observation.itinerary.meaningful_segment_count:
            errors.append("empty_scaffold_has_meaningful_segments")
        return errors


class AgentObservationBuilder:
    """Builds a compact observation from context/snapshot only; no I/O providers."""

    MAX_UNRESOLVED = 12

    def __init__(self, validator: Optional[AgentObservationInvariantValidator] = None):
        self.validator = validator or AgentObservationInvariantValidator()
        self.intent_candidate_semantic_policy = IntentCandidateSemanticPolicy()

    def build(
        self,
        request_context: dict[str, Any],
        *,
        cycle_index: int = 0,
        last_action: Optional[dict[str, Any]] = None,
        last_outcome: Optional[dict[str, Any]] = None,
    ) -> AgentObservation:
        snapshot = request_context.get("currentItinerarySnapshot")
        if not isinstance(snapshot, dict):
            snapshot = (
                request_context.get("timelineContext")
                if isinstance(request_context.get("timelineContext"), dict)
                else {}
            )
        days = [day for day in snapshot.get("days") or [] if isinstance(day, dict)]
        segments = [segment for day in days for segment in day.get("segments") or [] if isinstance(segment, dict)]
        active_version = self._active_version(request_context, snapshot)
        meaningful = [segment for segment in segments if self._is_meaningful(segment)]
        raw_pending = [item for item in request_context.get("pendingAmapPoiCandidates") or [] if isinstance(item, dict)]
        target_inventory = TargetInventory(
            dayIds=[str(day.get("id")) for day in days if str(day.get("id") or "")],
            dayNumbers=[int(day.get("dayNumber")) for day in days if isinstance(day.get("dayNumber"), int)],
            segmentIds=[str(segment.get("id")) for segment in segments if str(segment.get("id") or "")],
            candidateIds=[str(item.get("id")) for item in raw_pending if str(item.get("id") or "")],
            amapPoiIds=[
                str(candidate.get("id") or candidate.get("amapId"))
                for item in raw_pending
                for candidate in item.get("candidates") or []
                if isinstance(candidate, dict) and str(candidate.get("id") or candidate.get("amapId") or "")
            ],
            planningSlotIds=[],
        )
        lifecycle = self._lifecycle(active_version, meaningful, segments)
        partial_snapshot, partial_slots = self._partial_pending_truth(snapshot)
        unresolved = self._partial_unresolved_slots(partial_slots, request_context)
        unresolved.extend(self._unresolved_slots(days, request_context))
        unresolved.extend(self._semantic_unresolved_slots(days, unresolved))
        attempt = self._planning_attempt(request_context)
        unresolved.extend(self._planning_attempt_unresolved_slots(attempt, request_context, unresolved))
        unresolved = unresolved[: self.MAX_UNRESOLVED]
        target_inventory.planning_slot_ids = list(
            dict.fromkeys(
                item.planning_slot_id or item.slot_id
                for item in unresolved
                if item.planning_slot_id or item.source_attempt_turn_id
            )
        )
        required_unresolved = [slot for slot in unresolved if slot.required]
        pending = self._pending_groups(request_context)
        required = self._goal_ledger(self._required_intents(request_context), segments)
        confirmed = [
            segment for segment in meaningful if self._is_confirmed(segment) and self._segment_semantic_valid(segment)
        ]
        anchors = [segment for segment in meaningful if self._is_route_anchor(segment)]
        required_legs, covered_legs = self._route_coverage(snapshot, days)
        route_ready = bool(required_legs) and covered_legs >= required_legs
        complete_status: DoorToDoorStatus = (
            "waiting_for_poi_grounding"
            if required_unresolved
            else "ready"
            if route_ready
            else "partial"
            if required_legs > 0
            else "not_ready"
        )
        observation = AgentObservation(
            cycleIndex=cycle_index,
            latestMessage=str(request_context.get("latestUserMessage") or ""),
            effectiveMessage=str(request_context.get("effectiveUserMessage") or ""),
            recentTurns=self._recent_turns(request_context),
            segmentRefs=self._segment_refs(snapshot, required),
            request=ObservationRequest(
                latestMessage=str(request_context.get("latestUserMessage") or ""),
                effectiveMessage=str(request_context.get("effectiveUserMessage") or ""),
                intentContract=self._bounded_dict(request_context.get("requestIntentContract")),
                explicitNoWrite=self._explicit_no_write(request_context),
            ),
            itinerary=ObservationItinerary(
                lifecycleState=lifecycle,
                planId=snapshot.get("id") or snapshot.get("planId") or request_context.get("activePlanId"),
                activeVersionId=active_version,
                dayCount=len(days),
                segmentCount=len(segments),
                meaningfulSegmentCount=len(meaningful),
            ),
            requirementCoverage=RequirementCoverage(
                required=required,
                unresolvedRequiredSlotCount=len(required_unresolved),
            ),
            unresolvedSlots=unresolved,
            candidateState=CandidateState(
                pendingGroups=pending,
                selectedCandidateIdentity=str(request_context.get("activePendingPoiCandidateId") or "") or None,
            ),
            planningAttempt=PlanningAttemptState(
                persisted=bool(attempt.get("enabled") and attempt.get("sourceAssistantTurnId")),
                sourceAssistantTurnId=str(attempt.get("sourceAssistantTurnId") or "") or None,
                resultState=str(attempt.get("resultState") or ""),
                initialPlanAvailable=isinstance(attempt.get("initialPlan"), dict),
                unresolvedSlotCount=len(
                    [
                        item
                        for item in attempt.get("unresolvedSlots") or []
                        if isinstance(item, dict) and item.get("slotId")
                    ]
                ),
                nextActions=[str(item) for item in attempt.get("nextActions") or [] if str(item)][:8],
            ),
            mapState=MapState(
                confirmedRequiredPoiCount=len(confirmed),
                unconfirmedRequiredPoiCount=len(required_unresolved),
                confirmedAnchorMapReady=bool(anchors)
                and len([segment for segment in anchors if self._is_confirmed(segment)]) == len(anchors),
                completeIntentMapReady=not required_unresolved and bool(meaningful),
            ),
            routeState=RouteState(
                confirmedAnchorRequiredLegs=required_legs,
                confirmedAnchorCoveredLegs=covered_legs,
                confirmedAnchorRouteReady=route_ready,
                completeDoorToDoorStatus=complete_status,
            ),
            scheduleState=self._schedule_state(days, segments, snapshot),
            onlineFacts=self._online_facts(snapshot),
            versionLineage=VersionLineage(
                baseVersionId=str(request_context.get("baseVersionId") or active_version or "") or None,
                currentVersionId=active_version,
                versionsCreatedThisRun=[
                    str(item) for item in request_context.get("autonomyVersionsCreated") or [] if str(item)
                ],
            ),
            targetInventory=target_inventory,
            runtimeBudget=self._runtime_budget(request_context),
            planPortfolio=self._portfolio_state(request_context),
            partialTimelineState=self._partial_timeline_state(partial_snapshot, partial_slots, active_version),
            lastAction=last_action,
            lastOutcome=last_outcome,
        )
        errors = self.validator.validate(observation)
        # Fingerprints represent persisted/user-visible state, not loop telemetry.
        # Otherwise cycleIndex and shrinking budgets make an unchanged state look
        # new and the no-progress guard can execute the same write repeatedly.
        canonical = observation.model_dump(
            by_alias=True,
            exclude={
                "state_fingerprint",
                "invariant_errors",
                "cycle_index",
                "runtime_budget",
                "last_action",
                "last_outcome",
            },
        )
        fingerprint = sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        return observation.model_copy(update={"state_fingerprint": fingerprint, "invariant_errors": errors})

    @staticmethod
    def _partial_pending_truth(
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(snapshot.get("portfolioPartialTimeline"), dict):
            return {}, []
        try:
            reconciled = PortfolioPendingSlotService.reconcile(snapshot)
        except PortfolioPendingSlotError:
            return {}, []
        return reconciled, [item for item in reconciled.get("portfolioPendingSlots") or [] if isinstance(item, dict)]

    def _partial_unresolved_slots(
        self,
        slots: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[UnresolvedSlot]:
        result: list[UnresolvedSlot] = []
        for raw in slots:
            planning_slot_id = str(raw.get("planningSlotId") or raw.get("slotId") or "")
            requirement_level = str(raw.get("requirementLevel") or "preferred")
            candidate_count = self._scoped_pending_candidate_count(context, raw)
            reason = str(raw.get("reason") or raw.get("state") or "pending")
            if reason.strip().casefold() in {"ok", "resolved", "covered", "completed"}:
                reason = "portfolio_pending_slot"
            result.append(
                UnresolvedSlot(
                    slotId=planning_slot_id,
                    segmentId=None,
                    dayNumber=int(raw.get("dayNumber") or 0) or None,
                    timeWindow=str(raw.get("timeWindow") or ""),
                    reason=reason,
                    nextAction="ask_user" if candidate_count else "resolve_poi",
                    candidateCount=candidate_count,
                    required=requirement_level in {"required", "hard", "hard_requirement"},
                    goalId=str(raw.get("sourceGoalId") or raw.get("goalId") or "") or None,
                    intentType=str(raw.get("intentType") or ""),
                    rawNeed=str(raw.get("rawNeed") or raw.get("displayNeed") or ""),
                    briefId=str(raw.get("briefId") or "") or None,
                    poolId=str(raw.get("poolId") or "") or None,
                    planningSlotId=planning_slot_id or None,
                    requirementLevel=requirement_level,
                )
            )
        return result

    @staticmethod
    def _scoped_pending_candidate_count(context: dict[str, Any], slot: dict[str, Any]) -> int:
        key = PortfolioPendingSlotService.slot_key(slot)
        return sum(
            len(item.get("candidates") or [])
            for item in context.get("pendingAmapPoiCandidates") or []
            if isinstance(item, dict)
            and (
                str(item.get("briefId") or ""),
                str(item.get("poolId") or ""),
                str(item.get("planningSlotId") or item.get("slotId") or ""),
                int(item.get("dayNumber") or 0),
            )
            == key
        )

    @staticmethod
    def _partial_timeline_state(
        snapshot: dict[str, Any],
        slots: list[dict[str, Any]],
        active_version_id: Optional[str],
    ) -> PartialTimelineState:
        if not snapshot:
            return PartialTimelineState()
        context = snapshot.get("portfolioSelectionContext") or {}
        return PartialTimelineState(
            active=True,
            activeVersionId=active_version_id,
            planningSelectionRootTurnId=str(context.get("planningSelectionRootTurnId") or "") or None,
            rootPortfolioId=str(context.get("rootPortfolioId") or "") or None,
            focusBriefId=str(context.get("focusBriefId") or "") or None,
            pendingSlotCount=len(slots),
            slotKeys=[
                {
                    "briefId": item.get("briefId"),
                    "poolId": item.get("poolId"),
                    "planningSlotId": item.get("planningSlotId"),
                    "dayNumber": item.get("dayNumber"),
                }
                for item in slots
            ],
        )

    @staticmethod
    def _recent_turns(context: dict[str, Any]) -> list[dict[str, Any]]:
        turns = (
            context.get("activeConversationTurns") if isinstance(context.get("activeConversationTurns"), list) else []
        )
        return [
            {
                "role": str(item.get("role") or ""),
                "content": str(item.get("content") or "")[:500],
                "turnIndex": item.get("turnIndex"),
                "itineraryVersionId": item.get("itineraryVersionId"),
            }
            for item in turns[-8:]
            if isinstance(item, dict)
        ]

    @staticmethod
    def _portfolio_state(context: dict[str, Any]) -> PlanPortfolioState:
        raw = context.get("planPortfolio") if isinstance(context.get("planPortfolio"), dict) else {}
        summaries = raw.get("proposalSummaries") if isinstance(raw.get("proposalSummaries"), list) else []
        visible_limit = get_settings().agent_creative_portfolio_max_visible_proposals
        return PlanPortfolioState(
            portfolioId=str(raw.get("portfolioId") or "") or None,
            status=str(raw.get("status") or "none"),
            proposalCount=min(24, max(0, int(raw.get("proposalCount") or 0))),
            feasibleProposalCount=min(24, max(0, int(raw.get("feasibleProposalCount") or 0))),
            visibleProposalCount=min(visible_limit, max(0, int(raw.get("visibleProposalCount") or 0))),
            selectedProposalId=str(raw.get("selectedProposalId") or "") or None,
            expectedBaseVersionId=str(raw.get("expectedBaseVersionId") or "") or None,
            sourceUserTurnId=str(raw.get("sourceUserTurnId") or "") or None,
            requestContractFingerprint=str(raw.get("requestContractFingerprint") or ""),
            proposalSummaries=[item for item in summaries[:visible_limit] if isinstance(item, dict)],
        )

    @staticmethod
    def _segment_refs(snapshot: dict[str, Any], required: list[dict[str, Any]]) -> list[SegmentRef]:
        goal_facts_by_segment: dict[str, dict[str, Any]] = {}
        for day in snapshot.get("days") or []:
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict) or not segment.get("id"):
                    continue
                metadata = segment.get("semanticMetadata")
                if isinstance(metadata, dict):
                    goal_facts_by_segment[str(segment["id"])] = metadata
        ledger_facts_by_segment: dict[str, dict[str, Any]] = {}
        for goal in required:
            goal_id = str(goal.get("goalId") or "").strip()
            if not goal_id:
                continue
            required_min = max(0, int(goal.get("requiredMin") or 0))
            satisfied_ids = [
                str(segment_id) for segment_id in goal.get("satisfiedBySegmentIds") or [] if str(segment_id or "")
            ]
            for segment_id in satisfied_ids[:required_min]:
                explicit = goal_facts_by_segment.get(segment_id) or {}
                has_authoritative_semantics = any(key in explicit for key in ("goalId", "requirementLevel"))
                if not has_authoritative_semantics:
                    ledger_facts_by_segment[segment_id] = goal
        result: list[SegmentRef] = []
        for item in TimelineTargetBinder.descriptors(snapshot)[:80]:
            if not item.segment_id:
                continue
            segment_facts = goal_facts_by_segment.get(item.segment_id) or {}
            intent_type = str(item.intent_type or segment_facts.get("intentType") or "")
            ledger_facts = ledger_facts_by_segment.get(item.segment_id) or {}
            requirement_level = str(segment_facts.get("requirementLevel") or ledger_facts.get("requirementLevel") or "")
            result.append(
                SegmentRef(
                    segmentId=item.segment_id,
                    dayId=item.day_id,
                    dayNumber=item.day_number,
                    segmentOrder=item.segment_order,
                    startTime=item.start_time,
                    endTime=item.end_time,
                    kind=item.kind,
                    poiName=item.poi_name,
                    poiAmapId=item.poi_amap_id,
                    intentType=intent_type,
                    goalId=segment_facts.get("goalId") or ledger_facts.get("goalId"),
                    requirementLevel=requirement_level,
                    rawNeed=item.raw_need,
                    aliases=list(item.aliases),
                    required=requirement_level in {"required", "hard", "hard_requirement"},
                    routeAnchor=item.route_anchor,
                    userLocked=item.user_locked,
                    groundingStatus=item.grounding_status,
                )
            )
        return result

    @staticmethod
    def _active_version(context: dict[str, Any], snapshot: dict[str, Any]) -> Optional[str]:
        value = snapshot.get("versionId") or snapshot.get("activeVersionId") or context.get("activeVersionId")
        return str(value) if value else None

    @staticmethod
    def _is_meaningful(segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        name = str(poi.get("name") or segment.get("title") or "").strip()
        if not name or name in {"待添加", "新地点", "未命名"}:
            return False
        return bool(segment.get("id") or poi.get("amapId") or poi.get("amap_id"))

    @staticmethod
    def _is_confirmed(segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        status = str(poi.get("groundingStatus") or poi.get("grounding_status") or "")
        return bool(poi.get("amapId") or poi.get("amap_id")) and status not in {
            "waiting_for_poi_grounding",
            "provider_rate_limited",
        }

    @staticmethod
    def _is_route_anchor(segment: dict[str, Any]) -> bool:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        metadata = segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {}
        semantic_metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
        return any(
            value is True
            for value in (
                segment.get("routeAnchor"),
                semantic_metadata.get("routeAnchor"),
                metadata.get("routeAnchor"),
                grounding.get("routeAnchor"),
                poi.get("routeable"),
            )
        )

    @staticmethod
    def _lifecycle(
        active_version: Optional[str], meaningful: list[dict[str, Any]], all_segments: list[dict[str, Any]]
    ) -> LifecycleState:
        if not active_version and not meaningful:
            return "empty_scaffold"
        if not meaningful:
            return "draft"
        if not active_version:
            return "draft"
        return "partial"

    def _unresolved_slots(self, days: list[dict[str, Any]], context: dict[str, Any]) -> list[UnresolvedSlot]:
        result: list[UnresolvedSlot] = []
        goals_by_intent: dict[str, dict[str, Any]] = {}
        ambiguous_intents: set[str] = set()
        for goal in self._required_intents(context):
            intent_type = str(goal.get("intentType") or "").strip()
            goal_id = str(goal.get("goalId") or "").strip()
            if not intent_type or not goal_id:
                continue
            existing = goals_by_intent.get(intent_type)
            if existing and str(existing.get("goalId") or "") != goal_id:
                ambiguous_intents.add(intent_type)
                continue
            goals_by_intent[intent_type] = goal
        for intent_type in ambiguous_intents:
            goals_by_intent.pop(intent_type, None)
        for day in days:
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                semantic_metadata = (
                    segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                )
                grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
                status = str(
                    semantic_metadata.get("groundingStatus")
                    or poi.get("groundingStatus")
                    or poi.get("grounding_status")
                    or grounding.get("status")
                    or ""
                )
                unresolved = status in {
                    "waiting_for_poi_grounding",
                    "provider_rate_limited",
                    "optional_waiting",
                    "draft_only",
                }
                if not unresolved:
                    continue
                raw_reason = str(semantic_metadata.get("unresolvedReason") or status or "waiting_for_poi_grounding")
                reason = "waiting_for_poi_grounding" if raw_reason.lower() == "ok" else raw_reason
                intent_type = self._segment_intent_type(segment)
                has_authoritative_semantics = any(key in semantic_metadata for key in ("goalId", "requirementLevel"))
                if has_authoritative_semantics:
                    goal_id = str(semantic_metadata.get("goalId") or "")
                    metadata_requirement_level = str(semantic_metadata.get("requirementLevel") or "")
                    required = (
                        bool(semantic_metadata["required"])
                        if "required" in semantic_metadata
                        else metadata_requirement_level in {"required", "hard", "hard_requirement"}
                    )
                else:
                    ledger_goal = goals_by_intent.get(intent_type) or {}
                    goal_id = str(ledger_goal.get("goalId") or "")
                    ledger_requirement_level = str(ledger_goal.get("requirementLevel") or "")
                    ledger_required_min = int(
                        ledger_goal.get("requiredMin")
                        or ledger_goal.get("minCount")
                        or ledger_goal.get("target")
                        or ledger_goal.get("requiredCount")
                        or 0
                    )
                    required = (
                        ledger_requirement_level not in {"soft_experience", "optional"}
                        and bool(ledger_goal)
                        and ledger_required_min > 0
                    )
                result.append(
                    UnresolvedSlot(
                        slotId=str(segment.get("slotId") or segment.get("id") or poi.get("id") or "unresolved"),
                        segmentId=str(segment.get("id") or "") or None,
                        dayNumber=day.get("dayNumber"),
                        timeWindow=self._time_window(segment),
                        reason=reason,
                        nextAction="ask_user"
                        if self._pending_candidate_count(context, segment.get("id"))
                        else "resolve_poi",
                        candidateCount=self._pending_candidate_count(context, segment.get("id")),
                        required=required,
                        goalId=goal_id or None,
                        intentType=intent_type,
                        rawNeed=str(semantic_metadata.get("rawNeed") or poi.get("name") or segment.get("title") or ""),
                    )
                )
        return result[: self.MAX_UNRESOLVED]

    @staticmethod
    def _planning_attempt(context: dict[str, Any]) -> dict[str, Any]:
        attempt = context.get("resumePlanningAttempt")
        return attempt if isinstance(attempt, dict) and attempt.get("enabled") else {}

    def _planning_attempt_unresolved_slots(
        self,
        attempt: dict[str, Any],
        context: dict[str, Any],
        existing: list[UnresolvedSlot],
    ) -> list[UnresolvedSlot]:
        """Expose persisted pre-version slots without inventing segment/version identity."""
        if not attempt or context.get("activeVersionId"):
            return []
        existing_ids = {item.slot_id for item in existing}
        contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        goals_by_intent = {
            str(item.get("intentType") or ""): item
            for item in contract.get("requiredIntents") or []
            if isinstance(item, dict) and str(item.get("intentType") or "")
        }
        result: list[UnresolvedSlot] = []
        for raw in attempt.get("unresolvedSlots") or []:
            if not isinstance(raw, dict):
                continue
            slot_id = str(raw.get("slotId") or "").strip()
            if not slot_id or slot_id in existing_ids:
                continue
            intent_type = str(raw.get("intentType") or "").strip()
            goal = goals_by_intent.get(intent_type, {})
            goal_id = str(goal.get("goalId") or "").strip()
            if not goal_id:
                pool_id = str(raw.get("poolId") or "").strip()
                goal_id = pool_id[:-5] if pool_id.endswith("_pool") else ""
            requirement_level = str(goal.get("requirementLevel") or "required")
            required_min = int(
                goal.get("requiredMin") or goal.get("minCount") or goal.get("target") or goal.get("requiredCount") or 0
            )
            reason = str(raw.get("reason") or raw.get("state") or "waiting_for_poi_grounding")
            if reason.lower() == "ok":
                reason = "waiting_for_poi_grounding"
            preview = raw.get("topCandidatePreview") if isinstance(raw.get("topCandidatePreview"), dict) else {}
            result.append(
                UnresolvedSlot(
                    slotId=slot_id,
                    segmentId=None,
                    dayNumber=raw.get("dayNumber"),
                    timeWindow=str(raw.get("timeWindow") or ""),
                    reason=reason,
                    nextAction="resolve_poi" if str(raw.get("nextAction") or "") != "ask_user" else "ask_user",
                    candidateCount=max(
                        0,
                        int(raw.get("candidateCount") or raw.get("candidateHintCount") or (1 if preview else 0)),
                    ),
                    required=requirement_level not in {"soft_experience", "optional"} and required_min > 0,
                    goalId=goal_id or None,
                    intentType=intent_type,
                    rawNeed=str(raw.get("rawNeed") or ""),
                    candidateEvidence={
                        key: preview.get(key)
                        for key in ("name", "type", "city", "canonicalEntity", "score", "decision", "threshold")
                        if preview.get(key) is not None
                    },
                    sourceAttemptTurnId=str(attempt.get("sourceAssistantTurnId") or "") or None,
                )
            )
            existing_ids.add(slot_id)
        return result

    @staticmethod
    def _pending_groups(context: dict[str, Any]) -> list[dict[str, Any]]:
        groups = []
        for item in context.get("pendingAmapPoiCandidates") or []:
            if not isinstance(item, dict):
                continue
            safe_candidates = []
            for candidate in item.get("candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                candidate_id = candidate.get("id") or candidate.get("amapId")
                if not candidate_id:
                    continue
                safe_candidates.append(
                    {
                        "id": candidate_id,
                        "name": candidate.get("name"),
                        "district": candidate.get("district"),
                        "category": candidate.get("category"),
                        "confidence": candidate.get("confidence"),
                        "longitude": candidate.get("longitude"),
                        "latitude": candidate.get("latitude"),
                        "providerType": candidate.get("providerType") or candidate.get("type"),
                        "sourceGoalId": candidate.get("sourceGoalId"),
                    }
                )
            source_goal_ids = {
                str(candidate.get("sourceGoalId") or "")
                for candidate in item.get("candidates") or []
                if isinstance(candidate, dict) and str(candidate.get("sourceGoalId") or "")
            }
            groups.append(
                {
                    "id": item.get("id"),
                    "query": item.get("query"),
                    "category": item.get("category"),
                    "sourceGoalId": next(iter(source_goal_ids)) if len(source_goal_ids) == 1 else None,
                    "sourceSegmentId": item.get("sourceSegmentId"),
                    "status": item.get("status"),
                    "selectedAmapId": item.get("selectedAmapId"),
                    "candidateCount": len(item.get("candidates") or []),
                    "safeCandidates": safe_candidates[:5],
                }
            )
        return groups[:8]

    @staticmethod
    def _pending_candidate_count(context: dict[str, Any], segment_id: Any) -> int:
        return sum(
            len(item.get("candidates") or [])
            for item in context.get("pendingAmapPoiCandidates") or []
            if isinstance(item, dict) and str(item.get("sourceSegmentId") or "") == str(segment_id or "")
        )

    @staticmethod
    def _required_intents(context: dict[str, Any]) -> list[dict[str, Any]]:
        contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        return [item for item in contract.get("requiredIntents") or [] if isinstance(item, dict)][:12]

    def _goal_ledger(self, required: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for item in required:
            intent_type = str(item.get("intentType") or "")
            required_min_value = item.get("requiredMin")
            if required_min_value is None:
                required_min_value = item.get("minCount")
            if required_min_value is None:
                required_min_value = item.get("target")
            if required_min_value is None:
                required_min_value = item.get("requiredCount")
            target = max(
                0,
                int(required_min_value or 0),
            )
            satisfied: list[str] = []
            invalid: list[dict[str, Any]] = []
            for segment in segments:
                if self._segment_intent_type(segment) != intent_type:
                    continue
                segment_id = str(segment.get("id") or "")
                if self._is_confirmed(segment) and self._segment_semantic_valid(segment):
                    satisfied.append(segment_id)
                elif self._is_confirmed(segment):
                    poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                    semantic = self.intent_candidate_semantic_policy.evaluate(intent_type, poi)
                    invalid.append(
                        {
                            "segmentId": segment_id,
                            "intentType": intent_type,
                            "poiName": str(poi.get("name") or ""),
                            **semantic.to_camel_dict(),
                        }
                    )
            count = len(satisfied)
            entries.append(
                {
                    **item,
                    "requiredMin": target,
                    "satisfiedCount": count,
                    "status": (
                        "covered"
                        if target > 0 and count >= target
                        else "optional_pending"
                        if target == 0
                        else "unresolved"
                    ),
                    "satisfiedBySegmentIds": satisfied,
                    "invalidClaims": invalid,
                    "nextSafeActions": [] if target > 0 and count >= target else ["resolve_poi", "ask_user"],
                }
            )
        return entries

    def _semantic_unresolved_slots(
        self,
        days: list[dict[str, Any]],
        existing: list[UnresolvedSlot],
    ) -> list[UnresolvedSlot]:
        existing_ids = {slot.segment_id for slot in existing if slot.segment_id}
        result: list[UnresolvedSlot] = []
        for day in days:
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                segment_id = str(segment.get("id") or "")
                intent_type = self._segment_intent_type(segment)
                if not segment_id or not intent_type or segment_id in existing_ids:
                    continue
                if not self._is_confirmed(segment) or self._segment_semantic_valid(segment):
                    continue
                result.append(
                    UnresolvedSlot(
                        slotId=segment_id,
                        segmentId=segment_id,
                        dayNumber=day.get("dayNumber"),
                        timeWindow=self._time_window(segment),
                        reason=f"{intent_type}_semantic_mismatch",
                        nextAction="resolve_poi",
                        candidateCount=0,
                        required=True,
                    )
                )
        return result

    def _segment_semantic_valid(self, segment: dict[str, Any]) -> bool:
        intent_type = self._segment_intent_type(segment)
        if not intent_type:
            return True
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return self.intent_candidate_semantic_policy.evaluate(intent_type, poi).passed

    @staticmethod
    def _segment_intent_type(segment: dict[str, Any]) -> str:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        semantic_metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        grounding = poi.get("grounding") if isinstance(poi.get("grounding"), dict) else {}
        for value in (
            semantic_metadata.get("intentType"),
            segment.get("intentType"),
            poi.get("intentType"),
            grounding.get("intentType"),
        ):
            if str(value or "").strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _route_coverage(snapshot: dict[str, Any], days: list[dict[str, Any]]) -> tuple[int, int]:
        routes = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
        covered_pairs = {
            (str(item.get("fromSegmentId") or ""), str(item.get("toSegmentId") or ""))
            for item in routes
            if ProposalRouteEvidenceNormalizer.is_verified(item)
            and item.get("fromSegmentId")
            and item.get("toSegmentId")
        }
        required = 0
        covered = 0
        for day in days:
            anchors = [
                segment
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
                and AgentObservationBuilder._is_meaningful(segment)
                and AgentObservationBuilder._is_route_anchor(segment)
            ]
            required += max(0, len(anchors) - 1)
            covered += sum(
                1
                for left, right in zip(anchors, anchors[1:])
                if (str(left.get("id") or ""), str(right.get("id") or "")) in covered_pairs
            )
        return required, covered

    @staticmethod
    def _schedule_state(
        days: list[dict[str, Any]], segments: list[dict[str, Any]], snapshot: dict[str, Any]
    ) -> ScheduleState:
        schedule_segments: list[dict[str, Any]] = []
        provisional = 0
        for day in days:
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                schedule_segments.append({**segment, "dayNumber": day.get("dayNumber")})
        for segment in schedule_segments:
            start = AgentObservationBuilder._minutes(segment.get("startTime"))
            end = AgentObservationBuilder._minutes(segment.get("endTime"))
            if start is None or end is None or end <= start:
                continue
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            if not AgentObservationBuilder._is_confirmed(segment) and str(
                segment.get("kind") or poi.get("category") or ""
            ) not in {"meal", "rest"}:
                provisional += 1
        facts = AgentObservationFactService().schedule_facts(schedule_segments)
        overlap = facts.overlap_count
        chronology = facts.invalid_interval_count
        route_minutes_by_day = AgentObservationBuilder._selected_route_minutes_by_day(days, snapshot)
        free = 0
        for day in days:
            day_intervals = []
            for segment in day.get("segments") or []:
                start = (
                    AgentObservationBuilder._minutes(segment.get("startTime")) if isinstance(segment, dict) else None
                )
                end = AgentObservationBuilder._minutes(segment.get("endTime")) if isinstance(segment, dict) else None
                if start is not None and end is not None and end > start:
                    day_intervals.append((start, end))
            if day_intervals:
                day_intervals.sort()
                raw_gap = (
                    day_intervals[-1][1]
                    - day_intervals[0][0]
                    - sum(end - start for start, end in AgentObservationBuilder._merge(day_intervals))
                )
                free += max(0, raw_gap - route_minutes_by_day.get(str(day.get("id") or ""), 0))
        return ScheduleState(
            status="valid"
            if schedule_segments and not overlap and not chronology
            else "partial"
            if schedule_segments
            else "not_created",
            overlapCount=overlap,
            chronologyViolationCount=chronology,
            provisionalSegmentCount=provisional,
            genuineFreeGapMinutes=free,
        )

    @staticmethod
    def _selected_route_minutes_by_day(days: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, int]:
        """Deduct only persisted selected legs whose endpoints belong to one day.

        A route is transit time, not user free time.  Unselected alternatives and
        unresolved/cross-day legs are intentionally excluded so this remains a
        conservative, snapshot-derived metric rather than a guessed schedule.
        """
        segment_day = {
            str(segment.get("id")): str(day.get("id") or "")
            for day in days
            for segment in day.get("segments") or []
            if isinstance(segment, dict) and str(segment.get("id") or "")
        }
        total: dict[str, int] = {}
        for route in ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot):
            if not route.get("isSelected") or not ProposalRouteEvidenceNormalizer.is_verified(route):
                continue
            from_day = segment_day.get(str(route.get("fromSegmentId") or ""))
            to_day = segment_day.get(str(route.get("toSegmentId") or ""))
            if not from_day or from_day != to_day:
                continue
            seconds = route.get("durationSeconds")
            minutes = route.get("durationMinutes")
            try:
                duration = (int(seconds) + 59) // 60 if seconds is not None else int(minutes or 0)
            except (TypeError, ValueError):
                duration = 0
            total[from_day] = total.get(from_day, 0) + max(0, duration)
        return total

    @staticmethod
    def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        return merged

    @staticmethod
    def _minutes(value: Any) -> Optional[int]:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or ""))
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        return hour * 60 + minute if hour < 24 and minute < 60 else None

    @staticmethod
    def _time_window(segment: dict[str, Any]) -> str:
        return f"{segment.get('startTime') or ''}-{segment.get('endTime') or ''}".strip("-")

    @staticmethod
    def _bounded_dict(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): item
            for key, item in list(value.items())[:24]
            if key not in {"providerDebug", "snapshot", "photos"}
        }

    @staticmethod
    def _online_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "weather": {"count": len(snapshot.get("weatherSignals") or [])},
            "risk": {"count": len(snapshot.get("trafficCrowdingSignals") or [])},
            "reservation": {"count": len(snapshot.get("ticketLookupResults") or [])},
        }

    @staticmethod
    def _runtime_budget(context: dict[str, Any]) -> RuntimeBudget:
        limits = context.get("runtimeLimits") if isinstance(context.get("runtimeLimits"), dict) else {}
        return RuntimeBudget(
            remainingRunMs=max(0, int(limits.get("remainingRunMs") or 0)),
            remainingCycles=max(0, int(limits.get("remainingCycles") or 0)),
            remainingTools={
                str(k): int(v) for k, v in (limits.get("remainingTools") or {}).items() if isinstance(v, int)
            },
            remainingExternalCalls={
                str(k): int(v) for k, v in (limits.get("remainingExternalCalls") or {}).items() if isinstance(v, int)
            },
        )

    @staticmethod
    def _explicit_no_write(context: dict[str, Any]) -> bool:
        message = str(context.get("effectiveUserMessage") or context.get("latestUserMessage") or "")
        return bool(re.search(r"(不要改|先不要改|先不改|别改|不修改)", message))

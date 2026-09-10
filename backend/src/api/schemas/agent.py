import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError, model_validator

from src.api.schemas.itineraries import ItineraryPlanResponse
from src.api.schemas.itinerary_patches import ItineraryPatchOperation, ItineraryVersionResponse
from src.api.schemas.maps import MapPoiResolveQueryRequest
from src.api.schemas.planning import PlanningRunResponse
from src.api.schemas.preferences import PreferenceMemoryResponse


class AgentPlanningEventResponse(BaseModel):
    type: str
    label: str
    status: str
    detail: str = ""
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    turn_id: Optional[str] = Field(default=None, alias="turnId")
    provider_name: Optional[str] = Field(default=None, alias="providerName")
    tool_provider: Optional[str] = Field(default=None, alias="toolProvider")
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    duration_ms: int = Field(default=0, alias="durationMs")
    sequence: int = 0
    category: str = "internal"
    goal: str = ""
    action_label: str = Field(default="", alias="actionLabel")
    input_summary: str = Field(default="", alias="inputSummary")
    result_summary: str = Field(default="", alias="resultSummary")
    decision_summary: str = Field(default="", alias="decisionSummary")
    effect_summary: str = Field(default="", alias="effectSummary")
    user_visible: bool = Field(default=False, alias="userVisible")
    metadata: dict = Field(default_factory=dict)
    timestamp: str

    model_config = {"populate_by_name": True}


class AgentReasoningStatusResponse(BaseModel):
    message_type: Literal["reasoning_status"] = Field(default="reasoning_status", alias="messageType")
    id: str
    sequence: int
    run_id: str = Field(default="", alias="runId")
    semantic_key: str = Field(default="", alias="semanticKey")
    phase: Literal[
        "understanding",
        "context",
        "constraints",
        "planning",
        "tool",
        "verification",
        "finalizing",
        "places",
        "feasibility",
        "result",
    ]
    status: Literal["running", "completed", "fallback", "failed", "cancelled"]
    summary: str
    detail: Optional[str] = None
    source_event_type: str = Field(alias="sourceEventType")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    turn_id: Optional[str] = Field(default=None, alias="turnId")
    root_user_turn_id: Optional[str] = Field(default=None, alias="rootUserTurnId")
    assistant_turn_id: Optional[str] = Field(default=None, alias="assistantTurnId")
    started_at: str = Field(default="", alias="startedAt")
    completed_at: Optional[str] = Field(default=None, alias="completedAt")
    elapsed_ms: int = Field(default=0, alias="elapsedMs", ge=0)
    first_sequence: int = Field(default=0, alias="firstSequence", ge=0)
    latest_sequence: int = Field(default=0, alias="latestSequence", ge=0)
    worker_count: Optional[int] = Field(default=None, alias="workerCount", ge=1)
    completed_worker_count: Optional[int] = Field(default=None, alias="completedWorkerCount", ge=0)
    timestamp: str

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentReasoningStatusSnapshotResponse(BaseModel):
    session_id: str = Field(alias="sessionId")
    source_user_turn_id: Optional[str] = Field(default=None, alias="sourceUserTurnId")
    assistant_turn_id: Optional[str] = Field(default=None, alias="assistantTurnId")
    statuses: list[AgentReasoningStatusResponse] = Field(default_factory=list)
    active: bool = False
    next_sequence: int = Field(default=0, alias="nextSequence", ge=0)
    terminal_status: Optional[Literal["completed", "failed", "cancelled"]] = Field(
        default=None,
        alias="terminalStatus",
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentSessionCreateRequest(BaseModel):
    city: str = "北京"
    title: Optional[str] = None
    preference_card_id: Optional[str] = Field(default=None, alias="preferenceCardId")

    model_config = {"populate_by_name": True}


class ComparisonSummaryProjection(BaseModel):
    adoption_ready_count: StrictInt = Field(alias="adoptionReadyCount", ge=0)
    repairable_partial_count: StrictInt = Field(alias="repairablePartialCount", ge=0)
    remaining_qualified_entity_count: StrictInt = Field(alias="remainingQualifiedEntityCount", ge=0)
    remaining_poi_page_count: StrictInt = Field(alias="remainingPoiPageCount", ge=0)
    frontier_status: Literal[
        "has_more",
        "provider_pending",
        "qualification_exhausted",
        "poi_exhausted",
        "route_feasible_exhausted",
    ] = Field(alias="frontierStatus")
    explored_qualified_entity_count: StrictInt = Field(alias="exploredQualifiedEntityCount", ge=0)
    attempted_poi_page_count: StrictInt = Field(alias="attemptedPoiPageCount", ge=0)
    last_outcome_reason: Optional[StrictStr] = Field(default=None, alias="lastOutcomeReason")
    blocking_layer: Optional[Literal["provider", "qualification", "poi", "route"]] = Field(
        default=None,
        alias="blockingLayer",
    )

    model_config = {"populate_by_name": True, "extra": "forbid", "strict": True}


def project_comparison_summary(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    try:
        validated = ComparisonSummaryProjection.model_validate(value)
    except ValidationError:
        return None
    return validated.model_dump(by_alias=True)


def project_persisted_identity(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


class ConversationTurnResponse(BaseModel):
    id: str
    role: str
    content: str
    turn_index: int = Field(alias="turnIndex")
    status: str
    parent_turn_id: Optional[str] = Field(default=None, alias="parentTurnId")
    itinerary_version_id: Optional[str] = Field(default=None, alias="itineraryVersionId")
    planning_run_id: Optional[str] = Field(default=None, alias="planningRunId")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    comparison_projections: list[dict] = Field(default_factory=list, alias="comparisonProjections")
    comparison_projection_update_mode: Optional[Literal["append", "replace"]] = Field(
        default=None,
        alias="comparisonProjectionUpdateMode",
    )
    planning_selection_root_turn_id: Optional[str] = Field(
        default=None,
        alias="planningSelectionRootTurnId",
    )
    root_portfolio_id: Optional[str] = Field(default=None, alias="rootPortfolioId")
    comparison_summary: Optional[dict] = Field(default=None, alias="comparisonSummary")
    choice_options: list[dict] = Field(default_factory=list, alias="choiceOptions")
    clarification_checkpoint: Optional[dict] = Field(default=None, alias="clarificationCheckpoint")
    clarification_submission: Optional[dict] = Field(default=None, alias="clarificationSubmission")
    guide_advice: Optional[dict] = Field(default=None, alias="guideAdvice")
    shared_source: Optional[dict] = Field(default=None, alias="sharedSource")
    experience_specs: list[dict] = Field(default_factory=list, alias="experienceSpecs")
    candidate_gap_summary: Optional[dict] = Field(default=None, alias="candidateGapSummary")
    spatial_boundary_preview: Optional[dict] = Field(default=None, alias="spatialBoundaryPreview")
    local_poi_options: Optional[dict] = Field(default=None, alias="localPoiOptions")
    structured_choice_trace: Optional[dict] = Field(default=None, alias="structuredChoiceTrace")
    planning_direction_count: Optional[int] = Field(default=None, alias="planningDirectionCount")
    verified_comparison_proposal_count: Optional[int] = Field(
        default=None,
        alias="verifiedComparisonProposalCount",
    )
    visible_comparison_proposal_count: Optional[int] = Field(
        default=None,
        alias="visibleComparisonProposalCount",
    )
    partial_comparison_proposal_count: Optional[int] = Field(
        default=None,
        alias="partialComparisonProposalCount",
    )
    adoption_ready_proposal_count: Optional[int] = Field(
        default=None,
        alias="adoptionReadyProposalCount",
    )
    timeline_mutation_outcome: Optional[dict] = Field(default=None, alias="timelineMutationOutcome")
    timeline_mutation_transaction: Optional[dict] = Field(default=None, alias="timelineMutationTransaction")
    planning_steps: list[AgentPlanningEventResponse] = Field(default_factory=list, alias="planningSteps")
    tool_events: list[AgentPlanningEventResponse] = Field(default_factory=list, alias="toolEvents")
    reasoning_statuses: list[AgentReasoningStatusResponse] = Field(default_factory=list, alias="reasoningStatuses")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")

    model_config = {"populate_by_name": True}


class PendingPoiCandidateResponse(BaseModel):
    id: str
    query: str
    city: str
    category: str
    status: str
    candidates: list[dict]
    selected_amap_id: Optional[str] = Field(default=None, alias="selectedAmapId")
    source_segment_id: Optional[str] = Field(default=None, alias="sourceSegmentId")
    created_at: str = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class AgentSessionResponse(BaseModel):
    session_id: str = Field(alias="sessionId")
    status: str
    city: str
    title: str
    active_plan_id: str = Field(alias="activePlanId")
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    turns: list[ConversationTurnResponse] = Field(default_factory=list)
    itinerary: Optional[ItineraryPlanResponse] = None
    pending_poi_candidates: list[PendingPoiCandidateResponse] = Field(
        default_factory=list, alias="pendingPoiCandidates"
    )
    preference_memory: Optional[PreferenceMemoryResponse] = Field(default=None, alias="preferenceMemory")
    planning_run: Optional[PlanningRunResponse] = Field(default=None, alias="planningRun")

    model_config = {"populate_by_name": True}


class AgentChoiceOptionResponse(BaseModel):
    id: Optional[str] = None
    index: Optional[int] = None
    kind: str = "preset"
    action: Optional[
        Literal[
            "retry_model_planning",
            "confirm_rule_safe_draft",
            "manual_continuation",
            "continue_plan_expansion",
            "search_travel_guide_advice",
            "select_plan_proposal",
            "adopt_active_partial",
            "resume_density_candidate",
            "refresh_density_candidates",
            "expand_density_nearby",
            "open_density_map",
            "continue_clarification",
            "submit_clarification_batch",
            "select_spatial_boundary_candidate",
            "confirm_spatial_boundary",
            "change_spatial_boundary",
            "retry_spatial_grounding",
        ]
    ] = None
    label: str = ""
    value: str = ""
    allows_manual_input: bool = Field(default=False, alias="allowsManualInput")
    source_decision_id: Optional[str] = Field(default=None, alias="sourceDecisionId")
    source_observation_fingerprint: Optional[str] = Field(default=None, alias="sourceObservationFingerprint")
    source_user_turn_id: Optional[str] = Field(default=None, alias="sourceUserTurnId")
    expected_base_version_id: Optional[str] = Field(default=None, alias="expectedBaseVersionId")
    request_intent_contract_fingerprint: Optional[str] = Field(default=None, alias="requestIntentContractFingerprint")
    attempt: int = 1

    model_config = {"populate_by_name": True, "extra": "allow"}


class ClarificationBatchSelectionRequest(BaseModel):
    dimension_id: str = Field(alias="dimensionId", min_length=1)
    option_id: Optional[str] = Field(default=None, alias="optionId", min_length=1)
    manual_value: Optional[str] = Field(default=None, alias="manualValue", min_length=1, max_length=800)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def require_exactly_one_answer_source(self) -> "ClarificationBatchSelectionRequest":
        if bool(self.option_id) == bool(self.manual_value):
            raise ValueError("clarification_batch_selection_requires_exactly_one_answer_source")
        return self


class SelectedAgentChoiceRequest(BaseModel):
    source_assistant_turn_id: str = Field(alias="sourceAssistantTurnId")
    choice_id: str = Field(alias="choiceId")
    manual_value: Optional[str] = Field(default=None, alias="manualValue")
    batch_selections: list[ClarificationBatchSelectionRequest] = Field(
        default_factory=list,
        alias="batchSelections",
        max_length=3,
    )

    model_config = {"populate_by_name": True}


class AgentSessionSummaryResponse(BaseModel):
    session_id: str = Field(alias="sessionId")
    status: str
    city: str
    title: str
    active_plan_id: str = Field(alias="activePlanId")
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    turn_count: int = Field(default=0, alias="turnCount")
    updated_at: str = Field(alias="updatedAt")
    created_at: str = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class AgentSessionListResponse(BaseModel):
    sessions: list[AgentSessionSummaryResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class AgentDirectionSaveActiveRequest(BaseModel):
    base_version_id: str = Field(alias="baseVersionId")
    planning_selection_root_turn_id: str = Field(alias="planningSelectionRootTurnId")
    root_portfolio_id: str = Field(alias="rootPortfolioId")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentDirectionSaveActiveResponse(BaseModel):
    proposal_id: str = Field(alias="proposalId")
    active_version_id: str = Field(alias="activeVersionId")
    comparison_projection: dict = Field(alias="comparisonProjection")
    saved: bool
    unchanged: bool

    model_config = {"populate_by_name": True}


class AgentViewEditingProposal(BaseModel):
    planning_selection_root_turn_id: str = Field(alias="planningSelectionRootTurnId")
    root_portfolio_id: str = Field(alias="rootPortfolioId")
    proposal_id: str = Field(alias="proposalId")
    source_assistant_turn_id: str = Field(alias="sourceAssistantTurnId")
    active_version_id: str = Field(alias="activeVersionId")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentViewFocusedProposal(BaseModel):
    planning_selection_root_turn_id: str = Field(alias="planningSelectionRootTurnId")
    root_portfolio_id: str = Field(alias="rootPortfolioId")
    proposal_id: str = Field(alias="proposalId")
    source_assistant_turn_id: str = Field(alias="sourceAssistantTurnId")
    material_fingerprint: str = Field(alias="materialFingerprint")
    repair_choice_id: str = Field(alias="repairChoiceId")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentViewContext(BaseModel):
    schema_version: Literal["agent-view-context-v1"] = Field(alias="schemaVersion")
    active_view: Literal["comparison", "overview"] = Field(alias="activeView")
    editing_proposal: Optional[AgentViewEditingProposal] = Field(default=None, alias="editingProposal")
    focused_proposal: Optional[AgentViewFocusedProposal] = Field(default=None, alias="focusedProposal")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentMessageContext(BaseModel):
    selected_map_poi_id: Optional[str] = Field(default=None, alias="selectedMapPoiId")
    selected_day_number: Optional[int] = Field(default=None, alias="selectedDayNumber")
    selected_segment_id: Optional[str] = Field(default=None, alias="selectedSegmentId")
    candidate_poi_ids: list[str] = Field(default_factory=list, alias="candidatePoiIds")
    preference_card_id: Optional[str] = Field(default=None, alias="preferenceCardId")
    selected_map_poi: Optional[dict] = Field(default=None, alias="selectedMapPoi")
    candidate_map_pois: list[dict] = Field(default_factory=list, alias="candidateMapPois")
    preference_card: Optional[dict] = Field(default=None, alias="preferenceCard")
    timeline_context: Optional[dict] = Field(default=None, alias="timelineContext")
    selected_agent_choice: Optional[SelectedAgentChoiceRequest] = Field(default=None, alias="selectedAgentChoice")
    view_context: Optional[AgentViewContext] = Field(default=None, alias="viewContext")

    model_config = {"populate_by_name": True, "extra": "allow"}


class AgentMessageRequest(BaseModel):
    content: str
    request_id: Optional[str] = Field(default=None, alias="requestId", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    agent_model: Optional[str] = Field(default=None, alias="agentModel")
    context: AgentMessageContext = Field(default_factory=AgentMessageContext)

    model_config = {"populate_by_name": True}


class AgentMessageEditRequest(BaseModel):
    content: str
    regenerate: bool = True
    agent_model: Optional[str] = Field(default=None, alias="agentModel")
    context: AgentMessageContext = Field(default_factory=AgentMessageContext)

    model_config = {"populate_by_name": True}


class AgentFullItinerarySegment(BaseModel):
    poi_name: str = Field(alias="poiName")
    category: str = "scenic"
    kind: str = "visit"
    start_time: str = Field(alias="startTime")
    end_time: Optional[str] = Field(default=None, alias="endTime")
    duration_minutes: int = Field(default=90, alias="durationMinutes")
    notes: str = ""
    transport_mode: str = Field(default="walk", alias="transportMode")
    estimated_cost: float = Field(default=0, alias="estimatedCost")

    model_config = {"populate_by_name": True}


class AgentFullItineraryDay(BaseModel):
    day_number: int = Field(alias="dayNumber")
    title: str
    date: Optional[str] = None
    segments: list[AgentFullItinerarySegment]

    model_config = {"populate_by_name": True}


class AgentFullItinerary(BaseModel):
    title: str
    city: Optional[str] = None
    days: list[AgentFullItineraryDay]

    model_config = {"populate_by_name": True}


class AgentStructuredOutput(BaseModel):
    reply: str
    mode: str
    operations: list[ItineraryPatchOperation] = Field(default_factory=list)
    full_itinerary: Optional[AgentFullItinerary] = Field(default=None, alias="fullItinerary")
    poi_resolution_requests: list[MapPoiResolveQueryRequest] = Field(
        default_factory=list, alias="poiResolutionRequests"
    )
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class AgentInitialDaySlot(BaseModel):
    slot_id: Optional[str] = Field(default=None, alias="slotId")
    day_number: int = Field(alias="dayNumber")
    date: Optional[str] = None
    time_window: str = Field(default="", alias="timeWindow")
    start_time: str = Field(alias="startTime")
    duration_minutes: int = Field(default=90, alias="durationMinutes")
    kind: str = "visit"
    raw_need: str = Field(alias="rawNeed")
    route_anchor: bool = Field(default=True, alias="routeAnchor")
    priority: int = 50
    notes: str = ""

    model_config = {"populate_by_name": True, "extra": "ignore"}


class AgentMealExperienceBrief(BaseModel):
    brief_id: str = Field(default="", alias="briefId", max_length=96)
    proposal_brief_id: str = Field(default="", alias="proposalBriefId", max_length=96)
    planning_slot_id: str = Field(alias="planningSlotId", min_length=1, max_length=96)
    day_number: int = Field(alias="dayNumber", ge=1, le=31)
    meal_label: Literal["breakfast", "lunch", "snack", "dinner"] = Field(
        default="lunch", alias="mealLabel"
    )
    theme_id: str = Field(alias="themeId", min_length=1, max_length=64)
    theme_label: str = Field(alias="themeLabel", min_length=1, max_length=40)
    experience_mode: Literal[
        "signature_dish",
        "neighborhood_home_style",
        "traditional_snack",
        "market_food",
        "heritage_dining",
        "light_restorative_meal",
    ] = Field(alias="experienceMode")
    search_terms: list[str] = Field(alias="searchTerms", min_length=1, max_length=3)
    avoid_theme_ids: list[str] = Field(default_factory=list, alias="avoidThemeIds", max_length=8)
    selection_intent: str = Field(default="", alias="selectionIntent", max_length=160)

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @model_validator(mode="after")
    def search_terms_are_theme_hypotheses(self) -> "AgentMealExperienceBrief":
        normalized: set[str] = set()
        for raw_term in self.search_terms:
            term = re.sub(r"\s+", " ", str(raw_term or "")).strip()
            if not term or len(term) > 32:
                raise ValueError("meal_theme_search_term_invalid")
            compact = re.sub(r"[\s\-_,.()（）·・，。]+", "", term.casefold())
            if compact in normalized:
                raise ValueError("meal_theme_search_term_duplicate")
            if re.search(r"(?:分店|门店|总店|旗舰店|餐厅|饭庄|酒楼|酒店)$", compact):
                raise ValueError("meal_theme_must_not_preselect_restaurant")
            normalized.add(compact)
        return self


class AgentInitialIntentPool(BaseModel):
    pool_id: str = Field(alias="poolId")
    brief_id: str = Field(default="", alias="briefId")
    raw_need: str = Field(default="", alias="rawNeed")
    city: str
    intent_type: str = Field(alias="intentType")
    target_count: int = Field(alias="targetCount")
    requirement_level: Literal["required", "optional"] = Field(default="optional", alias="requirementLevel")
    goal_id: Optional[str] = Field(default=None, alias="goalId")
    soft_goal_id: Optional[str] = Field(default=None, alias="softGoalId")
    optional_experience_family: Optional[str] = Field(default=None, alias="optionalExperienceFamily")
    preferred_types: list[str] = Field(default_factory=list, alias="preferredTypes")
    rejected_types: list[str] = Field(default_factory=list, alias="rejectedTypes")
    route_preference: dict = Field(default_factory=dict, alias="routePreference")
    assign_to_slots: list[str] = Field(default_factory=list, alias="assignToSlots")
    candidate_hints: list[str] = Field(default_factory=list, alias="candidateHints")
    hint_policy: Literal["llm_common_knowledge_hint", "user_explicit_hint", "no_hint"] = Field(
        default="no_hint", alias="hintPolicy"
    )
    entity_binding_mode: Optional[Literal["category", "exact_entity"]] = Field(default=None, alias="entityBindingMode")
    exact_entity: Optional[str] = Field(default=None, alias="exactEntity")
    meal_experience_briefs: list[AgentMealExperienceBrief] = Field(
        default_factory=list, alias="mealExperienceBriefs", max_length=8
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @model_validator(mode="after")
    def entity_binding_is_consistent(self) -> "AgentInitialIntentPool":
        if self.entity_binding_mode is None:
            legacy_explicit = self.hint_policy == "user_explicit_hint" and len(self.candidate_hints) == 1
            self.entity_binding_mode = "exact_entity" if legacy_explicit else "category"
            if legacy_explicit and not str(self.exact_entity or "").strip():
                self.exact_entity = str(self.candidate_hints[0]).strip()
        if self.entity_binding_mode == "exact_entity" and not str(self.exact_entity or "").strip():
            raise ValueError("exact_entity_binding_requires_identity")
        if self.entity_binding_mode == "category" and self.exact_entity is not None:
            raise ValueError("category_binding_must_not_have_exact_entity")
        if self.meal_experience_briefs and self.intent_type != "meal":
            raise ValueError("meal_experience_brief_requires_meal_pool")
        if self.meal_experience_briefs:
            assigned_slots = set(self.assign_to_slots)
            brief_slots = [item.planning_slot_id for item in self.meal_experience_briefs]
            if set(brief_slots) != assigned_slots or len(brief_slots) != len(set(brief_slots)):
                raise ValueError("meal_experience_brief_slot_coverage_invalid")
            theme_ids = [re.sub(r"\s+", "", item.theme_id).casefold() for item in self.meal_experience_briefs]
            if len(theme_ids) != len(set(theme_ids)):
                raise ValueError("meal_experience_brief_theme_duplicate")
        return self


class AgentInitialPlanOutput(BaseModel):
    reply: str
    mode: str
    day_slots: list[AgentInitialDaySlot] = Field(default_factory=list, alias="daySlots")
    intent_pools: list[AgentInitialIntentPool] = Field(default_factory=list, alias="intentPools")
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @model_validator(mode="after")
    def meal_experience_lineage_is_consistent(self) -> "AgentInitialPlanOutput":
        slots = {str(item.slot_id or ""): item for item in self.day_slots if str(item.slot_id or "")}
        theme_ids: list[str] = []
        for pool in self.intent_pools:
            for brief in pool.meal_experience_briefs:
                slot = slots.get(brief.planning_slot_id)
                if slot is None or int(slot.day_number) != int(brief.day_number):
                    raise ValueError("meal_experience_brief_day_lineage_invalid")
                if (
                    str(brief.proposal_brief_id or "").strip()
                    and str(pool.brief_id or "").strip()
                    and brief.proposal_brief_id != pool.brief_id
                ):
                    raise ValueError("meal_experience_brief_proposal_lineage_invalid")
                theme_ids.append(re.sub(r"\s+", "", brief.theme_id).casefold())
        if len(theme_ids) != len(set(theme_ids)):
            raise ValueError("meal_experience_portfolio_theme_duplicate")
        return self


class AgentMessageResponse(BaseModel):
    request_replay: Optional[dict] = Field(default=None, alias="requestReplay")
    user_turn: ConversationTurnResponse = Field(alias="userTurn")
    assistant_turn: ConversationTurnResponse = Field(alias="assistantTurn")
    itinerary: Optional[ItineraryPlanResponse] = None
    version: Optional[ItineraryVersionResponse] = None
    pending_poi_candidates: list[PendingPoiCandidateResponse] = Field(
        default_factory=list, alias="pendingPoiCandidates"
    )
    preference_memory: Optional[PreferenceMemoryResponse] = Field(default=None, alias="preferenceMemory")
    warnings: list[str] = Field(default_factory=list)
    planning_run: Optional[PlanningRunResponse] = Field(default=None, alias="planningRun")
    planning_steps: list[AgentPlanningEventResponse] = Field(default_factory=list, alias="planningSteps")
    tool_events: list[AgentPlanningEventResponse] = Field(default_factory=list, alias="toolEvents")
    reasoning_statuses: list[AgentReasoningStatusResponse] = Field(default_factory=list, alias="reasoningStatuses")
    execution_mode: str = Field(default="", alias="executionMode")
    terminal_status: str = Field(default="", alias="terminalStatus")
    agent_decision_count: int = Field(default=0, alias="agentDecisionCount")
    outcome_statuses: dict = Field(default_factory=dict, alias="outcomeStatuses")

    model_config = {"populate_by_name": True}


class AgentSpatialMapSelectionBindRequest(BaseModel):
    source_assistant_turn_id: str = Field(alias="sourceAssistantTurnId", min_length=1, max_length=128)
    checkpoint_id: str = Field(alias="checkpointId", min_length=1, max_length=128)
    checkpoint_fingerprint: str = Field(alias="checkpointFingerprint", min_length=32, max_length=128)
    amap_poi_id: str = Field(alias="amapPoiId", min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=160)
    radius_meters: float = Field(alias="radiusMeters", ge=100, le=50000)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class AgentSpatialMapSelectionBindResponse(BaseModel):
    option_id: str = Field(alias="optionId")
    label: str
    map_selection_fingerprint: str = Field(alias="mapSelectionFingerprint")
    checkpoint: dict

    model_config = {"populate_by_name": True}


class AgentMessageEditResponse(BaseModel):
    edited_turn: ConversationTurnResponse = Field(alias="editedTurn")
    superseded_turn_ids: list[str] = Field(default_factory=list, alias="supersededTurnIds")
    restored_version_id: Optional[str] = Field(default=None, alias="restoredVersionId")
    assistant_turn: Optional[ConversationTurnResponse] = Field(default=None, alias="assistantTurn")
    itinerary: Optional[ItineraryPlanResponse] = None
    version: Optional[ItineraryVersionResponse] = None
    pending_poi_candidates: list[PendingPoiCandidateResponse] = Field(
        default_factory=list, alias="pendingPoiCandidates"
    )
    warnings: list[str] = Field(default_factory=list)
    planning_run: Optional[PlanningRunResponse] = Field(default=None, alias="planningRun")
    planning_steps: list[AgentPlanningEventResponse] = Field(default_factory=list, alias="planningSteps")
    tool_events: list[AgentPlanningEventResponse] = Field(default_factory=list, alias="toolEvents")

    model_config = {"populate_by_name": True}

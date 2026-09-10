from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from src.services.timeline_mutation_models import TimelineMutationIntent
from src.services.request_activity_coverage_service import RequestClauseCoverage


class _Directive(BaseModel):
    model_config = {"populate_by_name": True, "extra": "forbid"}


class DraftDayStrategy(_Directive):
    day_number: int = Field(alias="dayNumber", ge=1)
    theme: str = Field(min_length=1)
    required_goal_ids: list[str] = Field(default_factory=list, alias="requiredGoalIds")
    required_goal_counts: dict[str, int] = Field(default_factory=dict, alias="requiredGoalCounts")
    optional_goal_ids: list[str] = Field(default_factory=list, alias="optionalGoalIds")
    pace: Literal["relaxed", "standard", "intensive"] = "standard"
    max_route_anchors: int = Field(default=4, alias="maxRouteAnchors", ge=1, le=6)

    @model_validator(mode="after")
    def goal_counts_must_match_required_goals(self) -> "DraftDayStrategy":
        if any(count != 1 for count in self.required_goal_counts.values()):
            raise ValueError("draft_goal_daily_cardinality_exceeded")
        if not set(self.required_goal_counts).issubset(set(self.required_goal_ids)):
            raise ValueError("draft_goal_count_requires_goal_id")
        return self


class CandidateSelectionPolicy(_Directive):
    auto_select_when_dominant: bool = Field(default=True, alias="autoSelectWhenDominant")
    ask_when_material_tradeoff: bool = Field(default=True, alias="askWhenMaterialTradeoff")
    prefer_low_detour: bool = Field(default=True, alias="preferLowDetour")
    avoid_recent_entities: bool = Field(default=True, alias="avoidRecentEntities")


class SchedulePolicy(_Directive):
    respect_opening_windows_when_known: bool = Field(default=True, alias="respectOpeningWindowsWhenKnown")
    allow_provisional_when_unknown: bool = Field(default=True, alias="allowProvisionalWhenUnknown")


class RouteDetourEnvelope(_Directive):
    max_generalized_cost_delta: float = Field(alias="maxGeneralizedCostDelta", ge=0, le=180)
    max_detour_ratio: float = Field(alias="maxDetourRatio", ge=0, le=2)


class RouteMobilityEstimate(_Directive):
    transport_mode: Literal["transit", "walking", "bicycling", "driving"] = Field(
        alias="transportMode"
    )
    pace_class: Literal["relaxed", "standard", "intensive"] = Field(alias="paceClass")


class RoutePlanningPolicy(_Directive):
    objective: Literal[
        "least_generalized_cost",
        "balanced",
        "least_walking",
        "least_transfer",
    ] = "least_generalized_cost"
    source: Literal["user_explicit", "clarification_answer", "controller_estimate"]
    allow_experience_detour: bool = Field(default=False, alias="allowExperienceDetour")
    detour_envelope: Optional[RouteDetourEnvelope] = Field(default=None, alias="detourEnvelope")
    mobility_profile: Optional[RouteMobilityEstimate] = Field(default=None, alias="mobilityProfile")


class DraftDurationEstimate(_Directive):
    min: int = Field(ge=1, le=720)
    preferred: int = Field(ge=1, le=720)
    max: int = Field(ge=1, le=720)

    @model_validator(mode="after")
    def ordered_bounds(self) -> "DraftDurationEstimate":
        if not self.min <= self.preferred <= self.max:
            raise ValueError("draft_duration_estimate_bounds_invalid")
        return self


class DraftOccurrenceScheduleHint(_Directive):
    goal_id: str = Field(alias="goalId", min_length=1)
    day_number: int = Field(alias="dayNumber", ge=1)
    day_part: Literal["morning", "noon", "afternoon", "evening", "night", "flexible"] = Field(alias="dayPart")
    sequence: int = Field(ge=1, le=12)
    preferred_start_time: Optional[str] = Field(default=None, alias="preferredStartTime", pattern=r"^\d{2}:\d{2}$")
    duration_estimate: DraftDurationEstimate = Field(alias="durationEstimate")
    estimate_source: Literal["controller_estimate", "user_explicit", "trusted_server_fact"] = Field(
        alias="estimateSource"
    )
    confidence: float = Field(ge=0, le=1)


class DraftRouteGapSupplementHint(_Directive):
    """Controller-authored search intent for one optional route-gap activity.

    The Controller may describe a family and bounded search vocabulary, but it
    cannot mint a goal, occurrence, pool or planning-slot identity. Those
    identities are sealed only after a real candidate and route insertion have
    passed the server gates.
    """

    day_number: int = Field(alias="dayNumber", ge=1)
    intent_type: Literal["museum", "park", "area_walk", "local_culture", "shopping"] = Field(alias="intentType")
    experience_family: str = Field(alias="experienceFamily", min_length=1, max_length=80)
    query_hint: str = Field(alias="queryHint", min_length=1, max_length=80)
    duration_estimate: DraftDurationEstimate = Field(alias="durationEstimate")
    confidence: float = Field(ge=0, le=1)


class DraftItineraryDirective(_Directive):
    type: Literal["draft_itinerary"]
    request_coverage: Optional[list[RequestClauseCoverage]] = Field(default=None, alias="requestCoverage", max_length=32)
    goal_priority: list[str] = Field(default_factory=list, alias="goalPriority")
    day_strategies: list[DraftDayStrategy] = Field(alias="dayStrategies", min_length=1)
    optional_experience_budget: int = Field(default=0, alias="optionalExperienceBudget", ge=0, le=3)
    search_priority: list[str] = Field(default_factory=list, alias="searchPriority")
    candidate_selection_policy: CandidateSelectionPolicy = Field(
        default_factory=CandidateSelectionPolicy, alias="candidateSelectionPolicy"
    )
    schedule_policy: SchedulePolicy = Field(default_factory=SchedulePolicy, alias="schedulePolicy")
    route_planning_policy: Optional[RoutePlanningPolicy] = Field(default=None, alias="routePlanningPolicy")
    occurrence_schedule_hints: list[DraftOccurrenceScheduleHint] = Field(
        default_factory=list,
        alias="occurrenceScheduleHints",
    )
    route_gap_supplement_hints: list[DraftRouteGapSupplementHint] = Field(
        default_factory=list,
        alias="routeGapSupplementHints",
        max_length=6,
    )


class ReadItineraryDirective(_Directive):
    type: Literal["read_itinerary"]
    query_type: str = Field(default="timeline_summary", alias="queryType")
    target_text: str = Field(default="", alias="targetText")
    target_intent_type: Optional[str] = Field(default=None, alias="targetIntentType")
    include_day: bool = Field(default=True, alias="includeDay")
    include_time: bool = Field(default=True, alias="includeTime")
    include_grounding_status: bool = Field(default=True, alias="includeGroundingStatus")


class ResolvePoiDirective(_Directive):
    type: Literal["resolve_poi"]
    target_goal_id: Optional[str] = Field(default=None, alias="targetGoalId")
    target_segment_ids: list[str] = Field(default_factory=list, alias="targetSegmentIds")
    search_intent: str = Field(alias="searchIntent", min_length=1)
    search_mode: Literal["text", "near_route_corridor", "near_selected_poi"] = Field(default="text", alias="searchMode")
    max_candidates: int = Field(default=4, alias="maxCandidates", ge=1, le=5)
    auto_select_policy: Literal["dominant_safe_candidate_only", "never"] = Field(
        default="dominant_safe_candidate_only", alias="autoSelectPolicy"
    )
    ask_user_policy: Literal["material_tradeoff_only", "always"] = Field(
        default="material_tradeoff_only", alias="askUserPolicy"
    )


PatchOperationIntent = Literal[
    "replace_segment_start_time",
    "replace_segment_duration",
    "replace_segment_poi",
    "replace_segment_poi_from_candidate",
    "remove_segment",
    "move_segment",
    "add_segment",
    "timeline_command",
    "complex_patch",
]


class PatchDirective(_Directive):
    type: Literal["patch_itinerary"]
    operation_intent: Optional[PatchOperationIntent] = Field(default=None, alias="operationIntent")
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId", min_length=1)
    target_segment_ids: list[str] = Field(default_factory=list, alias="targetSegmentIds")
    requested_outcome: str = Field(default="", alias="requestedOutcome")
    mutation_intent: Optional[TimelineMutationIntent] = Field(default=None, alias="mutationIntent")
    preserve: list[str] = Field(default_factory=list)
    max_changed_segment_count: int = Field(default=1, alias="maxChangedSegmentCount", ge=1, le=3)
    start_time: Optional[str] = Field(default=None, alias="startTime")
    candidate_id: Optional[str] = Field(default=None, alias="candidateId")
    amap_poi_id: Optional[str] = Field(default=None, alias="amapPoiId")

    @model_validator(mode="after")
    def require_legacy_identity_or_semantic_intent(self):
        if self.mutation_intent is not None:
            return self
        if (
            not self.operation_intent
            or not self.base_version_id
            or not self.target_segment_ids
            or not self.requested_outcome
        ):
            raise ValueError("patch_directive_requires_identity_or_mutation_intent")
        return self


class OptimizeRouteDirective(_Directive):
    type: Literal["optimize_route"]
    base_version_id: str = Field(alias="baseVersionId", min_length=1)
    day_ids: list[str] = Field(default_factory=list, alias="dayIds")
    day_numbers: list[int] = Field(default_factory=list, alias="dayNumbers")
    optimization_objective: Literal["balanced", "least_walking", "least_time", "least_transfer"] = Field(
        default="balanced", alias="optimizationObjective"
    )


class VerifyExternalFactsDirective(_Directive):
    type: Literal["verify_external_facts"]
    fact_types: list[Literal["weather", "risk", "reservation"]] = Field(alias="factTypes", min_length=1)
    segment_ids: list[str] = Field(default_factory=list, alias="segmentIds")


class ClarificationChoice(_Directive):
    """A controller-authored, opaque semantic answer for a clarification turn.

    The label is presentation only.  The server persists and validates the
    semantic value through a checkpoint before it is allowed to influence the
    request contract.
    """

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    semantic_value: Any = Field(alias="semanticValue")
    allows_manual_input: bool = Field(default=False, alias="allowsManualInput")


class ClarificationQuestionDirective(_Directive):
    dimension_id: str = Field(alias="dimensionId", min_length=1)
    question: str = Field(min_length=1)
    why_it_matters: str = Field(alias="whyItMatters", min_length=1)
    allow_free_text: bool = Field(default=False, alias="allowFreeText")
    options: list[ClarificationChoice] = Field(min_length=2)


class AskUserDirective(_Directive):
    type: Literal["ask_user"]
    question: Optional[str] = Field(default=None, min_length=1)
    dimension_id: Optional[str] = Field(default=None, alias="dimensionId", min_length=1)
    # A continuation question must echo the complete durable checkpoint
    # identity supplied in the Controller context.  These remain optional at
    # the type boundary because the first question has no server-issued
    # checkpoint yet; AgentAutonomyController requires all four together when
    # a prior checkpoint exists and forbids the model from inventing them.
    checkpoint_id: Optional[str] = Field(default=None, alias="checkpointId", min_length=1)
    planning_root_id: Optional[str] = Field(default=None, alias="planningRootId", min_length=1)
    request_fingerprint: Optional[str] = Field(
        default=None,
        alias="requestFingerprint",
        min_length=1,
    )
    checkpoint_fingerprint: Optional[str] = Field(
        default=None,
        alias="checkpointFingerprint",
        min_length=1,
    )
    why_it_matters: Optional[str] = Field(default=None, alias="whyItMatters", min_length=1)
    allow_free_text: Optional[bool] = Field(default=None, alias="allowFreeText")
    options: list[ClarificationChoice] = Field(default_factory=list)
    # Kept only for old controller retries and non-planning error states.  A
    # planning clarification is not eligible for a persisted checkpoint until
    # it has a dimension and semantic options above.
    choice_ids: list[str] = Field(default_factory=list, alias="choiceIds")
    questions: list[ClarificationQuestionDirective] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def normalize_controller_authored_question_batch(self) -> "AskUserDirective":
        if self.questions:
            first = self.questions[0]
            if self.question not in (None, first.question) or self.dimension_id not in (None, first.dimension_id):
                raise ValueError("ask_user_batch_legacy_projection_mismatch")
            if self.options and self.options != first.options:
                raise ValueError("ask_user_batch_options_mismatch")
            if len({item.dimension_id for item in self.questions}) != len(self.questions):
                raise ValueError("ask_user_batch_dimension_duplicate")
            self.question = first.question
            self.dimension_id = first.dimension_id
            self.why_it_matters = first.why_it_matters
            self.allow_free_text = first.allow_free_text
            self.options = list(first.options)
        if not self.question:
            raise ValueError("ask_user_question_required")
        return self


class FinishDirective(_Directive):
    type: Literal["finish"]
    assistant_reply: str = Field(default="", alias="assistantReply")


ActionDirective = Annotated[
    Union[
        DraftItineraryDirective,
        ReadItineraryDirective,
        ResolvePoiDirective,
        PatchDirective,
        OptimizeRouteDirective,
        VerifyExternalFactsDirective,
        AskUserDirective,
        FinishDirective,
    ],
    Field(discriminator="type"),
]

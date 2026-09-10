"""Strict, proposal-only contracts for creative portfolio planning.

These models intentionally contain no final map identity, route result, or write
authority.  They are the boundary between a creative suggestion and the existing
verified itinerary transaction path.
"""

from __future__ import annotations

import copy

from hashlib import sha256
import json
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


CreativeAxis = Literal[
    "classic",
    "local_immersion",
    "food_led",
    "culture_deep_dive",
    "nature_relaxed",
    "photo_night",
    "family_light",
    "citywalk_hidden_gems",
]

GoalPriorityTier = Literal[
    "hard",
    "explicit_soft",
    "defining_theme",
    "inferred_preferred",
    "optional",
]


class _StrictModel(BaseModel):
    model_config = {"populate_by_name": True, "extra": "forbid"}


class GoalRequirement(_StrictModel):
    goal_id: str = Field(alias="goalId", min_length=1)
    intent_type: str = Field(alias="intentType", min_length=1)
    required_min: int = Field(default=1, alias="requiredMin", ge=0)
    preferred_count: int = Field(default=1, alias="preferredCount", ge=0)
    max_count: Optional[int] = Field(default=None, alias="maxCount", ge=0)
    cardinality_source: str = Field(default="explicit_user_request", alias="cardinalitySource")
    distribution_policy: str = Field(default="spread_across_distinct_days", alias="distributionPolicy")
    allowed_day_numbers: list[int] = Field(default_factory=list, alias="allowedDayNumbers")
    explicitly_named: bool = Field(default=False, alias="explicitlyNamed")
    exact_entity: Optional[str] = Field(default=None, alias="exactEntity")
    user_explicit: bool = Field(default=False, alias="userExplicit")
    priority_tier: GoalPriorityTier = Field(default="explicit_soft", alias="priorityTier")
    access_policy: Optional[str] = Field(default=None, alias="accessPolicy")
    distinctness_policy: Optional[str] = Field(default=None, alias="distinctnessPolicy")
    time_window: Optional[Union[str, dict[str, Any]]] = Field(default=None, alias="timeWindow")
    schedule_preference: dict[str, Any] = Field(default_factory=dict, alias="schedulePreference")
    detour_tolerance: Optional[dict[str, Any]] = Field(default=None, alias="detourTolerance")
    evidence_freshness: Optional[Union[str, dict[str, Any]]] = Field(default=None, alias="evidenceFreshness")
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    experience_families: list[str] = Field(default_factory=list, alias="experienceFamilies")
    unresolved_dimensions: list[str] = Field(default_factory=list, alias="unresolvedDimensions")

    @model_validator(mode="after")
    def cardinality_is_ordered(self) -> "GoalRequirement":
        if self.preferred_count < self.required_min:
            raise ValueError("goal_cardinality_preferred_below_minimum")
        if self.max_count is not None and (self.max_count < self.required_min or self.preferred_count > self.max_count):
            raise ValueError("goal_cardinality_maximum_invalid")
        if len(self.allowed_day_numbers) != len(set(self.allowed_day_numbers)) or any(
            day < 1 for day in self.allowed_day_numbers
        ):
            raise ValueError("goal_cardinality_allowed_days_invalid")
        return self


class ConstraintLedger(_StrictModel):
    schema_version: Literal["constraint-ledger-v1"] = Field(alias="schemaVersion")
    city: str = Field(min_length=1)
    start_date: Optional[str] = Field(default=None, alias="startDate")
    end_date: Optional[str] = Field(default=None, alias="endDate")
    day_count: int = Field(alias="dayCount", ge=1, le=31)
    hard_goals: list[GoalRequirement] = Field(default_factory=list, alias="hardGoals")
    soft_goals: list[GoalRequirement] = Field(default_factory=list, alias="softGoals")
    transport_preferences: list[str] = Field(default_factory=list, alias="transportPreferences")
    budget_tier: str = Field(default="standard", alias="budgetTier")
    pace: Literal["relaxed", "standard", "intensive"] = "standard"
    party_size: Optional[int] = Field(default=None, alias="partySize", ge=1)
    locked_entities: list[str] = Field(default_factory=list, alias="lockedEntities")
    forbidden_experience_types: list[str] = Field(default_factory=list, alias="forbiddenExperienceTypes")
    preference_snapshot: dict[str, Any] = Field(default_factory=dict, alias="preferenceSnapshot")
    source_fingerprint: str = Field(alias="sourceFingerprint", min_length=16)
    experience_intent: dict[str, Any] = Field(default_factory=dict, alias="experienceIntent")

    @model_validator(mode="after")
    def no_goal_can_be_hard_and_soft(self) -> "ConstraintLedger":
        overlap = {goal.goal_id for goal in self.hard_goals} & {goal.goal_id for goal in self.soft_goals}
        if overlap:
            raise ValueError("constraint_ledger_goal_is_both_hard_and_soft")
        return self


class CreativeDayRole(_StrictModel):
    day_number: int = Field(alias="dayNumber", ge=1)
    role: str = Field(min_length=1, max_length=120)
    target_route_anchors: Optional[int] = Field(
        default=None,
        alias="targetRouteAnchors",
        ge=1,
        le=6,
    )
    density_evidence: list[str] = Field(
        default_factory=list,
        alias="densityEvidence",
        max_length=8,
    )


class CreativeExperienceIntent(_StrictModel):
    family: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def not_a_grounded_place(self) -> "CreativeExperienceIntent":
        forbidden = {"amapId", "longitude", "latitude", "poiId", "coordinates"}
        if forbidden & set(self.__pydantic_extra__ or {}):
            raise ValueError("creative_intent_must_not_contain_poi_identity")
        return self


ExperienceShape = Literal["single_poi", "area", "micro_route", "open_walk"]
ExperienceRequirementLevel = Literal["required", "soft"]


class TripExperienceIntent(_StrictModel):
    """Identity-free trip-level experience semantics.

    The fingerprint deliberately excludes itself and any grounded entity. It is
    safe to carry through provider output, replay artifacts and consumer
    admission without becoming a second place compiler.
    """

    schema_version: Literal["experience-intent-v1"] = Field(alias="schemaVersion")
    trip_thesis: str = Field(alias="tripThesis", min_length=1, max_length=240)
    desired_signals: list[str] = Field(default_factory=list, alias="desiredSignals", max_length=16)
    avoid_signals: list[str] = Field(default_factory=list, alias="avoidSignals", max_length=16)
    decision_axes: list[str] = Field(default_factory=list, alias="decisionAxes", max_length=8)
    allowed_experience_shapes: list[ExperienceShape] = Field(default_factory=list, alias="allowedExperienceShapes")
    fingerprint: str = ""

    @model_validator(mode="after")
    def assign_fingerprint(self) -> "TripExperienceIntent":
        payload = self.model_dump(by_alias=True, exclude={"fingerprint"})
        self.fingerprint = canonical_fingerprint(payload)
        return self


class SlotExperienceIntent(_StrictModel):
    """Identity-free consumer contract for one brief-local planning slot."""

    schema_version: Literal["experience-intent-v1"] = Field(alias="schemaVersion")
    slot_id: str = Field(alias="slotId", min_length=1)
    family: str = Field(min_length=1, max_length=64)
    requirement_level: ExperienceRequirementLevel = Field(alias="requirementLevel")
    experience_shape: ExperienceShape = Field(alias="experienceShape")
    experience_goal: str = Field(alias="experienceGoal", min_length=1, max_length=240)
    desired_signals: list[str] = Field(default_factory=list, alias="desiredSignals", max_length=16)
    avoid_signals: list[str] = Field(default_factory=list, alias="avoidSignals", max_length=16)
    evidence_policy: dict[str, Any] = Field(default_factory=dict, alias="evidencePolicy")
    grounding_policy: dict[str, Any] = Field(default_factory=dict, alias="groundingPolicy")
    route_context: dict[str, Any] = Field(default_factory=dict, alias="routeContext")
    intent_fingerprint: str = Field(default="", alias="intentFingerprint")

    @model_validator(mode="after")
    def assign_fingerprint(self) -> "SlotExperienceIntent":
        payload = self.model_dump(by_alias=True, exclude={"intent_fingerprint"})
        self.intent_fingerprint = canonical_fingerprint(payload)
        return self


class CreativePortfolioDaySlot(_StrictModel):
    """Identity-free, brief-local schedule intent emitted by the provider."""

    slot_id: str = Field(alias="slotId", min_length=1)
    day_number: int = Field(alias="dayNumber", ge=1)
    time_window: str = Field(alias="timeWindow", min_length=1)
    start_time: Optional[str] = Field(default=None, alias="startTime")
    duration_minutes: int = Field(alias="durationMinutes", ge=1, le=720)
    kind: str = Field(min_length=1)
    raw_need: str = Field(alias="rawNeed", min_length=1)
    route_anchor: bool = Field(default=False, alias="routeAnchor")
    priority: int = Field(default=50, ge=0, le=100)
    required_goal_id: Optional[str] = Field(default=None, alias="requiredGoalId")
    soft_goal_id: Optional[str] = Field(default=None, alias="softGoalId")
    optional_experience_family: Optional[str] = Field(default=None, alias="optionalExperienceFamily")
    date: Optional[str] = None
    notes: str = ""
    requirement_level: Optional[ExperienceRequirementLevel] = Field(default=None, alias="requirementLevel")
    experience_shape: Optional[ExperienceShape] = Field(default=None, alias="experienceShape")
    experience_goal: Optional[str] = Field(default=None, alias="experienceGoal")
    desired_signals: list[str] = Field(default_factory=list, alias="desiredSignals", max_length=16)
    avoid_signals: list[str] = Field(default_factory=list, alias="avoidSignals", max_length=16)
    evidence_requirements: dict[str, Any] = Field(default_factory=dict, alias="evidenceRequirements")
    grounding_contract: dict[str, Any] = Field(default_factory=dict, alias="groundingContract")
    route_contract: dict[str, Any] = Field(default_factory=dict, alias="routeContract")
    intent_fingerprint: Optional[str] = Field(default=None, alias="intentFingerprint")


class CreativePortfolioIntentPool(_StrictModel):
    """Identity-free, brief-local candidate request contract."""

    pool_id: str = Field(alias="poolId", min_length=1)
    brief_id: str = Field(default="", alias="briefId")
    raw_need: str = Field(alias="rawNeed", min_length=1)
    city: str = Field(min_length=1)
    intent_type: str = Field(alias="intentType", min_length=1)
    target_count: int = Field(alias="targetCount", ge=1, le=4)
    requirement_level: Literal["required", "optional"] = Field(default="optional", alias="requirementLevel")
    goal_id: Optional[str] = Field(default=None, alias="goalId")
    soft_goal_id: Optional[str] = Field(default=None, alias="softGoalId")
    assign_to_slots: list[str] = Field(default_factory=list, alias="assignToSlots", max_length=8)
    optional_experience_family: Optional[str] = Field(default=None, alias="optionalExperienceFamily")
    preferred_types: list[str] = Field(default_factory=list, alias="preferredTypes", max_length=12)
    rejected_types: list[str] = Field(default_factory=list, alias="rejectedTypes", max_length=12)
    candidate_hints: list[str] = Field(default_factory=list, alias="candidateHints", max_length=8)
    route_preference: dict[str, Any] = Field(default_factory=dict, alias="routePreference")
    hint_policy: str = Field(default="no_hint", alias="hintPolicy")
    entity_binding_mode: Literal["category", "exact_entity"] = Field(default="category", alias="entityBindingMode")
    exact_entity: Optional[str] = Field(default=None, alias="exactEntity")

    @model_validator(mode="after")
    def exact_binding_has_identity(self) -> "CreativePortfolioIntentPool":
        if self.entity_binding_mode == "exact_entity" and not str(self.exact_entity or "").strip():
            raise ValueError("exact_entity_binding_requires_identity")
        if self.entity_binding_mode == "category" and self.exact_entity is not None:
            raise ValueError("category_binding_must_not_have_exact_entity")
        return self


class CreativeBrief(_StrictModel):
    brief_id: str = Field(alias="briefId", min_length=1)
    title: str = Field(min_length=1, max_length=80)
    primary_axis: CreativeAxis = Field(alias="primaryAxis")
    secondary_axes: list[CreativeAxis] = Field(default_factory=list, alias="secondaryAxes", max_length=3)
    narrative_arc: str = Field(default="", alias="narrativeArc", max_length=300)
    day_roles: list[CreativeDayRole] = Field(default_factory=list, alias="dayRoles")
    optional_experiences: list[CreativeExperienceIntent] = Field(
        default_factory=list, alias="optionalExperiences", max_length=3
    )
    required_goal_ids: list[str] = Field(default_factory=list, alias="requiredGoalIds")
    avoid_experience_types: list[str] = Field(default_factory=list, alias="avoidExperienceTypes")
    experience_intent: Optional[TripExperienceIntent] = Field(default=None, alias="experienceIntent")
    theme_families: list[str] = Field(default_factory=list, alias="themeFamilies", max_length=8)
    candidate_supply: dict[str, Any] = Field(default_factory=dict, alias="candidateSupply")
    direction_signature: str = Field(default="", alias="directionSignature", max_length=128)
    novelty_evidence: dict[str, Any] = Field(default_factory=dict, alias="noveltyEvidence")
    feasibility_evidence: dict[str, Any] = Field(default_factory=dict, alias="feasibilityEvidence")
    generation_source: str = Field(default="", alias="generationSource", max_length=64)

    @field_validator("secondary_axes")
    @classmethod
    def axes_must_be_distinct(cls, value: list[CreativeAxis]) -> list[CreativeAxis]:
        if len(set(value)) != len(value):
            raise ValueError("creative_brief_duplicate_secondary_axis")
        return value


class PlanScoreVector(_StrictModel):
    hard_constraint_passed: bool = Field(alias="hardConstraintPassed")
    preference_fit: float = Field(alias="preferenceFit", ge=0, le=100)
    thematic_coherence: float = Field(alias="thematicCoherence", ge=0, le=100)
    experience_diversity: float = Field(alias="experienceDiversity", ge=0, le=100)
    route_efficiency: float = Field(alias="routeEfficiency", ge=0, le=100)
    pacing_quality: float = Field(alias="pacingQuality", ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    robustness: float = Field(ge=0, le=100)
    uncertainty_penalty: float = Field(alias="uncertaintyPenalty", ge=0, le=100)
    estimated_cost_cny: float = Field(default=0, alias="estimatedCostCny", ge=0)
    evidence: dict[str, list[str]] = Field(default_factory=dict)


class PlanCandidate(_StrictModel):
    proposal_id: str = Field(alias="proposalId", min_length=1)
    portfolio_id: str = Field(alias="portfolioId", min_length=1)
    brief: CreativeBrief
    itinerary_snapshot: dict[str, Any] = Field(alias="itinerarySnapshot")
    grounded_evidence: list[dict[str, Any]] = Field(default_factory=list, alias="groundedEvidence")
    unresolved_evidence: list[dict[str, Any]] = Field(default_factory=list, alias="unresolvedEvidence")
    score: PlanScoreVector
    verifier: dict[str, Any]
    canonical_signature: str = Field(alias="canonicalSignature", min_length=16)
    generation_lineage: dict[str, Any] = Field(default_factory=dict, alias="generationLineage")

    @model_validator(mode="after")
    def candidate_identity_must_be_consistent(self) -> "PlanCandidate":
        if self.brief.brief_id == self.proposal_id:
            raise ValueError("proposal_id_must_not_reuse_brief_id")
        return self


class PlanPortfolio(_StrictModel):
    portfolio_id: str = Field(alias="portfolioId", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)
    source_user_turn_id: str = Field(alias="sourceUserTurnId", min_length=1)
    source_assistant_turn_id: Optional[str] = Field(default=None, alias="sourceAssistantTurnId")
    expected_base_version_id: Optional[str] = Field(default=None, alias="expectedBaseVersionId")
    source_observation_fingerprint: str = Field(alias="sourceObservationFingerprint", min_length=16)
    request_contract_fingerprint: str = Field(alias="requestContractFingerprint", min_length=16)
    status: Literal["building", "awaiting_selection", "committing", "committed", "expired", "failed", "cancelled"]
    proposal_ids: list[str] = Field(default_factory=list, alias="proposalIds")
    visible_proposal_ids: list[str] = Field(default_factory=list, alias="visibleProposalIds")
    selected_proposal_id: Optional[str] = Field(default=None, alias="selectedProposalId")
    dominant_proposal_id: Optional[str] = Field(default=None, alias="dominantProposalId")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")


def canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def proposal_structural_signature_material(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the title-independent material used for proposal identity."""

    structural = copy.deepcopy(snapshot)
    structural.pop("title", None)
    structural.pop("decisionRationale", None)
    structural.pop("creativeBrief", None)
    structural.pop("portfolioTitleEvidence", None)
    structural.pop("portfolioTitleGeneration", None)
    structural.pop("proposalSpecificTitleSignals", None)
    output_quality = structural.get("portfolioOutputQuality")
    if isinstance(output_quality, dict):
        output_quality.pop("originalCreativeTitle", None)
        output_quality.pop("displayTitle", None)
    return structural


def proposal_canonical_signature(snapshot: dict[str, Any]) -> str:
    return canonical_fingerprint(proposal_structural_signature_material(snapshot))

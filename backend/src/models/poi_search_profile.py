"""Strict provider-neutral contracts for semantic POI search compilation.

The profile deliberately stops before provider execution.  It describes what
must be searched and verified, while the AMap adapter remains the only layer
that translates provider category keys into AMap type codes or adds concrete
anchor coordinates.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SearchQueryMode = Literal[
    "exact_entity",
    "amap_text",
    "amap_around",
    "route_corridor",
    "web_seed_then_amap",
]
SearchAnchorPolicy = Literal[
    "none",
    "previous_only",
    "next_only",
    "between_adjacent",
]
EntityBindingMode = Literal["category", "exact_entity", "legacy"]
RequirementLevel = Literal["required", "soft", "optional"]
ExperienceShape = Literal["single_poi", "area", "micro_route", "open_walk"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExperienceSemanticInput(_StrictModel):
    """Server-owned semantic facts used to compile a search profile.

    ``previousAnchor`` and ``nextAnchor`` are intentionally provider-neutral
    context dictionaries.  They affect only execution identity and anchor
    policy; neither their IDs nor their coordinates may enter the semantic
    ``profileFingerprint``.
    """

    city: str = Field(min_length=1)
    poolId: str = Field(min_length=1)
    briefId: str = ""
    planningSlotId: str = ""
    dayNumber: int = Field(default=0, ge=0, le=366)
    requirementLevel: RequirementLevel
    goalId: Optional[str] = None
    softGoalId: Optional[str] = None
    rawNeed: str = Field(min_length=1)
    intentType: str = Field(min_length=1)
    optionalExperienceFamily: Optional[str] = None
    # A single family selected by the server-owned ExperienceSpec.  It is
    # intentionally distinct from Creative Portfolio's optional family: a
    # core intent such as night_view keeps its core query ladder while this
    # value adds the selected admission overlay.
    experienceSpecFamily: Optional[str] = None
    experienceSpecFamilyError: Optional[
        Literal["missing", "ambiguous", "invalid"]
    ] = None
    assignedMealFamily: Optional[str] = None
    preferredTypes: list[str] = Field(default_factory=list)
    rejectedTypes: list[str] = Field(default_factory=list)
    candidateHints: list[str] = Field(default_factory=list)
    hintPolicy: str = "no_hint"
    routePreference: dict[str, Any] = Field(default_factory=dict)
    entityBindingMode: EntityBindingMode = "category"
    exactEntity: Optional[str] = None
    semanticContext: str = ""
    previousAnchor: Optional[dict[str, Any]] = None
    nextAnchor: Optional[dict[str, Any]] = None
    excludedPhysicalPoiIds: list[str] = Field(default_factory=list)
    targetCount: int = Field(default=1, ge=1, le=20)
    evidenceTargetCount: Optional[int] = Field(default=None, ge=1, le=40)
    maxQueries: int = Field(default=4, ge=1, le=16)
    experienceShape: ExperienceShape = "single_poi"
    experienceGoal: str = ""
    desiredSignals: list[str] = Field(default_factory=list)
    avoidSignals: list[str] = Field(default_factory=list)
    evidencePolicy: dict[str, Any] = Field(default_factory=dict)
    groundingPolicy: dict[str, Any] = Field(default_factory=dict)
    routeContext: dict[str, Any] = Field(default_factory=dict)
    experienceSpecPolicy: dict[str, Any] = Field(default_factory=dict)
    intentFingerprint: str = ""

    @model_validator(mode="after")
    def require_exact_entity_for_exact_binding(self) -> "ExperienceSemanticInput":
        if self.entityBindingMode == "exact_entity" and not str(
            self.exactEntity or ""
        ).strip():
            raise ValueError("exactEntity is required for exact_entity binding")
        if self.experienceSpecFamilyError and str(
            self.experienceSpecFamily or ""
        ).strip():
            raise ValueError(
                "experienceSpecFamily and experienceSpecFamilyError are mutually exclusive"
            )
        return self


class SearchQueryPlan(_StrictModel):
    planId: str = Field(min_length=1)
    priority: int = Field(ge=0, le=1000)
    mode: SearchQueryMode
    keyword: str = Field(min_length=1)
    keywordVariants: list[str] = Field(min_length=1)
    providerCategoryKeys: list[str] = Field(min_length=1)
    preferredTypeGroups: list[str] = Field(default_factory=list)
    rejectedTypeGroups: list[str] = Field(default_factory=list)
    anchorPolicy: SearchAnchorPolicy = "none"
    radiusMeters: int = Field(default=1500, ge=50, le=5000)
    resultLimit: int = Field(default=12, ge=1, le=25)
    fallbackLevel: int = Field(default=0, ge=0, le=3)
    requiresAmapGrounding: bool = True
    stopWhenTargetReached: bool = True


class SearchScoringPolicy(_StrictModel):
    requiredSemanticFacets: list[str] = Field(default_factory=list)
    preferredFacetWeight: float = Field(default=0.35, ge=0.0, le=1.0)
    routeFitWeight: float = Field(default=0.2, ge=0.0, le=1.0)
    exactEntityRequired: bool = False
    distinctPhysicalPoiRequired: bool = True


class SearchFallbackPolicy(_StrictModel):
    status: Literal["none", "bounded_fallback", "explicit_fallback"] = "none"
    reasonCode: Optional[str] = None
    allowGenericScenic: bool = False
    allowSemanticBroadening: bool = True
    requiresUserVisibleDegradedState: bool = False


class SearchCoveragePolicy(_StrictModel):
    targetCount: int = Field(ge=1, le=20)
    evidenceTargetCount: int = Field(ge=1, le=40)
    distinctPhysicalPoiRequired: bool = True
    stopWhenTargetReached: bool = True


class SearchBudgetPolicy(_StrictModel):
    maxQueries: int = Field(ge=1, le=16)
    maxAmapCalls: int = Field(ge=1, le=16)
    maxWebSeedQueries: int = Field(default=0, ge=0, le=4)
    resultLimit: int = Field(default=12, ge=1, le=25)


class SearchSourceEvidence(_StrictModel):
    source: Literal["server_compiled_experience_semantic"] = (
        "server_compiled_experience_semantic"
    )
    rawNeed: str = Field(min_length=1)
    semanticContext: str = ""
    familySource: Literal[
        "optional_experience_family",
        "experience_spec_family",
        "experience_spec_family_missing",
        "experience_spec_family_ambiguous",
        "experience_spec_family_invalid",
        "assigned_meal_family",
        "unknown",
    ]
    goalId: Optional[str] = None
    softGoalId: Optional[str] = None
    hintPolicy: str = "no_hint"
    fallbackApplied: bool = False
    candidateHintsUsedAsSemanticEvidence: bool = False


class PoiSearchProfile(_StrictModel):
    schemaVersion: Literal["poi-search-profile-v1"] = "poi-search-profile-v1"
    profileId: str = Field(min_length=1)
    profileFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    city: str = Field(min_length=1)
    poolId: str = Field(min_length=1)
    briefId: str = ""
    planningSlotId: str = ""
    dayNumber: int = Field(default=0, ge=0, le=366)
    experienceFamily: str = Field(min_length=1)
    activityMode: str = Field(min_length=1)
    intentType: str = Field(min_length=1)
    requirementLevel: RequirementLevel
    entityBindingMode: EntityBindingMode
    exactEntity: Optional[str] = None
    semanticFacets: list[str] = Field(default_factory=list)
    keywordVariants: list[str] = Field(min_length=1)
    preferredPlaceFacets: list[str] = Field(default_factory=list)
    rejectedPlaceFacets: list[str] = Field(default_factory=list)
    queryPlans: list[SearchQueryPlan] = Field(min_length=1)
    scoringPolicy: SearchScoringPolicy
    fallbackPolicy: SearchFallbackPolicy
    coveragePolicy: SearchCoveragePolicy
    budgetPolicy: SearchBudgetPolicy
    excludedPhysicalPoiIds: list[str] = Field(default_factory=list)
    sourceEvidence: SearchSourceEvidence
    exclusionFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    executionFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    originalExperienceFamily: Optional[str] = None
    experienceShape: ExperienceShape = "single_poi"
    experienceGoal: str = ""
    desiredSignals: list[str] = Field(default_factory=list)
    avoidSignals: list[str] = Field(default_factory=list)
    evidencePolicy: dict[str, Any] = Field(default_factory=dict)
    groundingPolicy: dict[str, Any] = Field(default_factory=dict)
    routeContext: dict[str, Any] = Field(default_factory=dict)
    assignedMealFamily: Optional[str] = None
    experienceSpecPolicy: dict[str, Any] = Field(default_factory=dict)
    intentFingerprint: str = ""

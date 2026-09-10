from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from src.models.poi import POI
from src.models.poi_search_profile import PoiSearchProfile


PoiIntentType = Literal[
    "campus_visit",
    "night_view",
    "landmark",
    "museum",
    "park",
    "meal",
    "rest",
    "shopping",
    "area_walk",
    "local_culture",
]

PoiSpecificity = Literal["exact_entity", "area", "functional", "composite"]
EntityBindingMode = Literal["category", "exact_entity", "legacy"]


@dataclass
class PoiIntent:
    raw_need: str
    city: str
    day_number: Optional[int]
    time_window: str
    intent_type: PoiIntentType
    specificity: PoiSpecificity
    search_queries: list[str] = field(default_factory=list)
    preferred_types: list[str] = field(default_factory=list)
    rejected_types: list[str] = field(default_factory=list)
    selection_rules: list[str] = field(default_factory=list)
    ask_user_only_if: list[str] = field(default_factory=list)
    candidate_hints: list[str] = field(default_factory=list)
    hint_policy: str = "no_hint"
    semantic_context: str = ""
    target_count: int = 1
    # Internal grounding budget target. Portfolio may need enough distinct
    # evidence for repeated occurrences while the consuming pool still owns a
    # single slot. This is deliberately not part of the Agent schema.
    candidate_evidence_target_count: Optional[int] = None
    # Staged planning must not infer exact-entity semantics from raw_need.
    # Legacy mode preserves non-staged callers until they provide the explicit
    # binding contract; all DaySlot/IntentPool paths set category/exact_entity.
    entity_binding_mode: EntityBindingMode = "legacy"
    exact_entity: Optional[str] = None
    # Internal-only consumer contracts. They are compiled by the server from
    # the frozen IntentPool/meal assignment and are deliberately omitted from
    # the Agent schema.
    optional_experience_family: Optional[str] = None
    assigned_meal_family: Optional[str] = None
    # Provider-neutral, server-compiled execution contract. It is deliberately
    # internal-only and must never be exposed through ``to_agent_schema``.
    search_profile: Optional[PoiSearchProfile] = None

    def to_agent_schema(self) -> dict:
        return {
            "rawNeed": self.raw_need,
            "city": self.city,
            "dayNumber": self.day_number,
            "timeWindow": self.time_window,
            "intentType": self.intent_type,
            "specificity": self.specificity,
            "searchQueries": self.search_queries,
            "preferredTypes": self.preferred_types,
            "rejectedTypes": self.rejected_types,
            "selectionRules": self.selection_rules,
            "askUserOnlyIf": self.ask_user_only_if,
            "candidateHints": self.candidate_hints,
            "hintPolicy": self.hint_policy,
            "entityBindingMode": self.entity_binding_mode,
            "exactEntity": self.exact_entity,
        }


@dataclass
class IntentPool:
    pool_id: str
    raw_need: str
    city: str
    intent_type: PoiIntentType
    target_count: int
    brief_id: str = ""
    requirement_level: Literal["required", "soft", "optional"] = "optional"
    goal_id: Optional[str] = None
    soft_goal_id: Optional[str] = None
    optional_experience_family: Optional[str] = None
    preferred_types: list[str] = field(default_factory=list)
    rejected_types: list[str] = field(default_factory=list)
    route_preference: dict[str, Any] = field(default_factory=dict)
    assign_to_slots: list[str] = field(default_factory=list)
    candidate_hints: list[str] = field(default_factory=list)
    hint_policy: str = "no_hint"
    entity_binding_mode: EntityBindingMode = "category"
    exact_entity: Optional[str] = None
    experience_shape: str = "single_poi"
    experience_goal: str = ""
    desired_signals: list[str] = field(default_factory=list)
    avoid_signals: list[str] = field(default_factory=list)
    evidence_policy: dict[str, Any] = field(default_factory=dict)
    grounding_policy: dict[str, Any] = field(default_factory=dict)
    route_context: dict[str, Any] = field(default_factory=dict)
    intent_fingerprint: str = ""
    # Model-authored search hypotheses rebound by the server to exact meal
    # occurrences.  They never carry a final restaurant identity or provider
    # fact; Simple Open/Portfolio must ground them through real candidate data.
    meal_experience_briefs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CandidateScore:
    candidate: Any
    score: float
    components: dict[str, float]
    rejected_reasons: list[str] = field(default_factory=list)


@dataclass
class DaySlot:
    slot_id: str
    day_number: int
    date: Optional[str]
    time_window: str
    start_time: str
    duration_minutes: int
    kind: str
    raw_need: str
    route_anchor: bool
    priority: int = 50
    notes: str = ""
    requirement_level: str = "soft"
    experience_shape: str = "single_poi"
    experience_goal: str = ""
    desired_signals: list[str] = field(default_factory=list)
    avoid_signals: list[str] = field(default_factory=list)
    evidence_policy: dict[str, Any] = field(default_factory=dict)
    grounding_policy: dict[str, Any] = field(default_factory=dict)
    route_context: dict[str, Any] = field(default_factory=dict)
    intent_fingerprint: str = ""
    # Server-sealed goal occurrence lineage.  These fields are populated only
    # after the accepted Controller directive has been recompiled by
    # ConstraintLedgerCompiler + GoalOccurrenceCompiler; a model-authored slot
    # id is never treated as an occurrence id.
    goal_id: str = ""
    source_goal_id: str = ""
    occurrence_id: str = ""
    pool_id: str = ""
    lineage_authority: str = ""


@dataclass
class PersistableSegmentPlan:
    day_number: int
    start_time: str
    duration_minutes: int
    kind: str
    route_anchor: bool
    selected_poi: Optional[POI]
    display_title: str
    notes: str
    grounding_status: str
    ticket_status: str
    # ``route_anchor`` is a creative/density planning role.  It does not say
    # whether a materialized physical stop belongs in the day's travel chain.
    # Simple Open sets this field explicitly once a canonical AMap stop exists.
    # ``None`` preserves legacy callers that still use route_anchor as their
    # only route-membership fact; new production plans must write True/False.
    requires_route_edge: Optional[bool] = None
    date: Optional[str] = None
    transport_mode: str = "walk"
    estimated_cost: float = 0.0
    raw_need: str = ""
    intent_type: str = ""
    goal_id: Optional[str] = None
    requirement_level: str = ""
    # None means the plan did not carry an explicit requirement fact and the
    # persisted writer may recover it from the request contract. False is an
    # explicit optional fact and must never be upgraded by an intent-level
    # contract fallback.
    required: Optional[bool] = None
    # Exact server-compiled DaySlot identity. It is carried separately from
    # display text so a partial timeline can preserve auditable slot lineage.
    planning_slot_id: str = ""
    creative_brief_id: str = ""
    pool_id: str = ""
    source_goal_id: str = ""
    occurrence_id: str = ""
    lineage_authority: str = ""
    # Semantic preferences never imply a clock.  Exact clocks belong only in
    # schedule_constraints and are honored when they came from a user or
    # verified Provider fact.  schedule_decision is server-derived.
    schedule_preference: dict[str, Any] = field(default_factory=dict)
    schedule_constraints: dict[str, Any] = field(default_factory=dict)
    schedule_decision: dict[str, Any] = field(default_factory=dict)


@dataclass
class GroundingRunContext:
    seen_intent_keys: set[str] = field(default_factory=set)
    seen_amap_ids_by_day: dict[int, set[str]] = field(default_factory=dict)
    selected_by_slot: dict[str, POI] = field(default_factory=dict)
    provider_failures: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rate_limited: bool = False
    rate_limit_reason: str = ""
    poi_request_budget: int = 18
    route_aware_budget: int = 4
    route_aware_calls: int = 0

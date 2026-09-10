from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.api.schemas.planning import FeasibilityReportResponse, LocalReplanSuggestionResponse, PlanningRunResponse


class ItineraryGenerateRequest(BaseModel):
    inspiration_set_id: str = Field(alias="inspirationSetId")
    city: str
    date_range: Optional[dict] = Field(default=None, alias="dateRange")
    preference_profile_id: Optional[str] = Field(default=None, alias="preferenceProfileId")
    preference_summary: Optional[str] = Field(default=None, alias="preferenceSummary")
    planning_context: Optional[dict] = Field(default=None, alias="planningContext")

    model_config = {"populate_by_name": True}


class ItineraryEditRequest(BaseModel):
    operation: str
    segment_id: str = Field(alias="segmentId")
    value: str
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId")
    preference_summary: str = Field(default="", alias="preferenceSummary")
    planning_context: Optional[dict] = Field(default=None, alias="planningContext")

    model_config = {"populate_by_name": True}


class PoiResponse(BaseModel):
    id: str
    amap_id: Optional[str] = Field(default=None, alias="amapId")
    parent_poi_id: Optional[str] = Field(default=None, alias="parentPoiId")
    name: str
    type: str = ""
    city: str
    district: str = ""
    address: str = ""
    category: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    photo_url: Optional[str] = Field(default=None, alias="photoUrl")
    source: str
    source_note: str = Field(default="", alias="sourceNote")
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    confidence: float
    photos: list[dict] = Field(default_factory=list)
    provider_type_code: Optional[str] = Field(default=None, alias="providerTypeCode")
    tags: list[str] = Field(default_factory=list)
    source_claims: list[dict] = Field(default_factory=list, alias="sourceClaims")
    grounding_status: str = Field(default="draft_only", alias="groundingStatus")
    map_ready: bool = Field(default=False, alias="mapReady")
    routeable: bool = False
    matched_amap_name: Optional[str] = Field(default=None, alias="matchedAmapName")
    poi_specificity: str = Field(default="exact_entity", alias="poiSpecificity")
    intent_type: Optional[str] = Field(default=None, alias="intentType")
    needs_concrete_poi: bool = Field(default=False, alias="needsConcretePoi")
    grounding: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ItinerarySegmentResponse(BaseModel):
    id: str
    start_time: str = Field(alias="startTime")
    end_time: str = Field(alias="endTime")
    kind: str
    poi: PoiResponse
    transport_mode: str = Field(alias="transportMode")
    estimated_cost: float = Field(alias="estimatedCost")
    estimate_metadata: dict = Field(default_factory=dict, alias="estimateMetadata")
    semantic_metadata: dict = Field(default_factory=dict, alias="semanticMetadata")
    intent_type: Optional[str] = Field(default=None, alias="intentType")
    intent_slot_id: Optional[str] = Field(default=None, alias="intentSlotId")
    raw_need: str = Field(default="", alias="rawNeed")
    grounding_status: str = Field(default="draft_only", alias="groundingStatus")
    route_anchor: bool = Field(default=False, alias="routeAnchor")
    required: bool = False
    user_locked: bool = Field(default=False, alias="userLocked")
    notes: str

    model_config = {"populate_by_name": True}


class PendingTimelineSlotResponse(BaseModel):
    id: str
    planning_slot_id: str = Field(alias="planningSlotId")
    brief_id: str = Field(default="", alias="briefId")
    pool_id: Optional[str] = Field(default=None, alias="poolId")
    day_number: int = Field(alias="dayNumber")
    time_window: str = Field(default="", alias="timeWindow")
    start_time: str = Field(default="", alias="startTime")
    end_time: str = Field(default="", alias="endTime")
    duration_minutes: int = Field(default=0, alias="durationMinutes")
    raw_need: str = Field(default="", alias="rawNeed")
    intent_type: str = Field(default="", alias="intentType")
    kind: str = "activity"
    state: str = "pending"
    label: str = "待补行程"
    timing_status: str = Field(default="awaiting_route_confirmation", alias="timingStatus")
    timing_basis: str = Field(default="", alias="timingBasis")
    constraint_summary: str = Field(default="", alias="constraintSummary")
    placement_after_segment_id: Optional[str] = Field(default=None, alias="placementAfterSegmentId")
    placement_before_segment_id: Optional[str] = Field(default=None, alias="placementBeforeSegmentId")

    model_config = {"populate_by_name": True}


class ItineraryDayResponse(BaseModel):
    id: str
    day_number: int = Field(alias="dayNumber")
    title: str = ""
    date: Optional[str] = None
    weather_summary: str = Field(alias="weatherSummary")
    risk_summary: str = Field(alias="riskSummary")
    total_estimated_cost: float = Field(alias="totalEstimatedCost")
    segments: list[ItinerarySegmentResponse]
    pending_slots: list[PendingTimelineSlotResponse] = Field(
        default_factory=list,
        alias="pendingSlots",
    )

    model_config = {"populate_by_name": True}


class RouteOptionResponse(BaseModel):
    id: str
    from_segment_id: Optional[str] = Field(default=None, alias="fromSegmentId")
    to_segment_id: Optional[str] = Field(default=None, alias="toSegmentId")
    from_poi_id: str = Field(alias="fromPoiId")
    to_poi_id: str = Field(alias="toPoiId")
    provider: str
    mode: str
    label: str
    is_selected: bool = Field(alias="isSelected")
    sort_order: int = Field(alias="sortOrder")
    transport_mode: str = Field(alias="transportMode")
    distance_meters: int = Field(alias="distanceMeters")
    duration_seconds: int = Field(alias="durationSeconds")
    duration_minutes: int = Field(alias="durationMinutes")
    cost_amount: float = Field(alias="costAmount")
    cost_currency: str = Field(alias="costCurrency")
    cost_estimate: float = Field(alias="costEstimate")
    crowding_risk: str = Field(alias="crowdingRisk")
    source: str
    polyline: list[list[float]] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    provider_payload: dict = Field(default_factory=dict, alias="providerPayload")
    error: Optional[dict] = None
    status: str = "verified"
    queried_at: datetime = Field(alias="queriedAt")

    model_config = {"populate_by_name": True}


class WeatherSignalResponse(BaseModel):
    id: str
    city: str
    date: str
    hourly_forecast: list[dict] = Field(alias="hourlyForecast")
    daily_summary: str = Field(alias="dailySummary")
    risk_level: str = Field(alias="riskLevel")
    purpose_impact_reason: str = Field(alias="purposeImpactReason")
    source: str
    data_status: str = Field(alias="dataStatus")
    confidence: float
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    user_visible_caveat: str = Field(default="", alias="userVisibleCaveat")
    provider_name: str = Field(default="", alias="providerName")
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    queried_at: datetime = Field(alias="queriedAt")

    model_config = {"populate_by_name": True}


class TrafficCrowdingSignalResponse(BaseModel):
    id: str
    route_option_id: str = Field(alias="routeOptionId")
    real_data_available: bool = Field(alias="realDataAvailable")
    crowding_level: str = Field(alias="crowdingLevel")
    estimated_reason: str = Field(alias="estimatedReason")
    recommended_departure_adjustment: str = Field(alias="recommendedDepartureAdjustment")
    source: str
    queried_at: datetime = Field(alias="queriedAt")

    model_config = {"populate_by_name": True}


class POIRiskAlertResponse(BaseModel):
    id: str
    plan_id: str = Field(alias="planId")
    segment_id: str = Field(alias="segmentId")
    poi_name: str = Field(alias="poiName")
    status: str
    summary: str
    source_name: str = Field(alias="sourceName")
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    sources: list[dict] = Field(default_factory=list)
    confidence: float
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    user_visible_caveat: str = Field(default="", alias="userVisibleCaveat")
    queried_at: datetime = Field(alias="queriedAt")

    model_config = {"populate_by_name": True}


class TicketLookupResultResponse(BaseModel):
    id: str
    segment_id: str = Field(alias="segmentId")
    ticket_type: str = Field(alias="ticketType")
    status: str
    price_estimate: float = Field(alias="priceEstimate")
    booking_url: str = Field(alias="bookingUrl")
    source_name: str = Field(alias="sourceName")
    source_url: str = Field(alias="sourceUrl")
    credibility_rank: str = Field(alias="credibilityRank")
    queried_at: datetime = Field(alias="queriedAt")
    caveat: str
    provider_name: str = Field(alias="providerName")
    fallback_used: bool = Field(alias="fallbackUsed")
    provider_failure_reason: Optional[str] = Field(default=None, alias="providerFailureReason")
    confidence: float

    model_config = {"populate_by_name": True}


class BudgetBreakdownResponse(BaseModel):
    tier: str = "unknown"
    numeric_target: Optional[float] = Field(default=None, alias="numericTarget")
    known_activity_cost: float = Field(default=0, alias="knownActivityCost")
    known_meal_cost: float = Field(default=0, alias="knownMealCost")
    known_transport_cost: float = Field(default=0, alias="knownTransportCost")
    known_total: float = Field(default=0, alias="knownTotal")
    provisional_min: float = Field(default=0, alias="provisionalMin")
    provisional_preferred: float = Field(default=0, alias="provisionalPreferred")
    provisional_max: float = Field(default=0, alias="provisionalMax")
    unknown_items: list[str] = Field(default_factory=list, alias="unknownItems")
    is_complete: bool = Field(default=False, alias="isComplete")
    invariant_valid: bool = Field(default=True, alias="invariantValid")

    @model_validator(mode="after")
    def validate_budget_order(self):
        valid = (
            0 <= self.known_total
            <= self.provisional_min
            <= self.provisional_preferred
            <= self.provisional_max
        )
        if not valid:
            raise ValueError(
                "budget invariant requires 0 <= knownTotal <= provisionalMin <= provisionalPreferred <= provisionalMax"
            )
        self.invariant_valid = True
        return self

    model_config = {"populate_by_name": True}


class EnrichmentDimensionStatusResponse(BaseModel):
    status: str
    trip_start_date: Optional[str] = Field(default=None, alias="tripStartDate")
    forecast_available_from: Optional[str] = Field(default=None, alias="forecastAvailableFrom")
    queried_at: Optional[datetime] = Field(default=None, alias="queriedAt")
    next_action: str = Field(default="refresh_now", alias="nextAction")
    source: Optional[str] = None
    item_count: int = Field(default=0, alias="itemCount")

    model_config = {"populate_by_name": True}


class OnlineEnrichmentStatusResponse(BaseModel):
    weather: EnrichmentDimensionStatusResponse
    risk: EnrichmentDimensionStatusResponse
    reservation: EnrichmentDimensionStatusResponse

    model_config = {"populate_by_name": True}


class RouteCoverageResponse(BaseModel):
    grounded_anchor_required: int = Field(default=0, alias="groundedAnchorRequired")
    grounded_anchor_covered: int = Field(default=0, alias="groundedAnchorCovered")
    unresolved_slot_count: int = Field(default=0, alias="unresolvedSlotCount")
    complete_door_to_door_status: str = Field(default="not_required", alias="completeDoorToDoorStatus")

    model_config = {"populate_by_name": True}


class ItineraryPlanResponse(BaseModel):
    id: str
    title: str
    city: str
    template_type: str = Field(alias="templateType")
    budget_target: Optional[float] = Field(default=None, alias="budgetTarget")
    budget_tier: str = Field(default="unknown", alias="budgetTier")
    budget_estimate: float = Field(alias="budgetEstimate")
    budget_breakdown: Optional[BudgetBreakdownResponse] = Field(default=None, alias="budgetBreakdown")
    route_coverage: Optional[RouteCoverageResponse] = Field(default=None, alias="routeCoverage")
    schedule_diagnostics: dict = Field(default_factory=dict, alias="scheduleDiagnostics")
    online_enrichment: Optional[OnlineEnrichmentStatusResponse] = Field(default=None, alias="onlineEnrichment")
    budget_delta_explanation: str = Field(alias="budgetDeltaExplanation")
    decision_rationale: str = Field(alias="decisionRationale")
    status: str
    days: list[ItineraryDayResponse]
    route_options: list[RouteOptionResponse] = Field(alias="routeOptions")
    weather_signals: list[WeatherSignalResponse] = Field(alias="weatherSignals")
    traffic_crowding_signals: list[TrafficCrowdingSignalResponse] = Field(alias="trafficCrowdingSignals")
    poi_risk_alerts: list[POIRiskAlertResponse] = Field(default_factory=list, alias="poiRiskAlerts")
    ticket_lookup_results: list[TicketLookupResultResponse] = Field(default_factory=list, alias="ticketLookupResults")
    visit_facts_by_segment: dict[str, dict] = Field(default_factory=dict, alias="visitFactsBySegment")
    route_warnings: list[str] = Field(default_factory=list, alias="routeWarnings")
    feasibility_report: Optional[FeasibilityReportResponse] = Field(default=None, alias="feasibilityReport")
    local_replan_suggestions: list[LocalReplanSuggestionResponse] = Field(
        default_factory=list,
        alias="localReplanSuggestions",
    )

    model_config = {"populate_by_name": True}


class RouteSelectRequest(BaseModel):
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId")
    preference_summary: str = Field(default="", alias="preferenceSummary")
    planning_context: Optional[dict] = Field(default=None, alias="planningContext")

    model_config = {"populate_by_name": True}


class RouteOptimizeRequest(BaseModel):
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId")
    preference_summary: str = Field(default="", alias="preferenceSummary")
    planning_context: Optional[dict] = Field(default=None, alias="planningContext")
    day_id: Optional[str] = Field(default=None, alias="dayId")
    optimization_objective: Literal["balanced", "fastest", "cheapest"] = Field(
        default="balanced",
        alias="optimizationObjective",
    )

    model_config = {"populate_by_name": True}


class SavedItineraryVersionResponse(BaseModel):
    id: str
    session_id: str = Field(alias="sessionId")
    plan_id: str = Field(alias="planId")
    version_id: str = Field(alias="versionId")
    title: str
    created_at: str = Field(alias="createdAt")

    model_config = {"populate_by_name": True}


class SavedItineraryVersionsResponse(BaseModel):
    saved_versions: list[SavedItineraryVersionResponse] = Field(default_factory=list, alias="savedVersions")

    model_config = {"populate_by_name": True}


class ItineraryExportJsonResponse(BaseModel):
    exported_at: str = Field(alias="exportedAt")
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    plan_id: str = Field(alias="planId")
    itinerary_plan: ItineraryPlanResponse = Field(alias="itineraryPlan")
    route_options: list[RouteOptionResponse] = Field(alias="routeOptions")
    risk_signals: dict = Field(alias="riskSignals")

    model_config = {"populate_by_name": True}


class LocalReplanSuggestionRequest(BaseModel):
    user_input: str = Field(default="", alias="userInput")
    preference_summary: str = Field(default="", alias="preferenceSummary")
    planning_context: Optional[dict] = Field(default=None, alias="planningContext")

    model_config = {"populate_by_name": True}


class ItineraryPlanEnvelope(BaseModel):
    plan: ItineraryPlanResponse
    planning_run: Optional[PlanningRunResponse] = Field(default=None, alias="planningRun")

    model_config = {"populate_by_name": True}


class PlanComparisonResponse(BaseModel):
    comparison_id: str = Field(alias="comparisonId")
    provider_name: str = Field(alias="providerName")
    fallback_used: bool = Field(alias="fallbackUsed")
    user_visible_caveat: str = Field(alias="userVisibleCaveat")
    plans: list[ItineraryPlanResponse]
    planning_run: Optional[PlanningRunResponse] = Field(default=None, alias="planningRun")

    model_config = {"populate_by_name": True}

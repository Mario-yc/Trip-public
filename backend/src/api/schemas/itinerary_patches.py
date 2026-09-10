from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.api.schemas.itineraries import ItineraryPlanResponse
from src.api.schemas.maps import MapPoiResponse
from src.api.schemas.planning import PlanningRunResponse


ItineraryPatchOperationName = Literal[
    "replace_itinerary",
    "replace_trip_title",
    "replace_day_title",
    "replace_segment_start_time",
    "replace_segment_duration",
    "replace_transport_mode",
    "add_day",
    "add_segment",
    "replace_segment_poi",
    "replace_segment_poi_from_candidate",
    "expand_area_poi_candidates",
    "expand_meal_poi_candidates",
    "confirm_poi_anchor",
    "refresh_ticket_for_segment",
    "refresh_routes_for_day",
    "remove_segment",
    "move_segment",
    "reorder_segments",
    "update_segment_notes",
]


class ItineraryPatchOperation(BaseModel):
    op: ItineraryPatchOperationName
    value: Optional[str] = None
    day_id: Optional[str] = Field(default=None, alias="dayId")
    target_day_id: Optional[str] = Field(default=None, alias="targetDayId")
    segment_id: Optional[str] = Field(default=None, alias="segmentId")
    start_time: Optional[str] = Field(default=None, alias="startTime")
    title: Optional[str] = None
    kind: Optional[str] = None
    intent_type: Optional[str] = Field(default=None, alias="intentType")
    notes: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, alias="durationMinutes")
    estimated_cost: Optional[float] = Field(default=None, alias="estimatedCost")
    transport_mode: Optional[str] = Field(default=None, alias="transportMode")
    allow_unresolved: bool = Field(default=False, alias="allowUnresolved")
    amap_poi: Optional[MapPoiResponse] = Field(default=None, alias="amapPoi")
    candidate_id: Optional[str] = Field(default=None, alias="candidateId")
    radius: Optional[int] = None
    full_itinerary: Optional[dict] = Field(default=None, alias="fullItinerary")
    ordered_segment_ids: Optional[list[str]] = Field(default=None, alias="orderedSegmentIds")
    semantic_metadata: Optional[dict[str, Any]] = Field(default=None, alias="semanticMetadata")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ItineraryPatchRequest(BaseModel):
    # Public callers may label a local replan for UI/audit purposes, but cannot
    # claim an internal writer role such as ``agent`` or
    # ``user_timeline_mutation``.  The route gate itself is operation-based.
    source_type: Literal["manual", "local_replan"] = Field(default="manual", alias="sourceType")
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId")
    source_turn_id: Optional[str] = Field(default=None, alias="sourceTurnId")
    preference_summary: str = Field(default="", alias="preferenceSummary")
    planning_context: dict = Field(default_factory=dict, alias="planningContext")
    operations: list[ItineraryPatchOperation]

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ItineraryPatchSummaryResponse(BaseModel):
    id: str
    validation_status: str = Field(alias="validationStatus")
    metadata: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ItineraryVersionResponse(BaseModel):
    id: str
    version_number: int = Field(alias="versionNumber")
    source_type: str = Field(alias="sourceType")

    model_config = {"populate_by_name": True}


class PendingPoiCandidatePatchResponse(BaseModel):
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


class ItineraryPatchResponse(BaseModel):
    itinerary: ItineraryPlanResponse
    patch: ItineraryPatchSummaryResponse
    version: ItineraryVersionResponse
    validation_errors: list[str] = Field(default_factory=list, alias="validationErrors")
    planning_run: Optional[PlanningRunResponse] = Field(default=None, alias="planningRun")
    pending_poi_candidates: list[PendingPoiCandidatePatchResponse] = Field(
        default_factory=list, alias="pendingPoiCandidates"
    )

    model_config = {"populate_by_name": True}


class ItineraryRestoreVersionRequest(BaseModel):
    version_id: str = Field(alias="versionId")
    reason: Optional[str] = None

    model_config = {"populate_by_name": True}


class ItineraryRestoreVersionResponse(BaseModel):
    itinerary: ItineraryPlanResponse
    version: ItineraryVersionResponse

    model_config = {"populate_by_name": True}

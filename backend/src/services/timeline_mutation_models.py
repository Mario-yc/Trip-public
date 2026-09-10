from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.api.schemas.maps import MapPoiResponse


TimelineMutationOperation = Literal[
    "add_segment",
    "replace_poi",
    "remove_segment",
    "set_start_time",
    "set_duration",
    "set_transport_mode",
]


class TimelineMutationSelector(BaseModel):
    day_number: Optional[int] = Field(default=None, alias="dayNumber", ge=1, strict=True)
    intent_type: Optional[str] = Field(default=None, alias="intentType")
    current_text: Optional[str] = Field(default=None, alias="currentText")
    from_text: Optional[str] = Field(default=None, alias="fromText")
    to_text: Optional[str] = Field(default=None, alias="toText")
    time_window: Optional[str] = Field(default=None, alias="timeWindow")
    ordinal: Optional[int] = None
    kind: Optional[str] = None

    model_config = {"populate_by_name": True, "extra": "forbid"}


class TimelineMutationReplacement(BaseModel):
    poi_query: Optional[str] = Field(default=None, alias="poiQuery")
    poi_query_mode: Optional[Literal["exact_entity", "category_intent"]] = Field(default=None, alias="poiQueryMode")
    start_time: Optional[str] = Field(default=None, alias="startTime")
    duration_minutes: Optional[int] = Field(default=None, alias="durationMinutes")
    transport_mode: Optional[str] = Field(default=None, alias="transportMode")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PendingSlotSelectionContext(BaseModel):
    schema_version: Literal["pending-slot-selection-v1"] = Field(
        default="pending-slot-selection-v1", alias="schemaVersion"
    )
    selection_source: Literal["user_chat_choice", "user_map_selection", "user_manual_input"] = Field(
        alias="selectionSource"
    )
    planning_selection_root_turn_id: str = Field(alias="planningSelectionRootTurnId")
    root_portfolio_id: str = Field(alias="rootPortfolioId")
    focus_brief_id: str = Field(alias="focusBriefId")
    request_contract_fingerprint: str = Field(alias="requestContractFingerprint")
    pool_id: str = Field(alias="poolId")
    planning_slot_id: str = Field(alias="planningSlotId")
    day_number: int = Field(alias="dayNumber", ge=1, strict=True)
    candidate_record_id: Optional[str] = Field(default=None, alias="candidateRecordId")
    amap_id: str = Field(alias="amapId")
    asserted_base_version_id: str = Field(alias="baseVersionId")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class TimelineMutationIntent(BaseModel):
    schema_version: Literal["timeline-mutation-intent-v1"] = Field(
        default="timeline-mutation-intent-v1", alias="schemaVersion"
    )
    operation: TimelineMutationOperation
    selector: TimelineMutationSelector
    replacement: TimelineMutationReplacement = Field(default_factory=TimelineMutationReplacement)
    preserve: list[str] = Field(default_factory=lambda: ["other_segments", "other_days", "user_locked_times"])
    confidence: float = Field(default=0.8, ge=0, le=1)
    source: Literal["deterministic_parser", "model_semantic_extractor", "structured_ui"] = "model_semantic_extractor"
    source_text: str = Field(default="", alias="sourceText")
    pending_slot_selection: Optional[PendingSlotSelectionContext] = Field(default=None, alias="pendingSlotSelection")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def validate_operation_payload(self):
        if self.operation in {"add_segment", "replace_poi"} and not self.replacement.poi_query:
            raise ValueError(f"{self.operation} requires replacement.poiQuery")
        if self.operation == "set_start_time" and not self.replacement.start_time:
            raise ValueError("set_start_time requires replacement.startTime")
        if self.operation == "set_duration" and not self.replacement.duration_minutes:
            raise ValueError("set_duration requires replacement.durationMinutes")
        if self.operation == "set_transport_mode" and not self.replacement.transport_mode:
            raise ValueError("set_transport_mode requires replacement.transportMode")
        return self


class TimelineSegmentDescriptor(BaseModel):
    segment_id: str = Field(alias="segmentId")
    day_id: str = Field(alias="dayId")
    day_number: int = Field(alias="dayNumber")
    segment_order: int = Field(alias="segmentOrder")
    start_time: str = Field(alias="startTime")
    end_time: str = Field(alias="endTime")
    kind: str
    poi_name: str = Field(alias="poiName")
    poi_amap_id: Optional[str] = Field(default=None, alias="poiAmapId")
    poi_canonical_name: str = Field(default="", alias="poiCanonicalName")
    poi_longitude: Optional[float] = Field(default=None, alias="poiLongitude")
    poi_latitude: Optional[float] = Field(default=None, alias="poiLatitude")
    intent_type: Optional[str] = Field(default=None, alias="intentType")
    intent_slot_id: Optional[str] = Field(default=None, alias="intentSlotId")
    raw_need: str = Field(default="", alias="rawNeed")
    grounding_status: str = Field(default="draft_only", alias="groundingStatus")
    route_anchor: bool = Field(default=False, alias="routeAnchor")
    required: bool = False
    user_locked: bool = Field(default=False, alias="userLocked")
    aliases: list[str] = Field(default_factory=list)
    transport_mode: str = Field(default="", alias="transportMode")

    model_config = {"populate_by_name": True}


class BoundTimelineMutation(BaseModel):
    mutation_id: str = Field(alias="mutationId")
    session_id: str = Field(alias="sessionId")
    plan_id: str = Field(alias="planId")
    base_version_id: str = Field(alias="baseVersionId")
    base_snapshot_fingerprint: str = Field(alias="baseSnapshotFingerprint")
    intent: TimelineMutationIntent
    target_segment_ids: list[str] = Field(default_factory=list, alias="targetSegmentIds")
    target_day_id: Optional[str] = Field(default=None, alias="targetDayId")
    target_descriptors: list[TimelineSegmentDescriptor] = Field(default_factory=list, alias="targetDescriptors")
    adjacent_route_pair_ids: list[list[str]] = Field(default_factory=list, alias="adjacentRoutePairIds")
    adjacent_descriptors: list[TimelineSegmentDescriptor] = Field(default_factory=list, alias="adjacentDescriptors")
    insertion_index: Optional[int] = Field(default=None, alias="insertionIndex")
    binding_status: Literal["unique", "ambiguous", "target_not_found", "stale"] = Field(alias="bindingStatus")
    binding_evidence: list[str] = Field(default_factory=list, alias="bindingEvidence")
    # This value is injected only from the active, persisted itinerary version
    # (or an internal server call).  It is deliberately not part of
    # TimelineMutationIntent, whose payload may originate from the model/UI.
    route_decision_contract: Optional[dict[str, Any]] = Field(default=None, alias="routeDecisionContract")

    model_config = {"populate_by_name": True}


class TimelineMutationResolution(BaseModel):
    status: Literal["not_required", "unique_safe_candidate", "material_choice", "no_safe_candidate", "provider_failure"]
    selected_poi: Optional[MapPoiResponse] = Field(default=None, alias="selectedPoi")
    candidates: list[MapPoiResponse] = Field(default_factory=list)
    semantic_decisions: list[dict[str, Any]] = Field(default_factory=list, alias="semanticDecisions")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")

    model_config = {"populate_by_name": True}


class MutationPostconditionSpec(BaseModel):
    operation: TimelineMutationOperation
    base_version_id: str = Field(alias="baseVersionId")
    target_segment_ids: list[str] = Field(alias="targetSegmentIds")
    target_day_id: Optional[str] = Field(default=None, alias="targetDayId")
    allowed_derived_segment_ids: list[str] = Field(default_factory=list, alias="allowedDerivedSegmentIds")
    allowed_schedule_metadata_segment_ids: list[str] = Field(
        default_factory=list, alias="allowedScheduleMetadataSegmentIds"
    )
    expected_poi_amap_id: Optional[str] = Field(default=None, alias="expectedPoiAmapId")
    expected_poi_name: Optional[str] = Field(default=None, alias="expectedPoiName")
    expected_intent_type: Optional[str] = Field(default=None, alias="expectedIntentType")
    expected_start_time: Optional[str] = Field(default=None, alias="expectedStartTime")
    expected_duration_minutes: Optional[int] = Field(default=None, alias="expectedDurationMinutes")
    expected_transport_mode: Optional[str] = Field(default=None, alias="expectedTransportMode")
    max_direct_changed_segment_count: int = Field(default=1, alias="maxDirectChangedSegmentCount")
    allow_derived_schedule_changes: bool = Field(default=True, alias="allowDerivedScheduleChanges")
    preserve: list[str] = Field(default_factory=list)
    require_version_delta: int = Field(default=1, alias="requireVersionDelta")
    expected_pending_slot_key: Optional[list[Any]] = Field(default=None, alias="expectedPendingSlotKey")
    expected_manual_placement_source: Optional[str] = Field(default=None, alias="expectedManualPlacementSource")
    expected_candidate_record_id: Optional[str] = Field(default=None, alias="expectedCandidateRecordId")

    model_config = {"populate_by_name": True}


class CompiledTimelineMutation(BaseModel):
    operations: list[ItineraryPatchOperation]
    postcondition: MutationPostconditionSpec


class TimelineMutationDiff(BaseModel):
    direct_changed_segment_ids: list[str] = Field(default_factory=list, alias="directChangedSegmentIds")
    derived_changed_segment_ids: list[str] = Field(default_factory=list, alias="derivedChangedSegmentIds")
    unexpected_changed_segment_ids: list[str] = Field(default_factory=list, alias="unexpectedChangedSegmentIds")
    changed_route_pair_ids: list[list[str]] = Field(default_factory=list, alias="changedRoutePairIds")
    version_delta: int = Field(default=0, alias="versionDelta")

    model_config = {"populate_by_name": True}


class TimelineMutationOutcome(BaseModel):
    mutation_id: str = Field(alias="mutationId")
    status: Literal["success", "needs_confirmation", "no_change", "failed", "rolled_back"]
    operation: TimelineMutationOperation
    base_version_id: str = Field(alias="baseVersionId")
    result_version_id: Optional[str] = Field(default=None, alias="resultVersionId")
    patch_id: Optional[str] = Field(default=None, alias="patchId")
    target_segment_ids: list[str] = Field(default_factory=list, alias="targetSegmentIds")
    change_summary: dict[str, Any] = Field(default_factory=dict, alias="changeSummary")
    direct_changed_segment_ids: list[str] = Field(default_factory=list, alias="directChangedSegmentIds")
    derived_changed_segment_ids: list[str] = Field(default_factory=list, alias="derivedChangedSegmentIds")
    unexpected_changed_segment_ids: list[str] = Field(default_factory=list, alias="unexpectedChangedSegmentIds")
    touched_route_pairs: list[list[str]] = Field(default_factory=list, alias="touchedRoutePairs")
    changed_route_pair_ids: list[list[str]] = Field(default_factory=list, alias="changedRoutePairIds")
    route_write_delta: int = Field(default=0, alias="routeWriteDelta")
    route_status: Optional[str] = Field(default=None, alias="routeStatus")
    postcondition_passed: bool = Field(default=False, alias="postconditionPassed")
    structural_verifier_passed: bool = Field(default=False, alias="structuralVerifierPassed")
    rollback_performed: bool = Field(default=False, alias="rollbackPerformed")
    warnings: list[str] = Field(default_factory=list)
    options: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error_code: Optional[str] = Field(default=None, alias="errorCode")
    existing_outcome: bool = Field(default=False, alias="existingOutcome")

    model_config = {"populate_by_name": True}

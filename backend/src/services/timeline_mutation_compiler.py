from __future__ import annotations

from typing import Any

from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.services.timeline_mutation_models import (
    BoundTimelineMutation,
    CompiledTimelineMutation,
    MutationPostconditionSpec,
    TimelineMutationResolution,
)
from src.services.itinerary_schedule_service import ItineraryScheduleService


class TimelineMutationCompiler:
    def compile(
        self,
        bound: BoundTimelineMutation,
        resolution: TimelineMutationResolution,
        *,
        before_snapshot: dict[str, Any] | None = None,
    ) -> CompiledTimelineMutation:
        if bound.binding_status != "unique":
            raise ValueError("Timeline mutation requires one uniquely bound target")
        intent = bound.intent
        operation: ItineraryPatchOperation
        if intent.operation == "add_segment":
            if not bound.target_day_id:
                raise ValueError("add_segment requires one uniquely bound day")
            if resolution.status != "unique_safe_candidate" or resolution.selected_poi is None:
                raise ValueError("add_segment requires one safe resolved AMap candidate")
            target_day_ids = self._day_segment_ids(before_snapshot or {}, bound.target_day_id)
            intent_type = str(intent.selector.intent_type or "")
            duration = intent.replacement.duration_minutes or self._default_duration(intent_type)
            transport_mode = intent.replacement.transport_mode or self._day_transport_mode(
                before_snapshot or {}, bound.target_day_id
            )
            operation = ItineraryPatchOperation(
                op="add_segment",
                dayId=bound.target_day_id,
                title=resolution.selected_poi.name,
                kind="meal" if intent_type == "meal" else "visit",
                intentType=intent_type or None,
                startTime=intent.replacement.start_time,
                durationMinutes=duration,
                transportMode=transport_mode,
                notes=f"intentType={intent_type}; userRequest={intent.source_text}",
                amapPoi=resolution.selected_poi,
                semanticMetadata=self._pending_slot_semantic_metadata(
                    bound,
                    selected_amap_id=resolution.selected_poi.id,
                    duration_minutes=duration,
                ),
            )
            pending = intent.pending_slot_selection
            return CompiledTimelineMutation(
                operations=[operation],
                postcondition=MutationPostconditionSpec(
                    operation=intent.operation,
                    baseVersionId=bound.base_version_id,
                    targetSegmentIds=[],
                    targetDayId=bound.target_day_id,
                    allowedDerivedSegmentIds=target_day_ids,
                    allowedScheduleMetadataSegmentIds=target_day_ids,
                    expectedPoiAmapId=resolution.selected_poi.id,
                    expectedPoiName=resolution.selected_poi.name,
                    expectedIntentType=intent_type or None,
                    expectedStartTime=intent.replacement.start_time,
                    expectedDurationMinutes=duration,
                    expectedTransportMode=transport_mode,
                    expectedPendingSlotKey=(
                        [pending.focus_brief_id, pending.pool_id, pending.planning_slot_id, pending.day_number]
                        if pending is not None
                        else None
                    ),
                    expectedManualPlacementSource=(pending.selection_source if pending is not None else None),
                    expectedCandidateRecordId=(pending.candidate_record_id if pending is not None else None),
                    preserve=intent.preserve,
                ),
            )
        if len(bound.target_segment_ids) != 1:
            raise ValueError("Timeline mutation requires one uniquely bound segment")
        target_id = bound.target_segment_ids[0]
        spec = MutationPostconditionSpec(
            operation=intent.operation,
            baseVersionId=bound.base_version_id,
            targetSegmentIds=[target_id],
            targetDayId=bound.target_descriptors[0].day_id,
            allowedDerivedSegmentIds=self._allowed_derived_segment_ids(before_snapshot or {}, target_id),
            allowedScheduleMetadataSegmentIds=self._target_day_segment_ids(before_snapshot or {}, target_id),
            expectedIntentType=bound.target_descriptors[0].intent_type,
            preserve=intent.preserve,
        )
        if intent.operation == "replace_poi":
            if resolution.status != "unique_safe_candidate" or resolution.selected_poi is None:
                raise ValueError("replace_poi requires one safe resolved AMap candidate")
            operation = ItineraryPatchOperation(
                op="replace_segment_poi",
                segmentId=target_id,
                amapPoi=resolution.selected_poi,
            )
            original_duration = self._minutes(bound.target_descriptors[0].end_time) - self._minutes(
                bound.target_descriptors[0].start_time
            )
            spec.expected_poi_amap_id = resolution.selected_poi.id
            spec.expected_poi_name = resolution.selected_poi.name
            spec.expected_duration_minutes = original_duration
        elif intent.operation == "remove_segment":
            operation = ItineraryPatchOperation(op="remove_segment", segmentId=target_id)
        elif intent.operation == "set_start_time":
            operation = ItineraryPatchOperation(
                op="replace_segment_start_time", segmentId=target_id, startTime=intent.replacement.start_time
            )
            spec.expected_start_time = intent.replacement.start_time
        elif intent.operation == "set_duration":
            operation = ItineraryPatchOperation(
                op="replace_segment_duration", segmentId=target_id, durationMinutes=intent.replacement.duration_minutes
            )
            spec.expected_duration_minutes = intent.replacement.duration_minutes
        elif intent.operation == "set_transport_mode":
            operation = ItineraryPatchOperation(
                op="replace_transport_mode", segmentId=target_id, value=intent.replacement.transport_mode
            )
            spec.expected_transport_mode = intent.replacement.transport_mode
        else:
            raise ValueError(f"Unsupported timeline mutation operation: {intent.operation}")
        operations = [operation]
        if intent.operation == "replace_poi":
            operations.append(
                ItineraryPatchOperation(
                    op="replace_segment_duration",
                    segmentId=target_id,
                    durationMinutes=spec.expected_duration_minutes,
                )
            )
        return CompiledTimelineMutation(operations=operations, postcondition=spec)

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _allowed_derived_segment_ids(snapshot: dict[str, Any], target_id: str) -> list[str]:
        """Only later segments in the target day may move as schedule cascade."""
        for day in snapshot.get("days") or []:
            segments = [item for item in day.get("segments") or [] if isinstance(item, dict)]
            target_index = next((index for index, item in enumerate(segments) if item.get("id") == target_id), None)
            if target_index is None:
                continue
            return [str(item["id"]) for item in segments[target_index + 1 :] if item.get("id")]
        return []

    @staticmethod
    def _target_day_segment_ids(snapshot: dict[str, Any], target_id: str) -> list[str]:
        for day in snapshot.get("days") or []:
            segments = [item for item in day.get("segments") or [] if isinstance(item, dict)]
            if not any(item.get("id") == target_id for item in segments):
                continue
            return [str(item["id"]) for item in segments if item.get("id") and item.get("id") != target_id]
        return []

    @staticmethod
    def _day_segment_ids(snapshot: dict[str, Any], day_id: str) -> list[str]:
        for day in snapshot.get("days") or []:
            if str(day.get("id") or "") != day_id:
                continue
            return [str(item["id"]) for item in day.get("segments") or [] if item.get("id")]
        return []

    @staticmethod
    def _day_transport_mode(snapshot: dict[str, Any], day_id: str) -> str:
        for day in snapshot.get("days") or []:
            if str(day.get("id") or "") != day_id:
                continue
            for segment in day.get("segments") or []:
                mode = str(segment.get("transportMode") or "").strip()
                if mode:
                    return mode
        return "transit"

    @staticmethod
    def _default_duration(intent_type: str) -> int:
        return {"campus_visit": 120, "museum": 120, "meal": 75}.get(intent_type, 90)

    @staticmethod
    def _pending_slot_semantic_metadata(
        bound: BoundTimelineMutation,
        *,
        selected_amap_id: str,
        duration_minutes: int,
    ) -> dict[str, Any] | None:
        pending = bound.intent.pending_slot_selection
        if pending is None:
            return None
        intent_type = str(bound.intent.selector.intent_type or "")
        return {
            "creativeBriefId": pending.focus_brief_id,
            "poolId": pending.pool_id,
            "planningSlotId": pending.planning_slot_id,
            "intentSlotId": pending.planning_slot_id,
            "dayNumber": pending.day_number,
            "intentType": intent_type,
            "manualPlacementSource": pending.selection_source,
            "sourceCandidateRecordId": pending.candidate_record_id,
            "selectedAmapId": selected_amap_id,
            "coveredPendingSlot": True,
            "groundingStatus": "verified_amap",
            "routeAnchor": True,
            "required": True,
            "scheduleConstraints": ItineraryScheduleService.schedule_constraint_payload(
                time_window=bound.intent.selector.time_window,
                start_time=bound.intent.replacement.start_time,
                duration=duration_minutes,
                intent_type=intent_type,
                source="pending_slot_selection",
            ),
        }

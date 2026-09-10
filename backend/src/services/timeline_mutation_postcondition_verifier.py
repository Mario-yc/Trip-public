from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.itinerary_schedule_service import ItineraryScheduleService
from src.services.timeline_mutation_models import MutationPostconditionSpec, TimelineMutationDiff


@dataclass(frozen=True)
class MutationPostconditionReport:
    passed: bool
    diff: TimelineMutationDiff
    errors: list[str]


class TimelineMutationPostconditionVerifier:
    def __init__(self):
        self.semantic_policy = IntentCandidateSemanticPolicy()

    def verify(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        spec: MutationPostconditionSpec,
        *,
        version_delta: int,
    ) -> MutationPostconditionReport:
        before_segments = self._segments(before)
        after_segments = self._segments(after)
        before_days = self._segment_days(before)
        after_days = self._segment_days(after)
        before_routes = self._route_signatures(before)
        after_routes = self._route_signatures(after)
        target_ids = set(spec.target_segment_ids)
        added_ids = set(after_segments) - set(before_segments)
        if spec.operation == "add_segment":
            target_ids = set(added_ids)
        allowed_derived_ids = set(spec.allowed_derived_segment_ids)
        allowed_schedule_metadata_ids = set(spec.allowed_schedule_metadata_segment_ids)
        direct: list[str] = []
        derived: list[str] = []
        unexpected: list[str] = []
        errors: list[str] = []

        for segment_id in sorted(set(before_segments) | set(after_segments)):
            old = before_segments.get(segment_id)
            new = after_segments.get(segment_id)
            if old == new:
                continue
            if segment_id in target_ids:
                direct.append(segment_id)
                continue
            changed_times = self._schedule_changed(old, new)
            outside_target_day = bool(
                "other_days" in spec.preserve
                and spec.target_day_id
                and (before_days.get(segment_id) or after_days.get(segment_id)) != spec.target_day_id
            )
            locked_time_changed = bool(
                "user_locked_times" in spec.preserve
                and self._is_user_locked(old)
                and changed_times
            )
            if outside_target_day:
                errors.append(f"segment outside target day changed: {segment_id}")
            if locked_time_changed:
                errors.append(f"user-locked segment time changed: {segment_id}")
            time_derived = bool(
                not outside_target_day
                and not locked_time_changed
                and self._only_derived_schedule_changed(old, new)
                and self._only_schedule_metadata_changed(old, new)
                and (not self._schedule_metadata_changed(old, new) or self._valid_schedule_metadata(new))
                and spec.allow_derived_schedule_changes
                and changed_times
                and segment_id in allowed_derived_ids
            )
            metadata_derived = bool(
                not outside_target_day
                and not locked_time_changed
                and self._only_derived_schedule_changed(old, new)
                and self._only_schedule_metadata_changed(old, new)
                and self._valid_schedule_metadata(new)
                and spec.allow_derived_schedule_changes
                and not changed_times
                and segment_id in allowed_schedule_metadata_ids
            )
            if time_derived or metadata_derived:
                derived.append(segment_id)
            else:
                unexpected.append(segment_id)
        if unexpected:
            errors.append(f"unexpected direct segment changes: {unexpected}")
        if len(direct) > spec.max_direct_changed_segment_count:
            errors.append("too many directly changed segments")
        if version_delta != spec.require_version_delta:
            errors.append(f"version delta must be {spec.require_version_delta}, got {version_delta}")

        target_id = spec.target_segment_ids[0] if spec.target_segment_ids else ""
        old_target = before_segments.get(target_id)
        target = after_segments.get(target_id)
        if spec.operation == "add_segment":
            if len(added_ids) != 1:
                errors.append(f"add_segment must create exactly one segment, got {len(added_ids)}")
            removed_ids = set(before_segments) - set(after_segments)
            if removed_ids:
                errors.append(f"add_segment removed existing segments: {sorted(removed_ids)}")
            added_id = next(iter(added_ids), "")
            added = after_segments.get(added_id)
            if added is not None:
                if after_days.get(added_id) != spec.target_day_id:
                    errors.append("added segment is outside target day")
                poi = added.get("poi") if isinstance(added.get("poi"), dict) else {}
                if str(poi.get("amapId") or "") != str(spec.expected_poi_amap_id or ""):
                    errors.append("added AMap POI identity does not match")
                if spec.expected_poi_name and str(poi.get("name") or "") != str(spec.expected_poi_name):
                    errors.append("added POI name does not match resolved candidate")
                semantic = self.semantic_policy.evaluate(str(spec.expected_intent_type or ""), poi)
                if not semantic.passed:
                    errors.append(f"added segment semantic mismatch: {semantic.reason_code}")
                metadata = added.get("semanticMetadata") if isinstance(added.get("semanticMetadata"), dict) else {}
                if spec.expected_intent_type and metadata.get("intentType") != spec.expected_intent_type:
                    errors.append("added segment intentType does not match")
                duration = self._minutes(added.get("endTime")) - self._minutes(added.get("startTime"))
                if spec.expected_duration_minutes is not None and duration != spec.expected_duration_minutes:
                    errors.append("added segment duration does not match")
                if spec.expected_transport_mode and added.get("transportMode") != spec.expected_transport_mode:
                    errors.append("added segment transport mode does not match")
                self._check_pending_slot_selection(before, after, added, spec, errors)
        elif spec.operation == "remove_segment":
            if target is not None:
                errors.append("target segment still exists")
            for segment_id in before_segments:
                if segment_id != target_id and segment_id not in after_segments:
                    errors.append(f"unrelated segment removed: {segment_id}")
        else:
            if target is None:
                errors.append("target segment missing")
            elif spec.operation == "replace_poi":
                poi = target.get("poi") if isinstance(target.get("poi"), dict) else {}
                old_poi = old_target.get("poi") if isinstance((old_target or {}).get("poi"), dict) else {}
                if str(poi.get("amapId") or "") != str(spec.expected_poi_amap_id or ""):
                    errors.append("target AMap POI identity does not match")
                if spec.expected_poi_name and str(poi.get("name") or "") != str(spec.expected_poi_name):
                    errors.append("target POI canonical name does not match resolved replacement")
                if str(poi.get("name") or "") == str(old_poi.get("name") or ""):
                    errors.append("target POI did not change")
                semantic = self.semantic_policy.evaluate(str(spec.expected_intent_type or ""), poi)
                if not semantic.passed:
                    errors.append(f"target semantic mismatch: {semantic.reason_code}")
                metadata = target.get("semanticMetadata") if isinstance(target.get("semanticMetadata"), dict) else {}
                if spec.expected_intent_type and metadata.get("intentType") != spec.expected_intent_type:
                    errors.append("target intentType was not preserved")
                duration = self._minutes(target.get("endTime")) - self._minutes(target.get("startTime"))
                if spec.expected_duration_minutes is not None and duration != spec.expected_duration_minutes:
                    errors.append("replace_poi changed target duration")
                for other_id, old_segment in before_segments.items():
                    if other_id == target_id or other_id not in after_segments:
                        continue
                    if self._poi_identity(old_segment) != self._poi_identity(after_segments[other_id]):
                        errors.append(f"unrelated POI changed: {other_id}")
                self._check_replace_preserved_fields(old_target, target, errors)
            elif spec.operation == "set_start_time" and target.get("startTime") != spec.expected_start_time:
                errors.append("target start time does not match")
            elif spec.operation == "set_duration":
                duration = self._minutes(target.get("endTime")) - self._minutes(target.get("startTime"))
                if duration != spec.expected_duration_minutes:
                    errors.append("target duration does not match")
            elif spec.operation == "set_transport_mode":
                if target.get("transportMode") != spec.expected_transport_mode:
                    errors.append("target transport mode does not match")
                if old_target and self._poi_identity(old_target) != self._poi_identity(target):
                    errors.append("transport mutation changed target POI")

        self._check_chronology(after, errors)
        for segment in after_segments.values():
            errors.extend(
                f"{item.get('code')}:{item.get('segmentId')}"
                for item in ItineraryScheduleService.temporal_failures(segment)
            )
        changed_route_pairs = [
            list(pair)
            for pair in sorted(set(before_routes) | set(after_routes))
            if before_routes.get(pair) != after_routes.get(pair)
        ]
        diff = TimelineMutationDiff(
            directChangedSegmentIds=direct,
            derivedChangedSegmentIds=derived,
            unexpectedChangedSegmentIds=unexpected,
            changedRoutePairIds=changed_route_pairs,
            versionDelta=version_delta,
        )
        return MutationPostconditionReport(passed=not errors, diff=diff, errors=errors)

    @staticmethod
    def _check_pending_slot_selection(
        before: dict[str, Any],
        after: dict[str, Any],
        added: dict[str, Any],
        spec: MutationPostconditionSpec,
        errors: list[str],
    ) -> None:
        key = spec.expected_pending_slot_key
        if not key or len(key) != 4:
            return
        expected = (str(key[0]), str(key[1]), str(key[2]), int(key[3]))

        def pending_keys(snapshot: dict[str, Any]) -> set[tuple[str, str, str, int]]:
            result: set[tuple[str, str, str, int]] = set()
            for slot in snapshot.get("portfolioPendingSlots") or []:
                if not isinstance(slot, dict):
                    continue
                try:
                    result.add(
                        (
                            str(slot.get("briefId") or ""),
                            str(slot.get("poolId") or ""),
                            str(slot.get("planningSlotId") or ""),
                            int(slot.get("dayNumber") or 0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
            return result

        if expected not in pending_keys(before):
            errors.append("target pending slot was not present before mutation")
        if expected in pending_keys(after):
            errors.append("target pending slot was not removed after mutation")
        metadata = added.get("semanticMetadata") if isinstance(added.get("semanticMetadata"), dict) else {}
        actual = (
            str(metadata.get("creativeBriefId") or ""),
            str(metadata.get("poolId") or ""),
            str(metadata.get("planningSlotId") or ""),
            int(metadata.get("dayNumber") or 0),
        )
        if actual != expected:
            errors.append("added segment pending-slot lineage does not match")
        if spec.expected_manual_placement_source and (
            metadata.get("manualPlacementSource") != spec.expected_manual_placement_source
        ):
            errors.append("added segment manual placement source does not match")
        if spec.expected_candidate_record_id is not None and (
            metadata.get("sourceCandidateRecordId") != spec.expected_candidate_record_id
        ):
            errors.append("added segment candidate record identity does not match")

    @staticmethod
    def _segments(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(segment.get("id")): segment
            for day in snapshot.get("days") or []
            for segment in day.get("segments") or []
            if segment.get("id")
        }

    @staticmethod
    def _segment_days(snapshot: dict[str, Any]) -> dict[str, str]:
        locations: dict[str, str] = {}
        for day in snapshot.get("days") or []:
            day_id = str(day.get("id") or "")
            for segment in day.get("segments") or []:
                segment_id = str(segment.get("id") or "")
                if segment_id:
                    locations[segment_id] = str(segment.get("dayId") or day_id)
        return locations

    @staticmethod
    def _poi_identity(segment: dict[str, Any]) -> tuple[str, str, str]:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        return str(poi.get("id") or ""), str(poi.get("amapId") or ""), str(poi.get("name") or "")

    def _only_derived_schedule_changed(self, old: Any, new: Any) -> bool:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        ignored = {"startTime", "endTime", "estimateMetadata"}
        return {key: value for key, value in old.items() if key not in ignored} == {
            key: value for key, value in new.items() if key not in ignored
        }

    @staticmethod
    def _schedule_changed(old: Any, new: Any) -> bool:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        return (old.get("startTime"), old.get("endTime")) != (new.get("startTime"), new.get("endTime"))

    @staticmethod
    def _only_schedule_metadata_changed(old: Any, new: Any) -> bool:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        old_metadata = old.get("estimateMetadata") if isinstance(old.get("estimateMetadata"), dict) else {}
        new_metadata = new.get("estimateMetadata") if isinstance(new.get("estimateMetadata"), dict) else {}
        old_without_schedule = {key: value for key, value in old_metadata.items() if key != "schedule"}
        new_without_schedule = {key: value for key, value in new_metadata.items() if key != "schedule"}
        if old_without_schedule != new_without_schedule:
            return False
        allowed_schedule_fields = {"status", "routeBufferMinutes", "constraintPassed"}
        old_schedule = old_metadata.get("schedule") if isinstance(old_metadata.get("schedule"), dict) else {}
        new_schedule = new_metadata.get("schedule") if isinstance(new_metadata.get("schedule"), dict) else {}
        return (
            {key: value for key, value in old_schedule.items() if key not in allowed_schedule_fields}
            == {key: value for key, value in new_schedule.items() if key not in allowed_schedule_fields}
        )

    @staticmethod
    def _schedule_metadata_changed(old: Any, new: Any) -> bool:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        old_metadata = old.get("estimateMetadata") if isinstance(old.get("estimateMetadata"), dict) else {}
        new_metadata = new.get("estimateMetadata") if isinstance(new.get("estimateMetadata"), dict) else {}
        old_schedule = old_metadata.get("schedule") if isinstance(old_metadata.get("schedule"), dict) else {}
        new_schedule = new_metadata.get("schedule") if isinstance(new_metadata.get("schedule"), dict) else {}
        return old_schedule != new_schedule

    @staticmethod
    def _valid_schedule_metadata(segment: Any) -> bool:
        if not isinstance(segment, dict):
            return False
        metadata = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
        schedule = metadata.get("schedule") if isinstance(metadata.get("schedule"), dict) else {}
        status = schedule.get("status")
        valid_statuses = {
            "user_locked",
            "scheduled_without_incoming_route",
            "committed_route_evidence",
            "provisional_missing_route",
            "flexible_slot_scheduled",
        }
        if status not in valid_statuses:
            return False
        expected_buffer = 10 if status == "committed_route_evidence" else 0
        return schedule.get("routeBufferMinutes") == expected_buffer

    @staticmethod
    def _is_user_locked(segment: Any) -> bool:
        if not isinstance(segment, dict):
            return False
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        estimate = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
        duration = estimate.get("duration") if isinstance(estimate.get("duration"), dict) else {}
        schedule = estimate.get("schedule") if isinstance(estimate.get("schedule"), dict) else {}
        return any(value is True for value in (semantic.get("userLocked"), duration.get("userLocked"), schedule.get("userLocked")))

    def _check_replace_preserved_fields(
        self,
        old: dict[str, Any] | None,
        new: dict[str, Any],
        errors: list[str],
    ) -> None:
        if not isinstance(old, dict):
            return
        for field in ("id", "dayId", "segmentOrder", "kind", "transportMode"):
            if old.get(field) != new.get(field):
                errors.append(f"replace_poi unexpectedly changed {field}")

    @staticmethod
    def _route_signatures(snapshot: dict[str, Any]) -> dict[tuple[str, str], list[tuple[Any, ...]]]:
        grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for route in snapshot.get("routeOptions") or []:
            if not isinstance(route, dict):
                continue
            pair = (str(route.get("fromSegmentId") or ""), str(route.get("toSegmentId") or ""))
            if not all(pair):
                continue
            signature = (
                str(route.get("fromPoiId") or ""),
                str(route.get("toPoiId") or ""),
                str(route.get("provider") or ""),
                str(route.get("mode") or ""),
                int(route.get("distanceMeters") or 0),
                int(route.get("durationSeconds") or 0),
                str(route.get("error") or ""),
            )
            grouped.setdefault(pair, []).append(signature)
        return {pair: sorted(signatures) for pair, signatures in grouped.items()}

    def _check_chronology(self, snapshot: dict[str, Any], errors: list[str]) -> None:
        for day in snapshot.get("days") or []:
            previous_end = -1
            for segment in day.get("segments") or []:
                start = self._minutes(segment.get("startTime"))
                end = self._minutes(segment.get("endTime"))
                if start < previous_end or end <= start:
                    errors.append(f"schedule overlap or chronology violation in day {day.get('dayNumber')}")
                    break
                previous_end = end

    @staticmethod
    def _minutes(value: Any) -> int:
        try:
            hour, minute = str(value).split(":", 1)
            return int(hour) * 60 + int(minute)
        except (TypeError, ValueError):
            return -1

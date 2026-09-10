from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.services.timeline_mutation_models import (
    BoundTimelineMutation,
    TimelineMutationIntent,
    TimelineSegmentDescriptor,
)
from src.services.portfolio_pending_slot_service import (
    PortfolioPendingSlotError,
    PortfolioPendingSlotService,
)


class TimelineTargetBinder:
    """Binds semantic selectors exclusively against the active persisted snapshot."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def bind(self, session_id: str, intent: TimelineMutationIntent) -> BoundTimelineMutation:
        session = self.db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="Conversation session not found")
        base_version_id = str(session["active_version_id"] or "")
        plan_id = str(session["active_plan_id"] or "")
        if not base_version_id or not plan_id:
            raise HTTPException(status_code=409, detail={"code": "timeline_mutation_requires_active_version"})
        version = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ? AND plan_id = ?",
            (base_version_id, session_id, plan_id),
        ).fetchone()
        if version is None:
            raise HTTPException(status_code=409, detail={"code": "active_version_snapshot_missing"})
        snapshot = json.loads(version["snapshot_json"] or "{}")
        if intent.pending_slot_selection is not None:
            selection = intent.pending_slot_selection
            if selection.asserted_base_version_id != base_version_id:
                raise HTTPException(status_code=409, detail={"code": "stale_mutation_base"})
            command = selection.model_dump(by_alias=True)
            command["briefId"] = selection.focus_brief_id
            try:
                pending_slot = PortfolioPendingSlotService.find_exact_slot(snapshot, command)
            except PortfolioPendingSlotError as error:
                raise HTTPException(
                    status_code=409,
                    detail={"code": error.code, "message": error.message},
                ) from error
            intent = intent.model_copy(
                update={
                    "selector": intent.selector.model_copy(
                        update={
                            "day_number": int(pending_slot["dayNumber"]),
                            "intent_type": str(pending_slot.get("intentType") or ""),
                            "time_window": str(pending_slot.get("timeWindow") or ""),
                        }
                    ),
                    "replacement": intent.replacement.model_copy(
                        update={
                            "start_time": str(pending_slot.get("startTime") or ""),
                            "duration_minutes": int(pending_slot.get("durationMinutes") or 0),
                        }
                    ),
                }
            )
        fingerprint = self.fingerprint(snapshot)
        descriptors = self.descriptors(snapshot)
        target_day_id: Optional[str] = None
        if intent.operation == "add_segment":
            day_matches = [
                str(day.get("id") or "")
                for day in snapshot.get("days") or []
                if int(day.get("dayNumber") or 0) == int(intent.selector.day_number or 0)
                and str(day.get("id") or "")
            ]
            target_day_id = day_matches[0] if len(day_matches) == 1 else None
            matches = []
            evidence = ["dayNumber_exact", "day_target_bound"] if target_day_id else ["dayNumber_miss"]
            if intent.pending_slot_selection is not None:
                evidence.extend(
                    [
                        "pendingSlot_exact",
                        f"planningSlotId={intent.pending_slot_selection.planning_slot_id}",
                        f"focusBriefId={intent.pending_slot_selection.focus_brief_id}",
                    ]
                )
            status = "unique" if target_day_id else "target_not_found"
        else:
            matches, evidence = self._matches(descriptors, intent)
            status = "unique" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "target_not_found"
        route_pairs: list[list[str]] = []
        adjacent_descriptors: list[TimelineSegmentDescriptor] = []
        insertion_index: Optional[int] = None
        if status == "unique" and intent.operation == "add_segment":
            day_segments = sorted([item for item in descriptors if item.day_id == target_day_id], key=lambda item: item.segment_order)
            requested = self._minutes(intent.replacement.start_time or intent.selector.time_window or "")
            insertion_index = next((index for index, item in enumerate(day_segments) if self._minutes(item.start_time) >= requested), len(day_segments))
            anchors = [item for item in day_segments if item.route_anchor and item.poi_longitude is not None and item.poi_latitude is not None]
            previous = next((item for item in reversed(day_segments[:insertion_index]) if item.route_anchor and item.poi_longitude is not None and item.poi_latitude is not None), None)
            following = next((item for item in day_segments[insertion_index:] if item.route_anchor and item.poi_longitude is not None and item.poi_latitude is not None), None)
            adjacent_descriptors = [item for item in (previous, following) if item is not None]
            if previous is not None and following is not None:
                route_pairs.append([previous.segment_id, following.segment_id])
            evidence.extend([f"insertionIndex={insertion_index}", f"adjacentAnchorCount={len(adjacent_descriptors)}"])
        if status == "unique" and matches:
            target = matches[0]
            day_segments = sorted(
                [item for item in descriptors if item.day_id == target.day_id], key=lambda item: item.segment_order
            )
            index = next(index for index, item in enumerate(day_segments) if item.segment_id == target.segment_id)
            if index > 0:
                route_pairs.append([day_segments[index - 1].segment_id, target.segment_id])
            if index + 1 < len(day_segments):
                route_pairs.append([target.segment_id, day_segments[index + 1].segment_id])
        return BoundTimelineMutation(
            mutationId=f"mutation_{uuid4().hex[:12]}",
            sessionId=session_id,
            planId=plan_id,
            baseVersionId=base_version_id,
            baseSnapshotFingerprint=fingerprint,
            intent=intent,
            targetSegmentIds=[item.segment_id for item in matches],
            targetDayId=target_day_id or (matches[0].day_id if len(matches) == 1 else None),
            targetDescriptors=matches,
            adjacentRoutePairIds=route_pairs,
            adjacentDescriptors=adjacent_descriptors,
            insertionIndex=insertion_index,
            bindingStatus=status,
            bindingEvidence=evidence,
        )


    @staticmethod
    def _minutes(value: str) -> int:
        text = str(value or "")[:5]
        try:
            hours, minutes = text.split(":", 1)
            return int(hours) * 60 + int(minutes)
        except (TypeError, ValueError):
            return 24 * 60

    def assert_current(self, bound: BoundTimelineMutation) -> None:
        session = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (bound.session_id,)
        ).fetchone()
        if session is None or str(session["active_version_id"] or "") != bound.base_version_id:
            raise HTTPException(status_code=409, detail={"code": "stale_mutation_base"})
        version = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?",
            (bound.base_version_id, bound.session_id),
        ).fetchone()
        snapshot = json.loads(version["snapshot_json"] or "{}") if version else {}
        if self.fingerprint(snapshot) != bound.base_snapshot_fingerprint:
            raise HTTPException(status_code=409, detail={"code": "stale_mutation_snapshot"})

    @staticmethod
    def fingerprint(snapshot: dict[str, Any]) -> str:
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def descriptors(cls, snapshot: dict[str, Any]) -> list[TimelineSegmentDescriptor]:
        descriptors: list[TimelineSegmentDescriptor] = []
        for day in snapshot.get("days") or []:
            for order, segment in enumerate(day.get("segments") or [], start=1):
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                metadata = segment.get("semanticMetadata")
                if not isinstance(metadata, dict):
                    metadata = cls._compat_semantic_metadata(segment, poi, int(day.get("dayNumber") or 0))
                aliases = [str(value) for value in metadata.get("aliases") or [] if value]
                if poi.get("name") and str(poi["name"]) not in aliases:
                    aliases.append(str(poi["name"]))
                descriptors.append(
                    TimelineSegmentDescriptor(
                        segmentId=str(segment.get("id") or ""),
                        dayId=str(day.get("id") or ""),
                        dayNumber=int(day.get("dayNumber") or 0),
                        segmentOrder=order,
                        startTime=str(segment.get("startTime") or ""),
                        endTime=str(segment.get("endTime") or ""),
                        kind=str(segment.get("kind") or ""),
                        poiName=str(poi.get("name") or ""),
                        poiAmapId=poi.get("amapId"),
                        poiCanonicalName=str(poi.get("matchedAmapName") or poi.get("name") or ""),
                        poiLongitude=poi.get("longitude"),
                        poiLatitude=poi.get("latitude"),
                        intentType=metadata.get("intentType") or poi.get("intentType"),
                        intentSlotId=metadata.get("intentSlotId"),
                        rawNeed=str(metadata.get("rawNeed") or ""),
                        groundingStatus=str(metadata.get("groundingStatus") or poi.get("groundingStatus") or "draft_only"),
                        routeAnchor=bool(metadata.get("routeAnchor", poi.get("routeable", False))),
                        required=bool(metadata.get("required", False)),
                        userLocked=bool(metadata.get("userLocked", cls._user_locked(segment))),
                        aliases=aliases,
                        transportMode=str(segment.get("transportMode") or ""),
                    )
                )
        return descriptors

    def _matches(
        self, descriptors: list[TimelineSegmentDescriptor], intent: TimelineMutationIntent
    ) -> tuple[list[TimelineSegmentDescriptor], list[str]]:
        selector = intent.selector
        candidates = list(descriptors)
        evidence: list[str] = []
        if selector.day_number is not None:
            candidates = [item for item in candidates if item.day_number == selector.day_number]
            evidence.append("dayNumber_exact")
        if selector.kind:
            candidates = [item for item in candidates if item.kind == selector.kind]
            evidence.append("kind_exact")

        if intent.operation == "set_transport_mode" and selector.from_text and selector.to_text:
            matches: list[TimelineSegmentDescriptor] = []
            for day_number in sorted({item.day_number for item in candidates}):
                day_items = sorted(
                    [item for item in candidates if item.day_number == day_number], key=lambda item: item.segment_order
                )
                for left, right in zip(day_items, day_items[1:]):
                    if self._text_match(left, selector.from_text) and self._text_match(right, selector.to_text):
                        matches.append(left)
            return matches, [*evidence, "adjacent_route_pair_exact"]

        if selector.intent_type:
            typed = [item for item in candidates if item.intent_type == selector.intent_type]
            if typed:
                candidates = typed
                evidence.append("intentType_exact")
            else:
                return [], [*evidence, "intentType_miss"]
        if selector.current_text:
            named = [item for item in candidates if self._text_match(item, selector.current_text)]
            if named:
                candidates = named
                evidence.append("currentText_alias_match")
            else:
                return [], [*evidence, "currentText_miss"]
        if selector.time_window:
            window = str(selector.time_window).strip()
            if "-" in window:
                start_time, end_time = (part.strip() for part in window.split("-", 1))
                candidates = [
                    item
                    for item in candidates
                    if item.start_time == start_time and item.end_time == end_time
                ]
            else:
                candidates = [item for item in candidates if item.start_time.startswith(window)]
            evidence.append("timeWindow_match")
        if selector.ordinal is not None:
            ordered = sorted(candidates, key=lambda item: (item.day_number, item.segment_order))
            candidates = [ordered[selector.ordinal - 1]] if 0 < selector.ordinal <= len(ordered) else []
            evidence.append("ordinal_match")
        return candidates, evidence

    @staticmethod
    def _text_match(item: TimelineSegmentDescriptor, text: str) -> bool:
        needle = TimelineTargetBinder._normalize(text)
        values = [item.poi_name, item.poi_canonical_name, item.raw_need, *item.aliases]
        return any(
            needle and (needle == TimelineTargetBinder._normalize(value) or needle in TimelineTargetBinder._normalize(value) or TimelineTargetBinder._normalize(value) in needle)
            for value in values
            if value
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s的（()）·\-—_]", "", str(value or "")).casefold()

    @staticmethod
    def _user_locked(segment: dict[str, Any]) -> bool:
        estimate = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
        duration = estimate.get("duration") if isinstance(estimate.get("duration"), dict) else {}
        return bool(duration.get("userLocked"))

    @classmethod
    def _compat_semantic_metadata(
        cls, segment: dict[str, Any], poi: dict[str, Any], day_number: int
    ) -> dict[str, Any]:
        # Legacy snapshots without semanticMetadata cannot recover business
        # facts from prose. Keep only independently structured fields and
        # otherwise fail closed as optional/untyped.
        intent_type = str(segment.get("intentType") or poi.get("intentType") or "").strip() or None
        aliases = [str(poi.get("name"))] if poi.get("name") else []
        return {
            "intentType": intent_type,
            "intentSlotId": f"day{day_number}_{intent_type}" if intent_type else None,
            "rawNeed": str(segment.get("rawNeed") or poi.get("name") or ""),
            "groundingStatus": segment.get("groundingStatus") or poi.get("groundingStatus") or "draft_only",
            "routeAnchor": bool(segment.get("routeAnchor") or poi.get("routeable")),
            "required": bool(segment.get("required", False)),
            "userLocked": cls._user_locked(segment),
            "aliases": aliases,
        }

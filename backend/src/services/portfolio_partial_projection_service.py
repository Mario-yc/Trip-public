"""Pure projection of exact non-hard route blockers into pending-slot metadata."""
from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.services.portfolio_pending_slot_schedule_service import (
    PortfolioPendingSlotScheduleService,
)


class PortfolioPartialProjectionResult(BaseModel):
    status: Literal["unchanged", "sanitized", "rejected"]
    reason_code: str = Field(alias="reasonCode")
    snapshot: dict[str, Any]
    removed_segment_ids: list[str] = Field(
        default_factory=list, alias="removedSegmentIds"
    )
    failure_codes: list[str] = Field(default_factory=list, alias="failureCodes")
    pending_slots_added: list[dict[str, Any]] = Field(
        default_factory=list, alias="pendingSlotsAdded"
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PortfolioPartialProjectionService:
    """Remove only a precisely identified soft meal; never invent a placeholder."""

    _PAIR_SCOPED_SANITIZABLE_CODES = {"meal_detour_high", "meal_detour_poor"}
    _EXPLICIT_MEAL_SANITIZABLE_CODES = {"provider_route_matrix_unacceptable"}
    _SANITIZABLE_CODES = (
        _PAIR_SCOPED_SANITIZABLE_CODES | _EXPLICIT_MEAL_SANITIZABLE_CODES
    )

    def sanitize(self, snapshot: dict[str, Any]) -> PortfolioPartialProjectionResult:
        original = copy.deepcopy(snapshot)
        work = copy.deepcopy(snapshot)
        quality = (
            work.get("portfolioRouteQuality")
            if isinstance(work.get("portfolioRouteQuality"), dict)
            else {}
        )
        issues = [
            item
            for item in quality.get("routeQualityIssues") or []
            if isinstance(item, dict)
            and self._is_sanitizable_issue(item)
        ]
        if not issues:
            return PortfolioPartialProjectionResult(
                status="unchanged",
                reasonCode="no_sanitizable_route_blocker",
                snapshot=work,
            )
        segments: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for day in work.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if isinstance(segment, dict) and str(segment.get("id") or ""):
                    segments[str(segment["id"])] = (day, segment)
        targeted: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {}
        for issue in issues:
            route_pair = (
                issue.get("routePair")
                if isinstance(issue.get("routePair"), dict)
                else {}
            )
            pair = [
                str(issue.get("fromSegmentId") or route_pair.get("fromSegmentId") or ""),
                str(issue.get("toSegmentId") or route_pair.get("toSegmentId") or ""),
            ]
            explicit_meal_id = str(issue.get("mealSegmentId") or "")
            candidate_ids = (
                [explicit_meal_id]
                if str(issue.get("code") or "")
                in self._EXPLICIT_MEAL_SANITIZABLE_CODES
                else list(dict.fromkeys([*pair, explicit_meal_id]))
            )
            meals = [
                segments[segment_id]
                for segment_id in candidate_ids
                if segment_id in segments
                and self._is_meal_segment(segments[segment_id][1])
            ]
            if len(meals) != 1:
                return self._rejected(
                    original,
                    "meal_detour_segment_identity_ambiguous",
                    issues,
                )
            day, meal = meals[0]
            if self._is_hard_segment(meal):
                return self._rejected(
                    original,
                    "meal_detour_targets_hard_segment",
                    issues,
                )
            semantic = (
                meal.get("semanticMetadata")
                if isinstance(meal.get("semanticMetadata"), dict)
                else {}
            )
            if not all(
                str(semantic.get(key) or "").strip()
                for key in ("creativeBriefId", "poolId", "planningSlotId")
            ):
                return self._rejected(
                    original,
                    "meal_detour_lineage_missing",
                    issues,
                )
            targeted[str(meal["id"])] = (
                day,
                meal,
                str(issue.get("code") or ""),
            )
        pending_added: list[dict[str, Any]] = []
        removed_ids = sorted(targeted)
        for segment_id in removed_ids:
            day, segment, failure_code = targeted[segment_id]
            semantic = segment["semanticMetadata"]
            pending = self._pending_slot(
                day=day,
                segment=segment,
                semantic=semantic,
                reason=failure_code,
            )
            pending_added.append(pending)
            day["segments"] = [
                item
                for item in day.get("segments") or []
                if not isinstance(item, dict)
                or str(item.get("id") or "") != segment_id
            ]
        existing_pending = [
            copy.deepcopy(item)
            for item in work.get("portfolioPendingSlots") or []
            if isinstance(item, dict)
        ]
        existing_keys = {
            (
                str(item.get("briefId") or ""),
                str(item.get("poolId") or ""),
                str(item.get("planningSlotId") or ""),
                int(item.get("dayNumber") or 0),
            )
            for item in existing_pending
        }
        for pending in pending_added:
            key = (
                pending["briefId"],
                pending["poolId"],
                pending["planningSlotId"],
                pending["dayNumber"],
            )
            if key not in existing_keys:
                existing_pending.append(pending)
                existing_keys.add(key)
        work["portfolioPendingSlots"] = sorted(
            existing_pending,
            key=lambda item: (
                int(item.get("dayNumber") or 0),
                str(item.get("startTime") or item.get("timeWindow") or ""),
                str(item.get("planningSlotId") or ""),
            ),
        )
        for day in work.get("days") or []:
            if isinstance(day, dict):
                day["routeEvidence"] = self._without_segments(
                    day.get("routeEvidence"), removed_ids
                )
        work["portfolioRouteEvidence"] = self._without_segments(
            work.get("portfolioRouteEvidence"), removed_ids
        )
        work["routeEvidence"] = self._without_segments(
            work.get("routeEvidence"), removed_ids
        )
        remaining_issues = [
            copy.deepcopy(item)
            for item in quality.get("routeQualityIssues") or []
            if isinstance(item, dict)
            and not (
                self._is_sanitizable_issue(item)
                and bool(set(self._evidence_segment_ids(item)) & set(targeted))
            )
        ]
        quality["routeQualityIssues"] = remaining_issues
        quality["status"] = (
            "pending_reverification" if not remaining_issues else "failed"
        )
        quality["sanitizedBlockers"] = [
            {
                "code": code,
                "segmentId": segment_id,
                "projection": "metadata_only_pending_slot",
            }
            for segment_id, (_day, _segment, code) in sorted(targeted.items())
        ]
        work["portfolioRouteQuality"] = quality
        work["portfolioPartialProjection"] = {
            "status": "sanitized",
            "reason": "non_hard_meal_detour_projected_to_pending_slot",
            "removedSegmentIds": removed_ids,
            "failureCodes": sorted({code for _day, _segment, code in targeted.values()}),
            "fakePoiCount": 0,
            "pendingRouteAnchorCount": 0,
        }
        work = PortfolioPendingSlotScheduleService().project(work)
        return PortfolioPartialProjectionResult(
            status="sanitized",
            reasonCode="non_hard_meal_detour_projected_to_pending_slot",
            snapshot=work,
            removedSegmentIds=removed_ids,
            failureCodes=sorted({code for _day, _segment, code in targeted.values()}),
            pendingSlotsAdded=pending_added,
        )

    @classmethod
    def _rejected(
        cls,
        original: dict[str, Any],
        reason: str,
        issues: list[dict[str, Any]],
    ) -> PortfolioPartialProjectionResult:
        return PortfolioPartialProjectionResult(
            status="rejected",
            reasonCode=reason,
            snapshot=original,
            failureCodes=sorted(
                {
                    str(item.get("code") or "")
                    for item in issues
                    if str(item.get("code") or "")
                }
            ),
        )

    @staticmethod
    def _is_meal_segment(segment: dict[str, Any]) -> bool:
        semantic = (
            segment.get("semanticMetadata")
            if isinstance(segment.get("semanticMetadata"), dict)
            else {}
        )
        return (
            str(segment.get("kind") or "").lower() == "meal"
            or str(semantic.get("intentType") or "").lower() == "meal"
        )

    @classmethod
    def _is_sanitizable_issue(cls, issue: dict[str, Any]) -> bool:
        code = str(issue.get("code") or "")
        if code in cls._PAIR_SCOPED_SANITIZABLE_CODES:
            return True
        return bool(
            code in cls._EXPLICIT_MEAL_SANITIZABLE_CODES
            and str(issue.get("mealSegmentId") or "").strip()
        )

    @staticmethod
    def _is_hard_segment(segment: dict[str, Any]) -> bool:
        semantic = (
            segment.get("semanticMetadata")
            if isinstance(segment.get("semanticMetadata"), dict)
            else {}
        )
        return bool(
            semantic.get("required") is True
            or str(semantic.get("requirementLevel") or "").lower()
            in {"hard", "required"}
            or (
                semantic.get("goalId")
                and not semantic.get("softGoalId")
                and not semantic.get("portfolioOptional")
            )
        )

    @classmethod
    def _pending_slot(
        cls,
        *,
        day: dict[str, Any],
        segment: dict[str, Any],
        semantic: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        raw_need = str(
            semantic.get("rawNeed")
            or segment.get("title")
            or "当地特色午餐"
        )
        start = str(segment.get("startTime") or "")
        end = str(segment.get("endTime") or "")
        duration = cls._duration_minutes(start, end)
        planning_slot_id = str(semantic.get("planningSlotId") or "")
        brief_id = str(semantic.get("creativeBriefId") or "")
        return {
            "id": f"pending_{brief_id or 'single'}_{planning_slot_id}",
            "briefId": brief_id,
            "poolId": str(semantic.get("poolId") or ""),
            "planningSlotId": planning_slot_id,
            "dayNumber": int(
                semantic.get("dayNumber") or day.get("dayNumber") or 1
            ),
            "timeWindow": str(
                semantic.get("timeWindow")
                or (f"{start}-{end}" if start and end else "")
            ),
            "startTime": start,
            "endTime": end,
            "durationMinutes": int(
                semantic.get("durationMinutes") or duration or 60
            ),
            "intentType": "meal",
            "kind": "meal",
            "sourceGoalId": str(
                semantic.get("sourceGoalId")
                or semantic.get("softGoalId")
                or semantic.get("goalId")
                or ""
            ),
            "requirementLevel": str(
                semantic.get("requirementLevel") or "soft"
            ),
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
            "groundingStatus": "unresolved",
            "occurrenceId": str(
                semantic.get("occurrenceId")
                or (
                    "occ:"
                    f"{semantic.get('sourceGoalId') or semantic.get('softGoalId') or semantic.get('goalId')}"
                    f":day:{semantic.get('dayNumber') or day.get('dayNumber') or 1}"
                    if semantic.get("sourceGoalId")
                    or semantic.get("softGoalId")
                    or semantic.get("goalId")
                    else ""
                )
            ),
            "rawNeed": raw_need,
            "reason": reason,
            "state": "pending",
            "label": f"待补：{raw_need}",
        }

    @staticmethod
    def _duration_minutes(start: str, end: str) -> int:
        try:
            start_h, start_m = (int(part) for part in start.split(":", 1))
            end_h, end_m = (int(part) for part in end.split(":", 1))
        except (TypeError, ValueError):
            return 0
        return max(0, end_h * 60 + end_m - start_h * 60 - start_m)

    @staticmethod
    def _evidence_segment_ids(item: dict[str, Any]) -> list[str]:
        route_pair = item.get("routePair") if isinstance(item.get("routePair"), dict) else {}
        return [
            segment_id
            for segment_id in dict.fromkeys(
                [
                    str(item.get("fromSegmentId") or route_pair.get("fromSegmentId") or ""),
                    str(item.get("toSegmentId") or route_pair.get("toSegmentId") or ""),
                    str(item.get("mealSegmentId") or ""),
                ]
            )
            if segment_id
        ]

    @classmethod
    def _without_segments(cls, value: object, segment_ids: list[str]) -> list[dict[str, Any]]:
        blocked = set(segment_ids)
        return [
            copy.deepcopy(item)
            for item in value or []
            if isinstance(item, dict)
            and not (set(cls._evidence_segment_ids(item)) & blocked)
        ]

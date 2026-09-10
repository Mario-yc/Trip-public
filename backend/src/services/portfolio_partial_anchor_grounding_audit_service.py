"""Typed, fail-closed grounding audit for Portfolio partial timelines."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.services.map_poi_service import AMAP_PLACE_SOURCE


_LINEAGE_FIELDS = (
    "creativeBriefId",
    "poolId",
    "planningSlotId",
    "dayNumber",
    "sourceGoalId",
)


@dataclass(frozen=True)
class PartialAnchorGroundingAudit:
    anchor_count: int
    matched_anchor_count: int
    unmatched_anchor_count: int
    unmatched_anchor_keys: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    focus_brief_id: str = ""
    partial_eligible: bool = False
    write_attempted: bool = False
    active_version_id: str | None = None
    version_delta: int = 0
    patch_delta: int = 0
    route_write_delta: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchorCount": self.anchor_count,
            "matchedAnchorCount": self.matched_anchor_count,
            "unmatchedAnchorCount": self.unmatched_anchor_count,
            "unmatchedAnchorKeys": copy.deepcopy(self.unmatched_anchor_keys),
            "reasonCodes": list(self.reason_codes),
            "focusBriefId": self.focus_brief_id,
            "partialEligible": self.partial_eligible,
            "writeAttempted": self.write_attempted,
            "activeVersionId": self.active_version_id,
            "versionDelta": self.version_delta,
            "patchDelta": self.patch_delta,
            "routeWriteDelta": self.route_write_delta,
        }


class PortfolioPartialAnchorGroundingAuditService:
    """Prove every visible route anchor against server-owned AMap evidence."""

    def merge_route_repair_evidence(
        self,
        grounding_report: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        candidate_evidence: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        merged = copy.deepcopy(grounding_report)
        existing = [
            copy.deepcopy(item) for item in merged.get("routeRepairGroundingEvidence") or [] if isinstance(item, dict)
        ]
        quality = (
            snapshot.get("portfolioRouteQuality") if isinstance(snapshot.get("portfolioRouteQuality"), dict) else {}
        )
        for repair in quality.get("candidateRepairs") or []:
            if not isinstance(repair, dict):
                continue
            evidence = repair.get("groundingEvidence")
            if isinstance(evidence, dict) and self._complete_evidence(evidence):
                existing.append(copy.deepcopy(evidence))
        anchors = self._snapshot_anchors(snapshot)
        for raw in candidate_evidence or []:
            rebound = self._rebind_candidate_evidence(raw, anchors)
            if rebound is not None:
                existing.append(rebound)
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in existing:
            key = self._identity_tuple(item)
            if key is not None:
                deduped[key] = item
        merged["routeRepairGroundingEvidence"] = list(deduped.values())
        return merged

    def _rebind_candidate_evidence(
        self,
        raw: dict[str, Any],
        anchors: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Bind optimizer-owned AMap evidence to exactly one visible anchor.

        The optimizer may carry `briefId`/`id` aliases and omit `sourceGoalId`.
        It is still server-owned evidence, but it is accepted only when every
        supplied identity field agrees with one unique snapshot anchor.
        """

        if not isinstance(raw, dict):
            return None
        normalized = copy.deepcopy(raw)
        normalized["creativeBriefId"] = str(
            normalized.get("creativeBriefId") or normalized.get("briefId") or ""
        ).strip()
        normalized["poolId"] = str(normalized.get("poolId") or "").strip()
        normalized["planningSlotId"] = str(normalized.get("planningSlotId") or normalized.get("slotId") or "").strip()
        try:
            normalized["dayNumber"] = int(normalized.get("dayNumber") or 0)
        except (TypeError, ValueError):
            return None
        normalized["sourceGoalId"] = str(normalized.get("sourceGoalId") or "").strip()
        normalized["amapId"] = str(normalized.get("amapId") or normalized.get("id") or "").strip()
        normalized["source"] = str(normalized.get("source") or "").strip()
        coordinates = self._coordinates(normalized)
        if not normalized["amapId"] or normalized["source"] != AMAP_PLACE_SOURCE or coordinates is None:
            return None

        matches: list[dict[str, Any]] = []
        for anchor in anchors:
            if anchor.get("amapId") != normalized["amapId"]:
                continue
            if anchor.get("source") != normalized["source"]:
                continue
            if not self._coordinates_match(coordinates, self._coordinates(anchor)):
                continue
            supplied_lineage = {
                key: normalized.get(key) for key in _LINEAGE_FIELDS if normalized.get(key) not in (None, "", 0)
            }
            if any(anchor.get(key) != value for key, value in supplied_lineage.items()):
                continue
            matches.append(anchor)
        if len(matches) != 1:
            return None
        anchor = matches[0]
        rebound = {
            **normalized,
            **{key: anchor.get(key) for key in _LINEAGE_FIELDS},
            "amapId": anchor.get("amapId"),
            "source": anchor.get("source"),
            "longitude": anchor.get("longitude"),
            "latitude": anchor.get("latitude"),
        }
        return rebound if self._complete_evidence(rebound) else None

    def audit(
        self,
        snapshot: dict[str, Any],
        grounding_report: dict[str, Any],
        *,
        focus_brief_id: str,
    ) -> PartialAnchorGroundingAudit:
        evidence = self._server_evidence(grounding_report, focus_brief_id)
        anchors = self._snapshot_anchors(snapshot)
        unmatched: list[dict[str, Any]] = []
        reasons: list[str] = []
        matched = 0
        for anchor in anchors:
            reason = self._unmatched_reason(
                anchor,
                evidence,
                focus_brief_id=focus_brief_id,
            )
            if reason is None:
                matched += 1
                continue
            unmatched.append(anchor)
            if reason not in reasons:
                reasons.append(reason)
        return PartialAnchorGroundingAudit(
            anchor_count=len(anchors),
            matched_anchor_count=matched,
            unmatched_anchor_count=len(unmatched),
            unmatched_anchor_keys=unmatched,
            reason_codes=reasons,
            focus_brief_id=focus_brief_id,
            partial_eligible=bool(anchors) and not unmatched,
        )

    def project_verified_snapshot(
        self,
        snapshot: dict[str, Any],
        audit: PartialAnchorGroundingAudit,
    ) -> dict[str, Any] | None:
        """Move precisely scoped unproven anchors back to pending metadata."""
        if not audit.unmatched_anchor_keys:
            return copy.deepcopy(snapshot)
        if any(not all(item.get(field_name) for field_name in _LINEAGE_FIELDS) for item in audit.unmatched_anchor_keys):
            return None
        result = copy.deepcopy(snapshot)
        unmatched_ids = {str(item.get("segmentId") or "") for item in audit.unmatched_anchor_keys}
        pending = [copy.deepcopy(item) for item in result.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        pending_keys = {
            (
                str(item.get("briefId") or ""),
                str(item.get("poolId") or ""),
                str(item.get("planningSlotId") or item.get("slotId") or ""),
                int(item.get("dayNumber") or 0),
            )
            for item in pending
        }
        for day in result.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            kept: list[dict[str, Any]] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict) or str(segment.get("id") or "") not in unmatched_ids:
                    if isinstance(segment, dict):
                        kept.append(segment)
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                key = (
                    str(semantic.get("creativeBriefId") or ""),
                    str(semantic.get("poolId") or ""),
                    str(semantic.get("planningSlotId") or semantic.get("slotId") or ""),
                    day_number,
                )
                if key not in pending_keys:
                    schedule_constraints = (
                        semantic.get("scheduleConstraints")
                        if isinstance(semantic.get("scheduleConstraints"), dict)
                        else None
                    )
                    item = {
                        "id": f"pending:{key[2]}",
                        "briefId": key[0],
                        "poolId": key[1],
                        "planningSlotId": key[2],
                        "dayNumber": key[3],
                        "sourceGoalId": str(semantic.get("sourceGoalId") or ""),
                        "rawNeed": str(semantic.get("rawNeed") or segment.get("title") or "待补地点"),
                        "intentType": str(semantic.get("intentType") or segment.get("kind") or "visit"),
                        "kind": str(segment.get("kind") or "visit"),
                        "reason": "grounding_projection_mismatch",
                        "state": "pending",
                    }
                    if schedule_constraints:
                        item["semanticMetadata"] = {"scheduleConstraints": copy.deepcopy(schedule_constraints)}
                    else:
                        for field_name in (
                            "startTime",
                            "endTime",
                            "durationMinutes",
                            "timeWindow",
                        ):
                            if segment.get(field_name) not in (None, ""):
                                item[field_name] = segment[field_name]
                    pending.append(item)
                    pending_keys.add(key)
            day["segments"] = kept
        result["portfolioPendingSlots"] = pending
        return result

    def _server_evidence(
        self,
        grounding_report: dict[str, Any],
        focus_brief_id: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for report in grounding_report.get("poolReports") or []:
            if not isinstance(report, dict):
                continue
            report_brief = str(report.get("briefId") or "").strip()
            report_pool = str(report.get("poolId") or "").strip()
            report_goal = str(report.get("sourceGoalId") or report.get("goalId") or "").strip()
            if focus_brief_id and report_brief != focus_brief_id:
                continue
            slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
            for field_name in ("safeCandidates", "selectedCandidates"):
                for raw in report.get(field_name) or []:
                    if not isinstance(raw, dict):
                        continue
                    item = copy.deepcopy(raw)
                    slot_id = str(item.get("planningSlotId") or item.get("slotId") or "").strip()
                    item["creativeBriefId"] = str(
                        item.get("creativeBriefId") or item.get("briefId") or report_brief
                    ).strip()
                    item["poolId"] = str(item.get("poolId") or report_pool).strip()
                    item["planningSlotId"] = slot_id
                    item["dayNumber"] = int(item.get("dayNumber") or slot_days.get(slot_id) or 0)
                    item["sourceGoalId"] = str(item.get("sourceGoalId") or report_goal).strip()
                    item["amapId"] = str(item.get("amapId") or item.get("id") or "").strip()
                    if self._complete_evidence(item):
                        result.append(item)
        for raw in grounding_report.get("routeRepairGroundingEvidence") or []:
            if isinstance(raw, dict) and self._complete_evidence(raw):
                result.append(copy.deepcopy(raw))
        return result

    @staticmethod
    def _snapshot_anchors(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        anchors: list[dict[str, Any]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                if not bool(semantic.get("routeAnchor")):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                anchors.append(
                    {
                        "segmentId": str(segment.get("id") or ""),
                        "creativeBriefId": str(semantic.get("creativeBriefId") or "").strip(),
                        "poolId": str(semantic.get("poolId") or "").strip(),
                        "planningSlotId": str(semantic.get("planningSlotId") or semantic.get("slotId") or "").strip(),
                        "dayNumber": day_number,
                        "sourceGoalId": str(semantic.get("sourceGoalId") or "").strip(),
                        "amapId": str(poi.get("amapId") or "").strip(),
                        "source": str(poi.get("source") or "").strip(),
                        "longitude": poi.get("longitude"),
                        "latitude": poi.get("latitude"),
                    }
                )
        return anchors

    def _unmatched_reason(
        self,
        anchor: dict[str, Any],
        evidence: list[dict[str, Any]],
        *,
        focus_brief_id: str,
    ) -> str | None:
        if any(not anchor.get(field_name) for field_name in _LINEAGE_FIELDS):
            return "partial_anchor_lineage_missing"
        if focus_brief_id and anchor["creativeBriefId"] != focus_brief_id:
            return "partial_anchor_scope_mismatch"
        if not anchor.get("amapId"):
            return "partial_anchor_amap_identity_missing"
        if anchor.get("source") != AMAP_PLACE_SOURCE:
            return "partial_anchor_source_invalid"
        coordinates = self._coordinates(anchor)
        if coordinates is None:
            return "partial_anchor_coordinate_mismatch"
        same_amap = [item for item in evidence if item.get("amapId") == anchor["amapId"]]
        if not same_amap:
            return "partial_anchor_not_in_server_grounding"
        same_scope = [item for item in same_amap if all(item.get(key) == anchor.get(key) for key in _LINEAGE_FIELDS)]
        if not same_scope:
            return "partial_anchor_scope_mismatch"
        same_source = [item for item in same_scope if item.get("source") == AMAP_PLACE_SOURCE]
        if not same_source:
            return "partial_anchor_source_invalid"
        if not any(self._coordinates_match(coordinates, self._coordinates(item)) for item in same_source):
            return "partial_anchor_coordinate_mismatch"
        return None

    @classmethod
    def _complete_evidence(cls, value: dict[str, Any]) -> bool:
        return bool(
            all(value.get(field_name) for field_name in _LINEAGE_FIELDS)
            and value.get("amapId")
            and value.get("source") == AMAP_PLACE_SOURCE
            and cls._coordinates(value) is not None
        )

    @classmethod
    def _identity_tuple(cls, value: dict[str, Any]) -> tuple[Any, ...] | None:
        coordinates = cls._coordinates(value)
        if not cls._complete_evidence(value) or coordinates is None:
            return None
        return tuple(value.get(field_name) for field_name in _LINEAGE_FIELDS) + (
            value.get("amapId"),
            value.get("source"),
            *coordinates,
        )

    @staticmethod
    def _coordinates(value: dict[str, Any]) -> tuple[float, float] | None:
        try:
            longitude = float(value.get("longitude"))
            latitude = float(value.get("latitude"))
        except (TypeError, ValueError):
            return None
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            return None
        return longitude, latitude

    @staticmethod
    def _coordinates_match(
        left: tuple[float, float],
        right: tuple[float, float] | None,
    ) -> bool:
        return bool(right is not None and abs(left[0] - right[0]) <= 1e-5 and abs(left[1] - right[1]) <= 1e-5)

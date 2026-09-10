import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.models.poi import POI
from src.models.poi_intent import DaySlot, PersistableSegmentPlan
from src.services.campus_candidate_policy import CampusCandidatePolicy
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.meal_candidate_quality_policy import MealCandidateQualityPolicy
from src.services.night_view_candidate_policy import NightViewCandidatePolicy


GENERIC_REQUIRED_TITLES = {
    "上午高校参观",
    "下午高校参观",
    "核心地点参观",
    "上午核心地点参观",
    "下午核心地点参观",
    "夜景观景点",
    "午餐 当地特色美食",
    "晚餐 当地特色美食",
    "区域漫步",
    "待确认顺路餐饮",
    "待补充顺路地点",
}


@dataclass
class ItineraryQualityReport:
    can_create_active_version: bool
    quality_status: str
    hard_failures: list[str] = field(default_factory=list)
    soft_warnings: list[str] = field(default_factory=list)
    placeholder_segments: list[dict[str, Any]] = field(default_factory=list)
    weak_poi_violations: list[dict[str, Any]] = field(default_factory=list)
    missing_required_slots: list[dict[str, Any]] = field(default_factory=list)
    route_quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canCreateActiveVersion": self.can_create_active_version,
            "qualityStatus": self.quality_status,
            "hardFailures": self.hard_failures,
            "softWarnings": self.soft_warnings,
            "placeholderSegments": self.placeholder_segments,
            "weakPoiViolations": self.weak_poi_violations,
            "missingRequiredSlots": self.missing_required_slots,
            "routeQuality": self.route_quality,
        }


class ItineraryQualityContract:
    """Pre-persistence gate for complete automatic itinerary generation."""

    def __init__(self) -> None:
        self.campus_policy = CampusCandidatePolicy()
        self.meal_policy = MealCandidateQualityPolicy()
        self.night_view_policy = NightViewCandidatePolicy()

    def evaluate(
        self,
        *,
        day_slots: list[DaySlot],
        persistable_plans: list[PersistableSegmentPlan],
        unresolved: list[dict[str, Any]],
        required_intent_coverage: list[dict[str, Any]],
        pipeline_context: Optional[dict[str, Any]] = None,
        full_auto_required: bool = True,
    ) -> ItineraryQualityReport:
        pipeline_context = pipeline_context or {}
        hard_failures: list[str] = []
        soft_warnings: list[str] = []
        placeholder_segments: list[dict[str, Any]] = []
        weak_poi_violations: list[dict[str, Any]] = []
        missing_required_slots: list[dict[str, Any]] = []

        slot_by_day_start = {(slot.day_number, slot.start_time): slot for slot in day_slots}
        required_counts = self._required_counts_by_day(day_slots)
        selected_counts: dict[int, dict[str, int]] = {}

        for plan in persistable_plans:
            if not plan.route_anchor:
                continue
            slot = slot_by_day_start.get((plan.day_number, plan.start_time))
            intent_type = self._intent_type_for_plan(plan, slot)
            poi = plan.selected_poi
            if poi is None:
                placeholder_segments.append(self._plan_ref(plan, slot, "missing_selected_poi"))
                continue
            if self._is_generic_title(plan.display_title, poi.name):
                placeholder_segments.append(self._plan_ref(plan, slot, "generic_required_title"))
            if not self._is_real_amap_poi(poi):
                placeholder_segments.append(self._plan_ref(plan, slot, "not_real_amap_poi"))
                continue
            weak_reason = self._weak_reason(intent_type, plan, poi, pipeline_context)
            if weak_reason:
                weak_poi_violations.append({**self._plan_ref(plan, slot, weak_reason), "poiName": poi.name})
                continue
            if intent_type in {"campus_visit", "meal", "night_view"}:
                selected_counts.setdefault(plan.day_number, {"campus_visit": 0, "meal": 0, "night_view": 0})[
                    intent_type
                ] += 1

        for coverage in required_intent_coverage:
            if coverage.get("coverageStatus") == "covered":
                continue
            missing_required_slots.append(
                {
                    "poolId": coverage.get("poolId"),
                    "intentType": coverage.get("intentType"),
                    "targetCount": int(coverage.get("targetCount") or 0),
                    "selectedCount": int(coverage.get("selectedCount") or 0),
                    "missingCount": int(coverage.get("missingCount") or 0),
                    "unresolvedSlotIds": list(coverage.get("unresolvedSlotIds") or []),
                    "reason": coverage.get("reason") or coverage.get("coverageStatus") or "required_intent_unresolved",
                }
            )

        for day_number, counts in sorted(required_counts.items()):
            selected = selected_counts.get(day_number, {})
            for intent_type, required_count in counts.items():
                selected_count = int(selected.get(intent_type) or 0)
                if selected_count < required_count:
                    missing_required_slots.append(
                        {
                            "dayNumber": day_number,
                            "intentType": intent_type,
                            "targetCount": required_count,
                            "selectedCount": selected_count,
                            "missingCount": required_count - selected_count,
                            "reason": "daily_theme_required_slots_missing",
                        }
                    )

        unresolved_required = [
            {
                "slotId": item.get("slotId"),
                "dayNumber": item.get("dayNumber"),
                "intentType": item.get("intentType"),
                "reason": item.get("reason") or item.get("state") or "unresolved_route_anchor",
                "state": item.get("state") or "waiting_for_poi_grounding",
            }
            for item in unresolved
            if item.get("slotId") or item.get("reason") not in {"route_skipped_not_enough_anchors"}
        ]
        if unresolved_required:
            missing_required_slots.extend(unresolved_required)

        route_quality = self._route_quality(persistable_plans)
        if self._prefers_transit(pipeline_context):
            non_transit = [mode for mode in route_quality.get("selectedModes", []) if mode and mode != "transit"]
            if non_transit:
                soft_warnings.append("公交地铁优先下存在非 transit route 估算，需在路线刷新时展示 fallback caveat。")

        if placeholder_segments:
            hard_failures.append("required_placeholder_segments_present")
        if weak_poi_violations:
            hard_failures.append("weak_poi_selected")
        if missing_required_slots:
            hard_failures.append("required_slots_missing")

        hard_failures = list(dict.fromkeys(hard_failures))
        # This field controls active-version eligibility, never draft
        # persistence. Hard failures must therefore keep it false regardless
        # of whether the caller allows saving an unfinished draft.
        can_create = not hard_failures
        return ItineraryQualityReport(
            can_create_active_version=can_create,
            quality_status="pass" if can_create and not hard_failures else "draft_needs_completion",
            hard_failures=hard_failures,
            soft_warnings=list(dict.fromkeys(soft_warnings)),
            placeholder_segments=placeholder_segments,
            weak_poi_violations=weak_poi_violations,
            missing_required_slots=missing_required_slots,
            route_quality=route_quality,
        )

    def _required_counts_by_day(self, day_slots: list[DaySlot]) -> dict[int, dict[str, int]]:
        counts: dict[int, dict[str, int]] = {}
        for slot in day_slots:
            if not slot.route_anchor:
                continue
            intent_type = self._intent_type_for_slot(slot)
            if intent_type not in {"campus_visit", "meal", "night_view"}:
                continue
            counts.setdefault(slot.day_number, {"campus_visit": 0, "meal": 0, "night_view": 0})[intent_type] += 1
        return counts

    def _intent_type_for_plan(self, plan: PersistableSegmentPlan, slot: Optional[DaySlot]) -> str:
        if slot is not None:
            return self._intent_type_for_slot(slot)
        text = f"{plan.kind} {plan.display_title} {plan.notes}"
        if plan.kind == "meal" or re.search(r"(午餐|晚餐|早餐|美食|餐厅)", text):
            return "meal"
        if re.search(r"(高校|大学|学院|校园|校区|985|211)", text):
            return "campus_visit"
        if re.search(r"(夜景|夜游|观景|灯光)", text):
            return "night_view"
        return str(plan.kind or "visit")

    def _intent_type_for_slot(self, slot: DaySlot) -> str:
        text = f"{slot.kind} {slot.raw_need} {slot.notes}"
        if slot.kind == "meal" or re.search(r"(午餐|晚餐|早餐|美食|餐厅)", text):
            return "meal"
        if re.search(r"(高校|大学|学院|校园|校区|985|211)", text):
            return "campus_visit"
        if slot.kind == "night_view" or re.search(r"(夜景|夜游|观景|灯光)", text):
            return "night_view"
        return str(slot.kind or "visit")

    def _is_generic_title(self, display_title: str, poi_name: str) -> bool:
        title = re.sub(r"\s+", " ", str(display_title or poi_name or "")).strip()
        return title in GENERIC_REQUIRED_TITLES

    def _is_real_amap_poi(self, poi: POI) -> bool:
        if not poi.amap_id or poi.source != AMAP_PLACE_SOURCE:
            return False
        if float(poi.confidence or 0) < 0.8:
            return False
        try:
            lon = float(poi.longitude)
            lat = float(poi.latitude)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(lon)
            and math.isfinite(lat)
            and -180 <= lon <= 180
            and -90 <= lat <= 90
            and (lon != 0 or lat != 0)
        )

    def _weak_reason(
        self, intent_type: str, plan: PersistableSegmentPlan, poi: POI, pipeline_context: dict[str, Any]
    ) -> str:
        text = self._poi_text(poi)
        if intent_type == "campus_visit":
            request_text = f"{pipeline_context.get('latestUserMessage') or ''} {pipeline_context.get('effectiveUserMessage') or ''} {plan.display_title} {plan.notes}"
            return self.campus_policy.reject_reason(poi, request_text)
        if intent_type == "meal":
            request_text = f"{pipeline_context.get('latestUserMessage') or ''} {pipeline_context.get('effectiveUserMessage') or ''} {plan.display_title} {plan.notes}"
            city = self._selected_city(pipeline_context)
            quality = self.meal_policy.evaluate(request_text, [], poi, city=city)
            if not quality.acceptable:
                return quality.hard_reject_reasons[0] if quality.hard_reject_reasons else "weak_meal_entity"
        if intent_type == "night_view":
            return self.night_view_policy.reject_reason(poi)
        return ""

    def _poi_text(self, poi: POI) -> str:
        return " ".join(
            str(value or "") for value in [poi.name, poi.type, poi.category, poi.address, poi.district, poi.source_note]
        )

    def _selected_city(self, pipeline_context: dict[str, Any]) -> str:
        city = str(pipeline_context.get("selectedCity") or "").strip()
        fields = (
            (pipeline_context.get("understoodRequirements") or {}).get("fields")
            if isinstance(pipeline_context.get("understoodRequirements"), dict)
            else {}
        )
        if not city and isinstance(fields, dict):
            city = str(fields.get("destination") or "").strip()
        return city

    def _plan_ref(self, plan: PersistableSegmentPlan, slot: Optional[DaySlot], reason: str) -> dict[str, Any]:
        return {
            "dayNumber": plan.day_number,
            "slotId": slot.slot_id if slot is not None else None,
            "timeWindow": slot.time_window if slot is not None else None,
            "startTime": plan.start_time,
            "kind": plan.kind,
            "displayTitle": plan.display_title,
            "groundingStatus": plan.grounding_status,
            "reason": reason,
        }

    def _route_quality(self, plans: list[PersistableSegmentPlan]) -> dict[str, Any]:
        day_distances: dict[int, float] = {}
        max_leg = 0.0
        selected_modes: list[str] = []
        for day_number in sorted({plan.day_number for plan in plans}):
            route_plans = [
                plan
                for plan in sorted(plans, key=lambda item: item.start_time)
                if plan.day_number == day_number and plan.route_anchor and plan.selected_poi is not None
            ]
            previous: Optional[POI] = None
            for plan in route_plans:
                if plan.transport_mode:
                    selected_modes.append(self._normalize_transport_mode(plan.transport_mode))
                poi = plan.selected_poi
                if previous is not None and poi is not None:
                    distance = self._distance_km(previous, poi)
                    if distance is not None:
                        day_distances[day_number] = day_distances.get(day_number, 0.0) + distance
                        max_leg = max(max_leg, distance)
                previous = poi
        return {
            "totalDistanceKm": round(sum(day_distances.values()), 2),
            "maxDayDistanceKm": round(max(day_distances.values()) if day_distances else 0.0, 2),
            "maxLegDistanceKm": round(max_leg, 2),
            "selectedModes": sorted(set(selected_modes)),
            "distanceSource": "straight_line_coarse_diagnostic",
            "decisionRole": "ordering_and_diagnostics_only",
        }

    def _distance_km(self, a: POI, b: POI) -> Optional[float]:
        try:
            lon1 = math.radians(float(a.longitude))
            lat1 = math.radians(float(a.latitude))
            lon2 = math.radians(float(b.longitude))
            lat2 = math.radians(float(b.latitude))
        except (TypeError, ValueError):
            return None
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))

    def _prefers_transit(self, pipeline_context: dict[str, Any]) -> bool:
        text = (
            str(pipeline_context.get("latestUserMessage") or "")
            + " "
            + str(pipeline_context.get("effectiveUserMessage") or "")
        )
        fields = (
            (pipeline_context.get("understoodRequirements") or {}).get("fields")
            if isinstance(pipeline_context.get("understoodRequirements"), dict)
            else {}
        )
        transport = str((fields or {}).get("transportPreference") or "")
        return bool(re.search(r"(公交|地铁|公共交通|transit)", f"{text} {transport}", re.IGNORECASE))

    def _normalize_transport_mode(self, mode: str) -> str:
        normalized = str(mode or "").strip().lower()
        if normalized in {"public_transit", "bus", "metro", "subway"}:
            return "transit"
        if normalized in {"walk"}:
            return "walking"
        return normalized

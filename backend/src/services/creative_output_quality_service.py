"""City-neutral authoritative output-quality projection for Creative Portfolio.

The service classifies persisted evidence only. It never searches, invents
places, refreshes routes, or writes itinerary state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any

from src.services.creative_proposal_title_service import (
    CreativeProposalTitleService,
)
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


@dataclass(frozen=True)
class ThemeCompletionBudget:
    deadline_seconds: int = 20
    amap_detail_limit: int = 4
    identity_web_limit: int = 2
    auto_attempt_limit: int = 1

    def to_trace(self) -> dict[str, Any]:
        return {**asdict(self), "writeAuthority": "none"}


class CreativeOutputQualityService:
    THEME = "local_food_and_area_walk"
    AREA_FAMILIES = {
        "local_life",
        "market_walk",
        "heritage_walk",
        "art_walk",
        "area_walk",
    }
    MEAL_FAMILIES = {"meal", "local_food", "food"}

    @classmethod
    def evaluate(
        cls,
        snapshot: dict[str, Any],
        *,
        requested_theme: str = "",
    ) -> dict[str, Any]:
        segments_by_day: dict[int, list[dict[str, Any]]] = {}
        for raw_day in snapshot.get("days") or []:
            if not isinstance(raw_day, dict):
                continue
            day_number = int(raw_day.get("dayNumber") or 0)
            segments_by_day[day_number] = [
                item
                for item in raw_day.get("segments") or []
                if isinstance(item, dict) and cls.is_canonical_amap_poi(item.get("poi"))
            ]

        pending = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        pending_by_day: dict[int, int] = {}
        for item in pending:
            day = int(item.get("dayNumber") or 0)
            pending_by_day[day] = pending_by_day.get(day, 0) + 1

        grounded = sum(len(items) for items in segments_by_day.values())
        total = grounded + len(pending)
        pending_ratio = round(len(pending) / total, 4) if total else 1.0
        day_numbers = [int(day.get("dayNumber") or 0) for day in snapshot.get("days") or [] if isinstance(day, dict)]
        quality_failures: list[str] = []
        if grounded < 3:
            quality_failures.append("grounded_segment_floor_not_met")
        if any(not segments_by_day.get(day) for day in day_numbers):
            quality_failures.append("daily_grounded_segment_missing")
        if pending_ratio > 0.65:
            quality_failures.append("pending_ratio_exceeded")
        if any(count > 3 for count in pending_by_day.values()):
            quality_failures.append("pending_per_day_exceeded")

        admitted_meals: list[dict[str, Any]] = []
        area_segments: list[dict[str, Any]] = []
        area_segment_scope: dict[str, tuple[int, str]] = {}
        for day_number, items in segments_by_day.items():
            for segment in items:
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                family = str(semantic.get("optionalExperienceFamily") or semantic.get("family") or "")
                coverage_roles = {str(item).casefold() for item in semantic.get("coverageRoles") or []}
                report = (
                    semantic.get("consumerAdmissionReport")
                    if isinstance(semantic.get("consumerAdmissionReport"), dict)
                    else {}
                )
                role_reports = (
                    semantic.get("coverageRoleAdmissionReports")
                    if isinstance(semantic.get("coverageRoleAdmissionReports"), dict)
                    else {}
                )
                if not report and isinstance(segment.get("consumerAdmissionReport"), dict):
                    report = segment["consumerAdmissionReport"]
                admitted = bool(
                    report.get("scoreEligible") is True
                    or str(report.get("classification") or "").startswith("admitted_")
                )
                area_role_report = role_reports.get("area_walk_anchor")
                area_role_admitted = bool(
                    isinstance(area_role_report, dict)
                    and (
                        area_role_report.get("scoreEligible") is True
                        or str(area_role_report.get("classification") or "").startswith("admitted_")
                    )
                )
                if admitted and family in cls.MEAL_FAMILIES:
                    admitted_meals.append(segment)
                if admitted and (
                    family in cls.AREA_FAMILIES or ("area_walk_anchor" in coverage_roles and area_role_admitted)
                ):
                    area_segments.append(segment)
                    area_segment_scope[str(segment.get("id") or "")] = (
                        day_number,
                        str(
                            semantic.get("creativeBriefId")
                            or semantic.get("briefId")
                            or semantic.get("brief_id")
                            or segment.get("briefId")
                            or ""
                        ),
                    )

        area_identity = {
            PoiPhysicalIdentityService.canonical_amap_id(item.get("poi") or {})
            for item in area_segments
            if PoiPhysicalIdentityService.canonical_amap_id(item.get("poi") or {})
        }
        area_segment_ids = {str(item.get("id") or "") for item in area_segments}
        route_rows = [
            *(snapshot.get("routeOptions") or []),
            *(snapshot.get("portfolioRouteEvidence") or []),
            *(snapshot.get("portfolioThemeWalkingEvidence") or []),
        ]
        walking_verified = False
        for route in route_rows:
            if not isinstance(route, dict):
                continue
            from_id = str(route.get("fromSegmentId") or "")
            to_id = str(route.get("toSegmentId") or "")
            if from_id not in area_segment_ids or to_id not in area_segment_ids:
                continue
            from_day, from_brief = area_segment_scope.get(from_id, (0, ""))
            to_day, to_brief = area_segment_scope.get(to_id, (0, ""))
            same_authorized_scope = bool(
                from_day > 0 and from_day == to_day and bool(from_brief) and from_brief == to_brief
            )
            if (
                same_authorized_scope
                and str(route.get("mode") or route.get("transportMode") or "").casefold() == "walking"
                and str(route.get("status") or "").casefold() == "verified"
                and 8 * 60 <= int(route.get("durationSeconds") or 0) <= 35 * 60
            ):
                walking_verified = True
                break
        theme_eligible = bool(len(admitted_meals) >= 1 and len(area_identity) >= 2 and walking_verified)
        theme_requested = requested_theme == cls.THEME
        skeleton_preview_only = bool(
            "pending_ratio_exceeded" in quality_failures or "pending_per_day_exceeded" in quality_failures
        )
        visibility_mode = (
            "skeleton_preview_only" if skeleton_preview_only else "themed" if theme_eligible else "neutral_skeleton"
        )
        neutral_title = CreativeProposalTitleService.incomplete_status_title(snapshot)
        title_projection = snapshot.get("portfolioTitleEvidence")
        title_generation = (
            snapshot.get("portfolioTitleGeneration")
            if isinstance(snapshot.get("portfolioTitleGeneration"), dict)
            else {}
        )
        strict_complete_proposal = bool(
            isinstance(snapshot.get("portfolioVerifier"), dict)
            and snapshot["portfolioVerifier"].get("passed") is True
            and not pending
            and not snapshot.get("portfolioPartialTimeline")
            and str(snapshot.get("originProjectionMode") or "") != "partial_preview"
        )
        agent_title_ready = bool(
            strict_complete_proposal
            and title_generation.get("status") == "succeeded"
            and CreativeProposalTitleService.is_valid_agent_projection(
                snapshot,
                title_projection,
            )
        )
        grounded_content_title = str(title_projection.get("title") or "").strip() if agent_title_ready else ""
        return {
            "schemaVersion": "creative-output-quality-v2",
            "requestedTheme": requested_theme or None,
            "themeEligible": theme_eligible,
            "visibilityMode": visibility_mode,
            "displayTitle": (
                grounded_content_title or ("标题生成待重试" if strict_complete_proposal else neutral_title)
            ),
            "admittedLocalMealCount": len(admitted_meals),
            "distinctAreaWalkPhysicalAnchorCount": len(area_identity),
            "areaWalkWalkingRelationVerified": walking_verified,
            "groundedSegmentCount": grounded,
            "pendingSlotCount": len(pending),
            "pendingRatio": pending_ratio,
            "pendingCountByDay": {str(day): count for day, count in sorted(pending_by_day.items())},
            "neutralPartialQualityPassed": not quality_failures,
            "qualityFailureReasons": quality_failures,
            "completionAction": (
                {
                    "kind": "portfolio_theme_completion",
                    "theme": cls.THEME,
                    "label": "尝试补全“地方饮食与街区”",
                    "budget": ThemeCompletionBudget().to_trace(),
                }
                if theme_requested and not theme_eligible
                else None
            ),
        }

    @staticmethod
    def is_canonical_amap_poi(value: Any) -> bool:
        if not isinstance(value, dict) or str(value.get("source") or "") != "amap-place-search":
            return False
        amap_id = str(value.get("amapId") or "").strip().upper()
        if not re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id):
            return False
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and latitude != 0
            and longitude != 0
        )

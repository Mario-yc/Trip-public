"""Evidence-led clock placement for Simple Direction proposals.

Day-part words such as ``evening`` are semantic preferences, not clock
windows.  This service derives a concrete placement from the selected POI,
trip date, local solar boundary, opening evidence, neighbouring activities and
any server-verified arrival/lock constraints.  It never calls a Provider and
never persists an itinerary write.
"""

from __future__ import annotations

import copy
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from src.models.poi_intent import PersistableSegmentPlan


_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_OPENING_RE = re.compile(
    r"(?P<start>(?:[01]\d|2[0-3]):[0-5]\d)\s*[-–—至到]\s*"
    r"(?P<end>(?:[01]\d|2[0-3]):[0-5]\d)"
)


class SimpleOpenDynamicScheduleService:
    """Place Simple Direction segments without intent-level clock defaults."""

    def schedule(self, plans: Iterable[PersistableSegmentPlan]) -> list[PersistableSegmentPlan]:
        scheduled = [copy.deepcopy(item) for item in plans]
        by_day: dict[int, list[PersistableSegmentPlan]] = defaultdict(list)
        for plan in scheduled:
            by_day[int(plan.day_number)].append(plan)

        ordered: list[PersistableSegmentPlan] = []
        for day_number in sorted(by_day):
            day_plans = by_day[day_number]
            controller_sequence_authoritative = self.controller_sequence_authoritative(day_plans)
            day_plans.sort(
                key=lambda item: self.semantic_order_key(
                    item.schedule_preference,
                    item.start_time,
                    tie_breaker=str(item.planning_slot_id or item.occurrence_id or ""),
                    controller_sequence_authoritative=controller_sequence_authoritative,
                    explicit_start_time=(item.schedule_constraints or {}).get("explicitStartTime"),
                )
            )
            if not controller_sequence_authoritative:
                # The provider DaySlot order is candidate material.  When no
                # typed Controller sequence exists, reseal relative order from
                # the server-owned semantic day parts so noon cannot render
                # after an evening park merely because a slot id/order drifted.
                for sequence, plan in enumerate(day_plans, start=1):
                    preference = copy.deepcopy(plan.schedule_preference or {})
                    preference["sequence"] = sequence
                    preference["sequenceSource"] = "server_semantic_schedule_order"
                    plan.schedule_preference = preference
            previous_end: int | None = None
            for plan in day_plans:
                self._schedule_plan(plan, previous_end=previous_end)
                if plan.start_time and int(plan.duration_minutes or 0) > 0:
                    previous_end = self._minutes(plan.start_time) + int(plan.duration_minutes)
            ordered.extend(day_plans)

        return ordered

    def _schedule_plan(self, plan: PersistableSegmentPlan, *, previous_end: int | None) -> None:
        preference = dict(plan.schedule_preference or {})
        constraints = dict(plan.schedule_constraints or {})
        day_part = str(preference.get("dayPart") or "").strip().casefold()
        explicit_start = str(constraints.get("explicitStartTime") or "").strip()
        typed_duration_source = str(constraints.get("durationEstimateSource") or "").strip()
        duration_source = (
            typed_duration_source
            if typed_duration_source in {"controller_estimate", "server_policy_estimate"}
            else "controller_estimate"
        )
        raw_duration = int(plan.duration_minutes or 0)
        if raw_duration <= 0:
            raw_duration = self._typed_duration_estimate_minutes(constraints) or 0
        if raw_duration <= 0:
            plan.start_time = ""
            plan.schedule_decision = {
                "startTime": None,
                "endTime": None,
                "durationMinutes": None,
                "durationSource": "unavailable",
                "decisionSource": "dynamic_schedule_solver",
                "openingEvidenceStatus": "unverified",
                "solarBoundarySource": "not_evaluated_without_duration",
                "routeArrivalSource": "unverified",
                "scheduleConfidence": "flexible",
                "provisionalReasons": ["duration_estimate_unavailable"],
                # Missing evidence is not a proven constraint conflict.  The
                # slot remains editable/flexible and must not be promoted to a
                # verified clock or rejected as a hard scheduling failure.
                "constraintPassed": True,
            }
            return
        duration = raw_duration
        plan.duration_minutes = duration
        provisional_reasons: list[str] = []
        route_arrival = self._clock_minutes(constraints.get("routeArrivalTime"))
        try:
            route_travel_minutes = int(constraints.get("routeTravelMinutesFromPrevious") or 0)
        except (TypeError, ValueError):
            route_travel_minutes = 0
        if route_arrival is None and previous_end is not None and route_travel_minutes > 0:
            route_arrival = previous_end + route_travel_minutes
        route_arrival_source = (
            str(constraints.get("routeArrivalSource") or "verified_route")
            if route_arrival is not None
            else "unverified"
        )

        if explicit_start and _CLOCK_RE.fullmatch(explicit_start):
            start = self._minutes(explicit_start)
            decision_source = "user_explicit_clock"
            solar_source = "not_required_for_explicit_clock"
        elif day_part in {"evening", "night"}:
            trip_date = self._date(plan.date)
            poi = plan.selected_poi
            sunset = (
                self.local_sunset_minutes(trip_date, latitude=poi.latitude, longitude=poi.longitude)
                if trip_date is not None and poi is not None and poi.latitude is not None and poi.longitude is not None
                else None
            )
            if sunset is None:
                plan.start_time = ""
                plan.schedule_decision = {
                    "startTime": None,
                    "endTime": None,
                    "durationMinutes": duration,
                    "durationSource": duration_source,
                    "decisionSource": "dynamic_schedule_solver",
                    "openingEvidenceStatus": "unverified",
                    "solarBoundarySource": "unavailable",
                    "routeArrivalSource": route_arrival_source,
                    "scheduleConfidence": "unresolved",
                    "provisionalReasons": ["solar_boundary_unavailable"],
                    "constraintPassed": False,
                    "failureReason": "semantic_evening_boundary_unavailable",
                }
                return
            start = sunset
            decision_source = "dynamic_schedule_solver"
            solar_source = "local_sunset_from_date_and_coordinates"
        else:
            start = self._clock_minutes(plan.start_time) or self._clock_minutes(
                constraints.get("preferredStartTime")
            )
            decision_source = (
                "server_policy_slot_estimate"
                if duration_source == "server_policy_estimate"
                else "controller_slot_estimate"
            )
            solar_source = "not_applicable"
            if start is None:
                plan.start_time = ""
                plan.schedule_decision = {
                    "startTime": None,
                    "endTime": None,
                    "durationMinutes": duration,
                    "durationSource": duration_source,
                    "decisionSource": decision_source,
                    "openingEvidenceStatus": "unverified",
                    "solarBoundarySource": solar_source,
                    "routeArrivalSource": route_arrival_source,
                    "scheduleConfidence": "unresolved",
                    "provisionalReasons": ["controller_slot_time_unavailable"],
                    "constraintPassed": False,
                    "failureReason": "schedule_input_unavailable",
                }
                return

        earliest_start = self._clock_minutes(constraints.get("earliestStart"))
        latest_start = self._clock_minutes(constraints.get("latestStart"))
        next_locked_start = self._clock_minutes(constraints.get("nextLockedStartTime"))
        day_end = self._clock_minutes(constraints.get("dayEndTime"))
        locked_boundary_end = (
            next_locked_start if next_locked_start is not None else day_end
        )
        estimated_window_end = self._clock_minutes(constraints.get("windowEnd"))
        window_end = (
            locked_boundary_end
            if locked_boundary_end is not None
            else estimated_window_end
        )
        # Explicit ``False`` is the only soft-window signal.  Missing legacy
        # values remain hard, while a following locked activity/day boundary
        # is authoritative regardless of the estimate flag.
        hard_window = constraints.get("hard") is not False
        if earliest_start is not None:
            start = max(start, earliest_start)

        if previous_end is not None:
            start = max(start, previous_end)
        if route_arrival is not None:
            start = max(start, route_arrival)
        elif previous_end is not None:
            provisional_reasons.append("route_arrival_unverified")

        opening = self._opening_interval(plan)
        if opening is None:
            opening_status = "unverified"
            provisional_reasons.append("opening_hours_unverified")
        else:
            opening_status = "verified_provider_evidence"
            opening_start, opening_end = opening
            start = max(start, opening_start)
            if start + duration > opening_end:
                plan.start_time = ""
                plan.schedule_decision = {
                    "startTime": None,
                    "endTime": None,
                    "durationMinutes": duration,
                    "durationSource": duration_source,
                    "decisionSource": decision_source,
                    "openingEvidenceStatus": opening_status,
                    "solarBoundarySource": solar_source,
                    "routeArrivalSource": route_arrival_source,
                    "scheduleConfidence": "rejected",
                    "provisionalReasons": provisional_reasons,
                    "constraintPassed": False,
                    "failureReason": "opening_window_does_not_overlap_semantic_evening",
                }
                return

        start = self._round_up(start, 5)
        end = start + duration
        if hard_window and latest_start is not None and start > latest_start:
            self._reject(
                plan,
                duration,
                decision_source,
                opening_status,
                solar_source,
                route_arrival_source,
                provisional_reasons,
                "latest_start_constraint_missed",
                duration_source=duration_source,
            )
            return
        if (hard_window or locked_boundary_end is not None) and window_end is not None and end > window_end:
            self._reject(
                plan,
                duration,
                decision_source,
                opening_status,
                solar_source,
                route_arrival_source,
                provisional_reasons,
                "next_locked_activity_or_day_boundary_conflict",
                duration_source=duration_source,
            )
            return
        plan.start_time = self._format(start)
        plan.duration_minutes = duration
        plan.schedule_decision = {
            "startTime": self._format(start),
            "endTime": self._format(end),
            "durationMinutes": duration,
            "durationSource": "user_explicit" if decision_source == "user_explicit_clock" else duration_source,
            "decisionSource": decision_source,
            "openingEvidenceStatus": opening_status,
            "solarBoundarySource": solar_source,
            "routeArrivalSource": route_arrival_source,
            "scheduleConfidence": "provisional" if provisional_reasons else "verified_constraints",
            "provisionalReasons": list(dict.fromkeys(provisional_reasons)),
            "constraintPassed": True,
        }

    @staticmethod
    def _typed_duration_estimate_minutes(constraints: dict[str, object]) -> int | None:
        """Read only a bounded Controller estimate sealed by the server contract."""

        if str(constraints.get("durationEstimateSource") or "") not in {
            "controller_estimate",
            "server_policy_estimate",
        }:
            return None
        estimate = constraints.get("durationEstimate")
        if not isinstance(estimate, dict):
            return None
        values = (estimate.get("min"), estimate.get("preferred"), estimate.get("max"))
        if any(isinstance(value, bool) for value in values):
            return None
        try:
            minimum, preferred, maximum = (int(value) for value in values)
        except (TypeError, ValueError):
            return None
        confidence = constraints.get("durationEstimateConfidence")
        if isinstance(confidence, bool):
            return None
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            return None
        if not (0 < minimum <= preferred <= maximum <= 720):
            return None
        if not 0 <= confidence_value <= 1:
            return None
        return preferred

    @classmethod
    def semantic_evening_clock_failure(
        cls,
        *,
        trip_date: object,
        latitude: object,
        longitude: object,
        start_time: object,
        schedule_preference: dict[str, Any] | None,
        schedule_constraints: dict[str, Any] | None = None,
    ) -> str | None:
        """Recheck the materialized clock without trusting a stored success flag.

        Callers may supply the current authoritative occurrence preference to
        validate older proposal material.  This check neither rewrites that
        material nor treats missing opening hours as verified opening evidence.
        """
        preference = schedule_preference if isinstance(schedule_preference, dict) else {}
        if str(preference.get("dayPart") or "").strip().casefold() not in {"evening", "night"}:
            return None
        constraints = schedule_constraints if isinstance(schedule_constraints, dict) else {}
        explicit_start = cls._clock_minutes(constraints.get("explicitStartTime"))
        if explicit_start is not None and constraints.get("source") == "user_explicit_clock":
            # The user's concrete clock takes precedence over a day-part label,
            # as it does in the scheduling kernel.
            return None if cls._clock_minutes(start_time) == explicit_start else "explicit_clock_mismatch"
        parsed_date = cls._date(trip_date)
        try:
            if isinstance(latitude, bool) or isinstance(longitude, bool):
                raise ValueError("invalid_coordinates")
            lat, lon = float(latitude), float(longitude)
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("invalid_coordinates")
        except (TypeError, ValueError):
            return "semantic_evening_boundary_unavailable"
        sunset = cls.local_sunset_minutes(parsed_date, latitude=lat, longitude=lon) if parsed_date else None
        if sunset is None:
            return "semantic_evening_boundary_unavailable"
        actual_start = cls._clock_minutes(start_time)
        if actual_start is None or actual_start < sunset:
            return "semantic_evening_clock_mismatch"
        return None

    @classmethod
    def candidate_open_for_semantic_preference(
        cls,
        *,
        trip_date: str | None,
        latitude: float | None,
        longitude: float | None,
        open_time_today: str | None,
        day_part: str,
        duration_minutes: int,
    ) -> bool | None:
        """Return False only when Provider facts prove the candidate impossible."""

        if str(day_part or "").casefold() not in {"evening", "night"}:
            return True
        if int(duration_minutes or 0) <= 0:
            return None
        parsed_date = cls._date(trip_date)
        if parsed_date is None or latitude is None or longitude is None:
            return None
        sunset = cls.local_sunset_minutes(parsed_date, latitude=latitude, longitude=longitude)
        opening = cls._parse_opening(open_time_today)
        if sunset is None or opening is None:
            return None
        return max(sunset, opening[0]) + max(1, int(duration_minutes or 0)) <= opening[1]

    @staticmethod
    def _reject(
        plan: PersistableSegmentPlan,
        duration: int,
        decision_source: str,
        opening_status: str,
        solar_source: str,
        route_arrival_source: str,
        provisional_reasons: list[str],
        reason: str,
        *,
        duration_source: str = "controller_estimate",
    ) -> None:
        plan.start_time = ""
        plan.schedule_decision = {
            "startTime": None,
            "endTime": None,
            "durationMinutes": duration,
            "durationSource": duration_source,
            "decisionSource": decision_source,
            "openingEvidenceStatus": opening_status,
            "solarBoundarySource": solar_source,
            "routeArrivalSource": route_arrival_source,
            "scheduleConfidence": "rejected",
            "provisionalReasons": list(dict.fromkeys(provisional_reasons)),
            "constraintPassed": False,
            "failureReason": reason,
        }

    @staticmethod
    def local_sunset_minutes(value: date, *, latitude: float, longitude: float) -> int | None:
        """NOAA-style sunset calculation converted to China's civil timezone.

        The returned clock is derived from date and coordinates.  No activity
        time or day-part clock is encoded here.
        """

        day_of_year = value.timetuple().tm_yday
        lng_hour = float(longitude) / 15.0
        approximate = day_of_year + ((18.0 - lng_hour) / 24.0)
        mean_anomaly = (0.9856 * approximate) - 3.289
        true_longitude = (
            mean_anomaly
            + 1.916 * math.sin(math.radians(mean_anomaly))
            + 0.020 * math.sin(math.radians(2 * mean_anomaly))
            + 282.634
        ) % 360.0
        right_ascension = math.degrees(math.atan(0.91764 * math.tan(math.radians(true_longitude)))) % 360.0
        right_ascension += (math.floor(true_longitude / 90.0) * 90.0) - (math.floor(right_ascension / 90.0) * 90.0)
        right_ascension /= 15.0
        sin_declination = 0.39782 * math.sin(math.radians(true_longitude))
        cos_declination = math.cos(math.asin(sin_declination))
        denominator = cos_declination * math.cos(math.radians(float(latitude)))
        if denominator == 0:
            return None
        cos_hour = (math.cos(math.radians(90.833)) - sin_declination * math.sin(math.radians(latitude))) / denominator
        if cos_hour < -1.0 or cos_hour > 1.0:
            return None
        local_hour_angle = math.degrees(math.acos(cos_hour)) / 15.0
        local_mean_time = local_hour_angle + right_ascension - (0.06571 * approximate) - 6.622
        utc_hour = (local_mean_time - lng_hour) % 24.0
        utc_dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc) + timedelta(hours=utc_hour)
        china_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
        return china_dt.hour * 60 + china_dt.minute

    @staticmethod
    def controller_sequence_authoritative(plans: Iterable[PersistableSegmentPlan]) -> bool:
        candidates = list(plans)
        return SimpleOpenDynamicScheduleService.controller_sequence_authoritative_for_preferences(
            (
                plan.schedule_preference,
                str(plan.planning_slot_id or plan.occurrence_id or ""),
            )
            for plan in candidates
        )

    @staticmethod
    def controller_sequence_authoritative_for_preferences(
        preferences: Iterable[tuple[dict[str, object] | None, str]],
    ) -> bool:
        """Validate one Controller order before any stage treats it as authoritative.

        Candidate acquisition and final scheduling must use the same decision.
        Otherwise a slot can be searched around one predecessor and later be
        rendered beside another after the scheduler corrects a semantic inversion.
        """

        candidates = [
            (preference if isinstance(preference, dict) else {}, str(tie_breaker or ""))
            for preference, tie_breaker in preferences
        ]
        has_controller_sequence = any(
            str(preference.get("sequenceSource") or "") == "controller_schedule_hint"
            for preference, _tie_breaker in candidates
        )
        if not has_controller_sequence:
            return False
        if any(
            str(preference.get("sequenceSource") or "") != "controller_schedule_hint"
            for preference, _tie_breaker in candidates
        ):
            # A server-owned completion slot was not part of the Controller's
            # relative sequence decision.  Treating the mixed list as wholly
            # Controller-authored can place an afternoon supplement before an
            # explicit noon meal.  Reseal the complete day from semantic day
            # parts so acquisition, route assignment and final rendering share
            # the same chronological order.
            return False
        day_part_rank = {
            "early_morning": 0,
            "morning": 1,
            "flexible": 2,
            "noon": 3,
            "afternoon": 4,
            "evening": 5,
            "night": 6,
        }
        explicit_semantic_order: list[tuple[int, int, str]] = []
        for preference, tie_breaker in candidates:
            day_part = str(preference.get("dayPart") or "").casefold()
            if preference.get("userExplicit") is not True or day_part not in day_part_rank:
                continue
            try:
                sequence = int(preference.get("sequence"))
            except (TypeError, ValueError):
                return False
            explicit_semantic_order.append(
                (sequence, day_part_rank[day_part], tie_breaker)
            )
        ordered_ranks = [item[1] for item in sorted(explicit_semantic_order)]
        return ordered_ranks == sorted(ordered_ranks)

    @classmethod
    def semantic_order_key(
        cls,
        preference: dict[str, object] | None,
        start_time: object,
        *,
        tie_breaker: str = "",
        controller_sequence_authoritative: bool = False,
        explicit_start_time: object = None,
    ) -> tuple[int, int, str]:
        safe_preference = preference if isinstance(preference, dict) else {}
        day_part = str(safe_preference.get("dayPart") or "").casefold()
        try:
            sequence = int(safe_preference.get("sequence") or 999)
        except (TypeError, ValueError):
            sequence = 999
        clock_minutes = cls._clock_minutes(start_time)
        explicit_minutes = cls._clock_minutes(explicit_start_time)
        day_part_minutes = {
            "early_morning": 7 * 60,
            "morning": 9 * 60,
            "flexible": 10 * 60,
            "noon": 12 * 60,
            "afternoon": 15 * 60,
            "evening": 18 * 60,
            "night": 20 * 60,
        }.get(day_part)
        # An explicit user clock is a hard constraint.  A model-authored clock
        # is only an estimate and must not move an explicit noon/evening intent
        # across its semantic day part when no Controller sequence exists.
        semantic_minutes = (
            explicit_minutes
            if explicit_minutes is not None
            else day_part_minutes
            if day_part_minutes is not None
            else clock_minutes
            if clock_minutes is not None
            else 10 * 60
        )
        if controller_sequence_authoritative:
            return sequence, semantic_minutes, tie_breaker
        return semantic_minutes, sequence, tie_breaker

    @classmethod
    def _opening_interval(cls, plan: PersistableSegmentPlan) -> tuple[int, int] | None:
        poi = plan.selected_poi
        if poi is None:
            return None
        return cls._parse_opening(poi.open_time_today) or cls._parse_opening(poi.open_time_week)

    @staticmethod
    def _parse_opening(value: object) -> tuple[int, int] | None:
        match = _OPENING_RE.search(str(value or ""))
        if not match:
            return None
        start = SimpleOpenDynamicScheduleService._minutes(match.group("start"))
        end = SimpleOpenDynamicScheduleService._minutes(match.group("end"))
        if end <= start:
            end += 24 * 60
        return start, end

    @staticmethod
    def _date(value: object) -> date | None:
        try:
            return date.fromisoformat(str(value or ""))
        except ValueError:
            return None

    @staticmethod
    def _clock_minutes(value: object) -> int | None:
        text = str(value or "").strip()
        return SimpleOpenDynamicScheduleService._minutes(text) if _CLOCK_RE.fullmatch(text) else None

    @staticmethod
    def _minutes(value: object) -> int:
        text = str(value or "00:00")
        try:
            hour, minute = text.split(":", 1)
            return int(hour) * 60 + int(minute)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _round_up(value: int, step: int) -> int:
        return int(math.ceil(value / step) * step)

    @staticmethod
    def _format(value: int) -> str:
        value %= 24 * 60
        return f"{value // 60:02d}:{value % 60:02d}"

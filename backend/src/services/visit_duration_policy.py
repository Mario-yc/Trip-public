from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class DurationEstimate:
    preferred_minutes: int
    min_minutes: int
    max_minutes: int
    source: str
    confidence: float
    factors: tuple[str, ...] = ()
    user_locked: bool = False

    @property
    def duration_minutes(self) -> int:
        """Compatibility alias for callers that only need the preferred point value."""
        return self.preferred_minutes

    def to_metadata(self) -> dict[str, Any]:
        return {
            "duration": {
                "preferredMinutes": self.preferred_minutes,
                "minMinutes": self.min_minutes,
                "maxMinutes": self.max_minutes,
                "source": self.source,
                "confidence": self.confidence,
                "factors": list(self.factors),
                "userLocked": self.user_locked,
            }
        }


DurationDecision = DurationEstimate


class VisitDurationPolicy:
    RULES: dict[str, tuple[int, int, int]] = {
        "landmark": (90, 45, 180),
        "scenic": (90, 45, 180),
        "visit": (90, 45, 180),
        "museum": (150, 90, 240),
        "park": (120, 60, 240),
        "campus_major": (135, 120, 150),
        "campus_visit": (90, 75, 120),
        "night_view_tower": (90, 75, 120),
        "night_view": (75, 60, 90),
        "campus_cafeteria": (45, 35, 50),
        "quick_meal": (55, 45, 60),
        "meal": (75, 60, 90),
        "rest": (45, 30, 90),
        "coffee": (45, 30, 90),
        "shopping": (90, 45, 180),
        "area_walk": (120, 60, 210),
    }

    def normalize_duration(
        self,
        raw_duration: Any,
        kind: str = "visit",
        category: str = "",
        intent_type: str = "",
        context: dict[str, Any] | None = None,
    ) -> DurationEstimate:
        preferred, minimum, maximum, factors = self._rule(kind, category, intent_type)
        metadata = self._metadata(context)
        locked = bool(metadata.get("userLocked"))
        duration = self._to_int(raw_duration)
        if locked and duration is not None:
            return DurationEstimate(duration, duration, duration, "user_locked", 1.0, ("user_locked",), True)
        pace_multiplier = self._pace_multiplier(context or {})
        if duration is None:
            preferred = max(minimum, min(maximum, self._round_to_five(preferred * pace_multiplier)))
            source = "intent_and_poi_policy" if any(item not in {"visit", "landmark", "scenic"} for item in factors) else "policy_default"
            confidence = 0.76 if source == "intent_and_poi_policy" else 0.62
        else:
            preferred = self._round_to_five(max(minimum, min(maximum, duration)))
            source = "model_clamped" if preferred != duration else "model_accepted"
            confidence = 0.68 if preferred != duration else 0.78
        if self._relaxed(context or {}) and kind not in {"meal", "rest", "note", "transport"}:
            maximum += 15
        return DurationEstimate(preferred, minimum, maximum, source, confidence, tuple(factors), False)

    def normalize_segment_dict(self, segment: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        decision = self.normalize_duration(
            segment.get("durationMinutes"),
            kind=str(segment.get("kind") or "visit"),
            category=str(poi.get("category") or segment.get("category") or ""),
            intent_type=str(poi.get("intentType") or segment.get("intentType") or ""),
            context=context or {},
        )
        segment["durationMinutes"] = decision.preferred_minutes
        estimate_metadata = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
        segment["estimateMetadata"] = {**estimate_metadata, **decision.to_metadata()}
        if segment.get("startTime"):
            segment["endTime"] = self.end_time(str(segment["startTime"]), decision.preferred_minutes)
        note = self._strip_duration_marker(str(segment.get("notes") or "").strip())
        marker = f"durationSource={decision.source}; durationConfidence={decision.confidence:.2f}"
        segment["notes"] = f"{note}；{marker}" if note and marker not in note else note or marker

    def end_time(self, start_time: str, duration_minutes: int) -> str:
        try:
            start = datetime.strptime(start_time[:5], "%H:%M")
        except ValueError:
            return str(start_time)
        return (start + timedelta(minutes=duration_minutes)).strftime("%H:%M")

    def _rule(self, kind: str, category: str, intent_type: str) -> tuple[int, int, int, list[str]]:
        kind = str(kind or "visit").lower()
        category = str(category or "").lower()
        intent = str(intent_type or "").lower()
        text = " ".join([kind, category, intent])
        if intent in {"campus_visit", "campus"}:
            major = any(
                token in text
                for token in (
                    "qualification_verified",
                    "qualified_campus",
                    "authoritative_qualification",
                    "重点高校",
                    "major_campus",
                )
            )
            rule = self.RULES["campus_major" if major else "campus_visit"]
            return (*rule, ["campus_visit", "major_campus" if major else "standard_campus"])
        if intent in {"night_view", "night_walk", "view_tower"}:
            tower = any(token in text for token in ("塔", "tower", "电视塔", "观景台", "observation"))
            rule = self.RULES["night_view_tower" if tower else "night_view"]
            return (*rule, ["night_view", "tower" if tower else "outdoor"])
        if kind == "meal" or intent in {"meal", "food"}:
            if any(token in text for token in ("食堂", "cafeteria", "校园餐厅")):
                return (*self.RULES["campus_cafeteria"], ["meal", "campus_cafeteria"])
            if any(token in text for token in ("小吃", "面食", "快餐", "简餐", "snack", "noodle")):
                return (*self.RULES["quick_meal"], ["meal", "quick_meal"])
            return (*self.RULES["meal"], ["meal", "seated_meal"])
        if any(token in text for token in ("博物馆", "museum")):
            return (*self.RULES["museum"], ["museum"])
        if any(token in text for token in ("公园", "park")):
            return (*self.RULES["park"], ["park"])
        if kind in self.RULES:
            return (*self.RULES[kind], [kind])
        return (*self.RULES["landmark"], ["landmark"])

    def _metadata(self, context: dict[str, Any] | None) -> dict[str, Any]:
        context = context or {}
        estimate = context.get("estimateMetadata") if isinstance(context.get("estimateMetadata"), dict) else {}
        duration = estimate.get("duration") if isinstance(estimate.get("duration"), dict) else {}
        return duration

    def _pace_multiplier(self, context: dict[str, Any]) -> float:
        structured_pace = str(context.get("structuredPace") or "").strip()
        if structured_pace == "intensive":
            return 0.8
        if structured_pace == "relaxed":
            return 7 / 6
        text = self._explicit_pace_text(context)
        if any(token in text for token in ("紧凑", "特种兵", "compact")):
            return 0.8
        if any(token in text for token in ("深度", "摄影", "deep", "photo")):
            return 1.15
        if self._relaxed(context):
            return 7 / 6
        return 1.0

    def _relaxed(self, context: dict[str, Any]) -> bool:
        if str(context.get("structuredPace") or "").strip() == "relaxed":
            return True
        text = self._explicit_pace_text(context)
        rules = context.get("memoryRules") if isinstance(context.get("memoryRules"), dict) else {}
        pace = rules.get("pace") if isinstance(rules.get("pace"), dict) else {}
        return bool(pace.get("relaxed")) or any(token in text for token in ("轻松", "不赶", "亲子", "老人", "儿童"))

    def _explicit_pace_text(self, context: dict[str, Any]) -> str:
        requirements = context.get("understoodRequirements") if isinstance(context.get("understoodRequirements"), dict) else {}
        fields = requirements.get("fields") if isinstance(requirements.get("fields"), dict) else {}
        rules = context.get("memoryRules") if isinstance(context.get("memoryRules"), dict) else {}
        pace = rules.get("pace") if isinstance(rules.get("pace"), dict) else {}
        return " ".join(
            str(value or "")
            for value in (
                fields.get("pace"), pace.get("label"), context.get("currentPreferenceSummary"),
                context.get("effectiveUserMessage"), context.get("latestUserMessage"),
            )
        )

    @staticmethod
    def _round_to_five(value: float | int) -> int:
        return int(5 * round(float(value) / 5))

    def _to_int(self, value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _strip_duration_marker(self, note: str) -> str:
        cleaned = re.sub(r"(?:[；;]\s*)?durationSource=[^；;]+[；;]\s*durationConfidence=[0-9.]+", "", note)
        return cleaned.strip("；; ")

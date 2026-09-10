"""Deterministic, identity-free Experience Intent inference.

This extends the existing understoodRequirements producer. It does not resolve
places and never invents city-specific dishes or landmarks.
"""

from __future__ import annotations

import re
from typing import Any

from src.services.creative_planning_models import TripExperienceIntent


class ExperienceIntentService:
    _SIGNALS = (
        (re.compile(r"历史|文化|遗产|博物馆"), "heritage_context", "culture_deep_dive"),
        (re.compile(r"本地人|本地生活|日常生活|社区|市井"), "resident_daily_life", "local_immersion"),
        (re.compile(r"地方饮食|当地特色|地方菜|美食"), "provider_grounded_local_food", "food_led"),
        (re.compile(r"安静.*散步|散步|慢走"), "quiet_walk", "comfort"),
        (re.compile(r"夜景|夜游"), "night_view", "photo_night"),
        (re.compile(r"轻松|不能连续走|老人|父母|长辈"), "low_exertion", "comfort"),
    )

    def infer(self, text: str) -> dict[str, Any]:
        desired: list[str] = []
        axes: list[str] = []
        for pattern, signal, axis in self._SIGNALS:
            if pattern.search(text or ""):
                desired.append(signal)
                axes.append(axis)
        avoid: list[str] = []
        if re.search(r"不要.*打卡|不希望.*打卡", text or ""):
            avoid.append("checklist_only")
        if re.search(r"不喜欢.*商业|不要.*商业", text or ""):
            avoid.append("high_commercialization")
        desired = list(dict.fromkeys(desired))
        axes = list(dict.fromkeys(axes)) or ["classic"]
        shapes = ["single_poi"]
        if any(item in desired for item in ("resident_daily_life", "heritage_context")):
            shapes.extend(["area", "micro_route"])
        if "quiet_walk" in desired:
            shapes.append("open_walk")
        high_ambiguity = bool(
            len(axes) >= 2 and re.search(r"(还没想好|没有想好|哪一种作为主线|不确定.*主线)", text or "")
        )
        contract = TripExperienceIntent(
            schemaVersion="experience-intent-v1",
            tripThesis="在真实证据与路线约束内组织用户表达的体验主线",
            desiredSignals=desired,
            avoidSignals=avoid,
            decisionAxes=axes,
            allowedExperienceShapes=list(dict.fromkeys(shapes)),
        )
        return {
            "contract": contract.model_dump(by_alias=True),
            "highImpactAmbiguityDetected": high_ambiguity,
            "clarificationDecisionSource": "explicit_user_ambiguity"
            if high_ambiguity
            else "deterministic_context_inference",
            # This service only detects a machine-readable ambiguity. The
            # Controller owns every user-visible question and semantic option.
            "clarificationDimensionId": ("experience_intent.primary_axis" if high_ambiguity else ""),
            "clarificationQuestion": "",
            "clarificationOptions": [],
        }

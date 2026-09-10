"""Compile only server-authoritative request facts into a portfolio ledger."""

from __future__ import annotations

import copy
import re
from typing import Any

from src.services.creative_planning_models import ConstraintLedger, GoalRequirement, canonical_fingerprint


class ConstraintLedgerCompiler:
    def compile(self, context: dict[str, Any], directive: dict[str, Any] | None = None) -> ConstraintLedger:
        contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        dates = context.get("resolvedTripDates") if isinstance(context.get("resolvedTripDates"), dict) else {}
        required_items = [item for item in contract.get("requiredIntents") or [] if isinstance(item, dict)]
        experience_specs = {
            str(item.get("intentType") or ""): item
            for item in contract.get("experienceSpecs") or []
            if isinstance(item, dict) and str(item.get("intentType") or "")
        }
        # The request contract can carry soft experiences in requiredIntents
        # for coverage reporting. They are not hard itinerary constraints and
        # must not consume the mandatory candidate budget.
        required = [
            self._goal(item, experience_specs.get(str(item.get("intentType") or "")))
            for item in required_items
            if str(item.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
        ]
        soft_required = [
            item
            for item in required_items
            if str(item.get("requirementLevel") or "required") in {"soft_experience", "optional"}
        ]
        optional = [
            self._goal(item, experience_specs.get(str(item.get("intentType") or "")))
            for item in [*(contract.get("optionalIntents") or []), *soft_required]
            if isinstance(item, dict)
        ]
        required = [item for item in required if item is not None]
        optional = [
            item for item in optional if item is not None and item.goal_id not in {goal.goal_id for goal in required}
        ]
        preferences = context.get("preferenceMemory") if isinstance(context.get("preferenceMemory"), dict) else {}
        raw = {
            "contract": contract,
            "dates": dates,
            "city": context.get("city"),
            "directive": directive or {},
            "preferences": preferences,
        }
        understood = (
            context.get("understoodRequirements") if isinstance(context.get("understoodRequirements"), dict) else {}
        )
        experience_intent = (
            understood.get("experienceIntent") if isinstance(understood.get("experienceIntent"), dict) else {}
        )
        raw["experienceIntent"] = experience_intent
        understood_fields = understood.get("fields") if isinstance(understood.get("fields"), dict) else {}
        transport_values = list(contract.get("transportPreferences") or [])
        for key in ("transportPreference", "transport"):
            if understood_fields.get(key):
                transport_values.append(understood_fields[key])
        # The validated request text remains the compatibility source for
        # older controller contracts that did not yet expose transportPreferences.
        if not transport_values and context.get("latestUserMessage"):
            transport_values.extend(self._transport_preferences_from_text(context["latestUserMessage"]))
        return ConstraintLedger(
            schemaVersion="constraint-ledger-v1",
            city=str(context.get("city") or context.get("selectedCity") or contract.get("city") or "目的地"),
            startDate=dates.get("startDate"),
            endDate=dates.get("endDate"),
            dayCount=max(1, int(dates.get("dayCount") or contract.get("dayCount") or 1)),
            hardGoals=required,
            softGoals=optional,
            transportPreferences=self._transport_preferences(transport_values),
            budgetTier=str(contract.get("budgetTier") or "standard"),
            pace=str(contract.get("pace") or "standard"),
            partySize=contract.get("partySize"),
            lockedEntities=[str(item) for item in contract.get("lockedEntities") or []],
            forbiddenExperienceTypes=[str(item) for item in contract.get("negativeConstraints") or []],
            preferenceSnapshot=preferences,
            experienceIntent=experience_intent,
            sourceFingerprint=canonical_fingerprint(raw),
        )

    @staticmethod
    def _goal(
        value: dict[str, Any],
        experience_spec: dict[str, Any] | None = None,
    ) -> GoalRequirement | None:
        intent = str(value.get("intentType") or "").strip()
        if not intent:
            return None
        experience_spec = experience_spec or {}

        def policy(field: str) -> Any:
            return copy.deepcopy(
                value[field] if field in value and value.get(field) is not None else experience_spec.get(field)
            )

        return GoalRequirement(
            goalId=str(value.get("goalId") or f"goal_{intent}"),
            intentType=intent,
            requiredMin=max(
                0, int(value.get("requiredMin") if value.get("requiredMin") is not None else value.get("target") or 1)
            ),
            preferredCount=max(
                0,
                int(
                    value.get("preferredCount")
                    if value.get("preferredCount") is not None
                    else value.get("requiredMin")
                    if value.get("requiredMin") is not None
                    else value.get("target") or 1
                ),
            ),
            maxCount=value.get("maxCount"),
            cardinalitySource=str(value.get("cardinalitySource") or value.get("source") or "explicit_user_request"),
            distributionPolicy=str(value.get("distributionPolicy") or "spread_across_distinct_days"),
            allowedDayNumbers=[
                int(day)
                for day in (
                    value.get("allowedDayNumbers")
                    or experience_spec.get("allowedDayNumbers")
                    or []
                )
            ],
            explicitlyNamed=bool(value.get("explicitlyNamed")),
            exactEntity=str(value.get("exactEntity") or "").strip() or None,
            userExplicit=bool(value.get("userExplicit") or value.get("cardinalitySource") == "explicit_every_day"),
            priorityTier=str(
                value.get("priorityTier")
                or (
                    "explicit_soft"
                    if (
                        value.get("cardinalitySource") == "explicit_every_day"
                        or value.get("distributionPolicy") == "every_allowed_day"
                    )
                    and str(value.get("requirementLevel") or "") in {"soft_experience", "optional"}
                    else "hard"
                    if str(value.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
                    else "optional"
                )
            ),
            accessPolicy=policy("accessPolicy"),
            distinctnessPolicy=policy("distinctnessPolicy"),
            timeWindow=policy("timeWindow"),
            schedulePreference=policy("schedulePreference") or {},
            detourTolerance=policy("detourTolerance"),
            evidenceFreshness=policy("evidenceFreshness"),
            confidence=policy("confidence"),
            experienceFamilies=[
                str(item)
                for item in experience_spec.get("experienceFamilies") or []
                if str(item).strip()
            ],
            unresolvedDimensions=[
                str(item)
                for item in experience_spec.get("unresolvedDimensions") or []
                if str(item).strip()
            ],
        )

    @staticmethod
    def _transport_preferences_from_text(value: Any) -> list[str]:
        """Extract only explicit, non-negated preferences from compatibility text."""

        text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if not text:
            return []
        families = (
            ("public_transit", ("公共交通", "公交", "地铁", "transit", "metro", "subway")),
            ("walking", ("步行", "walking", "walk")),
            ("bicycling", ("骑行", "bicycling", "cycling", "bike")),
            ("driving", ("驾车", "打车", "driving", "taxi")),
        )
        positive_text = text
        for _canonical, markers in families:
            for marker in markers:
                escaped = re.escape(marker)
                positive_text = re.sub(
                    rf"(?:不要|不想|不愿|避免|拒绝|不考虑|禁止)(?:再)?(?:乘坐|搭乘|乘|坐|使用|用)?{escaped}",
                    "",
                    positive_text,
                )
                positive_text = re.sub(
                    rf"不(?:再)?(?:乘坐|搭乘|乘|坐|使用|用){escaped}",
                    "",
                    positive_text,
                )
                positive_text = positive_text.replace(f"no_{marker}", "").replace(f"without_{marker}", "")

        prioritized = [
            canonical
            for canonical, markers in families
            if any(f"{marker}优先" in positive_text or f"{marker}为主" in positive_text for marker in markers)
        ]
        if prioritized:
            return prioritized
        return [canonical for canonical, markers in families if any(marker in positive_text for marker in markers)]

    @staticmethod
    def _transport_preferences(values: list[Any]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            text = str(value or "").strip()
            folded = text.casefold().replace("-", "_").replace(" ", "_")
            if any(marker in folded for marker in ("公共交通", "公交", "地铁", "transit", "metro", "subway")):
                canonical = "public_transit"
            elif any(marker in folded for marker in ("步行", "walking", "walk")):
                canonical = "walking"
            elif any(marker in folded for marker in ("骑行", "bicycling", "cycling", "bike")):
                canonical = "bicycling"
            elif any(marker in folded for marker in ("驾车", "打车", "driving", "taxi")):
                canonical = "driving"
            else:
                canonical = folded or text
            if canonical and canonical not in normalized:
                normalized.append(canonical)
        return normalized

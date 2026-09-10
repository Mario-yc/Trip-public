"""City-neutral meal-slot assignment before provider grounding.

This policy schedules meal needs. It does not invent a city's representative
dishes or brands. Generic local-food requests remain pending until provider
detail or independent claims support a concrete candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class MealExperienceAssignment:
    slot_id: str
    meal_label: str
    dish_family: str = ""
    search_terms: tuple[str, ...] = ()
    rest_required: bool = False
    route_context: dict[str, Any] = field(default_factory=dict)
    dietary_restrictions: tuple[str, ...] = ()
    allow_pending: bool = True
    user_explicit: bool = False
    theme_id: str = ""
    theme_label: str = ""
    experience_mode: str = ""
    selection_intent: str = ""
    source_fingerprint: str = ""

    def to_camel_dict(self) -> dict[str, Any]:
        return {
            "slotId": self.slot_id,
            "mealLabel": self.meal_label,
            "dishFamily": self.dish_family,
            "searchTerms": list(self.search_terms),
            "restRequired": self.rest_required,
            "routeContext": dict(self.route_context),
            "dietaryRestrictions": list(self.dietary_restrictions),
            "allowPending": self.allow_pending,
            "userExplicit": self.user_explicit,
            "themeId": self.theme_id,
            "themeLabel": self.theme_label,
            "experienceMode": self.experience_mode,
            "selectionIntent": self.selection_intent,
            "sourceFingerprint": self.source_fingerprint,
        }


class MealExperienceAssignmentPolicy:
    @classmethod
    def family_for_candidate(cls, candidate: Any, *, preferred_family: str = "") -> str:
        claims = (
            candidate.get("sourceClaims", candidate.get("source_claims", []))
            if isinstance(candidate, Mapping)
            else getattr(candidate, "source_claims", getattr(candidate, "sourceClaims", []))
        )
        for claim in claims if isinstance(claims, list) else []:
            if not isinstance(claim, Mapping) or str(claim.get("stance") or "support") != "support":
                continue
            claim_key = str(claim.get("claimKey") or claim.get("claim_key") or "").strip().casefold()
            value = ""
            if claim_key.startswith("meal_family:"):
                value = claim_key.partition(":")[2]
            elif claim_key in {"meal_family", "dish_family", "cuisine_family"}:
                value = str(
                    claim.get("value")
                    or claim.get("claimValue")
                    or claim.get("claim_value")
                    or claim.get("family")
                    or ""
                )
            normalized = cls._normalize_family(value)
            if normalized:
                return normalized
        text = " ".join(
            str(candidate.get(key) or "")
            for key in ("name", "type", "category", "address", "district")
        ) if isinstance(candidate, Mapping) else " ".join(
            str(getattr(candidate, key, "") or "")
            for key in ("name", "type", "category", "address", "district")
        )
        return cls.family_for_text(text, preferred_family=preferred_family)

    @staticmethod
    def _normalize_family(value: str) -> str:
        return re.sub(r"[^\w-]+", "_", str(value or "").strip().casefold()).strip("_")[:64]

    @staticmethod
    def family_for_text(text: str, *, preferred_family: str = "") -> str:
        """Match only a caller-supplied explicit constraint; never infer a city dish."""
        preferred = str(preferred_family or "").strip()
        if preferred and re.sub(r"\s+", "", preferred) in re.sub(r"\s+", "", str(text or "")):
            return preferred
        return ""

    def assign(
        self,
        city: str,
        slots: list[Any],
        *,
        request_text: str,
        seed: str,
        user_explicit_food_constraints: Optional[Mapping[str, Any]] = None,
        route_context: Optional[dict[str, Any]] = None,
        dietary_restrictions: Optional[list[str]] = None,
    ) -> dict[str, MealExperienceAssignment]:
        del city, seed
        explicit = dict(user_explicit_food_constraints or {})
        restrictions = tuple(str(item).strip() for item in dietary_restrictions or [] if str(item).strip())
        rest_required = bool(re.search(r"(需要休息|休息一下|不能连续走|老人|长辈)", request_text or ""))
        assignments: dict[str, MealExperienceAssignment] = {}
        for slot in sorted(
            slots,
            key=lambda item: (int(getattr(item, "day_number", 0)), str(getattr(item, "start_time", ""))),
        ):
            slot_id = str(getattr(slot, "slot_id", "") or "")
            raw_need = str(getattr(slot, "raw_need", "") or "")
            meal_label = "dinner" if re.search(r"(晚餐|晚饭|dinner)", raw_need, re.IGNORECASE) else "lunch"
            raw_constraint = explicit.get(slot_id)
            if isinstance(raw_constraint, Mapping):
                search_terms = tuple(
                    str(item).strip()
                    for item in raw_constraint.get("searchTerms") or []
                    if str(item).strip()
                )[:3]
                constraint = str(
                    raw_constraint.get("groundedFamilyKey")
                    or raw_constraint.get("themeId")
                    or (search_terms[0] if search_terms else "")
                ).strip()
                theme_id = str(raw_constraint.get("themeId") or constraint).strip()
                theme_label = str(raw_constraint.get("themeLabel") or (search_terms[0] if search_terms else "")).strip()
                experience_mode = str(raw_constraint.get("experienceMode") or "").strip()
                selection_intent = str(raw_constraint.get("selectionIntent") or "").strip()
                source_fingerprint = str(raw_constraint.get("sourceFingerprint") or "").strip()
                user_explicit = raw_constraint.get("userExplicit") is True
            else:
                constraint = str(raw_constraint or "").strip()
                search_terms = (constraint,) if constraint else ()
                theme_id = constraint
                theme_label = constraint
                experience_mode = ""
                selection_intent = ""
                source_fingerprint = ""
                user_explicit = bool(constraint)
            assignments[slot_id] = MealExperienceAssignment(
                slot_id=slot_id,
                meal_label=meal_label,
                dish_family=constraint,
                search_terms=search_terms,
                rest_required=rest_required,
                route_context=dict(route_context or {}),
                dietary_restrictions=restrictions,
                allow_pending=not bool(constraint),
                user_explicit=user_explicit,
                theme_id=theme_id,
                theme_label=theme_label,
                experience_mode=experience_mode,
                selection_intent=selection_intent,
                source_fingerprint=source_fingerprint,
            )
        return assignments

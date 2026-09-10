from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class PoiDominanceDecision:
    action: str
    selected_candidate_id: Optional[str]
    safe_candidates: list[dict[str, Any]]
    rejected_candidates: list[dict[str, Any]]
    evidence: dict[str, Any]


class PoiCandidateDominanceService:
    """Builds a safe candidate set, then decides dominance or material choice."""

    DOMINANCE_MARGIN = 8.0
    MUSEUM_CATEGORIES = {"museum", "art_museum", "gallery"}
    MEAL_CATEGORIES = {"restaurant", "local_restaurant", "food"}
    CAMPUS_CATEGORIES = {"campus", "university", "education", "school"}

    def decide(
        self,
        *,
        intent: dict[str, Any],
        candidates: list[dict[str, Any]],
        route_context: dict[str, Any],
    ) -> PoiDominanceDecision:
        safe: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for candidate in candidates:
            reasons = self._hard_rejections(intent, candidate, route_context)
            if reasons:
                rejected.append({"candidateId": candidate.get("candidateId"), "reasonCodes": reasons})
                continue
            scored = dict(candidate)
            scored["dominanceScore"] = round(self._score(intent, candidate, route_context), 3)
            safe.append(scored)
        safe.sort(key=lambda item: (-float(item["dominanceScore"]), str(item.get("candidateId") or "")))
        if not safe:
            return PoiDominanceDecision(
                "unresolved", None, [], rejected, {"reason": "no_safe_candidate", "dominant": False}
            )
        if len(safe) == 1:
            return PoiDominanceDecision(
                "auto_select",
                str(safe[0].get("candidateId")),
                safe,
                rejected,
                {"dominant": True, "margin": None, "materialTradeoff": False},
            )
        margin = float(safe[0]["dominanceScore"]) - float(safe[1]["dominanceScore"])
        material_tradeoff = self._material_tradeoff(intent, safe[0], safe[1], margin)
        if margin >= self.DOMINANCE_MARGIN and not material_tradeoff:
            return PoiDominanceDecision(
                "auto_select",
                str(safe[0].get("candidateId")),
                safe,
                rejected,
                {"dominant": True, "margin": round(margin, 3), "materialTradeoff": False},
            )
        return PoiDominanceDecision(
            "ask_user",
            None,
            safe,
            rejected,
            {"dominant": False, "margin": round(margin, 3), "materialTradeoff": True},
        )

    def _hard_rejections(
        self, intent: dict[str, Any], candidate: dict[str, Any], route_context: dict[str, Any]
    ) -> list[str]:
        reasons: list[str] = []
        if candidate.get("provider") != "amap" or not candidate.get("amapPoiId"):
            reasons.append("provider_identity_invalid")
        if self._normalize_city(candidate.get("city")) != self._normalize_city(intent.get("city")):
            reasons.append("wrong_city")
        if candidate.get("longitude") is None or candidate.get("latitude") is None:
            reasons.append("coordinates_missing")
        intent_type = intent.get("intentType")
        category = candidate.get("category")
        allowed = (
            self.MUSEUM_CATEGORIES
            if intent_type == "museum"
            else self.MEAL_CATEGORIES
            if intent_type == "meal"
            else self.CAMPUS_CATEGORIES
            if intent_type == "campus_visit"
            else None
        )
        if allowed is not None and category not in allowed:
            reasons.append("category_mismatch")
        if float(candidate.get("semanticScore") or 0) < 0.65:
            reasons.append("semantic_mismatch")
        # AMap nearby distance / straight-line distance can rank the discovery
        # frontier, but it is not a Provider insertion matrix and therefore
        # cannot remove a candidate before the guarded writer verifies it.
        if candidate.get("entityKey") in set(route_context.get("recentEntityKeys") or []):
            reasons.append("recent_entity_repeat")
        return reasons

    @staticmethod
    def _normalize_city(value: Any) -> str:
        text = str(value or "").strip().casefold()
        return text.removesuffix("市")

    @staticmethod
    def _score(intent: dict[str, Any], candidate: dict[str, Any], route_context: dict[str, Any]) -> float:
        semantic = float(candidate.get("semanticScore") or 0) * 70.0
        max_detour = max(1.0, float(route_context.get("maxDetourMeters") or 3000))
        distance = float(candidate.get("distanceMeters") or max_detour)
        route_score = max(0.0, 20.0 * (1.0 - distance / max_detour))
        budget_score = 5.0 if not intent.get("budget") or candidate.get("priceLevel") == intent.get("budget") else 0.0
        provider_score = 5.0 if candidate.get("provider") == "amap" else 0.0
        return semantic + route_score + budget_score + provider_score

    @staticmethod
    def _material_tradeoff(
        intent: dict[str, Any], first: dict[str, Any], second: dict[str, Any], margin: float
    ) -> bool:
        if margin >= PoiCandidateDominanceService.DOMINANCE_MARGIN:
            return False
        if intent.get("intentType") == "meal" and first.get("cuisine") != second.get("cuisine"):
            return True
        return True

"""Translate provider-neutral POI search profiles into bounded AMap plans."""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Literal, Mapping, Union
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from src.models.poi_search_profile import (
    PoiSearchProfile,
    SearchQueryMode,
    SearchQueryPlan,
)


AmapSearchEndpoint = Literal["place/text", "place/around"]


class AmapPoiSearchPlan(BaseModel):
    """One concrete AMap request shape without executing the provider call."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schemaVersion: Literal["amap-poi-search-plan-v1"] = (
        "amap-poi-search-plan-v1"
    )
    planId: str = Field(min_length=1)
    sourcePlanId: str = Field(min_length=1)
    priority: int = Field(ge=0, le=1000)
    endpoint: AmapSearchEndpoint
    mode: SearchQueryMode
    city: str = Field(min_length=1)
    keyword: str = Field(min_length=1)
    category: str = Field(min_length=1)
    providerCategoryKey: str = Field(min_length=1)
    anchorPolicy: str = "none"
    radiusMeters: int = Field(default=1500, ge=50, le=5000)
    resultLimit: int = Field(default=12, ge=1, le=25)
    fallbackLevel: int = Field(default=0, ge=0, le=3)
    requiresAmapGrounding: bool = True
    stopWhenTargetReached: bool = True


_PROVIDER_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "all": ("all",),
    "scenic": ("scenic",),
    "food": ("food",),
    "experience": ("experience",),
    "shopping": ("shopping",),
    "campus": ("campus",),
    "museum": ("museum",),
    "lodging": ("lodging",),
    "transport": ("transport",),
    "heritage_district": ("culture", "scenic"),
    "pedestrian_street": ("shopping", "culture"),
    "neighborhood": ("local_service",),
    "community_market": ("market", "shopping", "food"),
    "market": ("market", "shopping", "food"),
    "art_district": ("culture", "experience"),
    "cultural_venue": ("culture", "museum", "experience"),
    "park": ("park",),
    "green_space": ("park", "scenic"),
}

class AmapPoiSearchPlanAdapter:
    """Pure, deterministic adapter from semantic profiles to AMap calls."""

    def adapt(
        self,
        profile: Union[PoiSearchProfile, Mapping[str, Any]],
    ) -> list[AmapPoiSearchPlan]:
        normalized_profile = self._profile(profile)
        max_provider_plans = min(
            normalized_profile.budgetPolicy.maxQueries,
            normalized_profile.budgetPolicy.maxAmapCalls,
        )
        source_plans = sorted(
            enumerate(normalized_profile.queryPlans),
            key=lambda item: (-item[1].priority, item[0]),
        )
        candidate_queues = [
            self._expand_source_plan(normalized_profile, source_plan)
            for _index, source_plan in source_plans
        ]

        selected: list[tuple[int, int, AmapPoiSearchPlan]] = []
        seen: set[tuple[str, str, str, str]] = set()
        selected_categories: set[str] = set()
        candidate_index = 0

        def select_candidate(
            source_index: int,
            *,
            prefer_new_category: bool,
        ) -> bool:
            nonlocal candidate_index
            queue = candidate_queues[source_index]
            for queue_index, candidate in enumerate(queue):
                identity = self._candidate_identity(candidate)
                if identity in seen:
                    continue
                if (
                    prefer_new_category
                    and candidate.category in selected_categories
                ):
                    continue
                queue.pop(queue_index)
                seen.add(identity)
                selected_categories.add(candidate.category)
                selected.append((source_index, candidate_index, candidate))
                candidate_index += 1
                return True
            return False

        # Preserve every source mode once when budget permits, but select a
        # different category for each baseline plan whenever possible.
        for source_index in range(len(candidate_queues)):
            if len(selected) >= max_provider_plans:
                break
            if not select_candidate(
                source_index,
                prefer_new_category=True,
            ):
                select_candidate(
                    source_index,
                    prefer_new_category=False,
                )

        # Spend the remaining budget on category breadth before keyword or
        # endpoint duplicates. This keeps market/art profiles semantically
        # broad without exceeding the existing AMap call budget.
        while len(selected) < max_provider_plans:
            made_progress = False
            for prefer_new_category in (True, False):
                for source_index in range(len(candidate_queues)):
                    if select_candidate(
                        source_index,
                        prefer_new_category=prefer_new_category,
                    ):
                        made_progress = True
                        break
                if made_progress:
                    break
            if not made_progress:
                break

        selected.sort(
            key=lambda item: (
                -item[2].priority,
                item[0],
                item[1],
            )
        )
        return [item[2] for item in selected]

    @classmethod
    def _candidate_identity(
        cls,
        candidate: AmapPoiSearchPlan,
    ) -> tuple[str, str, str, str]:
        return (
            candidate.endpoint,
            cls._normalize_text(candidate.keyword),
            candidate.category,
            candidate.anchorPolicy,
        )

    def _expand_source_plan(
        self,
        profile: PoiSearchProfile,
        source_plan: SearchQueryPlan,
    ) -> list[AmapPoiSearchPlan]:
        unknown_family = profile.experienceFamily == "unknown"
        category_pairs = self._category_pairs(profile, source_plan)
        keyword_variants = self._dedupe(
            [source_plan.keyword, *source_plan.keywordVariants]
        )
        if source_plan.mode == "exact_entity" or unknown_family:
            keyword_variants = keyword_variants[:1]

        endpoint = self._endpoint(source_plan.mode)
        radius_meters = max(50, min(source_plan.radiusMeters, 5000))
        result_limit = max(
            1,
            min(
                source_plan.resultLimit,
                profile.budgetPolicy.resultLimit,
                25,
            ),
        )
        result: list[AmapPoiSearchPlan] = []
        for keyword in keyword_variants:
            for provider_category_key, category in category_pairs:
                plan_identity = {
                    "schemaVersion": "amap-poi-search-plan-identity-v1",
                    "executionFingerprint": profile.executionFingerprint,
                    "sourcePlanId": source_plan.planId,
                    "endpoint": endpoint,
                    "keyword": self._normalize_text(keyword),
                    "category": category,
                    "anchorPolicy": source_plan.anchorPolicy,
                }
                result.append(
                    AmapPoiSearchPlan(
                        planId="amap_plan_" + self._fingerprint(plan_identity)[:24],
                        sourcePlanId=source_plan.planId,
                        priority=source_plan.priority,
                        endpoint=endpoint,
                        mode=source_plan.mode,
                        city=profile.city,
                        keyword=keyword,
                        category=category,
                        providerCategoryKey=provider_category_key,
                        anchorPolicy=source_plan.anchorPolicy,
                        radiusMeters=radius_meters,
                        resultLimit=result_limit,
                        fallbackLevel=source_plan.fallbackLevel,
                        requiresAmapGrounding=source_plan.requiresAmapGrounding,
                        stopWhenTargetReached=source_plan.stopWhenTargetReached,
                    )
                )
        return result

    def _category_pairs(
        self,
        profile: PoiSearchProfile,
        source_plan: SearchQueryPlan,
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen_categories: set[str] = set()
        for raw_key in source_plan.providerCategoryKeys:
            provider_key = self._normalize_key(raw_key)
            categories = _PROVIDER_CATEGORY_MAP.get(provider_key, ("all",))
            for category in categories:
                if category in seen_categories:
                    continue
                seen_categories.add(category)
                result.append((provider_key or "all", category))
        return result or [("all", "all")]

    @staticmethod
    def _profile(
        profile: Union[PoiSearchProfile, Mapping[str, Any]],
    ) -> PoiSearchProfile:
        if isinstance(profile, PoiSearchProfile):
            return profile
        return PoiSearchProfile.model_validate(profile)

    @staticmethod
    def _endpoint(mode: SearchQueryMode) -> AmapSearchEndpoint:
        if mode in {"amap_around", "route_corridor"}:
            return "place/around"
        return "place/text"

    @classmethod
    def _dedupe(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = cls._clean_text(value)
            key = cls._normalize_text(cleaned)
            if not cleaned or key in seen:
                continue
            seen.add(key)
            result.append(cleaned)
        return result

    @classmethod
    def _normalize_key(cls, value: Any) -> str:
        return cls._normalize_text(value).replace("-", "_").replace(" ", "_")

    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        return cls._clean_text(value).casefold()

    @staticmethod
    def _clean_text(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _fingerprint(payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(serialized.encode("utf-8")).hexdigest()

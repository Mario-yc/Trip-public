"""Pure Experience Semantic -> :class:`PoiSearchProfile` compilation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import unicodedata
from typing import Any, Iterable, Mapping, Optional

from src.models.poi_search_profile import (
    ExperienceSemanticInput,
    PoiSearchProfile,
    SearchBudgetPolicy,
    SearchCoveragePolicy,
    SearchFallbackPolicy,
    SearchQueryPlan,
    SearchScoringPolicy,
    SearchSourceEvidence,
)
from src.services.night_view_entity_search_policy import (
    NightViewEntitySearchPolicy,
)


@dataclass(frozen=True)
class _ExperienceFamilySpec:
    experience_family: str
    activity_mode: str
    semantic_facets: tuple[str, ...]
    keyword_variants: tuple[str, ...]
    preferred_place_facets: tuple[str, ...]
    rejected_place_facets: tuple[str, ...]
    provider_category_keys: tuple[str, ...]


_COMMON_REJECTED_PLACE_FACETS = (
    "hotel",
    "lodging",
    "parking",
    "residential_building",
    "office",
    "entrance_exit",
)

_FAMILY_SPECS: dict[str, _ExperienceFamilySpec] = {
    "heritage_walk": _ExperienceFamilySpec(
        experience_family="heritage",
        activity_mode="walk",
        semantic_facets=("heritage", "historic_district", "walkable"),
        keyword_variants=(
            "历史文化街区",
            "胡同",
            "古街",
            "老城街区",
            "传统建筑群",
            "历史街区漫步",
        ),
        preferred_place_facets=(
            "历史文化街区",
            "文化街区",
            "胡同",
            "老街",
            "古街",
            "古城",
            "古镇",
        ),
        rejected_place_facets=_COMMON_REJECTED_PLACE_FACETS
        + ("zoo", "theme_park", "generic_park"),
        provider_category_keys=("heritage_district", "pedestrian_street"),
    ),
    "local_life": _ExperienceFamilySpec(
        experience_family="local_life",
        activity_mode="observe_walk",
        semantic_facets=("local_life", "neighborhood", "walkable"),
        keyword_variants=(
            "社区市场",
            "本地生活街区",
            "社区商业街",
            "老社区商业",
            "居民生活街区",
            "社区文化中心",
        ),
        preferred_place_facets=(
            "社区",
            "里弄",
            "生活街区",
            "菜市场",
            "农贸市场",
            "胡同",
        ),
        rejected_place_facets=_COMMON_REJECTED_PLACE_FACETS
        + ("theme_park", "resort"),
        provider_category_keys=("neighborhood", "community_market"),
    ),
    "market_walk": _ExperienceFamilySpec(
        experience_family="market",
        activity_mode="walk_eat",
        semantic_facets=("market", "traditional_market", "walkable"),
        keyword_variants=(
            "传统市集",
            "菜市场",
            "农贸市场",
            "食品市场",
            "老字号街区",
            "市井商业街",
        ),
        preferred_place_facets=(
            "传统市场",
            "菜市场",
            "农贸市场",
            "社区市场",
            "市井市场",
            "市集",
            "集市",
            "夜市",
        ),
        rejected_place_facets=_COMMON_REJECTED_PLACE_FACETS
        + ("generic_mall", "theme_park"),
        provider_category_keys=("market", "pedestrian_street"),
    ),
    "art_walk": _ExperienceFamilySpec(
        experience_family="art",
        activity_mode="walk_visit",
        semantic_facets=("art", "creative_district", "walkable"),
        keyword_variants=(
            "艺术街区",
            "文化创意园",
            "艺术园区",
            "画廊",
            "艺术中心",
            "设计园区",
        ),
        preferred_place_facets=(
            "艺术区",
            "艺术街区",
            "艺术园区",
            "创意园区",
            "文化园区",
            "艺术中心",
            "艺术空间",
        ),
        rejected_place_facets=_COMMON_REJECTED_PLACE_FACETS
        + ("generic_mall", "theme_park"),
        provider_category_keys=("art_district", "cultural_venue"),
    ),
    "park_relax": _ExperienceFamilySpec(
        experience_family="park",
        activity_mode="relax_walk",
        semantic_facets=("park", "green_space", "relax"),
        keyword_variants=(
            "城市公园",
            "滨水公园",
            "森林公园",
            "湿地公园",
            "城市绿道",
            "休闲绿地",
        ),
        preferred_place_facets=(
            "城市公园",
            "滨水公园",
            "公园",
            "绿地",
            "园林",
            "滨水步道",
        ),
        rejected_place_facets=_COMMON_REJECTED_PLACE_FACETS
        + ("theme_park", "zoo", "amusement_park"),
        provider_category_keys=("park", "green_space"),
    ),
}

_EXACT_ENTITY_CATEGORY_KEYS: dict[str, tuple[str, ...]] = {
    "museum": ("museum",),
    "campus_visit": ("campus",),
    "meal": ("food",),
    "shopping": ("shopping",),
    "park": ("park",),
    "rest": ("food",),
    "area_walk": ("experience",),
    "local_culture": ("cultural_venue",),
    "night_view": ("experience",),
    "landmark": ("experience",),
}

_CORE_INTENT_EXPERIENCE_FAMILIES: dict[str, str] = {
    key: key for key in _EXACT_ENTITY_CATEGORY_KEYS
}


CREATIVE_OPTIONAL_FAMILY_INTENTS: dict[str, frozenset[str]] = {
    "heritage_walk": frozenset({"area_walk", "experience"}),
    "local_life": frozenset({"area_walk", "experience"}),
    "market_walk": frozenset({"area_walk", "experience"}),
    "art_walk": frozenset({"area_walk", "experience"}),
    "park_relax": frozenset({"area_walk", "experience", "park"}),
}


def creative_optional_semantic_rejection_reason(
    optional_family: Any,
    intent_type: Any,
    *,
    exact_entity: bool = False,
) -> Optional[str]:
    if exact_entity:
        return None
    family = str(optional_family or "").strip().casefold()
    if not family:
        return None
    allowed_intents = CREATIVE_OPTIONAL_FAMILY_INTENTS.get(family)
    if allowed_intents is None:
        return "creative_optional_family_unregistered"
    intent = str(intent_type or "").strip().casefold()
    if intent not in allowed_intents:
        return "creative_optional_intent_unregistered"
    return None


def search_profile_provider_rejection_reason(
    profile: PoiSearchProfile | Mapping[str, Any] | None,
) -> Optional[str]:
    if profile is None:
        return None
    payload = (
        profile.model_dump(by_alias=True, exclude_none=True)
        if isinstance(profile, PoiSearchProfile)
        else dict(profile)
    )
    source_evidence = payload.get("sourceEvidence")
    if not isinstance(source_evidence, Mapping):
        return None
    family_source = str(source_evidence.get("familySource") or "")
    if family_source in {
        "experience_spec_family_missing",
        "experience_spec_family_ambiguous",
        "experience_spec_family_invalid",
    }:
        return family_source
    if family_source != "optional_experience_family":
        return None
    return creative_optional_semantic_rejection_reason(
        payload.get("originalExperienceFamily"),
        payload.get("intentType"),
        exact_entity=payload.get("entityBindingMode") == "exact_entity",
    )


class ExperienceSearchProfileCompiler:
    """Compile frozen server semantics without provider calls or side effects."""

    def compile(self, semantic: ExperienceSemanticInput) -> PoiSearchProfile:
        raw_family, raw_family_source = self._family_source(semantic)
        experience_spec_family, experience_spec_error = self._experience_spec_family(semantic)
        # A core ExperienceSpec is an admission overlay, not a Creative
        # optional-family selector.  Combining the two sources would let an
        # arbitrary optional family override an answered hard requirement, so
        # retain neither and fail closed instead.
        if experience_spec_family and raw_family:
            experience_spec_family = ""
            experience_spec_error = "invalid"
        if experience_spec_error:
            family_source = f"experience_spec_family_{experience_spec_error}"
        elif experience_spec_family:
            family_source = "experience_spec_family"
        else:
            family_source = raw_family_source
        family_spec = _FAMILY_SPECS.get(raw_family)
        core_experience_family = (
            _CORE_INTENT_EXPERIENCE_FAMILIES.get(semantic.intentType)
            if not raw_family
            else None
        )
        if experience_spec_error:
            family_spec = None
            core_experience_family = None
            experience_family = "unknown"
        elif experience_spec_family:
            experience_family = experience_spec_family
        else:
            experience_family = (
                family_spec.experience_family
                if family_spec is not None
                else core_experience_family or "unknown"
            )
        original_family = raw_family or None

        exact_entity = self._clean_text(semantic.exactEntity)
        if semantic.entityBindingMode == "exact_entity" and not exact_entity:
            raise ValueError("exactEntity is required for exact_entity binding")
        creative_rejection_reason = creative_optional_semantic_rejection_reason(
            semantic.optionalExperienceFamily,
            semantic.intentType,
            exact_entity=bool(exact_entity),
        )

        if family_spec is None:
            activity_mode = self._fallback_activity_mode(semantic.intentType)
            semantic_facets = (
                ["exact_entity"] if semantic.entityBindingMode == "exact_entity" else []
            )
            keyword_variants = (
                self._dedupe([*semantic.candidateHints, semantic.rawNeed])
                if core_experience_family and semantic.candidateHints
                else self._unknown_keyword_variants(semantic, exact_entity)
            )
            preferred_place_facets = self._dedupe(semantic.preferredTypes)
            rejected_place_facets = self._dedupe(semantic.rejectedTypes)
            provider_category_keys = (
                self._exact_category_keys(semantic.intentType)
                if core_experience_family
                else ["all"]
            )
        else:
            activity_mode = family_spec.activity_mode
            semantic_facets = list(family_spec.semantic_facets)
            keyword_variants = self._dedupe(
                [*family_spec.keyword_variants, semantic.rawNeed]
            )
            preferred_place_facets = self._dedupe(
                [*family_spec.preferred_place_facets, *semantic.preferredTypes]
            )
            rejected_place_facets = self._dedupe(
                [*family_spec.rejected_place_facets, *semantic.rejectedTypes]
            )
            provider_category_keys = list(family_spec.provider_category_keys)

        if exact_entity:
            keyword_variants = [exact_entity]
            semantic_facets = self._dedupe([*semantic_facets, "exact_entity"])
            provider_category_keys = self._exact_category_keys(semantic.intentType)

        excluded_ids = self._canonical_ids(semantic.excludedPhysicalPoiIds)
        evidence_target_count = max(
            semantic.targetCount,
            semantic.evidenceTargetCount or semantic.targetCount,
        )
        result_limit = self._bounded_int(
            semantic.routePreference.get("resultLimit"), default=12, minimum=1, maximum=25
        )
        radius_meters = self._bounded_int(
            semantic.routePreference.get("radiusMeters")
            or semantic.routePreference.get("searchRadiusMeters"),
            default=1500,
            minimum=50,
            maximum=5000,
        )

        semantic_payload = {
            "schemaVersion": "poi-search-profile-semantic-v1",
            "city": self._fingerprint_text(semantic.city),
            "experienceFamily": experience_family,
            "originalExperienceFamily": original_family,
            "experienceSpecFamily": experience_spec_family,
            "experienceSpecFamilyError": experience_spec_error,
            "experienceFamilySource": family_source,
            "activityMode": activity_mode,
            "intentType": self._fingerprint_text(semantic.intentType),
            "requirementLevel": semantic.requirementLevel,
            "entityBindingMode": semantic.entityBindingMode,
            "exactEntity": self._fingerprint_text(exact_entity),
            "rawNeed": self._fingerprint_text(semantic.rawNeed),
            "semanticContext": self._fingerprint_text(semantic.semanticContext),
            "semanticFacets": sorted(self._fingerprint_values(semantic_facets)),
            "keywordVariants": sorted(self._fingerprint_values(keyword_variants)),
            "preferredPlaceFacets": sorted(
                self._fingerprint_values(preferred_place_facets)
            ),
            "rejectedPlaceFacets": sorted(
                self._fingerprint_values(rejected_place_facets)
            ),
            "candidateHints": sorted(
                self._fingerprint_values(semantic.candidateHints)
            ),
            "hintPolicy": self._fingerprint_text(semantic.hintPolicy),
            "routePreference": self._canonical_json_value(semantic.routePreference),
            "targetCount": semantic.targetCount,
            "evidenceTargetCount": evidence_target_count,
            "maxQueries": semantic.maxQueries,
            "experienceShape": semantic.experienceShape,
            "experienceGoal": self._fingerprint_text(semantic.experienceGoal),
            "desiredSignals": sorted(self._fingerprint_values(semantic.desiredSignals)),
            "avoidSignals": sorted(self._fingerprint_values(semantic.avoidSignals)),
            "evidencePolicy": self._canonical_json_value(semantic.evidencePolicy),
            "groundingPolicy": self._canonical_json_value(semantic.groundingPolicy),
            "routeContext": self._canonical_json_value(semantic.routeContext),
            "assignedMealFamily": self._fingerprint_text(
                semantic.assignedMealFamily
            ),
            "experienceSpecPolicy": self._canonical_json_value(
                semantic.experienceSpecPolicy
            ),
            "dayNumber": semantic.dayNumber,
            "intentFingerprint": semantic.intentFingerprint,
        }
        profile_fingerprint = self._fingerprint(semantic_payload)
        profile_id = "profile_" + self._fingerprint(
            {
                "profileFingerprint": profile_fingerprint,
                "poolId": semantic.poolId,
                "briefId": semantic.briefId,
                "planningSlotId": semantic.planningSlotId,
            }
        )[:24]

        query_plans = self._query_plans(
            semantic=semantic,
            profile_fingerprint=profile_fingerprint,
            exact_entity=exact_entity,
            keyword_variants=keyword_variants,
            provider_category_keys=provider_category_keys,
            preferred_type_groups=preferred_place_facets,
            rejected_type_groups=rejected_place_facets,
            radius_meters=radius_meters,
            result_limit=result_limit,
            unknown_family=(
                family_spec is None and core_experience_family is None
            ),
            experience_family=experience_family,
            core_intent_profile=bool(core_experience_family),
        )
        exclusion_fingerprint = self._fingerprint(
            {
                "schemaVersion": "poi-search-exclusions-v1",
                "excludedPhysicalPoiIds": excluded_ids,
            }
        )
        execution_fingerprint = self._fingerprint(
            {
                "schemaVersion": "poi-search-execution-v1",
                "profileFingerprint": profile_fingerprint,
                "exclusionFingerprint": exclusion_fingerprint,
                "anchors": {
                    "previous": self._anchor_execution_identity(
                        semantic.previousAnchor
                    ),
                    "next": self._anchor_execution_identity(semantic.nextAnchor),
                },
            }
        )

        unknown_family = (
            not exact_entity
            and family_spec is None
            and core_experience_family is None
        )
        experience_spec_rejection_reason = (
            f"experience_spec_family_{experience_spec_error}"
            if experience_spec_error
            else ""
        )
        unsupported_creative_semantic = bool(
            creative_rejection_reason or experience_spec_rejection_reason
        )
        fallback_policy = SearchFallbackPolicy(
            status=(
                "explicit_fallback"
                if unsupported_creative_semantic
                else "bounded_fallback"
            ),
            reasonCode=(
                experience_spec_rejection_reason
                or (
                    "unknown_experience_family"
                    if unknown_family
                    else creative_rejection_reason
                )
                or (
                    "core_intent_bounded_ladder"
                    if core_experience_family
                    else "family_specific_bounded_ladder"
                )
            ),
            allowGenericScenic=False,
            allowSemanticBroadening=(
                not exact_entity and not unsupported_creative_semantic
            ),
            requiresUserVisibleDegradedState=unsupported_creative_semantic,
        )

        return PoiSearchProfile(
            profileId=profile_id,
            profileFingerprint=profile_fingerprint,
            city=self._clean_text(semantic.city),
            poolId=self._clean_text(semantic.poolId),
            briefId=self._clean_text(semantic.briefId),
            planningSlotId=self._clean_text(semantic.planningSlotId),
            dayNumber=semantic.dayNumber,
            experienceFamily=experience_family,
            originalExperienceFamily=original_family,
            activityMode=activity_mode,
            intentType=self._clean_text(semantic.intentType),
            requirementLevel=semantic.requirementLevel,
            entityBindingMode=semantic.entityBindingMode,
            exactEntity=exact_entity or None,
            semanticFacets=semantic_facets,
            keywordVariants=keyword_variants,
            preferredPlaceFacets=preferred_place_facets,
            rejectedPlaceFacets=rejected_place_facets,
            queryPlans=query_plans,
            scoringPolicy=SearchScoringPolicy(
                requiredSemanticFacets=semantic_facets,
                preferredFacetWeight=0.35,
                routeFitWeight=0.0 if exact_entity else 0.2,
                exactEntityRequired=bool(exact_entity),
                distinctPhysicalPoiRequired=True,
            ),
            fallbackPolicy=fallback_policy,
            coveragePolicy=SearchCoveragePolicy(
                targetCount=semantic.targetCount,
                evidenceTargetCount=evidence_target_count,
                distinctPhysicalPoiRequired=True,
                stopWhenTargetReached=True,
            ),
            budgetPolicy=SearchBudgetPolicy(
                maxQueries=semantic.maxQueries,
                maxAmapCalls=semantic.maxQueries,
                maxWebSeedQueries=(
                    1
                    if any(plan.mode == "web_seed_then_amap" for plan in query_plans)
                    else 0
                ),
                resultLimit=result_limit,
            ),
            excludedPhysicalPoiIds=excluded_ids,
            sourceEvidence=SearchSourceEvidence(
                rawNeed=self._clean_text(semantic.rawNeed),
                semanticContext=self._clean_text(semantic.semanticContext),
                familySource=family_source,
                goalId=self._clean_text(semantic.goalId) or None,
                softGoalId=self._clean_text(semantic.softGoalId) or None,
                hintPolicy=self._clean_text(semantic.hintPolicy) or "no_hint",
                fallbackApplied=unsupported_creative_semantic,
                candidateHintsUsedAsSemanticEvidence=False,
            ),
            exclusionFingerprint=exclusion_fingerprint,
            executionFingerprint=execution_fingerprint,
            experienceShape=semantic.experienceShape,
            experienceGoal=self._clean_text(semantic.experienceGoal),
            desiredSignals=list(semantic.desiredSignals),
            avoidSignals=list(semantic.avoidSignals),
            evidencePolicy=dict(semantic.evidencePolicy),
            groundingPolicy=dict(semantic.groundingPolicy),
            routeContext=dict(semantic.routeContext),
            assignedMealFamily=self._clean_text(semantic.assignedMealFamily) or None,
            experienceSpecPolicy=dict(semantic.experienceSpecPolicy),
            intentFingerprint=semantic.intentFingerprint,
        )

    def _query_plans(
        self,
        *,
        semantic: ExperienceSemanticInput,
        profile_fingerprint: str,
        exact_entity: str,
        keyword_variants: list[str],
        provider_category_keys: list[str],
        preferred_type_groups: list[str],
        rejected_type_groups: list[str],
        radius_meters: int,
        result_limit: int,
        unknown_family: bool,
        experience_family: str,
        core_intent_profile: bool,
    ) -> list[SearchQueryPlan]:
        if exact_entity:
            return [
                self._query_plan(
                    profile_fingerprint=profile_fingerprint,
                    priority=100,
                    mode="exact_entity",
                    keyword=exact_entity,
                    keyword_variants=[exact_entity],
                    provider_category_keys=provider_category_keys,
                    preferred_type_groups=preferred_type_groups,
                    rejected_type_groups=rejected_type_groups,
                    anchor_policy="none",
                    radius_meters=radius_meters,
                    result_limit=result_limit,
                    fallback_level=0,
                )
            ]

        if core_intent_profile and (
            semantic.candidateHints or semantic.intentType == "night_view"
        ):
            if semantic.intentType == "night_view":
                # Only the authoritative user need/goal may choose a night-view
                # search family.  Controller candidate hints and broad desired
                # signals are discovery suggestions; allowing an unselected
                # hint such as ``滨水夜景`` to switch the whole ladder leaks an
                # alternative clarification answer into the active contract.
                semantic_text = " ".join(
                    [
                        semantic.rawNeed,
                        semantic.experienceGoal or "",
                    ]
                )
                direct_keywords = NightViewEntitySearchPolicy.direct_query_keywords(
                    candidate_hints=semantic.candidateHints,
                    city=semantic.city,
                    semantic_text=semantic_text,
                    occurrence_index=max(0, semantic.dayNumber - 1),
                )
            else:
                semantic_text = ""
                direct_keywords = self._dedupe(semantic.candidateHints)
            # A hard fuzzy night experience cannot be solved reliably by
            # spending the complete budget on generic AMap category queries.
            # Reserve one attempt for named-entity discovery, then exact-bind
            # every discovered entity back to AMap before admission.
            reserve_web_seed = (
                semantic.intentType == "night_view" and semantic.maxQueries >= 2
            )
            # A public night-view need is not an invitation to enumerate every
            # generic citywide hint.  Probe one authoritative entity phrase,
            # then reserve the bounded Web-to-AMap path for a distinct
            # canonical entity.  Additional direct variants are only useful
            # after a route-conditioned retry supplies a new cursor.
            # Preserve the core night-view ladder: reserve exactly one bounded
            # slot for named-entity discovery, while the remaining budget
            # stays available for distinct AMap place facets.
            direct_query_limit = (
                max(1, semantic.maxQueries - 1)
                if reserve_web_seed
                else semantic.maxQueries
            )
            plans = [
                self._query_plan(
                    profile_fingerprint=profile_fingerprint,
                    priority=max(70, 100 - index),
                    mode="amap_text",
                    keyword=keyword,
                    keyword_variants=[keyword],
                    provider_category_keys=provider_category_keys,
                    preferred_type_groups=preferred_type_groups,
                    rejected_type_groups=rejected_type_groups,
                    anchor_policy="none",
                    radius_meters=radius_meters,
                    result_limit=result_limit,
                    fallback_level=0,
                )
                for index, keyword in enumerate(
                    direct_keywords[:direct_query_limit]
                )
            ]
            raw_need = self._clean_text(semantic.rawNeed)
            if (
                len(plans) < semantic.maxQueries
                and not reserve_web_seed
                and raw_need
                and raw_need not in direct_keywords
            ):
                plans.append(
                    self._query_plan(
                        profile_fingerprint=profile_fingerprint,
                        priority=60,
                        mode="amap_text",
                        keyword=raw_need,
                        keyword_variants=[raw_need],
                        provider_category_keys=provider_category_keys,
                        preferred_type_groups=preferred_type_groups,
                        rejected_type_groups=rejected_type_groups,
                        anchor_policy="none",
                        radius_meters=radius_meters,
                        result_limit=result_limit,
                        fallback_level=1,
                    )
                )
            if len(plans) < semantic.maxQueries:
                web_keyword = (
                    NightViewEntitySearchPolicy.web_discovery_keyword(
                        semantic_text=semantic_text,
                        occurrence_index=max(0, semantic.dayNumber - 1),
                    )
                    if semantic.intentType == "night_view"
                    else raw_need or direct_keywords[0]
                )
                plans.append(
                    self._query_plan(
                        profile_fingerprint=profile_fingerprint,
                        priority=40,
                        mode="web_seed_then_amap",
                        keyword=web_keyword,
                        keyword_variants=[web_keyword],
                        provider_category_keys=provider_category_keys,
                        preferred_type_groups=preferred_type_groups,
                        rejected_type_groups=rejected_type_groups,
                        anchor_policy="none",
                        radius_meters=radius_meters,
                        result_limit=result_limit,
                        fallback_level=3,
                    )
                )
            return plans[: semantic.maxQueries]

        anchor_policy = self._anchor_policy(
            semantic.previousAnchor, semantic.nextAnchor
        )
        primary = self._query_plan(
            profile_fingerprint=profile_fingerprint,
            priority=100,
            mode="amap_text",
            keyword=keyword_variants[0],
            keyword_variants=keyword_variants,
            provider_category_keys=provider_category_keys,
            preferred_type_groups=preferred_type_groups,
            rejected_type_groups=rejected_type_groups,
            anchor_policy="none",
            radius_meters=radius_meters,
            result_limit=result_limit,
            fallback_level=1 if unknown_family else 0,
        )
        plans = [primary]
        if not unknown_family and semantic.maxQueries > len(plans):
            plans.append(
                self._query_plan(
                    profile_fingerprint=profile_fingerprint,
                    priority=90,
                    mode="amap_around",
                    keyword=keyword_variants[0],
                    keyword_variants=keyword_variants,
                    provider_category_keys=provider_category_keys,
                    preferred_type_groups=preferred_type_groups,
                    rejected_type_groups=rejected_type_groups,
                    anchor_policy=(
                        anchor_policy if anchor_policy != "none" else "previous_only"
                    ),
                    radius_meters=radius_meters,
                    result_limit=result_limit,
                    fallback_level=1,
                )
            )
        if (
            experience_family in {"local_life", "market", "art"}
            and semantic.maxQueries > len(plans)
        ):
            plans.append(
                self._query_plan(
                    profile_fingerprint=profile_fingerprint,
                    priority=80,
                    mode="route_corridor",
                    keyword=keyword_variants[0],
                    keyword_variants=keyword_variants,
                    provider_category_keys=provider_category_keys,
                    preferred_type_groups=preferred_type_groups,
                    rejected_type_groups=rejected_type_groups,
                    anchor_policy="between_adjacent",
                    radius_meters=radius_meters,
                    result_limit=result_limit,
                    fallback_level=2,
                )
            )
        if semantic.maxQueries > len(plans):
            plans.append(
                self._query_plan(
                    profile_fingerprint=profile_fingerprint,
                    priority=60,
                    mode="web_seed_then_amap",
                    keyword=keyword_variants[0],
                    keyword_variants=keyword_variants,
                    provider_category_keys=provider_category_keys,
                    preferred_type_groups=preferred_type_groups,
                    rejected_type_groups=rejected_type_groups,
                    anchor_policy="none",
                    radius_meters=radius_meters,
                    result_limit=result_limit,
                    fallback_level=3,
                )
            )
        return plans[: semantic.maxQueries]

    def _query_plan(
        self,
        *,
        profile_fingerprint: str,
        priority: int,
        mode: str,
        keyword: str,
        keyword_variants: list[str],
        provider_category_keys: list[str],
        preferred_type_groups: list[str],
        rejected_type_groups: list[str],
        anchor_policy: str,
        radius_meters: int,
        result_limit: int,
        fallback_level: int,
    ) -> SearchQueryPlan:
        plan_identity = {
            "profileFingerprint": profile_fingerprint,
            "mode": mode,
            "keyword": self._fingerprint_text(keyword),
            "providerCategoryKeys": sorted(
                self._fingerprint_values(provider_category_keys)
            ),
            "anchorPolicy": anchor_policy,
            "fallbackLevel": fallback_level,
        }
        return SearchQueryPlan(
            planId="query_" + self._fingerprint(plan_identity)[:24],
            priority=priority,
            mode=mode,
            keyword=keyword,
            keywordVariants=keyword_variants,
            providerCategoryKeys=provider_category_keys,
            preferredTypeGroups=preferred_type_groups,
            rejectedTypeGroups=rejected_type_groups,
            anchorPolicy=anchor_policy,
            radiusMeters=radius_meters,
            resultLimit=result_limit,
            fallbackLevel=fallback_level,
            requiresAmapGrounding=True,
            stopWhenTargetReached=True,
        )

    @staticmethod
    def _family_source(
        semantic: ExperienceSemanticInput,
    ) -> tuple[str, str]:
        optional_family = ExperienceSearchProfileCompiler._fingerprint_text(
            semantic.optionalExperienceFamily
        )
        if optional_family:
            return optional_family, "optional_experience_family"
        assigned_family = ExperienceSearchProfileCompiler._fingerprint_text(
            semantic.assignedMealFamily
        )
        if assigned_family:
            return assigned_family, "assigned_meal_family"
        return "", "unknown"

    @classmethod
    def _experience_spec_family(cls, semantic: ExperienceSemanticInput) -> tuple[str, str]:
        """Resolve the single server-authored ExperienceSpec family, if any.

        AgentService derives these fields from the current server-owned request
        contract before candidate collection.  A raw DaySlot/controller
        payload therefore never becomes an admission-family authority.
        """

        return (
            cls._fingerprint_text(semantic.experienceSpecFamily),
            cls._fingerprint_text(semantic.experienceSpecFamilyError),
        )

    @staticmethod
    def _fallback_activity_mode(intent_type: str) -> str:
        return {
            "area_walk": "walk",
            "park": "relax",
            "meal": "meal",
            "rest": "relax",
        }.get(str(intent_type or "").strip(), "visit")

    @staticmethod
    def _unknown_keyword_variants(
        semantic: ExperienceSemanticInput, exact_entity: str
    ) -> list[str]:
        if exact_entity:
            return [exact_entity]
        values = ExperienceSearchProfileCompiler._dedupe(
            [semantic.rawNeed, *semantic.candidateHints]
        )
        return values or ["待确认体验"]

    @staticmethod
    def _exact_category_keys(intent_type: str) -> list[str]:
        return list(
            _EXACT_ENTITY_CATEGORY_KEYS.get(
                str(intent_type or "").strip(), ("all",)
            )
        )

    @staticmethod
    def _anchor_policy(
        previous_anchor: Optional[Mapping[str, Any]],
        next_anchor: Optional[Mapping[str, Any]],
    ) -> str:
        if previous_anchor and next_anchor:
            return "between_adjacent"
        if previous_anchor:
            return "previous_only"
        if next_anchor:
            return "next_only"
        return "none"

    @classmethod
    def _anchor_execution_identity(
        cls, anchor: Optional[Mapping[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if not anchor:
            return None
        physical_id = next(
            (
                cls._clean_text(anchor.get(key))
                for key in (
                    "physicalPoiId",
                    "amapPoiId",
                    "amapId",
                    "providerPoiId",
                    "id",
                )
                if cls._clean_text(anchor.get(key))
            ),
            "",
        )
        longitude = cls._bounded_coordinate(anchor.get("longitude"), -180.0, 180.0)
        latitude = cls._bounded_coordinate(anchor.get("latitude"), -90.0, 90.0)
        return {
            "physicalPoiId": physical_id or None,
            "longitude": longitude,
            "latitude": latitude,
        }

    @staticmethod
    def _bounded_coordinate(
        raw: Any, minimum: float, maximum: float
    ) -> Optional[float]:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not minimum <= value <= maximum:
            return None
        return round(value, 6)

    @staticmethod
    def _bounded_int(
        raw: Any, *, default: int, minimum: int, maximum: int
    ) -> int:
        if isinstance(raw, bool):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    @classmethod
    def _canonical_ids(cls, values: Iterable[Any]) -> list[str]:
        return sorted(cls._dedupe(values), key=lambda value: value.casefold())

    @classmethod
    def _dedupe(cls, values: Iterable[Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = cls._clean_text(raw)
            key = cls._fingerprint_text(value)
            if value and key not in seen:
                result.append(value)
                seen.add(key)
        return result

    @classmethod
    def _fingerprint_values(cls, values: Iterable[Any]) -> list[str]:
        return cls._dedupe(cls._fingerprint_text(value) for value in values)

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        normalized = unicodedata.normalize("NFKC", str(value))
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _fingerprint_text(cls, value: Any) -> str:
        return cls._clean_text(value).casefold()

    @classmethod
    def _canonical_json_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._canonical_json_value(value[key])
                for key in sorted(value, key=lambda item: str(item))
            }
        if isinstance(value, (list, tuple)):
            return [cls._canonical_json_value(item) for item in value]
        if isinstance(value, str):
            return cls._fingerprint_text(value)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return cls._fingerprint_text(value)

    @staticmethod
    def _fingerprint(payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(serialized.encode("utf-8")).hexdigest()

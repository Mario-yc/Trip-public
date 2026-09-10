"""Web idea discovery followed by mandatory exact AMap identity grounding."""

from __future__ import annotations

import re
from datetime import date, datetime
from hashlib import sha256
from time import perf_counter
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, Field

from src.providers.travel_tools import ResilientWebSearchProvider
from src.models.poi_search_profile import PoiSearchProfile
from src.services.experience_search_profile_compiler import (
    search_profile_provider_rejection_reason,
)
from src.services.amap_poi_search_plan_adapter import AmapPoiSearchPlanAdapter
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.night_view_entity_search_policy import (
    NightViewEntitySearchPolicy,
)


class DiscoveredPoiEntity(BaseModel):
    """A Web-derived idea seed which has no authority to become a POI."""

    name: str = Field(min_length=2)
    aliases: list[str] = Field(default_factory=list)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_name: str = Field(alias="sourceName")
    provider_name: str = Field(alias="providerName")
    credibility_rank: str = Field(alias="credibilityRank")
    freshness: Optional[str] = None
    source_claims: list[dict[str, Any]] = Field(default_factory=list, alias="sourceClaims", max_length=2)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class DiscoveryCandidateEvidence(BaseModel):
    """The only final-POI identity allowed into discovery diagnostics."""

    amap_id: str = Field(alias="amapId", min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class SeedAmapGroundingEvidence(BaseModel):
    """Bounded summary for one Web entity seed and its AMap lookup."""

    seed_name: str = Field(alias="seedName", min_length=1, max_length=120)
    provider_name: str = Field(alias="providerName", min_length=1, max_length=80)
    status: Literal["grounded", "unresolved", "provider_failure"]
    reason_code: Optional[str] = Field(default=None, alias="reasonCode", max_length=120)
    duration_ms: float = Field(default=0.0, alias="durationMs", ge=0)
    candidate_count: int = Field(default=0, alias="candidateCount", ge=0)
    selected_candidates: list[DiscoveryCandidateEvidence] = Field(
        default_factory=list,
        alias="selectedCandidates",
        max_length=2,
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


class WebDiscoveryEvidence(BaseModel):
    """Safe per-call Web discovery evidence; never stores result payloads."""

    query_fingerprint: str = Field(alias="queryFingerprint", min_length=64, max_length=64)
    provider_name: str = Field(alias="providerName", min_length=1, max_length=80)
    provider_status: Literal["success", "failed", "skipped"] = Field(
        default="success",
        alias="providerStatus",
    )
    status: Literal["grounded", "unresolved", "provider_failure", "budget_exhausted"]
    reason_code: Optional[str] = Field(default=None, alias="reasonCode", max_length=120)
    duration_ms: float = Field(default=0.0, alias="durationMs", ge=0)
    web_duration_ms: float = Field(default=0.0, alias="webDurationMs", ge=0)
    amap_grounding_ms: float = Field(default=0.0, alias="amapGroundingMs", ge=0)
    result_count: int = Field(default=0, alias="resultCount", ge=0)
    seed_count: int = Field(default=0, alias="seedCount", ge=0)
    seed_records_truncated: bool = Field(
        default=False,
        alias="seedRecordsTruncated",
    )
    seed_groundings: list[SeedAmapGroundingEvidence] = Field(
        default_factory=list,
        alias="seedGroundings",
        max_length=6,
    )
    provider_attempts: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="providerAttempts",
        max_length=6,
    )
    search_profile_id: Optional[str] = Field(default=None, alias="searchProfileId", max_length=160)
    profile_fingerprint: Optional[str] = Field(default=None, alias="profileFingerprint", max_length=64)
    query_plan_id: Optional[str] = Field(default=None, alias="queryPlanId", max_length=160)
    fallback_level: Optional[int] = Field(default=None, alias="fallbackLevel", ge=0, le=6)
    provider_category_key: Optional[str] = Field(default=None, alias="providerCategoryKey", max_length=80)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PoiDiscoveryResult(BaseModel):
    status: Literal["grounded", "unresolved", "provider_failure", "budget_exhausted"]
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    web_query_count: int = Field(default=0, alias="webQueryCount")
    amap_query_count: int = Field(default=0, alias="amapQueryCount")
    web_seed_count: int = Field(default=0, alias="webSeedCount")
    web_only_final_poi_count: int = Field(default=0, alias="webOnlyFinalPoiCount")
    fake_coordinate_count: int = Field(default=0, alias="fakeCoordinateCount")
    provider_diagnostics: list[dict[str, Any]] = Field(default_factory=list, alias="providerDiagnostics")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    web_duration_ms: float = Field(default=0.0, alias="webDurationMs")
    amap_grounding_ms: float = Field(default=0.0, alias="amapGroundingMs")
    discovery_evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="discoveryEvidence",
        max_length=2,
    )
    web_snippet_consumed_count: int = Field(default=0, alias="webSnippetConsumedCount")
    web_claim_extracted_count: int = Field(default=0, alias="webClaimExtractedCount")
    independent_source_count: int = Field(default=0, alias="independentSourceCount")
    supporting_claim_count: int = Field(default=0, alias="supportingClaimCount")
    contradiction_claim_count: int = Field(default=0, alias="contradictionClaimCount")
    amap_detail_fetch_count: int = Field(default=0, alias="amapDetailFetchCount")
    amap_detail_cache_hit_count: int = Field(default=0, alias="amapDetailCacheHitCount")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class PoiDiscoveryService:
    """Discover public entity names on Web, then resolve only through AMap."""

    _CATEGORY_BY_INTENT = {
        "meal": "food",
        "campus_visit": "campus",
        "museum": "museum",
        "art_museum": "museum",
        "night_view": "scenic",
        "neighborhood_walk": "scenic",
        "area_walk": "scenic",
    }
    _QUERY_SECRET_PATTERN = re.compile(
        r"(?i)(?:authorization|api[-_ ]?key|password|passwd|credential|secret|token)"
        r"\s*[:=]\s*\S+|(?:https?|file|data|blob|javascript):\S+|"
        r"[A-Za-z]:[\\/]\S+|\\\\\S+|/(?:home|users?|tmp|var|etc)/\S+|~[\\/]\S+|"
        r"\b(?:sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16})\b"
    )
    _QUERY_UNSAFE_CHARACTER_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\s（）()，,。·、_-]+")

    def __init__(
        self,
        *,
        web_search_provider: Optional[object] = None,
        map_poi_service: Optional[MapPoiService] = None,
        max_web_queries: int = 1,
        max_amap_seed_queries: int = 4,
    ) -> None:
        self.web_search_provider = web_search_provider or ResilientWebSearchProvider()
        self.map_poi_service = map_poi_service or MapPoiService()
        self.max_web_queries = max(0, min(int(max_web_queries), 2))
        self.max_amap_seed_queries = max(0, min(int(max_amap_seed_queries), 6))

    def discover(
        self,
        *,
        search_profile: PoiSearchProfile | Mapping[str, Any] | None = None,
        city: Optional[str] = None,
        intent_type: Optional[str] = None,
        raw_need: Optional[str] = None,
        candidate_hints: Optional[list[str]] = None,
        evidence_candidate_names: Optional[list[str]] = None,
        excluded_candidate_names: Optional[list[str]] = None,
        query_variant_index: int = 0,
        trigger_reason: str,
        exclusion_context: Optional[Mapping[str, Any]] = None,
        budget_context: Optional[Mapping[str, Any]] = None,
    ) -> PoiDiscoveryResult:
        del exclusion_context, budget_context
        profile = (
            search_profile
            if isinstance(search_profile, PoiSearchProfile)
            else PoiSearchProfile.model_validate(search_profile)
            if search_profile is not None
            else None
        )
        city = str(profile.city if profile is not None else city or "").strip()
        intent_type = str(profile.intentType if profile is not None else intent_type or "").strip()
        raw_need = str(profile.sourceEvidence.rawNeed if profile is not None else raw_need or "").strip()
        hint_entities = self._candidate_hint_entities(
            candidate_hints or [],
            city,
            intent_type=intent_type,
            reject_semantic_descriptors=True,
        )
        evidence_entities = self._candidate_hint_entities(
            evidence_candidate_names or [],
            city,
        )
        excluded_entities = self._candidate_hint_entities(
            excluded_candidate_names or [],
            city,
        )
        available_hint_entities = [
            hint
            for hint in hint_entities
            if not any(self._entity_names_overlap(hint, excluded) for excluded in excluded_entities)
        ]
        if available_hint_entities:
            rotation = max(0, int(query_variant_index or 0)) % len(available_hint_entities)
            available_hint_entities = [
                *available_hint_entities[rotation:],
                *available_hint_entities[:rotation],
            ]
        available_evidence_entities = [
            entity
            for entity in evidence_entities
            if not any(self._entity_names_overlap(entity, excluded) for excluded in excluded_entities)
        ]
        focused_hint_entities = (
            available_evidence_entities
            if available_evidence_entities
            else available_hint_entities
            if str(intent_type or "") == "night_view" and available_hint_entities
            else []
        )
        if not city or not raw_need:
            return PoiDiscoveryResult(
                status="unresolved",
                failureReason="poi_discovery_missing_semantic_input",
                discoveryEvidence=[
                    WebDiscoveryEvidence(
                        queryFingerprint=sha256("地点发现查询".encode("utf-8")).hexdigest(),
                        providerName="server",
                        providerStatus="skipped",
                        status="unresolved",
                        reasonCode="poi_discovery_missing_semantic_input",
                    ).model_dump(by_alias=True, exclude_none=True)
                ],
            )
        rejection_reason = search_profile_provider_rejection_reason(profile)
        if rejection_reason:
            return PoiDiscoveryResult(
                status="unresolved",
                failureReason=rejection_reason,
                discoveryEvidence=[
                    WebDiscoveryEvidence(
                        searchProfileId=profile.profileId,
                        profileFingerprint=profile.profileFingerprint,
                        queryFingerprint=sha256("地点发现查询".encode("utf-8")).hexdigest(),
                        providerName="server",
                        providerStatus="skipped",
                        status="unresolved",
                        reasonCode=rejection_reason,
                    ).model_dump(
                        by_alias=True,
                        exclude_none=True,
                    )
                ],
            )

        source_plan = None
        amap_plan = None
        if profile is not None:
            source_plan = next(
                (plan for plan in profile.queryPlans if plan.mode == "web_seed_then_amap"),
                profile.queryPlans[0],
            )
            adapted = AmapPoiSearchPlanAdapter().adapt(profile)
            amap_plan = next(
                (plan for plan in adapted if plan.sourcePlanId == source_plan.planId),
                adapted[0] if adapted else None,
            )
        focused_hint = focused_hint_entities[0] if focused_hint_entities else ""
        query_keyword = (
            f"{focused_hint} {self._intent_query_qualifier(intent_type)}".strip()
            if focused_hint
            else source_plan.keyword
            if source_plan is not None
            else raw_need
        )
        query = f"{city} {query_keyword}" if focused_hint else f"{city} {query_keyword} 官方 地点"
        evidence_query_fingerprint = sha256(query.encode("utf-8")).hexdigest()
        evidence_profile = (
            {
                "searchProfileId": profile.profileId,
                "profileFingerprint": profile.profileFingerprint,
                "queryPlanId": source_plan.planId if source_plan is not None else None,
                "fallbackLevel": (source_plan.fallbackLevel if source_plan is not None else None),
                "providerCategoryKey": (amap_plan.providerCategoryKey if amap_plan is not None else None),
            }
            if profile is not None
            else {}
        )
        if self.max_web_queries <= 0 or self.max_amap_seed_queries <= 0:
            return PoiDiscoveryResult(
                status="budget_exhausted",
                failureReason="poi_discovery_budget_exhausted",
                discoveryEvidence=[
                    WebDiscoveryEvidence(
                        **evidence_profile,
                        queryFingerprint=evidence_query_fingerprint,
                        providerName="web",
                        providerStatus="skipped",
                        status="budget_exhausted",
                        reasonCode="poi_discovery_budget_exhausted",
                    ).model_dump(by_alias=True, exclude_none=True)
                ],
            )
        web_started = perf_counter()
        try:
            web = self.web_search_provider.search(
                query,
                count=max(3, self.max_amap_seed_queries),
                freshness="noLimit",
            )
        except Exception as error:
            web_duration_ms = round((perf_counter() - web_started) * 1000, 3)
            return PoiDiscoveryResult(
                status="provider_failure",
                webQueryCount=1,
                providerDiagnostics=[
                    {
                        "provider": "web",
                        "status": "failed",
                        "reason": type(error).__name__,
                    }
                ],
                failureReason="web_discovery_provider_failure",
                webDurationMs=web_duration_ms,
                discoveryEvidence=[
                    WebDiscoveryEvidence(
                        **evidence_profile,
                        queryFingerprint=evidence_query_fingerprint,
                        providerName=self._safe_provider_name(
                            getattr(self.web_search_provider, "provider_name", "web")
                        ),
                        providerStatus="failed",
                        status="provider_failure",
                        reasonCode="web_discovery_provider_failure",
                        durationMs=web_duration_ms,
                        webDurationMs=web_duration_ms,
                    ).model_dump(by_alias=True, exclude_none=True)
                ],
            )
        web_duration_ms = round((perf_counter() - web_started) * 1000, 3)
        web_provider_name = self._safe_provider_name(getattr(web, "provider_name", "web"))
        web_results = list(getattr(web, "results", []) or [])
        web_provider_status = "failed" if getattr(web, "failure_reason", None) else "success"
        provider_attempts = self._safe_provider_attempts(getattr(web, "provider_diagnostics", []) or [])
        diagnostics = [
            {
                "provider": "web",
                "providerName": str(getattr(web, "provider_name", "") or ""),
                "status": "failed" if getattr(web, "failure_reason", None) else "success",
                "resultCount": len(web_results),
            },
            *provider_attempts,
        ]
        seeds = self._entity_seeds(
            web_results,
            candidate_hints=focused_hint_entities if focused_hint else [],
            intent_type=intent_type,
            city=city,
        )
        if not seeds:
            status = "provider_failure" if getattr(web, "failure_reason", None) else "unresolved"
            reason_code = str(getattr(web, "failure_reason", None) or "web_discovery_no_entity_seed")
            return PoiDiscoveryResult(
                status=status,
                webQueryCount=1,
                providerDiagnostics=diagnostics,
                failureReason=reason_code,
                webDurationMs=web_duration_ms,
                discoveryEvidence=[
                    WebDiscoveryEvidence(
                        **evidence_profile,
                        queryFingerprint=evidence_query_fingerprint,
                        providerName=web_provider_name,
                        providerStatus=web_provider_status,
                        status=status,
                        reasonCode=self._safe_reason_code(reason_code),
                        durationMs=web_duration_ms,
                        webDurationMs=web_duration_ms,
                        resultCount=len(web_results),
                        providerAttempts=provider_attempts,
                    ).model_dump(by_alias=True, exclude_none=True)
                ],
            )
        category = amap_plan.category if amap_plan is not None else self._CATEGORY_BY_INTENT.get(intent_type, "all")
        grounded: list[dict[str, Any]] = []
        seen: set[str] = set()
        amap_queries = 0
        map_failures = 0
        seed_groundings: list[SeedAmapGroundingEvidence] = []
        amap_started = perf_counter()
        detail_attempts = 0
        detail_fetch_before = int(getattr(self.map_poi_service, "detail_fetch_count", 0) or 0)
        detail_cache_before = int(getattr(self.map_poi_service, "detail_cache_hit_count", 0) or 0)
        for seed in seeds[: self.max_amap_seed_queries]:
            amap_queries += 1
            seed_started = perf_counter()
            hint_bound = any(self._entity_names_overlap(seed.name, hint) for hint in focused_hint_entities)
            try:
                response = self.map_poi_service.search(
                    city,
                    seed.name,
                    category="all" if hint_bound else category,
                    limit=8,
                )
            except Exception as error:
                seed_duration_ms = round((perf_counter() - seed_started) * 1000, 3)
                map_failures += 1
                diagnostics.append(
                    {
                        "provider": "amap",
                        "status": "failed",
                        "seed": seed.name,
                        "reason": type(error).__name__,
                    }
                )
                seed_groundings.append(
                    SeedAmapGroundingEvidence(
                        seedName=self._safe_seed_name(seed.name),
                        providerName="amap-place-search",
                        status="provider_failure",
                        reasonCode="amap_seed_provider_failure",
                        durationMs=seed_duration_ms,
                    )
                )
                continue
            seed_duration_ms = round((perf_counter() - seed_started) * 1000, 3)
            response_pois = list(getattr(response, "pois", []) or [])
            map_provider_name = self._safe_provider_name(getattr(response, "provider_name", AMAP_PLACE_SOURCE))
            diagnostics.append(
                {
                    "provider": "amap",
                    "providerName": str(getattr(response, "provider_name", AMAP_PLACE_SOURCE)),
                    "status": "success",
                    "seed": seed.name,
                    "resultCount": len(response_pois),
                }
            )
            best = self._best_exact_amap_match(
                seed.name,
                response_pois,
                city=city,
                aliases=seed.aliases,
            )
            if best is None:
                seed_groundings.append(
                    SeedAmapGroundingEvidence(
                        seedName=self._safe_seed_name(seed.name),
                        providerName=map_provider_name,
                        status="unresolved",
                        reasonCode="amap_seed_no_exact_match",
                        durationMs=seed_duration_ms,
                        candidateCount=len(response_pois),
                    )
                )
                continue
            amap_id = str(best.get("amapId") or best.get("id") or "")
            if not amap_id or amap_id in seen:
                seed_groundings.append(
                    SeedAmapGroundingEvidence(
                        seedName=self._safe_seed_name(seed.name),
                        providerName=map_provider_name,
                        status="unresolved",
                        reasonCode="amap_seed_identity_duplicate_or_missing",
                        durationMs=seed_duration_ms,
                        candidateCount=len(response_pois),
                    )
                )
                continue
            if detail_attempts < 3 and re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id.upper()):
                detail_attempts += 1
                try:
                    detail = self.map_poi_service.detail(amap_id)
                    detail_payload = detail.model_dump(by_alias=True)
                    detail_identity_values = {
                        str(detail_payload.get(key) or "").strip().upper()
                        for key in ("amapId", "id")
                        if str(detail_payload.get(key) or "").strip()
                    }
                    detail_identity_matches = detail_identity_values == {amap_id.upper()}
                    enriched = (
                        self._best_exact_amap_match(
                            seed.name,
                            [detail_payload],
                            city=city,
                            aliases=[
                                *seed.aliases,
                                str(best.get("name") or ""),
                            ],
                        )
                        if detail_identity_matches
                        else None
                    )
                    if enriched is not None:
                        best = enriched
                    elif detail_identity_values:
                        diagnostics.append(
                            {
                                "provider": "amap-detail",
                                "status": "rejected",
                                "reason": "amap_detail_identity_mismatch",
                            }
                        )
                    else:
                        diagnostics.append(
                            {
                                "provider": "amap-detail",
                                "status": "rejected",
                                "reason": "amap_detail_identity_missing",
                            }
                        )
                except Exception:
                    pass
            seen.add(amap_id)
            if hint_bound:
                existing_note = str(best.get("sourceNote") or best.get("source_note") or "").strip()
                evidence_note = f"matchedCandidateHint:{seed.name}; matchedCandidateHintBinding:discovery_query"
                best["sourceNote"] = f"{existing_note}; {evidence_note}" if existing_note else evidence_note
            best.update(
                {
                    "candidateSource": "web_seed_amap_grounded",
                    "sourcePrecheck": {"passed": True, "scope": "web_seed_amap_grounded"},
                    "sourceClaims": list(seed.source_claims),
                    **(
                        {
                            "searchProfileId": profile.profileId,
                            "searchProfileFingerprint": profile.profileFingerprint,
                            "experienceFamily": profile.experienceFamily,
                            "activityMode": profile.activityMode,
                            "queryPlanId": source_plan.planId,
                            "searchMode": "web_seed_then_amap",
                            "fallbackLevel": source_plan.fallbackLevel,
                            "matchedKeyword": source_plan.keyword,
                            "sourceBriefId": profile.briefId,
                            "sourcePoolId": profile.poolId,
                            "sourcePlanningSlotId": profile.planningSlotId,
                        }
                        if profile is not None and source_plan is not None
                        else {}
                    ),
                    "discoveryProvenance": {
                        "triggerReason": trigger_reason,
                        "webQueryFingerprint": evidence_query_fingerprint,
                        "webProvider": str(getattr(web, "provider_name", "") or ""),
                        "webTitle": seed.title,
                        "webUrlHash": sha256(seed.url.encode("utf-8")).hexdigest(),
                        "webSourceName": seed.source_name,
                        "webCredibilityRank": seed.credibility_rank,
                        "webFreshness": seed.freshness,
                        "entitySeed": seed.name,
                        "entityAliases": list(seed.aliases),
                        "mapProvider": str(getattr(response, "provider_name", AMAP_PLACE_SOURCE)),
                        "amapQueryFingerprint": sha256(seed.name.encode("utf-8")).hexdigest(),
                        "amapId": amap_id,
                    },
                }
            )
            grounded.append(best)
            safe_amap_id = self._safe_amap_id(amap_id)
            safe_candidate_name = self._safe_candidate_name(best.get("name"))
            selected_candidates = (
                [
                    DiscoveryCandidateEvidence(
                        amapId=safe_amap_id,
                        name=safe_candidate_name,
                    )
                ]
                if safe_amap_id and safe_candidate_name
                else []
            )
            seed_groundings.append(
                SeedAmapGroundingEvidence(
                    seedName=self._safe_seed_name(seed.name),
                    providerName=map_provider_name,
                    status="grounded",
                    durationMs=seed_duration_ms,
                    candidateCount=len(response_pois),
                    selectedCandidates=selected_candidates,
                )
            )
        amap_grounding_ms = round((perf_counter() - amap_started) * 1000, 3)
        status = (
            "grounded"
            if grounded
            else "provider_failure"
            if map_failures == amap_queries and amap_queries
            else "unresolved"
        )
        failure_reason = None if grounded else "web_seeds_not_grounded_by_amap"
        return PoiDiscoveryResult(
            status=status,
            candidates=grounded,
            webQueryCount=1,
            amapQueryCount=amap_queries,
            webSeedCount=len(seeds),
            webOnlyFinalPoiCount=0,
            fakeCoordinateCount=0,
            providerDiagnostics=diagnostics,
            failureReason=failure_reason,
            webDurationMs=web_duration_ms,
            amapGroundingMs=amap_grounding_ms,
            webSnippetConsumedCount=sum(1 for seed in seeds if seed.source_claims),
            webClaimExtractedCount=sum(len(seed.source_claims) for seed in seeds),
            independentSourceCount=len(
                {
                    claim.get("sourceUrlHash")
                    for seed in seeds
                    for claim in seed.source_claims
                    if claim.get("sourceUrlHash")
                }
            ),
            supportingClaimCount=sum(
                1 for seed in seeds for claim in seed.source_claims if claim.get("stance") == "support"
            ),
            contradictionClaimCount=sum(
                1 for seed in seeds for claim in seed.source_claims if claim.get("stance") == "contradict"
            ),
            amapDetailFetchCount=max(
                0, int(getattr(self.map_poi_service, "detail_fetch_count", 0) or 0) - detail_fetch_before
            ),
            amapDetailCacheHitCount=max(
                0, int(getattr(self.map_poi_service, "detail_cache_hit_count", 0) or 0) - detail_cache_before
            ),
            discoveryEvidence=[
                WebDiscoveryEvidence(
                    **evidence_profile,
                    queryFingerprint=evidence_query_fingerprint,
                    providerName=web_provider_name,
                    providerStatus=web_provider_status,
                    status=status,
                    reasonCode=failure_reason,
                    durationMs=round(web_duration_ms + amap_grounding_ms, 3),
                    webDurationMs=web_duration_ms,
                    amapGroundingMs=amap_grounding_ms,
                    resultCount=len(web_results),
                    seedCount=len(seeds),
                    seedRecordsTruncated=len(seeds) > len(seed_groundings),
                    seedGroundings=seed_groundings,
                    providerAttempts=provider_attempts,
                ).model_dump(by_alias=True, exclude_none=True)
            ],
        )

    @classmethod
    def _safe_query_text(cls, value: object) -> str:
        text = cls._QUERY_SECRET_PATTERN.sub(" ", str(value or ""))
        text = cls._QUERY_UNSAFE_CHARACTER_PATTERN.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:160] or "地点发现查询"

    @classmethod
    def _safe_seed_name(cls, value: object) -> str:
        text = cls._QUERY_UNSAFE_CHARACTER_PATTERN.sub(" ", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:120] or "未知地点"

    @classmethod
    def _safe_candidate_name(cls, value: object) -> str:
        text = cls._QUERY_UNSAFE_CHARACTER_PATTERN.sub(" ", str(value or ""))
        return re.sub(r"\s+", " ", text).strip()[:120]

    @classmethod
    def _safe_snippet(cls, value: object) -> str:
        text = cls._QUERY_SECRET_PATTERN.sub(" ", str(value or ""))
        text = re.sub(
            r"(?i)(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
            " ",
            text,
        )
        text = re.sub(r"<[^>]{0,200}>", " ", text)
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:320]

    @classmethod
    def _source_claim(
        cls,
        item: object,
        *,
        url: str,
        intent_type: str = "",
        city: str = "",
    ) -> list[dict[str, Any]]:
        raw = getattr(item, "snippet", None) or getattr(item, "summary", None) or ""
        summary = cls._safe_snippet(raw)
        if not summary:
            return []
        source_name = cls._safe_provider_name(getattr(item, "source_name", "web") or "web")
        semantic_text = f"{getattr(item, 'title', '')} {summary}"
        city_text = re.sub(
            r"(特别行政区|自治州|地区|盟|市)$",
            "",
            str(city or "").strip(),
        )
        city_food_signal = bool(
            str(intent_type or "") == "meal"
            and city_text
            and city_text in semantic_text
            and re.search(
                r"餐厅|餐馆|饭店|酒楼|菜馆|菜品|招牌菜|美食|小吃|烤鸭|面食|火锅|烧烤|堂食",
                semantic_text,
            )
        )
        claim_key = "local_food" if city_food_signal else "operational_info"
        for key, pattern in (
            ("local_life", r"居民|社区|邻里|日常采购|日常生活|农贸市场|菜市场"),
            ("heritage_walk", r"历史|文化遗产|古迹|传统建筑|老街"),
            ("market_walk", r"市场|集市|摊位|商贩"),
            ("art_walk", r"艺术|画廊|展览|创意园|工作室"),
            ("local_food", r"当地|地方风味|特色菜|传统小吃|老字号"),
            ("night_view", r"夜景|夜游|灯光|夜间开放"),
        ):
            if claim_key == "operational_info" and re.search(pattern, semantic_text):
                claim_key = key
                break
        return [
            {
                "claimKey": claim_key,
                "stance": "support" if claim_key != "operational_info" else "neutral",
                "sourceType": "web_snippet",
                "sourceName": source_name,
                "sourceUrlHash": sha256(url.encode("utf-8")).hexdigest(),
                "freshness": str(getattr(item, "published_at", "") or "unknown")[:40],
                "confidence": 0.55,
                "summary": summary,
            }
        ]

    @staticmethod
    def _safe_provider_name(value: object) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "")).strip("-.")
        return text[:80] or "unknown"

    @staticmethod
    def _safe_reason_code(value: object) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "")).strip("_.:-")
        return text[:120] or "provider_failure"

    @classmethod
    def _safe_provider_attempts(cls, value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, int]] = set()
        allowed_statuses = {"success", "failed", "no_results", "skipped", "cache_hit"}
        queue = list(value)
        while queue and len(result) < 6:
            raw = queue.pop(0)
            if not isinstance(raw, dict):
                continue
            provider_name = cls._safe_provider_name(raw.get("providerName") or raw.get("provider"))
            nested = raw.get("providerDiagnostics")
            if provider_name == "multi-free-search" and isinstance(nested, list) and nested:
                queue = list(nested) + queue
                continue
            status = str(raw.get("status") or "").strip()
            if status not in allowed_statuses:
                continue
            reason_code = cls._safe_reason_code(raw.get("reasonCode") or raw.get("reason") or status)
            result_count = raw.get("resultCount")
            safe_result_count = (
                min(result_count, 1_000_000)
                if isinstance(result_count, int) and not isinstance(result_count, bool) and result_count >= 0
                else 0
            )
            identity = (provider_name, status, reason_code, safe_result_count)
            if identity in seen:
                continue
            seen.add(identity)
            row: dict[str, Any] = {
                "providerName": provider_name,
                "status": status,
                "reasonCode": reason_code,
            }
            duration = raw.get("durationMs")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
                row["durationMs"] = duration
            if isinstance(result_count, int) and not isinstance(result_count, bool) and result_count >= 0:
                row["resultCount"] = safe_result_count
            transport_route = str(raw.get("transportRoute") or "").strip()
            if transport_route in {"system", "direct"}:
                row["transportRoute"] = transport_route
            proxy_configured = raw.get("proxyConfigured")
            if isinstance(proxy_configured, bool):
                row["proxyConfigured"] = proxy_configured
            timeout_seconds = raw.get("timeoutSeconds")
            if (
                isinstance(timeout_seconds, (int, float))
                and not isinstance(timeout_seconds, bool)
                and 0 <= timeout_seconds <= 60
            ):
                row["timeoutSeconds"] = timeout_seconds
            result.append(row)
        return result

    @staticmethod
    def _safe_amap_id(value: object) -> str:
        text = re.sub(r"[^A-Za-z0-9_.:-]+", "", str(value or ""))
        return text[:120]

    @classmethod
    def _entity_seeds(
        cls,
        results: list[object],
        *,
        candidate_hints: Optional[list[str]] = None,
        intent_type: str = "",
        city: str = "",
    ) -> list[DiscoveredPoiEntity]:
        seeds: list[DiscoveredPoiEntity] = []
        seen: set[str] = set()
        for item in results:
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            url = str(getattr(item, "url", "") or "").strip()
            if not url:
                continue
            historical_entity_seed = cls._is_historical_entity_result(item)
            claims = (
                []
                if historical_entity_seed
                else cls._source_claim(
                    item,
                    url=url,
                    intent_type=intent_type,
                    city=city,
                )
            )
            semantic_text = cls._normalize_name(f"{title} {getattr(item, 'snippet', '')}")

            def append_seed(name: str, aliases: list[str]) -> None:
                normalized = cls._normalize_name(name)
                if (
                    len(normalized) < 2
                    or normalized in seen
                    or (
                        intent_type == "night_view"
                        and NightViewEntitySearchPolicy.is_generic_entity_seed(
                            name,
                            city,
                        )
                    )
                ):
                    return
                seen.add(normalized)
                seeds.append(
                    DiscoveredPoiEntity(
                        name=name,
                        aliases=aliases,
                        title=title,
                        url=url,
                        sourceName=str(getattr(item, "source_name", "") or "unknown"),
                        providerName=str(getattr(item, "provider_name", "") or "unknown"),
                        credibilityRank=str(getattr(item, "credibility_rank", "unknown") or "unknown"),
                        freshness=(str(getattr(item, "published_at", "") or "").strip() or None),
                        sourceClaims=claims,
                    )
                )

            matched_hint = False
            for hint in candidate_hints or []:
                if cls._normalize_name(hint) in semantic_text and (
                    historical_entity_seed or cls._claims_support_intent(claims, intent_type)
                ):
                    append_seed(hint, [hint])
                    matched_hint = True
            # Search titles commonly use a colon between the entity name and
            # editorial description (for example ``景山公园：夜景观赏指南``).
            # The description is evidence text, not part of the AMap entity
            # identity, so keep only the leading named place before grounding.
            name = re.split(r"\s*[-—_|｜:：]\s*", title, maxsplit=1)[0].strip()
            name = re.sub(
                r"\s*[（(](官方|官网|介绍|攻略|预订|预约)[^）)]*[）)]\s*$",
                "",
                name,
            ).strip()
            name = re.sub(r"\s*(官网|官方网站|官方介绍|旅游攻略)\s*$", "", name).strip()
            name = re.sub(
                r"\s*(?:(?:夜景|夜游|观景)(?:观赏|拍摄|游览)?(?:指南|介绍|攻略|资料)|(?:介绍|攻略|资料))\s*$",
                "",
                name,
            ).strip()
            if not candidate_hints:
                append_seed(name, [name])
            elif (
                not matched_hint
                and not historical_entity_seed
                and cls._claims_support_alternative_seed(claims, intent_type)
            ):
                # An entity-specific evidence lookup may return a different but
                # relevant named place.  Treat that title as a new entity seed;
                # never attach its claims to the original candidate hint.  The
                # alternative must still resolve to its own exact AMap identity
                # and pass downstream consumer admission.
                append_seed(name, [name])
        return seeds

    @staticmethod
    def _is_historical_entity_result(item: object) -> bool:
        observed_dates: list[date] = []
        published_at = str(getattr(item, "published_at", "") or "").strip()
        if published_at:
            try:
                observed_dates.append(datetime.fromisoformat(published_at.replace("Z", "+00:00")).date())
            except ValueError:
                pass
        evidence_text = " ".join(
            (
                published_at,
                str(getattr(item, "title", "") or ""),
                str(getattr(item, "snippet", "") or ""),
            )
        )
        for match in re.finditer(r"(?<!\d)(20\d{2})(?!\d)", evidence_text):
            try:
                observed_dates.append(date(int(match.group(1)), 1, 1))
            except ValueError:
                continue
        return bool(observed_dates and (date.today() - max(observed_dates)).days > 366)

    @classmethod
    def _candidate_hint_entities(
        cls,
        values: list[str],
        city: str,
        *,
        intent_type: str = "",
        reject_semantic_descriptors: bool = False,
    ) -> list[str]:
        entities: list[str] = []
        seen: set[str] = set()
        city_text = re.sub(r"\s+", " ", str(city or "")).strip()
        generic = {
            "夜景",
            "夜游",
            "观景",
            "观景点",
            "地标",
            "景点",
            "当地特色美食",
            "特色午餐",
        }
        for raw in values:
            value = cls._safe_seed_name(raw)
            if city_text:
                value = re.sub(
                    rf"^{re.escape(city_text)}\s+",
                    "",
                    value,
                ).strip()
            value = re.sub(
                r"\s*(?:夜景|观景(?:点|平台)?|夜游)$",
                "",
                value,
            ).strip()
            normalized = cls._normalize_name(value)
            if (
                len(normalized) < 2
                or value in generic
                or normalized in seen
                or (
                    reject_semantic_descriptors
                    and intent_type == "night_view"
                    and NightViewEntitySearchPolicy.is_generic_entity_seed(
                        value,
                        city,
                    )
                )
            ):
                continue
            seen.add(normalized)
            entities.append(value)
        return entities[:8]

    @classmethod
    def _entity_names_overlap(cls, left: object, right: object) -> bool:
        left_name = cls._normalize_name(left)
        right_name = cls._normalize_name(right)
        if not left_name or not right_name:
            return False
        return left_name == right_name or (
            min(len(left_name), len(right_name)) >= 3 and (left_name in right_name or right_name in left_name)
        )

    @staticmethod
    def _intent_query_qualifier(intent_type: str) -> str:
        return {
            "night_view": "夜景",
            "meal": "特色美食",
            "campus_visit": "参观",
            "museum": "展览",
        }.get(str(intent_type or ""), "体验")

    @staticmethod
    def _claims_support_intent(
        claims: list[dict[str, Any]],
        intent_type: str,
    ) -> bool:
        required_claim = {
            "night_view": "night_view",
            "meal": "local_food",
        }.get(str(intent_type or ""))
        return required_claim is None or any(
            str(claim.get("claimKey") or "") == required_claim and str(claim.get("stance") or "") == "support"
            for claim in claims
        )

    @classmethod
    def _claims_support_alternative_seed(
        cls,
        claims: list[dict[str, Any]],
        intent_type: str,
    ) -> bool:
        return cls._claims_support_intent(claims, intent_type) and any(
            str(claim.get("stance") or "") == "support" and str(claim.get("claimKey") or "") != "operational_info"
            for claim in claims
        )

    @classmethod
    def _best_exact_amap_match(
        cls,
        seed: str,
        pois: list[object],
        *,
        city: str,
        aliases: list[str] | None = None,
    ) -> Optional[dict[str, Any]]:
        accepted_names = {
            cls._normalize_name(value) for value in [seed, *(aliases or [])] if cls._normalize_name(value)
        }
        matches: list[tuple[int, float, dict[str, Any]]] = []
        for poi in pois:
            payload = (
                poi.model_dump(by_alias=True)
                if hasattr(poi, "model_dump")
                else dict(poi)
                if isinstance(poi, dict)
                else {}
            )
            amap_id = str(payload.get("amapId") or payload.get("id") or "")
            name = str(payload.get("name") or "")
            candidate_city = str(payload.get("city") or city)
            longitude = payload.get("longitude")
            latitude = payload.get("latitude")
            source = str(payload.get("source") or "").strip()
            provider_type = str(payload.get("providerType") or payload.get("type") or "").strip()
            normalized = cls._normalize_name(name)
            exactness = 3 if normalized in accepted_names else 0
            if (
                exactness == 0
                or not amap_id
                or not name
                or candidate_city not in {city, f"{city}市"}
                or not isinstance(longitude, (int, float))
                or not isinstance(latitude, (int, float))
                or float(longitude) == 0
                or float(latitude) == 0
                or not (-180 <= float(longitude) <= 180)
                or not (-90 <= float(latitude) <= 90)
                or not provider_type
                or source != AMAP_PLACE_SOURCE
            ):
                continue
            payload["amapId"] = amap_id
            payload["source"] = AMAP_PLACE_SOURCE
            payload["providerType"] = provider_type
            matches.append(
                (
                    exactness,
                    float(payload.get("confidence") or 0),
                    payload,
                )
            )
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def _normalize_name(value: object) -> str:
        return re.sub(
            r"[^0-9a-z\u4e00-\u9fff]+",
            "",
            str(value or "").lower(),
        )

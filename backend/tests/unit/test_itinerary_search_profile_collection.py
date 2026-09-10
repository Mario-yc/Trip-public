from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from src.models.poi import POI
from src.models.poi_search_profile import ExperienceSemanticInput
from src.models.poi_intent import PoiIntent
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
)
from src.services.itinerary_service import ItineraryService
from src.services.functional_slot_context_service import FunctionalSlotContext
from src.services.route_insertion_scorer import RouteInsertionScorer


def _poi(amap_id: str, name: str, provider_type: str):
    return SimpleNamespace(
        id=amap_id,
        amap_id=amap_id,
        name=name,
        city="北京",
        category=provider_type,
        type=provider_type,
        address="北京市朝阳区",
        district="朝阳区",
        longitude=116.47,
        latitude=39.96,
        source="amap-place-search",
        confidence=0.96,
    )


class _ProfileMapService:
    def __init__(self, *, excluded_id: str = "") -> None:
        self.calls: list[tuple[str, str, str, int]] = []
        self.excluded_id = excluded_id

    def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
        self.calls.append((city, keyword, category, limit))
        candidates = [
            _poi(
                self.excluded_id or "B0MARKET",
                "三源里菜市场",
                "购物服务;综合市场;农副产品市场",
            ),
            _poi("B0SCENIC", "城市热门景点", "风景名胜;风景名胜;观景点"),
        ]
        return SimpleNamespace(
            provider_name="amap-place-search",
            cache_hit=False,
            pois=candidates,
        )

    def search_nearby(self, *args, **kwargs):
        raise AssertionError("no anchor context: around search must not execute")


def _local_life_intent(*, excluded_ids: list[str] | None = None) -> PoiIntent:
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-local",
            briefId="brief-local",
            planningSlotId="slot-local",
            requirementLevel="optional",
            rawNeed="体验社区生活和市场",
            intentType="area_walk",
            optionalExperienceFamily="local_life",
            rejectedTypes=["住宅", "公司", "停车场"],
            excludedPhysicalPoiIds=list(excluded_ids or []),
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )
    return PoiIntent(
        raw_need="体验社区生活和市场",
        city="北京",
        day_number=1,
        time_window="14:00-17:00",
        intent_type="area_walk",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        preferred_types=list(profile.preferredPlaceFacets),
        rejected_types=list(profile.rejectedPlaceFacets),
        target_count=1,
        entity_binding_mode="category",
        optional_experience_family="local_life",
        search_profile=profile,
    )


def test_profile_collection_uses_adapter_category_and_preserves_query_provenance():
    map_service = _ProfileMapService()
    service = ItineraryService(
        sqlite3.connect(":memory:"),
        map_poi_service=map_service,
    )
    intent = _local_life_intent()

    candidates, state = service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )

    assert state == "ok"
    assert map_service.calls
    assert len(map_service.calls) == 1
    assert all(call[2] != "scenic" for call in map_service.calls)
    assert map_service.calls[0][2] in {"local_service", "market", "shopping", "food"}
    assert candidates
    assert all(
        getattr(candidate, "_trip_search_profile_fingerprint", "") == intent.search_profile.profileFingerprint
        for candidate in candidates
    )
    assert all(getattr(candidate, "_trip_query_plan_id", "") for candidate in candidates)


def test_family_semantic_mismatch_is_rejected_before_route_scoring():
    service = ItineraryService(
        sqlite3.connect(":memory:"),
        map_poi_service=_ProfileMapService(),
    )
    intent = _local_life_intent()
    candidates, _state = service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )

    scored, rejected = service._score_poi_candidates(intent, candidates)

    assert [item.candidate.id for item in scored] == ["B0MARKET"]
    selected = scored[0].candidate
    assert getattr(selected, "_trip_semantic_passed", None) is True
    assert getattr(selected, "_trip_exclusion_fingerprint", "") == intent.search_profile.exclusionFingerprint
    scenic = next(item for item in rejected if item.candidate.id == "B0SCENIC")
    assert any(
        reason
        in {
            "area_walk_family_mismatch",
            "area_walk_semantic_evidence_missing",
        }
        for reason in scenic.rejected_reasons
    )


def test_excluded_flexible_physical_identity_is_removed_during_collection():
    map_service = _ProfileMapService(excluded_id="B0USED")
    service = ItineraryService(
        sqlite3.connect(":memory:"),
        map_poi_service=map_service,
    )
    intent = _local_life_intent(excluded_ids=["B0USED"])

    candidates, _state = service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )

    assert all(candidate.id != "B0USED" for candidate in candidates)
    assert service._last_candidate_collection_stats["duplicateExcludedCount"] >= 1


def test_same_root_creative_families_ground_distinct_semantic_amap_identities():
    family_candidates = {
        "heritage_walk": (
            "B0HERITAGE",
            "南锣鼓巷历史文化街区",
            "风景名胜;特色街区;历史文化街区",
        ),
        "local_life": (
            "B0LOCAL",
            "东四社区文化市场",
            "购物服务;综合市场;社区市场",
        ),
        "market_walk": (
            "B0MARKET",
            "三源里菜市场",
            "购物服务;综合市场;农副产品市场",
        ),
        "art_walk": (
            "B0ART",
            "798艺术区",
            "科教文化服务;文化场馆;艺术中心",
        ),
        "park_relax": (
            "B0PARK",
            "奥林匹克森林公园",
            "风景名胜;公园广场;森林公园",
        ),
    }

    class FamilyMapService:
        def __init__(self, family: str) -> None:
            self.family = family
            self.calls: list[tuple[str, str]] = []

        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append((keyword, category))
            amap_id, name, provider_type = family_candidates[self.family]
            return SimpleNamespace(
                provider_name="amap-place-search",
                cache_hit=False,
                pois=[
                    _poi(amap_id, name, provider_type),
                    _poi("B0USED", name, provider_type),
                    _poi("B0SCENIC", "城市热门景点", "风景名胜;风景名胜;观景点"),
                ],
            )

        def search_nearby(self, *args, **kwargs):
            raise AssertionError("same-root fixture has no anchor context")

    selected_ids: set[str] = set()
    fingerprints: set[str] = set()
    executed_categories: set[str] = set()
    for family in family_candidates:
        profile = ExperienceSearchProfileCompiler().compile(
            ExperienceSemanticInput(
                city="北京",
                poolId=f"pool-{family}",
                briefId=f"brief-{family}",
                planningSlotId=f"slot-{family}",
                requirementLevel="optional",
                rawNeed=f"体验 {family}",
                intentType="area_walk",
                optionalExperienceFamily=family,
                excludedPhysicalPoiIds=["B0USED"],
                targetCount=1,
                evidenceTargetCount=1,
                maxQueries=4,
            )
        )
        intent = PoiIntent(
            raw_need=f"体验 {family}",
            city="北京",
            day_number=1,
            time_window="14:00-17:00",
            intent_type="area_walk",
            specificity="functional",
            search_queries=list(profile.keywordVariants),
            preferred_types=list(profile.preferredPlaceFacets),
            rejected_types=list(profile.rejectedPlaceFacets),
            target_count=1,
            entity_binding_mode="category",
            optional_experience_family=family,
            search_profile=profile,
        )
        map_service = FamilyMapService(family)
        service = ItineraryService(
            sqlite3.connect(":memory:"),
            map_poi_service=map_service,
        )
        candidates, state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=[],
        )
        scored, rejected = service._score_poi_candidates(intent, candidates)

        assert state == "ok"
        assert [item.candidate.id for item in scored] == [family_candidates[family][0]]
        assert all(item.candidate.id != "B0USED" for item in [*scored, *rejected])
        assert any(item.candidate.id == "B0SCENIC" for item in rejected)
        selected_ids.add(scored[0].candidate.id)
        fingerprints.add(profile.profileFingerprint)
        executed_categories.update(category for _keyword, category in map_service.calls)

    assert len(selected_ids) == len(family_candidates)
    assert len(fingerprints) == len(family_candidates)
    assert executed_categories != {"scenic"}


def test_profile_evidence_target_stops_core_hint_queries_before_generic_fallback():
    class HintMapService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.calls.append(keyword)
            return SimpleNamespace(
                provider_name="amap-place-search",
                cache_hit=False,
                pois=[
                    _poi(
                        f"AMAP_{len(self.calls)}",
                        keyword,
                        "科教文化服务;学校;高等院校",
                    )
                ],
            )

        def search_nearby(self, *args, **kwargs):
            raise AssertionError("core hint plan must not use around search")

    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-campus",
            planningSlotId="slot-campus",
            requirementLevel="required",
            rawNeed="高校参观",
            intentType="campus_visit",
            candidateHints=["A大学", "B大学"],
            hintPolicy="llm_common_knowledge_hint",
            targetCount=2,
            evidenceTargetCount=2,
            maxQueries=4,
        )
    )
    intent = PoiIntent(
        raw_need="高校参观",
        city="北京",
        day_number=1,
        time_window="09:00-11:00",
        intent_type="campus_visit",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        preferred_types=["高等院校"],
        rejected_types=["停车场"],
        candidate_hints=["A大学", "B大学"],
        target_count=2,
        search_profile=profile,
    )
    map_service = HintMapService()
    service = ItineraryService(
        sqlite3.connect(":memory:"),
        map_poi_service=map_service,
    )

    candidates, state = service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
    )

    assert state == "ok"
    assert map_service.calls == ["A大学", "B大学"]
    assert len(candidates) == 2
    assert service._last_candidate_collection_stats["earlyStopReason"] == ("unique_eligible_stop_count_reached")


def test_unsupported_creative_profile_is_rejected_before_amap_provider() -> None:
    for family, intent_type in (
        ("future_unknown_family", "area_walk"),
        ("art_walk", "future_intent"),
    ):
        profile = ExperienceSearchProfileCompiler().compile(
            ExperienceSemanticInput(
                city="北京",
                poolId=f"pool-{family}-{intent_type}",
                briefId="brief-unsupported",
                planningSlotId="slot-unsupported",
                requirementLevel="optional",
                rawNeed="未知创意体验",
                intentType=intent_type,
                optionalExperienceFamily=family,
                targetCount=1,
                evidenceTargetCount=1,
                maxQueries=4,
            )
        )
        intent = PoiIntent(
            raw_need="未知创意体验",
            city="北京",
            day_number=1,
            time_window="14:00-17:00",
            intent_type=intent_type,
            specificity="functional",
            search_queries=list(profile.keywordVariants),
            preferred_types=list(profile.preferredPlaceFacets),
            rejected_types=list(profile.rejectedPlaceFacets),
            target_count=1,
            entity_binding_mode="category",
            optional_experience_family=family,
            search_profile=profile,
        )
        map_service = _ProfileMapService()
        warnings: list[str] = []
        service = ItineraryService(
            sqlite3.connect(":memory:"),
            map_poi_service=map_service,
        )

        candidates, state = service._collect_poi_candidates(
            intent,
            plan=None,
            original=None,
            pois_by_id={},
            resolved_by_id={},
            warnings=warnings,
        )

        assert candidates == []
        assert state == "semantic_rejected"
        assert map_service.calls == []
        assert service._last_candidate_collection_stats["providerPreflightRejected"] is True
        assert service._last_candidate_collection_stats["providerPreflightReasonCode"] in {
            "creative_optional_family_unregistered",
            "creative_optional_intent_unregistered",
        }
        assert warnings == []


def test_density_nearby_search_profile_uses_verified_route_scope_before_citywide() -> None:
    route_contract = RouteInsertionScorer.build_route_decision_contract(
        source="test_server_route_policy",
        provenance={"transportMode": "transit", "contractVersion": 7},
        detour_tolerance={
            "maxGeneralizedCostDelta": 30.0,
            "maxDetourRatio": 0.3,
        },
        mobility_profile={
            "source": "test_server_route_policy",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    )
    assert route_contract is not None
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-night",
            briefId="brief-night",
            planningSlotId="slot-night-1",
            requirementLevel="required",
            rawNeed="每晚不同的户外公共夜景",
            intentType="night_view",
            candidateHints=["滨水公共夜景"],
            hintPolicy="controller_experience_spec",
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
            routeContext={
                "routeDecisionContract": route_contract,
                "clarificationContractVersion": 7,
            },
            evidencePolicy={"openingEvidence": "outdoor_public_policy"},
            groundingPolicy={"accessPolicy": "outdoor_public"},
        )
    )
    assert all(plan.mode != "amap_around" for plan in profile.queryPlans)
    assert profile.queryPlans[-1].mode == "web_seed_then_amap"
    assert profile.budgetPolicy.maxWebSeedQueries == 1
    intent = PoiIntent(
        raw_need="每晚不同的户外公共夜景",
        city="北京",
        day_number=1,
        time_window="19:00-22:00",
        intent_type="night_view",
        specificity="functional",
        search_queries=list(profile.keywordVariants),
        preferred_types=["滨水空间", "公共夜景"],
        rejected_types=["餐饮", "售票处"],
        candidate_hints=["滨水公共夜景"],
        target_count=1,
        search_profile=profile,
    )

    class RouteScopedMapService:
        def __init__(self) -> None:
            self.nearby_calls: list[tuple[str, float, float, str, str | None]] = []
            self.citywide_calls: list[str] = []

        def search(self, city, keyword, category="all", limit=12, bypass_cache=False):
            self.citywide_calls.append(keyword)
            return SimpleNamespace(provider_name="amap-place-search", cache_hit=False, pois=[])

        def search_nearby(
            self,
            city,
            longitude,
            latitude,
            keyword,
            category="all",
            radius=1500,
            limit=12,
            bypass_cache=False,
            *,
            query_scope_fingerprint=None,
        ):
            self.nearby_calls.append(
                (
                    city,
                    longitude,
                    latitude,
                    keyword,
                    query_scope_fingerprint,
                )
            )
            return SimpleNamespace(
                provider_name="amap-place-search",
                cache_hit=False,
                pois=[
                    _poi(
                        "B0NIGHT001",
                        "滨水公共夜景步道",
                        "风景名胜;水域景观;城市景观",
                    )
                ],
            )

    anchor = POI(
        id="poi-anchor",
        name="已验证日间锚点",
        city="北京",
        category="campus",
        latitude=39.95,
        longitude=116.45,
        source="amap-place-search",
        confidence=0.96,
        amap_id="B0ANCHOR01",
        type="科教文化服务;高等院校",
    )
    slot_context = FunctionalSlotContext(
        slot_id="slot-night-1",
        day_number=1,
        intent_type="night_view",
        raw_need=intent.raw_need,
        previous_anchor=anchor,
        next_anchor=None,
        same_day_anchors=[anchor],
        transport_mode="transit",
        search_radius_meters=3200,
    )
    map_service = RouteScopedMapService()
    service = ItineraryService(sqlite3.connect(":memory:"), map_poi_service=map_service)
    service._expand_density_nearby = True

    candidates, state = service._collect_poi_candidates(
        intent,
        plan=None,
        original=None,
        pois_by_id={},
        resolved_by_id={},
        warnings=[],
        slot_context=slot_context,
    )

    assert state == "ok"
    # Compiled fallback-plan cardinality is not an external-call budget.  The
    # route-scoped execution must issue exactly one verified nearby request and
    # must not prefetch any citywide fallback once that request yields a POI.
    assert len(map_service.nearby_calls) == 1
    assert map_service.citywide_calls == []
    assert [candidate.id for candidate in candidates] == ["B0NIGHT001"]
    stats = service._last_candidate_collection_stats
    assert stats["routeContextApplied"] is True
    scope = stats["routeContextQueryScope"]
    assert scope["dayNumber"] == 1
    assert scope["occurrenceId"] == "slot-night-1"
    assert scope["transportMode"] == "transit"
    assert scope["contractVersion"] == "7"
    assert len(scope["routeCorridorHash"]) == 64
    assert len(scope["evidenceRequirementFingerprint"]) == 64
    assert len(scope["queryFingerprint"]) == 64
    assert map_service.nearby_calls[0][4] == scope["queryFingerprint"]
    candidate = candidates[0]
    assert getattr(candidate, "_trip_route_query_scope_verified", None) is True
    assert getattr(candidate, "_trip_route_query_scope", None) == scope
    assert stats["eligibleCandidateCount"] == 0
    assert stats["consumerAdmissionPendingCount"] == 1

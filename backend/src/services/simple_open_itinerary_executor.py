from __future__ import annotations

import hashlib
import math
import re
import copy
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.api.schemas.agent import AgentInitialPlanOutput
from src.api.schemas.maps import MapPoiSearchResponse
from src.models.poi import POI
from src.models.poi_intent import PersistableSegmentPlan
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.guide_poi_identity_service import GuidePoiIdentityService
from src.services.meal_candidate_quality_policy import MealCandidateQualityPolicy
from src.services.meal_diversity_policy import MealDiversityPolicy
from src.services.meal_experience_portfolio import MealExperiencePortfolioPolicy
from src.services.map_poi_service import (
    AMAP_PLACE_AROUND_MAX_RADIUS_METERS,
    AMAP_PLACE_SOURCE,
    MapPoiService,
)
from src.services.agent_autonomy_service import AgentDecisionResult
from src.services.experience_independence_service import ExperienceIndependenceService
from src.services.night_view_candidate_policy import NightViewCandidatePolicy
from src.services.simple_open_dynamic_schedule_service import SimpleOpenDynamicScheduleService
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
from src.services.simple_open_route_assignment_service import SimpleOpenRouteAssignmentService
from src.services.spatial_geometry_service import SpatialGeometryService
from src.services.route_insertion_scorer import RouteInsertionScorer


MAX_SIMPLE_OPEN_SLOTS = 12
MAX_SIMPLE_OPEN_POI_SEARCHES = 6
MAX_SIMPLE_OPEN_DAILY_COMPLETION_SEARCHES = 2
_DINING_TYPE_RE = re.compile(
    r"(餐饮|餐厅|餐馆|饭店|酒楼|小吃|火锅|烧烤|咖啡|茶馆|甜品|烘焙|快餐|food|restaurant|cafe)",
    re.IGNORECASE,
)
_CAMPUS_TYPE_RE = re.compile(r"(科教文化服务|学校|高等院校|大学|学院|campus|university|college)", re.IGNORECASE)
_NIGHT_VIEW_HARD_REJECT_RE = re.compile(
    r"(餐饮|餐厅|餐馆|饭店|酒楼|小吃|火锅|烧烤|咖啡|茶馆|food|restaurant|cafe|"
    r"停车场|卫生间|售票处|服务中心|办公室|公司|住宅|小区)",
    re.IGNORECASE,
)
_NIGHT_VIEW_UNSAFE_QUERY_RE = re.compile(r"(摄影|拍照|航拍|剪影|写真|婚纱)", re.IGNORECASE)
_NIGHT_VIEW_GENERIC_QUERY_RE = re.compile(
    r"^(?:night[\s_-]*view|夜景|城市夜景|晚上看夜景|夜景地点|夜景地标)$",
    re.IGNORECASE,
)
_NIGHT_VIEW_PUBLIC_PARK_NATURAL_QUERY_RE = re.compile(
    r"(?:(?:公共|开放|附近).{0,20}(?:公园|滨水|水岸|夜景).{0,12}(?:散步|夜游|夜景)|"
    r"(?:公园|滨水|水岸).{0,10}(?:或|或者|/).{0,10}(?:公园|滨水|水岸|夜景))",
    re.IGNORECASE,
)
_GENERIC_MEAL_QUERIES = {
    "午餐",
    "晚餐",
    "餐饮",
    "餐厅",
    "美食",
    "当地美食",
    "当地餐饮",
    "特色美食",
    "特色餐饮",
    "当地特色美食",
    "当地特色餐饮",
    "当地特色餐厅",
    "地方风味餐厅",
    "传统市场周边餐饮",
    "午餐当地特色美食",
    "晚餐当地特色美食",
}
# Provider capability, not a cuisine knowledge base.  Each value must be
# demonstrated by a bounded real AMap request before it may narrow ``types``.
_VERIFIED_DESTINATION_CUISINE_PROVIDER_TYPES = {
    "北京": "北京菜",
}
_INTENT_PROVIDER_TYPE_RE: dict[str, re.Pattern[str]] = {
    "landmark": re.compile(r"(风景名胜|旅游景点|地标|广场|塔|城楼|纪念碑)", re.IGNORECASE),
    "museum": re.compile(r"(博物馆|美术馆|科技馆|展览馆|纪念馆|文化宫|科教文化服务)", re.IGNORECASE),
    "park": re.compile(r"(公园|园林|植物园|动物园|森林|湿地|绿地|风景名胜)", re.IGNORECASE),
    "shopping": re.compile(r"(购物|商场|商厦|商业街|步行街|百货|市场|零售)", re.IGNORECASE),
    "area_walk": re.compile(
        r"(风景名胜|旅游景点|公园|园林|街道|道路|步行街|商业街|历史街区|胡同|文化|市场|广场|商圈)",
        re.IGNORECASE,
    ),
    "local_culture": re.compile(
        r"(文化|民俗|非遗|胡同|历史街区|博物馆|纪念馆|文化馆|风景名胜)",
        re.IGNORECASE,
    ),
    "rest": re.compile(r"(住宿|酒店|宾馆|休息区|咖啡|茶馆|公园|广场)", re.IGNORECASE),
}


@dataclass(frozen=True)
class _AdjacentCandidateScope:
    """Read-only geometry for one bounded adjacent-stop candidate query."""

    day_seed: POI
    predecessor: POI
    successor: POI | None
    center_longitude: float
    center_latitude: float
    center_strategy: str
    predecessor_beam_rank: int


class SimpleOpenItineraryExecutor:
    """Ground a model-authored slot skeleton through bounded real AMap search.

    This component owns no conversation loop and performs no itinerary write.
    It only turns DaySlots into persistable segment plans; the established
    ``ItineraryPatchService`` remains the sole formal writer.
    """

    def __init__(
        self,
        map_poi_service: MapPoiService | None = None,
        intent_candidate_semantic_policy: IntentCandidateSemanticPolicy | None = None,
        dynamic_schedule_service: SimpleOpenDynamicScheduleService | None = None,
        route_assignment_service: SimpleOpenRouteAssignmentService | None = None,
        experience_independence_service: ExperienceIndependenceService | None = None,
        meal_candidate_quality_policy: MealCandidateQualityPolicy | None = None,
        meal_experience_portfolio_policy: MealExperiencePortfolioPolicy | None = None,
        meal_diversity_policy: MealDiversityPolicy | None = None,
    ):
        self.map_poi_service = map_poi_service or MapPoiService()
        self.intent_candidate_semantic_policy = intent_candidate_semantic_policy or IntentCandidateSemanticPolicy()
        self.dynamic_schedule_service = dynamic_schedule_service or SimpleOpenDynamicScheduleService()
        self.route_assignment_service = route_assignment_service or SimpleOpenRouteAssignmentService()
        self.experience_independence_service = experience_independence_service or ExperienceIndependenceService()
        self.meal_candidate_quality_policy = meal_candidate_quality_policy or MealCandidateQualityPolicy()
        self.meal_experience_portfolio_policy = meal_experience_portfolio_policy or MealExperiencePortfolioPolicy()
        self.meal_diversity_policy = meal_diversity_policy or MealDiversityPolicy()

    @staticmethod
    def _guide_hint_intent(intent_type: str) -> str:
        normalized = str(intent_type or "").strip()
        return "meal" if normalized == "food_experience" else normalized

    @classmethod
    def _guide_hints_by_intent(
        cls,
        raw_hints: list[dict[str, Any]] | None,
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str]] = set()
        for raw in raw_hints or []:
            if not isinstance(raw, dict):
                continue
            if (
                str(raw.get("schemaVersion") or "") != "guide-place-hint-v1"
                or str(raw.get("verificationStatus") or "") != "unresolved_amap_grounding"
            ):
                continue
            mention = str(raw.get("mentionText") or "").strip()
            intent_type = cls._guide_hint_intent(str(raw.get("intentType") or ""))
            if not mention or intent_type not in {
                "campus_visit",
                "meal",
                "park",
                "night_view",
                "museum",
                "landmark",
                "area_walk",
                "local_culture",
                "shopping",
                "scenic",
                "rest",
            }:
                continue
            key = (intent_type, mention.casefold())
            if key in seen:
                continue
            seen.add(key)
            grouped.setdefault(intent_type, []).append(copy.deepcopy(raw))
        return grouped

    @staticmethod
    def _normalized_guide_name(value: str) -> str:
        return GuidePoiIdentityService.normalize_name(value)

    @classmethod
    def _guide_hint_matches_selected(cls, hint: dict[str, Any], selected: POI | None) -> bool:
        if selected is None or not cls._is_real_amap_candidate(selected):
            return False
        return bool(GuidePoiIdentityService.candidate_method(hint, selected, selected.city))

    @classmethod
    def _guide_hint_matches_name(cls, hint: dict[str, Any], name: str) -> bool:
        return bool(GuidePoiIdentityService.name_method(hint, name))

    @classmethod
    def _guide_evidence_schedule_constraints(
        cls,
        hint: dict[str, Any] | None,
        *,
        selected: POI | None,
        day_number: int,
        planning_slot_id: str,
        primary_result_count: int,
        guide_match_count: int,
        semantic_rejection: bool,
        duplicate_rejection: bool,
        provider_called: bool | None = None,
        provider_outcome: str | None = None,
        query_text: str = "",
        search_scope: str = "",
        nearby_radius: int | None = None,
        provider_error_type: str = "",
        query_not_executed_reason: str = "",
        cache_hit: bool | None = None,
        candidate_processing_error_type: str = "",
        identity_evidence: dict[str, Any] | None = None,
        identity_ambiguous: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(hint, dict):
            return {}
        explicit_execution = provider_called is not None
        query_succeeded = not explicit_execution or (provider_called is True and provider_outcome == "success")
        matched = bool(
            query_succeeded
            and cls._guide_hint_matches_selected(hint, selected)
            and (identity_evidence is not None or cls._guide_hint_matches_name(hint, selected.name))
            and (not explicit_execution or guide_match_count == 1)
            and (identity_evidence is None or GuidePoiIdentityService.validate_evidence(
                hint=hint, candidate=selected, evidence=identity_evidence,
            ))
        )
        ambiguous = bool(query_succeeded and (int(guide_match_count) > 1 or identity_ambiguous))
        rejection_reason = None
        if not matched or ambiguous:
            rejection_reason = (
                "query_not_executed"
                if explicit_execution and provider_called is not True
                else "provider_failure"
                if explicit_execution and provider_outcome != "success"
                else "candidate_processing_failure"
                if candidate_processing_error_type
                else "ambiguous_amap_match"
                if ambiguous
                else "no_match_in_search_scope"
                if explicit_execution and guide_match_count == 0
                else "already_used"
                if duplicate_rejection
                else "hard_constraint_mismatch"
                if semantic_rejection
                else "hard_constraint_mismatch"
                if explicit_execution
                else "no_amap_match"
            )
        attempt = {
            "schemaVersion": "guide-place-attempt-v1",
            "mentionText": str(hint.get("mentionText") or ""),
            "intentType": cls._guide_hint_intent(str(hint.get("intentType") or "")),
            "sourceRefIds": [str(item) for item in hint.get("sourceRefIds") or [] if str(item)],
            "sourceFingerprints": [str(item) for item in hint.get("sourceFingerprints") or [] if str(item)],
            "guideEvidenceFingerprint": str(hint.get("guideEvidenceFingerprint") or ""),
            "planningSlotId": planning_slot_id,
            "dayNumber": day_number,
            "providerResultCount": max(0, int(primary_result_count)),
            "status": "grounded" if matched and not ambiguous else "rejected",
            "reasonCode": rejection_reason,
        }
        if explicit_execution:
            attempt.update(
                {
                    "providerCalled": provider_called is True,
                    "providerOutcome": str(provider_outcome or "not_called"),
                    "queryText": query_text if provider_called else "",
                    "requestedQueryText": str(hint.get("mentionText") or ""),
                    "searchScope": search_scope if provider_called else "not_executed",
                    "nearbyRadiusMeters": nearby_radius,
                    "guideMatchCount": max(0, int(guide_match_count)),
                    "providerErrorType": provider_error_type or None,
                    "candidateProcessingErrorType": candidate_processing_error_type or None,
                    "queryNotExecutedReason": query_not_executed_reason or None,
                    "cacheHit": cache_hit if provider_called else None,
                }
            )
        if not matched or ambiguous or selected is None:
            return {"guideEvidenceAttempt": attempt}
        amap_poi_id = str(selected.amap_id or selected.id or "").strip()
        return {
            "guideEvidenceAttempt": attempt,
            "guideEvidence": {
                "schemaVersion": "guide-place-evidence-v1",
                "mentionText": attempt["mentionText"],
                "intentType": attempt["intentType"],
                "sourceRefIds": copy.deepcopy(attempt["sourceRefIds"]),
                "sourceFingerprints": copy.deepcopy(attempt["sourceFingerprints"]),
                "guideEvidenceFingerprint": attempt["guideEvidenceFingerprint"],
                **({"sourceDocumentFingerprints": copy.deepcopy(hint["sourceDocumentFingerprints"])}
                   if isinstance(hint.get("sourceDocumentFingerprints"), list) else {}),
                **({"sourceExcerpt": str(hint["sourceExcerpt"])} if "sourceExcerpt" in hint else {}),
                "verificationStatus": "verified_amap_grounding",
                "amapPoiId": amap_poi_id,
                "planningSlotId": planning_slot_id,
                "dayNumber": day_number,
                **({"identityMatch": copy.deepcopy(identity_evidence)} if identity_evidence else {}),
            },
        }

    def build_segment_plans(
        self,
        initial_plan: AgentInitialPlanOutput,
        *,
        city: str,
        transport_mode: str,
        slot_lineage: dict[str, dict[str, Any]] | None = None,
        authoritative_lineage_required: bool = False,
        excluded_physical_aliases: set[str] | frozenset[str] | None = None,
        prior_required_candidates_by_occurrence: dict[str, list[POI] | tuple[POI, ...]] | None = None,
        route_decision_contract: dict[str, Any] | None = None,
        spatial_preference: dict[str, Any] | None = None,
        experience_policies_by_intent: dict[str, dict[str, Any]] | None = None,
        route_budget: int = 8,
        route_plan_id: str = "simple-direction-proposal",
        route_gap_supplement_hints: list[dict[str, Any]] | None = None,
        frontier_assignment: dict[str, Any] | None = None,
        adjacent_scope_claimer: Any = None,
        request_contract_fingerprint: str = "",
        guide_place_hints: list[dict[str, Any]] | None = None,
    ) -> tuple[list[PersistableSegmentPlan], list[dict[str, Any]]]:
        if isinstance(spatial_preference, dict) and str(spatial_preference.get("status") or "") != "resolved":
            raise ValueError("simple_open_spatial_preference_unresolved")
        # This mapping is compiled and sealed by AgentService from the accepted
        # Controller directive.  The provider-authored DaySlot skeleton cannot
        # grant itself a goal/occurrence identity.
        authoritative_lineage = slot_lineage or {}
        pool_by_slot = {slot_id: pool for pool in initial_plan.intent_pools for slot_id in pool.assign_to_slots}
        category_by_intent = {"campus_visit": "campus", "meal": "food", "park": "park"}
        # These aliases come only from persisted proposals rebound by
        # SimpleOpenDirectionService.  Controller/client text may request
        # ``avoidRecentEntities`` but cannot mint the physical exclusion set.
        prior_aliases = {str(alias).strip() for alias in (excluded_physical_aliases or set()) if str(alias).strip()}
        prior_identity_ids: set[str] = {
            alias.removeprefix("amap:").upper()
            for alias in prior_aliases
            if alias.startswith("amap:") and alias.removeprefix("amap:")
        }
        prior_physical_keys: set[str] = {
            alias.removeprefix("physical:")
            for alias in prior_aliases
            if alias.startswith("physical:") and alias.removeprefix("physical:")
        }
        prior_meal_brands = {
            alias.removeprefix("meal-brand:")
            for alias in prior_aliases
            if alias.startswith("meal-brand:") and alias.removeprefix("meal-brand:")
        }
        prior_meal_families = {
            alias.removeprefix("meal-family:")
            for alias in prior_aliases
            if alias.startswith("meal-family:") and alias.removeprefix("meal-family:")
        }
        persisted_required_candidates = {
            str(occurrence_id): [copy.deepcopy(candidate) for candidate in candidates if isinstance(candidate, POI)]
            for occurrence_id, candidates in (prior_required_candidates_by_occurrence or {}).items()
            if str(occurrence_id or "") and isinstance(candidates, (list, tuple))
        }
        used_identity_ids = set(prior_identity_ids)
        used_physical_keys = set(prior_physical_keys)
        used_meal_brands = set(prior_meal_brands)
        used_meal_families = set(prior_meal_families)
        plans: list[PersistableSegmentPlan] = []
        candidates_by_slot: dict[str, list[POI]] = {}
        events: list[dict[str, Any]] = []
        search_ordinal = 0
        candidate_slots = self._ordered_candidate_slots(
            initial_plan.day_slots,
            authoritative_lineage,
        )
        if authoritative_lineage_required:
            authorized_slots = [slot for slot in candidate_slots if str(slot.slot_id or "") in authoritative_lineage]
            # Unlike untrusted model amplification, a server-authorized slot is
            # an accepted occurrence and must never disappear through slicing.
            if len(authorized_slots) > MAX_SIMPLE_OPEN_SLOTS:
                raise ValueError("simple_open_authoritative_slot_budget_exceeded")
            rejected_slots = [slot for slot in candidate_slots if str(slot.slot_id or "") not in authoritative_lineage][
                :MAX_SIMPLE_OPEN_SLOTS
            ]
            events.extend(
                {
                    "type": "simple_open_slot_rejected",
                    "label": "拒绝未授权行程槽位",
                    "status": "failed",
                    "detail": "该 DaySlot 不属于服务端编译的 goal occurrence inventory，未进入地点检索或方案。",
                    "providerName": None,
                    "metadata": {
                        "executionProfile": "simple_open_v1",
                        "stepIndex": index,
                        "slotKey": slot.slot_id,
                        "reasonCode": "simple_open_slot_lineage_missing",
                        "providerCalled": False,
                    },
                }
                for index, slot in enumerate(rejected_slots, start=1)
            )
            candidate_slots = authorized_slots
        sorted_slots = candidate_slots[:MAX_SIMPLE_OPEN_SLOTS]
        # Formal user-goal grounding keeps its established six-call ceiling.
        # A server-sealed daily-completion slot receives exactly one additional
        # bounded call of its own, so adding the product obligation cannot starve
        # a must-go or explicit-every-day meal. The extra allowance is capped
        # independently and exists only when such sealed lineage is present.
        daily_completion_searches = min(
            MAX_SIMPLE_OPEN_DAILY_COMPLETION_SEARCHES,
            sum(
                1
                for slot in sorted_slots
                if authoritative_lineage.get(str(slot.slot_id or ""), {}).get("dayCompletionRequired") is True
            ),
        )
        search_budget = MAX_SIMPLE_OPEN_POI_SEARCHES + daily_completion_searches
        frontier_slots = self._frontier_slots(frontier_assignment)
        frontier_outcomes: list[dict[str, Any]] = []
        slot_frontier_snapshot = self._slot_frontier_snapshot(frontier_assignment)
        slot_query_outcomes: list[dict[str, Any]] = []
        remaining_query_scopes: list[dict[str, Any]] = []
        meal_briefs_by_slot: dict[str, dict[str, Any]] = {}
        meal_query_plans_by_slot: dict[str, dict[str, Any]] = {}
        meal_general_fallback_used = False
        guide_hints_by_intent = self._guide_hints_by_intent(guide_place_hints)
        guide_hint_offsets: dict[str, int] = {}
        guide_hint_by_slot: dict[str, dict[str, Any]] = {}
        slot_execution_inputs: list[tuple[Any, Any, dict[str, Any], str, str, str, bool]] = []
        for slot in sorted_slots:
            pool = pool_by_slot.get(slot.slot_id or "")
            lineage = authoritative_lineage.get(str(slot.slot_id or ""), {})
            intent_type = str(pool.intent_type if pool is not None else slot.kind or "landmark")
            hints = list(pool.candidate_hints) if pool is not None else []
            pool_slot_index = (
                list(pool.assign_to_slots).index(slot.slot_id)
                if pool is not None and slot.slot_id in pool.assign_to_slots
                else 0
            )
            frontier_slot = frontier_slots.get(str(slot.slot_id or ""), {})
            assigned_campus_name = (
                str(frontier_slot.get("canonicalName") or "").strip() if intent_type == "campus_visit" else ""
            )
            normalized_intent = self._guide_hint_intent(intent_type)
            compatible_guide_hints = guide_hints_by_intent.get(normalized_intent, [])
            guide_hint: dict[str, Any] | None = None
            if (
                not assigned_campus_name
                and str(getattr(pool, "entity_binding_mode", "") or "") != "exact_entity"
                and compatible_guide_hints
            ):
                hint_offset = guide_hint_offsets.get(normalized_intent, 0)
                guide_hint = compatible_guide_hints[hint_offset % len(compatible_guide_hints)]
                guide_hint_offsets[normalized_intent] = hint_offset + 1
                guide_hint_by_slot[str(slot.slot_id or "")] = copy.deepcopy(guide_hint)
            experience_policy = self._experience_policy_for_intent(
                experience_policies_by_intent,
                intent_type,
            )
            raw_query = (
                assigned_campus_name
                or str(
                    guide_hint.get("mentionText")
                    if isinstance(guide_hint, dict)
                    else hints[pool_slot_index % len(hints)]
                    if hints
                    else slot.raw_need
                ).strip()
            )
            if intent_type == "meal":
                meal_brief = self.meal_experience_portfolio_policy.brief_for_slot(
                    pool,
                    slot_id=str(slot.slot_id or ""),
                    day_number=int(slot.day_number),
                    raw_need=str(slot.raw_need or ""),
                    city=city,
                    source_fingerprint=request_contract_fingerprint,
                    occurrence_id=str(lineage.get("occurrenceId") or ""),
                )
                provider_types = self._destination_cuisine_provider_types(
                    city=city,
                    experience_policy=experience_policy,
                )
                meal_query_plan = self.meal_experience_portfolio_policy.query_plan(
                    meal_brief,
                    raw_query=raw_query,
                    city=city,
                    provider_types=provider_types,
                    local_food_required=self._experience_policy_requires_local_food(experience_policy),
                )
                meal_briefs_by_slot[str(slot.slot_id or "")] = meal_brief
                meal_query_plans_by_slot[str(slot.slot_id or "")] = meal_query_plan.to_camel_dict()
                if guide_hint is None:
                    raw_query = meal_query_plan.keyword
            query = self._safe_query_for_intent(
                raw_query,
                city=city,
                intent_type=intent_type,
                experience_policy=experience_policy,
                preserve_exact_entity=bool(
                    assigned_campus_name
                    or guide_hint is not None
                    or str(getattr(pool, "entity_binding_mode", "") or "") == "exact_entity"
                ),
            )
            requirement_level = str(
                lineage.get("requirementLevel") or (pool.requirement_level if pool is not None else "optional")
            )
            required = (
                str(lineage.get("requirementLevel") or "") in {"hard", "required"}
                if lineage
                else (pool.requirement_level == "required")
                if pool is not None
                else False
            )
            slot_execution_inputs.append((slot, pool, lineage, intent_type, query, requirement_level, required))

        protected_primary_slot_ids = {
            str(slot.slot_id or "")
            for slot, _pool, lineage, _intent, query, requirement_level, required in slot_execution_inputs
            if query
            and (
                required
                or (
                    authoritative_lineage_required
                    and bool(lineage)
                    and (requirement_level == "explicit_soft" or lineage.get("dayCompletionRequired") is True)
                )
            )
        }
        protected_searches_remaining = len(protected_primary_slot_ids)
        frontier_campus_days = {
            int(slot.day_number)
            for slot, _pool, _lineage, intent_type, _query, _level, _required in slot_execution_inputs
            if intent_type == "campus_visit"
            and str(frontier_slots.get(str(slot.slot_id or ""), {}).get("canonicalName") or "").strip()
        }

        alternative_query_scopes_used: set[str] = set()
        attempted_query_scopes: set[tuple[str, str]] = set()
        alternative_candidate_queues: dict[str, dict[str, Any]] = {}
        nearby_radius = self._strict_low_detour_nearby_radius(route_decision_contract)
        # The first accepted route anchor remains the day's locality boundary.
        # Separately track the last real admitted stop so each subsequent query
        # can recall candidates adjacent to the actual route predecessor without
        # walking the locality boundary forward one radius at a time.
        day_seed_anchor_by_day: dict[int, POI] = {}
        admitted_predecessor_by_day: dict[int, POI] = {}
        admitted_predecessor_beam_by_day: dict[int, list[POI]] = {}
        for slot_ordinal, (slot, pool, lineage, intent_type, query, requirement_level, required) in enumerate(
            slot_execution_inputs,
            start=1,
        ):
            selected: POI | None = None
            warning = ""
            semantic_rejection = False
            duplicate_rejection = False
            primary_cache_hit = False
            primary_result_count = 0
            primary_provider_outcome = "not_called"
            primary_provider_error_type = ""
            guide_query_state: dict[str, Any] = {"providerCalled": False, "providerOutcome": "not_called"}
            guide_hint = guide_hint_by_slot.get(str(slot.slot_id or ""))
            primary_baseline_candidates: list[POI] = []
            primary_admission_diagnostics: list[dict[str, Any]] = []
            alternative_baseline_candidates: list[POI] = []
            frontier_slot = frontier_slots.get(str(slot.slot_id or ""), {})
            experience_policy = self._experience_policy_for_intent(
                experience_policies_by_intent,
                intent_type,
            )
            frontier_campus_assigned = bool(
                intent_type == "campus_visit" and str(frontier_slot.get("canonicalName") or "").strip()
            )
            exact_entity = (
                str(frontier_slot.get("canonicalName") or "").strip()
                if frontier_campus_assigned
                else str(getattr(pool, "exact_entity", "") or "").strip()
            ) or None
            qualification_binding = (
                copy.deepcopy(frontier_slot.get("qualificationBinding"))
                if frontier_campus_assigned and isinstance(frontier_slot.get("qualificationBinding"), dict)
                else None
            )
            day_number = int(slot.day_number)
            day_seed_anchor = day_seed_anchor_by_day.get(day_number)
            admitted_predecessor = admitted_predecessor_by_day.get(day_number) or day_seed_anchor
            predecessor_beam = admitted_predecessor_beam_by_day.get(day_number) or (
                [admitted_predecessor] if admitted_predecessor is not None else []
            )
            fixed_successor = self._known_fixed_successor(
                slot_execution_inputs,
                current_index=slot_ordinal - 1,
                persisted_required_candidates=persisted_required_candidates,
                frontier_slots=frontier_slots,
            )
            adjacent_candidate_scopes = self._adjacent_candidate_scopes(
                day_seed=day_seed_anchor,
                predecessors=predecessor_beam,
                successor=fixed_successor,
            )
            adjacent_candidate_scope = adjacent_candidate_scopes[0] if adjacent_candidate_scopes else None
            blocked_by_missing_frontier_day_seed = bool(
                intent_type != "campus_visit"
                and day_number in frontier_campus_days
                and day_seed_anchor is None
                and nearby_radius
                and str(getattr(pool, "entity_binding_mode", "") or "") != "exact_entity"
            )
            nearby_search_enabled = bool(
                nearby_radius
                and adjacent_candidate_scopes
                and str(getattr(pool, "entity_binding_mode", "") or "") != "exact_entity"
                and callable(getattr(self.map_poi_service, "search_nearby", None))
            )
            adjacent_scope_entries: list[dict[str, Any]] = []
            slot_query: dict[str, Any] = {}
            claimed_scope_fingerprint = self._frontier_scope_fingerprint(frontier_slot.get("queryScopeFingerprint"))
            active_scope_index: int | None = None
            claimed_scope_mismatch = False
            if not frontier_campus_assigned and nearby_search_enabled:
                adjacent_scope_entries = self._adjacent_scope_frontier_entries(
                    adjacent_candidate_scopes,
                    route_decision_contract=route_decision_contract or {},
                    spatial_preference=spatial_preference or {},
                    slot_frontier_snapshot=slot_frontier_snapshot,
                    day_number=day_number,
                    slot_id=str(slot.slot_id or ""),
                    query=query,
                    radius=int(nearby_radius or 0),
                )
                deferred_scope_claim = bool(
                    isinstance(frontier_assignment, dict) and frontier_assignment.get("deferredSlotScopeClaim") is True
                )
                if deferred_scope_claim:
                    if frontier_slot:
                        raise ValueError("simple_direction_current_scope_claim_conflict")
                    if not callable(adjacent_scope_claimer):
                        raise ValueError("simple_direction_current_scope_claimer_missing")
                    current_scope_queue = []
                    for entry in adjacent_scope_entries:
                        scope = entry["scope"]
                        current_scope_queue.append(
                            {
                                "dayNumber": day_number,
                                "slotId": str(slot.slot_id or ""),
                                "daySeedAmapId": str(scope.day_seed.amap_id or scope.day_seed.id or ""),
                                "queryScopeFingerprint": str(entry["queryScopeFingerprint"]),
                                "centerRole": scope.center_strategy,
                                "queryRole": "adjacent_candidate_center",
                                "queryText": query,
                                "priority": int(entry["priority"]),
                                "currentPartialCompletionSlot": bool(
                                    selected is None
                                    and (
                                        required
                                        or lineage.get("completionRequired") is True
                                        or lineage.get("offerCompletionPriority") is True
                                    )
                                ),
                                "predecessorAmapId": str(scope.predecessor.amap_id or scope.predecessor.id or ""),
                                "successorAmapId": (
                                    str(scope.successor.amap_id or scope.successor.id or "")
                                    if scope.successor is not None
                                    else None
                                ),
                                "predecessorBeamRank": int(scope.predecessor_beam_rank),
                                "isActiveScope": False,
                                "attemptedThisTurn": False,
                                "providerOutcome": "not_called",
                                "remainingReason": "not_executed",
                            }
                        )
                    claimed_query = adjacent_scope_claimer(copy.deepcopy(current_scope_queue))
                    claimed_query = copy.deepcopy(claimed_query) if isinstance(claimed_query, dict) else {}
                    claimed_fingerprint = self._frontier_scope_fingerprint(claimed_query.get("queryScopeFingerprint"))
                    try:
                        claimed_page = int(claimed_query.get("page"))
                        claimed_offset = int(claimed_query.get("offset"))
                    except (TypeError, ValueError):
                        claimed_page = 0
                        claimed_offset = 0
                    valid_scope_fingerprints = {str(entry["queryScopeFingerprint"]) for entry in adjacent_scope_entries}
                    if (
                        str(claimed_query.get("slotId") or "") != str(slot.slot_id or "")
                        or str(claimed_query.get("daySeedAmapId") or "").strip().upper()
                        not in {
                            str(entry["scope"].day_seed.amap_id or entry["scope"].day_seed.id or "").strip().upper()
                            for entry in adjacent_scope_entries
                        }
                        or claimed_fingerprint not in valid_scope_fingerprints
                        or not str(claimed_query.get("slotFrontierKey") or "")
                        or not str(claimed_query.get("queryFingerprint") or "")
                        or claimed_query.get("exhausted") is True
                        or claimed_page <= 0
                        or claimed_offset <= 0
                    ):
                        raise ValueError("simple_direction_current_scope_claim_identity_mismatch")
                    frontier_slot = claimed_query
                    frontier_slots[str(slot.slot_id or "")] = copy.deepcopy(claimed_query)
                    claimed_scope_fingerprint = claimed_fingerprint
                if claimed_scope_fingerprint:
                    active_scope_index = next(
                        (
                            index
                            for index, entry in enumerate(adjacent_scope_entries)
                            if entry["queryScopeFingerprint"] == claimed_scope_fingerprint
                        ),
                        None,
                    )
                    claimed_scope_mismatch = active_scope_index is None
                if active_scope_index is None and not claimed_scope_mismatch:
                    available_scope_indexes = [
                        index
                        for index, entry in enumerate(adjacent_scope_entries)
                        if not bool((entry.get("query") or {}).get("exhausted") is True)
                    ]
                    if available_scope_indexes:
                        active_scope_index = min(
                            available_scope_indexes,
                            key=lambda index: (
                                0
                                if self._positive_frontier_int(
                                    (adjacent_scope_entries[index].get("query") or {}).get("page"),
                                    default=1,
                                    maximum=100,
                                )
                                == 1
                                else 1,
                                self._positive_frontier_int(
                                    (adjacent_scope_entries[index].get("query") or {}).get("page"),
                                    default=1,
                                    maximum=100,
                                ),
                                int(adjacent_scope_entries[index]["priority"]),
                            ),
                        )
                if active_scope_index is not None:
                    active_entry = adjacent_scope_entries[active_scope_index]
                    adjacent_candidate_scope = active_entry["scope"]
                    entry_query = active_entry.get("query") if isinstance(active_entry.get("query"), dict) else {}
                    if claimed_scope_fingerprint == active_entry["queryScopeFingerprint"] and frontier_slot:
                        slot_query = copy.deepcopy(frontier_slot)
                    elif entry_query:
                        slot_query = copy.deepcopy(entry_query)
                    if slot_query:
                        frontier_slot = {**frontier_slot, **copy.deepcopy(slot_query)}
                        frontier_slots[str(slot.slot_id or "")] = frontier_slot
            slot_frontier_exhausted = bool(
                claimed_scope_mismatch
                or (
                    adjacent_scope_entries
                    and all(
                        bool((entry.get("query") or {}).get("exhausted") is True) for entry in adjacent_scope_entries
                    )
                )
            )
            search_page = self._positive_frontier_int(frontier_slot.get("page"), default=1, maximum=100)
            search_offset = self._positive_frontier_int(frontier_slot.get("offset"), default=5, maximum=25)
            frontier_scope_fingerprint = (
                str(adjacent_scope_entries[active_scope_index]["queryScopeFingerprint"])
                if active_scope_index is not None
                else claimed_scope_fingerprint
            )
            provider_scope_fingerprint = (
                self._nearby_query_scope_fingerprint(
                    route_decision_contract or {},
                    spatial_preference=spatial_preference or {},
                    slot_id=str(slot.slot_id or ""),
                    day_number=day_number,
                    query=query,
                    scope=adjacent_candidate_scope,
                    radius=int(nearby_radius or 0),
                    page=search_page,
                )
                if nearby_search_enabled and adjacent_candidate_scope is not None
                else frontier_scope_fingerprint
            )
            remaining_search_budget = search_budget - search_ordinal
            primary_search_protected = str(slot.slot_id or "") in protected_primary_slot_ids
            may_use_search_budget = primary_search_protected or remaining_search_budget > protected_searches_remaining

            # A citywide candidate pool cannot satisfy a strict nearby scope.
            # Replacing the Provider call is the actual locality guard; cache
            # reuse from another anchor would silently bypass it.
            queued = (
                None
                if nearby_search_enabled
                or frontier_campus_assigned
                or blocked_by_missing_frontier_day_seed
                or guide_hint
                else alternative_candidate_queues.get(intent_type)
            )
            if isinstance(queued, dict):
                candidates = queued.get("candidates") if isinstance(queued.get("candidates"), list) else []
                candidates = sorted(
                    candidates,
                    key=lambda candidate: self._spatial_candidate_rank(
                        candidate,
                        spatial_preference or {},
                    ),
                )
                queued_for_assignment = list(candidates)
                while candidates and selected is None:
                    candidate = candidates.pop(0)
                    if not self._spatial_candidate_allowed(
                        candidate,
                        spatial_preference or {},
                    ):
                        semantic_rejection = True
                        continue
                    if not self._candidate_admitted_for_occurrence(
                        candidate,
                        city=city,
                        intent_type=intent_type,
                        raw_need=str(slot.raw_need or ""),
                        exact_entity=exact_entity,
                        optional_experience_family=str(getattr(pool, "optional_experience_family", "") or ""),
                        qualification_binding=qualification_binding,
                        experience_policy=experience_policy,
                        meal_experience_brief=meal_briefs_by_slot.get(str(slot.slot_id or "")),
                        used_meal_brands=used_meal_brands,
                        used_meal_families=used_meal_families,
                        trip_date=slot.date,
                        duration_minutes=slot.duration_minutes,
                        schedule_preference=lineage.get("schedulePreference") or {},
                        day_anchor=day_seed_anchor,
                    ):
                        semantic_rejection = True
                        continue
                    if self._reserve_selected_candidate(candidate, used_identity_ids, used_physical_keys):
                        selected = candidate
                    else:
                        duplicate_rejection = True
                if selected is not None:
                    candidates_by_slot[str(slot.slot_id or "")] = [selected, *queued_for_assignment]
                    pool_event_time = datetime.now(timezone.utc).isoformat()
                    events.append(
                        {
                            "type": "simple_open_candidate_pool_reused",
                            "label": "复用方向内已准入候选",
                            "status": "completed",
                            "detail": "未增加 Provider 调用；按当前 occurrence 重新准入后绑定剩余真实候选。",
                            "providerName": None,
                            "startedAt": pool_event_time,
                            "finishedAt": pool_event_time,
                            "timestamp": pool_event_time,
                            "durationMs": 0,
                            "metadata": {
                                "executionProfile": "simple_open_v1",
                                "stepIndex": slot_ordinal * 2 + 1,
                                "slotKey": slot.slot_id,
                                "queryRole": str(queued.get("queryRole") or "candidate_pool_reuse"),
                                "queryFingerprint": queued.get("queryFingerprint"),
                                "providerCalled": False,
                                "remainingSearchBudget": search_budget - search_ordinal,
                                "selectedAmapId": selected.amap_id,
                                "remainingCandidateCount": len(candidates),
                            },
                        }
                    )

            original_query_fingerprint = self._query_fingerprint(query)
            original_query_scope = (intent_type, original_query_fingerprint)
            budget_preserving_query = ""
            if (
                selected is None
                and query
                and primary_search_protected
                and remaining_search_budget <= protected_searches_remaining
                and original_query_scope in attempted_query_scopes
                and not nearby_search_enabled
                and not frontier_campus_assigned
                and guide_hint is None
                and not frontier_scope_fingerprint
                and not blocked_by_missing_frontier_day_seed
                and str(getattr(pool, "entity_binding_mode", "") or "") != "exact_entity"
            ):
                candidate_query = self._safe_alternative_query_for_intent(
                    city=city,
                    intent_type=intent_type,
                    primary_query=query,
                )
                candidate_fingerprint = self._query_fingerprint(candidate_query) if candidate_query else ""
                candidate_scope = (intent_type, candidate_fingerprint)
                if candidate_fingerprint and candidate_scope not in attempted_query_scopes:
                    budget_preserving_query = candidate_query
                    query = candidate_query

            if (
                selected is None
                and query
                and not blocked_by_missing_frontier_day_seed
                and not slot_frontier_exhausted
                and search_ordinal < search_budget
                and may_use_search_budget
            ):
                search_ordinal += 1
                search_started = datetime.now(timezone.utc)
                search_started_perf = time.perf_counter()
                tool_event = self._tool_call_event(
                    slot.slot_id,
                    query,
                    search_ordinal,
                    search_budget,
                    step_index=slot_ordinal * 2 + 1,
                )
                attempted_query_scopes.add((intent_type, self._query_fingerprint(query)))
                if budget_preserving_query:
                    tool_event["metadata"].update(
                        {
                            "queryRole": "budget_preserving_primary_alternative",
                            "originalQueryFingerprint": original_query_fingerprint,
                            "protectedPrimarySearchesRemaining": protected_searches_remaining,
                        }
                    )
                tool_event["metadata"].update(
                    {
                        "priorDirectionExclusionApplied": bool(prior_aliases),
                        "priorDirectionPhysicalAliasCount": len(prior_aliases),
                        "page": search_page,
                        "offset": search_offset,
                        "evidenceEntityFingerprint": str(frontier_slot.get("evidenceEntityFingerprint") or "") or None,
                    }
                )
                if intent_type == "meal":
                    tool_event["metadata"]["mealQueryPlan"] = copy.deepcopy(
                        meal_query_plans_by_slot.get(str(slot.slot_id or "")) or {}
                    )
                if nearby_search_enabled and adjacent_candidate_scope is not None:
                    tool_event["label"] = "调用高德周边地点搜索"
                    tool_event["detail"] = (
                        f"{query}（以{self._adjacent_scope_center_label(adjacent_candidate_scope)}为中心，"
                        f"{int(nearby_radius or 0)} 米范围）"
                    )
                    tool_event["metadata"].update(
                        {
                            "searchScope": "nearby_low_detour",
                            "anchorAmapId": str(
                                adjacent_candidate_scope.predecessor.amap_id
                                or adjacent_candidate_scope.predecessor.id
                                or ""
                            ),
                            "daySeedAmapId": str(
                                adjacent_candidate_scope.day_seed.amap_id or adjacent_candidate_scope.day_seed.id or ""
                            ),
                            "predecessorAmapId": str(
                                adjacent_candidate_scope.predecessor.amap_id
                                or adjacent_candidate_scope.predecessor.id
                                or ""
                            ),
                            "successorAmapId": (
                                str(
                                    adjacent_candidate_scope.successor.amap_id
                                    or adjacent_candidate_scope.successor.id
                                    or ""
                                )
                                if adjacent_candidate_scope.successor is not None
                                else None
                            ),
                            "searchCenterStrategy": adjacent_candidate_scope.center_strategy,
                            "radiusMeters": int(nearby_radius or 0),
                            "queryScopeFingerprint": provider_scope_fingerprint,
                            "distanceLimitIsRouteEvidence": False,
                        }
                    )
                elif frontier_campus_assigned and str(frontier_slot.get("priorCanonicalAmapId") or "").strip():
                    tool_event["label"] = "调用高德地点身份核验"
                    tool_event["detail"] = "按服务端冻结的校园 AMap 身份复核，不重新选择其他校区或分支。"
                    tool_event["metadata"].update(
                        {
                            "searchScope": "canonical_amap_detail",
                            "frozenAmapId": str(frontier_slot.get("priorCanonicalAmapId") or "").strip().upper(),
                            "campusIdentityMayChange": False,
                        }
                    )
                events.append(tool_event)
                try:
                    (
                        search,
                        selected,
                        duplicate_rejection,
                        semantic_rejection,
                        _primary_remaining,
                        primary_baseline_candidates,
                        primary_admission_diagnostics,
                    ) = self._search_candidate(
                        city=city,
                        query=query,
                        category=category_by_intent.get(intent_type, "all"),
                        intent_type=intent_type,
                        raw_need=str(slot.raw_need or ""),
                        exact_entity=exact_entity,
                        optional_experience_family=str(getattr(pool, "optional_experience_family", "") or ""),
                        used_identity_ids=used_identity_ids,
                        used_physical_keys=used_physical_keys,
                        trip_date=slot.date,
                        duration_minutes=slot.duration_minutes,
                        schedule_preference=lineage.get("schedulePreference") or {},
                        adjacent_scope=adjacent_candidate_scope if nearby_search_enabled else None,
                        day_anchor=day_seed_anchor,
                        nearby_radius=int(nearby_radius or 0) if nearby_search_enabled else None,
                        query_scope_fingerprint=provider_scope_fingerprint,
                        page=search_page,
                        offset=search_offset,
                        spatial_preference=spatial_preference or {},
                        required_amap_id=(
                            str(frontier_slot.get("priorCanonicalAmapId") or "").strip().upper()
                            if frontier_campus_assigned
                            else None
                        ),
                        qualification_binding=qualification_binding,
                        experience_policy=experience_policy,
                        meal_experience_brief=meal_briefs_by_slot.get(str(slot.slot_id or "")),
                        used_meal_brands=used_meal_brands,
                        used_meal_families=used_meal_families,
                        guide_hint=guide_hint,
                        guide_query_state=guide_query_state,
                    )
                    primary_provider_outcome = "success"
                    primary_cache_hit = bool(search.cache_hit)
                    primary_result_count = len(search.pois)
                    primary_rejection_reason_counts = self._admission_rejection_reason_counts(
                        primary_admission_diagnostics
                    )
                    tool_event["status"] = "completed"
                    tool_event["metadata"].update(
                        {
                            "providerOutcome": "success",
                            "cacheHit": primary_cache_hit,
                            "resultCount": primary_result_count,
                            "experienceIndependenceRejections": primary_admission_diagnostics,
                            "candidateAdmissionRejectionReasonCounts": primary_rejection_reason_counts,
                        }
                    )
                    tool_event["metadata"]["selectedAmapId"] = selected.amap_id if selected is not None else None
                    warning = (
                        self._semantic_rejection_warning(intent_type, primary_rejection_reason_counts)
                        if selected is None and semantic_rejection
                        else "未找到可安全绑定的高德地点"
                        if selected is None
                        else ""
                    )
                    if selected is not None and _primary_remaining:
                        alternative_candidate_queues[intent_type] = {
                            "queryRole": "primary_candidate_pool_reuse",
                            "queryFingerprint": self._query_fingerprint(query),
                            "candidates": _primary_remaining,
                        }
                    if selected is not None:
                        candidates_by_slot[str(slot.slot_id or "")] = [selected, *_primary_remaining]
                except Exception as error:
                    if guide_query_state.get("providerCalled") is True:
                        if guide_query_state.get("providerOutcome") == "success":
                            guide_query_state["candidateProcessingErrorType"] = type(error).__name__
                        else:
                            guide_query_state.update(providerOutcome="failure", providerErrorType=type(error).__name__)
                    primary_provider_outcome = "failure"
                    primary_provider_error_type = type(error).__name__
                    warning = f"高德地点查询失败：{type(error).__name__}"
                    tool_event["status"] = "failed"
                    tool_event["metadata"].update(
                        {
                            "providerOutcome": "failure",
                            "errorType": type(error).__name__,
                            "resultCount": 0,
                            "selectedAmapId": None,
                        }
                    )
                    if guide_hint is not None and guide_query_state.get("providerOutcome") == "success":
                        tool_event["metadata"].update(
                            {
                                "providerOutcome": "success",
                                "resultCount": int(guide_query_state.get("providerResultCount") or 0),
                                "candidateProcessingErrorType": type(error).__name__,
                            }
                        )
                search_finished = datetime.now(timezone.utc)
                tool_event.update(
                    {
                        "startedAt": search_started.isoformat(),
                        "finishedAt": search_finished.isoformat(),
                        "timestamp": search_finished.isoformat(),
                        "durationMs": max(0, int((time.perf_counter() - search_started_perf) * 1000)),
                    }
                )
                later_protected_searches = max(
                    0,
                    protected_searches_remaining - (1 if primary_search_protected else 0),
                )
                if (
                    selected is None
                    and intent_type == "meal"
                    and guide_hint is None
                    and primary_provider_outcome == "success"
                    and self._experience_policy_requires_local_food(experience_policy)
                    and not meal_general_fallback_used
                    and not blocked_by_missing_frontier_day_seed
                    and not slot_frontier_exhausted
                    and search_ordinal < search_budget
                    and (search_budget - search_ordinal) > later_protected_searches
                ):
                    meal_general_fallback_used = True
                    search_ordinal += 1
                    fallback_plan = self.meal_experience_portfolio_policy.query_plan(
                        meal_briefs_by_slot.get(str(slot.slot_id or ""), {}),
                        raw_query=query,
                        city=city,
                        provider_types=self._destination_cuisine_provider_types(
                            city=city,
                            experience_policy=experience_policy,
                        ),
                        local_food_required=True,
                        general_food_fallback=True,
                    )
                    fallback_event = self._tool_call_event(
                        slot.slot_id,
                        query,
                        search_ordinal,
                        search_budget,
                        step_index=slot_ordinal * 2 + 1,
                    )
                    fallback_event["label"] = "调用高德餐饮主题受限回退搜索"
                    fallback_event["metadata"].update(
                        {
                            "queryRole": "meal_general_food_shared_fallback",
                            "mealQueryPlan": fallback_plan.to_camel_dict(),
                            "fallbackOrdinalWithinDirection": 1,
                            "concreteKeywordPreserved": True,
                        }
                    )
                    events.append(fallback_event)
                    fallback_scope_fingerprint = hashlib.sha256(
                        f"{provider_scope_fingerprint or ''}|meal_general_food_fallback".encode("utf-8")
                    ).hexdigest()
                    fallback_started = datetime.now(timezone.utc)
                    fallback_perf = time.perf_counter()
                    try:
                        (
                            fallback_search,
                            selected,
                            _fallback_duplicate,
                            fallback_semantic,
                            fallback_remaining,
                            _fallback_baseline,
                            fallback_diagnostics,
                        ) = self._search_candidate(
                            city=city,
                            query=query,
                            category=category_by_intent.get(intent_type, "all"),
                            intent_type=intent_type,
                            raw_need=str(slot.raw_need or ""),
                            exact_entity=exact_entity,
                            optional_experience_family=str(getattr(pool, "optional_experience_family", "") or ""),
                            used_identity_ids=used_identity_ids,
                            used_physical_keys=used_physical_keys,
                            experience_policy=experience_policy,
                            trip_date=slot.date,
                            duration_minutes=slot.duration_minutes,
                            schedule_preference=lineage.get("schedulePreference") or {},
                            adjacent_scope=adjacent_candidate_scope if nearby_search_enabled else None,
                            day_anchor=day_seed_anchor,
                            nearby_radius=int(nearby_radius or 0) if nearby_search_enabled else None,
                            query_scope_fingerprint=fallback_scope_fingerprint,
                            spatial_preference=spatial_preference or {},
                            meal_experience_brief=meal_briefs_by_slot.get(str(slot.slot_id or "")),
                            used_meal_brands=used_meal_brands,
                            used_meal_families=used_meal_families,
                            general_food_fallback=True,
                        )
                        fallback_event["status"] = "completed"
                        fallback_event["metadata"].update(
                            {
                                "providerOutcome": "success",
                                "cacheHit": bool(fallback_search.cache_hit),
                                "resultCount": len(fallback_search.pois),
                                "selectedAmapId": selected.amap_id if selected is not None else None,
                                "candidateAdmissionRejectionReasonCounts": (
                                    self._admission_rejection_reason_counts(fallback_diagnostics)
                                ),
                            }
                        )
                        if selected is not None:
                            warning = ""
                            candidates_by_slot[str(slot.slot_id or "")] = [selected, *fallback_remaining]
                        elif fallback_semantic:
                            warning = self._semantic_rejection_warning(
                                intent_type,
                                self._admission_rejection_reason_counts(fallback_diagnostics),
                            )
                    except Exception as error:
                        fallback_event["status"] = "failed"
                        fallback_event["metadata"].update(
                            {
                                "providerOutcome": "failure",
                                "errorType": type(error).__name__,
                                "resultCount": 0,
                                "selectedAmapId": None,
                            }
                        )
                    fallback_finished = datetime.now(timezone.utc)
                    fallback_event.update(
                        {
                            "startedAt": fallback_started.isoformat(),
                            "finishedAt": fallback_finished.isoformat(),
                            "timestamp": fallback_finished.isoformat(),
                            "durationMs": max(0, int((time.perf_counter() - fallback_perf) * 1000)),
                        }
                    )
                daily_anchor_missing = bool(
                    slot.route_anchor
                    and not any(
                        plan.day_number == slot.day_number and plan.route_anchor and plan.selected_poi is not None
                        for plan in plans
                    )
                )
                current_required_night_anchor_unresolved = bool(
                    required and slot.route_anchor and intent_type == "night_view"
                )
                primary_candidates_exhausted_or_rejected = bool(
                    primary_provider_outcome == "success"
                    and (intent_type != "night_view" or required)
                    and (
                        (primary_result_count > 0 and (duplicate_rejection or semantic_rejection))
                        or (primary_result_count == 0 and current_required_night_anchor_unresolved)
                    )
                )
                alternative_query = self._safe_alternative_query_for_intent(
                    city=city,
                    intent_type=intent_type,
                    primary_query=query,
                )
                alternative_nearby_scope_fingerprint = (
                    self._nearby_query_scope_fingerprint(
                        route_decision_contract or {},
                        spatial_preference=spatial_preference or {},
                        slot_id=str(slot.slot_id or ""),
                        day_number=day_number,
                        query=alternative_query,
                        scope=adjacent_candidate_scope,
                        radius=int(nearby_radius or 0),
                        page=1,
                    )
                    if alternative_query and nearby_search_enabled and adjacent_candidate_scope is not None
                    else None
                )
                alternative_query_scope = (
                    str(lineage.get("occurrenceId") or slot.slot_id or f"slot:{slot_ordinal}")
                    if required and slot.route_anchor
                    else "legacy_non_hard_scope"
                )
                if (
                    selected is None
                    and primary_candidates_exhausted_or_rejected
                    and (daily_anchor_missing or current_required_night_anchor_unresolved)
                    and alternative_query_scope not in alternative_query_scopes_used
                    and alternative_query
                    and alternative_query != query
                    and (not nearby_search_enabled or current_required_night_anchor_unresolved)
                    and not frontier_campus_assigned
                    and guide_hint is None
                    and search_ordinal < search_budget
                    and (search_budget - search_ordinal) > later_protected_searches
                ):
                    alternative_query_scopes_used.add(alternative_query_scope)
                    search_ordinal += 1
                    attempted_query_scopes.add((intent_type, self._query_fingerprint(alternative_query)))
                    alternative_event = self._tool_call_event(
                        slot.slot_id,
                        alternative_query,
                        search_ordinal,
                        search_budget,
                        step_index=slot_ordinal * 2 + 1,
                    )
                    alternative_event["metadata"].update(
                        {
                            "queryRole": "safe_alternative",
                            "primaryQueryFingerprint": self._query_fingerprint(query),
                            "priorDirectionExclusionApplied": bool(prior_aliases),
                            "priorDirectionPhysicalAliasCount": len(prior_aliases),
                        }
                    )
                    if (
                        nearby_search_enabled
                        and adjacent_candidate_scope is not None
                        and alternative_nearby_scope_fingerprint
                    ):
                        alternative_event["label"] = "调用高德附近地点搜索"
                        alternative_event["detail"] = (
                            f"{alternative_query}（以{self._adjacent_scope_center_label(adjacent_candidate_scope)}为中心，"
                            f"{int(nearby_radius or 0)} 米范围）"
                        )
                        alternative_event["metadata"].update(
                            {
                                "searchScope": "nearby_low_detour",
                                "anchorAmapId": str(
                                    adjacent_candidate_scope.predecessor.amap_id
                                    or adjacent_candidate_scope.predecessor.id
                                    or ""
                                ),
                                "daySeedAmapId": str(
                                    adjacent_candidate_scope.day_seed.amap_id
                                    or adjacent_candidate_scope.day_seed.id
                                    or ""
                                ),
                                "predecessorAmapId": str(
                                    adjacent_candidate_scope.predecessor.amap_id
                                    or adjacent_candidate_scope.predecessor.id
                                    or ""
                                ),
                                "successorAmapId": (
                                    str(
                                        adjacent_candidate_scope.successor.amap_id
                                        or adjacent_candidate_scope.successor.id
                                        or ""
                                    )
                                    if adjacent_candidate_scope.successor is not None
                                    else None
                                ),
                                "searchCenterStrategy": adjacent_candidate_scope.center_strategy,
                                "radiusMeters": int(nearby_radius or 0),
                                "queryScopeFingerprint": alternative_nearby_scope_fingerprint,
                                "distanceLimitIsRouteEvidence": False,
                            }
                        )
                    events.append(alternative_event)
                    alternative_started = datetime.now(timezone.utc)
                    alternative_perf = time.perf_counter()
                    try:
                        (
                            alternative_search,
                            selected,
                            _alt_duplicate,
                            alt_semantic,
                            alternative_remaining,
                            alternative_baseline_candidates,
                            alternative_admission_diagnostics,
                        ) = self._search_candidate(
                            city=city,
                            query=alternative_query,
                            category=category_by_intent.get(intent_type, "all"),
                            intent_type=intent_type,
                            raw_need=str(slot.raw_need or ""),
                            exact_entity=exact_entity,
                            optional_experience_family=str(getattr(pool, "optional_experience_family", "") or ""),
                            qualification_binding=qualification_binding,
                            experience_policy=experience_policy,
                            meal_experience_brief=meal_briefs_by_slot.get(str(slot.slot_id or "")),
                            used_meal_brands=used_meal_brands,
                            used_meal_families=used_meal_families,
                            used_identity_ids=used_identity_ids,
                            used_physical_keys=used_physical_keys,
                            trip_date=slot.date,
                            duration_minutes=slot.duration_minutes,
                            schedule_preference=lineage.get("schedulePreference") or {},
                            adjacent_scope=adjacent_candidate_scope if nearby_search_enabled else None,
                            day_anchor=day_seed_anchor,
                            nearby_radius=int(nearby_radius or 0) if nearby_search_enabled else None,
                            query_scope_fingerprint=alternative_nearby_scope_fingerprint,
                            spatial_preference=spatial_preference or {},
                        )
                        alternative_event["status"] = "completed"
                        alternative_rejection_reason_counts = self._admission_rejection_reason_counts(
                            alternative_admission_diagnostics
                        )
                        alternative_event["metadata"].update(
                            {
                                "providerOutcome": "success",
                                "cacheHit": bool(alternative_search.cache_hit),
                                "resultCount": len(alternative_search.pois),
                                "selectedAmapId": selected.amap_id if selected is not None else None,
                                "experienceIndependenceRejections": alternative_admission_diagnostics,
                                "candidateAdmissionRejectionReasonCounts": alternative_rejection_reason_counts,
                            }
                        )
                        if selected is not None:
                            warning = ""
                            candidates_by_slot[str(slot.slot_id or "")] = [selected, *alternative_remaining]
                            if alternative_remaining:
                                alternative_candidate_queues[intent_type] = {
                                    "queryRole": "safe_alternative_pool_reuse",
                                    "queryFingerprint": self._query_fingerprint(alternative_query),
                                    "candidates": alternative_remaining,
                                }
                        else:
                            intent_label = self._intent_display_label(intent_type)
                            warning = (
                                self._semantic_rejection_warning(intent_type, alternative_rejection_reason_counts)
                                if alt_semantic
                                else (
                                    f"第 {slot.day_number} 天的{intent_label}候选因重复或语义冲突全部被拒绝，"
                                    "安全替代查询后仍未找到合格地点"
                                )
                            )
                    except Exception as error:
                        intent_label = self._intent_display_label(intent_type)
                        warning = (
                            f"第 {slot.day_number} 天的{intent_label}缓存候选均不可用，"
                            f"安全替代查询失败：{type(error).__name__}"
                        )
                        alternative_event["status"] = "failed"
                        alternative_event["metadata"].update(
                            {
                                "providerOutcome": "failure",
                                "errorType": type(error).__name__,
                                "resultCount": 0,
                                "selectedAmapId": None,
                            }
                        )
                    alternative_finished = datetime.now(timezone.utc)
                    alternative_event.update(
                        {
                            "startedAt": alternative_started.isoformat(),
                            "finishedAt": alternative_finished.isoformat(),
                            "timestamp": alternative_finished.isoformat(),
                            "durationMs": max(0, int((time.perf_counter() - alternative_perf) * 1000)),
                        }
                    )
            elif selected is None and query and blocked_by_missing_frontier_day_seed:
                warning = "当天高校锚点尚未通过高德身份核验；该时间槽未发起地点查询，保持待补充"
            elif selected is None and query and claimed_scope_mismatch:
                warning = "已领取的相邻候选查询范围与当前真实前后序地点不一致；未改用其他范围，保持待补充"
            elif selected is None and query:
                warning = (
                    "该日锚下的地点候选页已按当前执行上限检查完毕，该时间槽保持待补充"
                    if slot_frontier_exhausted
                    else "本轮剩余高德地点查询预算已为必选目标保留，该可选时间槽保持待补充"
                    if not required and search_ordinal < search_budget
                    else "本轮高德地点查询预算已用尽，该时间槽保持待补充"
                )
            else:
                warning = "该时间槽缺少可执行的地点查询"

            # Novelty is subordinate to hard-goal completeness.  When every
            # newly admitted candidate for a required category slot is
            # exhausted, a previously visible canonical identity may be reused
            # for that hard occurrence.  The proposal-level novelty verifier
            # must still prove material changes in other replaceable slots.
            # Exact-entity slots keep their existing identity-specific scope.
            if (
                selected is None
                and required
                and not blocked_by_missing_frontier_day_seed
                and not claimed_scope_mismatch
                and not frontier_campus_assigned
                and str(getattr(pool, "entity_binding_mode", "") or "") != "exact_entity"
            ):
                occurrence_id = str(lineage.get("occurrenceId") or "")
                fallback_candidates = [
                    *primary_baseline_candidates,
                    *alternative_baseline_candidates,
                    *persisted_required_candidates.get(occurrence_id, []),
                ]
                if adjacent_candidate_scope is not None and nearby_radius is not None:
                    fallback_candidates = [
                        candidate
                        for candidate in fallback_candidates
                        if self._candidate_within_adjacent_scope(
                            candidate,
                            adjacent_candidate_scope,
                            int(nearby_radius),
                        )
                    ]
                selected = self._required_prior_candidate_fallback(
                    fallback_candidates,
                    prior_identity_ids=prior_identity_ids,
                    prior_physical_keys=prior_physical_keys,
                    current_plans=plans,
                )
                if selected is not None:
                    warning = ""
                    candidates_by_slot[str(slot.slot_id or "")] = [selected]
                    reuse_time = datetime.now(timezone.utc).isoformat()
                    events.append(
                        {
                            "type": "simple_open_required_identity_reused",
                            "label": "保留必选地点完整性",
                            "status": "completed",
                            "detail": "新候选前沿耗尽；复用同根已核验实体，方案差异仍由其余可替换地点单独校验。",
                            "providerName": None,
                            "startedAt": reuse_time,
                            "finishedAt": reuse_time,
                            "timestamp": reuse_time,
                            "durationMs": 0,
                            "metadata": {
                                "executionProfile": "simple_open_v1",
                                "stepIndex": slot_ordinal * 2 + 1,
                                "slotKey": slot.slot_id,
                                "selectedAmapId": selected.amap_id,
                                "priorDirectionExclusionApplied": bool(prior_aliases),
                                "hardRequirementPreserved": True,
                                "noveltySatisfiedByThisReuse": False,
                            },
                        }
                    )
            if frontier_campus_assigned:
                frontier_outcome = {
                    "slotId": str(slot.slot_id or ""),
                    "evidenceEntityFingerprint": str(frontier_slot.get("evidenceEntityFingerprint") or ""),
                    "providerOutcome": (
                        "success"
                        if selected is not None
                        else "failure"
                        if primary_provider_outcome == "failure"
                        else "rejected"
                    ),
                    "selectedAmapId": str(selected.amap_id or "") if selected is not None else None,
                    "queryFingerprint": str(frontier_slot.get("queryFingerprint") or "")
                    or self._query_fingerprint(query),
                    "page": search_page,
                    "reasonCode": (
                        None
                        if selected is not None
                        else primary_provider_error_type or "campus_assignment_candidate_rejected"
                    ),
                }
                if selected is None and primary_provider_outcome != "failure":
                    frontier_outcome["rejectionReasonCodes"] = sorted(
                        self._admission_rejection_reason_counts(primary_admission_diagnostics)
                    ) or ["candidate_admission_rejected"]
                frontier_outcomes.append(frontier_outcome)
            if slot_query and primary_provider_outcome in {"success", "failure"}:
                admitted_groups = sorted(
                    {
                        self._physical_candidate_key(candidate)
                        for candidate in primary_baseline_candidates
                        if self._physical_candidate_key(candidate)
                    }
                )
                rejected_groups: list[dict[str, Any]] = []
                for diagnostic in primary_admission_diagnostics:
                    if not isinstance(diagnostic, dict):
                        continue
                    physical_group_id = str(diagnostic.get("physicalGroupId") or "")
                    reason_codes = [str(item) for item in diagnostic.get("reasonCodes") or [] if str(item)]
                    if physical_group_id:
                        rejected_groups.append(
                            {
                                "physicalGroupId": physical_group_id,
                                "reasonCode": reason_codes[0] if reason_codes else "independence_not_verified",
                            }
                        )
                slot_query_outcomes.append(
                    {
                        "query": copy.deepcopy(slot_query),
                        "providerOutcome": primary_provider_outcome,
                        "admittedPhysicalGroups": admitted_groups,
                        "rejectedPhysicalGroups": rejected_groups,
                        "selectedAmapId": str(selected.amap_id or "") if selected is not None else None,
                        "reasonCode": primary_provider_error_type or None,
                    }
                )
            if adjacent_scope_entries:
                active_scope_executed = bool(
                    active_scope_index is not None and primary_provider_outcome == "success" and selected is not None
                )
                for scope_index, entry in enumerate(adjacent_scope_entries):
                    query_state = entry.get("query") if isinstance(entry.get("query"), dict) else {}
                    if query_state.get("exhausted") is True:
                        continue
                    if active_scope_executed and scope_index == active_scope_index:
                        continue
                    scope = entry["scope"]
                    is_active_scope = scope_index == active_scope_index
                    attempted_this_turn = bool(is_active_scope and primary_provider_outcome in {"success", "failure"})
                    remaining_query_scopes.append(
                        {
                            "dayNumber": day_number,
                            "slotId": str(slot.slot_id or ""),
                            "daySeedAmapId": str(scope.day_seed.amap_id or scope.day_seed.id or ""),
                            "queryScopeFingerprint": str(entry["queryScopeFingerprint"]),
                            "centerRole": scope.center_strategy,
                            "queryRole": "adjacent_candidate_center",
                            "queryText": query,
                            "priority": int(entry["priority"]),
                            "currentPartialCompletionSlot": bool(
                                selected is None
                                and (
                                    required
                                    or lineage.get("completionRequired") is True
                                    or lineage.get("offerCompletionPriority") is True
                                )
                            ),
                            "predecessorAmapId": str(scope.predecessor.amap_id or scope.predecessor.id or ""),
                            "successorAmapId": (
                                str(scope.successor.amap_id or scope.successor.id or "")
                                if scope.successor is not None
                                else None
                            ),
                            "predecessorBeamRank": int(scope.predecessor_beam_rank),
                            "isActiveScope": is_active_scope,
                            "attemptedThisTurn": attempted_this_turn,
                            "providerOutcome": primary_provider_outcome if attempted_this_turn else "not_called",
                            "remainingReason": (
                                "provider_failure"
                                if attempted_this_turn and primary_provider_outcome == "failure"
                                else "no_candidate_selected"
                                if attempted_this_turn
                                else "not_executed"
                            ),
                        }
                    )
            if primary_search_protected and query:
                protected_searches_remaining -= 1

            grounding_status = "verified_amap" if selected is not None else "unresolved"
            if intent_type == "night_view" and selected is not None:
                grounding_status = "provisional"
                warning = "夜景适配性或夜间开放状态待核验"
            slot_finished = datetime.now(timezone.utc).isoformat()
            slot_event = self._slot_event(
                slot_id=slot.slot_id,
                query=query,
                selected=selected,
                warning=warning,
                grounding_status=grounding_status,
                step_index=slot_ordinal * 2 + 2,
            )
            if blocked_by_missing_frontier_day_seed:
                slot_event["metadata"].update(
                    {
                        "reasonCode": "simple_direction_frontier_day_seed_unresolved",
                        "providerCalled": False,
                    }
                )
            elif claimed_scope_mismatch:
                slot_event["metadata"].update(
                    {
                        "reasonCode": "simple_direction_claimed_adjacent_scope_mismatch",
                        "providerCalled": False,
                    }
                )
            slot_event.update(
                {
                    "startedAt": slot_finished,
                    "finishedAt": slot_finished,
                    "timestamp": slot_finished,
                    "durationMs": 0,
                }
            )
            events.append(slot_event)
            plan = PersistableSegmentPlan(
                day_number=slot.day_number,
                date=slot.date,
                start_time=slot.start_time,
                duration_minutes=slot.duration_minutes,
                kind=slot.kind,
                route_anchor=bool(slot.route_anchor),
                selected_poi=selected,
                display_title=selected.name if selected is not None else f"{slot.raw_need}待补充",
                notes=warning,
                grounding_status=grounding_status,
                ticket_status="not_checked",
                requires_route_edge=bool(selected is not None and self._is_real_amap_candidate(selected)),
                transport_mode=transport_mode,
                raw_need=slot.raw_need,
                intent_type=intent_type,
                goal_id=str(lineage.get("goalId") or "") or None,
                planning_slot_id=str(slot.slot_id or ""),
                pool_id=str(lineage.get("poolId") or (pool.pool_id if pool is not None else "")),
                source_goal_id=str(lineage.get("sourceGoalId") or ""),
                occurrence_id=str(lineage.get("occurrenceId") or ""),
                lineage_authority=str(lineage.get("lineageAuthority") or ""),
                requirement_level=requirement_level,
                required=required,
                schedule_preference=copy.deepcopy(lineage.get("schedulePreference") or {}),
                schedule_constraints={
                    **copy.deepcopy(lineage.get("scheduleConstraints") or {}),
                    "replaceablePoi": str(getattr(pool, "entity_binding_mode", "") or "") != "exact_entity",
                    "entityBindingMode": str(getattr(pool, "entity_binding_mode", "") or "functional"),
                    **(
                        {"localFoodRequired": True}
                        if intent_type == "meal" and self._experience_policy_requires_local_food(experience_policy)
                        else {}
                    ),
                    **(
                        {
                            "mealExperienceBrief": copy.deepcopy(
                                meal_briefs_by_slot.get(str(slot.slot_id or "")) or {}
                            ),
                            "mealQueryPlan": copy.deepcopy(meal_query_plans_by_slot.get(str(slot.slot_id or "")) or {}),
                            "mealSemanticEvidence": copy.deepcopy(selected.meal_semantic_evidence),
                        }
                        if intent_type == "meal" and selected is not None
                        else {
                            "mealExperienceBrief": copy.deepcopy(
                                meal_briefs_by_slot.get(str(slot.slot_id or "")) or {}
                            ),
                            "mealQueryPlan": copy.deepcopy(meal_query_plans_by_slot.get(str(slot.slot_id or "")) or {}),
                        }
                        if intent_type == "meal"
                        else {}
                    ),
                    **(
                        {
                            "qualificationBinding": copy.deepcopy(qualification_binding),
                            "qualificationBindingFingerprint": str(
                                qualification_binding.get("bindingFingerprint") or ""
                            ),
                        }
                        if isinstance(qualification_binding, dict)
                        else {}
                    ),
                    **(
                        {"experienceIndependenceEvidence": copy.deepcopy(selected.experience_independence_evidence)}
                        if selected is not None and selected.experience_independence_evidence
                        else {}
                    ),
                    **self._guide_evidence_schedule_constraints(
                        guide_hint,
                        selected=selected,
                        day_number=int(slot.day_number),
                        planning_slot_id=str(slot.slot_id or ""),
                        primary_result_count=int(guide_query_state.get("providerResultCount") or 0),
                        guide_match_count=len(guide_query_state.get("guideMatchIds") or []),
                        semantic_rejection=semantic_rejection,
                        duplicate_rejection=duplicate_rejection,
                        provider_called=guide_query_state.get("providerCalled") is True,
                        provider_outcome=str(guide_query_state.get("providerOutcome") or "not_called"),
                        query_text=str(guide_query_state.get("queryText") or ""),
                        search_scope=str(guide_query_state.get("searchScope") or ""),
                        nearby_radius=guide_query_state.get("nearbyRadiusMeters"),
                        provider_error_type=str(guide_query_state.get("providerErrorType") or ""),
                        candidate_processing_error_type=str(
                            guide_query_state.get("candidateProcessingErrorType") or ""
                        ),
                        cache_hit=guide_query_state.get("cacheHit"),
                        identity_evidence=(guide_query_state.get("guideIdentityMatches") or {}).get(
                            str(selected.amap_id or "").strip().upper() if selected is not None else ""
                        ),
                        identity_ambiguous=guide_query_state.get("guideIdentityAmbiguous") is True,
                        query_not_executed_reason=(
                            "missing_day_seed"
                            if blocked_by_missing_frontier_day_seed
                            else "claimed_scope_mismatch"
                            if claimed_scope_mismatch
                            else "frontier_exhausted"
                            if slot_frontier_exhausted
                            else "empty_query"
                            if not query
                            else "budget_exhausted"
                            if primary_provider_outcome == "not_called" and search_ordinal >= search_budget
                            else "budget_reserved"
                            if primary_provider_outcome == "not_called" and not may_use_search_budget
                            else ""
                        ),
                    ),
                },
            )
            plans.append(plan)
            if (
                plan.route_anchor
                and selected is not None
                and self._is_real_amap_candidate(selected)
                and day_number not in day_seed_anchor_by_day
                and (day_number not in frontier_campus_days or intent_type == "campus_visit")
            ):
                day_seed_anchor_by_day[day_number] = selected
            if selected is not None and self._is_real_amap_candidate(selected):
                admitted_predecessor_by_day[day_number] = selected
                predecessor_beam: list[POI] = []
                seen_predecessors: set[str] = set()
                for candidate in [selected, *candidates_by_slot.get(str(slot.slot_id or ""), [])]:
                    if not self._is_real_amap_candidate(candidate):
                        continue
                    identity = str(candidate.amap_id or candidate.id or "").strip().upper()
                    if identity in seen_predecessors:
                        continue
                    seen_predecessors.add(identity)
                    predecessor_beam.append(copy.deepcopy(candidate))
                admitted_predecessor_beam_by_day[day_number] = predecessor_beam
        if frontier_outcomes or slot_query_outcomes or remaining_query_scopes:
            frontier_event_time = datetime.now(timezone.utc).isoformat()
            events.append(
                {
                    "type": "simple_direction_frontier_outcomes",
                    "label": "记录资格实体前沿结果",
                    "status": "completed",
                    "detail": "仅回写服务端已领取的高校实体查询结果。",
                    "providerName": None,
                    "startedAt": frontier_event_time,
                    "finishedAt": frontier_event_time,
                    "timestamp": frontier_event_time,
                    "durationMs": 0,
                    "metadata": {
                        "outcomes": copy.deepcopy(frontier_outcomes),
                        "slotQueryOutcomes": copy.deepcopy(slot_query_outcomes),
                        "remainingQueryScopes": copy.deepcopy(remaining_query_scopes),
                    },
                }
            )
        assignment = None
        if route_decision_contract:
            assignment = self.route_assignment_service.assign(
                plans,
                candidates_by_slot,
                route_decision_contract=route_decision_contract,
                transport_mode=transport_mode,
                route_budget=route_budget,
                plan_id=route_plan_id,
            )
            plans = assignment.plans
            for plan in plans:
                spatial_evidence = self._spatial_candidate_evidence(
                    plan.selected_poi,
                    spatial_preference or {},
                )
                plan.schedule_constraints = {
                    **copy.deepcopy(plan.schedule_constraints or {}),
                    "routeAssignment": copy.deepcopy(assignment.audit),
                    "spatialPreferenceEvidence": spatial_evidence,
                }
            event_time = datetime.now(timezone.utc).isoformat()
            events.append(
                {
                    "type": "simple_open_route_assignment",
                    "label": "按真实路线成本分配候选",
                    "status": (
                        "completed"
                        if assignment.audit.get("topologyCompliance") == "verified"
                        and assignment.audit.get("routeCoverageComplete") is True
                        else "partial"
                    ),
                    "detail": (
                        "已通过日内拓扑门禁，并核验最终相邻公交路线。"
                        if assignment.audit.get("topologyCompliance") == "verified"
                        and assignment.audit.get("routeCoverageComplete") is True
                        else "路线证据不足；几何只用于拓扑筛选，方案保持不可确认。"
                    ),
                    "providerName": "amap-webservice",
                    "startedAt": event_time,
                    "finishedAt": event_time,
                    "timestamp": event_time,
                    "durationMs": 0,
                    "metadata": copy.deepcopy(assignment.audit),
                }
            )
        plans = self.dynamic_schedule_service.schedule(plans)
        # Route evidence v2 is final-pair-only.  A post-assignment supplement
        # would first verify the old pair and then replace it with two new
        # pairs, recreating the Provider baseline that this contract removes.
        # Optional activities must therefore be present in the bounded joint
        # candidate set before ``assign``; hints cannot mutate a route-verified
        # proposal afterwards.
        return self.dynamic_schedule_service.schedule(plans), events

    def _supplement_verified_route_gaps(
        self,
        plans: list[PersistableSegmentPlan],
        hints: list[dict[str, Any]],
        *,
        city: str,
        transport_mode: str,
        route_decision_contract: dict[str, Any],
        route_plan_id: str,
        remaining_search_budget: int,
        remaining_route_budget: int,
        baseline_route_legs: list[dict[str, Any]],
        excluded_physical_aliases: set[str] | frozenset[str],
    ) -> tuple[list[PersistableSegmentPlan], list[dict[str, Any]]]:
        """Add at most two optional activities per day with complete evidence.

        A hint alone never creates an occurrence.  The occurrence is sealed
        only after nearby AMap admission, exact Provider insertion evidence,
        detour-envelope validation and a real schedule-gap check all pass.
        """

        raise RuntimeError("post_assignment_route_gap_supplement_disabled_by_final_pair_contract")

        result = [copy.deepcopy(item) for item in plans]
        events: list[dict[str, Any]] = []
        mobility = (
            route_decision_contract.get("mobilityProfile")
            if isinstance(route_decision_contract.get("mobilityProfile"), dict)
            else None
        )
        tolerance = (
            route_decision_contract.get("detourTolerance")
            if isinstance(route_decision_contract.get("detourTolerance"), dict)
            else None
        )
        nearby_radius = self._strict_low_detour_nearby_radius(route_decision_contract)
        if not mobility or not tolerance or nearby_radius is None:
            return result, events
        used_ids = {
            str(plan.selected_poi.amap_id or "").strip().upper()
            for plan in result
            if plan.selected_poi is not None and str(plan.selected_poi.amap_id or "").strip()
        }
        used_physical = {
            self._physical_candidate_key(plan.selected_poi)
            for plan in result
            if plan.selected_poi is not None and self._physical_candidate_key(plan.selected_poi)
        }
        for alias in excluded_physical_aliases:
            if str(alias).startswith("amap:"):
                used_ids.add(str(alias).removeprefix("amap:").upper())
            elif str(alias).startswith("physical:"):
                used_physical.add(str(alias).removeprefix("physical:"))

        per_day_added: dict[int, int] = {}
        for hint_ordinal, hint in enumerate(hints, start=1):
            if remaining_search_budget <= 0 or remaining_route_budget < 2:
                break
            day_number = int(hint.get("dayNumber") or 0)
            if day_number <= 0 or per_day_added.get(day_number, 0) >= 2:
                continue
            anchors = sorted(
                [
                    item
                    for item in result
                    if int(item.day_number) == day_number
                    and item.route_anchor
                    and item.selected_poi is not None
                    and bool((item.schedule_decision or {}).get("constraintPassed"))
                ],
                key=self._sealed_plan_sequence,
            )
            max_anchors = max(1, min(6, int(hint.get("maxRouteAnchors") or 4)))
            if len(anchors) >= max_anchors or len(anchors) < 2:
                continue
            gap = self._widest_scheduled_gap(anchors)
            if gap is None:
                continue
            previous, following, gap_minutes = gap
            estimate = hint.get("durationEstimate") if isinstance(hint.get("durationEstimate"), dict) else {}
            try:
                duration = int(estimate.get("preferred") or 0)
            except (TypeError, ValueError):
                duration = 0
            if duration <= 0 or gap_minutes <= duration:
                continue
            left_poi = previous.selected_poi
            right_poi = following.selected_poi
            assert left_poi is not None and right_poi is not None
            if None in (left_poi.latitude, left_poi.longitude, right_poi.latitude, right_poi.longitude):
                continue
            midpoint_longitude = (float(left_poi.longitude) + float(right_poi.longitude)) / 2
            midpoint_latitude = (float(left_poi.latitude) + float(right_poi.latitude)) / 2
            query = str(hint.get("queryHint") or "").strip()
            intent_type = str(hint.get("intentType") or "").strip()
            family = str(hint.get("experienceFamily") or "").strip()
            if not query or not intent_type or not family:
                continue
            query_scope = hashlib.sha256(
                f"{route_plan_id}|gap|{day_number}|{hint_ordinal}|{query}".encode("utf-8")
            ).hexdigest()
            event_time = datetime.now(timezone.utc).isoformat()
            search_event = {
                "type": "simple_open_route_gap_search",
                "label": "检索顺路补充候选",
                "status": "failed",
                "detail": "仅在相邻已验证地点之间检索，不满足路线与排期证据时不会补充。",
                "providerName": "amap-webservice",
                "startedAt": event_time,
                "finishedAt": event_time,
                "timestamp": event_time,
                "durationMs": 0,
                "metadata": {
                    "dayNumber": day_number,
                    "queryFingerprint": self._query_fingerprint(query),
                    "queryRole": "near_route_corridor",
                    "budgetBefore": remaining_search_budget,
                    "budgetAfter": remaining_search_budget - 1,
                    "adjacentAmapIds": [left_poi.amap_id, right_poi.amap_id],
                },
            }
            remaining_search_budget -= 1
            events.append(search_event)
            try:
                search = self.map_poi_service.search_nearby(
                    city,
                    longitude=midpoint_longitude,
                    latitude=midpoint_latitude,
                    keyword=query,
                    category="all",
                    radius=nearby_radius,
                    limit=5,
                    query_scope_fingerprint=query_scope,
                )
            except Exception as error:
                search_event["metadata"].update(
                    {"providerOutcome": "failure", "errorType": type(error).__name__, "resultCount": 0}
                )
                continue
            candidates = self._admitted_route_gap_candidates(
                search.pois[:5],
                city=city,
                intent_type=intent_type,
                query=query,
                family=family,
                used_identity_ids=used_ids,
                used_physical_keys=used_physical,
            )
            search_event["status"] = "completed"
            search_event["metadata"].update(
                {
                    "providerOutcome": "success",
                    "cacheHit": bool(getattr(search, "cache_hit", False)),
                    "resultCount": len(search.pois),
                    "admittedCandidateIds": [item.amap_id for item in candidates],
                }
            )
            baseline_leg = self._find_route_leg(baseline_route_legs, left_poi, right_poi)
            baseline_cost = RouteInsertionScorer._generalized_cost(baseline_leg, mobility)
            if baseline_cost is None:
                search_event["metadata"]["supplementRejectedReason"] = "baseline_provider_route_missing"
                continue
            selected: tuple[POI, dict[str, Any], dict[str, Any], float, float] | None = None
            rejection_reason = "no_admitted_candidate" if not candidates else "provider_route_budget_insufficient"
            for candidate in candidates:
                if remaining_route_budget < 2:
                    break
                remaining_route_budget -= 2
                left_leg = self.route_assignment_service.route_leg_provider.verified_leg(
                    plan_id=f"{route_plan_id}:gap:{day_number}:{hint_ordinal}:left",
                    left=self.route_assignment_service.provider_poi_payload(left_poi),
                    right=self.route_assignment_service.provider_poi_payload(candidate),
                    transport_mode=transport_mode,
                )
                right_leg = self.route_assignment_service.route_leg_provider.verified_leg(
                    plan_id=f"{route_plan_id}:gap:{day_number}:{hint_ordinal}:right",
                    left=self.route_assignment_service.provider_poi_payload(candidate),
                    right=self.route_assignment_service.provider_poi_payload(right_poi),
                    transport_mode=transport_mode,
                )
                left_cost = RouteInsertionScorer._generalized_cost(left_leg, mobility)
                right_cost = RouteInsertionScorer._generalized_cost(right_leg, mobility)
                if left_cost is None or right_cost is None:
                    rejection_reason = "provider_route_insertion_incomplete"
                    continue
                selected_cost = float(left_cost[0]) + float(right_cost[0])
                delta = max(0.0, selected_cost - float(baseline_cost[0]))
                ratio = delta / max(float(baseline_cost[0]), 1.0)
                if delta > float(tolerance.get("maxGeneralizedCostDelta") or 0) or ratio > float(
                    tolerance.get("maxDetourRatio") or 0
                ):
                    rejection_reason = "route_detour_envelope_exceeded"
                    continue
                travel_minutes = self._route_duration_minutes(left_leg) + self._route_duration_minutes(right_leg)
                if duration + travel_minutes > gap_minutes:
                    rejection_reason = "schedule_gap_too_short"
                    continue
                selected = (candidate, left_leg, right_leg, delta, ratio)
                break
            if selected is None:
                search_event["metadata"]["supplementRejectedReason"] = rejection_reason
                continue
            candidate, left_leg, right_leg, delta, ratio = selected
            previous_end = str((previous.schedule_decision or {}).get("endTime") or "")
            following_start = str((following.schedule_decision or {}).get("startTime") or "")
            left_minutes = self._route_duration_minutes(left_leg)
            preferred_start = self._format_clock(self._clock_minutes(previous_end) + left_minutes)
            identity_seed = hashlib.sha256(
                f"{route_plan_id}|{day_number}|{hint_ordinal}|{family}|{candidate.amap_id}".encode("utf-8")
            ).hexdigest()[:16]
            goal_id = f"goal_route_gap_{identity_seed}"
            occurrence_id = f"occ:{goal_id}:day:{day_number}"
            insertion_sequence = self._sealed_plan_sequence(following)
            plan = PersistableSegmentPlan(
                day_number=day_number,
                date=previous.date,
                start_time=preferred_start,
                duration_minutes=duration,
                kind="visit",
                route_anchor=True,
                selected_poi=copy.deepcopy(candidate),
                display_title=str(candidate.name),
                notes=(
                    f"顺路补充：位于 {left_poi.name} → {right_poi.name} 之间；"
                    f"真实路线新增广义成本约 {delta:.1f}，填补约 {gap_minutes} 分钟空档。"
                ),
                grounding_status="verified_amap",
                ticket_status="not_checked",
                requires_route_edge=self._is_real_amap_candidate(candidate),
                transport_mode=transport_mode,
                raw_need=family,
                intent_type=intent_type,
                goal_id=goal_id,
                source_goal_id=goal_id,
                occurrence_id=occurrence_id,
                pool_id=f"pool_route_gap_{identity_seed}",
                planning_slot_id=f"slot_route_gap_{identity_seed}",
                lineage_authority="simple_open_route_gap_supplement",
                requirement_level="optional",
                required=False,
                schedule_preference={
                    "dayPart": "flexible",
                    "sequence": insertion_sequence,
                    "priority": "optional",
                    "userExplicit": False,
                    "sourceGoalId": goal_id,
                    "occurrenceId": occurrence_id,
                },
                schedule_constraints={
                    "preferredStartTime": preferred_start,
                    "earliestStart": preferred_start,
                    "windowEnd": following_start,
                    "routeTravelMinutesFromPrevious": left_minutes,
                    "routeArrivalSource": "verified_provider_route_insertion",
                    "isAutoSupplemented": True,
                    "supplementReason": "verified_route_gap",
                    "adjacentAnchorNames": [left_poi.name, right_poi.name],
                    "addedTravelMinutes": self._route_duration_minutes(left_leg)
                    + self._route_duration_minutes(right_leg)
                    - self._route_duration_minutes(baseline_leg),
                    "detourRatio": round(ratio, 6),
                    "openingEvidenceStatus": "unverified",
                    "providerRoutePairs": [copy.deepcopy(left_leg), copy.deepcopy(right_leg)],
                },
            )
            scheduled_candidate = self.dynamic_schedule_service.schedule([plan])[0]
            if not bool((scheduled_candidate.schedule_decision or {}).get("constraintPassed")):
                search_event["metadata"]["supplementRejectedReason"] = str(
                    (scheduled_candidate.schedule_decision or {}).get("failureReason") or "dynamic_schedule_rejected"
                )
                continue
            if not self._reserve_selected_candidate(candidate, used_ids, used_physical):
                continue
            opening_status = str((scheduled_candidate.schedule_decision or {}).get("openingEvidenceStatus") or "")
            scheduled_candidate.schedule_constraints["openingEvidenceStatus"] = opening_status
            for existing in result:
                if (
                    int(existing.day_number) == day_number
                    and self._sealed_plan_sequence(existing) >= insertion_sequence
                ):
                    existing.schedule_preference = {
                        **copy.deepcopy(existing.schedule_preference or {}),
                        "sequence": self._sealed_plan_sequence(existing) + 1,
                    }
            result.append(scheduled_candidate)
            baseline_route_legs.extend([copy.deepcopy(left_leg), copy.deepcopy(right_leg)])
            per_day_added[day_number] = per_day_added.get(day_number, 0) + 1
            search_event["metadata"].update(
                {
                    "selectedAmapId": candidate.amap_id,
                    "routeBudgetAfter": remaining_route_budget,
                    "generalizedCostDelta": round(delta, 4),
                    "detourRatio": round(ratio, 6),
                    "lineageAuthority": "simple_open_route_gap_supplement",
                }
            )
        return result, events

    def _admitted_route_gap_candidates(
        self,
        candidates: list[Any],
        *,
        city: str,
        intent_type: str,
        query: str,
        family: str,
        used_identity_ids: set[str],
        used_physical_keys: set[str],
    ) -> list[POI]:
        admitted: list[POI] = []
        for candidate in candidates:
            amap_id = str(getattr(candidate, "id", "") or "").strip().upper()
            physical = self._physical_candidate_key(candidate)
            if not self._is_accepted_candidate(
                candidate,
                amap_id,
                city,
                used_identity_ids,
                used_physical_keys,
                physical,
            ) or not self._candidate_type_matches_intent(candidate, intent_type):
                continue
            semantic = self.intent_candidate_semantic_policy.evaluate(
                intent_type,
                candidate,
                raw_need=query,
                exact_entity=None,
                optional_experience_family=family,
            )
            if not semantic.passed:
                continue
            admitted.append(
                POI(
                    id=f"poi_{uuid4().hex[:12]}",
                    amap_id=amap_id,
                    parent_poi_id=str(candidate.parent_poi_id or "").strip().upper() or None,
                    indoor_parent_poi_id=str(candidate.indoor_parent_poi_id or "").strip().upper() or None,
                    name=str(candidate.name),
                    city=str(candidate.city),
                    category=str(candidate.category or "scenic"),
                    latitude=float(candidate.latitude),
                    longitude=float(candidate.longitude),
                    source=AMAP_PLACE_SOURCE,
                    confidence=float(candidate.confidence or 0.0),
                    type=str(candidate.type or ""),
                    provider_type_code=str(candidate.provider_type_code or "") or None,
                    business_status=str(candidate.business_status or "") or None,
                    provider_queried_at=candidate.provider_queried_at,
                    provider_query_receipt_fingerprint=str(candidate.provider_query_receipt_fingerprint or "") or None,
                    district=str(candidate.district or ""),
                    adcode=str(candidate.adcode or "") or None,
                    address=str(candidate.address or ""),
                    source_note="simple_open_v1: 真实路线空档附近候选。",
                    photos=[self._photo_payload(item) for item in candidate.photos],
                    open_time_today=str(candidate.open_time_today or "") or None,
                    open_time_week=str(candidate.open_time_week or "") or None,
                )
            )
        return admitted

    @staticmethod
    def _sealed_plan_sequence(plan: PersistableSegmentPlan) -> int:
        try:
            return int((plan.schedule_preference or {}).get("sequence") or 999)
        except (TypeError, ValueError):
            return 999

    @classmethod
    def _widest_scheduled_gap(
        cls,
        anchors: list[PersistableSegmentPlan],
    ) -> tuple[PersistableSegmentPlan, PersistableSegmentPlan, int] | None:
        best: tuple[PersistableSegmentPlan, PersistableSegmentPlan, int] | None = None
        for previous, following in zip(anchors, anchors[1:]):
            previous_end = cls._clock_minutes((previous.schedule_decision or {}).get("endTime"))
            following_start = cls._clock_minutes((following.schedule_decision or {}).get("startTime"))
            gap = following_start - previous_end
            if gap > 0 and (best is None or gap > best[2]):
                best = (previous, following, gap)
        return best

    @staticmethod
    def _find_route_leg(
        legs: list[dict[str, Any]],
        left: POI,
        right: POI,
    ) -> dict[str, Any] | None:
        left_id = str(left.amap_id or left.id or "")
        right_id = str(right.amap_id or right.id or "")
        return next(
            (
                item
                for item in legs
                if str(item.get("fromAmapId") or "") == left_id and str(item.get("toAmapId") or "") == right_id
            ),
            None,
        )

    @staticmethod
    def _route_duration_minutes(leg: dict[str, Any] | None) -> int:
        if not isinstance(leg, dict):
            return 0
        seconds = SimpleOpenRouteAssignmentService._route_duration_seconds(leg)
        return max(0, int((seconds or 0) / 60 + 0.999))

    @staticmethod
    def _clock_minutes(value: object) -> int:
        try:
            hour, minute = str(value or "").split(":", 1)
            return int(hour) * 60 + int(minute)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _format_clock(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

    def _candidate_admitted_for_occurrence(
        self,
        candidate: POI,
        *,
        city: str,
        intent_type: str,
        raw_need: str,
        exact_entity: str | None,
        optional_experience_family: str,
        experience_policy: dict[str, Any] | None,
        trip_date: str | None,
        duration_minutes: int,
        schedule_preference: dict[str, Any],
        day_anchor: POI | None = None,
        qualification_binding: dict[str, Any] | None = None,
        meal_experience_brief: dict[str, Any] | None = None,
        used_meal_brands: set[str] | None = None,
        used_meal_families: set[str] | None = None,
    ) -> bool:
        """Re-run every occurrence-specific admission rule for queued POIs."""

        if self._candidate_semantic_rejection_reasons(
            candidate,
            city=city,
            intent_type=intent_type,
            raw_need=raw_need,
            exact_entity=exact_entity,
            optional_experience_family=optional_experience_family,
            qualification_binding=qualification_binding,
            experience_policy=experience_policy,
            intent_candidate_semantic_policy=self.intent_candidate_semantic_policy,
            meal_candidate_quality_policy=self.meal_candidate_quality_policy,
        ):
            return False
        if intent_type == "park":
            independence = self._park_independence_evidence(candidate, day_anchor)
            candidate.experience_independence_evidence = copy.deepcopy(independence)
            if str(independence.get("status") or "") != "standalone_verified":
                return False
        if intent_type == "meal":
            provider_types = self._destination_cuisine_provider_types(
                city=city,
                experience_policy=experience_policy,
            )
            meal_evidence = self.meal_experience_portfolio_policy.semantic_evidence(
                candidate,
                brief=meal_experience_brief,
                city=city,
                provider_types=provider_types,
                local_food_required=self._experience_policy_requires_local_food(experience_policy),
            )
            theme_required = bool(
                self._experience_policy_requires_local_food(experience_policy)
                or (meal_experience_brief or {}).get("searchTerms")
            )
            if theme_required and meal_evidence.get("themeGrounded") is not True:
                return False
            if (
                self._experience_policy_requires_local_food(experience_policy)
                and meal_evidence.get("localFoodPassed") is not True
            ):
                return False
            candidate.meal_semantic_evidence = copy.deepcopy(meal_evidence)
            if self.meal_diversity_policy.duplicate_reason(
                candidate,
                set(),
                used_meal_brands or set(),
                set(),
                used_meal_families or set(),
            ):
                return False
        feasible = self.dynamic_schedule_service.candidate_open_for_semantic_preference(
            trip_date=trip_date,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            open_time_today=candidate.open_time_today,
            day_part=str(schedule_preference.get("dayPart") or ""),
            duration_minutes=duration_minutes,
        )
        return feasible is not False

    @classmethod
    def _candidate_semantic_rejection_reasons(
        cls,
        candidate: Any,
        *,
        city: str,
        intent_type: str,
        raw_need: str,
        exact_entity: str | None = None,
        optional_experience_family: str = "",
        qualification_binding: dict[str, Any] | None = None,
        experience_policy: dict[str, Any] | None = None,
        intent_candidate_semantic_policy: IntentCandidateSemanticPolicy | None = None,
        meal_candidate_quality_policy: MealCandidateQualityPolicy | None = None,
        enforce_intent_semantic_policy: bool = True,
    ) -> list[str]:
        """Return the shared semantic hard-rejection reasons for one POI.

        Simple Open has three candidate consumers: fresh Provider search,
        direction-local candidate reuse, and persisted-snapshot activation.
        Keeping their semantic and local-food gates here prevents a candidate
        accepted by one boundary from being treated as valid by another.
        """

        if not cls._candidate_type_matches_intent(
            candidate,
            intent_type,
            experience_policy=experience_policy,
        ):
            return ["provider_type_intent_mismatch"]
        if enforce_intent_semantic_policy:
            semantic_policy = intent_candidate_semantic_policy or IntentCandidateSemanticPolicy()
            semantic = semantic_policy.evaluate(
                intent_type,
                candidate,
                raw_need=raw_need,
                exact_entity=exact_entity,
                optional_experience_family=optional_experience_family,
                qualification_binding=qualification_binding,
            )
            if not bool(getattr(semantic, "passed", False)):
                return [str(getattr(semantic, "reason_code", "") or "semantic_policy_rejected")]
        if str(intent_type or "") != "meal":
            return []
        meal_policy = meal_candidate_quality_policy or MealCandidateQualityPolicy()
        local_food_hints = ["当地特色美食"] if cls._experience_policy_requires_local_food(experience_policy) else []
        meal_quality = meal_policy.evaluate(raw_need, local_food_hints, candidate, city=city)
        if meal_quality.acceptable:
            return []
        return [str(reason) for reason in meal_quality.hard_reject_reasons or ["meal_evidence_pending"] if str(reason)]

    @staticmethod
    def _experience_policy_requires_local_food(experience_policy: dict[str, Any] | None) -> bool:
        if not isinstance(experience_policy, dict):
            return False
        if experience_policy.get("localFoodRequired") is True:
            return True
        constraint = experience_policy.get("localExperienceConstraint")
        return bool(
            isinstance(constraint, dict)
            and str(constraint.get("experienceType") or "") == "local_cuisine"
            and str(constraint.get("evidencePolicy") or "") == "provider_city_specific_fact"
        )

    def _park_independence_evidence(self, candidate: Any, day_anchor: POI | None) -> dict[str, Any]:
        evidence = self.experience_independence_service.evaluate(candidate, day_anchor or {})
        anchor_id = str(evidence.get("dayAnchorAmapId") or "")
        if anchor_id or str(evidence.get("status") or "") == "standalone_verified":
            return evidence
        reason_codes = [str(item) for item in evidence.get("reasonCodes") or [] if str(item)]
        if "day_anchor_missing" not in reason_codes:
            reason_codes.append("day_anchor_missing")
        return {
            **evidence,
            "status": "independence_pending",
            "reasonCodes": reason_codes,
        }

    @classmethod
    def _frontier_slots(cls, frontier_assignment: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not isinstance(frontier_assignment, dict):
            return {}
        slots: dict[str, dict[str, Any]] = {}
        assignments = frontier_assignment.get("campusAssignments")
        if frontier_assignment.get("schemaVersion") != "simple-direction-frontier-attempt-v1":
            assignments = frontier_assignment.get("assignments")
        if isinstance(assignments, dict):
            iterable = [
                {"slotId": slot_id, **value} for slot_id, value in assignments.items() if isinstance(value, dict)
            ]
        elif isinstance(assignments, list):
            iterable = [value for value in assignments if isinstance(value, dict)]
        else:
            iterable = []
        for item in iterable:
            slot_id = str(item.get("slotId") or "")
            if slot_id:
                slots[slot_id] = copy.deepcopy(item)
        for field_name in ("slotQueries", "queryPages"):
            queries = frontier_assignment.get(field_name)
            if isinstance(queries, dict):
                query_items = [
                    {"slotId": slot_id, **value} for slot_id, value in queries.items() if isinstance(value, dict)
                ]
            elif isinstance(queries, list):
                query_items = [value for value in queries if isinstance(value, dict)]
            else:
                query_items = []
            for item in query_items:
                slot_id = str(item.get("slotId") or "")
                if slot_id:
                    slots[slot_id] = {**slots.get(slot_id, {}), **copy.deepcopy(item)}
        return slots

    @staticmethod
    def _slot_frontier_snapshot(frontier_assignment: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(frontier_assignment, dict):
            return {}
        snapshot = frontier_assignment.get("slotFrontierSnapshot")
        if not isinstance(snapshot, dict):
            return {}
        if snapshot.get("schemaVersion") != SimpleDirectionFrontierService.SCHEMA_VERSION:
            return {}
        request_fingerprint = str(snapshot.get("requestContractFingerprint") or "")
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", request_fingerprint):
            return {}
        return copy.deepcopy(snapshot)

    @staticmethod
    def _positive_frontier_int(value: Any, *, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(parsed, maximum))

    @staticmethod
    def _frontier_scope_fingerprint(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized if re.fullmatch(r"[0-9A-Fa-f]{64}", normalized) else None

    @staticmethod
    def _experience_policy_for_intent(
        policies: dict[str, dict[str, Any]] | None,
        intent_type: str,
    ) -> dict[str, Any]:
        if not isinstance(policies, dict):
            return {}
        policy = policies.get(str(intent_type or "").strip())
        return copy.deepcopy(policy) if isinstance(policy, dict) else {}

    @staticmethod
    def _safe_query_for_intent(
        query: str,
        *,
        city: str,
        intent_type: str,
        experience_policy: dict[str, Any] | None = None,
        preserve_exact_entity: bool = False,
    ) -> str:
        """Keep Simple search hints inside an entity-admissible query boundary.

        Creative theme terms such as ``摄影`` describe an activity, but AMap
        text search commonly resolves them to photography businesses.  Strict
        Portfolio retains its own query planner; only Simple night slots replace
        those unsafe hints with a public night-view entity query.
        """

        normalized = str(query or "").strip()
        if intent_type == "meal" and not preserve_exact_entity:
            normalized_city = str(city or "").strip()
            city_variants = {normalized_city, normalized_city.removesuffix("市")}
            compact = re.sub(r"[\s，,。;；:/]+", "", normalized)
            for city_variant in sorted((item for item in city_variants if item), key=len, reverse=True):
                if compact.startswith(city_variant):
                    compact = compact[len(city_variant) :]
                    break
            provider_types = SimpleOpenItineraryExecutor._destination_cuisine_provider_types(
                city=normalized_city,
                experience_policy=experience_policy,
            )
            schedule_quantity_noise = bool(
                re.search(
                    r"[一二两三四五六七八九十\d]+\s*个\s*(?:不同)?\s*(?:地点|位置|行程|景点|活动)",
                    normalized,
                )
            )
            provider_type_token = re.sub(r"\s+", "", provider_types or "")
            provider_type_wrapper = (
                compact.replace(provider_type_token, "", 1)
                if provider_type_token and provider_type_token in compact
                else None
            )
            generic_provider_type_query = provider_type_wrapper in {
                "",
                "早餐",
                "午餐",
                "晚餐",
                "餐饮",
                "餐厅",
                "早餐餐厅",
                "午餐餐厅",
                "晚餐餐厅",
            }
            if provider_types and (
                compact in _GENERIC_MEAL_QUERIES or generic_provider_type_query or schedule_quantity_noise
            ):
                # Search intent and provider taxonomy are separate contracts.
                # ``provider_types`` is passed independently to AMap; using it
                # as the keyword collapsed every meal slot into the same result
                # pool and made physical novelty look like culinary novelty.
                return "餐厅"
            if compact in _GENERIC_MEAL_QUERIES:
                # Nearby search already carries a canonical adjacent center and
                # food category.  A concrete AMap category term has materially
                # better recall than the abstract experience phrase while the
                # downstream admission policy still owns semantic truth.
                return "餐厅"
        if intent_type != "night_view":
            return normalized
        normalized_city = str(city or "").strip()
        if normalized_city.endswith("市"):
            normalized_city = normalized_city[:-1]
        policy = experience_policy if isinstance(experience_policy, dict) else {}
        families = {
            str(item or "").strip().casefold()
            for item in policy.get("experienceFamilies") or []
            if isinstance(item, str) and str(item or "").strip()
        }
        access_policy = str(policy.get("accessPolicy") or "").strip().casefold()
        authoritative_policy = bool(families or access_policy)
        unsafe_activity_hint = bool(_NIGHT_VIEW_UNSAFE_QUERY_RE.search(normalized))
        generic_night_hint = bool(_NIGHT_VIEW_GENERIC_QUERY_RE.fullmatch(normalized))
        if preserve_exact_entity:
            return normalized
        public_park_policy = access_policy == "public_outdoor" and "park_relax" in families
        generic_public_park_hint = bool(_NIGHT_VIEW_PUBLIC_PARK_NATURAL_QUERY_RE.search(normalized))
        if public_park_policy and (generic_night_hint or generic_public_park_hint):
            # The authoritative policy is a nearby public-park category.  Do
            # not send the user's whole disjunctive sentence as an AMap name
            # query; exact-entity pools are explicitly exempt above.
            return "公园"
        if not unsafe_activity_hint and not (authoritative_policy and generic_night_hint):
            return normalized
        if not authoritative_policy:
            return f"{normalized_city} 夜景 观景台".strip()
        if public_park_policy:
            # Nearby AMap search already carries the sealed campus coordinate
            # and radius.  A bare category term is both more precise and more
            # likely to return the public park the user explicitly allowed.
            return "公园"
        if access_policy == "public_outdoor" and "waterfront_evening" in families:
            return f"{normalized_city} 滨水夜景".strip()
        if access_policy == "public_outdoor":
            return f"{normalized_city} 城市夜景".strip()
        return f"{normalized_city} 夜景 观景台".strip()

    @staticmethod
    def _destination_cuisine_provider_types(
        *,
        city: str,
        experience_policy: dict[str, Any] | None,
    ) -> str | None:
        policy = experience_policy if isinstance(experience_policy, dict) else {}
        constraint = policy.get("localExperienceConstraint")
        if not (
            isinstance(constraint, dict)
            and str(constraint.get("experienceType") or "") == "local_cuisine"
            and str(constraint.get("evidencePolicy") or "") == "provider_city_specific_fact"
        ):
            return None
        locality = constraint.get("locality")
        provider_city = str((locality.get("city") if isinstance(locality, dict) else "") or city).strip()
        provider_city = re.sub(r"(?:特别行政区|自治区|自治州|地区|盟|市)$", "", provider_city)
        provider_city = re.sub(r"[^A-Za-z\u4e00-\u9fff·]", "", provider_city)
        if len(provider_city) < 2:
            return None
        return _VERIFIED_DESTINATION_CUISINE_PROVIDER_TYPES.get(provider_city)

    @classmethod
    def _safe_alternative_query_for_intent(
        cls,
        *,
        city: str,
        intent_type: str,
        primary_query: str,
    ) -> str:
        alternatives = {
            "campus_visit": f"{city} 高等院校 校区",
            "meal": f"{city} 当地特色餐厅",
            "night_view": f"{city} 夜景 观景台",
            "museum": f"{city} 博物馆",
            "park": f"{city} 城市公园",
            "shopping": f"{city} 商业街区",
            "area_walk": f"{city} 历史街区",
            "local_culture": f"{city} 文化场馆",
            "landmark": f"{city} 城市地标",
        }
        candidate = alternatives.get(str(intent_type or ""), "")
        safe = cls._safe_query_for_intent(candidate, city=city, intent_type=intent_type) if candidate else ""
        return safe if safe and safe != str(primary_query or "").strip() else ""

    @staticmethod
    def _intent_display_label(intent_type: str) -> str:
        return {
            "campus_visit": "高校地点",
            "meal": "当地特色餐饮",
            "night_view": "夜景地点",
            "museum": "博物馆",
            "park": "公园",
            "shopping": "购物地点",
            "area_walk": "街区漫步",
            "local_culture": "当地文化体验",
            "landmark": "地标地点",
        }.get(str(intent_type or ""), "地点")

    @staticmethod
    def _ordered_candidate_slots(
        slots: Any,
        authoritative_lineage: dict[str, dict[str, Any]],
    ) -> list[Any]:
        candidates = list(slots or [])

        def lineage_for(slot: Any) -> dict[str, Any]:
            lineage = authoritative_lineage.get(str(slot.slot_id or ""), {})
            return lineage if isinstance(lineage, dict) else {}

        def mapping(lineage: dict[str, Any], key: str) -> dict[str, Any]:
            value = lineage.get(key)
            return value if isinstance(value, dict) else {}

        preferences_by_day: dict[int, list[tuple[dict[str, object] | None, str]]] = {}
        for slot in candidates:
            preference = mapping(lineage_for(slot), "schedulePreference")
            preferences_by_day.setdefault(int(slot.day_number), []).append((preference, str(slot.slot_id or "")))
        controller_sequence_by_day = {
            day_number: SimpleOpenDynamicScheduleService.controller_sequence_authoritative_for_preferences(preferences)
            for day_number, preferences in preferences_by_day.items()
        }
        return sorted(
            candidates,
            key=lambda slot: (
                int(slot.day_number),
                *SimpleOpenDynamicScheduleService.semantic_order_key(
                    mapping(lineage_for(slot), "schedulePreference"),
                    slot.start_time,
                    tie_breaker=str(slot.slot_id or ""),
                    controller_sequence_authoritative=controller_sequence_by_day.get(int(slot.day_number), False),
                    explicit_start_time=mapping(lineage_for(slot), "scheduleConstraints").get("explicitStartTime"),
                ),
            ),
        )

    @staticmethod
    def _strict_low_detour_nearby_radius(
        route_decision_contract: dict[str, Any] | None,
    ) -> int | None:
        contract = route_decision_contract if isinstance(route_decision_contract, dict) else {}
        adjacent = contract.get("adjacentLegConstraint")
        if str(contract.get("status") or "") == "ready" and isinstance(adjacent, dict):
            try:
                radius = int(float(adjacent.get("candidateSearchRadiusMeters")))
            except (TypeError, ValueError):
                return None
            if not 100 <= radius <= 50000:
                return None
            return min(radius, AMAP_PLACE_AROUND_MAX_RADIUS_METERS)
        return None

    @classmethod
    def _known_fixed_successor(
        cls,
        slot_execution_inputs: list[tuple[Any, Any, dict[str, Any], str, str, str, bool]],
        *,
        current_index: int,
        persisted_required_candidates: dict[str, list[POI]],
        frontier_slots: dict[str, dict[str, Any]],
    ) -> POI | None:
        """Return the next already-grounded fixed stop without calling a Provider."""

        if current_index < 0 or current_index >= len(slot_execution_inputs):
            return None
        current_slot = slot_execution_inputs[current_index][0]
        current_day = int(current_slot.day_number)
        for future_slot, future_pool, future_lineage, future_intent, _query, _level, _required in slot_execution_inputs[
            current_index + 1 :
        ]:
            future_day = int(future_slot.day_number)
            if future_day != current_day:
                if future_day > current_day:
                    break
                continue
            frontier_slot = frontier_slots.get(str(future_slot.slot_id or ""), {})
            frozen_amap_id = str(frontier_slot.get("priorCanonicalAmapId") or "").strip().upper()
            is_fixed = bool(
                str(getattr(future_pool, "entity_binding_mode", "") or "") == "exact_entity"
                or (future_intent == "campus_visit" and frozen_amap_id)
            )
            if not is_fixed:
                continue
            occurrence_id = str(future_lineage.get("occurrenceId") or "")
            for candidate in persisted_required_candidates.get(occurrence_id, []):
                candidate_amap_id = str(candidate.amap_id or candidate.id or "").strip().upper()
                if frozen_amap_id and candidate_amap_id != frozen_amap_id:
                    continue
                if cls._is_real_amap_candidate(candidate):
                    return copy.deepcopy(candidate)
        return None

    @classmethod
    def _adjacent_candidate_scope(
        cls,
        *,
        day_seed: POI | None,
        predecessor: POI | None,
        successor: POI | None,
    ) -> _AdjacentCandidateScope | None:
        scopes = cls._adjacent_candidate_scopes(
            day_seed=day_seed,
            predecessors=[predecessor] if predecessor is not None else [],
            successor=successor,
        )
        return scopes[0] if scopes else None

    @classmethod
    def _adjacent_candidate_scopes(
        cls,
        *,
        day_seed: POI | None,
        predecessors: list[POI],
        successor: POI | None,
    ) -> list[_AdjacentCandidateScope]:
        if day_seed is None or not cls._is_real_amap_candidate(day_seed):
            return []
        predecessor_beam: list[POI] = []
        seen_predecessors: set[str] = set()
        for predecessor in predecessors:
            if not cls._is_real_amap_candidate(predecessor):
                continue
            identity = str(predecessor.amap_id or predecessor.id or "").strip().upper()
            if identity in seen_predecessors:
                continue
            seen_predecessors.add(identity)
            predecessor_beam.append(predecessor)
        if not predecessor_beam:
            return []
        if successor is None or not cls._is_real_amap_candidate(successor):
            return [
                _AdjacentCandidateScope(
                    day_seed=day_seed,
                    predecessor=predecessor,
                    successor=None,
                    center_longitude=float(predecessor.longitude),
                    center_latitude=float(predecessor.latitude),
                    center_strategy="predecessor",
                    predecessor_beam_rank=rank,
                )
                for rank, predecessor in enumerate(predecessor_beam, start=1)
            ]

        # A fixed following stop makes one exact three-center frontier.  Keep
        # the actually selected predecessor first; alternative predecessor
        # beam members remain relevant only while no following identity is
        # frozen.
        predecessor = predecessor_beam[0]
        predecessor_longitude = float(predecessor.longitude)
        predecessor_latitude = float(predecessor.latitude)
        successor_longitude = float(successor.longitude)
        successor_latitude = float(successor.latitude)
        # Use the short longitude arc so the bounded midpoint remains valid at
        # the antimeridian as well as for ordinary same-city coordinates.
        longitude_delta = ((successor_longitude - predecessor_longitude + 180.0) % 360.0) - 180.0
        midpoint_longitude = predecessor_longitude + longitude_delta / 2.0
        if midpoint_longitude > 180.0:
            midpoint_longitude -= 360.0
        elif midpoint_longitude < -180.0:
            midpoint_longitude += 360.0
        midpoint_latitude = (predecessor_latitude + successor_latitude) / 2.0
        return [
            _AdjacentCandidateScope(
                day_seed=day_seed,
                predecessor=predecessor,
                successor=successor,
                center_longitude=midpoint_longitude,
                center_latitude=midpoint_latitude,
                center_strategy="midpoint",
                predecessor_beam_rank=1,
            ),
            _AdjacentCandidateScope(
                day_seed=day_seed,
                predecessor=predecessor,
                successor=successor,
                center_longitude=predecessor_longitude,
                center_latitude=predecessor_latitude,
                center_strategy="predecessor",
                predecessor_beam_rank=1,
            ),
            _AdjacentCandidateScope(
                day_seed=day_seed,
                predecessor=predecessor,
                successor=successor,
                center_longitude=successor_longitude,
                center_latitude=successor_latitude,
                center_strategy="successor",
                predecessor_beam_rank=1,
            ),
        ]

    @staticmethod
    def _adjacent_scope_center_label(scope: _AdjacentCandidateScope) -> str:
        if scope.center_strategy == "midpoint" and scope.successor is not None:
            return f"前序地点 {scope.predecessor.name} 与后序固定地点 {scope.successor.name} 的中点"
        if scope.center_strategy == "successor" and scope.successor is not None:
            return f"后序固定地点 {scope.successor.name}"
        return f"真实前序地点 {scope.predecessor.name}"

    @classmethod
    def _adjacent_scope_frontier_entries(
        cls,
        scopes: list[_AdjacentCandidateScope],
        *,
        route_decision_contract: dict[str, Any],
        spatial_preference: dict[str, Any],
        slot_frontier_snapshot: dict[str, Any],
        day_number: int,
        slot_id: str,
        query: str,
        radius: int,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for priority, scope in enumerate(scopes, start=1):
            query_scope_fingerprint = cls._nearby_frontier_scope_fingerprint(
                route_decision_contract,
                spatial_preference=spatial_preference,
                slot_id=slot_id,
                day_number=day_number,
                query=query,
                scope=scope,
                radius=radius,
            )
            slot_query = (
                SimpleDirectionFrontierService.begin_slot_query(
                    slot_frontier_snapshot,
                    day_number=day_number,
                    slot_id=slot_id,
                    day_seed_amap_id=str(scope.day_seed.amap_id or scope.day_seed.id or ""),
                    query_scope_fingerprint=query_scope_fingerprint,
                )
                if slot_frontier_snapshot
                else {}
            )
            entries.append(
                {
                    "scope": scope,
                    "priority": priority,
                    "queryScopeFingerprint": query_scope_fingerprint,
                    "query": slot_query,
                }
            )
        return entries

    @classmethod
    def _nearby_frontier_scope_fingerprint(
        cls,
        route_decision_contract: dict[str, Any],
        *,
        spatial_preference: dict[str, Any],
        slot_id: str,
        day_number: int,
        query: str,
        scope: _AdjacentCandidateScope,
        radius: int,
    ) -> str:
        return cls._nearby_scope_fingerprint(
            route_decision_contract,
            spatial_preference=spatial_preference,
            slot_id=slot_id,
            day_number=day_number,
            query=query,
            day_seed=scope.day_seed,
            predecessor=scope.predecessor,
            successor=scope.successor,
            center_strategy=scope.center_strategy,
            radius=radius,
            page=None,
        )

    @classmethod
    def _nearby_query_scope_fingerprint(
        cls,
        route_decision_contract: dict[str, Any],
        *,
        spatial_preference: dict[str, Any],
        slot_id: str,
        day_number: int,
        query: str,
        radius: int,
        anchor: POI | None = None,
        scope: _AdjacentCandidateScope | None = None,
        page: int = 1,
    ) -> str:
        if scope is None and anchor is None:
            raise ValueError("nearby_query_scope_anchor_missing")
        day_seed = scope.day_seed if scope is not None else anchor
        predecessor = scope.predecessor if scope is not None else anchor
        successor = scope.successor if scope is not None else None
        center_strategy = scope.center_strategy if scope is not None else "predecessor"
        if day_seed is None or predecessor is None:
            raise ValueError("nearby_query_scope_anchor_missing")
        return cls._nearby_scope_fingerprint(
            route_decision_contract,
            spatial_preference=spatial_preference,
            slot_id=slot_id,
            day_number=day_number,
            query=query,
            day_seed=day_seed,
            predecessor=predecessor,
            successor=successor,
            center_strategy=center_strategy,
            radius=radius,
            page=max(1, int(page)),
        )

    @staticmethod
    def _nearby_scope_fingerprint(
        route_decision_contract: dict[str, Any],
        *,
        spatial_preference: dict[str, Any],
        slot_id: str,
        day_number: int,
        query: str,
        day_seed: POI,
        predecessor: POI,
        successor: POI | None,
        center_strategy: str,
        radius: int,
        page: int | None,
    ) -> str:
        material = "|".join(
            [
                str(route_decision_contract.get("fingerprint") or ""),
                str(spatial_preference.get("fingerprint") or ""),
                "nearby_low_detour",
                str(day_number),
                slot_id,
                str(day_seed.amap_id or day_seed.id or "").upper(),
                str(predecessor.amap_id or predecessor.id or "").upper(),
                str(successor.amap_id or successor.id or "").upper() if successor is not None else "",
                str(center_strategy or "predecessor"),
                str(radius),
                query,
                str(page) if page is not None else "frontier",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _candidate_within_nearby_scope(candidate: Any, anchor: POI, radius: int) -> bool:
        try:
            anchor_latitude = float(anchor.latitude)
            candidate_latitude = float(candidate.latitude)
            anchor_longitude = float(anchor.longitude)
            candidate_longitude = float(candidate.longitude)
            if not all(
                math.isfinite(value)
                for value in (
                    anchor_latitude,
                    candidate_latitude,
                    anchor_longitude,
                    candidate_longitude,
                )
            ):
                return False
            left_latitude = math.radians(anchor_latitude)
            right_latitude = math.radians(candidate_latitude)
            latitude_delta = right_latitude - left_latitude
            longitude_delta = math.radians(candidate_longitude - anchor_longitude)
        except (TypeError, ValueError):
            return False
        value = (
            math.sin(latitude_delta / 2) ** 2
            + math.cos(left_latitude) * math.cos(right_latitude) * math.sin(longitude_delta / 2) ** 2
        )
        distance_meters = (
            6371008.8
            * 2
            * math.atan2(
                math.sqrt(value),
                math.sqrt(max(0.0, 1 - value)),
            )
        )
        return distance_meters <= float(radius)

    @classmethod
    def _candidate_within_adjacent_scope(
        cls,
        candidate: Any,
        scope: _AdjacentCandidateScope,
        radius: int,
    ) -> bool:
        # ``day_seed`` anchors locality identity and continuation; it is not a
        # permanent hard circle around every later stop.  Bound this exact
        # center with the unchanged Provider radius, then let the existing
        # final-pair route assignment verify predecessor/successor travel legs.
        try:
            center = (float(scope.center_longitude), float(scope.center_latitude))
            point = (float(candidate.longitude), float(candidate.latitude))
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in (*center, *point)):
            return False
        return SpatialGeometryService.distance_meters(center, point) <= float(radius)

    @classmethod
    def _spatial_candidate_allowed(
        cls,
        candidate: Any,
        spatial_preference: dict[str, Any],
    ) -> bool:
        if not spatial_preference or str(spatial_preference.get("status") or "") != "resolved":
            return True
        if str(spatial_preference.get("strength") or "preferred") != "required":
            return True
        return cls._spatial_candidate_evidence(candidate, spatial_preference).get("status") == "inside"

    @classmethod
    def _spatial_candidate_rank(
        cls,
        candidate: Any,
        spatial_preference: dict[str, Any],
    ) -> int:
        if not spatial_preference or str(spatial_preference.get("status") or "") != "resolved":
            return 0
        return {"inside": 0, "unknown": 1, "outside": 2}.get(
            str(cls._spatial_candidate_evidence(candidate, spatial_preference).get("status") or "unknown"),
            1,
        )

    @classmethod
    def _spatial_candidate_evidence(
        cls,
        candidate: Any,
        spatial_preference: dict[str, Any],
    ) -> dict[str, Any]:
        strength = str(spatial_preference.get("strength") or "preferred")
        fingerprint = str(spatial_preference.get("fingerprint") or "")
        if candidate is None or str(spatial_preference.get("status") or "") != "resolved":
            return {
                "status": "unknown",
                "strength": strength,
                "spatialPreferenceFingerprint": fingerprint,
            }
        resolution = (
            spatial_preference.get("resolution") if isinstance(spatial_preference.get("resolution"), dict) else {}
        )
        kind = str(resolution.get("kind") or "")
        if kind == "administrative_area":
            raw_candidate_adcode = (
                candidate.get("adcode") if isinstance(candidate, dict) else getattr(candidate, "adcode", "")
            )
            candidate_adcode = str(raw_candidate_adcode or "").strip()
            allowed_adcodes = {str(value).strip() for value in resolution.get("adcodes") or [] if str(value).strip()}
            status = (
                "inside"
                if candidate_adcode and candidate_adcode in allowed_adcodes
                else "outside"
                if candidate_adcode
                else "unknown"
            )
            return {
                "status": status,
                "strength": strength,
                "kind": kind,
                "candidateAdcode": candidate_adcode or None,
                "allowedAdcodes": sorted(allowed_adcodes),
                "spatialPreferenceFingerprint": fingerprint,
            }
        if kind == "reference_point_radius":
            reference = resolution.get("referencePlace") if isinstance(resolution.get("referencePlace"), dict) else {}
            try:
                candidate_latitude = float(
                    candidate.get("latitude") if isinstance(candidate, dict) else getattr(candidate, "latitude", None)
                )
                candidate_longitude = float(
                    candidate.get("longitude") if isinstance(candidate, dict) else getattr(candidate, "longitude", None)
                )
                reference_latitude = float(reference.get("latitude"))
                reference_longitude = float(reference.get("longitude"))
                radius_meters = float(resolution.get("radiusMeters"))
                distance_meters = cls._geometry_distance_meters(
                    candidate_latitude,
                    candidate_longitude,
                    reference_latitude,
                    reference_longitude,
                )
            except (TypeError, ValueError):
                return {
                    "status": "unknown",
                    "strength": strength,
                    "kind": kind,
                    "spatialPreferenceFingerprint": fingerprint,
                }
            return {
                "status": "inside" if distance_meters <= radius_meters else "outside",
                "strength": strength,
                "kind": kind,
                "distanceMeters": round(distance_meters, 2),
                "radiusMeters": radius_meters,
                "spatialPreferenceFingerprint": fingerprint,
            }
        if kind == "named_boundary_polygon":
            raw_longitude = (
                candidate.get("longitude") if isinstance(candidate, dict) else getattr(candidate, "longitude", None)
            )
            raw_latitude = (
                candidate.get("latitude") if isinstance(candidate, dict) else getattr(candidate, "latitude", None)
            )
            polygon = resolution.get("polygonGcj02") if isinstance(resolution.get("polygonGcj02"), list) else []
            try:
                point = (float(raw_longitude), float(raw_latitude))
                normalized_polygon = [(float(item[0]), float(item[1])) for item in polygon]
            except (TypeError, ValueError, IndexError):
                return {
                    "status": "unknown",
                    "strength": strength,
                    "kind": kind,
                    "spatialPreferenceFingerprint": fingerprint,
                }
            inside = SpatialGeometryService.point_in_polygon(point, normalized_polygon)
            containment = str(resolution.get("containment") or "inside")
            allowed = inside if containment == "inside" else not inside
            return {
                "status": "inside" if allowed else "outside",
                "strength": strength,
                "kind": kind,
                "containment": containment,
                "boundaryEvidenceFingerprint": str(resolution.get("boundaryEvidenceFingerprint") or ""),
                "geometryUsedAsRouteFeasibilityEvidence": False,
                "spatialPreferenceFingerprint": fingerprint,
            }
        return {
            "status": "unknown",
            "strength": strength,
            "kind": kind or None,
            "spatialPreferenceFingerprint": fingerprint,
        }

    @staticmethod
    def _geometry_distance_meters(
        left_latitude: float,
        left_longitude: float,
        right_latitude: float,
        right_longitude: float,
    ) -> float:
        left_latitude_radians = math.radians(left_latitude)
        right_latitude_radians = math.radians(right_latitude)
        latitude_delta = right_latitude_radians - left_latitude_radians
        longitude_delta = math.radians(right_longitude - left_longitude)
        value = (
            math.sin(latitude_delta / 2) ** 2
            + math.cos(left_latitude_radians) * math.cos(right_latitude_radians) * math.sin(longitude_delta / 2) ** 2
        )
        return (
            6371008.8
            * 2
            * math.atan2(
                math.sqrt(value),
                math.sqrt(max(0.0, 1 - value)),
            )
        )

    @staticmethod
    def _candidate_admission_rejection(
        amap_id: str,
        physical_key: str,
        reason_code: str,
    ) -> dict[str, Any]:
        """Keep bounded, machine-readable rejection evidence without raw POI dumps."""

        return {
            "physicalGroupId": str(physical_key or amap_id or "unknown"),
            "reasonCodes": [str(reason_code or "candidate_admission_rejected")],
        }

    @staticmethod
    def _admission_rejection_reason_counts(
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            for reason_code in diagnostic.get("reasonCodes") or []:
                normalized = str(reason_code or "").strip()
                if normalized:
                    counts[normalized] = counts.get(normalized, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _semantic_rejection_warning(
        intent_type: str,
        reason_counts: dict[str, int],
    ) -> str:
        reason_codes = set(reason_counts)
        if reason_codes and reason_codes <= {"provider_type_intent_mismatch"}:
            return "高德候选的地点类型与该行程意图冲突，地点保持待补充"
        if str(intent_type or "") == "campus_visit":
            return "高德已返回候选，但没有候选通过指定高校身份、资格与高校主地点核验，地点保持待补充"
        if reason_counts:
            return "高德已返回候选，但没有候选通过该行程意图的身份与语义核验，地点保持待补充"
        return "高德候选未通过安全准入核验，地点保持待补充"

    def _search_candidate(
        self,
        *,
        city: str,
        query: str,
        category: str,
        intent_type: str,
        raw_need: str,
        exact_entity: str | None,
        optional_experience_family: str,
        used_identity_ids: set[str],
        used_physical_keys: set[str],
        experience_policy: dict[str, Any] | None = None,
        trip_date: str | None = None,
        duration_minutes: int = 0,
        schedule_preference: dict[str, Any] | None = None,
        nearby_anchor: POI | None = None,
        adjacent_scope: _AdjacentCandidateScope | None = None,
        day_anchor: POI | None = None,
        nearby_radius: int | None = None,
        query_scope_fingerprint: str | None = None,
        page: int = 1,
        offset: int = 5,
        spatial_preference: dict[str, Any] | None = None,
        required_amap_id: str | None = None,
        qualification_binding: dict[str, Any] | None = None,
        meal_experience_brief: dict[str, Any] | None = None,
        used_meal_brands: set[str] | None = None,
        used_meal_families: set[str] | None = None,
        general_food_fallback: bool = False,
        guide_hint: dict[str, Any] | None = None,
        guide_query_state: dict[str, Any] | None = None,
    ) -> tuple[Any, POI | None, bool, bool, list[POI], list[POI], list[dict[str, Any]]]:
        normalized_required_amap_id = str(required_amap_id or "").strip().upper()
        pagination: dict[str, Any] = {}
        if int(page) != 1:
            pagination["page"] = int(page)
        if int(offset) != 5:
            pagination["offset"] = int(offset)
        provider_search_options: dict[str, Any] = {}
        provider_types: str | None = None
        if not exact_entity and intent_type == "meal":
            provider_types = self._destination_cuisine_provider_types(
                city=city,
                experience_policy=experience_policy,
            )
            if (
                provider_types
                and not general_food_fallback
                and self._experience_policy_requires_local_food(experience_policy)
            ):
                # The concrete theme keyword and AMap taxonomy are independent:
                # ``卤煮`` may be searched under the verified ``北京菜`` subtype
                # without rewriting the query itself to ``北京菜``.
                provider_search_options["provider_types"] = provider_types
        detail_loader = getattr(self.map_poi_service, "detail", None)
        if isinstance(guide_query_state, dict):
            guide_query_state.update(
                {
                    "providerCalled": True,
                    "providerOutcome": "started",
                    "queryText": query,
                    "searchScope": (
                        "canonical_amap_detail"
                        if normalized_required_amap_id and callable(detail_loader)
                        else "nearby_low_detour"
                        if (adjacent_scope is not None or nearby_anchor is not None) and nearby_radius is not None
                        else "city_text"
                    ),
                    "nearbyRadiusMeters": nearby_radius
                    if adjacent_scope is not None or nearby_anchor is not None
                    else None,
                }
            )
        if normalized_required_amap_id and callable(detail_loader):
            detail = detail_loader(normalized_required_amap_id)
            search = MapPoiSearchResponse(
                city=city,
                keyword=query,
                category=category,
                providerName=AMAP_PLACE_SOURCE,
                queriedAt=detail.provider_queried_at or datetime.now(timezone.utc),
                pois=[detail],
                providerQueryReceiptFingerprint=detail.provider_query_receipt_fingerprint,
            )
        elif adjacent_scope is not None and nearby_radius is not None:
            search = self.map_poi_service.search_nearby(
                city,
                adjacent_scope.center_longitude,
                adjacent_scope.center_latitude,
                query,
                category=category,
                radius=nearby_radius,
                limit=5,
                query_scope_fingerprint=query_scope_fingerprint,
                **provider_search_options,
                **pagination,
            )
        elif nearby_anchor is not None and nearby_radius is not None:
            search = self.map_poi_service.search_nearby(
                city,
                float(nearby_anchor.longitude),
                float(nearby_anchor.latitude),
                query,
                category=category,
                radius=nearby_radius,
                limit=5,
                query_scope_fingerprint=query_scope_fingerprint,
                **provider_search_options,
                **pagination,
            )
        else:
            search_parameters: dict[str, Any] = {
                "keyword": query,
                "category": category,
                "limit": 5,
                **provider_search_options,
                **pagination,
            }
            if query_scope_fingerprint:
                search_parameters["query_scope_fingerprint"] = query_scope_fingerprint
            search = self.map_poi_service.search(
                city,
                **search_parameters,
            )
        guide_identity = (
            GuidePoiIdentityService.resolve(hint=guide_hint, candidates=search.pois[:5], city=city)
            if guide_hint is not None else {}
        )
        if isinstance(guide_query_state, dict):
            guide_query_state.update(
                {
                    "providerOutcome": "success",
                    "providerResultCount": len(search.pois),
                    "cacheHit": bool(search.cache_hit),
                    "guideMatchIds": guide_identity.get("matchedCandidateIds", []),
                    "guideIdentityMatches": copy.deepcopy(guide_identity.get("matches", {})),
                    "guideIdentityAmbiguous": guide_identity.get("ambiguous") is True,
                }
            )
        duplicate_rejection = False
        semantic_rejection = False
        admitted: list[POI] = []
        baseline_admitted: list[POI] = []
        admission_diagnostics: list[dict[str, Any]] = []
        local_identity_ids: set[str] = set()
        local_physical_keys: set[str] = set()
        ranked_candidates = sorted(
            search.pois[:5],
            key=lambda candidate: self._spatial_candidate_rank(
                candidate,
                spatial_preference or {},
            ),
        )
        for candidate in ranked_candidates:
            candidate_amap_id = str(candidate.id or "").strip().upper()
            physical_key = self._physical_candidate_key(candidate)
            if guide_hint is not None and (
                candidate_amap_id not in guide_identity.get("matches", {}) or guide_identity.get("ambiguous") is True
            ):
                admission_diagnostics.append(
                    self._candidate_admission_rejection(
                        candidate_amap_id, physical_key,
                        "guide_identity_ambiguous" if guide_identity.get("ambiguous") is True
                        else "guide_identity_evidence_missing",
                    )
                )
                continue
            if normalized_required_amap_id and candidate_amap_id != normalized_required_amap_id:
                # A novelty-collision retry advances only the replaceable
                # experience pages.  Its already-grounded campus identity is
                # frozen by the server-side frontier and cannot drift to a
                # different campus/branch returned by another text-search
                # ordering.
                semantic_rejection = True
                admission_diagnostics.append(
                    self._candidate_admission_rejection(
                        candidate_amap_id,
                        physical_key,
                        "required_amap_identity_mismatch",
                    )
                )
                continue
            if not self._spatial_candidate_allowed(candidate, spatial_preference or {}):
                semantic_rejection = True
                admission_diagnostics.append(
                    self._candidate_admission_rejection(candidate_amap_id, physical_key, "spatial_scope_mismatch")
                )
                continue
            if (
                adjacent_scope is not None
                and nearby_radius is not None
                and not self._candidate_within_adjacent_scope(
                    candidate,
                    adjacent_scope,
                    nearby_radius,
                )
            ):
                admission_diagnostics.append(
                    self._candidate_admission_rejection(candidate_amap_id, physical_key, "nearby_scope_mismatch")
                )
                continue
            if (
                adjacent_scope is None
                and nearby_anchor is not None
                and nearby_radius is not None
                and not self._candidate_within_nearby_scope(candidate, nearby_anchor, nearby_radius)
            ):
                admission_diagnostics.append(
                    self._candidate_admission_rejection(candidate_amap_id, physical_key, "nearby_scope_mismatch")
                )
                continue
            amap_id = str(candidate.id or "").strip().upper()
            if not self._is_accepted_candidate(
                candidate,
                amap_id,
                city,
                set(),
                set(),
                physical_key,
            ):
                duplicate_rejection = True
                continue
            semantic_reasons = self._candidate_semantic_rejection_reasons(
                candidate,
                city=city,
                intent_type=intent_type,
                raw_need=raw_need,
                exact_entity=exact_entity,
                optional_experience_family=optional_experience_family,
                qualification_binding=qualification_binding,
                experience_policy=experience_policy,
                intent_candidate_semantic_policy=self.intent_candidate_semantic_policy,
                meal_candidate_quality_policy=self.meal_candidate_quality_policy,
            )
            if semantic_reasons:
                semantic_rejection = True
                admission_diagnostics.append(
                    self._candidate_admission_rejection(
                        candidate_amap_id,
                        physical_key,
                        semantic_reasons[0],
                    )
                )
                continue
            independence_evidence: dict[str, Any] = {}
            if intent_type == "park":
                independence_evidence = self._park_independence_evidence(candidate, day_anchor)
                if str(independence_evidence.get("status") or "") != "standalone_verified":
                    admission_diagnostics.append(copy.deepcopy(independence_evidence))
                    semantic_rejection = True
                    continue
            if amap_id in local_identity_ids or (physical_key and physical_key in local_physical_keys):
                duplicate_rejection = True
                continue
            schedule_feasible = self.dynamic_schedule_service.candidate_open_for_semantic_preference(
                trip_date=trip_date,
                latitude=candidate.latitude,
                longitude=candidate.longitude,
                open_time_today=candidate.open_time_today,
                day_part=str((schedule_preference or {}).get("dayPart") or ""),
                duration_minutes=duration_minutes,
            )
            if schedule_feasible is False:
                semantic_rejection = True
                admission_diagnostics.append(
                    self._candidate_admission_rejection(
                        candidate_amap_id,
                        physical_key,
                        "schedule_preference_mismatch",
                    )
                )
                continue
            canonical = POI(
                id=f"poi_{uuid4().hex[:12]}",
                amap_id=amap_id,
                parent_poi_id=str(candidate.parent_poi_id or "").strip().upper() or None,
                indoor_parent_poi_id=str(candidate.indoor_parent_poi_id or "").strip().upper() or None,
                name=str(candidate.name),
                city=str(candidate.city),
                category=str(candidate.category or category or "scenic"),
                latitude=float(candidate.latitude),
                longitude=float(candidate.longitude),
                source=AMAP_PLACE_SOURCE,
                confidence=float(candidate.confidence or 0.0),
                type=str(candidate.type or ""),
                provider_type_code=str(candidate.provider_type_code or "") or None,
                business_status=str(candidate.business_status or "") or None,
                provider_queried_at=candidate.provider_queried_at,
                provider_query_receipt_fingerprint=str(candidate.provider_query_receipt_fingerprint or "") or None,
                experience_independence_evidence=copy.deepcopy(independence_evidence),
                district=str(candidate.district or ""),
                adcode=str(candidate.adcode or "") or None,
                address=str(candidate.address or ""),
                source_note=(
                    "simple_open_v1: 本轮高德周边搜索结果身份绑定；周边范围仅约束候选，不代表路线已核验。"
                    if adjacent_scope is not None or nearby_anchor is not None
                    else "simple_open_v1: 本轮高德搜索结果身份绑定。"
                ),
                photos=[self._photo_payload(item) for item in candidate.photos],
                tags=[str(item) for item in candidate.tags if str(item)],
                source_claims=[copy.deepcopy(item) for item in candidate.source_claims if isinstance(item, dict)],
                provider_aliases=list(getattr(candidate, "provider_aliases", []) or []),
                open_time_today=str(candidate.open_time_today or "") or None,
                open_time_week=str(candidate.open_time_week or "") or None,
            )
            if intent_type == "meal":
                meal_evidence = self.meal_experience_portfolio_policy.semantic_evidence(
                    canonical,
                    brief=meal_experience_brief,
                    city=city,
                    provider_types=provider_types,
                    local_food_required=self._experience_policy_requires_local_food(experience_policy),
                )
                canonical.meal_semantic_evidence = copy.deepcopy(meal_evidence)
                meal_theme_required = bool(
                    self._experience_policy_requires_local_food(experience_policy)
                    or (meal_experience_brief or {}).get("searchTerms")
                )
                if meal_theme_required and meal_evidence.get("themeGrounded") is not True:
                    semantic_rejection = True
                    admission_diagnostics.append(
                        self._candidate_admission_rejection(
                            candidate_amap_id,
                            physical_key,
                            "meal_theme_ungrounded",
                        )
                    )
                    continue
                if (
                    self._experience_policy_requires_local_food(experience_policy)
                    and meal_evidence.get("localFoodPassed") is not True
                ):
                    semantic_rejection = True
                    admission_diagnostics.append(
                        self._candidate_admission_rejection(
                            candidate_amap_id,
                            physical_key,
                            "local_food_evidence_missing",
                        )
                    )
                    continue
                duplicate_reason = self.meal_diversity_policy.duplicate_reason(
                    canonical,
                    set(),
                    used_meal_brands or set(),
                    set(),
                    used_meal_families or set(),
                )
                if duplicate_reason:
                    duplicate_rejection = True
                    admission_diagnostics.append(
                        self._candidate_admission_rejection(
                            candidate_amap_id,
                            physical_key,
                            duplicate_reason,
                        )
                    )
                    continue
            baseline_admitted.append(copy.deepcopy(canonical))
            if not self._is_accepted_candidate(
                candidate,
                amap_id,
                city,
                used_identity_ids,
                used_physical_keys,
                physical_key,
            ):
                duplicate_rejection = True
                continue
            admitted.append(canonical)
            local_identity_ids.add(amap_id)
            parent_poi_id = str(candidate.parent_poi_id or "").strip().upper()
            if parent_poi_id:
                local_identity_ids.add(parent_poi_id)
            indoor_parent_poi_id = str(candidate.indoor_parent_poi_id or "").strip().upper()
            if indoor_parent_poi_id:
                local_identity_ids.add(indoor_parent_poi_id)
            if physical_key:
                local_physical_keys.add(physical_key)
        selected = admitted.pop(0) if admitted else None
        if selected is not None:
            self._reserve_selected_candidate(selected, used_identity_ids, used_physical_keys)
            if intent_type == "meal":
                selected_brand = self.meal_diversity_policy.canonical_meal_brand(selected)
                selected_family = self.meal_diversity_policy.dish_family(selected)
                if selected_brand and used_meal_brands is not None:
                    used_meal_brands.add(selected_brand)
                if selected_family and used_meal_families is not None:
                    used_meal_families.add(selected_family)
        return (
            search,
            selected,
            duplicate_rejection,
            semantic_rejection,
            admitted,
            baseline_admitted,
            admission_diagnostics,
        )

    @classmethod
    def _reserve_selected_candidate(
        cls,
        candidate: POI,
        used_identity_ids: set[str],
        used_physical_keys: set[str],
    ) -> bool:
        amap_id = str(candidate.amap_id or "").strip().upper()
        parent_id = str(candidate.parent_poi_id or "").strip().upper()
        indoor_parent_id = str(candidate.indoor_parent_poi_id or "").strip().upper()
        physical_key = cls._physical_candidate_key(candidate)
        if (
            not amap_id
            or amap_id in used_identity_ids
            or (parent_id and parent_id in used_identity_ids)
            or (indoor_parent_id and indoor_parent_id in used_identity_ids)
            or (physical_key and physical_key in used_physical_keys)
        ):
            return False
        used_identity_ids.add(amap_id)
        if parent_id:
            used_identity_ids.add(parent_id)
        if indoor_parent_id:
            used_identity_ids.add(indoor_parent_id)
        if physical_key:
            used_physical_keys.add(physical_key)
        return True

    @classmethod
    def _required_prior_candidate_fallback(
        cls,
        candidates: list[POI],
        *,
        prior_identity_ids: set[str],
        prior_physical_keys: set[str],
        current_plans: list[PersistableSegmentPlan],
    ) -> POI | None:
        """Reuse a prior canonical entity only to preserve a hard occurrence.

        Candidate admission, schedule checks, and Provider identity validation
        have already succeeded before a candidate enters ``candidates``.  This
        helper merely separates root-level novelty exclusions from duplicates
        already selected inside the current proposal.
        """

        current_identity_ids: set[str] = set()
        current_physical_keys: set[str] = set()
        for plan in current_plans:
            poi = plan.selected_poi
            if poi is None:
                continue
            amap_id = str(poi.amap_id or "").strip().upper()
            parent_id = str(poi.parent_poi_id or "").strip().upper()
            indoor_parent_id = str(poi.indoor_parent_poi_id or "").strip().upper()
            physical_key = cls._physical_candidate_key(poi)
            if amap_id:
                current_identity_ids.add(amap_id)
            if parent_id:
                current_identity_ids.add(parent_id)
            if indoor_parent_id:
                current_identity_ids.add(indoor_parent_id)
            if physical_key:
                current_physical_keys.add(physical_key)

        seen: set[str] = set()
        for candidate in candidates:
            amap_id = str(candidate.amap_id or candidate.id or "").strip().upper()
            parent_id = str(candidate.parent_poi_id or "").strip().upper()
            indoor_parent_id = str(candidate.indoor_parent_poi_id or "").strip().upper()
            physical_key = cls._physical_candidate_key(candidate)
            fingerprint = f"{amap_id}|{parent_id}|{indoor_parent_id}|{physical_key}"
            if not amap_id or fingerprint in seen:
                continue
            seen.add(fingerprint)
            if not cls._is_real_amap_candidate(candidate):
                continue
            # A hard occurrence may reuse only the exact canonical POI that was
            # already verified in this comparison root.  A shared parent or a
            # normalized name/address alias is sufficient for novelty
            # exclusion, but it is not proof that a different child POI is the
            # same accepted entity (for example, another campus or an attached
            # school under the same university parent).
            belongs_to_prior = amap_id in prior_identity_ids
            already_in_current = bool(
                amap_id in current_identity_ids
                or (parent_id and parent_id in current_identity_ids)
                or (indoor_parent_id and indoor_parent_id in current_identity_ids)
                or (physical_key and physical_key in current_physical_keys)
            )
            if belongs_to_prior and not already_in_current:
                return copy.deepcopy(candidate)
        return None

    @staticmethod
    def _is_real_amap_candidate(candidate: POI) -> bool:
        amap_id = str(candidate.amap_id or "").strip().upper()
        try:
            latitude = float(candidate.latitude)
            longitude = float(candidate.longitude)
        except (TypeError, ValueError):
            return False
        return bool(
            str(candidate.source or "") == AMAP_PLACE_SOURCE
            and re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id)
            and math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and latitude != 0
            and longitude != 0
        )

    @staticmethod
    def _is_accepted_candidate(
        candidate: Any,
        amap_id: str,
        city: str,
        used_identity_ids: set[str],
        used_physical_keys: set[str],
        physical_key: str,
    ) -> bool:
        parent_poi_id = str(candidate.parent_poi_id or "").strip().upper()
        indoor_parent_poi_id = str(candidate.indoor_parent_poi_id or "").strip().upper()
        return bool(
            re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id)
            and str(candidate.source or "") == AMAP_PLACE_SOURCE
            and amap_id not in used_identity_ids
            and (not parent_poi_id or parent_poi_id not in used_identity_ids)
            and (not indoor_parent_poi_id or indoor_parent_poi_id not in used_identity_ids)
            and (not physical_key or physical_key not in used_physical_keys)
            and candidate.latitude is not None
            and candidate.longitude is not None
            and str(candidate.city or "").strip() in {city, f"{city}市"}
        )

    @staticmethod
    def _physical_candidate_key(candidate: Any) -> str:
        def normalized(value: object) -> str:
            return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(value or "")).casefold()

        name = normalized(getattr(candidate, "name", ""))
        address = normalized(getattr(candidate, "address", ""))
        try:
            longitude = round(float(getattr(candidate, "longitude", None)), 6)
            latitude = round(float(getattr(candidate, "latitude", None)), 6)
        except (TypeError, ValueError):
            return ""
        if not name:
            return ""
        return f"{name}|{address}|{longitude:.6f}|{latitude:.6f}"

    @staticmethod
    def _merge_candidate_queues(existing: list[POI], incoming: list[POI]) -> list[POI]:
        merged: list[POI] = []
        seen: set[str] = set()
        for candidate in [*existing, *incoming]:
            identity = str(candidate.amap_id or candidate.id or "").strip().upper()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            merged.append(copy.deepcopy(candidate))
        return merged[:5]

    @staticmethod
    def _candidate_type_matches_intent(
        candidate: Any,
        intent_type: str,
        *,
        experience_policy: dict[str, Any] | None = None,
    ) -> bool:
        def field(name: str) -> Any:
            return candidate.get(name) if isinstance(candidate, dict) else getattr(candidate, name, "")

        provider_type = str(field("type") or "")
        provider_text = " ".join(str(field(name) or "") for name in ("name", "type"))
        if intent_type == "meal":
            return bool(_DINING_TYPE_RE.search(provider_type))
        if intent_type == "campus_visit":
            return bool(_CAMPUS_TYPE_RE.search(provider_type))
        if intent_type == "night_view":
            if _NIGHT_VIEW_HARD_REJECT_RE.search(provider_text):
                return False
            policy_candidate = candidate
            if isinstance(candidate, dict) and candidate.get("amapId"):
                # Persisted itinerary POIs keep the canonical Provider identity
                # in ``amapId`` while ``id`` is the application's local POI id.
                # Rebind that server-owned shape before applying the shared
                # admission policy; never validate a local id as an AMap id.
                policy_candidate = {**candidate, "id": candidate.get("amapId")}
            policy = experience_policy if isinstance(experience_policy, dict) else {}
            raw_families = policy.get("experienceFamilies")
            families = (
                list(
                    dict.fromkeys(
                        str(item or "").strip().casefold()
                        for item in raw_families
                        if isinstance(item, str) and str(item or "").strip()
                    )
                )
                if isinstance(raw_families, list)
                else []
            )
            night_policy = NightViewCandidatePolicy()
            decisions = [
                night_policy.evaluate(
                    policy_candidate,
                    experience_family=family,
                    enforce_legacy_availability=False,
                )
                for family in families
            ] or [
                night_policy.evaluate(
                    policy_candidate,
                    enforce_legacy_availability=False,
                )
            ]
            accepted = any(decision.get("decision") == "accepted" for decision in decisions)
            access_policy = str(policy.get("accessPolicy") or "").strip().casefold()
            if access_policy:
                access_class = night_policy.access_class(policy_candidate)
                allowed_access_classes = {
                    "public_outdoor": {"public_outdoor"},
                    "verified_controlled_access": {"controlled_access"},
                    "public_outdoor_or_verified_controlled_access": {
                        "public_outdoor",
                        "controlled_access",
                    },
                }.get(access_policy)
                if allowed_access_classes is None or access_class not in allowed_access_classes:
                    return False
            # A scenic/public entity without intrinsic night wording may still
            # be retained as *provisional* for later suitability/opening checks.
            # Weak entities (for example photography studios), dining, invalid
            # identities, and incompatible Provider types are never materialized.
            return bool(
                accepted
                or (
                    not policy
                    and any(
                        decision.get("rejectReason") == "night_view_signal_missing"
                        and decision.get("publicAccessTypePassed") is True
                        for decision in decisions
                    )
                )
            )
        matcher = _INTENT_PROVIDER_TYPE_RE.get(intent_type)
        return bool(matcher and matcher.search(provider_type))

    @staticmethod
    def prepare_initial_context(request_context: dict[str, Any]) -> None:
        """Clear only strict-only ambiguity for a sufficient first request."""
        if request_context.get("serverExecutionProfile") != "simple_open_v1":
            return
        if request_context.get("activeVersionId"):
            return
        dates = request_context.get("resolvedTripDates")
        city = str(
            request_context.get("city")
            or request_context.get("destination")
            or request_context.get("selectedCity")
            or ""
        ).strip()
        if not isinstance(dates, dict) or dates.get("status") != "resolved" or not city:
            return
        checkpoint = request_context.get("clarificationCheckpoint")
        if isinstance(checkpoint, dict):
            unresolved_checkpoint_dimensions = [
                str(item) for item in checkpoint.get("unresolvedDimensions") or [] if str(item)
            ]
            unresolved_checkpoint_ambiguities = [
                item
                for item in checkpoint.get("ambiguities") or []
                if isinstance(item, dict) and item.get("resolved") is not True
            ]
            if (
                unresolved_checkpoint_dimensions
                or unresolved_checkpoint_ambiguities
                or str(checkpoint.get("status") or "")
                in {
                    "awaiting_answer",
                    "awaiting_agent_resolution",
                    "candidate_refresh_required",
                }
            ):
                return
        contract = request_context.get("requestIntentContract")
        if isinstance(contract, dict):
            unresolved_contract_dimensions = [
                str(item.get("dimensionId") or "")
                for item in contract.get("clarificationDimensions") or []
                if isinstance(item, dict)
                and str(item.get("status") or "unresolved") == "unresolved"
                and str(item.get("dimensionId") or "")
            ]
            if unresolved_contract_dimensions:
                # These are host-compiled material decisions (for example the
                # number and admission policy of required night-view goals),
                # not the legacy broad-field ambiguity this compatibility seam
                # is allowed to clear.
                contract["clarificationRequired"] = True
                if not str(contract.get("clarificationReason") or "").strip():
                    contract["clarificationReason"] = "material_clarification_unresolved"
                requirements = request_context.get("understoodRequirements")
                if isinstance(requirements, dict):
                    requirements["highImpactAmbiguityDetected"] = True
                request_context.pop("simpleOpenInitialRequestSufficient", None)
                return
            contract["clarificationRequired"] = False
            contract["clarificationReason"] = None
        requirements = request_context.get("understoodRequirements")
        if isinstance(requirements, dict):
            requirements["highImpactAmbiguityDetected"] = False
        request_context["simpleOpenInitialRequestSufficient"] = True

    @staticmethod
    def adapt_initial_decision(request_context: dict[str, Any], result: AgentDecisionResult) -> AgentDecisionResult:
        """Remove only strict Portfolio distribution rejection codes."""
        if (
            request_context.get("serverExecutionProfile") != "simple_open_v1"
            or request_context.get("simpleOpenInitialRequestSufficient") is not True
            or result.decision.primary_action != "draft_itinerary"
            or result.gated_decision.accepted
        ):
            return result
        reasons = list(result.gated_decision.policy_reason_codes)
        strict_only_codes = {
            "draft_goal_daily_cardinality_exceeded",
            "draft_goal_cardinality_underallocated",
            "draft_goal_cardinality_overallocated",
            "draft_goal_day_not_allowed",
            "draft_goal_distribution_invalid",
            "required_goal_omitted_from_planning_directive",
            "draft_soft_goal_misclassified_as_required",
        }
        if not reasons or any(reason not in strict_only_codes for reason in reasons):
            return result
        reasons.append("simple_open_server_profile_adapter")
        return replace(
            result,
            gated_decision=result.gated_decision.model_copy(
                update={"accepted": True, "policyReasonCodes": list(dict.fromkeys(reasons))}
            ),
        )

    @staticmethod
    def classify_result(planning_context: dict[str, Any], route_status: str) -> str:
        if int(planning_context.get("simpleOpenGroundedSegmentCount") or 0) <= 0:
            return "FAILED"
        if (
            int(planning_context.get("simpleOpenUnresolvedSegmentCount") or 0) > 0
            or int(planning_context.get("simpleOpenProvisionalSegmentCount") or 0) > 0
            or route_status != "ready"
        ):
            return "PARTIAL"
        return "READY"

    @staticmethod
    def apply_grounding_outcome(
        plans: list[PersistableSegmentPlan],
        pipeline_context: dict[str, Any],
        grounding_report: dict[str, Any],
    ) -> bool:
        """Project executor-owned grounding truth into shared writer context."""
        grounded_count = sum(1 for item in plans if item.selected_poi is not None)
        unresolved_count = sum(1 for item in plans if item.selected_poi is None)
        provisional_count = sum(1 for item in plans if item.grounding_status == "provisional")
        unresolved_days = sorted({item.day_number for item in plans if item.selected_poi is None})
        pipeline_context.update(
            {
                "simpleOpenNonBlockingRoutes": True,
                "simpleOpenGroundedSegmentCount": grounded_count,
                "simpleOpenUnresolvedSegmentCount": unresolved_count,
                "simpleOpenProvisionalSegmentCount": provisional_count,
            }
        )
        grounding_report["unresolvedDays"] = unresolved_days
        grounding_report["resultState"] = (
            "simple_open_failed_no_real_poi"
            if grounded_count == 0
            else "simple_open_partial"
            if unresolved_count
            else "simple_open_ready"
        )
        grounding_report["finalization"] = {
            **copy.deepcopy(dict(grounding_report.get("finalization") or {})),
            "canCreateVersion": grounded_count > 0,
            "canCreateActiveVersion": grounded_count > 0,
            "canCreateCompleteVersion": grounded_count > 0 and not unresolved_days,
            "draftPersistenceOverride": grounded_count > 0 and bool(unresolved_days),
            "unresolvedPolicy": ("persist_viable_partial_days" if grounded_count > 0 else "no_real_provider_identity"),
        }
        pipeline_context["candidateFirstGrounding"] = grounding_report
        return grounded_count > 0

    @staticmethod
    def enrichment_report(
        *,
        route_status: str,
        route_write_delta: int,
        active_version_id: str,
        map_verifier: dict[str, Any],
        map_ready: bool,
    ) -> dict[str, Any]:
        warning = {
            "provider_failed": "路线 Provider 调用失败；已保留真实地点和可编辑时间轴。",
            "partial": "仅部分相邻路线可用；未验证区间不展示虚假时长。",
            "unknown": "路线尚未验证；已保存高德地点身份与坐标，可在后续刷新路线。",
        }.get(route_status)
        return {
            "state": "simple_open_partial",
            "mapReady": map_ready,
            "routeReady": route_status == "ready",
            "routeStatus": route_status,
            "routeWriteDelta": route_write_delta,
            "activeVersionId": active_version_id,
            "simpleOpenNonBlockingRoutes": True,
            "mapVerifier": map_verifier,
            "warnings": [warning] if warning else [],
        }

    @staticmethod
    def terminal_event_specs(
        *, result_class: str, active_version_id: str, route_status: str, verifier_passed: bool
    ) -> list[dict[str, Any]]:
        terminal_status = "ready" if result_class == "READY" else "partial"
        return [
            {
                "eventId": "simple_open_result_classified",
                "label": "分类简单行程结果",
                "detail": (
                    "真实地点、相邻路线和可编辑时间轴均已保存，本轮结果为 READY。"
                    if result_class == "READY"
                    else "真实地点和可编辑时间轴已保存；未核验事实或路线缺口保持可见，因此结果为 PARTIAL。"
                ),
                "preview": {
                    "resultClass": result_class,
                    "activeVersionId": active_version_id,
                    "routeStatus": route_status,
                    "writeCount": 1,
                },
            },
            {
                "eventId": "simple_open_persist_reloaded",
                "label": "重读持久化结果",
                "detail": "已从 active snapshot 重读本轮唯一版本。",
                "preview": {"activeVersionId": active_version_id, "verifierPassed": verifier_passed},
            },
            {
                "eventId": "simple_open_terminal",
                "label": "完成简单初始行程",
                "detail": f"同一用户 turn 已返回可见、可编辑的 {result_class} 行程。",
                "preview": {"activeVersionId": active_version_id, "terminalStatus": terminal_status},
            },
        ]

    @staticmethod
    def persisted_reload_mismatches(
        *,
        expected_version_id: str,
        active_version_id: str | None,
        persisted_snapshot: dict[str, Any],
        live_snapshot: dict[str, Any],
    ) -> list[str]:
        mismatches: list[str] = []
        if active_version_id != expected_version_id:
            mismatches.append("activeVersionId")
        comparable_keys = ("id", "title", "city", "days")
        mismatches.extend(key for key in comparable_keys if persisted_snapshot.get(key) != live_snapshot.get(key))
        route_keys = (
            "id",
            "fromSegmentId",
            "toSegmentId",
            "fromPoiId",
            "toPoiId",
            "distanceMeters",
            "durationSeconds",
            "mode",
            "provider",
            "isSelected",
        )
        persisted_routes = [
            {key: route.get(key) for key in route_keys}
            for route in persisted_snapshot.get("routeOptions") or []
            if isinstance(route, dict)
        ]
        live_routes = [
            {key: route.get(key) for key in route_keys}
            for route in live_snapshot.get("routeOptions") or []
            if isinstance(route, dict)
        ]
        if persisted_routes != live_routes:
            mismatches.append("routeOptions")
        return mismatches

    @classmethod
    def persisted_reload_matches(cls, **kwargs: Any) -> bool:
        return not cls.persisted_reload_mismatches(**kwargs)

    @staticmethod
    def _photo_payload(photo: Any) -> dict[str, Any]:
        if hasattr(photo, "model_dump"):
            return photo.model_dump(mode="json", by_alias=True)
        return dict(photo) if isinstance(photo, dict) else {"url": str(photo)}

    @staticmethod
    def _query_fingerprint(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _tool_call_event(
        cls,
        slot_id: str | None,
        query: str,
        search_ordinal: int,
        search_budget: int,
        *,
        step_index: int,
    ) -> dict[str, Any]:
        return {
            "type": "simple_open_tool_call",
            "label": "调用高德地点搜索",
            "status": "running",
            "detail": query,
            "providerName": AMAP_PLACE_SOURCE,
            "metadata": {
                "executionProfile": "simple_open_v1",
                "stepIndex": step_index,
                "slotKey": slot_id,
                "queryFingerprint": cls._query_fingerprint(query),
                "cacheHit": None,
                "providerOutcome": "not_started",
                "resultCount": None,
                "selectedAmapId": None,
                "budgetBefore": {"poiSearchRemaining": search_budget - search_ordinal + 1},
                "budgetAfter": {"poiSearchRemaining": search_budget - search_ordinal},
            },
        }

    @classmethod
    def _slot_event(
        cls,
        *,
        slot_id: str | None,
        query: str,
        selected: POI | None,
        warning: str,
        grounding_status: str,
        step_index: int,
    ) -> dict[str, Any]:
        event_type = (
            "simple_open_slot_provisional"
            if grounding_status == "provisional"
            else "simple_open_slot_grounded"
            if selected is not None
            else "simple_open_slot_unresolved"
        )
        return {
            "type": event_type,
            "label": "简单路径地点绑定",
            "status": "completed" if selected is not None else "fallback",
            "detail": selected.name if selected is not None else warning,
            "providerName": AMAP_PLACE_SOURCE if selected is not None else None,
            "metadata": {
                "executionProfile": "simple_open_v1",
                "stepIndex": step_index,
                "slotKey": slot_id,
                "queryFingerprint": cls._query_fingerprint(query),
                "selectedAmapId": selected.amap_id if selected is not None else None,
                "groundingStatus": grounding_status,
                "resultStatus": "matched" if selected is not None else "unresolved",
            },
        }

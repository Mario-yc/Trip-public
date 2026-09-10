"""Consumer-scoped admission for shared, real AMap candidate evidence.

The shared universe may reuse provider facts. It may not reuse a prior brief's
semantic approval. This service is deliberately deterministic and read-only:
it composes the existing trust, visit-anchor and night-view policies and emits
an auditable report for one brief/pool/slot consumer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from types import SimpleNamespace
from typing import Any, Callable

from src.services.candidate_provider_evidence_service import (
    CandidateProviderEvidenceService,
)
from src.services.night_view_candidate_policy import NightViewCandidatePolicy
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.meal_candidate_quality_policy import MealCandidateQualityPolicy
from src.services.meal_grounding_policy import MEAL_EXPERIENCE_ACCESS_POLICY
from src.services.poi_trust_policy import PoiTrustPolicy
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.travel_visit_anchor_eligibility_policy import TravelVisitAnchorEligibilityPolicy


_AMAP_ID_RE = re.compile(r"B[0-9A-Z]{8,31}")
_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "盟", "市")
_MEAL_FAMILIES = {"meal", "local_food", "food"}
_STRONG_MEAL_FACT_RE = re.compile(
    r"老字号|指定|招牌|必吃|菜品|烤鸭|卤煮|炸酱面|涮肉|豆汁|营业|"
    r"开放|预约|订位|排队|堂食|外卖|米其林|黑珍珠"
)
_SIGNAL_TERMS = {
    "community_market": ("社区", "居民", "农贸市场", "菜市场", "日常采购"),
    "resident_activity": ("居民", "邻里", "社区活动", "日常生活"),
    "resident_daily_life": ("居民", "社区", "邻里", "日常"),
    "local_food": ("当地", "地方风味", "特色菜", "传统小吃", "老字号"),
    "heritage_context": ("历史", "文化遗产", "古迹", "传统建筑", "老街"),
    "market_activity": ("市场", "集市", "摊位", "商贩"),
    "art_context": ("艺术", "画廊", "展览", "创意园", "工作室"),
    "night_view": ("夜景", "夜游", "灯光", "夜间开放"),
    "museum_only": ("博物馆", "纪念馆"),
    "tourist_only": ("纯观光", "游客中心", "景区"),
    "high_commercialization": ("大型商场", "购物中心", "商业综合体", "连锁"),
}


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(value or "")).casefold()


class ConsumerCandidateAdmissionService:
    """Recompute candidate admission at the consumer boundary."""

    def __init__(
        self,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.trust_policy = PoiTrustPolicy()
        self.semantic_policy = IntentCandidateSemanticPolicy()
        self.meal_policy = MealCandidateQualityPolicy()
        self.anchor_policy = TravelVisitAnchorEligibilityPolicy()
        self.night_policy = NightViewCandidatePolicy()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def build_consumer_context(
        *,
        brief_id: object,
        pool_id: object,
        planning_slot_id: object,
        day_number: object,
        city: object,
        family: object,
        activity_mode: object,
        requirement_level: object,
        experience_shape: object,
        experience_goal: object,
        optional_experience_family: object = None,
        assigned_meal_family: object = None,
        desired_signals: Any = None,
        avoid_signals: Any = None,
        evidence_requirements: Any = None,
        grounding_policy: Any = None,
        route_context: Any = None,
        experience_spec_policy: Any = None,
        spec_fingerprint: object = "",
        intent_fingerprint: object = "",
        exact_entity: object = None,
        preferred_types: Any = None,
        rejected_types: Any = None,
    ) -> dict[str, Any]:
        route = dict(route_context or {})
        evidence_policy_invalid = evidence_requirements is not None and not isinstance(evidence_requirements, dict)
        grounding_policy_invalid = grounding_policy is not None and not isinstance(grounding_policy, dict)
        experience_policy_invalid = experience_spec_policy is not None and not isinstance(experience_spec_policy, dict)
        evidence = dict(evidence_requirements) if isinstance(evidence_requirements, dict) else {}
        grounding = dict(grounding_policy) if isinstance(grounding_policy, dict) else {}
        experience_policy = dict(experience_spec_policy) if isinstance(experience_spec_policy, dict) else {}
        policy_error = next(
            (
                reason
                for invalid, reason in (
                    (
                        experience_policy_invalid,
                        "experience_spec_policy_invalid",
                    ),
                    (
                        grounding_policy_invalid,
                        "experience_grounding_policy_invalid",
                    ),
                    (
                        evidence_policy_invalid,
                        "experience_evidence_policy_invalid",
                    ),
                )
                if invalid
            ),
            "",
        )
        if experience_policy_invalid:
            access_policy = None
            evidence_freshness = None
        else:
            access_policy = (
                experience_policy.get("accessPolicy")
                if "accessPolicy" in experience_policy
                else grounding.get("accessPolicy")
            )
            if "evidenceFreshness" in experience_policy:
                evidence_freshness = experience_policy.get("evidenceFreshness")
            elif "evidenceFreshness" in evidence:
                evidence_freshness = evidence.get("evidenceFreshness")
            else:
                evidence_freshness = evidence if access_policy is not None and "maxAgeHours" in evidence else None
        # Preserve the whole ExperienceSpec in the consumer scope.  The
        # access sub-policy alone is insufficient: a change to family,
        # distinctness, time window, or detour tolerance changes whether this
        # same physical POI is admissible for the slot.
        normalized_experience_policy: dict[str, Any] = dict(experience_policy)
        if normalized_experience_policy:
            normalized_experience_policy.setdefault("experienceFamily", str(family or ""))
        if access_policy is not None:
            normalized_experience_policy["accessPolicy"] = access_policy
        if evidence_freshness is not None:
            normalized_experience_policy["evidenceFreshness"] = (
                dict(evidence_freshness) if isinstance(evidence_freshness, dict) else evidence_freshness
            )
        policy_fingerprint_material = {
            key: value
            for key, value in normalized_experience_policy.items()
            if key not in {"specFingerprint", "experienceSpecFingerprint"}
        }
        resolved_spec_fingerprint = (
            _fingerprint(policy_fingerprint_material)
            if policy_fingerprint_material
            else str(
                spec_fingerprint
                or experience_policy.get("specFingerprint")
                or experience_policy.get("experienceSpecFingerprint")
                or grounding.get("specFingerprint")
                or grounding.get("experienceSpecFingerprint")
                or ""
            )
        )
        consumer = {
            "briefId": str(brief_id or ""),
            "poolId": str(pool_id or ""),
            "planningSlotId": str(planning_slot_id or ""),
            "slotId": str(planning_slot_id or ""),
            "dayNumber": day_number,
            "city": str(city or ""),
            "optionalExperienceFamily": (str(optional_experience_family or "") or None),
            "family": str(family or ""),
            "assignedMealFamily": assigned_meal_family or None,
            "activityMode": str(activity_mode or ""),
            "requirementLevel": str(requirement_level or "soft"),
            "experienceShape": str(experience_shape or "single_poi"),
            "experienceGoal": str(experience_goal or ""),
            "goal": str(experience_goal or ""),
            "desiredSignals": list(desired_signals or []),
            "avoidSignals": list(avoid_signals or []),
            "evidencePolicy": evidence,
            "evidenceRequirements": evidence,
            "groundingPolicy": grounding,
            "routeContext": route,
            "previousAnchor": route.get("previousAnchor"),
            "nextAnchor": route.get("nextAnchor"),
            "intentFingerprint": str(intent_fingerprint or ""),
            "exactEntity": exact_entity or None,
            "preferredTypes": list(preferred_types or []),
            "rejectedTypes": list(rejected_types or []),
        }
        if normalized_experience_policy or resolved_spec_fingerprint or policy_error:
            consumer.update(
                {
                    "experienceSpecPolicy": (
                        {"invalidType": type(experience_spec_policy).__name__}
                        if experience_policy_invalid
                        else normalized_experience_policy
                    ),
                    "experienceSpecPolicyError": policy_error or None,
                    "accessPolicy": access_policy,
                    "evidenceFreshness": (
                        dict(evidence_freshness) if isinstance(evidence_freshness, dict) else evidence_freshness
                    ),
                    "specFingerprint": resolved_spec_fingerprint,
                }
            )
        return consumer

    @staticmethod
    def _experience_spec_contract(consumer: dict[str, Any]) -> dict[str, Any]:
        raw_policy = consumer.get("experienceSpecPolicy")
        policy_present = "experienceSpecPolicy" in consumer
        policy = dict(raw_policy) if isinstance(raw_policy, dict) else {}
        grounding_values = [consumer.get(key) for key in ("groundingPolicy", "groundingContract") if key in consumer]
        evidence_values = [consumer.get(key) for key in ("evidencePolicy", "evidenceRequirements") if key in consumer]
        grounding = next(
            (dict(value) for value in grounding_values if isinstance(value, dict)),
            {},
        )
        evidence = next(
            (dict(value) for value in evidence_values if isinstance(value, dict)),
            {},
        )
        error_reason = str(consumer.get("experienceSpecPolicyError") or "")
        if not error_reason and policy_present and not isinstance(raw_policy, dict):
            error_reason = "experience_spec_policy_invalid"
        if not error_reason and any(not isinstance(value, dict) for value in grounding_values):
            error_reason = "experience_grounding_policy_invalid"
        if not error_reason and any(not isinstance(value, dict) for value in evidence_values):
            error_reason = "experience_evidence_policy_invalid"
        unresolved = policy.get("unresolvedDimensions")
        if not error_reason and unresolved is not None and (
            not isinstance(unresolved, list) or bool(unresolved)
        ):
            error_reason = "experience_spec_unresolved"
        access_policy = consumer.get("accessPolicy")
        if access_policy is None:
            access_policy = policy.get("accessPolicy")
        if access_policy is None:
            access_policy = grounding.get("accessPolicy")
        evidence_freshness = consumer.get("evidenceFreshness")
        if evidence_freshness is None:
            evidence_freshness = policy.get("evidenceFreshness")
        if evidence_freshness is None:
            evidence_freshness = evidence.get("evidenceFreshness")
        if evidence_freshness is None and access_policy is not None and "maxAgeHours" in evidence:
            evidence_freshness = evidence
        spec_fingerprint = str(
            consumer.get("specFingerprint")
            or consumer.get("experienceSpecFingerprint")
            or policy.get("specFingerprint")
            or policy.get("experienceSpecFingerprint")
            or grounding.get("specFingerprint")
            or grounding.get("experienceSpecFingerprint")
            or ""
        ).strip()
        material_policy_fields = (
            "accessPolicy",
            "distinctnessPolicy",
            "timeWindow",
            "detourTolerance",
            "evidenceFreshness",
            "confidence",
        )
        active = bool(
            error_reason
            or access_policy not in (None, "")
            or evidence_freshness not in (None, {}, "")
            or any(
                policy.get(field) not in (None, "", {}, [])
                for field in material_policy_fields
            )
        )
        return {
            "active": active,
            "errorReason": error_reason,
            "accessPolicy": access_policy,
            "evidenceFreshness": (
                dict(evidence_freshness) if isinstance(evidence_freshness, dict) else evidence_freshness
            ),
            "specFingerprint": spec_fingerprint,
        }

    def evaluate(self, candidate: dict[str, Any], consumer: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate or {})
        consumer = dict(consumer or {})
        experience_contract = self._experience_spec_contract(consumer)
        experience_contract_active = bool(experience_contract["active"])
        if experience_contract_active:
            if not experience_contract["errorReason"]:
                copy_policy = dict(consumer.get("experienceSpecPolicy") or {})
                copy_policy.update(
                    {
                        "accessPolicy": experience_contract["accessPolicy"],
                        "evidenceFreshness": experience_contract["evidenceFreshness"],
                    }
                )
                consumer["experienceSpecPolicy"] = copy_policy
            consumer.update(
                {
                    "experienceSpecPolicyError": experience_contract["errorReason"] or None,
                    "accessPolicy": experience_contract["accessPolicy"],
                    "evidenceFreshness": experience_contract["evidenceFreshness"],
                    "specFingerprint": experience_contract["specFingerprint"],
                }
            )
        family = str(consumer.get("family") or consumer.get("optionalExperienceFamily") or "").strip()
        policy_family = "meal" if family in _MEAL_FAMILIES else family
        shape = str(consumer.get("experienceShape") or "single_poi").strip()
        consumer_scope_keys = [
            "briefId",
            "poolId",
            "planningSlotId",
            "slotId",
            "dayNumber",
            "city",
            "optionalExperienceFamily",
            "family",
            "assignedMealFamily",
            "activityMode",
            "requirementLevel",
            "experienceShape",
            "goal",
            "experienceGoal",
            "desiredSignals",
            "avoidSignals",
            "intentFingerprint",
            "evidenceRequirements",
            "evidencePolicy",
            "groundingContract",
            "groundingPolicy",
            "routeContract",
            "routeContext",
            "previousAnchor",
            "nextAnchor",
            "exactEntity",
            "preferredTypes",
            "rejectedTypes",
        ]
        if experience_contract_active:
            consumer_scope_keys.extend(
                (
                    "experienceSpecPolicy",
                    "accessPolicy",
                    "evidenceFreshness",
                    "specFingerprint",
                    "experienceSpecPolicyError",
                )
            )
        consumer_scope = {key: consumer.get(key) for key in consumer_scope_keys}
        consumer_fingerprint = _fingerprint(consumer_scope)
        provider_evidence = CandidateProviderEvidenceService.project(candidate)
        if experience_contract_active:
            provider_evidence["accessEvidence"] = self._experience_access_evidence_projection(
                candidate,
                policy_family=policy_family,
            )
        report = {
            "schemaVersion": "consumer-candidate-admission-v2",
            "classification": "rejected",
            "hardGatePassed": False,
            "evidenceSufficient": False,
            "scoreEligible": False,
            "reasonCodes": [],
            "consumerFingerprint": consumer_fingerprint,
            "consumerIntentFingerprint": consumer_fingerprint,
            "candidateEvidenceFingerprint": _fingerprint(provider_evidence),
            "evidenceUsed": provider_evidence,
            "evidenceSummary": provider_evidence,
            "sourceScope": {
                "briefId": candidate.get("briefId"),
                "poolId": candidate.get("poolId"),
                "planningSlotId": candidate.get("planningSlotId"),
                "sourcePrecheck": candidate.get("sourcePrecheck"),
            },
            "consumerScope": consumer_scope,
            "gateResults": [],
            "briefId": consumer.get("briefId"),
            "poolId": consumer.get("poolId"),
            "slotId": consumer.get("slotId"),
        }

        def gate(name: str, passed: bool, reason: str = "", **metadata: Any) -> None:
            row = {"gate": name, "passed": bool(passed)}
            if reason:
                row["reasonCode"] = reason
            row.update(metadata)
            report["gateResults"].append(row)

        identity_reason = self._identity_reason(candidate, expected_city=consumer.get("city"))
        if identity_reason:
            gate("amap_identity", False, identity_reason)
            return self._reject(report, identity_reason)
        gate("amap_identity", True)
        stale_report = candidate.get("consumerAdmissionReport")
        if (
            isinstance(stale_report, dict)
            and str(stale_report.get("consumerFingerprint") or "") != consumer_fingerprint
        ):
            gate("consumer_fingerprint_freshness", True, staleAdmissionInvalidated=True)
        else:
            gate("consumer_fingerprint_freshness", True)
        if self.trust_policy.is_mock_or_synthetic_poi_values(
            source=candidate.get("source"),
            amap_id=candidate.get("amapId") or candidate.get("id"),
            source_note=candidate.get("sourceNote"),
            name=candidate.get("name"),
            kind="meal" if policy_family == "meal" else "visit",
            intent_type=policy_family,
        ):
            gate("exact_entity_binding", False, "candidate_not_trusted")
            return self._reject(report, "candidate_not_trusted")

        exact_entity = str(consumer.get("exactEntity") or "").strip()
        if exact_entity and not self._matches_exact_entity(candidate, exact_entity):
            gate("exact_entity_binding", False, "exact_entity_mismatch")
            return self._reject(report, "exact_entity_mismatch")
        gate("exact_entity_binding", True)

        provider_text = _text(
            " ".join(
                [
                    str(candidate.get("type") or ""),
                    str(candidate.get("category") or ""),
                    *(str(item) for item in candidate.get("tags") or []),
                ]
            )
        )
        rejected_types = [_text(item) for item in consumer.get("rejectedTypes") or []]
        rejected_match = next(
            (item for item in rejected_types if item and item in provider_text),
            "",
        )
        structured_evidence_count = sum(
            1
            for present in (
                bool(candidate.get("providerTypeCode")),
                bool(candidate.get("type") or candidate.get("category")),
                bool(candidate.get("tags")),
            )
            if present
        )
        report["evidenceSummary"] = {
            "structuredEvidenceCount": structured_evidence_count,
            "providerTypeCodePresent": bool(candidate.get("providerTypeCode")),
            "sourceClaimCount": len(provider_evidence["sourceClaims"]),
        }
        if rejected_match:
            gate("provider_type", False, "provider_type_rejected")
            return self._reject(report, "provider_type_rejected")
        if not self._has_structured_provider_evidence(provider_evidence):
            gate("provider_type", False, "structured_provider_evidence_missing")
            report["classification"] = "pending_evidence"
            report["reasonCodes"] = ["structured_provider_evidence_missing"]
            report["evidenceSummary"]["nameOnlyPositiveSignal"] = bool(candidate.get("name"))
            return self._finalize(report)
        gate("provider_type", True)

        if shape not in {"single_poi", "area", "micro_route", "open_walk"}:
            gate("experience_shape_compatibility", False, "experience_shape_unsupported")
            return self._reject(report, "experience_shape_unsupported")
        route_context = consumer.get("routeContext") or consumer.get("routeContract") or {}
        boundary_anchors = route_context.get("boundaryAnchors") or []
        has_open_walk_geometry = bool(route_context.get("geometry"))
        has_open_walk_endpoints = bool(route_context.get("startAnchor") and route_context.get("endAnchor"))
        has_open_walk_boundaries = isinstance(boundary_anchors, list) and len(boundary_anchors) >= 2
        if shape == "open_walk" and not (has_open_walk_geometry or has_open_walk_endpoints or has_open_walk_boundaries):
            gate("experience_shape_compatibility", False, "open_walk_boundary_evidence_missing")
            report["classification"] = "pending_evidence"
            report["reasonCodes"] = ["open_walk_boundary_evidence_missing"]
            return self._finalize(report)
        if shape == "micro_route":
            member_count = int(consumer.get("admittedMemberCount") or 0)
            if not (2 <= member_count <= 4 and consumer.get("setLevelCoveragePassed") is True):
                gate("experience_shape_compatibility", False, "micro_route_set_evidence_missing")
                report["classification"] = "pending_evidence"
                report["reasonCodes"] = ["micro_route_set_evidence_missing"]
                return self._finalize(report)
        gate("experience_shape_compatibility", True)

        excluded = {term for item in consumer.get("avoidSignals") or [] for term in self._signal_terms(item)}
        intrinsic = _text(" ".join(self._intrinsic_values(candidate)))
        if any(signal and signal in intrinsic for signal in excluded):
            gate("semantic_affordance", False, "consumer_avoid_signal_matched")
            return self._reject(report, "consumer_avoid_signal_matched")

        semantic_candidate = {
            **candidate,
            # Recall provenance cannot become target-consumer authorization.
            "sourceNote": "",
            "source_note": "",
        }
        intent_type = str(consumer.get("activityMode") or family or "experience")
        semantic = self.semantic_policy.evaluate(
            intent_type,
            semantic_candidate,
            raw_need=str(consumer.get("experienceGoal") or consumer.get("goal") or ""),
            exact_entity=exact_entity or None,
            optional_experience_family=(
                str(consumer.get("optionalExperienceFamily") or family)
                if intent_type in {"area_walk", "experience", "park"}
                else ""
            ),
        )
        report["evidenceUsed"]["semanticPolicy"] = semantic.to_camel_dict()
        if not semantic.passed:
            gate("semantic_affordance", False, semantic.reason_code)
            return self._reject(report, semantic.reason_code)
        gate("semantic_affordance", True, semantic.reason_code)

        eligibility = self.anchor_policy.evaluate(candidate, family=policy_family)
        if eligibility.classification == "area_seed_only":
            gate("anchor_eligibility", False, eligibility.reason_code)
            report["classification"] = "area_seed_only"
            report["reasonCodes"] = [eligibility.reason_code]
            return self._finalize(report)
        if eligibility.classification != "final_visit_anchor":
            gate("anchor_eligibility", False, eligibility.reason_code)
            return self._reject(report, eligibility.reason_code)
        gate("anchor_eligibility", True, eligibility.reason_code)

        activity_mode = str(consumer.get("activityMode") or "").strip().casefold()
        is_night_view = family in {"night", "night_view", "photo_night", "public_city_view"} or activity_mode in {
            "night",
            "night_view",
            "photo_night",
        }
        if experience_contract_active:
            if policy_family == "meal":
                access_result = self._evaluate_meal_access_policy(
                    candidate,
                    access_policy=experience_contract["accessPolicy"],
                    evidence_freshness=experience_contract["evidenceFreshness"],
                    spec_fingerprint=experience_contract["specFingerprint"],
                    contract_error=experience_contract["errorReason"],
                )
            elif is_night_view:
                access_result = self.night_policy.evaluate_access_policy(
                    candidate,
                    access_policy=experience_contract["accessPolicy"],
                    evidence_freshness=experience_contract["evidenceFreshness"],
                    now=self._now_provider(),
                    spec_fingerprint=experience_contract["specFingerprint"],
                    contract_error=experience_contract["errorReason"],
                )
                access_result["policyFamily"] = "night_view"
            else:
                access_result = {
                    "decision": "rejected",
                    "reasonCode": "experience_access_policy_family_unsupported",
                    "accessPolicy": experience_contract["accessPolicy"],
                    "policyFamily": policy_family or "unknown",
                    "specFingerprint": experience_contract["specFingerprint"],
                }
            report["experienceAccessPolicy"] = access_result
            report["evidenceSummary"]["experienceAccessClass"] = access_result.get("accessClass")
            report["evidenceSummary"]["experienceAccessEvidenceAgeHours"] = access_result.get("evidenceAgeHours")
            if access_result.get("decision") != "accepted":
                reason = str(access_result.get("reasonCode") or "experience_access_policy_rejected")
                gate(
                    "specialized_policy",
                    False,
                    reason,
                    experienceAccessPolicy=dict(access_result),
                )
                report["reasonCodes"] = [reason]
                if access_result.get("decision") == "pending_evidence":
                    report["classification"] = "pending_evidence"
                    return self._finalize(report)
                return self._reject(report, reason)

        authoritative_amap_local_food_subtype = False
        if policy_family == "meal":
            assigned = str(consumer.get("assignedMealFamily") or exact_entity).strip()
            if assigned and not self._matches_exact_entity(candidate, assigned):
                gate("specialized_policy", False, "meal_family_mismatch")
                return self._reject(report, "meal_family_mismatch")
            meal = self.meal_policy.evaluate(
                str(consumer.get("experienceGoal") or consumer.get("goal") or ""),
                [assigned] if assigned else [],
                candidate,
                city=str(consumer.get("city") or ""),
            )
            report["evidenceUsed"]["mealQualityPolicy"] = {
                "acceptable": meal.acceptable,
                "localRelevanceScore": meal.local_relevance_score,
                "hardRejectReasons": list(meal.hard_reject_reasons),
                "softReasons": list(meal.soft_reasons),
            }
            authoritative_amap_local_food_subtype = "authoritative_amap_local_food_subtype" in meal.soft_reasons
            report["evidenceSummary"]["authoritativeAmapLocalFoodSubtype"] = authoritative_amap_local_food_subtype
            if not meal.acceptable:
                reason = str((meal.hard_reject_reasons or ["meal_evidence_pending"])[0])
                gate("specialized_policy", False, reason)
                if reason == "local_food_evidence_missing":
                    report["classification"] = "pending_evidence"
                    report["reasonCodes"] = [reason]
                    return self._finalize(report)
                return self._reject(report, reason)
        if is_night_view:
            experience_spec_policy = consumer.get("experienceSpecPolicy")
            experience_family = (
                str(experience_spec_policy.get("experienceFamily") or family)
                if isinstance(experience_spec_policy, dict)
                else family
            )
            night = self.night_policy.evaluate(
                SimpleNamespace(**self._attribute_candidate(candidate)),
                amap_identity=candidate.get("amapId") or candidate.get("id"),
                structured_hint=exact_entity or None,
                experience_family=experience_family,
                enforce_legacy_availability=not experience_contract_active,
            )
            report["evidenceUsed"]["nightViewPolicy"] = night
            if night.get("decision") != "accepted":
                gate("specialized_policy", False, str(night.get("rejectReason") or "night_view_policy_rejected"))
                return self._reject(report, str(night.get("rejectReason") or "night_view_policy_rejected"))
        gate("specialized_policy", True)

        claims = [item for item in provider_evidence["sourceClaims"] if isinstance(item, dict)]
        supporting_claims = [
            item
            for item in claims
            if str(item.get("stance") or "") == "support"
            and self._claim_matches_consumer(item, family=family, consumer=consumer)
        ]
        contradictions = [item for item in claims if str(item.get("stance") or "") in {"contradict", "contradiction"}]
        independent_sources = {
            str(item.get("sourceUrlHash") or item.get("sourceName") or "")
            for item in supporting_claims
            if str(item.get("sourceUrlHash") or item.get("sourceName") or "")
        }
        policy = consumer.get("evidenceRequirements") or consumer.get("evidencePolicy") or {}
        default_claim_minimum = (
            1
            if family
            in {
                "local_life",
                "heritage_walk",
                "market_walk",
                "art_walk",
                "meal",
                "local_food",
                "food",
            }
            else 0
        )
        minimum_claims = int(
            policy.get("minimumIndependentClaims") or policy.get("minimumIndependentSources") or default_claim_minimum
        )
        generic_amap_meal_evidence = bool(
            policy_family == "meal"
            and authoritative_amap_local_food_subtype
            and self._is_generic_local_meal_request(consumer)
        )
        if generic_amap_meal_evidence:
            minimum_claims = 0
        report["evidenceSummary"]["amapSubtypeSatisfiedGenericLocalMeal"] = generic_amap_meal_evidence
        evidence_sufficient = len(independent_sources) >= minimum_claims and not (
            contradictions and not supporting_claims
        )
        report["evidenceSummary"].update(
            {
                "independentSourceCount": len(independent_sources),
                "supportingClaimCount": len(supporting_claims),
                "relevantSupportingClaimCount": len(supporting_claims),
                "contradictionClaimCount": len(contradictions),
            }
        )
        report["evidenceSufficient"] = evidence_sufficient
        if not evidence_sufficient:
            gate("evidence_sufficiency", False, "consumer_evidence_insufficient")
            report["classification"] = "pending_evidence"
            report["reasonCodes"] = ["consumer_evidence_insufficient"]
            return self._finalize(report)
        gate("evidence_sufficiency", True)

        max_detour = route_context.get("maxDetourMinutes") if isinstance(route_context, dict) else None
        actual_detour = candidate.get("routeDetourMinutes") or candidate.get("detourMinutes")
        coarse_detour_exceeded = False
        if max_detour is not None and actual_detour is not None:
            try:
                coarse_detour_exceeded = float(actual_detour) > float(max_detour)
            except (TypeError, ValueError):
                coarse_detour_exceeded = False
        # Candidate metadata is not an insertion matrix: it has no exact
        # previous/candidate/next legs, old baseline, time window, or dynamic
        # mobility cost.  Preserve it as a discovery diagnostic only.  The
        # portfolio feasibility service and the guarded writer make the final
        # route decision from ProviderRouteInsertionService evidence.
        gate(
            "route_schedule_context",
            True,
            routeEvidenceDeferred=True,
            coarseDetourExceeded=coarse_detour_exceeded,
            coarseDetourMinutes=actual_detour,
            maxDetourMinutes=max_detour,
            decisiveRouteEvidence="provider_route_matrix_required",
        )

        requirement_level = str(consumer.get("requirementLevel") or "soft")
        if (
            requirement_level == "required"
            and not str(consumer.get("goal") or consumer.get("experienceGoal") or "").strip()
        ):
            gate("required_goal_coverage", False, "required_goal_contract_missing")
            return self._reject(report, "required_goal_contract_missing")
        gate("required_goal_coverage", True)
        report["hardGatePassed"] = True
        report["classification"] = "admitted_final_anchor" if shape == "single_poi" else "admitted_anchor_set_member"
        report["scoreEligible"] = True
        report["scoreComponents"] = {
            "evidenceStrength": min(1.0, (structured_evidence_count + len(supporting_claims)) / 4),
            "sourceFreshness": self._source_freshness_score(supporting_claims),
            "localDistinctiveness": self._local_distinctiveness_score(candidate, supporting_claims),
            "userIntentFit": self._desired_signal_fit(candidate, supporting_claims, consumer),
            "uncertaintyPenalty": 0.0 if evidence_sufficient else 1.0,
        }
        report["reasonCodes"] = ["consumer_admission_passed"]
        return self._finalize(report)

    def _is_generic_local_meal_request(self, consumer: dict[str, Any]) -> bool:
        if str(consumer.get("assignedMealFamily") or "").strip():
            return False
        if str(consumer.get("exactEntity") or "").strip():
            return False
        goal = str(consumer.get("experienceGoal") or consumer.get("goal") or "").strip()
        if not self.meal_policy.requires_local_food(goal):
            return False
        return _STRONG_MEAL_FACT_RE.search(goal) is None

    @classmethod
    def _reject(cls, report: dict[str, Any], reason: str) -> dict[str, Any]:
        report["classification"] = "rejected"
        report["reasonCodes"] = [reason]
        return cls._finalize(report)

    @staticmethod
    def _finalize(report: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in report.items() if key != "reportFingerprint"}
        report["reportFingerprint"] = _fingerprint(payload)
        return report

    @staticmethod
    def validate_report(report: dict[str, Any]) -> bool:
        if not isinstance(report, dict) or report.get("schemaVersion") != "consumer-candidate-admission-v2":
            return False
        report_payload = {key: value for key, value in report.items() if key != "reportFingerprint"}
        if str(report.get("reportFingerprint") or "") != _fingerprint(report_payload):
            return False
        evidence = dict(report.get("evidenceUsed") or {})
        for key in ("semanticPolicy", "mealQualityPolicy", "nightViewPolicy"):
            evidence.pop(key, None)
        if str(report.get("candidateEvidenceFingerprint") or "") != _fingerprint(evidence):
            return False
        return str(report.get("consumerFingerprint") or "") == _fingerprint(report.get("consumerScope") or {})

    @classmethod
    def report_matches_poi(
        cls,
        report: dict[str, Any],
        poi: dict[str, Any],
    ) -> bool:
        evidence = report.get("evidenceUsed")
        if not isinstance(evidence, dict) or not isinstance(poi, dict):
            return False
        expected = dict(evidence)
        for key in ("semanticPolicy", "mealQualityPolicy", "nightViewPolicy"):
            expected.pop(key, None)
        actual = CandidateProviderEvidenceService.project(poi)
        if "accessEvidence" in expected:
            access_policy = report.get("experienceAccessPolicy")
            policy_family = (
                str(access_policy.get("policyFamily") or "")
                if isinstance(access_policy, dict)
                else ""
            )
            actual["accessEvidence"] = cls._experience_access_evidence_projection(
                poi,
                policy_family=policy_family,
            )
        return _fingerprint(expected) == _fingerprint({key: actual.get(key) for key in expected})

    @staticmethod
    def _experience_access_evidence_projection(
        candidate: dict[str, Any],
        *,
        policy_family: str,
    ) -> dict[str, Any]:
        if str(policy_family or "").strip() == "meal":
            return {
                "source": candidate.get("source"),
                "providerTypeCode": candidate.get("providerTypeCode")
                or candidate.get("provider_type_code"),
                "providerEvidenceQueriedAt": candidate.get("providerEvidenceQueriedAt")
                or candidate.get("provider_evidence_queried_at"),
                "openTimeToday": candidate.get("openTimeToday")
                or candidate.get("open_time_today"),
                "businessStatus": candidate.get("businessStatus")
                or candidate.get("business_status"),
            }
        return NightViewCandidatePolicy.access_evidence_projection(candidate)

    def _evaluate_meal_access_policy(
        self,
        candidate: dict[str, Any],
        *,
        access_policy: Any,
        evidence_freshness: Any,
        spec_fingerprint: str,
        contract_error: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "decision": "rejected",
            "reasonCode": None,
            "accessPolicy": access_policy,
            "policyFamily": "meal",
            "accessClass": None,
            "evidenceTimestamp": None,
            "evidenceAgeHours": None,
            "policyExemption": None,
            "specFingerprint": spec_fingerprint,
        }
        if contract_error:
            result["reasonCode"] = contract_error
            return result
        if access_policy != MEAL_EXPERIENCE_ACCESS_POLICY:
            result["reasonCode"] = "experience_access_policy_unknown"
            return result
        if not isinstance(evidence_freshness, dict) or not evidence_freshness:
            result["reasonCode"] = "experience_evidence_freshness_invalid"
            return result
        allowed_freshness = {
            "maxAgeHours",
            "requiredForControlledAccess",
            "requiredForPublicOutdoor",
            "allowExplicitNoClosure",
        }
        max_age = evidence_freshness.get("maxAgeHours")
        if (
            not set(evidence_freshness).issubset(allowed_freshness)
            or not isinstance(max_age, (int, float))
            or isinstance(max_age, bool)
            or not math.isfinite(float(max_age))
            or float(max_age) <= 0
            or any(
                key in evidence_freshness and not isinstance(evidence_freshness[key], bool)
                for key in allowed_freshness - {"maxAgeHours"}
            )
        ):
            result["reasonCode"] = "experience_evidence_freshness_invalid"
            return result

        source = str(candidate.get("source") or "")
        provider_type_code = str(
            candidate.get("providerTypeCode")
            or candidate.get("provider_type_code")
            or ""
        )
        if source != "amap-place-search" or not provider_type_code.startswith("05"):
            result["reasonCode"] = "experience_access_policy_mismatch"
            return result
        result["accessClass"] = "controlled_food_service"

        access_state = " ".join(
            str(
                candidate.get(key)
                or candidate.get(
                    {
                        "openTimeToday": "open_time_today",
                        "businessStatus": "business_status",
                    }.get(key, "")
                )
                or ""
            )
            for key in ("openTimeToday", "businessStatus")
        )
        if re.search(r"暂停|关闭|停业|歇业|closed|suspended", access_state, re.IGNORECASE):
            result["reasonCode"] = "experience_access_explicitly_closed"
            return result

        if evidence_freshness.get("requiredForControlledAccess") is not True:
            result.update(
                {
                    "decision": "accepted",
                    "reasonCode": "experience_access_policy_passed",
                    "policyExemption": "amap_food_service_opening_evidence_not_required",
                }
            )
            return result

        timestamp = candidate.get("providerEvidenceQueriedAt") or candidate.get(
            "provider_evidence_queried_at"
        )
        if not timestamp or not access_state.strip():
            result["decision"] = "pending_evidence"
            result["reasonCode"] = "experience_access_evidence_missing"
            return result
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            now = self._now_provider()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - parsed).total_seconds() / 3600.0)
        except (TypeError, ValueError, OverflowError):
            result["decision"] = "pending_evidence"
            result["reasonCode"] = "experience_access_evidence_missing"
            return result
        result["evidenceTimestamp"] = parsed.isoformat()
        result["evidenceAgeHours"] = round(age_hours, 6)
        if age_hours > float(max_age):
            result["decision"] = "pending_evidence"
            result["reasonCode"] = "experience_access_evidence_stale"
            return result
        result["decision"] = "accepted"
        result["reasonCode"] = "experience_access_policy_passed"
        return result

    @staticmethod
    def _identity_reason(candidate: dict[str, Any], *, expected_city: Any = None) -> str:
        if str(candidate.get("source") or "") != "amap-place-search":
            return "amap_source_missing"
        amap_id = str(candidate.get("amapId") or candidate.get("id") or "").strip().upper()
        if not _AMAP_ID_RE.fullmatch(amap_id):
            return "amap_identity_missing"
        if PoiPhysicalIdentityService.invalid_parent_id(candidate):
            return "amap_parent_identity_invalid"
        if not str(candidate.get("city") or "").strip():
            return "amap_city_missing"
        if str(expected_city or "").strip() and ConsumerCandidateAdmissionService._city_key(
            candidate.get("city")
        ) != ConsumerCandidateAdmissionService._city_key(expected_city):
            return "amap_city_mismatch"
        try:
            longitude = float(candidate.get("longitude"))
            latitude = float(candidate.get("latitude"))
        except (TypeError, ValueError):
            return "amap_coordinates_missing"
        if not (
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and longitude
            and latitude
        ):
            return "amap_coordinates_invalid"
        return ""

    @staticmethod
    def _city_key(value: Any) -> str:
        city = re.sub(r"\s+", "", str(value or "")).casefold()
        for suffix in _CITY_SUFFIXES:
            if city.endswith(suffix) and len(city) > len(suffix):
                return city[: -len(suffix)]
        return city

    @classmethod
    def _matches_exact_entity(cls, candidate: dict[str, Any], expected: str) -> bool:
        needle = _text(expected)
        if not needle:
            return True
        return any(
            needle in value or value in needle
            for value in (
                _text(item)
                for item in [
                    candidate.get("name"),
                    *(candidate.get("aliases") or []),
                ]
            )
            if len(value) >= 2
        )

    @staticmethod
    def _intrinsic_values(candidate: dict[str, Any]) -> list[str]:
        return [
            str(candidate.get("name") or ""),
            str(candidate.get("type") or ""),
            str(candidate.get("category") or ""),
            *(str(item or "") for item in candidate.get("tags") or []),
        ]

    @staticmethod
    def _signal_terms(signal: Any) -> tuple[str, ...]:
        key = str(signal or "").strip().casefold()
        values = _SIGNAL_TERMS.get(key, (str(signal or ""),))
        return tuple(_text(item) for item in values if _text(item))

    @classmethod
    def _claim_matches_consumer(
        cls,
        claim: dict[str, Any],
        *,
        family: str,
        consumer: dict[str, Any],
    ) -> bool:
        claim_key = str(claim.get("claimKey") or claim.get("claimType") or "").casefold()
        family_keys = {
            "meal": {"meal", "local_food", "food"},
            "local_food": {"meal", "local_food", "food"},
            "food": {"meal", "local_food", "food"},
            "local_life": {"local_life", "community_market", "resident_activity", "resident_market"},
            "heritage_walk": {"heritage", "heritage_walk", "heritage_context"},
            "market_walk": {"market", "market_walk", "market_activity"},
            "art_walk": {"art", "art_walk", "art_context"},
            "night_view": {"night", "night_view"},
        }.get(family, {family})
        if any(key and key in claim_key for key in family_keys):
            return True
        claim_text = _text(
            " ".join(
                [
                    str(claim.get("summary") or ""),
                    str(claim.get("supportedSignals") or ""),
                ]
            )
        )
        desired = consumer.get("desiredSignals") or []
        return any(term and term in claim_text for signal in desired for term in cls._signal_terms(signal))

    @classmethod
    def _desired_signal_fit(
        cls,
        candidate: dict[str, Any],
        claims: list[dict[str, Any]],
        consumer: dict[str, Any],
    ) -> float:
        desired = list(consumer.get("desiredSignals") or [])
        if not desired:
            return 0.5
        text = _text(" ".join(cls._intrinsic_values(candidate) + [str(item.get("summary") or "") for item in claims]))
        matched = sum(1 for signal in desired if any(term and term in text for term in cls._signal_terms(signal)))
        return matched / len(desired)

    @staticmethod
    def _source_freshness_score(claims: list[dict[str, Any]]) -> float:
        if not claims:
            return 0.0
        known = sum(1 for item in claims if str(item.get("freshness") or "") not in {"", "unknown"})
        return known / len(claims)

    @staticmethod
    def _local_distinctiveness_score(candidate: dict[str, Any], claims: list[dict[str, Any]]) -> float:
        text = _text(
            " ".join(
                [
                    str(candidate.get("businessArea") or ""),
                    *(str(item) for item in candidate.get("tags") or []),
                    *(str(item.get("summary") or "") for item in claims),
                ]
            )
        )
        return 1.0 if any(_text(item) in text for item in ("当地", "居民", "社区", "传统", "老字号")) else 0.5

    @staticmethod
    def _has_structured_provider_evidence(evidence: dict[str, Any]) -> bool:
        return bool(evidence.get("providerTypeCode") and (evidence.get("type") or evidence.get("tags")))

    @staticmethod
    def _attribute_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        value = dict(candidate)
        value["amap_id"] = candidate.get("amapId") or candidate.get("id")
        value["source_note"] = candidate.get("sourceNote") or ""
        value.setdefault("aliases", [])
        return value

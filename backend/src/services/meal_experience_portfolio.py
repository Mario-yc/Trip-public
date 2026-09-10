"""Evidence-bound meal themes shared by Simple Open and strict Portfolio.

The model may propose bounded search hypotheses.  It never proves that a dish
is local and never selects a restaurant.  A meal theme becomes material only
after a real AMap candidate (or an already-bound supporting claim) contains a
matching term.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MEAL_EXPERIENCE_MODES = frozenset(
    {
        "signature_dish",
        "neighborhood_home_style",
        "traditional_snack",
        "market_food",
        "heritage_dining",
        "light_restorative_meal",
    }
)

_GENERIC_MEAL_TERMS = frozenset(
    {
        "餐饮",
        "餐厅",
        "餐馆",
        "美食",
        "特色美食",
        "当地美食",
        "当地特色美食",
        "当地特色餐厅",
        "地方风味餐厅",
        "中餐",
        "中餐厅",
        "餐饮服务",
    }
)
_BRANCH_RE = re.compile(
    r"(?:\([^)]*(?:店|门店|分店|总店|旗舰店|广场|商场|中心|校区|街|路)[^)]*\)"
    r"|（[^）]*(?:店|门店|分店|总店|旗舰店|广场|商场|中心|校区|街|路)[^）]*）)$"
)
_TRAILING_BRANCH_RE = re.compile(r"(?:总店|分店|旗舰店|门店|直营店|加盟店)$")


@dataclass(frozen=True)
class MealQueryPlan:
    keyword: str
    provider_type_policy: str
    provider_types: str | None
    theme_id: str
    theme_terms: tuple[str, ...]
    query_source: str
    fallback_allowed: bool

    def to_camel_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "providerTypePolicy": self.provider_type_policy,
            "providerTypes": self.provider_types,
            "themeId": self.theme_id,
            "themeTerms": list(self.theme_terms),
            "querySource": self.query_source,
            "fallbackAllowed": self.fallback_allowed,
        }


class MealExperiencePortfolioPolicy:
    """Compile untrusted meal hypotheses and bind them to provider evidence."""

    @classmethod
    def brief_for_slot(
        cls,
        pool: Any,
        *,
        slot_id: str,
        day_number: int,
        raw_need: str,
        city: str,
        source_fingerprint: str,
        occurrence_id: str = "",
    ) -> dict[str, Any]:
        raw_briefs = getattr(pool, "meal_experience_briefs", None) or []
        matched = next(
            (
                item
                for item in raw_briefs
                if isinstance(item, Mapping)
                and str(item.get("planningSlotId") or item.get("slotId") or "") == slot_id
            ),
            None,
        )
        if matched is not None:
            search_terms = cls._bounded_terms(matched.get("searchTerms") or [])
            mode = str(matched.get("experienceMode") or "signature_dish").strip()
            return {
                "schemaVersion": "meal-experience-brief-v1",
                "briefId": str(matched.get("briefId") or f"meal:{slot_id}"),
                "proposalBriefId": str(
                    matched.get("proposalBriefId") or getattr(pool, "brief_id", "") or ""
                ),
                "occurrenceId": str(occurrence_id or matched.get("occurrenceId") or ""),
                "planningSlotId": slot_id,
                "dayNumber": int(day_number),
                "mealLabel": str(matched.get("mealLabel") or cls._meal_label(raw_need)),
                "themeId": cls._normalized_key(matched.get("themeId") or (search_terms[0] if search_terms else "")),
                "themeLabel": str(matched.get("themeLabel") or (search_terms[0] if search_terms else ""))[:40],
                "experienceMode": mode if mode in MEAL_EXPERIENCE_MODES else "signature_dish",
                "searchTerms": search_terms,
                "avoidThemeIds": [
                    cls._normalized_key(item)
                    for item in matched.get("avoidThemeIds") or []
                    if cls._normalized_key(item)
                ][:8],
                "selectionIntent": str(matched.get("selectionIntent") or "")[:160],
                "generationSource": "llm_search_hypothesis",
                "sourceFingerprint": cls._fingerprint_or_derived(
                    source_fingerprint,
                    {"slotId": slot_id, "dayNumber": day_number, "city": city},
                ),
            }

        pool_hints = cls._bounded_terms(getattr(pool, "candidate_hints", None) or [])
        assigned_slots = list(getattr(pool, "assign_to_slots", None) or [])
        slot_hint_index = assigned_slots.index(slot_id) if slot_id in assigned_slots else 0
        slot_hint = pool_hints[slot_hint_index % len(pool_hints)] if pool_hints else ""
        concrete_hints = [slot_hint] if slot_hint and not cls.is_generic_meal_query(slot_hint, city=city) else []
        return {
            "schemaVersion": "meal-experience-brief-v1",
            "briefId": f"meal:{slot_id}",
            "proposalBriefId": str(getattr(pool, "brief_id", "") or ""),
            "occurrenceId": str(occurrence_id or ""),
            "planningSlotId": slot_id,
            "dayNumber": int(day_number),
            "mealLabel": cls._meal_label(raw_need),
            "themeId": cls._normalized_key(concrete_hints[0]) if concrete_hints else "",
            "themeLabel": concrete_hints[0] if concrete_hints else "",
            "experienceMode": "signature_dish",
            "searchTerms": concrete_hints[:3],
            "avoidThemeIds": [],
            "selectionIntent": "",
            "generationSource": "provider_candidate_fallback" if not concrete_hints else "controller_hint",
            "sourceFingerprint": cls._fingerprint_or_derived(
                source_fingerprint,
                {"slotId": slot_id, "dayNumber": day_number, "city": city},
            ),
        }

    @classmethod
    def query_plan(
        cls,
        brief: Mapping[str, Any],
        *,
        raw_query: str,
        city: str,
        provider_types: str | None,
        local_food_required: bool,
        general_food_fallback: bool = False,
    ) -> MealQueryPlan:
        terms = tuple(cls._bounded_terms(brief.get("searchTerms") or []))
        keyword = terms[0] if terms else cls._generic_keyword(raw_query, city=city)
        return MealQueryPlan(
            keyword=keyword,
            provider_type_policy=(
                "general_food_strong_locality_evidence"
                if general_food_fallback
                else "destination_cuisine_subtype"
                if provider_types and local_food_required
                else "food_service"
            ),
            provider_types=None if general_food_fallback else provider_types if local_food_required else None,
            theme_id=str(brief.get("themeId") or ""),
            theme_terms=terms,
            query_source=str(brief.get("generationSource") or "provider_candidate_fallback"),
            fallback_allowed=bool(local_food_required and not general_food_fallback),
        )

    @classmethod
    def semantic_evidence(
        cls,
        candidate: Any,
        *,
        brief: Mapping[str, Any] | None,
        city: str,
        provider_types: str | None,
        local_food_required: bool,
    ) -> dict[str, Any]:
        effective_brief = brief if isinstance(brief, Mapping) else {}
        fields = cls._candidate_fields(candidate)
        terms = cls._bounded_terms(effective_brief.get("searchTerms") or [])
        matches: list[dict[str, str]] = []
        for term in terms:
            normalized_term = cls._normalized_text(term)
            if not normalized_term:
                continue
            for field_name, values in fields.items():
                if any(normalized_term in cls._normalized_text(value) for value in values):
                    matches.append({"term": term, "field": field_name})
                    break

        fallback_term = ""
        if not terms:
            fallback_term = cls._provider_theme_term(
                fields.get("tags") or [],
                city=city,
                provider_types=provider_types,
            )
            if fallback_term:
                matches.append({"term": fallback_term, "field": "tags"})

        grounded_term = str(matches[0]["term"] if matches else "")
        theme_grounded = bool(grounded_term)
        local_evidence_kind = cls._local_food_evidence_kind(
            candidate,
            city=city,
            grounded_term=grounded_term,
            provider_types=provider_types,
        )
        local_food_passed = bool(local_evidence_kind) if local_food_required else True
        family_key = cls._normalized_key(grounded_term) if theme_grounded else ""
        source_fingerprint = str(effective_brief.get("sourceFingerprint") or "")
        material = {
            "schemaVersion": "meal-semantic-evidence-v1",
            "amapPoiId": cls._value(candidate, "amap_id", "amapId", "id").upper(),
            "canonicalBrand": cls.canonical_brand(cls._value(candidate, "name")),
            "themeId": str(effective_brief.get("themeId") or family_key),
            "themeLabel": str(effective_brief.get("themeLabel") or grounded_term),
            "groundedFamilyKey": family_key,
            "matchedTerms": list(dict.fromkeys(item["term"] for item in matches)),
            "matchedFields": list(dict.fromkeys(item["field"] for item in matches)),
            "providerCuisineType": str(provider_types or ""),
            "localFoodEvidenceKind": local_evidence_kind,
            "sourceClaimIds": cls._supporting_claim_ids(candidate),
            "themeGrounded": theme_grounded,
            "localFoodPassed": local_food_passed,
            "generationSource": str(effective_brief.get("generationSource") or "provider_candidate_fallback"),
            "sourceFingerprint": source_fingerprint,
        }
        material["evidenceFingerprint"] = cls._fingerprint(material)
        return material

    @classmethod
    def snapshot_quality(cls, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        meal_records: list[dict[str, Any]] = []
        failures: list[str] = []
        brands: set[str] = set()
        families: set[str] = set()
        for day in snapshot.get("days") or []:
            if not isinstance(day, Mapping):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, Mapping):
                    continue
                metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), Mapping) else {}
                if str(metadata.get("intentType") or segment.get("kind") or "") != "meal":
                    continue
                constraints = metadata.get("scheduleConstraints") if isinstance(metadata.get("scheduleConstraints"), Mapping) else {}
                evidence = constraints.get("mealSemanticEvidence") if isinstance(constraints.get("mealSemanticEvidence"), Mapping) else {}
                brief = constraints.get("mealExperienceBrief") if isinstance(constraints.get("mealExperienceBrief"), Mapping) else {}
                # Every Simple Open meal must explain what experience it adds.
                # Old snapshots without this evidence remain readable, but are
                # intentionally not confirmable.
                theme_required = True
                planning_slot_id = str(metadata.get("planningSlotId") or "")
                day_number = int(day.get("dayNumber") or 0)
                brand = str(evidence.get("canonicalBrand") or "")
                family = str(evidence.get("groundedFamilyKey") or "")
                if not brief:
                    failures.append("simple_direction_meal_brief_missing")
                elif (
                    str(brief.get("planningSlotId") or "") != planning_slot_id
                    or int(brief.get("dayNumber") or 0) != day_number
                ):
                    failures.append("simple_direction_meal_theme_ungrounded")
                if theme_required and (not evidence or evidence.get("themeGrounded") is not True):
                    failures.append("simple_direction_meal_theme_ungrounded")
                if not str(evidence.get("amapPoiId") or "").strip():
                    failures.append("simple_direction_meal_theme_ungrounded")
                if constraints.get("localFoodRequired") is True and evidence.get("localFoodPassed") is not True:
                    failures.append("simple_direction_meal_theme_ungrounded")
                if brief and str(brief.get("sourceFingerprint") or "") != str(evidence.get("sourceFingerprint") or ""):
                    failures.append("simple_direction_meal_theme_ungrounded")
                if brand and brand in brands:
                    failures.append("simple_direction_meal_brand_repeated")
                if family and family in families:
                    failures.append("simple_direction_meal_family_repeated")
                if brand:
                    brands.add(brand)
                if family:
                    families.add(family)
                meal_records.append(
                    {
                        "dayNumber": day_number,
                        "planningSlotId": planning_slot_id,
                        "amapPoiId": str(evidence.get("amapPoiId") or ""),
                        "canonicalBrand": brand,
                        "themeId": str(evidence.get("themeId") or ""),
                        "groundedFamilyKey": family,
                        "themeLabel": str(evidence.get("themeLabel") or ""),
                        "matchedTerms": copy.deepcopy(evidence.get("matchedTerms") or []),
                        "matchedFields": copy.deepcopy(evidence.get("matchedFields") or []),
                        "localFoodEvidenceKind": str(evidence.get("localFoodEvidenceKind") or ""),
                        "themeRequired": theme_required,
                        "mealExperienceBrief": copy.deepcopy(brief),
                    }
                )
        signature = [record["groundedFamilyKey"] for record in meal_records if record["groundedFamilyKey"]]
        passed = not failures and all(
            not record["themeRequired"] or bool(record["groundedFamilyKey"])
            for record in meal_records
        )
        return {
            "schemaVersion": "meal-quality-evidence-v1",
            "mealCount": len(meal_records),
            "mealSemanticEvidence": meal_records,
            "mealThemeSignature": signature,
            "mealQualityPassed": passed,
            "mealDiversityPassed": passed,
            "mealUnresolvedReasons": list(dict.fromkeys(failures)),
        }

    @classmethod
    def route_comfort_evidence(cls, legs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        durations = [cls._positive_number(item.get("durationSeconds")) for item in legs]
        distances = [cls._positive_number(item.get("distanceMeters")) for item in legs]
        walking = [cls._nonnegative_number(item.get("walkingDistanceMeters")) for item in legs]
        transfers = [cls._nonnegative_number(item.get("transferCount")) for item in legs]
        unknown_fields: list[str] = []
        if legs and any(value is None for value in walking):
            unknown_fields.append("walkingDistanceMeters")
        if legs and any(value is None for value in transfers):
            unknown_fields.append("transferCount")
        return {
            "schemaVersion": "route-comfort-evidence-v1",
            "source": "verified_provider_route_pairs",
            "routePairCount": len(legs),
            "totalTravelSeconds": int(sum(value or 0 for value in durations)),
            "totalDistanceMeters": int(sum(value or 0 for value in distances)),
            "maxAdjacentTravelSeconds": int(max((value or 0 for value in durations), default=0)),
            "walkingDistanceMeters": (
                int(sum(value or 0 for value in walking)) if legs and not unknown_fields.count("walkingDistanceMeters") else None
            ),
            "transferCount": (
                int(sum(value or 0 for value in transfers)) if legs and not unknown_fields.count("transferCount") else None
            ),
            "unknownFields": unknown_fields,
        }

    @classmethod
    def canonical_brand(cls, value: Any) -> str:
        original = str(value or "").strip().casefold()
        if not original:
            return ""
        normalized = _BRANCH_RE.sub("", original)
        normalized = re.sub(r"[\s\-_,.·・，。]+", "", normalized)
        previous = None
        while normalized and previous != normalized:
            previous = normalized
            normalized = _TRAILING_BRANCH_RE.sub("", normalized)
        return (normalized or cls._normalized_text(original))[:64]

    @classmethod
    def is_generic_meal_query(cls, value: Any, *, city: str) -> bool:
        compact = cls._normalized_text(value)
        city_key = cls._normalized_text(city).removesuffix("市")
        if city_key and compact.startswith(city_key):
            compact = compact[len(city_key) :]
        compact = re.sub(r"(?:早餐|午餐|晚餐|早饭|午饭|晚饭|中午|晚上)", "", compact)
        if not compact:
            return True
        if re.fullmatch(r"(?:当地|本地|地方)?(?:特色|传统|风味)?", compact):
            return True
        return compact in {cls._normalized_text(item) for item in _GENERIC_MEAL_TERMS} or bool(
            re.fullmatch(r"(?:当地|本地|地方)?(?:特色|传统|风味)?(?:美食|餐饮|餐厅)", compact)
        )

    @classmethod
    def _candidate_fields(cls, candidate: Any) -> dict[str, list[str]]:
        claims = cls._list_value(candidate, "source_claims", "sourceClaims")
        claim_values: list[str] = []
        for claim in claims:
            if not isinstance(claim, Mapping) or str(claim.get("stance") or "support") != "support":
                continue
            claim_values.extend(
                str(claim.get(key) or "")
                for key in ("value", "claimValue", "claim_value", "family", "text", "snippet")
                if str(claim.get(key) or "").strip()
            )
            claim_key = str(claim.get("claimKey") or claim.get("claim_key") or "")
            if ":" in claim_key:
                claim_values.append(claim_key.partition(":")[2])
        return {
            "name": [cls._value(candidate, "name")],
            "type": [cls._value(candidate, "type", "category")],
            "tags": [str(item) for item in cls._list_value(candidate, "tags") if str(item).strip()],
            "sourceClaims": claim_values,
        }

    @classmethod
    def _provider_theme_term(cls, tags: Sequence[str], *, city: str, provider_types: str | None) -> str:
        excluded = {cls._normalized_text(item) for item in _GENERIC_MEAL_TERMS}
        excluded.add(cls._normalized_text(provider_types))
        city_key = cls._normalized_text(city).removesuffix("市")
        if city_key:
            excluded.add(f"{city_key}菜")
        for raw_tag in tags:
            for part in re.split(r"[;；/／|、,，]", str(raw_tag or "")):
                tag = part.strip()
                normalized = cls._normalized_text(tag)
                if 2 <= len(normalized) <= 20 and normalized not in excluded:
                    return tag[:32]
        return ""

    @classmethod
    def _local_food_evidence_kind(
        cls,
        candidate: Any,
        *,
        city: str,
        grounded_term: str,
        provider_types: str | None,
    ) -> str:
        candidate_type = cls._value(candidate, "type")
        tags = [str(item) for item in cls._list_value(candidate, "tags")]
        provider_token = cls._normalized_text(provider_types)
        type_tokens = {
            cls._normalized_text(token)
            for value in [candidate_type, *tags]
            for token in re.split(r"[;；/／|]", str(value or ""))
            if cls._normalized_text(token)
        }
        if provider_token and provider_token in type_tokens:
            return "amap_destination_cuisine_subtype"
        claims = cls._list_value(candidate, "source_claims", "sourceClaims")
        for claim in claims:
            if not isinstance(claim, Mapping) or str(claim.get("stance") or "support") != "support":
                continue
            key = str(claim.get("claimKey") or claim.get("claim_key") or "").casefold()
            locality = cls._normalized_text(
                claim.get("locality") or claim.get("city") or claim.get("location") or ""
            )
            if key in {"local_food", "local_food_context"} and cls._normalized_text(city).removesuffix("市") in locality:
                return "supporting_local_food_claim"
        city_key = cls._normalized_text(city).removesuffix("市")
        candidate_text = cls._normalized_text(" ".join([cls._value(candidate, "name"), *tags]))
        if city_key and grounded_term and city_key in candidate_text and cls._normalized_text(grounded_term) in candidate_text:
            return "amap_city_marker_and_theme"
        return ""

    @classmethod
    def _supporting_claim_ids(cls, candidate: Any) -> list[str]:
        result: list[str] = []
        for claim in cls._list_value(candidate, "source_claims", "sourceClaims"):
            if not isinstance(claim, Mapping) or str(claim.get("stance") or "support") != "support":
                continue
            identity = str(claim.get("id") or claim.get("claimId") or claim.get("claimKey") or "").strip()
            if identity:
                result.append(identity[:96])
        return list(dict.fromkeys(result))[:8]

    @classmethod
    def _generic_keyword(cls, raw_query: str, *, city: str) -> str:
        return "餐厅" if cls.is_generic_meal_query(raw_query, city=city) else str(raw_query or "").strip()

    @staticmethod
    def _meal_label(raw_need: str) -> str:
        return "dinner" if re.search(r"(?:晚餐|晚饭|dinner)", str(raw_need or ""), re.IGNORECASE) else "lunch"

    @classmethod
    def _bounded_terms(cls, values: Any) -> list[str]:
        if not isinstance(values, (list, tuple)):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            term = re.sub(r"\s+", " ", str(value or "")).strip()[:32]
            normalized = cls._normalized_text(term)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(term)
        return result[:3]

    @staticmethod
    def _normalized_text(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)

    @classmethod
    def _normalized_key(cls, value: Any) -> str:
        return cls._normalized_text(value)[:64]

    @staticmethod
    def _value(candidate: Any, *names: str) -> str:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, Mapping) else getattr(candidate, name, None)
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _list_value(candidate: Any, *names: str) -> list[Any]:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, Mapping) else getattr(candidate, name, None)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _nonnegative_number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @classmethod
    def _fingerprint_or_derived(cls, value: str, fallback: Mapping[str, Any]) -> str:
        normalized = str(value or "").strip()
        return normalized if re.fullmatch(r"[0-9a-fA-F]{64}", normalized) else cls._fingerprint(fallback)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

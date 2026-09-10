import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class DiversityHintResult:
    hints: list[str]
    variant_id: str
    variant_seed: int
    candidate_hint_policy: str = "diversified_static_hint"


@dataclass(frozen=True)
class CreativeVariant:
    variant_id: str
    label: str
    description: str
    hint_terms: tuple[str, ...]


@dataclass(frozen=True)
class ItineraryNoveltyContext:
    previous_poi_keys: frozenset[str] = field(default_factory=frozenset)
    previous_canonical_entities: frozenset[str] = field(default_factory=frozenset)
    previous_night_view_families: frozenset[str] = field(default_factory=frozenset)
    previous_meal_brands: frozenset[str] = field(default_factory=frozenset)
    previous_experience_types: frozenset[str] = field(default_factory=frozenset)
    primary_variant_id: str = "classic"
    primary_variant_label: str = "经典必游"
    secondary_variant_id: str = "local_immersion"
    creativity_level: int = 2
    variant_seed: int = 0
    user_requested_creativity: bool = False

    def to_metadata(self) -> dict[str, Any]:
        return {
            "creativeVariantId": self.primary_variant_id,
            "creativeVariantLabel": self.primary_variant_label,
            "secondaryCreativeVariantId": self.secondary_variant_id,
            "creativityLevel": self.creativity_level,
            "creativeVariantSeed": self.variant_seed,
            "userRequestedCreativity": self.user_requested_creativity,
            "noveltyReferencePoiCount": len(self.previous_poi_keys),
            "noveltyReferenceEntityCount": len(self.previous_canonical_entities),
            "noveltyReferenceExperienceTypes": sorted(self.previous_experience_types),
        }


CREATIVE_VARIANTS = [
    CreativeVariant(
        "classic",
        "经典必游",
        "保留代表性地标和稳定路线，适合第一次到访。",
        ("地标", "经典景点", "核心景区", "代表性建筑"),
    ),
    CreativeVariant(
        "local_immersion",
        "本地沉浸",
        "降低打卡密度，增加本地生活街区、社区小店和慢逛时间。",
        ("本地生活", "社区街区", "老街", "胡同", "市集", "本地小馆"),
    ),
    CreativeVariant(
        "food_led",
        "美食主线",
        "围绕地方风味、夜市、小吃街和顺路餐饮组织路线。",
        ("当地特色美食", "老字号", "夜市", "小吃街", "本地菜", "风味餐厅"),
    ),
    CreativeVariant(
        "culture_deep_dive",
        "文化深游",
        "偏向博物馆、展览、历史街区和演出文化空间。",
        ("博物馆", "美术馆", "展览", "历史街区", "剧院", "文化空间"),
    ),
    CreativeVariant(
        "nature_relaxed",
        "自然松弛",
        "优先公园、湖边、湿地和低强度户外停留。",
        ("公园", "湖边", "湿地", "森林", "滨水步道", "自然风景"),
    ),
    CreativeVariant(
        "photo_night",
        "摄影夜游",
        "偏向观景点、天际线、滨水夜游和日落夜景节奏。",
        ("摄影", "日落", "夜景", "观景台", "天际线", "滨水夜游"),
    ),
    CreativeVariant(
        "family_light",
        "亲子轻松",
        "控制步行和换乘，加入亲子友好场馆与休息空间。",
        ("亲子", "科技馆", "儿童友好", "动物园", "游乐园", "轻松公园"),
    ),
    CreativeVariant(
        "citywalk_hidden_gems",
        "隐藏 CityWalk",
        "避开过度大众化线路，增加胡同、街巷、独立店和小众文化点。",
        ("CityWalk", "小众", "隐藏路线", "胡同", "街巷", "独立书店", "创意园区"),
    ),
]

CREATIVE_VARIANT_BY_ID = {variant.variant_id: variant for variant in CREATIVE_VARIANTS}


class ItineraryDiversityPolicy:
    def variant_seed(self, *parts: Any) -> int:
        text = "|".join(str(part or "") for part in parts)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return int(digest[:12], 16)

    def creative_profile(self, city: str, pipeline_context: dict[str, Any]) -> ItineraryNoveltyContext:
        message = " ".join(
            [
                str(pipeline_context.get("latestUserMessage") or ""),
                str(pipeline_context.get("effectiveUserMessage") or ""),
                str(pipeline_context.get("memoryText") or ""),
                str(pipeline_context.get("currentPreferenceSummary") or ""),
            ]
        )
        seed = self.variant_seed(
            pipeline_context.get("sessionId"),
            pipeline_context.get("userTurnId"),
            pipeline_context.get("latestUserMessage"),
            pipeline_context.get("effectiveUserMessage"),
            city,
        )
        explicit_classic = bool(re.search(r"(经典|第一次|第一次去|必去|大众路线|保守|稳妥)", message))
        explicit_creative = bool(re.search(r"(换一版|再来一版|重新规划|更有创意|有创意|小众|不要大众|不走大众|多变|隐藏|CityWalk|citywalk|本地|深度|沉浸)", message))
        creativity_level = 1 if explicit_classic and not explicit_creative else 3 if explicit_creative else 2
        variant_id = self._preferred_variant_id(message)
        if not variant_id:
            options = [variant.variant_id for variant in CREATIVE_VARIANTS if variant.variant_id != "classic"]
            variant_id = options[seed % len(options)] if creativity_level >= 2 else "classic"
        if explicit_classic and not explicit_creative:
            variant_id = "classic"
        secondary_options = [variant.variant_id for variant in CREATIVE_VARIANTS if variant.variant_id != variant_id]
        secondary_variant_id = secondary_options[(seed // 13) % len(secondary_options)] if secondary_options else "classic"
        variant = CREATIVE_VARIANT_BY_ID.get(variant_id, CREATIVE_VARIANT_BY_ID["classic"])
        return ItineraryNoveltyContext(
            primary_variant_id=variant.variant_id,
            primary_variant_label=variant.label,
            secondary_variant_id=secondary_variant_id,
            creativity_level=creativity_level,
            variant_seed=seed,
            user_requested_creativity=explicit_creative,
        )

    def _preferred_variant_id(self, message: str) -> str:
        text = str(message or "")
        if re.search(r"(985|211|高校|大学|校园)", text):
            return "photo_night" if re.search(r"(夜景|夜游|观景)", text) else "culture_deep_dive"
        if re.search(r"(隐藏|小众|不要大众|不走大众|CityWalk|citywalk|街巷|胡同|独立店)", text):
            return "citywalk_hidden_gems"
        if re.search(r"(本地|沉浸|生活|社区|市集)", text):
            return "local_immersion"
        if re.search(r"(美食|小吃|老字号|夜市|本地菜|风味)", text):
            return "food_led"
        if re.search(r"(博物馆|展览|文化|历史|美术馆|演出|剧院)", text):
            return "culture_deep_dive"
        if re.search(r"(自然|公园|湿地|森林|湖|松弛|轻松)", text):
            return "nature_relaxed"
        if re.search(r"(摄影|拍照|日落|夜景|夜游|观景)", text):
            return "photo_night"
        if re.search(r"(亲子|孩子|儿童|家庭)", text):
            return "family_light"
        return ""

    def fallback_intent_for_variant(self, city: str, profile: ItineraryNoveltyContext) -> Optional[dict[str, Any]]:
        if profile.creativity_level <= 1 or profile.primary_variant_id == "classic":
            return None
        by_variant = {
            "local_immersion": ("area_walk", "本地生活街区 CityWalk", ["街区", "步行街", "商圈", "公园", "广场"]),
            "food_led": ("area_walk", "美食街区与本地生活漫步", ["街区", "步行街", "商圈", "餐饮服务", "夜市"]),
            "culture_deep_dive": ("museum", "博物馆展览与历史街区", ["博物馆", "美术馆", "展览馆", "历史街区", "剧院"]),
            "nature_relaxed": ("park", "自然公园与滨水慢行", ["公园", "湿地", "湖泊", "森林", "风景名胜"]),
            "photo_night": ("landmark", "城市摄影地标与观景路线", ["地标", "观景点", "塔", "桥", "广场"]),
            "family_light": ("park", "亲子轻松场馆与公园", ["公园", "科技馆", "儿童乐园", "动物园", "游乐园"]),
            "citywalk_hidden_gems": ("area_walk", "小众街巷 CityWalk", ["街区", "胡同", "步行街", "创意园区", "独立书店"]),
        }
        intent_type, raw_need, preferred_types = by_variant.get(profile.primary_variant_id, by_variant["local_immersion"])
        kind = "area_walk" if intent_type == "area_walk" else "visit"
        if intent_type == "park":
            kind = "park"
        return {
            "kind": kind,
            "poolId": f"{intent_type}_pool",
            "rawNeed": f"{city} {raw_need}".strip(),
            "intentType": intent_type,
            "preferredTypes": preferred_types,
        }

    def creative_candidate_hints(self, city: str, intent_type: str, pipeline_context: dict[str, Any], limit: int = 6) -> list[str]:
        profile = self.creative_profile(city, pipeline_context)
        variant = CREATIVE_VARIANT_BY_ID.get(profile.primary_variant_id, CREATIVE_VARIANT_BY_ID["classic"])
        if profile.creativity_level <= 1 or profile.primary_variant_id == "classic":
            return []
        compatible = {
            "local_immersion": {"area_walk", "landmark", "meal", "shopping"},
            "food_led": {"area_walk", "meal", "shopping", "night_view"},
            "culture_deep_dive": {"museum", "area_walk", "landmark", "night_view"},
            "nature_relaxed": {"park", "area_walk", "landmark"},
            "photo_night": {"night_view", "landmark", "area_walk"},
            "family_light": {"park", "museum", "area_walk", "meal"},
            "citywalk_hidden_gems": {"area_walk", "landmark", "shopping", "meal"},
        }
        if intent_type not in compatible.get(profile.primary_variant_id, set()):
            return []
        hints = [f"{city} {term}" for term in variant.hint_terms]
        return _dedupe(hints)[:limit]

    def creative_theme_affinity(self, profile: ItineraryNoveltyContext, intent_type: str, candidate: Any) -> float:
        if profile.creativity_level <= 1:
            return 0.0
        variant = CREATIVE_VARIANT_BY_ID.get(profile.primary_variant_id)
        if variant is None:
            return 0.0
        text = _candidate_text(candidate)
        matched = sum(1 for term in variant.hint_terms if re.search(re.escape(term), text, re.IGNORECASE))
        if not matched:
            return 0.0
        weight = 0.018 if intent_type == "meal" else 0.028
        return min(0.06, matched * weight)

    def novelty_penalty(
        self,
        profile: ItineraryNoveltyContext,
        *,
        intent_type: str,
        candidate: Any,
        poi_key: str = "",
        canonical_key: str = "",
        family_key: str = "",
        meal_brand: str = "",
        exact_user_request: bool = False,
    ) -> float:
        if exact_user_request or profile.creativity_level <= 1:
            return 0.0
        penalty = 0.0
        if poi_key and poi_key in profile.previous_poi_keys:
            penalty = max(penalty, 0.16)
        if canonical_key and canonical_key in profile.previous_canonical_entities:
            penalty = max(penalty, 0.14)
        if intent_type == "night_view" and family_key and family_key in profile.previous_night_view_families:
            penalty = max(penalty, 0.12)
        if intent_type == "meal" and meal_brand and meal_brand in profile.previous_meal_brands:
            penalty = max(penalty, 0.08)
        experience = self.experience_type(intent_type, candidate)
        if experience and experience in profile.previous_experience_types and profile.user_requested_creativity:
            penalty = max(penalty, 0.04)
        return min(0.18, penalty)

    def experience_type(self, intent_type: str, candidate: Any) -> str:
        text = _candidate_text(candidate)
        if intent_type == "meal" or re.search(r"(餐饮|小吃|饭店|餐厅|咖啡|酒吧|夜市)", text):
            return "food"
        if intent_type == "night_view" or re.search(r"(夜景|观景|塔|天际线|滨水夜游)", text):
            return "night_view"
        if intent_type == "campus_visit" or re.search(r"(大学|学院|高校|校区)", text):
            return "campus"
        if intent_type == "museum" or re.search(r"(博物馆|美术馆|展览馆|纪念馆|科技馆)", text):
            return "culture"
        if intent_type == "park" or re.search(r"(公园|湿地|森林|湖|山|自然)", text):
            return "nature"
        if intent_type == "area_walk" or re.search(r"(胡同|街区|步行街|老街|CityWalk|citywalk|创意园)", text):
            return "citywalk"
        if re.search(r"(商圈|购物中心|商业)", text):
            return "shopping"
        return intent_type or "visit"

    def campus_hints(self, city: str, pool: Any, pipeline_context: dict[str, Any]) -> DiversityHintResult:
        seed = self.variant_seed(
            pipeline_context.get("sessionId"),
            pipeline_context.get("userTurnId"),
            pipeline_context.get("latestUserMessage"),
            pipeline_context.get("effectiveUserMessage"),
            city,
            getattr(pool, "pool_id", ""),
            getattr(pool, "raw_need", ""),
        )
        return DiversityHintResult(
            hints=[f"{city} 高校", f"{city} 大学", f"{city} 高等院校", f"{city} 校园参观"],
            variant_id=f"{_safe_variant_city(city)}_generic_{seed % 7}",
            variant_seed=seed,
        )

    def metadata_for_pool(self, city: str, pool: Any, pipeline_context: dict[str, Any]) -> dict[str, Any]:
        profile = self.creative_profile(city, pipeline_context)
        metadata: dict[str, Any] = {
            "creativeVariantId": profile.primary_variant_id,
            "creativeVariantLabel": profile.primary_variant_label,
            "secondaryCreativeVariantId": profile.secondary_variant_id,
            "creativityLevel": profile.creativity_level,
            "creativeVariantSeed": profile.variant_seed,
            "userRequestedCreativity": profile.user_requested_creativity,
        }
        if str(getattr(pool, "intent_type", "") or "") != "campus_visit":
            return metadata
        result = self.campus_hints(city, pool, pipeline_context)
        return {
            **metadata,
            "diversityVariantId": result.variant_id,
            "variantSeed": result.variant_seed,
            "candidateHintPolicy": result.candidate_hint_policy,
        }

    def _rotate(self, values: list[str], seed: int) -> list[str]:
        if not values:
            return []
        offset = seed % len(values)
        return [*values[offset:], *values[:offset]]


def _safe_variant_city(city: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "", str(city or "city"))[:12] or "city"


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
        key = re.sub(r"\s+", "", cleaned.casefold())
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


def _candidate_text(candidate: Any) -> str:
    return " ".join(
        str(getattr(candidate, key, "") or "")
        for key in ("name", "type", "category", "address", "district", "source_note")
    )

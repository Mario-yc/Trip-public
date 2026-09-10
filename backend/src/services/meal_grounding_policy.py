import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.services.city_food_terms import city_food_search_hints


MEAL_LABELS = {
    "breakfast": ("早餐", "08:00", 45),
    "lunch": ("午餐", "12:00", 75),
    "snack": ("小吃/咖啡", "15:30", 45),
    "dinner": ("晚餐", "17:30", 75),
}

# Server-owned semantics for an explicit meal ExperienceSpec.  These values
# describe what evidence the consumer may use; they do not select a restaurant
# or assert that a venue is currently open.
MEAL_EXPERIENCE_ACCESS_POLICY = "verified_amap_food_service"
MEAL_EXPERIENCE_DISTINCTNESS_POLICY = "distinct_physical_identity_per_occurrence"
MEAL_EXPERIENCE_EVIDENCE_FRESHNESS = {
    "maxAgeHours": 24,
    "requiredForControlledAccess": False,
    "requiredForPublicOutdoor": False,
    "allowExplicitNoClosure": False,
}


@dataclass(frozen=True)
class MealSlotIntent:
    slot_kind: str
    meal_label: str
    raw_need: str
    route_anchor: bool
    candidate_hints: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0
    notes: str = ""
    confidence: float = 0.0

    def to_camel_dict(self) -> dict[str, Any]:
        return {
            "slotKind": self.slot_kind,
            "mealLabel": self.meal_label,
            "rawNeed": self.raw_need,
            "routeAnchor": self.route_anchor,
            "candidateHints": list(self.candidate_hints),
            "estimatedCost": self.estimated_cost,
            "notes": self.notes,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class MealGroundingPolicyResult:
    explicit_food_experience: bool
    meal_slots: list[MealSlotIntent] = field(default_factory=list)
    preserved_user_intents: list[str] = field(default_factory=list)
    reason: str = ""

    def to_camel_dict(self) -> dict[str, Any]:
        return {
            "explicitFoodExperience": self.explicit_food_experience,
            "mealSlots": [slot.to_camel_dict() for slot in self.meal_slots],
            "preservedUserIntents": list(self.preserved_user_intents),
            "reason": self.reason,
        }


class MealGroundingPolicy:
    FOOD_EXPERIENCE_RE = re.compile(
        r"(当地|本地|地方|区域|特色|地道|风味|菜系|美食|餐饮体验|餐厅|饭店|小吃|夜市|美食街|菜市场|咖啡|下午茶|"
        r"想吃|想尝|品尝|尝试|regional|local food|restaurant|dining|cuisine|snack|street food|market|cafe)",
        re.IGNORECASE,
    )
    PASSIVE_MEAL_RE = re.compile(r"(留.*(午餐|晚餐|早餐|吃饭)|预留.*(午餐|晚餐|早餐|吃饭)|吃个饭|用餐时间)")
    MEAL_PATTERNS = {
        "breakfast": re.compile(r"(早餐|早饭|breakfast)", re.IGNORECASE),
        "lunch": re.compile(r"(午餐|午饭|中饭|lunch)", re.IGNORECASE),
        "dinner": re.compile(r"(晚餐|晚饭|dinner)", re.IGNORECASE),
        "snack": re.compile(r"(小吃|咖啡|下午茶|夜市|snack|cafe|coffee|street food)", re.IGNORECASE),
    }
    COST_BY_BUDGET = {
        "low": {"breakfast": 25.0, "lunch": 40.0, "dinner": 60.0, "snack": 35.0},
        "medium": {"breakfast": 40.0, "lunch": 80.0, "dinner": 120.0, "snack": 50.0},
        "high": {"breakfast": 80.0, "lunch": 150.0, "dinner": 250.0, "snack": 90.0},
        "unknown": {"breakfast": 40.0, "lunch": 80.0, "dinner": 120.0, "snack": 50.0},
    }

    def analyze(
        self,
        text: str,
        *,
        city: str = "",
        budget: object = None,
        day_count: int = 1,
    ) -> MealGroundingPolicyResult:
        message = str(text or "")
        city = str(city or "").strip() or "目的地"
        explicit = bool(self.FOOD_EXPERIENCE_RE.search(message)) and not self._only_passive_meal(message)
        labels = self._requested_labels(message)
        if explicit and not labels:
            # A broad "try local food along the way" request is one
            # experience goal, not a requirement that every lunch and dinner
            # become a grounded restaurant.
            labels = ["lunch"]
        elif not explicit and not labels:
            return MealGroundingPolicyResult(False, [], [], "no_explicit_meal_intent")

        cuisine_need = self._cuisine_need(message)
        budget_tier = self._budget_tier(budget, message)
        slots: list[MealSlotIntent] = []
        for label in labels:
            label_zh, _start, _duration = MEAL_LABELS[label]
            raw_need = f"{label_zh} {cuisine_need}" if cuisine_need else label_zh
            route_anchor = explicit
            hints = self._candidate_hints(city, raw_need, cuisine_need) if route_anchor else []
            notes = (
                "用户明确要求餐饮体验；候选仅作为结构化地图检索 seed，不代表最终餐厅。"
                if route_anchor
                else "普通用餐时间；可选择顺路餐厅，不强制餐厅 grounding。"
            )
            slots.append(
                MealSlotIntent(
                    slot_kind="meal",
                    meal_label=label,
                    raw_need=raw_need,
                    route_anchor=route_anchor,
                    candidate_hints=hints,
                    estimated_cost=self.estimate_cost(label, budget_tier),
                    notes=notes,
                    confidence=0.84 if route_anchor else 0.55,
                )
            )
        reason = "explicit_food_experience" if explicit else "passive_meal_time"
        preserved = [slot.raw_need for slot in slots if slot.route_anchor]
        return MealGroundingPolicyResult(explicit, slots, preserved, reason)

    def estimate_cost(self, meal_label: str, budget: object = None, text: str = "") -> float:
        tier = self._budget_tier(budget, text)
        label = meal_label if meal_label in MEAL_LABELS else self._label_for_text(str(meal_label or "")) or "lunch"
        return self.COST_BY_BUDGET[tier].get(label, self.COST_BY_BUDGET[tier]["lunch"])

    def budget_tier(self, budget: object = None, text: str = "") -> str:
        return self._budget_tier(budget, text)

    def estimate_cost_range(self, meal_label: str, budget: object = None, text: str = "") -> dict[str, object]:
        tier = self._budget_tier(budget, text)
        preferred = self.estimate_cost(meal_label, tier)
        label = meal_label if meal_label in MEAL_LABELS else self._label_for_text(str(meal_label or "")) or "lunch"
        spread = 0.25 if label in {"lunch", "dinner"} else 0.2
        return {
            "min": round(preferred * (1 - spread), 2),
            "preferred": preferred,
            "max": round(preferred * (1 + spread), 2),
            "source": "budget_tier_policy",
            "confidence": 0.68,
            "provisional": True,
            "budgetTier": tier,
        }

    def label_for_text(self, value: str) -> Optional[str]:
        return self._label_for_text(value)

    def _requested_labels(self, message: str) -> list[str]:
        labels = [label for label, pattern in self.MEAL_PATTERNS.items() if pattern.search(message)]
        return [label for label in ("breakfast", "lunch", "snack", "dinner") if label in labels]

    def _label_for_text(self, value: str) -> Optional[str]:
        for label, pattern in self.MEAL_PATTERNS.items():
            if pattern.search(value):
                return label
        return None

    def _only_passive_meal(self, message: str) -> bool:
        return bool(self.PASSIVE_MEAL_RE.search(message)) and not self.FOOD_EXPERIENCE_RE.search(message)

    def _cuisine_need(self, message: str) -> str:
        provider_category = re.search(
            r"(?:高德|AMap)[^，,。；;\n]{0,8}?(?:分类|类别|类型)"
            r"(?:中的|中|里的|里|为)?\s*(?P<cuisine>[\u4e00-\u9fff]{2,6}菜)"
            r"(?=$|[，,。；;、\s])",
            message,
            re.IGNORECASE,
        )
        if provider_category:
            return str(provider_category.group("cuisine") or "").strip()
        explicit = re.search(
            r"(?:想吃|想尝|品尝|尝试|安排)\s*([^，,。；;]{1,16}?)(?:\s*(?:不要|不想|但|，|,|。|；|;)|$)",
            message,
        )
        if explicit:
            value = re.sub(r"^(早餐|午餐|晚餐)\s*", "", explicit.group(1)).strip()
            schedule_quantity = re.fullmatch(
                r"[一二两三四五六七八九十\d]+\s*个\s*(?:不同)?\s*(?:地点|位置|行程|景点|活动)",
                value,
            )
            if value and not schedule_quantity:
                return value[:16]
        if re.search(r"(当地|本地|地方|区域|特色|地道|风味|local|regional)", message, re.IGNORECASE):
            return "当地特色美食"
        if re.search(r"(美食|餐饮体验|餐厅|饭店|dining|restaurant|cuisine)", message, re.IGNORECASE):
            return "餐饮体验"
        return ""

    def _candidate_hints(self, city: str, raw_need: str, cuisine_need: str) -> list[str]:
        if cuisine_need and re.search(r"(当地|本地|地方|区域|特色|地道|风味|美食|小吃)", cuisine_need):
            return city_food_search_hints(city, raw_need, raw_need, limit=4)
        seeds = [
            f"{city} {raw_need} 餐厅",
            f"{city} {cuisine_need or raw_need}",
            f"{city} 特色餐厅",
        ]
        deduped: list[str] = []
        seen: set[str] = set()
        for seed in seeds:
            cleaned = re.sub(r"\s+", " ", seed).strip()
            key = re.sub(r"\s+", "", cleaned.lower())
            if cleaned and key not in seen:
                deduped.append(cleaned)
                seen.add(key)
        return deduped[:4]

    def _budget_tier(self, budget: object, text: str) -> str:
        structured = str(budget or "").strip().lower()
        if structured in {"low", "medium", "high"}:
            return structured
        value = f"{structured} {text or ''}".lower()
        if re.search(r"(?:高预算|高档|奢华|预算宽裕|贵一点|high\s*budget)", value):
            return "high"
        if re.search(r"(?:低预算|省钱|经济型|便宜些|控制预算|low\s*budget)", value):
            return "low"
        if re.search(r"(?:中等预算|中档|适中|普通消费|正常消费|medium\s*budget)", value):
            return "medium"
        return "unknown"

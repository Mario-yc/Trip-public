import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional


LOCAL_FOOD_REQUEST_RE = re.compile(r"(当地|本地|地方|特色|风味|老字号|小吃|菜系|美食体验|美食|夜市|传统)")
INSTITUTIONAL_MEAL_RE = re.compile(
    r"(食堂|教工餐厅|学生餐厅|员工餐厅|园区食堂|公司|单位|机关|宿舍|便利店|超市|暖心驿站|水站|茶道|花道)"
)
LODGING_MEAL_RE = re.compile(r"(住宿服务|宾馆酒店|酒店|宾馆|旅馆|民宿|公寓|饭店住宿)")
COFFEE_ONLY_RE = re.compile(r"(咖啡|coffee|cafe|café|茶饮|甜品|蛋糕|烘焙)", re.IGNORECASE)


@dataclass(frozen=True)
class MealCandidateQuality:
    acceptable: bool
    local_relevance_score: float
    hard_reject_reasons: list[str] = field(default_factory=list)
    soft_reasons: list[str] = field(default_factory=list)


class MealCandidateQualityPolicy:
    def requires_local_food(self, raw_need: str, hints: Optional[list[str]] = None) -> bool:
        return bool(LOCAL_FOOD_REQUEST_RE.search(f"{raw_need} {' '.join(hints or [])}"))

    def evaluate(
        self, raw_need: str, hints: Optional[list[str]], candidate: Any, *, city: str = ""
    ) -> MealCandidateQuality:
        if not self.requires_local_food(raw_need, hints):
            return MealCandidateQuality(acceptable=True, local_relevance_score=0.0)
        text = self._candidate_text(candidate)
        hard_rejects: list[str] = []
        if (
            re.search(r"(晚餐|晚饭|dinner)", raw_need, re.IGNORECASE)
            and COFFEE_ONLY_RE.search(text)
            and not COFFEE_ONLY_RE.search(raw_need)
        ):
            hard_rejects.append("coffee_not_complete_dinner")
        if INSTITUTIONAL_MEAL_RE.search(text):
            hard_rejects.append("institutional_meal")
        if LODGING_MEAL_RE.search(text):
            hard_rejects.append("hotel_meal")
        if hard_rejects:
            return MealCandidateQuality(False, -0.32, hard_rejects)

        provider_type_code = self._value(candidate, "providerTypeCode", "provider_type_code")
        provider_type = self._value(candidate, "type", "category")
        source = self._value(candidate, "source")
        tags = self._list_value(candidate, "tags")
        claims = self._list_value(candidate, "sourceClaims", "source_claims")
        city_key = re.sub(r"(?:特别行政区|自治州|地区|盟|市)$", "", str(city or "").strip())
        cuisine_tokens = {
            token.strip().casefold()
            for value in [provider_type, *(str(item) for item in tags)]
            for token in re.split(r"[;；/／|]", value)
            if token.strip()
        }
        authoritative_city_cuisine = bool(
            city_key
            and len(city_key) >= 2
            and f"{city_key}菜".casefold() in cuisine_tokens
        )
        food_service_detail = bool(
            provider_type_code.startswith("05")
            and re.search(r"(?:^|[;；/／])餐饮服务(?:$|[;；/／])", provider_type)
        )
        supporting_claims = [
            claim
            for claim in claims
            if isinstance(claim, Mapping)
            and str(
                claim.get("claimKey")
                or claim.get("claim_key")
                or claim.get("claimType")
                or claim.get("claim_type")
                or ""
            )
            .strip()
            .casefold()
            in {"local_food", "local_food_context"}
            and str(claim.get("stance") or "support") == "support"
            and self._claim_matches_locality(claim, city_key)
        ]
        if source == "amap-place-search" and provider_type_code.startswith("05") and authoritative_city_cuisine:
            return MealCandidateQuality(
                True,
                0.12,
                soft_reasons=["authoritative_amap_local_food_subtype"],
            )
        if food_service_detail and supporting_claims:
            return MealCandidateQuality(True, 0.1, soft_reasons=["provider_detail_and_claim_supported"])
        return MealCandidateQuality(
            False,
            -0.18,
            ["local_food_evidence_missing"],
            ["generic_local_food_requires_provider_detail_and_claim"],
        )

    def _candidate_text(self, candidate: Any) -> str:
        fields = ("name", "type", "category", "address", "district")
        return " ".join(self._value(candidate, field_name) for field_name in fields)

    @staticmethod
    def _claim_matches_locality(claim: Mapping[str, Any], city_key: str) -> bool:
        if not city_key:
            return False
        locality = str(claim.get("locality") or claim.get("city") or claim.get("localityName") or "").strip()
        locality_key = re.sub(r"(?:特别行政区|自治区|自治州|地区|盟|市)$", "", locality)
        return bool(locality_key and locality_key.casefold() == city_key.casefold())

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

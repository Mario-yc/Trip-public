import re
from typing import Any, Optional

from src.services.meal_experience_assignment import MealExperienceAssignmentPolicy


LOCAL_FOOD_REQUEST_RE = re.compile(r"(当地|本地|特色|地方|风味|老字号|小吃|菜系|美食|夜市|传统)")
LOCAL_FOOD_CANDIDATE_RE = re.compile(r"(当地|本地|特色|地方|风味|老字号|小吃|菜系|美食|夜市|传统)")
GENERIC_BRANCH_RE = re.compile(
    r"(\([^)]*(店|门店|分店|总店|旗舰店|广场|商场|中心|校区|街|路)[^)]*\)|（[^）]*(店|门店|分店|总店|旗舰店|广场|商场|中心|校区|街|路)[^）]*）)"
)
TRAILING_BRANCH_RE = re.compile(r"(总店|分店|旗舰店|门店|直营店|加盟店)$")
CUISINE_TOKEN_RE = re.compile(r"(火锅|烤肉|烧烤|面馆|面食|粉面|饺子|包子|小吃|咖啡|茶饮|甜品)")
class MealDiversityPolicy:
    def canonical_meal_brand(self, candidate_or_name: Any) -> str:
        evidence = self._mapping_field(candidate_or_name, "mealSemanticEvidence", "meal_semantic_evidence")
        if evidence:
            grounded = str(evidence.get("canonicalBrand") or "").strip()
            if grounded:
                return grounded
        raw_name = (
            candidate_or_name.get("name")
            if isinstance(candidate_or_name, dict)
            else getattr(candidate_or_name, "name", candidate_or_name)
        )
        original = str(raw_name or "").strip().lower()
        if not original:
            return ""
        name = original
        name = GENERIC_BRANCH_RE.sub("", name)
        name = re.sub(r"[\s\-_,.·・，。]+", "", name)
        previous = None
        while previous != name:
            previous = name
            name = TRAILING_BRANCH_RE.sub("", name)
        if not name:
            name = re.sub(r"[\s\-_,.()（）·・，。]+", "", original)
        return name[:32]

    def cuisine_key(self, candidate: Any) -> str:
        text = self._candidate_text(candidate)
        tokens = CUISINE_TOKEN_RE.findall(text)
        if not tokens:
            return ""
        return "|".join(sorted(set(tokens))[:3])

    def dish_family(self, candidate: Any) -> str:
        evidence = self._mapping_field(candidate, "mealSemanticEvidence", "meal_semantic_evidence")
        grounded_family = str(evidence.get("groundedFamilyKey") or "").strip()
        if grounded_family:
            return grounded_family
        evidence_family = MealExperienceAssignmentPolicy.family_for_candidate(candidate)
        if evidence_family:
            return evidence_family
        return self.cuisine_key(candidate)

    def requires_local_food(self, raw_need: str, hints: Optional[list[str]] = None) -> bool:
        text = f"{raw_need} {' '.join(hints or [])}"
        return bool(LOCAL_FOOD_REQUEST_RE.search(text))

    def local_relevance_score(self, raw_need: str, hints: Optional[list[str]], candidate: Any) -> float:
        if not self.requires_local_food(raw_need, hints):
            return 0.0
        provider_type_code = self._field(candidate, "providerTypeCode", "provider_type_code")
        tags = self._list_field(candidate, "tags")
        claims = self._list_field(candidate, "sourceClaims", "source_claims")
        if provider_type_code and tags and any(
            isinstance(claim, dict) and str(claim.get("stance") or "support") == "support"
            for claim in claims
        ):
            return 0.08
        return -0.08

    def duplicate_reason(
        self,
        candidate: Any,
        day_brands: set[str],
        trip_brands: set[str],
        day_dish_families: Optional[set[str]] = None,
        trip_dish_families: Optional[set[str]] = None,
    ) -> str:
        brand = self.canonical_meal_brand(candidate)
        if brand and brand in day_brands:
            return "duplicate_meal_brand_same_day"
        if brand and brand in trip_brands:
            return "duplicate_meal_brand_trip"
        family = self.dish_family(candidate)
        if family and family in (day_dish_families or set()):
            return "duplicate_meal_dish_family_same_day"
        if family and family in (trip_dish_families or set()):
            return "duplicate_meal_dish_family_trip"
        return ""

    def matches_user_explicit_exclusion(
        self,
        candidate: Any,
        exclusions: set[str],
    ) -> bool:
        text = re.sub(r"[\s\-_,.()（）·・，。]+", "", self._candidate_text(candidate).casefold())
        return any(
            normalized and normalized in text
            for normalized in (
                re.sub(r"[\s\-_,.()（）·・，。]+", "", str(item or "").casefold())
                for item in exclusions
            )
        )

    def _candidate_text(self, candidate: Any) -> str:
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, dict):
            return " ".join(
                str(candidate.get(key) or "")
                for key in ("name", "type", "category", "address", "district")
            ) + " " + " ".join(str(item) for item in candidate.get("tags") or [])
        return " ".join(
            [
                str(getattr(candidate, "name", "") or ""),
                str(getattr(candidate, "type", "") or ""),
                str(getattr(candidate, "category", "") or ""),
                str(getattr(candidate, "address", "") or ""),
                str(getattr(candidate, "district", "") or ""),
                " ".join(str(item) for item in getattr(candidate, "tags", []) or []),
            ]
        )

    def _overlaps(self, left: str, right: str) -> bool:
        left_norm = re.sub(r"[\s\-_,.()（）·・，。]+", "", left.lower())
        right_norm = re.sub(r"[\s\-_,.()（）·・，。]+", "", right.lower())
        if not left_norm or not right_norm:
            return False
        if left_norm in right_norm or right_norm in left_norm:
            return True
        return any(len(token) >= 2 and token in right_norm for token in re.split(r"[|/、,，\s]+", left_norm))

    @staticmethod
    def _field(candidate: Any, *names: str) -> str:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, dict) else getattr(candidate, name, None)
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _list_field(candidate: Any, *names: str) -> list[Any]:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, dict) else getattr(candidate, name, None)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _mapping_field(candidate: Any, *names: str) -> dict[str, Any]:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, dict) else getattr(candidate, name, None)
            if isinstance(value, dict):
                return value
        return {}

import re
from typing import Any


MOCK_AMAP_ID_RE = re.compile(
    r"^(?:mock[_-]?amap(?:[_-].*)?|fake(?:[_-]?amap)?(?:[_-].*)?|spoof(?:[_-].*)?)$",
    re.IGNORECASE,
)
MOCK_PROVENANCE_RE = re.compile(
    r"(CLI eval mock|mock AMap candidate|runtime eval mock|only used when mockMapProvider|not a real map POI)",
    re.IGNORECASE,
)
GENERIC_MEAL_PLACEHOLDER_RE = re.compile(
    r"^(?:[\u4e00-\u9fa5A-Za-z]{0,12})?(?:本地菜餐厅|特色小吃馆|老字号餐厅|家常菜馆)$"
)


def _value(candidate: Any, *names: str) -> str:
    for name in names:
        value = getattr(candidate, name, None)
        if value:
            return str(value)
    return ""


class PoiTrustPolicy:
    def is_mock_or_synthetic_candidate(self, candidate: Any) -> bool:
        candidate_id = _value(candidate, "id", "amap_id", "amapId")
        if candidate_id and MOCK_AMAP_ID_RE.match(candidate_id):
            return True
        source_note = _value(candidate, "source_note", "sourceNote")
        if source_note and MOCK_PROVENANCE_RE.search(source_note):
            return True
        source = _value(candidate, "source")
        return source in {"mock-amap-place-search", "synthetic-amap-place-search"}

    def is_generic_meal_placeholder_name(self, name: Any) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        compact = re.sub(r"\s+", "", text)
        return bool(GENERIC_MEAL_PLACEHOLDER_RE.match(compact))

    def meal_candidate_rejection_reason(self, candidate: Any) -> str:
        if self.is_mock_or_synthetic_candidate(candidate):
            return "mock_or_synthetic_candidate"
        if self.is_generic_meal_placeholder_name(getattr(candidate, "name", "")):
            return "generic_meal_placeholder_candidate"
        return ""

    def is_mock_or_synthetic_poi_values(
        self,
        *,
        source: Any = "",
        amap_id: Any = "",
        source_note: Any = "",
        name: Any = "",
        kind: Any = "",
        intent_type: Any = "",
    ) -> bool:
        if amap_id and MOCK_AMAP_ID_RE.match(str(amap_id)):
            return True
        if source_note and MOCK_PROVENANCE_RE.search(str(source_note)):
            return True
        if str(source or "") in {"mock-amap-place-search", "synthetic-amap-place-search"}:
            return True
        if str(kind or "") == "meal" or str(intent_type or "") in {"meal", "dining", "food"}:
            return self.is_generic_meal_placeholder_name(name)
        return False

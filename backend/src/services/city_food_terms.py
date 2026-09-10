"""City-neutral meal search seeds.

The compatibility module name is retained for callers, but it intentionally
contains no city, dish, brand, or neighborhood answer table. User-explicit
food constraints are preserved verbatim; generic local-food requests remain
generic until provider detail and source claims establish relevance.
"""

from __future__ import annotations

import re


LOCAL_FOOD_NEED_RE = re.compile(
    r"(当地|本地|地方|特色|地道|风味|老字号|小吃|菜系|美食|夜市|传统)"
)


def city_food_terms(city: str, meal_label: str = "", *, limit: int = 8) -> list[str]:
    del city, meal_label, limit
    return []


def city_food_search_hints(
    city: str,
    raw_need: str = "",
    meal_label: str = "",
    *,
    limit: int = 8,
) -> list[str]:
    city = re.sub(r"\s+", " ", str(city or "")).strip() or "目的地"
    raw_need = re.sub(r"\s+", " ", str(raw_need or "")).strip()
    explicit = _explicit_food_constraint(raw_need)
    values = []
    if explicit:
        values.append(f"{city} {explicit} 餐厅")
    values.extend(
        [
            f"{city} 当地特色餐厅",
            f"{city} 地方风味餐厅",
            f"{city} 传统市场周边餐饮",
        ]
    )
    return _dedupe(values)[: max(0, int(limit))]


def _explicit_food_constraint(raw_need: str) -> str:
    value = re.sub(r"^(早餐|午餐|晚餐|小吃/咖啡)\s*", "", raw_need).strip()
    if not value or (LOCAL_FOOD_NEED_RE.search(value) and len(value) <= 10):
        return ""
    return value[:40]


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

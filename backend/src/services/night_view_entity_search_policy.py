from __future__ import annotations

import re
from typing import Iterable


class NightViewEntitySearchPolicy:
    """Separate semantic night-view discovery terms from physical POI names."""

    _GENERIC_CORES = {
        "夜景",
        "夜游",
        "观景",
        "观景点",
        "观景台",
        "观景平台",
        "摄影",
        "拍摄",
        "日落",
        "天际线",
        "城市夜景",
        "城市天际线",
        "公共夜景",
        "公共城市夜景",
        "公共城市夜景空间",
        "公共天际线视野",
        "公共观景空间",
        "滨水",
        "滨水夜游",
        "滨水空间",
        "滨水夜间公共空间",
        "夜游步道",
        "城市高点",
        "公共高点",
        "城市公园",
        "公共广场",
        "景点",
        "地标",
    }
    _EDITORIAL_RE = re.compile(
        r"(?:推荐|攻略|指南|盘点|榜单|合集|哪里|去哪|最佳|最美|必去|必看|"
        r"拍摄|摄影|日落|天际线|夜景地图|夜景路线|骑行信息|旅游信息|"
        r"资讯|线路|路线|专题|文旅|游记)"
    )
    _SEMANTIC_ONLY_RE = re.compile(
        r"^(?:(?:公共|户外|城市|滨水|夜间|开阔|代表性|不同|可用|当地)"
        r"|(?:夜景|夜游|观景|空间|体验|视野|高点|地点|景点|地标|公园|广场|步道|摄影|拍摄|日落|天际线))+$"
    )

    @classmethod
    def cleaned_hint(cls, value: object, city: object = "") -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。;；|｜")
        city_text = re.sub(r"\s+", " ", str(city or "")).strip()
        if city_text:
            text = re.sub(rf"^{re.escape(city_text)}\s*", "", text).strip()
        return re.sub(
            r"\s*(?:夜景|观景(?:点|台|平台)?|夜游)(?:空间|体验|视野|地点)?$",
            "",
            text,
        ).strip()

    @classmethod
    def is_generic_entity_seed(cls, value: object, city: object = "") -> bool:
        cleaned = cls.cleaned_hint(value, city)
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", cleaned).lower()
        if not normalized or len(normalized) < 2:
            return True
        if normalized in cls._GENERIC_CORES:
            return True
        if cls._EDITORIAL_RE.search(normalized):
            return True
        return bool(cls._SEMANTIC_ONLY_RE.fullmatch(normalized))

    @classmethod
    def named_entity_hints(
        cls,
        values: Iterable[object],
        *,
        city: object = "",
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            cleaned = cls.cleaned_hint(raw, city)
            normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", cleaned).lower()
            if cls.is_generic_entity_seed(cleaned) or normalized in seen:
                continue
            seen.add(normalized)
            result.append(cleaned)
        return result

    @classmethod
    def direct_query_keywords(
        cls,
        *,
        candidate_hints: Iterable[object],
        city: object = "",
        semantic_text: object = "",
        occurrence_index: int = 0,
    ) -> list[str]:
        named = cls.named_entity_hints(candidate_hints, city=city)
        semantic = str(semantic_text or "")
        # Provider text search is an entity catalogue, not a semantic search
        # engine.  For a generic city-view request, search actual viewing
        # facilities and central public-night-space concepts.  Do not silently
        # translate "public city view" into "waterfront": in large cities that
        # query commonly returns remote reservoirs and suburban parks which
        # are semantically weak and impossible to place on the user's route.
        facet_ladders = (
            ("夜游景区", "夜景公园", "城市观景台"),
            ("城市阳台", "夜游步道", "观景平台"),
            ("地标观景", "夜景广场", "城市夜游"),
        )
        ladder_index = max(0, int(occurrence_index or 0)) % len(facet_ladders)
        facets = list(facet_ladders[ladder_index])
        # ``水域景观`` is a broad provider type that is commonly present in a
        # public-city-view profile.  It is not evidence that the user selected
        # a waterfront experience.  Only explicit waterfront language may
        # switch the search ladder away from the general city-view strategy.
        if re.search(r"滨水|河岸|湖岸|水岸|码头|江边|河边|湖边", semantic):
            waterfront_ladders = (
                ("滨水公园", "滨水步道", "滨水广场"),
                ("河岸夜游", "水岸公园", "滨水观景台"),
                ("湖岸夜景", "夜游码头", "滨水夜景"),
            )
            facets = list(waterfront_ladders[ladder_index])
        # Concrete hints remain first for the first occurrence.  Later repeated
        # occurrences must spend at least one provider call on a distinct
        # catalogue facet before any shared hint can consume the bounded beam.
        return cls._dedupe(
            [*facets, *named] if occurrence_index > 0 else [*named, *facets]
        )

    @staticmethod
    def web_discovery_keyword(
        *,
        semantic_text: object = "",
        occurrence_index: int = 0,
    ) -> str:
        semantic = str(semantic_text or "")
        ladder_index = max(0, int(occurrence_index or 0)) % 3
        if re.search(r"滨水|河岸|湖岸|水岸|码头|江边|河边|湖边", semantic):
            return (
                "城市夜景 具体地点名称 滨水公园 滨水步道",
                "城市夜景 具体地点名称 河岸夜游 水岸公园",
                "城市夜景 具体地点名称 湖岸夜景 夜游码头",
            )[ladder_index]
        return (
            "城市夜景 具体地点名称 夜游景区 观景台",
            "城市夜景 具体地点名称 城市阳台 夜游步道 观景平台",
            "城市夜景 具体地点名称 地标观景 夜景广场 城市夜游",
        )[ladder_index]

    @staticmethod
    def _dedupe(values: Iterable[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            normalized = text.lower()
            if not text or normalized in seen:
                continue
            seen.add(normalized)
            result.append(text)
        return result

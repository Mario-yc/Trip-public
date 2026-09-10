from dataclasses import dataclass, field
from typing import Optional

from src.models.source_material import SourceMaterial
from src.providers.base.interfaces import BaseProvider
from src.providers.base.results import ProviderStatus


@dataclass
class ExtractedInspiration:
    city_candidates: list[str] = field(default_factory=list)
    poi_candidates: list[dict] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    budget_clues: list[str] = field(default_factory=list)
    route_clues: list[str] = field(default_factory=list)
    confidence: float = 0.75
    needs_user_confirmation: bool = False
    source_links: list[str] = field(default_factory=list)
    provider_name: str = "mock-vision-provider"
    fallback_used: bool = False
    provider_failure_reason: Optional[str] = None
    user_visible_caveat: Optional[str] = None


class VisionService:
    def extract_from_materials(
        self,
        city_hint: Optional[str],
        text_items: list[str],
        social_links: list[str],
        materials: Optional[list[SourceMaterial]] = None,
        provider_mode: str = "mock",
        default_provider: Optional[BaseProvider] = None,
        mock_provider: Optional[BaseProvider] = None,
    ) -> ExtractedInspiration:
        materials = materials or []
        mock_provider_name = mock_provider.name if mock_provider else "mock-vision-provider"

        if provider_mode == "default":
            try:
                default_health = default_provider.health() if default_provider else None
                if default_health is None or default_health.status != ProviderStatus.available:
                    raise RuntimeError("Default vision provider is not configured.")
                return self._heuristic_extract(
                    city_hint=city_hint,
                    text_items=text_items,
                    social_links=social_links,
                    materials=materials,
                    provider_name=default_health.provider_name,
                    fallback_used=False,
                )
            except Exception as exc:
                return self._heuristic_extract(
                    city_hint=city_hint,
                    text_items=text_items,
                    social_links=social_links,
                    materials=materials,
                    provider_name=mock_provider_name,
                    fallback_used=True,
                    provider_failure_reason=str(exc),
                    user_visible_caveat="默认视觉 provider 不可用，已使用 mock 识别结果继续生成行程草案。",
                )

        return self._heuristic_extract(
            city_hint=city_hint,
            text_items=text_items,
            social_links=social_links,
            materials=materials,
            provider_name=mock_provider_name,
            fallback_used=False,
        )

    def _heuristic_extract(
        self,
        city_hint: Optional[str],
        text_items: list[str],
        social_links: list[str],
        materials: list[SourceMaterial],
        provider_name: str,
        fallback_used: bool,
        provider_failure_reason: Optional[str] = None,
        user_visible_caveat: Optional[str] = None,
    ) -> ExtractedInspiration:
        combined_text = self._combined_text(text_items, materials)
        source_links = self._source_links(social_links, materials)
        city = city_hint or self._guess_city(combined_text) or "北京"

        poi_candidates = []
        for name in self._explicit_poi_names(combined_text):
            poi_candidates.append({"name": name, "confidence": 0.9, "sourceLinks": source_links[:2]})

        if not poi_candidates:
            for name in ["故宫博物院", "外滩", "广州塔", "深圳湾公园"]:
                if name in combined_text:
                    poi_candidates.append({"name": name, "confidence": 0.82, "sourceLinks": source_links[:2]})

        if not poi_candidates:
            poi_candidates.append({"name": f"{city}热门景点", "confidence": 0.48, "sourceLinks": source_links[:2]})

        style_tags = []
        if "拍照" in combined_text or "打卡" in combined_text:
            style_tags.append("拍照优先")
        if "轻松" in combined_text or "不赶" in combined_text:
            style_tags.append("轻松不赶路")
        if not style_tags:
            style_tags.append("自由行")

        budget_sources = [*text_items, *[material.raw_text for material in materials if material.raw_text]]
        budget_clues = [item for item in budget_sources if "预算" in item or "元" in item]

        return ExtractedInspiration(
            city_candidates=[city],
            poi_candidates=poi_candidates,
            style_tags=style_tags,
            budget_clues=budget_clues,
            route_clues=["待用户确认路线顺序"],
            confidence=0.72,
            needs_user_confirmation=any(candidate["confidence"] < 0.6 for candidate in poi_candidates),
            source_links=source_links,
            provider_name=provider_name,
            fallback_used=fallback_used,
            provider_failure_reason=provider_failure_reason,
            user_visible_caveat=user_visible_caveat,
        )

    def _combined_text(self, text_items: list[str], materials: list[SourceMaterial]) -> str:
        fragments = list(text_items)
        for material in materials:
            if material.raw_text:
                fragments.append(material.raw_text)
            if material.kind == "scenery_photo":
                fragments.append("风景照片 拍照 打卡")
            if material.kind in {"screenshot", "map_screenshot", "image_set"}:
                fragments.append("截图 地图 攻略")
        return " ".join(fragments)

    def _source_links(self, social_links: list[str], materials: list[SourceMaterial]) -> list[str]:
        links = []
        for link in social_links:
            if link and link not in links:
                links.append(link)
        for material in materials:
            if material.link_url and material.link_url not in links:
                links.append(material.link_url)
        return links

    def _guess_city(self, text: str) -> Optional[str]:
        for city in ["北京", "上海", "广州", "深圳"]:
            if city in text:
                return city
        return None

    def _city_for_poi(self, poi_name: str) -> list[str]:
        mapping = {
            "故宫博物院": ["北京"],
            "外滩": ["上海"],
            "广州塔": ["广州"],
            "深圳湾公园": ["深圳"],
        }
        return mapping.get(poi_name, [])

    def _explicit_poi_names(self, text: str) -> list[str]:
        known_pois = [
            "北京大学",
            "清华大学",
            "故宫博物院",
            "天坛公园",
            "颐和园",
            "圆明园",
            "景山公园",
            "北海公园",
            "外滩",
            "广州塔",
            "深圳湾公园",
        ]
        names = []
        for name in known_pois:
            if name in text and name not in names:
                names.append(name)
        if "故宫" in text and "故宫博物院" not in names:
            names.append("故宫博物院")
        return names

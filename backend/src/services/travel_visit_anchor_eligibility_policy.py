"""Fail-closed policy for POIs that may be shown as travel visit anchors."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class TravelVisitAnchorEligibilityDecision:
    classification: str
    reason_code: str


class TravelVisitAnchorEligibilityPolicy:
    """Separate visit destinations from useful-but-non-visitable area seeds."""

    _AREA_SEED_ONLY = re.compile(
        r"(社区服务站|社区便民点|便民服务|养老服务驿站|街道办事处|管理处|政务服务中心|"
        r"社区卫生服务站|房地产服务|停车场|公司办公|写字楼|住宅小区(入口|门)?$)"
    )
    _MARKET = re.compile(r"(菜市场|农贸市场|食品市场|传统市集|市集|商业街|步行街|夜市)")
    _LOCAL_LIFE = re.compile(
        r"(菜市场|农贸市场|食品市场|传统市集|市集|商业街|步行街|夜市|生活街区|社区商业|本地餐饮集群)"
    )
    _HERITAGE = re.compile(r"(历史街区|胡同|古街|传统建筑|历史保护区|文化街区)")
    _ART = re.compile(r"(艺术园区|艺术街区|艺术区|创意园|画廊|艺术中心|设计街区|美术馆)")
    _PARK = re.compile(r"(公园|滨水绿地|森林公园|湿地|绿道)")
    _MEAL = re.compile(r"(餐厅|餐馆|饭店|酒楼|小吃|餐饮|菜馆|面馆|正餐|市场)")

    def evaluate(
        self,
        candidate: dict[str, Any],
        *,
        family: str,
        explicit_research_intent: bool = False,
    ) -> TravelVisitAnchorEligibilityDecision:
        name = str(candidate.get("name") or candidate.get("title") or "").strip()
        category = str(candidate.get("type") or candidate.get("category") or "").strip()
        text = f"{name} {category}"
        if not name:
            return TravelVisitAnchorEligibilityDecision("reject", "anchor_name_missing")
        if self._AREA_SEED_ONLY.search(text) and not explicit_research_intent:
            return TravelVisitAnchorEligibilityDecision("area_seed_only", "functional_facility_not_visit_anchor")

        normalized_family = str(family or "").strip().casefold()
        required_pattern = {
            "heritage_walk": self._HERITAGE,
            "local_life": self._LOCAL_LIFE,
            "market_walk": self._MARKET,
            "art_walk": self._ART,
            "park_relax": self._PARK,
            "meal": self._MEAL,
        }.get(normalized_family)
        if required_pattern is not None and not required_pattern.search(text):
            return TravelVisitAnchorEligibilityDecision("reject", f"{normalized_family}_visit_anchor_mismatch")
        return TravelVisitAnchorEligibilityDecision("final_visit_anchor", "eligible_travel_visit_anchor")

from dataclasses import dataclass
from typing import Optional


RANK_ORDER = {
    "official": 0,
    "map": 1,
    "ota_aggregator": 2,
    "aggregator": 2,
    "guide": 3,
    "search": 4,
    "mock": 5,
    "unknown": 6,
    "unavailable": 7,
}

RANK_LABELS = {
    "official": "官方来源",
    "map": "地图服务",
    "ota_aggregator": "票务/OTA/聚合平台",
    "aggregator": "票务/OTA/聚合平台",
    "guide": "攻略或旅行内容",
    "search": "普通搜索结果",
    "mock": "mock/fallback 数据",
    "unknown": "unknown",
    "unavailable": "查询失败",
}


@dataclass
class SourceAssessment:
    source_name: str
    source_url: Optional[str]
    credibility_rank: str
    credibility_label: str
    provider_name: str
    confidence: float
    fallback_used: bool
    conflict_detected: bool
    conflict_reason: str
    recommendation: str


class SourceRankingService:
    def sort_by_credibility(self, items: list) -> list:
        return sorted(items, key=lambda item: RANK_ORDER.get(getattr(item, "credibility_rank", "unknown"), 99))

    def label_for_rank(self, rank: str) -> str:
        return RANK_LABELS.get(rank, RANK_LABELS["unknown"])

    def effective_rank(self, rank: str, fallback_used: bool) -> str:
        if fallback_used:
            return "mock"
        return rank if rank in RANK_ORDER else "unknown"

    def assess_ticket_sources(self, ticket_results: list) -> list[SourceAssessment]:
        assessments: list[SourceAssessment] = []
        for result in self.sort_by_credibility(ticket_results):
            effective_rank = self.effective_rank(getattr(result, "credibility_rank", "unknown"), bool(result.fallback_used))
            conflict_detected, conflict_reason = self._ticket_conflict_for_segment(result, ticket_results)
            assessments.append(
                SourceAssessment(
                    source_name=result.source_name,
                    source_url=result.source_url,
                    credibility_rank=effective_rank,
                    credibility_label=self.label_for_rank(effective_rank),
                    provider_name=result.provider_name,
                    confidence=result.confidence,
                    fallback_used=result.fallback_used,
                    conflict_detected=conflict_detected,
                    conflict_reason=conflict_reason,
                    recommendation=self._recommendation(effective_rank, result.fallback_used, conflict_detected),
                )
            )
        return sorted(assessments, key=lambda item: RANK_ORDER.get(item.credibility_rank, 99))

    def _ticket_conflict_for_segment(self, result, ticket_results: list) -> tuple[bool, str]:
        same_segment = [item for item in ticket_results if item.segment_id == result.segment_id]
        if len(same_segment) < 2 or any(item.fallback_used for item in same_segment):
            return False, ""
        statuses = {item.status for item in same_segment if item.status}
        prices = {round(float(item.price_estimate or 0), 2) for item in same_segment}
        if len(statuses) > 1:
            return True, "不同来源对预约/可用状态不一致。"
        if len(prices) > 1:
            return True, "不同来源给出的费用估算不一致。"
        return False, ""

    def _recommendation(self, rank: str, fallback_used: bool, conflict_detected: bool) -> str:
        if fallback_used or rank == "mock":
            return "这是 fallback 数据，只能用于 Demo 预览；出行前请重新核对官方来源。"
        if rank == "unavailable":
            return "联网查询失败，未返回真实来源；请检查 provider 配置或人工核对官方渠道。"
        if conflict_detected:
            return "多来源存在冲突，建议以官方来源为准，并在预约前二次确认。"
        if rank == "official":
            return "优先采用该官方来源作为预约和开放规则依据。"
        if rank == "map":
            return "可用于位置、营业状态和路线判断，关键票务仍需官方确认。"
        if rank == "ota_aggregator" or rank == "aggregator":
            return "只能作为补充信息，预约要求与名额以官方实时页面为准。"
        if rank == "guide":
            return "可用于灵感和路线参考，不作为开放/预约规则的最终依据。"
        if rank == "search":
            return "普通搜索结果仅作辅助线索，请优先核对官方来源。"
        return "来源类型未知，请人工确认。"

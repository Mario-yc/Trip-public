from datetime import datetime, timezone

from src.models.ticket_lookup_result import TicketLookupResult
from src.services.source_ranking_service import SourceRankingService


def ticket(segment_id: str, rank: str, status: str, fallback: bool = False, price: float = 40) -> TicketLookupResult:
    return TicketLookupResult(
        id=f"ticket_{segment_id}_{rank}_{status}",
        segment_id=segment_id,
        ticket_type="attraction",
        status=status,
        price_estimate=price,
        booking_url=f"https://example.com/{rank}",
        source_name=f"{rank} source",
        source_url=f"https://example.com/{rank}",
        credibility_rank=rank,
        caveat="以官方为准",
        provider_name="test-provider",
        fallback_used=fallback,
        confidence=0.7,
        queried_at=datetime.now(timezone.utc),
    )


def test_source_ranking_matches_required_credibility_order():
    service = SourceRankingService()
    items = [
        ticket("seg_1", "mock", "unknown"),
        ticket("seg_1", "search", "available"),
        ticket("seg_1", "official", "available"),
        ticket("seg_1", "guide", "available"),
        ticket("seg_1", "map", "available"),
        ticket("seg_1", "ota_aggregator", "available"),
        ticket("seg_1", "unknown", "available"),
    ]

    ranks = [item.credibility_rank for item in service.sort_by_credibility(items)]

    assert ranks == ["official", "map", "ota_aggregator", "guide", "search", "mock", "unknown"]


def test_source_assessment_flags_conflicts_and_demotes_fallback_to_mock():
    service = SourceRankingService()
    items = [
        ticket("seg_1", "official", "reservation_required"),
        ticket("seg_1", "ota_aggregator", "available"),
        ticket("seg_2", "official", "available", fallback=True),
    ]

    assessments = service.assess_ticket_sources(items)

    assert assessments[0].credibility_rank == "official"
    assert assessments[0].conflict_detected is True
    assert "以官方来源为准" in assessments[0].recommendation
    fallback_assessment = next(item for item in assessments if item.source_name == "official source" and item.fallback_used)
    assert fallback_assessment.credibility_rank == "mock"
    assert fallback_assessment.credibility_label == "mock/fallback 数据"
    assert "fallback 数据" in fallback_assessment.recommendation

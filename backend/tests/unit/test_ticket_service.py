import sqlite3

from src.providers.travel_tools import WebSearchItem, WebSearchResponse
from src.services.ticket_service import TicketService


def test_official_reservation_gate_requires_source_identity_to_match_poi():
    service = TicketService(sqlite3.connect(":memory:"))
    search = WebSearchResponse(
        query="北京师范大学 访客参观 官方预约入口",
        provider_name="test-search",
        confidence=0.9,
        results=[
            WebSearchItem(
                title="故宫博物院参观预约",
                url="https://www.dpm.org.cn/visit/booking.html",
                snippet="故宫博物院实行提前预约参观。",
                source_name="故宫博物院",
                credibility_rank="official",
                confidence=0.95,
            ),
            WebSearchItem(
                title="北京师范大学访客参观预约",
                url="https://visit.bnu.edu.cn/booking",
                snippet="北京师范大学访客参观需提前预约。",
                source_name="北京师范大学",
                credibility_rank="official",
                confidence=0.92,
            ),
        ],
    )

    rows = service._ticket_sources(search, "北京师范大学")

    assert rows[0]["official"] == "false"
    assert rows[1]["official"] == "true"
    assert rows[1]["reservation_evidence"] == "true"


def test_official_reservation_gate_rejects_irrelevant_official_campaign_page():
    service = TicketService(sqlite3.connect(":memory:"))
    search = WebSearchResponse(
        query="北京师范大学 访客参观 官方预约入口",
        results=[
            WebSearchItem(
                title="北京师范大学暑期夏令营招生",
                url="https://summer.bnu.edu.cn/notice",
                snippet="研究生夏令营报名通知。",
                source_name="北京师范大学",
                credibility_rank="official",
                confidence=0.9,
            )
        ],
    )

    assert service._ticket_sources(search, "北京师范大学") == []

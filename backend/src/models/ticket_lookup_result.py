from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class TicketLookupResult:
    id: str
    segment_id: str
    ticket_type: str
    status: str
    price_estimate: float
    booking_url: str
    source_name: str
    source_url: str
    credibility_rank: str
    caveat: str
    provider_name: str
    fallback_used: bool
    confidence: float
    provider_failure_reason: Optional[str] = None
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

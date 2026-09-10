from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class POIRiskAlert:
    id: str
    plan_id: str
    segment_id: str
    poi_name: str
    status: str
    summary: str
    source_name: str
    confidence: float
    failure_reason: Optional[str] = None
    source_url: Optional[str] = None
    sources: list[dict] = field(default_factory=list)
    user_visible_caveat: str = ""
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

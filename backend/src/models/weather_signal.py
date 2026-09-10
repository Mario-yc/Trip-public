from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class WeatherSignal:
    id: str
    city: str
    date: str
    hourly_forecast: list[dict]
    daily_summary: str
    risk_level: str
    purpose_impact_reason: str
    source: str = "天气服务"
    data_status: str = "unavailable"
    confidence: float = 0.0
    failure_reason: Optional[str] = None
    source_url: Optional[str] = None
    user_visible_caveat: str = ""
    provider_name: str = ""
    fallback_used: bool = False
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

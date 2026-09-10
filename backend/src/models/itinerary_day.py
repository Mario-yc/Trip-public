from dataclasses import dataclass
from typing import Optional


@dataclass
class ItineraryDay:
    id: str
    plan_id: str
    day_number: int
    date: Optional[str] = None
    title: str = ""
    weather_summary: str = ""
    risk_summary: str = ""
    total_estimated_cost: float = 0.0

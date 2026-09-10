from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TrafficCrowdingSignal:
    id: str
    route_option_id: str
    real_data_available: bool
    crowding_level: str
    estimated_reason: str
    recommended_departure_adjustment: str
    source: str = "mock-traffic-provider"
    queried_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

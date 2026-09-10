from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ItinerarySegment:
    id: str
    day_id: str
    segment_order: int
    kind: str
    start_time: str
    end_time: str
    poi_id: str
    transport_mode: str
    estimated_cost: float
    notes: str
    estimate_metadata: dict[str, Any] = field(default_factory=dict)
    semantic_metadata: dict[str, Any] = field(default_factory=dict)
    weather_signal_id: Optional[str] = None
    traffic_crowding_signal_id: Optional[str] = None
    ticket_lookup_result_id: Optional[str] = None

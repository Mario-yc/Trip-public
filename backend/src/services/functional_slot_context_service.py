from dataclasses import dataclass, field
from typing import Optional

from src.models.poi import POI


@dataclass
class FunctionalSlotContext:
    slot_id: str
    day_number: int
    intent_type: str
    raw_need: str
    previous_anchor: Optional[POI]
    next_anchor: Optional[POI]
    same_day_anchors: list[POI] = field(default_factory=list)
    transport_mode: str = ""
    search_radius_meters: int = 1500
    max_detour_km: float = 8.0
    max_detour_minutes: int = 60

    @property
    def has_route_anchor(self) -> bool:
        return self.previous_anchor is not None or self.next_anchor is not None

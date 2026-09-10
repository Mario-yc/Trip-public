from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PreferenceProfile:
    id: str
    user_id: str
    budget_range: str = ""
    pace_preference: str = ""
    transport_preferences: list[str] = field(default_factory=list)
    food_preferences: list[str] = field(default_factory=list)
    photo_preference: str = ""
    accessibility_notes: str = ""
    party_size: int = 1
    traveler_types: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PreferenceSummaryCard:
    id: str
    profile_id: str
    party_size: int
    traveler_types: list[str]
    budget_range: str
    pace_preference: str
    items: list[dict]
    summary_text: str = ""
    status: str = "draft"
    removed_items: list[dict] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

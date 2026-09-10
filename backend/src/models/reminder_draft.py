from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ReminderDraft:
    id: str
    email_address: str
    trigger_date: str
    subject: str
    body: str
    inspiration_set_id: Optional[str] = None
    itinerary_plan_id: Optional[str] = None
    simulated_status: str = "scheduled"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

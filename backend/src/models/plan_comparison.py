from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PlanComparison:
    id: str
    inspiration_set_id: str
    city: str
    plan_ids: list[str]
    provider_name: str
    fallback_used: bool
    user_visible_caveat: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractionResult:
    id: str
    inspiration_set_id: str
    city_candidates: list[str] = field(default_factory=list)
    poi_candidates: list[dict] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    budget_clues: list[str] = field(default_factory=list)
    route_clues: list[str] = field(default_factory=list)
    confidence: float = 0.0
    needs_user_confirmation: bool = False
    source_links: list[str] = field(default_factory=list)
    provider_name: str = ""
    fallback_used: bool = False
    provider_failure_reason: Optional[str] = None
    user_visible_caveat: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

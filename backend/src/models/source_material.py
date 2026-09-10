from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SourceMaterial:
    id: str
    kind: str
    inspiration_set_id: Optional[str] = None
    raw_text: Optional[str] = None
    link_url: Optional[str] = None
    thumbnail_path: Optional[str] = None
    original_path: Optional[str] = None
    original_retention: str = "temporary_cache"
    cache_status: str = "retained"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

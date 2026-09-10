from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InspirationSet:
    id: str
    user_id: str
    city: Optional[str] = None
    status: str = "uploading"
    theme_summary: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

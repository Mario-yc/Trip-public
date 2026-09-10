from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class POI:
    id: str
    name: str
    city: str
    category: str
    latitude: Optional[float]
    longitude: Optional[float]
    photo_url: Optional[str] = None
    source: str = "unresolved-map-poi"
    confidence: float = 0.75
    amap_id: Optional[str] = None
    type: str = ""
    district: str = ""
    adcode: Optional[str] = None
    address: str = ""
    source_note: str = ""
    source_url: Optional[str] = None
    photos: list[dict] = None
    tags: list[str] = None
    source_claims: list[dict[str, Any]] = None
    parent_poi_id: Optional[str] = None
    indoor_parent_poi_id: Optional[str] = None
    provider_type_code: Optional[str] = None
    business_status: Optional[str] = None
    provider_queried_at: Optional[datetime] = None
    provider_query_receipt_fingerprint: Optional[str] = None
    provider_aliases: list[str] = field(default_factory=list)
    experience_independence_evidence: dict[str, Any] = field(default_factory=dict)
    # Transient proposal evidence. It is copied into segment
    # scheduleConstraints before persistence, so no SQLite POI migration is
    # required and provider-owned source claims remain unmodified.
    meal_semantic_evidence: dict[str, Any] = field(default_factory=dict)
    # Provider-owned opening evidence.  Simple Direction uses it only as a
    # scheduling constraint; an empty value remains explicitly unverified.
    open_time_today: Optional[str] = None
    open_time_week: Optional[str] = None

    def __post_init__(self) -> None:
        if self.photos is None:
            self.photos = []
        if self.tags is None:
            self.tags = []
        if self.source_claims is None:
            self.source_claims = []

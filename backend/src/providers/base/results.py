from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProviderKind(str, Enum):
    llm = "llm"
    vision = "vision"
    map = "map"
    ticket = "ticket"
    weather = "weather"
    traffic = "traffic"
    search = "search"
    email = "email"


class ProviderStatus(str, Enum):
    available = "available"
    degraded = "degraded"
    unavailable = "unavailable"


class CredibilityRank(str, Enum):
    official = "official"
    aggregator = "aggregator"
    search = "search"
    mock = "mock"
    unknown = "unknown"


class ProviderResult(BaseModel):
    provider_kind: ProviderKind = Field(alias="providerKind")
    provider_name: str = Field(alias="providerName")
    is_mock: bool = Field(alias="isMock")
    status: ProviderStatus
    source_name: Optional[str] = Field(default=None, alias="sourceName")
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    queried_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), alias="queriedAt")
    confidence: Optional[float] = None
    credibility_rank: CredibilityRank = Field(default=CredibilityRank.unknown, alias="credibilityRank")
    user_visible_caveat: Optional[str] = Field(default=None, alias="userVisibleCaveat")
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

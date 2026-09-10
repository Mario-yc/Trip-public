from typing import Any, Optional
from datetime import datetime

from pydantic import BaseModel, Field, field_serializer


class MapConfigResponse(BaseModel):
    provider: str
    enabled: bool
    js_api_key: str = Field(alias="jsApiKey")
    security_js_code: Optional[str] = Field(default=None, alias="securityJsCode")

    model_config = {"populate_by_name": True}


class MapPoiPhotoResponse(BaseModel):
    title: str
    url: str


class MapPoiResponse(BaseModel):
    id: str
    name: str
    type: str
    city: str
    district: str
    adcode: Optional[str] = None
    address: str
    longitude: float
    latitude: float
    category: str
    source: str
    source_note: str = Field(alias="sourceNote")
    distance_meters: Optional[float] = Field(default=None, alias="distanceMeters")
    confidence: float
    provider_type_code: Optional[str] = Field(default=None, alias="providerTypeCode")
    # Only aliases returned by the AMap response, never user/model guesses.
    provider_aliases: list[str] = Field(default_factory=list, alias="providerAliases")
    tags: list[str] = Field(default_factory=list)
    business_area: Optional[str] = Field(default=None, alias="businessArea")
    rating: Optional[float] = None
    cost: Optional[float] = None
    open_time_today: Optional[str] = Field(default=None, alias="openTimeToday")
    open_time_week: Optional[str] = Field(default=None, alias="openTimeWeek")
    parent_poi_id: Optional[str] = Field(default=None, alias="parentPoiId")
    indoor_parent_poi_id: Optional[str] = Field(default=None, alias="indoorParentPoiId")
    business_status: Optional[str] = Field(default=None, alias="businessStatus")
    provider_queried_at: Optional[datetime] = Field(default=None, alias="providerQueriedAt")
    provider_query_receipt_fingerprint: Optional[str] = Field(
        default=None,
        alias="providerQueryReceiptFingerprint",
    )
    children: list[dict[str, Any]] = Field(default_factory=list)
    photos: list[MapPoiPhotoResponse] = Field(default_factory=list)
    source_claims: list[dict[str, Any]] = Field(default_factory=list, alias="sourceClaims")

    model_config = {"populate_by_name": True}

    @field_serializer("provider_queried_at")
    def serialize_provider_queried_at(self, value: Optional[datetime]) -> Optional[str]:
        """Keep nested POI evidence JSON-safe in every persistence/context path.

        This timestamp is evidence metadata, not an in-process datetime contract.
        Several existing callers intentionally use ``model_dump()`` rather than
        JSON mode before storing or embedding POIs, so the field serializer must
        be mode-independent.
        """

        return value.isoformat() if value is not None else None


class MapPoiSearchResponse(BaseModel):
    city: str
    keyword: str
    category: str
    provider_name: str = Field(alias="providerName")
    queried_at: datetime = Field(alias="queriedAt")
    pois: list[MapPoiResponse]
    cache_hit: bool = Field(default=False, alias="cacheHit")
    provider_query_receipt_fingerprint: Optional[str] = Field(
        default=None,
        alias="providerQueryReceiptFingerprint",
    )

    model_config = {"populate_by_name": True}


class MapPoiResolveNearRequest(BaseModel):
    longitude: float
    latitude: float
    radius: int = 1500


class MapPoiResolveQueryRequest(BaseModel):
    name: str
    category: str = "all"
    near: Optional[MapPoiResolveNearRequest] = None


class MapPoiResolveRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    turn_id: Optional[str] = Field(default=None, alias="turnId")
    city: str
    queries: list[MapPoiResolveQueryRequest]

    model_config = {"populate_by_name": True}


class MapPoiResolvedItemResponse(BaseModel):
    query: str
    status: str = "accepted"
    poi: MapPoiResponse


class MapPoiPendingItemResponse(BaseModel):
    candidate_record_id: str = Field(alias="candidateRecordId")
    query: str
    reason: str
    candidates: list[MapPoiResponse]

    model_config = {"populate_by_name": True}


class MapPoiResolveResponse(BaseModel):
    resolved: list[MapPoiResolvedItemResponse]
    pending: list[MapPoiPendingItemResponse]

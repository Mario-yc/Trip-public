from pydantic import BaseModel, Field
from typing import Optional


class SourceMaterialInput(BaseModel):
    kind: str
    inspiration_set_id: Optional[str] = Field(default=None, alias="inspirationSetId")
    raw_text: Optional[str] = Field(default=None, alias="rawText")
    link_url: Optional[str] = Field(default=None, alias="linkUrl")
    thumbnail_path: Optional[str] = Field(default=None, alias="thumbnailPath")
    save_original: bool = Field(default=False, alias="saveOriginal")

    model_config = {"populate_by_name": True}


class InspirationCreateRequest(BaseModel):
    city_hint: Optional[str] = Field(default=None, alias="cityHint")
    text_items: list[str] = Field(default_factory=list, alias="textItems")
    social_links: list[str] = Field(default_factory=list, alias="socialLinks")
    source_material_ids: list[str] = Field(default_factory=list, alias="sourceMaterialIds")
    save_original_images: bool = Field(default=False, alias="saveOriginalImages")

    model_config = {"populate_by_name": True}


class InspirationCreateResponse(BaseModel):
    inspiration_set_id: str = Field(alias="inspirationSetId")
    status: str
    source_material_ids: list[str] = Field(default_factory=list, alias="sourceMaterialIds")

    model_config = {"populate_by_name": True}


class SourceMaterialCreateResponse(BaseModel):
    source_material_id: str = Field(alias="sourceMaterialId")
    kind: str
    thumbnail_url: Optional[str] = Field(default=None, alias="thumbnailUrl")
    thumbnail_placeholder: bool = Field(default=True, alias="thumbnailPlaceholder")
    original_retention: str = Field(alias="originalRetention")
    cache_status: str = Field(alias="cacheStatus")
    metadata: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class SocialLinkIngestRequest(BaseModel):
    url: str


class SocialLinkIngestResponse(BaseModel):
    source_material_id: str = Field(alias="sourceMaterialId")
    fetch_status: str = Field(alias="fetchStatus")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    canonical_url: Optional[str] = Field(default=None, alias="canonicalUrl")
    extracted_text: Optional[str] = Field(default=None, alias="extractedText")

    model_config = {"populate_by_name": True}


class SourceMaterialCleanupResponse(BaseModel):
    cleared_count: int = Field(alias="clearedCount")
    retained_long_term_count: int = Field(alias="retainedLongTermCount")

    model_config = {"populate_by_name": True}


class PoiCandidate(BaseModel):
    name: str
    confidence: float
    source_links: list[str] = Field(default_factory=list, alias="sourceLinks")

    model_config = {"populate_by_name": True}


class CostItem(BaseModel):
    label: str
    amount_cny: float = Field(alias="amountCny")
    currency: str = "CNY"
    is_estimate: bool = Field(default=True, alias="isEstimate")

    model_config = {"populate_by_name": True}


class ItinerarySegment(BaseModel):
    id: str
    title: str
    poi_name: str = Field(alias="poiName")
    start_time: str = Field(alias="startTime")
    duration_minutes: int = Field(alias="durationMinutes")
    transport_mode: str = Field(alias="transportMode")
    cost_items: list[CostItem] = Field(default_factory=list, alias="costItems")
    reservation_notes: list[str] = Field(default_factory=list, alias="reservationNotes")

    model_config = {"populate_by_name": True}


class ItineraryDay(BaseModel):
    day_number: int = Field(alias="dayNumber")
    title: str
    segments: list[ItinerarySegment] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ItineraryDraft(BaseModel):
    title: str
    editable: bool = True
    days: list[ItineraryDay] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ExtractionResponse(BaseModel):
    inspiration_set_id: str = Field(alias="inspirationSetId")
    city_candidates: list[str] = Field(alias="cityCandidates")
    poi_candidates: list[PoiCandidate] = Field(alias="poiCandidates")
    style_tags: list[str] = Field(alias="styleTags")
    budget_clues: list[str] = Field(alias="budgetClues")
    route_clues: list[str] = Field(alias="routeClues")
    confidence: float
    needs_user_confirmation: bool = Field(alias="needsUserConfirmation")
    source_links: list[str] = Field(default_factory=list, alias="sourceLinks")
    provider_name: str = Field(alias="providerName")
    fallback_used: bool = Field(alias="fallbackUsed")
    provider_failure_reason: Optional[str] = Field(default=None, alias="providerFailureReason")
    user_visible_caveat: Optional[str] = Field(default=None, alias="userVisibleCaveat")
    itinerary_draft: ItineraryDraft = Field(alias="itineraryDraft")

    model_config = {"populate_by_name": True}

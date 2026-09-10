from typing import Optional

from pydantic import BaseModel, Field


class PreferenceExtractRequest(BaseModel):
    conversation_text: str = Field(alias="conversationText")

    model_config = {"populate_by_name": True}


class PreferenceUpdateRequest(BaseModel):
    party_size: Optional[int] = Field(default=None, alias="partySize")
    traveler_types: Optional[list[str]] = Field(default=None, alias="travelerTypes")
    budget_range: Optional[str] = Field(default=None, alias="budgetRange")
    pace_preference: Optional[str] = Field(default=None, alias="pacePreference")
    summary_text: Optional[str] = Field(default=None, alias="summaryText")
    items: Optional[list[dict]] = None

    model_config = {"populate_by_name": True}


class PreferenceSummaryCardResponse(BaseModel):
    id: str
    profile_id: str = Field(alias="profileId")
    party_size: int = Field(alias="partySize")
    traveler_types: list[str] = Field(alias="travelerTypes")
    budget_range: str = Field(alias="budgetRange")
    pace_preference: str = Field(alias="pacePreference")
    summary_text: str = Field(default="", alias="summaryText")
    items: list[dict]
    status: str
    provider_name: str = Field(default="preference-rule-extractor", alias="providerName")
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    provider_failure_reason: Optional[str] = Field(default=None, alias="providerFailureReason")
    user_visible_caveat: Optional[str] = Field(default=None, alias="userVisibleCaveat")

    model_config = {"populate_by_name": True}


class PreferenceEnvelope(BaseModel):
    summary_card: PreferenceSummaryCardResponse = Field(alias="summaryCard")

    model_config = {"populate_by_name": True}


class PreferenceMemoryUpdateRequest(BaseModel):
    memory_text: Optional[str] = Field(default=None, alias="memoryText")
    structured_memory: Optional[dict] = Field(default=None, alias="structuredMemory")
    auto_update_enabled: Optional[bool] = Field(default=None, alias="autoUpdateEnabled")

    model_config = {"populate_by_name": True}


class PreferenceMemoryResponse(BaseModel):
    user_id: str = Field(alias="userId")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    memory_text: str = Field(alias="memoryText")
    structured_memory: dict = Field(default_factory=dict, alias="structuredMemory")
    compiled_rules: dict = Field(default_factory=dict, alias="compiledRules")
    pending_confirmations: list[dict] = Field(default_factory=list, alias="pendingConfirmations")
    auto_update_enabled: bool = Field(alias="autoUpdateEnabled")
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")
    memory_diagnostics: dict = Field(default_factory=dict, alias="memoryDiagnostics")

    model_config = {"populate_by_name": True}

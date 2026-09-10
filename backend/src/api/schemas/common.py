from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    error_code: str = Field(alias="errorCode")
    message: str
    recoverable: bool = True
    fallback_used: bool = Field(default=False, alias="fallbackUsed")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app_name: str = Field(alias="appName")
    environment: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), alias="checkedAt")


class ApiEnvelope(BaseModel):
    data: Any

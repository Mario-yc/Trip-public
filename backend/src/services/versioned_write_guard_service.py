from typing import Optional

from fastapi import HTTPException


STALE_BASE_VERSION_DETAIL = "Base itinerary version is stale"
MISSING_BASE_VERSION_DETAIL = "Base itinerary version is required"


def ensure_base_version_current(base_version_id: Optional[str], active_version_id: Optional[str]) -> None:
    if active_version_id and not base_version_id:
        raise HTTPException(status_code=409, detail=MISSING_BASE_VERSION_DETAIL)
    if base_version_id and active_version_id and base_version_id != active_version_id:
        raise HTTPException(status_code=409, detail=STALE_BASE_VERSION_DETAIL)


def patch_validation_error_detail(patch_id: str, validation_errors: list[str]) -> dict:
    return {"patchId": patch_id, "validationErrors": validation_errors}

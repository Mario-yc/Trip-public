import pytest
from fastapi import HTTPException

from src.services.versioned_write_guard_service import (
    MISSING_BASE_VERSION_DETAIL,
    STALE_BASE_VERSION_DETAIL,
    ensure_base_version_current,
    patch_validation_error_detail,
)


def test_ensure_base_version_current_allows_matching_or_initial_missing_active_version():
    ensure_base_version_current("ver_1", "ver_1")
    ensure_base_version_current("ver_1", None)
    ensure_base_version_current(None, None)


def test_ensure_base_version_current_rejects_missing_base_version_for_active_version():
    with pytest.raises(HTTPException) as error:
        ensure_base_version_current(None, "ver_current")

    assert error.value.status_code == 409
    assert error.value.detail == MISSING_BASE_VERSION_DETAIL


def test_ensure_base_version_current_rejects_stale_base_version():
    with pytest.raises(HTTPException) as error:
        ensure_base_version_current("ver_old", "ver_current")

    assert error.value.status_code == 409
    assert error.value.detail == STALE_BASE_VERSION_DETAIL


def test_patch_validation_error_detail_is_api_error_compatible():
    detail = patch_validation_error_detail("patch_123", ["Day not found: day_missing"])

    assert detail == {
        "patchId": "patch_123",
        "validationErrors": ["Day not found: day_missing"],
    }

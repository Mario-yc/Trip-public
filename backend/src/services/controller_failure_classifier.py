from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass
from json import JSONDecodeError
from typing import Any, Optional
from urllib.error import URLError

from fastapi import HTTPException
from pydantic import ValidationError

from src.services.agent_decision_normalizer import DecisionNormalizationError
from src.services.controller_response_integrity import (
    ControllerOutputIncompleteError,
    ControllerOutputTruncatedError,
)
from src.services.request_activity_coverage_service import RequestActivityCoverageError


@dataclass(frozen=True)
class ControllerFailure:
    failure_class: str
    stage: str
    provider: str
    model: str
    duration_ms: int
    timeout_seconds: float
    retryable: bool
    http_status: Optional[int] = None
    schema_error_summary: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "failureClass": value["failure_class"],
            "stage": value["stage"],
            "provider": value["provider"],
            "model": value["model"],
            "durationMs": value["duration_ms"],
            "timeoutSeconds": value["timeout_seconds"],
            "retryable": value["retryable"],
            "httpStatus": value["http_status"],
            "schemaErrorSummary": value["schema_error_summary"],
        }


def classify_controller_failure(
    error: Exception,
    *,
    stage: str,
    provider: str,
    model: str,
    duration_ms: int,
    timeout_seconds: float,
) -> ControllerFailure:
    http_status = error.status_code if isinstance(error, HTTPException) else None
    text = str(error)
    schema_summary: Optional[str] = None
    retryable = True
    if isinstance(error, ControllerOutputTruncatedError):
        failure_class = "output_truncated"
        schema_summary = (
            f"finishReason={error.evidence.finish_reason};"
            f"contentBytes={error.evidence.content_bytes};"
            f"parseError={error.evidence.parse_error_category};"
            f"parsePosition={error.evidence.parse_error_position}"
        )[:500]
    elif isinstance(error, ControllerOutputIncompleteError):
        failure_class = "output_incomplete"
        schema_summary = (
            f"finishReason={error.evidence.finish_reason};"
            f"contentBytes={error.evidence.content_bytes};"
            f"parseError={error.evidence.parse_error_category}"
        )[:500]
    elif isinstance(error, (TimeoutError,)) or http_status in {408, 504}:
        failure_class = "provider_timeout"
    elif http_status == 429:
        failure_class = "rate_limited"
    elif isinstance(error, JSONDecodeError):
        failure_class = "invalid_json"
        schema_summary = text[:500]
    elif isinstance(error, RequestActivityCoverageError):
        failure_class = "schema_validation_failed"
        schema_summary = text[:500]
        # An intact response violating the request contract is not a transient
        # transport failure and must not gain an automatic retry from this tag.
        retryable = False
    elif isinstance(error, (ValidationError, DecisionNormalizationError)):
        failure_class = "action_directive_missing" if "action_directive_required_for_v2" in text else "schema_validation_failed"
        schema_summary = text[:500]
    elif isinstance(error, CancelledError):
        failure_class = "cancelled"
        retryable = False
    elif isinstance(error, RuntimeError) and text == "controller_provider_unavailable":
        failure_class = "provider_unavailable"
        retryable = False
    elif isinstance(error, ValueError) and text in {
        "controller_full_payload_too_large",
        "controller_repair_payload_too_large",
        "controller_lite_payload_too_large",
        "controller_full_projection_too_large",
        "controller_lite_projection_too_large",
    }:
        failure_class = "request_budget_exceeded"
        schema_summary = text
        retryable = False
    elif isinstance(error, (ConnectionError, OSError, URLError)):
        failure_class = "network_error"
    elif isinstance(error, HTTPException):
        failure_class = "provider_unavailable" if (http_status or 0) >= 500 or http_status == 400 else "network_error"
        retryable = failure_class != "provider_unavailable"
    else:
        failure_class = "controller_internal_error"
        schema_summary = text[:500]
        retryable = False
    return ControllerFailure(
        failure_class=failure_class,
        stage=stage,
        provider=provider,
        model=model,
        duration_ms=max(0, int(duration_ms)),
        timeout_seconds=max(0.0, float(timeout_seconds)),
        retryable=retryable,
        http_status=http_status,
        schema_error_summary=schema_summary,
    )

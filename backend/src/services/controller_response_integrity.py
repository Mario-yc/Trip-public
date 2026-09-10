from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


# These are decision-object budgets, not itinerary/search generation budgets.
# The previous live request consumed exactly 600 completion tokens and ended in
# the middle of ModelDecisionV3.  The bounded contract reserves enough room for
# one complete action directive while the transport still rejects any response
# that reaches the ceiling instead of treating it as parseable authority.
CONTROLLER_FULL_MAX_OUTPUT_TOKENS = 1_200
CONTROLLER_REPAIR_MAX_OUTPUT_TOKENS = 1_200
CONTROLLER_LITE_MAX_OUTPUT_TOKENS = 180


@dataclass(frozen=True)
class ControllerResponseIntegrityEvidence:
    finish_reason: str
    content_length: int
    content_bytes: int
    response_bytes: int | None
    parse_error_category: str
    parse_error_position: int | None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "finishReason": self.finish_reason,
            "contentLength": self.content_length,
            "contentBytes": self.content_bytes,
            "responseBytes": self.response_bytes,
            "parseErrorCategory": self.parse_error_category,
            "parseErrorPosition": self.parse_error_position,
        }


class ControllerResponseIntegrityError(RuntimeError):
    error_code = "controller_response_incomplete"

    def __init__(
        self,
        *,
        call_kind: str,
        evidence: ControllerResponseIntegrityEvidence,
    ) -> None:
        super().__init__(self.error_code)
        self.call_kind = call_kind if call_kind in {"full", "repair", "lite"} else "full"
        self.evidence = evidence

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "errorCode": self.error_code,
            "callKind": self.call_kind,
            **self.evidence.to_safe_dict(),
        }


class ControllerOutputTruncatedError(ControllerResponseIntegrityError):
    error_code = "controller_output_truncated"


class ControllerOutputIncompleteError(ControllerResponseIntegrityError):
    error_code = "controller_output_incomplete"


def controller_response_content(
    body: dict[str, Any],
    *,
    call_kind: str,
    response_bytes: int | None,
) -> str:
    """Return Controller content only when the provider marked it complete.

    The function may inspect JSON syntax for diagnostics, but it never repairs,
    trims, appends delimiters to, or returns a response whose finish reason is
    not ``stop``.  The complete object is still validated later by Pydantic.
    """

    choices = body.get("choices") if isinstance(body, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
    raw_content = message.get("content")
    content = raw_content if isinstance(raw_content, str) else ""
    finish_reason = str(first_choice.get("finish_reason") or "")
    parse_error_category = "not_checked"
    parse_error_position: int | None = None
    if content:
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            parse_error_category = error.msg[:120]
            parse_error_position = max(0, int(error.pos))
        else:
            parse_error_category = "syntactically_valid"
    else:
        parse_error_category = "empty_content"
    evidence = ControllerResponseIntegrityEvidence(
        finish_reason=finish_reason,
        content_length=len(content),
        content_bytes=len(content.encode("utf-8")),
        response_bytes=response_bytes,
        parse_error_category=parse_error_category,
        parse_error_position=parse_error_position,
    )
    if finish_reason == "length":
        raise ControllerOutputTruncatedError(call_kind=call_kind, evidence=evidence)
    if finish_reason != "stop" or not content:
        raise ControllerOutputIncompleteError(call_kind=call_kind, evidence=evidence)
    return content

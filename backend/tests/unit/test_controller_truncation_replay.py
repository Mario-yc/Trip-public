from __future__ import annotations

from hashlib import sha256
import json
from json import JSONDecodeError
from pathlib import Path

from src.services.agent_autonomy_service import AgentAutonomyController
from src.services.controller_response_integrity import controller_response_content


SOURCE_EPOCH = "f91e31f181bf522f82fbd9a93d0f89e182fd1f8f"
FROZEN_RESPONSE = Path(__file__).with_name("controller_full_length_truncated_response.json")
FROZEN_RESPONSE_BYTES = 2_271
FROZEN_RESPONSE_SHA256 = "ecb585d8b5b67dd2c288daf52a7a23c259d7877bf14757e868e7a13bdb3ff4de"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_shape() -> dict[str, object]:
    return {
        "schemaVersion": "controller-context-full-v1",
        "allowedActions": ["ask_user", "draft_itinerary"],
        "decisionContractVersion": "agent-decision-contract-v3",
        "itineraryLifecycle": "empty_scaffold",
        "goalIds": ["goal_campus_visit", "goal_night_view", "goal_daily_meal"],
    }


def _failure_fingerprint(response_bytes: bytes) -> tuple[str, dict[str, object]]:
    body = json.loads(response_bytes)
    choice = body["choices"][0]
    content = choice["message"]["content"]
    try:
        json.loads(content)
    except JSONDecodeError as error:
        parse_error_category = error.msg
        truncation_position = error.pos
    else:  # pragma: no cover - the frozen fixture is intentionally incomplete
        parse_error_category = "syntactically_valid_but_finish_reason_length"
        truncation_position = len(content)
    evidence = {
        "controllerSchemaVersion": "agent-decision-v3",
        "requestShapeFingerprint": sha256(_canonical_bytes(_request_shape())).hexdigest(),
        "finishReason": choice["finish_reason"],
        "providerResponseBytes": len(response_bytes),
        "contentBytes": len(content.encode("utf-8")),
        "parseErrorCategory": parse_error_category,
        "truncationPosition": truncation_position,
        "sourceEpoch": SOURCE_EPOCH,
        "terminalFailure": "controller_full_payload_too_large",
    }
    return sha256(_canonical_bytes(evidence)).hexdigest(), evidence


def _authoritative_context() -> dict[str, object]:
    required_goal = {
        "goalId": "goal_campus_visit",
        "intentType": "campus_visit",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferredCount": 2,
        "maxCount": 2,
        "allowedDayNumbers": [1, 2],
        "distributionPolicy": "spread_across_distinct_days",
    }
    return {
        "schemaVersion": "model-first-autonomy-context-v2",
        "latestUserMessage": "two day campus trip",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
        },
        "observation": {
            "request": {"intentContract": {"requiredIntents": [required_goal]}},
            "requirementCoverage": {"required": [required_goal]},
            "itinerary": {
                "lifecycleState": "active",
                "activeVersionId": "ver_frozen_existing",
                "meaningfulSegmentCount": 1,
            },
            "versionLineage": {
                "currentVersionId": "ver_frozen_existing",
            },
        },
    }


class _FrozenLegacyReplayProvider:
    """Replays the old transport boundary without making a network request."""

    model = "recorded-controller"

    def __init__(self, response_bytes: bytes):
        self.body = json.loads(response_bytes)
        self.calls: list[str] = []
        self.external_call_count = 0

    def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
        self.calls.append("repair" if repair_feedback else "full")
        if repair_feedback:
            # Frozen legacy terminal condition from the real run: the repair
            # request crossed the unchanged 15 KiB Full request boundary.
            raise ValueError("controller_full_payload_too_large")
        return controller_response_content(
            self.body,
            call_kind="full",
            response_bytes=len(_canonical_bytes(self.body)),
        )


def test_frozen_length_truncation_replays_same_legacy_failure_twice_without_side_effects():
    # The HTTP body itself has no line terminator.  Git may check out this
    # one-line fixture with LF or CRLF on Windows, so strip only trailing file
    # terminators before asserting the frozen transport bytes and fingerprint.
    response_bytes = FROZEN_RESPONSE.read_text(encoding="utf-8").rstrip("\r\n").encode("utf-8")
    assert len(response_bytes) == FROZEN_RESPONSE_BYTES
    assert sha256(response_bytes).hexdigest() == FROZEN_RESPONSE_SHA256
    fingerprints: list[str] = []

    for _ in range(2):
        fingerprint, evidence = _failure_fingerprint(response_bytes)
        provider = _FrozenLegacyReplayProvider(response_bytes)
        result = AgentAutonomyController(
            provider=provider,
            decision_timeout_seconds=0.5,
            lite_timeout_seconds=0.2,
            total_budget_seconds=1.0,
        ).decide(
            "two day campus trip",
            {},
            _authoritative_context(),
            available_tools={"resolve_poi", "patch_itinerary"},
            runtime_budget_tools={"resolve_poi", "patch_itinerary"},
        )

        assert evidence["finishReason"] == "length"
        assert evidence["parseErrorCategory"] != "syntactically_valid_but_finish_reason_length"
        assert result.controller_error == "ValueError:controller_full_payload_too_large"
        assert result.controller_failures[-1].failure_class == "request_budget_exceeded"
        assert result.schema_repair_attempts == 1
        assert provider.calls == ["full", "repair"]
        assert provider.external_call_count == 0
        assert result.source == "safe_fallback"
        fingerprints.append(fingerprint)

    assert fingerprints == [fingerprints[0], fingerprints[0]]
    assert fingerprints[0] == "b0e805b6dc8e8cabaa5435384d0dea19e2c2c4db24804ff3215926ac875d82d0"

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
import pytest

from src.cli import agent_cli
from src.core.config import Settings
from src.services.controller_availability_check_service import (
    CLAIM_FILENAME,
    SANITIZER_FILENAME,
    TELEMETRY_FILENAME,
    ControllerAvailabilityCheckService,
)
from src.services.controller_response_integrity import (
    ControllerOutputIncompleteError,
    ControllerOutputTruncatedError,
    ControllerResponseIntegrityEvidence,
)
from src.services.agent_decision_normalizer import (
    AgentDecisionNormalizer,
    DecisionNormalizationError,
)
from src.services.deepseek_agent_provider import DeepSeekAgentProvider


VALID_RESPONSE = json.dumps(
    {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "finish",
        "actionDirective": {
            "type": "finish",
            "assistantReply": "Controller availability check complete.",
        },
    },
    ensure_ascii=False,
)

TELEMETRY_SCHEMA_V2 = "trip-controller-availability-check-v2"
SCHEMA_FAILURE_FIELDS = {
    "schemaFailureStage",
    "schemaFailureCodes",
    "schemaFailurePaths",
    "schemaFailureCount",
}


def _settings(
    *,
    base_url: str = "https://api.deepseek.com",
    api_key: str = "sk-test-controller-availability-123456",
) -> Settings:
    return Settings(
        provider_mode="default",
        deepseek_api_key=api_key,
        deepseek_base_url=base_url,
        deepseek_model="deepseek-v4-flash",
        agent_initial_planning_mode="simple_open_v1",
    )


class FakeControllerProvider:
    def __init__(
        self,
        *,
        response: str = VALID_RESPONSE,
        error: Exception | None = None,
        base_url: str = "https://api.deepseek.com",
        finish_reason: str = "stop",
        response_integrity: str = "complete",
        http_status: int = 200,
        before_full: Callable[[], None] | None = None,
    ) -> None:
        self.api_key = "sk-test-controller-availability-123456"
        self.model = "deepseek-v4-flash"
        self.base_url = base_url
        self.response = response
        self.error = error
        self.finish_reason = finish_reason
        self.response_integrity = response_integrity
        self.http_status = http_status
        self.before_full = before_full
        self.full_calls = 0
        self.repair_calls = 0
        self.lite_calls = 0
        self.retry_calls = 0
        self.performance: dict[str, Any] | None = None

    def prepare_controller_performance(
        self,
        context: dict[str, Any],
        sink: dict[str, Any],
        *,
        call_kind: str,
    ) -> None:
        assert call_kind == "full"
        assert context["schemaVersion"] == "controller-context-full-v1"
        assert context["allowedActions"] == ["finish"]
        assert context["itineraryLifecycle"]["meaningfulSegmentCount"] == 0
        self.performance = sink

    def decide_autonomy(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
        repair_feedback: str = "",
    ) -> str:
        assert context["allowedActions"] == ["finish"]
        assert timeout_seconds == 10.0
        assert repair_feedback == ""
        if self.before_full is not None:
            self.before_full()
        self.full_calls += 1
        if repair_feedback:
            self.repair_calls += 1
        assert self.performance is not None
        self.performance.update(
            {
                "callKind": "full",
                "httpStatus": self.http_status,
                "payloadBytes": 321,
                "responseBytes": len(self.response.encode("utf-8")),
                "contentBytes": len(self.response.encode("utf-8")),
                "finishReason": self.finish_reason,
                "responseIntegrity": self.response_integrity,
                "responseSchemaVersion": "agent-decision-v3",
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def _run(
    tmp_path: Path,
    *,
    provider: FakeControllerProvider,
    check_id: str = "deepseek-controller-availability-test-01",
    settings: Settings | None = None,
) -> dict[str, Any]:
    return ControllerAvailabilityCheckService(
        settings=settings or _settings(),
        provider=provider,
    ).run(check_id=check_id, state_dir=tmp_path)


def test_cli_dispatches_availability_check_before_opening_business_database(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    expected = {
        "schemaVersion": "trip-controller-availability-check-v1",
        "status": "AVAILABLE",
        "checkId": "deepseek-controller-availability-cli-test",
    }

    class FakeService:
        def run(self, *, check_id: str, state_dir: Path) -> dict[str, Any]:
            assert check_id == expected["checkId"]
            assert state_dir == tmp_path
            return expected

    def fail_open_db():
        raise AssertionError("controller availability CLI must not open the business database")

    monkeypatch.setattr(agent_cli, "ControllerAvailabilityCheckService", FakeService)
    monkeypatch.setattr(agent_cli, "_open_db", fail_open_db)

    exit_code = agent_cli.main(
        [
            "controller-availability-check",
            "--check-id",
            expected["checkId"],
            "--state-dir",
            str(tmp_path),
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_available_requires_exactly_one_full_and_zero_other_calls_or_business_writes(tmp_path: Path) -> None:
    provider = FakeControllerProvider()

    result = _run(tmp_path, provider=provider)

    assert result["status"] == "AVAILABLE"
    assert result["httpStatus"] == 200
    assert result["callKind"] == "full"
    assert result["requestBytes"] == 321
    assert result["responseBytes"] > 0
    assert result["contentBytes"] > 0
    assert result["finishReason"] == "stop"
    assert result["responseIntegrity"] == "complete"
    assert result["controllerSchemaVersion"] == "agent-decision-v3"
    assert result["schemaValidation"] == "passed"
    assert result["failureClass"] is None
    assert result["fullCount"] == provider.full_calls == 1
    assert result["repairCount"] == provider.repair_calls == 0
    assert result["liteCount"] == provider.lite_calls == 0
    assert result["retryCount"] == provider.retry_calls == 0
    assert result["otherProviderCalls"] == {
        "webSearch": 0,
        "amapPlace": 0,
        "amapRoute": 0,
        "weather": 0,
        "ticket": 0,
    }
    assert result["businessDatabaseWriteCount"] == 0
    assert result["sanitizerPassed"] is True
    assert result["schemaVersion"] == TELEMETRY_SCHEMA_V2
    assert result["schemaFailureStage"] is None
    assert result["schemaFailureCodes"] == []
    assert result["schemaFailurePaths"] == []
    assert result["schemaFailureCount"] == 0
    assert list(tmp_path.rglob("*.sqlite")) == []
    assert list(tmp_path.rglob("*.sqlite3")) == []

    run_dir = tmp_path / result["checkId"]
    assert (run_dir / CLAIM_FILENAME).is_file()
    assert json.loads((run_dir / TELEMETRY_FILENAME).read_text(encoding="utf-8")) == result
    sanitizer = json.loads((run_dir / SANITIZER_FILENAME).read_text(encoding="utf-8"))
    assert sanitizer == {
        "schemaVersion": "trip-controller-availability-sanitizer-v1",
        "checkId": result["checkId"],
        "passed": True,
        "checkedFiles": [CLAIM_FILENAME, TELEMETRY_FILENAME],
        "prohibitedFieldCount": 0,
        "credentialMatchCount": 0,
    }


def test_one_shot_claim_is_durable_before_the_full_transport_starts(tmp_path: Path) -> None:
    check_id = "deepseek-controller-availability-claim-before-transport"

    def assert_claim_is_already_durable() -> None:
        claim_path = tmp_path / check_id / CLAIM_FILENAME
        assert claim_path.is_file()
        assert json.loads(claim_path.read_text(encoding="utf-8")) == {
            "schemaVersion": "trip-controller-availability-one-shot-v1",
            "checkId": check_id,
            "claimedAt": json.loads(claim_path.read_text(encoding="utf-8"))["claimedAt"],
            "state": "claimed_before_transport",
        }

    provider = FakeControllerProvider(before_full=assert_claim_is_already_durable)

    result = _run(tmp_path, provider=provider, check_id=check_id)

    assert result["status"] == "AVAILABLE"
    assert provider.full_calls == 1


def test_service_uses_the_production_controller_wrapper_once_with_a_fake_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = DeepSeekAgentProvider(
        api_key="sk-test-controller-availability-123456",
        model="deepseek-v4-flash",
        timeout_seconds=30.0,
    )
    provider.base_url = "https://api.deepseek.com"
    transport_payloads: list[dict[str, Any]] = []

    def fake_post_json(payload: dict[str, Any], *, timeout_seconds: float | None = None) -> dict[str, Any]:
        transport_payloads.append(payload)
        assert timeout_seconds == 10.0
        performance = provider._controller_performance_sink()
        assert performance is not None
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": VALID_RESPONSE},
                }
            ]
        }
        performance.update(
            {
                "httpStatus": 200,
                "payloadBytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                "responseBytes": len(json.dumps(body, ensure_ascii=False).encode("utf-8")),
                "contentBytes": len(VALID_RESPONSE.encode("utf-8")),
                "finishReason": "stop",
            }
        )
        return body

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = _run(tmp_path, provider=provider)

    assert result["status"] == "AVAILABLE"
    assert len(transport_payloads) == 1
    payload = transport_payloads[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    system_prompt = payload["messages"][0]["content"]
    assert "actionDirective.type is the mandatory discriminator" in system_prompt
    assert "set it exactly equal to primaryAction" in system_prompt
    assert "Never omit actionDirective.type" in system_prompt
    assert result["fullCount"] == 1
    assert result["repairCount"] == result["liteCount"] == result["retryCount"] == 0


def test_duplicate_check_id_is_rejected_before_any_second_transport(tmp_path: Path) -> None:
    check_id = "deepseek-controller-availability-duplicate-test"
    first_provider = FakeControllerProvider()
    first = _run(tmp_path, provider=first_provider, check_id=check_id)
    persisted_before = (tmp_path / check_id / TELEMETRY_FILENAME).read_bytes()
    second_provider = FakeControllerProvider()

    second = _run(tmp_path, provider=second_provider, check_id=check_id)

    assert first["status"] == "AVAILABLE"
    assert second["status"] == "DUPLICATE_REJECTED"
    assert second["failureClass"] == "check_id_already_claimed"
    assert second["fullCount"] == 0
    assert second_provider.full_calls == 0
    assert (tmp_path / check_id / TELEMETRY_FILENAME).read_bytes() == persisted_before


def test_transport_failure_is_classified_without_persisting_raw_error(tmp_path: Path) -> None:
    raw_secret = "sk-raw-transport-secret-123456"
    raw_error = HTTPException(
        status_code=502,
        detail=f"network failed Authorization: Bearer {raw_secret} https://api.deepseek.com/chat?key={raw_secret}",
    )
    provider = FakeControllerProvider(error=raw_error, http_status=502)

    result = _run(tmp_path, provider=provider)
    persisted = (tmp_path / result["checkId"] / TELEMETRY_FILENAME).read_text(encoding="utf-8")

    assert result["status"] == "UNAVAILABLE"
    assert result["failureClass"] == "provider_unavailable"
    assert result["schemaValidation"] == "not_run"
    assert result["controllerSchemaVersion"] is None
    assert result["fullCount"] == 1
    assert result["repairCount"] == result["liteCount"] == result["retryCount"] == 0
    assert raw_secret not in persisted
    assert "Authorization" not in persisted
    assert "chat?key=" not in persisted
    assert "network failed" not in persisted
    assert result["sanitizerPassed"] is True


def test_http_200_truncated_body_is_available_but_unusable(tmp_path: Path) -> None:
    response = '{"schemaVersion":"agent-decision-v3"'
    evidence = ControllerResponseIntegrityEvidence(
        finish_reason="length",
        content_length=len(response),
        content_bytes=len(response.encode("utf-8")),
        response_bytes=len(response.encode("utf-8")),
        parse_error_category="Expecting ',' delimiter",
        parse_error_position=len(response),
    )
    provider = FakeControllerProvider(
        response=response,
        error=ControllerOutputTruncatedError(call_kind="full", evidence=evidence),
        finish_reason="length",
        response_integrity="truncated",
    )

    result = _run(tmp_path, provider=provider)

    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["httpStatus"] == 200
    assert result["finishReason"] == "length"
    assert result["responseIntegrity"] == "truncated"
    assert result["schemaValidation"] == "not_run"
    assert result["failureClass"] == "output_truncated"
    assert result["repairCount"] == result["liteCount"] == result["retryCount"] == 0


def test_http_200_empty_content_is_available_but_unusable(tmp_path: Path) -> None:
    evidence = ControllerResponseIntegrityEvidence(
        finish_reason="stop",
        content_length=0,
        content_bytes=0,
        response_bytes=42,
        parse_error_category="empty_content",
        parse_error_position=None,
    )
    provider = FakeControllerProvider(
        response="",
        error=ControllerOutputIncompleteError(call_kind="full", evidence=evidence),
        finish_reason="stop",
        response_integrity="incomplete",
    )

    result = _run(tmp_path, provider=provider)

    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["httpStatus"] == 200
    assert result["contentBytes"] == 0
    assert result["responseIntegrity"] == "incomplete"
    assert result["failureClass"] == "output_incomplete"


def test_http_200_schema_invalid_body_fails_without_repair_or_lite(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "finish",
            "actionDirective": {"type": "finish", "unknown": "forbidden"},
        }
    )
    provider = FakeControllerProvider(response=response)

    result = _run(tmp_path, provider=provider)

    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["httpStatus"] == 200
    assert result["responseIntegrity"] == "complete"
    assert result["controllerSchemaVersion"] == "agent-decision-v3"
    assert result["schemaValidation"] == "failed"
    assert result["failureClass"] == "schema_validation_failed"
    assert provider.full_calls == 1
    assert result["repairCount"] == result["liteCount"] == result["retryCount"] == 0


def test_unrecognized_response_schema_is_reported_without_persisting_raw_value(tmp_path: Path) -> None:
    secret = "secret-schema-version-never-persist"
    response = json.dumps(
        {
            "schemaVersion": secret,
            "primaryAction": "finish",
            "actionDirective": {"type": "finish"},
        }
    )
    provider = FakeControllerProvider(response=response)

    result = _run(tmp_path, provider=provider, settings=_settings(api_key=secret))
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((tmp_path / result["checkId"]).glob("*.json"))
    )

    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["controllerSchemaVersion"] == "unrecognized"
    assert result["schemaValidation"] == "failed"
    assert secret not in artifact_text


def test_telemetry_discards_full_prompt_response_and_known_credential(tmp_path: Path) -> None:
    secret = "sk-controller-availability-never-persist-123456"
    response = json.dumps(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "finish",
            "actionDirective": {
                "type": "finish",
                "assistantReply": f"full raw response carries {secret}",
            },
        }
    )
    provider = FakeControllerProvider(response=response)

    result = _run(tmp_path, provider=provider, settings=_settings(api_key=secret))
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((tmp_path / result["checkId"]).glob("*.json"))
    )

    assert result["status"] == "AVAILABLE"
    assert secret not in artifact_text
    assert "full raw response" not in artifact_text
    assert "latestUserMessage" not in artifact_text
    assert "actionDirective" not in artifact_text
    assert "prompt" not in artifact_text.lower()
    assert "authorization" not in artifact_text.lower()
    assert "responsePayload" not in artifact_text
    assert "requestPayload" not in artifact_text
    assert result["sanitizerPassed"] is True


def test_unofficial_endpoint_fails_closed_before_transport_and_consumes_check_id(tmp_path: Path) -> None:
    check_id = "deepseek-controller-availability-unofficial-endpoint"
    provider = FakeControllerProvider(base_url="https://api.deepseek.com.evil.invalid")
    settings = _settings(base_url="https://api.deepseek.com.evil.invalid")

    result = _run(tmp_path, provider=provider, settings=settings, check_id=check_id)
    duplicate_provider = FakeControllerProvider()
    duplicate = _run(tmp_path, provider=duplicate_provider, check_id=check_id)

    assert result["status"] == "NOT_RUN"
    assert result["failureClass"] == "endpoint_not_allowed"
    assert result["fullCount"] == 0
    assert provider.full_calls == 0
    assert result["businessDatabaseWriteCount"] == 0
    assert result["sanitizerPassed"] is True
    assert duplicate["status"] == "DUPLICATE_REJECTED"
    assert duplicate_provider.full_calls == 0


def test_official_host_with_unapproved_path_fails_closed_before_transport(tmp_path: Path) -> None:
    provider = FakeControllerProvider(base_url="https://api.deepseek.com/custom-path")
    settings = _settings(base_url="https://api.deepseek.com/custom-path")

    result = _run(tmp_path, provider=provider, settings=settings)

    assert result["status"] == "NOT_RUN"
    assert result["failureClass"] == "endpoint_not_allowed"
    assert result["fullCount"] == 0
    assert provider.full_calls == 0


def test_production_beta_base_path_remains_explicitly_allowed(tmp_path: Path) -> None:
    provider = FakeControllerProvider(base_url="https://api.deepseek.com/beta")
    settings = _settings(base_url="https://api.deepseek.com/beta")

    result = _run(tmp_path, provider=provider, settings=settings)

    assert result["status"] == "AVAILABLE"
    assert result["fullCount"] == provider.full_calls == 1


def test_invalid_check_id_is_rejected_without_echoing_untrusted_input(tmp_path: Path) -> None:
    raw_check_id = "../Authorization-Bearer-sk-invalid-secret-123456"
    provider = FakeControllerProvider()
    state_dir = tmp_path / "availability"

    result = ControllerAvailabilityCheckService(
        settings=_settings(),
        provider=provider,
    ).run(check_id=raw_check_id, state_dir=state_dir)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "NOT_RUN"
    assert result["failureClass"] == "check_id_invalid"
    assert result["checkId"].startswith("invalid-")
    assert raw_check_id not in serialized
    assert "Authorization" not in serialized
    assert "sk-invalid-secret" not in serialized
    assert provider.full_calls == 0
    assert not state_dir.exists()


def test_check_id_matching_a_known_credential_is_rejected_before_claim_or_transport(tmp_path: Path) -> None:
    secret = "secret-controller-check-id"
    provider = FakeControllerProvider()
    state_dir = tmp_path / "availability"

    result = ControllerAvailabilityCheckService(
        settings=_settings(api_key=secret),
        provider=provider,
    ).run(check_id=secret, state_dir=state_dir)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "NOT_RUN"
    assert result["failureClass"] == "check_id_sensitive"
    assert result["checkId"].startswith("invalid-")
    assert secret not in serialized
    assert provider.full_calls == 0
    assert not state_dir.exists()


def test_artifact_persistence_failure_returns_only_safe_unusable_telemetry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider = FakeControllerProvider()
    service = ControllerAvailabilityCheckService(settings=_settings(), provider=provider)
    original_write = service._write_json_atomic

    def fail_after_claim(path: Path, payload: dict[str, Any]) -> None:
        if path.name != CLAIM_FILENAME:
            raise OSError("sensitive-local-path-must-not-escape")
        original_write(path, payload)

    monkeypatch.setattr(service, "_write_json_atomic", fail_after_claim)

    result = service.run(
        check_id="deepseek-controller-availability-artifact-write-failure",
        state_dir=tmp_path,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert provider.full_calls == 1
    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["failureClass"] == "artifact_persistence_failed"
    assert result["sanitizerPassed"] is False
    assert "sensitive-local-path" not in serialized


def test_unsafe_telemetry_is_replaced_before_any_artifact_write(tmp_path: Path) -> None:
    secret = "Authorization: Bearer sk-writer-side-secret-never-persist"
    check_id = "deepseek-controller-availability-writer-sanitizer"
    service = ControllerAvailabilityCheckService(settings=_settings(), provider=FakeControllerProvider())
    run_dir = tmp_path / check_id
    run_dir.mkdir()
    claim = {
        "schemaVersion": "trip-controller-availability-one-shot-v1",
        "checkId": check_id,
        "claimedAt": "2026-08-28T00:00:00+00:00",
        "state": "claimed_before_transport",
    }
    service._write_json_atomic(run_dir / CLAIM_FILENAME, claim)
    telemetry = service._base_telemetry(
        check_id=check_id,
        started_at="2026-08-28T00:00:00+00:00",
    )
    telemetry.update(
        {
            "status": "AVAILABLE",
            "fullCount": 1,
            "unsafeProviderError": secret,
        }
    )

    result = service._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)
    persisted = (run_dir / TELEMETRY_FILENAME).read_text(encoding="utf-8")
    sanitizer = json.loads((run_dir / SANITIZER_FILENAME).read_text(encoding="utf-8"))

    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["failureClass"] == "sanitizer_failed"
    assert result["sanitizerPassed"] is False
    assert result["fullCount"] == 1
    assert "unsafeProviderError" not in result
    assert "unsafeProviderError" not in persisted
    assert secret not in persisted
    assert "Authorization" not in persisted
    assert sanitizer["passed"] is False
    assert sanitizer["prohibitedFieldCount"] >= 1


@pytest.mark.parametrize(
    ("payload", "expected_stage", "expected_code", "expected_path"),
    [
        (
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
            },
            "model_validation",
            "required_field_missing",
            "actionDirective",
        ),
        (
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {},
            },
            "normalization",
            "required_field_missing",
            "actionDirective.type",
        ),
        (
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {"assistantReply": "bounded"},
            },
            "normalization",
            "required_field_missing",
            "actionDirective.type",
        ),
        (
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {
                    "type": "ask_user",
                    "question": "bounded",
                    "choiceIds": [],
                },
            },
            "normalization",
            "action_directive_type_mismatch",
            "actionDirective.type",
        ),
        (
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {"type": "finish"},
                "privateProviderField": "must-not-become-a-path",
            },
            "model_validation",
            "extra_field_forbidden",
            "unknown_contract_path",
        ),
        (
            {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {"type": "finish", "assistantReply": ["not-a-string"]},
            },
            "model_validation",
            "field_type_invalid",
            "actionDirective.assistantReply",
        ),
    ],
)
def test_schema_failure_diagnosis_is_versioned_and_allowlisted(
    tmp_path: Path,
    payload: dict[str, Any],
    expected_stage: str,
    expected_code: str,
    expected_path: str,
) -> None:
    provider = FakeControllerProvider(response=json.dumps(payload, ensure_ascii=False))

    result = _run(tmp_path, provider=provider)

    assert result["schemaVersion"] == TELEMETRY_SCHEMA_V2
    assert result["status"] == "AVAILABLE_BUT_UNUSABLE"
    assert result["schemaValidation"] == "failed"
    assert result["schemaFailureStage"] == expected_stage
    assert result["schemaFailureCodes"] == [expected_code]
    assert result["schemaFailurePaths"] == [expected_path]
    assert result["schemaFailureCount"] == 1
    assert result["fullCount"] == provider.full_calls == 1
    assert result["repairCount"] == provider.repair_calls == 0
    assert result["liteCount"] == provider.lite_calls == 0
    assert result["retryCount"] == provider.retry_calls == 0
    assert all(value == 0 for value in result["otherProviderCalls"].values())
    assert result["businessDatabaseWriteCount"] == 0
    assert list(tmp_path.rglob("*.sqlite")) == []
    assert list(tmp_path.rglob("*.sqlite3")) == []


def test_json_parse_failure_uses_only_the_bounded_diagnosis_enums(tmp_path: Path) -> None:
    provider = FakeControllerProvider(response='{"schemaVersion":"agent-decision-v3"')

    result = _run(tmp_path, provider=provider)

    assert result["schemaVersion"] == TELEMETRY_SCHEMA_V2
    assert result["schemaFailureStage"] == "json_parse"
    assert result["schemaFailureCodes"] == ["invalid_json"]
    assert result["schemaFailurePaths"] == ["unknown_contract_path"]
    assert result["schemaFailureCount"] == 1
    assert result["repairCount"] == result["liteCount"] == result["retryCount"] == 0
    assert all(value == 0 for value in result["otherProviderCalls"].values())
    assert result["businessDatabaseWriteCount"] == 0


def test_unknown_normalizer_failure_and_path_collapse_without_sensitive_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "sk-sensitive-normalizer-detail-123456"

    def fail_normalization(self, payload: Any, *, context: dict[str, Any]):
        raise DecisionNormalizationError(
            f"provider_error_{secret}",
            f"actionDirective.private_{secret}",
            f"Authorization: Bearer {secret}",
        )

    monkeypatch.setattr(AgentDecisionNormalizer, "normalize", fail_normalization)
    provider = FakeControllerProvider()

    result = _run(tmp_path, provider=provider, settings=_settings(api_key=secret))
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((tmp_path / result["checkId"]).glob("*.json"))
    )

    assert result["schemaFailureStage"] == "normalization"
    assert result["schemaFailureCodes"] == ["unknown_schema_failure"]
    assert result["schemaFailurePaths"] == ["unknown_contract_path"]
    assert result["schemaFailureCount"] == 1
    assert result["sanitizerPassed"] is True
    assert secret not in artifact_text
    assert "Authorization" not in artifact_text
    assert "provider_error" not in artifact_text
    assert "private_" not in artifact_text


def test_disallowed_but_schema_valid_action_is_diagnosed_at_primary_action_policy(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "read_itinerary",
            "actionDirective": {"type": "read_itinerary"},
        }
    )
    provider = FakeControllerProvider(response=response)

    result = _run(tmp_path, provider=provider)

    assert result["schemaFailureStage"] == "primary_action_policy"
    assert result["schemaFailureCodes"] == ["primary_action_not_allowed"]
    assert result["schemaFailurePaths"] == ["primaryAction"]
    assert result["schemaFailureCount"] == 1
    assert result["repairCount"] == result["liteCount"] == result["retryCount"] == 0


def test_legacy_v1_telemetry_remains_readable_and_cannot_carry_v2_fields(tmp_path: Path) -> None:
    legacy = {
        "schemaVersion": "trip-controller-availability-check-v1",
        "status": "AVAILABLE_BUT_UNUSABLE",
        "checkId": "deepseek-controller-availability-legacy-v1",
        "startedAt": "2026-08-27T17:28:28+00:00",
        "finishedAt": "2026-08-27T17:28:29+00:00",
        "httpStatus": 200,
        "callKind": "full",
        "durationMs": 938,
        "requestBytes": 6363,
        "responseBytes": 555,
        "contentBytes": 83,
        "finishReason": "stop",
        "responseIntegrity": "complete",
        "controllerSchemaVersion": "agent-decision-v3",
        "schemaValidation": "failed",
        "failureClass": "schema_validation_failed",
        "fullCount": 1,
        "repairCount": 0,
        "liteCount": 0,
        "retryCount": 0,
        "otherProviderCalls": {
            "webSearch": 0,
            "amapPlace": 0,
            "amapRoute": 0,
            "weather": 0,
            "ticket": 0,
        },
        "businessDatabaseWriteCount": 0,
        "sanitizerPassed": True,
    }
    artifact = tmp_path / "legacy-v1.json"
    artifact.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = ControllerAvailabilityCheckService.load_telemetry_artifact(artifact)

    assert loaded == legacy
    assert SCHEMA_FAILURE_FIELDS.isdisjoint(loaded)

    mixed = {**legacy, "schemaFailureStage": "model_validation"}
    artifact.write_text(json.dumps(mixed), encoding="utf-8")
    with pytest.raises(ValueError, match="telemetry_artifact_invalid"):
        ControllerAvailabilityCheckService.load_telemetry_artifact(artifact)


@pytest.mark.parametrize(
    "unsafe_codes",
    [
        ["not_allowlisted"],
        [{"rawError": "Authorization: Bearer sk-never-surface"}],
    ],
)
def test_v2_reader_rejects_unknown_or_unhashable_diagnosis_values_fail_closed(
    tmp_path: Path,
    unsafe_codes: list[Any],
) -> None:
    provider = FakeControllerProvider()
    result = _run(tmp_path, provider=provider)
    artifact = tmp_path / result["checkId"] / TELEMETRY_FILENAME
    unsafe = {
        **result,
        "status": "AVAILABLE_BUT_UNUSABLE",
        "schemaValidation": "failed",
        "failureClass": "schema_validation_failed",
        "schemaFailureStage": "model_validation",
        "schemaFailureCodes": unsafe_codes,
        "schemaFailurePaths": ["unknown_contract_path"],
        "schemaFailureCount": 1,
    }
    artifact.write_text(json.dumps(unsafe), encoding="utf-8")

    with pytest.raises(ValueError, match="telemetry_artifact_invalid"):
        ControllerAvailabilityCheckService.load_telemetry_artifact(artifact)

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.core.config import Settings, get_settings
from src.runtime.run_artifacts import redact_artifact
from src.services.agent_autonomy_service import ModelDecisionV3, _parse_controller_json_object
from src.services.agent_decision_contract_service import AgentDecisionContractService
from src.services.agent_decision_normalizer import AgentDecisionNormalizer, DecisionNormalizationError
from src.services.controller_context_projection_service import ControllerContextProjectionService
from src.services.controller_failure_classifier import classify_controller_failure
from src.services.deepseek_agent_provider import DeepSeekAgentProvider


TELEMETRY_SCHEMA_VERSION_V1 = "trip-controller-availability-check-v1"
TELEMETRY_SCHEMA_VERSION_V2 = "trip-controller-availability-check-v2"
TELEMETRY_SCHEMA_VERSION = TELEMETRY_SCHEMA_VERSION_V2
CLAIM_SCHEMA_VERSION = "trip-controller-availability-one-shot-v1"
SANITIZER_SCHEMA_VERSION = "trip-controller-availability-sanitizer-v1"
OFFICIAL_DEEPSEEK_HOST = "api.deepseek.com"
OFFICIAL_DEEPSEEK_BASE_PATHS = {"", "/", "/beta", "/beta/"}
CLAIM_FILENAME = "one-shot-claim.json"
TELEMETRY_FILENAME = "telemetry.json"
SANITIZER_FILENAME = "sanitizer-report.json"

_CHECK_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,94}[a-z0-9])?$")
_TELEMETRY_V1_KEYS = {
    "schemaVersion",
    "status",
    "checkId",
    "startedAt",
    "finishedAt",
    "httpStatus",
    "callKind",
    "durationMs",
    "requestBytes",
    "responseBytes",
    "contentBytes",
    "finishReason",
    "responseIntegrity",
    "controllerSchemaVersion",
    "schemaValidation",
    "failureClass",
    "fullCount",
    "repairCount",
    "liteCount",
    "retryCount",
    "otherProviderCalls",
    "businessDatabaseWriteCount",
    "sanitizerPassed",
}
_SCHEMA_FAILURE_KEYS = {
    "schemaFailureStage",
    "schemaFailureCodes",
    "schemaFailurePaths",
    "schemaFailureCount",
}
_TELEMETRY_V2_KEYS = _TELEMETRY_V1_KEYS | _SCHEMA_FAILURE_KEYS
_TELEMETRY_KEYS_BY_VERSION = {
    TELEMETRY_SCHEMA_VERSION_V1: _TELEMETRY_V1_KEYS,
    TELEMETRY_SCHEMA_VERSION_V2: _TELEMETRY_V2_KEYS,
}
_CLAIM_KEYS = {"schemaVersion", "checkId", "claimedAt", "state"}
_OTHER_PROVIDER_CALLS = {
    "webSearch": 0,
    "amapPlace": 0,
    "amapRoute": 0,
    "weather": 0,
    "ticket": 0,
}
_SCHEMA_FAILURE_STAGES = frozenset(
    {
        "json_parse",
        "normalization",
        "model_validation",
        "primary_action_policy",
    }
)
_SCHEMA_FAILURE_CODES = frozenset(
    {
        "invalid_json",
        "normalization_failed",
        "required_field_missing",
        "field_type_invalid",
        "literal_mismatch",
        "extra_field_forbidden",
        "action_directive_type_mismatch",
        "primary_action_not_allowed",
        "unknown_schema_failure",
    }
)
_SCHEMA_FAILURE_PATHS = frozenset(
    {
        "schemaVersion",
        "primaryAction",
        "actionDirective",
        "actionDirective.type",
        "actionDirective.assistantReply",
        "unknown_contract_path",
    }
)
_NORMALIZATION_FAILURE_CODES = {
    "action_directive_mismatch": "action_directive_type_mismatch",
    "controller_primary_action_not_allowed": "primary_action_not_allowed",
}
_MODEL_VALIDATION_FAILURE_CODES = {
    "missing": "required_field_missing",
    "union_tag_not_found": "required_field_missing",
    "dict_type": "field_type_invalid",
    "model_type": "field_type_invalid",
    "string_type": "field_type_invalid",
    "union_tag_invalid": "literal_mismatch",
    "literal_error": "literal_mismatch",
    "extra_forbidden": "extra_field_forbidden",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ControllerAvailabilityCheckService:
    """Run one production Controller Full call without entering Agent or DB lifecycles."""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        provider: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider

    def run(self, *, check_id: str, state_dir: Path) -> dict[str, Any]:
        check_id = str(check_id or "").strip()
        if _CHECK_ID_RE.fullmatch(check_id) is None:
            return self._duplicate_or_preclaim_failure(
                check_id=self._redacted_invalid_check_id(check_id),
                failure_class="check_id_invalid",
                status="NOT_RUN",
            )
        if any(credential in check_id for credential in self._known_credentials() if len(credential) >= 8):
            return self._duplicate_or_preclaim_failure(
                check_id=self._redacted_invalid_check_id(check_id),
                failure_class="check_id_sensitive",
                status="NOT_RUN",
            )

        state_dir = Path(state_dir)
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self._duplicate_or_preclaim_failure(
                check_id=check_id,
                failure_class="one_shot_evidence_write_failed",
                status="NOT_RUN",
            )
        run_dir = state_dir / check_id
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            return self._duplicate_or_preclaim_failure(
                check_id=check_id,
                failure_class="check_id_already_claimed",
                status="DUPLICATE_REJECTED",
            )

        started_at = _utc_now()
        claim = {
            "schemaVersion": CLAIM_SCHEMA_VERSION,
            "checkId": check_id,
            "claimedAt": started_at,
            "state": "claimed_before_transport",
        }
        try:
            self._write_json_atomic(run_dir / CLAIM_FILENAME, claim)
        except OSError:
            return self._duplicate_or_preclaim_failure(
                check_id=check_id,
                failure_class="one_shot_evidence_write_failed",
                status="NOT_RUN",
                started_at=started_at,
            )

        telemetry = self._base_telemetry(check_id=check_id, started_at=started_at)
        if not self._official_endpoint(self.settings.deepseek_base_url):
            telemetry["failureClass"] = "endpoint_not_allowed"
            return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)
        if not str(self.settings.deepseek_api_key or "").strip():
            telemetry["failureClass"] = "provider_unavailable"
            return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)

        provider = self.provider or DeepSeekAgentProvider()
        if not self._official_endpoint(str(getattr(provider, "base_url", "") or "")):
            telemetry["failureClass"] = "endpoint_not_allowed"
            return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)
        decide_autonomy = getattr(provider, "decide_autonomy", None)
        prepare_performance = getattr(provider, "prepare_controller_performance", None)
        if not callable(decide_autonomy) or not callable(prepare_performance):
            telemetry["failureClass"] = "controller_wrapper_unavailable"
            return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)

        try:
            controller_context = self._controller_context()
        except (TypeError, ValueError, ValidationError):
            telemetry["failureClass"] = "controller_context_invalid"
            return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)

        performance: dict[str, Any] = {
            "callKind": "full",
            "httpStatus": None,
            "payloadBytes": None,
            "responseBytes": None,
            "contentBytes": None,
            "finishReason": None,
            "responseIntegrity": None,
            "responseSchemaVersion": "agent-decision-v3",
        }
        try:
            prepare_performance(controller_context, performance, call_kind="full")
        except Exception:
            telemetry["failureClass"] = "controller_telemetry_unavailable"
            return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)

        telemetry["fullCount"] = 1
        call_started = time.monotonic()
        raw_response: Any = None
        try:
            raw_response = decide_autonomy(
                controller_context,
                timeout_seconds=float(self.settings.agent_controller_decision_timeout_seconds),
                repair_feedback="",
            )
            telemetry["durationMs"] = max(0, int((time.monotonic() - call_started) * 1000))
            self._copy_performance_telemetry(telemetry, performance)
            model_decision = self._validate_response(raw_response)
            telemetry["controllerSchemaVersion"] = model_decision.schema_version
            telemetry["schemaValidation"] = "passed"
        except Exception as error:
            telemetry["durationMs"] = max(0, int((time.monotonic() - call_started) * 1000))
            self._copy_performance_telemetry(telemetry, performance)
            telemetry["controllerSchemaVersion"] = self._response_schema_version(raw_response)
            if isinstance(error, (ValidationError, DecisionNormalizationError, json.JSONDecodeError)):
                telemetry["schemaValidation"] = "failed"
                self._apply_schema_failure_diagnosis(telemetry, error, raw_response=raw_response)
            failure = classify_controller_failure(
                error,
                stage="full",
                provider="deepseek",
                model=str(getattr(provider, "model", "deepseek") or "deepseek"),
                duration_ms=int(telemetry["durationMs"] or 0),
                timeout_seconds=float(self.settings.agent_controller_decision_timeout_seconds),
            )
            telemetry["failureClass"] = failure.failure_class
            if telemetry["httpStatus"] is None:
                telemetry["httpStatus"] = failure.http_status

        if self._is_available(telemetry):
            telemetry["status"] = "AVAILABLE"
            telemetry["failureClass"] = None
        elif telemetry["fullCount"] == 1 and telemetry["httpStatus"] == 200:
            telemetry["status"] = "AVAILABLE_BUT_UNUSABLE"
            telemetry["failureClass"] = telemetry["failureClass"] or self._unusable_failure_class(telemetry)
        else:
            telemetry["status"] = "UNAVAILABLE"
            telemetry["failureClass"] = telemetry["failureClass"] or "provider_unavailable"
        return self._persist_result(run_dir=run_dir, claim=claim, telemetry=telemetry)

    def _controller_context(self) -> dict[str, Any]:
        allowed_actions = ("finish",)
        contract = AgentDecisionContractService(
            decision_model=ModelDecisionV3,
            allowed_actions=allowed_actions,
        ).build()
        source_context = {
            "latestUserMessage": (
                "Controller availability check. Return the allowed finish action only; "
                "do not plan, call tools, or write data."
            ),
            "observation": {
                "cycleIndex": 0,
                "itinerary": {
                    "lifecycleState": "availability_check",
                    "meaningfulSegmentCount": 0,
                },
                "planningAttempt": {"persisted": False},
            },
            "observationFingerprint": "controller-availability-check-v1",
        }
        projection = ControllerContextProjectionService().build(
            source_context,
            allowed_actions=allowed_actions,
            decision_contract=contract,
            normalization_context={},
        )
        return projection.full

    @staticmethod
    def _validate_response(raw_response: Any) -> ModelDecisionV3:
        parsed, _aliases = _parse_controller_json_object(raw_response)
        normalized = AgentDecisionNormalizer().normalize(parsed, context={})
        model_decision = ModelDecisionV3.model_validate(normalized.normalized)
        if model_decision.primary_action != "finish":
            raise DecisionNormalizationError(
                "controller_primary_action_not_allowed",
                "primaryAction",
                model_decision.primary_action,
            )
        return model_decision

    @staticmethod
    def _response_schema_version(raw_response: Any) -> Optional[str]:
        try:
            parsed, _aliases = _parse_controller_json_object(raw_response)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            value = str(parsed.get("schemaVersion") or "").strip()
            if value == "agent-decision-v3":
                return value
            if value:
                return "unrecognized"
        return None

    @classmethod
    def _apply_schema_failure_diagnosis(
        cls,
        telemetry: dict[str, Any],
        error: Exception,
        *,
        raw_response: Any,
    ) -> None:
        stage, codes, paths, failure_count = cls._schema_failure_diagnosis(
            error,
            raw_response=raw_response,
        )
        telemetry.update(
            {
                "schemaFailureStage": stage,
                "schemaFailureCodes": codes,
                "schemaFailurePaths": paths,
                "schemaFailureCount": failure_count,
            }
        )

    @classmethod
    def _schema_failure_diagnosis(
        cls,
        error: Exception,
        *,
        raw_response: Any,
    ) -> tuple[str, list[str], list[str], int]:
        if isinstance(error, json.JSONDecodeError):
            return "json_parse", ["invalid_json"], ["unknown_contract_path"], 1

        if isinstance(error, DecisionNormalizationError):
            stage = (
                "primary_action_policy"
                if error.reason_code == "controller_primary_action_not_allowed"
                else "normalization"
            )
            code = _NORMALIZATION_FAILURE_CODES.get(error.reason_code, "unknown_schema_failure")
            if error.reason_code == "action_directive_mismatch" and cls._directive_type_is_missing(raw_response):
                code = "required_field_missing"
            path = cls._public_contract_path(error.path)
            return stage, [code], [path], 1

        if isinstance(error, ValidationError):
            issues = error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
            codes: list[str] = []
            paths: list[str] = []
            for issue in issues:
                issue_type = str(issue.get("type") or "")
                code = _MODEL_VALIDATION_FAILURE_CODES.get(issue_type, "unknown_schema_failure")
                path = cls._model_validation_path(issue_type, issue.get("loc"))
                if code not in codes:
                    codes.append(code)
                if path not in paths:
                    paths.append(path)
            return (
                "model_validation",
                codes or ["unknown_schema_failure"],
                paths or ["unknown_contract_path"],
                max(1, len(issues)),
            )

        return "model_validation", ["unknown_schema_failure"], ["unknown_contract_path"], 1

    @staticmethod
    def _directive_type_is_missing(raw_response: Any) -> bool:
        try:
            parsed, _aliases = _parse_controller_json_object(raw_response)
        except (TypeError, json.JSONDecodeError):
            return False
        directive = parsed.get("actionDirective") if isinstance(parsed, dict) else None
        return isinstance(directive, dict) and "type" not in directive

    @staticmethod
    def _model_validation_path(issue_type: str, location: Any) -> str:
        parts = tuple(part for part in location or () if isinstance(part, str))
        if parts == ("schemaVersion",):
            return "schemaVersion"
        if parts == ("primaryAction",):
            return "primaryAction"
        if parts == ("actionDirective",):
            if issue_type in {"union_tag_not_found", "union_tag_invalid"}:
                return "actionDirective.type"
            return "actionDirective"
        if parts and parts[0] == "actionDirective":
            if parts[-1] == "type":
                return "actionDirective.type"
            if parts[-1] == "assistantReply":
                return "actionDirective.assistantReply"
        return "unknown_contract_path"

    @staticmethod
    def _public_contract_path(path: Any) -> str:
        candidate = path if isinstance(path, str) else ""
        return candidate if candidate in _SCHEMA_FAILURE_PATHS else "unknown_contract_path"

    @staticmethod
    def _copy_performance_telemetry(telemetry: dict[str, Any], performance: dict[str, Any]) -> None:
        telemetry["httpStatus"] = ControllerAvailabilityCheckService._nonnegative_int(performance.get("httpStatus"))
        telemetry["requestBytes"] = ControllerAvailabilityCheckService._nonnegative_int(performance.get("payloadBytes"))
        telemetry["responseBytes"] = ControllerAvailabilityCheckService._nonnegative_int(
            performance.get("responseBytes")
        )
        telemetry["contentBytes"] = ControllerAvailabilityCheckService._nonnegative_int(performance.get("contentBytes"))
        finish_reason = str(performance.get("finishReason") or "").strip()
        telemetry["finishReason"] = finish_reason[:64] or None
        integrity = str(performance.get("responseIntegrity") or "").strip()
        telemetry["responseIntegrity"] = integrity if integrity in {"complete", "truncated", "incomplete"} else None

    @staticmethod
    def _nonnegative_int(value: Any) -> Optional[int]:
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        return None

    @staticmethod
    def _official_endpoint(value: str) -> bool:
        try:
            parsed = urlsplit(str(value or "").strip())
            port = parsed.port
        except (TypeError, ValueError):
            return False
        return (
            parsed.scheme.lower() == "https"
            and str(parsed.hostname or "").lower() == OFFICIAL_DEEPSEEK_HOST
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and parsed.path in OFFICIAL_DEEPSEEK_BASE_PATHS
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _is_available(telemetry: dict[str, Any]) -> bool:
        return (
            telemetry.get("fullCount") == 1
            and telemetry.get("httpStatus") == 200
            and isinstance(telemetry.get("responseBytes"), int)
            and telemetry["responseBytes"] > 0
            and isinstance(telemetry.get("contentBytes"), int)
            and telemetry["contentBytes"] > 0
            and telemetry.get("finishReason") == "stop"
            and telemetry.get("responseIntegrity") == "complete"
            and telemetry.get("controllerSchemaVersion") == "agent-decision-v3"
            and telemetry.get("schemaValidation") == "passed"
            and not telemetry.get("failureClass")
            and telemetry.get("repairCount") == 0
            and telemetry.get("liteCount") == 0
            and telemetry.get("retryCount") == 0
            and all(value == 0 for value in telemetry.get("otherProviderCalls", {}).values())
            and telemetry.get("businessDatabaseWriteCount") == 0
        )

    @staticmethod
    def _unusable_failure_class(telemetry: dict[str, Any]) -> str:
        if telemetry.get("finishReason") == "length":
            return "output_truncated"
        if telemetry.get("responseIntegrity") != "complete":
            return "output_incomplete"
        if telemetry.get("schemaValidation") != "passed":
            return "schema_validation_failed"
        return "controller_response_unusable"

    @staticmethod
    def _base_telemetry(*, check_id: str, started_at: str) -> dict[str, Any]:
        return {
            "schemaVersion": TELEMETRY_SCHEMA_VERSION,
            "status": "NOT_RUN",
            "checkId": check_id,
            "startedAt": started_at,
            "finishedAt": None,
            "httpStatus": None,
            "callKind": "full",
            "durationMs": 0,
            "requestBytes": None,
            "responseBytes": None,
            "contentBytes": None,
            "finishReason": None,
            "responseIntegrity": None,
            "controllerSchemaVersion": None,
            "schemaValidation": "not_run",
            "failureClass": None,
            "fullCount": 0,
            "repairCount": 0,
            "liteCount": 0,
            "retryCount": 0,
            "otherProviderCalls": dict(_OTHER_PROVIDER_CALLS),
            "businessDatabaseWriteCount": 0,
            "sanitizerPassed": False,
            "schemaFailureStage": None,
            "schemaFailureCodes": [],
            "schemaFailurePaths": [],
            "schemaFailureCount": 0,
        }

    def _duplicate_or_preclaim_failure(
        self,
        *,
        check_id: str,
        failure_class: str,
        status: str,
        started_at: Optional[str] = None,
    ) -> dict[str, Any]:
        telemetry = self._base_telemetry(check_id=check_id, started_at=started_at or _utc_now())
        telemetry.update(
            {
                "status": status,
                "finishedAt": _utc_now(),
                "failureClass": failure_class,
            }
        )
        telemetry["sanitizerPassed"] = self._payloads_are_sanitized({}, telemetry)[0]
        return telemetry

    def _persist_result(
        self,
        *,
        run_dir: Path,
        claim: dict[str, Any],
        telemetry: dict[str, Any],
    ) -> dict[str, Any]:
        telemetry["finishedAt"] = _utc_now()
        sanitized, prohibited_count, credential_count = self._payloads_are_sanitized(claim, telemetry)
        telemetry["sanitizerPassed"] = sanitized
        if not sanitized:
            telemetry = self._sanitizer_failure_telemetry(
                claim=claim,
                attempted_telemetry=telemetry,
            )
        try:
            self._write_json_atomic(run_dir / TELEMETRY_FILENAME, telemetry)

            disk_claim = json.loads((run_dir / CLAIM_FILENAME).read_text(encoding="utf-8"))
            disk_telemetry = json.loads((run_dir / TELEMETRY_FILENAME).read_text(encoding="utf-8"))
            disk_sanitized, disk_prohibited, disk_credentials = self._payloads_are_sanitized(
                disk_claim,
                disk_telemetry,
            )
            prohibited_count = max(prohibited_count, disk_prohibited)
            credential_count = max(credential_count, disk_credentials)
            final_sanitized = sanitized and disk_sanitized
            if telemetry["sanitizerPassed"] != final_sanitized:
                telemetry["sanitizerPassed"] = final_sanitized
                self._write_json_atomic(run_dir / TELEMETRY_FILENAME, telemetry)
            sanitizer_report = {
                "schemaVersion": SANITIZER_SCHEMA_VERSION,
                "checkId": telemetry["checkId"],
                "passed": telemetry["sanitizerPassed"] is True,
                "checkedFiles": [CLAIM_FILENAME, TELEMETRY_FILENAME],
                "prohibitedFieldCount": prohibited_count,
                "credentialMatchCount": credential_count,
            }
            self._write_json_atomic(run_dir / SANITIZER_FILENAME, sanitizer_report)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            telemetry.update(
                {
                    "status": "AVAILABLE_BUT_UNUSABLE" if telemetry.get("fullCount") == 1 else "NOT_RUN",
                    "finishedAt": _utc_now(),
                    "failureClass": "artifact_persistence_failed",
                    "sanitizerPassed": False,
                }
            )
            try:
                self._write_json_atomic(run_dir / TELEMETRY_FILENAME, telemetry)
            except OSError:
                pass
        return telemetry

    def _sanitizer_failure_telemetry(
        self,
        *,
        claim: dict[str, Any],
        attempted_telemetry: dict[str, Any],
    ) -> dict[str, Any]:
        claimed_id = claim.get("checkId")
        candidate_id = claimed_id if isinstance(claimed_id, str) else ""
        if _CHECK_ID_RE.fullmatch(candidate_id) is None or any(
            credential in candidate_id for credential in self._known_credentials() if len(credential) >= 8
        ):
            candidate_id = self._redacted_invalid_check_id(candidate_id)
        full_count = 1 if attempted_telemetry.get("fullCount") == 1 else 0
        safe = self._base_telemetry(check_id=candidate_id, started_at=_utc_now())
        safe.update(
            {
                "status": "AVAILABLE_BUT_UNUSABLE" if full_count == 1 else "NOT_RUN",
                "finishedAt": _utc_now(),
                "failureClass": "sanitizer_failed",
                "fullCount": full_count,
                "sanitizerPassed": False,
            }
        )
        return safe

    @staticmethod
    def _redacted_invalid_check_id(check_id: str) -> str:
        digest = sha256(check_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"invalid-{digest}"

    def _payloads_are_sanitized(
        self,
        claim: dict[str, Any],
        telemetry: dict[str, Any],
    ) -> tuple[bool, int, int]:
        prohibited_count = 0
        if claim and set(claim) != _CLAIM_KEYS:
            prohibited_count += len(set(claim).symmetric_difference(_CLAIM_KEYS))
        expected_keys = _TELEMETRY_KEYS_BY_VERSION.get(str(telemetry.get("schemaVersion") or ""))
        if expected_keys is None:
            prohibited_count += 1
        elif set(telemetry) != expected_keys:
            prohibited_count += len(set(telemetry).symmetric_difference(expected_keys))
        if not self._diagnosis_contract_is_valid(telemetry):
            prohibited_count += 1
        if redact_artifact(claim) != claim or redact_artifact(telemetry) != telemetry:
            prohibited_count += 1

        serialized = json.dumps([claim, telemetry], ensure_ascii=False, sort_keys=True)
        credential_count = sum(1 for value in self._known_credentials() if value and value in serialized)
        return prohibited_count == 0 and credential_count == 0, prohibited_count, credential_count

    @staticmethod
    def _diagnosis_contract_is_valid(telemetry: dict[str, Any]) -> bool:
        version = telemetry.get("schemaVersion")
        if version == TELEMETRY_SCHEMA_VERSION_V1:
            return _SCHEMA_FAILURE_KEYS.isdisjoint(telemetry)
        if version != TELEMETRY_SCHEMA_VERSION_V2:
            return False

        stage = telemetry.get("schemaFailureStage")
        codes = telemetry.get("schemaFailureCodes")
        paths = telemetry.get("schemaFailurePaths")
        count = telemetry.get("schemaFailureCount")
        if stage is not None and (not isinstance(stage, str) or stage not in _SCHEMA_FAILURE_STAGES):
            return False
        if not isinstance(codes, list) or any(
            not isinstance(code, str) or code not in _SCHEMA_FAILURE_CODES for code in codes
        ):
            return False
        if not isinstance(paths, list) or any(
            not isinstance(path, str) or path not in _SCHEMA_FAILURE_PATHS for path in paths
        ):
            return False
        if len(set(codes)) != len(codes) or len(set(paths)) != len(paths):
            return False
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
        if telemetry.get("schemaValidation") == "failed":
            return (
                stage in _SCHEMA_FAILURE_STAGES
                and bool(codes)
                and bool(paths)
                and count >= max(len(codes), len(paths), 1)
            )
        return stage is None and codes == [] and paths == [] and count == 0

    @classmethod
    def load_telemetry_artifact(cls, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError("telemetry_artifact_invalid") from error
        if not isinstance(payload, dict):
            raise ValueError("telemetry_artifact_invalid")
        expected_keys = _TELEMETRY_KEYS_BY_VERSION.get(str(payload.get("schemaVersion") or ""))
        if expected_keys is None or set(payload) != expected_keys or not cls._diagnosis_contract_is_valid(payload):
            raise ValueError("telemetry_artifact_invalid")
        if redact_artifact(payload) != payload:
            raise ValueError("telemetry_artifact_invalid")
        return payload

    def _known_credentials(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.settings.deepseek_api_key,
                self.settings.map_provider_key,
                self.settings.map_js_api_key,
                self.settings.map_provider_security_js_code,
                self.settings.ticket_provider_key,
                self.settings.weather_provider_key,
                self.settings.search_provider_key,
                self.settings.bocha_api_key,
                self.settings.tavily_api_key,
                self.settings.anysearch_api_key,
                self.settings.brave_search_api_key,
                self.settings.google_cse_api_key,
                self.settings.google_cse_cx,
            )
            if isinstance(value, str) and value
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

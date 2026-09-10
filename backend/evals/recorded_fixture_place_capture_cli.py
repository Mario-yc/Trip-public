"""Auditable, explicit-only entrypoint for one frozen Place acquisition.

Readiness is the default and has no credential, transport, executor, or session
side effects.  The execution branch merely validates and delegates to the
already hardened final transport and one-shot executor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from backend.evals import recorded_fixture_place_capture_executor as _executor
from backend.evals.recorded_fixture_capture import (
    CASES_DIR,
    PROJECT_ROOT,
    canonical_sha256,
    validate_capture_preflight_manifest,
    validate_zero_network_capture_session_envelope,
)
from backend.evals.recorded_fixture_place_capture_executor import (
    PlaceCaptureExecutionError,
    execute_zero_network_place_capture,
)
from backend.evals.recorded_fixture_place_capture_transport import (
    AmapWebServicePlaceCaptureTransport,
    RealPlaceTransportError,
    exact_outbound_request_sequence_fingerprint,
    validate_real_place_network_authorization,
)


ACQUISITION_PREPARATION_ROOT = (
    PROJECT_ROOT / ".ai-runs" / "recorded-fixture-place-acquisition-preparation"
)

_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000
_REPARSE_POINT = 0x00000400
_PROVIDER_DIAGNOSTIC_SCHEMA_VERSION = "trip-amap-provider-diagnostic-v1"
_PROVIDER_DIAGNOSTIC_FIELDS = {
    "schemaVersion",
    "provider",
    "status",
    "infocode",
}
_SAFE_AMAP_STATUS = frozenset({"0", "1"})
_SAFE_AMAP_INFOCODE = re.compile(r"[0-9]{5}", flags=re.ASCII)
_ZERO_EFFECT_LEDGER = {
    "network": 0,
    "amap": 0,
    "web": 0,
    "controller": 0,
    "capture": 0,
    "version": 0,
    "patch": 0,
    "routeWrite": 0,
}


class PlaceCaptureCliError(RuntimeError):
    """Stable, non-sensitive CLI failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _StrictArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise PlaceCaptureCliError("cli_arguments_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    """Validate readiness or explicitly delegate exactly one Place capture."""

    validated: dict[str, Any] | None = None
    envelope: dict[str, Any] | None = None
    transport: AmapWebServicePlaceCaptureTransport | None = None
    try:
        args = _parse_args(argv)
        runtime_evidence = _load_json_object(args.runtime_evidence)
        manifest = _load_json_object(args.manifest)
        envelope = _load_json_object(args.envelope)
        place_authorization = _load_json_object(args.place_authorization)
        network_authorization = _load_json_object(args.network_authorization)
        validated = _validate_readiness(
            runtime_evidence=runtime_evidence,
            manifest=manifest,
            envelope=envelope,
            place_authorization=place_authorization,
            network_authorization=network_authorization,
            staging_root=Path(args.staging_root),
        )
        summary = _readiness_summary(validated)
        if not args.execute_place_capture:
            _emit(summary)
            return 0

        credential = _read_map_provider_key()
        try:
            transport = AmapWebServicePlaceCaptureTransport(
                network_authorization=deepcopy(network_authorization)
            )
            result = execute_zero_network_place_capture(
                envelope=deepcopy(envelope),
                manifest=deepcopy(manifest),
                runtime_evidence=deepcopy(runtime_evidence),
                authorization=deepcopy(place_authorization),
                staging_root=validated["stagingRoot"],
                credential=credential,
                transport=transport,
                cases_dir=CASES_DIR,
                max_response_bytes=validated["maxResponseBytes"],
            )
        finally:
            credential = ""

        completed = result.get("status") == "completed"
        execution_summary = {
            **summary,
            "status": (
                "PLACE_CAPTURE_EXECUTION_COMPLETED"
                if completed
                else "PLACE_CAPTURE_EXECUTION_FAILED"
            ),
            "reasonCode": result.get("reasonCode"),
            "consumed": result.get("consumed") is True,
            "promotable": result.get("promotable") is True,
            **_transport_effect_evidence(transport),
        }
        provider_diagnostic = _validated_provider_diagnostic(
            result.get("providerDiagnostic")
        )
        if provider_diagnostic is not None:
            execution_summary["providerDiagnostic"] = provider_diagnostic
        _emit(execution_summary)
        return 0 if completed else 1
    except PlaceCaptureCliError as error:
        _emit_failure(
            error.reason_code,
            consumed=_observed_capture_consumed(validated, envelope),
            effect_evidence=_transport_effect_evidence(transport),
        )
    except PlaceCaptureExecutionError as error:
        _emit_failure(
            error.reason_code,
            consumed=_observed_capture_consumed(validated, envelope),
            effect_evidence=_transport_effect_evidence(transport),
        )
    except RealPlaceTransportError as error:
        _emit_failure(
            error.reason_code,
            consumed=_observed_capture_consumed(validated, envelope),
            effect_evidence=_transport_effect_evidence(transport),
            provider_diagnostic=error.provider_diagnostic,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        json.JSONDecodeError,
    ):
        _emit_failure(
            "capture_inputs_invalid",
            consumed=_observed_capture_consumed(validated, envelope),
            effect_evidence=_transport_effect_evidence(transport),
        )
    except Exception:
        _emit_failure(
            "capture_cli_internal_error",
            consumed=_observed_capture_consumed(validated, envelope),
            effect_evidence=_transport_effect_evidence(transport),
        )
    return 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _StrictArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--runtime-evidence", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--place-authorization", required=True)
    parser.add_argument("--network-authorization", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--execute-place-capture", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def _load_json_object(raw_path: Any) -> dict[str, Any]:
    if not isinstance(raw_path, str) or re.search(r"^[a-z][a-z0-9+.-]*://", raw_path, re.I):
        raise PlaceCaptureCliError("json_input_path_invalid")
    path = Path(raw_path)
    if not path.is_absolute():
        raise PlaceCaptureCliError("json_input_path_invalid")
    try:
        details_before = path.lstat()
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PlaceCaptureCliError("json_input_path_invalid") from error
    if (
        not stat.S_ISREG(details_before.st_mode)
        or path.is_symlink()
        or _is_reparse_point(details_before)
        or os.path.normcase(str(absolute)) != os.path.normcase(str(resolved))
        or details_before.st_size <= 0
        or details_before.st_size > _MAX_JSON_BYTES
    ):
        raise PlaceCaptureCliError("json_input_path_invalid")
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        try:
            details_opened_before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details_opened_before.st_mode)
                or _file_identity(details_opened_before)
                != _file_identity(details_before)
            ):
                raise PlaceCaptureCliError("json_input_path_changed")
            encoded = _read_bounded_descriptor(descriptor)
            details_opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        details_after = path.lstat()
        if (
            len(encoded) > _MAX_JSON_BYTES
            or _file_identity(details_opened_after)
            != _file_identity(details_opened_before)
            or _file_identity(details_after) != _file_identity(details_before)
            or path.is_symlink()
            or _is_reparse_point(details_after)
        ):
            raise PlaceCaptureCliError("json_input_path_changed")
        decoded = encoded.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except PlaceCaptureCliError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise PlaceCaptureCliError("json_input_invalid") from error
    if not isinstance(value, dict):
        raise PlaceCaptureCliError("json_input_object_required")
    _validate_json_shape(value)
    return value


def _validate_readiness(
    *,
    runtime_evidence: dict[str, Any],
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    place_authorization: dict[str, Any],
    network_authorization: dict[str, Any],
    staging_root: Path,
) -> dict[str, Any]:
    validate_capture_preflight_manifest(
        manifest=deepcopy(manifest),
        runtime_evidence=deepcopy(runtime_evidence),
        cases_dir=CASES_DIR,
    )
    validate_zero_network_capture_session_envelope(
        envelope=deepcopy(envelope),
        manifest=deepcopy(manifest),
        runtime_evidence=deepcopy(runtime_evidence),
        cases_dir=CASES_DIR,
    )
    allowlist = envelope.get("exactPlaceRequestAllowlist")
    now_utc = datetime.now(timezone.utc)
    _executor._validate_authorization(
        deepcopy(place_authorization),
        envelope=deepcopy(envelope),
        allowlist=deepcopy(allowlist),
        now_utc=now_utc,
    )
    outbound_requests = _executor._validated_outbound_requests(deepcopy(allowlist))
    outbound_fingerprint = exact_outbound_request_sequence_fingerprint(
        outbound_requests
    )
    root, root_identity = _validated_preparation_staging_root(staging_root)
    root_binding_fingerprint = canonical_sha256(
        {
            "stagingRootIdentity": {
                "device": root_identity[0],
                "file": root_identity[1],
                "createdAtNs": root_identity[2],
            }
        }
    )
    session_directory_name = "place-session-" + canonical_sha256(
        {"authorizationId": place_authorization["authorizationId"]}
    )[:32].lower()
    network_validation = validate_real_place_network_authorization(
        authorization=deepcopy(network_authorization),
        place_authorization=deepcopy(place_authorization),
        source_fingerprint=place_authorization["sourceFingerprint"],
        envelope_fingerprint=place_authorization["envelopeFingerprint"],
        content_fingerprint=place_authorization["contentFingerprint"],
        allowlist_fingerprint=place_authorization[
            "exactPlaceRequestAllowlistFingerprint"
        ],
        outbound_request_sequence_fingerprint=outbound_fingerprint,
        max_calls=len(outbound_requests),
        session_directory_name=session_directory_name,
        staging_root_binding_fingerprint=root_binding_fingerprint,
        expected_paths=sorted({request["path"] for request in outbound_requests}),
        now=now_utc,
    )
    request_count = allowlist.get("requestCount") if isinstance(allowlist, dict) else 0
    if request_count != len(outbound_requests) or request_count <= 0:
        raise PlaceCaptureCliError("exact_place_allowlist_invalid")
    return {
        "sourceFingerprint": place_authorization["sourceFingerprint"],
        "envelopeFingerprint": envelope["envelopeFingerprint"],
        "contentFingerprint": envelope["contentFingerprint"],
        "allowlistFingerprint": allowlist["allowlistFingerprint"],
        "outboundRequestSequenceFingerprint": outbound_fingerprint,
        "requestCount": request_count,
        "allowedPaths": sorted({request["path"] for request in outbound_requests}),
        "captureScope": "place_only",
        "stagingRoot": root,
        "networkAuthorizationFingerprint": network_validation[
            "authorizationFingerprint"
        ],
        "maxResponseBytes": network_authorization["transportProfile"][
            "maxResponseBytes"
        ],
    }


def _readiness_summary(validated: dict[str, Any]) -> dict[str, Any]:
    challenge_material = {
        "schemaVersion": "trip-place-acquisition-cli-challenge-v1",
        "sourceFingerprint": validated["sourceFingerprint"],
        "envelopeFingerprint": validated["envelopeFingerprint"],
        "contentFingerprint": validated["contentFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": validated[
            "allowlistFingerprint"
        ],
        "outboundRequestSequenceFingerprint": validated[
            "outboundRequestSequenceFingerprint"
        ],
        "requestCount": validated["requestCount"],
        "captureScope": "place_only",
    }
    return {
        "status": "READY_FOR_EXPLICIT_PLACE_CAPTURE_EXECUTION",
        **{key: value for key, value in challenge_material.items() if key != "schemaVersion"},
        "allowedPaths": deepcopy(validated["allowedPaths"]),
        "consumed": False,
        "promotable": False,
        "zeroEffectLedger": deepcopy(_ZERO_EFFECT_LEDGER),
        "challengeFingerprint": canonical_sha256(challenge_material),
    }


def _validated_preparation_staging_root(
    value: Path,
) -> tuple[Path, tuple[int, int, int]]:
    if not isinstance(value, Path) or not value.is_absolute():
        raise PlaceCaptureCliError("staging_root_invalid")
    preparation_root = Path(ACQUISITION_PREPARATION_ROOT)
    try:
        preparation_details = preparation_root.lstat()
        root_details = value.lstat()
        preparation_resolved = preparation_root.resolve(strict=True)
        root_resolved = value.resolve(strict=True)
    except OSError as error:
        raise PlaceCaptureCliError("staging_root_invalid") from error
    if (
        not stat.S_ISDIR(preparation_details.st_mode)
        or not stat.S_ISDIR(root_details.st_mode)
        or preparation_root.is_symlink()
        or value.is_symlink()
        or _is_reparse_point(preparation_details)
        or _is_reparse_point(root_details)
        or root_resolved.parent != preparation_resolved
    ):
        raise PlaceCaptureCliError("staging_root_invalid")
    try:
        if next(root_resolved.iterdir(), None) is not None:
            raise PlaceCaptureCliError("staging_root_not_empty")
    except OSError as error:
        raise PlaceCaptureCliError("staging_root_invalid") from error
    details = os.stat(root_resolved, follow_symlinks=False)
    return root_resolved, (details.st_dev, details.st_ino, details.st_ctime_ns)


def _read_map_provider_key() -> str:
    try:
        credential = os.environ["MAP_PROVIDER_KEY"]
    except KeyError as error:
        raise PlaceCaptureCliError("map_provider_key_missing") from error
    if not isinstance(credential, str) or not credential:
        raise PlaceCaptureCliError("map_provider_key_invalid")
    return credential


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> Any:
    raise ValueError("nonfinite_json_number")


def _validate_json_shape(value: Any) -> None:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise PlaceCaptureCliError("json_input_shape_invalid")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _is_reparse_point(details: os.stat_result) -> bool:
    return bool(getattr(details, "st_file_attributes", 0) & _REPARSE_POINT)


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_ctime_ns,
        details.st_mtime_ns,
        details.st_size,
    )


def _read_bounded_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_JSON_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _observed_capture_consumed(
    validated: dict[str, Any] | None,
    envelope: dict[str, Any] | None,
) -> bool:
    if not isinstance(validated, dict) or not isinstance(envelope, dict):
        return False
    staging_root = validated.get("stagingRoot")
    allowlist = envelope.get("exactPlaceRequestAllowlist")
    if not isinstance(staging_root, Path) or not isinstance(allowlist, dict):
        return False
    envelope_fingerprint = envelope.get("envelopeFingerprint")
    allowlist_fingerprint = allowlist.get("allowlistFingerprint")
    if not isinstance(envelope_fingerprint, str) or not isinstance(
        allowlist_fingerprint, str
    ):
        return False
    claim_key = canonical_sha256(
        {
            "envelopeFingerprint": envelope_fingerprint,
            "exactPlaceRequestAllowlistFingerprint": allowlist_fingerprint,
        }
    )
    claim_path = staging_root / f"place-envelope-claim-{claim_key[:32].lower()}.json"
    try:
        details = claim_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(details.st_mode)
        and not claim_path.is_symlink()
        and not _is_reparse_point(details)
    )


def _transport_effect_evidence(
    transport: AmapWebServicePlaceCaptureTransport | None,
) -> dict[str, dict[str, int]]:
    external_calls = 0
    if transport is not None and type(transport) is AmapWebServicePlaceCaptureTransport:
        try:
            observed = transport.real_external_place_calls
        except (AttributeError, OSError, TypeError, ValueError):
            observed = 0
        if isinstance(observed, int) and not isinstance(observed, bool) and observed > 0:
            external_calls = observed
    ledger = {
        **_ZERO_EFFECT_LEDGER,
        "network": external_calls,
        "amap": external_calls,
        "capture": external_calls,
    }
    field = "externalEffectLedger" if external_calls else "zeroEffectLedger"
    return {field: ledger}


def _emit(payload: dict[str, Any]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _emit_failure(
    reason_code: str,
    *,
    consumed: bool,
    effect_evidence: dict[str, dict[str, int]],
    provider_diagnostic: Any = None,
) -> None:
    payload = {
        "status": "PLACE_CAPTURE_CLI_FAILED",
        "reasonCode": reason_code,
        "consumed": bool(consumed),
        "promotable": False,
        **deepcopy(effect_evidence),
    }
    safe_provider_diagnostic = _validated_provider_diagnostic(provider_diagnostic)
    if safe_provider_diagnostic is not None:
        payload["providerDiagnostic"] = safe_provider_diagnostic
    _emit(payload)


def _validated_provider_diagnostic(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != _PROVIDER_DIAGNOSTIC_FIELDS:
        return None
    status = value.get("status")
    infocode = value.get("infocode")
    if (
        value.get("schemaVersion") != _PROVIDER_DIAGNOSTIC_SCHEMA_VERSION
        or value.get("provider") != "amap_web_service"
        or type(status) is not str
        or status not in _SAFE_AMAP_STATUS
        or type(infocode) is not str
        or _SAFE_AMAP_INFOCODE.fullmatch(infocode) is None
    ):
        return None
    return {
        "schemaVersion": _PROVIDER_DIAGNOSTIC_SCHEMA_VERSION,
        "provider": "amap_web_service",
        "status": status,
        "infocode": infocode,
    }


if __name__ == "__main__":
    raise SystemExit(main())

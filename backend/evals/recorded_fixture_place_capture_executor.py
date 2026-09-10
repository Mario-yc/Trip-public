"""One-shot Place acquisition executor for exact approved transports.

HTTP is isolated in the exact AMap adapter. This executor reads no credentials
from process state, durably consumes the two grants before any transport call,
and writes only non-promotable quarantine evidence.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.evals.recorded_fixture_capture import (
    CASES_DIR,
    PROJECT_ROOT,
    canonical_sha256,
    validate_zero_network_capture_session_envelope,
)
from backend.evals.recorded_fixture_place_capture_transport import (
    AmapWebServicePlaceCaptureTransport,
    RealPlaceTransportError,
    exact_outbound_request_sequence_fingerprint,
    validate_real_place_network_authorization,
)


_ENDPOINT_PATHS = {
    "place/text": "/v3/place/text",
    "place/around": "/v3/place/around",
}
_ENDPOINT_PARAMETERS = {
    "place/text": frozenset(
        {
            "keywords",
            "city",
            "citylimit",
            "offset",
            "page",
            "extensions",
            "output",
            "types",
        }
    ),
    "place/around": frozenset(
        {
            "location",
            "radius",
            "keywords",
            "types",
            "city",
            "citylimit",
            "sortrule",
            "offset",
            "page",
            "extensions",
            "output",
        }
    ),
}
_AUTHORIZATION_FIELDS = {
    "authorizationId",
    "scope",
    "sourceFingerprint",
    "envelopeFingerprint",
    "contentFingerprint",
    "exactPlaceRequestAllowlistFingerprint",
    "maxCalls",
    "issuedAt",
    "expiresAt",
}
_TRANSPORT_RESPONSE_FIELDS = {
    "statusCode",
    "contentType",
    "redirected",
    "body",
}
_SECRET_FIELD_NAMES = frozenset(
    {
        "key",
        "apikey",
        "accesskey",
        "credential",
        "secret",
        "password",
        "signature",
        "authorization",
        "cookie",
        "cookies",
        "setcookie",
        "proxy",
        "proxies",
        "proxyauthorization",
        "token",
        "accesstoken",
        "header",
        "headers",
        "xapikey",
    }
)
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
_SUCCESS_BUNDLE_NAME = "place-capture-quarantine.json"
_PARTIAL_BUNDLE_NAME = "failed-partial-quarantine.json"
_STATE_NAME = "session-state.json"
_ENVELOPE_CLAIM_PREFIX = "place-envelope-claim-"
_ENVELOPE_CLAIM_SCHEMA_VERSION = "trip-place-envelope-acquisition-claim-v1"
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000
_PROVIDER_DIAGNOSTIC_SCHEMA_VERSION = "trip-amap-provider-diagnostic-v1"
_PROVIDER_DIAGNOSTIC_FIELDS = {
    "schemaVersion",
    "provider",
    "status",
    "infocode",
}
_SAFE_AMAP_STATUS = frozenset({"0", "1"})
_SAFE_AMAP_INFOCODE = re.compile(r"[0-9]{5}", flags=re.ASCII)


class PlaceCaptureExecutionError(RuntimeError):
    """A safe, typed failure raised before any injected transport call."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class _CapturedResponseError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class _ScriptedTransportFailure(RuntimeError):
    pass


class ScriptedFakePlaceTransport:
    """Closed, deterministic transport used by this zero-network stage.

    Arbitrary callables are deliberately rejected by the executor.  This exact
    type only replays caller-supplied in-memory response objects and therefore
    has no code path capable of opening a socket or following a redirect.
    """

    __slots__ = (
        "_responses",
        "_fail_at",
        "calls",
        "credentials",
        "first_call_state",
    )
    transport_kind = "injected_fake"

    def __init__(
        self,
        *,
        responses: list[dict[str, Any]],
        fail_at: int | None = None,
    ) -> None:
        if not isinstance(responses, list) or not responses:
            raise ValueError("scripted_fake_responses_invalid")
        if fail_at is not None and (
            not isinstance(fail_at, int)
            or isinstance(fail_at, bool)
            or fail_at < 1
            or fail_at > len(responses)
        ):
            raise ValueError("scripted_fake_failure_ordinal_invalid")
        self._responses = tuple(deepcopy(responses))
        self._fail_at = fail_at
        self.calls: list[dict[str, Any]] = []
        self.credentials: list[str] = []
        self.first_call_state: dict[str, Any] | None = None

    def __init_subclass__(cls, **_kwargs: Any) -> None:
        raise TypeError("scripted_fake_transport_is_final")

    @property
    def scripted_response_count(self) -> int:
        return len(self._responses)

    def _observe_persisted_claim(self, state: dict[str, Any]) -> None:
        if self.calls or self.first_call_state is not None:
            raise _ScriptedTransportFailure("scripted_claim_observation_invalid")
        self.first_call_state = deepcopy(state)

    def __call__(self, *, request: dict[str, Any], credential: str) -> dict[str, Any]:
        self.calls.append(deepcopy(request))
        self.credentials.append(credential)
        ordinal = len(self.calls)
        if self._fail_at == ordinal:
            raise _ScriptedTransportFailure("scripted_transport_failure")
        return deepcopy(self._responses[ordinal - 1])


class _DirectoryLease:
    """Keep a staging directory anchored while relative writes are performed."""

    def __init__(
        self,
        path: Path,
        *,
        parent: _DirectoryLease | None = None,
        relative_name: str = "",
        expected_identity: tuple[int, int, int] | None = None,
    ) -> None:
        self.path = path
        self._parent = parent
        self._relative_name = relative_name
        self._expected_identity = expected_identity
        self._fd: int | None = None
        self._handle: int | None = None

    def __enter__(self) -> _DirectoryLease:
        if os.name == "nt":
            self._open_windows_directory()
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if self._parent is not None and self._parent._fd is not None:
                self._fd = os.open(
                    self._relative_name,
                    flags,
                    dir_fd=self._parent._fd,
                )
            else:
                self._fd = os.open(self.path, flags)
            details = os.fstat(self._fd)
            if not stat.S_ISDIR(details.st_mode):
                self.close()
                raise OSError("staging_directory_not_directory")
            self._assert_expected_identity(details)
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                ctypes.c_void_p(self._handle)
            )
            self._handle = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def child_exists(self, name: str) -> bool:
        if os.name == "nt":
            return (self.path / name).exists()
        if self._fd is None:
            raise OSError("staging_directory_lease_closed")
        try:
            os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def mkdir(self, name: str) -> None:
        if os.name == "nt":
            (self.path / name).mkdir(mode=0o700, exist_ok=False)
        else:
            if self._fd is None:
                raise OSError("staging_directory_lease_closed")
            os.mkdir(name, mode=0o700, dir_fd=self._fd)
        self.sync()

    def sync(self) -> None:
        if os.name == "nt":
            if self._handle is None:
                raise OSError("staging_directory_lease_closed")
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel32.FlushFileBuffers(ctypes.c_void_p(self._handle)):
                raise OSError(ctypes.get_last_error(), "directory_sync_failed")
        else:
            if self._fd is None:
                raise OSError("staging_directory_lease_closed")
            os.fsync(self._fd)

    def _open_windows_directory(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(self.path),
            0xC0000000,  # GENERIC_READ | GENERIC_WRITE
            0x00000001 | 0x00000002,  # share read/write, deliberately not delete
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000 | 0x80000000,
            # BACKUP_SEMANTICS | OPEN_REPARSE_POINT | WRITE_THROUGH
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, invalid_handle):
            raise OSError(ctypes.get_last_error(), "staging_directory_lock_failed")
        self._handle = int(handle)
        information = _ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(
            ctypes.c_void_p(self._handle), ctypes.byref(information)
        ):
            error_code = ctypes.get_last_error()
            self.close()
            raise OSError(error_code, "staging_directory_identity_failed")
        file_attribute_directory = 0x00000010
        file_attribute_reparse_point = 0x00000400
        if not information.dwFileAttributes & file_attribute_directory or (
            information.dwFileAttributes & file_attribute_reparse_point
        ):
            self.close()
            raise OSError("staging_directory_indirect")
        required = kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(self._handle), None, 0, 0
        )
        if required <= 0:
            error_code = ctypes.get_last_error()
            self.close()
            raise OSError(error_code, "staging_directory_identity_failed")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(self._handle), buffer, len(buffer), 0
        )
        if written <= 0 or written >= len(buffer):
            error_code = ctypes.get_last_error()
            self.close()
            raise OSError(error_code, "staging_directory_identity_failed")
        actual_path = buffer.value
        if actual_path.startswith("\\\\?\\UNC\\"):
            actual_path = "\\\\" + actual_path[8:]
        elif actual_path.startswith("\\\\?\\"):
            actual_path = actual_path[4:]
        if os.path.normcase(os.path.abspath(actual_path)) != os.path.normcase(
            os.path.abspath(self.path)
        ):
            self.close()
            raise OSError("staging_directory_identity_changed")
        try:
            details = os.stat(self.path, follow_symlinks=False)
        except OSError:
            self.close()
            raise
        self._assert_expected_identity(details)

    def _assert_expected_identity(self, details: os.stat_result) -> None:
        if self._expected_identity is None:
            return
        observed = (details.st_dev, details.st_ino, details.st_ctime_ns)
        if observed != self._expected_identity:
            self.close()
            raise OSError("staging_directory_identity_changed")



def execute_zero_network_place_capture(
    *,
    envelope: dict[str, Any],
    manifest: dict[str, Any],
    runtime_evidence: dict[str, Any],
    authorization: dict[str, Any],
    staging_root: Path,
    credential: str,
    transport: ScriptedFakePlaceTransport | AmapWebServicePlaceCaptureTransport,
    cases_dir: Path = CASES_DIR,
    now: datetime | None = None,
    max_response_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Consume one explicit Place-only grant using an exact approved transport.

    The caller is the authorization trust boundary for this stage.  The grant
    is not cryptographically signed here.  Its envelope bindings and durable
    one-shot consumption are nevertheless enforced before the first call.
    """

    envelope_snapshot = deepcopy(envelope)
    manifest_snapshot = deepcopy(manifest)
    runtime_snapshot = deepcopy(runtime_evidence)
    authorization_snapshot = deepcopy(authorization)

    validate_zero_network_capture_session_envelope(
        envelope=envelope_snapshot,
        manifest=manifest_snapshot,
        runtime_evidence=runtime_snapshot,
        cases_dir=cases_dir,
    )

    now_utc = _aware_utc_now(now)
    allowlist = envelope_snapshot.get("exactPlaceRequestAllowlist")
    authorization_fingerprint = _validate_authorization(
        authorization_snapshot,
        envelope=envelope_snapshot,
        allowlist=allowlist,
        now_utc=now_utc,
    )
    outbound_requests = _validated_outbound_requests(allowlist)
    if len(outbound_requests) != authorization_snapshot["maxCalls"]:
        raise PlaceCaptureExecutionError("authorization_call_count_mismatch")
    if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool):
        raise PlaceCaptureExecutionError("response_size_limit_invalid")
    if max_response_bytes <= 0 or max_response_bytes > 10_000_000:
        raise PlaceCaptureExecutionError("response_size_limit_invalid")
    if not isinstance(credential, str) or not credential or len(credential) > 4096:
        raise PlaceCaptureExecutionError("credential_invalid")
    is_scripted = type(transport) is ScriptedFakePlaceTransport
    is_real_transport = type(transport) is AmapWebServicePlaceCaptureTransport
    if not is_scripted and not is_real_transport:
        raise PlaceCaptureExecutionError("exact_place_transport_required")
    if is_scripted and transport.scripted_response_count != len(outbound_requests):
        raise PlaceCaptureExecutionError("scripted_fake_transport_state_invalid")
    transport_authorization = (
        transport.network_authorization if is_real_transport else None
    )
    persistable_request_material = json.dumps(
        {
            "authorization": authorization_snapshot,
            "networkAuthorization": transport_authorization,
            "outboundRequests": outbound_requests,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if credential in persistable_request_material:
        raise PlaceCaptureExecutionError("credential_binding_collision")

    root, root_identity = _validated_staging_root(staging_root)
    root_binding_fingerprint = canonical_sha256(
        {
            "stagingRootIdentity": {
                "device": root_identity[0],
                "file": root_identity[1],
                "createdAtNs": root_identity[2],
            }
        }
    )
    session_directory_name = (
        "place-session-"
        + canonical_sha256(
            {"authorizationId": authorization_snapshot["authorizationId"]}
        )[:32].lower()
    )
    try:
        outbound_sequence_fingerprint = exact_outbound_request_sequence_fingerprint(
            outbound_requests
        )
    except RealPlaceTransportError as error:
        raise PlaceCaptureExecutionError(error.reason_code) from None
    network_authorization_validation: dict[str, Any] | None = None
    if is_real_transport:
        try:
            network_authorization_validation = (
                validate_real_place_network_authorization(
                    authorization=transport_authorization,
                    place_authorization=authorization_snapshot,
                    source_fingerprint=authorization_snapshot["sourceFingerprint"],
                    envelope_fingerprint=authorization_snapshot[
                        "envelopeFingerprint"
                    ],
                    content_fingerprint=authorization_snapshot["contentFingerprint"],
                    allowlist_fingerprint=authorization_snapshot[
                        "exactPlaceRequestAllowlistFingerprint"
                    ],
                    outbound_request_sequence_fingerprint=(
                        outbound_sequence_fingerprint
                    ),
                    max_calls=len(outbound_requests),
                    session_directory_name=session_directory_name,
                    staging_root_binding_fingerprint=root_binding_fingerprint,
                    expected_paths=sorted(
                        {request["path"] for request in outbound_requests}
                    ),
                    now=now_utc,
                )
            )
        except RealPlaceTransportError as error:
            raise PlaceCaptureExecutionError(error.reason_code) from None
        if (
            transport_authorization["transportProfile"]["maxResponseBytes"]
            != max_response_bytes
        ):
            raise PlaceCaptureExecutionError(
                "network_authorization_response_limit_mismatch"
            )
    try:
        with _DirectoryLease(root, expected_identity=root_identity) as root_lease:
            if root_lease.child_exists(session_directory_name):
                raise PlaceCaptureExecutionError("authorization_already_consumed")
            if is_scripted and (transport.calls or transport.credentials):
                raise PlaceCaptureExecutionError("scripted_fake_transport_state_invalid")
            if is_real_transport and (
                transport.attempted_place_calls
                or transport.network_authorization_fingerprint
            ):
                raise PlaceCaptureExecutionError("real_transport_state_invalid")
            _claim_envelope_acquisition(
                root_lease=root_lease,
                envelope=envelope_snapshot,
                allowlist=allowlist,
                authorization=authorization_snapshot,
                authorization_fingerprint=authorization_fingerprint,
            )
            try:
                root_lease.mkdir(session_directory_name)
            except FileExistsError:
                raise PlaceCaptureExecutionError(
                    "authorization_already_consumed"
                ) from None
            session_directory = root / session_directory_name
            with _DirectoryLease(
                session_directory,
                parent=root_lease,
                relative_name=session_directory_name,
            ) as session_lease:
                return _execute_claimed_session(
                    session_lease=session_lease,
                    session_directory_name=session_directory_name,
                    authorization=authorization_snapshot,
                    authorization_fingerprint=authorization_fingerprint,
                    outbound_requests=outbound_requests,
                    transport=transport,
                    network_authorization_validation=(
                        network_authorization_validation
                    ),
                    staging_root_binding_fingerprint=root_binding_fingerprint,
                    outbound_sequence_fingerprint=outbound_sequence_fingerprint,
                    credential=credential,
                    now_utc=now_utc,
                    max_response_bytes=max_response_bytes,
                )
    except PlaceCaptureExecutionError:
        raise
    except OSError:
        raise PlaceCaptureExecutionError("session_claim_or_sync_failed") from None


def _claim_envelope_acquisition(
    *,
    root_lease: _DirectoryLease,
    envelope: dict[str, Any],
    allowlist: dict[str, Any],
    authorization: dict[str, Any],
    authorization_fingerprint: str,
) -> None:
    identity = {
        "envelopeFingerprint": envelope["envelopeFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": allowlist["allowlistFingerprint"],
    }
    claim_key = canonical_sha256(identity)
    claim = {
        "schemaVersion": _ENVELOPE_CLAIM_SCHEMA_VERSION,
        **identity,
        "claimKey": claim_key,
        "consumed": True,
        "state": "acquisition_consumed",
        "firstAuthorizationId": authorization["authorizationId"],
        "firstAuthorizationFingerprint": authorization_fingerprint,
    }
    claim["claimFingerprint"] = canonical_sha256(claim)
    filename = f"{_ENVELOPE_CLAIM_PREFIX}{claim_key[:32].lower()}.json"
    try:
        _write_json_atomic(root_lease, filename, claim, replace=False)
    except FileExistsError:
        raise PlaceCaptureExecutionError(
            "capture_envelope_already_consumed"
        ) from None


def _transport_base_evidence(
    transport: ScriptedFakePlaceTransport | AmapWebServicePlaceCaptureTransport,
    *,
    network_authorization_validation: dict[str, Any] | None,
    staging_root_binding_fingerprint: str,
    outbound_sequence_fingerprint: str,
) -> dict[str, Any]:
    if type(transport) is ScriptedFakePlaceTransport:
        return {
            "captureKind": "injected_fake_zero_network",
            "acquisitionMode": "injected_fake_zero_network",
            "transportKind": "injected_fake",
            "realExternalCapture": False,
        }
    if (
        type(transport) is not AmapWebServicePlaceCaptureTransport
        or network_authorization_validation is None
    ):
        raise PlaceCaptureExecutionError("exact_place_transport_required")
    return {
        "captureKind": "recorded_fixture_acquisition",
        "acquisitionMode": "amap_web_service_https_authorized",
        "transportKind": "amap_place_https",
        "realExternalCapture": False,
        "networkAuthorizationId": transport.network_authorization_id,
        "networkAuthorizationFingerprint": network_authorization_validation[
            "authorizationFingerprint"
        ],
        "stagingRootBindingFingerprint": staging_root_binding_fingerprint,
        "outboundRequestSequenceFingerprint": outbound_sequence_fingerprint,
        "transportRoute": transport.network_authorization["transportProfile"][
            "proxyMode"
        ],
    }


def _transport_metrics(
    transport: ScriptedFakePlaceTransport | AmapWebServicePlaceCaptureTransport,
) -> dict[str, Any]:
    if type(transport) is ScriptedFakePlaceTransport:
        attempted = len(transport.calls)
        fake_calls = attempted
        stub_calls = 0
        external_calls = 0
    else:
        attempted = transport.attempted_place_calls
        fake_calls = 0
        stub_calls = transport.stub_place_calls
        external_calls = transport.real_external_place_calls
    effect_ledger = {
        "network": external_calls,
        "amap": external_calls,
        "web": 0,
        "controller": 0,
        "capture": external_calls,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    evidence: dict[str, Any] = {
        "realExternalCapture": bool(external_calls),
        "attemptedPlaceCalls": attempted,
        "externalPlaceCalls": external_calls,
        "stubTransportCalls": stub_calls,
        "fakeTransportCalls": fake_calls,
    }
    if type(transport) is ScriptedFakePlaceTransport:
        evidence["attemptedFakeTransportCalls"] = attempted
    if external_calls:
        evidence["externalEffectLedger"] = effect_ledger
    else:
        evidence["zeroEffectLedger"] = effect_ledger
    return evidence


def _execute_claimed_session(
    *,
    session_lease: _DirectoryLease,
    session_directory_name: str,
    authorization: dict[str, Any],
    authorization_fingerprint: str,
    outbound_requests: list[dict[str, Any]],
    transport: ScriptedFakePlaceTransport | AmapWebServicePlaceCaptureTransport,
    network_authorization_validation: dict[str, Any] | None,
    staging_root_binding_fingerprint: str,
    outbound_sequence_fingerprint: str,
    credential: str,
    now_utc: datetime,
    max_response_bytes: int,
) -> dict[str, Any]:
    base_evidence = {
        "authorizationId": authorization["authorizationId"],
        "authorizationFingerprint": authorization_fingerprint,
        "sourceFingerprint": authorization["sourceFingerprint"],
        "envelopeFingerprint": authorization["envelopeFingerprint"],
        "contentFingerprint": authorization["contentFingerprint"],
        "exactPlaceRequestAllowlistFingerprint": authorization[
            "exactPlaceRequestAllowlistFingerprint"
        ],
        "maxCalls": authorization["maxCalls"],
        **_transport_base_evidence(
            transport,
            network_authorization_validation=network_authorization_validation,
            staging_root_binding_fingerprint=staging_root_binding_fingerprint,
            outbound_sequence_fingerprint=outbound_sequence_fingerprint,
        ),
    }
    state = {
        "schemaVersion": "trip-place-capture-session-state-v1",
        "state": "consumed_in_progress",
        "consumed": True,
        "promotable": False,
        **base_evidence,
        "attemptedPlaceCalls": 0,
        "externalPlaceCalls": 0,
        "stubTransportCalls": 0,
        "fakeTransportCalls": 0,
        **(
            {"attemptedFakeTransportCalls": 0}
            if type(transport) is ScriptedFakePlaceTransport
            else {}
        ),
        "completedResponses": 0,
        "zeroEffectLedger": deepcopy(_ZERO_EFFECT_LEDGER),
    }
    try:
        _write_json_atomic(session_lease, _STATE_NAME, state, replace=False)
    except OSError:
        raise PlaceCaptureExecutionError("session_state_persist_failed") from None
    if type(transport) is ScriptedFakePlaceTransport:
        transport._observe_persisted_claim(state)
    else:
        claim_receipt = {
            "state": "consumed_in_progress",
            "networkAuthorizationId": transport.network_authorization_id,
            "networkAuthorizationFingerprint": network_authorization_validation[
                "authorizationFingerprint"
            ],
            "placeAuthorizationId": authorization["authorizationId"],
            "placeAuthorizationFingerprint": authorization_fingerprint,
            "sessionDirectoryName": session_directory_name,
            "stagingRootBindingFingerprint": staging_root_binding_fingerprint,
            "outboundRequestSequenceFingerprint": outbound_sequence_fingerprint,
            "maxCalls": len(outbound_requests),
        }
        try:
            transport._bind_claimed_execution(
                validated_authorization=network_authorization_validation,
                claim_receipt=claim_receipt,
                claim_state_path=session_lease.path / _STATE_NAME,
                expected_requests=outbound_requests,
                now=now_utc,
            )
        except RealPlaceTransportError as error:
            return _persist_partial_failure(
                session_lease=session_lease,
                session_directory_name=session_directory_name,
                base_evidence=base_evidence,
                state=state,
                records=[],
                failed_ordinal=0,
                reason_code=error.reason_code,
                transport=transport,
                provider_diagnostic=error.provider_diagnostic,
            )

    captured_records: list[dict[str, Any]] = []
    for outbound in outbound_requests:
        try:
            raw_response = transport(
                request=deepcopy(outbound),
                credential=credential,
            )
            response = _validated_response(
                raw_response,
                credential=credential,
                max_response_bytes=max_response_bytes,
                endpoint_path=outbound["path"],
            )
        except _CapturedResponseError as error:
            return _persist_partial_failure(
                session_lease=session_lease,
                session_directory_name=session_directory_name,
                base_evidence=base_evidence,
                state=state,
                records=captured_records,
                failed_ordinal=outbound["ordinal"],
                reason_code=error.reason_code,
                transport=transport,
            )
        except RealPlaceTransportError as error:
            return _persist_partial_failure(
                session_lease=session_lease,
                session_directory_name=session_directory_name,
                base_evidence=base_evidence,
                state=state,
                records=captured_records,
                failed_ordinal=outbound["ordinal"],
                reason_code=error.reason_code,
                transport=transport,
                provider_diagnostic=error.provider_diagnostic,
            )
        except Exception:
            return _persist_partial_failure(
                session_lease=session_lease,
                session_directory_name=session_directory_name,
                base_evidence=base_evidence,
                state=state,
                records=captured_records,
                failed_ordinal=outbound["ordinal"],
                reason_code="transport_error",
                transport=transport,
            )

        response_sha256 = _recorded_response_sha256(response)
        captured_records.append(
            {
                "ordinal": outbound["ordinal"],
                "requestFingerprint": outbound["requestFingerprint"],
                "auditFingerprint": outbound["auditFingerprint"],
                "request": {
                    "endpoint": outbound["path"],
                    "params": deepcopy(outbound["params"]),
                },
                "responseSha256": response_sha256,
                "response": response,
            }
        )

    bundle = {
        "schemaVersion": "trip-recorded-amap-v1",
        "recordedAt": _format_utc(now_utc),
        "recordingType": "recorded/non-live",
        "recordedProvider": "AMap Web Service",
        "responseHashAlgorithm": "sha256-canonical-json-v1",
        "sanitization": (
            "Credentials, signatures, headers, URL queries and machine paths "
            "are excluded; this bundle remains non-promotable until offline replay "
            "and canonical identity admission succeed."
        ),
        "promotable": False,
        "quarantine": {
            "kind": "place_only_capture",
            **base_evidence,
            "requestCount": len(outbound_requests),
            "completedResponseCount": len(captured_records),
            "routeCaptureAuthorized": False,
            "routeCalls": 0,
            **_transport_metrics(transport),
        },
        "responses": captured_records,
    }
    bundle["bundleFingerprint"] = canonical_sha256(bundle)
    try:
        _write_json_atomic(
            session_lease,
            _SUCCESS_BUNDLE_NAME,
            bundle,
            replace=False,
        )
    except OSError:
        return _persist_partial_failure(
            session_lease=session_lease,
            session_directory_name=session_directory_name,
            base_evidence=base_evidence,
            state=state,
            records=captured_records,
            failed_ordinal=len(outbound_requests) + 1,
            reason_code="quarantine_publish_failed",
            transport=transport,
        )

    final_metrics = _transport_metrics(transport)
    completed_state = {
        **state,
        "state": "consumed_completed",
        "completedResponses": len(captured_records),
        "quarantineBundleFile": _SUCCESS_BUNDLE_NAME,
        "bundleFingerprint": bundle["bundleFingerprint"],
        **final_metrics,
    }
    try:
        _write_json_atomic(
            session_lease,
            _STATE_NAME,
            completed_state,
            replace=True,
        )
    except OSError:
        raise PlaceCaptureExecutionError("session_state_finalize_failed") from None

    return {
        "status": "completed",
        "reasonCode": None,
        "consumed": True,
        "promotable": False,
        **base_evidence,
        "requestCount": len(outbound_requests),
        "completedResponseCount": len(captured_records),
        "sessionDirectoryName": session_directory_name,
        "quarantineBundleFile": _SUCCESS_BUNDLE_NAME,
        "partialQuarantineFile": None,
        "routeCaptureAuthorized": False,
        "routeCalls": 0,
        **final_metrics,
        "bundleFingerprint": bundle["bundleFingerprint"],
    }


def _validate_authorization(
    authorization: Any,
    *,
    envelope: dict[str, Any],
    allowlist: Any,
    now_utc: datetime,
) -> str:
    if not isinstance(authorization, dict) or set(authorization) != _AUTHORIZATION_FIELDS:
        raise PlaceCaptureExecutionError("authorization_schema_invalid")
    authorization_id = authorization.get("authorizationId")
    if not isinstance(authorization_id, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", authorization_id
    ) is None:
        raise PlaceCaptureExecutionError("authorization_id_invalid")
    if authorization.get("scope") != "place_only":
        raise PlaceCaptureExecutionError("authorization_scope_invalid")
    if not isinstance(allowlist, dict):
        raise PlaceCaptureExecutionError("exact_place_allowlist_invalid")
    expected = {
        "sourceFingerprint": (envelope.get("sourceBindings") or {}).get(
            "sourceFingerprint"
        ),
        "envelopeFingerprint": envelope.get("envelopeFingerprint"),
        "contentFingerprint": envelope.get("contentFingerprint"),
        "exactPlaceRequestAllowlistFingerprint": allowlist.get(
            "allowlistFingerprint"
        ),
    }
    for field, value in expected.items():
        if not isinstance(authorization.get(field), str) or authorization.get(field) != value:
            raise PlaceCaptureExecutionError(f"authorization_{field}_mismatch")
    max_calls = authorization.get("maxCalls")
    request_count = allowlist.get("requestCount")
    if (
        not isinstance(max_calls, int)
        or isinstance(max_calls, bool)
        or max_calls <= 0
        or max_calls != request_count
    ):
        raise PlaceCaptureExecutionError("authorization_call_count_mismatch")
    issued_at = _parse_aware_utc(authorization.get("issuedAt"), "authorization_issued_at_invalid")
    expires_at = _parse_aware_utc(
        authorization.get("expiresAt"), "authorization_expires_at_invalid"
    )
    if issued_at > now_utc or expires_at <= issued_at or now_utc >= expires_at:
        raise PlaceCaptureExecutionError("authorization_expired_or_not_yet_valid")
    return canonical_sha256(authorization)


def _validated_outbound_requests(allowlist: Any) -> list[dict[str, Any]]:
    if not isinstance(allowlist, dict):
        raise PlaceCaptureExecutionError("exact_place_allowlist_invalid")
    requests = allowlist.get("requests")
    request_count = allowlist.get("requestCount")
    if (
        not isinstance(requests, list)
        or not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or request_count != len(requests)
        or request_count <= 0
    ):
        raise PlaceCaptureExecutionError("exact_place_allowlist_invalid")
    outbound: list[dict[str, Any]] = []
    for ordinal, request in enumerate(requests, start=1):
        if not isinstance(request, dict):
            raise PlaceCaptureExecutionError("exact_place_request_invalid")
        endpoint = request.get("endpoint")
        params = request.get("sanitizedParams")
        if endpoint not in _ENDPOINT_PATHS or not isinstance(params, dict):
            raise PlaceCaptureExecutionError("exact_place_request_invalid")
        if set(params) - _ENDPOINT_PARAMETERS[endpoint]:
            raise PlaceCaptureExecutionError("exact_place_request_parameters_invalid")
        if any(_is_secret_field(key) for key in params):
            raise PlaceCaptureExecutionError("exact_place_request_secret_invalid")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in params.items()):
            raise PlaceCaptureExecutionError("exact_place_request_parameters_invalid")
        outbound.append(
            {
                "method": "GET",
                "scheme": "https",
                "host": "restapi.amap.com",
                "path": _ENDPOINT_PATHS[endpoint],
                "params": deepcopy(params),
                "allowRedirects": False,
                "ordinal": ordinal,
                "requestFingerprint": str(request.get("requestFingerprint") or ""),
                "auditFingerprint": str(request.get("auditFingerprint") or ""),
            }
        )
    return outbound


def _validated_staging_root(value: Any) -> tuple[Path, tuple[int, int, int]]:
    try:
        root = Path(value)
    except TypeError as error:
        raise PlaceCaptureExecutionError("staging_root_invalid") from error
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise PlaceCaptureExecutionError("staging_root_invalid")
    try:
        absolute = Path(os.path.abspath(root))
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise PlaceCaptureExecutionError("staging_root_invalid") from error
    if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
        raise PlaceCaptureExecutionError("staging_root_indirect")
    protected = (
        PROJECT_ROOT / "backend" / "evals" / "fixtures",
        PROJECT_ROOT / "backend" / "evals" / "cases",
    )
    if any(_is_relative_to(resolved, item.resolve()) for item in protected):
        raise PlaceCaptureExecutionError("formal_capture_target_forbidden")
    try:
        details = os.stat(resolved, follow_symlinks=False)
    except OSError as error:
        raise PlaceCaptureExecutionError("staging_root_invalid") from error
    return resolved, (details.st_dev, details.st_ino, details.st_ctime_ns)


def _validated_response(
    value: Any,
    *,
    credential: str,
    max_response_bytes: int,
    endpoint_path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _TRANSPORT_RESPONSE_FIELDS:
        raise _CapturedResponseError("transport_response_schema_invalid")
    if value.get("redirected") is not False:
        raise _CapturedResponseError("transport_redirect_forbidden")
    status_code = value.get("statusCode")
    if not isinstance(status_code, int) or isinstance(status_code, bool) or status_code != 200:
        raise _CapturedResponseError("transport_http_status_invalid")
    content_type = value.get("contentType")
    if not isinstance(content_type, str) or content_type.split(";", 1)[0].strip().casefold() != (
        "application/json"
    ):
        raise _CapturedResponseError("transport_content_type_invalid")
    body = value.get("body")
    if isinstance(body, str):
        encoded = body.encode("utf-8")
    elif isinstance(body, bytes):
        encoded = body
    else:
        raise _CapturedResponseError("transport_body_invalid")
    if not encoded or len(encoded) > max_response_bytes:
        raise _CapturedResponseError("transport_body_size_invalid")
    if credential.encode("utf-8") in encoded:
        raise _CapturedResponseError("credential_echo_forbidden")
    try:
        decoded = encoded.decode("utf-8")
        payload = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _CapturedResponseError("transport_json_invalid") from error
    if not isinstance(payload, dict):
        raise _CapturedResponseError("transport_json_object_required")
    _validate_json_shape(payload)
    sanitized_payload = _sanitize_place_response(
        payload,
        endpoint_path=endpoint_path,
    )
    _validate_sanitized_json(sanitized_payload, credential=credential)
    if str(sanitized_payload.get("status") or "") != "1" or str(
        sanitized_payload.get("infocode") or ""
    ) != "10000":
        raise _CapturedResponseError("amap_response_unsuccessful")
    return sanitized_payload


def _sanitize_place_response(
    payload: dict[str, Any],
    *,
    endpoint_path: str,
) -> dict[str, Any]:
    if endpoint_path not in _ENDPOINT_PATHS.values():
        raise _CapturedResponseError("transport_endpoint_forbidden")
    sanitized = deepcopy(payload)
    pois = sanitized.get("pois")
    if pois is None:
        return sanitized
    if not isinstance(pois, list):
        raise _CapturedResponseError("transport_place_pois_invalid")
    for poi in pois:
        if not isinstance(poi, dict):
            raise _CapturedResponseError("transport_place_pois_invalid")
        poi["photos"] = []
    return sanitized


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
            raise _CapturedResponseError("transport_json_shape_invalid")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _validate_sanitized_json(value: Any, *, credential: str) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if _is_secret_field(key):
                    raise _CapturedResponseError("response_secret_field_forbidden")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if credential and credential in item:
                raise _CapturedResponseError("credential_echo_forbidden")
            if re.search(r"https?://", item, flags=re.IGNORECASE):
                raise _CapturedResponseError("response_full_url_forbidden")


def _is_secret_field(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return normalized in _SECRET_FIELD_NAMES or (
        "cookie" in normalized
        or normalized.startswith("proxy")
        or normalized.endswith("token")
        or normalized.endswith("signature")
        or normalized.endswith("credential")
        or normalized.endswith("password")
        or normalized.endswith("header")
        or normalized.endswith("headers")
    )


def _persist_partial_failure(
    *,
    session_lease: _DirectoryLease,
    session_directory_name: str,
    base_evidence: dict[str, Any],
    state: dict[str, Any],
    records: list[dict[str, Any]],
    failed_ordinal: int,
    reason_code: str,
    transport: ScriptedFakePlaceTransport | AmapWebServicePlaceCaptureTransport,
    provider_diagnostic: Any = None,
) -> dict[str, Any]:
    metrics = _transport_metrics(transport)
    safe_provider_diagnostic = _validated_provider_diagnostic(provider_diagnostic)
    partial = {
        "schemaVersion": "trip-place-capture-partial-quarantine-v1",
        "status": "failed_partial",
        "promotable": False,
        **base_evidence,
        "failedOrdinal": failed_ordinal,
        "reasonCode": reason_code,
        "completedResponseCount": len(records),
        "responses": deepcopy(records),
        "routeCaptureAuthorized": False,
        "routeCalls": 0,
        **metrics,
    }
    if safe_provider_diagnostic is not None:
        partial["providerDiagnostic"] = safe_provider_diagnostic
    partial["partialFingerprint"] = canonical_sha256(partial)
    partial_file: str | None = _PARTIAL_BUNDLE_NAME
    try:
        _write_json_atomic(
            session_lease,
            _PARTIAL_BUNDLE_NAME,
            partial,
            replace=False,
        )
    except OSError:
        partial_file = None
        reason_code = "partial_quarantine_write_failed"
    failed_state = {
        **state,
        "state": "consumed_failed",
        "completedResponses": len(records),
        "reasonCode": reason_code,
        "partialQuarantineFile": partial_file,
        "failedOrdinal": failed_ordinal,
        **metrics,
    }
    if safe_provider_diagnostic is not None:
        failed_state["providerDiagnostic"] = safe_provider_diagnostic
    try:
        _write_json_atomic(
            session_lease,
            _STATE_NAME,
            failed_state,
            replace=True,
        )
    except OSError:
        pass
    result = {
        "status": "failed",
        "reasonCode": reason_code,
        "consumed": True,
        "promotable": False,
        **base_evidence,
        "requestCount": base_evidence["maxCalls"],
        "completedResponseCount": len(records),
        "failedOrdinal": failed_ordinal,
        "sessionDirectoryName": session_directory_name,
        "quarantineBundleFile": None,
        "partialQuarantineFile": partial_file,
        "routeCaptureAuthorized": False,
        "routeCalls": 0,
        **metrics,
    }
    if safe_provider_diagnostic is not None:
        result["providerDiagnostic"] = safe_provider_diagnostic
    return result


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


def _write_json_atomic(
    directory: _DirectoryLease,
    filename: str,
    payload: dict[str, Any],
    *,
    replace: bool,
) -> None:
    if Path(filename).name != filename or not filename:
        raise OSError("staging_filename_invalid")
    temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    try:
        if os.name == "nt":
            temporary = directory.path / temporary_name
            destination = directory.path / filename
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(temporary, destination)
            else:
                os.rename(temporary, destination)
        else:
            if directory._fd is None:
                raise OSError("staging_directory_lease_closed")
            file_descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory._fd,
            )
            try:
                handle = os.fdopen(file_descriptor, "wb")
            except Exception:
                os.close(file_descriptor)
                raise
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(
                    temporary_name,
                    filename,
                    src_dir_fd=directory._fd,
                    dst_dir_fd=directory._fd,
                )
            else:
                os.link(
                    temporary_name,
                    filename,
                    src_dir_fd=directory._fd,
                    dst_dir_fd=directory._fd,
                    follow_symlinks=False,
                )
                os.unlink(temporary_name, dir_fd=directory._fd)
        directory.sync()
    finally:
        try:
            if os.name == "nt":
                (directory.path / temporary_name).unlink()
            elif directory._fd is not None:
                os.unlink(temporary_name, dir_fd=directory._fd)
        except FileNotFoundError:
            pass


def _recorded_response_sha256(response: dict[str, Any]) -> str:
    return canonical_sha256(response)


def _aware_utc_now(value: datetime | None) -> datetime:
    current = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise PlaceCaptureExecutionError("current_time_invalid")
    return current.astimezone(timezone.utc)


def _parse_aware_utc(value: Any, reason_code: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PlaceCaptureExecutionError(reason_code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlaceCaptureExecutionError(reason_code) from error
    if parsed.tzinfo is None:
        raise PlaceCaptureExecutionError(reason_code)
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True

"""Production-shaped, bounded AMap Place acquisition transport.

The transport is deliberately separate from the capture executor.  It cannot
be constructed with an arbitrary opener, URL, headers, or credential source.
Tests replace the private opener factory at the module boundary; production
uses urllib with normal TLS verification and an explicit no-redirect policy.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import stat
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from backend.evals.recorded_fixture_capture import canonical_sha256
from src.services.amap_rate_limiter import (
    AMAP_PLACE_CAPTURE_CALL_GATE,
    AmapRateLimitCooldownActive,
)


_NETWORK_AUTHORIZATION_FIELDS = {
    "schemaVersion",
    "networkAuthorizationId",
    "scope",
    "transportKind",
    "placeAuthorizationId",
    "placeAuthorizationFingerprint",
    "sourceFingerprint",
    "envelopeFingerprint",
    "contentFingerprint",
    "exactPlaceRequestAllowlistFingerprint",
    "outboundRequestSequenceFingerprint",
    "maxCalls",
    "sessionDirectoryName",
    "stagingRootBindingFingerprint",
    "transportProfile",
    "issuedAt",
    "expiresAt",
}
_TRANSPORT_PROFILE_FIELDS = {
    "kind",
    "host",
    "paths",
    "proxyMode",
    "timeoutSeconds",
    "userAgentProfile",
    "maxResponseBytes",
}
_REQUEST_FIELDS = {
    "method",
    "scheme",
    "host",
    "path",
    "params",
    "allowRedirects",
    "ordinal",
    "requestFingerprint",
    "auditFingerprint",
}
_PLACE_PATHS = frozenset({"/v3/place/text", "/v3/place/around"})
_PLACE_PARAMETERS = {
    "/v3/place/text": frozenset(
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
    "/v3/place/around": frozenset(
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
_FIXED_USER_AGENT = "trip-recorded-place-capture/1.0"
_HEX_64 = re.compile(r"[0-9a-fA-F]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_AMAP_STATUS = frozenset({"0", "1"})
_SAFE_AMAP_INFOCODE = re.compile(r"[0-9]{5}", flags=re.ASCII)
_PROVIDER_DIAGNOSTIC_FIELDS = {
    "schemaVersion",
    "provider",
    "status",
    "infocode",
}
_PROVIDER_DIAGNOSTIC_SCHEMA_VERSION = "trip-amap-provider-diagnostic-v1"


class RealPlaceTransportError(RuntimeError):
    """Safe transport failure containing a reason and optional whitelisted codes."""

    def __init__(
        self,
        reason_code: str,
        *,
        provider_diagnostic: Any = None,
    ):
        self.reason_code = reason_code
        self.provider_diagnostic = _validated_provider_diagnostic(provider_diagnostic)
        super().__init__(reason_code)


def _amap_provider_diagnostic(status: Any, infocode: Any) -> dict[str, str] | None:
    """Return only fixed-format AMap status codes; omit malformed provider data."""

    if (
        type(status) is not str
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


def _validated_provider_diagnostic(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != _PROVIDER_DIAGNOSTIC_FIELDS:
        return None
    if (
        value.get("schemaVersion") != _PROVIDER_DIAGNOSTIC_SCHEMA_VERSION
        or value.get("provider") != "amap_web_service"
    ):
        return None
    return _amap_provider_diagnostic(value.get("status"), value.get("infocode"))


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: Any,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        raise RealPlaceTransportError("transport_redirect_forbidden")


def _build_hardened_opener(proxy_mode: str) -> Any:
    if proxy_mode == "direct":
        return build_opener(ProxyHandler({}), _NoRedirectHandler())
    if proxy_mode == "system":
        return build_opener(ProxyHandler(), _NoRedirectHandler())
    raise RealPlaceTransportError("transport_proxy_mode_invalid")


_PRODUCTION_OPENER_FACTORY = _build_hardened_opener


def validate_real_place_network_authorization(
    *,
    authorization: dict[str, Any],
    place_authorization: dict[str, Any],
    source_fingerprint: str,
    envelope_fingerprint: str,
    content_fingerprint: str,
    allowlist_fingerprint: str,
    outbound_request_sequence_fingerprint: str,
    max_calls: int,
    session_directory_name: str,
    staging_root_binding_fingerprint: str,
    expected_paths: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a caller-issued Place-only network grant without side effects."""

    snapshot = deepcopy(authorization)
    if not isinstance(snapshot, dict) or set(snapshot) != _NETWORK_AUTHORIZATION_FIELDS:
        raise RealPlaceTransportError("network_authorization_schema_invalid")
    if snapshot.get("schemaVersion") != "trip-amap-place-network-authorization-v1":
        raise RealPlaceTransportError("network_authorization_schema_invalid")
    if not _safe_id(snapshot.get("networkAuthorizationId")):
        raise RealPlaceTransportError("network_authorization_id_invalid")
    if snapshot.get("scope") != "place_only":
        raise RealPlaceTransportError("network_authorization_scope_invalid")
    if snapshot.get("transportKind") != "amap_place_https":
        raise RealPlaceTransportError("network_authorization_transport_invalid")
    if not isinstance(place_authorization, dict):
        raise RealPlaceTransportError("place_authorization_invalid")
    expected_bindings = {
        "placeAuthorizationId": place_authorization.get("authorizationId"),
        "placeAuthorizationFingerprint": canonical_sha256(place_authorization),
        "sourceFingerprint": source_fingerprint,
        "envelopeFingerprint": envelope_fingerprint,
        "contentFingerprint": content_fingerprint,
        "exactPlaceRequestAllowlistFingerprint": allowlist_fingerprint,
        "outboundRequestSequenceFingerprint": outbound_request_sequence_fingerprint,
        "maxCalls": max_calls,
        "sessionDirectoryName": session_directory_name,
        "stagingRootBindingFingerprint": staging_root_binding_fingerprint,
    }
    for field, expected in expected_bindings.items():
        if snapshot.get(field) != expected:
            raise RealPlaceTransportError(f"network_authorization_{field}_mismatch")
    for fingerprint_field in (
        "placeAuthorizationFingerprint",
        "sourceFingerprint",
        "envelopeFingerprint",
        "contentFingerprint",
        "exactPlaceRequestAllowlistFingerprint",
        "outboundRequestSequenceFingerprint",
        "stagingRootBindingFingerprint",
    ):
        if not _hex_64(snapshot.get(fingerprint_field)):
            raise RealPlaceTransportError("network_authorization_fingerprint_invalid")
    if (
        not isinstance(max_calls, int)
        or isinstance(max_calls, bool)
        or max_calls <= 0
        or snapshot.get("maxCalls") != max_calls
    ):
        raise RealPlaceTransportError("network_authorization_call_count_invalid")
    if not _safe_id(snapshot.get("sessionDirectoryName")):
        raise RealPlaceTransportError("network_authorization_session_invalid")

    normalized_paths = sorted(set(expected_paths))
    if not normalized_paths or any(path not in _PLACE_PATHS for path in normalized_paths):
        raise RealPlaceTransportError("network_authorization_paths_invalid")
    profile = snapshot.get("transportProfile")
    if not isinstance(profile, dict) or set(profile) != _TRANSPORT_PROFILE_FIELDS:
        raise RealPlaceTransportError("network_authorization_profile_invalid")
    if (
        profile.get("kind") != "amap_place_https_v1"
        or profile.get("host") != "restapi.amap.com"
        or profile.get("paths") != normalized_paths
        or profile.get("proxyMode") not in {"direct", "system"}
        or profile.get("userAgentProfile") != "trip-place-capture-v1"
    ):
        raise RealPlaceTransportError("network_authorization_profile_invalid")
    timeout_seconds = profile.get("timeoutSeconds")
    maximum_bytes = profile.get("maxResponseBytes")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0.1 <= float(timeout_seconds) <= 10.0
        or not isinstance(maximum_bytes, int)
        or isinstance(maximum_bytes, bool)
        or not 1 <= maximum_bytes <= 10_000_000
    ):
        raise RealPlaceTransportError("network_authorization_profile_invalid")

    current = _aware_utc(now)
    issued_at = _parse_utc(snapshot.get("issuedAt"))
    expires_at = _parse_utc(snapshot.get("expiresAt"))
    if issued_at > current or expires_at <= issued_at or current >= expires_at:
        raise RealPlaceTransportError("network_authorization_expired_or_not_yet_valid")
    return {
        "authorization": snapshot,
        "authorizationFingerprint": canonical_sha256(snapshot),
    }


class AmapWebServicePlaceCaptureTransport:
    """Exact final AMap Place HTTPS adapter with lazy network initialization."""

    __slots__ = (
        "_authorization",
        "_authorization_fingerprint",
        "_expected_requests",
        "_claim_receipt",
        "_claim_state_fingerprint",
        "_claim_state_path",
        "_call_gate",
        "_next_index",
        "_opener",
        "_production_http_active",
        "_attempted_calls",
        "_production_calls",
        "_stub_calls",
    )
    transport_kind = "amap_place_https"
    capture_kind = "recorded_fixture_acquisition"

    def __init__(self, *, network_authorization: dict[str, Any]) -> None:
        if not isinstance(network_authorization, dict):
            raise RealPlaceTransportError("network_authorization_schema_invalid")
        self._authorization = deepcopy(network_authorization)
        self._authorization_fingerprint = ""
        self._expected_requests: tuple[dict[str, Any], ...] = ()
        self._claim_receipt: dict[str, Any] = {}
        self._claim_state_fingerprint = ""
        self._claim_state_path: Path | None = None
        self._call_gate = AMAP_PLACE_CAPTURE_CALL_GATE
        self._next_index = 0
        self._opener: Any = None
        self._production_http_active = False
        self._attempted_calls = 0
        self._production_calls = 0
        self._stub_calls = 0

    def __init_subclass__(cls, **_kwargs: Any) -> None:
        raise TypeError("amap_place_capture_transport_is_final")

    @property
    def network_authorization(self) -> dict[str, Any]:
        return deepcopy(self._authorization)

    @property
    def network_authorization_fingerprint(self) -> str:
        return self._authorization_fingerprint

    @property
    def network_authorization_id(self) -> str:
        return str(self._authorization.get("networkAuthorizationId") or "")

    @property
    def attempted_place_calls(self) -> int:
        return self._attempted_calls

    @property
    def real_external_place_calls(self) -> int:
        return self._production_calls

    @property
    def stub_place_calls(self) -> int:
        return self._stub_calls

    @property
    def production_http_active(self) -> bool:
        return self._production_http_active

    @property
    def production_http_configured(self) -> bool:
        return _build_hardened_opener is _PRODUCTION_OPENER_FACTORY

    def _bind_claimed_execution(
        self,
        *,
        validated_authorization: dict[str, Any],
        claim_receipt: dict[str, Any],
        claim_state_path: Path,
        expected_requests: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> None:
        if self._expected_requests or self._opener is not None or self._attempted_calls:
            raise RealPlaceTransportError("real_transport_already_bound")
        validation = deepcopy(validated_authorization)
        if (
            not isinstance(validation, dict)
            or validation.get("authorization") != self._authorization
            or not _hex_64(validation.get("authorizationFingerprint"))
        ):
            raise RealPlaceTransportError("network_authorization_binding_invalid")
        claim = deepcopy(claim_receipt)
        expected_claim = {
            "state": "consumed_in_progress",
            "networkAuthorizationId": self.network_authorization_id,
            "networkAuthorizationFingerprint": validation["authorizationFingerprint"],
            "placeAuthorizationId": self._authorization.get("placeAuthorizationId"),
            "placeAuthorizationFingerprint": self._authorization.get(
                "placeAuthorizationFingerprint"
            ),
            "sessionDirectoryName": self._authorization.get("sessionDirectoryName"),
            "stagingRootBindingFingerprint": self._authorization.get(
                "stagingRootBindingFingerprint"
            ),
            "outboundRequestSequenceFingerprint": self._authorization.get(
                "outboundRequestSequenceFingerprint"
            ),
            "maxCalls": self._authorization.get("maxCalls"),
        }
        if claim != expected_claim:
            raise RealPlaceTransportError("network_authorization_claim_mismatch")
        _, claim_state_fingerprint = _read_persisted_claim_state(
            claim_state_path,
            claim_receipt=claim,
        )
        requests = deepcopy(expected_requests)
        if (
            not isinstance(requests, list)
            or len(requests) != self._authorization.get("maxCalls")
        ):
            raise RealPlaceTransportError("real_transport_request_count_mismatch")
        for ordinal, request in enumerate(requests, start=1):
            _validate_exact_request(request, ordinal=ordinal)
        if canonical_sha256(requests) != self._authorization.get(
            "outboundRequestSequenceFingerprint"
        ):
            raise RealPlaceTransportError("real_transport_request_sequence_mismatch")
        if sorted({request["path"] for request in requests}) != self._authorization[
            "transportProfile"
        ]["paths"]:
            raise RealPlaceTransportError("real_transport_request_paths_mismatch")
        _assert_authorization_current(self._authorization, now=now)
        self._authorization_fingerprint = validation["authorizationFingerprint"]
        self._claim_receipt = claim
        self._claim_state_fingerprint = claim_state_fingerprint
        self._claim_state_path = Path(claim_state_path)
        self._expected_requests = tuple(requests)

    def __call__(self, *, request: dict[str, Any], credential: str) -> dict[str, Any]:
        if not self._expected_requests or not self._authorization_fingerprint:
            raise RealPlaceTransportError("real_transport_not_bound")
        if self._next_index >= len(self._expected_requests):
            raise RealPlaceTransportError("real_transport_call_limit_exceeded")
        expected = self._expected_requests[self._next_index]
        if request != expected:
            raise RealPlaceTransportError("real_transport_request_mismatch")
        if not isinstance(credential, str) or not credential:
            raise RealPlaceTransportError("real_transport_credential_invalid")
        _assert_authorization_current(self._authorization)
        if self._claim_state_path is None:
            raise RealPlaceTransportError("real_transport_claim_state_missing")
        _, current_claim_fingerprint = _read_persisted_claim_state(
            self._claim_state_path,
            claim_receipt=self._claim_receipt,
            allow_pending=True,
        )
        if current_claim_fingerprint != self._claim_state_fingerprint:
            raise RealPlaceTransportError("real_transport_claim_state_changed")
        if self._opener is None:
            self._production_http_active = (
                _build_hardened_opener is _PRODUCTION_OPENER_FACTORY
            )
            self._opener = _build_hardened_opener(
                self._authorization["transportProfile"]["proxyMode"]
            )
        try:
            with self._call_gate.external_call_lease(
                production_http_active=self._production_http_active
            ):
                self._claim_state_fingerprint = _persist_pending_transport_attempt(
                    self._claim_state_path,
                    claim_receipt=self._claim_receipt,
                    expected_fingerprint=self._claim_state_fingerprint,
                    attempted_ordinal=self._next_index + 1,
                )
                self._next_index += 1
                self._attempted_calls += 1
                if self._production_http_active:
                    self._production_calls += 1
                else:
                    self._stub_calls += 1
                return self._open_once(expected, credential=credential)
        except AmapRateLimitCooldownActive:
            raise RealPlaceTransportError(
                "amap_key_transport_cooldown_active"
            ) from None

    def _open_once(self, request: dict[str, Any], *, credential: str) -> dict[str, Any]:
        profile = self._authorization["transportProfile"]
        params = deepcopy(request["params"])
        params["key"] = credential
        request_url = (
            f"https://restapi.amap.com{request['path']}?"
            f"{urlencode(params, doseq=False)}"
        )
        http_request = Request(
            request_url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": _FIXED_USER_AGENT,
                "Connection": "close",
            },
        )
        try:
            response = self._opener.open(
                http_request,
                timeout=float(profile["timeoutSeconds"]),
            )
            with response:
                status = _response_status(response)
                if status != 200:
                    raise RealPlaceTransportError("transport_http_status_invalid")
                final_url = str(response.geturl() or "")
                if final_url != request_url:
                    raise RealPlaceTransportError("transport_redirect_forbidden")
                content_type = str(response.headers.get("Content-Type") or "")
                if content_type.split(";", 1)[0].strip().casefold() != "application/json":
                    raise RealPlaceTransportError("transport_content_type_invalid")
                maximum_bytes = int(profile["maxResponseBytes"])
                raw_length = response.headers.get("Content-Length")
                if raw_length not in (None, ""):
                    try:
                        declared_length = int(raw_length)
                    except (TypeError, ValueError):
                        raise RealPlaceTransportError(
                            "transport_content_length_invalid"
                        ) from None
                    if declared_length < 0 or declared_length > maximum_bytes:
                        raise RealPlaceTransportError("transport_body_size_invalid")
                body = response.read(maximum_bytes + 1)
                if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
                    raise RealPlaceTransportError("transport_body_size_invalid")
        except RealPlaceTransportError:
            raise
        except HTTPError as error:
            try:
                if error.code in {301, 302, 303, 307, 308}:
                    raise RealPlaceTransportError(
                        "transport_redirect_forbidden"
                    ) from None
                raise RealPlaceTransportError("transport_http_error") from None
            finally:
                error.close()
        except (TimeoutError, socket.timeout):
            raise RealPlaceTransportError("transport_timeout") from None
        except ssl.SSLError:
            raise RealPlaceTransportError("transport_tls_error") from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise RealPlaceTransportError("transport_timeout") from None
            raise RealPlaceTransportError("transport_error") from None
        except (OSError, ValueError):
            raise RealPlaceTransportError("transport_error") from None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RealPlaceTransportError("transport_json_invalid") from None
        if not isinstance(payload, dict):
            raise RealPlaceTransportError("transport_json_object_required")
        if str(payload.get("status") or "") != "1" or str(
            payload.get("infocode") or ""
        ) != "10000":
            if payload.get("status") == "0" and payload.get("infocode") == "10021":
                self._call_gate.mark_provider_rate_limited(
                    production_http_active=self._production_http_active
                )
            raise RealPlaceTransportError(
                "amap_response_unsuccessful",
                provider_diagnostic=_amap_provider_diagnostic(
                    payload.get("status"),
                    payload.get("infocode"),
                ),
            )
        return {
            "statusCode": 200,
            "contentType": "application/json",
            "redirected": False,
            "body": body,
        }


def _validate_exact_request(request: Any, *, ordinal: int) -> None:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise RealPlaceTransportError("real_transport_request_schema_invalid")
    if (
        request.get("method") != "GET"
        or request.get("scheme") != "https"
        or request.get("host") != "restapi.amap.com"
        or request.get("path") not in _PLACE_PATHS
        or request.get("allowRedirects") is not False
        or request.get("ordinal") != ordinal
    ):
        raise RealPlaceTransportError("real_transport_request_invalid")
    params = request.get("params")
    if (
        not isinstance(params, dict)
        or set(params) - _PLACE_PARAMETERS[request["path"]]
        or "key" in {str(key).casefold() for key in params}
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in params.items())
    ):
        raise RealPlaceTransportError("real_transport_request_parameters_invalid")
    for field in ("requestFingerprint", "auditFingerprint"):
        if not _hex_64(request.get(field)):
            raise RealPlaceTransportError("real_transport_request_fingerprint_invalid")


def _read_persisted_claim_state(
    path: Any,
    *,
    claim_receipt: dict[str, Any],
    allow_pending: bool = False,
) -> tuple[dict[str, Any], str]:
    if not isinstance(path, Path) or path.name != "session-state.json":
        raise RealPlaceTransportError("real_transport_claim_state_invalid")
    if path.parent.name != claim_receipt.get("sessionDirectoryName"):
        raise RealPlaceTransportError("real_transport_claim_state_invalid")
    try:
        details = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or details.st_size > 65_536
        ):
            raise RealPlaceTransportError("real_transport_claim_state_invalid")
        raw = path.read_bytes()
    except RealPlaceTransportError:
        raise
    except OSError:
        raise RealPlaceTransportError("real_transport_claim_state_invalid") from None
    if not raw or len(raw) > 65_536:
        raise RealPlaceTransportError("real_transport_claim_state_invalid")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RealPlaceTransportError("real_transport_claim_state_invalid") from None
    expected_bindings = {
        "state": "consumed_in_progress",
        "consumed": True,
        "promotable": False,
        "authorizationId": claim_receipt.get("placeAuthorizationId"),
        "authorizationFingerprint": claim_receipt.get(
            "placeAuthorizationFingerprint"
        ),
        "networkAuthorizationId": claim_receipt.get("networkAuthorizationId"),
        "networkAuthorizationFingerprint": claim_receipt.get(
            "networkAuthorizationFingerprint"
        ),
        "stagingRootBindingFingerprint": claim_receipt.get(
            "stagingRootBindingFingerprint"
        ),
        "outboundRequestSequenceFingerprint": claim_receipt.get(
            "outboundRequestSequenceFingerprint"
        ),
        "maxCalls": claim_receipt.get("maxCalls"),
    }
    if not isinstance(state, dict) or any(
        state.get(field) != value for field, value in expected_bindings.items()
    ):
        raise RealPlaceTransportError("real_transport_claim_state_invalid")
    integer_fields = (
        "attemptedPlaceCalls",
        "externalPlaceCalls",
        "stubTransportCalls",
        "fakeTransportCalls",
        "completedResponses",
    )
    if any(
        not isinstance(state.get(field), int)
        or isinstance(state.get(field), bool)
        or state[field] < 0
        for field in integer_fields
    ):
        raise RealPlaceTransportError("real_transport_claim_state_invalid")
    pending = state.get("effectLedgerStatus") == "transport_attempt_pending"
    if pending:
        if (
            not allow_pending
            or "zeroEffectLedger" in state
            or state["attemptedPlaceCalls"] < 1
            or state["completedResponses"] >= state["attemptedPlaceCalls"]
            or any(
                state[field] != 0
                for field in (
                    "externalPlaceCalls",
                    "stubTransportCalls",
                    "fakeTransportCalls",
                )
            )
        ):
            raise RealPlaceTransportError("real_transport_claim_state_invalid")
    else:
        zero_effect = state.get("zeroEffectLedger")
        if (
            state.get("effectLedgerStatus") not in (None, "not_started")
            or any(state[field] != 0 for field in integer_fields)
            or not isinstance(zero_effect, dict)
            or any(
                zero_effect.get(field) != 0
                for field in (
                    "network",
                    "amap",
                    "web",
                    "controller",
                    "capture",
                    "version",
                    "patch",
                    "routeWrite",
                )
            )
        ):
            raise RealPlaceTransportError("real_transport_claim_state_invalid")
    return state, canonical_sha256(state)


def _persist_pending_transport_attempt(
    path: Path,
    *,
    claim_receipt: dict[str, Any],
    expected_fingerprint: str,
    attempted_ordinal: int,
) -> str:
    state, fingerprint = _read_persisted_claim_state(
        path,
        claim_receipt=claim_receipt,
        allow_pending=True,
    )
    if fingerprint != expected_fingerprint:
        raise RealPlaceTransportError("real_transport_claim_state_changed")
    if attempted_ordinal != state["attemptedPlaceCalls"] + 1:
        raise RealPlaceTransportError("real_transport_claim_state_invalid")
    pending = deepcopy(state)
    pending.pop("zeroEffectLedger", None)
    pending["attemptedPlaceCalls"] = attempted_ordinal
    pending["completedResponses"] = attempted_ordinal - 1
    pending["effectLedgerStatus"] = "transport_attempt_pending"
    rendered = json.dumps(
        pending,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    temporary = path.with_name(path.name + ".transport-attempt.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RealPlaceTransportError(
            "real_transport_attempt_state_persist_failed"
        ) from None
    _, persisted_fingerprint = _read_persisted_claim_state(
        path,
        claim_receipt=claim_receipt,
        allow_pending=True,
    )
    return persisted_fingerprint


def _assert_authorization_current(
    authorization: dict[str, Any], *, now: datetime | None = None
) -> None:
    current = _aware_utc(now)
    issued_at = _parse_utc(authorization.get("issuedAt"))
    expires_at = _parse_utc(authorization.get("expiresAt"))
    if issued_at > current or expires_at <= issued_at or current >= expires_at:
        raise RealPlaceTransportError("network_authorization_expired_or_not_yet_valid")


def _response_status(response: Any) -> int:
    raw = getattr(response, "status", None)
    if raw is None:
        raw = response.getcode()
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise RealPlaceTransportError("transport_http_status_invalid")
    return raw


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _hex_64(value: Any) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _aware_utc(value: datetime | None) -> datetime:
    current = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise RealPlaceTransportError("network_authorization_time_invalid")
    return current.astimezone(timezone.utc)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RealPlaceTransportError("network_authorization_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RealPlaceTransportError("network_authorization_time_invalid") from None
    if parsed.tzinfo is None:
        raise RealPlaceTransportError("network_authorization_time_invalid")
    return parsed.astimezone(timezone.utc)


def exact_outbound_request_sequence_fingerprint(
    requests: list[dict[str, Any]],
) -> str:
    snapshot = deepcopy(requests)
    if not isinstance(snapshot, list) or not snapshot:
        raise RealPlaceTransportError("real_transport_request_count_mismatch")
    for ordinal, request in enumerate(snapshot, start=1):
        _validate_exact_request(request, ordinal=ordinal)
    return canonical_sha256(snapshot)

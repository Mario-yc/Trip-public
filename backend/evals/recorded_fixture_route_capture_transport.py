"""Exact, bounded AMap Route HTTPS adapter for the Route acquisition control plane.

The class deliberately exposes neither an opener nor a URL override.  Tests replace
the private opener factory at this module boundary; production callers can only send
the exact request sequence that the Route executor durably claimed first.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
import socket
import ssl
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_SAFE_INFOCODE = re.compile(r"^[0-9]{5}$")
_MAX_NETWORK_AUTHORIZATION_TTL = timedelta(minutes=10)
ROUTE_TRANSPORT_PROFILE = {
    "kind": "amap_route_https_v1",
    "host": "restapi.amap.com",
    "paths": ["/v3/direction/transit/integrated", "/v3/direction/walking"],
    "proxyMode": "direct",
    "timeoutSeconds": 5.0,
    "userAgentProfile": "trip-route-capture-v1",
    "maxResponseBytes": 1_000_000,
}


class RealRouteTransportError(ValueError):
    """A stable, secret-free Route transport failure."""

    def __init__(self, reason_code: str, *, provider_diagnostic: dict[str, str] | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.provider_diagnostic = deepcopy(provider_diagnostic) if provider_diagnostic else None


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> Request | None:
        raise RealRouteTransportError("route_transport_redirect_forbidden")


class AmapWebServiceRouteCaptureTransport:
    """Final production-shaped HTTPS Route adapter with no caller-configurable I/O."""

    def __init_subclass__(cls, **kwargs: Any) -> None:  # pragma: no cover - defensive
        raise TypeError("AmapWebServiceRouteCaptureTransport is final")

    def __init__(self, *, network_authorization: dict[str, Any]) -> None:
        self._network_authorization = _copy_network_authorization(network_authorization)
        _validate_transport_profile(self._network_authorization.get("transportProfile"))
        self._claimed = False
        self._expected_requests: list[dict[str, Any]] = []
        self._call_count = 0
        self._attempted_count = 0
        self._stub_count = 0
        self._external_count = 0
        self._opener: Any = None

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def attempted_count(self) -> int:
        return self._attempted_count

    @property
    def stub_count(self) -> int:
        return self._stub_count

    @property
    def external_count(self) -> int:
        return self._external_count

    def _bind_claimed_execution(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        expected_requests: list[dict[str, Any]],
    ) -> None:
        if self._claimed or not isinstance(expected_requests, list) or not expected_requests:
            raise RealRouteTransportError("route_transport_claim_invalid")
        try:
            persisted = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealRouteTransportError("route_transport_claim_missing") from error
        if not isinstance(persisted, dict):
            raise RealRouteTransportError("route_transport_claim_invalid")
        if persisted != state:
            raise RealRouteTransportError("route_transport_claim_state_mismatch")
        if (
            persisted.get("state") != "consumed_in_progress"
            or persisted.get("consumed") is not True
        ):
            raise RealRouteTransportError("route_transport_claim_invalid")
        if (
            persisted.get("networkAuthorizationId")
            != self._network_authorization["networkAuthorizationId"]
            or persisted.get("networkAuthorizationFingerprint")
            != _canonical_sha256(self._network_authorization)
        ):
            raise RealRouteTransportError("route_transport_claim_authorization_mismatch")
        self._expected_requests = deepcopy(expected_requests)
        self._claimed = True

    def __call__(self, *, request: dict[str, Any], credential: str) -> dict[str, Any]:
        if not self._claimed:
            raise RealRouteTransportError("route_transport_not_claimed")
        if not isinstance(credential, str) or not credential:
            raise RealRouteTransportError("route_credential_missing")
        if self._call_count >= len(self._expected_requests):
            raise RealRouteTransportError("route_transport_call_limit_exceeded")
        expected = self._expected_requests[self._call_count]
        if request != expected:
            raise RealRouteTransportError("route_transport_request_mismatch")
        _validate_request(request)
        _validate_network_authorization_window(self._network_authorization)
        params = {**request["params"], "key": credential}
        # The complete URL is deliberately ephemeral and never stored, hashed, or attached to an error.
        url = f"https://restapi.amap.com{request['path']}?{urlencode(params, doseq=False)}"
        http_request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "trip-route-capture/1",
                "Connection": "close",
            },
        )
        self._attempted_count += 1
        try:
            if self._opener is None:
                self._opener = _build_hardened_opener()
                if _build_hardened_opener is _PRODUCTION_OPENER_FACTORY:
                    self._external_count += 1
                else:
                    self._stub_count += 1
            elif _build_hardened_opener is _PRODUCTION_OPENER_FACTORY:
                self._external_count += 1
            else:
                self._stub_count += 1
            with self._opener.open(http_request, timeout=ROUTE_TRANSPORT_PROFILE["timeoutSeconds"]) as response:
                payload = _read_response(response, request=request, original_url=url)
        except RealRouteTransportError:
            raise
        except socket.timeout as error:
            raise RealRouteTransportError("route_transport_timeout") from error
        except ssl.SSLError as error:
            raise RealRouteTransportError("route_transport_tls_error") from error
        except HTTPError as error:
            raise RealRouteTransportError("route_transport_http_error") from error
        except URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, socket.timeout):
                raise RealRouteTransportError("route_transport_timeout") from error
            if isinstance(reason, ssl.SSLError):
                raise RealRouteTransportError("route_transport_tls_error") from error
            raise RealRouteTransportError("route_transport_error") from error
        except OSError as error:
            raise RealRouteTransportError("route_transport_error") from error
        self._call_count += 1
        return payload


def _build_hardened_opener() -> Any:
    return build_opener(ProxyHandler({}), _NoRedirectHandler())


_PRODUCTION_OPENER_FACTORY = _build_hardened_opener


def _copy_network_authorization(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RealRouteTransportError("route_network_authorization_invalid")
    return deepcopy(value)


def _validate_transport_profile(value: Any) -> None:
    if value != ROUTE_TRANSPORT_PROFILE:
        raise RealRouteTransportError("route_network_transport_profile_invalid")


def _validate_network_authorization_window(value: dict[str, Any]) -> None:
    issued_at = _parse_utc(value.get("issuedAt"))
    expires_at = _parse_utc(value.get("expiresAt"))
    now = datetime.now(timezone.utc)
    if (
        issued_at > now
        or expires_at <= issued_at
        or expires_at - issued_at > _MAX_NETWORK_AUTHORIZATION_TTL
        or now >= expires_at
    ):
        raise RealRouteTransportError("route_authorization_expired_or_not_yet_valid")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RealRouteTransportError("route_authorization_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RealRouteTransportError("route_authorization_time_invalid") from error
    if parsed.tzinfo is None:
        raise RealRouteTransportError("route_authorization_time_invalid")
    return parsed.astimezone(timezone.utc)


def _validate_request(request: Any) -> None:
    if not isinstance(request, dict):
        raise RealRouteTransportError("route_transport_request_invalid")
    if (
        request.get("method") != "GET"
        or request.get("scheme") != "https"
        or request.get("host") != "restapi.amap.com"
        or request.get("path") not in ROUTE_TRANSPORT_PROFILE["paths"]
    ):
        raise RealRouteTransportError("route_transport_request_invalid")
    mode = request.get("mode")
    expected_path = "/v3/direction/transit/integrated" if mode == "transit" else "/v3/direction/walking"
    if request.get("path") != expected_path:
        raise RealRouteTransportError("route_transport_request_invalid")
    params = request.get("params")
    expected_keys = {"origin", "destination", "city", "cityd", "strategy"} if mode == "transit" else {"origin", "destination"}
    if (
        not isinstance(params, dict)
        or set(params) != expected_keys
        or not all(isinstance(key, str) and isinstance(item, str) and item for key, item in params.items())
    ):
        raise RealRouteTransportError("route_transport_request_invalid")


def _read_response(response: Any, *, request: dict[str, Any], original_url: str) -> dict[str, Any]:
    status_code = _response_status_code(response)
    if status_code != 200:
        raise RealRouteTransportError("route_transport_http_status_invalid")
    if _content_type(response) != "application/json":
        raise RealRouteTransportError("route_transport_content_type_invalid")
    final_url = _response_url(response)
    if final_url is not None and final_url != original_url:
        raise RealRouteTransportError("route_transport_redirect_forbidden")
    content_length = _content_length(response)
    if content_length is not None and content_length > ROUTE_TRANSPORT_PROFILE["maxResponseBytes"]:
        raise RealRouteTransportError("route_transport_body_size_invalid")
    body = response.read(ROUTE_TRANSPORT_PROFILE["maxResponseBytes"] + 1)
    if not isinstance(body, bytes) or not body or len(body) > ROUTE_TRANSPORT_PROFILE["maxResponseBytes"]:
        raise RealRouteTransportError("route_transport_body_size_invalid")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RealRouteTransportError("route_transport_json_invalid") from error
    if not isinstance(payload, dict):
        raise RealRouteTransportError("route_transport_json_object_required")
    if payload.get("status") != "1" or payload.get("infocode") != "10000":
        raise RealRouteTransportError(
            "amap_route_response_unsuccessful",
            provider_diagnostic=_provider_diagnostic(payload),
        )
    return _normalized_route_metrics(payload, mode=request["mode"])


def _response_status_code(response: Any) -> int | None:
    getter = getattr(response, "getcode", None)
    value = getter() if callable(getter) else getattr(response, "status", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get_content_type", None)
    if callable(getter):
        return str(getter() or "").strip().casefold()
    if isinstance(headers, dict):
        return str(headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    getter = getattr(response, "getheader", None)
    value = getter("Content-Type") if callable(getter) else None
    return str(value or "").split(";", 1)[0].strip().casefold()


def _content_length(response: Any) -> int | None:
    getter = getattr(response, "getheader", None)
    value = getter("Content-Length") if callable(getter) else None
    if value is None:
        headers = getattr(response, "headers", None)
        if isinstance(headers, dict):
            value = headers.get("Content-Length")
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RealRouteTransportError("route_transport_content_length_invalid") from None
    return parsed if parsed >= 0 else None


def _response_url(response: Any) -> str | None:
    getter = getattr(response, "geturl", None)
    if not callable(getter):
        return None
    value = getter()
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "restapi.amap.com":
        raise RealRouteTransportError("route_transport_redirect_forbidden")
    return value


def _normalized_route_metrics(payload: dict[str, Any], *, mode: str) -> dict[str, float]:
    route = payload.get("route")
    collection_name = "transits" if mode == "transit" else "paths"
    if not isinstance(route, dict) or not isinstance(route.get(collection_name), list) or not route[collection_name]:
        raise RealRouteTransportError("route_transport_payload_invalid")
    first = route[collection_name][0]
    if not isinstance(first, dict):
        raise RealRouteTransportError("route_transport_payload_invalid")
    try:
        duration = float(first.get("duration"))
        distance = float(first.get("distance"))
    except (TypeError, ValueError):
        raise RealRouteTransportError("route_transport_payload_invalid") from None
    if duration <= 0 or distance <= 0:
        raise RealRouteTransportError("route_transport_payload_invalid")
    return {"durationSeconds": duration, "distanceMeters": distance}


def _provider_diagnostic(payload: dict[str, Any]) -> dict[str, str] | None:
    status = payload.get("status")
    infocode = payload.get("infocode")
    if type(status) is not str or status not in {"0", "1"}:
        return None
    if type(infocode) is not str or _SAFE_INFOCODE.fullmatch(infocode) is None:
        return None
    return {
        "schemaVersion": "trip-amap-provider-diagnostic-v1",
        "provider": "amap_web_service",
        "status": status,
        "infocode": infocode,
    }


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> Any:
    raise ValueError("nonfinite_json_number")


def _canonical_sha256(value: Any) -> str:
    import hashlib

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()

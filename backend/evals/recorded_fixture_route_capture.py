"""Exact-scope Route acquisition control plane with a zero-network CLI path.

The executor accepts only the final scripted fake transport or the final
production-shaped HTTPS adapter.  This module's CLI intentionally exposes the
former only, so Z0 can exercise the full claim and quarantine contract without
adding a credential or live-network command surface.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from backend.evals.recorded_fixture_capture import canonical_sha256
from backend.evals.recorded_fixture_route_capture_transport import (
    AmapWebServiceRouteCaptureTransport,
    RealRouteTransportError,
    ROUTE_TRANSPORT_PROFILE,
)


_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_ROUTE_ENVELOPE_SCHEMA = "trip-zero-network-route-envelope-v1"
_ROUTE_AUTH_SCHEMA = "trip-zero-network-route-network-authorization-v1"
_CLAIM_PREFIX = "route-envelope-claim-"
_STATE_NAME = "route-session-state.json"
_BUNDLE_NAME = "route-capture-quarantine.json"
_PARTIAL_NAME = "failed-partial-route-quarantine.json"
_MAX_AUTHORIZATION_TTL = timedelta(minutes=10)
_ZERO_LEDGER = {
    "network": 0,
    "amap": 0,
    "web": 0,
    "controller": 0,
    "capture": 0,
    "version": 0,
    "patch": 0,
    "routeWrite": 0,
}


class RouteCaptureError(ValueError):
    """Secret-free failure code for the zero-network Route control plane."""


def main(argv: list[str] | None = None) -> int:
    """Run only the explicit zero-network Route control plane.

    This CLI intentionally has no HTTP transport, credential option, proxy
    option, or implicit execute path.  A caller can validate frozen material
    without touching staging; execution is possible only with a finite JSON
    script that becomes the exact final fake transport.
    """

    parser = argparse.ArgumentParser(prog="recorded_fixture_route_capture", add_help=True)
    parser.add_argument("--route-manifest", required=True)
    parser.add_argument("--route-envelope", required=True)
    parser.add_argument("--route-authorization", required=True)
    parser.add_argument("--network-authorization", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--scripted-responses")
    parser.add_argument("--execute-zero-network-route-capture", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        manifest = _read_json_file(Path(args.route_manifest), "route_cli_manifest_invalid")
        envelope = _read_json_file(Path(args.route_envelope), "route_cli_envelope_invalid")
        route_auth = _read_json_file(Path(args.route_authorization), "route_cli_authorization_invalid")
        network_auth = _read_json_file(Path(args.network_authorization), "route_cli_network_authorization_invalid")
        validate_zero_network_route_capture_envelope(envelope=envelope, route_manifest=manifest)
        now_utc = _aware_utc(None)
        authorization_fingerprint = _validate_route_authorization(
            route_auth, envelope=envelope, now=now_utc
        )
        _validate_route_network_authorization(
            network_auth,
            route_authorization=route_auth,
            place_authorization_fingerprint=authorization_fingerprint,
            envelope=envelope,
            now=now_utc,
        )
        root = _validated_staging_root(Path(args.staging_root))
        if network_auth["stagingRootBindingFingerprint"] != _root_binding_fingerprint(root):
            raise RouteCaptureError("route_network_staging_root_mismatch")
        if not args.execute_zero_network_route_capture:
            if any(root.iterdir()):
                raise RouteCaptureError("route_staging_root_not_empty")
            result = {
                "status": "READY_FOR_EXPLICIT_ZERO_NETWORK_ROUTE_CAPTURE",
                "consumed": False,
                "promotable": False,
                "requestCount": envelope["exactRouteRequestAllowlist"]["requestCount"],
                "routeManifestFingerprint": manifest["routeManifestFingerprint"],
                "routeEnvelopeFingerprint": envelope["envelopeFingerprint"],
                "zeroEffectLedger": deepcopy(_ZERO_LEDGER),
            }
        else:
            if not isinstance(args.scripted_responses, str):
                raise RouteCaptureError("route_cli_scripted_responses_required")
            scripted = _read_json_file(Path(args.scripted_responses), "route_cli_scripted_responses_invalid")
            responses = scripted.get("responses") if set(scripted) == {"responses"} else None
            if not isinstance(responses, list):
                raise RouteCaptureError("route_cli_scripted_responses_invalid")
            result = execute_zero_network_route_capture(
                envelope=envelope,
                route_manifest=manifest,
                route_authorization=route_auth,
                network_authorization=network_auth,
                staging_root=Path(args.staging_root),
                transport=ScriptedFakeRouteTransport(responses),
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if result.get("status") == "completed" or not args.execute_zero_network_route_capture else 3
    except RouteCaptureError as error:
        print(json.dumps({"status": "failed", "reasonCode": str(error)}, separators=(",", ":")))
        return 2
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"status": "failed", "reasonCode": "route_cli_input_invalid"}, separators=(",", ":")))
        return 2


class ScriptedFakeRouteTransport:
    """Exact final fake transport; arbitrary callables and subclasses are barred."""

    def __init_subclass__(cls, **kwargs: Any) -> None:  # pragma: no cover - defensive
        raise TypeError("ScriptedFakeRouteTransport is final")

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        if not isinstance(responses, list) or not responses:
            raise RouteCaptureError("scripted_route_transport_invalid")
        self._responses = deepcopy(responses)
        self.calls: list[dict[str, Any]] = []
        self._claimed = False

    @property
    def response_count(self) -> int:
        return len(self._responses)

    def _observe_claim(self, state: dict[str, Any]) -> None:
        if self._claimed or state.get("state") != "consumed_in_progress" or state.get("consumed") is not True:
            raise RouteCaptureError("scripted_route_claim_invalid")
        self._claimed = True

    def __call__(self, *, request: dict[str, Any]) -> dict[str, Any]:
        if not self._claimed:
            raise RouteCaptureError("scripted_route_not_claimed")
        index = len(self.calls)
        if index >= len(self._responses):
            raise RouteCaptureError("scripted_route_call_limit_exceeded")
        self.calls.append(deepcopy(request))
        response = self._responses[index]
        if not isinstance(response, dict):
            raise RouteCaptureError("scripted_route_response_invalid")
        return deepcopy(response)


def build_zero_network_route_capture_envelope(*, route_manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = _object_copy(route_manifest, "route_manifest_invalid")
    _validate_route_manifest_shape(manifest)
    leases = deepcopy(manifest["orderedRouteLeases"])
    requests = []
    for ordinal, lease in enumerate(leases, start=1):
        request = {
            "ordinal": ordinal,
            "fromAmapId": lease["fromAmapId"],
            "toAmapId": lease["toAmapId"],
            "mode": lease["mode"],
            "method": lease["providerRequest"]["method"],
            "scheme": lease["providerRequest"]["scheme"],
            "host": lease["providerRequest"]["host"],
            "path": lease["providerRequest"]["path"],
            "params": deepcopy(lease["providerRequest"]["params"]),
            "leaseFingerprint": lease["leaseFingerprint"],
        }
        request["requestFingerprint"] = canonical_sha256(request)
        requests.append(request)
    allowlist = {
        "requestCount": len(requests),
        "requests": requests,
    }
    allowlist["allowlistFingerprint"] = canonical_sha256(allowlist)
    envelope = {
        "schemaVersion": _ROUTE_ENVELOPE_SCHEMA,
        "status": "prepared",
        "captureScope": "route_only",
        "consumed": False,
        "promotable": False,
        "sourceBindings": deepcopy(manifest["sourceBindings"]),
        "routeManifestFingerprint": manifest["routeManifestFingerprint"],
        "exactRouteRequestAllowlist": allowlist,
        "routeCaptureAuthorized": False,
        "placeCaptureAuthorized": False,
        "zeroEffectLedger": deepcopy(_ZERO_LEDGER),
    }
    envelope["envelopeFingerprint"] = canonical_sha256(envelope)
    return envelope


def validate_zero_network_route_capture_envelope(*, envelope: dict[str, Any], route_manifest: dict[str, Any]) -> None:
    manifest = _object_copy(route_manifest, "route_manifest_invalid")
    actual = _object_copy(envelope, "route_capture_envelope_invalid")
    _validate_route_manifest_shape(manifest)
    expected = build_zero_network_route_capture_envelope(route_manifest=manifest)
    if actual != expected:
        raise RouteCaptureError("route_capture_envelope_tampered")


def execute_zero_network_route_capture(
    *,
    envelope: dict[str, Any],
    route_manifest: dict[str, Any],
    route_authorization: dict[str, Any],
    network_authorization: dict[str, Any],
    staging_root: Path,
    transport: ScriptedFakeRouteTransport | AmapWebServiceRouteCaptureTransport,
    credential: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Consume one exact Route grant using an exact fake or HTTPS transport."""

    if type(transport) not in {
        ScriptedFakeRouteTransport,
        AmapWebServiceRouteCaptureTransport,
    }:
        raise RouteCaptureError("exact_route_transport_required")
    manifest = _object_copy(route_manifest, "route_manifest_invalid")
    envelope_snapshot = _object_copy(envelope, "route_capture_envelope_invalid")
    place_auth = _object_copy(route_authorization, "route_authorization_invalid")
    network_auth = _object_copy(network_authorization, "route_network_authorization_invalid")
    validate_zero_network_route_capture_envelope(envelope=envelope_snapshot, route_manifest=manifest)
    requests = envelope_snapshot["exactRouteRequestAllowlist"]["requests"]
    now_utc = _aware_utc(now)
    place_fingerprint = _validate_route_authorization(
        place_auth,
        envelope=envelope_snapshot,
        now=now_utc,
    )
    network_fingerprint = _validate_route_network_authorization(
        network_auth,
        route_authorization=place_auth,
        place_authorization_fingerprint=place_fingerprint,
        envelope=envelope_snapshot,
        now=now_utc,
    )
    if type(transport) is ScriptedFakeRouteTransport and (
        transport.response_count != len(requests) or transport.calls
    ):
        raise RouteCaptureError("scripted_route_transport_state_invalid")
    if type(transport) is AmapWebServiceRouteCaptureTransport and transport.attempted_count:
        raise RouteCaptureError("route_transport_state_invalid")
    root = _validated_staging_root(staging_root)
    root_binding = _root_binding_fingerprint(root)
    if network_auth["stagingRootBindingFingerprint"] != root_binding:
        raise RouteCaptureError("route_network_staging_root_mismatch")
    session_name = network_auth["sessionDirectoryName"]
    with _RootLease(root) as lease:
        claim_filename = _claim_filename(envelope_snapshot)
        existing_entries = list(root.iterdir())
        if existing_entries:
            if (root / claim_filename).is_file():
                raise RouteCaptureError("route_envelope_already_consumed")
            raise RouteCaptureError("route_staging_root_not_empty")
        if (root / session_name).exists():
            raise RouteCaptureError("route_authorization_already_consumed")
        claim_filename = _claim_envelope(
            root_lease=lease,
            envelope=envelope_snapshot,
            route_authorization=place_auth,
            authorization_fingerprint=place_fingerprint,
        )
        try:
            session_directory = root / session_name
            session_directory.mkdir()
        except FileExistsError:
            raise RouteCaptureError("route_authorization_already_consumed") from None
        try:
            state = {
                "schemaVersion": "trip-zero-network-route-session-state-v1",
                "state": "consumed_in_progress",
                "consumed": True,
                "promotable": False,
                "routeAuthorizationId": place_auth["authorizationId"],
                "routeAuthorizationFingerprint": place_fingerprint,
                "networkAuthorizationId": network_auth["networkAuthorizationId"],
                "networkAuthorizationFingerprint": network_fingerprint,
                "routeEnvelopeFingerprint": envelope_snapshot["envelopeFingerprint"],
                "routeManifestFingerprint": manifest["routeManifestFingerprint"],
                "requestCount": len(requests),
                "completedResponses": 0,
                **_transport_metrics(transport),
                **_transport_effect_ledger(transport),
                "claimFile": claim_filename,
            }
            _write_json_new(session_directory / _STATE_NAME, state)
            if type(transport) is ScriptedFakeRouteTransport:
                transport._observe_claim(state)
            else:
                transport._bind_claimed_execution(
                    state_path=session_directory / _STATE_NAME,
                    state=state,
                    expected_requests=requests,
                )
        except (OSError, RouteCaptureError):
            raise RouteCaptureError("route_session_state_persist_failed") from None
        except RealRouteTransportError as error:
            raise RouteCaptureError(error.reason_code) from None
        records: list[dict[str, Any]] = []
        for request in requests:
            try:
                if type(transport) is ScriptedFakeRouteTransport:
                    response = transport(request=deepcopy(request))
                else:
                    pending_state = {
                        **state,
                        "effectLedgerStatus": "transport_attempt_pending",
                        "attemptedRouteCalls": transport.attempted_count + 1,
                    }
                    _write_json_replace(session_directory / _STATE_NAME, pending_state)
                    response = transport(
                        request=deepcopy(request),
                        credential=str(credential or ""),
                    )
                _validate_route_response(response)
            except RealRouteTransportError as error:
                return _persist_partial(
                    session_directory=session_directory,
                    state=state,
                    records=records,
                    failed_ordinal=request["ordinal"],
                    reason_code=error.reason_code,
                    transport=transport,
                    provider_diagnostic=error.provider_diagnostic,
                )
            except RouteCaptureError as error:
                return _persist_partial(
                    session_directory=session_directory,
                    state=state,
                    records=records,
                    failed_ordinal=request["ordinal"],
                    reason_code=str(error),
                    transport=transport,
                )
            records.append(
                {
                    "ordinal": request["ordinal"],
                    "request": deepcopy(request),
                    "response": deepcopy(response),
                    "responseSha256": canonical_sha256(response),
                }
            )
        bundle = {
            "schemaVersion": "trip-recorded-amap-route-v1",
            "recordedAt": now_utc.isoformat(),
            "recordingType": "recorded/non-live",
            "recordedProvider": (
                "AMap Web Service"
                if type(transport) is AmapWebServiceRouteCaptureTransport
                else "Synthetic AMap Route transport"
            ),
            "promotable": False,
            "quarantine": {
                "kind": "route_only_capture",
                "routeManifestFingerprint": manifest["routeManifestFingerprint"],
                "routeEnvelopeFingerprint": envelope_snapshot["envelopeFingerprint"],
                "requestCount": len(requests),
                "completedResponseCount": len(records),
                "routeCalls": 0,
                **_transport_metrics(transport),
                **_transport_effect_ledger(transport),
            },
            "responses": records,
        }
        bundle["bundleFingerprint"] = canonical_sha256(bundle)
        try:
            _write_json_new(session_directory / _BUNDLE_NAME, bundle)
            completed_state = _state_with_transport_effects({
                **state,
                "state": "consumed_completed",
                "completedResponses": len(records),
                **_transport_metrics(transport),
                "quarantineBundleFile": _BUNDLE_NAME,
                "bundleFingerprint": bundle["bundleFingerprint"],
            }, transport)
            _write_json_replace(session_directory / _STATE_NAME, completed_state)
        except OSError:
            return _persist_partial(
                session_directory=session_directory,
                state=state,
                records=records,
                failed_ordinal=len(requests) + 1,
                reason_code="route_quarantine_publish_failed",
                transport=transport,
            )
        return {
            "status": "completed",
            "reasonCode": None,
            "consumed": True,
            "promotable": False,
            "requestCount": len(requests),
            "completedResponseCount": len(records),
            "sessionDirectoryName": session_name,
            "quarantineBundleFile": _BUNDLE_NAME,
            "bundleFingerprint": bundle["bundleFingerprint"],
            **_transport_metrics(transport),
            **_transport_effect_ledger(transport),
        }


def validate_route_capture_quarantine(*, bundle: dict[str, Any], route_envelope: dict[str, Any]) -> None:
    candidate = _object_copy(bundle, "route_quarantine_invalid")
    envelope = _object_copy(route_envelope, "route_capture_envelope_invalid")
    if candidate.get("schemaVersion") != "trip-recorded-amap-route-v1" or candidate.get("recordingType") != "recorded/non-live" or candidate.get("promotable") is not False:
        raise RouteCaptureError("route_quarantine_schema_invalid")
    fingerprint = candidate.get("bundleFingerprint")
    material = deepcopy(candidate)
    material.pop("bundleFingerprint", None)
    if not _is_hex(fingerprint) or canonical_sha256(material) != fingerprint:
        raise RouteCaptureError("route_quarantine_fingerprint_mismatch")
    details = candidate.get("quarantine")
    records = candidate.get("responses")
    requests = envelope.get("exactRouteRequestAllowlist", {}).get("requests")
    if not isinstance(details, dict) or not isinstance(records, list) or not isinstance(requests, list):
        raise RouteCaptureError("route_quarantine_records_invalid")
    if details.get("requestCount") != len(requests) or details.get("completedResponseCount") != len(requests) or len(records) != len(requests):
        raise RouteCaptureError("route_quarantine_incomplete")
    for ordinal, (request, record) in enumerate(zip(requests, records), start=1):
        if not isinstance(record, dict) or record.get("ordinal") != ordinal or record.get("request") != request:
            raise RouteCaptureError("route_quarantine_request_mismatch")
        response = record.get("response")
        if not isinstance(response, dict) or record.get("responseSha256") != canonical_sha256(response):
            raise RouteCaptureError("route_quarantine_response_hash_mismatch")
        _validate_route_response(response)


def _validate_route_manifest_shape(manifest: dict[str, Any]) -> None:
    if manifest.get("schemaVersion") != "trip-recorded-exact-route-manifest-v1":
        raise RouteCaptureError("route_manifest_schema_invalid")
    fingerprint = manifest.get("routeManifestFingerprint")
    material = deepcopy(manifest)
    material.pop("routeManifestFingerprint", None)
    if not _is_hex(fingerprint) or canonical_sha256(material) != fingerprint:
        raise RouteCaptureError("route_manifest_fingerprint_mismatch")
    if not _is_hex(manifest.get("productionDryRouteTraceFingerprint")):
        raise RouteCaptureError("route_manifest_dry_trace_binding_invalid")
    leases = manifest.get("orderedRouteLeases")
    walking_policy = manifest.get("conditionalWalkingCapturePolicy")
    if (
        not isinstance(leases, list)
        or not leases
        or walking_policy != {
            "status": "not_authorized_until_preferred_transit_unavailable",
            "routeCaptureAuthorized": False,
            "maxCalls": 0,
        }
        or manifest.get("pairCount") != len(leases)
    ):
        raise RouteCaptureError("route_manifest_lease_invalid")
    if manifest.get("routeBudget") != {"maxCalls": len(leases), "hardCap": 24}:
        raise RouteCaptureError("route_manifest_budget_invalid")
    if len(leases) > 24:
        raise RouteCaptureError("route_manifest_budget_invalid")
    for lease in leases:
        required = {
            "planningRoot", "rootPortfolioId", "briefId", "dayNumber", "planningSlotId",
            "candidatePhysicalId", "adjacentAnchorIds", "fromAmapId", "toAmapId", "mode",
            "routeContractFingerprint", "reason", "condition", "fromResponseSha256",
            "toResponseSha256", "providerRequest", "leaseFingerprint",
        }
        if not isinstance(lease, dict) or set(lease) != required:
            raise RouteCaptureError("route_manifest_lease_invalid")
        if lease["mode"] not in {"transit", "walking"} or not _is_hex(lease["leaseFingerprint"]):
            raise RouteCaptureError("route_manifest_lease_invalid")
        material = deepcopy(lease)
        material.pop("leaseFingerprint", None)
        if canonical_sha256(material) != lease["leaseFingerprint"]:
            raise RouteCaptureError("route_manifest_lease_invalid")
        if lease["mode"] == "walking" and lease["condition"] != "preferred_unavailable_only":
            raise RouteCaptureError("route_manifest_walking_unconditional")
        _validate_provider_request(lease["providerRequest"], mode=lease["mode"])
        if lease["mode"] != "transit" or lease["condition"] != "preferred":
            raise RouteCaptureError("route_manifest_preferred_transit_invalid")


def _validate_route_authorization(auth: dict[str, Any], *, envelope: dict[str, Any], now: datetime) -> str:
    required = {
        "authorizationId", "scope", "sourceFingerprint", "routeEnvelopeFingerprint",
        "routeManifestFingerprint", "exactRouteRequestAllowlistFingerprint", "maxCalls",
        "issuedAt", "expiresAt",
    }
    if set(auth) != required or not _safe_id(auth.get("authorizationId")) or auth.get("scope") != "route_only":
        raise RouteCaptureError("route_authorization_schema_invalid")
    expected = {
        "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
        "routeEnvelopeFingerprint": envelope["envelopeFingerprint"],
        "routeManifestFingerprint": envelope["routeManifestFingerprint"],
        "exactRouteRequestAllowlistFingerprint": envelope["exactRouteRequestAllowlist"]["allowlistFingerprint"],
        "maxCalls": envelope["exactRouteRequestAllowlist"]["requestCount"],
    }
    if any(auth.get(key) != value for key, value in expected.items()):
        raise RouteCaptureError("route_authorization_binding_mismatch")
    _validate_window(auth, now)
    return canonical_sha256(auth)


def _validate_route_network_authorization(network: dict[str, Any], *, route_authorization: dict[str, Any], place_authorization_fingerprint: str, envelope: dict[str, Any], now: datetime) -> str:
    required = {
        "schemaVersion", "networkAuthorizationId", "scope", "transportKind", "routeAuthorizationId",
        "routeAuthorizationFingerprint", "sourceFingerprint", "routeEnvelopeFingerprint",
        "routeManifestFingerprint", "exactRouteRequestAllowlistFingerprint", "outboundRequestSequenceFingerprint",
        "maxCalls", "sessionDirectoryName", "stagingRootBindingFingerprint", "transportProfile", "issuedAt", "expiresAt",
    }
    if set(network) != required or network.get("schemaVersion") != _ROUTE_AUTH_SCHEMA or network.get("scope") != "route_only" or network.get("transportKind") != "amap_route_https" or not _safe_id(network.get("networkAuthorizationId")) or not _safe_id(network.get("sessionDirectoryName")):
        raise RouteCaptureError("route_network_authorization_schema_invalid")
    requests = envelope["exactRouteRequestAllowlist"]["requests"]
    expected = {
        "routeAuthorizationId": route_authorization["authorizationId"],
        "routeAuthorizationFingerprint": place_authorization_fingerprint,
        "sourceFingerprint": envelope["sourceBindings"]["sourceFingerprint"],
        "routeEnvelopeFingerprint": envelope["envelopeFingerprint"],
        "routeManifestFingerprint": envelope["routeManifestFingerprint"],
        "exactRouteRequestAllowlistFingerprint": envelope["exactRouteRequestAllowlist"]["allowlistFingerprint"],
        "outboundRequestSequenceFingerprint": canonical_sha256(requests),
        "maxCalls": len(requests),
    }
    if (
        any(network.get(key) != value for key, value in expected.items())
        or network.get("transportProfile") != ROUTE_TRANSPORT_PROFILE
        or not _is_hex(network.get("stagingRootBindingFingerprint"))
    ):
        raise RouteCaptureError("route_network_authorization_binding_mismatch")
    _validate_window(network, now)
    return canonical_sha256(network)


def _claim_envelope(*, root_lease: "_RootLease", envelope: dict[str, Any], route_authorization: dict[str, Any], authorization_fingerprint: str) -> str:
    identity = {
        "routeEnvelopeFingerprint": envelope["envelopeFingerprint"],
        "exactRouteRequestAllowlistFingerprint": envelope["exactRouteRequestAllowlist"]["allowlistFingerprint"],
    }
    claim_key = canonical_sha256(identity)
    claim = {
        "schemaVersion": "trip-zero-network-route-envelope-claim-v1",
        **identity,
        "claimKey": claim_key,
        "consumed": True,
        "state": "acquisition_consumed",
        "firstAuthorizationId": route_authorization["authorizationId"],
        "firstAuthorizationFingerprint": authorization_fingerprint,
    }
    claim["claimFingerprint"] = canonical_sha256(claim)
    filename = _claim_filename(envelope)
    try:
        root_lease.write_new(filename, claim)
    except FileExistsError:
        raise RouteCaptureError("route_envelope_already_consumed") from None
    return filename


def _claim_filename(envelope: dict[str, Any]) -> str:
    identity = {
        "routeEnvelopeFingerprint": envelope["envelopeFingerprint"],
        "exactRouteRequestAllowlistFingerprint": envelope[
            "exactRouteRequestAllowlist"
        ]["allowlistFingerprint"],
    }
    return f"{_CLAIM_PREFIX}{canonical_sha256(identity)[:32]}.json"


def _persist_partial(
    *,
    session_directory: Path,
    state: dict[str, Any],
    records: list[dict[str, Any]],
    failed_ordinal: int,
    reason_code: str,
    transport: ScriptedFakeRouteTransport | AmapWebServiceRouteCaptureTransport,
    provider_diagnostic: dict[str, str] | None = None,
) -> dict[str, Any]:
    safe_diagnostic = _safe_provider_diagnostic(provider_diagnostic)
    partial = {
        "schemaVersion": "trip-recorded-amap-route-v1",
        "recordingType": "recorded/non-live",
        "promotable": False,
        "quarantine": {
            "kind": "route_only_capture_partial",
            "completedResponseCount": len(records),
            "failedOrdinal": failed_ordinal,
            "reasonCode": reason_code,
            "routeCalls": 0,
            **_transport_metrics(transport),
            **_transport_effect_ledger(transport),
        },
        "responses": records,
    }
    if safe_diagnostic is not None:
        partial["providerDiagnostic"] = safe_diagnostic
    partial["bundleFingerprint"] = canonical_sha256(partial)
    _write_json_new(session_directory / _PARTIAL_NAME, partial)
    failed_state = _state_with_transport_effects({
        **state,
        "state": "consumed_failed",
        "completedResponses": len(records),
        "partialQuarantineFile": _PARTIAL_NAME,
        "reasonCode": reason_code,
        **_transport_metrics(transport),
    }, transport)
    if safe_diagnostic is not None:
        failed_state["providerDiagnostic"] = safe_diagnostic
    _write_json_replace(session_directory / _STATE_NAME, failed_state)
    result = {
        "status": "failed",
        "reasonCode": reason_code,
        "consumed": True,
        "promotable": False,
        "requestCount": state["requestCount"],
        "completedResponseCount": len(records),
        "failedOrdinal": failed_ordinal,
        "partialQuarantineFile": _PARTIAL_NAME,
        **_transport_metrics(transport),
        **_transport_effect_ledger(transport),
    }
    if safe_diagnostic is not None:
        result["providerDiagnostic"] = safe_diagnostic
    return result


def _transport_metrics(
    transport: ScriptedFakeRouteTransport | AmapWebServiceRouteCaptureTransport,
) -> dict[str, int | bool]:
    if type(transport) is ScriptedFakeRouteTransport:
        return {
            "fakeTransportCalls": len(transport.calls),
            "realTransportAttempts": 0,
            "realTransportCalls": 0,
            "stubTransportCalls": 0,
            "externalRouteCalls": 0,
            "realExternalCapture": False,
        }
    return {
        "fakeTransportCalls": 0,
        "realTransportAttempts": transport.attempted_count,
        "realTransportCalls": transport.call_count,
        "stubTransportCalls": transport.stub_count,
        "externalRouteCalls": transport.external_count,
        "realExternalCapture": transport.external_count > 0,
    }


def _transport_effect_ledger(
    transport: ScriptedFakeRouteTransport | AmapWebServiceRouteCaptureTransport,
) -> dict[str, dict[str, int]]:
    external_calls = (
        transport.external_count
        if type(transport) is AmapWebServiceRouteCaptureTransport
        else 0
    )
    effect = {
        "network": external_calls,
        "amap": external_calls,
        "capture": external_calls,
        "web": 0,
        "controller": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }
    return {
        "externalEffectLedger" if external_calls else "zeroEffectLedger": effect,
    }


def _state_with_transport_effects(
    state: dict[str, Any],
    transport: ScriptedFakeRouteTransport | AmapWebServiceRouteCaptureTransport,
) -> dict[str, Any]:
    material = deepcopy(state)
    material.pop("zeroEffectLedger", None)
    material.pop("externalEffectLedger", None)
    return {**material, **_transport_effect_ledger(transport)}


def _safe_provider_diagnostic(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "provider",
        "status",
        "infocode",
    }:
        return None
    status = value.get("status")
    infocode = value.get("infocode")
    if (
        value.get("schemaVersion") != "trip-amap-provider-diagnostic-v1"
        or value.get("provider") != "amap_web_service"
        or type(status) is not str
        or status not in {"0", "1"}
        or type(infocode) is not str
        or re.fullmatch(r"[0-9]{5}", infocode) is None
    ):
        return None
    return {
        "schemaVersion": "trip-amap-provider-diagnostic-v1",
        "provider": "amap_web_service",
        "status": status,
        "infocode": infocode,
    }


def _validate_route_response(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"durationSeconds", "distanceMeters"}:
        raise RouteCaptureError("route_response_schema_invalid")
    for field in ("durationSeconds", "distanceMeters"):
        number = value[field]
        if not isinstance(number, (int, float)) or isinstance(number, bool) or number <= 0:
            raise RouteCaptureError("route_response_nonpositive")


def _validate_provider_request(value: Any, *, mode: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "method",
        "scheme",
        "host",
        "path",
        "params",
    }:
        raise RouteCaptureError("route_provider_request_schema_invalid")
    expected_path = (
        "/v3/direction/transit/integrated"
        if mode == "transit"
        else "/v3/direction/walking"
    )
    if (
        value.get("method") != "GET"
        or value.get("scheme") != "https"
        or value.get("host") != "restapi.amap.com"
        or value.get("path") != expected_path
    ):
        raise RouteCaptureError("route_provider_request_invalid")
    expected_keys = (
        {"origin", "destination", "city", "cityd", "strategy"}
        if mode == "transit"
        else {"origin", "destination"}
    )
    params = value.get("params")
    if (
        not isinstance(params, dict)
        or set(params) != expected_keys
        or not all(
            isinstance(key, str) and isinstance(item, str) and item
            for key, item in params.items()
        )
    ):
        raise RouteCaptureError("route_provider_request_parameters_invalid")


def _validated_staging_root(value: Path) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise RouteCaptureError("route_staging_root_invalid")
    try:
        original_details = supplied.lstat()
        if _is_link_or_reparse(original_details):
            raise OSError
        path = supplied.resolve(strict=True)
        resolved_details = path.lstat()
    except OSError:
        raise RouteCaptureError("route_staging_root_invalid") from None
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RouteCaptureError("route_staging_root_invalid")
    if _is_link_or_reparse(resolved_details) or not stat.S_ISDIR(resolved_details.st_mode):
        raise RouteCaptureError("route_staging_root_invalid")
    return path


def _is_link_or_reparse(details: os.stat_result) -> bool:
    reparse_point = 0x0400
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & reparse_point)


def _root_binding_fingerprint(root: Path) -> str:
    details = os.stat(root)
    return canonical_sha256({"stagingRootIdentity": {"device": details.st_dev, "file": details.st_ino, "createdAtNs": details.st_ctime_ns}})


class _RootLease:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._identity = _root_binding_fingerprint(root)

    def __enter__(self) -> "_RootLease":
        if _root_binding_fingerprint(self.root) != self._identity:
            raise RouteCaptureError("route_staging_root_changed")
        return self

    def __exit__(self, *_args: Any) -> None:
        if _root_binding_fingerprint(self.root) != self._identity:
            raise RouteCaptureError("route_staging_root_changed")
        return None

    def write_new(self, filename: str, value: dict[str, Any]) -> None:
        _write_json_new(self.root / filename, value)


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json_replace(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    _write_json_new(temporary, value)
    os.replace(temporary, path)


def _validate_window(value: dict[str, Any], now: datetime) -> None:
    issued = _parse_utc(value.get("issuedAt"))
    expires = _parse_utc(value.get("expiresAt"))
    if (
        issued > now
        or expires <= issued
        or expires - issued > _MAX_AUTHORIZATION_TTL
        or now >= expires
    ):
        raise RouteCaptureError("route_authorization_expired_or_not_yet_valid")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RouteCaptureError("route_authorization_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RouteCaptureError("route_authorization_time_invalid") from None
    if parsed.tzinfo is None:
        raise RouteCaptureError("route_authorization_time_invalid")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise RouteCaptureError("route_authorization_time_invalid")
    return current.astimezone(timezone.utc)


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _object_copy(value: Any, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RouteCaptureError(reason)
    return deepcopy(value)


def _read_json_file(path: Path, reason: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10_000_000:
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RouteCaptureError(reason) from None
    if not isinstance(value, dict):
        raise RouteCaptureError(reason)
    return value

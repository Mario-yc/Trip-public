"""Immutable, zero-network combined evidence overlay for recorded Place/Route data.

The sealer never promotes a fixture or alters a tracked case.  It writes a
write-once bundle plus an explicit overlay under a caller-owned directory
(normally ``.ai-runs``), then the replay helper validates exactly that overlay
with no Provider transport capability.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from backend.evals.recorded_fixture_capture import canonical_sha256
from backend.evals.recorded_fixture_canonical_binding import (
    CanonicalBindingError,
    _canonical_hash_matches,
    validate_canonical_identity_binding_certificate,
    validate_exact_route_pair_mode_manifest,
)
from backend.evals.recorded_fixture_route_capture import (
    RouteCaptureError,
    validate_route_capture_quarantine,
)


_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_BUNDLE_NAME = "recorded-combined-evidence.json"
_OVERLAY_NAME = "recorded-case-overlay.json"


class CombinedEvidenceError(ValueError):
    """Stable failure for immutable overlay sealing and dry replay."""


def seal_combined_recorded_evidence(
    *,
    output_directory: Path,
    case_id: str,
    place_quarantine: dict[str, Any],
    route_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    place_envelope: dict[str, Any],
    route_envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    canonical_identity_certificate: dict[str, Any],
    route_manifest: dict[str, Any],
    dry_route_trace: dict[str, Any],
) -> dict[str, Any]:
    """Write a non-promotable combined bundle and explicit case overlay once."""

    if not isinstance(case_id, str) or not case_id.strip():
        raise CombinedEvidenceError("overlay_case_id_invalid")
    _validate_inputs(
        place_quarantine=place_quarantine,
        route_quarantine=route_quarantine,
        preflight_manifest=preflight_manifest,
        place_envelope=place_envelope,
        route_envelope=route_envelope,
        runtime_evidence=runtime_evidence,
        certificate=canonical_identity_certificate,
        route_manifest=route_manifest,
        dry_route_trace=dry_route_trace,
    )
    root = _empty_output_directory(output_directory)
    place = deepcopy(place_quarantine)
    route = deepcopy(route_quarantine)
    certificate = deepcopy(canonical_identity_certificate)
    route_manifest = deepcopy(route_manifest)
    place_records = [
        _place_record(
            record,
            case_id=case_id,
            captured_at=place["recordedAt"],
            provider=place["recordedProvider"],
        )
        for record in place["responses"]
    ]
    route_records = [
        _route_record(
            record,
            case_id=case_id,
            captured_at=route["recordedAt"],
            provider=route["recordedProvider"],
        )
        for record in route["responses"]
    ]
    bundle = {
        "schemaVersion": "trip-recorded-combined-evidence-v1",
        "recordingType": "recorded/non-live",
        "recordedAt": {
            "place": place["recordedAt"],
            "route": route["recordedAt"],
        },
        "recordedProvider": {
            "place": place["recordedProvider"],
            "route": route["recordedProvider"],
        },
        "promotable": False,
        "sourceBindings": deepcopy(certificate["sourceBindings"]),
        "caseId": case_id,
        "placeQuarantineBundleFingerprint": place["bundleFingerprint"],
        "routeQuarantineBundleFingerprint": route["bundleFingerprint"],
        "canonicalIdentityCertificateFingerprint": certificate["certificateFingerprint"],
        "routeManifestFingerprint": route_manifest["routeManifestFingerprint"],
        "dryRouteTraceFingerprint": canonical_sha256(dry_route_trace),
        "records": [*place_records, *route_records],
        "zeroEffectLedger": {
            "network": 0,
            "amap": 0,
            "web": 0,
            "controller": 0,
            "capture": 0,
            "version": 0,
            "patch": 0,
            "routeWrite": 0,
        },
    }
    _reject_sensitive(bundle)
    bundle["bundleFingerprint"] = canonical_sha256(bundle)
    overlay = {
        "schemaVersion": "trip-recorded-case-overlay-v1",
        "recordingType": "recorded/non-live",
        "promotable": False,
        "caseId": case_id,
        "combinedBundleFile": _BUNDLE_NAME,
        "combinedBundleFingerprint": bundle["bundleFingerprint"],
        "sourceBindings": deepcopy(bundle["sourceBindings"]),
        "expectedRecords": [
            {
                "endpoint": row["request"]["endpoint"],
                "params": deepcopy(row["request"]["params"]),
                **({"requestPair": deepcopy(row["requestPair"])} if "requestPair" in row else {}),
                "responseSha256": row["responseSha256"],
            }
            for row in bundle["records"]
        ],
        "canonicalIdentityCertificateFingerprint": certificate["certificateFingerprint"],
        "routeManifestFingerprint": route_manifest["routeManifestFingerprint"],
        "dryRouteTraceFingerprint": bundle["dryRouteTraceFingerprint"],
    }
    overlay["overlayFingerprint"] = canonical_sha256(overlay)
    _write_json_new(root / _BUNDLE_NAME, bundle)
    try:
        _write_json_new(root / _OVERLAY_NAME, overlay)
    except OSError:
        # A partial seal is deliberately visible and never auto-repaired.
        raise CombinedEvidenceError("overlay_publish_failed") from None
    return {
        "bundleFile": _BUNDLE_NAME,
        "overlayFile": _OVERLAY_NAME,
        "bundleFingerprint": bundle["bundleFingerprint"],
        "overlayFingerprint": overlay["overlayFingerprint"],
        "recordCount": len(bundle["records"]),
        "promotable": False,
        "zeroEffectLedger": deepcopy(bundle["zeroEffectLedger"]),
    }


def validate_combined_recorded_evidence_overlay(*, overlay_path: Path) -> dict[str, Any]:
    """Validate a local immutable overlay with no network or service calls."""

    path = Path(overlay_path)
    overlay = _read_json_object(path, "overlay_unreadable")
    if overlay.get("schemaVersion") != "trip-recorded-case-overlay-v1" or overlay.get("recordingType") != "recorded/non-live" or overlay.get("promotable") is not False:
        raise CombinedEvidenceError("overlay_schema_invalid")
    fingerprint = overlay.get("overlayFingerprint")
    material = deepcopy(overlay)
    material.pop("overlayFingerprint", None)
    if not _is_hex(fingerprint) or canonical_sha256(material) != fingerprint:
        raise CombinedEvidenceError("overlay_fingerprint_mismatch")
    bundle_file = overlay.get("combinedBundleFile")
    if bundle_file != _BUNDLE_NAME:
        raise CombinedEvidenceError("overlay_bundle_file_invalid")
    bundle = _read_json_object(path.parent / bundle_file, "combined_bundle_unreadable")
    if bundle.get("schemaVersion") != "trip-recorded-combined-evidence-v1" or bundle.get("recordingType") != "recorded/non-live" or bundle.get("promotable") is not False:
        raise CombinedEvidenceError("combined_bundle_schema_invalid")
    bundle_fingerprint = bundle.get("bundleFingerprint")
    bundle_material = deepcopy(bundle)
    bundle_material.pop("bundleFingerprint", None)
    if not _is_hex(bundle_fingerprint) or canonical_sha256(bundle_material) != bundle_fingerprint:
        raise CombinedEvidenceError("combined_bundle_fingerprint_mismatch")
    if overlay.get("combinedBundleFingerprint") != bundle_fingerprint or overlay.get("caseId") != bundle.get("caseId") or overlay.get("sourceBindings") != bundle.get("sourceBindings"):
        raise CombinedEvidenceError("overlay_bundle_binding_mismatch")
    _reject_sensitive(bundle)
    records = bundle.get("records")
    expected = overlay.get("expectedRecords")
    if not isinstance(records, list) or not isinstance(expected, list) or len(records) != len(expected) or not records:
        raise CombinedEvidenceError("overlay_record_count_invalid")
    actual_expected: list[dict[str, Any]] = []
    request_keys: set[str] = set()
    route_count = 0
    place_count = 0
    for row in records:
        if not isinstance(row, dict):
            raise CombinedEvidenceError("combined_record_invalid")
        request = row.get("request")
        response = row.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict) or not _is_hex(row.get("responseSha256")):
            raise CombinedEvidenceError("combined_record_invalid")
        if not _canonical_hash_matches(response, row["responseSha256"]):
            raise CombinedEvidenceError("combined_response_hash_mismatch")
        endpoint = request.get("endpoint")
        params = request.get("params")
        if not isinstance(endpoint, str) or not isinstance(params, dict):
            raise CombinedEvidenceError("combined_request_invalid")
        expected_row = {
            "endpoint": endpoint,
            "params": deepcopy(params),
            "responseSha256": row["responseSha256"],
        }
        if endpoint.startswith("/v3/direction/"):
            pair = row.get("requestPair")
            if not isinstance(pair, dict) or set(pair) != {"fromAmapId", "toAmapId", "mode"}:
                raise CombinedEvidenceError("combined_route_pair_invalid")
            _validate_route_source_lineage(row)
            expected_row["requestPair"] = deepcopy(pair)
            route_count += 1
        elif endpoint in {"/v3/place/text", "/v3/place/around"}:
            place_count += 1
        else:
            raise CombinedEvidenceError("combined_endpoint_invalid")
        key = canonical_sha256({"request": expected_row, "ordinal": len(actual_expected) + 1})
        if key in request_keys:
            raise CombinedEvidenceError("combined_record_duplicate")
        request_keys.add(key)
        actual_expected.append(expected_row)
    if actual_expected != expected or route_count == 0 or place_count == 0:
        raise CombinedEvidenceError("overlay_record_binding_mismatch")
    return {"overlay": overlay, "bundle": bundle}


class StrictRecordedOverlayReplaySession:
    """Stateful, zero-network dry adapter for the opaque-choice contract.

    It deliberately does not call a product writer.  It is an explicit
    recorded-shaped harness whose mutable in-memory state makes first and
    duplicate choice deltas observable instead of returning scripted constants.
    """

    def __init__(self, *, overlay_path: Path) -> None:
        validated = validate_combined_recorded_evidence_overlay(overlay_path=overlay_path)
        self._bundle = validated["bundle"]
        self._active_version_id: str | None = None
        self._timeline_fingerprint: str | None = None
        self._committed_choices: set[tuple[str, str]] = set()

    def commit_opaque_choice(self, *, source_assistant_turn_id: str, choice_id: str) -> dict[str, Any]:
        if not _safe_opaque_id(source_assistant_turn_id) or not _safe_opaque_id(choice_id):
            raise CombinedEvidenceError("opaque_choice_identity_invalid")
        identity = (source_assistant_turn_id, choice_id)
        if identity in self._committed_choices:
            return {
                "sourceAssistantTurnId": source_assistant_turn_id,
                "choiceId": choice_id,
                "writeDelta": {"version": 0, "patch": 0, "routeWrite": 0},
                "activeVersionId": self._active_version_id,
            }
        if self._committed_choices:
            raise CombinedEvidenceError("synthetic_overlay_choice_conflict")
        timeline_fingerprint = canonical_sha256(
            {
                "bundleFingerprint": self._bundle["bundleFingerprint"],
                "sourceAssistantTurnId": source_assistant_turn_id,
                "choiceId": choice_id,
            }
        )
        self._committed_choices.add(identity)
        self._active_version_id = timeline_fingerprint
        self._timeline_fingerprint = timeline_fingerprint
        return {
            "sourceAssistantTurnId": source_assistant_turn_id,
            "choiceId": choice_id,
            "writeDelta": {"version": 1, "patch": 1, "routeWrite": 0},
            "activeVersionId": self._active_version_id,
        }

    def reload(self) -> dict[str, str]:
        if self._active_version_id is None or self._timeline_fingerprint is None:
            raise CombinedEvidenceError("synthetic_overlay_reload_before_commit")
        return {
            "activeVersionId": self._active_version_id,
            "timelineFingerprint": self._timeline_fingerprint,
        }


def run_strict_recorded_overlay_replay(
    *, overlay_path: Path,
    source_assistant_turn_id: str,
    choice_id: str,
) -> dict[str, Any]:
    """Exercise the explicit stateful dry adapter over an immutable overlay."""

    session = StrictRecordedOverlayReplaySession(overlay_path=overlay_path)
    first = session.commit_opaque_choice(
        source_assistant_turn_id=source_assistant_turn_id,
        choice_id=choice_id,
    )
    duplicate = session.commit_opaque_choice(
        source_assistant_turn_id=source_assistant_turn_id,
        choice_id=choice_id,
    )
    reload = session.reload()
    return {
        "schemaVersion": "trip-strict-recorded-overlay-replay-v1",
        "syntheticContractOnly": True,
        "recordingType": "recorded/non-live",
        "networkSentinel": {"network": 0, "amap": 0, "web": 0, "controller": 0},
        "preAdoptionWriteDelta": {"version": 0, "patch": 0, "routeWrite": 0},
        "firstOpaqueChoiceCommit": first,
        "duplicateOpaqueChoiceCommit": duplicate,
        "reload": reload,
        "combinedBundleFingerprint": session._bundle["bundleFingerprint"],
    }


def _validate_inputs(*, place_quarantine: dict[str, Any], route_quarantine: dict[str, Any], preflight_manifest: dict[str, Any], place_envelope: dict[str, Any], route_envelope: dict[str, Any], runtime_evidence: dict[str, Any], certificate: dict[str, Any], route_manifest: dict[str, Any], dry_route_trace: dict[str, Any]) -> None:
    try:
        validate_canonical_identity_binding_certificate(
            certificate=certificate,
            place_quarantine=place_quarantine,
            preflight_manifest=preflight_manifest,
            envelope=place_envelope,
            runtime_evidence=runtime_evidence,
        )
        validate_exact_route_pair_mode_manifest(
            route_manifest=route_manifest,
            anchor_binding_certificate=certificate,
            dry_route_trace=dry_route_trace,
        )
        if route_manifest.get("productionDryRouteTraceFingerprint") != dry_route_trace.get(
            "productionDryRouteTraceFingerprint"
        ):
            raise CombinedEvidenceError("route_manifest_dry_trace_mismatch")
        validate_route_capture_quarantine(bundle=route_quarantine, route_envelope=route_envelope)
    except (CanonicalBindingError, RouteCaptureError) as error:
        raise CombinedEvidenceError(str(error)) from None
    if route_envelope.get("routeManifestFingerprint") != route_manifest.get("routeManifestFingerprint"):
        raise CombinedEvidenceError("route_envelope_manifest_mismatch")
    for quarantine, expected_providers in (
        (place_quarantine, {"AMap Web Service"}),
        (route_quarantine, {"Synthetic AMap Route transport", "AMap Web Service"}),
    ):
        if (
            not isinstance(quarantine.get("recordedAt"), str)
            or not quarantine["recordedAt"]
            or quarantine.get("recordedProvider") not in expected_providers
        ):
            raise CombinedEvidenceError("recorded_capture_provenance_invalid")

def _place_record(
    record: dict[str, Any], *, case_id: str, captured_at: str, provider: str
) -> dict[str, Any]:
    request = record.get("request")
    response = record.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise CombinedEvidenceError("place_record_invalid")
    endpoint = request.get("endpoint")
    if endpoint not in {"/v3/place/text", "/v3/place/around"}:
        raise CombinedEvidenceError("place_record_endpoint_invalid")
    result = {
        "caseId": case_id,
        "capturedAt": captured_at,
        "provider": provider,
        "request": {"endpoint": endpoint, "params": deepcopy(request.get("params") or {})},
        "response": deepcopy(response),
        "responseSha256": record.get("responseSha256"),
    }
    if not _canonical_hash_matches(result["response"], result["responseSha256"]):
        raise CombinedEvidenceError("place_record_hash_invalid")
    return result


def _route_record(
    record: dict[str, Any], *, case_id: str, captured_at: str, provider: str
) -> dict[str, Any]:
    request = record.get("request")
    response = record.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise CombinedEvidenceError("route_record_invalid")
    mode = request.get("mode")
    if mode not in {"transit", "walking"}:
        raise CombinedEvidenceError("route_record_mode_invalid")
    endpoint = request.get("path")
    if not isinstance(endpoint, str):
        raise CombinedEvidenceError("route_record_endpoint_invalid")
    pair = {"fromAmapId": request.get("fromAmapId"), "toAmapId": request.get("toAmapId"), "mode": mode}
    if any(not isinstance(value, str) or not value for value in pair.values()):
        raise CombinedEvidenceError("route_record_pair_invalid")
    request_material = deepcopy(request)
    source_request_fingerprint = request_material.pop("requestFingerprint", None)
    if (
        not _is_hex(source_request_fingerprint)
        or canonical_sha256(request_material) != source_request_fingerprint
        or not _canonical_hash_matches(response, record.get("responseSha256"))
    ):
        raise CombinedEvidenceError("route_record_source_lineage_invalid")
    replay_response = {"status": "1", "infocode": "10000", "route": deepcopy(response)}
    result = {
        "caseId": case_id,
        "capturedAt": captured_at,
        "provider": provider,
        "request": {"endpoint": endpoint, "params": deepcopy(request.get("params") or {})},
        "requestPair": pair,
        "response": replay_response,
        "responseSha256": canonical_sha256(replay_response),
        "sourceRequestFingerprint": source_request_fingerprint,
        "sourceResponseSha256": record["responseSha256"],
    }
    return result


def _validate_route_source_lineage(row: dict[str, Any]) -> None:
    route_response = row.get("response")
    if (
        not _is_hex(row.get("sourceRequestFingerprint"))
        or not _is_hex(row.get("sourceResponseSha256"))
        or not isinstance(route_response, dict)
        or route_response.get("status") != "1"
        or route_response.get("infocode") != "10000"
        or not isinstance(route_response.get("route"), dict)
        or not _canonical_hash_matches(
            route_response["route"], row["sourceResponseSha256"]
        )
    ):
        raise CombinedEvidenceError("combined_route_source_lineage_invalid")


def _empty_output_directory(value: Path) -> Path:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise CombinedEvidenceError("overlay_output_directory_invalid")
    try:
        supplied_details = supplied.lstat()
        if _is_link_or_reparse(supplied_details):
            raise OSError
        resolved = supplied.resolve(strict=True)
        details = resolved.lstat()
    except OSError:
        raise CombinedEvidenceError("overlay_output_directory_invalid") from None
    if not resolved.is_absolute() or resolved.is_symlink() or _is_link_or_reparse(details) or not resolved.is_dir() or any(resolved.iterdir()):
        raise CombinedEvidenceError("overlay_output_directory_invalid")
    if not stat_is_directory(details):
        raise CombinedEvidenceError("overlay_output_directory_invalid")
    return resolved


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())


def _read_json_object(path: Path, reason: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 10_000_000:
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CombinedEvidenceError(reason) from None
    if not isinstance(value, dict):
        raise CombinedEvidenceError(reason)
    return value


def _reject_sensitive(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if "http://" in rendered.casefold() or "https://" in rendered.casefold() or re.search(r'"(?:key|token|authorization|cookie|proxy|signature)"\s*:', rendered, flags=re.IGNORECASE):
        raise CombinedEvidenceError("combined_evidence_sensitive_value_forbidden")


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _safe_opaque_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9._:-]{8,256}", value))


def stat_is_directory(details: os.stat_result) -> bool:
    return stat.S_ISDIR(details.st_mode)


def _is_link_or_reparse(details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & 0x0400)

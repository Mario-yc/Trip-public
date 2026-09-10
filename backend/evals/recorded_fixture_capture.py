"""Build and seal the zero-network Phase-1 Place capture preflight.

The tool intentionally stops before any external request.  It consumes the
server-produced semantic trace from ``run_offline.py --capture-semantic-manifest``
and decides whether the exact Phase-1 Place request manifest can be sealed into
a prepared envelope awaiting explicit authorization.  Route prerequisites are
recorded only; this module does not authorize or enter Route capture.  It never
turns legacy candidate hints or mock response IDs into Provider input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

CASES_DIR = PROJECT_ROOT / "backend" / "evals" / "cases"
DEDICATED_CASE_ID = "creative_portfolio_search_profile_recorded"
REQUIRED_CASE_IDS = (DEDICATED_CASE_ID,)
CASE_PATH_BY_ID = {
    DEDICATED_CASE_ID: "fixed_creative_portfolio_goal_recorded/creative_portfolio_search_profile_recorded.json",
}
SCHEMA_VERSION = "recorded-fixture-capture-preflight-v1"
CAPTURE_SESSION_ENVELOPE_SCHEMA_VERSION = (
    "recorded-fixture-zero-network-capture-session-envelope-v1"
)
_CONFIGURATION_PATHS = (
    "backend/.env.example",
    "frontend/.env.example",
)
_FIXED_JOURNEY_PATHS = (
    "e2e/portfolio-user-journey.spec.ts",
    "e2e/portfolio-deterministic-ui.spec.ts",
)
_UNTRACKED_SOURCE_PREFIXES = (
    "backend/evals/",
    "backend/src/",
    "backend/tests/",
    "frontend/src/",
    "frontend/tests/",
    "e2e/",
)
_UNTRACKED_SOURCE_SUFFIXES = frozenset({".py", ".json", ".ts", ".tsx", ".js", ".jsx", ".css"})
_PLACE_ENDPOINT_PARAMETERS = {
    "place/text": frozenset(
        {"keywords", "city", "citylimit", "offset", "page", "extensions", "output", "types"}
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
_SECRET_PARAMETER_NAMES = frozenset(
    {"key", "api_key", "apikey", "authorization", "cookie", "proxy", "token", "headers", "url"}
)
_PLACE_IDENTITY_ROUTE_DISCOVERY_BLOCKERS = frozenset(
    {
        "non_canonical_amap_identity",
        "offline_mock_amap_poi_provenance",
        "poi_source_not_amap_place_search",
    }
)


def canonical_sha256(value: Any) -> str:
    """Return the stable SHA-256 used by capture authorization evidence."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def compute_source_fingerprint(*, project_root: Path = PROJECT_ROOT) -> str:
    """Fingerprint the exact source/configuration surface used by the Goal gates."""

    head = _git_stdout(project_root, "rev-parse", "HEAD").decode("utf-8").strip()
    dirty_diff = _git_stdout(project_root, "diff", "--binary", "HEAD", "--", ".")
    untracked_output = _git_stdout(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
    ).decode("utf-8")
    untracked_paths = sorted(
        path.replace("\\", "/")
        for path in untracked_output.splitlines()
        if _is_untracked_source_path(path.replace("\\", "/"))
    )
    material = {
        "head": head,
        "dirtyDiffSha256": hashlib.sha256(dirty_diff).hexdigest().upper(),
        "dirtyDiffUtf8Bytes": len(dirty_diff),
        "configurationFingerprints": {
            path: _file_sha256(project_root / path) for path in _CONFIGURATION_PATHS
        },
        "fixedJourneyFingerprints": {
            path: _file_sha256(project_root / path) for path in _FIXED_JOURNEY_PATHS
        },
        "untrackedSourceFiles": {
            path: _file_sha256(project_root / Path(path)) for path in untracked_paths
        },
    }
    return canonical_sha256(material)


def _git_stdout(project_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"source fingerprint git command failed: {' '.join(args)}") from error
    return completed.stdout


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest().upper()
    except OSError as error:
        raise ValueError(f"source fingerprint input is unreadable: {path.name}") from error


def _is_untracked_source_path(path: str) -> bool:
    return path.startswith(_UNTRACKED_SOURCE_PREFIXES) and Path(path).suffix.lower() in (
        _UNTRACKED_SOURCE_SUFFIXES
    )


def build_capture_preflight_manifest(
    *,
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> dict[str, Any]:
    """Return an identity-free manifest from actual offline runtime evidence.

    A missing production Search Profile is a terminal *preflight* condition.
    It does not consume the one external capture session, because no safe
    Provider query or identity binding exists to send to AMap.
    """

    results = {
        str(item.get("id") or ""): item
        for item in runtime_evidence.get("cases") or []
        if isinstance(item, dict)
    }
    cases: list[dict[str, Any]] = []
    for case_id in REQUIRED_CASE_IDS:
        case_path = _resolve_case_path(cases_dir, case_id)
        case_payload = _load_case(case_path, expected_case_id=case_id)
        result = results.get(case_id) or {}
        trace = result.get("recordedCaptureSemanticTrace")
        if not isinstance(trace, dict):
            trace = {}
        raw_profile_runs = trace.get("profileRuns")
        profile_runs, profile_run_errors = _safe_profile_runs(raw_profile_runs)
        profile_status = str(trace.get("profileExecutionStatus") or "not_observed")
        logical_slots, logical_slot_errors = _safe_logical_slots(trace.get("logicalSlots"))
        logical_edges, logical_edge_errors = _safe_logical_edges(trace.get("logicalRouteTopology"))
        route_discovery = result.get("recordedRouteDiscovery")
        route_budget = (
            _safe_route_budget(route_discovery.get("routeBudget"))
            if isinstance(route_discovery, dict)
            else {"known": False, "source": "", "reason": "route_discovery_not_observed"}
        )
        city = _case_city(case_payload, trace, profile_runs)
        observed_adcode = _safe_observed_amap_adcode(trace.get("observedAmapAdcode"))
        blocked_reasons = _capture_preflight_blockers(
            trace=trace,
            profile_status=profile_status,
            profile_runs=profile_runs,
            profile_run_errors=profile_run_errors,
            logical_slots=logical_slots,
            logical_slot_errors=logical_slot_errors,
            logical_edges=logical_edges,
            logical_edge_errors=logical_edge_errors,
            route_discovery=route_discovery,
        )
        deferred_route_prerequisites = _deferred_route_prerequisites(trace)
        cases.append(
            {
                "caseId": case_id,
                "originalUserMessages": _case_messages(case_payload, trace),
                "city": city,
                "adcode": observed_adcode["adcode"],
                "adcodeSource": observed_adcode["source"],
                "semanticSlots": logical_slots,
                "productionSearchProfile": {
                    "executionStatus": profile_status,
                    "runs": profile_runs,
                },
                "logicalRouteTopology": logical_edges,
                "placeSearchBudget": _place_budget(profile_runs),
                "routeBudget": route_budget,
                "transport": _transport_summary(logical_edges),
                "logicalPlanSource": str(trace.get("logicalPlanSource") or "not_observed"),
                "persistedInitialPlanStatus": str(trace.get("persistedInitialPlanStatus") or "missing"),
                "logicalRouteAuthorization": _safe_route_authorization(trace.get("logicalRouteAuthorization")),
                "placeIdentityCaptureEligible": not blocked_reasons,
                "routePairFreezeEligible": False,
                "captureBlockedReasons": blocked_reasons,
                "deferredRoutePrerequisites": deferred_route_prerequisites,
            }
        )

    blockers = [
        {
            "caseId": item["caseId"],
            "reasons": item["captureBlockedReasons"],
        }
        for item in cases
        if item["captureBlockedReasons"]
    ]
    phase1 = _build_phase1_place_request_manifest(
        runtime_evidence=runtime_evidence,
        results=results,
        cases_dir=cases_dir,
        legacy_blockers=blockers,
    )
    place_identity_ready = (
        not blockers and phase1["manifest"]["status"] == "ready"
    )
    for case in cases:
        case["placeIdentityCaptureEligible"] = bool(
            case.get("placeIdentityCaptureEligible") and place_identity_ready
        )
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "capturePreflightId": f"capture-preflight-{uuid.uuid4().hex}",
        "captureState": (
            "ready_for_place_identity_capture" if place_identity_ready else "preflight_blocked"
        ),
        "terminalStatus": (
            None if place_identity_ready else "STOPPED_AT_RECORDED_CAPTURE_GAP"
        ),
        "externalCaptureSession": {
            "id": None,
            "consumed": False,
            "recordedFixtureCaptureUsed": 0,
            "externalPlaceCalls": 0,
            "externalRouteCalls": 0,
            "ledgerDelta": 0,
        },
        "networkCalls": 0,
        "allowlist": [],
        "cases": cases,
        "blockers": blockers,
        "sourceFingerprint": phase1["sourceFingerprint"],
        "dedicatedCaseSha256": phase1["dedicatedCaseSha256"],
        "runtimeEvidenceSha256": phase1["runtimeEvidenceSha256"],
        "opaqueChoiceCheckpointFingerprint": phase1[
            "opaqueChoiceCheckpointFingerprint"
        ],
        "routeContractFingerprint": phase1["routeContractFingerprint"],
        "placeRequestMultisetFingerprint": phase1[
            "placeRequestMultisetFingerprint"
        ],
        "phase1PlaceRequestManifest": phase1["manifest"],
        "placeRequestClosureStatus": phase1["manifest"][
            "placeRequestClosureStatus"
        ],
        "placeIdentityCaptureEligible": place_identity_ready,
        "placeIdentityClosureComplete": False,
        "routePairFreezeEligible": False,
    }
    manifest["manifestFingerprint"] = _manifest_fingerprint(manifest)
    return manifest


def _build_phase1_place_request_manifest(
    *,
    runtime_evidence: dict[str, Any],
    results: dict[str, dict[str, Any]],
    cases_dir: Path,
    legacy_blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    result = results.get(DEDICATED_CASE_ID) or {}
    trace = result.get("recordedCaptureSemanticTrace")
    trace = trace if isinstance(trace, dict) else {}
    raw_requests = trace.get("phase1PlaceRequestEvidence")
    requests = deepcopy(raw_requests) if isinstance(raw_requests, list) else []
    blockers: list[str] = []
    if legacy_blockers:
        blockers.append("legacy_capture_preflight_blocked")
        blockers.extend(
            str(reason)
            for blocker in legacy_blockers
            if isinstance(blocker, dict)
            for reason in blocker.get("reasons") or []
            if str(reason or "")
        )
    if not isinstance(raw_requests, list) or not requests:
        blockers.append("exact_place_request_evidence_missing")
    blockers.extend(
        f"phase1_runtime_evidence_error:{str(error)}"
        for error in trace.get("phase1PlaceRequestErrors") or []
        if str(error or "")
    )
    external_ledger = result.get("realExternalCallLedger")
    if not _exact_zero_ledger(
        external_ledger,
        fields={"network", "amap", "web", "controller"},
    ):
        blockers.append("external_call_ledger_nonzero_or_missing")
    replay = result.get("persistedClarificationChoiceReplay")
    if not isinstance(replay, dict):
        blockers.append("opaque_choice_replay_evidence_missing")
        replay = {}
    replay_route_contract = (
        replay.get("routeDecisionContract")
        if isinstance(replay.get("routeDecisionContract"), dict)
        else {}
    )
    write_delta = replay.get("writeDelta")
    if not _exact_zero_ledger(
        write_delta,
        fields={"version", "patch", "route"},
    ):
        blockers.append("formal_write_ledger_nonzero_or_missing")
    capture_delta = replay.get("recordedFixtureCaptureDelta")
    if not _is_exact_nonnegative_int(capture_delta) or capture_delta != 0:
        blockers.append("recorded_capture_ledger_nonzero_or_missing")

    case_path = _resolve_case_path(cases_dir, DEDICATED_CASE_ID)
    expected_case_sha = _file_sha256(case_path)
    current_source_fingerprint = compute_source_fingerprint()
    expected_runtime_sha = _runtime_trace_fingerprint(trace)
    root_bindings = {
        "sourceFingerprint": current_source_fingerprint,
        "dedicatedCaseSha256": expected_case_sha,
        "runtimeEvidenceSha256": expected_runtime_sha,
        "opaqueChoiceCheckpointFingerprint": str(
            replay.get("checkpointFingerprint") or ""
        ),
        "routeContractFingerprint": str(
            replay_route_contract.get("fingerprint") or ""
        ),
    }
    for field, expected in root_bindings.items():
        observed = str(trace.get(field) or "")
        if not _is_sha256(expected) or observed != expected:
            blockers.append(f"{field}_stale_or_invalid")
    if str(trace.get("opaqueChoiceCheckpointFingerprint") or "") != str(
        replay.get("checkpointFingerprint") or ""
    ):
        blockers.append("opaque_choice_checkpoint_result_binding_invalid")
    route_fingerprint = str(trace.get("routeContractFingerprint") or "")
    route_authorization = trace.get("logicalRouteAuthorization")
    route_authorization = route_authorization if isinstance(route_authorization, dict) else {}
    if (
        route_fingerprint != str(replay_route_contract.get("fingerprint") or "")
        or route_fingerprint != str(route_authorization.get("contractFingerprint") or "")
    ):
        blockers.append("route_contract_result_binding_invalid")

    request_audits: list[str] = []
    request_fingerprints: list[str] = []
    receipt_fingerprints: list[str] = []
    receipt_acquisitions: list[tuple[str, int]] = []
    occurrence_request_scopes: list[tuple[str, str, str]] = []
    for index, request in enumerate(requests):
        request_errors = _place_request_errors(request, trace=trace)
        blockers.extend(f"request_{index}_{error}" for error in request_errors)
        if isinstance(request, dict):
            request_audits.append(str(request.get("auditFingerprint") or ""))
            request_fingerprints.append(str(request.get("requestFingerprint") or ""))
            receipt = request.get("budgetReceipt")
            receipt = receipt if isinstance(receipt, dict) else {}
            occurrence = request.get("profileOccurrence")
            occurrence = occurrence if isinstance(occurrence, dict) else {}
            lineage = request.get("queryPlanLineage")
            lineage = lineage if isinstance(lineage, dict) else {}
            receipt_fingerprints.append(str(receipt.get("receiptFingerprint") or ""))
            receipt_acquisitions.append(
                (
                    str(receipt.get("budgetObjectId") or ""),
                    _safe_nonnegative_int(receipt.get("acquisitionOrdinal")),
                )
            )
            occurrence_request_scopes.append(
                (
                    str(occurrence.get("occurrenceFingerprint") or ""),
                    str(lineage.get("queryScopeFingerprint") or ""),
                    str(request.get("requestFingerprint") or ""),
                )
            )
    audit_counts = Counter(request_audits)
    if any(value > 1 for key, value in audit_counts.items() if key):
        blockers.append("unexplained_duplicate_place_request_audit")
    if len(receipt_fingerprints) != len(set(receipt_fingerprints)) or any(
        not _is_sha256(value) for value in receipt_fingerprints
    ):
        blockers.append("budget_receipt_reused_or_invalid")
    if len(receipt_acquisitions) != len(set(receipt_acquisitions)) or any(
        not budget_id or ordinal <= 0 for budget_id, ordinal in receipt_acquisitions
    ):
        blockers.append("budget_acquisition_reused_or_invalid")
    if len(occurrence_request_scopes) != len(set(occurrence_request_scopes)):
        blockers.append("unexplained_duplicate_occurrence_request")

    request_multiset = [
        {
            "auditFingerprint": audit,
            "requestFingerprint": request_fingerprint,
            "count": count,
        }
        for (audit, request_fingerprint), count in sorted(
            Counter(zip(request_audits, request_fingerprints)).items()
        )
    ]
    place_multiset_fingerprint = canonical_sha256(request_multiset)
    raw_web_invocations = trace.get("phase1WebInvocationCount")
    raw_web_seed_count = trace.get("phase1WebSeedCount")
    if not _is_exact_nonnegative_int(raw_web_invocations) or not _is_exact_nonnegative_int(
        raw_web_seed_count
    ):
        blockers.append("web_dependency_ledger_missing_or_invalid")
    web_invocations = (
        raw_web_invocations if _is_exact_nonnegative_int(raw_web_invocations) else 0
    )
    web_seed_count = (
        raw_web_seed_count if _is_exact_nonnegative_int(raw_web_seed_count) else 0
    )
    deferred = deepcopy(trace.get("phase1DeferredDependencies"))
    if not isinstance(deferred, list):
        blockers.append("web_dependency_evidence_missing_or_invalid")
        deferred = []
    if len(deferred) != web_invocations or sum(
        _safe_nonnegative_int(item.get("seedCount"))
        for item in deferred
        if isinstance(item, dict)
    ) != web_seed_count:
        blockers.append("web_dependency_ledger_mismatch")
    if web_seed_count > 0 and not _web_seed_requests_closed(deferred):
        blockers.append("web_seed_place_request_dependency_unclosed")

    observed_adcode = _safe_observed_amap_adcode(trace.get("observedAmapAdcode"))
    request_cities = [
        str((request.get("sanitizedParams") or {}).get("city") or "")
        for request in requests
        if isinstance(request, dict)
        and isinstance(request.get("sanitizedParams"), dict)
    ]
    if (
        observed_adcode["status"] != "observed"
        or observed_adcode["observedRequestCount"] != len(requests)
        or not request_cities
        or set(request_cities) != {observed_adcode["adcode"]}
    ):
        blockers.append("observed_adcode_place_request_binding_invalid")
    blockers = list(dict.fromkeys(blockers))
    status = "ready" if not blockers else "blocked"
    manifest = {
        "status": status,
        "placeRequestClosureStatus": (
            "phase1_exact_requests_frozen" if status == "ready" else "phase1_incomplete"
        ),
        "requestCount": len(requests),
        "requests": requests,
        "requestMultiset": request_multiset,
        "webInvocationCount": web_invocations,
        "webSeedCount": web_seed_count,
        "deferredDependencies": deferred,
        "blockers": blockers,
        "placeIdentityCaptureEligible": status == "ready",
        "placeIdentityClosureComplete": False,
        "routePairFreezeEligible": False,
    }
    return {
        **root_bindings,
        "placeRequestMultisetFingerprint": place_multiset_fingerprint,
        "manifest": manifest,
    }


def _load_case(path: Path, *, expected_case_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"capture case is unreadable: {path.name}") from error
    if not isinstance(payload, dict) or str(payload.get("id") or "") != expected_case_id:
        raise ValueError(f"capture case is invalid: {path.name}")
    return payload


def _resolve_case_path(cases_dir: Path, case_id: str) -> Path:
    relative = Path(CASE_PATH_BY_ID.get(case_id, f"{case_id}.json"))
    nested = cases_dir / relative
    return nested if nested.is_file() else cases_dir / relative.name


def _is_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9A-Fa-f]{64}", str(value or "")))


def _is_exact_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _exact_zero_ledger(value: Any, *, fields: set[str]) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == fields
        and all(_is_exact_nonnegative_int(value.get(field)) and value[field] == 0 for field in fields)
    )


def _runtime_trace_fingerprint(trace: dict[str, Any]) -> str:
    material = deepcopy(trace)
    material.pop("runtimeEvidenceSha256", None)
    return canonical_sha256(material)


def _manifest_fingerprint(manifest: dict[str, Any]) -> str:
    material = deepcopy(manifest)
    material.pop("capturePreflightId", None)
    material.pop("manifestFingerprint", None)
    return canonical_sha256(material)


def _place_request_errors(request: Any, *, trace: dict[str, Any]) -> list[str]:
    if not isinstance(request, dict):
        return ["invalid"]
    errors: list[str] = []
    endpoint = str(request.get("endpoint") or "")
    expected_request_keys = {
        "endpoint",
        "sanitizedParams",
        "requestFingerprint",
        "profileOccurrence",
        "queryPlanLineage",
        "budgetReceipt",
        "productionLowLevelFetch",
        "auditFingerprint",
    }
    if endpoint == "place/around":
        expected_request_keys.add("anchorLineage")
    if set(request) != expected_request_keys:
        errors.append("schema_invalid")
    params = request.get("sanitizedParams")
    if endpoint not in _PLACE_ENDPOINT_PARAMETERS:
        errors.append("endpoint_invalid")
    if not isinstance(params, dict):
        errors.append("parameters_invalid")
        params = {}
    parameter_names = {str(key).lower() for key in params}
    if parameter_names & _SECRET_PARAMETER_NAMES:
        errors.append("secret_parameter_present")
    allowed = _PLACE_ENDPOINT_PARAMETERS.get(endpoint, frozenset())
    if set(params) - allowed:
        errors.append("unknown_parameter_present")
    expected_request_fingerprint = canonical_sha256(
        {"endpoint": endpoint, "sanitizedParams": params}
    )
    if str(request.get("requestFingerprint") or "") != expected_request_fingerprint:
        errors.append("fingerprint_invalid")

    occurrence = request.get("profileOccurrence")
    lineage = request.get("queryPlanLineage")
    if not _request_lineage_matches_trace(
        occurrence,
        lineage,
        endpoint=endpoint,
        trace=trace,
    ):
        errors.append("profile_or_query_plan_lineage_invalid")
    if endpoint == "place/around" and not _valid_around_lineage(
        params=params,
        anchor_lineage=request.get("anchorLineage"),
    ):
        errors.append("around_anchor_lineage_invalid")
    if endpoint == "place/around" and str(
        (request.get("anchorLineage") or {}).get("queryScopeFingerprint") or ""
    ) != str((lineage or {}).get("queryScopeFingerprint") or ""):
        errors.append("around_query_scope_binding_invalid")
    if endpoint == "place/around" and isinstance(request.get("anchorLineage"), dict):
        if str((request.get("anchorLineage") or {}).get("queryScopeFingerprint") or "") != str(
            (lineage or {}).get("queryScopeFingerprint") if isinstance(lineage, dict) else ""
        ):
            errors.append("around_query_scope_binding_invalid")

    receipt = request.get("budgetReceipt")
    if not _valid_budget_receipt(
        receipt,
        endpoint=endpoint,
        request_fingerprint=str(request.get("requestFingerprint") or ""),
        occurrence=occurrence,
        lineage=lineage,
    ):
        errors.append("budget_receipt_invalid")
    if not _valid_production_low_level_fetch(
        request.get("productionLowLevelFetch"),
        endpoint=endpoint,
        occurrence=occurrence,
        lineage=lineage,
        receipt=receipt,
    ):
        errors.append("production_low_level_fetch_provenance_invalid")
    expected_audit = deepcopy(request)
    expected_audit.pop("auditFingerprint", None)
    if str(request.get("auditFingerprint") or "") != canonical_sha256(expected_audit):
        errors.append("audit_fingerprint_invalid")
    return errors


def _request_lineage_matches_trace(
    occurrence: Any,
    lineage: Any,
    *,
    endpoint: str,
    trace: dict[str, Any],
) -> bool:
    if not isinstance(occurrence, dict) or not isinstance(lineage, dict):
        return False
    required_occurrence = {
        "profileId",
        "profileFingerprint",
        "executionFingerprint",
        "briefId",
        "poolId",
        "planningSlotId",
        "dayNumber",
        "occurrenceFingerprint",
    }
    if set(occurrence) != required_occurrence:
        return False
    if set(lineage) != {
        "sourcePlanId",
        "sourcePlanFingerprint",
        "providerPlanId",
        "providerPlanFingerprint",
        "queryScopeFingerprint",
    }:
        return False
    if not all(
        _is_sha256(lineage.get(field))
        for field in (
            "sourcePlanFingerprint",
            "providerPlanFingerprint",
            "queryScopeFingerprint",
        )
    ):
        return False
    for profile in trace.get("profileRuns") or []:
        if not isinstance(profile, dict):
            continue
        scope = profile.get("scope") if isinstance(profile.get("scope"), dict) else {}
        expected_occurrence = {
            "profileId": str(profile.get("profileId") or ""),
            "profileFingerprint": str(profile.get("profileFingerprint") or ""),
            "executionFingerprint": str(profile.get("executionFingerprint") or ""),
            "briefId": str(scope.get("briefId") or ""),
            "poolId": str(scope.get("poolId") or ""),
            "planningSlotId": str(scope.get("planningSlotId") or ""),
            "dayNumber": _safe_nonnegative_int(scope.get("dayNumber")),
            "occurrenceFingerprint": str(
                profile.get("occurrenceFingerprint") or ""
            ),
        }
        occurrence_material = deepcopy(expected_occurrence)
        observed_occurrence_fingerprint = occurrence_material.pop(
            "occurrenceFingerprint", ""
        )
        if (
            occurrence != expected_occurrence
            or not all(expected_occurrence.values())
            or observed_occurrence_fingerprint != canonical_sha256(occurrence_material)
        ):
            continue
        for plan in profile.get("queryPlans") or []:
            if not isinstance(plan, dict):
                continue
            if (
                str(plan.get("planId") or "") == str(lineage.get("providerPlanId") or "")
                and str(plan.get("sourcePlanId") or "")
                == str(lineage.get("sourcePlanId") or "")
                and str(plan.get("sourcePlanFingerprint") or "")
                == str(lineage.get("sourcePlanFingerprint") or "")
                and str(plan.get("providerPlanFingerprint") or "")
                == str(lineage.get("providerPlanFingerprint") or "")
                and str(plan.get("planId") or "")
                and str(plan.get("sourcePlanId") or "")
            ):
                if endpoint != "place/around":
                    expected_query_scope = canonical_sha256(
                        {
                            "occurrenceFingerprint": str(
                                occurrence.get("occurrenceFingerprint") or ""
                            ),
                            "sourcePlanFingerprint": str(
                                lineage.get("sourcePlanFingerprint") or ""
                            ),
                            "providerPlanFingerprint": str(
                                lineage.get("providerPlanFingerprint") or ""
                            ),
                            "requestShape": {
                                "endpoint": str(plan.get("endpoint") or ""),
                                "city": str(plan.get("city") or ""),
                                "keyword": str(plan.get("keyword") or ""),
                                "category": str(plan.get("category") or ""),
                                "limit": _safe_nonnegative_int(
                                    plan.get("resultLimit")
                                ),
                                "radius": 0,
                            },
                        }
                    )
                    if str(lineage.get("queryScopeFingerprint") or "") != (
                        expected_query_scope
                    ):
                        continue
                return True
    return False


def _valid_around_lineage(*, params: dict[str, Any], anchor_lineage: Any) -> bool:
    if not isinstance(anchor_lineage, dict):
        return False
    if not str(params.get("location") or ""):
        return False
    radius = _safe_nonnegative_int(params.get("radius"))
    if radius <= 0:
        return False
    required = {"slotId", "previous", "next", "queryScopeFingerprint"}
    if (
        set(anchor_lineage) != required
        or not str(anchor_lineage.get("slotId") or "")
        or not _is_sha256(anchor_lineage.get("queryScopeFingerprint"))
    ):
        return False
    anchors = [
        item
        for item in (anchor_lineage.get("previous"), anchor_lineage.get("next"))
        if item is not None
    ]
    if not anchors or not all(_valid_anchor(item) for item in anchors):
        return False
    try:
        longitude_text, latitude_text = str(params["location"]).split(",", maxsplit=1)
        center = (round(float(longitude_text), 6), round(float(latitude_text), 6))
    except (KeyError, TypeError, ValueError):
        return False
    return center in {
        (round(float(item["longitude"]), 6), round(float(item["latitude"]), 6))
        for item in anchors
    }


def _valid_anchor(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"amapId", "longitude", "latitude"}:
        return False
    if re.fullmatch(r"B[0-9A-Z]{8,31}", str(value.get("amapId") or "").upper()) is None:
        return False
    try:
        longitude = float(value["longitude"])
        latitude = float(value["latitude"])
    except (TypeError, ValueError):
        return False
    return -180 <= longitude <= 180 and -90 <= latitude <= 90


def _valid_budget_receipt(
    receipt: Any,
    *,
    endpoint: str,
    request_fingerprint: str,
    occurrence: Any,
    lineage: Any,
) -> bool:
    if not isinstance(receipt, dict):
        return False
    if set(receipt) != {
        "budgetObjectId",
        "acquired",
        "endpoint",
        "requestFingerprint",
        "profileOccurrenceFingerprint",
        "queryPlanLineageFingerprint",
        "queryScopeFingerprint",
        "queryVariantFingerprint",
        "before",
        "after",
        "beforeSnapshot",
        "afterSnapshot",
        "acquisitionOrdinal",
        "fetchOrdinal",
        "receiptFingerprint",
    }:
        return False
    expected_fingerprint_material = deepcopy(receipt)
    observed_fingerprint = str(expected_fingerprint_material.pop("receiptFingerprint", "") or "")
    if observed_fingerprint != canonical_sha256(expected_fingerprint_material):
        return False
    if re.fullmatch(r"budget-object-[1-9][0-9]*", str(receipt.get("budgetObjectId") or "")) is None:
        return False
    if (
        receipt.get("acquired") is not True
        or str(receipt.get("endpoint") or "") != endpoint
        or str(receipt.get("requestFingerprint") or "") != request_fingerprint
        or str(receipt.get("profileOccurrenceFingerprint") or "")
        != canonical_sha256(occurrence)
        or str(receipt.get("queryPlanLineageFingerprint") or "")
        != canonical_sha256(lineage)
        or str(receipt.get("queryScopeFingerprint") or "")
        != str((lineage or {}).get("queryScopeFingerprint") or "")
        or not _is_sha256(receipt.get("queryVariantFingerprint"))
    ):
        return False
    if not _is_exact_nonnegative_int(receipt.get("acquisitionOrdinal")) or not _is_exact_nonnegative_int(
        receipt.get("fetchOrdinal")
    ):
        return False
    acquisition_ordinal = int(receipt["acquisitionOrdinal"])
    fetch_ordinal = int(receipt["fetchOrdinal"])
    if acquisition_ordinal <= 0 or fetch_ordinal <= acquisition_ordinal:
        return False
    before = receipt.get("before")
    after = receipt.get("after")
    before_snapshot = receipt.get("beforeSnapshot")
    after_snapshot = receipt.get("afterSnapshot")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not _valid_budget_snapshot_pair(
        before_snapshot,
        after_snapshot,
        before=before,
        after=after,
    ):
        return False
    counter_fields = {"usedCalls", "newQueryCalls", "textSearchCalls", "aroundSearchCalls"}
    if set(before) != counter_fields or set(after) != counter_fields:
        return False
    deltas = {
        field: _safe_nonnegative_int(after.get(field)) - _safe_nonnegative_int(before.get(field))
        for field in counter_fields
    }
    expected_endpoint_counter = (
        "textSearchCalls" if endpoint == "place/text" else "aroundSearchCalls"
    )
    other_endpoint_counter = (
        "aroundSearchCalls" if endpoint == "place/text" else "textSearchCalls"
    )
    return (
        deltas["usedCalls"] == 1
        and deltas["newQueryCalls"] == 1
        and deltas[expected_endpoint_counter] == 1
        and deltas[other_endpoint_counter] == 0
    )


def _valid_production_low_level_fetch(
    value: Any,
    *,
    endpoint: str,
    occurrence: Any,
    lineage: Any,
    receipt: Any,
) -> bool:
    if not isinstance(value, dict) or not isinstance(receipt, dict):
        return False
    expected_keys = {
        "endpoint",
        "productionMethod",
        "acquisitionOrdinal",
        "enteredOrdinal",
        "profileOccurrenceFingerprint",
        "queryPlanLineageFingerprint",
        "markerFingerprint",
    }
    if set(value) != expected_keys:
        return False
    material = deepcopy(value)
    observed_fingerprint = str(material.pop("markerFingerprint", "") or "")
    expected_method = "_fetch_amap_place" if endpoint == "place/text" else "_fetch_amap_around"
    if (
        observed_fingerprint != canonical_sha256(material)
        or str(value.get("endpoint") or "") != endpoint
        or str(value.get("productionMethod") or "") != expected_method
        or str(value.get("profileOccurrenceFingerprint") or "")
        != canonical_sha256(occurrence)
        or str(value.get("queryPlanLineageFingerprint") or "")
        != canonical_sha256(lineage)
    ):
        return False
    if not all(
        _is_exact_nonnegative_int(value.get(field))
        for field in ("acquisitionOrdinal", "enteredOrdinal")
    ):
        return False
    return bool(
        value["acquisitionOrdinal"] == receipt.get("acquisitionOrdinal")
        and value["acquisitionOrdinal"] < value["enteredOrdinal"] < receipt.get("fetchOrdinal", 0)
    )


def _valid_budget_snapshot_pair(
    before_snapshot: Any,
    after_snapshot: Any,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> bool:
    if not isinstance(before_snapshot, dict) or not isinstance(after_snapshot, dict):
        return False
    expected_keys = {
        "source",
        "budget",
        "used",
        "cacheHitCount",
        "duplicateExternalQueryCount",
        "reusedQueryCount",
        "newQueryCount",
        "skippedBecauseBudget",
    }
    if set(before_snapshot) != expected_keys or set(after_snapshot) != expected_keys:
        return False
    budget_fields = {
        "amapPoiTextSearchMax",
        "amapPoiTextAndDetailMax",
        "amapPoiAroundSearchMax",
        "amapRouteRefreshMax",
        "amapTotalExternalCallsMax",
    }
    used_fields = {
        "usedPlaceText",
        "usedPlaceDetail",
        "usedPlaceAround",
        "usedRoute",
        "usedTotalExternal",
    }
    for snapshot in (before_snapshot, after_snapshot):
        budget = snapshot.get("budget")
        used = snapshot.get("used")
        if (
            not isinstance(budget, dict)
            or set(budget) != budget_fields
            or not all(_is_exact_nonnegative_int(budget.get(field)) for field in budget_fields)
            or not isinstance(used, dict)
            or set(used) != used_fields
            or not all(_is_exact_nonnegative_int(used.get(field)) for field in used_fields)
            or not all(
                _is_exact_nonnegative_int(snapshot.get(field))
                for field in (
                    "cacheHitCount",
                    "duplicateExternalQueryCount",
                    "reusedQueryCount",
                    "newQueryCount",
                    "skippedBecauseBudget",
                )
            )
        ):
            return False
    if not all(
        isinstance(counters, dict)
        and set(counters)
        == {"usedCalls", "newQueryCalls", "textSearchCalls", "aroundSearchCalls"}
        and all(_is_exact_nonnegative_int(counters.get(field)) for field in counters)
        for counters in (before, after)
    ):
        return False
    if (
        not str(before_snapshot.get("source") or "")
        or before_snapshot.get("source") != after_snapshot.get("source")
        or before_snapshot.get("budget") != after_snapshot.get("budget")
    ):
        return False
    for field in (
        "cacheHitCount",
        "duplicateExternalQueryCount",
        "reusedQueryCount",
        "skippedBecauseBudget",
    ):
        if _safe_nonnegative_int(before_snapshot.get(field)) != _safe_nonnegative_int(
            after_snapshot.get(field)
        ):
            return False
    if (
        _safe_nonnegative_int(before_snapshot.get("newQueryCount"))
        != _safe_nonnegative_int(before.get("newQueryCalls"))
        or _safe_nonnegative_int(after_snapshot.get("newQueryCount"))
        != _safe_nonnegative_int(after.get("newQueryCalls"))
    ):
        return False
    for snapshot, counters in ((before_snapshot, before), (after_snapshot, after)):
        used = snapshot["used"]
        if (
            _safe_nonnegative_int(used.get("usedTotalExternal"))
            != _safe_nonnegative_int(counters.get("usedCalls"))
            or _safe_nonnegative_int(used.get("usedPlaceText"))
            != _safe_nonnegative_int(counters.get("textSearchCalls"))
            or _safe_nonnegative_int(used.get("usedPlaceAround"))
            != _safe_nonnegative_int(counters.get("aroundSearchCalls"))
        ):
            return False
    return True


def _web_seed_requests_closed(deferred: list[Any]) -> bool:
    matching = [
        item
        for item in deferred
        if isinstance(item, dict) and str(item.get("kind") or "") == "web_seed_place_requests"
    ]
    return bool(matching) and all(str(item.get("status") or "") == "closed" for item in matching)


def validate_capture_preflight_manifest(
    *,
    manifest: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> None:
    """Fail closed if any Phase-1 evidence or authorization binding changed."""

    if not isinstance(manifest, dict):
        raise ValueError("capture preflight manifest must be an object")
    rebuilt = build_capture_preflight_manifest(
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    expected = deepcopy(rebuilt)
    actual = deepcopy(manifest)
    expected.pop("capturePreflightId", None)
    actual.pop("capturePreflightId", None)
    if actual != expected:
        raise ValueError("capture preflight manifest content or binding was tampered")
    if str(manifest.get("manifestFingerprint") or "") != _manifest_fingerprint(manifest):
        raise ValueError("capture preflight manifest fingerprint is invalid")
    phase1 = manifest.get("phase1PlaceRequestManifest")
    if not isinstance(phase1, dict) or phase1.get("status") != "ready":
        raise ValueError("Phase-1 Place request manifest is not ready")
    if phase1.get("placeRequestClosureStatus") != "phase1_exact_requests_frozen":
        raise ValueError("Phase-1 Place request closure is not frozen")
    if phase1.get("requestCount") != len(phase1.get("requests") or []):
        raise ValueError("Phase-1 Place request count is inconsistent")
    if manifest.get("captureState") != "ready_for_place_identity_capture":
        raise ValueError("Place identity capture is not eligible")
    if manifest.get("networkCalls") != 0:
        raise ValueError("capture preflight network ledger is nonzero")
    external = manifest.get("externalCaptureSession")
    if not isinstance(external, dict) or external != {
        "id": None,
        "consumed": False,
        "recordedFixtureCaptureUsed": 0,
        "externalPlaceCalls": 0,
        "externalRouteCalls": 0,
        "ledgerDelta": 0,
    }:
        raise ValueError("capture preflight external ledger is invalid")


def build_zero_network_capture_session_envelope(
    *,
    manifest: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> dict[str, Any]:
    """Seal the validated Phase-1 Place allowlist without executing capture.

    The display ID is diagnostic only.  Every authorization-relevant field is
    derived from the validated preflight manifest and covered by the stable
    content fingerprint.
    """

    manifest_snapshot = deepcopy(manifest)
    runtime_evidence_snapshot = deepcopy(runtime_evidence)
    validate_capture_preflight_manifest(
        manifest=manifest_snapshot,
        runtime_evidence=runtime_evidence_snapshot,
        cases_dir=cases_dir,
    )
    phase1 = manifest_snapshot["phase1PlaceRequestManifest"]
    exact_requests = deepcopy(phase1["requests"])
    request_multiset = deepcopy(phase1["requestMultiset"])
    receipt_references = [
        _capture_budget_receipt_reference(request)
        for request in exact_requests
    ]
    exact_allowlist_material = {
        "requestCount": deepcopy(phase1["requestCount"]),
        "requests": exact_requests,
        "requestMultiset": request_multiset,
        "requestMultisetFingerprint": str(
            manifest_snapshot["placeRequestMultisetFingerprint"]
        ),
    }
    exact_allowlist = {
        **exact_allowlist_material,
        "allowlistFingerprint": canonical_sha256(exact_allowlist_material),
    }
    envelope = {
        "schemaVersion": CAPTURE_SESSION_ENVELOPE_SCHEMA_VERSION,
        "captureSessionDisplayId": f"capture-envelope-{uuid.uuid4().hex}",
        "sessionId": None,
        "consumed": False,
        "status": "prepared",
        "authorizationStatus": "awaiting_explicit_authorization",
        "captureScope": "place_only",
        "sourceBindings": {
            "sourceFingerprint": str(manifest_snapshot["sourceFingerprint"]),
            "dedicatedCaseSha256": str(
                manifest_snapshot["dedicatedCaseSha256"]
            ),
            "runtimeEvidenceSha256": str(
                manifest_snapshot["runtimeEvidenceSha256"]
            ),
            "opaqueChoiceCheckpointFingerprint": str(
                manifest_snapshot["opaqueChoiceCheckpointFingerprint"]
            ),
            "routeContractFingerprint": str(
                manifest_snapshot["routeContractFingerprint"]
            ),
            "placeRequestMultisetFingerprint": str(
                manifest_snapshot["placeRequestMultisetFingerprint"]
            ),
            "capturePreflightManifestFingerprint": str(
                manifest_snapshot["manifestFingerprint"]
            ),
        },
        "contentFingerprints": {
            "phase1PlaceRequestManifest": canonical_sha256(phase1),
            "exactPlaceRequestAllowlist": exact_allowlist[
                "allowlistFingerprint"
            ],
            "budgetReceiptReferences": canonical_sha256(receipt_references),
        },
        "exactPlaceRequestAllowlist": exact_allowlist,
        "budgetReceiptReferences": receipt_references,
        "phaseOrdering": {
            "currentPhase": "place_identity_capture",
            "placePhaseStatus": "prepared",
            "placeAuthorizationStatus": "awaiting_explicit_authorization",
            "productionReplayStatus": "not_started",
            "canonicalIdentityBindingCertificate": None,
            "exactRoutePairModeManifest": None,
            "routePhaseStatus": "blocked_prerequisites_missing",
            "routePhaseBlockers": [
                "canonical_identity_binding_certificate_missing",
                "exact_route_pair_mode_manifest_missing",
                "route_capture_not_authorized",
            ],
        },
        "placeIdentityClosureComplete": False,
        "routePairFreezeEligible": False,
        "routeCaptureAuthorized": False,
        "externalCaptureSession": {
            "id": None,
            "consumed": False,
            "recordedFixtureCaptureUsed": 0,
            "externalPlaceCalls": 0,
            "externalRouteCalls": 0,
            "ledgerDelta": 0,
        },
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
    fingerprint = _capture_session_envelope_fingerprint(envelope)
    envelope["contentFingerprint"] = fingerprint
    envelope["envelopeFingerprint"] = fingerprint
    return envelope


def validate_zero_network_capture_session_envelope(
    *,
    envelope: dict[str, Any],
    manifest: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> None:
    """Fail closed unless the envelope is a fresh, exact Place-only seal."""

    envelope_snapshot = deepcopy(envelope)
    manifest_snapshot = deepcopy(manifest)
    runtime_evidence_snapshot = deepcopy(runtime_evidence)
    validate_capture_preflight_manifest(
        manifest=manifest_snapshot,
        runtime_evidence=runtime_evidence_snapshot,
        cases_dir=cases_dir,
    )
    if not isinstance(envelope_snapshot, dict):
        raise ValueError("capture session envelope must be an object")
    display_id = str(envelope_snapshot.get("captureSessionDisplayId") or "")
    if re.fullmatch(r"capture-envelope-[0-9a-f]{32}", display_id) is None:
        raise ValueError("capture session display ID is invalid")
    observed_fingerprint = str(envelope_snapshot.get("contentFingerprint") or "")
    if (
        not _is_sha256(observed_fingerprint)
        or str(envelope_snapshot.get("envelopeFingerprint") or "")
        != observed_fingerprint
        or observed_fingerprint
        != _capture_session_envelope_fingerprint(envelope_snapshot)
    ):
        raise ValueError("capture session envelope fingerprint is invalid")
    rebuilt = build_zero_network_capture_session_envelope(
        manifest=manifest_snapshot,
        runtime_evidence=runtime_evidence_snapshot,
        cases_dir=cases_dir,
    )
    if _capture_session_envelope_material(envelope_snapshot) != (
        _capture_session_envelope_material(rebuilt)
    ):
        raise ValueError(
            "capture session envelope content or preflight binding was tampered"
        )
    if (
        envelope_snapshot.get("sessionId") is not None
        or envelope_snapshot.get("consumed") is not False
        or envelope_snapshot.get("status") != "prepared"
        or envelope_snapshot.get("authorizationStatus")
        != "awaiting_explicit_authorization"
        or envelope_snapshot.get("captureScope") != "place_only"
    ):
        raise ValueError("capture session envelope lifecycle is invalid")
    if (
        envelope_snapshot.get("placeIdentityClosureComplete") is not False
        or envelope_snapshot.get("routePairFreezeEligible") is not False
        or envelope_snapshot.get("routeCaptureAuthorized") is not False
    ):
        raise ValueError("capture session envelope phase ordering is invalid")
    if envelope_snapshot.get("zeroEffectLedger") != {
        "network": 0,
        "amap": 0,
        "web": 0,
        "controller": 0,
        "capture": 0,
        "version": 0,
        "patch": 0,
        "routeWrite": 0,
    }:
        raise ValueError("capture session envelope zero-effect ledger is invalid")


def require_capture_session_phase(
    *,
    envelope: dict[str, Any],
    manifest: dict[str, Any],
    runtime_evidence: dict[str, Any],
    requested_phase: str,
    cases_dir: Path = CASES_DIR,
) -> None:
    """Validate phase ordering without authorizing or executing any capture."""

    envelope_snapshot = deepcopy(envelope)
    manifest_snapshot = deepcopy(manifest)
    runtime_evidence_snapshot = deepcopy(runtime_evidence)
    validate_zero_network_capture_session_envelope(
        envelope=envelope_snapshot,
        manifest=manifest_snapshot,
        runtime_evidence=runtime_evidence_snapshot,
        cases_dir=cases_dir,
    )
    phase = str(requested_phase or "").strip().lower()
    if phase in {"place_only_prepared", "inspect_place_envelope"}:
        return
    if phase in {"place", "place_capture", "place_identity_capture"}:
        raise ValueError("place_capture_not_authorized")
    if phase.startswith("route") or phase in {
        "freeze_route_pairs",
        "exact_route_pair_mode_manifest",
    }:
        blockers = envelope_snapshot["phaseOrdering"]["routePhaseBlockers"]
        raise ValueError(f"route_phase_blocked:{','.join(blockers)}")
    raise ValueError(f"unsupported_capture_phase:{phase or 'missing'}")


def _capture_budget_receipt_reference(request: dict[str, Any]) -> dict[str, Any]:
    """Project only identities already present in one validated Place request."""

    occurrence = request["profileOccurrence"]
    lineage = request["queryPlanLineage"]
    receipt = request["budgetReceipt"]
    return {
        "endpoint": str(request["endpoint"]),
        "requestFingerprint": str(request["requestFingerprint"]),
        "auditFingerprint": str(request["auditFingerprint"]),
        "profileOccurrenceFingerprint": str(
            occurrence["occurrenceFingerprint"]
        ),
        "queryPlanLineageFingerprint": str(
            receipt["queryPlanLineageFingerprint"]
        ),
        "queryScopeFingerprint": str(lineage["queryScopeFingerprint"]),
        "budgetObjectId": str(receipt["budgetObjectId"]),
        "acquisitionOrdinal": int(receipt["acquisitionOrdinal"]),
        "fetchOrdinal": int(receipt["fetchOrdinal"]),
        "receiptFingerprint": str(receipt["receiptFingerprint"]),
    }


def _capture_session_envelope_material(envelope: dict[str, Any]) -> dict[str, Any]:
    material = deepcopy(envelope)
    material.pop("captureSessionDisplayId", None)
    material.pop("contentFingerprint", None)
    material.pop("envelopeFingerprint", None)
    return material


def _capture_session_envelope_fingerprint(envelope: dict[str, Any]) -> str:
    return canonical_sha256(_capture_session_envelope_material(envelope))


def _case_messages(case: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    observed = [
        str(value)
        for value in trace.get("originalUserMessages") or []
        if str(value or "").strip()
    ]
    if observed:
        return observed
    return [
        str(step.get("content") or "")
        for step in case.get("steps") or []
        if isinstance(step, dict)
        and step.get("action") == "send_agent_message"
        and str(step.get("content") or "").strip()
    ]


def _case_city(
    case: dict[str, Any], trace: dict[str, Any], profile_runs: list[dict[str, Any]]
) -> str:
    for profile in profile_runs:
        if profile["city"]:
            return profile["city"]
    for step in case.get("steps") or []:
        if not isinstance(step, dict) or step.get("action") != "create_session":
            continue
        payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
        city = str(payload.get("city") or "").strip()
        if city:
            return city
    return ""


def _safe_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result >= 0 else 0


def _safe_profile_runs(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if value is None:
        return [], ["profile_runs_missing"]
    if not isinstance(value, list):
        return [], ["profile_runs_invalid"]
    errors: list[str] = []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append("profile_run_invalid")
            continue
        result.append(_safe_profile_run(item))
    return result, errors


def _safe_profile_run(profile: dict[str, Any]) -> dict[str, Any]:
    scope = profile.get("scope") if isinstance(profile.get("scope"), dict) else {}
    semantic_role = (
        profile.get("semanticRole") if isinstance(profile.get("semanticRole"), dict) else {}
    )
    source_evidence = profile.get("sourceEvidence") if isinstance(profile.get("sourceEvidence"), dict) else {}
    return {
        "profileId": str(profile.get("profileId") or ""),
        "profileFingerprint": str(profile.get("profileFingerprint") or ""),
        "executionFingerprint": str(profile.get("executionFingerprint") or ""),
        "occurrenceFingerprint": str(profile.get("occurrenceFingerprint") or ""),
        "scope": {
            "briefId": str(scope.get("briefId") or ""),
            "poolId": str(scope.get("poolId") or ""),
            "planningSlotId": str(scope.get("planningSlotId") or ""),
            "dayNumber": _safe_nonnegative_int(scope.get("dayNumber")),
        },
        "semanticRole": {
            "experienceFamily": str(semantic_role.get("experienceFamily") or ""),
            "intentType": str(semantic_role.get("intentType") or ""),
            "requirementLevel": str(semantic_role.get("requirementLevel") or ""),
            "entityBindingMode": str(semantic_role.get("entityBindingMode") or ""),
        },
        "city": str(profile.get("city") or ""),
        "sourceEvidence": {
            "source": str(source_evidence.get("source") or ""),
            "candidateHintsUsedAsSemanticEvidence": bool(
                source_evidence.get("candidateHintsUsedAsSemanticEvidence")
            ),
        },
        "queryPlans": [
            {
                "planId": str(item.get("planId") or ""),
                "sourcePlanId": str(item.get("sourcePlanId") or ""),
                "sourcePlanFingerprint": str(
                    item.get("sourcePlanFingerprint") or ""
                ),
                "providerPlanFingerprint": str(
                    item.get("providerPlanFingerprint") or ""
                ),
                "endpoint": str(item.get("endpoint") or ""),
                "mode": str(item.get("mode") or ""),
                "city": str(item.get("city") or ""),
                "keyword": str(item.get("keyword") or ""),
                "category": str(item.get("category") or ""),
                "providerCategoryKey": str(item.get("providerCategoryKey") or ""),
                "anchorPolicy": str(item.get("anchorPolicy") or ""),
                "radiusMeters": _safe_nonnegative_int(item.get("radiusMeters")),
                "resultLimit": _safe_nonnegative_int(item.get("resultLimit")),
            }
            for item in profile.get("queryPlans") or []
            if isinstance(item, dict)
        ],
        "budget": dict(profile.get("budget")) if isinstance(profile.get("budget"), dict) else {},
    }


def _safe_logical_slots(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], ["logical_slots_missing_or_invalid"]
    errors: list[str] = []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append("logical_slot_invalid")
            continue
        result.append(_safe_logical_slot(item))
    return result, errors


def _safe_logical_slot(slot: dict[str, Any]) -> dict[str, Any]:
    return {
        "briefId": str(slot.get("briefId") or ""),
        "dayNumber": _safe_nonnegative_int(slot.get("dayNumber")),
        "planningSlotId": str(slot.get("planningSlotId") or ""),
        "semanticRole": str(slot.get("semanticRole") or ""),
        "experienceFamily": str(slot.get("experienceFamily") or ""),
        "intentType": str(slot.get("intentType") or ""),
        "poolId": str(slot.get("poolId") or ""),
        "routeAnchor": bool(slot.get("routeAnchor")),
        "profileId": str(slot.get("profileId") or ""),
        "profileBindingStatus": str(slot.get("profileBindingStatus") or ""),
    }


def _safe_logical_edges(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(value, list):
        return [], ["logical_route_topology_missing_or_invalid"]
    errors: list[str] = []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            errors.append("logical_route_edge_invalid")
            continue
        result.append(_safe_logical_edge(item))
    return result, errors


def _safe_logical_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "briefId": str(edge.get("briefId") or ""),
        "dayNumber": _safe_nonnegative_int(edge.get("dayNumber")),
        "fromLogicalNode": str(edge.get("fromLogicalNode") or ""),
        "toLogicalNode": str(edge.get("toLogicalNode") or ""),
        "mode": str(edge.get("mode") or ""),
        "modeReason": str(edge.get("modeReason") or ""),
    }


def _safe_route_authorization(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "status": str(raw.get("status") or "missing"),
        "preferredMode": str(raw.get("preferredMode") or ""),
        "modeSource": str(raw.get("modeSource") or ""),
        "contractFingerprint": str(raw.get("contractFingerprint") or ""),
        "budgetState": str(raw.get("budgetState") or "not_derived"),
        "budgetReason": str(raw.get("budgetReason") or ""),
    }


def _safe_observed_amap_adcode(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    adcode = str(raw.get("adcode") or "")
    source = str(raw.get("source") or "")
    request_count = _safe_nonnegative_int(raw.get("observedRequestCount"))
    status = str(raw.get("status") or "not_observed")
    if (
        status == "observed"
        and re.fullmatch(r"\d{6}", adcode)
        and source == "server_observed_amap_request_param"
        and request_count > 0
    ):
        return {
            "adcode": adcode,
            "source": source,
            "observedRequestCount": request_count,
            "status": status,
        }
    return {
        "adcode": "",
        "source": "not_observed",
        "observedRequestCount": request_count,
        "status": status,
    }


def _has_complete_profile_scope(profile: dict[str, Any]) -> bool:
    scope = profile.get("scope") if isinstance(profile.get("scope"), dict) else {}
    return bool(
        str(profile.get("profileId") or "")
        and str(profile.get("profileFingerprint") or "")
        and str(profile.get("executionFingerprint") or "")
        and _is_sha256(profile.get("occurrenceFingerprint"))
        and str(scope.get("briefId") or "")
        and str(scope.get("poolId") or "")
        and str(scope.get("planningSlotId") or "")
        and _safe_nonnegative_int(scope.get("dayNumber")) > 0
        and isinstance(profile.get("queryPlans"), list)
        and profile.get("queryPlans")
        and all(
            _is_sha256(plan.get("sourcePlanFingerprint"))
            and _is_sha256(plan.get("providerPlanFingerprint"))
            for plan in profile.get("queryPlans") or []
            if isinstance(plan, dict)
        )
        and isinstance(profile.get("sourceEvidence"), dict)
        and str((profile.get("sourceEvidence") or {}).get("source") or "")
    )


def _has_complete_logical_slot(slot: dict[str, Any]) -> bool:
    return bool(
        str(slot.get("briefId") or "")
        and str(slot.get("poolId") or "")
        and str(slot.get("planningSlotId") or "")
        and _safe_nonnegative_int(slot.get("dayNumber")) > 0
        and str(slot.get("semanticRole") or "")
        and str(slot.get("experienceFamily") or "")
        and str(slot.get("intentType") or "")
        and str(slot.get("profileId") or "")
        and str(slot.get("profileBindingStatus") or "") == "matched"
    )


def _has_complete_logical_edge(edge: dict[str, Any]) -> bool:
    return bool(
        str(edge.get("briefId") or "")
        and _safe_nonnegative_int(edge.get("dayNumber")) > 0
        and str(edge.get("fromLogicalNode") or "")
        and str(edge.get("toLogicalNode") or "")
        and str(edge.get("modeReason") or "") == "server_initial_plan_route_anchor_adjacency"
        and not str(edge.get("mode") or "")
    )


def _capture_preflight_blockers(
    *,
    trace: dict[str, Any],
    profile_status: str,
    profile_runs: list[dict[str, Any]],
    profile_run_errors: list[str],
    logical_slots: list[dict[str, Any]],
    logical_slot_errors: list[str],
    logical_edges: list[dict[str, Any]],
    logical_edge_errors: list[str],
    route_discovery: Any,
) -> list[str]:
    """Return all fail-closed preflight blockers without inferring missing facts."""

    blockers: list[str] = []
    if profile_status != "executed" or not profile_runs:
        blockers.append("missing_production_search_profile")
    if profile_run_errors or not all(_has_complete_profile_scope(item) for item in profile_runs):
        blockers.append("production_search_profile_scope_invalid")
    if str(trace.get("logicalPlanSource") or "") != "persisted_server_initial_plan":
        blockers.append("persisted_server_initial_plan_missing_or_invalid")
    if str(trace.get("persistedInitialPlanStatus") or "") != "observed":
        blockers.append("persisted_server_initial_plan_not_observed")
    if not logical_slots or logical_slot_errors or not all(_has_complete_logical_slot(item) for item in logical_slots):
        blockers.append("logical_slot_scope_incomplete")
    if not logical_edges or logical_edge_errors or not all(_has_complete_logical_edge(item) for item in logical_edges):
        blockers.append("logical_route_topology_incomplete")
    if trace.get("logicalRouteTopologyErrors") or trace.get("profileScopeIntegrityErrors") or trace.get("semanticTraceErrors"):
        blockers.append("semantic_trace_integrity_invalid")
    if bool(trace.get("legacyMockAmapPresent")):
        blockers.append("offline_mock_amap_poi_provenance")
    if not _case_city({}, trace, profile_runs):
        blockers.append("capture_city_not_observed")
    observed_adcode = _safe_observed_amap_adcode(trace.get("observedAmapAdcode"))
    if observed_adcode["status"] != "observed":
        blockers.append("capture_adcode_not_observed")
    authorization = _safe_route_authorization(trace.get("logicalRouteAuthorization"))
    if authorization["status"] != "ready" or not authorization["preferredMode"]:
        blockers.append("route_authorization_not_ready")
    if not isinstance(route_discovery, dict):
        blockers.append("dry_route_discovery_not_observed")
    else:
        if str(route_discovery.get("mode") or "") != "dry_route_discovery":
            blockers.append("dry_route_discovery_not_observed")
        network_calls = route_discovery.get("networkCalls")
        if not _is_exact_nonnegative_int(network_calls):
            blockers.append("dry_route_discovery_network_ledger_missing_or_invalid")
        elif network_calls != 0:
            blockers.append("dry_route_discovery_network_calls_nonzero")
        blockers.extend(_route_discovery_place_identity_blockers(route_discovery))
    return list(dict.fromkeys(blockers))


def _deferred_route_prerequisites(trace: dict[str, Any]) -> list[str]:
    """List route-stage facts that cannot exist before canonical POI binding."""

    authorization = _safe_route_authorization(trace.get("logicalRouteAuthorization"))
    if authorization["budgetState"] != "derived":
        return ["route_budget_not_derived"]
    return []


def _route_discovery_place_identity_blockers(route_discovery: dict[str, Any]) -> list[str]:
    """Retain upstream provenance failures even if a parallel trace omits them."""

    reasons = {
        str(reason)
        for reason in route_discovery.get("captureBlockedReasons") or []
        if str(reason) in _PLACE_IDENTITY_ROUTE_DISCOVERY_BLOCKERS
    }
    for entry in route_discovery.get("actualRouteRequests") or []:
        if not isinstance(entry, dict):
            continue
        reasons.update(
            str(reason)
            for reason in entry.get("captureBlockedReasons") or []
            if str(reason) in _PLACE_IDENTITY_ROUTE_DISCOVERY_BLOCKERS
        )
    return sorted(reasons)


def _safe_route_budget(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"known": False, "source": "", "reason": "route_budget_not_observed"}
    return {
        "known": bool(value.get("known")),
        "source": ",".join(
            str(item.get("source") or "")
            for item in value.get("scopes") or []
            if isinstance(item, dict) and str(item.get("source") or "")
        ),
        "uniquePairCount": int(value.get("uniquePairCount") or 0),
        "exceeded": bool(value.get("exceeded")),
    }


def _place_budget(profile_runs: list[dict[str, Any]]) -> dict[str, Any]:
    maximum = 0
    queries = 0
    for profile in profile_runs:
        budget = profile.get("budget") if isinstance(profile.get("budget"), dict) else {}
        maximum += int(budget.get("maxAmapCalls") or 0)
        queries += len(profile.get("queryPlans") or [])
    return {
        "known": bool(profile_runs),
        "maxAmapCalls": maximum if profile_runs else None,
        "compiledQueryPlanCount": queries,
        "source": "production_experience_search_profile" if profile_runs else "",
    }


def _transport_summary(edges: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for edge in edges:
        mode = str(edge.get("mode") or "")
        reason = str(edge.get("modeReason") or "")
        key = (mode, reason)
        if not mode or key in seen:
            continue
        seen.add(key)
        result.append({"mode": mode, "reason": reason})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the no-network semantic preflight for recorded AMap capture."
    )
    parser.add_argument("--runtime-evidence", type=Path, required=True)
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        runtime_evidence = json.loads(args.runtime_evidence.read_text(encoding="utf-8"))
        if not isinstance(runtime_evidence, dict):
            raise ValueError("runtime evidence must be a JSON object")
        manifest = build_capture_preflight_manifest(
            runtime_evidence=runtime_evidence,
            cases_dir=args.cases_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "captureState": manifest["captureState"],
                "terminalStatus": manifest["terminalStatus"],
                "externalCaptureSessionConsumed": manifest["externalCaptureSession"]["consumed"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if manifest["captureState"] == "ready_for_place_identity_capture" else 2


if __name__ == "__main__":
    raise SystemExit(main())

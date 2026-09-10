"""Fail-closed Z0 builders for recorded Place identity and Route scope evidence.

The module intentionally consumes only already-sanitized, non-promotable
quarantine material.  It has no transport, credential, filesystem-write, or
Provider capability.  A certificate is useful only when every occurrence has
exactly one admitted canonical AMap identity; uncertainty stays a blocker.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from backend.evals.recorded_fixture_capture import (
    CASES_DIR,
    canonical_sha256,
    validate_capture_preflight_manifest,
    validate_zero_network_capture_session_envelope,
)
from src.services.candidate_provider_evidence_service import (
    CandidateProviderEvidenceService,
)
from src.services.consumer_candidate_admission_service import (
    ConsumerCandidateAdmissionService,
)
from src.services.map_poi_service import MapPoiService
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_AMAP_ID = re.compile(r"^B[0-9A-Z]{8,31}$")
_CANONICAL_BINDING_SCHEMA = "trip-recorded-canonical-identity-binding-v1"
_CANONICAL_CANDIDATE_UNIVERSE_SCHEMA = (
    "trip-recorded-canonical-candidate-universe-v1"
)
_PROVISIONAL_SELECTED_ANCHOR_SCHEMA = (
    "trip-recorded-provisional-selected-anchor-binding-v1"
)
_ROUTE_MANIFEST_SCHEMA = "trip-recorded-exact-route-manifest-v1"
_ROUTE_MODES = {"transit", "walking"}
_MAX_ROUTE_CALLS = 24


class CanonicalBindingError(ValueError):
    """Stable, secret-free failure emitted by the Z0 evidence builders."""


@dataclass(frozen=True)
class _Occurrence:
    ordinal: int
    brief_id: str
    pool_id: str
    planning_slot_id: str
    day_number: int
    start_time: str
    profile_fingerprint: str
    occurrence_fingerprint: str
    query_plan_fingerprint: str
    query_scope_fingerprint: str
    family: str
    intent_type: str
    requirement_level: str
    city: str
    consumer_admission_input: dict[str, Any]


def build_canonical_identity_binding_certificate(
    *,
    place_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> dict[str, Any]:
    """Bind one admitted canonical identity to every frozen Place occurrence.

    ``place_quarantine`` must be a *complete* Place-only bundle produced by the
    existing executor.  The builder deliberately rejects a response with zero
    or multiple POIs instead of selecting a plausible first result.
    """

    quarantine = _object_copy(place_quarantine, "place_quarantine_invalid")
    manifest = _object_copy(preflight_manifest, "preflight_manifest_invalid")
    envelope_snapshot = _object_copy(envelope, "capture_envelope_invalid")
    runtime = _object_copy(runtime_evidence, "runtime_evidence_invalid")
    _validate_preflight_envelope_bindings(
        manifest,
        envelope_snapshot,
        runtime,
        cases_dir=cases_dir,
    )

    allowlist = _validated_allowlist(envelope_snapshot)
    responses = _validated_complete_place_quarantine(quarantine, allowlist)
    occurrences = _derive_occurrences(
        allowlist=allowlist,
        runtime_evidence=runtime,
        manifest=manifest,
    )
    occurrence_evidence = _group_occurrence_evidence(occurrences, responses)

    parser = object.__new__(MapPoiService)
    admission = ConsumerCandidateAdmissionService()
    bound: list[dict[str, Any]] = []
    physical_ids: set[str] = set()
    for evidence_group in occurrence_evidence:
        occurrence = evidence_group[0][0]
        consumer = deepcopy(occurrence.consumer_admission_input)
        admitted: dict[str, dict[str, Any]] = {}
        for request_occurrence, record in evidence_group:
            pois = _validated_place_response_pois(record)
            for poi in pois:
                parsed = MapPoiService._parse_poi(
                    parser,
                    poi,
                    request_occurrence.family or "all",
                )
                amap_id = PoiPhysicalIdentityService.canonical_amap_id(
                    {"amapId": parsed.id, "id": parsed.id}
                )
                if not _AMAP_ID.fullmatch(amap_id):
                    raise CanonicalBindingError("canonical_amap_identity_invalid")
                candidate = _consumer_candidate(parsed, request_occurrence, amap_id)
                report = admission.evaluate(candidate, consumer)
                if report.get("scoreEligible") is not True:
                    continue
                candidate_fingerprint = canonical_sha256(candidate)
                existing = admitted.get(amap_id)
                support = _supporting_place_evidence(request_occurrence, record)
                if existing is not None:
                    if existing["candidateFingerprint"] != candidate_fingerprint:
                        raise CanonicalBindingError("canonical_amap_identity_conflict")
                    existing["supportingEvidence"].append(support)
                    continue
                admitted[amap_id] = {
                    "candidateFingerprint": candidate_fingerprint,
                    "parsed": parsed,
                    "report": report,
                    "occurrence": request_occurrence,
                    "record": record,
                    "supportingEvidence": [support],
                }
        if not admitted:
            raise CanonicalBindingError("consumer_admission_rejected")
        if len(admitted) != 1:
            raise CanonicalBindingError("place_identity_ambiguous_or_missing")
        amap_id, selected = next(iter(admitted.items()))
        if amap_id in physical_ids:
            raise CanonicalBindingError("canonical_amap_identity_duplicate")
        physical_ids.add(amap_id)
        parsed = selected["parsed"]
        report = selected["report"]
        selected_occurrence = selected["occurrence"]
        record = selected["record"]
        admission_fingerprint = canonical_sha256(
            {
                "consumerFingerprint": report.get("consumerFingerprint"),
                "candidateEvidenceFingerprint": report.get("candidateEvidenceFingerprint"),
                "classification": report.get("classification"),
                "scoreEligible": report.get("scoreEligible"),
                "scope": {
                    "briefId": occurrence.brief_id,
                    "poolId": occurrence.pool_id,
                    "planningSlotId": occurrence.planning_slot_id,
                    "dayNumber": occurrence.day_number,
                },
            }
        )
        bound.append(
            {
                "ordinal": len(bound) + 1,
                "occurrenceId": occurrence.occurrence_fingerprint,
                "scope": {
                    "briefId": occurrence.brief_id,
                    "poolId": occurrence.pool_id,
                    "planningSlotId": occurrence.planning_slot_id,
                    "dayNumber": occurrence.day_number,
                    "startTime": occurrence.start_time,
                    "profileFingerprint": occurrence.profile_fingerprint,
                    "queryPlanFingerprint": selected_occurrence.query_plan_fingerprint,
                    "queryScopeFingerprint": selected_occurrence.query_scope_fingerprint,
                },
                "responseOrdinal": record["ordinal"],
                "requestFingerprint": record["requestFingerprint"],
                "auditFingerprint": record["auditFingerprint"],
                "responseSha256": record["responseSha256"],
                "supportingEvidence": selected["supportingEvidence"],
                "canonicalIdentity": {
                    "amapId": amap_id,
                    "longitude": round(float(parsed.longitude), 6),
                    "latitude": round(float(parsed.latitude), 6),
                    "city": str(parsed.city or occurrence.city),
                    "source": "amap-place-search",
                },
                "consumerAdmission": {
                    "schemaVersion": str(report.get("schemaVersion") or ""),
                    "consumerFingerprint": str(report.get("consumerFingerprint") or ""),
                    "candidateEvidenceFingerprint": str(
                        report.get("candidateEvidenceFingerprint") or ""
                    ),
                    "admissionFingerprint": admission_fingerprint,
                },
            }
        )

    certificate = {
        "schemaVersion": _CANONICAL_BINDING_SCHEMA,
        "recordingType": "recorded/non-live",
        "promotable": False,
        "sourceBindings": _source_bindings(
            manifest,
            envelope_snapshot,
            runtime_evidence=runtime,
        ),
        "placeQuarantineBundleFingerprint": str(quarantine.get("bundleFingerprint") or ""),
        "occurrences": bound,
    }
    certificate["certificateFingerprint"] = canonical_sha256(certificate)
    return certificate


def build_canonical_candidate_universe_certificate(
    *,
    place_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> dict[str, Any]:
    """Classify every recorded Place candidate without selecting an identity.

    Place search is a candidate producer.  A category query may legitimately
    admit more than one physical POI; route-conditioned evaluation, not result
    order, decides which identity can enter a proposal.  This certificate keeps
    the current consumer scope and every classification intact while leaving
    final selection explicitly pending.
    """

    quarantine = _object_copy(place_quarantine, "place_quarantine_invalid")
    manifest = _object_copy(preflight_manifest, "preflight_manifest_invalid")
    envelope_snapshot = _object_copy(envelope, "capture_envelope_invalid")
    runtime = _object_copy(runtime_evidence, "runtime_evidence_invalid")
    _validate_preflight_envelope_bindings(
        manifest,
        envelope_snapshot,
        runtime,
        cases_dir=cases_dir,
    )
    allowlist = _validated_allowlist(envelope_snapshot)
    responses = _validated_complete_place_quarantine(quarantine, allowlist)
    occurrences = _derive_occurrences(
        allowlist=allowlist,
        runtime_evidence=runtime,
        manifest=manifest,
    )
    grouped = _group_occurrence_evidence(occurrences, responses)
    parser = object.__new__(MapPoiService)
    admission = ConsumerCandidateAdmissionService()
    occurrence_rows: list[dict[str, Any]] = []
    totals = Counter()

    for evidence_group in grouped:
        occurrence = evidence_group[0][0]
        consumer = deepcopy(occurrence.consumer_admission_input)
        candidates: dict[str, dict[str, Any]] = {}
        for request_occurrence, record in evidence_group:
            for poi in _validated_place_response_pois(record):
                parsed = MapPoiService._parse_poi(
                    parser,
                    poi,
                    request_occurrence.family or "all",
                )
                amap_id = PoiPhysicalIdentityService.canonical_amap_id(
                    {"amapId": parsed.id, "id": parsed.id}
                )
                if not _AMAP_ID.fullmatch(amap_id):
                    raise CanonicalBindingError("canonical_amap_identity_invalid")
                candidate = _consumer_candidate(parsed, request_occurrence, amap_id)
                report = admission.evaluate(candidate, consumer)
                classification = str(report.get("classification") or "rejected")
                disposition_by_classification = {
                    "admitted_final_anchor": "admitted",
                    "admitted_anchor_set_member": "admitted",
                    "pending_evidence": "pending",
                    "area_seed_only": "pending",
                    "rejected": "rejected",
                }
                disposition = disposition_by_classification.get(classification)
                if disposition is None:
                    raise CanonicalBindingError("consumer_admission_classification_invalid")
                if (disposition == "admitted") != bool(report.get("scoreEligible")):
                    raise CanonicalBindingError("consumer_admission_classification_invalid")
                candidate_fingerprint = canonical_sha256(candidate)
                report_projection = _candidate_admission_report_projection(report)
                report_fingerprint = canonical_sha256(report_projection)
                support = _supporting_place_evidence(request_occurrence, record)
                existing = candidates.get(amap_id)
                if existing is not None:
                    if existing["candidateFingerprint"] != candidate_fingerprint:
                        raise CanonicalBindingError("canonical_amap_identity_conflict")
                    if existing["consumerAdmission"]["reportFingerprint"] != report_fingerprint:
                        raise CanonicalBindingError("consumer_admission_report_conflict")
                    existing["supportingEvidence"].append(support)
                    continue
                candidates[amap_id] = {
                    "candidateFingerprint": candidate_fingerprint,
                    "candidatePayload": deepcopy(candidate),
                    "canonicalIdentity": {
                        "amapId": amap_id,
                        "longitude": round(float(parsed.longitude), 6),
                        "latitude": round(float(parsed.latitude), 6),
                        "city": str(parsed.city or occurrence.city),
                        "source": "amap-place-search",
                    },
                    "classification": classification,
                    "admissionDisposition": disposition,
                    "scoreEligible": bool(report.get("scoreEligible")),
                    "reasonCodes": report_projection["reasonCodes"],
                    "consumerAdmission": {
                        "schemaVersion": str(report.get("schemaVersion") or ""),
                        "consumerFingerprint": str(
                            report.get("consumerFingerprint") or ""
                        ),
                        "candidateEvidenceFingerprint": str(
                            report.get("candidateEvidenceFingerprint") or ""
                        ),
                        "reportFingerprint": report_fingerprint,
                    },
                    "supportingEvidence": [support],
                }
        projected = [candidates[key] for key in sorted(candidates)]
        counts = Counter(item["admissionDisposition"] for item in projected)
        totals.update(counts)
        occurrence_rows.append(
            {
                "occurrenceId": occurrence.occurrence_fingerprint,
                "scope": {
                    "briefId": occurrence.brief_id,
                    "poolId": occurrence.pool_id,
                    "planningSlotId": occurrence.planning_slot_id,
                    "dayNumber": occurrence.day_number,
                    "startTime": occurrence.start_time,
                    "profileFingerprint": occurrence.profile_fingerprint,
                },
                "semanticRole": {
                    "experienceFamily": occurrence.family,
                    "intentType": occurrence.intent_type,
                    "requirementLevel": occurrence.requirement_level,
                },
                "consumerAdmissionInput": deepcopy(consumer),
                "consumerAdmissionInputFingerprint": canonical_sha256(consumer),
                "candidateCounts": {
                    "total": len(projected),
                    "admitted": counts["admitted"],
                    "pending": counts["pending"],
                    "rejected": counts["rejected"],
                },
                "selectionStatus": "pending_exact_route_matrix",
                "candidates": projected,
            }
        )

    certificate = {
        "schemaVersion": _CANONICAL_CANDIDATE_UNIVERSE_SCHEMA,
        "recordingType": "recorded/non-live",
        "promotable": False,
        "sourceBindings": _source_bindings(
            manifest,
            envelope_snapshot,
            runtime_evidence=runtime,
        ),
        "placeQuarantineBundleFingerprint": str(
            quarantine.get("bundleFingerprint") or ""
        ),
        "occurrenceCount": len(occurrence_rows),
        "candidateCounts": {
            "admitted": totals["admitted"],
            "pending": totals["pending"],
            "rejected": totals["rejected"],
        },
        "finalIdentitySelectionComplete": False,
        "routeMatrixRequired": True,
        "occurrences": occurrence_rows,
    }
    certificate["certificateFingerprint"] = canonical_sha256(certificate)
    return certificate


def canonical_candidate_universe_pool_reports(
    certificate: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project immutable raw facts into the existing shared-universe input.

    Consumer authorization remains occurrence-local evidence in the
    certificate.  It is deliberately absent from the projected candidate so
    the staging consumer must run Admission again for its exact brief/slot.
    """

    value = _object_copy(certificate, "canonical_candidate_universe_invalid")
    if value.get("schemaVersion") != _CANONICAL_CANDIDATE_UNIVERSE_SCHEMA:
        raise CanonicalBindingError("canonical_candidate_universe_schema_invalid")
    fingerprint = value.get("certificateFingerprint")
    material = deepcopy(value)
    material.pop("certificateFingerprint", None)
    if not _is_hex(fingerprint) or canonical_sha256(material) != fingerprint:
        raise CanonicalBindingError("canonical_candidate_universe_fingerprint_invalid")
    reports: list[dict[str, Any]] = []
    for occurrence in value.get("occurrences") or []:
        if not isinstance(occurrence, dict) or not isinstance(
            occurrence.get("scope"), dict
        ):
            raise CanonicalBindingError("canonical_candidate_universe_occurrence_invalid")
        scope = occurrence["scope"]
        candidates = occurrence.get("candidates")
        if not isinstance(candidates, list):
            raise CanonicalBindingError("canonical_candidate_universe_occurrence_invalid")
        source_candidates = []
        for row in candidates:
            payload = row.get("candidatePayload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                raise CanonicalBindingError("canonical_candidate_universe_candidate_invalid")
            projected = deepcopy(payload)
            for field in (
                "consumerAdmission",
                "consumerAdmissionInput",
                "consumerAdmissionReport",
                "scoreEligible",
            ):
                projected.pop(field, None)
            projected["sourceEvidenceEligible"] = True
            source_candidates.append(projected)
        reports.append(
            {
                "briefId": str(scope.get("briefId") or ""),
                "poolId": str(scope.get("poolId") or ""),
                "intentType": str(
                    (occurrence.get("semanticRole") or {}).get("intentType") or ""
                ),
                "requiredSlotIds": [str(scope.get("planningSlotId") or "")],
                "slotDayNumbers": {
                    str(scope.get("planningSlotId") or ""): scope.get("dayNumber")
                },
                "sourceCandidates": source_candidates,
            }
        )
    return reports


def validate_canonical_candidate_universe_certificate(
    *,
    certificate: dict[str, Any],
    place_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> None:
    expected = build_canonical_candidate_universe_certificate(
        place_quarantine=place_quarantine,
        preflight_manifest=preflight_manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    if _object_copy(certificate, "canonical_candidate_universe_invalid") != expected:
        raise CanonicalBindingError("canonical_candidate_universe_tampered")


def build_provisional_staged_anchor_binding_certificate(
    *,
    place_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    staged_itinerary_snapshot: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> dict[str, Any]:
    """Bind only route anchors selected by the production staging path.

    The Place candidate universe remains non-selecting.  This bridge accepts a
    staged snapshot only after matching each route-anchor segment to one exact
    occurrence and re-running Consumer Admission from the sealed input.  A
    stale report, cross-slot identity, or non-admitted candidate fails before a
    Route manifest can be issued.  The result is intentionally provisional:
    the exact Provider matrix, not staging order, owns final identity selection.
    """

    universe = build_canonical_candidate_universe_certificate(
        place_quarantine=place_quarantine,
        preflight_manifest=preflight_manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    snapshot = _object_copy(
        staged_itinerary_snapshot,
        "staged_itinerary_snapshot_invalid",
    )
    occurrences_by_scope: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for occurrence in universe["occurrences"]:
        scope = occurrence["scope"]
        key = (
            str(scope.get("briefId") or ""),
            str(scope.get("poolId") or ""),
            str(scope.get("planningSlotId") or ""),
            int(scope.get("dayNumber") or 0),
        )
        if not all(key) or key in occurrences_by_scope:
            raise CanonicalBindingError("canonical_candidate_universe_scope_invalid")
        occurrences_by_scope[key] = occurrence

    selected_by_scope: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for day in snapshot.get("days") or []:
        if not isinstance(day, dict):
            raise CanonicalBindingError("staged_route_anchor_shape_invalid")
        try:
            day_number = int(day.get("dayNumber") or 0)
        except (TypeError, ValueError):
            raise CanonicalBindingError("staged_route_anchor_scope_invalid") from None
        for segment in day.get("segments") or []:
            if not isinstance(segment, dict):
                raise CanonicalBindingError("staged_route_anchor_shape_invalid")
            semantic = (
                segment.get("semanticMetadata")
                if isinstance(segment.get("semanticMetadata"), dict)
                else {}
            )
            if semantic.get("routeAnchor") is not True:
                continue
            key = (
                str(semantic.get("creativeBriefId") or ""),
                str(semantic.get("poolId") or ""),
                str(
                    semantic.get("planningSlotId")
                    or semantic.get("slotId")
                    or ""
                ),
                day_number,
            )
            if not all(key) or key in selected_by_scope:
                raise CanonicalBindingError("staged_route_anchor_scope_invalid")
            selected_by_scope[key] = segment
    if not selected_by_scope:
        raise CanonicalBindingError("staged_route_anchor_missing")

    admission = ConsumerCandidateAdmissionService()
    selected_physical_ids: set[str] = set()
    bound: list[dict[str, Any]] = []
    selection_projection: list[dict[str, Any]] = []
    for key, segment in sorted(
        selected_by_scope.items(),
        key=lambda item: (
            item[0][3],
            str((item[1].get("semanticMetadata") or {}).get("startTime") or ""),
            item[0],
        ),
    ):
        occurrence = occurrences_by_scope.get(key)
        if occurrence is None:
            raise CanonicalBindingError("staged_route_anchor_scope_mismatch")
        semantic = segment["semanticMetadata"]
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else None
        if not isinstance(poi, dict):
            raise CanonicalBindingError("staged_route_anchor_identity_invalid")
        amap_id = PoiPhysicalIdentityService.canonical_amap_id(poi)
        if not _AMAP_ID.fullmatch(amap_id) or amap_id in selected_physical_ids:
            raise CanonicalBindingError("staged_route_anchor_identity_invalid")
        candidates = [
            item
            for item in occurrence["candidates"]
            if item["canonicalIdentity"]["amapId"] == amap_id
        ]
        if len(candidates) != 1:
            raise CanonicalBindingError("staged_route_anchor_identity_mismatch")
        selected_candidate = candidates[0]
        consumer_input = occurrence.get("consumerAdmissionInput")
        if (
            not isinstance(consumer_input, dict)
            or canonical_sha256(consumer_input)
            != occurrence.get("consumerAdmissionInputFingerprint")
            or semantic.get("consumerAdmissionInput") != consumer_input
        ):
            raise CanonicalBindingError("staged_consumer_admission_input_stale")
        scope = occurrence["scope"]
        alternatives: list[dict[str, Any]] = []
        for candidate in sorted(
            occurrence["candidates"],
            key=lambda item: (
                str((item.get("canonicalIdentity") or {}).get("amapId") or ""),
                str(item.get("candidateFingerprint") or ""),
            ),
        ):
            if (
                candidate.get("admissionDisposition") != "admitted"
                or candidate.get("scoreEligible") is not True
            ):
                continue
            candidate_payload = candidate.get("candidatePayload")
            if (
                not isinstance(candidate_payload, dict)
                or canonical_sha256(candidate_payload)
                != candidate.get("candidateFingerprint")
                or str(candidate_payload.get("source") or "")
                != "amap-place-search"
            ):
                raise CanonicalBindingError("staged_route_anchor_candidate_tampered")
            identity = candidate.get("canonicalIdentity")
            candidate_amap_id = (
                str(identity.get("amapId") or "")
                if isinstance(identity, dict)
                else ""
            )
            if (
                not _AMAP_ID.fullmatch(candidate_amap_id)
                or PoiPhysicalIdentityService.canonical_amap_id(candidate_payload)
                != candidate_amap_id
            ):
                raise CanonicalBindingError("staged_route_anchor_identity_mismatch")
            fresh_candidate_report = admission.evaluate(
                deepcopy(candidate_payload), deepcopy(consumer_input)
            )
            if (
                fresh_candidate_report.get("scoreEligible") is not True
                or fresh_candidate_report.get("consumerFingerprint")
                != candidate["consumerAdmission"]["consumerFingerprint"]
                or fresh_candidate_report.get("candidateEvidenceFingerprint")
                != candidate["consumerAdmission"]["candidateEvidenceFingerprint"]
                or fresh_candidate_report.get("classification")
                != candidate.get("classification")
            ):
                raise CanonicalBindingError("staged_consumer_admission_rejected")
            supports = sorted(
                (deepcopy(item) for item in candidate.get("supportingEvidence") or []),
                key=lambda item: int(item.get("ordinal") or 0),
            )
            if not supports:
                raise CanonicalBindingError("staged_route_anchor_evidence_missing")
            primary = supports[0]
            admission_fingerprint = canonical_sha256(
                {
                    "consumerFingerprint": fresh_candidate_report.get(
                        "consumerFingerprint"
                    ),
                    "candidateEvidenceFingerprint": fresh_candidate_report.get(
                        "candidateEvidenceFingerprint"
                    ),
                    "classification": fresh_candidate_report.get("classification"),
                    "scoreEligible": fresh_candidate_report.get("scoreEligible"),
                    "scope": {
                        "briefId": key[0],
                        "poolId": key[1],
                        "planningSlotId": key[2],
                        "dayNumber": key[3],
                    },
                }
            )
            alternatives.append(
                {
                    "responseOrdinal": primary["ordinal"],
                    "requestFingerprint": primary["requestFingerprint"],
                    "auditFingerprint": primary["auditFingerprint"],
                    "responseSha256": primary["responseSha256"],
                    "supportingEvidence": supports,
                    "candidateFingerprint": candidate["candidateFingerprint"],
                    "canonicalIdentity": deepcopy(identity),
                    "consumerAdmission": {
                        "schemaVersion": str(
                            fresh_candidate_report.get("schemaVersion") or ""
                        ),
                        "consumerFingerprint": str(
                            fresh_candidate_report.get("consumerFingerprint") or ""
                        ),
                        "candidateEvidenceFingerprint": str(
                            fresh_candidate_report.get("candidateEvidenceFingerprint")
                            or ""
                        ),
                        "admissionFingerprint": admission_fingerprint,
                        "reportFingerprint": str(
                            fresh_candidate_report.get("reportFingerprint") or ""
                        ),
                    },
                }
            )
        if not alternatives:
            raise CanonicalBindingError("staged_route_anchor_not_admitted")
        selected_alternatives = [
            item
            for item in alternatives
            if item["canonicalIdentity"]["amapId"] == amap_id
        ]
        if len(selected_alternatives) != 1:
            raise CanonicalBindingError("staged_route_anchor_identity_mismatch")
        selected_alternative = selected_alternatives[0]
        fresh_report = admission.evaluate(deepcopy(poi), deepcopy(consumer_input))
        if (
            fresh_report.get("scoreEligible") is not True
            or fresh_report.get("consumerFingerprint")
            != selected_alternative["consumerAdmission"]["consumerFingerprint"]
            or fresh_report.get("candidateEvidenceFingerprint")
            != selected_alternative["consumerAdmission"][
                "candidateEvidenceFingerprint"
            ]
        ):
            raise CanonicalBindingError("staged_consumer_admission_rejected")
        staged_report = semantic.get("consumerAdmissionReport")
        if (
            not isinstance(staged_report, dict)
            or not ConsumerCandidateAdmissionService.validate_report(staged_report)
            or staged_report.get("scoreEligible") is not True
            or staged_report.get("consumerFingerprint")
            != fresh_report.get("consumerFingerprint")
            or staged_report.get("candidateEvidenceFingerprint")
            != fresh_report.get("candidateEvidenceFingerprint")
            or staged_report.get("reportFingerprint")
            != fresh_report.get("reportFingerprint")
            or staged_report.get("classification")
            != fresh_report.get("classification")
        ):
            raise CanonicalBindingError("staged_consumer_admission_stale")
        bound.append(
            {
                "ordinal": len(bound) + 1,
                "occurrenceId": occurrence["occurrenceId"],
                "scope": {
                    **deepcopy(scope),
                    "queryPlanFingerprint": selected_alternative[
                        "supportingEvidence"
                    ][0]["queryPlanFingerprint"],
                    "queryScopeFingerprint": selected_alternative[
                        "supportingEvidence"
                    ][0]["queryScopeFingerprint"],
                },
                "responseOrdinal": selected_alternative["responseOrdinal"],
                "requestFingerprint": selected_alternative["requestFingerprint"],
                "auditFingerprint": selected_alternative["auditFingerprint"],
                "responseSha256": selected_alternative["responseSha256"],
                "supportingEvidence": deepcopy(
                    selected_alternative["supportingEvidence"]
                ),
                "canonicalIdentity": deepcopy(
                    selected_alternative["canonicalIdentity"]
                ),
                "consumerAdmission": deepcopy(
                    selected_alternative["consumerAdmission"]
                ),
                "candidateAlternatives": alternatives,
            }
        )
        selected_physical_ids.add(amap_id)
        selection_projection.append(
            {
                "occurrenceId": occurrence["occurrenceId"],
                "scope": {
                    "briefId": key[0],
                    "poolId": key[1],
                    "planningSlotId": key[2],
                    "dayNumber": key[3],
                },
                "amapId": amap_id,
                "candidateFingerprint": selected_candidate["candidateFingerprint"],
                "consumerAdmissionReportFingerprint": selected_candidate[
                    "consumerAdmission"
                ]["reportFingerprint"],
                "stagedConsumerAdmissionReportFingerprint": str(
                    fresh_report.get("reportFingerprint") or ""
                ),
            }
        )

    certificate = {
        "schemaVersion": _PROVISIONAL_SELECTED_ANCHOR_SCHEMA,
        "recordingType": "recorded/non-live",
        "promotable": False,
        "finalIdentitySelectionComplete": False,
        "routeMatrixRequired": True,
        "sourceBindings": deepcopy(universe["sourceBindings"]),
        "placeQuarantineBundleFingerprint": universe[
            "placeQuarantineBundleFingerprint"
        ],
        "candidateUniverseCertificateFingerprint": universe[
            "certificateFingerprint"
        ],
        "stagedSelectionFingerprint": canonical_sha256(selection_projection),
        "occurrences": bound,
    }
    certificate["certificateFingerprint"] = canonical_sha256(certificate)
    _validate_provisional_selected_anchor_shape(certificate)
    return certificate


def validate_provisional_staged_anchor_binding_certificate(
    *,
    certificate: dict[str, Any],
    place_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    staged_itinerary_snapshot: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> None:
    expected = build_provisional_staged_anchor_binding_certificate(
        place_quarantine=place_quarantine,
        preflight_manifest=preflight_manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        staged_itinerary_snapshot=staged_itinerary_snapshot,
        cases_dir=cases_dir,
    )
    if _object_copy(certificate, "canonical_identity_certificate_invalid") != expected:
        raise CanonicalBindingError("provisional_selected_anchor_certificate_tampered")


def _group_occurrence_evidence(
    occurrences: list[_Occurrence],
    responses: list[dict[str, Any]],
) -> list[list[tuple[_Occurrence, dict[str, Any]]]]:
    if len(occurrences) != len(responses):
        raise CanonicalBindingError("place_occurrence_response_count_mismatch")
    grouped: dict[str, list[tuple[_Occurrence, dict[str, Any]]]] = {}
    signatures: dict[str, tuple[Any, ...]] = {}
    for occurrence, record in zip(occurrences, responses):
        if occurrence.ordinal != record["ordinal"]:
            raise CanonicalBindingError("place_response_ordinal_mismatch")
        signature = (
            occurrence.brief_id,
            occurrence.pool_id,
            occurrence.planning_slot_id,
            occurrence.day_number,
            occurrence.start_time,
            occurrence.profile_fingerprint,
            occurrence.family,
            occurrence.intent_type,
            occurrence.requirement_level,
            occurrence.city,
        )
        existing = signatures.get(occurrence.occurrence_fingerprint)
        if existing is not None and existing != signature:
            raise CanonicalBindingError("place_request_occurrence_conflicting_duplicate")
        signatures[occurrence.occurrence_fingerprint] = signature
        grouped.setdefault(occurrence.occurrence_fingerprint, []).append((occurrence, record))
    return list(grouped.values())


def _candidate_admission_report_projection(
    report: dict[str, Any],
) -> dict[str, Any]:
    """Keep only stable, decision-bearing Consumer Admission fields."""

    return {
        "schemaVersion": str(report.get("schemaVersion") or ""),
        "classification": str(report.get("classification") or "rejected"),
        "hardGatePassed": bool(report.get("hardGatePassed")),
        "evidenceSufficient": bool(report.get("evidenceSufficient")),
        "scoreEligible": bool(report.get("scoreEligible")),
        "consumerFingerprint": str(report.get("consumerFingerprint") or ""),
        "candidateEvidenceFingerprint": str(
            report.get("candidateEvidenceFingerprint") or ""
        ),
        "reasonCodes": sorted(
            {str(item) for item in report.get("reasonCodes") or [] if str(item)}
        ),
    }


def _validated_place_response_pois(record: dict[str, Any]) -> list[dict[str, Any]]:
    response = record.get("response")
    pois = response.get("pois") if isinstance(response, dict) else None
    if (
        not isinstance(pois, list)
        or not pois
        or any(not isinstance(poi, dict) for poi in pois)
    ):
        raise CanonicalBindingError("place_identity_ambiguous_or_missing")
    return pois


def _consumer_candidate(parsed: Any, occurrence: _Occurrence, amap_id: str) -> dict[str, Any]:
    return {
        "id": amap_id,
        "amapId": amap_id,
        "name": parsed.name,
        "type": parsed.type,
        # SharedCandidateUniverseBuilder requires the provider taxonomy under
        # ``providerType`` and preserves ``type`` for the persisted POI shape.
        # Emit both aliases at the recorded-evidence boundary so a fresh
        # consumer admission sees the same canonical provider facts before and
        # after production staging.
        "providerType": parsed.type,
        "category": parsed.category,
        "providerTypeCode": parsed.provider_type_code,
        "tags": list(parsed.tags or []),
        "businessArea": parsed.business_area,
        "city": parsed.city or occurrence.city,
        "district": getattr(parsed, "district", None),
        "address": parsed.address,
        "longitude": parsed.longitude,
        "latitude": parsed.latitude,
        "rating": getattr(parsed, "rating", None),
        "cost": getattr(parsed, "cost", None),
        "openTimeToday": getattr(parsed, "open_time_today", None),
        "openTimeWeek": getattr(parsed, "open_time_week", None),
        "parentPoiId": getattr(parsed, "parent_poi_id", None),
        "children": CandidateProviderEvidenceService.project(
            {"children": getattr(parsed, "children", None) or []},
            include_missing=False,
        ).get("children", []),
        "source": "amap-place-search",
        "briefId": occurrence.brief_id,
        "poolId": occurrence.pool_id,
        "planningSlotId": occurrence.planning_slot_id,
        "dayNumber": occurrence.day_number,
        "sourcePrecheck": "recorded_place_quarantine",
    }


def _supporting_place_evidence(
    occurrence: _Occurrence,
    record: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ordinal": occurrence.ordinal,
        "requestFingerprint": record["requestFingerprint"],
        "auditFingerprint": record["auditFingerprint"],
        "responseSha256": record["responseSha256"],
        "queryPlanFingerprint": occurrence.query_plan_fingerprint,
        "queryScopeFingerprint": occurrence.query_scope_fingerprint,
    }


def validate_canonical_identity_binding_certificate(
    *,
    certificate: dict[str, Any],
    place_quarantine: dict[str, Any],
    preflight_manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime_evidence: dict[str, Any],
    cases_dir: Path = CASES_DIR,
) -> None:
    expected = build_canonical_identity_binding_certificate(
        place_quarantine=place_quarantine,
        preflight_manifest=preflight_manifest,
        envelope=envelope,
        runtime_evidence=runtime_evidence,
        cases_dir=cases_dir,
    )
    if _object_copy(certificate, "canonical_identity_certificate_invalid") != expected:
        raise CanonicalBindingError("canonical_identity_certificate_tampered")


def build_exact_route_pair_mode_manifest(
    *,
    anchor_binding_certificate: dict[str, Any],
    dry_route_trace: dict[str, Any],
) -> dict[str, Any]:
    """Derive an ordered, exact transit-first route request multiset.

    The only candidates are identities already sealed in the binding
    certificate.  Repeated physical pairs in distinct logical scopes remain
    distinct requests, while an exactly identical scope is rejected as a
    duplicate instead of silently widening a lease.
    """

    certificate = _object_copy(anchor_binding_certificate, "anchor_binding_certificate_invalid")
    trace = _object_copy(dry_route_trace, "dry_route_trace_invalid")
    certificate_schema = str(certificate.get("schemaVersion") or "")
    if certificate_schema == _CANONICAL_BINDING_SCHEMA:
        _validate_certificate_shape(certificate)
        final_identity_selection_complete = True
    elif certificate_schema == _PROVISIONAL_SELECTED_ANCHOR_SCHEMA:
        _validate_provisional_selected_anchor_shape(certificate)
        final_identity_selection_complete = False
    else:
        raise CanonicalBindingError("route_anchor_binding_schema_invalid")
    bindings = certificate["sourceBindings"]
    _validate_production_issued_dry_trace(
        trace=trace,
        bindings=bindings,
        certificate_fingerprint=certificate["certificateFingerprint"],
    )
    route_contract = str(trace.get("routeContractFingerprint") or "")
    if not _is_hex(route_contract) or route_contract != bindings["routeContractFingerprint"]:
        raise CanonicalBindingError("route_contract_binding_invalid")
    root = trace.get("planningRoot")
    if not isinstance(root, dict) or set(root) != {"rootPortfolioId", "planningRoot", "briefId"}:
        raise CanonicalBindingError("route_planning_root_invalid")
    if not all(isinstance(value, str) and value for value in root.values()):
        raise CanonicalBindingError("route_planning_root_invalid")

    occurrences_by_id = {
        occurrence["occurrenceId"]: occurrence for occurrence in certificate["occurrences"]
    }
    topology = _validated_exact_route_topology(
        trace=trace,
        occurrences_by_id=occurrences_by_id,
        route_contract=route_contract,
        root=root,
    )
    leases: list[dict[str, Any]] = []
    seen_scope_fingerprints: set[str] = set()
    for entry in topology["preferredTransitLegs"]:
        left = occurrences_by_id[entry["fromOccurrenceId"]]
        right = occurrences_by_id[entry["toOccurrenceId"]]
        left_variants = _route_identity_variants(left)
        right_variants = _route_identity_variants(right)
        for left_variant in left_variants:
            for right_variant in right_variants:
                if (
                    left_variant["canonicalIdentity"]["amapId"]
                    == right_variant["canonicalIdentity"]["amapId"]
                ):
                    continue
                lease = _lease_from_topology_entry(
                    entry=entry,
                    occurrences_by_id=occurrences_by_id,
                    root=root,
                    route_contract=route_contract,
                    mode="transit",
                    condition="preferred",
                    left_variant=left_variant,
                    right_variant=right_variant,
                )
                if lease["leaseFingerprint"] in seen_scope_fingerprints:
                    raise CanonicalBindingError("route_lease_duplicate")
                seen_scope_fingerprints.add(lease["leaseFingerprint"])
                leases.append(lease)
    if topology["conditionalWalkingFallbacks"]:
        # A fallback is not a capture authorization.  It becomes eligible only
        # after a separately sealed preferred-transit-unavailable receipt, so
        # this pre-effect manifest must not reserve calls or issue a walking
        # lease in advance.
        raise CanonicalBindingError("walking_fallback_requires_post_transit_reissue")
    if not leases:
        raise CanonicalBindingError("route_pair_manifest_empty")
    if len(leases) > _MAX_ROUTE_CALLS:
        reason = (
            "candidate_route_manifest_budget_exceeded"
            if certificate_schema == _PROVISIONAL_SELECTED_ANCHOR_SCHEMA
            else "route_pair_budget_exceeded"
        )
        raise CanonicalBindingError(reason)
    manifest = {
        "schemaVersion": _ROUTE_MANIFEST_SCHEMA,
        "recordingType": "recorded/non-live",
        "promotable": False,
        "sourceBindings": deepcopy(bindings),
        "selectedAnchorBindingCertificateFingerprint": certificate[
            "certificateFingerprint"
        ],
        "selectedAnchorBindingSchemaVersion": certificate_schema,
        "finalIdentitySelectionComplete": final_identity_selection_complete,
        "canonicalIdentityCertificateFingerprint": (
            certificate["certificateFingerprint"]
            if final_identity_selection_complete
            else None
        ),
        "productionDryRouteTraceFingerprint": trace["productionDryRouteTraceFingerprint"],
        "routeContractFingerprint": route_contract,
        "orderedRouteLeases": leases,
        "conditionalWalkingCapturePolicy": {
            "status": "not_authorized_until_preferred_transit_unavailable",
            "routeCaptureAuthorized": False,
            "maxCalls": 0,
        },
        "pairCount": len(leases),
        "routeBudget": {"maxCalls": len(leases), "hardCap": _MAX_ROUTE_CALLS},
    }
    manifest["routeManifestFingerprint"] = canonical_sha256(manifest)
    return manifest


def validate_exact_route_pair_mode_manifest(
    *,
    route_manifest: dict[str, Any],
    anchor_binding_certificate: dict[str, Any],
    dry_route_trace: dict[str, Any],
) -> None:
    expected = build_exact_route_pair_mode_manifest(
        anchor_binding_certificate=anchor_binding_certificate,
        dry_route_trace=dry_route_trace,
    )
    if _object_copy(route_manifest, "route_manifest_invalid") != expected:
        raise CanonicalBindingError("route_pair_manifest_tampered")


def _validated_exact_route_topology(
    *,
    trace: dict[str, Any],
    occurrences_by_id: dict[str, dict[str, Any]],
    route_contract: str,
    root: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Accept only the explicit selected topology emitted by the dry route path.

    A Place capture is not itself route evidence.  In particular, a meal or
    another Phase-1 request cannot become an anchor merely because it happens
    earlier in the day's clock order.  This boundary therefore consumes the
    production-shaped topology verbatim and binds every logical occurrence to
    a certificate occurrence before a Route lease is constructed.
    """

    preferred = trace.get("preferredTransitLegs")
    fallbacks = trace.get("conditionalWalkingFallbacks")
    if not isinstance(preferred, list) or not preferred or not isinstance(fallbacks, list):
        raise CanonicalBindingError("dry_route_topology_missing_or_invalid")
    seen: set[tuple[str, str, str, str]] = set()
    projected_preferred = [
        _project_production_topology_entry(
            entry=entry,
            occurrences_by_id=occurrences_by_id,
            route_contract=route_contract,
            root=root,
        )
        for entry in preferred
    ]
    projected_fallbacks = [
        _project_production_topology_entry(
            entry=entry,
            occurrences_by_id=occurrences_by_id,
            route_contract=route_contract,
            root=root,
        )
        for entry in fallbacks
    ]
    for entry in [*projected_preferred, *projected_fallbacks]:
        key = _topology_key(entry)
        if key in seen:
            raise CanonicalBindingError("dry_route_topology_duplicate")
        seen.add(key)
    for entry in projected_preferred:
        if entry.get("mode") != "transit" or entry.get("condition") != "preferred":
            raise CanonicalBindingError("dry_route_topology_preferred_invalid")
    for entry in projected_fallbacks:
        if entry.get("mode") != "walking" or entry.get("condition") != "preferred_unavailable_only":
            raise CanonicalBindingError("dry_route_topology_walking_invalid")
    return {
        "preferredTransitLegs": projected_preferred,
        "conditionalWalkingFallbacks": projected_fallbacks,
    }


def _project_production_topology_entry(
    *,
    entry: Any,
    occurrences_by_id: dict[str, dict[str, Any]],
    route_contract: str,
    root: dict[str, str],
) -> dict[str, Any]:
    required = {
        "briefId", "dayNumber", "fromLogicalNode", "toLogicalNode", "mode", "reason", "condition",
    }
    if not isinstance(entry, dict) or set(entry) != required:
        raise CanonicalBindingError("dry_route_topology_entry_invalid")
    if (
        not isinstance(entry["briefId"], str)
        or not entry["briefId"]
        or not isinstance(entry["dayNumber"], int)
        or entry["briefId"] != root["briefId"]
        or not isinstance(entry["fromLogicalNode"], str)
        or not isinstance(entry["toLogicalNode"], str)
    ):
        raise CanonicalBindingError("dry_route_topology_entry_invalid")
    prefix = f"{entry['briefId']}::"
    if not entry["fromLogicalNode"].startswith(prefix) or not entry["toLogicalNode"].startswith(prefix):
        raise CanonicalBindingError("dry_route_topology_scope_mismatch")
    from_slot = entry["fromLogicalNode"][len(prefix):]
    to_slot = entry["toLogicalNode"][len(prefix):]
    matches = list(occurrences_by_id.values())
    left_candidates = [
        occurrence for occurrence in matches
        if occurrence["scope"]["briefId"] == entry["briefId"]
        and occurrence["scope"]["dayNumber"] == entry["dayNumber"]
        and occurrence["scope"]["planningSlotId"] == from_slot
    ]
    right_candidates = [
        occurrence for occurrence in matches
        if occurrence["scope"]["briefId"] == entry["briefId"]
        and occurrence["scope"]["dayNumber"] == entry["dayNumber"]
        and occurrence["scope"]["planningSlotId"] == to_slot
    ]
    if len(left_candidates) != 1 or len(right_candidates) != 1:
        raise CanonicalBindingError("dry_route_topology_occurrence_invalid")
    left, right = left_candidates[0], right_candidates[0]
    left_scope = left["scope"]
    right_scope = right["scope"]
    if (
        left_scope["briefId"] != right_scope["briefId"]
        or left_scope["dayNumber"] != right_scope["dayNumber"]
    ):
        raise CanonicalBindingError("dry_route_topology_scope_mismatch")
    if (
        not isinstance(entry["reason"], str)
        or entry["reason"] not in {"production_selected_adjacent", "replacement_adjacent", "replacement_bypass"}
        or not isinstance(entry["mode"], str)
        or not isinstance(entry["condition"], str)
    ):
        raise CanonicalBindingError("dry_route_topology_entry_invalid")
    return {
        "fromOccurrenceId": left["occurrenceId"],
        "toOccurrenceId": right["occurrenceId"],
        "planningSlotId": right_scope["planningSlotId"],
        "candidatePhysicalId": right["canonicalIdentity"]["amapId"],
        "adjacentAnchorIds": [left["occurrenceId"], right["occurrenceId"]],
        "mode": entry["mode"],
        "reason": entry["reason"],
        "condition": entry["condition"],
        "routeContractFingerprint": route_contract,
    }


def _validate_production_issued_dry_trace(
    *, trace: dict[str, Any], bindings: dict[str, str], certificate_fingerprint: str
) -> None:
    if trace.get("schemaVersion") != "trip-production-issued-dry-route-trace-v1":
        raise CanonicalBindingError("production_dry_route_trace_missing")
    fingerprint = trace.get("productionDryRouteTraceFingerprint")
    material = deepcopy(trace)
    material.pop("productionDryRouteTraceFingerprint", None)
    required = {
        "schemaVersion", "captureSemanticTraceFingerprint", "anchorBindingCertificateFingerprint",
        "sourceFingerprint", "dedicatedCaseSha256", "routeContractFingerprint", "planningRoot",
        "preferredTransitLegs", "conditionalWalkingFallbacks", "productionDryRouteTraceFingerprint",
    }
    if (
        set(trace) != required
        or not _is_hex(fingerprint)
        or canonical_sha256(material) != fingerprint
        or not _is_hex(trace.get("captureSemanticTraceFingerprint"))
        or trace.get("anchorBindingCertificateFingerprint") != certificate_fingerprint
        or trace.get("captureSemanticTraceFingerprint")
        != bindings["captureSemanticTraceFingerprint"]
        or trace.get("sourceFingerprint") != bindings["sourceFingerprint"]
        or trace.get("dedicatedCaseSha256") != bindings["dedicatedCaseSha256"]
        or trace.get("routeContractFingerprint") != bindings["routeContractFingerprint"]
    ):
        raise CanonicalBindingError("production_dry_route_trace_binding_invalid")


def _lease_from_topology_entry(
    *,
    entry: dict[str, Any],
    occurrences_by_id: dict[str, dict[str, Any]],
    root: dict[str, str],
    route_contract: str,
    mode: str,
    condition: str,
    left_variant: dict[str, Any] | None = None,
    right_variant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    left = occurrences_by_id[entry["fromOccurrenceId"]]
    right = occurrences_by_id[entry["toOccurrenceId"]]
    left_variant = left_variant or left
    right_variant = right_variant or right
    left_identity = left_variant["canonicalIdentity"]
    right_identity = right_variant["canonicalIdentity"]
    right_scope = right["scope"]
    lease = {
        "planningRoot": root["planningRoot"],
        "rootPortfolioId": root["rootPortfolioId"],
        "briefId": right_scope["briefId"],
        "dayNumber": right_scope["dayNumber"],
        "planningSlotId": entry["planningSlotId"],
        "candidatePhysicalId": right_identity["amapId"],
        "adjacentAnchorIds": deepcopy(entry["adjacentAnchorIds"]),
        "fromAmapId": left_identity["amapId"],
        "toAmapId": right_identity["amapId"],
        "mode": mode,
        "providerRequest": _provider_route_request(
            left_identity, right_identity, mode=mode
        ),
        "routeContractFingerprint": route_contract,
        "reason": entry["reason"],
        "condition": condition,
        "fromResponseSha256": left_variant["responseSha256"],
        "toResponseSha256": right_variant["responseSha256"],
    }
    lease["leaseFingerprint"] = canonical_sha256(lease)
    return lease


def _route_identity_variants(occurrence: dict[str, Any]) -> list[dict[str, Any]]:
    alternatives = occurrence.get("candidateAlternatives")
    if alternatives is None:
        alternatives = [occurrence]
    if not isinstance(alternatives, list) or not alternatives:
        raise CanonicalBindingError("route_candidate_alternatives_invalid")
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for alternative in alternatives:
        if not isinstance(alternative, dict):
            raise CanonicalBindingError("route_candidate_alternatives_invalid")
        identity = alternative.get("canonicalIdentity")
        response_sha = alternative.get("responseSha256")
        if (
            not isinstance(identity, dict)
            or not _AMAP_ID.fullmatch(str(identity.get("amapId") or ""))
            or not _is_hex(response_sha)
        ):
            raise CanonicalBindingError("route_candidate_alternatives_invalid")
        key = (str(identity["amapId"]), str(response_sha))
        if key in seen:
            raise CanonicalBindingError("route_candidate_alternatives_duplicate")
        seen.add(key)
        projected.append(alternative)
    return sorted(
        projected,
        key=lambda item: (
            str(item["canonicalIdentity"]["amapId"]),
            str(item.get("candidateFingerprint") or ""),
            str(item["responseSha256"]),
        ),
    )


def _topology_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        entry["fromOccurrenceId"],
        entry["toOccurrenceId"],
        entry["planningSlotId"],
        entry["mode"],
    )


def _route_scope_key(lease: dict[str, Any]) -> tuple[str, str, str, str, tuple[str, ...]]:
    return (
        lease["fromAmapId"],
        lease["toAmapId"],
        lease["planningSlotId"],
        lease["candidatePhysicalId"],
        tuple(lease["adjacentAnchorIds"]),
    )


def _validated_allowlist(envelope: dict[str, Any]) -> dict[str, Any]:
    allowlist = envelope.get("exactPlaceRequestAllowlist")
    if not isinstance(allowlist, dict) or set(allowlist) != {
        "requestCount",
        "requests",
        "requestMultiset",
        "requestMultisetFingerprint",
        "allowlistFingerprint",
    }:
        raise CanonicalBindingError("place_allowlist_invalid")
    requests = allowlist.get("requests")
    if not isinstance(requests, list) or not requests or allowlist.get("requestCount") != len(requests):
        raise CanonicalBindingError("place_allowlist_invalid")
    if not all(isinstance(request, dict) for request in requests):
        raise CanonicalBindingError("place_allowlist_invalid")
    expected_multiset = [
        {
            "auditFingerprint": audit,
            "requestFingerprint": request,
            "count": count,
        }
        for (audit, request), count in sorted(
            Counter(
                (
                    str(item.get("auditFingerprint") or ""),
                    str(item.get("requestFingerprint") or ""),
                )
                for item in requests
            ).items()
        )
    ]
    multiset_fingerprint = allowlist.get("requestMultisetFingerprint")
    if (
        allowlist.get("requestMultiset") != expected_multiset
        or not _is_hex(multiset_fingerprint)
        or canonical_sha256(expected_multiset) != multiset_fingerprint
    ):
        raise CanonicalBindingError("place_allowlist_multiset_invalid")
    material = deepcopy(allowlist)
    observed_fingerprint = material.pop("allowlistFingerprint", None)
    if (
        not _is_hex(observed_fingerprint)
        or canonical_sha256(material) != observed_fingerprint
    ):
        raise CanonicalBindingError("place_allowlist_fingerprint_invalid")
    return allowlist


def _validated_complete_place_quarantine(
    quarantine: dict[str, Any], allowlist: dict[str, Any]
) -> list[dict[str, Any]]:
    if quarantine.get("schemaVersion") != "trip-recorded-amap-v1" or quarantine.get("recordingType") != "recorded/non-live":
        raise CanonicalBindingError("place_quarantine_schema_invalid")
    if quarantine.get("promotable") is not False:
        raise CanonicalBindingError("place_quarantine_promotable_invalid")
    if not _is_hex(quarantine.get("bundleFingerprint")):
        raise CanonicalBindingError("place_quarantine_fingerprint_invalid")
    material = deepcopy(quarantine)
    material.pop("bundleFingerprint", None)
    if canonical_sha256(material) != quarantine["bundleFingerprint"]:
        raise CanonicalBindingError("place_quarantine_fingerprint_mismatch")
    details = quarantine.get("quarantine")
    records = quarantine.get("responses")
    if not isinstance(details, dict) or not isinstance(records, list):
        raise CanonicalBindingError("place_quarantine_records_invalid")
    expected = allowlist["requestCount"]
    if details.get("kind") != "place_only_capture" or details.get("requestCount") != expected or details.get("completedResponseCount") != expected or len(records) != expected:
        raise CanonicalBindingError("place_quarantine_incomplete")
    if details.get("routeCaptureAuthorized") is not False or details.get("routeCalls") != 0:
        raise CanonicalBindingError("place_quarantine_scope_invalid")
    result: list[dict[str, Any]] = []
    for ordinal, (request, record) in enumerate(zip(allowlist["requests"], records), start=1):
        if not isinstance(request, dict) or not isinstance(record, dict):
            raise CanonicalBindingError("place_quarantine_records_invalid")
        if record.get("ordinal") != ordinal or record.get("requestFingerprint") != request.get("requestFingerprint") or record.get("auditFingerprint") != request.get("auditFingerprint"):
            raise CanonicalBindingError("place_quarantine_request_binding_mismatch")
        response = record.get("response")
        if not isinstance(response, dict) or not _is_hex(record.get("responseSha256")):
            raise CanonicalBindingError("place_quarantine_response_invalid")
        endpoint = {
            "place/text": "/v3/place/text",
            "place/around": "/v3/place/around",
        }.get(request.get("endpoint"))
        recorded_request = record.get("request")
        if (
            endpoint is None
            or not isinstance(recorded_request, dict)
            or recorded_request != {
                "endpoint": endpoint,
                "params": request.get("sanitizedParams"),
            }
        ):
            raise CanonicalBindingError("place_quarantine_request_shape_mismatch")
        if not _canonical_hash_matches(response, record["responseSha256"]):
            raise CanonicalBindingError("place_quarantine_response_hash_mismatch")
        if response.get("status") != "1" or response.get("infocode") != "10000":
            raise CanonicalBindingError("place_quarantine_response_unsuccessful")
        for poi in response.get("pois") or []:
            if not isinstance(poi, dict) or poi.get("photos") != []:
                raise CanonicalBindingError("place_quarantine_response_not_sanitized")
        _reject_sensitive(response)
        result.append(record)
    return result


def _derive_occurrences(
    *, allowlist: dict[str, Any], runtime_evidence: dict[str, Any], manifest: dict[str, Any]
) -> list[_Occurrence]:
    cases = runtime_evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict):
        raise CanonicalBindingError("runtime_case_binding_invalid")
    trace = cases[0].get("recordedCaptureSemanticTrace")
    if not isinstance(trace, dict):
        raise CanonicalBindingError("runtime_trace_missing")
    if trace.get("sourceFingerprint") != manifest.get("sourceFingerprint"):
        raise CanonicalBindingError("runtime_trace_source_binding_mismatch")
    profile_runs = trace.get("profileRuns")
    if not isinstance(profile_runs, list):
        raise CanonicalBindingError("runtime_profile_runs_missing")
    by_profile: dict[str, dict[str, Any]] = {}
    for profile in profile_runs:
        if not isinstance(profile, dict) or not _is_hex(profile.get("profileFingerprint")):
            raise CanonicalBindingError("runtime_profile_invalid")
        profile_fingerprint = profile["profileFingerprint"]
        existing = by_profile.get(profile_fingerprint)
        if existing is not None:
            if existing != profile:
                raise CanonicalBindingError("runtime_profile_conflicting_duplicate")
            continue
        by_profile[profile_fingerprint] = profile
    slots = trace.get("logicalSlots")
    if not isinstance(slots, list):
        raise CanonicalBindingError("runtime_logical_slots_missing")
    start_by_slot: dict[tuple[str, str, str, int], str] = {}
    for slot in slots:
        if not isinstance(slot, dict):
            raise CanonicalBindingError("runtime_logical_slot_invalid")
        scope = (
            str(slot.get("briefId") or ""),
            str(slot.get("poolId") or ""),
            str(slot.get("planningSlotId") or ""),
            slot.get("dayNumber"),
        )
        start = str(slot.get("startTime") or "")
        if not all(scope[:3]) or not isinstance(scope[3], int) or not start:
            raise CanonicalBindingError("runtime_logical_slot_invalid")
        if scope in start_by_slot:
            raise CanonicalBindingError("runtime_logical_slot_duplicate")
        start_by_slot[scope] = start
    result: list[_Occurrence] = []
    for ordinal, request in enumerate(allowlist["requests"], start=1):
        if not isinstance(request, dict):
            raise CanonicalBindingError("place_allowlist_invalid")
        occurrence = request.get("profileOccurrence")
        lineage = request.get("queryPlanLineage")
        if not isinstance(occurrence, dict) or not isinstance(lineage, dict):
            raise CanonicalBindingError("place_request_lineage_missing")
        profile_fingerprint = str(occurrence.get("profileFingerprint") or "")
        occurrence_fingerprint = str(occurrence.get("occurrenceFingerprint") or "")
        query_plan_fingerprint = str(lineage.get("providerPlanFingerprint") or "")
        query_scope_fingerprint = str(lineage.get("queryScopeFingerprint") or "")
        if not all(_is_hex(value) for value in (profile_fingerprint, occurrence_fingerprint, query_plan_fingerprint, query_scope_fingerprint)):
            raise CanonicalBindingError("place_request_lineage_invalid")
        profile = by_profile.get(profile_fingerprint)
        if profile is None:
            raise CanonicalBindingError("place_request_profile_not_found")
        scope = profile.get("scope")
        role = profile.get("semanticRole")
        if not isinstance(scope, dict) or not isinstance(role, dict):
            raise CanonicalBindingError("runtime_profile_invalid")
        scope_key = (
            str(occurrence.get("briefId") or ""),
            str(occurrence.get("poolId") or ""),
            str(occurrence.get("planningSlotId") or ""),
            occurrence.get("dayNumber"),
        )
        if scope_key not in start_by_slot or any(scope.get(key) != value for key, value in (("briefId", scope_key[0]), ("poolId", scope_key[1]), ("planningSlotId", scope_key[2]), ("dayNumber", scope_key[3]))):
            raise CanonicalBindingError("place_request_occurrence_scope_mismatch")
        family = str(role.get("experienceFamily") or "").strip()
        intent_type = str(role.get("intentType") or "").strip()
        requirement_level = str(role.get("requirementLevel") or "").strip()
        city = str(profile.get("city") or "").strip()
        if not all((family, intent_type, requirement_level, city)):
            raise CanonicalBindingError("place_request_consumer_scope_missing")
        consumer_input = profile.get("consumerAdmissionInput")
        if not isinstance(consumer_input, dict):
            raise CanonicalBindingError("runtime_consumer_admission_input_missing")
        if any(
            consumer_input.get(key) != value
            for key, value in (
                ("briefId", scope_key[0]),
                ("poolId", scope_key[1]),
                ("planningSlotId", scope_key[2]),
                ("dayNumber", scope_key[3]),
                ("city", city),
                ("family", family),
                ("activityMode", intent_type),
                ("requirementLevel", requirement_level),
            )
        ):
            raise CanonicalBindingError("runtime_consumer_admission_scope_mismatch")
        route_context = consumer_input.get("routeContext")
        route_contract = (
            route_context.get("routeDecisionContract")
            if isinstance(route_context, dict)
            and isinstance(route_context.get("routeDecisionContract"), dict)
            else {}
        )
        if route_contract.get("fingerprint") != trace.get("routeContractFingerprint"):
            raise CanonicalBindingError("runtime_consumer_route_contract_mismatch")
        experience_spec_policy = consumer_input.get("experienceSpecPolicy")
        if experience_spec_policy:
            spec_fingerprint = consumer_input.get("specFingerprint")
            if not isinstance(experience_spec_policy, dict):
                raise CanonicalBindingError("runtime_consumer_experience_spec_invalid")
            policy_material = {
                key: value
                for key, value in experience_spec_policy.items()
                if key not in {"specFingerprint", "experienceSpecFingerprint"}
            }
            if (
                not _is_hex(spec_fingerprint)
                or canonical_sha256(policy_material) != str(spec_fingerprint).upper()
                or consumer_input.get("experienceSpecPolicyError") is not None
            ):
                raise CanonicalBindingError("runtime_consumer_experience_spec_invalid")
        if family in {"meal", "public_city_view"} and (
            not isinstance(experience_spec_policy, dict)
            or experience_spec_policy.get("unresolvedDimensions") != []
            or not experience_spec_policy.get("experienceFamilies")
            or not experience_spec_policy.get("allowedDayNumbers")
        ):
            raise CanonicalBindingError("runtime_consumer_experience_spec_incomplete")
        profile_query_plans = profile.get("queryPlans")
        if not isinstance(profile_query_plans, list) or not any(
            _query_plan_lineage_matches(
                plan=plan,
                occurrence_fingerprint=occurrence_fingerprint,
                lineage=lineage,
            )
            for plan in profile_query_plans
            if isinstance(plan, dict)
        ):
            raise CanonicalBindingError("place_request_query_plan_lineage_mismatch")
        result.append(
            _Occurrence(
                ordinal=ordinal,
                brief_id=scope_key[0],
                pool_id=scope_key[1],
                planning_slot_id=scope_key[2],
                day_number=scope_key[3],
                start_time=start_by_slot[scope_key],
                profile_fingerprint=profile_fingerprint,
                occurrence_fingerprint=occurrence_fingerprint,
                query_plan_fingerprint=query_plan_fingerprint,
                query_scope_fingerprint=query_scope_fingerprint,
                family=family,
                intent_type=intent_type,
                requirement_level=requirement_level,
                city=city,
                consumer_admission_input=deepcopy(consumer_input),
            )
        )
    return result


def _query_plan_lineage_matches(
    *,
    plan: dict[str, Any],
    occurrence_fingerprint: str,
    lineage: dict[str, Any],
) -> bool:
    """Recompute the production query-scope seal from its authoritative plan.

    ``profileRuns[*].queryPlans`` is an adapter audit record and intentionally
    does not copy ``queryScopeFingerprint`` into every plan.  The fingerprint
    is instead sealed on the outbound request.  Recomputing it here preserves
    the cross-component scope binding without inventing a consumer-only field.
    """

    provider_plan_fingerprint = str(plan.get("providerPlanFingerprint") or "")
    source_plan_fingerprint = str(plan.get("sourcePlanFingerprint") or "")
    if (
        not _is_hex(occurrence_fingerprint)
        or not _is_hex(provider_plan_fingerprint)
        or not _is_hex(source_plan_fingerprint)
        or provider_plan_fingerprint
        != str(lineage.get("providerPlanFingerprint") or "")
        or source_plan_fingerprint != str(lineage.get("sourcePlanFingerprint") or "")
        or str(plan.get("planId") or "") != str(lineage.get("providerPlanId") or "")
        or str(plan.get("sourcePlanId") or "") != str(lineage.get("sourcePlanId") or "")
    ):
        return False
    endpoint = str(plan.get("endpoint") or "")
    if endpoint not in {"place/text", "place/around"}:
        return False
    try:
        limit = int(plan.get("resultLimit"))
        radius = int(plan.get("radiusMeters")) if endpoint == "place/around" else 0
    except (TypeError, ValueError):
        return False
    expected = canonical_sha256(
        {
            "occurrenceFingerprint": occurrence_fingerprint,
            "sourcePlanFingerprint": source_plan_fingerprint,
            "providerPlanFingerprint": provider_plan_fingerprint,
            "requestShape": {
                "endpoint": endpoint,
                "city": str(plan.get("city") or ""),
                "keyword": str(plan.get("keyword") or ""),
                "category": str(plan.get("category") or ""),
                "limit": limit,
                "radius": radius,
            },
        }
    )
    return expected == str(lineage.get("queryScopeFingerprint") or "")


def _validate_preflight_envelope_bindings(
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    runtime: dict[str, Any],
    *,
    cases_dir: Path,
) -> None:
    try:
        validate_capture_preflight_manifest(
            manifest=manifest,
            runtime_evidence=runtime,
            cases_dir=cases_dir,
        )
        validate_zero_network_capture_session_envelope(
            envelope=envelope,
            manifest=manifest,
            runtime_evidence=runtime,
            cases_dir=cases_dir,
        )
    except ValueError as error:
        raise CanonicalBindingError("production_capture_binding_invalid") from None
    if manifest.get("captureState") != "ready_for_place_identity_capture" or manifest.get("terminalStatus") is not None:
        raise CanonicalBindingError("preflight_not_ready")
    if envelope.get("status") != "prepared" or envelope.get("captureScope") != "place_only" or envelope.get("routeCaptureAuthorized") is not False:
        raise CanonicalBindingError("capture_envelope_not_place_only")
    if not _is_hex(manifest.get("manifestFingerprint")) or not _is_hex(envelope.get("envelopeFingerprint")):
        raise CanonicalBindingError("capture_binding_fingerprint_invalid")
    bindings = envelope.get("sourceBindings")
    if not isinstance(bindings, dict) or bindings.get("capturePreflightManifestFingerprint") != manifest.get("manifestFingerprint"):
        raise CanonicalBindingError("capture_binding_manifest_mismatch")
    if bindings.get("sourceFingerprint") != manifest.get("sourceFingerprint") or bindings.get("dedicatedCaseSha256") != manifest.get("dedicatedCaseSha256"):
        raise CanonicalBindingError("capture_binding_source_mismatch")
    if not isinstance(runtime.get("cases"), list):
        raise CanonicalBindingError("runtime_evidence_invalid")


def _source_bindings(
    manifest: dict[str, Any],
    envelope: dict[str, Any],
    *,
    runtime_evidence: dict[str, Any],
) -> dict[str, str]:
    source = envelope["sourceBindings"]
    capture_semantic_trace_fingerprint = _runtime_semantic_trace_fingerprint(
        runtime_evidence
    )
    if str(source["runtimeEvidenceSha256"]) != capture_semantic_trace_fingerprint:
        raise CanonicalBindingError("runtime_trace_binding_mismatch")
    result = {
        "sourceFingerprint": str(source["sourceFingerprint"]),
        "dedicatedCaseSha256": str(source["dedicatedCaseSha256"]),
        "runtimeEvidenceSha256": str(source["runtimeEvidenceSha256"]),
        "captureSemanticTraceFingerprint": capture_semantic_trace_fingerprint,
        "capturePreflightManifestFingerprint": str(source["capturePreflightManifestFingerprint"]),
        "exactPlaceRequestAllowlistFingerprint": str(envelope["exactPlaceRequestAllowlist"]["allowlistFingerprint"]),
        "routeContractFingerprint": str(manifest["routeContractFingerprint"]),
    }
    if not all(_is_hex(value) for value in result.values()):
        raise CanonicalBindingError("source_binding_fingerprint_invalid")
    return result


def _runtime_semantic_trace_fingerprint(runtime_evidence: dict[str, Any]) -> str:
    cases = runtime_evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict):
        raise CanonicalBindingError("runtime_semantic_trace_missing")
    trace = cases[0].get("recordedCaptureSemanticTrace")
    if not isinstance(trace, dict):
        raise CanonicalBindingError("runtime_semantic_trace_missing")
    material = deepcopy(trace)
    fingerprint = material.pop("runtimeEvidenceSha256", None)
    if not _is_hex(fingerprint) or canonical_sha256(material) != fingerprint:
        raise CanonicalBindingError("runtime_semantic_trace_fingerprint_invalid")
    return fingerprint


def _validate_certificate_shape(certificate: dict[str, Any]) -> None:
    if certificate.get("schemaVersion") != _CANONICAL_BINDING_SCHEMA or certificate.get("recordingType") != "recorded/non-live" or certificate.get("promotable") is not False:
        raise CanonicalBindingError("canonical_identity_certificate_schema_invalid")
    fingerprint = certificate.get("certificateFingerprint")
    if not _is_hex(fingerprint):
        raise CanonicalBindingError("canonical_identity_certificate_fingerprint_invalid")
    material = deepcopy(certificate)
    material.pop("certificateFingerprint", None)
    if canonical_sha256(material) != fingerprint:
        raise CanonicalBindingError("canonical_identity_certificate_fingerprint_mismatch")
    bindings = certificate.get("sourceBindings")
    rows = certificate.get("occurrences")
    if not isinstance(bindings, dict) or not isinstance(rows, list) or not rows:
        raise CanonicalBindingError("canonical_identity_certificate_invalid")
    required_binding_keys = {
        "sourceFingerprint",
        "dedicatedCaseSha256",
        "runtimeEvidenceSha256",
        "captureSemanticTraceFingerprint",
        "capturePreflightManifestFingerprint",
        "exactPlaceRequestAllowlistFingerprint",
        "routeContractFingerprint",
    }
    if set(bindings) != required_binding_keys or not all(_is_hex(bindings[key]) for key in required_binding_keys):
        raise CanonicalBindingError("canonical_identity_certificate_bindings_invalid")
    ids: set[str] = set()
    occurrence_ids: set[str] = set()
    for ordinal, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or row.get("ordinal") != ordinal:
            raise CanonicalBindingError("canonical_identity_certificate_occurrence_invalid")
        identity = row.get("canonicalIdentity")
        scope = row.get("scope")
        if not isinstance(identity, dict) or not isinstance(scope, dict):
            raise CanonicalBindingError("canonical_identity_certificate_occurrence_invalid")
        amap_id = identity.get("amapId")
        occurrence_id = row.get("occurrenceId")
        if not _AMAP_ID.fullmatch(str(amap_id or "")) or not _is_hex(occurrence_id) or amap_id in ids or occurrence_id in occurrence_ids:
            raise CanonicalBindingError("canonical_identity_certificate_occurrence_invalid")
        ids.add(amap_id)
        occurrence_ids.add(occurrence_id)


def _validate_provisional_selected_anchor_shape(
    certificate: dict[str, Any],
) -> None:
    if (
        certificate.get("schemaVersion") != _PROVISIONAL_SELECTED_ANCHOR_SCHEMA
        or certificate.get("recordingType") != "recorded/non-live"
        or certificate.get("promotable") is not False
        or certificate.get("finalIdentitySelectionComplete") is not False
        or certificate.get("routeMatrixRequired") is not True
    ):
        raise CanonicalBindingError("provisional_selected_anchor_schema_invalid")
    fingerprint = certificate.get("certificateFingerprint")
    material = deepcopy(certificate)
    material.pop("certificateFingerprint", None)
    if not _is_hex(fingerprint) or canonical_sha256(material) != fingerprint:
        raise CanonicalBindingError("provisional_selected_anchor_fingerprint_invalid")
    bindings = certificate.get("sourceBindings")
    occurrences = certificate.get("occurrences")
    if not isinstance(bindings, dict) or not isinstance(occurrences, list) or not occurrences:
        raise CanonicalBindingError("provisional_selected_anchor_invalid")
    required_binding_keys = {
        "sourceFingerprint",
        "dedicatedCaseSha256",
        "runtimeEvidenceSha256",
        "captureSemanticTraceFingerprint",
        "capturePreflightManifestFingerprint",
        "exactPlaceRequestAllowlistFingerprint",
        "routeContractFingerprint",
    }
    if set(bindings) != required_binding_keys or not all(
        _is_hex(bindings[key]) for key in required_binding_keys
    ):
        raise CanonicalBindingError("provisional_selected_anchor_bindings_invalid")
    if not _is_hex(certificate.get("candidateUniverseCertificateFingerprint")):
        raise CanonicalBindingError("provisional_selected_anchor_universe_invalid")
    if not _is_hex(certificate.get("stagedSelectionFingerprint")):
        raise CanonicalBindingError("provisional_selected_anchor_selection_invalid")
    ids: set[str] = set()
    occurrence_ids: set[str] = set()
    for ordinal, row in enumerate(occurrences, start=1):
        if not isinstance(row, dict) or row.get("ordinal") != ordinal:
            raise CanonicalBindingError("provisional_selected_anchor_occurrence_invalid")
        identity = row.get("canonicalIdentity")
        scope = row.get("scope")
        admission = row.get("consumerAdmission")
        alternatives = row.get("candidateAlternatives")
        if (
            not isinstance(identity, dict)
            or not isinstance(scope, dict)
            or not isinstance(admission, dict)
            or not isinstance(alternatives, list)
            or not alternatives
        ):
            raise CanonicalBindingError("provisional_selected_anchor_occurrence_invalid")
        amap_id = identity.get("amapId")
        occurrence_id = row.get("occurrenceId")
        if (
            not _AMAP_ID.fullmatch(str(amap_id or ""))
            or not _is_hex(occurrence_id)
            or amap_id in ids
            or occurrence_id in occurrence_ids
            or not _is_hex(admission.get("consumerFingerprint"))
            or not _is_hex(admission.get("candidateEvidenceFingerprint"))
            or not _is_hex(admission.get("admissionFingerprint"))
            or not _is_hex(admission.get("reportFingerprint"))
        ):
            raise CanonicalBindingError("provisional_selected_anchor_occurrence_invalid")
        alternative_ids: set[str] = set()
        selected_matches = 0
        for alternative in alternatives:
            if not isinstance(alternative, dict) or set(alternative) != {
                "responseOrdinal",
                "requestFingerprint",
                "auditFingerprint",
                "responseSha256",
                "supportingEvidence",
                "candidateFingerprint",
                "canonicalIdentity",
                "consumerAdmission",
            }:
                raise CanonicalBindingError(
                    "provisional_route_candidate_alternative_invalid"
                )
            alternative_identity = alternative.get("canonicalIdentity")
            alternative_admission = alternative.get("consumerAdmission")
            alternative_id = (
                str(alternative_identity.get("amapId") or "")
                if isinstance(alternative_identity, dict)
                else ""
            )
            if (
                not _AMAP_ID.fullmatch(alternative_id)
                or alternative_id in alternative_ids
                or not isinstance(alternative_admission, dict)
                or not all(
                    _is_hex(alternative_admission.get(key))
                    for key in (
                        "consumerFingerprint",
                        "candidateEvidenceFingerprint",
                        "admissionFingerprint",
                        "reportFingerprint",
                    )
                )
                or not all(
                    _is_hex(alternative.get(key))
                    for key in (
                        "requestFingerprint",
                        "auditFingerprint",
                        "responseSha256",
                        "candidateFingerprint",
                    )
                )
                or not isinstance(alternative.get("responseOrdinal"), int)
                or alternative["responseOrdinal"] < 1
                or not isinstance(alternative.get("supportingEvidence"), list)
                or not alternative["supportingEvidence"]
            ):
                raise CanonicalBindingError(
                    "provisional_route_candidate_alternative_invalid"
                )
            alternative_ids.add(alternative_id)
            if (
                alternative_id == amap_id
                and alternative["responseOrdinal"] == row.get("responseOrdinal")
                and alternative["requestFingerprint"]
                == row.get("requestFingerprint")
                and alternative["auditFingerprint"] == row.get("auditFingerprint")
                and alternative["responseSha256"] == row.get("responseSha256")
                and alternative["canonicalIdentity"] == identity
                and alternative["consumerAdmission"] == admission
            ):
                selected_matches += 1
        if selected_matches != 1:
            raise CanonicalBindingError(
                "provisional_selected_anchor_alternative_binding_invalid"
            )
        ids.add(amap_id)
        occurrence_ids.add(occurrence_id)


def _provider_route_request(
    left: dict[str, Any], right: dict[str, Any], *, mode: str
) -> dict[str, Any]:
    if mode not in _ROUTE_MODES:
        raise CanonicalBindingError("route_mode_invalid")
    try:
        origin = f"{float(left['longitude'])},{float(left['latitude'])}"
        destination = f"{float(right['longitude'])},{float(right['latitude'])}"
    except (KeyError, TypeError, ValueError):
        raise CanonicalBindingError("route_coordinate_missing") from None
    if mode == "walking":
        return {
            "method": "GET",
            "scheme": "https",
            "host": "restapi.amap.com",
            "path": "/v3/direction/walking",
            "params": {"origin": origin, "destination": destination},
        }
    from_city = str(left.get("city") or "").strip()
    to_city = str(right.get("city") or "").strip()
    if not from_city or not to_city:
        raise CanonicalBindingError("route_city_missing")
    return {
        "method": "GET",
        "scheme": "https",
        "host": "restapi.amap.com",
        "path": "/v3/direction/transit/integrated",
        "params": {
            "origin": origin,
            "destination": destination,
            "city": from_city,
            "cityd": to_city,
            "strategy": "0",
        },
    }


def _reject_sensitive(value: Any) -> None:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise CanonicalBindingError("place_quarantine_response_invalid") from None
    if "http://" in rendered.casefold() or "https://" in rendered.casefold():
        raise CanonicalBindingError("place_quarantine_sensitive_value_forbidden")
    if re.search(r'"(?:key|token|authorization|cookie|proxy|signature)"\s*:', rendered, flags=re.IGNORECASE):
        raise CanonicalBindingError("place_quarantine_sensitive_value_forbidden")


def _object_copy(value: Any, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalBindingError(reason)
    return deepcopy(value)


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _canonical_hash_matches(value: Any, fingerprint: Any) -> bool:
    """Compare canonical content hashes while accepting hex case only."""

    return _is_hex(fingerprint) and canonical_sha256(value) == fingerprint.upper()

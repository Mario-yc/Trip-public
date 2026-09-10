"""Safe, bounded projections for Search Profile planning diagnostics."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_PROFILE_SCALAR_KEYS = (
    "schemaVersion",
    "profileId",
    "profileFingerprint",
    "city",
    "poolId",
    "briefId",
    "planningSlotId",
    "experienceFamily",
    "originalExperienceFamily",
    "activityMode",
    "intentType",
    "requirementLevel",
    "entityBindingMode",
    "exclusionFingerprint",
    "executionFingerprint",
)
_CHECKPOINT_EVIDENCE_VERSION = "portfolio-resume-evidence-v2"
_SEMANTIC_CONTRACT_SCHEMA = "portfolio-search-semantic-contract-v2"
_HEX_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_TEXT_RE = re.compile(
    r"(?:authorization|password|passwd|secret|cookie|credential|api[-_]?key|token)\s*[:=]",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:(?:^|\s)[A-Za-z]:[\\/]|(?:^|\s)/(?:home|users|tmp|var|etc|root|mnt|private)(?:/|\s|$))",
    re.IGNORECASE,
)
_GROUNDING_IDENTIFIER_KEYS = (
    "briefId",
    "creativeBriefId",
    "focusBriefId",
    "poolId",
    "planningSlotId",
    "slotId",
    "sourceGoalId",
    "goalId",
    "occurrenceId",
    "intentType",
    "requirementLevel",
    "status",
    "resultState",
    "routeStatus",
    "coverageStatus",
    "coverageReason",
    "reasonCode",
    "providerState",
)
_GROUNDING_COUNT_KEYS = (
    "dayNumber",
    "dayCount",
    "segmentCount",
    "anchorCount",
    "candidateCount",
    "selectedCount",
    "selectedCandidateCount",
    "resolvedCount",
    "unresolvedCount",
    "missingCount",
    "targetCount",
    "requiredCount",
    "matchedAnchorCount",
    "unmatchedAnchorCount",
    "persistableSegmentCount",
    "routeAnchorRequiredCount",
    "routeAnchorSelectedCount",
)
_GROUNDING_PERFORMANCE_KEYS = (
    "candidateGroundingMs",
    "candidateDiscoveryMs",
    "webDiscoveryMs",
    "amapGroundingMs",
    "routePreflightMs",
    "repairMs",
    "verifierMs",
    "workerQueueMs",
    "providerCallCount",
    "webSearchCount",
    "amapTextCount",
    "amapAroundCount",
    "amapRouteCount",
)
_CHECKPOINT_POOL_IDENTIFIER_KEYS = (
    "poolId",
    "briefId",
    "intentType",
    "requirementLevel",
    "goalId",
    "softGoalId",
    "sourceGoalId",
    "optionalExperienceFamily",
    "hintPolicy",
    "entityBindingMode",
    "queryCountSource",
    "searchProfileId",
    "searchProfileFingerprint",
    "exclusionFingerprint",
    "executionFingerprint",
    "experienceFamily",
    "activityMode",
    "queryFingerprint",
    "selectedSearchMode",
    "diversityRelaxationReason",
    "priorityClass",
    "budgetSkippedReason",
    "coverageStatus",
    "coverageReason",
    "providerState",
)
_CHECKPOINT_POOL_NUMBER_KEYS = (
    "targetCount",
    "candidateHintCount",
    "selectionThreshold",
    "queryCount",
    "compiledQueryPlanCount",
    "executedQueryPlanCount",
    "profileTargetCount",
    "profileEvidenceTargetCount",
    "duplicateExcludedCount",
    "familySpecificAmapCandidateCount",
    "webSeedGroundedCandidateCount",
    "rawCandidateCount",
    "candidateCount",
    "eligibleCandidateCount",
    "uniqueEligibleEntityCount",
    "selectedCandidateCount",
    "nearbyCandidateCount",
    "citywideCandidateCount",
    "cacheHitCount",
    "skippedBecauseBudget",
    "rejectedWeakEntityCount",
    "rejectedDuplicateCount",
    "rejectedDetourCount",
    "rejectedByDetourCount",
    "duplicateMealBrandCount",
    "uniqueMealFamilyCount",
    "zhajiangmianCount",
    "scoredCount",
    "rejectedCount",
    "selectedCount",
    "missingCount",
    "reservedPlaceTextCalls",
    "usedPlaceTextCalls",
    "semanticRejectedCount",
)


def project_search_profile(profile: Any) -> dict[str, Any]:
    """Return execution evidence without user text or provider payloads."""

    payload = _mapping(profile)
    if not payload:
        return {}
    projected: dict[str, Any] = {}
    for key in _PROFILE_SCALAR_KEYS:
        value = _safe_bounded_text(payload.get(key), 80) if key == "city" else _safe_identifier(payload.get(key))
        if value is not None:
            projected[key] = value

    query_plans: list[dict[str, Any]] = []
    for raw in payload.get("queryPlans") or []:
        plan = _mapping(raw)
        if not plan:
            continue
        item: dict[str, Any] = {}
        for key in ("planId", "mode", "anchorPolicy"):
            value = _safe_identifier(plan.get(key))
            if value is not None:
                item[key] = value
        for key in ("priority", "radiusMeters", "resultLimit", "fallbackLevel"):
            value = _safe_nonnegative_number(plan.get(key))
            if value is not None:
                item[key] = value
        provider_keys = _safe_identifier_list(plan.get("providerCategoryKeys"), 12)
        if provider_keys:
            item["providerCategoryKeys"] = provider_keys
        for key in ("requiresAmapGrounding", "stopWhenTargetReached"):
            if isinstance(plan.get(key), bool):
                item[key] = plan[key]
        if item.get("planId"):
            query_plans.append(item)
    projected["queryPlans"] = query_plans[:16]

    for policy_key, numeric_keys, boolean_keys in (
        (
            "coveragePolicy",
            ("targetCount", "evidenceTargetCount"),
            ("distinctPhysicalPoiRequired", "stopWhenTargetReached"),
        ),
        (
            "budgetPolicy",
            ("maxQueries", "maxAmapCalls", "maxWebSeedQueries", "resultLimit"),
            (),
        ),
    ):
        policy = _mapping(payload.get(policy_key))
        safe_policy: dict[str, Any] = {}
        for key in numeric_keys:
            value = _safe_nonnegative_number(policy.get(key))
            if value is not None:
                safe_policy[key] = value
        for key in boolean_keys:
            if isinstance(policy.get(key), bool):
                safe_policy[key] = policy[key]
        if safe_policy:
            projected[policy_key] = safe_policy

    fallback = _mapping(payload.get("fallbackPolicy"))
    safe_fallback: dict[str, Any] = {}
    for key in ("status", "reasonCode"):
        value = _safe_identifier(fallback.get(key))
        if value is not None:
            safe_fallback[key] = value
    for key in (
        "allowGenericScenic",
        "allowSemanticBroadening",
        "requiresUserVisibleDegradedState",
    ):
        if isinstance(fallback.get(key), bool):
            safe_fallback[key] = fallback[key]
    if safe_fallback:
        projected["fallbackPolicy"] = safe_fallback
    projected["excludedPhysicalPoiCount"] = len(
        [item for item in payload.get("excludedPhysicalPoiIds") or [] if str(item)]
    )
    return projected


def project_pool_report_for_trace(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project a pool report for persisted planning events and API previews."""

    projected = _project_pool_report_scalar_evidence(report)
    for key in (
        "queryPlanIds",
        "queryPlanModes",
        "queryProviderKeys",
    ):
        values = _safe_identifier_list(report.get(key), 32)
        if values:
            projected[key] = values
    for key in (
        "selectedCanonicalEntities",
        "selectedMealFamilies",
    ):
        values = _safe_bounded_text_list(report.get(key), 64, 160)
        if values:
            projected[key] = values
    rejected_counts = _project_count_map(report.get("rejectedReasonCounts"))
    if rejected_counts:
        projected["rejectedReasonCounts"] = rejected_counts
    budget = _project_amap_call_budget(report.get("amapCallBudget"))
    if budget:
        projected["amapCallBudget"] = budget
    shortcut = _project_coverage_shortcut(report.get("coverageShortcutEvidence"))
    if shortcut:
        projected["coverageShortcutEvidence"] = shortcut
    profile = project_search_profile(report.get("searchProfile"))
    if profile:
        projected["searchProfile"] = profile
    for key in ("safeCandidates", "selectedCandidates", "topCandidates"):
        if isinstance(report.get(key), list):
            projected[key] = [
                candidate
                for item in report[key][:8]
                if isinstance(item, Mapping)
                for candidate in [_project_candidate(item)]
                if candidate
            ]
    provider_debug = _project_provider_debug(report.get("providerDebug"))
    if provider_debug:
        projected["providerDebug"] = provider_debug
    if isinstance(report.get("webDiscovery"), Mapping):
        projected["webDiscovery"] = _project_web_discovery(report["webDiscovery"])
    return projected


def project_pool_report_for_checkpoint(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep resumable identity/evidence without persisting the execution contract."""

    projected = _project_pool_report_scalar_evidence(report)
    shortcut = _project_coverage_shortcut(report.get("coverageShortcutEvidence"))
    if shortcut:
        projected["coverageShortcutEvidence"] = shortcut
    if isinstance(report.get("webDiscovery"), Mapping):
        projected["webDiscovery"] = _project_web_discovery(report["webDiscovery"])
    profile = project_search_profile(report.get("searchProfile"))
    if not profile:
        profile = project_search_profile(report.get("searchProfileTrace"))
    if profile:
        # Deliberately use a non-executable key.  A projected profile omits
        # keywords and cannot be passed back to the provider adapter.
        raw_profile = _mapping(report.get("searchProfile") or report.get("searchProfileTrace"))
        profile["excludedPhysicalPoiIds"] = _safe_identifier_list(
            raw_profile.get("excludedPhysicalPoiIds"),
            64,
        )
        semantic_contract = checkpoint_semantic_contract_evidence(
            report,
            profile,
        )
        if semantic_contract:
            profile["semanticContractEvidence"] = semantic_contract
            profile["semanticContractFingerprint"] = checkpoint_semantic_contract_fingerprint(semantic_contract)
        projected["searchProfileTrace"] = profile
        projected["checkpointEvidenceProjectionVersion"] = _CHECKPOINT_EVIDENCE_VERSION
    for key in ("safeCandidates", "selectedCandidates", "topCandidates"):
        candidates = report.get(key)
        if isinstance(candidates, list):
            projected[key] = [
                candidate
                for item in candidates[:64]
                if isinstance(item, Mapping)
                for candidate in [_project_checkpoint_candidate(item)]
                if candidate
            ]
    return projected


def project_grounding_report_for_persistence(
    grounding_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild persisted waiting state from fixed safe evidence fields."""

    projected: dict[str, Any] = {}
    if isinstance(grounding_report.get("rateLimited"), bool):
        projected["rateLimited"] = grounding_report["rateLimited"]
    for key in ("rateLimitReason", "resultState", "routeStatus"):
        value = _safe_identifier(grounding_report.get(key))
        if value is not None:
            projected[key] = value
    for key in (
        "persistableSegmentCount",
        "routeAnchorRequiredCount",
        "routeAnchorSelectedCount",
    ):
        value = _safe_nonnegative_number(grounding_report.get(key))
        if value is not None:
            projected[key] = value
    warnings = _safe_reason_codes(grounding_report.get("warnings"))
    if warnings:
        projected["warnings"] = warnings
    unresolved_days = [
        day
        for raw_day in grounding_report.get("unresolvedDays") or []
        for day in [_safe_positive_int(raw_day)]
        if day is not None
    ]
    if unresolved_days:
        projected["unresolvedDays"] = unresolved_days[:64]
    pool_reports = grounding_report.get("poolReports")
    if isinstance(pool_reports, list):
        projected["poolReports"] = [
            project_pool_report_for_checkpoint(report) for report in pool_reports[:64] if isinstance(report, Mapping)
        ]
    for key in (
        "unresolvedSlots",
        "dayReadiness",
        "requiredIntentCoverage",
        "blockingRequiredIntents",
    ):
        safe_value = _project_scope_count_records(grounding_report.get(key))
        if safe_value:
            projected[key] = safe_value
    planning_preview = _project_planning_preview(grounding_report.get("planningPreview"))
    if planning_preview:
        projected["planningPreview"] = planning_preview
    route_repair = _project_canonical_candidate_list(grounding_report.get("routeRepairGroundingEvidence"))
    if route_repair:
        projected["routeRepairGroundingEvidence"] = route_repair
    for key in (
        "partialAnchorGroundingAudit",
        "partialAnchorGroundingProjectionAudit",
    ):
        audit = _project_grounding_audit(grounding_report.get(key))
        if audit:
            projected[key] = audit
    pipeline_context = _project_scope_count_record(grounding_report.get("pipelineContext"))
    if pipeline_context:
        for key in (
            "checkpointExpansionResume",
            "checkpointPortfolioResume",
        ):
            if isinstance(
                _mapping(grounding_report.get("pipelineContext")).get(key),
                bool,
            ):
                pipeline_context[key] = grounding_report["pipelineContext"][key]
        projected["pipelineContext"] = pipeline_context
    for key in (
        "performance",
        "portfolioStagingPerformance",
        "portfolioCandidateDiscovery",
    ):
        performance = _project_performance(grounding_report.get(key))
        if performance:
            projected[key] = performance
    return projected


def checkpoint_semantic_contract_evidence(
    report: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the non-executable semantic contract bound to checkpoint reuse."""

    safe_profile = project_search_profile(profile)
    required_identifiers = (
        "schemaVersion",
        "profileId",
        "profileFingerprint",
        "poolId",
        "briefId",
        "planningSlotId",
        "experienceFamily",
        "activityMode",
        "intentType",
        "requirementLevel",
        "entityBindingMode",
        "exclusionFingerprint",
        "executionFingerprint",
    )
    if any(not safe_profile.get(key) for key in required_identifiers):
        return {}
    if safe_profile.get("schemaVersion") != "poi-search-profile-v1":
        return {}
    for key in (
        "profileFingerprint",
        "exclusionFingerprint",
        "executionFingerprint",
    ):
        if not _HEX_FINGERPRINT_RE.fullmatch(str(safe_profile.get(key) or "")):
            return {}

    query_plans: list[dict[str, Any]] = []
    for plan in safe_profile.get("queryPlans") or []:
        if not isinstance(plan, Mapping):
            return {}
        provider_keys = _safe_identifier_list(
            plan.get("providerCategoryKeys"),
            12,
        )
        plan_id = _safe_identifier(plan.get("planId"))
        mode = _safe_identifier(plan.get("mode"))
        anchor_policy = _safe_identifier(plan.get("anchorPolicy"))
        fallback_level = _safe_nonnegative_number(plan.get("fallbackLevel"))
        if (
            not plan_id
            or not mode
            or not anchor_policy
            or not provider_keys
            or fallback_level is None
            or not isinstance(plan.get("requiresAmapGrounding"), bool)
            or not isinstance(plan.get("stopWhenTargetReached"), bool)
        ):
            return {}
        query_plans.append(
            {
                "planId": plan_id,
                "mode": mode,
                "providerCategoryKeys": sorted(set(provider_keys)),
                "anchorPolicy": anchor_policy,
                "fallbackLevel": fallback_level,
                "requiresAmapGrounding": plan["requiresAmapGrounding"],
                "stopWhenTargetReached": plan["stopWhenTargetReached"],
            }
        )
    if not query_plans:
        return {}

    coverage = _required_policy(
        safe_profile.get("coveragePolicy"),
        number_keys=("targetCount", "evidenceTargetCount"),
        bool_keys=(
            "distinctPhysicalPoiRequired",
            "stopWhenTargetReached",
        ),
    )
    budget = _required_policy(
        safe_profile.get("budgetPolicy"),
        number_keys=(
            "maxQueries",
            "maxAmapCalls",
            "maxWebSeedQueries",
            "resultLimit",
        ),
    )
    fallback = _required_fallback_policy(safe_profile.get("fallbackPolicy"))
    if not coverage or not budget or not fallback:
        return {}
    if coverage["evidenceTargetCount"] < coverage["targetCount"]:
        return {}

    raw_profile = _mapping(profile)
    excluded_ids = _safe_identifier_list(
        raw_profile.get("excludedPhysicalPoiIds"),
        64,
    )
    if len(excluded_ids) != _safe_nonnegative_number(safe_profile.get("excludedPhysicalPoiCount")) or len(
        set(excluded_ids)
    ) != len(excluded_ids):
        return {}

    required_slot_ids = _safe_identifier_list(
        report.get("requiredSlotIds"),
        64,
    )
    if not required_slot_ids or len(set(required_slot_ids)) != len(required_slot_ids):
        return {}
    slot_days_raw = report.get("slotDayNumbers")
    if not isinstance(slot_days_raw, Mapping):
        return {}
    slot_day_numbers: dict[str, int] = {}
    for slot_id in required_slot_ids:
        day_number = _safe_positive_int(slot_days_raw.get(slot_id))
        if day_number is None:
            return {}
        slot_day_numbers[slot_id] = day_number
    profile_slot_id = str(safe_profile.get("planningSlotId") or "")
    if profile_slot_id not in required_slot_ids:
        return {}

    scope = {
        "city": safe_profile["city"],
        "briefId": safe_profile["briefId"],
        "poolId": safe_profile["poolId"],
        "planningSlotId": profile_slot_id,
        "requiredSlotIds": sorted(required_slot_ids),
        "slotDayNumbers": {key: slot_day_numbers[key] for key in sorted(slot_day_numbers)},
        "targetCount": coverage["targetCount"],
        "intentType": safe_profile["intentType"],
        "requirementLevel": safe_profile["requirementLevel"],
    }
    for profile_key, report_key in (
        ("city", "city"),
        ("briefId", "briefId"),
        ("poolId", "poolId"),
        ("intentType", "intentType"),
        ("requirementLevel", "requirementLevel"),
    ):
        expected = str(safe_profile.get(profile_key) or "").strip()
        actual = str(report.get(report_key) or "").strip()
        if expected.casefold() != actual.casefold():
            return {}
    if report.get("targetCount") != coverage["targetCount"]:
        return {}

    candidate_evidence: list[dict[str, Any]] = []
    for raw_candidate in report.get("selectedCandidates") or []:
        if not isinstance(raw_candidate, Mapping):
            return {}
        if not _candidate_source_scope_is_compatible(raw_candidate):
            return {}
        candidate = _project_checkpoint_candidate(raw_candidate)
        if not _candidate_contract_is_complete(
            candidate,
            profile=safe_profile,
            required_slot_ids=set(required_slot_ids),
            slot_day_numbers=slot_day_numbers,
        ):
            return {}
        candidate_evidence.append(candidate)
    candidate_evidence.sort(
        key=lambda item: (
            str(item.get("planningSlotId") or ""),
            int(item.get("dayNumber") or 0),
            str(item.get("amapId") or ""),
        )
    )

    original_family = _safe_identifier(safe_profile.get("originalExperienceFamily"))
    return {
        "schemaVersion": _SEMANTIC_CONTRACT_SCHEMA,
        "profile": {
            "profileId": safe_profile["profileId"],
            "profileFingerprint": safe_profile["profileFingerprint"],
            "experienceFamily": safe_profile["experienceFamily"],
            "originalExperienceFamily": original_family,
            "activityMode": safe_profile["activityMode"],
            "entityBindingMode": safe_profile["entityBindingMode"],
        },
        "queryPlans": query_plans,
        "fallbackPolicy": fallback,
        "coveragePolicy": coverage,
        "budgetPolicy": budget,
        "exclusion": {
            "exclusionFingerprint": safe_profile["exclusionFingerprint"],
            "excludedPhysicalPoiIds": excluded_ids,
            "excludedPhysicalPoiCount": len(excluded_ids),
        },
        "executionFingerprint": safe_profile["executionFingerprint"],
        "scope": scope,
        "selectedCanonicalEntities": candidate_evidence,
    }


def checkpoint_semantic_contract_fingerprint(
    evidence: Mapping[str, Any],
) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(evidence),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _project_pool_report_scalar_evidence(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    city = _safe_bounded_text(report.get("city"), 80)
    if city is not None:
        projected["city"] = city
    for key in _CHECKPOINT_POOL_IDENTIFIER_KEYS:
        value = _safe_identifier(report.get(key))
        if value is not None:
            projected[key] = value
    for key in _CHECKPOINT_POOL_NUMBER_KEYS:
        value = _safe_nonnegative_number(report.get(key))
        if value is not None:
            projected[key] = value
    for key in ("strictGenericPool", "requiredIntent"):
        if isinstance(report.get(key), bool):
            projected[key] = report[key]
    for key in (
        "requiredSlotIds",
        "resolvedSlotIds",
        "unresolvedSlotIds",
    ):
        if isinstance(report.get(key), list):
            projected[key] = _safe_identifier_list(report.get(key), 64)
    slot_day_numbers = _mapping(report.get("slotDayNumbers"))
    safe_slot_days = {
        slot_id: day_number
        for raw_slot_id, raw_day_number in list(slot_day_numbers.items())[:64]
        for slot_id in [_safe_identifier(raw_slot_id)]
        for day_number in [_safe_positive_int(raw_day_number)]
        if slot_id is not None and day_number is not None
    }
    if safe_slot_days:
        projected["slotDayNumbers"] = safe_slot_days
    return projected


def _project_provider_debug(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    for raw in value[:8]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {}
        source = _safe_bounded_text(raw.get("source"), 80)
        if source is not None:
            item["source"] = source
        for key in (
            "classifiedReason",
            "providerStatus",
            "infoCode",
            "status",
            "reason",
        ):
            identifier = _safe_identifier(raw.get(key))
            if identifier is not None:
                item[key] = identifier
        http_status = _safe_nonnegative_number(raw.get("httpStatusCode"))
        if http_status is not None:
            item["httpStatusCode"] = http_status
        if item:
            projected.append(item)
    return projected


def _project_count_map(value: Any) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for raw_key, raw_value in list(_mapping(value).items())[:64]:
        key = _safe_identifier(raw_key)
        count = _safe_nonnegative_number(raw_value)
        if key is not None and count is not None:
            result[key] = count
    return result


def _project_amap_call_budget(value: Any) -> dict[str, Any]:
    payload = _mapping(value)
    projected: dict[str, Any] = {}
    nested_keys = {
        "budget": (
            "amapPoiTextSearchMax",
            "amapPoiAroundSearchMax",
            "amapRouteRefreshMax",
            "amapTotalExternalCallsMax",
        ),
        "used": (
            "usedPlaceText",
            "usedPlaceAround",
            "usedRoute",
            "usedTotalExternal",
        ),
    }
    for section, allowed_keys in nested_keys.items():
        raw_section = _mapping(payload.get(section))
        safe_section = {
            key: number
            for key in allowed_keys
            for number in [_safe_nonnegative_number(raw_section.get(key))]
            if number is not None
        }
        if safe_section:
            projected[section] = safe_section
    for key in (
        "usedPlaceText",
        "usedPlaceAround",
        "usedRoute",
        "usedTotalExternal",
        "cacheHitCount",
        "duplicateExternalQueryCount",
        "reusedQueryCount",
        "newQueryCount",
        "skippedBecauseBudget",
        "cooldownRemainingSeconds",
    ):
        number = _safe_nonnegative_number(payload.get(key))
        if number is not None:
            projected[key] = number
    if isinstance(payload.get("rateLimited"), bool):
        projected["rateLimited"] = payload["rateLimited"]
    source = _safe_identifier(payload.get("source"))
    if source is not None:
        projected["source"] = source
    return projected


def _project_coverage_shortcut(value: Any) -> dict[str, Any]:
    payload = _mapping(value)
    projected: dict[str, Any] = {}
    fingerprint = _safe_identifier(payload.get("profileFingerprint"))
    if fingerprint is not None and _HEX_FINGERPRINT_RE.fullmatch(fingerprint):
        projected["profileFingerprint"] = fingerprint
    for key in (
        "candidateCount",
        "targetCount",
        "evidenceTargetCount",
        "excludedPhysicalPoiCount",
    ):
        number = _safe_nonnegative_number(payload.get(key))
        if number is not None:
            projected[key] = number
    return projected


def _project_candidate_provider_evidence(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    provider_type_code = _safe_bounded_text(
        candidate.get("providerTypeCode")
        or candidate.get("provider_type_code")
        or candidate.get("typecode"),
        64,
    )
    if provider_type_code is not None:
        projected["providerTypeCode"] = provider_type_code
    for target, aliases in (
        ("parentPoiId", ("parentPoiId", "parent_poi_id")),
        ("indoorParentPoiId", ("indoorParentPoiId", "indoor_parent_poi_id")),
    ):
        value = next(
            (
                candidate.get(alias)
                for alias in aliases
                if candidate.get(alias) is not None
            ),
            None,
        )
        safe_identifier = _safe_identifier(value)
        if safe_identifier is not None:
            projected[target] = safe_identifier
    business_status = _safe_bounded_text(
        candidate.get("businessStatus") or candidate.get("business_status"),
        120,
    )
    if business_status is not None:
        projected["businessStatus"] = business_status
    provider_receipt = str(
        candidate.get("providerQueryReceiptFingerprint")
        or candidate.get("provider_query_receipt_fingerprint")
        or ""
    ).strip().lower()
    if _HEX_FINGERPRINT_RE.fullmatch(provider_receipt):
        projected["providerQueryReceiptFingerprint"] = provider_receipt
    raw_queried_at = (
        candidate.get("providerQueriedAt")
        if candidate.get("providerQueriedAt") is not None
        else candidate.get("provider_queried_at")
    )
    isoformat = getattr(raw_queried_at, "isoformat", None)
    if callable(isoformat):
        raw_queried_at = isoformat()
    provider_queried_at = _safe_bounded_text(raw_queried_at, 64)
    if provider_queried_at is not None:
        projected["providerQueriedAt"] = provider_queried_at
    tags = candidate.get("tags")
    if isinstance(tags, str):
        tags = [item.strip() for item in re.split(r"[;,；，]", tags) if item.strip()]
    safe_tags = _safe_bounded_text_list(tags, 12, 80)
    if safe_tags:
        projected["tags"] = safe_tags
    safe_claims: list[dict[str, Any]] = []
    for raw_claim in list(candidate.get("sourceClaims") or [])[:8]:
        if not isinstance(raw_claim, Mapping):
            continue
        claim: dict[str, Any] = {}
        for key in (
            "claimKey",
            "claimType",
            "stance",
            "sourceType",
            "sourceName",
            "freshness",
            "summary",
        ):
            value = _safe_bounded_text(raw_claim.get(key), 500)
            if value is not None:
                claim[key] = value
        source_url_hash = str(raw_claim.get("sourceUrlHash") or "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{16,128}", source_url_hash):
            claim["sourceUrlHash"] = source_url_hash
        confidence = _safe_nonnegative_number(raw_claim.get("confidence"))
        if confidence is not None:
            claim["confidence"] = confidence
        supported_signals = _safe_bounded_text_list(
            raw_claim.get("supportedSignals"),
            8,
            80,
        )
        if supported_signals:
            claim["supportedSignals"] = supported_signals
        if claim:
            safe_claims.append(claim)
    if safe_claims:
        projected["sourceClaims"] = safe_claims
    return projected


def _project_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    projected.update(_project_candidate_provider_evidence(candidate))
    for key in (
        "name",
        "city",
        "category",
        "type",
        "providerType",
    ):
        value = _safe_bounded_text(candidate.get(key), 200)
        if value is not None:
            projected[key] = value
    for key in (
        "id",
        "amapId",
        "source",
        "briefId",
        "poolId",
        "planningSlotId",
        "sourceGoalId",
        "occurrenceId",
        "semanticDecision",
        "candidateSource",
        "searchProfileId",
        "searchProfileFingerprint",
        "experienceFamily",
        "activityMode",
        "queryPlanId",
        "searchMode",
        "sourceBriefId",
        "sourcePoolId",
        "sourcePlanningSlotId",
        "decision",
    ):
        value = _safe_identifier(candidate.get(key))
        if value is not None:
            projected[key] = value
    longitude = _safe_coordinate(candidate.get("longitude"), longitude=True)
    latitude = _safe_coordinate(candidate.get("latitude"), longitude=False)
    if longitude is not None:
        projected["longitude"] = longitude
    if latitude is not None:
        projected["latitude"] = latitude
    day_number = _safe_positive_int(candidate.get("dayNumber"))
    if day_number is not None:
        projected["dayNumber"] = day_number
    for key in (
        "confidence",
        "candidateScore",
        "localRouteScore",
        "semanticMatchScore",
        "providerConfidence",
        "fallbackLevel",
        "score",
    ):
        number = _safe_finite_number(candidate.get(key))
        if number is not None:
            projected[key] = number
    if isinstance(candidate.get("semanticPassed"), bool):
        projected["semanticPassed"] = candidate["semanticPassed"]
    for key in ("matchedSemanticFacets", "rejectedReasons"):
        values = _safe_identifier_list(candidate.get(key), 24)
        if values:
            projected[key] = values
    components = {
        key: number
        for raw_key, raw_value in list(_mapping(candidate.get("components")).items())[:32]
        for key in [_safe_identifier(raw_key)]
        for number in [_safe_finite_number(raw_value)]
        if key is not None and number is not None
    }
    if components:
        projected["components"] = components
    return projected


def _project_checkpoint_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source = _safe_identifier(candidate.get("source"))
    amap_id = _safe_identifier(candidate.get("amapId") or candidate.get("id"))
    longitude = _safe_coordinate(candidate.get("longitude"), longitude=True)
    latitude = _safe_coordinate(candidate.get("latitude"), longitude=False)
    if (
        source != "amap-place-search"
        or not amap_id
        or longitude is None
        or latitude is None
        or (longitude == 0 and latitude == 0)
    ):
        return {}
    projected: dict[str, Any] = {
        "id": amap_id,
        "amapId": amap_id,
        "source": source,
        "longitude": longitude,
        "latitude": latitude,
    }
    projected.update(_project_candidate_provider_evidence(candidate))
    for key in (
        "name",
        "city",
        "category",
        "type",
        "providerType",
        "district",
        "address",
    ):
        value = _safe_bounded_text(candidate.get(key), 200)
        if value is not None:
            projected[key] = value
    for key in (
        "briefId",
        "poolId",
        "planningSlotId",
        "sourceGoalId",
        "occurrenceId",
        "candidateSource",
        "searchProfileId",
        "searchProfileFingerprint",
        "exclusionFingerprint",
        "experienceFamily",
        "activityMode",
        "queryPlanId",
        "searchMode",
        "sourceBriefId",
        "sourcePoolId",
        "sourcePlanningSlotId",
    ):
        value = _safe_identifier(candidate.get(key))
        if value is not None:
            projected[key] = value
    day_number = _safe_positive_int(candidate.get("dayNumber"))
    if day_number is not None:
        projected["dayNumber"] = day_number
    for key in (
        "confidence",
        "candidateScore",
        "localRouteScore",
        "semanticMatchScore",
        "providerConfidence",
        "distanceMeters",
        "distance_meters",
        "fallbackLevel",
    ):
        value = _safe_nonnegative_number(candidate.get(key))
        if value is not None:
            projected[key] = value
    if isinstance(candidate.get("semanticPassed"), bool):
        projected["semanticPassed"] = candidate["semanticPassed"]
    matched_facets = _safe_identifier_list(
        candidate.get("matchedSemanticFacets"),
        24,
    )
    if matched_facets:
        projected["matchedSemanticFacets"] = matched_facets
    return projected


def _candidate_contract_is_complete(
    candidate: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    required_slot_ids: set[str],
    slot_day_numbers: Mapping[str, int],
) -> bool:
    slot_id = str(candidate.get("planningSlotId") or "")
    day_number = _safe_positive_int(candidate.get("dayNumber"))
    return bool(
        candidate.get("source") == "amap-place-search"
        and candidate.get("amapId")
        and candidate.get("name")
        and candidate.get("city")
        and (candidate.get("type") or candidate.get("providerType"))
        and slot_id in required_slot_ids
        and day_number == slot_day_numbers.get(slot_id)
        and str(candidate.get("briefId") or "") == str(profile.get("briefId") or "")
        and str(candidate.get("poolId") or "") == str(profile.get("poolId") or "")
        and candidate.get("semanticPassed") is True
        and _safe_nonnegative_number(candidate.get("semanticMatchScore")) is not None
        and str(candidate.get("searchProfileFingerprint") or "") == str(profile.get("profileFingerprint") or "")
        and str(candidate.get("exclusionFingerprint") or "") == str(profile.get("exclusionFingerprint") or "")
        and str(candidate.get("experienceFamily") or "") == str(profile.get("experienceFamily") or "")
        and str(candidate.get("activityMode") or "") == str(profile.get("activityMode") or "")
    )


def _candidate_source_scope_is_compatible(
    candidate: Mapping[str, Any],
) -> bool:
    for source_key, bound_key in (
        ("sourceBriefId", "briefId"),
        ("sourcePoolId", "poolId"),
        ("sourcePlanningSlotId", "planningSlotId"),
    ):
        if source_key not in candidate:
            continue
        source_value = _safe_identifier(candidate.get(source_key))
        bound_value = _safe_identifier(candidate.get(bound_key))
        if source_value is None or source_value != bound_value:
            return False
    return True


def _required_policy(
    value: Any,
    *,
    number_keys: tuple[str, ...],
    bool_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    policy = _mapping(value)
    projected: dict[str, Any] = {}
    for key in number_keys:
        number = _safe_nonnegative_number(policy.get(key))
        if number is None:
            return {}
        projected[key] = number
    for key in bool_keys:
        if not isinstance(policy.get(key), bool):
            return {}
        projected[key] = policy[key]
    return projected


def _required_fallback_policy(value: Any) -> dict[str, Any]:
    policy = _mapping(value)
    status = _safe_identifier(policy.get("status"))
    if not status:
        return {}
    projected: dict[str, Any] = {"status": status}
    reason_code = _safe_identifier(policy.get("reasonCode"))
    if reason_code is not None:
        projected["reasonCode"] = reason_code
    for key in (
        "allowGenericScenic",
        "allowSemanticBroadening",
        "requiresUserVisibleDegradedState",
    ):
        if not isinstance(policy.get(key), bool):
            return {}
        projected[key] = policy[key]
    return projected


def _project_scope_count_record(value: Any) -> dict[str, Any]:
    record = _mapping(value)
    projected: dict[str, Any] = {}
    for key in _GROUNDING_IDENTIFIER_KEYS:
        identifier = _safe_identifier(record.get(key))
        if identifier is not None:
            projected[key] = identifier
    for key in _GROUNDING_COUNT_KEYS:
        number = _safe_nonnegative_number(record.get(key))
        if number is not None:
            projected[key] = number
    reason_codes = _safe_reason_codes(record.get("reasonCodes"))
    if reason_codes:
        projected["reasonCodes"] = reason_codes
    return projected


def _project_scope_count_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [projected for item in value[:64] for projected in [_project_scope_count_record(item)] if projected]


def _project_planning_preview(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        days = [item for item in value[:64] if isinstance(item, Mapping)]
        return {
            "dayCount": len(days),
            "segmentCount": sum(len(item.get("segments") or []) for item in days),
        }
    preview = _mapping(value)
    projected = _project_scope_count_record(preview)
    days = preview.get("days")
    if isinstance(days, list):
        projected["dayCount"] = len(days[:64])
        projected["segmentCount"] = sum(
            len(item.get("segments") or []) for item in days[:64] if isinstance(item, Mapping)
        )
    return projected


def _project_canonical_candidate_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        projected
        for item in value[:64]
        if isinstance(item, Mapping)
        for projected in [_project_checkpoint_candidate(item)]
        if projected
    ]


def _project_grounding_audit(value: Any) -> dict[str, Any]:
    audit = _mapping(value)
    projected = _project_scope_count_record(audit)
    for key in ("partialEligible", "passed", "eligible"):
        if isinstance(audit.get(key), bool):
            projected[key] = audit[key]
    for key in ("anchors", "matchedAnchors", "unmatchedAnchors"):
        candidates = _project_canonical_candidate_list(audit.get(key))
        if candidates:
            projected[key] = candidates
    return projected


def _project_performance(value: Any) -> dict[str, Any]:
    performance = _mapping(value)
    projected = _project_scope_count_record(performance)
    for key in _GROUNDING_PERFORMANCE_KEYS:
        number = _safe_nonnegative_number(performance.get(key))
        if number is not None:
            projected[key] = number
    return projected


def _safe_reason_codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:64]:
        identifier = _safe_identifier(raw)
        if identifier is not None and identifier not in result:
            result.append(identifier)
    return result


def _project_web_discovery(discovery: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in (
        "status",
        "reasonCode",
        "failureReason",
        "searchProfileFingerprint",
        "exclusionFingerprint",
        "experienceFamily",
        "activityMode",
        "profileCoverageStatus",
    ):
        value = _safe_identifier(discovery.get(key))
        if value is not None:
            projected[key] = value
    for key in (
        "webQueryCount",
        "webSeedCount",
        "webSeedAmapGroundingCount",
        "webDiscoveryMs",
        "amapGroundingMs",
        "profileTargetCount",
        "profileResolvedSlotCount",
        "profileUnresolvedSlotCount",
    ):
        value = _safe_nonnegative_number(discovery.get(key))
        if value is not None:
            projected[key] = value
    if isinstance(discovery.get("reused"), bool):
        projected["reused"] = discovery["reused"]
    attempts = discovery.get("attempts")
    if isinstance(attempts, list):
        projected["attemptCount"] = len(attempts)
        projected["attempts"] = [_project_web_attempt(item) for item in attempts[:16] if isinstance(item, Mapping)]
    return projected


def _project_web_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in ("status", "providerStatus", "reasonCode"):
        value = _safe_identifier(attempt.get(key))
        if value is not None:
            projected[key] = value
    if isinstance(attempt.get("reused"), bool):
        projected["reused"] = attempt["reused"]
    provider_name = _safe_bounded_text(attempt.get("providerName"), 80)
    if provider_name is not None:
        projected["providerName"] = provider_name
    else:
        projected.pop("providerName", None)
    query = str(attempt.get("query") or "").strip()
    if query:
        projected["queryFingerprint"] = hashlib.sha256(query.encode("utf-8")).hexdigest()
    else:
        query_fingerprint = _safe_identifier(attempt.get("queryFingerprint"))
        if query_fingerprint and re.fullmatch(r"[0-9a-f]{64}", query_fingerprint):
            projected["queryFingerprint"] = query_fingerprint
    reason_codes = _safe_identifier_list(attempt.get("reasonCodes"), 12)
    if reason_codes:
        projected["reasonCodes"] = reason_codes
    scope = _mapping(attempt.get("scope"))
    safe_scope: dict[str, Any] = {}
    for key in ("briefId", "poolId", "planningSlotId", "sourceGoalId"):
        value = _safe_identifier(scope.get(key))
        if value is not None:
            safe_scope[key] = value
    day_number = _safe_nonnegative_number(scope.get("dayNumber"))
    if day_number is not None:
        safe_scope["dayNumber"] = day_number
    if safe_scope:
        projected["scope"] = safe_scope
    selected_candidates: list[dict[str, Any]] = []
    for raw_candidate in attempt.get("selectedCandidates") or []:
        candidate = _mapping(raw_candidate)
        amap_id = _safe_identifier(candidate.get("amapId"))
        name = _safe_bounded_text(candidate.get("name"), 160)
        candidate_scope = _mapping(candidate.get("scope"))
        if amap_id and name and candidate_scope == scope:
            selected_candidates.append({"amapId": amap_id, "name": name, "scope": safe_scope})
    if selected_candidates:
        projected["selectedCandidates"] = selected_candidates[:4]
    return projected


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(by_alias=True)
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if _IDENTIFIER_RE.fullmatch(normalized) and not _SENSITIVE_TEXT_RE.search(normalized) else None


def _safe_bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > limit
        or any(ord(character) < 32 for character in normalized)
        or "://" in normalized
        or "\\" in normalized
        or _SENSITIVE_TEXT_RE.search(normalized)
        or _ABSOLUTE_PATH_RE.search(normalized)
    ):
        return None
    return normalized


def _safe_identifier_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for raw in value[:limit] for item in [_safe_identifier(raw)] if item is not None]


def _safe_bounded_text_list(
    value: Any,
    limit: int,
    text_limit: int,
) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for raw in value[:limit] for item in [_safe_bounded_text(raw, text_limit)] if item is not None]


def _safe_finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return value


def _safe_nonnegative_number(value: Any) -> int | float | None:
    number = _safe_finite_number(value)
    if number is None or number < 0:
        return None
    return number


def _safe_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _safe_coordinate(value: Any, *, longitude: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    limit = 180 if longitude else 90
    return numeric if math.isfinite(numeric) and -limit <= numeric <= limit else None

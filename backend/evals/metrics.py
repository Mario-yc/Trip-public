from __future__ import annotations

from typing import Any

try:
    from backend.evals.replay import DEFAULT_TRACE_STAGES
except ModuleNotFoundError:
    from replay import DEFAULT_TRACE_STAGES  # type: ignore


_SEARCH_PROFILE_CORE_METRIC_KEYS = (
    "compiledSearchProfileCount",
    "distinctSearchProfileFingerprintCount",
    "familySearchProfileCoverageCount",
    "familySearchSemanticMismatchCount",
    "genericScenicCollapseCount",
    "profileCoverageShortcutHitCount",
    "invalidCoverageShortcutCount",
    "semanticCandidateAcceptedCount",
    "semanticCandidateRejectedCount",
    "familySpecificAmapCandidateCount",
    "webSeedGroundedCandidateCount",
    "duplicateExcludedBeforeRouteCount",
    "routePreflightAvoidedBySemanticFilterCount",
)

_SEARCH_PROFILE_METRIC_KEYS = (
    *_SEARCH_PROFILE_CORE_METRIC_KEYS,
    "profileLineageMismatchCount",
    "uniqueSearchProfileIdCount",
    "uniqueSearchProfileScopeCount",
    "familyActivitySearchContractCount",
    "familyActivitySearchContractCoverageCount",
    "profileFingerprintContractCollisionCount",
    "profileCompileEventMismatchCount",
    "profileSemanticContractMismatchCount",
    "profileExclusionMismatchCount",
    "profileCompiledCheckpointMismatchCount",
    "profileCandidateLineageMismatchCount",
    "profileAdapterMismatchCount",
    "profileDbBaselineMismatchCount",
    "nonCanonicalAmapProfileCandidateCount",
    "profileCheckpointCount",
    "profileCheckpointReportCount",
    "profilePlanningRunReferenceCount",
    "profilePlanningRunLoadedCount",
    "profilePlanningRunMetricEventCount",
    "profilePlanningRunMetricCycleCount",
    "profilePlanningRunStagedVerifierCycleCount",
    "profileExcludedPhysicalPoiCount",
    "profileCoverageShortcutEvidenceCount",
    "profileAdapterCallCount",
    "profileAdapterPlanAdoptionCount",
    "profileWebProviderCallCount",
    "profileAmapProviderCallCount",
    "profilePortfolioGenerateCallCount",
    "profilePortfolioStageCallCount",
    "profilePortfolioVerifierCallCount",
    "profilePortfolioVerifierFailureCount",
    "preAdoptionVersionWriteCount",
    "preAdoptionPatchWriteCount",
    "preAdoptionRouteWriteCount",
)


def summarize_results(results: list[dict[str, Any]], repeat: int = 1) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.get("passed"))
    failed = total - passed
    step_counts = [int(result.get("stepCount") or 0) for result in results]
    durations = [int(result.get("traceDurationMs") or 0) for result in results]
    latencies = sorted(int(result.get("latencyMs") or 0) for result in results)
    stage_coverage = _stage_coverage(results)
    avg_steps = round(sum(step_counts) / total, 2) if total else 0.0
    stability_results = [result for result in results if result.get("stabilityMetricEligible")]
    search_profile_results = [
        result
        for result in results
        if result.get("searchProfileMetricEligible")
        or any(int(result.get(key) or 0) for key in _SEARCH_PROFILE_CORE_METRIC_KEYS)
    ]
    stability_by_case = _stability_by_case(stability_results)
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "passRate": round(passed / total, 4) if total else 0.0,
        "repeat": repeat,
        "passAtK": _pass_at_k(results, repeat),
        "failures": [
            {
                "id": result.get("id"),
                "reason": result.get("failureReason") or "unknown failure",
            }
            for result in results
            if not result.get("passed")
        ],
        "avgSteps": avg_steps,
        "avgTraceDurationMs": round(sum(durations) / total, 2) if total else 0.0,
        "p95LatencyMs": _percentile(latencies, 0.95),
        "invalidPatchCount": sum(int(result.get("invalidPatchCount") or 0) for result in results),
        "verifierFailures": sum(int(result.get("verifierFailures") or 0) for result in results),
        "toolCallCount": sum(int(result.get("toolCallCount") or 0) for result in results),
        "failedToolCallCount": sum(int(result.get("failedToolCallCount") or 0) for result in results),
        "traceReplayFailures": sum(1 for result in results if result.get("traceReplay", {}).get("passed") is False),
        "stageCoverage": stage_coverage,
        "stabilitySampleCount": len(stability_results),
        "searchProfileSampleCount": len(search_profile_results),
        "timelineCreatedRate": _boolean_rate(stability_results, "timelineCreated"),
        "partialTimelineRate": _boolean_rate(stability_results, "partialTimeline"),
        "fullTimelineRate": _boolean_rate(stability_results, "fullTimeline"),
        "nightViewCardinalityMismatchCount": sum(
            1 for result in stability_results if result.get("nightViewCardinalityMismatch")
        ),
        "nightViewPersistedOverAllocationCount": sum(
            1 for result in stability_results if result.get("nightViewPersistedOverAllocation")
        ),
        "directiveRepairCount": sum(int(result.get("directiveRepairCount") or 0) for result in stability_results),
        "deterministicFallbackCount": sum(
            int(result.get("deterministicFallbackCount") or 0) for result in stability_results
        ),
        "cardinalityTerminalFailureCount": sum(
            1 for result in stability_results if result.get("cardinalityTerminalFailure")
        ),
        "groundingTerminalFailureCount": sum(
            1 for result in stability_results if result.get("groundingTerminalFailure")
        ),
        "continuationScopeDriftCount": _continuation_scope_drift_count(stability_results),
        "unexpectedVersionWriteCount": sum(1 for result in stability_results if result.get("unexpectedVersionWrite")),
        "unexpectedPatchWriteCount": sum(1 for result in stability_results if result.get("unexpectedPatchWrite")),
        "unexpectedRouteWriteCount": sum(1 for result in stability_results if result.get("unexpectedRouteWrite")),
        "fakeOrNonAmapRouteAnchorCount": sum(
            int(result.get("fakeOrNonAmapRouteAnchorCount") or 0) for result in stability_results
        ),
        "unexpectedWriteCount": sum(1 for result in stability_results if result.get("unexpectedWrite")),
        "webDiscoveryAttemptedRate": _boolean_rate(stability_results, "webDiscoveryAttempted"),
        "candidateBudgetExceededCount": sum(1 for result in stability_results if result.get("candidateBudgetExceeded")),
        "requiredBudgetExceededCount": sum(
            int(result.get("requiredBudgetExceededCount") or 0) for result in stability_results
        ),
        "explicitMealBudgetExceededCount": sum(
            int(result.get("explicitMealBudgetExceededCount") or 0) for result in stability_results
        ),
        "sameDayDuplicatePoiCount": sum(
            int(result.get("sameDayDuplicatePoiCount") or 0) for result in stability_results
        ),
        "crossDayReuseCount": sum(int(result.get("crossDayReuseCount") or 0) for result in stability_results),
        "visibleProposalCountDistribution": _value_distribution(stability_results, "visibleProposalCount"),
        "proposalOrderMismatchCount": _proposal_order_mismatch_count(stability_results),
        "webOnlyFinalPoiCount": sum(int(result.get("webOnlyFinalPoiCount") or 0) for result in stability_results),
        "fakeCoordinateCount": sum(int(result.get("fakeCoordinateCount") or 0) for result in stability_results),
        "maxConcurrentBriefWorkers": max(
            (int(result.get("maxConcurrentBriefWorkers") or 0) for result in stability_results),
            default=0,
        ),
        **_search_profile_metric_totals(search_profile_results),
        "stabilityByCase": stability_by_case,
    }


def count_verifier_failures(planning_steps: list[dict[str, Any]]) -> int:
    failures = 0
    for step in planning_steps:
        if step.get("type") != "verify":
            continue
        metadata = step.get("metadata") or {}
        hard_failures = metadata.get("hardFailures") or metadata.get("hard_failures") or []
        if step.get("status") == "failed" or metadata.get("passed") is False or hard_failures:
            failures += 1
    return failures


def count_tool_calls(planning_steps: list[dict[str, Any]]) -> int:
    return sum(1 for step in planning_steps if _is_tool_call(step))


def count_failed_tool_calls(planning_steps: list[dict[str, Any]]) -> int:
    return sum(1 for step in planning_steps if _is_tool_call(step) and step.get("status") == "failed")


def _is_tool_call(step: dict[str, Any]) -> bool:
    return step.get("type") == "tool" or bool(step.get("toolName"))


def _stage_coverage(results: list[dict[str, Any]]) -> dict[str, int]:
    expected_stages = {
        str(stage)
        for result in results
        for stage in (result.get("traceReplay") or {}).get("expectedStages") or []
        if str(stage)
    }
    coverage = {stage: 0 for stage in sorted(expected_stages or set(DEFAULT_TRACE_STAGES))}
    for result in results:
        replay = result.get("traceReplay") or {}
        counts = replay.get("stageCounts") or {}
        for stage in coverage:
            if int(counts.get(stage) or 0) > 0:
                coverage[stage] += 1
    return coverage


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * percentile))))
    return values[index]


def _pass_at_k(results: list[dict[str, Any]], repeat: int) -> float:
    if repeat <= 1:
        return round(sum(1 for result in results if result.get("passed")) / len(results), 4) if results else 0.0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("id")), []).append(result)
    passed_cases = sum(
        1 for attempts in grouped.values() if any(attempt.get("passed") for attempt in attempts[:repeat])
    )
    return round(passed_cases / len(grouped), 4) if grouped else 0.0


def _boolean_rate(results: list[dict[str, Any]], key: str) -> float:
    return round(sum(1 for result in results if result.get(key) is True) / len(results), 4) if results else 0.0


def _stability_by_case(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("id")), []).append(result)
    return {
        case_id: {
            "sampleCount": len(attempts),
            "timelineCreatedRate": _boolean_rate(attempts, "timelineCreated"),
            "partialTimelineRate": _boolean_rate(attempts, "partialTimeline"),
            "fullTimelineRate": _boolean_rate(attempts, "fullTimeline"),
            "nightViewCardinalityMismatchCount": sum(
                1 for attempt in attempts if attempt.get("nightViewCardinalityMismatch")
            ),
            "nightViewPersistedOverAllocationCount": sum(
                1 for attempt in attempts if attempt.get("nightViewPersistedOverAllocation")
            ),
            "directiveRepairCount": sum(int(attempt.get("directiveRepairCount") or 0) for attempt in attempts),
            "deterministicFallbackCount": sum(
                int(attempt.get("deterministicFallbackCount") or 0) for attempt in attempts
            ),
            "cardinalityTerminalFailureCount": sum(
                1 for attempt in attempts if attempt.get("cardinalityTerminalFailure")
            ),
            "groundingTerminalFailureCount": sum(1 for attempt in attempts if attempt.get("groundingTerminalFailure")),
            "continuationScopeDriftCount": _continuation_scope_drift_count(attempts),
            "unexpectedVersionWriteCount": sum(1 for attempt in attempts if attempt.get("unexpectedVersionWrite")),
            "unexpectedPatchWriteCount": sum(1 for attempt in attempts if attempt.get("unexpectedPatchWrite")),
            "unexpectedRouteWriteCount": sum(1 for attempt in attempts if attempt.get("unexpectedRouteWrite")),
            "fakeOrNonAmapRouteAnchorCount": sum(
                int(attempt.get("fakeOrNonAmapRouteAnchorCount") or 0) for attempt in attempts
            ),
            "unexpectedWriteCount": sum(1 for attempt in attempts if attempt.get("unexpectedWrite")),
            "webDiscoveryAttemptedRate": _boolean_rate(attempts, "webDiscoveryAttempted"),
            "candidateBudgetExceededCount": sum(1 for attempt in attempts if attempt.get("candidateBudgetExceeded")),
            "requiredBudgetExceededCount": sum(
                int(attempt.get("requiredBudgetExceededCount") or 0) for attempt in attempts
            ),
            "explicitMealBudgetExceededCount": sum(
                int(attempt.get("explicitMealBudgetExceededCount") or 0) for attempt in attempts
            ),
            "sameDayDuplicatePoiCount": sum(int(attempt.get("sameDayDuplicatePoiCount") or 0) for attempt in attempts),
            "crossDayReuseCount": sum(int(attempt.get("crossDayReuseCount") or 0) for attempt in attempts),
            "visibleProposalCountDistribution": _value_distribution(attempts, "visibleProposalCount"),
            "proposalOrderMismatchCount": _proposal_order_mismatch_count(attempts),
            "webOnlyFinalPoiCount": sum(int(attempt.get("webOnlyFinalPoiCount") or 0) for attempt in attempts),
            "fakeCoordinateCount": sum(int(attempt.get("fakeCoordinateCount") or 0) for attempt in attempts),
            "maxConcurrentBriefWorkers": max(
                (int(attempt.get("maxConcurrentBriefWorkers") or 0) for attempt in attempts),
                default=0,
            ),
            **_search_profile_metric_totals(
                attempts,
                keys=_SEARCH_PROFILE_CORE_METRIC_KEYS,
            ),
        }
        for case_id, attempts in sorted(grouped.items())
    }


def _search_profile_metric_totals(
    results: list[dict[str, Any]],
    *,
    keys: tuple[str, ...] = _SEARCH_PROFILE_METRIC_KEYS,
) -> dict[str, int]:
    return {key: sum(int(result.get(key) or 0) for result in results) for key in keys}


def _continuation_scope_drift_count(results: list[dict[str, Any]]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("id") or ""), []).append(result)
    drift_count = 0
    for attempts in grouped.values():
        fingerprints = [str(attempt.get("pendingSlotScopeFingerprint") or "") for attempt in attempts]
        baseline = next((fingerprint for fingerprint in fingerprints if fingerprint), "")
        for attempt, fingerprint in zip(attempts, fingerprints):
            if attempt.get("continuationScopeDrift"):
                drift_count += 1
            elif attempt.get("partialTimeline") and not fingerprint:
                drift_count += 1
            elif baseline and fingerprint and fingerprint != baseline:
                drift_count += 1
    return drift_count


def _value_distribution(results: list[dict[str, Any]], key: str) -> dict[str, int]:
    distribution: dict[str, int] = {}
    for result in results:
        value = str(int(result.get(key) or 0))
        distribution[value] = distribution.get(value, 0) + 1
    return dict(sorted(distribution.items()))


def _proposal_order_mismatch_count(results: list[dict[str, Any]]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("id") or ""), []).append(result)
    mismatches = 0
    for attempts in grouped.values():
        baseline = next(
            (
                tuple(item.get("visibleProposalBriefIds") or [])
                for item in attempts
                if item.get("visibleProposalBriefIds")
            ),
            (),
        )
        mismatches += sum(
            1 for item in attempts if baseline and tuple(item.get("visibleProposalBriefIds") or []) != baseline
        )
    return mismatches

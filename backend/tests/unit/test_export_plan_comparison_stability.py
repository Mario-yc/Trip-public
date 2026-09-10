from __future__ import annotations

import copy

import pytest

from backend.evals.export_plan_comparison_stability import (
    _delta_distance,
    _iteration_passed,
    _representative_trace_has_forbidden_content,
    _representative_trace_valid,
)


def _valid_backend_metrics() -> dict[str, object]:
    return {
        "initialProviderTimeoutObserved": True,
        "timeoutFailureCount": 1,
        "firstStreamEventSequenceValid": True,
        "persistedSafeChoiceCount": 1,
        "safePartialCreated": True,
        "timelineCreated": True,
        "comparisonProjectionCount": 1,
        "firstVisiblePlanBlockedByLaterPlans": False,
        "laterVerifiedPlanAppended": True,
        "laterVerifiedProposalCount": 2,
        "comparisonPlanCount": 3,
        "distinctCompletedBriefCount": 3,
        "distinctVisibleProposalBriefCount": 3,
        "portfolioPendingSlotCountSequence": [2, 1, 0],
        "safePartialWriteDelta": [1, 1, 0],
        "previewWriteDelta": [0, 0, 0],
        "adoptionWriteDelta": [0, 0, 0],
        "expansionWriteDeltas": [[0, 0, 0], [0, 0, 0]],
        "expansionFocusBriefIds": ["brief_two", "brief_three"],
        "persistedExpansionChoiceCount": 2,
        "checkpointExpansionResumeCount": 1,
        "expansionControllerCallDelta": 0,
        "expansionInitialPlanProviderCallDelta": 0,
        "expansionCreativePortfolioProviderCallDelta": 0,
        "expansionDeterministicPortfolioFallbackCount": 1,
        "globalPortfolioRebuildCount": 0,
        "exactChoiceWriteDeltas": [[1, 1, 0], [1, 1, 0]],
        "duplicateChoiceWriteDelta": [0, 0, 0],
        "fakeOrNonAmapAnchorCount": 0,
        "comparisonScopeDriftCount": 0,
        "comparisonPortfolioCount": 1,
        "briefReissueCount": 0,
        "briefScopeDriftCount": 0,
        "rootPortfolioDriftCount": 0,
        "canonicalDuplicateExpansionCount": 0,
        "pendingSlotScopeDriftCount": 0,
        "activeTimelineProjectionMismatchCount": 0,
        "rawInternalErrorCount": 0,
    }


def _valid_frontend_metrics() -> dict[str, object]:
    return {
        "comparisonAutoNavigated": True,
        "autoNavigationCount": 1,
        "projectedPlanCount": 3,
        "distinctProposalCount": 3,
        "stableColorMapping": True,
        "rootPortfolioDriftCount": 0,
    }


def test_iteration_passed_accepts_two_checkpoint_expansions_and_three_distinct_briefs():
    assert _iteration_passed(_valid_backend_metrics(), _valid_frontend_metrics())


def test_delta_distance_counts_matching_and_mismatched_write_evidence_on_python_39():
    assert _delta_distance([0, 0, 0], [0, 0, 0]) == 0
    assert _delta_distance([1, 1, 0], [1, 1, 0]) == 0
    assert _delta_distance([1, 0, 2], [0, 0, 0]) == 3
    assert _delta_distance([1, 0], [0, 0, 0]) == 1


def test_representative_trace_requires_three_briefs_exact_expansion_and_real_amap_anchors():
    trace = {
        "plans": [
            {
                "kind": "safe_partial",
                "briefId": "partial_brief",
                "groundedAnchors": [
                    {
                        "source": "amap-place-search",
                        "amapId": "B0REAL1",
                        "longitude": 116.3,
                        "latitude": 39.9,
                        "briefId": "partial_brief",
                        "poolId": "pool_partial",
                        "planningSlotId": "slot_partial_day_1",
                        "dayNumber": 1,
                        "sourceGoalId": "goal_partial",
                    }
                ],
            },
            {
                "kind": "verified_proposal",
                "briefId": "brief_two",
                "canonicalSignature": "signature-two",
                "groundedAnchors": [],
            },
            {
                "kind": "verified_proposal",
                "briefId": "brief_three",
                "canonicalSignature": "signature-three",
                "groundedAnchors": [],
            },
        ],
        "expansions": [
            {
                "expansionFocusMode": "discover_next",
                "checkpointExpansionResume": False,
                "writeDelta": [0, 0, 0],
            },
            {
                "expansionFocusMode": "exact",
                "checkpointExpansionResume": True,
                "writeDelta": [0, 0, 0],
            },
        ],
        "slotChoices": [{"duplicate": True, "writeDelta": [0, 0, 0]}],
        "pairwiseEvidence": {
            "minPairwiseStructuralDistance": 0.5,
            "minPairwisePhysicalPoiJaccardDistance": 0.5,
            "minDirectionalNewPoiFraction": 0.5,
            "uncommittedProposalCount": 2,
            "uncommittedProposalWriteDelta": [0, 0, 0],
        },
        "writes": {
            "safePartial": [1, 1, 0],
            "expansions": [[0, 0, 0], [0, 0, 0]],
            "exactSlotChoices": [[1, 1, 0], [1, 1, 0]],
            "duplicate": [0, 0, 0],
        },
        "providerCalls": {
            "controllerExpansionDelta": 0,
            "initialPlanExpansionDelta": 0,
            "creativePortfolioProviderExpansionDelta": 0,
        },
        "phaseEvents": [
            {"captureOrder": 0, "phase": "safe_partial"},
            {"captureOrder": 1, "phase": "portfolio_expansion"},
        ],
    }

    assert _representative_trace_valid(trace)
    spoofed = copy.deepcopy(trace)
    spoofed["plans"][0]["groundedAnchors"][0]["source"] = "amap-fake"
    assert not _representative_trace_valid(spoofed)

    insufficient_physical_novelty = copy.deepcopy(trace)
    insufficient_physical_novelty["pairwiseEvidence"][
        "minPairwisePhysicalPoiJaccardDistance"
    ] = 0.0
    assert not _representative_trace_valid(insufficient_physical_novelty)

    insufficient_new_poi_fraction = copy.deepcopy(trace)
    insufficient_new_poi_fraction["pairwiseEvidence"][
        "minDirectionalNewPoiFraction"
    ] = 0.2
    assert not _representative_trace_valid(insufficient_new_poi_fraction)


def test_representative_trace_rejects_sensitive_keys_urls_and_absolute_paths():
    assert not _representative_trace_has_forbidden_content(
        {"phase": "portfolio_staging", "durationMs": 25}
    )
    assert _representative_trace_has_forbidden_content({"providerPayload": {"status": 1}})
    assert _representative_trace_has_forbidden_content({"path": r"C:\\Users\\secret.json"})
    assert _representative_trace_has_forbidden_content({"source": "https://example.com/image.jpg"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distinctCompletedBriefCount", 2),
        ("distinctVisibleProposalBriefCount", 2),
        ("comparisonPlanCount", 2),
        ("expansionFocusBriefIds", ["brief_two", "brief_two"]),
        ("persistedExpansionChoiceCount", 1),
        ("checkpointExpansionResumeCount", 0),
        ("expansionControllerCallDelta", 1),
        ("expansionInitialPlanProviderCallDelta", 1),
        ("expansionCreativePortfolioProviderCallDelta", 1),
        ("expansionDeterministicPortfolioFallbackCount", 2),
        ("globalPortfolioRebuildCount", 1),
        ("expansionWriteDeltas", [[0, 0, 0], [1, 0, 0]]),
        ("comparisonPortfolioCount", 2),
        ("briefReissueCount", 1),
        ("briefScopeDriftCount", 1),
        ("rootPortfolioDriftCount", 1),
        ("canonicalDuplicateExpansionCount", 1),
    ],
)
def test_iteration_passed_rejects_incomplete_or_rebuilt_expansion_evidence(
    field: str,
    value: object,
):
    backend = copy.deepcopy(_valid_backend_metrics())
    backend[field] = value

    assert not _iteration_passed(backend, _valid_frontend_metrics())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("projectedPlanCount", 2),
        ("distinctProposalCount", 2),
        ("stableColorMapping", False),
        ("rootPortfolioDriftCount", 1),
    ],
)
def test_iteration_passed_rejects_incomplete_frontend_comparison_evidence(
    field: str,
    value: object,
):
    frontend = copy.deepcopy(_valid_frontend_metrics())
    frontend[field] = value

    assert not _iteration_passed(_valid_backend_metrics(), frontend)

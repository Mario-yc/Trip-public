"""Run the fixed recorded/mock comparison journey and export attributable evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Optional


ROOT = Path(__file__).resolve().parents[2]
ZERO_DELTA = [0, 0, 0]
ONE_COMMIT_DELTA = [1, 1, 0]
EXPECTED_EXPANSION_COUNT = 2
EXPECTED_CHECKPOINT_RESUME_COUNT = 1
EXPECTED_DETERMINISTIC_FALLBACK_COUNT = 1
MIN_DISTINCT_COMPARISON_BRIEFS = 3
MIN_DIRECTIONAL_NEW_POI_FRACTION = 0.25
FORBIDDEN_TRACE_KEY_FRAGMENTS = (
    "prompt",
    "reasoning",
    "providerpayload",
    "responseheaders",
    "credential",
    "secret",
    "apikey",
    "authorization",
    "cookie",
)


def _run(
    command: list[str],
    *,
    cwd: Path,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[dict[str, object], str]:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    child_env = {
        **os.environ,
        "FORCE_COLOR": "0",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        **(extra_env or {}),
    }
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env,
    )
    duration_ms = round((time.perf_counter() - started) * 1000)
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    output = output.replace(str(ROOT), "<repo>").replace(ROOT.as_posix(), "<repo>")
    if completed.returncode != 0:
        raise SystemExit(output)
    return {
        "startedAt": started_at,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "durationMs": duration_ms,
        "exitCode": completed.returncode,
    }, output


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def _metric(output: str, prefix: str) -> dict[str, object]:
    matches = []
    for line in output.splitlines():
        marker_index = line.find(prefix)
        if marker_index >= 0:
            matches.append(line[marker_index + len(prefix):])
    if len(matches) != 1:
        raise SystemExit(f"expected one {prefix} line, found {len(matches)}")
    return json.loads(matches[0])


def _iteration_passed(
    backend: dict[str, object],
    frontend: dict[str, object],
) -> bool:
    expansion_write_deltas = backend.get("expansionWriteDeltas")
    expansion_focus_brief_ids = backend.get("expansionFocusBriefIds")
    exact_choice_write_deltas = backend.get("exactChoiceWriteDeltas")
    if not isinstance(expansion_write_deltas, list) or not isinstance(
        expansion_focus_brief_ids, list
    ):
        return False
    if not isinstance(exact_choice_write_deltas, list):
        return False
    expansion_focus_ids = [
        str(item or "").strip() for item in expansion_focus_brief_ids
    ]
    return bool(
        backend.get("initialProviderTimeoutObserved")
        and int(backend.get("timeoutFailureCount") or 0) >= 1
        and backend.get("firstStreamEventSequenceValid")
        and int(backend.get("persistedSafeChoiceCount") or 0) == 1
        and backend.get("safePartialCreated")
        and backend.get("timelineCreated")
        and int(backend.get("comparisonProjectionCount") or 0) >= 1
        and not backend.get("firstVisiblePlanBlockedByLaterPlans")
        and backend.get("laterVerifiedPlanAppended")
        and int(backend.get("laterVerifiedProposalCount") or 0)
        >= EXPECTED_EXPANSION_COUNT
        and int(backend.get("comparisonPlanCount") or 0)
        >= MIN_DISTINCT_COMPARISON_BRIEFS
        and int(backend.get("distinctCompletedBriefCount") or 0)
        >= MIN_DISTINCT_COMPARISON_BRIEFS
        and int(backend.get("distinctVisibleProposalBriefCount") or 0)
        >= MIN_DISTINCT_COMPARISON_BRIEFS
        and backend.get("portfolioPendingSlotCountSequence") == [2, 1, 0]
        and backend.get("safePartialWriteDelta") == ONE_COMMIT_DELTA
        and backend.get("previewWriteDelta") == ZERO_DELTA
        and backend.get("adoptionWriteDelta") == ZERO_DELTA
        and len(expansion_write_deltas) == EXPECTED_EXPANSION_COUNT
        and all(delta == ZERO_DELTA for delta in expansion_write_deltas)
        and len(expansion_focus_ids) == EXPECTED_EXPANSION_COUNT
        and all(expansion_focus_ids)
        and len(set(expansion_focus_ids)) == EXPECTED_EXPANSION_COUNT
        and int(backend.get("persistedExpansionChoiceCount") or 0)
        == EXPECTED_EXPANSION_COUNT
        and int(backend.get("checkpointExpansionResumeCount") or 0)
        == EXPECTED_CHECKPOINT_RESUME_COUNT
        and int(backend.get("expansionControllerCallDelta") or 0) == 0
        and int(backend.get("expansionInitialPlanProviderCallDelta") or 0) == 0
        and int(backend.get("expansionCreativePortfolioProviderCallDelta") or 0)
        == 0
        and int(backend.get("expansionDeterministicPortfolioFallbackCount") or 0)
        == EXPECTED_DETERMINISTIC_FALLBACK_COUNT
        and int(backend.get("globalPortfolioRebuildCount") or 0) == 0
        and all(delta == ONE_COMMIT_DELTA for delta in exact_choice_write_deltas)
        and len(exact_choice_write_deltas) == 2
        and backend.get("duplicateChoiceWriteDelta") == ZERO_DELTA
        and int(backend.get("fakeOrNonAmapAnchorCount") or 0) == 0
        and int(backend.get("comparisonScopeDriftCount") or 0) == 0
        and int(backend.get("comparisonPortfolioCount") or 0) == 1
        and int(backend.get("briefReissueCount") or 0) == 0
        and int(backend.get("briefScopeDriftCount") or 0) == 0
        and int(backend.get("rootPortfolioDriftCount") or 0) == 0
        and int(backend.get("canonicalDuplicateExpansionCount") or 0) == 0
        and int(backend.get("pendingSlotScopeDriftCount") or 0) == 0
        and int(backend.get("activeTimelineProjectionMismatchCount") or 0) == 0
        and int(backend.get("rawInternalErrorCount") or 0) == 0
        and frontend.get("comparisonAutoNavigated")
        and int(frontend.get("autoNavigationCount") or 0) == 1
        and int(frontend.get("projectedPlanCount") or 0)
        >= MIN_DISTINCT_COMPARISON_BRIEFS
        and int(frontend.get("distinctProposalCount") or 0)
        >= MIN_DISTINCT_COMPARISON_BRIEFS
        and frontend.get("stableColorMapping") is True
        and int(frontend.get("rootPortfolioDriftCount") or 0) == 0
    )


def _representative_trace_valid(trace: object) -> bool:
    if not isinstance(trace, dict):
        return False
    plans = trace.get("plans")
    expansions = trace.get("expansions")
    slot_choices = trace.get("slotChoices")
    writes = trace.get("writes")
    provider_calls = trace.get("providerCalls")
    pairwise = trace.get("pairwiseEvidence")
    phase_events = trace.get("phaseEvents")
    if not all(
        isinstance(item, list)
        for item in (plans, expansions, slot_choices)
    ) or not isinstance(writes, dict) or not isinstance(provider_calls, dict) or not isinstance(
        pairwise, dict
    ) or not isinstance(phase_events, list):
        return False
    brief_ids = {
        str(item.get("briefId") or "")
        for item in plans
        if isinstance(item, dict) and str(item.get("briefId") or "")
    }
    proposal_signatures = {
        str(item.get("canonicalSignature") or "")
        for item in plans
        if isinstance(item, dict)
        and item.get("kind") == "verified_proposal"
        and str(item.get("canonicalSignature") or "")
    }
    all_anchors = [
        anchor
        for plan in plans
        if isinstance(plan, dict)
        for anchor in plan.get("groundedAnchors") or []
        if isinstance(anchor, dict)
    ]
    return bool(
        len(plans) >= MIN_DISTINCT_COMPARISON_BRIEFS
        and len(brief_ids) >= MIN_DISTINCT_COMPARISON_BRIEFS
        and len(proposal_signatures) >= EXPECTED_EXPANSION_COUNT
        and float(pairwise.get("minPairwiseStructuralDistance") or 0) > 0
        and float(pairwise.get("minPairwisePhysicalPoiJaccardDistance") or 0) > 0
        and float(pairwise.get("minDirectionalNewPoiFraction") or 0)
        >= MIN_DIRECTIONAL_NEW_POI_FRACTION
        and int(pairwise.get("uncommittedProposalCount") or 0)
        == EXPECTED_EXPANSION_COUNT
        and pairwise.get("uncommittedProposalWriteDelta") == ZERO_DELTA
        and len(expansions) == EXPECTED_EXPANSION_COUNT
        and expansions[0].get("expansionFocusMode") == "discover_next"
        and expansions[0].get("checkpointExpansionResume") is False
        and expansions[1].get("expansionFocusMode") == "exact"
        and expansions[1].get("checkpointExpansionResume") is True
        and all(item.get("writeDelta") == ZERO_DELTA for item in expansions)
        and writes.get("safePartial") == ONE_COMMIT_DELTA
        and writes.get("expansions") == [ZERO_DELTA, ZERO_DELTA]
        and writes.get("exactSlotChoices")
        == [ONE_COMMIT_DELTA, ONE_COMMIT_DELTA]
        and writes.get("duplicate") == ZERO_DELTA
        and provider_calls.get("controllerExpansionDelta") == 0
        and provider_calls.get("initialPlanExpansionDelta") == 0
        and provider_calls.get("creativePortfolioProviderExpansionDelta") == 0
        and all_anchors
        and all(
            anchor.get("source") == "amap-place-search"
            and anchor.get("amapId")
            and anchor.get("longitude") is not None
            and anchor.get("latitude") is not None
            and anchor.get("briefId")
            and anchor.get("poolId")
            and anchor.get("planningSlotId")
            and anchor.get("dayNumber")
            and anchor.get("sourceGoalId")
            for anchor in all_anchors
        )
        and [event.get("captureOrder") for event in phase_events]
        == list(range(len(phase_events)))
        and not _representative_trace_has_forbidden_content(trace)
        and any(
            item.get("duplicate") is True and item.get("writeDelta") == ZERO_DELTA
            for item in slot_choices
            if isinstance(item, dict)
        )
    )


def _representative_trace_has_forbidden_content(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
            if any(fragment in normalized_key for fragment in FORBIDDEN_TRACE_KEY_FRAGMENTS):
                return True
            if _representative_trace_has_forbidden_content(item):
                return True
        return False
    if isinstance(value, list):
        return any(_representative_trace_has_forbidden_content(item) for item in value)
    if isinstance(value, str):
        return bool(
            re.search(r"(?i)(?:[a-z]:\\|\\\\|file://|https?://|www\.|\.env(?:\b|/))", value)
        )
    return False


def _delta_distance(actual: object, expected: list[int]) -> int:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return sum(abs(item) for item in expected) + 1
    try:
        return sum(
            abs(int(value) - wanted)
            for value, wanted in zip(actual, expected)
        )
    except (TypeError, ValueError):
        return sum(abs(item) for item in expected) + 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if _git("status", "--short"):
        raise SystemExit("worktree must be clean before evidence export")

    backend_selector = (
        "backend/tests/contract/test_agent_sessions_api.py::"
        "test_rule_safe_timeout_with_uninvoked_lite_choice_stream_publishes_partial_comparison_from_empty_session"
    )
    frontend_title = "portfolio visible stream appends A to B to C"
    iteration_runs = []
    representative_trace = None
    for iteration in range(30):
        backend, backend_output = _run(
            [sys.executable, "-m", "pytest", backend_selector, "-q", "-s"],
            cwd=ROOT,
            extra_env={"TRIP_STABILITY_TRACE_EXPORT": "1"} if iteration == 0 else None,
        )
        frontend, frontend_output = _run(
            [
                "npm.cmd",
                "test",
                "--",
                "--run",
                "tests/integration/agentStreamingInteraction.test.tsx",
                "-t",
                frontend_title,
            ],
            cwd=ROOT / "frontend",
        )
        backend_metrics = _metric(
            backend_output,
            "TRIP_RULE_SAFE_COMPARISON_STABILITY_METRICS=",
        )
        if iteration == 0:
            representative_trace = _metric(
                backend_output,
                "TRIP_RULE_SAFE_COMPARISON_REPRESENTATIVE_TRACE=",
            )
            if not _representative_trace_valid(representative_trace):
                raise SystemExit("representative trace failed integrity validation")
        frontend_metrics = _metric(
            frontend_output,
            "TRIP_RULE_SAFE_COMPARISON_FRONTEND_METRICS=",
        )
        iteration_passed = _iteration_passed(backend_metrics, frontend_metrics)
        if not iteration_passed:
            raise SystemExit(
                json.dumps(
                    {
                        "iteration": iteration + 1,
                        "backend": backend_metrics,
                        "frontend": frontend_metrics,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        iteration_runs.append(
            {
                "iteration": iteration + 1,
                "passed": iteration_passed,
                "metrics": {
                    "backend": backend_metrics,
                    "frontend": frontend_metrics,
                },
                "commandRuns": {"backend": backend, "frontend": frontend},
            }
        )
    if _git("status", "--short"):
        raise SystemExit("worktree changed during evidence export")

    def rate(predicate) -> float:
        return sum(bool(predicate(item)) for item in iteration_runs) / len(iteration_runs)

    unexpected_write_count = 0
    expansion_unexpected_write_count = 0
    for item in iteration_runs:
        metrics = item["metrics"]["backend"]
        for key, expected in (
            ("safePartialWriteDelta", [1, 1, 0]),
            ("previewWriteDelta", [0, 0, 0]),
            ("adoptionWriteDelta", [0, 0, 0]),
            ("duplicateChoiceWriteDelta", [0, 0, 0]),
        ):
            unexpected_write_count += _delta_distance(metrics.get(key), expected)
        for delta in metrics.get("expansionWriteDeltas") or []:
            distance = _delta_distance(delta, ZERO_DELTA)
            expansion_unexpected_write_count += distance
            unexpected_write_count += distance
        for delta in metrics.get("exactChoiceWriteDeltas") or []:
            unexpected_write_count += _delta_distance(delta, ONE_COMMIT_DELTA)

    report = {
        "schemaVersion": "trip-rule-safe-comparison-stability-v2",
        "gitCommit": _git("rev-parse", "HEAD"),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "scenarioType": "recorded_mock_fixed_public_stream_path",
        "providerMetadata": {
            "controller": "recorded-timeout-then-recovery",
            "poi": "recorded-amap-fixture",
            "route": "recorded-amap-fixture",
            "liveExternalCalls": 0,
        },
        "commands": {
            "backend": (
                "trip\\Scripts\\python.exe -m pytest "
                + backend_selector
                + " -q -s"
            ),
            "frontend": (
                "npm test -- --run tests/integration/agentStreamingInteraction.test.tsx "
                f"-t {frontend_title}"
            ),
        },
        "iterations": iteration_runs,
        "representativeTrace": representative_trace,
        "aggregate": {
            "passed": sum(bool(item["passed"]) for item in iteration_runs),
            "failed": sum(not bool(item["passed"]) for item in iteration_runs),
            "providerTimeoutObservedRate": rate(
                lambda item: item["metrics"]["backend"]["initialProviderTimeoutObserved"]
                and item["metrics"]["backend"]["timeoutFailureCount"] >= 1
            ),
            "persistedSafeChoiceRate": rate(
                lambda item: item["metrics"]["backend"]["persistedSafeChoiceCount"] == 1
            ),
            "timelineCreatedRate": rate(
                lambda item: item["metrics"]["backend"]["timelineCreated"]
            ),
            "partialTimelineRate": rate(
                lambda item: item["metrics"]["backend"]["safePartialCreated"]
            ),
            "streamEvidenceRate": rate(
                lambda item: item["metrics"]["backend"]["firstStreamEventSequenceValid"]
            ),
            "comparisonAutoNavigationRate": rate(
                lambda item: item["metrics"]["frontend"]["comparisonAutoNavigated"]
                and item["metrics"]["frontend"]["autoNavigationCount"] == 1
            ),
            "laterPlanAppendRate": rate(
                lambda item: item["metrics"]["backend"]["laterVerifiedPlanAppended"]
            ),
            "threeDistinctBriefCompletionRate": rate(
                lambda item: item["metrics"]["backend"]["distinctCompletedBriefCount"]
                >= MIN_DISTINCT_COMPARISON_BRIEFS
            ),
            "threePlanComparisonRate": rate(
                lambda item: item["metrics"]["backend"]["comparisonPlanCount"]
                >= MIN_DISTINCT_COMPARISON_BRIEFS
                and item["metrics"]["frontend"]["projectedPlanCount"]
                >= MIN_DISTINCT_COMPARISON_BRIEFS
            ),
            "checkpointExpansionReuseRate": rate(
                lambda item: item["metrics"]["backend"][
                    "checkpointExpansionResumeCount"
                ]
                == EXPECTED_CHECKPOINT_RESUME_COUNT
                and item["metrics"]["backend"]["persistedExpansionChoiceCount"]
                == EXPECTED_EXPANSION_COUNT
            ),
            "pendingSlotCompletionRate": rate(
                lambda item: item["metrics"]["backend"]["portfolioPendingSlotCountSequence"]
                == [2, 1, 0]
            ),
            "firstVisiblePlanBlockedByLaterPlansCount": sum(
                bool(item["metrics"]["backend"]["firstVisiblePlanBlockedByLaterPlans"])
                for item in iteration_runs
            ),
            "expansionUnexpectedWriteCount": expansion_unexpected_write_count,
            "unexpectedWriteCount": unexpected_write_count,
            "expansionControllerRecallCount": sum(
                int(item["metrics"]["backend"]["expansionControllerCallDelta"])
                for item in iteration_runs
            ),
            "expansionInitialPlanProviderRecallCount": sum(
                int(
                    item["metrics"]["backend"][
                        "expansionInitialPlanProviderCallDelta"
                    ]
                )
                for item in iteration_runs
            ),
            "expansionCreativePortfolioProviderRecallCount": sum(
                int(
                    item["metrics"]["backend"][
                        "expansionCreativePortfolioProviderCallDelta"
                    ]
                )
                for item in iteration_runs
            ),
            "expansionDeterministicPortfolioFallbackCount": sum(
                int(
                    item["metrics"]["backend"][
                        "expansionDeterministicPortfolioFallbackCount"
                    ]
                )
                for item in iteration_runs
            ),
            "globalPortfolioRebuildCount": sum(
                int(item["metrics"]["backend"]["globalPortfolioRebuildCount"])
                for item in iteration_runs
            ),
            "briefReissueCount": sum(
                int(item["metrics"]["backend"]["briefReissueCount"])
                for item in iteration_runs
            ),
            "briefScopeDriftCount": sum(
                int(item["metrics"]["backend"]["briefScopeDriftCount"])
                for item in iteration_runs
            ),
            "rootPortfolioDriftCount": sum(
                int(item["metrics"]["backend"]["rootPortfolioDriftCount"])
                + int(item["metrics"]["frontend"]["rootPortfolioDriftCount"])
                for item in iteration_runs
            ),
            "canonicalDuplicateExpansionCount": sum(
                int(
                    item["metrics"]["backend"][
                        "canonicalDuplicateExpansionCount"
                    ]
                )
                for item in iteration_runs
            ),
            "fakeAnchorCount": sum(
                int(item["metrics"]["backend"]["fakeOrNonAmapAnchorCount"])
                for item in iteration_runs
            ),
            "scopeDriftCount": sum(
                int(item["metrics"]["backend"]["comparisonScopeDriftCount"])
                + int(item["metrics"]["backend"]["pendingSlotScopeDriftCount"])
                for item in iteration_runs
            ),
            "activeTimelineProjectionMismatchCount": sum(
                int(item["metrics"]["backend"]["activeTimelineProjectionMismatchCount"])
                for item in iteration_runs
            ),
            "rawInternalErrorCount": sum(
                int(item["metrics"]["backend"]["rawInternalErrorCount"])
                for item in iteration_runs
            ),
            "replayFailureCount": sum(
                int(
                    item["metrics"]["backend"]["duplicateChoiceWriteDelta"] != [0, 0, 0]
                    or item["metrics"]["backend"]["activeTimelineProjectionMismatchCount"] != 0
                )
                for item in iteration_runs
            ),
        },
        "liveSmoke": {
            "deepSeek": "NOT_RUN(provider mode recorded/mock)",
            "amap": "NOT_RUN(provider mode recorded/mock)",
        },
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": output.name,
                "gitCommit": report["gitCommit"],
                "passed": len(iteration_runs),
                "aggregate": report["aggregate"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

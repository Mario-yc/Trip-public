import json
import re
import sqlite3

from backend.evals.metrics import count_failed_tool_calls, count_tool_calls, summarize_results
from backend.evals.run_offline import (
    CASES_DIR,
    _canonical_profile_ids,
    _collect_planning_steps,
    _compiler_fingerprint,
    _fake_or_non_amap_route_anchor_count,
    _latest_profile_replay_steps,
    _persisted_profile_metric_events,
    _profile_metric_cycle_count,
    _profile_staged_verifier_cycle_count,
    _scheduled_goal_counts,
    _search_profile_stability_metrics,
    _session_write_counts,
    _stability_failures,
)
from src.services.experience_search_profile_compiler import ExperienceSearchProfileCompiler


def _required_search_profile_case() -> dict:
    return {
        "id": "creative_portfolio_search_profile_recorded",
        "searchProfileEval": {
            "required": True,
            "minimumCompiledProfileCount": 2,
            "minimumFamilySpecificAmapCandidateCount": 2,
            "minimumSemanticCandidateRejectedCount": 1,
            "minimumDuplicateExcludedBeforeRouteCount": 1,
            "minimumCoverageShortcutHitCount": 1,
            "minimumExcludedPhysicalPoiCount": 1,
            "minimumAdapterPlanAdoptionCount": 1,
            "minimumWebProviderCallCount": 1,
            "minimumPlanningRunCount": 2,
            "minimumPlanningRunMetricEventCount": 4,
            "minimumPlanningRunMetricCycleCount": 2,
            "minimumPlanningRunStagedVerifierCycleCount": 2,
            "minimumPortfolioStageCallCount": 2,
            "minimumPortfolioVerifierCallCount": 2,
            "expectedPreAdoptionVersionWriteCount": 0,
            "expectedPreAdoptionPatchWriteCount": 0,
            "expectedPreAdoptionRouteWriteCount": 0,
        },
    }


def _passing_search_profile_metrics() -> dict:
    return {
        "searchProfileMetricEligible": True,
        "compiledSearchProfileCount": 2,
        "distinctSearchProfileFingerprintCount": 2,
        "uniqueSearchProfileIdCount": 2,
        "uniqueSearchProfileScopeCount": 2,
        "familyActivitySearchContractCount": 2,
        "familyActivitySearchContractCoverageCount": 2,
        "profileFingerprintContractCollisionCount": 0,
        "familySearchProfileCoverageCount": 2,
        "familySearchSemanticMismatchCount": 0,
        "genericScenicCollapseCount": 0,
        "profileCoverageShortcutHitCount": 1,
        "invalidCoverageShortcutCount": 0,
        "semanticCandidateRejectedCount": 1,
        "familySpecificAmapCandidateCount": 2,
        "duplicateExcludedBeforeRouteCount": 1,
        "profileLineageMismatchCount": 0,
        "profileCompileEventMismatchCount": 0,
        "profileSemanticContractMismatchCount": 0,
        "profileExclusionMismatchCount": 0,
        "profileCompiledCheckpointMismatchCount": 0,
        "profileCandidateLineageMismatchCount": 0,
        "profileAdapterMismatchCount": 0,
        "profileDbBaselineMismatchCount": 0,
        "nonCanonicalAmapProfileCandidateCount": 0,
        "profileCheckpointCount": 1,
        "profileCheckpointReportCount": 2,
        "profilePlanningRunReferenceCount": 2,
        "profilePlanningRunLoadedCount": 2,
        "profilePlanningRunMetricEventCount": 4,
        "profilePlanningRunMetricCycleCount": 2,
        "profilePlanningRunStagedVerifierCycleCount": 2,
        "profileExcludedPhysicalPoiCount": 1,
        "profileCoverageShortcutEvidenceCount": 1,
        "profileAdapterCallCount": 1,
        "profileAdapterPlanAdoptionCount": 1,
        "profileWebProviderCallCount": 1,
        "profileAmapProviderCallCount": 1,
        "profilePortfolioGenerateCallCount": 1,
        "profilePortfolioStageCallCount": 2,
        "profilePortfolioVerifierCallCount": 2,
        "profilePortfolioVerifierFailureCount": 0,
        "preAdoptionVersionWriteCount": 0,
        "preAdoptionPatchWriteCount": 0,
        "preAdoptionRouteWriteCount": 0,
    }


def test_eval_metrics_include_tool_counts_latency_and_pass_at_k():
    results = [
        {
            "id": "case_a",
            "passed": True,
            "stepCount": 3,
            "latencyMs": 10,
            "traceDurationMs": 5,
            "invalidPatchCount": 0,
            "verifierFailures": 0,
            "toolCallCount": 2,
            "failedToolCallCount": 0,
            "traceReplay": {"passed": True, "stageCounts": {"plan": 1, "verify": 1}},
        },
        {
            "id": "case_b",
            "passed": False,
            "failureReason": "bad patch",
            "stepCount": 5,
            "latencyMs": 100,
            "traceDurationMs": 20,
            "invalidPatchCount": 1,
            "verifierFailures": 1,
            "toolCallCount": 1,
            "failedToolCallCount": 1,
            "traceReplay": {"passed": False, "stageCounts": {"plan": 1}},
        },
    ]

    summary = summarize_results(results, repeat=1)

    assert summary["passRate"] == 0.5
    assert summary["passAtK"] == 0.5
    assert summary["toolCallCount"] == 3
    assert summary["failedToolCallCount"] == 1
    assert summary["p95LatencyMs"] == 100
    assert summary["invalidPatchCount"] == 1
    assert summary["verifierFailures"] == 1


def test_tool_call_counters_accept_tool_name_or_tool_type():
    steps = [
        {"type": "tool", "status": "completed"},
        {"toolName": "patch_itinerary", "status": "failed"},
        {"type": "verify", "status": "failed"},
    ]

    assert count_tool_calls(steps) == 2
    assert count_failed_tool_calls(steps) == 1


def test_offline_eval_extracts_profile_compile_and_discovery_metrics():
    steps = [
        {
            "type": "compile_experience_search_profile",
            "metadata": {
                "resultPreview": {
                    "compiledSearchProfileCount": 5,
                    "distinctSearchProfileFingerprintCount": 5,
                    "familySearchProfileCoverageCount": 4,
                    "familySearchSemanticMismatchCount": 1,
                    "genericScenicCollapseCount": 0,
                }
            },
        },
        {
            "type": "portfolio_candidate_discovery",
            "metadata": {
                "resultPreview": {
                    "profileCoverageShortcutHitCount": 2,
                    "invalidCoverageShortcutCount": 1,
                    "consumerAdmissionCoverageRejectedCount": 4,
                    "semanticCandidateAcceptedCount": 7,
                    "semanticCandidateRejectedCount": 3,
                    "familySpecificAmapCandidateCount": 6,
                    "webSeedGroundedCandidateCount": 2,
                    "duplicateExcludedBeforeRouteCount": 4,
                    "routePreflightAvoidedBySemanticFilterCount": 3,
                }
            },
        },
    ]

    assert _search_profile_stability_metrics(steps) == {
        "compiledSearchProfileCount": 5,
        "distinctSearchProfileFingerprintCount": 5,
        "familySearchProfileCoverageCount": 4,
        "familySearchSemanticMismatchCount": 1,
        "genericScenicCollapseCount": 0,
        "profileCoverageShortcutHitCount": 2,
        "invalidCoverageShortcutCount": 1,
        "consumerAdmissionCoverageRejectedCount": 4,
        "semanticCandidateAcceptedCount": 7,
        "semanticCandidateRejectedCount": 3,
        "familySpecificAmapCandidateCount": 6,
        "webSeedGroundedCandidateCount": 2,
        "duplicateExcludedBeforeRouteCount": 4,
        "routePreflightAvoidedBySemanticFilterCount": 3,
    }


def test_planning_step_collection_uses_latest_response_canonical_source():
    latest = [
        {"type": "compile_experience_search_profile", "timestamp": "t3"},
        {"type": "portfolio_candidate_discovery", "timestamp": "t4"},
    ]
    context = {
        "_responses": [
            {
                "planningSteps": [
                    {"type": "normalize_request", "timestamp": "t1"},
                    {"type": "deterministic_enrichment", "timestamp": "t2"},
                ]
            },
            {
                "planningSteps": latest,
                "toolEvents": [
                    {**latest[0], "timestamp": "duplicate-t3"},
                    {**latest[1], "timestamp": "duplicate-t4"},
                ],
                "assistantTurn": {"planningSteps": list(latest)},
                "planningRun": {"toolCalls": list(latest)},
            },
        ]
    }

    assert _collect_planning_steps(context) == latest


def test_persisted_profile_events_follow_assistant_turn_run_lineage(tmp_path):
    db_path = tmp_path / "profile-events.db"
    first_events = [
        {"type": "compile_experience_search_profile", "traceSummary": {"run": 1}},
        {"type": "portfolio_candidate_discovery", "traceSummary": {"run": 1}},
    ]
    second_events = [
        {"type": "compile_experience_search_profile", "traceSummary": {"run": 2}},
        {"type": "portfolio_candidate_discovery", "traceSummary": {"run": 2}},
    ]
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversation_turns (
                session_id TEXT,
                role TEXT,
                planning_run_id TEXT,
                turn_index INTEGER,
                created_at TEXT
            );
            CREATE TABLE planning_runs (
                id TEXT PRIMARY KEY,
                tool_calls_json TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO planning_runs (id, tool_calls_json) VALUES (?, ?)",
            [
                ("run-1", json.dumps(first_events)),
                ("run-2", json.dumps(second_events)),
                ("unrelated", json.dumps(second_events)),
            ],
        )
        connection.executemany(
            """INSERT INTO conversation_turns
               (session_id, role, planning_run_id, turn_index, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                ("session-1", "assistant", "run-1", 2, "t2"),
                ("session-1", "assistant", "run-1", 3, "t3"),
                ("session-1", "assistant", "run-2", 4, "t4"),
                ("session-2", "assistant", "unrelated", 1, "t1"),
                ("session-1", "user", "unrelated", 5, "t5"),
            ],
        )

    assert _persisted_profile_metric_events(db_path, "session-1") == [
        *first_events,
        *second_events,
    ]
    assert _latest_profile_replay_steps([*first_events, *second_events]) == second_events
    assert _profile_metric_cycle_count([first_events, list(reversed(second_events))]) == 1
    complete_pipeline = [
        *first_events,
        {
            "type": "portfolio_staging_performance",
            "traceSummary": {"briefMetrics": [{"verifierPassed": True}]},
        },
        {"type": "stage_plan_portfolio", "status": "completed"},
    ]
    failed_verifier_pipeline = [
        *second_events,
        {
            "type": "portfolio_staging_performance",
            "traceSummary": {"briefMetrics": [{"verifierPassed": False}]},
        },
        {"type": "stage_plan_portfolio", "status": "completed"},
    ]
    assert _profile_staged_verifier_cycle_count([complete_pipeline, failed_verifier_pipeline]) == 2

    incomplete_verifier_pipeline = [
        *second_events,
        {
            "type": "portfolio_staging_performance",
            "traceSummary": {"briefMetrics": [{"verifierPassed": True}, {}]},
        },
        {"type": "stage_plan_portfolio", "status": "completed"},
    ]
    nondict_verifier_pipeline = [
        *second_events,
        {
            "type": "portfolio_staging_performance",
            "traceSummary": {"briefMetrics": [{"verifierPassed": True}, "invalid-verifier-row"]},
        },
        {"type": "stage_plan_portfolio", "status": "completed"},
    ]
    incomplete_stage_pipeline = [
        *second_events,
        {
            "type": "portfolio_staging_performance",
            "traceSummary": {"briefMetrics": [{"verifierPassed": True}]},
        },
        {"type": "stage_plan_portfolio", "status": "failed"},
    ]
    assert (
        _profile_staged_verifier_cycle_count(
            [
                incomplete_verifier_pipeline,
                nondict_verifier_pipeline,
                incomplete_stage_pipeline,
            ]
        )
        == 0
    )


def test_required_search_profile_gate_rejects_missing_zero_and_tampered_lineage():
    case = _required_search_profile_case()

    assert _stability_failures({}, case) == ["required Search Profile metrics are missing"]

    zero_failures = _stability_failures(
        {"searchProfileMetricEligible": True},
        case,
    )
    assert "required Search Profile compile metrics are all zero" in zero_failures
    assert "Search Profile persisted compile/discovery events are missing" in zero_failures

    passing = _passing_search_profile_metrics()
    assert _stability_failures(passing, case) == []

    fingerprint_or_exclusion_tamper = {
        **passing,
        "profileLineageMismatchCount": 1,
    }
    assert "Search Profile event/checkpoint/provider lineage mismatch" in _stability_failures(
        fingerprint_or_exclusion_tamper,
        case,
    )

    coverage_tamper = {
        **passing,
        "invalidCoverageShortcutCount": 1,
    }
    assert "Search Profile coverage shortcut lineage is invalid" in _stability_failures(
        coverage_tamper,
        case,
    )

    dangling_second_run = {
        **passing,
        "profilePlanningRunLoadedCount": 1,
    }
    assert "Search Profile planning-run lineage is incomplete" in _stability_failures(
        dangling_second_run,
        case,
    )

    missing_second_cycle = {
        **passing,
        "profilePlanningRunMetricEventCount": 2,
        "profilePlanningRunMetricCycleCount": 1,
        "profilePlanningRunStagedVerifierCycleCount": 1,
        "profilePortfolioStageCallCount": 1,
        "profilePortfolioVerifierCallCount": 1,
    }
    missing_second_cycle_failures = _stability_failures(
        missing_second_cycle,
        case,
    )
    assert "Search Profile persisted compile/discovery events are missing" in missing_second_cycle_failures
    assert "Search Profile persisted replay cycle coverage is incomplete" in missing_second_cycle_failures
    assert "Search Profile per-run staging/verifier cycle coverage is incomplete" in missing_second_cycle_failures
    assert "production Creative Portfolio staging was not executed" in missing_second_cycle_failures
    assert "production Creative Portfolio verifier evidence is incomplete" in missing_second_cycle_failures


def test_search_profile_fingerprint_gate_allows_same_contract_across_unique_scopes():
    metrics = {
        **_passing_search_profile_metrics(),
        "compiledSearchProfileCount": 3,
        "distinctSearchProfileFingerprintCount": 2,
        "uniqueSearchProfileIdCount": 3,
        "uniqueSearchProfileScopeCount": 3,
        "familyActivitySearchContractCount": 2,
        "familyActivitySearchContractCoverageCount": 3,
        "profileFingerprintContractCollisionCount": 0,
        "familySearchProfileCoverageCount": 3,
        "profileCheckpointReportCount": 3,
    }

    assert _stability_failures(metrics, _required_search_profile_case()) == []

    collision = {
        **metrics,
        "distinctSearchProfileFingerprintCount": 1,
        "profileFingerprintContractCollisionCount": 1,
    }
    assert "Search Profile fingerprint contract coverage is inconsistent" in _stability_failures(
        collision,
        _required_search_profile_case(),
    )

    duplicate_scope = {**metrics, "uniqueSearchProfileScopeCount": 2}
    assert "Search Profile profile IDs/scopes are missing or duplicated" in _stability_failures(
        duplicate_scope,
        _required_search_profile_case(),
    )

    missing_activity = {
        **metrics,
        "familyActivitySearchContractCoverageCount": 2,
    }
    assert "Search Profile fingerprint contract coverage is inconsistent" in _stability_failures(
        missing_activity,
        _required_search_profile_case(),
    )


def test_generic_case_does_not_require_search_profile_metrics():
    assert _stability_failures({}, {"id": "legacy_generic_case"}) == []


def test_eval_recomputes_exclusion_fingerprint_with_compiler_canonicalization():
    raw_ids = [" AMAP_B ", "amap_b", "ＡＭＡＰ_A"]
    canonical_ids = _canonical_profile_ids(raw_ids)
    payload = {
        "schemaVersion": "poi-search-exclusions-v1",
        "excludedPhysicalPoiIds": canonical_ids,
    }

    assert canonical_ids == ExperienceSearchProfileCompiler._canonical_ids(raw_ids)
    assert _compiler_fingerprint(payload) == ExperienceSearchProfileCompiler._fingerprint(payload)


def test_recorded_profile_amap_fixture_uses_canonical_physical_ids():
    case = json.loads((CASES_DIR / "creative_portfolio_search_profile_recorded.json").read_text(encoding="utf-8"))
    amap_ids = [
        str(candidate.get("id") or "")
        for candidates in (case.get("mockAmapResponses") or {}).values()
        for candidate in candidates
        if isinstance(candidate, dict)
    ]

    assert amap_ids
    assert all(re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id) for amap_id in amap_ids)


def test_transient_plan_route_write_is_counted_and_fails_pre_adoption_gate(tmp_path):
    db_path = tmp_path / "transient-route.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversation_sessions (
                id TEXT PRIMARY KEY,
                active_plan_id TEXT
            );
            CREATE TABLE itinerary_versions (session_id TEXT);
            CREATE TABLE itinerary_patches (session_id TEXT);
            CREATE TABLE route_options (plan_id TEXT);
            INSERT INTO conversation_sessions (id, active_plan_id)
            VALUES ('sess_profile', 'active_plan');
            """
        )

    before = _session_write_counts(db_path, "sess_profile")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO route_options (plan_id) VALUES (?)",
            ("transient_non_active_plan",),
        )
    after = _session_write_counts(db_path, "sess_profile")

    assert after["route"] - before["route"] == 1
    metrics = {
        **_passing_search_profile_metrics(),
        "preAdoptionRouteWriteCount": after["route"] - before["route"],
    }
    assert "unexpected pre-adoption version/patch/route DB delta" in _stability_failures(
        metrics,
        _required_search_profile_case(),
    )


def test_eval_metrics_aggregate_timeline_cardinality_and_write_stability_by_case():
    results = [
        {
            "id": "singular",
            "passed": True,
            "stabilityMetricEligible": True,
            "timelineCreated": True,
            "partialTimeline": True,
            "nightViewCardinalityMismatch": False,
            "nightViewPersistedOverAllocation": False,
            "directiveRepairCount": 1,
            "deterministicFallbackCount": 1,
            "groundingTerminalFailure": False,
            "continuationScopeDrift": False,
            "pendingSlotScopeFingerprint": "brief|pool|slot|2",
            "unexpectedWrite": False,
            "compiledSearchProfileCount": 5,
            "distinctSearchProfileFingerprintCount": 5,
            "familySearchProfileCoverageCount": 5,
            "familySearchSemanticMismatchCount": 0,
            "genericScenicCollapseCount": 0,
            "profileCoverageShortcutHitCount": 2,
            "invalidCoverageShortcutCount": 1,
            "semanticCandidateAcceptedCount": 7,
            "semanticCandidateRejectedCount": 3,
            "familySpecificAmapCandidateCount": 6,
            "webSeedGroundedCandidateCount": 2,
            "duplicateExcludedBeforeRouteCount": 4,
            "routePreflightAvoidedBySemanticFilterCount": 3,
            "traceReplay": {"passed": True, "stageCounts": {}},
        },
        {
            "id": "every_night",
            "passed": False,
            "stabilityMetricEligible": True,
            "timelineCreated": False,
            "partialTimeline": False,
            "nightViewCardinalityMismatch": True,
            "nightViewPersistedOverAllocation": True,
            "cardinalityTerminalFailure": True,
            "groundingTerminalFailure": True,
            "continuationScopeDrift": True,
            "pendingSlotScopeFingerprint": "other|pool|slot|2",
            "unexpectedVersionWrite": True,
            "fakeOrNonAmapRouteAnchorCount": 1,
            "unexpectedWrite": True,
            "traceReplay": {"passed": True, "stageCounts": {}},
        },
    ]

    summary = summarize_results(results)

    assert summary["stabilitySampleCount"] == 2
    assert summary["searchProfileSampleCount"] == 1
    assert summary["timelineCreatedRate"] == 0.5
    assert summary["partialTimelineRate"] == 0.5
    assert summary["nightViewCardinalityMismatchCount"] == 1
    assert summary["nightViewPersistedOverAllocationCount"] == 1
    assert summary["directiveRepairCount"] == 1
    assert summary["deterministicFallbackCount"] == 1
    assert summary["cardinalityTerminalFailureCount"] == 1
    assert summary["groundingTerminalFailureCount"] == 1
    assert summary["continuationScopeDriftCount"] == 1
    assert summary["unexpectedVersionWriteCount"] == 1
    assert summary["fakeOrNonAmapRouteAnchorCount"] == 1
    assert summary["unexpectedWriteCount"] == 1
    assert summary["compiledSearchProfileCount"] == 5
    assert summary["distinctSearchProfileFingerprintCount"] == 5
    assert summary["familySearchProfileCoverageCount"] == 5
    assert summary["familySearchSemanticMismatchCount"] == 0
    assert summary["genericScenicCollapseCount"] == 0
    assert summary["profileCoverageShortcutHitCount"] == 2
    assert summary["invalidCoverageShortcutCount"] == 1
    assert summary["semanticCandidateAcceptedCount"] == 7
    assert summary["semanticCandidateRejectedCount"] == 3
    assert summary["familySpecificAmapCandidateCount"] == 6
    assert summary["webSeedGroundedCandidateCount"] == 2
    assert summary["duplicateExcludedBeforeRouteCount"] == 4
    assert summary["routePreflightAvoidedBySemanticFilterCount"] == 3
    assert summary["stabilityByCase"]["singular"] == {
        "sampleCount": 1,
        "timelineCreatedRate": 1.0,
        "partialTimelineRate": 1.0,
        "fullTimelineRate": 0.0,
        "nightViewCardinalityMismatchCount": 0,
        "nightViewPersistedOverAllocationCount": 0,
        "directiveRepairCount": 1,
        "deterministicFallbackCount": 1,
        "cardinalityTerminalFailureCount": 0,
        "groundingTerminalFailureCount": 0,
        "continuationScopeDriftCount": 0,
        "unexpectedVersionWriteCount": 0,
        "unexpectedPatchWriteCount": 0,
        "unexpectedRouteWriteCount": 0,
        "fakeOrNonAmapRouteAnchorCount": 0,
        "unexpectedWriteCount": 0,
        "webDiscoveryAttemptedRate": 0.0,
        "candidateBudgetExceededCount": 0,
        "requiredBudgetExceededCount": 0,
        "explicitMealBudgetExceededCount": 0,
        "sameDayDuplicatePoiCount": 0,
        "crossDayReuseCount": 0,
        "visibleProposalCountDistribution": {"0": 1},
        "proposalOrderMismatchCount": 0,
        "webOnlyFinalPoiCount": 0,
        "fakeCoordinateCount": 0,
        "maxConcurrentBriefWorkers": 0,
        "compiledSearchProfileCount": 5,
        "distinctSearchProfileFingerprintCount": 5,
        "familySearchProfileCoverageCount": 5,
        "familySearchSemanticMismatchCount": 0,
        "genericScenicCollapseCount": 0,
        "profileCoverageShortcutHitCount": 2,
        "invalidCoverageShortcutCount": 1,
        "semanticCandidateAcceptedCount": 7,
        "semanticCandidateRejectedCount": 3,
        "familySpecificAmapCandidateCount": 6,
        "webSeedGroundedCandidateCount": 2,
        "duplicateExcludedBeforeRouteCount": 4,
        "routePreflightAvoidedBySemanticFilterCount": 3,
    }


def test_scheduled_goal_counts_preserve_explicit_zero_in_controller_evidence():
    steps = [
        {
            "type": "agent_decision",
            "metadata": {
                "actionDirective": {
                    "type": "draft_itinerary",
                    "dayStrategies": [
                        {
                            "dayNumber": 1,
                            "requiredGoalIds": ["goal_night_view"],
                            "requiredGoalCounts": {"goal_night_view": 0},
                        }
                    ],
                }
            },
        }
    ]

    assert _scheduled_goal_counts(steps, "goal_night_view") == {}


def test_fake_anchor_metric_rejects_non_amap_source_and_reads_top_level_anchor_flag():
    body = {
        "itinerary": {
            "days": [
                {
                    "segments": [
                        {
                            "routeAnchor": True,
                            "poi": {
                                "amapId": "AMAP_FORGED",
                                "latitude": 39.9,
                                "longitude": 116.4,
                                "source": "foo",
                            },
                        },
                        {
                            "semanticMetadata": {"routeAnchor": True},
                            "poi": {
                                "amapId": "AMAP_VALID",
                                "latitude": 39.9,
                                "longitude": 116.4,
                                "source": "amap-place-search",
                            },
                        },
                        {
                            "routeAnchor": False,
                            "poi": {
                                "amapId": "AMAP_NON_ANCHOR",
                                "latitude": 39.9,
                                "longitude": 116.4,
                                "source": "foo",
                            },
                        },
                    ]
                }
            ]
        }
    }

    assert _fake_or_non_amap_route_anchor_count(body) == 1
